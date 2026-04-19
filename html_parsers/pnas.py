"""PNAS (pnas.org) HTML parser."""

import re
from html import unescape
from urllib.parse import parse_qs, urlparse

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_all_meta,
    get_meta,
    parse_meta_authors,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "Open in Viewer",
    "Crossref",
    "PubMed",
    "Google Scholar",
)

# Reference heading pattern
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|supporting information|appendix',
    re.IGNORECASE,
)

# Site chrome headings
_CHROME_RE = re.compile(
    r'^further reading|^related articles|^you may also|^continue reading'
    r'|^sign up for|^metrics|^total views|^total citations'
    r'|^full text$|^actions$|^resources$|^on this page|^cite$'
    r'|^add to collections|^information\s*&|^view options'
    r'|^figures$|^tables$|^media$|^share$'
    r'|^request username|^create a new account|^login$|^change password',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    Targets PNAS floating navigation bar (section nav + Info/Metrics/Citations
    collateral menu), "Open in Viewer" figure buttons, "Sign up for PNAS alerts"
    promos, and collapsed-bibliography CSS rules. Also expands collapsed
    contributor sections so affiliations are visible when rendered via CDP.
    """
    # Floating bar: section nav + Info/Metrics/Citations collateral menu
    html = _remove_nested_element(
        html, r'<div[^>]*class="[^"]*core-nav-wrapper[^"]*"[^>]*>'
    )
    # Expand collapsed contributor sections inside core-collateral dialog
    # so affiliations are visible when rendered via CDP.
    html = re.sub(r'(id=con\d+_content)\s+style=display:none', r'\1', html)
    # Remove "Open in Viewer" buttons inside figures
    html = re.sub(r'<button[^>]*data-open=viewer[^>]*>.*?</button>', '', html, flags=re.DOTALL)
    # Remove "Sign up for PNAS alerts" promo blocks
    html = _remove_nested_element(
        html, r'<div[^>]*class=["\']?signup-alert-ad["\']?[^>]*>'
    )
    # Expand collapsed bibliography. The static "Show all references"
    # toggle lives in a truncation-wrapper div after the list; the same
    # markup also appears inside <template id=citations_truncate_template>
    # and <template id=coreProducts_truncate_template>, which JS can clone
    # back into the DOM when the page is reopened. Strip every occurrence
    # so no variant of the toggle renders.
    while True:
        stripped = _remove_nested_element(
            html, r'<div[^>]*class="?truncation-wrapper[^>]*>'
        )
        if stripped == html:
            break
        html = stripped
    # A runtime CollapsibleText widget (embedded as a data: URI script)
    # adds `data-method` on the list and `hidden` on listitems beyond the
    # first few; CSS then hides them. Modifying the saved HTML cannot
    # undo runtime attribute additions, so inject a style override with
    # higher specificity + !important that forces listitems visible
    # regardless of the hidden attribute added by JS.
    override = (
        "<style>"
        "#bibliography-collapsible-text [role=listitem][hidden]"
        "{display:flex!important}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    # Legacy collapse (older PNAS HTMLs): CSS rules on [data-method=height]
    # and the matching HTML attribute. No-op on current captures; kept as a
    # safety net for older files in the wild.
    html = re.sub(r'\[data-method=height\]\{max-height:[^}]*\}', '', html)
    html = re.sub(r'\[data-method\]\{[^}]*overflow:hidden[^}]*\}', '', html)
    html = re.sub(r'\[data-method\]:after\{[^}]*\}', '', html)
    html = re.sub(r'(<[^>]*)\s+data-method=height', r'\1', html)
    return html


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
    title = get_meta(html, "citation_title")
    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    volume = get_meta(html, "citation_volume")
    issue = get_meta(html, "citation_issue")
    doi = format_doi(get_meta(html, "citation_doi"))

    date = get_meta(html, "citation_publication_date")
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
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_body_affiliations(html):
    """Extract author-to-affiliation mapping from core-collateral contributors tab.

    Each author is a ``<div id=conN property=author typeof=Person>`` block
    containing givenName, familyName, and nested
    ``<div property=affiliation typeof=Organization><span property=name>``
    elements.  Returns a dict mapping (givenName, familyName) -> [affiliation].
    """
    author_affs = {}
    for m in re.finditer(
        r'<div[^>]*\bid=con\d*\b[^>]*property=author[^>]*typeof=Person[^>]*>',
        html,
    ):
        # Bound the author block at the next author div (id=conN, with or
        # without a numeric suffix). Single-author papers use id=con only.
        rest = html[m.end():]
        next_author = re.search(
            r'<div[^>]*\bid=con\d*\b[^>]*property=author', rest,
        )
        end = m.end() + (next_author.start() if next_author else 5000)
        block = html[m.start():end]

        gn = re.search(r'property=givenName[^>]*>([^<]+)', block)
        fn = re.search(r'property=familyName[^>]*>([^<]+)', block)
        if not gn or not fn:
            continue
        given = gn.group(1).strip()
        family = fn.group(1).strip()

        affs = []
        for am in re.finditer(
            r'property=affiliation[^>]*typeof=Organization[^>]*>'
            r'.*?property=name[^>]*>(.*?)</span>',
            block, re.DOTALL,
        ):
            text = strip_tags(am.group(1)).strip()
            if text:
                affs.append(text)
        author_affs[(given, family)] = affs

    return author_affs


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name.
    Uses citation_author meta tags, falling back to body HTML structure
    (core-collateral contributors tab) when meta tags lack affiliations.
    """
    meta_authors = parse_meta_authors(html)
    authors = [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in meta_authors
    ]

    # If meta tags lack affiliations, try body HTML structure
    if not any(a["affiliation"] for a in authors):
        body_affs = _parse_body_affiliations(html)
        if body_affs:
            for i, meta_a in enumerate(meta_authors):
                # Match meta author name to body author by given/family name
                name = meta_a["name"]  # "LastName, Given" or "Given LastName"
                for (given, family), affs in body_affs.items():
                    if family in name and given.split()[0] in name:
                        authors[i]["affiliation"] = affs
                        break

    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    Fields are parsed from citation-content HTML (<em> for journal, <b> for
    volume) and the Google Scholar lookup URL (title, authors, year, pages, doi).
    """
    refs = []
    m = re.search(r'id=["\']?bibliography-collapsible-text["\']?', html)
    if not m:
        return refs

    # Scope to the bibliography section only (</section> closes it)
    bib_start = m.start()
    sec_end = re.search(r'</section>', html[bib_start:])
    bib_html = html[bib_start:bib_start + sec_end.start()] if sec_end else html[bib_start:]
    # Find all listitem starts
    items = [rm.start() for rm in re.finditer(
        r'<div[^>]*role=["\']?listitem["\']?', bib_html
    )]

    for i, start in enumerate(items):
        end = items[i + 1] if i + 1 < len(items) else len(bib_html)
        entry = bib_html[start:end]

        # Citation-content raw HTML (preserves <em>/<i>/<b> tags)
        cm = re.search(
            r'class=["\']?citation-content["\']?[^>]*>(.*?)</div>',
            entry, re.DOTALL,
        )
        if not cm:
            continue
        raw_html = cm.group(1)

        # Journal from <em> or <i>
        jm = re.search(r'<(em|i)>(.*?)</\1>', raw_html)
        journal = strip_tags(jm.group(2)).strip().rstrip('.') if jm else ""

        # Volume from <b>; fallback: text right after journal tag
        # ("Vol(Issue):Pages" or "Vol, Pages")
        volume = ""
        issue = ""
        post_pages = ""
        vm = re.search(r'<b>(\d+)</b>', raw_html)
        if vm:
            volume = vm.group(1)
        elif jm:
            after_journal = raw_html[jm.end():]
            after_text = strip_tags(after_journal).strip().lstrip(',').strip()
            # Match "Vol(Issue):Pages" e.g. "11(12):951-964"
            m_vip = re.match(
                r'(\d+)\s*(?:\((\d+[^)]*)\))?\s*[:,]\s*([\w\d\-\u2013\u2014]+)',
                after_text,
            )
            if m_vip:
                volume = m_vip.group(1)
                issue = m_vip.group(2) or ""
                post_pages = m_vip.group(3).replace('\u2013', '-').replace('\u2014', '-')

        # DOI from Crossref link
        doi = ""
        dm = re.search(r'href=(https://doi\.org/[^\s>"\']+)', entry)
        if dm:
            doi = unescape(dm.group(1))

        # Google Scholar lookup URL carries title, authors, year, pages.
        # The "scholar?q=..." search URL has none of these structured fields.
        title = ""
        year = ""
        pages = ""
        authors = []
        gs = re.search(
            r'href="(https://scholar\.google\.com/scholar_lookup\?[^"]*)"',
            entry,
        )
        if gs:
            gs_params = parse_qs(urlparse(unescape(gs.group(1))).query)
            title = gs_params.get('title', [''])[0]
            year = gs_params.get('publication_year', [''])[0]
            pages = gs_params.get('pages', [''])[0]
            if not doi:
                gs_doi = gs_params.get('doi', [''])[0]
                if gs_doi:
                    doi = format_doi(gs_doi)
            authors = [
                a.strip() for a in gs_params.get('author', []) if a.strip()
            ]

        # Fallback: parse year from "(YYYY)" in the citation text
        if not year:
            ym = re.search(r'\((\d{4})\)', strip_tags(raw_html))
            if ym:
                year = ym.group(1)

        # Fallback: parse title from text between (Year) and journal tag
        # Format: "Authors (Year) Title. Journal Vol(Issue):Pages."
        if not title and jm:
            pre_em = raw_html[:jm.start()]
            pre_text = strip_tags(pre_em).strip()
            # Match "(Year) Title."
            m_title = re.search(r'\(\d{4}\)\s*(.+?)\.\s*$', pre_text)
            if m_title:
                title = m_title.group(1).strip()

        # Fallback: parse authors from citation text.  Authors are
        # everything before either "(Year)" or before <em>/<i> if no year.
        if not authors and jm:
            pre_em = raw_html[:jm.start()]
            pre_text = strip_tags(pre_em).strip()
            # Cut off at "(YYYY)" if present
            m_year = re.search(r'\(\d{4}\)', pre_text)
            author_text = pre_text[:m_year.start()].strip() if m_year else pre_text
            author_text = author_text.rstrip(',').rstrip('.').strip()
            if author_text:
                # Split on " & " or ", " — handle "A B, C D & E F" patterns
                parts = re.split(r'\s*&\s*|,\s+(?=[A-Z])', author_text)
                authors = [
                    p.strip().rstrip('.').strip()
                    for p in parts if p.strip()
                ]

        # Use post-journal pages if GS URL didn't provide them
        if not pages and post_pages:
            pages = post_pages

        refs.append({"": {
            "title": title,
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _find_h2_headings(html):
    """Find all h2 headings and positions."""
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
    PNAS-specific: start from Abstract or Significance h2 (inclusive);
    remove site chrome headings; append keywords from meta tag.
    """
    # Find article container
    m = re.search(r'class="[^"]*article-container[^"]*"[^>]*>', html)
    if not m:
        return ""

    content = html[m.end():]
    h2s = _find_h2_headings(content)
    if not h2s:
        return ""

    # Find key section indices
    abstract_idx = None
    significance_idx = None
    for i, (pos, text) in enumerate(h2s):
        if text.lower() == "abstract":
            abstract_idx = i
        elif text.lower() == "significance":
            significance_idx = i

    # Start main_text from Significance or Abstract (whichever comes first)
    start = 0
    if significance_idx is not None and abstract_idx is not None:
        start = h2s[min(significance_idx, abstract_idx)][0]
    elif abstract_idx is not None:
        start = h2s[abstract_idx][0]
    elif significance_idx is not None:
        start = h2s[significance_idx][0]
    else:
        # No abstract or significance — look for articleBody directly
        ab = re.search(r'<[^>]*property=articleBody[^>]*>', content)
        if ab:
            start = ab.end()

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text) in enumerate(h2s):
        if _REF_RE.search(text) and pos >= start:
            first_ref_idx = i
            break

    # Build body: from abstract/significance through body to supplementary
    parts = []

    # Capture intro content before first h2 (only when start is not at an h2)
    first_h2_after_start = None
    for pos, text in h2s:
        if pos >= start:
            first_h2_after_start = pos
            break
    if first_h2_after_start and first_h2_after_start > start:
        parts.append((start, first_h2_after_start))

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
    text = drop_noise(text, _NOISE)

    # Append keywords from meta tag
    kw_str = get_meta(html, "keywords")
    if kw_str:
        keywords = [k.strip() for k in kw_str.split(",") if k.strip()]
        if keywords:
            text += "\n\n## Keywords\n\n" + ", ".join(keywords)

    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse PNAS HTML into a papers/*.json-format dict."""
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
