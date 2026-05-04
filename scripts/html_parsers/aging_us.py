"""Aging (aging-us.com) HTML parser — rewritten for the 2024+ site redesign.

Post-redesign structure:
  - No `citation_*` meta tags. Metadata sourced from a schema.org
    JSON-LD block (`<script type=application/ld+json>`).
  - Article column wrapped in a Tailwind `<div class="flex-1
    min-w-[400px]">` with no semantic class.
  - Title lives in `<h1 id=article-title>`.
  - Author metadata in `<span class=author>Display Name</span>` siblings.
  - "Research Paper | Volume X, Issue Y | pp A-B" in a flex-wrap row.
  - Abstract in `<h3>Abstract</h3>` + sibling `<div>...<p>...</p></div>`.
  - Body in `<div class="article-text mt-6 text-justify">` containing
    `<div class=section-container>` per section; each section starts
    with `<h2 id=<slug> class="... article-header-1 ...">`.
  - References in `<h2>References</h2>` + `<ul class=space-y-3>` with
    `<li id=Rn ...>` items.
"""

import json
import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_doi,
    format_name,
    neutralize_media_queries,
    strip_common,
    strip_tags,
    tags_to_text,
)

_NOISE = (
    "[PubMed]",
    "Open in a new tab",
)


# ---------------------------------------------------------------------------
# JSON-LD + HTML shared helpers
# ---------------------------------------------------------------------------

def _load_jsonld(html):
    """Return the MedicalScholarlyArticle node from the schema.org JSON-LD.

    Aging-us injects one `<script type=application/ld+json>` block with a
    `@graph` containing a MedicalScholarlyArticle entry.
    """
    for m in re.finditer(
        r'<script[^>]*type=["\']?application/ld\+json["\']?[^>]*>(.*?)</script>',
        html, re.DOTALL,
    ):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        graph = data.get("@graph") if isinstance(data, dict) else None
        if graph is None and isinstance(data, dict):
            graph = [data]
        for node in graph or []:
            t = node.get("@type") if isinstance(node, dict) else None
            if t and "Article" in (t if isinstance(t, str) else " ".join(t)):
                return node
    return {}


