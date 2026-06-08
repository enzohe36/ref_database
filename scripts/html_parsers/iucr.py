"""IUCr (journals.iucr.org) HTML parser."""

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
    strip_common,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = ()


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for journals.iucr.org.

    Step 1: cap body width to 752 px and center; neutralize @media so the
            narrow-form CSS branch (max-width: 1200/800/600/...) applies
            unconditionally. IUCr ships only max-width queries, so the
            narrow rules already apply at vw <= 1200; the call still
            normalises any min-width-only blocks that future templates
            might introduce.
    Step 2: remove the cookie consent banner — `<div class=popup
            style=...position:fixed...>` placed as the last child of
            <body>, identified by inline `position:fixed` + the
            "We use cookies" copy. No separate backdrop.
    Step 3: remove the page-wide fixed `<div id=header>` (full-width site
            header) and the article-tools fixed left-rail
            `<div id=art_leftbox>`. Both are flagged by scan_sticky.
            (`#art_leftbox_narrow`, the horizontal version that activates
            at narrower vw, is left as publisher-native chrome — it is
            not flagged sticky and is part of the article column.)
    Step 4: no extra vertical columns — IUCr uses a single-column
            layout (`div.layout_cjo_singlecolumn`). No-op.
    Step 5: no ad slots in the captured HTML.
    Step 6: page background is white; main column has no shadow. No-op.
    Step 8: figures — `<div class=im>` wrappers hold the image (an
            `<a>` link wrapping `<img>`), and `<div class=imt>` holds
            the caption. Both already render in document order
            (image above caption). Force the image to span the column
            width with a small bottom margin so the caption sits
            visibly below.
    Step 9: no collapsed regions to expand — author affiliations are
            rendered always-visible inside `<div id=aff>` (and also
            extracted from `citation_author_institution` meta tags by
            `_parse_authors`). No-op.
    """
    html = neutralize_media_queries(html)

    # Step 2 — cookie consent banner (last <body> child, inline
    # `position:fixed`, "We use cookies" copy). Match by the inline
    # `position:fixed` style on a `class=popup` div pinned to bottom.
    html = _remove_nested_element(
        html,
        r'<div\s+class=popup\s+style="bottom:0px;left:0px;[^"]*position:fixed[^"]*">',
    )

    # Step 3 — sticky elements flagged by scan_sticky.
    # `#header` is the publisher's page-wide top banner (fixed). Removing
    # it lets the article column flow from the top of the viewport.
    # `#art_leftbox` is the article-tools left-rail (Issue contents /
    # PDF / Navigation / Highlighting / Citation / Stats / Previous /
    # Next), pinned to left:2em via inline `position:fixed`.
    html = remove_elements_by_id(html, "header", "art_leftbox")

    override = (
        "<style>"
        # Step 1 — lock body to 752 px wide, centered. IUCr's body is
        # otherwise full-bleed; `#jpage_d`, `#article`, `#main` and
        # `#pagebody` have no fixed widths but inherit body's width.
        "html{margin:0!important;padding:0!important;"
        "background:#fff!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "min-width:0!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "background:#fff!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # IUCr wraps the article in #jpage_<journal> > #article > #pagebody
        # > .layout_cjo_singlecolumn > #main > #iucr-art. Keep them all
        # auto-width and override the publisher's `padding-top:120px` on
        # #pagebody (which compensated for the now-removed fixed `#header`).
        # `#iucr-art` ships a 60-px-wide off-white `border-left` that
        # decorates the publisher's article-tools rail; with the rail
        # gone, zero the border so the column sits flush inside body.
        "div#jpage_a,div#jpage_b,div#jpage_c,div#jpage_d,div#jpage_e,"
        "div#jpage_f,div#jpage_j,div#jpage_m,div#jpage_q,div#jpage_s,"
        "div#jpage_x,div#article,div#pagebody,div.layout_cjo_singlecolumn,"
        "div#main,div#main.article,div#iucr-art"
        "{width:auto!important;min-width:0!important;"
        "max-width:100%!important;"
        "margin-left:auto!important;margin-right:auto!important;"
        "padding-left:0!important;padding-right:0!important;"
        "box-sizing:border-box!important;}"
        "div#pagebody{padding-top:0!important;margin-top:0!important;}"
        "div#iucr-art{border-left:0!important;border-right:0!important;}"
        # Step 8 — figure layout. IUCr wraps each figure in a
        # `<table class=fig>` with two cells per row: the image cell
        # (`td.td_align_center.width_20`) holds the figure thumbnail
        # `<img class=figlnkthm>` (HTML width=3000 attribute) inside an
        # `<a>` link, and a side-caption cell holds `<span class="font_size_2 caption">`.
        # Force the table to render as a column-spanning block, the image
        # to scale down to column width above its caption, and the side
        # caption cell to flow below the image instead of beside it.
        "table.fig{display:block!important;width:100%!important;"
        "max-width:100%!important;margin:0 0 16px 0!important;"
        "padding:0!important;table-layout:auto!important;}"
        "table.fig tbody,table.fig tr"
        "{display:block!important;width:100%!important;max-width:100%!important;}"
        "table.fig td,table.fig td.td_align_center,"
        "table.fig td.width_20"
        "{display:block!important;width:100%!important;max-width:100%!important;"
        "padding:0!important;}"
        "table.fig img,img.figlnkthm"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 12px 0!important;padding:0!important;"
        "box-sizing:border-box!important;}"
        "table.fig a{display:block!important;width:100%!important;"
        "max-width:100%!important;margin:0!important;padding:0!important;}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
    return html
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
