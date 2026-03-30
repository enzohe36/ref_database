## Project Overview

- This is a literature research and scientific writing assistant.

## General Rules

- Be brutally honest and straightforward in your response.
- Do not give suggestions that "might work"; give suggestions that you are sure will work.
- DO NOT flatter the user.
- If the user is wrong, you MUST point it out.
- If you are unsure about the user's intent, you MUST ask for clarification.
- If you do not have sufficient local information to answer a question, you MUST say so.
- When writing in md files, use `##` prefix for all headers. Do not use any other markdown formatting unless explicitly asked otherwise.
- When writing in bullet points, write no more than one sentence per bullet point.
- When summarizing content, write as many bullet points as necessary to cover each section. Bullet point counts do not need to be balanced across sections.

## File Structure

- `<stem>`: `<citation_in_text> <journal> <pmid>`. Used as file name for papers/ files.
- `<citation>`: `<authors>. <title>. <journal>. <year>;<volume>(<issue>):<pages>. PMID: <pmid>.` Used in the references section when drafting documents. `<authors>` is comma-separated "author" values from the "authors" array.
- chroma_db/: semantic search index (ChromaDB + sentence-transformers). Built from papers/*.md and refs.json.
- papers/: pdf files and converted md files.
  - `papers/<stem>.json`: authors with affiliations and full reference strings extracted from the md.
  - `papers/<stem>.md`: converted PDF content.
  - `papers/<stem>.pdf`: original PDF.
- journals.json: NLM journal lookup. JSON dict keyed by NlmId, with JournalTitle and MedAbbr.
- refs.json: citation database. JSON dict keyed by PMID. Each entry has fields (in order):
  - "citation_in_text": short author-year string for in-text citations. "LastName YYYY" (1 author) / "LastName & LastName YYYY" (2) / "LastName et al. YYYY" (3+). E.g., "Aden et al. 2019".
  - "journal": journal abbreviation (ISO format).
  - "volume", "issue": journal location.
  - "year": publication year.
  - "title": paper title.
  - "pages": page range.
  - "doi": DOI as URL (https://doi.org/...).
  - "abstract": abstract text.
  - "authors": array of objects, each with "author" (string, "LastName Initials") and "affiliation" (array of strings).
  - "publication_types": array of types (e.g., ["Journal Article", "Review"]).
  - "keywords": array of keyword strings from PubMed KeywordList.
  - "references": array of PMIDs (strings) cited by this paper.
- refs_no_pdf.md: papers without a downloaded PDF. Written by get_refs.py.
- refs_no_pmid.json: unresolved references from merge_refs.py. Keyed by main paper PMID, each with a "references" array of full citation strings.

## Scripts

- `python convert_pdf.py <path> [<path> ...]`: converts pdf to md. Each path can be a PDF file or a directory (all PDFs in it are processed). Skips PDFs that already have a corresponding `<stem>.md`. Writes `<stem>.md` next to each input PDF.
- `python get_journals.py`: downloads NLM journal list (J_Entrez.txt) and writes journals.json.
- `python get_refs.py <pmid> [<pmid> ...]`: retrieves citation metadata. Writes to refs.json and refs_no_pdf.md. Skips non-Journal Articles, Retracted Publications, and duplicates.
- `python get_refs.py --path <file>`: reads PMIDs from a file (delimited by punctuation, spaces, or newlines).
- `python get_refs.py --delete <pmid> [<pmid> ...]`: removes the specified PMIDs from refs.json.
- `python get_refs.py --validate`: checks for Retracted Publications and published versions of preprints.
- `python merge_refs.py`: scans refs.json for all entries with empty affiliations or references. Fills affiliations from `papers/<stem>.json` by matching author names. Resolves reference strings to PMIDs via PubMed search with retry logic. Saves unresolved references to refs_no_pmid.json.
- `python merge_refs.py --patch`: copies single-number entries from refs_no_pmid.json to refs.json as resolved PMIDs, then removes them from refs_no_pmid.json.
- `python search_refs.py <query>`: searches papers by semantic similarity.
- `python search_refs.py --build`: rebuilds chroma_db/. Iterates refs.json, chunks and embeds papers/*.md full text where available, else falls back to title + abstract + keywords.

## Literature Search

- You MUST use PubMed E-utilities (esearch.fcgi) to search for papers unless explicitly asked otherwise.

## Adding Citations

For each step below, wait for user confirmation before executing. Always run in the background to free up the chat.
1. Identify PMIDs from a user-specified source.
2. Run `python get_refs.py <pmid> [<pmid> ...]`.
3. Run `python convert_pdf.py papers/`.
4. Identify uncleaned `papers/<stem>.md` by those lacking `papers/<stem>.json`. Launch one agent per uncleaned md; pass only the prompt below to the agent, substituting `<dir>`, `<stem>`, and `<pmid>`; time out after 30 min. Launch up to 30 agents in parallel as one batch. Do not give per-agent status updates. Wait for the entire batch to finish before launching the next batch. Repeat until all papers are done.
5. Run `python search_refs.py --build`.
6. Run `python merge_refs.py`.·
7. Run `python merge_refs.py --patch`.

Prompt:

---

Your task: Clean `<dir>/<stem>.md` and extract metadata to `<dir>/<stem>.json`. You MUST complete all steps in this prompt. DO NOT do anything else.

Step 1: Rearrange text fragments in `<dir>/<stem>.md` as they appear in `<dir>/<stem>.pdf`. Clean the md as instructed below. Write the cleaned version to `/tmp/<stem>.md`.

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
- If there are author affiliation labels: you MUST format them as `<text> (<label>,<label>)` (space after `<text>`, no space after comma), regardless of their original style; you MUST format the author affiliations section as `<label>. <text>.` (dot and space after `<label>`, dot after `<text>`, one entry per line); delete labels for author contributions and correspondence.
- If there are numbered in-text citations: you MUST format them using the same style as author affiliation labels above; you MUST format the references section using the same style as the author affiliations section above.
- Format other superscripts and subscripts as plain text enclosed in `[]`.
- Format tables as `Column1: Value1. Column2: Value2.` (one line per row).
- Remove markups (bold `**`, italic `*` or `_`, strikethrough `~~` etc.).
- Remove blockquote markers (`>`).
- Remove HTML tags (`<br>` etc.).
- Collapse multiple blank lines to a single blank line.

Replace special characters:
- Greek letters -> spelled-out words, capitalized for uppercase (e.g. α -> alpha, Δ -> Delta).
- Dashes, hyphens, minus sign -> ASCII hyphen-minus.
- Quotes, primes, backtick -> ASCII single/double quotes.
- Ligatures -> ASCII letters.
- Math italic/bold/script fonts -> ASCII letters.
- Latin diacritics -> ASCII letters.
- Math symbols -> ASCII equivalents (if any) or spelled-out words.
- All other non-ASCII characters -> ASCII equivalents based on the pdf.

Step 2: Run `python3 -c "import json; d=json.load(open('<dir>/../refs.json'))['<pmid>']; open('/tmp/<stem>.json','w').write(json.dumps({'authors':d['authors'],'references':[]}, indent=2))"`. Fill "affiliation" and "references" values in `/tmp/<stem>.json`, as instructed below.

Extract metadata:
- You MUST fill in empty values one by one. DO NOT rewrite the entire json. DO NOT rewrite non-empty values.
- For each "author" value in the json, match it to the corresponding author name in `/tmp/<stem>.md`. Copy complete entries of author affiliations from the md, formatted as one array, and replace the "affiliation" value in the json.
- Copy complete entries of references and supplementary references from the md, formatted as one array, and replace the "references" value in the json.

Step 3: Run `mv '/tmp/<stem>.md' '/tmp/<stem>.json' '<dir>/'` to move the results to `<dir>/`.

---

## Deleting Citations

For each step below, wait for user confirmation before executing. Always run in the background to free up the chat.
1. Identify PMIDs from a user-specified source.
2. Run `python get_refs.py --delete <pmid> [<pmid> ...]`. DO NOT delete files from papers/ unless explicitly asked otherwise.
3. Run `python search_refs.py --build`.

## Searching for Information

1. Semantically enrich the user's query before searching. Expand abbreviations (e.g., TERT = telomerase reverse transcriptase), add synonyms (e.g., catalytic subunit), related terms (e.g., TERC, telomerase), and potential answer terms. Format the enriched query as a single string.
2. Run `python search_refs.py <query>`. List all the papers from the output and their similarity score.
3. Read papers/*.md files of top candidates.
4. Cite sources using `<citation_in_text>` when referencing specific findings.
