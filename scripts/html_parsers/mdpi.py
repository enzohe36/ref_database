"""MDPI (mdpi.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    neutralize_media_queries,
    parse_meta_authors,
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Phase 2 layout override. See remove_banners.
_OVERRIDE_CSS = """<style>
:root, html, body { background: white !important; }
body {
    max-width: 752px !important;
    margin: 0 auto !important;
    padding: 0 !important;
    background: white !important;
}
:root article.bright {
    width: auto !important;
    max-width: 100% !important;
    margin: 0 !important;
    padding: 56px 16px !important;
    background: white !important;
    box-sizing: border-box !important;
    float: none !important;
    line-height: 1.5em !important;
    font-size: 110% !important;
}
/* Neutralize the MDPI two-column wrapper so the article column fills the
   body cap rather than sitting at large-9. */
:root .html-content__container,
:root .content__container,
:root .content__column,
:root .middle-column__main,
:root .main-section,
:root .page-highlight,
:root .abstract_div,
:root .row.full-width,
:root #container,
:root #main-content,
:root #middle-column,
html body #main-content .row-fixed-left-column #middle-column.large-9 {
    width: auto !important;
    max-width: 100% !important;
    float: none !important;
    margin: 0 !important;
    padding: 0 !important;
    background: white !important;
}
/* Hide the desktop-only left sidebar (Foundation .show-for-large-up which
   should hide on small/medium but the em-based @media gating slips past
   neutralize_media_queries). */
:root #left-column.show-for-large-up,
:root .show-for-large-up {
    display: none !important;
}
/* Site top-bar chrome ships with position:absolute and an explicit pixel
   width that overflows the body cap; constrain it to the body envelope. */
