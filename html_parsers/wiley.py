"""Wiley (onlinelibrary.wiley.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
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
    "Open in new window",
    "Open in a new tab",
    "Open in viewer",
    "Web of Science",
    "Google Scholar",
    "PubMed",
    "Search for more papers by this author",
    "CAS",
    "Wiley Online Library",
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

    Wiley HTMLs in this corpus do not contain visually impairing overlays;
    returns html unchanged.
    """
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, volume, issue, year, pages, doi.

    Uses standard citation_* meta tags. Wiley emits extra citation_author
    tags for affiliations, but the main paper's title/journal/volume/... tags
    are reliable.
    """
    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_online_date"))
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

def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Wiley wraps each author in a <div class="author-info accordion-tabbed__content">
    block containing <p class=author-name> (display name) followed by one or
    more <p> elements holding the affiliation text. Prefer the desktop list
    (loa-wrapper loa-authors hidden-xs desktop-authors) to avoid duplicates
    from the mobile list.
    """
    # Scope to desktop loa wrapper if present
    dm = re.search(
        r'<div\s+class="?loa-wrapper\s+loa-authors\s+hidden-xs\s+desktop-authors"?[^>]*>',
        html,
    )
    if dm:
        pos = dm.end()
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
        scope = html[dm.end():end]
    else:
        scope = html

    authors = []
    seen = set()
    for m in re.finditer(
        r'<div\s+class="?author-info\s+accordion-tabbed__content"?[^>]*>(.*?)</div>',
        scope, re.DOTALL,
    ):
        block = m.group(1)
        # First <p class=author-name> inside is the display name
        nm = re.search(r'<p\s+class="?author-name"?[^>]*>([^<]+)</p>', block)
        if not nm:
            continue
        display = unescape(nm.group(1)).strip()
        if display in seen:
            continue
        seen.add(display)

        # Remaining <p> tags carry the affiliation text(s); skip those that
        # match the name again (which Wiley repeats) and the moreInfoLink.
        affs = []
        for pm in re.finditer(r'<p[^>]*>([^<]*)</p>', block):
            text = unescape(pm.group(1)).strip()
            if not text or text == display:
                continue
            affs.append(text)

        authors.append({
            "author": _display_to_initials(display),
            "affiliation": affs,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _normalize_ref_author(name):
    """Normalize a Wiley reference-author span into 'Last IN' via shared helpers.

    Handles the three shapes Wiley uses ('Stein H', 'Adam, N.',
    'R. P. Barnes') uniformly via parse_combined_name + format_name.
    """
    return format_author_name(name)


def _parse_references(html):
    """Extract the reference list.

    Wiley references live inside <section class="article-section
    article-section__references"> -> <ul class="rlist separator"> with each
    ref as <li data-bib-id=bN> containing structured spans:
      <span class=author>LastName IN</span>
      (<span class=pubYear>YYYY</span>)
      <span class=articleTitle>Title</span>
      <i class=journalTitle>Journal</i>
      <span class=vol>Vol</span>
      <span class=pageFirst>X</span> - <span class=pageLast>Y</span>
      <span class="hidden data-doi">10.xxx/...</span>
    """
    rs = re.search(
        r'<section\s+class="?article-section\s+article-section__references"?[^>]*>',
        html,
    )
    if not rs:
        return []
    # Scope to matching </section>
    pos = rs.end()
    depth = 1
    end = len(html)
    while depth > 0:
        no = re.search(r'<section[\s>]', html[pos:])
        nc = re.search(r'</section>', html[pos:])
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
    refs_html = html[rs.end():end]

    refs = []
    li_starts = list(re.finditer(r'<li\s+data-bib-id=[^>]*>', refs_html))
    for i, li_m in enumerate(li_starts):
        li_end = li_starts[i + 1].start() if i + 1 < len(li_starts) else len(refs_html)
        entry = refs_html[li_m.end():li_end]

        def _field(cls, tag='span'):
            m = re.search(
                rf'<{tag}\s+class="?{cls}"?[^>]*>(.*?)</{tag}>',
                entry, re.DOTALL,
            )
            return strip_tags(m.group(1)).strip() if m else ""

        title = _field("articleTitle")
        if not title:
            title = _field("chapterTitle")
        if not title:
            title = _field("bookTitle")

        journal = _field("journalTitle", tag='i').rstrip('.')
        if not journal:
            journal = _field("journalTitle").rstrip('.')

        year = _field("pubYear")
        volume = _field("vol")
        issue = _field("issue")
        fpage = _field("pageFirst")
        lpage = _field("pageLast")
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # DOI: prefer the visible linkout URL (the "hidden data-doi" span has
        # been seen to contain mangled values with '?' substituted for '-')
        doi = ""
        dm = re.search(
            r'href="?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry
        )
        if dm:
            doi = format_doi(unescape(dm.group(1)))
        if not doi:
            dm = re.search(
                r'<span\s+class="?hidden\s+data-doi"?[^>]*>([^<]+)</span>',
                entry,
            )
            if dm:
                doi_text = dm.group(1).strip()
                if doi_text and '?' not in doi_text:
                    doi = format_doi(doi_text)

        # Authors (structured). Wiley journals use three name formats:
        #   "Stein H"       (LastName Initials) — keep as-is
        #   "Adam, N."      (Last, Initials)   — strip comma/dots
        #   "R. P. Barnes"  (Initials Last)    — flip
        authors = []
        for am in re.finditer(
            r'<span\s+class="?author"?[^>]*>([^<]+)</span>', entry
        ):
            name = unescape(am.group(1)).strip()
            if name:
                authors.append(_normalize_ref_author(name))

        refs.append({"": {
            "title": title,
            "journal": journal,
            "volume": volume,
            "issue": issue,
            "year": year,
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_main_text(html):
    """Extract body text.

    Scope to <div class=article__body>; cut off at the References section
    (<section class="article-section article-section__references">). Any
    supplementary sections after References are captured via an SUPP_RE
    heading match.
    """
    body_m = re.search(r'<div\s+class="?article__body"?[^>]*>', html)
    if not body_m:
        return ""

    # Scope to matching </div>
    pos = body_m.end()
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
    body_html = html[body_m.end():end]

    # Find references section boundary
    ref_m = re.search(
        r'<section\s+class="?article-section\s+article-section__references"?',
        body_html,
    )
    if ref_m:
        before = body_html[:ref_m.start()]
        after = body_html[ref_m.end():]
    else:
        before = body_html
        after = ""

    # Parse before-refs block
    before = extract_captions(before)
    before = strip_common(before)
    text = tags_to_text(before)
    parts = [text] if text.strip() else []

    # Parse supplementary sections that come after references
    if after:
        for sm in re.finditer(
            r'<section\s+class="?article-section[^>]*>(.*?)</section>',
            after, re.DOTALL,
        ):
            inner = sm.group(1)
            hm = re.search(r'<h[23][^>]*>(.*?)</h[23]>', inner, re.DOTALL)
            heading = strip_tags(hm.group(1)).strip() if hm else ""
            if heading and _SUPP_RE.search(heading):
                chunk = extract_captions(inner)
                chunk = strip_common(chunk)
                supp_text = tags_to_text(chunk)
                if supp_text.strip():
                    parts.append(supp_text)

    result = "\n\n".join(parts)
    return drop_noise(result, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Wiley HTML into a refs.json-format dict plus main_text."""
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
