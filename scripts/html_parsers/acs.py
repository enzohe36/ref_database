"""American Chemical Society (acs) HTML parser."""

import re
import urllib.parse
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    neutralize_media_queries,
    remove_elements_by_id,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new window",
    "Click to copy section link",
    "Section link copied!",
    "View Author Information",
    "High Resolution Image",
    "Download MS PowerPoint Slide",
    "Download",
    "Google Scholar",
    "View",
)

# Top-level NLM_sec openings. Newer ACS papers include an id=secN on the
# wrapper; older papers (e.g. 2001) omit it, so match either form but only
# level_1 (top-level) sections.
_ALL_SECTION_RE = re.compile(
    r'<div\s+(?:id=sec\d+\s+)?class="NLM_sec\s+NLM_sec_level_1"[^>]*>', re.DOTALL
)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r"supplement|extended data|source data|expanded view|appendix",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize ACS HTML to a single centered text column.

    Chrome stripped (Step 3):
      - Site `<header>` and `<footer>`.
      - Outer `<main id=main-desktop>` wrapper attributes are kept; the
        article wrapper is the inner `<main class="content article">`
        which holds `<article>`.
      - Pre-title chrome inside `.article_header-left` (breadcrumbs,
        mobile logo, access-icons, share dropdown) — strip via the
        targeted classes so only the publication-type/date row + title
        remain at the top of the reading column.
      - "Cited By" section (`<div class=cited-by>`) and every following
        post-article block (Citing/Recommended/Subjects/Comments).

    Visibility tweaks (Step 4):
      - Force-expand the "View Author Information" affiliations accordion
        (`#affiliations-popup`).
      - Hide the `.figure-viewer` modal via CSS only — its `cit-fg-*`
        spans are read by `_parse_metadata`, so it must not be removed
        from the DOM.

    Reading column wrapper: `<main class="content article">`. Cap at
    752 px with 56 px top/bottom + 16 px side padding.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    # The site header is `<header class="header header_...">`. Inline
    # sub-headers (`<header class=article__tags__header>` for the Subjects
    # block, `<header class=...>` inside reference lists) must NOT be
    # stripped — the parser captures their text. Match only on the
    # site-header class prefix.
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html, r'<header\b[^>]*class="header(?:\s+|")',
        )
        if html == before:
            break
    # Footer: `<footer class=footer ...>`. Inline figshare-widget footer
    # `<footer class=frontend-filesViewer-...>` carries the
    # "Share / Download / figshare" caption that the parser emits into
    # main_text — match only the site footer's `class=footer` token.
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html, r'<footer\b[^>]*\bclass=(?:"footer"|footer\b)',
        )
        if html == before:
            break
    # Cookie / CCPA / chat widgets that float over the article.
    for sel in (
        "onetrust-consent-sdk", "onetrust-banner-sdk",
        "SnapABug_CMW", "SnapABug_CM",
    ):
        html = remove_elements_by_id(html, sel)
    # Pre-title chrome inside article_header-left.
    for cls in (
        "article_header-dropzone-1", "article_header-logo",
        "article_header-icons", "article_header-share",
        # Mobile layout surfaces a headerLogo/e-alerts block at the very
        # top of article_header-left (visible only below the desktop
        # breakpoint). Strip so the publication-type line is the first
        # rendered text at every viewport.
        "headerLogo_e-alertsMobile_container",
        "e-alertsMobile_container",
    ):
        for _ in range(8):
            before = html
            html = _remove_nested_element(
                html, rf'<div\b[^>]*class="?{cls}\b[^>]*>',
            )
            if html == before:
                break
    # Sticky duplicate header that appears on scroll (article_stickyheader).
    html = _remove_nested_element(
        html, r'<div\b[^>]*class="?[^"]*\barticle_stickyheader\b[^"]*"?[^>]*>',
    )
    # Right-rail content column (cover image, PDF buttons, alerts).
    html = remove_elements_by_id(html, "article_content-right")
    # Cited By + everything after (block has class=cited-by; the trailing
    # widgets sit inside article-side panes the wrapper still contains).
    html = _remove_nested_element(
        html, r'<div\b[^>]*\bclass="?cited-by\b[^>]*>',
    )
    # Floating section-tab navigator (Abstract / Figures / References /
    # Supporting Info shortcuts). It is position:absolute with a dummy
    # body ~29,000 px tall that extends docH, and overlaps the article
    # when pinned sticky. Drop the wrapper.
    html = _remove_nested_element(
        html, r'<nav\b[^>]*\bclass=[\'"]?tab-nav-shortcut__wrapper\b[^>]*>',
    )
    # Recommended-articles drawer. (Don't strip subjects__heading —
    # Subjects sits before Cited By per the user's column-end anchor
    # and feeds main_text. Don't strip #about-article-metrics — it's
    # a hidden popup whose `margin-top:15px` participates in the
    # margin cascade above the Abstract heading; removing it
    # collapses 15 px of spacing.)
    for sel in (
        "show-recommended__list",
        "article_drawer_RecommendedShowMore",
        "article_drawer_RecommendedShowLess",
    ):
        html = remove_elements_by_id(html, sel)
    # Skip-link anchors that occupy a line at the top of body.
    html = remove_elements_by_id(html, "skip-to-article", "skip-to-sidebar")

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze, column cap, expand author-info,
    # hide figure-viewer modal (DOM-preserved for parser).
    # -------------------------------------------------------------------
    override = (
        "<style>"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Outer main wrapper + ancestor grids collapse to plain block.
        # `.article-grid` ships as a 2-column CSS grid (article__left-side
        # + article__right-side) that pins the left side to track 1 with
        # track 2 reserved for the sidebar. Override to single-column so
        # the left-side content takes the full wrapper width.
        "#main-desktop,#pb-page-content,#pb-page-content>div,"
        ".article__pubdate,.article-grid,"
        "#article__left-side,.article__left-side,.article_fullPage{"
        "display:block !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important;float:none !important;"
        "grid-template-columns:none !important;"
        "grid-template-areas:none !important;"
        "background:#fff !important}"
        # Capped reading-column wrapper.
        ":root main.content.article{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        ":root main.content.article *{"
        "max-width:100% !important;min-width:0 !important;"
        "box-sizing:border-box !important}"
        # The article header splits into a 2-col flex row at desktop
        # widths (article_header-left ~ calc(100% - 16.25rem), right
        # 16.25rem). After right column removal the left is calc-clamped
        # to less than 100%. Force flex children to plain block + 100%
        # width.
        ":root main.content.article .article_header,"
        ":root main.content.article .article_header-left,"
        ":root main.content.article .article_header-meta,"
        ":root main.content.article .article_header-meta-left,"
        ":root main.content.article .article_header-meta-right,"
        ":root main.content.article .article_header-meta-center,"
        ":root main.content.article .article_header-footer,"
        ":root main.content.article .article_header-footer-left,"
        ":root main.content.article .article_header-footer-right,"
        ":root main.content.article #articleBody,"
        ":root main.content.article .NLM_sec,"
        ":root main.content.article .article__content,"
        ":root main.content.article article{"
        "display:block !important;width:auto !important;"
        "max-width:100% !important;min-width:0 !important;"
        "margin-left:0 !important;margin-right:0 !important;"
        "padding-left:0 !important;padding-right:0 !important;"
        "float:none !important;clear:none !important}"
        # `.article_header` has `padding-top: 32px` (publisher chrome
        # to vertically separate the metadata block from the journal
        # logo above), pushing T from 56 to 88. Zero — the wrapper's
        # 56 px padding-top already provides the gap. Keep pb=16 —
        # that's the publisher's natural gap between the metric block
        # and the Abstract heading; zeroing it collapses 16 px of
        # spacing before "Abstract" and other downstream sections.
        ":root main.content.article .article_header{"
        "padding-top:0 !important}"
        # Force-expand the "View Author Information" affiliations accordion.
        # Site CSS keeps it hidden via height:0 / display:none.
        "#affiliations-popup{display:block !important;"
        "max-height:none !important;height:auto !important;"
        "overflow:visible !important;visibility:visible !important}"
        # Hide the figure-viewer fullscreen modal (lives at fixed position
        # over the page). Cannot DOM-remove: it carries cit-fg-* spans
        # that _parse_metadata reads.
        ".figure-viewer{display:none !important}"
        # Figshare embedded-PDF loaders render below the article (~1200 px
        # canvas) and add docH past the wrapper bottom on newer captures.
        ".figshare-loader,.fs-page-wrapper,"
        ".fs-figshare-loader-holder,.fs-canvas-document-container{"
        "display:none !important}"
        # Trailing display:inline canvas element generates a baseline
        # line-box at the bottom of body (~16 px below wrapper).
        "canvas.hiddenCanvasElement{display:none !important}"
        # Tables: force fixed layout + word-break so wide DNA/sequence
        # cells don't push past the wrapper.
        ":root main.content.article table{"
        "width:100% !important;max-width:100% !important;"
        "table-layout:fixed !important}"
        ":root main.content.article td,:root main.content.article th{"
        "word-break:break-all !important;overflow-wrap:anywhere !important;"
        "white-space:normal !important}"
        # First-child margin reset — DIRECT children only (`>`) per the
        # SKILL.md pitfall. Descendant `*:first-child{margin-top:0}`
        # zeroes every section heading's top margin (headings are
        # typically the first child of their section container),
        # causing the body paragraph right below each heading to
        # overlap the heading text. Descendant `*:last-child
        # {margin-bottom:0}` is safe for the margin cascade; but
        # descendant `padding-bottom:0` would zero the inner padding
        # of nested last-children (e.g. the reference View button's
        # `padding:4px 8px` collapses to `4px 8px 0`, cramming the
        # text against the bottom border) — so keep padding-bottom
        # on direct child only.
        ":root main.content.article > *:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        # Trailing margin-bottom reset — direct-child chain only.
        # Descendant `*:last-child{margin-bottom:0}` zeros the
        # publisher's intentional `.mb-4` (mb=24) and trailing
        # paragraph mb=16 used to space between sections (e.g. the
        # Terms & Conditions paragraph above "Author Information"),
        # collapsing inter-section spacing. The 6-level direct-child
        # chain reaches the actual last-leaf of the wrapper and zeros
        # only that one's trailing margin.
        ":root main.content.article > *:last-child,"
        ":root main.content.article > *:last-child > *:last-child,"
        ":root main.content.article > *:last-child > *:last-child > *:last-child,"
        ":root main.content.article > *:last-child > *:last-child > *:last-child > *:last-child,"
        ":root main.content.article > *:last-child > *:last-child > *:last-child > *:last-child > *:last-child,"
        ":root main.content.article > *:last-child > *:last-child > *:last-child > *:last-child > *:last-child > *:last-child"
        "{margin-bottom:0 !important;padding-bottom:0 !important}"
        # Native ACS CSS sets `.article-grid .article_abstract h2
        # {margin:0}`, leaving no separation between the "Abstract"
        # heading block and the content (graphical abstract figure +
        # paragraph) that immediately follows. Restore ~16 px of
        # breathing room.
        ":root main.content.article .article_abstract-content{"
        "margin-top:16px !important}"
        # The Funding Statement / articleNote blue box natively ships
        # `padding:1rem` (16 px all around) but the previous descendant
        # `*:first-child{padding-top:0}` zeroed padding-top on the h4
        # inside, leaving the "Funding Statement" heading text flush
        # against the box's upper edge. Restore the heading's natural
        # padding-top so the text sits inside the padded box.
        ":root main.content.article .extra-info-sec.articleNote,"
        ":root main.content.article .extra-info-sec.fnGroup{"
        "padding-top:16px !important}"
        # Figures: ACS wraps each figure in
        #   <figure id=f_<N> class="article__inlineFigure ...">
        #     <button class=figure-viewer__trigger>
        #       <img class="inline-fig internalNav"
        #            src='data:image/svg+xml,<placeholder>'
        #            style="background-image:var(--sf-img-N) ...">
        #     </button>
        #     <div class=figure-bottom-links>
        #       <div class=download-hi-res-img>
        #         <a href=https://pubs.acs.org/cms/<doi>/asset/images/large/<file>.jpeg>
        #            High Resolution Image</a>
        #       </div>
        #       <div class=download-ppt><a>Download MS PowerPoint Slide</a></div>
        #     </div>
        #   </figure>
        # The image is rendered via SingleFile's `--sf-img-<N>` CSS var
        # (background-image on the <img> with a transparent SVG src).
        # Native fixed style="width:405px;height:275px" caps the figure
        # to the publisher's column width (narrower than the 720-px
        # reading column). Force the figure-viewer button + img to
        # full column width; the background-image fills the box at
        # `background-size:contain`, preserving aspect ratio.
        # get_refs.py uses `_ACS_FIGURES_FIX_JS` to swap the inline
        # SVG src to the high-res JPEG URL pulled from the
        # `.download-hi-res-img > a` link, so SingleFile inlines the
        # full-resolution image. Also hide the JS-only download link
        # row (PPT / Hi-Res) — non-functional inside the saved HTML.
        ":root main.content.article figure.article__inlineFigure{"
        "margin:1rem 0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important;"
        "display:block !important}"
        ":root main.content.article figure.article__inlineFigure "
        "button.figure-viewer__trigger{"
        "all:unset !important;display:block !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important;cursor:default !important}"
        ":root main.content.article figure.article__inlineFigure "
        "img.inline-fig{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
        # Drop the JS-only download links row.
        ":root main.content.article figure.article__inlineFigure "
        ".figure-bottom-links{display:none !important}"
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

def _get_meta_mixed(html, name):
    """Get meta content handling mixed quoted/unquoted attribute style."""
    esc = re.escape(name)
    for pat in (
        rf'<meta[^>]*name="?\'?{esc}"?\'?[^>]*content="([^"]*)"',
        rf"<meta[^>]*name=\"?'?{esc}\"?'?[^>]*content='([^']*)'",
        rf'<meta[^>]*name="?\'?{esc}"?\'?[^>]*content=([^\s>]+)',
    ):
        m = re.search(pat, html)
        if m:
            return unescape(m.group(1).strip())
    return ""


def _parse_cit_fg(html):
    """Parse journal/year/volume/issue/pages from article_header cit-fg-* spans.

    Format:
      <span class=cit-fg-title><i>Biochemistry</i></span>
      <span class=cit-fg-year> 2014</span>
      <span class=cit-fg-volume>, 53</span>
      <span class=cit-fg-issue>, 17</span>
      <span class=cit-fg-pageRange>, 2781-2792</span>
    """
    def _grab(cls):
        m = re.search(rf'class=cit-fg-{cls}[^>]*>(.*?)</span>', html, re.DOTALL)
        if not m:
            return ""
        return strip_tags(m.group(1)).strip().lstrip(',').strip()

    journal = _grab('title').rstrip('.')
    year = _grab('year')
    volume = _grab('volume')
    issue = _grab('issue')
    pages = _grab('pageRange').replace('\u2013', '-').replace('\u2014', '-')
    return journal, year, volume, issue, pages


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    ACS-specific: title from dc.Title meta, DOI from publication_doi meta,
    journal/volume/issue/year/pages from article_header cit-fg-* spans.
    """
    title = _get_meta_mixed(html, "dc.Title")
    doi = _get_meta_mixed(html, "publication_doi")
    if not doi:
        # Fall back to dc.Identifier with scheme=doi
        dm = re.search(
            r'<meta[^>]*name="?dc\.Identifier"?[^>]*scheme="?doi"?[^>]*content="?([^\s">]+)',
            html,
        )
        if dm:
            doi = dm.group(1)

    journal, year, volume, issue, pages = _parse_cit_fg(html)
    if not year:
        date = _get_meta_mixed(html, "dc.Date")
        if date:
            ym = re.search(r'(\d{4})', date)
            if ym:
                year = ym.group(1)

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": format_doi(doi),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_affiliations(html):
    """Parse the affiliations-popup dict: symbol -> text.

    Each affiliation: <div class=aff-info id=aff1>
                        <span class=aff-symbol>† ‡</span>
                        <span class=aff-text>...</span>
                      </div>
    Returns list of (symbol_set, text) — symbol_set is a set of individual
    symbol chars so authors carrying any of them can reference this aff.
    """
    affs = []
    for m in re.finditer(
        r'<div\s+class="?aff-info"?[^>]*>(.*?)</div>', html, re.DOTALL
    ):
        block = m.group(1)
        sm = re.search(
            r'<span\s+class="?aff-symbol"?[^>]*>(.*?)</span>', block, re.DOTALL
        )
        tm = re.search(
            r'<span\s+class="?aff-text"?[^>]*>(.*?)</span>', block, re.DOTALL
        )
        if not tm:
            continue
        symbol_text = strip_tags(sm.group(1)).strip() if sm else ""
        # Symbol may contain "†" or "† ‡" — split into individual chars
        symbols = {c for c in symbol_text if not c.isspace()}
        # Affiliation text often has leading superscript marker <sup>†</sup>
        aff_html = tm.group(1)
        # Drop leading superscript markers
        aff_html = re.sub(r'^(?:\s*<sup>[^<]*</sup>\s*)+', '', aff_html)
        text = strip_tags(aff_html).strip()
        if text:
            affs.append((symbols, text))
    return affs


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    ACS-specific: display names in <span class=hlFld-ContribAuthor><a>Given Last</a>;
    affiliation symbols in following <span class=author-aff-symbol><sup>X</sup>;
    affiliations in <div id=affiliations-popup> with <span class=aff-symbol>.
    """
    # Scope to the main author list <ul class=loa ...> (also handle quoted
    # class values like "loa non-jats-loa"). Needed to avoid matching
    # hlFld-ContribAuthor spans in Cited By / Author Info / etc.
    loa_m = re.search(r'<ul\s+class=("(?:loa[^"]*)"|loa\b)[^>]*>', html)
    if not loa_m:
        return []
    # ACS author <ul> omits </li> closers inside, so <ul> depth tracking is
    # reliable; walk forward to the matching </ul>.
    pos = loa_m.end()
    depth = 1
    end = len(html)
    while depth > 0:
        no = re.search(r'<ul[\s>]', html[pos:])
        nc = re.search(r'</ul>', html[pos:])
        if not nc:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                end = pos + nc.start()
            pos += nc.end()
    loa_html = html[loa_m.end():end]

    affs = _parse_affiliations(html)

    authors = []
    seen = set()
    # Find each <li> block; it is the stable scoping boundary for one author
    li_starts = list(re.finditer(r'<li\b[^>]*>', loa_html))
    for i, li_m in enumerate(li_starts):
        end = li_starts[i + 1].start() if i + 1 < len(li_starts) else len(loa_html)
        block = loa_html[li_m.end():end]

        # Locate the ContribAuthor span; the name is its direct textual content
        ca_m = re.search(
            r'<span\s+class="?hlFld-ContribAuthor"?[^>]*>(.*?)</span>',
            block, re.DOTALL,
        )
        if not ca_m:
            continue
        inner = ca_m.group(1)
        # Older ACS: name wrapped in <a>Given Last</a>. Newer ACS: plain text.
        a_m = re.search(r'<a[^>]*>([^<]+)</a>', inner)
        display = unescape(a_m.group(1) if a_m else strip_tags(inner)).strip()
        if not display or display in seen:
            continue
        seen.add(display)

        # Affiliation symbols (one per sup tag) for popup-based lookup
        symbols = set()
        for sm in re.finditer(
            r'class="author-xref-symbol author-aff-symbol"[^>]*><sup>([^<]+)</sup>',
            block,
        ):
            for c in sm.group(1):
                if not c.isspace():
                    symbols.add(c)

        author_affs = [text for syms, text in affs if syms & symbols]

        # Newer ACS: affiliation inside loa-info-affiliations-info per author
        if not author_affs:
            for am in re.finditer(
                r'<div\s+class="?loa-info-affiliations-info"?[^>]*>(.*?)</div>',
                block, re.DOTALL,
            ):
                text = strip_tags(am.group(1)).strip()
                if text:
                    author_affs.append(text)

        # If nothing matched and only one global affiliation exists, use it
        if not author_affs and len(affs) == 1:
            author_affs = [affs[0][1]]

        authors.append({
            "author": format_author_name(display),
            "affiliation": author_affs,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_inline_ref(entry):
    """Parse an inline ref format (<span class=NLM_string-ref>).

    Older ACS papers use a single inline citation string like:
      "Joyce, C. M., and Steitz, T. A. (1994) Annu. Rev. Biochem. 63, 777−822."
    Returns dict with title, journal, year, volume, pages, authors.
    """
    m = re.search(
        r'class="?NLM_string-ref"?[^>]*>(.*?)</span>', entry, re.DOTALL
    )
    if not m:
        return None
    raw = m.group(1)

    # Journal is the first <i>...</i> block (next <i> is volume)
    journal = ""
    volume = ""
    it_iter = list(re.finditer(r'<i>(.*?)</i>', raw, re.DOTALL))
    if it_iter:
        journal = strip_tags(it_iter[0].group(1)).strip().rstrip('.').strip()
        if len(it_iter) > 1:
            vol_text = strip_tags(it_iter[1].group(1)).strip()
            vm = re.match(r'(\d+)', vol_text)
            if vm:
                volume = vm.group(1)

    text = strip_tags(raw).strip()
    # "(YYYY)"
    year = ""
    ym = re.search(r'\((\d{4})\)', text)
    if ym:
        year = ym.group(1)

    # Authors: everything before "(YYYY)"
    authors = []
    if ym:
        author_text = text[:ym.start()].strip().rstrip(',').rstrip('.').strip()
        # Split "Surname, I., and Surname, I." into author strings
        # Split on ", and " first, then each chunk is "Surname, I. N."
        parts = re.split(r'\s*,\s+and\s+|\s+and\s+', author_text)
        for p in parts:
            p = p.strip().rstrip(',').strip()
            if not p:
                continue
            # "Joyce, C. M." -> "Joyce CM"
            if ',' in p:
                authors.append(format_author_name(p))
            else:
                authors.append(p)

    # Pages: "VVV, fpage-lpage." at end
    pages = ""
    pm = re.search(
        r'(\d+)\s*[−\-\u2013\u2014]\s*(\d+)\s*\.?\s*$',
        text,
    )
    if pm:
        pages = f"{pm.group(1)}-{pm.group(2)}"

    return {
        "title": "",
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": "",
        "pages": pages,
        "doi": "",
        "authors": authors,
    }


def _parse_structured_ref(entry):
    """Parse a structured ref (NLM_contrib-group, NLM_year, NLM_article-title, etc.).

    Newer ACS papers wrap each field in a dedicated span. Journal may be in
    <span class=citation_source-journal> (older) or as a bare <i>Journal</i>
    following the article-title (newer JACS / JPCB format).
    """
    def _field(cls):
        m = re.search(rf'class="?{cls}"?[^>]*>(.*?)</span>', entry, re.DOTALL)
        return strip_tags(m.group(1)).strip() if m else ""

    title = _field("NLM_article-title")
    if not title:
        title = _field("NLM_chapter-title")
    journal = _field("citation_source-journal").rstrip('.')
    if not journal:
        journal = _field("NLM_source").rstrip('.')

    volume = _field("NLM_volume")
    year = _field("NLM_year")
    fpage = _field("NLM_fpage")
    lpage = _field("NLM_lpage")
    pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

    # Authors: each author is in a <span class=NLM_contrib-group> span.
    # Newer ACS nests <span class=NLM_string-name> inside, so the first
    # </span> closes the nested span and we must walk forward to the outer
    # closing </span>. Use span-depth tracking to handle both shapes.
    authors = []
    for cm in re.finditer(r'<span\s+class="?NLM_contrib-group"?[^>]*>', entry):
        pos = cm.end()
        depth = 1
        end = len(entry)
        while depth > 0:
            no = re.search(r'<span[\s>]', entry[pos:])
            nc = re.search(r'</span>', entry[pos:])
            if not nc:
                break
            if no and no.start() < nc.start():
                depth += 1
                pos += no.end()
            else:
                depth -= 1
                if depth == 0:
                    end = pos + nc.start()
                pos += nc.end()
        name = strip_tags(entry[cm.end():end]).strip().rstrip(',').strip()
        if ',' in name:
            authors.append(format_author_name(name))
        elif name:
            authors.append(name)

    # Require at least one non-journal structured field to trust this ref.
    # Journal alone can appear in inline-string-ref notes via stray <i>, so
    # we only try the <i> journal fallback once we already have authors/year/title.
    if not (title or authors or year or volume):
        return None

    # Newer format: first <i>...</i> that is NOT inside an NLM_volume span is
    # the journal. Detect by stripping volume spans before scanning for <i>.
    if not journal:
        stripped = re.sub(
            r'<span\s+class="?NLM_volume"?[^>]*>.*?</span>',
            ' ', entry, flags=re.DOTALL,
        )
        im = re.search(r'<i>([^<]+)</i>', stripped)
        if im:
            journal = im.group(1).strip().rstrip('.').strip()
    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": "",
        "pages": pages,
        "doi": "",
        "authors": authors,
    }


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    ACS-specific: references live inside <ol id=references> with per-entry
    <li id=...>; each <div class=NLM_citation ... data-doi=DOI> holds either
    a structured layout (NLM_contrib-group + NLM_year + NLM_article-title + ...)
    or an inline NLM_string-ref with everything as a single string (older papers).
    """
    m = re.search(r'<ol\s+id="?references"?[^>]*>', html)
    if not m:
        return []

    # Scope to the matching </ol>
    pos = m.end()
    depth = 1
    ol_end = len(html)
    while depth > 0 and pos < len(html):
        no = re.search(r'<ol[\s>]', html[pos:])
        nc = re.search(r'</ol>', html[pos:])
        if not nc:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                ol_end = pos + nc.start()
                break
            pos += nc.end()
    refs_html = html[m.end():ol_end]

    refs = []
    # Each ref starts with <li id=...>. Older ACS (e.g. 2001 Biochemistry) uses
    # IDs like <paperid>b<NNNNN> for bibliography entries and <paperid>n<NNNNN>
    # for footnotes/abbreviations. Skip the n-prefixed notes.
    li_starts = list(re.finditer(r'<li\s+id="?([^"\'>\s]+)"?[^>]*>', refs_html))
    for i, li_m in enumerate(li_starts):
        end = li_starts[i + 1].start() if i + 1 < len(li_starts) else len(refs_html)
        entry = refs_html[li_m.end():end]

        li_id = li_m.group(1)
        if re.search(r'n\d+$', li_id):
            continue

        # DOI from data-doi on NLM_citation div
        doi = ""
        dm = re.search(r'data-doi=["\']?(10\.[^"\'>\s]+)', entry)
        if dm:
            doi = format_doi(dm.group(1))

        ref = _parse_structured_ref(entry)
        if ref is None:
            ref = _parse_inline_ref(entry)
        if ref is None:
            # Last-resort fallback: full text
            cm = re.search(r'class="?NLM_citation[^"]*"[^>]*>(.*?)</div>', entry, re.DOTALL)
            text = strip_tags(cm.group(1)).strip() if cm else ""
            ref = {
                "title": text,
                "journal": "",
                "year": "",
                "volume": "",
                "issue": "",
                "pages": "",
                "doi": doi,
                "authors": [],
            }
        else:
            ref["doi"] = doi or ref.get("doi", "")
        refs.append({"": ref})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _extract_section(html, start_match):
    """Extract a div from opening match to matching close, returning the slice."""
    pos = start_match.end()
    depth = 1
    while depth > 0 and pos < len(html):
        no = re.search(r'<div[\s>]', html[pos:])
        nc = re.search(r'</div>', html[pos:])
        if not nc:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            pos += nc.end()
    return html[start_match.start():pos]


def _get_section_heading(section_html):
    """Get first heading text from a section div."""
    m = re.search(r'<h[1-4][^>]*>(.*?)</h[1-4]>', section_html, re.DOTALL)
    if m:
        return strip_tags(m.group(1)).strip()
    return ""


def _find_refs_boundary(article):
    """Return the position where the References section starts inside article.

    Priority (earliest of the following):
      1. <h2>References</h2>
      2. <ol id=references>
    Falls back to len(article) when nothing matches.
    """
    candidates = []
    h2_m = re.search(r'<h2[^>]*>\s*References\s*</h2>', article, re.IGNORECASE)
    if h2_m:
        candidates.append(h2_m.start())
    ol_m = re.search(r'<ol\s+id="?references"?', article)
    if ol_m:
        candidates.append(ol_m.start())
    return min(candidates) if candidates else len(article)


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    ACS-specific: take the abstract <div class=article_abstract> plus the full
    slab from the first body section (or start of article_content) up to the
    References section as one block (this captures NLM_sec body sections plus
    Acknowledgment / Abbreviations / Author Information back matter wrapped
    in different classes). After References, append any NLM_sec supplementary
    sections.
    """
    article_m = re.search(r'<article[^>]*>', html)
    if not article_m:
        return ""
    article = html[article_m.end():]

    parts = []

    # Abstract container + body start position (after the abstract's </div>)
    body_start = 0
    abs_m = re.search(r'<div\s+class="?article_abstract"?[^>]*>', article)
    if abs_m:
        abs_html = _extract_section(article, abs_m)
        body_start = abs_m.start() + len(abs_html)
        abs_html = extract_captions(abs_html)
        abs_html = strip_common(abs_html)
        abs_text = tags_to_text(abs_html)
        if abs_text.strip():
            parts.append(abs_text)

    # Body slab: from just after the abstract to the References boundary.
    # This captures Funding Statement blocks and any other pre-body content
    # (e.g. <div class=extra-info-sec articleNote>) that sits between the
    # abstract and the first NLM_sec body section.
    refs_pos = _find_refs_boundary(article)
    if body_start < refs_pos:
        body_html = article[body_start:refs_pos]
        body_html = extract_captions(body_html)
        body_html = strip_common(body_html)
        text = tags_to_text(body_html)
        if text.strip():
            parts.append(text)

    # Supplementary: any NLM_sec level_1 sections after References boundary
    post = article[refs_pos:]
    for sec_m in _ALL_SECTION_RE.finditer(post):
        section_html = _extract_section(post, sec_m)
        heading = _get_section_heading(section_html)
        if heading and _SUPP_RE.search(heading):
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
    """Parse ACS HTML into a papers/*.json-format dict."""
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
