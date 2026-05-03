---
name: develop-parser
description: Contract, file layout, verification criteria, and bootstrap process for developing or modifying an HTML parser module in html_parsers/. Use when adding a new publisher parser or changing an existing one.
paths: html_parsers/**/*.py, papers_test/**, papers_test_ref/**
---

## Developing Parser Modules

Each publisher needs a parser module at `html_parsers/<second_level_domain>.py`. Modules must be standalone: no cross-module imports. Shared utilities go in `html_parsers/_helpers.py`. A canonical template lives at `html_parsers/_template.py`; copy it to `html_parsers/<second_level_domain>.py` and fill in the stubs.

The module must expose two public functions:
- `remove_banners(html)` -- Remove cookie banners, consent dialogs, overlays. Return html unmodified if nothing to remove. User provides specifics per publisher; do not guess.
- `parse_article(html)` -- Single entry point. Returns dict with papers/*.json keys in order: title, journal, year, volume, issue, pages, doi, authors, main_text, references.

`parse_article` delegates to these private functions (identical signatures in every parser):
- `_parse_metadata(html) -> dict` with keys title, journal, year, volume, issue, pages, doi.
- `_parse_authors(html) -> list` of `{"author": "LastName IN", "affiliation": [str, ...]}`.
- `_parse_references(html) -> list` of `{"": {title, journal, year, volume, issue, pages, doi, authors}}`.
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
  - Metadata (in papers/*.json-format keys): title, journal, year, volume, issue, pages, doi, authors.
  - "references" key: references + supplementary references.
  - "main_text" key: from abstract to before the first references section + supplementary materials

## Verification loop (mandatory)

Static structure checks (header counts, chr totals, regex on HTML) are a first pass only. They cannot judge whether the JSON actually captures what a reader sees, because publisher HTMLs routinely hide content behind JS-rendered sections, collapsed accordions, or unusual markup that regex misses. Static checks pass while real bugs go unreported.

Agent + browser-rendered text verification is therefore **required** before a parser can be considered done. After static checks look clean for a paper, run this loop:

1. Open the paper's HTML in a real browser via CDP (reuse `get_refs.start_browser() / stop_browser()`), extract `document.body.innerText`.
2. Spawn one agent per paper (Agent tool, Explore or general-purpose). Hand it the rendered text and the parser's JSON output. Ask it to report every field in the JSON that disagrees with the rendered text, and every chunk of rendered text missing from the JSON.
3. Evaluate each reported bug. Decide: parser defect (fix), known caveat (e.g. affiliations hidden from rendered text — document), or agent false positive (ignore with justification).
4. Fix every parser defect in the module.
5. Re-run convert_html.py, then re-run the agent verification on the same papers.
6. Repeat until an agent round reports no new parser defects.

One clean round is not enough when a previous round found defects — changes can introduce regressions in unrelated fields, so the final pass must be a fresh one where nothing was changed since the verification started. Reporting a parser done after only static checks, or without a clean final agent round, is a protocol violation.

## Capture-time image resolution

`get_refs.py` runs single-file via Edge/CDP and inlines whatever `<img src>` resolves to at capture time. Some publishers serve a thumbnail by default (typically 100–600 px), with the high-res version one indirection away — a parent `<a href>`, a `data-large-src` attribute, a sub-page that hosts the full image, or a CDN URL pattern derivable from the article id. When this happens, the `<img>` data URL inlined in `papers/<stem>.html` is small (≈5–15 KB) and scales up blurry under the format-html skill's full-column-width figure CSS.

The fix is in `get_refs.py`, wired into `_PUBLISHER_RULES` (around `get_refs.py:1008–1084`). Two implementation flavors:

**Browser-script (pre-SingleFile DOM rewrite).** Use when the high-res URL is reachable from inside the captured page DOM (parent `<a>`, data attribute). Reference: plos / biorxiv / cshlp / jci entries.

```python
"<domain>": {
    "wait_until": "networkidle0",
    "wait_delay": 8000,
    "browser_script": "<path>/highres_<domain>.js",
}
```

The `.js` file walks every figure `<img>`, swaps `src` to the high-res URL pulled from the parent `<a>` / data attribute, and waits for the new images to load before SingleFile captures.

**Post-capture server-side fetch.** Use when the high-res lives on a sub-page or behind logic the browser script can't reach (login redirect, client-side React rendering, derivable-from-id CDN URL). Reference: `_iucr_inline_figures` (`get_refs.py:567–703`), `_imrpress_inline_figures`, `_annualreviews_inline_figures`.

```python
"<domain>": {
    "wait_until": "networkidle0",
    "wait_delay": 8000,
    "post_capture": lambda h, p: _<domain>_inline_figures(h, p),
}
```

`_<domain>_inline_figures(html, output_path)` parses the saved HTML, derives high-res URLs (sub-page scrape, CDN pattern from article id), fetches via `urllib`, base64-encodes, and replaces the thumbnail `src` with a data URL.

**Decision rule.** Browser-script is preferred when both are technically feasible — lower latency, no extra HTTP round-trips, no double-fetch. Reach for post-capture only when the high-res URL is unreachable from inside the page DOM.

**Defer rule.** If the high-res URL pattern can't be confirmed from 1–2 sample papers (no parent `<a>`, no data attribute, no obvious sub-page link, no derivable CDN URL), do not ship a partial fix. Log the publisher to a deferred-image list (e.g. `temp/deferred_image_publishers.md`) with sample PMID and what was tried, and surface to the user. CSS-only changes still ship — thumbnails will display at full column width but be visibly blurry, which is no worse than the pre-change state.

**Re-fetch + backup convention.** After extending `get_refs.py`:

1. Re-run `python get_refs.py <pmid>` for the test paper to capture HTML through the modified pipeline.
2. Confirm the new `papers/<stem>.html` has high-res image data URLs (data URL length > 50 KB per figure, vs ~5–15 KB for thumbnails).
3. Copy the freshly-captured raw HTML to `papers_ref/<stem>.html` (raw-backup convention; mirror of `papers/` before `convert_html.py` runs).
4. Run `python convert_html.py papers/<stem>.html` to apply `remove_banners` and regenerate the JSON. `convert_html.py` mutates the file in place — `papers_ref/<stem>.html` is the clean reset source if formatting needs to be re-derived later.

**JSON parity caveat.** Most parsers strip `<img>` tags via `extract_captions()` + `strip_common()` before `tags_to_text()`, so changing the inlined image bytes does not affect parser output. Verify per parser that no field captures image URLs (rare). If JSON parity breaks, the parser is touching image markup and needs a tighter selector that ignores `src` content.

## Bootstrap process for a new publisher

Requires at least 30 papers in `papers/<second_level_domain>/`; if fewer, confirm with user before proceeding.

1. Copy HTMLs from papers/ into `papers_test/<second_level_domain>/` subfolders by second-level domain extracted from the SingleFile URL comment. Replace punctuation with `_` in folder names. papers/ is not modified.
2. Examine several htmls from `papers_test/<second_level_domain>/` as starting example.
3. Extract rendered text from the htmls as content reference.
4. Ask the user to identify visually impairing elements (cookie banners, consent overlays, login modals) for remove_banners. Do not assume what to remove.
5. Audit figure handling. For 1–2 sample papers with figures, inspect the saved HTML to determine: (a) whether the captured `<img src>` is high-res or a thumbnail, (b) the figure container selector and image selector, (c) the native layout (image above caption vs side-by-side). If thumbnail-only, decide on a `get_refs.py` strategy (browser-script vs post-capture) before implementing the parser, since capture-time inlining changes which `<img>` bytes end up in `papers/<stem>.html`. See § "Capture-time image resolution".
6. Implement parse_article, verifying each field against refs.json and the rendered text.
7. Run convert_html.py on the full `papers_test/<second_level_domain>/` directory.
8. Sample 3 papers. Spawn one agent per paper to extract text from rendered html and inspect converted json verify all content is correctly filled in.
9. If issues are detected, implement fixes, reset the test folder (see below), run convert_html.py, and spawn agents to verify.
10. Write main_text to `papers_test/<second_level_domain>/<stem>.md` for user inspection.

## Resetting Test Folders

convert_html.py mutates HTMLs in papers_test/ (banner removal) and writes JSON alongside. To re-run a parser from a clean state:
- Single domain: `rm -rf papers_test/<domain>/ && unzip -q papers_test_ref/<domain>.zip -d papers_test/<domain>/`.
- All domains: `rm -rf papers_test/ && for z in papers_test_ref/*.zip; do d=$(basename "$z" .zip); unzip -q "$z" -d "papers_test/$d/"; done`.

papers_test_ref/ is the reference snapshot and must not be modified during parser development. If new test HTMLs are added to papers_test/, re-zip the affected subfolder into papers_test_ref/ so future resets pick them up.
