"""Shared web-search plumbing used by spec_lookup.py, identifier_lookup.py,
and pricing.py — search + full-page-text fetching lived as three near-
identical copies before this; centralized here so a future search-backend
swap (like the duckduckgo_search -> ddgs migration) only needs to happen
once.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ElectroGraderBot/1.0)"}

# `ddgs` scrapes DuckDuckGo's HTML results — unlike modules/marketplaces/
# baselinker (a real per-token rate-limited API), there's no per-tenant
# account/token here, just this one process's shared outbound IP. At the
# 100-company scale target, many companies clicking "Fetch specs" at once
# could otherwise fire dozens of concurrent DDGS requests and get the
# WHOLE server rate-limited/blocked by DuckDuckGo — not just the company
# that "caused" it. This throttle is deliberately GLOBAL (one shared timer
# for the whole process, not one per company) for that reason. The
# lookup_cache_store cache means most repeat lookups never reach this at
# all, so the cost of throttling here is paid rarely in practice.
_ddgs_lock = threading.Lock()
_last_ddgs_call_at = 0.0
_MIN_DDGS_INTERVAL = 0.5  # max ~2 DDGS requests/second for the whole process


def _ddgs_rate_limit_wait() -> None:
    global _last_ddgs_call_at
    with _ddgs_lock:
        elapsed = time.time() - _last_ddgs_call_at
        if elapsed < _MIN_DDGS_INTERVAL:
            time.sleep(_MIN_DDGS_INTERVAL - elapsed)
        _last_ddgs_call_at = time.time()


# ddgs's "auto" backend shuffles which of its ~9 underlying search engines
# it tries on each call and gives up after a bounded number of them —
# confirmed empirically (scripts/verify-style probing, not guessed) that
# the exact same query can return 0 results on one call and 5 solid results
# moments later on an identical retry, purely because a different random
# subset of engines got tried. Measured empty-result rate across 8 varied
# product EAN queries: 50%. _RETRY_EMPTY_ATTEMPTS below exists specifically
# to absorb that — it is NOT a fallback for "no data exists", only for
# "this particular attempt happened to draw an unlucky engine subset".
_RETRY_EMPTY_ATTEMPTS = 2  # extra attempts beyond the first, only if empty


def search(query: str, max_results: int = 5, retry_on_empty: bool = False) -> List[dict]:
    """Returns raw ddgs.text() hit dicts (each with 'title'/'href'/'body').

    retry_on_empty: retries up to _RETRY_EMPTY_ATTEMPTS more times if the
    result comes back empty (never if it comes back with results, however
    few). Deliberately opt-in, not the default: it trades a few extra
    seconds (paid only on the already-empty, worst-case path) for a better
    find-rate, which is the right tradeoff for the deep/background
    identifier lookup (identifier_lookup.find_identifiers, not user-
    blocking) but NOT for the synchronous Fast-First Level 1 layer, whose
    whole point is a fast, bounded time-to-first-result — callers there
    (identifier_lookup.fast_ensure_identifiers, app.py's _run_fast_layer)
    deliberately leave this at its default False."""
    max_attempts = 1 + (_RETRY_EMPTY_ATTEMPTS if retry_on_empty else 0)
    hits: List[dict] = []
    for attempt in range(max_attempts):
        _ddgs_rate_limit_wait()
        try:
            with DDGS() as ddgs:
                hits = list(ddgs.text(query, max_results=max_results))
        except Exception:
            hits = []
        if hits:
            break
    return hits


def fetch_page_text(url: str, max_chars: int = 4000) -> str:
    """Fetches a URL and returns its visible text, stripped of script/style/
    nav/footer/header noise — much more content for the AI to work with
    than a search snippet alone."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return text[:max_chars]
    except Exception:
        return ""


def fetch_pages_parallel(urls: List[str], max_chars: int = 4000) -> Dict[str, str]:
    """Same fetch_page_text() per URL, just concurrent instead of
    sequential — part of the Fast-First lookup redesign's Level 2 (fetch
    only the top few candidate URLs, but don't make the caller wait for
    them one at a time). Returns {url: text}; a URL that failed to fetch is
    simply omitted rather than mapped to "", so callers can tell the
    difference between "fetched, empty" and "never came back"."""
    if not urls:
        return {}
    results: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as executor:
        future_to_url = {executor.submit(fetch_page_text, url, max_chars): url for url in urls}
        for future in future_to_url:
            url = future_to_url[future]
            text = future.result()
            if text:
                results[url] = text
    return results
