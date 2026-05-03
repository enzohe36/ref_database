#!/usr/bin/env python3
"""Search the paper collection by semantic similarity.

Usage:
    python search_refs.py <query>        # search papers
    python search_refs.py --build        # rebuild index

Uses ChromaDB for vector storage and sentence-transformers for embeddings.
"""

import os
import sys
import json
import argparse
import chromadb
from sentence_transformers import SentenceTransformer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REFS_FILE = os.path.join(BASE_DIR, "refs.json")
PAPERS_DIR = os.path.join(BASE_DIR, "papers")
DB_PATH = os.path.join(BASE_DIR, "chroma_db")
COLLECTION_NAME = "papers"
MODEL_NAME = "BAAI/bge-base-en-v1.5"
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80


def detect_device():
    """Auto-detect best available device."""
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
    except (ImportError, AttributeError):
        pass
    return "cpu"


def load_references():
    """Load refs.json and return {pmid: entry}."""
    if not os.path.exists(REFS_FILE):
        return {}
    with open(REFS_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_main_text(stem):
    """Load main_text from papers/<stem>.json. Returns None if missing or empty."""
    path = os.path.join(PAPERS_DIR, stem + ".json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    text = data.get("main_text")
    if not text:
        return None
    return text


def chunk_text(text):
    """Split text into overlapping chunks by word count."""
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + CHUNK_SIZE, len(words))
        chunk = " ".join(words[start:end])
        if len(chunk.strip()) > 60:
            chunks.append(chunk)
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def do_build():
    """Build the semantic search index."""
    refs = load_references()
    if not refs:
        print("No papers found in refs.json", file=sys.stderr)
        sys.exit(1)

    print("Loading embedding model...")
    device = detect_device()
    model = SentenceTransformer(MODEL_NAME)

    os.makedirs(DB_PATH, exist_ok=True)
    client = chromadb.PersistentClient(path=DB_PATH)
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    all_chunks, all_ids, all_metadata = [], [], []
    indexed_papers = 0

    for pmid, entry in refs.items():
        stem = entry.get("stem")
        if not stem:
            continue
        text = load_main_text(stem)
        if not text:
            continue

        chunks = chunk_text(text)
        if not chunks:
            continue
        indexed_papers += 1
        for i, chunk in enumerate(chunks):
            all_chunks.append(chunk)
            all_ids.append(f"{pmid}::chunk{i}")
            all_metadata.append({"pmid": pmid, "chunk_index": i})

    print(f"Embedding {len(all_chunks)} chunks from {indexed_papers} papers...")

    BATCH = 256
    for batch_start in range(0, len(all_chunks), BATCH):
        batch_end = min(batch_start + BATCH, len(all_chunks))
        embeddings = model.encode(
            all_chunks[batch_start:batch_end],
            show_progress_bar=False,
            device=device,
        ).tolist()
        collection.add(
            documents=all_chunks[batch_start:batch_end],
            embeddings=embeddings,
            ids=all_ids[batch_start:batch_end],
            metadatas=all_metadata[batch_start:batch_end],
        )
        print(f"  {batch_end}/{len(all_chunks)} chunks indexed")

    print(f"Built: {indexed_papers} papers, {len(all_chunks)} chunks "
          f"({len(refs) - indexed_papers} skipped, no main_text)")


def do_query(query_terms):
    """Query the index and return ranked papers with their best matching snippet."""
    if not os.path.exists(DB_PATH):
        print("Index not found. Run --build first.", file=sys.stderr)
        sys.exit(1)

    refs = load_references()

    device = detect_device()
    model = SentenceTransformer(MODEL_NAME)
    client = chromadb.PersistentClient(path=DB_PATH)
    collection = client.get_collection(COLLECTION_NAME)

    query = " ".join(query_terms)
    embedding = model.encode([query], device=device).tolist()[0]

    results = collection.query(
        query_embeddings=[embedding],
        n_results=30,
        include=["metadatas", "distances", "documents"],
    )

    metas = results["metadatas"][0]
    distances = results["distances"][0]
    documents = results["documents"][0]

    # Deduplicate by PMID, keep best (score, snippet) per paper
    seen = {}
    for meta, dist, doc in zip(metas, distances, documents):
        pmid = meta["pmid"]
        score = round(1 - dist, 4)
        if pmid not in seen or score > seen[pmid][0]:
            seen[pmid] = (score, doc)

    ranked = sorted(seen.items(), key=lambda x: -x[1][0])[:10]

    output = []
    for pmid, (score, snippet) in ranked:
        if score <= 0:
            break
        output.append({
            "pmid": pmid,
            "stem": refs.get(pmid, {}).get("stem", ""),
            "score": score,
            "snippet": snippet,
        })

    print(json.dumps(output, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Search the paper collection")
    parser.add_argument("terms", nargs="*", metavar="TERM", help="Search query")
    parser.add_argument("--build", action="store_true", help="Rebuild search index")
    args = parser.parse_args()

    if args.build:
        do_build()
    elif args.terms:
        do_query(args.terms)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
