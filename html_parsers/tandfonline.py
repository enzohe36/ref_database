"""Taylor & Francis (tandfonline.com) HTML parser."""

# Note: tandfonline HTML uses unquoted attributes (class=foo not class="foo").
# All regex patterns must handle both quoted and unquoted attribute values.

import re
import urllib.parse
from html import unescape

from ._helpers import (
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new window",
    "Display full size",
    "Download PDF",
    "Download MS Word",
    "Download Zip",
    "Download figure",
    "PubMed",
    "Web of Science",
    "Google Scholar",
)

# All NLM_sec div opening tags (any attribute order)
_ALL_SECTION_RE = re.compile(
    r'<div\s[^>]*class="?NLM_sec[^">\s]*[^>]*>', re.DOTALL
)

# Supplementary section patterns (kept after first references)
_SUPPLEMENTARY_RE = re.compile(
    r"supplement|extended data|source data|expanded view|appendix",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    Targets tandfonline floating bottom bar (Article contents / Related research).
    """
    return remove_elements_by_selector(html, "sectionsNavigation")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _get_meta(html, name):
    """Get content of a <meta> tag by name, handling unquoted attributes.

    Tandfonline-specific: the shared _helpers.get_meta does not handle the
    mixed quoted/unquoted attribute style used by tandfonline. Handles both:
      <meta name="dc.Title" content="...">
      <meta name=dc.Title content="...">
    and content can also be unquoted.
    """
    esc = re.escape(name)
    patterns = [
        # name then content, quoted content
        rf'<meta[^>]*name="?\'?{esc}"?\'?[^>]*content="([^"]*)"',
        rf"<meta[^>]*name=\"?'?{esc}\"?'?[^>]*content='([^']*)'",
        # name then content, unquoted content
        rf'<meta[^>]*name="?\'?{esc}"?\'?[^>]*content=([^\s>]+)',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return unescape(m.group(1).strip())
    return ""


def _parse_volume_issue(html):
    """Extract volume and issue from issue-heading span.

    Format: 'Volume 46, 2026 - \\n Issue 4'
    Handles both class="issue-heading" and class=issue-heading.
    """
    m = re.search(
        r'class="?issue-heading"?[^>]*>(.*?)</span>', html, re.DOTALL
    )
    if not m:
        return "", ""
    text = strip_tags(m.group(1)).strip()
    vol_m = re.search(r"Volume\s+(\d+)", text)
    iss_m = re.search(r"Issue\s+(\S+)", text)
    volume = vol_m.group(1) if vol_m else ""
    issue = iss_m.group(1) if iss_m else ""
    return volume, issue


def _parse_pages(html):
    """Extract page range from 'Pages X-Y' text in itemPageRangeHistory div."""
    m = re.search(r'class="?itemPageRangeHistory"?[^>]*>.*?Pages\s+([\d][^\s<]+)', html, re.DOTALL)
    if not m:
        return ""
    return m.group(1).replace("\u2013", "-").replace("\u2014", "-")


def _parse_title(html):
    """Extract title, combining dc.Title and dc.Title.Subtitle if present."""
    title = _get_meta(html, "dc.Title")
    subtitle = _get_meta(html, "dc.Title.Subtitle")
    if subtitle:
        title = f"{title}: {subtitle}"
    return title


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, volume, issue, year, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - volume, issue: str (may be empty)
      - year: 4-digit string
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    Tandfonline-specific: uses dc.Date for year, issue-heading span for volume
    and issue, itemPageRangeHistory for pages, and dc.Identifier[scheme=doi]
    (falling back to publication_doi) for DOI.
    """
    date = _get_meta(html, "dc.Date")
    year = ""
    if date:
        ym = re.search(r"(\d{4})", date)
        if ym:
            year = ym.group(1)

    volume, issue = _parse_volume_issue(html)
    pages = _parse_pages(html)

    # DOI from dc.Identifier with scheme=doi
    doi = ""
    doi_m = re.search(
        r'<meta[^>]*name="?dc\.Identifier"?[^>]*scheme="?doi"?[^>]*content="?([^\s">]+)',
        html,
    )
    if doi_m:
        doi = doi_m.group(1)
    if not doi:
        doi = _get_meta(html, "publication_doi")

    return {
        "title": _parse_title(html),
        "journal": _get_meta(html, "citation_journal_title"),
        "volume": volume,
        "issue": issue,
        "year": year,
        "pages": pages,
        "doi": format_doi(doi),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name.
    Tandfonline-specific: parses contribDegrees spans. Each span contains
    <a class=author> with LastName%2C+Given in its href, and an
    <span class=overlay> with affiliation text.
    """
    authors = []
    seen = set()

    # Find each contribDegrees span by walking from each opening tag
    # Class may have additional values: "contribDegrees corresponding MTN"
    for m in re.finditer(r'<span\s+class="?contribDegrees[^>]*>', html):
        # Walk forward to find matching </span>, handling nesting
        pos = m.end()
        depth = 1
        while depth > 0 and pos < len(html):
            next_open = re.search(r'<span[\s>]', html[pos:])
            next_close = re.search(r'</span>', html[pos:])
            if next_close is None:
                break
            if next_open and next_open.start() < next_close.start():
                depth += 1
                pos += next_open.end()
            else:
                depth -= 1
                pos += next_close.end()

        block = html[m.end():pos]

        # Extract name from author link
        name_m = re.search(r'class="?author"?[^>]*>(.*?)</a>', block, re.DOTALL)
        if not name_m:
            continue

        display_name = strip_tags(name_m.group(1)).strip()
        if display_name in seen:
            continue
        seen.add(display_name)

        # Try to get LastName, Given from href URL
        href_m = re.search(r'class="?author"?[^>]*href="?([^\s">]+)', block)
        author = display_name
        if href_m:
            href = href_m.group(1)
            # URL like /author/Clatterbuck+Soper%2C+Sarah+F
            parts = href.split("/author/")
            if len(parts) > 1:
                decoded = urllib.parse.unquote_plus(parts[1])
                author = format_author_name(decoded)

        # Extract affiliation from overlay span
        affiliations = []
        overlay_m = re.search(
            r'<span\s+class="?overlay"?>(.*?)</span>', block, re.DOTALL
        )
        if overlay_m:
            aff_html = overlay_m.group(1)
            # Remove orcid links and their images
            aff_html = re.sub(r'<a[^>]*class="?orcid-author"?[^>]*>.*?</a>', '', aff_html, flags=re.DOTALL)
            aff_text = strip_tags(aff_html).strip()
            # Remove "Correspondence" + email at end of affiliation
            aff_text = re.sub(r'Correspondence\S*$', '', aff_text).strip()
            if aff_text:
                # Affiliations are prefixed with superscript labels (a, b, c...)
                # Split on semicolons which separate multiple affiliations
                parts = re.split(r'\s*;\s*', aff_text)
                affiliations = [p.strip() for p in parts if p.strip()]

        authors.append({
            "author": author,
            "affiliation": affiliations,
        })

    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _flip_ref_author(name):
    """Convert 'Initials LastName' (Scholar URL shape) to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {journal, volume, issue, year, title, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    Uses Google Scholar lookup URLs for structured fields, with DOIs from
    getFTR data-target attributes (not from scholar URL tracking params).
    Falls back to plain text from <li> entries.
    """
    # Find references section (unquoted id)
    refs_m = re.search(
        r'<div\s+id="?references-Section1"?[^>]*>(.*)',
        html, re.DOTALL,
    )
    if not refs_m:
        return []

    refs_html = refs_m.group(1)
    # Truncate at next major section to avoid matching outside references
    end_m = re.search(r'</div>\s*</div>\s*</article>', refs_html, re.DOTALL)
    if end_m:
        refs_html = refs_html[:end_m.start()]

    refs = []

    # Find all reference <li> start positions (no </li> tags in tandfonline HTML)
    # ID formats: CIT0001, cit0001, B1, R1
    li_starts = list(re.finditer(r'<li\s+id="?(?:CIT|cit|B|R)\d+[^>]*>', refs_html, re.DOTALL))
    for i, li_m in enumerate(li_starts):
        end = li_starts[i + 1].start() if i + 1 < len(li_starts) else min(li_m.start() + 5000, len(refs_html))
        entry = refs_html[li_m.end():end]

        # Get DOI from getFTR data-target (the reliable source)
        doi_m = re.search(r'data-target="?(10\.[^\s">]+)', entry)
        ref_doi = format_doi(doi_m.group(1)) if doi_m else ""

        # Try Google Scholar lookup URL (double-encoded in getFTRLinkout)
        gs_m = re.search(r'scholar_lookup%3F([^"\'>\s]+)', entry)
        if not gs_m:
            # Direct scholar_lookup URL
            gs_m = re.search(
                r'scholar\.google\.com/scholar_lookup\?([^"\'>\s]+)', entry
            )

        if gs_m:
            qs = gs_m.group(1)
            # Decode URL encoding
            qs = urllib.parse.unquote(qs)
            # Strip tracking params appended by tandfonline after &amp;
            # Scholar params use & between them; tracking params start with &amp;doi=
            qs = re.split(r'&amp;', qs)[0]
            qs = unescape(qs).replace("&amp;", "&")
            params = urllib.parse.parse_qs(qs)

            # Scholar URL doesn't include issue; parse it from citation text
            # Format: "YYYY;VOLUME(ISSUE):PAGES"
            issue = ""
            text = strip_tags(entry)
            iss_m = re.search(r'\d{4};\d+\(([^)]+)\)\s*:', text)
            if iss_m:
                issue = iss_m.group(1)

            ref = {
                "title": params.get("title", [""])[0],
                "journal": params.get("journal", [""])[0],
                "volume": params.get("volume", [""])[0],
                "issue": issue,
                "year": params.get("publication_year", [""])[0],
                "pages": params.get("pages", [""])[0].replace("\u2013", "-"),
                "doi": ref_doi,
                "authors": [
                    _flip_ref_author(a) for a in params.get("author", []) if a.strip()
                ],
            }
        else:
            # Fallback: extract text from the <span> content
            span_m = re.search(r'<span>(.*?)</span>', entry, re.DOTALL)
            cite_text = strip_tags(span_m.group(1)).strip() if span_m else ""
            ref = {
                "title": cite_text,
                "journal": "",
                "volume": "",
                "issue": "",
                "year": "",
                "pages": "",
                "doi": ref_doi,
                "authors": [],
            }

        refs.append({"": ref})

    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _is_top_level_section(tag_html):
    """Check if a matched NLM_sec tag is top-level (not level_2+)."""
    return not re.search(r'NLM_sec_level_[2-9]', tag_html)


def _get_section_heading(section_html):
    """Get the first heading text from a section div."""
    m = re.search(r'<h[1-4][^>]*>(.*?)</h[1-4]>', section_html, re.DOTALL)
    if m:
        return strip_tags(m.group(1)).strip()
    return ""


def _extract_section(html, start_match):
    """Extract a section div content from its opening match to matching close."""
    pos = start_match.end()
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
            pos += next_close.end()
    return html[start_match.start():pos]


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    Tandfonline-specific: include abstract (hlFld-Abstract), keywords
    (abstractKeywords), and top-level NLM_sec body sections before references;
    include top-level NLM_sec supplementary sections after references.
    """
    # Find article element
    article_m = re.search(r'<article[^>]*>(.*)</article>', html, re.DOTALL)
    if not article_m:
        return ""
    article = article_m.group(1)

    parts = []

    # Abstract (nesting-aware extraction)
    abs_m = re.search(r'<div\s[^>]*class="?hlFld-Abstract"?[^>]*>', article)
    if abs_m:
        abs_html = _extract_section(article, abs_m)
        abs_html = strip_common(abs_html)
        parts.append(tags_to_text(abs_html))

    # Keywords
    kw_m = re.search(r'<div\s[^>]*class="?abstractKeywords"?[^>]*>', article)
    if kw_m:
        kw_html = _extract_section(article, kw_m)
        kw_text = strip_tags(kw_html).strip()
        if kw_text:
            parts.append(kw_text)

    # Find references section position to determine body boundary
    refs_pos = len(article)
    refs_m = re.search(r'<div\s+id="?references-Section1"?', article)
    if refs_m:
        refs_pos = refs_m.start()

    # Body sections (top-level NLM_sec divs before references)
    for sec_m in _ALL_SECTION_RE.finditer(article):
        if sec_m.start() >= refs_pos:
            break
        if not _is_top_level_section(sec_m.group()):
            continue

        section_html = _extract_section(article, sec_m)
        section_html = extract_captions(section_html)
        section_html = strip_common(section_html)
        text = tags_to_text(section_html)
        if text.strip():
            parts.append(text)

    # Look for supplementary content sections after references
    if refs_m:
        post_refs = article[refs_pos:]
        for sec_m in _ALL_SECTION_RE.finditer(post_refs):
            if not _is_top_level_section(sec_m.group()):
                continue
            section_html = _extract_section(post_refs, sec_m)
            heading = _get_section_heading(section_html)
            if _SUPPLEMENTARY_RE.search(heading):
                section_html = extract_captions(section_html)
                section_html = strip_common(section_html)
                text = tags_to_text(section_html)
                if text.strip():
                    parts.append(text)

    result = "\n\n".join(parts)
    return drop_noise(result, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse tandfonline HTML into a refs.json-format dict plus main_text."""
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
