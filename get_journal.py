#!/usr/bin/env python3
"""Download NLM journal list and format as journals.json.

Usage:
    python get_journal.py

Downloads J_Entrez.txt from NCBI FTP, parses journal entries,
and writes journals.json keyed by NlmId with JournalTitle and MedAbbr.
"""

import json
import os
import re
import urllib.request

URL = "https://ftp.ncbi.nih.gov/pubmed/J_Entrez.txt"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journals.json")


def main():
    print(f"Downloading {URL}...", flush=True)
    with urllib.request.urlopen(URL) as resp:
        data = resp.read().decode("utf-8")

    entries = []
    current = {}
    for line in data.split('\n'):
        line = line.strip()
        if line.startswith('---'):
            if current:
                entries.append(current)
            current = {}
        elif ': ' in line:
            key, val = line.split(': ', 1)
            current[key] = val.strip()
    if current:
        entries.append(current)

    journal_map = {}
    for e in entries:
        nlmid = e.get('NlmId', '').strip()
        abbr = e.get('MedAbbr', '').strip()
        title = e.get('JournalTitle', '').strip()
        if nlmid and abbr:
            journal_map[nlmid] = {
                "JournalTitle": title,
                "MedAbbr": abbr,
            }

    raw = json.dumps(journal_map, indent=2, ensure_ascii=False)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(raw)
        f.write("\n")

    print(f"Written {len(journal_map)} journals to {OUTPUT}")


if __name__ == "__main__":
    main()
