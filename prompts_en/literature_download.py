#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Download full-text PDFs for the results of literature_search.py.

WORKFLOW
  1. python literature_search.py                      (creates semantic_scholar_results.json)
  2. python literature_download.py semantic_scholar_results.json
  3. Open manual_downloads.html for the papers that could not be fetched automatically.
  4. Save those PDFs into manual_downloads/ (any file name) and run step 2 again.

WHAT THE SCRIPT DOES (stages, each only for papers still missing)
  A. Plain HTTP: tries every known source (Semantic Scholar, Unpaywall, OpenAlex, Europe PMC,
     arXiv/preprint matches, DOI) until a real PDF comes back.
  B. Browser stage: opens the remaining papers in a visible Google Chrome window (Playwright),
     which passes Cloudflare-type bot checks and uses your network/VPN/cookies, then follows
     the publisher's "PDF" link.
  C. Manual import: PDFs placed in manual_downloads/ are matched by DOI or title and moved
     into the output folder.
  D. Report: manual_downloads.html lists everything still missing, sorted by citation count,
     with clickable links (preprints first, then DOI/publisher, then search links).

SETUP (once)
  python3 -m venv .venv && . .venv/bin/activate
  python -m pip install requests beautifulsoup4 playwright pypdf
  Google Chrome must be installed (browser stage). pypdf is optional (better manual matching).

TIPS FOR MAXIMUM SUCCESS
  * Connect to the university VPN (full tunnel) BEFORE starting: most paywalled papers then open.
  * Keep the Chrome window open and do not click in it while the browser stage runs.
  * If a site asks for a login (Shibboleth/institution), log in once in that Chrome window; the
    session is kept in --browser-profile and reused in later runs.
  * To retry only the missing papers after changing network/VPN: add --browser-only.
  * Re-running is safe and fast: PDFs already in the output folder are skipped.

OUTPUT
  <outdir>/                 PDFs (Author_Year_Title_hash.pdf)
  <manifest>                JSON with metadata, status and notes per paper
  manual_downloads.html     to-do list for manual downloads (next to the manifest)

STATUS VALUES: downloaded, manual_check_required (page reached, no PDF), download_failed
  (network/HTTP errors, e.g. 403), no_source (nothing known).

