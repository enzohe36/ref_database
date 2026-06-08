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

_OVERRIDE_CSS = """<style>
html, body { background: #ffffff !important; }
body {
    max-width: 752px !important;
    width: auto !important;
    margin: 0 auto !important;
    padding: 0 16px !important;
    box-sizing: border-box !important;
    background: #ffffff !important;
}
/* PLOS pegs the article column at 980 px via:
       main, #pagehdr      { width: 100%; min-width: 61.25rem;
                             max-width: 61.25rem; margin: 0 auto; }
       .set-grid           { width: 61.25rem; }
   The min-width is what forces the column past the body cap — clear
   it (and width / max-width) so each wrapper shrinks to its parent.
   Same for the page footer's .row grid wrapper. */
:root main,
:root #main-content,
:root #pagehdr,
:root .set-grid,
:root .plos-row,
:root #pageftr .row,
:root .row,
:root .title-block,
:root .article-body {
    max-width: 100% !important;
    width: auto !important;
    min-width: 0 !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
    box-sizing: border-box !important;
}
/* The article column is `.article-content` floated left with a 12.5rem
   `.article-aside` floated right (download / share / related). Drop the
   float layout so the article column fills the body cap and the aside
   stacks below at narrow widths. The title-block children
   (`.article-title-etc` / `.title-authors` / `.classifications`) ship
   the same fixed `width: 47.1875rem` (755 px) — flatten those too. */
:root .article-content,
:root .article-container,
:root #artText,
:root .article-aside,
:root .article-title-etc,
:root .title-authors,
:root .classifications,
:root #author-list,
:root #artTitle,
:root .date-doi,
:root .center {
    width: auto !important;
    max-width: 100% !important;
    min-width: 0 !important;
    float: none !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    box-sizing: border-box !important;
}
/* Embedded figshare data viewer ships its own viewer at fixed width
   (550 px) that overflows the column at vw < 550. Cap it. */
figshare-widget,
figshare-widget * {
    max-width: 100% !important;
    box-sizing: border-box !important;
}
figshare-widget {
    display: block !important;
    width: 100% !important;
}
/* Step 11 — `.carousel-wrapper` (the figure-carousel above the article)
   ships `overflow: hidden; width: 32rem (512px)` and a child
   `.slider { width: 99999px }`. At vw < 528 (32rem + body padding) the
   wrapper escapes the body cap; cap it to fit. */
:root .carousel-wrapper {
    width: auto !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
}
/* Step 8 — figure layout: image above caption, image width-aligned with
   caption, 8 px gap. PLOS markup is
       <div class=figure data-doi=...>
         <div class=img-box> <a> <img alt=thumbnail> </a> </div>
         <div class=figure-inline-download> ... </div>
         <div class=figcaption> ... </div>
         <p class=caption_target> ... </p>
   The img-box ships fixed pixel widths from the thumbnail (320 px) — let
   it expand to the column. */
:root .figure,
:root .img-box,
:root .img-box a {
    display: block !important;
    width: 100% !important;
    max-width: 100% !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    box-sizing: border-box !important;
}
:root .img-box img {
    display: block !important;
    width: 100% !important;
    height: auto !important;
    max-width: 100% !important;
    margin: 0 0 8px 0 !important;
}
:root .figure .figcaption,
:root .figure .caption_target {
    display: block !important;
    width: 100% !important;
    margin: 4px 0 !important;
}
/* Tables ship a fixed pixel width. Cap to the column. */
:root table,
:root .table-wrap {
    max-width: 100% !important;
    width: auto !important;
    table-layout: auto !important;
    box-sizing: border-box !important;
}
</style>"""


def remove_banners(html):
    """Apply Phase 2 layout rules for journals.plos.org.

    Step 1: cap body width at 752 px, center, neutralize @media so the
            publisher's narrow CSS branch always applies.
    Step 2: remove the `#cookie-consent` banner and the
            `.reveal-modal-bg` modal backdrop sibling.
    Step 3: sticky elements — `#hs-web-interactives-top-anchor` (the
            HubSpot CTA injection anchor wrapper, `position: fixed`
            top:0 full-viewport even when no CTA is rendered) and
            `#floatTitleTop` (the publisher's scroll-triggered title
            bar, `position: fixed` top:0 full-viewport-wide that pops
            in once the user scrolls past the article header).
    Step 5: ad blocks — `<div class=advertisement ...>` placeholders
            inside the page header.
    """
    html = neutralize_media_queries(html)

    # Step 2 — cookie consent banner and the paired modal backdrop.
    # `.reveal-modal-bg` is the dark page-level overlay that ships with
    # the consent dialog; it sits as a sibling of `#cookie-consent` at
    # the top of <body>.
    html = remove_elements_by_id(html, "cookie-consent")
    html = remove_elements_by_selector(html, "reveal-modal-bg")

    # Step 3 — HubSpot CTA injection anchors. The top-anchor element is
    # `position: fixed` full-viewport (1200x900 at vw=1200) waiting for
    # HubSpot to inject a floating call-to-action. Remove the entire
    # family — top/bottom push/anchor + the floating-container with its
    # four corner anchors — so none of them paint over the article.
    html = remove_elements_by_id(
        html,
        "hs-web-interactives-top-push-anchor",
        "hs-web-interactives-top-anchor",
        "hs-web-interactives-bottom-anchor",
        "hs-web-interactives-floating-container",
        "floatTitleTop",
    )

    # Step 5 — ad placeholders. PLOS ships a 728x90 ad slot in the page
    # header and a 160x600 ad slot in the article aside, both as
    # `<div class=advertisement id=div-gpt-ad-...>` (unquoted attrs).
    # The aside also wraps the 160x600 ad in a `<div class=skyscraper-container>`
    # which keeps reserving 624 px of vertical chrome even after the
    # inner ad div is removed; drop the wrapper too.
    while True:
        prev = html
        html = _remove_nested_element(
            html, r'<div[^>]*\bclass=(?:"advertisement"|advertisement\b)[^>]*>',
        )
        if html == prev:
            break
    while True:
        prev = html
        html = _remove_nested_element(
            html,
            r'<div[^>]*\bclass=(?:"skyscraper-container"|skyscraper-container\b)[^>]*>',
        )
        if html == prev:
            break

    # Step 10 cleanup — `<div class=final-section-spacing style=height:NNNpx>`
    # is a JS-injected spacer that pads the article-content column to
    # match the (taller) article-aside sidebar in the publisher's
    # 2-column desktop layout. With the aside un-floated and stacked
    # below the article (Step 1), the spacer becomes a 365 px empty
    # band between the references and the aside; remove it.
    while True:
        prev = html
        html = _remove_nested_element(
            html,
            r'<div[^>]*\bclass=(?:"final-section-spacing"|final-section-spacing\b)[^>]*>',
        )
        if html == prev:
            break

    if "</head>" in html:
        html = html.replace("</head>", _OVERRIDE_CSS + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", _OVERRIDE_CSS + r"\1", html, count=1)
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
