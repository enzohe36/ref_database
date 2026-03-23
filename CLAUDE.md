## Project Overview

- This is a literature research and grant writing assistant.
- Project-specific files (aims, drafts, notes) are in projects/.
- Conda environment: py310 (used for convert_pdf.py).

## General Rules

- Markdown section headers are allowed but be conservative. Do not use other markdown formatting (bold, italic, links, etc.) unless explicitly asked.
- When writing in bullet points, incomplete sentences are allowed. Do not write more than one sentence per bullet point.
- Bullet points are intended to inform the user so that they could write full paragraphs in their own words.
- If you cannot access a paper, you must ask the user for it.
- If you are unsure about the user's intent, you must ask for clarification.
- If you are unsure about factual content, you must ask the user for additional information.
- Be brutally honest and straightforward in your response.
- Do not give suggestions that "might work"; give suggestions that you are sure will work.
- If the user is wrong, point it out.

## File Structure

- refs.json is the citation database. JSON dict keyed by PMID. Each entry has fields:
  - "publication_types": array of types (e.g., ["Journal Article", "Review"]).
  - "citation_in_text": short author-year string for in-text citations. "LastName YYYY" (1 author) / "LastName & LastName YYYY" (2) / "LastName et al. YYYY" (3+). E.g., "Aden et al. 2019".
  - "title": article title.
  - "journal": journal abbreviation (ISO format).
  - "pub_date": publication year.
  - "volume", "issue", "pagination": journal location. May be empty.
  - "doi": DOI as URL (https://doi.org/...). May be empty.
  - "authors": array of {"name": "LastName Initials", "affiliations": [...]}.
  - "references": array of PMIDs (strings) cited by this paper.
- <citation_short> = "<citation_in_text> <journal> <pmid>" (e.g., "Aden et al. 2019 Gastroenterology 30273559"). Used as file name for papers/ files.
- <citation> = full citation string for reference lists. Format: "Author1, Author2, ... YYYY. Title. Journal. Volume(Issue):Pages. PMID: PMID."
- papers/ stores pdf files, converted md files, and _summary.md files.
- papers/<citation_short>.md contains the converted PDF content.
- papers/<citation_short>_summary.md contains section-by-section summaries.
- keyword_map.pkl is the TF-IDF search index (scikit-learn). Built from papers/*.md.
- synonyms.json normalizes aliases (e.g., TERT -> telomerase reverse transcriptase).
- projects/ contains project-specific files (aims, drafts, notes). Not tracked by git.

## Scripts

- `python cite.py <pmid> [<pmid> ...]`: fetches PubMed XML. Adds metadata to refs.json, appends to temp_refs.md. Skips non-Journal Articles, retracted articles, and duplicates.
- `python cite.py --validate`: checks for retracted articles and searches for published versions of preprints.
- `conda run -n <env> python convert_pdf.py <file.pdf> [<file.pdf> ...]`: converts PDFs to text using pymupdf4llm (with fitz fallback for scanned PDFs). Writes converted text to <citation_short>.md in the same directory as the PDF, overwriting existing content.
- `python search.py <term> [<term> ...]`: search papers, returns ranked JSON with PMIDs and scores.
- `python search.py --build`: rebuild keyword_map.pkl from papers/*.md.

## Literature Search

- Use PubMed E-utilities (esearch.fcgi) to search for papers by keyword, author, journal, etc.
- Prioritize papers that are highly cited, recent (2021-2026), or from prestigious journals (e.g. Nature, Cell, Science).
- When searching for a source, prioritize local content.

## Adding Citations

1. The user specifies which citation(s) to add. The input may be PMIDs, PubMed URLs, DOI URLs, or other formats - extract the PMID from whatever is provided.
2. Run: `python cite.py <pmid> [<pmid> ...]`. The script accepts multiple PMIDs and processes them sequentially with a delay between PubMed requests.
3. When prompted by the user, convert all PDFs in parallel using all available threads: `ls papers/*.pdf | xargs -P $(sysctl -n hw.ncpu) -I {} conda run -n py310 python convert_pdf.py "{}"`
4. Launch one agent per paper in parallel to clean up and summarize. Use the following prompt for each agent, substituting <citation_short> with the paper's file name:

---

Only read and modify files in papers/! Do not access internet or any other directories!

Your task: clean up and summarize one paper.

File: papers/<citation_short>.pdf

Step 1 - Clean up: Read papers/<citation_short>.md. Consult the PDF only when something is ambiguous (e.g., column interleaving, garbled text). Edit papers/<citation_short>.md to apply all of the following cleanup rules. Keep the original wording; do not rewrite unless it is a necessary fix.

DELETE these sections/elements entirely:
- Everything before the title (journal name, logos, article type labels)
- Author names, affiliations, corresponding author info, email addresses
- Article history (received/accepted dates)
- Table of contents / contents listing (lines with leader dots)
- Page headers and footers (author name / journal name / volume / page range lines)
- Standalone page numbers
- Copyright and DOI lines
- Picture placeholders (==> picture ... intentionally omitted <==)
- Garbled text extracted from figures/charts/diagrams (----- Start/End of picture text -----)
- References / bibliography section. Stop deleting at supplementary material if present.
- Acknowledgements, funding, conflict of interest, author contributions, data availability

KEEP these sections/elements:
- Paper title (as ## heading)
- Abstract text
- Keywords
- All body sections (introduction, results, discussion, methods, etc.) regardless of heading style
- Figure and table captions (lines starting with "Fig." or "Table N")
- Box/sidebar content
- Supplementary methods and supplementary figure/table captions
- Not all papers have typical section titles - reviews, comments, protocols, and other non-primary articles may have arbitrary section structures. Keep all substantive content regardless of section naming.
- Prioritize retaining all body sections over removing non-body sections.

FIX formatting:
- Section headings: use ## prefix, plain text, no bold/italic
- Remove all bold (**), italic (* or _), strikethrough (~~), and other markup
- Remove blockquote markers (>)
- Remove HTML tags (<br>, etc.)
- Convert markdown tables to plain text: keep the caption, then list content as "Column1: Value1. Column2: Value2." per row, or summarize if the table is simple.
- Rejoin fragmented paragraphs and deinterleave two-column layouts using the PDF as reference.
- Collapse multiple blank lines to single blank line

FIX encoding (use context and the PDF to determine the correct replacement):
- Ligatures: fi, fl, ff, ffi, ffl and font-specific variants -> ASCII equivalents
- Dashes/hyphens: en dash, em dash, minus sign, non-breaking hyphen -> ASCII hyphen
- Quotes: curly single/double quotes -> straight quotes; prime symbols -> apostrophe
- Math fonts: mathematical bold/italic/script characters -> plain ASCII or Greek equivalents
- Font-specific Greek: codepoints used as Greek letters (e.g., in gammaH2AX, beta-actin, Pol delta, mu-units) -> standard Greek Unicode
- Standardize mu: micro sign (U+00B5) -> Greek mu (U+03BC)
- Font-specific symbols: garbled codepoints used as parentheses, equals signs, plus signs, etc. -> correct ASCII
- Keep legitimate non-ASCII: Greek letters, Latin diacritics in names, math symbols (<=, >=, ~, etc.), degree sign, plus-minus, multiplication sign, Angstrom

Step 2 - Summarize: Check if papers/<citation_short>_summary.md exists. If not, create it by reading papers/<citation_short>.md and summarizing section by section. Write as many bullet points as necessary per section; do not balance counts across sections. Refer to the original PDF if anything in the md file is unclear. If the authors explicitly discuss future directions, add a Future Directions section. Use the following format:
  ```
  Section 1 title
  - Bullet point 1
  - Bullet point 2
  - ...

  Section 2 title
  ...

  Future Directions
  ...
  ```
---

1. After all agents finish, run: `python search.py --build` to rebuild the search index.
2. Analyze keyword_map.pkl to identify terms that should be synonyms (e.g., abbreviations vs full names, alternate spellings, gene names vs protein names). Suggest expansions to synonyms.json and update it if the user approves.

## Deleting Citations

1. The user specifies which citation(s) to remove. Find the corresponding PMID from refs.json.
2. Remove the PMID key from refs.json.
3. Run: `python search.py` to rebuild the search index.
4. Do NOT delete files from papers/ (PDFs, md, _summary.md) unless the user explicitly asks. Inform the user which files remain.

## Searching for Information

1. Identify keywords from user's request.
2. Run: `python search.py <term> [<term> ...]` to get ranked candidate papers.
3. Read _summary.md files of top candidates. If a _summary.md does not exist, read the full md file instead.
4. Read full md files only if the summary is insufficient for the question.
5. Cite sources using <citation_in_text> when referencing specific findings.