Note: Open-access metadata does not replace checking the actual usage rights.
"""

import argparse
import base64
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sys
import time
import unicodedata
from urllib.parse import quote_plus, urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("Fehlende Pakete. Bitte ausführen: python -m pip install requests beautifulsoup4")

STOP = {"a", "an", "the", "of", "for", "and", "in", "on", "to", "with", "via",
        "from", "as", "by", "using", "use", "based", "study", "research", "article"}
OA_STATUSES = {"GOLD", "GREEN", "HYBRID", "BRONZE"}


_builtin_print = print


def print(*args, **kwargs):  # noqa: A001
    """Playwright can switch a piped stdout to non-blocking mode; retry instead of crashing."""
    for _ in range(200):
        try:
            return _builtin_print(*args, **kwargs)
        except BlockingIOError:
            time.sleep(0.05)


def read_results(path):
    """Accept a JSON list or a typical API response containing a result list."""
    with path.open(encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = next((data[key] for key in ("data", "results", "papers")
                     if isinstance(data.get(key), list)), None)
    if not isinstance(data, list):
        raise ValueError("Erwartet wird eine JSON-Liste oder ein Objekt mit 'data', 'results' oder 'papers' als Liste.")
    return [record for record in data if isinstance(record, dict)]


def normalize(raw):
    """Normalize field names from both Semantic Scholar data formats."""

    # An open-access PDF can be either:
    # - a dictionary from the original Semantic Scholar API, or
    # - a string in our normalized search results.
    oa_raw = raw.get("openAccessPdf") or raw.get("open_access_pdf")

    if isinstance(oa_raw, dict):
        pdf_url = oa_raw.get("url")
        oa_status = (oa_raw.get("status") or "").upper()
    elif isinstance(oa_raw, str):
        pdf_url = oa_raw
        oa_status = ""
    else:
        pdf_url = None
        oa_status = ""

    # If a separate pdf_url field already exists, it takes precedence.
    pdf_url = raw.get("pdf_url") or pdf_url

    authors = []
    for author in raw.get("authors") or []:
        name = author.get("name", "") if isinstance(author, dict) else str(author)
        if name:
            authors.append(name)

    external_ids = raw.get("externalIds") or {}
    if not isinstance(external_ids, dict):
        external_ids = {}

    oa_flag = (
        raw.get("isOpenAccess") is True
        or raw.get("is_open_access") is True
        or raw.get("fulltext_status") == "open_access_pdf_available"
        or oa_status in OA_STATUSES
        or bool(pdf_url)
    )

    return {
        "paper_id": raw.get("paper_id") or raw.get("paperId"),
        "title": raw.get("title") or "Ohne Titel",
        "authors": authors,
        "year": raw.get("year"),
        "venue": raw.get("venue"),
        "doi": raw.get("doi") or external_ids.get("DOI"),
        "abstract": raw.get("abstract"),
        "citation_count": raw.get("citation_count") or raw.get("citationCount") or 0,
        "pdf_url": pdf_url,
        "pdf_candidates": [c for c in raw.get("pdf_candidates") or [] if isinstance(c, dict)],
        "oa_evidence": bool(oa_flag),
    }


def clean(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def pdf_filename(paper):
    author = paper["authors"][0].split()[-1] if paper["authors"] else "Unbekannt"
    words = [word for word in re.findall(r"[A-Za-z0-9]+", paper["title"])
             if word.lower() not in STOP]
    label = clean(f"{author}_{paper['year'] or 'oJ'}_{'_'.join(words[:5])}")[:85]
    # Add a unique suffix, including when author, year, and title prefix match.
    unique = paper["doi"] or paper["paper_id"] or paper["pdf_url"] or paper["title"]
    suffix = hashlib.sha256(str(unique).encode("utf-8")).hexdigest()[:8]
    return f"{label or 'Paper'}_{suffix}.pdf"


def valid_web_url(url):
    return isinstance(url, str) and urlparse(url).scheme in ("http", "https")


def browser_headers(extra=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }
    if extra:
        headers.update(extra)
    return headers


def is_real_pdf(response):
    # The PDF file header is more reliable than the HTTP Content-Type alone.
    return b"%PDF-" in response.content[:1024]


def extract_pdf_candidates_from_text(text):
    candidates = []
    pattern = re.compile(r"https?://[^\s\"'<>]+\.pdf(?:\?[^\s\"'<>]*)?", re.IGNORECASE)
    for match in pattern.finditer(text):
        candidates.append(match.group(0).rstrip(').,;'))
    return candidates


def pdf_link_from_html(response):
    """Search for a PDF on the landing page if the link initially opens one."""
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "html" not in content_type and not response.text.lstrip().lower().startswith(("<!doctype", "<html")):
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    seen = set()

    for key in ("citation_pdf_url", "citation_pdf_url", "eprints.document_url", "wkhealth_pdf_url"):
        for tag in soup.find_all("meta", attrs={"name": key}):
            content = tag.get("content")
            if content:
                candidate = urljoin(response.url, content)
                if valid_web_url(candidate) and candidate not in seen:
                    seen.add(candidate)
                    return candidate

    for tag in soup.select("link[rel=alternate][href], link[as=pdf][href], a[href]"):
        href = tag.get("href")
        if href:
            candidate = urljoin(response.url, href)
            if valid_web_url(candidate) and candidate not in seen:
                text = (tag.get("title") or tag.get_text(" ", strip=True) or "").lower()
                if candidate.lower().endswith(".pdf") or "/pdf" in candidate.lower() or "pdf" in text:
                    return candidate

    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        label = tag.get_text(" ", strip=True).lower()
        if href.lower().endswith(".pdf") or "/pdf" in href.lower() or ("download" in href.lower() and "pdf" in label):
            candidate = urljoin(response.url, href)
            if valid_web_url(candidate) and candidate not in seen:
                return candidate

    for candidate in extract_pdf_candidates_from_text(response.text):
        if valid_web_url(candidate):
            return candidate
    return None


def normalize_candidate_url(url):
    """Rewrite known landing-page URLs to their direct PDF equivalents."""
    match = re.match(r"https?://(?:www\.)?arxiv\.org/(?:abs|html)/(.+?)(?:v\d+)?/?$", url)
    if match:
        return f"https://arxiv.org/pdf/{match.group(1)}"
    match = re.match(r"https?://(?:www\.)?ncbi\.nlm\.nih\.gov/pmc/articles/(?:PMC)?(\d+)/?$", url) or \
        re.match(r"https?://pmc\.ncbi\.nlm\.nih\.gov/articles/(?:PMC)?(\d+)/?$", url)
    if match:
        return f"https://europepmc.org/backend/ptpmcrender.fcgi?accid=PMC{match.group(1)}&blobtype=pdf"
    return url


def candidate_urls(paper):
    """All distinct URLs worth trying, direct-PDF sources first, DOI landing page last."""
    urls = [c["url"] for c in paper.get("pdf_candidates") or []]
    urls.append(paper.get("pdf_url"))
    if paper.get("doi"):
        urls.append(f"https://doi.org/{paper['doi']}")
    ordered = []
    for url in urls:
        if valid_web_url(url):
            url = normalize_candidate_url(url)
            if url not in ordered:
                ordered.append(url)
    return ordered


def get_session():
    session = requests.Session()
    session.headers.update(browser_headers())
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                    respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    return session


def fetch_pdf(session, url, output_file):
    """Fetch url (and one PDF link found on a landing page). Returns (ok, message)."""
    try:
        response = session.get(url, timeout=40, allow_redirects=True, headers=browser_headers())
        response.raise_for_status()
        if is_real_pdf(response):
            output_file.write_bytes(response.content)
            return True, f"PDF gespeichert; Quelle: {response.url}"
        followup = pdf_link_from_html(response)
        if followup and followup != url:
            second = session.get(followup, timeout=40, allow_redirects=True,
                                 headers=browser_headers({"Referer": response.url}))
            second.raise_for_status()
            if is_real_pdf(second):
                output_file.write_bytes(second.content)
                return True, f"PDF über Landingpage gespeichert; Quelle: {second.url}"
        return False, f"{url}: Landingpage ohne abrufbares PDF"
    except requests.RequestException as exc:
        return False, f"{url}: {exc}"
    except OSError as exc:
        return False, f"{url}: Dateifehler {exc}"


def save_pdf(session, paper, output_file):
    """Try every known source until one yields a real PDF."""
    urls = candidate_urls(paper)
    if not urls:
        return "no_source", "Keine PDF-Quelle bekannt."
    errors = []
    for url in urls:
        ok, message = fetch_pdf(session, url, output_file)
        if ok:
            return "downloaded", message
        errors.append(message)
    reached_pages = [e for e in errors if "Landingpage" in e]
    status = "manual_check_required" if reached_pages else "download_failed"
    return status, " | ".join(errors)[:600]


def manual_links(entry):
    """Ordered, de-duplicated (label, url) links a human can click, most promising first."""
    links = []

    def add(label, url):
        if valid_web_url(url) and all(url != u for _, u in links):
            links.append((label, url))

    for cand in entry.get("pdf_candidates") or []:
        if cand.get("kind") == "preprint":
            add(f"Preprint ({cand.get('source')})", normalize_candidate_url(cand["url"]))
    if entry.get("doi"):
        add("DOI / Verlagsseite", f"https://doi.org/{entry['doi']}")
    for cand in entry.get("pdf_candidates") or []:
        if "ptpmcrender" not in cand["url"]:
            add(f"OA-Quelle ({cand.get('source')})", normalize_candidate_url(cand["url"]))
    title = quote_plus(entry["title"])
    add("Google Scholar", f"https://scholar.google.com/scholar?q={title}")
    add("arXiv-Suche", f"https://arxiv.org/search/?query={title}&searchtype=title")
    add("Unpaywall-Suche (CORE)", f"https://core.ac.uk/search?q=%22{title}%22")
    return links


def write_manual_report(entries, outdir, path):
    """HTML page listing all papers needing human action, sorted by citation count."""
    todo = [e for e in entries if e["download_status"] != "downloaded"]
    todo.sort(key=lambda e: (-(e.get("citation_count") or 0), -(e.get("year") or 0)))
    rows = []
    for number, e in enumerate(todo, 1):
        links = " ".join(f'<a href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(l)}</a>'
                         for l, u in manual_links(e))
        authors = ", ".join(e["authors"][:3]) + (" et al." if len(e["authors"]) > 3 else "")
        rows.append(
            f"<tr><td>{number}</td><td><b>{html.escape(e['title'])}</b><br>"
            f"{html.escape(authors)} ({e.get('year') or 'o.J.'}), {html.escape(str(e.get('venue') or ''))}"
            f"<br><small>Zitierungen: {e.get('citation_count') or 0} · Status: {e['download_status']}</small></td>"
            f"<td class=l>{links}</td>"
            f"<td><code>{html.escape(e['local_pdf_filename'])}</code></td></tr>")
    page = f"""<!doctype html><meta charset="utf-8"><title>Manuelle Downloads</title>
