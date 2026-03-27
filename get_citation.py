#!/usr/bin/env python3
"""Fetch and parse a PubMed citation by PMID.

Usage:
    python get_citation.py [pmid] [[pmid] ...]
    python get_citation.py --validate

Outputs JSON with parsed citation fields:
    pmid, publication_types, citation_in_text, title, journal, year,
    volume, issue, pages, doi, references, abstract, citation_short
"""

import os
import sys
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def fetch_xml(pmid):
    """Fetch XML from PubMed E-utilities."""
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pubmed&id={pmid}&rettype=xml&retmode=xml"
    )
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


def gt(elem, path, default=""):
    """Get text from an element path."""
    el = elem.find(path) if elem is not None else None
    return el.text if el is not None and el.text else default


def attrs(el):
    """Format XML attributes as parenthetical string."""
    if el is None or not el.attrib:
        return ""
    return " (" + ", ".join(f"{k}={v}" for k, v in el.attrib.items()) + ")"



def parse_xml(xml_data, pmid):
    """Parse PubMed XML into citation fields and formatted output."""
    root = ET.fromstring(xml_data)
    article = root.find(".//PubmedArticle")
    mc = article.find("MedlineCitation")
    art = mc.find("Article")
    jrnl = art.find("Journal")
    ji = jrnl.find("JournalIssue")
    pd = ji.find("PubDate")
    pag = art.find("Pagination")

    journal_abbrev = gt(jrnl, "ISOAbbreviation")
    year = gt(pd, "Year")
    volume = gt(ji, "Volume")
    issue = gt(ji, "Issue")
    pages = gt(pag, "MedlinePgn") if pag is not None else ""
    title = gt(art, "ArticleTitle")
    doi_raw = ""
    for el in art.findall("ELocationID"):
        if el.get("EIdType") == "doi":
            doi_raw = el.text or ""
    # Fall back to ArticleIdList if ELocationID has no DOI
    if not doi_raw:
        pd_data_tmp = article.find("PubmedData")
        if pd_data_tmp is not None:
            aid_list_tmp = pd_data_tmp.find("ArticleIdList")
            if aid_list_tmp is not None:
                for aid in aid_list_tmp.findall("ArticleId"):
                    if aid.get("IdType") == "doi":
                        doi_raw = aid.text or ""
    doi = f"https://doi.org/{doi_raw}" if doi_raw else ""

    # Authors
    authors_raw = []
    for auth in art.findall(".//Author"):
        ln = gt(auth, "LastName")
        init = gt(auth, "Initials")
        if ln:
            affs = [aff.text for aff in auth.findall(".//Affiliation") if aff.text]
            authors_raw.append({"name": f"{ln} {init}".strip(), "affiliations": affs})

    # Abstract
    abstract_parts = []
    for ab in art.findall(".//AbstractText"):
        label = ab.get("Label", "")
        text = ET.tostring(ab, encoding="unicode", method="text").strip()
        if label:
            abstract_parts.append(f"{label}: {text}")
        else:
            abstract_parts.append(text)
    abstract = " ".join(abstract_parts)

    # Keywords
    keywords = []
    kw_list = mc.find("KeywordList")
    if kw_list is not None:
        for kw in kw_list.findall("Keyword"):
            if kw.text:
                keywords.append(kw.text.strip())

    # Publication types
    pub_types = [pt.text for pt in art.findall(".//PublicationType") if pt.text]

    # CitationShort
    author_last_names = [gt(a, "LastName") for a in art.findall(".//Author") if gt(a, "LastName")]
    num_authors = len(author_last_names)
    first_last = author_last_names[0] if author_last_names else ""
    if num_authors == 1:
        authors_short = first_last
    elif num_authors == 2:
        second_last = author_last_names[1]
        authors_short = f"{first_last} & {second_last}"
    else:
        authors_short = f"{first_last} et al."
    # Get PMID from ArticleIdList
    pd_data = article.find("PubmedData")
    pmid_from_aid = ""
    if pd_data is not None:
        aid_list = pd_data.find("ArticleIdList")
        if aid_list is not None:
            for aid in aid_list.findall("ArticleId"):
                if aid.get("IdType") == "pubmed":
                    pmid_from_aid = aid.text or ""
    pmid_final = pmid_from_aid or gt(mc, "PMID")

    citation_in_text = f"{authors_short} {year}"
    citation_short = f"{citation_in_text} {journal_abbrev} {pmid_final}"

    # Reference PMIDs (deduplicated)
    references = []
    if pd_data is not None:
        for ref in pd_data.findall(".//Reference"):
            for aid in ref.findall(".//ArticleId"):
                if aid.get("IdType") == "pubmed" and aid.text:
                    if aid.text not in references:
                        references.append(aid.text)

    # Validate
    if "Journal Article" not in pub_types:
        return None
    if "Retracted Publication" in pub_types:
        return None

    return {
        "pmid": pmid_final,
        "publication_types": pub_types,
        "citation_in_text": citation_in_text,
        "title": title,
        "journal": journal_abbrev,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "_authors_raw": authors_raw,
        "references": references,
        "abstract": abstract,
        "keywords": keywords,
        "citation_short": citation_short,
    }


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REFS_FILE = os.path.join(BASE_DIR, "refs.json")
REFS_TEMP_FILE = os.path.join(BASE_DIR, "temp_refs.md")


