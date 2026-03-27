#!/usr/bin/env python3
"""Search the paper collection by semantic similarity.

Usage:
    conda run -n py312 python search_refs.py [query]        # search papers
    conda run -n py312 python search_refs.py --build        # rebuild index

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


def get_citation_short(entry, pmid):
    """Derive citation_short from a refs.json entry."""
    cit = entry.get("citation_in_text", "")
    journal = entry.get("journal", "")
    return f"{cit} {journal} {pmid}"


def load_paper_text(citation_short):
    """Try to load full paper text from papers/ directory."""
    for ext in [".md", ".txt"]:
        path = os.path.join(PAPERS_DIR, citation_short + ext)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    return None


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
    full_text_count = 0

    for pmid, entry in refs.items():
        citation_short = get_citation_short(entry, pmid)
        full_text = load_paper_text(citation_short)

        if full_text:
            text = full_text
            full_text_count += 1
        else:
            parts = [entry.get("title", "")]
            abstract = entry.get("abstract", "")
            if abstract:
                parts.append(abstract)
            keywords = entry.get("keywords", [])
            if keywords:
                parts.append(" ".join(keywords))
            text = " ".join(parts)

        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            all_chunks.append(chunk)
            all_ids.append(f"{pmid}::chunk{i}")
            all_metadata.append({"pmid": pmid, "chunk_index": i})

    print(f"Embedding {len(all_chunks)} chunks from {len(refs)} papers...")

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

    print(f"Built: {len(refs)} papers, {len(all_chunks)} chunks, "
          f"{full_text_count} full_text, {len(refs) - full_text_count} abstract-only")


def do_query(query_terms):
    """Query the index and return ranked papers."""
    if not os.path.exists(DB_PATH):
        print("Index not found. Run --build first.", file=sys.stderr)
        sys.exit(1)

    device = detect_device()
    model = SentenceTransformer(MODEL_NAME)
    client = chromadb.PersistentClient(path=DB_PATH)
    collection = client.get_collection(COLLECTION_NAME)

    query = " ".join(query_terms)
    embedding = model.encode([query], device=device).tolist()[0]

    results = collection.query(
        query_embeddings=[embedding],
        n_results=30,
        include=["metadatas", "distances"],
    )

    metas = results["metadatas"][0]
    distances = results["distances"][0]

    # Deduplicate by PMID, keep best score per paper
    seen = {}
    for meta, dist in zip(metas, distances):
        pmid = meta["pmid"]
        score = round(1 - dist, 4)
        if pmid not in seen or score > seen[pmid]:
            seen[pmid] = score

    ranked = sorted(seen.items(), key=lambda x: -x[1])[:10]

    output = []
    for pmid, score in ranked:
        if score <= 0:
            break
        output.append({"pmid": pmid, "score": score})

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
