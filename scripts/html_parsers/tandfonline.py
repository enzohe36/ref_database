"""Taylor & Francis (tandfonline.com) HTML parser."""

# Note: tandfonline HTML uses unquoted attributes (class=foo not class="foo").
# All regex patterns must handle both quoted and unquoted attribute values.

import re
import urllib.parse
from html import unescape

from ._helpers import (
    _remove_nested_element,
    affiliation_from_email,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    remove_elements_by_id,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new window",
    "Display full size",
    "Download PDF",
    "Download MS Word",
    "Download Zip",
    "Download figure",
    "PubMed",
    "Web of Science",
    "Google Scholar",
)

# All NLM_sec div opening tags (any attribute order)
_ALL_SECTION_RE = re.compile(
    r'<div\s[^>]*class="?NLM_sec[^">\s]*[^>]*>', re.DOTALL
)

# Supplementary section patterns (kept after first references)
_SUPPLEMENTARY_RE = re.compile(
    r"supplement|extended data|source data|expanded view|appendix",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

# Lock the publisher's CSS to its narrow (≤ 1024 px) layout regardless of
# the viewer's actual viewport. Tandfonline (Silverchair) ships a single
# stylesheet whose desktop layout — multi-column grid, 100-px-wide vertical
# metrics sidebar, expanded gray-box journal-nav with cover image + submit
# buttons, larger title font — is entirely controlled by `@media (min-width:
# 1025px)` blocks; the corresponding narrow form lives in `@media (max-width:
# 1024px)` blocks (and the unconditional defaults). Forcing every desktop
# breakpoint OFF and every narrow breakpoint ON at any viewport collapses
# the head block, metrics widget, and article column into a single 720-style
# layout that scales by re-centering the page-body wrapper.
#
# Two transforms applied to every `<style>` block:
#   1. `@media (min-width: N) { rules }` for N ≥ 1025 → entire block deleted
#      (desktop rules never fire).
#   2. `@media (max-width: N) { rules }` for N ≥ 720 → `@media` wrapper
#      stripped, leaving rules unconditional (narrow rules always fire).
# Other media queries (orientation, prefers-color-scheme, print, max-width
# mobile breakpoints below 720) are left intact.
_MEDIA_THRESH_MIN_DESKTOP = 721   # delete `@media (min-width: N)` for N >= this
_MEDIA_THRESH_MAX_NARROW = 720    # unwrap `@media (max-width: N)` for N >= this


def _scan_balanced_block(text, open_idx):
    """Return the index just past the matching `}` for `{` at open_idx.

    Tracks brace depth, skipping over CSS strings ("..." or '...') and
    `url(...)` content (which can hold parens but not braces in valid
    CSS). Returns -1 if no matching brace found within the slice.
    """
    depth = 1
    i = open_idx + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c in '"\'':
            quote = c
            i += 1
            while i < n and text[i] != quote:
                if text[i] == '\\':
                    i += 2
                else:
                    i += 1
            i += 1
        elif c == '{':
            depth += 1
            i += 1
        elif c == '}':
            depth -= 1
            i += 1
            if depth == 0:
                return i
        else:
            i += 1
    return -1


def _neutralize_media_queries_in_css(css):
    """Walk CSS text once; rewrite or delete viewport-width @media blocks.

    Lock to the narrow (≤ 1024 px) layout: delete every `@media (min-width:
    N)` block for N ≥ _MEDIA_THRESH_MIN_DESKTOP, and unwrap every `@media
    (max-width: N)` block for N ≥ _MEDIA_THRESH_MAX_NARROW.
    """
    # Match @media at-rule heads: `@media <feature-list> {`. Capture the
    # full feature list (everything between `@media` and the opening `{`)
    # so we can decide whether the block represents a viewport-width gate.
    media_re = re.compile(r"@media\b([^{]+)\{", re.IGNORECASE)
    out_parts = []
    pos = 0
    while True:
        m = media_re.search(css, pos)
        if not m:
            out_parts.append(css[pos:])
            break
        out_parts.append(css[pos:m.start()])

        feat = m.group(1)
        body_start = m.end()
        body_end = _scan_balanced_block(css, body_start - 1)
        if body_end == -1:
            # Unbalanced — leave the rest untouched and stop.
            out_parts.append(css[m.start():])
            break

        rules = css[body_start:body_end - 1]  # inside the braces
        next_pos = body_end

        # Decide based on min-width / max-width values in feature list.
        # Only flip a block when it is purely viewport-width gated; if it
        # mixes other features (orientation, prefers-color-scheme, etc.)
        # leave it alone to be safe.
        min_w = re.search(r"\(\s*min-width\s*:\s*(\d+)(?:px|em|rem)?\s*\)", feat, re.IGNORECASE)
        max_w = re.search(r"\(\s*max-width\s*:\s*(\d+)(?:px|em|rem)?\s*\)", feat, re.IGNORECASE)
        # Reject if other recognizable feature tokens are present.
        feat_clean = re.sub(r"\(\s*(?:min-width|max-width)\s*:\s*\d+(?:px|em|rem)?\s*\)", "", feat, flags=re.IGNORECASE)
        feat_clean = re.sub(r"\b(?:and|only|screen|all)\b|,", "", feat_clean, flags=re.IGNORECASE).strip()

        if feat_clean:
            # Mixed feature query — keep block as-is.
            out_parts.append(css[m.start():body_end])
        elif min_w and not max_w and int(min_w.group(1)) >= _MEDIA_THRESH_MIN_DESKTOP:
            # Desktop-only block — delete entirely.
            pass
        elif max_w and not min_w and int(max_w.group(1)) >= _MEDIA_THRESH_MAX_NARROW:
            # Narrow block — unwrap so its rules apply unconditionally.
            out_parts.append(rules)
        else:
            # Other width range (e.g., mobile-only ≤ 600, ultra-wide ≥ 1440)
            # or a `min-width: N and max-width: M` pair — keep as-is.
            out_parts.append(css[m.start():body_end])
        pos = next_pos
    return "".join(out_parts)


_STYLE_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.DOTALL | re.IGNORECASE)


