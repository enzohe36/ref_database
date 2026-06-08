"""American Chemical Society (acs) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    neutralize_media_queries,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new window",
    "Click to copy section link",
    "Section link copied!",
    "View Author Information",
    "High Resolution Image",
    "Download MS PowerPoint Slide",
    "Download",
    "Google Scholar",
    "View",
)

# Top-level NLM_sec openings. Newer ACS papers include an id=secN on the
# wrapper; older papers (e.g. 2001) omit it, so match either form but only
# level_1 (top-level) sections.
_ALL_SECTION_RE = re.compile(
    r'<div\s+(?:id=sec\d+\s+)?class="NLM_sec\s+NLM_sec_level_1"[^>]*>', re.DOTALL
)

# Supplementary section patterns (kept after first references). ACS uses
# "Supporting Information" as the canonical heading for supplementary
# materials; the generic "supplement" token doesn't match it.
_SUPP_RE = re.compile(
    r"supplement|supporting information|extended data|source data|"
    r"expanded view|appendix",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for pubs.acs.org.

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies.
    Step 2: no native cookie banner ships in the captured DOM (the
            "Manage Cookies" link in the footer opens the Osano drawer
            on demand, but the drawer itself is not in the static
            snapshot).
    Step 3: sticky/fixed elements — the page-wide top `<header
            class=header>` (position: fixed, 130 px tall, lives at
            top:0); the off-canvas right-side `.recentlyViewed` drawer
            (1200 px wide, parked off-screen until activated); and the
            `.w-slide` / `.w-slide_head` slide-in modal pair (also
            off-screen until activated). All four are removed in DOM so
            the page no longer reserves their space.
    Step 5: ad blocks — `<div class=advertisement>` (and its inner
            `advertisement-link`) wrappers ship empty in the static
            snapshot but reserve vertical space, and the `<li
            class="sponsoredContent show-recommended__item">` slot in
            the post-article recommendations row is a sponsored
            placeholder. All removed.
    Step 8: figures — cap `.article__inlineFigure`, its inner
            `.figure-viewer__trigger` button and `img.inline-fig` to
            the column width; image above caption with 12 px gap.
            `.NLM_table-wrap` gets `overflow-x:auto` so wide native
            tables scroll within the column instead of overflowing.
    Step 9: expand the `#affiliations-popup` drawer (publisher hides
            with `display:none` until the user clicks "View Author
            Information") so each author's affiliation renders inline
            beneath the author block.
    Step 10: zero out `main.content`'s `margin-top:77px` (publisher
             reserves it for the fixed header removed in Step 3).
    Step 11: drop the `.article-grid` `padding-inline:16px` and force
             a single full-width grid column; cap `.article__left-side`
             so it shrinks to the body cap at every viewport.
    """
    html = neutralize_media_queries(html)

    # Step 3 — page-wide fixed header (always-on top chrome). Use the
    # full opening-tag pattern so we don't false-match other <header>s
    # (the article body has its own <header class=article__tags__header>).
    while True:
        new = _remove_nested_element(
            html,
            r'<header\s+class="?header header_article_inactive[^"]*"?[^>]*>',
        )
        if new == html:
            break
        html = new
    # Step 3 — off-canvas drawers (faux-sticky / fixed at off-screen x).
    while True:
        new = _remove_nested_element(
            html,
            r'<div\s+class=("[^"]*\brecentlyViewed\b[^"]*"|recentlyViewed\b)[^>]*>',
        )
        if new == html:
            break
        html = new
    for cls in ("article__w-slide", "w-slide_head"):
        while True:
            prev = html
            html = remove_elements_by_selector(html, cls)
            if html == prev:
                break

    # Step 5 — ad slots. Empty in static capture but reserve vertical
    # space, plus the sponsored-content slot in the recommendations row.
    for cls in ("advertisement", "sponsoredContent"):
        while True:
            prev = html
            html = remove_elements_by_selector(html, cls)
            if html == prev:
                break
    # The advertisement wrapper inside ACS pages is `<div class=advertisement>`
    # (unquoted) — remove_elements_by_selector matches double-quoted classes
    # only. Use _remove_nested_element to catch the unquoted form.
    while True:
        new = _remove_nested_element(
            html,
            r'<div\s+class=advertisement\b[^>]*>',
        )
        if new == html:
            break
        html = new
    while True:
        new = _remove_nested_element(
            html,
            r'<li\s+class="sponsoredContent[^"]*"[^>]*>',
        )
        if new == html:
            break
        html = new

    override = (
        "<style>"
        "html{margin:0!important;padding:0!important;"
        "background:#fff!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "min-width:0!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "background:#fff!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # Step 10 — remove the 77 px margin-top the publisher reserves
        # on `main.content` for the fixed `<header>` we deleted in Step 3.
        # Without this override the article column starts ~77 px below
        # the page top and `scan_gaps` reports a leading empty band.
        "main.content{margin-top:0!important;}"
        # Step 11 — `.article-grid` ships `padding-inline:16px` on top of
        # the body's own 16 px gutter (32 px total each side). Drop the
        # grid's padding and force a single full-width grid column so the
        # article column shrinks with the viewport at narrow widths
        # instead of overflowing right.
        ".article-grid{padding-inline:0!important;padding-left:0!important;"
        "padding-right:0!important;column-gap:0!important;"
        "grid-template-columns:1fr!important;"
        "grid-template-areas:\"left\"!important;"
        "max-width:100%!important;width:100%!important;}"
        ".article__left-side{width:auto!important;max-width:100%!important;"
        "min-width:0!important;}"
        # Step 8 — figures: cap image/figure/button to the column width
        # so wide native images shrink to fit; image renders above
        # caption with 12 px gap. ACS markup is
        #   <figure class=article__inlineFigure>
        #     <button class=figure-viewer__trigger>
        #       <img class="inline-fig internalNav">
        #     </button>
        #     <figcaption><div class=hlFld-FigureCaption><p>caption</p></div>
        ".article__inlineFigure"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;"
        "margin-left:0!important;margin-right:0!important;"
        "box-sizing:border-box!important;}"
        ".article__inlineFigure .figure-viewer__trigger"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;padding:0!important;"
        "border:none!important;background:transparent!important;}"
        ".article__inlineFigure img.inline-fig"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 12px 0!important;"
        "box-sizing:border-box!important;}"
        ".article__inlineFigure figcaption"
        "{display:block!important;width:100%!important;"
        "margin:0!important;}"
        # Step 8 (continued) — table-wrap inner tables ship native pixel
        # widths from the publisher PDF that overflow the narrow column.
        # Scroll horizontally inside the wrap rather than overflow the page.
        ".NLM_table-wrap"
        "{max-width:100%!important;overflow-x:auto!important;"
        "box-sizing:border-box!important;}"
        ".NLM_table-wrap table"
        "{max-width:100%!important;}"
        # Step 9 — expand the affiliations popup so author affiliations
        # render inline instead of staying hidden behind the
        # "View Author Information" button.
        "#affiliations-popup,#affiliations-popup.all-aff-infos"
        "{display:block!important;visibility:visible!important;"
        "position:static!important;opacity:1!important;"
        "width:auto!important;max-width:100%!important;"
        "margin:8px 0!important;padding:0!important;"
        "background:transparent!important;border:none!important;"
        "box-shadow:none!important;}"
        "#affiliations-popup .aff-info"
        "{display:block!important;margin:2px 0!important;}"
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

def _get_meta_mixed(html, name):
    """Get meta content handling mixed quoted/unquoted attribute style."""
    esc = re.escape(name)
    for pat in (
        rf'<meta[^>]*name="?\'?{esc}"?\'?[^>]*content="([^"]*)"',
        rf"<meta[^>]*name=\"?'?{esc}\"?'?[^>]*content='([^']*)'",
        rf'<meta[^>]*name="?\'?{esc}"?\'?[^>]*content=([^\s>]+)',
    ):
        m = re.search(pat, html)
        if m:
            return unescape(m.group(1).strip())
    return ""


def _parse_cit_fg(html):
    """Parse journal/year/volume/issue/pages from article_header cit-fg-* spans.

    Format:
      <span class=cit-fg-title><i>Biochemistry</i></span>
      <span class=cit-fg-year> 2014</span>
      <span class=cit-fg-volume>, 53</span>
      <span class=cit-fg-issue>, 17</span>
      <span class=cit-fg-pageRange>, 2781-2792</span>
    """
    def _grab(cls):
        m = re.search(rf'class=cit-fg-{cls}[^>]*>(.*?)</span>', html, re.DOTALL)
        if not m:
            return ""
        return strip_tags(m.group(1)).strip().lstrip(',').strip()

    journal = _grab('title').rstrip('.')
    year = _grab('year')
    volume = _grab('volume')
    issue = _grab('issue')
    pages = _grab('pageRange').replace('\u2013', '-').replace('\u2014', '-')
    return journal, year, volume, issue, pages


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    ACS-specific: title from dc.Title meta, DOI from publication_doi meta,
    journal/volume/issue/year/pages from article_header cit-fg-* spans.
    """
    title = _get_meta_mixed(html, "dc.Title")
    doi = _get_meta_mixed(html, "publication_doi")
    if not doi:
        # Fall back to dc.Identifier with scheme=doi
        dm = re.search(
            r'<meta[^>]*name="?dc\.Identifier"?[^>]*scheme="?doi"?[^>]*content="?([^\s">]+)',
            html,
        )
        if dm:
            doi = dm.group(1)

    journal, year, volume, issue, pages = _parse_cit_fg(html)
    if not year:
        date = _get_meta_mixed(html, "dc.Date")
        if date:
            ym = re.search(r'(\d{4})', date)
            if ym:
                year = ym.group(1)

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": format_doi(doi),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_affiliations(html):
    """Parse the affiliations-popup dict: symbol -> text.

    Each affiliation: <div class=aff-info id=aff1>
                        <span class=aff-symbol>† ‡</span>
                        <span class=aff-text>...</span>
                      </div>
    Returns list of (symbol_set, text) — symbol_set is a set of individual
    symbol chars so authors carrying any of them can reference this aff.
    """
    affs = []
    for m in re.finditer(
        r'<div\s+class="?aff-info"?[^>]*>(.*?)</div>', html, re.DOTALL
    ):
        block = m.group(1)
        sm = re.search(
            r'<span\s+class="?aff-symbol"?[^>]*>(.*?)</span>', block, re.DOTALL
        )
        tm = re.search(
            r'<span\s+class="?aff-text"?[^>]*>(.*?)</span>', block, re.DOTALL
        )
        if not tm:
            continue
        symbol_text = strip_tags(sm.group(1)).strip() if sm else ""
        # Symbol may contain "†" or "† ‡" — split into individual chars
        symbols = {c for c in symbol_text if not c.isspace()}
        # Affiliation text often has leading superscript marker <sup>†</sup>
        aff_html = tm.group(1)
        # Drop leading superscript markers
        aff_html = re.sub(r'^(?:\s*<sup>[^<]*</sup>\s*)+', '', aff_html)
        text = strip_tags(aff_html).strip()
        if text:
            affs.append((symbols, text))
    return affs


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    ACS-specific: display names in <span class=hlFld-ContribAuthor><a>Given Last</a>;
    affiliation symbols in following <span class=author-aff-symbol><sup>X</sup>;
    affiliations in <div id=affiliations-popup> with <span class=aff-symbol>.
    """
    # Scope to the main author list <ul class=loa ...> (also handle quoted
    # class values like "loa non-jats-loa"). Needed to avoid matching
    # hlFld-ContribAuthor spans in Cited By / Author Info / etc.
    loa_m = re.search(r'<ul\s+class=("(?:loa[^"]*)"|loa\b)[^>]*>', html)
    if not loa_m:
        return []
    # ACS author <ul> omits </li> closers inside, so <ul> depth tracking is
    # reliable; walk forward to the matching </ul>.
    pos = loa_m.end()
    depth = 1
    end = len(html)
    while depth > 0:
        no = re.search(r'<ul[\s>]', html[pos:])
        nc = re.search(r'</ul>', html[pos:])
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
    loa_html = html[loa_m.end():end]

    affs = _parse_affiliations(html)

    authors = []
    seen = set()
    # Find each <li> block; it is the stable scoping boundary for one author
    li_starts = list(re.finditer(r'<li\b[^>]*>', loa_html))
    for i, li_m in enumerate(li_starts):
        end = li_starts[i + 1].start() if i + 1 < len(li_starts) else len(loa_html)
        block = loa_html[li_m.end():end]

        # Locate the ContribAuthor span; the name is its direct textual content
        ca_m = re.search(
            r'<span\s+class="?hlFld-ContribAuthor"?[^>]*>(.*?)</span>',
            block, re.DOTALL,
        )
        if not ca_m:
            continue
        inner = ca_m.group(1)
        # Older ACS: name wrapped in <a>Given Last</a>. Newer ACS: plain text.
        a_m = re.search(r'<a[^>]*>([^<]+)</a>', inner)
        display = unescape(a_m.group(1) if a_m else strip_tags(inner)).strip()
        if not display or display in seen:
            continue
        seen.add(display)

        # Affiliation symbols (one per sup tag) for popup-based lookup
        symbols = set()
        for sm in re.finditer(
            r'class="author-xref-symbol author-aff-symbol"[^>]*><sup>([^<]+)</sup>',
            block,
        ):
            for c in sm.group(1):
                if not c.isspace():
                    symbols.add(c)

        author_affs = [text for syms, text in affs if syms & symbols]

        # Newer ACS: affiliation inside loa-info-affiliations-info per author
        if not author_affs:
            for am in re.finditer(
                r'<div\s+class="?loa-info-affiliations-info"?[^>]*>(.*?)</div>',
                block, re.DOTALL,
            ):
                text = strip_tags(am.group(1)).strip()
                if text:
                    author_affs.append(text)

        # If nothing matched and only one global affiliation exists, use it
        if not author_affs and len(affs) == 1:
            author_affs = [affs[0][1]]

        authors.append({
            "author": format_author_name(display),
            "affiliation": author_affs,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_inline_ref(entry):
    """Parse an inline ref format (<span class=NLM_string-ref>).

    Older ACS papers use a single inline citation string like:
      "Joyce, C. M., and Steitz, T. A. (1994) Annu. Rev. Biochem. 63, 777−822."
    Returns dict with title, journal, year, volume, pages, authors.
    """
    m = re.search(
        r'class="?NLM_string-ref"?[^>]*>(.*?)</span>', entry, re.DOTALL
    )
    if not m:
        return None
    raw = m.group(1)

    # Journal is the first <i>...</i> block (next <i> is volume)
    journal = ""
    volume = ""
    it_iter = list(re.finditer(r'<i>(.*?)</i>', raw, re.DOTALL))
    if it_iter:
        journal = strip_tags(it_iter[0].group(1)).strip().rstrip('.').strip()
        if len(it_iter) > 1:
            vol_text = strip_tags(it_iter[1].group(1)).strip()
            vm = re.match(r'(\d+)', vol_text)
            if vm:
                volume = vm.group(1)

    text = strip_tags(raw).strip()
    # "(YYYY)"
    year = ""
    ym = re.search(r'\((\d{4})\)', text)
    if ym:
        year = ym.group(1)

    # Authors: everything before "(YYYY)". Keep the trailing "." intact —
    # it is the terminal initial's period (e.g. "Steitz, T. A.") and
    # _looks_like_initials needs it to recognize the initials run.
    authors = []
    if ym:
        author_text = text[:ym.start()].strip().rstrip(',').strip()
        authors = _split_inline_authors(author_text)

    # Pages: "VVV, fpage-lpage." at end
    pages = ""
    pm = re.search(
        r'(\d+)\s*[−\-\u2013\u2014]\s*(\d+)\s*\.?\s*$',
        text,
    )
    if pm:
        pages = f"{pm.group(1)}-{pm.group(2)}"

    # When no <i> tags carried the journal/volume (older ACS bare-text
    # format), the run after "(YYYY)" looks like
    # "Journal Name VV, fpage-lpage." Split off the trailing volume
    # number and promote it to `volume`.
    if not journal and ym:
        tail = text[ym.end():].strip()
        jm = re.match(
            r'(.+?)\s+(\d+)\s*,\s*\d+\s*[-–—−]\s*\d+\s*\.?\s*$',
            tail,
        )
        if jm:
            journal = jm.group(1).strip().rstrip('.').strip()
            if not volume:
                volume = jm.group(2)
    elif journal and not volume:
        # Journal italic absorbed the volume (e.g. "Biochemistry 35").
        vm = re.search(r'\s+(\d+)\s*$', journal)
        if vm:
            volume = vm.group(1)
            journal = journal[:vm.start()].rstrip().rstrip(',').strip()

    return {
        "title": "",
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": "",
        "pages": pages,
        "doi": "",
        "authors": authors,
    }


def _looks_like_initials(tok):
    """True if tok matches one or more `X.` initials runs.

    Covers `R.`, `R. M.`, `J.-B.`. Not `Smith` (no terminal dot) and not
    `Smith J.` (multi-word with leading capital word).
    """
    if not tok:
        return False
    pieces = tok.split()
    for p in pieces:
        if not p.endswith('.'):
            return False
        head = p.rstrip('.')
        head_clean = re.sub(r'[\-‐‑‒–.]', '', head)
        if not head_clean:
            return False
        if not (head_clean.isalpha() and head_clean.isupper()
                and 1 <= len(head_clean) <= 4):
            return False
    return True


def _split_inline_authors(author_text):
    """Split "Last, F. M., Last, F., and Last, F. M." into formatted names.

    The ACS inline-ref author region is a comma-and-space-separated list
    where each author appears as `Surname, Initial1.[ Initial2.[ ...]]`.
    Commas separate adjacent fields *within* one author (surname / initials)
    and adjacent authors. Pair adjacent comma-split tokens (surname +
    initials) using the heuristic: the first token is a surname; the next
    token, if it matches the initials shape (one or more `X.` letters
    separated by spaces), is its initials run. Otherwise the surname
    stands alone.

    Also handles the comma-omitted final-author form
    `Last, F. and Last, F.` — when an initials chunk contains ` and `,
    the `and Lastname` portion is split off as its own surname token.
    """
    if not author_text:
        return []
    raw_parts = [p.strip() for p in author_text.split(',') if p.strip()]
    # Split out trailing `and Surname` chunks fused into an initials part
    # (e.g. "L. S. and Karam" -> "L. S." + "and Karam").
    parts = []
    for p in raw_parts:
        m = re.match(r'^(.+?)\s+and\s+(.+)$', p)
        if m and _looks_like_initials(m.group(1)):
            parts.append(m.group(1).strip())
            parts.append('and ' + m.group(2).strip())
        else:
            parts.append(p)
    authors = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        if tok.lower().startswith('and '):
            tok = tok[4:].strip()
        if i + 1 < len(parts) and _looks_like_initials(parts[i + 1]):
            combined = f"{tok}, {parts[i + 1]}"
            authors.append(format_author_name(combined))
            i += 2
        else:
            if tok:
                authors.append(format_author_name(tok))
            i += 1
    return authors


def _parse_structured_ref(entry):
    """Parse a structured ref (NLM_contrib-group, NLM_year, NLM_article-title, etc.).

    Newer ACS papers wrap each field in a dedicated span. Journal may be in
    <span class=citation_source-journal> (older) or as a bare <i>Journal</i>
    following the article-title (newer JACS / JPCB format).
    """
    def _field(cls):
        m = re.search(rf'class="?{cls}"?[^>]*>(.*?)</span>', entry, re.DOTALL)
        return strip_tags(m.group(1)).strip() if m else ""

    title = _field("NLM_article-title")
    if not title:
        title = _field("NLM_chapter-title")
    journal = _field("citation_source-journal").rstrip('.')
    if not journal:
        journal = _field("NLM_source").rstrip('.')

    volume = _field("NLM_volume")
    year = _field("NLM_year")
    fpage = _field("NLM_fpage")
    lpage = _field("NLM_lpage")
    pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

    # Authors: each author is in a <span class=NLM_contrib-group> span.
    # Newer ACS nests <span class=NLM_string-name> inside, so the first
    # </span> closes the nested span and we must walk forward to the outer
    # closing </span>. Use span-depth tracking to handle both shapes.
    authors = []
    for cm in re.finditer(r'<span\s+class="?NLM_contrib-group"?[^>]*>', entry):
        pos = cm.end()
        depth = 1
        end = len(entry)
        while depth > 0:
            no = re.search(r'<span[\s>]', entry[pos:])
            nc = re.search(r'</span>', entry[pos:])
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
        name = strip_tags(entry[cm.end():end]).strip().rstrip(',').strip()
        if ',' in name:
            authors.append(format_author_name(name))
        elif name:
            authors.append(name)

    # Require at least one non-journal structured field to trust this ref.
    # Journal alone can appear in inline-string-ref notes via stray <i>, so
    # we only try the <i> journal fallback once we already have authors/year/title.
    if not (title or authors or year or volume):
        return None

    # Newer format: first <i>...</i> that is NOT inside an NLM_volume span is
    # the journal. Detect by stripping volume spans before scanning for <i>.
    if not journal:
        stripped = re.sub(
            r'<span\s+class="?NLM_volume"?[^>]*>.*?</span>',
            ' ', entry, flags=re.DOTALL,
        )
        im = re.search(r'<i>([^<]+)</i>', stripped)
        if im:
            journal = im.group(1).strip().rstrip('.').strip()
    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": "",
        "pages": pages,
        "doi": "",
        "authors": authors,
    }


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    ACS-specific: references live inside <ol id=references> with per-entry
    <li id=...>; each <div class=NLM_citation ... data-doi=DOI> holds either
    a structured layout (NLM_contrib-group + NLM_year + NLM_article-title + ...)
    or an inline NLM_string-ref with everything as a single string (older papers).
    """
    m = re.search(r'<ol\s+id="?references"?[^>]*>', html)
    if not m:
        return []

    # Scope to the matching </ol>
    pos = m.end()
    depth = 1
    ol_end = len(html)
    while depth > 0 and pos < len(html):
        no = re.search(r'<ol[\s>]', html[pos:])
        nc = re.search(r'</ol>', html[pos:])
        if not nc:
            break
        if no and no.start() < nc.start():
            depth += 1
            pos += no.end()
        else:
            depth -= 1
            if depth == 0:
                ol_end = pos + nc.start()
                break
            pos += nc.end()
    refs_html = html[m.end():ol_end]

    refs = []
    # Each ref starts with <li id=...>. Older ACS (e.g. 2001 Biochemistry) uses
    # IDs like <paperid>b<NNNNN> for bibliography entries and <paperid>n<NNNNN>
    # for footnotes/abbreviations. Skip the n-prefixed notes.
    li_starts = list(re.finditer(r'<li\s+id="?([^"\'>\s]+)"?[^>]*>', refs_html))
    for i, li_m in enumerate(li_starts):
        end = li_starts[i + 1].start() if i + 1 < len(li_starts) else len(refs_html)
        entry = refs_html[li_m.end():end]

        li_id = li_m.group(1)
        if re.search(r'n\d+$', li_id):
            continue

        # DOI from data-doi on NLM_citation div
        doi = ""
        dm = re.search(r'data-doi=["\']?(10\.[^"\'>\s]+)', entry)
        if dm:
            doi = format_doi(dm.group(1))

        ref = _parse_structured_ref(entry)
        if ref is None:
            ref = _parse_inline_ref(entry)
        if ref is None:
            # Last-resort fallback: full text
            cm = re.search(r'class="?NLM_citation[^"]*"[^>]*>(.*?)</div>', entry, re.DOTALL)
            text = strip_tags(cm.group(1)).strip() if cm else ""
            ref = {
                "title": text,
                "journal": "",
                "year": "",
                "volume": "",
                "issue": "",
                "pages": "",
                "doi": doi,
                "authors": [],
            }
        else:
            ref["doi"] = doi or ref.get("doi", "")
        refs.append({"": ref})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _extract_section(html, start_match):
    """Extract a div from opening match to matching close, returning the slice."""
    pos = start_match.end()
    depth = 1
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
            pos += nc.end()
    return html[start_match.start():pos]


def _get_section_heading(section_html):
    """Get first heading text from a section div."""
    m = re.search(r'<h[1-4][^>]*>(.*?)</h[1-4]>', section_html, re.DOTALL)
    if m:
        return strip_tags(m.group(1)).strip()
    return ""


def _find_refs_boundary(article):
    """Return the position where the References section starts inside article.

    Priority (earliest of the following):
      1. <h2>References</h2>
      2. <ol id=references>
    Falls back to len(article) when nothing matches.
    """
    candidates = []
    h2_m = re.search(r'<h2[^>]*>\s*References\s*</h2>', article, re.IGNORECASE)
    if h2_m:
        candidates.append(h2_m.start())
    ol_m = re.search(r'<ol\s+id="?references"?', article)
    if ol_m:
        candidates.append(ol_m.start())
    return min(candidates) if candidates else len(article)


def _strip_body_chrome(html_fragment):
    """Drop in-body chrome that pollutes main_text on ACS pages.

    Removes: tooltip popovers (role=tooltip), CC license blocks, article tag
    rails (Subjects), copyright statements, footnote groups (fnGroup author
    notes — funding/correspondence), and empty headings (`<h[1-4]> </h[1-4]>`)
    that would otherwise concatenate with the next paragraph through the
    standard text pipeline.
    """
    # role=tooltip popovers (License Summary, Subjects help, etc.)
    while True:
        m = re.search(r'<div[^>]*\brole=["\']?tooltip["\']?[^>]*>', html_fragment)
        if not m:
            break
        new = _remove_nested_element(html_fragment, re.escape(m.group()))
        if new == html_fragment:
            break
        html_fragment = new
    # CC license blocks
    html_fragment = remove_elements_by_selector(
        html_fragment,
        "article__cc-license",
        "article__tags",
        "article_header-article-copyright",
        "extra-info-sec",
        "article-section__back",
    )
    # Empty headings (`<h2 id=_iN> </h2>`) — they otherwise produce an empty
    # `## ` marker that joins with the following paragraph.
    html_fragment = re.sub(
        r'<h([1-6])[^>]*>\s*</h\1>', '', html_fragment
    )
    return html_fragment


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/supporting information/extended data/source data/expanded
        view/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> _strip_body_chrome
    -> extract_captions -> strip_common -> tags_to_text -> drop_noise.
    ACS-specific layout:
      - Abstract paragraph lives in <div class=article_abstract-content>
        (the wider article_abstract div also wraps the license, Subjects rail,
        and footnote group, which we drop).
      - Body NLM_sec_level_1 divs follow, then a <div id=ac_i*> /
        <div class=articleBody_back> with Author Information / Acknowledgment.
      - References live in <ol id=references> inside a "References" h2
        section; everything after it is Cited By / Recommended / Supporting
        Information / page chrome.
    """
    article_m = re.search(r'<article[^>]*>', html)
    if not article_m:
        return ""
    article = html[article_m.end():]

    parts = []

    # Abstract paragraph: the inner article_abstract-content container holds
    # only the abstract text (the outer article_abstract div also wraps
    # license / Subjects / fnGroup chrome that we don't want in main_text).
    abs_m = re.search(
        r'<div\s+class="?article_abstract-content[^>]*>', article
    )
    if abs_m:
        abs_html = _extract_section(article, abs_m)
        abs_html = _strip_body_chrome(abs_html)
        abs_html = extract_captions(abs_html)
        abs_html = strip_common(abs_html)
        abs_text = tags_to_text(abs_html)
        if abs_text.strip():
            parts.append("## Abstract\n\n" + abs_text.lstrip("# ").lstrip())

    # Body slab: from the first NLM_sec_level_1 to the References boundary.
    refs_pos = _find_refs_boundary(article)
    body_start_m = _ALL_SECTION_RE.search(article)
    if body_start_m and body_start_m.start() < refs_pos:
        body_html = article[body_start_m.start():refs_pos]
        body_html = _strip_body_chrome(body_html)
        body_html = extract_captions(body_html)
        body_html = strip_common(body_html)
        text = tags_to_text(body_html)
        if text.strip():
            parts.append(text)

    # Supplementary: any NLM_sec level_1 sections after References boundary
    post = article[refs_pos:]
    for sec_m in _ALL_SECTION_RE.finditer(post):
        section_html = _extract_section(post, sec_m)
        heading = _get_section_heading(section_html)
        if heading and _SUPP_RE.search(heading):
            section_html = _strip_body_chrome(section_html)
            section_html = extract_captions(section_html)
            section_html = strip_common(section_html)
            text = tags_to_text(section_html)
            if text.strip():
                parts.append(text)

    result = "\n\n".join(parts)
    return drop_noise(result, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse ACS HTML into a papers/*.json-format dict."""
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
