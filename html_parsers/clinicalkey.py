"""ClinicalKey (clinicalkey.com) HTML parser."""

import base64
import re
import urllib.request
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "View full size",
    "Download PDF",
    "Cross Ref",
    "Open in a new tab",
    "Subscribe to RSS feed",
    "Image, AltText currently not available",
)

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

# Signatures of inline analytics/tracking scripts. Any <script>...</script>
# block whose content contains one of these strings is removed. These
# scripts are harmless to parsing but their JS string literals embed
# raw HTML fragments ('... placed in the <head> section', 'closing
# </body> tag', etc.) that break naive HTML renderers (e.g. VS Code's
# Preview) — the embedded strings appear as visible body text.
_TRACKING_SCRIPT_SIGNATURES = ("NREUM", "pageDataTracker", "_satellite")

# Figure container id attribute: <div class=c-cksc-inline-container
#   id=1-s2.0-<PII>-gr<N> ...>. Elsevier uses -gr<N> for regular figures
# and -ga<N> for graphical abstracts; both resolve to the same CDN
# URL pattern.
_FIG_ID_RE = re.compile(r"id=1-s2\.0-([\w-]+-(?:gr|ga)\d+)\b")

# Elsevier public CDN URL for figure images. The _lrg variant is the
# large (article-body) rendering; the non-_lrg is a thumbnail.
_ELSEVIER_IMG_URL = "https://ars.els-cdn.com/content/image/1-s2.0-{key}_lrg.jpg"


def _fetch_figure_image(fig_id):
    """Download an Elsevier figure image and return a base64 data URI,
    or None if the fetch fails. Publicly accessible — no auth needed.
    """
    url = _ELSEVIER_IMG_URL.format(key=fig_id)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "image/jpeg")
            data = resp.read()
    except Exception:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{ctype};base64,{b64}"


def _rehydrate_figures(html):
    """Inline figure images from the Elsevier public CDN.

    ClinicalKey saves figure <img> tags with src='data:,' — an empty
    placeholder — because Angular lazy-populates the real source only
    after OneTrust consent + scroll, neither of which fires during a
    single-file capture. The figure container's id (1-s2.0-<PII>-gr<N>)
    maps directly to a public URL on ars.els-cdn.com; fetch each and
    embed as a base64 data URI so the HTML file is self-contained.

    Only rehydrates when the <img> src is 'data:,' — if a previous
    capture already inlined the image, leaves it unchanged. Silently
    skips figures whose image fetch fails (network error, 404, etc.).
    """
    fig_ids = _FIG_ID_RE.findall(html)
    if not fig_ids:
        return html
    # Preserve original figure order; deduplicate in case a figure id
    # appears twice (inline + thumbnail reference).
    seen = set()
    uniq_ids = [f for f in fig_ids if not (f in seen or seen.add(f))]

    for fig_id in uniq_ids:
        data_uri = _fetch_figure_image(fig_id)
        if not data_uri:
            continue
        # Scope replacement to this figure's container: rewrite the
        # FIRST src=data:, that appears after the container's id.
        pattern = re.compile(
            r"(id=1-s2\.0-" + re.escape(fig_id) + r"\b[^>]*>(?:(?!id=1-s2\.0-).)*?<img\s+)src=data:,",
            re.DOTALL,
        )
        html = pattern.sub(
            lambda m, uri=data_uri: f'{m.group(1)}src="{uri}"',
            html,
            count=1,
        )
    return html


