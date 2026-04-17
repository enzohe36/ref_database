"""Journal of Visualized Experiments (jove.com) HTML parser."""

import re
from html import unescape

from ._helpers import (
    drop_noise,
    extract_captions,
    format_doi,
    get_meta,
    parse_meta_authors,
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
    """Remove floating banners, cookie consent dialogs, and overlays.

    JoVE pages have no visually impairing elements; return unmodified.
    """
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, volume, issue, year, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - volume, issue: str (may be empty)
      - year: 4-digit string
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
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "year": year,
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
    initials = "".join(p[0] for p in pieces if p and p[0].isupper())
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

    Returns list of {"": {journal, volume, issue, year, title, pages, doi, authors}}.
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
            "volume": volume,
            "issue": issue,
            "year": year,
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
    """Parse JoVE HTML into a refs.json-format dict plus main_text."""
    meta = _parse_metadata(html)
    return {
        "stem": "",
        "journal": meta["journal"],
        "volume": meta["volume"],
        "issue": meta["issue"],
        "year": meta["year"],
        "title": meta["title"],
        "pages": meta["pages"],
        "doi": meta["doi"],
        "authors": _parse_authors(html),
        "publication_types": [],
        "references": _parse_references(html),
        "main_text": _parse_main_text(html),
    }
