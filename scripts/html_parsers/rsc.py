"""Royal Society of Chemistry (rsc.org) HTML parser."""

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
    parse_meta_authors,
    remove_elements_by_id,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = ()

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
    """Apply Phase 2 layout rules for pubs.rsc.org articlelanding pages.

    Step 1: cap body width at 752 px, center, neutralize @media so the
            publisher's narrow CSS branch always applies. RSC's stylesheet
            also reserves a 16 px non-overlay scrollbar gutter; force
            overflow-y:overlay + zero-width WebKit scrollbar so body
            renders 720 at vw=720.
    Step 2: remove the OneTrust consent banner (#onetrust-consent-sdk
            wrapper) and its dim-overlay sibling
            (.onetrust-pc-dark-filter).
    Step 3: remove sticky chrome flagged by scan_sticky.py — the
            `.pubs-nav-drawer` slide-out menu (position:fixed top:0,
            visible only when toggled but still occupies the static
            screenshot's left edge) and the OneTrust banner already
            stripped in Step 2.
    Step 4: collapse the `.layout-control` flex grid (60/40 primary /
            secondary split) to a plain block stack. Both panels then
            sit one above the other inside the body cap. The secondary
            panel holds the About / Cited by / Related-content tabs +
            article metadata (DOI / Submitted / Accepted / Download /
            Permissions); kept as publisher-native sidebar content
            stacked below the article instead of beside it.
    Step 5: hide the Google ad slot that trails the article
            (`#pbgrd-mpur-c`) and the bottom-of-page Spotlight + Ad
            sections that ship as `.layout__content--padded.text--centered`
            siblings of `.layout-control` inside `.viewport`.
    Step 6: page background is white; explicit white on html/body and
            the layout-control descendants. The OneTrust dim-overlay's
            tinted backdrop is removed in Step 2.
    Step 7: figures inline at full natural resolution via the
            _RSC_FIGURES_FIX_JS pre-capture script (swaps `<img src>` ←
            parent `<a href>` high-res GIF URL). No retrieval issue.
    Step 8: figures live inside `<div class=img-tbl id=fig<N>>` →
            `<figure class=img-tbl__image>` → `<a>` → `<img>` followed
            by `<figcaption class=img-tbl__caption>`. Force img to
            column width with the caption below.
    Step 9: expand `.drawer-control .drawer__content` (the per-article
            "Author affiliations" panel, `<div id=pnlAuthorAffiliations>`,
            wraps each author affiliation in a sibling `<p>`). Closed
            state is `display:none` on a `position:relative` parent —
            expansion qualifies as in-place push-down (no
            position:absolute/fixed, no z-index, no box-shadow). The
            `.tab__panel` rule reads `position:absolute` when closed
            and `position:relative` when open, BUT tabs are mutually
            exclusive in the publisher's native UI — only the active
            one is shown. Leave them; do NOT force every tab open.
    """
    html = neutralize_media_queries(html)

    # Step 2 — OneTrust cookie banner + dim overlay backdrop.
    html = remove_elements_by_id(
        html,
        "onetrust-consent-sdk",
        "onetrust-banner-sdk",
        "onetrust-pc-sdk",
    )
    # The dim filter ships as a sibling div with unquoted class attrs.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass=["\']?onetrust-pc-dark-filter\b[^>]*>',
    )

    # Step 3 — sticky / fixed-position chrome other than chrome handled
    # by Step 2. `.pubs-nav-drawer` is the slide-out site menu (fixed
    # top:0 left:0, visible only when the hamburger is clicked; renders
    # as a 292 px column in the static screenshot). `.skipto-control` is
    # the accessibility skip-to-content overlay.
    for cls in ("pubs-nav-drawer", "skipto-control"):
        html = _remove_nested_element(
            html,
            rf'<div\b[^>]*\bclass=["\']?{re.escape(cls)}\b[^>]*>',
        )

    # Step 5 — ad slots. `#pbgrd-mpur-c` is the Google ad <section>
    # below the article. Bottom-of-page Spotlight + Advertisements
    # sections render as `<section class="layout__content--padded
    # text--centered">` siblings of `.layout-control` inside `.viewport`.
    html = re.sub(
        r'<section\b[^>]*\bid=["\']?pbgrd-mpur-c\b[^>]*>.*?</section>',
        '', html, flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(
        r'<section\b[^>]*\bclass=["\']?[^"\'>]*\blayout__content--padded\b'
        r'[^"\'>]*\btext--centered\b[^>]*>.*?</section>',
        '', html, flags=re.DOTALL | re.IGNORECASE,
    )

    override = (
        "<style>"
        # Step 1 — cap body, center, force narrow form.
        # Reclaim 16 px non-overlay scrollbar gutter so body renders 720
        # at vw=720.
        "html{overflow-y:overlay !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important;"
        "background:#fff !important}"
        "html::-webkit-scrollbar{width:0 !important;height:0 !important}"
        "body{width:auto !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "padding:0 !important;background:#fff !important;"
        "box-sizing:border-box !important;"
        "overflow-wrap:break-word !important;word-wrap:break-word !important}"
        # Page-wide RSC wrappers — collapse to body cap so nothing
        # escapes the centered envelope.
        "main,#maincontent,.viewport,.r-gutter,"
        ".layout-control,.layout__content,.layout__content--padded{"
        "display:block !important;float:none !important;"
        "width:auto !important;max-width:100% !important;"
        "min-width:0 !important;"
        "margin-left:auto !important;margin-right:auto !important;"
        "padding-left:0 !important;padding-right:0 !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # Step 4 — collapse the 60/40 layout flex split. Primary panel
        # (article body) sits above secondary panel (About / Cited by /
        # Related tabs + article metadata).
        ".layout__panel--primary,.layout__panel--secondary{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:100% !important;"
        "min-width:0 !important;flex:0 0 auto !important;"
        "margin:0 !important;padding:16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        ".layout__panel--primary *,.layout__panel--secondary *{"
        "max-width:100% !important;min-width:0 !important}"
        # The journal thumbnail (`.list__image-col` + `.list__item-img`)
        # in the "From the journal" article-nav banner: the universal
        # max-width rule above collapses the table-cell + img to ~16 px
        # because min-width:0 lets table-cell shrink-to-fit ignore the
        # img's intrinsic size. Restore native dimensions.
        ".list__image-col,.list__item-img{"
        "min-width:auto !important;max-width:none !important}"
        # The capsule article image and crossmark button float right on
        # native CSS — un-float so they don't push text off-axis.
        ".capsule__article-image,.crossmark-button{float:none !important}"
        # Step 9 — expand the Author affiliations drawer (in-place push-
        # down). The per-tab panels (.tab__panel) stay native: only the
        # `.open` panel renders, others are mutually-exclusive and
        # publisher-native chrome.
        ".drawer__content{display:block !important}"
        ".tab__panel:not(.open){display:none !important}"
        # Step 8 — figures: img above caption at column width.
        # RSC structure: <div class=img-tbl id=fig<N>> →
        # <figure class=img-tbl__image> → <a> → <img>; sibling
        # <figcaption class=img-tbl__caption>.
        ".img-tbl{margin:1rem 0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important;"
        "display:block !important}"
        "figure.img-tbl__image{display:block !important;"
        "margin:0 !important;padding:0 !important;float:none !important}"
        "figure.img-tbl__image > a{display:block !important;"
        "margin:0 !important;padding:0 !important}"
        "figure.img-tbl__image > a > img,figure.img-tbl__image > img{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
        "figcaption.img-tbl__caption{display:block !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
    return html
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
    if journal:
        journal = re.sub(r"  +", " ", journal.replace(".", "")).strip()
    else:
        journal = ""

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
    RSC citation_author meta tags use 'Given Last' form; format_author_name
    handles the flip via parse_combined_name + format_name.
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

def _flip_initials_first(name):
    """Convert 'F. M. LastName' to 'LastName FM' via shared helpers."""
    return format_author_name(name)


def _parse_citation_reference(content):
    """Parse a single citation_reference meta tag content string.

    RSC format: 'citation_title=...; citation_author=A; citation_author=B;
    citation_journal_title=X; citation_volume=Y; citation_pages=FP-LP;
    citation_publication_date=YYYY;'

    Field separators are ';' optionally followed by whitespace/newlines.
    Returns dict {title, journal, year, volume, issue, pages, doi, authors}.
    """
    fields = {}
    author_parts = []
    for part in re.split(r";\s*", content):
        part = part.strip()
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "citation_author":
            author_parts.append(val)
        else:
            fields[key] = val

    if not fields and not author_parts:
        # Freeform fallback: store full text as title
        return {
            "title": content.strip(),
            "journal": "",
            "year": "",
            "volume": "",
            "issue": "",
            "pages": "",
            "doi": "",
            "authors": [],
        }

    authors = [_flip_initials_first(a) for a in author_parts if a]

    pages = fields.get("citation_pages", "")
    if not pages:
        fp = fields.get("citation_first_page", "")
        lp = fields.get("citation_last_page", "")
        pages = f"{fp}-{lp}" if lp else fp
    pages = pages.replace("\u2013", "-").replace("\u2014", "-")

    journal = fields.get("citation_journal_title", "")
    journal = re.sub(r"\s+", " ", journal).strip().rstrip(".")

    year = fields.get("citation_publication_date", "")
    if year:
        m = re.search(r"(\d{4})", year)
        year = m.group(1) if m else year

    return {
        "title": fields.get("citation_title", "").strip(),
        "journal": journal,
        "year": year,
        "volume": fields.get("citation_volume", ""),
        "issue": fields.get("citation_issue", ""),
        "pages": pages,
        "doi": format_doi(fields.get("citation_doi", "")),
        "authors": authors,
    }


def _parse_references(html):
    """Extract the reference list from citation_reference meta tags."""
    refs = []
    for m in re.finditer(
        r'<meta[^>]*name=["\']?citation_reference["\']?'
        r'[^>]*content="([^"]*)"'
        r'|<meta[^>]*content="([^"]*)"'
        r'[^>]*name=["\']?citation_reference["\']?',
        html,
    ):
        content = unescape(m.group(1) or m.group(2) or "")
        ref = _parse_citation_reference(content)
        refs.append({"": ref})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract from <div class=capsule__text>.

    The capsule__text div sits inside capsule__column-wrapper and contains
    the article abstract as one or more <p> elements.
    """
    m = re.search(
        r'<div[^>]*class=["\']?capsule__text[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    if not m:
        return ""
    return strip_tags(m.group(1)).strip()


def _extract_pnl_article_content(html):
    """Return the inner HTML of the <div id=pnlArticleContent> wrapper.

    This is the article body container on RSC landing pages. Walks balanced
    <div> tags to find the closing </div>. Returns "" if not found.
    """
    m = re.search(r'<div\s+id=pnlArticleContent\b[^>]*>', html)
    if not m:
        return ""
    start = m.end()
    depth = 1
    pos = start
    open_pat = re.compile(r"<div\b", re.IGNORECASE)
    close_pat = re.compile(r"</div\s*>", re.IGNORECASE)
    while depth > 0 and pos < len(html):
        nopen = open_pat.search(html, pos)
        nclose = close_pat.search(html, pos)
        if not nclose:
            break
        if nopen and nopen.start() < nclose.start():
            depth += 1
            pos = nopen.end()
        else:
            depth -= 1
            pos = nclose.end()
            if depth == 0:
                return html[start:pos - len(nclose.group())]
    return html[start:]


def _parse_main_text(html):
    """Extract body text from RSC article-landing pages.

    RSC publishes the full article body inside a <div id=pnlArticleContent
    class=t-html> wrapper that is present in the SingleFile capture but
    paywall-gated visually. The body covers the abstract through the end
    of the back matter (acknowledgements, etc.) and ends at the first
    <h2 class=h--heading2>References</h2> heading. Anything after the
    references list inside the wrapper (Footnote, etc.) is dropped — none
    of the test fixtures expose supplement / extended-data sections that
    the SKILL.md boundary rule would keep.
    """
    body_html = _extract_pnl_article_content(html)
    if not body_html:
        return ""

    # Cut off at the first References heading
    ref_m = re.search(
        r'<h2[^>]*class=["\']?h--heading2[^>]*>\s*References\b',
        body_html, re.IGNORECASE,
    )
    if ref_m:
        body_html = body_html[:ref_m.start()]

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse RSC HTML into a papers/*.json-format dict."""
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
