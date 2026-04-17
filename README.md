## Literature Reference Database

## Adding Citations

1. Give Claude PMIDs or a source to extract them from.
2. Claude runs `python get_refs.py` to fetch metadata and HTML.
3. Claude runs `python convert_html.py papers/` to parse HTML into structured JSON.
4. Claude runs `python merge_refs.py` to resolve reference PMIDs.
5. Check refs_no_html.md for papers where HTML retrieval failed. For each:
   - Retrieve the HTML manually (e.g. via SingleFile Safari extension) and save to `papers/<stem>.html`, then tell Claude to rerun from step 3.
   - If HTML is unavailable, download the PDF to `papers/<stem>.pdf` and tell Claude to use the PDF fallback workflow.
6. Check refs_no_pmid.json for unresolved references. Fill in the empty keys with the correct PMIDs, then tell Claude to run `python merge_refs.py --patch`.
7. Claude runs `python search_refs.py --build` to rebuild the search index.

## Deleting Citations

1. Tell Claude which PMIDs to delete.
2. Claude runs `python get_refs.py --delete` and `python search_refs.py --build`.

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

## Manual HTML Retrieval

When automatic HTML retrieval fails (paper listed in refs_no_html.md):

1. Open the DOI URL in Safari.
2. Use the SingleFile Safari extension to save the complete page as a single HTML file.
3. Rename and move the file to `papers/<stem>.html`.
4. Tell Claude to continue from step 3 of the Adding Citations workflow.
