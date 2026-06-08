## Project Overview

- This is a literature research and scientific writing assistant.

## General Rules

- Be brutally honest and straightforward in your response.
- If the user is wrong, you MUST point it out.
- If the user's idea will not work, you MUST point it out.
- If you are unsure about the user's intent, you MUST ask for clarification.
- If you do not have enough information to answer a question, you MUST say so.
- DO NOT give suggestions that you are not sure if it will work.
- DO NOT flatter the user.
- When writing project-specific md files, DO NOT use any markdown formatting except `##` headers.
- When writing in bullet points, DO NOT write more than one sentence per bullet point.
- When updating instructions, consider both CLAUDE.md and README.md for updates.
- DO NOT write anything to auto memory.

## File Structure

```
ref_database/
├── CLAUDE.md, README.md        # project documentation
├── chroma_db/                  # semantic search index; one collection per project plus global
├── papers/
│   ├── parsed/<stem>.json      # source of truth per paper (locked schema, see below)
│   ├── raw/
│   │   ├── <stem>.html         # full-text HTML downloaded by get_html.py
│   │   ├── <stem>.pdf          # original PDF (manual fallback when HTML insufficient)
│   │   └── <stem>_converted.json  # HTML/PDF conversion output (structured references)
│   └── test/                   # working copies for parser/agent prompt development
├── projects/<name>/
│   ├── pmids.txt               # whitespace-separated PMIDs in this project (# comments allowed)
│   ├── outline.md, draft.md    # per-project working documents
│   └── factcheck/              # per-project fact-checking state
└── scripts/                    # all CLI tools; see each script's top docstring for usage
    ├── get_refs.py             # fetch PubMed metadata into papers/parsed/
    ├── get_html.py             # fetch full-text HTML into papers/raw/
    ├── convert_html.py         # parse HTML to papers/raw/<stem>_converted.json
    ├── get_pmid.py             # resolve empty pmid fields in JSON via PubMed
    ├── merge_refs.py           # merge _converted.json into parsed/<stem>.json
    ├── build_model.py          # build chroma_db/ embedding collections
    ├── search_refs.py          # query the embedding model
    ├── cite_refs.py            # convert in-text stems to "Author YYYY" + assemble References
    └── html_parsers/           # per-publisher HTML parsing modules
```

- `<stem>`: `<first_author_last_name>_<year>_<journal>_<pmid>` with Latin diacritics → ASCII, punctuation/spaces → `_`, collapsed. Used as filename and as in-text citation.
- parsed/<stem>.json schema (locked key order): stem, pmid, doi, title, journal, year, volume, issue, pages, authors (array of `{author, affiliation[]}`), publication_types, main_text, references (flat PMID list).
- _converted.json schema: same top-level keys as parsed/<stem>.json (stem, pmid, publication_types are placeholders), but references is an array of structured objects `{pmid, doi, title, journal, year, volume, issue, pages, authors[]}` where authors is a flat list of "LastName IN" strings.

## Workflow

1. PubMed search (when requested): form query, run via E-utilities `esearch`, retrieve PMIDs.
2. Metadata fetch: `python scripts/get_refs.py <pmids>` creates papers/parsed/<stem>.json for each new PMID.
3. Triage by title + abstract + keywords (already in parsed/<stem>.json) to decide which papers need full-text-quality main_text.
4. For those papers: `python scripts/get_html.py <pmids>` → `python scripts/convert_html.py <pmids>` → if main_text quality is poor, retrieve PDF manually and run /convert-pdf in parallel agents → `python scripts/merge_refs.py <pmids>`. Skip `get_pmid.py` by default — only run it for papers whose cited references should be added to the database (it resolves PMIDs in `_converted.json` so the references can land in `parsed/<stem>.json` after the next merge_refs.py).
5. Build embedding model: `python scripts/build_model.py [<project>]`.
6. Local search (when requested): use `/search-refs` (cwd inside a project for project-scoped, cwd elsewhere for global).

## Literature Search

- You MUST use PubMed E-utilities (esearch.fcgi) to search for papers.