:root .top-bar.bright,
:root .html-profile-nav,
:root .html-profile-nav.bright,
:root .html-profile-nav.affix-top {
    position: static !important;
    width: 100% !important;
    max-width: 100% !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    box-sizing: border-box !important;
}
/* Figure layout: high-res image above caption, full column width. */
:root article.bright .html-fig-wrap,
:root article.bright .html-fig-wrap > *,
:root article.bright .html-fig-wrap .html-fig_img,
:root article.bright .html-fig-wrap .html-figpopup,
:root article.bright .html-fig-wrap .html-figpopup-link {
    display: block !important;
    width: 100% !important;
    max-width: 100% !important;
    text-align: left !important;
    box-sizing: border-box !important;
    float: none !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
}
:root article.bright .html-fig-wrap img,
:root article.bright .html-fig-wrap .html-fig_img img,
:root article.bright .html-fig-wrap .html-figpopup img {
    display: block !important;
    width: 100% !important;
    height: auto !important;
    max-width: 100% !important;
    margin: 0 0 5px 0 !important;
}
/* Caption sits beneath the image with a small gap. */
:root article.bright .html-fig-wrap .html-fig_description {
    display: block !important;
    width: 100% !important;
    max-width: 100% !important;
    margin-top: 5px !important;
    text-align: left !important;
}
</style>"""

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Google Scholar",
    "CrossRef",
    "PubMed",
    "Open in a new tab",
)

# Reference section heading pattern
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
    """Strip MDPI site chrome and lock article column to the 720-px native form."""
    html = neutralize_media_queries(html)
    # Cookie / consent CMP host (Usercentrics shadow-root host).
    html = remove_elements_by_id(html, "usercentrics-cmp-ui")
    # Sticky page-side navigation arrows (prev / next-article rails).
    html = remove_elements_by_id(
        html, "small_left", "small_right", "big_left", "big_right",
    )
    if "</head>" in html:
        html = html.replace("</head>", _OVERRIDE_CSS + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", _OVERRIDE_CSS + r"\1", html, count=1)
    return html

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO-ish abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    MDPI lacks citation_journal_abbrev; uses citation_journal_title (e.g.
    "Genes", "Molecules"). The "(Basel)" PubMed disambiguation suffix is
    not present in the HTML and is not synthesized here.
    """
    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")

    date = get_meta(html, "citation_publication_date") or get_meta(html, "citation_online_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    return {
        "title": get_meta(html, "citation_title"),
        "journal": journal.rstrip(".") if journal else "",
        "year": year,
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    MDPI has citation_author meta tags in "LastName, Given" form but
    lacks citation_author_institution tags; affiliations are in
    <div class=art-affiliations> keyed by numeric superscript, and each
    author's name chip carries a <sup>N,M,*</sup> listing their affiliation
    numbers. This function maps name->affiliation via those superscripts.
    """
    # Names from meta tags (preferred — already normalized)
    names = [
        format_author_name(a["name"])
        for a in parse_meta_authors(html)
    ]
    if not names:
        return []

    # Build affiliation number -> text map. Parse the art-affiliations
    # container first to scope the search; each affiliation entry has an
    # optional <sup>N</sup> label and an affiliation-name div. When a
    # paper has a single institution and no superscripts, fall back to
    # storing it under a synthesized key "1".
    aff_map = {}
    aff_order = []
    cm = re.search(
        r'<div[^>]*class=["\']?art-affiliations[^"\'>]*["\']?[^>]*>', html,
    )
    if cm:
        pos = cm.end()
        depth = 1
        div_open = re.compile(r"<div\b", re.IGNORECASE)
        div_close = re.compile(r"</div\s*>", re.IGNORECASE)
        while depth > 0 and pos < len(html):
            nxt_o = div_open.search(html, pos)
            nxt_c = div_close.search(html, pos)
            if nxt_c is None:
                break
            if nxt_o and nxt_o.start() < nxt_c.start():
                depth += 1
                pos = nxt_o.end()
            else:
                depth -= 1
                pos = nxt_c.start() if depth == 0 else nxt_c.end()
        aff_scope = html[cm.end():pos]
    else:
        aff_scope = html
    # Iterate individual affiliation divs. Walk them with depth-aware
    # matching since each contains two nested <div> children
    # (affiliation-item with <sup>, affiliation-name with text).
    aff_block_pat = re.compile(
        r'<div\s+class=["\']?affiliation["\']?[^>]*>', re.IGNORECASE,
    )
    div_open = re.compile(r"<div\b", re.IGNORECASE)
    div_close = re.compile(r"</div\s*>", re.IGNORECASE)
    bpos = 0
    while True:
        bm = aff_block_pat.search(aff_scope, bpos)
        if not bm:
            break
        pos = bm.end()
        depth = 1
        while depth > 0 and pos < len(aff_scope):
            nxt_o = div_open.search(aff_scope, pos)
            nxt_c = div_close.search(aff_scope, pos)
            if nxt_c is None:
                break
            if nxt_o and nxt_o.start() < nxt_c.start():
                depth += 1
                pos = nxt_o.end()
            else:
                depth -= 1
                if depth == 0:
                    chunk = aff_scope[bm.end():nxt_c.start()]
                    pos = nxt_c.end()
                    bpos = pos
                    break
                pos = nxt_c.end()
        else:
            bpos = pos
            continue
        sup_m = re.search(r"<sup>([^<]+)</sup>", chunk)
        key = sup_m.group(1).strip() if sup_m else ""
        name_m = re.search(
            r'<div\s+class=["\']?affiliation-name["\']?[^>]*>(.*?)</div>',
            chunk, re.DOTALL,
        )
        if not name_m:
            continue
        text = strip_tags(name_m.group(1)).strip().rstrip(".").rstrip(";")
        text = re.sub(r"\s+", " ", text)
        if not text or "correspondence" in text.lower():
            continue
        if not key or key == "*":
            key = str(len(aff_order) + 1)
        aff_map[key] = text
        aff_order.append(key)

    # Find the art-authors block and map each author's profile-card-drop
    # name chip to the following <sup>N,M,...</sup>. The chip carries the
    # display name in "Given LastName" order so we match by last-name token
    # against the meta-derived "LastName IN" form.
    authors_m = re.search(
        r'<div[^>]*class=["\']?art-authors[^"\'>]*["\']?[^>]*>(.*?)<div\s+class=["\']?art-affiliations',
        html, re.DOTALL,
    )
    chip_map = []  # list of (last_name, [aff_nums])
    if authors_m:
        block = authors_m.group(1)
        chips = re.findall(
            r'<div\s+class=["\']?profile-card-drop[^>]*>([^<]+)</div>'
            r'(.*?)'
            r'(?=<div\s+class=["\']?profile-card-drop|$)',
            block, re.DOTALL,
        )
        for name_raw, tail in chips:
            display = unescape(name_raw.strip())
            last = display.rsplit(" ", 1)[-1] if " " in display else display
            sup_m = re.search(r"<sup>([^<]+)</sup>", tail)
            nums = []
            if sup_m:
                nums = [
                    n.strip()
                    for n in re.split(r"[,\s]+", sup_m.group(1))
                    if n.strip() and n.strip() != "*"
                ]
            chip_map.append((last, nums))

    authors = []
    single_aff = [aff_map[k] for k in aff_order] if len(aff_map) == 1 else []
    for i, meta_author in enumerate(parse_meta_authors(html)):
        formatted = format_author_name(meta_author["name"])
        nums = []
        if i < len(chip_map):
            nums = chip_map[i][1]
        if not nums:
            surname = formatted.split()[0] if formatted else ""
            for last, ns in chip_map:
                if last == surname:
                    nums = ns
                    break
        affs = [aff_map[n] for n in nums if n in aff_map]
        # Fallback: single shared affiliation for papers without sup labels.
        if not affs and single_aff:
            affs = list(single_aff)
        authors.append({
            "author": formatted,
            "affiliation": affs,
        })
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
    MDPI references appear inside <ol class=html-xxx> after the References
    heading. Each <li id=BN-...> has the form:
      Authors. Title. <span class=html-italic>Journal.</span> <b>Year</b>,
      <span class=html-italic>Volume</span>, pages. [Google Scholar] [CrossRef] [PubMed]
    """
    refs = []
    hm = re.search(r'<h2[^>]*>\s*References\s*</h2>', html)
    if not hm:
        return refs

    after = html[hm.end():]
    # MDPI varies the ol class by paper: html-x, html-xx, html-xxx.
    ol_m = re.search(
        r'<ol[^>]*class=["\']?html-x{1,3}["\']?[^>]*>', after,
    )
    if not ol_m:
        return refs

    list_start = ol_m.end()
    # Bound the reference list at its closing </ol> so footer ol/li
    # elements are not pulled into the reference set.
    close_m = re.search(r"</ol>", after[list_start:])
    list_end = list_start + (close_m.start() if close_m else len(after) - list_start)
    list_html = after[list_start:list_end]
    # Each <li> has no explicit closing tag (HTML5 implicit close). Split
    # by the next <li start position; the last entry ends at list_end.
    li_positions = [m.start() for m in re.finditer(r"<li\b[^>]*>", list_html)]
    li_positions.append(len(list_html))

    for i in range(len(li_positions) - 1):
        entry_full = list_html[li_positions[i]:li_positions[i + 1]]
        # Strip the <li ...> opening tag
        entry = re.sub(r"^<li[^>]*>", "", entry_full, count=1)

        # DOI from CrossRef link
        doi = ""
        dm = re.search(
            r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)',
            entry,
        )
        if dm:
            doi = format_doi(unescape(dm.group(1)))

        # Drop the [Google Scholar] [CrossRef] [PubMed] link group
        # (everything from the first [<a ... class=google-scholar onward).
        citation = re.split(
            r'\[<a[^>]*class=["\']?google-scholar',
            entry, maxsplit=1,
        )[0]

        # Book-chapter layout: authors optional, chapter title, period,
        # "In <span class=html-italic>Book</span>; editor, Ed.; Publisher:
        # City, [Chapter N;] pp. X-Y."
        # No <b>Year</b> — year appears as a bare 4-digit token near the end.
        # Example entries:
        #   "Pearson's Correlation Coefficient. In <i>Encyclopedia of Public
        #    Health</i>; Kirch, W., Ed.; Springer: Dordrecht, 2008."
        #   "Kessler, M.; Zietlow, R.; Meyer, T.F. Stem cells in the
        #    reproductive system. In <i>Adults Stem Cell Niches</i>; ...,
        #    2014; Chapter 6; pp. 139-169."
        chap_m = re.search(
            r'\.\s+In\s+<span\s+class=["\']?html-italic["\']?\s*>'
            r'([^<]+)</span>',
            citation,
        )
        if chap_m and not re.search(r"<b>\s*\d{4}", citation):
            book = unescape(chap_m.group(1)).strip().rstrip(".").rstrip(",")
            before = citation[:chap_m.start()]
            after = citation[chap_m.end():]
            before_plain = re.sub(r"\s+", " ", strip_tags(before)).strip()
            after_plain = re.sub(r"\s+", " ", strip_tags(after)).strip()

            # Year from a bare 4-digit token in the publisher portion.
            year_chap = ""
            yc = re.search(r"\b(19|20)\d{2}\b", after_plain)
            if yc:
                year_chap = yc.group(0)

            pages_chap = ""
            pm = re.search(
                r"pp\.\s*([\w\d]+\s*[\-\u2013\u2014]\s*[\w\d]+)",
                after_plain,
            )
            if pm:
                pages_chap = re.sub(
                    r"[\u2010-\u2014]", "-", pm.group(1).replace(" ", "")
                )

            # Split authors and title: last ". " in before_plain is the
            # author/title boundary. If no period, the whole thing is the
            # title (editor-only book, no chapter authors).
            split_idx = before_plain.rfind(". ")
            if split_idx > 0:
                chap_authors_str = before_plain[:split_idx]
                chap_title = before_plain[split_idx + 2:].strip().rstrip(".")
            else:
                chap_authors_str = ""
                chap_title = before_plain.rstrip(".")

            chap_authors = []
            for part in re.split(r"\s*;\s*", chap_authors_str):
                part = part.strip().rstrip(",").strip()
                if part and not part.lower().startswith("et al"):
                    chap_authors.append(format_author_name(part))

            refs.append({"": {
                "title": chap_title,
                "journal": book,
                "year": year_chap,
                "volume": "",
                "issue": "",
                "pages": pages_chap,
                "doi": doi,
                "authors": chap_authors,
            }})
            continue

        # Journal from the first <span class=html-italic>
        journal = ""
        jm = re.search(
            r'<span\s+class=["\']?html-italic["\']?\s*>([^<]+)</span>',
            citation,
        )
        if jm:
            journal = unescape(jm.group(1)).strip().rstrip(".").rstrip(",")

        # Year from first <b>YYYY</b>
        year = ""
        ym = re.search(r"<b>\s*(\d{4})[a-z]?\s*</b>", citation)
        if ym:
            year = ym.group(1)

        # Volume from second <span class=html-italic> (after <b>Year</b>)
        volume = ""
        spans = re.findall(
            r'<span\s+class=["\']?html-italic["\']?\s*>([^<]+)</span>',
            citation,
        )
        if len(spans) >= 2:
            volume = unescape(spans[1]).strip().rstrip(",").rstrip(".")

        # Plain text version for pages extraction
        plain = strip_tags(citation).strip()
        plain = re.sub(r"\s+", " ", plain)

        # Pages: after <b>Year</b> comes ", [Volume, ]Pages." — take the
        # last comma-separated segment before the trailing period. Accept
        # plain ranges ("683-691"), prefixed article numbers ("e00226-17",
        # "eaax6366", "jcs234914"), Cell-style e-supplements
        # ("117-130.e6"), and dotted chapter numbering ("12.29.11-12.29.19").
        # If the last segment equals the already-captured volume, pages are
        # genuinely absent.
        pages = ""
        after_year = re.search(
            r"<b>\s*\d{4}[a-z]?\s*</b>\s*,\s*(.+?)\s*$",
            citation, re.DOTALL,
        )
        if after_year:
            tail_plain = strip_tags(after_year.group(1)).strip()
            tail_plain = re.sub(r"\s+", " ", tail_plain).rstrip(".").strip()
            segs = [s.strip() for s in tail_plain.split(",") if s.strip()]
            if len(segs) >= 2:
                candidate = segs[-1]
            elif len(segs) == 1 and segs[0] != volume:
                candidate = segs[0]
            else:
                candidate = ""
            if candidate:
                pages = re.sub(r"[\u2010-\u2014]", "-", candidate).strip(". ")

        # Title: text between the author list and the journal span.
        # Authors end with "." before the title. Use structure: after
        # the last "." that precedes the journal's italic span, the title
        # starts. Simpler: split by ". " before the <span class=html-italic>.
        title = ""
        if jm:
            before_journal = citation[:jm.start()]
            before_plain = strip_tags(before_journal).strip()
            before_plain = re.sub(r"\s+", " ", before_plain)
            # Authors end at "Surname, I.J.[; Surname, I.J.]*" — the title
            # is the substring after the LAST ". " in before_plain.
            split_idx = before_plain.rfind(". ")
            if split_idx > 0:
                author_str = before_plain[:split_idx]
                title = before_plain[split_idx + 2:].strip().rstrip(".")
            else:
                author_str = before_plain.rstrip(".")
        else:
            # No italic journal — treat whole entry as title
            author_str = ""
            title = plain

        # Issue: if pages extraction found "V(I), pages" pattern — look
        # in the raw text for "Vol, Issue" — but MDPI typically omits
        # issue from references.
        issue = ""
        im = re.search(
            r"</b>\s*,\s*<span[^>]*html-italic[^>]*>[^<]+</span>\s*,\s*"
            r"(\d+)\s*\(([^)]+)\)",
            citation,
        )
        if im:
            volume = im.group(1)
            issue = im.group(2).strip()

        # Authors: split author_str at "; " (MDPI uses semicolons) then
        # format each as "LastName IN".
        authors = []
        for part in re.split(r"\s*;\s*", author_str):
            part = part.strip().rstrip(",").strip()
            if not part:
                continue
            if part.lower().startswith("et al"):
                continue
            # MDPI uses "LastName, F.M." — format_author_name handles it
            authors.append(format_author_name(part))

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

