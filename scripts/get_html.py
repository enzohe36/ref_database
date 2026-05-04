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
newlines.
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
            with urllib.request.urlopen(req, timeout=60) as resp:
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
            with urllib.request.urlopen(req, timeout=30) as resp:
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
            with urllib.request.urlopen(req, timeout=30) as resp:
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


def _get_post_capture(url):
    """Return the post_capture callable for a URL, or None if no rule matches."""
    if not url:
        return None
    for key, rule in _PUBLISHER_RULES.items():
        if key in url:
            return rule.get("post_capture")
    return None


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
        "wait_delay": 15000,
    },
    "biorxiv.org": {
        "url": lambda u: u if u.endswith(".full") else u.rstrip("/") + ".full",
        # Figures use lazysizes (src=1x1 gif, real URL in data-src). The
        # force-lazyload JS runs at 'load' and swaps them in; SingleFile
        # then needs time to fetch + embed the real images.
        "wait_delay": 15000,
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
    },
    # iucr landing pages link to per-figure sub-pages instead of
    # inlining full-resolution figures. SingleFile only saves the
    # landing page (100 px thumbnails). `post_capture` enriches the
    # saved HTML by visiting each sub-page and inlining its main
    # image as a data URL in the thumbnail's <img src>.
    "journals.iucr.org": {
        "post_capture": lambda path, port: _iucr_inline_figures(path, port),
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
    # inlined src becomes the visible foreground).
    "pubs.acs.org": {
        "wait_delay": 15000,
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
    # Nature (nature.com): figure images lazy-load via empty srcset; the
    # JSON-LD `image` array holds the lw1200 URLs in order. The browser-
    # script `_NATURE_FIGURES_FIX_JS` swaps each <img> to the matching
    # JSON-LD URL so SingleFile inlines the full-resolution image.
    "nature.com": {
        "wait_delay": 20000,
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
            with urllib.request.urlopen(req, timeout=30) as resp:
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
    """Convert a URL to a filesystem-safe name (punctuation -> underscore).

    Always derived from the input URL exactly as supplied — redirects and
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
