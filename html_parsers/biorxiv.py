"""bioRxiv (biorxiv.org) HTML parser.

bioRxiv runs on the HighWire Press platform — same DOM idioms as CSHLP
(`div.section.ref-list` + `ol.cit-list > li` for references,
`<div class="fulltext-view">` for body, `citation_*` meta tags for
metadata). This parser extracts full-text content when the capture is
from the `/content/<doi>v1.full` URL; abstract-only landing pages yield
metadata + abstract only.
"""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
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
    """Normalize bioRxiv HTML to a single centered text column.

    bioRxiv runs on HighWire; the visible article sits inside
    ``.main-content-wrapper`` with siblings for prev/next article nav
    (``.content-left-wrapper`` + ``.content-right-wrapper``) and global
    chrome (<header>, <footer>, #sliding-popup cookie banner).

    Chrome stripped (Step 3):
      - <header id=section-header> site masthead, <footer>, breadcrumbs.
      - Cookie consent ``#sliding-popup`` banner.
      - Prev/next article nav sidebars (``.content-left-wrapper``,
        ``.content-right-wrapper``).
      - In-page tab bar (Abstract / Full Text / PDF) via
        ``pane-highwire-panel-tabs`` and
        ``pane-highwire-panel-tabs-container``.
      - Everything from ``pane-highwire-back-to-top`` to the end of
        ``.main-content-wrapper`` (disqus stub, share tools, related
        collections, subject-collections carousel, forward form, citation
        export — none of it is reading content).

    Reading column (Step 4): cap ``.main-content-wrapper`` at 752 px with
    56 px top/bottom and 16 px side padding.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    html = _remove_nested_element(html, r"<header\b[^>]*>")
    html = _remove_nested_element(html, r"<footer\b[^>]*>")
    html = remove_elements_by_id(html, "sliding-popup")
    # Prev/next-article sidebars (HighWire ``node-pager`` layout).
    html = _remove_nested_element(
        html,
        r'<div[^>]*\bclass="[^"]*\bcontent-left-wrapper\b[^"]*"[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div[^>]*\bclass="[^"]*\bcontent-right-wrapper\b[^"]*"[^>]*>',
    )
    # Note: keep `.pane-highwire-panel-tabs` (Abstract / Full Text /
    # Info/History / Metrics / Preview PDF row). It's a static
    # row of tab labels at narrow vw — useful navigation, not chrome.
    # Disqus comment placeholder + "Back to top" link (both sit at the
    # bottom of the active tab's content column).
    html = remove_elements_by_selector(
        html,
        "pane-disqus-comment",
        "pane-highwire-back-to-top",
        "pane-highwire-node-pager",
    )
    # Entire right-sidebar column inside the tab container: article
    # tools, variant link, print / supplementary / share / cite / email /
    # collections / subject carousel / forward form / citation export.
    # None of it is reading content.
    html = _remove_nested_element(
        html,
        r'<div[^>]*\bclass="[^"]*\bpanel-region-sidebar-right\b[^"]*"[^>]*>',
    )
    # Trailing omega-12 row (article clipboard copy, service links,
    # citation-export tool, and the "Subject Collections" carousel) that
    # lives below the two-col split.
    html = _remove_nested_element(
        html,
        r'<div[^>]*\bclass="[^"]*\bpanels-flexible\b[^"]*"[^>]*>',
    )
    html = remove_elements_by_selector(
        html,
        "pane-highwire-article-clipboard-copy",
        "pane-highwire-article-collections",
        "pane-biorxiv-subject-collections",
        "pane-highwire-subject-collections",
        "pane-forward-form",
        "pane-highwire-citation-export",
    )
    # Remove the whole omega-12 panel display that holds the post-article
    # chrome (preface / content / postscript rows below the tab split).
    html = re.sub(
        r'<div\s+class="panel-display omega-12-onecol"[\s\S]*?</div>\s*</div>\s*</div>',
        "",
        html,
        count=1,
    )
    # Sticky comments/side-panel (HighWire `<aside class=csh_panelc>` +
    # `<div class=csh_panelc-header>`) — positioned fixed at T=2400
    # (offscreen below at top viewport) but surfaces mid-scroll. Kill
    # both so nothing pops when scrolling through the article.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<aside\b[^>]*\bclass=["\']?[^"\'>]*\bcsh_panelc\b[^>]*>',
        )
        if html == before:
            break
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bcsh_panelc-header\b[^>]*>',
        )
        if html == before:
            break

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        # Layout freeze (Step 2). The HighWire "grid-*" layout uses
        # 960-px containers that off-center the article at wide viewports;
        # force everything to fluid-with-cap so the wrapper cap is the
        # only thing that sets the visible column width.
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important;"
        "overflow-y:overlay}"
        "html::-webkit-scrollbar{width:0}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:100% !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Collapse HighWire's outer grid scaffolding so the main-content
        # wrapper sizes to its own cap instead of inheriting grid-28 /
        # suffix-1 / prefix-1 widths.
        "#page,#zone-content,#region-content,.region-content-inner,"
        ".panel-panel,.panel-region-content,.panels-jcore-2col,"
        ".grid-28,.grid-17,.grid-11,.suffix-1,.prefix-1,.alpha,.omega{"
        "float:none !important;width:auto !important;"
        "max-width:100% !important;min-width:0 !important;"
        "margin:0 auto !important;padding:0 !important;"
        "display:block !important}"
        # Capped reading column (Step 4).
        ".main-content-wrapper{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        ".main-content-wrapper *{"
        "max-width:100% !important;min-width:0 !important}"
        # Zero the horizontal offsets on panel/pane wrappers so the text
        # column lines up flush with the 16-px wrapper padding.
        ".main-content-wrapper .panel-pane,"
        ".main-content-wrapper .pane-content,"
        ".main-content-wrapper .highwire-article-citation,"
        ".main-content-wrapper .highwire-cite,"
        ".main-content-wrapper .fulltext-view,"
        ".main-content-wrapper .highwire-markup,"
        ".main-content-wrapper .article,"
        ".main-content-wrapper .section,"
        ".main-content-wrapper .panel-panel,"
        ".main-content-wrapper .panel-display{"
        "float:none !important;width:auto !important;"
        "margin-left:0 !important;margin-right:0 !important;"
        "padding-left:0 !important;padding-right:0 !important}"
        # Headings inside `.highwire-markup` ship with
        # `margin-left:-15px` to bleed outside the article gutter; zero
        # it so h2 aligns with body text at the wrapper padding edge.
        # Figures / tables / videos ship with `margin:25px -15px` for the
        # same bleed-out effect — zero the horizontal margins there too.
        # Keep their padding-left/right so the caption text inside `.fig`
        # stays indented from the figure box edge as it does in raw.
        ".main-content-wrapper h1,.main-content-wrapper h2,"
        ".main-content-wrapper h3,.main-content-wrapper h4,"
        ".main-content-wrapper .fig,.main-content-wrapper .table,"
        ".main-content-wrapper .video-content{"
        "margin-left:0 !important;margin-right:0 !important}"
        # Figure images: a get_refs.py browser-script swaps
        # `img.highwire-fragment` src from the medium GIF (~440 px) to
        # the large JPG/PNG on the parent <a href> (~800-1500 px native).
        # The publisher renders the medium image at its native pixel
        # dimensions centered inside the `.fig-inline-img-wrapper`,
        # leaving a visibly narrower image than its caption. Force the
        # large image to fill the wrapper (block, full width) so the
        # figure aligns with caption width. The publisher's parent <a>
        # carries `padding: 8px` which would shave 16 px off the image
        # width; zero those so img matches caption width exactly.
        ":root .main-content-wrapper a.highwire-fragment{"
        "padding-left:0 !important;padding-right:0 !important}"
        ":root .main-content-wrapper img.highwire-fragment{"
        "display:block !important;width:100% !important;"
        "height:auto !important;margin:0 !important}"
        # Empty panel-separator spacers (trailing siblings of the tab
        # container) add ~13 px to the doc bottom.
        ".main-content-wrapper .panel-separator{"
        "display:none !important;margin:0 !important;"
        "padding:0 !important;height:0 !important;border:none !important}"
        # First-child margin reset — DIRECT children only (`>`) per the
        # SKILL.md pitfall. Descendant `*:first-child{margin-top:0}`
        # zeros every section heading's top margin (H2s/H3s are the
        # first child of their .section/.subsection containers),
        # collapsing biorxiv's 15-25 px section rhythm. Descendant
        # `*:last-child{margin-bottom:0}` is still safe.
        ".main-content-wrapper>*:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        ".main-content-wrapper>*:last-child,"
        ".main-content-wrapper>*:last-child>*:last-child,"
        ".main-content-wrapper>*:last-child>*:last-child>*:last-child,"
        ".main-content-wrapper>*:last-child>*:last-child>*:last-child>*:last-child,"
        ".main-content-wrapper>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child,"
        ".main-content-wrapper>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child"
        "{margin-bottom:0 !important;padding-bottom:0 !important}"
        # Tab container has its own `margin-bottom:10px` that isn't
        # caught by `*:last-child` because the two panel-separator
        # spacers are stacked after it.
        ".main-content-wrapper .pane-highwire-panel-tabs-container{"
        "margin-bottom:0 !important}"
        # Drupal ships hidden modal dialogs (Email article / Citation
        # Tools / Share) and per-author qTip popup templates in the DOM.
        # Their built-in `display:none` is toggled on by JS, so when a
        # reader opens the saved HTML without JS enabled, the dialogs
        # render as visible blocks below the article.
        # Per-author popup templates have class stems like `author-tooltip-0`
        # — anchored with the `author-tooltip-` prefix so the sibling
        # `has-author-tooltip` wrapper class on the cite card is unaffected.
        ".ui-dialog,.cluetip,.cluetip-default,"
        "[class^=author-tooltip-],[class*=' author-tooltip-'],"
        "[id^=highwire-author-tooltip-],"
        "#sliding-popup,#eu-cookie-withdraw-banner{"
        "display:none !important}"
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
    via `format_author_name` below the helper flips to "LastName IN".
    """
    from ._helpers import format_author_name
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
