"""Nature (nature.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    format_name,
    get_meta,
    neutralize_media_queries,
    parse_meta_authors,
    remove_elements_by_id,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "Source data",
    "Full size image",
    "Full size table",
)

# Reference section titles (removed from main_text)
_REF_SECTIONS = {"references"}

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# Sections to skip (not part of main_text)
_PRE_BODY = {"inline recommendations"}


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize Nature HTML to a single centered text column.

    Per format-html-extra.md the reading column spans "Article /
    Published: 26 August 2012" through the Subjects list at the bottom
    of <div id=article-info-section>. Removed elements fall into three
    buckets: (a) items format-html-extra.md names explicitly, (b) ads,
    (c) toolbars. Non-chrome article content (Inline Recommendations,
    article-info-section, rightslink-section, etc.) stays in the DOM.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    # (a) instruction-doc items --------------------------------------
    # "Your privacy, your choice" cookie banner (a <dialog>).
    html = _remove_nested_element(
        html, r'<dialog\b[^>]*\bclass=["\']?[^"\'>]*\bcc-banner\b',
    )
    # (b) ads --------------------------------------------------------
    # 728x90 leaderboard ad.
    html = _remove_nested_element(
        html,
        r'<aside\b[^>]*\bclass=["\']?[^"\'>]*\bc-ad--728x90\b',
    )
    # Nature Briefing newsletter-signup promo banner.
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass="[^"]*\bc-site-messages--nature-briefing\b[^"]*"[^>]*>',
        )
        if html == before:
            break
    # Cookie-consent ghost placeholder <div data-cc-ghost> that reserves
    # space for a deferred ad load.
    html = _remove_nested_element(
        html, r'<div\b[^>]*\bdata-cc-ghost\b',
    )
    # "Similar content being viewed by others" inline-recommendations
    # strip inside <main>. User-requested removal.
    html = _remove_nested_element(
        html,
        r'<section\b[^>]*\bclass=["\']?[^"\'>]*\bc-article-recommendations\b',
    )
    # (c) toolbars ---------------------------------------------------
    # Right-column reading-companion toolbar (<aside class=c-article-extras>):
    # Download-PDF button, share icons, Sections/Figures/References tabs.
    html = _remove_nested_element(
        html,
        r'<aside\b[^>]*\bclass=["\']?[^"\'>]*\bc-article-extras\b',
    )
    # Site footer (nature.com "About / Publish / Privacy" bar). The
    # outer `<footer>` also wraps a journal-name + ISSN c-meta block,
    # but that is publication-level metadata redundant with the
    # article-header citation already inside the wrapper (journal
    # abbreviation + volume/pages + DOI). Strip the whole footer for
    # consistency with the other parsers' chrome-strip pattern and to
    # keep the spec's `B=56` measurement well-defined against the
    # last article paragraph (vs. against a sibling block outside the
    # wrapper).
    html = _remove_nested_element(html, r"<footer\b[^>]*>")
    # Site header — nature.com uses `c-header` and Springer (link.springer
    # .com, aliased to this parser) uses `eds-c-header`. Both render
    # logo + nav + search at the top of the body, above the article
    # masthead. Strip for consistency with the footer-strip pattern.
    html = _remove_nested_element(html, r"<header\b[^>]*>")
    # Springer's search/menu popup expanders (`.eds-c-header__expander`)
    # are siblings of the `<header>`, not inside it — the header strip
    # above misses them. They render visible search bars + nav menus
    # above the article.
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass=["\']?[^"\'>]*\beds-c-header__expander\b',
        )
        if html == before:
            break
    # Springer's `<a class=c-skip-link>` "Skip to main content" link
    # and `<div class=c-status-message ...c-status-message--banner>`
    # site-wide notice/cookie banner sit between the header and the
    # article. The skip-link is normally hidden via `top:-45px` but
    # SingleFile may capture it visible.
    html = _remove_nested_element(
        html, r'<a\b[^>]*\bclass=["\']?[^"\'>]*\bc-skip-link\b',
    )
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass="[^"]*\bc-status-message--banner\b[^"]*"[^>]*>',
        )
        if html == before:
            break
    # Springer wraps its 728x90 leaderboard ad in `<aside class="u-lazy-
    # ad-wrapper ...">` (the existing `c-ad--728x90` strip above only
    # handled the inner div, not the outer aside that holds the lazy
    # placeholder). Strip the wrapper.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<aside\b[^>]*\bclass=["\']?[^"\'>]*\bu-lazy-ad-wrapper\b',
        )
        if html == before:
            break
    # Springer breadcrumbs nav: `<nav><ol class=c-breadcrumbs>...</ol></nav>`
    # at the top of the article, above the masthead. Site-chrome.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<nav\b[^>]*>\s*<ol\b[^>]*\bclass=["\']?[^"\'>]*\bc-breadcrumbs\b',
        )
        if html == before:
            break
    # Content before "Article / Published: 26 August 2012" anchor ------
    # Top-of-body chrome wrapper (<div data-test=top-containers>): holds
    # grade-c browser notice, leaderboard ad slot, breadcrumb nav.
    html = _remove_nested_element(
        html, r'<div\b[^>]*\bdata-test=["\']?top-containers\b',
    )
    # DO NOT strip `c-status-message` — Erratum / "This article has
    # been updated" notices sit BETWEEN the "Published: …" anchor and
    # "Abstract" (verified by DOM position in raw), i.e. inside the
    # reading column, not before it. They are content, not chrome.

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        # Layout freeze (Step 2).
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # #content carries `padding:16px` which adds a gutter inside the
        # body between the cap and the wrapper. Zero so the cap measures
        # from the body edge.
        "#content{padding:0 !important;margin:0 !important;"
        "width:100% !important;max-width:100% !important}"
        # <main> ships with float:left (u-float-left) so the sibling aside
        # can dock to its right; with the aside removed, clear the float
        # and let main fill its parent.
        "main.c-article-main-column{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;"
        # padding-bottom trimmed to compensate line-box descent + the
        # .c-article-section__content's native 40 px margin-bottom
        # that sits below the last text inside the final section.
        "padding:56px 16px 19px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # Zero margin-top along the wrapper's first-descendant chain
        # (every level's first element child), so collapsed margins
        # don't leak through main's top padding and push T past 56.
        # Sections, headings, and elements deeper than the chain keep
        # their native margins, preserving publisher typography.
        "main.c-article-main-column>*:first-child,"
        "main.c-article-main-column>*:first-child>*:first-child,"
        "main.c-article-main-column>*:first-child>*:first-child>*:first-child,"
        "main.c-article-main-column>*:first-child>*:first-child>*:first-child>*:first-child,"
        "main.c-article-main-column>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child,"
        "main.c-article-main-column>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child"
        "{margin-top:0 !important;padding-top:0 !important}"
        "main.c-article-main-column>*:last-child,"
        "main.c-article-main-column>*:last-child>*:last-child,"
        "main.c-article-main-column>*:last-child>*:last-child>*:last-child,"
        "main.c-article-main-column>*:last-child>*:last-child>*:last-child>*:last-child,"
        "main.c-article-main-column>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child,"
        "main.c-article-main-column>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child"
        "{margin-bottom:0 !important;padding-bottom:0 !important}"
        # `<section class=c-article-recommendations>` ("Similar content
        # being viewed by others") sat before each c-article-section
        # with its native 48 px margin-bottom contributing to the gap
        # above the section's H2 title. After chrome removal, the gap
        # above "Main" (and every subsequent section title) collapses
        # from 48 to 24 px. Bump the parent section's margin-top so
        # the 48 px rhythm returns. Use `margin-top` (collapses with
        # neighbors per CSS spec) rather than `padding-top` (would
        # uniformly inflate every section's height by 24 px and shift
        # all subsequent content down).
        "main.c-article-main-column div.c-article-section{"
        "margin-top:24px !important}"
        # Author list collapse — Springer hides authors beyond the
        # first few via `c-article-author-list__item--hide-small-screen`
        # when the viewport is narrow. The 752-px body cap forces
        # narrow mode, so the full author list is hidden behind a
        # "Show authors" button that is JS-driven and dead in the
        # static capture. Force every author item visible and hide
        # the now-redundant button.
        ":root .c-article-author-list__item--hide-small-screen{"
        "display:inline !important}"
        ".c-article-author-list__button{display:none !important}"
        # Figures: nature wraps each figure in
        #   <figure>
        #     <figcaption>
        #       <b class=c-article-section__figure-caption>Figure N: title</b>
        #     </figcaption>
        #     <div class=c-article-section__figure-content>
        #       <div class=c-article-section__figure-item>
        #         <picture class=c-article-section__figure-picture>
        #           <img src="data:..." srcset sizes loading=lazy
        #                width=685 height=N>
        #         </picture>
        #         <span class=u-visually-hidden>AI alt disclaimer</span>
        #         <div class=c-article-section__figure-link>
        #           <a class=c-article__pill-button data-track-action="view figure"
        #              href=https://www.nature.com/articles/<id>/figures/<N>>
        #              Full size image</a>
        #         </div>
        #       </div>
        #       <div class=c-article-section__figure-description data-test=bottom-caption>
        #         <p>caption body</p>
        #       </div>
        #     </div>
        #   </figure>
        # Native order: caption-title (figcaption) FIRST, image MIDDLE,
        # description LAST. Per the figure layout contract, image must
        # be ABOVE the entire caption. Use flex column with `order`:
        # picture→1, figcaption→2, description→3, hide
        # `.c-article-section__figure-link` (JS sub-page navigation,
        # dead in static capture).
        # The high-res JPEG URL pattern is `https://media.springernature.com/lw1200/.../<id>_Fig<N>_HTML.jpg` —
        # exposed in the page's JSON-LD `image` array; get_refs.py uses
        # `_NATURE_FIGURES_FIX_JS` to swap <img src> ← that URL.
        ":root figure:has(.c-article-section__figure-content){"
        "display:flex !important;flex-direction:column !important;"
        "margin:1rem 0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important}"
        ":root figure .c-article-section__figure-content{"
        "display:flex !important;flex-direction:column !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important}"
        ":root figure .c-article-section__figure-item{"
        "display:flex !important;flex-direction:column !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important}"
        ":root figure picture.c-article-section__figure-picture{"
        "order:-1 !important;display:block !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important;padding:0 !important}"
        ":root figure picture.c-article-section__figure-picture > img{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 !important}"
        # Hide the JS-only "Full size image" pill button (sub-page nav).
        ":root figure .c-article-section__figure-link{"
        "display:none !important}"
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
    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_online_date")
            or get_meta(html, "dc.date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev")
    journal = re.sub(r"  +", " ", journal.replace(".", "")).strip()
    if not journal:
        # Springer book chapters (link.springer.com/protocol or /chapter)
        # lack citation_journal_abbrev but embed the series name in a
        # JSON blob: "seriesTitle":"Methods in Molecular Biology".
        series_m = re.search(r'"seriesTitle"\s*:\s*"([^"]+)"', html)
        if series_m:
            journal = series_m.group(1).strip()

    volume = get_meta(html, "citation_volume")
    if not volume:
        # Springer book chapters expose the series volume inline after
        # the series link: '((MIMB,volume 2102))' or '((SCBI,volume 104))'.
        vm = re.search(r'\(\(\w+,\s*volume\s*(\d+)\)\)', html)
        if vm:
            volume = vm.group(1)

    return {
        "title": get_meta(html, "citation_title"),
        "journal": journal,
        "year": year,
        "volume": volume,
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
    Author name format is enforced by _helpers.format_author_name.
    Uses citation_author / citation_author_institution meta tags.
    """
    meta_authors = parse_meta_authors(html)
    return [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in meta_authors
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _flip_author_name(name):
    """Convert 'IN LastName' (e.g. 'JD Griffith') to 'LastName IN' via shared helpers."""
    return format_author_name(name)


def _parse_freeform_citation(text):
    """Parse a freeform citation string (no key=value pairs).

    Extracts year, DOI, and stores the full text as title for PubMed lookup.
    """
    text = re.sub(r'\s+', ' ', text).strip()

    # Extract DOI
    doi = ""
    doi_m = re.search(r'(https?://doi\.org/\S+)', text)
    if doi_m:
        doi = doi_m.group(1).rstrip('.')
    elif re.search(r'(10\.\d{4,}/\S+)', text):
        doi_m = re.search(r'(10\.\d{4,}/\S+)', text)
        doi = f"https://doi.org/{doi_m.group(1).rstrip('.')}"

    # Extract year from (YYYY) pattern
    year = ""
    year_m = re.search(r'\((\d{4})\)', text)
    if year_m:
        year = year_m.group(1)

    return {
        "title": text,
        "journal": "",
        "year": year,
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": doi,
        "authors": [],
    }


def _parse_citation_reference(content):
    """Parse a single citation_reference meta tag content string.

    Format: 'citation_journal_title=X; citation_title=Y; ...'
    Falls back to freeform parsing for plain-text citations.
    Returns a dict with {title, journal, year, volume, issue, pages, doi, authors}.
    """
    fields = {}
    author_parts = []
    for part in content.split("; "):
        if "=" in part:
            key, val = part.split("=", 1)
            key = key.strip()
            val = val.strip()
            # Accumulate citation_author values (may appear multiple times)
            if key == "citation_author":
                author_parts.append(val)
            else:
                fields[key] = val

    # If no key=value pairs found, parse as freeform citation
    if not fields and not author_parts:
        return _parse_freeform_citation(content)

    authors = []
    # Authors may be in a single comma-separated field or multiple fields
    raw = ", ".join(author_parts)
    if raw:
        authors = [_flip_author_name(a.strip()) for a in raw.split(", ") if a.strip()]

    journal = fields.get("citation_journal_title", "")
    journal = journal.replace(".", "")
    # Collapse multiple spaces after dot removal
    journal = re.sub(r"  +", " ", journal).strip()

    title = fields.get("citation_title", "")
    # Book citations carry citation_publisher instead of citation_journal_title;
    # the book title plays the journal role per the project convention.
    if not journal and fields.get("citation_publisher") and title:
        journal = title
        title = ""

    return {
        "title": title,
        "journal": journal,
        "year": fields.get("citation_publication_date", ""),
        "volume": fields.get("citation_volume", ""),
        "issue": "",
        "pages": fields.get("citation_pages", ""),
        "doi": format_doi(fields.get("citation_doi", "")),
        "authors": authors,
    }


_DOTTED_AUTHOR_RE = re.compile(
    r"[A-Z][\w\-']+(?:\s[\w\-']+)*,\s+(?:[A-Z]\.\s*){1,5}"
)
_COMPACT_AUTHOR_RE = re.compile(
    r"([A-Z][\w\-']+(?:\s[\w\-']+)*)\s+([A-Z]{1,5})(?=\s*(?:,|&|et al|\.|$))"
)


def _parse_body_reference(item_html):
    """Parse a single <p class=c-article-references__text> body reference.

    Uses <i>Journal</i> and <b>Volume</b> tags as structural anchors so
    field boundaries don't depend on period-splitting in prose (journal
    abbreviations like "J. Exp. Med." and titles containing colons or
    species names no longer confuse the parser).

    Covers three observed Nature/Springer layouts:
      A. Authors. Title. <i>Journal</i> <b>Vol</b>[, Pages] (YEAR).
      B. Authors. Title. <i>Journal</i> YEAR; <b>Vol</b>: Pages.
      C. Authors . YEAR <i>Journal</i> <b>Vol</b>: Pages  (no title)

    Fields:
    - volume: content of the first <b>...</b>.
    - journal: <i>...</i> preceding <b>, plus an optional following
      "(<i>...</i>.)" continuation (e.g., "DNA Repair (Amst)").
    - year: parenthesized (YYYY) anywhere → bare YYYY between </i>
      and <b> (Layout B) → bare YYYY before <i> (Layout C).
    - pages/issue: plain text between </b> and trailing (YYYY).
      Parenthesized span within is issue; remainder is pages.
    - head: text before journal <i> with trailing Layout-C year
      removed. Split into authors and title via "et al." or the last
      "LastName, I[. I.]" / "LastName IN" match.

    Falls back to a title-only record only when no <b> or no <i>
    precedes <b> (≤0.02% of observed body refs).
    """
    doi = ""
    m = re.search(r'href=["\']?(https?://doi\.org/[^\s"\'<>]+)', item_html)
    if m:
        doi = format_doi(m.group(1))

    b_m = re.search(r"<b[^>]*>\s*(.+?)\s*</b>", item_html, re.DOTALL)
    if not b_m:
        return _body_fallback(item_html, doi)
    volume = re.sub(r"<[^>]+>", "", b_m.group(1)).strip()

    pre_b = item_html[:b_m.start()]
    # Find all <i>...</i> blocks individually (non-greedy $-anchored search
    # can span TWO <i> tags when the regex engine expands .+? to reach the
    # end — seen on refs like "Ludérus ... <i>et al</i>. ... <i>J. Cell
    # Biol.</i>" where the `<i>et al</i>` author italic precedes the
    # journal italic). Journal is the last <i> block before <b>; an
    # optional "(<i>X</i>)" continuation right after (e.g. "DNA Repair
    # (Amst)") folds into the journal name.
    i_matches = list(re.finditer(r"<i[^>]*>(.+?)</i>", pre_b, re.DOTALL))
    if not i_matches:
        return _body_fallback(item_html, doi)
    last_i = i_matches[-1]
    # Check for a (<i>...</i>) continuation after the primary journal block —
    # only applies when the last <i> is inside parens, with another <i>
    # immediately before. e.g. "<i>DNA Repair</i> (<i>Amst</i>)".
    journal = unescape(re.sub(r"<[^>]+>", "", last_i.group(1))).strip().rstrip(".").strip()
    head_end = last_i.start()
    if len(i_matches) >= 2:
        prev_i = i_matches[-2]
        between = pre_b[prev_i.end():last_i.start()]
        after_last = pre_b[last_i.end():].rstrip(" .,")
        if re.match(r"\s*\(\s*$", between) and after_last.startswith(")"):
            cont = journal
            journal = f"{unescape(re.sub(r'<[^>]+>', '', prev_i.group(1))).strip().rstrip('.').strip()} ({cont})"
            head_end = prev_i.start()
    head_html = pre_b[:head_end]

    year = ""
    pyrs = re.findall(r"\(\s*(\d{4})[a-z]?\s*\)", item_html)
    if pyrs:
        year = pyrs[-1]
    else:
        m = re.search(r"</i>\s*\.?\s*(\d{4})[a-z]?\s*[;,]", item_html)
        if m:
            year = m.group(1)
        else:
            m = re.search(r"[.\s](\d{4})[a-z]?\s+<i", item_html)
            if m:
                year = m.group(1)

    after = item_html[b_m.end():]
    after_text = unescape(re.sub(r"<[^>]+>", "", after))
    after_text = re.sub(r"\s+", " ", after_text).strip()
    after_text = re.sub(r"\(\s*\d{4}[a-z]?\s*\)\s*\.?\s*$", "", after_text).strip()
    after_text = re.sub(r"^\s*[,:;]\s*", "", after_text).strip(" ,:;.")
    issue = ""
    im = re.search(r"\(([^)]+)\)", after_text)
    if im:
        issue = im.group(1).strip().rstrip(".")
        after_text = (after_text[:im.start()] + after_text[im.end():]).strip(" ,:;.")
    pages = after_text.replace("\u2013", "-").strip()

    head = unescape(re.sub(r"<[^>]+>", "", head_html))
    head = re.sub(r"\s+", " ", head).strip()
    if year:
        head = re.sub(r"\s*" + re.escape(year) + r"[a-z]?\s*\.?\s*$", "", head)
    head = head.strip().rstrip(".")

    authors, title = _split_body_authors_title(head)

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


def _body_fallback(item_html, doi):
    """Parse refs that lack <i>/<b> structural tags.

    Tries three plaintext formats in order:
      1. "Authors. Title. Journal Vol[, Pages] (YEAR)." — year-at-end,
         common for modern refs that lost styling in the HTML.
      2. "AuthorList (YEAR) Title. Journal, Vol, Pages" — EMBO/Oxford
         comma-style.
      3. "AuthorList (YEAR) Title. Journal Vol[(Issue)][:Pages]" —
         older Springer colon-style.
    Returns a title-only record if none match (true books, theses,
    software citations).
    """
    text = re.sub(r"<a[^>]*>.*?</a>", " ", item_html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"https?://doi\.org/\S+", "", text)
    # BMC-style trailing bare DOI (no https://doi.org/ prefix): 'Science.
    # 1991, 251: 1351-1355. 10.1126/science.1900642.' — strip so the tail
    # anchors of the BMC/semicolon regexes below can match on pages.
    bare_doi_m = re.search(r"\s+(10\.\d{4,}/\S+?)\s*\.?\s*$", text)
    if bare_doi_m:
        if not doi:
            doi = format_doi(bare_doi_m.group(1).rstrip("."))
        text = text[: bare_doi_m.start()].rstrip()
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")

    year_end = _parse_year_at_end_plaintext(text, doi)
    if year_end is not None:
        return year_end

    # Modern Springer/Nature plaintext refs use two compact pagination
    # separators that lose <i>/<b> styling in the saved HTML:
    #
    # A. Semicolon:
    #    "Authors. Title. Journal. YEAR;VOL[(Issue)]:PAGES."
    # B. Colon-after-authors, comma-separated pagination (BMC/BioMed):
    #    "Authors: Title. Journal. YEAR, VOL[(Issue)]: PAGES."
    #
    # Anchor on the pagination tail so prose-containing titles don't slip
    # past. Run these before the book-chapter patterns because they're
    # unambiguous.
    for sep1, sep2, auth_delim in (
        (";", ":", "."),      # Semicolon / Guterres / Oncogene
        (",", ":", ":"),      # BMC / Wang / Waldner
    ):
        pat = (
            r"^(?P<authors>.+?)" + re.escape(auth_delim)
            + r"\s+(?P<title>.+?)[.?!]\s+"
            + r"(?P<journal>[A-Z][^.]*?)\.\s+"
            + r"(?P<year>\d{4})\s*" + re.escape(sep1) + r"\s*"
            + r"(?P<vol>\d+)(?:\s*\((?P<issue>[^)]+)\))?\s*"
            + re.escape(sep2) + r"\s*"
            + r"(?P<pages>[\w.\-\u2013\u2014]+)\.?\s*$"
        )
        m = re.match(pat, text)
        if m:
            auth_str = m.group("authors")
            if auth_delim == ".":
                auth_str = auth_str.rstrip(",").strip()
            authors = _parse_body_author_list(auth_str)
            if not authors:
                authors = [
                    a.strip() for a in auth_str.split(",") if a.strip()
                ]
            return {
                "title": m.group("title").strip().rstrip("."),
                "journal": m.group("journal").strip().rstrip("."),
                "year": m.group("year"),
                "volume": m.group("vol"),
                "issue": m.group("issue") or "",
                "pages": re.sub(
                    r"[\u2010-\u2014]", "-", m.group("pages")
                ).rstrip("."),
                "doi": doi,
                "authors": authors,
            }

    # Nature body refs for book chapters in the Heim/Mitelman form:
    # "Authors. in Book Title pages (Publisher, City, Year)."
    # Anchor on "<authors ending in a period>. in <CapitalizedBookTitle>"
    # so the "in" inside prose (e.g., "Python in Science Conference") does
    # not hijack the match.
    chap_m = re.match(
        r"^(?P<authors>.+?\.)\s+in\s+(?P<book>[A-Z][^.]*?)\s+"
        r"(?P<pages>\d+[\-\u2013\u2014]\d+|[A-Za-z]?\d+(?:[\-\u2013\u2014][A-Za-z]?\d+)?)?"
        r"\s*\(([^)]*?)(?P<year>\d{4})\s*\)\.?\s*$",
        text,
    )
    if not chap_m:
        # Standalone book monograph:
        # "Authors. Book Title (Publisher, City, Year)."
        # Require the closing paren to carry a publisher-like token so
        # regular journal refs don't get misread as books.
        mono_m = re.match(
            r"^(?P<authors>.+?\.)\s+(?P<book>[A-Z][^()]+?)\s+"
            r"\((?P<paren>[^)]*?(?:Press|Publishers?|Publishing|Freeman|"
            r"Wiley|Springer|Elsevier|Chapman\s*&\s*Hall|CRC|Academic|"
            r"University|Laboratory|INSERM|Humana|Dekker|Garland|Saunders|"
            r"Mosby|Kluwer|Blackwell|ASM)[^)]*?)"
            r"(?P<year>\d{4})\s*\)\.?\s*$",
            text,
        )
        if mono_m:
            mono_authors = _parse_body_author_list(mono_m.group("authors").rstrip(","))
            if not mono_authors:
                mono_authors = [
                    a.strip() for a in mono_m.group("authors").rstrip(",.").split(",") if a.strip()
                ]
            return {
                "title": "",
                "journal": mono_m.group("book").strip().rstrip(",.").strip(),
                "year": mono_m.group("year"),
                "volume": "",
                "issue": "",
                "pages": "",
                "doi": doi,
                "authors": mono_authors,
            }
    if chap_m:
        chap_authors = _parse_body_author_list(chap_m.group("authors").rstrip(","))
        if not chap_authors:
            chap_authors = [
                a.strip() for a in chap_m.group("authors").rstrip(",").split(",") if a.strip()
            ]
        return {
            "title": "",
            "journal": chap_m.group("book").strip().rstrip(",.").strip(),
            "year": chap_m.group("year"),
            "volume": "",
            "issue": "",
            "pages": re.sub(r"[\u2010-\u2014]", "-", chap_m.group("pages") or ""),
            "doi": doi,
            "authors": chap_authors,
        }

    m = re.match(r"^(.+?)\s+\((\d{4})[a-z]?\)\.?\s+(.+)$", text)
    if not m:
        return {
            "title": text, "journal": "", "year": "",
            "volume": "", "issue": "", "pages": "", "doi": doi, "authors": [],
        }
    authors_str, year, rest = m.group(1), m.group(2), m.group(3)
    # Try the structured helper first (handles "LastName, I." and "LastName I")
    # before falling back to naive comma split.
    authors = _parse_body_author_list(authors_str.rstrip(","))
    if not authors:
        authors = [a.strip() for a in authors_str.rstrip(",").split(",") if a.strip()]

    # Two tail shapes observed in Springer/EMBO plaintext refs:
    #   "Journal Vol[(Issue)][:Pages]"   (colon-separated, older style)
    #   "Journal, Vol[(Issue)], Pages"   (comma-separated, EMBO/Oxford style)
    tail = re.search(
        r"[.?!]\s+(.+?),\s*(\d+)(?:\(([\d\w\-\u2013]+)\))?,\s+([\w\-\u2013]+)\s*\.?\s*$",
        rest,
    )
    if tail:
        title = rest[: tail.start()].rstrip(".").strip()
        journal = tail.group(1).strip().rstrip(".")
        volume = tail.group(2)
        issue = tail.group(3) or ""
        pages = tail.group(4).replace("\u2013", "-").strip()
    else:
        tail = re.search(
            r"[.?!]\s+([^.?!]+?)\s+(\d+)(?:\(([\d\w\-\u2013]+)\))?(?::\s*(.+?))?$",
            rest,
        )
        if tail:
            title = rest[: tail.start()].rstrip(".").strip()
            journal = tail.group(1).strip().rstrip(".")
            volume = tail.group(2) or ""
            issue = tail.group(3) or ""
            pages = (tail.group(4) or "").replace("\u2013", "-").strip()
        else:
            # Book-monograph fallback: "Book Title. City: Publisher" —
            # the period before "City:" separates the book name (journal
            # role) from the publisher metadata. Covers "ggplot2:
            # elegant graphics for data analysis. Berlin: Springer".
            book_m = re.match(
                r"^(?P<book>.+?)\.\s+[^.]+?:\s+[A-Z].*$",
                rest.rstrip("."),
            )
            if book_m:
                title = ""
                journal = book_m.group("book").strip().rstrip(".")
                volume = issue = pages = ""
            else:
                title, journal, volume, issue, pages = (
                    rest.rstrip(".").strip(), "", "", "", "",
                )

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


def _parse_year_at_end_plaintext(text, doi):
    """Parse 'Authors. Title. Journal Vol[, Pages] (YEAR).' without tags.

    Mirrors the tag-anchored logic but identifies the journal as the
    trailing sequence of capital-letter words preceded by ". "
    (covers multi-word abbreviations like "Nucleic Acids Res." and
    "FEBS J."). Returns None when the text doesn't end in "(YEAR)"
    or can't locate a journal-like suffix — caller falls through.
    """
    core = text.rstrip(".")
    ym = re.search(r"\(\s*(\d{4})[a-z]?\s*\)\s*$", core)
    if not ym:
        return None
    year = ym.group(1)
    core = core[: ym.start()].rstrip(" ,.")

    volume = pages = ""
    m = re.search(r"\s+(\d+),\s+([\w\-\u2013]+)\s*$", core)
    if m:
        volume = m.group(1)
        pages = m.group(2).replace("\u2013", "-")
        core = core[: m.start()].rstrip(" ,.")
    else:
        m = re.search(r"\s+(\d+)\s*$", core)
        if m:
            volume = m.group(1)
            core = core[: m.start()].rstrip(" ,.")
        else:
            # e.g. "Nucleic Acids Res. 1–18" or "F1000Res" alone
            m = re.search(r"\s+([\w\-\u2013]+)\s*$", core)
            if m and re.search(r"[\d\u2013\-]", m.group(1)):
                pages = m.group(1).replace("\u2013", "-")
                core = core[: m.start()].rstrip(" ,.")

    jm = re.search(
        r"(?:(?<=\.\s)|^)([A-Z][\w]*\.?(?:\s+[A-Z][\w]*\.?)*)$",
        core,
    )
    if not jm:
        return None
    journal = jm.group(1).rstrip(".").strip()
    head = core[: jm.start()].rstrip(" .")

    authors, title = _split_body_authors_title(head)
    if not authors and not title:
        return None

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": "",
        "pages": pages,
        "doi": doi,
        "authors": authors,
    }


