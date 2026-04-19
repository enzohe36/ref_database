## Project Overview

- This is a literature research and scientific writing assistant.

## General Rules

- Be brutally honest and straightforward in your response.
- If the user is wrong, you MUST point it out.
- If the user's idea will not work, you MUST point it out.
- If you are unsure about the user's intent, you MUST ask for clarification.
- If you don't have sufficient information to answer a question, you MUST say so.
- DO NOT give suggestions that you aren't sure if it will work.
- DO NOT flatter the user.
- When writing in md files, DO NOT use any markdown formatting except `##` headers, unless explicitly asked to.
- When writing in bullet points, DO NOT write more than one sentence per bullet point.
- When summarizing content, DO NOT balance summary length across sections. write as much as necessary to cover each section.
- When updating instructions, consider both CLAUDE.md and README.md for updates.

## File Structure

- `<stem>`: `<first_author_last_name>_<year>_<journal>_<pmid>` with Latin diacritics converted to ASCII, punctuation and spaces to `_`, collapsed. Stored as "stem" (first field) in refs.json. Used as file name for papers/ files.
- `<citation_in_text>`: derived from stem at conversion time. "LastName YYYY" (1 author) / "LastName & LastName YYYY" (2) / "LastName et al. YYYY" (3+).
- `<citation>`: `<authors>. <title>. <journal>. <year>;<volume>(<issue>):<pages>. PMID: <pmid>.` Used in the references section when drafting documents. `<authors>` is comma-separated "author" values from the "authors" array.
- chroma_db/: semantic search index (ChromaDB + sentence-transformers). Built from papers/*.json and refs.json.
- html_parsers/: Python package with per-publisher HTML parsing modules (nih.py, nature.py). Module names are second-level domains.
- papers/: article files.
  - `papers/<stem>.html`: full article HTML downloaded by get_refs.py via single-file.
  - `papers/<stem>.json`: structured article data (affiliations, references, main_text) created by convert_html.py, updated by merge_refs.py.
  - `papers/<stem>.pdf`: original PDF (fallback when HTML unavailable).
- papers_test/<second_level_domain>/: working copies of HTMLs used during parser development. Modified in place by convert_html.py (banner removal, JSON output). Re-create from papers_test_ref/ when a parser change makes existing test output stale.
- papers_test_ref/<second_level_domain>.zip: pristine reference copies of the HTMLs in papers_test/ at bootstrap time. Used to reset papers_test/ subfolders.
- journals.json: not stored. NLM journal data is downloaded to a temp file by parse_citation.py on each run when full citation parsing is needed.
- refs.json: citation database. JSON dict keyed by PMID. Each entry has fields (in order):
  - "stem": filesystem-safe name for papers/ files. Use as in-text citation when drafting; convert_citation.py converts to readable format.
  - "pub_type": array of publication types (e.g., ["Journal Article", "Review"]).
  - "title": paper title.
  - "journal": journal abbreviation (ISO format).
  - "year": publication year.
  - "volume", "issue": journal location.
  - "pages": page range.
  - "doi": DOI as URL (https://doi.org/...).
  - "authors": array of objects, each with "author" (string, "LastName Initials") and "affiliation" (array of strings).
  - "references": array of PMIDs (strings) cited by this paper.
- refs_no_html.md: papers where HTML retrieval failed. Written by get_refs.py.
- refs_no_pmid.json: unresolved references from merge_refs.py. Keyed by main paper PMID; each entry has "stem" (copied from refs.json for readability) and "references" (array of single-key dicts with empty key for manual PMID entry).

## Scripts

- `python get_refs.py <pmid> [<pmid> ...]`: retrieves citation metadata from PubMed, writes to refs.json, fetches full paper HTML to papers/<stem>.html via single-file. Records HTML fetch failures in refs_no_html.md. Skips non-Journal Articles, Retracted Publications, and duplicates.
- `python get_refs.py --path <file>`: reads PMIDs from a file (delimited by punctuation, spaces, or newlines).
- `python get_refs.py --delete <pmid> [<pmid> ...]`: removes the specified PMIDs from refs.json.
- `python get_refs.py --validate`: checks for Retracted Publications and published versions of preprints.
- `python convert_html.py [<path> ...]`: parses HTML using publisher-specific logic (html_parsers/ package), fills author affiliations, structured references, and main_text into papers/<stem>.json. Each path can be an HTML file or a directory (all .html files in it are processed). Defaults to papers/ when no path is given. Skips files whose JSON already has non-empty main_text.
- `python convert_pdf.py <path> [<path> ...]`: converts PDF to md (fallback when HTML unavailable).
- `python merge_refs.py`: for every refs.json entry with a corresponding papers/<stem>.json, fills empty affiliations by matching author names, resolves unresolved structured references via PubMed (DOI shortcut, then author surnames + title chunks + journal + year with iterative relaxation), and unions resolved PMIDs into refs.json's references list. Existing refs.json field values are never overwritten; references is the only field augmented. Unresolved references go to refs_no_pmid.json.
- `python merge_refs.py --patch`: copies manually resolved PMIDs from refs_no_pmid.json into papers/<stem>.json and refs.json (unioned), then removes them from refs_no_pmid.json.
- `python merge_refs.py --add-refs`: citation-graph expansion. Collects every PMID cited in refs.json's references lists, subtracts PMIDs already keyed in refs.json, and invokes get_refs.py on the remainder to fetch metadata and HTML.
- `python convert_citation.py <file>`: converts stem citations in a document to in-text citation format and adds a References section. Modifies the file in place.
- `python search_refs.py <query>`: searches papers by semantic similarity.
- `python search_refs.py --build`: rebuilds chroma_db/. Iterates refs.json, chunks and embeds papers/*.json main_text where available.

## Literature Search

- You MUST use PubMed E-utilities (esearch.fcgi) to search for papers unless explicitly asked to.

## Skills

- `/develop-parser`: parser contract, file layout, verification criteria, and bootstrap process for html_parsers/ modules. Auto-activates when editing files under html_parsers/, papers_test/, or papers_test_ref/.

## Searching for Information

1. Semantically enrich the user's query before searching. Expand abbreviations (e.g., TERT = telomerase reverse transcriptase), add synonyms (e.g., catalytic subunit), related terms (e.g., TERC, telomerase), and potential answer terms. Format the enriched query as a single string.
2. Run `python search_refs.py <query>`. List all the papers from the output and their similarity score.
3. Read papers/*.json main_text of top candidates.
4. Cite sources using `<stem>` when referencing specific findings. The user will run convert_citation.py to convert stems to readable citations.
