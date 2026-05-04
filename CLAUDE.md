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
- DO NOT write to the auto-memory system unless the user explicitly confirms. Do not save user/feedback/project/reference memories on your own initiative.

## File Structure

- `<stem>`: `<first_author_last_name>_<year>_<journal>_<pmid>` with Latin diacritics converted to ASCII, punctuation and spaces to `_`, collapsed.
- `<citation_in_text>`: derived from stem at conversion time. "LastName YYYY" (1 author) / "LastName & LastName YYYY" (2) / "LastName et al. YYYY" (3+).
- `<citation>`: `<authors>. <title>. <journal>. <year>;<volume>(<issue>):<pages>. PMID: <pmid>.` Used in the references section when drafting documents.
- chroma_db/: semantic search index (single directory, multiple named collections). One collection per project plus `_global`. Built by build_model.py from papers/parsed/*.json.
- scripts/html_parsers/: Python package with per-publisher HTML parsing modules. Module names are second-level domains.
- NLM journal list (NlmId -> {JournalTitle, MedAbbr}) is downloaded fresh in memory each time convert_html.py runs. No on-disk cache.
- papers/: split into three subdirectories.
  - papers/parsed/<stem>.json: source of truth per paper. Schema (locked key order): stem, pmid, doi, title, journal, year, volume, issue, pages, authors (array of {author, affiliation[]}), publication_types, main_text, references (flat PMID list).
  - papers/raw/<stem>.html: raw or banner-cleaned HTML downloaded by get_html.py.
  - papers/raw/<stem>.pdf: original PDF (manually retrieved fallback when HTML is insufficient).
  - papers/raw/<stem>_converted.json: structured output of HTML/PDF conversion. Same top-level keys as papers/parsed/<stem>.json (stem, pmid, publication_types are always empty placeholders), but references is an array of structured reference objects: {pmid, doi, title, journal, year, volume, issue, pages, authors[]} where authors is a flat list of "LastName IN" strings.
  - papers/test/<stem>.{html,pdf,_converted.json}: working copies for parser/agent prompt development. Modified in place by convert_html.py when invoked on a specific test HTML file.
- projects/<name>/: per-project workspace.
  - projects/<name>/pmids.txt: space- or newline-separated PMIDs that belong to this project.
  - projects/<name>/drafts/, factcheck/, etc.: existing per-project document files.

## Scripts

- `python scripts/get_refs.py <pmid|list> ...`: fetches PubMed metadata for each PMID and writes papers/parsed/<stem>.json. Skips PMIDs whose parsed JSON already exists. A list arg is a file containing PMIDs separated by spaces or newlines.
- `python scripts/get_html.py <pmid|url|list> ...`: fetches full-text HTML via Edge + single-file. For PMID args: read DOI from papers/parsed/<stem>.json, save to papers/raw/<stem>.html. For URL args: fetch directly, save to papers/raw/<url_name>.html.
- `python scripts/convert_html.py [<pmid|html|list> ...]`: parses HTML using scripts/html_parsers/ and writes papers/raw/<stem>_converted.json. No args: scans papers/raw/ for *.html files lacking a corresponding _converted.json. PMID args locate papers/raw/<stem>.html via parsed/<stem>.json. HTML file args produce _converted.json next to the input HTML (so files in papers/test/ produce output in papers/test/).
- `python scripts/get_pmids.py [<pmid|json|list> ...]`: walks JSON files recursively and resolves every empty `pmid` field via PubMed, using the sibling bibliographic fields in the same dict (DOI shortcut + iterative relaxation, with PublicationType disambiguation when multiple matches return). On a `_converted.json` this resolves both the main-paper top-level `pmid` and each `references[i].pmid`. Sequential, PubMed rate-limited; writes back incrementally. No args: every papers/raw/<stem>_converted.json on disk. PMID args resolve to papers/raw/<stem>_converted.json. JSON args are processed directly and need not live under papers/raw/. A list arg is a file containing PMIDs and/or JSON paths separated by spaces or newlines.
- `python scripts/merge_refs.py [<pmid|list> ...]`: merges papers/raw/<stem>_converted.json into papers/parsed/<stem>.json (parallel). Fills empty author affiliations, replaces main_text when it qualifies, unions references PMIDs. No args: every parsed/<stem>.json with a corresponding _converted.json. A list arg is a file containing PMIDs. Run get_pmids.py first if reference PMIDs need resolving (optional; see workflow note).
- `python scripts/build_model.py [<project_name> ...]`: builds the embedding model. No args: rebuild the `_global` chroma collection over every papers/parsed/<stem>.json with non-empty main_text. With project args: rebuild each named project's collection from PMIDs in projects/<name>/pmids.txt.
- `python scripts/search_refs.py "<query>"`: searches the embedding model. Project resolution is cwd-based: cwd inside projects/<name>/ queries that project's collection; cwd elsewhere queries the `_global` collection.
- `python scripts/cite_refs.py <document>`: converts in-text stems in a document to "Author YYYY" form, detects/creates a "References" section, adds full citations, sorts all entries alphabetically, and auto-appends every cited PMID to the current project's pmids.txt. Project is resolved from cwd; errors out if not run from inside a projects/<name>/ subtree.

## Literature Search

- You MUST use PubMed E-utilities (esearch.fcgi) to search for papers unless explicitly asked to.

## Skills

- `/develop-parser`: parser contract, file layout, verification criteria, and bootstrap process for scripts/html_parsers/ modules. Auto-activates when editing files under scripts/html_parsers/ or papers/test/.
- `/convert-pdf`: invoked per stem (typically in parallel) to update papers/raw/<stem>_converted.json from a manually retrieved papers/raw/<stem>.pdf. Verifies _converted.json exists; updates authors[].affiliation, main_text, and references in place.
- `/check-fact`: fact-check a finished review draft and add inline citations from the local corpus. One agent per paragraph runs in parallel; conclusion runs in a second wave restricted to body-cited stems. Outputs <draft_stem>.cited.md, then run `python scripts/cite_refs.py` to convert stems and emit the References section. Auto-activates on drafts under projects/<name>/.

## Workflow

1. PubMed search (when requested): form query, run via E-utilities `esearch`, retrieve PMIDs.
2. Metadata fetch: `python scripts/get_refs.py <pmids>` creates papers/parsed/<stem>.json for each new PMID.
3. Triage by title + abstract + keywords (already in parsed/<stem>.json) to decide which papers need full-text-quality main_text.
4. For those papers: `python scripts/get_html.py <pmids>` → `python scripts/convert_html.py <pmids>` → if main_text quality is poor, retrieve PDF manually and run /convert-pdf in parallel agents → `python scripts/merge_refs.py <pmids>`. Skip `get_pmids.py` by default — only run it for papers whose cited references should be added to the database (it resolves PMIDs in `_converted.json` so the references can land in `parsed/<stem>.json` after the next merge_refs.py).
5. Build embedding model: `python scripts/build_model.py [<project>]`.
6. Local search (when requested): `python scripts/search_refs.py "<query>"` (cwd inside a project for project-scoped, cwd outside for global).

## Searching for Information

1. Semantically enrich the user's query before searching. Expand abbreviations (e.g., TERT = telomerase reverse transcriptase), add synonyms (e.g., catalytic subunit), related terms (e.g., TERC, telomerase), and potential answer terms. Format the enriched query as a single string.
2. Run `python scripts/search_refs.py "<query>"`. Output is a JSON array of {pmid, stem, score, snippet} for the top papers.
3. Triage by snippet: drop papers whose snippet is clearly off-topic before reading further.
4. Read papers/parsed/<stem>.json main_text of the remaining candidates. DO NOT cite from the snippet alone, since a 400-word window may drop qualifiers that change a finding's meaning.
5. Cite sources using `<stem>` when referencing specific findings. The user will run cite_refs.py to convert stems to readable citations.