def _split_body_authors_title(head):
    """Split head text into (authors list, title string).

    Recognizes "et al." as an anchor; otherwise walks the last dotted
    "LastName, I[. I.]" match or the last compact "LastName IN" match
    (Layout C). When no author pattern matches (e.g., consortium names
    like "The Cancer Genome Atlas Network"), emits authors=[] and
    title=head so the raw string is still searchable.
    """
    et_al = re.search(r"\b[Ee]t al\.?", head)
    if et_al:
        authors_str = head[:et_al.end()]
        title = head[et_al.end():].lstrip(" .").rstrip(".")
        return _parse_body_author_list(authors_str), title.strip()

    last_end = 0
    for m in _DOTTED_AUTHOR_RE.finditer(head):
        last_end = m.end()
    if not last_end:
        for m in _COMPACT_AUTHOR_RE.finditer(head):
            last_end = m.end()
    if last_end:
        authors_str = head[:last_end]
        title = head[last_end:].lstrip(" .").rstrip(".")
        return _parse_body_author_list(authors_str), title.strip()
    return [], head.strip()


def _parse_body_author_list(authors_str):
    """Extract "LastName IN" strings from the author section text."""
    # Normalize " and " / ", and " / " & " separators into ", " so the
    # greedy surname pattern below doesn't absorb the following author
    # ("Prat S and Willmitzer L" → "Prat S, Willmitzer L"). Seen in old
    # EMBO J reference lists.
    authors_str = re.sub(r"\s*,?\s+(?:and|&)\s+", ", ", authors_str)
    authors = []
    for m in re.finditer(
        r"([A-Z][\w\-']+(?:\s[\w\-']+)*),\s+((?:[A-Z]\.\s*){1,5})",
        authors_str,
    ):
        authors.append(format_name(m.group(2).strip(), m.group(1)))
    if authors:
        return authors
    for m in re.finditer(
        r"([A-Z][\w\-']+(?:\s[\w\-']+)*)\s+([A-Z]{1,5})(?=\s*(?:,|&|et al|\.|$))",
        authors_str,
    ):
        authors.append(format_name(m.group(2), m.group(1)))
    return authors


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].

    Prefers body parsing (anchored on <i>Journal</i>/<b>Volume</b> tags)
    and falls back to citation_reference meta tags only when body entries
    are fewer. Body parsing tolerates freeform meta content (no
    citation_title=...; k/v pairs), supplement parens, Layout B/C
    ordering, and non-digit volumes that the meta path can't resolve.
    """
    meta_refs = [
        {"": _parse_citation_reference(unescape(m.group(1)))}
        for m in re.finditer(
            r'<meta[^>]*name=["\']?citation_reference["\']?'
            r'[^>]*content="([^"]*)"',
            html,
        )
    ]
    body_refs = [
        {"": _parse_body_reference(m.group(1))}
        for m in re.finditer(
            r'<p[^>]*class=["\']?c-article-references__text["\']?[^>]*>'
            r'(.*?)(?=<p\s|</li>)',
            html,
            re.DOTALL,
        )
    ]
    return body_refs if len(body_refs) >= len(meta_refs) else meta_refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_keywords(html):
    """Extract article-specific keywords from Subjects list in body HTML.

    Uses c-article-subject-list (visible "Subjects" section) rather than
    JSON-LD keywords, which mix article keywords with journal categories.
    """
    keywords = []
    for m in re.finditer(
        r'<li[^>]*class=["\']?c-article-subject-list__subject["\']?[^>]*>'
        r'.*?<a[^>]*>([^<]+)</a>',
        html,
        re.DOTALL,
    ):
        kw = unescape(m.group(1)).strip()
        if kw:
            keywords.append(kw)
    return keywords


def _parse_abstract(html):
    """Extract abstract from <section data-title=Abstract>."""
    m = re.search(
        r'<section[^>]*data-title=["\']?Abstract["\']?[^>]*>(.*?)</section>',
        html,
        re.DOTALL,
    )
    if not m:
        return ""
    # Remove heading tags (e.g. <h2>Abstract</h2>) to avoid header leaking
    content = re.sub(r'<h[1-6][^>]*>.*?</h[1-6]>', '', m.group(1), flags=re.DOTALL)
    text = strip_tags(content).strip()
    # Safety net: strip leading "Abstract" if h-tag removal missed it
    if text.startswith("Abstract"):
        text = text[len("Abstract"):].strip()
    return text


def _extract_article(html):
    """Return the <article> element content, or full html as fallback."""
    m = re.search(r"<article[^>]*>(.*)</article>", html, re.DOTALL)
    return m.group(1) if m else html


def _section_boundaries(article):
    """Find all <section data-title=...> start positions and their titles.

    Returns list of (start_pos, end_of_opening_tag_pos, title) sorted by position.
    """
    entries = []
    for m in re.finditer(
        r'<section[^>]*data-title="([^"]*)"'
        r"|<section[^>]*data-title='([^']*)'"
        r"|<section[^>]*data-title=([^\s>\"']+)",
        article,
    ):
        title = m.group(1) or m.group(2) or m.group(3) or ""
        entries.append((m.start(), m.end(), unescape(title).strip()))
    return entries


def _find_start(article, sections):
    """Find main_text start: after Abstract and Inline Recommendations."""
    start = 0
    for i, (pos, tag_end, title) in enumerate(sections):
        if title.lower() in _PRE_BODY:
            # End of this section = start of next section
            next_pos = sections[i + 1][0] if i + 1 < len(sections) else len(article)
            if next_pos > start:
                start = next_pos
        else:
            break
    return start


def _remove_section(html, start_pattern):
    """Remove a <section> element matching start_pattern, handling nesting."""
    m = re.search(start_pattern, html)
    if not m:
        return html, False
    pos = m.end()
    depth = 1
    while depth > 0 and pos < len(html):
        next_open = re.search(r'<section[\s>]', html[pos:])
        next_close = re.search(r'</section>', html[pos:])
        if next_close is None:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos += next_open.end()
        else:
            depth -= 1
            pos += next_close.end()
    return html[:m.start()] + html[pos:], True


def _build_body(article, sections):
    """Build main_text HTML from two zones.

    Zone 1 (before first references): keep everything.
    Zone 2 (after first references): keep only supplementary sections.
    Remove all references sections.
    """
    # Find first references section position
    first_ref_idx = None
    for i, (pos, tag_end, title) in enumerate(sections):
        if title.lower() in _REF_SECTIONS:
            first_ref_idx = i
            break

    if first_ref_idx is None:
        # No references found — include all non-pre-body sections
        return None

    # Collect section ranges to include
    parts = []
    for i, (pos, tag_end, title) in enumerate(sections):
        tl = title.lower()
        # Skip pre-body sections
        if tl in _PRE_BODY:
            continue
        # Skip references sections
        if tl in _REF_SECTIONS:
            continue

        end = sections[i + 1][0] if i + 1 < len(sections) else len(article)

        if i < first_ref_idx:
            # Zone 1: keep everything
            parts.append((pos, end))
        else:
            # Zone 2: keep only supplementary sections
            if _SUPP_RE.search(title):
                parts.append((pos, end))

    return parts


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    Nature-specific: start is below Abstract and keywords/Inline Recommendations.
    """
    article = _extract_article(html)
    sections = _section_boundaries(article)

    if not sections:
        return ""

    parts = _build_body(article, sections)

    if parts is None:
        # No references found — use start/end fallback
        start = _find_start(article, sections)
        end = len(article)
        if start >= end:
            return ""
        parts = [(start, end)]
    elif not parts:
        # Fallback for articles without body sections (e.g. News & Views)
        m = re.search(r'<div[^>]*class=["\']?main-content[^>]*>', article)
        if not m:
            return ""
        start = m.end()
        # End at first references or end of article
        end = len(article)
        for pos, tag_end, title in sections:
            if title.lower() in _REF_SECTIONS and pos > start:
                end = pos
                break
        if start >= end:
            return ""
        parts = [(start, end)]

    # Extract abbreviation lists from pre-body sections (e.g. Inline Recommendations)
    abbr_html = ""
    for i, (pos, tag_end, title) in enumerate(sections):
        if title.lower() not in _PRE_BODY:
            break
        end = sections[i + 1][0] if i + 1 < len(sections) else len(article)
        pre_body = article[pos:end]
        for am in re.finditer(r'<dl[^>]*class=["\']?c-abbreviation[_-]list[^>]*>.*?</dl>',
                              pre_body, re.DOTALL):
            abbr_html += am.group(0)

    body_html = ""
    if abbr_html:
        body_html += "<h2>Abbreviations</h2><p></p>" + abbr_html
    for start, end in parts:
        body_html += article[start:end]

    # Remove any remaining references sections in the HTML
    while True:
        body_html, removed = _remove_section(
            body_html,
            r'<section[^>]*data-title=["\']?References["\']?[^>]*>'
        )
        if not removed:
            break

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Nature HTML into a papers/*.json-format dict."""
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
