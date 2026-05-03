---
name: format-html
description: Reading-layout contract for the remove_banners function in html_parsers/. Use when normalizing a publisher's saved HTML into a single centered text column (~752 px cap, 56 px top/bottom + 16 px side padding).
paths: html_parsers/**/*.py
---

## Goal

For every publisher parser under `html_parsers/`, implement `remove_banners(html)` so that when a reader opens `papers/<stem>.html` in any browser at any viewport width, they see a single centered reading column — white background, publisher chrome stripped, article title/body/references preserved.

`parse_article` output must be bit-identical before and after a `remove_banners` change — the function only alters presentation. Reference implementations: `html_parsers/eurekaselect.py` and `html_parsers/jove.py`.

**Preserve native typography.** Unless the user explicitly asks to change it, do not override the publisher's font family, font size, line height, letter spacing, or paragraph spacing. `remove_banners` strips chrome and caps the reading column — it does not restyle text. CSS you inject should target layout (width, margin, padding, display, float, background) and visibility, not typography.

## The target (measurement spec)

Open the cleaned HTML and run the text-bounds snippet (§ Verification → Visual verification) that walks text nodes and reports L/R/T/B/width. At every viewport:

| Property | Target                                          |
|----------|-------------------------------------------------|
| `L`      | `max(16, (vw − 720) / 2)`                       |
| `R`      | equal to `L`                                    |
| `T`      | `56`                                            |
| `B`      | `56`                                            |
| `width`  | `vw − 32` for `vw ≤ 752`, else `720`            |

Tolerance: ±4 px. A parser is done when every one of 600/720/820/1024/1280/1600/1920 satisfies all five rows simultaneously.

### How to measure T and B around boxed content

The 56-px top/bottom rule is normally applied to the rendered text bounds — first/last text node's bounding rect. There is one exception: when the first or last reading-content element renders **with a visible box border** (a card-style wrapper with a border line drawn around the text), measure the 56 px to the *box border* edge, not to the text inside it. The box itself is the visible top/bottom of the reading column at that point.

- No visible box: `T` = `56` from doc top to first text rect; `B` = `56` from last text rect to doc bottom.
- Visible box border: `T` = `56` from doc top to the top border line; `B` = `56` from the bottom border line to doc bottom.

This matters for publishers who put the article-header chrome in a bordered card (e.g. JoVE) or who keep a journal-meta footer in a bordered band: the visual "edge of the column" at top/bottom is the box border, not the inset text.

## Quality check workflow

Five artifacts are checked, in order. Stop at the first failure and fix before proceeding.

1. **Per-viewport target bounds.** `L / R / T / B / W` against the format-html spec at each viewport, ±4 px tolerance.
2. **Cross-viewport layout diff.** Compare the layout at the vw=720 reference against each wider viewport — flag any element whose `display` mode flipped or whose width-to-body ratio shifted significantly. This catches alternative-layout regressions the bounds check misses (the page can pass `L/R/T/B/W` at every viewport while serving a completely different layout at narrow vs wide — gray-box wrapper height jumping 89→321 px, metrics widget switching horizontal→vertical sidebar, journal banner flipping inline-block→block).
3. **Native-parity at vw=720.** Compare formatted's element heights and positions against the raw HTML rendered at vw=720 (the spec reference). Flags elements whose size or relative-top shifted by ≥4 px between raw and formatted — meaning your CSS introduced or deviated from the publisher's native vertical rhythm. **Caveat:** the script also flags intentional changes (DOM-stripped chrome shifts sibling positions; accordion-expanded content was previously hidden; column-cap width change reflows text). Triage the flagged elements: real issues are CSS rules you added that move/resize elements without a stripping/expansion explanation; ignore the noise.
4. **JSON parity.** `parse_article` output before vs after — must be bit-identical (`remove_banners` is presentation-only).
5. **DOM-strip audit.** `temp/strip_audit.py` flags content elements whose count dropped — a guard against over-removing chrome strips that swept in real article content.

