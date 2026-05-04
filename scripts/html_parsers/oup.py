"""Oxford University Press (oup.com) HTML parser."""

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
    "Google Scholar",
    "Crossref",
    "Search ADS",
    "PubMed",
    "OpenURL",
    "WorldCat",
)

# Reference section heading pattern
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# h2 classes for body vs back matter vs references
_BODY_HEADING = "section-title"
_BACK_HEADING = "backsection-title"
_REF_HEADING = "backreferences-title"
_ABSTRACT_HEADING = "abstract-title"


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------


def _extract_nested(html, start_pattern):
    """Return (match_start, match_end, inner_html) for the first element
    matching start_pattern, with the matching close tag located by
    tracking same-tag nesting. Returns None when no match.
    """
    m = re.search(start_pattern, html, re.DOTALL)
    if not m:
        return None
    tag_m = re.match(r"<(\w+)", m.group())
    if not tag_m:
        return None
    tag = tag_m.group(1)
    pos = m.end()
    depth = 1
    open_pat = re.compile(rf"<{tag}[\s>]", re.IGNORECASE)
    close_pat = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
    while depth > 0 and pos < len(html):
        next_open = open_pat.search(html, pos)
        next_close = close_pat.search(html, pos)
        if next_close is None:
            return None
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            pos = next_close.end()
    return m.start(), pos, html[m.start():pos]


def _inline_metadata_widgets(html):
    """Fill an empty `<div class="article-metadata-panel ...
    at-ArticleMetadata">` placeholder with Keywords + Topic + Issue
    Section so it renders as the single-box layout academic.oup.com
    shows natively.

    On oup.com, JS moves the three bands into that placeholder ~5–18 s
    after load. When SingleFile captures before the JS settles (slow
    network or cold cache) the placeholder is empty and the Topic /
    Issue widgets still sit at the end of the article. This reproduces
    the move statically. When the placeholder is already populated
    (late-capture case) the function is a no-op.
    """
    # Locate the placeholder. Match both "clearfix at-ArticleMetadata"
    # and "at-ArticleMetadata clearfix" orderings defensively.
    ph = _extract_nested(
        html,
        r'<div[^>]*\bclass="[^"]*\bat-ArticleMetadata\b[^"]*"[^>]*>',
    )
    if not ph:
        return html
    ph_start, ph_end, ph_full = ph
    # Find the opening tag end to inspect inner HTML
    ot_end = html.find(">", ph_start) + 1
    inner = html[ot_end:ph_end - len("</div>")].strip()
    if len(inner) > 100:
        # Already populated by runtime JS — leave alone.
        return html

    # Extract Keywords, Topic, and the SolrResourceMetadata's inner
    # panel (which holds article-metadata-taxonomies +
    # article-metadata-tocSections as siblings). Use the INNER classes
    # rather than the outer widget wrappers so the injected content
    # sits as a direct child of the at-ArticleMetadata panel — matching
    # what native academic.oup.com's JS does (it moves the children of
    # .article-metadata-panel.solr-resource-metadata, not the panel
    # itself, into the placeholder; and the outer .article-metadata-
    # panel has `>div:first-of-type{border-top: ...}` which would
    # render a line above Issue Section if any wrapper were kept).
    kw = _extract_nested(
        html,
        r'<div\b[^>]*\bclass=["\']?kwd-group\b',
    )
    rt = _extract_nested(
        html,
        r'<div\b[^>]*\bclass=["\']?related-topic-tags\b',
    )
    # The inner SolrResourceMetadata panel — we pull its INNER HTML
    # (its direct children: article-metadata-taxonomies, article-
    # metadata-tocSections, and anything else the publisher adds on
    # other papers) so they end up as siblings inside the placeholder.
    rm_panel = _extract_nested(
        html,
        r'<div\b[^>]*\bclass="article-metadata-panel solr-resource-metadata[^"]*"',
    )
    if not (kw or rt or rm_panel):
        return html

    # Also remove the outer widget wrappers so Topic / Issue aren't
    # duplicated at the end of the article body.
    rt_widget = _extract_nested(
        html,
        r'<div\b[^>]*\bclass="widget widget-RelatedTags\b[^"]*"',
    )
    rm_widget = _extract_nested(
        html,
        r'<div\b[^>]*\bclass="widget widget-SolrResourceMetadata\b[^"]*"',
    )

    # Extract the rm_panel's inner-HTML (its direct children) rather
    # than the panel wrapper itself.
    rm_inner = ""
    if rm_panel:
        rm_start, rm_end, rm_full = rm_panel
        rm_open_end = rm_full.find(">") + 1
        rm_inner = rm_full[rm_open_end:-len("</div>")]

    # Remove originals (highest offset first so earlier removals don't
    # shift later offsets). Remove widget wrappers rather than just
    # the inner divs to drop all the related chrome at the end.
    found = [x for x in (kw, rt_widget, rm_widget) if x]
    found.sort(key=lambda x: x[0], reverse=True)
    kw_html = kw[2] if kw else ""
    rt_html = rt[2] if rt else ""
    for start, end, _inner in found:
        html = html[:start] + html[end:]

    # After removing, re-locate the placeholder (its offset may have
    # shifted if any of the removed blocks preceded it).
    ph2 = _extract_nested(
        html,
        r'<div[^>]*\bclass="[^"]*\bat-ArticleMetadata\b[^"]*"[^>]*>',
    )
    if not ph2:
        return html
    ph2_start, ph2_end, _ = ph2
    ph2_ot_end = html.find(">", ph2_start) + 1

    # Build the populated placeholder: Keywords → Topic →
    # (taxonomies + Issue Section, etc. — whatever the inner solr panel
    # held as siblings).
    filled = kw_html + rt_html + rm_inner
    return (
        html[:ph2_ot_end]
        + filled
        + html[ph2_end - len("</div>"):]
    )


