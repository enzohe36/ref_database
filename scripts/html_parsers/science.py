"""Science / AAAS (science.org) HTML parser. Also handles sagepub.com (shared Atypon Literatum layout)."""

import re
from html import unescape
from urllib.parse import parse_qs, urlparse

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    neutralize_media_queries,
    remove_elements_by_id,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "View all access and purchase options for this article.",
    "Get full access to this article",
    "Get Access",
    "Crossref",
    "Google Scholar",
    "PubMed",
    "Open URL",
    "View Article",
)

# Reference section title pattern
_REF_RE = re.compile(r"\breferences\b", re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r"supplement|extended data|source data|expanded view|powerpoint|appendix",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

_SCIENCEADVISER_SIDEBAR_RE = re.compile(
    r'<div class="mb-2x mb-xl-3x">\s*'
    r'<div class="d-flex flex-column[^"]*">\s*'
    r'<h3[^>]*>Sign up for ScienceAdviser</h3>'
    r'.*?</div>\s*</div>',
    re.DOTALL,
)


def remove_banners(html):
    """Normalize Science / AAAS HTML to a single centered text column.

    Chrome stripped (Step 3):
      - <header> (main navigation, header--compact) and its #mainNavbar.
      - "Get Science's award-winning newsletter" donation banner
        (class "alert-donation").
      - "eLetters" and everything below it inside <article> is hidden via
        CSS (no stable id; stripped by section index).
      - <footer> site chrome.
      - Left-side collapsible menu (#header-side-menu), breadcrumbs nav,
        article_sections_menu / article_collateral_menu floating nav bars,
        right-rail <aside> (core-collateral, current-issue-aside,
        multi-search, news-feature cards).

    Reading column: <article typeof=ScholarlyArticle>. Science's layout
    ships <article> as `display:grid` with a multi-column template that
    confines body content to ~656 px of a 688-wide grid area. Force
    `grid-template-columns: 1fr` and cap the article to 752 px.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    # Outer site <header class="main-header ...">. The article uses
    # nested <header data-extent=frontmatter> elements for title /
    # authors / affiliations which must stay; keying on class=main-header
    # avoids those.
    html = _remove_nested_element(
        html, r'<header\b[^>]*class="[^"]*main-header[^"]*"[^>]*>'
    )
    # Siblings of the outer header (literatum leaderboard ad, secondary
    # header bar, main-menu dropdowns) live OUTSIDE the <header> wrapper
    # above — strip each by distinguishing class/id. Multiple pb-ad
    # blocks may appear; loop.
    for _ in range(6):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*class="[^"]*\bheader-ads\b[^"]*"[^>]*>'
        )
        if html == before:
            break
    for cls_pat in (
        r'header-sidebar',
        r'main-header__secondary',
        r'secondary-dropdown',
        r'st-header',  # fixed-position article sticky toolbar
    ):
        html = _remove_nested_element(
            html, rf'<div\b[^>]*\bclass=["\']?[^"\'>]*\b{cls_pat}\b[^>]*>'
        )
    html = remove_elements_by_id(html, "main-menu")
    for _ in range(6):
        before = html
        html = _remove_nested_element(html, r"<footer\b[^>]*>")
        if html == before:
            break
    # After-credits row (Share / Download PDF) trailing the article.
    # Class is unquoted so match permissively. Don't target
    # `core-container` — that class also wraps lots of in-article
    # material (sections / figures / tables).
    html = _remove_nested_element(
        html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bafter-credits\b[^>]*>'
    )
    # Left side menu + floating nav chrome (class strings are double
    # quoted on these; selector helper matches).
    html = remove_elements_by_id(
        html,
        "header-side-menu",
        "mainNavbar",
        "article_sections_menu",
        "article_collateral_menu",
        "CybotCookiebotDialog",
        "CybotCookiebotDialogBodyUnderlay",
    )
    html = remove_elements_by_selector(html, "alert-donation")
    # "SIGN UP FOR THE AWARD-WINNING SCIENCEADVISER NEWSLETTER" promo
    # form (`<form class="news-article__newsletter ...">`) that renders
    # in the article body.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<form\b[^>]*\bclass=["\']?[^"\'>]*\bnews-article__newsletter\b',
        )
        if html == before:
            break
    # "Sign up for ScienceAdviser" newsletter-promo box that renders
    # between Abstract and INTRODUCTION. No stable semantic class —
    # the outer wrapper is a utility-class chain (`.mt-2x` >
    # `.mb-2x.mb-xl-3x` > `.d-flex...`). Strip the `.mt-2x` div that
    # contains the ScienceAdviser heading.
    m = re.search(r"Sign up for ScienceAdviser", html)
    if m:
        # Walk backward to the last `<div class="mt-2x">` before this
        # text, then use _remove_nested_element to strip it.
        back = html.rfind('<div class=mt-2x>', 0, m.start())
        if back < 0:
            back = html.rfind('<div class="mt-2x">', 0, m.start())
        if back >= 0:
            # Find matching </div> via nesting counter starting after <div>
            depth = 1
            pos = back + html[back:].find('>') + 1
            while depth > 0 and pos < len(html):
                nopen = html.find('<div', pos)
                nclose = html.find('</div>', pos)
                if nclose < 0:
                    break
                if 0 <= nopen < nclose:
                    depth += 1
                    pos = nopen + 4
                else:
                    depth -= 1
                    pos = nclose + 6
            if depth == 0:
                html = html[:back] + html[pos:]
    # Breadcrumb and content-navigation are <nav> (not <div>), so
    # remove_elements_by_selector misses them. Strip directly. Loop —
    # multiple copies exist.
    for cls_pat in ("breadcrumbs", "content-navigation"):
        for _ in range(4):
            before = html
            html = _remove_nested_element(
                html,
                rf'<nav\b[^>]*\bclass=["\']?[^"\'>]*\b{cls_pat}\b[^>]*>',
            )
            if html == before:
                break
    # Right-rail aside (core-collateral, current issue, news-feature).
    html = _remove_nested_element(
        html, r'<aside\b[^>]*data-core-aside=["\']?right-rail'
    )
    # Article sections toolbar (black bar with hamburger + article-nav
    # icons, position:sticky in native). Strip from DOM entirely — the
    # user's "no floating menu hamburger" request implies the bar
    # itself shouldn't show either, and keeping a DOM shell only leaves
    # unused vertical whitespace.
    html = _remove_nested_element(
        html, r'<div\b[^>]*\bdata-core-nav=["\']?header'
    )
    # Strip the `hidden` attribute from every <div role=listitem>
    # inside the references [data-method=clamp] wrapper. These trailing
    # entries are hidden by default and revealed on clicking
    # "Show all references"; stripping the attribute leaves them
    # rendered with native layout. The `hidden` attribute can be
    # double-quoted, unquoted, or a bare attribute — match all.
    html = re.sub(
        r'(<div\b[^>]*role=listitem[^>]*?)\s+hidden(?=[\s>])',
        r'\1',
        html,
    )
    html = re.sub(
        r'(<div\b[^>]*role=listitem[^>]*?)\s+hidden="[^"]*"',
        r'\1',
        html,
    )
    # Remove `.references-popup-wrapper` from the DOM (not just CSS hide).
    # It ships as a `position:sticky; top:80px` hover-tooltip helper —
    # offline rendering doesn't need it and its sticky positioning adds
    # a ghost 24-px offset to the article's first-rendered child even
    # when hidden with display:none in some browsers.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass=["\']?[^"\'>]*\breferences-popup-wrapper\b',
    )
    # DOM patch: Atypon ships citation metrics as
    # `<span class=total-text data-count=XXX>0</span>` and relies on
    # client JS to replace the "0" with the real count at render time.
    # SingleFile captures before JS runs, so both Downloads and
    # Citations render as "0". Substitute the real value (comma-
    # formatted) at capture time. Same pattern as the pnas parser.
    def _patch_total_text(match):
        open_tag = match.group(1)
        count = int(match.group(2))
        close_tag = match.group(3)
        return f"{open_tag}{count:,}{close_tag}"
    html = re.sub(
        r'(<span\b[^>]*\bclass=["\']?[^"\'>]*\btotal-text\b[^"\'>]*["\']?[^>]*\bdata-count=["\']?(\d+)["\']?[^>]*>)'
        r'\s*0\s*'
        r'(</span>)',
        _patch_total_text,
        html,
        flags=re.IGNORECASE,
    )

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important;"
        "overflow-y:overlay !important}"
        "html::-webkit-scrollbar{width:0 !important;height:0 !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Collapse wrappers between body and the article.
        "main,#main,#main-content,.page,.container,.container-fluid,"
        ".row,.col,[class*=col-]{"
        "display:block !important;float:none !important;"
        "width:100% !important;max-width:100% !important;"
        "min-width:0 !important;flex:0 0 auto !important;"
        "margin:0 !important;padding:0 !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # Cap the article wrapper and neutralize its grid template.
        "article[typeof=ScholarlyArticle]{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important;"
        "grid-template-columns:1fr !important;"
        "grid-template-areas:none !important}"
        "article[typeof=ScholarlyArticle] *{"
        "max-width:100% !important;min-width:0 !important;"
        "grid-area:auto !important}"
        # Exempt the info-panel action buttons (Notifications / Bookmark
        # / Cite / PDF) from the `min-width:0` sweep — native sets them
        # to 40×40 px icon buttons with ~8 px padding around the 24-px
        # icon. Zeroing min-width collapsed them to ~26 px (icon width +
        # hairline padding), making them look horizontally squashed.
        "article[typeof=ScholarlyArticle] .info-panel a.btn--slim,"
        "article[typeof=ScholarlyArticle] .info-panel .btn--pdf,"
        "article[typeof=ScholarlyArticle] .info-panel .btn{"
        "min-width:40px !important;"
        # Enforce a consistent 40×40 square across viewport widths. A
        # native media query shortens .btn--slim to 32 px height at
        # ≥992 px viewports, which makes the icon boxes look
        # horizontally stretched at wider pages. Force 40.
        "height:40px !important;min-height:40px !important}"
        # `.info-panel` is the metrics + action-buttons row between the
        # article header and body, bracketed by a 1 px top border and a
        # 1 px bottom border. Center contents vertically (symmetric
        # 12 px padding) and put metrics on the left, buttons on the
        # right (flex row, space-between) — matches the pnas layout.
        ":root article[typeof=ScholarlyArticle] .info-panel{"
        "display:flex !important;flex-direction:row !important;"
        "flex-wrap:nowrap !important;"
        "justify-content:space-between !important;"
        "align-items:center !important;"
        "padding-top:12px !important;padding-bottom:12px !important}"
        ":root article[typeof=ScholarlyArticle] .info-panel__left-content,"
        ":root article[typeof=ScholarlyArticle] .info-panel__right-content{"
        "width:auto !important;max-width:none !important;"
        "flex:0 1 auto !important;margin:0 !important;"
        # Remove the stray top border on right-content (draws a spurious
        # line above the action icons) and bottom border for symmetry.
        "border-top:0 !important;border-bottom:0 !important;"
        "padding-top:0 !important;padding-bottom:0 !important;"
        # Flex-center the inner button row vertically (default column
        # layout combined with the hidden dropdown sibling bottom-aligns
        # the tools otherwise).
        "justify-content:center !important;align-items:center !important}"
        # Restore native min-width on reference numeric labels. The
        # `*{min-width:0}` rule above collapses `[role=listitem]>.label`
        # (24-px red circle number) to its text width (~5 px). The
        # native stylesheet sets min-width:1.5rem/24px/height:1.5rem.
        "article[typeof=ScholarlyArticle] [role=listitem]>.label{"
        "min-width:1.5rem !important;height:1.5rem !important;"
        "flex:0 0 1.5rem !important;flex-shrink:0 !important}"
        # Zero margin/padding only on the wrapper's DIRECT first/last
        # children so inner sections (e.g. "Editor's summary") keep
        # their native top margin.
        "article[typeof=ScholarlyArticle]>*:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        "article[typeof=ScholarlyArticle]>*:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # Descendant *:last-child margin-bottom zero (safe per skill).
        # Catches nested trailing margins (references list, eLetters
        # sibling padding) that the direct-child form leaves behind.
        # Margin-only — descendant padding-zero kills the bordered
        # `Download` button's native pb=8 in the Supplementary Materials
        # block, collapsing it to 26 → 34 px (loses bottom border).
        "article[typeof=ScholarlyArticle] *:last-child{"
        "margin-bottom:0 !important}"
        # Author-list collapse — Atypon hides authors with
        # `data-hidden-on=sm` / `sm md` / etc. at narrow viewports
        # (replaced by a `[...]` truncation marker + "+N authors"
        # button). The 752-px body cap triggers narrow mode; force
        # all authors visible and hide the now-redundant truncation
        # markers + count pill.
        ":root .contributors .authors [data-hidden-on]{"
        "display:inline !important}"
        ".contributors .authors [data-displayed-on],"
        ".contributors .extra-authors-list,"
        ".contributors .authors__hidden-extra,"
        ".contributors__count{display:none !important}"
        # The "Show all references" button has internal vertical padding
        # that pushes past the last rendered character. Zero its internal
        # padding so the wrapper's own 56-px bottom padding is the only
        # contributor to the bottom gap.
        "article[typeof=ScholarlyArticle] .truncation-wrapper,"
        "article[typeof=ScholarlyArticle] .truncation-wrapper *{"
        "padding-top:0 !important;padding-bottom:0 !important;"
        "margin-top:0 !important;margin-bottom:0 !important;"
        "border-top:0 !important;border-bottom:0 !important}"
        # Science nests sidebars (.core-collateral, .current-issue-aside,
        # .multi-search, .card-do--news-feature) with negative margins
        # inside the article — they snap to x=0. Hide them.
        "article[typeof=ScholarlyArticle] .core-collateral,"
        "article[typeof=ScholarlyArticle] .current-issue-aside,"
        "article[typeof=ScholarlyArticle] .multi-search,"
        "article[typeof=ScholarlyArticle] .card-do--news-feature{"
        "display:none !important}"
        # `.references-popup-wrapper` is a hover popup helper (shows a
        # tooltip when you hover a reference number). Native renders it
        # as `position:sticky; top:80px`, which in our non-grid cleaned
        # layout pushes the whole article down by 24 px so first-text T
        # becomes 80 instead of target 56. Offline rendering doesn't
        # need hover popups — hide the wrapper.
        "article[typeof=ScholarlyArticle] > .references-popup-wrapper{"
        "display:none !important}"
        # Empty placeholder div inside the article header for an optional
        # news-feature badge: `<div class="mb-1_5x mt-3">` with no
        # children. Its margins (mt:16, mb:24) collapse to 24 px and
        # escape through the HEADER/core-container parents, pushing the
        # first content 24 px below the article's content-top. Zero them.
        "article[typeof=ScholarlyArticle] header .mb-1_5x.mt-3{"
        "margin:0 !important}"
        # `.meta-panel` is `display:flex; align-items:center` — the
        # left-content (article-type label) is shorter than the right-
        # content (action buttons) so flex centers it ~5 px below the
        # meta-panel's content-top. Use flex-start so the article-type
        # label aligns flush with the wrapper's padding-top edge.
        "article[typeof=ScholarlyArticle] .meta-panel{"
        "align-items:flex-start !important}"
        # Hide eLetters comments section at the bottom of the article.
        "article[typeof=ScholarlyArticle] section[data-extent=eletters],"
        "article[typeof=ScholarlyArticle] #eletters,"
        "article[typeof=ScholarlyArticle] section[id^=eletters]{"
        "display:none !important}"
        # Expand collapsed figure captions ("Expand for more") and the
        # references truncation ("Show all references"). Native layout:
        #   Captions:  <div class="collapsible-wrapper collapsed"
        #              style="height:248px"> with
        #              .collapsible-wrapper.collapsed { max-height:132px;
        #              overflow:hidden } and a ::after white-gradient
        #              overlay (140 px tall) that fades out the bottom.
        #   References: .items-collapse__items.collapse with
        #              height:510px and the same ::after gradient.
        # Override both the height constraints and the gradient overlays,
        # and hide the expand affordance buttons.
        "article[typeof=ScholarlyArticle] .collapsible-wrapper,"
        "article[typeof=ScholarlyArticle] .collapsible-wrapper.collapsed,"
        "article[typeof=ScholarlyArticle] .items-collapse__items,"
        "article[typeof=ScholarlyArticle] .items-collapse__items.collapse,"
        "article[typeof=ScholarlyArticle] "
        ".items-collapse:not(.hide-show-more) .items-collapse__items.collapse,"
        "article[typeof=ScholarlyArticle] .collapse{"
        "height:auto !important;max-height:none !important;"
        "overflow:visible !important;display:block !important;"
        "-webkit-line-clamp:unset !important;"
        "-webkit-box-orient:horizontal !important}"
        "article[typeof=ScholarlyArticle] .collapsible-wrapper.collapsed::after,"
        "article[typeof=ScholarlyArticle] .items-collapse__items::after,"
        "article[typeof=ScholarlyArticle] "
        ".items-collapse:not(.hide-show-more) .items-collapse__items::after,"
        "article[typeof=ScholarlyArticle] [data-method]::after{"
        "display:none !important;content:none !important}"
        # References list uses [data-method=clamp] on a div wrapper.
        # Un-clip it (its `hidden` attributes on trailing listitems are
        # also removed in the Step-3 DOM sweep below so items render
        # with their native layout, same as after clicking
        # "Show all references").
        "article[typeof=ScholarlyArticle] [data-method]{"
        "overflow:visible !important;max-height:none !important;"
        "height:auto !important}"
        "article[typeof=ScholarlyArticle] .collapsible-figure-btn__wrapper,"
        "article[typeof=ScholarlyArticle] .truncation-wrapper,"
        "article[typeof=ScholarlyArticle] .items-collapse__truncation{"
        "display:none !important}"
        # Figures / tables may ship with fixed pixel widths — clamp.
        "article[typeof=ScholarlyArticle] figure,"
        "article[typeof=ScholarlyArticle] table,"
        "article[typeof=ScholarlyArticle] img,"
        "article[typeof=ScholarlyArticle] iframe{"
        "width:100% !important;max-width:100% !important;"
        "height:auto !important}"
        # Figures: science.org (Atypon, same family as nejm/pnas) wraps
        # each figure in
        #   <div class=figure-wrap>
        #     <button class="figure-pop-btn">OPEN IN VIEWER</button>
        #     <figure id=F<N> class=graphic>
        #       <img src='data:image/svg+xml,<placeholder>'
        #            style="background-image:var(--sf-img-N) ..."
        #            height=<H> width=<W>>
        #       <figcaption>
        #         <div class=caption><span class=heading>Fig. N</span> title</div>
        #         <div class=notes><div role=doc-footnote>...body...</div></div>
        #       </figcaption>
        #     </figure>
        #   </div>
        # The image is rendered via SingleFile's `background-image:
        # var(--sf-img-N)` (foreground src is transparent SVG). Native
        # `<figure>` margin and the JS-only "OPEN IN VIEWER" button
        # cause visual chrome above the image. Force figure full-width
        # block, hide the JS-only button, force img display:block at
        # full width with 5 px caption gap.
        ":root article[typeof=ScholarlyArticle] figure.graphic{"
        "margin:1rem 0 !important;padding:0 !important;"
        "display:block !important;"
        "width:100% !important;max-width:100% !important}"
        ":root article[typeof=ScholarlyArticle] figure.graphic > img{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
        # Hide the JS-only OPEN IN VIEWER button (lightbox is JS-only).
        ":root article[typeof=ScholarlyArticle] .figure-wrap "
        "button.figure-pop-btn{display:none !important}"
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

def _format_given_last(name):
    """Convert 'Given Last' or 'Last, Given' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_volume_issue(html):
    """Extract volume and issue from semantic spans in the body HTML."""
    volume = ""
    vm = re.search(
        r'property=volumeNumber[^>]*>([^<]+)</span>',
        html,
    )
    if vm:
        volume = vm.group(1).strip()
    issue = ""
    im = re.search(
        r'property=issueNumber[^>]*>([^<]+)</span>',
        html,
    )
    if im:
        issue = im.group(1).strip()
    return volume, issue


def _parse_pages_from_abstract(html):
    """Extract pages from the trailing 'Journal V, FP-LP.' line in the abstract.

    SAGE abstracts often end with the formal citation, e.g.
    '<i>Antioxid. Redox Signal.</i> 39, 411-431.'
    Falls back to <span property=pageStart>FP</span>-<span property=pageEnd>LP</span>
    used by science.org.
    """
    m = re.search(
        r'<i>[^<]+</i>\s*\d+,\s*(\d[\w\-\u2013\u2014]*\s*[-\u2013\u2014]\s*\d[\w]*)\.?',
        html,
    )
    if m:
        return m.group(1).replace("\u2013", "-").replace("\u2014", "-").replace(" ", "")
    fp_m = re.search(r'property=pageStart[^>]*>([^<]+)</span>', html)
    lp_m = re.search(r'property=pageEnd[^>]*>([^<]+)</span>', html)
    if fp_m and lp_m:
        return f"{fp_m.group(1).strip()}-{lp_m.group(1).strip()}"
    if fp_m:
        return fp_m.group(1).strip()
    # science.org Sci Adv format embeds an elocator after the volume <b>:
    #   "<i>Sci. Adv.</i></span><span ...><b>9</b>,</span><span ...>eadi4148</span>"
    em = re.search(
        r'<i>[^<]+</i>\s*</span>\s*<span[^>]*>\s*<b>\d+</b>\s*,?\s*</span>'
        r'\s*<span[^>]*>\s*([a-z]{2,}[\d\-]+)\s*</span>',
        html,
    )
    if em:
        return em.group(1).strip()
    return ""


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    SAGE uses dc.* meta tags for most fields. citation_journal_title is
    present; volume/issue/pages must be parsed from body HTML.
    """
    title = get_meta(html, "dc.Title") or get_meta(html, "citation_title")

    # Prefer the ISO abbreviation embedded as <i>Abbrev.</i> in the
    # closing line of the abstract ("<i>Antioxid. Redox Signal.</i> 39, ..."),
    # since meta tags only carry the full journal title.
    journal = ""
    abbrev_m = re.search(
        r'<i>([^<]+?)</i>\s*\d+,\s*\d[\w\-\u2013\u2014]*\s*[-\u2013\u2014]\s*\d',
        html,
    )
    if abbrev_m:
        journal = abbrev_m.group(1).strip()
    if not journal:
        journal = (get_meta(html, "citation_journal_abbrev")
                   or get_meta(html, "citation_journal_title")
                   or "")
    if journal:
        journal = re.sub(r"\s+", " ", journal.replace(".", "")).strip()

    # Date: dc.Date is "YYYY-MM" or YYYY
    date = get_meta(html, "dc.Date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    # DOI: prefer dc.Identifier scheme=doi (full DOI), falling back to
    # citation_doi, then publisher-id (which uses '_' in place of '/').
    doi_raw = ""
    dm = re.search(
        r'<meta[^>]*name=["\']?dc\.Identifier["\']?[^>]*scheme=["\']?doi["\']?'
        r'[^>]*content=["\']?([^"\'\s>]+)',
        html, re.IGNORECASE,
    )
    if dm:
        doi_raw = unescape(dm.group(1))
    if not doi_raw:
        doi_raw = get_meta(html, "citation_doi")
    if not doi_raw:
        # Publisher-id form like "10.1089_ars.2022.0105"
        pm = re.search(
            r'<meta[^>]*name=["\']?dc\.Identifier["\']?[^>]*'
            r'scheme=["\']?publisher-id["\']?[^>]*content=["\']?([^"\'\s>]+)',
            html, re.IGNORECASE,
        )
        if pm:
            v = unescape(pm.group(1))
            if v.startswith("10.") and "_" in v:
                doi_raw = v.replace("_", "/", 1)
    if not doi_raw:
        # Body HTML carries the DOI link
        bm = re.search(
            r'<a[^>]*href=(https?://doi\.org/[^>"\'\s]+)',
            html,
        )
        if bm:
            doi_raw = bm.group(1)

    volume, issue = _parse_volume_issue(html)
    pages = _parse_pages_from_abstract(html)

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": format_doi(doi_raw),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_authors(html):
    """Extract authors with affiliations.

    SAGE uses Schema.org markup:
      <div id=conN property=author typeof=Person>
        <span property=givenName>Given</span>
        <span property=familyName>Last</span>
        ...
        <div class=affiliations>
          <div ...><span property=name>Affiliation text</span></div>
    """
    authors = []
    # Find each con block
    for m in re.finditer(
        r'<div[^>]*id=con(\d+)[^>]*property=author[^>]*typeof=Person[^>]*>',
        html,
    ):
        con_n = int(m.group(1))
        # End at next con block
        next_m = re.search(
            rf'<div[^>]*id=con{con_n + 1}[^>]*property=author', html[m.end():]
        )
        end = m.end() + next_m.start() if next_m else m.end() + 5000
        block = html[m.start():end]

        gn = re.search(r'property=givenName[^>]*>([^<]+)', block)
        fn = re.search(r'property=familyName[^>]*>([^<]+)', block)
        if not gn or not fn:
            continue
        given = strip_tags(gn.group(1)).strip()
        family = strip_tags(fn.group(1)).strip()
        author = _format_given_last(f"{given} {family}")

        # Affiliations: <span property=name>...</span>
        affs = []
        for am in re.finditer(
            r'property=name[^>]*>(.*?)</span>',
            block, re.DOTALL,
        ):
            text = strip_tags(am.group(1)).strip()
            if text:
                affs.append(text)
        # Filter out duplicates and the author's own name (which also gets
        # property=name on Person elements)
        seen = set()
        clean = []
        for a in affs:
            if a == family or a == given or a in seen:
                continue
            seen.add(a)
            clean.append(a)

        authors.append({"author": author, "affiliation": clean})
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_one_reference(block):
    """Parse one <div id=BN class=citations> block.

    Returns dict {title, journal, year, volume, issue, pages, doi, authors}.
    """
    # Citation text
    cm = re.search(
        r'<div[^>]*class=["\']?citation-content["\']?[^>]*>(.*?)</div>',
        block, re.DOTALL,
    )
    raw_html = cm.group(1) if cm else ""
    raw_text = strip_tags(raw_html).strip()

    # DOI from Crossref link
    doi = ""
    dm = re.search(
        r'<a[^>]*href=(https?://doi\.org/[^>"\'\s]+)',
        block,
    )
    if dm:
        doi = unescape(dm.group(1))

    # Google Scholar lookup URL carries structured fields
    title = ""
    year = ""
    pages = ""
    journal = ""
    volume = ""
    issue = ""
    authors = []
    gs = re.search(
        r'href="(https?://scholar\.google\.com/scholar_lookup\?[^"]+)"',
        block,
    )
    if gs:
        params = parse_qs(urlparse(unescape(gs.group(1))).query)
        title = params.get("title", [""])[0]
        year = params.get("publication_year", [""])[0]
        pages = params.get("pages", [""])[0]
        journal = params.get("journal", [""])[0]
        volume = params.get("volume", [""])[0]
        issue = params.get("issue", [""])[0]
        authors = list(params.get("author", []))
        if not doi:
            gs_doi = params.get("doi", [""])[0]
            if gs_doi:
                doi = format_doi(gs_doi)

    # OpenURL link can also carry fields when scholar_lookup is absent
    if not title or not authors:
        ou = re.search(
            r'href="([^"]*search\.serialssolutions\.com[^"]*)"',
            block,
        )
        if ou:
            params = parse_qs(urlparse(unescape(ou.group(1))).query)
            if not title:
                title = params.get("rft.atitle", [""])[0]
            if not journal:
                journal = (params.get("rft.jtitle", [""])[0]
                           or params.get("rft.title", [""])[0])
            if not volume:
                volume = params.get("rft.volume", [""])[0]
            if not issue:
                issue = params.get("rft.issue", [""])[0]
            if not year:
                year = params.get("rft.date", [""])[0]
            if not pages:
                fp = params.get("rft.spage", [""])[0]
                lp = params.get("rft.epage", [""])[0]
                if fp and lp:
                    pages = f"{fp}-{lp}"
                elif fp:
                    pages = fp
            if not authors:
                first = params.get("rft.aufirst", [""])[0]
                last = params.get("rft.aulast", [""])[0]
                if last:
                    authors = [f"{last} {first[:1]}".strip() if first else last]

    journal = re.sub(r"\s+", " ", journal.replace(".", "")).strip()

    # Old-style science.org citations omit the article title; the citation
    # text is just "Authors, <em>Journal</em> Vol, Pages (Year)." Both
    # scholar_lookup and OpenURL fill the journal abbreviation into the
    # generic `title` / `rft.title` slots, so `title` ends up echoing the
    # journal. Detect this by matching against the <em> abbreviation and
    # clear the spurious title. Also parse comma-separated author names
    # from the text before <em>, since the lookup URLs lack `author`.
    em_m = re.search(r'<em>([^<]+)</em>', raw_html)
    if em_m and title:
        em_text = re.sub(r"\s+", " ", em_m.group(1).replace(".", "")).strip()
        title_norm = re.sub(r"\s+", " ", title.replace(".", "")).strip()
        if em_text and em_text == title_norm:
            title = ""
            if not journal:
                journal = em_text
    if not authors and em_m:
        pre = strip_tags(raw_html[:em_m.start()]).strip().rstrip(",").strip()
        if pre:
            authors = [a.strip() for a in pre.split(",") if a.strip()]

    # Volume/issue often missing from scholar_lookup but present in the
    # citation-content text after the journal name. Two formats observed:
    #   SAGE:    "<em>Nucleic Acids Res</em>, 2012; 40(22):11531-11544;"
    #   science: "<em>Nat. Rev. Mol. Cell Biol.</em> <b>11</b>, 171-181 (2010)"
    if not volume:
        vm = re.search(
            r"</em>[,\s]*\d{4}\s*;\s*(\d+)(?:\(([^)]+)\))?\s*:\s*\d",
            raw_html,
        )
        if vm:
            volume = vm.group(1)
            if not issue and vm.group(2):
                issue = vm.group(2)
    if not volume:
        # science.org wraps the volume in <b> (sometimes nested <b><i>vol</i></b>)
        # right after </em>, with optional issue number in parentheses.
        vm = re.search(
            r"</em>\s*<b>\s*(?:<i>)?\s*(\d+)\s*(?:</i>)?\s*</b>"
            r"(?:\s*\(([^)]+)\))?",
            raw_html,
        )
        if vm:
            volume = vm.group(1)
            if not issue and vm.group(2):
                issue = vm.group(2)

    # Pages missing from scholar_lookup/OpenURL on Science e-only papers
    # (article numbers like "aab4070", "e2201662119") — pull the token
    # between the volume </b> and the "(YEAR)" from the citation text.
    if not pages:
        pm = re.search(
            r"</b>[,\s]*"
            r"(?:\([^)]*\)[,\s]*)?"  # optional issue in parens
            r"([A-Za-z]?[\w.\-\u2010-\u2014]+?)"
            r"\s*\(\d{4}\)",
            raw_html,
        )
        if pm:
            tok = re.sub(r'[\u2010-\u2014]', '-', pm.group(1)).strip(".,")
            # Accept article numbers, page ranges, or dotted numbering.
            if re.match(r'^[A-Za-z]?[\w.]+(-[A-Za-z]?[\w.]+)?$', tok):
                pages = tok

    # Reformat author strings: "R. J. O'Sullivan" -> "O'Sullivan RJ".
    # Treat ALL leading short uppercase tokens (with optional trailing dot)
    # as initials, not just the first.
    norm_authors = []
    for a in authors:
        a = a.strip()
        if not a:
            continue
        parts = a.split()
        # Detect leading initial tokens. Clean each token of dots and hyphens
        # (incl. Unicode hyphens U+2010..U+2013) so hyphenated initials like
        # "M.-B.", "J.-P.", "A.-M." are recognized as 2-letter initial
        # groups ("MB", "JP", "AM") rather than falling through to the
        # unflipped fallback.
        def _initial_form(tok):
            return re.sub(r"[.\-\u2010\u2011\u2012\u2013]", "", tok)
        i = 0
        while i < len(parts):
            tok = _initial_form(parts[i])
            if tok.isupper() and 1 <= len(tok) <= 3 and tok.isalpha():
                i += 1
            else:
                break
        if 0 < i < len(parts):
            initials = "".join(_initial_form(parts[k]) for k in range(i))
            last = " ".join(parts[i:]).rstrip(".")
            norm_authors.append(f"{last} {initials}")
        else:
            norm_authors.append(a.rstrip("."))

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "authors": norm_authors,
    }, raw_text


def _parse_references(html):
    """Extract references from <div class=citations> blocks.

    Atypon-based sites expose each reference as <div class=citations>,
    usually with an id matching a reference-number prefix:
      - SAGE: B1, B2, ...
      - science.org (modern): R1, R2, ...
      - science.org (legacy): REF1, REF2, ...
    Some legacy Science papers (e.g. 2007-era) omit the id on specific
    citation divs, instead identifying the reference number via a
    sibling ``<div class=label>N</div>``. Match both layouts and
    deduplicate by reference number so the hidden/visible copies the
    Atypon template renders don't each count.
    """
    refs = []
    seen_numbers = set()
    # Walk every <div class=citations> opening in document order.
    # Atypon Science papers render each reference in the main list plus
    # a visible core-collateral duplicate (accessible from the text), and
    # some are additionally cloned as hidden screen-reader entries. Some
    # references (e.g. legacy Science papers) omit the id attribute and
    # instead carry a sibling <div class=label>N</div>. Dedupe by the
    # numeric reference key extracted from either the id or the label so
    # every cited work appears exactly once regardless of which Atypon
    # clone is visible.
    # Match divs whose class is exactly "citations" — multi-class divs
    # like class="citations to-citation__accordion external-links" are
    # empty UI chrome (accordions/tooltips) that should not be treated
    # as references.
    for m in re.finditer(
        r'<div(?P<attrs>\s+[^>]*?)'
        r'class=(?:"citations"|\'citations\'|citations(?=[\s>]))[^>]*>',
        html,
    ):
        attrs = m.group("attrs") or ""
        before = html[max(0, m.start() - 200):m.start()]
        # Skip hidden clones (screen-reader copies) since their text
        # duplicates a visible entry elsewhere.
        if re.search(r'role=listitem[^>]*\bhidden\b', before):
            continue
        # Derive a numeric reference key from the id ("REF4", "R1",
        # "B1", "core-collateral-REF4") or from the preceding
        # <div class=label>N</div> sibling for id-less entries.
        id_m = re.search(r'\bid=\S*?([A-Z]+)(\d+)\b', attrs)
        if id_m:
            key = int(id_m.group(2))
        else:
            label_m = re.search(
                r'<div\s+class=["\']?label["\']?[^>]*>\s*(\d+)\s*</div>\s*$',
                before,
            )
            if not label_m:
                continue
            key = int(label_m.group(1))
        if key in seen_numbers:
            continue
        seen_numbers.add(key)
        next_m = re.search(
            r'<div\s+[^>]*class=["\']?citations["\']?',
            html[m.end():],
        )
        end = m.end() + next_m.start() if next_m else m.end() + 8000
        block = html[m.start():end]
        ref, raw_text = _parse_one_reference(block)
        # Fallback: use raw text as title only when no structured fields
        # were recovered (journal, volume, year all empty). Old-style
        # citations legitimately have no title; keep title empty if any
        # structured field was parsed.
        if (
            not ref["title"]
            and raw_text
            and not ref["journal"]
            and not ref["volume"]
            and not ref["year"]
        ):
            ref["title"] = raw_text
        refs.append({"": ref})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract from <h2 property=name>Abstract</h2> + sibling sections.

    SAGE abstracts use structured sub-sections (Aims, Results, Innovation,
    Conclusion) inside <section id=abs-sec-N>. science.org puts the abstract
    in <section data-extent=frontmatter>. Bounds the slice at bodymatter or
    the next h2 to avoid pulling in newsletter/related-article chrome.
    """
    abstract_stops = [
        r'<section[^>]*id=bodymatter',
        r'<section[^>]*data-extent=bodymatter',
        r'<section[^>]*class=["\']?denial-block',
        r'<div[^>]*class=["\']?alert-signup',
        r'<form[^>]*newsletter',
    ]
    chunk = _bounded_slice(
        html,
        r'<h2[^>]*property=name[^>]*>\s*Abstract\s*</h2>',
        abstract_stops,
    )
    if not chunk:
        chunk = _bounded_slice(
            html,
            r'<h2[^>]*>\s*Abstract\s*</h2>',
            abstract_stops + [r'<h2[^>]*>'],
        )
    if not chunk:
        return ""
    chunk = strip_common(chunk)
    return tags_to_text(chunk).strip()


def _bounded_slice(html, start_pat, end_pats):
    """Return the substring between start_pat and the earliest of end_pats.

    Used to slice <section> zones whose internal nested <section> tags break
    naive non-greedy matching. start_pat must capture the opening tag; the
    slice begins after it.
    """
    sm = re.search(start_pat, html)
    if not sm:
        return ""
    start = sm.end()
    end = len(html)
    for pat in end_pats:
        em = re.search(pat, html[start:])
        if em:
            end = min(end, start + em.start())
    return html[start:end]


def _parse_bodymatter(html):
    """Extract body sections inside <section id=bodymatter>.

    Slice from the bodymatter opening tag to the next backmatter (or
    citing-articles widget) start. Avoids </section> matching pitfalls that
    arise from nested sub-sections.
    """
    body = _bounded_slice(
        html,
        r'<section[^>]*id=bodymatter[^>]*>',
        [
            r'<section[^>]*id=backmatter[^>]*>',
            r'<section[^>]*id=bibliography[^>]*>',
            r'<section[^>]*class=["\']?citing-articles',
            r'<section[^>]*class=["\']?recommended-articles',
        ],
    )
    if not body:
        return ""
    # Drop denial blocks (paywalled SAGE pages)
    body = re.sub(
        r'<section[^>]*class=["\']?denial-block[^>]*>.*?</section>',
        '', body, flags=re.DOTALL,
    )
    body = extract_captions(body)
    body = strip_common(body)
    return tags_to_text(body).strip()


def _parse_backmatter(html):
    """Extract back matter (data availability, acknowledgements, supp text)
    excluding references and citing-articles widgets.

    Slices from <section id=backmatter> until either the bibliography
    section or the citing-articles list. Then drops any remaining
    bibliography or recommended-articles fragments.
    """
    backmatter = _bounded_slice(
        html,
        r'<section[^>]*id=backmatter[^>]*>',
        [
            r'<section[^>]*id=bibliography[^>]*>',
            r'<ol[^>]*class=["\']?citing-articles',
            r'<section[^>]*class=["\']?citing-articles',
            r'<section[^>]*class=["\']?recommended-articles',
            r'<div[^>]*class=["\']?trendmd',
        ],
    )
    if not backmatter:
        return ""
    backmatter = extract_captions(backmatter)
    backmatter = strip_common(backmatter)
    return tags_to_text(backmatter).strip()


def _parse_main_text(html):
    """Extract body text.

    SAGE landing pages typically expose only the abstract (paywalled body).
    Combine abstract + any non-paywalled body + back matter (excluding
    references). Falls back to abstract alone when the rest is locked.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append("## Abstract\n\n" + abstract)
    body = _parse_bodymatter(html)
    if body and body.strip() != abstract.strip():
        parts.append(body)
    back = _parse_backmatter(html)
    if back:
        parts.append(back)
    text = "\n\n".join(parts).strip()
    return drop_noise(text, _NOISE) if text else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse SAGE HTML into a papers/*.json-format dict."""
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
