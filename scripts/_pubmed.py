"""Shared PubMed E-utilities helpers.

Functions used across get_refs.py and get_pmid.py.
"""

import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from itertools import combinations
from pathlib import Path

from _net import polite_urlopen
from _project import repo_root, parsed_path

_PUBMED_API_FILE = repo_root() / "api_pubmed.txt"
_pubmed_throttle_cached = None


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

def pubmed_throttle():
    """Return (rate_gap_seconds, url_suffix) for PubMed E-utilities.

    With api_pubmed.txt present and non-empty: 0.11 s gap + '&api_key=<key>'.
    Without: 0.31 s gap + ''.
    """
    global _pubmed_throttle_cached
    if _pubmed_throttle_cached is not None:
        return _pubmed_throttle_cached
    key = ""
    try:
        key = _PUBMED_API_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        pass
    if key:
        _pubmed_throttle_cached = (0.11, f"&api_key={key}")
    else:
        _pubmed_throttle_cached = (0.31, "")
    return _pubmed_throttle_cached


# ---------------------------------------------------------------------------
# Stem
# ---------------------------------------------------------------------------

def make_stem(first_last_name, year, journal, pmid):
    """Filesystem-safe stem from first author's last name, year, journal, pmid.

    Latin diacritics → ASCII; punctuation/spaces → '_'; collapse repeats.
    """
    raw = f"{first_last_name} {year} {journal} {pmid}"
    nfkd = unicodedata.normalize("NFKD", raw)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_str)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug


# ---------------------------------------------------------------------------
# XML helpers + PubMed efetch
# ---------------------------------------------------------------------------

def gt(elem, path, default=""):
    """Get text from an element path."""
    el = elem.find(path) if elem is not None else None
    return el.text if el is not None and el.text else default


def fetch_xml(pmid):
    """Fetch PubMed XML for a single PMID via efetch.fcgi."""
    _, api_suffix = pubmed_throttle()
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pubmed&id={pmid}&rettype=xml&retmode=xml{api_suffix}"
    )
    with polite_urlopen(url) as resp:
        return resp.read().decode("utf-8")


