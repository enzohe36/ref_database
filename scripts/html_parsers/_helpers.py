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
    """Remove HTML elements by id. Handles nested content.

    Requires a word-or-quote boundary after the id value so that
    `remove_elements_by_id(html, "footer")` does not also match
    `id=footersearch` (unquoted ids where the target is a prefix of
    the actual id). Quoted ids are anchored by the closing quote;
    unquoted ids are anchored by a word boundary (which also covers
    trailing whitespace, `>`, or attribute separators).
    """
    for eid in ids:
        html = _remove_nested_element(
            html,
            rf'<\w+[^>]*\bid=(?:"{re.escape(eid)}"|\'{re.escape(eid)}\'|'
            rf'{re.escape(eid)}\b)[^>]*>',
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
    interleaved between name and content (e.g. scheme=WTN8601).

    Quoted values use separate patterns per quote type so that an inner
    apostrophe inside a double-quoted value (e.g. content="Slice'N'Dice")
    is not mistaken for the closing quote.
    """
    esc = re.escape(name)
    # content value: double- or single-quoted (match only the same quote on
    # both sides), or unquoted (ends at space or >).
    val_qd = r'"([^"]*)"'
    val_qs = r"'([^']*)'"
    val_u = r'([^\s>]+)'
    # name can be quoted or unquoted; require word boundary at end so
    # "dc.Date" doesn't also match "dc.DateAccepted".
    name_pat = rf'["\']?{esc}\b["\']?'
    # Inter-attribute separator: any chars not breaking out of the tag
    sep = r'[^>]*?\s'
    pats = []
    for vq in (val_qd, val_qs):
        pats.append(rf'<meta[^>]*name={name_pat}{sep}content={vq}')
        pats.append(rf'<meta[^>]*content={vq}{sep}name={name_pat}')
    pats.append(rf'<meta[^>]*name={name_pat}{sep}content={val_u}')
    pats.append(rf'<meta[^>]*content={val_u}{sep}name={name_pat}')
    return pats


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
    Double- and single-quoted patterns are tried separately so apostrophes
    inside double-quoted values are not treated as closing quotes.
    """
    esc = re.escape(name)
    name_pat = rf'["\']?{esc}["\']?'
    val_qd = r'"([^"]*)"'
    val_qs = r"'([^']*)'"
    patterns = []
    for vq in (val_qd, val_qs):
        patterns.append(rf'<meta[^>]*name={name_pat}\s+content={vq}')
        patterns.append(rf'<meta[^>]*content={vq}[^>]*name={name_pat}')
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
# "Smith Jr." -> surname "Smith", "Yates 3rd" -> surname "Yates").
_NAME_SUFFIXES = frozenset({
    "jr", "sr", "ii", "iii", "iv", "v",
    "phd", "md", "dr",
})
# Ordinal digit suffixes ("2nd", "3rd", "4th", ...) matched via regex so
# arbitrary values are covered without enumerating them.
_ORDINAL_SUFFIX_RE = re.compile(r"^\d+(?:st|nd|rd|th)$")


def _is_name_suffix(tok):
    """True if tok is a generational suffix (Jr, Sr, III, 3rd, PhD, ...)."""
    norm = tok.rstrip(".").lower()
    return norm in _NAME_SUFFIXES or bool(_ORDINAL_SUFFIX_RE.match(norm))

# Academic honorifics stripped from the head of combined names so they
# don't leak into initials ("Prof. Ming-De Li" -> "Ming-De Li" -> "Li MD",
# "Dr. Ying Li" -> "Ying Li" -> "Li Y"). Case-insensitive, period-tolerant.
_NAME_HONORIFICS = frozenset({
    "prof", "dr", "mr", "mrs", "ms", "mx", "sir", "dame",
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


def format_name(given, surname, suffix=""):
    """Build canonical 'Surname IN [Suffix]' from an explicit given/surname pair.

    This is the only function in the codebase that builds initials.
    Given is split on whitespace, ASCII hyphen, Unicode hyphens
    (U+2010–U+2013), and periods; the first alphabetic character of
    each part contributes one initial. Optional suffix (Jr, III, 3rd,
    ...) is appended verbatim after the initials.

    Examples:
      format_name("Jean-Baptiste", "Boulé")      -> "Boulé JB"
      format_name("J.-B.", "Boulé")              -> "Boulé JB"
      format_name("Ryan P.", "Barnes")           -> "Barnes RP"
      format_name("Mariarosaria", "de Rosa")     -> "de Rosa M"
      format_name("", "Smith")                   -> "Smith"
      format_name("JA", "Smith")                 -> "Smith JA"
      format_name("John", "Smith", "Jr")         -> "Smith J Jr"
      format_name("JR", "Yates", "III")          -> "Yates JR III"
    """
    given = _normalize_unicode(given or "").strip()
    surname = _normalize_unicode(surname or "").strip().rstrip(",")
    suffix = (suffix or "").strip().rstrip(",.").strip()
    tail = f" {suffix}" if suffix else ""
    if not given:
        return surname + tail if surname else tail.lstrip()

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
    # PubMed caps initials at two letters (first and second given-name
    # part). Keeps 'Reijns MAM' ↔ 'Reijns MA', 'Huang SYN' ↔ 'Huang SY'
    # aligned with refs.json.
    initials = initials[:2]
    if not initials:
        return surname + tail if surname else tail.lstrip()
    if not surname:
        return initials + tail
    return f"{surname} {initials}{tail}"


def parse_combined_name(name):
    """Split a combined author-name string into (given, surname, suffix).

    Handles three input shapes:
      - 'Last, Given'      -> unambiguous comma split
      - 'Initials Last'    -> leading all-upper short token(s) are initials
                              (e.g. 'JD Griffith', 'J.D. Griffith')
      - 'Given Last'       -> default; surname is the trailing run of
                              tokens, extended left while the preceding
                              token is a known surname prefix (de, van,
                              nick, d', etc.)

    Trailing/interior suffixes (Jr., III, PhD, 3rd, ...) are stripped
    before surname detection and returned as the third element.
    Returns ('', '', '') for empty input, ('', name, '') for a
    single-token input.

    Examples:
      parse_combined_name("Barnes, Ryan P.")       -> ("Ryan P.", "Barnes", "")
      parse_combined_name("Jean-Baptiste Boulé")   -> ("Jean-Baptiste", "Boulé", "")
      parse_combined_name("Titia de Lange")        -> ("Titia", "de Lange", "")
      parse_combined_name("JR Yates III")          -> ("JR", "Yates", "III")
      parse_combined_name("Smith, John Jr.")       -> ("John", "Smith", "Jr")
      parse_combined_name("Yates 3rd JR")          -> ("JR", "Yates", "3rd")
    """
    if not name:
        return ("", "", "")
    name = _normalize_unicode(name).strip().strip(",").strip()
    if not name:
        return ("", "", "")
    suffix = ""

    # Strip trailing suffix tokens from the whole string before shape
    # detection. Handles the 'Given Last, Suffix' form ('Thomas E.
    # Cheatham, III') where the comma is a suffix separator rather than
    # a surname-reversal marker. After stripping, the remaining string
    # falls through cleanly to the shape detector (shape 3 for this
    # example, shape 1 for 'Smith, John Jr.' etc.).
    while True:
        stripped = name.rstrip()
        tok_m = re.search(r"[\s,]+(\S+)$", stripped)
        if not tok_m:
            break
        raw_tok = tok_m.group(1)
        # Single letter followed by a period is an initial, not a suffix
        # ("Grishin N. V.", "Uski V." — trailing "V." is initial V, not
        # Roman V).
        if (len(raw_tok.rstrip(".")) == 1 and raw_tok.endswith(".")
                and raw_tok[0].isalpha()):
            break
        # All-caps 1-2 letter tokens without a period are canonical
        # initials on already-formatted names ("Lakey JR", author MD
        # pair), not Jr./MD. suffixes. Real word suffixes carry a
        # period or are longer (Jr. / PhD. / III).
        if (raw_tok.isalpha() and raw_tok.isupper()
                and 1 <= len(raw_tok) <= 2):
            break
        if not _is_name_suffix(raw_tok):
            break
        if not suffix:
            suffix = raw_tok.rstrip(",.").strip()
        name = stripped[:tok_m.start()].rstrip().rstrip(",").rstrip()
    if not name:
        return ("", "", "")

    # Shape 1: 'Last, Given' — comma is unambiguous. Dutch/Portuguese
    # convention trails the surname prefix on the given side (e.g.
    # 'Lange, Titia de'); absorb trailing prefix tokens back into the
    # surname so the result is ('Titia', 'de Lange').
    if "," in name:
        last, given = name.split(",", 1)
        last = last.strip()
        given_tokens = given.strip().split()
        # Strip generational suffixes (Jr., III, PhD, ...) from both
        # sides — publishers are inconsistent about placement:
        # 'Yates III, John R.' puts it in the surname; 'Smith, John
        # Jr.' puts it in the given. Either way it shouldn't leak
        # into the initials or surname output.
        last_tokens = last.split()
        while last_tokens and _is_name_suffix(last_tokens[-1]):
            if not suffix:
                suffix = last_tokens[-1].rstrip(",.").strip()
            last_tokens.pop()
        last = " ".join(last_tokens)
        while given_tokens and _is_name_suffix(given_tokens[-1]):
            if not suffix:
                suffix = given_tokens[-1].rstrip(",.").strip()
            given_tokens.pop()
        # Absorb trailing particle tokens back into the surname, but only
        # if a given name would remain. Without this guard, 'Gui, Bin'
        # collapses to ('', 'Bin Gui') because Arabic patronymic 'bin' is
        # a registered prefix that coincides with the Chinese given name
        # 'Bin'. Similarly protects single-token given names that happen
        # to spell 'de', 'al', etc.
        while len(given_tokens) > 1 and _is_surname_prefix(given_tokens[-1]):
            last = given_tokens.pop() + " " + last
        # Interior-prefix case: 'Marion de Procé, Sophie' — the
        # publisher places 'Marion' inside the surname but PubMed
        # records 'Marion' as a middle given name with 'de Procé' as
        # the surname. When the surname contains an interior prefix
        # token whose predecessor is NOT itself a prefix, split there
        # — tokens before the prefix move into the given side.
        # Prefix stacks like 'van der Berg' or 'd'Adda di Fagagna'
        # are preserved because their interior prefix's predecessor
        # is itself a prefix, so the check fails and no split happens.
        last_tokens = last.split()
        for i in range(1, len(last_tokens)):
            if (_is_surname_prefix(last_tokens[i])
                    and not _is_surname_prefix(last_tokens[i - 1])):
                given_tokens.extend(last_tokens[:i])
                last = " ".join(last_tokens[i:])
                break
        return (" ".join(given_tokens), last, suffix)

    tokens = name.split()
    if not tokens:
        return ("", "", suffix)
    if len(tokens) == 1:
        return ("", tokens[0], suffix)

    # Strip leading honorifics (Prof., Dr., Mr., ...) and trailing
    # suffix tokens (Jr., III, PhD, ...). Single-letter-with-period tokens
    # ("N.", "V.") are initials, not Roman-numeral suffixes, so skip those.
    while len(tokens) > 1 and tokens[0].rstrip(".").lower() in _NAME_HONORIFICS:
        tokens.pop(0)
    while len(tokens) > 1:
        tail = tokens[-1]
        if (len(tail.rstrip(".")) == 1 and tail.endswith(".")
                and tail[0].isalpha()):
            break
        if (tail.isalpha() and tail.isupper() and 1 <= len(tail) <= 2):
            break
        if not _is_name_suffix(tail):
            break
        if not suffix:
            suffix = tail.rstrip(",.").strip()
        tokens.pop()
    # Also strip interior suffix tokens sandwiched between surname and
    # initials ('Yates 3rd JR' → ['Yates','JR']). Ordinal/generational
    # suffix tokens ("2nd", "3rd", "Jr", "III", "PhD", ...) are never
    # part of the surname proper. Single-letter-with-period ("N.") and
    # all-caps 1-2 letter tokens ("JR") are preserved via the same
    # initials-vs-suffix tests used in the trailing loop above.
    new_tokens = []
    for t in tokens:
        if (len(t.rstrip(".")) == 1 and t.endswith(".") and t[0].isalpha()):
            new_tokens.append(t)
        elif t.isalpha() and t.isupper() and 1 <= len(t) <= 2:
            new_tokens.append(t)
        elif not _is_name_suffix(t):
            new_tokens.append(t)
        elif not suffix:
            suffix = t.rstrip(",.").strip()
    tokens = new_tokens
    if len(tokens) == 1:
        return ("", tokens[0], suffix)
    if not tokens:
        return ("", "", suffix)

    # Shape 2a: 'Last Initials' — trailing tokens are initials
    # (e.g. 'Smith J A', 'Boulé F. M.', IUCr 'Last F. M.').
    j = len(tokens)
    while j > 0 and _is_initials_token(tokens[j - 1]):
        j -= 1
    if 0 < j < len(tokens):
        return (" ".join(tokens[j:]), " ".join(tokens[:j]), suffix)

    # Shape 2b: 'Initials Last' — leading tokens are all-upper initials
    # (e.g. 'JD Griffith', 'J.D. Griffith'). Only fires with exactly two
    # tokens (single initial + surname) or multiple leading initials, so
    # a middle-name input like 'A. Hunter Shain' falls through to shape 3
    # rather than misparsing 'Hunter Shain' as surname. Also stops
    # consuming initials when a candidate token is a known surname
    # prefix ('T. DE LANGE' must not treat 'DE' as an initial despite
    # its all-caps rendering).
    if _is_initials_token(tokens[0]):
        i = 0
        while i < len(tokens) and _is_initials_token(tokens[i]):
            if tokens[i].rstrip(".").lower() in _SURNAME_PREFIXES:
                break
            i += 1
        if 0 < i < len(tokens) and (i >= 2 or len(tokens) == 2):
            return (" ".join(tokens[:i]), " ".join(tokens[i:]), suffix)

    # Shape 3: 'Given Last' — surname starts at the last token and
    # extends left across any compound-surname prefixes. Stop before
    # consuming the first given-name token so 'Bin Gui' stays as
    # ('Bin', 'Gui') instead of collapsing to ('', 'Bin Gui') via the
    # Arabic patronymic 'bin'.
    i = len(tokens) - 1
    while i > 1 and _is_surname_prefix(tokens[i - 1]):
        i -= 1
    return (" ".join(tokens[:i]), " ".join(tokens[i:]), suffix)


def format_author_name(name):
    """Legacy adapter: format a single combined name string to 'Surname IN [Suffix]'.

    Equivalent to: format_name(*parse_combined_name(name)).
    Prefer calling format_name directly when the parser has access to
    a separate given/surname pair from the HTML source.
    """
    given, surname, suffix = parse_combined_name(name)
    return format_name(given, surname, suffix)


# Mapping from academic email domains to the canonical institution name
# PubMed records for papers hosted by that institution. Used as a
# last-resort affiliation inference when the publisher HTML exposes only
# author correspondence emails without structured affiliation data (older
# CSHLP symposium papers, T&F Cell Cycle, etc.). Narrow on purpose —
# only domains where every paper we encounter maps to a single canonical
# affiliation string should be added.
_EMAIL_DOMAIN_TO_AFFILIATION = {
    "rockefeller.edu": "The Rockefeller University, New York, NY, USA",
    "mail.rockefeller.edu": "The Rockefeller University, New York, NY, USA",
}


def affiliation_from_email(email):
    """Return a canonical institution string inferred from an email domain,
    or '' when the domain isn't in the known map.

    The returned string is conservative — it omits department and
    lab-specific detail the email can't resolve. Callers should only use
    this as a fallback when no structural affiliation is available in
    the HTML.
    """
    if not email or "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[1].lower().strip().rstrip(".")
    return _EMAIL_DOMAIN_TO_AFFILIATION.get(domain, "")


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


# ---------------------------------------------------------------------------
# Media-query neutralizer
# ---------------------------------------------------------------------------
# Lock the publisher's CSS to its narrow (≤ 1024 px) layout regardless of the
# viewer's actual viewport. Two transforms applied to viewport-width @media:
#   1. `@media (min-width: N) { rules }` for N ≥ 1025 — entire block deleted
#      (desktop rules never fire).
#   2. `@media (max-width: N) { rules }` for N ≥ 720 — `@media` wrapper
#      stripped, leaving rules unconditional (narrow rules always fire).
# Other media queries (orientation, prefers-color-scheme, print, mobile-only
# breakpoints below MAX_NARROW, mixed-feature ranges) are left intact.
#
# Use when piecemeal CSS overrides multiply (display / padding / pseudo-content
# rules per-element) — the neutralizer collapses all viewport-gated layout
# differences into one transform.

# Threshold reasoning relative to the vw=720 reference (per the develop-parser
# layout target). `@media (min-width: N)` fires only when vw >= N — so MIN_DESKTOP=721
# deletes every block whose threshold is above 720 (rule does NOT fire at the
# reference). Catches publishers using 768 / 992 / 1024 as desktop breakpoint
# in addition to the 1025+ ones. `@media (max-width: N)` fires when vw <= N —
# so MAX_NARROW=720 unwraps every block at-or-above 720 (rule DOES fire at the
# reference) so it applies unconditionally at wider viewports too.
_MEDIA_THRESH_MIN_DESKTOP = 721
_MEDIA_THRESH_MAX_NARROW = 720
_MEDIA_RE = re.compile(r"@media\b([^{]+)\{", re.IGNORECASE)
_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.DOTALL | re.IGNORECASE)
_FEATURE_NW_RE = re.compile(
    r"\(\s*(min-width|max-width|min-device-width|max-device-width)"
    r"\s*:\s*(\d+)(?:px|em|rem)?\s*\)",
    re.IGNORECASE,
)


def _scan_balanced_block(text, open_idx):
    """Return the index just past the matching `}` for `{` at open_idx."""
    depth = 1
    i = open_idx + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c in '"\'':
            quote = c
            i += 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        else:
            i += 1
    return -1


def _neutralize_css(css):
    out = []
    pos = 0
    while True:
        m = _MEDIA_RE.search(css, pos)
        if not m:
            out.append(css[pos:])
            break
        out.append(css[pos:m.start()])
        feat = m.group(1)
        body_start = m.end()
        body_end = _scan_balanced_block(css, body_start - 1)
        if body_end == -1:
            out.append(css[m.start():])
            break
        rules = css[body_start:body_end - 1]
        next_pos = body_end
        # Strip width/device-width features (handled below) and ignorable
        # features that don't affect viewport-width gating (orientation,
        # prefers-*, print, pixel-ratio, hover). Whatever's left in
        # feat_clean is an unrecognized feature; in that case leave the
        # block alone to avoid breaking rules we don't understand.
        feat_clean = _FEATURE_NW_RE.sub("", feat)
        feat_clean = re.sub(
            r"\(\s*(?:orientation|prefers-[\w-]+|hover|pointer|"
            r"-webkit-min-device-pixel-ratio|min-resolution|max-resolution"
            r")\s*:\s*[^)]+\)|"
            r"\bprint\b|"
            r"\b(?:and|only|or|not|screen|all)\b|,",
            "", feat_clean, flags=re.IGNORECASE,
        ).strip()
        widths = _FEATURE_NW_RE.findall(feat)
        # Combine all min-* and max-* features (treat min-device-width same
        # as min-width for our viewport-targeting purposes).
        mins = [int(v) for k, v in widths if k.lower().startswith("min-")]
        maxs = [int(v) for k, v in widths if k.lower().startswith("max-")]
        min_w = max(mins) if mins else None  # most-restrictive lower bound
        max_w = min(maxs) if maxs else None  # most-restrictive upper bound
        if feat_clean:
            out.append(css[m.start():body_end])
        elif min_w is not None and max_w is None and min_w >= _MEDIA_THRESH_MIN_DESKTOP:
            pass
        elif max_w is not None and min_w is None and max_w >= _MEDIA_THRESH_MAX_NARROW:
            out.append(rules)
        else:
            out.append(css[m.start():body_end])
        pos = next_pos
    return "".join(out)


def neutralize_media_queries(html):
    """Rewrite every <style> block in html so the publisher's narrow @media
    layout applies at any viewport. See module docstring above."""
    return _STYLE_BLOCK_RE.sub(
        lambda m: m.group(1) + _neutralize_css(m.group(2)) + m.group(3),
        html,
    )
