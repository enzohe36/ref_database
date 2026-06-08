"""Detect sticky elements in a formatted HTML via multi-position scroll test.

Complements (does not replace) the static computed-style scan in Phase 2
Step 3. The static scan finds elements declared `position: fixed/sticky`
in CSS; this script catches:

  - JS-driven faux-sticky (element repositioned on every scroll event
    via `transform: translate3d(0, scrollY, 0)` or similar)
  - `position: sticky` elements that engage only past a scroll threshold
    (they look normal at scrollY=0 but stick once scrolled past)

Recipe (three layers):

  1. Snapshot every visible element's viewport-relative `top` at
     scrollY=0.
  2. Sweep multiple scroll positions (default 0, 500, 1500, 3000, 6000).
     Wait two `requestAnimationFrame` ticks after each scroll for layout
     commit + paint.
  3. For each element seen in every snapshot, compute the standard
     deviation of its viewport-relative top across positions. Elements
     with low std-dev (always near the same viewport row regardless of
     scroll) are sticky.
  4. Filter invisibles (display, visibility, opacity, w<5/h<5,
     off-screen). De-dupe to outermost.

Usage:
  PORT=9301 python scan_sticky.py <parser> <html>
"""
import importlib
import json
import os
import sys
import time
import urllib.request

import websocket

PORT = int(os.environ.get("PORT", "9998"))
DEFAULT_SCROLL_POSITIONS = (0, 500, 1500, 3000, 6000)
DEFAULT_VIEWPORT = (1200, 900)
STICKY_THRESHOLD = 0.85   # sticky_score > this → flagged

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


# Snapshot every visible element. Tag each with a unique `data-_sscan`
# attribute so we can identify the SAME element across snapshots even
# if the DOM grows (lazy-load).
_SNAPSHOT_JS = r"""
JSON.stringify((() => {
  const out = [];
  if (typeof window.__sscan_id !== 'number') window.__sscan_id = 0;
  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (parseFloat(cs.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) continue;
    if (r.bottom < -100 || r.top > window.innerHeight + 100) continue;
    if (!el.dataset._sscan) {
      el.dataset._sscan = String(++window.__sscan_id);
    }
    out.push({
      id: el.dataset._sscan,
      top: r.top,
      position: cs.position,
      tag: el.tagName.toLowerCase(),
      elId: el.id,
      cls: (el.className||'').toString().slice(0,80),
      w: Math.round(r.width),
      h: Math.round(r.height),
    });
  }
  return out;
})())
"""

# Two requestAnimationFrame ticks: first for layout commit, second for
# paint. A single rAF fires before layout commit on Chromium and the
# re-captured rects can match the old positions.
_DOUBLE_RAF = (
    "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
)


def _evaluate(call, expression, await_promise=False):
    return call("Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": await_promise,
    })


def detect_sticky(call, scroll_positions=DEFAULT_SCROLL_POSITIONS,
                  threshold=STICKY_THRESHOLD):
    """Multi-position scroll test. Returns list of sticky-element dicts."""
    snapshots = []           # list[ {id: top} ]
    metadata = {}            # id → first-seen element info

    for y in scroll_positions:
        _evaluate(call, f"window.scrollTo(0, {y})")
        _evaluate(call, _DOUBLE_RAF, await_promise=True)
        r = _evaluate(call, _SNAPSHOT_JS)
        elements = json.loads(r["result"]["result"]["value"])
        snap = {}
        for e in elements:
            snap[e["id"]] = e["top"]
            metadata.setdefault(e["id"], e)
        snapshots.append(snap)

    # Reset scroll
    _evaluate(call, "window.scrollTo(0, 0)")

    # Elements present in every snapshot
    common_ids = set(snapshots[0].keys())
    for s in snapshots[1:]:
        common_ids &= set(s.keys())

    # Stickiness score: 1 - stddev / scroll_quarter
    # Normal content's viewport-top changes by ΔY across scroll, so its
    # stddev across the 5 positions is ~scroll_total / sqrt(12). Sticky
    # content has stddev ~0. Normalize by scroll_total/4 so the score
    # is well within [0, 1] for normal content.
    scroll_total = max(scroll_positions) - min(scroll_positions)
    norm = max(1, scroll_total / 4)

    sticky = []
    for eid in common_ids:
        tops = [s[eid] for s in snapshots]
        mean = sum(tops) / len(tops)
        var = sum((t - mean) ** 2 for t in tops) / len(tops)
        stddev = var ** 0.5
        score = 1 - min(1, stddev / norm)
        if score > threshold:
            m = dict(metadata[eid])
            m["sticky_score"] = round(score, 3)
            m["top_range"] = [round(min(tops), 1), round(max(tops), 1)]
            sticky.append(m)

    if not sticky:
        return []

    # De-dupe to outermost: an element whose ancestor is also sticky
    # inherits the visual stickiness — report only the outermost.
    sticky_ids = [s["id"] for s in sticky]
    dedup_js = (
        "JSON.stringify((() => {"
        f"  const ids = new Set({json.dumps(sticky_ids)});"
        "  const outer = [];"
        "  for (const id of ids) {"
        "    const el = document.querySelector("
        "      `[data-_sscan='${id}']`);"
        "    if (!el) continue;"
        "    let p = el.parentElement, ancestor_sticky = false;"
        "    while (p) {"
        "      if (ids.has(p.dataset._sscan)) { ancestor_sticky = true; break; }"
        "      p = p.parentElement;"
        "    }"
        "    if (!ancestor_sticky) outer.push(id);"
        "  }"
        "  return outer;"
        "})())"
    )
    r = _evaluate(call, dedup_js)
    outer_ids = set(json.loads(r["result"]["result"]["value"]))
    return [s for s in sticky if s["id"] in outer_ids]


def main(parser_name, html_path):
    parser = importlib.import_module(f"html_parsers.{parser_name}")
    raw = open(html_path).read()
    fmt_path = "/tmp/_scan_sticky.formatted.html"
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
    call("Emulation.setDeviceMetricsOverride", {
        "width": DEFAULT_VIEWPORT[0], "height": DEFAULT_VIEWPORT[1],
        "deviceScaleFactor": 1, "mobile": False,
    })
    time.sleep(0.5)
    sticky = detect_sticky(call)
    if not sticky:
        print(f"vw={DEFAULT_VIEWPORT[0]} scroll_test: 0 sticky elements")
    else:
        print(f"vw={DEFAULT_VIEWPORT[0]} scroll_test: {len(sticky)} "
              f"sticky element(s)")
        for s in sticky:
            print(f"  score={s['sticky_score']:.2f}  pos={s['position']:>8}  "
                  f"top_range={s['top_range'][0]:.0f}..{s['top_range'][1]:.0f}  "
                  f"{s['tag']}#{s['elId']}.{s['cls'][:60]}  "
                  f"({s['w']}x{s['h']})")
    call("Emulation.clearDeviceMetricsOverride")
    ws.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("Usage: scan_sticky.py <parser> <html>")
    main(sys.argv[1], sys.argv[2])
