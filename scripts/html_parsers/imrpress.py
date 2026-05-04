"""IMR Press (imrpress.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_all_meta,
    get_meta,
    neutralize_media_queries,
    remove_elements_by_selector,
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

# MathJax <inline-formula> markup is emitted with the character rendered
# twice (MathJax_Preview + mjx-chtml) and the MathML script tag contains
# <math alttext="..."> with a '>' inside its attribute value — which
# confuses the regex-based tag stripper and leaves "role=presentation"
# junk in the output. Extract the primary <mi> character from the MathML
# script and replace the whole formula with it.
_INLINE_FORMULA_RE = re.compile(
    r"<inline-formula[^>]*>.*?</inline-formula>", re.DOTALL,
)
_MATHML_MI_RE = re.compile(
    r"<math\b[^>]*>.*?<mi\b[^>]*>([^<]+)</mi>", re.DOTALL,
)


def _strip_inline_formulas(html):
    """Replace <inline-formula>...</inline-formula> with the <mi> char.

    Falls back to empty string if no <mi> character can be extracted.
    """
    def _sub(m):
        block = m.group(0)
        mm = _MATHML_MI_RE.search(block)
        if mm:
            return unescape(mm.group(1)).strip()
        return ""
    return _INLINE_FORMULA_RE.sub(_sub, html)


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
    """Normalize IMR Press HTML to a single centered text column.

    IMR Press is a Nuxt-rendered Vue layout. The reading content lives in
    <div class=right-layout watermark-area>; the left sidebar (Academic
    Editor card, Download/3D-view panel, article-tab list) lives in the
    sibling <div class=left-layout>. Both are flex children of
    <div class=article-detail>, which in turn sits inside
    <main class="page-content base-max-width-layout">.

    Chrome stripped (Step 3):
      - <header class=classic-header> site top bar.
      - <footer class=classic-footer> site footer.
      - <div class=cookie-consent> "We use cookies..." banner.
      - <div class=floating-tool-container> fixed Cite/Download/Share
        cluster (bottom-right FAB).
      - <div class=left-layout> sidebar — its flex-basis pushes the
        reading column off-center on wide viewports.

    Reading column (Step 4): <div class=right-layout watermark-area>.
    The hidden references list (`<ul class=article-references-list
    style=display:none>`) is expanded so references render inline.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # Step 3 — strip chrome.
    html = _remove_nested_element(html, r"<header\b[^>]*>")
    html = _remove_nested_element(html, r"<footer\b[^>]*>")
    # IMR Press uses unquoted class attributes in the rendered Nuxt HTML,
    # so remove_elements_by_selector (which matches class="...") misses
    # them. Use _remove_nested_element directly for each.
    # `placeholder-box` reserves the 7rem (~113 px) fixed-header gap;
    # since we just removed the header, drop the spacer too.
    for cls in ("cookie-consent", "floating-tool-container",
                "left-layout", "placeholder-box"):
        for _ in range(5):
            before = html
            html = _remove_nested_element(
                html, rf'<div\b[^>]*\bclass={cls}\b[^>]*>'
            )
            if html == before:
                break
    # Orange "Frontiers in Bioscience-Landmark (FBL) is published by
    # IMR Press from Volume X Issue Y (2021). Previous articles were
    # published by another publisher..." disclaimer banner. Lives in
    # a standalone `<article class=base-journal-theme-block>` inside
    # `.right-layout.watermark-area`, above the "1 Mar 2017 Review"
    # spec start anchor. Strip it so the column starts at the
    # publication-date row as the spec requires.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<article\b[^>]*\bclass=base-journal-theme-block\b[^>]*>',
        )
        if html == before:
            break
    # "Publisher's Note: IMR Press stays neutral with regard to
    # jurisdictional claims..." disclaimer block at the bottom of the
    # article, lives in a separate trailing `<article class=article>`
    # containing only a `<div class=rich-text>` with the disclaimer.
    # Match by the rich-text containing the trailing-disclaimer text.
    html = re.sub(
        r"<article\b[^>]*>\s*<div\b[^>]*\bclass=rich-text\b[^>]*>"
        r"\s*<p>\s*<strong>\s*Publisher’s Note\b.*?</article>",
        "", html, count=2, flags=re.DOTALL,
    )

    # Steps 2 + 4 — layout freeze and reading-column cap.
    # The marker comment makes injection idempotent — re-running
    # remove_banners on already-formatted HTML strips the previous block
    # before injecting the new one (otherwise convert_html accumulates
    # one duplicate style block per run on the same file).
    _INJECT_MARKER = "<!--imrpress-format-html-->"
    html = re.sub(
        re.escape(_INJECT_MARKER) + r"<style>.*?</style>",
        "", html, flags=re.DOTALL,
    )
    override = (
        _INJECT_MARKER
        + "<style>"
        # Overlay-scrollbar trick so the scrollbar doesn't eat ~3 px
        # from the inline axis and shrink the column to 717/vw-3.
        "html{overflow-y:overlay}"
        "html::-webkit-scrollbar{width:0}"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Collapse flex ancestors so the right-layout wrapper sizes to
        # its parent instead of being pinned by flex-basis rules.
        ".layout-container,.page-content,.article-detail{"
        "display:block !important;flex:unset !important;"
        "width:100% !important;max-width:100% !important;"
        "min-width:0 !important;min-height:0 !important;"
        "margin:0 !important;padding:0 !important;"
        "background:#fff !important}"
        # Cap the main reading column.
        ".right-layout.watermark-area{"
        "float:none !important;display:block !important;"
        "flex:unset !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;"
        "padding:56px 16px !important;"
        "box-sizing:border-box !important;"
        "background:#fff !important}"
        # Clamp descendants so no inline fixed width overflows.
        ".right-layout.watermark-area *{"
        "max-width:100% !important;min-width:0 !important}"
        # Tables have intrinsic min-content width that beats max-width;
        # force fixed layout so wide data tables honor the column cap.
        ".right-layout.watermark-area table{"
        "table-layout:fixed !important;width:100% !important;"
        "word-break:break-word !important}"
        # Expand the collapsed references accordion (inline style=display:none).
        ".right-layout.watermark-area .article-references-list,"
        ".article-references-list{display:block !important}"
        # Zero first/last descendant margins so the 56 px padding is the
        # only contributor to the top/bottom gaps. :root prefix beats the
        # site's `.article-detail .right-layout .article{margin-bottom:2rem}`
        # rule on the last article block (References).
        # Zero margin-top only on the DIRECT first child (descendant
        # form kills section headings' native top margin). For the
        # bottom, the descendant *:last-child form is safe because
        # section breaks rely on the following heading's margin-top,
        # not on any preceding element's margin-bottom.
        ":root .right-layout.watermark-area > *:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        # Direct-child only — descendant `*:last-child{margin-bottom:0}`
        # zeros the 2rem natural margin-bottom on article-info, so the
        # following <h3>Abstract</h3> sits flush instead of 32 px below.
        ":root .right-layout.watermark-area > *:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # Last LI inside the last article (article-references) has a
        # 1rem margin-bottom that collapses up through UL → article and
        # adds ~16 px to the wrapper's effective bottom edge.
        ":root .right-layout.watermark-area > article:last-child "
        "*:last-child{margin-bottom:0 !important}"
        # Figures: `_imrpress_inline_figures` (get_refs.py post_capture)
        # extracts the figN.jpg URL from elsewhere in the saved HTML and
        # inlines it as a data URL on `<img id=S<sec>-F<N>-g1>` inside
        # `<figure class=ipub-html-image>`. Native rendering centers the
        # img at its intrinsic pixel dimensions, leaving a visibly
        # narrower image than the caption column. Force block + 100%
        # width so the figure aligns with caption width above the
        # `<span class=ipub-html-label>` + caption `<p>`.
        ":root .right-layout.watermark-area figure.ipub-html-image{"
        "display:block !important;text-align:left !important;"
        "margin:1rem 0 !important;padding:0 !important}"
        ":root .right-layout.watermark-area figure.ipub-html-image img{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
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
    """Parse a single <li class=article-references-item>.

    IMR Press references are emitted as a <mixed-citation> string in the
    canonical form:
        Authors. Title. Journal. Year; Volume(Issue): Pages. DOI.
    with authors separated by ", " and optionally ending in "et al.".
    Volume/issue/pages and DOI are optional (online-ahead-of-print refs
    omit them).
    """
    # Prefer <mixed-citation>, fall back to <span class=rich-text>.
    mc = re.search(
        r"<mixed-citation[^>]*>(.*?)</mixed-citation>",
        entry_html, re.DOTALL,
    )
    if not mc:
        mc = re.search(
            r'<span[^>]*class="?rich-text"?[^>]*>(.*?)</span>',
            entry_html, re.DOTALL,
        )
    inner_html = mc.group(1) if mc else entry_html

    # DOI from link href (more reliable than parsing the text).
    doi = ""
    dm = re.search(
        r'href=["\']?(https?://(?:dx\.)?doi\.org/[^"\'\s>]+)',
        entry_html,
    )
    if dm:
        doi = format_doi(
            unescape(dm.group(1)).replace("dx.doi.org", "doi.org")
        )

    # Replace MathJax inline-formula blocks with their primary character
    # (stripping them entirely removes legitimate math symbols like γ in
    # "γH2AX"). _strip_inline_formulas reads the <mi> inside the MathML
    # script to recover the character.
    inner_html = _strip_inline_formulas(inner_html)

    # Flatten HTML to a single-line text citation.
    text = unescape(strip_tags(inner_html)).strip()
    text = re.sub(r"\s+", " ", text)
    # Drop embedded DOI URL (appears at end of the citation text).
    text = re.sub(r"\s*https?://(?:dx\.)?doi\.org/\S+", "", text)
    # Drop trailing parenthetical notes like "(online ahead of print)".
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    text = text.rstrip(".")

    # Split on ". " into semantic segments. Form:
    #   Authors. Title. Journal[. Subtitle]. Year; Volume[(Issue)][: Pages]
    segments = [s.strip() for s in re.split(r"\.\s+", text) if s.strip()]

    authors = []
    if segments:
        for p in re.split(r",\s*", segments[0]):
            p = p.strip().rstrip(".")
            if not p or p.lower() in ("et al", "et al."):
                continue
            authors.append(_format_imrp_author(p))

    title = segments[1] if len(segments) >= 2 else ""

    # Locate the citation segment (starts with a 4-digit year). Journal is
    # everything between title and citation, rejoined by ". " so journals
    # that contain a period ("Nature Reviews. Endocrinology", "American
    # Journal of Physiology. Endocrinology and Metabolism") are preserved.
    year = volume = issue = pages = ""
    journal = ""
    cite_idx = None
    for i in range(2, len(segments)):
        if re.match(r"^\d{4}(?:;|\s*$)", segments[i]):
            cite_idx = i
            break
    if cite_idx is not None and cite_idx >= 3:
        journal = ". ".join(segments[2:cite_idx]).rstrip(".,")
    elif len(segments) >= 3:
        journal = segments[2].rstrip(".,")
    if cite_idx is not None:
        cite = segments[cite_idx]
        ym = re.match(r"(\d{4})", cite)
        if ym:
            year = ym.group(1)
            tail = cite[ym.end():].lstrip("; ").strip()
            vm = re.match(
                r"(\w+)(?:\((\w+)\))?(?:\s*:\s*([A-Za-z0-9\-\u2013]+))?",
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
        "year": year,
        "volume": volume,
        "issue": issue,
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
        abstract_html = _strip_inline_formulas(abstract_html)
        text = tags_to_text(strip_common(abstract_html))
        text = re.sub(r"^\s*Abstract\s*", "", text).strip()
        if text:
            parts.append("## Abstract\n" + text)

    kw_html = _slice_article(html, "keywords")
    if kw_html:
        # IMR Press writes <li> without closing </li>, so the usual
        # <li>...</li> regex finds nothing. Split the UL body on <li
        # boundaries and pull text up to the next <li or end.
        ul_m = re.search(r"<ul[^>]*>(.*?)</ul>", kw_html, re.DOTALL)
        body = ul_m.group(1) if ul_m else kw_html
        starts = [m.start() for m in re.finditer(r"<li[\s>]", body)]
        kws = []
        for i, s in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(body)
            # Strip the opening <li ...> tag, then flatten the remainder.
            chunk = re.sub(r"^<li[^>]*>", "", body[s:end])
            kw = unescape(strip_tags(chunk)).strip()
            if kw:
                kws.append(kw)
        if kws:
            parts.append("## Keywords\n" + "; ".join(kws))

    body_html = _slice_article(html, "content")
    if body_html:
        # Promote IMR Press section titles to <h3> so tags_to_text renders
        # them as "## Heading" instead of flattened paragraphs. The markup
        # is <div class=ipub-html-title>1. Introduction</div>, which the
        # default heading regex in tags_to_text would otherwise miss.
        body_html = re.sub(
            r'<div[^>]*class="?ipub-html-title"?[^>]*>(.*?)</div>',
            r"<h3>\1</h3>",
            body_html,
            flags=re.DOTALL,
        )
        body_html = _strip_inline_formulas(body_html)
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
    """Parse IMR Press HTML into a papers/*.json-format dict."""
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
