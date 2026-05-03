"""IUCr (journals.iucr.org) HTML parser.

IUCr paper landing pages carry metadata, abstract, keywords, and
structured references via citation_* meta tags, plus the full article
body inside <div id=body>, the back matter inside <div id=bm>
(Acknowledgements / Conflict of interest / Funding), and the reference
list inside <div id=bibl>.
"""

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
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
    remove_elements_by_selector,
)

# Publisher-specific noise strings removed from main_text
_NOISE = ()


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize IUCr HTML to a single centered text column.

    IUCr's article pages are built around <div id=iucr-art> (the reading
    column: mainheading + title + authors + affiliations + abstract +
    body + references). The left sidebar (Issue contents / Download PDF /
    3D view / Previous article thumbnails) is <div id=art_leftbox> and
    <div id=art_leftbox_narrow>, both positioned fixed. Site chrome
    (header + footer + cookie bar) plus TrendMD's "We recommend" widget
    tail the article in sibling DOM.

    Chrome stripped (Step 3):
      - <div id=header> masthead (journal logo + nav + search).
      - <div id=footer> + <div id=footersearch> bottom chrome.
      - <div id=art_leftbox> / <div id=art_leftbox_narrow> sidebars.
      - <div id=pl> / <div id=pr> hidden popup-left / popup-right
        panels (fixed-position, nothing readable inside).
      - <div id=trendmd-suggestions> "We recommend" TrendMD carousel.
      - Bottom-fixed "We use cookies" banner — matched by its inline
        z-index:99999 style (the only fixed-position .popup on the page).

    Reading column (Step 4): <div id=iucr-art> (full-text articles).
    IUCr also publishes issue-landing pages (<div class="articles"
    id=articlelpNNNN>) and abstract-only minimum-view pages
    (<div class="articles" id=articlemvNNNN>); the cap is applied to
    all three via a `[id^=articlelp], [id^=articlemv]` fallback.
    """
    # Step 3 — strip chrome.
    html = remove_elements_by_id(
        html, "header", "footer", "footersearch",
        "art_leftbox", "art_leftbox_narrow",
        "pl", "pr",
        "trendmd-suggestions",
        "journalsocialmedia",
    )
    # Cookie banner: bottom-fixed <div class=popup style="...z-index:99999...">.
    # Iterate — there is only one, but the helper removes at most one
    # per call so a loop is safe if the layout changes.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass=popup\b[^>]*style=[^>]*z-index:99999[^>]*>',
        )
        if html == before:
            break

    # Steps 2 + 4 — layout freeze and reading-column cap.
    # The marker comment makes this injection idempotent — re-running
    # remove_banners on already-formatted HTML strips the previous
    # block before injecting the new one (otherwise convert_html
    # accumulates one duplicate style block per run on the same file).
    _INJECT_MARKER = "<!--iucr-format-html-->"
    html = re.sub(
        re.escape(_INJECT_MARKER) + r"<style>.*?</style>",
        "", html, flags=re.DOTALL,
    )
    override = (
        _INJECT_MARKER
        + "<style>"
        "html{overflow-y:overlay}"
        "html::-webkit-scrollbar{width:0}"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Collapse every wrapper between body and #iucr-art so the cap
        # on #iucr-art is not shrunk by an inherited fixed width.
        # Also zero the box-shadow/border that `#main` picks up at
        # wider viewports (the natural stylesheet draws a 3px/10px
        # drop-shadow around the article body at vw > 720 px).
        "#jpage_d,#article,#pagebody,.layout_cjo_singlecolumn,"
        "#main.article{"
        "display:block !important;width:100% !important;"
        "max-width:100% !important;min-width:0 !important;"
        "margin:0 !important;padding:0 !important;"
        "background:#fff !important;float:none !important;"
        "box-shadow:none !important;border:none !important}"
        # Capped reading column.
        # Note: the natural stylesheet gives #iucr-art a 5em colored
        # left border and `float:left; width:calc(100% - 6.5em)`. Zero
        # the border and override the float+width to reclaim the full
        # column width; `border:none` is mandatory (padding:56px 16px
        # alone leaves the 80-px border in place and shifts the text).
        "#iucr-art,[id^=articlelp],[id^=articlemv]{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;"
        "padding:56px 16px !important;"
        "border:none !important;"
        "box-sizing:border-box !important;"
        "background:#fff !important}"
        "#iucr-art *,[id^=articlelp] *,[id^=articlemv] *{"
        "max-width:100% !important;min-width:0 !important}"
        # Table intrinsic width beats max-width; force fixed layout.
        "#iucr-art table,[id^=articlelp] table,[id^=articlemv] table{"
        "table-layout:fixed !important;width:100% !important;"
        "word-break:break-word !important}"
        # Exempt the journal-logo layout table (<table class=layout>)
        # inside `.jicon_d`: it's a 2-cell icon+title grid whose
        # natural width is ~124 px. Forcing it to width:100% expands
        # `.jh_left` to the full column and bumps the float:right
        # `.jh_right` (Volume|Part|Date|Pages + DOI + Open access +
        # Cited by) below it, destroying the 2-column metadata row.
        "#iucr-art .jicon_d table.layout,"
        "[id^=articlelp] .jicon_d table.layout,"
        "[id^=articlemv] .jicon_d table.layout{"
        "table-layout:auto !important;width:auto !important}"
        # Figure caption tables (<table class=fig>): native layout puts
        # the thumbnail in a left td (20% width) and the caption in the
        # right td (80%). At the narrow reading-column width this forces
        # the caption to start at ~140 px from the column edge and wrap
        # in a narrow box. Block-stack so the image sits above the
        # caption.
        "#iucr-art table.fig,#iucr-art table.fig tbody,"
        "#iucr-art table.fig tr,#iucr-art table.fig td,"
        "[id^=articlelp] table.fig,[id^=articlelp] table.fig tbody,"
        "[id^=articlelp] table.fig tr,[id^=articlelp] table.fig td,"
        "[id^=articlemv] table.fig,[id^=articlemv] table.fig tbody,"
        "[id^=articlemv] table.fig tr,[id^=articlemv] table.fig td{"
        "display:block !important;width:100% !important;"
        "text-align:left !important;"
        "box-sizing:border-box !important}"
        # The publisher's `table.fig{padding-bottom:5px}` is a native
        # 5-px buffer that visually sits *outside* the table-cell row
        # baseline in the native side-by-side layout. When the cells are
        # block-stacked it appears below the caption — duplicating the
        # caption-text-bot/cell-inner-bot collapse that's already 0 in
        # raw. Move it to act as the gap *above* the caption (i.e.
        # below the image) instead, by zeroing the table padding-bottom
        # and adding the same 5 px as image margin-bottom. This keeps
        # the publisher's native vertical buffer (same width as the
        # padding above the image inside the image cell — i.e. the
        # space natively between fig content and caption-row baseline)
        # without hardcoding a value the publisher didn't define.
        "#iucr-art table.fig,"
        "[id^=articlelp] table.fig,"
        "[id^=articlemv] table.fig{"
        "padding-bottom:0 !important}"
        "#iucr-art table.fig img.figlnkthm,"
        "[id^=articlelp] table.fig img.figlnkthm,"
        "[id^=articlemv] table.fig img.figlnkthm{"
        "margin-bottom:5px !important}"
        # Post-capture (get_refs.py) swaps the 100-px thumbnail src for
        # the figure page's full image (usually 640 px wide — iucr's
        # only public variant). Natural 640 px is narrower than the
        # caption column (688 px at vw=720), so figures look smaller
        # than their captions. Force img.figlnkthm to fill the column
        # width; height auto-scales to preserve aspect ratio.
        ":root #iucr-art img.figlnkthm,"
        ":root [id^=articlelp] img.figlnkthm,"
        ":root [id^=articlemv] img.figlnkthm{"
        "width:100% !important;height:auto !important;"
        "max-width:100% !important}"
        # A stray <hr> sits just below the closing of the outer
        # <div id=article> wrapper — hide it so the document height
        # reflects the reading-column bottom, not the horizontal-rule.
        "body > hr,#article > hr,#jpage_d > hr{display:none !important}"
        # First-/last-child margin-zero, but only on DIRECT children of
        # the wrapper — the descendant form `*:first-child` kills every
        # section h3's native 2em margin-top (each h3 is the first
        # child of its DIVSECn parent), collapsing the visual break
        # between sections.
        ":root #iucr-art > *:first-child,"
        ":root [id^=articlelp] > *:first-child,"
        ":root [id^=articlemv] > *:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        # `.mainheading` sits inside #iucr-art's #fm and is the first
        # rendered element; native stylesheet adds margin-top:1-2em
        # depending on viewport. Zero it so the wrapper's 56-px top
        # padding is the only spacing above the banner.
        ":root #iucr-art .mainheading{"
        "margin-top:0 !important;padding-top:0 !important}"
        # Restore breathing space above the author block: native IUCr
        # renders a 15 px visual gap between <div id=atl> (title) and
        # <div id=aug> (authors), created by a <div style=float:right>
        # CrossMark badge between them. The reading-column width cap
        # collapses that interaction, so authors render flush against
        # the title bottom. Explicit margin-top matches the native gap.
        ":root #iucr-art #aug,"
        ":root [id^=articlelp] #aug,"
        ":root [id^=articlemv] #aug{margin-top:16px !important}"
        # Direct-child only. The descendant form zeros margin/padding on
        # every last-child descendant — including the <p> inside
        # `.oainfo` (license box), which collapses the 10.8-px gap
        # between the license text and the bottom edge of the gray
        # box. `.jinfo_header_article` has its own rule below.
        ":root #iucr-art > *:last-child,"
        ":root [id^=articlelp] > *:last-child,"
        ":root [id^=articlemv] > *:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # Extra: the bottom `<div id=bm>` ends with a .jinfo_header_article
        # block whose natural stylesheet ships margin-bottom:3em (~36 px
        # at the parent's 12-px font-size). Zero it so the bm's rendered
        # bottom sits flush with its last child and the 56 px wrapper
        # padding is the sole bottom gap.
        ":root #iucr-art .jinfo_header_article,"
        ":root [id^=articlelp] .jinfo_header_article,"
        ":root [id^=articlemv] .jinfo_header_article{margin-bottom:0 !important}"
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

def _longest(values):
    """Return the longest string from values (descriptive variant)."""
    return max(values, key=len) if values else ""


def _parse_metadata(html):
    """Extract metadata from citation_* meta tags.

    IUCr emits multiple citation_journal_abbrev variants (progressively more
    specific); pick the longest for the fullest abbreviation.
    """
    date = get_meta(html, "citation_date") or get_meta(
        html, "citation_online_date"
    )
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = _longest(get_all_meta(html, "citation_journal_abbrev"))
    if not journal:
        journal = get_meta(html, "citation_journal_title")
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

    IUCr pairs each citation_author with a single citation_author_institution
    that follows it in document order (and optional citation_author_email).
    Names are in "Last, I.N." format; convert to "Last IN".
    """
    authors = []
    current = None
    # Longer alternation first so citation_author_institution / _email are
    # matched as themselves, not as "citation_author" + trailing junk.
    for m in re.finditer(
        r'<meta[^>]*name=["\']?(citation_author_institution|citation_author_email|citation_author)\b["\']?[^>]*content=("[^"]*"|\'[^\']*\'|[^\s>]+)',
        html,
    ):
        name_attr = m.group(1)
        raw = m.group(2)
        if raw.startswith('"') or raw.startswith("'"):
            value = raw[1:-1]
        else:
            value = raw
        value = unescape(value).strip()
        if name_attr == "citation_author":
            if current is not None:
                authors.append(current)
            current = {"name": value, "affiliations": []}
        elif name_attr == "citation_author_institution" and current is not None:
            current["affiliations"].append(value.strip(", "))
    if current is not None:
        authors.append(current)

    return [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a["affiliations"],
        }
        for a in authors
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _format_iucr_ref_author(name):
    """Convert IUCr 'Last F. M.' to 'Last FM' via shared helpers."""
    return format_author_name(name)


