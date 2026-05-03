#!/usr/bin/env python3
"""Fetch PubMed abstracts and write lightweight papers/<stem>.json.

Usage:
    python fetch_abstracts.py <pmid> [<pmid> ...]
    python fetch_abstracts.py --query "<pubmed query>" [--retmax N]

Reuses get_refs.py PubMed parsing. Bypasses HTML retrieval entirely:
papers/<stem>.json gets main_text = title + abstract + keywords. refs.json
is updated via append_to_references for new PMIDs. Idempotent: skips PMIDs
whose papers/<stem>.json already has non-empty main_text.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from get_refs import (
    fetch_xml,
    parse_xml,
    append_to_references,
    load_references,
    pubmed_throttle,
    PAPERS_DIR,
)


def search_pmids_with_retmax(query, retmax):
    """PubMed esearch with a configurable retmax."""
    _, api_suffix = pubmed_throttle()
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={urllib.parse.quote(query)}"
        f"&retmax={retmax}&retmode=xml{api_suffix}"
    )
    with urllib.request.urlopen(url) as resp:
        xml_data = resp.read().decode("utf-8")
    root = ET.fromstring(xml_data)
    return [el.text for el in root.findall(".//IdList/Id")]


def has_main_text(stem):
    """True if papers/<stem>.json exists with non-empty main_text."""
    path = os.path.join(PAPERS_DIR, stem + ".json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    return bool(data.get("main_text"))


def write_paper_json(parsed):
    """Write papers/<stem>.json with main_text = title + abstract + keywords.

    Field shape mirrors convert_html.py output so downstream scripts
    (search_refs.py) treat it identically.
    """
    stem = parsed["citation_short"]
    title = parsed.get("title", "") or ""
    abstract = parsed.get("abstract", "") or ""
    keywords = parsed.get("keywords") or []
    main_text = f"{title}\n\nAbstract: {abstract}"
    if keywords:
        main_text += f"\n\nKeywords: {', '.join(keywords)}"

    authors = [
        {"author": a["name"], "affiliation": a.get("affiliation", [])}
        for a in parsed.get("_authors_raw", [])
    ]

    data = {
        "stem": stem,
        "pmid": parsed["pmid"],
        "title": title,
        "journal": parsed.get("journal", ""),
        "year": parsed.get("year", ""),
        "doi": parsed.get("doi", ""),
        "authors": authors,
        "references": [],
        "main_text": main_text,
    }
    path = os.path.join(PAPERS_DIR, stem + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return stem


def process_pmid(pmid, fetched_count):
    """Fetch, parse, write JSON, update refs.json. Returns (stem, status)."""
    refs = load_references()
    if pmid in refs:
        stem = refs[pmid].get("stem", "")
        if stem and has_main_text(stem):
            return stem, "skipped (already has main_text)"

    if fetched_count > 0:
        time.sleep(pubmed_throttle()[0])
    try:
        xml_data = fetch_xml(pmid)
    except Exception as e:
        return pmid, f"fetch error: {e}"

    parsed = parse_xml(xml_data, pmid)
    if parsed is None:
        return pmid, "skipped (not a journal article or retracted)"

    actual_pmid = parsed["pmid"]
    if actual_pmid not in refs:
        append_to_references(parsed)
    stem = write_paper_json(parsed)
    return stem, "written"


def main():
    parser = argparse.ArgumentParser(
        description="Fetch PubMed abstracts into papers/<stem>.json"
    )
    parser.add_argument("pmids", nargs="*", help="PMIDs to fetch")
    parser.add_argument(
        "--query", help="PubMed query; resolved to PMIDs via esearch"
    )
    parser.add_argument(
        "--retmax", type=int, default=40,
        help="Max PMIDs per query (default 40)"
    )
    args = parser.parse_args()

    pmids = list(args.pmids)
    if args.query:
        try:
            found = search_pmids_with_retmax(args.query, args.retmax)
        except Exception as e:
            print(f"esearch failed: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"esearch returned {len(found)} PMIDs for: {args.query}",
              file=sys.stderr)
        pmids.extend(found)

    if not pmids:
        parser.print_help()
        sys.exit(1)

    seen = set()
    deduped = []
    for p in pmids:
        if p and p not in seen:
            seen.add(p)
            deduped.append(p)

    fetched = 0
    for pmid in deduped:
        stem, status = process_pmid(pmid, fetched)
        print(f"{stem}: {status}")
        if status == "written":
            fetched += 1


if __name__ == "__main__":
    main()
