---
name: search-refs
description: Search the local paper corpus to answer a literature question or surface evidence for a claim. Auto-invoke whenever the user asks "what does the literature say about X", asks for evidence/sources/citations, or asks any question that should be answered from the local corpus rather than from training data. Returns stem-format citations from papers/parsed/.
---

## When to invoke

User asks any of: "what does the literature say about X", "find papers on X", "is there evidence for X", "cite sources for X", "what do we have on X". Or any factual question whose answer should come from the local paper corpus rather than training data.

Skip if: the user explicitly asks for a PubMed search (use E-utilities `esearch.fcgi` instead), or wants to inspect a specific known paper by stem/PMID (just read the JSON directly).

## Workflow

1. Semantically enrich the user's query before searching. Expand abbreviations (e.g., TERT = telomerase reverse transcriptase), add synonyms (e.g., catalytic subunit), related terms (e.g., TERC, telomerase), and potential answer terms. Format the enriched query as a single string.

2. Run `python scripts/search_refs.py "<query>"`. Output is a JSON array of `{pmid, stem, score, snippet}` for the top papers.

3. Triage by snippet — drop papers whose snippet is clearly off-topic before reading further.

4. Read `papers/parsed/<stem>.json` `main_text` of the remaining candidates. DO NOT cite from the snippet alone — a 400-word window may drop qualifiers that change a finding's meaning.

5. Cite sources by writing the literal `stem` value (basename of `papers/parsed/<stem>.json`, e.g. `Cao_2024_Nature_38123456`) — copied verbatim from each result, no conversion or shortening. Multiple stems separated by `; `. The user runs `cite_refs.py` separately to convert stems to readable citations and assemble the References section.

## Project resolution

`search_refs.py` resolves the chroma collection from `cwd`:

- Inside `projects/<name>/` → query that project's collection.
- Outside any project subtree → query the `_global` collection.

The skill does not change cwd. If the user wants a project-scoped search, they should be in the project directory before invoking.
