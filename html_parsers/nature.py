"""Nature (nature.com) HTML parser."""

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

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "Source data",
    "Full size image",
    "Full size table",
)

# Reference section titles (removed from main_text)
_REF_SECTIONS = {"references"}

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# Sections to skip (not part of main_text)
_PRE_BODY = {"inline recommendations"}


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    Targets Springer Nature cookie consent dialog and status message
    banners (e.g. "EMBO Press journals have moved...", "BMC journals
    have moved...").
    """
    # Cookie consent dialog: <dialog class=cc-banner ...>...</dialog>
    html = _remove_nested_element(
        html, r'<dialog[^>]*class=["\']?cc-banner[^>]*>'
    )
    # Status message banners
    html = _remove_nested_element(
        html, r'<div[^>]*class="[^"]*c-status-message--banner[^"]*"[^>]*>'
    )
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, volume, issue, year, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - volume, issue: str (may be empty)
      - year: 4-digit string
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    """
    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_online_date")
            or get_meta(html, "dc.date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev")
    journal = re.sub(r"  +", " ", journal.replace(".", "")).strip()

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

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name.
    Uses citation_author / citation_author_institution meta tags.
    """
    meta_authors = parse_meta_authors(html)
    return [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in meta_authors
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _flip_author_name(name):
    """Convert 'IN LastName' (e.g. 'JD Griffith') to 'LastName IN' via shared helpers."""
    return format_author_name(name)


def _parse_freeform_citation(text):
    """Parse a freeform citation string (no key=value pairs).

    Extracts year, DOI, and stores the full text as title for PubMed lookup.
    """
    text = re.sub(r'\s+', ' ', text).strip()

    # Extract DOI
    doi = ""
    doi_m = re.search(r'(https?://doi\.org/\S+)', text)
    if doi_m:
        doi = doi_m.group(1).rstrip('.')
    elif re.search(r'(10\.\d{4,}/\S+)', text):
        doi_m = re.search(r'(10\.\d{4,}/\S+)', text)
        doi = f"https://doi.org/{doi_m.group(1).rstrip('.')}"

    # Extract year from (YYYY) pattern
    year = ""
    year_m = re.search(r'\((\d{4})\)', text)
    if year_m:
        year = year_m.group(1)

    return {
        "title": text,
        "journal": "",
        "volume": "",
        "issue": "",
        "year": year,
        "pages": "",
        "doi": doi,
        "authors": [],
    }


def _parse_citation_reference(content):
    """Parse a single citation_reference meta tag content string.

    Format: 'citation_journal_title=X; citation_title=Y; ...'
    Falls back to freeform parsing for plain-text citations.
    Returns a dict with {journal, volume, issue, year, title, pages, doi, authors}.
    """
    fields = {}
    author_parts = []
    for part in content.split("; "):
        if "=" in part:
            key, val = part.split("=", 1)
            key = key.strip()
            val = val.strip()
            # Accumulate citation_author values (may appear multiple times)
            if key == "citation_author":
                author_parts.append(val)
            else:
                fields[key] = val

    # If no key=value pairs found, parse as freeform citation
    if not fields and not author_parts:
        return _parse_freeform_citation(content)

    authors = []
    # Authors may be in a single comma-separated field or multiple fields
    raw = ", ".join(author_parts)
    if raw:
        authors = [_flip_author_name(a.strip()) for a in raw.split(", ") if a.strip()]

    journal = fields.get("citation_journal_title", "")
    journal = journal.replace(".", "")
    # Collapse multiple spaces after dot removal
    journal = re.sub(r"  +", " ", journal).strip()

    return {
        "title": fields.get("citation_title", ""),
        "journal": journal,
        "volume": fields.get("citation_volume", ""),
        "issue": "",
        "year": fields.get("citation_publication_date", ""),
        "pages": fields.get("citation_pages", ""),
        "doi": format_doi(fields.get("citation_doi", "")),
        "authors": authors,
    }


def _parse_body_reference(item_html):
    """Parse a single <p class=c-article-references__text> body reference.

    Format: 'AuthorList (YEAR[letter]) Title. Journal Vol[(Issue)][:Pages]'
    Used as fallback when citation_reference meta tags are absent — Springer
    Nature paywall pages (e.g. Methods Mol Biol chapters) keep the visible
    reference list but strip the meta tags. Returns same dict shape as
    _parse_citation_reference.
    """
    doi = ""
    m = re.search(r'href=["\']?(https?://doi\.org/[^\s"\'<>]+)', item_html)
    if m:
        doi = format_doi(m.group(1))

    text = re.sub(r"<a[^>]*>.*?</a>", " ", item_html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"https?://doi\.org/\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")

    m = re.match(r"^(.+?)\s+\((\d{4})[a-z]?\)\s+(.+)$", text)
    if not m:
        return {
            "journal": "", "volume": "", "issue": "", "year": "",
            "title": text, "pages": "", "doi": doi, "authors": [],
        }
    authors_str, year, rest = m.group(1), m.group(2), m.group(3)
    authors = [a.strip() for a in authors_str.rstrip(",").split(",") if a.strip()]

    # Split title. Journal Vol[(Issue)][:Pages] — anchor at $ and use [^.]+?
    # for the journal so it can't absorb title text. Journal abbreviations
    # don't embed periods; parens (e.g. "Genes (Basel)") are fine.
    tail = re.search(
        r"\.\s+([^.]+?)\s+(\d+)(?:\((\d[\w\-]*)\))?(?::\s*(.+?))?$",
        rest,
    )
    if tail:
        title = rest[: tail.start()].rstrip(".").strip()
        journal = tail.group(1).strip().rstrip(".")
        volume = tail.group(2) or ""
        issue = tail.group(3) or ""
        pages = (tail.group(4) or "").replace("\u2013", "-").strip()
    else:
        title, journal, volume, issue, pages = rest.rstrip(".").strip(), "", "", "", ""

    return {
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "year": year,
        "title": title,
        "pages": pages,
        "doi": doi,
        "authors": authors,
    }


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {journal, volume, issue, year, title, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    Primary source: citation_reference meta tags. Falls back to
    <p class=c-article-references__text> body items when the visible list is
    longer than the meta list (older Springer articles can have incomplete
    meta) or when meta tags are absent (Springer Nature paywall pages).
    """
    meta_refs = [
        {"": _parse_citation_reference(unescape(m.group(1)))}
        for m in re.finditer(
            r'<meta[^>]*name=["\']?citation_reference["\']?'
            r'[^>]*content="([^"]*)"',
            html,
        )
    ]
    body_refs = [
        {"": _parse_body_reference(m.group(1))}
        for m in re.finditer(
            r'<p[^>]*class=["\']?c-article-references__text["\']?[^>]*>'
            r'(.*?)(?=<p\s|</li>)',
            html,
            re.DOTALL,
        )
    ]
    return body_refs if len(body_refs) > len(meta_refs) else meta_refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_keywords(html):
    """Extract article-specific keywords from Subjects list in body HTML.

    Uses c-article-subject-list (visible "Subjects" section) rather than
    JSON-LD keywords, which mix article keywords with journal categories.
    """
    keywords = []
    for m in re.finditer(
        r'<li[^>]*class=["\']?c-article-subject-list__subject["\']?[^>]*>'
        r'.*?<a[^>]*>([^<]+)</a>',
        html,
        re.DOTALL,
    ):
        kw = unescape(m.group(1)).strip()
        if kw:
            keywords.append(kw)
    return keywords


def _parse_abstract(html):
    """Extract abstract from <section data-title=Abstract>."""
    m = re.search(
        r'<section[^>]*data-title=["\']?Abstract["\']?[^>]*>(.*?)</section>',
        html,
        re.DOTALL,
    )
    if not m:
        return ""
    # Remove heading tags (e.g. <h2>Abstract</h2>) to avoid header leaking
    content = re.sub(r'<h[1-6][^>]*>.*?</h[1-6]>', '', m.group(1), flags=re.DOTALL)
    text = strip_tags(content).strip()
    # Safety net: strip leading "Abstract" if h-tag removal missed it
    if text.startswith("Abstract"):
        text = text[len("Abstract"):].strip()
    return text


def _extract_article(html):
    """Return the <article> element content, or full html as fallback."""
    m = re.search(r"<article[^>]*>(.*)</article>", html, re.DOTALL)
    return m.group(1) if m else html


def _section_boundaries(article):
    """Find all <section data-title=...> start positions and their titles.

    Returns list of (start_pos, end_of_opening_tag_pos, title) sorted by position.
    """
    entries = []
    for m in re.finditer(
        r'<section[^>]*data-title="([^"]*)"'
        r"|<section[^>]*data-title='([^']*)'"
        r"|<section[^>]*data-title=([^\s>\"']+)",
        article,
    ):
        title = m.group(1) or m.group(2) or m.group(3) or ""
        entries.append((m.start(), m.end(), unescape(title).strip()))
    return entries


def _find_start(article, sections):
    """Find main_text start: after Abstract and Inline Recommendations."""
    start = 0
    for i, (pos, tag_end, title) in enumerate(sections):
        if title.lower() in _PRE_BODY:
            # End of this section = start of next section
            next_pos = sections[i + 1][0] if i + 1 < len(sections) else len(article)
            if next_pos > start:
                start = next_pos
        else:
            break
    return start


def _remove_section(html, start_pattern):
    """Remove a <section> element matching start_pattern, handling nesting."""
    m = re.search(start_pattern, html)
    if not m:
        return html, False
    pos = m.end()
    depth = 1
    while depth > 0 and pos < len(html):
        next_open = re.search(r'<section[\s>]', html[pos:])
        next_close = re.search(r'</section>', html[pos:])
        if next_close is None:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos += next_open.end()
        else:
            depth -= 1
            pos += next_close.end()
    return html[:m.start()] + html[pos:], True


def _build_body(article, sections):
    """Build main_text HTML from two zones.

    Zone 1 (before first references): keep everything.
    Zone 2 (after first references): keep only supplementary sections.
    Remove all references sections.
    """
    # Find first references section position
    first_ref_idx = None
    for i, (pos, tag_end, title) in enumerate(sections):
        if title.lower() in _REF_SECTIONS:
            first_ref_idx = i
            break

    if first_ref_idx is None:
        # No references found — include all non-pre-body sections
        return None

    # Collect section ranges to include
    parts = []
    for i, (pos, tag_end, title) in enumerate(sections):
        tl = title.lower()
        # Skip pre-body sections
        if tl in _PRE_BODY:
            continue
        # Skip references sections
        if tl in _REF_SECTIONS:
            continue

        end = sections[i + 1][0] if i + 1 < len(sections) else len(article)

        if i < first_ref_idx:
            # Zone 1: keep everything
            parts.append((pos, end))
        else:
            # Zone 2: keep only supplementary sections
            if _SUPP_RE.search(title):
                parts.append((pos, end))

    return parts


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    Nature-specific: start is below Abstract and keywords/Inline Recommendations.
    """
    article = _extract_article(html)
    sections = _section_boundaries(article)

    if not sections:
        return ""

    parts = _build_body(article, sections)

    if parts is None:
        # No references found — use start/end fallback
        start = _find_start(article, sections)
        end = len(article)
        if start >= end:
            return ""
        parts = [(start, end)]
    elif not parts:
        # Fallback for articles without body sections (e.g. News & Views)
        m = re.search(r'<div[^>]*class=["\']?main-content[^>]*>', article)
        if not m:
            return ""
        start = m.end()
        # End at first references or end of article
        end = len(article)
        for pos, tag_end, title in sections:
            if title.lower() in _REF_SECTIONS and pos > start:
                end = pos
                break
        if start >= end:
            return ""
        parts = [(start, end)]

    # Extract abbreviation lists from pre-body sections (e.g. Inline Recommendations)
    abbr_html = ""
    for i, (pos, tag_end, title) in enumerate(sections):
        if title.lower() not in _PRE_BODY:
            break
        end = sections[i + 1][0] if i + 1 < len(sections) else len(article)
        pre_body = article[pos:end]
        for am in re.finditer(r'<dl[^>]*class=["\']?c-abbreviation[_-]list[^>]*>.*?</dl>',
                              pre_body, re.DOTALL):
            abbr_html += am.group(0)

    body_html = ""
    if abbr_html:
        body_html += "<h2>Abbreviations</h2><p></p>" + abbr_html
    for start, end in parts:
        body_html += article[start:end]

    # Remove any remaining references sections in the HTML
    while True:
        body_html, removed = _remove_section(
            body_html,
            r'<section[^>]*data-title=["\']?References["\']?[^>]*>'
        )
        if not removed:
            break

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Nature HTML into a refs.json-format dict plus main_text."""
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
