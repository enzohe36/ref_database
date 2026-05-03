"""PLOS (plos.org) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    parse_meta_authors,
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Download:",
    "PNG",
    "TIFF",
    "larger image",
    "original image",
)

# Reference section title pattern
_REF_RE = re.compile(r"\breferences\b", re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r"supplement|supporting information|extended data|source data"
    r"|expanded view|powerpoint|appendix",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize PLOS HTML to a single centered text column.

    Per format-html-extra.md the reading column starts at "Open Access
    Peer-reviewed" and the left TOC (#nav-article) is stripped so the
    body block reclaims its space. Removals fall into (a) instruction-
    doc items, (b) ads, (c) toolbars. Non-chrome content
    (#almSignposts metric badges, figure-lightbox modal, etc.) stays
    in the DOM.
    """
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    # (a) instruction-doc items --------------------------------------
    # Cookie banner + dark-filter overlay (#cookie-consent wraps both).
    html = remove_elements_by_id(html, "cookie-consent")
    # Left-side TOC (#nav-article). Kept in the DOM below via CSS
    # display:none — its text ("Abstract Author Summary Introduction..."
    # / "Reader Comments" / "Figures") is picked up by parse_main_text,
    # so DOM removal would break JSON parity.
    # Site footer (<footer id=pageftr>). Falls below the article
    # wrapper in the formatted layout, so it's bottom chrome per
    # "ends before bottom chrome".
    html = _remove_nested_element(
        html, r'<footer\b[^>]*\bid=["\']?pageftr\b',
    )
    # (c) toolbars ---------------------------------------------------
    # Site <header>: top nav/search bar. First bare <header> (no class)
    # is the site chrome; inner <header class=title-block> carries the
    # article title and must stay.
    html = _remove_nested_element(html, r"<header>")
    # Filesviewer modal <header>/<footer>: figure-viewer modal chrome.
    for _ in range(5):
        before = html
        html = _remove_nested_element(
            html,
            r'<header\b[^>]*\bclass="[^"]*frontend-filesViewer[^"]*"',
        )
        html = _remove_nested_element(
            html,
            r'<footer\b[^>]*\bclass="[^"]*frontend-filesViewer[^"]*"',
        )
        if html == before:
            break
    # Right-side <aside class=article-aside>: Download PDF / Cite /
    # Share / Save / EndNote / Print action toolbar.
    html = _remove_nested_element(
        html,
        r'<aside\b[^>]*\bclass=["\']?[^"\'>]*\barticle-aside\b',
    )
    # #figures-list: right-side figure-thumbnail navigation panel.
    html = remove_elements_by_id(html, "figures-list")
    # #almSignposts: Save / Citation / Views / Shares metrics widget
    # that renders above the "Open Access Peer-reviewed" spec start
    # anchor. The widget's first number (e.g. "75" Saves) became the
    # first visible text at T=76, violating the start-anchor rule.
    html = remove_elements_by_id(html, "almSignposts")
    # #figure-carousel-section: dedicated "Figures" thumbnail carousel at
    # the bottom of the article (duplicates the inline figures already
    # embedded in the main text). Remove the whole section heading +
    # carousel, not the individual in-text figures.
    html = remove_elements_by_id(html, "figure-carousel-section")

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        "main#main-content{"
        "float:none !important;display:block !important;"
        "width:100% !important;max-width:752px !important;"
        # PLOS's main stylesheet sets `min-width: 61.25rem` (~980 px) on
        # <main>, which forces the wrapper to overflow narrow viewports.
        # Zero it so the cap holds.
        "min-width:0 !important;"
        # Override margin including negative right margin some PLOS
        # stylesheets leave on <main>.
        # padding-bottom trimmed by 30 to compensate for a ~30 px
        # trailing margin-collapse gap between the last reference item
        # and the zero-height final-section-spacer at the bottom of
        # article-content. Target B = 56 px below the last rendered
        # text baseline.
        "margin:0 auto !important;padding:56px 16px 26px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # Collapse inner grid wrappers (.set-grid, .article-container)
        # so main-content's children fill the wrapper.
        ":root main#main-content .set-grid,"
        ":root main#main-content .article-container,"
        ":root main#main-content .article-content{"
        "display:block !important;float:none !important;"
        "width:auto !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important}"
        # Zero margin along the first-/last-descendant chain so
        # collapsed margins don't leak through main's padding, while
        # section titles deeper in the tree keep native margins.
        "main#main-content>*:first-child,"
        "main#main-content>*:first-child>*:first-child,"
        "main#main-content>*:first-child>*:first-child>*:first-child,"
        "main#main-content>*:first-child>*:first-child>*:first-child>*:first-child,"
        "main#main-content>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child,"
        "main#main-content>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child>*:first-child"
        "{margin-top:0 !important}"
        "main#main-content>*:last-child,"
        "main#main-content>*:last-child>*:last-child,"
        "main#main-content>*:last-child>*:last-child>*:last-child,"
        "main#main-content>*:last-child>*:last-child>*:last-child>*:last-child,"
        "main#main-content>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child,"
        "main#main-content>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child>*:last-child"
        "{margin-bottom:0 !important}"
        # Clamp descendants so fixed-width tables/figures don't overflow.
        "main#main-content *{max-width:100% !important;min-width:0 !important}"
        "main#main-content table{table-layout:fixed !important;"
        "width:100% !important}"
        # PLOS closes the article body with a decorative spacer div
        # (class=final-section-spacing, inline height ~357 px) that
        # pushes the bottom gap well past the wrapper padding. Zero it.
        "main#main-content .final-section-spacing{height:0 !important}"
        # `.classifications` sits first-visible inside article-meta
        # with margin-top:18 px, which collapses through the ancestor
        # chain and pushes T past target. The first-descendant-chain
        # selectors above don't reach it because script tags (hidden
        # but still DOM children) short-circuit `*:first-child` at the
        # title-block level. Zero classifications' top margin explicitly.
        "main#main-content .classifications{margin-top:0 !important}"
        # Hide (but keep in DOM) the left-side TOC + article-tabs list.
        # Text content inside these elements is picked up by
        # parse_main_text, so removal would break parity.
        "main#main-content #nav-article,"
        "main#main-content .article-tab-container,"
        "main#main-content ul.nav-tabs{display:none !important}"
        # almSignposts already removed from DOM above (not just hidden) —
        # keeping it via display:none would block the first-descendant
        # chain selectors from reaching the first-visible element.
        # HubSpot interactives top anchor — full-viewport fixed overlay.
        "#hs-web-interactives-top-anchor,"
        "#hs-interactives-modal-overlay{display:none !important}"
        # Figure images: a get_refs.py browser-script swaps `<img src>`
        # from `size=inline` (320 px wide) to `size=large` (~1500-2000 px
        # native), and `_plos_inline_figures` post_capture fills any that
        # the browser-side fetch missed. Force the large image to fill
        # the figure's caption width so figure aligns with caption.
        # The publisher's `.img-box` wrapper hard-caps width to 20rem
        # (320px); override to 100% so the wrapper fills the column,
        # then `width:100%` on the img makes it fill the wrapper.
        ":root main#main-content .figure .img-box{"
        "display:block !important;"
        "width:100% !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important}"
        ":root main#main-content .figure .img-box img,"
        ":root main#main-content .figure img{"
        "display:block !important;width:100% !important;"
        "height:auto !important;margin:0 auto !important}"
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
    date = (get_meta(html, "citation_date")
            or get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_online_date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    # PLOS sets citation_journal_abbrev to the same verbose title as
    # citation_journal_title (e.g. "PLOS Genetics"); the canonical
    # PubMed ISO abbreviation is not exposed in the HTML.
    journal = (get_meta(html, "citation_journal_abbrev")
               or get_meta(html, "citation_journal_title")
               or "")
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
    PLOS citation_author meta tags use 'Given Last' form; format_author_name
    (via parse_combined_name + format_name) handles the flip and particles.
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
    """Convert 'BA Kunz' (initials-first) to 'Kunz BA' via shared helpers."""
    return format_author_name(name)


def _parse_freeform_citation(text):
    """Parse a plain-text PLOS citation into structured fields.

    Two formats occur in PLOS references:
      - "Authors. Title. Journal. YYYY;V:FP-LP. pmid:N"
      - "Authors (YYYY) Title. Journal V: FP-LP."
    Returns dict {title, journal, year, volume, issue, pages, doi, authors}.
    """
    text = re.sub(r"\s+", " ", text).strip()
    year = ""
    pages = ""
    volume = ""
    issue = ""
    journal = ""
    title = ""
    authors_str = ""

    # Modern format: "YYYY;V:FP-LP" or "YYYY;V(I):FP-LP"
    m = re.search(
        r"\.\s*(\d{4})\s*;\s*(\d+)(?:\(([^)]+)\))?\s*:\s*([\w\u2013\u2014\-]+)\.?",
        text,
    )
    if m:
        year = m.group(1)
        volume = m.group(2)
        issue = m.group(3) or ""
        pages = m.group(4).replace("\u2013", "-").replace("\u2014", "-")
        prefix = text[: m.start()].strip(" .")
        parts = re.split(r"\.\s+(?=[A-Z])", prefix)
        if len(parts) >= 3:
            authors_str = parts[0]
            title = parts[1]
            journal = ". ".join(parts[2:]).strip().rstrip(".")
        elif len(parts) == 2:
            authors_str = parts[0]
            title = parts[1]
    else:
        # Older format: "Authors (YYYY) Title. Journal V: FP-LP."
        m = re.search(r"\((\d{4})\)", text)
        if m:
            year = m.group(1)
            authors_str = text[: m.start()].strip().rstrip(",")
            rest = text[m.end():].strip(" .")
            tm = re.match(r"(.+?)\.\s+(.+)", rest)
            if tm:
                title = tm.group(1).strip()
                jvp = tm.group(2).strip()
                vm = re.search(
                    r"^(.*?)\s+(\d+)(?:\(([^)]+)\))?\s*:\s*([\w\u2013\u2014\-]+)",
                    jvp,
                )
                if vm:
                    journal = vm.group(1).strip().rstrip(".,")
                    volume = vm.group(2)
                    issue = vm.group(3) or ""
                    pages = vm.group(4).replace("\u2013", "-").replace("\u2014", "-")
                else:
                    journal = jvp.rstrip(".")
            else:
                title = rest

    authors = []
    if authors_str:
        for raw in re.split(r",\s*", authors_str):
            raw = raw.strip()
            if not raw:
                continue
            authors.append(raw)

    doi = ""
    dm = re.search(r"10\.\d{4,}/\S+", text)
    if dm:
        doi = format_doi(dm.group(0).rstrip(".,"))

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "authors": authors,
    }