<style>body{{font:14px sans-serif;margin:2em}}table{{border-collapse:collapse}}td{{border:1px solid #ccc;
padding:6px;vertical-align:top}}.l a{{display:block;margin:2px 0}}code{{font-size:12px}}</style>
<h1>{len(todo)} Paper brauchen manuelle Aktion</h1>
<p>Sortiert nach Zitierungen. PDF öffnen/herunterladen und <b>in den Ordner <code>{html.escape(str(outdir))}</code>
unter dem rechts angegebenen Dateinamen</b> speichern (oder in <code>manual_downloads/</code> legen und das
Skript erneut starten: es ordnet die PDFs per DOI/Titel zu; beliebige Dateinamen sind okay). Der erneute Lauf überspringt vorhandene PDFs.</p>
<table><tr><th>#</th><th>Paper</th><th>Links</th><th>Dateiname</th></tr>{''.join(rows)}</table>"""
    path.write_text(page, encoding="utf-8")
    return len(todo)


def pdf_match_text(pdf):
    """File name plus (if pypdf is available) text of the first two pages."""
    text = pdf.stem
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf))
        text += " " + " ".join((page.extract_text() or "") for page in reader.pages[:2])
    except Exception:
        pass
    return text


def import_manual_pdfs(entries, outdir, folder):
    """Match PDFs a human dropped into `folder` to papers by DOI or title words (file name / first pages)."""
    if not folder.is_dir():
        return 0
    imported = 0
    for pdf in folder.glob("*.pdf"):
        with pdf.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                continue
        text = pdf_match_text(pdf)
        stem = set(re.findall(r"[a-z0-9]{4,}", clean(text).lower()))
        best, best_score = None, 0.0
        for e in entries:
            if e["download_status"] == "downloaded":
                continue
            if e.get("doi") and e["doi"].lower() in text.lower():
                best, best_score = e, 1.0
                break
            words = {w for w in re.findall(r"[a-z0-9]{4,}", clean(e["title"]).lower())}
            score = len(words & stem) / max(len(words), 1)
            if score > best_score:
                best, best_score = e, score
        if best and best_score >= 0.8:
            shutil.move(str(pdf), outdir / best["local_pdf_filename"])
            best["download_status"], best["note"] = "downloaded", "Manuell heruntergeladen und importiert."
            imported += 1
        else:
            print(f"  Nicht zugeordnet: {pdf.name}")
    return imported


