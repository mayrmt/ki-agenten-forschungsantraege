"""Simplified seminar version of a literature search with Semantic Scholar.

Requirements:
1. Python 3
2. The "requests" package
3. The SEMANTIC_SCHOLAR_API_KEY environment variable
4. A "search_queries.txt" file in the same directory

The search_queries.txt file contains one search string per line.
Blank lines and lines beginning with # are ignored.
"""

import os
import json
import csv
import time
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

YEAR_FROM = 2010
RESULTS_PER_QUERY = 10
PAUSE_BETWEEN_REQUESTS = 2

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"


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
        )
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
            "doi": external_ids.get("DOI"),
            "url": paper.get("url"),
            "is_open_access": paper.get("isOpenAccess"),
            "open_access_pdf": open_access_pdf.get("url"),
            "search_query": query
        })

    return results


# ---------------------------------------------------------------------------
# Remove duplicates
# ---------------------------------------------------------------------------

def remove_duplicates(results):
    """Remove duplicate results based on DOI or title."""

    seen = set()
    unique_results = []

    for paper in results:
        doi = paper.get("doi")
        title = paper.get("title")

        if doi:
            key = doi.lower().strip()
        elif title:
            key = title.lower().strip()
        else:
            continue

        if key not in seen:
            seen.add(key)
            unique_results.append(paper)

    return unique_results


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

def main():
    script_directory = Path(__file__).resolve().parent
    query_file = script_directory / "search_queries.txt"

    if not query_file.exists():
        raise FileNotFoundError(
            "Die Datei search_queries.txt wurde nicht gefunden."
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
