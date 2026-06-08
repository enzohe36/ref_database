---
name: check-fact
description: Fact-check a finished review draft and add inline citations from the local paper corpus. One agent per paragraph runs in parallel; conclusion runs in a second wave restricted to citations already used in the body. Outputs <draft_stem>.cited.md and feeds it to scripts/cite_refs.py for the final References section. Use when the user asks to "fact-check the draft", "add citations to a review", "verify claims and cite sources" or names a draft.md inside projects/<name>/.
paths: projects/**/draft*.md, projects/**/factcheck/**
---

## Overview

The fact-check workflow runs over a draft markdown file inside a `projects/<name>/` subtree. Each paragraph in the body is sent to a separate Agent that searches the local corpus, reads candidate papers' `main_text`, and emits per-sentence verdicts with stem-format citations. The conclusion runs in a second wave with a closed-stem rule (it may only cite stems already cited in the body). The main session reviews flagged factual errors, applies a `decisions.json` of accepted rewrites, and runs `scripts/apply_verdicts.py` to assemble `<draft_stem>.cited.md`. Finally `python scripts/cite_refs.py <document>` converts stems to "Author et al. YYYY" form and generates the References section.

The skill is **project-scoped**. All state lives under `projects/<name>/factcheck/`. The draft must be inside a project subtree.

## Citation format

A citation is the literal `stem` value (basename of `papers/parsed/<stem>.json`, e.g. `Cao_2024_Nature_38123456`). `search_refs.py` returns the stem in every result; the eligible-papers file lists it under each PMID. Copy the stem verbatim into the verdict's `citations` array — no conversion, no shortening, no author-year reformatting.

## When to invoke

User says: "fact-check the draft", "add citations to <draft>", "verify and cite", "go through the review and check claims", "check facts in projects/X/draft.md".

Skip if: the draft has no project parent (`projects/<name>/`), there is no embedding model built (`chroma_db/` is missing), or the user wants to verify a single specific claim (just run `scripts/search_refs.py` directly).

## Prerequisites

1. The embedding model must be built: `python scripts/build_model.py` (no args = `_global` collection over every `papers/parsed/<stem>.json` with non-empty `main_text`). Fact-check agents query `_global` because they run with `cwd` outside any project subtree.

2. The draft must use one-line-per-paragraph formatting (paragraph == one non-blank content line). The skill's parser does not handle multi-line paragraphs.

3. Paragraph-aware structure: numbered headings (`1.`, `2.1`, `2.3.1`) or `## Heading` for the conclusion. The first content line of the file is the title (preserved verbatim). The `Abstract` heading and its single paragraph are excluded entirely. The highest-numbered top-level section with body content is treated as the conclusion (Wave 2, closed-stem rule).

## Pipeline (six stages)

### Stage 1 — split sentences

```
python .claude/skills/check-fact/scripts/split_sentences.py <draft.md>
```

Walks the draft, strips existing inline citation stems, sentence-splits each paragraph at `. ; : ! ?` while masking decimals, abbreviations (`e.g.`, `et al.`, `Fig.`, etc.), DOIs, mutation notations (`Y537S`, `S499A/S733A`), and unit-after-decimal patterns (`3.8 mo`, `1.95 Å`). Writes `projects/<name>/factcheck/inputs/NN_<section_id>_p<para_idx>.json`, one per paragraph, with `global_idx`, `section_id`, `header`, `para_idx`, `wave` (1 = body, 2 = conclusion), `paragraph_text`, and `sentences[]`.

### Stage 2 — eligibility filter

```
python .claude/skills/check-fact/scripts/build_eligible_papers.py <project_name> [--threshold 5000]
```

Walks `papers/parsed/*.json`, keeps those with `main_text` word count > threshold (default 5000). Writes `projects/<name>/factcheck/papers_full_text.json` keyed by PMID. Agents may only cite stems whose PMID is in this file — this prevents citing abstract-only papers where claims cannot be verified against the body. Tune the threshold down if too few papers pass.

### Stage 3 — parallel paragraph agents (two waves)

**Wave 1 — body paragraphs.** For each input file under `factcheck/inputs/` whose `wave == 1`, dispatch one Agent in a single message:

```python
Agent({
  subagent_type: "general-purpose",
  description: "Fact-check para NN_<section_id>",
  prompt: <agent_prompt>,
  run_in_background: true,
})
```

The prompt is `.claude/skills/check-fact/scripts/agent_prompt.md` with the placeholders filled in:
- `{REPO_ROOT}` — absolute path to repo root
- `{INPUT_PATH}` — absolute path to the paragraph's input JSON
- `{OUTPUT_PATH}` — absolute path to its destination verdict JSON in `factcheck/verdicts/`
- `{ELIGIBLE_PATH}` — absolute path to `factcheck/papers_full_text.json`
- `{ALLOWLIST_BLOCK}` — empty string for Wave 1
- `{ALLOWLIST_FILTER}` — empty string for Wave 1

Each agent ends by writing the JSON output and emitting `WROTE <absolute path>` as its final message. The 30+ agents run in true parallel; each handles one paragraph (typically 4-12 sentences) and writes to a distinct file.

**Build the conclusion allowlist.** After all Wave 1 verdicts return:

```python
python -c "
import json
from pathlib import Path
fc = Path('projects/<name>/factcheck')
allowed = set()
for vp in (fc / 'verdicts').glob('*.json'):
    data = json.loads(vp.read_text())
    for v in data['verdicts']:
        if v['verdict'] == 'supported':
            allowed.update(v.get('citations', []))
(fc / 'conclusion_allowlist.json').write_text(json.dumps(sorted(allowed), indent=2))
"
```

**Wave 2 — conclusion paragraphs.** Same Agent dispatch as Wave 1, but the prompt's `{ALLOWLIST_BLOCK}` is set to:

```
ALLOWLIST: {ALLOWLIST_PATH} — a JSON array of stem strings. Every stem in your output `citations` MUST appear in this allowlist. A conclusion synthesises prior sections; introducing new citations there is bad form.
```

and `{ALLOWLIST_FILTER}` to ` (and stems in the eligible-papers file but absent from the allowlist are also excluded — the closed-stem rule is stricter)`.

If a conclusion claim is not supported by any allowlisted stem, the agent should emit `verdict: "unsupported"` with `citations: []` and an `error_note` flagging the gap.

### Stage 4 — main-session review of high-severity errors

After all verdicts exist, aggregate flagged sentences:

```python
python -c "
import json
from pathlib import Path
fc = Path('projects/<name>/factcheck')
for vp in sorted((fc / 'verdicts').glob('*.json')):
    data = json.loads(vp.read_text())
    for v in data['verdicts']:
        if v['severity'] in ('medium', 'high') or v['verdict'] == 'error':
            print(f\"[{data['global_idx']:02d} {data['section_id']} s{v['sent_idx']}] {v['verdict']}/{v['severity']}\")
            print(f\"  text: {v['text'][:120]}\")
            if v.get('error_note'):
                print(f\"  note: {v['error_note'][:200]}\")
            if v.get('suggested_rewrite'):
                print(f\"  suggest: {v['suggested_rewrite'][:200]}\")
"
```

For each high/medium-severity error verdict the main session decides:

- **Real factual error with concrete evidence** (e.g., wrong residue count, reversed direction, wrong drug name): accept the agent's `suggested_rewrite` (or write a better one) into `projects/<name>/factcheck/decisions.json`.
- **Unsupported because the supporting paper failed the threshold**: leave the claim text unchanged (no decision needed — it just won't get a citation). The user may admit the paper later by relaxing the threshold or running `python scripts/get_html.py <pmid>` to upgrade its `main_text`.
- **False alarm (agent was wrong)**: leave the claim unchanged. No decision needed.

`decisions.json` schema (all keys are strings):

```json
{
  "<global_idx>": {
    "<sent_idx>": "rewritten sentence text"
  }
}
```

Use the object form when you also want to override the verdict's citations:

```json
{
  "<global_idx>": {
    "<sent_idx>": {
      "text": "rewritten sentence text",
      "citations": ["Stem1", "Stem2"]
    }
  }
}
```

### Stage 5 — assemble draft.cited.md

```
python .claude/skills/check-fact/scripts/apply_verdicts.py <draft.md>
```

Re-splits the draft, applies decisions, scrubs inline author-year and stem refs from each sentence (so agent rewrites containing `(Cao 2024)` or `(Chan_2012_...)` don't double-cite), attaches `verdict.citations`, consolidates consecutive identical citation sets within each paragraph (drops citations from N..N+k-1, keeps on N+k), inserts each remaining citation set as `(Stem1; Stem2)` immediately before the sentence's terminating punctuation, and reassembles. Title, abstract, and section-only headers pass through verbatim. Output: `<project>/<draft_stem>.cited.md`.

### Stage 6 — convert stems to readable form

```
python scripts/cite_refs.py projects/<name>/<draft_stem>.cited.md
```

`cite_refs.py` converts each stem to "Author et al. YYYY" form, alphabetises the References section by first author + year, and renumbers entries. Runs from anywhere — no project context needed.

Optional follow-up: agents may have introduced citations from `_global` papers that aren't yet in `projects/<name>/pmids.txt`. To make those papers searchable in future project-scoped queries, manually append the new PMIDs (visible in the draft's References section) to `projects/<name>/pmids.txt`, then rebuild the project collection:

```
python scripts/build_model.py <name>
```

## Critical implementation notes

1. **Inline citation scrubbing.** Agents' `suggested_rewrite` text often contains author-year refs like `(Cao 2024)` or stem-format refs like `(Chan_2012_Breast_Cancer_Res_22264274)` mixed into prose. `apply_verdicts.py` strips these via a regex that matches both formats but spares non-citation parens (`(Section 3.2)`, `(approximately 50%)`, `(1.8 Å)`). The verdict's `citations` field is the canonical citation source.

2. **Compound author last names.** Stems for hyphenated authors (Robin-Jagerschmidt, Le-Trilling) are written `Robin_Jagerschmidt_2000_Mol_Endocrinol_10894152` in the file system. Stem regex must allow `(?:_[A-Z][a-zA-Z]*)*` repetition before the year. Three places use the regex: `split_sentences.py`, `apply_verdicts.py`, and `scripts/cite_refs.py` (already correct in the repo's version).

3. **Citation consolidation.** Within a paragraph, consecutive sentences carrying the same citation set keep the citation only on the last sentence in the run. Empty citation sets break runs. This matches the convention "the entire preceding passage is from X."

4. **Closed-stem rule for the conclusion.** Wave 2 agents must filter their search results not just by the eligible-papers file but also by the conclusion allowlist (union of all "supported" citations from Wave 1). This prevents the conclusion from introducing first-time citations.

5. **One agent per paragraph, not per sentence.** Each agent receives the full paragraph as context. Sentences starting with "Importantly,", "In contrast,", or numbered list items only make sense with the surrounding argument. Sentence-level dispatch loses too much context.

6. **Search cwd matters.** Agents run with `cwd = repo_root`, which puts them outside any project subtree. `scripts/search_refs.py` then queries the `_global` chroma collection. If you ran agents from inside the project, search would be limited to `projects/<name>/pmids.txt` — defeating the purpose since the goal is to discover *new* citations.

## Verification

After Stage 6:

1. `grep -c '^[0-9]\+\. ' <draft_stem>.cited.md` should equal the count of unique cited stems.
2. `grep -oE 'PMID: [0-9]+' <draft_stem>.cited.md | sort | uniq -d` should be empty (no duplicate PMIDs).
3. Visual scan: every body paragraph ends in a citation (per consolidation), the conclusion has no first-appearance citations, the abstract is unchanged.

## Out of scope

- The threshold for full-text eligibility is global (default 5000 words). The skill does not auto-tune per draft. Lower it if too many claims fall to "unsupported" because their supporting papers are abstract-only.
- The skill does not retrieve missing papers. If an agent reports an unsupported claim and identifies the missing paper by PMID, the user runs `python scripts/get_html.py <pmid>` and `python scripts/merge_refs.py <pmid>` separately, then reruns the affected paragraph agent.
- The skill does not modify the abstract.