def _parse_structured_citation(content):
    """Parse a PLOS citation_reference meta tag with 'key=value;' pairs.

    PLOS uses a ';' delimiter (no trailing space). Field names are the
    older Crossref-style: citation_first_page, citation_last_page,
    citation_publication_date.
    """
    fields = {}
    author_parts = []
    for part in content.split(";"):
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
        return None

    authors = [_flip_initials_first(a) for a in author_parts if a]

    fp = fields.get("citation_first_page", "")
    lp = fields.get("citation_last_page", "")
    pages = f"{fp}-{lp}" if lp else fp

    journal = fields.get("citation_journal_title", "")
    journal = re.sub(r"\s+", " ", journal).strip().rstrip(".")

    return {
        "title": fields.get("citation_title", ""),
        "journal": journal,
        "year": fields.get("citation_publication_date", ""),
        "volume": fields.get("citation_volume", ""),
        "issue": "",
        "pages": pages,
        "doi": format_doi(fields.get("citation_doi", "")),
        "authors": authors,
    }


def _extract_list_dois(html):
    """Extract DOI from each <li id=refN> in <ol class=references>.

    Returns list of DOIs in <li> order. Used to enrich meta-tag-derived
    references with DOIs found only in the bibliography HTML.
    """
    m = re.search(r"<ol class=references>(.*?)</ol>", html, re.DOTALL)
    if not m:
        return []
    bib = m.group(1)
    starts = [rm.start() for rm in re.finditer(r"<li id=ref\d+", bib)]
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(bib)
        item = bib[s:e]
        doi = ""
        dm = re.search(r"data-doi=([^\s>]+)", item)
        if dm:
            doi = format_doi(unescape(dm.group(1)).rstrip("/"))
        else:
            dm = re.search(r"href=(https://doi\.org/[^\s>\"']+)", item)
            if dm:
                doi = unescape(dm.group(1))
        out.append(doi)
    return out


