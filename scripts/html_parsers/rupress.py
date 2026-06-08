"""Rockefeller University Press (rupress) HTML parser."""

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
    "Open in a new tab",
    "Google Scholar",
    "Crossref",
    "Search ADS",
    "PubMed",
    "OpenURL",
    "WorldCat",
)

# Reference section heading pattern
_REF_RE = re.compile(r'\breferences\b', re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# h2 classes for body vs back matter vs references.
# rupress emits `<h2 class="back<name>-title ...">` for every back-matter
# heading (backreferences-title, backacknowledgements-title, etc.); the
# generic `_BACK_PREFIX` catches all of them so Acknowledgments / Author
# Notes / Footnotes are recognized as back matter rather than `other`.
_BODY_HEADING = "section-title"
_BACK_PREFIX = "back"
_REF_HEADING = "backreferences-title"
_ABSTRACT_HEADING = "abstract-title"


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------


def remove_banners(html):
    """Apply Phase 2 layout rules for rupress.org (Silverchair).

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies.
    Step 2: remove the OneTrust cookie banner. The whole consent SDK
            wrapper (`#onetrust-consent-sdk`) carries the banner, the
            preference-center modal, and its dark-filter backdrop —
            removing the wrapper drops all three in one shot.
    Step 3: remove sticky elements detected by scan_sticky.py — the
            focus-only `<a class=skipnav>` (position: fixed top:-100,
            unquoted-class `<a>` so the by-selector helper does not
            match it), the `#InfoColumn` left article-navigation column
            (position: fixed; the publisher's narrow CSS translates it
            off-screen but the wrapper still ships sticky), and the
            entire `widget-ArticleJumpLinks` widget. Removing
            `#InfoColumn` also cleans up its `info-inner-wrap can-stick`
            child.
    Step 3 (cont.): rupress saves the page with the Hypothes.is
            annotation client embedded — `<hypothesis-sidebar>`,
            `<hypothesis-adder>`, `<hypothesis-notebook>`,
            `<hypothesis-profile>`, `<hypothesis-highlight-cluster-toolbar>`
            tags ship with shadow-rooted UI. The sidebar paints a fixed
            right-edge column ~22 px wide that reaches to the viewport
            edge at every width, breaking the centering invariant.
            Strip all hypothesis-* custom elements.
    Step 5: remove ad blocks. rupress uses the same Silverchair classes
            as ASH/AACR/biologists/portlandpress: `ad-banner` and
            `widget-AdBlock`. Both can appear multiple times.
    Step 8: figure CSS so each image sits above its caption, full width
            of the column. Same Silverchair markup as siblings.
    Step 9: per-author affiliation popups (`.al-author-info-wrap`) are
            floating tooltip cards (position:absolute, z-index:1200,
            box-shadow, fixed pixel width ~290-320), NOT push-down
            expansions — leaving them collapsed. Affiliations are
            already extracted from publisher metadata by
            `_parse_authors`, so the popup contributes nothing.
    """
    html = neutralize_media_queries(html)

    # Step 2 — OneTrust cookie consent SDK (banner + preference-center
    # modal + dark-filter backdrop all live under #onetrust-consent-sdk).
    html = remove_elements_by_id(html, "onetrust-consent-sdk")

    # Step 3 — sticky elements detected by scan_sticky. rupress ships
    # these with unquoted attribute values, so the by-selector helper
    # (which matches double-quoted class on `<div>`) does not apply
    # for the `<a>` skip link.
    html = _remove_nested_element(
        html, r'<a\s[^>]*\bclass=skipnav\b[^>]*>',
    )
    # `#InfoColumn` is the left article-navigation column (position:
    # fixed; translated off-screen by narrow-form CSS but still sticky).
    # Removing it also strips the inner `info-inner-wrap can-stick`
    # child and the widget-ArticleJumpLinks flyout living inside it.
    html = remove_elements_by_id(html, "InfoColumn")
    html = remove_elements_by_selector(html, "widget-ArticleJumpLinks")

    # Step 3 (cont.) — Hypothes.is annotation client. The custom
    # elements ship with `<template shadowrootmode=open>` UI; the
    # sidebar in particular paints a thin fixed column at the right
    # viewport edge that breaks the centering invariant. Strip all
    # five tags. `_remove_nested_element`'s `\w+` tag-name capture
    # does not match hyphenated custom elements (`hypothesis-sidebar`
    # captures only `hypothesis`), so iterate with a local nesting walk
    # over the actual hyphenated tag name.
    for tag in (
        "hypothesis-sidebar", "hypothesis-adder", "hypothesis-notebook",
        "hypothesis-profile", "hypothesis-highlight-cluster-toolbar",
    ):
        open_re = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
        close_re = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
        while True:
            m = open_re.search(html)
            if not m:
                break
            pos = m.end()
            depth = 1
            while depth > 0 and pos < len(html):
                no = open_re.search(html, pos)
                nc = close_re.search(html, pos)
                if nc is None:
                    break
                if no and no.start() < nc.start():
                    depth += 1
                    pos = no.end()
                else:
                    depth -= 1
                    pos = nc.end()
            html = html[:m.start()] + html[pos:]

    # Step 5 — ad blocks. Loop because the helper removes one element
    # per call and multiple instances exist.
    for cls in ("ad-banner", "widget-AdBlock"):
        while True:
            prev = html
            html = remove_elements_by_selector(html, cls)
            if html == prev:
                break

    override = (
        "<style>"
        "html{margin:0!important;padding:0!important;"
        "background:#fff!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "background:#fff!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # Step 8 — figures. Same Silverchair markup as ASH/AACR/biologists/
        # portlandpress:
        #   <div class="fig fig-section"> > <div class=fig-label>
        #     <div class=graphic-wrap> > <a><img class=content-image></a>
        #     <div class="caption fig-caption">caption</div>
        ".fig.fig-section,.graphic-wrap"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;"
        "margin-left:0!important;margin-right:0!important;"
        "padding-left:0!important;padding-right:0!important;"
        "box-sizing:border-box!important;}"
        ".graphic-wrap a,.graphic-wrap img.content-image"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 12px 0!important;"
        "box-sizing:border-box!important;}"
        ".fig-caption"
        "{display:block!important;width:100%!important;"
        "margin-left:0!important;margin-right:0!important;}"
        # Table-wrap shells use absolute pixel widths from the publisher
        # PDF that overflow the narrow column. Cap them to the parent.
        ".table-wrap,.table-wrap-inner,.table-wrap table,table.table-wrap"
        "{max-width:100%!important;width:auto!important;"
        "margin-left:0!important;margin-right:0!important;"
        "table-layout:auto!important;}"
        ".table-wrap{overflow-x:auto!important;}"
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

def _parse_citation_primary(html):
    """Parse journal, volume, issue, pages, DOI from ww-citation-primary div.

    Format: '<em>Journal</em>, Volume X, Issue Y, Date, Pages N-M, <a href=DOI>'
    or: '<em>Journal</em>, Volume X, Issue Y, Date, elocator, <a href=DOI>'
    """
    m = re.search(
        r'class=ww-citation-primary[^>]*>(.*?)</div>',
        html,
        re.DOTALL,
    )
    if not m:
        return {}, ""

    content = m.group(1)

    # Journal name from <em>
    journal = ""
    jm = re.search(r'<em>(.*?)</em>', content)
    if jm:
        journal = strip_tags(jm.group(1)).strip()

    # DOI from link
    doi = ""
    dm = re.search(r'href=["\']?(https?://doi\.org/[^"\'>\s]+)', content)
    if dm:
        doi = dm.group(1)

    text = strip_tags(content).strip()

    # Volume
    volume = ""
    vm = re.search(r'Volume\s+(\d+)', text)
    if vm:
        volume = vm.group(1)

    # Issue
    issue = ""
    im = re.search(r'Issue\s+(\d+)', text)
    if im:
        issue = im.group(1)

    # Pages
    pages = ""
    pm = re.search(r'Pages?\s+(\S+)', text)
    if pm:
        pages = pm.group(1).rstrip(",")
        # Normalize dash
        pages = re.sub(r'[–—]', '-', pages)
    if not pages:
        # eLocator ID: token before DOI link, after date
        # e.g. "..., 24 April 2025, gkaf299, https://doi.org/..."
        em = re.search(r',\s*([a-z]{2,}[\d]+)\s*,\s*https?://doi', text)
        if em:
            pages = em.group(1)
    if not pages:
        # Royal Society format: "Journal (YYYY) Vol (Issue): elocator ."
        em = re.search(r'\(\s*\d+\s*\)\s*:\s*(\S+?)\s*\.?$', text.strip())
        if em:
            pages = em.group(1).rstrip(".,")

    # Year from date string (e.g. "30 October 2015" or "July 2019")
    year = ""
    ym = re.search(r'(\d{4})', text)
    if ym:
        year = ym.group(1)

    return {
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
    }, doi


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    rupress-specific: tries citation_ meta tags first (newer format), falls back to
    ww-citation-primary div (older format).
    """
    # Try meta tags first
    title = get_meta(html, "citation_title")
    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    volume = get_meta(html, "citation_volume")
    issue = get_meta(html, "citation_issue")
    doi_raw = get_meta(html, "citation_doi")
    date = get_meta(html, "citation_publication_date")
    year = ""
    if date:
        ym = re.search(r"(\d{4})", date)
        if ym:
            year = ym.group(1)

    # Fall back to ww-citation-primary for missing fields
    if not title or not journal:
        citation, _ = _parse_citation_primary(html)
        if not title:
            m = re.search(r'og:title[^>]*content="([^"]+)"', html)
            if m:
                title = unescape(m.group(1)).strip()
        if not journal:
            journal = citation.get("journal", "")
        if not volume:
            volume = citation.get("volume", "")
        if not issue:
            issue = citation.get("issue", "")
        if not year:
            year = citation.get("year", "")
        if not doi_raw:
            doi_raw = citation.get("doi", "")

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage
    if not pages:
        citation, _ = _parse_citation_primary(html)
        pages = citation.get("pages", "")

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": format_doi(doi_raw),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

# rupress meta tags glue ROR identifier URLs to the head of affiliation
# strings with no separator ("https://ror.org/00bvhmc43Cancer Immunity
# Laboratory ..."). Stripped per-affiliation before storage.
_ROR_PREFIX_RE = re.compile(r"^\s*https?://ror\.org/[0-9a-z]{9,}\s*")


def _clean_aff(aff):
    """Strip leading ROR identifier URL from an affiliation string."""
    if not aff:
        return aff
    return _ROR_PREFIX_RE.sub("", aff).strip(" ,")


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name.
    Tries citation_author meta tags first (newer rupress format with comma-
    separated LastName, Given). Falls back to al-author-name links
    (older format, full display names).

    When meta citation_author_institution is missing for an author
    (common on older NAR / Genetics papers), falls back to the
    <div class="info-card-author"> author-popup widget which carries
    each author's full-name display plus an optional <div class=aff>.

    rupress-specific: newer papers (2024+) embed ROR identifier URLs
    inline at the head of each affiliation string with no separator
    (e.g. "https://ror.org/00bvhmc43Cancer Immunity Laboratory ...").
    Strip those prefixes via _clean_aff before returning.
    """
    # Build name -> affiliation lookup from info-card author widgets.
    info_card_affs = {}
    for m in re.finditer(
        r'<div\s+class="info-card-author[^"]*"[^>]*>(.*?)(?=<div\s+class="info-card-author|\Z)',
        html, re.DOTALL,
    ):
        block = m.group(1)
        name_m = re.search(
            r'<div\s+class=info-card-name[^>]*>\s*([^<]+?)\s*(?:<|$)',
            block,
        )
        if not name_m:
            continue
        display = unescape(name_m.group(1)).strip()
        if not display:
            continue
        affs = []
        # <div class=aff> may contain a nested <div class=institution>
        # that closes before the outer aff div. Walk the block with a
        # depth counter so the full aff text (institution + address) is
        # captured rather than just the institution stub.
        pos = 0
        while pos < len(block):
            am = re.search(r'<div\s+class=aff\b[^>]*>', block[pos:])
            if not am:
                break
            start = pos + am.end()
            depth = 1
            p = start
            while depth > 0 and p < len(block):
                no = re.search(r'<div[\s>]', block[p:])
                nc = re.search(r'</div>', block[p:])
                if not nc:
                    break
                if no and no.start() < nc.start():
                    depth += 1
                    p += no.end()
                else:
                    depth -= 1
                    if depth == 0:
                        inner = block[start:p + nc.start()]
                    p += nc.end()
            else:
                inner = block[start:p]
            # Drop <span class="label title-label">N</span> superscripts
            txt = re.sub(r'<span[^>]*class="?label[^>]*>.*?</span>', '', inner, flags=re.DOTALL)
            txt = unescape(re.sub(r'<[^>]+>', ', ', txt))
            txt = re.sub(r'\s*,\s*,\s*', ', ', txt).strip(' ,')
            txt = re.sub(r'\s+', ' ', txt).strip()
            if txt:
                affs.append(_clean_aff(txt))
            pos = p
        if affs:
            info_card_affs[display] = affs

    def lookup_info_card(meta_name):
        """Match a citation_author display name to the info-card lookup.
        citation_author is "Last, Given"; info-card is "Given Last".
        """
        if meta_name in info_card_affs:
            return info_card_affs[meta_name]
        if "," in meta_name:
            last, given = meta_name.split(",", 1)
            flipped = f"{given.strip()} {last.strip()}"
            if flipped in info_card_affs:
                return info_card_affs[flipped]
        return []

    # Try meta tags first
    meta_authors = parse_meta_authors(html)
    if meta_authors:
        result = []
        for a in meta_authors:
            affs = a.get("affiliations", [])
            if affs:
                affs = [_clean_aff(x) for x in affs]
            if not affs:
                affs = lookup_info_card(a["name"])
            result.append({
                "author": format_author_name(a["name"]),
                "affiliation": affs,
            })
        # Shared-affiliation broadcast. Two safe patterns:
        # Rule A: when >=2 authors already carry the same single
        #   affiliation string, broadcast it to empty-aff authors.
        #   Skips cases where different authors have different affs
        #   (don't overwrite publisher-specified mappings) and cases
        #   where only one author has an aff (avoids attributing a
        #   single corresponding-author lab to unrelated co-authors).
        # Rule B: when ONLY the last author carries affs and >=2 other
        #   authors are empty (typical of older OUP meta where the
        #   citation_author_institution tags all cluster at the end
        #   and parse_meta_authors attaches them to the last author),
        #   broadcast the last author's affs to all empty authors.
        #   Narrow on purpose — requires last-author-only, not
        #   mid-stream singletons (Wang_2006 where the single aff
        #   belongs to a non-last author stays unbroadcast).
        with_affs = [a for a in result if a["affiliation"]]
        unique_affs = {tuple(a["affiliation"]) for a in with_affs}
        if len(with_affs) >= 2 and len(unique_affs) == 1:
            shared = list(next(iter(unique_affs)))
            for a in result:
                if not a["affiliation"]:
                    a["affiliation"] = list(shared)
        elif (len(result) >= 3 and result[-1]["affiliation"]
                and len(with_affs) == 1
                and sum(1 for a in result if not a["affiliation"]) >= 2):
            shared = list(result[-1]["affiliation"])
            for a in result:
                if not a["affiliation"]:
                    a["affiliation"] = list(shared)
        return result

    # Fallback: body HTML author links
    authors = []
    seen = set()
    for m in re.finditer(
        r'class="al-author-name[^"]*"[^>]*>.*?'
        r'<a[^>]*>([^<]+)</a>',
        html,
        re.DOTALL,
    ):
        name = unescape(m.group(1)).strip()
        if name and name not in seen and not name.startswith("http"):
            seen.add(name)
            affs = info_card_affs.get(name, [])
            authors.append({
                "author": name,
                "affiliation": affs,
            })
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
    Each reference has structured fields: surname, given-names, article-title,
    source, year, volume, fpage, lpage.
    """
    refs = []
    # Find ref-list div (quoted or unquoted class attribute)
    m = re.search(r'class=(?:"[^"]*ref-list[^"]*"|ref-list\b)', html)
    if not m:
        return refs

    ref_section = html[m.start():]

    # Split by ref-item boundaries (each ref lives in a js-splitview-ref-item).
    # Match only ref-item divs to exclude footnotes/copyright that follow.
    items = list(re.finditer(
        r'<div\s+content-id=\S+\s+class=js-splitview-ref-item\b', ref_section,
    ))
    if not items:
        # Portland Press and older OUP: data-content-id attribute, no
        # js-splitview-ref-item class on the outer wrapper.
        items = list(re.finditer(r'<div\s+(?:data-)?content-id=\S+', ref_section))
    if not items:
        return refs

    # Find end boundary for the last ref entry: the first div after it
    # that is NOT a ref-item (copyright, widget, footnote, etc.).
    last_end = len(ref_section)
    after_last = ref_section[items[-1].end():]
    boundary = re.search(
        r'<div\s+class=["\']?(?:widget|copyright|license|footnote)\b',
        after_last,
    )
    if boundary:
        last_end = items[-1].end() + boundary.start()

    for idx, im in enumerate(items):
        end = items[idx + 1].start() if idx + 1 < len(items) else last_end
        entry = ref_section[im.start():end]

        # Structured fields. Inner content may contain tags (e.g. <em>),
        # so match lazily up to </div> and strip any inner tags.
        def _field(cls):
            fm = re.search(
                rf'class=(?:"{cls}"|{cls}\b)[^>]*>(.*?)</div>',
                entry, re.DOTALL,
            )
            return strip_tags(fm.group(1)).strip() if fm else ""

        title = _field("article-title")
        if not title:
            title = _field("chapter-title")

        journal = _field("source").replace(".", "")
        volume = _field("volume")
        issue = _field("issue")
        year = _field("year")
        fpage = _field("fpage")
        lpage = _field("lpage")
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage

        # Fallback: extract pages from inline text (older format uses
        # <strong>vol</strong>, pages instead of fpage/lpage divs)
        if not pages:
            pm = re.search(
                r'<strong>\s*\d+\s*</strong>\s*,\s*'
                r'(\d[\d\w]*)\s*[–—-]\s*(\d[\d\w]*)',
                entry,
            )
            if pm:
                pages = f"{pm.group(1)}-{pm.group(2)}"
            else:
                pm = re.search(
                    r'[\s,;:]\s*(\d+)\s*[–—-]\s*(\d+)\s*\.?\s*(?:<|$)',
                    entry,
                )
                if pm:
                    pages = f"{pm.group(1)}-{pm.group(2)}"

        # Authors. Surname and given-names divs may be separated by commas,
        # whitespace, or just adjacent; allow up to 20 chars between them.
        # Given-names may be full names ("Thomas A.") or already-compacted
        # initials ("WE", "J.W.", "T"); handle both.
        authors = []
        for nm in re.finditer(
            r'class=(?:"surname"|surname\b)[^>]*>([^<]*)</div>'
            r'.{0,20}?'
            r'class=(?:"given-names"|given-names\b)[^>]*>([^<]*)</div>',
            entry, re.DOTALL,
        ):
            surname = unescape(nm.group(1)).strip().rstrip(",")
            given = unescape(nm.group(2)).strip()
            authors.append(format_name(given, surname))

        # DOI (matches both doi.org and dx.doi.org)
        doi = ""
        dm = re.search(r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry)
        if dm:
            doi = format_doi(unescape(dm.group(1)))
        if not doi:
            # Fallback: DOI in comment div (e.g. "[Epub ahead of print; doi:10.1234/...]")
            dm = re.search(r'doi:\s*(10\.\S+?)[\])<,\s]', entry)
            if dm:
                doi = format_doi(unescape(dm.group(1)))

        # Fallback: full text
        if not title and not authors:
            title = strip_tags(entry).strip()

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

def _parse_abstract(html):
    """Extract abstract from rupress HTML."""
    # Match <section class=abstract ...> (unquoted) that is NOT a graphical
    # abstract.  OUP uses unquoted class=abstract for the text abstract and
    # quoted class="abstract ... graphicalAbstract" for graphical ones.
    for m in re.finditer(
        r'<section\s+class=(["\']?)abstract\1[^>]*>(.*?)</section>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        tag = html[m.start():m.start() + 120].lower()
        if 'graphical' in tag:
            continue
        text = strip_tags(m.group(2)).strip()
        if text:
            return text
    # Fallback: section after abstract-title h2
    m = re.search(
        r'class="[^"]*abstract-title[^"]*"[^>]*>.*?</h2>\s*<section[^>]*>(.*?)</section>',
        html,
        re.DOTALL,
    )
    if m:
        return strip_tags(m.group(1)).strip()
    return ""


def _parse_keywords(html):
    """Extract keywords from rupress HTML (often absent).

    Tries kwd-group/kwd-part (classic Silverchair) first, then
    content-metadata-keywords (Royal Society / Portland Press variant).
    """
    keywords = []
    m = re.search(
        r'class=["\']?kwd-group[^>]*>(.*?)</div>',
        html,
        re.DOTALL,
    )
    if m:
        for km in re.finditer(r'class=["\']?kwd-part[^>]*>(.*?)</(?:span|a)>', m.group(1), re.DOTALL):
            kw = strip_tags(km.group(1)).strip().rstrip(",")
            if kw:
                keywords.append(kw)
    if not keywords:
        # RSP/PP markup: content-metadata-keywords-title <div> followed by
        # one or more <a class=content-metadata--item>...</a> entries.
        tm = re.search(
            r'class=["\']?content-metadata-keywords-title[^>]*>.*?</div>',
            html, re.DOTALL,
        )
        if tm:
            tail = html[tm.end():tm.end() + 4000]
            # Stop scanning at the next non-keyword block
            stop = re.search(r'</div>\s*</div>', tail)
            scope = tail[:stop.start()] if stop else tail
            for am in re.finditer(
                r'<a[^>]*class=["\']?content-metadata--item[^>]*>(.*?)</a>',
                scope, re.DOTALL,
            ):
                kw = strip_tags(am.group(1)).strip().rstrip(",")
                if kw:
                    keywords.append(kw)
    return keywords


def _extract_div_content(html, start_pos):
    """Extract full content of a div starting at start_pos (after opening tag).

    Walks nested <div> tags to find the matching closing </div>, so that
    chrome that lives outside the article-body div (email alerts, sign-in
    panels, "Recommended" widgets, etc.) is excluded.
    """
    pos = start_pos
    depth = 1
    while depth > 0 and pos < len(html):
        next_open = re.search(r'<div[\s>]', html[pos:])
        next_close = re.search(r'</div>', html[pos:])
        if next_close is None:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos += next_open.end()
        else:
            depth -= 1
            if depth == 0:
                return html[start_pos:pos + next_close.start()]
            pos += next_close.end()
    return html[start_pos:pos]


def _find_h2_sections(html):
    """Find section headings in rupress HTML.

    Returns list of (start_pos, heading_text, class_type) where class_type
    is 'body', 'back', 'ref', 'abstract', or 'other'.
    """
    entries = []
    for m in re.finditer(
        r'<h2[^>]*class=(?:"([^"]*)"|(\S+))[^>]*>(.*?)</h2>',
        html, re.DOTALL,
    ):
        cls = m.group(1) or m.group(2) or ""
        text = strip_tags(m.group(3)).strip()
        if not text:
            continue
        if _ABSTRACT_HEADING in cls:
            kind = "abstract"
        elif _REF_HEADING in cls or _REF_RE.search(text):
            kind = "ref"
        elif _BACK_PREFIX in cls and "-title" in cls:
            kind = "back"
        elif _BODY_HEADING in cls:
            kind = "body"
        else:
            kind = "other"
        entries.append((m.start(), text, kind))
    return entries


def _parse_body(html):
    """Extract the body-zone text (between abstract and references).

    Boundary rules:
    - Start: after Abstract section.
    - End: above the last references heading.
    - Remove inner references sections.
    - Everything else between start and end is retained.
    """
    # Work within article-body (matches quoted "article-body ..." and
    # "body main-article-body" class variants, plus Portland Press's
    # unquoted class=article-body). Use nesting-aware extraction so
    # chrome that follows the article-body div (email alerts, sign-in
    # modals, "Recommended" widgets) is not captured.
    body_m = re.search(
        r'<div[^>]*class=(?:"[^"]*article-body[^"]*"|article-body\b)[^>]*>',
        html,
    )
    if not body_m:
        return ""

    content = _extract_div_content(html, body_m.end())
    h2s = _find_h2_sections(content)
    if not h2s:
        return ""

    # Start: after abstract
    start = 0
    for pos, text, kind in h2s:
        if kind == "abstract":
            # Skip past the abstract section
            sec_end = content.find('</section>', pos)
            if sec_end >= 0:
                start = sec_end + len('</section>')
            else:
                start = pos + 200
        elif kind in ("body", "back"):
            break

    # Find first references heading
    first_ref_idx = None
    for i, (pos, text, kind) in enumerate(h2s):
        if kind == "ref" or _REF_RE.search(text):
            first_ref_idx = i
            break

    # Build body from two zones
    parts = []

    # Capture un-headed intro text between abstract and first body h2
    # (some OUP journals omit the "Introduction" heading).
    # Skip metadata panels (keywords, issue section) that may appear
    # before the actual intro text. Cap the gap at the first references
    # heading so commentary articles (which lack body H2s but have
    # back-matter H2s after refs) don't drag the references list into
    # the gap as fallback intro content.
    first_body_pos = None
    for pos, text, kind in h2s:
        if kind in ("body", "back") and pos >= start:
            first_body_pos = pos
            break
    if first_ref_idx is not None:
        ref_pos = h2s[first_ref_idx][0]
        if first_body_pos is None or first_body_pos > ref_pos:
            first_body_pos = ref_pos
    if first_body_pos is not None and start < first_body_pos:
        gap = content[start:first_body_pos]
        # Skip past article-metadata-panel if present
        meta_end = re.search(
            r'class="[^"]*article-metadata-panel[^"]*"[^>]*>',
            gap,
        )
        if meta_end:
            # Find the closing </div> for the metadata panel by locating
            # the first <p> tag after it (actual body text)
            first_p = re.search(r'<p\b', gap[meta_end.end():])
            if first_p:
                intro_start = start + meta_end.end() + first_p.start()
                if intro_start < start + first_body_pos:
                    parts.append((intro_start, first_body_pos))
        else:
            parts.append((start, first_body_pos))

    for i, (pos, text, kind) in enumerate(h2s):
        # Skip abstract and references
        if kind == "abstract" or kind == "ref" or _REF_RE.search(text):
            continue
        # Skip anything before start (e.g. graphical abstract between
        # the text abstract and the first body heading)
        end_pos = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)
        if pos < start:
            continue
        if first_ref_idx is None or i < first_ref_idx:
            # Zone 1: keep everything
            parts.append((pos, end_pos))
        else:
            # Zone 2: keep only supplementary
            if _SUPP_RE.search(text):
                parts.append((pos, end_pos))

    # Fallback: no sections matched, use start to first ref
    if not parts:
        end_pos = len(content)
        if first_ref_idx is not None:
            end_pos = h2s[first_ref_idx][0]
        if start < end_pos:
            parts.append((start, end_pos))

    if not parts:
        return ""

    body_html = ""
    for s, e in parts:
        body_html += content[s:e]

    # Some Silverchair publishers embed raw HTML inside attribute values
    # (e.g. data-section-title="Rnaseh2c<sup>-/-</sup> mice"). The unescaped
    # < character breaks the tags_to_text regex for headings, so strip these
    # attributes before downstream processing.
    body_html = re.sub(
        r'\s+data-section-title="[^"]*"',
        '',
        body_html,
    )
    body_html = re.sub(
        r"\s+data-section-title='[^']*'",
        '',
        body_html,
    )

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    rupress-specific: main_text is composed of abstract + keywords + body (with
    markdown headers "## Abstract" and "## Keywords" prepended to their sections).
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append(f"## Abstract\n\n{abstract}")
    keywords = _parse_keywords(html)
    if keywords:
        parts.append(f"## Keywords\n\n{', '.join(keywords)}")
    body = _parse_body(html)
    if body:
        parts.append(body)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Rockefeller University Press HTML into a papers/*.json-format dict."""
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
