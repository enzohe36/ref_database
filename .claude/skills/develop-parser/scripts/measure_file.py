"""Measure a formatted HTML file directly (no `remove_banners` re-application).

Two checks, both reporting only failures:

  1. Per-viewport target check — `L / R / T / B / W` against the develop-parser layout
     spec at each viewport, ±4 px tolerance.

  2. Cross-viewport layout diff — compares dimensions / display mode of every
     identifiable element (one with a class, id, or "structural" tag) between
     the vw=720 reference and each wider viewport. Flags elements whose
     `display` mode flips, or whose `height` / `width` changes beyond
     threshold (the wiley `journal-banner-text` switching inline-block→block,
     the tandfonline metrics widget collapsing 720-wide horizontal row → 100-
     wide vertical sidebar, the gray-box journal-nav inflating 89→321 px).

Use after `convert_html.py` runs (which writes the formatted HTML back in
place). The script reads the file as-is — no further `remove_banners`.

Requires Chrome/Edge with `--remote-debugging-port=9998` (override via PORT
env var).

Usage:
  python measure_file.py <html_path> [vw1 vw2 ...]
"""
import json
import os
import sys
import time
import urllib.request

import websocket  # pip install websocket-client

PORT = int(os.environ.get("PORT", "9998"))
DEFAULT_VIEWPORTS = (600, 720, 820, 1024, 1280, 1600, 1920)
SETTLE = 4
REFERENCE_VW = 720  # cross-viewport diff is computed against this viewport

# Cross-viewport diff thresholds. The width of a block-level element naturally
# varies with the body (the body cap is 752 px, the body itself is `min(vw,
# 752)` wide, and every full-width descendant tracks the body). Normalizing
# each element's width by the body width at the same viewport collapses that
# natural responsive change to ~0; only alternative-layout switches (e.g. a
# 100-px sidebar form vs a 720-px horizontal row, or `display:inline-block`
# vs `display:block`) produce a ratio shift.
#
# An element is flagged when EITHER:
#   - `display` mode flipped (regardless of size — catches inline-block→block
#     even on small elements like wiley `.journal-banner-text`)
#   - it was close to full-width at the reference vw (`width/body_w >=
#     STRUCTURAL_RATIO`) AND its ratio shifted by `>= DIFF_RATIO` at the
#     target vw (catches structural wrappers like the tandfonline metrics
#     widget collapsing 720-wide horizontal → 100-wide sidebar)
#
# Inline / partial-width elements are skipped from the ratio check because
# their width naturally varies with text wrapping.
DIFF_RATIO = 0.15
STRUCTURAL_RATIO = 0.5
# Maximum diffs to print per viewport (largest ratio shift first).
MAX_DIFFS_PER_VW = 8


def _cdp(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == mid:
            return m


_BOUNDS_JS = r"""
JSON.stringify((function(){
    const vw = window.innerWidth;
    const docH = document.documentElement.scrollHeight;
    function inFloat(el){
        while (el && el !== document.body) {
            const p = getComputedStyle(el).position;
            if (p === 'fixed' || p === 'sticky' || p === 'absolute') return true;
            el = el.parentElement;
        }
        return false;
    }
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    let minL = Infinity, maxR = -Infinity, top = Infinity, bottom = -Infinity;
    let n;
    while ((n = w.nextNode())) {
        if (!n.nodeValue || !n.nodeValue.trim()) continue;
        if (inFloat(n.parentElement)) continue;
        const rg = document.createRange(); rg.selectNodeContents(n);
        for (const rc of rg.getClientRects()) {
            if (rc.width < 1 || rc.height < 1) continue;
            if (rc.left < minL) minL = rc.left;
            if (rc.right > maxR) maxR = rc.right;
            const t = rc.top + window.scrollY;
            const b = rc.bottom + window.scrollY;
            if (t < top) top = t;
            if (b > bottom) bottom = b;
        }
    }
    return {vw, L: Math.round(minL), R: Math.round(vw - maxR),
            T: Math.round(top), B: docH - Math.round(bottom),
            width: Math.round(maxR - minL), docH};
})());
"""


# Walks DOM depth-first, emitting one record per "identifiable" element. The
# path string lets two viewports' records be matched by structural position
# (DOM structure is identical at every viewport — only CSS differs). Also
# returns the body's content-area width to use as the normalization base for
# cross-viewport width comparisons.
_ELEMENTS_JS = r"""
JSON.stringify((function(){
    const STRUCTURAL = new Set(['ARTICLE','MAIN','SECTION','NAV','ASIDE','HEADER',
        'FOOTER','H1','H2','H3','H4','H5','H6','FIGURE','FIGCAPTION','TABLE',
        'UL','OL','BLOCKQUOTE','DL']);
    const bodyRect = document.body.getBoundingClientRect();
    const bodyW = Math.round(bodyRect.width);
    const out = [];
    function walk(el, path) {
        const cs = getComputedStyle(el);
        // Skip subtrees the page itself hides — they have no rendered position.
        if (cs.display === 'none' || cs.visibility === 'hidden') return;
        const r = el.getBoundingClientRect();
        const cls = (el.className||'').toString().trim();
        const id = el.id || '';
        const identified = !!cls || !!id || STRUCTURAL.has(el.tagName);
        if (identified && r.height >= 1 && r.width >= 1) {
            out.push({
                path,
                tag: el.tagName,
                id,
                cls: cls.slice(0, 40),
                h: Math.round(r.height),
                w: Math.round(r.width),
                display: cs.display,
            });
        }
        let i = 0;
        for (const c of el.children) {
            walk(c, path + '/' + c.tagName + '[' + i + ']');
            i++;
        }
    }
    walk(document.body, 'BODY');
    return {bodyW, elements: out};
})());
"""


def _measure(path, vw):
    tab = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"http://localhost:{PORT}/json/new?file://{path}", method="PUT"
    )).read())
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=120)
    try:
        _cdp(ws, 1, "Page.enable")
        _cdp(ws, 2, "Runtime.enable")
        _cdp(ws, 3, "Emulation.setDeviceMetricsOverride", {
            "width": vw, "height": 1200, "deviceScaleFactor": 1, "mobile": False,
        })
        time.sleep(SETTLE)
        b = _cdp(ws, 4, "Runtime.evaluate", {"expression": _BOUNDS_JS, "returnByValue": True})
        e = _cdp(ws, 5, "Runtime.evaluate", {"expression": _ELEMENTS_JS, "returnByValue": True})
        bounds = json.loads(b["result"]["result"]["value"])
        snap = json.loads(e["result"]["result"]["value"])
        return bounds, snap["bodyW"], snap["elements"]
    finally:
        ws.close()
        urllib.request.urlopen(urllib.request.Request(
            f"http://localhost:{PORT}/json/close/{tab['id']}", method="PUT"
        ))