def _parse_references(html):
    """Extract the reference list.

    Primary source: citation_reference meta tags, which come in two formats
    (freeform text or 'key=value;' pairs). Enriched with DOIs parsed from
    the bibliography <ol class=references> HTML when the meta tag lacks one.
    """
    refs = []
    metas = re.findall(
        r'<meta[^>]*name=["\']?citation_reference["\']?[^>]*content="([^"]*)"',
        html,
    )
    list_dois = _extract_list_dois(html)

    for i, content in enumerate(metas):
        content = unescape(content)
        parsed = _parse_structured_citation(content) if "=" in content else None
        if parsed is None:
            parsed = _parse_freeform_citation(content)
        if not parsed["doi"] and i < len(list_dois) and list_dois[i]:
            parsed["doi"] = list_dois[i]
        refs.append({"": parsed})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _extract_article(html):
    """Return the <div class=article-container> content.

    Uses depth-aware matching because the container has nested <div>s.
    """
    m = re.search(r'<div[^>]*class=["\']?article-container["\']?[^>]*>', html)
    if not m:
        return html
    start = m.end()
    pos = start
    depth = 1
    while depth > 0 and pos < len(html):
        nxt_open = re.search(r"<div[\s>]", html[pos:])
        nxt_close = re.search(r"</div>", html[pos:])
        if nxt_close is None:
            break
        if nxt_open and nxt_open.start() < nxt_close.start():
            depth += 1
            pos += nxt_open.end()
        else:
            depth -= 1
            if depth == 0:
                return html[start:pos + nxt_close.start()]
            pos += nxt_close.end()
    return html[start:]


