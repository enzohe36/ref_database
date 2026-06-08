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
    remove_elements_by_id,
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
    """Apply Phase 2 layout rules for imrpress.com.

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies. The
            `.article-detail` wrapper ships its own `max-width:1200px`
            and a 60/40 left-sidebar/right-article split that needs
            collapsing to the body cap.
    Step 2: remove `.cookie-consent` (position:fixed bottom banner).
    Step 3: remove sticky chrome flagged by scan_sticky.py — the
            page-wide fixed `header.classic-header`, the right-side
            fixed `.floating-tool-container` (Cite/Share/PDF rail),
            and the Hotjar `#hj-survey-toggle-1` floater.
            `.side-bar.base-sticky-menu` (article TOC sidebar) is
            removed via Step 4 by stripping the parent
            `.left-layout` column.
    Step 4: remove `.left-layout` — IMR Press' left article TOC sidebar
            (Catalogue / Figures / References / Citations tabs). It
            spans full article height and lives beside the right-layout
            article column.
    Step 5: no ad slots ship in the captured imrpress HTML
            (no ad/gpt/dfp/sponsored markers found).
    Step 6: page background is white; `.layout-container` and `body`
            are explicitly forced white for symmetry. The footer ships
            a navy `.classic-footer` background; cap it to body width
            so the colored band doesn't bleed past the centered cap.
    Step 7: figure images are inlined at full natural resolution
            (1.7K-5K px wide JPEGs) — well above the 720-px column
            width target. No retrieval issue.
    Step 8: figures live inside `<figure class=ipub-html-image>` with
            an `<img>` followed by `<span class=ipub-html-label>`
            label and one or more caption `<p>` paragraphs. Force
            block layout, image at column width, caption below.
            Tables inside `.ipub-html-table-wrap` already overflow
            cleanly when wider than the column; cap the wrapper so it
            doesn't push body width past the cap.
    Step 9: expand collapsed content:
            - `.name-list.ellipsis-3-lines` (line-clamped 3 lines) →
              show all author rows.
            - `.article-info-container` (inline `style=display:none`,
              holds editor / received / accepted / publication metadata)
              → reveal.
            - `.article-references-list` (inline `style=display:none`,
              the entire references list) → reveal so all references
              render below the toggle heading.
            Layer-1 DOM strip (inline style attribute) handles the
            references list and article-info container; Layer-2 CSS
            override handles the line-clamped author row.
    """
    html = neutralize_media_queries(html)

    # IMR Press writes class attributes UNQUOTED (`class=floating-tool-container`),
    # so the shared remove_elements_by_selector helper (which expects
    # double-quoted `class="..."`) doesn't match. Use _remove_nested_element
    # with patterns tolerating both quote conventions.
    def _strip_class(html_, tag, name):
        return _remove_nested_element(
            html_,
            rf'<{tag}\b[^>]*\bclass=(?:"[^"]*\b{re.escape(name)}\b[^"]*"|'
            rf"'[^']*\b{re.escape(name)}\b[^']*'|"
            rf'{re.escape(name)}\b)[^>]*>',
        )

    # Step 2 — cookie consent banner.
    html = _strip_class(html, "div", "cookie-consent")
    # Step 3 — fixed-position site header, right-side floating tool rail,
    # and Hotjar survey toggle button.
    html = _strip_class(html, "header", "classic-header")
    html = _strip_class(html, "div", "floating-tool-container")
    html = remove_elements_by_id(html, "hj-survey-toggle-1")
    # Step 4 — left article TOC sidebar.
    html = _strip_class(html, "div", "left-layout")
    # Step 10 — drop the page-content placeholder-box. The publisher
    # reserved 128 px (one viewport) at the top of <main> so the
    # now-removed `position:fixed` header didn't overlap content; with
    # the header gone the reservation becomes a leading blank band.
    html = _strip_class(html, "div", "placeholder-box")
    # Step 9 (Layer 1) — strip inline display:none on references and
    # article-info containers so the parser-visible content also renders.
    html = re.sub(
        r'(<ul[^>]*class="[^"]*article-references-list[^"]*"[^>]*?)\s*style="display:none"',
        r"\1", html,
    )
    html = re.sub(
        r'(<div[^>]*class="[^"]*article-info-container[^"]*"[^>]*?)\s*style="display:none"',
        r"\1", html,
    )

    override = (
        "<style>"
        # Step 1 / Step 6 — lock layout to 752 px, center, white bg.
        "html{margin:0!important;padding:0!important;"
        "background:#fff!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "min-width:0!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "background:#fff!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # Page-wide IMR Press wrappers ship max-width:1200px and the
        # `.article-detail` 60/40 sidebar/article split. Collapse to body.
        # Force-white only on the body wrappers, NOT on .base-max-width-layout
        # (that class is reused inside .classic-footer where the navy bg
        # must stay).
        ".layout-container,.page-content,.article-detail,"
        ".right-layout"
        "{width:auto!important;max-width:100%!important;"
        "margin-left:auto!important;margin-right:auto!important;"
        "padding-left:0!important;padding-right:0!important;"
        "box-sizing:border-box!important;background:#fff!important;}"
        ".base-max-width-layout"
        "{width:auto!important;max-width:100%!important;"
        "margin-left:auto!important;margin-right:auto!important;"
        "padding-left:0!important;padding-right:0!important;"
        "box-sizing:border-box!important;}"
        # Footer keeps its navy bg but must respect body cap.
        ".classic-footer{max-width:100%!important;width:auto!important;"
        "box-sizing:border-box!important;}"
        # Step 9 (Layer 2) — un-clamp the author row.
        ".name-list.ellipsis-3-lines,.name-list"
        "{-webkit-line-clamp:none!important;line-clamp:none!important;"
        "overflow:visible!important;display:block!important;"
        "max-height:none!important;}"
        # Step 9 — ensure references list renders even if the inline
        # style strip missed (defense in depth).
        ".article-references-list{display:block!important;}"
        ".article-info-container{display:block!important;}"
        # Step 8 — figures: image above caption, image at column width.
        "figure.ipub-html-image"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;margin:16px 0!important;"
        "box-sizing:border-box!important;}"
        "figure.ipub-html-image img"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 auto 8px auto!important;}"
        "figure.ipub-html-image .ipub-html-label,"
        "figure.ipub-html-image p"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;margin:0 0 4px 0!important;}"
        # The "Full Image" CTA button below each figure is publisher
        # chrome but clutters reading; collapse without removing
        # (parser ignores it via _wrap_figure_captions logic).
        ".ipub-button{display:none!important;}"
        # Tables: keep horizontal scroll wrapper at column width.
        ".ipub-html-table-wrap"
        "{max-width:100%!important;width:auto!important;"
        "box-sizing:border-box!important;overflow-x:auto!important;}"
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


