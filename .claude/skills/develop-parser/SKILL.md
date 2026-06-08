---
name: develop-parser
description: Two-phase contract for developing or modifying an HTML parser module in scripts/html_parsers/. Phase 1 (parse_html) implements parse_article against unmodified HTML and produces a JSON parity reference. Phase 2 (format_html) implements remove_banners and must preserve bit-identical JSON output. Use when adding a new publisher parser or modifying an existing one.
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

### One parser per publisher

Every publisher gets a dedicated parser module at `scripts/html_parsers/<second_level_domain>.py`. There is no alias mechanism — `detect_domain` returns the raw second-level domain unchanged.

## Bootstrap

Before Phase 1:

1. Confirm `papers/raw/` contains at least 3 HTMLs with complete content (all sections, figures, references) for the publisher. If fewer than 3, ask the user to fetch more before continuing.
2. Copy the 3 HTMLs into `papers/test/<stem>.html`. Flat layout — no subfolders.
3. For a brand-new parser, copy `scripts/html_parsers/_template.py` to `scripts/html_parsers/<sld>.py` and update the module docstring. For an existing parser being modified, skip this step.

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

**IMPORTANT!** If a publisher's HTML files are all confirmed to be abstract-only (no full text, no figures, no references), skip Phase 2 entirely. Flag this issue when reporting verification results.

## Phase 2 — format_html

Goal: lock the page to its native 720-px-wide layout, apply the publisher-specific cleanup needed for clean reading, and resolve any image/spacing issues before snapshotting parity. Work step-by-step in the order below. Each step has detection criteria and a clear "what to remove or change" rule — implementation (selectors, CSS, regex) is the developer's judgement; do not import from sibling parser modules.

### Setup

Open the test HTML in a fresh Edge instance with CDP enabled (`--remote-debugging-port=PORT --remote-allow-origins=*`). After each change to `remove_banners`, apply it, write the result to a sibling `<stem>.formatted.html`, and reload Edge. All inspection runs via CDP.

Reusable scripts under `.claude/skills/develop-parser/scripts/`:

- `scan_sticky.py <parser> <html>` — Step 3 detector. Combines a static computed-style scan with a multi-position scroll test (snapshot viewport-relative tops at multiple `scrollY` positions, flag elements with low std-dev). Catches both declared `position: fixed/sticky` and JS-driven faux-sticky.
- `scan_gaps.py <parser> <html> [vw1 vw2 ...] [--threshold N]` — Steps 10 + 11 verifier. Runs three pixel-level scans per viewport on a chunked full-page screenshot (Chromium silently corrupts single-shot captures above ~16K px): (1) vertical empty bands ≥ threshold (default 80 px) for spacing, (2) column margins L/R/W (smallest left/right white margin around content + spanned content width) for width adjustment, and (3) bg-around-column — sample near (5–30 px) and far (50+ px) bands on each side of the main reading column and report median RGB per band, catching colored page backgrounds AND box-shadows around the column that the DOM ancestor-chain scan misses. Default viewports cover the three Phase 2 width regimes — narrower than the cap, at the cap, wider than the cap.

### Workflow

Before touching `remove_banners`, create a TaskCreate to-do list with one task per step (Steps 1 through 13). Work strictly in order. Mark a task `in_progress` when you start it, `completed` only after its detection criteria pass on the iteration fixture. Do not skip ahead, do not batch.

**IMPORTANT!** Each step has a detection rule and a "what to remove or change" rule. Act ONLY on what the detection rule flags. Do not pre-emptively remove or restyle anything outside what the step explicitly calls for. Do not remove site chromes (especially header and footer) unless they are flagged by one of the following steps.

**Step 1 — Lock layout to 720-px-wide native form and center the main text column.** Two requirements:

