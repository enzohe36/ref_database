#!/usr/bin/env python3
"""Fetch full-text HTML into papers/raw/.

Usage:
    python get_html.py <pmid|url|list> [<pmid|url|list> ...]

For each PMID arg: read doi from papers/parsed/<stem>.json, fetch the page
via Edge + single-file, save to papers/raw/<stem>.html. PMIDs whose
papers/raw/<stem>.html already exists are skipped.

For each URL arg: fetch directly, save to papers/raw/<url_name>.html where
<url_name> is the URL with all non-alphanumeric characters collapsed to
underscores. URL-keyed outputs that already exist are skipped.

A list arg is a file containing PMIDs and/or URLs separated by spaces or
newlines. Lines starting with '#' are ignored (comments).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import websocket
from html import unescape

from _cli import parse_argv
from _net import polite_urlopen
from _project import parsed_path, raw_dir, raw_html_path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPERS_DIR = str(raw_dir())
EDGE_PATH = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

raw_dir().mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Per-stem attempt tracking
# ---------------------------------------------------------------------------

_ATTEMPTS = {}  # stem -> [reason, reason, ...] accumulated across retries


def _record_attempt(stem, reason):
    """Append a failure reason for this stem."""
    _ATTEMPTS.setdefault(stem, []).append(reason)


def _emit_stem_log(stem, outcome):
    """Print the per-stem attempt log if any failures were recorded, then
    clear the record. outcome is 'success' or a terminal failure reason.

    Silent when no failures were recorded (clean first-try success).
    """
    attempts = _ATTEMPTS.pop(stem, [])
    if not attempts:
        return
    lines = [stem]
    for i, reason in enumerate(attempts, start=1):
        lines.append(f"    try {i}: {reason}")
    lines.append(f"    => {outcome}")
    print("\n".join(lines), file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Browser + single-file HTML fetch
# ---------------------------------------------------------------------------

CDP_PORT = 9222
BATCH_SIZE = 50
TAB_DELAY = 1        # seconds between opening tabs
PAGE_LOAD_WAIT = 10  # seconds to wait for tabs to load


# ---------------------------------------------------------------------------
# Publisher rule sheet
#
# Applied to the resolved URL after the DOI's redirect chain has settled
# (preload stage), before SingleFile is invoked. Controls:
#   - `url`: idempotent rewrite of the resolved URL (e.g. upgrade to
#     full-text, bypass a publisher re-routing).
#   - `wait`: SingleFile --browser-wait-until strategy override.
#   - `wait_delay`: SingleFile --browser-wait-delay override (ms).
#     Use for sites whose critical content is injected by JS several
#     seconds after the load event (e.g. academic.oup.com populates
#     the article-metadata box via XHR ~5-8 s after load).
# Callers look up the first rule whose key is a substring of the URL.
# Rules must be idempotent so multiple passes (initial fetch, retry) leave
# the URL unchanged once the terminal target is reached.
# ---------------------------------------------------------------------------

def _cdp_eval_await(ws_url, expr, timeout=60):
    """Evaluate JS in a page and await any returned promise.

    Returns the resolved value, or None on timeout / error / exception.
    Used for `fetch(...).then(...)` expressions that resolve to a data URL.
    """
    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
        ws.settimeout(timeout)
        try:
            ws.send(json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expr,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            }))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    return msg.get("result", {}).get("result", {}).get("value")
        finally:
            ws.close()
    except Exception:
        return None


def _iucr_inline_figures(saved_path, port):
    """Post-capture hook: inline iucr full-resolution figures.

    Strategy (batch fetch from a single same-origin tab):
      1. Extract the landing-page URL from the SingleFile header comment.
      2. Collect every <a href=...fig{N}.html><img class=figlnkthm ...></a>
         wrapper from the saved HTML.
      3. Open one tab at the iucr landing URL (same-origin with the
         fig sub-pages — cookies carry, CORS is a non-issue).
      4. Run a single JS blob that `fetch()`es each sub-page, extracts
         the main image URL, then `fetch()`es the image and returns an
         array of {url, dataUrl} in parallel. Wall time: ~5 s vs. ~80 s
         with sequential per-tab loading.
      5. Substitute each returned data URL into its thumbnail's <img src>.
    """
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Landing URL lives in SingleFile's header comment at the top of the file.
    url_m = re.search(r"Page saved with SingleFile\s+url:\s*(\S+)", html[:2000])
    if not url_m:
        return
    landing_url = url_m.group(1)

    # <a href=...figN.html> wrapping an <img class=figlnkthm>.
    # href may be unquoted, single-quoted, or double-quoted. Match the
    # whole wrapper so we can substitute the img tag inside.
    pattern = re.compile(
        r'<a\b[^>]*?href='
        r'(?:"([^"]*?fig\d+\.html)"|'
        r"'([^']*?fig\d+\.html)'|"
        r'([^\s>]*?fig\d+\.html))'
        r'[^>]*?>\s*'
        r'(<img\b[^>]*?\bclass=[^>]*?figlnkthm[^>]*?>)',
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(html))
    if not matches:
        return

    fig_pairs = [(m.group(1) or m.group(2) or m.group(3), m.group(4))
                 for m in matches]
    fig_urls = [u for u, _ in fig_pairs]
    print(f"  iucr post-capture: {len(fig_urls)} figure(s) to enrich", flush=True)

    # Open one same-origin tab at the landing URL.
    try:
        target = _cdp_open_tab(landing_url, port)
        tab_id = target["id"]
        ws_url = target["webSocketDebuggerUrl"]
    except Exception:
        print("    error: could not open landing tab", flush=True)
        return
    try:
        # Let the landing tab settle (Cloudflare + cookies).
        time.sleep(15)
        fig_urls_js = json.dumps(fig_urls)
        expr = r"""
        (async function() {
            const urls = __URLS__;
            const origin = location.origin;
            const href = location.href;
            async function one(url) {
                try {
                    const pageHtml = await fetch(url).then(r => r.ok ? r.text() : null);
                    if (!pageHtml) return {url, error: 'page-fetch-failed', origin, href};
                    // Prefer the magnified `fig<N>mag.<ext>` (full-resolution
                    // figure, ~3000+ px native) over `fig<N>.<ext>` (medium
                    // 640-px preview). The mag URL appears as an `<a href>`
                    // (the "view larger" link) on the sub-page; the medium
                    // URL appears as `<img src>`. Fall back to the medium
                    // when no mag link exists.
                    const magM = pageHtml.match(/(?:href|src)=["']?([^"'\s>]+fig\d+mag\.(?:png|jpg|jpeg|webp|gif))/i);
                    const m = magM || pageHtml.match(/<img[^>]*src=["']?([^"'\s>]+fig\d+[a-z]*\.(?:png|jpg|jpeg|webp|gif))/i);
                    if (!m) return {url, error: 'no-img-in-page'};
                    const imgUrl = new URL(m[1], url).href;
                    const blob = await fetch(imgUrl).then(r => r.ok ? r.blob() : null);
                    if (!blob) return {url, error: 'img-fetch-failed', imgUrl};
                    const dataUrl = await new Promise((res, rej) => {
                        const r = new FileReader();
                        r.onloadend = () => res(r.result);
                        r.onerror = rej;
                        r.readAsDataURL(blob);
                    });
                    return {url, imgUrl, dataUrl, size: dataUrl.length};
                } catch (e) { return {url, error: String(e), origin, href}; }
            }
            return Promise.all(urls.map(one));
        })()
        """.replace("__URLS__", fig_urls_js)
        results = _cdp_eval_await(ws_url, expr, timeout=120)
    finally:
        try:
            _cdp_close_tab(tab_id, port)
        except Exception:
            pass

    if not results:
        print("    error: batch fetch returned nothing", flush=True)
        return

    modified = False
    url_to_result = {r["url"]: r for r in results if isinstance(r, dict) and "url" in r}
    for fig_url, img_tag in fig_pairs:
        r = url_to_result.get(fig_url)
        if not r or not r.get("dataUrl"):
            reason = r.get("error", "unknown") if r else "no-result"
            print(f"    skip: {fig_url}  ({reason})", flush=True)
            continue
        data_url = r["dataUrl"]
        new_img = img_tag
        new_img, n1 = re.subn(
            r'\bsrc=(["\'])[^"\']*\1',
            lambda _m, u=data_url: f'src="{u}"',
            new_img, count=1,
        )
        if n1 == 0:
            new_img = re.sub(
                r'\bsrc=data:[^\s>]+',
                lambda _m, u=data_url: f'src="{u}"',
                new_img, count=1,
            )
        new_img = re.sub(r'\bwidth=(["\']?)\d+\1\s*', '', new_img)
        new_img = re.sub(r'\bheight=(["\']?)\d+\1\s*', '', new_img)
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        print(f"    inlined: {fig_url}  ({r.get('size', 0)} b)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _plos_inline_figures(saved_path, port):
    """Post-capture hook: inline plos large figure images via urllib.

    PLOS's `<img>` placeholder gets `src=data:,` (empty) after SingleFile
    capture for figures whose browser-side fetch failed during the inline
    pass (browser fetch/XHR are intercepted on plos — only `<img src=>`
    works, and the inline pass is unreliable). The `<a href=...size=large>`
    "Download larger image" link sits in the same `.figure` block and
    contains the canonical full-resolution URL (302-redirects to a signed
    GCS URL). Fetch each one server-side via urllib and substitute as a
    data URL into the thumbnail's `<img src>`. `port` is unused
    (server-side fetch).
    """
    del port
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Walk every `.figure ... <div class=figcaption>` block; pull the
    # thumbnail `<img>` and the `size=large` `<a href>`.
    fig_pattern = re.compile(
        r'<div\s+class=(?:"?figure"?)\b[^>]*>(?P<body>.*?)<div\s+class=(?:"?figcaption"?)',
        re.DOTALL | re.IGNORECASE,
    )
    pairs = []
    for m in fig_pattern.finditer(html):
        body = m.group('body')
        # The thumbnail img inside .img-box (any class set, src may be empty
        # `data:,` after a failed browser-side inline). Take the FIRST img.
        img_m = re.search(r'<img\b[^>]*>', body)
        url_m = re.search(
            r'href=["\']?([^"\'\s>]+\bsize=large[^"\'\s>]*)', body,
        )
        if img_m and url_m:
            pairs.append((img_m.group(0), unescape(url_m.group(1))))

    # Also walk the lightbox carousel: each carousel slot is
    #   <div class="carousel-item lightbox-figure" data-doi=10.1371/<journal>.<id>.gN>
    #     <img src=data:, loading=lazy alt="Figure N">
    #   </div>
    # No adjacent `<a href=…size=large>` link — derive the URL from
    # data-doi by routing it through PLOS's article/figure/image endpoint:
    #   https://journals.plos.org/<journal>/article/figure/image?id=<doi>&size=large
    # The `<journal>` segment is the third dotted token in the DOI
    # (`10.1371/journal.<journal>.<id>.gN` → `<journal>`).
    carousel_re = re.compile(
        r'<div\s+class=("?[^"\'>]*?\bcarousel-item\b[^"\'>]*?\blightbox-figure\b[^"\'>]*?"?)'
        r'[^>]*\bdata-doi=("([^"]+)"|\'([^\']+)\'|([^\s>]+))',
        re.IGNORECASE,
    )
    for m in carousel_re.finditer(html):
        doi = (m.group(3) or m.group(4) or m.group(5)).strip()
        # Expect `10.1371/journal.<journal>.<id>.gN` or `…tN` (table); only
        # extract the journal token. Fail open — skip if the DOI doesn't
        # match the expected shape.
        m_doi = re.match(r'10\.1371/journal\.([a-z]+)\.', doi, re.IGNORECASE)
        if not m_doi:
            continue
        journal = m_doi.group(1).lower()
        # Param ordering matters on this endpoint: `?id=…&size=large`
        # returns 404; `?download&size=large&id=…` returns 200. Match
        # the format PLOS itself uses on the inline `.figure` block's
        # download anchor (extracted by the loop above).
        url = (
            f"https://journals.plos.org/{journal}/article/figure/image"
            f"?download&size=large&id={doi}"
        )
        # Find the first <img> inside this carousel-item div (closing
        # </div> is at most ~500 bytes downstream — bound the scan).
        slot = html[m.start():m.start() + 1500]
        img_m = re.search(r'<img\b[^>]*>', slot)
        if not img_m:
            continue
        pairs.append((img_m.group(0), url))

    if not pairs:
        return

    print(
        f"  plos post-capture: {len(pairs)} figure(s) to enrich",
        flush=True,
    )

    import base64
    url_to_data = {}
    for _, url in pairs:
        if url in url_to_data:
            continue
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _BROWSER_UA},
            )
            with polite_urlopen(req, timeout=60) as resp:
                ct = resp.headers.get(
                    "Content-Type", "image/png",
                ).split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(
                f"    fetch-error: {url[:80]}...  ({e})", flush=True,
            )
            continue
        url_to_data[url] = (
            "data:" + ct + ";base64,"
            + base64.b64encode(blob).decode("ascii")
        )

    modified = False
    for img_tag, url in pairs:
        data_url = url_to_data.get(url)
        if not data_url:
            continue
        # Replace `src=data:,` (or any src) with the full data URL.
        new_img = re.sub(
            r'\bsrc=("|\')?data:[^"\'\s>]*\1?',
            lambda _m, u=data_url: f'src="{u}"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            new_img = re.sub(
                r'\bsrc=("|\')?[^"\'\s>]*\1?',
                lambda _m, u=data_url: f'src="{u}"',
                img_tag, count=1,
            )
        if new_img != img_tag:
            html = html.replace(img_tag, new_img, 1)
            modified = True
            print(
                f"    inlined: {url[:80]}  ({len(data_url)} chars)",
                flush=True,
            )

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _imrpress_inline_figures(saved_path, port):
    """Post-capture hook: inline imrpress figure images.

    imrpress (FBL / FBE / FBS journals on imrpress.com) saves articles
    with all `<img id=S<sec>-F<N>-g<i> src=data:,>` figure placeholders;
    Vue/JS populates them on render and SingleFile captures the empty
    state. The full figure URLs (`https://storage.imrpress.com/.../figN.jpg`)
    are still present elsewhere in the HTML — in `<meta name=twitter:image>`,
    JSON-LD blocks, and Vue script data — but never as `<img src>`.

    Strategy: extract every `figN.jpg` URL, match each to the
    `<img id=...-F<N>-g<i>>` placeholder by figure number N, fetch the
    image bytes server-side via `urllib` (the browser-side `fetch()` is
    blocked by CORS — `imrpress.com` and `storage.imrpress.com` are
    different origins and the storage host doesn't return
    `Access-Control-Allow-Origin`), and inline as data URLs.

    `port` is unused but kept for the post_capture signature symmetry
    with iucr.
    """
    del port  # server-side fetch; no browser tab needed
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Collect figure URLs by figure number. The same fig URL appears multiple
    # times (with and without `?x-oss-process=...` query); pick one.
    fig_urls = {}  # {N: url}
    for m in re.finditer(
        r'https://storage\.imrpress\.com/[^"\'>\s\\]+?fig(\d+)\.jpg(?:\?[^"\'>\s\\]*)?',
        html,
    ):
        n = int(m.group(1))
        if n not in fig_urls:
            fig_urls[n] = m.group(0)
    if not fig_urls:
        return

    # `<img id=S<sec>-F<N>-g<i> src=data:,>` placeholders.
    pattern = re.compile(
        r'<img\b[^>]*?\bid=S\d+-F(\d+)-g\d+\b[^>]*?>',
        re.IGNORECASE,
    )
    img_matches = list(pattern.finditer(html))
    if not img_matches:
        return

    pairs = []  # (N, img_tag, fig_url)
    for m in img_matches:
        n = int(m.group(1))
        if n in fig_urls:
            pairs.append((n, m.group(0), fig_urls[n]))
    if not pairs:
        return

    print(f"  imrpress post-capture: {len(pairs)} figure(s) to enrich",
          flush=True)

    # Server-side fetch each unique URL once and build a {url: data_url} map.
    import base64
    url_to_data = {}
    for fig_url in {u for _, _, u in pairs}:
        try:
            req = urllib.request.Request(
                fig_url, headers={"User-Agent": _BROWSER_UA},
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error: {fig_url[:60]}...  ({e})", flush=True)
            continue
        url_to_data[fig_url] = "data:" + ct + ";base64," + base64.b64encode(blob).decode("ascii")

    modified = False
    for n, img_tag, fig_url in pairs:
        data_url = url_to_data.get(fig_url)
        if not data_url:
            print(f"    skip fig{n}.jpg  (no data)", flush=True)
            continue
        new_img = re.sub(
            r'\bsrc=data:[^\s>]*',
            f'src="{data_url}"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        print(f"    inlined fig{n}.jpg  ({len(data_url)} b)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _annualreviews_inline_figures(saved_path, port):
    """Post-capture hook: replace annualreviews thumbnail with full-res GIF.

    annualreviews ships an inline base64 thumbnail (~500 px wide) for
    each figure as `<a class=media-link href=https://...f<N>.gif>
    <img src=data:image/gif;base64,...>`. The URL on the parent `<a>`
    serves the full-resolution GIF (~1500-2300 px wide). Server-side
    fetch each `media-link` href via urllib and replace the inline
    thumbnail with the full-res data URL.

    `port` is unused; kept for post_capture signature symmetry.
    """
    del port
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Match each `<a class=media-link href=...><img src=data:...>` pair.
    # Handles both quoted and unquoted attribute forms — annualreviews
    # emits both styles within the same article.
    pattern = re.compile(
        r'<a\b[^>]*\bclass=["\']?media-link\b[^>]*\bhref=["\']?'
        r'(https?://[^"\'>\s]+?\.(?:gif|jpe?g|png))["\']?[^>]*>'
        r'\s*(<img\b[^>]*\bsrc=(?:"data:[^"]*"|\'data:[^\']*\'|data:[^\s>]+)[^>]*>)',
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(html))
    if not matches:
        return

    print(f"  annualreviews post-capture: {len(matches)} figure(s) to enrich",
          flush=True)

    import base64
    url_to_data = {}
    urls = {m.group(1) for m in matches}
    for fig_url in urls:
        if fig_url in url_to_data:
            continue
        try:
            req = urllib.request.Request(
                fig_url, headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": "https://www.annualreviews.org/",
                },
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get("Content-Type", "image/gif").split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error: {fig_url[:60]}...  ({e})", flush=True)
            continue
        url_to_data[fig_url] = (
            "data:" + ct + ";base64," + base64.b64encode(blob).decode("ascii")
        )

    modified = False
    for m in matches:
        fig_url = m.group(1)
        img_tag = m.group(2)
        data_url = url_to_data.get(fig_url)
        if not data_url:
            continue
        new_img = re.sub(
            r'\bsrc=(?:"data:[^"]*"|\'data:[^\']*\'|data:[^\s>]+)',
            f'src="{data_url}"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        print(f"    inlined {fig_url.rsplit('/',1)[-1]}  ({len(data_url)} b)",
              flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _cshlp_inline_figures(saved_path, port):
    """Post-capture hook: refetch cshlp inline thumbnails as .large.jpg.

    cshlp ships each figure as
      `<div class=fig><a class=fig-inline-link href=...F<N>.expansion.html>
        <img src=...F<N>.small.gif>`
    The pre-capture `_CSHLP_FIGURES_FIX_JS` rewrites the img src to the
    `.large.jpg` URL before SingleFile inlines, but old captures and
    captures where SingleFile saved before the JS swap completed end up
    with the small thumbnail (~200 px wide, illegible at 720-px column
    width). This hook walks the saved HTML, locates each
    fig-inline-link's `.expansion.html` href, swaps to `.large.jpg`,
    fetches via urllib, and replaces the inner img's src with the
    base64-encoded large image.

    `port` is unused; kept for post_capture signature symmetry.
    """
    del port
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Locate each <a class=fig-inline-link>...<img>. href and class can
    # appear in either order; match each <a>, check it has both
    # attributes, then capture the inner img.
    matches = []
    for m in re.finditer(
        r'<a\b([^>]*)>\s*(<img\b[^>]*>)', html, re.IGNORECASE,
    ):
        attrs = m.group(1)
        if "fig-inline-link" not in attrs:
            continue
        hm = re.search(
            r'\bhref=("([^"]*)"|\'([^\']*)\'|([^\s>]+))', attrs,
        )
        if not hm:
            continue
        href = hm.group(2) or hm.group(3) or hm.group(4)
        if not href.endswith(".expansion.html"):
            continue
        # Synthesize a match-like object
        matches.append((m.start(2), m.end(2), m.group(2), href))
    if not matches:
        return

    print(f"  cshlp post-capture: {len(matches)} figure(s) to refetch",
          flush=True)

    import base64
    url_to_data = {}
    for img_start, img_end, img_tag, href in matches:
        large_url = re.sub(r'\.expansion\.html$', '.large.jpg', href)
        if large_url in url_to_data:
            continue
        try:
            req = urllib.request.Request(
                large_url, headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": href,
                },
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get(
                    "Content-Type", "image/jpeg"
                ).split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error: {large_url[-50:]} ({e})", flush=True)
            continue
        url_to_data[large_url] = ("data:" + ct + ";base64,"
                                  + base64.b64encode(blob).decode("ascii"))

    modified = False
    for img_start, img_end, img_tag, href in matches:
        large_url = re.sub(r'\.expansion\.html$', '.large.jpg', href)
        data_url = url_to_data.get(large_url)
        if not data_url:
            continue
        new_img = re.sub(
            r'\bsrc=("[^"]*"|\'[^\']*\'|[^\s>]+)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        print(f"    inlined {large_url.rsplit('/', 1)[-1]} "
              f"({len(data_url)} b)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _biorxiv_inline_figures(saved_path, port):
    """Post-capture hook: refetch bioRxiv equation embeds via urllib.

    bioRxiv equation/formula embeds (`<img class="highwire-embed
    lazyloaded" src=data:, data-src=…/embed/graphic-N.gif>`) fail to
    inline during the SingleFile pass — the CDN serves them only with a
    valid Referer header, and SingleFile's deferred image fetch runs
    without one. SingleFile additionally appends `.backup.<timestamp>`
    suffixes onto the data-src after each retry. Strategy: walk every
    broken `<img …highwire-embed…>`, strip any `.backup.<digits>`
    suffix(es) from the data-src, refetch via urllib server-side with a
    `Referer: https://www.biorxiv.org/` header, and inline the bytes as
    a base64 data URL. `port` is unused; server-side fetch.
    """
    del port
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    pairs = []  # (img_tag, clean_url)
    for start_m in re.finditer(
        r'<img\b[^<>]*\bhighwire-embed\b', html,
    ):
        s = start_m.start()
        i = s
        in_sq = in_dq = False
        while i < len(html):
            c = html[i]
            if c == "'" and not in_dq:
                in_sq = not in_sq
            elif c == '"' and not in_sq:
                in_dq = not in_dq
            elif c == '>' and not in_sq and not in_dq:
                break
            i += 1
        if i >= len(html):
            continue
        img_tag = html[s:i + 1]
        if not re.search(
            r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?)[\s>]', img_tag,
        ):
            continue
        dsrc_m = re.search(
            r'\bdata-src=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            img_tag,
        )
        if not dsrc_m:
            continue
        url = unescape(
            dsrc_m.group(1) or dsrc_m.group(2) or dsrc_m.group(3),
        )
        # Strip any .backup.<timestamp> suffix(es) SingleFile appended
        # on failed retries; timestamp may be an integer or a float
        # (e.g. `.backup.1751516754` or `.backup.1751516754.2611`). The
        # canonical URL is the bare CDN path with no suffix.
        clean = re.sub(r'(?:\.backup\.\d+(?:\.\d+)?)+$', '', url)
        pairs.append((img_tag, clean))

    if not pairs:
        return

    print(f"  biorxiv post-capture: {len(pairs)} embed(s) to refetch",
          flush=True)

    import base64
    url_to_data = {}
    for _, clean_url in pairs:
        if clean_url in url_to_data:
            continue
        try:
            req = urllib.request.Request(
                clean_url, headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": "https://www.biorxiv.org/",
                },
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get(
                    "Content-Type", "image/gif",
                ).split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error: {clean_url[-60:]} ({e})", flush=True)
            continue
        url_to_data[clean_url] = (
            "data:" + ct + ";base64,"
            + base64.b64encode(blob).decode("ascii")
        )

    modified = False
    for img_tag, clean_url in pairs:
        data_url = url_to_data.get(clean_url)
        if not data_url:
            continue
        new_img = re.sub(
            r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        ident = clean_url.rsplit('/', 1)[-1]
        print(f"    inlined {ident} ({len(data_url)} chars)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _jci_inline_figures(saved_path, port):
    """Post-capture hook: inline JCI figure thumbnails via CloudFront.

    JCI articles ship every figure as
      `<a href=https://[www.|insight.]jci.org/articles/view/<ID>/figure/<N>>
        <img class=figure_thumbnail src=data:, alt=… title=…>`
    The pre-capture `_JCI_FIGURES_FIX_JS` browser-script normally
    rewrites `<img src>` to a derivable CloudFront URL before SingleFile
    captures, but on slow loads (5+ figures, busy CDN) some images miss
    the swap and end up with the empty placeholder. The medium-resolution
    JPEG lives at a deterministic CloudFront URL derived from the
    article ID + figure number on the parent `<a href>`:
      bucket = (int(article_id) // 1000) * 1000
      url = https://dm5migu4zj3pb.cloudfront.net/manuscripts/<bucket>/
            <article_id>/medium/JCI<article_id>.f<N>.jpg
    Substring `jci.org` matches both `www.jci.org` and `insight.jci.org`.
    `port` is unused; server-side fetch.
    """
    del port
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    pair_re = re.compile(
        r'<a\s+href=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))[^>]*>\s*'
        r'(<img\b[^<>]*?\bclass=("[^"]*\bfigure_thumbnail\b[^"]*"'
        r"|'[^']*\bfigure_thumbnail\b[^']*'"
        r'|[^\s>]*\bfigure_thumbnail\b[^\s>]*)[^>]*>)',
        re.IGNORECASE,
    )
    pairs = []  # (cdn_url, img_tag)
    for m in pair_re.finditer(html):
        href = m.group(1) or m.group(2) or m.group(3)
        img_tag = m.group(4)
        if "data:image/jpeg;base64" in img_tag or "data:image/png;base64" in img_tag:
            continue
        if not re.search(
            r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?)[\s>]', img_tag,
        ):
            continue
        href_clean = unescape(href).rstrip("/")
        href_m = re.search(
            r'/articles/view/(\d+)/figure/(\d+)$',
            href_clean,
            re.IGNORECASE,
        )
        if not href_m:
            continue
        article_id = href_m.group(1)
        fig_num = href_m.group(2)
        bucket = (int(article_id) // 1000) * 1000
        cdn_url = (
            f"https://dm5migu4zj3pb.cloudfront.net/manuscripts/{bucket}/"
            f"{article_id}/medium/JCI{article_id}.f{fig_num}.jpg"
        )
        pairs.append((cdn_url, img_tag))

    if not pairs:
        return

    print(f"  jci post-capture: {len(pairs)} figure(s) to enrich",
          flush=True)

    import base64
    url_to_data = {}
    for cdn_url, _ in pairs:
        if cdn_url in url_to_data:
            continue
        try:
            req = urllib.request.Request(
                cdn_url, headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": "https://www.jci.org/",
                },
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get(
                    "Content-Type", "image/jpeg",
                ).split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error: {cdn_url[-60:]} ({e})", flush=True)
            continue
        url_to_data[cdn_url] = (
            "data:" + ct + ";base64,"
            + base64.b64encode(blob).decode("ascii")
        )

    modified = False
    for cdn_url, img_tag in pairs:
        data_url = url_to_data.get(cdn_url)
        if not data_url:
            continue
        new_img = re.sub(
            r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        ident = cdn_url.rsplit('/', 1)[-1]
        print(f"    inlined {ident} ({len(data_url)} chars)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _nature_inline_figures(saved_path, port):
    """Post-capture hook: refetch any Nature figure with empty `data:,` src.

    The pre-capture `_NATURE_FIGURES_FIX_JS` swaps each
    `<picture><img>` src to the matching JSON-LD lw1200 URL before
    SingleFile inlines. When an individual fetch fails (transient
    network / CDN throttling / SingleFile timeout) the resulting saved
    src is `data:,` (empty). This hook deterministically re-fetches via
    Python urllib and inlines the bytes as a base64 data URL, matching
    the figure to its URL by DOM order against the JSON-LD `image`
    array.

    `port` is unused; kept for post_capture signature symmetry.
    """
    del port
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Extract JSON-LD URL list. The image array lives on the
    # ScholarlyArticle node which is usually nested inside `mainEntity`
    # of the page-level @type=WebPage block, but can also sit at the
    # top level — handle both shapes.
    def _find_image_list(node):
        if isinstance(node, dict):
            v = node.get("image")
            if isinstance(v, list) and v and all(
                isinstance(x, str) for x in v
            ):
                return v
            for k in ("mainEntity", "@graph"):
                sub = node.get(k)
                found = _find_image_list(sub)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _find_image_list(item)
                if found:
                    return found
        return None

    urls = []
    for sm in re.finditer(
        r'<script[^>]*type=["\']?application/ld\+json["\']?[^>]*>'
        r'(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(sm.group(1))
        except Exception:
            continue
        urls = _find_image_list(data) or []
        if urls:
            break
    if not urls:
        return

    # GROUP 1: Article-body figures.
    # Each <picture class=c-article-section__figure-picture> wraps one
    # inline figure's <img> in DOM order matching the JSON-LD list.
    pic_iter = list(re.finditer(
        r'<picture[^>]*\bc-article-section__figure-picture\b[^>]*>\s*'
        r'(<img\b[^>]*>)',
        html,
    ))

    def _img_src(img_tag):
        # Use (?:^|\s) lookbehind-ish prefix instead of \b because \b
        # matches inside `data-src=` (the boundary between `-` and `s`).
        # That false-positive made the Nature hook read data-src URLs
        # as the bare src and miss every broken figure with both attrs.
        m = re.search(
            r'(?:^|\s)src=("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
            img_tag,
        )
        if not m:
            return None
        return m.group(2) or m.group(3) or m.group(4)

    def _img_data_src(img_tag):
        # Use (?:^|\s) prefix so we ONLY match the data-src attribute,
        # never a substring of any other attr. Returns absolute URL,
        # promoting protocol-relative `//host/...` to `https://host/...`.
        m = re.search(
            r'(?:^|\s)data-src=("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
            img_tag,
        )
        if not m:
            return None
        url = unescape(m.group(2) or m.group(3) or m.group(4))
        if url.startswith("//"):
            return "https:" + url
        return url

    broken = []
    for idx, pm in enumerate(pic_iter):
        img_tag = pm.group(1)
        if _img_src(img_tag) != "data:,":
            continue
        # Prefer the image's own `data-src` URL when present (handles
        # extended-data / `esm/` figures that JSON-LD omits and any
        # figure index past the JSON-LD list length). Fall back to
        # JSON-LD by index for figures that lack data-src.
        url = _img_data_src(img_tag) or (
            urls[idx] if idx < len(urls) else None
        )
        if not url:
            continue
        broken.append((idx, pm.start(1), pm.end(1), img_tag, url))

    # GROUP 2: Reading-companion sidebar figures.
    # Nature's post-2020 layout duplicates each article figure as a
    # thumbnail inside `<div class="c-reading-companion__panel
    # c-reading-companion__figures">`. SingleFile inlines the article-
    # body `<picture>`s but the sidebar copies often time out and stay
    # at `src=data:,`. The sidebar figures map 1:1 to the JSON-LD list
    # (same article figures, same DOM order) — walk them with a fresh
    # index from 0. Each sidebar `<img>` also carries its own data-src
    # which is preferred when present (covers extended-data figures
    # that ride along in the same panel).
    rc_match = re.search(
        r'<div[^>]*\bc-reading-companion__figures\b[^>]*>',
        html,
    )
    if rc_match:
        rc_start = rc_match.start()
        rc_section = html[rc_start:rc_start + 200000]
        for idx, pm in enumerate(re.finditer(
            r'<picture[^>]*>\s*(<img\b[^>]*>)',
            rc_section,
        )):
            img_tag = pm.group(1)
            if _img_src(img_tag) != "data:,":
                continue
            url = _img_data_src(img_tag) or (
                urls[idx] if idx < len(urls) else None
            )
            if not url:
                continue
            full_start = rc_start + pm.start(1)
            full_end = rc_start + pm.end(1)
            broken.append((idx, full_start, full_end, img_tag, url))

    # GROUP 3: Catch-all for any remaining `<img>` whose src=data:, is
    # NOT inside the article-body or sidebar containers — supplementary
    # figures, extended-data panels, biology callouts, etc. These all
    # carry their own `data-src` URL on the same tag; ignore the ones
    # we already collected (by start position).
    seen_starts = {b[1] for b in broken}
    for m in re.finditer(
        r'<img\b[^<>]*?\bdata-src=', html,
    ):
        s = m.start()
        if s in seen_starts:
            continue
        # Walk to the real end of the tag (quote-aware).
        i = s
        in_sq = in_dq = False
        while i < len(html):
            c = html[i]
            if c == "'" and not in_dq:
                in_sq = not in_sq
            elif c == '"' and not in_sq:
                in_dq = not in_dq
            elif c == '>' and not in_sq and not in_dq:
                break
            i += 1
        if i >= len(html):
            continue
        img_tag = html[s:i + 1]
        if _img_src(img_tag) != "data:,":
            continue
        url = _img_data_src(img_tag)
        if not url or "media.springernature.com" not in url:
            continue
        broken.append((-1, s, i + 1, img_tag, url))

    if not broken:
        return

    print(f"  nature post-capture: {len(broken)} broken figure(s) "
          f"to refetch", flush=True)

    import base64
    # Process from end so byte positions don't shift.
    for idx, start, end, img_tag, fig_url in sorted(
        broken, key=lambda b: -b[1]
    ):
        try:
            req = urllib.request.Request(
                fig_url, headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": "https://www.nature.com/",
                },
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get(
                    "Content-Type", "image/png"
                ).split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error fig{idx + 1}: {e}", flush=True)
            continue
        data_url = ("data:" + ct + ";base64,"
                    + base64.b64encode(blob).decode("ascii"))
        new_img = re.sub(
            r'\bsrc=(?:"data:,"|\'data:,\'|data:,)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        html = html[:start] + new_img + html[end:]
        print(f"    inlined fig{idx + 1}: "
              f"{fig_url.rsplit('/', 1)[-1]} ({len(blob)} b)", flush=True)

    with open(saved_path, "w", encoding="utf-8") as f:
        f.write(html)


def _aging_us_inline_figures(saved_path, port):
    """Post-capture hook: inline aging-us.com figure images via urllib.

    aging-us.com is a Next.js SPA that ships every figure as
      `<a data-figure-id=f<N> href=https://www.aging-us.com/article/<id>/figure/f<N>/large/>
        <img class="max-w-full mx-auto border border-slate-200" src=data:,>`
    Bytes are lazy-loaded in the browser after hydration; SingleFile typically
    captures before hydration completes (or before the swapped src finishes
    fetching), leaving the empty `data:,` placeholder.

    The high-resolution PNG lives at a predictable CDN URL derived from the
    `<a data-figure-id>`'s href:
      `https://www.aging-us.com/article/<id>/figure/<fid>/large/`
        -> `https://cdn.aging-us.com/article/<id>/figure/<fid>/large.png`

    Strategy: walk every `<a data-figure-id=...>...<img class=max-w-full ...>`
    pair, derive the CDN URL, fetch via urllib server-side (cdn.aging-us.com
    serves the PNG with a permissive CORS/edge cache), and inline as a data
    URL. Skip images that already carry real bytes (`base64` substring in src).

    `port` is unused; kept for post_capture signature symmetry.
    """
    del port  # server-side fetch; no browser tab needed
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # `<a data-figure-id=fX href=...> ... <img class=...max-w-full...>`. The
    # img is the immediate first child but allow whitespace/newlines between
    # the two tags. data-figure-id and href appear in that order in observed
    # captures, but be defensive about attribute quoting (often unquoted in
    # SingleFile output).
    pair_re = re.compile(
        r'<a\b[^>]*\bdata-figure-id=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))'
        r'[^>]*\bhref=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))'
        r'[^>]*>\s*'
        r'(<img\b[^>]*\bclass=[^>]*max-w-full[^>]*>)',
        re.IGNORECASE,
    )
    pairs = []  # (fid, href, img_tag)
    for m in pair_re.finditer(html):
        fid = m.group(1) or m.group(2) or m.group(3)
        href = m.group(4) or m.group(5) or m.group(6)
        img_tag = m.group(7)
        # Skip images that already have real inline bytes (e.g. an earlier
        # post-capture run, or the rare case where the SPA hydrated before
        # SingleFile saved).
        if "base64" in img_tag:
            continue
        # Derive the CDN URL from the article-level href. The href ends
        # `/article/<id>/figure/<fid>/large/` (with trailing slash).
        href_clean = unescape(href).rstrip("/")
        cdn_m = re.search(
            r'/article/(\d+)/figure/([^/]+)/large$', href_clean,
        )
        if not cdn_m:
            continue
        article_id = cdn_m.group(1)
        # The CDN is case-strict: newer articles serve `f<N>` (lowercase)
        # and older articles (e.g. id 100141) serve `F<N>` (uppercase). The
        # on-page href already encodes the correct casing per article, so
        # preserve it verbatim instead of normalising.
        cdn_fid = cdn_m.group(2)
        cdn_url = (
            f"https://cdn.aging-us.com/article/{article_id}"
            f"/figure/{cdn_fid}/large.png"
        )
        pairs.append((fid, cdn_url, img_tag))
    if not pairs:
        return

    print(f"  aging-us post-capture: {len(pairs)} figure(s) to enrich",
          flush=True)

    import base64
    url_to_data = {}
    for _, cdn_url, _ in pairs:
        if cdn_url in url_to_data:
            continue
        try:
            req = urllib.request.Request(
                cdn_url, headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": "https://www.aging-us.com/",
                },
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get(
                    "Content-Type", "image/png",
                ).split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error: {cdn_url[-60:]} ({e})", flush=True)
            continue
        url_to_data[cdn_url] = (
            "data:" + ct + ";base64,"
            + base64.b64encode(blob).decode("ascii")
        )

    modified = False
    for fid, cdn_url, img_tag in pairs:
        data_url = url_to_data.get(cdn_url)
        if not data_url:
            print(f"    skip {fid}  (no data)", flush=True)
            continue
        # Replace `src=data:,` (unquoted, single- or double-quoted).
        new_img = re.sub(
            r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        print(f"    inlined {fid}  ({len(data_url)} chars)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _bmj_inline_figures(saved_path, port):
    """Post-capture hook: refetch bmj inline thumbnails as .large.jpg.

    BMJ journals (heart.bmj.com, emj.bmj.com, rapm.bmj.com, etc.) run on
    the HighWire platform and ship each figure as
      `<div id=F<N> class=fig><div class=highwire-figure>
        <div class=fig-inline-img-wrapper><div class=fig-inline-img>
          <a href=...F<N>.large.jpg?width=...&height=...&carousel=1
             class="... colorbox-load ...">
            <span><img src=data:image/gif;base64,...
                       data-src=...F<N>.medium.gif width=440 height=196></span>`
    The medium.gif (~263-440 px native) gets inlined by SingleFile after
    `_LAZYLOAD_FIX_JS` swaps src ← data-src, but it upscales to the 716-px
    column width and renders as a low-resolution thumbnail. The
    full-resolution `F<N>.large.jpg` URL is directly available on the
    parent `<a class=colorbox-load>` and on the sibling
    `<a class=highwire-figure-link-newtab>` — no sub-page indirection.

    Strategy (CDP same-origin batch fetch): walk every `<img>` whose
    `data-src` matches the `.../F<N>.medium.<ext>` HighWire pattern,
    derive the `.large.jpg` URL by simple substitution, then open one
    same-origin tab at the landing URL and `fetch()` each high-res image
    in parallel. urllib won't work because Cloudflare bot protection
    (`cf-mitigated: challenge`) on bmj.com 403s any request without a
    valid `__cf_bm` cookie; the live tab carries that cookie.
    """
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Match each `<img>` whose data-src is a HighWire `.medium.<ext>` URL
    # (gif/jpg/png). data-src may be quoted or unquoted; capture both the
    # full <img> tag (for replacement) and the medium URL (for substitution).
    img_re = re.compile(
        r'<img\b[^>]*\bdata-src=(?:"([^"]+\.medium\.(?:gif|jpg|jpeg|png))"'
        r"|'([^']+\.medium\.(?:gif|jpg|jpeg|png))'"
        r'|([^\s>]+\.medium\.(?:gif|jpg|jpeg|png)))[^>]*>',
        re.IGNORECASE,
    )
    pairs = []  # (img_tag, large_url)
    for m in img_re.finditer(html):
        img_tag = m.group(0)
        medium_url = unescape(m.group(1) or m.group(2) or m.group(3))
        large_url = re.sub(
            r'\.medium\.(?:gif|jpg|jpeg|png)$', '.large.jpg', medium_url,
            flags=re.IGNORECASE,
        )
        if large_url == medium_url:
            continue
        pairs.append((img_tag, large_url))
    if not pairs:
        return

    # Landing URL lives in SingleFile's header comment at the top of the file.
    url_m = re.search(
        r"Page saved with SingleFile\s+url:\s*(\S+)", html[:2000],
    )
    if not url_m:
        # Fall back to deriving the landing origin from the first .large.jpg
        # URL — gives same-origin cookies even without the header.
        from urllib.parse import urlsplit
        parts = urlsplit(pairs[0][1])
        landing_url = f"{parts.scheme}://{parts.netloc}/"
    else:
        landing_url = url_m.group(1)

    print(f"  bmj post-capture: {len(pairs)} figure(s) to refetch",
          flush=True)

    # Open one same-origin tab at the landing URL.
    try:
        target = _cdp_open_tab(landing_url, port)
        tab_id = target["id"]
        ws_url = target["webSocketDebuggerUrl"]
    except Exception as e:
        print(f"    error: could not open landing tab ({e})", flush=True)
        return
    try:
        # Let the landing tab settle (Cloudflare challenge + cookies).
        time.sleep(15)
        # Deduplicate URLs (same .large.jpg requested only once).
        unique_urls = sorted({u for _, u in pairs})
        urls_js = json.dumps(unique_urls)
        expr = r"""
        (async function() {
            const urls = __URLS__;
            async function one(url) {
                try {
                    const blob = await fetch(url, {credentials: 'include'})
                        .then(r => r.ok ? r.blob() : null);
                    if (!blob) return {url, error: 'fetch-failed'};
                    const dataUrl = await new Promise((res, rej) => {
                        const r = new FileReader();
                        r.onloadend = () => res(r.result);
                        r.onerror = rej;
                        r.readAsDataURL(blob);
                    });
                    return {url, dataUrl, size: dataUrl.length};
                } catch (e) { return {url, error: String(e)}; }
            }
            return Promise.all(urls.map(one));
        })()
        """.replace("__URLS__", urls_js)
        results = _cdp_eval_await(ws_url, expr, timeout=120)
    finally:
        try:
            _cdp_close_tab(tab_id, port)
        except Exception:
            pass

    if not results:
        print("    error: batch fetch returned nothing", flush=True)
        return

    url_to_data = {}
    for r in results:
        if isinstance(r, dict) and r.get("dataUrl"):
            url_to_data[r["url"]] = r["dataUrl"]
        elif isinstance(r, dict):
            print(f"    skip: {r.get('url', '?')[-60:]}  "
                  f"({r.get('error', 'unknown')})", flush=True)

    modified = False
    for img_tag, large_url in pairs:
        data_url = url_to_data.get(large_url)
        if not data_url:
            continue
        # Replace <img>'s src attribute (currently the inlined medium gif
        # bytes or `data:,` placeholder) with the high-res data URL.
        new_img = re.sub(
            r'\bsrc=("[^"]*"|\'[^\']*\'|[^\s>]+)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        # Drop hard-coded width/height from the medium thumbnail so the
        # high-res image renders at the column's natural width.
        new_img = re.sub(r'\bwidth=(["\']?)\d+\1\s*', '', new_img)
        new_img = re.sub(r'\bheight=(["\']?)\d+\1\s*', '', new_img)
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        print(f"    inlined {large_url.rsplit('/', 1)[-1]} "
              f"({len(data_url)} b)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _silverchair_inline_figures(saved_path, port, *, label, referer):
    """Generic post-capture hook for Silverchair-platform publishers.

    Every Silverchair journal (ashpublications.org, portlandpress.com, etc.)
    ships figures as `<img class=content-image src='data:image/svg+xml,
    <placeholder>' data-src=https://<host>.silverchair-cdn.com/.../m_<id>.jpeg
    ?Expires=...&Signature=...&Key-Pair-Id=...>`. (`src` is sometimes the
    even shorter `data:,` placeholder.) The signed JPEG URL on `data-src` is
    valid ~7-700 days from page-load. In practice neither the universal
    `_LAZYLOAD_FIX_JS` swap (src ← data-src) nor SingleFile's deferred-image
    fetch causes the bytes to land in the saved HTML — captures uniformly
    keep the placeholder.

    Strategy: walk every `<img class=content-image>` whose `data-src` is a
    `silverchair-cdn.com` URL, fetch the signed image via urllib server-
    side, and inline as a data URL. The CloudFront signature is self-
    contained — no cookies required. `port` is unused; kept for post_capture
    signature symmetry. `label` prefixes log lines (e.g. "ashpublications");
    `referer` is the page origin sent on each fetch.
    """
    del port  # server-side fetch; no browser tab needed
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Locate each `<img class=content-image ...>` tag and extract its full
    # span. Cannot use a naive `<img[^>]*>` regex because the `src` attribute
    # holds an SVG placeholder whose payload contains literal `>` chars
    # (`<svg ...><rect ...></svg>`), so `[^>]*` truncates the tag at the
    # first `>` inside the SVG. Walk the source instead, tracking whether
    # we are inside a quoted attribute value.
    pairs = []  # (img_tag, signed_url)
    for start_m in re.finditer(
        r'<img\b[^<>]*?\bclass=content-image\b', html,
    ):
        s = start_m.start()
        i = s
        in_squote = False
        in_dquote = False
        while i < len(html):
            c = html[i]
            if c == "'" and not in_dquote:
                in_squote = not in_squote
            elif c == '"' and not in_squote:
                in_dquote = not in_dquote
            elif c == '>' and not in_squote and not in_dquote:
                break
            i += 1
        if i >= len(html):
            continue
        img_tag = html[s:i + 1]
        # Skip images that already carry real bytes (e.g. an earlier
        # post-capture run).
        if "data:image/jpeg;base64" in img_tag or "data:image/png;base64" in img_tag:
            continue
        # Extract the data-src URL (double-quoted in observed fixtures, but
        # also accept single-quoted / unquoted).
        dsrc_m = re.search(
            r'\bdata-src=(?:"([^"]+(?:silverchair-cdn|cdn\.rupress)\.(?:com|org)[^"]+)"'
            r"|'([^']+(?:silverchair-cdn|cdn\.rupress)\.(?:com|org)[^']+)'"
            r'|([^\s>]+(?:silverchair-cdn|cdn\.rupress)\.(?:com|org)[^\s>]+))',
            img_tag,
            re.IGNORECASE,
        )
        if not dsrc_m:
            continue
        signed_url = unescape(
            dsrc_m.group(1) or dsrc_m.group(2) or dsrc_m.group(3)
        )
        pairs.append((img_tag, signed_url))
    if not pairs:
        return

    print(f"  {label} post-capture: {len(pairs)} figure(s) to enrich",
          flush=True)

    import base64
    url_to_data = {}
    for _, signed_url in pairs:
        if signed_url in url_to_data:
            continue
        try:
            req = urllib.request.Request(
                signed_url, headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": referer,
                },
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get(
                    "Content-Type", "image/jpeg",
                ).split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error: {signed_url.split('?', 1)[0][-60:]} "
                  f"({e})", flush=True)
            continue
        url_to_data[signed_url] = (
            "data:" + ct + ";base64,"
            + base64.b64encode(blob).decode("ascii")
        )

    modified = False
    for img_tag, signed_url in pairs:
        data_url = url_to_data.get(signed_url)
        if not data_url:
            continue
        # Replace the img tag's `src` (currently the SVG placeholder) with
        # the high-res data URL. The src attribute in the observed markup
        # is single-quoted because the SVG payload contains double quotes.
        new_img = re.sub(
            r'\bsrc=("[^"]*"|\'[^\']*\'|[^\s>]+)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        ident = signed_url.split('?', 1)[0].rsplit('/', 1)[-1]
        print(f"    inlined {ident} ({len(data_url)} chars)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _oup_inline_figures(saved_path, port):
    """Post-capture hook: inline academic.oup.com (NAR etc.) figures by
    matching each broken `<img>` to a per-figure CDN URL by filename stem.

    OUP (academic.oup.com) is on the Silverchair platform but its figures
    arrive without a `data-src` attribute — instead the image carries
    `<img class=content-image src=data:, data-path-from-xml=<stem>.jpg>`.
    The signed full-resolution `oup.silverchair-cdn.com/.../<stem>.jpeg`
    URLs (matching that filename stem) DO appear elsewhere in the same
    HTML — typically in `<a href=…>` "View large figure" links and other
    metadata blocks. Strategy: collect every silverchair-cdn URL in the
    document, index by filename stem (without extension), then rewrite
    each broken `<img>`'s src by stem-match against `data-path-from-xml`.

    `port` is unused; kept for post_capture signature symmetry. Server-
    side urllib fetch — the CloudFront signature on each URL is self-
    contained, no Cloudflare cookie needed.
    """
    del port
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Index every signed silverchair-cdn URL in the page by filename stem
    # (basename without extension). Same stem → same figure, regardless
    # of which `<a>`/`<meta>` first surfaced the URL.
    url_re = re.compile(
        r'https://oup\.silverchair-cdn\.com/oup/backfile/[^\s"\'<>&]+',
        re.IGNORECASE,
    )
    stem_to_url = {}
    for m in url_re.finditer(html):
        url = unescape(m.group(0))
        fname = url.split('?', 1)[0].rsplit('/', 1)[-1]
        stem = fname.rsplit('.', 1)[0]
        # Drop the silverchair "m_" thumbnail prefix when comparing — full
        # res `gkab965fig1.jpeg` and thumb `m_gkab965fig1.jpeg` share the
        # same article-relative key. Prefer non-prefixed (full-res) URLs.
        bare = stem[2:] if stem.startswith("m_") else stem
        if bare in stem_to_url and stem.startswith("m_"):
            continue  # keep already-stored full-res URL
        stem_to_url[bare] = url

    if not stem_to_url:
        return

    # Locate each `<img class=content-image …>` (quote-aware tag walk
    # because alt text contains literal `>` chars). Pair with the URL
    # whose filename stem matches the tag's `data-path-from-xml`.
    pairs = []
    for start_m in re.finditer(
        r'<img\b[^<>]*?\bclass=content-image\b', html,
    ):
        s = start_m.start()
        i = s
        in_sq = in_dq = False
        while i < len(html):
            c = html[i]
            if c == "'" and not in_dq:
                in_sq = not in_sq
            elif c == '"' and not in_sq:
                in_dq = not in_dq
            elif c == '>' and not in_sq and not in_dq:
                break
            i += 1
        if i >= len(html):
            continue
        img_tag = html[s:i + 1]
        # Skip already-healed / self-contained inline images.
        if "data:image/jpeg;base64" in img_tag or "data:image/png;base64" in img_tag:
            continue
        path_m = re.search(
            r'\bdata-path-from-xml=("([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            img_tag,
        )
        if not path_m:
            continue
        fname = path_m.group(2) or path_m.group(3) or path_m.group(4)
        stem = fname.rsplit('.', 1)[0]
        bare = stem[2:] if stem.startswith("m_") else stem
        signed_url = stem_to_url.get(bare)
        if not signed_url:
            continue
        pairs.append((img_tag, signed_url))

    if not pairs:
        return

    print(f"  oup post-capture: {len(pairs)} figure(s) to enrich",
          flush=True)

    import base64
    url_to_data = {}
    for _, signed_url in pairs:
        if signed_url in url_to_data:
            continue
        try:
            req = urllib.request.Request(
                signed_url, headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": "https://academic.oup.com/",
                },
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get(
                    "Content-Type", "image/jpeg",
                ).split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error: {signed_url.split('?', 1)[0][-60:]} "
                  f"({e})", flush=True)
            continue
        url_to_data[signed_url] = (
            "data:" + ct + ";base64,"
            + base64.b64encode(blob).decode("ascii")
        )

    modified = False
    for img_tag, signed_url in pairs:
        data_url = url_to_data.get(signed_url)
        if not data_url:
            continue
        new_img = re.sub(
            r'\bsrc=("[^"]*"|\'[^\']*\'|[^\s>]+)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        ident = signed_url.split('?', 1)[0].rsplit('/', 1)[-1]
        print(f"    inlined {ident} ({len(data_url)} chars)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _springer_inline_figures(saved_path, port):
    """Post-capture hook: refetch Springer (link.springer.com) figures.

    Springer Nature articles (EMBO J, Cell Death Differ, Nat Commun-on-
    Springer-host, etc.) ship figures in two distinct shapes:

    1. `<img data-src=URL src=data:,>` — lazysizes populated `data-src`
       with the canonical lw685 CDN URL but SingleFile failed to inline
       the fetched bytes. Rescue is purely local — read `data-src`,
       fetch via urllib, substitute into `src`.
    2. `<img aria-describedby="figure-N-desc" src=data:, …>` with NO
       `data-src`. The lw1200 URLs live in the JSON-LD `image` array
       in document order; convert lw1200 → lw685 (685-px column width
       is ample for the article body) and match by figure index.

    Both patterns can co-occur on a single article (older / newer figure
    blocks). The hook handles both in a single pass: process `data-src`
    figures first, then walk the remaining broken `aria-describedby`
    figures and match against the JSON-LD list by sequential index.
    `port` is unused; server-side fetch.
    """
    del port
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    import base64

    # ---- Phase 1: data-src recovery ---------------------------------
    pairs_dsrc = []  # (full_img_tag, url)
    # Quote-aware <img> walker: alt text may contain literal `>` chars.
    for start_m in re.finditer(
        r'<img\b[^<>]*?\bdata-src=', html,
    ):
        s = start_m.start()
        i = s
        in_sq = in_dq = False
        while i < len(html):
            c = html[i]
            if c == "'" and not in_dq:
                in_sq = not in_sq
            elif c == '"' and not in_sq:
                in_dq = not in_dq
            elif c == '>' and not in_sq and not in_dq:
                break
            i += 1
        if i >= len(html):
            continue
        img_tag = html[s:i + 1]
        # Only act on figures whose visible src is the empty placeholder.
        if not re.search(r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?)[\s>]',
                         img_tag):
            continue
        dsrc_m = re.search(
            r'\bdata-src=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            img_tag,
        )
        if not dsrc_m:
            continue
        url = unescape(dsrc_m.group(1) or dsrc_m.group(2) or dsrc_m.group(3))
        if "media.springernature.com" not in url:
            continue
        pairs_dsrc.append((img_tag, url))

    # ---- Phase 2: JSON-LD recovery for figures with no data-src -----
    def _find_image_list(node):
        if isinstance(node, dict):
            v = node.get("image")
            if isinstance(v, list) and v and all(
                isinstance(x, str) for x in v
            ):
                return v
            if isinstance(v, str):
                return [v]
            for k in ("mainEntity", "@graph"):
                sub = node.get(k)
                found = _find_image_list(sub)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _find_image_list(item)
                if found:
                    return found
        return None

    json_urls = []
    for sm in re.finditer(
        r'<script[^>]*type=["\']?application/ld\+json["\']?[^>]*>(.+?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(sm.group(1))
        except Exception:
            continue
        urls = _find_image_list(data) or []
        if urls:
            json_urls = urls
            break

    pairs_jsonld = []
    if json_urls:
        # Walk every broken `<img aria-describedby=figure-N…>` in DOM
        # order. Each missing `data-src` → use json_urls[idx].
        idx = 0
        for m in re.finditer(
            r'<img\b[^<>]*\baria-describedby=["\'][^"\']*figure-\d+',
            html,
        ):
            s = m.start()
            i = s
            in_sq = in_dq = False
            while i < len(html):
                c = html[i]
                if c == "'" and not in_dq:
                    in_sq = not in_sq
                elif c == '"' and not in_sq:
                    in_dq = not in_dq
                elif c == '>' and not in_sq and not in_dq:
                    break
                i += 1
            if i >= len(html):
                continue
            img_tag = html[s:i + 1]
            if not re.search(
                r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?)[\s>]',
                img_tag,
            ):
                idx += 1
                continue
            if "data-src=" in img_tag:
                idx += 1
                continue
            if idx < len(json_urls):
                url = json_urls[idx].replace("lw1200", "lw685")
                pairs_jsonld.append((img_tag, url))
            idx += 1

    pairs = pairs_dsrc + pairs_jsonld
    if not pairs:
        return

    print(f"  springer post-capture: {len(pairs)} broken figure(s) "
          f"to refetch", flush=True)

    url_to_data = {}
    for _, url in pairs:
        if url in url_to_data:
            continue
        try:
            req = urllib.request.Request(
                url, headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": "https://link.springer.com/",
                },
            )
            with polite_urlopen(req, timeout=30) as resp:
                ct = resp.headers.get(
                    "Content-Type", "image/png",
                ).split(";")[0].strip()
                blob = resp.read()
        except Exception as e:
            print(f"    fetch-error: {url.rsplit('/', 1)[-1][:60]} "
                  f"({e})", flush=True)
            continue
        url_to_data[url] = (
            "data:" + ct + ";base64,"
            + base64.b64encode(blob).decode("ascii")
        )

    modified = False
    for img_tag, url in pairs:
        data_url = url_to_data.get(url)
        if not data_url:
            continue
        new_img = re.sub(
            r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        print(f"    inlined {url.rsplit('/', 1)[-1].split('?', 1)[0]} "
              f"({len(data_url)} chars)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _acs_inline_figures(saved_path, port):
    """Post-capture hook: rescue pubs.acs.org `<img class=inline-fig>` placeholders.

    ACS (pubs.acs.org) ships every inline figure as
      `<figure class=article__inlineFigure>
         <button class=figure-viewer__trigger>
           <img class="inline-fig internalNav" src=data:,
                data-lg-src=/cms/<doi>/asset/images/large/<file>.jpeg>
         </button>
         <div class=figure-bottom-links>
           <div class=download-hi-res-img>
             <a href=https://pubs.acs.org/cms/<doi>/asset/images/large/<file>.jpeg>
                High Resolution Image</a>`
    `_ACS_FIGURES_FIX_JS` swaps `<img.inline-fig src> ← <a.download-hi-res-img href>`
    so SingleFile fetches the high-res JPEG. On newer fixtures (e.g.
    Brown_2011, Lee_2020) SingleFile inlines the bytes successfully and the
    saved `<img>` ends up with `src="data:image/jpeg;base64,..."`. On older
    fixtures (e.g. Lin_2001) SingleFile's in-page image fetch occasionally
    fails (Cloudflare race / cross-origin timing) and the swap leaves
    `src=data:,` (empty data URL sentinel).

    Strategy (CDP same-origin batch fetch, mirrors `_bmj_inline_figures`):
    walk every `<img class=inline-fig>` whose src is the empty `data:,`
    placeholder, derive the absolute high-res URL from `data-lg-src=`
    (relative; resolved against `https://pubs.acs.org`) or fall back to
    the sibling `.download-hi-res-img a[href]`, then open one same-origin
    tab on pubs.acs.org and `fetch(url, {credentials: 'include'})` each
    JPEG in parallel. urllib won't work because Cloudflare bot protection
    (`cf-mitigated: challenge`) 403s any request without a valid `__cf_bm`
    cookie; the live tab carries that cookie. Imgs that already hold real
    base64 bytes are skipped, so this hook is a no-op on captures where
    SingleFile already inlined every figure.
    """
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Find every `<img>` tagged `inline-fig` whose `src` is the empty
    # `data:,` placeholder. Capture the full <img> tag so we can rewrite
    # its src in-place. `data-lg-src=` is unquoted in observed captures
    # but be defensive about quoting for robustness.
    img_re = re.compile(
        r'<img\b[^>]*?\bclass=(?:"[^"]*\binline-fig\b[^"]*"'
        r"|'[^']*\binline-fig\b[^']*'"
        r'|[^\s>]*\binline-fig\b[^\s>]*)[^>]*>',
        re.IGNORECASE,
    )
    pairs = []  # (img_tag, abs_url, fig_id)
    for m in img_re.finditer(html):
        img_tag = m.group(0)
        # Skip imgs that already hold real bytes (Brown/Lee path).
        if "data:image/jpeg;base64" in img_tag or "data:image/png;base64" in img_tag:
            continue
        # Only act on the empty `data:,` sentinel — leave anything else
        # (real http URL, real placeholder SVG, etc.) untouched.
        if not re.search(
            r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?(?=[\s>]))',
            img_tag,
        ):
            continue
        # Pull the high-res URL from `data-lg-src=` first; it's the
        # canonical source of the JPEG path. Fall back to the sibling
        # `.download-hi-res-img a[href]` if absent.
        dlg_m = re.search(
            r'\bdata-lg-src=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            img_tag,
            re.IGNORECASE,
        )
        url = None
        if dlg_m:
            url = unescape(dlg_m.group(1) or dlg_m.group(2) or dlg_m.group(3))
        if not url:
            continue
        # Resolve relative URLs against pubs.acs.org.
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://pubs.acs.org" + url
        # Derive a short identifier for log lines.
        id_m = re.search(
            r'\bid=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            img_tag,
        )
        fig_id = (
            (id_m.group(1) or id_m.group(2) or id_m.group(3))
            if id_m else url.rsplit("/", 1)[-1]
        )
        pairs.append((img_tag, url, fig_id))
    if not pairs:
        return

    # Landing URL for the same-origin tab. Pull from SingleFile's header
    # comment when available; otherwise default to the article DOI URL via
    # pubs.acs.org root.
    url_m = re.search(
        r"Page saved with SingleFile\s+url:\s*(\S+)", html[:2000],
    )
    landing_url = url_m.group(1) if url_m else "https://pubs.acs.org/"

    print(f"  acs post-capture: {len(pairs)} figure(s) to refetch",
          flush=True)

    try:
        target = _cdp_open_tab(landing_url, port)
        tab_id = target["id"]
        ws_url = target["webSocketDebuggerUrl"]
    except Exception as e:
        print(f"    error: could not open landing tab ({e})", flush=True)
        return
    try:
        # Let the landing tab settle (Cloudflare challenge + cookies).
        time.sleep(15)
        unique_urls = sorted({u for _, u, _ in pairs})
        urls_js = json.dumps(unique_urls)
        expr = r"""
        (async function() {
            const urls = __URLS__;
            async function one(url) {
                try {
                    const blob = await fetch(url, {credentials: 'include'})
                        .then(r => r.ok ? r.blob() : null);
                    if (!blob) return {url, error: 'fetch-failed'};
                    const dataUrl = await new Promise((res, rej) => {
                        const r = new FileReader();
                        r.onloadend = () => res(r.result);
                        r.onerror = rej;
                        r.readAsDataURL(blob);
                    });
                    return {url, dataUrl, size: dataUrl.length};
                } catch (e) { return {url, error: String(e)}; }
            }
            return Promise.all(urls.map(one));
        })()
        """.replace("__URLS__", urls_js)
        results = _cdp_eval_await(ws_url, expr, timeout=120)
    finally:
        try:
            _cdp_close_tab(tab_id, port)
        except Exception:
            pass

    if not results:
        print("    error: batch fetch returned nothing", flush=True)
        return

    url_to_data = {}
    for r in results:
        if isinstance(r, dict) and r.get("dataUrl"):
            url_to_data[r["url"]] = r["dataUrl"]
        elif isinstance(r, dict):
            print(f"    skip: {r.get('url', '?').rsplit('/', 1)[-1]}  "
                  f"({r.get('error', 'unknown')})", flush=True)

    modified = False
    for img_tag, url, fig_id in pairs:
        data_url = url_to_data.get(url)
        if not data_url:
            continue
        # Replace the empty `data:,` placeholder with the high-res data
        # URL. Match unquoted / single-quoted / double-quoted variants.
        new_img = re.sub(
            r'\bsrc=(?:"data:,?"|\'data:,?\'|data:,?(?=[\s>]))',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        print(f"    inlined {fig_id} ({len(data_url)} chars)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _molbiolcell_inline_figures(saved_path, port):
    """Post-capture hook: replace molbiolcell.org figure thumbnails with high-res JPEGs.

    Mol Biol Cell (www.molbiolcell.org, Atypon platform — same shape as
    onlinelibrary.wiley.com) ships every figure as
      `<img class=figure__image
            src="data:image/jpeg;base64,<~10-30 KB 500-px thumbnail>"
            data-lg-src=/cms/asset/<uuid>/<asset-id>.{jpg,png}>`
    The base64 src is the publisher's 500-px thumbnail; rendered at the
    658-px column width it visibly upscales / blurs. The full-resolution
    image is exposed on the same `<img>` via `data-lg-src=` (relative path,
    resolved against `https://www.molbiolcell.org`).

    Strategy (server-side urllib, mirrors `_silverchair_inline_figures` /
    `_aging_us_inline_figures`): walk every `<img class=figure__image>`
    that carries a `data-lg-src` attribute, fetch the high-res asset via
    urllib (Cloudflare-fronted but no `cf-mitigated: challenge` — public
    requests succeed without cookies), and replace the thumbnail `src`
    with the full-resolution data URL. Skips imgs whose `data-lg-src`
    is missing. `port` is unused; kept for post_capture signature symmetry.
    """
    del port  # server-side fetch; no browser tab needed
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # The base64 thumbnail src and the data-lg-src attribute both contain
    # only URL-safe / base64 chars — no literal `>` — so a plain `[^>]*`
    # match captures the full <img> tag. The `class=figure__image` attribute
    # appears unquoted in observed SingleFile output, but tolerate quotes.
    img_re = re.compile(
        r'<img\b[^>]*?\bclass=(?:"[^"]*\bfigure__image\b[^"]*"'
        r"|'[^']*\bfigure__image\b[^']*'"
        r'|[^\s>]*\bfigure__image\b[^\s>]*)[^>]*>',
        re.IGNORECASE,
    )
    pairs = []  # (img_tag, abs_url, fig_id)
    for m in img_re.finditer(html):
        img_tag = m.group(0)
        # Pull `data-lg-src=` (unquoted in observed captures, but accept
        # all three quoting styles defensively).
        dlg_m = re.search(
            r'\bdata-lg-src=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            img_tag,
            re.IGNORECASE,
        )
        if not dlg_m:
            continue
        url = unescape(dlg_m.group(1) or dlg_m.group(2) or dlg_m.group(3))
        # Resolve relative URLs against www.molbiolcell.org.
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://www.molbiolcell.org" + url
        fig_id = url.rsplit("/", 1)[-1]
        pairs.append((img_tag, url, fig_id))
    if not pairs:
        return

    print(f"  molbiolcell post-capture: {len(pairs)} figure(s) to enrich",
          flush=True)

    import base64
    url_to_data = {}
    # Cloudflare in front of www.molbiolcell.org occasionally 403s
    # individual asset fetches that would otherwise succeed (no global
    # `cf-mitigated: challenge` — looks like burst rate-limiting). Retry
    # with progressive backoff before giving up.
    retries = (0, 4.0, 12.0)  # immediate, then 4 s, then 12 s
    for _, url, fig_id in pairs:
        if url in url_to_data:
            continue
        blob = None
        ct = "image/jpeg"
        last_err = None
        for delay in retries:
            if delay:
                time.sleep(delay)
            try:
                req = urllib.request.Request(
                    url, headers={
                        "User-Agent": _BROWSER_UA,
                        "Referer": "https://www.molbiolcell.org/",
                        "Accept": "image/avif,image/webp,image/apng,"
                                  "image/*,*/*;q=0.8",
                    },
                )
                with polite_urlopen(req, timeout=30) as resp:
                    ct = resp.headers.get(
                        "Content-Type", "image/jpeg",
                    ).split(";")[0].strip()
                    blob = resp.read()
                break
            except Exception as e:
                last_err = e
                continue
        if blob is None:
            print(f"    fetch-error: {fig_id} ({last_err})", flush=True)
            continue
        url_to_data[url] = (
            "data:" + ct + ";base64,"
            + base64.b64encode(blob).decode("ascii")
        )

    modified = False
    for img_tag, url, fig_id in pairs:
        data_url = url_to_data.get(url)
        if not data_url:
            continue
        # Replace whatever the current `src=` is (the 500-px base64
        # thumbnail) with the high-res data URL. The src attribute may be
        # double-quoted (newer captures) or unquoted (older captures with
        # the base64 payload running to the next attribute boundary).
        new_img = re.sub(
            r'\bsrc=("[^"]*"|\'[^\']*\'|[^\s>]+)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        print(f"    inlined {fig_id} ({len(data_url)} chars)", flush=True)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


def _singlefile_inline_css_var_figures(
    saved_path, port, label, fig_container_re=None,
):
    """Post-capture hook: promote SingleFile CSS-var background bytes to <img src>.

    Several publishers (pnas.org via Atypon, cshlp.org via HighWire) emit
    figures that SingleFile captures as
      `<img src='data:image/svg+xml,<svg ...><rect fill-opacity=0/></svg>'
            style="...background-image:var(--sf-img-<K>)..."></img>`
    The `<img src>` is a transparent SVG placeholder; the real high-resolution
    image bytes live in a `:root{--sf-img-<K>: url("data:image/...;base64,...")}`
    CSS variable in the page's inline `<style>`. Browsers render correctly via
    the CSS-var background-image, but downstream tooling reading `<img src>`
    only sees the SVG placeholder.

    Strategy (purely local — no network, no browser tab needed):
      1. Build a {var_id: data_url} map from all `--sf-img-<K>: url("data:...")`
         definitions in the saved HTML.
      2. Walk every `<img>` whose `style` contains
         `background-image:var(--sf-img-<K>)` AND whose `src` is an SVG
         `data:image/svg+xml,...` placeholder. Use the same quote-tracking
         tag-walk as `_silverchair_inline_figures` because the SVG payload
         contains literal `>` chars that break naive `<img[^>]*>` regexes.
      3. Rewrite each such `<img src>` to the data URL pulled from the matching
         CSS variable, and neutralise the now-redundant background-image so
         the placeholder doesn't double-render.

    `label` prefixes log lines (e.g. "pnas", "cshlp"). `fig_container_re` is
    an optional compiled regex matching the publisher's article-figure
    container (e.g. `<figure id=fig<N> class=graphic>`); when provided, the
    log distinguishes article-level figures from incidental UI icons that
    also use the CSS-var trick. `port` is unused; kept for post_capture
    signature symmetry.
    """
    del port  # local rewrite; no browser tab needed
    try:
        html = open(saved_path, encoding="utf-8").read()
    except Exception:
        return

    # Build var_id -> data: URL map from all SingleFile CSS-var definitions.
    var_map = {}
    for vm in re.finditer(
        r'--sf-img-(\d+)\s*:\s*url\("(data:[^"]+)"\)', html,
    ):
        var_map[vm.group(1)] = vm.group(2)
    if not var_map:
        return

    # Walk each `<img>` start; can't use a naive `<img[^>]*>` because the SVG
    # placeholder src payload contains literal `>` chars (`<svg ...><rect/>
    # </svg>`). Track quote state to find the real tag terminator.
    pairs = []  # (img_tag, var_id)
    for start_m in re.finditer(r'<img\b', html):
        s = start_m.start()
        i = s
        in_squote = False
        in_dquote = False
        while i < len(html):
            c = html[i]
            if c == "'" and not in_dquote:
                in_squote = not in_squote
            elif c == '"' and not in_squote:
                in_dquote = not in_dquote
            elif c == '>' and not in_squote and not in_dquote:
                break
            i += 1
        if i >= len(html):
            continue
        img_tag = html[s:i + 1]
        bg = re.search(
            r'background-image:\s*var\(--sf-img-(\d+)\)', img_tag,
        )
        if not bg:
            continue
        # Only rewrite when the current src is an SVG placeholder (or
        # the rare `data:,` empty placeholder); leave already-real images
        # alone (idempotent across reruns).
        if "svg+xml" not in img_tag and "data:," not in img_tag:
            continue
        pairs.append((img_tag, bg.group(1)))
    if not pairs:
        return

    # Count figure-level inlining for clearer logging; the bulk count
    # includes small UI icons that also use the CSS-var trick.
    fig_var_ids = set()
    if fig_container_re is not None:
        for fm in fig_container_re.finditer(html):
            chunk = html[fm.end():fm.end() + 5000]
            bg_m = re.search(
                r'background-image:\s*var\(--sf-img-(\d+)\)', chunk,
            )
            if bg_m:
                fig_var_ids.add(bg_m.group(1))

    if fig_container_re is not None:
        print(
            f"  {label} post-capture: {len(pairs)} <img>(s) "
            f"({len(fig_var_ids)} figure-level) to inline",
            flush=True,
        )
    else:
        print(
            f"  {label} post-capture: {len(pairs)} <img>(s) to inline",
            flush=True,
        )

    modified = False
    inlined_vars = set()
    for img_tag, var_id in pairs:
        data_url = var_map.get(var_id)
        if not data_url:
            continue
        # Replace src=... (the SVG placeholder is single-quoted because its
        # payload contains double quotes; allow other quotings defensively).
        new_img = re.sub(
            r'\bsrc=("[^"]*"|\'[^\']*\'|[^\s>]+)',
            'src="' + data_url + '"',
            img_tag, count=1,
        )
        # Strip the now-redundant background-image var so the placeholder
        # SVG doesn't double-render and so re-runs are no-ops.
        new_img = re.sub(
            r'background-image:\s*var\(--sf-img-\d+\)\s*!important;?',
            'background-image:none!important;',
            new_img,
        )
        if new_img == img_tag:
            continue
        html = html.replace(img_tag, new_img, 1)
        modified = True
        if fig_container_re is None or var_id in fig_var_ids:
            if var_id not in inlined_vars:
                print(
                    f"    inlined --sf-img-{var_id} ({len(data_url)} chars)",
                    flush=True,
                )
                inlined_vars.add(var_id)

    if modified:
        with open(saved_path, "w", encoding="utf-8") as f:
            f.write(html)


# Compiled figure-container regexes for the SingleFile CSS-var inliner.
# Keep these module-level so the post_capture lambdas don't recompile per
# call.
_PNAS_FIG_RE = re.compile(
    r'<figure\s+id=[Ff](?:ig)?\d+\s+class=graphic[^>]*>',
)
_CSHLP_FIG_RE = re.compile(
    r'<div[^>]*\bid=F\d+[^>]*\bclass="?fig\b[^>]*>',
)


def _get_post_capture(url):
    """Return the post_capture callable for a URL, or None if no rule matches."""
    if not url:
        return None
    for key, rule in _PUBLISHER_RULES.items():
        if key in url:
            return rule.get("post_capture")
    return None


def _post_capture_needs_browser(url):
    """Return True if the matching publisher's post_capture hook needs CDP.

    Hooks that open same-origin CDP tabs (e.g. _acs_inline_figures,
    _bmj_inline_figures, _iucr_inline_figures) need a live browser at
    `port`; hooks that only call urllib do not. Heal callers consult this
    to decide whether to start a browser before invoking the hook.
    """
    if not url:
        return False
    for key, rule in _PUBLISHER_RULES.items():
        if key in url:
            return bool(rule.get("post_capture_needs_browser"))
    return False


_BROKEN_IMG_RE = re.compile(
    r'<img\b[^>]*?\bsrc=(?:"data:,?"|\'data:,?\'|data:,?)[\s>]',
    re.IGNORECASE,
)

# Tags whose `data:,` `src` is structural UI chrome (badges, tracking
# pixels, branding logos, JS-injected widgets), NOT a failed-to-fetch
# article figure. Heal hooks can never recover these — they are
# intentionally absent (the real asset is JS-injected post-capture, or
# is a static UI affordance that has no source URL). Recognized across
# the corpus:
#   - Altmetric badge     `alt="Article has an altmetric score of …"`
#   - 1x1/0x0 tracking pixel `height=1 width=1` (acs/ashpublications/bmj)
#                           `width=0 height=0` (aacrjournals)
#   - Journal/branding logo `class=… __journal__logo …` (frontiersin),
#                           `class=fb-featured-image` (oup banner),
#                           `class=brand-logo`, `class=…__logo…`
#   - ACS figure-viewer placeholder slot `class=fv-img` — the real fig
#     bytes live in the *sibling* `<img>` next to it
#   - Wiley QR-code placeholder `class=qrcode__image` — only generated
#     after user clicks "Get QR"
#   - NIH/PMC header dropdown chevrons `class=usa-icon` /
#     `class=ncbi-header__login-dropdown-icon`
#   - HighWire (cshlp/etc.) "Current Issue" cover thumbnail
#     `alt="Current Issue"` — sidebar widget, not article body
#   - HighWire (cshlp/etc.) house-ad `class=adborder0` and ad images
#     inside `<a href=…/cgi/adclick/…>` (Advansta SDS-PAGE Visioband etc.)
# Keep this list narrow on purpose: anything that looks like a real
# figure (`content-image`, `figure-N-desc`, `inline-fig`, `media-link`,
# `figure__image`, `figure_thumbnail`, `xfigimg`, etc.) MUST keep
# counting so the heal hook still fires for the publishers that need it.
_UI_PLACEHOLDER_RES = (
    re.compile(r'\baltmetric\b', re.IGNORECASE),
    re.compile(
        r'\b(?:height=["\']?1["\']?\s+width=["\']?1["\']?'
        r'|width=["\']?1["\']?\s+height=["\']?1["\']?'
        r'|height=["\']?0["\']?\s+width=["\']?0["\']?'
        r'|width=["\']?0["\']?\s+height=["\']?0["\']?)',
        re.IGNORECASE,
    ),
    re.compile(
        r'\bclass=(?:"[^"]*|\'[^\']*|)'
        r'(?:[A-Za-z_-]+__journal__logo'
        r'|fb-featured-image'
        r'|brand[-_]?logo'
        r'|[A-Za-z_-]+__logo'
        r'|fv-img'
        r'|qrcode__'
        r'|usa-icon'
        r'|usa-search__submit-icon'  # PMC search button icon (BEM variant)
        r'|ncbi-header__login'
        r'|adborder\d*)',
        re.IGNORECASE,
    ),
    re.compile(r'\balt="?Current\s+Issue"?', re.IGNORECASE),
    # Hidden UI placeholders (display:none) — never visible, never an
    # article figure. Common on PMC pages and on capsule UI components
    # that ship a placeholder for a JS-injected image.
    re.compile(r'\bstyle=["\']?display\s*:\s*none', re.IGNORECASE),
)


_AD_CONTEXT_RE = re.compile(
    r'/cgi/adclick/|/cgi/ads/|/ad-server/|googletag\.|googleads\.',
    re.IGNORECASE,
)


def _is_ad_context_placeholder(html, tag_start):
    """Return True if the broken `<img>` at tag_start sits inside an ad link.

    HighWire publishers (cshlp, bmj) wrap house-ad images in
    `<a href=".../cgi/adclick/...">`; the inner `<img>` is dynamically
    populated and ships as `<img src=data:,>` in static captures. The
    image content is intentionally absent and not recoverable by any
    heal hook.
    """
    before = html[max(0, tag_start - 600):tag_start]
    last_open_a = before.rfind("<a ")
    if last_open_a == -1:
        return False
    last_close_a = before.rfind("</a>")
    if last_close_a > last_open_a:
        return False
    a_tag = before[last_open_a:]
    return bool(_AD_CONTEXT_RE.search(a_tag))


def _is_ui_placeholder(tag):
    """Return True if a `<img src=data:,>` tag is structural UI chrome."""
    return any(p.search(tag) for p in _UI_PLACEHOLDER_RES)


def _full_img_tag(html, start):
    """Walk from `<` at `start` to the matching `>` honoring quoted attrs.

    The src attribute on broken-figure tags often holds a quoted SVG
    placeholder whose payload contains literal `>` chars; a naive
    `<img[^>]*>` regex truncates the tag at the first `>` inside the
    SVG. Walk the source instead, tracking single/double quote state.
    Returns the full `<img …>` substring, or "" if no closing `>` found.
    """
    i = start
    in_sq = False
    in_dq = False
    n = len(html)
    while i < n:
        c = html[i]
        if c == "'" and not in_dq:
            in_sq = not in_sq
        elif c == '"' and not in_sq:
            in_dq = not in_dq
        elif c == '>' and not in_sq and not in_dq:
            return html[start:i + 1]
        i += 1
    return ""


def _has_broken_figures(html):
    """Generic publisher-agnostic check for `<img src=data:,>` placeholders
    that represent failed figure fetches (excluding UI chrome).

    SingleFile leaves an empty `data:,` URL in the `src` attribute of
    figure images whose inline-fetch failed during capture. convert_html.py
    uses this count to decide whether to invoke the publisher's
    `post_capture` hook to heal them. Structural UI placeholders
    (Altmetric badges, 1x1 tracking pixels, branding logos, ACS carousel
    placeholders, Wiley QR codes, ad images, etc.) are excluded because
    no heal hook can recover them — they are stable artifacts of
    publisher chrome, not failed asset fetches.

    Returns the count of *healable* broken `<img>` tags found.
    """
    n = 0
    for m in _BROKEN_IMG_RE.finditer(html):
        # The broken-img regex matches only up to the byte after the
        # `data:,` placeholder, not to the closing `>`; UI-class checks
        # need the full tag (e.g. `class=fv-img` may appear after
        # `src=data:,`). Walk to the real tag end before classifying.
        full = _full_img_tag(html, m.start()) or m.group(0)
        if _is_ui_placeholder(full):
            continue
        if _is_ad_context_placeholder(html, m.start()):
            continue
        n += 1
    return n


def _elsevier_pii_to_sciencedirect(url):
    """Rewrite Elsevier linkinghub / ClinicalKey URL -> ScienceDirect PII URL.

    Both linkinghub (`https://linkinghub.elsevier.com/retrieve/pii/<PII>`)
    and ClinicalKey (`https://www.clinicalkey.com/.../1-s2.0-<PII>...` or
    `?v=<PII>`) expose the article's PII. ScienceDirect serves the same
    article at `https://www.sciencedirect.com/science/article/pii/<PII>`
    without ClinicalKey's auth gate on figures. Returns the URL unchanged
    if no PII can be extracted.
    """
    m = re.search(r"(?:/pii/|1-s2\.0-|[?&]v=)([A-Z0-9]{10,})", url)
    if not m:
        return url
    pii = m.group(1)
    return f"https://www.sciencedirect.com/science/article/pii/{pii}"


_PUBLISHER_RULES = {
    "cshlp.org": {
        "url": lambda u: u if u.endswith(".long") else u.rstrip("/") + ".long",
        "wait": "load",
        # Same image-fetch needs as biorxiv: the figure-fix browser-script
        # rewrites <img src> to F<N>.large.jpg (~200 KB), and SingleFile
        # needs time to fetch + embed the larger images.
        # CSHLP (HighWire) figures arrive from SingleFile as
        # `<img src='data:image/svg+xml,<placeholder>'
        #   style="...background-image:var(--sf-img-<N>)...">`, with the real
        # GIF bytes (~28 KB each, ~150-200 px wide thumbnails) embedded in
        # `--sf-img-<N>` CSS variables. Same SingleFile CSS-var trick as
        # pnas; share the generic `_singlefile_inline_css_var_figures`
        # helper to promote each CSS-var data URL to its `<img src>`.
        # Pure local rewrite — no network calls. (El_Hage_2010-style
        # captures that ship native JPEGs are unaffected: the helper
        # only rewrites <img>s whose current src is the SVG placeholder.)
        # Two distinct figure shapes coexist on cshlp HTML:
        #   1. SingleFile CSS-var <img src='data:image/svg+xml,...'> with
        #      the real GIF in `--sf-img-<N>` — handled by the CSS-var
        #      inliner above.
        #   2. Figure-level `<a class=fig-inline-link href=...expansion.html>
        #      <img src=data:,>` — the inliner above doesn't touch these
        #      because there's no CSS var; `_cshlp_inline_figures` refetches
        #      the `.large.jpg` server-side. Wire both in series so a
        #      single capture heals every figure regardless of shape.
        "post_capture": lambda path, port: (
            _singlefile_inline_css_var_figures(
                path, port, label="cshlp", fig_container_re=_CSHLP_FIG_RE,
            ),
            _cshlp_inline_figures(path, port),
        ),
    },
    "biorxiv.org": {
        "url": lambda u: u if u.endswith(".full") else u.rstrip("/") + ".full",
        # Figures use lazysizes (src=1x1 gif, real URL in data-src). The
        # force-lazyload JS runs at 'load' and swaps them in; SingleFile
        # then needs time to fetch + embed the real images.
        "wait_delay": 15000,
        # Equation/formula embeds (`<img class=highwire-embed lazyloaded
        # src=data:, data-src=…/embed/graphic-N.gif>`) fail SingleFile's
        # inline pass — the CDN refuses requests without a Referer.
        # Post-capture refetches them server-side with a biorxiv referer.
        "post_capture": lambda path, port: _biorxiv_inline_figures(path, port),
    },
    "plos.org": {
        # Figure images are JS-populated with `size=inline` (320 px) by
        # default. The plos figure-fix browser-script swaps src to
        # `size=large` (~1500-2000 px, ~150 KB each) — with 6+ figures
        # SingleFile needs ~30 s to fetch + embed all of them. The
        # browser-script's swap is unreliable (some figures end up as
        # `data:,` empty placeholders); `_plos_inline_figures` post-
        # capture re-fetches them server-side via urllib as a backup.
        "wait_delay": 30000,
        "post_capture": lambda path, port: _plos_inline_figures(path, port),
    },
    "clinicalkey.com": {
        "url": _elsevier_pii_to_sciencedirect,
    },
    "linkinghub.elsevier.com": {
        "url": _elsevier_pii_to_sciencedirect,
    },
    # academic.oup.com (Silverchair): the article-metadata box (Keywords
    # + Topic + Issue Section) is populated by a delayed XHR. Observed
    # landing time varies 8–18 s depending on cache/load; 30 000 ms
    # gives reliable coverage. Scoped to OUP only — other publishers
    # pay the 5 s default.
    "academic.oup.com": {
        "wait_delay": 30000,
        # OUP figures arrive with `<img class=content-image src=data:,
        # data-path-from-xml=…>` and no `data-src`. The matching signed
        # `oup.silverchair-cdn.com/.../<stem>.jpeg` URL appears in
        # `<meta property=og:image>` and other metadata blocks; the hook
        # joins them by filename stem.
        "post_capture": lambda path, port: _oup_inline_figures(path, port),
    },
    # iucr landing pages link to per-figure sub-pages instead of
    # inlining full-resolution figures. SingleFile only saves the
    # landing page (100 px thumbnails). `post_capture` enriches the
    # saved HTML by visiting each sub-page and inlining its main
    # image as a data URL in the thumbnail's <img src>.
    "journals.iucr.org": {
        "post_capture": lambda path, port: _iucr_inline_figures(path, port),
        # _iucr_inline_figures opens a same-origin CDP tab to fetch each
        # figure sub-page; needs a live browser at `port`.
        "post_capture_needs_browser": True,
    },
    # aging-us.com is a Next.js SPA that ships every figure as
    # `<a data-figure-id=fN href=.../figure/fN/large/><img src=data:,>`.
    # Bytes are lazy-loaded after hydration; SingleFile typically captures
    # before hydration completes. The full-resolution PNG lives at a
    # predictable CDN URL (`cdn.aging-us.com/article/<id>/figure/fN/large.png`)
    # derived from the parent `<a>`'s href. `_aging_us_inline_figures`
    # post-capture re-fetches each one server-side via urllib.
    "aging-us.com": {
        "post_capture": lambda path, port: _aging_us_inline_figures(path, port),
    },
    # imrpress (FBL / FBE / FBS) figure <img> tags ship with
    # src=data:, placeholders; Vue/JS populates them on render and
    # SingleFile captures the empty state. `post_capture` extracts
    # the figN.jpg URLs from elsewhere in the HTML and inlines them
    # as data URLs.
    "imrpress.com": {
        "post_capture": lambda path, port: _imrpress_inline_figures(path, port),
    },
    # JCI inlines a 100-px GIF thumbnail per figure; the medium JPEG
    # is at a predictable CloudFront URL derived from the article id +
    # figure number. The browser-script (`_JCI_FIGURES_FIX_JS`)
    # rewrites <img src> at page load so SingleFile fetches and
    # inlines the medium image during its normal capture pass — no
    # post-capture pass, no separate urllib calls. Bump wait_delay
    # so SingleFile gives CloudFront enough time to deliver all
    # 5–10 figures before capture starts.
    "jci.org": {
        "wait_delay": 12000,
        # Some captures still leave `<img class=figure_thumbnail src=data:,>`
        # placeholders even with the browser-script JS swap. Post-capture
        # derives the medium-resolution CloudFront URL from the parent
        # `<a href=…/articles/view/<ID>/figure/<N>>` and refetches via
        # urllib. Substring `jci.org` matches both `www.jci.org` and
        # `insight.jci.org`.
        "post_capture": lambda path, port: _jci_inline_figures(path, port),
    },
    # annualreviews ships base64 thumbnails (~500 px) inline; full-res
    # ~1500-2300 px GIF lives at the parent `<a class=media-link href=
    # ...gif>` URL. SingleFile won't refetch when we swap <img src> in
    # the browser — use post_capture urllib fetch instead.
    "annualreviews.org": {
        "wait_delay": 15000,
        "post_capture": lambda path, port: _annualreviews_inline_figures(path, port),
    },
    # aacrjournals.org (Silverchair): figure <img class=content-image>
    # ships with src=data:image/svg+xml,<placeholder> and the medium
    # JPEG URL on data-src (signed CloudFront URL,
    # `aacr.silverchair-cdn.com/.../m_<id>fig<N>.jpeg`). The universal
    # _LAZYLOAD_FIX_JS in _write_inline_browser_script swaps src ←
    # data-src; SingleFile then fetches and inlines the medium image.
    # Bump wait_delay so the swapped images have time to fetch (~5–12
    # figures per article).
    "aacrjournals.org": {
        "wait_delay": 20000,
        # aacrjournals.org figures arrive with `<img class=content-image
        # src=data:, data-src=https://aacr.silverchair-cdn.com/.../m_<id>
        # .png?Expires=…&Signature=…>`. Universal lazyload swap is
        # unreliable here; reuse the generic Silverchair urllib hook to
        # refetch the signed CloudFront URL.
        "post_capture": lambda path, port: _silverchair_inline_figures(
            path, port,
            label="aacrjournals",
            referer="https://aacrjournals.org/",
        ),
    },
    # journals.biologists.com (Silverchair, hosts JCS / Development /
    # Disease Models & Mechanisms): figure <img class=content-image>
    # ships with `src=data:,` placeholder + `data-src=https://cob.
    # silverchair-cdn.com/cob/.../m_<id>.png?Expires=…&Signature=…`.
    # Generic Silverchair hook handles this verbatim — same CloudFront
    # signature pattern, same data-src field.
    "journals.biologists.com": {
        "wait_delay": 20000,
        "post_capture": lambda path, port: _silverchair_inline_figures(
            path, port,
            label="biologists",
            referer="https://journals.biologists.com/",
        ),
    },
    # rupress.org (JEM / JGP, runs on the Silverchair platform but uses
    # its own CloudFront-signed CDN at `cdn.rupress.org` rather than
    # silverchair-cdn.com). Figure <img class="content-image lazyLoadInit">
    # ships with `src=data:,` + `data-src=https://cdn.rupress.org/rup/
    # .../m_<id>.png?Expires=…&Signature=…`. The generic Silverchair
    # hook accepts both hosts (regex extended above).
    "rupress.org": {
        "post_capture": lambda path, port: _silverchair_inline_figures(
            path, port,
            label="rupress",
            referer="https://rupress.org/",
        ),
    },
    # link.springer.com (Springer Nature): figures arrive in two shapes
    # depending on layout era — `<img data-src=URL src=data:,>` (where
    # the lazysizes-populated CDN URL is on data-src) and
    # `<img aria-describedby="figure-N-desc" src=data:,>` (where the
    # canonical URL lives in the JSON-LD `image` array). The hook
    # handles both: data-src first, then JSON-LD by figure index.
    # Pure server-side urllib — `media.springernature.com` is a public
    # CDN, no auth needed.
    "link.springer.com": {
        "post_capture": lambda path, port: _springer_inline_figures(path, port),
    },
    # ashpublications.org (Silverchair, same platform as aacrjournals):
    # figure <img class=content-image> ships with src=data:image/svg+xml,
    # <placeholder> and the medium JPEG URL on data-src (signed URL,
    # `ash.silverchair-cdn.com/.../m_<id>.jpeg?Expires=...&Signature=...`).
    # The universal `_LAZYLOAD_FIX_JS` swaps src ← data-src in the live DOM,
    # but neither the swap nor SingleFile's deferred-image fetch causes the
    # bytes to land in the saved HTML — captures uniformly keep the SVG
    # placeholder. `_silverchair_inline_figures` post-capture re-fetches
    # each signed URL via urllib server-side (CloudFront signature is self-
    # contained, no cookies required) and inlines as a data URL.
    "ashpublications.org": {
        "post_capture": lambda path, port: _silverchair_inline_figures(
            path, port,
            label="ashpublications",
            referer="https://ashpublications.org/",
        ),
    },
    # portlandpress.com (Silverchair, same platform as ashpublications):
    # figure <img class=content-image> ships with src=data:, or src='data:
    # image/svg+xml,...' placeholder and the medium image URL on data-src
    # (signed URL, `port.silverchair-cdn.com/.../m_<id>.png?Expires=...
    # &Signature=...`). Same fix as ashpublications: re-fetch each signed
    # URL via urllib server-side and inline as a data URL.
    "portlandpress.com": {
        "post_capture": lambda path, port: _silverchair_inline_figures(
            path, port,
            label="portlandpress",
            referer="https://portlandpress.com/",
        ),
    },
    # royalsocietypublishing.org (Silverchair, same platform as
    # ashpublications / portlandpress): figure <img class=content-image>
    # ships with src=data:, placeholder and the medium image URL on data-src
    # (signed URL, `trs.silverchair-cdn.com/.../m_<id>.png?Expires=...
    # &Signature=...&Key-Pair-Id=...`). Same fix as ashpublications: re-
    # fetch each signed URL via urllib server-side and inline as a data
    # URL. Covers all Royal Society journal subdomains (rsfs, rsta, rstb,
    # rspa, rspb, rsif, rsbl, rsos, rsnr, ...) since they all live under
    # the single royalsocietypublishing.org host.
    "royalsocietypublishing.org": {
        "post_capture": lambda path, port: _silverchair_inline_figures(
            path, port,
            label="royalsocietypublishing",
            referer="https://royalsocietypublishing.org/",
        ),
    },
    # Dovepress (dovepress.com) ships a thumbnail (~5-8 KB) inside
    # `<table class=thumbnail-table>` with the high-res JPEG URL on the
    # parent `<a class=float_border href=...>`. The browser-script
    # `_DOVEPRESS_FIGURES_FIX_JS` swaps <img src> ← <a href> at page
    # load so SingleFile fetches and inlines the high-res image.
    "dovepress.com": {
        "wait_delay": 15000,
    },
    # BioOne (bioone.org): each figure ships with an `<img>` inlined at
    # ~30 KB thumbnail resolution; the high-res JPEG URL lives on the
    # parent `<a target=_blank href=...graphic/...jpg>`. The browser-
    # script `_BIOONE_FIGURES_FIX_JS` swaps <img src> ← <a href> at page
    # load so SingleFile fetches and inlines the full-res image.
    "bioone.org": {
        "wait_delay": 15000,
    },
    # JoVE (jove.com): each figure ships with `<img class=xfigimg>` at
    # medium resolution (~150 KB) and a sibling <a> link to the full-
    # resolution JPEG on jove.com/files/ftp_upload/<id>/<id>figNlarge.jpg.
    # The browser-script `_JOVE_FIGURES_FIX_JS` swaps the img src to
    # the larger URL so SingleFile inlines the full-res image. Also bump
    # delay to give SingleFile time to fetch the larger images.
    "jove.com": {
        "wait_delay": 15000,
    },
    # RSC (pubs.rsc.org): figures ship a small placeholder/thumbnail on
    # `<img>` and the high-res GIF URL on the parent
    # `<a href=https://pubs.rsc.org/image/article/.../<id>-f<N>_hi-res.gif>`.
    # The browser-script `_RSC_FIGURES_FIX_JS` swaps <img src> ← <a href>
    # so SingleFile inlines the high-res image. RSC also lazy-loads via
    # `data-original` (handled by `_LAZYLOAD_FIX_JS` separately, but the
    # _hi-res swap takes precedence).
    "rsc.org": {
        "wait_delay": 15000,
    },
    # MDPI (mdpi.com): each figure ships with `data-large` URL on
    # `<img>` for full-resolution; the inline `src` is a small data-URL
    # thumbnail and only the visible-viewport images get upgraded by
    # the publisher's lazyload. The browser-script `_MDPI_FIGURES_FIX_JS`
    # swaps src ← data-large for every figure so SingleFile inlines the
    # full-res variant. Bump wait_delay so SingleFile finishes fetching
    # the larger images (typical 100 KB-3 MB each, 5-15 figures per
    # paper — comparable to plos).
    "mdpi.com": {
        "wait_delay": 25000,
    },
    # eLife (elifesciences.org): figures lazy-load via empty `src`; the
    # high-res IIIF JPEG (1500-px max edge) is on the parent
    # `<a class=captioned-asset__link>`. The browser-script
    # `_ELIFESCIENCES_FIGURES_FIX_JS` swaps <img src> ← <a href> so
    # SingleFile inlines the IIIF image.
    "elifesciences.org": {
        "wait_delay": 15000,
    },
    # ACS (pubs.acs.org): figures render via SingleFile's CSS-var
    # background-image trick (`background-image: var(--sf-img-N)`); the
    # high-res JPEG URL is on the `.download-hi-res-img > a` link
    # inside `.figure-bottom-links`. The browser-script
    # `_ACS_FIGURES_FIX_JS` swaps <img.inline-fig src> ← that <a href>
    # at load so SingleFile fetches and inlines the full-resolution
    # image as the foreground (`background-image:none` ensures the
    # inlined src becomes the visible foreground). On older fixtures
    # (e.g. Lin_2001) SingleFile's in-page fetch occasionally fails and
    # leaves `src=data:,`. `_acs_inline_figures` post-capture refetches
    # any leftover `<img class=inline-fig src=data:,>` via a same-origin
    # CDP tab (Cloudflare bot protection 403s urllib).
    "pubs.acs.org": {
        "wait_delay": 15000,
        "post_capture": lambda path, port: _acs_inline_figures(path, port),
        # _acs_inline_figures opens a same-origin CDP tab (Cloudflare 403s
        # urllib); needs a live browser at `port`.
        "post_capture_needs_browser": True,
    },
    # Wiley (onlinelibrary.wiley.com): figures lazy-load via
    # `data-lg-src`; full URL on parent `<a>`. The browser-script
    # `_WILEY_FIGURES_FIX_JS` swaps <img src> ← parent <a href> for
    # every `figure.figure > a[href*=/cms/asset/]` so SingleFile
    # captures the full-resolution image. Bump wait_delay so SingleFile
    # has time to fetch the larger images.
    "onlinelibrary.wiley.com": {
        "wait_delay": 20000,
    },
    # Mol Biol Cell (molbiolcell.org, Atypon — same platform as Wiley):
    # figures arrive with the publisher's 500-px base64 thumbnail in
    # `<img class=figure__image src="data:image/jpeg;base64,...">` and the
    # full-resolution JPEG/PNG URL on the same tag's
    # `data-lg-src=/cms/asset/<uuid>/<asset-id>.<ext>` (relative). Rendered
    # at the 658-px column width the thumbnail visibly upscales / blurs.
    # `_molbiolcell_inline_figures` post-capture refetches each high-res
    # asset via urllib server-side (Cloudflare-fronted but no challenge —
    # public, no cookies needed) and replaces the thumbnail src with the
    # full-res data URL.
    "molbiolcell.org": {
        "post_capture": lambda path, port: _molbiolcell_inline_figures(path, port),
    },
    # Nature (nature.com): figure images lazy-load via empty srcset; the
    # JSON-LD `image` array holds the lw1200 URLs in order. The browser-
    # script `_NATURE_FIGURES_FIX_JS` swaps each <img> to the matching
    # JSON-LD URL so SingleFile inlines the full-resolution image.
    "nature.com": {
        "wait_delay": 20000,
        "post_capture": lambda path, port: _nature_inline_figures(path, port),
    },
    # BMJ (heart.bmj.com, emj.bmj.com, rapm.bmj.com, etc.) runs on the
    # HighWire platform. Each figure ships with `<img data-src=...F<N>.medium.gif
    # width=263-440>` (low-res, upscaled to 716-px column width). The
    # full-resolution `F<N>.large.jpg` URL is directly available on the
    # parent `<a class=colorbox-load href=...>` and on the sibling
    # `<a class=highwire-figure-link-newtab>` — no sub-page indirection.
    # `_bmj_inline_figures` post-capture derives the .large.jpg URL by
    # simple substitution of the data-src medium URL and re-fetches via
    # urllib server-side. Single substring `bmj.com` matches every
    # journal subdomain.
    "bmj.com": {
        "post_capture": lambda path, port: _bmj_inline_figures(path, port),
        # _bmj_inline_figures opens a same-origin CDP tab; needs a live
        # browser at `port`.
        "post_capture_needs_browser": True,
    },
    # ScienceDirect (sciencedirect.com): each figure has download links
    # with the high-res JPEG URL on `<a class=download-link href=...
    # _lrg.jpg>`. The browser-script `_SCIENCEDIRECT_FIGURES_FIX_JS`
    # swaps <img src> ← that <a href> so SingleFile inlines the full-
    # resolution image. Bump wait_delay so SingleFile has time to fetch
    # 5-15 figures per paper at 200KB-2MB each.
    "sciencedirect.com": {
        "wait_delay": 25000,
    },
    # PNAS (pnas.org, Atypon platform): figures arrive from SingleFile as
    # `<img src='data:image/svg+xml,<transparent placeholder>'
    #   style="...background-image:var(--sf-img-<N>)...">`. The full-
    # resolution JPEG bytes are embedded in `:root{--sf-img-<N>:
    # url("data:image/jpeg;base64,...")}` CSS variables in the page's inline
    # `<style>` block (SingleFile's CSS-var inlining mode), so browsers
    # render the figure correctly via background-image. There is no DOM
    # `<a href>` / `data-src` indirection and no derivable CDN URL —
    # the CSS-var bytes are the only available source. The shared
    # `_singlefile_inline_css_var_figures` post-capture promotes each
    # CSS-var data URL to its `<img src>` so downstream tooling that
    # reads `src` (rather than the CSS background) sees the real image.
    # Pure local rewrite — no network calls.
    "pnas.org": {
        "post_capture": lambda path, port: _singlefile_inline_css_var_figures(
            path, port, label="pnas", fig_container_re=_PNAS_FIG_RE,
        ),
    },
    # Sage (journals.sagepub.com, Atypon platform): unlike sister Atypon
    # journals (wiley, molbiolcell, pnas), Sage figure <img>s are captured
    # by SingleFile directly with the high-resolution JPEG inlined as a
    # `data:image/jpeg;base64,...` `src=` (no `data-lg-src` indirection,
    # no `--sf-img-N` background-image trick). Article figures live in
    # `<figure id=fig<N>-<doi-tail> class=graphic>`; biography photos in
    # `<span class=inline-graphic>` (typically `data:image/webp;base64,...`,
    # also valid bytes). Verified across 3 fixtures (10597123261435796,
    # 20584601261443992, 24723444261432710): 11 figure-imgs + 1 inline-
    # graphic, all decode cleanly via Pillow at full publisher resolution
    # (1500-2700 px wide, 200-800 KB each). No post_capture hook needed,
    # default wait_delay sufficient. The Phase 2 audit's `naturalWidth=0`
    # finding for the 10597 biography WEBP was a measurement artifact
    # (likely missing WEBP support in the audit harness) — the embedded
    # bytes are a structurally valid RIFF/WEBP/VP8L 373x440 image.
    #
    # tandfonline.com (Atypon platform): NO-OP — investigated, no higher-
    # resolution variant exists for the publisher. Figures arrive with the
    # 500-px-long-axis thumbnail inlined as `<img id=d1eN
    # src="data:image/{gif,jpeg};base64,...">`. The popup that the
    # `<button class=show-full-size>` triggers loads the same JS sub-page
    # at `/doi/figure/<DOI>`, whose `<img src=/cms/asset/<uuid>/<journal>_a
    # _<id>_f000N{,_oc}.{gif,jpg}>` resolves to byte-identical bytes (e.g.
    # Kabir 2010 F1: 42208 B embedded == 42208 B from /cms/asset/, both
    # 500x488 GIF). The Atypon CDN keys on the UUID and ignores filename
    # variants (`_oc` vs `_p` vs `_orig` vs `_oc_hires` all return the
    # same blob). No `data-lg-src` / `data-large-src` / `download-hi-res`
    # indirection in the page DOM, no `/doi/img/` endpoint (404), no
    # `/action/showImage` endpoint (500). Verified across all 3 fixtures
    # (Kabir 2010, Timashev 2020, John 1999): 14 figures, all already
    # inlined at the publisher's maximum resolution. Audit's "Display full
    # size" popup hypothesis was incorrect — the popup just zooms the same
    # 500-px image. Mild upscaling (1.0–1.4x to 716-px column width) is
    # the only path; acceptable per audit ("not blocking"). No rule entry
    # needed (no URL rewrite, no wait_delay tweak, no post_capture).
}


_DEFAULT_WAIT_DELAY = 5000


def apply_publisher_rule(url, default_wait=None):
    """Look up the publisher rule for url and return
    (new_url, wait, wait_delay).

    Matches the first rule whose key is a substring of url. When no rule
    matches, returns (url, default_wait, _DEFAULT_WAIT_DELAY).
    `wait` is None when the matched rule does not override the wait
    strategy — caller decides the default. `wait_delay` is the
    SingleFile --browser-wait-delay in ms.
    """
    if not url:
        return url, default_wait, _DEFAULT_WAIT_DELAY
    for key, rule in _PUBLISHER_RULES.items():
        if key in url:
            new_url = rule["url"](url) if rule.get("url") else url
            return (
                new_url,
                rule.get("wait", default_wait),
                rule.get("wait_delay", _DEFAULT_WAIT_DELAY),
            )
    return url, default_wait, _DEFAULT_WAIT_DELAY


def start_browser():
    """Launch Edge with CDP (non-headless to bypass Cloudflare).

    Returns (process, port, profile_dir).
    """
    profile_dir = tempfile.mkdtemp(prefix="edge_fetch_")
    args = [
        EDGE_PATH,
        "--disable-gpu",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--remote-allow-origins=*",
        "--start-maximized",
        f"--user-data-dir={profile_dir}",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        try:
            urllib.request.urlopen(
                f"http://localhost:{CDP_PORT}/json/version", timeout=2
            )
            return proc, CDP_PORT, profile_dir
        except Exception:
            continue
    proc.terminate()
    shutil.rmtree(profile_dir, ignore_errors=True)
    raise RuntimeError("Failed to start Edge browser")


def stop_browser(proc, profile_dir):
    """Terminate Edge and clean up profile directory."""
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if profile_dir and os.path.exists(profile_dir):
        shutil.rmtree(profile_dir, ignore_errors=True)


def _cdp_open_tab(url, port):
    """Open a new tab via CDP and return the target info."""
    req = urllib.request.Request(
        f"http://localhost:{port}/json/new?{url}", method="PUT"
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def _cdp_close_tab(tab_id, port):
    """Close a tab via CDP."""
    try:
        req = urllib.request.Request(
            f"http://localhost:{port}/json/close/{tab_id}", method="PUT"
        )
        urllib.request.urlopen(req)
    except Exception:
        pass



def _fetch_one(stem, doi_url, output_path, port):
    """Fetch a single page via single-file.

    Opens a pre-load tab to handle redirects, Cloudflare, and JS challenges.
    Waits for it to fully load, resolves the final URL, closes the pre-load
    tab, then runs single-file with the resolved URL.
    Returns "ok" or "error".
    """
    try:
        # Pre-load: open tab, let it fully load (resolves redirects, sets cookies)
        target = _cdp_open_tab(doi_url, port)
        tab_id = target["id"]
        time.sleep(PAGE_LOAD_WAIT)

        # Get resolved URL
        resolved_url = doi_url
        try:
            tabs = json.loads(
                urllib.request.urlopen(f"http://localhost:{port}/json/list").read()
            )
            for t in tabs:
                if t["id"] == tab_id:
                    resolved_url = t.get("url", doi_url)
                    break
        except Exception:
            pass

        # Close pre-load tab (session/cookies persist)
        _cdp_close_tab(tab_id, port)
        time.sleep(1)

        # Apply publisher rule sheet to resolved URL (e.g. cshlp .long,
        # biorxiv .full, ClinicalKey/linkinghub -> ScienceDirect).
        resolved_url, wait, wait_delay = apply_publisher_rule(
            resolved_url, default_wait="load",
        )

        # Capture with single-file using resolved URL.
        # --load-deferred-images-max-idle-time gives lazyload swaps 8 s
        # (up from the 1.5 s default) to settle before capture starts.
        # NB: --load-deferred-images-dispatch-scroll-event is intentionally
        # omitted — it triggers Silverchair's sticky-sidebar JS to write a
        # giant inline `min-height` onto `data-pb-dropzone=contents0/2`,
        # producing ~20-50k px of trailing whitespace on tandfonline / oup
        # / aacrjournals etc. The lazyload swap is handled instead by
        # _LAZYLOAD_FIX_JS in the --browser-script (see _fetch_batch).
        # Build a publisher-aware --browser-script (lazyload fix is
        # universal, JCI-specific figure rewrite added when the URL
        # is on jci.org).
        script_dir = tempfile.mkdtemp(prefix="single_file_script_")
        try:
            js_path = _write_inline_browser_script(
                {}, script_dir, url=resolved_url,
            )
        except Exception:
            js_path = None
        sf_args = [
            "single-file",
            "--browser-server",
            f"http://localhost:{port}",
            f"--browser-wait-until={wait}",
            f"--browser-wait-delay={wait_delay}",
            "--browser-load-max-time=120000",
            "--browser-capture-max-time=120000",
            "--remove-hidden-elements=false",
            "--block-scripts=false",
            "--removed-elements-selector=script[src]",
            "--load-deferred-images=true",
            "--load-deferred-images-max-idle-time=8000",
        ]
        if js_path:
            sf_args.append(f"--browser-script={js_path}")
        sf_args += [resolved_url, output_path]
        try:
            result = subprocess.run(
                sf_args, capture_output=True, text=True, timeout=300,
            )
        finally:
            shutil.rmtree(script_dir, ignore_errors=True)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            post = _get_post_capture(resolved_url)
            if post:
                try:
                    post(output_path, port)
                except Exception as e:
                    print(f"  post-capture error: {e!r}", flush=True)
            return "ok"
        return "error"
    except Exception:
        return "error"



def _domain_from_url(url):
    """Extract domain (netloc) from a URL."""
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _absolutize_css_urls(css_text, base_url):
    """Rewrite url(...) references in CSS to be absolute against base_url.

    Skips data:, absolute http(s):, protocol-relative //, and fragment-only
    (#...) references. Used when inlining a cross-origin stylesheet into
    the document — the stylesheet's relative paths must resolve against
    the original CSS URL, not the document URL, to keep @font-face and
    background-image references working.
    """
    def _abs(m):
        raw = m.group(1).strip()
        quote = ""
        if raw and raw[0] in ("'", '"'):
            quote = raw[0]
            raw = raw.strip(quote).strip()
        if not raw:
            return m.group(0)
        if (raw.startswith("data:") or raw.startswith("http://")
                or raw.startswith("https://") or raw.startswith("//")
                or raw.startswith("#")):
            return m.group(0)
        absolute = urllib.parse.urljoin(base_url, raw)
        return f"url({quote}{absolute}{quote})"
    return re.sub(r"url\(([^)]+)\)", _abs, css_text)


def _collect_cross_origin_css(ws_url, page_url):
    """Read cross-origin <link rel=stylesheet> hrefs from the tab at ws_url,
    download each server-side, absolutize url() references, and return a
    dict of {href: css_text} for use with --browser-script inlining.

    Returns {} on any error or if there are no cross-origin stylesheets.
    """
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
    except Exception:
        return {}
    try:
        js = (
            "(function(){"
            "var page=new URL(location.href);var out=[];"
            "document.querySelectorAll('link[rel=\"stylesheet\"][href]').forEach("
            "function(l){try{var u=new URL(l.href);"
            "if(u.origin!==page.origin)out.push(l.href);}catch(e){}});"
            "return JSON.stringify(out);})();"
        )
        ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": js, "returnByValue": True},
        }))
        msg = json.loads(ws.recv())
        raw = msg.get("result", {}).get("result", {}).get("value", "[]")
        urls = json.loads(raw)
    except Exception:
        urls = []
    finally:
        try:
            ws.close()
        except Exception:
            pass

    css_map = {}
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/css,*/*;q=0.1",
            })
            with polite_urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    continue
                data = resp.read()
        except Exception:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        css_map[url] = _absolutize_css_urls(text, url)
    return css_map


# Force-load lazysizes-style deferred images before SingleFile captures.
# biorxiv / cshlp (HighWire) and other sites render figures as
# `<img class=lazyload src=data:image/gif... data-src=<real url>>` and rely
# on lazysizes' IntersectionObserver to swap `data-src` → `src` when the
# element enters the viewport. SingleFile's --dispatch-scroll-event trick
# does not reliably trigger lazysizes for all figures (tested on biorxiv:
# every `<img>` stays on the 1x1 gif placeholder). Running at the `load`
# event — after lazysizes has initialised — force-copies `data-src` to
# `src` and clears the `lazyload` class so lazysizes doesn't undo the
# change. SingleFile's image-embedding step then inlines the real images.
# JCI thumbnails (`<a href=.../figure/N><img class=figure_thumbnail
# src=data:image/gif;base64,...></a>`) ship as 100-px GIFs only. The
# medium JPEG (~700 px) lives at a predictable CloudFront URL derived
# from the article ID and figure number:
#   /articles/view/<id>/figure/<N>
#   → //dm5migu4zj3pb.cloudfront.net/manuscripts/<bucket>/<id>/medium/JCI<id>.f<N>.jpg
# where <bucket> = floor(<id>/1000)*1000.
# Rewrite the thumbnail's <img src> to the medium URL before SingleFile
# captures. SingleFile then fetches and inlines the medium image as a
# data URL during its normal capture pass — no separate post_capture
# pass, no separate urllib calls, no jci.org rate-limit hits (the
# medium asset lives on CloudFront, a different origin from jci.org).
_JCI_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('a[href*=\"/figure/\"] img.figure_thumbnail').forEach(function(img){\n"
    "      var a = img.closest('a'); if (!a || !a.href) return;\n"
    "      var m = a.href.match(/\\/articles\\/view\\/(\\d+)\\/figure\\/(\\d+)/);\n"
    "      if (!m) return;\n"
    "      var id = m[1], n = m[2];\n"
    "      var bucket = Math.floor(parseInt(id, 10) / 1000) * 1000;\n"
    "      var url = 'https://dm5migu4zj3pb.cloudfront.net/manuscripts/' +\n"
    "                bucket + '/' + id + '/medium/JCI' + id + '.f' + n + '.jpg';\n"
    "      img.setAttribute('src', url);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


_LAZYLOAD_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('img[data-src]').forEach(function(img){\n"
    "      var s = img.getAttribute('src') || '';\n"
    "      if (s && !s.startsWith('data:')) return;\n"
    "      var real = img.getAttribute('data-src');\n"
    "      if (!real) return;\n"
    "      img.setAttribute('src', real);\n"
    "      img.classList.remove('lazyload','lazyloading');\n"
    "      img.classList.add('lazyloaded');\n"
    "    });\n"
    "    document.querySelectorAll('img[data-srcset]').forEach(function(img){\n"
    "      if (img.getAttribute('srcset')) return;\n"
    "      var real = img.getAttribute('data-srcset');\n"
    "      if (real) img.setAttribute('srcset', real);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# biorxiv (and other HighWire sites that use the same `<div class=fig>
# <div class=highwire-figure><div class=fig-inline-img-wrapper><div
# class=fig-inline-img><a href=...F<N>.large.jpg?...><img
# class=highwire-fragment data-src=...F<N>.medium.gif></a>` structure)
# ship two image variants: the medium GIF (~440 px wide) wired into
# `data-src` for the lazyload mechanism, and the large JPG/PNG (~800-
# 1500 px wide) on the parent `<a href>` for the on-click colorbox
# carousel. Rewrite both `data-src` AND `src` to the large URL early
# (DOMContentLoaded), so `_LAZYLOAD_FIX_JS`'s later swap (which copies
# data-src → src on `load`) propagates the large URL too, and SingleFile
# only ever fetches the large image during its capture. Strip carousel
# query params (`?width=800&height=600&carousel=1`) — same image is
# served regardless of params.
_BIORXIV_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('img.highwire-fragment').forEach(function(img){\n"
    "      var a = img.closest('a'); if (!a || !a.href) return;\n"
    "      var url = a.href.split('?')[0];\n"
    "      if (!/\\/F\\d+\\.large\\.(jpg|jpeg|gif|png)$/i.test(url)) return;\n"
    "      img.setAttribute('data-src', url);\n"
    "      img.setAttribute('src', url);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'loading') {\n"
    "    document.addEventListener('DOMContentLoaded', fix);\n"
    "  } else { fix(); }\n"
    "})();\n"
)


# cshlp (Cold Spring Harbor — genesdev / genome / mcb / rna etc., all
# HighWire) uses the same `F<N>.{small,medium,large}.jpg|gif` URL family
# as biorxiv, but the article markup is different: live figures render as
# `<div class="fig pos-float"><div class=fig-inline><a class=fig-inline-link
# href=...F<N>.expansion.html><img src=...F<N>.small.gif></a>`. The parent
# <a> points to a sub-page (`F<N>.expansion.html`), not the large image
# directly. Transform the href: swap `.expansion.html` → `.large.jpg`,
# then set img.src to that URL so SingleFile inlines the large image
# (~200 KB, ~800-1500 px native) instead of the small thumbnail
# (146-200 px). DOMContentLoaded so SingleFile sees the large URL when
# building its image-inline list.
_CSHLP_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('div.fig a.fig-inline-link').forEach(function(a){\n"
    "      if (!a.href) return;\n"
    "      var url = a.href.replace(/\\.expansion\\.html(\\?[^\"\\']*)?$/, '.large.jpg');\n"
    "      if (url === a.href) return;\n"
    "      var img = a.querySelector('img');\n"
    "      if (!img) return;\n"
    "      img.setAttribute('src', url);\n"
    "    });\n"
    "  }\n"
    "  // cshlp's `<div class=fig><a class=fig-inline-link>` figure DOM is\n"
    "  // injected by JS after DOMContentLoaded — running fix at DCL finds\n"
    "  // 0 matches. Use `load` (after all sub-resources, including the JS\n"
    "  // that builds the figure scaffolding, are loaded).\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# PLOS journals (plosgenetics, plosbiology, plosone, plosmedicine, etc.)
# render figures with the URL pattern
#   /article/figure/image?size=<inline|medium|large|original>&id=<DOI>.g<N>
# At narrow viewports the live page populates `<img src>` with size=inline
# (320 px wide), so SingleFile inlines those thumbnails. Swap to `size=
# large` (~1500-2000 px native, ~150 KB) so the image fills the column
# at full resolution. The `<img>` tags are JS-populated, so the swap
# must run on `load` (after Vue/template rendering).
#
# Setting `img.setAttribute('src', new_url)` on the existing img doesn't
# trigger a fresh load (the inline thumbnail's request is already in
# flight or cached, and the browser doesn't re-fetch). Instead, replace
# the img element with a freshly-constructed one — the new element has
# no load history and fetches the size=large URL cleanly.
# ScienceDirect (Elsevier) wraps each figure in
#   <figure class=figure id=fig<N>>
#     <span>
#       <img src="data:..." aria-describedby=cap<N>>
#       <ol>
#         <li><a class=download-link
#            href=https://ars.els-cdn.com/content/image/<id>-gr<N>_lrg.jpg>
#            Download high-res image</a></li>
#         <li><a class=download-link
#            href=https://ars.els-cdn.com/content/image/<id>-gr<N>.jpg>
#            Download full-size</a></li>
#       </ol>
#     </span>
#     <span class=captions>...</span>
#   </figure>
# The high-res JPEG URL is on the first `<a class=download-link>`
# (the `_lrg.jpg` variant, sized for "high-res image (NMB)" in title).
# Swap <img src> ← that <a href> at load so SingleFile fetches the
# full-resolution image.
_SCIENCEDIRECT_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('figure.figure').forEach(function(fig){\n"
    "      var img = fig.querySelector('img');\n"
    "      if (!img) return;\n"
    "      var a = fig.querySelector('a.download-link[href*=\"_lrg.\"]');\n"
    "      if (!a) a = fig.querySelector('a.download-link[href]');\n"
    "      if (!a || !a.href) return;\n"
    "      img.setAttribute('src', a.href);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# Nature (nature.com / Springer Nature) lazy-loads figure images via
# empty `srcset`; the JSON-LD metadata in the page contains the full
# `lw1200`-resolution image URLs in order under `"image":[...]`. Pull
# those URLs and assign each to the matching
# `<picture class=c-article-section__figure-picture> > img>` in DOM
# order, so SingleFile inlines the high-res figure.
_NATURE_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    var pictures = document.querySelectorAll('picture.c-article-section__figure-picture > img');\n"
    "    if (!pictures.length) return;\n"
    "    var urls = [];\n"
    "    document.querySelectorAll('script[type=\"application/ld+json\"]').forEach(function(s){\n"
    "      try {\n"
    "        var data = JSON.parse(s.textContent);\n"
    "        var arr = data && data.image;\n"
    "        if (Array.isArray(arr)) urls = urls.concat(arr);\n"
    "      } catch(e){}\n"
    "    });\n"
    "    pictures.forEach(function(img, idx){\n"
    "      var u = urls[idx];\n"
    "      if (u) img.setAttribute('src', u);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# Wiley (onlinelibrary.wiley.com) wraps each figure in
#   <figure class=figure>
#     <a target=_blank href=https://onlinelibrary.wiley.com/cms/asset/<uuid>/<file>.<ext>>
#       <picture>
#         <img class=figure__image src="data:..." data-lg-src=/cms/asset/<uuid>/<file>.<ext>
#              loading=lazy>
#       </picture>
#     </a>
#     <figcaption>...</figcaption>
#   </figure>
# Images lazy-load via `data-lg-src`; SingleFile captures with partial
# load — many figures end up at placeholder src. The full URL is on
# the parent <a href>. Swap <img src> ← <a href> at load so SingleFile
# fetches the full image.
_WILEY_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('figure.figure > a[href]').forEach(function(a){\n"
    "      if (!a.href || !/\\/cms\\/asset\\//.test(a.href)) return;\n"
    "      var img = a.querySelector('img');\n"
    "      if (!img) return;\n"
    "      img.setAttribute('src', a.href);\n"
    "      img.removeAttribute('loading');\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# ACS (pubs.acs.org) renders each figure as
#   <figure id=f_<N> class=article__inlineFigure>
#     <button class=figure-viewer__trigger>
#       <img class="inline-fig internalNav" src='data:image/svg+xml,<svg.../>'
#            style="...background-image:var(--sf-img-N)...">
#     </button>
#     <div class=figure-bottom-links>
#       <div class=download-hi-res-img>
#         <a href=https://pubs.acs.org/cms/<doi>/asset/images/large/<file>.jpeg>
#            High Resolution Image</a>
#       </div>
#       ...
#     </div>
#   </figure>
# The high-res JPEG URL lives on the inner `<a>` of `.download-hi-res-img`
# inside `.figure-bottom-links`. Swap <img.inline-fig src> ← that
# <a href> at load so SingleFile fetches and inlines the full-resolution
# image as the foreground (replacing the transparent SVG placeholder).
_ACS_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('figure.article__inlineFigure').forEach(function(fig){\n"
    "      var img = fig.querySelector('img.inline-fig');\n"
    "      if (!img) return;\n"
    "      var a = fig.querySelector('.download-hi-res-img a[href]');\n"
    "      if (!a || !a.href) return;\n"
    "      img.setAttribute('src', a.href);\n"
    "      // Strip the background-image CSS var so the inlined src\n"
    "      // becomes the visible foreground.\n"
    "      img.style.backgroundImage = 'none';\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# eLife (elifesciences.org) wraps each figure in
#   <figure class=captioned-asset>
#     <a class=captioned-asset__link
#        href=https://iiif.elifesciences.org/lax/.../<id>-fig<N>-v2.tif/full/,1500/0/default.jpg>
#       <picture><img class=captioned-asset__image src="..." srcset sizes></picture>
#     </a>
#     <figcaption>...</figcaption>
#   </figure>
# Most captures arrive with empty `src` (lazy-loaded; SingleFile sees
# the empty placeholder). The high-res IIIF JPEG (1500-px max edge,
# typically 100-500 KB) is on the parent <a href>. Swap <img src> ← <a
# href> at load so SingleFile inlines the IIIF image.
_ELIFESCIENCES_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('a.captioned-asset__link').forEach(function(a){\n"
    "      if (!a.href) return;\n"
    "      var img = a.querySelector('img');\n"
    "      if (!img) return;\n"
    "      img.setAttribute('src', a.href);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# MDPI (mdpi.com) renders each figure inside `<div class=html-fig-wrap>`
# whose `<img>` carries multiple URL attributes:
#   data-large=<full-res URL>   (typically PNG/JPG, 100 KB-3 MB)
#   data-original=<full-res URL> (same)
#   data-lsrc=<medium 550-px JPG>
#   src="data:image/jpeg;base64,<small thumbnail>"
# The publisher's lazyload swaps src ← data-lsrc (medium) only when the
# image enters the viewport — many figures below the fold stay at the
# inlined thumbnail. Swap src ← data-large directly so SingleFile
# always inlines the highest-resolution variant.
_MDPI_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('.html-fig-wrap img[data-large]').forEach(function(img){\n"
    "      var url = img.getAttribute('data-large');\n"
    "      if (!url) return;\n"
    "      if (url.charAt(0) === '/') url = window.location.origin + url;\n"
    "      img.setAttribute('src', url);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# RSC (pubs.rsc.org) wraps each figure in
#   <figure class=img-tbl__image>
#     <a href=https://pubs.rsc.org/image/article/.../<id>-f<N>_hi-res.gif>
#       <img src="data:..." (thumbnail/placeholder)
#            data-original=/image/article/.../<id>-f<N>.gif (medium)>
#     </a>
#     <figcaption class=img-tbl__caption>Fig. N caption</figcaption>
# Swap <img src> ← parent <a href> so SingleFile fetches the hi-res
# GIF instead of the medium / placeholder. Run on `load` since the
# img is rendered server-side (no JS dependency).
_RSC_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('figure.img-tbl__image > a[href*=\"_hi-res\"]').forEach(function(a){\n"
    "      if (!a.href) return;\n"
    "      var img = a.querySelector('img');\n"
    "      if (!img) return;\n"
    "      img.setAttribute('src', a.href);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# JoVE (jove.com) ships figures as
#   <p class=jove_content>
#     <img class=xfigimg src="data:image/jpeg;base64,..." (medium ~150 KB)>
#     <strong class=xfig>Figure N</strong>
#     <strong>: Caption</strong>
#     <a href=https://www.jove.com/files/ftp_upload/<id>/<id>figNlarge.jpg>
#       Please click here to view a larger version of this figure.</a>
# The high-res JPEG URL is on the sibling <a> link inside the same
# <p class=jove_content>. Swap <img.xfigimg src> ← that <a href> at
# load so SingleFile fetches the large JPEG instead of the medium one.
_JOVE_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('p.jove_content img.xfigimg').forEach(function(img){\n"
    "      var p = img.closest('p.jove_content');\n"
    "      if (!p) return;\n"
    "      var a = p.querySelector('a[href$=\"large.jpg\"], a[href*=\"fig\"][href$=\".jpg\"]');\n"
    "      if (!a || !a.href) return;\n"
    "      img.setAttribute('src', a.href);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# BioOne (bioone.org) wraps each figure in
#   <div class="fig panel">
#     <h2 class=label>FIG. N.</h2>
#     <div class=caption>...</div>
#     <a target=_blank href=https://bioone.org/.../graphic/<id>-f<N>.jpg>
#       <img alt=<id>-f<N>.tif src="data:image/jpeg;base64,<thumb>">
# The parent <a> points to the full-resolution JPEG. Swap <img src> ←
# parent <a href> at page load so SingleFile fetches the high-res image
# during capture.
_BIOONE_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('.fig.panel a[href*=\"/graphic/\"]').forEach(function(a){\n"
    "      if (!a.href) return;\n"
    "      var img = a.querySelector('img');\n"
    "      if (!img) return;\n"
    "      img.setAttribute('src', a.href);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


# Dovepress (dovepress.com) wraps each figure in a `<table
# class=thumbnail-table>` whose left `<td>` contains a small thumbnail
# (~5-8 KB) inside `<a class=float_border href=<HIRES_JPG>>` — the
# parent <a> points to the full-resolution JPEG URL
# (`https://www.dovepress.com/article/fulltext_file/<id>/<key>/<JOURNAL>_A_<id>_O_F<N>g.jpg`).
# Swap <img src> to the href so SingleFile fetches the high-res image
# during capture. Run on `load` since the image markup is server-
# rendered (no JS dependency, but we want to be sure all `<a>` hrefs
# are resolved before the swap).
_DOVEPRESS_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('table.thumbnail-table a.float_border').forEach(function(a){\n"
    "      if (!a.href) return;\n"
    "      var img = a.querySelector('img');\n"
    "      if (!img) return;\n"
    "      img.setAttribute('src', a.href);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


_PLOS_FIGURES_FIX_JS = (
    "(function(){\n"
    "  function fix(){\n"
    "    document.querySelectorAll('.figure').forEach(function(fig){\n"
    "      var img = fig.querySelector('.img-box img');\n"
    "      if (!img) return;\n"
    "      var dl = fig.querySelector('a[href*=\"size=large\"]');\n"
    "      if (!dl) return;\n"
    "      var url = dl.href.replace(/&download=?(&|$)/, '&').replace(/[?]download=?(&|$)/, '?');\n"
    "      if (!/[?&]size=large/.test(url)) return;\n"
    "      var fresh = document.createElement('img');\n"
    "      // Copy only `alt` + `data-*` — `class=thumbnail` and\n"
    "      // `loading=lazy` cause SingleFile to skip the image\n"
    "      // (treated as deferred placeholder, leaves src=data:,).\n"
    "      var alt = img.getAttribute('alt');\n"
    "      if (alt) fresh.setAttribute('alt', alt);\n"
    "      for (var i = 0; i < img.attributes.length; i++) {\n"
    "        var att = img.attributes[i];\n"
    "        if (att.name.indexOf('data-') === 0) fresh.setAttribute(att.name, att.value);\n"
    "      }\n"
    "      fresh.setAttribute('src', url);\n"
    "      img.parentNode.replaceChild(fresh, img);\n"
    "    });\n"
    "  }\n"
    "  if (document.readyState === 'complete') { fix(); }\n"
    "  else { window.addEventListener('load', fix); }\n"
    "})();\n"
)


def _write_inline_browser_script(css_map, dest_dir, url=None):
    """Write a JS file for SingleFile's --browser-script.

    The script does:
      1. Replace cross-origin <link rel=stylesheet> with inline <style> at
         the same DOM position (preserves cascade order). Same-origin
         sheets are left alone — SingleFile reads their cssRules directly.
      2. Force-load lazysizes-style deferred images (see _LAZYLOAD_FIX_JS).
      3. (jci.org only) Rewrite figure-thumbnail <img src> from the 100-px
         placeholder GIF to the predictable medium-resolution CloudFront
         URL derived from the article id + figure number. SingleFile then
         fetches and inlines the medium image during its capture pass.
      4. (biorxiv.org only) Rewrite `img.highwire-fragment` figure src
         from the medium GIF (~440 px) pulled in by the lazyload fix to
         the large JPG/PNG (~800-1500 px) on the parent `<a href>`, so
         SingleFile inlines the large image instead.

    Always returns a file path (the lazyload fix is universally safe:
    it only touches `<img>` with both `data-src` and a placeholder
    `data:` src).
    """
    parts = []
    if css_map:
        pairs = ",\n".join(
            f"  [{json.dumps(url)}, {json.dumps(css)}]"
            for url, css in css_map.items()
        )
        parts.append(
            "(function(){\n"
            "  var CSS_MAP = new Map([\n" + pairs + "\n  ]);\n"
            "  function replace(){\n"
            "    document.querySelectorAll('link[rel=\"stylesheet\"][href]')"
            ".forEach(function(link){\n"
            "      var css = CSS_MAP.get(link.href);\n"
            "      if (!css) return;\n"
            "      var style = document.createElement('style');\n"
            "      style.textContent = css;\n"
            "      link.parentNode.replaceChild(style, link);\n"
            "    });\n"
            "  }\n"
            "  if (document.readyState === 'loading') {\n"
            "    document.addEventListener('DOMContentLoaded', replace);\n"
            "  } else {\n"
            "    replace();\n"
            "  }\n"
            "})();\n"
        )
    parts.append(_LAZYLOAD_FIX_JS)
    if url and "jci.org" in url:
        parts.append(_JCI_FIGURES_FIX_JS)
    if url and "biorxiv.org" in url:
        parts.append(_BIORXIV_FIGURES_FIX_JS)
    if url and "cshlp.org" in url:
        parts.append(_CSHLP_FIGURES_FIX_JS)
    if url and "plos.org" in url:
        parts.append(_PLOS_FIGURES_FIX_JS)
    if url and "dovepress.com" in url:
        parts.append(_DOVEPRESS_FIGURES_FIX_JS)
    if url and "bioone.org" in url:
        parts.append(_BIOONE_FIGURES_FIX_JS)
    if url and "jove.com" in url:
        parts.append(_JOVE_FIGURES_FIX_JS)
    if url and "rsc.org" in url:
        parts.append(_RSC_FIGURES_FIX_JS)
    if url and "mdpi.com" in url:
        parts.append(_MDPI_FIGURES_FIX_JS)
    if url and "elifesciences.org" in url:
        parts.append(_ELIFESCIENCES_FIGURES_FIX_JS)
    if url and "pubs.acs.org" in url:
        parts.append(_ACS_FIGURES_FIX_JS)
    if url and "onlinelibrary.wiley.com" in url:
        parts.append(_WILEY_FIGURES_FIX_JS)
    if url and "nature.com" in url:
        parts.append(_NATURE_FIGURES_FIX_JS)
    if url and "sciencedirect.com" in url:
        parts.append(_SCIENCEDIRECT_FIGURES_FIX_JS)
    js = "\n".join(parts)
    import hashlib
    key_bits = []
    if css_map:
        key_bits.extend(sorted(css_map))
    if url and "jci.org" in url:
        key_bits.append("jci")
    if url and "biorxiv.org" in url:
        key_bits.append("biorxiv")
    if url and "cshlp.org" in url:
        key_bits.append("cshlp")
    if url and "plos.org" in url:
        key_bits.append("plos")
    if url and "dovepress.com" in url:
        key_bits.append("dovepress")
    if url and "bioone.org" in url:
        key_bits.append("bioone")
    if url and "jove.com" in url:
        key_bits.append("jove")
    if url and "rsc.org" in url:
        key_bits.append("rsc")
    if url and "mdpi.com" in url:
        key_bits.append("mdpi")
    if url and "elifesciences.org" in url:
        key_bits.append("elifesciences")
    if url and "pubs.acs.org" in url:
        key_bits.append("acs")
    if url and "onlinelibrary.wiley.com" in url:
        key_bits.append("wiley")
    if url and "nature.com" in url:
        key_bits.append("nature")
    if url and "sciencedirect.com" in url:
        key_bits.append("sciencedirect")
    key = "|".join(key_bits) if key_bits else "lazyload-only"
    fname = hashlib.md5(key.encode()).hexdigest()[:16] + ".js"
    path = os.path.join(dest_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(js)
    return path


_CAPTCHA_BANNER_JS = """
(function() {
    var banner = document.createElement('div');
    banner.id = '__captcha_banner__';
    banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
        'padding:12px;background:#d32f2f;color:white;text-align:center;' +
        'font:bold 16px sans-serif;display:flex;align-items:center;justify-content:center;gap:16px;';
    var text = document.createElement('span');
    text.textContent = 'Solve captcha if needed, then press ENTER';
    var btn = document.createElement('button');
    btn.id = '__block_btn__';
    btn.textContent = 'Mark as blocked';
    btn.style.cssText = 'padding:4px 12px;background:white;color:#d32f2f;border:none;' +
        'border-radius:4px;font:bold 14px sans-serif;cursor:pointer;';
    banner.appendChild(text);
    banner.appendChild(btn);
    document.body.appendChild(banner);
})();
"""

_WAIT_FOR_ACTION_JS = """
new Promise(function(resolve) {
    document.addEventListener('keydown', function handler(e) {
        if (e.key === 'Enter') {
            var b = document.getElementById('__captcha_banner__');
            if (b) b.remove();
            document.removeEventListener('keydown', handler);
            resolve('done');
        }
    });
    var btn = document.getElementById('__block_btn__');
    if (btn) {
        btn.addEventListener('click', function() {
            var b = document.getElementById('__captcha_banner__');
            if (b) b.remove();
            resolve('blocked');
        });
    }
});
"""


def _cdp_inject_banner(ws_url):
    """Inject captcha banner and start listening for user action.

    Returns a websocket connection with a pending awaitPromise (id=2)
    that resolves with 'done' (Enter) or 'blocked' (button click).
    """
    ws = websocket.create_connection(ws_url, timeout=600)
    # Inject the banner
    ws.send(json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": _CAPTCHA_BANNER_JS},
    }))
    ws.recv()  # consume response for id=1

    # Start waiting for user action
    ws.send(json.dumps({
        "id": 2,
        "method": "Runtime.evaluate",
        "params": {
            "expression": _WAIT_FOR_ACTION_JS,
            "awaitPromise": True,
        },
    }))
    return ws


def _cdp_poll_action(ws):
    """Non-blocking check if the user has acted (Enter or block button).

    Returns 'done', 'blocked', 'reinject' (context destroyed, need to
    re-inject banner), or None (no action yet).
    """
    ws.settimeout(0)
    try:
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 2:
                # CDP error = execution context destroyed (page navigated)
                if "error" in msg:
                    return "reinject"
                value = msg.get("result", {}).get("result", {}).get("value")
                if value in ("done", "blocked"):
                    ws.close()
                    return value
                return None
    except (websocket.WebSocketTimeoutException, BlockingIOError):
        return None
    except Exception:
        return None




_CAPTCHA_MARKERS = [
    "cf-turnstile", "challenges.cloudflare.com", "challenge-platform",
    "Just a moment", "Validate User",
    "hcaptcha.com", "h-captcha",
]
_BLOCK_MARKERS = [
    "cf-ratelimit-blocked", "Access Denied",
    "403 Forbidden", "429 Too Many Requests",
]


def _cdp_check_page_status(ws_url):
    """Check page status after loading.

    Returns one of:
        "ok"      - DOI pattern found in page (article loaded)
        "captcha" - no DOI, captcha elements detected
        "blocked" - no DOI, block indicators detected (no captcha)
        "unknown" - no DOI, no captcha, no block indicators
    """
    js = """
    (function() {
        var html = document.documentElement ? document.documentElement.innerHTML : '';
        var pageTitle = document.title || '';
        var all = pageTitle + ' ' + html;
        if (/10\\.\\d{4,}\\//.test(html)) return 'ok';
        var captchaMarkers = """ + json.dumps(_CAPTCHA_MARKERS) + """;
        for (var i = 0; i < captchaMarkers.length; i++) {
            if (all.indexOf(captchaMarkers[i]) !== -1) return 'captcha';
        }
        var blockMarkers = """ + json.dumps(_BLOCK_MARKERS) + """;
        for (var i = 0; i < blockMarkers.length; i++) {
            if (all.indexOf(blockMarkers[i]) !== -1) return 'blocked';
        }
        return 'unknown';
    })();
    """
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": js},
        }))
        msg = json.loads(ws.recv())
        ws.close()
        return msg.get("result", {}).get("result", {}).get("value", "unknown")
    except Exception:
        return "unknown"


def _fetch_batch(batch, port):
    """Fetch one batch: preload all, resolve URLs, capture all.

    The preload step is needed because single-file does not follow DOI
    redirects (e.g. https://doi.org/... -> publisher URL). Opening each
    DOI in a browser tab resolves the redirect chain; the final URL is
    then passed to single-file for capture. Direct URLs (no redirect)
    can be fetched by single-file without preloading.

    Returns list of (stem, status) tuples.
    """
    import queue
    import threading

    # --- Preload: open all tabs ---
    preload_tab_ids = []
    for stem, doi_url, output_path in batch:
        try:
            target = _cdp_open_tab(doi_url, port)
            preload_tab_ids.append(target["id"])
        except Exception:
            preload_tab_ids.append(None)
        time.sleep(TAB_DELAY)

    time.sleep(PAGE_LOAD_WAIT)

    # --- Batch resolve URLs and WebSocket endpoints ---
    tab_info = {}  # tab_id -> {url, ws}
    try:
        tabs_list = json.loads(
            urllib.request.urlopen(
                f"http://localhost:{port}/json/list"
            ).read()
        )
        for t in tabs_list:
            tab_info[t["id"]] = {
                "url": t.get("url", ""),
                "ws": t.get("webSocketDebuggerUrl", ""),
            }
    except Exception:
        pass

    preload_tabs = []  # (tab_id, resolved_url) per batch item
    for (stem, doi_url, output_path), tab_id in zip(batch, preload_tab_ids):
        if tab_id is None:
            preload_tabs.append((None, doi_url))
        else:
            info = tab_info.get(tab_id, {})
            raw_url = info.get("url", doi_url)
            # Apply publisher rule sheet to rewrite the resolved URL before
            # capture (e.g. cshlp .long, biorxiv .full, ClinicalKey -> ScienceDirect).
            new_url, _, _ = apply_publisher_rule(raw_url)
            preload_tabs.append((tab_id, new_url))

    # Dir for per-stem browser-script files; each holds the cross-origin
    # CSS content for that paper so SingleFile's capture tab can inline
    # stylesheets at the original <link> position (preserves cascade order).
    script_temp_dir = tempfile.mkdtemp(prefix="sf_script_")
    script_by_stem = {}  # stem -> path to browser-script JS file

    # --- Step A: Per-tab page status check ---
    blocked_reasons = {}  # stem -> specific failure reason string
    non_ok_tabs = []  # [(stem, tab_id, ws_url, domain, status)]
    for (stem, doi_url, output_path), (tab_id, resolved_url) in zip(
        batch, preload_tabs
    ):
        if tab_id is None:
            continue
        domain = _domain_from_url(resolved_url)
        ws_url = tab_info.get(tab_id, {}).get("ws", "")
        if ws_url:
            status = _cdp_check_page_status(ws_url)
        else:
            status = "unknown"
        if status == "ok":
            css_map = _collect_cross_origin_css(ws_url, resolved_url)
            js_path = _write_inline_browser_script(
                css_map, script_temp_dir, url=resolved_url,
            )
            if js_path:
                script_by_stem[stem] = js_path
            _cdp_close_tab(tab_id, port)
        else:
            non_ok_tabs.append((stem, tab_id, ws_url, domain, status))

    # --- Step B: Group remaining tabs by (domain, status) ---
    groups = {}  # (domain, status) -> [(stem, tab_id, ws_url)]
    for stem, tab_id, ws_url, domain, status in non_ok_tabs:
        groups.setdefault((domain, status), []).append((stem, tab_id, ws_url))

    # --- Step C: Act on each group ---
    # Blocked groups: close tabs, skip capture
    for (domain, status), tabs in list(groups.items()):
        if status == "blocked":
            for stem, tab_id, _ in tabs:
                blocked_reasons[stem] = "preload: blocked by status check"
                _cdp_close_tab(tab_id, port)
            del groups[(domain, status)]

    # Captcha/unknown groups: banner + wait for user
    if groups:
        pending = {}  # (domain, status) -> (ws_conn, ws_url, tabs)
        for (domain, status), tabs in groups.items():
            listen_ws = None
            listen_ws_url = None
            for _, tab_id, ws_url in tabs:
                if not ws_url:
                    continue
                try:
                    if listen_ws is None:
                        listen_ws = _cdp_inject_banner(ws_url)
                        listen_ws_url = ws_url
                    else:
                        ws = websocket.create_connection(ws_url, timeout=10)
                        ws.send(json.dumps({
                            "id": 1,
                            "method": "Runtime.evaluate",
                            "params": {"expression": _CAPTCHA_BANNER_JS},
                        }))
                        ws.recv()
                        ws.close()
                except Exception:
                    continue
            if listen_ws:
                pending[(domain, status)] = (listen_ws, listen_ws_url, tabs)
            else:
                for _, tid, _ in tabs:
                    _cdp_close_tab(tid, port)

        while pending:
            # Refresh tab list for title re-checks
            live_tabs = {}  # tab_id -> ws_url
            try:
                tabs_list = json.loads(
                    urllib.request.urlopen(
                        f"http://localhost:{port}/json/list"
                    ).read()
                )
                for t in tabs_list:
                    ws = t.get("webSocketDebuggerUrl", "")
                    if ws:
                        live_tabs[t["id"]] = ws
            except Exception:
                pass

            for key in list(pending):
                ws_conn, listen_ws_url, tabs = pending[key]

                # Check if any tab in this group now has a DOI (article loaded)
                resolved = False
                for stem, tid, _ in tabs:
                    tab_ws = live_tabs.get(tid)
                    if not tab_ws:
                        continue
                    if _cdp_check_page_status(tab_ws) == "ok":
                        resolved = True
                        break
                if resolved:
                    try:
                        ws_conn.close()
                    except Exception:
                        pass
                    for s, tid, tws in tabs:
                        resolved_url = live_tabs.get(tid) or ""
                        if tws:
                            css_map = _collect_cross_origin_css(
                                tws, resolved_url
                            )
                            js_path = _write_inline_browser_script(
                                css_map, script_temp_dir, url=resolved_url,
                            )
                            if js_path:
                                script_by_stem[s] = js_path
                        _cdp_close_tab(tid, port)
                    del pending[key]
                    continue

                # Check for user action (Enter or Mark as blocked)
                action = _cdp_poll_action(ws_conn)
                if action == "done":
                    for s, tid, tws in tabs:
                        if tws:
                            url_for_tab = live_tabs.get(tid) or ""
                            css_map = _collect_cross_origin_css(tws, url_for_tab)
                            js_path = _write_inline_browser_script(
                                css_map, script_temp_dir, url=url_for_tab,
                            )
                            if js_path:
                                script_by_stem[s] = js_path
                        _cdp_close_tab(tid, port)
                    del pending[key]
                elif action == "blocked":
                    for stem, tid, _ in tabs:
                        blocked_reasons[stem] = "preload: user marked blocked"
                        _cdp_close_tab(tid, port)
                    del pending[key]
                elif action == "reinject":
                    # Page navigated; re-inject banner on fresh ws
                    try:
                        ws_conn.close()
                    except Exception:
                        pass
                    # Find a live ws_url from this group's tabs
                    fresh_ws = None
                    for _, tid, _ in tabs:
                        if tid in live_tabs:
                            fresh_ws = live_tabs[tid]
                            break
                    try:
                        new_ws = _cdp_inject_banner(
                            fresh_ws or listen_ws_url)
                        pending[key] = (new_ws, fresh_ws or listen_ws_url,
                                        tabs)
                    except Exception:
                        pass

            if pending:
                time.sleep(2)

    time.sleep(1)

    # --- Build capture list (exclude blocked stems) ---
    capture_items = []
    results = []
    results_lock = threading.Lock()

    for (stem, doi_url, output_path), (tab_id, resolved_url) in zip(
        batch, preload_tabs
    ):
        if tab_id is None:
            with results_lock:
                results.append((stem, "preload: tab open failed"))
            continue
        if stem in blocked_reasons:
            with results_lock:
                results.append((stem, blocked_reasons[stem]))
            continue
        capture_items.append(
            (stem, resolved_url, output_path, script_by_stem.get(stem))
        )

    # --- Capture ---
    task_queue = queue.Queue()

    def worker():
        while True:
            item = task_queue.get()
            if item is None:
                break
            stem, resolved_url, output_path, script_path = item
            try:
                _, wait, wait_delay = apply_publisher_rule(
                    resolved_url, default_wait="load",
                )
                args = [
                    "single-file",
                    "--browser-server",
                    f"http://localhost:{port}",
                    f"--browser-wait-until={wait}",
                    f"--browser-wait-delay={wait_delay}",
                    "--browser-load-max-time=120000",
                    "--browser-capture-max-time=120000",
                    "--remove-hidden-elements=false",
                    "--block-scripts=false",
                    "--removed-elements-selector=script[src]",
                    "--remove-unused-fonts=false",
                    "--remove-unused-styles=false",
                    "--remove-alternative-fonts=false",
                    "--remove-alternative-medias=false",
                    "--load-deferred-images=true",
                    "--load-deferred-images-max-idle-time=8000",
                ]
                if script_path:
                    args.append(f"--browser-script={script_path}")
                args.extend([resolved_url, output_path])
                subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if not os.path.exists(output_path):
                    status = "single-file: no output"
                elif os.path.getsize(output_path) <= 1000:
                    status = "single-file: output too small"
                else:
                    with open(output_path, encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    # If SingleFile's saved url is still doi.org, the
                    # redirect to the publisher page never resolved (the
                    # page captured is the doi.org placeholder/error page,
                    # not the article). Treat as a fetch failure so the
                    # next batch retries with a fresh tab.
                    sf_url_m = re.search(
                        r"Page saved with SingleFile\s+url:\s*(\S+)",
                        content[:2000],
                    )
                    sf_url = sf_url_m.group(1) if sf_url_m else ""
                    if "cf-ratelimit-blocked" in content:
                        os.remove(output_path)
                        status = "single-file: cf rate-limit page"
                    elif "Validate User" in content[:1000]:
                        os.remove(output_path)
                        status = "single-file: validate user page"
                    elif re.match(
                        r"https?://(?:dx\.)?doi\.org/", sf_url
                    ):
                        os.remove(output_path)
                        status = "single-file: doi.org redirect unresolved"
                    elif ("citation_title" not in content
                            and "Abstract" not in content
                            and "abstract" not in content):
                        os.remove(output_path)
                        status = "single-file: no article markers"
                    else:
                        # Run publisher post_capture (e.g. iucr/imrpress
                        # figure-image inlining) before reporting "ok".
                        # _fetch_one already does this; the batch worker
                        # was missing the call, leaving imrpress papers
                        # with empty figure src=data:, placeholders.
                        post = _get_post_capture(resolved_url)
                        if post:
                            try:
                                post(output_path, port)
                            except Exception as e:
                                print(
                                    f"  post-capture error: {e!r}",
                                    flush=True,
                                )
                        status = "ok"
            except subprocess.TimeoutExpired:
                status = "single-file: timeout"
            except Exception as e:
                status = f"single-file: {type(e).__name__}"
            with results_lock:
                results.append((stem, status))
            task_queue.task_done()

    n_workers = min(BATCH_SIZE, len(capture_items))
    threads = []
    for _ in range(n_workers):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)

    for item in capture_items:
        task_queue.put(item)
        time.sleep(TAB_DELAY)

    task_queue.join()

    for _ in threads:
        task_queue.put(None)
    for t in threads:
        t.join(timeout=10)

    shutil.rmtree(script_temp_dir, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _url_name(url):
    """Convert a URL to a filesystem-safe name.

    Replace every run of non-alphanumeric characters with a single '_' (this
    also collapses any consecutive '_' that appear in the input). Always
    derived from the input URL exactly as supplied — redirects and
    SingleFile-comment URLs in the saved HTML are not consulted, so a given
    input URL deterministically maps to one filename.
    """
    name = re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_")
    return name[:200]  # avoid silly-long filenames


def _doi_to_url(doi):
    """Strip the doi.org prefix if present; return as-is otherwise."""
    if not doi:
        return ""
    return doi if doi.startswith("http") else f"https://doi.org/{doi}"


def _read_doi_for_pmid(pmid):
    """Read DOI from papers/parsed/<stem>.json. Returns (stem, doi) or (None, None)."""
    from _project import pmid_to_stem
    stem = pmid_to_stem(pmid)
    if not stem:
        return None, None
    try:
        with open(parsed_path(stem), encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return stem, ""
    return stem, data.get("doi", "")


def main():
    args = parse_argv(accept={"pmids", "urls"})
    pmids = args["pmids"]
    urls = args["urls"]
    if not pmids and not urls:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    # Build fetch_items: (stem, url, output_path).
    fetch_items = []

    for pmid in pmids:
        stem, doi = _read_doi_for_pmid(pmid)
        if not stem:
            print(f"PMID {pmid}: no papers/parsed/<stem>.json (run get_refs.py first)",
                  file=sys.stderr)
            continue
        if not doi:
            print(f"{stem}: no doi in parsed JSON; skipping", file=sys.stderr)
            continue
        out = str(raw_html_path(stem))
        if os.path.exists(out):
            print(f"{stem}: skipped (html already exists)")
            continue
        fetch_items.append((stem, _doi_to_url(doi), out))

    for url in urls:
        stem = _url_name(url)
        out = str(raw_dir() / f"{stem}.html")
        if os.path.exists(out):
            print(f"{stem}: skipped (html already exists)")
            continue
        fetch_items.append((stem, url, out))

    if not fetch_items:
        return

    retry_counts = {}
    MAX_RETRIES = 3
    remaining = list(fetch_items)
    item_by_stem = {stem: (stem, url, path) for stem, url, path in fetch_items}

    while remaining:
        next_remaining = []

        for batch_start in range(0, len(remaining), BATCH_SIZE):
            batch = remaining[batch_start:batch_start + BATCH_SIZE]
            proc, port, profile_dir = None, None, None
            try:
                proc, port, profile_dir = start_browser()
                results = _fetch_batch(batch, port)
                for stem, status in results:
                    if status == "ok":
                        _emit_stem_log(stem, "success")
                        continue
                    retry_counts[stem] = retry_counts.get(stem, 0) + 1
                    _record_attempt(stem, status)
                    if retry_counts[stem] < MAX_RETRIES:
                        next_remaining.append(item_by_stem[stem])
                    elif retry_counts[stem] == MAX_RETRIES:
                        _emit_stem_log(stem, "HTML retrieval failed after 3 attempts")
            except RuntimeError as e:
                print(f"  Browser error: {e}", file=sys.stderr)
                for stem, url, path in batch:
                    if os.path.exists(path):
                        _emit_stem_log(stem, "success")
                        continue
                    retry_counts[stem] = retry_counts.get(stem, 0) + 1
                    _record_attempt(stem, f"browser error: {e}")
                    if retry_counts[stem] < MAX_RETRIES:
                        next_remaining.append(item_by_stem[stem])
                    elif retry_counts[stem] == MAX_RETRIES:
                        _emit_stem_log(stem, "HTML retrieval failed after 3 attempts")
            finally:
                stop_browser(proc, profile_dir)

        if not next_remaining:
            break
        remaining = next_remaining


if __name__ == "__main__":
    main()
