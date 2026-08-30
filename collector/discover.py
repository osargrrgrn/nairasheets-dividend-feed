import html
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[1]
DEBUG_FILE = ROOT / "discovery_debug.json"

PROFILE_URL = (
    "https://ngxgroup.com/exchange/data/company-profile/"
    "?directory=companydirectory&symbol=MTNN"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CONNECT_TIMEOUT = 6
READ_TIMEOUT = 12
MAX_SCRIPTS = 20
MAX_SCRIPT_BYTES = 2_000_000
MAX_TOTAL_SECONDS = 75

SCRIPT_RE = re.compile(
    r"<script[^>]+src=[\"']([^\"']+)[\"']",
    re.I,
)

PDF_URL_RE = re.compile(
    r"https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^\"'<>\s\\]+?\.pdf",
    re.I,
)

CLUE_PATTERNS = (
    r"https?://[^\"'\s<>]+(?:ajax|api|graphql|disclos|document|company)[^\"'\s<>]*",
    r"/wp-admin/admin-ajax\.php",
    r"/wp-json/[^\"'\s<>]+",
    r"admin_url[^,\n]{0,250}",
    r"ajax_url[^,\n]{0,250}",
    r"ajaxurl[^,\n]{0,250}",
    r"jet_blog_ajax[^,\n]{0,350}",
    r"jet_engine[^,\n]{0,350}",
    r"jet-engine[^,\n]{0,350}",
    r"corporate[_ -]?disclos[^,\n]{0,350}",
    r"latest[_ -]?disclos[^,\n]{0,350}",
    r"Financial_NewsDocs[^,\n]{0,350}",
)

STRONG_NEGATIVE_TITLE_HINTS = (
    "pending litigation",
    "litigation update",
    "exit of ",
    "resignation of ",
    "appointment of ",
    "notification of transaction in own shares",
    "transaction in own shares",
    "share awards",
    "tr-1 notification",
    "notification from sustainable capital",
    "director dealings",
    "dealing in shares",
)


def _extract_clues(text: str):
    clues = set()
    if not text:
        return []

    decoded = html.unescape(text).replace("\\/", "/").replace("\\u002F", "/")

    for pattern in CLUE_PATTERNS:
        for match in re.findall(pattern, decoded, flags=re.I):
            if isinstance(match, tuple):
                match = " ".join(str(x) for x in match if x)
            match = re.sub(r"\s+", " ", str(match)).strip()
            if match:
                clues.add(match[:1000])

    return sorted(clues)


def _get(session, url):
    return session.get(
        url,
        headers=HEADERS,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        allow_redirects=True,
    )


def discover_official_pdfs():
    started = time.monotonic()
    debug = {
        "method": "ngx_ajax_endpoint_diagnostic_patch_14",
        "profile_url": PROFILE_URL,
        "profile": {},
        "scripts": [],
        "all_clues": [],
        "pdfs_seen_directly": [],
    }

    print("NGX AJAX endpoint diagnostic started", flush=True)
    print("Inspecting MTNN company profile and its JavaScript...", flush=True)

    all_clues = set()
    direct_pdfs = set()

    with requests.Session() as session:
        try:
            response = _get(session, PROFILE_URL)
            page_text = response.text

            debug["profile"] = {
                "status": response.status_code,
                "final_url": response.url,
                "bytes": len(response.content),
                "content_type": response.headers.get("content-type", ""),
            }

            page_clues = _extract_clues(page_text)
            all_clues.update(page_clues)

            normalized = html.unescape(page_text).replace("\\/", "/")
            direct_pdfs.update(PDF_URL_RE.findall(normalized))

            script_urls = []
            for src in SCRIPT_RE.findall(page_text):
                full = urljoin(response.url, html.unescape(src))
                if full not in script_urls:
                    script_urls.append(full)

            def priority(url):
                low = url.lower()
                score = 0
                if "ngxgroup.com" in low:
                    score -= 10
                if any(x in low for x in (
                    "jet", "elementor", "ajax", "data", "company",
                    "custom", "main", "script"
                )):
                    score -= 5
                return score

            script_urls.sort(key=priority)
            script_urls = script_urls[:MAX_SCRIPTS]

            print(f"Linked scripts selected for inspection: {len(script_urls)}", flush=True)

            for i, script_url in enumerate(script_urls, start=1):
                if time.monotonic() - started > MAX_TOTAL_SECONDS:
                    print("Diagnostic time limit reached.", flush=True)
                    break

                record = {"url": script_url}
                try:
                    r = _get(session, script_url)
                    body = r.content[:MAX_SCRIPT_BYTES]
                    text = body.decode("utf-8", errors="ignore")

                    clues = _extract_clues(text)
                    all_clues.update(clues)

                    normalized_js = html.unescape(text).replace("\\/", "/")
                    direct_pdfs.update(PDF_URL_RE.findall(normalized_js))

                    record.update({
                        "status": r.status_code,
                        "bytes_inspected": len(body),
                        "content_type": r.headers.get("content-type", ""),
                        "clues": clues[:80],
                    })
                except Exception as exc:
                    record["error"] = repr(exc)

                debug["scripts"].append(record)

                if i % 5 == 0:
                    print(
                        f"Scripts inspected: {i}/{len(script_urls)} | "
                        f"endpoint clues: {len(all_clues)}",
                        flush=True,
                    )

        except Exception as exc:
            debug["profile"]["error"] = repr(exc)

    debug["all_clues"] = sorted(all_clues)[:300]
    debug["pdfs_seen_directly"] = sorted(direct_pdfs)[:100]
    debug["elapsed_seconds"] = round(time.monotonic() - started, 2)

    DEBUG_FILE.write_text(
        json.dumps(debug, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Diagnostic elapsed seconds: {debug['elapsed_seconds']}", flush=True)
    print(f"Candidate endpoint/config clues: {len(all_clues)}", flush=True)
    print(f"Direct disclosure PDF links exposed: {len(direct_pdfs)}", flush=True)
    print("Diagnostic written to discovery_debug.json", flush=True)

    return []


def title_is_strongly_irrelevant(title: str, url: str = "") -> bool:
    hay = f"{title} {url}".lower().replace("_", " ")
    return any(hint in hay for hint in STRONG_NEGATIVE_TITLE_HINTS)