A parser is done only when all five pass (with check 3 caveats accepted).

Scripts:
- `scripts/measure_file.py` — runs checks 1 and 2 in one pass against an already-formatted file (post-`convert_html.py`).
- `scripts/measure_layout.py` — check 1 only, applies `remove_banners` itself for iterative parser development before `convert_html.py` writes back.
- `scripts/compare_to_raw.py` — check 3. Takes raw HTML path (typically `papers_ref/<stem>.html`) and formatted HTML path (`papers/<stem>.html`), reports resized + relocated elements relative to a common-id anchor at vw=720.

The work loop:

1. **Pick a representative HTML.** One whose `detect_domain()` matches the parser. Prefer a paper with a live `papers/<stem>.json` to use as the parity baseline. If `.json` doesn't exist: `python convert_html.py papers/<stem>.html`.

2. **Snapshot JSON parity baseline.**
   ```bash
   cp papers/<stem>.json /tmp/<stem>.ref.json
   ```

3. **Develop `remove_banners`** following the 4-step workflow below.

4. **Measure layout (target bounds + cross-viewport diff).** Two scripts cover this depending on where you are in the loop.

   For iterative parser development (before `convert_html.py` writes the formatted HTML back to `papers/`):
   ```bash
   python .claude/skills/format-html/scripts/measure_layout.py <parser> papers/<stem>.html
   ```
   Applies `remove_banners` and writes the result to `/tmp/<parser>_formatted.html`, then reports `L / R / T / B / W` at every viewport against the target spec.

   After `convert_html.py` has run (or for a final verification pass against an already-formatted file):
   ```bash
   python .claude/skills/format-html/scripts/measure_file.py papers/<stem>.html
   ```
   Reads the file as-is, reports both per-viewport target bounds (check 1) AND cross-viewport layout diff (check 2). Both scripts require a Chrome/Edge instance with `--remote-debugging-port=9998` (override via `PORT` env var).

5. **Diagnose any T/B/L/R/W failure.** `scripts/probe_text_chain.py` walks from the topmost (or bottommost) rendered text node up to `<body>`, dumping each ancestor's bounding rect plus computed margin/padding. The element whose `mt`/`pt` (or `mb`/`pb`) sums to the overshoot is the one to override.
   ```bash
   python .claude/skills/format-html/scripts/probe_text_chain.py <parser> papers/<stem>.html 720 first
   python .claude/skills/format-html/scripts/probe_text_chain.py <parser> papers/<stem>.html 720 last
   ```

6. **Fix bugs and iterate.** See § Common failure modes for recipes.

7. **Verify JSON parity.**
   ```bash
   rm papers/<stem>.json
   python convert_html.py papers/<stem>.html
   diff /tmp/<stem>.ref.json papers/<stem>.json    # must be empty
   ```
   Non-empty diff = the change touched article content. Revert and find a stricter selector. **Note:** `convert_html.py` writes the `remove_banners` output back to `papers/<stem>.html` in place. To re-run a clean test, restore the source from `papers_ref/<stem>.html` first.

8. **Verify at all viewport widths.** Re-run `measure_layout.py` and confirm `L / R / T / B / W` pass at every one of 600/720/820/1024/1280/1600/1920.

9. **Run the DOM-strip audit.** Review every "suspicious" signature flagged. See § DOM-strip audit.

## The 4-step workflow

Publishers ship responsive layouts that reshape themselves based on the viewport width. Fighting the desktop layout element-by-element is brittle; every publisher's desktop CSS has different grid / float / flex quirks, and every change you make is one media query away from regressing. **The 720-px layout is uniformly simpler**: at narrow viewport the publisher's own CSS already collapses sidebars, removes floating masthead bands, and stacks the article into a single column. So the right workflow is to pin the page to the 720-px layout *once* and then strip the few remaining chrome elements from that clean starting point.

### Step 1 — Render at vw = 720

Open the saved HTML in a browser resized to 720 px width. This is the baseline you will match at every other viewport. At 720 px most publishers:

