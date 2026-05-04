#!/usr/bin/env python3
"""Heuristic scan of an outline for weak areas needing refinement.

Reports candidate weak areas in JSON. The skill uses this as input but
applies judgment on top — the script is heuristic, not authoritative.

Signals reported:
  - thin_subsection: subsection with fewer than MIN_BULLETS bullets
  - orphan_bullet: bullet without a stem citation
  - vague_bullet: bullet with no quantitative anchor, no named cell line/model,
                  no specific mechanism marker
  - recency_thin: subsection with no 2021+ citations
  - empty_header: subsection or section with no bullets at all

Usage:
    python identify_weak_areas.py <outline.md> [--scratch <scratch.md>] [--min-bullets N]

If --scratch is provided, the script appends prior-tick "weakest areas" notes
to the report so the agent can prioritize unaddressed gaps.
"""

import argparse
import json
import re
import sys
from pathlib import Path

MIN_BULLETS_DEFAULT = 3
RECENT_YEAR_THRESHOLD = 2021

SECTION_HEADER = re.compile(r"^## (\d+(?:\.\d+)*)\s+(.+)$")
NAMED_HEADER = re.compile(r"^## (Title|Abstract|References|Conclusion.*?)$")
BULLET_LINE = re.compile(r"^-\s+(.+)$")
STEM_CITATION = re.compile(r"\b[A-Z][a-z]+(?:_[A-Z][a-z]+)*_(\d{4})_[A-Za-z0-9_]+_\d+\b")

# Heuristic vagueness markers: presence of any of these in a bullet without
# a quantitative or named-entity counterweight signals vagueness.
VAGUE_HEDGES = re.compile(
    r"\b(may|might|could|appear(?:s)?\s+to|seem(?:s)?\s+to|likely|plausibl[ey]|"
    r"potentially|suggested?|implied)\b",
    re.IGNORECASE,
)

# Specificity markers that compensate for hedging: numeric values, mutation codes,
# specific cell lines, specific concentrations, specific residues.
SPECIFIC_MARKERS = re.compile(
    r"(\d+(\.\d+)?\s*(%|nM|μM|uM|mM|kDa|Å|kb|bp|months?|days?|years?|h|hours?|fold)"
    r"|\b[A-Z]\d+[A-Z]\b"
    r"|MCF-?7|T47D|BT-?\d+|MDA-MB-\d+|ZR-?\d+|SUM\d+"
    r"|p\.\w+\d+\w+"
    r"|residue \w+\d+)",
    re.IGNORECASE,
)


def parse_outline(text):
    """Parse outline into hierarchical structure.

    Returns list of (section_id, header, bullets) tuples in document order.
    Subsections are reported as separate entries.
    """
    sections = []
    current_id = None
    current_header = None
    current_bullets = []

    for line in text.splitlines():
        m_sec = SECTION_HEADER.match(line)
        m_named = NAMED_HEADER.match(line)
        m_bullet = BULLET_LINE.match(line)

        if m_sec:
            if current_id is not None:
                sections.append((current_id, current_header, current_bullets))
            current_id = m_sec.group(1)
            current_header = m_sec.group(2).strip()
            current_bullets = []
        elif m_named:
            if current_id is not None:
                sections.append((current_id, current_header, current_bullets))
            current_id = m_named.group(1).lower().split()[0]
            current_header = m_named.group(1).strip()
            current_bullets = []
        elif m_bullet:
            if current_id is not None:
                current_bullets.append(m_bullet.group(1).strip())

    if current_id is not None:
        sections.append((current_id, current_header, current_bullets))
    return sections


def is_leaf_subsection(section_id, all_ids):
    """Return True if section_id has no further subsections (e.g., 2.3.1
    with no 2.3.1.X children, OR 3.1 with no 3.1.X children)."""
    prefix = section_id + "."
    return not any(sid.startswith(prefix) for sid in all_ids if sid != section_id)


