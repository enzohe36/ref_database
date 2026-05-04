## Literature Reference Database

## Adding Citations

1. Collect PMIDs (e.g. from PubMed search, Claude's suggestions, or a file).
2. Run `python scripts/get_refs.py <pmid> [<pmid> ...]` to fetch PubMed metadata into papers/parsed/<stem>.json. A list arg is a file containing PMIDs.
3. Triage by title + abstract + keywords (already in parsed/<stem>.json) to decide which papers need full-text-quality main_text.
4. For those papers, run in order:
   - `python scripts/get_html.py <pmid> [<pmid> ...]` to fetch full-text HTML to papers/raw/<stem>.html.
   - `python scripts/convert_html.py <pmid> [<pmid> ...]` to parse the HTML into papers/raw/<stem>_converted.json.
   - If main_text quality is poor and a PDF is available, retrieve the PDF manually to papers/raw/<stem>.pdf and ask Claude to invoke the `convert-pdf` skill in parallel agents.
   - `python scripts/merge_refs.py <pmid> [<pmid> ...]` to apply the _converted.json updates onto papers/parsed/<stem>.json (parallel).
5. Run `python scripts/build_model.py` to rebuild the global search index, or `python scripts/build_model.py <project_name>` for a specific project.

Optional, only when a paper's cited references are worth adding to the database: run `python scripts/get_pmids.py <pmid|json|list> [<pmid|json|list> ...]` *before* the `merge_refs.py` step above. It resolves empty `pmid` fields in `_converted.json`'s `references[]` via PubMed (sequential, rate-limited). The next `merge_refs.py` then unions those resolved reference PMIDs into the parent paper's `references` list.

## Searching

- Local semantic search: `python scripts/search_refs.py "<query>"`.
  - Run from inside `projects/<name>/` to search that project's collection.
  - Run from anywhere else to search the global collection.

## Drafting and Citing

- Cite paper stems inline in your draft as `LastName_YYYY_Journal_PMID`.
- Run `python scripts/cite_refs.py <draft.md>` from inside `projects/<name>/`. The script converts inline stems to "Author YYYY" form, creates/updates the References section (sorted alphabetically), and auto-appends every cited PMID to `projects/<name>/pmids.txt`.

## File Structure

- papers/parsed/<stem>.json: source of truth per paper (PubMed metadata + main_text + flat reference PMID list).
- papers/raw/<stem>.html: raw or banner-cleaned HTML.
- papers/raw/<stem>.pdf: original PDF (manual fallback).
- papers/raw/<stem>_converted.json: structured output of HTML/PDF conversion. References here are structured objects (with bib fields) rather than flat PMIDs.
- papers/test/: working copies for parser/agent prompt development. Files here have their _converted.json written next to the input.
- projects/<name>/pmids.txt: per-project PMID membership list.
- projects/<name>/drafts/, factcheck/, etc.: per-project document files.
- chroma_db/: shared embedding store. Holds named collections — one per project plus `_global`.
- scripts/html_parsers/: per-publisher HTML parser modules.
- NLM journal list is downloaded fresh in memory by convert_html.py (no on-disk cache).

## Stem Format

`<first_author_last_name>_<year>_<journal>_<pmid>` with Latin diacritics converted to ASCII, punctuation and spaces replaced with `_`, collapsed. Stored as "stem" in papers/parsed/<stem>.json.

Example: "Rocken 2024 Nat Commun 12345678" becomes `Rocken_2024_Nat_Commun_12345678`.
