"""Royal Society of Chemistry (rsc.org) HTML parser."""

import re
from html import unescape

from ._helpers import (
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    parse_meta_authors,
    remove_elements_by_id,
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

    - onetrust-consent-sdk: OneTrust cookie banner ("This site uses
      cookies").
    - rsc-onetrust-cookie-footer: RSC-specific persistent cookie footer
      ("This website collects cookies to deliver a better user
      experience").
    """
    return remove_elements_by_id(
        html, "onetrust-consent-sdk", "rsc-onetrust-cookie-footer"
    )


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
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
    if journal:
        journal = re.sub(r"  +", " ", journal.replace(".", "")).strip()
    else:
        journal = ""

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

def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    RSC citation_author meta tags use 'Given Last' form; format_author_name
    handles the flip via parse_combined_name + format_name.
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

def _flip_initials_first(name):
    """Convert 'F. M. LastName' to 'LastName FM' via shared helpers."""
    return format_author_name(name)


def _parse_citation_reference(content):
    """Parse a single citation_reference meta tag content string.

    RSC format: 'citation_title=...; citation_author=A; citation_author=B;
    citation_journal_title=X; citation_volume=Y; citation_pages=FP-LP;
    citation_publication_date=YYYY;'

    Field separators are ';' optionally followed by whitespace/newlines.
    Returns dict {title, journal, year, volume, issue, pages, doi, authors}.
    """
    fields = {}
    author_parts = []
    for part in re.split(r";\s*", content):
        part = part.strip()
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "citation_author":
            author_parts.append(val)
        else:
            fields[key] = val

    if not fields and not author_parts:
        # Freeform fallback: store full text as title
        return {
            "title": content.strip(),
            "journal": "",
            "year": "",
            "volume": "",
            "issue": "",
            "pages": "",
            "doi": "",
            "authors": [],
        }

    authors = [_flip_initials_first(a) for a in author_parts if a]

    pages = fields.get("citation_pages", "")
    if not pages:
        fp = fields.get("citation_first_page", "")
        lp = fields.get("citation_last_page", "")
        pages = f"{fp}-{lp}" if lp else fp
    pages = pages.replace("\u2013", "-").replace("\u2014", "-")

    journal = fields.get("citation_journal_title", "")
    journal = re.sub(r"\s+", " ", journal).strip().rstrip(".")

    year = fields.get("citation_publication_date", "")
    if year:
        m = re.search(r"(\d{4})", year)
        year = m.group(1) if m else year

    return {
        "title": fields.get("citation_title", "").strip(),
        "journal": journal,
        "year": year,
        "volume": fields.get("citation_volume", ""),
        "issue": fields.get("citation_issue", ""),
        "pages": pages,
        "doi": format_doi(fields.get("citation_doi", "")),
        "authors": authors,
    }


def _parse_references(html):
    """Extract the reference list from citation_reference meta tags."""
    refs = []
    for m in re.finditer(
        r'<meta[^>]*name=["\']?citation_reference["\']?'
        r'[^>]*content="([^"]*)"'
        r'|<meta[^>]*content="([^"]*)"'
        r'[^>]*name=["\']?citation_reference["\']?',
        html,
    ):
        content = unescape(m.group(1) or m.group(2) or "")
        ref = _parse_citation_reference(content)
        refs.append({"": ref})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract from <div class=capsule__text>.

    The capsule__text div sits inside capsule__column-wrapper and contains
    the article abstract as one or more <p> elements.
    """
    m = re.search(
        r'<div[^>]*class=["\']?capsule__text[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    if not m:
        return ""
    return strip_tags(m.group(1)).strip()


def _parse_main_text(html):
    """Extract body text.

    RSC HTML landing pages contain only the abstract; full body text is
    paywalled. The convert_html.py pipeline will fall back to PMC when
    main_text is short. We still emit the abstract so that papers without
    PMC fallbacks have at least the summary content available.
    """
    abstract = _parse_abstract(html)
    if not abstract:
        return ""
    return f"## Abstract\n\n{abstract}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse RSC HTML into a papers/*.json-format dict."""
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