def _wrap_figure_captions(html):
    """Inject a <figcaption> into each IMR Press <figure> so extract_captions
    can pull the label + description.

    IMR Press emits figures as:
      <figure ...>
        <img ...>
        <span class=ipub-html-label>Fig. N.</span>
        <p><strong>Title</strong>. Description.</p>
        <div class=ipub-button>Full Image</div>
      </figure>
    No <figcaption> wrapper exists, so the shared extract_captions helper
    drops the entire figure. Build a <figcaption> from the ipub-html-label
    span plus every <p> inside the figure and inject it before </figure>.
    """
    def _build(m):
        block = m.group(0)
        # Pull label text (e.g. "Fig. 1.")
        label = ""
        lm = re.search(
            r'<span[^>]*class="?ipub-html-label"?[^>]*>(.*?)</span>',
            block, re.DOTALL,
        )
        if lm:
            label = re.sub(r"\s+", " ", strip_tags(lm.group(1))).strip()
        # Pull every <p>...</p> body inside the figure
        paras = []
        for pm in re.finditer(r"<p[^>]*>(.*?)</p>", block, re.DOTALL):
            inner = re.sub(r"\s+", " ", strip_tags(pm.group(1))).strip()
            if inner:
                paras.append(inner)
        caption = label
        if paras:
            caption = (caption + " " + " ".join(paras)).strip()
        if not caption:
            return block
        # Inject <figcaption> just before </figure>
        return re.sub(
            r"</figure>",
            f"<figcaption>{caption}</figcaption></figure>",
            block, count=1,
        )
    return re.sub(
        r"<figure\b[^>]*>.*?</figure>", _build, html, flags=re.DOTALL,
    )


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
        body_html = _wrap_figure_captions(body_html)
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
