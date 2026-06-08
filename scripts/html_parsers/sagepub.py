"""SAGE Publications (sagepub) HTML parser."""

import re
from html import unescape
from urllib.parse import parse_qs, urlparse

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    format_name,
    get_meta,
    neutralize_media_queries,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Lines starting with any string in this tuple are dropped from main_text
# after the text pipeline runs.
_NOISE = (
    "Open in a new tab",
    "View all access and purchase options for this article.",
    "Get full access to this article",
    "Get Access",
    "Crossref",
    "Google Scholar",
    "PubMed",
    "Web of Science",
    "Open URL",
    "View Article",
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

# CSS injected before </head> to lock the rendered article to a 720-px-wide
# native column, hide Atypon's fixed/sticky chrome, collapse Bootstrap
# col-sm-4/col-md-4 right-rail, and resolve the figure layout to a
# single block-level image above its caption.
_OVERRIDE_CSS = (
    "<style>"
    # Step 1 / Step 6 — lock layout to 752 px wide, white background.
    "html{margin:0!important;padding:0!important;"
    "background:#fff!important;}"
    "body{max-width:752px!important;width:auto!important;"
    "min-width:0!important;"
    "margin:0 auto!important;padding:0 16px!important;"
    "box-sizing:border-box!important;"
    "background:#fff!important;"
    "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
    # Atypon ships fixed-pixel `.container` widths from Bootstrap; let
    # them shrink to body so the column-margin scan stays clean.
    ".container,.container-fluid,.container-lg,.container-md,"
    ".container-sm,.container-xl,#pb-page-content,main.content"
    "{width:auto!important;max-width:100%!important;"
    "margin-left:auto!important;margin-right:auto!important;}"
    # Step 3 — hide sticky chrome (top site banner, back-to-top button,
    # sticky right-rail wrapper, off-canvas drawer panels, in-article
    # tools strip).
    "header.header.fixed,header.header.fixed.base,"
    ".scroll-to-target.fixed-element,"
    ".sticko__parent,.sticko__parent.fixed-element,"
    ".w-slide,.w-slide_head,"
    "nav.coolBar.stickybar"
    "{display:none!important;}"
    # Step 5 — hide remaining empty ad slots that survive after the
    # `pb-las` wrappers are removed (Google DFP placeholder divs,
    # bottom-skyscraper reservation, generic literatumAd widgets).
    "[id^=div-gpt-ad-],#bottom-skyscraper-placeholder,"
    "[data-widget-def=literatumAd]"
    "{display:none!important;}"
    # Step 4 — hide right-rail Support & Resources / tools column at all
    # breakpoints (the publisher's wide CSS lays it out alongside the
    # main column but it does not belong in the 720-px reading layout).
    "div.col-sm-4,div.col-md-4,div.col-lg-4"
    "{display:none!important;}"
    # Make the article column (col-sm-8 col-md-8 col-lg-8) span the body cap.
    "div.col-sm-8,div.col-md-8,div.col-lg-8"
    "{width:100%!important;max-width:100%!important;"
    "float:none!important;margin-left:0!important;"
    "margin-right:0!important;flex:0 0 100%!important;}"
    # Step 8 — figures: image above caption, both fill column.
    "figure.fig,figure.figure,figure.article__inlineFigure"
    "{display:block!important;width:100%!important;"
    "max-width:100%!important;float:none!important;"
    "margin:0 0 16px 0!important;padding:0!important;"
    "box-sizing:border-box!important;}"
    "figure.fig img,figure.figure img,figure.article__inlineFigure img"
    "{display:block!important;width:100%!important;"
    "max-width:100%!important;height:auto!important;"
    "margin:0 0 12px 0!important;"
    "box-sizing:border-box!important;}"
    "figure.fig figcaption,figure.figure figcaption,"
    "figure.article__inlineFigure figcaption"
    "{display:block!important;width:100%!important;"
    "margin:0!important;}"
    # Step 9 — no in-place push-down expansion to perform. The Atypon
    # `.author-info.accordion-tabbed__content` block opens as a floating
    # popover (position:absolute, z-index:6-10, max-width:320-360px,
    # bordered card); replicating it as push-down would violate Step 9.
    # Affiliations are already extracted by _parse_authors straight from
    # the HTML source.
    "</style>"
)


def remove_banners(html):
    """Apply Phase 2 layout rules for sagepub.com (Atypon Literatum).

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies (the
            wide-viewport CSS adds a right-rail sticky sidebar).
    Step 2: OneTrust cookie consent — sagepub ships the `#onetrust-consent-sdk`
            wrapper holding the `.onetrust-pc-dark-filter` overlay and
            `#onetrust-banner-sdk` banner. Both render as
            `position: fixed` overlays the moment the page is opened
            and remain in the DOM after capture even when the user
            has dismissed the banner. The `<div class=core-collateral>`
            tabbed-tools dialog (`role=dialog aria-modal=true`) is
            parked off-canvas at `transform: translateX(100%)` and
            extends the body's bounding rect past the cap; remove it
            so the column-margin scan sees a clean L=R envelope.
    Step 3: sticky elements — `header.header.fixed.base` (top site
            banner), `.scroll-to-target.fixed-element` (back-to-top
            button), `.sticko__parent.fixed-element` (right-rail tools/
            issue/metrics column at wide viewports), `.w-slide` /
            `.w-slide_head` (off-canvas slide-in drawers fixed
            off-screen), and `nav.coolBar.stickybar` (in-article tools
            strip).
    Step 4: hide `col-sm-4` / `col-md-4` / `col-lg-4` right rail.
    Step 5: ad slots — Atypon publishes empty `<div class=pb-las>` ad-slot
            wrappers (containing Google DFP `<div id=div-gpt-ad-*>`
            ad calls) inside the `<aside data-core-aside=right-rail>`
            and at the foot of the article body. The empty
            `#bottom-skyscraper-placeholder` reservation div and the
            generic `[data-widget-def=literatumAd]` widgets are hidden
            via CSS as well. On SAGE Open papers without
            related-articles content the surrounding
            article-aside-grid otherwise reserves a few hundred pixels
            of vertical space when the ad slots stay in.
    Step 6: page background already white; html/body forced to white
            for symmetry so the bg-around-column scan stays clean.
    Step 8: figures — `<figure class=fig>` carries an `<img>` with
            sibling `<figcaption>`. Force figure to block, image full
            column width above caption with 12 px gap.
    Step 9: no in-place push-down expansion to perform. The only
            collapsed item, `.author-info.accordion-tabbed__content`,
            is rendered by the publisher as a floating overlay
            (position:absolute, z-index:6-10, max-width:320-360px,
            bordered card framing it as a popover) — Step 9 forbids
            replicating overlays as push-down. The affiliation text
            is already harvested directly from the HTML source by
            `_parse_authors`, so visual expansion is unnecessary.
    """
    html = neutralize_media_queries(html)

    # Step 2 — OneTrust cookie consent banner + overlay, plus the
    # off-canvas core-collateral tabbed-tools dialog. Both are
    # `position: fixed` (or absolute off-screen) and stay in the DOM
    # after capture.
    while True:
        new = _remove_nested_element(
            html, r'<div\s+[^>]*\bid=["\']?onetrust-consent-sdk["\']?[^>]*>',
        )
        if new == html:
            break
        html = new
    while True:
        new = _remove_nested_element(
            html, r'<div\s+[^>]*\bid=["\']?onetrust-banner-sdk["\']?[^>]*>',
        )
        if new == html:
            break
        html = new
    for cls in ("onetrust-pc-dark-filter", "core-collateral"):
        while True:
            prev = html
            html = remove_elements_by_selector(html, cls)
            if html == prev:
                break

    # Step 5 — ad placeholders. Atypon ships empty `pb-las` ad-slot
    # wrappers (containing `div-gpt-ad-*` Google ad slots) inside the
    # `<aside data-core-aside=right-rail>` and below the article body.
    # On papers without related-articles content (e.g. SAGE Open
    # methods/perspective papers), the surrounding article-aside-grid
    # collapses and the empty ad slots reserve a few hundred pixels of
    # vertical space. The empty `bottom-skyscraper-placeholder` div and
    # any `literatumAd` widget are hidden via CSS as well.
    for cls in ("ad-slot", "advertisement", "pb-las"):
        while True:
            prev = html
            html = remove_elements_by_selector(html, cls)
            if html == prev:
                break

    if "</head>" in html:
        html = html.replace("</head>", _OVERRIDE_CSS + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", _OVERRIDE_CSS + r"\1", html, count=1)
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_volume_issue(html):
    """Extract volume and issue from semantic spans in the body HTML."""
    volume = ""
    vm = re.search(r'property=volumeNumber[^>]*>([^<]+)</span>', html)
    if vm:
        volume = vm.group(1).strip()
    issue = ""
    im = re.search(r'property=issueNumber[^>]*>([^<]+)</span>', html)
    if im:
        issue = im.group(1).strip()
    return volume, issue


def _parse_pages_from_abstract(html):
    """Extract pages from the trailing 'Journal V, FP-LP.' line in the abstract.

    SAGE biomed abstracts often end with the formal citation, e.g.
    '<i>Antioxid. Redox Signal.</i> 39, 411-431.' Falls back to
    <span property=pageStart>FP</span>-<span property=pageEnd>LP</span>.
    """
    m = re.search(
        r'<i>[^<]+</i>\s*\d+,\s*(\d[\w\-–—]*\s*[-–—]\s*\d[\w]*)\.?',
        html,
    )
    if m:
        return (
            m.group(1)
            .replace("–", "-")
            .replace("—", "-")
            .replace(" ", "")
        )
    fp_m = re.search(r'property=pageStart[^>]*>([^<]+)</span>', html)
    lp_m = re.search(r'property=pageEnd[^>]*>([^<]+)</span>', html)
    if fp_m and lp_m:
        return f"{fp_m.group(1).strip()}-{lp_m.group(1).strip()}"
    if fp_m:
        return fp_m.group(1).strip()
    return ""


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    SAGE uses dc.* meta tags for most fields. citation_journal_title carries
    the full journal title; volume/issue/pages live in the body HTML.
    """
    title = get_meta(html, "dc.Title") or get_meta(html, "citation_title")

    # Prefer the ISO abbreviation embedded as <i>Abbrev.</i> in the closing
    # line of biomed abstracts ("<i>Antioxid. Redox Signal.</i> 39, ..."),
    # since meta tags only carry the full journal title. SAGE Open papers
    # don't have the trailing-citation pattern; fall back to meta.
    journal = ""
    abbrev_m = re.search(
        r'<i>([^<]+?)</i>\s*\d+,\s*\d[\w\-–—]*\s*[-–—]\s*\d',
        html,
    )
    if abbrev_m:
        journal = abbrev_m.group(1).strip()
    if not journal:
        journal = (
            get_meta(html, "citation_journal_abbrev")
            or get_meta(html, "citation_journal_title")
            or ""
        )
    if journal:
        journal = re.sub(r"\s+", " ", journal.replace(".", "")).strip()

    # Date: dc.Date is "YYYY-MM" or YYYY
    date = get_meta(html, "dc.Date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    # DOI: prefer dc.Identifier scheme=doi (full DOI), then citation_doi,
    # then publisher-id (which uses '_' in place of '/').
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
        # Publisher-id form like "10.1089_ars.2022.0105" or "10.1177_..."
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

# Author Person blocks. SAGE renders authors twice: a compact byline in the
# header (no affiliations), and a full Authors panel in the contributors tab
# (with affiliations). The compact byline can use either <div id=conN> or
# <span property=author> depending on layout. The contributors tab uses
# <div id=conN> (or <div id=con> for single-author papers) with structured
# property=affiliation children. We collect from the whole document and
# coalesce duplicates by (given, family) so the contributor-tab affiliations
# attach to the canonical author.

_AUTHOR_OPEN_RE = re.compile(
    r'<(?P<tag>div|span)\s+[^>]*property=author[^>]*typeof=Person[^>]*>',
    re.IGNORECASE,
)


def _balanced_slice(html, m):
    """Return html[m.start():end] where end is the matching closing tag of m."""
    tag = m.group("tag")
    pos = m.end()
    depth = 1
    open_pat = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
    close_pat = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
    while depth > 0 and pos < len(html):
        next_open = open_pat.search(html, pos)
        next_close = close_pat.search(html, pos)
        if next_close is None:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            pos = next_close.end()
    return html[m.start():pos]


def _affiliations_from_block(block):
    """Extract affiliation strings from <div property=affiliation> children.

    Each affiliation block carries <span property=name>Institution</span>.
    Returns a deduped list preserving document order.
    """
    affs = []
    seen = set()
    # Restrict to property=affiliation containers so we don't pick up the
    # author's own givenName/familyName which also have property=name on
    # the Person element itself.
    for am in re.finditer(
        r'<div[^>]*property=affiliation[^>]*>(.*?)</div>\s*(?=<div|</div>)',
        block, re.DOTALL,
    ):
        inner = am.group(1)
        nm = re.search(
            r'property=name[^>]*>(.*?)</span>',
            inner, re.DOTALL,
        )
        text = strip_tags(nm.group(1) if nm else inner).strip()
        text = re.sub(r"\s+", " ", text)
        if text and text not in seen:
            seen.add(text)
            affs.append(text)
    return affs


def _parse_authors(html):
    """Extract authors with affiliations from SAGE's Schema.org markup.

    SAGE uses property=author / typeof=Person blocks. The compact byline
    block carries givenName + familyName but no affiliation; the
    contributors tab block carries the same name plus property=affiliation
    children. Walk every author block, key by (given, family), and merge
    affiliations from all duplicates.
    """
    by_key = {}
    order = []
    for m in _AUTHOR_OPEN_RE.finditer(html):
        block = _balanced_slice(html, m)
        gn = re.search(r'property=givenName[^>]*>(.*?)</span>', block, re.DOTALL)
        fn = re.search(r'property=familyName[^>]*>(.*?)</span>', block, re.DOTALL)
        if not gn or not fn:
            continue
        given = strip_tags(gn.group(1)).strip()
        family = strip_tags(fn.group(1)).strip()
        if not family:
            continue
        key = (given, family)
        affs = _affiliations_from_block(block)
        if key not in by_key:
            by_key[key] = {
                "author": format_name(given, family),
                "affiliation": [],
                "_seen_aff": set(),
            }
            order.append(key)
        slot = by_key[key]
        for a in affs:
            if a not in slot["_seen_aff"]:
                slot["_seen_aff"].add(a)
                slot["affiliation"].append(a)
    out = []
    for key in order:
        slot = by_key[key]
        out.append({"author": slot["author"], "affiliation": slot["affiliation"]})
    return out


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

# Reference id patterns: SAGE biomed uses bare B<N> ("B1"), SAGE Open and
# newer journals use "bibr<N>-<doi-tail>", legacy entries may be id-less
# with a sibling <div class=label>N</div>. Match the common shapes
# case-insensitively.
_REF_ID_RE = re.compile(r'\bid=\S*?(?:bibr|B|R|REF)0*(\d+)\b', re.IGNORECASE)


def _parse_one_reference(block):
    """Parse one <div class=citations> reference block.

    Returns ({title, journal, year, volume, issue, pages, doi, authors},
    raw_text). Pulls structured fields out of the Google Scholar lookup
    URL when present, with OpenURL serial-solutions URL as a fallback.
    Reference author strings (either compact 'Last IN' or 'Initials Last'
    forms depending on the SAGE journal template) are routed through
    format_author_name to satisfy the author-name contract.
    """
    cm = re.search(
        r'<div[^>]*class=["\']?citation-content["\']?[^>]*>(.*?)</div>',
        block, re.DOTALL,
    )
    raw_html = cm.group(1) if cm else ""
    raw_text = strip_tags(raw_html).strip()

    doi = ""
    dm = re.search(
        r'<a[^>]*href=(https?://doi\.org/[^>"\'\s]+)',
        block,
    )
    if dm:
        doi = unescape(dm.group(1))

    title = ""
    year = ""
    pages = ""
    journal = ""
    volume = ""
    issue = ""
    raw_authors = []
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
        raw_authors = list(params.get("author", []))
        if not doi:
            gs_doi = params.get("doi", [""])[0]
            if gs_doi:
                doi = format_doi(gs_doi)

    if not title or not raw_authors:
        ou = re.search(
            r'href="([^"]*search\.serialssolutions\.com[^"]*)"',
            block,
        )
        if ou:
            params = parse_qs(urlparse(unescape(ou.group(1))).query)
            if not title:
                title = params.get("rft.atitle", [""])[0]
            if not journal:
                journal = (
                    params.get("rft.jtitle", [""])[0]
                    or params.get("rft.title", [""])[0]
                )
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
            if not raw_authors:
                first = params.get("rft.aufirst", [""])[0]
                last = params.get("rft.aulast", [""])[0]
                if last:
                    raw_authors = [f"{first} {last}".strip() if first else last]

    journal = re.sub(r"\s+", " ", journal.replace(".", "")).strip()

    # Volume / issue often missing from scholar_lookup but present in the
    # citation-content text after the journal name. Two SAGE patterns:
    #   biomed:  "<em>Nucleic Acids Res</em>, 2012; 40(22):11531-11544;"
    #   medical: "<em>J Anat</em> 2012; 221: 537-567."
    #   psych:   "<em>Cell</em>, 141(4), 559-563."
    if not volume:
        vm = re.search(
            r"</em>[,\s]*(?:\d{4}\s*[;,]\s*)?(\d+)(?:\s*\(([^)]+)\))?\s*[:,]\s*\d",
            raw_html,
        )
        if vm:
            volume = vm.group(1)
            if not issue and vm.group(2):
                issue = vm.group(2)

    # Author-name contract: combined name strings (e.g. "S. J. Altschuler",
    # "MS. Alam", "A Vleeming") go through format_author_name, which
    # routes through parse_combined_name + format_name.
    authors = []
    for raw in raw_authors:
        s = unescape(raw).strip()
        if not s:
            continue
        formatted = format_author_name(s)
        if formatted:
            authors.append(formatted)

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "authors": authors,
    }, raw_text


def _parse_references(html):
    """Extract references from <div class=citations> blocks.

    Atypon exposes each reference as <div class=citations>, optionally with
    an id like B<N> (biomed) or bibr<N>-<doi-tail> (SAGE Open). The same
    reference is rendered multiple times (main list + screen-reader/visible
    duplicates); dedupe by the numeric reference key extracted from the id
    or from a sibling <div class=label>N</div>.
    """
    refs = []
    seen_numbers = set()
    # Match divs whose class is exactly "citations" — multi-class divs like
    # class="citations to-citation__accordion external-links" are empty UI
    # chrome (accordions/tooltips) that should not be treated as references.
    for m in re.finditer(
        r'<div(?P<attrs>\s+[^>]*?)'
        r'class=(?:"citations"|\'citations\'|citations(?=[\s>]))[^>]*>',
        html,
    ):
        attrs = m.group("attrs") or ""
        before = html[max(0, m.start() - 200):m.start()]
        # Skip hidden screen-reader clones — their text duplicates a visible
        # entry elsewhere.
        if re.search(r'role=listitem[^>]*\bhidden\b', before):
            continue
        id_m = _REF_ID_RE.search(attrs)
        if id_m:
            key = int(id_m.group(1))
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
        # Fallback: use raw text as title when no journal/volume/pages were
        # recovered. Covers software/dataset citations and unstructured
        # entries where structured links carry only author + year.
        # Strip a leading "1. " label that appears on numbered SAGE journals.
        if (
            not ref["title"]
            and raw_text
            and not ref["journal"]
            and not ref["volume"]
            and not ref["pages"]
        ):
            ref["title"] = re.sub(r"^\s*\d+\.\s*", "", raw_text)
        refs.append({"": ref})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

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


def _parse_abstract(html):
    """Extract the abstract section.

    SAGE biomed abstracts use structured sub-sections (Aims, Results,
    Innovation, Conclusion) inside <section id=abs-sec-N>. SAGE Open uses
    a flat <div role=paragraph>. Bounds the slice at bodymatter or the next
    h2 to avoid pulling in newsletter / related-article chrome.
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
    # Drop denial blocks (paywalled landing pages)
    body = re.sub(
        r'<section[^>]*class=["\']?denial-block[^>]*>.*?</section>',
        '', body, flags=re.DOTALL,
    )
    body = extract_captions(body)
    body = strip_common(body)
    return tags_to_text(body).strip()


def _parse_backmatter(html):
    """Extract back matter (acknowledgements, data availability, supp text).

    Excludes references and citing-articles widgets.
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

    Combines abstract + bodymatter + backmatter (excluding references).
    SAGE landing pages may expose only the abstract (paywalled body); the
    abstract carries through in that case.
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
