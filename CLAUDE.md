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
  - "journal": journal abbreviation (ISO format).
  - "volume", "issue": journal location.
  - "year": publication year.
  - "title": paper title.
  - "pages": page range.
  - "doi": DOI as URL (https://doi.org/...).
  - "authors": array of objects, each with "author" (string, "LastName Initials") and "affiliation" (array of strings).
  - "publication_types": array of types (e.g., ["Journal Article", "Review"]).
  - "references": array of PMIDs (strings) cited by this paper.
- refs_no_html.md: papers where HTML retrieval failed. Written by get_refs.py.
- refs_no_pmid.json: unresolved references from merge_refs.py. Keyed by main paper PMID, each with a "references" array of single-key dicts (empty key for manual PMID entry).

## Scripts

- `python get_refs.py <pmid> [<pmid> ...]`: retrieves citation metadata from PubMed, writes to refs.json, fetches full paper HTML to papers/<stem>.html via single-file. Records HTML fetch failures in refs_no_html.md. Skips non-Journal Articles, Retracted Publications, and duplicates.
- `python get_refs.py --path <file>`: reads PMIDs from a file (delimited by punctuation, spaces, or newlines).
- `python get_refs.py --delete <pmid> [<pmid> ...]`: removes the specified PMIDs from refs.json.
- `python get_refs.py --validate`: checks for Retracted Publications and published versions of preprints.
- `python convert_html.py <path> [<path> ...]`: parses HTML using publisher-specific logic (html_parsers/ package), fills author affiliations, structured references, and main_text into papers/<stem>.json. Each path can be an HTML file or a directory (all .html files in it are processed). Skips files whose JSON already has non-empty main_text.
- `python convert_pdf.py <path> [<path> ...]`: converts PDF to md (fallback when HTML unavailable).
- `python merge_refs.py`: scans refs.json for entries with empty affiliations or references. Fills affiliations from papers/<stem>.json by matching author names. Resolves structured references (from HTML) or raw citation strings (from PDF) to PMIDs via PubMed search. Updates papers/<stem>.json with resolved PMIDs and copies them to refs.json. Saves unresolved references to refs_no_pmid.json.
- `python merge_refs.py --patch`: copies manually resolved PMIDs from refs_no_pmid.json into papers/<stem>.json and refs.json, then removes them from refs_no_pmid.json.
- `python convert_citation.py <file>`: converts stem citations in a document to in-text citation format and adds a References section. Modifies the file in place.
- `python search_refs.py <query>`: searches papers by semantic similarity.
- `python search_refs.py --build`: rebuilds chroma_db/. Iterates refs.json, chunks and embeds papers/*.json main_text where available.

## Literature Search

- You MUST use PubMed E-utilities (esearch.fcgi) to search for papers unless explicitly asked to.

## Adding Citations

Wait for user confirmation before each step.
1. Identify PMIDs from a user-specified source.
2. User runs `python get_refs.py <pmid> [<pmid> ...]`.
3. User runs `python convert_html.py papers/`.
4. User runs `python merge_refs.py`.
5. User resolves unresolved PMIDs in refs_no_pmid.json, then runs `python merge_refs.py --patch`.
6. User runs `python search_refs.py --build`.

If the user manually retrieved HTML or PDF for papers listed in refs_no_html.md:
- If HTML: user saves to papers/<stem>.html, proceeds from step 3.
- If PDF: user runs `python convert_pdf.py`, then launch one agent per paper to format it as papers/<stem>.json using the prompt below, then user proceeds from step 4.


## Developing Parser Modules

Each publisher needs a parser module at `html_parsers/<second_level_domain>.py`. Modules must be standalone: no cross-module imports. Shared utilities go in `html_parsers/_helpers.py`. A canonical template lives at `html_parsers/_template.py`; copy it to `html_parsers/<second_level_domain>.py` and fill in the stubs.

The module must expose two public functions:
- `remove_banners(html)` -- Remove cookie banners, consent dialogs, overlays. Return html unmodified if nothing to remove. User provides specifics per publisher; do not guess.
- `parse_article(html)` -- Single entry point. Returns dict with refs.json keys in order: stem (empty), journal, volume, issue, year, title, pages, doi, authors, publication_types (empty list), references, main_text.

`parse_article` delegates to these private functions (identical signatures in every parser):
- `_parse_metadata(html) -> dict` with keys title, journal, volume, issue, year, pages, doi.
- `_parse_authors(html) -> list` of `{"author": "LastName IN", "affiliation": [str, ...]}`.
- `_parse_references(html) -> list` of `{"": {journal, volume, issue, year, title, pages, doi, authors}}`.
- `_parse_main_text(html) -> str`.

Output format conventions (both main paper and references):
- journal: ISO abbreviation without trailing period.
- year: 4-digit publication year, not received/accepted/online year.
- pages: "firstpage-lastpage" or firstpage alone.
- doi: https://doi.org/... URL.
- authors in main paper: list of dicts `{"author": "LastName IN", "affiliation": [str, ...]}`.
- authors in references: list of plain strings in "LastName IN" format (no affiliation).
- The canonical "LastName IN" formatter is `_helpers.format_name(given, surname)`. Never build initials or split names inline in a parser.

Author-name contract (applies to `_parse_authors` and `_parse_references`):
- Extract `(given, surname)` pairs from the HTML. Prefer structured sources (separate given-name/surname tags, schema.org microdata, JSON blobs) over combined strings — they eliminate the surname-boundary guess.
- Call `format_name(given, surname)` to emit `"LastName IN"`.
- When the HTML only exposes a combined name string (e.g. `citation_author` meta in `Given Last` form, `dc.contributor`, body citations like `JD Griffith` or `Boulé J.-B.`), pass it to `format_author_name`, which routes through `parse_combined_name` + `format_name`.
- Never tokenize, split, flip, or build initials inline. Hyphenated given names (`Jean-Baptiste` → `JB`), dotted initials (`J.-B.`), already-compact initials (`JA` stays `JA`), compound surname prefixes (`de Lange`, `d'Adda di Fagagna`, `Nick McElhinny`), and trailing suffixes (`Jr.`, `III`) are all handled centrally. Extending coverage for a new compound surname or initial convention happens in `_helpers`, not in a parser.
- Forbidden patterns inside parsers (all greppable): inline `re.split(...)` applied to author names, `p[0] for p in ...split()` for initials, bespoke surname-flip helpers, per-parser `_PARTICLES` / `_NAME_PARTICLES` / `_SURNAME_PREFIXES` sets.

File layout (canonical; must match in every parser):
- Module docstring: single line, `"""<Publisher> (<second-level-domain>) HTML parser."""`.
- Imports: stdlib first (alphabetized), then `from html import unescape`, then `from ._helpers import (...)` with names alphabetized in the tuple.
- No in-body imports.
- Module constants after imports, in fixed order when present: `_NOISE`, `_REF_RE`, `_SUPP_RE`, `_CHROME_RE`, then any publisher-specific constants.
- Six section dividers, in this exact order: Banner removal, Metadata, Authors, References, Main text, Public API.
- Divider format: 75 hyphens, matching the template.
- Publisher-specific private helpers (e.g. `_parse_abstract`, `_find_h2_headings`) live under the section where they are first called, without their own dividers.
- Docstrings: every public and private function has one. First line is a one-sentence description. Longer functions add paragraphs for output format, key caveats, and publisher-specific quirks.
- `parse_article` body is identical in every parser (unpacks `_parse_metadata` result and calls the three other helpers).

When modifying a parser for any reason, check that it still matches the template and bring it into shape in the same change.

Parser objective: the JSON output should capture all content visible in the rendered HTML. All of the following must be in the JSON: metadata, author info, references. main_text must contain: abstract, keywords, abbreviations, body sections, figure/table captions, table content, methods, supplementary materials (methods, tables, captions, references).

Content verification uses rendered HTML text as reference. Open the HTML in a browser via CDP and extract visible text using `document.body.innerText`. This captures exactly what a user sees, handling CSS visibility, JS-rendered content, and complex layouts. The rendered text represents all content available in the HTML without needing a PDF. Caveat: author affiliations are sometimes collapsed (not visible in rendered text but present in the HTML source, e.g. meta tags or hidden divs). If rendered text lacks affiliations but refs.json has author entries, check the HTML source directly. Compare:
- refs.json entry vs parsed metadata: field-by-field match.
- Rendered text vs JSON file: chr ratio (json_file / rendered_text) should be >= 1.0 (JSON has formatting overhead). Ratio < 1.0 indicates missing content.
- Figure/table mention counts: rendered vs JSON main_text.
- Section headers: rendered vs JSON main_text.

main_text boundary rules (define both parsing logic and verification criteria; use rendered HTML text as reference):
- Body sections: keep everything from abstract to before the first references section.
- Supplementary materials: search after the first references section for supplementary content (sections matching "supplement", "extended data", "source data", "expanded view", "powerpoint", "appendix"). Append to main_text.
- Remove all references sections from main_text.

Both refs.json and rendered text are references for verification only. Their values must not be copied into the JSON output. The actual HTML-to-JSON conversion uses script-based parsing (regex on HTML structure) for precise field-level accuracy.

Evaluation criteria for successful parser development:
- Chr ratio (json_file / rendered_text) >= 1.0 for all papers with main_text.
- Reference count: json references = rendered reference count.
- Content completeness (every item in rendered text must appear in JSON):
  - Metadata (in refs.json-format keys): title, journal, volume, issue, year, pages, doi, authors.
  - "references" key: references + supplementary references.
  - "main_text" key: from abstract to before the first references section + supplementary materials

Bootstrap process for a new publisher (requires at least 30 papers in `papers/<second_level_domain>/`; if fewer, confirm with user before proceeding):
1. Copy HTMLs from papers/ into `papers_test/<second_level_domain>/` subfolders by second-level domain extracted from the SingleFile URL comment. Replace punctuation with `_` in folder names. papers/ is not modified.
2. Examine several htmls from `papers_test/<second_level_domain>/` as starting example.
3. Extract rendered text from the htmls as content reference.
4. Ask the user to identify visually impairing elements (cookie banners, consent overlays, login modals) for remove_banners. Do not assume what to remove.
5. Implement parse_article, verifying each field against refs.json and the rendered text.
6.  Run convert_html.py on the full `papers_test/<second_level_domain>/` directory.
7.  Sample 3 papers. Spawn one agent per paper to extract text from rendered html and inspect converted json verify all content is correctly filled in.
8.  If issues are detected, implement fixes, reset the test folder (see below), run convert_html.py, and spawn agents to verify.
9.  Write main_text to `papers_test/<second_level_domain>/<stem>.md` for user inspection.

## Resetting Test Folders

convert_html.py mutates HTMLs in papers_test/ (banner removal) and writes JSON alongside. To re-run a parser from a clean state:
- Single domain: `rm -rf papers_test/<domain>/ && unzip -q papers_test_ref/<domain>.zip -d papers_test/<domain>/`.
- All domains: `rm -rf papers_test/ && for z in papers_test_ref/*.zip; do d=$(basename "$z" .zip); unzip -q "$z" -d "papers_test/$d/"; done`.

papers_test_ref/ is the reference snapshot and must not be modified during parser development. If new test HTMLs are added to papers_test/, re-zip the affected subfolder into papers_test_ref/ so future resets pick them up.


## Deleting Citations

For each step below, wait for user confirmation before executing. Always run in the background to free up the chat.
1. Identify PMIDs from a user-specified source.
2. Run `python get_refs.py --delete <pmid> [<pmid> ...]`. DO NOT delete files from papers/ unless explicitly asked to.
3. Run `python search_refs.py --build`.

## Searching for Information

1. Semantically enrich the user's query before searching. Expand abbreviations (e.g., TERT = telomerase reverse transcriptase), add synonyms (e.g., catalytic subunit), related terms (e.g., TERC, telomerase), and potential answer terms. Format the enriched query as a single string.
2. Run `python search_refs.py <query>`. List all the papers from the output and their similarity score.
3. Read papers/*.json main_text of top candidates.
4. Cite sources using `<stem>` when referencing specific findings. The user will run convert_citation.py to convert stems to readable citations.
