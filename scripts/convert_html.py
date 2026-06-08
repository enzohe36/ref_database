#!/usr/bin/env python3
"""Convert article HTML to papers/raw/<stem>_converted.json.

Usage:
    python convert_html.py [<pmid|html|list> ...]

No args: convert every papers/raw/<stem>.html that lacks a corresponding
papers/raw/<stem>_converted.json.

PMID arg: locate papers/raw/<stem>.html (stem looked up via parsed/<stem>.json),
write papers/raw/<stem>_converted.json.

HTML file arg: parse that file, write _converted.json next to it (so files
in papers/test/ produce output in papers/test/).

List arg: file containing PMIDs and/or HTML paths separated by spaces or
newlines. Lines starting with '#' are ignored (comments).
"""

import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor

from html_parsers import (
    clean_parsed_output,
    detect_domain,
    detect_url,
    get_parser,
)
from get_html import (
    start_browser,
    stop_browser,
    apply_publisher_rule,
    CDP_PORT,
    _get_post_capture,
    _has_broken_figures,
    _post_capture_needs_browser,
)
from _cli import parse_argv
from _net import polite_urlopen
from _project import parsed_path, pmid_to_stem, raw_dir, raw_html_path

MIN_MAIN_TEXT_LEN = 5000
MIN_REFERENCES = 5
MAX_RETRIES = 3

# Publisher-specific URL rewrites and wait strategies are centralized in
# get_refs._PUBLISHER_RULES. convert_html.py uses apply_publisher_rule on
# the resolved URL during retry to mirror what get_refs.py does on the
# initial fetch.

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
        # Reject too-small payloads — tiny HTMLs are publisher 403/404 stubs
        # or maintenance pages, not the article. Real fetched articles are
        # tens of KB minimum (the previous 1000-byte threshold let
        # 1174-byte error stubs overwrite the original publisher HTML).
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 50000:
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
        with polite_urlopen(url, timeout=15) as resp:
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
# _abbreviate_journals normalizes to the NLM MedAbbr so the canonical journal
# string is consistent across papers/parsed/<stem>.json.
#
# The NLM journal list is downloaded fresh each invocation and held in
# memory (no on-disk cache). The parsed dict is shared with parallel workers
# through the ProcessPoolExecutor initializer so they don't each re-parse.

_JOURNALS_URL = "https://ftp.ncbi.nlm.nih.gov/pubmed/J_Entrez.txt"
_JOURNAL_LOOKUP = None
# Prefix index: tuple(normalized paren-stripped JournalTitle tokens[:n]) ->
# abbr, populated only when exactly one NLM entry registers that prefix.
# Min prefix length = 3 tokens (see _PREFIX_MIN_TOKENS).
_JOURNAL_PREFIX_INDEX = None
_PREFIX_MIN_TOKENS = 3


def ensure_journals():
    """Download the NLM journal list and return {NlmId: {JournalTitle, MedAbbr}}.

    No on-disk cache: every invocation re-downloads _JOURNALS_URL and parses
    the J_Entrez flat text in memory.
    """
    print(f"Downloading {_JOURNALS_URL}...", flush=True)
    with polite_urlopen(_JOURNALS_URL, timeout=60) as resp:
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

    print(f"Loaded {len(journal_map)} journals")
    return journal_map


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


