#!/usr/bin/env python3
"""Mechanical writing-style audit for prose drafts produced by /write-draft.

Checks four dimensions per section (or whole draft):
  1. Sentence length (max <= 30 words excluding parens, average <= 20).
  2. Parenthesis usage (only abbreviations or stem-form citations).
  3. Abbreviation first-mention (full name + (ABBR) on first occurrence).
  4. Cross-reference (zero hits on section/cross-reference grep pattern).

Usage:
    python audit_writing.py <draft.md> --section <N>
    python audit_writing.py <draft.md> --whole

Section mode: audits a single top-level section. <N> is the integer section
number that appears in `## N. Title` or `## N.X` headers; the audit covers
the named section and any of its subsections, ending at the next top-level
section or end of file.

Whole mode: audits the entire draft. Reports per-section sentence-length
stats and aggregate violations across all four dimensions.

Exits 0 on clean audit, 1 if any violation found.
"""

import argparse
import re
import sys
from pathlib import Path

# Sentence boundary set per the writing requirements.
SENTENCE_BOUNDARY = r"[.;:!?]"

# Stem-form citation pattern (matches Author_YYYY_Journal_PMID).
STEM_PATTERN = re.compile(r"^[A-Z][a-z]+(?:[_][A-Z][a-z]+)*_[0-9]{4}_[A-Za-z0-9_]+_[0-9]+$")

# Abbreviation pattern for parenthesis content. Allows Greek letters (α β γ),
# digits, and short alphanumeric tokens. Examples: ERα, PR, STAT1, ISGF3, MHC-I.
ABBREV_PATTERN = re.compile(r"^[A-Z][A-Za-zα-ωΑ-Ω0-9\-/]*(?:[+-])?$")

# Section/cross-reference patterns to forbid.
XREF_PATTERN = re.compile(
    r"(\bSection\s+[0-9]+(?:\.[0-9]+)?\b"
    r"|\brest of this review\b"
    r"|\bthe (?:next|previous) section\b"
    r"|\bexplored in\b"
    r"|\bdiscussed in (?:Section|the)\b"
    r"|\breturned to\b"
    r"|\bdefined in Section\b"
    r"|\bestablished in Section\b"
    r"|, below\)"
    r"|, above\))",
    re.IGNORECASE,
)

# Universally-known abbreviations that need not be expanded.
UNIVERSAL_ABBREVS = {
    "DNA", "RNA", "mRNA", "PCR", "PDF", "HTML", "JSON", "API",
    "USA", "UK", "EU", "FDA", "EMA",
}

# Known section header patterns for splitting.
TOP_LEVEL_SECTION = re.compile(r"^## (\d+)\.\s+(.+)$")
SUB_SECTION = re.compile(r"^## (\d+)\.(\d+)(?:\.(\d+))?\s+(.+)$")
NAMED_SECTION = re.compile(r"^## (Title|Abstract|References|Conclusion.*?)$")


def split_sections(text):
    """Split draft text into sections keyed by top-level number or name.

    Returns ordered list of (key, header_line, body_lines).
    Subsections are grouped under their parent top-level section.
    """
    sections = []
    current_key = None
    current_header = None
    current_body = []

    for line in text.splitlines():
        m_top = TOP_LEVEL_SECTION.match(line)
        m_sub = SUB_SECTION.match(line)
        m_named = NAMED_SECTION.match(line)
        if m_top:
            if current_key is not None:
                sections.append((current_key, current_header, current_body))
            current_key = m_top.group(1)
            current_header = line
            current_body = []
        elif m_named and not m_sub:
            if current_key is not None:
                sections.append((current_key, current_header, current_body))
            current_key = m_named.group(1).lower().split()[0]
            current_header = line
            current_body = []
        else:
            current_body.append(line)

    if current_key is not None:
        sections.append((current_key, current_header, current_body))
    return sections


def strip_parens(text):
    """Remove parenthesized content from text for sentence-length counting."""
    out = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def split_sentences(text):
    """Split text into sentences at . ; : ! ? boundaries.

    Returns list of (sentence_text, end_position) tuples.
    Skips empty sentences and stem-citation-internal punctuation.
    """
    # Mask stem citations (which contain underscores but no punctuation that
    # would split on the boundary set, so they're fine), and decimal numbers
    # (3.14 should not split). For our purposes, a digit-dot-digit sequence
    # is masked.
    masked = re.sub(r"(\d)\.(\d)", r"\1\2", text)
    sentences = []
    buf = []
    for ch in masked:
        buf.append(ch)
        if ch in ".;:!?":
            s = "".join(buf).strip()
            if s and len(s) > 1:
                sentences.append(s.replace("", "."))
            buf = []
    tail = "".join(buf).strip()
    if tail:
        sentences.append(tail.replace("", "."))
    return sentences


def count_words(text):
    """Count words in text. Words are whitespace-separated tokens."""
    return len([t for t in re.split(r"\s+", text.strip()) if t])