- drop the right sidebar entirely
- collapse the left sidebar into a hamburger button
- stack title / authors / abstract / body / references vertically
- reduce or hide the full-bleed masthead band
- keep only a minimal top bar (logo + menu trigger) and a footer

Whatever you see at 720 is the layout the reader will end up seeing at every viewport after this function is done.

### Step 2 — Freeze the 720-px layout

Before removing anything, make the HTML render as if vw = 720 **regardless of the actual viewport**. Two layers; use the heaviest one only when the lighter one isn't enough.

#### 2a. Cap the body

Fluid-with-cap body — caps the page-level wrapper at 752 px:

```css
html {
  width: 100% !important;
  max-width: 100% !important;
  margin: 0 !important;
  background: #fff !important;
}
body {
  width: 100% !important;
  min-width: 0 !important;
  max-width: 752px !important;
  margin: 0 auto !important;
  background: #fff !important;
  color: #000 !important;
}
```

This freezes the *page-level* width to 720 inside the wrapper. But it does **not** cancel `@media` queries — those still fire on the real viewport, so a publisher that uses `@media (min-width: 1025px)` to switch column grid, reveal a journal-cover thumbnail, or change a metrics widget from horizontal to vertical will still serve the desktop variant at wider viewports. If `measure_layout.py` reports the same numbers at every viewport, you're done — the body cap was sufficient. If you see different layouts between vw=720 and vw=1280 (different gray-box height, different metrics arrangement, banner wrapping that doesn't happen at 720), you need 2b.

#### 2b. Neutralize publisher viewport-width @media queries

Rewrite every `<style>` block in the captured HTML so the publisher's CSS only ever applies its narrow form, regardless of the real viewport:

- delete every `@media (min-width: N) { rules }` for N ≥ 1025 (desktop rules never fire)
- unwrap every `@media (max-width: N) { rules }` for N ≥ 720 (narrow rules apply unconditionally)
- leave every other media query alone (orientation, `prefers-color-scheme`, print, mobile-only `<720` breakpoints, mixed-feature ranges)

Wired up as the first step of `remove_banners`:

```python
from .._helpers import _remove_nested_element, ...
# Or, if not yet promoted to _helpers, copy the two helpers from
# .claude/skills/format-html/scripts/neutralize_media.py:
#   _scan_balanced_block(text, open_idx)
#   _neutralize_css(css)
# and a top-level _STYLE_RE.sub() wrapper.

def remove_banners(html):
    html = _neutralize_media_queries(html)   # Step 2b
    html = _remove_nested_element(html, ...)  # Step 3 chrome strips
    ...
```

A reference implementation is at `.claude/skills/format-html/scripts/neutralize_media.py` — the `neutralize_media_queries(html)` function is publisher-agnostic and free of external dependencies.

When this runs successfully you should see the publisher's CSS file shrink dramatically (e.g. tandfonline: 26 of 31 desktop blocks deleted; 74 of 86 narrow blocks unwrapped). The cumulative effect of dozens of viewport-gated rules — grid switches, column collapse, padding adjustments, font-size bumps — collapses into a single transform.

**When to use 2b instead of piling up 2a-style overrides:** if you find yourself adding three or more per-element rules to suppress desktop-only behavior (e.g. `.foo{display:inline-block !important}`, then `.bar{padding:0 !important}`, then `.baz::before{content:"/" !important}`), stop adding rules and apply the neutralizer instead. The neutralizer fixes all of them at once and makes future similar issues impossible.

### Step 3 — Strip chrome

With the layout frozen, remove every non-text element. Five categories — they cover every chrome removal across the corpus:

| Category            | Typical selectors (non-exhaustive)                                     |
|---------------------|------------------------------------------------------------------------|
| **Top blocks**      | `<header>`, cookie banners (`#onetrust-consent-sdk`, `#CybotCookiebotDialog`, `#usercentrics-cmp-ui`, `.cookie-*`), breadcrumbs, leaderboard ads, status/beta banners |
| **Bottom blocks**   | `<footer>`, post-article CTAs (sign-up, newsletter), "Related articles", "Cited by", "We recommend", next-prev article nav |
| **Side columns**    | Left sidebar (article nav, TOC, download panel), right sidebar (metrics, figures-jump, cover thumbnail, share), collapsed mobile nav |
| **Floating blocks** | `position: fixed/sticky/absolute` elements: floating article toolbars, jump-to-section menus, dismiss buttons, modal overlays, "Skip to main content" |
| **Colored backgrounds** | Branded masthead strips, colored section dividers, ribbon bars — anything whose `background-color` isn't white/transparent and isn't a figure/table border |

**Prefer DOM removal over CSS `display:none`.** Removal is the default — `display:none` leaves dead DOM in the saved file. Hide only when the target has no stable selector or when removal would collapse a flex/grid layout the wrapper depends on.

Use the helpers in `_helpers.py`:

- **`remove_elements_by_id(html, *ids)`** — stable ids. Matches both quoted and unquoted `id=` attributes.
- **`remove_elements_by_selector(html, *class_substrings)`** — `<div>` with double-quoted class containing the substring. Does not match other tags or unquoted class attrs.
- **`_remove_nested_element(html, start_pattern)`** — arbitrary opening-tag regex. Use when the helper doesn't match (non-div tags, unquoted attrs, inline-style matching). Removes ONE occurrence per call — wrap in a loop (up to ~10 iterations) if multiple instances may exist.

Anchor class patterns with `\b` so `accessbar-sticky` doesn't also match `accessbar-sticky-foo`. Don't let class-terminator character classes like `["'\s>]` consume the `>` — that swallows the next sibling element.

### Step 4 — Cap the main text column

The "main text column" is the **highest** common ancestor of title + authors + affiliations + abstract + body + figure captions + references. Capping a deeper wrapper leaves the abstract or authors at full bleed (a real bug we hit on ACS).

Inject one CSS block on that wrapper:

```css
YOUR_MAIN_WRAPPER_SELECTOR {
  float:       none !important;
  display:     block !important;
  width:       auto !important;
  max-width:   752px !important;
  margin:      0 auto !important;
  padding:     56px 16px !important;   /* 56 top/bottom, 16 left/right */
  box-sizing:  border-box !important;
  background:  #fff !important;
}
```

Geometry produced by this rule, with the Step 2 layout freeze in place:

- `vw ≤ 752` — wrapper fills the viewport; text content is `vw − 32` wide, left/right margins = 16.
- `vw > 752` — wrapper caps at 752 px, centered; text content = 720 wide, left/right margins grow symmetrically as `(vw − 752) / 2 + 16`.
- `padding: 56px 16px` gives 56 px of breathing room above the first rendered character and below the last, matching the sides to the cleaner rhythm of desktop reading.

If the natural stylesheet leaves padding / margin on an inner wrapper of the capped element, the effective text column shrinks below 720 px. Zero the offenders:

```css
YOUR_INNER_SECTION_OR_ROW {
  margin:     0        !important;
  padding:    0        !important;
  max-width:  none     !important;
  width:      auto     !important;
  box-sizing: border-box !important;
}
```

First-/last-child margin stacking can push the first rendered text past 56 from the wrapper top or bump the bottom gap past 56. Zero those where needed — but **always use the direct-child combinator `>`**, never the descendant form (see § Pitfalls):

```css
YOUR_MAIN_WRAPPER_SELECTOR > *:first-child { margin-top:    0 !important; padding-top:    0 !important; }
YOUR_MAIN_WRAPPER_SELECTOR > *:last-child  { margin-bottom: 0 !important; padding-bottom: 0 !important; }
```

**Handle start/end anchors.** If per-publisher notes say "text starts at X" / "text ends at Y", remove every element inside the wrapper before X (or after Y). The `parse_article` output must not change — only visual chrome can go.

## Figure layout

