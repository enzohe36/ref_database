#!/usr/bin/env python3
"""Merge papers/*.json metadata into refs.json.

Usage:
    python merge_refs.py

Scans refs.json for entries with empty affiliations or references.
For each, constructs the filename as papers/[citation_in_text] [journal] [pmid].json
and fills in missing values:
- Affiliations: matched by "author" name between refs.json and papers/*.json.
- References: full citations from papers/*.json are parsed, searched on PubMed
  for PMIDs, and stored as an array of PMIDs in refs.json.

Only fills missing values; does not overwrite existing affiliations or references.
Reports which affiliations or references failed to be retrieved.
"""

import os
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from cite import load_references, save_references

PAPERS_DIR = "papers"
JOURNALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journals.json")


# ---------------------------------------------------------------------------
# Journal lookup
# ---------------------------------------------------------------------------

def _collapse_single_uppers(tokens):
    """Collapse consecutive single-uppercase tokens into one word.
    E.g. ['U', 'S', 'A'] -> ['USA'], ['Proc', 'Natl', 'Acad', 'Sci', 'U', 'S', 'A'] -> ['Proc', 'Natl', 'Acad', 'Sci', 'USA']
    """
    result = []
    i = 0
    while i < len(tokens):
        if len(tokens[i]) == 1 and tokens[i].isupper():
            # Collect consecutive single uppercase letters
            run = tokens[i]
            j = i + 1
            while j < len(tokens) and len(tokens[j]) == 1 and tokens[j].isupper():
                run += tokens[j]
                j += 1
            result.append(run)
            i = j
        else:
            result.append(tokens[i])
            i += 1
    return result


_journal_lookup = None
_journal_data = None
def _get_journal_data():
    """Load raw journal data."""
    global _journal_data
    if _journal_data is None:
        with open(JOURNALS_FILE, encoding="utf-8") as f:
            _journal_data = json.load(f)
    return _journal_data


def _get_journal_lookup():
    """Load journal lookup: MedAbbr (dots removed) -> MedAbbr (original)."""
    global _journal_lookup
    if _journal_lookup is None:
        jdata = _get_journal_data()
        _journal_lookup = {}
        for entry in jdata.values():
            abbr = entry.get('MedAbbr', '').strip()
            if abbr:
                key = re.sub(r'\.', '', abbr).strip()
                _journal_lookup[key] = abbr
    return _journal_lookup


def _build_end_index(lookup):
    """Build reverse index from a lookup dict.
    Tokenizes keys, collapses consecutive single-uppercase letters,
    indexes by last token.
    Returns: {last_token: [(collapsed_tokens, original_value)]}
    """
    end_index = {}
    for key, val in lookup.items():
        tokens = [t for t in re.split(r'[,\;:\?\! ]+', key) if t]
        if not tokens:
            continue
        collapsed = _collapse_single_uppers(tokens)
        last = collapsed[-1]
        if last not in end_index:
            end_index[last] = []
        end_index[last].append((collapsed, val))
    return end_index


_journal_end_index = None
def _get_journal_end_index():
    """Build reverse index for MedAbbr matching."""
    global _journal_end_index
    if _journal_end_index is None:
        _journal_end_index = _build_end_index(_get_journal_lookup())
    return _journal_end_index


_title_end_index = None
def _get_title_end_index():
    """Build reverse index for JournalTitle fallback matching (case-insensitive).
    Keys are lowercased for case-insensitive comparison."""
    global _title_end_index
    if _title_end_index is None:
        jdata = _get_journal_data()
        title_lookup = {}
        for entry in jdata.values():
            title = entry.get('JournalTitle', '').strip()
            abbr = entry.get('MedAbbr', '').strip()
            if title and abbr:
                key = re.sub(r'\.', '', title).strip().lower()
                title_lookup[key] = abbr
        _title_end_index = _build_end_index(title_lookup)
    return _title_end_index


