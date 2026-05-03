"""American Association for Cancer Research (aacrjournals) HTML parser."""

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

# h2 classes for section types in AACR (Silverchair platform)
_BODY_HEADING = "section-title"
_BACK_HEADING = "backsection-title"
_REF_HEADING = "backreferences-title"
_ABSTRACT_HEADING = "abstract-title"
_BACK_OTHER = "backacknowledgements-title"


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize AACR (Silverchair) HTML to a single centered text column.

    Chrome stripped (Step 3):
      - GDPR cookie consent banner (`.gdpr-cookie-wrapper`).
      - Site masthead and footer (Silverchair uses
        `<section class="master-header...">` and `<section class="footer_wrap...">`,
        not <header>/<footer>).
      - Left `#InfoColumn` and right `#Sidebar` page-grid columns.
      - In-article mobile nav button (`.article-browse-top.article-browse-mobile-nav`)
        which sits above the publication-type/date line at the start of the
        reading column.
      - Trailing `widget-ArticleLinks`/`toolbar-wrap`/comments/metrics
        widgets that follow the copyright line at the end of the reading
        column. The `.pub-history-wrap` citation block (journal-name +
        volume/issue/pages + DOI link + "Article history" dropdown) is
        kept — it is non-redundant article metadata and serves as the
        canonical citation line at the top of the reading column.

    Reading column wrapper: `.widget-ArticleMainView`. Cap it at 752 px with
    56 px top/bottom + 16 px side padding. The "Author & Article
    Information" accordion (`.js-metadata-wrap.metadata`) is force-
    expanded — it contains the corresponding-author block, funding
    awards, and the publication-history dates that aren't visible
    anywhere else without JS.
    """
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    html = remove_elements_by_selector(html, "gdpr-cookie-wrapper")
    # Silverchair uses <section> (not <header>/<footer>) for chrome.
    html = _remove_nested_element(
        html, r'<section\b[^>]*class="[^"]*master-header\b[^"]*"[^>]*>',
    )
    html = _remove_nested_element(
        html, r'<section\b[^>]*class="[^"]*footer_wrap\b[^"]*"[^>]*>',
    )
    html = remove_elements_by_id(html, "InfoColumn", "Sidebar")
    # Mobile nav button above the article-info row.
    html = remove_elements_by_selector(html, "article-browse-mobile-nav")
    # Note: keep `.toolbar-wrap` — it holds the article-header tools
    # row (Split-Screen, Views, PDF, Share, Tools, Cite, Versions)
    # which renders inline with the metadata block at narrow vw.
    # Trailing widgets: ArticleLinks (empty), then a chain of dynamic
    # widget rails (Cited By, Metrics, comments, Related). Strip the whole
    # trailing chain by id where stable, and by class for the rest.
    html = _remove_nested_element(
        html, r'<div\b[^>]*class="[^"]*widget-ArticleLinks\b[^"]*"[^>]*>',
    )
    for cls in (
        "widget-ArticleLevelMetrics", "widget-ArticleCitedBy",
        "widget-ArticleListNewAndPopular", "widget-UserCommentBody",
        "widget-UserComment", "widget-Lockss", "widget-VideoListAccess",
        "widget-AdBlock",
    ):
        for _ in range(8):
            before = html
            html = _remove_nested_element(
                html, rf'<div\b[^>]*class="[^"]*{cls}\b[^"]*"[^>]*>',
            )
            if html == before:
                break

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze + reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Collapse the page-grid parent so #ContentColumn fills the body
        # width regardless of viewport. The 3-col CSS grid
        # (InfoColumn | ContentColumn | Sidebar) lives on
        # .page-column-wrap.article-browse_content; with the two side
        # tracks now empty, the surviving ContentColumn is still pinned
        # to the center track unless the grid is flattened.
        "#main,.master-main,.content-main_content,"
        ".page-column-wrap,.article-browse_content,"
        "#ContentColumn{display:block !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important;float:none !important;"
        "background:#fff !important;"
        "grid-template-columns:none !important}"
        # Capped reading-column wrapper.
        ":root .widget-ArticleMainView{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        ":root .widget-ArticleMainView *{"
        "max-width:100% !important;min-width:0 !important;"
        "box-sizing:border-box !important}"
        # Tables ship inside <div class=table-overflow> which sets
        # overflow:auto; the table itself uses table-layout:auto, so its
        # natural min-content width can exceed the wrapper and force
        # horizontal scrolling. Force fixed table layout + 100% width so
        # cells wrap instead.
        ":root .widget-ArticleMainView table{"
        "width:100% !important;max-width:100% !important;"
        "table-layout:fixed !important}"
        ":root .widget-ArticleMainView .table-overflow{overflow:visible !important}"
        ":root .widget-ArticleMainView td,:root .widget-ArticleMainView th{"
        "word-break:break-all !important;overflow-wrap:anywhere !important;"
        "white-space:normal !important}"
        # Zero side padding/margin on every nested wrapper inside the cap
        # so text sits flush with the wrapper's own 16-px gutter. Keep
        # vertical margins (margin-top/bottom) intact so the publisher's
        # native section rhythm — e.g. the 16-px margin-top that
        # separates the body `.article-section-wrapper` from the
        # abstract `.article-section-wrapper` — is preserved.
        ":root .widget-ArticleMainView .article-browse_content-wrap,"
        ":root .widget-ArticleMainView .content-inner-wrap,"
        ":root .widget-ArticleMainView .module-widget,"
        ":root .widget-ArticleMainView .article-top-widget,"
        ":root .widget-ArticleMainView .content-metadata_wrap,"
        ":root .widget-ArticleMainView .article-section-wrapper,"
        ":root .widget-ArticleMainView section{"
        "margin-left:0 !important;margin-right:0 !important;"
        "padding-left:0 !important;padding-right:0 !important;"
        "width:auto !important;max-width:100% !important}"
        # First-/last-child margin reset — scoped to direct children of
        # the wrapper only (via `>`). Blanket `*:first-child` was
        # zeroing the natural margin/padding on every reference list
        # item, collapsing the references against each other. Same
        # caution applies to `*:last-child`: a descendant rule zeros
        # the last <p>'s margin-bottom inside `section.abstract`, which
        # the publisher uses to space the final paragraph away from the
        # abstract section's bottom border.
        ":root .widget-ArticleMainView>*:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        ":root .widget-ArticleMainView>*:last-child,"
        ":root .widget-ArticleMainView>*:last-child>*:last-child,"
        ":root .widget-ArticleMainView>*:last-child>*:last-child>*:last-child,"
        ":root .widget-ArticleMainView>*:last-child>*:last-child>*:last-child>*:last-child,"
        ":root .widget-ArticleMainView>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child,"
        ":root .widget-ArticleMainView>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child"
        "{margin-bottom:0 !important;padding-bottom:0 !important}"
        # Force-expand the "Author & Article Information" accordion —
        # the publisher's stylesheet sets `.js-metadata-wrap{display:none}`
        # and JS toggles it on click. Without JS the corresponding-
        # author block, funding awards, and publication-history dates
        # are invisible.
        ":root .widget-ArticleMainView .js-metadata-wrap{"
        "display:block !important}"
        # The permissions/copyright block ships with a trailing 8px
        # margin-bottom that isn't zeroed by *:last-child because its
        # DOM siblings (copyright-year, copyright-holder) render as
        # zero-height/hidden. Kill that residual bottom gap explicitly.
        ":root .widget-ArticleMainView .permissionstatement-section-wrapper,"
        ":root .widget-ArticleMainView .metadata-copyright{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # Figure action row (`.fig-orig` containing "View large" and
        # "Download slide" buttons): the publisher's natural rule
        # `.fig-orig{display:flex;...}` was being beaten by my generic
        # descendant rules (`*:first-child{margin-top:0;padding-top:0}`
        # zeroed the first <a>'s padding so the two buttons rendered
        # back-to-back as "VIEW LARGEDOWNLOAD SLIDE"). Re-establish
        # flex layout with explicit gap, restore button padding, and
        # honor the publisher's `.fig-view-orig{display:none}` rule.
        ":root .widget-ArticleMainView .fig-view-orig{display:none !important}"
        ":root .widget-ArticleMainView .fig-orig{"
        "display:flex !important;justify-content:center !important;"
        "flex-wrap:wrap !important;gap:.5rem 1rem !important;"
        "margin:1rem 0 !important}"
        ":root .widget-ArticleMainView .fig-orig a{"
        "display:inline-block !important;margin:0 !important;"
        "padding:.5rem 1rem !important}"
        # Figures: each `<div class="fig fig-section">` ships with a
        # duplicate `<div class="fig fig-modal reveal-modal">` modal that
        # the publisher hides via JS (`aria-hidden=true`, default
        # `display:none` on `.reveal-modal`). Without JS the modal can
        # leak as a duplicate figure block — hide it explicitly.
        ":root .widget-ArticleMainView .fig.fig-modal{display:none !important}"
        # The lazyload fix in get_refs.py swaps img.content-image src ←
        # data-src to fetch the medium JPEG (~700–1000 px, ~150 KB) at
        # capture time. Force the inline figure image to fill the column
        # width above the caption. The parent `<a class=fig-link>` has
        # no padding natively but its inline-block default leaves the
        # image baseline-positioned; force block + zero margin.
        ":root .widget-ArticleMainView .fig.fig-section .graphic-wrap{"
        "margin:0 !important;padding:0 !important}"
        ":root .widget-ArticleMainView .fig.fig-section a.fig-link{"
        "display:block !important;margin:0 !important;padding:0 !important}"
        ":root .widget-ArticleMainView .fig.fig-section img.content-image{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
        # Trailing display:inline canvas (`hiddenCanvasElement`) generates
        # a baseline line-box at body bottom that adds ~16 px to docH
        # below the wrapper. Hide it.
        "canvas.hiddenCanvasElement{display:none !important}"
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

    Biologists wraps the journal name in <em>...</em> inside <div class=source>,
    so accept nested tags and strip them. Stops at the first </div>, which is
    fine because these fields are not nested.
    """
    m = re.search(rf'class=["\']?{cls}["\']?[^>]*>(.*?)</div>', entry, re.DOTALL)
    return strip_tags(m.group(1)).strip() if m else ""