def load_references():
    """Load refs.json, return dict."""
    if not os.path.exists(REFS_FILE):
        return {}
    with open(REFS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_references(refs):
    """Save dict to refs.json with compact arrays for publication_types and references."""
    # Custom serialization: indent=2 but keep publication_types and references on one line
    raw = json.dumps(refs, indent=2, ensure_ascii=False)
    # Collapse multi-line arrays for these keys onto one line
    for key in ("publication_types", "keywords", "references"):
        def _collapse(m):
            items = [s.strip().rstrip(",") for s in m.group(2).split("\n") if s.strip()]
            return m.group(1) + " " + ", ".join(items) + " ]"
        raw = re.sub(
            rf'("{key}": \[)\s*\n(.*?)\n\s*\]',
            _collapse, raw, flags=re.DOTALL,
        )
    with open(REFS_FILE, "w", encoding="utf-8") as f:
        f.write(raw)
        f.write("\n")


def is_duplicate(pmid):
    """Check if PMID already exists in refs.json."""
    refs = load_references()
    return pmid in refs


def append_to_references(parsed):
    """Add entry to refs.json."""
    refs = load_references()
    filtered = [pt for pt in parsed['publication_types']
                if not pt.startswith("Research Support")]
    authors = [{"author": auth["name"], "affiliation": []} for auth in parsed.get('_authors_raw', [])]
    refs[parsed['pmid']] = {
        "citation_in_text": parsed['citation_in_text'],
        "journal": parsed['journal'],
        "volume": parsed['volume'],
        "issue": parsed['issue'],
        "year": parsed['year'],
        "title": parsed['title'],
        "pages": parsed['pages'],
        "doi": parsed['doi'],
        "abstract": parsed.get('abstract', ''),
        "authors": authors,
        "publication_types": filtered,
        "keywords": parsed.get('keywords', []),
        "references": parsed.get('references', []),
    }
    save_references(refs)


def append_to_references_temp(parsed):
    """Append citation_short + doi_url to no_pdf.md if PDF is not already present."""
    pdf_path = os.path.join(PAPERS_DIR, f"{parsed['citation_short']}.pdf")
    if os.path.exists(pdf_path):
        return
    with open(REFS_TEMP_FILE, "a", encoding="utf-8") as f:
        f.write(f"{parsed['citation_short']}\n")
        f.write(f"{parsed['doi']}\n\n")


PAPERS_DIR = os.path.join(BASE_DIR, "papers")



def search_pmids(query):
    """Search PubMed and return list of PMIDs."""
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={urllib.parse.quote(query)}&retmax=20&retmode=xml"
    )
    with urllib.request.urlopen(url) as resp:
        xml_data = resp.read().decode("utf-8")
    root = ET.fromstring(xml_data)
    return [id_el.text for id_el in root.findall(".//IdList/Id")]


