"""Shared helpers for HTML parsing across publishers."""

import re
import unicodedata
from html import unescape


def tags_to_text(html):
    """Convert HTML fragment to plain text preserving structure."""
    # Headers
    html = re.sub(
        r"<h[1-4][^>]*>(.*?)</h[1-4]>",
        lambda m: "\n\n## " + re.sub(r"<[^>]+>", "", m.group(1)).strip() + "\n",
        html,
        flags=re.DOTALL,
    )
    # Sup/sub
    html = re.sub(r"<sup>(.*?)</sup>", r"[\1]", html, flags=re.DOTALL)
    html = re.sub(r"<sub>(.*?)</sub>", r"[\1]", html, flags=re.DOTALL)
    # Tables: convert cells to ". "-separated values, rows to newlines
    html = re.sub(r"<t[hd][^>]*>", ". ", html)
    html = re.sub(r"<tr[^>]*>", "\n", html)
    html = re.sub(r"</t(?:able|head|body|r|h|d)>", "", html)
    html = re.sub(r"<t(?:able|head|body)[^>]*>", "", html)
    # Block elements
    html = re.sub(r"<p[^>]*>", "\n", html)
    html = re.sub(r"</p>", "\n", html)
    html = re.sub(r"<div[^>]*>", "\n", html)
    html = re.sub(r"</div>", "\n", html)
    html = re.sub(r"<br\s*/?>", "\n", html)
    html = re.sub(r"<li[^>]*>", "\n", html)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    html = unescape(html)
    # Collapse whitespace: join single newlines (hard-wrapped source lines)
    # into paragraphs, preserve blank lines as paragraph breaks.
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in html.split("\n")]
    paragraphs = []
    current = []
    for l in lines:
        if l == "":
            if current:
                paragraphs.append(" ".join(current))
                current = []
            paragraphs.append("")
        else:
            current.append(l)
    if current:
        paragraphs.append(" ".join(current))
    # Collapse consecutive blank lines
    out = []
    prev_blank = False
    for p in paragraphs:
        if p == "":
            if not prev_blank:
                out.append("")
            prev_blank = True
        else:
            prev_blank = False
            out.append(p)
    return "\n".join(out).strip()


def extract_captions(html):
    """Replace <figure> elements and table wrappers with their caption text.

    Extracts caption text from known caption containers, removes images
    and non-caption content, then replaces the element with plain text.

    Call before strip_common to preserve captions in main_text output.
    """
    def _strip_tags_only(html_fragment):
        """Strip HTML tags without unescaping entities.

        Unlike strip_tags, keeps entities like &lt; intact so they don't
        interfere with HTML processing when re-inserted into the document.
        tags_to_text calls unescape at the end.
        """
        return re.sub(r"<[^>]+>", "", html_fragment)

    def _extract_from(content):
        """Extract caption text from figure/table element content."""
        # Preserve <table> elements (will be converted by tags_to_text later)
        tables = re.findall(r"<table[^>]*>.*?</table>", content, re.DOTALL)
        # Preserve table footnotes (role=doc-footnote)
        footnotes = []
        for fm in re.finditer(
            r'<div[^>]*role=["\']?doc-footnote["\']?[^>]*>(.*?)</div>',
            content, re.DOTALL,
        ):
            text = _strip_tags_only(fm.group(1)).strip()
            if text:
                footnotes.append(text)
        # Remove images, download links, and SVGs
        clean = re.sub(r"<img[^>]*/?>", "", content)
        clean = re.sub(r"<svg[^>]*>.*?</svg>", "", clean, flags=re.DOTALL)
        clean = re.sub(r"<picture[^>]*>.*?</picture>", "", clean, flags=re.DOTALL)
        clean = re.sub(r"<ol[^>]*>.*?</ol>", "", clean, flags=re.DOTALL)

        # Extract text from caption containers
        parts = []
        # Headings inside figures (PMC obj_head: h2/h3/h4 figure titles)
        for cm in re.finditer(
            r"<h[2-4][^>]*>(.*?)</h[2-4]>", clean, re.DOTALL
        ):
            text = _strip_tags_only(cm.group(1)).strip()
            if text:
                parts.append(text)
        # figcaption (Nature short title)
        for cm in re.finditer(
            r"<figcaption[^>]*>(.*?)</figcaption>", clean, re.DOTALL
        ):
            text = _strip_tags_only(cm.group(1)).strip()
            if text:
                parts.append(text)
        # Nature full description: id=figure-N-desc or class contains figure-description
        for cm in re.finditer(
            r"<div[^>]*(?:figure-description|figure-\d+-desc)[^>]*>",
            clean,
        ):
            # Use _remove_nested_element logic to find matching </div>
            pos = cm.end()
            depth = 1
            while depth > 0 and pos < len(clean):
                next_open = re.search(r"<div[\s>]", clean[pos:])
                next_close = re.search(r"</div>", clean[pos:])
                if next_close is None:
                    break
                if next_open and next_open.start() < next_close.start():
                    depth += 1
                    pos += next_open.end()
                else:
                    depth -= 1
                    if depth == 0:
                        desc_html = clean[cm.end():pos + next_close.start()]
                        text = _strip_tags_only(desc_html).strip()
                        text = re.sub(r"\s+", " ", text)
                        if text:
                            parts.append(text)
                    pos += next_close.end()
        # ScienceDirect/PMC captions: <span class=captions or class="captions ..."
        # Use nesting-aware extraction since caption spans contain nested spans
        for cm in re.finditer(
            r'<span[^>]*class="?captions[^>]*>', clean,
        ):
            pos = cm.end()
            depth = 1
            while depth > 0 and pos < len(clean):
                next_open = re.search(r"<span[\s>]", clean[pos:])
                next_close = re.search(r"</span>", clean[pos:])
                if next_close is None:
                    break
                if next_open and next_open.start() < next_close.start():
                    depth += 1
                    pos += next_open.end()
                else:
                    depth -= 1
                    if depth == 0:
                        cap_html = clean[cm.end():pos + next_close.start()]
                        text = _strip_tags_only(cap_html).strip()
                        text = re.sub(r"\s+", " ", text)
                        if text:
                            parts.append(text)
                    pos += next_close.end()
        result = "\n".join(parts)
        if tables:
            result += "\n" + "\n".join(tables)
        if footnotes:
            result += "\n" + "\n".join(footnotes)
        return result

    # Replace <figure> elements
    html = re.sub(
        r"<figure[^>]*>(.*?)</figure>",
        lambda m: _extract_from(m.group(1)),
        html, flags=re.DOTALL,
    )

    # Table wrappers are left in place — tags_to_text handles them.
    # No replacement needed; caption + table data flow through naturally.

    return html