def _parse_inline_authors_title(entry):
    """Parse authors and title from inline citation text (older AACR format).

    In older AACR papers, references have format:
      "Rodier F, Kim SH, Nijjar T, Campisi J. Cancer and aging: ... <div class=source>..."
    i.e. authors and title are plain text before the first structured div
    (source/article-title). Authors end with a period; title follows up to
    the next period before the source div.

    Returns (authors_list, title) where authors are "LastName IN" strings.
    """
    # Find citation container
    cm = re.search(r'class="citation mixed-citation"[^>]*>(.*?)</div>', entry, re.DOTALL)
    if not cm:
        return [], ""
    cit_html = cm.group(1)

    # Take text before the first source/article-title/year div (whichever first)
    first_div = re.search(
        r'<div\s+class=["\']?(?:source|article-title|year|volume|fpage)["\']?',
        cit_html,
    )
    pre = cit_html[:first_div.start()] if first_div else cit_html
    pre_text = unescape(re.sub(r'<[^>]+>', ' ', pre)).strip()
    pre_text = re.sub(r'\s+', ' ', pre_text)

    # Split authors from title: authors end with ". " before title
    # Authors part ends with "LastName IN." or "et al."
    # Title begins after. Find last "<Initials>[.] " or "et al. " boundary.
    m = re.search(r'(.*?(?:et\s+al\.?|\b[A-Z]{1,5}))\.\s+(.+?)\.?\s*$', pre_text)
    if m:
        author_text = m.group(1).strip().rstrip('.').strip()
        title = m.group(2).strip()
    else:
        author_text = pre_text
        title = ""

    # Parse authors: "LastName IN, LastName IN, ..."
    authors = []
    for part in re.split(r',\s*', author_text):
        part = part.strip().rstrip('.').strip()
        if part and part.lower() != "et al":
            authors.append(part)
    return authors, title


