#!/usr/bin/env python3
"""Split a draft markdown file into per-paragraph sentence JSON for fact-check agents.

Usage:
    python split_sentences.py <draft.md>

Output: <project>/factcheck/inputs/NN_<section_id>_p<para_idx>.json (one per paragraph).

The draft path must be inside a projects/<name>/ subtree. The factcheck/ dir is
created next to pmids.txt at projects/<name>/factcheck/.

Behavior:
  - Walks heading lines (numeric prefix like `1.`, `2.1`, `2.3.1`, or `## ...`).
  - Skips: title (line 1), abstract paragraph (between `Abstract` heading and the
    first numbered section), and any `## References` block.
  - The conclusion (section starting with the highest-numbered top-level header,
    e.g. `5. Conclusion...` or `## Conclusion...`) is included as wave 2 input.
  - For each paragraph: strip inline citation stems, sentence-split at . ; : ! ?
    while masking decimals, abbreviations, DOIs, mutation notations, and unit-after-
    decimal patterns.
"""
import argparse
import json
import re
import sys
from pathlib import Path

STEM = r"[A-Z][a-zA-Z]*(?:_[A-Z][a-zA-Z]*)*_\d{4}_[\w]+_\d+"
HEADING_RE = re.compile(r"^(?:##\s+)?(\d+(?:\.\d+)*)\.?\s+(.+)$")
ABBREVS = [
    "e.g.", "i.e.", "cf.", "etc.", "approx.", "et al.", "vs.",
    "Fig.", "Eq.", "Dr.", "Mr.", "Mrs.", "Ms.", "Prof.",
    "No.", "St.", "Mt.",
]


def strip_citations(text):
    """Remove inline citation stems and clean up surrounding decoration."""
    text = re.sub(rf"\s*\(\s*{STEM}(?:\s*;\s*{STEM})*\s*\)", "", text)
    text = re.sub(rf";\s*{STEM}", "", text)
    text = re.sub(rf"{STEM}\s*;\s*", "", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def split_sentences(paragraph):
    placeholders = []

    def stash(s):
        idx = len(placeholders)
        placeholders.append(s)
        return f"\x00{idx}\x00"

    masked = paragraph
    masked = re.sub(r"\d+\.\d+(?:\.\d+)*", lambda m: stash(m.group(0)), masked)
    masked = re.sub(r"10\.\d+/\S+", lambda m: stash(m.group(0)), masked)
    for abbr in sorted(ABBREVS, key=len, reverse=True):
        masked = re.sub(re.escape(abbr), lambda m: stash(m.group(0)),
                        masked, flags=re.IGNORECASE)

    parts = re.split(r"([.;:!?])(?=\s|$)", masked)
    sentences = []
    cur = ""
    for i, p in enumerate(parts):
        if i % 2 == 0:
            cur = p
        else:
            full = (cur + p).strip()
            if full:
                sentences.append(full)
            cur = ""
    if cur.strip():
        sentences.append(cur.strip())

    def restore(s):
        return re.sub(r"\x00(\d+)\x00",
                      lambda m: placeholders[int(m.group(1))], s)

    return [restore(s) for s in sentences if s.strip()]


def parse_draft(text):
    """Return (title, [(section_id, header, [paragraph_text, ...]), ...])."""
    lines = text.splitlines()
    title = lines[0].strip() if lines else ""

    sections = []
    cur_section = None
    cur_paragraphs = []
    in_abstract = False
    in_body = False

    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.lower() in ("abstract", "## abstract"):
            in_abstract = True
            in_body = False
            continue

        if stripped.lower() in ("## references", "references"):
            break

        m = HEADING_RE.match(stripped)
        if m:
            if cur_section is not None:
                sections.append((*cur_section, cur_paragraphs))
            cur_section = (m.group(1), stripped)
            cur_paragraphs = []
            in_abstract = False
            in_body = True
            continue

        if in_abstract:
            continue
        if in_body and cur_section is not None:
            cur_paragraphs.append(stripped)

    if cur_section is not None:
        sections.append((*cur_section, cur_paragraphs))

    return title, sections


def find_factcheck_dir(draft_path):
    """Locate projects/<name>/ ancestor of the draft and return its factcheck/ dir.

    Errors out if the draft is not inside a projects/<name>/ subtree.
    """
    p = draft_path.resolve()
    for ancestor in p.parents:
        if ancestor.parent.name == "projects":
            return ancestor / "factcheck"
    raise SystemExit(
        f"draft {draft_path} is not inside a projects/<name>/ subtree; "
        f"the fact-check workflow is project-scoped."
    )


def conclusion_section_id(sections):
    """Return the section_id whose body counts as the conclusion (highest top-level
    integer with non-empty paragraphs). Empty if none."""
    body_top_levels = [
        int(sec_id.split(".")[0])
        for sec_id, _hdr, paras in sections
        if paras
    ]
    if not body_top_levels:
        return None
    return str(max(body_top_levels))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("draft", type=Path)
    args = ap.parse_args()

    if not args.draft.exists():
        print(f"draft not found: {args.draft}", file=sys.stderr)
        sys.exit(1)

    fc_dir = find_factcheck_dir(args.draft)
    inputs_dir = fc_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    for p in inputs_dir.glob("*.json"):
        p.unlink()

    title, sections = parse_draft(args.draft.read_text())
    conclusion_id = conclusion_section_id(sections)

    global_idx = 0
    written = 0
    for section_id, header, paragraphs in sections:
        if not paragraphs:
            continue
        wave = 2 if section_id == conclusion_id else 1
        for para_idx, para_text in enumerate(paragraphs):
            global_idx += 1
            cleaned = strip_citations(para_text)
            sentences = split_sentences(cleaned)
            payload = {
                "global_idx": global_idx,
                "section_id": section_id,
                "header": header,
                "para_idx": para_idx,
                "wave": wave,
                "paragraph_text": cleaned,
                "sentences": [
                    {"sent_idx": i, "text": s} for i, s in enumerate(sentences)
                ],
            }
            sec_safe = section_id.replace(".", "_")
            fname = f"{global_idx:02d}_{sec_safe}_p{para_idx}.json"
            (inputs_dir / fname).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            )
            written += 1

    wave1 = sum(1 for s in sections if s[0] != conclusion_id and s[2])
    wave1_paras = sum(len(s[2]) for s in sections if s[0] != conclusion_id)
    wave2_paras = sum(len(s[2]) for s in sections if s[0] == conclusion_id)
    print(f"title: {title}")
    print(f"wrote {written} input files to {inputs_dir}")
    print(f"  wave 1 subsections: {wave1}, paragraphs: {wave1_paras}")
    print(f"  wave 2 paragraphs (conclusion section {conclusion_id!r}): {wave2_paras}")


if __name__ == "__main__":
    main()
