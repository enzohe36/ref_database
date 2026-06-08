"""Shared CLI argument parser used by get_refs.py, get_html.py, convert_html.py,
get_pmid.py, merge_refs.py.

Tokenizes positional args into:
  - PMIDs (digits-only token)
  - URLs (token starting with http)
  - .html paths
  - .json paths
  - lists (anything else): paths to list-files whose contents are re-tokenized.
    Inside a list-file, lines whose first non-whitespace character is '#' are
    ignored (comment lines).

Each script declares which token types it accepts via the `accept` set.
"""

import re
import sys
from pathlib import Path


def _strip_comment_lines(text):
    """Drop lines whose first non-whitespace character is '#'."""
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _classify(token):
    """Return ('pmid'|'url'|'html'|'json'|'list', token)."""
    if re.fullmatch(r"\d+", token):
        return ("pmid", token)
    if token.startswith("http://") or token.startswith("https://"):
        return ("url", token)
    if token.endswith(".html"):
        return ("html", token)
    if token.endswith(".json"):
        return ("json", token)
    return ("list", token)


def parse_tokens(tokens, accept):
    """Tokenize a flat list (CLI args or file lines) into typed buckets.

    accept: set of strings from {"pmids", "urls", "htmls", "jsons"}.
    Returns dict with keys "pmids", "urls", "htmls", "jsons" (lists, deduped, in
    first-seen order). Tokens with disallowed types raise SystemExit.
    """
    pmids, urls, htmls, jsons = [], [], [], []
    pmid_seen, url_seen, html_seen, json_seen = set(), set(), set(), set()

    queue = list(tokens)
    while queue:
        tok = queue.pop(0).strip()
        if not tok:
            continue
        kind, value = _classify(tok)
        if kind == "pmid":
            if "pmids" not in accept:
                _reject(tok, "PMID", accept)
            if value not in pmid_seen:
                pmid_seen.add(value)
                pmids.append(value)
        elif kind == "url":
            if "urls" not in accept:
                _reject(tok, "URL", accept)
            if value not in url_seen:
                url_seen.add(value)
                urls.append(value)
        elif kind == "html":
            if "htmls" not in accept:
                _reject(tok, "HTML file", accept)
            ap = str(Path(value).resolve())
            if ap not in html_seen:
                html_seen.add(ap)
                htmls.append(ap)
        elif kind == "json":
            if "jsons" not in accept:
                _reject(tok, "JSON file", accept)
            ap = str(Path(value).resolve())
            if ap not in json_seen:
                json_seen.add(ap)
                jsons.append(ap)
        else:
            # list: read file and re-tokenize its content (comment lines stripped)
            path = Path(value)
            if not path.exists():
                print(f"path not found: {value}", file=sys.stderr)
                sys.exit(1)
            content = path.read_text(encoding="utf-8")
            content = _strip_comment_lines(content)
            queue = re.split(r"\s+", content) + queue

    return {"pmids": pmids, "urls": urls, "htmls": htmls, "jsons": jsons}


def _reject(token, kind, accept):
    accepted_msg = ", ".join(sorted(accept))
    print(
        f"unsupported argument {token!r} ({kind}). this script accepts: {accepted_msg}",
        file=sys.stderr,
    )
    sys.exit(1)


def parse_argv(accept):
    """Parse sys.argv[1:] with the given accept set. Empty argv returns empty buckets."""
    return parse_tokens(sys.argv[1:], accept)