def audit_sentence_length(body_text):
    """Audit sentence length: max <= 30 words, avg <= 20.

    Returns (violations_list, max_len, avg_len, n_sentences).
    """
    paragraphs = [p for p in body_text.split("\n\n") if p.strip() and not p.strip().startswith(("- ", "## ", "<!--"))]
    all_sentences = []
    for para in paragraphs:
        para_no_parens = strip_parens(para)
        all_sentences.extend(split_sentences(para_no_parens))

    if not all_sentences:
        return [], 0, 0.0, 0

    violations = []
    lengths = []
    for s in all_sentences:
        n = count_words(s)
        lengths.append(n)
        if n > 30:
            violations.append(("LONG_SENTENCE", n, s[:120] + ("..." if len(s) > 120 else "")))

    avg_len = sum(lengths) / len(lengths)
    max_len = max(lengths)
    if avg_len > 20:
        violations.append(("HIGH_AVG", avg_len, f"average sentence length {avg_len:.1f} > 20"))

    return violations, max_len, avg_len, len(all_sentences)


def audit_parenthesis(body_text):
    """Audit parenthesis content: must be abbreviation or stem citation.

    Returns list of (violation_type, content, context_snippet).
    """
    violations = []
    paren_pattern = re.compile(r"\(([^()]+?)\)")
    for line in body_text.splitlines():
        if line.strip().startswith(("## ", "<!--")):
            continue
        for m in paren_pattern.finditer(line):
            content = m.group(1).strip()
            # Multi-citation: split on `;` and check each
            if ";" in content:
                parts = [p.strip() for p in content.split(";")]
                for part in parts:
                    if not (STEM_PATTERN.match(part) or ABBREV_PATTERN.match(part)):
                        violations.append(("BAD_PAREN", part, line[:150]))
                continue
            # Comma-separated abbreviation list (rare, but allow)
            if "," in content and all(ABBREV_PATTERN.match(p.strip()) for p in content.split(",")):
                continue
            if STEM_PATTERN.match(content) or ABBREV_PATTERN.match(content):
                continue
            violations.append(("BAD_PAREN", content, line[:150]))
    return violations


def audit_abbreviation_first_mention(body_text):
    """Audit abbreviation first-mention: full name + (ABBR) on first use.

    Returns list of (abbreviation, context_snippet) violations.
    """
    violations = []
    seen_with_expansion = set()
    seen_naked = {}

    # Find candidate abbreviations: ≥2-char tokens with at least one uppercase.
    # Allow Greek letters and digits.
    abbrev_token = re.compile(r"\b([A-Z][A-Za-zα-ωΑ-Ω0-9\-/]+)\b")

    # Find expansions: phrases ending in "(ABBR)".
    expansion_pattern = re.compile(r"\(([A-Z][A-Za-zα-ωΑ-Ω0-9\-/]*(?:[+-])?)\)")

    lines = body_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(("## ", "<!--", "- ")) and not line.strip().startswith("- "):
            continue
        # Skip stem-citation parens by removing them first
        line_no_stems = re.sub(
            r"\([^)]*[A-Z][a-z]+_[0-9]{4}_[A-Za-z0-9_]+_[0-9]+[^)]*\)",
            "",
            line,
        )
        # Record expansions on this line
        for m in expansion_pattern.finditer(line_no_stems):
            seen_with_expansion.add(m.group(1))
        # Find naked abbreviation tokens (after expansions registered)
        # Strip out any (...) content first
        line_outside_parens = strip_parens(line_no_stems)
        for m in abbrev_token.finditer(line_outside_parens):
            tok = m.group(1)
            # Skip universal abbrevs and pure-uppercase short tokens that
            # are obviously single words (like "I", "A").
            if tok in UNIVERSAL_ABBREVS:
                continue
            if len(tok) < 2:
                continue
            # Skip tokens that don't look like abbreviations: must have
            # at least 2 uppercase letters OR contain a digit.
            uppercase_count = sum(1 for c in tok if c.isupper())
            has_digit = any(c.isdigit() for c in tok)
            if uppercase_count < 2 and not has_digit:
                continue
            if tok not in seen_with_expansion and tok not in seen_naked:
                seen_naked[tok] = (i, line[:150])

    for tok, (lineno, snippet) in seen_naked.items():
        violations.append(("UNEXPANDED_ABBREV", tok, snippet))
    return violations


def audit_xref(body_text):
    """Audit cross-references. Returns list of matches with line numbers."""
    violations = []
    for i, line in enumerate(body_text.splitlines(), start=1):
        if line.strip().startswith(("## ", "<!--")):
            continue
        for m in XREF_PATTERN.finditer(line):
            violations.append(("XREF", m.group(0), line[:150]))
    return violations


def collect_sentences(body_text):
    """Collect all non-paren-stripped sentences from body text.

    Returns list of (sentence_text, word_count) tuples. Used by the
    whole-draft sentence-length audit. Per-section audits also call this
    for informational stats but do not fail on length violations.
    """
    paragraphs = [p for p in body_text.split("\n\n") if p.strip() and not p.strip().startswith(("- ", "## ", "<!--"))]
    items = []
    for para in paragraphs:
        para_no_parens = strip_parens(para)
        for s in split_sentences(para_no_parens):
            items.append((s, count_words(s)))
    return items


