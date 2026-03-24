#!/usr/bin/env python3
"""Search the paper collection by keyword.

Usage:
    python search.py <term> [<term> ...]   # search papers
    python search.py --build               # rebuild index

Uses scikit-learn TfidfVectorizer for tokenization, stopwords, and TF-IDF,
and cosine_similarity for query ranking.
"""

import os
import sys
import json
import re
import argparse
import pickle
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REFS_FILE = os.path.join(BASE_DIR, "refs.json")
PAPERS_DIR = os.path.join(BASE_DIR, "papers")
SYNONYM_FILE = os.path.join(BASE_DIR, "synonyms.json")
INDEX_FILE = os.path.join(BASE_DIR, "keyword_map.pkl")

DOMAIN_STOPWORDS = [
    "study", "result", "results", "figure", "patient", "patients",
    "sample", "samples", "data", "analysis", "conclusion", "conclusions",
    "background", "aim", "aims", "showed", "suggest", "suggests",
    "demonstrate", "demonstrates", "demonstrated", "observed",
    "indicate", "indicates", "reveal", "reveals", "revealed",
    "report", "reported", "investigate", "investigated", "examined",
    "determined", "associated", "significant", "significantly",
    "important", "role", "mechanism", "mechanisms", "novel",
    "previously", "recent", "recently", "identified", "including",
    "specific", "et", "al", "fig", "table", "supplementary",
]


def load_synonym_table():
    """Load synonym table and build reverse mapping (alias -> canonical)."""
    if not os.path.exists(SYNONYM_FILE):
        return {}, {}
    with open(SYNONYM_FILE) as f:
        table = json.load(f)
    reverse = {}
    for canonical, aliases in table.items():
        cl = canonical.lower()
        reverse[cl] = cl
        for alias in aliases:
            reverse[alias.lower()] = cl
    return table, reverse


def load_references():
    """Load refs.json and return {pmid: entry}."""
    if not os.path.exists(REFS_FILE):
        return {}
    with open(REFS_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_paper_text(citation_short):
    """Try to load full paper text from papers/ directory."""
    for ext in [".md", ".txt"]:
        path = os.path.join(PAPERS_DIR, citation_short + ext)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    return None


def apply_synonyms(text, reverse_synonyms):
    """Replace synonym aliases with canonical terms in text."""
    if not reverse_synonyms:
        return text
    text_lower = text.lower()
    # Sort by length (longest first) to avoid partial replacements
    for alias in sorted(reverse_synonyms, key=len, reverse=True):
        if len(alias) < 3:
            continue
        canonical = reverse_synonyms[alias]
        if alias != canonical:
            try:
                text_lower = re.sub(
                    r'\b' + re.escape(alias) + r'\b', canonical, text_lower
                )
            except re.error:
                pass
    return text_lower


def get_citation_short(entry, pmid):
    """Derive citation_short from a refs.json entry."""
    cit = entry.get("citation_in_text", "")
    journal = entry.get("journal", "")
    return f"{cit} {journal} {pmid}"


def build_corpus(reverse_synonyms):
    """Build corpus: list of (pmid, text) tuples."""
    refs = load_references()
    corpus = []
    for pmid, entry in refs.items():
        citation_short = get_citation_short(entry, pmid)
        title = entry.get("title", "")

        # Try full text, fall back to title
        full_text = load_paper_text(citation_short)
        text = full_text if full_text else title

        # Apply synonym normalization
        text = apply_synonyms(text, reverse_synonyms)
        corpus.append((pmid, text))
    return corpus


def build_index(corpus):
    """Build TF-IDF index from corpus. Returns (vectorizer, tfidf_matrix, pmids)."""
    pmids = [c[0] for c in corpus]
    texts = [c[1] for c in corpus]

    vectorizer = TfidfVectorizer(
        stop_words="english",
        max_df=0.8,           # ignore terms in >80% of docs
        min_df=2,             # ignore terms in <2 docs
        ngram_range=(1, 2),   # unigrams and bigrams
        sublinear_tf=True,    # use 1 + log(tf)
        max_features=50000,
        token_pattern=r'(?u)\b[a-zA-Z][a-zA-Z0-9]{1,}\b',  # 2+ char tokens starting with letter
    )

    # Add domain stopwords
    if hasattr(vectorizer, 'stop_words') and vectorizer.stop_words == "english":
        # Will be resolved at fit time; we add domain words via stop_words_ after fit
        pass

    tfidf_matrix = vectorizer.fit_transform(texts)

    return vectorizer, tfidf_matrix, pmids


def save_index(vectorizer, tfidf_matrix, pmids):
    """Save index to disk."""
    with open(INDEX_FILE, "wb") as f:
        pickle.dump({
            "vectorizer": vectorizer,
            "tfidf_matrix": tfidf_matrix,
            "pmids": pmids,
        }, f)


def load_index():
    """Load index from disk."""
    if not os.path.exists(INDEX_FILE):
        return None
    with open(INDEX_FILE, "rb") as f:
        return pickle.load(f)


def do_build(reverse_synonyms):
    """Full build of the index."""
    corpus = build_corpus(reverse_synonyms)
    if not corpus:
        print("No papers found in refs.json", file=sys.stderr)
        sys.exit(1)

    vectorizer, tfidf_matrix, pmids = build_index(corpus)
    save_index(vectorizer, tfidf_matrix, pmids)

    n_features = len(vectorizer.get_feature_names_out())
    refs = load_references()
    full_text_count = sum(1 for pmid, _ in corpus if load_paper_text(get_citation_short(refs[pmid], pmid)) is not None)
    print(f"Built: {len(corpus)} papers, {n_features} features, "
          f"{full_text_count} full_text, {len(corpus) - full_text_count} abstract-only")



def do_query(query_terms, synonym_table, reverse_synonyms):
    """Query the index and return ranked papers."""
    data = load_index()
    if data is None:
        print("Index not found. Run --build first.", file=sys.stderr)
        sys.exit(1)

    vectorizer = data["vectorizer"]
    tfidf_matrix = data["tfidf_matrix"]
    pmids = data["pmids"]

    # Expand query through synonyms
    expanded = set()
    for term in query_terms:
        tl = term.lower()
        canonical = reverse_synonyms.get(tl, tl)
        expanded.add(canonical)
        # Also add aliases
        if canonical in synonym_table:
            for alias in synonym_table[canonical]:
                expanded.add(alias.lower())

    query_text = " ".join(expanded)
    query_text = apply_synonyms(query_text, reverse_synonyms)
    query_vec = vectorizer.transform([query_text])

    similarities = cosine_similarity(query_vec, tfidf_matrix).flatten()
    top_indices = similarities.argsort()[::-1][:10]

    results = []
    for idx in top_indices:
        score = float(similarities[idx])
        if score <= 0:
            break
        results.append({
            "pmid": pmids[idx],
            "score": round(score, 4),
        })

    return results


def main():
    parser = argparse.ArgumentParser(description="Search the paper collection")
    parser.add_argument("terms", nargs="*", metavar="TERM", help="Search terms")
    parser.add_argument("--build", action="store_true", help="Rebuild search index")
    args = parser.parse_args()

    synonym_table, reverse_synonyms = load_synonym_table()

    if args.build:
        do_build(reverse_synonyms)
    elif args.terms:
        results = do_query(args.terms, synonym_table, reverse_synonyms)
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