def strip_common(html):
    """Remove hidden divs, images, image maps, style/script blocks."""
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    html = re.sub(
        r'<div hidden="hidden"[^>]*>.*?</div>\s*</div>', "", html, flags=re.DOTALL
    )
    # Remove data: URIs that may contain angle brackets (SingleFile SVG)
    # before the img regex, which breaks on > inside attribute values.
    html = re.sub(r"""src='data:[^']*'""", "", html)
    html = re.sub(r'src="data:[^"]*"', "", html)
    html = re.sub(r"<img[^>]*/?>", "", html)
    html = re.sub(r"<map[^>]*>.*?</map>", "", html, flags=re.DOTALL)
    return html


def drop_noise(text, noise_prefixes):
    """Drop lines starting with any of the given prefixes, collapse blank lines."""
    lines = text.split("\n")
    out = []
    prev_blank = False
    for raw in lines:
        line = raw.strip()
        if any(line.startswith(n) for n in noise_prefixes):
            continue
        if line == "":
            if not prev_blank:
                out.append("")
            prev_blank = True
        else:
            prev_blank = False
            out.append(line)
    return "\n".join(out).strip()


def strip_tags(html):
    """Remove all HTML tags and decode entities."""
    return unescape(re.sub(r"<[^>]+>", "", html))


def _remove_nested_element(html, start_pattern):
    """Remove an element matching start_pattern, handling nested tags of the same type."""
    m = re.search(start_pattern, html, re.DOTALL)
    if not m:
        return html
    # Determine the tag name
    tag_m = re.match(r"<(\w+)", m.group())
    if not tag_m:
        return html
    tag = tag_m.group(1)
    # Walk forward, counting open/close tags to find the matching close
    pos = m.end()
    depth = 1
    open_pat = re.compile(rf"<{tag}[\s>]", re.IGNORECASE)
    close_pat = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
    while depth > 0 and pos < len(html):
        next_open = open_pat.search(html, pos)
        next_close = close_pat.search(html, pos)
        if next_close is None:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            pos = next_close.end()
    return html[:m.start()] + html[pos:]


