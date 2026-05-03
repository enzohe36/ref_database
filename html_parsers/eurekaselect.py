"""Bentham Science (eurekaselect.com) HTML parser.

Eurekaselect pages are typically abstract-only landing pages for paywalled
articles. No references, no main body beyond abstract + keywords. Authors
and affiliations live in a Print modal rather than semantic markup.
"""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    format_author_name,
    format_doi,
    get_all_meta,
    get_meta,
    neutralize_media_queries,
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
    """Normalize Eurekaselect HTML to a single centered text column.

    Chrome stripped (Step 3):
      - <header> site navigation
      - <footer> site footer
      - "Purchase PDF" add-to-cart button (id=addcartbtn)
      - "Become a …" 4-up promo row (class=ebm-banners)
      - Related-journals carousel (id=related)
      - Everything from "« Previous / Next »" navigation onward inside
        <main id="article"> — cuts article-level prev/next, action-button
        row (Mark Item / Rights & Permissions / Print / Cite / share /
        second Purchase PDF), Article Metrics sidebar, and Related
        Articles card. The reading content upstream (abstract + body +
        keywords + references) stays intact.

    Reading column (Step 4) spans two top-level article blocks:
    <section id="article-banner"> (journal name + title + authors)
    followed by <main id="article"> (abstract + body + references).
    Both are capped at 752 px with 16 px side padding; top padding 56 px
    sits on the banner, bottom padding 56 px on the main — zero between
    them so the two wrappers read as one column.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    html = _remove_nested_element(html, r"<header\b[^>]*>")
    html = _remove_nested_element(html, r"<footer\b[^>]*>")
    html = remove_elements_by_id(html, "addcartbtn", "related")
    html = remove_elements_by_selector(html, "ebm-banners")
    # Right-side `<div class="col-md-4 ji-links">` menu (Article Metrics,
    # Find-your-institution, Journal Information accordion, Related
    # Articles). None of it is reading content.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass="[^"]*\bji-links\b[^"]*"[^>]*>',
        )
        if html == before:
            break
    # ShareThis inline share-buttons row (facebook/twitter/linkedin/
    # sharethis) that renders below the abstract. Lives in a separate
    # <span class="sharethis-inline-share-buttons ...">.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<span\b[^>]*\bclass="[^"]*\bsharethis-inline-share-buttons\b[^"]*"[^>]*>',
        )
        if html == before:
            break

    # Cut from the "« Previous / Next »" nav card-body up to (but not
    # including) the PrintWindow modal. Everything in between (action
    # buttons, Article Metrics card, Related Articles card) goes.
    # PrintWindow must stay because _parse_affiliations_from_modal reads
    # author affiliations out of it — removing it breaks parse_article
    # output parity. The PrintWindow is hidden by Bootstrap's .modal
    # default display:none, so keeping it has no visual cost.
    html = re.sub(
        r"<div\s+class=card-body>\s*<a[^>]*>\s*«\s*Previous"
        r".*?(?=<div\s+class=[\"']?modal\b)",
        "",
        html,
        flags=re.DOTALL,
    )

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        # Layout freeze (Step 2). html fills the viewport; body is pinned
        # to 720 px and centered. This way wider viewports show the reading
        # column centered with symmetric margins instead of glued to the
        # left edge.
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Capped reading column (Step 4). Two wrappers treated as one:
        # banner gets top padding, main gets bottom padding, none between.
        "section#article-banner,main#article{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;box-sizing:border-box !important}"
        "section#article-banner{padding:56px 16px 0 16px !important}"
        # Disable hover shadows on the abstract / content boxes so the
        # page renders as static print.
        ".bigbox-hvr-slow:hover,.hvr-shadow:hover,.hvr-glow:hover,"
        ".hvr-grow-shadow:hover,.hvr-box-shadow-outset:hover,"
        ".hvr-box-shadow-inset:hover,.hvr-border-fade:hover{"
        "box-shadow:none !important;transform:none !important}"
        "main#article{padding:0 16px 56px 16px !important}"
        # Bootstrap's flex grid makes .col-md-* size to content instead of
        # parent when the row is narrower than the md breakpoint — at 720 px
        # viewport, col-md-8 blows out to the width of its widest
        # descendant. Hard-reset the whole grid inside the wrappers to
        # plain block layout with width:100%.
        "section#article-banner .container,main#article .container,"
        "section#article-banner .row,main#article .row,"
        "section#article-banner [class*=\"col-\"],main#article [class*=\"col-\"]{"
        "display:block !important;float:none !important;"
        "margin-left:0 !important;margin-right:0 !important;"
        "padding-left:0 !important;padding-right:0 !important;"
        "width:100% !important;max-width:100% !important;min-width:0 !important;"
        "flex:0 0 auto !important;box-sizing:border-box !important}"
        # Clamp any descendant so no inline width / SVG natural size can
        # push a flex ancestor past the wrapper.
        "section#article-banner *,main#article *{"
        "max-width:100% !important;min-width:0 !important}"
        # Remove the blue gradient behind journal info. Force all
        # banner descendants to black text so the previously
        # white-on-blue journal name / volume / issue chips render
        # legibly on white. EXCEPT `.badge-info` ("Research Article"
        # label) which has its own blue background and should stay
        # native-white on that colored badge.
        "section#article-banner{"
        "background:#fff !important;background-image:none !important}"
        "section#article-banner,section#article-banner *{"
        "color:#000 !important}"
        "section#article-banner .badge-info,"
        "section#article-banner .badge-info *{"
        "color:#fff !important}"
        # Buttons above the abstract (Editor-in-Chief, Back, Journal,
        # Subscribe, Submit Now) natively use `border: 1px solid
        # rgb(248,249,250)` (near-white) meant for a dark background.
        # On our white banner background those borders are invisible;
        # force them to black.
        "section#article-banner .btn,"
        "section#article-banner button,"
        "section#article-banner .dropdown-toggle{"
        "border-color:#000 !important}"
        # Direct-child first-child only (SKILL.md pitfall — descendant
        # form zeros every section-heading's top margin).
        "section#article-banner>*:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        "main#article>*:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
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
    """Extract bundled metadata from citation_* meta tags."""
    date = get_meta(html, "citation_publish_on")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    return {
        "title": get_meta(html, "citation_title"),
        "journal": get_meta(html, "citation_journal_title"),
        "year": year,
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_affiliations_from_modal(html):
    """Extract affiliation strings from the Print modal.

    Layout: <p><strong>Affiliation: </strong><ul><li>AFF1</li><li>AFF2</li></ul>
    """
    m = re.search(
        r"<strong>\s*Affiliation:?\s*</strong>(.*?)(?:<p>|</div>)",
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    affs = []
    # <li> tags here may be unclosed; terminate at next <li>, </ul>, or </li>.
    for li in re.finditer(
        r"<li[^>]*>(.*?)(?=<li|</ul>|</li>)",
        m.group(1), re.DOTALL,
    ):
        text = re.sub(
            r"\s+", " ", unescape(strip_tags(li.group(1))).strip()
        ).rstrip(",. ")
        if text:
            affs.append(text)
    return affs


def _parse_authors(html):
    """Extract authors from citation_author meta tags.

    Affiliations are sourced from the Print modal since the HTML does not
    provide per-author affiliation mapping. All authors share the same
    affiliation list.
    """
    names = get_all_meta(html, "citation_author")
    affiliations = _parse_affiliations_from_modal(html)
    authors = []
    for n in names:
        cleaned = re.sub(r"\s+", " ", n).strip().rstrip("*")
        if not cleaned:
            continue
        authors.append({
            "author": format_author_name(cleaned),
            "affiliation": list(affiliations),
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Eurekaselect abstract pages do not expose references."""
    return []


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract text from <div id=abstract>."""
    m = re.search(
        r'<div[^>]*id="?abstract"?[^>]*>(.*?)</div>\s*</div>',
        html, re.DOTALL,
    )
    if not m:
        return ""
    inner = m.group(1)
    tm = re.search(
        r'<div[^>]*class="?text-justify"?[^>]*>(.*?)</div>',
        inner, re.DOTALL,
    )
    if not tm:
        return ""
    content = strip_common(tm.group(1))
    text = tags_to_text(content)
    return text.strip()


def _parse_keywords(html):
    """Extract keywords from the abstract card."""
    m = re.search(
        r"<strong>\s*Keywords:?\s*</strong>(.*?)</p>", html, re.DOTALL,
    )
    if not m:
        return []
    inner = m.group(1)
    kws = []
    for am in re.finditer(r"<a[^>]*>(.*?)</a>", inner, re.DOTALL):
        text = unescape(strip_tags(am.group(1))).strip().rstrip(",.")
        if text:
            kws.append(text)
    return kws


def _parse_main_text(html):
    """Build main_text from abstract + keywords.

    Eurekaselect landing pages do not include body sections or references,
    so main_text consists of only the abstract and keyword list.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append("## Abstract\n" + abstract)
    keywords = _parse_keywords(html)
    if keywords:
        parts.append("## Keywords\n" + ", ".join(keywords))
    return drop_noise("\n\n".join(parts), _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Eurekaselect HTML into a papers/*.json-format dict."""
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