- **Cap body width** to the publisher's native 720 + gutters AND center on the page (`max-width: 752px; margin: 0 auto`) so wider viewports don't let content drift to one side. The main text column must remain horizontally centered in the viewport at every viewport ≥ cap.
- **Force the publisher's CSS to resolve to its narrow-form branch at any viewport** so desktop @media-gated sidebars don't appear when the actual viewport is wide (`neutralize_media_queries` from `_helpers.py` rewrites the relevant `<style>` blocks).

Verification of the locked layout is deferred to Steps 10–13.

**Step 2 — Cookie banner and overlay.** Identify the publisher's consent banner and any dark/semitransparent backdrop that pairs with it. Both go. A banner sometimes carries its own backdrop and sometimes ships the backdrop as a separate sibling — inspect before stripping.

**Step 3 — Sticky elements** (anything that stays put for a section or for the whole page while scrolling). Two scans, both run via `scan_sticky.py`:

1. Static computed-style scan — walk every element, keep those whose computed `position` is `fixed` or `sticky`, filter invisibles (display/visibility/opacity/zero-rect/off-screen).
2. Multi-position scroll test — snapshot every visible element's viewport-relative `top` at multiple `scrollY` positions, two `requestAnimationFrame` ticks between scroll and re-capture; sticky elements have low std-dev across positions. Catches JS-driven faux-sticky (transform-on-scroll) and `position: sticky` elements that engage only past a scroll threshold.

De-dupe to outermost (a sticky parent has visually-sticky children even if their own `position: static`). Remove the detected outer elements.

**Step 4 — Vertical columns other than the main text column** (typically span from top page-wide chrome to bottom page-wide chrome). Detection: anchor on the main reading column's bounding rect; flag elements with height ≥ ~50% of `document.scrollHeight` AND x-range overlapping main by < ~20% of main's width; de-dupe to outermost. After Step 1 most publishers' narrow CSS has already collapsed sidebars — usually a no-op, but remove explicitly when something remains.

**Step 5 — Ad blocks.** Detection: word-boundary token match against the publisher's ad-naming conventions on element class/id (typical conventions to look for include the literal `ad`/`ads`, common ad-tech suffixes from the GAM/GPT/DFP family, and "sponsored" markers). Use word boundaries so legitimate words like "address" or "addiction" aren't false-matched. Include zero-size reservation wrappers — they still pad vertical space when no ad loads. Remove outer wrappers; ad slots may use any block-level tag, not necessarily `<div>`.

**Step 6 — Colored backgrounds and shadows around the main text block.** Two complementary detectors:

1. **DOM ancestor scan** — walk only the ancestor chain of the main reading column; clear any non-white, non-transparent `backgroundColor` on those ancestors. Do NOT touch siblings of main (footer panels, recommendations) — they sit beside or below main, not behind it.
2. **Browser-rendered band scan** (`scan_gaps.py` `bg:` line) — sample pixels in two bands on each side of the main column: a **near band** (5–30 px outside the column edge) catches `box-shadow` and visible borders that the ancestor scan completely misses; a **far band** (50+ px outside, out to viewport edge) catches the page-level background as actually painted (covers `background-image` gradients/patterns, `::before`/`::after` pseudo-element backdrops, and z-index sibling overlays — all invisible to a `getComputedStyle().backgroundColor` walk). Verdict per viewport: `clean`, `page-bg-colored` (far band non-white), `shadow` (near band differs from far), or both.

Apply CSS that targets the actual offender — `box-shadow: none` on the wrapper that ships the shadow, `background: transparent` on the colored wrapper, etc. Re-run `scan_gaps.py` until the bg verdict is `clean` at every viewport.

**Step 7 — Image quality check.** Inspect every figure image via CDP — natural dimensions, decode-complete state, and source. Flag every image whose natural dimensions are zero, whose decoding never completed, whose source is a placeholder rather than real bytes, or whose native resolution is meaningfully below the target column width. Categorize and resolve each before continuing:

