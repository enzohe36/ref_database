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
    remove_elements_by_id,
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

def remove_banners(html):
    """Normalize Wiley HTML to a single centered text column.

    Chrome stripped (Step 3):
      - <header class=page-header> (top nav) and <footer
        class=page-footer>.
      - `<nav class=skip-links>` and the floating article toolbar
        (`<nav class="coolBar stickybar">` with QR code / PDF / Cite /
        Share buttons).
      - "Citing Literature" panel (#cited-by section) — per note main
        text ends before "Citing Literature".

    Reading column: the outer <article data-mathjax ...> wraps the
    journal + volume/issue/pages line, the title, authors, abstract,
    body, references. Per note start anchor is the journal+vol/issue
    line which sits inside this outer article.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    html = _remove_nested_element(html, r"<header\b[^>]*>")
    html = _remove_nested_element(html, r"<footer\b[^>]*>")
    # Skip-links navigation (on-screen at narrow vw).
    html = _remove_nested_element(
        html, r'<nav\b[^>]*\bclass="[^"]*skip-links[^"]*"[^>]*>'
    )
    # Note: `<nav class="coolBar stickybar">` is sticky at desktop
    # but renders `position: static` at narrow viewports — an inline
    # TOC-like row ("About / Sections / CITE / Tools / Share").
    # Keep it.
    # "Citing Literature" section at the bottom of the article.
    html = remove_elements_by_id(html, "cited-by")
    # Also strip the trailing "Related articles" panel in the <aside>
    # inside the article and the access-denial slot placeholder.
    html = remove_elements_by_id(html, "accessDenialslot")
    # Leaderboard ad above the article (`.banner-wrapper` housing
    # `.advert-leaderboard`).
    html = _remove_nested_element(
        html, r'<div\b[^>]*\bclass="[^"]*\bbanner-wrapper\b[^"]*"[^>]*>'
    )
    html = remove_elements_by_id(html, "advert-leaderboard")
    # Stray "Download PDF" link div right after the article (bare inline
    # style `text-align:right`). Matches the specific inline-style +
    # single anchor child pattern Wiley puts at that position.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*style=text-align:right!important[^>]*>',
    )
    # Osano Cookie Preferences floating widget button (bottom-left
    # sticky: `<button class="osano-cm-window__widget">`).
    for _ in range(4):
        before = html
        html = _remove_nested_element(
            html,
            r'<button\b[^>]*\bclass="[^"]*\bosano-cm-widget\b[^"]*"[^>]*>',
        )
        if html == before:
            break
    html = _remove_nested_element(
        html, r'<div\b[^>]*\bclass="[^"]*\bosano-cm\b[^"]*"[^>]*>'
    )

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important;"
        "overflow-y:overlay !important}"
        "html::-webkit-scrollbar{width:0 !important;height:0 !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        "main,#main-content,.pageBody,.page-body,#pb-page-content,"
        ".container,.container-fluid,.row,"
        "[class*=col-]{"
        "display:block !important;float:none !important;"
        "width:100% !important;max-width:100% !important;"
        "min-width:0 !important;flex:0 0 auto !important;"
        "margin:0 !important;padding:0 !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # Outer article wraps journal/vol/issue line + title + body +
        # references.
        "article[data-mathjax]{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        "article[data-mathjax] *{"
        "max-width:100% !important;min-width:0 !important}"
        # Zero margin only on the wrapper's DIRECT first/last children
        # so nested section headings keep their native vertical rhythm.
        "article[data-mathjax]>*:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        "article[data-mathjax]>*:last-child,"
        "article[data-mathjax]>*:last-child>*:last-child,"
        "article[data-mathjax]>*:last-child>*:last-child>*:last-child,"
        "article[data-mathjax]>*:last-child>*:last-child>*:last-child>*:last-child,"
        "article[data-mathjax]>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child,"
        "article[data-mathjax]>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child"
        "{margin-bottom:0 !important;padding-bottom:0 !important}"
        # Kill only the *margin*-bottom cascade on nested last-children
        # (descendant combinator). The Bibliography accordion's nested
        # `margin-bottom` chain at .accordion / .article-section /
        # ancestor wrappers otherwise leaves 62-68 px of trailing
        # whitespace below the last reference. Padding-bottom is left
        # intact so `.accordion__content` (which has a 1px solid blue
        # border on all sides + padding:1.5rem) keeps its 24-px inner
        # gap between the last reference and the bottom border.
        "article[data-mathjax] *:last-child{"
        "margin-bottom:0 !important}"
        # Hide `.getFTR__placeholder` — a 40-px tall empty `<div>` that
        # the GetFTR JS service populates with an icon/status badge.
        # In a static capture it's dead and just inflates the .extra-
        # links flex row to 48 px tall, leaving 15 px of empty space
        # below the visible link text and pushing measured B at the
        # last reference from 56 to 71-72 at vw ≥ 720.
        "article[data-mathjax] .extra-links .getFTR__placeholder{"
        "display:none !important}"
        # Force the journal + volume/issue/pages line visible at the
        # top of the article per format-html-extra note. Native CSS
        # hides .volume-issue and .citation__page-range at desktop
        # viewports (and the 752-px body cap forces narrow layout).
        # Use the narrow `inline-block` so the slash separator
        # (`.citation__page-range::before { content:"/" }`, also
        # narrow-only) sits between volume and pages without inserting
        # a line break.
        "article[data-mathjax] .volume-issue,"
        "article[data-mathjax] .citation__page-range{"
        "display:inline-block !important;visibility:visible !important}"
        # Restore narrow-only "/" separators that the publisher
        # places between (a) journal name and volume-issue and
        # (b) volume-issue and citation__page-range. The publisher's
        # desktop @media overrides both ::before/::after content to
        # `none`.
        "article[data-mathjax] .citation__page-range::before{"
        "content:\"/\" !important}"
        "article[data-mathjax] .journal-banner-text>a::after{"
        "content:\"/\" !important;margin:0 0.1875rem !important}"
        # Keep the journal name inline with volume/issue/pages on a
        # single line at any viewport. The publisher's `.journal-
        # banner-text` switches from `display:inline-block` (narrow)
        # to `display:block` at desktop, pushing volume + pages onto
        # a second line. Lock to the narrow inline-block form.
        "article[data-mathjax] .journal-banner-text{"
        "display:inline-block !important}"
        # Expand the Bibliography accordion. Native markup sets
        # style="display:none" inline on .accordion__content; override.
        # Also lock padding-bottom to the narrow value (1.125rem) — the
        # publisher's desktop @media bumps it to 1.5rem, which adds 6 px
        # of trailing whitespace below the last reference at vw≥820.
        "article[data-mathjax] .accordion__content{"
        "display:block !important;height:auto !important;"
        "max-height:none !important;overflow:visible !important;"
        "padding-bottom:1.125rem !important}"
        # Hide the right-rail <aside> and its col container (related
        # articles, metrics, figures-jump) that render alongside the
        # article at wide vw.
        "article[data-mathjax] aside,"
        "article[data-mathjax] .article-row-right{"
        "display:none !important}"
        # Hide the "Citing Literature" / trending / recommended panels
        # inside #publicationContentRefs, and the access-denial placeholder.
        "article[data-mathjax] #cited-by,"
        "article[data-mathjax] .issue-items,"
        "article[data-mathjax] .article-recommendations,"
        "article[data-mathjax] .recommended-articles,"
        "article[data-mathjax] #accessDenialslot{"
        "display:none !important}"
        # Figures: wiley wraps each figure in
        #   <figure class=figure id=f<N>>
        #     <a target=_blank href=https://onlinelibrary.wiley.com/cms/asset/<uuid>/<file>.<ext>>
        #       <picture>
        #         <img class=figure__image src="data:..." data-lg-src=/cms/asset/<uuid>/<file>.<ext>
        #              loading=lazy>
        #       </picture>
        #     </a>
        #     <figcaption class=figure__caption>
        #       <div class=figure__caption__header>
        #         <strong class=figure__title>Figure N</strong>
        #         <div class=figure-extra>
        #           <a class=open-figure-link>Open in figure viewer</a>
        #           <a class=ppt-figure-link>PowerPoint</a>
        #         </div>
        #       </div>
        #       <div class="figure__caption figure__caption-text"><p>...</p></div>
        #     </figcaption>
        #   </figure>
        # Native order: image above caption (correct). Images lazy-load
        # via `data-lg-src`; partial captures leave many figures at
        # placeholder src. get_refs.py uses `_WILEY_FIGURES_FIX_JS` to
        # swap src ← parent <a href> (same as data-lg-src). Visual
        # fixes: force img full-width above caption, hide JS-only
        # "Open in figure viewer" + "PowerPoint" buttons in
        # `.figure-extra`.
        "article[data-mathjax] figure.figure{"
        "margin:1rem 0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important;"
        "display:block !important}"
        "article[data-mathjax] figure.figure > a,"
        "article[data-mathjax] figure.figure picture{"
        "display:block !important;margin:0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important}"
        "article[data-mathjax] figure.figure img.figure__image{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
        # Drop the JS-only figure-extra toolbar.
        "article[data-mathjax] figure.figure .figure-extra{"
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
