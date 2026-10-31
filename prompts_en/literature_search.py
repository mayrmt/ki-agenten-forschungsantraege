"""Literature search with Semantic Scholar, optimized for maximum PDF availability.

WORKFLOW
  1. Put one search string per line into search_queries.txt (blank lines and # comments ignored).
  2. export SEMANTIC_SCHOLAR_API_KEY='your key'
  3. python literature_search.py            (see --help for options)
  4. python literature_download.py semantic_scholar_results.json

WHAT IT DOES
  * Runs every query, merges duplicates (same DOI/title) and remembers which queries found a paper.
  * For every paper collects all known PDF sources into "pdf_candidates": Semantic Scholar, arXiv ID,
    PMC, Unpaywall, OpenAlex, Europe PMC.
  * If nothing is known, looks for a preprint with (almost) the same title and a shared author
    on arXiv, OpenAlex and Semantic Scholar.
  * Writes semantic_scholar_results.json (input for the download script) and a .csv overview.

SETUP (once)
  python3 -m venv .venv && . .venv/bin/activate && python -m pip install requests

REQUIREMENTS
  Python 3, "requests", the SEMANTIC_SCHOLAR_API_KEY environment variable, and a contact e-mail
  (--email or CONTACT_EMAIL; Unpaywall requires one). The enrichment step takes ~10-20 minutes
  for ~250 papers.
"""

import argparse
import os
import json
import csv
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

import requests


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

YEAR_FROM = 2010
RESULTS_PER_QUERY = 25
PAUSE_BETWEEN_REQUESTS = 2

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
UNPAYWALL_URL = "https://api.unpaywall.org/v2/"
OPENALEX_URL = "https://api.openalex.org/works"
ARXIV_URL = "http://export.arxiv.org/api/query"
EUROPE_PMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Unpaywall requires a real contact e-mail; OpenAlex uses it for the faster "polite pool".
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "matthias.mayr@unibw.de")
ENRICH = True            # look up additional PDF sources for every paper
SEARCH_PREPRINTS = True  # if no PDF source is known: look for a preprint with the same title/authors
TITLE_MATCH_THRESHOLD = 0.9
HEADERS = {"User-Agent": f"literature-search/1.0 (mailto:{CONTACT_EMAIL or 'unknown'})"}


# ---------------------------------------------------------------------------
# Load search strings
# ---------------------------------------------------------------------------

def load_search_queries(filename):
    """Read search strings from a text file."""

    queries = []

    with open(filename, "r", encoding="utf-8") as file:
        for line in file:
            query = line.strip()

            if query and not query.startswith("#"):
                queries.append(query)

    return queries


# ---------------------------------------------------------------------------
# Search Semantic Scholar
# ---------------------------------------------------------------------------

def search_semantic_scholar(query, api_key):
    """Run a simple literature search with Semantic Scholar."""

    params = {
        "query": query,
        "limit": RESULTS_PER_QUERY,
        "year": f"{YEAR_FROM}-",
        "fields": (
            "paperId,title,authors,year,abstract,"
            "citationCount,venue,url,externalIds,"
            "isOpenAccess,openAccessPdf"
        )  # externalIds also delivers ArXiv / PubMed / PMC IDs
    }

    headers = {
        "x-api-key": api_key
    }

    for attempt in range(5):
        try:
            response = requests.get(
                API_URL,
                params=params,
                headers=headers,
                timeout=30
            )
        except requests.RequestException as error:
            print(f"Netzwerkfehler: {error}")
            return []

        if response.status_code == 429:
            wait_time = 2 ** attempt
            print(
                f"Rate Limit erreicht. "
                f"Warte {wait_time} Sekunden und versuche es erneut."
            )
            time.sleep(wait_time)
            continue

        if response.status_code != 200:
            print(
                f"Fehler bei der API-Anfrage: "
                f"{response.status_code} {response.text}"
            )
            return []

        data = response.json()
        break

    else:
        print("Anfrage nach mehreren Versuchen abgebrochen.")
        return []

    results = []

    for paper in data.get("data", []):
        authors = [
            author.get("name")
            for author in paper.get("authors", [])
            if author.get("name")
        ]

        external_ids = paper.get("externalIds") or {}
        open_access_pdf = paper.get("openAccessPdf") or {}

        results.append({
            "paper_id": paper.get("paperId"),
            "title": paper.get("title"),
            "year": paper.get("year"),
            "authors": authors,
            "abstract": paper.get("abstract"),
            "citation_count": paper.get("citationCount"),
            "venue": paper.get("venue"),
            "doi": (external_ids.get("DOI") or "").lower() or None,
            "arxiv_id": external_ids.get("ArXiv"),
            "pmcid": external_ids.get("PubMedCentral"),
            "url": paper.get("url"),
            "is_open_access": paper.get("isOpenAccess"),
            "open_access_pdf": open_access_pdf.get("url"),
            "search_query": query,
            "search_queries": [query]
        })

    return results