def parse_xml(xml_data):
    """Parse PubMed XML into the papers/parsed/<stem>.json schema (locked key order).

    Returns dict or None when the article is not a Journal Article or is retracted.
    main_text is composed from abstract + keywords (no title prefix).
    """
    root = ET.fromstring(xml_data)
    article = root.find(".//PubmedArticle")
    if article is None:
        return None
    mc = article.find("MedlineCitation")
    art = mc.find("Article")
    jrnl = art.find("Journal")
    ji = jrnl.find("JournalIssue")
    pd = ji.find("PubDate")
    pag = art.find("Pagination")

    journal_abbrev = gt(jrnl, "ISOAbbreviation")
    year = gt(pd, "Year")
    volume = gt(ji, "Volume")
    issue = gt(ji, "Issue")

    start_page = gt(pag, "StartPage") if pag is not None else ""
    end_page = gt(pag, "EndPage") if pag is not None else ""
    if start_page and end_page:
        pages = f"{start_page}-{end_page}"
    elif start_page:
        pages = start_page
    else:
        pages = ""
    if not pages:
        for el in art.findall("ELocationID"):
            if el.get("EIdType") == "pii" and el.text:
                pages = el.text
                break

    title_el = art.find("ArticleTitle")
    title = (
        ET.tostring(title_el, encoding="unicode", method="text").strip().rstrip(".")
        if title_el is not None
        else ""
    )

    doi_raw = ""
    for el in art.findall("ELocationID"):
        if el.get("EIdType") == "doi":
            doi_raw = el.text or ""
    if not doi_raw:
        pd_data_tmp = article.find("PubmedData")
        if pd_data_tmp is not None:
            aid_list_tmp = pd_data_tmp.find("ArticleIdList")
            if aid_list_tmp is not None:
                for aid in aid_list_tmp.findall("ArticleId"):
                    if aid.get("IdType") == "doi":
                        doi_raw = aid.text or ""
    doi = f"https://doi.org/{doi_raw}" if doi_raw else ""

    authors = []
    for auth in art.findall(".//Author"):
        ln = gt(auth, "LastName")
        init = gt(auth, "Initials")
        if ln:
            affs = [
                ai.findtext("Affiliation", "").strip()
                for ai in auth.findall("AffiliationInfo")
            ]
            authors.append({
                "author": f"{ln} {init}".strip(),
                "affiliation": [a for a in affs if a],
            })

    abstract_parts = []
    for ab in art.findall(".//AbstractText"):
        label = ab.get("Label", "")
        text = ET.tostring(ab, encoding="unicode", method="text").strip()
        if label:
            abstract_parts.append(f"{label}: {text}")
        else:
            abstract_parts.append(text)
    abstract = " ".join(abstract_parts)

    keywords = []
    kw_list = mc.find("KeywordList")
    if kw_list is not None:
        for kw in kw_list.findall("Keyword"):
            if kw.text:
                keywords.append(kw.text.strip())

    publication_types = [pt.text for pt in art.findall(".//PublicationType") if pt.text]

    pd_data = article.find("PubmedData")
    pmid_from_aid = ""
    if pd_data is not None:
        aid_list = pd_data.find("ArticleIdList")
        if aid_list is not None:
            for aid in aid_list.findall("ArticleId"):
                if aid.get("IdType") == "pubmed":
                    pmid_from_aid = aid.text or ""
    pmid_final = pmid_from_aid or gt(mc, "PMID")

    first_last = ""
    for a in art.findall(".//Author"):
        ln = gt(a, "LastName")
        if ln:
            first_last = ln
            break
    stem = make_stem(first_last, year, journal_abbrev, pmid_final)

    references = []
    if pd_data is not None:
        for ref in pd_data.findall(".//Reference"):
            for aid in ref.findall(".//ArticleId"):
                if aid.get("IdType") == "pubmed" and aid.text:
                    if aid.text not in references:
                        references.append(aid.text)

    if "Journal Article" not in publication_types:
        return None
    if "Retracted Publication" in publication_types:
        return None

    main_parts = []
    if abstract:
        main_parts.append(f"Abstract: {abstract}")
    if keywords:
        main_parts.append(f"Keywords: {', '.join(keywords)}")
    main_text = "\n\n".join(main_parts)

    # Filter out Research Support publication_types (not informative)
    publication_types_filtered = [
        pt for pt in publication_types if not pt.startswith("Research Support")
    ]

    return {
        "stem": stem,
        "pmid": pmid_final,
        "doi": doi,
        "title": title,
        "journal": journal_abbrev,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "authors": authors,
        "publication_types": publication_types_filtered,
        "main_text": main_text,
        "references": references,
    }


# ---------------------------------------------------------------------------
# Parsed JSON I/O (locked key order, compact pub_types + references arrays)
# ---------------------------------------------------------------------------

PARSED_KEY_ORDER = [
    "stem", "pmid", "doi", "title", "journal", "year", "volume", "issue",
    "pages", "authors", "publication_types", "main_text", "references",
]

CONVERTED_TOPLEVEL_KEY_ORDER = PARSED_KEY_ORDER  # same shape


def _ordered(data, key_order):
    return {k: data.get(k, _default_for(k)) for k in key_order}


def _default_for(k):
    if k in ("authors", "publication_types", "references"):
        return []
    return ""


def _format_json(data, compact_keys=("publication_types", "references")):
    """Pretty-print JSON, then collapse named arrays of strings onto one line."""
    raw = json.dumps(data, indent=2, ensure_ascii=False)
    for key in compact_keys:
        def _collapse(m, _k=key):
            items = [s.strip().rstrip(",") for s in m.group(2).split("\n") if s.strip()]
            return m.group(1) + " " + ", ".join(items) + " ]"
        raw = re.sub(
            rf'("{key}": \[)\s*\n(.*?)\n\s*\]',
            _collapse,
            raw,
            flags=re.DOTALL,
        )
    return raw + "\n"


