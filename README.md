## Literature Reference Database

## Adding Citations

1. Collect PMIDs (e.g. from PubMed search, Claude's suggestions, or a file).
2. Run `python get_refs.py <pmid> [<pmid> ...]` to fetch metadata to refs.json and HTML to papers/. Use `--path <file>` to read PMIDs from a file.
3. Run `python convert_html.py` to parse papers/*.html into papers/*.json.
4. Run `python merge_refs.py` to fill affiliations and resolve reference PMIDs via PubMed.
5. Check refs_no_html.md for papers where HTML retrieval failed. For each:
   - Retrieve the HTML manually (e.g. via SingleFile Safari extension) and save to `papers/<stem>.html`, then rerun from step 3.
   - If HTML is unavailable, download the PDF to `papers/<stem>.pdf` and ask Claude to use the PDF fallback workflow.
6. Check refs_no_pmid.json for unresolved references. Fill the empty `""` keys with the correct PMIDs, then run `python merge_refs.py --patch`.
7. Run `python search_refs.py --build` to rebuild the semantic search index.

## Deleting Citations

1. Decide which PMIDs to delete.
2. Run `python get_refs.py --delete <pmid> [<pmid> ...]` to remove entries from refs.json (papers/ files are left untouched).
3. Run `python search_refs.py --build` to rebuild the semantic search index.

## File Structure

- papers/<stem>.html: full article HTML (downloaded automatically or manually via SingleFile).
- papers/<stem>.json: structured article data (metadata, affiliations, references, main_text).
- papers/<stem>.pdf: original PDF (fallback).
- refs.json: citation database keyed by PMID.
- refs_no_html.md: papers where automatic HTML retrieval failed. Format: stem + DOI, one pair per entry.
- refs_no_pmid.json: unresolved references. Keyed by main paper PMID, each with a "references" array of single-key dicts. Fill the empty `""` key with the correct PMID to resolve.

## Stem Format

`<first_author_last_name>_<year>_<journal>_<pmid>` with Latin diacritics converted to ASCII, punctuation and spaces replaced with `_`, collapsed. Stored as "stem" in refs.json.

Example: "Rocken 2024 Nat Commun 12345678" becomes `Rocken_2024_Nat_Commun_12345678`.
