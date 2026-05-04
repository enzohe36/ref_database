#!/usr/bin/env python3
"""Logical-connection audit for an outline subsection.

Reads a subsection's bullets and reports a heuristic classification per
consecutive bullet pair. Output categories:

  - SUPPORTS:  N+1 reinforces N with additional evidence
  - EXTENDS:   N+1 adds a new dimension to the same claim
  - CONTRASTS: N+1 presents counter-evidence or a different mechanism
  - QUALIFIES: N+1 narrows or conditions N's claim
  - BRIDGES:   N+1 transitions from one cluster to the next
  - BROKEN:    no clear relationship; needs reorder, bridge, or move

The script is heuristic: it relies on lexical signals to PROPOSE a
classification. The agent must apply judgment to confirm or revise. Pairs
without strong lexical signal are reported as UNCLASSIFIED — the agent
classifies them by reading the bullets directly.

Usage:
    python audit_connections.py <outline.md> --section <id>

Examples:
    python audit_connections.py projects/foo/outline.md --section 3.1
    python audit_connections.py projects/foo/outline.md --section 4

Section <id> matches a `## N` or `## N.X` header in the outline. The
script audits only the bullets directly under the matched header (does
not recurse into deeper subsections of a parent section).
"""

import argparse
import json
import re
import sys
from pathlib import Path

SECTION_HEADER = re.compile(r"^## (\d+(?:\.\d+)*)\s+(.+)$")
BULLET_LINE = re.compile(r"^-\s+(.+)$")
STEM_CITATION = re.compile(r"\b[A-Z][a-z]+(?:_[A-Z][a-z]+)*_(\d{4})_[A-Za-z0-9_]+_\d+\b")

# Lexical signals for each connection type.
SUPPORTS_MARKERS = re.compile(
    r"\b(furthermore|additionally|consistent with|in agreement|moreover|"
    r"in line with|similarly|likewise|reinforces|confirms)\b",
    re.IGNORECASE,
)
EXTENDS_MARKERS = re.compile(
    r"\b(extends?|builds? on|further|beyond|in addition to|moreover|adds?|"
    r"expands?|generalizes?)\b",
    re.IGNORECASE,
)
CONTRASTS_MARKERS = re.compile(
    r"\b(however|in contrast|conversely|unlike|whereas|but|nevertheless|"
    r"yet|on the other hand|by contrast|paradoxically|despite)\b",
    re.IGNORECASE,
)
QUALIFIES_MARKERS = re.compile(
    r"\b(only|specifically|narrows?|conditions?|requires?|depends? on|"
    r"qualif(?:y|ies)|caveat|when|provided that|limited to|restricted to)\b",
    re.IGNORECASE,
)
BRIDGES_MARKERS = re.compile(
    r"\b(downstream|upstream|in turn|next|then|leads? to|drives?|results? in|"
    r"feeds? into|sets up|enables|the next|moving to|turning to)\b",
    re.IGNORECASE,
)


def parse_outline_for_section(text, section_id):
    """Extract bullets for the given section_id only (not subsections).

    Returns (header, bullets) or (None, []) if not found.
    """
    in_target = False
    target_header = None
    bullets = []

    for line in text.splitlines():
        m_sec = SECTION_HEADER.match(line)
        m_bullet = BULLET_LINE.match(line)

        if m_sec:
            if m_sec.group(1) == section_id:
                in_target = True
                target_header = m_sec.group(2).strip()
                bullets = []
            elif in_target:
                # Hit the next section header — stop collecting.
                break
        elif m_bullet and in_target:
            bullets.append(m_bullet.group(1).strip())

    return target_header, bullets


def shared_entities(b1, b2):
    """Extract overlap of named entities between two bullets.

    Returns count of shared protein/gene names and shared stem citations.
    """
    # Stems
    stems1 = set(re.findall(r"\b[A-Z][a-z]+(?:_[A-Z][a-z]+)*_\d{4}_[A-Za-z0-9_]+_\d+\b", b1))
    stems2 = set(re.findall(r"\b[A-Z][a-z]+(?:_[A-Z][a-z]+)*_\d{4}_[A-Za-z0-9_]+_\d+\b", b2))
    shared_stems = len(stems1 & stems2)

    # Named entities: capitalized multi-letter tokens (proteins, complexes).
    entity_pat = re.compile(r"\b([A-Z][A-Za-zα-ωΑ-Ω0-9\-/]{2,})\b")
    ents1 = set(entity_pat.findall(b1))
    ents2 = set(entity_pat.findall(b2))
    shared_entities = len(ents1 & ents2)

    return shared_stems, shared_entities


