"""PMC (pmc.ncbi.nlm.nih.gov) HTML parser."""

import re
import urllib.parse
from html import unescape

from ._helpers import (
    _is_name_suffix,
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
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
    "Find articles by",
    "Search in PMC",
    "Search in PubMed",
    "View in NLM Catalog",
    "Add to search",
    "See this image in",
    "Go to:",
)

# Section id prefixes that contain body content
_BODY_ID_RE = re.compile(r'^[sS]\d|^sec\d', re.IGNORECASE)

# h2 headings that mark end of body content in flat layouts
_DROP_H2 = {
    "references",
}


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """No-op stub. Pending re-development of html formatting."""
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_cite_line(html):
    """Extract journal abbreviation and page range from the inline citation.

    PMC pages show a citation line like:
      J Biol Chem. 2012 Oct 20;287(50):41583–41594. doi: ...
    or for author manuscripts:
      Published in final edited form as: Mol Cell. 2019 ...;75(1):117–130.
    Returns (journal_abbrev, pages) or ("", "").
    """
    # Journal abbreviation from the journal context menu button
    jm = re.search(
        r"aria-controls=journal_context_menu[^>]*>(.*?)</button>", html
    )
    journal = jm.group(1).strip() if jm else ""

    # Page range from the citation text after the journal button
    pages = ""
    cite_m = re.search(
        r"</button></div>\.\s*(\d{4}.*?)(?:doi:|pmid:|Epub)",
        html, re.DOTALL,
    )
    if not cite_m:
        cite_m = re.search(
            r"Published in final edited form as:.*?</em>\s*.*?\.\s*"
            r"(\d{4}.*?)(?:doi:|pmid:|Epub)",
            html, re.DOTALL,
        )
    if cite_m:
        cite_text = re.sub(r"<[^>]+>", "", cite_m.group(1)).strip()
        pm = re.search(r":(\S+?)\.(?:\s|$)", cite_text)
        if pm:
            # Normalize en-dash / em-dash to hyphen
            pages = pm.group(1).replace("\u2013", "-").replace("\u2014", "-")

    return journal, pages


