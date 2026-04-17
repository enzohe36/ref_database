"""Molecular Biology of the Cell (molbiolcell.org) HTML parser.

Literatum-based journal platform shared with other ASCB titles. Despite
the shared class naming conventions, no existing parser under
html_parsers/ matches molbiolcell's specific fingerprints
(<li class="references__item">, <h2 class="article-section__title">,
<section class="abstractSection">).
"""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_doi,
    get_meta,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Crossref",
    "Medline",
    "Google Scholar",
    "Open in a new tab",
    "Search for more papers by this author",
)

# Reference section heading pattern
_REF_RE = re.compile(r"\breferences\b", re.IGNORECASE)

# Supplementary section patterns
_SUPP_RE = re.compile(
    r"supplement|extended data|source data|expanded view|powerpoint|appendix",
    re.IGNORECASE,
)

# Sections excluded from main_text (site chrome appearing after the body)
_CHROME_RE = re.compile(
    r"^(?:related articles?|cited by|figures|tables|references)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    - header fixed base mboc-theme: site-wide floating top bar (MBoC
      logo, Home, Current Issue, Archive, etc.).
    - scroll-to-target fixed-element: article sticky bar (Sections,
      View PDF, Tools, Share).
    """
    html = _remove_nested_element(
        html,
        r'<header [^>]*class="header fixed base mboc-theme"[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div class="scroll-to-target fixed-element"[^>]*>',
    )
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, volume, issue, year, pages, doi.

    Returns dict with those 7 keys. molbiolcell lacks citation_* meta for
    volume/issue/pages — volume/issue parsed from the breadcrumb URL
    (/toc/mboc/<vol>/<issue>); pages absent from HTML and returned as "".
    citation_journal_abbrev is absent, so journal falls back to the full
    citation_journal_title ("Molecular Biology of the Cell").
    """
    title = get_meta(html, "dc.Title") or get_meta(html, "citation_title")

    journal = get_meta(html, "citation_journal_title") or ""
    journal = journal.rstrip(".")

    # Year sources, in order of reliability:
    #   1. dc.Rights "Copyright © YYYY ..." — original publication year.
    #   2. <span class=epub-section__date>DD Mon YYYY</span> — matches dc.Rights
    #      for modern papers, but can reflect a re-indexing date for older
    #      articles (e.g. Scherthan 2000 shows "13 Oct 2017").
    #   3. dc.Date fallback.
    year = ""
    rights = get_meta(html, "dc.Rights")
    if rights:
        ym = re.search(r"Copyright\s*\S*\s*(\d{4})", rights)
        if ym:
            year = ym.group(1)
    if not year:
        em = re.search(
            r'class=["\']?epub-section__date["\']?[^>]*>([^<]+)</span>', html,
        )
        if em:
            ym = re.search(r"(\d{4})", em.group(1))
            if ym:
                year = ym.group(1)
    if not year:
        date = get_meta(html, "dc.Date") or get_meta(html, "citation_publication_date")
        if date:
            ym = re.search(r"(\d{4})", date)
            if ym:
                year = ym.group(1)

    doi = format_doi(
        get_meta(html, "publication_doi") or get_meta(html, "citation_doi")
    )

    # Volume/issue: DOI-encoded format "mbc.<vol>.<issue>.<firstpage>"
    # (older papers) takes priority; fall back to the toc breadcrumb
    # (must be /toc/<journal>/<digits>/<digits>, not "current").
    volume = ""
    issue = ""
    pages = ""
    dm = re.match(
        r"https?://doi\.org/10\.1091/mbc\.(\d+)\.(\d+)\.(\d+)", doi or "",
    )
    if dm:
        volume = dm.group(1)
        issue = dm.group(2)
        pages = dm.group(3)
    if not volume:
        # Breadcrumb TOC link; require the article__tocHeading class so a
        # nav-menu link like /toc/mboc/0/0 ("In Press") does not match.
        # Class value may be quoted or unquoted; digits must both be
        # positive (skip 0/0).
        for bm in re.finditer(
            r'href=["\']?https?://[^"\'>\s]*/toc/[^/]+/(\d+)/(\d+)[^>]*'
            r'class=["\']?[^"\'>\s]*article__tocHeading',
            html,
        ):
            if bm.group(1) != "0" and bm.group(2) != "0":
                volume = bm.group(1)
                issue = bm.group(2)
                break

    return {
        "title": title,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "year": year,
        "pages": pages,
        "doi": doi,
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

_SURNAME_PARTICLES = {
    "de", "del", "della", "dell'", "di", "da", "dos", "du",
    "van", "von", "vander", "der", "den", "ten", "ter",
    "la", "le", "el", "al", "zu", "af",
}


def _format_display_name(name):
    """Convert 'Given Middle LastName' to 'LastName IN'.

    dc.Creator values are full names without commas; same particle-aware
    formatter as jci/jove parsers.
    """
    name = (name.replace("\u2018", "'").replace("\u2019", "'")
                .replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2009", " ").replace("\u00a0", " ")).strip()
    if not name:
        return ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0]

    i = len(parts) - 1
    surname_parts = [parts[i]]
    i -= 1
    while i >= 0 and parts[i].lower().rstrip(".") in _SURNAME_PARTICLES:
        surname_parts.insert(0, parts[i])
        i -= 1
    if (len(surname_parts) > 1 and i >= 1 and parts[i] and
            parts[i][0].isupper() and not parts[i].endswith(".")):
        surname_parts.insert(0, parts[i])
        i -= 1

    surname = " ".join(surname_parts)
    given = " ".join(parts[:i + 1])
    pieces = re.split(r"[\s.\-\u2010\u2011\u2012\u2013]+", given)
    initials = "".join(p[0] for p in pieces if p and p[0].isupper())
    return f"{surname} {initials}" if initials else surname


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Uses dc.Creator meta tags (ordered) for names and the per-author
    <div class="author-info accordion-tabbed__content"> blocks for
    affiliations. Some papers omit affiliations entirely (empty list).
    """
    # Names from dc.Creator meta tags (preserve order)
    names = []
    for m in re.finditer(
        r'<meta[^>]*name=["\']?dc\.Creator["\']?[^>]*content=["\']([^"\']*)',
        html,
    ):
        names.append(unescape(m.group(1)).strip())

    # Author info blocks, in order
    affiliations = []
    info_pat = re.compile(
        r'class=["\']?author-info\s+accordion-tabbed__content["\']?[^>]*>',
    )
    div_open = re.compile(r"<div\b", re.IGNORECASE)
    div_close = re.compile(r"</div\s*>", re.IGNORECASE)
    pos = 0
    while True:
        om = info_pat.search(html, pos)
        if not om:
            break
        start = om.end()
        depth = 1
        p = start
        while depth > 0 and p < len(html):
            nxt_o = div_open.search(html, p)
            nxt_c = div_close.search(html, p)
            if nxt_c is None:
                break
            if nxt_o and nxt_o.start() < nxt_c.start():
                depth += 1
                p = nxt_o.end()
            else:
                depth -= 1
                if depth == 0:
                    chunk = html[start:nxt_c.start()]
                    # Drop the bottom-info block (contains "Search for more
                    # papers by this author" link)
                    chunk = re.sub(
                        r'<div\s+class=["\']?bottom-info["\']?.*?</div>',
                        "", chunk, flags=re.DOTALL,
                    )
                    text = strip_tags(chunk)
                    text = re.sub(r"\s+", " ", text).strip()
                    # Trim leading "author-type" artifact and "Search for"
                    text = re.sub(
                        r"^(?:Corresponding author.*?\.)?\s*", "", text,
                    )
                    text = text.rstrip(";").rstrip(".").strip()
                    affiliations.append(text)
                    pos = nxt_c.end()
                    break
                p = nxt_c.end()
        else:
            break

    authors = []
    for i, name in enumerate(names):
        aff = affiliations[i] if i < len(affiliations) else ""
        authors.append({
            "author": _format_display_name(name),
            "affiliation": [aff] if aff else [],
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {journal, volume, issue, year, title, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    molbiolcell references follow the pattern:
      <li class=references__item>
        <span class=references__note>
          Authors (<span class=references__year>YYYY</span>). Title.
          <span class=references__source><strong>Journal.</strong></span>
          <i>Vol</i>, fpage-lpage. [links]
        </span>
      </li>
    """
    refs = []
    # molbiolcell HTML embeds the reference list twice (one inline under
    # the article body, another inside a tabbed "References" panel). Scope
    # to the first <ul class=references> and parse only that list.
    ul_m = re.search(
        r'<ul[^>]*class=["\']?(?:[^"\'>]*\s)?references(?:\s[^"\'>]*)?["\']?[^>]*>',
        html,
    )
    if not ul_m:
        return refs
    ul_start = ul_m.end()
    close_m = re.search(r"</ul>", html[ul_start:])
    ul_end = ul_start + (close_m.start() if close_m else 0)
    list_html = html[ul_start:ul_end]

    li_pat = re.compile(
        r'<li[^>]*class=["\']?references__item["\']?[^>]*>', re.IGNORECASE,
    )
    li_starts = [m.start() for m in li_pat.finditer(list_html)]
    if not li_starts:
        return refs
    li_starts.append(len(list_html))

    for i in range(len(li_starts) - 1):
        entry = list_html[li_starts[i]:li_starts[i + 1]]
        # Strip numeric label prefix (older MBC papers wrap the list
        # number in <span class=label>N </span>).
        entry = re.sub(
            r'<span\s+class=["\']?label["\']?[^>]*>[^<]*</span>',
            "", entry,
        )

        # Extract structured pieces
        year = ""
        ym = re.search(
            r'<span\s+class=["\']?references__year["\']?[^>]*>([^<]+)</span>',
            entry,
        )
        if ym:
            year = re.search(r"\d{4}", ym.group(1)).group(0) if re.search(r"\d{4}", ym.group(1)) else ""

        journal = ""
        jm = re.search(
            r'<span\s+class=["\']?references__source["\']?[^>]*>'
            r'(?:<strong>)?(.*?)(?:</strong>)?</span>',
            entry, re.DOTALL,
        )
        if jm:
            journal = strip_tags(jm.group(1)).strip().rstrip(".").rstrip(",")

        # DOI from Crossref linkout. URL params are HTML-entity encoded
        # (&amp;); match dbid=16 (Crossref) and extract the key= value.
        doi = ""
        for lm2 in re.finditer(
            r'href=["\']?(https?://[^"\'>\s]*servlet/linkout[^"\'>\s]*)',
            entry,
        ):
            url = unescape(lm2.group(1))
            if "dbid=16" not in url:
                continue
            km = re.search(r"[?&]key=([^&\s\"'>]+)", url)
            if km:
                candidate = km.group(1).replace("%2F", "/")
                if candidate.startswith("10."):
                    doi = format_doi(candidate)
                    break
        if not doi:
            dm2 = re.search(
                r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry,
            )
            if dm2:
                doi = format_doi(unescape(dm2.group(1)))

        # Strip the trailing link block (Crossref/Medline/Google Scholar)
        # and the references__suffix span to isolate the citation text.
        cite = re.sub(
            r'<span\s+class=["\']?references__suffix["\']?.*?</span>',
            "", entry, flags=re.DOTALL,
        )
        cite = re.sub(
            r"<a\b[^>]*>.*?</a>", "", cite, flags=re.DOTALL,
        )
        plain = strip_tags(cite).strip()
        plain = re.sub(r"\s+", " ", plain)

        # Prefer structured fields when present (older MBC papers wrap
        # authors/title in <span class=references__authors / __article-title>).
        authors = []
        title = ""
        vol_pages = ""
        sa = re.search(
            r'<span\s+class=["\']?references__authors["\']?[^>]*>(.*?)</span>',
            entry, re.DOTALL,
        )
        st = re.search(
            r'<span\s+class=["\']?references__article-title["\']?[^>]*>'
            r'(.*?)</span>',
            entry, re.DOTALL,
        )
        if sa and st:
            author_text = strip_tags(sa.group(1)).strip().rstrip(",").rstrip(";")
            author_text = re.sub(r"\s+", " ", author_text)
            title = strip_tags(st.group(1)).strip().rstrip(".")
            # Parse authors: "Surname I.N., Surname I.N., ..."
            # Older MBC uses no comma between surname and initials.
            tokens = re.split(r",\s*", author_text)
            for t in tokens:
                t = t.strip().rstrip(".")
                if not t or t.lower().startswith("et al"):
                    continue
                mm = re.match(r"^(.+?)\s+([A-Z]\.?(?:\s*[A-Z]\.?)*)$", t)
                if mm:
                    surname = mm.group(1).strip()
                    initials = re.sub(r"[.\s]", "", mm.group(2))
                    authors.append(f"{surname} {initials}")
                else:
                    authors.append(t)
            # vol_pages from plain text after the journal
            if journal:
                jpos = plain.find(journal)
                if jpos >= 0:
                    vol_pages = plain[jpos + len(journal):].strip()

        if not authors:
            # Newer MBC papers lack the structured spans; parse from plain
            # text. Authors segment ends at "(YYYY)."; title follows.
            sm = re.search(r"\((\d{4}[a-z]?)\)\.\s*", plain)
            if sm:
                author_str = plain[:sm.start()].strip().rstrip(",").rstrip(";")
                rest = plain[sm.end():].strip()
                if journal:
                    jpos = rest.find(journal)
                    if jpos > 0:
                        title = rest[:jpos].strip().rstrip(".").strip()
                        vol_pages = rest[jpos + len(journal):].strip()
                    else:
                        title = rest
                else:
                    title = rest
                author_str = re.sub(r"\band\b", ",", author_str)
                tokens = [t.strip() for t in author_str.split(",") if t.strip()]
                initials_re = re.compile(r"^[A-Z]\.?(?:\s*[A-Z]\.?)*\.?$")
                i = 0
                while i < len(tokens):
                    s = tokens[i]
                    if s.lower().startswith("et al"):
                        i += 1
                        continue
                    if i + 1 < len(tokens) and initials_re.match(tokens[i + 1]):
                        initials = re.sub(r"[.\s]", "", tokens[i + 1])
                        authors.append(f"{s} {initials}" if initials else s)
                        i += 2
                    else:
                        authors.append(s)
                        i += 1
            else:
                title = plain.rstrip(".")

        # Parse volume/pages from vol_pages: "Vol, fpage-lpage." or variants.
        # vol_pages typically starts with stray punctuation after the journal
        # name was sliced out (", 22, 3474-3487" or "., 22, 3474-3487.").
        volume = ""
        issue = ""
        pages = ""
        vp = re.sub(r"\s+", " ", vol_pages).strip()
        vp = vp.lstrip(".,; ").rstrip(".,; ")
        vm = re.match(
            r"(\d+)\s*(?:\(([^)]+)\))?\s*,\s*(\w[\w\-\u2013]*?)\s*$", vp,
        )
        if vm:
            volume = vm.group(1)
            issue = (vm.group(2) or "").strip()
            pages = re.sub(r"[\u2013\u2014]", "-", vm.group(3))

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

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    molbiolcell-specific: full-text lives inside <div class=hlFld-Fulltext>.
    Section titles are <h2 class="article-section__title ...">; iterate
    sequential sections, stop at References.
    """
    parts = []

    # Abstract: <div class="abstractSection abstractInFull"><p>...</p></div>
    am = re.search(
        r'<div[^>]*class=["\']?[^"\'>]*abstractSection[^"\'>]*["\']?[^>]*>'
        r"(.*?)</div>",
        html, re.DOTALL,
    )
    if am:
        abs_html = am.group(1)
        abs_html = extract_captions(abs_html)
        abs_html = strip_common(abs_html)
        abs_text = tags_to_text(abs_html).strip()
        abs_text = drop_noise(abs_text, _NOISE)
        if abs_text:
            parts.append(f"## Abstract\n\n{abs_text}")

    fm = re.search(
        r'<div[^>]*class=["\']?[^"\'>]*hlFld-Fulltext[^"\'>]*["\']?[^>]*>',
        html,
    )
    if not fm:
        return "\n\n".join(parts).strip()
    body_start = fm.end()
    # Bound at whichever comes first: a REFERENCES heading, or the first
    # <ul class=references> (older MBC papers put the reference list
    # directly after Acknowledgments with no intervening h2).
    candidates = []
    ref_h2 = re.search(
        r'<h2[^>]*>\s*REFERENCES\s*</h2>', html[body_start:], re.IGNORECASE,
    )
    if ref_h2:
        candidates.append(ref_h2.start())
    ref_ul = re.search(
        r'<ul[^>]*class=["\']?(?:[^"\'>]*\s)?references(?:\s[^"\'>]*)?["\']?',
        html[body_start:],
    )
    if ref_ul:
        candidates.append(ref_ul.start())
    body_end = body_start + (min(candidates) if candidates else len(html) - body_start)
    body_html = html[body_start:body_end]

    # Strip "Previous/Next section" navigation links and the citedByEntry/
    # related list if present.
    body_html = re.sub(
        r"<a[^>]*>\s*(?:Previous|Next)\s+section\s*</a>",
        "", body_html, flags=re.IGNORECASE,
    )

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    body_text = tags_to_text(body_html)
    body_text = drop_noise(body_text, _NOISE)
    if body_text.strip():
        parts.append(body_text.strip())
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse molbiolcell HTML into a refs.json-format dict plus main_text."""
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
