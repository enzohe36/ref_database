"""<Publisher> (<second-level-domain>) HTML parser.

Author-name contract (applies to _parse_authors and _parse_references):
  1. Extract (given, surname) pairs from HTML. Prefer structured sources
     (separate given-name/surname tags, JSON keys) over combined strings.
  2. Call format_name(given, surname) to emit "Surname IN".
  3. If the HTML only exposes a combined 'Given Last' / 'Last, Given' /
     'Initials Last' string, pass it to format_author_name (which routes
     through parse_combined_name + format_name).
  4. Never tokenize, split, flip, or build initials inline. Compound
     surname prefixes and hyphenated given-name handling live in
     _helpers; parsers do not duplicate that logic.
"""

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
    parse_meta_authors,
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
    """Normalize the saved HTML to a single centered text column.

    Follow the 4-step workflow from the format-html skill
    (.claude/skills/format-html/SKILL.md):

        Step 1 — Render the raw HTML at vw = 720 to see the publisher's
                 narrow layout. That is the baseline you will match.
        Step 2 — Freeze that layout with a fluid-with-cap body (html 100%,
                 body max-width 752 px) so desktop-layout media queries
                 stop firing.
        Step 3 — Remove chrome in five categories: top blocks, bottom
                 blocks, side columns, floating blocks, colored backgrounds.
                 Prefer DOM removal over CSS display:none.
        Step 4 — Cap the main text column at 752 px, 56 px top/bottom +
                 16 px side padding, centered, white background. Zero
                 inner-wrapper paddings and first/last-child margins if
                 they shrink or shift text.

    Preserve native typography. Unless the user asks otherwise, do not
    override the publisher's font family, font size, line height, letter
    spacing, or paragraph spacing — inject layout/visibility CSS only.

    Ask the user to identify visually impairing elements before implementing
    — do not guess. Every change must preserve bit-identical parse_article
    output.
    """
    # -------------------------------------------------------------------
    # Step 3 — strip chrome. Replace the placeholder selectors with the
    # publisher's actual markers. Prefer the _helpers.py helpers over raw
    # regex; anchor class patterns with \b.
    # -------------------------------------------------------------------
    # (3a) Top blocks: cookie banner, site header, leaderboard ad, breadcrumbs.
    # html = remove_elements_by_id(html, "COOKIE_BANNER_ID")
    # html = _remove_nested_element(html, r'<header\b[^>]*>')
    # html = _remove_nested_element(html, r'<div[^>]*\bclass="[^"]*\bBREADCRUMB\b[^"]*"[^>]*>')
    #
    # (3b) Bottom blocks: site footer, related-articles, sign-up CTAs.
    # html = _remove_nested_element(html, r'<footer\b[^>]*>')
    #
    # (3c) Side columns: left nav, right sidebar.
    # html = remove_elements_by_id(html, "LEFT_SIDEBAR_ID", "RIGHT_SIDEBAR_ID")
    #
    # (3d) Floating blocks: sticky toolbars, dismiss buttons, overlays.
    # html = _remove_nested_element(html, r'<div[^>]*\bclass="[^"]*\bFLOATING_TOOLBAR\b[^"]*"[^>]*>')
    #
    # (3e) Colored backgrounds: branded masthead strips. Usually caught
    # above as top/bottom/side blocks; if a colored band survives, target
    # it here.

    # -------------------------------------------------------------------
    # Steps 2 + 4 — inject layout-freeze CSS and cap the main wrapper.
    # Replace MAIN_WRAPPER_SELECTOR with the highest common ancestor of
    # title + authors + affiliations + abstract + body + references +
    # figure captions.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        # Layout freeze (Step 2): fluid html, body fluid with 752-px cap.
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important;color:#000 !important}"
        # Capped reading column (Step 4).
        "MAIN_WRAPPER_SELECTOR{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # If T or B exceed ±4 tolerance after Step 4, see SKILL.md
        # § Pitfalls for first-/last-child margin/padding resets.
        # Use the direct-child combinator `>` (descendant `*:last-child
        # { padding-bottom: 0 }` collapses nested bordered-box
        # interiors). Conditional — drop entirely when not needed.
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
      - title: str without trailing period
      - journal: ISO abbreviation if the publisher exposes one (e.g.
        citation_journal_abbrev), else the full journal title verbatim,
        with dots stripped. Never hardcode an abbreviation or maintain
        a title->abbreviation map; each publisher emits one journal-name
        style consistently across its journals.
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    """
    date = get_meta(html, "citation_publication_date") or get_meta(html, "citation_online_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    journal = journal.replace(".", "") if journal else ""

    title = get_meta(html, "citation_title")
    title = title.rstrip(".").rstrip() if title else ""

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

    Two patterns — pick the one that matches the HTML source. Both
    produce the canonical "LastName IN" output via shared helpers in
    _helpers. Do not tokenize, split, flip, or build initials inline.

    Pattern A — HTML exposes separate given/surname fields (preferred).
    Use this whenever the publisher emits <span class=given-name> /
    <span class=surname> tags, schema.org familyName/givenName, JSON
    blobs with explicit keys, or any other structured pair:

        for m in re.finditer(r'...given-name...>([^<]*).*surname...>([^<]*)<', html):
            given = unescape(m.group(1)).strip()
            surname = unescape(m.group(2)).strip()
            authors.append({
                "author": format_name(given, surname),
                "affiliation": [...],
            })

    Pattern B — HTML only exposes combined name strings. When the only
    source is citation_author / dc.contributor meta tags (forms like
    "Given Last", "Last, Given", or "JD Griffith"), delegate the split
    to parse_combined_name via format_author_name:

        return [
            {
                "author": format_author_name(a["name"]),
                "affiliation": a.get("affiliations", []),
            }
            for a in parse_meta_authors(html)
        ]

    Prefer Pattern A when the HTML supports it — it avoids the surname-
    boundary guesswork inherent in combined strings. format_name and
    parse_combined_name together handle hyphenated given names (Jean-
    Baptiste → JB), Unicode hyphens, dotted initials (J.B. → JB),
    already-compact initials (JA stays JA), compound surname prefixes
    (de Lange, d'Adda di Fagagna, Nick McElhinny), and trailing
    generational suffixes (Jr., III). Extending that coverage happens
    in _helpers, never in individual parsers.
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

def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper
    (notably title without trailing period, journal as ISO abbreviation
    when the source exposes one and full title otherwise — same
    dot-stripping rules enforced centrally in clean_parsed_output),
    with one exception: authors is a list of "LastName IN" strings
    (plain strings, not dicts with affiliation).
    Empty fields are "". Empty authors is [].

    Reference-author names almost always arrive as combined strings
    ("JD Griffith" in citation_reference meta, "Boulé J.-B." in body
    text). Pass them through format_author_name — never implement
    inline initial-building or surname detection here.
    """
    return []


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    """
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse <Publisher> HTML into a papers/*.json-format dict."""
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