def _parse_reference_string(value):
    """Parse a single citation_reference string into a structured dict.

    IUCr emits two formats:
    - Structured: 'citation_author=X; citation_author=Y; citation_year=YYYY;
                   citation_journal_title=ABBR; citation_volume=V;
                   citation_firstpage=A; citation_lastpage=B;'
    - Freeform (online-ahead-of-print refs with no volume/page yet, or
      older legacy entries): 'Last, I. J., Last, I. J. & Last, I. J.
      (YYYY). Journal Abbr. Volume, Pages. https://doi.org/...'
    The structured form is detected by the 'citation_*=' prefix on the
    first non-empty field; anything else is routed through the freeform
    parser so refs aren't silently dropped.
    """
    stripped = value.strip()
    if not stripped.startswith("citation_"):
        return _parse_freeform_reference(stripped)

    fields = [f.strip() for f in value.split(";") if f.strip()]
    authors = []
    data = {}
    for f in fields:
        if "=" not in f:
            continue
        k, v = f.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k == "citation_author":
            authors.append(_format_iucr_ref_author(v))
        else:
            data[k] = v

    # IUCr appends sentence-ending periods to some citation_firstpage /
    # citation_lastpage values (e.g. "e4519.", "D531."). Strip them so
    # pages is a clean numeric/elocation token.
    firstpage = data.get("citation_firstpage", "").rstrip(".")
    lastpage = data.get("citation_lastpage", "").rstrip(".")
    pages = f"{firstpage}-{lastpage}" if firstpage and lastpage else firstpage

    journal = data.get("citation_journal_title", "").rstrip(".")
    return {
        "title": data.get("citation_title", ""),
        "journal": journal,
        "year": data.get("citation_year", ""),
        "volume": data.get("citation_volume", ""),
        "issue": data.get("citation_issue", ""),
        "pages": pages,
        "doi": format_doi(data.get("citation_doi", "")),
        "authors": authors,
    }


