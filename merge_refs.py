#!/usr/bin/env python3
"""Merge papers/*.json metadata into refs.json.

Usage:
    python merge_refs.py
    python merge_refs.py --patch

Scans refs.json for all entries with empty affiliations or references.
For each, loads papers/<stem>.json and fills in missing values:
- Affiliations: matched by "author" name between refs.json and papers/*.json.
- References: structured citation objects from papers/*.json are searched
  on PubMed for PMIDs. Resolved PMIDs are written back to papers/*.json
  (as single-key dicts keyed by PMID) and copied to refs.json.
  Unresolved references are saved to refs_no_pmid.json.

--patch copies manually resolved PMIDs from refs_no_pmid.json into
papers/*.json and refs.json, then removes them from refs_no_pmid.json.

Only fills missing values; does not overwrite existing affiliations or references.
"""

import json
import os
import re
import sys

from get_refs import load_references, save_references, make_stem
from parse_citation import (
    parse_citation,
    search_with_retry,
    search_structured_ref,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPERS_DIR = os.path.join(BASE_DIR, "papers")
NO_PMID_FILE = os.path.join(BASE_DIR, "refs_no_pmid.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def stem_for_entry(pmid, entry):
    """Construct stem from refs.json entry."""
    # Use stored stem if available
    if entry.get("stem"):
        return entry["stem"]
    # Fallback: reconstruct from fields
    authors = entry.get("authors", [])
    first_last = authors[0]["author"].split()[0] if authors else ""
    year = entry.get("year", "")
    journal = entry.get("journal", "")
    return make_stem(first_last, year, journal, pmid)


def has_empty_affiliations(entry):
    for author in entry.get("authors", []):
        if not author.get("affiliation"):
            return True
    return False


def has_empty_references(entry):
    return not entry.get("references")


# ---------------------------------------------------------------------------
# refs_no_pmid.json I/O
# ---------------------------------------------------------------------------

def load_no_pmid():
    if not os.path.exists(NO_PMID_FILE):
        return {}
    with open(NO_PMID_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_no_pmid(data):
    raw = json.dumps(data, indent=2, ensure_ascii=False)
    with open(NO_PMID_FILE, "w", encoding="utf-8") as f:
        f.write(raw)
        f.write("\n")


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------

def _resolve_structured_refs(paper_refs):
    """Resolve structured reference objects to PMIDs.

    Input: list of single-key dicts [{"": {...}}, {"": {...}}, ...]
    Returns: (resolved_refs, unresolved_refs)
      resolved_refs: [{"12345678": {...}}, ...]
      unresolved_refs: [{"": {...}}, ...]
    """
    resolved = []
    unresolved = []

    for ref_dict in paper_refs:
        # Each is a single-key dict
        key = list(ref_dict.keys())[0]
        ref_data = ref_dict[key]

        # Already resolved
        if key and key != "":
            resolved.append(ref_dict)
            continue

        # Try to resolve
        result_pmid = None
        try:
            result_pmid, _, _ = search_structured_ref(ref_data)
        except Exception:
            pass

        if result_pmid:
            resolved.append({result_pmid: ref_data})
            print(
                json.dumps(
                    {
                        "reference": ref_data.get("title", "")[:80],
                        "found": result_pmid,
                    }
                )
            )
        else:
            unresolved.append({"": ref_data})

    return resolved, unresolved


def _resolve_string_refs(citations, pmid):
    """Resolve raw citation strings (from PDF/agent) to PMIDs.

    Input: list of plain citation strings
    Returns: (resolved_pmids, unresolved_strings)
    """
    no_pmid = load_no_pmid()
    already_unresolved = set(no_pmid.get(pmid, {}).get("references", []))

    resolved_pmids = []
    unresolved = list(no_pmid.get(pmid, {}).get("references", []))

    for citation_string in citations:
        if citation_string in already_unresolved:
            continue

        groups = parse_citation(citation_string)
        if groups is None:
            unresolved.append(citation_string)
            continue

        result = None
        for attempt in range(2):
            try:
                result_pmid, _, _ = search_with_retry(groups, citation_string)
                if result_pmid:
                    result = result_pmid
                break
            except Exception:
                if attempt == 0:
                    continue

        if result:
            resolved_pmids.append(result)
            print(
                json.dumps(
                    {
                        "pmid": pmid,
                        "reference": citation_string[:80],
                        "found": result,
                    }
                )
            )
        else:
            unresolved.append(citation_string)

    return resolved_pmids, unresolved


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------

def validate_no_pmid():
    """Copy manually resolved PMIDs from refs_no_pmid.json into papers/*.json
    and refs.json, then remove them from refs_no_pmid.json."""
    no_pmid = load_no_pmid()
    if not no_pmid:
        print("refs_no_pmid.json is empty or not found.", file=sys.stderr)
        return

    refs = load_references()
    total_moved = 0

    for pmid, obj in list(no_pmid.items()):
        remaining = []
        resolved_pmids = []

        for ref_dict in obj.get("references", []):
            if isinstance(ref_dict, dict):
                key = list(ref_dict.keys())[0]
                if key and key != "":
                    resolved_pmids.append(key)
                else:
                    remaining.append(ref_dict)
            elif isinstance(ref_dict, str):
                # Legacy string format
                if re.fullmatch(r"\d+", ref_dict.strip()):
                    resolved_pmids.append(ref_dict.strip())
                else:
                    remaining.append(ref_dict)

        if resolved_pmids and pmid in refs:
            existing = refs[pmid].get("references", [])
            added = []
            for r in resolved_pmids:
                if r not in existing:
                    existing.append(r)
                    added.append(r)
            refs[pmid]["references"] = existing
            total_moved += len(added)
            if added:
                print(json.dumps({"pmid": pmid, "moved": added}))

            # Also update papers/<stem>.json if it exists
            stem = stem_for_entry(pmid, refs[pmid])
            paper_path = os.path.join(PAPERS_DIR, f"{stem}.json")
            if os.path.exists(paper_path):
                with open(paper_path, encoding="utf-8") as f:
                    paper_data = json.load(f)
                paper_refs = paper_data.get("references", [])
                # Update resolved refs in paper data
                new_paper_refs = []
                for pr in paper_refs:
                    if isinstance(pr, dict):
                        pk = list(pr.keys())[0]
                        if pk == "":
                            # Check if this was resolved
                            ref_data = pr[pk]
                            matched = False
                            for rp in resolved_pmids:
                                # Simple match: just move it
                                if not matched:
                                    new_paper_refs.append({rp: ref_data})
                                    matched = True
                            if not matched:
                                new_paper_refs.append(pr)
                        else:
                            new_paper_refs.append(pr)
                    else:
                        new_paper_refs.append(pr)
                paper_data["references"] = new_paper_refs
                with open(paper_path, "w", encoding="utf-8") as f:
                    json.dump(paper_data, f, indent=2, ensure_ascii=False)
                    f.write("\n")

        if remaining:
            obj["references"] = remaining
        else:
            del no_pmid[pmid]

    save_references(refs)
    save_no_pmid(no_pmid)
    print(f"Moved {total_moved} PMIDs to refs.json.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    refs = load_references()

    if len(sys.argv) >= 2 and sys.argv[1] == "--patch":
        validate_no_pmid()
        return

    for pmid, entry in refs.items():
        needs_aff = has_empty_affiliations(entry)
        needs_ref = has_empty_references(entry)

        if not needs_aff and not needs_ref:
            continue

        stem = stem_for_entry(pmid, entry)
        filepath = os.path.join(PAPERS_DIR, f"{stem}.json")

        if not os.path.exists(filepath):
            if needs_aff or needs_ref:
                print(
                    json.dumps({"pmid": pmid, "error": f"file not found: {filepath}"})
                )
            continue

        with open(filepath, encoding="utf-8") as f:
            paper_data = json.load(f)

        # Fill empty affiliations
        if needs_aff:
            paper_authors = paper_data.get("authors", [])
            if paper_authors:
                # Build lookup: paper authors may have "author" key or "name" key
                paper_map = {}
                for a in paper_authors:
                    name = a.get("author", a.get("name", ""))
                    aff = a.get("affiliation", a.get("affiliations", []))
                    if name and aff:
                        paper_map[name] = aff

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
                missing = [
                    a["author"]
                    for a in entry["authors"]
                    if not a.get("affiliation")
                ]
                print(
                    json.dumps(
                        {
                            "pmid": pmid,
                            "affiliations_filled": 0,
                            "affiliations_missing": missing,
                        }
                    )
                )

        # Fill empty references
        if needs_ref:
            paper_refs = paper_data.get("references", [])
            if not paper_refs:
                print(
                    json.dumps(
                        {
                            "pmid": pmid,
                            "references_filled": 0,
                            "references_missing": "no references in source",
                        }
                    )
                )
                continue

            # Detect format: structured (list of dicts) vs raw strings
            if isinstance(paper_refs[0], dict):
                # Structured references from HTML
                resolved_refs, unresolved_refs = _resolve_structured_refs(paper_refs)

                # Update papers/<stem>.json
                paper_data["references"] = resolved_refs + unresolved_refs
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(paper_data, f, indent=2, ensure_ascii=False)
                    f.write("\n")

                # Copy resolved PMIDs to refs.json
                resolved_pmids = [
                    list(r.keys())[0]
                    for r in resolved_refs
                    if list(r.keys())[0]
                ]
                if resolved_pmids:
                    entry["references"] = resolved_pmids

                print(
                    json.dumps(
                        {
                            "pmid": pmid,
                            "references_resolved": len(resolved_pmids),
                            "references_total": len(paper_refs),
                        }
                    )
                )

                # Record unresolved in refs_no_pmid.json
                if unresolved_refs:
                    no_pmid = load_no_pmid()
                    no_pmid[pmid] = {"references": unresolved_refs}
                    save_no_pmid(no_pmid)

            elif isinstance(paper_refs[0], str):
                # Raw citation strings from PDF/agent
                resolved_pmids, unresolved = _resolve_string_refs(paper_refs, pmid)

                if resolved_pmids:
                    entry["references"] = resolved_pmids
                print(
                    json.dumps(
                        {
                            "pmid": pmid,
                            "references_resolved": len(resolved_pmids),
                            "references_total": len(paper_refs),
                        }
                    )
                )

                no_pmid = load_no_pmid()
                if unresolved:
                    no_pmid[pmid] = {"references": unresolved}
                elif pmid in no_pmid:
                    del no_pmid[pmid]
                save_no_pmid(no_pmid)

    save_references(refs)


if __name__ == "__main__":
    main()