def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    - <header class="x-header ...">: site-wide sticky header with
      ClinicalKey logo, Study Resources, Clinical References, Search,
      Menu hamburger, Browse, Tools (top bars 1-3).
    - <div class="c-cksc-fixed-toolbar x-fixed-toolbar">: floating
      article toolbar with article title, CME credit, Download PDF
      (4th top bar).
    - reading-assistant: floating sidebar card.
    - tree-wrapper (c-ck-tree-wrapper): floating Outline sidebar.
    - <div data-testid=openable>: floating blue "Source" button + its
      side panel (rights/accessibility links).
    - Inline analytics scripts (NewRelic NREUM, Adobe DTM _satellite,
      pageDataTracker) — their JS string literals contain raw HTML
      fragments that confuse non-spec HTML renderers.
    """
    html = _remove_nested_element(
        html,
        r'<header class="x-header\b[^"]*"[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div class="c-cksc-fixed-toolbar x-fixed-toolbar"[^>]*>',
    )
    html = remove_elements_by_id(html, "reading-assistant")
    html = _remove_nested_element(
        html,
        r'<div data-testid=tree-wrapper\b[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div data-testid=openable\b[^>]*>',
    )
    # Strip inline analytics scripts by content signature. Non-greedy
    # match; relies on the fact that none of the targeted scripts
    # embed a literal `</script>` (they'd need `<\/script>` escaping).
    def _drop_if_tracking(m):
        content = m.group(1)
        for sig in _TRACKING_SCRIPT_SIGNATURES:
            if sig in content:
                return ""
        return m.group(0)
    html = re.sub(
        r'<script\b[^>]*>(.*?)</script>',
        _drop_if_tracking,
        html,
        flags=re.DOTALL,
    )
    # Inline figure images from Elsevier's public CDN. ClinicalKey
    # captures leave figure <img> tags with src='data:,' placeholders
    # because lazy-load / consent gating prevents the real src from
    # being set at capture time. The figure container ids give us the
    # CDN URL directly.
    html = _rehydrate_figures(html)
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_citation_line(html):
    """Parse the journal-citation line for journal, year, volume, pages.

    Line format: "Neurobiology of Pain, January 01, 2024, Volume 15, Article 100156, ..."
    or variations: "Journal Name, Month DD, YYYY, Volume N, Issue M, Pages A-B, ..."
    Returns dict with journal, year, volume, issue, pages.
    """
    out = {"journal": "", "year": "", "volume": "", "issue": "", "pages": ""}
    m = re.search(
        r'<p class="?c-cksc-content-journal-citation"?[^>]*>(.*?)</p>',
        html, re.DOTALL,
    )
    if not m:
        return out
    text = unescape(strip_tags(m.group(1))).strip()
    # First segment before first comma is journal
    segments = [s.strip() for s in text.split(",")]
    if segments:
        out["journal"] = segments[0]
    # Find year (4-digit)
    ym = re.search(r"\b(\d{4})\b", text)
    if ym:
        out["year"] = ym.group(1)
    # Volume N
    vm = re.search(r"Volume\s+(\S+)", text)
    if vm:
        out["volume"] = vm.group(1).rstrip(",")
    # Issue N
    im = re.search(r"Issue\s+(\S+)", text)
    if im:
        out["issue"] = im.group(1).rstrip(",")
    # Pages A-B or Pages A or Article N
    pm = re.search(r"(?:Pages?|Article)\s+(\S+(?:\s*[-\u2013]\s*\S+)?)", text)
    if pm:
        out["pages"] = pm.group(1).replace("\u2013", "-").rstrip(",")
    return out


def _parse_title(html):
    """Extract title from the header h1."""
    m = re.search(
        r'<h1[^>]*class="?c-cksc-content-header__title"?[^>]*>(.*?)</h1>',
        html, re.DOTALL,
    )
    if not m:
        return ""
    # Pull text from inner span (role=heading)
    inner = m.group(1)
    span_m = re.search(
        r'<span[^>]*role=heading[^>]*>(.*?)</span>', inner, re.DOTALL,
    )
    text = span_m.group(1) if span_m else inner
    return unescape(strip_tags(text)).strip()


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    ClinicalKey has no citation_* meta tags; fields are parsed from the
    rendered header. DOI is not exposed in ClinicalKey HTML and is left empty.
    """
    cite = _parse_citation_line(html)
    return {
        "title": _parse_title(html),
        "journal": cite["journal"],
        "year": cite["year"],
        "volume": cite["volume"],
        "issue": cite["issue"],
        "pages": cite["pages"],
        "doi": "",
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _format_ck_author(name):
    """Convert ClinicalKey 'Given Middle Last' to 'Last IN' via shared helpers."""
    return format_author_name(name.strip().rstrip(";"))


def _parse_authors(html):
    """Extract authors; affiliations are not exposed in ClinicalKey HTML."""
    m = re.search(
        r'<p[^>]*class="?c-cksc-content-journal-authors"?[^>]*>(.*?)</p>',
        html, re.DOTALL,
    )
    if not m:
        return []
    inner = m.group(1)
    authors = []
    for sm in re.finditer(r"<span[^>]*>(.*?)</span>", inner, re.DOTALL):
        raw = unescape(strip_tags(sm.group(1))).strip()
        name = _format_ck_author(raw)
        if name:
            authors.append({"author": name, "affiliation": []})
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_ref_entry(entry_html):
    """Parse a single <li> reference entry to a structured dict.

    Entry layout: <li ...><div>authors... title. journal year; vol: pp-pp.</div>
    <div><a href=DOI>Cross Ref</a></div></li>

    The first <div> contains the citation text as authors separated by
    commas, ending with ':', then the title, then publication info.
    """
    # First inner div holds the citation text
    first_div = re.search(r"<div[^>]*>(.*?)</div>", entry_html, re.DOTALL)
    cite_text = ""
    if first_div:
        cite_text = unescape(strip_tags(first_div.group(1))).strip()
    cite_text = re.sub(r"\s+", " ", cite_text)

    # DOI from second div (if any). Normalize http://dx.doi.org/... ->
    # https://doi.org/...
    doi = ""
    for dm in re.finditer(
        r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'\s>]+)',
        entry_html,
    ):
        doi = format_doi(unescape(dm.group(1)))
        break

    # Split authors and rest: authors block ends at ":" (e.g. "Umar S.:")
    authors = []
    rest = cite_text
    colon = cite_text.find(":")
    if colon > 0:
        auth_block = cite_text[:colon]
        rest = cite_text[colon + 1:].strip()
        for token in auth_block.split(","):
            t = token.strip()
            if t and t.lower() != "et al.":
                authors.append(t)

    # Title ends at the first period followed by space + capital or digit
    title = ""
    journal = year = volume = issue = pages = ""
    if rest:
        tm = re.match(r"(.+?)\.\s*(.+)$", rest, re.DOTALL)
        if tm:
            title = tm.group(1).strip()
            tail = tm.group(2).strip()
        else:
            title = rest
            tail = ""
        # Parse tail: "Journal YYYY; VOL: pp. PAGES" or "Journal YYYY; VOL:"
        # Extract year
        ym = re.search(r"\b(\d{4})\b", tail)
        if ym:
            year = ym.group(1)
        # Journal is text before year
        if year:
            jm = re.match(r"(.+?)\s+\d{4}", tail)
            if jm:
                journal = jm.group(1).strip().rstrip(".")
        # Volume: N or N (I)
        vm = re.search(
            r"\d{4}\s*;\s*(\d+)(?:\s*\((\d+)\))?\s*:", tail,
        )
        if vm:
            volume = vm.group(1)
            if vm.group(2):
                issue = vm.group(2)
        # Pages: after "pp." or last ":..."
        pm = re.search(r"pp\.\s*([A-Za-z0-9\-\u2013]+)", tail)
        if pm:
            pages = pm.group(1).replace("\u2013", "-")

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


def _parse_references(html):
    """Extract the reference list from <section class=c-ckc-bibliography>."""
    sec_m = re.search(
        r'<section[^>]*class="?c-ckc-bibliography"?[^>]*>', html,
    )
    if not sec_m:
        return []
    # Slice from section start to </section> at matching depth
    pos = sec_m.end()
    depth = 1
    end = len(html)
    while depth > 0 and pos < len(html):
        nxt_open = re.search(r"<section[\s>]", html[pos:])
        nxt_close = re.search(r"</section>", html[pos:])
        if nxt_close is None:
            break
        if nxt_open and nxt_open.start() < nxt_close.start():
            depth += 1
            pos += nxt_open.end()
        else:
            depth -= 1
            if depth == 0:
                end = pos + nxt_close.start()
                break
            pos += nxt_close.end()
    section = html[sec_m.end():end]

    # Each reference is a <li class=c-ckc-bibliography__item id=bNNNN>
    li_starts = [
        m.start() for m in re.finditer(
            r'<li[^>]*class="?c-ckc-bibliography__item"?[^>]*>', section,
        )
    ]
    refs = []
    for i, start in enumerate(li_starts):
        stop = li_starts[i + 1] if i + 1 < len(li_starts) else len(section)
        entry = section[start:stop]
        refs.append({"": _parse_ref_entry(entry)})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _slice_main_content(html):
    """Locate <div class=c-cksc-content> and return its content up to the
    references section.

    Matches the exact class 'c-cksc-content' (not c-cksc-content-player etc.)
    by requiring a terminator after the class name.
    """
    start_m = re.search(
        r'<div[^>]*class=(?:"c-cksc-content"|c-cksc-content)(?=[\s>])[^>]*>',
        html,
    )
    if not start_m:
        return ""
    start = start_m.end()
    ref_m = re.search(
        r'<section[^>]*class="?c-ckc-bibliography"?[^>]*>', html[start:],
    )
    if ref_m:
        return html[start:start + ref_m.start()]
    return html[start:]


def _replace_ck_figures(body_html):
    """Replace c-ckc-figure divs with their label + caption text."""
    def repl(m):
        inner = m.group(1)
        # figcaption (label, e.g. "Fig. 1")
        label = ""
        lm = re.search(
            r'<figcaption[^>]*>(.*?)</figcaption>', inner, re.DOTALL,
        )
        if lm:
            label = strip_tags(lm.group(1)).strip()
        # Caption paragraph
        cap = ""
        cm = re.search(
            r'<p[^>]*class="?c-ckc-figure__caption"?[^>]*>(.*?)</p>',
            inner, re.DOTALL,
        )
        if cm:
            # Keep inline HTML for sup/sub/i/b etc, tags_to_text will clean
            cap = cm.group(1).strip()
        text = (label + ". " if label else "") + cap
        return "\n\n" + text + "\n\n"
    return re.sub(
        r'<div[^>]*class="?c-ckc-figure"?[^>]*>(.*?)</div>\s*</div>',
        repl, body_html, flags=re.DOTALL,
    )


def _replace_ck_tables(body_html):
    """Expand c-ckc-table blocks so label + caption + table flow into text."""
    def repl(m):
        inner = m.group(1)
        # Replace the table label and caption paragraphs with plain text;
        # keep the <table> intact for tags_to_text.
        label = ""
        lm = re.search(
            r'<p[^>]*class="?c-ckc-table__label"?[^>]*>(.*?)</p>',
            inner, re.DOTALL,
        )
        if lm:
            label = strip_tags(lm.group(1)).strip()
        cap = ""
        cm = re.search(
            r'<p[^>]*class="?c-ckc-table__caption"?[^>]*>(.*?)</p>',
            inner, re.DOTALL,
        )
        if cm:
            cap = cm.group(1).strip()
        # Preserve the table element
        tm = re.search(r"<table[^>]*>.*?</table>", inner, re.DOTALL)
        table_html = tm.group(0) if tm else ""
        header = (label + ". " if label else "") + cap
        return "\n\n" + header + "\n" + table_html + "\n\n"
    return re.sub(
        r'<div[^>]*class="?c-ckc-table"?[^>]*>(.*?)</div>\s*</div>',
        repl, body_html, flags=re.DOTALL,
    )


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: appendices (c-ckc-appendices) are kept after references.
      - Remove all references sections.
    """
    body_html = _slice_main_content(html)
    if not body_html:
        return ""

    # Remove the inline <style> block that lives at the top of c-cksc-content
    body_html = re.sub(
        r"<style[^>]*>.*?</style>", "", body_html, flags=re.DOTALL,
    )

    # Merge split section label + title: "<h2 class=c-ckc-section__label>2</h2>
    # <div class=c-ckc-section-title><h2>Methods</h2></div>" -> "<h2>2. Methods</h2>"
    def _merge_label(m):
        label = strip_tags(m.group(1)).strip()
        title = strip_tags(m.group(2)).strip()
        return f"<h2>{label}. {title}</h2>"
    body_html = re.sub(
        r'<h2[^>]*class="?c-ckc-section__label"?[^>]*>(.*?)</h2>\s*'
        r'<div[^>]*class="?c-ckc-section-title"?[^>]*>\s*'
        r'<h2[^>]*>(.*?)</h2>\s*</div>',
        _merge_label, body_html, flags=re.DOTALL,
    )
    # Same for h3 labels + h3 titles
    def _merge_label3(m):
        label = strip_tags(m.group(1)).strip()
        title = strip_tags(m.group(2)).strip()
        return f"<h3>{label}. {title}</h3>"
    body_html = re.sub(
        r'<h3[^>]*class="?c-ckc-section__label"?[^>]*>(.*?)</h3>\s*'
        r'<div[^>]*class="?c-ckc-section-title"?[^>]*>\s*'
        r'<h3[^>]*>(.*?)</h3>\s*</div>',
        _merge_label3, body_html, flags=re.DOTALL,
    )

    # Replace figures and tables with their captions (+ table content)
    body_html = _replace_ck_figures(body_html)
    body_html = _replace_ck_tables(body_html)

    # Extract table-zoom, toolbar buttons, and SVGs via strip_common
    body_html = strip_common(body_html)

    # Remove UI toolbar chrome (<button>) that leaks "View full size", etc.
    body_html = re.sub(
        r"<button[^>]*>.*?</button>", "", body_html, flags=re.DOTALL,
    )

    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse ClinicalKey HTML into a papers/*.json-format dict."""
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
