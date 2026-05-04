"""Journal of Visualized Experiments (jove.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
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

# Publisher-specific noise strings removed from main_text.
# JoVE pages are paywalled: section previews end with "...." and a
# "Access restricted. Please log in..." overlay message; drop those so
# they do not pollute main_text.
_NOISE = (
    "Access restricted. Please log in or start a trial to view this content.",
    "Please log in or start a trial",
    "Access restricted.",
    "Open in a new tab",
)

# Reference section title pattern
_REF_RE = re.compile(r"\breferences\b", re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r"supplement|extended data|source data|expanded view|powerpoint|appendix",
    re.IGNORECASE,
)

# Main-text sections (h2 id values) in the order they appear.
# Abstract through Materials are kept as main_text; References onward are
# dropped except for supplementary-matching titles (JoVE has none).
_MAIN_SECTION_IDS = (
    "summary",
    "abstract",
    "introduction",
    "protocol",
    "results",
    "discussion",
    "disclosures",
    "acknowledgements",
    "materials",
)

# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize JoVE HTML to a single centered text column.

    JoVE uses Chakra UI with build-generated class names (e.g. css-xxxxx)
    that change between deployments, so chrome targeting uses stable
    `data-atm="..."` test-id attributes plus modern `:has()` selectors
    to hide DOM subtrees without needing fragile regex cuts.

    Chrome stripped:
      - Cookie consent banner ("We value your privacy", class
        `cky-consent-container`, plus Cookieyes modal overlay).
      - Site header and footer + their sticky/flex wrappers
        (`vector-layout_header`, `vector-layout_footer`) which would
        otherwise remain as empty bands after <header>/<footer> removal.
      - EqualWeb accessibility widget (`INDbtnWrap`).
      - Outer Chakra flex column (`css-1kk26sq`) is collapsed from
        100dvh flex to auto-height block so no empty space trails the
        article.
      - Left-side "In This Article" table of contents (the div that
        directly contains the navigator h2 with data-atm=
        "article-section-navigator-title"). Removing it lets the
        sibling article-body column reclaim the flex space.
      - "Reprints and Permissions" section and everything after it in
        the article container (Explore More Articles tags, etc.).

    Reading column: the `.chakra-container` element wraps the entire
    article (breadcrumb + sticky header + body + references). Cap it at
    752 px with 56 px top/bottom and 16 px side padding.
    """
    # Lock layout to publisher's narrow (≤1024 px) form at any viewport.
    html = neutralize_media_queries(html)
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    # Top-of-page chrome above the "Research Article" label the user
    # identified as the reading-column start: site header (<header>),
    # breadcrumb nav (<nav>, the only one on the page), and the
    # "Scheduled Maintenance" aria-live announcement (<output>).
    html = _remove_nested_element(html, r"<header\b[^>]*>")
    html = _remove_nested_element(html, r"<nav\b[^>]*>")
    html = _remove_nested_element(html, r"<output\b[^>]*aria-live\b[^>]*>")
    html = _remove_nested_element(html, r"<footer\b[^>]*>")
    # After <header>/<footer> are stripped, their sticky/flex wrappers
    # remain as empty 57 px / footer-height bands. Drop them too.
    html = remove_elements_by_id(
        html, "vector-layout_header", "vector-layout_footer",
    )
    # EqualWeb/Interdeal floating accessibility widget ("Explore your
    # accessibility options" circle button).
    html = remove_elements_by_id(html, "INDbtnWrap")
    # Scheduled-Maintenance banner wrapper: the <output aria-live> child
    # is already stripped above, but its parent <div class=css-1ohckfi>
    # keeps a light-blue background band at the top of __next. Drop it.
    # class attr is unquoted, so use _remove_nested_element directly —
    # remove_elements_by_selector requires double-quoted class values.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass=["\']?[^"\'>]*css-1ohckfi[^"\'>]*["\']?[^>]*>',
    )
    # Action-buttons block (Cite / Download PDF / Download Materials
    # List / English etc.). Layout was inconsistent across viewports
    # (Materials List wrapping to its own line at some widths), and the
    # actions aren't useful in an offline reading snapshot.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass="[^"]*\btext-article-action-buttons-wrapper\b[^"]*"[^>]*>',
    )
    # Remove the empty text-to-speech button wrapper that JoVE places as
    # the first child of the title-section flex row. It has w=h=0 so it
    # doesn't render, but the flex gap on the parent adds a 16 px offset
    # between it and the H1 — pushing the title right of its siblings.
    # class attr is unquoted so remove_elements_by_selector (which
    # requires class="...") misses it; match directly.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass=["\']?[^"\'>]*text-article-header-tts-button-wrapper',
    )
    # Cookie banner: cookieyes uses several sibling containers sharing
    # the `cky-` prefix. Strip each class variant.
    for _ in range(10):
        before = html
        html = remove_elements_by_selector(
            html, "cky-consent-container", "cky-modal", "cky-overlay",
        )
        if html == before:
            break

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze, column cap, and :has()-based hiding
    # for the TOC and reprints-onward blocks.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        # Layout freeze (Step 2). html fills the viewport; body fills
        # the viewport up to 752 px, centered when wider. At vw ≤ 752
        # body shrinks with the viewport; at vw > 752 body caps at
        # 752 and the surrounding space becomes centering margin.
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Outer Chakra flex column (css-1kk26sq) ships with
        # width:100dvw;height:100dvh;display:flex which leaves a tall
        # empty band below the article when content is shorter than the
        # viewport. Collapse it to a plain block that sizes to content.
        ".css-1kk26sq{display:block !important;"
        "width:100% !important;height:auto !important;"
        "min-height:0 !important}"
        # Inner scroller (#vector-fullpage-layout_scroller, class
        # css-1jhsr8m) ships with min/max-height:calc(100dvh - ...) plus
        # overflow-y:auto, so the article scrolls inside an internal
        # ~90vh viewport instead of extending the document — leaving
        # visible empty space below the text. Un-constrain height and
        # disable the internal scroll. Same fix for the flex main-
        # container and the inner vector-layout_main which has
        # height:100%.
        "#vector-fullpage-layout_scroller,"
        ".vector-fullpage-layout_main-container,"
        "#vector-layout_main{"
        "display:block !important;flex:unset !important;"
        "height:auto !important;min-height:0 !important;"
        "max-height:none !important;overflow:visible !important;"
        # Also zero their default padding: the scroller ships with
        # `padding:0 5px` (5 px gutter for its scrollbar) and
        # vector-layout_main ships with `padding:16px 0` (top/bottom
        # band around the article). Both leak through and push the
        # chakra-container inward, so its 56px/16px padding no longer
        # measures from body edge.
        "padding:0 !important;margin:0 !important}"
        # Force white backgrounds on the wrapper chain. The site paints
        # rgb(249,249,249) gray on css-u41yqu and var(--chakra-colors-
        # background) on css-1kk26sq, which shows through behind the
        # capped chakra-container.
        ".css-1kk26sq,"
        "#vector-fullpage-layout_scroller,"
        ".vector-fullpage-layout_main-container,"
        "#vector-layout_main,"
        ".css-8atqhb,.css-u41yqu,.chakra-container{"
        "background:#fff !important;background-color:#fff !important;"
        "background-image:none !important}"
        # Capped reading column (Step 4) on the outermost article wrapper.
        ".chakra-container{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important}"
        ".chakra-container *{max-width:100% !important;min-width:0 !important}"
        # Zero all horizontal padding/margin on every descendant so the
        # only horizontal whitespace is the chakra-container's own 16 px
        # padding — text sits 16 px from the wrapper edge regardless of
        # how many Chakra stacks (css-xxxx) are nested or which h1/h2/p
        # tag the text is in. Explicitly EXEMPT buttons: the original
        # "border-left/right:none" rule stripped their left/right borders
        # while leaving top/bottom, giving a "broken border" look on
        # Cite / Download PDF / Download Materials List / English etc.
        ".chakra-container *:not(button):not(a.chakra-button){"
        "padding-left:0 !important;padding-right:0 !important;"
        "margin-left:0 !important;margin-right:0 !important;"
        "border-left:none !important;border-right:none !important;"
        "box-shadow:none !important}"
        # Restore default 40-px padding-left on numbered/bulleted lists.
        # The descendant zero above kills it, and with the publisher's
        # `list-style-position:outside`, the marker (ref number) renders
        # in a virtual column to the LEFT of the list — clipped off
        # the page when pl=0. Triple `:root` to out-rank the
        # `.chakra-container *:not(button):not(a.chakra-button)`
        # selector (specificity 0,2,2). Also restore the publisher's
        # native 16-px top/bottom margins on the list — the descendant
        # `*:first-child{margin-top:0}` / `*:last-child{margin-bottom:0}`
        # rules zero them, collapsing the gap between the "References"
        # heading and the first list item.
        ":root:root:root .chakra-container ol,"
        ":root:root:root .chakra-container ul{"
        "padding-left:40px !important;"
        "margin-top:16px !important;margin-bottom:16px !important}"
        # Un-float the article header: site CSS puts the title + metadata
        # block in position:sticky so it pins to the viewport top as the
        # reader scrolls. Force static so it flows with the rest of the
        # column.
        "#sticky-header{position:static !important}"
        # Zero first/last-child margins (descendant form — reclaiming
        # every trailing margin keeps the bottom flush with the wrapper
        # padding; the direct-child form leaves deep last-children
        # contributing ~30-230 px of trailing whitespace at vw=1280).
        ".chakra-container *:first-child{margin-top:0 !important;padding-top:0 !important}"
        ".chakra-container *:last-child{margin-bottom:0 !important;padding-bottom:0 !important}"
        # Exempt Chakra popovers: their <section>/body/content uses the
        # native 11 px padding-top to breathe around the email tooltip
        # text. Zeroing it collapses the tooltip around its contents.
        ".chakra-container .chakra-popover__content{"
        "padding-top:11px !important;padding-bottom:11px !important}"
        # Hide empty wrapper divs. The <nav> breadcrumb removal leaves an
        # empty `<div class=css-11mgymx></div>` as the first child of
        # `.css-old1by` with a baked 2.75rem (44 px) height — exactly the
        # T-overshoot observed (101 px vs target 56 px). `:empty` matches
        # divs with no child elements and no text, which is safe because
        # Chakra's layout divs always carry content when in use.
        ".chakra-container div:empty{display:none !important}"
        # `#sticky-header` (text-article-header-wrapper) ships padding-
        # top:24px to separate the sticky title bar from the content
        # above it. After chrome stripping there is nothing above it, so
        # that 24 px becomes extra T beyond the 56 px wrapper padding.
        "#sticky-header{padding-top:0 !important}"
        # Hide the "In This Article" TOC (the div that holds the nav h2).
        "div:has(> [data-atm=\"article-section-navigator-title\"]){"
        "display:none !important}"
        # The article column (chakra-stack wrapping header + body) ships
        # with width:70% inside a flex row shared with an empty 153-px
        # sidebar sibling. After sibling is hidden the 70% cap still
        # clamps content to ~510 px at any viewport. Two fixes:
        #   1. Collapse the flex row parent to block layout so the
        #      article column fills its full width (688 px inside the
        #      chakra-container's 16-px side padding).
        #   2. Drop the 70% width on the article column itself.
        # Target via the stable `.text-article-header-wrapper` class
        # rather than build-generated chakra css-xxxx ids.
        "div:has(> div > .text-article-header-wrapper){"
        "display:block !important}"
        "div:has(> .text-article-header-wrapper){"
        "width:100% !important;flex:1 1 auto !important}"
        # The header wrapper carried padding-bottom:16px to space itself
        # from the action-buttons block (now stripped). Reclaim it.
        # Also drop the 1-px box border (top/bottom — left/right are
        # already neutralized in the column-flat rule above) so the
        # article header reads as plain inline copy without a card-style
        # frame around it.
        ".text-article-header-wrapper{"
        "padding-bottom:0 !important;border:0 !important}"
        # Parent of `.text-article-header-wrapper` is a flex column with
        # `gap:24px` between siblings; that 24-px gap was visually
        # absorbed by the header card's bottom border in raw, but with
        # the border stripped it becomes 24 px of dead space between
        # the last header text and the Summary section. Zero the flex
        # gap on this specific stack so the only spacing above the
        # Summary heading is the publisher's natural 32-px padding-top
        # on the Summary's own chakra-stack (= raw's "summary to its
        # own block border" gap).
        ":root .chakra-stack:has(> .text-article-header-wrapper){"
        "gap:0 !important}"
        # Hide every sibling AFTER the "Reprints and Permissions" section
        # (Explore More Articles, ads). The Reprints block itself is part
        # of the main reading column and is kept.
        "div:has(> [data-atm=\"article-content-label-reprints and permissions\"]) ~ *{"
        "display:none !important}"
        # Authors + DOI + published-date block is collapsed by default
        # (.text-article-header-details-section gets max-height:0 +
        # overflow:hidden from site CSS to force a click-to-expand UX).
        # Force it open so readers see authors/affiliations + metadata.
        ".text-article-header-details-section{"
        "max-height:none !important;overflow:visible !important}"
        # Figures: jove inlines each figure as
        #   <p class=jove_content>
        #     <img class=xfigimg src="data:image/jpeg;base64,..." (medium-res)>
        #     <strong class=xfig>Figure N</strong>
        #     <strong>: Caption text.</strong>
        #     <a href=https://www.jove.com/files/ftp_upload/<id>/<id>fig<N>large.jpg>
        #       Please click here to view a larger version of this figure.</a>
        # Native rendering puts the image inline at intrinsic pixel
        # dimensions — narrower than the column. Force the img to
        # block-display at full column width above the inline caption.
        # The high-res JPEG is on the sibling <a href ending in .jpg> —
        # get_refs.py uses a browser-script to swap <img src> ← <a href>
        # at capture time so the inlined image is full-res; this CSS
        # handles the visual layout regardless.
        "p.jove_content img.xfigimg{"
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
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    JoVE quirks: citation_journal_abbrev absent, so journal falls back to
    citation_journal_title ("Journal of Visualized Experiments (JoVE)");
    citation_firstpage carries an "e"-prefix elocator (e.g. "e56001") that
    PubMed/refs.json strip to a bare numeric ("56001").
    """
    title = get_meta(html, "citation_title")

    date = get_meta(html, "citation_publication_date") or get_meta(html, "citation_online_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    # Strip elocator "e" prefix (e56001 -> 56001) to match refs.json.
    if firstpage and re.match(r"^e\d+$", firstpage):
        firstpage = firstpage[1:]
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")

    return {
        "title": title,
        "journal": journal.rstrip(".") if journal else "",
        "year": year,
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

# Surname particles (lowercase) that attach to the following token in
# compound surnames like "de Bono", "van der Berg", "de La Maza".
_SURNAME_PARTICLES = {
    "de", "del", "della", "dell'", "di", "da", "dos", "du",
    "van", "von", "vander", "der", "den", "ten", "ter",
    "la", "le", "el", "al", "zu", "af",
}


def _jove_format_name(name):
    """Convert 'Given Middle LastName' to 'LastName IN'.

    JoVE citation_author values are full names without commas. Handles
    compound surnames with particles; same logic as the JCI parser.
    """
    name = (name.replace("\u2018", "'").replace("\u2019", "'")
                .replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2009", " ").replace("\u00a0", " ")).strip()
    if not name:
        return ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0]

    i = len(parts) - 1
    surname_parts = [parts[i]]
    i -= 1
    while i >= 0 and parts[i].lower().rstrip(".") in _SURNAME_PARTICLES:
        surname_parts.insert(0, parts[i])
        i -= 1
    if (len(surname_parts) > 1 and i >= 1 and parts[i] and
            parts[i][0].isupper() and not parts[i].endswith(".")):
        surname_parts.insert(0, parts[i])
        i -= 1

    surname = " ".join(surname_parts)
    given = " ".join(parts[:i + 1])
    pieces = re.split(r"[\s.\-\u2010\u2011\u2012\u2013]+", given)
    initials = "".join(p[0] for p in pieces if p and p[0].isupper())[:2]
    return f"{surname} {initials}" if initials else surname


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Uses citation_author + citation_author_institution meta tag sequence.
    """
    return [
        {
            "author": _jove_format_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in parse_meta_authors(html)
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    JoVE pages are paywalled and truncate the reference list at 2-3 entries;
    what is visible in the <ol> inside the References section is captured.
    Each <li> follows the pattern:
      "Authors <a>Title.</a> <em>Journal</em>. <strong>Vol</strong> (Issue),
       pages (Year)."
    """
    refs = []
    hm = re.search(r'<h2[^>]*id=["\']?references["\']?[^>]*>', html)
    if not hm:
        return refs
    after = html[hm.end():]
    ol_m = re.search(r"<ol[^>]*>(.*?)</ol>", after, re.DOTALL)
    if not ol_m:
        return refs
    list_html = ol_m.group(1)

    for lm in re.finditer(r"<li[^>]*>(.*?)</li>", list_html, re.DOTALL):
        entry = lm.group(1)

        title = ""
        tm = re.search(r"<a[^>]*>(.*?)</a>", entry, re.DOTALL)
        if tm:
            title = strip_tags(tm.group(1)).strip().rstrip(".")

        journal = ""
        jm = re.search(r"<em>(.*?)</em>", entry, re.DOTALL)
        if jm:
            journal = strip_tags(jm.group(1)).strip().rstrip(".").rstrip(",")

        volume = ""
        vm = re.search(r"<strong>([^<]+)</strong>", entry)
        if vm:
            volume = unescape(vm.group(1)).strip()

        plain = strip_tags(entry).strip()
        plain = re.sub(r"\s+", " ", plain)

        year = ""
        ym = re.search(r"\((\d{4})\)", plain)
        if ym:
            year = ym.group(1)

        # Issue in parentheses after the volume, e.g. "5 (22)" or
        # "(22)" after <strong>5</strong>.
        issue = ""
        im = re.search(r"<strong>[^<]+</strong>\s*\(([^)]+)\)", entry)
        if im:
            issue = im.group(1).strip()

        # Pages: text like ", 1619-1622 (year)." or "Chapter 18 Unit 18 16".
        pages = ""
        pm = re.search(
            r"(\d+\s*[-\u2013]\s*\d+|[A-Za-z]+\s+\d+[^<()]{0,40})\s*\((\d{4})\)",
            plain,
        )
        if pm:
            pages = re.sub(r"[\u2013\u2014]", "-", pm.group(1)).strip().rstrip(".")

        # Authors: text before the <a> title tag
        authors = []
        before = entry.split("<a", 1)[0] if "<a" in entry else ""
        auth_text = strip_tags(before).strip().rstrip(",").strip()
        auth_text = re.sub(r"\s+", " ", auth_text)
        # JoVE format: "Surname, IN., Surname, IN., ..." with period-
        # delimited initials. Split at ", " and pair surname/initials.
        # Initials token may or may not have a trailing period on the last
        # initial (the preceding ", " splits the run at the previous dot).
        tokens = [t.strip() for t in auth_text.split(",") if t.strip()]
        initials_re = re.compile(r"^[A-Z]\.?(?:\s*[A-Z]\.?)*\.?$")
        i = 0
        while i < len(tokens):
            surname = tokens[i]
            if surname.lower().startswith("et al"):
                i += 1
                continue
            if i + 1 < len(tokens) and initials_re.match(tokens[i + 1]):
                initials = re.sub(r"[.\s]", "", tokens[i + 1])
                authors.append(f"{surname} {initials}" if initials else surname)
                i += 2
            else:
                authors.append(surname)
                i += 1

        # DOI (unlikely in JoVE refs)
        doi = ""
        dm = re.search(r"href=[\"']?https?://(?:dx\.)?doi\.org/([^\"'>\s]+)", entry)
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

def _extract_section(html, section_id):
    """Extract HTML for an h2 section by id, up to the next h2.

    Drops the leading paywall-overlay markers (div.article-protected-
    message-overlay blocks) and the MathJax containers rendered before
    each heading's body.
    """
    m = re.search(
        rf'<h2[^>]*id=["\']?{re.escape(section_id)}["\']?[^>]*>(.*?)</h2>',
        html, re.DOTALL,
    )
    if not m:
        return None, ""
    title = strip_tags(m.group(1)).strip()
    rest = html[m.end():]
    next_m = re.search(r"<h2[^>]*>", rest)
    end = next_m.start() if next_m else len(rest)
    return title, rest[:end]


def _strip_nested_tag(html, tag):
    """Remove all occurrences of a tag (including hyphenated names) and
    their nested content. Uses a manual depth counter so nested same-tag
    pairs are matched correctly — unlike _helpers._remove_nested_element
    which truncates hyphenated tag names on the \\w boundary.
    """
    open_pat = re.compile(rf"<{re.escape(tag)}\b[^>]*>", re.IGNORECASE)
    close_pat = re.compile(rf"</{re.escape(tag)}\s*>", re.IGNORECASE)
    out = []
    i = 0
    while i < len(html):
        om = open_pat.search(html, i)
        if not om:
            out.append(html[i:])
            break
        out.append(html[i:om.start()])
        pos = om.end()
        depth = 1
        while depth > 0 and pos < len(html):
            nxt_open = open_pat.search(html, pos)
            nxt_close = close_pat.search(html, pos)
            if nxt_close is None:
                pos = len(html)
                break
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                pos = nxt_open.end()
            else:
                depth -= 1
                pos = nxt_close.end()
        i = pos
    return "".join(out)


def _clean_section_html(section_html):
    """Strip MathJax chrome, the css-1hyfx7x math-row wrapper, and the
    paywall overlay paragraph. JoVE embeds MathJax sequences inside
    <div class=css-1hyfx7x> wrappers with literal comma separators
    between containers; drop the wrapper so those commas do not leak
    into main_text.
    """
    # Drop the css-1hyfx7x div which holds only MathJax rendering.
    def _drop_math_row(html_):
        open_pat = re.compile(
            r'<div[^>]*class=["\']?[^"\'>]*css-1hyfx7x[^"\'>]*["\']?[^>]*>',
        )
        while True:
            om = open_pat.search(html_)
            if not om:
                return html_
            # Find matching </div> with depth tracking
            pos = om.end()
            depth = 1
            div_open = re.compile(r"<div\b", re.IGNORECASE)
            div_close = re.compile(r"</div\s*>", re.IGNORECASE)
            while depth > 0 and pos < len(html_):
                nxt_o = div_open.search(html_, pos)
                nxt_c = div_close.search(html_, pos)
                if nxt_c is None:
                    pos = len(html_)
                    break
                if nxt_o and nxt_o.start() < nxt_c.start():
                    depth += 1
                    pos = nxt_o.end()
                else:
                    depth -= 1
                    pos = nxt_c.end()
            html_ = html_[:om.start()] + html_[pos:]
    section_html = _drop_math_row(section_html)

    # Also strip any remaining MathJax containers outside that wrapper.
    for tag in ("mjx-container", "mjx-assistive-mml", "mjx-math", "math"):
        section_html = _strip_nested_tag(section_html, tag)

    # Paywall overlay paragraph
    section_html = re.sub(
        r'<p[^>]*data-atm=["\']?article-protected-message-overlay["\']?[^>]*>'
        r'.*?</p>',
        "", section_html, flags=re.DOTALL,
    )
    return section_html


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    JoVE-specific: iterates the fixed set of <h2 id=...> article-content
    sections (Summary through Materials), skips References onward, and
    drops MathJax/paywall chrome from each section's body.
    """
    parts = []
    for sid in _MAIN_SECTION_IDS:
        title, content = _extract_section(html, sid)
        if not content:
            continue
        content = _clean_section_html(content)
        content = extract_captions(content)
        content = strip_common(content)
        text = tags_to_text(content)
        text = drop_noise(text, _NOISE)
        # Drop trailing truncation dots and whitespace
        text = re.sub(r"\.{4,}\s*$", "", text).strip()
        if not text:
            continue
        parts.append(f"## {title}\n\n{text}")
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse JoVE HTML into a papers/*.json-format dict."""
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
