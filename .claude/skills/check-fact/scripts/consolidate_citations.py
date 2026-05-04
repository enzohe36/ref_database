"""Consolidate consecutive identical citation sets within a paragraph.

If sentences N..N+k all carry the same citation set, drop the citation from
N..N+k-1 and keep it on N+k (last in run). Empty citation set breaks runs.
"""
from __future__ import annotations

from typing import Dict, List


def consolidate(sentences: List[Dict]) -> List[Dict]:
    """sentences: [{text, citations: [stem, ...], ...}, ...]
    Mutates the citations field in place and returns the list.
    """
    n = len(sentences)
    i = 0
    while i < n:
        cur_set = frozenset(sentences[i].get("citations", []))
        if not cur_set:
            i += 1
            continue
        j = i
        while j + 1 < n and frozenset(sentences[j + 1].get("citations", [])) == cur_set:
            j += 1
        for k in range(i, j):
            sentences[k]["citations"] = []
        i = j + 1
    return sentences


if __name__ == "__main__":
    samples = [
        {"text": "A.", "citations": ["X"]},
        {"text": "B.", "citations": ["X"]},
        {"text": "C.", "citations": ["X"]},
        {"text": "D.", "citations": ["Y"]},
        {"text": "E.", "citations": []},
        {"text": "F.", "citations": ["X"]},
        {"text": "G.", "citations": ["X", "Z"]},
        {"text": "H.", "citations": ["X", "Z"]},
    ]
    for s in consolidate(samples):
        print(s)
