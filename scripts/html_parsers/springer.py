"""Springer (springer.com) HTML parser."""

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
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text
_NOISE = (
    "Open in a new tab",
    "Source data",
    "Full size image",
    "Full size table",
)

# Reference section titles (removed from main_text)
_REF_SECTIONS = {"references"}

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r'supplement|extended data|source data|expanded view|powerpoint|appendix',
    re.IGNORECASE,
)

# Sections to skip (not part of main_text)
_PRE_BODY = {"inline recommendations"}


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Apply Phase 2 layout rules for Springer (link.springer.com).

    Step 1: cap body width at 752px, center, neutralize @media queries so
            the publisher's narrow CSS branch always applies.
    Step 2: remove the cookie consent dialog (`<dialog class=cc-banner>`).
            The dialog ships its backdrop as the `::backdrop` pseudo-element
            on the same node, so a single removal handles both.
    Step 3: remove the position:sticky `c-status-message--sticky` info
            banner (work-in-progress / archive notice) that engages on
            scroll.
    Step 5: remove ad wrappers — `<aside class="c-ad ...">` leaderboard /
            inline ads. (Springer pages don't ship the lazy-MPU
            `u-lazy-ad-wrapper` or the empty `u-show-following-ad` marker.)
    Step 8: figure CSS — size the figure image to full column width,
            force display:block on the picture/figure-content wrappers
            (defaults are inline) so figcaption -> image -> description
            stack vertically in DOM order, and add an 8 px margin below
            the image. The native CSS already paints the image
            (`.c-article-section__figure-item img{display:block}`); this
            CSS is purely sizing/alignment, not a hidden-state reveal.
    Step 9: no-op. Author list, tables, references, and figure images
            all render inline in Springer's link.springer.com layout —
            no collapsed-state UI to expand.
    """
    html = neutralize_media_queries(html)

    # Step 2 — cookie consent dialog. position:fixed dialog whose ::backdrop
    # pseudo-element ships with it, so one removal kills banner + overlay.
    html = _remove_nested_element(
        html, r'<dialog\b[^>]*\bclass=["\']?cc-banner\b[^>]*>'
    )

    # Step 3 — sticky `c-status-message--sticky` info banner (e.g. WIP /
    # archive notices). position:sticky engages once the user scrolls
    # past the article header.
    html = _remove_nested_element(
        html,
        r'<div\b[^>]*\bclass="[^"]*\bc-status-message--sticky\b[^"]*"[^>]*>'
    )

    # Step 5 — ad blocks. `<aside class="c-ad ...">` covers leaderboard /
    # inline ads. Loop so multiple instances per page are all removed.
    for pat in (
        r'<aside\b[^>]*\bclass="[^"]*\bc-ad\b[^"]*"[^>]*>',
    ):
        while True:
            prev = html
            html = _remove_nested_element(html, pat)
            if html == prev:
                break

    override = (
        "<style>"
        "html{margin:0!important;padding:0!important;}"
        "body{max-width:752px!important;width:auto!important;"
        "margin:0 auto!important;padding:0 16px!important;"
        "box-sizing:border-box!important;"
        "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
        # Step 8: size the figure image to column width and force
        # display:block on picture/figure-content wrappers so DOM order
        # (figcaption -> figure-content) renders vertically.
        "figure{display:block!important;width:100%!important;"
        "max-width:100%!important;margin:1em 0!important;}"
        ".c-article-section__figure-content,"
        ".c-article-section__figure-item,"
        ".c-article-section__figure-picture"
        "{display:block!important;width:100%!important;}"
        "figure picture img,figure img"
        "{display:block!important;width:100%!important;"
        "max-width:100%!important;height:auto!important;"
        "margin:0 0 8px 0!important;}"
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
    """
    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_online_date")
            or get_meta(html, "dc.date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev")
    journal = re.sub(r"  +", " ", journal.replace(".", "")).strip()
    if not journal:
        # Springer book chapters (link.springer.com/protocol or /chapter)
        # lack citation_journal_abbrev but embed the series name in a
        # JSON blob: "seriesTitle":"Methods in Molecular Biology".
        series_m = re.search(r'"seriesTitle"\s*:\s*"([^"]+)"', html)
        if series_m:
            journal = series_m.group(1).strip()

    volume = get_meta(html, "citation_volume")
    if not volume:
        # Springer book chapters expose the series volume inline after
        # the series link: '((MIMB,volume 2102))' or '((SCBI,volume 104))'.
        vm = re.search(r'\(\(\w+,\s*volume\s*(\d+)\)\)', html)
        if vm:
            volume = vm.group(1)

    return {
        "title": get_meta(html, "citation_title"),
        "journal": journal,
        "year": year,
        "volume": volume,
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
    Author name format is enforced by _helpers.format_author_name.
    Uses citation_author / citation_author_institution meta tags.
    """
    meta_authors = parse_meta_authors(html)
    return [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in meta_authors
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _flip_author_name(name):
    """Convert 'IN LastName' (e.g. 'JD Griffith') to 'LastName IN' via shared helpers."""
    return format_author_name(name)


def _parse_freeform_citation(text):
    """Parse a freeform citation string (no key=value pairs).

    Extracts year, DOI, and stores the full text as title for PubMed lookup.
    """
    text = re.sub(r'\s+', ' ', text).strip()

    # Extract DOI
    doi = ""
    doi_m = re.search(r'(https?://doi\.org/\S+)', text)
    if doi_m:
        doi = doi_m.group(1).rstrip('.')
    elif re.search(r'(10\.\d{4,}/\S+)', text):
        doi_m = re.search(r'(10\.\d{4,}/\S+)', text)
        doi = f"https://doi.org/{doi_m.group(1).rstrip('.')}"

    # Extract year from (YYYY) pattern
    year = ""
    year_m = re.search(r'\((\d{4})\)', text)
    if year_m:
        year = year_m.group(1)

    return {
        "title": text,
        "journal": "",
        "year": year,
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": doi,
        "authors": [],
    }


def _parse_citation_reference(content):
    """Parse a single citation_reference meta tag content string.

    Format: 'citation_journal_title=X; citation_title=Y; ...'
    Falls back to freeform parsing for plain-text citations.
    Returns a dict with {title, journal, year, volume, issue, pages, doi, authors}.
    """
    fields = {}
    author_parts = []
    for part in content.split("; "):
        if "=" in part:
            key, val = part.split("=", 1)
            key = key.strip()
            val = val.strip()
            # Accumulate citation_author values (may appear multiple times)
            if key == "citation_author":
                author_parts.append(val)
            else:
                fields[key] = val

    # If no key=value pairs found, parse as freeform citation
    if not fields and not author_parts:
        return _parse_freeform_citation(content)

    authors = []
    # Authors may be in a single comma-separated field or multiple fields
    raw = ", ".join(author_parts)
    if raw:
        authors = [_flip_author_name(a.strip()) for a in raw.split(", ") if a.strip()]

    journal = fields.get("citation_journal_title", "")
    journal = journal.replace(".", "")
    # Collapse multiple spaces after dot removal
    journal = re.sub(r"  +", " ", journal).strip()

    title = fields.get("citation_title", "")
    # Book citations carry citation_publisher instead of citation_journal_title;
    # the book title plays the journal role per the project convention.
    if not journal and fields.get("citation_publisher") and title:
        journal = title
        title = ""

    return {
        "title": title,
        "journal": journal,
        "year": fields.get("citation_publication_date", ""),
        "volume": fields.get("citation_volume", ""),
        "issue": "",
        "pages": fields.get("citation_pages", ""),
        "doi": format_doi(fields.get("citation_doi", "")),
        "authors": authors,
    }


_DOTTED_AUTHOR_RE = re.compile(
    r"[A-Z][\w\-']+(?:\s[\w\-']+)*,\s+(?:[A-Z]\.\s*){1,5}"
)
_COMPACT_AUTHOR_RE = re.compile(
    r"([A-Z][\w\-']+(?:\s[\w\-']+)*)\s+([A-Z]{1,5})(?=\s*(?:,|&|et al|\.|$))"
)


def _parse_body_reference(item_html):
    """Parse a single <p class=c-article-references__text> body reference.

    Uses <i>Journal</i> and <b>Volume</b> tags as structural anchors so
    field boundaries don't depend on period-splitting in prose (journal
    abbreviations like "J. Exp. Med." and titles containing colons or
    species names no longer confuse the parser).

    Covers three observed Springer/Nature layouts:
      A. Authors. Title. <i>Journal</i> <b>Vol</b>[, Pages] (YEAR).
      B. Authors. Title. <i>Journal</i> YEAR; <b>Vol</b>: Pages.
      C. Authors . YEAR <i>Journal</i> <b>Vol</b>: Pages  (no title)

    Falls back to a title-only record only when no <b> or no <i>
    precedes <b> (≤0.02% of observed body refs).
    """
    doi = ""
    m = re.search(r'href=["\']?(https?://doi\.org/[^\s"\'<>]+)', item_html)
    if m:
        doi = format_doi(m.group(1))

    b_m = re.search(r"<b[^>]*>\s*(.+?)\s*</b>", item_html, re.DOTALL)
    if not b_m:
        return _body_fallback(item_html, doi)
    volume = re.sub(r"<[^>]+>", "", b_m.group(1)).strip()

    pre_b = item_html[:b_m.start()]
    # Find all <i>...</i> blocks individually. Journal is the last <i>
    # block before <b>; an optional "(<i>X</i>)" continuation right
    # after (e.g. "DNA Repair (Amst)") folds into the journal name.
    i_matches = list(re.finditer(r"<i[^>]*>(.+?)</i>", pre_b, re.DOTALL))
    if not i_matches:
        return _body_fallback(item_html, doi)
    last_i = i_matches[-1]
    journal = unescape(re.sub(r"<[^>]+>", "", last_i.group(1))).strip().rstrip(".").strip()
    head_end = last_i.start()
    if len(i_matches) >= 2:
        prev_i = i_matches[-2]
        between = pre_b[prev_i.end():last_i.start()]
        after_last = pre_b[last_i.end():].rstrip(" .,")
        if re.match(r"\s*\(\s*$", between) and after_last.startswith(")"):
            cont = journal
            journal = f"{unescape(re.sub(r'<[^>]+>', '', prev_i.group(1))).strip().rstrip('.').strip()} ({cont})"
            head_end = prev_i.start()
    head_html = pre_b[:head_end]

    year = ""
    pyrs = re.findall(r"\(\s*(\d{4})[a-z]?\s*\)", item_html)
    if pyrs:
        year = pyrs[-1]
    else:
        m = re.search(r"</i>\s*\.?\s*(\d{4})[a-z]?\s*[;,]", item_html)
        if m:
            year = m.group(1)
        else:
            m = re.search(r"[.\s](\d{4})[a-z]?\s+<i", item_html)
            if m:
                year = m.group(1)

    after = item_html[b_m.end():]
    after_text = unescape(re.sub(r"<[^>]+>", "", after))
    after_text = re.sub(r"\s+", " ", after_text).strip()
    after_text = re.sub(r"\(\s*\d{4}[a-z]?\s*\)\s*\.?\s*$", "", after_text).strip()
    after_text = re.sub(r"^\s*[,:;]\s*", "", after_text).strip(" ,:;.")
    issue = ""
    im = re.search(r"\(([^)]+)\)", after_text)
    if im:
        issue = im.group(1).strip().rstrip(".")
        after_text = (after_text[:im.start()] + after_text[im.end():]).strip(" ,:;.")
    pages = after_text.replace("–", "-").strip()

    head = unescape(re.sub(r"<[^>]+>", "", head_html))
    head = re.sub(r"\s+", " ", head).strip()
    if year:
        head = re.sub(r"\s*" + re.escape(year) + r"[a-z]?\s*\.?\s*$", "", head)
    head = head.strip().rstrip(".")

    authors, title = _split_body_authors_title(head)

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "authors": authors,
    }


def _body_fallback(item_html, doi):
    """Parse refs that lack <i>/<b> structural tags.

    Tries plaintext formats: year-at-end, BMC semicolon, BMC
    colon-after-authors, book chapters, monographs, then a generic
    "Authors (YEAR) Title. Journal Vol[(Issue)][:Pages]" form. Returns
    a title-only record if none match (true books, theses, software).
    """
    text = re.sub(r"<a[^>]*>.*?</a>", " ", item_html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"https?://doi\.org/\S+", "", text)
    # BMC-style trailing bare DOI (no https://doi.org/ prefix): 'Science.
    # 1991, 251: 1351-1355. 10.1126/science.1900642.' — strip so the tail
    # anchors of the BMC/semicolon regexes below can match on pages.
    bare_doi_m = re.search(r"\s+(10\.\d{4,}/\S+?)\s*\.?\s*$", text)
    if bare_doi_m:
        if not doi:
            doi = format_doi(bare_doi_m.group(1).rstrip("."))
        text = text[: bare_doi_m.start()].rstrip()
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")

    year_end = _parse_year_at_end_plaintext(text, doi)
    if year_end is not None:
        return year_end

    # Two compact pagination separators that lose <i>/<b> styling:
    #
    # A. Semicolon:
    #    "Authors. Title. Journal. YEAR;VOL[(Issue)]:PAGES."
    # B. Colon-after-authors, comma-separated pagination (BMC/BioMed):
    #    "Authors: Title. Journal. YEAR, VOL[(Issue)]: PAGES."
    for sep1, sep2, auth_delim in (
        (";", ":", "."),      # Semicolon / Guterres / Oncogene
        (",", ":", ":"),      # BMC / Wang / Waldner
    ):
        pat = (
            r"^(?P<authors>.+?)" + re.escape(auth_delim)
            + r"\s+(?P<title>.+?)[.?!]\s+"
            + r"(?P<journal>[A-Z][^.]*?)\.\s+"
            + r"(?P<year>\d{4})\s*" + re.escape(sep1) + r"\s*"
            + r"(?P<vol>\d+)(?:\s*\((?P<issue>[^)]+)\))?\s*"
            + re.escape(sep2) + r"\s*"
            + r"(?P<pages>[\w.\-–—]+)\.?\s*$"
        )
        m = re.match(pat, text)
        if m:
            auth_str = m.group("authors")
            if auth_delim == ".":
                auth_str = auth_str.rstrip(",").strip()
            authors = _parse_body_author_list(auth_str)
            if not authors:
                authors = [
                    a.strip() for a in auth_str.split(",") if a.strip()
                ]
            return {
                "title": m.group("title").strip().rstrip("."),
                "journal": m.group("journal").strip().rstrip("."),
                "year": m.group("year"),
                "volume": m.group("vol"),
                "issue": m.group("issue") or "",
                "pages": re.sub(
                    r"[‐-—]", "-", m.group("pages")
                ).rstrip("."),
                "doi": doi,
                "authors": authors,
            }

    # Book-chapter form: "Authors. in Book Title pages (Publisher, City, Year)."
    chap_m = re.match(
        r"^(?P<authors>.+?\.)\s+in\s+(?P<book>[A-Z][^.]*?)\s+"
        r"(?P<pages>\d+[\-–—]\d+|[A-Za-z]?\d+(?:[\-–—][A-Za-z]?\d+)?)?"
        r"\s*\(([^)]*?)(?P<year>\d{4})\s*\)\.?\s*$",
        text,
    )
    if not chap_m:
        # Standalone book monograph
        mono_m = re.match(
            r"^(?P<authors>.+?\.)\s+(?P<book>[A-Z][^()]+?)\s+"
            r"\((?P<paren>[^)]*?(?:Press|Publishers?|Publishing|Freeman|"
            r"Wiley|Springer|Elsevier|Chapman\s*&\s*Hall|CRC|Academic|"
            r"University|Laboratory|INSERM|Humana|Dekker|Garland|Saunders|"
            r"Mosby|Kluwer|Blackwell|ASM)[^)]*?)"
            r"(?P<year>\d{4})\s*\)\.?\s*$",
            text,
        )
        if mono_m:
            mono_authors = _parse_body_author_list(mono_m.group("authors").rstrip(","))
            if not mono_authors:
                mono_authors = [
                    a.strip() for a in mono_m.group("authors").rstrip(",.").split(",") if a.strip()
                ]
            return {
                "title": "",
                "journal": mono_m.group("book").strip().rstrip(",.").strip(),
                "year": mono_m.group("year"),
                "volume": "",
                "issue": "",
                "pages": "",
                "doi": doi,
                "authors": mono_authors,
            }
    if chap_m:
        chap_authors = _parse_body_author_list(chap_m.group("authors").rstrip(","))
        if not chap_authors:
            chap_authors = [
                a.strip() for a in chap_m.group("authors").rstrip(",").split(",") if a.strip()
            ]
        return {
            "title": "",
            "journal": chap_m.group("book").strip().rstrip(",.").strip(),
            "year": chap_m.group("year"),
            "volume": "",
            "issue": "",
            "pages": re.sub(r"[‐-—]", "-", chap_m.group("pages") or ""),
            "doi": doi,
            "authors": chap_authors,
        }

    m = re.match(r"^(.+?)\s+\((\d{4})[a-z]?\)\.?\s+(.+)$", text)
    if not m:
        return {
            "title": text, "journal": "", "year": "",
            "volume": "", "issue": "", "pages": "", "doi": doi, "authors": [],
        }
    authors_str, year, rest = m.group(1), m.group(2), m.group(3)
    authors = _parse_body_author_list(authors_str.rstrip(","))
    if not authors:
        authors = [a.strip() for a in authors_str.rstrip(",").split(",") if a.strip()]

    # Two tail shapes observed in Springer/EMBO plaintext refs:
    #   "Journal Vol[(Issue)][:Pages]"   (colon-separated, older style)
    #   "Journal, Vol[(Issue)], Pages"   (comma-separated, EMBO/Oxford style)
    tail = re.search(
        r"[.?!]\s+(.+?),\s*(\d+)(?:\(([\d\w\-–]+)\))?,\s+([\w\-–]+)\s*\.?\s*$",
        rest,
    )
    if tail:
        title = rest[: tail.start()].rstrip(".").strip()
        journal = tail.group(1).strip().rstrip(".")
        volume = tail.group(2)
        issue = tail.group(3) or ""
        pages = tail.group(4).replace("–", "-").strip()
    else:
        tail = re.search(
            r"[.?!]\s+([^.?!]+?)\s+(\d+)(?:\(([\d\w\-–]+)\))?(?::\s*(.+?))?$",
            rest,
        )
        if tail:
            title = rest[: tail.start()].rstrip(".").strip()
            journal = tail.group(1).strip().rstrip(".")
            volume = tail.group(2) or ""
            issue = tail.group(3) or ""
            pages = (tail.group(4) or "").replace("–", "-").strip()
        else:
            book_m = re.match(
                r"^(?P<book>.+?)\.\s+[^.]+?:\s+[A-Z].*$",
                rest.rstrip("."),
            )
            if book_m:
                title = ""
                journal = book_m.group("book").strip().rstrip(".")
                volume = issue = pages = ""
            else:
                title, journal, volume, issue, pages = (
                    rest.rstrip(".").strip(), "", "", "", "",
                )

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "authors": authors,
    }


def _parse_year_at_end_plaintext(text, doi):
    """Parse 'Authors. Title. Journal Vol[, Pages] (YEAR).' without tags.

    Identifies the journal as the trailing sequence of capital-letter
    words preceded by ". ". Returns None when the text doesn't end in
    "(YEAR)" or can't locate a journal-like suffix.
    """
    core = text.rstrip(".")
    ym = re.search(r"\(\s*(\d{4})[a-z]?\s*\)\s*$", core)
    if not ym:
        return None
    year = ym.group(1)
    core = core[: ym.start()].rstrip(" ,.")

    volume = pages = ""
    m = re.search(r"\s+(\d+),\s+([\w\-–]+)\s*$", core)
    if m:
        volume = m.group(1)
        pages = m.group(2).replace("–", "-")
        core = core[: m.start()].rstrip(" ,.")
    else:
        m = re.search(r"\s+(\d+)\s*$", core)
        if m:
            volume = m.group(1)
            core = core[: m.start()].rstrip(" ,.")
        else:
            m = re.search(r"\s+([\w\-–]+)\s*$", core)
            if m and re.search(r"[\d–\-]", m.group(1)):
                pages = m.group(1).replace("–", "-")
                core = core[: m.start()].rstrip(" ,.")

    jm = re.search(
        r"(?:(?<=\.\s)|^)([A-Z][\w]*\.?(?:\s+[A-Z][\w]*\.?)*)$",
        core,
    )
    if not jm:
        return None
    journal = jm.group(1).rstrip(".").strip()
    head = core[: jm.start()].rstrip(" .")

    authors, title = _split_body_authors_title(head)
    if not authors and not title:
        return None

    return {
        "title": title,
        "journal": journal,
        "year": year,
        "volume": volume,
        "issue": "",
        "pages": pages,
        "doi": doi,
        "authors": authors,
    }


def _split_body_authors_title(head):
    """Split head text into (authors list, title string).

    Recognizes "et al." as an anchor; otherwise walks the last dotted
    "LastName, I[. I.]" match or the last compact "LastName IN" match.
    Emits authors=[] and title=head when no author pattern matches
    (consortium names, etc.).
    """
    et_al = re.search(r"\b[Ee]t al\.?", head)
    if et_al:
        authors_str = head[:et_al.end()]
        title = head[et_al.end():].lstrip(" .").rstrip(".")
        return _parse_body_author_list(authors_str), title.strip()

    last_end = 0
    for m in _DOTTED_AUTHOR_RE.finditer(head):
        last_end = m.end()
    if not last_end:
        for m in _COMPACT_AUTHOR_RE.finditer(head):
            last_end = m.end()
    if last_end:
        authors_str = head[:last_end]
        title = head[last_end:].lstrip(" .").rstrip(".")
        return _parse_body_author_list(authors_str), title.strip()
    return [], head.strip()


def _parse_body_author_list(authors_str):
    """Extract "LastName IN" strings from the author section text.

    The compact-form regex extends its surname capture leftward across
    optional lowercase particle tokens (de, van, der, di, d'Adda, ...)
    so 'de Lange T' parses as ('de Lange', 'T') instead of dropping
    the 'de' particle. Particles attach to the right-adjacent
    capitalized token; the regex itself does not enumerate the particle
    set — surname-vs-particle disambiguation is handled centrally by
    format_name when given the combined surname string.
    """
    # Normalize "and" / "&" separators to commas.
    authors_str = re.sub(r"\s*,?\s+(?:and|&)\s+", ", ", authors_str)
    authors = []
    for m in re.finditer(
        r"([A-Z][\w\-']+(?:\s[\w\-']+)*),\s+((?:[A-Z]\.\s*){1,5})",
        authors_str,
    ):
        authors.append(format_name(m.group(2).strip(), m.group(1)))
    if authors:
        return authors
    # Compact form: optional leading lowercase particle tokens (de, van,
    # der, di, d'Adda, etc.) absorbed into the surname capture.
    for m in re.finditer(
        r"((?:(?:[a-z][\w\-']*|[a-z]['’][\w\-']+)\s+)*"
        r"[A-Z][\w\-']+"
        r"(?:\s+(?:[a-z][\w\-']*|[a-z]['’][\w\-']+|[A-Z][\w\-']+))*)"
        r"\s+([A-Z]{1,5})(?=\s*(?:,|&|et al|\.|$))",
        authors_str,
    ):
        authors.append(format_name(m.group(2), m.group(1)))
    return authors


def _parse_references(html):
    """Extract the reference list.

    Returns list of {"": {title, journal, year, volume, issue, pages, doi, authors}}.

    Prefers body parsing (anchored on <i>Journal</i>/<b>Volume</b> tags)
    and falls back to citation_reference meta tags only when body entries
    are fewer.
    """
    meta_refs = [
        {"": _parse_citation_reference(unescape(m.group(1)))}
        for m in re.finditer(
            r'<meta[^>]*name=["\']?citation_reference["\']?'
            r'[^>]*content="([^"]*)"',
            html,
        )
    ]
    body_refs = [
        {"": _parse_body_reference(m.group(1))}
        for m in re.finditer(
            r'<p[^>]*class=["\']?c-article-references__text["\']?[^>]*>'
            r'(.*?)(?=<p\s|</li>)',
            html,
            re.DOTALL,
        )
    ]
    return body_refs if len(body_refs) >= len(meta_refs) else meta_refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_keywords(html):
    """Extract article-specific keywords from the Subjects list in body HTML."""
    keywords = []
    for m in re.finditer(
        r'<li[^>]*class=["\']?c-article-subject-list__subject["\']?[^>]*>'
        r'.*?<a[^>]*>([^<]+)</a>',
        html,
        re.DOTALL,
    ):
        kw = unescape(m.group(1)).strip()
        if kw:
            keywords.append(kw)
    return keywords


def _parse_abstract(html):
    """Extract abstract from <section data-title=Abstract>."""
    m = re.search(
        r'<section[^>]*data-title=["\']?Abstract["\']?[^>]*>(.*?)</section>',
        html,
        re.DOTALL,
    )
    if not m:
        return ""
    content = re.sub(r'<h[1-6][^>]*>.*?</h[1-6]>', '', m.group(1), flags=re.DOTALL)
    text = strip_tags(content).strip()
    if text.startswith("Abstract"):
        text = text[len("Abstract"):].strip()
    return text


def _extract_article(html):
    """Return the <article> element content, or full html as fallback."""
    m = re.search(r"<article[^>]*>(.*)</article>", html, re.DOTALL)
    return m.group(1) if m else html


def _section_boundaries(article):
    """Find all <section data-title=...> start positions and their titles.

    Returns list of (start_pos, end_of_opening_tag_pos, title) sorted by position.
    """
    entries = []
    for m in re.finditer(
        r'<section[^>]*data-title="([^"]*)"'
        r"|<section[^>]*data-title='([^']*)'"
        r"|<section[^>]*data-title=([^\s>\"']+)",
        article,
    ):
        title = m.group(1) or m.group(2) or m.group(3) or ""
        entries.append((m.start(), m.end(), unescape(title).strip()))
    return entries


def _find_start(article, sections):
    """Find main_text start: after Abstract and Inline Recommendations."""
    start = 0
    for i, (pos, tag_end, title) in enumerate(sections):
        if title.lower() in _PRE_BODY:
            next_pos = sections[i + 1][0] if i + 1 < len(sections) else len(article)
            if next_pos > start:
                start = next_pos
        else:
            break
    return start


def _remove_section(html, start_pattern):
    """Remove a <section> element matching start_pattern, handling nesting."""
    m = re.search(start_pattern, html)
    if not m:
        return html, False
    pos = m.end()
    depth = 1
    while depth > 0 and pos < len(html):
        next_open = re.search(r'<section[\s>]', html[pos:])
        next_close = re.search(r'</section>', html[pos:])
        if next_close is None:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos += next_open.end()
        else:
            depth -= 1
            pos += next_close.end()
    return html[:m.start()] + html[pos:], True


def _build_body(article, sections):
    """Build main_text HTML from two zones.

    Zone 1 (before first references): keep everything.
    Zone 2 (after first references): keep only supplementary sections.
    Remove all references sections.
    """
    first_ref_idx = None
    for i, (pos, tag_end, title) in enumerate(sections):
        if title.lower() in _REF_SECTIONS:
            first_ref_idx = i
            break

    if first_ref_idx is None:
        return None

    parts = []
    for i, (pos, tag_end, title) in enumerate(sections):
        tl = title.lower()
        if tl in _PRE_BODY:
            continue
        if tl in _REF_SECTIONS:
            continue

        end = sections[i + 1][0] if i + 1 < len(sections) else len(article)

        if i < first_ref_idx:
            parts.append((pos, end))
        else:
            if _SUPP_RE.search(title):
                parts.append((pos, end))

    return parts


def _parse_main_text(html):
    """Extract body text.

    Boundary rules:
      - Body sections: keep everything from abstract to before first references.
      - Supplementary: after first references, keep only sections matching
        supplement/extended data/source data/expanded view/powerpoint/appendix.
      - Remove all references sections.
    Pipeline: locate article container -> slice body zones -> extract_captions
    -> strip_common -> tags_to_text -> drop_noise.
    """
    article = _extract_article(html)
    sections = _section_boundaries(article)

    if not sections:
        return ""

    parts = _build_body(article, sections)

    if parts is None:
        start = _find_start(article, sections)
        end = len(article)
        if start >= end:
            return ""
        parts = [(start, end)]
    elif not parts:
        m = re.search(r'<div[^>]*class=["\']?main-content[^>]*>', article)
        if not m:
            return ""
        start = m.end()
        end = len(article)
        for pos, tag_end, title in sections:
            if title.lower() in _REF_SECTIONS and pos > start:
                end = pos
                break
        if start >= end:
            return ""
        parts = [(start, end)]

    # Extract abbreviation lists from pre-body sections (e.g. Inline Recommendations)
    abbr_html = ""
    for i, (pos, tag_end, title) in enumerate(sections):
        if title.lower() not in _PRE_BODY:
            break
        end = sections[i + 1][0] if i + 1 < len(sections) else len(article)
        pre_body = article[pos:end]
        for am in re.finditer(r'<dl[^>]*class=["\']?c-abbreviation[_-]list[^>]*>.*?</dl>',
                              pre_body, re.DOTALL):
            abbr_html += am.group(0)

    body_html = ""
    if abbr_html:
        body_html += "<h2>Abbreviations</h2><p></p>" + abbr_html
    for start, end in parts:
        body_html += article[start:end]

    while True:
        body_html, removed = _remove_section(
            body_html,
            r'<section[^>]*data-title=["\']?References["\']?[^>]*>'
        )
        if not removed:
            break

    body_html = extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse Springer HTML into a papers/*.json-format dict."""
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
