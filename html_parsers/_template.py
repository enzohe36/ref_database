"""<Publisher> (<second-level-domain>) HTML parser.

Author-name contract (applies to _parse_authors and _parse_references):
  1. Extract (given, surname) pairs from HTML. Prefer structured sources
     (separate given-name/surname tags, JSON keys) over combined strings.
  2. Call format_name(given, surname) to emit "Surname IN".
  3. If the HTML only exposes a combined 'Given Last' / 'Last, Given' /
     'Initials Last' string, pass it to format_author_name (which routes
     through parse_combined_name + format_name).
  4. Never tokenize, split, flip, or build initials inline. Compound
     surname prefixes and hyphenated given-name handling live in
     _helpers; parsers do not duplicate that logic.
"""

import re
from html import unescape

from ._helpers import (
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

# Publisher-specific noise strings removed from main_text
_NOISE = ()

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

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    Return html unmodified if nothing needs removing. Ask the user to
    identify visually impairing elements before implementing; do not guess.
    """
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, volume, issue, year, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str without trailing period
      - journal: ISO abbreviation if the publisher exposes one (e.g.
        citation_journal_abbrev), else the full journal title verbatim,
        with dots stripped. Never hardcode an abbreviation or maintain
        a title->abbreviation map; each publisher emits one journal-name
        style consistently across its journals.
      - volume, issue: str (may be empty)
      - year: 4-digit string
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    """
    date = get_meta(html, "citation_publication_date") or get_meta(html, "citation_online_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    journal = journal.replace(".", "") if journal else ""

    title = get_meta(html, "citation_title")
    title = title.rstrip(".").rstrip() if title else ""

    return {
        "title": title,
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

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.

    Two patterns — pick the one that matches the HTML source. Both
    produce the canonical "LastName IN" output via shared helpers in
    _helpers. Do not tokenize, split, flip, or build initials inline.

    Pattern A — HTML exposes separate given/surname fields (preferred).
    Use this whenever the publisher emits <span class=given-name> /
    <span class=surname> tags, schema.org familyName/givenName, JSON
    blobs with explicit keys, or any other structured pair:

        for m in re.finditer(r'...given-name...>([^<]*).*surname...>([^<]*)<', html):
            given = unescape(m.group(1)).strip()
            surname = unescape(m.group(2)).strip()
            authors.append({
                "author": format_name(given, surname),
                "affiliation": [...],
            })

    Pattern B — HTML only exposes combined name strings. When the only
    source is citation_author / dc.contributor meta tags (forms like
    "Given Last", "Last, Given", or "JD Griffith"), delegate the split
    to parse_combined_name via format_author_name:

        return [
            {
                "author": format_author_name(a["name"]),
                "affiliation": a.get("affiliations", []),
            }
            for a in parse_meta_authors(html)
        ]

    Prefer Pattern A when the HTML supports it — it avoids the surname-
    boundary guesswork inherent in combined strings. format_name and
    parse_combined_name together handle hyphenated given names (Jean-
    Baptiste → JB), Unicode hyphens, dotted initials (J.B. → JB),
    already-compact initials (JA stays JA), compound surname prefixes
    (de Lange, d'Adda di Fagagna, Nick McElhinny), and trailing
    generational suffixes (Jr., III). Extending that coverage happens
    in _helpers, never in individual parsers.
    """
    return [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in parse_meta_authors(html)
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {journal, volume, issue, year, title, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper
    (notably title without trailing period, journal as ISO abbreviation
    when the source exposes one and full title otherwise — same
    dot-stripping rules enforced centrally in clean_parsed_output),
    with one exception: authors is a list of "LastName IN" strings
    (plain strings, not dicts with affiliation).
    Empty fields are "". Empty authors is [].

    Reference-author names almost always arrive as combined strings
    ("JD Griffith" in citation_reference meta, "Boulé J.-B." in body
    text). Pass them through format_author_name — never implement
    inline initial-building or surname detection here.
    """
    return []


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    """
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse <Publisher> HTML into a refs.json-format dict plus main_text."""
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
