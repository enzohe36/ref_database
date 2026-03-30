#!/usr/bin/env python3
"""Convert PDFs to markdown text files.

Usage:
    python convert_pdf.py <path> [<path> ...]

Each path can be a PDF file or a directory (all .pdf files in the directory
will be processed). Output .md files are written next to each input PDF.
"""

import os
import sys
import multiprocessing
import pymupdf4llm


def convert_pdf_fitz(pdf_path):
    """Fallback: convert PDF using raw fitz text extraction."""
    import fitz
    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()
    return "\n".join(pages)


def convert_pdf(pdf_path):
    """Convert a PDF to markdown using pymupdf4llm, with fitz fallback."""
    text = pymupdf4llm.to_markdown(pdf_path)
    # If pymupdf4llm produced little text, fall back to fitz
    alpha_count = sum(1 for c in text if c.isalpha())
    if alpha_count < 2000:
        print(f"  {os.path.basename(pdf_path)}: pymupdf4llm produced little text, trying fitz fallback...", flush=True)
        text = convert_pdf_fitz(pdf_path)
    return text


def process_one(path):
    """Convert a single PDF and write the result."""
    stem = os.path.splitext(os.path.basename(path))[0]
    pdf_dir = os.path.dirname(path)
    target_md = os.path.join(pdf_dir, f"{stem}.md")

    if os.path.exists(target_md):
        return

    try:
        text = convert_pdf(path)
        with open(target_md, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"  Wrote {stem}.md", flush=True)
    except Exception as e:
        print(f"  Error converting {stem}: {e}", file=sys.stderr, flush=True)


def main():
    if len(sys.argv) < 2:
        print("Usage: python convert_pdf.py <path> [<path> ...]", file=sys.stderr)
        sys.exit(1)

    paths = []
    for arg in sys.argv[1:]:
        path = os.path.join(os.getcwd(), arg) if not os.path.isabs(arg) else arg
        if not os.path.exists(path):
            print(f"Not found: {path}", file=sys.stderr)
            continue
        if os.path.isdir(path):
            pdfs = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(".pdf")]
            if not pdfs:
                print(f"No PDFs found in: {path}", file=sys.stderr)
            paths.extend(sorted(pdfs))
        else:
            paths.append(path)

    n_workers = multiprocessing.cpu_count()
    print(f"Converting {len(paths)} PDFs using {n_workers} workers...", flush=True)

    with multiprocessing.Pool(n_workers) as pool:
        pool.map(process_one, paths)

    print("Done.", flush=True)


if __name__ == "__main__":
    main()
