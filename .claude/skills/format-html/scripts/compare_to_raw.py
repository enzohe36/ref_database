"""Compare a formatted HTML's element spacing against the raw HTML rendered at
vw=720 (the format-html spec's reference). Flags elements whose top-of-element
position relative to the article wrapper differs significantly between the
raw and formatted renderings — meaning the formatter changed the publisher's
native vertical rhythm at the reference viewport.

Use this as quality check #2.5 (between per-viewport bounds and JSON parity)
to verify the "preserve native at vw=720" invariant. The bounds check tells
you the spec is met; this check tells you whether the layout matches what
the publisher's narrow CSS would produce on its own.

What's flagged:
  - Element exists in raw but missing in formatted (your DOM strip removed it)
  - Element's position relative to wrapper top differs by >= TOP_DELTA_PX
    (your CSS introduced extra/less spacing somewhere up the chain)
  - Element's height differs by >= HEIGHT_DELTA_PX
    (your CSS shrank/expanded the element)

What's NOT flagged:
  - Element is new in formatted (your remove_banners injected something —
    typically the <style> block, harmless)
  - Position differences inside floated/sticky/absolute subtrees

Usage:
  python compare_to_raw.py <raw_path> <formatted_path>

Both paths are required. The raw path should be the publisher's saved HTML
before convert_html.py touched it (typically `papers_ref/<stem>.html`); the
formatted path is the post-convert_html artifact (`papers/<stem>.html`).
"""
import json
import os
import sys
import time
import urllib.request

import websocket  # pip install websocket-client

PORT = int(os.environ.get("PORT", "9998"))
SETTLE = 4
VW = 720

# Threshold for flagging position / height differences. Sub-pixel rendering
# and minor reflow typically stays under 4 px; format-html spec uses ±4 px
# tolerance so the same threshold here.
TOP_DELTA_PX = 4
HEIGHT_DELTA_PX = 4
MAX_DIFFS_TO_PRINT = 25


def _cdp(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == mid:
            return m


# Walk DOM and emit one record per element with a stable ID. ID is the
# match key: the same `<li id=B210>` exists in raw and formatted regardless
# of how much chrome was stripped between them. Skip display:none / hidden
# subtrees (those don't contribute to vertical layout).
_SNAPSHOT_JS = r"""
JSON.stringify((function(){
    const out = [];
    function walk(el) {
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return;
        if (el.id) {
            const r = el.getBoundingClientRect();
            if (r.height >= 1) {
                out.push({
                    id: el.id,
                    tag: el.tagName,
                    cls: (el.className||'').toString().slice(0, 40),
                    top: Math.round(r.top + window.scrollY),
                    h: Math.round(r.height),
                });
            }
        }
        for (const c of el.children) walk(c);
    }
    walk(document.body);
    return out;
})());
"""


def _snapshot(path):
    """Render `path` at vw=720 and return list of {path, tag, id, cls, top, h}."""
    tab = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"http://localhost:{PORT}/json/new?file://{path}", method="PUT"
    )).read())
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=120)
    try:
        _cdp(ws, 1, "Page.enable")
        _cdp(ws, 2, "Runtime.enable")
        _cdp(ws, 3, "Emulation.setDeviceMetricsOverride", {
            "width": VW, "height": 1200,
            "deviceScaleFactor": 1, "mobile": False,
        })
        time.sleep(SETTLE)
        r = _cdp(ws, 4, "Runtime.evaluate", {
            "expression": _SNAPSHOT_JS, "returnByValue": True,
        })
        return json.loads(r["result"]["result"]["value"])
    finally:
        ws.close()
        urllib.request.urlopen(urllib.request.Request(
            f"http://localhost:{PORT}/json/close/{tab['id']}", method="PUT"
        ))