def classify_pair(b1, b2):
    """Heuristic classification of the (b1 -> b2) transition.

    Returns (label, rationale).
    """
    rationales = []

    # Lexical-marker signals on b2 (the second bullet often carries the
    # connective marker that names its relation to b1).
    has_supports = bool(SUPPORTS_MARKERS.search(b2))
    has_extends = bool(EXTENDS_MARKERS.search(b2))
    has_contrasts = bool(CONTRASTS_MARKERS.search(b2))
    has_qualifies = bool(QUALIFIES_MARKERS.search(b2))
    has_bridges = bool(BRIDGES_MARKERS.search(b2))

    # Entity overlap signal
    shared_stems, shared_ents = shared_entities(b1, b2)

    # Decision tree (priority: explicit lexical marker > entity overlap > none)
    if has_contrasts:
        label = "CONTRASTS"
        rationales.append("explicit contrast marker in b2")
    elif has_qualifies and shared_ents >= 1:
        label = "QUALIFIES"
        rationales.append("qualifier marker in b2 with shared entity")
    elif has_supports and shared_ents >= 1:
        label = "SUPPORTS"
        rationales.append("supports marker in b2 with shared entity")
    elif has_extends and shared_ents >= 1:
        label = "EXTENDS"
        rationales.append("extends marker in b2 with shared entity")
    elif has_bridges:
        label = "BRIDGES"
        rationales.append("bridging marker in b2")
    elif shared_stems >= 1 or shared_ents >= 2:
        # Strong entity overlap without explicit marker — likely SUPPORTS
        # or EXTENDS but ambiguous; flag for agent classification.
        label = "UNCLASSIFIED"
        rationales.append(f"entity overlap (stems={shared_stems}, entities={shared_ents}) but no explicit marker")
    elif shared_ents == 0 and shared_stems == 0:
        label = "BROKEN"
        rationales.append("no shared entities or stems and no connective marker")
    else:
        label = "UNCLASSIFIED"
        rationales.append(f"weak signals (stems={shared_stems}, entities={shared_ents})")

    return label, "; ".join(rationales)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outline", type=Path)
    ap.add_argument("--section", required=True, help="Section id (e.g., 3.1, 4, 2.3.1)")
    args = ap.parse_args()

    if not args.outline.exists():
        print(f"ERROR: outline not found: {args.outline}", file=sys.stderr)
        sys.exit(2)

    text = args.outline.read_text()
    header, bullets = parse_outline_for_section(text, args.section)

    if header is None:
        print(f"ERROR: section '{args.section}' not found in outline", file=sys.stderr)
        sys.exit(2)

    if len(bullets) < 2:
        report = {
            "section_id": args.section,
            "header": header,
            "bullet_count": len(bullets),
            "pairs": [],
            "note": "Fewer than 2 bullets — no pairs to audit.",
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        sys.exit(0)

    pairs = []
    counts = {"SUPPORTS": 0, "EXTENDS": 0, "CONTRASTS": 0, "QUALIFIES": 0, "BRIDGES": 0, "UNCLASSIFIED": 0, "BROKEN": 0}
    for i in range(len(bullets) - 1):
        label, rationale = classify_pair(bullets[i], bullets[i + 1])
        counts[label] += 1
        pairs.append({
            "from_bullet": i + 1,
            "to_bullet": i + 2,
            "label": label,
            "rationale": rationale,
            "from_snippet": bullets[i][:100] + ("..." if len(bullets[i]) > 100 else ""),
            "to_snippet": bullets[i + 1][:100] + ("..." if len(bullets[i + 1]) > 100 else ""),
        })

    report = {
        "section_id": args.section,
        "header": header,
        "bullet_count": len(bullets),
        "pair_count": len(pairs),
        "label_counts": counts,
        "pairs": pairs,
        "note": (
            "Heuristic classification. UNCLASSIFIED pairs need agent judgment. "
            "BROKEN pairs require reorder, bridging bullet insertion, or moving "
            "one bullet elsewhere. Fix all BROKEN pairs in the same tick."
        ),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    # Exit 1 if any BROKEN pairs (so the calling skill knows to act)
    sys.exit(1 if counts["BROKEN"] > 0 else 0)


if __name__ == "__main__":
    main()
