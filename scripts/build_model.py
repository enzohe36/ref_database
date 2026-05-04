#!/usr/bin/env python3
"""Build the embedding model (chroma collection) for semantic search.

Usage:
    python build_model.py                            # rebuild _global collection
    python build_model.py <project_name> [<name>...]  # rebuild named project collections

No args: rebuild the `_global` chroma collection over every
papers/parsed/<stem>.json that has a non-empty main_text.

With <project_name> args: rebuild each named project's collection from
PMIDs in projects/<name>/pmids.txt.

Single chroma_db/ directory at repo root holds named collections; one per
project plus _global.
"""

import json
import os
import sys

import chromadb
from sentence_transformers import SentenceTransformer

from _project import (
    chroma_dir,
    iter_parsed,
    parsed_path,
    pmid_to_stem,
    project_pmids,
    projects_dir,
)

MODEL_NAME = "BAAI/bge-base-en-v1.5"
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80
GLOBAL_COLLECTION = "_global"


def detect_device():
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
    except (ImportError, AttributeError):
        pass
    return "cpu"


def chunk_text(text):
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


def _iter_papers_for_collection(name):
    """Yield (pmid, stem, main_text) tuples for a collection.

    name == GLOBAL_COLLECTION → every parsed/<stem>.json with main_text.
    name == <project> → parsed/<stem>.json for each PMID in projects/<name>/pmids.txt.
    """
    if name == GLOBAL_COLLECTION:
        for path in iter_parsed():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            text = data.get("main_text") or ""
            if not text:
                continue
            yield data.get("pmid", ""), data.get("stem", path.stem), text
        return

    pmids = project_pmids(name)
    if not pmids:
        return
    for pmid in sorted(pmids):
        stem = pmid_to_stem(pmid)
        if not stem:
            continue
        try:
            with open(parsed_path(stem), encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, FileNotFoundError):
            continue
        text = data.get("main_text") or ""
        if not text:
            continue
        yield pmid, stem, text


def build_collection(client, model, device, name):
    """Build (or rebuild) a single collection by name."""
    try:
        client.delete_collection(name)
    except Exception:
        pass
    collection = client.create_collection(
        name=name, metadata={"hnsw:space": "cosine"},
    )

    all_chunks, all_ids, all_metadata = [], [], []
    indexed = 0
    for pmid, stem, text in _iter_papers_for_collection(name):
        chunks = chunk_text(text)
        if not chunks:
            continue
        indexed += 1
        for i, chunk in enumerate(chunks):
            all_chunks.append(chunk)
            all_ids.append(f"{pmid or stem}::chunk{i}")
            all_metadata.append({"pmid": pmid, "stem": stem, "chunk_index": i})

    if not all_chunks:
        print(f"[{name}] empty: no papers with main_text")
        return

    print(f"[{name}] embedding {len(all_chunks)} chunks from {indexed} papers...")

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
        print(f"  [{name}] {batch_end}/{len(all_chunks)}")

    print(f"[{name}] built: {indexed} papers, {len(all_chunks)} chunks")


def main():
    args = sys.argv[1:]
    if not args:
        targets = [GLOBAL_COLLECTION]
    else:
        targets = []
        for name in args:
            if not (projects_dir() / name).is_dir():
                print(f"project not found: {name}", file=sys.stderr)
                sys.exit(1)
            targets.append(name)

    cdir = chroma_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    print(f"loading {MODEL_NAME}...")
    device = detect_device()
    model = SentenceTransformer(MODEL_NAME)
    client = chromadb.PersistentClient(path=str(cdir))

    for name in targets:
        build_collection(client, model, device, name)


if __name__ == "__main__":
    main()
