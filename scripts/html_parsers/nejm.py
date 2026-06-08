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
    """Apply Phase 2 layout rules for nejm.org.

    Step 1: cap body width at 752 px, center, neutralize @media so the
            publisher's narrow CSS branch always applies.
    Step 2: remove the TrustArc cookie banner + overlay.
    Step 3: no JS/CSS sticky elements detected once Step 1 is applied
            (header is a normal flow block under narrow CSS).
    Step 4: no off-main vertical columns remain after Step 1 collapses
            the right-rail into the narrow flow.
    Step 5: remove NEJM ad slots — `<div id=DTM_Position_*>` (topbanner /
            medrectangle reservation), `<div id=ad-*>` literatum ad slots
            (header, right-rail, bottom).
    Step 6: bg verdict clean at vw=1200 — no override needed.
    Step 7: figure images are full-resolution data URLs — no retrieval
            issue.
    Step 8: figure CSS — `<div class=figure-wrap>` ships `float: right`
            with a narrow flex-basis; override to full-width block so
            image+caption fill column with image above caption (figure DOM
            already orders `<img>` then `<figcaption>`).
    Step 9: no in-place expansion needed — the only collapsed candidate is
            `<section id=tab-contributors>`, but its parent
            `<div class=core-collateral>` is a `position:fixed` slide-in
            side panel (`right:0; top:0; max-width:495px;
            transform:translateX(100%)`) that overlays the page rather
            than pushing siblings down. Per Step 9 hard requirement, do
            not replicate overlay expansion. Author affiliations are
            already extracted from the underlying schema.org microdata
            by `_parse_authors`, regardless of panel visibility.
    """
    html = neutralize_media_queries(html)

    # Step 2 — TrustArc cookie consent banner + dark page overlay.
    html = remove_elements_by_id(
        html,
        "trustarc-banner-overlay",
        "truste-consent-track",
    )

    # Step 5 — ad slots (DTM_Position_* and literatum ad-* wrappers).
    # Iterate ad-* slots since they vary per page; loop the helper until
    # no remaining match.
    for eid in (
        "DTM_Position_Topbanner",
        "DTM_Position_MedRectangle",
        "ad-global-banner-FULLx64-1",
        "ad-article-right-rail-300x250-1",
        "ad-article-bottom-FULLx320-1",
    ):
        html = remove_elements_by_id(html, eid)
    # Catch-all for any remaining literatum ad slots whose suffix differs
    # across page templates.
    while True:
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bid=ad-[a-zA-Z0-9_-]+[^>]*>',
        )
        if html == before:
            break

    override = (
        "<style>"
        "html{margin:0!important;padding:0!important;"
        "background:#fff!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "min-width:0!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "background:#fff!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # Step 8 — figures fill column, image above caption with spacing.
        ".figure-wrap{float:none!important;clear:both!important;"
        "width:100%!important;max-width:100%!important;"
        "margin:0 0 16px 0!important;}"
        "figure.graphic,figure.table"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;margin:0 0 16px 0!important;}"
        "figure.graphic>img,figure.table img,"
        "figure.graphic .graphic-wrap img,figure.table .graphic-wrap img"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 12px 0!important;}"
        "figure.graphic>figcaption,figure.table>figcaption"
        "{display:block!important;width:100%!important;}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
    return html
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