def write_parsed(data, path=None):
    """Write papers/parsed/<stem>.json (or override path) in locked key order."""
    ordered = _ordered(data, PARSED_KEY_ORDER)
    target = Path(path) if path else parsed_path(ordered["stem"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_format_json(ordered), encoding="utf-8")
    return target


def read_parsed(path):
    """Read parsed/<stem>.json. Returns dict."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Reference query (used by get_pmid.py)
# ---------------------------------------------------------------------------

def _surname(author):
    """Extract surname from 'LastName IN' or 'LastName' format."""
    if not author:
        return ""
    parts = str(author).strip().split()
    if len(parts) >= 2 and parts[-1].isalpha() and parts[-1].isupper():
        return " ".join(parts[:-1])
    return str(author).strip()


def _title_chunks(title, n=3):
    """Chunk title into unquoted N-word [ti] AND-groups."""
    words = re.sub(r"[,.\;:\?\!\(\)\[\]]", " ", title).split()
    return [" ".join(words[i:i + n]) + "[ti]" for i in range(0, len(words), n)]


def _build_query_groups(ref):
    """Turn a structured-ref dict into four AND-joinable query groups."""
    authors = ref.get("authors") or []
    title = ref.get("title") or ""
    journal = ref.get("journal") or ""
    year = ref.get("year") or ""

    author_terms = []
    for a in authors[:5]:
        if not isinstance(a, str):
            continue
        surname = _surname(a)
        if surname:
            author_terms.append(f"{surname}[au]")

    return {
        "authors": author_terms,
        "title": _title_chunks(title) if title else [],
        "journal": [f"{journal}[ta]"] if journal else [],
        "yvip": [f"{year}[dp]"] if year else [],
    }


def _join_groups(groups, exclude_keys=None, exclude_chunks=None):
    exclude_keys = exclude_keys or set()
    exclude_chunks = exclude_chunks or {}
    parts = []
    for key in ("authors", "title", "journal", "yvip"):
        if key in exclude_keys:
            continue
        skip = exclude_chunks.get(key, set())
        for i, chunk in enumerate(groups[key]):
            if i not in skip:
                parts.append(chunk)
    return " AND ".join(parts) if parts else ""


_last_request_time = 0.0


def _throttle():
    """Rate-limit per pubmed_throttle()."""
    global _last_request_time
    delay, _ = pubmed_throttle()
    elapsed = time.time() - _last_request_time
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_request_time = time.time()


def _fetch_publication_types(pmids):
    """Fetch PublicationType lists for multiple PMIDs in one efetch call.

    Returns {pmid: [publication_type, ...]}.
    """
    if not pmids:
        return {}
    _throttle()
    _, api_suffix = pubmed_throttle()
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pubmed&id={','.join(pmids)}&rettype=xml&retmode=xml{api_suffix}"
    )
    with polite_urlopen(url) as resp:
        xml_data = resp.read().decode("utf-8")
    root = ET.fromstring(xml_data)
    out = {}
    for article in root.findall(".//PubmedArticle"):
        mc = article.find("MedlineCitation")
        if mc is None:
            continue
        pmid_el = mc.find("PMID")
        if pmid_el is None or not pmid_el.text:
            continue
        pts = [pt.text for pt in article.findall(".//PublicationType") if pt.text]
        out[pmid_el.text] = pts
    return out


def _disambiguate_candidates(candidates):
    """Pick the canonical published version among multiple PMID candidates.

    Tiered preference:
      1. Journal Article and not Preprint and not Published Erratum.
      2. Preprint (only if no tier 1).
      3. Anything else (errata, etc.) — only if no tier 1 or 2.

    Returns a PMID iff exactly one candidate occupies the top non-empty tier;
    None if the top non-empty tier still has >= 2 candidates (truly ambiguous).
    """
    if not candidates:
        return None
    pt_map = _fetch_publication_types(candidates)
    if not pt_map:
        return None

    tier1, tier2, tier3 = [], [], []
    for pmid in candidates:
        pts = pt_map.get(pmid, [])
        is_preprint = "Preprint" in pts
        is_erratum = "Published Erratum" in pts
        is_article = "Journal Article" in pts
        if is_article and not is_preprint and not is_erratum:
            tier1.append(pmid)
        elif is_preprint:
            tier2.append(pmid)
        else:
            tier3.append(pmid)

    for tier in (tier1, tier2, tier3):
        if len(tier) == 1:
            return tier[0]
        if tier:
            return None  # ambiguous within top non-empty tier
    return None


def _search_pmid(query):
    """Run esearch.fcgi. Returns (pmid_or_None, count). Rate-limited.

    pmid is set when:
      - exactly one match (count == 1), OR
      - multiple matches resolve to a single canonical via PublicationType
        disambiguation (preprint/erratum vs published Journal Article).

    The returned count is the ESEARCH count when no disambiguation runs;
    when disambiguation succeeds the count is reported as 1 (so callers
    that branch on `count == 1` continue to work). When disambiguation
    fails the original count >= 2 is preserved so retry-relaxation logic
    can still flag the query as suspicious.
    """
    _throttle()
    _, api_suffix = pubmed_throttle()
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={urllib.parse.quote(query)}&retmax=5&retmode=xml"
        f"{api_suffix}"
    )
    with polite_urlopen(url) as resp:
        xml_data = resp.read().decode("utf-8")
    root = ET.fromstring(xml_data)
    count = int(root.findtext(".//Count", "0"))
    ids = [el.text for el in root.findall(".//IdList/Id") if el.text]

    if count == 0:
        return None, 0
    if count == 1 and ids:
        return ids[0], 1
    if not ids:
        return None, count
    resolved = _disambiguate_candidates(ids)
    if resolved:
        return resolved, 1
    return None, count


def _search_with_retry(groups):
    """Iteratively relax the query until a single hit is found."""
    GROUP_KEYS = ["authors", "title", "journal", "yvip"]

    query = _join_groups(groups)
    if not query:
        return None
    pmid, count = _search_pmid(query)
    if count == 1:
        return pmid

    suspicious = []
    for key in GROUP_KEYS:
        if not groups[key]:
            continue
        q = _join_groups(groups, exclude_keys={key})
        if not q:
            continue
        pmid, cnt = _search_pmid(q)
        if cnt == 1:
            return pmid
        if cnt >= 2:
            suspicious.append((key,))

    if not suspicious:
        pair_suspicious = []
        for combo in combinations(GROUP_KEYS, 2):
            if not any(groups[k] for k in combo):
                continue
            q = _join_groups(groups, exclude_keys=set(combo))
            if not q:
                continue
            pmid, cnt = _search_pmid(q)
            if cnt == 1:
                return pmid
            if cnt >= 2:
                pair_suspicious.append(combo)
        suspicious = pair_suspicious

    for sus in suspicious:
        if len(sus) == 1:
            key = sus[0]
            for i in range(len(groups[key])):
                q = _join_groups(groups, exclude_chunks={key: {i}})
                if not q:
                    continue
                pmid, cnt = _search_pmid(q)
                if cnt == 1:
                    return pmid
        else:
            for drop_full, refine in [(sus[0], sus[1]), (sus[1], sus[0])]:
                for i in range(len(groups[refine])):
                    q = _join_groups(
                        groups,
                        exclude_keys={drop_full},
                        exclude_chunks={refine: {i}},
                    )
                    if not q:
                        continue
                    pmid, cnt = _search_pmid(q)
                    if cnt == 1:
                        return pmid
    return None


def search_structured_ref(ref):
    """Resolve a structured reference dict to a PMID via DOI shortcut + iterative search."""
    doi = ref.get("doi") or ""
    if doi:
        bare_doi = re.sub(r"^https?://doi\.org/", "", doi)
        if bare_doi:
            pmid, count = _search_pmid(f"{bare_doi}[doi]")
            if count == 1:
                return pmid
    return _search_with_retry(_build_query_groups(ref))
