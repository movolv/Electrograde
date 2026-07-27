"""Shared web-search plumbing used by spec_lookup.py, identifier_lookup.py,
and pricing.py — search + full-page-text fetching lived as three near-
identical copies before this; centralized here so a future search-backend
swap (like the duckduckgo_search -> ddgs migration) only needs to happen
once.
"""
from typing import List

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ElectroGraderBot/1.0)"}


def search(query: str, max_results: int = 5) -> List[dict]:
    """Returns raw ddgs.text() hit dicts (each with 'title'/'href'/'body')."""
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception:
        return []


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