def _reverse_match(tokens, token_spans, end_index, original_text, case_insensitive=False):
    """Reverse-match tokens against an end_index.

    tokens: list of token strings
    token_spans: list of (start, end) positions in original_text for each token
    Validates that yvip after journal starts with a number or paren.

    Returns (title_str, abbr, yvip_str, had_partial) or None.
    had_partial: True if a journal's suffix matched but couldn't complete
    because it needed tokens before the start of the text.
    """
    if len(tokens) < 2:
        return None

    match_tokens = [t.lower() for t in tokens] if case_insensitive else tokens
    had_partial = False

    for pos in range(len(match_tokens) - 2, -1, -1):
        tok = match_tokens[pos]
        if tok not in end_index:
            continue

        best = None
        for j_tokens, abbr in end_index[tok]:
            j_len = len(j_tokens)
            start = pos - j_len + 1
            if start < 0:
                # Partial match: journal suffix matches but needs tokens
                # before the start of the text
                # Check if the available tokens do match the tail of j_tokens
                available = pos + 1  # number of tokens from 0..pos
                tail = j_tokens[-available:]
                if match_tokens[:pos + 1] == tail:
                    had_partial = True
                continue
            if match_tokens[start:pos + 1] == j_tokens:
                if best is None or j_len > len(best[0]):
                    best = (j_tokens, abbr, start)

        if best:
            j_tokens, abbr, start = best
            title_str = original_text[:token_spans[start][0]].rstrip(' .,;:') if start > 0 else ''
            yvip_str = original_text[token_spans[pos][1]:].lstrip(' .,;:')
            if _is_yvip_start(yvip_str):
                return title_str, abbr, yvip_str, had_partial

    return None if not had_partial else ('', None, '', True)


def _is_yvip_start(yvip_str):
    """Check if yvip_str starts with a yvip-like token (number or paren-enclosed)."""
    if not yvip_str:
        return True  # empty yvip is fine (no yvip present)
    first = yvip_str.split()[0] if yvip_str.split() else ''
    return bool(re.match(r'^[\d(]', first))


def match_journal(text):
    """Match journal by reverse-matching from end of text.

    1. Tokenize text with spans, collapse consecutive single uppercase
    2. Try MedAbbr index first; validate yvip starts with number/paren
    3. If no valid match, try JournalTitle index as fallback (case-insensitive)

    Returns: (title_str, journal_abbr, yvip_str) or (None, None, None).
    """
    # Tokenize with spans
    # Tokenize: parenthesized groups as single tokens (dots inside preserved),
    # then regular tokens split at punctuation+space
    raw_tokens = []
    raw_spans = []
    for m in re.finditer(r'\([^)]*\)|[^,.\;:\?\! ()]+', text):
        tok = m.group()
        # Strip dots from token (but keep parens)
        tok_clean = re.sub(r'\.', '', tok)
        if tok_clean:
            raw_tokens.append(tok_clean)
            raw_spans.append((m.start(), m.end()))

    # Collapse consecutive single uppercase tokens
    tokens = []
    spans = []
    i = 0
    while i < len(raw_tokens):
        if len(raw_tokens[i]) == 1 and raw_tokens[i].isupper():
            run = raw_tokens[i]
            start_span = raw_spans[i][0]
            j = i + 1
            while j < len(raw_tokens) and len(raw_tokens[j]) == 1 and raw_tokens[j].isupper():
                run += raw_tokens[j]
                j += 1
            tokens.append(run)
            spans.append((start_span, raw_spans[j - 1][1]))
            i = j
        else:
            tokens.append(raw_tokens[i])
            spans.append(raw_spans[i])
            i += 1

    # Try MedAbbr (case-sensitive)
    result = _reverse_match(tokens, spans, _get_journal_end_index(), text)
    if result and result[1] is not None:
        return result[0], result[1], result[2]

    had_partial = result[3] if result else False

    # Fallback: try JournalTitle (case-insensitive)
    result = _reverse_match(tokens, spans, _get_title_end_index(), text, case_insensitive=True)
    if result and result[1] is not None:
        return result[0], result[1], result[2]

    if not had_partial and result:
        had_partial = result[3]

    return None, None, None, had_partial


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def stem_for_entry(pmid, entry):
    """Construct [citation_in_text] [journal] [pmid] stem from refs.json entry."""
    citation = entry.get("citation_in_text", "")
    journal = entry.get("journal", "")
    return f"{citation} {journal} {pmid}"


