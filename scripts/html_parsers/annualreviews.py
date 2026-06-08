"""Annual Reviews (annualreviews.org) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    affiliation_from_email,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    format_name,
    get_meta,
    neutralize_media_queries,
    parse_meta_authors,
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
)

_NOISE = (
    "Go to section...",
    "Open in a new tab",
    "[PubMed]",
    "[Medline]",
    "[Web of Science]",
    "[Google Scholar]",
    "[Citing articles]",
    "CLOSE ×",
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for annualreviews.org.

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies.
    Step 2: remove the cookie-bar (no separate backdrop on this site).
    Step 3: remove sticky elements — the article-navigation-bar (nav
            tabs Info/Figures/References/Cited-By that pin to the top)
            and the right-rail sidebar_right that holds tools, social
            share, altmetrics, and "Journal News" advertising.
    Step 4: covered by Step 3 (sidebar_right is also the tall sidebar).
    Step 5: covered by Step 3 (the journal-news ad lives inside
            sidebar_right; nothing else carries an ad-naming
            convention in main column or article-body).
    Steps 6, 7: no-op (no colored bg or shadow around main column;
            figures inline images are usable resolution).
    Step 8: figure CSS — image fills column, image above caption,
            12 px gap. Annual Reviews markup ships caption BEFORE
            image inside `<div class="figure ...">`, so reorder via
            CSS flex column-reverse with `.caption` and `.image` as
            children.
    Step 9: force-show affiliations / citation popup that the publisher
            collapses by default (`.showhide .minus` is display:none in
            the publisher CSS — override to display:block).
    Steps 10-12: scan_gaps clean across all three test fixtures.
    """
    html = neutralize_media_queries(html)

    # Step 2 — cookie consent banner (no separate backdrop on this site).
    html = remove_elements_by_id(html, "cookie-bar")

    # Step 3 / 4 / 5 — sticky chrome and tall right-rail sidebar
    # (which also contains the in-page Journal News ad).
    html = remove_elements_by_id(html, "sidebar_right")
    html = _remove_nested_element(
        html,
        r'<nav[^>]*\bclass="[^"]*\barticle-navigation-bar\b[^"]*"[^>]*>',
    )

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
        # Step 8 — figures: image above caption, both fill column.
        # Annual Reviews markup is
        #   <div class="figure html-fulltext-responsive-figure">
        #     <div class=caption>...</div>
        #     <div class=image><a><img></a></div>
        #   </div>
        # Use flex column-reverse so the .image child renders above the
        # .caption child without DOM mutation.
        ".figure.html-fulltext-responsive-figure"
        "{display:flex!important;flex-direction:column-reverse!important;"
        "width:100%!important;max-width:100%!important;"
        "margin:0 0 16px 0!important;}"
        ".figure.html-fulltext-responsive-figure .image"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;margin:0 0 12px 0!important;}"
        ".figure.html-fulltext-responsive-figure .image img"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;}"
        ".figure.html-fulltext-responsive-figure .caption"
        "{display:block!important;width:100%!important;}"
        # Step 9 — expand collapsed author affiliations / citation popup.
        # Publisher CSS: `.showhide .minus { display: none }`.
        ".showhide .minus,.showhide .js-minus,.showhide .js-description"
        "{display:block!important;visibility:visible!important;"
        "opacity:1!important;}"
        # Hide the +/− toggle chrome since the content is always shown.
        ".showhide .js-plus,.showhide .fa-plus-circle,"
        ".showhide .fa-minus-circle"
        "{display:none!important;}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
    return html
def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Uses standard citation_* meta tags.
    """
    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_date")
            or get_meta(html, "citation_online_date")
            or get_meta(html, "citation_year"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)
    if not year:
        year = get_meta(html, "citation_year")

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    journal = journal.rstrip(".") if journal else ""

    # Annual Reviews sets citation_issue to strings like "Volume 42, 2008"
    # rather than an issue number. Strip it unless it's a bare integer.
    issue = get_meta(html, "citation_issue")
    if issue and not re.fullmatch(r'\s*\d+\s*', issue):
        issue = ""

    return {
        "title": get_meta(html, "citation_title"),
        "journal": journal,
        "year": year,
        "volume": get_meta(html, "citation_volume"),
        "issue": issue,
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers.

    Compound-surname particles (de, van, etc.) are handled by
    parse_combined_name + format_name in _helpers.
    """
    return format_author_name(name)