def _parse_freeform_reference(text):
    """Parse a freeform IUCr reference string.

    Expected shape:
        'Last, F. M., Last, F. M. & Last, F. M. (YYYY). Journal Abbr.
         Volume, Pages. https://doi.org/...'
    Missing tail segments (no volume, no DOI) are tolerated.
    """
    empty = {
        "title": "", "journal": "", "year": "", "volume": "",
        "issue": "", "pages": "", "doi": "", "authors": [],
    }
    if not text:
        return empty

    # Extract DOI first (anywhere in the string).
    doi = ""
    dm = re.search(r"https?://(?:dx\.)?doi\.org/\S+", text)
    if dm:
        raw = dm.group(0).rstrip(".,")
        doi = format_doi(raw.replace("dx.doi.org", "doi.org"))
        text = text[:dm.start()].rstrip() + text[dm.end():]
    text = text.rstrip(" .,")

    # Locate "(YYYY)" — author block ends here.
    ym = re.search(r"\((\d{4})\)\s*\.?", text)
    if not ym:
        return empty
    year = ym.group(1)
    author_block = text[:ym.start()].rstrip(" ,.")
    rest = text[ym.end():].lstrip(" .,").rstrip(" .,")

    # Authors: "Last, F. M., Last, F. M. & Last, F. M."
    # Split on commas then rejoin Last+initials pairs. Each author is
    # typically "Last, F. M." — the surname comes before the first comma
    # and the initials come after it and before the next author's
    # surname. The pattern "Surname, I[. J. K.]" has at most one comma;
    # commas separating authors follow the initials.
    authors = []
    pieces = re.split(r"\s*&\s*|\s+and\s+", author_block)
    for piece in pieces:
        piece = piece.strip().rstrip(",")
        if not piece:
            continue
        # A piece may contain multiple comma-separated authors.
        tokens = [t.strip() for t in piece.split(",") if t.strip()]
        i = 0
        while i < len(tokens):
            surname = tokens[i]
            # Next token is initials if it looks like "F.", "F. M.", "F.-B."
            if i + 1 < len(tokens) and _looks_like_initials(tokens[i + 1]):
                authors.append(format_author_name(f"{surname}, {tokens[i + 1]}"))
                i += 2
            else:
                authors.append(format_author_name(surname))
                i += 1

    # Journal / volume / pages live after the year.
    # IUCr freeform refs use the shape "Journal Abbr. Volume, Pages" —
    # the first space-separated chunk that looks like a bare number is
    # the volume boundary, not the comma (the abbrev ends with a period
    # followed by the volume: "SLAS Discov. 29, 100145").
    journal = ""
    volume = ""
    pages = ""
    if rest:
        segments = [s.strip() for s in re.split(r",\s*", rest) if s.strip()]
        first = segments[0] if segments else ""
        # Strip a trailing "<space>NNN" number from the journal into volume.
        vol_m = re.search(r"\s+(\w+)$", first)
        if vol_m and re.match(r"^[0-9IVXLCDM]+$", vol_m.group(1)):
            journal = first[:vol_m.start()].rstrip(". ").rstrip(".")
            volume = vol_m.group(1)
        else:
            journal = first.rstrip(". ").rstrip(".")
        if not volume and len(segments) >= 2:
            volume = segments[1].strip().rstrip(".")
            if len(segments) >= 3:
                pages = segments[2].replace("\u2013", "-").rstrip(".")
        elif volume and len(segments) >= 2:
            pages = segments[1].replace("\u2013", "-").rstrip(".")

    return {
        "title": "",
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": "",
        "pages": pages,
        "doi": doi,
        "authors": authors,
    }


