"""Project resolution and shared path helpers.

A project is a directory under repo_root()/projects/<name>/ holding a single
file: pmids.txt (space- or newline-separated PMIDs). Project membership is
implicit; everything else (paper content, metadata) lives in the global
content store under repo_root()/papers/.
"""

import os
import re
from pathlib import Path


def repo_root():
    """Repo root = parent of the scripts/ directory containing this module."""
    return Path(__file__).resolve().parent.parent


def parsed_dir():
    return repo_root() / "papers" / "parsed"


def raw_dir():
    return repo_root() / "papers" / "raw"


def test_dir():
    return repo_root() / "papers" / "test"


def projects_dir():
    return repo_root() / "projects"


def chroma_dir():
    return repo_root() / "chroma_db"


def current_project_from_cwd():
    """Return project name if cwd is inside projects/<name>/..., else None."""
    try:
        cwd = Path.cwd().resolve()
    except (OSError, FileNotFoundError):
        return None
    pdir = projects_dir().resolve()
    try:
        rel = cwd.relative_to(pdir)
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    return parts[0]


def project_pmids_file(name):
    return projects_dir() / name / "pmids.txt"


def project_pmids(name):
    """Read projects/<name>/pmids.txt; return set of PMID strings.

    Lines whose first non-whitespace character is '#' are ignored (comments).
    """
    path = project_pmids_file(name)
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    text = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )
    return set(re.findall(r"\d+", text))


def pmid_to_stem(pmid):
    """Given a PMID, find papers/parsed/<stem>.json. Returns stem or None."""
    pdir = parsed_dir()
    if not pdir.exists():
        return None
    matches = list(pdir.glob(f"*_{pmid}.json"))
    if len(matches) != 1:
        return None
    return matches[0].stem


def parsed_path(stem):
    return parsed_dir() / f"{stem}.json"


def raw_html_path(stem):
    return raw_dir() / f"{stem}.html"


def raw_pdf_path(stem):
    return raw_dir() / f"{stem}.pdf"


def raw_converted_path(stem):
    return raw_dir() / f"{stem}_converted.json"


def iter_parsed():
    """Yield Path for every papers/parsed/<stem>.json."""
    pdir = parsed_dir()
    if not pdir.exists():
        return
    for p in sorted(pdir.glob("*.json")):
        yield p


def stem_from_parsed_path(path):
    return Path(path).stem
