#!/usr/bin/env python3
"""Convert article HTML pages to structured JSON.

Usage:
    python convert_html.py [<path> ...]

Each path can be an HTML file or a directory (all .html files in the
directory will be processed). Defaults to papers/ when no path is given.
Skips files whose JSON already has non-empty main_text.

Two-phase processing:
  Phase 1: Parse all HTMLs in parallel via ProcessPoolExecutor (no browser).
  Phase 2: Fetch failed HTMLs in rounds. Each round opens one browser,
           preloads all URLs with 1s delay, captures in parallel, then
           re-parses. Rounds repeat until no requests remain or MAX_RETRIES
           rounds are exhausted.
"""

import atexit
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor

from html_parsers import (
    clean_parsed_output,
    detect_domain,
    detect_url,
    get_parser,
    _DOMAIN_ALIASES,
)
from get_refs import (
    start_browser,
    stop_browser,
    _append_to_section,
    REFS_NO_HTML_FILE,
    CDP_PORT,
)

MIN_MAIN_TEXT_LEN = 5000
MIN_REFERENCES = 5
MAX_RETRIES = 3

# URL transforms for retry: domain substring -> function to modify URL.
# Publishers listed here always retry with the transformed URL before
# PMC fallback, even when metadata is present.
_RETRY_URL_TRANSFORMS = {
    "cshlp": lambda url: url if url.endswith(".long") else url.rstrip("/") + ".long",
}

