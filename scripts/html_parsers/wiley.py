"""Wiley (onlinelibrary.wiley.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    neutralize_media_queries,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

_NOISE = (
    "Open in new window",
    "Open in a new tab",
    "Open in viewer",
    "Web of Science",
    "Google Scholar",
    "PubMed",
    "Search for more papers by this author",
    "CAS",
    "Wiley Online Library",
)

_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

# CSS injected before </head> to lock the rendered article to a 720-px-wide
# native column, force the article__content reading column to fill that
# width (overriding Bootstrap col-md-8/col-lg-8 sizing), apply the
# 56/16 reading-column padding, and resolve the Wiley figure layout to a
# single block-level image above its caption.
_OVERRIDE_CSS = """<style>
html, body { background: #ffffff !important; }
body {
    max-width: 752px !important;
    margin: 0 auto !important;
    background: #ffffff !important;
}
:root #article__content {
    width: 100% !important;
    max-width: 100% !important;
    flex: 0 0 100% !important;
    padding: 56px 16px !important;
    margin: 0 !important;
    float: none !important;
    box-sizing: border-box !important;
}
:root .row.article-row { display: block !important; margin: 0 !important; }
/* Bootstrap container/row/col wrappers around #article__content add
   15 px gutter padding and 20 px article top padding — zero them so the
   #article__content padding above is the sole margin source. */
