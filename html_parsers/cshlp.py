"""Cold Spring Harbor Laboratory Press (cshlp.org) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    parse_meta_authors,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "Previous Section",
    "Next Section",
    "View this table:",
    "View inline",
    "View popup",
    "In this window",
    "In a new window",
    "In this page",
    "Download as PowerPoint",
    "View larger version:",
)

# h2 headings that are reference sections (removed from main_text)
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|appendix',
    re.IGNORECASE,
)

# Site chrome (end boundary)
_CHROME_RE = re.compile(
    r'^articles citing this article|^citing articles via|^we recommend',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    Targets CSHLP cookie consent banner.
    """
    html = _remove_nested_element(
        html, r'<div[^>]*id=["\']?cookie-law[^>]*>'
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
    title = get_meta(html, "citation_title")
    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    volume = get_meta(html, "citation_volume")
    issue = get_meta(html, "citation_issue")
    doi = format_doi(get_meta(html, "citation_doi"))

    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_date")
            or get_meta(html, "DC.Date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    return {
        "title": title,
        "journal": journal.rstrip(".") if journal else "",
        "volume": volume,
        "issue": issue,
        "year": year,
        "pages": pages,
        "doi": doi,
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_body_affiliations(html, authors):
    """Extract affiliations from the HTML body contributor/affiliation list.

    Maps author xref-aff links to affiliation entries.
    """
    # Build aff-id -> text map from <ol class="affiliation-list">
    aff_map = {}
    for am in re.finditer(
        r'<li class=aff><a id=(aff-\d+)[^>]*></a>\s*<address>(.*?)</address>',
        html, re.DOTALL,
    ):
        aff_id = am.group(1)
        text = strip_tags(am.group(2)).strip()
        # Remove leading superscript label (e.g. "1 " or "1")
        text = re.sub(r'^\d+\s*', '', text).strip().rstrip(';')
        if text:
            aff_map[aff_id] = text

    if not aff_map:
        return authors

    # Map each author to their affiliations via xref-aff links
    contribs = list(re.finditer(r'<li[^>]*id=contrib-\d+[^>]*>(.*?)</li>', html, re.DOTALL))
    for i, contrib in enumerate(contribs):
        if i >= len(authors):
            break
        entry = contrib.group(1)
        aff_ids = re.findall(r'class=xref-aff href=#(aff-\d+)', entry)
        affs = [aff_map[aid] for aid in aff_ids if aid in aff_map]
        # If no xref-aff links but only one affiliation, assign it
        if not affs and len(aff_map) == 1:
            affs = list(aff_map.values())
        authors[i]["affiliation"] = affs

    return authors


def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers.

    CSHLP Silverchair meta tags store names as 'Given Middle Last';
    format_author_name handles the flip and compound-surname particles
    via parse_combined_name + format_name in _helpers.
    """
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name; names
    without a comma (CSHLP meta tags often emit "Given Middle Last") are
    flipped first by _display_to_initials so the surname lands first.
    Uses citation_author meta tags, with body fallback for affiliations
    when citation_author_institution tags are absent.
    """
    meta_authors = parse_meta_authors(html)
    authors = [
        {
            "author": _display_to_initials(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in meta_authors
    ]
    # If any author has affiliations, meta tags worked — return as-is
    if any(a["affiliation"] for a in authors):
        return authors
    # Fallback: parse affiliations from HTML body
    return _parse_body_affiliations(html, authors)


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {journal, volume, issue, year, title, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    Handles two CSHLP HTML formats:
      - New format: all authors in cit-auth spans, cit-article-title,
        cit-lpage, cit-jnl-abbrev (abbr tag).
      - Old format: only first author in cit-auth span, remaining authors
        as inline text, title as inline text between year and journal,
        no cit-lpage, journal in cit-source span with trailing dot.
    """
    refs = []
    m = re.search(r'class="?cit-list\b', html)
    if not m:
        return refs

    # Find all reference entries: <div class="cit ref-cit ..."> or <li> with cit class
    ref_html = html[m.start():]
    ref_starts = [rm.start() for rm in re.finditer(r'<div class="?cit ref-cit\b', ref_html)]
    if not ref_starts:
        # Fallback: <li> entries
        ol_end = ref_html.find('</ol>')
        if ol_end < 0:
            ol_end = len(ref_html)
        ref_starts = [lm.start() for lm in re.finditer(r'<li[^>]*>', ref_html[:ol_end])]

    for i, start in enumerate(ref_starts):
        end = ref_starts[i + 1] if i + 1 < len(ref_starts) else start + 5000
        entry = ref_html[start:end]

        # --- Authors (structured spans) ---
        authors = []
        for am in re.finditer(
            r'<span class=cit-name-surname>([^<]*)</span>\s*'
            r'<span class=cit-name-given-names>([^<]*)</span>',
            entry,
        ):
            surname = unescape(am.group(1)).strip().rstrip(",")
            given = unescape(am.group(2)).strip().rstrip(".")
            initials = given.replace(".", "").replace(" ", "")
            authors.append(f"{surname} {initials}" if initials else surname)

        # --- Inline authors (old format: only first author in spans) ---
        num_auth_spans = len(re.findall(r'<span class=cit-auth>', entry))
        if num_auth_spans <= 1:
            inline_m = re.search(
                r'</span></span>(.*?)<span class=cit-pub-date',
                entry, re.DOTALL,
            )
            if inline_m:
                inline_text = inline_m.group(1)
                for im in re.finditer(
                    r'(?:^|[.,]\s*)(?:and\s+)?'
                    r'((?:[a-z]+\s+)*[A-Z][A-Za-z\'-]+)\s*,\s*'
                    r'([A-Z]\.?(?:[A-Z]\.?)*)',
                    inline_text,
                ):
                    surname = unescape(im.group(1)).strip()
                    initials = im.group(2).replace(".", "")
                    authors.append(f"{surname} {initials}" if initials else surname)

        # Helper for unquoted or quoted class matching
        def _cit_field(cls):
            fm = re.search(rf'class="?{cls}"?[^>]*>([^<]*)', entry)
            return unescape(fm.group(1)).strip() if fm else ""

        # --- Title ---
        # Use full-content extraction for cit-article-title (may contain
        # inline HTML like <em>)
        title = ""
        title_span = re.search(
            r'class="?cit-article-title"?[^>]*>(.*?)</span>',
            entry, re.DOTALL,
        )
        if title_span:
            title = strip_tags(title_span.group(1)).strip()
            title = re.sub(r'\s+', ' ', title)

        # Fallback for old format: extract between year and journal span
        if not title:
            title_m = re.search(
                r'cit-pub-date[^>]*>[^<]*</span>\.\s*(.*?)\.?\s*'
                r'<(?:span class=cit-source|abbr class=cit-jnl-abbrev)',
                entry, re.DOTALL,
            )
            if title_m:
                title = strip_tags(title_m.group(1)).strip()
                title = re.sub(r'\s+', ' ', title)

        # --- Journal ---
        journal = _cit_field("cit-jnl-abbrev") or _cit_field("cit-source")
        journal = journal.rstrip(".")

        # --- Year, volume, pages ---
        year = _cit_field("cit-pub-date").rstrip(".")
        volume = _cit_field("cit-vol")
        fpage = _cit_field("cit-fpage")
        lpage = _cit_field("cit-lpage")

        # Fallback for lpage: extract from inline text after cit-fpage
        if fpage and not lpage:
            lp_m = re.search(
                r'cit-fpage[^>]*>[^<]*</span>\s*[-\u2013]\s*(\d+)',
                entry,
            )
            if lp_m:
                lpage = lp_m.group(1)

        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # --- DOI ---
        doi = ""
        dm = re.search(r'data-doi=([^\s>]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1).strip('"')))

        # Fallback
        if not title and not authors:
            title = strip_tags(entry).strip()

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

def _find_h2_headings(html):
    """Find all h2 headings and their positions.

    Returns list of (start_pos, heading_text).
    """
    entries = []
    for m in re.finditer(r'<h2[^>]*>(.*?)</h2>', html, re.DOTALL):
        text = strip_tags(m.group(1)).strip()
        if text:
            entries.append((m.start(), text))
    return entries


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    CSHLP-specific: start at the Abstract heading (abstract + keywords included);
    site chrome (e.g. "Articles Citing This Article") is dropped as an end boundary.
    """
    # Find article container
    m = re.search(r'class="article\s[^"]*fulltext-view[^"]*"[^>]*>', html)
    if not m:
        m = re.search(r'class="article[^"]*"[^>]*>', html)
    if not m:
        return ""

    content = html[m.end():]
    h2s = _find_h2_headings(content)
    if not h2s:
        return ""

    # Find start: at the Abstract heading (include abstract + keywords)
    start = 0
    for i, (hpos, text) in enumerate(h2s):
        if text.lower() == "abstract":
            start = hpos
            break
    else:
        # No Abstract h2 — try abstract div
        abs_div = re.search(r'<div[^>]*class="?section abstract"?', content)
        if abs_div:
            start = abs_div.start()

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text) in enumerate(h2s):
        if _REF_RE.search(text) and pos >= start:
            first_ref_idx = i
            break

    # Build body from two zones
    # First, capture any content between start and the first body h2
    # (intro text that appears without a heading)
    parts = []
    first_body_h2 = None
    for pos, text in h2s:
        if pos >= start:
            first_body_h2 = pos
            break
    if first_body_h2 and first_body_h2 > start:
        parts.append((start, first_body_h2))

    for i, (pos, text) in enumerate(h2s):
        if pos < start:
            continue
        if _REF_RE.search(text):
            continue
        if _CHROME_RE.search(text.strip()):
            continue

        end = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)

        if first_ref_idx is None or i < first_ref_idx:
            parts.append((pos, end))
        else:
            if _SUPP_RE.search(text):
                parts.append((pos, end))

    if not parts:
        return ""

    body_html = ""
    for s, e in parts:
        body_html += content[s:e]

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse CSHLP HTML into a refs.json-format dict plus main_text."""
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
