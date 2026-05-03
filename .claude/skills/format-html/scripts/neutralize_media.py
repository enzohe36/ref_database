"""Rewrite every <style> block in `html` so the publisher's CSS renders the
same 720-px layout regardless of the actual viewport.

Two transforms applied to viewport-width @media queries:
  1. `@media (min-width: N) { rules }` for N ≥ MIN_DESKTOP — entire block deleted
     (desktop-only rules never fire).
  2. `@media (max-width: N) { rules }` for N ≥ MAX_NARROW — `@media` wrapper
     stripped, leaving rules unconditional (narrow rules always fire).

Other media queries (orientation, prefers-color-scheme, print, mobile-only
breakpoints below MAX_NARROW, mixed-feature ranges) are left intact.

Usage from a parser:
    from .scripts.neutralize_media import neutralize_media_queries
    html = neutralize_media_queries(html)

Or copy this file's two functions into the parser if cross-package import
is awkward — they're free of external dependencies.

Use this when: piecemeal CSS overrides (display, padding, ::before content)
keep multiplying as you discover more elements that change between viewports.
The neutralizer collapses all the @media-driven layout differences into a
single transform, so there's nothing left to override per-element.

Don't use when: a parser already passes the format-html target spec across
all viewports without it. Adding a wholesale CSS rewrite for a parser that
was working is unnecessary risk.
"""
import re

# Threshold reasoning relative to the vw=720 reference (per format-html spec).
# `@media (min-width: N)` only fires when vw >= N — MIN_DESKTOP=721 catches
# every rule that does NOT fire at the reference (covers 768 / 992 / 1024 and
# 1025+ desktop breakpoints publishers use). `@media (max-width: N)` fires
# when vw <= N — MAX_NARROW=720 unwraps every rule that DOES fire at the
# reference so it applies at wider viewports too.
MIN_DESKTOP = 721   # delete `@media (min-width: N)` for N >= this
MAX_NARROW = 720    # unwrap `@media (max-width: N)` for N >= this


def _scan_balanced_block(text, open_idx):
    """Return the index just past the matching `}` for `{` at open_idx.

    Skips CSS strings ("..." or '...') so quoted braces don't confuse the
    counter. Returns -1 if no matching brace found.
    """
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


_MEDIA_RE = re.compile(r"@media\b([^{]+)\{", re.IGNORECASE)
_STYLE_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.DOTALL | re.IGNORECASE)
_FEATURE_NW = re.compile(
    r"\(\s*(min-width|max-width)\s*:\s*(\d+)(?:px|em|rem)?\s*\)",
    re.IGNORECASE,
)


def _neutralize_css(css, min_desktop=MIN_DESKTOP, max_narrow=MAX_NARROW):
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

        # Reject blocks with mixed feature tokens beyond min/max-width.
        feat_clean = _FEATURE_NW.sub("", feat)
        feat_clean = re.sub(
            r"\b(?:and|only|screen|all)\b|,", "", feat_clean, flags=re.IGNORECASE
        ).strip()

        widths = _FEATURE_NW.findall(feat)
        min_w = next((int(v) for k, v in widths if k.lower() == "min-width"), None)
        max_w = next((int(v) for k, v in widths if k.lower() == "max-width"), None)

        if feat_clean:
            out.append(css[m.start():body_end])  # mixed-feature — leave alone
        elif min_w is not None and max_w is None and min_w >= min_desktop:
            pass  # desktop-only block — drop
        elif max_w is not None and min_w is None and max_w >= max_narrow:
            out.append(rules)  # narrow block — unwrap
        else:
            out.append(css[m.start():body_end])  # other range — leave alone
        pos = next_pos
    return "".join(out)


def neutralize_media_queries(html, min_desktop=MIN_DESKTOP, max_narrow=MAX_NARROW):
    """Rewrite every <style> block in html, returning the transformed html."""
    return _STYLE_RE.sub(
        lambda m: m.group(1) + _neutralize_css(m.group(2), min_desktop, max_narrow) + m.group(3),
        html,
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: python neutralize_media.py <html_path>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], "r", errors="replace") as f:
        text = f.read()
    sys.stdout.write(neutralize_media_queries(text))
