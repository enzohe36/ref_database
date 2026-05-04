---
name: write-draft
description: This skill should be used when the user asks to "write a draft", "compile a draft", "draft the review", "convert outline to prose", or names an `outline.md` inside `projects/<name>/` and wants prose written from it. Two-phase autonomous workflow that gates drafting on full-text availability for every cited paper, auto-revises outline bullets whose claims do not match the cited full text, then writes the entire draft section by section under strict prose-style requirements (paragraph-cluster structure, stress-position chaining, sentence-length limits, abbreviation expansion, cross-reference prohibition).
paths: projects/**/outline.md, projects/**/draft.md
---

## Overview

The skill compiles a fully-written prose draft from an existing outline at `projects/<name>/outline.md` and writes it to `projects/<name>/draft.md`. It is autonomous within a single invocation: it processes every section without per-section user approval. Two halts are possible: Phase 1 step 4 halts if any cited paper lacks full text, and Phase 1 step 5 may halt if outline-vs-source mismatches cannot be auto-revised cleanly. All other progression is automatic.

The skill is **project-scoped**. All state lives under `projects/<name>/`. The outline must be inside a project subtree.

## When to invoke

User says: "write a draft", "compile the draft", "draft the review", "convert outline.md to prose", "write the review based on outline", "draft each section".

Skip if: `outline.md` does not exist, the user is asking for outline editing rather than drafting, or the user wants ralph-style iterative refinement of the outline (different workflow).

## Hard preconditions

1. `projects/<name>/outline.md` exists.
2. The outline has informative section headers in the form `## 1. Section name`, `## 2.1 Subsection name`, etc.
3. Each section contains bullets in the canonical-vs-extensions or equivalent structured form. Flat unstructured bullet lists are out of scope — defer to outline-development workflows first.
4. `refs.json` exists and contains stem entries for every cited stem in the outline.
5. `papers/<stem>.json` exists for every cited stem (either abstract-only or full-text). If any stem is missing entirely, halt and ask the user to run `python fetch_abstracts.py <pmid>` for the missing stems before re-invoking.

## Phase 1 — Verification gate

Phase 1 is a hard gate. Drafting cannot begin until every step passes.

### Step 1: Outline structure check

Read `outline.md`. Verify each top-level section (`## N.` and `## N.X`) has at least one bullet. Verify the file ends with `## References` or has no References section yet — both are acceptable.

If the outline lacks the canonical-vs-extensions structure (e.g., flat bullet lists with no apparent grouping), halt and tell the user: "The outline at `projects/<name>/outline.md` does not have the canonical-vs-extensions structure required for drafting. Refine the outline first (per your project's outline-development workflow), then re-invoke."

### Step 2: Stem inventory

Extract all cited stems from the outline body (not the References section):

```
grep -oE '[A-Z][a-z]+_[0-9]{4}_[A-Za-z_]+_[0-9]+' projects/<name>/outline.md | sort -u
```

Note the regex may split compound surnames (e.g., `Tecalco_Cruz_2021` may match as `Cruz_2021`). Cross-check each match against `refs.json` to resolve to the canonical stem.

### Step 3: Full-text status check (all stems, no early exit)

For each cited stem, check `papers/<stem>.json` `main_text` length. Classification:

- **Full text**: `main_text` length ≥ 5000 chars AND does not start with `"<title>\n\nAbstract:"` pattern.
- **Abstract-only**: `main_text` length < 5000 chars OR starts with `"<title>\n\nAbstract:"`.

Iterate through every cited stem. Do not exit early on the first abstract-only finding. Build a complete list of abstract-only stems with their PMIDs, journals, and the section(s) where each is cited.

### Step 4: Halt-on-abstract-only with batched retrieval request

If the abstract-only list is non-empty, halt the skill and emit a structured retrieval request to the user. Format:

```
Drafting blocked. The following N papers are cited in the outline but have abstract-only full-text status. Drafting requires full text for every cited paper.

[Per-paper list, sorted by section appearance:]
- Stem: <stem>
  PMID: <pmid>
  Journal: <journal>
  Cited in: Section X.Y (and Section X.Z if applicable)

To retrieve all papers in one batch:

  python get_refs.py <pmid1> <pmid2> ... <pmidN>

Then delete the abstract-only JSONs and re-convert:

  for stem in <stem1> <stem2> ... ; do rm -f papers/${stem}.json; done
  python convert_html.py papers/<stem1>.html papers/<stem2>.html ...

If any HTML conversion fails because the publisher domain has no parser (e.g., bmj, e_crt), you have two options for those specific papers:
  (a) Develop a parser via /develop-parser.
  (b) Manually retrieve the PDF, place at papers/raw/<stem>.pdf, and run /convert-pdf.

If a paper is permanently unretrievable (preprint server gone, journal defunct, no parser developable), drop the citation from the outline or replace it with an alternative source. The skill cannot proceed with abstract-only.

After retrieval is complete, re-invoke the skill.
```