def _extract_pub_line(html):
    """Extract "Research Paper | Volume X, Issue Y | pp A-B" metadata row.

    Returns dict with keys: 'pub_type', 'volume', 'issue', 'pages'.
    """
    out = {"pub_type": "", "volume": "", "issue": "", "pages": ""}
    # Volume / Issue — match "Volume N, Issue M"
    m = re.search(r"Volume\s+(\d+)\s*,\s*Issue\s+(\d+)", html)
    if m:
        out["volume"] = m.group(1)
        out["issue"] = m.group(2)
    # Pages — match "pp 274—284" or "pp 274-284"
    m = re.search(r"pp\s+(\d+)\s*[—\-]\s*(\d+)", html)
    if m:
        out["pages"] = f"{m.group(1)}-{m.group(2)}"
    return out


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize aging-us HTML to a single centered text column.

    Chrome stripped (Step 3):
      - Site `<header>` / `<footer>`.
      - Left sidebar column (`aside` tag or `.space-y-6.ml-2` container
        holding journal banners / ad art / EPIC awards).

    Reading column (Step 4): `div[class*="min-w-[400px]"]` — the Tailwind
    article-column wrapper that contains the metadata row, h1 title,
    author list, abstract, body (`.article-text`), and references.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # Step 3 — strip chrome.
    for _ in range(5):
        before = html
        html = _remove_nested_element(html, r"<header\b[^>]*>")
        if html == before:
            break
    for _ in range(5):
        before = html
        html = _remove_nested_element(html, r"<footer\b[^>]*>")
        if html == before:
            break
    # Left sidebar column: a flex sibling of the article column.
    # Identify by an `<aside>` with content, OR a <div> whose class
    # contains `space-y-6 ml-2` (journal banner / award column).
    for _ in range(4):
        before = html
        html = _remove_nested_element(html, r"<aside\b[^>]*>")
        if html == before:
            break
    for _ in range(4):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass="[^"]*\bspace-y-6\b[^"]*\bml-2\b[^"]*"[^>]*>',
        )
        if html == before:
            break

    # Steps 2 + 4 — layout freeze and reading-column cap.
    # Wrapper: div[class*="min-w-[400px]"] — Tailwind arbitrary value
    # used uniquely on the article column. The brackets are literal
    # characters inside the quoted attribute selector value.
    override = (
        "<style>"
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Collapse the outer flex / max-width containers that shape the
        # site's full-viewport layout.
        'body>div,body>div>div,[class*="max-w-["]{'
        "display:block !important;width:auto !important;"
        "max-width:100% !important;min-width:0 !important;"
        "margin:0 auto !important;padding:0 !important;"
        "float:none !important;background:#fff !important;"
        "box-shadow:none !important}"
        # Capped reading-column wrapper.
        ':root div[class*="min-w-[400px]"]{'
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        ':root div[class*="min-w-[400px]"] *{'
        "max-width:100% !important;min-width:0 !important;"
        "box-sizing:border-box !important}"
        # Strip the utility-class inner padding that natively inset the
        # content column by 15-30 px on each side. Zero only the axes
        # the matched utility actually controls (`pt-[30px]` → padding-
        # top; `px-[15px]` → padding-left/right) so a `pb-[15px]` on the
        # same div keeps its native bottom padding.
        ':root div[class*="min-w-[400px]"] > div[class*="pt-[30px]"]{'
        "padding-top:0 !important}"
        ':root div[class*="min-w-[400px]"] > div[class*="px-[15px]"]{'
        "padding-left:0 !important;padding-right:0 !important}"
        # Direct-child first-/last-child margin zero.
        ':root div[class*="min-w-[400px]"] > *:first-child{'
        "margin-top:0 !important;padding-top:0 !important}"
        ':root div[class*="min-w-[400px]"] > *:last-child{'
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # Tables: fixed layout so wide cells don't push past the wrapper.
        ':root div[class*="min-w-[400px]"] table{'
        "width:100% !important;max-width:100% !important;"
        "table-layout:fixed !important}"
        # Figures: aging-us renders each figure inside a Tailwind card
        #   <div class="my-8 bg-white shadow-... p-8">
        #     <a data-figure-id=F<N> href=<sub-page>/figure/F<N>/large/>
        #       <img alt class="max-w-full mx-auto border ...">
        # The img has Tailwind `max-w-full mx-auto`, which lets it scale
        # DOWN to container but not UP past its intrinsic size — at
        # narrow column the image renders at native pixel width
        # (~400-700 px), narrower than the 720-px column. Force block +
        # 100% width above the caption. The high-res JPEG lives on a
        # sub-page (`.../figure/F<N>/large/`) which requires sub-page
        # traversal to inline (not a single-URL swap) — deferred to a
        # post_capture pass in get_refs.py; this CSS just handles the
        # visible layout.
        ':root div[class*="min-w-[400px]"] a[data-figure-id]{'
        "display:block !important;width:auto !important;"
        "margin:0 !important;padding:0 !important}"
        ':root div[class*="min-w-[400px]"] a[data-figure-id] > img{'
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
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
    """Extract bundled metadata via JSON-LD + HTML fallbacks."""
    jld = _load_jsonld(html)

    title = (jld.get("headline") or "").strip()
    if not title:
        m = re.search(r'<h1[^>]*id=["\']?article-title["\']?[^>]*>([^<]+)', html)
        if m:
            title = unescape(m.group(1)).strip()

    # datePublished: "2010-04-30T05:00:00.000Z"
    year = ""
    dp = jld.get("datePublished") or ""
    if dp:
        m = re.search(r"(\d{4})", dp)
        if m:
            year = m.group(1)

    # DOI in sameAs, may be "https://doi.org/10.18632/aging.100141"
    doi = ""
    same = jld.get("sameAs") or ""
    if isinstance(same, list):
        same = next((s for s in same if "doi.org" in s), "")
    if same and "doi.org" in same:
        doi = format_doi(same)

    # Journal from HTML `<title>` tag or site chrome. Fallback to "Aging".
    journal = ""
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        t = unescape(m.group(1)).strip()
        # Format "Article title | Aging"
        if " | " in t:
            journal = t.split(" | ")[-1].strip()

    pub = _extract_pub_line(html)
    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": pub["volume"],
        "issue": pub["issue"],
        "pages": pub["pages"],
        "doi": doi,
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _format_lastfirst(name):
    """Convert 'Last, First Middle' to 'Last IN' via the shared formatter.

    JSON-LD stores authors as {"name": "Kusumoto-Matsuo, Rika"}. Split
    on the first comma; first part is surname, second is given name.
    """
    if "," not in name:
        # Fallback: treat whole string as "Given Last" (already handled by
        # format_author_name logic elsewhere).
        return name.strip()
    last, given = name.split(",", 1)
    return format_name(given.strip(), last.strip())


def _parse_authors(html):
    """Extract authors with affiliations.

    Author names come from JSON-LD `author[]`. Affiliations are stored
    in a numbered list: `<ul class="mt-2 space-y-0.5 ml-3"><li><sup>N</sup>
    <span>Affiliation text</span></li>…</ul>`. Each author span contains
    one or more `<sup>N,</sup>` references to the numbered affiliations.
    """
    jld = _load_jsonld(html)
    authors = []
    for a in jld.get("author") or []:
        name = a.get("name") if isinstance(a, dict) else ""
        if not name:
            continue
        authors.append({
            "author": _format_lastfirst(name),
            "affiliation": [],
        })

    # Build affiliation-number → text map from the numbered list.
    # Anchor on "mt-2 space-y-0.5 ml-3" which is unique to this list.
    aff_map = {}
    ul_m = re.search(
        r'<ul\s+class="[^"]*\bmt-2\b[^"]*\bspace-y-0\.5\b[^"]*\bml-3\b[^"]*"[^>]*>(.*?)</ul>',
        html, re.DOTALL,
    )
    if ul_m:
        ul_html = ul_m.group(1)
        for li in re.finditer(
            r'<li[^>]*>.*?<sup[^>]*>([^<]+)</sup>\s*<span[^>]*>([^<]+)</span>',
            ul_html, re.DOTALL,
        ):
            num = li.group(1).strip().rstrip(",.*").strip()
            text = unescape(li.group(2)).strip().rstrip(",.;").strip()
            if num and text:
                aff_map[num] = text

    # Map authors to affiliation numbers via `<sup>N,</sup>` superscripts
    # inside each `<span class=author>` block. Each author block contains
    # nested spans, so match opening `<span class=author>` and then walk
    # forward with a depth counter to find the matching `</span>`.
    author_blocks = []
    for open_m in re.finditer(
        r'<span\s+class=["\']?author["\']?[^>]*>',
        html,
    ):
        pos = open_m.end()
        depth = 1
        while depth > 0 and pos < len(html):
            no = re.search(r'<span\b[^>]*>', html[pos:])
            nc = re.search(r'</span>', html[pos:])
            if not nc:
                break
            if no and no.start() < nc.start():
                depth += 1
                pos += no.end()
            else:
                depth -= 1
                if depth == 0:
                    author_blocks.append(html[open_m.end():pos + nc.start()])
                    break
                pos += nc.end()
    for i, block in enumerate(author_blocks):
        if i >= len(authors):
            break
        nums = []
        for sm in re.finditer(r'<sup[^>]*>([^<]+)</sup>', block):
            raw_num = sm.group(1).strip()
            # "1," "7" "*" etc. — extract digit(s) only
            for n in re.findall(r"\d+", raw_num):
                if n not in nums:
                    nums.append(n)
        affs = [aff_map[n] for n in nums if n in aff_map]
        authors[i]["affiliation"] = affs
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

_REF_LI_RE = re.compile(
    r'<li\s+id=["\']?R\d+["\']?[^>]*>(.*?)(?=<li\s+id=["\']?R\d+["\']?|</ul>)',
    re.DOTALL,
)


def _parse_ref_text(text):
    """Parse one aging-us reference's plain text into structured fields.

    Observed format after `strip_tags`:
      "Author1, Author2 and Author3. Title. Journal. Year; Volume:FP-LP."
    or with issue:
      "... Year; Volume(Issue):FP-LP."
    Some older entries omit the period after author list.
    """
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\[\s*PubMed\s*\]\.?", "", text).strip()

    result = {
        "title": "", "journal": "", "year": "", "volume": "", "issue": "",
        "pages": "", "doi": "", "authors": [],
    }

    m = re.search(
        r"(\d{4})\s*;\s*(\w+)"
        r"(?:\s*\(([^)]+)\))?"
        r"\s*:\s*([\w\d\-–]+)",
        text,
    )
    if m:
        result["year"] = m.group(1)
        result["volume"] = m.group(2)
        if m.group(3):
            result["issue"] = m.group(3)
        result["pages"] = m.group(4).replace("–", "-")

    if m:
        pre = text[:m.start()].strip().rstrip(".").strip()
        parts = [p.strip() for p in pre.split(". ") if p.strip()]
        author_text = ""
        if parts:
            result["journal"] = parts[-1].rstrip(".").strip()
            rest = parts[:-1]
            if rest:
                result["title"] = rest[-1].strip()
                author_text = ". ".join(rest[:-1]).strip()
        else:
            author_text = pre

        # Older entries have no period between author and title
        # (e.g. "Martin GM Genetics and aging..."). Detect a leading
        # "Lastname Initials " pattern.
        if not author_text and result["title"]:
            am = re.match(
                r"([A-Z][A-Za-zÀ-ÿ\-']+)\s+([A-Z]{1,4})\s+([A-Z].+)$",
                result["title"],
            )
            if am:
                author_text = f"{am.group(1)} {am.group(2)}"
                result["title"] = am.group(3).strip()

        if author_text:
            author_text = re.sub(r",?\s+and\s+", ", ", author_text)
            for a in author_text.split(","):
                a = a.strip().rstrip(".").strip()
                if not a or a.lower() == "et al":
                    continue
                result["authors"].append(a)

    return result


def _parse_references(html):
    """Extract reference list from <h2>References</h2> + <ul class=space-y-3>."""
    # Locate the References section: <h2...>References</h2>
    h2 = re.search(r'<h2[^>]*>\s*References\s*</h2>', html, re.IGNORECASE)
    if not h2:
        return []
    section = html[h2.end():]
    # Find the enclosing <ul>
    ul_m = re.search(r'<ul[^>]*>', section)
    if not ul_m:
        return []
    # Scope to matching </ul>
    pos = ul_m.end()
    depth = 1
    end = len(section)
    while depth > 0 and pos < len(section):
        no = re.search(r'<ul[\s>]', section[pos:])
        nc = re.search(r'</ul>', section[pos:])
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
    refs_html = section[ul_m.end():end]

    refs = []
    for li_m in _REF_LI_RE.finditer(refs_html):
        entry = li_m.group(1)
        # DOI from any href
        doi = ""
        dm = re.search(r'https?://(?:dx\.)?doi\.org/([^\s"\'>]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1)))
        # Strip leading "<b>N.</b>" label
        inner = re.sub(r"^\s*<b>[^<]*</b>\s*", "", entry)
        text = strip_tags(inner)
        parsed = _parse_ref_text(text)
        parsed["doi"] = doi or parsed.get("doi", "")
        refs.append({"": parsed})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract text from <h3>Abstract</h3> + sibling <div>."""
    m = re.search(r'<h3[^>]*>\s*Abstract\s*</h3>', html, re.IGNORECASE)
    if not m:
        return ""
    # Take the next <div>...</div> after the heading
    after = html[m.end():]
    dm = re.search(r'<div[^>]*>', after)
    if not dm:
        return ""
    pos = dm.end()
    depth = 1
    end = len(after)
    while depth > 0 and pos < len(after):
        no = re.search(r'<div[\s>]', after[pos:])
        nc = re.search(r'</div>', after[pos:])
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
    return strip_tags(after[dm.end():end]).strip()


def _parse_main_text(html):
    """Abstract + body sections (Introduction → before References)."""
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")

    body_m = re.search(
        r'<div\s+class="?article-text[^"]*"?[^>]*>',
        html,
    )
    if body_m:
        pos = body_m.end()
        depth = 1
        end = len(html)
        while depth > 0 and pos < len(html):
            no = re.search(r'<div[\s>]', html[pos:])
            nc = re.search(r'</div>', html[pos:])
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
        body_html = html[body_m.end():end]
        body_html = extract_captions(body_html)
        body_html = strip_common(body_html)
        text = tags_to_text(body_html).strip()
        if text:
            parts.append(text)

    result = "\n\n".join(parts)
    return drop_noise(result, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse aging-us HTML into a papers/*.json-format dict."""
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
