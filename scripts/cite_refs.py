#!/usr/bin/env python3
"""Convert in-text citation stems to readable form and update the References section.

Usage:
    python cite_refs.py <document.md>

Project resolution: cwd-based. Must be run from inside a projects/<name>/
subtree (errors out otherwise — the auto-add step requires a project).

Behavior:
  1. Scan the document for in-text stems and convert each to its in-text
     citation form ("LastName YYYY" / "LastName & LastName YYYY" /
     "LastName et al. YYYY").
  2. Detect the "References" section. Create one at the end if absent.
  3. Add a full citation for each cited stem to the References section.
  4. Sort all entries in the References section alphabetically (including
     any pre-existing entries), then renumber.
  5. For every cited PMID not already in projects/<name>/pmids.txt, append
     it to the project's pmids list.

Citation lookup source: papers/parsed/<stem>.json globally. A cited stem
may correspond to a paper in any project (or an orphan).
"""

import argparse
import json
import re
import sys
from pathlib import Path

from _project import (
    add_pmids_to_project,
    current_project_from_cwd,
    parsed_path,
)


STEM_RE = re.compile(r"\b([A-Z][a-zA-Z]*(?:_[A-Z][a-zA-Z]*)*_\d{4}_[\w]+_\d+)\b")
REFERENCES_HEADER_RE = re.compile(r"^##\s*References\s*$", re.MULTILINE)
NUMBERED_ENTRY_RE = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
PMID_IN_ENTRY_RE = re.compile(r"PMID:\s*(\d+)", re.IGNORECASE)


def derive_pmid(stem):
    return stem.rsplit("_", 1)[-1]


def lastname(author_field):
    """Parse 'LastName Initials' -> 'LastName'. Consortium names returned verbatim."""
    if " " not in author_field:
        return author_field.strip()
    return author_field.split(" ", 1)[0].strip()


def in_text_form(entry):
    authors = entry.get("authors") or []
    year = entry.get("year") or ""
    if not authors:
        return f"Anon {year}"
    last_names = [lastname(a["author"]) for a in authors if a.get("author")]
    if len(last_names) == 1:
        return f"{last_names[0]} {year}"
    if len(last_names) == 2:
        return f"{last_names[0]} & {last_names[1]} {year}"
    return f"{last_names[0]} et al. {year}"


def full_citation(entry):
    authors = entry.get("authors") or []
    author_str = ", ".join(a["author"] for a in authors if a.get("author"))
    title = (entry.get("title") or "").rstrip(".")
    journal = entry.get("journal") or ""
    year = entry.get("year") or ""
    volume = entry.get("volume") or ""
    issue = entry.get("issue") or ""
    pages = entry.get("pages") or ""
    parts = [author_str + "." if author_str else ""]
    if title:
        parts.append(f"{title}.")
    if journal:
        parts.append(f"{journal}.")
    loc = year
    if volume:
        loc += f";{volume}"
    if issue:
        loc += f"({issue})"
    if pages:
        loc += f":{pages}"
    if loc:
        parts.append(f"{loc}.")
    return " ".join(p for p in parts if p)


def _load_parsed(stem):
    path = parsed_path(stem)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _split_existing_section(text):
    r"""Return (body_before_references, existing_entries[]).

    Existing entries are extracted by splitting the post-"## References"
    body on numbered list markers (^\d+\.\s+). Trims trailing whitespace
    on each entry. Returns ([], []) if no References section found.
    """
    m = REFERENCES_HEADER_RE.search(text)
    if not m:
        return text.rstrip() + "\n", []
    body = text[:m.start()].rstrip() + "\n"
    after = text[m.end():]
    # Split on numbered markers
    pieces = re.split(r"\n\s*\d+\.\s+", "\n" + after)
    entries = [p.strip() for p in pieces[1:] if p.strip()]
    return body, entries


def _entries_sort_key(entry_text):
    """Sort by lowercase citation string (effectively first-author surname)."""
    return entry_text.lstrip().lower()


def _pmid_of(entry_text):
    m = PMID_IN_ENTRY_RE.search(entry_text)
    return m.group(1) if m else None


def convert(text):
    """Return (new_text, cited_pmids_in_appearance_order)."""
    seen_stems = []
    seen_set = set()
    missing = []

    def _replace(match):
        stem = match.group(1)
        pmid = derive_pmid(stem)
        entry = _load_parsed(stem)
        if entry is None:
            missing.append(stem)
            return match.group(0)
        if stem not in seen_set:
            seen_set.add(stem)
            seen_stems.append(stem)
        return in_text_form(entry)

    body_with_refs, existing_entries = _split_existing_section(text)
    new_body = STEM_RE.sub(_replace, body_with_refs)

    if missing:
        names = ", ".join(missing[:5])
        more = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
        print(f"warning: {len(missing)} stem(s) missing parsed/<stem>.json: {names}{more}",
              file=sys.stderr)

    new_entries_by_pmid = {}
    cited_pmids = []
    for stem in seen_stems:
        pmid = derive_pmid(stem)
        cited_pmids.append(pmid)
        entry = _load_parsed(stem)
        if entry is None:
            continue
        cite = f"{full_citation(entry)} PMID: {pmid}."
        new_entries_by_pmid[pmid] = cite

    # Build merged entries list. Existing entries with a PMID we already
    # have a freshly-built citation for are replaced (keeps formatting
    # consistent); other existing entries are preserved verbatim.
    merged = []
    seen_pmids = set()
    for ent in existing_entries:
        pmid = _pmid_of(ent)
        if pmid and pmid in new_entries_by_pmid:
            merged.append(new_entries_by_pmid[pmid])
            seen_pmids.add(pmid)
        else:
            merged.append(ent)
    for pmid, cite in new_entries_by_pmid.items():
        if pmid not in seen_pmids:
            merged.append(cite)
            seen_pmids.add(pmid)

    merged.sort(key=_entries_sort_key)

    refs_section = ["", "## References", ""]
    for i, ent in enumerate(merged, start=1):
        refs_section.append(f"{i}. {ent}")

    new_text = new_body.rstrip() + "\n" + "\n".join(refs_section) + "\n"
    return new_text, cited_pmids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="Document to convert (modified in place)")
    args = parser.parse_args()

    project = current_project_from_cwd()
    if not project:
        print("error: cite_refs.py must be run from inside a projects/<name>/ subtree.",
              file=sys.stderr)
        sys.exit(1)

    if not args.file.exists():
        print(f"file not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    text = args.file.read_text(encoding="utf-8")
    new_text, cited_pmids = convert(text)

    args.file.write_text(new_text, encoding="utf-8")

    added = add_pmids_to_project(project, cited_pmids)
    print(f"converted {len(set(cited_pmids))} unique citations in {args.file}")
    if added:
        print(f"added {len(added)} new PMID(s) to project {project!r}: {', '.join(added)}")


if __name__ == "__main__":
    main()
