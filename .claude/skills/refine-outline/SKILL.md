---
name: refine-outline
description: This skill should be used when the user asks to "refine the outline", "iterate the outline", "run an outline tick", "deepen the outline", "search literature and update bullets", or names an `outline.md` inside `projects/<name>/` and wants one tick of evidence-driven refinement. Executes one ralph-style tick of the per-iteration outline-refinement loop: reads the full outline for context, identifies the 2–3 weakest areas, generates Level-5 evidence questions, runs online (PubMed via fetch_abstracts.py) plus local (chroma_db via search_refs.py) search, synthesizes answers under search-first discipline, edits bullets in the chosen target subsections only, and audits the logical connections of edited subsections. The user (or `run_ralph.sh` driver) re-invokes for the next tick.
paths: projects/**/outline.md, projects/**/.session_scratch.md, projects/**/.ralph_prompt.md
---

## Overview

The skill executes one tick of the autonomous outline-refinement loop documented in `projects/<name>/plan.md` (Phase E pattern) and `projects/<name>/plan.history.md` (Phase B pattern). It does NOT manage scope decisions, locked decisions, full-text fact-checking, or termination — those belong to the user's project-level workflow. The skill treats the per-tick loop as a pure function: input is the current outline state plus the prior tick's notes, output is one iteration of evidence-driven refinement plus a clean scratch-file marker.

The skill is **project-scoped**. All state lives under `projects/<name>/`. The outline must be inside a project subtree.

## Citation format

A citation in an outline bullet is the literal `stem` value (basename of `papers/parsed/<stem>.json`, e.g. `Cao_2024_Nature_38123456`). `search_refs.py` returns the stem in every result. Copy it verbatim into the bullet — no conversion, no shortening. Multiple stems separated by `; `.

## When to invoke

User says: "refine the outline", "iterate the outline", "run one outline tick", "deepen the outline", "search literature and update bullets", "next ralph tick".

The skill is also designed to be invoked by `run_ralph.sh` (or any equivalent driver) per tick. Place the skill name in `projects/<name>/.ralph_prompt.md` to drive autonomous multi-tick iteration.

Skip if: the user wants prose drafting (use `/write-draft`), full-text fact-checking (use `/check-fact`), parser development (use `/develop-parser`), or PDF retrieval (use `/convert-pdf`).

## Hard preconditions

1. `projects/<name>/outline.md` exists. Skeleton-only (headers, no bullets) is fine for the first tick.
2. `refs.json` exists at the repo root.
3. `papers/` directory exists at the repo root. The skill bootstraps it on first invocation if empty.
4. `fetch_abstracts.py` and `search_refs.py` are available and runnable.
5. A `projects/<name>/.ralph_prompt.md` may exist with project-specific locked decisions (framing axis, scope constraints, included/excluded topics, termination criteria). The skill reads it but does not modify it. If absent, the skill operates with no extra constraints.

## State files

The skill reads and writes:

- `projects/<name>/outline.md` — primary deliverable; edited in scoped chunks per tick.
- `projects/<name>/.session_scratch.md` — iteration counter, Q&A history with iteration tag and L1–L5 level tag, weakest-areas tracker. Created if absent. Last line after each clean tick is `iteration <N> complete`.
- `papers/<stem>.json` — abstract-only JSONs accumulated across ticks via `fetch_abstracts.py`. Idempotent.
- `chroma_db/` — semantic index. Rebuilt once per tick.
- `refs.json` — appended by `fetch_abstracts.py` for newly-retrieved PMIDs.

The skill does NOT manage:
- `.ralph_prompt.md` — user authors. The skill reads it for locked decisions.
- `.ralph_done` — driver/user writes when stopping. The skill never writes it.
- `.ralph_questions.md` — out of scope (a separate phase-checkpoint skill or manual session handles end-of-loop user questions).

## Per-tick workflow

Execute exactly ONE iteration. Do NOT loop within a tick. Persist all state changes before exiting. The very last write to the scratch file must be `iteration <N> complete` — this marker is how the next tick detects clean exit.

### Step 1: Read state

Read in order:
1. `projects/<name>/.ralph_prompt.md` (if present) — extract locked decisions, framing axis, termination criteria, search-query bias, drug filter, or any other project-specific constraints.
2. `CLAUDE.md` at repo root — workflow and citation rules.
3. `projects/<name>/outline.md` — full outline, end to end.
4. `projects/<name>/.session_scratch.md` — iteration counter and prior-tick notes.

Determine current iteration N from the scratch file's `iteration:` line. If `iteration N complete` is the last line, start iteration N+1. If absent or in-progress (previous tick crashed), retry iteration N from the beginning. If the scratch file does not exist, create it with `iteration: 0` and `iteration 0 complete` markers, then start iteration 1.

