#!/usr/bin/env python3
"""Convert in-text citation stems in a markdown file to "Author et al. YYYY" form
and append a numbered References section. Modifies the file in place.

Stem format: LastName_YYYY_Journal_PMID  (PMID is the last underscore segment)
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
REFS_FILE = REPO_ROOT / "refs.json"

STEM_RE = re.compile(r"\b([A-Z][a-zA-Z]*(?:_[A-Z][a-zA-Z]*)*_\d{4}_[\w]+_\d+)\b")
REFERENCES_HEADER_RE = re.compile(r"^##\s*References\s*$", re.MULTILINE)


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
    pmid = ""
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


def derive_pmid(stem):
    return stem.rsplit("_", 1)[-1]


def convert(text, refs):
    """Return (new_text, ordered_stems_first_appearance)."""
    seen = []
    seen_set = set()

    def replace(match):
        stem = match.group(1)
        pmid = derive_pmid(stem)
        if pmid not in refs:
            raise KeyError(f"stem references missing PMID {pmid} (stem={stem})")
        if stem not in seen_set:
            seen.append(stem)
            seen_set.add(stem)
        return in_text_form(refs[pmid])

    # Strip an existing References section (we regenerate).
    body_split = REFERENCES_HEADER_RE.split(text, maxsplit=1)
    body = body_split[0].rstrip() + "\n"

    new_body = STEM_RE.sub(replace, body)

    refs_section_lines = ["", "## References", ""]
    for i, stem in enumerate(seen, start=1):
        pmid = derive_pmid(stem)
        entry = refs[pmid]
        cite = full_citation(entry)
        refs_section_lines.append(f"{i}. {cite} PMID: {pmid}.")

    new_text = new_body.rstrip() + "\n" + "\n".join(refs_section_lines) + "\n"
    return new_text, seen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="Markdown file to convert (modified in place)")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"file not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    if not REFS_FILE.exists():
        print(f"refs.json not found: {REFS_FILE}", file=sys.stderr)
        sys.exit(1)

    refs = json.loads(REFS_FILE.read_text())
    text = args.file.read_text()

    try:
        new_text, seen = convert(text, refs)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    args.file.write_text(new_text)
    print(f"converted {len(seen)} unique citation stems in {args.file}")


if __name__ == "__main__":
    main()
