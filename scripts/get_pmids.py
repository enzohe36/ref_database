#!/usr/bin/env python3
"""Resolve every empty `pmid` field in JSON files via PubMed search.

Usage:
    python get_pmids.py [<pmid|json|list> ...]

No args: every papers/raw/<stem>_converted.json on disk.

PMID args are resolved to papers/raw/<stem>_converted.json via
papers/parsed/<stem>.json lookup.

JSON args are processed directly — they do NOT have to live under
papers/raw/. Any JSON works as long as the empty-pmid dicts have enough
sibling bibliographic fields (doi, title, journal, year, etc.) to form
a useful query.

A list arg is a file containing PMIDs and/or JSON paths, separated by
spaces or newlines.

Resolution scope: the script walks the JSON recursively and, for every
dict that contains an empty (or missing-value) `pmid` key, builds a
PubMed query from that dict's sibling bibliographic fields (DOI shortcut
+ author/title/journal/year iterative relaxation, with PublicationType
disambiguation when multiple matches return). On a typical
papers/raw/<stem>_converted.json this resolves both the main paper's
top-level `pmid` and each `references[i].pmid` — same code path for both,
because both sit in dicts that have the same shape of bibliographic
sibling fields.

Writes resolved PMIDs back into the same JSON file incrementally.
Sequential, not concurrent (PubMed rate limits).
"""

import json
import os
import sys
from pathlib import Path

from _cli import parse_argv
from _project import (
    pmid_to_stem,
    raw_converted_path,
    raw_dir,
)
from _pubmed import search_structured_ref


def _find_pmid_dicts(obj):
    """Yield every dict (anywhere in obj) that contains a `pmid` key."""
    if isinstance(obj, dict):
        if "pmid" in obj:
            yield obj
        for v in obj.values():
            yield from _find_pmid_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_pmid_dicts(v)


def _flat_authors(authors):
    """Normalize an `authors` value to a flat list of "LastName IN" strings.

    Accepts either references-style flat strings or main-paper-style dicts
    with `author` keys. Other shapes are skipped.
    """
    out = []
    for a in authors or []:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict) and a.get("author"):
            out.append(a["author"])
    return out


def _query_view(d):
    """Shallow copy of d with `authors` normalized for query building."""
    view = dict(d)
    view["authors"] = _flat_authors(d.get("authors"))
    return view


def solve_one(json_path_obj):
    """Resolve all empty-pmid dicts in one JSON file. Writes incrementally."""
    with open(json_path_obj, encoding="utf-8") as f:
        data = json.load(f)
    label = json_path_obj.name

    targets = list(_find_pmid_dicts(data))
    n_attempted = 0
    n_resolved = 0

    for target in targets:
        if target.get("pmid"):
            continue
        n_attempted += 1
        try:
            pmid = search_structured_ref(_query_view(target))
        except Exception as e:
            print(f"{label}: search error: {e}", file=sys.stderr)
            continue
        if not pmid:
            continue
        target["pmid"] = pmid
        n_resolved += 1
        with open(json_path_obj, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

    if n_attempted == 0:
        print(f"{label}: nothing to resolve")
    else:
        print(f"{label}: resolved {n_resolved}/{n_attempted}")


def _resolve_inputs(pmids, jsons):
    """Combine PMID args and JSON-file args into a deduped Path list."""
    paths = []
    seen = set()

    for pmid in pmids:
        stem = pmid_to_stem(pmid)
        if not stem:
            print(f"PMID {pmid}: no papers/parsed/<stem>.json", file=sys.stderr)
            continue
        cp = raw_converted_path(stem)
        if not cp.exists():
            print(f"{stem}: no papers/raw/<stem>_converted.json", file=sys.stderr)
            continue
        key = str(cp)
        if key in seen:
            continue
        seen.add(key)
        paths.append(cp)

    for jp in jsons:
        if not os.path.exists(jp):
            print(f"Not found: {jp}", file=sys.stderr)
            continue
        if jp in seen:
            continue
        seen.add(jp)
        paths.append(Path(jp))

    return paths


def _default_scan():
    """Every papers/raw/<stem>_converted.json on disk."""
    rd = raw_dir()
    if not rd.exists():
        return []
    return sorted(rd.glob("*_converted.json"))


def main():
    if not sys.argv[1:]:
        paths = _default_scan()
    else:
        parsed = parse_argv(accept={"pmids", "jsons"})
        paths = _resolve_inputs(parsed["pmids"], parsed["jsons"])

    if not paths:
        print("nothing to process", file=sys.stderr)
        return

    for jp in paths:
        try:
            solve_one(jp)
        except Exception as e:
            print(f"{jp.name}: error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