Exit the skill at this point. The next invocation restarts Phase 1 from step 1.

### Step 5: Outline verification with auto-revision

For each cited bullet in the outline, perform a claim-vs-source check:

1. Read the bullet text and identify the headline claim.
2. Read the cited paper's full `main_text`.
3. Verify each substantive element of the claim is explicitly supported by the paper:
   - Quantitative details (residue numbers, percentages, trial endpoints, fold changes) match the source.
   - Mechanistic claims are stated by the paper, not extrapolated.
   - Causal direction matches the paper's framing.

For each bullet whose claim does not match its cited source, generate an auto-revision:

- **Tighten**: trim the claim to what the paper actually supports. Keep the citation.
- **Replace citation**: if the paper does not support the headline at all but a different paper in the corpus does, switch the citation. Find candidates via `python search_refs.py "<claim keywords>"`.
- **Drop**: if no corpus paper supports the headline, drop the bullet entirely.

Apply each revision directly to `outline.md`. Log every change to `projects/<name>/revisions_applied.md` with three fields:

- Original bullet text
- Revised bullet text (or "DROPPED" if removed)
- Reason: the specific paper passage or absence-of-passage that drove the change

If a bullet's mismatch cannot be cleanly auto-resolved (ambiguous between tighten/replace/drop, or no clear path), halt and surface the bullet to the user for manual resolution. The user resolves and re-invokes.

After all bullets verify or auto-revise cleanly, proceed to Phase 2.

## Phase 2 — Autonomous drafting loop

Phase 2 processes every section in `outline.md` sequentially, in one invocation, without pausing for user approval.

For each section in order (Title, Abstract if present in outline, then `## 1.` through the last numbered section, then `## Conclusion` if separate, then `## References`):

### Section drafting steps

1. Read the section's outline bullets and the full `main_text` of every cited paper in the section.
2. Identify semantic clusters of interconnected information among the bullets. Each cluster becomes one paragraph. The cluster count drives the paragraph count — there is no minimum or maximum.
3. For each paragraph, write flowing prose under the writing requirements below.
4. Run the per-section auto-audit (see "Per-section auto-audit" below). If any check fails, revise and re-audit. Repeat until clean.
5. Append the section to `projects/<name>/draft.md`.
6. Advance to the next section.

After all sections are drafted, run the final-pass verification.

### Writing requirements (apply to every paragraph)

**Paragraph structure**

- Write in complete paragraphs with proper flow and transition between sentences.
- Change paragraph only when introducing a new group of interconnected information. Do not break paragraphs by bullet count.

**Stress position rule (within each paragraph)**

- The stress position is the clause before a dot, semicolon, colon, exclamation mark, or question mark. Content at the stress position is what the sentence emphasizes.
- The stress-position content of sentence N becomes the topic of sentence N+1, to be explained, extended, or contradicted.
- This drives topic-chained flow rather than topic-stuffed paragraphs.

**Sentence length** (enforced at whole-draft level, not per section)

- Individual sentence length ≤ 30 words, excluding parenthesized content.
- Average sentence length ≤ 20 words across the entire draft.
- Sentence boundaries are dot, semicolon, colon, exclamation mark, or question mark.
- Per-section audits report sentence-length statistics for information only and do not fail on length violations. The whole-draft audit aggregates sentences across all sections and is the gate.

**Diction and structure**

- Avoid repetitive mentioning of terms between adjacent sentences. Substitute synonyms or use pronoun back-reference where the meaning is preserved.
- Avoid dependent clauses unless expanded explanation is necessary or simplification disrupts passage flow.
- Avoid over-technical terms when a plain alternative is available. Example: "refractory to immune checkpoint blockade" → "resistant to immune checkpoint blockade".

**Abbreviations and term expansion**

- First mention of an abbreviation: full name followed by the abbreviation in parenthesis. Example: "Estrogen receptor α (ERα) is a ligand-activated transcription factor."
- First mention of an uncommon technical term: expand inline. Example: "alveologenesis, a process in which terminal milk-producing alveoli form,".
- Subsequent mentions use the abbreviation or term alone.
- Domain-universal abbreviations (DNA, RNA, mRNA, PCR) need not be expanded.

**Parenthesis usage (strict)**