def _find_sections(article):
    """List (start, title) of top-level toc-sections with <h2> headings.

    Matches the anchor tags that PLOS places before each toc-section
    (abstract0, abstract1, s1..s5, ack, authcontrib, references).
    """
    entries = []
    for m in re.finditer(
        r'<a[^>]*id=(?:"([^"]+)"|\'([^\']+)\'|(\S+))'
        r'[^>]*\btitle=(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))',
        article,
    ):
        anchor_id = (m.group(1) or m.group(2) or m.group(3) or "").strip(" \"'")
        title = (m.group(4) or m.group(5) or m.group(6) or "").strip()
        # Older PLOS: s1..s5; newer PLOS: sec001..secNNN.
        if not re.fullmatch(
            r"abstract\d+|s\d+|sec\d+|ack|authcontrib|references",
            anchor_id,
        ):
            continue
        entries.append((m.start(), title or anchor_id))
    return entries


def _remove_div_by_id(html, pattern):
    """Remove <div> matching pattern, handling nested <div> tags."""
    while True:
        m = re.search(pattern, html)
        if not m:
            return html
        pos = m.end()
        depth = 1
        while depth > 0 and pos < len(html):
            nxt_open = re.search(r"<div[\s>]", html[pos:])
            nxt_close = re.search(r"</div>", html[pos:])
            if nxt_close is None:
                break
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                pos += nxt_open.end()
            else:
                depth -= 1
                pos += nxt_close.end()
        html = html[:m.start()] + html[pos:]


