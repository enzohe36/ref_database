#!/usr/bin/env python3
"""Fetch and parse PubMed citations by PMID.

Usage:
    python get_refs.py <pmid> [<pmid> ...]
    python get_refs.py --path <file>
    python get_refs.py --delete <pmid> [<pmid> ...]
    python get_refs.py --validate

Retrieves citation metadata from PubMed. Writes to refs.json, generates
papers/<stem>.json, fetches full paper HTML via single-file to papers/<stem>.html.
Records HTML fetch failures in refs_no_html.md.
Skips non-Journal Articles, Retracted Publications, and duplicates.
--path reads PMIDs from a file (delimited by punctuation, spaces, or newlines).
--validate checks for Retracted Publications and published versions of preprints.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import websocket


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REFS_FILE = os.path.join(BASE_DIR, "refs.json")
REFS_NO_HTML_FILE = os.path.join(BASE_DIR, "refs_no_html.md")
PAPERS_DIR = os.path.join(BASE_DIR, "papers")
os.makedirs(PAPERS_DIR, exist_ok=True)
EDGE_PATH = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"



# ---------------------------------------------------------------------------
# Stem generation
# ---------------------------------------------------------------------------

def make_stem(first_last_name, year, journal, pmid):
    """Build a filesystem-safe stem from first author's last name, year, journal, and pmid.

    Converts Latin diacritics to ASCII, replaces punctuation and spaces
    with '_', collapses multiple '_' into one.
    """
    raw = f"{first_last_name} {year} {journal} {pmid}"
    nfkd = unicodedata.normalize("NFKD", raw)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_str)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug


# ---------------------------------------------------------------------------
# PubMed fetch and parse
# ---------------------------------------------------------------------------

def fetch_xml(pmid):
    """Fetch XML from PubMed E-utilities."""
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pubmed&id={pmid}&rettype=xml&retmode=xml"
    )
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


def gt(elem, path, default=""):
    """Get text from an element path."""
    el = elem.find(path) if elem is not None else None
    return el.text if el is not None and el.text else default


def parse_xml(xml_data, pmid):
    """Parse PubMed XML into citation fields and formatted output."""
    root = ET.fromstring(xml_data)
    article = root.find(".//PubmedArticle")
    mc = article.find("MedlineCitation")
    art = mc.find("Article")
    jrnl = art.find("Journal")
    ji = jrnl.find("JournalIssue")
    pd = ji.find("PubDate")
    pag = art.find("Pagination")

    journal_abbrev = gt(jrnl, "ISOAbbreviation")
    year = gt(pd, "Year")
    volume = gt(ji, "Volume")
    issue = gt(ji, "Issue")
    # Pages: prefer StartPage-EndPage, fallback to PII
    start_page = gt(pag, "StartPage") if pag is not None else ""
    end_page = gt(pag, "EndPage") if pag is not None else ""
    if start_page and end_page:
        pages = f"{start_page}-{end_page}"
    elif start_page:
        pages = start_page
    else:
        pages = ""
    if not pages:
        for el in art.findall("ELocationID"):
            if el.get("EIdType") == "pii" and el.text:
                pages = el.text
                break
    title_el = art.find("ArticleTitle")
    title = (
        ET.tostring(title_el, encoding="unicode", method="text").strip().rstrip(".")
        if title_el is not None
        else ""
    )
    doi_raw = ""
    for el in art.findall("ELocationID"):
        if el.get("EIdType") == "doi":
            doi_raw = el.text or ""
    if not doi_raw:
        pd_data_tmp = article.find("PubmedData")
        if pd_data_tmp is not None:
            aid_list_tmp = pd_data_tmp.find("ArticleIdList")
            if aid_list_tmp is not None:
                for aid in aid_list_tmp.findall("ArticleId"):
                    if aid.get("IdType") == "doi":
                        doi_raw = aid.text or ""
    doi = f"https://doi.org/{doi_raw}" if doi_raw else ""

    # Authors
    authors_raw = []
    for auth in art.findall(".//Author"):
        ln = gt(auth, "LastName")
        init = gt(auth, "Initials")
        if ln:
            affs = [
                ai.findtext("Affiliation", "").strip()
                for ai in auth.findall("AffiliationInfo")
            ]
            authors_raw.append({
                "name": f"{ln} {init}".strip(),
                "affiliation": [a for a in affs if a],
            })

    # Abstract
    abstract_parts = []
    for ab in art.findall(".//AbstractText"):
        label = ab.get("Label", "")
        text = ET.tostring(ab, encoding="unicode", method="text").strip()
        if label:
            abstract_parts.append(f"{label}: {text}")
        else:
            abstract_parts.append(text)
    abstract = " ".join(abstract_parts)

    # Keywords
    keywords = []
    kw_list = mc.find("KeywordList")
    if kw_list is not None:
        for kw in kw_list.findall("Keyword"):
            if kw.text:
                keywords.append(kw.text.strip())

    # Publication types
    pub_types = [pt.text for pt in art.findall(".//PublicationType") if pt.text]

    # CitationShort
    author_last_names = [
        gt(a, "LastName")
        for a in art.findall(".//Author")
        if gt(a, "LastName")
    ]
    num_authors = len(author_last_names)
    first_last = author_last_names[0] if author_last_names else ""
    if num_authors == 1:
        authors_short = first_last
    elif num_authors == 2:
        second_last = author_last_names[1]
        authors_short = f"{first_last} & {second_last}"
    else:
        authors_short = f"{first_last} et al."

    pd_data = article.find("PubmedData")
    pmid_from_aid = ""
    if pd_data is not None:
        aid_list = pd_data.find("ArticleIdList")
        if aid_list is not None:
            for aid in aid_list.findall("ArticleId"):
                if aid.get("IdType") == "pubmed":
                    pmid_from_aid = aid.text or ""
    pmid_final = pmid_from_aid or gt(mc, "PMID")

    citation_in_text = f"{authors_short} {year}"
    citation_short = make_stem(first_last, year, journal_abbrev, pmid_final)

    # Reference PMIDs (deduplicated)
    references = []
    if pd_data is not None:
        for ref in pd_data.findall(".//Reference"):
            for aid in ref.findall(".//ArticleId"):
                if aid.get("IdType") == "pubmed" and aid.text:
                    if aid.text not in references:
                        references.append(aid.text)

    # Validate
    if "Journal Article" not in pub_types:
        return None
    if "Retracted Publication" in pub_types:
        return None

    return {
        "pmid": pmid_final,
        "publication_types": pub_types,
        "citation_in_text": citation_in_text,
        "title": title,
        "journal": journal_abbrev,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "_authors_raw": authors_raw,
        "references": references,
        "abstract": abstract,
        "keywords": keywords,
        "citation_short": citation_short,
    }


# ---------------------------------------------------------------------------
# refs.json I/O
# ---------------------------------------------------------------------------

def load_references():
    """Load refs.json, return dict."""
    if not os.path.exists(REFS_FILE):
        return {}
    with open(REFS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_references(refs):
    """Save dict to refs.json with compact arrays for publication_types and references."""
    raw = json.dumps(refs, indent=2, ensure_ascii=False)
    for key in ("publication_types", "references"):

        def _collapse(m):
            items = [s.strip().rstrip(",") for s in m.group(2).split("\n") if s.strip()]
            return m.group(1) + " " + ", ".join(items) + " ]"

        raw = re.sub(
            rf'("{key}": \[)\s*\n(.*?)\n\s*\]',
            _collapse,
            raw,
            flags=re.DOTALL,
        )
    with open(REFS_FILE, "w", encoding="utf-8") as f:
        f.write(raw)
        f.write("\n")


def is_duplicate(pmid):
    """Check if PMID already exists in refs.json."""
    refs = load_references()
    return pmid in refs


def append_to_references(parsed):
    """Add entry to refs.json."""
    refs = load_references()
    filtered = [
        pt
        for pt in parsed["publication_types"]
        if not pt.startswith("Research Support")
    ]
    authors = [
        {"author": auth["name"], "affiliation": auth.get("affiliation", [])}
        for auth in parsed.get("_authors_raw", [])
    ]
    refs[parsed["pmid"]] = {
        "stem": parsed["citation_short"],
        "journal": parsed["journal"],
        "volume": parsed["volume"],
        "issue": parsed["issue"],
        "year": parsed["year"],
        "title": parsed["title"],
        "pages": parsed["pages"],
        "doi": parsed["doi"],
        "authors": authors,
        "publication_types": filtered,
        "references": parsed.get("references", []),
    }
    save_references(refs)


# ---------------------------------------------------------------------------
# refs_no_html.md
# ---------------------------------------------------------------------------

def _append_to_section(filepath, section_header, stem, doi, reason=""):
    """Append stem + doi + reason under a ## section in a markdown file.

    Creates the section if it doesn't exist. Skips if stem already listed.
    """
    content = ""
    if os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        if stem in content:
            return

    entry = f"stem:   {stem}\ndoi:    {doi}\nreason: {reason}\n"

    if section_header in content:
        # Append after the section header
        idx = content.index(section_header) + len(section_header)
        # Find end of header line
        nl = content.find("\n", idx)
        if nl == -1:
            content += "\n\n" + entry
        else:
            content = content[:nl + 1] + "\n" + entry + content[nl + 1:]
    else:
        # Create new section at end
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"\n{section_header}\n\n{entry}"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def append_to_no_html(parsed, reason="HTML retrieval failed after 3 attempts"):
    """Append citation_short + doi to refs_no_html.md under retrieval failures."""
    _append_to_section(
        REFS_NO_HTML_FILE,
        "## HTML Retrieval Failures",
        parsed["citation_short"],
        parsed["doi"],
        reason,
    )


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

        # Capture with single-file using resolved URL
        result = subprocess.run(
            [
                "single-file",
                "--browser-server",
                f"http://localhost:{port}",
                "--browser-wait-until=load",
                "--browser-wait-delay=5000",
                "--browser-load-max-time=120000",
                "--browser-capture-max-time=120000",
                "--remove-hidden-elements=false",
                "--block-scripts=false",
                "--removed-elements-selector=script[src]",
                resolved_url,
                output_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
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


def _gather_cross_origin_css(ws_url, page_url, dest_dir):
    """Read <link rel=stylesheet> hrefs from the tab at ws_url, download
    any that are cross-origin to page_url, and write to dest_dir.

    Same-origin sheets are skipped — single-file's serializer can read
    their cssRules directly. Cross-origin sheets are opaque to page-context
    JS and get stripped; injecting their contents as same-origin <style>
    tags via --browser-stylesheet bypasses that.

    Returns a list of local file paths.
    """
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
    except Exception:
        return []
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

    files = []
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
        import hashlib
        fname = hashlib.md5(url.encode()).hexdigest()[:16] + ".css"
        path = os.path.join(dest_dir, fname)
        try:
            with open(path, "wb") as f:
                f.write(data)
            files.append(path)
        except Exception:
            pass
    return files


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
            preload_tabs.append((tab_id, info.get("url", doi_url)))

    # Dir for cross-origin CSS downloads; handed to single-file as
    # --browser-stylesheet so same-origin serialization picks them up.
    css_temp_dir = tempfile.mkdtemp(prefix="sf_css_")
    css_by_stem = {}  # stem -> list of local CSS file paths

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
            css_by_stem[stem] = _gather_cross_origin_css(
                ws_url, resolved_url, css_temp_dir
            )
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
                            css_by_stem[s] = _gather_cross_origin_css(
                                tws, resolved_url, css_temp_dir
                            )
                        _cdp_close_tab(tid, port)
                    del pending[key]
                    continue

                # Check for user action (Enter or Mark as blocked)
                action = _cdp_poll_action(ws_conn)
                if action == "done":
                    for s, tid, tws in tabs:
                        if tws:
                            css_by_stem[s] = _gather_cross_origin_css(
                                tws, "", css_temp_dir
                            )
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
            (stem, resolved_url, output_path, css_by_stem.get(stem, []))
        )

    # --- Capture ---
    task_queue = queue.Queue()

    def worker():
        while True:
            item = task_queue.get()
            if item is None:
                break
            stem, resolved_url, output_path, css_files = item
            try:
                args = [
                    "single-file",
                    "--browser-server",
                    f"http://localhost:{port}",
                    "--browser-wait-until=load",
                    "--browser-wait-delay=5000",
                    "--browser-load-max-time=120000",
                    "--browser-capture-max-time=120000",
                    "--remove-hidden-elements=false",
                    "--block-scripts=false",
                    "--removed-elements-selector=script[src]",
                    "--remove-unused-fonts=false",
                    "--remove-unused-styles=false",
                    "--remove-alternative-fonts=false",
                    "--remove-alternative-medias=false",
                ]
                for css_path in css_files:
                    args.append(f"--browser-stylesheet={css_path}")
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
                    if "cf-ratelimit-blocked" in content:
                        os.remove(output_path)
                        status = "single-file: cf rate-limit page"
                    elif "Validate User" in content[:1000]:
                        os.remove(output_path)
                        status = "single-file: validate user page"
                    elif ("citation_title" not in content
                            and "Abstract" not in content
                            and "abstract" not in content):
                        os.remove(output_path)
                        status = "single-file: no article markers"
                    else:
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

    shutil.rmtree(css_temp_dir, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# PubMed search
# ---------------------------------------------------------------------------

def search_pmids(query):
    """Search PubMed and return list of PMIDs."""
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={urllib.parse.quote(query)}&retmax=20&retmode=xml"
    )
    with urllib.request.urlopen(url) as resp:
        xml_data = resp.read().decode("utf-8")
    root = ET.fromstring(xml_data)
    return [id_el.text for id_el in root.findall(".//IdList/Id")]


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def validate():
    """Validate all PMIDs in refs.json."""
    refs = load_references()
    pmids = list(refs.keys())
    print(f"Validating {len(pmids)} entries...", flush=True)
    retracted = []
    preprints = []
    fetch_count = 0
    all_pmids_set = set(pmids)

    for pmid in pmids:
        if fetch_count > 0:
            time.sleep(0.4)
        try:
            xml_data = fetch_xml(pmid)
            fetch_count += 1
            root = ET.fromstring(xml_data)
            pub_types = [
                pt.text for pt in root.findall(".//PublicationType") if pt.text
            ]
        except Exception as e:
            print(json.dumps({"pmid": pmid, "status": "error", "message": str(e)}))
            continue

        if "Retracted Publication" in pub_types:
            retracted.append(pmid)
        if "Preprint" in pub_types:
            title_el = root.find(".//ArticleTitle")
            title = (
                ET.tostring(title_el, encoding="unicode", method="text").strip()
                if title_el is not None
                else ""
            )
            preprints.append((pmid, title))

    if retracted:
        for pmid in retracted:
            print(json.dumps({"pmid": pmid, "status": "retracted"}))
    else:
        print(json.dumps({"status": "ok", "message": "No retracted articles found."}))

    for pmid, title in preprints:
        if fetch_count > 0:
            time.sleep(0.4)
        try:
            query = f"{title} NOT preprint[pt]"
            result_pmids = search_pmids(query)
            fetch_count += 1
        except Exception as e:
            print(
                json.dumps(
                    {
                        "pmid": pmid,
                        "status": "error",
                        "message": f"Search failed: {e}",
                    }
                )
            )
            continue

        candidates = [
            p for p in result_pmids if p != pmid and p not in all_pmids_set
        ]
        print(
            json.dumps(
                {
                    "pmid": pmid,
                    "status": "preprint",
                    "title": title,
                    "candidates": candidates,
                }
            )
        )

    if not preprints:
        print(json.dumps({"status": "ok", "message": "No preprints to check."}))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python get_refs.py <pmid> [<pmid> ...]\n"
            "       python get_refs.py --path <file>\n"
            "       python get_refs.py --delete <pmid> [<pmid> ...]\n"
            "       python get_refs.py --validate",
            file=sys.stderr,
        )
        sys.exit(1)

    if sys.argv[1] == "--validate":
        validate()
        return

    if sys.argv[1] == "--delete":
        if len(sys.argv) < 3:
            print(
                "Usage: python get_refs.py --delete <pmid> [<pmid> ...]",
                file=sys.stderr,
            )
            sys.exit(1)
        refs = load_references()
        for pmid in sys.argv[2:]:
            if pmid in refs:
                del refs[pmid]
                print(json.dumps({"pmid": pmid, "status": "deleted"}))
            else:
                print(json.dumps({"pmid": pmid, "status": "not found"}))
        save_references(refs)
        return

    if sys.argv[1] == "--path":
        if len(sys.argv) < 3:
            print("Usage: python get_refs.py --path <file>", file=sys.stderr)
            sys.exit(1)
        with open(sys.argv[2], encoding="utf-8") as f:
            pmids = re.findall(r"\d+", f.read())
    else:
        pmids = sys.argv[1:]

    # For each PMID, three independent checks:
    #   1. refs.json entry exists? If not, fetch from PubMed and add.
    #   2. papers/<stem>.json exists? If not, generate.
    #   3. papers/<stem>.html exists? If not, fetch via browser.
    # Each check uses the parsed data but doesn't depend on whether
    # previous checks added or skipped.

    refs = load_references()
    fetched_count = 0
    need_html = []  # (parsed, stem) for papers needing HTML fetch

    for pmid in pmids:
        pmid = re.sub(r".*/(\d+)/?$", r"\1", pmid.strip().rstrip("/"))

        # Step 1: refs.json
        if pmid in refs:
            # Reconstruct parsed-like dict from existing entry for steps 2-3
            entry = refs[pmid]
            stem = entry.get("stem", "")
            doi = entry.get("doi", "")
            parsed_proxy = {"pmid": pmid, "citation_short": stem, "doi": doi}
        else:
            if fetched_count > 0:
                time.sleep(0.4)
            try:
                xml_data = fetch_xml(pmid)
                fetched_count += 1
                parsed = parse_xml(xml_data, pmid)
            except Exception as e:
                print(json.dumps({"pmid": pmid, "status": "error", "message": str(e)}))
                continue
            if parsed is None:
                pub_types = [
                    pt.text
                    for pt in ET.fromstring(xml_data).findall(".//PublicationType")
                    if pt.text
                ]
                reason = (
                    "Retracted Publication"
                    if "Retracted Publication" in pub_types
                    else "not a Journal Article"
                )
                print(
                    json.dumps(
                        {
                            "pmid": pmid,
                            "status": "skipped",
                            "message": f"PMID {pmid}: {reason}. PublicationTypes: {pub_types}",
                        }
                    )
                )
                continue
            append_to_references(parsed)
            parsed_proxy = parsed

        stem = parsed_proxy["citation_short"]
        doi = parsed_proxy["doi"]

        # Step 2: papers/<stem>.html
        html_path = os.path.join(PAPERS_DIR, f"{stem}.html")
        if not os.path.exists(html_path):
            if doi:
                need_html.append((parsed_proxy, stem))
            else:
                append_to_no_html(parsed_proxy, "no DOI available")

    # Fetch HTML for all papers that need it
    if not need_html:
        return

    parsed_by_stem = {stem: p for p, stem in need_html}

    def on_failure(stem):
        append_to_no_html(parsed_by_stem[stem])

    fetch_items = [
        (stem, p["doi"], os.path.join(PAPERS_DIR, f"{stem}.html"))
        for p, stem in need_html
    ]

    # Track retry counts
    retry_counts = {}  # stem -> attempts so far
    MAX_RETRIES = 3
    remaining = list(fetch_items)

    # Build lookup for retry
    item_by_stem = {stem: (stem, doi, path) for stem, doi, path in fetch_items}

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
                        on_failure(stem)
                        _emit_stem_log(
                            stem, "HTML retrieval failed after 3 attempts"
                        )
            except RuntimeError as e:
                print(f"  Browser error: {e}", file=sys.stderr)
                for stem, doi, path in batch:
                    if os.path.exists(path):
                        _emit_stem_log(stem, "success")
                        continue
                    retry_counts[stem] = retry_counts.get(stem, 0) + 1
                    _record_attempt(stem, f"browser error: {e}")
                    if retry_counts[stem] < MAX_RETRIES:
                        next_remaining.append(item_by_stem[stem])
                    elif retry_counts[stem] == MAX_RETRIES:
                        on_failure(stem)
                        _emit_stem_log(
                            stem, "HTML retrieval failed after 3 attempts"
                        )
            finally:
                stop_browser(proc, profile_dir)

        # Failures retry in the next round
        if not next_remaining:
            break
        remaining = next_remaining


if __name__ == "__main__":
    main()
