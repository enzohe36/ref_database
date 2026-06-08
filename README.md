## Project Overview

- This is a literature research and scientific writing assistant.

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

## Example Workflow

1. Find papers via PubMed (esearch or the PubMed website) and collect their PMIDs.
2. Pull metadata: `python scripts/get_refs.py <pmids>`. Creates `papers/parsed/<stem>.json` with title, abstract, authors, journal, etc. for each new PMID.
3. Read the parsed JSONs and decide which papers need full text.
4. For those papers, retrieve and convert the full text:
   - `python scripts/get_html.py <pmids>` saves the article HTML to `papers/raw/<stem>.html`.
   - `python scripts/convert_html.py <pmids>` extracts structured content into `papers/raw/<stem>_converted.json`.
   - If the converted `main_text` is missing or too short, manually download the PDF to `papers/raw/<stem>.pdf` and ask Claude to run `/convert-pdf`.
   - `python scripts/merge_refs.py <pmids>` merges the converted output back into `papers/parsed/<stem>.json`.
5. Optional: to also track each paper's cited references in the corpus, run `python scripts/get_pmid.py <pmid>` (resolves the PMIDs of the references in `_converted.json`), then `python scripts/merge_refs.py <pmid>` again to push them into `papers/parsed/<stem>.json`.
6. Rebuild the embedding model whenever new papers land: `python scripts/build_model.py` (global) or `python scripts/build_model.py <project>` (project-scoped, reads PMIDs from `projects/<name>/pmids.txt`).
7. Search the corpus: `python scripts/search_refs.py "<query>"`. Run from inside `projects/<name>/` to scope to that project's collection, or anywhere else to search `global`. Asking Claude a literature question auto-invokes `/search-refs` for the same effect.
8. Drafting inside `projects/<name>/` uses three skills in order: `/refine-outline` to iteratively refine `outline.md` against retrieved evidence; `/write-draft` to compile the outline into prose `draft.md`; `/check-fact` to fact-check the draft and add inline stem citations. Finish with `python scripts/cite_refs.py projects/<name>/<draft>.cited.md` to convert the stems to "Author YYYY" form and assemble the References section.

## Example Script Usage

Each script's top docstring documents its full behavior. Below are the supported invocation modes.

For `get_refs.py`, `get_html.py`, `convert_html.py`, `get_pmid.py`, and `merge_refs.py`, a `<list>` arg is a path to a text file containing whitespace-separated PMIDs and/or URLs/JSON paths; lines whose first non-whitespace character is `#` are ignored.

### get_refs.py

fetch PubMed metadata into papers/parsed/<stem>.json.

```
python scripts/get_refs.py <pmid> [<pmid> ...]   # one or more PMIDs
python scripts/get_refs.py <list>                # list file
```

### get_html.py

fetch full-text HTML into papers/raw/.

```
python scripts/get_html.py <pmid> [...]          # by PMID; reads DOI from papers/parsed/<stem>.json
python scripts/get_html.py <url> [...]           # direct URL; saved as papers/raw/<url_name>.html
python scripts/get_html.py <list>                # list file mixing PMIDs and URLs
```

### convert_html.py

parse HTML into papers/raw/<stem>_converted.json.

```
python scripts/convert_html.py                   # no args: convert every HTML lacking a _converted.json
python scripts/convert_html.py <pmid> [...]      # by PMID
python scripts/convert_html.py <html_path> [...] # by file path; output sits next to input (works for papers/test/)
python scripts/convert_html.py <list>            # list file
```

### get_pmid.py

resolve empty pmid fields in JSON via PubMed.

```
python scripts/get_pmid.py                       # no args: walk every papers/raw/*_converted.json
python scripts/get_pmid.py <pmid> [...]          # by PMID (resolves the matching _converted.json)
python scripts/get_pmid.py <json_path> [...]     # arbitrary JSON file path
python scripts/get_pmid.py <list>                # list file mixing PMIDs and JSON paths
```

### merge_refs.py

merge _converted.json into papers/parsed/<stem>.json.

```
python scripts/merge_refs.py                     # no args: merge every parsed/<stem>.json with a converted sibling
python scripts/merge_refs.py <pmid> [...]        # by PMID
python scripts/merge_refs.py <list>              # list file
```

### build_model.py

build the chroma_db/ embedding collections.

```
python scripts/build_model.py                    # no args: rebuild the global collection
python scripts/build_model.py <project> [...]    # rebuild named project collection(s) from projects/<name>/pmids.txt
```

### search_refs.py

semantic search over the embedding model.

```
python scripts/search_refs.py "<query>"          # query the collection resolved from cwd:
                                                 #   inside projects/<name>/  -> that project's collection
                                                 #   elsewhere                -> the global collection
```

### cite_refs.py

convert in-text stems and assemble the References section.

```
python scripts/cite_refs.py <document.md>              # Author-Year mode (default):
                                                       #   stems -> "Author YYYY";
                                                       #   References as alphabetical
                                                       #   plain paragraphs.
python scripts/cite_refs.py --numbered <document.md>   # Numeric mode:
                                                       #   stems -> [1], [2], ...
                                                       #   in first-appearance order;
                                                       #   References as numbered list.
                                                       #   Brackets and `; ` separators
                                                       #   preserved verbatim.
```

Runs from anywhere; no project context needed.
