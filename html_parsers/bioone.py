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
    parse_meta_authors,
    remove_elements_by_id,
    remove_elements_by_selector,
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

_REF_RE = re.compile(r'\breferences\b|literature\s+cited', re.IGNORECASE)

_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize BioOne HTML to a single centered text column.

    Chrome stripped (Step 3):
      - Site `<header class=hidden-print role=banner>` and `<footer>`.
      - "This website uses cookies to provide you with a variety of
        services" CookieConsent banner (`.cc-window`).
      - `#rightRail` sidebar with related articles and downloads.

    Visibility tweaks (Step 4):
      - Force-expand the "Author Affiliations +" accordion
        (`#affiliations` with inline `style=display:none`).

    Reading column wrapper: `<main id=main-content>`. Cap at 752 px
    with 56 px top/bottom + 16 px side padding.
    """
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    for _ in range(5):
        before = html
        html = _remove_nested_element(html, r'<header\b[^>]*>')
        if html == before:
            break
    for _ in range(5):
        before = html
        html = _remove_nested_element(html, r'<footer\b[^>]*>')
        if html == before:
            break
    html = remove_elements_by_id(html, "rightRail")
    # Site top nav (`<nav id=top>` with logo + account) renders 36 px
    # tall above #main-content; strip so the wrapper sits at body top.
    html = remove_elements_by_id(html, "top")
    # `#divNotSignedSection` wraps both the article-section tabs
    # (`#navbar` → ARTICLE / FIGURES & TABLES / REFERENCES / CITED BY)
    # AND the sign-in popup (`#divPopupDownloadOptions`). Keep the
    # tabs; strip only the sign-in form.
    html = remove_elements_by_id(html, "divPopupDownloadOptions")
    # Floating "access" badge (e.g. Open Access label) pinned at the
    # bottom of the viewport in narrow layouts.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*class="access hidden-print"[^>]*>',
    )
    # "How to translate text using browser tools" help link floats at the
    # top of the article panel — remove it so the first rendered text is
    # the publication-date anchor.
    html = _remove_nested_element(
        html,
        r'<a\b[^>]*href=[^>]*/help/tools-and-features[^>]*>',
    )
    # CookieConsent banner (cc-window or cookie-consent variants).
    for cls in ("cc-window", "cookie-consent", "cookieconsent"):
        for _ in range(3):
            before = html
            html = _remove_nested_element(
                html, rf'<div\b[^>]*class="[^"]*\b{cls}\b[^"]*"[^>]*>',
            )
            if html == before:
                break

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
        # The site wraps content in #page (hardcoded width:1200px) and
        # Bootstrap .container + .row > .col-xs-8 (article) + .col-xs-4
        # (#rightRail). Collapse outer width-constraining wrappers so the
        # article column fills body width — but preserve the publisher's
        # natural margins/padding on `.row` and inner `.col-*`, since
        # the citation block (`#articleSubmission`, `#articleCitation`)
        # uses `.row.ArticleContentRow{margin-top:15px;padding-top:5px}`
        # plus the Bootstrap negative-gutter `ml:-15px` to space and
        # align its sub-rows correctly.
        "#page,.container,.container-fluid,.container.body-content,"
        ".JAHProceedingsArticleCol{"
        "display:block !important;width:100% !important;"
        "max-width:100% !important;min-width:0 !important;"
        "margin:0 !important;padding:0 !important;float:none !important;"
        "border:none !important;flex:1 1 auto !important;"
        "background:#fff !important}"
        ".row,.row>[class*='col-']{"
        "display:block !important;width:100% !important;"
        "max-width:100% !important;min-width:0 !important;"
        "float:none !important;flex:1 1 auto !important}"
        # PAHArticleCol gets the same flatten (width, padding, float) but
        # NOT margin-top — its natural 25 px top margin provides the
        # breathing room between the article header and the Abstract
        # heading on the live page.
        ".PAHArticleCol{"
        "display:block !important;width:100% !important;"
        "max-width:100% !important;min-width:0 !important;"
        "margin-left:0 !important;margin-right:0 !important;"
        "margin-bottom:0 !important;"
        "padding:0 !important;float:none !important;"
        "border:none !important;flex:1 1 auto !important;"
        "background:#fff !important}"
        # Width/border-only flatten on visible panel wrappers inside
        # main#main-content. Keep publisher-natural margin/padding so
        # the citation block's `.panel-body.ArticleContent{padding-left:15px}`
        # and similar internal gutters remain. Do NOT override `display`
        # — `.panel.panel-default` / `.panel-body` are reused by hidden
        # paywall popups (`#divPopupDownloadOptions` style=display:none)
        # that would otherwise be exposed by a blanket display:block.
        ":root main#main-content .ProceedingsArticlePanel,"
        ":root main#main-content .panel.panel-default,"
        ":root main#main-content .panel-body{"
        "width:auto !important;max-width:100% !important;min-width:0 !important;"
        "border:none !important;background:#fff !important}"
        # `.SPIEPanel` is the outermost wrapper around the entire
        # article column inside `main#main-content` — duplicating the
        # role our cap (`main#main-content{padding:56px 16px}`) already
        # serves. Its native rendering adds an outer 3-px top margin and
        # 20-px bottom margin plus a 15-px-on-all-sides `.panel-body`
        # padding, which:
        #   - shrinks the usable column from 688 → 658 px (R fails),
        #   - pushes the first text (`.DetailDate`) 18 px below the
        #     wrapper's 56-px top padding (T fails),
        #   - opens a 35-px gap between the last content row and the
        #     wrapper's 56-px bottom padding (B fails).
        # Collapse the outer SPIEPanel and its direct .panel-body so
        # the inner `.ProceedingsArticleOpenAccessPanel` /
        # `.ArticleContentPanel` subpanels become the visible chrome,
        # at full 688-px width. Inner panels keep their own padding —
        # that is the citation block layout we want to preserve.
        ":root main#main-content .SPIEPanel{"
        "margin:0 !important}"
        # SPIEPanel's direct .panel-body is the outermost article shell —
        # zero its vertical padding too (in addition to the horizontal
        # zero below) so the inner header card sits flush at the
        # wrapper's 56-px top padding. Inner ArticleContentPanel
        # panel-bodies keep their pt=15/pb=15 for inter-section spacing.
        ":root main#main-content .SPIEPanel > .panel-body{"
        "padding-top:0 !important;padding-bottom:0 !important}"
        # Zero ALL padding on every .panel-body inside main#main-content
        # and zero every .row's Bootstrap negative gutters in tandem.
        # The publisher nests `.panel.panel-default > .panel-body{padding:
        # 15px} > .row{ml:-15px;mr:-15px}` repeatedly: SPIEPanel wraps
        # the article, ArticleContentPanel wraps each content section
        # (header, citation, footnotes, references). Each layer would
        # otherwise shave 30 px off the column. Half-zero (only outer
        # SPIEPanel) leaves the inner ArticleContentPanel still narrowing
        # to 658. Vertical padding stays so inter-section gaps survive.
        ":root main#main-content .panel-body{"
        "padding-left:0 !important;padding-right:0 !important}"
        ":root main#main-content .row{"
        "margin-left:0 !important;margin-right:0 !important}"
        # Last `.ArticleContentPanel` (the citation/footnotes block at
        # the article tail) ships with `margin-bottom:25px`. With site
        # chrome stripped, it's the last visible content — zero its
        # trailing margin so the wrapper's 56-px bottom padding is the
        # only B contribution.
        ":root main#main-content "
        ".ArticleContentPanel:last-of-type{margin-bottom:0 !important}"
        # Reclaim ONLY the height of the stripped article-tabs row
        # (`#divNotSignedSection`/`#navbar`, ~64 px). The publisher's
        # natural section spacing (`.PAHArticleCol{margin-top:25px}`
        # plus `.ArticleContentHeadRow{margin-top:30px;padding-top:10px}`)
        # gives the Abstract heading the same breathing room above the
        # citation as on the live page — keep it. Strip only the
        # vertical right border that used to frame the column against
        # the right rail (now gone).
        ":root main#main-content .PAHArticleCol{"
        "border-right:none !important}"
        # Capped reading-column wrapper.
        ":root main#main-content{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        ":root main#main-content *{"
        "max-width:100% !important;min-width:0 !important;"
        "box-sizing:border-box !important}"
        # Force-expand author-affiliations accordion (inline style=display:none).
        "#affiliations{display:block !important;"
        "visibility:visible !important;"
        "max-height:none !important;height:auto !important;"
        "overflow:visible !important}"
        # Tables: force fixed layout + break-all so wide cells don't
        # push past the wrapper.
        ":root main#main-content table{"
        "width:100% !important;max-width:100% !important;"
        "table-layout:fixed !important}"
        ":root main#main-content td,:root main#main-content th{"
        "word-break:break-all !important;overflow-wrap:anywhere !important;"
        "white-space:normal !important}"
        # First-/last-child margin reset — scoped to direct children
        # of the wrapper only (via `>`). Blanket `*:first-child` was
        # zeroing the natural padding on every reference-list item.
        ":root main#main-content>*:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        ":root main#main-content>*:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # The spec start anchor "17 May 2013" is rendered inside
        # `<text class=DetailDate>`, which ships with margin-top:32px.
        # It lives 10 levels deep inside the wrapper so the direct-
        # child first-child zero doesn't reach it. Target the class
        # directly — only one `.DetailDate` in the article (the
        # publication-date line), so no risk of collapsing other
        # section headings.
        ":root main#main-content .DetailDate{"
        "margin-top:0 !important;padding-top:0 !important}"
        # Title class ships `float:left; margin-top:2px; margin-
        # bottom:0`, which makes the author byline sit flush with
        # the last line of the title. Clear the float AND add a
        # sensible margin-bottom so title and author list are
        # visually separated.
        ":root main#main-content .ProceedingsArticleOpenAccessHeaderText{"
        "float:none !important;display:block !important;"
        "margin-bottom:16px !important}"
        # Figures: bioone wraps each figure in
        #   <div class="fig panel" style="display:float;clear:both">
        #     <a id=...></a>
        #     <h2 class=label>FIG. N.</h2>
        #     <div class=caption><p>...</p></div>
        #     <a target=_blank href=<HIRES_JPG>>
        #       <img src="data:image/jpeg;base64,..." (28-39 KB thumb)>
        # Native order is label → caption → image. Per the figure layout
        # contract, image must render above caption. Use flex column with
        # `order` to put the image link first while keeping label/caption
        # in source order. The high-res JPEG URL is on the inner <a href>
        # — get_refs.py needs a browser-script swap to inline it; this
        # CSS handles the visual order + full-width layout only.
        ":root main#main-content .fig.panel{"
        "display:flex !important;flex-direction:column !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:1rem 0 !important;padding:0 !important;"
        "float:none !important;clear:both !important}"
        ":root main#main-content .fig.panel > a[href*='/graphic/']{"
        "order:-1 !important;display:block !important;"
        "width:100% !important;margin:0 0 5px 0 !important}"
        ":root main#main-content .fig.panel > a[href*='/graphic/'] > img{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 !important}"
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

    Uses standard citation_* meta tags.
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

    # BioOne's citation_journal_abbrev often contains a topical tag (e.g.
    # "rare") rather than an ISO journal abbreviation. Prefer
    # citation_journal_title (e.g. "Radiation Research") — refs.json
    # convention tolerates the full title when no clean ISO abbrev is in
    # the HTML.
    journal = get_meta(html, "citation_journal_title") or get_meta(html, "citation_journal_abbrev")
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

# Particles still used by inline reference-author detection below.
_PARTICLES = {"de", "del", "della", "di", "du", "la", "le", "van", "von", "der", "da", "dos", "das"}


def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Uses citation_author + citation_author_institution meta tags.
    BioOne stores "Given Last" names and prefixes affiliations with a
    superscript tag letter (e.g. "aDepartment of..."); strip the tag.
    """
    authors = []
    for a in parse_meta_authors(html):
        affs = []
        for aff in a.get("affiliations", []):
            # Drop leading lowercase tag letter (a, b, c...) that marks
            # the affiliation footnote; only strip when followed by a
            # capital letter (starts the real affiliation text).
            aff = re.sub(r'^[a-z](?=[A-Z])', '', aff).strip()
            if aff:
                affs.append(aff)
        authors.append({
            "author": _display_to_initials(a["name"]),
            "affiliation": affs,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _flip_initials_last(name):
    """Convert 'JF Ward' (BioOne inline form) to 'Ward JF' via shared helpers."""
    return format_author_name(name)


def _parse_references(html):
    """Extract reference list from BioOne's <div class="ref-list table">.

    Each ref lives in nested <div class="ref-label cell"><div class="ref-content cell">
    and contains plain text like:
        1.
        JF Ward
        Some biochemical consequences ... Radiat Res 1981; 86:185–95.
        <a href="http://scholar.google.com/scholar_lookup?title=...&volume=86&publication_year=1981&pages=185-280">
    Structured fields (title/volume/year/pages) are pulled from the Google
    Scholar lookup URL; authors and journal are pulled from the surrounding
    text.
    """
    m = re.search(r'<div\s+class="?ref-list[^"]*"?[^>]*>', html)
    if not m:
        return []
    # Scope to matching </div> by depth
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
    refs_html = html[m.end():end]

    refs = []
    # Each ref-content cell is one reference entry
    for rm in re.finditer(
        r'<div\s+class="?ref-content cell"?[^>]*>(.*?)</div>\s*</div>',
        refs_html, re.DOTALL,
    ):
        entry = rm.group(1)

        # Google Scholar lookup URL → title, volume, year, pages
        title = volume = year = pages = ""
        gs = re.search(
            r'href="(https?://scholar\.google\.com/scholar_lookup\?[^"]+)"',
            entry,
        )
        if gs:
            qs = unescape(gs.group(1))
            qs = urllib.parse.urlparse(qs).query
            params = urllib.parse.parse_qs(qs)
            title = params.get("title", [""])[0]
            volume = params.get("volume", [""])[0]
            year = params.get("publication_year", [""])[0]
            pages = params.get("pages", [""])[0].replace('\u2013', '-')

        # DOI (rarely present inline)
        doi = ""
        dm = re.search(r'href="?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1)))

        # Plain-text citation: strip the <span class=lookupLink> block and
        # the label <p>, keep everything else as text
        cleaned = re.sub(
            r'<span\s+class="?lookupLink"?[^>]*>.*?</span>',
            '', entry, flags=re.DOTALL,
        )
        cleaned = re.sub(
            r'<p\s+class="?ref-label"?[^>]*>.*?</p>',
            '', cleaned, flags=re.DOTALL,
        )
        cleaned = re.sub(r'<a\s+id=[^>]*></a>', '', cleaned)
        text = re.sub(r'\s+', ' ', strip_tags(cleaned)).strip()

        # Authors end when a full-word starts (title). In BioOne refs authors
        # are listed one per line as "Initials Last". The block continues
        # until the title sentence begins; the title ends at the journal
        # name. Walk word-by-word accepting tokens until we hit a token that
        # doesn't look like an author.
        authors = []
        if text:
            # Split into whitespace-separated tokens and regroup into author
            # pairs ("Initials" + "Surname"). Stop at the first token that
            # isn't initials-like AND isn't a surname following initials.
            tokens = text.split(' ')
            i = 0
            while i < len(tokens) - 1:
                tok = tokens[i].rstrip(',').rstrip('.')
                # Initials token: 1-5 uppercase letters (optionally with dots)
                if re.fullmatch(r'[A-Z][A-Z\.]{0,4}', tok):
                    surname_parts = [tokens[i + 1].rstrip(',').rstrip('.')]
                    # Allow particle + Last for multi-word surnames
                    if (i + 2 < len(tokens)
                            and tokens[i + 1].lower() in _PARTICLES):
                        surname_parts.append(tokens[i + 2].rstrip(',').rstrip('.'))
                        advance = 3
                    else:
                        advance = 2
                    surname = ' '.join(surname_parts).strip(',').strip()
                    if surname and surname[0].isupper():
                        initials = tok.replace('.', '')
                        authors.append(f"{surname} {initials}")
                        i += advance
                        continue
                break

            # Remainder after authors is "Title. Journal Year; Vol:Pages."
            remainder = ' '.join(tokens[i:]).strip().rstrip(',').strip()
        else:
            remainder = ""

        # Journal: text between the title-terminating punctuation and the
        # " YYYY;" anchor. Titles may end with '.', '?', or '!' — the
        # question/exclamation endings are common and must not prevent
        # journal capture.
        journal = ""
        if remainder and year:
            jm = re.search(
                rf'[.?!]\s+([^.?!]+?)\s+{re.escape(year)}\s*;',
                remainder,
            )
            if jm:
                journal = jm.group(1).strip().rstrip('.').strip()

        # Title fallback: if Scholar URL missing, extract title from text
        if not title and remainder:
            tm = re.match(r'(.+?)\.\s+[A-Z]', remainder)
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
    # Find the following ArticleContentText block
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

    BioOne wraps body sections inside <div id=article-body class=body> with
    each section in <div class=section> and <h2 class=main-title> headings.
    References are a sibling <div class="ref-list table"> excluded by the
    container scoping.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")

    m = re.search(r'<div\s+id="?article-body"?[^>]*>', html)
    if m:
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
        body_html = html[m.end():end]

        # Cut off at the first REFERENCES h2 if present inside body scope
        ref_h2 = re.search(
            r'<h2[^>]*>\s*(?:REFERENCES|References|Literature\s+Cited)\s*</h2>',
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
