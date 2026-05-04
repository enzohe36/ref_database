"""Publisher detection and parser dispatch for HTML article pages.

Detects the publisher domain from SingleFile's saved URL comment,
then dynamically imports the corresponding html_parsers/<domain>.py module.
"""

import importlib
import re
from urllib.parse import urlparse

from ._helpers import format_author_name

# Trailing "et al." fragments in freeform citation text that leak into
# the last author name after a naive comma-split ("Huschtscha LI et al"
# as a single author). Stripped from each reference-author string in
# clean_parsed_output before re-normalizing through format_author_name.
_ET_AL_RE = re.compile(r"\s*,?\s*et\s+al\.?\s*$", re.IGNORECASE)


def detect_url(html):
    """Extract the original URL from SingleFile's url comment.

    Returns URL string, or empty string if not found.
    """
    m = re.search(r"url[=:]\s*(?:\(\d+\))?(https?://[^\s>]+)", html[:3000])
    return m.group(1) if m else ""


# Domains that share the same HTML structure and parser.
# When an alias is used, the original URL is unreliable for retry;
# use DOI with preloading to resolve the correct publisher URL.
_DOMAIN_ALIASES = {
    "elsevier": "sciencedirect",
    "springer": "nature",
    "portlandpress": "oup",
    "royalsocietypublishing": "oup",
    "rupress": "oup",
    # science.org and sagepub both run on Atypon Literatum and share the
    # same dc.* meta tags, core-author/core-collateral DOM, and
    # <div id=R/B class=citations> reference markup.
    "sagepub": "science",
    # ashpublications (Blood etc.) uses the same Silverchair article-body /
    # data-content-id=b / ref-list layout as aacrjournals.
    "ashpublications": "aacrjournals",
    # biologists.com (J Cell Sci etc.) uses the same Silverchair article-body
    # and ref-list layout as aacrjournals; refs use content-id=<paperid>cN
    # instead of data-content-id=bN — aacrjournals handles both.
    "biologists": "aacrjournals",
}


def detect_domain(html):
    """Extract second-level domain from SingleFile's url comment.

    SingleFile saves: <!-- saved from url=(XXXX)https://www.nature.com/... -->
    or in the first few lines: url: https://www.nature.com/...

    Returns second-level domain string (e.g. "nature", "oup", "sciencedirect").
    Raises ValueError if no URL found.
    """
    url = detect_url(html)
    if not url:
        raise ValueError("No SingleFile URL comment found in HTML")

    netloc = urlparse(url).netloc
    parts = [p for p in netloc.split(".") if p != "www"]
    sld = parts[-2] if len(parts) >= 2 else parts[0]
    # Replace punctuation with _ for valid Python module names
    sld = re.sub(r"[^A-Za-z0-9]", "_", sld)
    return _DOMAIN_ALIASES.get(sld, sld)



def get_parser(domain):
    """Import and return the parser module for a domain.

    Tries to import journals.<domain>. Raises ValueError if no module found.
    """
    try:
        return importlib.import_module(f"html_parsers.{domain}")
    except ImportError:
        raise ValueError(
            f"No parser module for domain: {domain}. "
            f"Create html_parsers/{domain}.py to add support."
        )


def clean_parsed_output(parsed):
    """Enforce output formatting rules on a parser dict in place.

    - title (main paper and each reference): strip trailing period and
      trailing whitespace.
    - journal (main paper and each reference): strip all dots.
    - author (main paper): strip all dots so initials render as
      "Smith JA", not "Smith J.A." or "Smith J. A.".
    - author (each reference): strip trailing "et al" / "et al.", then
      renormalize through format_author_name so freeform citations that
      leak hyphens or dots into the initials segment ("Paik J-H",
      "Barnes, Ryan P.") come out canonical ("Paik JH", "Barnes RP").
    These rules are applied centrally so individual parsers don't have to
    repeat them. parsed may be None or missing any field; handled safely.
    """
    if not parsed:
        return parsed
    if parsed.get("title"):
        parsed["title"] = parsed["title"].rstrip(".").rstrip()
    if parsed.get("journal"):
        parsed["journal"] = parsed["journal"].replace(".", "")
    for a in parsed.get("authors") or []:
        if isinstance(a, dict) and a.get("author"):
            a["author"] = a["author"].replace(".", "")
    for r in parsed.get("references") or []:
        if not isinstance(r, dict):
            continue
        inner = r.get("") if "" in r else None
        if not isinstance(inner, dict):
            continue
        if inner.get("title"):
            inner["title"] = inner["title"].rstrip(".").rstrip()
        if inner.get("journal"):
            inner["journal"] = inner["journal"].replace(".", "")
        cleaned_authors = []
        for a in inner.get("authors") or []:
            if not isinstance(a, str):
                cleaned_authors.append(a)
                continue
            trimmed = _ET_AL_RE.sub("", a).strip()
            if not trimmed:
                continue
            cleaned_authors.append(format_author_name(trimmed))
        inner["authors"] = cleaned_authors
    return parsed
