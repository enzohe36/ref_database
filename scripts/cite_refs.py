#!/usr/bin/env python3
"""Convert in-text citation stems to readable form and update the References section.

Usage:
    python cite_refs.py <document.md>                # Author-Year mode (default)
    python cite_refs.py --numbered <document.md>     # Numeric mode

Runs from anywhere — no project context needed. Citation lookup is global:
papers/parsed/<stem>.json is read directly regardless of which project the
cited paper "belongs to".

Input citation format:
  An in-text citation is a literal stem (basename of papers/parsed/<stem>.json,
  e.g. `Cao_2024_Nature_38123456`). Stems may appear bare, in parens
  `(StemA; StemB)`, or in brackets `[StemA; StemB]`, with multiple stems
  separated by `; `. Stem regex allows compound surnames
  (`Robin_Jagerschmidt_…`, `Tecalco_Cruz_…`) via
  `[A-Z][a-z]+(?:_[A-Z][a-zA-Z]*)*_[0-9]{4}_…_[0-9]+`.

Default (Author-Year) mode:
  1. Replace each stem with its in-text form ("LastName YYYY" /
     "LastName & LastName YYYY" / "LastName et al. YYYY").
  2. Build or update the "References" section. Existing entries are parsed
     regardless of their format (numbered list, bulleted list, or plain
     paragraphs). New citations are merged with existing entries by PMID
     (re-rendering any whose PMID is now cited), sorted alphabetically,
     and emitted as plain paragraphs separated by blank lines. Pre-existing
     entries with no live citation are preserved verbatim.

--numbered mode:
  1. Replace each stem with its citation number in order of first appearance,
     preserving surrounding brackets and `; ` separators —
     `[StemA; StemB; StemC]` becomes `[1; 2; 3]`.
  2. Rebuild the "References" section as a numbered list in appearance order.
     Orphan entries from a pre-existing References section are dropped; a
     warning is emitted if a dropped entry's "Author YYYY" form is still
     present in the body (signal of a mixed-style citation worth fixing).
"""

import argparse
import json
import re
import sys
from pathlib import Path

from _project import parsed_path, pmid_to_stem


STEM_RE = re.compile(r"\b([A-Za-z][a-zA-Z]*(?:_[A-Za-z][a-zA-Z]*)*_\d{4}_[\w]+_\d+)\b")
REFERENCES_HEADER_RE = re.compile(r"^##\s*References\s*$", re.MULTILINE)
PMID_IN_ENTRY_RE = re.compile(r"PMID:\s*(\d+)", re.IGNORECASE)
NUMBERED_LEAD_RE = re.compile(r"^\s*\d+\.\s+")
BULLET_LEAD_RE = re.compile(r"^\s*-\s+")


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
    """Return (body_before_references, existing_entries[]).

    Handles three input formats for the existing References section so the
    script can round-trip its own output regardless of which mode produced it:
      - Numbered list (`1. ...`, `2. ...`)
      - Bulleted list (`- ...`)
      - Plain paragraphs separated by blank lines
    Leading markers are stripped from each returned entry. Returns
    (text, []) if no References section is found.
    """
    m = REFERENCES_HEADER_RE.search(text)
    if not m:
        return text.rstrip() + "\n", []
    body = text[:m.start()].rstrip() + "\n"
    after = text[m.end():].strip("\n")
    if not after:
        return body, []

    entries = []
    for block in re.split(r"\n\s*\n", after):
        block = block.strip("\n")
        if not block.strip():
            continue
        first_line = block.lstrip().splitlines()[0]
        if NUMBERED_LEAD_RE.match(first_line):
            pieces = re.split(r"(?m)^\s*\d+\.\s+", block)
        elif BULLET_LEAD_RE.match(first_line):
            pieces = re.split(r"(?m)^\s*-\s+", block)
        else:
            pieces = [block]
        for p in pieces:
            p = p.strip()
            if p:
                entries.append(p)
    return body, entries


def _entries_sort_key(entry_text):
    """Sort by lowercase citation string (effectively first-author surname)."""
    return entry_text.lstrip().lower()


def _pmid_of(entry_text):
    m = PMID_IN_ENTRY_RE.search(entry_text)
    return m.group(1) if m else None


def _author_year_form_from_entry(entry_text):
    """Best-effort 'Author YYYY' form for a free-text References entry.

    Extract the PMID, resolve it to a stem, load parsed/<stem>.json, and
    rebuild the in-text form. Returns None if any step fails.
    """
    pmid = _pmid_of(entry_text)
    if not pmid:
        return None
    stem = pmid_to_stem(pmid)
    if not stem:
        return None
    entry = _load_parsed(stem)
    if entry is None:
        return None
    return in_text_form(entry)


