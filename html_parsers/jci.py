"""Journal of Clinical Investigation (jci.org) HTML parser."""

import re
from html import unescape

from ._helpers import (
    drop_noise,
    extract_captions,
    format_doi,
    get_meta,
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "View this article via:",
    "View this table:",
    "In this window",
    "In a new window",
    "Open in a new tab",
    "Google Scholar",
    "CrossRef",
    "PubMed",
    "View Supplemental data",
)

# Reference section title pattern
_REF_RE = re.compile(r"\breferences\b", re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r"supplement|extended data|source data|expanded view|powerpoint|appendix",
    re.IGNORECASE,
)

# Site chrome (sections removed from main_text).
# Kept sections include abstract, body sections (Introduction/Results/
# Discussion/Methods), acknowledgments, author contributions, footnotes —
# per CLAUDE.md, keep everything from abstract to before first references.
# Only non-content panels are filtered here.
_CHROME_RE = re.compile(
    r"^(?:version history|related articles?)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Remove floating banners, cookie consent dialogs, and overlays.

    - article-tools-nav: floating article toolbar (Tools, Go to, View
      PDF, Top).
    """
    return remove_elements_by_id(html, "article-tools-nav")


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
    """
    title = get_meta(html, "citation_title")
    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")

    date = get_meta(html, "citation_publication_date") or get_meta(html, "citation_online_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    volume = get_meta(html, "citation_volume")
    issue = get_meta(html, "citation_issue")

    # Fallback: parse "Reference information: <i>Journal</i>. Year;Vol(Issue):Pages."
    # from the footnotes panel (used for elocator IDs like e178278 where
    # citation_firstpage meta is absent).
    if not pages or not volume or not issue:
        fm = re.search(
            r"Reference information[^<]*</b>\s*<i>[^<]+</i>\s*\.\s*"
            r"(\d{4})\s*;\s*(\w+)\s*\(([^)]+)\)\s*:\s*([\w.\-\u2013]+)",
            html,
        )
        if fm:
            if not volume:
                volume = fm.group(2)
            if not issue:
                issue = fm.group(3)
            if not pages:
                p = re.sub(r"[\u2013\u2014]", "-", fm.group(4))
                pages = p.rstrip(".")

    return {
        "title": title,
        "journal": journal.rstrip(".") if journal else "",
        "volume": volume,
        "issue": issue,
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


def _jci_format_name(name):
    """Convert 'Given Middle LastName' to 'LastName IN'.

    JCI citation_author meta tags store 'Given LastName' with no comma;
    format_author_name in _helpers only handles the comma form and returns
    no-comma input unchanged, so JCI needs its own formatter.
    Handles compound surnames with particles (de, van, von, ...) by
    walking backward from the final token: collects trailing particles,
    and pulls in one leading capitalized token if given-name tokens remain
    before it. Matches PubMed/refs.json convention for names like
    "de Bono J" and "Fenor de La Maza MLD".
    Normalizes curly quotes/thin spaces to match _helpers behavior.
    """
    name = (name.replace("\u2018", "'").replace("\u2019", "'")
                .replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2009", " ").replace("\u00a0", " ")).strip()
    if not name:
        return ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0]

    # Walk from end: last token is surname, collect trailing particles.
    i = len(parts) - 1
    surname_parts = [parts[i]]
    i -= 1
    while i >= 0 and parts[i].lower().rstrip(".") in _SURNAME_PARTICLES:
        surname_parts.insert(0, parts[i])
        i -= 1
    # If particles were collected and tokens remain before the current
    # position, fold in one leading capitalized, non-initial token as
    # part of the compound surname (e.g., "Fenor de La Maza").
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
    Parses citation_author meta tags for names (JCI has no
    citation_author_institution tags) and maps to affiliations via the
    author-list <sup> superscript references pointing to <p class=affiliations>.
    """
    # Author names from citation_author meta tags
    names = []
    for m in re.finditer(
        r'<meta[^>]*name=["\']?citation_author["\']?'
        r'[^>]*content=["\']([^"\']*)["\']',
        html,
    ):
        names.append(unescape(m.group(1)).strip())
    if not names:
        return []

    # Build aff number -> text map from <p class=affiliations>
    aff_map = {}
    for m in re.finditer(
        r'<p\s+class=["\']?affiliations["\']?[^>]*>(.*?)(?=<p\s+class=["\']?affiliations|</(?:div|section)>)',
        html, re.DOTALL,
    ):
        raw = m.group(1)
        num_m = re.match(r"\s*<sup>(\d+)</sup>", raw)
        if not num_m:
            continue
        num = num_m.group(1)
        text = strip_tags(raw[num_m.end():]).strip().rstrip(",;.").strip()
        if text:
            aff_map[num] = text

    # Build author index -> [aff_num, ...] via data-dropdown=author-affiliation-N
    author_aff_nums = {}
    for m in re.finditer(
        r'data-dropdown=["\']?author-affiliation-(\d+)["\']?[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        idx = int(m.group(1))
        sup_nums = re.findall(r"<sup>([\d,\s]+)</sup>", m.group(2))
        nums = []
        for s in sup_nums:
            for n in re.split(r"[,\s]+", s):
                if n:
                    nums.append(n)
        author_aff_nums[idx] = nums

    authors = []
    for i, name in enumerate(names):
        affs = [aff_map[n] for n in author_aff_nums.get(i, []) if n in aff_map]
        authors.append({
            "author": _jci_format_name(name),
            "affiliation": affs,
        })
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_ref_text(text):
    """Parse a single JCI reference line into structured fields.

    Raw text form: 'Authors. Title. <i>Journal.</i> Year;Vol(Issue):fpage-lpage.'
    Some older references omit volume/issue/pages. Journal appears in <i>.
    """
    return text  # placeholder; real work done by _parse_references


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {journal, volume, issue, year, title, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].
    JCI references are <li class=reference value=N> entries inside
    <div id=references>. Raw text form:
      'Surname IN, Surname IN. Title. <i>Journal.</i> Year;Vol(Issue):fpage-lpage.'
    followed by a <div class=reference_linkouts> block with DOI/PubMed links.
    """
    refs = []
    m = re.search(r'<div\s+id=["\']?references["\']?[^>]*>', html)
    if not m:
        return refs

    section = html[m.end():]
    # Section ends at </div> that closes id=references — safe upper bound: next <dl/dd/dt
    # but the <ol>...</ol> is enough.
    ol_m = re.search(r"<ol[^>]*>(.*?)</ol>", section, re.DOTALL)
    if not ol_m:
        return refs
    list_html = ol_m.group(1)

    for lm in re.finditer(
        r'<li\s+class=["\']?reference["\']?(?:\s+value=\d+)?[^>]*>(.*?)</li>',
        list_html, re.DOTALL,
    ):
        entry = lm.group(1)

        # DOI from linkouts
        doi = ""
        dm = re.search(
            r'href=["\']?https?://(?:dx\.)?doi\.org/([^"\'>\s]+)', entry,
        )
        if dm:
            doi = format_doi(unescape(dm.group(1)))

        # Strip linkouts to isolate citation text
        citation = re.sub(
            r'<div\s+class=["\']?reference_linkouts["\']?.*?</div>',
            "", entry, flags=re.DOTALL,
        )

        # Journal inside <i>...</i> (take first italic chunk before year digits)
        journal = ""
        jm = re.search(r"<i>([^<]+)</i>", citation)
        if jm:
            journal = unescape(jm.group(1)).strip().rstrip(".").rstrip(",")

        plain = strip_tags(citation).strip()
        plain = re.sub(r"\s+", " ", plain)

        # Volume / issue / pages: "Year;Vol(Issue):fpage-lpage" or variants
        year = ""
        volume = ""
        issue = ""
        pages = ""
        ym = re.search(
            r"(\d{4})"
            r"(?:\s*;\s*(\w+)"
            r"(?:\s*\(([^)]+)\))?"
            r"(?:\s*:\s*([\w.\-\u2013]+))?"
            r")?",
            plain,
        )
        if ym:
            year = ym.group(1)
            volume = (ym.group(2) or "").strip()
            issue = (ym.group(3) or "").strip()
            pages_raw = (ym.group(4) or "").strip().rstrip(".")
            pages = re.sub(r"[\u2013\u2014]", "-", pages_raw)

        # Split authors/title: the segment before <i>Journal</i> is "Authors. Title."
        # Authors are first; split at the first ". " (period-space) since JCI
        # uses initials without trailing dots ("Atianand MK" not "Atianand M.K.").
        # Handles all common forms: single author, multiple authors, "et al".
        authors = []
        title = ""
        if jm:
            before_journal = citation[:jm.start()]
            before_plain = strip_tags(before_journal).strip()
            before_plain = re.sub(r"\s+", " ", before_plain).rstrip()
            split_idx = before_plain.find(". ")
            if split_idx > 0:
                author_str = before_plain[:split_idx]
                title = before_plain[split_idx + 2:].strip().rstrip(".")
            else:
                author_str = before_plain.rstrip(".")
            # Parse authors: comma-separated "Surname IN" entries.
            # Drop "et al" trailing token; keep corporate-author strings.
            for a in author_str.split(","):
                a = a.strip().rstrip(".").strip()
                if not a or a.lower().startswith("et al"):
                    continue
                authors.append(a)
        else:
            title = plain

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

def _extract_jci_figures(html):
    """Replace <div class=figure> blocks with plain-text captions.

    JCI figures: <div class=figure> with a <a class=figure_number>Figure N</a>
    and a <span class=figure_title><b>short title</b></span>, followed by
    the descriptive caption text. Images and svg are dropped. Entities like
    &lt; are preserved so downstream tag-stripping does not misinterpret
    literal < in captions (e.g. "P < 0.001") as tag starts.
    """
    def _strip_keep_entities(s):
        return re.sub(r"<[^>]+>", "", s)

    def repl(m):
        inner = m.group(1)
        # Figure/Table label
        label = ""
        lm = re.search(
            r'<a\s+class=["\']?figure_number["\']?[^>]*>(.*?)</a>',
            inner, re.DOTALL,
        )
        if lm:
            label = _strip_keep_entities(lm.group(1)).strip()
        # Drop thumbnail link, figure_number link, images, svg
        clean = re.sub(
            r'<a[^>]*>\s*<img[^>]*/?>\s*</a>', "", inner,
        )
        clean = re.sub(
            r'<a\s+class=["\']?figure_number["\']?[^>]*>.*?</a>',
            "", clean, flags=re.DOTALL,
        )
        clean = re.sub(r"<img[^>]*/?>", "", clean)
        clean = re.sub(r"<svg[^>]*>.*?</svg>", "", clean, flags=re.DOTALL)
        text = _strip_keep_entities(clean).strip()
        text = re.sub(r"\s+", " ", text)
        if label:
            text = f"{label}. {text}" if text else label
        return "\n\n" + text + "\n\n"

    return re.sub(
        r"<div\s+class=figure>(.*?)</div>",
        repl, html, flags=re.DOTALL,
    )


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    JCI-specific: iterates <dl class=article-section> panels, identifying
    each by its <span class=section-title>, and drops acknowledgments,
    author contributions, footnotes, version history, references, graphical
    abstract thumbnails.
    """
    parts = []
    hit_references = False

    for m in re.finditer(
        r'<dl\s+class=["\']?article-section["\']?[^>]*>(.*?)</dl>',
        html, re.DOTALL,
    ):
        section_html = m.group(1)
        title_m = re.search(
            r'class=["\']?section-title["\']?[^>]*>(.*?)</span>',
            section_html, re.DOTALL,
        )
        title = strip_tags(title_m.group(1)).strip() if title_m else ""
        tlow = title.lower()

        if _REF_RE.fullmatch(tlow) or tlow == "references":
            hit_references = True
            continue

        if hit_references and not _SUPP_RE.search(tlow):
            continue

        if _CHROME_RE.match(tlow):
            continue

        # Drop graphical abstract figure thumbnails — keep its caption text
        # by converting via _extract_jci_figures; skip purely graphical ones
        if tlow == "graphical abstract":
            continue

        body_m = re.search(
            r'<div[^>]*id=["\']?[^"\'>]+["\']?\s+class=["\']?content\s+active["\']?[^>]*>(.*)',
            section_html, re.DOTALL,
        )
        content_html = body_m.group(1) if body_m else section_html

        # Heading
        heading = f"## {title}" if title else ""
        content_html = _extract_jci_figures(content_html)
        content_html = extract_captions(content_html)
        content_html = strip_common(content_html)
        text = tags_to_text(content_html)
        text = drop_noise(text, _NOISE)
        if not text.strip():
            continue
        parts.append(f"{heading}\n\n{text}" if heading else text)

    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse JCI HTML into a refs.json-format dict plus main_text."""
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
