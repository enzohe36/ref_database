#!/usr/bin/env python3
"""Build factcheck/papers_full_text.json: papers with main_text exceeding a word threshold.

Usage:
    python build_eligible_papers.py <project_name> [--threshold N]

Reads papers/parsed/*.json globally and emits projects/<name>/factcheck/papers_full_text.json
keyed by PMID with {stem, word_count} for stems exceeding the threshold (default 5000 words).
This list is the citation eligibility filter handed to fact-check agents — they may only
cite stems whose PMID is in this dict.
"""
import argparse
import json
import sys
from pathlib import Path

# Add scripts/ to path so we can import _project.
SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from _project import iter_parsed, projects_dir  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project")
    ap.add_argument("--threshold", type=int, default=5000)
    args = ap.parse_args()

    fc_dir = projects_dir() / args.project / "factcheck"
    fc_dir.mkdir(parents=True, exist_ok=True)
    out = fc_dir / "papers_full_text.json"

    eligible = {}
    total = 0
    no_text = 0
    short_text = 0
    parse_err = 0
    for path in iter_parsed():
        total += 1
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            parse_err += 1
            print(f"  skip {path.name}: {e}", file=sys.stderr)
            continue
        main_text = data.get("main_text") or ""
        if not main_text:
            no_text += 1
            continue
        wc = len(main_text.split())
        if wc <= args.threshold:
            short_text += 1
            continue
        stem = path.stem
        pmid = str(data.get("pmid") or stem.rsplit("_", 1)[-1])
        eligible[pmid] = {"stem": stem, "word_count": wc}

    out.write_text(json.dumps(eligible, indent=2, ensure_ascii=False) + "\n")
    print(f"total papers: {total}")
    print(f"  parse errors: {parse_err}")
    print(f"  no main_text: {no_text}")
    print(f"  main_text <= {args.threshold} words: {short_text}")
    print(f"  eligible (main_text > {args.threshold} words): {len(eligible)}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
