"""Detect vertical empty bands AND measure column margins in a formatted HTML
via full-page screenshot.

Bypasses DOM/HTML inspection entirely. The page is captured as it actually
paints; rows whose sampled columns are all near-white are treated as empty.

Two scans run per viewport:

  - **Gap scan** — vertical bands where every pixel in every row is
    near-white. Catches removed-element vacancies, layout-fix artifacts,
    and heavy figure/table reservations that no longer carry visible
    content. (Tight-transition detection — section header with no space
    above — is out of scope; that needs a band-density classifier.)
  - **Column-margin scan** — for each non-empty row, the leftmost and
    rightmost non-white pixel are measured. Aggregated to the worst-case
    (smallest) left/right margins page-wide and the corresponding content
    column width. Verifies that the body cap and any width overrides
    actually fit content inside the viewport with non-zero gutters at
    every viewport tested (Phase 2 Step 11).

Usage:
  PORT=9301 python scan_gaps.py <parser> <html> [vw1 vw2 ...] [--threshold N]

Defaults: viewports 720 1200; threshold 80 px (gaps below this are noise).

All pixel columns in each row are checked via numpy. A row counts as
empty when its minimum channel value across all columns is >= NEAR_WHITE
(default 250) — every pixel in the row is near-white. This catches
true gaps and excludes mostly-white figure backgrounds that still have
sparse dark content (lines, axis ticks, data points).
"""
import base64
import importlib
import json
import os
import sys
import time
import urllib.request
from io import BytesIO

import numpy as np
import websocket
from PIL import Image

PORT = int(os.environ.get("PORT", "9998"))
DEFAULT_VIEWPORTS = (500, 720, 1200)
DEFAULT_THRESHOLD = 80
NEAR_WHITE = 250
CHUNK_HEIGHT = 8000  # Chromium silently corrupts captureBeyondViewport
                     # screenshots above ~16K px; chunk at half that

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
))))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))