def _parse_structured_authors(entry):
    """Parse authors from <div class=surname>/<div class=given-names> pairs.

    AACR stores given names as pre-concatenated initials (e.g. "RK", "H. Tomas",
    "MA."). When the value is all-uppercase with no interior space, treat it
    as already-formatted initials and keep it verbatim; otherwise split on
    whitespace/period and take the first letter of each part.
    """
    authors = []
    # Between the surname and given-names divs the markup varies:
    #   AACR:       </div> <div class=given-names>
    #   Biologists: </div>, <div class=given-names>   (comma-space in text)
    # Allow any short run of characters (no angle brackets) between them.
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
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    AACR-specific: each ref is wrapped in <div data-content-id=b...>; newer
    papers have structured surname/given-names/article-title divs, older
    papers have inline author/title text before structured source/year/volume.
    """
    refs = []
    m = re.search(r'class="ref-list[^"]*"', html)
    if not m:
        return refs

    ref_section = html[m.start():]

    # Locate each ref entry. Different Silverchair journals use different
    # ID attribute names:
    #   AACR:       <div data-content-id=bN xmlns=...>
    #   Biologists: <div content-id=<paperid>cN xmlns=...>
    # Both variants carry an `xmlns` attribute that distinguishes the ref
    # wrapper from surrounding chrome.
    items = list(re.finditer(
        r'<div\s+(?:data-)?content-id=[^\s>]+\s+xmlns',
        ref_section,
    ))
    if not items:
        return refs

    # Boundary for last ref: find widget/footnote/copyright after it
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

        # Structured fields
        title = _extract_ref_field(entry, "article-title")
        if not title:
            title = _extract_ref_field(entry, "chapter-title")
        journal = _extract_ref_field(entry, "source").rstrip('.')
        volume = _extract_ref_field(entry, "volume")
        issue = _extract_ref_field(entry, "issue")
        year = _extract_ref_field(entry, "year")
        fpage = _extract_ref_field(entry, "fpage")
        lpage = _extract_ref_field(entry, "lpage")
        # Older AACR refs wrap only fpage in a div; lpage is plain text
        # right after (e.g. "<div class=fpage>977</div>–90.").
        if fpage and not lpage:
            fm = re.search(
                rf'class=["\']?fpage["\']?[^>]*>{re.escape(fpage)}</div>\s*[–—-]\s*(\d[\w]*)',
                entry,
            )
            if fm:
                lpage = fm.group(1)
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # Authors: structured first, then fall back to inline text
        authors = _parse_structured_authors(entry)
        if not authors or not title:
            inline_authors, inline_title = _parse_inline_authors_title(entry)
            if not authors:
                authors = inline_authors
            if not title:
                title = inline_title

        # DOI from Crossref link
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
    # Find article-body container (AACR uses unquoted class=article-body)
    body_m = re.search(r'<div\s+class=article-body\b[^>]*>', html)
    if not body_m:
        return ""

    # Scope to matching </div> to avoid pulling page chrome at the tail
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

    # Start: after abstract section
    start = 0
    for pos, text, kind in h2s:
        if kind == "abstract":
            abs_end = content.find('</section>', pos)
            if abs_end >= 0:
                start = abs_end + len('</section>')
            else:
                start = pos + 200
            break

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text, kind) in enumerate(h2s):
        if kind == "ref" or _REF_RE.search(text):
            first_ref_idx = i
            break

    parts = []

    # Capture un-headed intro content between abstract and first body h2
    first_non_abs_pos = None
    for pos, text, kind in h2s:
        if pos >= start and kind != "abstract":
            first_non_abs_pos = pos
            break
    if first_non_abs_pos is not None and first_non_abs_pos > start:
        parts.append((start, first_non_abs_pos))
    elif first_non_abs_pos is None and first_ref_idx is None:
        # No headings after abstract: take everything until end
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
    AACR-specific: main_text is composed of abstract + body with "## Abstract"
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
    """Parse AACR HTML into a papers/*.json-format dict."""
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
