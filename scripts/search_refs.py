#!/usr/bin/env python3
"""Search the embedding model with a query.

Usage:
    python search_refs.py "<query>"

Project resolution is cwd-based:
  - cwd inside projects/<name>/ → query that project's collection.
  - cwd elsewhere → query the global collection.

Output: JSON array of {pmid, stem, score, snippet} for the top matches.
"""

import json
import sys

import chromadb
from sentence_transformers import SentenceTransformer

from _project import chroma_dir, current_project_from_cwd

MODEL_NAME = "BAAI/bge-base-en-v1.5"
GLOBAL_COLLECTION = "global"
TOP_K = 30
RESULT_LIMIT = 10


def detect_device():
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
    except (ImportError, AttributeError):
        pass
    return "cpu"


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    query = " ".join(args)
    cdir = chroma_dir()
    if not cdir.exists():
        print("chroma_db/ not found. Run build_model.py first.", file=sys.stderr)
        sys.exit(1)

    name = current_project_from_cwd() or GLOBAL_COLLECTION
    device = detect_device()
    model = SentenceTransformer(MODEL_NAME)
    client = chromadb.PersistentClient(path=str(cdir))
    try:
        collection = client.get_collection(name)
    except Exception:
        print(f"collection {name!r} not found. Run build_model.py {name if name != GLOBAL_COLLECTION else ''}",
              file=sys.stderr)
        sys.exit(1)

    embedding = model.encode([query], device=device).tolist()[0]
    results = collection.query(
        query_embeddings=[embedding],
        n_results=TOP_K,
        include=["metadatas", "distances", "documents"],
    )
    metas = results["metadatas"][0]
    distances = results["distances"][0]
    documents = results["documents"][0]

    seen = {}
    for meta, dist, doc in zip(metas, distances, documents):
        pmid = meta.get("pmid", "")
        stem = meta.get("stem", "")
        key = pmid or stem
        score = round(1 - dist, 4)
        if key not in seen or score > seen[key][0]:
            seen[key] = (score, doc, pmid, stem)

    ranked = sorted(seen.items(), key=lambda x: -x[1][0])[:RESULT_LIMIT]
    output = []
    for _key, (score, snippet, pmid, stem) in ranked:
        if score <= 0:
            break
        output.append({
            "pmid": pmid,
            "stem": stem,
            "score": score,
            "snippet": snippet,
        })

    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
