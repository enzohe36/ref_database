---
name: convert-pdf
description: Convert papers/raw/<stem>.pdf into a high-quality update of papers/raw/<stem>_converted.json. Use when the html-derived _converted.json has poor or missing main_text and a manually retrieved PDF is available as the fallback source. Spawned in parallel by the user when many PDFs need processing; one agent invocation per stem.
---

## When to use

Use this skill only when:
1. `papers/raw/<stem>.pdf` exists (manually retrieved by the user).
2. `papers/raw/<stem>_converted.json` already exists. If it does not, abort and ask the user to run `python scripts/convert_html.py` first to create the schema scaffold. **Do not create the JSON from scratch.**

This skill operates as an UPDATE — it fills in `authors[].affiliation`, `main_text`, and `references` keys of the existing JSON in place. All other top-level keys (`stem`, `pmid`, `doi`, `title`, `journal`, `year`, `volume`, `issue`, `pages`, `publication_types`) are PRESERVED, not overwritten.

## Workflow per stem

You will be invoked with one stem to process. `<stem>`, `<dir>`, and `<pmid>` are the placeholders the spawning context fills in. `<dir>` is the absolute path to `papers/raw/`. The skill assumes `<dir>/<stem>.pdf` and `<dir>/<stem>_converted.json` both exist.

### Step 1: convert PDF to a working markdown

Run the bundled script:

    python .claude/skills/convert-pdf/convert_pdf.py <dir>/<stem>.pdf

The script writes `<dir>/<stem>.md`. This is a working file only — it is deleted at the end of step 3 and is not part of the canonical state.

### Step 2: clean the markdown

Rearrange text fragments in `<dir>/<stem>.md` so they appear in the same order as `<dir>/<stem>.pdf`. Then clean as follows. Write the cleaned version to `/tmp/<stem>.md`.

Keep these sections:
- Paper/section titles.
- Author names.
- Author affiliations (no author contributions or correspondence).
- Abstract.
- Keywords.
- Abbreviations.
- Body sections (introduction, methods, results, discussion, or any other section containing the main content of the paper).
- Tables.
- Figure/table captions.
- References.
- Supplementary materials (methods, tables, captions, references etc.).

Delete these sections:
- Adjacent articles in the same journal issue.
- Front and back covers.
- Everything before paper title (journal info, logos, article type labels etc.).
- Author contributions and correspondence.
- Front matter (article history, copyright, ISSN, DOI etc.).
- Page margins (headers, footers, page numbers, watermarks etc.).
- Picture artifacts (placeholders, garbled text).
- Boilerplates (acknowledgements, funding, conflict of interest, data availability, license information etc.).

Fix formatting:
- Format paper/section titles as `##` headers.
- If there are author affiliation labels: format them as `<text> (<label>,<label>)` (space after `<text>`, no space after comma); format the author affiliations section as `<label>. <text>.` (dot and space after `<label>`, dot after `<text>`, one entry per line); delete labels for author contributions and correspondence.
- If there are numbered in-text citations: format them using the same style as author affiliation labels above; format the references section using the same style as the author affiliations section above.
- Format other superscripts and subscripts as plain text enclosed in `[]`.
- Format tables as `Column1: Value1. Column2: Value2.` (one line per row).
- Remove markups (bold `**`, italic `*` or `_`, strikethrough `~~` etc.).
- Remove blockquote markers (`>`).
- Remove HTML tags (`<br>` etc.).
- Collapse multiple blank lines to a single blank line.

Replace special characters:
- Greek letters → spelled-out words, capitalized for uppercase (e.g. α → alpha, Δ → Delta).
- Dashes, hyphens, minus sign → ASCII hyphen-minus.
- Quotes, primes, backtick → ASCII single/double quotes.
- Ligatures → ASCII letters.
- Math italic/bold/script fonts → ASCII letters.
- Latin diacritics → ASCII letters.
- Math symbols → ASCII equivalents (if any) or spelled-out words.
- All other non-ASCII characters → ASCII equivalents based on the pdf.

### Step 3: update papers/raw/<stem>_converted.json in place

Update three keys in `<dir>/<stem>_converted.json`. Use the Edit tool to modify each key individually. **DO NOT rewrite the whole file. DO NOT touch any other key.**

For the `authors` key (already populated with PubMed authors from convert_html.py):
- For each entry in the existing `authors` array whose `affiliation` is `[]`, find the matching author in `/tmp/<stem>.md` (by surname) and set `affiliation` to a list of strings copied verbatim from the md.
- Do not add or remove authors.

For the `main_text` key:
- Replace its value with the cleaned body of `/tmp/<stem>.md`. Use the boundary rules below.

main_text boundary rules:
- Body sections: keep everything from abstract to before the first references section.
- Supplementary materials: search after the first references section for supplementary content (sections matching "supplement", "extended data", "source data", "expanded view", "powerpoint", "appendix"). Append to main_text.
- Remove all references sections from main_text.

For the `references` key:
- Replace its value with an array of structured reference objects. Each object has these keys in this order:

```
{
  "pmid": "",
  "doi": "...",
  "title": "...",
  "journal": "...",
  "year": "...",
  "volume": "...",
  "issue": "...",
  "pages": "...",
  "authors": ["LastName IN", ...]
}
```

- Extract from the references section in `/tmp/<stem>.md`. Include supplementary references too.
- Leave `pmid` as the empty string `""` for every entry. PMIDs are filled later by `python scripts/get_pmids.py` only if the user opts in to adding these references to the database (it is not part of the default workflow).
- Empty bib fields are `""`. Empty `authors` is `[]`.

### Step 4: cleanup

Delete the working markdown files:

    rm -f <dir>/<stem>.md /tmp/<stem>.md

Do not write any markdown file as a persistent output. The only artifact this skill produces is the in-place update to `<dir>/<stem>_converted.json`.
