"""PNAS (pnas.org) HTML parser."""

import re
from html import unescape
from urllib.parse import parse_qs, urlparse

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_all_meta,
    get_meta,
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
    "Open in Viewer",
    "Crossref",
    "PubMed",
    "Google Scholar",
)

# Reference heading pattern
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|supporting information|appendix',
    re.IGNORECASE,
)

# Site chrome headings
_CHROME_RE = re.compile(
    r'^further reading|^related articles|^you may also|^continue reading'
    r'|^sign up for|^metrics|^total views|^total citations'
    r'|^full text$|^actions$|^resources$|^on this page|^cite$'
    r'|^add to collections|^information\s*&|^view options'
    r'|^figures$|^tables$|^media$|^share$'
    r'|^request username|^create a new account|^login$|^change password',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize PNAS (Atypon) HTML to a single centered text column.

    Per format-html-extra.md the reading column starts at "Research
    Article" and ends before bottom chrome; the floating top bar
    (Info / Metrics / Link / Share icons) is stripped and the
    "Show all references" truncation stays collapsed. Removals fall
    into (a) items format-html-extra.md names, (b) ads, (c) toolbars.
    Content like "Cited By", related-articles carousels, and the
    references pop-up wrapper stays in the DOM.
    """
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    # (a) instruction-doc items --------------------------------------
    # Floating action-icon toolbar (Info / Metrics / Link / Share)
    # that format-html-extra.md names as "the floating top bar".
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass="[^"]*\bcore-fv__toolbar\b[^"]*"',
    )
    # (b) ads --------------------------------------------------------
    # "Sign up for PNAS alerts" newsletter-signup promo. Two markup
    # variants: `<section class=signup-ad>` (article-aside ad) and
    # `<div class=signup-alert-ad>` (in-column ad above references).
    html = _remove_nested_element(
        html,
        r'<section\b[^>]*\bclass=["\']?[^"\'>]*\bsignup-ad\b',
    )
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bsignup-alert-ad\b',
        )
        if html == before:
            break
    # (c) toolbars ---------------------------------------------------
    # Site <header class=main-header>: top nav / search / menu.
    # Inner <header data-extent=frontmatter> is article content and
    # stays.
    html = _remove_nested_element(
        html,
        r'<header\b[^>]*\bclass="[^"]*\bmain-header\b[^"]*"',
    )
    # Site <footer> chain (legal / feedback / contact bars).
    for _ in range(5):
        before = html
        html = _remove_nested_element(html, r"<footer\b[^>]*>")
        if html == before:
            break
    # Scroll-triggered sticky top toolbar (class=st-header) with
    # article title + PDF button.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bst-header\b(?!__)',
    )
    # Figure-viewer modal (<main class=core-fv__content>).
    html = _remove_nested_element(
        html,
        r'<main\b[^>]*\bclass="[^"]*\bcore-fv__content\b[^"]*"',
    )
    # Offscreen duplicate mobile nav menu.
    html = remove_elements_by_id(html, "main-menu")
    # Section-nav hamburger rail (.core-sections-menu: hamburger +
    # Abstract / Materials and Methods / ... TOC) and the collateral
    # icon rail (nav#article_collateral_menu: info / metrics / eye /
    # link / figures / table / play / share). The bell/bookmark/cite/
    # PDF panel (.info-panel) and citation metrics are kept.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass=(?:"[^"]*\bcore-sections-menu\b[^"]*"|core-sections-menu\b)',
    )
    html = _remove_nested_element(
        html,
        r'<nav\b[^>]*\bid=["\']?article_collateral_menu\b',
    )
    # Bottom chrome after the article body (per "ends before bottom
    # chrome" in format-html-extra.md):
    # - #cited-by__content: citation-metric block
    # - .article-further-reading: related-articles carousel
    # - .multi-search--grid: recommended-papers grid
    # - <section class=mt-2x>: trailing recommended-content popup
    html = remove_elements_by_id(html, "cited-by__content")
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass="[^"]*\barticle-further-reading\b[^"]*"',
    )
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass="[^"]*\bmulti-search--grid\b[^"]*"',
        )
        if html == before:
            break
    html = _remove_nested_element(
        html,
        r'<section\b[^>]*\bclass=["\']?mt-2x["\']?\s*>',
    )
    # References pop-up overlay widget (hover popup that shows when a
    # reference link is hovered). Not visible on initial render, but
    # its absolute-positioned container reports a bounding box below
    # docH. Treat as UI chrome.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass=["\']?[^"\'>]*\breferences-popup-wrapper\b',
    )
    # DOM patch: citation-metric counts. PNAS ships
    # `<span class=total-text data-count=XXX>0</span>` and relies on
    # client JS to replace the "0" with the `data-count` value at
    # render time. SingleFile captures before JS runs, so both Views
    # and Citations display "0". Substitute the real value (comma-
    # formatted) at capture time.
    def _patch_total_text(match):
        open_tag = match.group(1)
        count = int(match.group(2))
        close_tag = match.group(3)
        return f"{open_tag}{count:,}{close_tag}"
    html = re.sub(
        r'(<span\b[^>]*\bclass=["\']?[^"\'>]*\btotal-text\b[^"\'>]*["\']?[^>]*\bdata-count=["\']?(\d+)["\']?[^>]*>)'
        r'\s*0\s*'
        r'(</span>)',
        _patch_total_text,
        html,
        flags=re.IGNORECASE,
    )

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Cap <main>.
        "main{"
        "float:none !important;display:block !important;"
        "width:100% !important;max-width:752px !important;min-width:0 !important;"
        # padding-bottom trimmed: the article ends in a reference list
        # whose line-height leaves ~41 px of empty space below the last
        # text baseline. Target B = 56 px.
        "margin:0 auto !important;padding:56px 16px 15px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # Atypon's .article-container uses Bootstrap grid; collapse
        # grid/flex to block so the article body fills the wrapper.
        # All margins are zeroed (including mt-5's 3rem margin-top).
        ":root main .article-container,"
        ":root main .container,"
        ":root main .core-container,"
        ":root main .row,"
        ":root main [class*=col-]{"
        "display:block !important;float:none !important;"
        "width:auto !important;max-width:100% !important;min-width:0 !important;"
        "margin:0 !important;padding:0 !important;"
        "flex:0 0 auto !important}"
        # Header/core-container rule `header .core-container > *
        # {padding-left:1.5rem !important}` with specificity (0,2,2)
        # adds a 24-px inset on every direct child of the article
        # header's .core-container (meta-panel, title, authors).
        # Beat it with (1,2,3) to zero the left inset only — PNAS
        # relies on padding-top/bottom of h1, .core-self-citation and
        # .info-panel for the vertical rhythm between metadata rows.
        ":root body main header .core-container>*{"
        "padding-left:0 !important;padding-right:0 !important}"
        # `.core-self-citation` (journal name / volume / page / date
        # line) ships with margin-left:24px that pushes its contents
        # (including "March 31, 1998") 24 px inward from the H1/article
        # left edge, creating an uneven indent. Zero it.
        ":root main header .core-self-citation{"
        "margin-left:0 !important;margin-right:0 !important}"
        # Override the native `@media (max-width:767px)` rules on
        # `.info-panel__right-content` that overflow the container
        # (margin:0-1.5rem; padding:0 1.5rem; width:calc(100%+3rem))
        # and re-add a top border plus 0.75rem padding-top. At our
        # capped 752-px body width the narrow breakpoint always fires,
        # but we want the wide-viewport layout (side-by-side metrics +
        # buttons, no border above buttons). Reset those properties so
        # the base rules take over.
        ":root main .info-panel__right-content{"
        "border-block-start:0 !important;border-top:0 !important;"
        "margin:0 !important;padding:0 !important;"
        "padding-block-start:0 !important;width:auto !important}"
        ":root main .info-panel__left-content{width:auto !important}"
        # Override native `@media (max-width:575px){.info-panel{
        # justify-content:flex-start}}` which left-aligns the row at
        # very narrow viewports. Keep the default space-between so the
        # button cluster stays right-aligned at every width.
        ":root main .info-panel{justify-content:space-between !important}"
        # Hide the entire right-content cluster of .meta-panel (share
        # icons + crossmark badge). At narrow vw it wraps to its own
        # row and adds ~30 px of height, pushing the first rendered
        # text below target. Keep in DOM so parse_main_text output is
        # unchanged.
        ":root main .meta-panel__right-content{display:none !important}"
        # core-collateral is a position:fixed dialog holding metadata,
        # metrics, references, figures tabs that PNAS uses for the
        # toolbar popouts. It sits at x=100vw off-screen, but keep
        # display:none to guarantee it never appears visibly.
        # parse_references reads it, so it must stay in the DOM.
        ".core-collateral{display:none !important}"
        # Expand the truncated references list. PNAS caps the <div id=
        # bibliography-collapsible-text> at max-height:388px AND sets
        # display:none on refs past ~4 until the user clicks "Show all
        # references". Uncap, un-hide the hidden children, and hide the
        # button.
        "main #bibliography-collapsible-text{"
        "max-height:none !important;overflow:visible !important}"
        "main #bibliography-collapsible-text>*{display:flex !important}"
        "main .truncation-wrapper{display:none !important}"
        # `[data-method]::after` draws a 200-px gradient overlay at the
        # bottom of the references list to fade out the truncated
        # content. With max-height uncapped above, the overlay still
        # renders, hiding the last ~200 px of visible references behind
        # a semitransparent `#f6f6f6` panel.
        "main #bibliography-collapsible-text::after{"
        "content:none !important;display:none !important;"
        "background:none !important}"
        # "Open in Viewer" button overlaid on each figure. At narrow vw
        # it overflows horizontally. Hide — figure viewer modal is
        # already removed.
        "main .figure-pop-btn{display:none !important}"
        # Collapsed table wrappers (`<figure class=table><div class=
        # collapsible-wrapper collapsed style="overflow:hidden">`) hide
        # rows beyond the first ~280 px behind an "EXPAND FOR MORE"
        # button. Force the wrapper open and hide the now-redundant
        # button.
        "main .collapsible-wrapper.collapsed,"
        "main .collapsible-wrapper{"
        "max-height:none !important;height:auto !important;"
        "overflow:visible !important}"
        "main .collapsible-figure-btn,"
        "main .collapsible-figure-btn__wrapper{display:none !important}"
        # Body sections (abstract / bodymatter / backmatter etc. inside
        # `<article>`) get `padding:1.5rem` (24 px) horizontal under the
        # publisher's narrow-viewport `@media (max-width: 831px)` rule,
        # which the 752-px body cap forces unconditionally. `.figure-wrap`
        # also has its own `padding:.75rem` (12 px). Zero both so content
        # fills the column edge-to-edge.
        ":root main article section,"
        ":root main article .figure-wrap{"
        "padding-left:0 !important;padding-right:0 !important}"
        # Zero margin along the first-/last-descendant chain so
        # collapsed margins don't leak through main's padding, while
        # section titles deeper in the tree keep native margins
        # (32 px in PNAS typography).
        "main>*:first-child,"
        "main>*:first-child>*:first-child,"
        "main>*:first-child>*:first-child>*:first-child,"
        "main>*:first-child>*:first-child>*:first-child>*:first-child,"
        "main>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child,"
        "main>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child"
        "{margin-top:0 !important;padding-top:0 !important}"
        "main>*:last-child,"
        "main>*:last-child>*:last-child,"
        "main>*:last-child>*:last-child>*:last-child,"
        "main>*:last-child>*:last-child>*:last-child>*:last-child,"
        "main>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child,"
        "main>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child"
        "{margin-bottom:0 !important;padding-bottom:0 !important}"
        # Atypon applies a fixed 80-px margin-top to the <article> element
        # inside .article-container. The element is not :first-child
        # (there's a sibling <style> tag before it), so the rule above
        # misses; zero explicitly.
        "main .article-container>article{"
        "margin-top:0 !important;padding-top:0 !important}"
        # Clamp descendants so wide tables/figures don't overflow. Do
        # not force min-width:0 globally — PNAS gives reference-list
        # .label cells a 24-px min-width so numbers align, and blanket
        # zeroing shrinks them to single-digit-character width.
        "main *{max-width:100% !important}"
        "main table{table-layout:fixed !important;width:100% !important}"
        # Figures: pnas (Atypon) wraps each figure in
        #   <div class=figure-wrap>
        #     <header><div class=label>Fig. N.</div></header>
        #     <button class=figure-pop-btn>Open in Viewer</button>  (already hidden above)
        #     <figure id=fig<N> class=graphic>
        #       <img src='data:image/svg+xml,<placeholder>'
        #            style="background-image:var(--sf-img-N) ...">
        #       <figcaption>...</figcaption>
        #     </figure>
        #   </div>
        # The image is rendered via SingleFile's `background-image`
        # CSS-var trick (foreground src is transparent SVG). Native
        # `<figure>` has 40 px horizontal margin — zero it. Force the
        # img to display:block at full column width with the standard
        # 5 px caption gap. The background-image fills the box at
        # `background-size:100% 100%` so it scales with width:100%.
        ":root main article figure.graphic{"
        "margin:1rem 0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important;"
        "display:block !important}"
        ":root main article figure.graphic > img{"
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
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    """
    title = get_meta(html, "citation_title")
    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    volume = get_meta(html, "citation_volume")
    issue = get_meta(html, "citation_issue")
    doi = format_doi(get_meta(html, "citation_doi"))

    date = get_meta(html, "citation_publication_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_body_affiliations(html):
    """Extract author-to-affiliation mapping from core-collateral contributors tab.

    Each author is a ``<div id=conN property=author typeof=Person>`` block
    containing givenName, familyName, and nested
    ``<div property=affiliation typeof=Organization><span property=name>``
    elements.  Returns a dict mapping (givenName, familyName) -> [affiliation].
    """
    author_affs = {}
    for m in re.finditer(
        r'<div[^>]*\bid=con\d*\b[^>]*property=author[^>]*typeof=Person[^>]*>',
        html,
    ):
        # Bound the author block at the next author div (id=conN, with or
        # without a numeric suffix). Single-author papers use id=con only.
        rest = html[m.end():]
        next_author = re.search(
            r'<div[^>]*\bid=con\d*\b[^>]*property=author', rest,
        )
        end = m.end() + (next_author.start() if next_author else 5000)
        block = html[m.start():end]

        gn = re.search(r'property=givenName[^>]*>([^<]+)', block)
        fn = re.search(r'property=familyName[^>]*>([^<]+)', block)
        if not gn or not fn:
            continue
        given = gn.group(1).strip()
        family = fn.group(1).strip()

        affs = []
        for am in re.finditer(
            r'property=affiliation[^>]*typeof=Organization[^>]*>'
            r'.*?property=name[^>]*>(.*?)</span>',
            block, re.DOTALL,
        ):
            text = strip_tags(am.group(1)).strip()
            if text:
                affs.append(text)
        author_affs[(given, family)] = affs

    return author_affs


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name.
    Uses citation_author meta tags, falling back to body HTML structure
    (core-collateral contributors tab) when meta tags lack affiliations.
    """
    meta_authors = parse_meta_authors(html)
    authors = [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in meta_authors
    ]

    # If meta tags lack affiliations, try body HTML structure
    if not any(a["affiliation"] for a in authors):
        body_affs = _parse_body_affiliations(html)
        if body_affs:
            for i, meta_a in enumerate(meta_authors):
                # Match meta author name to body author by given/family name
                name = meta_a["name"]  # "LastName, Given" or "Given LastName"
                for (given, family), affs in body_affs.items():
                    if family in name and given.split()[0] in name:
                        authors[i]["affiliation"] = affs
                        break

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
    Fields are parsed from citation-content HTML (<em> for journal, <b> for
    volume) and the Google Scholar lookup URL (title, authors, year, pages, doi).
    """
    refs = []
    m = re.search(r'id=["\']?bibliography-collapsible-text["\']?', html)
    if not m:
        return refs

    # Scope to the bibliography section only (</section> closes it)
    bib_start = m.start()
    sec_end = re.search(r'</section>', html[bib_start:])
    bib_html = html[bib_start:bib_start + sec_end.start()] if sec_end else html[bib_start:]
    # Find all listitem starts
    items = [rm.start() for rm in re.finditer(
        r'<div[^>]*role=["\']?listitem["\']?', bib_html
    )]

    for i, start in enumerate(items):
        end = items[i + 1] if i + 1 < len(items) else len(bib_html)
        entry = bib_html[start:end]

        # Citation-content raw HTML (preserves <em>/<i>/<b> tags)
        cm = re.search(
            r'class=["\']?citation-content["\']?[^>]*>(.*?)</div>',
            entry, re.DOTALL,
        )
        if not cm:
            continue
        raw_html = cm.group(1)

        # Journal from <em> or <i>
        jm = re.search(r'<(em|i)>(.*?)</\1>', raw_html)
        journal = strip_tags(jm.group(2)).strip().rstrip('.') if jm else ""

        # Volume from <b>; fallback: text right after journal tag
        # ("Vol(Issue):Pages" or "Vol, Pages")
        volume = ""
        issue = ""
        post_pages = ""
        vm = re.search(r'<b>(\d+)</b>', raw_html)
        if vm:
            volume = vm.group(1)
            # Article numbers / page ranges appear between </b> and "(YEAR)"
            # in the modern Atypon layout: "<em>J</em> <b>10</b>, e66198 (2021)."
            after_b = raw_html[vm.end():]
            m_art = re.search(
                r'^[,\s]*'
                r'(?:\(([^)]+)\)[,\s]*)?'  # optional (issue)
                r'([A-Za-z]?[\w.\-\u2010-\u2014]+?)'
                r'\s*\(\d{4}\)',
                after_b,
            )
            if m_art:
                if m_art.group(1) and not issue:
                    issue = m_art.group(1).strip()
                tok = re.sub(r'[\u2010-\u2014]', '-', m_art.group(2)).strip('.,')
                if re.match(r'^[A-Za-z]?[\w.]+(-[A-Za-z]?[\w.]+)?$', tok):
                    post_pages = tok
        elif jm:
            after_journal = raw_html[jm.end():]
            after_text = strip_tags(after_journal).strip().lstrip(',').strip()
            # Match "Vol(Issue):Pages" e.g. "11(12):951-964"
            m_vip = re.match(
                r'(\d+)\s*(?:\((\d+[^)]*)\))?\s*[:,]\s*([\w\d\-\u2013\u2014]+)',
                after_text,
            )
            if m_vip:
                volume = m_vip.group(1)
                issue = m_vip.group(2) or ""
                post_pages = m_vip.group(3).replace('\u2013', '-').replace('\u2014', '-')

        # DOI from Crossref link
        doi = ""
        dm = re.search(r'href=(https://doi\.org/[^\s>"\']+)', entry)
        if dm:
            doi = unescape(dm.group(1))

        # Google Scholar lookup URL carries title, authors, year, pages.
        # The "scholar?q=..." search URL has none of these structured fields.
        title = ""
        year = ""
        pages = ""
        authors = []
        gs = re.search(
            r'href="(https://scholar\.google\.com/scholar_lookup\?[^"]*)"',
            entry,
        )
        if gs:
            gs_params = parse_qs(urlparse(unescape(gs.group(1))).query)
            title = gs_params.get('title', [''])[0]
            year = gs_params.get('publication_year', [''])[0]
            pages = gs_params.get('pages', [''])[0]
            if not doi:
                gs_doi = gs_params.get('doi', [''])[0]
                if gs_doi:
                    doi = format_doi(gs_doi)
            authors = [
                a.strip() for a in gs_params.get('author', []) if a.strip()
            ]

        # Fallback: parse year from "(YYYY)" in the citation text
        if not year:
            ym = re.search(r'\((\d{4})\)', strip_tags(raw_html))
            if ym:
                year = ym.group(1)

        # Fallback: parse title from text between (Year) and journal tag
        # Format: "Authors (Year) Title. Journal Vol(Issue):Pages."
        if not title and jm:
            pre_em = raw_html[:jm.start()]
            pre_text = strip_tags(pre_em).strip()
            # Match "(Year) Title."
            m_title = re.search(r'\(\d{4}\)\s*(.+?)\.\s*$', pre_text)
            if m_title:
                title = m_title.group(1).strip()

        # Fallback: parse authors from citation text.  Authors are
        # everything before either "(Year)" or before <em>/<i> if no year.
        if not authors and jm:
            pre_em = raw_html[:jm.start()]
            pre_text = strip_tags(pre_em).strip()
            # Cut off at "(YYYY)" if present
            m_year = re.search(r'\(\d{4}\)', pre_text)
            author_text = pre_text[:m_year.start()].strip() if m_year else pre_text
            author_text = author_text.rstrip(',').rstrip('.').strip()
            if author_text:
                # Split on " & " or ", " — handle "A B, C D & E F" patterns
                parts = re.split(r'\s*&\s*|,\s+(?=[A-Z])', author_text)
                authors = [
                    p.strip().rstrip('.').strip()
                    for p in parts if p.strip()
                ]

        # Use post-journal pages if GS URL didn't provide them
        if not pages and post_pages:
            pages = post_pages

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

def _find_h2_headings(html):
    """Find all h2 headings and positions."""
    entries = []
    for m in re.finditer(r'<h2[^>]*>(.*?)</h2>', html, re.DOTALL):
        text = strip_tags(m.group(1)).strip()
        if text:
            entries.append((m.start(), text))
    return entries


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    PNAS-specific: start from Abstract or Significance h2 (inclusive);
    remove site chrome headings; append keywords from meta tag.
    """
    # Find article container
    m = re.search(r'class="[^"]*article-container[^"]*"[^>]*>', html)
    if not m:
        return ""

    content = html[m.end():]
    h2s = _find_h2_headings(content)
    if not h2s:
        return ""

    # Find key section indices
    abstract_idx = None
    significance_idx = None
    for i, (pos, text) in enumerate(h2s):
        if text.lower() == "abstract":
            abstract_idx = i
        elif text.lower() == "significance":
            significance_idx = i

    # Start main_text from Significance or Abstract (whichever comes first)
    start = 0
    if significance_idx is not None and abstract_idx is not None:
        start = h2s[min(significance_idx, abstract_idx)][0]
    elif abstract_idx is not None:
        start = h2s[abstract_idx][0]
    elif significance_idx is not None:
        start = h2s[significance_idx][0]
    else:
        # No abstract or significance — look for articleBody directly
        ab = re.search(r'<[^>]*property=articleBody[^>]*>', content)
        if ab:
            start = ab.end()

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text) in enumerate(h2s):
        if _REF_RE.search(text) and pos >= start:
            first_ref_idx = i
            break

    # Build body: from abstract/significance through body to supplementary
    parts = []

    # Capture intro content before first h2 (only when start is not at an h2)
    first_h2_after_start = None
    for pos, text in h2s:
        if pos >= start:
            first_h2_after_start = pos
            break
    if first_h2_after_start and first_h2_after_start > start:
        parts.append((start, first_h2_after_start))

    for i, (pos, text) in enumerate(h2s):
        if pos < start:
            continue
        if _REF_RE.search(text):
            continue
        if _CHROME_RE.search(text.strip()):
            continue

        end = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)

        if first_ref_idx is None or i < first_ref_idx:
            parts.append((pos, end))
        else:
            if _SUPP_RE.search(text):
                parts.append((pos, end))

    if not parts:
        return ""

    body_html = ""
    for s, e in parts:
        body_html += content[s:e]

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    text = drop_noise(text, _NOISE)

    # Append keywords from meta tag
    kw_str = get_meta(html, "keywords")
    if kw_str:
        keywords = [k.strip() for k in kw_str.split(",") if k.strip()]
        if keywords:
            text += "\n\n## Keywords\n\n" + ", ".join(keywords)

    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse PNAS HTML into a papers/*.json-format dict."""
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