def _extract_plos_figures(html):
    """Replace <div class=figure> blocks with their caption text.

    PLOS inline figures contain:
      <div class=figure data-doi=...>
        <div class=img-box>...</div>
        <div class=figure-inline-download>...</div>
        <div class=figcaption><span>Figure N. </span>Short title</div>
        <p class=caption_target>...<p>Long description</p>
        <p class=caption_object><a>DOI URL</a></p>
      </div>
    The surrounding image/download chrome is dropped; only the title and
    long description survive.
    """
    def _process(block):
        parts = []
        cm = re.search(
            r'<div[^>]*class=["\']?figcaption["\']?[^>]*>(.*?)</div>',
            block, re.DOTALL,
        )
        if cm:
            text = re.sub(r"\s+", " ", strip_tags(cm.group(1)).strip())
            if text:
                parts.append(text)
        tm = re.search(
            r'<p[^>]*class=["\']?caption_target["\']?[^>]*>(.*?)'
            r'(?=<p[^>]*class=["\']?caption_object|$)',
            block, re.DOTALL,
        )
        if tm:
            text = re.sub(r"\s+", " ", strip_tags(tm.group(1)).strip())
            if text:
                parts.append(text)
        return "\n\n" + "\n".join(parts) + "\n\n"

    out = []
    i = 0
    while True:
        m = re.search(r'<div\s+class=figure\s+data-doi=[^>]*>', html[i:])
        if not m:
            out.append(html[i:])
            break
        out.append(html[i:i + m.start()])
        start_abs = i + m.start()
        pos = i + m.end()
        depth = 1
        done = False
        while depth > 0 and pos < len(html):
            nxt_open = re.search(r"<div[\s>]", html[pos:])
            nxt_close = re.search(r"</div>", html[pos:])
            if nxt_close is None:
                break
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                pos += nxt_open.end()
            else:
                depth -= 1
                if depth == 0:
                    out.append(_process(html[start_abs:pos + nxt_close.start()]))
                    pos += nxt_close.end()
                    done = True
                    break
                pos += nxt_close.end()
        if not done:
            out.append(html[start_abs:pos])
        i = pos
    return "".join(out)


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/supporting information/extended data/source data/
        expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article-container -> drop figure-carousel-section
    -> slice body zones -> extract PLOS figures -> strip_common
    -> tags_to_text -> drop_noise. Appends keywords when present in the
    citation_keywords meta tag.
    """
    article = _extract_article(html)
    article = _remove_div_by_id(
        article, r'<div[^>]*id=["\']?figure-carousel-section["\']?[^>]*>'
    )

    sections = _find_sections(article)
    if not sections:
        return ""

    first_ref_idx = None
    for i, (_pos, title) in enumerate(sections):
        if title.lower() == "references":
            first_ref_idx = i
            break

    parts = []
    for i, (pos, title) in enumerate(sections):
        if title.lower() == "references":
            continue
        end = sections[i + 1][0] if i + 1 < len(sections) else len(article)
        if first_ref_idx is None or i < first_ref_idx:
            parts.append((pos, end))
        else:
            if _SUPP_RE.search(title):
                parts.append((pos, end))

    if not parts:
        return ""

    intro = article[: sections[0][0]]
    body_html = intro
    for s, e in parts:
        body_html += article[s:e]

    body_html = _extract_plos_figures(body_html)
    body_html = strip_common(body_html)
    body_html = extract_captions(body_html)
    text = tags_to_text(body_html)
    text = drop_noise(text, _NOISE)

    kw_str = get_meta(html, "citation_keywords") or get_meta(html, "keywords")
    if kw_str:
        keywords = [k.strip() for k in re.split(r"[,;]", kw_str) if k.strip()]
        if keywords:
            text += "\n\n## Keywords\n\n" + ", ".join(keywords)

    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse PLOS HTML into a papers/*.json-format dict."""
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