def remove_elements_by_id(html, *ids):
    """Remove HTML elements by id. Handles nested content."""
    for eid in ids:
        html = _remove_nested_element(
            html, rf'<\w+[^>]*\bid=["\']?{re.escape(eid)}["\']?[^>]*>'
        )
    return html


def remove_elements_by_selector(html, *selectors):
    """Remove elements matching class/id substrings like 'cookie-banner'."""
    for sel in selectors:
        html = _remove_nested_element(
            html, rf'<div[^>]*(?:class|id)="[^"]*{re.escape(sel)}[^"]*"[^>]*>'
        )
    return html


def _meta_content_pattern(name):
    """Build regex patterns for <meta name=X content=Y> in both orders,
    handling quoted and unquoted attribute values, and other attributes
    interleaved between name and content (e.g. scheme=WTN8601)."""
    esc = re.escape(name)
    # content value: quoted or unquoted (ends at space or >)
    val_q = r'["\']([^"\']*)["\']'
    val_u = r'([^\s>]+)'
    # name can be quoted or unquoted; require word boundary at end so
    # "dc.Date" doesn't also match "dc.DateAccepted".
    name_pat = rf'["\']?{esc}\b["\']?'
    # Inter-attribute separator: any chars not breaking out of the tag
    sep = r'[^>]*?\s'
    return [
        # name then content, quoted
        rf'<meta[^>]*name={name_pat}{sep}content={val_q}',
        # content then name, quoted
        rf'<meta[^>]*content={val_q}{sep}name={name_pat}',
        # name then content, unquoted
        rf'<meta[^>]*name={name_pat}{sep}content={val_u}',
        # content then name, unquoted
        rf'<meta[^>]*content={val_u}{sep}name={name_pat}',
    ]


def get_meta(html, name):
    """Get content of a <meta> tag by name. Returns first match or ''."""
    for pat in _meta_content_pattern(name):
        m = re.search(pat, html)
        if m:
            return unescape(m.group(1))
    return ""


def get_all_meta(html, name):
    """Get all content values of <meta> tags by name.

    Only uses quoted-value patterns to avoid false matches from
    unquoted patterns re-matching inside quoted attribute values.
    """
    esc = re.escape(name)
    name_pat = rf'["\']?{esc}["\']?'
    val_q = r'["\']([^"\']*)["\']'
    patterns = [
        rf'<meta[^>]*name={name_pat}\s+content={val_q}',
        rf'<meta[^>]*content={val_q}[^>]*name={name_pat}',
    ]
    values = []
    seen = set()
    for pat in patterns:
        for m in re.finditer(pat, html):
            val = unescape(m.group(1))
            if val not in seen:
                seen.add(val)
                values.append(val)
    return values


def _normalize_unicode(text):
    """Normalize problematic Unicode in author names.

    Replaces curly quotes and thin spaces with ASCII equivalents.
    Preserves Latin diacritics (PubMed retains them in author names).
    """
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2009", " ").replace("\u00a0", " ")
    return text


# Compound-surname prefixes. When a token matching one of these
# precedes the surname, it is absorbed into the surname
# (e.g. "de Lange" -> surname "de Lange", not "Lange").
# Case-insensitive match; apostrophe-prefixed tokens (d'Adda, o'Brien)
# are handled by the leading-lowercase rule instead.
_SURNAME_PREFIXES = frozenset({
    "de", "del", "della", "di", "da", "das", "do", "dos", "du",
    "la", "le", "les",
    "van", "von", "der", "den", "dem", "des", "ter", "te", "ten",
    "el", "al", "bin", "ibn", "ben",
    "mac", "mc", "nick",
})

# Generational suffixes and titles stripped from the tail of combined
# names before surname detection ("JR Yates III" -> surname "Yates",
# "Smith Jr." -> surname "Smith").
_NAME_SUFFIXES = frozenset({
    "jr", "sr", "ii", "iii", "iv", "v",
    "phd", "md", "dr",
})

# Characters that separate initial-bearing tokens within a given name:
# whitespace, ASCII hyphen, Unicode hyphens (U+2010–U+2013), periods.
_GIVEN_SPLIT_RE = re.compile(r"[\s.\-\u2010\u2011\u2012\u2013]+")


def _is_initials_token(tok):
    """True if tok looks like initials: all-upper letters, optional dots
    or hyphens, 1-4 chars. Handles 'JD', 'J.D.', 'J-H', 'J.-H.'.
    """
    stripped = re.sub(r"[.\-\u2010\u2011\u2012\u2013]", "", tok)
    return bool(stripped) and stripped.isalpha() and stripped.isupper() and 1 <= len(stripped) <= 4