def _diff(raw_elements, fmt_elements):
    """Compare elements (matched by id) and return resized + relocated lists.

    Only resized / relocated lists are returned — DOM-level "dropped" diffs
    are omitted because intentional chrome strips (the whole point of
    remove_banners) overwhelm the signal.
    """
    fmt_by_id = {e["id"]: e for e in fmt_elements}

    # Find an anchor present in both renderings — use the first id common
    # to both whose top is in the article body region (top > 200 in both).
    # Diff each common element's position relative to that anchor; this
    # cancels the body-cap centering shift.
    common = [e for e in raw_elements if e["id"] in fmt_by_id]
    raw_anchor_id = None
    for e in raw_elements:
        if e["id"] in fmt_by_id and e["top"] > 200:
            raw_anchor_id = e["id"]
            break
    if not raw_anchor_id:
        return [], []
    raw_anchor_top = next(e["top"] for e in raw_elements if e["id"] == raw_anchor_id)
    fmt_anchor_top = fmt_by_id[raw_anchor_id]["top"]

    resized = []
    moved = []
    for r in common:
        f = fmt_by_id[r["id"]]
        h_delta = f["h"] - r["h"]
        # Position relative to anchor (cancels body-cap shift)
        raw_rel = r["top"] - raw_anchor_top
        fmt_rel = f["top"] - fmt_anchor_top
        top_delta = fmt_rel - raw_rel
        if abs(h_delta) >= HEIGHT_DELTA_PX:
            resized.append({
                "id": f["id"], "tag": f["tag"], "cls": f["cls"],
                "raw_h": r["h"], "fmt_h": f["h"], "h_delta": h_delta,
            })
        if abs(top_delta) >= TOP_DELTA_PX:
            moved.append({
                "id": f["id"], "tag": f["tag"], "cls": f["cls"],
                "raw_rel": raw_rel, "fmt_rel": fmt_rel, "top_delta": top_delta,
            })
    resized.sort(key=lambda d: -abs(d["h_delta"]))
    moved.sort(key=lambda d: -abs(d["top_delta"]))
    return resized, moved


def _format_resized(d):
    sign = "+" if d["h_delta"] > 0 else ""
    label = f"<{d['tag']:<7} #{d['id'][:20]:<20} .{d['cls'][:25]:<25}>"
    return f"  RESIZED {label}  h: {d['raw_h']} → {d['fmt_h']} ({sign}{d['h_delta']})"


def _format_moved(d):
    sign = "+" if d["top_delta"] > 0 else ""
    label = f"<{d['tag']:<7} #{d['id'][:20]:<20} .{d['cls'][:25]:<25}>"
    return f"  MOVED   {label}  rel-top: {d['raw_rel']} → {d['fmt_rel']} ({sign}{d['top_delta']})"


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    raw_path = os.path.abspath(sys.argv[1])
    fmt_path = os.path.abspath(sys.argv[2])
    if not os.path.exists(raw_path):
        print(f"raw not found: {raw_path}", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(fmt_path):
        print(f"formatted not found: {fmt_path}", file=sys.stderr)
        sys.exit(2)

    raw = _snapshot(raw_path)
    fmt = _snapshot(fmt_path)

    common_ids = set(e["id"] for e in raw) & set(e["id"] for e in fmt)
    print(f"raw elements (with id): {len(raw)}  formatted: {len(fmt)}  common: {len(common_ids)}")

    resized, moved = _diff(raw, fmt)

    if not resized and not moved:
        print("  ✓ no spacing or sizing deviations from native at vw=720")
        return

    if resized:
        print(f"\n  Resized elements (Δh >= {HEIGHT_DELTA_PX} px; first {MAX_DIFFS_TO_PRINT}):")
        for d in resized[:MAX_DIFFS_TO_PRINT]:
            print(_format_resized(d))
        if len(resized) > MAX_DIFFS_TO_PRINT:
            print(f"  ... ({len(resized) - MAX_DIFFS_TO_PRINT} more)")

    if moved:
        print(f"\n  Moved elements (Δrel-top >= {TOP_DELTA_PX} px from anchor; first {MAX_DIFFS_TO_PRINT}):")
        for d in moved[:MAX_DIFFS_TO_PRINT]:
            print(_format_moved(d))
        if len(moved) > MAX_DIFFS_TO_PRINT:
            print(f"  ... ({len(moved) - MAX_DIFFS_TO_PRINT} more)")


if __name__ == "__main__":
    main()