The reading column rules above govern the text. Figures get an additional contract: every figure renders **full-column-width with the caption directly below the image**, separated by a small gap. No side-by-side image+caption layouts (table cells, flex rows). No thumbnail-sized inline images that float at native pixel dimensions inside a wider container.

Reference parsers that already implement this: `annualreviews.py`, `biorxiv.py`, `iucr.py`, `cshlp.py`, `jci.py`, `plos.py`. Modeled selectors and the buffer-handling pattern come from these files.

CSS goes inside the same `<style>` block injected by `remove_banners`, after the column-cap rules. Standard template — replace `WRAPPER`, `FIGURE_SEL`, `IMG_SEL` with publisher-specific selectors:

```css
/* Block-stack figures whose native layout is side-by-side (table / flex). */
:root WRAPPER FIGURE_SEL,
:root WRAPPER FIGURE_SEL > * {
    display: block !important;
    width: 100% !important;
    text-align: left !important;
    box-sizing: border-box !important;
}

/* Image: full-column width, above caption, small gap. */
:root WRAPPER IMG_SEL {
    display: block !important;
    width: 100% !important;
    height: auto !important;
    max-width: 100% !important;
    margin: 0 0 5px 0 !important;
}

/* Zero parent padding/margin that would shave width off the image. */
:root WRAPPER FIGURE_SEL > a,
:root WRAPPER FIGURE_SEL .img-wrapper {
    padding: 0 !important;
    margin: 0 !important;
}
```

The 5 px `margin-bottom` is the iucr / jci convention. When the publisher's stylesheet already provides a vertical buffer between image and caption (e.g. `padding-bottom` on the figure container that visually sits below the image cell), omit the bottom margin and instead zero whichever inherited side padding was shaving the image width — the publisher's native buffer survives unmodified that way. iucr `table.fig` is the canonical example: native `padding-bottom: 5px` is moved from the table to `img.figlnkthm` `margin-bottom: 5px` so the gap appears below the image instead of below the (block-stacked) caption row.

**Scope every selector to the publisher's figure container.** Bare `img { width: 100% }` is forbidden — it catches inline icons, journal-meta logos, and author-tooltip avatars, blowing them up to column width. Always anchor with the figure-wrapper class or the article-content id.

**`:root` prefix is usually needed.** Publisher figure wrappers ship with single-class CSS rules that beat unprefixed `!important`. The 0-1-0 boost from `:root` is the smallest hammer that wins; only escalate to `body main` or id-prefixed selectors when even that fails.

**Layout-only, never typography.** Caption text styling (font, size, line-height) stays as the publisher set it. The image-formatting CSS only touches `display`, `width`, `height`, `max-width`, `margin`, `padding`, `box-sizing`, `text-align`.

**Capture-time prerequisite.** This CSS only displays whatever `<img>` bytes are inlined in the saved HTML. If the inlined `src` is a low-res thumbnail, full-width display will simply scale up a small image — the figure looks blurry. The fix for thumbnail-only captures is in `get_refs.py`, not CSS — see develop-parser skill, §"Capture-time image resolution".

**Verification.** `measure_file.py` and `measure_layout.py` only check text bounds; they do not inspect image rendering. Add an explicit visual check to the workflow: open the HTML at vw=720 in the CDP browser, confirm each figure renders full-column-width with the caption directly below and a visible gap. Browse 1–2 representative papers per publisher.

## Injection point

```python
override = "<style>...</style>"
if "</head>" in html:
    html = html.replace("</head>", override + "</head>", 1)
else:
    html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
```

Some SingleFile outputs (ACS) omit `</head>`; fall back to injecting before `<body`. Order inside `remove_banners`: Step 2 layout-freeze CSS → Step 3 structural removals → Step 4 column-cap CSS injection.

## Specificity ladder (lowest → highest)

When a publisher's class rule beats a generic wrapper rule despite `!important`, climb the ladder:

| Selector form                              | Specificity |
|--------------------------------------------|-------------|
| `main`                                     | 0-0-1       |
| `main *`                                   | 0-0-2       |
| `body main`                                | 0-0-2       |
| `body main *`                              | 0-0-3       |
| `.some-class`                              | 0-1-0       |
| `:root body main`                          | 0-1-2       |
| `:root body main *`                        | 0-1-3       |

Prefix `:root` to beat single-class publisher rules without needing to introduce id or attribute selectors.

## Verification

### JSON parity (hard invariant)

Formatting changes must not alter parser output. Capture a reference **before** touching `remove_banners`, re-convert **after**, require a bit-identical diff.

1. Ensure `papers/<stem>.json` exists.
2. Copy it to `/tmp/<stem>.ref.json`.
3. Make the `remove_banners` change.
4. Delete the on-disk JSON and run `python convert_html.py papers/<stem>.html`.
5. `diff /tmp/<stem>.ref.json papers/<stem>.json` must be empty.

Do this for every paper being migrated, not just one — per-paper layout variations surface only in a subset.

### DOM-strip audit (over-removal check)

JSON parity catches content that `parse_article` reads, but `remove_banners` can silently over-remove elements the parser doesn't touch (a figure, a body paragraph, an acknowledgements block). The audit compares element **signatures** — `(tag, id, first-class-token)` tuples — between raw and cleaned HTML and reports every signature whose count dropped, then flags the ones that look like content rather than chrome.

Heuristic for "content-tag" signatures: the tag is in `{h1..h6, p, article, section, figure, figcaption, table, tbody, thead, tr, td, th, blockquote, dl, dt, dd, code, pre}` AND neither the class nor the id matches known-chrome tokens (`nav|toolbar|widget|footer|header|sidebar|menu|crumb|masthead|cookie|gdpr|consent|promo|ad-|advert|recommend|related|metrics|cited-?by|share|social|tooltip|modal|popup|dialog|dropdown|breadcrumb|pagination|alert-|banner|search|signin|sign-up|login|donation|cta|skip|visually|hidden|offscreen|util-|utility|cover|datalist|accessbar|jump|subscribe|newsletter|outline|overlay|trendmd|altmetric|dimensions|plumx`).

Run:

```bash
python temp/strip_audit.py <parser> <html_path>
```

and review the "suspicious" list. Every flagged signature should fall into one of:

- **Duplicate content clone** — the source HTML ships two copies (one inline, one in a sidebar / modal / hidden data-extraction list). The visible copy must survive.
- **Chrome my regex missed** — legit chrome with class/id outside the heuristic pattern. Either widen the heuristic or accept the false positive.
- **Empty placeholder** — element with no visible text (e.g. `<h3 class=article-title>` with zero-length content).

If a flagged removal doesn't fit, it's a real over-removal bug: tighten the `_remove_nested_element` regex or scope it by id instead of class.

### Visual verification (mandatory)

JSON parity says nothing about what the page looks like — CSS tweaks, untouched sibling chrome, and post-article footers are all invisible to the parser. Measure the **actual rendered text**, not the wrapper — the wrapper can be 752 px while inner text collapses or overflows.

`scripts/measure_layout.py` runs the measurement against your parser's `remove_banners` output via CDP. It walks every text node in `<body>`, filters out fixed/sticky/absolute subtrees, takes the union of `getClientRects()`, and reports `L / R / T / B / W` per viewport against the target table.

```bash
python .claude/skills/format-html/scripts/measure_layout.py wiley papers/Arudchandran_2000_Genes_Cells_11029655.html
```

Defaults to viewports 600/720/820/1024/1280/1600/1920 — pass widths as additional CLI args to override. Each row prints values flagged `✓` (within ±4 of target) or `~target`.

Diagnose any failure with `scripts/probe_text_chain.py`:

```bash
python .claude/skills/format-html/scripts/probe_text_chain.py wiley papers/<stem>.html 720 first  # for T failure
python .claude/skills/format-html/scripts/probe_text_chain.py wiley papers/<stem>.html 720 last   # for B failure
```