:root #pb-page-content .container,
:root #pb-page-content .container > .row,
:root #pb-page-content .container > .row > div {
    padding-left: 0 !important;
    padding-right: 0 !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    max-width: 100% !important;
    width: 100% !important;
}
:root #pb-page-content article {
    padding: 0 !important;
    margin: 0 !important;
}
:root #article__content figure.figure,
:root #article__content figure.figure > * {
    display: block !important;
    width: 100% !important;
    text-align: left !important;
    box-sizing: border-box !important;
}
:root #article__content figure.figure picture,
:root #article__content figure.figure a {
    display: block !important;
    width: 100% !important;
}
:root #article__content figure.figure img.figure__image {
    display: block !important;
    width: 100% !important;
    height: auto !important;
    max-width: 100% !important;
    margin: 0 0 5px 0 !important;
}
</style>"""


def remove_banners(html):
    """Apply Phase 2 layout rules for onlinelibrary.wiley.com.

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies at any
            viewport.
    Step 2: Osano cookie consent — the page ships a hidden
            `.osano-cm-window` dialog plus an `.osano-cm-info-dialog`
            full-viewport drawer. Both are `position: fixed` and remain
            in the DOM after capture.
    Step 3: sticky elements — `<nav class="coolBar stickybar">` is the
            in-article tools bar (PDF / share / cite / etc.) that pins
            below the page header on scroll; `.w-slide` / `.w-slide_head`
            are the off-canvas slide-in drawers that ship `position: fixed`
            even when not activated.
    Step 5: ad slots — Wiley publishes empty `<div class="ad-slot">` /
            `<div class="advertisement">` reservation wrappers in some
            article templates; remove on sight.
    Steps 6-11: handled by the override CSS injected before </head>,
            which (a) forces the article column to fill the body cap,
            (b) zeros Bootstrap container/row gutters, (c) renders
            figures block-level with image above caption, and
            (d) keeps publisher chrome (header/footer) inside the cap.
    """
    html = neutralize_media_queries(html)

    # Step 2 — Osano cookie consent dialog and drawer (fixed-position,
    # always present in the captured DOM regardless of opt-in state).
    for cls in (
        "osano-cm-window",
        "osano-cm-info-dialog",
    ):
        while True:
            prev = html
            html = remove_elements_by_selector(html, cls)
            if html == prev:
                break

    # Step 3 — sticky/fixed elements:
    #   1. `<nav class="coolBar stickybar">` is the in-article tools
    #      strip (PDF / share / cite). It sticks below the site header
    #      on scroll and obscures content; remove the whole nav block.
    while True:
        new = _remove_nested_element(
            html,
            r'<nav\s+class="?coolBar\s+stickybar[^"]*"?[^>]*>',
        )
        if new == html:
            break
        html = new
    #   2. `.w-slide` / `.w-slide_head` off-canvas drawers (fixed,
    #      parked off-screen until activated).
    for cls in ("w-slide", "w-slide_head"):
        while True:
            prev = html
            html = remove_elements_by_selector(html, cls)
            if html == prev:
                break

    # Step 5 — ad placeholders. Empty in the static capture but reserve
    # vertical space when present.
    for cls in ("ad-slot", "advertisement"):
        while True:
            prev = html
            html = remove_elements_by_selector(html, cls)
            if html == prev:
                break

    if "</head>" in html:
        html = html.replace("</head>", _OVERRIDE_CSS + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", _OVERRIDE_CSS + r"\1", html, count=1)
    return html
def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Uses standard citation_* meta tags, with a body-HTML fallback for
    volume / issue / year when citation_issue is empty (supplement
    issues like 'S1') or citation_online_date reflects the acceptance
    rather than publication year. The fallback reads the ToC URL
    '/toc/<journal-id>/<year>/<volume>/<issue>' from the byline's
    <a class=volume-issue> anchor.
    """
    # Body-HTML fallback: extracts year, volume, issue from the ToC
    # link's URL. Present on every Wiley article page as e.g.
    # <a href="https://onlinelibrary.wiley.com/toc/10982280/2024/65/S1" class=volume-issue>.
    toc_m = re.search(
        r'href=(?:"|\')?https?://[^/]*wiley\.com/toc/\d+/(\d{4})/([^/]+)/([^/\s"\']+)'
        r'[^>]*class=(?:"|\')?volume-issue',
        html,
    )
    body_year = toc_m.group(1) if toc_m else ""
    body_volume = toc_m.group(2) if toc_m else ""
    body_issue = toc_m.group(3) if toc_m else ""

    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_online_date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)
    # Prefer body's publication year over citation_online_date, which
    # Wiley populates with the acceptance date for some papers.
    if body_year:
        year = body_year

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    journal = journal.rstrip(".") if journal else ""

    volume = get_meta(html, "citation_volume") or body_volume
    issue = get_meta(html, "citation_issue") or body_issue

    return {
        "title": get_meta(html, "citation_title"),
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Wiley wraps each author in a <div class="author-info accordion-tabbed__content">
    block containing <p class=author-name> (display name) followed by one or
    more <p> elements holding the affiliation text. Prefer the desktop list
    (loa-wrapper loa-authors hidden-xs desktop-authors) to avoid duplicates
    from the mobile list.
    """
    # Scope to desktop loa wrapper if present
    dm = re.search(
        r'<div\s+class="?loa-wrapper\s+loa-authors\s+hidden-xs\s+desktop-authors"?[^>]*>',
        html,
    )
    if dm:
        pos = dm.end()
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
        scope = html[dm.end():end]
    else:
        scope = html

    authors = []
    seen = set()
    for m in re.finditer(
        r'<div\s+class="?author-info\s+accordion-tabbed__content"?[^>]*>(.*?)</div>',
        scope, re.DOTALL,
    ):
        block = m.group(1)
        # First <p class=author-name> inside is the display name
        nm = re.search(r'<p\s+class="?author-name"?[^>]*>([^<]+)</p>', block)
        if not nm:
            continue
        display = unescape(nm.group(1)).strip()
        if display in seen:
            continue
        seen.add(display)

        # Remaining <p> tags carry the affiliation text(s); skip those that
        # match the name again (which Wiley repeats), the moreInfoLink, and
        # <p class="author-type">...</p> role labels ("Corresponding
        # Author"), which would otherwise leak in as a fake affiliation.
        affs = []
        for pm in re.finditer(r'(<p[^>]*>)([^<]*)</p>', block):
            open_tag, inner = pm.group(1), pm.group(2)
            if re.search(r'\bclass=(["\']?)[^"\'>]*author-type\b', open_tag):
                continue
            text = unescape(inner).strip()
            if not text or text == display:
                continue
            affs.append(text)

        authors.append({
            "author": _display_to_initials(display),
            "affiliation": affs,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _normalize_ref_author(name):
    """Normalize a Wiley reference-author span into 'Last IN' via shared helpers.

    Handles the three shapes Wiley uses ('Stein H', 'Adam, N.',
    'R. P. Barnes') uniformly via parse_combined_name + format_name.
    """
    return format_author_name(name)


def _parse_references(html):
    """Extract the reference list.

    Wiley references live inside <section class="article-section
    article-section__references"> -> <ul class="rlist separator"> with each
    ref as <li data-bib-id=bN> containing structured spans:
      <span class=author>LastName IN</span>
      (<span class=pubYear>YYYY</span>)
      <span class=articleTitle>Title</span>
      <i class=journalTitle>Journal</i>
      <span class=vol>Vol</span>
      <span class=pageFirst>X</span> - <span class=pageLast>Y</span>
      <span class="hidden data-doi">10.xxx/...</span>
    """
    rs = re.search(
        r'<section\s+class="?article-section\s+article-section__references"?[^>]*>',
        html,
    )
    if not rs:
        return []
    # Scope to matching </section>
    pos = rs.end()
    depth = 1
    end = len(html)
    while depth > 0:
        no = re.search(r'<section[\s>]', html[pos:])
        nc = re.search(r'</section>', html[pos:])
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
    refs_html = html[rs.end():end]

    refs = []
    li_starts = list(re.finditer(r'<li\s+data-bib-id=[^>]*>', refs_html))
    for i, li_m in enumerate(li_starts):
        li_end = li_starts[i + 1].start() if i + 1 < len(li_starts) else len(refs_html)
        entry = refs_html[li_m.end():li_end]

        def _field(cls, tag='span'):
            m = re.search(
                rf'<{tag}\s+class="?{cls}"?[^>]*>(.*?)</{tag}>',
                entry, re.DOTALL,
            )
            return strip_tags(m.group(1)).strip() if m else ""

        title = _field("articleTitle")
        if not title:
            title = _field("chapterTitle")
        # Only reach for bookTitle as the title when no chapter title is
        # available; otherwise bookTitle belongs in journal.
        has_chapter_title = bool(title)

        journal = _field("journalTitle", tag='i').rstrip('.')
        if not journal:
            journal = _field("journalTitle").rstrip('.')
        if not journal:
            # Book chapters: "<span class=bookTitle>Book</span>" holds the
            # book name, which plays the journal role for chapter citations.
            journal = _field("bookTitle").rstrip('.')
        if not title and not has_chapter_title:
            # Standalone book entry — put the book name in the title so the
            # existing behavior for un-chaptered books is preserved.
            title = _field("bookTitle")
        if not title and not journal:
            # Wiley emits a catch-all "<span class=otherTitle>...</span>" for
            # unstructured entries (lab manuals, non-journal works). Treat
            # the whole payload as the book title (journal role).
            other = _field("otherTitle")
            if other:
                journal = other.rstrip('.')

        # Unstructured book chapter fallback. Older Wiley papers emit only
        # author/pubYear/pageFirst/pageLast spans; the chapter title sits as
        # plain text after ") " and the book title is the <i>...</i> tag
        # following "In:". Example:
        #   "(1998) Chapter Title. In: <i>Book Name</i> (eds ...), pp. ..."
        if (not title or not journal):
            in_m = re.search(r'\.\s*In:?\s*<i>([^<]+)</i>', entry, re.I)
            if in_m:
                if not journal:
                    journal = in_m.group(1).strip().rstrip('.')
                if not title:
                    pre = entry[:in_m.start()]
                    # Text between "</span>)" (pubYear close) and the
                    # final "." is the chapter title. strip_tags collapses
                    # inline italics used for species names, etc.
                    py_end = re.search(
                        r'</span>\s*\)\s*', pre,
                    )
                    if py_end:
                        title_text = strip_tags(pre[py_end.end():]).strip()
                        title_text = re.sub(r'\s+', ' ', title_text).rstrip('.').strip()
                        if title_text:
                            title = title_text

        year = _field("pubYear")
        volume = _field("vol")
        issue = _field("issue")
        fpage = _field("pageFirst")
        lpage = _field("pageLast")
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # Wiley templates for Chem Europe, Aging Cell, Environ Mol Mutagen,
        # and some Photochem Photobiol entries emit the article identifier
        # (e.g., "e210181811", "e82324", "zcab038", "113553") as bare text
        # after the vol / citedIssue span — NOT wrapped in pageFirst. When
        # fpage stays empty, capture the trailing token before the first
        # trailing '.' or ';'.
        if not pages:
            # Find the last <span class=vol> / citedIssue close, then the
            # next alphanumeric-hyphen token before a sentence terminator.
            vol_span_m = list(re.finditer(
                r'<span[^>]*class=["\']?(?:vol|citedIssue)[^>]*>[^<]*</span>',
                entry,
            ))
            if vol_span_m:
                after = entry[vol_span_m[-1].end():]
                # Skip separator punctuation, then grab the token.
                after = re.sub(r'^\s*[,.:;]?\s*', '', after)
                tok_m = re.match(
                    r'([A-Za-z]?\d[\w\-\u2013\u2014.]*)',
                    after,
                )
                if tok_m:
                    tok = re.sub(r"[\u2010-\u2014]", "-", tok_m.group(1)).strip(".,")
                    # Accept article numbers / page ranges; reject volume
                    # duplicates and bare 1-3 digit numbers that are likely
                    # an issue value.
                    if tok and tok != volume and (
                        re.search(r"[a-zA-Z]", tok) or "-" in tok or len(tok) >= 4
                    ):
                        pages = tok

        # DOI: prefer the visible linkout URL (the "hidden data-doi" span has
        # been seen to contain mangled values with '?' substituted for '-')
        doi = ""
        dm = re.search(
            r'href="?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry
        )
        if dm:
            doi = format_doi(unescape(dm.group(1)))
        if not doi:
            dm = re.search(
                r'<span\s+class="?hidden\s+data-doi"?[^>]*>([^<]+)</span>',
                entry,
            )
            if dm:
                doi_text = dm.group(1).strip()
                if doi_text and '?' not in doi_text:
                    doi = format_doi(doi_text)

        # Authors (structured). Wiley journals use three name formats:
        #   "Stein H"       (LastName Initials) — keep as-is
        #   "Adam, N."      (Last, Initials)   — strip comma/dots
        #   "R. P. Barnes"  (Initials Last)    — flip
        authors = []
        for am in re.finditer(
            r'<span\s+class="?author"?[^>]*>([^<]+)</span>', entry
        ):
            name = unescape(am.group(1)).strip()
            if name:
                authors.append(_normalize_ref_author(name))

        # Skip composite-reference header <li>s that contain only a
        # <span class=bullet> label (e.g., chemistry papers number
        # reference 1 as empty header and list the actual citations
        # under sub-items 1a, 1b, 1c).
        if not (title or journal or year or authors or doi):
            continue

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

def _parse_main_text(html):
    """Extract body text.

    Scope to <div class=article__body>; cut off at the References section
    (<section class="article-section article-section__references">). Any
    supplementary sections after References are captured via an SUPP_RE
    heading match.
    """
    body_m = re.search(r'<div\s+class="?article__body"?[^>]*>', html)
    if not body_m:
        return ""

    # Scope to matching </div>
    pos = body_m.end()
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
    body_html = html[body_m.end():end]

    # Strip figure-extra widgets ("Open in figure viewer", "PowerPoint")
    # that sit in the figcaption header, otherwise they get concatenated
    # to the "FIGURE N" label as inline link text.
    body_html = re.sub(
        r'<div\s+class="?figure-extra"?[^>]*>.*?</div>',
        '', body_html, flags=re.DOTALL,
    )

    # Strip the Supporting Information section. Its body is a download
    # table (Filename / Description) plus a publisher disclaimer; both
    # are publisher chrome, not paper content.
    body_html = re.sub(
        r'<section\s+class="?article-section\s+article-section__supporting"?[^>]*>.*?</section>',
        '', body_html, flags=re.DOTALL,
    )

    # Find references section boundary
    ref_m = re.search(
        r'<section\s+class="?article-section\s+article-section__references"?',
        body_html,
    )
    if ref_m:
        before = body_html[:ref_m.start()]
        after = body_html[ref_m.end():]
    else:
        before = body_html
        after = ""

    # Parse before-refs block
    before = extract_captions(before)
    before = strip_common(before)
    text = tags_to_text(before)
    parts = [text] if text.strip() else []

    # Parse supplementary sections that come after references
    if after:
        for sm in re.finditer(
            r'<section\s+class="?article-section[^>]*>(.*?)</section>',
            after, re.DOTALL,
        ):
            inner = sm.group(1)
            hm = re.search(r'<h[23][^>]*>(.*?)</h[23]>', inner, re.DOTALL)
            heading = strip_tags(hm.group(1)).strip() if hm else ""
            if heading and _SUPP_RE.search(heading):
                chunk = extract_captions(inner)
                chunk = strip_common(chunk)
                supp_text = tags_to_text(chunk)
                if supp_text.strip():
                    parts.append(supp_text)

    result = "\n\n".join(parts)
    return drop_noise(result, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Wiley HTML into a papers/*.json-format dict."""
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
