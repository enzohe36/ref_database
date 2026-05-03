"""Diagnose a measured T or B failure by walking from the first/last visible
text node up to <body>, dumping each ancestor's bounding rect + computed
margin/padding. Use when measure_layout.py reports T or B over the ±4
tolerance: the ancestor whose mt/pt/mb/pb adds up to the overshoot is the
element to target with a CSS override.

Usage:
  python probe_text_chain.py <parser_module> <html_path> <vw> [first|last]

  first (default): chain for the topmost rendered text — diagnoses T issues
  last:             chain for the bottommost rendered text — diagnoses B issues
"""
import importlib
import json
import os
import sys
import time
import urllib.request

import websocket  # pip install websocket-client

PORT = int(os.environ.get("PORT", "9998"))
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


def _build_js(end):
    """end == 'first' picks the topmost rendered text; 'last' picks the bottommost."""
    cmp = "rc.top < bestKey" if end == "first" else "rc.bottom > bestKey"
    init = "Infinity" if end == "first" else "-Infinity"
    update_target = "rc.top" if end == "first" else "rc.bottom + window.scrollY"
    return r"""
JSON.stringify((function(){
    function inFloat(el){
        while (el && el !== document.body) {
            const p = getComputedStyle(el).position;
            if (p === 'fixed' || p === 'sticky' || p === 'absolute') return true;
            el = el.parentElement;
        }
        return false;
    }
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    let bestKey = """ + init + r""", bestNode = null;
    let n;
    while ((n = w.nextNode())) {
        if (!n.nodeValue || !n.nodeValue.trim()) continue;
        if (inFloat(n.parentElement)) continue;
        const rg = document.createRange(); rg.selectNodeContents(n);
        for (const rc of rg.getClientRects()) {
            if (rc.width < 1 || rc.height < 1) continue;
            if (""" + cmp + r""") { bestKey = """ + update_target + r"""; bestNode = n; }
        }
    }
    if (!bestNode) return { error: 'no text' };
    const chain = [];
    let el = bestNode.parentElement;
    while (el && el.tagName !== 'BODY') {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        chain.push({
            tag: el.tagName,
            cls: (el.className||'').toString().slice(0, 60),
            id: el.id || '',
            top: Math.round(r.top + window.scrollY),
            bot: Math.round(r.bottom + window.scrollY),
            left: Math.round(r.left),
            width: Math.round(r.width),
            mt: cs.marginTop, mb: cs.marginBottom,
            ml: cs.marginLeft, mr: cs.marginRight,
            pt: cs.paddingTop, pb: cs.paddingBottom,
            pl: cs.paddingLeft, pr: cs.paddingRight,
        });
        el = el.parentElement;
    }
    return { text: bestNode.nodeValue.trim().slice(0, 60), chain };
})());
"""


def main():
    if len(sys.argv) < 4:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    parser_name = sys.argv[1]
    html_path = sys.argv[2]
    vw = int(sys.argv[3])
    end = sys.argv[4] if len(sys.argv) > 4 else "first"
    if end not in ("first", "last"):
        print(f"end must be 'first' or 'last', got {end!r}", file=sys.stderr)
        sys.exit(2)

    parser = importlib.import_module(f"html_parsers.{parser_name}")
    with open(html_path, "r", errors="replace") as f:
        raw = f.read()
    formatted = parser.remove_banners(raw)
    out_path = f"/tmp/{parser_name}_formatted.html"
    with open(out_path, "w") as f:
        f.write(formatted)

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
            "expression": _build_js(end), "returnByValue": True,
        })
        result = json.loads(r["result"]["result"]["value"])
    finally:
        ws.close()
        urllib.request.urlopen(urllib.request.Request(
            f"http://localhost:{PORT}/json/close/{tab['id']}", method="PUT"
        ))

    if "error" in result:
        print(result["error"])
        return
    print(f"text ({end}): {result['text']!r}")
    print(f"chain (innermost → outermost):")
    for x in result["chain"]:
        print(
            f"  <{x['tag']:<7} #{x['id'][:12]:<12} .{x['cls'][:40]:<40}>"
            f"  top={x['top']:>5} bot={x['bot']:>5} L={x['left']:>4} W={x['width']:>4}"
        )
        if (x["mt"], x["mb"], x["pt"], x["pb"], x["ml"], x["mr"], x["pl"], x["pr"]) != ("0px",) * 8:
            print(
                f"      mt={x['mt']} mb={x['mb']} pt={x['pt']} pb={x['pb']}"
                f"  ml={x['ml']} mr={x['mr']} pl={x['pl']} pr={x['pr']}"
            )


if __name__ == "__main__":
    main()