# Single-file wait strategy overrides per domain.
# Default is "networkIdle"; some sites need "load" to avoid
# execution context errors from late JS navigation.
_WAIT_UNTIL_OVERRIDES = {
    "cshlp": "load",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Direct fetch (no preloading)
# ---------------------------------------------------------------------------

def _fetch_direct(url, output_path, port, wait_until="networkIdle"):
    """Fetch a URL directly via single-file without preloading.

    Writes to a temp file first, replaces original only on success
    to avoid losing the existing HTML on fetch failure.
    """
    import subprocess
    try:
        tmp_path = output_path + ".tmp"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        result = subprocess.run(
            [
                "single-file",
                "--browser-server",
                f"http://localhost:{port}",
                f"--browser-wait-until={wait_until}",
                "--browser-wait-delay=5000",
                "--block-scripts=false",
                url,
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
            os.replace(tmp_path, output_path)
            return True
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


# ---------------------------------------------------------------------------
# PMC lookup and fetch
# ---------------------------------------------------------------------------

def _pmcid_for_doi(doi):
    """Look up PMC ID for a DOI via NCBI ID Converter API. Returns PMC ID or None."""
    # Strip https://doi.org/ prefix if present
    bare_doi = re.sub(r"^https?://doi\.org/", "", doi)
    url = (
        f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
        f"?ids={bare_doi}&format=json"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        for record in data.get("records", []):
            pmcid = record.get("pmcid", "")
            if pmcid:
                return pmcid.replace("PMC", "")
    except Exception:
        pass
    return None


def _fetch_pmc(pmcid, output_path, port):
    """Fetch PMC article via single-file. Returns True on success."""
    import subprocess

    pmc_url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmcid}/"
    try:
        # Fetch to a temp file first, then replace original on success.
        # Avoids losing the original if the fetch fails.
        tmp_path = output_path + ".pmc_tmp"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        subprocess.run(
            [
                "single-file",
                "--browser-server",
                f"http://localhost:{port}",
                "--browser-wait-until=networkIdle",
                "--browser-wait-delay=5000",
                "--block-scripts=false",
                pmc_url,
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
            os.replace(tmp_path, output_path)
            return True
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


# ---------------------------------------------------------------------------
# Journal name -> NLM MedAbbr lookup
# ---------------------------------------------------------------------------
#
# Applied as a post-processing step on parser output. Parsers emit whatever
# form the publisher supplies (full title, ISO abbrev, sub-journal variant);
# _abbreviate_journals normalizes to the NLM MedAbbr so refs.json and
# papers/<stem>.json agree on the canonical journal string.
#
# The NLM journal list is downloaded once per convert run to a temp file
# (cleaned up at exit) and shared with parallel workers through the
# ProcessPoolExecutor initializer so they don't each redownload.

_JOURNALS_URL = "https://ftp.ncbi.nih.gov/pubmed/J_Entrez.txt"
_JOURNALS_FILE = None
_JOURNAL_LOOKUP = None
# Prefix index: tuple(normalized paren-stripped JournalTitle tokens[:n]) ->
# abbr, populated only when exactly one NLM entry registers that prefix.
# Min prefix length = 3 tokens (see _PREFIX_MIN_TOKENS).
_JOURNAL_PREFIX_INDEX = None
_PREFIX_MIN_TOKENS = 3


def _cleanup_journals_file():
    """atexit hook: remove the temp journals.json written by ensure_journals."""
    global _JOURNALS_FILE
    if _JOURNALS_FILE and os.path.exists(_JOURNALS_FILE):
        os.remove(_JOURNALS_FILE)
        _JOURNALS_FILE = None


atexit.register(_cleanup_journals_file)


def ensure_journals():
    """Download the NLM J_Entrez.txt file, parse it, and write the
    result to a temp JSON keyed by NlmId. Returns the file path.
    Re-downloads each run; short-circuits if the path is already set
    (via _worker_init in child processes).
    """
    global _JOURNALS_FILE
    if _JOURNALS_FILE and os.path.exists(_JOURNALS_FILE):
        return _JOURNALS_FILE
    print(f"Downloading {_JOURNALS_URL}...", flush=True)
    with urllib.request.urlopen(_JOURNALS_URL) as resp:
        data = resp.read().decode("utf-8")

    entries = []
    current = {}
    for line in data.split("\n"):
        line = line.strip()
        if line.startswith("---"):
            if current:
                entries.append(current)
            current = {}
        elif ": " in line:
            key, val = line.split(": ", 1)
            current[key] = val.strip()
    if current:
        entries.append(current)

    journal_map = {}
    for e in entries:
        nlmid = e.get("NlmId", "").strip()
        abbr = e.get("MedAbbr", "").strip()
        title = e.get("JournalTitle", "").strip()
        if nlmid and abbr:
            journal_map[nlmid] = {"JournalTitle": title, "MedAbbr": abbr}

    fd, path = tempfile.mkstemp(prefix="journals_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(journal_map, f, indent=2, ensure_ascii=False)
        f.write("\n")
    _JOURNALS_FILE = path
    print(f"Loaded {len(journal_map)} journals")
    return path


def _norm_journal_key(s):
    """Case- and punctuation-insensitive key for journal-name matching.

    Normalizes '&' to 'and' (NLM uses '&' in some titles where publishers
    emit 'and') and strips a leading 'the' so 'The Journal of biological
    chemistry' matches 'Journal of Biological Chemistry'.
    """
    if not s:
        return ""
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def _load_journal_lookup(journals_path):
    """Populate the module-level lookup and prefix index from the
    journals.json temp file.

    Verbatim lookup: publisher -> MedAbbr with tiered priority per key.
    Each candidate is registered with a priority tier:
      0 (highest) - MedAbbr verbatim. Parsers that already emit the
        MedAbbr pass through unchanged; PubMed-canonical MedAbbrs win
        when they collide with a JournalTitle of a different journal
        (e.g. 'Nucleus' is the MedAbbr of the Austin, Tex. Nucleus and
        also the ' : '-stripped form of The Nucleus (Calcutta) title —
        the Austin entry wins because its MedAbbr is the exact match).
      1 - JournalTitle verbatim.
      2 - Transforms: ' : ' qualifier stripped ('Genes to cells :
        devoted to...' -> 'Genes to Cells'), trailing 'of the United
        States of America' stripped (PNAS), MedAbbr paren suffix
        stripped ('DNA Repair (Amst)' -> 'DNA Repair').
    If multiple distinct MedAbbrs share the top (lowest) tier for a
    key, the key is dropped — an unambiguous resolution is preferred
    over a guess.

    Prefix index: tuple of normalized JournalTitle tokens[:n] -> MedAbbr
    for n >= _PREFIX_MIN_TOKENS. Tokenization keeps parenthesized
    content so 'Angewandte Chemie (International ed. in English)'
    tokenizes as [angewandte, chemie, international, ed, in, english]
    and a publisher string 'Angewandte Chemie International Edition'
    matches at the 3-token prefix. Prefixes registered by more than one
    entry are discarded.
    """
    global _JOURNAL_LOOKUP, _JOURNAL_PREFIX_INDEX
    with open(journals_path, encoding="utf-8") as f:
        jdata = json.load(f)

    # key -> list of (tier, abbr)
    candidates = {}
    prefix_counts = {}
    prefix_abbr = {}

    def add(key, abbr, tier):
        if not key:
            return
        candidates.setdefault(key, []).append((tier, abbr))

    for entry in jdata.values():
        abbr = entry.get("MedAbbr", "").strip()
        if not abbr:
            continue
        jt = entry.get("JournalTitle", "").strip()
        add(_norm_journal_key(abbr), abbr, 0)
        if "-" in abbr:
            # NLM hyphenates some tokens ('Sub-cellular' in MedAbbrs like
            # 'Subcell Biochem' are not hyphenated but JournalTitles
            # sometimes are). Index the hyphen-removed concatenated form
            # so publisher non-hyphenated forms match.
            add(_norm_journal_key(abbr.replace("-", "")), abbr, 2)
        if jt:
            add(_norm_journal_key(jt), abbr, 1)
            if "-" in jt:
                add(_norm_journal_key(jt.replace("-", "")), abbr, 2)
            if " : " in jt:
                add(_norm_journal_key(jt.split(" : ", 1)[0]), abbr, 2)
            trimmed = re.sub(
                r"\s+of\s+the\s+United\s+States\s+of\s+America\s*$",
                "", jt, flags=re.IGNORECASE,
            )
            if trimmed != jt:
                add(_norm_journal_key(trimmed), abbr, 2)
            # Prefix index: tokenize keeping paren content so qualified
            # NLM titles expose their descriptive words as token
            # candidates (see docstring).
            toks = _norm_journal_key(jt).split()
            for n in range(_PREFIX_MIN_TOKENS, len(toks) + 1):
                prefix = tuple(toks[:n])
                prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
                if prefix_counts[prefix] == 1:
                    prefix_abbr[prefix] = abbr
        if "(" in abbr:
            stripped = re.sub(r"\s*\([^)]*\)\s*", " ", abbr).strip()
            if stripped:
                add(_norm_journal_key(stripped), abbr, 2)

    d = {}
    for key, cands in candidates.items():
        cands.sort(key=lambda x: x[0])
        top = cands[0][0]
        top_abbrs = {c[1] for c in cands if c[0] == top}
        if len(top_abbrs) == 1:
            d[key] = next(iter(top_abbrs))
        # else: ambiguous top tier — leave key unmapped so lookup
        # returns the publisher string verbatim rather than guessing.
    _JOURNAL_LOOKUP = d
    _JOURNAL_PREFIX_INDEX = {
        p: prefix_abbr[p] for p, c in prefix_counts.items() if c == 1
    }


def _lookup_journal(name):
    """Resolve a publisher journal name to an NLM MedAbbr.

    Tries in order: verbatim lookup, verbatim lookup with
    parenthesized qualifier stripped from the query (publishers like
    Elsevier brand sub-journals as 'X (BBA) - Y'), then progressive
    prefix matching — iterates n from _PREFIX_MIN_TOKENS upward and
    returns the shortest publisher-token prefix that identifies
    exactly one NLM entry. Returns the original name if nothing
    matches.
    """
    if not name or not _JOURNAL_LOOKUP:
        return name
    key = _norm_journal_key(name)
    if key in _JOURNAL_LOOKUP:
        return _JOURNAL_LOOKUP[key]
    if "(" in name:
        stripped_key = _norm_journal_key(
            re.sub(r"\s*\([^)]*\)\s*", " ", name)
        )
        if stripped_key and stripped_key != key and stripped_key in _JOURNAL_LOOKUP:
            return _JOURNAL_LOOKUP[stripped_key]
    if _JOURNAL_PREFIX_INDEX:
        toks = key.split()
        for n in range(_PREFIX_MIN_TOKENS, len(toks) + 1):
            hit = _JOURNAL_PREFIX_INDEX.get(tuple(toks[:n]))
            if hit is not None:
                return hit
    return name


def _abbreviate_journals(parsed):
    """Replace journal names in parsed dict with their NLM MedAbbr.

    Walks the papers/*.json-format dict and rewrites 'journal' on the main
    paper and on each reference. Parsers that already emit the MedAbbr
    are unaffected (the lookup's passthrough entries map MedAbbr to
    itself). Unknown journals are left verbatim so parser output is
    preserved for titles absent from the NLM list. No-op when the
    lookup has not been loaded (for tests or direct parser calls).
    """
    if not parsed or not _JOURNAL_LOOKUP:
        return parsed
    if parsed.get("journal"):
        parsed["journal"] = _lookup_journal(parsed["journal"])
    for ref in parsed.get("references") or []:
        if not isinstance(ref, dict):
            continue
        inner = ref.get("") if "" in ref else ref
        if isinstance(inner, dict) and inner.get("journal"):
            inner["journal"] = _lookup_journal(inner["journal"])
    return parsed


# ---------------------------------------------------------------------------
# Parse HTML into papers/*.json-format object
# ---------------------------------------------------------------------------

def _parse_html(html, parser):
    """Parse HTML into a papers/*.json-format dict using the publisher parser.

    Calls parser.parse_article(html) to produce the papers/*.json-format dict,
    runs clean_parsed_output to enforce shared formatting rules (no
    trailing period in titles, no dots in journal abbreviations), then
    _abbreviate_journals to normalize main-paper and reference journal
    names to their NLM MedAbbr via the lookup loaded in main().
    Returns dict with all paper fields, or None if parsing fails.
    """
    try:
        parsed = parser.parse_article(html)
    except Exception:
        return None
    parsed = clean_parsed_output(parsed)
    return _abbreviate_journals(parsed)


def _quality_ok(parsed):
    """Check if main_text and references pass quality check.

    Both must hold: main_text >= MIN_MAIN_TEXT_LEN chars and
    references count >= MIN_REFERENCES. A short reference list
    indicates the parser only got an abstract or a stub page.
    """
    if not parsed:
        return False
    text = parsed.get("main_text", "")
    refs = parsed.get("references") or []
    return len(text) >= MIN_MAIN_TEXT_LEN and len(refs) >= MIN_REFERENCES


def _has_metadata(parsed):
    """Check if parsed result has metadata (title or abstract).

    Distinguishes genuine retrieval failures (blocked/rate-limited page with
    no content at all) from pages that loaded fine but lack full-text
    (old papers, paywalls).
    """
    if not parsed:
        return False
    return bool(parsed.get("title"))


# ---------------------------------------------------------------------------
# Failure logging
# ---------------------------------------------------------------------------

_ATTEMPTS = {}  # stem -> [reason, reason, ...] accumulated across tries


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


def _mark_success(stem):
    """Emit the stem's attempt log (if any) and mark it resolved."""
    _emit_stem_log(stem, "success")


def _log_parse_failure(stem, doi, reason=""):
    """Append terminal failure to refs_no_html.md and emit per-stem log."""
    _append_to_section(
        REFS_NO_HTML_FILE,
        "## HTML Parsing Failures",
        stem,
        doi,
        reason,
    )
    _emit_stem_log(stem, reason)


# ---------------------------------------------------------------------------
# Process one HTML file
# ---------------------------------------------------------------------------

def _resolve_doi(stem):
    """Look up DOI from refs.json by PMID in stem."""
    parts = stem.rsplit("_", 1)
    if parts and parts[-1].isdigit():
        from get_refs import load_references
        refs = load_references()
        entry = refs.get(parts[-1], {})
        return entry.get("doi", "")
    return ""


def process_html(html_path):
    """Process a single HTML file (parse only, no browser retrieval).

    Pure worker: no mutation of module globals or shared log files beyond
    writing the per-path JSON and banner-cleaned HTML. Safe to run inside
    a ProcessPoolExecutor worker; the caller is responsible for replaying
    attempts/terminal state into the main-process log.

    Returns a dict with keys:
      html_path: input path
      needs:    None | "retry" | "pmc"
      stem:     filename stem
      doi:      resolved DOI (from parsed HTML or refs.json)
      pmcid:    PMC ID when needs == "pmc", else None
      attempts: list of failure reasons accumulated in this call
      terminal: None | ("success",) | ("failure", reason)
    """
    attempts = []

    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    stem = os.path.splitext(os.path.basename(html_path))[0]

    # Detect publisher
    try:
        publisher = detect_domain(html)
        parser = get_parser(publisher)
    except ValueError as e:
        msg = str(e)
        if "No parser module" in msg:
            # "No parser module for domain: plos. Create html_parsers/..."
            domain_name = msg.split(":", 1)[1].strip().split(".", 1)[0]
            reason = f"parse: no parser for domain: {domain_name}"
        elif "No SingleFile URL" in msg:
            reason = "parse: no SingleFile URL"
        else:
            reason = f"parse: {msg}"
        attempts.append(reason)
        return {
            "html_path": html_path,
            "needs": None,
            "stem": stem,
            "doi": _resolve_doi(stem),
            "pmcid": None,
            "attempts": attempts,
            "terminal": ("failure", reason),
        }

    # Remove banners and write cleaned HTML back
    cleaned = parser.remove_banners(html)
    if cleaned != html:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(cleaned)
        html = cleaned

    # Parse
    parsed = _parse_html(html, parser)

    # Resolve DOI
    doi = parsed.get("doi", "") if parsed else ""
    if not doi:
        doi = _resolve_doi(stem)

    is_pmc = publisher == "nih"
    json_path = os.path.join(os.path.dirname(html_path), f"{stem}.json")

    # Determine if fetching is needed
    original_url = detect_url(html) or ""
    _needs_transform = False
    for k, transform in _RETRY_URL_TRANSFORMS.items():
        if k in original_url and transform(original_url) != original_url:
            _needs_transform = True
            break
    needs = None
    pmcid = None
    if not is_pmc and not _quality_ok(parsed) and doi:
        if not _has_metadata(parsed) or _needs_transform:
            needs = "retry"
        else:
            pmcid = _pmcid_for_doi(doi)
            if pmcid:
                needs = "pmc"

    # Write whatever we have
    if parsed:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=2, ensure_ascii=False)
            f.write("\n")

    terminal = None
    if _quality_ok(parsed):
        terminal = ("success",)
    else:
        if not parsed:
            reason = "parse: parser exception"
        elif _has_metadata(parsed):
            reason = "parse: abstract only"
        else:
            reason = "parse: no content"
        attempts.append(reason)
        if not needs:
            terminal = ("failure", reason)

    return {
        "html_path": html_path,
        "needs": needs,
        "stem": stem,
        "doi": doi,
        "pmcid": pmcid,
        "attempts": attempts,
        "terminal": terminal,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _should_skip(html_path):
    """Check if the corresponding JSON already passes the quality check."""
    stem = os.path.splitext(os.path.basename(html_path))[0]
    json_path = os.path.join(os.path.dirname(html_path), f"{stem}.json")
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        if _quality_ok(data):
            return True
    return False


def _collect_paths(args):
    """Collect HTML paths from CLI arguments.

    Directory args are walked recursively so that papers/ (which now
    contains per-publisher subdirectories) can be passed as a single
    argument.
    """
    paths = []
    for arg in args:
        path = os.path.join(os.getcwd(), arg) if not os.path.isabs(arg) else arg
        if not os.path.exists(path):
            print(f"Not found: {path}", file=sys.stderr)
            continue
        if os.path.isdir(path):
            htmls = []
            for root, _dirs, files in os.walk(path):
                for f in files:
                    if f.endswith(".html"):
                        htmls.append(os.path.join(root, f))
            paths.extend(sorted(htmls))
        else:
            paths.append(path)
    return [p for p in paths if not _should_skip(p)]


def _fetch_round(fetch_items, port):
    """Run one round of fetch requests with 1s delay between each.

    Each item is (html_path, fetch_type, url_or_pmcid, needs_preload).
    Items needing preload: open tabs sequentially, wait, resolve URLs.
    Direct items: capture immediately with single-file.
    All captures run in parallel.
    Returns list of items that still need fetching.
    """
    from get_refs import (
        _cdp_open_tab,
        _cdp_close_tab,
        PAGE_LOAD_WAIT,
    )
    import threading
    import queue
    import subprocess

    still_needed = []

    # Resolve URLs: preload items that need it, direct items use URL as-is
    resolved_urls = []
    preload_tabs = {}  # index -> tab_id
    for i, (html_path, fetch_type, url_or_id, needs_preload) in enumerate(fetch_items):
        if fetch_type == "pmc":
            url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{url_or_id}/"
        else:
            url = url_or_id
        resolved_urls.append(url)
        if needs_preload:
            try:
                target = _cdp_open_tab(url, port)
                preload_tabs[i] = target["id"]
            except Exception:
                pass
            time.sleep(1)

    # Wait for preloaded tabs and resolve their URLs
    if preload_tabs:
        time.sleep(PAGE_LOAD_WAIT)
        tab_info = {}
        try:
            tabs_list = json.loads(
                urllib.request.urlopen(
                    f"http://localhost:{port}/json/list"
                ).read()
            )
            for t in tabs_list:
                tab_info[t["id"]] = t.get("url", "")
        except Exception:
            pass

        for i, tid in preload_tabs.items():
            resolved = tab_info.get(tid, resolved_urls[i])
            resolved_urls[i] = resolved
            try:
                req = urllib.request.Request(
                    f"http://localhost:{port}/json/close/{tid}", method="PUT"
                )
                urllib.request.urlopen(req)
            except Exception:
                pass
        time.sleep(1)

    # Capture all in parallel
    results = {}
    results_lock = threading.Lock()
    task_queue = queue.Queue()

    def worker():
        while True:
            item = task_queue.get()
            if item is None:
                break
            idx, resolved_url, output_path = item
            tmp_path = output_path + ".tmp"
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            # Determine wait strategy from URL
            wait = "networkIdle"
            for domain_key, w in _WAIT_UNTIL_OVERRIDES.items():
                if domain_key in resolved_url:
                    wait = w
                    break
            try:
                subprocess.run(
                    [
                        "single-file",
                        "--browser-server",
                        f"http://localhost:{port}",
                        f"--browser-wait-until={wait}",
                        "--browser-wait-delay=5000",
                        "--block-scripts=false",
                        resolved_url,
                        tmp_path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if not os.path.exists(tmp_path):
                    outcome = "no output"
                elif os.path.getsize(tmp_path) <= 1000:
                    os.remove(tmp_path)
                    outcome = "output too small"
                else:
                    os.replace(tmp_path, output_path)
                    outcome = "ok"
            except subprocess.TimeoutExpired:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                outcome = "timeout"
            except Exception as e:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                outcome = type(e).__name__
            with results_lock:
                results[idx] = outcome
            task_queue.task_done()

    n_workers = min(30, len(fetch_items))
    threads = []
    for _ in range(n_workers):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)

    for i, (html_path, _, _, _) in enumerate(fetch_items):
        task_queue.put((i, resolved_urls[i], html_path))
        time.sleep(1)

    task_queue.join()
    for _ in threads:
        task_queue.put(None)
    for t in threads:
        t.join(timeout=10)

    # Re-parse fetched files, determine which still need work
    for i, (html_path, fetch_type, url_or_id, _preload) in enumerate(fetch_items):
        stem = os.path.splitext(os.path.basename(html_path))[0]
        outcome = results.get(i)
        if outcome != "ok":
            _record_attempt(stem, f"fetch ({fetch_type}): {outcome or 'no result'}")
            still_needed.append((html_path, fetch_type, url_or_id, _preload))
            continue
        # Re-parse
        try:
            with open(html_path, encoding="utf-8") as f:
                html = f.read()
            publisher = detect_domain(html)
            parser = get_parser(publisher)
            cleaned = parser.remove_banners(html)
            if cleaned != html:
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(cleaned)
                html = cleaned
            parsed = _parse_html(html, parser)
        except Exception as e:
            _record_attempt(stem, f"reparse ({fetch_type}): {type(e).__name__}")
            still_needed.append((html_path, fetch_type, url_or_id, _preload))
            continue

        doi = parsed.get("doi", "") if parsed else ""
        if not doi:
            doi = _resolve_doi(stem)
        is_pmc = publisher == "nih"

        if _quality_ok(parsed):
            # Success — write JSON
            _mark_success(stem)
            json_path = os.path.join(
                os.path.dirname(html_path), f"{stem}.json"
            )
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(parsed, f, indent=2, ensure_ascii=False)
                f.write("\n")
        elif fetch_type == "retry" and _has_metadata(parsed):
            # Retry got metadata but no main_text — try PMC next
            if not is_pmc:
                pmcid = _pmcid_for_doi(doi)
                if pmcid:
                    _record_attempt(stem, "retry: abstract only; pmc scheduled")
                    still_needed.append((html_path, "pmc", pmcid, False))
                    continue
            # Write whatever we have
            json_path = os.path.join(
                os.path.dirname(html_path), f"{stem}.json"
            )
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(parsed, f, indent=2, ensure_ascii=False)
                f.write("\n")
            _record_attempt(stem, "retry: abstract only; no pmc available")
            _log_parse_failure(stem, doi, "retry: abstract only; no pmc available")
        elif fetch_type == "pmc":
            # PMC fetch succeeded but quality failed — retrying same URL
            # won't help. Persist whatever parsed and log.
            if parsed:
                json_path = os.path.join(
                    os.path.dirname(html_path), f"{stem}.json"
                )
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            _record_attempt(stem, "pmc: fallback insufficient")
            _log_parse_failure(stem, doi, "pmc: fallback insufficient")
        else:
            _record_attempt(stem, f"{fetch_type}: no content after fetch")
            still_needed.append((html_path, fetch_type, url_or_id, _preload))

    return still_needed


def _worker_init(journals_path):
    """Initialize a ProcessPoolExecutor worker.

    Seeds the worker's _JOURNALS_FILE with the parent-downloaded temp
    path so ensure_journals() short-circuits (no redownload), and
    builds the journal-abbreviation lookup.
    """
    global _JOURNALS_FILE
    _JOURNALS_FILE = journals_path
    _load_journal_lookup(journals_path)


def main():
    args = sys.argv[1:] or ["papers/"]
    to_process = _collect_paths(args)

    # Download the NLM journal list once before parsing and load the
    # abbreviation lookup. Workers receive the temp file path via
    # _worker_init so they don't each redownload or rebuild.
    journals_path = ensure_journals()
    _load_journal_lookup(journals_path)

    # Phase 1: parse all in parallel (no fetching). Parsers are pure-Python
    # regex work, so use processes to get real concurrency past the GIL.
    fetch_items = []  # (html_path, fetch_type, url_or_pmcid, needs_preload)
    n_workers = os.cpu_count()
    if len(to_process) <= 1 or not n_workers or n_workers <= 1:
        results = [process_html(p) for p in to_process]
    else:
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_worker_init,
            initargs=(journals_path,),
        ) as pool:
            results = list(pool.map(process_html, to_process))

    for result in results:
        # Replay attempts and terminal state into the main-process log.
        stem = result["stem"]
        if result["attempts"]:
            _ATTEMPTS.setdefault(stem, []).extend(result["attempts"])
        terminal = result["terminal"]
        if terminal and terminal[0] == "success":
            _mark_success(stem)
        elif terminal and terminal[0] == "failure":
            _log_parse_failure(stem, result["doi"], terminal[1])

        html_path = result["html_path"]
        needs = result["needs"]
        if needs == "retry":
            with open(html_path, encoding="utf-8") as f:
                html = f.read()
            original_url = detect_url(html)
            doi = result["doi"]
            _aliased = any(a in original_url for a in _DOMAIN_ALIASES)
            # Preload always: resolves redirects for aliased domains and
            # sets session cookies so publishers with rate-limit / WAF
            # challenges (e.g. academic.oup.com 429) don't block single-file.
            needs_preload = True
            url = doi if _aliased else (original_url or doi)
            # Apply URL transforms (e.g. cshlp .long suffix)
            if url:
                for domain_key, transform in _RETRY_URL_TRANSFORMS.items():
                    if domain_key in (original_url or ""):
                        url = transform(url)
                        break
                fetch_items.append((html_path, "retry", url, needs_preload))
        elif needs == "pmc" and result["pmcid"]:
            fetch_items.append((html_path, "pmc", result["pmcid"], False))

    if not fetch_items:
        return

    # Phase 2: fetch in rounds
    round_num = 0
    while fetch_items and round_num < MAX_RETRIES:
        round_num += 1
        proc, port, profile_dir = None, None, None
        try:
            proc, port, profile_dir = start_browser()
            fetch_items = _fetch_round(fetch_items, port)
            stop_browser(proc, profile_dir)
            proc = None
        except Exception as e:
            print(f"  Browser error: {e}", file=sys.stderr, flush=True)
        finally:
            if proc:
                stop_browser(proc, profile_dir)

    # Log remaining failures (each round already recorded the specific
    # fetch cause; terminal log emits the accumulated attempt history).
    if fetch_items:
        for html_path, fetch_type, _, _ in fetch_items:
            stem = os.path.splitext(os.path.basename(html_path))[0]
            doi = _resolve_doi(stem)
            _log_parse_failure(
                stem, doi, f"fetch ({fetch_type}): failed after {MAX_RETRIES} rounds"
            )


if __name__ == "__main__":
    main()
