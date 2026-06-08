"""American Society of Hematology (ashpublications) HTML parser."""

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
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "Google Scholar",
    "Crossref",
    "Search ADS",
    "PubMed",
)

# Reference section heading pattern
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# h2 classes for section types in ASH (Silverchair platform)
_BODY_HEADING = "section-title"
_BACK_HEADING = "backsection-title"
_REF_HEADING = "backreferences-title"
_ABSTRACT_HEADING = "abstract-title"
_BACK_OTHER = "backacknowledgements-title"


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for ashpublications.org (Silverchair).

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies.
    Step 2: remove the GdprCookieBanner widget (position: fixed bottom).
            No separate backdrop ships with it.
    Step 3: remove sticky elements detected by scan_sticky.py — the
            focus-only `<a class=skipnav>` (position: fixed top:-100,
            visible only on tab focus; unquoted-class `<a>` so the
            generic by-selector helper does not match it) and the
            entire `widget-ArticleJumpLinks` widget (its inner
            `jumplink-flyout-trigger-wrap` is the position-fixed
            launcher; its sibling `jumplink-list-flyout-container` is
            also position:fixed, opens via JS click on the trigger,
            and at vw=1200 paints its 1152-px-wide jump-link list
            outside the body cap — leaving it behind after stripping
            only the trigger violates Step 1's width envelope). The
            skip-link `<a>` ships with an unquoted `class=skipnav`,
            so it goes through `_remove_nested_element` with an
            explicit anchored pattern.
    Step 5: remove ad blocks. ASH uses the same two Silverchair classes
            as AACR: `ad-banner` (visible shells) and `widget-AdBlock`
            (the inner widgets). Both can appear multiple times.
    Step 8: figure CSS so each image sits above its caption, full width
            of the column.
    Step 9: per-author affiliation popups (`.al-author-info-wrap`) are
            floating tooltips, NOT push-down expansions — leaving them
            collapsed. Affiliations are extracted from publisher metadata
            by `_parse_authors`, so the popup contributes nothing.
    """
    html = neutralize_media_queries(html)

    # Step 2 — GDPR cookie banner.
    html = remove_elements_by_selector(html, "widget-GdprCookieBanner")

    # Step 3 — sticky elements detected by scan_sticky. ASH ships these
    # with unquoted attribute values, so the by-selector helper (which
    # only matches double-quoted class on `<div>`) does not apply.
    # `<a href=#skipNav class=skipnav>` — focus-only skip link.
    html = _remove_nested_element(
        html, r'<a\s[^>]*\bclass=skipnav\b[^>]*>',
    )
    # The position-fixed jumplink-flyout-trigger-wrap is the launcher;
    # the click-revealed jumplink-list-flyout-container is its sibling
    # inside `widget-ArticleJumpLinks`. Strip the whole widget so the
    # orphan list does not paint outside the body cap at wide viewports.
    html = remove_elements_by_selector(html, "widget-ArticleJumpLinks")

    # Step 5 — ad blocks. Loop because the helper removes one element per
    # call and multiple instances exist.
    for cls in ("ad-banner", "widget-AdBlock"):
        while True:
            prev = html
            html = remove_elements_by_selector(html, cls)
            if html == prev:
                break

    override = (
        "<style>"
        "html{margin:0!important;padding:0!important;"
        "background:#fff!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "background:#fff!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # Step 8 — figures. Same Silverchair markup as AACR, with the
        # caption wrapper named `caption fig-caption` (no `-wrap` suffix
        # like AACR uses):
        #   <div class="fig fig-section"> > <div class=fig-label>
        #     <div class=graphic-wrap> > <a><img class=content-image></a>
        #     <div class="caption fig-caption">caption</div>
        ".fig.fig-section,.graphic-wrap"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;"
        "margin-left:0!important;margin-right:0!important;"
        "padding-left:0!important;padding-right:0!important;"
        "box-sizing:border-box!important;}"
        ".graphic-wrap a,.graphic-wrap img.content-image"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 12px 0!important;"
        "box-sizing:border-box!important;}"
        ".fig-caption"
        "{display:block!important;width:100%!important;"
        "margin-left:0!important;margin-right:0!important;}"
        # Table-wrap shells use absolute pixel widths from the publisher
        # PDF that overflow the narrow column. Cap them to the parent.
        ".table-wrap,.table-wrap-inner,.table-wrap table,table.table-wrap"
        "{max-width:100%!important;width:auto!important;"
        "margin-left:0!important;margin-right:0!important;"
        "table-layout:auto!important;}"
        ".table-wrap{overflow-x:auto!important;}"
        # Step 10 — collapse the empty cadmore podcast/video iframe
        # placeholder. SingleFile captures the static DOM only; the
        # iframe is wired to load via JS at view-time and never gets
        # populated in the snapshot, so it sits as a 150-220 px blank
        # reservation between section headings (e.g. y=2033-2253 on
        # the ZNF467 fixture). Hiding via display:none preserves the
        # surrounding `Podcast` label + audio-caption text and the
        # parser's text-extraction path is untouched.
        "div.cmpl_iframe_div,iframe.cmpl_iframe"
        "{display:none!important;}"
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

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    """
    date = get_meta(html, "citation_publication_date") or get_meta(html, "citation_online_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
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

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Uses citation_author + citation_author_institution meta tags.
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

def _extract_ref_field(entry, cls):
    """Extract text of a child div with given class (e.g. article-title, source).

    Stops at the first </div>, which is fine because these fields are not
    nested. Accepts nested tags inside the field and strips them.
    """
    m = re.search(rf'class=["\']?{cls}["\']?[^>]*>(.*?)</div>', entry, re.DOTALL)
    return strip_tags(m.group(1)).strip() if m else ""


def _parse_inline_authors_title(entry):
    """Parse authors and title from inline citation text (older Silverchair format).

    In older ASH papers, references have format:
      "Smith JA, Jones B. Article title here. <div class=source>..."
    i.e. authors and title are plain text before the first structured div
    (source/article-title). Authors end with a period; title follows up to
    the next period before the source div.

    Returns (authors_list, title) where authors are "LastName IN" strings.
    """
    cm = re.search(r'class="citation mixed-citation"[^>]*>(.*?)</div>', entry, re.DOTALL)
    if not cm:
        return [], ""
    cit_html = cm.group(1)

    first_div = re.search(
        r'<div\s+class=["\']?(?:source|article-title|year|volume|fpage)["\']?',
        cit_html,
    )
    pre = cit_html[:first_div.start()] if first_div else cit_html
    pre_text = unescape(re.sub(r'<[^>]+>', ' ', pre)).strip()
    pre_text = re.sub(r'\s+', ' ', pre_text)

    m = re.search(r'(.*?(?:et\s+al\.?|\b[A-Z]{1,5}))\.\s+(.+?)\.?\s*$', pre_text)
    if m:
        author_text = m.group(1).strip().rstrip('.').strip()
        title = m.group(2).strip()
    else:
        author_text = pre_text
        title = ""

    authors = []
    for part in re.split(r',\s*', author_text):
        part = part.strip().rstrip('.').strip()
        if part and part.lower() != "et al":
            authors.append(part)
    return authors, title


def _parse_structured_authors(entry):
    """Parse authors from <div class=surname>/<div class=given-names> pairs.

    Silverchair stores given names as pre-concatenated initials. Hand each
    pair to format_name; helpers handle initial-only vs. full-name input.
    """
    authors = []
    for nm in re.finditer(
        r'class=["\']?surname["\']?[^>]*>([^<]*)</div>'
        r'[^<]{0,6}'
        r'(?:</span>)?[^<]{0,6}'
        r'(?:<div\s+)?class=["\']?given-names["\']?[^>]*>([^<]*)</div>',
        entry,
    ):
        surname = unescape(nm.group(1)).strip()
        given = unescape(nm.group(2)).strip().rstrip('.')
        authors.append(format_name(given, surname))
    return authors


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings.
    ASH-specific: each ref is wrapped in <div data-content-id=b<N>...> or
    <div data-content-id=bib<N>...>; both carry an `xmlns` attribute that
    distinguishes the ref wrapper from surrounding chrome. Newer papers have
    structured surname/given-names/article-title divs; older papers have
    inline author/title text before structured source/year/volume.
    """
    refs = []
    m = re.search(r'class="ref-list[^"]*"', html)
    if not m:
        return refs

    ref_section = html[m.start():]

    items = list(re.finditer(
        r'<div\s+(?:data-)?content-id=[^\s>]+\s+xmlns',
        ref_section,
    ))
    if not items:
        return refs

    last_end = len(ref_section)
    after_last = ref_section[items[-1].end():]
    boundary = re.search(
        r'<div\s+class=["\']?(?:widget|copyright|license|footnote)\b',
        after_last,
    )
    if boundary:
        last_end = items[-1].end() + boundary.start()

    for idx, im in enumerate(items):
        end = items[idx + 1].start() if idx + 1 < len(items) else last_end
        entry = ref_section[im.start():end]

        title = _extract_ref_field(entry, "article-title")
        if not title:
            title = _extract_ref_field(entry, "chapter-title")
        journal = _extract_ref_field(entry, "source").rstrip('.')
        volume = _extract_ref_field(entry, "volume")
        issue = _extract_ref_field(entry, "issue")
        year = _extract_ref_field(entry, "year")
        fpage = _extract_ref_field(entry, "fpage")
        lpage = _extract_ref_field(entry, "lpage")
        if fpage and not lpage:
            fm = re.search(
                rf'class=["\']?fpage["\']?[^>]*>{re.escape(fpage)}</div>\s*[–—-]\s*(\d[\w]*)',
                entry,
            )
            if fm:
                lpage = fm.group(1)
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        authors = _parse_structured_authors(entry)
        if not authors or not title:
            inline_authors, inline_title = _parse_inline_authors_title(entry)
            if not authors:
                authors = inline_authors
            if not title:
                title = inline_title

        doi = ""
        dm = re.search(
            r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry
        )
        if dm:
            doi = format_doi(unescape(dm.group(1)))

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

def _find_h2_sections(html):
    """Find h2 headings, classifying each by class attribute.

    Returns list of (start_pos, heading_text, kind) where kind is
    'abstract', 'body', 'back', 'ref', or 'other'.
    """
    entries = []
    for m in re.finditer(r'<h2[^>]*class="([^"]*)"[^>]*>(.*?)</h2>', html, re.DOTALL):
        cls = m.group(1)
        text = strip_tags(m.group(2)).strip()
        if not text:
            continue
        if _ABSTRACT_HEADING in cls:
            kind = "abstract"
        elif _REF_HEADING in cls:
            kind = "ref"
        elif _BACK_HEADING in cls or _BACK_OTHER in cls:
            kind = "back"
        elif _BODY_HEADING in cls:
            kind = "body"
        else:
            kind = "other"
        entries.append((m.start(), text, kind))
    return entries


def _parse_abstract(html):
    """Extract abstract text from <section class=abstract>."""
    m = re.search(
        r'<section\s+class=(["\']?)abstract\1[^>]*>(.*?)</section>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return ""
    return strip_tags(m.group(2)).strip()


def _parse_body(html):
    """Extract body text between abstract and the first references heading.

    Boundary rules:
      - Start: end of abstract section (or first body/back h2 if no abstract).
      - End: first references h2.
      - After references: keep only supplementary-matched sections.
    """
    body_m = re.search(r'<div\s+class=article-body\b[^>]*>', html)
    if not body_m:
        return ""

    content = html[body_m.end():]
    pos = body_m.end()
    depth = 1
    while depth > 0 and pos < len(html):
        no = re.search(r'<div[\s>]', html[pos:])
        nc = re.search(r'</div>', html[pos:])
        if not nc:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos = pos + no.end()
        else:
            depth -= 1
            if depth == 0:
                content = html[body_m.end():pos + nc.start()]
                break
            pos = pos + nc.end()
    h2s = _find_h2_sections(content)
    if not h2s:
        return ""

    start = 0
    for pos, text, kind in h2s:
        if kind == "abstract":
            abs_end = content.find('</section>', pos)
            if abs_end >= 0:
                start = abs_end + len('</section>')
            else:
                start = pos + 200
            break

    first_ref_idx = None
    for i, (pos, text, kind) in enumerate(h2s):
        if kind == "ref" or _REF_RE.search(text):
            first_ref_idx = i
            break

    parts = []

    first_non_abs_pos = None
    for pos, text, kind in h2s:
        if pos >= start and kind != "abstract":
            first_non_abs_pos = pos
            break
    if first_non_abs_pos is not None and first_non_abs_pos > start:
        parts.append((start, first_non_abs_pos))
    elif first_non_abs_pos is None and first_ref_idx is None:
        parts.append((start, len(content)))

    for i, (pos, text, kind) in enumerate(h2s):
        if pos < start:
            continue
        if kind == "abstract" or kind == "ref" or _REF_RE.search(text):
            continue
        end_pos = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)
        if first_ref_idx is None or i < first_ref_idx:
            parts.append((pos, end_pos))
        else:
            if _SUPP_RE.search(text):
                parts.append((pos, end_pos))

    if not parts:
        return ""

    body_html = ""
    for s, e in parts:
        body_html += content[s:e]

    # Strip duplicate "fig-modal" lightbox copies of every figure — they
    # render the same caption a second time and inflate main_text. The
    # parent <a> opens this hidden modal at runtime; in the static
    # snapshot it sits in the DOM with aria-hidden=true and re-emits
    # fig-label + caption alongside the inline figure. Same Silverchair
    # idiom as aacrjournals.
    while True:
        body_html2 = _remove_nested_element(
            body_html, r'<div[^>]*\bclass="?fig fig-modal\b',
        )
        if body_html2 == body_html:
            break
        body_html = body_html2

    # Heading attributes like data-section-title="... <em>EZH2</em> ..."
    # contain inline emphasis tags whose '>' breaks the h2/h3/h4 regex in
    # tags_to_text. Drop these attributes from h2-h4 tags before the text
    # pipeline. The h-tag's inner text already carries the heading.
    body_html = re.sub(
        r'(<h[2-4]\b[^>]*?)\s+data-section-title="[^"]*(?:<[^>]*>[^"]*)+"',
        r'\1',
        body_html,
    )

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    ASH-specific: main_text is composed of abstract + body with "## Abstract"
    prepended to the abstract section.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")
    body = _parse_body(html)
    if body:
        parts.append(body)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse ASH HTML into a papers/*.json-format dict."""
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