def cdp_call(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            return msg


def scan_for_gaps(img, threshold):
    """Vectorized: a row is empty iff every pixel in the row is near-white
    (min channel value across the entire row >= NEAR_WHITE)."""
    arr = np.asarray(img.convert("RGB"))             # (H, W, 3)
    row_min = arr.min(axis=(1, 2))                    # (H,) — darkest channel of any pixel in row
    is_empty = row_min >= NEAR_WHITE                  # (H,) bool
    # Run-length-encode the True bands.
    gaps = []
    in_gap = False
    gap_start = 0
    h = arr.shape[0]
    for y in range(h):
        if is_empty[y]:
            if not in_gap:
                in_gap = True
                gap_start = y
        else:
            if in_gap:
                if y - gap_start >= threshold:
                    gaps.append((gap_start, y))
                in_gap = False
    if in_gap and h - gap_start >= threshold:
        gaps.append((gap_start, h))
    return gaps


def scan_bg_around_column(img, main_x, main_right):
    """Sample pixel bands just outside and far outside the main reading
    column to detect colored backgrounds and box-shadows that the DOM
    ancestor-chain scan can miss (background-image gradients/patterns,
    box-shadow, pseudo-element backdrops, z-index sibling overlays).

    Two bands per side:
      - near band  (5-30 px outside column edge) — captures box-shadow
      - far band   (50+ px out to viewport edge) — captures the page-level
                    background color seen behind main

    Returns a dict with per-band median RGB tuples and a verdict string.
    Verdicts (any combination):
      - 'page-bg-colored' — far-band median is not near-white
      - 'shadow' — near-band median differs from far-band median (shadow
                   falloff or visible border)
      - 'clean' — both bands near-white and matching
    """
    arr = np.asarray(img.convert("RGB"))
    h, w = arr.shape[:2]
    main_x = max(0, min(w, main_x))
    main_right = max(0, min(w, main_right))
    if main_right - main_x < 50:
        return {"verdict": "no-main-column"}

    # Only sample rows where the main column has some non-white pixel
    # (where the article actually has content — skip blank top/bottom).
    main_band = arr[:, main_x:main_right]
    main_min = main_band.min(axis=(1, 2))
    rows_with_content = main_min < NEAR_WHITE
    if not rows_with_content.any():
        return {"verdict": "no-main-content"}

    band_specs = [
        ("near_left", max(0, main_x - 30), max(0, main_x - 5)),
        ("far_left", 0, max(0, main_x - 50)),
        ("near_right", min(w, main_right + 5), min(w, main_right + 30)),
        ("far_right", min(w, main_right + 50), w),
    ]
    bands = {}
    for label, x0, x1 in band_specs:
        if x1 - x0 < 5:
            bands[label] = None
            continue
        sub = arr[rows_with_content, x0:x1].reshape(-1, 3)
        if sub.size == 0:
            bands[label] = None
            continue
        med = tuple(int(v) for v in np.median(sub, axis=0))
        bands[label] = med

    def is_near_white(rgb):
        return rgb is not None and min(rgb) >= NEAR_WHITE

    def colors_differ(a, b, tol=8):
        if a is None or b is None:
            return False
        return max(abs(a[i] - b[i]) for i in range(3)) >= tol

    far_colored = (not is_near_white(bands["far_left"])
                   or not is_near_white(bands["far_right"]))
    shadow_left = colors_differ(bands["near_left"], bands["far_left"])
    shadow_right = colors_differ(bands["near_right"], bands["far_right"])

    verdicts = []
    if far_colored:
        verdicts.append("page-bg-colored")
    if shadow_left or shadow_right:
        verdicts.append("shadow")
    if not verdicts:
        verdicts.append("clean")

    return {
        "main_x": main_x, "main_right": main_right,
        "content_rows": int(rows_with_content.sum()),
        "bands": bands,
        "verdict": "+".join(verdicts),
    }


def find_main_column(call):
    """Heuristic locate the main reading column at current viewport.
    Returns dict with x, right, w; or None if not found."""
    js = (
        "JSON.stringify((() => {"
        "  const selectors = ["
        "    'main.c-article-main-column', 'main', 'article',"
        "    '.article.fulltext-view', '.article-fulltext',"
        "    '.core-container', 'div.widget-items',"
        "    '#bodyContent', '#main-content', '[role=main]'"
        "  ];"
        "  let best = null, best_score = 0;"
        "  for (const sel of selectors) {"
        "    for (const el of document.querySelectorAll(sel)) {"
        "      const cs = getComputedStyle(el);"
        "      if (cs.display === 'none' || cs.visibility === 'hidden') continue;"
        "      const r = el.getBoundingClientRect();"
        "      if (r.width < 100 || r.height < 200) continue;"
        "      const text_len = (el.textContent || '').length;"
        "      if (text_len < 1000) continue;"
        "      const score = text_len * Math.min(r.width, 1000);"
        "      if (score > best_score) { best = el; best_score = score; }"
        "    }"
        "  }"
        "  if (!best) return null;"
        "  const r = best.getBoundingClientRect();"
        "  return {x: Math.round(r.x), right: Math.round(r.right),"
        "          w: Math.round(r.width)};"
        "})())"
    )
    r = call("Runtime.evaluate", {"expression": js, "returnByValue": True})
    val = r["result"]["result"]["value"]
    if not val:
        return None
    return json.loads(val)


def scan_column_margins(img):
    """Measure the worst-case (smallest) left/right white margins around
    rendered content, plus the resulting content column width.

    For every non-empty row, find the leftmost and rightmost non-near-white
    pixel. Aggregate to:
      - L = MIN leftmost-pixel-x across all content rows (worst-case
            content-edge encroachment on the left gutter)
      - R = vw - 1 - MAX rightmost-pixel-x  (same on the right)
      - W = (vw - 1 - max_right) ... actually rightmost - leftmost (the
            spanned content width)

    Returns (L, R, W, content_row_count). Returns (None, None, None, 0)
    when the page has no content rows (entirely blank).
    """
    arr = np.asarray(img.convert("RGB"))                  # (H, W, 3)
    pixel_min_per_col = arr.min(axis=2)                   # (H, W)
    is_dark = pixel_min_per_col < NEAR_WHITE              # (H, W) bool
    row_has_content = is_dark.any(axis=1)                 # (H,)
    content_rows = is_dark[row_has_content]               # (Hc, W)
    if content_rows.shape[0] == 0:
        return None, None, None, 0
    h, w = arr.shape[:2]
    # Per-row leftmost / rightmost non-white pixel x
    col_idx = np.arange(w)
    # mask non-content cols with sentinel for argmin/argmax
    left_per_row = np.where(content_rows, col_idx[None, :], w).min(axis=1)
    right_per_row = np.where(content_rows, col_idx[None, :], -1).max(axis=1)
    L = int(left_per_row.min())                # smallest left margin
    R = int(w - 1 - right_per_row.max())       # smallest right margin
    W = int(right_per_row.max() - left_per_row.min() + 1)
    return L, R, W, int(content_rows.shape[0])


def text_near_y(call, y_top, y_bottom, vw):
    """Use CDP to find what text is just above/below a gap."""
    js = (
        "JSON.stringify((() => {"
        f"  const top = {y_top}, bot = {y_bottom};"
        "  function nearestText(yTarget, dir) {"
        "    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);"
        "    let n, best = null, bestDist = Infinity;"
        "    while ((n = w.nextNode())) {"
        "      if (!n.nodeValue || !n.nodeValue.trim()) continue;"
        "      const rg = document.createRange(); rg.selectNodeContents(n);"
        "      for (const r of rg.getClientRects()) {"
        "        if (r.width < 1 || r.height < 1) continue;"
        "        const y = (dir === 'above' ? r.bottom : r.top) + window.scrollY;"
        "        const dist = (dir === 'above' ? yTarget - y : y - yTarget);"
        "        if (dist < 0 || dist > bestDist) continue;"
        "        bestDist = dist;"
        "        best = n.nodeValue.replace(/\\s+/g,' ').trim().slice(0,40);"
        "      }"
        "    }"
        "    return best;"
        "  }"
        "  return {above: nearestText(top, 'above'), below: nearestText(bot, 'below')};"
        "})())"
    )
    r = call("Runtime.evaluate", {"expression": js, "returnByValue": True})
    return json.loads(r["result"]["result"]["value"])


def main(parser_name, html_path, viewports, threshold):
    parser = importlib.import_module(f"html_parsers.{parser_name}")
    raw = open(html_path).read()
    fmt_path = "/tmp/_scan_gaps.formatted.html"
    open(fmt_path, "w").write(parser.remove_banners(raw))

    tabs = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/json").read())
    tab = next(t for t in tabs if t.get("type") == "page")
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60)
    mid = [0]

    def call(m, p=None):
        mid[0] += 1
        return cdp_call(ws, mid[0], m, p)

    call("Page.navigate", {"url": f"file://{fmt_path}"})
    time.sleep(2.5)
    # Scroll-trigger lazy images
    for y in (0, 4000, 8000, 12000, 16000, 24000, 32000, 0):
        call("Runtime.evaluate", {"expression": f"window.scrollTo(0,{y})"})
        time.sleep(0.25)
    time.sleep(1)

    for vw in viewports:
        call("Emulation.setDeviceMetricsOverride", {
            "width": vw, "height": 900,
            "deviceScaleFactor": 1, "mobile": False,
        })
        time.sleep(0.5)
        call("Runtime.evaluate", {"expression": "window.scrollTo(0,0)"})
        time.sleep(0.5)
        doch = call("Runtime.evaluate", {
            "expression": "document.documentElement.scrollHeight"
        })["result"]["result"]["value"]
        # Chunk capture: Chromium's captureBeyondViewport silently
        # tiles/corrupts screenshots above ~16K px. Capture in
        # CHUNK_HEIGHT-px slices and vertically concatenate.
        chunks = []
        y = 0
        while y < doch:
            h = min(CHUNK_HEIGHT, doch - y)
            res = call("Page.captureScreenshot", {
                "captureBeyondViewport": True,
                "format": "png",
                "clip": {"x": 0, "y": y, "width": vw,
                         "height": h, "scale": 1},
            })
            chunks.append(np.asarray(
                Image.open(BytesIO(base64.b64decode(
                    res["result"]["data"]))).convert("RGB")
            ))
            y += h
        arr_full = np.concatenate(chunks, axis=0)
        img = Image.fromarray(arr_full)
        gaps = scan_for_gaps(img, threshold)
        L, R, W, n_rows = scan_column_margins(img)
        margin_str = (f"L={L} R={R} W={W} (content_rows={n_rows})"
                      if L is not None else "no content rows")
        # Background-around-column scan: locate main column, sample bands.
        main = find_main_column(call)
        if main:
            bg = scan_bg_around_column(img, main["x"], main["right"])
            bands = bg.get("bands", {}) or {}
            band_str = " ".join(
                f"{k}={v}" if v else f"{k}=-"
                for k, v in bands.items()
            )
            bg_str = f"main=({main['x']},{main['right']}) {band_str}  → {bg.get('verdict')}"
        else:
            bg_str = "no-main-column"
        print(f"vw={vw} docH={doch} img={img.size[0]}x{img.size[1]} "
              f"gaps_>={threshold}px: {len(gaps)}  margins: {margin_str}")
        print(f"  bg: {bg_str}")
        for y_top, y_bot in gaps[:25]:
            ctx = text_near_y(call, y_top, y_bot, vw)
            above = ctx.get("above") or "(none)"
            below = ctx.get("below") or "(none)"
            print(f"  y={y_top}-{y_bot} (h={y_bot - y_top}px)  "
                  f"above: '{above}'  below: '{below}'")
    call("Emulation.clearDeviceMetricsOverride")
    ws.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    threshold = DEFAULT_THRESHOLD
    if "--threshold" in args:
        i = args.index("--threshold")
        threshold = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    if len(args) < 2:
        sys.exit("Usage: scan_gaps.py <parser> <html> [vw1 vw2 ...] "
                 "[--threshold N]")
    parser_name = args[0]
    html_path = args[1]
    viewports = [int(a) for a in args[2:]] if len(args) > 2 else list(
        DEFAULT_VIEWPORTS)
    main(parser_name, html_path, viewports, threshold)