def remove_banners(html):
    """Normalize OUP (Silverchair) HTML to a single centered text column.

    Per format-html-extra.md the reading column spans "Journal Article"
    through the last content before "Comments". Removals fall into
    three buckets: (a) items format-html-extra.md names (cookie
    consent, opacity overlay, reading-column anchors), (b) ads,
    (c) toolbars. The right-side #Sidebar (related-taxonomies panel)
    stays in the DOM.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    # (a) instruction-doc items --------------------------------------
    # OneTrust cookie consent (banner + overlay + preference modal).
    html = remove_elements_by_id(html, "onetrust-consent-sdk")
    # Semi-transparent grey full-viewport blocker inline style.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bstyle="[^"]*\bz-index:2000000000[^"]*\bopacity:0\.05',
    )
    # <section class=master-header>: site nav / journal banner above
    # "Journal Article" start anchor.
    html = _remove_nested_element(
        html,
        r'<section\b[^>]*\bclass="[^"]*\bmaster-header\b[^"]*"',
    )
    # <section class=footer_wrap>: site footer (below the "Comments"
    # end anchor).
    html = _remove_nested_element(
        html,
        r'<section\b[^>]*\bclass="[^"]*\bfooter_wrap\b[^"]*"',
    )
    # Bottom #Comment section: left in the DOM and hidden via CSS
    # below. parse_main_text picks up the comment-modal boilerplate
    # ("Add comment / ..."), so removing the DOM would break parity.
    # (b) ads --------------------------------------------------------
    html = remove_elements_by_id(
        html,
        "adBlockHeader", "adBlockMainBodyTop", "adBlockMainBodyBottom",
        "adBlockFooter", "adBlockStickyFooter",
    )
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass="[^"]*\bHeaderRevealerAd\b[^"]*"',
    )
    # (c) toolbars ---------------------------------------------------
    # Left sidebar (#InfoColumn): issue info, Download/Cite/Share
    # panel, article-section nav, sticky toolbar.
    # Right sidebar (#Sidebar): related-taxonomies panel. Reflows
    # BELOW ContentColumn once the layout is frozen to 720 px, so it
    # becomes bottom chrome in the capped layout and honors the
    # "ends before Comments" anchor.
    html = remove_elements_by_id(html, "InfoColumn", "Sidebar")
    # "Article Navigation" mobile toggle box that sits above the
    # article body at narrow viewport widths.
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass="[^"]*\barticle-browse-top\b[^"]*"',
        )
        if html == before:
            break

    # Fill the article-metadata box (Keywords / Topic / Issue Section)
    # with the three widgets from the end of the article body. No-op
    # when the retrieval already captured the post-JS state.
    html = _inline_metadata_widgets(html)


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
        # Cap ContentColumn — the article body lives here. With
        # InfoColumn removed, it stands alone and can fill the wrapper.
        "div#ContentColumn{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # Silverchair's .center-inner-row and .master-main apply a
        # display:grid with named columns for InfoColumn + ContentColumn
        # + Sidebar. With the siblings removed, collapse the grid so
        # ContentColumn is placed at full width.
        ":root body .master-main,"
        ":root body .center-inner-row,"
        ":root body .content-main,"
        ":root body .master-container{"
        "display:block !important;float:none !important;"
        "width:auto !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important}"
        # Zero margin along the first-/last-descendant chain so
        # collapsed margins don't leak through the wrapper's padding,
        # while section titles deeper in the tree keep native margins.
        "div#ContentColumn>*:first-child,"
        "div#ContentColumn>*:first-child>*:first-child,"
        "div#ContentColumn>*:first-child>*:first-child>*:first-child,"
        "div#ContentColumn>*:first-child>*:first-child>*:first-child>*:first-child,"
        "div#ContentColumn>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child,"
        "div#ContentColumn>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child"
        "{margin-top:0 !important}"
        "div#ContentColumn>*:last-child,"
        "div#ContentColumn>*:last-child>*:last-child,"
        "div#ContentColumn>*:last-child>*:last-child>*:last-child,"
        "div#ContentColumn>*:last-child>*:last-child>*:last-child>*:last-child,"
        "div#ContentColumn>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child,"
        "div#ContentColumn>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child"
        "{margin-bottom:0 !important}"
        # Clamp every descendant so fixed-width tables or figures don't
        # overflow at narrow vw.
        "div#ContentColumn *{max-width:100% !important;min-width:0 !important}"
        "div#ContentColumn table{table-layout:fixed !important;"
        "width:100% !important}"
        # Hide the Comments section, modal, and "Add comment" button —
        # must stay in the DOM so parse_main_text picks up the same
        # strings it did before remove_banners, but should not render.
        # The actual markup uses <div class=comments> with inner
        # #usercomments, plus #divCommentModal and #comment-modal-opener.
        "div#ContentColumn div.comments,"
        "div#divCommentModal,div#Comment,"
        "a#comment-modal-opener,#comment-modal-opener{display:none !important}"
        # Figures: oup (Silverchair, same family as aacrjournals) wraps
        # each figure in
        #   <div data-id=<doi>-f<N> class="fig fig-section js-fig-section">
        #     <div class=graphic-wrap>
        #       <img class=content-image src="data:..." data-path-from-xml=<file>.tif>
        #     </div>
        #     <div class=graphic-bottom>
        #       <div class="label fig-label">Figure N.</div>
        #       <div class="caption fig-caption">...</div>
        #       <div class="fig-orig original-slide figure-button-wrap">
        #         <a class=fig-view-orig href=https://academic.oup.com/view-large/figure/...>
        #            View Large</a>
        #         <a class=ppt>Download Slide</a>
        #       </div>
        #     </div>
        #   </div>
        # Native order: image above caption (correct). The img is
        # inlined directly — no separate hi-res link reachable as a
        # direct image URL (view-large is a sub-page); get_refs.py
        # extension deferred. Visual fixes: force img full-width above
        # caption, hide JS-only "View Large" / "Download Slide" buttons
        # (non-functional inside saved HTML).
        "div#ContentColumn div.fig.fig-section{"
        "margin:1rem 0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important;"
        "display:block !important}"
        "div#ContentColumn div.fig.fig-section .graphic-wrap{"
        "margin:0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important}"
        "div#ContentColumn div.fig.fig-section img.content-image{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
        # Drop the JS-only "View Large" / "Download Slide" button row
        # inside .fig-orig.
        "div#ContentColumn div.fig.fig-section .fig-orig.figure-button-wrap{"
        "display:none !important}"
        # Hide the modal-clone copy of the figure that Silverchair
        # appends after each .fig-section (`<div class='fig fig-modal
        # reveal-modal'>` — same fingerprint as aacrjournals — hidden
        # via JS `aria-hidden=true`).
        "div#ContentColumn div.fig.fig-modal{display:none !important}"
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

def _parse_citation_primary(html):
    """Parse journal, volume, issue, pages, DOI from ww-citation-primary div.

    Format: '<em>Journal</em>, Volume X, Issue Y, Date, Pages N-M, <a href=DOI>'
    or: '<em>Journal</em>, Volume X, Issue Y, Date, elocator, <a href=DOI>'
    """
    m = re.search(
        r'class=ww-citation-primary[^>]*>(.*?)</div>',
        html,
        re.DOTALL,
    )
    if not m:
        return {}, ""

    content = m.group(1)

    # Journal name from <em>
    journal = ""
    jm = re.search(r'<em>(.*?)</em>', content)
    if jm:
        journal = strip_tags(jm.group(1)).strip()

    # DOI from link
    doi = ""
    dm = re.search(r'href=["\']?(https?://doi\.org/[^"\'>\s]+)', content)
    if dm:
        doi = dm.group(1)

    text = strip_tags(content).strip()

    # Volume
    volume = ""
    vm = re.search(r'Volume\s+(\d+)', text)
    if vm:
        volume = vm.group(1)

    # Issue
    issue = ""
    im = re.search(r'Issue\s+(\d+)', text)
    if im:
        issue = im.group(1)

    # Pages
    pages = ""
    pm = re.search(r'Pages?\s+(\S+)', text)
    if pm:
        pages = pm.group(1).rstrip(",")
        # Normalize dash
        pages = re.sub(r'[–—]', '-', pages)
    if not pages:
        # eLocator ID: token before DOI link, after date
        # e.g. "..., 24 April 2025, gkaf299, https://doi.org/..."
        em = re.search(r',\s*([a-z]{2,}[\d]+)\s*,\s*https?://doi', text)
        if em:
            pages = em.group(1)
    if not pages:
        # Royal Society format: "Journal (YYYY) Vol (Issue): elocator ."
        em = re.search(r'\(\s*\d+\s*\)\s*:\s*(\S+?)\s*\.?$', text.strip())
        if em:
            pages = em.group(1).rstrip(".,")

    # Year from date string (e.g. "30 October 2015" or "July 2019")
    year = ""
    ym = re.search(r'(\d{4})', text)
    if ym:
        year = ym.group(1)

    return {
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
    }, doi


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    OUP-specific: tries citation_ meta tags first (newer format), falls back to
    ww-citation-primary div (older format).
    """
    # Try meta tags first
    title = get_meta(html, "citation_title")
    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    volume = get_meta(html, "citation_volume")
    issue = get_meta(html, "citation_issue")
    doi_raw = get_meta(html, "citation_doi")
    date = get_meta(html, "citation_publication_date")
    year = ""
    if date:
        ym = re.search(r"(\d{4})", date)
        if ym:
            year = ym.group(1)

    # Fall back to ww-citation-primary for missing fields
    if not title or not journal:
        citation, _ = _parse_citation_primary(html)
        if not title:
            m = re.search(r'og:title[^>]*content="([^"]+)"', html)
            if m:
                title = unescape(m.group(1)).strip()
        if not journal:
            journal = citation.get("journal", "")
        if not volume:
            volume = citation.get("volume", "")
        if not issue:
            issue = citation.get("issue", "")
        if not year:
            year = citation.get("year", "")
        if not doi_raw:
            doi_raw = citation.get("doi", "")

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage
    if not pages:
        citation, _ = _parse_citation_primary(html)
        pages = citation.get("pages", "")

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": format_doi(doi_raw),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name.
    Tries citation_author meta tags first (newer OUP format with comma-
    separated LastName, Given). Falls back to al-author-name links
    (older format, full display names).

    When meta citation_author_institution is missing for an author
    (common on older NAR / Genetics papers), falls back to the
    <div class="info-card-author"> author-popup widget which carries
    each author's full-name display plus an optional <div class=aff>.
    """
    # Build name -> affiliation lookup from info-card author widgets.
    info_card_affs = {}
    for m in re.finditer(
        r'<div\s+class="info-card-author[^"]*"[^>]*>(.*?)(?=<div\s+class="info-card-author|\Z)',
        html, re.DOTALL,
    ):
        block = m.group(1)
        name_m = re.search(
            r'<div\s+class=info-card-name[^>]*>\s*([^<]+?)\s*(?:<|$)',
            block,
        )
        if not name_m:
            continue
        display = unescape(name_m.group(1)).strip()
        if not display:
            continue
        affs = []
        # <div class=aff> may contain a nested <div class=institution>
        # that closes before the outer aff div. Walk the block with a
        # depth counter so the full aff text (institution + address) is
        # captured rather than just the institution stub.
        pos = 0
        while pos < len(block):
            am = re.search(r'<div\s+class=aff\b[^>]*>', block[pos:])
            if not am:
                break
            start = pos + am.end()
            depth = 1
            p = start
            while depth > 0 and p < len(block):
                no = re.search(r'<div[\s>]', block[p:])
                nc = re.search(r'</div>', block[p:])
                if not nc:
                    break
                if no and no.start() < nc.start():
                    depth += 1
                    p += no.end()
                else:
                    depth -= 1
                    if depth == 0:
                        inner = block[start:p + nc.start()]
                    p += nc.end()
            else:
                inner = block[start:p]
            # Drop <span class="label title-label">N</span> superscripts
            txt = re.sub(r'<span[^>]*class="?label[^>]*>.*?</span>', '', inner, flags=re.DOTALL)
            txt = unescape(re.sub(r'<[^>]+>', ', ', txt))
            txt = re.sub(r'\s*,\s*,\s*', ', ', txt).strip(' ,')
            txt = re.sub(r'\s+', ' ', txt).strip()
            if txt:
                affs.append(txt)
            pos = p
        if affs:
            info_card_affs[display] = affs

    def lookup_info_card(meta_name):
        """Match a citation_author display name to the info-card lookup.
        citation_author is "Last, Given"; info-card is "Given Last".
        """
        if meta_name in info_card_affs:
            return info_card_affs[meta_name]
        if "," in meta_name:
            last, given = meta_name.split(",", 1)
            flipped = f"{given.strip()} {last.strip()}"
            if flipped in info_card_affs:
                return info_card_affs[flipped]
        return []

    # Try meta tags first
    meta_authors = parse_meta_authors(html)
    if meta_authors:
        result = []
        for a in meta_authors:
            affs = a.get("affiliations", [])
            if not affs:
                affs = lookup_info_card(a["name"])
            result.append({
                "author": format_author_name(a["name"]),
                "affiliation": affs,
            })
        # Shared-affiliation broadcast. Two safe patterns:
        # Rule A: when >=2 authors already carry the same single
        #   affiliation string, broadcast it to empty-aff authors.
        #   Skips cases where different authors have different affs
        #   (don't overwrite publisher-specified mappings) and cases
        #   where only one author has an aff (avoids attributing a
        #   single corresponding-author lab to unrelated co-authors).
        # Rule B: when ONLY the last author carries affs and >=2 other
        #   authors are empty (typical of older OUP meta where the
        #   citation_author_institution tags all cluster at the end
        #   and parse_meta_authors attaches them to the last author),
        #   broadcast the last author's affs to all empty authors.
        #   Narrow on purpose — requires last-author-only, not
        #   mid-stream singletons (Wang_2006 where the single aff
        #   belongs to a non-last author stays unbroadcast).
        with_affs = [a for a in result if a["affiliation"]]
        unique_affs = {tuple(a["affiliation"]) for a in with_affs}
        if len(with_affs) >= 2 and len(unique_affs) == 1:
            shared = list(next(iter(unique_affs)))
            for a in result:
                if not a["affiliation"]:
                    a["affiliation"] = list(shared)
        elif (len(result) >= 3 and result[-1]["affiliation"]
                and len(with_affs) == 1
                and sum(1 for a in result if not a["affiliation"]) >= 2):
            shared = list(result[-1]["affiliation"])
            for a in result:
                if not a["affiliation"]:
                    a["affiliation"] = list(shared)
        return result

    # Fallback: body HTML author links
    authors = []
    seen = set()
    for m in re.finditer(
        r'class="al-author-name[^"]*"[^>]*>.*?'
        r'<a[^>]*>([^<]+)</a>',
        html,
        re.DOTALL,
    ):
        name = unescape(m.group(1)).strip()
        if name and name not in seen and not name.startswith("http"):
            seen.add(name)
            affs = info_card_affs.get(name, [])
            authors.append({
                "author": name,
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
    Each reference has structured fields: surname, given-names, article-title,
    source, year, volume, fpage, lpage.
    """
    refs = []
    # Find ref-list div (quoted or unquoted class attribute)
    m = re.search(r'class=(?:"[^"]*ref-list[^"]*"|ref-list\b)', html)
    if not m:
        return refs

    ref_section = html[m.start():]

    # Split by ref-item boundaries (each ref lives in a js-splitview-ref-item).
    # Match only ref-item divs to exclude footnotes/copyright that follow.
    items = list(re.finditer(
        r'<div\s+content-id=\S+\s+class=js-splitview-ref-item\b', ref_section,
    ))
    if not items:
        # Portland Press and older OUP: data-content-id attribute, no
        # js-splitview-ref-item class on the outer wrapper.
        items = list(re.finditer(r'<div\s+(?:data-)?content-id=\S+', ref_section))
    if not items:
        return refs

    # Find end boundary for the last ref entry: the first div after it
    # that is NOT a ref-item (copyright, widget, footnote, etc.).
    last_end = len(ref_section)
    after_last = ref_section[items[-1].end():]
    boundary = re.search(
        r'<div\s+class=["\']?(?:widget|copyright|license|footnote)\b',
        after_last,
    )
    if boundary:
        last_end = items[-1].end() + boundary.start()

    for idx, im in enumerate(items):
        end = items[idx + 1].start() if idx + 1 < len(items) else last_end
        entry = ref_section[im.start():end]

        # Structured fields. Inner content may contain tags (e.g. <em>),
        # so match lazily up to </div> and strip any inner tags.
        def _field(cls):
            fm = re.search(
                rf'class=(?:"{cls}"|{cls}\b)[^>]*>(.*?)</div>',
                entry, re.DOTALL,
            )
            return strip_tags(fm.group(1)).strip() if fm else ""

        title = _field("article-title")
        if not title:
            title = _field("chapter-title")

        journal = _field("source").replace(".", "")
        volume = _field("volume")
        issue = _field("issue")
        year = _field("year")
        fpage = _field("fpage")
        lpage = _field("lpage")
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # Fallback: extract pages from inline text (older format uses
        # <strong>vol</strong>, pages instead of fpage/lpage divs)
        if not pages:
            pm = re.search(
                r'<strong>\s*\d+\s*</strong>\s*,\s*'
                r'(\d[\d\w]*)\s*[–—-]\s*(\d[\d\w]*)',
                entry,
            )
            if pm:
                pages = f"{pm.group(1)}-{pm.group(2)}"
            else:
                pm = re.search(
                    r'[\s,;:]\s*(\d+)\s*[–—-]\s*(\d+)\s*\.?\s*(?:<|$)',
                    entry,
                )
                if pm:
                    pages = f"{pm.group(1)}-{pm.group(2)}"

        # Authors. Surname and given-names divs may be separated by commas,
        # whitespace, or just adjacent; allow up to 20 chars between them.
        # Given-names may be full names ("Thomas A.") or already-compacted
        # initials ("WE", "J.W.", "T"); handle both.
        authors = []
        for nm in re.finditer(
            r'class=(?:"surname"|surname\b)[^>]*>([^<]*)</div>'
            r'.{0,20}?'
            r'class=(?:"given-names"|given-names\b)[^>]*>([^<]*)</div>',
            entry, re.DOTALL,
        ):
            surname = unescape(nm.group(1)).strip().rstrip(",")
            given = unescape(nm.group(2)).strip()
            authors.append(format_name(given, surname))

        # DOI (matches both doi.org and dx.doi.org)
        doi = ""
        dm = re.search(r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1)))
        if not doi:
            # Fallback: DOI in comment div (e.g. "[Epub ahead of print; doi:10.1234/...]")
            dm = re.search(r'doi:\s*(10\.\S+?)[\])<,\s]', entry)
            if dm:
                doi = format_doi(unescape(dm.group(1)))

        # Fallback: full text
        if not title and not authors:
            title = strip_tags(entry).strip()

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