- **Loading issue** — the image bytes are in the saved HTML but the browser isn't decoding them. Common cause: a deferred-loading attribute that holds off decoding until the image scrolls into view. Fix in the parser (`remove_banners`) by removing whatever defers the load, or scroll-trigger the page before sampling.
- **Retrieval issue** — the image bytes are missing from the saved HTML (placeholder, broken URL, or publisher serves a thumbnail when a full-res variant exists). Fix in `get_html.py` via a per-publisher post-capture hook: walk the saved HTML, locate the original/full-res URL from whatever the page exposes (machine-readable metadata blocks, alternate-source attributes, parent links to a higher-res asset), fetch deterministically, encode, and write back.

The same per-publisher post-capture hook also runs automatically during `convert_html.py` whenever the saved HTML still carries `<img src=data:,>` placeholders (a transient capture-time fetch failure). Re-running the conversion is enough to heal them — no need to re-fetch the page through `get_html.py`.

Resolve every flagged image before proceeding. Re-inspect after each fix.

**Step 8 — Resize images.** With image data confirmed loaded and at usable resolution, apply CSS so each figure: (a) image width-aligned with caption; (b) image rendered above caption; (c) visible spacing between image and caption.

**Step 9 — Expand collapsed items.** Expand the following collapsed content:

- author list
- author info/affiliations
- main text sections
- tables
- figure/table captions
- references
- supplementary materials
- boilerplates (footnotes, publication histories, metrics, comments, cited-by etc.).

**IMPORTANT!** Only expand the content if the page's native layout expands the content in-place, pushing other content down. This is a hard requirement -- if the publisher's native behavior is an overlay, DO NOT attempt to replicate the expansion. Determine the native behavior by opening the page in a browser and triggering the publisher's own UI control once; class names that suggest expansion (panel, drawer, info-card) are not evidence either way.

Common examples: "show all authors", "+n authors", "show more", "expand for more", "show all references", page-wide section header with a navigate-down icon.

Common false positives: per-author affiliation popups that appear as a link; inline-citation hover popovers; in-figure "View larger" modal triggers.

The end result should emulate the publisher's native layout after clicking by the user. Three methods of expanding content in order of reliability:

1. DOM strip — neutralize whatever attribute or state flag the publisher uses to mark the element as collapsed.
2. CSS override — force-reveal selectors gated by a publisher collapse class or visibility attribute.
3. JS click simulation — only when content is genuinely lazy-loaded after click. SingleFile captures post-JS DOM, so layers 1 and 2 cover the typical case.

**Step 10 — Verify and fix spacing; restore native layout.** Run `scan_gaps.py`. Every reported gap must be resolved before the workflow is complete — either eliminated, or explicitly accepted as a publisher-native blank region.

Gaps caused by site chrome we deliberately keep (the area between the site header and the article column, the area between the article column and the site footer, the lane next to a publisher-native sidebar) are always publisher-native — accept them as-is. Do NOT hide the chrome to close those gaps.

For each remaining gap:

1. Identify what was at that position in the unmodified HTML (inspect by y-coordinate or diff against the raw HTML).
2. Determine the cause — most commonly: an earlier step removed an element but its parent / wrapper / sibling still reserves the height.
3. Restore the publisher's native layout around the affected position. Prefer removing the leftover reservation (parent wrapper, fixed height, residual margin/padding) over adding compensating CSS. Do NOT artificially collapse spacing the publisher renders natively (paragraph, section, figure margins) just to silence the scan.
4. Re-render and re-run `scan_gaps.py`. Repeat until every detected band is resolved or explained.

Tight transitions (sections too close) are out of scope for `scan_gaps`; that needs a text-band density detector.

Re-verify after every iteration with `scan_gaps.py`.

**Step 11 — Verify dynamic width adjustment + main-column centering.** The body cap from Step 1 must shrink to the viewport at narrow widths and clamp to 720 + gutters at wide widths, AND the main text column must remain horizontally centered at every viewport — every page-wide element (including site header / footer chrome that we keep) must honor that envelope. The column-margin scan in `scan_gaps.py` reports L/R/W per viewport; the same run that checks spacing also confirms the width envelope and centering.