def _neutralize_media_queries(html):
    """Rewrite every <style> block to lock the layout at the narrow form."""
    return _STYLE_RE.sub(
        lambda m: m.group(1) + _neutralize_media_queries_in_css(m.group(2)) + m.group(3),
        html,
    )


def remove_banners(html):
    """Normalize Taylor & Francis HTML to a single centered text column.

    Reading column wrapper: `.page-body.pagefulltext` — wraps the entire
    article from the journal / volume / issue header through the
    references. Capped at 752 px with 56 px top/bottom and 16 px side
    padding.

    Per-publisher notes (temp/format-html-extra.md):
      - Main text column starts at "Nucleus\nVolume 6, 2015 - Issue 2"
        (journal + volume/issue line preceding the article title).
      - Main text column ends before "Related research" section.
      - Preserve publication cover image, "Nucleus" journal link + ">",
        "Volume 6, 2015 - Issue 2", "Submit an article" button
        (`.submitAnArticle`), "Journal homepage" button (`.jHomepage`).
        Native responsive CSS hides these at narrow viewports; force
        visible since the 752-px body cap triggers narrow-mode layout.
      - Strip the bottom floating "In this article / Article contents"
        hamburger nav (`<div class=sections-nav>`).
      - Do NOT strip `publication-tabs-dropdown` — that class is on the
        outer tabs container wrapping the entire article body.
    """
    # -------------------------------------------------------------------
    # Step 1 — lock the publisher's CSS to its narrow (≤ 1024) layout.
    # See `_neutralize_media_queries` docstring above. Delete every
    # `@media (min-width: ≥1025)` block (desktop layout never fires)
    # and unwrap every `@media (max-width: ≥720)` block (narrow rules
    # apply unconditionally). The whole head block — gray-box journal-
    # nav, metrics widget, article title, author/page row — collapses
    # to its narrow form at any viewport.
    # -------------------------------------------------------------------
    html = _neutralize_media_queries(html)

    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    # Top: site nav + breadcrumb.
    html = _remove_nested_element(html, r"<header\b[^>]*>")
    html = remove_elements_by_id(html, "bc-nav")
    # Bottom: site footer.
    html = _remove_nested_element(html, r"<footer\b[^>]*>")
    # Bottom-floating sticky "In this article / Article contents" nav.
    # Two class tokens seen in the wild: `sections-nav` (static hamburger)
    # and `sectionsNavigation` (Silverchair widget wrapper, fixed pos).
    for cls in ("sections-nav", "sectionsNavigation"):
        for _ in range(3):
            before = html
            html = _remove_nested_element(
                html,
                rf'<div\b[^>]*\bclass=["\']?[^"\'>]*\b{cls}\b[^>]*>',
            )
            if html == before:
                break
    # Mobile-only tab-nav buttons that sit in the article column header.
    for cls in ("related-mobile", "article-contents-mobile"):
        for _ in range(4):
            before = html
            html = _remove_nested_element(
                html,
                rf'<button\b[^>]*\bclass=["\']?[^"\'>]*\b{cls}\b[^>]*>',
            )
            if html == before:
                break
    # "Related research" header widget (`furtherReadingTitle`) and the
    # dropzone tab panels that trail it (People also read, Recommended
    # articles, Cited by). Main text ends before "Related research".
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass="[^"]*\bfurtherReadingTitle\b[^"]*"[^>]*>',
        )
        if html == before:
            break
    for _ in range(6):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bdata-pb-dropzone-name=["\']'
            r'(?:People also read|Recommended articles|Cited by)',
        )
        if html == before:
            break
    # In-column Silverchair chrome widgets that sit between the
    # page-body header and article metadata. Strip the blue search-
    # triangle corner + search widget, the "Download PDF" CTA below
    # references, and various Silverchair banners.
    for cls_pat in (
        "literatumBreadcrumbs",
        "literatumCartLink",
        "literatumInstitutionBanner",
        "literatumNavigationLoginBar",
        "quickSearchWidget",
        "advancedSearchLinkDropZone",
        "gql-alternative-widget",
        # `.widget.pageHeader` holds the searchButtonIcon button that
        # renders as the blue search corner at the page top — a
        # remnant of site-header chrome not covered by stripping
        # <header>. Per user request: "Delete the blue search button".
        "pageHeader",
        # `gql-content-navigation` is the previous/next-article nav
        # widget that sits between the keywords block and the first
        # article body section. Its inner buttons render at desktop
        # only; the wrapper itself remains as a 59-px empty band at
        # narrow viewports.
        "gql-content-navigation",
        # Sales promo block tailing the references column.
        "advertising-offer",
        # "Recommended articles" / "People also read" strip below
        # references — two related class roots (one is the outer
        # widget, one is the inner content section).
        "recommended-articles-widget",
        "hum-recommendations",
        # Empty general-html widget shells remain after the
        # advertising-offer / recommended-articles content was stripped
        # — adds ~7 px each to the trailing whitespace.
        "general-html",
    ):
        for _ in range(3):
            before = html
            html = _remove_nested_element(
                html,
                rf'<div\b[^>]*\bclass=["\']?[^"\'>]*\b{cls_pat}\b[^>]*>',
            )
            if html == before:
                break

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        # Step 2 — freeze the 720-px layout regardless of viewport.
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important;"
        # Overlay-style scrollbar so the 16 px gutter on the right is
        # not eaten by a non-overlay scrollbar at narrow viewports.
        "overflow-y:overlay !important}"
        "html::-webkit-scrollbar{width:0 !important;height:0 !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Cancel publisher's Bootstrap-style desktop column grid AND
        # the inner-widget paddings that put the journal-nav text
        # 21+30 px right of the page-body inner edge. These are
        # surgical removals (zero, not fabricated values) of publisher
        # rules that survive the @media neutralizer (some are in
        # unconditional CSS rather than viewport-gated @media blocks).
        ":root [class*=\"col-md-\"]{"
        "float:none !important;width:100% !important;"
        "max-width:100% !important;flex:0 0 100% !important;"
        "margin-left:0 !important;margin-right:0 !important}"
        ":root .page-body.pagefulltext .container,"
        ":root .page-body.pagefulltext .container-fluid{"
        "margin-left:0 !important;margin-right:0 !important;"
        "width:100% !important;max-width:none !important}"
        # Step 4 — cap the reading column. Use the standard 752-px cap
        # with 56-px top/bottom + 16-px side padding (per format-html
        # spec). The publisher's own narrow-mode horizontal indents on
        # `.literatumSeriesNavigation` (mt=7, ml=7, mr=7) and
        # `.widget-body` (pt=7, pl=7) used to layer on top of a 720-px
        # wrapper with 0 horizontal padding to land near 16; switching
        # to a clean 752/16 wrapper plus widget-padding zeros below
        # gives a stable L/R/T/B that matches the spec at every vw.
        # Horizontal padding is zero — the publisher's narrow-mode
        # CSS supplies a ~22 px left inset via inner widget paddings,
        # which the parser preserves so all metadata-block elements
        # keep their relative positions. To bring the smallest text
        # margin from L=22 down to L=16 (format-html spec floor)
        # without disturbing those relative offsets, the entire
        # wrapper is shifted 6 px left via `translateX(-6px)`. Layout
        # flow is unaffected; only the visual position changes.
        ".page-body.pagefulltext{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 0 !important;"
        "transform:translateX(-6px) !important;"
        "box-sizing:border-box !important}"
        # Hide TOC hamburger + ReadSpeaker tool-toggle (dead buttons
        # in the static capture). Also hide the article-level tab-nav
        # bar ("Full Article / Figures & data / References / …") which
        # is a dead JS navigator in the static capture.
        ".page-body.pagefulltext .tab-nav,"
        ".page-body.pagefulltext .article-contents-mobile,"
        ".page-body.pagefulltext #tocPopOver,"
        ".page-body.pagefulltext .article-contents,"
        ".page-body.pagefulltext .rsbtn_tooltoggle{display:none !important}"
        # `publicationContentBody` and `publicationContentHeader` ship
        # with linear-gradient backgrounds that paint a gray strip
        # behind the article-title + metadata block. Kill both so the
        # article reads on plain white background — matches the rest
        # of the converted output and keeps the title/byline/metric
        # row visually neutral.
        ".page-body.pagefulltext .publicationContentBody,"
        ".page-body.pagefulltext .publicationContentHeader{"
        "background-image:none !important;background-color:#fff !important}"
        # Reclaim the 53 px reserved for the tab-nav container that
        # natively reserves space for the TOC bar. With the TOC hidden
        # the height collapse keeps the layout tight without zeroing
        # publisher-native vertical paddings on the metric/title block.
        ".page-body.pagefulltext .tabs-widget>div:first-child{"
        "height:auto !important;min-height:0 !important}"
        # `.publicationContentBody .widget-body` natively has 7-px
        # padding-top reserved for the tab-nav bar. We hide the bar,
        # so that 7 px is dead space — zero only the top padding.
        # Keep horizontal/bottom paddings intact so the publisher's
        # native narrow-viewport rendering is preserved.
        ".page-body.pagefulltext .publicationContentBody>.wrapped>"
        ".widget-body{padding-top:0 !important}"
        # The masthead widget (`.widget.widget-compact-vertical` —
        # holds Full access / metric-totals) ships at outer ml=7
        # plus inner widget-body pl=7 = 14 px total inset. That
        # leaves the Full access icon at L=14, exceeding the title
        # column's left margin (L=21.6 from publicationContentHeader's
        # pl=21.5938). Increase the outer ml to 14.59 so total inset
        # matches the title column (14.59+7=21.59), without touching
        # the inner pl=7 (the publisher's icon-to-content gap stays
        # intact).
        ":root .page-body.pagefulltext .widget.widget-compact-vertical{"
        "margin-left:14.5938px !important;"
        "margin-right:14.5938px !important}"
        # Inset the gray journal-banner (`.publicationSerialHeader`)
        # symmetrically so its background aligns with the body
        # column. Value mirrors `.publicationContentHeader`'s
        # publisher-native `padding-left:21.5938px` — the same gutter
        # the publisher uses to inset metadata content from the
        # column edge — applied on both sides so the banner is
        # column-wide rather than viewport edge-to-edge.
        ":root .page-body.pagefulltext .publicationSerialHeader{"
        "margin-left:21.5938px !important;"
        "margin-right:21.5938px !important}"
        # `article.article` natively has `margin-top:24px` to space
        # the article body below the tab-nav bar. With the bar hidden
        # this 24 px becomes dead space too — zero it so the abstract
        # heading sits flush below the metric/title block.
        ".page-body.pagefulltext .publicationContentBody article.article{"
        "margin-top:0 !important}"
        # Hide trailing chrome remnants:
        # - `.col-md-1-4` sibling of references column (right rail with
        #   stripped recommended-articles content — 40 px orphan)
        # - `.extra-links .openUrl` per-reference institutional resolver
        #   button (35 px taller than sibling text links)
        ":root .row.row-md > .col-md-7-12:has(.references)"
        " ~ .col-md-1-4{display:none !important}"
        ".page-body.pagefulltext .extra-links .openUrl{"
        "display:none !important}"
        # Direct-child *:last-child margin-bottom zero (per skill).
        # Without this the trailing reference/Bibliography margin
        # leaves ~60 px of empty space below the last text inside
        # the page-body wrapper. Use `>` (direct-child) only — the
        # descendant form would zero every nested last-child's
        # padding-bottom and collapse the journal-heading gray box
        # 14 px shorter than its native height (`widget-body` and
        # `literatumSeriesNavigation` inside it natively contribute
        # 7 px each via padding-bottom and margin-bottom).
        ".page-body.pagefulltext > *:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # Trailing reference margin only — `.references-list li:last-child`
        # has native `margin-bottom: 32px` that contributes 32 px of
        # trailing whitespace below the last text inside the wrapper.
        # Scope the zero strictly to that LI so the descendant rule
        # doesn't touch journal-nav widgets like `.literatumSeriesNavigation`,
        # which natively use mb=7 to space themselves from the next
        # widget below.
        ".page-body.pagefulltext .references-list>li:last-child,"
        ".page-body.pagefulltext .ref-list>li:last-child,"
        ".page-body.pagefulltext .NLM_ref-list>li:last-child,"
        ".page-body.pagefulltext .references>li:last-child{"
        "margin-bottom:0 !important}"
        # The orphan tabs-widget shell that used to wrap the dropzones
        # we stripped above renders as an empty tab bar at the column
        # bottom. Hide any tabs-widget whose ancestry is NOT through
        # `.publication-tabs` (the in-article figure tabs we keep).
        ".page-body.pagefulltext .tabs-widget:not("
        ".publication-tabs .tabs-widget){display:none !important}"
        # Figures: tandfonline (Atypon Literatum) wraps each figure in
        #   <div class=figureView>
        #     <div class=short-legend>
        #       <p class=captionText>Figure N. Caption text...</p>
        #     </div>
        #     <a href=# class=thumbnail data-behaviour=show-popup
        #        data-popup-event-type=fig data-id=f<N>>
        #       <img id=d<N> src="data:image/webp;base64,..." loading=lazy
        #            height=<H> width=<W>>
        #     </a>
        #     <div><button class=show-full-size>Display full size</button></div>
        #   </div>
        # Native order is CAPTION first, image second, full-size button
        # third. Per the figure layout contract, image must render
        # above caption — use flex column-reverse-style ordering. The
        # high-res URL is not exposed in the saved DOM (JS-only popup
        # via show-popup), so get_refs.py extension deferred. Visual
        # fixes: reorder via flex `order`, force img full-width above
        # caption, hide JS-only "Display full size" button.
        ":root .page-body.pagefulltext .figureView{"
        "display:flex !important;flex-direction:column !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:1rem 0 !important;padding:0 !important}"
        ":root .page-body.pagefulltext .figureView "
        ".short-legend{order:2 !important;width:100% !important;"
        "max-width:100% !important;margin:0 !important;padding:0 !important}"
        ":root .page-body.pagefulltext .figureView "
        "a.thumbnail{order:1 !important;display:block !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important;padding:0 !important;"
        "cursor:default !important}"
        ":root .page-body.pagefulltext .figureView "
        "a.thumbnail > img{display:block !important;"
        "width:100% !important;height:auto !important;"
        "max-width:100% !important;margin:0 !important}"
        # Hide the JS-only "Display full size" button (non-functional
        # without JS) and put it last in flex order.
        ":root .page-body.pagefulltext .figureView "
        "button.show-full-size,"
        ":root .page-body.pagefulltext .figureView "
        "div:has(> button.show-full-size){display:none !important}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _get_meta(html, name):
    """Get content of a <meta> tag by name, handling unquoted attributes.

    Tandfonline-specific: the shared _helpers.get_meta does not handle the
    mixed quoted/unquoted attribute style used by tandfonline. Handles both:
      <meta name="dc.Title" content="...">
      <meta name=dc.Title content="...">
    and content can also be unquoted.
    """
    esc = re.escape(name)
    patterns = [
        # name then content, quoted content
        rf'<meta[^>]*name="?\'?{esc}"?\'?[^>]*content="([^"]*)"',
        rf"<meta[^>]*name=\"?'?{esc}\"?'?[^>]*content='([^']*)'",
        # name then content, unquoted content
        rf'<meta[^>]*name="?\'?{esc}"?\'?[^>]*content=([^\s>]+)',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return unescape(m.group(1).strip())
    return ""


def _parse_volume_issue(html):
    """Extract volume and issue.

    Primary source: <span class=issue-heading>...'Volume 46, 2026 - Issue 4'.
    Fallback: JSON-LD BreadcrumbList item "name":"Volume 29, Issue 20" in
    the page <script type=application/ld+json> block (present even when the
    issue-heading span is not emitted by SingleFile).
    """
    m = re.search(
        r'class="?issue-heading"?[^>]*>(.*?)</span>', html, re.DOTALL
    )
    text = ""
    if m:
        text = strip_tags(m.group(1)).strip()
    if not re.search(r"Volume\s+\d+", text):
        m2 = re.search(
            r'"name"\s*:\s*"\s*Volume\s+\d+\s*,\s*Issue\s+\S+?\s*"',
            html,
        )
        if m2:
            text = m2.group(0)
    vol_m = re.search(r"Volume\s+(\d+)", text)
    iss_m = re.search(r"Issue\s+([^\s,\"<]+)", text)
    volume = vol_m.group(1) if vol_m else ""
    issue = iss_m.group(1) if iss_m else ""
    return volume, issue


def _parse_pages(html):
    """Extract page range from 'Pages X-Y' text in itemPageRangeHistory div."""
    m = re.search(r'class="?itemPageRangeHistory"?[^>]*>.*?Pages\s+([\d][^\s<]+)', html, re.DOTALL)
    if not m:
        return ""
    return m.group(1).replace("\u2013", "-").replace("\u2014", "-")


def _parse_title(html):
    """Extract title, combining dc.Title and dc.Title.Subtitle if present."""
    title = _get_meta(html, "dc.Title")
    subtitle = _get_meta(html, "dc.Title.Subtitle")
    if subtitle:
        title = f"{title}: {subtitle}"
    return title


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    Tandfonline-specific: uses dc.Date for year, issue-heading span for volume
    and issue, itemPageRangeHistory for pages, and dc.Identifier[scheme=doi]
    (falling back to publication_doi) for DOI.
    """
    date = _get_meta(html, "dc.Date")
    year = ""
    if date:
        ym = re.search(r"(\d{4})", date)
        if ym:
            year = ym.group(1)

    volume, issue = _parse_volume_issue(html)
    pages = _parse_pages(html)

    # DOI from dc.Identifier with scheme=doi
    doi = ""
    doi_m = re.search(
        r'<meta[^>]*name="?dc\.Identifier"?[^>]*scheme="?doi"?[^>]*content="?([^\s">]+)',
        html,
    )
    if doi_m:
        doi = doi_m.group(1)
    if not doi:
        doi = _get_meta(html, "publication_doi")

    return {
        "title": _parse_title(html),
        "journal": _get_meta(html, "citation_journal_title"),
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": format_doi(doi),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name.
    Tandfonline-specific: parses contribDegrees spans. Each span contains
    <a class=author> with LastName%2C+Given in its href, and an
    <span class=overlay> with affiliation text.
    """
    authors = []
    seen = set()

    # Find each contribDegrees span by walking from each opening tag
    # Class may have additional values: "contribDegrees corresponding MTN"
    for m in re.finditer(r'<span\s+class="?contribDegrees[^>]*>', html):
        # Walk forward to find matching </span>, handling nesting
        pos = m.end()
        depth = 1
        while depth > 0 and pos < len(html):
            next_open = re.search(r'<span[\s>]', html[pos:])
            next_close = re.search(r'</span>', html[pos:])
            if next_close is None:
                break
            if next_open and next_open.start() < next_close.start():
                depth += 1
                pos += next_open.end()
            else:
                depth -= 1
                pos += next_close.end()

        block = html[m.end():pos]

        # Extract name from author link
        name_m = re.search(r'class="?author"?[^>]*>(.*?)</a>', block, re.DOTALL)
        if not name_m:
            continue

        display_name = strip_tags(name_m.group(1)).strip()
        if display_name in seen:
            continue
        seen.add(display_name)

        # Try to get LastName, Given from href URL
        href_m = re.search(r'class="?author"?[^>]*href="?([^\s">]+)', block)
        author = display_name
        if href_m:
            href = href_m.group(1)
            # URL like /author/Clatterbuck+Soper%2C+Sarah+F
            parts = href.split("/author/")
            if len(parts) > 1:
                decoded = urllib.parse.unquote_plus(parts[1])
                author = format_author_name(decoded)

        # Extract affiliation from overlay span
        affiliations = []
        overlay_m = re.search(
            r'<span\s+class="?overlay"?>(.*?)</span>', block, re.DOTALL
        )
        if overlay_m:
            aff_html = overlay_m.group(1)
            # Remove orcid links and their images
            aff_html = re.sub(r'<a[^>]*class="?orcid-author"?[^>]*>.*?</a>', '', aff_html, flags=re.DOTALL)
            aff_text = strip_tags(aff_html).strip()
            # Remove "Correspondence" + email at end of affiliation
            aff_text = re.sub(r'Correspondence\S*$', '', aff_text).strip()
            if aff_text:
                # Affiliations are prefixed with superscript labels (a, b, c...)
                # Split on semicolons which separate multiple affiliations
                parts = re.split(r'\s*;\s*', aff_text)
                affiliations = [p.strip() for p in parts if p.strip()]

        # Email-domain inference: older T&F Cell Cycle HTML exposes only
        # the corresponding-author email in the overlay, not a structured
        # affiliation block. Fall back to the known-domain map so
        # authors from major academic institutions still get an aff.
        if not affiliations:
            for em in re.finditer(r'mailto:([^"\'\s>]+)', block):
                aff = affiliation_from_email(em.group(1))
                if aff:
                    affiliations = [aff]
                    break

        authors.append({
            "author": author,
            "affiliation": affiliations,
        })

    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _flip_ref_author(name):
    """Convert 'Initials LastName' (Scholar URL shape) to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    Uses Google Scholar lookup URLs for structured fields, with DOIs from
    getFTR data-target attributes (not from scholar URL tracking params).
    Falls back to plain text from <li> entries.
    """
    # Find references section (unquoted id)
    refs_m = re.search(
        r'<div\s+id="?references-Section1"?[^>]*>(.*)',
        html, re.DOTALL,
    )
    if not refs_m:
        return []

    refs_html = refs_m.group(1)
    # Truncate at next major section to avoid matching outside references
    end_m = re.search(r'</div>\s*</div>\s*</article>', refs_html, re.DOTALL)
    if end_m:
        refs_html = refs_html[:end_m.start()]

    refs = []

    # Find all reference <li> start positions (no </li> tags in tandfonline HTML)
    # ID formats: CIT0001, cit0001, B1, R1
    li_starts = list(re.finditer(r'<li\s+id="?(?:CIT|cit|B|R)\d+[^>]*>', refs_html, re.DOTALL))
    for i, li_m in enumerate(li_starts):
        end = li_starts[i + 1].start() if i + 1 < len(li_starts) else min(li_m.start() + 5000, len(refs_html))
        entry = refs_html[li_m.end():end]

        # Get DOI from getFTR data-target (the reliable source)
        doi_m = re.search(r'data-target="?(10\.[^\s">]+)', entry)
        ref_doi = format_doi(doi_m.group(1)) if doi_m else ""

        # Try Google Scholar lookup URL (double-encoded in getFTRLinkout)
        gs_m = re.search(r'scholar_lookup%3F([^"\'>\s]+)', entry)
        if not gs_m:
            # Direct scholar_lookup URL
            gs_m = re.search(
                r'scholar\.google\.com/scholar_lookup\?([^"\'>\s]+)', entry
            )

        if gs_m:
            qs = gs_m.group(1)
            # Decode URL encoding
            qs = urllib.parse.unquote(qs)
            # Strip tracking params appended by tandfonline after &amp;
            # Scholar params use & between them; tracking params start with &amp;doi=
            qs = re.split(r'&amp;', qs)[0]
            qs = unescape(qs).replace("&amp;", "&")
            params = urllib.parse.parse_qs(qs)

            # Scholar URL doesn't include issue; parse it from citation text
            # Format: "YYYY;VOLUME(ISSUE):PAGES"
            issue = ""
            text = strip_tags(entry)
            iss_m = re.search(r'\d{4};\d+\(([^)]+)\)\s*:', text)
            if iss_m:
                issue = iss_m.group(1)

            ref = {
                "title": params.get("title", [""])[0],
                "journal": params.get("journal", [""])[0],
                "year": params.get("publication_year", [""])[0],
                "volume": params.get("volume", [""])[0],
                "issue": issue,
                "pages": params.get("pages", [""])[0].replace("\u2013", "-"),
                "doi": ref_doi,
                "authors": [
                    _flip_ref_author(a) for a in params.get("author", []) if a.strip()
                ],
            }
        else:
            # Fallback: extract text from the <span> content
            span_m = re.search(r'<span>(.*?)</span>', entry, re.DOTALL)
            cite_text = strip_tags(span_m.group(1)).strip() if span_m else ""
            ref = {
                "title": cite_text,
                "journal": "",
                "year": "",
                "volume": "",
                "issue": "",
                "pages": "",
                "doi": ref_doi,
                "authors": [],
            }

        refs.append({"": ref})

    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _is_top_level_section(tag_html):
    """Check if a matched NLM_sec tag is top-level (not level_2+)."""
    return not re.search(r'NLM_sec_level_[2-9]', tag_html)


def _get_section_heading(section_html):
    """Get the first heading text from a section div."""
    m = re.search(r'<h[1-4][^>]*>(.*?)</h[1-4]>', section_html, re.DOTALL)
    if m:
        return strip_tags(m.group(1)).strip()
    return ""


def _extract_section(html, start_match):
    """Extract a section div content from its opening match to matching close."""
    pos = start_match.end()
    depth = 1
    while depth > 0 and pos < len(html):
        next_open = re.search(r'<div[\s>]', html[pos:])
        next_close = re.search(r'</div>', html[pos:])
        if next_close is None:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos += next_open.end()
        else:
            depth -= 1
            pos += next_close.end()
    return html[start_match.start():pos]


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    Tandfonline-specific: include abstract (hlFld-Abstract), keywords
    (abstractKeywords), and top-level NLM_sec body sections before references;
    include top-level NLM_sec supplementary sections after references.
    """
    # Find article element
    article_m = re.search(r'<article[^>]*>(.*)</article>', html, re.DOTALL)
    if not article_m:
        return ""
    article = article_m.group(1)

    parts = []

    # Abstract (nesting-aware extraction)
    abs_m = re.search(r'<div\s[^>]*class="?hlFld-Abstract"?[^>]*>', article)
    if abs_m:
        abs_html = _extract_section(article, abs_m)
        abs_html = strip_common(abs_html)
        parts.append(tags_to_text(abs_html))

    # Keywords
    kw_m = re.search(r'<div\s[^>]*class="?abstractKeywords"?[^>]*>', article)
    if kw_m:
        kw_html = _extract_section(article, kw_m)
        kw_text = strip_tags(kw_html).strip()
        if kw_text:
            parts.append(kw_text)

    # Find references section position to determine body boundary
    refs_pos = len(article)
    refs_m = re.search(r'<div\s+id="?references-Section1"?', article)
    if refs_m:
        refs_pos = refs_m.start()

    # Body sections (top-level NLM_sec divs before references)
    for sec_m in _ALL_SECTION_RE.finditer(article):
        if sec_m.start() >= refs_pos:
            break
        if not _is_top_level_section(sec_m.group()):
            continue

        section_html = _extract_section(article, sec_m)
        section_html = extract_captions(section_html)
        section_html = strip_common(section_html)
        text = tags_to_text(section_html)
        if text.strip():
            parts.append(text)

    # Look for supplementary content sections after references
    if refs_m:
        post_refs = article[refs_pos:]
        for sec_m in _ALL_SECTION_RE.finditer(post_refs):
            if not _is_top_level_section(sec_m.group()):
                continue
            section_html = _extract_section(post_refs, sec_m)
            heading = _get_section_heading(section_html)
            if _SUPPLEMENTARY_RE.search(heading):
                section_html = extract_captions(section_html)
                section_html = strip_common(section_html)
                text = tags_to_text(section_html)
                if text.strip():
                    parts.append(text)

    result = "\n\n".join(parts)
    return drop_noise(result, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse tandfonline HTML into a papers/*.json-format dict."""
    meta = _parse_metadata(html)
    return {
        "title": meta["title"],
        "journal": meta["journal"],
        "year": meta["year"],
        "volume": meta["volume"],
        "issue": meta["issue"],
        "pages": meta["pages"],
        "doi": meta["doi"],
        "authors": _parse_authors(html),
        "main_text": _parse_main_text(html),
        "references": _parse_references(html),
    }