def _parse_abstract(html):
    """Extract abstract from OUP HTML."""
    # Match <section class=abstract ...> (unquoted) that is NOT a graphical
    # abstract.  OUP uses unquoted class=abstract for the text abstract and
    # quoted class="abstract ... graphicalAbstract" for graphical ones.
    for m in re.finditer(
        r'<section\s+class=(["\']?)abstract\1[^>]*>(.*?)</section>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        tag = html[m.start():m.start() + 120].lower()
        if 'graphical' in tag:
            continue
        text = strip_tags(m.group(2)).strip()
        if text:
            return text
    # Fallback: section after abstract-title h2
    m = re.search(
        r'class="[^"]*abstract-title[^"]*"[^>]*>.*?</h2>\s*<section[^>]*>(.*?)</section>',
        html,
        re.DOTALL,
    )
    if m:
        return strip_tags(m.group(1)).strip()
    return ""


def _parse_keywords(html):
    """Extract keywords from OUP HTML (often absent).

    Tries kwd-group/kwd-part (classic Silverchair) first, then
    content-metadata-keywords (Royal Society / Portland Press variant).
    """
    keywords = []
    m = re.search(
        r'class=["\']?kwd-group[^>]*>(.*?)</div>',
        html,
        re.DOTALL,
    )
    if m:
        for km in re.finditer(r'class=["\']?kwd-part[^>]*>(.*?)</(?:span|a)>', m.group(1), re.DOTALL):
            kw = strip_tags(km.group(1)).strip().rstrip(",")
            if kw:
                keywords.append(kw)
    if not keywords:
        # RSP/PP markup: content-metadata-keywords-title <div> followed by
        # one or more <a class=content-metadata--item>...</a> entries.
        tm = re.search(
            r'class=["\']?content-metadata-keywords-title[^>]*>.*?</div>',
            html, re.DOTALL,
        )
        if tm:
            tail = html[tm.end():tm.end() + 4000]
            # Stop scanning at the next non-keyword block
            stop = re.search(r'</div>\s*</div>', tail)
            scope = tail[:stop.start()] if stop else tail
            for am in re.finditer(
                r'<a[^>]*class=["\']?content-metadata--item[^>]*>(.*?)</a>',
                scope, re.DOTALL,
            ):
                kw = strip_tags(am.group(1)).strip().rstrip(",")
                if kw:
                    keywords.append(kw)
    return keywords


def _extract_div_content(html, start_pos):
    """Extract full content of a div starting at start_pos (after opening tag).

    Walks nested <div> tags to find the matching closing </div>, so that
    chrome that lives outside the article-body div (email alerts, sign-in
    panels, "Recommended" widgets, etc.) is excluded.
    """
    pos = start_pos
    depth = 1
    while depth > 0 and pos < len(html):
        next_open = re.search(r'<div[\s>]', html[pos:])
        next_close = re.search(r'</div>', html[pos:])
        if next_close is None:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos += next_open.end()
        else:
            depth -= 1
            if depth == 0:
                return html[start_pos:pos + next_close.start()]
            pos += next_close.end()
    return html[start_pos:pos]


def _find_h2_sections(html):
    """Find section headings in OUP HTML.

    Returns list of (start_pos, heading_text, class_type) where class_type
    is 'body', 'back', 'ref', 'abstract', or 'other'.
    """
    entries = []
    for m in re.finditer(
        r'<h2[^>]*class=(?:"([^"]*)"|(\S+))[^>]*>(.*?)</h2>',
        html, re.DOTALL,
    ):
        cls = m.group(1) or m.group(2) or ""
        text = strip_tags(m.group(3)).strip()
        if not text:
            continue
        if _ABSTRACT_HEADING in cls:
            kind = "abstract"
        elif _REF_HEADING in cls:
            kind = "ref"
        elif _BACK_HEADING in cls:
            kind = "back"
        elif _BODY_HEADING in cls:
            kind = "body"
        else:
            kind = "other"
        entries.append((m.start(), text, kind))
    return entries


def _parse_body(html):
    """Extract the body-zone text (between abstract and references).

    Boundary rules:
    - Start: after Abstract section.
    - End: above the last references heading.
    - Remove inner references sections.
    - Everything else between start and end is retained.
    """
    # Work within article-body (matches quoted "article-body ..." and
    # "body main-article-body" class variants, plus Portland Press's
    # unquoted class=article-body). Use nesting-aware extraction so
    # chrome that follows the article-body div (email alerts, sign-in
    # modals, "Recommended" widgets) is not captured.
    body_m = re.search(
        r'<div[^>]*class=(?:"[^"]*article-body[^"]*"|article-body\b)[^>]*>',
        html,
    )
    if not body_m:
        return ""

    content = _extract_div_content(html, body_m.end())
    h2s = _find_h2_sections(content)
    if not h2s:
        return ""

    # Start: after abstract
    start = 0
    for pos, text, kind in h2s:
        if kind == "abstract":
            # Skip past the abstract section
            sec_end = content.find('</section>', pos)
            if sec_end >= 0:
                start = sec_end + len('</section>')
            else:
                start = pos + 200
        elif kind in ("body", "back"):
            break

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text, kind) in enumerate(h2s):
        if kind == "ref" or _REF_RE.search(text):
            first_ref_idx = i
            break

    # Build body from two zones
    parts = []

    # Capture un-headed intro text between abstract and first body h2
    # (some OUP journals omit the "Introduction" heading).
    # Skip metadata panels (keywords, issue section) that may appear
    # before the actual intro text.
    first_body_pos = None
    for pos, text, kind in h2s:
        if kind in ("body", "back") and pos >= start:
            first_body_pos = pos
            break
    if first_body_pos is not None and start < first_body_pos:
        gap = content[start:first_body_pos]
        # Skip past article-metadata-panel if present
        meta_end = re.search(
            r'class="[^"]*article-metadata-panel[^"]*"[^>]*>',
            gap,
        )
        if meta_end:
            # Find the closing </div> for the metadata panel by locating
            # the first <p> tag after it (actual body text)
            first_p = re.search(r'<p\b', gap[meta_end.end():])
            if first_p:
                intro_start = start + meta_end.end() + first_p.start()
                if intro_start < start + first_body_pos:
                    parts.append((intro_start, first_body_pos))
        else:
            parts.append((start, first_body_pos))

    for i, (pos, text, kind) in enumerate(h2s):
        # Skip abstract and references
        if kind == "abstract" or kind == "ref" or _REF_RE.search(text):
            continue
        # Skip anything before start (e.g. graphical abstract between
        # the text abstract and the first body heading)
        end_pos = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)
        if pos < start:
            continue
        if first_ref_idx is None or i < first_ref_idx:
            # Zone 1: keep everything
            parts.append((pos, end_pos))
        else:
            # Zone 2: keep only supplementary
            if _SUPP_RE.search(text):
                parts.append((pos, end_pos))

    # Fallback: no sections matched, use start to first ref
    if not parts:
        end_pos = len(content)
        if first_ref_idx is not None:
            end_pos = h2s[first_ref_idx][0]
        if start < end_pos:
            parts.append((start, end_pos))

    if not parts:
        return ""

    body_html = ""
    for s, e in parts:
        body_html += content[s:e]

    # Some Silverchair publishers embed raw HTML inside attribute values
    # (e.g. data-section-title="Rnaseh2c<sup>-/-</sup> mice"). The unescaped
    # < character breaks the tags_to_text regex for headings, so strip these
    # attributes before downstream processing.
    body_html = re.sub(
        r'\s+data-section-title="[^"]*"',
        '',
        body_html,
    )
    body_html = re.sub(
        r"\s+data-section-title='[^']*'",
        '',
        body_html,
    )

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    OUP-specific: main_text is composed of abstract + keywords + body (with
    markdown headers "## Abstract" and "## Keywords" prepended to their sections).
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")
    keywords = _parse_keywords(html)
    if keywords:
        parts.append(f"## Keywords\n\n{', '.join(keywords)}")
    body = _parse_body(html)
    if body:
        parts.append(body)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse OUP HTML into a papers/*.json-format dict."""
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
