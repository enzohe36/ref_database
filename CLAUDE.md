## Project Overview

- This is a literature research and scientific writing assistant.

## General Rules

- Be brutally honest and straightforward in your response.
- Do not give suggestions that "might work"; give suggestions that you are sure will work.
- If the user is wrong, you must point it out.
- If you are unsure about the user's intent, you must ask for clarification.
- If you do not have sufficient local information to answer a question, you must say so.
- Do not access the internet unless explicitly asked.
- When writing in md files, use ## prefix for all headers. Do not use any other markdown-specific formatting.
- When writing in bullet points, write no more than one sentence per bullet point.
- When summarizing content, write as many bullet points as necessary to cover each section. Bullet point counts do not need to be balanced across sections.

## File Structure

- refs.json is the citation database. JSON dict keyed by PMID. Each entry has fields (in order):
  - "citation_in_text": short author-year string for in-text citations. "LastName YYYY" (1 author) / "LastName & LastName YYYY" (2) / "LastName et al. YYYY" (3+). E.g., "Aden et al. 2019".
  - "journal": journal abbreviation (ISO format).
  - "volume", "issue": journal location. May be empty.
  - "year": publication year.
  - "title": paper title.
  - "pages": page range. May be empty.
  - "doi": DOI as URL (https://doi.org/...).
  - "abstract": abstract text.
  - "authors": array of objects, each with "author" (string, "LastName Initials") and "affiliation" (array of strings).
  - "publication_types": array of types (e.g., ["Journal Article", "Review"]).
  - "keywords": array of keyword strings from PubMed KeywordList.
  - "references": array of PMIDs (strings) cited by this paper.
- [stem] = "[citation_in_text] [journal] [pmid]". Used as file name for papers/ files.
- [citation] = "[authors]. [title]. [journal]. [year];[volume]([issue]):[pages]. PMID: [pmid]." [authors] is the comma-separated "author" values from the "authors" array in refs.json; others are the same keys as in refs.json.
- papers/ stores pdf files and converted md files.
- papers/[stem].md contains the converted PDF content.
- chroma_db/ is the semantic search index (ChromaDB + sentence-transformers). Built from papers/*.md and refs.json.
- projects/ contains project-specific files (aims, drafts, notes).

## Scripts

- `python get_citation.py [pmid] [[pmid] ...]`: retrieves citation metadata. Writes to refs.json and temp_refs.md. Skips non-Journal Articles, Retracted Publications, and duplicates.
- `python get_citation.py --validate`: checks for Retracted Publications and published versions of preprints.
- `conda run -n py312 python convert_pdf.py [[stem].pdf] [[[stem].pdf] ...]`: converts pdf to md. Writes to [stem].md in the same directory as the pdf.
- `conda run -n py312 python search_refs.py [query]`: searches papers by semantic similarity.
- `conda run -n py312 python search_refs.py --build`: rebuild chroma_db/. Iterates refs.json, chunks and embeds papers/*.md full text where available, else falls back to title + abstract + keywords.
- `python merge_refs.py [stem].json [[stem].json ...]`: merges authors and references from [stem].json files into refs.json. Replaces authors directly; resolves reference strings to PMIDs via PubMed search.

## Literature Search

- Use PubMed E-utilities (esearch.fcgi) to search for papers.
- Prioritize papers that are highly cited, recent (2021-2026), or from prestigious journals (Nature, Cell, Science etc.).

## Adding Citations

1. Extract PMIDs from a user-specified source. If the source is a comma-separated list, parse it into individual PMIDs first. Pass each PMID as a separate argument to get_citation.py.
2. Convert user-specified pdfs to mds. Run in the background.
3. Identify uncleaned papers/[stem].pdf by those lacking papers/[stem].json. Launch one agent to clean each paper; pass only the prompt below to the agent, substituting [dir], [stem], and [pmid]. Launch up to 5 agents in parallel. Run in the background. When one agent finishes, spawn a new one to clean the next paper. Repeat until all papers are done or you hit a usage limit.
4. Rebuild chroma_db/.
5. Merge authors and references from all [stem].json files into refs.json.

Prompt:
---
Your task: Clean [dir]/[stem].md and extract metadata to [dir]/[stem].json. You MUST complete all steps in this prompt in exactly the listed order. DO NOT do anything not specified in this prompt.

Step 1: Rearrange text fragments in [dir]/[stem].md to match the order in [dir]/[stem].pdf. Clean the md as instructed below. Write the clean version to /tmp/[stem].md.

Keep these sections:
- Paper title.
- Author names.
- Author affiliations (no author contributions or correspondence).
- Abstract.
- Keywords.
- Abbreviations.
- Body sections (introduction, methods, results, discussion, or any other section containing the main content of the paper).
- Tables.
- Figure/table captions.
- References.
- Supplementary methods, tables, captions, references.

Delete these sections:
- Adjacent articles in the same journal issue.
- Front and back covers.
- Everything before title (journal info, logos, article type labels etc.).
- Author contributions and correspondence.
- Front matter (article history, copyright, ISSN, DOI etc.).
- Page margins (headers, footers, page numbers, watermarks etc.).
- Picture artifacts (placeholders, garbled text).
- Boilerplates (acknowledgements, funding, conflict of interest, data availability, license information etc.).

Fix formatting:
- Format paper/section titles as ## headers.
- Format author affiliation labels and numbered in-text citations as "[text] ([label],[label])" (space after [text], no space after comma), regardless of their original style. Remove labels for author contributions and correspondence.
- If there are author affiliation labels, you MUST format the author affiliations section as a numbered or lettered list, depending on the style of the labels. Use the format "[label]. [text]." (dot and space after [label], dot after [text], one entry per line).
- If there are numbered in-text citations, you must format the references section using the same style as the author affiliations section.
- Format other superscripts and subscripts as plain text enclosed in [].
- Format tables as "Column1: Value1. Column2: Value2." (one line per row).
- Remove markups (bold (**), italic (* or _), strikethrough (~~) etc.).
- Remove blockquote markers (>).
- Remove HTML tags (<br> etc.).
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

Step 2: Write the output of `python3 -c "import json; d=json.load(open('[dir]/../refs.json'))['[pmid]']; print(json.dumps({'authors':d['authors'],'references':[]}, indent=2))"` to /tmp/[stem].json. Fill in "affiliation" and "references" values from the md, as instructed below.

Extract authors and references:
- Match the authors names in the paper to the "author" values in the json, which may be formatted differently. DO NOT modify the "author" values in the json.
- For each author, copy complete entries of author affiliations exactly as they appear in the md, formatted as one array, and replace the "affiliation" value in the metadata. Only replace the original line; DO NOT rewrite the entire json.
- Copy complete entries of references and supplementary references exactly as they appear in the md, formatted as one array, and replace the "references" value in the metadata. Only replace the original line; DO NOT rewrite the entire json.

Step 3: Run `mv '/tmp/[stem].md' '/tmp/[stem].json' '[dir]/'` to move the results to [dir]/.
---

## Deleting Citations

1. The user specifies which citation(s) to remove. Find the corresponding PMID from refs.json.
2. Remove the PMID key from refs.json.
3. Rebuild chroma_db/.
4. DO NOT delete files from papers/ (PDFs, md) unless the user explicitly asks. Inform the user which files remain.

## Searching for Information

1. Semantically enrich the user's query before searching. Expand abbreviations (e.g., TERT = telomerase reverse transcriptase), add synonyms (e.g., catalytic subunit), related terms (e.g., TERC, telomerase), and potential answer terms. Format the enriched query as a single string.
2. Pass the enriched query to search_refs.py to get ranked candidate papers.
3. Read papers/*.md files of top candidates.
4. Cite sources using [citation_in_text] when referencing specific findings.