It walks from the topmost (`first`) or bottommost (`last`) rendered text node up to `<body>`, dumping each ancestor's bounding rect plus computed margin/padding. The element whose `mt`/`pt` (or `mb`/`pb`) sums to the overshoot is the one to override. Walk *outward* from the text — the closest ancestor with non-zero spacing usually owns the issue.

If no single ancestor explains the overshoot, the cause is typically (a) a sibling element pushing the text down or up, (b) line-box leading (see § Pitfalls 3), or (c) a viewport-gated layout switch (see § Step 2b — neutralize publisher viewport-width @media queries).

### Common failure modes

- `L ≠ R` — a sibling float/flex/grid off-axes the centered column. Add `float:none !important; flex:none !important` on the wrapper; collapse a grid parent to block via `div:has(> WRAPPER) { display:block !important }`.
- `width < vw − 32` at narrow vw — an inner row/section still has horizontal padding or margin. Add the offender to a descendant-zero rule.
- `T > 56` — chrome above the first text. Options: remove the offending element with a helper; or if it's an image/figure that must stay, zero its vertical margin via `WRAPPER > *:first-child { margin-top: 0 !important; padding-top: 0 !important }` (direct-child combinator only — see § Pitfalls).
- `B > 56` — chrome below the last text. Typically references-sidebar, comments, "Cited by", "We recommend". Strip with a helper.
- Negative `R` at narrow vw — a descendant has a fixed pixel `width` that overflows the wrapper. Force `WRAPPER * { max-width: 100% !important }`. For SVG/canvas/highcharts subtrees, remove the subtree.
- Scrollbar eats 16 px on the right — `html { overflow-y: overlay } html::-webkit-scrollbar { width: 0 }`.
- Publisher class rule beats your rule despite `!important` — boost specificity by prefixing selectors with `:root` (adds 0-1-0 specificity without matching a real element). See § Specificity ladder.

## Pitfalls

Three recurring root causes of spacing and button-rendering regressions across parsers. Every one of them has the same underlying theme: **an over-broad CSS reset written to kill one specific effect silently nukes unrelated publisher rules throughout the cascade.**

### 1. Descendant combinator on first-/last-child resets

`WRAPPER *:first-child { margin-top: 0 }` matches every first-child at every depth in the subtree, not just the wrapper's first child. It zeros:

- every section heading (`h2/h3/h4` that happens to be first-child of its section) — section rhythm collapses, all sections run flush
- every first reference `<li>` — reference items stack against each other
- every first `<a>` of an inline-block button group — button chips (e.g. aacrjournals figure "View large / Download slide") abut as a single run-on string
- the first element inside boxed metadata cards (e.g. science "Editor's summary" box) — the box's internal top padding collapses

The same rule on `:last-child` has a symmetric failure: `WRAPPER *:last-child { padding-bottom: 0 }` zeros the inner padding of every nested last-child including bordered boxes (wiley `.accordion__content` Bibliography panel, science "Editor's summary"), so the box's last child renders flush against the visible bottom border with no breathing room.

**Always use the direct-child combinator `>`**:

```css
WRAPPER > *:first-child { margin-top:    0 !important; padding-top:    0 !important; }
WRAPPER > *:last-child  { margin-bottom: 0 !important; padding-bottom: 0 !important; }
```

If the wrapper's outermost child still has nested margin-bottom cascading from many ancestor wrappers (62-68 px of trailing whitespace below the last reference even with the direct-child reset), use a descendant rule for **margin only**, never padding:

```css
WRAPPER *:last-child { margin-bottom: 0 !important; }
```

This kills the outer margin cascade without destroying inner box padding.

If the publisher's own CSS already zeros nested first/last-child margin (e.g. cshlp), drop the reset entirely rather than adding a redundant block.

### 2. Shorthand properties overwriting multi-axis values

Shorthand CSS properties assigned to kill one axis silently clobber the others:

