"""The Royal Society Publishing (royalsocietypublishing) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    format_name,
    get_meta,
    neutralize_media_queries,
    parse_meta_authors,
    remove_elements_by_id,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Lines starting with any string in this tuple are dropped from main_text
# after the text pipeline runs.
_NOISE = (
    "Open in a new tab",
    "Google Scholar",
    "Crossref",
    "Search ADS",
    "PubMed",
    "OpenURL",
    "WorldCat",
    "Web of Science",
)

# Reference section heading pattern
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# h2 classes for body vs back matter vs references
_BODY_HEADING = "section-title"
_BACK_HEADING = "backsection-title"
_REF_HEADING = "backreferences-title"
_ABSTRACT_HEADING = "abstract-title"


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

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
    # Atypon ships fixed-pixel `.container` widths; let them shrink to body.
    ".container,#main,.content-main,.content-main_content,"
    "section.master-main,section.master-header,section.footer_wrap"
    "{width:auto!important;max-width:100%!important;"
    "margin-left:auto!important;margin-right:auto!important;}"
    # Step 3 — hide sticky chrome (skipnav fixed accessibility link,
    # mobile-mask off-canvas drawer overlay, OneTrust cookie banner).
    "a.skipnav,.mobile-mask,"
    "#onetrust-banner-sdk,#onetrust-consent-sdk,.otPcPanel,.otFlat"
    "{display:none!important;}"
    # Step 4 — hide InfoColumn (left sidebar) and Sidebar (right rail).
    # The 3-column grid collapses to single-column; ContentColumn spans.
    "#InfoColumn,#Sidebar"
    "{display:none!important;}"
    # Atypon page-column-wrap is `display:grid` with three template
    # columns and a negative right margin (margin:24px -32px 24px 0)
    # used by the publisher's bootstrap row math — collapse to single
    # column and zero the negative gutter so ContentColumn aligns with
    # body's internal padding.
    ".page-column-wrap,.page-column-wrap.article-browse_content"
    "{display:block!important;width:100%!important;max-width:100%!important;"
    "margin-left:0!important;margin-right:0!important;"
    "padding-left:0!important;padding-right:0!important;}"
    "#ContentColumn,div.page-column.page-column--center"
    "{width:100%!important;max-width:100%!important;"
    "float:none!important;margin:0!important;padding:0!important;}"
    # Step 8 — figures: image above caption, both fill column.
    "figure.fig,figure.figure,div.fig-graphic"
    "{display:block!important;width:100%!important;"
    "max-width:100%!important;float:none!important;"
    "margin:0 0 16px 0!important;padding:0!important;"
    "box-sizing:border-box!important;text-align:left!important;}"
    "figure.fig img,figure.figure img,"
    "div.fig-graphic img.content-image"
    "{display:block!important;width:100%!important;"
    "max-width:100%!important;height:auto!important;"
    "margin:0 0 12px 0!important;float:none!important;"
    "box-sizing:border-box!important;}"
    "figure.fig figcaption,figure.figure figcaption,"
    "div.fig-caption"
    "{display:block!important;width:100%!important;"
    "margin:0!important;}"
    # Step 11 — figshare-widget article container ships its own
    # fixed pixel width and a footer that overflows past the body cap.
    # Cap the widget root and its inline-mode container so they shrink
    # to the column.
    "figshare-widget,"
    "[class*='frontend-filesViewer-inlineMode']"
    "{max-width:100%!important;width:auto!important;"
    "box-sizing:border-box!important;}"
    # Step 9 — no expansion. The .al-author-info-wrap and
    # .info-card-author.authorInfo_ArticleTopInfo containers open as
    # floating overlays (position:absolute, z-index, box-shadow,
    # bordered card ~318px wide); replicating them as push-down would
    # violate Step 9. Affiliations are already extracted by
    # _parse_authors directly from the HTML source.
    "</style>"
)


def remove_banners(html):
    """Apply Phase 2 layout rules for royalsocietypublishing.org (Atypon).

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies (the
            wide-viewport CSS lays out a 3-column grid: InfoColumn
            sidebar / ContentColumn / Sidebar right rail).
    Step 2: OneTrust cookie consent banner — `#onetrust-banner-sdk` is
            position:fixed at bottom (z-index 2147483645) and persists
            in the captured DOM; hide it plus the sibling
            `#onetrust-consent-sdk` and `.otPcPanel` preference panel.
    Step 3: sticky elements — `a.skipnav` (fixed accessibility skip
            link), `.mobile-mask` (fixed off-canvas drawer overlay),
            `#InfoColumn` (becomes position:fixed at narrow viewports
            as the slide-in drawer; sticky `.info-inner-wrap.can-stick`
            sits inside it). Hide all.
    Step 4: vertical columns — at wide viewports the page-column-wrap
            grid lays out InfoColumn + ContentColumn + Sidebar
            side-by-side. Hide `#InfoColumn` and `#Sidebar`, collapse
            the grid to block, force ContentColumn to span the body cap.
    Step 5: ad slots — `.ad-banner` reservation wrappers ship with the
            article template (top-of-page banner, riser, etc.). Remove
            on sight even when the ad payload itself failed to load.
    Step 6: page background already white; html/body forced to white
            for symmetry so the bg-around-column scan stays clean.
    Step 7: image-quality issues vary by paper — figures live in
            `<div class=fig-graphic><img class=content-image>` blocks.
            Some captures ship `data:,` placeholders for figure images
            (retrieval failure); resolve via convert_html.py rerun /
            get_html.py post-capture hook, not in the parser.
    Step 8: figures — `figure.fig` and bare `div.fig-graphic` carry
            `<img class=content-image>` plus a sibling caption block.
            Force figure to block, image full column width above
            caption with 12 px gap.
    Step 9: no in-place push-down expansion to perform. The collapsed
            items, `.al-author-info-wrap` and
            `.info-card-author.authorInfo_ArticleTopInfo`, are
            rendered as floating popovers (position:absolute,
            z-index:1, box-shadow, bordered card ~318px wide) — Step 9
            forbids replicating overlays as push-down. Affiliations
            are already harvested directly from the HTML source by
            `_parse_authors`.
    """
    html = neutralize_media_queries(html)

    # Step 5 — ad-banner reservation wrappers. Atypon emits these as
    # top/bottom site-wide and right-rail ad slots.
    for cls in ("ad-banner", "advertisement"):
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

def _parse_citation_primary(html):
    """Parse journal, volume, issue, pages, DOI from ww-citation-primary div.

    Royal Society format: '<em>Journal</em> (YYYY) Vol (Issue): elocator .'
    """
    m = re.search(
        r'class=ww-citation-primary[^>]*>(.*?)</div>',
        html,
        re.DOTALL,
    )
    if not m:
        return {}, ""

    content = m.group(1)

    # Journal name from <em>
    journal = ""
    jm = re.search(r'<em>(.*?)</em>', content)
    if jm:
        journal = strip_tags(jm.group(1)).strip()

    # DOI from link (may be absent in RSP citation-primary)
    doi = ""
    dm = re.search(r'href=["\']?(https?://doi\.org/[^"\'>\s]+)', content)
    if dm:
        doi = dm.group(1)

    text = strip_tags(content).strip()

    # Volume / Issue / Pages — try OUP-style strings first, then RSP shape
    volume = ""
    vm = re.search(r'Volume\s+(\d+)', text)
    if vm:
        volume = vm.group(1)
    issue = ""
    im = re.search(r'Issue\s+(\d+)', text)
    if im:
        issue = im.group(1)

    pages = ""
    pm = re.search(r'Pages?\s+(\S+)', text)
    if pm:
        pages = pm.group(1).rstrip(",")
        pages = re.sub(r'[–—]', '-', pages)

    # RSP shape: 'Journal (YYYY) VOL (ISSUE): pages.'
    if not (volume and issue and pages):
        rsp = re.search(
            r'\((\d{4})\)\s*(\d+)\s*(?:\(\s*([^)]+?)\s*\))?\s*:\s*([^.\s]+)',
            text,
        )
        if rsp:
            if not volume:
                volume = rsp.group(2) or ""
            if not issue:
                issue = (rsp.group(3) or "").strip()
            if not pages:
                pages = rsp.group(4).rstrip(".,")

    # Year
    year = ""
    ym = re.search(r'\((\d{4})\)', text)
    if ym:
        year = ym.group(1)
    elif (ym := re.search(r'(\d{4})', text)):
        year = ym.group(1)

    return {
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
    }, doi


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Royal Society Publishing uses Silverchair, so citation_* meta tags are
    present. Falls back to ww-citation-primary div for fields the meta tags
    omit (issue, pages on certain papers).
    """
    title = get_meta(html, "citation_title")
    journal = (
        get_meta(html, "citation_journal_abbrev")
        or get_meta(html, "citation_journal_title")
    )
    volume = get_meta(html, "citation_volume")
    issue = get_meta(html, "citation_issue")
    doi_raw = get_meta(html, "citation_doi")
    date = get_meta(html, "citation_publication_date")
    year = ""
    if date:
        ym = re.search(r"(\d{4})", date)
        if ym:
            year = ym.group(1)

    if not title or not journal:
        citation, _ = _parse_citation_primary(html)
        if not title:
            m = re.search(r'og:title[^>]*content="([^"]+)"', html)
            if m:
                title = unescape(m.group(1)).strip()
        if not journal:
            journal = citation.get("journal", "")
        if not volume:
            volume = citation.get("volume", "")
        if not issue:
            issue = citation.get("issue", "")
        if not year:
            year = citation.get("year", "")
        if not doi_raw:
            doi_raw = citation.get("doi", "")

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage
    if not pages:
        citation, _ = _parse_citation_primary(html)
        pages = citation.get("pages", "")
        if not issue:
            issue = citation.get("issue", "")
        if not volume:
            volume = citation.get("volume", "")
        if not year:
            year = citation.get("year", "")

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

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Royal Society's Silverchair build emits citation_author and
    citation_author_institution meta tags in document order, the same
    structure parse_meta_authors handles. Falls back to al-author-name
    + info-card-author widgets when meta tags are absent.
    """
    info_card_affs = {}
    for m in re.finditer(
        r'<div\s+class="info-card-author[^"]*"[^>]*>(.*?)(?=<div\s+class="info-card-author|\Z)',
        html, re.DOTALL,
    ):
        block = m.group(1)
        name_m = re.search(
            r'<div\s+class=info-card-name[^>]*>\s*([^<]+?)\s*(?:<|$)',
            block,
        )
        if not name_m:
            continue
        display = unescape(name_m.group(1)).strip()
        if not display:
            continue
        affs = []
        pos = 0
        while pos < len(block):
            am = re.search(r'<div\s+class=aff\b[^>]*>', block[pos:])
            if not am:
                break
            start = pos + am.end()
            depth = 1
            p = start
            inner = ""
            while depth > 0 and p < len(block):
                no = re.search(r'<div[\s>]', block[p:])
                nc = re.search(r'</div>', block[p:])
                if not nc:
                    break
                if no and no.start() < nc.start():
                    depth += 1
                    p += no.end()
                else:
                    depth -= 1
                    if depth == 0:
                        inner = block[start:p + nc.start()]
                    p += nc.end()
            else:
                inner = block[start:p]
            txt = re.sub(
                r'<span[^>]*class="?label[^>]*>.*?</span>',
                '', inner, flags=re.DOTALL,
            )
            txt = unescape(re.sub(r'<[^>]+>', ', ', txt))
            txt = re.sub(r'\s*,\s*,\s*', ', ', txt).strip(' ,')
            txt = re.sub(r'\s+', ' ', txt).strip()
            if txt:
                affs.append(txt)
            pos = p
        if affs:
            info_card_affs[display] = affs

    def lookup_info_card(meta_name):
        """Match a citation_author display name to the info-card lookup."""
        if meta_name in info_card_affs:
            return info_card_affs[meta_name]
        if "," in meta_name:
            last, given = meta_name.split(",", 1)
            flipped = f"{given.strip()} {last.strip()}"
            if flipped in info_card_affs:
                return info_card_affs[flipped]
        return []

    meta_authors = parse_meta_authors(html)
    if meta_authors:
        result = []
        for a in meta_authors:
            affs = a.get("affiliations", [])
            if not affs:
                affs = lookup_info_card(a["name"])
            result.append({
                "author": format_author_name(a["name"]),
                "affiliation": affs,
            })
        # Shared-affiliation broadcast (same rules as oup.py)
        with_affs = [a for a in result if a["affiliation"]]
        unique_affs = {tuple(a["affiliation"]) for a in with_affs}
        if len(with_affs) >= 2 and len(unique_affs) == 1:
            shared = list(next(iter(unique_affs)))
            for a in result:
                if not a["affiliation"]:
                    a["affiliation"] = list(shared)
        elif (len(result) >= 3 and result[-1]["affiliation"]
                and len(with_affs) == 1
                and sum(1 for a in result if not a["affiliation"]) >= 2):
            shared = list(result[-1]["affiliation"])
            for a in result:
                if not a["affiliation"]:
                    a["affiliation"] = list(shared)
        return result

    # Fallback: body HTML author links
    authors = []
    seen = set()
    for m in re.finditer(
        r'class="al-author-name[^"]*"[^>]*>.*?'
        r'<a[^>]*>([^<]+)</a>',
        html,
        re.DOTALL,
    ):
        name = unescape(m.group(1)).strip()
        if name and name not in seen and not name.startswith("http"):
            seen.add(name)
            affs = info_card_affs.get(name, [])
            authors.append({
                "author": name,
                "affiliation": affs,
            })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Royal Society uses Silverchair ref-list markup with each entry wrapped
    in <div data-content-id=... xmlns:helper=...><div class="ref false">
    <div class=ref-content>. Inside, structured fields appear as
    <div class=surname>/<div class=given-names>/<div class=article-title>/
    <div class=source>/<div class=year>/<div class=volume>/<div class=fpage>/
    <div class=lpage>.
    """
    refs = []
    m = re.search(r'class=(?:"[^"]*ref-list[^"]*"|ref-list\b)', html)
    if not m:
        return refs

    ref_section = html[m.start():]

    # Each ref begins with <div data-content-id=...>
    items = list(re.finditer(
        r'<div\s+content-id=\S+\s+class=js-splitview-ref-item\b', ref_section,
    ))
    if not items:
        items = list(re.finditer(
            r'<div\s+(?:data-)?content-id=\S+', ref_section,
        ))
    if not items:
        return refs

    last_end = len(ref_section)
    after_last = ref_section[items[-1].end():]
    boundary = re.search(
        r'<div\s+class=["\']?(?:widget|copyright|license|footnote)\b',
        after_last,
    )
    if boundary:
        last_end = items[-1].end() + boundary.start()

    for idx, im in enumerate(items):
        end = items[idx + 1].start() if idx + 1 < len(items) else last_end
        entry = ref_section[im.start():end]

        def _field(cls):
            fm = re.search(
                rf'class=(?:"{cls}"|{cls}\b)[^>]*>(.*?)</div>',
                entry, re.DOTALL,
            )
            return strip_tags(fm.group(1)).strip() if fm else ""

        title = _field("article-title")
        if not title:
            title = _field("chapter-title")

        journal = _field("source").replace(".", "")
        volume = _field("volume")
        issue = _field("issue")
        year = _field("year")
        fpage = _field("fpage")
        lpage = _field("lpage")
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        if not pages:
            pm = re.search(
                r'<strong>\s*\d+\s*</strong>\s*,\s*'
                r'(\d[\d\w]*)\s*[–—-]\s*(\d[\d\w]*)',
                entry,
            )
            if pm:
                pages = f"{pm.group(1)}-{pm.group(2)}"
            else:
                pm = re.search(
                    r'[\s,;:]\s*(\d+)\s*[–—-]\s*(\d+)\s*\.?\s*(?:<|$)',
                    entry,
                )
                if pm:
                    pages = f"{pm.group(1)}-{pm.group(2)}"

        # Authors. <div class=name><div class=surname>X</div> <div class=given-names>Y</div></div>
        authors = []
        for nm in re.finditer(
            r'class=(?:"surname"|surname\b)[^>]*>([^<]*)</div>'
            r'.{0,20}?'
            r'class=(?:"given-names"|given-names\b)[^>]*>([^<]*)</div>',
            entry, re.DOTALL,
        ):
            surname = unescape(nm.group(1)).strip().rstrip(",")
            given = unescape(nm.group(2)).strip()
            authors.append(format_name(given, surname))

        # DOI
        doi = ""
        dm = re.search(
            r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry,
        )
        if dm:
            doi = format_doi(unescape(dm.group(1)))
        if not doi:
            dm = re.search(r'doi:\s*(10\.\S+?)[\])<,\s]', entry)
            if dm:
                doi = format_doi(unescape(dm.group(1)))

        if not title and not authors:
            title = strip_tags(entry).strip()

        refs.append({"": {
            "title": title,
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract text from the <section class=abstract> block.

    Royal Society's first abstract-title h2 is followed by <section class=abstract>.
    Skips graphical abstracts (rare on RSP but matches the OUP convention).
    """
    for m in re.finditer(
        r'<section\s+class=(["\']?)abstract\1[^>]*>(.*?)</section>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        tag = html[m.start():m.start() + 120].lower()
        if 'graphical' in tag:
            continue
        text = strip_tags(m.group(2)).strip()
        if text:
            return text
    m = re.search(
        r'class="[^"]*abstract-title[^"]*"[^>]*>.*?</h2>\s*<section[^>]*>(.*?)</section>',
        html, re.DOTALL,
    )
    if m:
        return strip_tags(m.group(1)).strip()
    return ""


def _parse_keywords(html):
    """Extract keywords from RSP's content-metadata-keywords block."""
    keywords = []
    tm = re.search(
        r'class=["\']?content-metadata-keywords-title[^>]*>.*?</div>',
        html, re.DOTALL,
    )
    if tm:
        tail = html[tm.end():tm.end() + 4000]
        stop = re.search(r'</div>\s*</div>', tail)
        scope = tail[:stop.start()] if stop else tail
        for am in re.finditer(
            r'<a[^>]*class=["\']?content-metadata--item[^>]*>(.*?)</a>',
            scope, re.DOTALL,
        ):
            kw = strip_tags(am.group(1)).strip().rstrip(",")
            if kw:
                keywords.append(kw)
    if not keywords:
        m = re.search(
            r'class=["\']?kwd-group[^>]*>(.*?)</div>',
            html, re.DOTALL,
        )
        if m:
            for km in re.finditer(
                r'class=["\']?kwd-part[^>]*>(.*?)</(?:span|a)>',
                m.group(1), re.DOTALL,
            ):
                kw = strip_tags(km.group(1)).strip().rstrip(",")
                if kw:
                    keywords.append(kw)
    return keywords


def _extract_div_content(html, start_pos):
    """Extract full content of a div starting at start_pos (after opening tag)."""
    pos = start_pos
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
            if depth == 0:
                return html[start_pos:pos + next_close.start()]
            pos += next_close.end()
    return html[start_pos:pos]


def _find_h2_sections(html):
    """Find Silverchair section headings.

    Returns list of (start_pos, heading_text, class_type) where class_type
    is 'body', 'back', 'ref', 'abstract', or 'other'.
    """
    entries = []
    for m in re.finditer(
        r'<h2[^>]*class=(?:"([^"]*)"|(\S+))[^>]*>(.*?)</h2>',
        html, re.DOTALL,
    ):
        cls = m.group(1) or m.group(2) or ""
        text = strip_tags(m.group(3)).strip()
        if not text:
            continue
        if _ABSTRACT_HEADING in cls:
            kind = "abstract"
        elif _REF_HEADING in cls:
            kind = "ref"
        elif _BACK_HEADING in cls:
            kind = "back"
        elif _BODY_HEADING in cls:
            kind = "body"
        else:
            kind = "other"
        entries.append((m.start(), text, kind))
    return entries


def _parse_body(html):
    """Extract the body-zone text (between abstract and references)."""
    body_m = re.search(
        r'<div[^>]*class=(?:"[^"]*article-body[^"]*"|article-body\b)[^>]*>',
        html,
    )
    if not body_m:
        return ""

    content = _extract_div_content(html, body_m.end())
    h2s = _find_h2_sections(content)
    if not h2s:
        return ""

    start = 0
    for pos, text, kind in h2s:
        if kind == "abstract":
            sec_end = content.find('</section>', pos)
            if sec_end >= 0:
                start = sec_end + len('</section>')
            else:
                start = pos + 200
        elif kind in ("body", "back"):
            break

    first_ref_idx = None
    for i, (pos, text, kind) in enumerate(h2s):
        if kind == "ref" or _REF_RE.search(text):
            first_ref_idx = i
            break

    parts = []

    first_body_pos = None
    for pos, text, kind in h2s:
        if kind in ("body", "back") and pos >= start:
            first_body_pos = pos
            break
    if first_body_pos is not None and start < first_body_pos:
        gap = content[start:first_body_pos]
        meta_end = re.search(
            r'class="[^"]*article-metadata-panel[^"]*"[^>]*>',
            gap,
        )
        if meta_end:
            first_p = re.search(r'<p\b', gap[meta_end.end():])
            if first_p:
                intro_start = start + meta_end.end() + first_p.start()
                if intro_start < start + first_body_pos:
                    parts.append((intro_start, first_body_pos))
        else:
            parts.append((start, first_body_pos))

    for i, (pos, text, kind) in enumerate(h2s):
        if kind == "abstract" or kind == "ref" or _REF_RE.search(text):
            continue
        end_pos = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)
        if pos < start:
            continue
        if first_ref_idx is None or i < first_ref_idx:
            parts.append((pos, end_pos))
        else:
            if _SUPP_RE.search(text):
                parts.append((pos, end_pos))

    if not parts:
        end_pos = len(content)
        if first_ref_idx is not None:
            end_pos = h2s[first_ref_idx][0]
        if start < end_pos:
            parts.append((start, end_pos))

    if not parts:
        return ""

    body_html = ""
    for s, e in parts:
        body_html += content[s:e]

    body_html = re.sub(
        r'\s+data-section-title="[^"]*"', '', body_html,
    )
    body_html = re.sub(
        r"\s+data-section-title='[^']*'", '', body_html,
    )

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


def _parse_main_text(html):
    """Extract body text.

    Composition: '## Abstract' + abstract, '## Keywords' + keywords, body.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")
    keywords = _parse_keywords(html)
    if keywords:
        parts.append(f"## Keywords\n\n{', '.join(keywords)}")
    body = _parse_body(html)
    if body:
        parts.append(body)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Royal Society Publishing HTML into a papers/*.json-format dict."""
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
