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
    get_meta,
    neutralize_media_queries,
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
    """Apply Phase 2 layout rules for pnas.org (Atypon).

    Step 1: cap body width at 752 px (720 + 16/16 gutters), center on
            page, neutralize @media so the publisher's narrow-form CSS
            applies at any viewport.
    Step 3: neutralize sticky/fixed chrome — the in-article sticky
            section navigation ([data-core-nav=header]) and the
            position:sticky core-collateral figure-viewer shell.
    Step 5: ad blocks — pb-ad header bar, signup-alert-ad / signup-ad
            promo banners that PNAS embeds inline in the article body.
    Step 8: add an 8 px bottom margin between figure img and figcaption
            so they're not flush against each other.
    Step 9: expand collapsed in-scope content — reference list entries
            hidden via the HTML5 `hidden` attribute (revealed natively
            by the "Show all references" toggle).
    """
    # Step 1 — force narrow-form CSS branch unconditionally so desktop
    # @media-gated sidebars don't engage at wide viewports, then cap and
    # center the body.
    html = neutralize_media_queries(html)

    override = (
        "<style>"
        # Step 1 — body cap + centering. 752 = 720 reading column + 32 px
        # gutter padding.
        "html{margin:0!important;padding:0!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # Step 3 — strip sticky / fixed page chrome.
        ".core-collateral,"
        "[data-core-nav=header]"
        "{display:none!important;}"
        # Step 5 — ad blocks and inline signup promos.
        ".pb-ad,"
        ".signup-alert-ad,"
        ".signup-ad"
        "{display:none!important;}"
        # Step 8 — visible spacing between figure image and caption.
        ".article-container figure img"
        "{margin-bottom:8px!important;}"
        # Step 9 — expand collapsed reference list entries. PNAS wraps
        # the bibliography in a `[data-method=height]` container that
        # caps height at 388 px and applies a 200 px gradient fade via
        # ::after, with hidden refs gated by `[data-method] [hidden]
        # {display:none!important}` (more specific than a bare [hidden]
        # rule). Override the cap, the fade, and the hidden gate, then
        # hide the now-orphan "Show all references" button wrapper.
        "[data-method=height]"
        "{max-height:none!important;}"
        "[data-method=height]::after"
        "{display:none!important;content:none!important;}"
        "[data-method] [role=listitem][hidden]"
        "{display:block!important;}"
        ".truncation-wrapper"
        "{display:none!important;}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
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

def _split_pnas_authors(text):
    """Split a PNAS reference author-text into individual author strings.

    PNAS uses two formats interchangeably:
      Format 1 (modern, dominant): "I Surname, II Surname, III Surname"
        — initials precede each surname.  Splitting on ", " followed by
        an uppercase letter yields one author per chunk.
      Format 2 (older, e.g. 1997 issues): "Surname, I. I., Surname, I. I."
        — surname-comma-initials with comma INSIDE each author.  A naive
        ", [A-Z]" split fragments these into 2N pieces.

    Strategy: split on " & " or "; " (unambiguous separators) first.
    Within each segment, detect Format 2 by checking whether the second
    comma-token is initials-only (1-3 capital letters with optional
    periods, spaces, or hyphens).  If so, regroup surname-initials pairs.
    Otherwise apply the Format 1 split on ", " before an uppercase letter.
    """
    initials_re = re.compile(r'^[A-Z]\.?(?:[\s\-]*[A-Z]\.?)*$')
    chunks = re.split(r'\s*(?:&|;)\s*', text)
    out = []
    for chunk in chunks:
        chunk = chunk.strip().rstrip(',').rstrip('.').strip()
        if not chunk:
            continue
        # Normalize thin-space (U+2009) and nbsp (U+00A0) to regular space
        # so initials-tokens "J. B." render uniformly for the splitter
        # and for downstream format_author_name.
        chunk = chunk.replace('\u2009', ' ').replace('\u00a0', ' ')
        comma_tokens = [t.strip() for t in chunk.split(',') if t.strip()]
        # Format 2 detection: first token is a single-word surname; the
        # second comma-token is initials-only.  Single-author chunks
        # ("Kunkel, T. A.") and multi-author chunks
        # ("Bell, J. B., Eckert, K. A., ...") both satisfy this.
        is_fmt2 = (
            len(comma_tokens) >= 2
            and bool(initials_re.match(comma_tokens[1]))
            and ' ' not in comma_tokens[0]
        )
        if is_fmt2:
            i = 0
            while i < len(comma_tokens):
                surname = comma_tokens[i]
                if (i + 1 < len(comma_tokens)
                        and initials_re.match(comma_tokens[i + 1])):
                    out.append(f"{surname}, {comma_tokens[i + 1]}")
                    i += 2
                else:
                    out.append(surname)
                    i += 1
        else:
            for p in re.split(r',\s+(?=[A-Z])', chunk):
                p = p.strip().rstrip('.').strip()
                if p:
                    out.append(p)
    return out


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
            # Article numbers / page ranges appear between </b> and "(YEAR)"
            # in the modern Atypon layout: "<em>J</em> <b>10</b>, e66198 (2021)."
            after_b = raw_html[vm.end():]
            m_art = re.search(
                r'^[,\s]*'
                r'(?:\(([^)]+)\)[,\s]*)?'  # optional (issue)
                r'([A-Za-z]?[\w.\-\u2010-\u2014]+?)'
                r'\s*\(\d{4}\)',
                after_b,
            )
            if m_art:
                if m_art.group(1) and not issue:
                    issue = m_art.group(1).strip()
                tok = re.sub(r'[\u2010-\u2014]', '-', m_art.group(2)).strip('.,')
                if re.match(r'^[A-Za-z]?[\w.]+(-[A-Za-z]?[\w.]+)?$', tok):
                    post_pages = tok
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
            # Book references wrap the book title in <em> the same way
            # journal articles wrap the journal name; the GS URL
            # disambiguates.  When GS provides a title that matches the
            # <em> text and provides no journal_title or pages parameter,
            # the <em> was the book title — clear journal and volume so
            # we don't double-record it.
            gs_journal = gs_params.get('journal_title', [''])[0]
            gs_pages = gs_params.get('pages', [''])[0]
            if (title and journal and not gs_journal and not gs_pages
                    and title.strip().lower() == journal.strip().lower()):
                journal = ""
                volume = ""
                issue = ""
                post_pages = ""

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
                authors = _split_pnas_authors(author_text)

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

    # Strip in-body chrome (signup ads etc.) before the text pipeline.
    # PNAS uses unquoted class attributes, so use _remove_nested_element
    # directly with patterns that tolerate quoted or unquoted values.
    for cls in ("signup-alert-ad", "signup-ad"):
        prev = None
        while prev != body_html:
            prev = body_html
            body_html = _remove_nested_element(
                body_html,
                rf'<div[^>]*\bclass=(?:"[^"]*\b{re.escape(cls)}\b[^"]*"|'
                rf"'[^']*\b{re.escape(cls)}\b[^']*'|"
                rf'{re.escape(cls)}(?=[\s>]))[^>]*>',
            )
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