def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    PMC-specific: journal and pages fall back to the inline citation line
    when citation_* meta tags are missing or incomplete.
    """
    date = get_meta(html, "citation_publication_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    # Journal abbreviation and full page range from inline citation
    cite_journal, cite_pages = _parse_cite_line(html)
    journal = cite_journal or get_meta(html, "citation_journal_title")
    if cite_pages:
        pages = cite_pages

    return {
        "title": get_meta(html, "citation_title"),
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

def _join_affiliation_fragments(affiliations):
    """Join PMC affiliation fragments split at cross-reference markers.

    PMC sometimes splits one affiliation sentence into multiple
    citation_author_institution tags at ‡/§/¶ markers, producing
    fragments that end with " and" (e.g., "From the Departments of
    Biochemistry and Biophysics and"). Join such fragments with the
    next entry into a single string.
    """
    if not affiliations:
        return affiliations
    joined = []
    buf = ""
    for aff in affiliations:
        buf = (buf + " " + aff).strip() if buf else aff
        if re.search(r"\band(?:\s+the)?$", buf):
            continue
        joined.append(buf)
        buf = ""
    if buf:
        # Dangling fragment — strip trailing connector words.
        joined.append(re.sub(r"\s+and(?:\s+the)?$", "", buf))
    return joined


def _display_to_initials(name):
    """Convert 'Given Last' to 'Last IN' via shared helpers.

    PMC citation_author meta tags emit 'Given Middle Last' without a
    comma; format_author_name handles the flip and compound-surname
    particles via parse_combined_name + format_name in _helpers.
    """
    return format_author_name(name)


def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    Author name format is enforced by _helpers.format_author_name; PMC
    meta tags emit "Given Middle Last" without a comma, so names are
    flipped first by _display_to_initials so the surname lands first.
    """
    meta_authors = parse_meta_authors(html)
    return [
        {
            "author": _display_to_initials(a["name"]),
            "affiliation": _join_affiliation_fragments(
                a.get("affiliations", [])
            ),
        }
        for a in meta_authors
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

# Suffix detection delegated to _helpers._is_name_suffix (covers Jr, Sr,
# II-V, PhD, MD, and any \d+(st|nd|rd|th) ordinal — case-insensitive).


def _find_author_title_boundary(text):
    """Return index of the `. ` or `(YYYY)` that separates authors from title.

    Walk forward through each `. ` occurrence. A `.` followed by `,` or
    by a dotted single-letter initial ("W.") is intra-author — continue.
    A `.` followed by a multi-character initial-like token ("DNA")
    where the token AFTER that isn't initial-like ("Charge") is the
    title boundary. A `.` followed by a non-initial token is always
    the boundary. If the text contains `(YYYY)` (Ryan / IUCr style),
    the boundary is right before that paren. Returns -1 if not found.
    """
    pm = re.search(r"\s+\(\d{4}[a-z]?\)", text)
    pyear_pos = pm.start() if pm else len(text) + 1
    for m in re.finditer(r"\.(?=\s)", text):
        pos = m.start()
        if pos >= pyear_pos:
            break
        rs = pos + 1
        while rs < len(text) and text[rs] == " ":
            rs += 1
        if rs < len(text) and text[rs] == ",":
            continue
        nm = re.match(r"(\S+)", text[rs:])
        if not nm:
            continue
        first_raw = nm.group(1)
        if first_raw.rstrip(".").endswith(","):
            # "A.," — intra-author separator, next is sibling author
            continue
        first = first_raw.rstrip(",.")
        stripped = re.sub(r"[.\-]", "", first)
        is_init = (
            stripped and stripped.isupper() and stripped.isalpha()
            and 1 <= len(stripped) <= 3
        )
        if is_init:
            # Single-letter dotted initial like "W." — intra-author.
            if first_raw.endswith(".") and len(first_raw.rstrip(".")) == 1:
                continue
            # Multi-letter initial-like token ("DNA"): boundary unless the
            # token AFTER is also initial-like (still listing authors).
            after = rs + nm.end()
            nm2 = re.match(r"(\S+)", text[after:].lstrip())
            if nm2:
                second = nm2.group(1).rstrip(",.")
                second_stripped = re.sub(r"[.\-]", "", second)
                is_sec_init = (
                    second_stripped and second_stripped.isupper()
                    and second_stripped.isalpha()
                    and 1 <= len(second_stripped) <= 3
                )
                if not is_sec_init:
                    return pos
            continue
        return pos
    return pm.start() if pm else -1


def _is_initials_only(chunk):
    """True if every whitespace-separated token in chunk is an initial group."""
    toks = chunk.split()
    if not toks:
        return False
    for t in toks:
        stripped = re.sub(r"[.\-]", "", t.rstrip(",."))
        if not (stripped and stripped.isupper() and stripped.isalpha()
                and 1 <= len(stripped) <= 3):
            return False
    return True


def _parse_cite_authors(section):
    """Convert an author-section string into a list of 'Last IN' entries.

    Handles four PMC cite styles:
    - Modern tight: "Williams JS, Kunkel TA"
    - Old dotted: "Agard D. A., Sedat J. W."
    - IUCr: "Brünger, A. T.", "Adams, P. D., Grosse-Kunstleve, R. W. & Bell, J."
      (commas separate surname from initials AND author from author,
      "&amp;" / "&" as final separator)
    - Suffixes: "Tinoco I., Jr,", "Wilson DM III"
    - Particles: "de Lange", "van der Berg", "del Villar-Guerra"
    Initials are concatenated with no dots. Returns plain-string list.
    """
    section = section.strip()
    if not section:
        return []
    # Distinguish suffix from initials: all-caps 1-2 letter tokens
    # ('JR', 'MD') are canonical initials in citation form, not suffixes
    # — even though 'jr' / 'md' are registered suffixes. Mixed-case
    # 'Jr' / 'Md' or longer 'III' / '3rd' are real suffixes.
    def is_real_suffix(tok):
        if tok.isalpha() and tok.isupper() and 1 <= len(tok) <= 2:
            return False
        return _is_name_suffix(tok)

    chunks = [c.strip() for c in re.split(r",\s*|\s*&(?:amp;)?\s+", section) if c.strip()]
    merged = []
    for c in chunks:
        c_clean = c.rstrip(".").strip()
        if merged and (_is_initials_only(c) or is_real_suffix(c_clean)):
            merged[-1] = merged[-1] + " " + c_clean
        else:
            merged.append(c)
    authors = []
    for entry in merged:
        tokens = entry.split()
        surnames, initials, suffix = [], [], ""
        for tok in tokens:
            tok_clean = tok.rstrip(",.")
            if is_real_suffix(tok_clean):
                suffix = tok_clean
                continue
            stripped = re.sub(r"[.\-]", "", tok_clean)
            if stripped and stripped.isupper() and stripped.isalpha() and 1 <= len(stripped) <= 3:
                initials.append(stripped)
            else:
                surnames.append(tok_clean)
        if surnames and initials:
            out = " ".join(surnames) + " " + "".join(initials)
            if suffix:
                out += " " + suffix
            authors.append(out)
        elif surnames:
            authors.append(" ".join(surnames))
    return authors


def _parse_cite(cite_text):
    """Parse a <cite> text into the full reference dict.

    Handles three cite formats seen across PMC archives:
    A. Modern NLM: "Authors. Title. Journal. YYYY[ Mon DD]; Vol[(Issue)][:Pages]"
    B. Book chapter: "Authors (YYYY) Title. Journal Vol (Issue):Pages"
    C. IUCr:        "Authors (YYYY). Journal. Vol, Pages"

    Uses the full <cite> text so author lists aren't truncated (Scholar
    URLs carry only the first 5 author params and can cross reference
    boundaries). Returns dict with title/journal/year/volume/issue/
    pages/doi/authors; missing fields are empty.
    """
    text = re.sub(r"\s+", " ", cite_text.strip())
    doi = ""
    dm = re.search(r"\bdoi:\s*(\S+?)\.?\s*$", text)
    if dm:
        doi = dm.group(1).rstrip(".,; ")
        text = text[:dm.start()].rstrip(" .")

    volume = issue = pages = ""
    # Format A: ". YYYY[ Mon[ DD]][; Vol(Issue):Pages]"
    # Allow either ". YYYY" or ") YYYY" (e.g. "DNA Repair (Amst) 2005;...")
    # and make the vol/issue/pages tail optional (some refs stop at year +
    # doi without a ;Vol block).
    # Allow an optional period after the year ('Nat Cell Biol. 2004. Jul;6(7):673-80')
    # before the month/volume continuation.
    ym_a = re.search(
        r"(?:\.|\))\s+(\d{4})\.?(?:\s+\w+(?:\s+\d+)?)?(?:\s*[;:](.*))?$",
        text, re.DOTALL,
    )
    # Format B/C: "(YYYY)..." — author-section ends at the paren
    ym_b = re.search(r"\s+\((\d{4})[a-z]?\)\s*\.?\s*(.*)$", text, re.DOTALL)

    if ym_a:
        year = ym_a.group(1)
        after = (ym_a.group(2) or "").strip()
        if after:
            vm = re.match(
                r"(\d+[A-Za-z]?)(?:\(([^)]+)\))?(?::\s*([^.]+))?",
                after,
            )
            if vm:
                volume = vm.group(1) or ""
                issue = vm.group(2) or ""
                pages = (vm.group(3) or "").replace("\u2013", "-").rstrip(" .")
        head = text[:ym_a.start()].rstrip(" .)")
        parts = head.rsplit(". ", 1)
        if len(parts) == 2:
            at, journal = parts[0], parts[1].rstrip(".")
        else:
            at, journal = head, ""
        bd = _find_author_title_boundary(at)
        if bd < 0:
            author_section, title = "", at
        else:
            author_section = at[:bd]
            title = at[bd + 1:].lstrip(". ").rstrip(".")
    elif ym_b:
        year = ym_b.group(1)
        after = ym_b.group(2).strip(" .,")
        # Book chapter: "ChapterTitle. In: BookTitle. Publisher, pp X-Y"
        in_m = re.search(r"\.\s*In:\s*", after)
        if in_m:
            title = after[:in_m.start()].strip().lstrip(". ").rstrip(". ")
            rest = after[in_m.end():].strip(" .,")
            pp_m = re.search(r",?\s*pp\.?\s*([\w\d]+\s*[-\u2013]\s*[\w\d]+)\s*\.?\s*$", rest)
            if pp_m:
                pages = pp_m.group(1).replace("\u2013", "-").replace(" ", "")
                rest = rest[:pp_m.start()].rstrip(",. ")
            # "BookTitle. Publisher" → keep BookTitle as journal (the
            # reference's parent publication); drop the publisher.
            journal = rest.split(". ", 1)[0].rstrip(",. ")
            volume = issue = ""
        else:
            vip = re.search(
                r"(\d+[A-Za-z]*)\s*\(\s*([\w\d\-\u2013]+)\s*\)\s*:\s*([^.]+?)\s*(?:\.|$)",
                after,
            )
            vp = re.search(
                r"(\d+[A-Za-z]*)\s*[,:]\s*([\w\d]+[-\u2013][\w\d]+|\d+)\b",
                after,
            )
            if vip:
                volume = vip.group(1)
                issue = vip.group(2).replace("\u2013", "-")
                pages = vip.group(3).replace("\u2013", "-").strip()
                before_vip = after[:vip.start()].rstrip(" ,.")
            elif vp:
                volume = vp.group(1)
                pages = vp.group(2).replace("\u2013", "-")
                before_vip = after[:vp.start()].rstrip(" ,.")
            else:
                before_vip = after
            parts = before_vip.rsplit(". ", 1)
            if len(parts) == 2:
                title = parts[0].lstrip(". ")
                journal = parts[1].rstrip(",. ")
            else:
                title, journal = "", before_vip.rstrip(",. ")
        author_section = text[:ym_b.start()].rstrip(" ,")
    else:
        return {
            "title": text.rstrip("."),
            "journal": "", "year": "",
            "volume": "", "issue": "", "pages": "", "doi": format_doi(doi) if doi else "",
            "authors": [],
        }

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": format_doi(doi) if doi else "",
        "authors": _parse_cite_authors(author_section),
    }


def _flip_scholar_author(name):
    """Flip Google Scholar 'Initials Last' → 'Last IN' via shared helpers."""
    return format_author_name(name)


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.
    Each reference dict uses the same field formats as the main paper, with
    one exception: authors is a list of "LastName IN" strings (plain strings,
    not dicts with affiliation). Empty fields are "". Empty authors is [].

    Hybrid source strategy:
    - Structured fields (title, journal, year, volume, issue, pages, doi)
      prefer Google Scholar lookup URL when present (well-formed and
      scoped per <li>). Fall back to <cite> parsing.
    - Authors always pulled from <cite> first because Scholar URLs cap
      at 5 author params and truncate. If <cite> parsing yields no
      authors, fall back to the Scholar URL list with _flip_scholar_author.
    """
    m = re.search(r"<article[^>]*>(.*)</article>", html, re.DOTALL)
    if not m:
        return []
    article = m.group(1)

    # Find all reference <li> entries
    bib_starts = [
        (bm.start(), bm.group(1))
        for bm in re.finditer(r'<li\s+id="?([^">\s]+)"?>', article)
        if not _BODY_ID_RE.match(bm.group(1))
    ]

    # Filter to only entries inside ref-list sections
    ref_sections = list(re.finditer(
        r'<section[^>]*class="?ref-list"?[^>]*>', article
    ))
    if not ref_sections:
        return []

    ref_section_start = ref_sections[0].start()
    bib_starts = [(pos, bid) for pos, bid in bib_starts if pos > ref_section_start]

    refs = []
    for i, (pos, bib_id) in enumerate(bib_starts):
        end = bib_starts[i + 1][0] if i + 1 < len(bib_starts) else pos + 5000
        entry_html = article[pos:end]

        # Parse <cite> (source of author list; also backup structured fields).
        cite_m = re.search(r"<cite>(.*?)</cite>", entry_html, re.DOTALL)
        cite_text = unescape(strip_tags(cite_m.group(1)).strip()) if cite_m else ""
        cite_parsed = _parse_cite(cite_text) if cite_text else None

        # Prefer Scholar URL for structured fields when available.
        scholar_m = re.search(
            r"scholar\.google\.com/scholar_lookup\?([^\"']+)", entry_html,
        )
        if scholar_m:
            # PMC's Scholar lookup URLs encode spaces as %20, so a literal
            # "+" in the source ("NAD(+)-mediated") is meant as a plus
            # sign — but parse_qs follows form-encoding rules and would
            # decode it as a space. Pre-escape "+" so parse_qs returns it
            # verbatim.
            raw = unescape(scholar_m.group(1)).replace("&amp;", "&").replace("+", "%2B")
            params = urllib.parse.parse_qs(raw)
            journal = params.get("journal", [""])[0]
            # Scholar URLs with bare & in journal (e.g. "Genes & Development")
            # get truncated by parse_qs. Fall back to <cite>-parsed journal.
            if journal and journal != journal.rstrip():
                if cite_parsed and cite_parsed.get("journal"):
                    journal = cite_parsed["journal"]
            cite_authors = cite_parsed["authors"] if cite_parsed else []
            if cite_authors:
                authors = cite_authors
            else:
                authors = [
                    _flip_scholar_author(a)
                    for a in params.get("author", []) if a.strip()
                ]
            ref = {
                "title": params.get("title", [""])[0],
                "journal": journal,
                "year": params.get("publication_year", [""])[0],
                "volume": params.get("volume", [""])[0],
                "issue": params.get("issue", [""])[0],
                "pages": params.get("pages", [""])[0],
                "doi": format_doi(params.get("doi", [""])[0]),
                "authors": authors,
            }
            # Scholar URL in PMC sometimes emits just `pages=<volume>` when
            # the paper has an elocation ID instead of a page range (e.g.
            # EMBO J. 2019;38(5). → pages=38, no volume/issue). When the
            # Scholar URL lacks volume but <cite> parsing produced volume,
            # prefer the cite-parsed volume/issue/pages.
            if cite_parsed and not ref["volume"] and cite_parsed.get("volume"):
                ref["volume"] = cite_parsed["volume"]
                ref["issue"] = cite_parsed.get("issue", "") or ref["issue"]
                # If Scholar's `pages` duplicates the volume, it's the
                # elocation-ID placeholder — drop it unless cite has real
                # pages.
                if ref["pages"] == cite_parsed["volume"]:
                    ref["pages"] = cite_parsed.get("pages", "")
                elif cite_parsed.get("pages") and not ref["pages"]:
                    ref["pages"] = cite_parsed["pages"]
            # Book chapter: Scholar URL puts the BOOK title in `title=` and
            # omits the chapter title entirely. <cite> parsing detects the
            # "In: BookTitle" pattern and splits chapter title from book
            # title correctly — prefer cite's fields when cite identifies
            # a book chapter (journal set, volume empty, pages present).
            if (cite_parsed and cite_text and re.search(r"\.\s*In:\s*", cite_text)
                    and cite_parsed.get("title") and cite_parsed.get("journal")):
                ref["title"] = cite_parsed["title"]
                ref["journal"] = cite_parsed["journal"]
                if cite_parsed.get("pages") and not ref["pages"]:
                    ref["pages"] = cite_parsed["pages"]
            # Scholar URL doesn't always include doi; fall back to the
            # DOI anchor link in the entry HTML.
            if not ref["doi"]:
                dm = re.search(
                    r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", entry_html
                )
                if dm:
                    ref["doi"] = format_doi(dm.group(1))
        elif cite_parsed:
            ref = cite_parsed
        else:
            ref = {
                "title": "", "journal": "", "year": "", "volume": "", "issue": "",
                "pages": "", "doi": "", "authors": [],
            }

        refs.append({"": ref})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _find_main_body(article):
    """Find the main-article-body element content."""
    bm = re.search(
        r'class="body main-article-body"[^>]*>', article, re.DOTALL
    )
    return bm.end() if bm else -1


def _find_start(article, body_start):
    """Find main_text start: at the body beginning (includes abstract)."""
    return body_start


def _find_end(article, body_start):
    """Find main_text end: end of article content.

    References are removed as inner sections. The end is the closing
    </article> tag or end of string.
    """
    return len(article)


def _remove_inner_refs(body_html):
    """Remove ref-list sections that appear within the main_text range."""
    while True:
        m = re.search(r'<section[^>]*class="?ref-list"?[^>]*>', body_html)
        if not m:
            break
        # Walk to matching </section>
        pos = m.end()
        depth = 1
        while depth > 0 and pos < len(body_html):
            next_open = re.search(r'<section[\s>]', body_html[pos:])
            next_close = re.search(r'</section>', body_html[pos:])
            if next_close is None:
                break
            if next_open and next_open.start() < next_close.start():
                depth += 1
                pos += next_open.end()
            else:
                depth -= 1
                pos += next_close.end()
        body_html = body_html[:m.start()] + body_html[pos:]
    return body_html


def _parse_main_text(html):
    """Extract body text.

    Boundary rules (from CLAUDE.md):
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    PMC-specific: start below abstract and keywords; end above the first
    ref-list section; remove any inner ref-list sections from the range;
    everything else (acknowledgements, tables, captions, etc.) is retained.
    """
    m = re.search(r"<article[^>]*>(.*)</article>", html, re.DOTALL)
    if not m:
        return ""
    article = m.group(1)

    body_start = _find_main_body(article)
    if body_start < 0:
        return ""

    start = _find_start(article, body_start)
    end = _find_end(article, body_start)

    if start >= end:
        # Fallback: flat layout with h2 headings
        body_html = article[body_start:]
        h2s = [
            (hm.start(), re.sub(r"<[^>]+>", "", hm.group(1)).strip())
            for hm in re.finditer(r"<h2[^>]*>(.*?)</h2>", body_html, re.DOTALL)
        ]
        start = 0
        for pos, label in h2s:
            if label.lower() not in _DROP_H2:
                start = pos
                break
        end = len(body_html)
        for pos, label in h2s:
            if pos > start and label.lower() in _DROP_H2:
                end = pos
                break
        body_html = body_html[start:end]
    else:
        body_html = article[start:end]

    body_html = _remove_inner_refs(body_html)
    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse PMC HTML into a papers/*.json-format dict."""
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