def has_empty_affiliations(entry):
    for author in entry.get("authors", []):
        if not author.get("affiliation"):
            return True
    return False


def has_empty_references(entry):
    return not entry.get("references")


# ---------------------------------------------------------------------------
# Initial-like detection
# ---------------------------------------------------------------------------

def _is_initial_default(word):
    """Default initial: 1-3 uppercase only. Authors included in query."""
    letters = re.sub(r'[^A-Za-z]', '', word)
    return bool(letters) and letters.isupper() and len(letters) <= 3


def _is_initial_tolerance(word):
    """Tolerance initial: authors dropped from query.
    4+ all-uppercase, or 2+ uppercase with 0-2 lowercase around each."""
    letters = re.sub(r'[^A-Za-z]', '', word)
    if not letters:
        return False
    if letters.isupper() and len(letters) >= 4:
        return True
    if not letters.isupper() and re.match(r'^(?:[a-z]{0,2}[A-Z]){2,}[a-z]{0,2}$', letters):
        return True
    return False


def _is_initial_like(word):
    return _is_initial_default(word) or _is_initial_tolerance(word)


# ---------------------------------------------------------------------------
# Stage 1: Extract authors and year after authors
# ---------------------------------------------------------------------------

def extract_authors(citation):
    """Extract author list from the beginning of a citation.

    Returns (last_names, query_names, rest) or (None, None, citation).
    """
    NW = r"[A-Za-z][A-Za-z'\-]+"
    LP = r"(?:[A-Za-z][a-z'\-]+\s+)*"
    LN = LP + NW
    I_DOT = r"[A-Z]\.\s*"
    WTOK = r"[A-Za-z][A-Za-z.\-]*"

    # Last initial may omit dot; must not be followed by any letter
    I_DOTS_TRAIL = rf"(?:{I_DOT})*[A-Z]\.?\s*(?![A-Za-z])"
    author_pats = [
        re.compile(rf"{LN},\s*{I_DOTS_TRAIL}"),
        re.compile(rf"(?:{I_DOT}){{1,4}}{LN}"),
        re.compile(rf"{LN}\s+(?P<tok>{WTOK})(?=[\s,;&.):\[\]]|$)"),
    ]
    PAT_VALIDATE_IDX = 2

    et_al_pat = re.compile(r",?\s*et\s+al(?=[\s.]|$)")
    sep_pat = re.compile(r"[\s,.;]*(?:and|&|[Jj]r\.?|2nd|3rd)[\s,.;]*|[\s,.;]+")
    breaker_pat = re.compile(r'[\s]*([,.])\s*([A-Za-z][A-Za-z\'\-]*)')

    pos = 0
    last_end = 0
    count = 0

    while pos < len(citation):
        m = et_al_pat.match(citation, pos)
        if m and count > 0:
            last_end = m.end()
            break

        matched = False
        for i, pat in enumerate(author_pats):
            m = pat.match(citation, pos)
            if m:
                if i == PAT_VALIDATE_IDX:
                    tok = m.group('tok')
                    if not _is_initial_like(tok):
                        continue
                pos = m.end()
                last_end = pos
                count += 1
                matched = True
                break

        if not matched:
            break

        match_text = citation[:last_end].rstrip()
        last_word = match_text.split()[-1]
        remaining = citation[last_end:]
        brk = breaker_pat.match(remaining)
        if (brk and not _is_initial_like(last_word)
                and not _is_initial_like(brk.group(2))
                and brk.group(2).lower() not in ('and', '&', 'jr', '2nd', '3rd')):
            break

        m = sep_pat.match(citation, pos)
        if m and m.end() > m.start():
            pos = m.end()
            et_look = re.match(
                rf'(?:{LN}\s+(?P<tok2>{WTOK})\s+)?et\s+al(?=[\s.]|$)',
                citation[pos:])
            if et_look:
                last_end = pos + et_look.end()
                break
        else:
            break

    if count > 0:
        author_str = citation[:last_end].rstrip()
        cleaned = re.sub(r',?\s*et\s+al\.?', '', author_str)
        cleaned = re.sub(r'\band\b|&', ',', cleaned)
        parts = re.split(r'[,;]+', cleaned)
        last_names = []
        query_names = []
        for part in parts:
            words = part.strip().split()
            name_words = []
            tolerance_only = False
            for w in words:
                if not w:
                    continue
                if _is_initial_default(w):
                    continue
                if _is_initial_tolerance(w):
                    tolerance_only = True
                    continue
                name_words.append(w)
            stripped = ' '.join(name_words).strip(' .')
            if stripped:
                last_names.append(stripped)
                if not tolerance_only:
                    query_names.append(stripped)
        return last_names, query_names, citation[last_end:]
    return None, None, citation