def scan_section(section_id, header, bullets, min_bullets=MIN_BULLETS_DEFAULT):
    """Scan one section/subsection for weak-area signals.

    Returns list of {type, section_id, detail} dicts.
    """
    signals = []

    # Skip non-numbered sections (Title, Abstract, References, Conclusion).
    is_numbered = bool(re.match(r"^\d+(\.\d+)*$", section_id))

    if not bullets:
        signals.append({
            "type": "empty_header",
            "section_id": section_id,
            "detail": f"section '{header}' has no bullets",
        })
        return signals

    if is_numbered and "." in section_id and len(bullets) < min_bullets:
        # Only flag thin for leaf subsections (the rule applies to populated
        # subsections, not parent section headers that exist only to group
        # subsections).
        signals.append({
            "type": "thin_subsection",
            "section_id": section_id,
            "detail": f"section '{header}' has only {len(bullets)} bullets (min {min_bullets})",
        })

    years_seen = set()
    for i, bullet in enumerate(bullets):
        # Orphan: no stem citation in the bullet.
        stems = STEM_CITATION.findall(bullet)
        if not stems and is_numbered:
            signals.append({
                "type": "orphan_bullet",
                "section_id": section_id,
                "detail": f"bullet {i+1}: '{bullet[:120]}{'...' if len(bullet) > 120 else ''}'",
            })
        for year_str in stems:
            try:
                years_seen.add(int(year_str))
            except ValueError:
                pass

        # Vagueness: hedge present without specificity counterweight.
        has_hedge = bool(VAGUE_HEDGES.search(bullet))
        has_specificity = bool(SPECIFIC_MARKERS.search(bullet))
        if has_hedge and not has_specificity:
            signals.append({
                "type": "vague_bullet",
                "section_id": section_id,
                "detail": f"bullet {i+1}: hedge without specific anchor — '{bullet[:120]}{'...' if len(bullet) > 120 else ''}'",
            })

    if is_numbered and "." in section_id and bullets and not any(y >= RECENT_YEAR_THRESHOLD for y in years_seen):
        signals.append({
            "type": "recency_thin",
            "section_id": section_id,
            "detail": f"section '{header}' has no citations from {RECENT_YEAR_THRESHOLD}+ (years seen: {sorted(years_seen)})",
        })

    return signals


def extract_prior_weakest_areas(scratch_path):
    """Extract 'Weakest areas for iteration N+1' from the most recent
    iteration block in the scratch file.

    Returns list of strings (the bulleted area descriptions), or empty list.
    """
    if not scratch_path or not scratch_path.exists():
        return []
    text = scratch_path.read_text()
    # Find all "Weakest areas for iteration N+1:" blocks; keep the last one.
    pattern = re.compile(
        r"Weakest areas for iteration\s+\d+\+?1?\s*:\s*\n((?:\s*-\s+.+\n?)+)",
        re.IGNORECASE,
    )
    matches = pattern.findall(text)
    if not matches:
        return []
    last_block = matches[-1]
    items = []
    for line in last_block.splitlines():
        m = re.match(r"\s*-\s+(.+)", line)
        if m:
            items.append(m.group(1).strip())
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outline", type=Path)
    ap.add_argument("--scratch", type=Path, default=None)
    ap.add_argument("--min-bullets", type=int, default=MIN_BULLETS_DEFAULT)
    args = ap.parse_args()

    if not args.outline.exists():
        print(f"ERROR: outline not found: {args.outline}", file=sys.stderr)
        sys.exit(2)

    text = args.outline.read_text()
    sections = parse_outline(text)
    all_ids = [sid for sid, _, _ in sections]

    all_signals = []
    for sid, header, bullets in sections:
        sigs = scan_section(sid, header, bullets, min_bullets=args.min_bullets)
        all_signals.extend(sigs)

    prior = extract_prior_weakest_areas(args.scratch) if args.scratch else []

    # Aggregate by section_id for ranking.
    by_section = {}
    for sig in all_signals:
        by_section.setdefault(sig["section_id"], []).append(sig)

    ranked = sorted(
        by_section.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )

    report = {
        "outline": str(args.outline),
        "total_sections_scanned": len(sections),
        "total_signals": len(all_signals),
        "signals_by_section": {sid: sigs for sid, sigs in ranked},
        "ranked_section_ids": [sid for sid, _ in ranked],
        "prior_weakest_areas": prior,
        "note": "Heuristic scan. Apply judgment. Use prior_weakest_areas as a shortcut if those areas remain unaddressed.",
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
