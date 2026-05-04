#!/usr/bin/env python3
"""Fetch PubMed metadata into papers/parsed/<stem>.json.

Usage:
    python get_refs.py <pmid> [<pmid> ...]
    python get_refs.py <list>

Each PMID is fetched from PubMed efetch, parsed, and written to
papers/parsed/<stem>.json in the locked schema. PMIDs whose
papers/parsed/<stem>.json already exists are skipped (no re-fetch).

A list arg is a file containing PMIDs separated by spaces or newlines.
PMIDs and lists can be mixed in the same invocation.

This script does only metadata fetch. HTML retrieval, conversion,
reference resolution, merging, and embedding are separate scripts.
"""

import sys
import time

from _cli import parse_argv
from _project import parsed_path, pmid_to_stem
from _pubmed import fetch_xml, parse_xml, pubmed_throttle, write_parsed


def process_pmid(pmid, fetched_count):
    """Fetch + parse + write. Returns (stem_or_pmid, status)."""
    if fetched_count > 0:
        time.sleep(pubmed_throttle()[0])
    try:
        xml_data = fetch_xml(pmid)
    except Exception as e:
        return pmid, f"fetch error: {e}"

    parsed = parse_xml(xml_data)
    if parsed is None:
        return pmid, "skipped (not a journal article or retracted)"

    stem = parsed["stem"]
    if parsed_path(stem).exists():
        return stem, "skipped (parsed JSON already exists)"

    write_parsed(parsed)
    return stem, "written"


def main():
    args = parse_argv(accept={"pmids"})
    pmids = args["pmids"]
    if not pmids:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    # Fast pre-filter: skip PMIDs whose papers/parsed/*_<pmid>.json already exists.
    todo = []
    for p in pmids:
        existing_stem = pmid_to_stem(p)
        if existing_stem:
            print(f"{existing_stem}: skipped (parsed JSON already exists)")
            continue
        todo.append(p)

    fetched = 0
    for pmid in todo:
        stem, status = process_pmid(pmid, fetched)
        print(f"{stem}: {status}")
        if status == "written":
            fetched += 1


if __name__ == "__main__":
    main()