def extract_authors_year(rest):
    """Extract year immediately after author list.
    Returns (year, remaining) or (None, rest).
    """
    rest = rest.strip(' ,.')
    m = re.match(r'\(?([12]\d{3})\)?[,.\s]*', rest)
    if m:
        return m.group(1), rest[m.end():]
    return None, rest


# ---------------------------------------------------------------------------
# Stages 3+4: Combined journal + yvip detection
# ---------------------------------------------------------------------------

_PUNCT_STRIP = r'^[\s,.\;:\(\)\[\]\?\! ]+|[\s,.\;:\(\)\[\]\?\! ]+$'


def _strip_punct(seg):
    """Strip leading/trailing punctuation and space. Keep '-' for page ranges."""
    return re.sub(_PUNCT_STRIP, '', seg)


def _strip_range(seg):
    """Strip punctuation, then if contains '-', keep only before '-'."""
    seg = _strip_punct(seg)
    if '-' in seg:
        seg = seg.split('-')[0].strip()
    return _strip_punct(seg)


def parse_yvip_query(yvip_str, year_from_authors):
    """Parse yvip string (with original punctuation) for year and volume/issue/pages.

    1. Extract year (only if no year after authors):
       - First token if 4-digit [12]xxx
       - Or last ()-enclosed 4-digit at end
       Drop year from yvip.

    2. If \\(.*\\) found: before()=volume, inside()=issue, after()=pages.
       Strip leading/trailing punctuation from each.

    3. Else split at ,|;|:
       - 1 part: pages
       - 2 parts: volume + pages
       - 3 parts: volume + issue + pages
       Strip leading/trailing punctuation from each.

    Returns (year, query_string).
    """
    if not yvip_str:
        return None, ''

    # Remove "pp." / "pp" from yvip
    rest = re.sub(r'(?<![A-Za-z])pp\.?(?![A-Za-z])', '', yvip_str).strip()
    year = None

    if not year_from_authors:
        # Year at end: (yyyy)
        m = re.search(r'\(([12]\d{3})\)\s*$', rest)
        if m:
            year = m.group(1)
            rest = rest[:m.start()].strip()
        else:
            # Year at beginning: yyyy followed by punctuation
            m = re.match(r'\s*([12]\d{3})\s*[,.\;:]', rest)
            if m:
                year = m.group(1)
                rest = rest[m.end():].strip()
            else:
                # Year as first (yyyy) anywhere: treat as separator
                m = re.search(r'\(([12]\d{3})\)', rest)
                if m:
                    year = m.group(1)
                    before = rest[:m.start()].strip()
                    after = rest[m.end():].strip()
                    # Rejoin before and after with comma as separator
                    parts = [p for p in [before, after] if _strip_punct(p)]
                    rest = ', '.join(parts)

    rest = _strip_punct(rest)
    if not rest:
        return year, ''

    # Try () match: volume(issue)pages
    m = re.search(r'\((.+?)\)', rest)
    if m:
        vol = _strip_range(rest[:m.start()])
        issue = _strip_range(m.group(1))
        pages = _strip_range(rest[m.end():])
        pts = []
        if vol: pts.append(f'{vol}[vi]')
        if issue: pts.append(f'{issue}[ip]')
        if pages: pts.append(f'{pages}[pg]')
        return year, ' AND '.join(pts)

    # No parens: split at ,|;|:
    parts = [_strip_range(s) for s in re.split(r'[,;:]', rest) if _strip_range(s)]
    if len(parts) == 3:
        return year, f'{parts[0]}[vi] AND {parts[1]}[ip] AND {parts[2]}[pg]'
    if len(parts) == 2:
        return year, f'{parts[0]}[vi] AND {parts[1]}[pg]'
    if len(parts) == 1:
        return year, f'{parts[0]}[pg]'

    return year, ''