def _load_journal_lookup(jdata):
    """Populate the module-level lookup and prefix index from an in-memory
    {NlmId: {JournalTitle, MedAbbr}} dict (as returned by ensure_journals()).

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
    """Emit per-stem log of terminal failure (no sidecar log file in new design)."""
    _emit_stem_log(stem, reason)


# ---------------------------------------------------------------------------
# Process one HTML file
# ---------------------------------------------------------------------------

def _resolve_doi(stem):
    """Look up DOI from papers/parsed/<stem>.json by stem."""
    pjson = parsed_path(stem)
    if not pjson.exists():
        return ""
    try:
        with open(pjson, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("doi", "")
    except (json.JSONDecodeError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Schema transformation: parser output -> _converted.json
# ---------------------------------------------------------------------------

CONVERTED_TOP_KEYS = [
    "stem", "pmid", "doi", "title", "journal", "year", "volume", "issue",
    "pages", "authors", "publication_types", "main_text", "references",
]

CONVERTED_REF_KEYS = [
    "pmid", "doi", "title", "journal", "year", "volume", "issue", "pages",
    "authors",
]


def _converted_ref_from(item):
    """Transform one entry of parser-output references into the locked _converted ref schema.

    Parser emits each ref as `{"<pmid_or_empty>": {bib fields}}`. The single
    key is "" for unresolved refs or a PMID string when already resolved.
    Output: flat object with explicit pmid field plus the bib fields.
    """
    out = {k: ("" if k != "authors" else []) for k in CONVERTED_REF_KEYS}
    if not isinstance(item, dict):
        return out
    if len(item) == 1:
        key = next(iter(item))
        inner = item[key] if isinstance(item[key], dict) else {}
        out["pmid"] = key if key else ""
    else:
        # Already in flat form — read keys directly.
        inner = item
        out["pmid"] = item.get("pmid", "") or ""
    for k in CONVERTED_REF_KEYS:
        if k == "pmid":
            continue
        v = inner.get(k)
        if k == "authors":
            out[k] = v if isinstance(v, list) else []
        else:
            out[k] = v if v else ""
    return out


def _build_converted(parsed):
    """Wrap parser output (papers/*.json shape) into the _converted.json schema."""
    refs_in = parsed.get("references") or []
    refs_out = [_converted_ref_from(r) for r in refs_in]
    return {
        "stem": "",
        "pmid": "",
        "doi": parsed.get("doi", "") or "",
        "title": parsed.get("title", "") or "",
        "journal": parsed.get("journal", "") or "",
        "year": parsed.get("year", "") or "",
        "volume": parsed.get("volume", "") or "",
        "issue": parsed.get("issue", "") or "",
        "pages": parsed.get("pages", "") or "",
        "authors": parsed.get("authors") or [],
        "publication_types": [],
        "main_text": parsed.get("main_text", "") or "",
        "references": refs_out,
    }


def _converted_path(html_path):
    """Map an html_path to its _converted.json sidecar (same directory)."""
    stem = os.path.splitext(os.path.basename(html_path))[0]
    return os.path.join(os.path.dirname(html_path), f"{stem}_converted.json")


def _pmc_html_path(html_path):
    """Map an html_path to its <stem>_pmc.html sidecar.

    The PMC fallback writes its captured page here instead of overwriting
    the publisher's HTML — this keeps publisher-side metadata (authors,
    journal-specific volume/issue/pages, doi) intact when the PMC page
    is, e.g., an old scanned-PDF stub that drops them.
    """
    stem = os.path.splitext(os.path.basename(html_path))[0]
    return os.path.join(os.path.dirname(html_path), f"{stem}_pmc.html")


def _main_text_quality_ok(parsed):
    """True iff parsed['main_text'] passes the per-field threshold."""
    if not parsed:
        return False
    return len(parsed.get("main_text", "") or "") >= MIN_MAIN_TEXT_LEN


def _references_quality_ok(parsed):
    """True iff parsed['references'] passes the per-field threshold."""
    if not parsed:
        return False
    return len(parsed.get("references") or []) >= MIN_REFERENCES


def _merge_pmc_into_converted(json_path, pmc_parsed):
    """Merge PMC-side `main_text` and `references` into the publisher-
    side _converted.json, leaving every other key untouched.

    Replacement rule (per-field):
      - main_text: replace with PMC's only when PMC's main_text passes
        the per-field threshold (so we don't downgrade good publisher
        text with a worse PMC stub).
      - references: replace with PMC's only when PMC's reference list
        passes the per-field threshold AND it has at least as many
        entries as the existing publisher list (avoids overwriting a
        well-extracted publisher list with a shorter PMC one).

    Other keys (title / doi / journal / year / volume / issue / pages /
    authors / publication_types) stay as the publisher parser produced
    them. Returns the merged dict (also written to disk).

    If json_path doesn't exist (publisher parse produced nothing), the
    PMC parse is written as the entire result so the fixture isn't
    silently dropped.
    """
    pmc_built = _build_converted(pmc_parsed) if pmc_parsed else None

    if not os.path.exists(json_path):
        # Phase 1 publisher parse produced nothing — fall back to PMC
        # alone, matching the legacy behaviour for that case.
        if pmc_built is None:
            return None
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(pmc_built, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return pmc_built

    try:
        with open(json_path, encoding="utf-8") as f:
            existing = json.load(f)
    except (json.JSONDecodeError, OSError):
        existing = None

    if existing is None:
        if pmc_built is None:
            return None
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(pmc_built, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return pmc_built

    if pmc_parsed is None:
        return existing

    merged = dict(existing)
    pmc_main_text = pmc_parsed.get("main_text", "") or ""
    if (
        len(pmc_main_text) >= MIN_MAIN_TEXT_LEN
        and len(pmc_main_text) > len(existing.get("main_text", "") or "")
    ):
        merged["main_text"] = pmc_main_text

    pmc_refs = pmc_parsed.get("references") or []
    pub_refs = existing.get("references") or []
    if len(pmc_refs) >= MIN_REFERENCES and len(pmc_refs) >= len(pub_refs):
        # Re-shape PMC refs through the same _converted ref schema the
        # publisher refs use — ensures key order and typing match.
        merged["references"] = [_converted_ref_from(r) for r in pmc_refs]

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return merged


def _heal_broken_figures(html_path, html):
    """Re-fetch broken figures via the matching publisher's post_capture hook.

    Generic check (`_has_broken_figures`) gates the call: only fires when
    the saved HTML still contains `<img src=data:,>` placeholders left over
    from SingleFile fetches that failed during capture. Healed HTML is
    written back to `html_path` by the publisher hook itself; this returns
    the freshly-loaded contents so downstream parsing sees the heal.

    When the matched publisher's post_capture hook needs CDP (ACS / BMJ /
    IUCR — they open same-origin tabs to bypass Cloudflare), start a
    browser around the call and pass the live port. Hooks that only call
    urllib (cshlp, plos, nature, ...) get `port=None`.

    Returns the HTML, healed-or-unchanged. Never raises -- a hook failure
    leaves the placeholders in place and the parser sees the broken
    figures, matching the pre-heal status quo.
    """
    n = _has_broken_figures(html)
    if not n:
        return html
    url = detect_url(html) or ""
    if not url:
        return html
    post = _get_post_capture(url)
    if not post:
        return html
    print(
        f"  {os.path.basename(html_path)}: {n} broken figure(s); "
        f"calling heal hook",
        flush=True,
    )
    needs_browser = _post_capture_needs_browser(url)
    proc = None
    profile_dir = None
    port = None
    if needs_browser:
        try:
            proc, port, profile_dir = start_browser()
        except Exception as e:
            print(
                f"    heal hook skipped: browser failed to start "
                f"({type(e).__name__}: {e})",
                flush=True,
            )
            return html
    try:
        try:
            post(html_path, port)
        except Exception as e:
            print(
                f"    heal hook failed: {type(e).__name__}: {e}",
                flush=True,
            )
            return html
        try:
            with open(html_path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            return html
    finally:
        if proc is not None:
            stop_browser(proc, profile_dir)


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

    # Heal broken figures (publisher's post_capture hook re-fetches any
    # `<img src=data:,>` placeholders left by failed inline fetches during
    # capture). No-op when no broken images are present.
    html = _heal_broken_figures(html_path, html)

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
    json_path = _converted_path(html_path)

    # Determine if fetching is needed
    original_url = detect_url(html) or ""
    # A transform is "available" when the rule sheet would rewrite the
    # saved URL to a different target — typically an abstract-to-fulltext
    # upgrade (cshlp .long, biorxiv .full) or a publisher swap
    # (ClinicalKey/linkinghub -> ScienceDirect). Forces a retry past the
    # PMC fallback because the upgraded URL is likely to satisfy the
    # quality gate where the abstract URL could not.
    _transformed_url, _, _ = apply_publisher_rule(original_url)
    _needs_transform = bool(original_url) and _transformed_url != original_url
    needs = None
    pmcid = None
    if not is_pmc and not _quality_ok(parsed) and doi:
        if not _has_metadata(parsed) or _needs_transform:
            needs = "retry"
        else:
            pmcid = _pmcid_for_doi(doi)
            if pmcid:
                needs = "pmc"

    # Write whatever we have (transformed to _converted.json schema)
    if parsed:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(_build_converted(parsed), f, indent=2, ensure_ascii=False)
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
    """Skip if _converted.json already exists and is high quality."""
    json_path = _converted_path(html_path)
    if not os.path.exists(json_path):
        return False
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    return _quality_ok(data)


def _collect_html_paths(pmids, html_paths):
    """Resolve PMID inputs to papers/raw/<stem>.html paths and merge with explicit
    HTML inputs. Filters out paths whose _converted.json already passes quality.
    """
    paths = []
    seen = set()

    for pmid in pmids:
        stem = pmid_to_stem(pmid)
        if not stem:
            print(f"PMID {pmid}: no papers/parsed/<stem>.json (run get_refs.py first)",
                  file=sys.stderr)
            continue
        hp = str(raw_html_path(stem))
        if not os.path.exists(hp):
            print(f"{stem}: no papers/raw/<stem>.html (run get_html.py first)",
                  file=sys.stderr)
            continue
        if hp in seen:
            continue
        seen.add(hp)
        paths.append(hp)

    for hp in html_paths:
        if not os.path.exists(hp):
            print(f"Not found: {hp}", file=sys.stderr)
            continue
        if hp in seen:
            continue
        seen.add(hp)
        paths.append(hp)

    return [p for p in paths if not _should_skip(p)]


def _default_scan():
    """No-arg default: papers/raw/*.html lacking a corresponding _converted.json."""
    rd = raw_dir()
    if not rd.exists():
        return []
    out = []
    for p in sorted(rd.glob("*.html")):
        if _should_skip(str(p)):
            continue
        out.append(str(p))
    return out


def _fetch_round(fetch_items, port):
    """Run one round of fetch requests with 1s delay between each.

    Each item is (html_path, fetch_type, url_or_pmcid, needs_preload).
    Items needing preload: open tabs sequentially, wait, resolve URLs.
    Direct items: capture immediately with single-file.
    All captures run in parallel.
    Returns list of items that still need fetching.
    """
    from get_html import (
        _cdp_open_tab,
        _cdp_close_tab,
        apply_publisher_rule,
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
            # Apply publisher rule sheet to rewrite the resolved URL before
            # capture (e.g. cshlp .long, biorxiv .full,
            # ClinicalKey/linkinghub -> ScienceDirect). Must come AFTER the
            # redirect chain has settled, i.e. after PAGE_LOAD_WAIT.
            resolved, _, _ = apply_publisher_rule(resolved)
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
            # Wait strategy + delay from publisher rule sheet.
            _, wait, wait_delay = apply_publisher_rule(
                resolved_url, default_wait="networkIdle",
            )
            try:
                subprocess.run(
                    [
                        "single-file",
                        "--browser-server",
                        f"http://localhost:{port}",
                        f"--browser-wait-until={wait}",
                        f"--browser-wait-delay={wait_delay}",
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

    for i, (html_path, fetch_type, _, _) in enumerate(fetch_items):
        # PMC fetches must NEVER overwrite the publisher HTML — write
        # PMC content to <stem>_pmc.html and merge per-field into the
        # already-written publisher _converted.json. Retry fetches keep
        # the legacy behaviour of writing back to <stem>.html (the
        # captured publisher page is the one we want to upgrade).
        out_path = (
            _pmc_html_path(html_path) if fetch_type == "pmc" else html_path
        )
        task_queue.put((i, resolved_urls[i], out_path))
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

        # Decide which file we just wrote. PMC fetches go to the
        # <stem>_pmc.html sidecar; retry fetches to the original
        # <stem>.html (upgraded publisher capture).
        parse_path = (
            _pmc_html_path(html_path) if fetch_type == "pmc" else html_path
        )

        # Re-parse
        try:
            with open(parse_path, encoding="utf-8") as f:
                html = f.read()
            publisher = detect_domain(html)
            parser = get_parser(publisher)
            html = _heal_broken_figures(parse_path, html)
            cleaned = parser.remove_banners(html)
            if cleaned != html:
                with open(parse_path, "w", encoding="utf-8") as f:
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
        json_path = _converted_path(html_path)

        if fetch_type == "pmc":
            # Merge PMC's main_text / references into the publisher-side
            # _converted.json (publisher metadata stays intact even when
            # PMC's content wins). When publisher Phase 1 produced
            # nothing, the merge writes the PMC parse alone.
            if not _has_metadata(parsed):
                _record_attempt(stem, "pmc: no metadata; retrying")
                still_needed.append((html_path, fetch_type, url_or_id, _preload))
                continue
            merged = _merge_pmc_into_converted(json_path, parsed)
            if merged and _quality_ok(merged):
                _mark_success(stem)
            else:
                _record_attempt(stem, "pmc: fallback insufficient")
                _log_parse_failure(stem, doi, "pmc: fallback insufficient")
        elif _quality_ok(parsed):
            _mark_success(stem)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(_build_converted(parsed), f, indent=2, ensure_ascii=False)
                f.write("\n")
        elif fetch_type == "retry" and _has_metadata(parsed):
            # Publisher refetch came back with metadata but still
            # below the quality bar — write what we have so far, then
            # try PMC fallback (it merges into this JSON next round).
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(_build_converted(parsed), f, indent=2, ensure_ascii=False)
                f.write("\n")
            if not is_pmc:
                pmcid = _pmcid_for_doi(doi)
                if pmcid:
                    _record_attempt(stem, "retry: abstract only; pmc scheduled")
                    still_needed.append((html_path, "pmc", pmcid, False))
                    continue
            _record_attempt(stem, "retry: abstract only; no pmc available")
            _log_parse_failure(stem, doi, "retry: abstract only; no pmc available")
        else:
            _record_attempt(stem, f"{fetch_type}: no content after fetch")
            still_needed.append((html_path, fetch_type, url_or_id, _preload))

    return still_needed


def _worker_init(jdata):
    """Initialize a ProcessPoolExecutor worker by loading the journal lookup
    from the in-memory dict pickled by the parent."""
    _load_journal_lookup(jdata)


def main():
    if not sys.argv[1:]:
        to_process = _default_scan()
    else:
        parsed = parse_argv(accept={"pmids", "htmls"})
        to_process = _collect_html_paths(parsed["pmids"], parsed["htmls"])

    if not to_process:
        return

    # Download the NLM journal list (in-memory only) and load the lookup.
    # Workers receive the parsed dict via _worker_init so they don't each
    # re-download.
    jdata = ensure_journals()
    _load_journal_lookup(jdata)

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
            initargs=(jdata,),
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
            # Preload always: sets session cookies so publishers with
            # rate-limit / WAF challenges (e.g. academic.oup.com 429)
            # don't block single-file.
            needs_preload = True
            url = original_url or doi
            # Publisher rule sheet (get_refs._PUBLISHER_RULES) is applied
            # inside _fetch_round after preload resolves the redirect chain,
            # so no transform is applied here.
            if url:
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
