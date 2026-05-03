"""Annual Reviews (annualreviews.org) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    affiliation_from_email,
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

_NOISE = (
    "Go to section...",
    "Open in a new tab",
    "[PubMed]",
    "[Medline]",
    "[Web of Science]",
    "[Google Scholar]",
    "[Citing articles]",
)

_REF_RE = re.compile(r'\breferences\b|literature\s+cited', re.IGNORECASE)

_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize Annual Reviews HTML to a single centered text column.

    Chrome stripped (Step 3):
      - Fixed-position cookie consent bar (`#cookie-bar`).
      - Site `<header>` and `<footer>`.
      - Breadcrumb nav inside `#main-content-container`.
      - "Most Read This Month" panel (`.mostreadcontainer`) that renders
        at the tail of `#main-content-container`.
      - Trendmd / recommendations / related-articles blocks that follow.

    Reading column wrapper: `<main id=main-content-container>`. Cap to
    752 px with 56 px top/bottom + 16 px side padding.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    html = remove_elements_by_id(html, "cookie-bar")
    for _ in range(5):
        before = html
        html = _remove_nested_element(html, r'<header\b[^>]*>')
        if html == before:
            break
    for _ in range(5):
        before = html
        html = _remove_nested_element(html, r'<footer\b[^>]*>')
        if html == before:
            break
    # Breadcrumb nav at the top of main-content-container.
    html = _remove_nested_element(html, r'<nav\b[^>]*aria-label=breadcrumbs\b[^>]*>')
    # Most Read + Most Cited + trailing recommendation panels.
    for cls in ("mostreadcontainer", "mostcitedcontainer",
                "recommendation-items", "sidebar-pub2web-element"):
        for _ in range(5):
            before = html
            html = _remove_nested_element(
                html, rf'<div\b[^>]*class="[^"]*\b{cls}\b[^"]*"[^>]*>',
            )
            if html == before:
                break
    # Trendmd widget(s) inside the article column.
    html = _remove_nested_element(
        html, r'<div\b[^>]*class="[^"]*\btrendmd-widget\b[^"]*"[^>]*>',
    )
    # Right-rail sidebar with "Reference Details" / cited-by panel.
    # On wide viewports it renders to the right of #main-content-container,
    # at narrow viewports it stacks below — both add document height past
    # the wrapper's bottom padding.
    html = remove_elements_by_id(html, "sidebar_right")
    html = _remove_nested_element(
        html, r'<aside\b[^>]*\bclass="[^"]*\bfooter-sidebar\b[^"]*"[^>]*>',
    )
    # Sticky article-tools navigation bar (Download / Cite / Share /
    # Tools dropdown) that pins to the top of the article column at
    # viewport widths >= 845 px.
    html = _remove_nested_element(
        html, r'<nav\b[^>]*\bclass="[^"]*\barticle-navigation-bar\b[^"]*"[^>]*>',
    )
    # Trailing empty `.bottom-side-nav` placeholder inside the wrapper
    # (h=0 but margin-bottom=30px). With visible content ending above
    # it, the margin shows up as 30 px of empty space between the
    # references and the wrapper's padding-bottom.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=bottom-side-nav\b[^>]*>',
        )
        if html == before:
            break
    # Trailing hidden chrome inside the wrapper that becomes the
    # ACTUAL last child of `#main-content-container`, hiding the
    # references list from the structural last-child chain selector
    # below. Strip them so the chain reaches the visible content.
    html = _remove_nested_element(
        html, r'<dialog\b[^>]*\bid=["\']?messageBox\b',
    )
    html = re.sub(
        r'<input\b[^>]*\bid=["\']?fancyBoxImgLoadErrMsg[^>]*>', '', html,
    )

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # The site layout wraps #main-content-container in a Bootstrap
        # row with sibling sidebars (col-md-3). Collapse the outer
        # container/row so main fills body width. Inner article wrappers
        # (#html_fulltext, #itemFullTextId, #html-body) get the same
        # flatten treatment so their natural sidebar-gutter padding
        # doesn't leak into the reading width.
        ".container,.container-fluid,"
        ".row,.row>[class*='col-'],"
        "#html_fulltext,#itemFullTextId,#html-body,"
        "#article-level-0-front-and-body,"
        "#article-level-0-back,"
        "#article-level-0-figs-and-tables,"
        "#article-level-0-end-metadata{"
        "display:block !important;width:100% !important;"
        "max-width:100% !important;min-width:0 !important;"
        "margin:0 !important;padding:0 !important;float:none !important;"
        "flex:1 1 auto !important;background:#fff !important}"
        # Capped reading-column wrapper.
        ":root #main-content-container{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # Clamp every descendant to fit inside the column. `min-width: 0`
        # was here historically to let flex children shrink, but on a
        # wildcard descendant selector it forces text-content blocks to
        # reflow narrower than the publisher's narrow CSS intended,
        # adding ~32 px to every reference list item. Drop it; if a
        # specific flex/grid child needs it later, scope it there.
        ":root #main-content-container *{"
        "max-width:100% !important;"
        "box-sizing:border-box !important}"
        # Tables: force fixed layout + break-all so wide cells don't push
        # past the wrapper.
        ":root #main-content-container table{"
        "width:100% !important;max-width:100% !important;"
        "table-layout:fixed !important}"
        ":root #main-content-container td,:root #main-content-container th{"
        "word-break:break-all !important;overflow-wrap:anywhere !important;"
        "white-space:normal !important}"
        # First-/last-child margin reset — scoped to direct children of
        # the wrapper only (via `>`). Blanket `*:first-child` was
        # collapsing reference list items' natural padding/margin and
        # killing section-heading top margin.
        ":root #main-content-container>*:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        # Direct-child only — descendant `*:last-child{margin-bottom:0}`
        # also kills the publisher's natural margin-bottom on inline
        # last-children like `h4.item-meta-data__journal-issue` (mb=11px,
        # spaces "Volume 42, 2008" from the H1 article title below).
        ":root #main-content-container>*:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # The wrapper's actual last child is a zero-height `.clearfix`
        # sibling-after the references; the publisher's natural
        # margin-bottom on the last `.articleSection` (mb:10),
        # `ol.articlereference-vancouver` (mb:11), and `li.refbody`
        # (pb:10) escape past it and inflate B by ~31 px. Zero those
        # specific trailing margins/padding via stable publisher
        # class names — surgical, not chain-based.
        ":root #main-content-container .articleSection:last-of-type,"
        ":root #main-content-container ol.articlereference-vancouver{"
        "margin-bottom:0 !important}"
        ":root #main-content-container ol.articlereference-vancouver "
        "> li.refbody:last-child{padding-bottom:0 !important}"
        # The .article-cover wrapper ships with margin-top:-25px (negative
        # pull for cover-art overlap); zero it so first text sits at
        # padding-top.
        ":root #main-content-container .article-cover{"
        "margin-top:0 !important}"
        # Force-expand the "View Affiliations and Author Notes"
        # accordion. The publisher's stylesheet sets `.js-plus{display:
        # inline}`, `.js-minus{display:none}`, and `#showHideAffiliation
        # Content{display:none}` — JS toggles them on click. Without JS
        # the affiliation block is invisible. Scoped to
        # `#showHideAffiliation` so the sibling "View Citation" widget
        # keeps its default collapsed state.
        ":root #main-content-container #showHideAffiliation .js-plus{"
        "display:none !important}"
        ":root #main-content-container #showHideAffiliation .js-minus,"
        ":root #main-content-container #showHideAffiliation .js-minus.minus{"
        "display:inline !important}"
        ":root #main-content-container #showHideAffiliationContent{"
        "display:block !important}"
        # Figure images: scale the full-res GIF (~1500-2300 px native,
        # inlined by `_annualreviews_inline_figures` post_capture) to
        # fill the figure container width (= caption width), preserving
        # aspect ratio, and center it.
        ":root #main-content-container .figure .image{"
        "text-align:center !important}"
        ":root #main-content-container .figure .image img,"
        ":root #main-content-container .figure .image a.media-link{"
        "display:inline-block !important;"
        "width:100% !important;height:auto !important;"
        "max-width:100% !important}"
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

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Uses standard citation_* meta tags.
    """
    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_date")
            or get_meta(html, "citation_online_date")
            or get_meta(html, "citation_year"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)
    if not year:
        year = get_meta(html, "citation_year")

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    journal = journal.rstrip(".") if journal else ""

    # Annual Reviews sets citation_issue to strings like "Volume 42, 2008"
    # rather than an issue number. Strip it unless it's a bare integer.
    issue = get_meta(html, "citation_issue")
    if issue and not re.fullmatch(r'\s*\d+\s*', issue):
        issue = ""

    return {
        "title": get_meta(html, "citation_title"),
        "journal": journal,
        "year": year,
        "volume": get_meta(html, "citation_volume"),
        "issue": issue,
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers.

    Compound-surname particles (de, van, etc.) are handled by
    parse_combined_name + format_name in _helpers.
    """
    return format_author_name(name)


_CURRENT_ADDRESS_RE = re.compile(r"^\W*\d*\s*current\s*address\s*:", re.IGNORECASE)


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Uses citation_author + citation_author_institution meta tags. Annual
    Reviews stores names as "Given Last" (first-last), not "Last, Given",
    so convert via display-to-initials helper instead of format_author_name
    directly (which expects a comma).

    Drops 'Current address:' note-style institutions that publishers
    sometimes emit in place of the primary affiliation — for these,
    falls back to email-domain inference so the primary lab is
    attributed instead of a post-publication relocation note.
    """
    authors = []
    for a in parse_meta_authors(html):
        affs = [
            af for af in a.get("affiliations", [])
            if not _CURRENT_ADDRESS_RE.match(af)
        ]
        authors.append({
            "author": _display_to_initials(a["name"]),
            "affiliation": affs,
        })
    # Email-domain inference for authors left with no aff after the
    # current-address filter. Pulls the correspondence email from the
    # HTML body (mailto: links) since Annual Reviews rarely sets
    # citation_author_email meta.
    if any(not a["affiliation"] for a in authors):
        body_email = ""
        em = re.search(r"mailto:([^\"'\s>]+)", html)
        if em:
            body_email = em.group(1)
        aff = affiliation_from_email(body_email)
        if aff:
            for a in authors:
                if not a["affiliation"]:
                    a["affiliation"] = [aff]
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Structure:
      <ol id=articlereference class=articlereference-vancouver>
        <li class=refbody id=ref-B1>
          <span class=reference-surname>...</span>
          <span class=reference-given-names>...</span>, ...
          <span class=reference-year>YYYY</span>.
          <span class=reference-article-title>Title</span>
          <span class=reference-source><span class=reference-italic>Journal</span></span>
          <span class=reference-volume>Vol</span>:<span class=reference-fpage>X</span>-<span class=reference-lpage>Y</span>
        </li>
    """
    m = re.search(r'<ol\s+id="?articlereference"?[^>]*>', html)
    if not m:
        return []
    # Scope to </ol>
    pos = m.end()
    depth = 1
    end = len(html)
    while depth > 0:
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
                end = pos + nc.start()
            pos += nc.end()
    refs_html = html[m.end():end]

    refs = []
    # ID variants seen: B1 (older), ref-B1 (newer), R1
    li_starts = list(re.finditer(
        r'<li\s+class="?refbody"?\s+id="?(?:ref-)?[A-Za-z]?\d+"?[^>]*>', refs_html
    ))
    for i, li_m in enumerate(li_starts):
        li_end = li_starts[i + 1].start() if i + 1 < len(li_starts) else len(refs_html)
        entry = refs_html[li_m.end():li_end]

        def _field(cls):
            fm = re.search(
                rf'<span\s+class="?reference-{cls}"?[^>]*>(.*?)</span>',
                entry, re.DOTALL,
            )
            return strip_tags(fm.group(1)).strip() if fm else ""

        title = _field("article-title")
        if not title:
            title = _field("chapter-title")
        year = _field("year")
        volume = _field("volume")
        fpage = _field("fpage").replace('\u2013', '').replace('\u2014', '').rstrip('-').strip()
        lpage = _field("lpage").replace('\u2013', '').replace('\u2014', '').lstrip('-').strip()
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # Source may contain a nested reference-italic span with journal text
        jm = re.search(
            r'<span\s+class="?reference-source"?[^>]*>(.*?)</span>\s*</span>',
            entry, re.DOTALL,
        )
        if not jm:
            jm = re.search(
                r'<span\s+class="?reference-source"?[^>]*>(.*?)</span>',
                entry, re.DOTALL,
            )
        journal = strip_tags(jm.group(1)).strip().rstrip('.') if jm else ""

        # Authors: pairs of reference-surname / reference-given-names
        authors = []
        for am in re.finditer(
            r'<span\s+class="?reference-surname"?[^>]*>([^<]*)</span>\s*'
            r'<span\s+class="?reference-given-names"?[^>]*>([^<]*)</span>',
            entry,
        ):
            surname = unescape(am.group(1)).strip()
            given = unescape(am.group(2)).strip().rstrip('.')
            authors.append(format_name(given, surname))

        # DOI
        doi = ""
        dm = re.search(r'href="?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1)))

        refs.append({"": {
            "title": title,
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": "",
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _extract_div(html, start_match):
    """Extract the div slice from its opening tag through its matching </div>."""
    pos = start_match.end()
    depth = 1
    end = len(html)
    while depth > 0:
        no = re.search(r'<div[\s>]', html[pos:])
        nc = re.search(r'</div>', html[pos:])
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
    return html[start_match.end():end]


def _parse_main_text(html):
    """Extract body text.

    Boundary rules:
      - Use <div id=html-body> as the main body container (abstract + sections).
      - References and site chrome are excluded by container scoping.
      - Supplementary materials: scan for sections matching supplement / etc.
    """
    # Prefer html-body when present (contains its own Abstract + all sections)
    body_m = re.search(r'<div\s+[^>]*id="?html-body"?[^>]*>', html)
    if body_m:
        body_html = _extract_div(html, body_m)
    else:
        # Fallback to abstract_content + article-level container
        abs_m = re.search(r'<div\s+id="?abstract_content"?[^>]*>', html)
        if not abs_m:
            return ""
        body_html = _extract_div(html, abs_m)

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Annual Reviews HTML into a papers/*.json-format dict."""
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
