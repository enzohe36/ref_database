"""Bentham Science (eurekaselect.com) HTML parser.

Eurekaselect pages are typically abstract-only landing pages for paywalled
articles. No references, no main body beyond abstract + keywords. Authors
and affiliations live in a Print modal rather than semantic markup.
"""

import re
from html import unescape

from ._helpers import (
    drop_noise,
    format_author_name,
    format_doi,
    get_all_meta,
    get_meta,
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

    - fixed-header: floating nav bar (menu hamburger + main navigation).
    """
    return remove_elements_by_id(html, "fixed-header")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata from citation_* meta tags."""
    date = get_meta(html, "citation_publish_on")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    return {
        "title": get_meta(html, "citation_title"),
        "journal": get_meta(html, "citation_journal_title"),
        "year": year,
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_affiliations_from_modal(html):
    """Extract affiliation strings from the Print modal.

    Layout: <p><strong>Affiliation: </strong><ul><li>AFF1</li><li>AFF2</li></ul>
    """
    m = re.search(
        r"<strong>\s*Affiliation:?\s*</strong>(.*?)(?:<p>|</div>)",
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    affs = []
    # <li> tags here may be unclosed; terminate at next <li>, </ul>, or </li>.
    for li in re.finditer(
        r"<li[^>]*>(.*?)(?=<li|</ul>|</li>)",
        m.group(1), re.DOTALL,
    ):
        text = re.sub(
            r"\s+", " ", unescape(strip_tags(li.group(1))).strip()
        ).rstrip(",. ")
        if text:
            affs.append(text)
    return affs


def _parse_authors(html):
    """Extract authors from citation_author meta tags.

    Affiliations are sourced from the Print modal since the HTML does not
    provide per-author affiliation mapping. All authors share the same
    affiliation list.
    """
    names = get_all_meta(html, "citation_author")
    affiliations = _parse_affiliations_from_modal(html)
    authors = []
    for n in names:
        cleaned = re.sub(r"\s+", " ", n).strip().rstrip("*")
        if not cleaned:
            continue
        authors.append({
            "author": format_author_name(cleaned),
            "affiliation": list(affiliations),
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Eurekaselect abstract pages do not expose references."""
    return []


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract text from <div id=abstract>."""
    m = re.search(
        r'<div[^>]*id="?abstract"?[^>]*>(.*?)</div>\s*</div>',
        html, re.DOTALL,
    )
    if not m:
        return ""
    inner = m.group(1)
    tm = re.search(
        r'<div[^>]*class="?text-justify"?[^>]*>(.*?)</div>',
        inner, re.DOTALL,
    )
    if not tm:
        return ""
    content = strip_common(tm.group(1))
    text = tags_to_text(content)
    return text.strip()


def _parse_keywords(html):
    """Extract keywords from the abstract card."""
    m = re.search(
        r"<strong>\s*Keywords:?\s*</strong>(.*?)</p>", html, re.DOTALL,
    )
    if not m:
        return []
    inner = m.group(1)
    kws = []
    for am in re.finditer(r"<a[^>]*>(.*?)</a>", inner, re.DOTALL):
        text = unescape(strip_tags(am.group(1))).strip().rstrip(",.")
        if text:
            kws.append(text)
    return kws


def _parse_main_text(html):
    """Build main_text from abstract + keywords.

    Eurekaselect landing pages do not include body sections or references,
    so main_text consists of only the abstract and keyword list.
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
    """Parse Eurekaselect HTML into a papers/*.json-format dict."""
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
