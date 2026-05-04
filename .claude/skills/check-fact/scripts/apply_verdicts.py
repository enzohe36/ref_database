#!/usr/bin/env python3
"""Apply fact-check verdicts to a draft and write <draft_stem>.cited.md.

Usage:
    python apply_verdicts.py <draft.md> [--decisions decisions.json] [--auto-accept]

The draft must be inside projects/<name>/. State (verdicts/, decisions.json) is
read from projects/<name>/factcheck/. Output is written next to the draft as
<draft_stem>.cited.md.

Pipeline:
  1. Re-split the draft using split_sentences.py logic.
  2. For each sentence, look up its verdict from factcheck/verdicts/*.json.
  3. Apply override from factcheck/decisions.json (if present); otherwise the
     original sentence text. With --auto-accept, error verdicts whose
     suggested_rewrite is set are auto-applied.
  4. Scrub inline author-year refs and stem refs from each sentence so the
     citations from verdict.citations don't double-cite.
  5. Consolidate consecutive identical citation sets within each paragraph
     (drop from N..N+k-1, keep on N+k).
  6. Insert each remaining citation set as `(Stem1; Stem2)` immediately
     before the sentence's terminating punctuation.
  7. Reassemble paragraphs (single space joiner) and the document.
     Title, abstract, and bare section-only headers pass through verbatim.

decisions.json schema (all keys are strings):
{
  "<global_idx>": {
    "<sent_idx>": "rewritten sentence text"
                  | {"text": "...", "citations": ["Stem1", "Stem2"]}
  },
  ...
}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from split_sentences import (  # noqa: E402
    parse_draft,
    strip_citations,
    split_sentences,
    find_factcheck_dir,
    conclusion_section_id,
)
from consolidate_citations import consolidate  # noqa: E402

TERMINATOR_RE = re.compile(r"([.;:!?])\s*$")
AUTHOR_YEAR = (
    r"[A-Z][a-zA-Z]+(?:\s+et\s+al\.?|\s+&\s+[A-Z][a-zA-Z]+)?\s+\d{4}[a-z]?"
)
STEM_TOKEN = r"[A-Z][a-zA-Z]*(?:_[A-Z][a-zA-Z]*)*_\d{4}_[\w]+_\d+"
INLINE_CITE_RE = re.compile(
    rf"\s*\(\s*(?:{AUTHOR_YEAR}|{STEM_TOKEN})(?:\s*[;,]\s*(?:{AUTHOR_YEAR}|{STEM_TOKEN}))*\s*\)"
)


def scrub_inline_cites(sentence: str) -> str:
    return INLINE_CITE_RE.sub("", sentence)


def insert_citation(sentence: str, citations: list[str]) -> str:
    """Insert (Stem1; Stem2) immediately before the terminating punctuation."""
    sentence = scrub_inline_cites(sentence)
    if not citations:
        return sentence
    cite = "(" + "; ".join(citations) + ")"
    m = TERMINATOR_RE.search(sentence)
    if m:
        return sentence[: m.start()] + " " + cite + sentence[m.start():]
    return sentence + " " + cite


def parse_with_abstract(text: str):
    """Return (title, abstract_lines, sections). Abstract is the raw lines between
    the 'Abstract' heading and the first numbered section."""
    from split_sentences import HEADING_RE  # local import to avoid cycle

    lines = text.splitlines()
    title = lines[0].strip() if lines else ""

    abstract_lines: list[str] = []
    sections: list[tuple[str, str, list[str]]] = []
    cur_section = None
    cur_paragraphs: list[str] = []
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
            abstract_lines.append(stripped)
            continue
        if in_body and cur_section is not None:
            cur_paragraphs.append(stripped)
    if cur_section is not None:
        sections.append((*cur_section, cur_paragraphs))
    return title, abstract_lines, sections


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("draft", type=Path)
    ap.add_argument("--decisions", type=Path, default=None)
    ap.add_argument(
        "--auto-accept",
        action="store_true",
        help="Apply agent suggested_rewrite to any error verdict without an explicit decision.",
    )
    args = ap.parse_args()

    if not args.draft.exists():
        print(f"draft not found: {args.draft}", file=sys.stderr)
        sys.exit(1)

    fc_dir = find_factcheck_dir(args.draft)
    verdicts_dir = fc_dir / "verdicts"
    decisions_file = args.decisions or (fc_dir / "decisions.json")

    decisions = {}
    if decisions_file.exists():
        decisions = json.loads(decisions_file.read_text())
        print(f"loaded {sum(len(v) for v in decisions.values())} sentence decisions "
              f"across {len(decisions)} paragraphs from {decisions_file}")

    verdicts_by_idx: dict[int, dict] = {}
    for vp in sorted(verdicts_dir.glob("*.json")):
        data = json.loads(vp.read_text())
        verdicts_by_idx[data["global_idx"]] = data
    print(f"loaded {len(verdicts_by_idx)} verdict files")

    title, abstract_lines, sections = parse_with_abstract(args.draft.read_text())

    cited: dict[int, str] = {}
    global_idx = 0
    for section_id, header, paragraphs in sections:
        if not paragraphs:
            continue
        for para_idx, para in enumerate(paragraphs):
            global_idx += 1
            verdict_data = verdicts_by_idx.get(global_idx)
            if verdict_data is None:
                cited[global_idx] = para
                continue

            cleaned = strip_citations(para)
            sentence_texts = split_sentences(cleaned)
            decision_for_para = decisions.get(str(global_idx), {})

            sentence_records = []
            for sent_idx, sentence in enumerate(sentence_texts):
                v = (
                    verdict_data["verdicts"][sent_idx]
                    if sent_idx < len(verdict_data["verdicts"])
                    else None
                )
                citations = list(v.get("citations", [])) if v else []
                decision = decision_for_para.get(str(sent_idx))
                if isinstance(decision, dict):
                    final = decision.get("text", sentence)
                    if "citations" in decision:
                        citations = list(decision["citations"])
                elif isinstance(decision, str):
                    final = decision
                elif (
                    args.auto_accept
                    and v is not None
                    and v.get("verdict") == "error"
                    and v.get("suggested_rewrite")
                ):
                    final = v["suggested_rewrite"]
                else:
                    final = sentence
                sentence_records.append({"text": final, "citations": citations})

            consolidate(sentence_records)
            cited_sentences = [
                insert_citation(r["text"], r["citations"])
                for r in sentence_records
            ]
            cited[global_idx] = " ".join(cited_sentences)

    parts: list[str] = [title, "", "Abstract", ""]
    parts.extend(abstract_lines)
    parts.append("")

    global_idx = 0
    for section_id, header, paragraphs in sections:
        parts.append(header)
        parts.append("")
        if not paragraphs:
            continue
        for _ in range(len(paragraphs)):
            global_idx += 1
            parts.append(cited[global_idx])
            parts.append("")

    out_path = args.draft.with_name(args.draft.stem.split(".", 1)[0] + ".cited.md")
    out_path.write_text("\n".join(parts).rstrip() + "\n")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
