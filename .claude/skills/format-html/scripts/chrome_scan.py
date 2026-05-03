"""Scan rendered papers for header/footer chrome missed by remove_banners.

For each input paper this script launches headless Chrome via
get_refs.start_browser(), navigates to the on-disk HTML, and reports any
element that matches:
  - Header: absY < 100, width > viewport * 0.5, height > 20
  - Footer: absY >= article_bottom - 50, width > 400, height > 20
excluding every <article>, its descendants, and any element that contains
an article (outer page wrappers). The article region is the union
bounding box of every <article> on the page (falls back to #iucr-art /
#main.article / .article_content-left when no <article> is present).

Run from the project root:
    python .claude/skills/format-html/scripts/chrome_scan.py papers/<stem>.html [...]

0 header + 0 footer candidates means remove_banners cleared all detectable
chrome. Non-zero results are either a missing selector in remove_banners
(real bug) or HTML on disk that predates the latest remove_banners (re-run
convert_html.py on that paper to refresh).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import websocket

# Script lives at .claude/skills/format-html/scripts/chrome_scan.py —
# walk up four levels to reach the project root where get_refs.py lives.
BASE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
sys.path.insert(0, BASE)
from get_refs import start_browser, stop_browser  # noqa: E402


def _rpc(ws, i, method, params=None):
    payload = {"id": i, "method": method}
    if params is not None:
        payload["params"] = params
    ws.send(json.dumps(payload))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == i:
            return msg


def scan_one(ws, path, vw=1280):
    _rpc(ws, 1, "Emulation.setDeviceMetricsOverride",
         {"width": vw, "height": 1200, "deviceScaleFactor": 1, "mobile": False})
    file_url = "file://" + urllib.parse.quote(os.path.abspath(path))
    _rpc(ws, 2, "Page.navigate", {"url": file_url})
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
            if msg.get("method") == "Page.loadEventFired":
                break
        except websocket.WebSocketTimeoutException:
            break
    time.sleep(2)
    expr = r"""
    (function(){
      // Article region = union bounding box over every <article> element
      // (some publishers split title/abstract/body/refs into sibling
      // <article> tags). Fall back to #iucr-art or #main when no
      // <article> is present.
      const articles = Array.from(document.querySelectorAll('article'));
      if (articles.length === 0) {
        const fb = document.getElementById('iucr-art')
          || document.querySelector('#main.article')
          || document.querySelector('.article_content-left');
        if (fb) articles.push(fb);
      }
      const vw = window.innerWidth;
      let artTop = 0, artBottom = document.documentElement.scrollHeight;
      if (articles.length) {
        artTop = Math.min(...articles.map(a => a.getBoundingClientRect().top + window.scrollY));
        artBottom = Math.max(...articles.map(a => a.getBoundingClientRect().bottom + window.scrollY));
      }
      // Exclude every article, their descendants, AND any element that
      // contains any article (outer page wrappers).
      const related = (el) => {
        for (const a of articles) {
          if (el === a || a.contains(el) || el.contains(a)) return true;
        }
        return false;
      };
      const out = {header: [], footer: []};
      const seen = new Set();
      document.querySelectorAll('body *').forEach(el => {
        if (related(el)) return;
        const r = el.getBoundingClientRect();
        const top = r.top + window.scrollY;
        const bottom = r.bottom + window.scrollY;
        if (r.width === 0 || r.height === 0) return;
        // Skip container wrappers if we already captured a wider ancestor descendant
        const key = `${Math.round(top)}_${Math.round(r.width)}_${Math.round(r.height)}`;
        if (seen.has(key)) return;
        seen.add(key);
        if (top < 100 && r.width > vw * 0.5 && r.height > 20) {
          out.header.push({top: Math.round(top), w: Math.round(r.width), h: Math.round(r.height), tag: el.tagName, id: el.id, cls: (el.className || '').slice(0, 80)});
        }
        if (top >= artBottom - 50 && r.width > 400 && r.height > 20) {
          out.footer.push({top: Math.round(top), w: Math.round(r.width), h: Math.round(r.height), tag: el.tagName, id: el.id, cls: (el.className || '').slice(0, 80)});
        }
      });
      return JSON.stringify({vw, artTop: Math.round(artTop), artBottom: Math.round(artBottom), docH: document.documentElement.scrollHeight, ...out});
    })()
    """
    r = _rpc(ws, 10, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return json.loads(r["result"]["result"]["value"])


def main():
    paths = sys.argv[1:]
    proc, port, pd = start_browser()
    try:
        t = json.load(urllib.request.urlopen(f"http://localhost:{port}/json"))
        page = next(x for x in t if x["type"] == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
        _rpc(ws, 0, "Page.enable")
        for p in paths:
            info = scan_one(ws, p)
            print(f"\n=== {os.path.basename(p)} (vw={info['vw']}, artTop={info['artTop']}, artBottom={info['artBottom']}) ===")
            print(f"HEADER candidates ({len(info['header'])}):")
            for e in info["header"][:15]:
                print(f"  top={e['top']:>4} w={e['w']:>5} h={e['h']:>4} <{e['tag'].lower()} id={e['id']!r} class={e['cls']!r}>")
            print(f"FOOTER candidates ({len(info['footer'])}):")
            for e in info["footer"][:15]:
                print(f"  top={e['top']:>6} w={e['w']:>5} h={e['h']:>4} <{e['tag'].lower()} id={e['id']!r} class={e['cls']!r}>")
        ws.close()
    finally:
        stop_browser(proc, pd)


if __name__ == "__main__":
    main()