_STOP_WORDS = {'a', 'an', 'the', 'in', 'on', 'at', 'to', 'for', 'of', 'by',
               'with', 'from', 'into', 'through', 'during', 'before', 'after',
               'above', 'below', 'between', 'under', 'over', 'and', 'but',
               'or', 'nor', 'as', 'is', 'are', 'was', 'were'}


def clean_title_query(title_str):
    """Clean title text: remove stop words, then 3-word chunked query."""
    if not title_str:
        return ''
    text = re.sub(r'[,.\;:\?\!\(\)\[\]\+\-]', ' ', title_str)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return ''
    words = [w for w in text.split() if w.lower() not in _STOP_WORDS]
    if not words:
        return ''
    chunks = [' '.join(words[i:i+3]) for i in range(0, len(words), 3)]
    return ' AND '.join(chunks)


# ---------------------------------------------------------------------------
# Stage 5: Form query and search PubMed
# ---------------------------------------------------------------------------

def build_query_groups(query_names, year, journal, title_str, yvip_str):
    """Build query as 4 groups: authors, title, journal, yvip.

    Returns dict with keys 'authors', 'title', 'journal', 'yvip',
    each a list of query chunks.
    """
    yvip_year, yvip_query = parse_yvip_query(yvip_str, year)
    query_year = year or yvip_year
    title_query = clean_title_query(title_str)

    groups = {
        'authors': [f'{name}[au]' for name in (query_names or [])[:10]],
        'title': title_query.split(' AND ') if title_query else [],
        'journal': [f'{journal}[ta]'] if journal else [],
        'yvip': [],
    }
    if query_year:
        groups['yvip'].append(f'{query_year}[dp]')
    if yvip_query:
        groups['yvip'].extend(yvip_query.split(' AND '))
    return groups


def _join_groups(groups, exclude_keys=None, exclude_chunks=None):
    """Join query groups into a single query string, optionally excluding
    entire groups or specific chunks within a group."""
    exclude_keys = exclude_keys or set()
    exclude_chunks = exclude_chunks or {}  # {group_key: set of chunk indices}
    parts = []
    for key in ('authors', 'title', 'journal', 'yvip'):
        if key in exclude_keys:
            continue
        chunks = groups[key]
        skip = exclude_chunks.get(key, set())
        for i, chunk in enumerate(chunks):
            if i not in skip:
                parts.append(chunk)
    return ' AND '.join(parts) if parts else ''


_last_request_time = 0