def _looks_like_initials(tok):
    """True if token is a short dotted-initial run like 'F.', 'F. M.', 'J.-B.'."""
    stripped = re.sub(r"[.\-\s\u2010\u2011\u2012\u2013]", "", tok)
    return bool(stripped) and stripped.isalpha() and stripped.isupper() and len(stripped) <= 4


def _parse_references(html):
    """Extract references from citation_reference meta tags.

    The <meta name=citation_reference> tags carry the structured fields
    but omit DOIs for most entries (only freeform refs include the DOI
    URL in their text). The visible <div id=bibl> anchors are in the
    same order as the meta tags and carry DOI links as explicit
    <a class=biblink href=https://doi.org/...> anchors, which we lift
    into the corresponding meta-parsed ref.
    """
    refs = []
    for value in get_all_meta(html, "citation_reference"):
        refs.append({"": _parse_reference_string(value)})
    bibl_dois = _parse_bibl_dois(html)
    for i, doi in enumerate(bibl_dois):
        if i >= len(refs) or not doi:
            continue
        if not refs[i][""]["doi"]:
            refs[i][""]["doi"] = doi
    return refs


def _parse_bibl_dois(html):
    """Return per-BB-anchor DOI list from <div id=bibl>.

    Output is positional — the Nth entry corresponds to the Nth
    citation_reference meta tag (both are sorted alphabetically by
    first-author surname). Missing DOIs are returned as empty strings
    so positional alignment with the meta list is preserved.
    """
    bibl = _slice_div(html, "bibl")
    if not bibl:
        return []
    anchors = list(re.finditer(r"<a\s+class=bbanchor\s+id=BB\d+></a>", bibl))
    dois = []
    for i, am in enumerate(anchors):
        start = am.end()
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(bibl)
        entry = bibl[start:end]
        dm = re.search(
            r'href=["\']?(https?://(?:dx\.)?doi\.org/[^"\'\s>]+)',
            entry,
        )
        if dm:
            import urllib.parse
            raw = unescape(dm.group(1))
            # bibl links URL-encode the DOI slash ("%2F"); unquote so the
            # stored DOI matches the canonical https://doi.org/10.xxx/yyy form.
            raw = urllib.parse.unquote(raw)
            dois.append(format_doi(raw.replace("dx.doi.org", "doi.org")))
        else:
            dois.append("")
    return dois


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Get abstract text, preferring meta tag over body markup."""
    text = get_meta(html, "citation_abstract")
    if text:
        return text.strip()
    m = _slice_div(html, "abs")
    if m:
        return tags_to_text(m).strip()
    return ""


def _parse_keywords(html):
    """Return keyword list.

    Prefers anchor text inside <div id=kwdg> because the display-case
    keywords there ("SARS-CoV-2", "NSP13 helicase") are what readers see;
    the citation_keywords meta tag uses uppercase ("SARS-COV-2; NSP13
    HELICASE"). Falls back to the meta tag when the kwdg block is absent.
    """
    kw_block = _slice_div(html, "kwdg")
    if kw_block:
        kws = []
        for m in re.finditer(r"<a[^>]*>([^<]+)</a>", kw_block):
            kw = unescape(m.group(1)).strip().rstrip(".,;")
            if kw:
                kws.append(kw)
        if kws:
            return kws
    raw = get_meta(html, "citation_keywords")
    if not raw:
        return []
    return [kw.strip() for kw in raw.split(";") if kw.strip()]


def _slice_div(html, div_id):
    """Return inner HTML of <div id=div_id> using depth-tracked <div> matching.

    Returns empty string when the container is not found.
    """
    m = re.search(rf'<div[^>]*\bid=["\']?{re.escape(div_id)}["\']?[^>]*>', html)
    if not m:
        return ""
    pos = m.end()
    depth = 1
    while depth > 0 and pos < len(html):
        no = re.search(r"<div[\s>]", html[pos:])
        nc = re.search(r"</div>", html[pos:])
        if nc is None:
            return ""
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                return html[m.end():pos + nc.start()]
            pos += nc.end()
    return ""


def _parse_main_text(html):
    """Build main_text from abstract + keywords + body + back matter.

    Body sections live in <div id=body>. Back matter lives as individual
    siblings <div id=ack>, <div id=coi>, <div id=funding> inside
    <div id=bm>; the outer #bm wrapper also holds references (#bibl),
    journal info, and the TrendMD "We recommend" recommender, so only
    the three named back-matter divs are extracted.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append("## Abstract\n" + abstract)
    keywords = _parse_keywords(html)
    if keywords:
        parts.append("## Keywords\n" + "; ".join(keywords))

    for section_id in ("editdetails", "body", "ack", "coi", "funding"):
        section_html = _slice_div(html, section_id)
        if not section_html:
            continue
        section_html = extract_captions(section_html)
        section_html = strip_common(section_html)
        text = tags_to_text(section_html)
        if text.strip():
            parts.append(text)

    return drop_noise("\n\n".join(parts), _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse IUCr HTML into a papers/*.json-format dict."""
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
