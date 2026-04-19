"""BioOne (bioone.org) HTML parser."""

import re
import urllib.parse
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

_NOISE = (
    "Google Scholar",
    "Open in a new tab",
    "Open in new window",
    "View full-size image",
)

_REF_RE = re.compile(r'\breferences\b|literature\s+cited', re.IGNORECASE)

_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    - <aside id=cookieConsentLandmark>: wraps the cookieconsent.js banner
      ("This website uses cookies to provide you with a variety of
      services..."), which the library builds inside at page load.
    - <div id=accessByCopy>: "Access provided by <Institution>" panel
      (populated by JS at runtime; stripped when captured).
    - <div class="access hidden-print">: the black-bar wrapper that
      holds the access-provided panel and its close button. The
      institution text is injected by JS at page load and isn't
      always captured by SingleFile, but the wrapper renders the
      black bar regardless.

    Also rewrites every #e5e6e7 color to #fff. That hex is used
    exclusively for background / background-color on `main` and on
    every article panel (.SPIEPanel, .ArticleContentPanel,
    .KeyWordsPanel, .RelatedContentPanel, .HelpTopicsPanel,
    .SectionAnchorPanel, .TOCLineItemPanel, .ChorusArticlePanel,
    ...) — verified via CDP — so a global swap flips the entire text
    backdrop from gray to white without touching any unrelated color.
    """
    html = _remove_nested_element(
        html,
        r'<aside[^>]*\bid=["\']?cookieConsentLandmark["\']?[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div[^>]*\bid=["\']?accessByCopy["\']?[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div class="access hidden-print"[^>]*>',
    )
    html = html.replace("#e5e6e7", "#fff")
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Uses standard citation_* meta tags.
    """
    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    # BioOne's citation_journal_abbrev often contains a topical tag (e.g.
    # "rare") rather than an ISO journal abbreviation. Prefer
    # citation_journal_title (e.g. "Radiation Research") — refs.json
    # convention tolerates the full title when no clean ISO abbrev is in
    # the HTML.
    journal = get_meta(html, "citation_journal_title") or get_meta(html, "citation_journal_abbrev")
    journal = journal.rstrip(".") if journal else ""

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

# Particles still used by inline reference-author detection below.
_PARTICLES = {"de", "del", "della", "di", "du", "la", "le", "van", "von", "der", "da", "dos", "das"}


def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Uses citation_author + citation_author_institution meta tags.
    BioOne stores "Given Last" names and prefixes affiliations with a
    superscript tag letter (e.g. "aDepartment of..."); strip the tag.
    """
    authors = []
    for a in parse_meta_authors(html):
        affs = []
        for aff in a.get("affiliations", []):
            # Drop leading lowercase tag letter (a, b, c...) that marks
            # the affiliation footnote; only strip when followed by a
            # capital letter (starts the real affiliation text).
            aff = re.sub(r'^[a-z](?=[A-Z])', '', aff).strip()
            if aff:
                affs.append(aff)
        authors.append({
            "author": _display_to_initials(a["name"]),
            "affiliation": affs,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _flip_initials_last(name):
    """Convert 'JF Ward' (BioOne inline form) to 'Ward JF' via shared helpers."""
    return format_author_name(name)


def _parse_references(html):
    """Extract reference list from BioOne's <div class="ref-list table">.

    Each ref lives in nested <div class="ref-label cell"><div class="ref-content cell">
    and contains plain text like:
        1.
        JF Ward
        Some biochemical consequences ... Radiat Res 1981; 86:185–95.
        <a href="http://scholar.google.com/scholar_lookup?title=...&volume=86&publication_year=1981&pages=185-280">
    Structured fields (title/volume/year/pages) are pulled from the Google
    Scholar lookup URL; authors and journal are pulled from the surrounding
    text.
    """
    m = re.search(r'<div\s+class="?ref-list[^"]*"?[^>]*>', html)
    if not m:
        return []
    # Scope to matching </div> by depth
    pos = m.end()
    depth = 1
    end = len(html)
    while depth > 0:
        no = re.search(r'<div[\s>]', html[pos:])
        nc = re.search(r'</div>', html[pos:])
        if not nc:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                end = pos + nc.start()
            pos += nc.end()
    refs_html = html[m.end():end]

    refs = []
    # Each ref-content cell is one reference entry
    for rm in re.finditer(
        r'<div\s+class="?ref-content cell"?[^>]*>(.*?)</div>\s*</div>',
        refs_html, re.DOTALL,
    ):
        entry = rm.group(1)

        # Google Scholar lookup URL → title, volume, year, pages
        title = volume = year = pages = ""
        gs = re.search(
            r'href="(https?://scholar\.google\.com/scholar_lookup\?[^"]+)"',
            entry,
        )
        if gs:
            qs = unescape(gs.group(1))
            qs = urllib.parse.urlparse(qs).query
            params = urllib.parse.parse_qs(qs)
            title = params.get("title", [""])[0]
            volume = params.get("volume", [""])[0]
            year = params.get("publication_year", [""])[0]
            pages = params.get("pages", [""])[0].replace('\u2013', '-')

        # DOI (rarely present inline)
        doi = ""
        dm = re.search(r'href="?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1)))

        # Plain-text citation: strip the <span class=lookupLink> block and
        # the label <p>, keep everything else as text
        cleaned = re.sub(
            r'<span\s+class="?lookupLink"?[^>]*>.*?</span>',
            '', entry, flags=re.DOTALL,
        )
        cleaned = re.sub(
            r'<p\s+class="?ref-label"?[^>]*>.*?</p>',
            '', cleaned, flags=re.DOTALL,
        )
        cleaned = re.sub(r'<a\s+id=[^>]*></a>', '', cleaned)
        text = re.sub(r'\s+', ' ', strip_tags(cleaned)).strip()

        # Authors end when a full-word starts (title). In BioOne refs authors
        # are listed one per line as "Initials Last". The block continues
        # until the title sentence begins; the title ends at the journal
        # name. Walk word-by-word accepting tokens until we hit a token that
        # doesn't look like an author.
        authors = []
        if text:
            # Split into whitespace-separated tokens and regroup into author
            # pairs ("Initials" + "Surname"). Stop at the first token that
            # isn't initials-like AND isn't a surname following initials.
            tokens = text.split(' ')
            i = 0
            while i < len(tokens) - 1:
                tok = tokens[i].rstrip(',').rstrip('.')
                # Initials token: 1-5 uppercase letters (optionally with dots)
                if re.fullmatch(r'[A-Z][A-Z\.]{0,4}', tok):
                    surname_parts = [tokens[i + 1].rstrip(',').rstrip('.')]
                    # Allow particle + Last for multi-word surnames
                    if (i + 2 < len(tokens)
                            and tokens[i + 1].lower() in _PARTICLES):
                        surname_parts.append(tokens[i + 2].rstrip(',').rstrip('.'))
                        advance = 3
                    else:
                        advance = 2
                    surname = ' '.join(surname_parts).strip(',').strip()
                    if surname and surname[0].isupper():
                        initials = tok.replace('.', '')
                        authors.append(f"{surname} {initials}")
                        i += advance
                        continue
                break

            # Remainder after authors is "Title. Journal Year; Vol:Pages."
            remainder = ' '.join(tokens[i:]).strip().rstrip(',').strip()
        else:
            remainder = ""

        # Journal: text between the last full-stop before "Year;" and the
        # matching " YYYY;" pattern. If Scholar URL gave us volume/year, we
        # can pin the boundary precisely.
        journal = ""
        if remainder and year:
            jm = re.search(rf'\.\s+([^.]+?)\s+{re.escape(year)}\s*;', remainder)
            if jm:
                journal = jm.group(1).strip().rstrip('.').strip()

        # Title fallback: if Scholar URL missing, extract title from text
        if not title and remainder:
            tm = re.match(r'(.+?)\.\s+[A-Z]', remainder)
            if tm:
                title = tm.group(1).strip()

        refs.append({"": {
            "title": title,
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": "",
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract text from BioOne's ArticleContentRow after the
    ArticleContentBoldText 'Abstract' header."""
    m = re.search(
        r'<text\s+class="?ArticleContentBoldText"?[^>]*>\s*Abstract\s*</text>',
        html,
    )
    if not m:
        return ""
    # Find the following ArticleContentText block
    after = html[m.end():]
    tm = re.search(
        r'<text\s+class="?ArticleContentText"?[^>]*>(.*?)</text>',
        after, re.DOTALL,
    )
    if not tm:
        return ""
    inner = strip_common(tm.group(1))
    return strip_tags(inner).strip()


def _parse_main_text(html):
    """Extract body text.

    BioOne wraps body sections inside <div id=article-body class=body> with
    each section in <div class=section> and <h2 class=main-title> headings.
    References are a sibling <div class="ref-list table"> excluded by the
    container scoping.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")

    m = re.search(r'<div\s+id="?article-body"?[^>]*>', html)
    if m:
        pos = m.end()
        depth = 1
        end = len(html)
        while depth > 0:
            no = re.search(r'<div[\s>]', html[pos:])
            nc = re.search(r'</div>', html[pos:])
            if not nc:
                break
            if no and no.start() < nc.start():
                depth += 1
                pos += no.end()
            else:
                depth -= 1
                if depth == 0:
                    end = pos + nc.start()
                pos += nc.end()
        body_html = html[m.end():end]

        # Cut off at the first REFERENCES h2 if present inside body scope
        ref_h2 = re.search(
            r'<h2[^>]*>\s*(?:REFERENCES|References|Literature\s+Cited)\s*</h2>',
            body_html,
        )
        if ref_h2:
            body_html = body_html[:ref_h2.start()]

        body_html = extract_captions(body_html)
        body_html = strip_common(body_html)
        text = tags_to_text(body_html)
        if text.strip():
            parts.append(text)

    result = "\n\n".join(parts)
    return drop_noise(result, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse BioOne HTML into a papers/*.json-format dict."""
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