def search_pmid(query):
    """Search PubMed for a query. Returns (pmid, count).
    Rate-limited to max 2 requests/second."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < 0.4:
        time.sleep(0.4 - elapsed)
    _last_request_time = time.time()

    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={urllib.parse.quote(query)}&retmax=2&retmode=xml"
    )
    with urllib.request.urlopen(url) as resp:
        xml_data = resp.read().decode("utf-8")
    root = ET.fromstring(xml_data)
    count = int(root.findtext(".//Count", "0"))
    ids = root.findall(".//IdList/Id")
    if count == 1 and ids:
        return ids[0].text, count
    return None, count


def search_with_retry(groups, citation_string):
    """Search PubMed with retry logic.

    Step 0: full formatted query
    Step 0a: if 2+, try unformatted citation; if 0 or 2+, warn and skip
    Step 1: drop one group at a time
    Step 2: drop two groups at a time
    Step 3: refine suspicious groups by dropping chunks
    Step 3a: last resort with unformatted citation
    """
    from itertools import combinations

    GROUP_KEYS = ['authors', 'title', 'journal', 'yvip']

    # Step 0: full formatted query
    query = _join_groups(groups)
    if not query:
        return None, 0, "empty query"
    pmid, count = search_pmid(query)
    if count == 1:
        return pmid, count, None

    # Step 0a: if 2+, try unformatted citation
    if count >= 2:
        pmid2, count2 = search_pmid(citation_string)
        if count2 == 1:
            return pmid2, count2, None
        return None, count, f"step 0a: unformatted returned {count2}"

    # Step 1: drop one group
    suspicious = []
    for key in GROUP_KEYS:
        if not groups[key]:
            continue
        q = _join_groups(groups, exclude_keys={key})
        if not q:
            continue
        pmid, cnt = search_pmid(q)
        if cnt == 1:
            return pmid, cnt, None
        if cnt >= 2:
            suspicious.append((key,))

    # Step 2: drop two groups
    if not suspicious:
        suspicious2 = []
        for combo in combinations(GROUP_KEYS, 2):
            if not any(groups[k] for k in combo):
                continue
            q = _join_groups(groups, exclude_keys=set(combo))
            if not q:
                continue
            pmid, cnt = search_pmid(q)
            if cnt == 1:
                return pmid, cnt, None
            if cnt >= 2:
                suspicious2.append(combo)
        if not suspicious2:
            return None, 0, "step 2: all returned 0"
        suspicious = suspicious2

    # Step 3: refine suspicious groups
    for sus in suspicious:
        if len(sus) == 1:
            # Drop one chunk within the suspicious group
            key = sus[0]
            for i in range(len(groups[key])):
                q = _join_groups(groups, exclude_chunks={key: {i}})
                if not q:
                    continue
                pmid, cnt = search_pmid(q)
                if cnt == 1:
                    return pmid, cnt, None
        elif len(sus) == 2:
            # Keep one group fully dropped, drop chunks from the other
            for drop_full, refine in [(sus[0], sus[1]), (sus[1], sus[0])]:
                for i in range(len(groups[refine])):
                    q = _join_groups(groups, exclude_keys={drop_full},
                                     exclude_chunks={refine: {i}})
                    if not q:
                        continue
                    pmid, cnt = search_pmid(q)
                    if cnt == 1:
                        return pmid, cnt, None

    # Step 3a: last resort
    pmid, cnt = search_pmid(citation_string)
    if cnt == 1:
        return pmid, cnt, None
    return None, cnt, f"step 3a: unformatted returned {cnt}"


def parse_citation(citation_string):
    """Parse a full citation string and build a PubMed search query.

    Stages:
    1. Extract authors (Jr., 2nd, 3rd treated as separators)
    1.5. Remove noise words (eds, et al, Jr, 2nd, 3rd)
    2. Extract year after authors
    3+4. Delete pmid/doi/https, detect journal (reverse match), split into
         title/journal/yvip
    5. Form query
    """
    # Stage 1: authors (Jr., 2nd, 3rd consumed as separators; et al by et_al_pat)
    _, query_names, rest = extract_authors(citation_string)

    # Stage 1.5: remove noise words from rest
    noise = r'(?<![A-Za-z])(?:eds?|et\s+al|[Jj]r|2nd|3rd)\.?(?![A-Za-z])'
    rest = re.sub(noise, '', rest)
    rest = re.sub(r'\s+', ' ', rest).strip()

    # Stage 2: year after authors
    year, rest_after_year = extract_authors_year(rest)

    # Stage 3+4: delete metadata, detect journal, get title + yvip
    rest_for_journal = rest_after_year
    # Delete pmid, doi, available at, https and everything after
    rest_for_journal = re.sub(r'(?:https?:|pmid:|doi:|available\s+at:).*', '',
                               rest_for_journal, flags=re.IGNORECASE).strip()

    result = match_journal(rest_for_journal)
    title_str, journal, yvip_str = result[0], result[1], result[2]
    had_partial = result[3] if len(result) > 3 else False

    if journal is None and had_partial:
        # Partial match found but journal name extends into author-consumed text.
        # Retry on the full citation.
        full_preprocessed = re.sub(r'(?:https?:|pmid:|doi:|available\s+at:).*', '',
                                    citation_string, flags=re.IGNORECASE).strip()
        full_preprocessed = re.sub(noise, '', full_preprocessed)
        full_preprocessed = re.sub(r'\s+', ' ', full_preprocessed).strip()
        result2 = match_journal(full_preprocessed)
        title_str, journal, yvip_str = result2[0], result2[1], result2[2]
        if journal is not None:
            year = None  # re-extract from yvip

    if journal is None:
        return None  # journal not found

    # Stage 5: build query groups
    return build_query_groups(query_names, year, journal, title_str, yvip_str)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    refs = load_references()
    fetch_count = 0

    for pmid, entry in refs.items():
        needs_aff = has_empty_affiliations(entry)
        needs_ref = has_empty_references(entry)

        if not needs_aff and not needs_ref:
            continue

        stem = stem_for_entry(pmid, entry)
        filepath = os.path.join(PAPERS_DIR, f"{stem}.json")

        if not os.path.exists(filepath):
            if needs_aff or needs_ref:
                print(json.dumps({"pmid": pmid, "error": f"file not found: {filepath}"}))
            continue

        with open(filepath, encoding="utf-8") as f:
            paper_data = json.load(f)

        # Fill empty affiliations
        if needs_aff:
            paper_authors = paper_data.get("authors", [])
            if paper_authors:
                paper_map = {a["author"]: a.get("affiliation", [])
                             for a in paper_authors}
                filled = 0
                missing = []
                for author in entry["authors"]:
                    if not author.get("affiliation"):
                        aff = paper_map.get(author["author"], [])
                        if aff:
                            author["affiliation"] = aff
                            filled += 1
                        else:
                            missing.append(author["author"])
                msg = {"pmid": pmid, "affiliations_filled": filled}
                if missing:
                    msg["affiliations_missing"] = missing
                print(json.dumps(msg))
            else:
                missing = [a["author"] for a in entry["authors"]
                           if not a.get("affiliation")]
                print(json.dumps({"pmid": pmid, "affiliations_filled": 0,
                                  "affiliations_missing": missing}))

        # Fill empty references
        if needs_ref:
            citations = paper_data.get("references", [])
            if not citations:
                print(json.dumps({"pmid": pmid, "references_filled": 0,
                                  "references_missing": "no references in source"}))
                continue

            resolved_pmids = []
            warnings = []
            for citation_string in citations:
                # Parse citation
                groups = parse_citation(citation_string)
                if groups is None:
                    warnings.append({"type": "journal parsing failed",
                                     "citation": citation_string[:120]})
                    continue

                # Search PubMed with retry
                result = None
                for attempt in range(2):
                    try:
                        result_pmid, count, warn = search_with_retry(
                            groups, citation_string)
                        if result_pmid:
                            result = result_pmid
                        elif warn:
                            warnings.append({"type": warn,
                                             "citation": citation_string[:120],
                                             "query": _join_groups(groups)})
                        break
                    except Exception as e:
                        if attempt == 0:
                            continue  # retry once on error
                        warnings.append({"type": "search error",
                                         "citation": citation_string[:120],
                                         "error": str(e)})

                if result:
                    resolved_pmids.append(result)
                    print(json.dumps({"pmid": pmid,
                                      "reference": citation_string[:80],
                                      "found": result}))

            # Write resolved PMIDs to refs.json (even if some citations failed)
            if resolved_pmids:
                entry["references"] = resolved_pmids
            print(json.dumps({"pmid": pmid,
                              "references_resolved": len(resolved_pmids),
                              "references_total": len(citations)}))

            # Write warnings to log file
            if warnings:
                log_entry = {"pmid": pmid, "file": filepath,
                             "warnings": warnings}
                with open("merge_refs.log", "a", encoding="utf-8") as log:
                    log.write(json.dumps(log_entry) + "\n")

    save_references(refs)


if __name__ == "__main__":
    main()