Per-viewport rules:

1. **L = R, both ≥ 0 (centering invariant)** — the main text column sits inside the cap with symmetric gutters. At vw < cap, L/R should be the publisher's native gutter (often near zero); at vw ≥ cap, L = R = (vw − cap) / 2 (cap-centered). Asymmetric margins (R > L by hundreds of px) mean either the column is flush-left instead of centered, OR a sidebar / absolutely-positioned element is escaping the cap. Either case fails Step 1's centering requirement.
2. **W ≤ cap** — the spanned content width must not exceed the body cap. W > cap means a publisher wrapper ships its own fixed pixel width that ignores the cap; cap it so it shrinks to its parent.
3. **Body's rendered width = min(vw, cap)** — verify via CDP. If body stays at cap when the viewport is narrower, the publisher has set an explicit pixel-valued `width` on body alongside any `max-width`; both need to be overridden so body shrinks to viewport. (Width-overflow elements caught by rule 2 also surface separately when sampled via the per-element CDP scan from earlier steps.)

After each adjustment, re-render the formatted HTML, reload, and re-run `scan_gaps.py`. The check passes when L = R (within a few px), W ≤ cap, and body width tracks `min(vw, cap)` at every default viewport.

**Step 12 — Cross-fixture verification.** Steps 1-11 are typically driven against a single representative test HTML. Once that fixture is clean, re-run Steps 10 and 11 (`scan_gaps.py` for spacing + L/R/W) against every other test HTML for the publisher. Confirm that each problem identified and resolved during single-fixture development stays resolved on the others — the same fixed-position banner class, the same ghost reservation, the same ad wrapper selectors, the same figure CSS, the same width caps. Any regression on a sibling fixture means the fix was over-fit to one DOM variant; tighten the selector or add the missing variant before declaring the parser complete.

**Step 13 — Verify JSON parity (hard invariant).** `remove_banners` must not change what the parser extracts. The Phase 1 reference snapshot (`/tmp/<stem>.ref.json`, captured before any `remove_banners` work) is the parity target.

For every test fixture:

1. Reset the test HTML: `cp papers/raw/<stem>.html papers/test/<stem>.html` (overwrites any in-place mutation `convert_html.py` made on prior runs).
2. Delete the prior `papers/test/<stem>_converted.json`.
3. Run `python scripts/convert_html.py papers/test/<stem>.html` — this applies `remove_banners` and re-parses.
4. `diff /tmp/<stem>.ref.json papers/test/<stem>_converted.json` — must be empty.

A non-empty diff means a chrome-strip selector ate parseable content (most often: the parser was reading a caption, a section heading, or a reference-list element that `remove_banners` removed). Tighten the offending selector — match more narrowly, exclude the parsed element, or move the strip to a visibility-only override instead of DOM removal so the parser still sees the source bytes — and re-run. The parity check must pass on every fixture before the parser is considered complete.

### Helpers

Available in `_helpers.py`:

- `remove_elements_by_id(html, *ids)` — stable ids. Matches both quoted and unquoted `id=` attributes.
- `remove_elements_by_selector(html, *class_substrings)` — `<div>` with double-quoted class containing the substring.
- `_remove_nested_element(html, start_pattern)` — arbitrary opening-tag regex. Use when the above helpers don't match (non-`<div>` tags, unquoted attrs). Removes ONE occurrence per call — wrap in a loop if multiple instances may exist.
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

### Visual Handoff

Open the converted HTML in papers/test/ and raw HTML in papers/raw/ side-by-side in different CDP browser windows at vw=720. Hand off to the user for inspection.

## Resetting test files

`convert_html.py` mutates HTMLs in `papers/test/` (banner removal applied in place). To re-run a parser from a clean state, copy a fresh `papers/raw/<stem>.html` back to `papers/test/<stem>.html`, deleting any prior `papers/test/<stem>_converted.json` first. There is no separate reference-snapshot directory — `papers/raw/` is the canonical source.
