"""Cancer Research and Treatment / Korean Cancer Society (e_crt) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    format_name,
    get_all_meta,
    get_meta,
    neutralize_media_queries,
    parse_combined_name,
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Lines starting with any string in this tuple are dropped from main_text
# after the text pipeline runs. Populate after running the parser end-to-end
# and inspecting the residual noise that survives extract_captions and
# strip_common (e.g. "Open in a new tab", "Download Article", "Google Scholar").
_NOISE = (
    "Download Figure",
    "Download Table",
    "Click here",
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for e-crt.org.

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies. The
            site's default desktop CSS forces `#container{width:1250px}`
            and `.wrapper{min-width:1250px}` outside of @media — those
            need explicit overrides on top of the neutralizer.
    Step 2: no cookie/consent banner ships in the captured HTML.
    Step 3: remove sticky chrome flagged by scan_sticky.py — the
            `.top_btn` floating "TOP" anchor (`position:fixed bottom:0
            right:0`), the entire `#headerWrap` site nav (the
            publisher's JS converts it to `position:fixed top:0` on
            scroll past its native position so it pins to the viewport
            page-wide), the `.article_paging` prev/next chevrons that
            JS pins to `position:fixed` on the same scroll trigger,
            the JS-toggled `.articleMenu.fixed` tab bar that re-pins
            to the viewport, and the `#MathJax_Message` `position:fixed`
            status pill.
    Step 4: no full-height vertical sidebar columns ship beside the
            article column — `#container` is single-column at any
            viewport once Step 1 collapses it.
    Step 5: no ad slots ship in the captured HTML (no ad/gpt/dfp/
            sponsored markers found on the article page).
    Step 6: page background is white. The dark-grey `#footerWrap`
            (`background:#2f2f2f`) and the publisher's `#footerWrap:after`
            page-wide colored band live below the article column —
            kept as publisher-native chrome but capped to body width
            so the dark band doesn't bleed past the centered cap.
    Step 7: figures are inlined as <img> inside `div.panel`. Captures
            in the test fixtures inline at full natural resolution
            (no thumbnail / placeholder pattern observed). No retrieval
            issue.
    Step 8: figures live inside `<div class=panel>` with the image as
            a direct child and the caption + download link as
            siblings. The site's default rule is `div.panel>img{width:80%
            margin:auto}`; force block layout, image at column width,
            caption below.
    Step 9: the publisher's three article-header tabs ("Author
            information", "Article notes", "Copyright and License
            information") are rendered as `<div class=tabCon
            style="display:none">` and toggled in-place push-down by
            the publisher's own UI control (no `position:absolute|fixed`,
            no `z-index`, no `box-shadow` — opening just changes
            `display:none` to `block` and shifts following content
            down). Force the three tabCon panels visible. The
            duplicated tab-content divs (`#a_data`, `#a_references`,
            `#a_citations`, `#a_metrics`, all `style=height:0;overflow:
            hidden`) are NOT expanded — they are tab-switched panels
            that overlay each other, not push-down content, and they
            duplicate body content the parser already extracts.
    """
    html = neutralize_media_queries(html)

    # Step 3 — sticky chrome.
    # `.top_btn` is an <a>, so the helper's <div>-only selector match
    # is insufficient — match by class on any tag.
    html = _remove_nested_element(
        html, r'<a[^>]*\bclass=top_btn\b[^>]*>'
    )
    # The publisher's site-wide nav bar (publisher logo + main menu +
    # Korean translate widget). The static CSS is `position:relative`
    # but a JS scroll handler on the page promotes `#headerWrap` to
    # `position:fixed top:0` once scrolled past, so it pins page-wide.
    html = remove_elements_by_id(html, "headerWrap")
    # The two prev/next article navigation chevrons in `.article_paging`.
    # Static rule is `position:absolute`, JS converts to `position:fixed`
    # on scroll past the article header — they then float at viewport
    # mid-height alongside scrolled content.
    html = _remove_nested_element(
        html, r'<div[^>]*\bclass=article_paging\b[^>]*>'
    )
    # The article-tab nav bar (Full Article / Figure & data / Reference /
    # Citations / Metrics / Download PDF). Lives in `<div class=articleMenu>`
    # at the top of the article body and JS-toggles to `.articleMenu.fixed`
    # (position:fixed top:58px) on scroll past its native position.
    html = _remove_nested_element(
        html, r'<div[^>]*\bclass=articleMenu\b[^>]*>'
    )
    # MathJax progress pill (position:fixed bottom:1.5em z-index:102).
    # Hidden at load via inline style, but JS reveals it during
    # MathJax processing — strip outright so it can't paint.
    html = remove_elements_by_id(html, "MathJax_Message")
    # Full-screen image-viewer and table-viewer modals
    # (`.image_figure_wrap` / `.image_table_wrap`, both
    # `position:fixed top:0 z-index:1000`). They open via JS click on
    # a figure/table thumbnail and tile a viewer over the entire
    # viewport; their `position:absolute` thumbnails inside still pin
    # to viewport edges even when the wrapper is `height:0`.
    html = _remove_nested_element(
        html, r'<div[^>]*\bclass=image_figure_wrap\b[^>]*>'
    )
    html = _remove_nested_element(
        html, r'<div[^>]*\bclass=image_table_wrap\b[^>]*>'
    )
    # The figure-/table-zoom photo viewer overlay (`#overlay`,
    # `position:absolute top:0 left:0 width:100% height:100%`). Hidden
    # at load via inline style but a JS click handler reveals it.
    html = remove_elements_by_id(html, "overlay")

    override = (
        "<style>"
        # Step 1 / Step 6 — lock layout to 752 px, center, white bg.
        "html{margin:0!important;padding:0!important;"
        "background:#fff!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "min-width:0!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "background:#fff!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # Page-wide e-crt wrappers ship explicit pixel widths
        # (`#container{width:1250px}`, `.wrapper{min-width:1250px}`,
        # `#gnb{width:1250px}`) outside of @media queries. Collapse
        # them to body so they shrink to viewport.
        ".wrapper,#container,#headerWrap,#footerWrap,.contents,"
        "#gnb,.articleBrief,.titArea,.articleCon"
        "{width:auto!important;min-width:0!important;max-width:100%!important;"
        "margin-left:auto!important;margin-right:auto!important;"
        "padding-left:0!important;padding-right:0!important;"
        "box-sizing:border-box!important;}"
        # The site renders #container as a 350-px-left-padded
        # `.titArea` + `.contents` column split. Cancel the offset.
        ".titArea{padding:20px 0!important;height:auto!important;}"
        ".contents{float:none!important;width:100%!important;}"
        # Step 8 — figures: image above caption, image at column width.
        "div.panel{padding:15px!important;margin:16px 0!important;"
        "box-sizing:border-box!important;max-width:100%!important;"
        "overflow-x:auto!important;}"
        "div.panel>img{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 auto 8px auto!important;}"
        "div.panel a.download{float:none!important;display:inline-block!important;"
        "margin-top:8px!important;}"
        # Step 9 — expand the three article-header tabCon panels
        # (Author information / Article notes / Copyright and License).
        # Native UI is in-place push-down, not overlay.
        ".tabCon{display:block!important;}"
        # Tables inside articleCon: cap to column width with horizontal
        # scroll so wide data tables don't stretch the body cap.
        "div.articleCon table{max-width:100%!important;"
        "table-layout:auto!important;}"
        # Step 10 — hide the empty `articleCon id=a_*` tab residuals.
        # The publisher ships sibling `<div class=articleCon id=a_data>`
        # / `id=a_references` / `id=a_citations` / `id=a_metrics`
        # blocks alongside the primary article body container; they
        # duplicate the body's figures/references and are normally
        # tab-switched. Inline `style=height:0;overflow:hidden`
        # mostly clamps them, but each still leaves a ~15-px residual
        # reservation. Hide outright (display:none) to close the
        # ~80-px blank band between the in-body references and the
        # page footer. The parser keys on data-sf-nesting-track-id,
        # not these ids, so visibility-only hiding doesn't change
        # extraction.
        "div.articleCon#a_data,div.articleCon#a_references,"
        "div.articleCon#a_citations,div.articleCon#a_metrics"
        "{display:none!important;}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
    return html
def _parse_title(html):
    """Title from <title> tag.

    citation_title meta tag is empty in this CMS, so fall back to the
    document <title>. og:title is identical when present.
    """
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if m:
        return unescape(strip_tags(m.group(1))).strip()
    return ""


_JOURNAL_INFO_RE = re.compile(
    r"Cancer Research and Treatment[^<]*?(\d{4});\d+\(\d+\)"
)


def _parse_year(html):
    """Cover year for the issue, not the citation_publication_date online year.

    citation_publication_date in this CMS reports the online-publication date
    (e.g. 2025/4/24) but the article's formal cover year is the volume year
    (e.g. 2026 for Volume 58(2)). PubMed records use the cover year, so the
    parser must too. Pull the cover year out of the visible journal-info
    line ("...Cancer Association 2026;58(2):434-442"); fall back to the
    citation_publication_date 4-digit year only when the journal-info
    string is absent.
    """
    m = _JOURNAL_INFO_RE.search(html)
    if m:
        return m.group(1)
    date = get_meta(html, "citation_publication_date")
    if date:
        ym = re.search(r"(\d{4})", date)
        if ym:
            return ym.group(1)
    return ""


def _parse_metadata(html):
    """Extract metadata from citation_* meta tags + <title> fallback for title."""
    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage and lastpage != firstpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(
        html, "citation_journal_title"
    )
    journal = journal.rstrip(".") if journal else ""

    title = get_meta(html, "citation_title") or _parse_title(html)

    return {
        "title": title,
        "journal": journal,
        "year": _parse_year(html),
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

_WRITER_BLOCK_RE = re.compile(r"<dd class=writer\b[^>]*>(.*?)</dd>", re.DOTALL)
_AUTHOR_SUP_RE = re.compile(
    r">([^<>]{2,80})</a>(?:\s*<sup>([\d,\s]+)</sup>)?"
)
_AI_AFF_RE = re.compile(
    r"<p\s+class=ai_aff[^>]*>\s*<sup>\s*(\d+)\s*</sup>([^<]+)"
)


def _parse_visible_authors(html):
    """Parse authors and their numbered affiliations from the visible page.

    Returns a list of (Combined Name, [aff_str, ...]) preserving document
    order, or None when the writer block is missing. Source structures:

      <dd class=writer>
        ...
        <a ...>Junkyu Kim</a><sup>1</sup>
        <a ...>Min-Ji Kim</a><sup>2</sup>
        ...
      </dd>
      ...
      <p class=ai_aff><sup>1</sup>Division of Hematology-Oncology, ...</p>
      <p class=ai_aff><sup>2</sup>Biomedical Statistics Center, ...</p>

    The citation_author_institution meta tags drop multi-affiliation indices
    and leave the institution content empty for some authors (e.g. the
    corresponding author whose affiliation overlaps an earlier author),
    so prefer the visible numbered list.
    """
    block_m = _WRITER_BLOCK_RE.search(html)
    if not block_m:
        return None
    block = block_m.group(1)
    aff_map = {}
    for am in _AI_AFF_RE.finditer(html):
        aff_map[am.group(1).strip()] = unescape(am.group(2)).strip().rstrip(",")
    pairs = []
    for am in _AUTHOR_SUP_RE.finditer(block):
        name = unescape(am.group(1)).strip()
        # Skip non-author anchors that may slip in (ORCID URLs, PubMed
        # search links pointing back to the authors). Real names contain
        # at least one letter and no slashes/protocol prefixes.
        if "/" in name or "://" in name or not re.search(r"[A-Za-z]", name):
            continue
        sup = am.group(2) or ""
        idxs = [t.strip() for t in sup.split(",") if t.strip()]
        affs = [aff_map[i] for i in idxs if i in aff_map]
        pairs.append((name, affs))
    return pairs


def _parse_authors(html):
    """Extract authors with affiliations from the visible writer block.

    Combined names are routed through format_author_name (see SKILL.md
    § Author-name contract).
    """
    pairs = _parse_visible_authors(html)
    if pairs is None:
        # Fallback: meta tags. Loses multi-affiliation indices and may
        # leave the corresponding author's affiliation empty, but covers
        # any future fixture that omits the visible writer block.
        return _parse_meta_authors(html)
    return [
        {"author": format_author_name(name), "affiliation": list(affs)}
        for name, affs in pairs
    ]


def _parse_meta_authors(html):
    """Legacy fallback: paired citation_author + citation_author_institution meta tags."""
    authors = []
    current = None
    pattern = re.compile(
        r'<meta[^>]*name=["\']?citation_author(_institution)?["\']?'
        r'[^>]*content="([^"]*)"'
        r"|"
        r'<meta[^>]*content="([^"]*)"'
        r'[^>]*name=["\']?citation_author(_institution)?["\']?'
    )
    for m in pattern.finditer(html):
        is_inst = bool(m.group(1) or m.group(4))
        value = (m.group(2) or m.group(3) or "").strip()
        value = unescape(value).strip()
        if is_inst:
            if current is not None and value:
                current["affiliation"].append(value)
        else:
            if current is not None:
                authors.append(current)
            current = {
                "author": format_author_name(value),
                "affiliation": [],
            }
    if current is not None:
        authors.append(current)
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

# citation_reference content uses key=value pairs separated by ';'. Pairs may
# contain '=' inside the value (none observed) but never inside the key.
_KV_RE = re.compile(r"\s*([a-z_]+)\s*=\s*([^;]*?)\s*(?:;|$)")


def _parse_citation_reference(content):
    """Parse one citation_reference content string into a ref dict.

    Format observed:
      citation_title=...; citation_author=...; citation_author=...;
      citation_journal_title=...; citation_volume=...; citation_issue=...;
      citation_pages=...; citation_date=YYYY;

    citation_issue duplicates citation_volume in this publisher's CMS for
    references — it is dropped when the two are identical. citation_date
    is YYYY only here, so use it as the year.

    Authors come pre-formatted as "Initials Surname" (e.g. "GM Blumenthal"),
    so passing them through format_author_name routes them through the
    shape-2b path of parse_combined_name and emits canonical "Surname IN".
    """
    pairs = _KV_RE.findall(content)
    title = journal = volume = issue = pages = year = ""
    authors = []
    for key, val in pairs:
        val = unescape(val).strip()
        if not val:
            continue
        if key == "citation_title":
            title = val
        elif key == "citation_author":
            authors.append(format_author_name(val))
        elif key == "citation_journal_title":
            journal = val.rstrip(".")
        elif key == "citation_volume":
            volume = val
        elif key == "citation_issue":
            issue = val
        elif key == "citation_pages":
            pages = val
        elif key == "citation_date":
            ym = re.search(r"\d{4}", val)
            if ym:
                year = ym.group(0)
    # Drop spurious duplicate of volume into issue (publisher CMS quirk).
    if issue and issue == volume:
        issue = ""
    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": "",
        "authors": [a for a in authors if a],
    }


def _parse_references(html):
    """Extract the reference list from citation_reference meta tags.

    These tags carry the cleanest structured form of the bibliography
    (title, journal, volume, issue, pages, date, author list). The visible
    <ul class=reference><li> nodes contain the same data as a freeform
    citation string but require fuzzy splitting; the meta tags avoid that.
    """
    refs = []
    for content in get_all_meta(html, "citation_reference"):
        refs.append({"": _parse_citation_reference(content)})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

# Body container is `<div class=articleCon data-sf-nesting-track-id=1.2.8.4.1>`.
# After it, sibling `<div class=articleCon id=a_data ...>`,
# `<div class=articleCon id=a_references ...>`, etc. duplicate the content
# (Figure & Data, References, Citations, Metrics tabs). Slice ends at the
# first sibling articleCon with an `id=a_*` attribute.
_BODY_OPEN_RE = re.compile(
    r'<div[^>]*class=articleCon\b[^>]*data-sf-nesting-track-id=1\.2\.8\.4\.1\b[^>]*>'
)
_BODY_END_RE = re.compile(
    r'<div[^>]*class=articleCon[^>]*\bid=a_[a-z]+\b'
)


def _slice_body(html):
    """Return the inner HTML of the primary articleCon body container.

    The container is identified by its data-sf-nesting-track-id (stable
    across all e_crt papers — it tracks SingleFile's tree position). The
    end is taken at the next sibling articleCon with an id=a_* attribute,
    which marks the duplicated tab content (a_data, a_references, etc.).
    """
    m = _BODY_OPEN_RE.search(html)
    if not m:
        return ""
    start = m.end()
    end_m = _BODY_END_RE.search(html, start)
    end = end_m.start() if end_m else len(html)
    return html[start:end]


# Reference section heading id (the in-body REFERENCES list lives under
# the <h4 id=sec08> heading). Drop everything from this heading onward so
# references don't bleed into main_text.
_REF_HEADING_RE = re.compile(
    r'<h[2-4][^>]*\bid=sec\d+[^>]*>\s*REFERENCES?\s*</h[2-4]>',
    re.IGNORECASE,
)


def _drop_references_section(body_html):
    """Cut everything from the <h4 id=secNN>REFERENCES</h4> heading on.

    e_crt places the references inline within the main articleCon, after
    the Acknowledgments / Author Contributions / Conflicts of Interest
    blocks. The heading text is "REFERENCES" (uppercase). Cutting at the
    heading drops the inline <ul class=reference> too.
    """
    m = _REF_HEADING_RE.search(body_html)
    if not m:
        return body_html
    return body_html[:m.start()]


def _normalize_definition_lists(body_html):
    """Insert newlines before <dd> and <dt> so paragraphs don't merge.

    This CMS uses <dl><dt class=boldTit>...subsection title...<dd>...para 1...
    <dd>...para 2...</dl> with most <dd>/<dt> tags unclosed. The default
    tags_to_text strips bare <dd>/<dt> without inserting whitespace, so
    consecutive paragraphs collapse onto one line and section headings
    (<h4>) merge with the body text that follows. Convert <dt class=boldTit>
    to <h5> (so it renders as a paragraph break) and bare <dd>/<dt> to
    paragraph-breaking <p> openers.
    """
    body_html = re.sub(
        r'<dt[^>]*class=boldTit[^>]*>',
        '<p><b>',
        body_html,
    )
    body_html = re.sub(r'<dt[^>]*>', '<p>', body_html)
    body_html = re.sub(r'<dd[^>]*>', '<p>', body_html)
    body_html = re.sub(r'</d[tdl]>', '', body_html)
    body_html = re.sub(r'<dl[^>]*>', '', body_html)
    return body_html


def _parse_main_text(html):
    """Extract body text from the primary articleCon, excluding references."""
    body_html = _slice_body(html)
    if not body_html:
        return ""
    body_html = _drop_references_section(body_html)
    body_html = _normalize_definition_lists(body_html)
    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Cancer Research and Treatment HTML into a papers/*.json-format dict."""
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