def _is_surname_prefix(tok):
    """True if tok is a known lowercase surname prefix, or a lowercase-starting
    token (Dutch/apostrophe compounds like d'Adda)."""
    lowered = tok.lower().rstrip(".")
    if lowered in _SURNAME_PREFIXES:
        return True
    # Apostrophe compounds: d'Adda, o'Brien, l'Heureux — the leading
    # lowercase letter + apostrophe marks a compound that attaches left.
    if len(tok) >= 2 and tok[0].islower() and tok[1] == "'":
        return True
    return False


def format_name(given, surname):
    """Build canonical 'Surname IN' from an explicit given/surname pair.

    This is the only function in the codebase that builds initials.
    Given is split on whitespace, ASCII hyphen, Unicode hyphens
    (U+2010–U+2013), and periods; the first alphabetic character of
    each part contributes one initial.

    Examples:
      format_name("Jean-Baptiste", "Boulé")      -> "Boulé JB"
      format_name("J.-B.", "Boulé")              -> "Boulé JB"
      format_name("Ryan P.", "Barnes")           -> "Barnes RP"
      format_name("Mariarosaria", "de Rosa")     -> "de Rosa M"
      format_name("", "Smith")                   -> "Smith"
      format_name("JA", "Smith")                 -> "Smith JA"
    """
    given = _normalize_unicode(given or "").strip()
    surname = _normalize_unicode(surname or "").strip().rstrip(",")
    if not given:
        return surname

    # Whitespace-separated tokens only count when they start with a
    # capital (so surname particles like 'de'/'van' in 'Mary de Rosa'
    # don't leak in). Within a token, hyphen- or period-separated
    # subparts all contribute a first-letter initial regardless of case
    # — hyphenated Chinese-style names like 'Shiou-chi' or 'Kei-ichi'
    # are two-syllable compounds where both syllables are given-name
    # parts, and the second syllable's case is just a romanization
    # choice that shouldn't drop an initial.
    initials = ""
    for token in given.split():
        if not token or not token[0].isupper():
            continue
        compact = token.replace(".", "").replace("-", "")
        for u in ("\u2010", "\u2011", "\u2012", "\u2013"):
            compact = compact.replace(u, "")
        if compact.isalpha() and compact.isupper() and len(compact) <= 4:
            # Already-compact initials like 'JA' / 'J.A.' / 'J-A'.
            initials += compact
            continue
        for subpart in _GIVEN_SPLIT_RE.split(token):
            if subpart and subpart[0].isalpha():
                initials += subpart[0].upper()
    if not initials:
        return surname
    if not surname:
        return initials
    return f"{surname} {initials}"