- `padding: 0 !important` to kill horizontal inset also zeros vertical padding (pnas metadata rows collapsed from 127 px to 95 px). Use `padding-left: 0; padding-right: 0`.
- `*{ min-width: 0 !important }` to let flex children shrink also collapses table-cells and fixed-size badges (rsc journal-thumbnail cell went 50 → 16 px, science reference-number circles went 24 → 5 px). Apply `min-width: 0` only to the specific flex/grid containers that need it, and add per-element restorations where native layout relied on an explicit min-width.
- `margin: 0 !important` (vs `margin-top: 0`) has the same failure mode.

**Rule of thumb:** write axis-specific properties when you're trying to zero a single axis, and scope broad `*` rules to specific class/tag targets rather than the whole descendant tree.

### 3. Line-box descent below the visible glyph baseline

`measure_layout.py` reads the visible text bottom from `Range.getClientRects().bottom`, which is the line-box bottom (glyph descender + line-height leading), not the glyph baseline. When the last text is on a line whose `line-height` is taller than the glyph (typical for inline link rows like CAS / PubMed / Web of Science / Google Scholar), the line-box bottom can sit 7–15 px below the visible glyph. The wrapper's `padding-bottom: 56` then reads as `B = 56 + 7-15`, failing the ±4 tolerance.

Two interventions, in order of preference:

- **Hide the JS-driven sibling that's inflating the line.** Wiley's `.extra-links` flex row contains a `.getFTR__placeholder` div (40 px tall, JS-populated lookup-icon — empty in static capture) that pushes the row to 48 px even though the link text glyphs are 14 px tall. Hiding the placeholder collapses the row to glyph height and B drops to 56.
- **Compensate via padding-bottom.** If the descent is intrinsic to the line-height, set asymmetric vertical padding on the wrapper: `padding: 56px 16px 50px 16px`. Use the smallest correction that brings B into tolerance — 6 px for a single trailing line of text (sciencedirect copyright text), more for inline icon rows.

Don't reach for `line-height: 1` to flatten the line — it works on the specific element but degrades the visible spacing of the surrounding text.

### 4. JS-dependent state frozen in the saved snapshot

SingleFile captures a DOM whose scripts won't re-run; any "click to expand / see more / toggle" widget stays in its authored-default state. Publishers implement that default in several ways:

| Mechanism                                                | Fix                                                                                                     |
|----------------------------------------------------------|---------------------------------------------------------------------------------------------------------|
| `max-height: 0; overflow: hidden` + `::after` gradient mask (science figcaptions; wiley Bibliography)    | Override height/overflow AND `::after{ content: none; display: none }` — the gradient persists otherwise |
| `width: 0; height: 0; opacity: 0; visibility: hidden` on individual list items (frontiersin "See more" author list) | Unset every hiding property on the affected descendants                                                 |
| `hidden` HTML attribute on trailing list items (science references `[data-method=clamp]`)                | Strip the attribute from the DOM in `remove_banners` rather than fighting the `[hidden]` CSS rule       |
| Inline `style="display:none"` set by rendering template (wiley accordions)                               | CSS `display: block !important` overrides inline style when `!important` is present                    |
| Foundation-style `data-dropdown` / `data-reveal` widgets that render as flat text (mdpi action row)      | Widget is unrecoverable — strip the element as toolbar chrome                                           |

If the content matters, force-unhide via CSS. If it's interactive chrome with no static fallback, strip.

### Bonus: narrow-width responsive CSS hides desktop-only metadata

The 752-px body cap forces every page into the publisher's narrow/mobile responsive mode, which hides desktop-only metadata. Force those elements visible when the per-publisher note says they belong in the reading column:

- wiley `.volume-issue`, `.citation__page-range` (Volume 5, Issue 10 pp. 789-802)
- tandfonline `.issueSerialNavigation .cover img`, `.submitAnArticle`, `.jHomepage` (publication cover + Submit an article + Journal homepage)

Pattern: `display: inline-block !important; visibility: visible !important` on the hidden element, plus any height/width restoration the native desktop styles provided.