- Reserve parenthesis for two purposes only: abbreviations and stem-form citations.
- No parenthetical asides, no parenthetical examples, no parenthetical clarifications.
- If a clarification is needed, integrate it as a clause or sentence.

**Cross-reference prohibition (strict)**

- No references to other sections by number ("Section 4.1", "Section 3.2").
- No "discussed below", "discussed above", "explored in", "returned to", "rest of this review", "the next section", "the previous section".
- Each section must read as a standalone unit. Where a claim depends on context from another section, restate the necessary context inline.

### Per-section auto-audit

After drafting each section, run:

```
python .claude/skills/write-draft/scripts/audit_writing.py projects/<name>/draft.md --section <N>
```

The script reports across four dimensions:

1. **Sentence length** (informational only at the per-section level): reports the section's sentence count, max length, and average length. Does not fail the per-section audit. Length enforcement is whole-draft only.
2. **Parenthesis usage**: lists every parenthetical that is neither an abbreviation pattern (e.g., `(ERα)`, `(PR)`) nor a stem-form citation (matches `[A-Z][a-z]+_[0-9]{4}_[A-Za-z_]+_[0-9]+`). Per-section enforcement.
3. **Abbreviation first-mention**: lists abbreviation-shaped tokens (≥2 letters, mixed case allowed for Greek letters) whose first occurrence in the section is not immediately preceded by a full-name expansion. Per-section enforcement.
4. **Cross-reference**: lists every match for the cross-reference grep pattern. Per-section enforcement.

If any of dimensions 2–4 reports violations, revise the offending sentences/paragraphs and re-run the audit. Repeat until the audit reports zero violations across those three dimensions. Then advance to the next section. Sentence-length statistics are tracked across sections and audited at the whole-draft level after all sections are drafted.

The judgment-based requirements (stress-position chaining, adjacent-sentence term repetition, dependent-clause minimization, plain-alternative diction) are not auto-checkable. Apply them during writing; self-review each paragraph against them before running the auto-audit.

### Final-pass verification

After all sections drafted, run:

```
python .claude/skills/write-draft/scripts/audit_writing.py projects/<name>/draft.md --whole
```

Whole-draft checks:

- Aggregate sentence-length statistics: max ≤ 30, average ≤ 20.
- Aggregate parenthesis-usage check: 100% of parens are abbreviations or stem citations.
- First-mention abbreviation check across the whole draft: each abbreviation introduced exactly once with its full form, in order of first appearance.
- Cross-reference scan: zero hits.
- Cited-stem reconciliation: stems in `draft.md` body match the outline's cited stems (no orphan citations from the outline, no uncited additions in the draft).

If any whole-draft check fails, identify offending sections and revise. Re-run until clean.

### State file

Maintain `projects/<name>/draft_progress.md` with one entry per skill invocation:

```
## Run YYYY-MM-DD HH:MM:SS

- Phase 1 status: passed | halted at step <N> (with reason)
- Sections drafted: <list of section headers>
- Per-section audit results: <pass/fail per dimension>
- Final verification status: passed | failed (with details)
- Total cited stems: <N>
- Auto-revisions applied to outline: <N> (see revisions_applied.md)
```

This file is append-only across runs.

## Outputs

- `projects/<name>/draft.md` — the prose draft, full text covering every section in the outline.
- `projects/<name>/revisions_applied.md` — log of auto-applied bullet revisions during Phase 1 step 5 (one entry per revision).
- `projects/<name>/draft_progress.md` — per-run log of drafting progress and audit results.

## Out of scope

- Outline construction. The skill assumes the outline is already developed via the user's outline-development workflow (manual editing, ralph loop, etc.).
- Iterative outline refinement (ralph-style loops). Use the user's existing iteration workflow before invoking write-draft.
- Citation refinement and fact-checking after drafting. Use `/check-fact` (separate skill) to verify and add citations to the prose draft.
- Abstract drafting as a separate task. The abstract is treated as Section 0 within the autonomous loop; if the outline has an `## Abstract` block, that block is drafted before Section 1.
- PDF retrieval. Defers to `/convert-pdf` (separate skill).
- HTML parser development. Defers to `/develop-parser` (separate skill).
- Reference-list generation. The user runs `python convert_citation.py draft.md` after the skill completes.

## What to tell the user when the skill completes

A successful completion message states:

- The draft path: `projects/<name>/draft.md`
- Total sections drafted, total cited stems, total auto-revisions applied (with reference to `revisions_applied.md` if non-zero)
- The final verification status (all four whole-draft dimensions passed)
- The next steps: review the draft; review `revisions_applied.md` for any auto-revisions to confirm or revert; run `python convert_citation.py draft.md` to generate the References section; optionally run `/check-fact` to verify citations
