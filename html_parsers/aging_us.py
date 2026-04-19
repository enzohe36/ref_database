"""Aging (aging-us.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    strip_common,
    strip_tags,
    tags_to_text,
)

_NOISE = (
    "[PubMed]",
    "Open in a new tab",
    "View this article via",
)

_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    Strips the <header class=navigation> top bar (Aging logo, social media
    icons for Facebook/X/Instagram/LinkedIn/YouTube/Bluesky/Reddit, and the
    "Submit an Article" button).
    """
    return _remove_nested_element(
        html,
        r'<header[^>]*class="?navigation\b[^>]*>',
    )


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Uses standard citation_* meta tags. The journal title in citation_journal_title
    may contain "(Albany NY)" etc.; rstrip trailing period only.
    """
    date = get_meta(html, "citation_publication_date") or get_meta(html, "citation_date")
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
        "year": year,
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    aging-us wraps each author in <span class=author>Display Name<sup
    class=author-affiliations>...<a data-tooltip="Affiliation text">N</a>...</sup></span>
    inside an <h4 class=authors> header. Affiliations live in data-tooltip
    attributes of the nested anchor tags.
    """
    authors = []
    seen = set()
    # Scope to the <h4 class=authors> block
    h4_m = re.search(r'<h4\s+class="?authors"?[^>]*>(.*?)</h4>', html, re.DOTALL)
    scope = h4_m.group(1) if h4_m else html

    for m in re.finditer(
        r'<span\s+class="?author"?[^>]*>(.*?)</span>\s*(?=<span\s+class="?author"?|$)',
        scope, re.DOTALL,
    ):
        block = m.group(1)
        # Name is the text before <sup class=author-affiliations>
        sup_m = re.search(r'<sup\s+class="?author-affiliations', block)
        name_html = block[:sup_m.start()] if sup_m else block
        display = strip_tags(name_html).strip().rstrip(',').strip()
        if not display or display in seen:
            continue
        seen.add(display)

        affs = []
        for am in re.finditer(r'data-tooltip="([^"]+)"', block):
            t = unescape(am.group(1)).strip()
            if t and t not in affs:
                affs.append(t)

        authors.append({
            "author": _display_to_initials(display),
            "affiliation": affs,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_ref_text(text):
    """Parse one aging-us reference's plain text into structured fields.

    Format observed (after stripping [PubMed] link text):
      "Author1, Author2 and Author3. Title. Journal. Year; Volume:Pages."
    or:
      "Author1 Title. Journal. Year; Volume:Pages."  (older, no period after authors)
    Returns dict with title/journal/year/volume/issue/pages/doi/authors.
    """
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    # Strip trailing "[PubMed]" and similar markers
    text = re.sub(r'\[\s*PubMed\s*\]\s*\.?$', '', text).strip()

    result = {
        "title": "", "journal": "", "year": "", "volume": "", "issue": "",
        "pages": "", "doi": "", "authors": [],
    }

    # Year/volume/pages: " YYYY; VOLUME:PAGES"  (pages may be "X-Y" or single page)
    m = re.search(
        r'(\d{4})\s*;\s*([\d]+)(?:\s*\(([^)]+)\))?\s*:\s*([\w\d\-\u2013]+)',
        text,
    )
    if m:
        result["year"] = m.group(1)
        result["volume"] = m.group(2)
        if m.group(3):
            result["issue"] = m.group(3)
        result["pages"] = m.group(4).replace('\u2013', '-')

    # Separate pre-year portion into authors + title + journal
    if m:
        pre = text[:m.start()].strip().rstrip('.').strip()
        # Journal is the last ". "-separated token before the year
        parts = [p.strip() for p in pre.split('. ') if p.strip()]
        author_text = ""
        if parts:
            result["journal"] = parts[-1].rstrip('.').strip()
            rest = parts[:-1]
            if rest:
                result["title"] = rest[-1].strip()
                author_text = '. '.join(rest[:-1]).strip()
        else:
            author_text = pre

        # Fallback: older aging-us entries have a single author with no period
        # between author and title (e.g. "Martin GM Genetics and aging; ...").
        # The title then incorrectly contains the author prefix. Detect a
        # leading "Lastname Initials " pattern and split.
        if not author_text and result["title"]:
            am = re.match(
                r'([A-Z][A-Za-zÀ-ÿ\-\']+)\s+([A-Z]{1,4})\s+([A-Z].+)$',
                result["title"],
            )
            if am:
                author_text = f"{am.group(1)} {am.group(2)}"
                result["title"] = am.group(3).strip()

        if author_text:
            # Authors separated by commas and "and". Strip "et al." prefix.
            author_text = re.sub(r',?\s+and\s+', ', ', author_text)
            for a in author_text.split(','):
                a = a.strip().rstrip('.').strip()
                if not a or a.lower() == 'et al':
                    continue
                result["authors"].append(a)

    return result


def _parse_references(html):
    """Extract reference list from <ul class=references><li id=R1>...</li>...

    Each <li> contains a plain-text citation with inline [PubMed] link. Parsing
    relies on the inline text pattern 'Authors. Title. Journal. Year; Vol:Pages'.
    """
    m = re.search(r'<ul\s+class="?references"?[^>]*>', html)
    if not m:
        return []
    # Scope to matching </ul>
    pos = m.end()
    depth = 1
    end = len(html)
    while depth > 0:
        no = re.search(r'<ul[\s>]', html[pos:])
        nc = re.search(r'</ul>', html[pos:])
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
    li_starts = list(re.finditer(r'<li\s+id="?R\d+"?[^>]*>', refs_html))
    for i, li_m in enumerate(li_starts):
        li_end = li_starts[i + 1].start() if i + 1 < len(li_starts) else len(refs_html)
        entry_html = refs_html[li_m.end():li_end]

        # DOI from any href inside the entry
        doi = ""
        dm = re.search(r'https?://(?:dx\.)?doi\.org/([^\s"\'>]+)', entry_html)
        if dm:
            doi = format_doi(unescape(dm.group(1)))

        # Strip leading "<b>N.</b>" label
        inner = re.sub(r'^\s*<b>[^<]*</b>\s*', '', entry_html)
        text = strip_tags(inner)
        parsed = _parse_ref_text(text)
        parsed["doi"] = doi or parsed.get("doi", "")
        refs.append({"": parsed})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract text from <div class="abstract article-text">."""
    m = re.search(r'<div\s+class="?abstract\s+article-text"?[^>]*>(.*?)</div>', html, re.DOTALL)
    if not m:
        return ""
    # Drop the "Abstract" h3 header; the rest is the body
    content = m.group(1)
    content = re.sub(r'<h3[^>]*>.*?</h3>', '', content, flags=re.DOTALL)
    return strip_tags(content).strip()


def _parse_main_text(html):
    """Extract body text.

    Boundary rules: abstract + each section-container (Introduction, Results,
    Discussion, Methods, and any supplementary-matching sections). References
    section and site chrome are excluded.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")

    # Body: the non-abstract <div class=article-text> block. Take everything
    # up to the first <h2>References</h2>; this captures section-containers
    # as well as bare h2 sections like Acknowledgments and Conflicts of
    # Interest that sit outside the section-container wrappers.
    body_chunks = []
    for m in re.finditer(r'<div\s+class="?article-text"?[^>]*>', html):
        tag = m.group(0)
        if 'abstract' in tag:
            continue
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

        # Cut off at the first References h2 if present
        ref_h2 = re.search(r'<h2[^>]*>\s*References\s*</h2>', body_html, re.IGNORECASE)
        if ref_h2:
            body_html = body_html[:ref_h2.start()]

        body_html = extract_captions(body_html)
        body_html = strip_common(body_html)
        text = tags_to_text(body_html)
        if text.strip():
            body_chunks.append(text)
        break  # only the first non-abstract article-text block

    if body_chunks:
        parts.append("\n\n".join(body_chunks))

    # Keywords from meta tag
    kw = get_meta(html, "citation_keywords") or get_meta(html, "keywords")
    if kw:
        keywords = [k.strip() for k in re.split(r'[,;]', kw) if k.strip()]
        if keywords:
            parts.append(f"## Keywords\n\n{', '.join(keywords)}")

    result = "\n\n".join(parts)
    return drop_noise(result, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse aging-us HTML into a papers/*.json-format dict."""
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
