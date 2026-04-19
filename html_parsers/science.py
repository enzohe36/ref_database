"""Science / AAAS (science.org) HTML parser. Also handles sagepub.com (shared Atypon Literatum layout)."""

import re
from html import unescape
from urllib.parse import parse_qs, urlparse

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "View all access and purchase options for this article.",
    "Get full access to this article",
    "Get Access",
    "Crossref",
    "Google Scholar",
    "PubMed",
    "Open URL",
    "View Article",
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

_SCIENCEADVISER_SIDEBAR_RE = re.compile(
    r'<div class="mb-2x mb-xl-3x">\s*'
    r'<div class="d-flex flex-column[^"]*">\s*'
    r'<h3[^>]*>Sign up for ScienceAdviser</h3>'
    r'.*?</div>\s*</div>',
    re.DOTALL,
)


def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    - onetrust-consent-sdk: OneTrust cookie banner ("Accept Non-Essential
      Cookies") on sagepub.com.
    - main-header: site-wide top bar (dark navbar with hamburger menu,
      header ads, quick search) on science.org.
    - header fixed: site-wide top bar (Sage Journals logo, Search,
      Access, Cart, nav) on sagepub.com.
    - data-core-nav=header: floating article nav bar (Contents menu
      button + section list + "Information & Authors" /
      "Metrics & Citations" icon pills) on science.org. The collateral
      panel content outside this bar is kept because _parse_authors
      reads affiliations from it.
    - alert-donation--visible: ScienceAdviser subscribe modal overlay.
    - mb-2x/d-flex sidebar block containing the "Sign up for
      ScienceAdviser" heading.
    """
    html = remove_elements_by_id(html, "onetrust-consent-sdk")
    html = _remove_nested_element(
        html,
        r'<header class="main-header\b[^"]*"[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<header class="header fixed"[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div data-core-nav=header\b[^>]*>',
    )
    html = _remove_nested_element(
        html,
        r'<div class="alert-donation bg-white alert-donation--visible"[^>]*>',
    )
    html = _SCIENCEADVISER_SIDEBAR_RE.sub("", html)
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _format_given_last(name):
    """Convert 'Given Last' or 'Last, Given' to 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_volume_issue(html):
    """Extract volume and issue from semantic spans in the body HTML."""
    volume = ""
    vm = re.search(
        r'property=volumeNumber[^>]*>([^<]+)</span>',
        html,
    )
    if vm:
        volume = vm.group(1).strip()
    issue = ""
    im = re.search(
        r'property=issueNumber[^>]*>([^<]+)</span>',
        html,
    )
    if im:
        issue = im.group(1).strip()
    return volume, issue


def _parse_pages_from_abstract(html):
    """Extract pages from the trailing 'Journal V, FP-LP.' line in the abstract.

    SAGE abstracts often end with the formal citation, e.g.
    '<i>Antioxid. Redox Signal.</i> 39, 411-431.'
    Falls back to <span property=pageStart>FP</span>-<span property=pageEnd>LP</span>
    used by science.org.
    """
    m = re.search(
        r'<i>[^<]+</i>\s*\d+,\s*(\d[\w\-\u2013\u2014]*\s*[-\u2013\u2014]\s*\d[\w]*)\.?',
        html,
    )
    if m:
        return m.group(1).replace("\u2013", "-").replace("\u2014", "-").replace(" ", "")
    fp_m = re.search(r'property=pageStart[^>]*>([^<]+)</span>', html)
    lp_m = re.search(r'property=pageEnd[^>]*>([^<]+)</span>', html)
    if fp_m and lp_m:
        return f"{fp_m.group(1).strip()}-{lp_m.group(1).strip()}"
    if fp_m:
        return fp_m.group(1).strip()
    # science.org Sci Adv format embeds an elocator after the volume <b>:
    #   "<i>Sci. Adv.</i></span><span ...><b>9</b>,</span><span ...>eadi4148</span>"
    em = re.search(
        r'<i>[^<]+</i>\s*</span>\s*<span[^>]*>\s*<b>\d+</b>\s*,?\s*</span>'
        r'\s*<span[^>]*>\s*([a-z]{2,}[\d\-]+)\s*</span>',
        html,
    )
    if em:
        return em.group(1).strip()
    return ""


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    SAGE uses dc.* meta tags for most fields. citation_journal_title is
    present; volume/issue/pages must be parsed from body HTML.
    """
    title = get_meta(html, "dc.Title") or get_meta(html, "citation_title")

    # Prefer the ISO abbreviation embedded as <i>Abbrev.</i> in the
    # closing line of the abstract ("<i>Antioxid. Redox Signal.</i> 39, ..."),
    # since meta tags only carry the full journal title.
    journal = ""
    abbrev_m = re.search(
        r'<i>([^<]+?)</i>\s*\d+,\s*\d[\w\-\u2013\u2014]*\s*[-\u2013\u2014]\s*\d',
        html,
    )
    if abbrev_m:
        journal = abbrev_m.group(1).strip()
    if not journal:
        journal = (get_meta(html, "citation_journal_abbrev")
                   or get_meta(html, "citation_journal_title")
                   or "")
    if journal:
        journal = re.sub(r"\s+", " ", journal.replace(".", "")).strip()

    # Date: dc.Date is "YYYY-MM" or YYYY
    date = get_meta(html, "dc.Date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    # DOI: prefer dc.Identifier scheme=doi (full DOI), falling back to
    # citation_doi, then publisher-id (which uses '_' in place of '/').
    doi_raw = ""
    dm = re.search(
        r'<meta[^>]*name=["\']?dc\.Identifier["\']?[^>]*scheme=["\']?doi["\']?'
        r'[^>]*content=["\']?([^"\'\s>]+)',
        html, re.IGNORECASE,
    )
    if dm:
        doi_raw = unescape(dm.group(1))
    if not doi_raw:
        doi_raw = get_meta(html, "citation_doi")
    if not doi_raw:
        # Publisher-id form like "10.1089_ars.2022.0105"
        pm = re.search(
            r'<meta[^>]*name=["\']?dc\.Identifier["\']?[^>]*'
            r'scheme=["\']?publisher-id["\']?[^>]*content=["\']?([^"\'\s>]+)',
            html, re.IGNORECASE,
        )
        if pm:
            v = unescape(pm.group(1))
            if v.startswith("10.") and "_" in v:
                doi_raw = v.replace("_", "/", 1)
    if not doi_raw:
        # Body HTML carries the DOI link
        bm = re.search(
            r'<a[^>]*href=(https?://doi\.org/[^>"\'\s]+)',
            html,
        )
        if bm:
            doi_raw = bm.group(1)

    volume, issue = _parse_volume_issue(html)
    pages = _parse_pages_from_abstract(html)

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

def _parse_authors(html):
    """Extract authors with affiliations.

    SAGE uses Schema.org markup:
      <div id=conN property=author typeof=Person>
        <span property=givenName>Given</span>
        <span property=familyName>Last</span>
        ...
        <div class=affiliations>
          <div ...><span property=name>Affiliation text</span></div>
    """
    authors = []
    # Find each con block
    for m in re.finditer(
        r'<div[^>]*id=con(\d+)[^>]*property=author[^>]*typeof=Person[^>]*>',
        html,
    ):
        con_n = int(m.group(1))
        # End at next con block
        next_m = re.search(
            rf'<div[^>]*id=con{con_n + 1}[^>]*property=author', html[m.end():]
        )
        end = m.end() + next_m.start() if next_m else m.end() + 5000
        block = html[m.start():end]

        gn = re.search(r'property=givenName[^>]*>([^<]+)', block)
        fn = re.search(r'property=familyName[^>]*>([^<]+)', block)
        if not gn or not fn:
            continue
        given = strip_tags(gn.group(1)).strip()
        family = strip_tags(fn.group(1)).strip()
        author = _format_given_last(f"{given} {family}")

        # Affiliations: <span property=name>...</span>
        affs = []
        for am in re.finditer(
            r'property=name[^>]*>(.*?)</span>',
            block, re.DOTALL,
        ):
            text = strip_tags(am.group(1)).strip()
            if text:
                affs.append(text)
        # Filter out duplicates and the author's own name (which also gets
        # property=name on Person elements)
        seen = set()
        clean = []
        for a in affs:
            if a == family or a == given or a in seen:
                continue
            seen.add(a)
            clean.append(a)

        authors.append({"author": author, "affiliation": clean})
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_one_reference(block):
    """Parse one <div id=BN class=citations> block.

    Returns dict {title, journal, year, volume, issue, pages, doi, authors}.
    """
    # Citation text
    cm = re.search(
        r'<div[^>]*class=["\']?citation-content["\']?[^>]*>(.*?)</div>',
        block, re.DOTALL,
    )
    raw_html = cm.group(1) if cm else ""
    raw_text = strip_tags(raw_html).strip()

    # DOI from Crossref link
    doi = ""
    dm = re.search(
        r'<a[^>]*href=(https?://doi\.org/[^>"\'\s]+)',
        block,
    )
    if dm:
        doi = unescape(dm.group(1))

    # Google Scholar lookup URL carries structured fields
    title = ""
    year = ""
    pages = ""
    journal = ""
    volume = ""
    issue = ""
    authors = []
    gs = re.search(
        r'href="(https?://scholar\.google\.com/scholar_lookup\?[^"]+)"',
        block,
    )
    if gs:
        params = parse_qs(urlparse(unescape(gs.group(1))).query)
        title = params.get("title", [""])[0]
        year = params.get("publication_year", [""])[0]
        pages = params.get("pages", [""])[0]
        journal = params.get("journal", [""])[0]
        volume = params.get("volume", [""])[0]
        issue = params.get("issue", [""])[0]
        authors = list(params.get("author", []))
        if not doi:
            gs_doi = params.get("doi", [""])[0]
            if gs_doi:
                doi = format_doi(gs_doi)

    # OpenURL link can also carry fields when scholar_lookup is absent
    if not title or not authors:
        ou = re.search(
            r'href="([^"]*search\.serialssolutions\.com[^"]*)"',
            block,
        )
        if ou:
            params = parse_qs(urlparse(unescape(ou.group(1))).query)
            if not title:
                title = params.get("rft.atitle", [""])[0]
            if not journal:
                journal = (params.get("rft.jtitle", [""])[0]
                           or params.get("rft.title", [""])[0])
            if not volume:
                volume = params.get("rft.volume", [""])[0]
            if not issue:
                issue = params.get("rft.issue", [""])[0]
            if not year:
                year = params.get("rft.date", [""])[0]
            if not pages:
                fp = params.get("rft.spage", [""])[0]
                lp = params.get("rft.epage", [""])[0]
                if fp and lp:
                    pages = f"{fp}-{lp}"
                elif fp:
                    pages = fp
            if not authors:
                first = params.get("rft.aufirst", [""])[0]
                last = params.get("rft.aulast", [""])[0]
                if last:
                    authors = [f"{last} {first[:1]}".strip() if first else last]

    journal = re.sub(r"\s+", " ", journal.replace(".", "")).strip()

    # Old-style science.org citations omit the article title; the citation
    # text is just "Authors, <em>Journal</em> Vol, Pages (Year)." Both
    # scholar_lookup and OpenURL fill the journal abbreviation into the
    # generic `title` / `rft.title` slots, so `title` ends up echoing the
    # journal. Detect this by matching against the <em> abbreviation and
    # clear the spurious title. Also parse comma-separated author names
    # from the text before <em>, since the lookup URLs lack `author`.
    em_m = re.search(r'<em>([^<]+)</em>', raw_html)
    if em_m and title:
        em_text = re.sub(r"\s+", " ", em_m.group(1).replace(".", "")).strip()
        title_norm = re.sub(r"\s+", " ", title.replace(".", "")).strip()
        if em_text and em_text == title_norm:
            title = ""
            if not journal:
                journal = em_text
    if not authors and em_m:
        pre = strip_tags(raw_html[:em_m.start()]).strip().rstrip(",").strip()
        if pre:
            authors = [a.strip() for a in pre.split(",") if a.strip()]

    # Volume/issue often missing from scholar_lookup but present in the
    # citation-content text after the journal name. Two formats observed:
    #   SAGE:    "<em>Nucleic Acids Res</em>, 2012; 40(22):11531-11544;"
    #   science: "<em>Nat. Rev. Mol. Cell Biol.</em> <b>11</b>, 171-181 (2010)"
    if not volume:
        vm = re.search(
            r"</em>[,\s]*\d{4}\s*;\s*(\d+)(?:\(([^)]+)\))?\s*:\s*\d",
            raw_html,
        )
        if vm:
            volume = vm.group(1)
            if not issue and vm.group(2):
                issue = vm.group(2)
    if not volume:
        # science.org wraps the volume in <b> (sometimes nested <b><i>vol</i></b>)
        # right after </em>, with optional issue number in parentheses.
        vm = re.search(
            r"</em>\s*<b>\s*(?:<i>)?\s*(\d+)\s*(?:</i>)?\s*</b>"
            r"(?:\s*\(([^)]+)\))?",
            raw_html,
        )
        if vm:
            volume = vm.group(1)
            if not issue and vm.group(2):
                issue = vm.group(2)

    # Reformat author strings: "R. J. O'Sullivan" -> "O'Sullivan RJ".
    # Treat ALL leading short uppercase tokens (with optional trailing dot)
    # as initials, not just the first.
    norm_authors = []
    for a in authors:
        a = a.strip()
        if not a:
            continue
        parts = a.split()
        # Detect leading initial tokens. Clean each token of dots and hyphens
        # (incl. Unicode hyphens U+2010..U+2013) so hyphenated initials like
        # "M.-B.", "J.-P.", "A.-M." are recognized as 2-letter initial
        # groups ("MB", "JP", "AM") rather than falling through to the
        # unflipped fallback.
        def _initial_form(tok):
            return re.sub(r"[.\-\u2010\u2011\u2012\u2013]", "", tok)
        i = 0
        while i < len(parts):
            tok = _initial_form(parts[i])
            if tok.isupper() and 1 <= len(tok) <= 3 and tok.isalpha():
                i += 1
            else:
                break
        if 0 < i < len(parts):
            initials = "".join(_initial_form(parts[k]) for k in range(i))
            last = " ".join(parts[i:]).rstrip(".")
            norm_authors.append(f"{last} {initials}")
        else:
            norm_authors.append(a.rstrip("."))

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "authors": norm_authors,
    }, raw_text


def _parse_references(html):
    """Extract references from <div class=citations> blocks.

    Atypon-based sites expose each reference as <div class=citations>,
    usually with an id matching a reference-number prefix:
      - SAGE: B1, B2, ...
      - science.org (modern): R1, R2, ...
      - science.org (legacy): REF1, REF2, ...
    Some legacy Science papers (e.g. 2007-era) omit the id on specific
    citation divs, instead identifying the reference number via a
    sibling ``<div class=label>N</div>``. Match both layouts and
    deduplicate by reference number so the hidden/visible copies the
    Atypon template renders don't each count.
    """
    refs = []
    seen_numbers = set()
    # Walk every <div class=citations> opening in document order.
    # Atypon Science papers render each reference in the main list plus
    # a visible core-collateral duplicate (accessible from the text), and
    # some are additionally cloned as hidden screen-reader entries. Some
    # references (e.g. legacy Science papers) omit the id attribute and
    # instead carry a sibling <div class=label>N</div>. Dedupe by the
    # numeric reference key extracted from either the id or the label so
    # every cited work appears exactly once regardless of which Atypon
    # clone is visible.
    # Match divs whose class is exactly "citations" — multi-class divs
    # like class="citations to-citation__accordion external-links" are
    # empty UI chrome (accordions/tooltips) that should not be treated
    # as references.
    for m in re.finditer(
        r'<div(?P<attrs>\s+[^>]*?)'
        r'class=(?:"citations"|\'citations\'|citations(?=[\s>]))[^>]*>',
        html,
    ):
        attrs = m.group("attrs") or ""
        before = html[max(0, m.start() - 200):m.start()]
        # Skip hidden clones (screen-reader copies) since their text
        # duplicates a visible entry elsewhere.
        if re.search(r'role=listitem[^>]*\bhidden\b', before):
            continue
        # Derive a numeric reference key from the id ("REF4", "R1",
        # "B1", "core-collateral-REF4") or from the preceding
        # <div class=label>N</div> sibling for id-less entries.
        id_m = re.search(r'\bid=\S*?([A-Z]+)(\d+)\b', attrs)
        if id_m:
            key = int(id_m.group(2))
        else:
            label_m = re.search(
                r'<div\s+class=["\']?label["\']?[^>]*>\s*(\d+)\s*</div>\s*$',
                before,
            )
            if not label_m:
                continue
            key = int(label_m.group(1))
        if key in seen_numbers:
            continue
        seen_numbers.add(key)
        next_m = re.search(
            r'<div\s+[^>]*class=["\']?citations["\']?',
            html[m.end():],
        )
        end = m.end() + next_m.start() if next_m else m.end() + 8000
        block = html[m.start():end]
        ref, raw_text = _parse_one_reference(block)
        # Fallback: use raw text as title only when no structured fields
        # were recovered (journal, volume, year all empty). Old-style
        # citations legitimately have no title; keep title empty if any
        # structured field was parsed.
        if (
            not ref["title"]
            and raw_text
            and not ref["journal"]
            and not ref["volume"]
            and not ref["year"]
        ):
            ref["title"] = raw_text
        refs.append({"": ref})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract from <h2 property=name>Abstract</h2> + sibling sections.

    SAGE abstracts use structured sub-sections (Aims, Results, Innovation,
    Conclusion) inside <section id=abs-sec-N>. science.org puts the abstract
    in <section data-extent=frontmatter>. Bounds the slice at bodymatter or
    the next h2 to avoid pulling in newsletter/related-article chrome.
    """
    abstract_stops = [
        r'<section[^>]*id=bodymatter',
        r'<section[^>]*data-extent=bodymatter',
        r'<section[^>]*class=["\']?denial-block',
        r'<div[^>]*class=["\']?alert-signup',
        r'<form[^>]*newsletter',
    ]
    chunk = _bounded_slice(
        html,
        r'<h2[^>]*property=name[^>]*>\s*Abstract\s*</h2>',
        abstract_stops,
    )
    if not chunk:
        chunk = _bounded_slice(
            html,
            r'<h2[^>]*>\s*Abstract\s*</h2>',
            abstract_stops + [r'<h2[^>]*>'],
        )
    if not chunk:
        return ""
    chunk = strip_common(chunk)
    return tags_to_text(chunk).strip()


def _bounded_slice(html, start_pat, end_pats):
    """Return the substring between start_pat and the earliest of end_pats.

    Used to slice <section> zones whose internal nested <section> tags break
    naive non-greedy matching. start_pat must capture the opening tag; the
    slice begins after it.
    """
    sm = re.search(start_pat, html)
    if not sm:
        return ""
    start = sm.end()
    end = len(html)
    for pat in end_pats:
        em = re.search(pat, html[start:])
        if em:
            end = min(end, start + em.start())
    return html[start:end]


def _parse_bodymatter(html):
    """Extract body sections inside <section id=bodymatter>.

    Slice from the bodymatter opening tag to the next backmatter (or
    citing-articles widget) start. Avoids </section> matching pitfalls that
    arise from nested sub-sections.
    """
    body = _bounded_slice(
        html,
        r'<section[^>]*id=bodymatter[^>]*>',
        [
            r'<section[^>]*id=backmatter[^>]*>',
            r'<section[^>]*id=bibliography[^>]*>',
            r'<section[^>]*class=["\']?citing-articles',
            r'<section[^>]*class=["\']?recommended-articles',
        ],
    )
    if not body:
        return ""
    # Drop denial blocks (paywalled SAGE pages)
    body = re.sub(
        r'<section[^>]*class=["\']?denial-block[^>]*>.*?</section>',
        '', body, flags=re.DOTALL,
    )
    body = extract_captions(body)
    body = strip_common(body)
    return tags_to_text(body).strip()


def _parse_backmatter(html):
    """Extract back matter (data availability, acknowledgements, supp text)
    excluding references and citing-articles widgets.

    Slices from <section id=backmatter> until either the bibliography
    section or the citing-articles list. Then drops any remaining
    bibliography or recommended-articles fragments.
    """
    backmatter = _bounded_slice(
        html,
        r'<section[^>]*id=backmatter[^>]*>',
        [
            r'<section[^>]*id=bibliography[^>]*>',
            r'<ol[^>]*class=["\']?citing-articles',
            r'<section[^>]*class=["\']?citing-articles',
            r'<section[^>]*class=["\']?recommended-articles',
            r'<div[^>]*class=["\']?trendmd',
        ],
    )
    if not backmatter:
        return ""
    backmatter = extract_captions(backmatter)
    backmatter = strip_common(backmatter)
    return tags_to_text(backmatter).strip()


def _parse_main_text(html):
    """Extract body text.

    SAGE landing pages typically expose only the abstract (paywalled body).
    Combine abstract + any non-paywalled body + back matter (excluding
    references). Falls back to abstract alone when the rest is locked.
    """
    parts = []
    abstract = _parse_abstract(html)
    if abstract:
        parts.append("## Abstract\n\n" + abstract)
    body = _parse_bodymatter(html)
    if body and body.strip() != abstract.strip():
        parts.append(body)
    back = _parse_backmatter(html)
    if back:
        parts.append(back)
    text = "\n\n".join(parts).strip()
    return drop_noise(text, _NOISE) if text else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse SAGE HTML into a papers/*.json-format dict."""
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
