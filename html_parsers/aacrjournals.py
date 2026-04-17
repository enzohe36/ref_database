"""American Association for Cancer Research (aacrjournals) HTML parser."""

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
    "Google Scholar",
    "Crossref",
    "Search ADS",
    "PubMed",
)

# Reference section heading pattern
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# h2 classes for section types in AACR (Silverchair platform)
_BODY_HEADING = "section-title"
_BACK_HEADING = "backsection-title"
_REF_HEADING = "backreferences-title"
_ABSTRACT_HEADING = "abstract-title"
_BACK_OTHER = "backacknowledgements-title"


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    - GdprCookieBanner widget ("This site uses cookies. By continuing to
      use our website...") on aacrjournals and Silverchair siblings.
    - ArticleJumpLinks widget (ashpublications / Silverchair "interactive
      table of contents" bar with Abstract/Introduction/Methods/... links
      that overlays the bottom of the article).
    - OneTrust consent SDK wrapper (biologists' "By clicking 'Accept all
      cookies'..." banner and its preference center).
    """
    html = _remove_nested_element(
        html,
        r'<div[^>]*class="[^"]*widget-GdprCookieBanner[^"]*"[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div[^>]*class="[^"]*widget-ArticleJumpLinks[^"]*"[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div[^>]*\bid=["\']?onetrust-consent-sdk["\']?[^>]*>',
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

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Uses citation_author + citation_author_institution meta tags.
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

def _extract_ref_field(entry, cls):
    """Extract text of a child div with given class (e.g. article-title, source).

    Biologists wraps the journal name in <em>...</em> inside <div class=source>,
    so accept nested tags and strip them. Stops at the first </div>, which is
    fine because these fields are not nested.
    """
    m = re.search(rf'class=["\']?{cls}["\']?[^>]*>(.*?)</div>', entry, re.DOTALL)
    return strip_tags(m.group(1)).strip() if m else ""


def _parse_inline_authors_title(entry):
    """Parse authors and title from inline citation text (older AACR format).

    In older AACR papers, references have format:
      "Rodier F, Kim SH, Nijjar T, Campisi J. Cancer and aging: ... <div class=source>..."
    i.e. authors and title are plain text before the first structured div
    (source/article-title). Authors end with a period; title follows up to
    the next period before the source div.

    Returns (authors_list, title) where authors are "LastName IN" strings.
    """
    # Find citation container
    cm = re.search(r'class="citation mixed-citation"[^>]*>(.*?)</div>', entry, re.DOTALL)
    if not cm:
        return [], ""
    cit_html = cm.group(1)

    # Take text before the first source/article-title/year div (whichever first)
    first_div = re.search(
        r'<div\s+class=["\']?(?:source|article-title|year|volume|fpage)["\']?',
        cit_html,
    )
    pre = cit_html[:first_div.start()] if first_div else cit_html
    pre_text = unescape(re.sub(r'<[^>]+>', ' ', pre)).strip()
    pre_text = re.sub(r'\s+', ' ', pre_text)

    # Split authors from title: authors end with ". " before title
    # Authors part ends with "LastName IN." or "et al."
    # Title begins after. Find last "<Initials>[.] " or "et al. " boundary.
    m = re.search(r'(.*?(?:et\s+al\.?|\b[A-Z]{1,5}))\.\s+(.+?)\.?\s*$', pre_text)
    if m:
        author_text = m.group(1).strip().rstrip('.').strip()
        title = m.group(2).strip()
    else:
        author_text = pre_text
        title = ""

    # Parse authors: "LastName IN, LastName IN, ..."
    authors = []
    for part in re.split(r',\s*', author_text):
        part = part.strip().rstrip('.').strip()
        if part and part.lower() != "et al":
            authors.append(part)
    return authors, title


def _parse_structured_authors(entry):
    """Parse authors from <div class=surname>/<div class=given-names> pairs.

    AACR stores given names as pre-concatenated initials (e.g. "RK", "H. Tomas",
    "MA."). When the value is all-uppercase with no interior space, treat it
    as already-formatted initials and keep it verbatim; otherwise split on
    whitespace/period and take the first letter of each part.
    """
    authors = []
    # Between the surname and given-names divs the markup varies:
    #   AACR:       </div> <div class=given-names>
    #   Biologists: </div>, <div class=given-names>   (comma-space in text)
    # Allow any short run of characters (no angle brackets) between them.
    for nm in re.finditer(
        r'class=["\']?surname["\']?[^>]*>([^<]*)</div>'
        r'[^<]{0,6}'
        r'(?:</span>)?[^<]{0,6}'
        r'(?:<div\s+)?class=["\']?given-names["\']?[^>]*>([^<]*)</div>',
        entry,
    ):
        surname = unescape(nm.group(1)).strip()
        given = unescape(nm.group(2)).strip().rstrip('.')
        authors.append(format_name(given, surname))
    return authors


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {journal, volume, issue, year, title, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    AACR-specific: each ref is wrapped in <div data-content-id=b...>; newer
    papers have structured surname/given-names/article-title divs, older
    papers have inline author/title text before structured source/year/volume.
    """
    refs = []
    m = re.search(r'class="ref-list[^"]*"', html)
    if not m:
        return refs

    ref_section = html[m.start():]

    # Locate each ref entry. Different Silverchair journals use different
    # ID attribute names:
    #   AACR:       <div data-content-id=bN xmlns=...>
    #   Biologists: <div content-id=<paperid>cN xmlns=...>
    # Both variants carry an `xmlns` attribute that distinguishes the ref
    # wrapper from surrounding chrome.
    items = list(re.finditer(
        r'<div\s+(?:data-)?content-id=[^\s>]+\s+xmlns',
        ref_section,
    ))
    if not items:
        return refs

    # Boundary for last ref: find widget/footnote/copyright after it
    last_end = len(ref_section)
    after_last = ref_section[items[-1].end():]
    boundary = re.search(
        r'<div\s+class=["\']?(?:widget|copyright|license|footnote)\b',
        after_last,
    )
    if boundary:
        last_end = items[-1].end() + boundary.start()

    for idx, im in enumerate(items):
        end = items[idx + 1].start() if idx + 1 < len(items) else last_end
        entry = ref_section[im.start():end]

        # Structured fields
        title = _extract_ref_field(entry, "article-title")
        if not title:
            title = _extract_ref_field(entry, "chapter-title")
        journal = _extract_ref_field(entry, "source").rstrip('.')
        volume = _extract_ref_field(entry, "volume")
        issue = _extract_ref_field(entry, "issue")
        year = _extract_ref_field(entry, "year")
        fpage = _extract_ref_field(entry, "fpage")
        lpage = _extract_ref_field(entry, "lpage")
        # Older AACR refs wrap only fpage in a div; lpage is plain text
        # right after (e.g. "<div class=fpage>977</div>–90.").
        if fpage and not lpage:
            fm = re.search(
                rf'class=["\']?fpage["\']?[^>]*>{re.escape(fpage)}</div>\s*[–—-]\s*(\d[\w]*)',
                entry,
            )
            if fm:
                lpage = fm.group(1)
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # Authors: structured first, then fall back to inline text
        authors = _parse_structured_authors(entry)
        if not authors or not title:
            inline_authors, inline_title = _parse_inline_authors_title(entry)
            if not authors:
                authors = inline_authors
            if not title:
                title = inline_title

        # DOI from Crossref link
        doi = ""
        dm = re.search(
            r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry
        )
        if dm:
            doi = format_doi(unescape(dm.group(1)))

        refs.append({"": {
            "journal": journal,
            "volume": volume,
            "issue": issue,
            "year": year,
            "title": title,
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _find_h2_sections(html):
    """Find h2 headings, classifying each by class attribute.

    Returns list of (start_pos, heading_text, kind) where kind is
    'abstract', 'body', 'back', 'ref', or 'other'.
    """
    entries = []
    for m in re.finditer(r'<h2[^>]*class="([^"]*)"[^>]*>(.*?)</h2>', html, re.DOTALL):
        cls = m.group(1)
        text = strip_tags(m.group(2)).strip()
        if not text:
            continue
        if _ABSTRACT_HEADING in cls:
            kind = "abstract"
        elif _REF_HEADING in cls:
            kind = "ref"
        elif _BACK_HEADING in cls or _BACK_OTHER in cls:
            kind = "back"
        elif _BODY_HEADING in cls:
            kind = "body"
        else:
            kind = "other"
        entries.append((m.start(), text, kind))
    return entries


def _parse_abstract(html):
    """Extract abstract text from <section class=abstract>."""
    m = re.search(
        r'<section\s+class=(["\']?)abstract\1[^>]*>(.*?)</section>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return ""
    return strip_tags(m.group(2)).strip()


def _parse_body(html):
    """Extract body text between abstract and the first references heading.

    Boundary rules:
      - Start: end of abstract section (or first body/back h2 if no abstract).
      - End: first references h2.
      - After references: keep only supplementary-matched sections.
    """
    # Find article-body container (AACR uses unquoted class=article-body)
    body_m = re.search(r'<div\s+class=article-body\b[^>]*>', html)
    if not body_m:
        return ""

    # Scope to matching </div> to avoid pulling page chrome at the tail
    content = html[body_m.end():]
    pos = body_m.end()
    depth = 1
    while depth > 0 and pos < len(html):
        no = re.search(r'<div[\s>]', html[pos:])
        nc = re.search(r'</div>', html[pos:])
        if not nc:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos = pos + no.end()
        else:
            depth -= 1
            if depth == 0:
                content = html[body_m.end():pos + nc.start()]
                break
            pos = pos + nc.end()
    h2s = _find_h2_sections(content)
    if not h2s:
        return ""

    # Start: after abstract section
    start = 0
    for pos, text, kind in h2s:
        if kind == "abstract":
            abs_end = content.find('</section>', pos)
            if abs_end >= 0:
                start = abs_end + len('</section>')
            else:
                start = pos + 200
            break

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text, kind) in enumerate(h2s):
        if kind == "ref" or _REF_RE.search(text):
            first_ref_idx = i
            break

    parts = []

    # Capture un-headed intro content between abstract and first body h2
    first_non_abs_pos = None
    for pos, text, kind in h2s:
        if pos >= start and kind != "abstract":
            first_non_abs_pos = pos
            break
    if first_non_abs_pos is not None and first_non_abs_pos > start:
        parts.append((start, first_non_abs_pos))
    elif first_non_abs_pos is None and first_ref_idx is None:
        # No headings after abstract: take everything until end
        parts.append((start, len(content)))

    for i, (pos, text, kind) in enumerate(h2s):
        if pos < start:
            continue
        if kind == "abstract" or kind == "ref" or _REF_RE.search(text):
            continue
        end_pos = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)
        if first_ref_idx is None or i < first_ref_idx:
            parts.append((pos, end_pos))
        else:
            if _SUPP_RE.search(text):
                parts.append((pos, end_pos))

    if not parts:
        return ""

    body_html = ""
    for s, e in parts:
        body_html += content[s:e]

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
    AACR-specific: main_text is composed of abstract + body with "## Abstract"
    prepended to the abstract section.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")
    body = _parse_body(html)
    if body:
        parts.append(body)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse AACR HTML into a refs.json-format dict plus main_text."""
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
