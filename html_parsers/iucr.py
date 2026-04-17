"""IUCr (journals.iucr.org) HTML parser.

IUCr paper landing pages expose metadata, abstract, keywords, and
structured references via citation_* meta tags. The rendered body is
abstract-only and links out to the full text on a different URL.
"""

import re
from html import unescape

from ._helpers import (
    drop_noise,
    format_author_name,
    format_doi,
    get_all_meta,
    get_meta,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = ()


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Return html unmodified; no visually impairing elements per user."""
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _longest(values):
    """Return the longest string from values (descriptive variant)."""
    return max(values, key=len) if values else ""


def _parse_metadata(html):
    """Extract metadata from citation_* meta tags.

    IUCr emits multiple citation_journal_abbrev variants (progressively more
    specific); pick the longest for the fullest abbreviation.
    """
    date = get_meta(html, "citation_date") or get_meta(
        html, "citation_online_date"
    )
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = _longest(get_all_meta(html, "citation_journal_abbrev"))
    if not journal:
        journal = get_meta(html, "citation_journal_title")
    journal = journal.rstrip(".") if journal else ""

    return {
        "title": get_meta(html, "citation_title"),
        "journal": journal,
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "year": year,
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_authors(html):
    """Extract authors with affiliations.

    IUCr pairs each citation_author with a single citation_author_institution
    that follows it in document order (and optional citation_author_email).
    Names are in "Last, I.N." format; convert to "Last IN".
    """
    authors = []
    current = None
    # Longer alternation first so citation_author_institution / _email are
    # matched as themselves, not as "citation_author" + trailing junk.
    for m in re.finditer(
        r'<meta[^>]*name=["\']?(citation_author_institution|citation_author_email|citation_author)\b["\']?[^>]*content=("[^"]*"|\'[^\']*\'|[^\s>]+)',
        html,
    ):
        name_attr = m.group(1)
        raw = m.group(2)
        if raw.startswith('"') or raw.startswith("'"):
            value = raw[1:-1]
        else:
            value = raw
        value = unescape(value).strip()
        if name_attr == "citation_author":
            if current is not None:
                authors.append(current)
            current = {"name": value, "affiliations": []}
        elif name_attr == "citation_author_institution" and current is not None:
            current["affiliations"].append(value.strip(", "))
    if current is not None:
        authors.append(current)

    return [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a["affiliations"],
        }
        for a in authors
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _format_iucr_ref_author(name):
    """Convert IUCr 'Last F. M.' to 'Last FM' via shared helpers."""
    return format_author_name(name)


def _parse_reference_string(value):
    """Parse a single citation_reference string into a structured dict.

    Format: 'citation_author=X; citation_author=Y; citation_year=YYYY;
             citation_journal_title=ABBR; citation_volume=V;
             citation_firstpage=A; citation_lastpage=B;'
    """
    fields = [
        f.strip() for f in value.split(";") if f.strip()
    ]
    authors = []
    data = {}
    for f in fields:
        if "=" not in f:
            continue
        k, v = f.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k == "citation_author":
            authors.append(_format_iucr_ref_author(v))
        else:
            data[k] = v

    firstpage = data.get("citation_firstpage", "")
    lastpage = data.get("citation_lastpage", "")
    pages = f"{firstpage}-{lastpage}" if firstpage and lastpage else firstpage

    journal = data.get("citation_journal_title", "").rstrip(".")
    return {
        "title": data.get("citation_title", ""),
        "journal": journal,
        "volume": data.get("citation_volume", ""),
        "issue": data.get("citation_issue", ""),
        "year": data.get("citation_year", ""),
        "pages": pages,
        "doi": format_doi(data.get("citation_doi", "")),
        "authors": authors,
    }


def _parse_references(html):
    """Extract references from citation_reference meta tags."""
    refs = []
    for value in get_all_meta(html, "citation_reference"):
        refs.append({"": _parse_reference_string(value)})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Get abstract text, preferring meta tag over body markup."""
    text = get_meta(html, "citation_abstract")
    if text:
        return text.strip()
    m = re.search(
        r'<div[^>]*class="?ica_abstract"?[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    if m:
        return tags_to_text(m.group(1)).strip()
    return ""


def _parse_keywords(html):
    """Return keyword list from citation_keywords meta (semicolon-delimited)."""
    raw = get_meta(html, "citation_keywords")
    if not raw:
        return []
    return [kw.strip() for kw in raw.split(";") if kw.strip()]


def _parse_main_text(html):
    """Build main_text from abstract + keywords.

    IUCr landing pages lack body text; the full article lives at a
    separate URL (citation_fulltext_url) that is not included in the saved
    HTML.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append("## Abstract\n" + abstract)
    keywords = _parse_keywords(html)
    if keywords:
        parts.append("## Keywords\n" + ", ".join(keywords))
    return drop_noise("\n\n".join(parts), _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse IUCr HTML into a refs.json-format dict plus main_text."""
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