def validate():
    """Validate all PMIDs in refs.json: check for retracted articles, and search for published versions of preprints."""
    refs = load_references()
    pmids = list(refs.keys())
    print(f"Validating {len(pmids)} entries...", flush=True)
    retracted = []
    preprints = []  # (pmid, title)
    fetch_count = 0
    all_pmids_set = set(pmids)

    for pmid in pmids:
        if fetch_count > 0:
            time.sleep(0.4)
        try:
            xml_data = fetch_xml(pmid)
            fetch_count += 1
            root = ET.fromstring(xml_data)
            pub_types = [pt.text for pt in root.findall(".//PublicationType") if pt.text]
        except Exception as e:
            print(json.dumps({"pmid": pmid, "status": "error", "message": str(e)}))
            continue

        if "Retracted Publication" in pub_types:
            retracted.append(pmid)
        if "Preprint" in pub_types:
            title_el = root.find(".//ArticleTitle")
            title = ET.tostring(title_el, encoding="unicode", method="text").strip() if title_el is not None else ""
            preprints.append((pmid, title))

    if retracted:
        for pmid in retracted:
            print(json.dumps({"pmid": pmid, "status": "retracted"}))
    else:
        print(json.dumps({"status": "ok", "message": "No retracted articles found."}))

    for pmid, title in preprints:
        if fetch_count > 0:
            time.sleep(0.4)
        try:
            query = f"{title} NOT preprint[pt]"
            result_pmids = search_pmids(query)
            fetch_count += 1
        except Exception as e:
            print(json.dumps({"pmid": pmid, "status": "error", "message": f"Search failed: {e}"}))
            continue

        candidates = [p for p in result_pmids if p != pmid and p not in all_pmids_set]
        print(json.dumps({"pmid": pmid, "status": "preprint", "title": title, "candidates": candidates}))

    if not preprints:
        print(json.dumps({"status": "ok", "message": "No preprints to check."}))


def main():
    if len(sys.argv) < 2:
        print("Usage: python get_citation.py [pmid] [[pmid] ...]\n"
              "       python get_citation.py --validate", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--validate":
        validate()
        return

    pmids = sys.argv[1:]
    results = []
    fetched_count = 0
    for pmid in pmids:
        pmid = re.sub(r".*/(\d+)/?$", r"\1", pmid.strip().rstrip("/"))

        if is_duplicate(pmid):
            print(json.dumps({"pmid": pmid, "status": "duplicate", "message": f"PMID {pmid} already exists in refs.json"}))
            continue

        # Rate-limit PubMed requests (3/sec without API key)
        if fetched_count > 0:
            time.sleep(0.4)
        try:
            xml_data = fetch_xml(pmid)
            fetched_count += 1
            parsed = parse_xml(xml_data, pmid)
        except Exception as e:
            print(json.dumps({"pmid": pmid, "status": "error", "message": str(e)}))
            continue
        if parsed is None:
            pub_types = [pt.text for pt in ET.fromstring(xml_data).findall(".//PublicationType") if pt.text]
            reason = "Retracted Publication" if "Retracted Publication" in pub_types else "not a Journal Article"
            print(json.dumps({"pmid": pmid, "status": "skipped", "message": f"PMID {pmid}: {reason}. PublicationTypes: {pub_types}"}))
            continue
        results.append(parsed)

        append_to_references(parsed)
        append_to_references_temp(parsed)

    if len(results) == 1:
        print(json.dumps(results[0], indent=2, ensure_ascii=False))
    elif len(results) > 1:
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
