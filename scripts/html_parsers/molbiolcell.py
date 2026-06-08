"""American Society for Cell Biology (molbiolcell) HTML parser."""

import re
from html import unescape

from ._helpers import (
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    neutralize_media_queries,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Lines starting with any string in this tuple are dropped from main_text
# after the text pipeline runs.
_NOISE = (
    "Crossref",
    "Medline",
    "Google Scholar",
    "Open in a new tab",
    "Search for more papers by this author",
    "Previous Section",
    "Next Section",
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for molbiolcell.org (Atypon Literatum).

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies (the
            wide-viewport CSS adds a right-rail sticky sidebar).
    Step 2: no cookie banner ships in the captured HTML (the publisher
            relies on a runtime JS prompt that doesn't fire under
            SingleFile).
    Step 3: sticky elements — `header.header.fixed.base` (top sticky
            site banner), `.scroll-to-target.fixed-element` (back-to-
            top button), `.sticko__parent.fixed-element` (the right-
            rail tools/issue/metrics column at wide viewports), and
            `.w-slide` / `.w-slide_head` (off-canvas drawer panels
            position:fixed off-screen).
    Step 4: hide the `.col-sm-4` / `.col-md-4` right rail (Support &
            Resources panel + sticky tools sidebar) — the publisher's
            wide CSS lays it out alongside `.col-sm-8 .article__content`
            but it does not belong in the 720-px reading layout.
    Step 5: no ad slots ship in the captured HTML (no `gpt-ad`,
            `ad-banner`, or `widget-AdBlock` markers; the publisher's
            adblocker__* warning panel is hidden by default and only
            renders when ad-block is detected).
    Step 6: page background already white; html/body forced to white
            for symmetry so the bg-around-column scan stays clean.
    Step 8: figures — `<figure class=article__inlineFigure>` carries
            `<img class=figure__image>` with sibling `<figcaption>`.
            Force figure to block, image full column width above
            caption with 12 px gap.
    Step 9: no in-place push-down expansion to perform. The only
            collapsed item, `.author-info.accordion-tabbed__content`,
            is rendered by the publisher as a floating overlay
            (`position:absolute; z-index:10; max-width:22.5rem`,
            solid border framing it as a popover next to the author
            name) — Step 9 forbids replicating overlays as push-down.
            The affiliation text is already harvested directly from
            the HTML source by `_parse_authors`, so visual expansion
            is unnecessary.
    """
    html = neutralize_media_queries(html)

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
        # Atypon ships fixed-pixel `.container` widths from Bootstrap;
        # let them shrink to body.
        ".container,#pb-page-content,main.content"
        "{width:auto!important;max-width:100%!important;"
        "margin-left:auto!important;margin-right:auto!important;}"
        # Step 3 — hide sticky chrome (site header, back-to-top button,
        # sticky right-rail wrapper, off-canvas drawer panels).
        "header.header.fixed,header.header.fixed.base,"
        ".scroll-to-target.fixed-element,"
        ".sticko__parent,.sticko__parent.fixed-element,"
        ".w-slide,.w-slide_head"
        "{display:none!important;}"
        # Step 4 — hide right-rail Support & Resources column.
        "div.col-sm-4,div.col-md-4"
        "{display:none!important;}"
        # Make the article column (col-sm-8 col-md-8) span the body cap.
        "div.col-sm-8.article__content,div.col-md-8.article__content,"
        "div.col-sm-8,div.col-md-8"
        "{width:100%!important;max-width:100%!important;"
        "float:none!important;margin-left:0!important;"
        "margin-right:0!important;}"
        # Step 8 — figures: image above caption, both fill column.
        "figure.article__inlineFigure"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;float:none!important;"
        "margin:0 0 16px 0!important;padding:0!important;"
        "box-sizing:border-box!important;}"
        "figure.article__inlineFigure img.figure__image"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 12px 0!important;"
        "box-sizing:border-box!important;}"
        "figure.article__inlineFigure figcaption"
        "{display:block!important;width:100%!important;"
        "margin:0!important;}"
        # Step 9 — no expansion. The .author-info.accordion-tabbed__content
        # block opens as a floating popover (position:absolute, z-index:10,
        # 360px max-width, bordered card); replicating it as push-down would
        # violate Step 9. Affiliations are already extracted by _parse_authors
        # straight from the HTML source.
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

    molbiolcell exposes minimal Literatum citation_* meta — only
    citation_journal_title is present. Title comes from dc.Title;
    DOI from publication_doi. Volume/issue parsed from the breadcrumb
    /toc/<journal>/<vol>/<issue> link constrained by article__tocHeading
    so the nav-menu /toc/mboc/0/0 ("In Press") link is skipped. Pages
    come from a sibling `<div class=meta>X-Y</div>` next to the
    Vol/No/<date> meta block (the only place a page range appears in
    Atypon's molbiolcell template; citation_firstpage / citation_pages
    are not emitted).

    Year sources, in order of reliability:
      1. dc.Rights "Copyright © YYYY ..." — original publication year.
      2. <span class=epub-section__date>DD Mon YYYY</span>.
      3. dc.Date.
    """
    title = get_meta(html, "dc.Title") or get_meta(html, "citation_title")

    journal = (get_meta(html, "citation_journal_abbrev")
               or get_meta(html, "citation_journal_title") or "")
    journal = journal.rstrip(".")

    year = ""
    rights = get_meta(html, "dc.Rights")
    if rights:
        ym = re.search(r"Copyright\s*\S*\s*(\d{4})", rights)
        if ym:
            year = ym.group(1)
    if not year:
        em = re.search(
            r'class=["\']?epub-section__date["\']?[^>]*>([^<]+)</span>', html,
        )
        if em:
            ym = re.search(r"(\d{4})", em.group(1))
            if ym:
                year = ym.group(1)
    if not year:
        date = get_meta(html, "dc.Date") or get_meta(html, "citation_publication_date")
        if date:
            ym = re.search(r"(\d{4})", date)
            if ym:
                year = ym.group(1)

    doi = format_doi(
        get_meta(html, "publication_doi") or get_meta(html, "citation_doi")
    )

    volume = ""
    issue = ""
    for bm in re.finditer(
        r'href=["\']?https?://[^"\'>\s]*/toc/[^/]+/(\d+)/(\d+)[^>]*'
        r'class=["\']?[^"\'>\s]*article__tocHeading',
        html,
    ):
        if bm.group(1) != "0" and bm.group(2) != "0":
            volume = bm.group(1)
            issue = bm.group(2)
            break

    # Pages live in a sibling <div class=meta>X-Y</div> next to the
    # Vol/No header block. Capture every <div class=meta>...</div> and
    # take the first whose visible text matches a numeric `start-end`
    # range. Other meta divs hold a Vol/No anchor or a publication date
    # (e.g. "December 01, 2000") so the explicit numeric-range filter
    # picks out the right one without misreading the date.
    pages = ""
    for pm in re.finditer(
        r'<div\s+class=["\']?meta["\']?[^>]*>\s*(.*?)\s*</div>',
        html, re.DOTALL,
    ):
        text = re.sub(r'<[^>]*>', '', pm.group(1)).strip()
        rm = re.match(r'^(\d+)\s*[-–—]\s*(\d+)\s*$', text)
        if rm:
            pages = f"{rm.group(1)}-{rm.group(2)}"
            break

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
    Names come from dc.Creator meta tags (ordered, full display "Given
    Last") and go through format_author_name. Affiliations come from
    per-author <div class="author-info accordion-tabbed__content"> blocks
    in the same document order. Some papers omit affiliations entirely
    (empty list).
    """
    names = []
    for m in re.finditer(
        r'<meta[^>]*name=["\']?dc\.Creator["\']?[^>]*content=["\']?([^"\'>]*)',
        html,
    ):
        names.append(unescape(m.group(1)).strip())

    affiliations = []
    info_pat = re.compile(
        r'class=["\']?author-info\s+accordion-tabbed__content["\']?[^>]*>',
    )
    div_open = re.compile(r"<div\b", re.IGNORECASE)
    div_close = re.compile(r"</div\s*>", re.IGNORECASE)
    pos = 0
    while True:
        om = info_pat.search(html, pos)
        if not om:
            break
        start = om.end()
        depth = 1
        p = start
        while depth > 0 and p < len(html):
            nxt_o = div_open.search(html, p)
            nxt_c = div_close.search(html, p)
            if nxt_c is None:
                break
            if nxt_o and nxt_o.start() < nxt_c.start():
                depth += 1
                p = nxt_o.end()
            else:
                depth -= 1
                if depth == 0:
                    chunk = html[start:nxt_c.start()]
                    chunk = re.sub(
                        r'<div\s+class=["\']?bottom-info["\']?.*?</div>',
                        "", chunk, flags=re.DOTALL,
                    )
                    text = strip_tags(chunk)
                    text = re.sub(r"\s+", " ", text).strip()
                    text = re.sub(
                        r"^(?:Corresponding author.*?\.)?\s*", "", text,
                    )
                    # Strip leading "*Address correspondence to: ... )." block
                    # MBC inserts before the affiliation for the corresponding
                    # author.
                    text = re.sub(
                        r"^\*?\s*Address correspondence to:.*?\)\.?\s*",
                        "", text, flags=re.IGNORECASE,
                    )
                    text = text.rstrip(";").rstrip(".").strip()
                    affiliations.append(text)
                    pos = nxt_c.end()
                    break
                p = nxt_c.end()
        else:
            break

    authors = []
    for i, name in enumerate(names):
        aff = affiliations[i] if i < len(affiliations) else ""
        authors.append({
            "author": format_author_name(name),
            "affiliation": [aff] if aff else [],
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Authors are flat "LastName IN" strings (no affiliation).

    molbiolcell references follow the Literatum pattern:
      <li class=references__item>
        <span class=references__note>
          Authors (<span class=references__year>YYYY</span>). Title.
          <span class=references__source><strong>Journal.</strong></span>
          <i>Vol</i>, fpage-lpage. [link panel]
        </span>
      </li>
    Older MBC papers wrap authors in <span class=references__authors> and
    title in <span class=references__article-title>; newer ones (2020+)
    omit those structured spans and require plain-text parsing.
    """
    refs = []
    # MBC embeds the reference list twice (inline body + tabbed
    # "References" panel). Scope to the first <ul class=references>.
    ul_m = re.search(
        r'<ul[^>]*class=["\']?(?:[^"\'>]*\s)?references(?:\s[^"\'>]*)?["\']?[^>]*>',
        html,
    )
    if not ul_m:
        return refs
    ul_start = ul_m.end()
    close_m = re.search(r"</ul>", html[ul_start:])
    ul_end = ul_start + (close_m.start() if close_m else 0)
    list_html = html[ul_start:ul_end]

    li_pat = re.compile(
        r'<li[^>]*class=["\']?references__item["\']?[^>]*>', re.IGNORECASE,
    )
    li_starts = [m.start() for m in li_pat.finditer(list_html)]
    if not li_starts:
        return refs
    li_starts.append(len(list_html))

    for i in range(len(li_starts) - 1):
        entry = list_html[li_starts[i]:li_starts[i + 1]]
        entry = re.sub(
            r'<span\s+class=["\']?label["\']?[^>]*>[^<]*</span>',
            "", entry,
        )

        year = ""
        ym = re.search(
            r'<span\s+class=["\']?references__year["\']?[^>]*>([^<]+)</span>',
            entry,
        )
        if ym:
            ym2 = re.search(r"\d{4}", ym.group(1))
            if ym2:
                year = ym2.group(0)

        journal = ""
        jm = re.search(
            r'<span\s+class=["\']?references__source["\']?[^>]*>'
            r'(?:<strong>)?(.*?)(?:</strong>)?</span>',
            entry, re.DOTALL,
        )
        if jm:
            journal = strip_tags(jm.group(1)).strip().rstrip(".").rstrip(",")

        # DOI from Crossref linkout (dbid=16) preferred over generic link.
        doi = ""
        for lm2 in re.finditer(
            r'href=["\']?(https?://[^"\'>\s]*servlet/linkout[^"\'>\s]*)',
            entry,
        ):
            url = unescape(lm2.group(1))
            if "dbid=16" not in url:
                continue
            km = re.search(r"[?&]key=([^&\s\"'>]+)", url)
            if km:
                candidate = km.group(1).replace("%2F", "/")
                if candidate.startswith("10."):
                    doi = format_doi(candidate)
                    break
        if not doi:
            dm2 = re.search(
                r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry,
            )
            if dm2:
                doi = format_doi(unescape(dm2.group(1)))

        cite = re.sub(
            r'<span\s+class=["\']?references__suffix["\']?.*?</span>',
            "", entry, flags=re.DOTALL,
        )
        cite = re.sub(r"<a\b[^>]*>.*?</a>", "", cite, flags=re.DOTALL)
        plain = strip_tags(cite).strip()
        plain = re.sub(r"\s+", " ", plain)

        # Pull structured author/title spans first (older MBC). Authors
        # are emitted as combined "Surname I.N." strings — pass each
        # through format_author_name to satisfy the author-name contract.
        authors = []
        title = ""
        vol_pages = ""
        sa = re.search(
            r'<span\s+class=["\']?references__authors["\']?[^>]*>(.*?)</span>',
            entry, re.DOTALL,
        )
        st = re.search(
            r'<span\s+class=["\']?references__article-title["\']?[^>]*>'
            r'(.*?)</span>',
            entry, re.DOTALL,
        )
        if sa and st:
            author_text = strip_tags(sa.group(1)).strip().rstrip(",").rstrip(";")
            author_text = re.sub(r"\s+", " ", author_text)
            title = strip_tags(st.group(1)).strip().rstrip(".")
            for tok in re.split(r",\s*", author_text):
                tok = tok.strip().rstrip(".")
                if not tok or tok.lower().startswith("et al"):
                    continue
                authors.append(format_author_name(tok))
            if journal:
                jpos = plain.find(journal)
                if jpos >= 0:
                    vol_pages = plain[jpos + len(journal):].strip()

        if not authors:
            # Newer MBC: parse from plain text. Authors segment ends at
            # "(YYYY).", title follows up to the journal name. Each name
            # token is "Surname I.N." — combine surname + initials and
            # route through format_author_name.
            sm = re.search(r"\((\d{4}[a-z]?)\)\.\s*", plain)
            if sm:
                author_str = plain[:sm.start()].strip().rstrip(",").rstrip(";")
                rest = plain[sm.end():].strip()
                if journal:
                    jpos = rest.find(journal)
                    if jpos > 0:
                        title = rest[:jpos].strip().rstrip(".").strip()
                        vol_pages = rest[jpos + len(journal):].strip()
                    else:
                        title = rest
                else:
                    title = rest
                author_str = re.sub(r"\band\b", ",", author_str)
                tokens = [t.strip() for t in author_str.split(",") if t.strip()]
                initials_re = re.compile(r"^[A-Z]\.?(?:\s*[A-Z]\.?)*\.?$")
                j = 0
                while j < len(tokens):
                    s = tokens[j]
                    if s.lower().startswith("et al"):
                        j += 1
                        continue
                    if j + 1 < len(tokens) and initials_re.match(tokens[j + 1]):
                        combined = f"{s} {tokens[j + 1]}"
                        authors.append(format_author_name(combined))
                        j += 2
                    else:
                        authors.append(format_author_name(s))
                        j += 1
            else:
                title = plain.rstrip(".")

        # Volume/pages from the trailing tail. Format is typically
        # ", 22, 3474-3487" (after the journal slice).
        volume = ""
        issue = ""
        pages = ""
        vp = re.sub(r"\s+", " ", vol_pages).strip()
        vp = vp.lstrip(".,; ").rstrip(".,; ")
        vm = re.match(
            r"(\d+)\s*(?:\(([^)]+)\))?\s*,\s*(\w[\w\-–]*?)\s*$", vp,
        )
        if vm:
            volume = vm.group(1)
            issue = (vm.group(2) or "").strip()
            pages = re.sub(r"[–—]", "-", vm.group(3))

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

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first
        references section.
      - Supplementary: after first references, keep only sections matching
        supplement / extended data / source data / expanded view /
        powerpoint / appendix.
      - Remove all references sections from main_text.

    Pipeline: locate article container -> slice body zones ->
    extract_captions -> strip_common -> tags_to_text -> drop_noise.
    molbiolcell-specific: full-text lives inside <div class=hlFld-Fulltext>;
    abstract sits in a separate <div class=abstractSection>.
    """
    parts = []

    am = re.search(
        r'<div[^>]*class=["\']?[^"\'>]*abstractSection[^"\'>]*["\']?[^>]*>'
        r"(.*?)</div>",
        html, re.DOTALL,
    )
    if am:
        abs_html = am.group(1)
        abs_html = extract_captions(abs_html)
        abs_html = strip_common(abs_html)
        abs_text = tags_to_text(abs_html).strip()
        abs_text = drop_noise(abs_text, _NOISE)
        if abs_text:
            parts.append(f"## Abstract\n\n{abs_text}")

    fm = re.search(
        r'<div[^>]*class=["\']?[^"\'>]*hlFld-Fulltext[^"\'>]*["\']?[^>]*>',
        html,
    )
    if not fm:
        return "\n\n".join(parts).strip()
    body_start = fm.end()
    # Bound at whichever comes first: a REFERENCES h2 heading, or the
    # first <ul class=references> (older MBC papers put the reference
    # list directly after Acknowledgments with no intervening h2).
    candidates = []
    ref_h2 = re.search(
        r'<h2[^>]*>\s*REFERENCES\s*</h2>', html[body_start:], re.IGNORECASE,
    )
    if ref_h2:
        candidates.append(ref_h2.start())
    ref_ul = re.search(
        r'<ul[^>]*class=["\']?(?:[^"\'>]*\s)?references(?:\s[^"\'>]*)?["\']?',
        html[body_start:],
    )
    if ref_ul:
        candidates.append(ref_ul.start())
    body_end = body_start + (min(candidates) if candidates else len(html) - body_start)
    body_html = html[body_start:body_end]

    body_html = re.sub(
        r"<a[^>]*>\s*(?:Previous|Next)\s+section\s*</a>",
        "", body_html, flags=re.IGNORECASE,
    )

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    body_text = tags_to_text(body_html)
    body_text = drop_noise(body_text, _NOISE)
    if body_text.strip():
        parts.append(body_text.strip())
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse molbiolcell HTML into a papers/*.json-format dict."""
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
