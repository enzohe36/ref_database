#!/usr/bin/env python3
"""Merge papers/*.json metadata into refs.json.

Usage:
    python merge_refs.py
    python merge_refs.py --patch
    python merge_refs.py --add-refs

Default: for every refs.json entry with a corresponding papers/<stem>.json,
fill empty affiliations from the paper JSON, resolve any structured refs in
the paper JSON to PMIDs via PubMed search, and union resolved PMIDs into
refs.json's references list. Existing refs.json field values are never
overwritten; the references list is the only field that is augmented (via
union) rather than left alone.

--patch copies manually-resolved PMIDs from refs_no_pmid.json into
papers/<stem>.json and refs.json (unioned), then removes them from
refs_no_pmid.json.

--add-refs collects every PMID cited in refs.json's references lists,
subtracts the PMIDs already present as refs.json keys, and invokes
get_refs.py on the remainder to fetch metadata and HTML.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

from get_refs import load_references, pubmed_throttle, save_references

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPERS_DIR = os.path.join(BASE_DIR, "papers")
NO_PMID_FILE = os.path.join(BASE_DIR, "refs_no_pmid.json")


# ---------------------------------------------------------------------------
# refs_no_pmid.json I/O
# ---------------------------------------------------------------------------

def load_no_pmid():
    if not os.path.exists(NO_PMID_FILE):
        return {}
    with open(NO_PMID_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_no_pmid(data):
    if not data:
        if os.path.exists(NO_PMID_FILE):
            os.remove(NO_PMID_FILE)
        return
    raw = json.dumps(data, indent=2, ensure_ascii=False)
    with open(NO_PMID_FILE, "w", encoding="utf-8") as f:
        f.write(raw)
        f.write("\n")


# ---------------------------------------------------------------------------
# PubMed query builder
# ---------------------------------------------------------------------------

def _surname(author):
    """Extract surname from 'LastName IN' format.

    PubMed author strings end with an all-caps initials token; drop it and
    return the remaining text. Preserves compound surnames ('de Lange T' ->
    'de Lange'; 'Nick McElhinny SA' -> 'Nick McElhinny').
    """
    parts = author.strip().split()
    if len(parts) >= 2 and parts[-1].isalpha() and parts[-1].isupper():
        return " ".join(parts[:-1])
    return author.strip()


def _title_chunks(title, n=3):
    """Chunk title into unquoted N-word [ti] AND-groups.

    Stop words are preserved verbatim. Verified empirically: quoted phrases
    return 0 hits, per-word [ti] stumbles on stop words, but unquoted
    multi-word [ti] chunks resolve correctly via PubMed's phrase matching
    on the title field.
    """
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
    """Join query groups with AND, omitting excluded keys and chunks."""
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


# ---------------------------------------------------------------------------
# PubMed esearch
# ---------------------------------------------------------------------------

_last_request_time = 0.0


def _search_pmid(query):
    """Run esearch.fcgi. Returns (pmid_or_None, count). Rate-limited per
    pubmed_throttle() (0.1 s with API key, 0.4 s without)."""
    global _last_request_time
    delay, api_suffix = pubmed_throttle()
    elapsed = time.time() - _last_request_time
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_request_time = time.time()

    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={urllib.parse.quote(query)}&retmax=2&retmode=xml"
        f"{api_suffix}"
    )
    with urllib.request.urlopen(url) as resp:
        xml_data = resp.read().decode("utf-8")
    root = ET.fromstring(xml_data)
    count = int(root.findtext(".//Count", "0"))
    ids = root.findall(".//IdList/Id")
    if count == 1 and ids:
        return ids[0].text, count
    return None, count


def _search_with_retry(groups):
    """Iteratively relax the query until a single hit is found.

    Full query -> drop one group -> drop two groups -> drop individual
    chunks within 'suspicious' groups (those that returned count>=2 when
    dropped, implying they narrow without uniquely identifying).
    """
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


def _search_structured_ref(ref):
    """Resolve a structured reference dict to a single PubMed PMID.

    DOI shortcut first (bare DOI as [doi]); falls back to the
    author + title + journal + year query with iterative relaxation.
    Returns a PMID string or None.
    """
    doi = ref.get("doi") or ""
    if doi:
        bare_doi = re.sub(r"^https?://doi\.org/", "", doi)
        if bare_doi:
            pmid, count = _search_pmid(f"{bare_doi}[doi]")
            if count == 1:
                return pmid

    return _search_with_retry(_build_query_groups(ref))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _union_pmids(existing, paper_pmids):
    """Append PMIDs from paper_pmids that are not already in existing.

    Returns (new_list, added_count). Preserves existing order; dedupes
    within paper_pmids too (in case a paper lists the same PMID twice).
    """
    out = list(existing)
    seen = set(out)
    added = 0
    for p in paper_pmids:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        added += 1
    return out, added


def _ref_identity(ref_data):
    """Stable identity for a structured reference dict.

    Prefers DOI over title; returns None if neither is populated. Used
    to detect when the same reference appears in both the paper JSON
    and refs_no_pmid.json so we can dedupe writes and drop stale
    entries once they have been resolved upstream.
    """
    if not isinstance(ref_data, dict):
        return None
    doi = (ref_data.get("doi") or "").strip().lower()
    if doi:
        return ("doi", doi)
    title = (ref_data.get("title") or "").strip().lower()
    if title:
        return ("title", title)
    return None


# ---------------------------------------------------------------------------
# Default merge — two-phase
# ---------------------------------------------------------------------------
#
# Phase 1: sequential PubMed lookup. For each papers/<stem>.json with
# unresolved structured references (empty-key entries in the refs list),
# call _search_structured_ref and write the PMID back into the paper
# JSON. Sequential because esearch is rate-limited to ~3 req/s and each
# query may iteratively relax several times. Phase 1 also prunes
# refs_no_pmid.json in place: whenever a paper reference carries a PMID
# (pre-existing or newly retrieved), any matching entry in
# refs_no_pmid[main_pmid] is dropped.
#
# Phase 2: parallel per-paper patch computation. Each worker diffs its
# paper against the corresponding refs.json entry — no network, just
# local JSON — and returns (affiliation fills, PMIDs to union,
# still-unresolved refs). The main thread applies all patches in memory
# and writes refs.json + refs_no_pmid.json once at the end. Parallel is
# safe because workers don't mutate shared state.

def _write_paper(paper_path, paper_data):
    """Persist paper_data to paper_path in the standard pretty-JSON form."""
    with open(paper_path, "w", encoding="utf-8") as f:
        json.dump(paper_data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _resolve_paper_refs(paper_path, main_pmid, no_pmid):
    """Phase 1: resolve empty-key refs and prune refs_no_pmid in place.

    For each reference in the paper JSON:
      - If it already has a PMID, drop any matching entry from
        no_pmid[main_pmid] (a user-edited resolution there is now
        stale; the paper itself is the source of truth).
      - If it has an empty key, call PubMed. On success, persist the
        PMID into the paper JSON and drop the matching no_pmid entry.

    Writes the paper JSON after every successful resolution so a crash
    loses at most one in-flight query. Runs in the main thread —
    PubMed esearch is rate-limited.

    Verification of resolved refs against no_pmid uses a one-time
    identity index of no_pmid[main_pmid] so each prune is O(1); when
    the paper has no no_pmid entry, skips the identity computation
    altogether (common case).
    """
    with open(paper_path, encoding="utf-8") as f:
        paper_data = json.load(f)
    paper_refs = paper_data.get("references") or []

    np_entry = no_pmid.get(main_pmid)
    if np_entry:
        np_list = np_entry.get("references") or []
        ident_to_positions = {}
        for idx, rd in enumerate(np_list):
            if not isinstance(rd, dict):
                continue
            ident = _ref_identity(rd[next(iter(rd))])
            if ident is not None:
                ident_to_positions.setdefault(ident, []).append(idx)
        pruned_positions = set()
    else:
        np_list = None
        ident_to_positions = None
        pruned_positions = None

    def _mark_pruned(ref_data):
        if ident_to_positions is None:
            return
        ident = _ref_identity(ref_data)
        if ident is None:
            return
        positions = ident_to_positions.pop(ident, None)
        if positions:
            pruned_positions.update(positions)

    for i, ref_dict in enumerate(paper_refs):
        if not isinstance(ref_dict, dict):
            continue
        key = next(iter(ref_dict))
        if key and re.fullmatch(r"\d+", key):
            _mark_pruned(ref_dict[key])
            continue
        if key != "":
            continue
        ref_data = ref_dict[key]
        try:
            found = _search_structured_ref(ref_data)
        except Exception:
            found = None
        if not found:
            continue
        paper_refs[i] = {found: ref_data}
        paper_data["references"] = paper_refs
        _write_paper(paper_path, paper_data)
        _mark_pruned(ref_data)
        print(json.dumps({
            "paper": os.path.basename(paper_path),
            "reference": (ref_data.get("title") or "")[:80],
            "found": found,
        }))

    if np_entry and pruned_positions:
        new_list = [r for idx, r in enumerate(np_list) if idx not in pruned_positions]
        if new_list:
            np_entry["references"] = new_list
        else:
            del no_pmid[main_pmid]
        save_no_pmid(no_pmid)


def _compute_patch(pmid, entry, paper_path):
    """Phase 2 worker: compute a merge patch for one paper.

    Returns a dict with:
      - 'affiliations': list of (author_index, aff_list) for refs.json
        authors whose affiliation is currently empty but present on the
        paper-side author with the matching display name.
      - 'paper_pmids': list of PMID strings extracted from the paper's
        resolved references (main thread unions into refs.json).
      - 'unresolved': list of still-empty-key reference dicts that go
        into refs_no_pmid.json.
      - 'stem': the paper's stem (for the refs_no_pmid payload).
    Workers touch only their own paper file; main thread owns refs.json.
    """
    with open(paper_path, encoding="utf-8") as f:
        paper_data = json.load(f)

    paper_author_aff = {
        a["author"]: a["affiliation"]
        for a in paper_data.get("authors") or []
        if isinstance(a, dict) and a.get("author") and a.get("affiliation")
    }
    affiliation_fills = []
    for i, author in enumerate(entry.get("authors") or []):
        if not isinstance(author, dict) or author.get("affiliation"):
            continue
        aff = paper_author_aff.get(author.get("author"))
        if aff:
            affiliation_fills.append((i, aff))

    paper_pmids = []
    unresolved = []
    for ref_dict in paper_data.get("references") or []:
        if not isinstance(ref_dict, dict):
            continue
        key = next(iter(ref_dict))
        if key and re.fullmatch(r"\d+", key):
            paper_pmids.append(key)
        elif key == "":
            unresolved.append(ref_dict)

    return {
        "pmid": pmid,
        "stem": entry.get("stem"),
        "affiliations": affiliation_fills,
        "paper_pmids": paper_pmids,
        "unresolved": unresolved,
    }


def _apply_patch(refs, no_pmid, patch):
    """Apply a phase-2 patch to the in-memory refs and no_pmid dicts.

    Phase 1 already pruned no_pmid of any entries whose references
    are now resolved in the paper. This step only appends the
    still-unresolved refs, skipping any whose identity is already
    present (so user edits in refs_no_pmid.json are never duplicated).
    """
    pmid = patch["pmid"]
    entry = refs[pmid]
    authors = entry.get("authors") or []
    for i, aff in patch["affiliations"]:
        if 0 <= i < len(authors):
            authors[i]["affiliation"] = aff
            print(json.dumps({
                "pmid": pmid,
                "affiliation_filled": authors[i].get("author"),
            }))
    existing = entry.setdefault("references", [])
    unioned, added = _union_pmids(existing, patch["paper_pmids"])
    if added:
        entry["references"] = unioned

    existing_entry = no_pmid.get(pmid) or {}
    existing_list = list(existing_entry.get("references") or [])
    existing_idents = set()
    for ref_dict in existing_list:
        if not isinstance(ref_dict, dict):
            continue
        key = next(iter(ref_dict))
        ident = _ref_identity(ref_dict[key])
        if ident is not None:
            existing_idents.add(ident)

    appended = list(existing_list)
    for ref_dict in patch["unresolved"]:
        if not isinstance(ref_dict, dict):
            continue
        key = next(iter(ref_dict))
        ident = _ref_identity(ref_dict[key])
        if ident is not None and ident in existing_idents:
            continue
        appended.append(ref_dict)
        if ident is not None:
            existing_idents.add(ident)

    if appended:
        no_pmid[pmid] = {
            "stem": patch["stem"] or existing_entry.get("stem"),
            "references": appended,
        }
    elif pmid in no_pmid:
        del no_pmid[pmid]


def _run_default_merge():
    refs = load_references()
    no_pmid = load_no_pmid()

    work = []
    for pmid, entry in refs.items():
        stem = entry.get("stem")
        if not stem:
            continue
        paper_path = os.path.join(PAPERS_DIR, f"{stem}.json")
        if not os.path.exists(paper_path):
            continue
        work.append((pmid, entry, paper_path))

    # Phase 1: sequential PubMed PMID resolution, eager per-paper writes,
    # in-place pruning of refs_no_pmid for refs the paper has resolved.
    print(f"Phase 1: resolving unresolved refs in {len(work)} papers (sequential).",
          file=sys.stderr)
    for main_pmid, _, paper_path in work:
        _resolve_paper_refs(paper_path, main_pmid, no_pmid)

    # Phase 2: parallel per-paper patch computation (local I/O + diff only)
    n_workers = os.cpu_count() or 1
    print(
        f"Phase 2: computing refs.json patches (parallel, {n_workers} workers).",
        file=sys.stderr,
    )
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        patches = list(ex.map(
            lambda w: _compute_patch(*w), work
        ))

    for patch in patches:
        _apply_patch(refs, no_pmid, patch)

    save_references(refs)
    save_no_pmid(no_pmid)


# ---------------------------------------------------------------------------
# --patch: apply manual resolutions from refs_no_pmid.json
# ---------------------------------------------------------------------------

def _run_patch():
    no_pmid = load_no_pmid()
    if not no_pmid:
        print("refs_no_pmid.json is empty or not found.", file=sys.stderr)
        return

    refs = load_references()
    total_moved = 0

    for pmid, obj in list(no_pmid.items()):
        stem = obj.get("stem") or refs.get(pmid, {}).get("stem")
        entries = obj.get("references") or []
        resolved_inner = []
        remaining = []
        for ref_dict in entries:
            if not isinstance(ref_dict, dict):
                remaining.append(ref_dict)
                continue
            key = next(iter(ref_dict))
            if key and re.fullmatch(r"\d+", key):
                resolved_inner.append((key, ref_dict[key]))
            else:
                remaining.append(ref_dict)

        resolved_pmids = [p for p, _ in resolved_inner]

        # Union into refs.json
        if resolved_pmids and pmid in refs:
            existing = refs[pmid].get("references") or []
            unioned, added = _union_pmids(existing, resolved_pmids)
            if added:
                refs[pmid]["references"] = unioned
                total_moved += added
                print(json.dumps({"pmid": pmid, "moved": resolved_pmids}))

        # Apply to papers/<stem>.json: match unresolved entries by title
        if stem and resolved_inner:
            paper_path = os.path.join(PAPERS_DIR, f"{stem}.json")
            if os.path.exists(paper_path):
                with open(paper_path, encoding="utf-8") as f:
                    paper_data = json.load(f)
                paper_refs = paper_data.get("references") or []
                for rp, ref_data in resolved_inner:
                    target_title = (ref_data.get("title") or "").strip()
                    for i, pr in enumerate(paper_refs):
                        if not isinstance(pr, dict):
                            continue
                        pk = next(iter(pr))
                        if pk:
                            continue
                        inner = pr[pk]
                        if (inner.get("title") or "").strip() == target_title:
                            paper_refs[i] = {rp: inner}
                            break
                paper_data["references"] = paper_refs
                with open(paper_path, "w", encoding="utf-8") as f:
                    json.dump(paper_data, f, indent=2, ensure_ascii=False)
                    f.write("\n")

        # Update or remove refs_no_pmid.json entry (stem first for readability)
        if remaining:
            no_pmid[pmid] = (
                {"stem": stem, "references": remaining}
                if stem else {"references": remaining}
            )
        else:
            del no_pmid[pmid]

    save_references(refs)
    save_no_pmid(no_pmid)
    print(f"Moved {total_moved} PMIDs to refs.json.")


# ---------------------------------------------------------------------------
# --add-refs: citation-graph expansion
# ---------------------------------------------------------------------------

def _run_add_refs():
    refs = load_references()
    cited = set()
    for entry in refs.values():
        for p in entry.get("references") or []:
            if isinstance(p, str) and p.isdigit():
                cited.add(p)
    existing = set(refs.keys())
    missing = sorted(cited - existing)
    print(
        f"Cited: {len(cited)}. Already present: {len(cited & existing)}. "
        f"Missing: {len(missing)}."
    )
    if not missing:
        return
    cmd = [sys.executable, os.path.join(BASE_DIR, "get_refs.py"), *missing]
    subprocess.run(cmd, check=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) >= 2:
        flag = sys.argv[1]
        if flag == "--patch":
            _run_patch()
            return
        if flag == "--add-refs":
            _run_add_refs()
            return
        print(
            "Usage: python merge_refs.py [--patch | --add-refs]",
            file=sys.stderr,
        )
        sys.exit(1)

    _run_default_merge()


if __name__ == "__main__":
    main()