def _extract_mdpi_figures(html):
    """Replace <div class=html-fig-wrap> blocks with plain-text captions.

    MDPI figures: <div class=html-fig-wrap id=...> contains an image wrapper
    and <div class=html-fig_description> holding "<b>Figure N.</b> <b>title</b>. body".
    Entities like &lt; are preserved (using tag-only stripping) so literal
    < in captions do not break downstream HTML processing.
    """
    def _strip_keep_entities(s):
        return re.sub(r"<[^>]+>", "", s)

    def repl(block):
        inner = block.group(1)
        dm = re.search(
            r'<div[^>]*class=["\']?html-fig_description[^"\'>]*["\']?[^>]*>'
            r"(.*?)</div>",
            inner, re.DOTALL,
        )
        if not dm:
            return ""
        text = _strip_keep_entities(dm.group(1)).strip()
        text = re.sub(r"\s+", " ", text)
        return "\n\n" + text + "\n\n"

    # html-fig-wrap opens; find matching close by depth-aware div walk
    out = []
    i = 0
    pat = re.compile(
        r'<div[^>]*class=["\']?html-fig-wrap[^"\'>]*["\']?[^>]*>',
    )
    while True:
        m = pat.search(html, i)
        if not m:
            out.append(html[i:])
            break
        out.append(html[i:m.start()])
        pos = m.end()
        depth = 1
        div_open = re.compile(r"<div\b", re.IGNORECASE)
        div_close = re.compile(r"</div\s*>", re.IGNORECASE)
        while depth > 0 and pos < len(html):
            nxt_o = div_open.search(html, pos)
            nxt_c = div_close.search(html, pos)
            if nxt_c is None:
                pos = len(html)
                break
            if nxt_o and nxt_o.start() < nxt_c.start():
                depth += 1
                pos = nxt_o.end()
            else:
                depth -= 1
                pos = nxt_c.end()
        block_html = html[m.end():pos - len("</div>")]
        # Construct a fake match and delegate
        class _M:
            def group(self_, n):
                return block_html
        out.append(repl(_M()))
        i = pos
    return "".join(out)


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    MDPI-specific: article body lives in <div class=html-body> which ends
    at the <h2>References</h2> heading. The Abstract lives outside of
    html-body in its own <div class=art-abstract>; prepend it here.
    Back-matter sections (Funding, Acknowledgments, etc.) sit between
    html-body and References and are included.
    """
    parts = []

    # Abstract: sourced from the <section class=html-abstract> nested
    # inside <div class="art-abstract ...">. The section already contains
    # its own <h2>Abstract</h2> heading which becomes "## Abstract" via
    # tags_to_text; no manual prefix is prepended here.
    abs_m = re.search(
        r'<section[^>]*class=["\']?html-abstract["\']?[^>]*>(.*?)</section>',
        html, re.DOTALL,
    )
    if abs_m:
        abs_html = abs_m.group(1)
        abs_html = extract_captions(abs_html)
        abs_html = strip_common(abs_html)
        text = tags_to_text(abs_html)
        text = drop_noise(text, _NOISE)
        if text.strip():
            if not text.lstrip().startswith("## "):
                text = f"## Abstract\n\n{text}"
            parts.append(text.strip())

    # Body: from <div class=html-body> to before <h2>References</h2>
    body_m = re.search(
        r'<div[^>]*class=["\']?html-body[^"\'>]*["\']?[^>]*>',
        html,
    )
    if body_m:
        # End at the next <h2>References</h2>
        start = body_m.end()
        ref_m = re.search(r"<h2[^>]*>\s*References\s*</h2>", html[start:])
        end = start + (ref_m.start() if ref_m else len(html) - start)
        body_html = html[start:end]
        body_html = _extract_mdpi_figures(body_html)
        body_html = extract_captions(body_html)
        body_html = strip_common(body_html)
        text = tags_to_text(body_html)
        text = drop_noise(text, _NOISE)
        if text.strip():
            parts.append(text.strip())

    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse MDPI HTML into a papers/*.json-format dict."""
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