### Step 2: Identify weak areas (full-outline scan)

Examine the full outline for weak-area signals:

- Subsections with fewer than 3 bullets
- Bullets without a stem citation (orphan claims)
- Bullets with vague wording (no quantitative anchor, no named cell line/model, no specific mechanism)
- Subsections with no 2021+ citations (recency-thin)
- Adjacent bullets with no clear logical connection
- Sections whose bullets do not match the framing axis specified in `.ralph_prompt.md` (if any)

Use the prior tick's "weakest areas for next iteration" notes from the scratch file as a shortcut: if those areas are still unaddressed in the current outline, start from them. If they have been addressed (covered by edits in subsequent ticks or no longer signal weakness), proceed with a fresh scan.

The full-outline scan is what surfaces cross-cutting issues (a citation in one section that contradicts another; a paper better-fitting a different section; redundant bullets shared across subsections). Do not skip it even if shortcuts from the scratch file are available.

Run the helper:
```
python .claude/skills/refine-outline/scripts/identify_weak_areas.py projects/<name>/outline.md --scratch projects/<name>/.session_scratch.md
```
The script reports candidate weak areas in JSON. Use the report as input but apply judgment — the script is heuristic, not authoritative.

### Step 3: Pick 2–3 targets

From the weak-area scan, choose the 2–3 areas this tick will address. Smaller scope per tick produces better evidence depth and safer atomicity. Write the chosen targets to the scratch file under a `## Iteration N targets` heading before generating questions.

Targets are subsection identifiers (e.g., `3.2`, `4.1`) or specific bullet ranges (e.g., `bullets 3-5 in section 2.3`). Be specific.

### Step 4: Generate questions (5-level hierarchy)

Question generation is top-down. Lower levels depend on higher-level answers. Only Level-5 questions trigger searches. Levels 1–4 are answered by agent judgment but written to the scratch file for auditability.

**Level 1 — Scope.** Generate L1 questions only if the user's `.ralph_prompt.md` allows scope changes AND recent evidence suggests the top-level structure should shift. Rare after the first few ticks.

**Level 2 — Subsections.** Generate L2 questions only if a section's subsection structure is being reconsidered this tick.

**Level 3 — Bullets.** Generate L3 questions when this tick is adding new bullets and needs to decide their order/grouping.

**Level 4 — Logical connections.** Generate L4 questions when the audit found BROKEN pairs that need bridging bullets or reordering.

**Level 5 — Per-bullet evidence needs.** Always generated. Aim for 3–7 questions per tick covering the chosen 2–3 target areas. Each L5 question should:

- Name a specific protein, complex, mutation, or mechanism class
- Ask for a specific kind of evidence (mechanism, structure, effect size, model system, comparator, counter-finding)
- Be answerable from a small number of abstracts (1–5 papers)
- Constrain scope by offering candidate answers when the question space is broad

Good L5 question example: "What is the structural basis of Y537S/D538G ESR1 mutations' constitutive activity — which helix shifts, which interactions are lost?"

Bad L5 question example: "What is known about ERα in breast cancer?" (too broad, no specific evidence target).

Write all generated questions to the scratch file under `## Iteration N questions` with iteration tag and level tag (L1–L5) BEFORE any search runs. Do not search before writing — the audit trail requires that questions are recorded first.

Do not re-ask questions already answered in prior ticks (read the scratch history). Do not ask questions sourced from agent prior knowledge alone — questions must be derived from outline gaps.

### Step 5: Online search (PubMed)

For each Level-5 question, translate to one or more PubMed queries. Apply enrichment per CLAUDE.md: expand abbreviations, add synonyms, propose related terms, anticipate answer vocabulary.

Two-pass per question:

1. **Recent-first pass** (primary harvest): append `AND ("2021"[PDAT] : "2026"[PDAT])` to the query and run:
   ```
   python fetch_abstracts.py --query "<query>" --retmax 40
   ```
2. **Foundational pass** (only if recent harvest is thin, fewer than 5 relevant returns): run unfiltered with `--retmax 20`. Foundational papers (field-defining mechanisms, original characterizations) are kept; mid-impact older papers are dropped if a 2021+ paper makes the same point.

Multiple recent-first queries per question are expected. Vary phrasing if the first attempt under-retrieves.

### Step 6: Local search (rebuild + query)