def _warn_missing(missing):
    if not missing:
        return
    names = ", ".join(missing[:5])
    more = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
    print(f"warning: {len(missing)} stem(s) missing parsed/<stem>.json: {names}{more}",
          file=sys.stderr)


def _assemble(new_body, refs_body):
    """Glue the converted body and the rendered References section together."""
    if refs_body:
        return new_body.rstrip() + "\n\n## References\n\n" + refs_body + "\n"
    return new_body.rstrip() + "\n\n## References\n"


def _convert_author_year(text):
    """Default mode: stems -> 'Author YYYY'; refs as alphabetical paragraphs."""
    body_with_refs, existing_entries = _split_existing_section(text)

    seen_stems = []
    seen_set = set()
    missing = []

    def _replace(match):
        stem = match.group(1)
        entry = _load_parsed(stem)
        if entry is None:
            missing.append(stem)
            return match.group(0)
        if stem not in seen_set:
            seen_set.add(stem)
            seen_stems.append(stem)
        return in_text_form(entry)

    new_body = STEM_RE.sub(_replace, body_with_refs)
    _warn_missing(missing)

    new_entries_by_pmid = {}
    cited_pmids = []
    for stem in seen_stems:
        pmid = derive_pmid(stem)
        cited_pmids.append(pmid)
        entry = _load_parsed(stem)
        if entry is None:
            continue
        new_entries_by_pmid[pmid] = f"{full_citation(entry)} PMID: {pmid}."

    # Existing entry with a now-cited PMID is replaced (keeps formatting
    # consistent); other existing entries are preserved verbatim. New
    # entries with no matching existing entry are appended.
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

    refs_body = "\n\n".join(merged)
    return _assemble(new_body, refs_body), cited_pmids


def _convert_numbered(text):
    """--numbered mode: stems -> integer labels; refs as appearance-ordered numbered list."""
    body_with_refs, existing_entries = _split_existing_section(text)

    ordered_stems = []
    seen_set = set()
    missing = []
    for m in STEM_RE.finditer(body_with_refs):
        stem = m.group(1)
        if stem in seen_set:
            continue
        seen_set.add(stem)
        if _load_parsed(stem) is None:
            missing.append(stem)
            continue
        ordered_stems.append(stem)

    _warn_missing(missing)

    stem_to_num = {stem: i + 1 for i, stem in enumerate(ordered_stems)}

    def _replace(match):
        stem = match.group(1)
        n = stem_to_num.get(stem)
        return str(n) if n is not None else match.group(0)

    new_body = STEM_RE.sub(_replace, body_with_refs)

    cited_pmids_set = {derive_pmid(s) for s in ordered_stems}
    for ent in existing_entries:
        pmid = _pmid_of(ent)
        if pmid and pmid in cited_pmids_set:
            continue
        ay = _author_year_form_from_entry(ent)
        if ay and ay in new_body:
            short = ent[:60] + ("..." if len(ent) > 60 else "")
            print(f'warning: dropping existing References entry "{short}" but its '
                  f'"{ay}" form still appears in the body — fix to a stem citation',
                  file=sys.stderr)

    refs_lines = []
    for i, stem in enumerate(ordered_stems, start=1):
        entry = _load_parsed(stem)
        pmid = derive_pmid(stem)
        refs_lines.append(f"{i}. {full_citation(entry)} PMID: {pmid}.")

    refs_body = "\n".join(refs_lines)
    return _assemble(new_body, refs_body), [derive_pmid(s) for s in ordered_stems]


def convert(text, numbered=False):
    """Return (new_text, cited_pmids_in_appearance_order)."""
    if numbered:
        return _convert_numbered(text)
    return _convert_author_year(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="Document to convert (modified in place)")
    parser.add_argument(
        "--numbered", action="store_true",
        help="Replace stems with citation numbers in first-appearance order "
             "and emit References as a numbered list.",
    )
    args = parser.parse_args()

    if not args.file.exists():
        print(f"file not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    text = args.file.read_text(encoding="utf-8")
    new_text, cited_pmids = convert(text, numbered=args.numbered)

    args.file.write_text(new_text, encoding="utf-8")

    print(f"converted {len(set(cited_pmids))} unique citations in {args.file}")


if __name__ == "__main__":
    main()
