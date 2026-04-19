"""Frontiers Media (frontiersin.org) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    format_name,
    get_all_meta,
    get_meta,
    parse_meta_authors,
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Download Figure",
    "Expand Figure",
    "Expand Table",
    "Download Table",
    "Open lightbox",
    "CrossRef Full Text",
    "Google Scholar",
    "PubMed Abstract",
    "CrossRef",
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
    """Remove floating banners, cookie consent dialogs, and overlays.

    - onetrust-consent-sdk: OneTrust cookie banner ("We use cookies /
      Our website uses cookies...") and its dark overlay.
    - <nav class=Ibar>: floating top bar (Frontiers logo, menu
      hamburger "Open Menu", Search, Login).
    """
    html = remove_elements_by_id(html, "onetrust-consent-sdk")
    html = _remove_nested_element(html, r"<nav class=Ibar\b[^>]*>")
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

    # Frontiers sometimes stores an internal asset ID in citation_firstpage
    # instead of the article's elocation ID. When no lastpage is present and
    # firstpage disagrees with the DOI's trailing segment, prefer the DOI
    # segment (the true elocation ID).
    doi = get_meta(html, "citation_doi")
    if firstpage and not lastpage and doi:
        doi_seg = doi.rsplit(".", 1)[-1]
        if doi_seg and doi_seg.isdigit() and doi_seg.lstrip("0") != firstpage:
            pages = doi_seg.lstrip("0") or doi_seg

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(
        html, "citation_journal_title"
    )
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

def _parse_affiliation_map(html):
    """Parse <ul class=AffiliationList__list> into {number: text}.

    Single-affiliation papers omit the leading <span>N.</span> marker; in
    that case the single affiliation is keyed as "1".
    """
    aff_map = {}
    m = re.search(
        r'<ul[^>]*class="?AffiliationList__list[^>]*>',
        html,
    )
    if not m:
        return aff_map
    # Slice to </ul>
    pos = m.end()
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
    block = html[m.end():end]

    # Each <li class=AffiliationList__item>. Tags are unclosed.
    li_starts = [
        lm.start() for lm in re.finditer(
            r'<li[^>]*class="?AffiliationList__item', block,
        )
    ]
    for i, start in enumerate(li_starts):
        stop = li_starts[i + 1] if i + 1 < len(li_starts) else len(block)
        item = block[start:stop]
        sm = re.search(r"<span[^>]*>(.*?)</span>", item, re.DOTALL)
        if sm:
            num = unescape(strip_tags(sm.group(1))).strip().rstrip(".,")
            rest = item[sm.end():]
        else:
            num = str(i + 1)  # no numeric prefix => single-affiliation case
            rest = item
        text = unescape(strip_tags(rest)).strip().rstrip(",. ")
        text = re.sub(r"\s+", " ", text)
        # Drop "See more" button text that follows the last affiliation
        text = re.sub(r"\s*See more\s*$", "", text)
        if num and text:
            aff_map[num] = text
    return aff_map


def _parse_people_sups(html):
    """Return {full_name_text: [sup_number, ...]} from the PeopleList."""
    out = {}
    plist_m = re.search(
        r'<div[^>]*class="?PeopleList\b', html,
    )
    if not plist_m:
        return out
    aff_m = re.search(
        r'<div[^>]*class="[^"]*AffiliationList', html,
    )
    end = aff_m.start() if aff_m else len(html)
    block = html[plist_m.end():end]

    for m in re.finditer(
        r'<p[^>]*class="[^"]*PeopleListItem__name[^"]*"[^>]*>(.*?)</p>',
        block, re.DOTALL,
    ):
        inner = m.group(1)
        name_text = re.sub(r"<sup.*?</sup>", "", inner, flags=re.DOTALL)
        name_text = re.sub(r"\s+", " ", unescape(strip_tags(name_text))).strip()
        sups = []
        for sm in re.finditer(r"<sup[^>]*>(.*?)</sup>", inner, re.DOTALL):
            for tok in re.findall(
                r"\d+", unescape(strip_tags(sm.group(1)))
            ):
                if tok not in sups:
                    sups.append(tok)
        if name_text:
            out[name_text] = sups
    return out


def _parse_authors(html):
    """Extract authors with affiliations.

    Uses citation_author meta tags (comma-separated "Last, First ") for the
    ordered list of authors and formatted names. Affiliations are linked
    via numeric superscripts in PeopleList back to the AffiliationList.
    citation_author_institution values are used as fallback when the
    PeopleList sup-to-affiliation mapping fails.
    """
    meta_authors = parse_meta_authors(html)
    aff_map = _parse_affiliation_map(html)
    people_sups = _parse_people_sups(html)

    def _norm(s):
        return re.sub(r"\s+", " ", s.replace(",", " ")).strip().lower()

    sups_by_norm = {_norm(k): v for k, v in people_sups.items()}

    # If every author lacks a sup AND there is exactly one affiliation,
    # apply it to all authors.
    all_single = (
        aff_map
        and len(aff_map) == 1
        and all(not v for v in people_sups.values())
    )
    default_single = list(aff_map.values())[0] if all_single else None

    authors = []
    for a in meta_authors:
        name = format_author_name(a["name"])
        raw = a["name"].strip().rstrip(", ")
        if "," in raw:
            last, given = raw.split(",", 1)
            people_form = f"{given.strip()} {last.strip()}"
        else:
            people_form = raw
        sups = sups_by_norm.get(_norm(people_form))
        if sups and aff_map:
            affs = [aff_map[s] for s in sups if s in aff_map]
        elif default_single:
            affs = [default_single]
        else:
            affs = a.get("affiliations", [])
        authors.append({"author": name, "affiliation": affs})
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_ref_entry(entry_html):
    """Parse a single <li class=References__item>."""
    # Authors
    authors = []
    for nm in re.finditer(
        r'<span[^>]*class="?References__surname"?[^>]*>(.*?)</span>\s*'
        r'<span[^>]*class="?References__givenNames"?[^>]*>(.*?)</span>',
        entry_html, re.DOTALL,
    ):
        surname = unescape(strip_tags(nm.group(1))).strip().rstrip(",. ")
        given = unescape(strip_tags(nm.group(2))).strip().rstrip(",. ")
        if surname:
            authors.append(format_name(given, surname))

    # Journal
    journal = ""
    jm = re.search(
        r'<i[^>]*class="?References__source"?[^>]*>(.*?)</i>',
        entry_html, re.DOTALL,
    )
    if jm:
        journal = unescape(strip_tags(jm.group(1))).strip().rstrip(".,")

    # DOI from CrossRef link
    doi = ""
    dm = re.search(
        r'href=["\']?(https?://(?:dx\.)?doi\.org/[^"\'\s>]+)',
        entry_html,
    )
    if dm:
        doi = format_doi(
            unescape(dm.group(1)).replace("dx.doi.org", "doi.org")
        )

    # Tail text after the personGroup closing </span>. Strip all tags.
    pg_m = re.search(
        r'<span[^>]*class="?References__personGroup', entry_html,
    )
    tail_start = 0
    if pg_m:
        # Find matching </span> for the personGroup by depth walking
        tag_end = entry_html.find(">", pg_m.end() - 1)
        pos = tag_end + 1
        depth = 1
        while depth > 0 and pos < len(entry_html):
            no = re.search(r"<span[\s>]", entry_html[pos:])
            nc = re.search(r"</span>", entry_html[pos:])
            if nc is None:
                break
            if no and no.start() < nc.start():
                depth += 1
                pos += no.end()
            else:
                depth -= 1
                if depth == 0:
                    tail_start = pos + nc.end()
                    break
                pos += nc.end()
    tail_html = entry_html[tail_start:]
    # Strip the links list from tail
    tail_html = re.sub(
        r'<ul[^>]*class="[^"]*References__links[^"]*"[^>]*>.*',
        '', tail_html, flags=re.DOTALL,
    )
    tail_text = re.sub(r"\s+", " ", unescape(strip_tags(tail_html))).strip()

    # Year: first (YYYY) at start. Optional leading punctuation (".", ",")
    # tolerates tail like ". (2017). Title..." that arises when an "et al."
    # span sits inside the personGroup and the HTML emits a stray period
    # immediately after the personGroup closes.
    year = ""
    ym = re.match(r"\s*[.,]?\s*\(\s*(\d{4})[a-z]?\s*\)", tail_text)
    if ym:
        year = ym.group(1)
        tail_text = tail_text[ym.end():].strip(" .,")

    # Title: up to journal name
    title = ""
    volume = issue = pages = ""
    if journal and journal in tail_text:
        idx = tail_text.find(journal)
        title = tail_text[:idx].strip().rstrip(".,")
        after = tail_text[idx + len(journal):].strip(" ,.")
        vm = re.match(
            r"(\d+)\s*(?:\(([^)]+)\))?\s*(?:,\s*([A-Za-z0-9\-\u2013]+))?",
            after,
        )
        if vm:
            volume = vm.group(1)
            if vm.group(2):
                issue = vm.group(2).strip()
            if vm.group(3):
                pages = vm.group(3).replace("\u2013", "-")
    else:
        # Title up to first period+space
        tm = re.match(r"(.+?)\.\s+", tail_text)
        if tm:
            title = tm.group(1).strip()

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "authors": authors,
    }


def _parse_references(html):
    """Extract references from <ul class=References>."""
    refs = []
    for ul_m in re.finditer(
        r'<ul[^>]*class="?References\b[^>]*>', html,
    ):
        # Skip the References__links ul that appears inside each entry;
        # only match the top-level References wrapper by requiring the
        # class to be exactly "References" (no "__" suffix).
        cls_m = re.search(r'class=("?)([^\s>"]+)', ul_m.group(0))
        if not cls_m:
            continue
        cls = cls_m.group(2)
        if cls != "References":
            continue

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
                r'<li[^>]*class="?References__item', section,
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

def _slice_article_content(html):
    """Return HTML inside <div class=ArticleContent>."""
    m = re.search(
        r'<div[^>]*class=(?:"ArticleContent"|ArticleContent\b)[^>]*>', html,
    )
    if not m:
        return ""
    pos = m.end()
    depth = 1
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
                return html[m.end():pos + nc.start()]
            pos += nc.end()
    return html[m.end():pos]


def _remove_references_section(body_html):
    """Remove the <div id=hN><h2>References</h2>...</div> block."""
    m = re.search(
        r'<div[^>]*id="?h\d+"?[^>]*>\s*<h2[^>]*>\s*References\s*</h2>',
        body_html, re.IGNORECASE,
    )
    if not m:
        return body_html
    pos = m.end()
    depth = 1
    end = len(body_html)
    while depth > 0 and pos < len(body_html):
        no = re.search(r"<div[\s>]", body_html[pos:])
        nc = re.search(r"</div>", body_html[pos:])
        if nc is None:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                end = pos + nc.end()
                break
            pos += nc.end()
    return body_html[:m.start()] + body_html[end:]


def _remove_summary_block(body_html):
    """Remove <div class=Summary>...</div> (citation + keywords recap)."""
    m = re.search(
        r'<div[^>]*class="?Summary\b[^>]*>', body_html,
    )
    if not m:
        return body_html
    pos = m.end()
    depth = 1
    end = len(body_html)
    while depth > 0 and pos < len(body_html):
        no = re.search(r"<div[\s>]", body_html[pos:])
        nc = re.search(r"</div>", body_html[pos:])
        if nc is None:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                end = pos + nc.end()
                break
            pos += nc.end()
    return body_html[:m.start()] + body_html[end:]


def _replace_article_figures(body_html):
    """Replace ArticleFigure blocks with title + caption text."""
    def _walk(html, start):
        pos = start
        depth = 1
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
                    return pos + nc.end(), html[start:pos + nc.start()]
                pos += nc.end()
        return pos, html[start:pos]

    out = []
    cursor = 0
    while True:
        fm = re.search(
            r'<div[^>]*class="?ArticleFigure\b[^>]*>',
            body_html[cursor:],
        )
        if not fm:
            break
        abs_start = cursor + fm.start()
        content_start = cursor + fm.end()
        end_pos, inner = _walk(body_html, content_start)
        out.append(body_html[cursor:abs_start])

        title = ""
        tm = re.search(
            r'<p[^>]*class="?ArticleFigure__title"?[^>]*>(.*?)</p>',
            inner, re.DOTALL,
        )
        if tm:
            title = strip_tags(tm.group(1)).strip()
        cap = ""
        cm = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", inner, re.DOTALL)
        if cm:
            cap = strip_tags(cm.group(1)).strip()
        out.append("\n\n" + (title + ". " if title else "") + cap + "\n\n")
        cursor = end_pos

    out.append(body_html[cursor:])
    return "".join(out)


def _replace_article_tables(body_html):
    """Replace ArticleTable blocks with title + caption + table text."""
    def _walk(html, start):
        pos = start
        depth = 1
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
                    return pos + nc.end(), html[start:pos + nc.start()]
                pos += nc.end()
        return pos, html[start:pos]

    out = []
    cursor = 0
    while True:
        tm = re.search(
            r'<div[^>]*class="?ArticleTable\b[^>]*>',
            body_html[cursor:],
        )
        if not tm:
            break
        abs_start = cursor + tm.start()
        content_start = cursor + tm.end()
        end_pos, inner = _walk(body_html, content_start)
        out.append(body_html[cursor:abs_start])

        title = ""
        lm = re.search(
            r'<p[^>]*class="?ArticleTable__title"?[^>]*>(.*?)</p>',
            inner, re.DOTALL,
        )
        if lm:
            title = strip_tags(lm.group(1)).strip()
        desc = ""
        dm = re.search(
            r'<div[^>]*class="?ArticleTable__description"?[^>]*>(.*?)</div>',
            inner, re.DOTALL,
        )
        if dm:
            desc = strip_tags(dm.group(1)).strip()
        table_m = re.search(r"<table[^>]*>.*?</table>", inner, re.DOTALL)
        table_html = table_m.group(0) if table_m else ""
        out.append(
            "\n\n"
            + (title + ". " if title else "")
            + (desc + "\n" if desc else "")
            + table_html
            + "\n\n"
        )
        cursor = end_pos

    out.append(body_html[cursor:])
    return "".join(out)


def _parse_main_text(html):
    """Extract body text from ArticleContent, excluding references and Summary."""
    body_html = _slice_article_content(html)
    if not body_html:
        return ""

    body_html = _remove_references_section(body_html)
    body_html = _remove_summary_block(body_html)
    # Drop the standalone <h2>Statements</h2> grouping header; its subsections
    # already have their own <h3> titles (Author contributions, Funding, etc.).
    body_html = re.sub(
        r"<h2[^>]*>\s*Statements\s*</h2>", "", body_html, flags=re.IGNORECASE,
    )
    body_html = _replace_article_figures(body_html)
    body_html = _replace_article_tables(body_html)

    # Remove interactive buttons (download/expand)
    body_html = re.sub(
        r"<button[^>]*>.*?</button>", "", body_html, flags=re.DOTALL,
    )
    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Frontiers HTML into a papers/*.json-format dict."""
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