_CURRENT_ADDRESS_RE = re.compile(r"^\W*\d*\s*current\s*address\s*:", re.IGNORECASE)


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Uses citation_author + citation_author_institution meta tags. Annual
    Reviews stores names as "Given Last" (first-last), not "Last, Given",
    so convert via display-to-initials helper instead of format_author_name
    directly (which expects a comma).

    Drops 'Current address:' note-style institutions that publishers
    sometimes emit in place of the primary affiliation — for these,
    falls back to email-domain inference so the primary lab is
    attributed instead of a post-publication relocation note.
    """
    authors = []
    for a in parse_meta_authors(html):
        affs = [
            af for af in a.get("affiliations", [])
            if not _CURRENT_ADDRESS_RE.match(af)
        ]
        authors.append({
            "author": _display_to_initials(a["name"]),
            "affiliation": affs,
        })
    # Email-domain inference for authors left with no aff after the
    # current-address filter. Pulls the correspondence email from the
    # HTML body (mailto: links) since Annual Reviews rarely sets
    # citation_author_email meta.
    if any(not a["affiliation"] for a in authors):
        body_email = ""
        em = re.search(r"mailto:([^\"'\s>]+)", html)
        if em:
            body_email = em.group(1)
        aff = affiliation_from_email(body_email)
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

    Structure:
      <ol id=articlereference class=articlereference-vancouver>
        <li class=refbody id=ref-B1>
          <span class=reference-surname>...</span>
          <span class=reference-given-names>...</span>, ...
          <span class=reference-year>YYYY</span>.
          <span class=reference-article-title>Title</span>
          <span class=reference-source><span class=reference-italic>Journal</span></span>
          <span class=reference-volume>Vol</span>:<span class=reference-fpage>X</span>-<span class=reference-lpage>Y</span>
        </li>
    """
    m = re.search(r'<ol\s+id="?articlereference"?[^>]*>', html)
    if not m:
        return []
    # Scope to </ol>
    pos = m.end()
    depth = 1
    end = len(html)
    while depth > 0:
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
                end = pos + nc.start()
            pos += nc.end()
    refs_html = html[m.end():end]

    refs = []
    # ID variants seen: B1 (older), ref-B1 (newer), R1
    li_starts = list(re.finditer(
        r'<li\s+class="?refbody"?\s+id="?(?:ref-)?[A-Za-z]?\d+"?[^>]*>', refs_html
    ))
    for i, li_m in enumerate(li_starts):
        li_end = li_starts[i + 1].start() if i + 1 < len(li_starts) else len(refs_html)
        entry = refs_html[li_m.end():li_end]

        def _field(cls):
            fm = re.search(
                rf'<span\s+class="?reference-{cls}"?[^>]*>(.*?)</span>',
                entry, re.DOTALL,
            )
            return strip_tags(fm.group(1)).strip() if fm else ""

        title = _field("article-title")
        if not title:
            title = _field("chapter-title")
        year = _field("year")
        volume = _field("volume")
        fpage = _field("fpage").replace('\u2013', '').replace('\u2014', '').rstrip('-').strip()
        lpage = _field("lpage").replace('\u2013', '').replace('\u2014', '').lstrip('-').strip()
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # Source may contain a nested reference-italic span with journal text
        jm = re.search(
            r'<span\s+class="?reference-source"?[^>]*>(.*?)</span>\s*</span>',
            entry, re.DOTALL,
        )
        if not jm:
            jm = re.search(
                r'<span\s+class="?reference-source"?[^>]*>(.*?)</span>',
                entry, re.DOTALL,
            )
        journal = strip_tags(jm.group(1)).strip().rstrip('.') if jm else ""

        # Authors: pairs of reference-surname / reference-given-names
        authors = []
        for am in re.finditer(
            r'<span\s+class="?reference-surname"?[^>]*>([^<]*)</span>\s*'
            r'<span\s+class="?reference-given-names"?[^>]*>([^<]*)</span>',
            entry,
        ):
            surname = unescape(am.group(1)).strip()
            given = unescape(am.group(2)).strip().rstrip('.')
            authors.append(format_name(given, surname))

        # DOI
        doi = ""
        dm = re.search(r'href="?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1)))

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

def _extract_div(html, start_match):
    """Extract the div slice from its opening tag through its matching </div>."""
    pos = start_match.end()
    depth = 1
    end = len(html)
    while depth > 0:
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
    return html[start_match.end():end]


def _parse_main_text(html):
    """Extract body text.

    Boundary rules:
      - Use <div id=html-body> as the main body container (abstract + sections).
      - References and site chrome are excluded by container scoping.
      - Supplementary materials: scan for sections matching supplement / etc.
    """
    # Prefer html-body when present (contains its own Abstract + all sections)
    body_m = re.search(r'<div\s+[^>]*id="?html-body"?[^>]*>', html)
    if body_m:
        body_html = _extract_div(html, body_m)
    else:
        # Fallback to abstract_content + article-level container
        abs_m = re.search(r'<div\s+id="?abstract_content"?[^>]*>', html)
        if not abs_m:
            return ""
        body_html = _extract_div(html, abs_m)

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Annual Reviews HTML into a papers/*.json-format dict."""
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
