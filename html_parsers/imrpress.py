"""IMR Press (imrpress.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_all_meta,
    get_meta,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Cited within:",
    "Google Scholar",
    "Crossref",
    "PubMed",
)

# Reference section title pattern
_REF_RE = re.compile(r"\breferences\b", re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r"supplement|extended data|source data|expanded view|powerpoint|appendix",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Return html unmodified; no visually impairing elements per user."""
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract metadata from citation_* meta tags."""
    date = get_meta(html, "citation_publication_date") or get_meta(
        html, "citation_online_date"
    )
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(
        html, "citation_journal_title"
    )
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

def _parse_affiliation_map(html):
    """Parse <div class=affilications-container> (typo is literal in markup)
    into {number_str: affiliation_text}.

    Uses nesting-aware <div> matching to find the container's closing tag
    since the inner `<div class="note-text rich-text">` confuses naive
    regex-based slicing.
    """
    aff_map = {}
    m = re.search(
        r'<div[^>]*class="?affilications-container[^>]*>', html,
    )
    if not m:
        return aff_map
    pos = m.end()
    depth = 1
    end = len(html)
    while depth > 0 and pos < len(html):
        no = re.search(r"<div[\s>]", html[pos:])
        nc = re.search(r"</div>", html[pos:])
        if nc is None:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                end = pos + nc.start()
                break
            pos += nc.end()
    block = html[m.end():end]

    # Split on <p ...> markers since IMR Press omits </p> closing tags.
    p_starts = [pm.start() for pm in re.finditer(r"<p[^>]*>", block)]
    for i, pstart in enumerate(p_starts):
        pend = p_starts[i + 1] if i + 1 < len(p_starts) else len(block)
        inner = block[pstart:pend]
        # Strip inner note-text div
        inner = re.sub(
            r'<div[^>]*class="[^"]*note-text[^"]*"[^>]*>.*?</div>',
            "", inner, flags=re.DOTALL,
        )
        sup_m = re.search(r"<sup[^>]*>(.*?)</sup>", inner, re.DOTALL)
        if not sup_m:
            continue
        num = unescape(strip_tags(sup_m.group(1))).strip().rstrip(",.")
        rest = inner[sup_m.end():]
        text = unescape(strip_tags(rest)).strip().rstrip(",. ")
        text = re.sub(r"\s+", " ", text)
        if num and text:
            aff_map[num] = text
    return aff_map


def _format_imrp_author(name):
    """Convert 'Given Middle Last' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Author names come from citation_author meta tags. Affiliation linking
    is done by matching numeric superscripts in <span class=name-item-text>
    against the affilications-container list.
    """
    meta_names = get_all_meta(html, "citation_author")
    aff_map = _parse_affiliation_map(html)

    def _norm(s):
        return re.sub(r"\s+", " ", re.sub(r"[.,*]", "", s)).strip().lower()

    # Per-author superscripts from DOM, indexed by normalized name
    author_sups = {}
    for am in re.finditer(
        r'<span[^>]*class="?name-item-text"?[^>]*>(.*?)</span>',
        html, re.DOTALL,
    ):
        inner = am.group(1)
        name_text = re.sub(r"<sup.*?</sup>", "", inner, flags=re.DOTALL)
        name_text = unescape(strip_tags(name_text)).strip()
        sups = []
        for sm in re.finditer(r"<sup[^>]*>(.*?)</sup>", inner, re.DOTALL):
            for tok in re.findall(
                r"\d+", unescape(strip_tags(sm.group(1)))
            ):
                if tok not in sups:
                    sups.append(tok)
        if name_text:
            author_sups[_norm(name_text)] = sups

    authors = []
    for n in meta_names:
        raw = n.strip().rstrip(",* ")
        name = _format_imrp_author(raw)
        sups = author_sups.get(_norm(raw))
        if sups and aff_map:
            affs = [aff_map[s] for s in sups if s in aff_map]
        else:
            affs = []
        authors.append({"author": name, "affiliation": affs})
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_ref_entry(entry_html):
    """Parse a single <li class=article-references-item>."""
    # Main text inside <span class=rich-text>
    tm = re.search(
        r'<span[^>]*class="?rich-text"?[^>]*>(.*?)</span>',
        entry_html, re.DOTALL,
    )
    text_html = tm.group(1) if tm else entry_html

    # Journal inside <i>
    journal = ""
    jm = re.search(r"<i[^>]*>(.*?)</i>", text_html, re.DOTALL)
    if jm:
        journal = unescape(strip_tags(jm.group(1))).strip().rstrip(".,")

    # Plain text version of citation
    text = unescape(strip_tags(text_html)).strip()
    text = re.sub(r"\s+", " ", text)

    # Year: last (YYYY) in text
    year = ""
    ys = re.findall(r"\((\d{4})\)", text)
    if ys:
        year = ys[-1]

    # DOI from Crossref link
    doi = ""
    dm = re.search(
        r'href=["\']?(https?://(?:dx\.)?doi\.org/[^"\'\s>]+)',
        entry_html,
    )
    if dm:
        doi = format_doi(
            unescape(dm.group(1)).replace("dx.doi.org", "doi.org")
        )

    # Authors: text before ": " (end of author/title delimiter is ": ")
    authors = []
    title = ""
    colon_idx = text.find(": ")
    rest = text
    if colon_idx > 0:
        auth_block = text[:colon_idx]
        rest = text[colon_idx + 2:]
        # Split by ", " and " and "
        parts = re.split(r",\s*|\s+and\s+", auth_block)
        for p in parts:
            p = p.strip()
            if p and p.lower() != "et al.":
                authors.append(_format_imrp_author(p))

    # Title: up to the next period (journal italics come after title).
    if journal and journal in rest:
        title_part = rest.split(journal, 1)[0].strip().rstrip(".,")
        title = title_part
        tail = rest.split(journal, 1)[1].strip(" ,.")
    else:
        tail = ""
        tm2 = re.match(r"(.+?)\.\s+", rest)
        title = tm2.group(1).strip() if tm2 else rest.split("(")[0].strip()

    # Volume (issue), pages after the journal
    volume = issue = pages = ""
    if journal:
        # Volume with optional issue and optional pages
        vm = re.search(
            r"(\d+)\s*(?:\((\d+)\))?(?:\s*,\s*([A-Za-z0-9\-\u2013]+))?",
            tail,
        )
        if vm:
            volume = vm.group(1)
            if vm.group(2):
                issue = vm.group(2)
            if vm.group(3):
                pages = vm.group(3).replace("\u2013", "-")

    return {
        "title": title,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "year": year,
        "pages": pages,
        "doi": doi,
        "authors": [a for a in authors if a],
    }


def _parse_references(html):
    """Extract references from <ul class=article-references-list>."""
    refs = []
    for ul_m in re.finditer(
        r'<ul[^>]*class="?article-references-list"?[^>]*>', html,
    ):
        pos = ul_m.end()
        depth = 1
        end = len(html)
        while depth > 0 and pos < len(html):
            no = re.search(r"<ul[\s>]", html[pos:])
            nc = re.search(r"</ul>", html[pos:])
            if nc is None:
                break
            if no and no.start() < nc.start():
                depth += 1
                pos += no.end()
            else:
                depth -= 1
                if depth == 0:
                    end = pos + nc.start()
                    break
                pos += nc.end()
        section = html[ul_m.end():end]

        li_starts = [
            m.start() for m in re.finditer(
                r'<li[^>]*class="?article-references-item', section,
            )
        ]
        for i, start in enumerate(li_starts):
            stop = li_starts[i + 1] if i + 1 < len(li_starts) else len(section)
            entry = section[start:stop]
            refs.append({"": _parse_ref_entry(entry)})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _slice_article(html, article_class):
    """Return content of <article class="article <article_class>">."""
    m = re.search(
        r'<article[^>]*class="[^"]*article-' + article_class + r'[^"]*"[^>]*>',
        html,
    )
    if not m:
        return ""
    pos = m.end()
    depth = 1
    while depth > 0 and pos < len(html):
        no = re.search(r"<article[\s>]", html[pos:])
        nc = re.search(r"</article>", html[pos:])
        if nc is None:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                return html[m.end():pos + nc.start()]
            pos += nc.end()
    return html[m.end():pos]


def _parse_main_text(html):
    """Assemble main_text from abstract + keywords + article content.

    IMR Press renders structural sections into separate <article> wrappers:
      - article-abstract
      - article-keywords
      - article-content (body; may be empty for TOC-only landing pages)
    """
    parts = []
    abstract_html = _slice_article(html, "abstract")
    if abstract_html:
        # Drop the inline "Abstract" heading that lives inside the block
        abstract_html = re.sub(
            r'<h[2-4][^>]*>\s*Abstract\s*</h[2-4]>', '',
            abstract_html, flags=re.DOTALL | re.IGNORECASE,
        )
        text = tags_to_text(strip_common(abstract_html))
        text = re.sub(r"^\s*Abstract\s*", "", text).strip()
        if text:
            parts.append("## Abstract\n" + text)

    kw_html = _slice_article(html, "keywords")
    if kw_html:
        kws = []
        for li in re.finditer(r"<li[^>]*>(.*?)</li>", kw_html, re.DOTALL):
            kw = unescape(strip_tags(li.group(1))).strip()
            if kw:
                kws.append(kw)
        if kws:
            parts.append("## Keywords\n" + ", ".join(kws))

    body_html = _slice_article(html, "content")
    if body_html:
        body_html = extract_captions(body_html)
        body_html = strip_common(body_html)
        body_html = re.sub(
            r"<button[^>]*>.*?</button>", "", body_html, flags=re.DOTALL,
        )
        text = tags_to_text(body_html)
        if text.strip():
            parts.append(text)

    return drop_noise("\n\n".join(parts), _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse IMR Press HTML into a refs.json-format dict plus main_text."""
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
