#!/usr/bin/env python3
"""Merge [stem].json metadata into refs.json.

Usage:
    python merge_refs.py [stem].json [[stem].json ...]

For each [stem].json:
- Replaces the "authors" array in refs.json with the one from [stem].json.
- Searches PubMed for each reference string in [stem].json to retrieve its
  PMID, then replaces the "references" array in refs.json with the collected
  PMIDs.
"""

import os
import sys
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from cite import load_references, save_references


def search_pmid(query):
    """Search PubMed for a reference string and return the top PMID, or None."""
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={urllib.parse.quote(query)}&retmax=1&retmode=xml"
    )
    with urllib.request.urlopen(url) as resp:
        xml_data = resp.read().decode("utf-8")
    root = ET.fromstring(xml_data)
    ids = root.findall(".//IdList/Id")
    return ids[0].text if ids else None


def pmid_from_stem(filepath):
    """Extract PMID from [stem].json filename (last token before .json)."""
    return os.path.basename(filepath).rsplit(".", 1)[0].split()[-1]


def main():
    if len(sys.argv) < 2:
        print("Usage: python merge_refs.py [stem].json [[stem].json ...]",
              file=sys.stderr)
        sys.exit(1)

    refs = load_references()
    fetch_count = 0

    for filepath in sys.argv[1:]:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        pmid = pmid_from_stem(filepath)

        if pmid not in refs:
            print(json.dumps({"pmid": pmid, "status": "error",
                              "message": "PMID not found in refs.json"}))
            continue

        # Merge authors
        authors = data.get("authors", [])
        if authors:
            refs[pmid]["authors"] = authors
            print(json.dumps({"pmid": pmid, "authors": len(authors)}))

        # Resolve references
        references = data.get("references", [])
        if not references:
            print(json.dumps({"pmid": pmid, "references": 0}))
            continue

        pmids = []
        for ref_string in references:
            if fetch_count > 0:
                time.sleep(0.4)
            try:
                result = search_pmid(ref_string)
                fetch_count += 1
                if result:
                    pmids.append(result)
                print(json.dumps({"pmid": pmid, "reference": ref_string[:80],
                                  "found": result}))
            except Exception as e:
                print(json.dumps({"pmid": pmid, "reference": ref_string[:80],
                                  "error": str(e)}))

        refs[pmid]["references"] = pmids

    save_references(refs)


if __name__ == "__main__":
    main()
