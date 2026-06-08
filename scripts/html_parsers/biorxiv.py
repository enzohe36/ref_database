"""bioRxiv (biorxiv.org) HTML parser."""

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
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "Previous Section",
    "Next Section",
    "View this table:",
    "View inline",
    "View popup",
    "Download as PowerPoint",
    "View larger version:",
)

# h2 headings that are reference sections
_REF_RE = re.compile(r"\brefe?rences\b|\bliterature\s+cited\b", re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r"supplement|extended data|source data|expanded view|appendix",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for biorxiv.org (HighWire).

    Step 1: cap body width at 752 px; neutralize @media so the narrow
            (single-column) layout applies at any viewport.
    Step 2: EU cookie compliance bottom popup (`#sliding-popup`).
    Step 3: HighWire docked-nav and full-viewport overlay (sticky on
            scroll). Hidden via CSS so the parser still sees the source.
    Step 4: vertical sidebars — `#col-2`, `#col-3` (HighWire columns
            that flank the article container in two-column layouts).
            After Step 1's media-query collapse most of this folds
            already; explicit hide is belt-and-braces.
    Step 5: ad block — biorxiv ships top banner ad (`#ads3`) and a
            `.no-ad.tower_col_2` placeholder li.
    Step 6: page background — body cascade leaves a grey backdrop
            visible at wide viewports once the cap kicks in. Force
            html + body backgrounds to white.
    Step 8: figures — `.fig.pos-float` wraps an inline image; force
            block + caption-below layout.
    Step 9: no-op. `.affiliation-list.hideaffil` and
            `.cb-section.default-closed.collapsed` are HighWire
            conventions absent from the DOM in our test fixtures
            (CSS rule for `.hideaffil` exists but no element uses
            the class; `.cb-section` does not appear at all). Even
            if present, `.cb-section.default-closed.collapsed` lives
            inside `#col-2`/`#col-3` which Step 4 already hides.
    """
    html = neutralize_media_queries(html)

    # Step 2 — cookie compliance popup
    html = remove_elements_by_id(html, "sliding-popup")

    # Step 5 — ad blocks
    html = remove_elements_by_id(html, "ads3")
    html = _remove_nested_element(
        html,
        r'<li[^>]*\bclass="no-ad tower_col_2"[^>]*>',
    )
    while True:
        prev = html
        html = _remove_nested_element(
            html,
            r'<\w+[^>]*\bclass=("[^"]*\bbanner-ads\b[^"]*"|'
            r"'[^']*\bbanner-ads\b[^']*'|banner-ads\b)[^>]*>",
        )
        if html == prev:
            break

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
        # Override publisher's fixed pixel widths so the floats collapse
        # into the body cap. biorxiv uses Drupal Omega zones + grid-28
        # layout with hard-coded widths (960 / 886 px).
        "#pageid-content,#content-block,"
        "#zone-branding,#zone-content,#zone-menu,#zone-user,"
        "#zone-postscript,#zone-footer,"
        "#region-content,.region-inner,.region-content-inner,"
        ".panel-display,.panel-row-wrapper,"
        ".grid-1,.grid-2,.grid-3,.grid-4,.grid-5,.grid-6,.grid-7,"
        ".grid-8,.grid-9,.grid-10,.grid-11,.grid-12,.grid-13,.grid-14,"
        ".grid-15,.grid-16,.grid-17,.grid-18,.grid-19,.grid-20,.grid-21,"
        ".grid-22,.grid-23,.grid-24,.grid-25,.grid-26,.grid-27,.grid-28,"
        ".grid-29,.grid-30,"
        ".container-12,.container-16,.container-24,.container-30"
        "{width:auto!important;max-width:100%!important;"
        "float:none!important;padding:0!important;margin:0!important;"
        "background:transparent!important;}"
        "body{background:#fff!important;}"
        "#header,#footer{width:auto!important;max-width:100%!important;"
        "background:#fff!important;position:relative!important;}"
        # HighWire's header/footer use fixed-pixel-width inner bars
        # that escape the body cap.
        ".bar,.bar-inner,.footer-group,"
        ".footer-col-left,.footer-col-right"
        "{width:auto!important;max-width:100%!important;"
        "margin-left:0!important;padding-left:0!important;"
        "float:none!important;}"
        ".header-qs,#hdr-login,.inst-branding,#authstring"
        "{position:static!important;left:auto!important;top:auto!important;"
        "width:auto!important;max-width:100%!important;}"
        ".inst-branding,#hdr-login"
        "{height:auto!important;min-height:0!important;"
        "padding-top:0!important;padding-bottom:0!important;}"
        ".inst-branding:empty,#hdr-login:empty"
        "{display:none!important;}"
        # Step 4 — hide left/right sidebars.
        "#col-2,#col-3{display:none!important;}"
        # Step 3 — hide HighWire's docked-nav, full-viewport overlay,
        # and the right-edge collapsible panel (aside.csh_panelc).
        "#bg-hovering-img,div#docked-nav,div#docked-nav3,"
        "aside.csh_panelc"
        "{display:none!important;}"
        # Step 8 — figures: image fills column, image above caption.
        # bioRxiv markup is `.fig.pos-float > .highwire-figure >
        # .fig-inline-img-wrapper > .fig-inline-img > a > img.highwire-
        # fragment.fragment-image`. The publisher's default styling
        # caps the image at 400 px (lazyload thumbnail dimensions),
        # leaving large white reservation below.
        ".fig.pos-float,.highwire-figure,"
        ".fig-inline-img-wrapper,.fig-inline-img,.fig-inline"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;float:none!important;"
        "margin-left:0!important;margin-right:0!important;"
        "padding-left:0!important;padding-right:0!important;"
        "box-sizing:border-box!important;height:auto!important;"
        "min-height:0!important;}"
        ".fig-inline-img a,.fig-inline-img a img,"
        ".fig-inline-img img,"
        "img.highwire-fragment.fragment-image,"
        "img.lazyload,img.lazyloading"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 12px 0!important;"
        "opacity:1!important;"
        "box-sizing:border-box!important;}"
        ".fig-caption"
        "{display:block!important;width:100%!important;"
        "margin:0!important;}"
        ".fig-inline .callout,.highwire-figure .callout"
        "{display:none!important;}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
    return html
def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    bioRxiv preprints lack volume/issue; `citation_firstpage` carries the
    preprint ID (e.g. "2023.04.10.536247"). The citation_journal_title
    value is "bioRxiv" — keep as-is (no trailing period to strip).
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
    """Extract authors with affiliations from citation_* meta tags.

    bioRxiv exposes per-author `citation_author` + consecutive
    `citation_author_institution` tags; `parse_meta_authors` aligns
    them. Names are in "Given Last" form (first-last) — `format_name`
    via `format_author_name` flips to "LastName IN".
    """
    return [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in parse_meta_authors(html)
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages,
    doi, authors}}. HighWire full-text captures place the list inside
    `ol.cit-list` with each `<li>` wrapping a `<div class="cit ref-cit"
    data-doi=...>` that carries `cite` with structured spans:
      - `.cit-auth` > `.cit-name-surname` + `.cit-name-given-names`
      - `.cit-article-title`
      - `abbr.cit-jnl-abbrev`
      - `.cit-vol`, `.cit-issue`, `.cit-fpage`, `.cit-lpage`
      - `.cit-pub-date`

    Abstract-only captures have no `ol.cit-list` so the list is empty
    (consistent with bioRxiv landing pages pre-full-text capture).
    """
    refs = []
    m = re.search(r'class="?cit-list\b', html)
    if not m:
        return refs

    ref_html = html[m.start():]
    # <div class="cit ref-cit ..."> is the stable entry anchor
    ref_starts = [
        rm.start() for rm in re.finditer(r'<div\s+class="?cit\s+ref-cit\b', ref_html)
    ]
    if not ref_starts:
        ol_end = ref_html.find("</ol>")
        if ol_end < 0:
            ol_end = len(ref_html)
        ref_starts = [
            lm.start() for lm in re.finditer(r"<li[^>]*>", ref_html[:ol_end])
        ]

    for i, start in enumerate(ref_starts):
        end = ref_starts[i + 1] if i + 1 < len(ref_starts) else start + 5000
        entry = ref_html[start:end]

        # --- Authors (structured spans) ---
        authors = []
        for am in re.finditer(
            r'<span[^>]*class="?cit-name-surname"?[^>]*>([^<]*)</span>\s*'
            r',?\s*<span[^>]*class="?cit-name-given-names"?[^>]*>([^<]*)</span>',
            entry,
        ):
            surname = unescape(am.group(1)).strip().rstrip(",")
            given = unescape(am.group(2)).strip().rstrip(".")
            authors.append(format_name(given, surname))

        def _cit_field(cls):
            fm = re.search(rf'class="?{cls}"?[^>]*>([^<]*)', entry)
            return unescape(fm.group(1)).strip() if fm else ""

        # --- Title ---
        title = ""
        title_span = re.search(
            r'class="?cit-article-title"?[^>]*>(.*?)</span>',
            entry, re.DOTALL,
        )
        if title_span:
            title = strip_tags(title_span.group(1)).strip()
            title = re.sub(r"\s+", " ", title)

        # --- Journal ---
        journal = _cit_field("cit-jnl-abbrev") or _cit_field("cit-source")
        journal = journal.rstrip(".")

        # --- Year, volume, issue, pages ---
        year = _cit_field("cit-pub-date").rstrip(".")
        volume = _cit_field("cit-vol")
        issue = _cit_field("cit-issue")
        fpage = _cit_field("cit-fpage")
        lpage = _cit_field("cit-lpage")
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # --- DOI ---
        doi = ""
        dm = re.search(r'data-doi=["\']?([^\s"\'>]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1)))

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

def _find_h2_headings(html):
    """Find all h2 headings and their positions."""
    entries = []
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL):
        text = strip_tags(m.group(1)).strip()
        if text:
            entries.append((m.start(), text))
    return entries


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first
        references.
      - Supplementary: after first references, keep only sections
        matching supplement/extended data/source data/appendix.
      - Remove all references sections.

    bioRxiv full-text: article container is `div.fulltext-view`. Start
    at the Abstract h2 (or the `div.section.abstract` block if no h2),
    end before the first references-heading.
    bioRxiv abstract-only: fall back to `div.section.abstract` content.
    """
    # Full-text view takes precedence
    m = re.search(r'<div[^>]*\bclass="[^"]*\bfulltext-view\b[^"]*"[^>]*>', html)
    if not m:
        # Abstract-only fallback
        return _parse_abstract_only(html)

    content = html[m.end():]
    h2s = _find_h2_headings(content)

    # Find start: the Abstract or Summary h2 (usually the first content h2).
    start = 0
    for hpos, text in h2s:
        if text.lower() in ("abstract", "summary"):
            start = hpos
            break
    else:
        abs_div = re.search(r'<div[^>]*\bclass="?section abstract"?', content)
        if abs_div:
            start = abs_div.start()

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text) in enumerate(h2s):
        if _REF_RE.search(text) and pos >= start:
            first_ref_idx = i
            break

    # Body span: start to first-ref (or end of content).
    if first_ref_idx is not None:
        body_end = h2s[first_ref_idx][0]
    else:
        body_end = len(content)

    body_html = content[start:body_end]

    # Supplementary zone: after first references heading.
    supp_html = ""
    if first_ref_idx is not None:
        tail = content[h2s[first_ref_idx][0]:]
        supp_h2s = _find_h2_headings(tail)
        # Skip the references heading itself; keep subsequent supp headings.
        for i, (pos, text) in enumerate(supp_h2s[1:], start=1):
            if _SUPP_RE.search(text):
                nxt = supp_h2s[i + 1][0] if i + 1 < len(supp_h2s) else len(tail)
                supp_html += tail[pos:nxt]

    combined = body_html + supp_html
    combined = extract_captions(combined)
    combined = strip_common(combined)
    text = tags_to_text(combined)
    return drop_noise(text, _NOISE).strip()


def _parse_abstract_only(html):
    """Extract just the abstract for capture variants without full-text.

    Targets `<div class="section abstract" id=abstract-N>` with nested
    heading + paragraphs.
    """
    m = re.search(
        r'<div\s+class="?section abstract"?\s+id=abstract-\d+[^>]*>',
        html,
    )
    if not m:
        return ""

    pos = m.end()
    depth = 1
    end = len(html)
    while depth > 0:
        no = re.search(r"<div[\s>]", html[pos:])
        nc = re.search(r"</div>", html[pos:])
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
    block = html[m.end():end]
    block = strip_common(block)
    text = tags_to_text(block)
    return drop_noise(text, _NOISE).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse a bioRxiv HTML page into a papers/*.json-format dict."""
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