After all this tick's abstracts have been fetched, rebuild the project's chroma collection ONCE so the new abstracts become searchable in this tick's queries:
```
python scripts/build_model.py <name>
```
This rebuilds `chroma_db/<name>` from `projects/<name>/pmids.txt`. Do not rebuild between every individual query — that wastes time. Then for each question (run from inside `projects/<name>/` so cwd resolves to the project's collection rather than `_global`):
```
python scripts/search_refs.py "<question>"
```
This surfaces top-K semantically relevant abstracts from the now-augmented project collection. The collection contains all PMIDs in `projects/<name>/pmids.txt`, so this finds cross-cutting evidence within the project that might otherwise be missed.

### Step 7: Synthesize answers (search-first discipline)

For each question, construct an answer ONLY from `search_refs.py` results plus the abstracts retrieved this tick. Prior knowledge is NOT a permitted claim source for the outline — it was only a guide for query formulation.

Process:
1. Read the retrieved `papers/<stem>.json` `main_text` (title + abstract + keywords) for each top hit.
2. Triage by content. Drop irrelevant papers.
3. Apply source prioritization:
   - **Year tier**: 2021–2026 preferred. Older only when foundational/field-defining or no 2021+ paper makes the same point.
   - **Journal tier**: prefer top-tier — Nature family, Science family, Cell family, NEJM, JAMA, JCI, Lancet *, Annu Rev *. Read the journal field in `refs.json` to check.
   - **Tie-breaking**: cite top-tier when both available; cite both only if they add independent evidence (different model, different cohort).
   - Do NOT exclude lower-tier or older papers if they are the sole source for a needed mechanistic finding.
4. Synthesize a one-paragraph answer with `<stem>` citations. Each claim must trace to one or more retrieved stems.
5. Mark the question answered (≥1 supporting stem), partially answered (some sub-aspects unsupported), or unanswerable (no relevant retrieval after query refinement).

Record findings under each question in the scratch file under `## Iteration N findings`: per stem, journal + year + one-line takeaway, plus the synthesized answer with stem citations.

If `search_refs.py` returns nothing relevant for a question, the question is unanswered. Refine the query and re-loop, or mark unanswerable. Do NOT fall back to agent prior knowledge.

### Step 8: Update outline (scoped edits)

Edits are confined to the 2–3 target areas chosen in Step 3. Do not edit unrelated sections this tick, even if you noticed something edit-worthy during the full-outline scan.

For each target area:
- Add new bullets sourced from synthesized answers in Step 7.
- Refine existing bullets where new evidence sharpens, qualifies, or contradicts a prior claim.
- Remove bullets whose supporting answer turned out to be unsupported by retrieved abstracts on re-examination (no orphan claims).
- Reuse stems freely across bullets where supported by the retrieved abstract.

Bullet formatting contract (per `CLAUDE.md` and the project's plan.md):

- Each bullet is a single statement that, when expanded to prose, runs ≤ 2 sentences.
- Citations use stem form per `CLAUDE.md`. Multiple stems separated by `; `.
- Only `##` headers, no other markdown formatting.
- Every claim must trace to an abstract that explicitly states it AND to a question/answer pair recorded in this or a prior iteration's scratch file. If an abstract only implies the claim, weaken the wording or drop the bullet.
- Subsections may be added when an evidence cluster warrants grouping. No empty headers — fill or remove.
- If `.ralph_prompt.md` specifies a framing axis (e.g., "every bullet leads with the functional/regulatory claim"), apply it.

Exception: if a bullet needs to MOVE from one section to another to fix logical flow, that is allowed even if it touches a non-target section. Note the move in the scratch file under `## Iteration N moves`.

### Step 9: Logical-connection audit (scoped)

Audit only the subsections edited this tick. For each edited subsection, walk consecutive bullet pairs and classify each pair as one of:

- **SUPPORTS**: bullet N+1 reinforces bullet N with additional evidence
- **EXTENDS**: N+1 adds a new dimension to the same claim
- **CONTRASTS**: N+1 presents counter-evidence or a different mechanism
- **QUALIFIES**: N+1 narrows or conditions N's claim
- **BRIDGES**: N+1 transitions from one cluster to the next within the subsection
- **BROKEN**: no clear relationship; reorder, insert a bridging bullet, or move one bullet elsewhere

Run the helper:
```
python .claude/skills/refine-outline/scripts/audit_connections.py projects/<name>/outline.md --section <id>
```
The script reports per-pair classifications based on a heuristic. Apply judgment — fix BROKEN pairs in this tick before advancing.

Write per-pair classifications to the scratch file under `## Iteration N audit`. Do not advance to Step 10 until the edited subsections have zero BROKEN pairs.

### Step 10: Update scratch file and exit

Append a tick-closing block to `projects/<name>/.session_scratch.md`:

```
## Iteration N
- Targets: <list>
- Questions generated: <list with L1-L5 tags>
- Papers added to evidence pile: <stem list>
- Sections updated: <list>
- Stems added (NEW since prior tick): <count and list>
- Stems dropped from outline: <count and list>
- Audit results: <pass/fail per edited subsection>
- Weakest areas for iteration N+1: <list of 2-3 specific gaps>

iteration N complete
```

The `iteration N complete` line must be the very last line written. This marker is the atomicity gate for the next tick's crash-recovery logic.

Exit cleanly. The driver (or user) invokes the next tick.

## Final-tick exhaustive audit

When the user (or termination criteria in `.ralph_prompt.md`) signals this is the last tick before stopping, the tick performs an additional exhaustive audit beyond the per-tick scoped audit:

- Walk consecutive bullet pairs across ALL subsections in the outline (not just edited ones).
- Classify each pair per Step 9 categories.
- Fix every BROKEN pair within the same tick before exiting.

If the user wants a Step-8 reformat (outline body + single References section), they invoke `python convert_citation.py outline.md` separately after the final tick. The skill does NOT generate the References section — that is a one-time post-processing step outside the per-tick loop.

## Question-generation contract (strict)

The skill enforces these rules during Step 4:

1. Questions must be derived from outline gaps surfaced by the full-outline scan in Step 2 OR by the prior tick's weakest-areas notes. Do NOT generate questions sourced from agent prior knowledge alone.
2. Questions must be written to the scratch file with iteration tag and L1–L5 level tag BEFORE any search runs.
3. Do NOT re-ask questions answered in prior ticks (check the scratch history first).
4. Do NOT re-ask questions marked "unanswerable" in prior ticks unless query refinement would meaningfully change the search space.
5. Aim for 3–7 Level-5 questions per tick. If a tick legitimately needs more, prefer to split across two ticks rather than over-stuff one.
6. Each Level-5 question must be specific (named entity + specific evidence kind), answerable from a small number of abstracts, and tied to a known outline gap.

## Per-tick scope rules

- READ the full outline every tick.
- ANALYZE for weak areas across the full outline every tick (with scratch shortcut allowed).
- EDIT scoped to 2–3 chosen targets per tick. Bullet moves across sections allowed when needed for logical flow.
- AUDIT scoped to edited subsections per tick. Final-tick audit is full-outline.

## Constraints

- Do NOT call AskUserQuestion. The skill is non-interactive within a tick. End-of-loop user checkpoints are out of scope.
- Do NOT loop within this tick. Exactly one iteration per invocation.
- Do NOT skip writing the `iteration <N> complete` marker.
- Allowed scripts: `fetch_abstracts.py`, `search_refs.py`, plus the skill's bundled audit helpers. Do NOT run `get_refs.py`, `convert_html.py`, or `merge_refs.py` — those are full-text-upgrade territory and belong to a different workflow.
- Do NOT write `.ralph_done` — that is the driver's or user's decision.
- Do NOT generate the References section — that is a one-time post-processing step.

## Driver integration

The skill is designed to be invoked by `run_ralph.sh` per tick. Example `.ralph_prompt.md` body:

```
Invoke /refine-outline. Locked decisions for this loop:
- Framing axis: function/regulation lead, immune-microenvironment lens
- Scope: STAT1/STAT2 antagonism in HR+ breast cancer
- Termination criteria: ≥10 iterations, ≥50 unique stems, zero BROKEN pairs full-outline, ≥30% citation turnover since baseline
```

The driver re-invokes per tick until the termination criteria are met or `MAX` ticks are reached. The user manually writes `.ralph_done` when satisfied (or extends another phase via a new `.ralph_prompt.md`).

For one-off manual ticks, invoke `/refine-outline` directly. The skill executes one tick and exits.

## Outputs

- `projects/<name>/outline.md` — updated with this tick's edits.
- `projects/<name>/.session_scratch.md` — appended with this tick's iteration block. Last line is `iteration <N> complete`.
- `papers/<stem>.json` — abstracts retrieved this tick (idempotent additions).
- `refs.json` — appended with metadata for newly-retrieved PMIDs.
- `chroma_db/` — rebuilt with this tick's additions.

## Out of scope

- Scope and framing decisions (user defines manually before the first tick or in `.ralph_prompt.md`).
- Locked-decision management (user maintains `.ralph_prompt.md` constraints).
- Termination decision (user invokes the next tick or stops the driver).
- Full-text upgrade and fact-checking (use `/check-fact`).
- Prose drafting (use `/write-draft`).
- Final reformat to body + References section (run `python convert_citation.py outline.md` after the final tick).
- End-of-loop user-checkpoint question batching.
- HTML parser development (use `/develop-parser`).
- PDF retrieval (use `/convert-pdf`).