def browser_pdf_links(page):
    """PDF links on a rendered page, best first: citation meta tag, then 'PDF' links; no review/supplement files."""
    found = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('meta[name=citation_pdf_url],meta[name="eprints.document_url"]')
            .forEach(m => out.push(m.content));
        document.querySelectorAll('a[href]').forEach(a => {
            const s = (a.href + ' ' + a.innerText + ' ' + (a.getAttribute('aria-label') || '') + ' ' + a.className).toLowerCase();
            if (/pdf|download|fulltext/.test(s) && !/review|supplement|epub|reference|\\.zip|\\.docx?|xlsx/.test(s)) out.push(a.href);
        });
        return out;
    }""")
    links = []
    for url in found:
        url = urljoin(page.url, url)
        if valid_web_url(url) and url not in links:
            links.append(url)
    # explicit '.pdf' / '/pdf' URLs first
    links.sort(key=lambda u: 0 if re.search(r"\.pdf($|\?)|/pdf", u.lower()) else 1)
    return links[:6]


def fetch_in_page(page, url):
    """Fetch url from inside the loaded page (same cookies/session/origin as a human click). Returns bytes or None."""
    try:
        encoded = page.evaluate("""async (u) => {
            const r = await fetch(u, {credentials: 'include'});
            if (!r.ok) return null;
            const bytes = new Uint8Array(await r.arrayBuffer());
            let bin = '';
            for (let i = 0; i < bytes.length; i += 32768) bin += String.fromCharCode(...bytes.subarray(i, i + 32768));
            return btoa(bin);
        }""", url)
    except Exception:
        return None
    return base64.b64decode(encoded) if encoded else None


def browser_stage(entries, outdir, profile_dir, wait_seconds=30):
    """Open the remaining papers in a real Chrome (passes Cloudflare, uses VPN/IP/cookies) and fetch the PDF."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Browser-Stufe übersprungen: python -m pip install playwright (und Google Chrome installiert).")
        return 0
    todo = [e for e in entries if e["download_status"] != "downloaded"]
    if not todo:
        return 0
    print(f"\nBrowser-Stufe: {len(todo)} Paper werden in Chrome geöffnet (Fenster bitte offen lassen) ...")
    saved = 0
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir), channel="chrome", headless=False, accept_downloads=True,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for number, entry in enumerate(todo, 1):
            target = outdir / entry["local_pdf_filename"]
            urls = candidate_urls(entry)
            # try DOI page first in the browser, it carries the publisher's PDF link
            urls.sort(key=lambda u: 0 if "doi.org/" in u else 1)
            done = False
            for url in urls[:4]:
                try:
                    page.goto(url, wait_until="commit", timeout=45000)
                    for _ in range(wait_seconds // 2):
                        time.sleep(2)
                        if not re.search(r"just a moment|verify|checking your browser|attention required",
                                         page.title().lower()):
                            break
                    time.sleep(1.5)
                    pdf_urls = [page.url] + browser_pdf_links(page)
                    for pdf_url in pdf_urls:
                        body = fetch_in_page(page, pdf_url)
                        if body is None:
                            response = ctx.request.get(pdf_url, headers={"Referer": page.url}, timeout=60000)
                            body = response.body() if response.ok else None
                        if body and b"%PDF-" in body[:1024]:
                            target.write_bytes(body)
                            entry["download_status"] = "downloaded"
                            entry["note"] = f"Per Browser geladen; Quelle: {pdf_url}"
                            done = True
                            break
                except Exception as exc:
                    entry["note"] = f"Browser: {str(exc)[:150]} | " + entry["note"]
                if done:
                    break
            saved += done
            print(f"  [{number}/{len(todo)}] {'downloaded' if done else 'offen'}: {entry['title'][:65]}", flush=True)
        ctx.close()
    return saved


def main():
    parser = argparse.ArgumentParser(
        description="Open-Access-PDFs aus Semantic-Scholar-Treffern herunterladen. "
                    "Stufen: HTTP-Download -> Chrome-Browser -> Import manuell geladener PDFs -> HTML-Liste offener Paper.",
        epilog="Beispiele:\n"
               "  python literature_download.py semantic_scholar_results.json\n"
               "  python literature_download.py semantic_scholar_results.json --browser-only   (nach VPN-Wechsel)\n"
               "  python literature_download.py semantic_scholar_results.json --no-browser     (nur HTTP, schnell)\n"
               "Details: siehe Kopf der Datei literature_download.py.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="JSON-Datei mit Semantic-Scholar-Suchergebnissen")
    parser.add_argument("outdir", nargs="?", type=Path, default=Path("paper_reader_corpus"),
                        help="Zielordner für PDFs (Standard: paper_reader_corpus)")
    parser.add_argument("manifest", nargs="?", type=Path,
                        default=Path("paper_reader_fulltext_manifest_downloaded.json"),
                        help="Ausgabedatei für bibliografische Daten und Downloadstatus")
    parser.add_argument("--manual-dir", type=Path, default=Path("manual_downloads"),
                        help="Ordner, in den manuell geladene PDFs gelegt werden (werden automatisch zugeordnet)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Browser-Stufe (echtes Chrome via Playwright) überspringen")
    parser.add_argument("--browser-only", action="store_true",
                        help="Nur die Browser-Stufe für noch fehlende PDFs ausführen (schneller Wiederholungslauf)")
    parser.add_argument("--browser-profile", type=Path, default=Path(".browser_profile"),
                        help="Chrome-Profilordner (speichert Cookies/Cloudflare-Freigaben)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Optional: nur die ersten N Open-Access-Kandidaten verarbeiten")
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit muss mindestens 1 sein")
    try:
        raw_results = read_results(args.source)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"Eingabedatei konnte nicht gelesen werden: {exc}")

    papers = [normalize(record) for record in raw_results]
    # Every paper is processed: papers without any source end up in the manual list with search links.
    candidates = papers
    skipped = sum(1 for p in papers if not p["pdf_candidates"] and not valid_web_url(p["pdf_url"]) and not p.get("doi"))

    # Remove duplicate results, which may occur when search strings overlap.
    seen = set()
    unique_papers = []
    for p in candidates:
        key = str(p["paper_id"] or p["doi"] or p["pdf_url"])
        if key not in seen:
            seen.add(key)
            unique_papers.append(p)
    duplicates = len(candidates) - len(unique_papers)
    if args.limit is not None:
        unique_papers = unique_papers[:args.limit]

    args.outdir.mkdir(parents=True, exist_ok=True)
    session = get_session()
    manifest = []
    for index, paper in enumerate(unique_papers, 1):
        filename = pdf_filename(paper)
        entry = {key: paper[key] for key in (
            "paper_id", "title", "authors", "year", "venue", "doi", "abstract", "pdf_url", "pdf_candidates",
            "citation_count")}
        entry.update({"id": index, "local_pdf_filename": filename,
                      "download_status": "manual_check_required", "note": ""})
        output_path = args.outdir / filename
        already_downloaded = False
        if output_path.is_file():
            with output_path.open("rb") as existing:
                already_downloaded = existing.read(5) == b"%PDF-"
        if already_downloaded:
            status, note = "downloaded", "Bereits vorhanden; erneuter Download ausgelassen."
        else:
            if args.browser_only:
                status, note = "manual_check_required", ""
            else:
                status, note = save_pdf(session, paper, output_path)
                time.sleep(0.5)
        entry["download_status"] = status
        entry["note"] = note
        manifest.append(entry)
        print(f"[{index}/{len(unique_papers)}] {status}: {paper['title'][:65]}")

    if not args.no_browser:
        gained = browser_stage(manifest, args.outdir, args.browser_profile)
        print(f"Zusätzlich per Browser geladen: {gained}")

    imported = import_manual_pdfs(manifest, args.outdir, args.manual_dir)
    if imported:
        print(f"\nManuell heruntergeladene PDFs zugeordnet: {imported}")

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    report = args.manifest.with_name("manual_downloads.html")
    open_count = write_manual_report(manifest, args.outdir, report)
    args.manual_dir.mkdir(exist_ok=True)
    manual_lines = [e for e in manifest if e["download_status"] != "downloaded"]
    manual_lines.sort(key=lambda e: (-(e.get("citation_count") or 0), -(e.get("year") or 0)))
    print(f"\nManuelle Aktion nötig: {open_count} Paper -> öffne {report.resolve()}")
    for e in manual_lines[:10]:
        print(f"  - {e['title'][:70]} | https://doi.org/{e['doi']}" if e.get("doi") else f"  - {e['title'][:70]}")

    counts = Counter(item["download_status"] for item in manifest)
    print(f"\nTreffer in der Eingabedatei: {len(papers)}")
    print(f"Ohne geeigneten OA-PDF-Nachweis/-Link: {skipped}")
    print(f"Doppelte Treffer entfernt: {duplicates}")
    print(f"Verarbeitete Kandidaten: {len(manifest)}")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")
    print(f"PDF-Ordner: {args.outdir}")
    print(f"Manifest:   {args.manifest}")
    print("Hinweis: OA-Metadaten ersetzen keine Prüfung der konkreten Nutzungsrechte.")


if __name__ == "__main__":
    main()