def parse_combined_name(name):
    """Split a combined author-name string into (given, surname).

    Handles three input shapes:
      - 'Last, Given'      -> unambiguous comma split
      - 'Initials Last'    -> leading all-upper short token(s) are initials
                              (e.g. 'JD Griffith', 'J.D. Griffith')
      - 'Given Last'       -> default; surname is the trailing run of
                              tokens, extended left while the preceding
                              token is a known surname prefix (de, van,
                              nick, d', etc.)

    Trailing suffixes (Jr., III, PhD, ...) are stripped before surname
    detection. Returns ('', '') for empty input, ('', name) for a
    single-token input.

    Examples:
      parse_combined_name("Barnes, Ryan P.")       -> ("Ryan P.", "Barnes")
      parse_combined_name("Jean-Baptiste Boulé")   -> ("Jean-Baptiste", "Boulé")
      parse_combined_name("Titia de Lange")        -> ("Titia", "de Lange")
      parse_combined_name("Aziz El Hage")          -> ("Aziz", "El Hage")
      parse_combined_name("Scott A. Nick McElhinny") -> ("Scott A.", "Nick McElhinny")
      parse_combined_name("Fabrizio d'Adda di Fagagna") -> ("Fabrizio", "d'Adda di Fagagna")
      parse_combined_name("JD Griffith")           -> ("JD", "Griffith")
      parse_combined_name("JR Yates III")          -> ("JR", "Yates")
    """
    if not name:
        return ("", "")
    name = _normalize_unicode(name).strip().strip(",").strip()
    if not name:
        return ("", "")

    # Shape 1: 'Last, Given' — comma is unambiguous. Dutch/Portuguese
    # convention trails the surname prefix on the given side (e.g.
    # 'Lange, Titia de'); absorb trailing prefix tokens back into the
    # surname so the result is ('Titia', 'de Lange').
    if "," in name:
        last, given = name.split(",", 1)
        last = last.strip()
        given_tokens = given.strip().split()
        while given_tokens and _is_surname_prefix(given_tokens[-1]):
            last = given_tokens.pop() + " " + last
        return (" ".join(given_tokens), last)

    tokens = name.split()
    if not tokens:
        return ("", "")
    if len(tokens) == 1:
        return ("", tokens[0])

    # Strip trailing suffix tokens (Jr., III, PhD, ...).
    while len(tokens) > 1 and tokens[-1].rstrip(".").lower() in _NAME_SUFFIXES:
        tokens.pop()
    if len(tokens) == 1:
        return ("", tokens[0])

    # Shape 2a: 'Last Initials' — trailing tokens are initials
    # (e.g. 'Smith J A', 'Boulé F. M.', IUCr 'Last F. M.').
    j = len(tokens)
    while j > 0 and _is_initials_token(tokens[j - 1]):
        j -= 1
    if 0 < j < len(tokens):
        return (" ".join(tokens[j:]), " ".join(tokens[:j]))

    # Shape 2b: 'Initials Last' — leading tokens are all-upper initials
    # (e.g. 'JD Griffith', 'J.D. Griffith'). Only fires with exactly two
    # tokens (single initial + surname) or multiple leading initials, so
    # a middle-name input like 'A. Hunter Shain' falls through to shape 3
    # rather than misparsing 'Hunter Shain' as surname.
    if _is_initials_token(tokens[0]):
        i = 0
        while i < len(tokens) and _is_initials_token(tokens[i]):
            i += 1
        if 0 < i < len(tokens) and (i >= 2 or len(tokens) == 2):
            return (" ".join(tokens[:i]), " ".join(tokens[i:]))

    # Shape 3: 'Given Last' — surname starts at the last token and
    # extends left across any compound-surname prefixes.
    i = len(tokens) - 1
    while i > 0 and _is_surname_prefix(tokens[i - 1]):
        i -= 1
    return (" ".join(tokens[:i]), " ".join(tokens[i:]))


def format_author_name(name):
    """Legacy adapter: format a single combined name string to 'Surname IN'.

    Equivalent to: format_name(*parse_combined_name(name)).
    Prefer calling format_name directly when the parser has access to
    a separate given/surname pair from the HTML source.
    """
    given, surname = parse_combined_name(name)
    return format_name(given, surname)


def format_doi(doi):
    """Ensure DOI is formatted as https://doi.org/... URL."""
    if not doi:
        return ""
    if doi.startswith("http"):
        return doi
    return f"https://doi.org/{doi}"


def parse_meta_authors(html):
    """Parse citation_author + citation_author_institution meta tags.

    Returns list of {"name": "Full Name", "affiliations": ["aff1", ...]}.
    Tags must appear in document order: each citation_author is followed
    by its citation_author_institution tags before the next citation_author.

    Handles both attribute orders (name-then-content and content-then-name).
    """
    authors = []
    current = None
    # Four sub-patterns: name-then-content (double/single-quoted) and
    # content-then-name (double/single-quoted). Each sub-pattern places
    # the value in groups (1)+(2) or (3)+(4).
    pattern = (
        # name-then-content, double-quoted
        r'<meta[^>]*name=["\']?citation_author(_institution)?["\']?'
        r'[^>]*content="([^"]*)"'
        r"|"
        # name-then-content, single-quoted
        r'<meta[^>]*name=["\']?citation_author(_institution)?["\']?'
        r"[^>]*content='([^']*)'"
        r"|"
        # content-then-name, double-quoted
        r'<meta[^>]*content="([^"]*)"'
        r'[^>]*name=["\']?citation_author(_institution)?["\']?'
        r"|"
        # content-then-name, single-quoted
        r"<meta[^>]*content='([^']*)'"
        r'[^>]*name=["\']?citation_author(_institution)?["\']?'
    )
    for m in re.finditer(pattern, html):
        # Determine whether this is an _institution tag and pull the value
        is_inst = bool(m.group(1) or m.group(3) or m.group(6) or m.group(8))
        value = (
            m.group(2) or m.group(4) or m.group(5) or m.group(7) or ""
        )
        value = unescape(value.strip())
        if is_inst:
            if current is not None:
                current["affiliations"].append(value.strip(", "))
        else:
            if current is not None:
                authors.append(current)
            current = {"name": value, "affiliations": []}
    if current is not None:
        authors.append(current)
    return authors
