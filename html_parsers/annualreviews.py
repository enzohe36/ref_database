"""Annual Reviews (annualreviews.org) HTML parser."""

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
    parse_meta_authors,
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
    """Remove floating banners, cookie consent dialogs, and overlays.

    Strips the <div id=cookie-bar> fixed-position banner ("We use cookies
    to track usage and preferences.") and the <div id=hiddenContext>
    element whose data-cookie* attributes seed the same banner.
    """
    html = _remove_nested_element(
        html, r'<div[^>]*\bid=["\']?cookie-bar["\']?[^>]*>',
    )
    html = _remove_nested_element(
        html, r'<div[^>]*\bid=["\']?hiddenContext["\']?[^>]*>',
    )
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, volume, issue, year, pages, doi.

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
        "volume": get_meta(html, "citation_volume"),
        "issue": issue,
        "year": year,
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


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Uses citation_author + citation_author_institution meta tags. Annual
    Reviews stores names as "Given Last" (first-last), not "Last, Given",
    so convert via display-to-initials helper instead of format_author_name
    directly (which expects a comma).
    """
    return [
        {
            "author": _display_to_initials(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in parse_meta_authors(html)
    ]


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
            "volume": volume,
            "issue": "",
            "year": year,
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
    """Parse Annual Reviews HTML into a refs.json-format dict plus main_text."""
    meta = _parse_metadata(html)
    return {
        "stem": "",
        "journal": meta["journal"],
        "volume": meta["volume"],
        "issue": meta["issue"],
        "year": meta["year"],
        "title": meta["title"],
        "pages": meta["pages"],
        "doi": meta["doi"],
        "authors": _parse_authors(html),
        "publication_types": [],
        "references": _parse_references(html),
        "main_text": _parse_main_text(html),
    }
