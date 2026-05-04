---
name: develop-parser
description: Two-phase contract for developing or modifying an HTML parser module in scripts/html_parsers/. Phase 1 (parse_html) implements parse_article against unmodified HTML and produces a JSON parity reference. Phase 2 (format_html) implements remove_banners and must preserve bit-identical JSON output. Use when adding a new publisher parser, extending an existing parser to a new alias, or modifying a parser.
paths: scripts/html_parsers/**/*.py, papers/test/**
---

## Overview

Each publisher needs a parser module at `scripts/html_parsers/<second_level_domain>.py`. Modules are standalone — no cross-module imports. Shared utilities live in `scripts/html_parsers/_helpers.py`. The canonical scaffold is `scripts/html_parsers/_template.py`.

Development happens in two phases, in order:

- Phase 1 — parse_html: implement the four `_parse_*` functions against the unmodified HTML. Run convert_html.py, verify the JSON, and snapshot it as the parity reference.
- Phase 2 — format_html: implement `remove_banners` against the seven hard requirements. Re-run convert_html.py and confirm the JSON is bit-identical to the Phase 1 snapshot.

Assumption: parsers are developed using HTMLs that contain complete content (all paper sections, all figures, all references). Edge cases (paywalled abstract-only HTMLs, papers with no figures, irregularly delimited reference lists) are left to your judgment during development — this skill does not codify conditional fallbacks for them.

## Shared contracts

These apply to both phases.

### File layout

Every parser module matches the canonical layout in `_template.py`:

- Module docstring: single line, `"""<Publisher> (<second-level-domain>) HTML parser."""`.
- Imports: stdlib first (alphabetized), then `from html import unescape`, then `from ._helpers import (...)` with names alphabetized in the tuple.
- No in-body imports.
- Module constants in fixed order when present: `_NOISE`, then any publisher-specific constants you need.
- Six section dividers in this exact order, each formatted as `# ---------------------------------------------------------------------------` (75 hyphens) with a blank line on either side and a section title comment line: Banner removal, Metadata, Authors, References, Main text, Public API.
- Publisher-specific private helpers live under the section where they are first called, without their own dividers.
- Every public and private function has a docstring whose first line is a one-sentence description.
- `parse_article` body is identical in every parser — copy from the template verbatim.

When modifying a parser for any reason, check that it still matches the layout and bring it into shape in the same change.

### Output shape

`parse_article` returns a dict with keys in this exact order:

```
title, journal, year, volume, issue, pages, doi, authors, main_text, references
```

Field formats (apply to both the main paper and to each entry in `references`):

- title: str without trailing period.
- journal: ISO abbreviation when the publisher exposes one, else the full journal title verbatim. Dots are stripped centrally in `clean_parsed_output` — your parser may emit them or not.
- year: 4-digit publication year. Not received/accepted/online year.
- volume, issue: str (may be empty).
- pages: `firstpage-lastpage` or `firstpage` alone.
- doi: `https://doi.org/...` URL — call `format_doi` to ensure the prefix.
- authors in main paper: list of `{"author": "LastName IN", "affiliation": [str, ...]}`.
- authors in references: list of plain strings in `LastName IN` form (no affiliation).

References are wrapped: `[{"": {title, journal, ..., authors: [str, ...]}}]`.

### Author-name contract

Applies to both `_parse_authors` and `_parse_references`. The contract is the most-violated rule across the codebase — read it before writing any author-extraction code.

- Extract `(given, surname)` pairs from the HTML when the source exposes them as separate fields. Prefer structured sources (separate given-name/surname tags, schema.org microdata, structured JSON keys) over combined strings — they eliminate the surname-boundary guess.
- Call `format_name(given, surname)` to emit `"LastName IN"` from a structured pair.
- When the HTML only exposes a combined name string (e.g. `Given Last`, `Last, Given`, `JD Griffith`, `Boulé J.-B.`), pass it to `format_author_name`, which routes through `parse_combined_name` + `format_name`.
- Never tokenize, split, flip, or build initials inline. Hyphenated given names (`Jean-Baptiste` → `JB`), dotted initials (`J.-B.`), already-compact initials (`JA` stays `JA`), compound surname prefixes (`de Lange`, `d'Adda di Fagagna`, `Nick McElhinny`), and trailing suffixes (`Jr.`, `III`) are all handled centrally. Extending coverage for a new compound surname or initial convention happens in `_helpers`, not in a parser.

Forbidden patterns inside parsers (greppable): inline `re.split(...)` applied to author names, `p[0] for p in ...split()` for initials, bespoke surname-flip helpers, per-parser `_PARTICLES` / `_NAME_PARTICLES` / `_SURNAME_PREFIXES` sets.

### Aliases

A parser may serve multiple second-level domains via `_DOMAIN_ALIASES` in `scripts/html_parsers/__init__.py`. The aliases for a parser are: the canonical name (the parser's filename) plus every key in `_DOMAIN_ALIASES` whose value is the canonical name.

The test pool must include at least 3 HTMLs from each alias. Applies to new-parser development, alias extension (adding a key to `_DOMAIN_ALIASES`), and modifications to an existing parser.

## Bootstrap

Before Phase 1:

1. Enumerate the parser's aliases (canonical name + every `_DOMAIN_ALIASES` key whose value is the canonical name).
2. Confirm `papers/raw/` contains at least 3 HTMLs with complete content (all sections, figures, references) per alias. If any alias falls short, ask the user to fetch more before continuing.
3. Copy 3 HTMLs per alias into `papers/test/<stem>.html`. Flat layout — no per-alias subfolders.
4. For a brand-new parser, copy `scripts/html_parsers/_template.py` to `scripts/html_parsers/<sld>.py` and update the module docstring. For an existing parser being modified, skip this step.

## Phase 1 — parse_html

Implement the four `_parse_*` functions to satisfy the output contract. Leave `remove_banners` as the no-op that ships in the template. Inspect the actual HTML and choose the extraction strategy — the SKILL.md describes the contract, not the selectors.

### Main_text boundary rules

- Body sections: keep everything from the abstract to before the first references section.
- Supplementary: after the first references section, keep only sections whose heading matches supplement / extended data / source data / expanded view / powerpoint / appendix.
- Remove all references sections from `main_text`.

### Standard text pipeline

```
body_html = extract_captions(body_html)
body_html = strip_common(body_html)
text = tags_to_text(body_html)
return drop_noise(text, _NOISE)
```

This order is the strong default (27/28 parsers). Reverse the first two only when figure handling requires it.

`_NOISE` ships empty. Populate after running the parser end-to-end and inspecting the residual lines that survive `extract_captions` and `strip_common` — short trailing strings like "Open in a new tab", "Download Article", "Google Scholar".

### Capture-time image resolution

`get_html.py` runs single-file via Edge/CDP and inlines whatever `<img src>` resolves to at capture time. Some publishers serve a thumbnail by default (typically 100–600 px), with the high-res version one indirection away — a parent `<a href>`, a `data-large-src` attribute, a sub-page that hosts the full image, or a CDN URL pattern derivable from the article id. When this happens, the inlined `<img>` data URL is small (≈5–15 KB) and scales up blurry under the Phase 2 figure CSS.

The fix is in `get_refs.py` (`_PUBLISHER_RULES`), not in the parser. Two flavors:

- Browser-script (pre-SingleFile DOM rewrite): a `.js` file walks every figure `<img>`, swaps `src` to the high-res URL pulled from the parent `<a>` / data attribute, and waits for the new images to load before SingleFile captures. Use when the high-res URL is reachable from inside the captured page DOM.
- Post-capture server-side fetch: a `_<domain>_inline_figures(html, output_path)` callback parses the saved HTML, derives high-res URLs (sub-page scrape, CDN pattern from article id), fetches via `urllib`, base64-encodes, and replaces the thumbnail `src` with a data URL. Use when the high-res URL is unreachable from inside the page DOM.

Browser-script is preferred when both are technically feasible — lower latency, no extra HTTP round-trips. If the high-res URL pattern can't be confirmed from 1–2 sample papers, log to a deferred-image list and surface to the user rather than ship a partial fix.

After extending `get_refs.py`, re-run `python scripts/get_refs.py <pmid>` for the test paper, confirm the new HTML has high-res image data URLs (data URL length > 50 KB per figure vs ~5–15 KB for thumbnails), then re-run `python scripts/convert_html.py papers/raw/<stem>.html`.

### Verification chain

Mandatory before Phase 2. Per test paper.

Reference sources, in priority order:

1. `papers/parsed/<stem>.json` — PubMed metadata fetched by `get_refs.py`. Authoritative for title, journal, year, volume, issue, pages, doi, abstract, author names + affiliations. Already on disk before Phase 1 starts.
2. Rendered text — `document.body.innerText` extracted from the test HTML in a CDP browser. Ground truth for everything visible: full main_text, section headers, figure/table captions, references list, supplementary materials.
3. HTML source — fallback for content present but not visually rendered (most commonly author affiliations in meta tags or `display:none` divs).

Layer 1 — quantitative static checks (cheap, run on every iteration):

- Metadata field-by-field equality vs `papers/parsed/<stem>.json` (title, journal, year, volume, issue, pages, doi, author names).
- `chr_ratio = len(json_file) / len(rendered_text)` ≥ 1.0. Below 1.0 means content is missing (JSON has formatting overhead).
- Reference count: `len(json["references"])` equals the rendered references list count.
- Section header count: JSON main_text headers match rendered text headers.
- Figure/table mention count: `Fig. N` / `Table N` references match between JSON and rendered text.

Layer 2 — agent inspection (run after Layer 1 passes):

- Spawn one agent per test paper with three inputs: rendered text, the parser's `_converted.json`, the HTML source.
- Agent reports every JSON field that disagrees with the rendered text, every chunk of rendered text missing from JSON, every author affiliation present in the HTML source but missing from JSON.
- Triage each report:
  - Parser defect → fix the parser code.
  - Known caveat → document (e.g. publisher hides affiliations from rendered text; pulled from HTML source).
  - Agent false positive → ignore with justification.

Layer 3 — iteration:

- Fix every parser defect, re-run `python scripts/convert_html.py papers/test/<stem>.html`, then re-run Layer 1 + Layer 2 from scratch.
- The final agent round must be clean AND no parser changes since that round started. A clean round that follows fixes is not enough — fixes can introduce regressions in unrelated fields.

Layer 4 — snapshot for Phase 2 parity:

- Once a clean final round lands, copy each test paper's `_converted.json` to `/tmp/<stem>.ref.json`. These snapshots are the bit-identical-diff baseline for Phase 2.

## Phase 2 — format_html

Goal: lock the page to its native 720-px-width layout, apply the explicit margins, and remove only the chrome categories listed below. Preserve native typography. Preserve all other site content; do not pre-emptively strip headers, footers, breadcrumbs, related articles, post-article CTAs, etc. After the hard requirements are met, hand off to the user for inspection — the user identifies any additional content to remove.

### Hard requirements

The only changes allowed without explicit user direction:

1. Lock rendering to the publisher's native 720-px-width layout. Apply a body cap (max-width:752px, margin:0 auto, white background). If the publisher's desktop-layout media queries still fire at wider viewports, call `neutralize_media_queries(html)` from `_helpers` to rewrite every `<style>` block so the narrow-form CSS applies unconditionally.
2. Main reading column margins: 56 px top/bottom, 16 px left/right. Apply via `padding: 56px 16px` on the highest common ancestor of title + authors + affiliations + abstract + body + figures + references.
3. Remove cookie banner AND any associated dark/semitransparent overlay. The overlay is often a separate sibling element rendered as a fixed/absolute backdrop — both must go.
4. Remove colored backgrounds. Any background-color that isn't white or transparent and isn't a figure/table border is in scope.
5. Remove sticky elements. Verify `position: sticky` or `position: fixed` via computed styles before removing — do not remove based on naming alone.
6. Remove side blocks that prevent the main text column from filling the page width at 720 px. A side block qualifies only when its presence shrinks the rendered text column below the page width.
7. Remove advertisement blocks.
8. Figures: use high-res images (extend capture in `get_refs.py` if the publisher serves thumbnails by default — see § Capture-time image resolution); width-align image with caption; image above caption; visible spacing between image and caption. Generic CSS:

   ```css
   :root WRAPPER FIGURE_SEL,
   :root WRAPPER FIGURE_SEL > * {
       display: block !important;
       width: 100% !important;
       text-align: left !important;
       box-sizing: border-box !important;
   }
   :root WRAPPER IMG_SEL {
       display: block !important;
       width: 100% !important;
       height: auto !important;
       max-width: 100% !important;
       margin: 0 0 5px 0 !important;
   }
   ```

   Replace `WRAPPER`, `FIGURE_SEL`, `IMG_SEL` with publisher-specific selectors. Bare `img { width: 100% }` is forbidden — it catches inline icons and journal-meta logos.

### Out of scope

Do NOT include in `remove_banners` without user direction: header / site nav, footer, breadcrumbs, related articles, "Cited by", post-article sign-up / newsletter CTAs, metrics widgets, share buttons, accordion expand/collapse fixes, line-box descent compensation, JS-only state hidden in the snapshot.

The user inspects the rendered page after the hard requirements land and identifies any additional content to remove. Do not pre-emptively strip.

### Helpers

Available in `_helpers.py`:

- `remove_elements_by_id(html, *ids)` — stable ids. Matches both quoted and unquoted `id=` attributes.
- `remove_elements_by_selector(html, *class_substrings)` — `<div>` with double-quoted class containing the substring.
- `_remove_nested_element(html, start_pattern)` — arbitrary opening-tag regex. Use when the helper doesn't match (non-div tags, unquoted attrs). Removes ONE occurrence per call — wrap in a loop if multiple instances may exist.
- `neutralize_media_queries(html)` — rewrite `<style>` blocks so the publisher's narrow-form CSS applies unconditionally.

### Injection point

```python
override = "<style>...</style>"
if "</head>" in html:
    html = html.replace("</head>", override + "</head>", 1)
else:
    html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
```

Some SingleFile outputs omit `</head>`; fall back to injecting before `<body`.

### Verification

All three are mandatory.

JSON parity (hard invariant):

1. Reset HTML by copying `papers/raw/<stem>.html` back to `papers/test/<stem>.html` (overwrites the in-place mutation `convert_html.py` made on the previous run).
2. Delete the prior `papers/test/<stem>_converted.json`.
3. Run `python scripts/convert_html.py papers/test/<stem>.html`.
4. `diff /tmp/<stem>.ref.json papers/test/<stem>_converted.json` — must be empty for every test paper. Non-empty diff means the chrome strip is too aggressive — tighten the selector.

Margin check at 720-px viewport:

```bash
python .claude/skills/develop-parser/scripts/measure_layout.py <parser> papers/test/<stem>.html 720
```

Confirm the reading column reports 56 px top/bottom and 16 px left/right (±4 px tolerance for sub-pixel rendering).

Visual handoff:

Open one test paper at vw=720 in the CDP browser. Hand off to the user for inspection. The user identifies any remaining chrome they want removed (which then becomes a follow-up `remove_banners` change driven by an explicit user request).

## Resetting test files

`convert_html.py` mutates HTMLs in `papers/test/` (banner removal applied in place). To re-run a parser from a clean state, copy a fresh `papers/raw/<stem>.html` back to `papers/test/<stem>.html`, deleting any prior `papers/test/<stem>_converted.json` first. There is no separate reference-snapshot directory — `papers/raw/` is the canonical source.
