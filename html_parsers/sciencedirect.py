"""ScienceDirect (sciencedirect.com) HTML parser."""

import json
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
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "View article",
    "View in Scopus",
    "Google Scholar",
    "Crossref",
    "Full size image",
    "Recommended articles",
    "What\u2019s this?",
    "What's this?",
    "Download all",
    "Speed:",
)

# h2 headings that are reference sections (removed from main_text)
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# Site chrome sections (always removed)
_CHROME_RE = re.compile(
    r'^cited by|^substances|^recommended articles|^article metrics|^part of special issue',
    re.IGNORECASE,
)

# h2 headings that appear before main_text
_PRE_BODY = {
    "abstract", "summary", "graphical abstract", "highlights", "keywords",
}


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    Targets ScienceDirect floating feedback button (_pendo-badge) and the
    OneTrust cookie consent SDK (banner + dark overlay + preference center).
    """
    # Feedback button
    html = _remove_nested_element(
        html, r'<button[^>]*class="[^"]*_pendo-badge[^"]*"[^>]*>'
    )
    # OneTrust cookie consent (banner + dark overlay + preference center)
    html = _remove_nested_element(
        html, r'<div[^>]*id=["\']?onetrust-consent-sdk[^>]*>'
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
    date = get_meta(html, "citation_publication_date")
    if not date:
        date = get_meta(html, "citation_online_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    issue = get_meta(html, "citation_issue")
    if issue == "N/A":
        issue = ""

    return {
        "title": get_meta(html, "citation_title"),
        "journal": get_meta(html, "citation_journal_title"),
        "volume": get_meta(html, "citation_volume"),
        "issue": issue,
        "year": year,
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _load_preload_affiliations(html):
    """Read affiliation data from window.__PRELOADED_STATE__.

    SingleFile with --block-scripts=false preserves the inline script that
    holds the article JSON, including author affiliations. The JSON uses
    Elsevier's XML-to-JSON shape: {"#name": ..., "$": {...}, "$$": [...]}.
    Returns (ref_to_text, authors_by_surname, group_affs_by_surname).
    ref_to_text and authors_by_surname use lowercase ids so case mismatches
    between the rendered HTML (bAFF1) and the preload state (AFF1) resolve.
    group_affs_by_surname[surname] is the list of all affiliation texts in
    that author's author-group; used as a fallback for old papers where
    cross-refs point at footnotes / are absent and every author shares the
    single affiliation listed in the group.
    """
    m = re.search(
        r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;',
        html, re.DOTALL,
    )
    if not m:
        return {}, {}, {}
    try:
        state = json.loads(m.group(1))
    except (ValueError, json.JSONDecodeError):
        return {}, {}, {}
    content = (state.get("authors") or {}).get("content") or []
    ref_to_text = {}
    authors_by_surname = {}
    group_affs_by_surname = {}
    for group in content:
        if group.get("#name") != "author-group":
            continue
        local_affs = []
        for child in group.get("$$", []) or []:
            if child.get("#name") != "affiliation":
                continue
            aid = ((child.get("$") or {}).get("id", "") or "").lower()
            text = ""
            for c in child.get("$$", []) or []:
                if c.get("#name") == "textfn":
                    text = c.get("_", "")
                    break
            if text:
                local_affs.append(text)
                if aid:
                    ref_to_text[aid] = text
        for child in group.get("$$", []) or []:
            if child.get("#name") != "author":
                continue
            surname = ""
            refids = []
            for c in child.get("$$", []) or []:
                cn = c.get("#name")
                if cn == "surname":
                    surname = (c.get("_") or "").replace("\xa0", " ").strip()
                elif cn == "cross-ref":
                    rid = ((c.get("$") or {}).get("refid", "") or "").lower()
                    if rid:
                        refids.append(rid)
            if surname:
                authors_by_surname[surname] = refids
                group_affs_by_surname[surname] = local_affs
    return ref_to_text, authors_by_surname, group_affs_by_surname


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name.
    ScienceDirect has <span class="text given-name"> and
    <span class="text surname"> elements for each author,
    with affiliation labels in <span class=author-ref> elements.
    Affiliation definitions are read from window.__PRELOADED_STATE__
    (preserved by SingleFile --block-scripts=false).
    """
    ref_to_text, preload_refs, group_affs = _load_preload_affiliations(html)

    authors = []
    # Find author group area
    ag_m = re.search(r'<div[^>]*id=author-group[^>]*>', html)
    if not ag_m:
        ag_m = re.search(r'<div[^>]*class=["\']?AuthorGroups[^>]*>', html)
    if not ag_m:
        return authors

    # Find the end of the author group section
    ag_end = html.find('id=show-more-btn', ag_m.end())
    if ag_end < 0:
        ag_end = ag_m.end() + 30000
    ag_html = html[ag_m.end():ag_end]

    # Extract each author: given-name/surname followed by author-ref labels
    # Pattern: author name spans, then zero or more author-ref <sup> labels,
    # until the next author or end
    for m in re.finditer(
        r'<span\b[^>]*given-name[^>]*>([^<]*)</span>\s*'
        r'<span\b[^>]*surname[^>]*>([^<]*)</span>',
        ag_html,
    ):
        given = unescape(m.group(1)).replace("\xa0", " ").strip()
        surname = unescape(m.group(2)).replace("\xa0", " ").strip()
        author_name = format_name(given, surname)

        # Collect affiliation refids (baff0005 style) that follow this author
        after = ag_html[m.end():m.end() + 500]
        next_author = re.search(r'<span\b[^>]*given-name', after)
        search_area = after[:next_author.start()] if next_author else after
        refids = [
            rm.group(1)
            for rm in re.finditer(
                r'<span[^>]*class=["\']?author-ref[^>]*id=["\']?b'
                r'([Aa][Ff][Ff]\d+)',
                search_area,
            )
        ]
        # Normalize case: HTML has bAff0005 / baff0005; preload uses aff0005
        refids = [r.lower() for r in refids]

        # Resolve via preload meta (HTML source of truth for affiliations).
        # Falls back through three lookups: HTML refids -> preload refids
        # by surname -> all affiliations in the same author-group. The last
        # fallback covers old papers where cross-refs point at footnotes
        # (or are absent) and every author shares the single affiliation
        # listed in the group.
        affiliations = []
        if ref_to_text:
            if refids:
                affiliations = [ref_to_text[r] for r in refids if r in ref_to_text]
            if not affiliations and surname in preload_refs:
                affiliations = [
                    ref_to_text[r]
                    for r in preload_refs[surname]
                    if r in ref_to_text
                ]
            if not affiliations and surname in group_affs:
                affiliations = list(group_affs[surname])

        authors.append({
            "author": author_name,
            "affiliation": affiliations,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _is_initial_token(token):
    """Check if a space-separated token represents author initials.

    A token is initials if, after removing dots, it is 1-4 alpha characters AND:
      - it is all uppercase (e.g. "J.P." -> "JP"), OR
      - it contains dots (abbreviated form, e.g. "F.d.S.e." -> "FdSe"), OR
      - it has 2+ uppercase letters with no consecutive lowercase
        (e.g. "FdSe" — mixed-case initials without dots).
    """
    stripped = token.replace(".", "")
    if not stripped.isalpha() or not 1 <= len(stripped) <= 4:
        return False
    if stripped.isupper():
        return True
    if "." in token:
        return True
    # Without dots: require 2+ uppercase and no consecutive lowercase letters
    upper_count = sum(1 for c in stripped if c.isupper())
    has_consec_lower = bool(re.search(r"[a-z]{2}", stripped))
    return upper_count >= 2 and not has_consec_lower


def _format_ref_author(name):
    """Convert reference author name to 'LastName IN' format.

    ScienceDirect references use two formats:
      'I.N. LastName' (initials first)  -> 'LastName IN'
      'LastName I.N.' (initials last)   -> 'LastName IN'
    The first unspaced part of a name is always initial, with or without dot.
    Initials may include lowercase particles (e.g. Portuguese 'd', 'e').
    """
    parts = name.split()
    if len(parts) < 2:
        return name
    # Check for leading initials
    i = 0
    while i < len(parts) - 1:
        if _is_initial_token(parts[i]):
            i += 1
        else:
            break
    if i > 0:
        initials = "".join(p.replace(".", "") for p in parts[:i])
        surname = " ".join(parts[i:])
        return f"{surname} {initials}"
    # Check for trailing initials
    j = len(parts) - 1
    while j > 0:
        if _is_initial_token(parts[j]):
            j -= 1
        else:
            break
    if j < len(parts) - 1:
        surname = " ".join(parts[: j + 1])
        initials = "".join(p.replace(".", "") for p in parts[j + 1 :])
        return f"{surname} {initials}"
    return name


def _parse_host(host_text):
    """Parse journal, volume, year, pages from host string.

    Typical formats:
        'Gut, 66 (2017), pp. 683-691'
        'Nature, 543 (2017), pp. 676-680'
        'Int J Cancer, 136 (2015), pp. E359-E386'
    """
    journal = volume = year = pages = ""
    # Try: journal, volume (year), pp. pages
    m = re.match(
        r'(.+?),\s*(\d+)\s*\((\d{4})\)(?:,\s*pp?\.\s*(.+))?',
        host_text.strip(),
    )
    if m:
        journal = m.group(1).strip()
        volume = m.group(2)
        year = m.group(3)
        pages = (m.group(4) or "").strip().rstrip(",. ")
        # Strip trailing DOI that some host strings include after pages
        pages = re.sub(r',\s*10\.\d{4,}/\S+$', '', pages).strip().rstrip(",. ")
    else:
        # Fallback: try to extract year
        ym = re.search(r'\((\d{4})\)', host_text)
        if ym:
            year = ym.group(1)
        journal = host_text.strip()
    return journal, volume, year, pages


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {journal, volume, issue, year, title, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    Parses structured spans: authors, title, host (journal info), DOI link.
    """
    refs = []
    # Find all reference ol elements (main + supplementary)
    for ol_m in re.finditer(r'<ol[^>]*class=["\']?references[^>]*>', html):
        ol_end = html.find('</ol>', ol_m.end())
        if ol_end < 0:
            ol_end = len(html)
        ol_html = html[ol_m.end():ol_end]

        li_starts = [m.start() for m in re.finditer(r'<li[^>]*>', ol_html)]
        for i, start in enumerate(li_starts):
            end = li_starts[i + 1] if i + 1 < len(li_starts) else len(ol_html)
            entry = ol_html[start:end]

            # Extract structured fields from spans/divs
            authors_m = re.search(
                r'class="authors[^"]*"[^>]*>(.*?)</div>', entry, re.DOTALL
            )
            title_m = re.search(
                r'class="title[^"]*"[^>]*>(.*?)</div>', entry, re.DOTALL
            )
            host_m = re.search(
                r'class="host[^"]*"[^>]*>(.*?)</div>', entry, re.DOTALL
            )

            title = strip_tags(title_m.group(1)).strip() if title_m else ""
            authors_text = strip_tags(authors_m.group(1)).strip() if authors_m else ""
            host_text = strip_tags(host_m.group(1)).strip() if host_m else ""

            # Parse authors
            authors = []
            if authors_text:
                # Remove trailing "et al."
                clean = re.sub(r',?\s*et\s*al\.?\s*$', '', authors_text)
                authors = [
                    _format_ref_author(a.strip())
                    for a in clean.split(",")
                    if a.strip()
                ]

            # Parse host (journal, volume, year, pages)
            journal, volume, year, pages = _parse_host(host_text)

            # Extract DOI
            doi = ""
            doi_m = re.search(r'href=["\']?(https?://doi\.org/[^"\'\s>]+)', entry)
            if doi_m:
                doi = unescape(doi_m.group(1))

            # Fallback: if no structured fields, use full text
            if not title and not authors_text:
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

def _extract_div_content(html, start_pos):
    """Extract full content of a div starting at start_pos (after opening tag).

    Uses nesting-aware matching to find the correct closing </div>.
    """
    pos = start_pos
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
            if depth == 0:
                return html[start_pos:pos + next_close.start()]
            pos += next_close.end()
    return html[start_pos:pos]


def _abstract_to_text(content):
    """Convert abstract HTML content to text preserving paragraph structure."""
    # Remove the section-title h2 (Abstract/Summary) that lives inside the div
    content = re.sub(
        r'<h2[^>]*class="[^"]*section-title[^"]*"[^>]*>.*?</h2>',
        '', content, flags=re.DOTALL,
    )
    # Remove PaperClip video/audio players embedded in abstract sections
    while True:
        prev = content
        content = _remove_nested_element(
            content, r'<span[^>]*class=["\']?video-player[^>]*>'
        )
        content = _remove_nested_element(
            content, r'<span[^>]*class=["\']?audio-player[^>]*>'
        )
        if content == prev:
            break
    content = strip_common(content)
    text = tags_to_text(content)
    text = re.sub(r'^(?:Abstract|Summary)\s*', '', text).strip()
    return text


def _parse_highlights(html):
    """Extract Highlights from ScienceDirect HTML.

    Highlights appear in Cell Press journals as a separate abstract div
    (class="abstract author-highlights") with bullet points.
    Returns text with bullet points or empty string if absent.
    """
    for m in re.finditer(
        r'<div[^>]*class="abstract\b([^"]*)"[^>]*>',
        html,
    ):
        if "highlights" not in m.group(1):
            continue
        content = _extract_div_content(html, m.end())
        # Remove the section-title h2
        content = re.sub(
            r'<h2[^>]*class="[^"]*section-title[^"]*"[^>]*>.*?</h2>',
            '', content, flags=re.DOTALL,
        )
        text = strip_tags(content).strip()
        text = re.sub(r'^Highlights\s*', '', text).strip()
        if text:
            return text
    return ""


def _parse_abstract(html):
    """Extract abstract from ScienceDirect HTML.

    Skips Highlights (class="abstract author-highlights") and Graphical
    abstract (class="abstract graphical") in favor of the real abstract
    (class="abstract author" without "highlights"/"graphical").
    Uses nesting-aware extraction for multiparagraph abstracts.
    Preserves paragraph breaks for structured abstracts.
    """
    # Find all abstract divs, prefer the one without "highlights" or "graphical"
    for m in re.finditer(
        r'<div[^>]*class="abstract\b([^"]*)"[^>]*>',
        html,
    ):
        cls_rest = m.group(1)
        if "highlights" in cls_rest or "graphical" in cls_rest:
            continue
        content = _extract_div_content(html, m.end())
        text = _abstract_to_text(content)
        if text:
            return text

    # Fallback: accept any abstract div (including highlights)
    m = re.search(r'<div[^>]*class="abstract\b[^"]*"[^>]*>', html)
    if m:
        content = _extract_div_content(html, m.end())
        text = _abstract_to_text(content)
        if text:
            return text

    # Fallback: find h2 Abstract/Summary and extract until next h2
    for label in ("Abstract", "Summary"):
        pattern = (
            r'<h2[^>]*>[^<]*' + label + r'[^<]*</h2>\s*(.*?)'
            r'(?=<h2[^>]*>)'
        )
        m = re.search(pattern, html, re.DOTALL)
        if m:
            return _abstract_to_text(m.group(1))
    return ""


def _parse_keywords(html):
    """Extract keywords from keyword divs inside the Keywords section.

    The Keywords section has <h2>Keywords</h2> followed by <div class=keyword>
    elements. Abbreviations sections use the same structure but are excluded.
    """
    keywords = []
    # Find Keywords or Subject areas section (not Abbreviations) by h2 header
    for m in re.finditer(
        r'<h2[^>]*>\s*(?:<[^>]*>)*\s*(?:Keywords|Subject areas)\s*(?:<[^>]*>)*\s*</h2>',
        html,
        re.DOTALL,
    ):
        # Extract keyword divs until next h2 or closing section
        rest = html[m.end():m.end() + 5000]
        end = re.search(r'<h2[^>]*>', rest)
        block = rest[:end.start()] if end else rest
        for kw_m in re.finditer(
            r'<div[^>]*class=["\']?keyword["\']?[^>]*>(.*?)</div>',
            block,
            re.DOTALL,
        ):
            kw = strip_tags(kw_m.group(1)).strip()
            if kw:
                keywords.append(kw)
        if keywords:
            break
    return keywords


def _parse_abbreviations(html):
    """Extract abbreviations from the Keywords area (outside Body div).

    Abbreviations appear in <div class=keywords-section> with an
    <h2>Abbreviations</h2> header. Each abbreviation is a pair of
    nested keyword divs: outer has the term, inner has the definition.
    """
    pairs = []
    for m in re.finditer(
        r'<h2[^>]*>\s*(?:<[^>]*>)*\s*Abbreviations\s*(?:<[^>]*>)*\s*</h2>',
        html,
        re.DOTALL,
    ):
        rest = html[m.end():m.end() + 5000]
        end = re.search(r'<h2[^>]*>', rest)
        block = rest[:end.start()] if end else rest
        # Each top-level keyword div has: <span>TERM</span><div class=keyword><span>DEF</span>
        # Terms may contain nested tags (e.g. <sup>Ubi</sup>PCNA)
        for kw_m in re.finditer(
            r'<div[^>]*class=["\']?keyword["\']?[^>]*>'
            r'\s*<span[^>]*>(.*?)</span>'
            r'\s*<div[^>]*class=["\']?keyword["\']?[^>]*>'
            r'\s*<span[^>]*>(.*?)</span>',
            block,
            re.DOTALL,
        ):
            term = strip_tags(kw_m.group(1)).strip()
            defn = strip_tags(kw_m.group(2)).strip()
            if term and defn:
                pairs.append(f"{term}: {defn}")
        break
    if not pairs:
        return ""
    return "## Abbreviations\n" + "\n".join(pairs)


def _find_h2_sections(html):
    """Find article section headings (not TOC/sidebar headings).

    Only matches h2 elements with section-title class or sect/sectitle IDs.
    Returns list of (start_pos, heading_text).
    """
    entries = []
    for m in re.finditer(
        r'<h2[^>]*(?:class="[^"]*section-title[^"]*"|id=sect[^>]*|class=[^>]*section-title[^>]*)[^>]*>(.*?)</h2>',
        html,
        re.DOTALL,
    ):
        text = strip_tags(m.group(1)).strip()
        if text:
            entries.append((m.start(), text))
    return entries


def _is_paywall(html):
    """Detect paywall preview pages (Section snippets, article/abs/ URL)."""
    return bool(
        re.search(r'Section snippets', html)
        or re.search(r'article/abs/', html[:3000])
    )


def _parse_body(html):
    """Extract the body-zone text (between abstract and references).

    Boundary rules:
    - Before first references: keep everything.
    - After first references: keep only supplementary materials.
    - Remove all references sections and site chrome.
    - Returns empty string for paywall preview pages.
    """
    if _is_paywall(html):
        return ""

    # Find the Body div which wraps the article body content
    body_m = re.search(r'<div[^>]*class="Body[^"]*"[^>]*id=body[^>]*>', html)
    if not body_m:
        body_m = re.search(r'<div[^>]*id=body[^>]*>', html)
    if not body_m:
        return ""

    content = html[body_m.end():]
    h2s = _find_h2_sections(content)

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text) in enumerate(h2s):
        if _REF_RE.search(text):
            first_ref_idx = i
            break

    # Build body from two zones
    # First, capture any content between Body div and the first h2
    # (body text that appears without a heading, e.g. short primers)
    parts = []
    first_h2 = h2s[0][0] if h2s else len(content)
    if first_h2 > 0:
        parts.append((0, first_h2))

    for i, (pos, text) in enumerate(h2s):
        # Skip site chrome
        if _CHROME_RE.search(text.strip()):
            continue
        # Skip references
        if _REF_RE.search(text):
            continue

        # Section range: from this h2 to the next
        end = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)

        if first_ref_idx is None or i < first_ref_idx:
            # Zone 1: keep everything
            parts.append((pos, end))
        else:
            # Zone 2: keep only supplementary
            if _SUPP_RE.search(text):
                parts.append((pos, end))

    # If no h2 sections matched, use full body content up to first ref or chrome
    if not parts and h2s:
        end = len(content)
        for pos, text in h2s:
            if _REF_RE.search(text) or _CHROME_RE.search(text.strip()):
                end = pos
                break
        if end > 0:
            parts.append((0, end))

    if not parts:
        return ""

    body_html = ""
    for start, end in parts:
        body_html += content[start:end]

    # Remove site chrome elements that appear between article sections
    body_html = _remove_nested_element(
        body_html, r'<div[^>]*class=["\']?related-content-links[^>]*>'
    )
    # Remove video player elements (contain "Speed: 1xPaused" noise)
    while True:
        prev = body_html
        body_html = _remove_nested_element(
            body_html, r'<span[^>]*class=["\']?video-player[^>]*>'
        )
        if body_html == prev:
            break
    # Remove iframes (srcdoc attribute leaks escaped HTML as text)
    body_html = re.sub(r'<iframe[^>]*>.*?</iframe>', '', body_html, flags=re.DOTALL)
    # Remove download forms (UI chrome in supplemental sections)
    body_html = re.sub(r'<form[^>]*>.*?</form>', '', body_html, flags=re.DOTALL)
    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    ScienceDirect-specific: main_text is composed of highlights + abstract +
    keywords + abbreviations + body (with markdown headers prepended).
    """
    parts = []
    highlights = _parse_highlights(html)
    if highlights:
        parts.append("## Highlights\n" + highlights)
    abstract = _parse_abstract(html)
    if abstract:
        parts.append("## Abstract\n" + abstract)
    keywords = _parse_keywords(html)
    if keywords:
        parts.append("## Keywords\n" + ", ".join(keywords))
    abbreviations = _parse_abbreviations(html)
    if abbreviations:
        parts.append(abbreviations)
    body = _parse_body(html)
    if body:
        parts.append(body)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse ScienceDirect HTML into a refs.json-format dict plus main_text."""
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
