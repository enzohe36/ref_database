"""BioOne (bioone.org) HTML parser."""

import re
import urllib.parse
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    neutralize_media_queries,
    parse_meta_authors,
    strip_common,
    strip_tags,
    tags_to_text,
)

_NOISE = (
    "Google Scholar",
    "Open in a new tab",
    "Open in new window",
    "View full-size image",
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for bioone.org.

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies. BioOne
            ships several hard-coded `.container{width:1170px}` rules
            from Bootstrap; override to shrink to body.
    Step 2: remove `<aside id=cookieConsentLandmark>` consent banner
            (no separate backdrop sibling — the dialog ships standalone).
    Step 3: hide `<nav id=fixed>` (already display:none, kept for safety)
            AND `<div class="access hidden-print">` — a `position:fixed`
            "Access provided by <institution>" banner the publisher pins
            to bottom-right via inline `style=display:block`. The
            scan_sticky multi-position scroll test flags it (top stays
            constant across scroll positions).
    Step 4: hide `<div id=rightRail>` — the right-column tools panel
            (DOWNLOAD PAPER / SAVE TO LIBRARY / GET CITATION). Lives in
            `.col-xs-4` next to the article column (`.col-xs-8`); after
            removal the article column is forced to span full width.
    Step 5: no ad slots ship in the captured HTML.
    Step 6: page background already white; html/body forced to white
            for symmetry so the bg-around-column scan stays clean.
    Step 8: figures — BioOne wraps each figure in
            `<div class="fig panel">` containing an `<a>` link whose
            child `<img>` is the figure. Sibling `<div class=caption>`
            holds the caption (and `<h2 class=label>` holds "FIG. N.").
            Force figure block, image full column width above caption,
            with visible gap between image and caption text.
    Step 9: expand collapsed `<div id=affiliations style=display:none>`
            (Author Affiliations section toggled by an Author Affiliations +
            link). No other collapsed regions in the BioOne HTML.
    """
    html = neutralize_media_queries(html)

    # Step 2 — cookie consent banner.
    html = _remove_nested_element(
        html, r'<aside\b[^>]*\bid=cookieConsentLandmark\b[^>]*>',
    )

    override = (
        "<style>"
        # Step 1 / Step 6 — lock layout to 752 px wide, white background.
        "html{margin:0!important;padding:0!important;"
        "background:#fff!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "min-width:0!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "background:#fff!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # BioOne ships several hard-coded .container widths (1170px, 970px,
        # 750px, 1130px) from Bootstrap and a `#page{min-width:1200px}`
        # rule that forces every descendant to span 1200 px regardless of
        # body's width. Force them all to fit within body.
        "div#page,.container,.container-fluid,.container.body-content,"
        "main#main-content,header[role=banner],footer,nav#top,#navbar,"
        "#divMENUBAR,div.row,.panel,.panel-body,.panel-default"
        "{width:auto!important;min-width:0!important;"
        "max-width:100%!important;"
        "margin-left:auto!important;margin-right:auto!important;"
        "padding-left:0!important;padding-right:0!important;}"
        # The panel wrappers around the article body all carry hard-coded
        # `width:1100px` / `width:765px` / etc. Override to fit body.
        ".SPIEPanel,.ProceedingsArticleOpenAccessPanel,"
        ".ProceedingsArticleOpenAccessPanelHeight3,"
        ".ArticleContentPanel,.PAHArticleCol,#mainArticle,"
        "#divARTICLECONTENTTop,#divArticleContent,#divNotSignedSection,"
        ".ArticleContentBoldText,.ArticleContentRow,.ArticleContentText,"
        ".ProceedingsArticleOpenAccessHeaderRow,"
        ".ProceedingsArticleOpenAccessHeaderRowWidth,"
        ".ProceedingsArticleOpenAccessRow,"
        ".ProceedingsArticleOpenAccessFooterRow,"
        ".ProceedingsArticleOpenAccessFooterTextRow,"
        ".ProceedingsArticleOpenAccessContentTextBackGround,"
        ".ProceedingsArticleOpenAccessContentTextPadding"
        "{width:auto!important;min-width:0!important;"
        "max-width:100%!important;height:auto!important;"
        "padding-left:0!important;padding-right:0!important;}"
        # Step 3 — hide fixed-position nav (already display:none in source,
        # kept for safety against any inline-style override) and the
        # `position:fixed` "Access provided by ..." banner pinned to
        # bottom-right by an inline style override.
        "nav#fixed,div.access,div.access.hidden-print"
        "{display:none!important;}"
        # Step 4 — hide right-column tools panel and let the article
        # column span full width.
        "#rightRail,div.col-xs-4.JAHProceedingsArticleCol"
        "{display:none!important;}"
        # The article column is laid out with Bootstrap col-xs-8 inside a
        # row alongside col-xs-4 rightRail. Force col-xs-8 to span full
        # width once rightRail is hidden.
        "div.col-xs-8"
        "{width:100%!important;max-width:100%!important;"
        "float:none!important;flex:1 1 100%!important;"
        "padding-left:0!important;padding-right:0!important;}"
        # The "How to translate text using browser tools" link uses an
        # inline `style=float:right;margin-right:-30px` that overhangs
        # the body cap. Cap it to fit within content width.
        "a[href*='tools-and-features']"
        "{margin-right:0!important;float:none!important;"
        "display:block!important;}"
        # The footer's nav UL ships `margin-left:-290px;width:800px`,
        # which protrudes off the left of the body cap. Force it inside.
        "footer ul,footer div.col ul"
        "{width:auto!important;margin-left:0!important;"
        "max-width:100%!important;}"
        # Step 8 — figures: BioOne wraps each figure in
        # `<div class="fig panel">` containing an `<a>` link whose child
        # `<img>` is the figure. The `<div class=caption>` sibling holds
        # the caption; `<h2 class=label>` holds "FIG. N." Force
        # caption-aligned column-width image rendered above caption,
        # with visible gap between image and caption text.
        "div.fig,div.fig.panel,figure"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;float:none!important;clear:both!important;"
        "margin:0 0 16px 0!important;padding:0!important;"
        "box-sizing:border-box!important;}"
        "div.fig a,div.fig.panel a,figure a"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;margin:0!important;padding:0!important;}"
        "div.fig img,div.fig.panel img,figure img"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 12px 0!important;"
        "box-sizing:border-box!important;}"
        "div.fig div.caption,div.fig.panel div.caption,figure figcaption"
        "{display:block!important;width:100%!important;"
        "margin:0!important;padding:0!important;}"
        # Step 9 — expand the collapsed affiliations panel. The publisher
        # toggles `<div id=affiliations style=display:none>` via the
        # Author Affiliations + link; force-show as a block.
        "#affiliations"
        "{display:block!important;visibility:visible!important;"
        "opacity:1!important;height:auto!important;"
        "max-height:none!important;overflow:visible!important;}"
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

    Uses standard citation_* meta tags. BioOne's citation_journal_abbrev
    often carries a topical tag (e.g. "rare") instead of an ISO abbrev,
    so citation_journal_title is preferred — _abbreviate_journals in
    convert_html.py later normalizes through the NLM list.
    """
    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = (get_meta(html, "citation_journal_title")
               or get_meta(html, "citation_journal_abbrev"))
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

def _parse_authors(html):
    """Extract authors with affiliations.

    Uses citation_author + citation_author_institution meta tags. BioOne
    affiliations are prefixed with a single lowercase superscript tag
    letter (e.g. "aDepartment of..."); strip it when followed by a
    capital letter that begins the real affiliation text.
    """
    authors = []
    for a in parse_meta_authors(html):
        affs = []
        for aff in a.get("affiliations", []):
            aff = re.sub(r'^[a-z](?=[A-Z])', '', aff).strip()
            if aff:
                affs.append(aff)
        authors.append({
            "author": format_author_name(a["name"]),
            "affiliation": affs,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _scope_to_matching_div(html, m):
    """Return substring inside the <div> opened by match m, depth-balanced."""
    pos = m.end()
    depth = 1
    end = len(html)
    while depth > 0:
        no = re.search(r'<div[\s>]', html[pos:])
        nc = re.search(r'</div>', html[pos:])
        if not nc:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                end = pos + nc.start()
            pos += nc.end()
    return html[m.end():end]


def _parse_references(html):
    """Extract reference list from BioOne's <div class="ref-list table">.

    Each ref lives in nested <div class="ref-label cell"><div class="ref-content cell">.
    Two entry shapes appear across BioOne journals:

    Radiation Research style — bare "Initials Last" tokens, Scholar URL
    has no author= params:
        1. D Greene-Schloesser ME Robbins ... Radiation-induced brain
        injury: A review. Front Oncol 2012; 2:73.

    Acta Chiropterologica style — comma-separated "Last, Initials"
    tokens, Scholar URL carries author= params:
        1. Adams, A. M., M. K. Jantzen ... 2012. Do you hear what I hear?
        Methods in Ecology and Evolution, 3: 992-998.

    Authors are pulled from the Scholar URL author= params when present
    (cleanest source); otherwise from inline text via initials-token
    walking. Title/volume/year/pages come from the Scholar URL. Journal
    is parsed from inline text by anchoring on the year ("Journal YYYY;"
    in RR) or on the volume ("Journal, Vol:" in Acta Chiropterologica).
    """
    m = re.search(r'<div\s+class="?ref-list[^"]*"?[^>]*>', html)
    if not m:
        return []
    refs_html = _scope_to_matching_div(html, m)

    refs = []
    for rm in re.finditer(
        r'<div\s+class="?ref-content cell"?[^>]*>(.*?)</div>\s*</div>',
        refs_html, re.DOTALL,
    ):
        entry = rm.group(1)

        # Google Scholar lookup URL: title, volume, year, pages, optional authors
        title = volume = year = pages = ""
        scholar_authors = []
        gs = re.search(
            r'href="(https?://scholar\.google\.com/scholar_lookup\?[^"]+)"',
            entry,
        )
        if gs:
            qs = unescape(gs.group(1))
            params = urllib.parse.parse_qs(urllib.parse.urlparse(qs).query)
            title = params.get("title", [""])[0]
            volume = params.get("volume", [""])[0]
            year = params.get("publication_year", [""])[0]
            pages = params.get("pages", [""])[0].replace("–", "-")
            scholar_authors = params.get("author", [])

        # DOI (rarely present inline)
        doi = ""
        dm = re.search(r'href="?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1)))

        # Plain-text citation: drop the lookupLink span and the label <p>
        cleaned = re.sub(
            r'<span\s+class="?lookupLink"?[^>]*>.*?</span>',
            "", entry, flags=re.DOTALL,
        )
        cleaned = re.sub(
            r'<p\s+class="?ref-label"?[^>]*>.*?</p>',
            "", cleaned, flags=re.DOTALL,
        )
        cleaned = re.sub(r"<a\s+id=[^>]*></a>", "", cleaned)
        text = re.sub(r"\s+", " ", strip_tags(cleaned)).strip()

        # Authors: Scholar URL author= params win; each value is a
        # "Given Last" string routed through the central name pipeline.
        authors = []
        if scholar_authors:
            for raw in scholar_authors:
                formatted = format_author_name(raw)
                if formatted:
                    authors.append(formatted)
            remainder = text
        else:
            remainder = text
            if text:
                tokens = text.split(" ")
                i = 0
                while i < len(tokens) - 1:
                    tok = tokens[i].rstrip(",").rstrip(".")
                    next_tok = tokens[i + 1].rstrip(",").rstrip(".")
                    if (re.fullmatch(r"[A-Z][A-Z\.]{0,4}", tok)
                            and next_tok
                            and next_tok[0].isupper()
                            and not next_tok.isupper()):
                        # Pass "Initials Last" combined to the central
                        # pipeline; it absorbs surname prefixes (de/van/...)
                        # and emits canonical "Last IN". Surname guard:
                        # skip when next token is all-caps (corporate
                        # authors like "POSIT TEAM" must not be treated
                        # as initials + surname).
                        candidate = f"{tok} {next_tok}"
                        formatted = format_author_name(candidate)
                        if not formatted or " " not in formatted:
                            break
                        authors.append(formatted)
                        i += 2
                        continue
                    break
                remainder = " ".join(tokens[i:]).strip().rstrip(",").strip()

        # Journal extraction — three shapes attempted in order:
        #   RR strict:  "Title. Journal Year; Vol:Pages." — period anchor.
        #   RR loose:   "title-tail Journal Year; Vol:Pages." — year anchor
        #               only (some refs omit the title-terminating period).
        #   Acta:       "Title. Journal, Vol: Pages."
        # The loose form scans back from the year anchor up to a small
        # token window; capped to reduce false positives that swallow the
        # title tail.
        journal = ""
        if remainder and year:
            jm = re.search(
                rf"[.?!]\s+([^.?!]+?)\s+{re.escape(year)}\s*;",
                remainder,
            )
            if jm:
                journal = jm.group(1).strip().rstrip(".").strip()
            if not journal:
                jm = re.search(
                    rf"\s((?:[A-Z]\S*\s+){{0,5}}[A-Z]\S*)\s+"
                    rf"{re.escape(year)}\s*;",
                    remainder,
                )
                if jm:
                    journal = jm.group(1).strip().rstrip(".").strip()
        if not journal and remainder and volume:
            jm = re.search(
                rf"[.?!]\s+([^.?!,;]+?),\s+{re.escape(volume)}\s*:",
                remainder,
            )
            if jm:
                journal = jm.group(1).strip().rstrip(".").strip()

        # Title fallback when Scholar URL omits it. Skip when the
        # remainder begins with an author-ish surname token (e.g.
        # "Fox, J., and S. Weisberg." book refs) — the regex would
        # match the first initialed token "Fox, J" as the title.
        # Detect by looking for "Last, In." prefix with a comma.
        if not title and remainder:
            looks_like_authors = bool(
                re.match(r"[A-Z][A-Za-zÀ-ſ-]+,\s+[A-Z]\.", remainder)
            )
            if not looks_like_authors:
                tm = re.match(r"(.+?)\.\s+[A-Z]", remainder)
                if tm:
                    title = tm.group(1).strip()

        refs.append({"": {
            "title": title,
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": "",
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract text from BioOne's ArticleContentRow after the
    ArticleContentBoldText 'Abstract' header."""
    m = re.search(
        r'<text\s+class="?ArticleContentBoldText"?[^>]*>\s*Abstract\s*</text>',
        html,
    )
    if not m:
        return ""
    after = html[m.end():]
    tm = re.search(
        r'<text\s+class="?ArticleContentText"?[^>]*>(.*?)</text>',
        after, re.DOTALL,
    )
    if not tm:
        return ""
    inner = strip_common(tm.group(1))
    return strip_tags(inner).strip()


def _parse_main_text(html):
    """Extract body text.

    BioOne wraps body sections inside <div id=article-body class=body>
    with each section in <div class=section> and <h2 class=main-title>
    headings. The references list is a sibling <div class="ref-list
    table"> excluded by the container scope; an inline REFERENCES <h2>
    inside body scope (rare) is also cut.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")

    m = re.search(r'<div\s+id="?article-body"?[^>]*>', html)
    if m:
        body_html = _scope_to_matching_div(html, m)

        ref_h2 = re.search(
            r"<h2[^>]*>\s*(?:REFERENCES|References|Literature\s+Cited)"
            r"\s*</h2>",
            body_html,
        )
        if ref_h2:
            body_html = body_html[:ref_h2.start()]

        body_html = extract_captions(body_html)
        body_html = strip_common(body_html)
        text = tags_to_text(body_html)
        if text.strip():
            parts.append(text)

    result = "\n\n".join(parts)
    return drop_noise(result, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse BioOne HTML into a papers/*.json-format dict."""
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
