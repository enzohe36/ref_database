"""bioRxiv (biorxiv.org) HTML parser.

bioRxiv landing pages carry only the abstract, author list, and metadata
in HTML; the full manuscript and references live in a separate PDF. This
parser extracts what is available and leaves references empty.
"""

import re

from ._helpers import (
    drop_noise,
    format_author_name,
    format_doi,
    get_meta,
    parse_meta_authors,
    strip_common,
    strip_tags,
    tags_to_text,
)

_NOISE = (
    "Open in a new tab",
    "Previous Section",
    "Next Section",
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    bioRxiv HTMLs in this corpus do not contain visually impairing overlays;
    returns html unchanged.
    """
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    bioRxiv preprints lack volume/issue; citation_firstpage carries the
    preprint ID (e.g. "2023.04.10.536247").
    """
    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_title") or get_meta(html, "citation_journal_abbrev")
    journal = journal.rstrip(".") if journal else ""

    return {
        "title": get_meta(html, "citation_title"),
        "journal": journal,
        "year": year,
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Uses citation_author + citation_author_institution meta tags. bioRxiv
    stores names as "Given Last" (first-last); convert to "LastName IN".
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
    """Return []. bioRxiv HTML does not include the reference list."""
    return []


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_main_text(html):
    """Extract the abstract text.

    bioRxiv places the abstract in <div class="section abstract" id=abstract-N>
    with an <h2> heading (typically "Abstract" or "SUMMARY") and paragraphs.
    """
    m = re.search(
        r'<div\s+class="section abstract"\s+id=abstract-\d+[^>]*>',
        html,
    )
    if not m:
        return ""

    pos = m.end()
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
    block = html[m.end():end]
    block = strip_common(block)
    text = tags_to_text(block)
    return drop_noise(text, _NOISE).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse a bioRxiv landing page into a papers/*.json-format dict."""
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
