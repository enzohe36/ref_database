"""eLife Sciences (elifesciences.org) HTML parser."""

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
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Download asset",
    "Open asset",
    "Request a detailed protocol",
    "\u2a2f",
    "Figure supplement",
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
    """Normalize eLife HTML to a single centered text column.

    Chrome stripped (Step 3):
      - Cookiebot consent dialog (``#CybotCookiebotDialog`` +
        ``#CybotCookiebotDialogBodyUnderlay``) and the mainMenu overlay.
      - ``<header class=site-header>``, ``<footer class=site-footer>``,
        and the primary site nav (``<nav class=nav-primary>``).
      - Article tab bar ``<nav class=tabbed-navigation>`` (Full text /
        Figures / Peer review / Side by side) and the jump-menu wrapper.
      - Download / share action blocks
        (``.article-download-links-list--js``).
      - Metrics section (``<section id=metrics>``) — per-publisher end
        anchor; everything below is chart chrome.
      - Highcharts SVG subtrees (multiple per article, loop removal).

    Reading column (Step 4): ``.wrapper--content-with-header-and-aside``
    is the highest common ancestor of title + authors + abstract + body
    + references. Cap at 752 px with 56 top/bottom and 16 side padding.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    html = _remove_nested_element(html, r"<header\b[^>]*class=[\"']?[^\"'>]*site-header[^\"'>]*[\"']?[^>]*>")
    html = _remove_nested_element(html, r"<footer\b[^>]*>")
    html = _remove_nested_element(html, r"<nav\b[^>]*class=[\"']?[^\"'>]*nav-primary[^\"'>]*[\"']?[^>]*>")
    html = remove_elements_by_id(
        html,
        "CybotCookiebotDialog",
        "CybotCookiebotDialogBodyUnderlay",
        "mainMenuOverlay",
        "metrics",
    )
    # Do NOT DOM-remove `#info` (author affiliations) or `#share` and
    # the trailing `.visuallyhidden` Download-links section — parse_article
    # reads from those to fill author affiliations and include the
    # self-DOI tail of main_text. They're hidden via CSS in the Step 4
    # block below so parser output stays bit-identical.
    # Article-navigation tab bar + per-section jump menu.
    html = _remove_nested_element(
        html, r"<nav\b[^>]*class=[\"']?[^\"'>]*tabbed-navigation[^\"'>]*[\"']?[^>]*>",
    )
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass=[\"\']?[^"\'>]*jump-menu__wrapper[^"\'>]*[\"\']?[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass=[\"\']?[^"\'>]*article-download-links-list[^"\'>]*[\"\']?[^>]*>',
    )
    # article-meta block (Categories and tags / research organism) —
    # hide via CSS rather than DOM remove. Removing it leaves the
    # download-links visuallyhidden section as the trailing element and
    # its 50 px height leaks into the document scrollHeight (which no
    # longer has a subsequent anchor to clamp). CSS-hiding keeps the
    # element in flow so the wrapper bottom lines up with the last
    # reference.
    # Highcharts SVG subtrees litter the "Metrics" dashboards; we've
    # already removed the #metrics section, but bare ``.highcharts-
    # container`` divs sometimes appear in article figures. Loop to
    # exhaust every occurrence.
    for _ in range(20):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass=[\"\']?[^"\'>]*highcharts-container[^"\'>]*[\"\']?[^>]*>',
        )
        if html == before:
            break
    # "email-cta" signup section and investor-logos strip live below
    # the main wrapper — not inside the reading column but still
    # contributing to doc height (and visible at wide viewports).
    html = _remove_nested_element(
        html,
        r'<section\b[^>]*\bclass=[\"\']?[^"\'>]*email-cta[^"\'>]*[\"\']?[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<ol\b[^>]*\bclass=[\"\']?[^"\'>]*investor-logos[^"\'>]*[\"\']?[^>]*>',
    )
    # Hypothes.is "speech-bubble" buttons render as <button> with a
    # placeholder span that sits at ``left:-9999px`` (per eLife's site
    # CSS) so the glyph stays offscreen until the annotator activates.
    # The element is ``position:relative``, so the text-bounds walker
    # can't skip it as a floated descendant — strip via DOM.
    for _ in range(60):
        before = html
        html = _remove_nested_element(
            html,
            r'<button\b[^>]*\bclass=[\"\']?[^"\'>]*speech-bubble[^"\'>]*[\"\']?[^>]*>',
        )
        if html == before:
            break

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important;"
        "overflow-y:overlay}"
        "html::-webkit-scrollbar{width:0}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:100% !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # The eLife page uses a 12-col grid inside
        # `.content-container-grid` and a 3-col aside layout inside
        # `.wrapper--content-with-header-and-aside` at desktop viewports.
        # Collapse both to plain block so the wrapper cap fully controls
        # the width.
        ".global-inner,.global-inner > div,"
        "main#maincontent,.main-content-grid,"
        ".content-container-grid,.content-header-grid-top,"
        ".content-header-grid__main,.content-header__body,"
        ".wrapper--content,.article-section{"
        "display:block !important;grid-template-columns:1fr !important;"
        "width:auto !important;max-width:100% !important;"
        "margin-left:0 !important;margin-right:0 !important;"
        "padding-left:0 !important;padding-right:0 !important}"
        # Capped reading column (Step 4).
        ".wrapper--content-with-header-and-aside{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "height:auto !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # The grid wrapper `.main-content-grid` ships with a grid-row
        # template that leaves a trailing empty row at the bottom; our
        # display:block override doesn't clear the rows, so hard-reset
        # grid props to block-stack behaviour.
        ".wrapper--content-with-header-and-aside .main-content-grid,"
        ".wrapper--content-with-header-and-aside .content-container-grid{"
        "display:block !important;grid-template-rows:none !important;"
        "grid-template-columns:none !important;"
        "grid-auto-rows:auto !important;gap:0 !important}"
        ".wrapper--content-with-header-and-aside *{"
        "max-width:100% !important;min-width:0 !important}"
        # Scroll-margin-top on article-section adds an unwanted 72 px
        # padding when the tab bar is present; kill only that.
        ".wrapper--content-with-header-and-aside .article-section{"
        "scroll-margin-top:0 !important}"
        # Hide only non-content blocks via CSS:
        #   - `#share`: self-DOI + social-sharer row (widget, not content)
        #   - `section.article-section.visuallyhidden`: the accessibility
        #     "Download links" anchor (empty section, leaks height when
        #     scripts don't hide it).
        # `#info` (author information, contributions, funding,
        # competing-interests, peer-review decision letter, author
        # response) is KEPT visible — it's article content.
        "#share,"
        "section.article-section.visuallyhidden{"
        "display:none !important}"
        # First-/last-child margin stacking — scope to direct wrapper
        # children only; deep-tree `*:first-child` flattens legitimate
        # section-header spacing inside #info.
        ".wrapper--content-with-header-and-aside > *:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        # `.content-header-grid-top` (holds "Research Article"
        # breadcrumb) ships margin-top:24 — bumps T from 56 to 81.
        # Zero it directly.
        ".wrapper--content-with-header-and-aside .content-header-grid-top{"
        "margin-top:0 !important}"
        ".wrapper--content-with-header-and-aside > *:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # Figures: elifesciences wraps each figure in
        #   <figure class=captioned-asset>
        #     <a class=captioned-asset__link
        #        href=https://iiif.elifesciences.org/lax/.../<id>-fig<N>-v2.tif/full/,1500/0/default.jpg>
        #       <picture class=captioned-asset__picture>
        #         <img src="data:..." class=captioned-asset__image
        #              srcset sizes>
        #       </picture>
        #     </a>
        #     <figcaption class=captioned-asset__caption>
        #       <h6>title</h6>
        #       <div class=caption-text__body><p>caption...
        #         <button class=caption-text__toggle--see-more>see more</button>
        #       </p></div>
        #       <span class=doi doi--asset><a>doi</a></span>
        #     </figcaption>
        #   </figure>
        # Native order: image above caption (correct). The high-res
        # IIIF JPEG is on the parent <a href> — get_refs.py uses
        # `_ELIFESCIENCES_FIGURES_FIX_JS` to swap <img src> ← <a href>.
        # Visual fixes: force img full-width above caption, and
        # expand the JS-clamped caption ("see more" button) so the
        # full caption text is readable without JS.
        ":root .wrapper--content-with-header-and-aside figure.captioned-asset{"
        "margin:1rem 0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important}"
        ":root .wrapper--content-with-header-and-aside "
        "a.captioned-asset__link,"
        ":root .wrapper--content-with-header-and-aside "
        "picture.captioned-asset__picture{"
        "display:block !important;margin:0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important}"
        ":root .wrapper--content-with-header-and-aside "
        "img.captioned-asset__image{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
        # JS-clamped caption: the publisher uses a "see more" toggle
        # implemented via a button + presumably max-height/overflow.
        # Hide the dead button and force full-text display.
        ":root .wrapper--content-with-header-and-aside "
        "button.caption-text__toggle{display:none !important}"
        ":root .wrapper--content-with-header-and-aside "
        ".caption-text__body{"
        "max-height:none !important;overflow:visible !important}"
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

def _parse_self_citation(html):
    """Parse the 'Cite this article' block for volume/pages.

    Format: <div class=reference__origin><i>eLife</i> <b>9</b>:e55438.</div>
    Returns (volume, pages).
    """
    m = re.search(
        r'<div[^>]*class="?reference__origin"?[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    if not m:
        return "", ""
    inner = m.group(1)
    vm = re.search(r"<b[^>]*>\s*([^<]+?)\s*</b>\s*:\s*([^<.]+)", inner)
    if not vm:
        return "", ""
    return vm.group(1).strip(), vm.group(2).strip()


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    eLife has no citation_* meta tags; uses dc.* meta tags plus the
    self-citation block for volume and elocation id.
    """
    title = get_meta(html, "dc.title")

    date = get_meta(html, "dc.date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    # DOI from dc.identifier: "doi:10.7554/eLife.55438"
    doi_raw = get_meta(html, "dc.identifier")
    doi = ""
    if doi_raw:
        doi_raw = doi_raw.replace("doi:", "").strip()
        doi = format_doi(doi_raw)

    # eLife has no citation_journal_* meta tags. application-name carries
    # "eLife"; og:site_name has the same value but uses property= which
    # get_meta does not match.
    journal = get_meta(html, "application-name")
    journal = journal.rstrip(".") if journal else ""

    # Volume + pages from self-citation block
    volume, pages = _parse_self_citation(html)

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": "",
        "pages": pages,
        "doi": doi,
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _format_elife_author(name):
    """Convert 'Given Middle Last' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Author list from dc.contributor meta tags (ordered).
    Affiliations from <ol class=authors-details__authors> — each
    <li class=authors-details__author> has <h4 class=author-details__name>
    for name and the FIRST <section class=author-details__section> without
    an <h5> heading holds the affiliation(s).
    """
    names = get_all_meta(html, "dc.contributor")

    # Build affiliation lookup keyed by full name
    aff_by_name = {}
    details_m = re.search(
        r'<ol[^>]*class="?authors-details__authors"?[^>]*>', html,
    )
    if details_m:
        # Slice to the closing </ol>
        pos = details_m.end()
        depth = 1
        end = len(html)
        while depth > 0 and pos < len(html):
            nxt_open = re.search(r"<ol[\s>]", html[pos:])
            nxt_close = re.search(r"</ol>", html[pos:])
            if nxt_close is None:
                break
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                pos += nxt_open.end()
            else:
                depth -= 1
                if depth == 0:
                    end = pos + nxt_close.start()
                    break
                pos += nxt_close.end()
        details = html[details_m.end():end]
        # Each author is inside <li class=authors-details__author>...<h4
        # class=author-details__name>Full Name</h4>... sections
        li_starts = [
            m.start() for m in re.finditer(
                r'<li[^>]*class="?authors-details__author', details,
            )
        ]
        for i, start in enumerate(li_starts):
            stop = li_starts[i + 1] if i + 1 < len(li_starts) else len(details)
            block = details[start:stop]
            name_m = re.search(
                r'<h4[^>]*class="?author-details__name"?[^>]*>(.*?)</h4>',
                block, re.DOTALL,
            )
            if not name_m:
                continue
            full_name = unescape(strip_tags(name_m.group(1))).strip()
            # First <section class=author-details__section> that does NOT
            # contain an <h5 class=author-details__heading>
            affs = []
            for sm in re.finditer(
                r'<section[^>]*class="?author-details__section"?[^>]*>(.*?)</section>',
                block, re.DOTALL,
            ):
                sec_inner = sm.group(1)
                if re.search(
                    r'<h5[^>]*class="?author-details__heading',
                    sec_inner,
                ):
                    continue
                # Affiliation text lives in <span class=author-details__text>
                # for single-affiliation authors and <li class=author-details__text>
                # inside <ol class=author-details__list> for multi-affiliation
                # authors. Match both tags.
                for spn in re.finditer(
                    r'<(?:span|li)[^>]*class="?author-details__text"?[^>]*>(.*?)</(?:span|li)>',
                    sec_inner, re.DOTALL,
                ):
                    text = unescape(strip_tags(spn.group(1))).strip()
                    if text:
                        affs.append(text)
                if affs:
                    break  # only the first affiliation-containing section
            aff_by_name[full_name] = affs

    return [
        {
            "author": _format_elife_author(n),
            "affiliation": aff_by_name.get(n, []),
        }
        for n in names
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_ref_entry(entry_html):
    """Parse a single <li class=reference-list__item> entry."""
    # Authors: each <li class=reference__author> contains text or <a>text</a>
    authors = []
    for am in re.finditer(
        r'<li[^>]*class="?reference__author"?[^>]*>(.*?)</li>',
        entry_html, re.DOTALL,
    ):
        txt = unescape(strip_tags(am.group(1))).strip()
        if txt:
            authors.append(txt)

    # Year from <span class=reference__authors_list_suffix>(YYYY)</span>
    year = ""
    ym = re.search(
        r'<span[^>]*class="?reference__authors_list_suffix"?[^>]*>\s*\(?\s*(\d{4})[a-z]?\s*\)?\s*</span>',
        entry_html,
    )
    if ym:
        year = ym.group(1)

    # Title from <a ... class=reference__title ...>TITLE</a> (href and
    # class attributes can appear in either order)
    title = ""
    doi = ""
    tm = re.search(
        r'<a[^>]*\bclass="?reference__title\b[^>]*>(.*?)</a>',
        entry_html, re.DOTALL,
    )
    if tm:
        tag = tm.group(0)
        title = unescape(strip_tags(tm.group(1))).strip()
        hm = re.search(r'href="?([^"\s>]+)', tag)
        if hm:
            href = unescape(hm.group(1))
            if "doi.org" in href:
                doi = format_doi(href)
    else:
        tm2 = re.search(
            r'<span[^>]*class="?reference__title\b[^"]*"?[^>]*>(.*?)</span>',
            entry_html, re.DOTALL,
        )
        if tm2:
            title = unescape(strip_tags(tm2.group(1))).strip()

    # DOI fallback from <a class=doi__link>
    if not doi:
        dm = re.search(
            r'<a[^>]*class="?doi__link"?[^>]*href="?([^"\s>]+)',
            entry_html,
        )
        if dm:
            doi = format_doi(unescape(dm.group(1)))

    # Origin: <div class=reference__origin><i>Journal</i> <b>Volume</b>:pages.</div>
    journal = volume = issue = pages = ""
    om = re.search(
        r'<div[^>]*class="?reference__origin"?[^>]*>(.*?)</div>',
        entry_html, re.DOTALL,
    )
    if om:
        origin = om.group(1)
        jm = re.search(r"<i[^>]*>(.*?)</i>", origin, re.DOTALL)
        if jm:
            journal = unescape(strip_tags(jm.group(1))).strip().rstrip(".")
        vm = re.search(r"<b[^>]*>(.*?)</b>", origin, re.DOTALL)
        if vm:
            volume = unescape(strip_tags(vm.group(1))).strip()
        # Pages: after the <b>...</b>: text
        pm = re.search(
            r"</b>\s*:?\s*([A-Za-z0-9\u2013\u2014\-]+)", origin,
        )
        if pm:
            pages = pm.group(1).replace("\u2013", "-").replace("\u2014", "-")

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
    """Extract the reference list."""
    refs = []
    for ol_m in re.finditer(
        r'<ol[^>]*class="?reference-list"?[^>]*>', html,
    ):
        pos = ol_m.end()
        depth = 1
        end = len(html)
        while depth > 0 and pos < len(html):
            nxt_open = re.search(r"<ol[\s>]", html[pos:])
            nxt_close = re.search(r"</ol>", html[pos:])
            if nxt_close is None:
                break
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                pos += nxt_open.end()
            else:
                depth -= 1
                if depth == 0:
                    end = pos + nxt_close.start()
                    break
                pos += nxt_close.end()
        section = html[ol_m.end():end]

        li_starts = [
            m.start() for m in re.finditer(
                r'<li[^>]*class="?reference-list__item', section,
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

def _extract_section_content(html, start_pos):
    """Return the inner HTML of the section starting at start_pos (after <section ...>)."""
    pos = start_pos
    depth = 1
    while depth > 0 and pos < len(html):
        nxt_open = re.search(r"<section[\s>]", html[pos:])
        nxt_close = re.search(r"</section>", html[pos:])
        if nxt_close is None:
            break
        if nxt_open and nxt_open.start() < nxt_close.start():
            depth += 1
            pos += nxt_open.end()
        else:
            depth -= 1
            if depth == 0:
                return html[start_pos:pos + nxt_close.start()]
            pos += nxt_close.end()
    return html[start_pos:pos]


_INFO_KEEP_HEADERS = re.compile(
    r"^(?:Acknowledg\w*|Ethics|Data availability)\s*$",
    re.IGNORECASE,
)


def _extract_info_subsections(info_html):
    """Return the Acknowledgements and Ethics subsection HTML from info."""
    parts = []
    for m in re.finditer(
        r'<section[^>]*class=(?:"[^"]*article-section[^"]*"|[^"\s>]*article-section[^"\s>]*)[^>]*>',
        info_html,
    ):
        content = _extract_section_content(info_html, m.end())
        hm = re.search(
            r'<h[2-4][^>]*class="?article-section__header_text"?[^>]*>(.*?)</h[2-4]>',
            content, re.DOTALL,
        )
        if not hm:
            continue
        header = unescape(strip_tags(hm.group(1))).strip()
        if _INFO_KEEP_HEADERS.match(header):
            parts.append(content)
    return "\n".join(parts)


def _find_article_sections(html):
    """Find top-level <section class="article-section ..."> elements only.

    Nested subsections inside a top-level section are filtered out so their
    content is not duplicated. Returns list of (id_value, tag_start_pos,
    content_html). id may be empty (e.g. Acknowledgements, Ethics).
    """
    raw = []
    for m in re.finditer(
        r'<section[^>]*class=(?:"[^"]*article-section[^"]*"|[^"\s>]*article-section[^"\s>]*)[^>]*>',
        html,
    ):
        tag = m.group(0)
        sid = ""
        idm = re.search(r'id="?([^"\s>]+)', tag)
        if idm:
            sid = idm.group(1)
        content = _extract_section_content(html, m.end())
        raw.append((sid, m.start(), m.end(), content))

    # Filter: keep only sections not nested within another article-section.
    # A section at position P is nested if another section's [start, end]
    # range fully contains P.
    out = []
    for sid, tag_start, tag_end, content in raw:
        nested = False
        for osid, ot_start, ot_end, ocontent in raw:
            if ot_start == tag_start:
                continue
            outer_end = ot_end + len(ocontent) + len("</section>")
            if ot_start < tag_start < outer_end:
                nested = True
                break
        if not nested:
            out.append((sid, tag_end, content))
    return out


def _parse_main_text(html):
    """Extract body text.

    Keeps article-section blocks from abstract through the section before
    id=references. Drops 'info' and 'metrics' sections.
    """
    sections = _find_article_sections(html)
    if not sections:
        return ""

    keep = []
    drop_ids = {"references", "metrics"}
    for sid, _, content in sections:
        sid_lc = sid.lower()
        if sid_lc in drop_ids:
            continue
        if sid_lc == "info":
            # Keep only Acknowledgements / Ethics subsections from info.
            keep.append((sid, _extract_info_subsections(content)))
            continue
        keep.append((sid, content))

    if not keep:
        return ""

    body_html = ""
    for sid, content in keep:
        # Rebuild the section with its header from the captured inner HTML
        body_html += "\n" + content + "\n"

    # Append supplementary sections that appear after references
    for sid, _, content in sections:
        sid_lc = sid.lower()
        if sid_lc in drop_ids or sid_lc == "references":
            continue
        # If this section appears after references in document order, include
        # only when heading matches _SUPP_RE.
        if _SUPP_RE.search(sid_lc):
            body_html += "\n" + content + "\n"

    # Strip asset-viewer controls, download buttons, etc.
    body_html = _remove_nested_element(
        body_html, r'<a[^>]*class="[^"]*download[^"]*"[^>]*>',
    )
    # Remove SVG icons fully
    body_html = re.sub(r"<svg[^>]*>.*?</svg>", "", body_html, flags=re.DOTALL)

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse eLife HTML into a papers/*.json-format dict."""
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