def report_section(key, header, body_text):
    """Run per-section audits. Returns (clean, report_text).

    Sentence-length stats are reported for information but do NOT count
    toward `clean` — sentence-length enforcement is whole-draft only.
    Parenthesis, abbreviation, and cross-reference checks DO count toward
    `clean` and fail the per-section audit if violated.
    """
    sentences = collect_sentences(body_text)
    n_sents = len(sentences)
    if n_sents:
        lengths = [n for _, n in sentences]
        max_len = max(lengths)
        avg_len = sum(lengths) / n_sents
    else:
        max_len, avg_len = 0, 0.0

    paren_viols = audit_parenthesis(body_text)
    abbr_viols = audit_abbreviation_first_mention(body_text)
    xref_viols = audit_xref(body_text)

    lines = []
    lines.append(f"=== {header.strip()} ===")
    lines.append(f"Sentences: {n_sents} | max length: {max_len} | avg length: {avg_len:.1f} (informational; sentence-length enforced whole-draft only)")

    if paren_viols:
        lines.append(f"\n[Parens] Violations ({len(paren_viols)}):")
        for v in paren_viols:
            lines.append(f"  {v[0]}: '{v[1]}' in: {v[2]}")
    else:
        lines.append("[Parens] clean")

    if abbr_viols:
        lines.append(f"\n[Abbrev] Unexpanded abbreviations on first mention ({len(abbr_viols)}):")
        for v in abbr_viols:
            lines.append(f"  {v[0]}: '{v[1]}' first appears in: {v[2]}")
    else:
        lines.append("[Abbrev] clean")

    if xref_viols:
        lines.append(f"\n[Xref] Cross-reference violations ({len(xref_viols)}):")
        for v in xref_viols:
            lines.append(f"  {v[0]}: '{v[1]}' in: {v[2]}")
    else:
        lines.append("[Xref] clean")

    clean = not (paren_viols or abbr_viols or xref_viols)
    return clean, "\n".join(lines), sentences


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft", type=Path, help="Path to draft.md")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--section", help="Section number to audit (e.g., 1, 2, 3)")
    grp.add_argument("--whole", action="store_true", help="Audit the whole draft")
    args = ap.parse_args()

    if not args.draft.exists():
        print(f"ERROR: draft not found: {args.draft}", file=sys.stderr)
        sys.exit(2)

    text = args.draft.read_text()
    sections = split_sections(text)

    if args.section:
        target = args.section
        matched = [(k, h, b) for k, h, b in sections if k == target]
        if not matched:
            print(f"ERROR: section '{target}' not found in draft", file=sys.stderr)
            sys.exit(2)
        all_clean = True
        reports = []
        for key, header, body in matched:
            body_text = "\n".join(body)
            clean, report, _ = report_section(key, header, body_text)
            reports.append(report)
            if not clean:
                all_clean = False
        print("\n\n".join(reports))
        sys.exit(0 if all_clean else 1)

    # Whole-draft mode: per-section reports plus aggregate sentence-length check.
    all_clean = True
    reports = []
    all_sentences = []
    for key, header, body in sections:
        body_text = "\n".join(body)
        clean, report, sentences = report_section(key, header, body_text)
        reports.append(report)
        all_sentences.extend(sentences)
        if not clean:
            all_clean = False
    print("\n\n".join(reports))

    # Aggregate sentence-length check (whole-draft enforcement).
    print("\n\n=== AGGREGATE SENTENCE-LENGTH CHECK (whole draft) ===")
    if all_sentences:
        lengths = [n for _, n in all_sentences]
        max_len = max(lengths)
        avg_len = sum(lengths) / len(lengths)
        print(f"Total sentences: {len(all_sentences)}")
        print(f"Max sentence length: {max_len} (limit: 30)")
        print(f"Average sentence length: {avg_len:.2f} (limit: 20)")
        long_sentences = [(s, n) for s, n in all_sentences if n > 30]
        length_clean = True
        if long_sentences:
            length_clean = False
            print(f"\n{len(long_sentences)} sentences exceed the 30-word limit:")
            for s, n in long_sentences:
                snippet = s[:120] + ("..." if len(s) > 120 else "")
                print(f"  ({n} words) {snippet}")
        if avg_len > 20:
            length_clean = False
            print(f"\nAverage sentence length {avg_len:.2f} exceeds the 20-word limit.")
        if length_clean:
            print("[Sentence length] clean")
        else:
            all_clean = False
    else:
        print("No sentences found.")

    print("\n=== WHOLE-DRAFT SUMMARY ===")
    print(f"Sections audited: {len(sections)}")
    print(f"Status: {'CLEAN' if all_clean else 'VIOLATIONS PRESENT'}")
    sys.exit(0 if all_clean else 1)


if __name__ == "__main__":
    main()
