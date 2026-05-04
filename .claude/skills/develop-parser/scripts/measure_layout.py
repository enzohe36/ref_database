"""Measure rendered text bounds (L/R/T/B/width) of a formatted HTML at one or
more viewports via CDP, against the develop-parser layout target spec.

Usage:
  python measure_layout.py <parser_module> <html_path> [vw1 vw2 ...]

  parser_module:  dotted name under html_parsers/, e.g. `wiley` or `tandfonline`
  html_path:      raw or formatted HTML; the script applies remove_banners and
                  measures the result so you can iterate on the parser without
                  running convert_html.py
  vw...:          viewport widths in px; default = 600 720 820 1024 1280 1600 1920

Reports L, R, T, B, width per viewport. Target (per develop-parser layout spec):
    L = max(16, (vw - 720) / 2),  R = L,  T = 56,  B = 56,
    width = vw - 32 for vw <= 752 else 720;  tolerance ±4 px.

Requires a Chrome/Edge instance with --remote-debugging-port; defaults to
9998. Override via PORT env var.
"""
import importlib
import json
import os
import sys
import time
import urllib.request

import websocket  # pip install websocket-client

PORT = int(os.environ.get("PORT", "9998"))
DEFAULT_VIEWPORTS = (600, 720, 820, 1024, 1280, 1600, 1920)
SETTLE_SECONDS = 4

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
))))
sys.path.insert(0, REPO_ROOT)


def _cdp_call(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            return msg


_MEASURE_JS = r"""
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
        const rg = document.createRange();
        rg.selectNodeContents(n);
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
    return {
        vw,
        L: Math.round(minL),
        R: Math.round(vw - maxR),
        T: Math.round(top),
        B: docH - Math.round(bottom),
        width: Math.round(maxR - minL),
        docH,
    };
})());
"""


def _measure(out_path, vw):
    tab = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"http://localhost:{PORT}/json/new?file://{out_path}", method="PUT"
    )).read())
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=120)
    try:
        _cdp_call(ws, 1, "Page.enable")
        _cdp_call(ws, 2, "Runtime.enable")
        _cdp_call(ws, 3, "Emulation.setDeviceMetricsOverride", {
            "width": vw, "height": 1200,
            "deviceScaleFactor": 1, "mobile": False,
        })
        time.sleep(SETTLE_SECONDS)
        r = _cdp_call(ws, 4, "Runtime.evaluate", {
            "expression": _MEASURE_JS, "returnByValue": True,
        })
        return json.loads(r["result"]["result"]["value"])
    finally:
        ws.close()
        urllib.request.urlopen(urllib.request.Request(
            f"http://localhost:{PORT}/json/close/{tab['id']}", method="PUT"
        ))


def _format_check(m):
    """Return a per-row pass/fail summary against develop-parser layout targets."""
    vw = m["vw"]
    target_L = max(16, (vw - 720) // 2)
    target_W = vw - 32 if vw <= 752 else 720
    rows = [
        ("L", m["L"], target_L),
        ("R", m["R"], target_L),
        ("T", m["T"], 56),
        ("B", m["B"], 56),
        ("W", m["width"], target_W),
    ]
    flags = []
    for name, got, target in rows:
        ok = abs(got - target) <= 4
        flags.append(f"{name}={got}({'✓' if ok else f'~{target}'})")
    return " ".join(flags)


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    parser_name = sys.argv[1]
    html_path = sys.argv[2]
    viewports = [int(x) for x in sys.argv[3:]] if len(sys.argv) > 3 else list(DEFAULT_VIEWPORTS)

    parser = importlib.import_module(f"html_parsers.{parser_name}")

    with open(html_path, "r", errors="replace") as f:
        raw = f.read()
    formatted = parser.remove_banners(raw)
    out_path = f"/tmp/{parser_name}_formatted.html"
    with open(out_path, "w") as f:
        f.write(formatted)
    print(f"formatted: {out_path}  raw={len(raw):,}  out={len(formatted):,}  delta={len(formatted)-len(raw):+,}")

    for vw in viewports:
        m = _measure(out_path, vw)
        print(f"  vw={vw:>4}: {_format_check(m)}  docH={m['docH']}")


if __name__ == "__main__":
    main()
