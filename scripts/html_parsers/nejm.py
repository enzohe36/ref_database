"""NEJM (nejm.org) HTML parser."""

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
    remove_elements_by_id,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "Go to Citation",
    "Crossref",
    "PubMed",
    "Web of Science",
    "Google Scholar",
    "OpenURL",
    "Copy Citation",
    "Download",
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
    """Normalize NEJM HTML to a single centered text column.

    The reading column spans `<article>` from the article header through the
    last reference. NEJM ships heavy site chrome (sticky top header, navigation
    menus, share toolbars, related-content rails, info panel tabs, figure
    viewers, "More from this issue" lists, footer) that surrounds the
    `<article>` element. The strategy is to keep `<article>` and strip
    everything outside or floating above it.
    """
    # Lock layout to publisher's narrow form at any viewport.
    html = neutralize_media_queries(html)

    # (a) Top blocks ------------------------------------------------
    # TrustArc cookie consent overlay (loaded via scripts, but
    # SingleFile may capture leftover banner DOM).
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass=["\']?[^"\'>]*\btruste[_-]\w+\b',
        )
        if html == before:
            break
    # Site header (logo + nav + search) at the very top of <body>.
    html = _remove_nested_element(html, r'<header\b[^>]*\bclass=["\']?[^"\'>]*\bng-header\b')
    # Site-header overlay + leaderboard ad slot above <article>.
    for cls in ("ng-header_overlay", "ng-header_after"):
        for _ in range(3):
            before = html
            html = _remove_nested_element(
                html, rf'<div\b[^>]*\bclass=["\']?[^"\'>]*\b{cls}\b',
            )
            if html == before:
                break
    # Global ad banner immediately under the header.
    html = remove_elements_by_id(html, "ad-global-banner-FULLx64-1")
    # Article-tools modals (Save, Alert, Citation, doPopup) sitting
    # between body and article, hidden by default but visible in
    # SingleFile snapshots.
    for cls in (
        "article-tools__savePopup",
        "article-tools__articleAlertPopup",
        "ng-do-media_popup",
    ):
        for _ in range(3):
            before = html
            html = _remove_nested_element(
                html, rf'<div\b[^>]*\bclass=["\']?[^"\'>]*\b{cls}\b',
            )
            if html == before:
                break
    # In-article popup overlays (Reference popup, etc.).
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\breferences-popup-wrapper\b',
        )
        if html == before:
            break
    # meta-panel right-content holds share / article-tools buttons; the
    # left-content carries the "Original Article" article-type badge
    # which we want to keep as the first visible element. Strip only
    # the right-content side. info-panel (Sign in / Share / Cite / View
    # PDF action bar lower in the article header) is kept — it is the
    # primary article-action toolbar and belongs in the reading column.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bmeta-panel__right-content\b',
        )
        if html == before:
            break
    # Remove the [hidden] HTML attribute from bibliography listitems so
    # the publisher's flex layout for label + citation continues to
    # apply when references past index 5 become visible. CSS-overriding
    # [hidden] with display:block clobbers the listitem's flex display
    # and the "1." / "2." labels stack above each citation instead of
    # sitting beside them. Stripping the attribute keeps DOM consistent
    # with parser output (parser walks `<div id=rN class=citations>`,
    # which is unaffected) and lets the publisher's stylesheet render
    # the items normally.
    html = re.sub(
        r'(<div\b[^>]*\brole=listitem\b[^>]*?)\s+hidden(?=[\s>])',
        r"\1",
        html,
    )
    # Inline "Quick Take" and "Research Summary" preview cards that float
    # alongside the abstract (rendered as cards with thumbnail + title).
    # They duplicate the article title and link to derivative content;
    # not part of the article body.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass="(?:[^"]*\s)?core-digital-object(?:\s[^"]*)?"',
        )
        if html == before:
            break
    # "Show all references" / "Show fewer" toggle button (chrome that
    # gates the bibliography clamp behavior — we force-show everything
    # via CSS below, so the button is meaningless).
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\btruncation-wrapper\b',
        )
        if html == before:
            break

    # (b) Floating blocks -------------------------------------------
    # Right-rail collateral tabs. Lives inside <article> but is sidebar
    # chrome that duplicates body figure captions / table captions /
    # reference list, leaking content into main_text. The shared
    # remove_elements_by_id helper matches `tab-information` inside
    # `tab-information-label` (button id — `\b` treats `-` as a word
    # boundary), so use a stricter section-id regex with `(?=[\s>])`
    # lookahead anchored on attribute-end.
    #
    # Sections kept (NOT stripped) because the parser reads structured
    # data from them:
    #   - tab-contributors: holds <div id=conN property=author> blocks
    #     with given/familyName + affiliation microdata. Required by
    #     _parse_authors. The body slice in _parse_main_text ends at
    #     bibliography (well before tab-contributors), so this section
    #     never leaks into main_text.
    #   - tab-information: holds the Published-In / Copyright / Topics
    #     metadata; benign in DOM, also outside the main-text slice.
    for sid in (
        "tab-metrics-inner", "tab-citations", "tab-figures",
        "tab-tables", "cited-by",
    ):
        for _ in range(3):
            before = html
            html = _remove_nested_element(
                html,
                rf'<section\b[^>]*\bid={re.escape(sid)}(?=[\s>])',
            )
            if html == before:
                break
    # Article-collateral pill nav (sticky nav with icons on the right).
    html = remove_elements_by_id(html, "article_collateral_menu")
    # Right-rail "core-collateral-tables" / "core-collateral-figures"
    # blocks listing the full table + figure caption again. Strip so
    # main_text doesn't duplicate the inline figcaptions.
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html,
            r'<section\b[^>]*\bclass=["\']?[^"\'>]*\bcore-collateral-(?:tables|figures)\b',
        )
        if html == before:
            break
    # Figure Viewer / Table Viewer modals (large media overlays).
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bcore-fv\b',
        )
        if html == before:
            break

    # In-article floating chrome (all live inside <article> alongside
    # the body content so they survive the post-</article> wipe below).
    # Drop them physically — none belong in the reading column.
    #   - core-sections-menu: sticky "ABSTRACT / METHODS / RESULTS /
    #     ... / REFERENCES" navigation pill that the reader perceives
    #     as a floating bar on one side.
    #   - more-like-this: bottom "MORE LIKE THIS" article-card list.
    #   - relatedArticlesWidget: in-article "Related articles" rail.
    #   - literatumAd: in-article ad slot.
    #   - UX3HTMLWidget: free-form publisher widget panel.
    #   - companion-sr-only-focusable: invisible "open AI companion"
    #     button that body.innerText surfaces at the top of the page.
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bcore-sections-menu\b',
        )
        if html == before:
            break
    # Custom elements can't be removed by _remove_nested_element
    # (its tag-detection regex stops at hyphens). Walk the open/close
    # pairs manually.
    while True:
        m = re.search(r"<more-like-this\b[^>]*>", html)
        if not m:
            break
        end_m = re.search(r"</more-like-this\s*>", html[m.end():])
        if not end_m:
            break
        html = html[: m.start()] + html[m.end() + end_m.end():]
    for widget in ("relatedArticlesWidget", "literatumAd", "UX3HTMLWidget"):
        for _ in range(5):
            before = html
            html = _remove_nested_element(
                html, rf'<div\b[^>]*\bdata-widget-def={widget}\b',
            )
            if html == before:
                break
    html = re.sub(
        r'<a\b[^>]*\bclass=["\']?[^"\'>]*\bcompanion-sr-only-focusable\b[^>]*>.*?</a>',
        "", html, flags=re.DOTALL,
    )
    # In-article "Related articles" rail right before the bibliography
    # close. Lives inside <article> as <div class=ng-related-articles>.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bng-related-articles\b',
        )
        if html == before:
            break
    # Right-rail aside that wraps Med Rectangle ad slot, sidebar
    # spacing dividers, NEJM Jobs widget, etc. Empty after chrome
    # strips above; leaves a 32-px margin-top stub against the bottom
    # of the body wrapper that pushes the article's pb=56 measurement
    # past tolerance.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<aside\b[^>]*\bdata-core-aside=right-rail\b',
        )
        if html == before:
            break
    # Right-rail tabbed dialog. Sits between the bibliography and
    # </article> with `<div class=core-collateral role=dialog aria-modal=true>`.
    # The aria-modal=true plus the publisher's z-index:1040 backdrop
    # painted via the .modal-backdrop rule create a semi-transparent
    # gray block over the entire page in the static SingleFile snapshot.
    # Strip it physically — but first preserve the inner
    # <section id=tab-contributors> so _parse_authors can read its
    # structured author + affiliation microdata. The preserved markup
    # is re-inserted as a display:none wrapper just before </article>
    # so it stays inside the parser's traversal scope.
    tabs_m = re.search(
        r'<section\b[^>]*\bid=tab-contributors\b[^>]*>.*?</section>\s*</section>',
        html, re.DOTALL,
    )
    saved_tab = tabs_m.group() if tabs_m else ""
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=core-collateral\b',
        )
        if html == before:
            break
    if saved_tab:
        html = html.replace(
            "</article>",
            f'<div hidden style="display:none !important">{saved_tab}</div></article>',
            1,
        )
    # Strip everything between </article> and </body>. NEJM packs the
    # post-article wrapper with: Download-PDF rail, article navigation,
    # to-citation accordions, sticky-header re-render, "More from this
    # issue" recirc widget, "Related articles", "More Like This",
    # "CareerCenter" job listings, "Tap into groundbreaking research"
    # subscribe CTA, and the site footer with NEJM Group nav. None of
    # this belongs in the reading column. Everything from </article>
    # forward is chrome — drop it wholesale, leaving only </body>/</html>
    # closes that the saved page may not include.
    art_close = html.find("</article>")
    if art_close != -1:
        post = html[art_close + len("</article>"):]
        body_close = post.find("</body>")
        html_close = post.find("</html>")
        tail = ""
        if body_close != -1:
            tail = post[body_close:]
        elif html_close != -1:
            tail = post[html_close:]
        html = html[: art_close + len("</article>")] + tail
    # Site footer (already covered by the post-article wipe above when
    # it sat outside <article>; keep this strip as a safety net for
    # SingleFile captures where the footer is nested inside <article>).
    html = _remove_nested_element(html, r"<footer\b[^>]*>")

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
        # Cap the article wrapper. The reading column starts at the
        # "Original Article" badge inside the article header and ends
        # at the last reference (the post-bibliography tab-information /
        # tab-contributors sections are kept in the DOM so the parser
        # can read structured authors, but hidden via CSS below so the
        # rendered column ends after references).
        "article{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;"
        # pb trimmed from 56 to 50 to absorb a ~6-px line-box descent
        # below the last reference's text. external-links are hidden
        # via CSS below and the last-listitem margin-bottom is zeroed,
        # so this 6-px correction is uniform across viewports.
        "padding:56px 16px 50px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        "article>*:first-child{margin-top:0 !important;padding-top:0 !important}"
        "article>*:last-child{margin-bottom:0 !important;padding-bottom:0 !important}"
        # Author list — NEJM defaults to a compact byline ("First 7
        # authors, +18, Last author"). The +18 reveal is JS-driven and
        # dead in the static capture. Force every hidden author span
        # visible and hide the +N reveal button.
        "[data-hidden-on=all]{display:inline !important;"
        "visibility:visible !important}"
        "[data-action=reveal]{display:none !important}"
        # Reference list — the [hidden] attribute on listitems past
        # index 5 is stripped from the DOM above (so the publisher's
        # flex layout for label + citation applies). No CSS override
        # needed.
        # core-collateral is now stripped from the DOM above (with
        # tab-contributors moved into a display:none wrapper before
        # </article> so the parser still finds it). Also hide any
        # publisher modal-backdrop CSS that may still paint a gray
        # overlay independently.
        ".modal-backdrop{display:none !important}"
        "[hidden]{display:none !important}"
        # The reading column should end after the references list —
        # hide the post-references "Notes" / acknowledgments section
        # and the "Supplementary Material" file-list. Both stay in the
        # DOM (supplementary-materials feeds main_text via the parser);
        # CSS-hidden so they do not render.
        "section#backnotes,section#supplementary-materials{"
        "display:none !important}"
        # Per-reference action toolbar (Go to Citation / Crossref /
        # PubMed / Web of Science / Google Scholar / OpenURL). Wraps
        # to a second row at narrow viewports and pushes B past the
        # 56-px tolerance. The DOI / external-link data is captured
        # inline in the JSON output, so the toolbar adds no value to
        # the reading column.
        "#bibliography-collapsible-text .external-links{"
        "display:none !important}"
        # Zero the trailing margin on the last reference so the
        # bibliography ends flush against the article's pb.
        "#bibliography-collapsible-text>[role=listitem]:last-child{"
        "margin-bottom:0 !important}"
        # Constrain figures, tables, and images to the text-column
        # width so wide tables don't blow past the 720-px cap.
        "article figure,article .figure-wrap,"
        "article .table-wrap,article .graphic-wrap{"
        "max-width:100% !important;width:auto !important;"
        "margin-left:0 !important;margin-right:0 !important}"
        "article table{max-width:100% !important;"
        "width:100% !important;table-layout:fixed !important;"
        "word-wrap:break-word !important}"
        "article img{max-width:100% !important;height:auto !important}"
        # Figures: SingleFile inlines the high-res JPEG (~600-1000 KB)
        # on the `<img>` inside `<figure id=f<N> class=graphic>`. Native
        # `<figure>` browser default has 40 px horizontal margin which
        # shaves the image off the column edges, and the img keeps its
        # intrinsic width=2640 attribute. Force the figure to zero
        # horizontal margin and the img to block + 100% width above the
        # `<figcaption>`.
        ":root article figure.graphic{"
        "margin:1rem 0 !important;padding:0 !important}"
        ":root article figure.graphic > img{"
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

    NEJM exposes metadata via three sources (in priority order):
      - <meta name=dc.Title|dc.Identifier|dc.Date> — title, DOI, date
      - <meta name=citation_journal_title> — journal name (full title)
      - <div class=core-self-citation> with schema.org microdata —
        volume, issue, page range, datePublished

    The journal full title ("New England Journal of Medicine") is
    normalized to its NLM MedAbbr ("N Engl J Med") by
    convert_html._abbreviate_journals after parsing.
    """
    title = get_meta(html, "dc.Title")
    if not title:
        m = re.search(r"<h1[^>]*\bproperty=name[^>]*>(.*?)</h1>", html, re.DOTALL)
        if m:
            title = strip_tags(m.group(1)).strip()
    title = unescape(title).strip().rstrip(".") if title else ""

    journal = get_meta(html, "citation_journal_title")
    journal = unescape(journal).replace(".", "").strip() if journal else ""

    doi = get_meta(html, "dc.Identifier")
    if doi and not doi.startswith("10."):
        # dc.Identifier may be a publisher-id; prefer the doi-scheme entry.
        m = re.search(
            r'<meta[^>]*scheme=["\']?doi["\']?[^>]*content=["\']?([^"\'>]+)',
            html,
        )
        if m:
            doi = m.group(1)
    doi = format_doi(doi) if doi else ""

    # Volume / issue / pages / year from core-self-citation microdata.
    volume = ""
    issue = ""
    pages = ""
    year = ""

    csc = re.search(
        r"<div[^>]*\bclass=core-self-citation[^>]*>(.*?)</div>\s*<div\s+class=info-panel",
        html, re.DOTALL,
    )
    body = csc.group(1) if csc else html

    vm = re.search(r'property=volumeNumber[^>]*>(\d+)', body)
    if vm:
        volume = vm.group(1)
    im = re.search(r'property=issueNumber[^>]*>(\d+)', body)
    if im:
        issue = im.group(1)
    fp = re.search(r'property=pageStart[^>]*>([\w\-]+)', body)
    lp = re.search(r'property=pageEnd[^>]*>([\w\-]+)', body)
    if fp and lp:
        pages = f"{fp.group(1)}-{lp.group(1)}"
    elif fp:
        pages = fp.group(1)

    # Year: prefer dc.Date (WTN8601 scheme), else datePublished microdata.
    date = get_meta(html, "dc.Date")
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)
    if not year:
        m = re.search(r"property=datePublished[^>]*>([^<]+)", body)
        if m:
            ym = re.search(r"(\d{4})", m.group(1))
            if ym:
                year = ym.group(1)

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

def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.

    NEJM exposes authors twice:
      - In the article-header byline as `<span property=author>...</span>` —
        compact list with optional "+N" reveal toggle.
      - In `<div id=conN property=author typeof=Person data-expandable=item>`
        inside `<section id=tab-contributors>` — full list with structured
        affiliations.

    The con-block list is the source of truth: it always contains every
    author, has structured given/family-name spans, and lists each
    author's affiliation as `<div property=affiliation typeof=Organization>
    <span property=name>...</span></div>`.
    """
    authors = []
    # Walk every <div id=conN property=author>...</div> block.
    for m in re.finditer(
        r'<div[^>]*\bid=con\d+[^>]*\bproperty=author[^>]*>',
        html,
    ):
        start = m.start()
        # Find matching close — use the simple count-based walker so
        # nested <div>s inside the affiliations panel don't terminate
        # the block early.
        depth = 1
        pos = m.end()
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
                pos += nc.end()
        block = html[start:pos]

        gm = re.search(
            r'<span[^>]*\bproperty=givenName[^>]*>(.*?)</span>',
            block, re.DOTALL,
        )
        fm = re.search(
            r'<span[^>]*\bproperty=familyName[^>]*>(.*?)</span>',
            block, re.DOTALL,
        )
        if not (gm and fm):
            continue
        given = unescape(strip_tags(gm.group(1))).strip()
        surname = unescape(strip_tags(fm.group(1))).strip()

        affiliations = []
        for am in re.finditer(
            r'<div[^>]*\bproperty=affiliation[^>]*>(.*?)</div>',
            block, re.DOTALL,
        ):
            inner = am.group(1)
            nm = re.search(
                r'<span[^>]*\bproperty=name[^>]*>(.*?)</span>',
                inner, re.DOTALL,
            )
            text = strip_tags(nm.group(1) if nm else inner)
            text = unescape(re.sub(r"\s+", " ", text)).strip().rstrip(",.")
            if text:
                affiliations.append(text)

        authors.append({
            "author": format_name(given, surname),
            "affiliation": affiliations,
        })

    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_ref_authors_title(text):
    """Split 'Authors. Title.' head into ([author strings], title).

    NEJM body refs encode authors in compact "LastName IN" form, with
    several variations:
        - "Burstein HJ, Somerfield MR, Barton DL, et al."
        - "Bidard F-C, Kaklamani VG, Neven P, et al."  (hyphenated initials)
        - "Sledge GW Jr, Toi M, Neven P, et al."       (Jr/Sr/III suffix)
        - "van Kruchten M, de Vries EG, ... et al."    (lowercase prefix)
        - "O’Shaughnessy J, Burris HA, et al."   (curly apostrophe)

    Boundary detection uses two cues, in order:
      1. "et al" — split there (most NEJM refs of >3 authors).
      2. Last "Name Initials." pattern — the period closing the final
         author marks the start of the title. Must be followed by a
         capital letter (the title's first word).

    The matched author run is then comma-split and routed through
    format_author_name, which is forgiving of the publisher's name
    quirks (hyphens, apostrophes, prefixes, suffixes).
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return [], ""

    # Normalize curly quotes so the surname regex character class can match.
    norm = text.replace("’", "'").replace("‘", "'")

    # Cue 1: "et al" terminator.
    em = re.search(r"\bet\s+al\.?\s*", norm)
    if em:
        authors_str = norm[:em.start()].rstrip(", .")
        title = norm[em.end():].lstrip(" .,").rstrip(".")
        return _split_nejm_authors(authors_str), title

    # Cue 2: search for the period that ends the author list. The
    # period sits immediately after compact initials (1-4 capitals,
    # optionally separated by hyphens or dots — e.g. "JA", "F-C",
    # "J.A.") plus an optional Jr/Sr/III suffix and is followed by a
    # space + capital letter (title start).
    boundary_re = re.compile(
        r"\s+[A-Z](?:[A-Z]|[-‐-–\.][A-Z])*"
        r"(?:\s+(?:Jr|Sr|II|III|IV|V|2nd|3rd|4th)\.?)?"
        r"\.\s+(?=[A-ZÀ-ſ])"
    )
    matches = list(boundary_re.finditer(norm))
    if matches:
        m = matches[0]
        # The match ends at "Period + space"; pull the period position.
        period_offset = m.group(0).rfind(".")
        cut = m.start() + period_offset
        authors_str = norm[:cut].rstrip(", ")
        title = norm[cut + 1:].lstrip(" ").rstrip(".")
        return _split_nejm_authors(authors_str), title

    # No clear boundary — return everything as title with no authors.
    return [], norm.rstrip(".")


def _split_nejm_authors(authors_str):
    """Split a comma-separated NEJM author list and normalize each.

    Filters out empty fragments and any leftover "et al" tokens; routes
    each remaining chunk through format_author_name to handle hyphenated
    initials, lowercase prefixes, suffixes, and curly apostrophes.
    """
    out = []
    for raw in authors_str.split(","):
        chunk = raw.strip().rstrip(".")
        if not chunk:
            continue
        if re.match(r"^et\s+al\.?$", chunk, re.IGNORECASE):
            continue
        out.append(format_author_name(chunk))
    return out


def _parse_reference_block(block):
    """Parse one <div id=rN class=citations> reference block.

    The citation text is inside `<div class=citation-content>` in the form:
        "Authors. Title. <em>Journal</em> YYYY;Vol(Issue):Pages."
    The DOI is in a Crossref link or directly resolvable; PubMed link is
    also present but a CrossRef href that points at doi.org is preferred.
    """
    out = {
        "title": "", "journal": "", "year": "",
        "volume": "", "issue": "", "pages": "",
        "doi": "", "authors": [],
    }

    # DOI: prefer direct doi.org URLs; when the Crossref link is the
    # NEJM /servlet/linkout gateway form
    # (`...?...&amp;key=10.1056%2FXxx&amp;...`), unwrap the `key`
    # parameter (encoded with HTML entities, so search across `&` or
    # `&amp;`).
    dm = re.search(
        r'<a[^>]+href=["\']?(https?://(?:dx\.)?doi\.org/[^\s"\'<>]+)["\']?'
        r'[^>]*>\s*Crossref\s*</a>',
        block,
    )
    if not dm:
        dm = re.search(
            r'href=["\']?(https?://(?:dx\.)?doi\.org/[^\s"\'<>]+)',
            block,
        )
    if dm:
        out["doi"] = format_doi(dm.group(1).rstrip(".,"))
    else:
        km = re.search(
            r"(?:&|&amp;)key=([^&\"'<>\s]+?)(?:&|&amp;|[\"'])",
            block,
        )
        if km:
            from urllib.parse import unquote
            doi_raw = unquote(km.group(1))
            if doi_raw.startswith("10."):
                out["doi"] = format_doi(doi_raw)

    cm = re.search(
        r'<div[^>]*\bclass=citation-content[^>]*>(.*?)</div>',
        block, re.DOTALL,
    )
    if not cm:
        return out
    content_html = cm.group(1)

    # Journal in <em>...</em>
    jm = re.search(r"<em>(.*?)</em>", content_html, re.DOTALL)
    journal = ""
    if jm:
        journal = strip_tags(jm.group(1)).strip().rstrip(".")
    out["journal"] = journal

    # Build a tagless string for parsing the rest, but keep the journal
    # boundary so we know where the title ends.
    head_html = content_html[: jm.start()] if jm else content_html
    tail_html = content_html[jm.end():] if jm else ""
    head = unescape(re.sub(r"\s+", " ", strip_tags(head_html))).strip()
    tail = unescape(re.sub(r"\s+", " ", strip_tags(tail_html))).strip()

    authors, title = _parse_ref_authors_title(head)
    out["authors"] = authors
    out["title"] = title

    # Year and pagination tail. Forms observed:
    #   " 2021;39:3959-3977."                    plain
    #   " 2022;40:Suppl 16:1032-1032."           ASCO meeting abstract
    #   " 2024;42:Suppl:LBA1001-LBA1001."        ASCO with no Suppl number
    #   " 2023;83:Suppl 5:P3-07-28-P3-07-28."    SABCS abstract
    #   " 2022;13(4):e12345."
    tail = tail.lstrip(" .,;")
    # Salvage journal-name typos that leak a single trailing letter
    # outside the </em> (e.g. "<em>Cancer Re</em>s 2022;...").
    lm = re.match(r"^([A-Za-z]{1,3})\s+(?=\d{4}\s*;)", tail)
    if lm and out["journal"]:
        out["journal"] = (out["journal"] + lm.group(1)).strip().rstrip(".")
        tail = tail[lm.end():]
    ym = re.match(
        r"^(?P<year>\d{4})\s*;\s*"
        r"(?P<vol>\d+\w*)\s*"
        r"(?:\((?P<issue1>[^)]+)\))?"
        r"\s*(?::\s*(?P<issue2>[Ss]uppl(?:\s+\w+)?))?"
        r"\s*(?::\s*(?P<pages>[\w][\w\-‐-–—.]*))?",
        tail,
    )
    if ym:
        out["year"] = ym.group("year")
        out["volume"] = ym.group("vol") or ""
        out["issue"] = (ym.group("issue1") or ym.group("issue2") or "").strip()
        out["pages"] = (ym.group("pages") or "").replace("–", "-").rstrip(".")
    else:
        # Year only — book / older format.
        ym2 = re.search(r"(\d{4})", tail)
        if ym2:
            out["year"] = ym2.group(1)

    # Prescribing-information / regulatory document: when the parser
    # detected no authors and no volume/pages, the `<em>` content is
    # the document's full title (italicized like a book) rather than a
    # journal. Swap so title carries the document name and journal is
    # empty (NEJM emits "Stemline Therapeutics. <em>Orserdu (elacestrant):
    # highlights of prescribing information</em>. 2023 (URL)").
    if (
        not out["authors"]
        and not out["volume"]
        and not out["pages"]
        and out["journal"]
        and out["title"]
        and len(out["title"].split()) <= 3
    ):
        out["title"], out["journal"] = out["journal"], ""

    return out


def _parse_references(html):
    """Extract NEJM reference list.

    Each reference lives in `<div id=rN class=citations>...</div>` inside
    `<section id=bibliography>`. The structured citation text is in
    `<div class=citation-content>`; external-links siblings carry DOI,
    PubMed, and Web of Science links.
    """
    bm = re.search(
        r'<section[^>]*\bid=bibliography\b[^>]*>(.*?)</section>',
        html, re.DOTALL,
    )
    if not bm:
        return []
    bib = bm.group(1)

    refs = []
    for m in re.finditer(
        r'<div\s+id=r\d+\s+class=citations>',
        bib,
    ):
        start = m.start()
        depth = 1
        pos = m.end()
        while depth > 0 and pos < len(bib):
            no = re.search(r"<div[\s>]", bib[pos:])
            nc = re.search(r"</div>", bib[pos:])
            if nc is None:
                break
            if no and no.start() < nc.start():
                depth += 1
                pos += no.end()
            else:
                depth -= 1
                pos += nc.end()
        block = bib[start:pos]
        refs.append({"": _parse_reference_block(block)})

    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _strip_decorative_blocks(html):
    """Remove inline collateral that should not appear in main_text.

    NEJM body inserts:
      - <div class="core-digital-object ...> Visual Abstract / Audio
        Summary buttons inside the abstract.
      - <div class=quick-take> "Quick Take" video CTAs.
      - reference popups (already removed in remove_banners but may
        appear inline in content).
      - external-links toolbars under each table/figure header.
    Strip these before tags_to_text so they don't leak into output.
    """
    for sel in (
        "core-digital-object",
        "quick-take",
        "external-links",
        "table-tools",
        "figure-tools",
        "fig-tools",
    ):
        for _ in range(5):
            before = html
            html = _remove_nested_element(
                html,
                rf'<div\b[^>]*\bclass=["\']?[^"\'>]*\b{re.escape(sel)}\b',
            )
            if html == before:
                break
    # Remove the "Open in Viewer" buttons inside <header> of figure wrappers.
    html = re.sub(
        r"<button\b[^>]*\bdata-open=viewer\b[^>]*>.*?</button>",
        "", html, flags=re.DOTALL,
    )
    # NEJM wraps each figure / table footnote in a div with
    # `role=doc-footnote`. _helpers.extract_captions treats these as
    # standalone footnotes AND re-captures them via the surrounding
    # `<figcaption>` walker, producing duplicate text in main_text.
    # Strip the role attribute so only the figcaption walker fires.
    html = re.sub(
        r'(<div[^>]*?)\brole=["\']?doc-footnote["\']?',
        r"\1",
        html,
    )
    return html


def _parse_main_text(html):
    """Extract body text from NEJM article.

    Boundary rules:
      - Body sections: keep everything from <section id=summary-abstract>
        through the last <section id=sec-N> (just before bibliography).
      - Supplementary: keep the `<section id=supplementary-materials>`
        block, which carries supplementary file labels (NEJM does not
        inline supplementary content).
      - Remove all references sections (`<section id=bibliography>`).
    """
    abs_m = re.search(
        r'<section[^>]*\bid=summary-abstract\b[^>]*>',
        html,
    )
    body_m = re.search(
        r'<section[^>]*\bid=bodymatter\b[^>]*>',
        html,
    )
    bib_m = re.search(
        r'<section[^>]*\bid=bibliography\b[^>]*>',
        html,
    )
    supp_m = re.search(
        r'<section[^>]*\bid=supplementary-materials\b[^>]*>',
        html,
    )
    backnotes_m = re.search(
        r'<section[^>]*\bid=backnotes\b[^>]*>',
        html,
    )

    pieces = []
    # Body zone: from abstract through bodymatter, ending before backnotes
    # / supplementary-materials / bibliography (whichever comes first).
    if abs_m and body_m:
        body_end = len(html)
        for end_m in (backnotes_m, supp_m, bib_m):
            if end_m and end_m.start() > body_m.start():
                body_end = min(body_end, end_m.start())
        pieces.append(html[abs_m.start():body_end])

    # Supplementary zone (file labels only).
    if supp_m:
        sm_end = re.search(r"</section>", html[supp_m.end():])
        if sm_end:
            pieces.append(html[supp_m.start():supp_m.end() + sm_end.end()])

    if not pieces:
        return ""

    body_html = "\n".join(pieces)
    body_html = _strip_decorative_blocks(body_html)
    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse NEJM HTML into a papers/*.json-format dict."""
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
