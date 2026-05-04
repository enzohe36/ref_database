"""Dove Medical Press (dovepress.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
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
    "Download Article",
    "Cite this article",
    "Get Permission",
    "Fulltext",
    "Metrics",
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

def remove_banners(html):
    """Normalize Dovepress HTML to a single centered text column.

    Dovepress chrome to strip:
      - <div id=mobile-bg>, <div class=top_bg>, <div class=mast_bg>:
        site mobile menu, top nav, journal masthead.
      - <aside>: left-rail metadata sidebar.
      - <p class=back>: breadcrumb above the article body.
      - <div id=btn-readspeaker>: audio "Listen" button.
      - <div class=mobile-social>, <div class=tabs print-hide>: share +
        cite tabs at top of article.
      - <div class=rs_skip> "Download Article" button.
      - <p class=article-cc-license>: Creative Commons banner after
        references.
      - <footer> (×2 — site nav + copyright bar).
    """
    html = neutralize_media_queries(html)

    # Top-of-page chrome blocks ----------------------------------------
    html = remove_elements_by_id(html, "mobile-bg", "modal_wrapper")
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\btop_bg\b',
        )
        if html == before:
            break
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bmast_bg\b',
        )
        if html == before:
            break
    # Left-rail metadata sidebar (volume archive, related articles).
    html = _remove_nested_element(html, r"<aside\b[^>]*>")
    # Breadcrumb / back link above the article body.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html, r'<p\b[^>]*\bclass=["\']?back["\']?[^>]*>',
        )
        if html == before:
            break
    # Listen / ReadSpeaker audio button.
    html = remove_elements_by_id(html, "btn-readspeaker")
    # Mobile-only share-button row at the top of the article header
    # (small redundant clone of the .tabs row's share button). The
    # `<div class="tabs print-hide">` tab strip (Fulltext / Metrics /
    # Get Permission / Cite this article) is kept — it belongs in the
    # reading column.
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass=["\']?[^"\'>]*\bmobile-social\b',
        )
        if html == before:
            break
    # NOTE: do not strip <div class=rs_skip> (Download Article [PDF])
    # or <p class=article-cc-license> (Creative Commons license + © 2026
    # Dove Medical Press attribution block). They are part of the
    # reading column the user expects to see.
    # Site footers (mobile-friendly nav + copyright). There are two
    # <footer> elements at the bottom of the page.
    for _ in range(3):
        before = html
        html = _remove_nested_element(html, r"<footer\b[^>]*>")
        if html == before:
            break

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        # Layout freeze (Step 2).
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Reset every wrapper between <body> and the cap target so
        # they neither narrow the column (.grid is 90% width by
        # default) nor inset the content (.tabs-padding pads 20.625
        # px on every side). Use :root prefix to beat single-class
        # publisher rules without needing inline-style hooks.
        ":root #page,:root .grid_bg,:root .grid,"
        ":root #content,:root #html-readaloud-text,"
        ":root .tabs-bg,:root .tab-content,"
        ":root .articles{"
        "width:auto !important;max-width:100% !important;"
        "margin:0 !important;padding:0 !important;"
        "background:#fff !important}"
        # Cap the reading column on .tabs-padding — the highest
        # common ancestor of the article-type tag, title, byline,
        # body, and references.
        ":root .tabs-padding{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;"
        # pt trimmed to 46 to absorb the 10-px line-box descent above
        # the "Original Research" tag's small-caps glyph; pb trimmed
        # to 43 to absorb the trailing <p>'s 9-px margin-bottom on the
        # last reference plus the .tab-content's 9-px tail gap.
        "padding:46px 16px 43px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        ":root .tabs-padding>*:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        ":root .tabs-padding>*:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # Figures: dovepress wraps each figure in
        #   <table class=thumbnail-table>
        #     <tr><td><a class=float_border href=<HIRES_JPG>>
        #            <img class=imgthubnail src=<thumbnail data URL>></a></td>
        #         <td><p class=tabtext>Figure N caption text</p></td></tr>
        # Native layout puts thumbnail (150 px wide, capped via the
        # publisher's `.float_border{width:150px}` rule) on the left and
        # caption text on the right. Block-stack so the image sits above
        # the caption at full column width. The high-res JPEG URL is on
        # the parent <a href> — get_refs.py needs a browser-script
        # rewrite to swap <img src> ← parent <a href> for full-res
        # capture; this CSS only handles the layout, so until the
        # capture rule lands, the image is the inlined thumbnail
        # scaled up.
        ":root .tabs-padding table.thumbnail-table,"
        ":root .tabs-padding table.thumbnail-table tbody,"
        ":root .tabs-padding table.thumbnail-table tr,"
        ":root .tabs-padding table.thumbnail-table td{"
        "display:block !important;width:100% !important;"
        "text-align:left !important;"
        "box-sizing:border-box !important}"
        ":root .tabs-padding table.thumbnail-table a.float_border{"
        "display:block !important;width:auto !important;"
        "max-width:100% !important;margin:0 !important}"
        ":root .tabs-padding table.thumbnail-table img.imgthubnail,"
        ":root .tabs-padding table.thumbnail-table img.imgsmall{"
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

    Dovepress exposes everything via citation_* meta tags. citation_journal_abbrev
    is a short journal code ("BCTT") rather than the NLM MedAbbr; the post-parser
    NLM lookup normalizes citation_journal_title to the canonical
    "Breast Cancer (Dove Med Press)" form.
    """
    title = get_meta(html, "citation_title")
    title = unescape(title).strip().rstrip(".") if title else ""

    journal = get_meta(html, "citation_journal_title")
    journal = unescape(journal).replace(".", "").strip() if journal else ""

    year = ""
    date = get_meta(html, "citation_publication_date")
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = ""
    if firstpage and lastpage and firstpage != lastpage:
        pages = f"{firstpage}-{lastpage}"
    elif firstpage:
        pages = firstpage

    return {
        "title": title,
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

    Dovepress encodes author names twice and affiliations once:
      - citation_author meta tags carry full given+surname strings
        ("Joshua Behar"), in document order.
      - The article body opens with a `<p>` of the form
        "Joshua Behar,<sup>1,</sup><sup>*</sup> Christine Shiang,
        <sup>1,</sup><sup>*</sup> ... John T Lafin<sup>4</sup>"
        followed by `<sup>N</sup>Department of ..., ...; <sup>2</sup>...`
        listing each affiliation by superscript number.

    Strategy: parse each citation_author into "LastName IN" via
    format_author_name, then walk the body byline to collect each
    author's superscript numbers, then map to the matching affiliation
    string via the numbered list.
    """
    names = [
        format_author_name(unescape(v))
        for v in _citation_authors(html)
    ]
    if not names:
        return []

    body_block, aff_block = _author_byline_blocks(html)
    aff_map = _parse_affiliation_map(aff_block) if aff_block else {}

    body_authors = _walk_byline(body_block) if body_block else []
    keys_by_index = [keys for _name, keys in body_authors]

    authors = []
    for i, name in enumerate(names):
        keys = keys_by_index[i] if i < len(keys_by_index) else []
        affiliations = [aff_map[k] for k in keys if k in aff_map]
        authors.append({"author": name, "affiliation": affiliations})
    return authors


def _citation_authors(html):
    """Return the value strings of every citation_author meta tag, in order."""
    out = []
    for m in re.finditer(
        r'<meta[^>]*name=["\']?citation_author["\']?[^>]*content=["\']([^"\']*)["\']',
        html,
    ):
        out.append(m.group(1).strip())
    return out


def _author_byline_blocks(html):
    """Locate the body byline + affiliation list inside <div class=article-inner_html>.

    The first paragraph in `<div class=article-inner_html>` follows this
    pattern (one HTML element per author chunk separated by commas):

        <p>Author1<sup>n,</sup><sup>*</sup> Author2<sup>n</sup> ... <br><br>
        <sup>1</sup>Aff1<sup>2</sup>Aff2<br><br>
        *These authors contributed equally...
        Correspondence: ...
        <strong>Purpose:</strong> ...

    Returns (byline_html, affiliations_html). The byline ends at the
    first `<br><br>`; the affiliation list ends at the SECOND `<br><br>`
    so that the corresponding-author / abstract content does not bleed
    into affiliation parsing.
    """
    m = re.search(
        r'<div[^>]*\bclass=article-inner_html[^>]*>\s*<p\b[^>]*>(.*?)</p>',
        html, re.DOTALL,
    )
    if not m:
        return (None, None)
    p = m.group(1)
    br_re = re.compile(r"<br\s*/?>\s*<br\s*/?>", re.IGNORECASE)
    sm = br_re.search(p)
    if not sm:
        return (p, "")
    byline = p[:sm.start()]
    rest = p[sm.end():]
    # Affiliation block ends at the next <br><br> (which separates
    # affiliations from "*These authors contributed equally" /
    # "Correspondence" / abstract).
    sm2 = br_re.search(rest)
    aff_block = rest[:sm2.start()] if sm2 else rest
    return (byline, aff_block)


def _walk_byline(byline):
    """Walk a Dovepress byline into [(name, [sup_keys])] author entries.

    Dovepress encodes "Name1,<sup>aff,</sup><sup>aff</sup> Name2,..." —
    sups appear AFTER each name's closing comma, but the affiliation
    keys belong to the preceding name. The walker reads each name's
    visible text, consumes the trailing comma, then collects every
    immediately-following <sup>...</sup> block (digits or symbols).
    """
    out = []
    i = 0
    n = len(byline)
    while i < n:
        while i < n and byline[i].isspace():
            i += 1
        if i >= n:
            break
        # Accumulate name characters until we hit a comma, a <sup>, or a
        # block-level break (which shouldn't occur inside the byline).
        name_start = i
        while i < n and byline[i] not in ",<":
            i += 1
        name = byline[name_start:i].strip().rstrip(",")
        # Consume the trailing comma after the name (if any).
        if i < n and byline[i] == ",":
            i += 1
        # Consume any number of <sup>...</sup> blocks, including
        # whitespace between them.
        sups = []
        while i < n:
            j = i
            while j < n and byline[j].isspace():
                j += 1
            if byline[j:j + 5].lower() != "<sup>":
                break
            close = byline.lower().find("</sup>", j + 5)
            if close == -1:
                break
            content = byline[j + 5:close]
            text = strip_tags(content).strip().rstrip(",.")
            for tok in re.split(r"[,\s]+", text):
                tok = tok.strip()
                if tok:
                    sups.append(tok)
            i = close + 6
        if name:
            out.append((name, sups))
    return out


def _parse_affiliation_map(aff_html):
    """Parse the affiliation list HTML into {key: text}.

    The list is a single inline run starting with <sup>1</sup> and
    delimited by subsequent <sup>N</sup> markers, e.g.:

        <sup>1</sup>Department of Internal Medicine, ...;
        <sup>2</sup>Department of Medicine, ...;
        <sup>3</sup>Department of Molecular Biology, ...;

    Returns an OrderedDict-like dict keyed on the superscript label.
    """
    aff_map = {}
    # Find each <sup>...</sup> position; the affiliation text is the
    # plaintext between this <sup> and the next (or end of list).
    sups = list(re.finditer(r"<sup\b[^>]*>(.*?)</sup>", aff_html, re.DOTALL))
    for i, sm in enumerate(sups):
        key = strip_tags(sm.group(1)).strip().rstrip(",.")
        if not key.isdigit():
            # Skip non-numeric markers (e.g. "*" footnote anchors).
            continue
        body_start = sm.end()
        body_end = sups[i + 1].start() if i + 1 < len(sups) else len(aff_html)
        body = aff_html[body_start:body_end]
        body = strip_tags(body)
        body = unescape(re.sub(r"\s+", " ", body)).strip().rstrip(";,. ")
        if body:
            aff_map[key] = body
    return aff_map


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_reference_block(content_html):
    """Parse a single <p id=cit\\d+ class=$reftext>...</p> reference body.

    Format observed across all dovepress refs:
        "<a name=cit0001 href=#ref-cit0001>1.</a> Authors. Title.
         <em>Journal</em>. YYYY, MonDD;Vol(Issue):Pages. doi:10.xxxx/..."

    Title may itself contain a colon. Authors are space- or
    comma-separated "LastName Initials" tokens with intermittent
    "Author1, Initials" forms when the publisher's data extraction
    introduces a stray comma. Keep best-effort: split on commas,
    strip "et al" tails.
    """
    out = {
        "title": "", "journal": "", "year": "",
        "volume": "", "issue": "", "pages": "",
        "doi": "", "authors": [],
    }
    # Drop the leading <a name=cit0001>1.</a> citation-number anchor.
    content = re.sub(
        r"<a\b[^>]*\bname=cit\d+[^>]*>.*?</a>",
        "", content_html, count=1, flags=re.DOTALL,
    )

    # DOI: trailing "doi:10.xxxx/..." (sometimes prefixed with another
    # "doi: " from buggy publisher data).
    text = unescape(re.sub(r"\s+", " ", strip_tags(content))).strip()
    dm = re.search(r"doi:\s*(?:doi:\s*)?(10\.[^\s,;]+)", text, re.IGNORECASE)
    if dm:
        out["doi"] = format_doi(dm.group(1).rstrip(".,"))
        text = text[:dm.start()].rstrip(" .;")

    # Journal in <em>...</em>
    jm = re.search(r"<em>(.*?)</em>", content, re.DOTALL)
    if jm:
        out["journal"] = strip_tags(jm.group(1)).strip().rstrip(".")

    # Split head/tail at <em>.
    if jm:
        head_html = content[:jm.start()]
        tail_html = content[jm.end():]
    else:
        head_html = content
        tail_html = ""
    head = unescape(re.sub(r"\s+", " ", strip_tags(head_html))).strip().rstrip(". ")
    tail = unescape(re.sub(r"\s+", " ", strip_tags(tail_html))).strip()
    # Strip the trailing doi:... if it slipped into tail.
    tail = re.sub(r"\s*doi:.*$", "", tail, flags=re.IGNORECASE).strip(" .,;")

    # Authors / title from head: the title is the last sentence terminated
    # by a period before the journal italic. Walk backward from the end.
    authors_str, title = _split_dovepress_head(head)
    out["title"] = title
    out["authors"] = [
        format_author_name(a) for a in _split_dovepress_authors(authors_str)
        if a.strip()
    ]

    # Tail: "YYYY, MonDD;Vol(Issue):Pages" or "YYYY;Vol:Pages"
    # The "MonDD" component is optional and Dovepress-specific.
    ym = re.match(
        r"^(\d{4})(?:,\s*[^;]+)?\s*;\s*(\d+\w*)"
        r"(?:\s*\(([^)]+)\))?"
        r"(?:\s*:\s*([\w\-–—.]+))?",
        tail,
    )
    if ym:
        out["year"] = ym.group(1)
        out["volume"] = ym.group(2) or ""
        out["issue"] = ym.group(3) or ""
        out["pages"] = (ym.group(4) or "").replace("–", "-").rstrip(".")
    else:
        ym2 = re.search(r"(\d{4})", tail)
        if ym2:
            out["year"] = ym2.group(1)

    return out


def _split_dovepress_head(head):
    """Split 'Authors. Title' head into (authors_str, title).

    Dovepress titles end at the period before the <em>Journal</em>
    block. The author list boundary sits at the last "LastName Initials"
    token whose trailing punctuation is `.` followed by a space — that
    period closes the author run and the title starts immediately after.
    "et al." is also recognized as a terminator.
    """
    # If "et al" appears, the author list ends at the end of that phrase.
    # Some Dovepress refs duplicate the marker (e.g. "...Mahalakshmi, S
    # et al, et al. Differential ..."), so consume every consecutive
    # "et al" run plus its surrounding punctuation before slicing.
    em = re.search(
        r"\bet\s+al\.?(?:\s*[,;]?\s*\bet\s+al\.?)*\s*[,.;]?\s*",
        head, re.IGNORECASE,
    )
    if em:
        authors_str = head[:em.start()].rstrip(" .,")
        title = head[em.end():].lstrip(" .,").rstrip(".")
        return authors_str + " et al", title

    # Walk every "LastName Initials" match; the last match whose trailing
    # boundary is a period (followed by a space and a capital letter) marks
    # the end of the author list. Accept "," followed by another author as
    # a non-terminating boundary so the loop keeps walking.
    last_end = 0
    for m in re.finditer(
        r"\b[A-Z][\w\-']+(?:\s+[A-Z][\w\-']+)*\s+[A-Z]{1,5}\b(?!\w)",
        head,
    ):
        end = m.end()
        if end >= len(head):
            last_end = end
            continue
        nxt = head[end:end + 2]
        if nxt.startswith(",") or nxt.startswith("."):
            last_end = end

    if not last_end:
        return head.strip(), ""
    return head[:last_end].strip(), head[last_end:].lstrip(" .,").rstrip(".")


def _split_dovepress_authors(authors_str):
    """Split an authors string into canonical 'LastName Initials' tokens.

    Dovepress mixes three encodings within the same reference's author list:
      A. "Schmid P, Cortes J, Pusztai L"                  (LastName Initials)
      B. "McArthur, H, Kümmel, S, Bergh, J"               (LastName, Initials)
      C. "Dreyer, Marie, Hatogai, Ken, Hall, Katie"       (LastName, FirstName)

    Forms A/B/C are all comma-separated. Walk the comma-split list with
    one-token lookahead:
      - Multi-word chunks (Form A, e.g. "Schmid P") emit as-is.
      - Single-word chunk + initials lookahead (Form B) → merge with
        a space.
      - Single-word chunk + capitalized first-name lookahead (Form C)
        → call format_name(given, surname) to build canonical initials.
      - Trailing "et al" tokens are dropped.

    Output strings are already in "LastName Initials" form, so the
    caller can apply format_author_name idempotently.
    """
    tokens = []
    for raw in authors_str.split(","):
        tok = re.sub(r"\bet\s+al\.?\s*$", "", raw, flags=re.IGNORECASE).strip()
        if tok and not re.match(r"^et\s+al\.?$", tok, re.IGNORECASE):
            tokens.append(tok)

    initials_re = re.compile(r"^[A-Z]\.?(?:\s*[A-Z]\.?){0,3}$")
    given_re = re.compile(r"^[A-Z][a-zA-ZÀ-ſ\-']+$")

    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Form A: "Surname Initials" already in canonical multi-word form.
        if " " in tok:
            out.append(tok)
            i += 1
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        # Form B: "Surname, Initials" — initials carry no spaces, no
        # lowercase. Must check Form B before Form C because a single-
        # capital token (e.g. "S") matches the Form-C regex too.
        if nxt and initials_re.match(nxt):
            out.append(f"{tok} {nxt}")
            i += 2
            continue
        # Form C: "Surname, GivenName" — given name is a capitalized
        # word with lowercase tail (e.g. "Marie", "Ken"). Build
        # canonical "Surname I" via format_name.
        if nxt and given_re.match(nxt):
            out.append(format_name(nxt, tok))
            i += 2
            continue
        # Single-word token alone — emit as a surname-only entry.
        out.append(tok)
        i += 1
    return out


def _parse_references(html):
    """Extract Dovepress reference list."""
    refs = []
    for m in re.finditer(
        r'<p\b[^>]*\bid=cit\d+\b[^>]*>(.*?)</p>',
        html, re.DOTALL,
    ):
        refs.append({"": _parse_reference_block(m.group(1))})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _strip_inline_chrome(html):
    """Remove inline chrome from the article body.

    Dovepress wraps each figure/table in
        <table class=thumbnail-table>
            <tbody><tr>
                <td>...thumbnail image...</td>
                <td><p class=tabtext><strong>Figure N</strong> caption text</p></td>
            </tr></tbody>
        </table>
    The thumbnail image is decorative; the `<p class=tabtext>` caption
    is the figure/table caption and must survive into main_text.
    Strategy: replace the entire `<table class=thumbnail-table>` element
    with its inner caption paragraph(s).
    """
    def _replace(m):
        body = m.group(0)
        captions = re.findall(
            r'<p\b[^>]*\bclass=tabtext[^>]*>.*?</p>',
            body, re.DOTALL,
        )
        return "\n".join(captions)
    html = re.sub(
        r"<table\b[^>]*\bclass=thumbnail-table[^>]*>.*?</table>",
        _replace, html, flags=re.DOTALL,
    )
    return html


def _parse_main_text(html):
    """Extract body text from <div class=article-inner_html>.

    Boundaries:
      - Start: the first structured-abstract label inside the article
        body (e.g. "<strong>Purpose:</strong>"), so the byline,
        affiliation list, and correspondence paragraph that share the
        opening `<p>` are dropped from main_text.
      - End: just before the references section (`<h2>References</h2>`).
    Reference paragraphs are bounded inside the same wrapper but appear
    after `<h2>References</h2>`, so the end-cut takes care of them.
    """
    am = re.search(
        r'<div[^>]*\bclass=article-inner_html[^>]*>(.*?)</div>\s*</div>',
        html, re.DOTALL,
    )
    if not am:
        return ""
    body = am.group(1)

    # Cut at the start of the References section if present.
    rm = re.search(r"<h2>\s*References\s*</h2>", body)
    if rm:
        body = body[:rm.start()]

    # Trim the byline+affiliations+correspondence preamble. Dovepress
    # structured abstracts open with one of these <strong> labels.
    abstract_re = re.compile(
        r"<strong>\s*(?:"
        r"Purpose|Aim|Aims|Objective|Objectives|Background|Introduction|"
        r"Abstract|Patients\s+and\s+Methods|Materials\s+and\s+Methods|"
        r"Methods)\s*:\s*</strong>",
        re.IGNORECASE,
    )
    sm = abstract_re.search(body)
    if sm:
        # Slice at the keyword itself — the byline / affiliations /
        # correspondence preamble share the same wrapping <p>, so
        # anchoring on the surrounding <p> would re-include them.
        body = body[sm.start():]

    body = _strip_inline_chrome(body)
    body = extract_captions(body)
    body = strip_common(body)
    text = tags_to_text(body)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Dovepress HTML into a papers/*.json-format dict."""
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