# ---------------------------------------------------------------------------
# Remove duplicates
# ---------------------------------------------------------------------------

def normalize_title(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def remove_duplicates(results):
    """Merge duplicates (same DOI or normalized title); keep all queries that found a paper."""

    by_key = {}

    for paper in results:
        title_key = normalize_title(paper.get("title"))
        keys = [k for k in (paper.get("doi"), title_key) if k]
        if not keys:
            continue

        existing = next((by_key[k] for k in keys if k in by_key), None)
        if existing is None:
            existing = paper
        else:
            for query in paper["search_queries"]:
                if query not in existing["search_queries"]:
                    existing["search_queries"].append(query)
            # fill gaps in the kept record
            for field in ("doi", "arxiv_id", "pmcid", "abstract", "open_access_pdf"):
                existing[field] = existing.get(field) or paper.get(field)
            existing["is_open_access"] = bool(existing.get("is_open_access") or paper.get("is_open_access"))

        for k in keys:
            by_key[k] = existing

    unique = []
    for paper in by_key.values():
        if not any(paper is other for other in unique):
            unique.append(paper)
    return unique


# ---------------------------------------------------------------------------
# Find additional PDF sources
# ---------------------------------------------------------------------------

def get_json(url, params=None, retries=3):
    """GET a JSON document; returns None on any failure."""
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException:
            time.sleep(1 + attempt)
            continue
        if response.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None
    return None


def add_candidate(candidates, url, source, kind="published"):
    if url and url.startswith(("http://", "https://")) and all(c["url"] != url for c in candidates):
        candidates.append({"url": url, "source": source, "kind": kind})


def candidates_from_unpaywall(doi):
    if not (doi and CONTACT_EMAIL):
        return []
    data = get_json(f"{UNPAYWALL_URL}{quote(doi, safe='/')}", {"email": CONTACT_EMAIL})
    found = []
    for location in (data or {}).get("oa_locations") or []:
        kind = "preprint" if location.get("version") == "submittedVersion" else "published"
        add_candidate(found, location.get("url_for_pdf"), "unpaywall", kind)
        add_candidate(found, location.get("url"), "unpaywall", kind)
    return found


def candidates_from_openalex(doi=None, title=None):
    params = {"select": "title,doi,open_access,best_oa_location,locations"}
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL
    if doi:
        data = get_json(f"{OPENALEX_URL}/doi:{quote(doi, safe='/')}", params)
        works = [data] if data else []
    else:
        params.update({"search": title, "per-page": 3})
        works = ((get_json(OPENALEX_URL, params) or {}).get("results")) or []
        works = [w for w in works if title_similarity(title, w.get("title")) >= TITLE_MATCH_THRESHOLD]

    found = []
    for work in works:
        locations = [work.get("best_oa_location")] + (work.get("locations") or [])
        for location in locations:
            if not location:
                continue
            kind = "preprint" if location.get("version") == "submittedVersion" else "published"
            add_candidate(found, location.get("pdf_url"), "openalex", kind)
            add_candidate(found, location.get("landing_page_url"), "openalex", kind)
        add_candidate(found, (work.get("open_access") or {}).get("oa_url"), "openalex")
    return found


def candidates_from_europe_pmc(doi=None, pmcid=None):
    """Europe PMC serves full-text PDFs directly for PMC records."""
    found = []
    if pmcid:
        pmcid = pmcid if str(pmcid).upper().startswith("PMC") else f"PMC{pmcid}"
        add_candidate(found, f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf", "europepmc")
    elif doi:
        data = get_json(EUROPE_PMC_URL, {"query": f'DOI:"{doi}"', "format": "json", "resultType": "lite"})
        for hit in ((data or {}).get("resultList") or {}).get("result") or []:
            if hit.get("pmcid") and hit.get("isOpenAccess") == "Y":
                add_candidate(found, f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={hit['pmcid']}&blobtype=pdf",
                              "europepmc")
    return found


def title_similarity(a, b):
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def surname(name):
    parts = re.findall(r"[A-Za-z]+", name or "")
    return parts[-1].lower() if parts else ""


def candidates_from_arxiv_title(paper):
    """Find a preprint with (almost) the same title and at least one shared author surname."""
    title = paper.get("title") or ""
    if not title:
        return []
    query = "ti:" + " AND ti:".join(f'"{w}"' for w in re.findall(r"[A-Za-z0-9]{4,}", title)[:8])
    try:
        response = requests.get(ARXIV_URL, params={"search_query": query, "max_results": 5},
                                headers=HEADERS, timeout=30)
        root = ElementTree.fromstring(response.content)
    except (requests.RequestException, ElementTree.ParseError):
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    wanted = {surname(n) for n in paper.get("authors") or []} - {""}
    found = []
    for entry in root.findall("a:entry", ns):
        entry_title = (entry.findtext("a:title", "", ns) or "").replace("\n", " ")
        entry_authors = {surname(a.findtext("a:name", "", ns)) for a in entry.findall("a:author", ns)}
        if title_similarity(title, entry_title) >= TITLE_MATCH_THRESHOLD and (not wanted or wanted & entry_authors):
            arxiv_id = (entry.findtext("a:id", "", ns) or "").rsplit("/abs/", 1)[-1]
            add_candidate(found, f"https://arxiv.org/pdf/{arxiv_id}", "arxiv_title_match", "preprint")
    return found


def candidates_from_semantic_scholar_preprint(paper, api_key):
    """Title match in Semantic Scholar: another record of the same work may carry an OA PDF or arXiv ID."""
    try:
        response = requests.get(API_URL, params={"query": paper["title"], "limit": 5,
                                                 "fields": "title,authors,externalIds,openAccessPdf"},
                                headers={"x-api-key": api_key}, timeout=30)
        hits = response.json().get("data", []) if response.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return []

    wanted = {surname(n) for n in paper.get("authors") or []} - {""}
    found = []
    for hit in hits:
        hit_authors = {surname(a.get("name")) for a in hit.get("authors") or []}
        if hit.get("paperId") == paper.get("paper_id"):
            continue
        if title_similarity(paper["title"], hit.get("title")) >= TITLE_MATCH_THRESHOLD and (not wanted or wanted & hit_authors):
            arxiv_id = (hit.get("externalIds") or {}).get("ArXiv")
            if arxiv_id:
                add_candidate(found, f"https://arxiv.org/pdf/{arxiv_id}", "s2_title_match", "preprint")
            add_candidate(found, (hit.get("openAccessPdf") or {}).get("url"), "s2_title_match", "preprint")
    return found


def enrich_paper(paper, api_key):
    """Collect every known PDF source into paper["pdf_candidates"] (best sources first)."""
    candidates = []
    add_candidate(candidates, paper.get("open_access_pdf"), "semantic_scholar")
    if paper.get("arxiv_id"):
        add_candidate(candidates, f"https://arxiv.org/pdf/{paper['arxiv_id']}", "arxiv_id", "preprint")
    if paper.get("pmcid"):
        for c in candidates_from_europe_pmc(pmcid=paper["pmcid"]):
            add_candidate(candidates, c["url"], c["source"])

    if ENRICH:
        doi = paper.get("doi")
        for finder in (
            lambda: candidates_from_unpaywall(doi),
            lambda: candidates_from_openalex(doi=doi) if doi else [],
            lambda: candidates_from_europe_pmc(doi=doi) if doi else [],
        ):
            for c in finder():
                add_candidate(candidates, c["url"], c["source"], c["kind"])

    if SEARCH_PREPRINTS and ENRICH and not any(c["kind"] == "preprint" or c["source"] != "semantic_scholar"
                                               for c in candidates) and paper.get("title"):
        for finder in (
            lambda: candidates_from_arxiv_title(paper),
            lambda: candidates_from_openalex(title=paper["title"]),
            lambda: candidates_from_semantic_scholar_preprint(paper, api_key),
        ):
            for c in finder():
                add_candidate(candidates, c["url"], c["source"], c["kind"])
            time.sleep(0.5)

    paper["pdf_candidates"] = candidates
    paper["pdf_url"] = candidates[0]["url"] if candidates else None
    paper["has_pdf_candidate"] = bool(candidates)
    return paper


# ---------------------------------------------------------------------------
# Save results
# ---------------------------------------------------------------------------

def save_as_json(results, filename):
    with open(filename, "w", encoding="utf-8") as file:
        json.dump(results, file, ensure_ascii=False, indent=2)


def save_as_csv(results, filename):
    fieldnames = [
        "title",
        "year",
        "authors",
        "venue",
        "citation_count",
        "doi",
        "pdf_url",
        "has_pdf_candidate",
        "url",
        "search_query",
        "abstract"
    ]

    with open(filename, "w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()

        for paper in results:
            row = paper.copy()
            row["authors"] = "; ".join(row.get("authors") or [])

            writer.writerow({
                field: row.get(field)
                for field in fieldnames
            })


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Literaturrecherche mit Semantic Scholar. Liest Suchstrings aus search_queries.txt, "
                    "sucht pro Paper zusätzliche PDF-Quellen (Unpaywall, OpenAlex, Europe PMC, Preprints) "
                    "und schreibt semantic_scholar_results.json/.csv für literature_download.py.",
        epilog="Beispiele:\n"
               "  export SEMANTIC_SCHOLAR_API_KEY='...'\n"
               "  python literature_search.py\n"
               "  python literature_search.py --year-from 2018 --results-per-query 50\n"
               "  python literature_search.py --no-enrich      (nur Semantic Scholar, schnell)\n"
               "Danach: python literature_download.py semantic_scholar_results.json",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queries", type=Path, default=None,
                        help="Datei mit Suchstrings, einer pro Zeile, # = Kommentar "
                             "(Standard: search_queries.txt neben dem Skript)")
    parser.add_argument("--year-from", type=int, default=YEAR_FROM,
                        help=f"Frühestes Erscheinungsjahr (Standard: {YEAR_FROM})")
    parser.add_argument("--results-per-query", type=int, default=RESULTS_PER_QUERY,
                        help=f"Treffer je Suchstring (Standard: {RESULTS_PER_QUERY})")
    parser.add_argument("--email", default=CONTACT_EMAIL,
                        help="Kontakt-E-Mail für Unpaywall/OpenAlex (Standard: $CONTACT_EMAIL oder "
                             f"{CONTACT_EMAIL})")
    parser.add_argument("--no-enrich", action="store_true",
                        help="Keine zusätzliche PDF-Quellensuche (deutlich schneller, weniger Downloads)")
    parser.add_argument("--no-preprints", action="store_true",
                        help="Keine Preprint-Suche per Titel/Autoren (arXiv, OpenAlex, Semantic Scholar)")
    return parser.parse_args()


def main():
    global YEAR_FROM, RESULTS_PER_QUERY, CONTACT_EMAIL, ENRICH, SEARCH_PREPRINTS, HEADERS
    args = parse_args()
    YEAR_FROM = args.year_from
    RESULTS_PER_QUERY = args.results_per_query
    CONTACT_EMAIL = args.email
    ENRICH = not args.no_enrich
    SEARCH_PREPRINTS = not args.no_preprints
    HEADERS = {"User-Agent": f"literature-search/1.0 (mailto:{CONTACT_EMAIL or 'unknown'})"}

    script_directory = Path(__file__).resolve().parent
    query_file = args.queries or script_directory / "search_queries.txt"

    if not query_file.exists():
        raise FileNotFoundError(
            f"Die Datei {query_file} wurde nicht gefunden."
        )

    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")

    if not api_key:
        raise ValueError(
            "SEMANTIC_SCHOLAR_API_KEY wurde nicht gefunden."
        )

    queries = load_search_queries(query_file)

    if not queries:
        raise ValueError(
            "search_queries.txt enthält keine Suchstrings."
        )

    print(f"Geladene Suchstrings: {len(queries)}")

    all_results = []

    for number, query in enumerate(queries, start=1):
        print()
        print(f"Suche {number}/{len(queries)}")
        print(query)

        results = search_semantic_scholar(
            query=query,
            api_key=api_key
        )

        print(f"Gefundene Treffer: {len(results)}")
        all_results.extend(results)

        if number < len(queries):
            time.sleep(PAUSE_BETWEEN_REQUESTS)

    unique_results = remove_duplicates(all_results)

    print()
    print(f"Treffer insgesamt: {len(all_results)}")
    print(f"Nach Dublettenprüfung: {len(unique_results)}")

    if ENRICH:
        if not CONTACT_EMAIL:
            print("Hinweis: CONTACT_EMAIL nicht gesetzt - Unpaywall wird übersprungen.")
        print("Suche zusätzliche PDF-Quellen (Unpaywall, OpenAlex, Europe PMC, arXiv-Preprints) ...")
        for number, paper in enumerate(unique_results, start=1):
            enrich_paper(paper, api_key)
            print(f"  [{number}/{len(unique_results)}] {len(paper['pdf_candidates'])} Quellen: "
                  f"{(paper.get('title') or '')[:60]}")
    else:
        for paper in unique_results:
            paper["pdf_candidates"] = []
            paper["has_pdf_candidate"] = bool(paper.get("open_access_pdf"))

    with_pdf = sum(1 for p in unique_results if p.get("has_pdf_candidate"))
    print(f"Mit mindestens einer PDF-Quelle: {with_pdf}/{len(unique_results)}")

    json_file = script_directory / "semantic_scholar_results.json"
    csv_file = script_directory / "semantic_scholar_results.csv"

    save_as_json(unique_results, json_file)
    save_as_csv(unique_results, csv_file)

    print()
    print("Recherche abgeschlossen.")
    print(f"JSON: {json_file.name}")
    print(f"CSV:  {csv_file.name}")

if __name__ == "__main__":
    main()
