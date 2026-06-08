"""Cold Spring Harbor Laboratory Press (cshlp.org) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    affiliation_from_email,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    neutralize_media_queries,
    parse_meta_authors,
    remove_elements_by_id,
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
    "In this window",
    "In a new window",
    "In this page",
    "Download as PowerPoint",
    "View larger version:",
)

# h2 headings that are reference sections (removed from main_text)
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|appendix',
    re.IGNORECASE,
)

# Site chrome (end boundary)
_CHROME_RE = re.compile(
    r'^articles citing this article|^citing articles via|^we recommend',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for genesdev.cshlp.org (HighWire).

    Step 1: cap body width at 752 px, center on the page; override the
            publisher's fixed pixel widths on body/#header/#footer/
            #content-block/#article-cb-main so the article column
            shrinks to the cap. (no @media queries in the source — the
            CSS layout is static, so neutralize_media_queries is a no-op
            here but kept for symmetry.)
    Step 2: no cookie banner element ships in the DOM (only an unused
            CSS rule for `#cookie-law` remains).
    Step 3: sticky elements — `#bg-hovering-img` (full-viewport
            transparent overlay) and `div#docked-nav*` (HighWire docked
            sidebar nav, JS-attached on scroll). Hidden via CSS so the
            DOM stays intact for the parser.
    Step 4: vertical sidebars — `#col-2` (left article-tools sidebar,
            article-cb-main / article-dyn-nav lives here) and `#col-3`
            (right journal-current-issue sidebar). Both span the full
            page height alongside the article column; hidden via CSS.
    Step 5: ad block — `#ads3` (top banner ad slot) and the
            `.no-ad.tower_col_2` placeholder li.
    Step 6: page background — body cascade leaves `background:#FFF` as
            the final rule, but the layered `background-color:#CCCCCC`
            shows through at wide viewports because the body is capped.
            Force html + body backgrounds to white so the background-
            around-column scan stays clean at every viewport.
    Step 8: figures — `.fig.pos-float` wraps `.fig-inline > a > img`
            with caption in `.fig-caption` sibling. Force the wrapper
            to block, image full width above caption, 12 px gap.
    Step 9: expand the `<ol class="affiliation-list hideaffil">` block.
            Closed-state CSS is `.hideaffil{position:absolute;left:-9999px;
            width:5000px}` — the off-screen-accessibility hide idiom, not a
            floating overlay (no z-index, no box-shadow, no fixed pixel
            width framing as a card). The publisher's "+" toggle
            (`affiliation-list-reveal`) returns the <ol> to normal block
            flow, pushing siblings down — push-down per Step 9.
            Tables and references are not collapsed in the source — no
            override needed for them.
    """
    html = neutralize_media_queries(html)

    # Step 5 — ad blocks. The ad slot div, the no-ad placeholder li,
    # and the .banner-ads absolutely-positioned slot in the header.
    html = remove_elements_by_id(html, "ads3")
    # Remove <li class="no-ad tower_col_2"> ad placeholder
    html = _remove_nested_element(
        html,
        r'<li[^>]*\bclass="no-ad tower_col_2"[^>]*>',
    )
    # Remove the header .banner-ads slot (position:absolute, escapes the
    # body cap when no ad ships).
    while True:
        prev = html
        html = _remove_nested_element(
            html,
            r'<\w+[^>]*\bclass=("[^"]*\bbanner-ads\b[^"]*"|'
            r"'[^']*\bbanner-ads\b[^']*'|banner-ads\b)[^>]*>",
        )
        if html == prev:
            break

    override = (
        "<style>"
        # Step 1 / Step 6 — lock layout to 752 px wide, white background.
        "html{margin:0!important;padding:0!important;"
        "background:#fff!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "min-width:0!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "background:#fff!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # Override publisher's fixed pixel widths so the floats collapse
        # into the body cap. content-block is the main article column.
        "#pageid-content{width:auto!important;max-width:100%!important;}"
        "#header,#footer{width:auto!important;max-width:100%!important;"
        "background:#fff!important;}"
        # HighWire's header/footer use fixed-pixel-width inner bars
        # (.bar 980px, .bar-inner 960px, .footer-col-left 756px,
        # .footer-col-right 203px) that escape the body cap. Override
        # so they shrink to the body width.
        ".bar,.bar-inner,.footer-group,"
        ".footer-col-left,.footer-col-right"
        "{width:auto!important;max-width:100%!important;"
        "margin-left:0!important;padding-left:0!important;"
        "float:none!important;}"
        # The header's search / login / institutional-branding widgets
        # are absolutely-positioned with hard-coded `left:756px` (the
        # right edge of the publisher's 756 px content-block). They
        # escape the 752-px body cap at wide viewports. Convert to
        # static flow so they sit inside the cap.
        "#header,#footer{position:relative!important;}"
        ".header-qs,#hdr-login,.inst-branding,#authstring"
        "{position:static!important;left:auto!important;top:auto!important;"
        "width:auto!important;max-width:100%!important;}"
        # The publisher's `.inst-branding` and `#hdr-login` ship empty
        # in static-page captures (the actual login/branding is JS-
        # populated post-load and SingleFile snapshots before the JS
        # settles). They reserve 30 / 88 px of blank vertical space —
        # collapse them so the gap detector stays clean.
        ".inst-branding,#hdr-login"
        "{height:auto!important;min-height:0!important;"
        "padding-top:0!important;padding-bottom:0!important;}"
        ".inst-branding:empty,#hdr-login:empty"
        "{display:none!important;}"
        "#content-block{float:none!important;width:auto!important;"
        "max-width:100%!important;padding:0!important;margin:0!important;"
        "border-right:none!important;background:#fff!important;}"
        # Step 4 — hide left/right sidebars (publisher-native columns
        # that don't fit the 720 reading layout).
        "#col-2,#col-3{display:none!important;}"
        # Step 3 — hide HighWire's docked-nav and full-viewport overlay
        # (only fires past a scroll threshold via JS, but flagged by the
        # multi-position scroll test).
        "#bg-hovering-img,div#docked-nav,div#docked-nav3"
        "{display:none!important;}"
        # Step 9 — expand the off-screen affiliation list so each
        # author's institution renders inline under the author block.
        ".affiliation-list.hideaffil"
        "{position:static!important;left:auto!important;width:auto!important;"
        "max-width:100%!important;display:block!important;"
        "margin:8px 0!important;padding:0 0 0 16px!important;}"
        ".affiliation-list.hideaffil li.aff"
        "{display:list-item!important;margin:2px 0!important;}"
        # Step 8 — figures: image fills column, image above caption,
        # 12 px gap. CSHLP markup is
        #   <div class="fig pos-float"> > <div class=fig-inline>
        #     > <a><img></a> <div class=callout>...</div>
        #   <div class=fig-caption>caption</div>
        ".fig.pos-float,.fig-inline"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;float:none!important;"
        "margin-left:0!important;margin-right:0!important;"
        "padding-left:0!important;padding-right:0!important;"
        "box-sizing:border-box!important;}"
        ".fig-inline a,.fig-inline a img,.fig-inline img"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 12px 0!important;"
        "box-sizing:border-box!important;}"
        ".fig-caption"
        "{display:block!important;width:100%!important;"
        "margin:0!important;}"
        # Hide the per-figure callout (View larger version, In a new
        # window, Download as PowerPoint Slide) — those phrases are
        # already filtered by _NOISE in the parser, but they clutter
        # the visual layout.
        ".fig-inline .callout{display:none!important;}"
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
    title = get_meta(html, "citation_title")
    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    volume = get_meta(html, "citation_volume")
    issue = get_meta(html, "citation_issue")
    doi = format_doi(get_meta(html, "citation_doi"))

    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_date")
            or get_meta(html, "DC.Date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    return {
        "title": title,
        "journal": journal.rstrip(".") if journal else "",
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_body_affiliations(html, authors):
    """Extract affiliations from the HTML body contributor/affiliation list.

    Handles two CSHLP affiliation formats:
      - New format: one <a id=aff-N> + <address> per affiliation; contributor
        xref uses class=xref-aff href=#aff-N.
      - Old format: a single <a id=aff-1> + <address> blob containing
        multiple affiliations separated by <sup>N</sup> labels; contributor
        xref uses class=xref- href=#target-N with visible label text matching
        the <sup>N</sup> marker.
    """
    # Build label -> text map. Key is the visible numeric label (str).
    aff_map = {}
    aff_blocks = list(re.finditer(
        r'<li class=aff><a id=(aff-\d+)[^>]*></a>\s*<address>(.*?)</address>',
        html, re.DOTALL,
    ))
    for am in aff_blocks:
        addr_html = am.group(2)
        # Split on <sup>N</sup> markers if present.
        sup_marks = list(re.finditer(r'<sup>(\d+)</sup>', addr_html))
        if len(sup_marks) >= 2:
            for j, sm in enumerate(sup_marks):
                label = sm.group(1)
                start = sm.end()
                end = sup_marks[j + 1].start() if j + 1 < len(sup_marks) else len(addr_html)
                text = strip_tags(addr_html[start:end]).strip()
                text = text.rstrip(';').strip().rstrip(',').strip()
                if text:
                    aff_map[label] = text
        else:
            label = am.group(1).split('-', 1)[1]  # 'aff-1' -> '1'
            text = strip_tags(addr_html).strip()
            text = re.sub(r'^\d+\s*', '', text).strip().rstrip(';').rstrip(',').strip()
            if text:
                aff_map[label] = text

    if not aff_map:
        return authors

    # Map each author to their affiliations via xref labels (visible text).
    contribs = list(re.finditer(r'<li[^>]*id=contrib-\d+[^>]*>(.*?)</li>', html, re.DOTALL))
    for i, contrib in enumerate(contribs):
        if i >= len(authors):
            break
        entry = contrib.group(1)
        labels = []
        for xm in re.finditer(
            r'<a[^>]*class=xref-(?:aff)?[^>]*href=#([^\s>]+)[^>]*>([^<]*)</a>',
            entry,
        ):
            href = xm.group(1)
            visible = xm.group(2).strip()
            # Skip footnote / correspondence links — only affiliation refs.
            if href.startswith('fn') or href.startswith('sec'):
                continue
            if visible and visible in aff_map:
                labels.append(visible)
        affs = []
        seen = set()
        for lab in labels:
            if lab in aff_map and lab not in seen:
                affs.append(aff_map[lab])
                seen.add(lab)
        # If no xref labels found but only one affiliation, assign it.
        if not affs and len(aff_map) == 1:
            affs = list(aff_map.values())
        authors[i]["affiliation"] = affs

    return authors


def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers.

    CSHLP Silverchair meta tags store names as 'Given Middle Last';
    format_author_name handles the flip and compound-surname particles
    via parse_combined_name + format_name in _helpers.
    """
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name; names
    without a comma (CSHLP meta tags often emit "Given Middle Last") are
    flipped first by _display_to_initials so the surname lands first.
    Uses citation_author meta tags, with body fallback for affiliations
    when citation_author_institution tags are absent.
    """
    meta_authors = parse_meta_authors(html)
    authors = [
        {
            "author": _display_to_initials(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in meta_authors
    ]
    # If any author has affiliations, meta tags worked — return as-is
    if any(a["affiliation"] for a in authors):
        return authors
    # Fallback: parse affiliations from HTML body
    authors = _parse_body_affiliations(html, authors)
    # Email-domain inference: older CSHLP symposium HTMLs expose the
    # corresponding author's email (citation_author_email meta) but no
    # structural affiliation block. When the email maps to a known
    # academic domain, attribute that institution to authors who
    # otherwise have no aff. get_meta handles both quoted and unquoted
    # content values (older CSHLP uses unquoted).
    if not any(a["affiliation"] for a in authors):
        email = get_meta(html, "citation_author_email")
        aff = affiliation_from_email(email)
        if aff:
            for a in authors:
                if not a["affiliation"]:
                    a["affiliation"] = [aff]
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    Handles two CSHLP HTML formats:
      - New format: all authors in cit-auth spans, cit-article-title,
        cit-lpage, cit-jnl-abbrev (abbr tag).
      - Old format: only first author in cit-auth span, remaining authors
        as inline text, title as inline text between year and journal,
        no cit-lpage, journal in cit-source span with trailing dot.
    """
    refs = []
    m = re.search(r'class="?cit-list\b', html)
    if not m:
        return refs

    # Find all reference entries: <div class="cit ref-cit ..."> or <li> with cit class
    ref_html = html[m.start():]
    ref_starts = [rm.start() for rm in re.finditer(r'<div class="?cit ref-cit\b', ref_html)]
    if not ref_starts:
        # Fallback: <li> entries
        ol_end = ref_html.find('</ol>')
        if ol_end < 0:
            ol_end = len(ref_html)
        ref_starts = [lm.start() for lm in re.finditer(r'<li[^>]*>', ref_html[:ol_end])]

    for i, start in enumerate(ref_starts):
        end = ref_starts[i + 1] if i + 1 < len(ref_starts) else start + 5000
        entry = ref_html[start:end]

        # Older CSHLP HTMLs include both an <ol class="cit-authors-list
        # duplicate"> block (verbose, used by some downstream sprinkles) AND
        # the <cite> block (canonical citation text). Strip the duplicate ol
        # so only the cite-side names are extracted.
        entry = re.sub(
            r'<ol class="cit-authors-list duplicate">.*?</ol>',
            '', entry, flags=re.DOTALL,
        )

        # --- Authors (structured spans) ---
        authors = []
        for am in re.finditer(
            r'<span class=cit-name-surname>([^<]*)</span>\s*'
            r'<span class=cit-name-given-names>([^<]*)</span>',
            entry,
        ):
            surname = unescape(am.group(1)).strip().rstrip(",")
            given = unescape(am.group(2)).strip().rstrip(".")
            initials = given.replace(".", "").replace(" ", "")
            authors.append(f"{surname} {initials}" if initials else surname)

        # --- Inline authors (old format: only first author in spans) ---
        num_auth_spans = len(re.findall(r'<span class=cit-auth>', entry))
        if num_auth_spans <= 1:
            inline_m = re.search(
                r'</span></span>(.*?)<span class=cit-pub-date',
                entry, re.DOTALL,
            )
            if inline_m:
                inline_text = inline_m.group(1)
                for im in re.finditer(
                    r'(?:^|[.,]\s*)(?:and\s+)?'
                    r'((?:[a-z]+\s+)*[A-Z][A-Za-z\'-]+)\s*,\s*'
                    r'([A-Z]\.?(?:[A-Z]\.?)*)',
                    inline_text,
                ):
                    surname = unescape(im.group(1)).strip()
                    initials = im.group(2).replace(".", "")
                    authors.append(f"{surname} {initials}" if initials else surname)

        # Helper for unquoted or quoted class matching
        def _cit_field(cls):
            fm = re.search(rf'class="?{cls}"?[^>]*>([^<]*)', entry)
            return unescape(fm.group(1)).strip() if fm else ""

        # --- Title ---
        # Use full-content extraction for cit-article-title (may contain
        # inline HTML like <em>)
        title = ""
        title_span = re.search(
            r'class="?cit-article-title"?[^>]*>(.*?)</span>',
            entry, re.DOTALL,
        )
        if title_span:
            title = strip_tags(title_span.group(1)).strip()
            title = re.sub(r'\s+', ' ', title)

        # Fallback for old format: extract between year and journal span
        if not title:
            title_m = re.search(
                r'cit-pub-date[^>]*>[^<]*</span>\.\s*(.*?)\.?\s*'
                r'<(?:span class=cit-source|abbr class=cit-jnl-abbrev)',
                entry, re.DOTALL,
            )
            if title_m:
                title = strip_tags(title_m.group(1)).strip()
                title = re.sub(r'\s+', ' ', title)

        # --- Journal ---
        journal = _cit_field("cit-jnl-abbrev") or _cit_field("cit-source")
        journal = journal.rstrip(".")

        # --- Year, volume, pages ---
        year = _cit_field("cit-pub-date").rstrip(".")
        volume = _cit_field("cit-vol")
        fpage = _cit_field("cit-fpage")
        lpage = _cit_field("cit-lpage")

        # Fallback for lpage: extract from inline text after cit-fpage
        if fpage and not lpage:
            lp_m = re.search(
                r'cit-fpage[^>]*>[^<]*</span>\s*[-\u2013]\s*(\d+)',
                entry,
            )
            if lp_m:
                lpage = lp_m.group(1)

        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # --- DOI ---
        doi = ""
        dm = re.search(r'data-doi=([^\s>]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1).strip('"')))

        # Pages fallback for article-number journals (CSH Perspectives,
        # eLife, etc.): when no fpage/lpage spans exist, the article ID
        # sits either in the ijlink ``resid=...`` URL or at the end of
        # the DOI. Cover multiple ijlink and DOI tail shapes:
        #   resid=6/9/a016428            -> a016428
        #   resid=cshperspect.a016428v1  -> cshperspect.a016428
        #   resid=2023.05.08.539880v1    -> 2023.05.08.539880
        #   DOI tail "cshperspect.a016428", "eLife.66198", bioRxiv dated
        #   "2023.05.08.539880", BMC "1471-2105-10-48", PO uppercase
        #   prefixes like "PO.17.00298".
        if not pages:
            rid_m = re.search(
                r'resid=(?:\d+/\d+/)?([A-Za-z][\w.\-]+?)(?:v\d+)?(?=["&\s>])',
                entry,
            )
            if rid_m:
                pages = rid_m.group(1)
            elif doi:
                tail_m = re.search(
                    r'/([A-Za-z][\w.]*\d[\w.\-]*|\d[\d.\-]+\d)$',
                    doi,
                )
                if tail_m:
                    pages = tail_m.group(1)

        # Old-layout fallback: some refs emit only cit-lpage with no
        # cit-fpage span. Accept it as a single-page value.
        if not pages and lpage:
            pages = lpage

        # Fallback
        if not title and not authors:
            title = strip_tags(entry).strip()

        refs.append({"": {
            "title": title,
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": "",
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _find_h2_headings(html):
    """Find all h2 headings and their positions.

    Returns list of (start_pos, heading_text).
    """
    entries = []
    for m in re.finditer(r'<h2[^>]*>(.*?)</h2>', html, re.DOTALL):
        text = strip_tags(m.group(1)).strip()
        if text:
            entries.append((m.start(), text))
    return entries


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    CSHLP-specific: start at the Abstract heading (abstract + keywords included);
    site chrome (e.g. "Articles Citing This Article") is dropped as an end boundary.
    """
    # Find article container
    m = re.search(r'class="article\s[^"]*fulltext-view[^"]*"[^>]*>', html)
    if not m:
        m = re.search(r'class="article[^"]*"[^>]*>', html)
    if not m:
        return ""

    content = html[m.end():]
    h2s = _find_h2_headings(content)
    if not h2s:
        return ""

    # Find start: at the Abstract heading (include abstract + keywords)
    start = 0
    for i, (hpos, text) in enumerate(h2s):
        if text.lower() == "abstract":
            start = hpos
            break
    else:
        # No Abstract h2 — try abstract div
        abs_div = re.search(r'<div[^>]*class="?section abstract"?', content)
        if abs_div:
            start = abs_div.start()

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text) in enumerate(h2s):
        if _REF_RE.search(text) and pos >= start:
            first_ref_idx = i
            break

    # Build body from two zones
    # First, capture any content between start and the first body h2
    # (intro text that appears without a heading)
    parts = []
    first_body_h2 = None
    for pos, text in h2s:
        if pos >= start:
            first_body_h2 = pos
            break
    if first_body_h2 and first_body_h2 > start:
        parts.append((start, first_body_h2))

    for i, (pos, text) in enumerate(h2s):
        if pos < start:
            continue
        if _REF_RE.search(text):
            continue
        if _CHROME_RE.search(text.strip()):
            continue

        end = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)

        if first_ref_idx is None or i < first_ref_idx:
            parts.append((pos, end))
        else:
            if _SUPP_RE.search(text):
                parts.append((pos, end))

    if not parts:
        return ""

    body_html = ""
    for s, e in parts:
        body_html += content[s:e]

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse CSHLP HTML into a papers/*.json-format dict."""
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