def _bounds_fails(m):
    """Per-viewport target failures."""
    vw = m["vw"]
    target_L = max(16, (vw - 720) // 2)
    target_W = vw - 32 if vw <= 752 else 720
    fails = []
    for name, got, target in [("L", m["L"], target_L), ("R", m["R"], target_L),
                              ("T", m["T"], 56), ("B", m["B"], 56), ("W", m["width"], target_W)]:
        if abs(got - target) > 4:
            fails.append(f"{name}={got}(~{target})")
    return fails


def _layout_diffs(ref_body_w, ref_elems, tgt_body_w, tgt_elems):
    """Return elements whose layout flipped between viewports.

    An element flagged when EITHER:
      - `display` mode changed (e.g., inline-block → block)
      - `width / body_width` ratio changed by >= DIFF_RATIO (alternative
        layout — element collapsed or expanded out of proportion to the
        natural body-cap responsive change)
    """
    ref_by_path = {e["path"]: e for e in ref_elems}
    diffs = []
    for e in tgt_elems:
        r = ref_by_path.get(e["path"])
        if not r:
            continue  # not in reference — can happen with display:none flips
        ref_ratio = r["w"] / ref_body_w if ref_body_w else 0
        tgt_ratio = e["w"] / tgt_body_w if tgt_body_w else 0
        ratio_delta = abs(tgt_ratio - ref_ratio)
        display_changed = e["display"] != r["display"]
        # Only check ratio shifts for elements that were structurally wide at
        # the reference vw (otherwise we flag every text-reflowing inline span).
        ratio_shift = (
            ref_ratio >= STRUCTURAL_RATIO and ratio_delta >= DIFF_RATIO
        )
        if display_changed or ratio_shift:
            diffs.append({
                "tag": e["tag"], "id": e["id"], "cls": e["cls"],
                "ref_display": r["display"], "tgt_display": e["display"],
                "ref_h": r["h"], "tgt_h": e["h"],
                "ref_w": r["w"], "tgt_w": e["w"],
                "ratio_delta": ratio_delta,
                "display_changed": display_changed,
            })
    diffs.sort(key=lambda d: -d["ratio_delta"])
    return diffs


def _format_diff(d):
    parts = []
    if d["display_changed"]:
        parts.append(f"display:{d['ref_display']}→{d['tgt_display']}")
    if d["ratio_delta"] >= DIFF_RATIO:
        parts.append(f"w:{d['ref_w']}→{d['tgt_w']}  h:{d['ref_h']}→{d['tgt_h']}")
    label = f"<{d['tag']:<7} #{d['id'][:15]:<15} .{d['cls'][:30]:<30}>"
    return f"  {label}  {'  '.join(parts)}"


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    path = os.path.abspath(sys.argv[1])
    viewports = [int(x) for x in sys.argv[2:]] if len(sys.argv) > 2 else list(DEFAULT_VIEWPORTS)
    if REFERENCE_VW not in viewports:
        viewports = [REFERENCE_VW] + viewports

    # Collect bounds + element snapshots across viewports.
    snapshots = {}
    for vw in viewports:
        snapshots[vw] = _measure(path, vw)

    # 1. Per-viewport target failures.
    print("Per-viewport target (L/R/T/B/W vs develop-parser layout spec, ±4):")
    any_fail = False
    for vw in viewports:
        bounds, _, _ = snapshots[vw]
        fails = _bounds_fails(bounds)
        if fails:
            print(f"  vw={vw:>4}: {' '.join(fails)}")
            any_fail = True
    if not any_fail:
        print("  ✓ all viewports pass")

    # 2. Cross-viewport layout diff against vw=720.
    print(f"\nCross-viewport layout diff (vs vw={REFERENCE_VW} reference):")
    _, ref_body_w, ref_elems = snapshots[REFERENCE_VW]
    any_diff = False
    for vw in viewports:
        if vw == REFERENCE_VW:
            continue
        _, tgt_body_w, tgt_elems = snapshots[vw]
        diffs = _layout_diffs(ref_body_w, ref_elems, tgt_body_w, tgt_elems)
        if diffs:
            print(f"  vw={vw}: {len(diffs)} differing element(s)")
            for d in diffs[:MAX_DIFFS_PER_VW]:
                print(_format_diff(d))
            if len(diffs) > MAX_DIFFS_PER_VW:
                print(f"    ... ({len(diffs) - MAX_DIFFS_PER_VW} more)")
            any_diff = True
    if not any_diff:
        print("  ✓ no layout diffs at any viewport")


if __name__ == "__main__":
    main()
