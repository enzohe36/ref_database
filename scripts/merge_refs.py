#!/usr/bin/env python3
"""Merge papers/raw/<stem>_converted.json into papers/parsed/<stem>.json.

Usage:
    python merge_refs.py [<pmid|list> ...]

No args: every papers/parsed/<stem>.json with a corresponding
papers/raw/<stem>_converted.json.

A list arg is a file containing PMIDs separated by spaces or newlines.

For each pair (parsed/<stem>.json, raw/<stem>_converted.json):
  - authors: fill empty author affiliations from _converted.json (matched by
    surname). Existing parsed authors are not added to or removed; only empty
    affiliations are populated.
  - main_text: replace parsed/<stem>.json's value when _converted.json's
    main_text qualifies (>= 5000 chars and >= 5 references — the same bar
    convert_html.py uses for "successful conversion"). Otherwise leave as-is.
  - references: union the parsed PMID list with the non-empty pmid fields
    from _converted.json's references array.

Runs in parallel (ThreadPoolExecutor). PMID resolution for empty `pmid`
fields in `_converted.json`'s references[] is a separate step; run
get_pmids.py before merge_refs.py if needed.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from _cli import parse_argv
from _project import (
    parsed_dir,
    parsed_path,
    pmid_to_stem,
    raw_converted_path,
)
from _pubmed import write_parsed

MIN_MAIN_TEXT_LEN = 5000
MIN_REFERENCES = 5


def _surname_only(author_string):
    """Strip trailing all-caps initials token: 'LastName IN' -> 'LastName'."""
    parts = (author_string or "").strip().split()
    if len(parts) >= 2 and parts[-1].isalpha() and parts[-1].isupper():
        return " ".join(parts[:-1])
    return (author_string or "").strip()


def _merge_authors(parsed_authors, converted_authors):
    """Fill empty 'affiliation' lists in parsed_authors from converted_authors.

    Matching: prefer exact full author string match, fall back to surname.
    Only existing parsed authors are touched; no additions.
    """
    by_full = {}
    by_surname = {}
    for a in converted_authors or []:
        if not isinstance(a, dict):
            continue
        full = a.get("author")
        affs = a.get("affiliation") or []
        if not full or not affs:
            continue
        by_full[full] = affs
        by_surname.setdefault(_surname_only(full), affs)

    out = []
    for a in parsed_authors or []:
        if not isinstance(a, dict):
            out.append(a)
            continue
        if a.get("affiliation"):
            out.append(a)
            continue
        full = a.get("author") or ""
        affs = by_full.get(full) or by_surname.get(_surname_only(full))
        if affs:
            a = dict(a)
            a["affiliation"] = list(affs)
        out.append(a)
    return out


def _merge_references(parsed_refs, converted_refs):
    """Union parsed PMID list with pmid fields from converted refs[]."""
    seen = set()
    out = []
    for p in parsed_refs or []:
        if not p:
            continue
        s = str(p)
        if s not in seen:
            seen.add(s)
            out.append(s)
    for r in converted_refs or []:
        if not isinstance(r, dict):
            continue
        pmid = r.get("pmid")
        if not pmid:
            continue
        s = str(pmid)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _converted_main_text_qualifies(converted):
    """True iff _converted.json's main_text + references meet the quality bar
    used by convert_html.py to decide whether HTML conversion succeeded."""
    text = converted.get("main_text") or ""
    refs = converted.get("references") or []
    return len(text) >= MIN_MAIN_TEXT_LEN and len(refs) >= MIN_REFERENCES


def merge_one(parsed_path_obj, converted_path_obj):
    """Apply _converted.json updates onto parsed/<stem>.json. Writes if changed."""
    with open(parsed_path_obj, encoding="utf-8") as f:
        parsed = json.load(f)
    with open(converted_path_obj, encoding="utf-8") as f:
        converted = json.load(f)

    parsed["authors"] = _merge_authors(
        parsed.get("authors"), converted.get("authors")
    )

    if _converted_main_text_qualifies(converted):
        parsed["main_text"] = converted.get("main_text") or ""

    parsed["references"] = _merge_references(
        parsed.get("references"), converted.get("references")
    )

    write_parsed(parsed, path=parsed_path_obj)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_pmids_to_pairs(pmids):
    """Map PMID args to (parsed_path, converted_path) tuples."""
    pairs = []
    seen = set()
    for pmid in pmids:
        stem = pmid_to_stem(pmid)
        if not stem:
            print(f"PMID {pmid}: no papers/parsed/<stem>.json", file=sys.stderr)
            continue
        pp = parsed_path(stem)
        cp = raw_converted_path(stem)
        if not cp.exists():
            print(f"{stem}: no papers/raw/<stem>_converted.json", file=sys.stderr)
            continue
        key = str(pp)
        if key in seen:
            continue
        seen.add(key)
        pairs.append((pp, cp))
    return pairs


def _default_scan():
    """Every parsed/<stem>.json with a corresponding raw/<stem>_converted.json."""
    pd = parsed_dir()
    if not pd.exists():
        return []
    pairs = []
    for pp in sorted(pd.glob("*.json")):
        stem = pp.stem
        cp = raw_converted_path(stem)
        if cp.exists():
            pairs.append((pp, cp))
    return pairs


def _merge_safe(pair):
    pp, cp = pair
    try:
        merge_one(pp, cp)
        print(f"{pp.stem}: merged")
    except Exception as e:
        print(f"{pp.stem}: error: {e}", file=sys.stderr)


def main():
    if not sys.argv[1:]:
        pairs = _default_scan()
    else:
        parsed = parse_argv(accept={"pmids"})
        pairs = _resolve_pmids_to_pairs(parsed["pmids"])

    if not pairs:
        print("nothing to process", file=sys.stderr)
        return

    print(f"merging {len(pairs)} parsed/<stem>.json files")
    n_workers = min(16, max(1, (os.cpu_count() or 1) * 2))
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        list(pool.map(_merge_safe, pairs))


if __name__ == "__main__":
    main()
