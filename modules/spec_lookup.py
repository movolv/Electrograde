"""Zero-manual-database spec lookup.

Flow: take whichever identifiers are available (EAN/barcode, ASIN, manifest
item description, manually-typed model number) -> search the web
(DuckDuckGo, no API key needed) -> scrape top result pages for raw text ->
ask Claude to distill that into brand/model/name/category/specs/box-contents.

This is best-effort: web scraping is inherently fragile against site
structure changes, and a manifest's EAN/description is an unverified claim,
not ground truth — the result here is later cross-checked against the
actual photographed item in modules/vision_grading.py. If nothing usable is
found, the caller should fall back to manual entry in the UI.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Tuple

from modules import ai_client, web_search


@dataclass
class SpecResult:
    product_name: str = ""
    brand: str = ""
    model: str = ""
    category: str = ""
    power: str = ""  # e.g. "1200W" — empty if not found/not applicable
    spec_summary: str = ""
    box_contents: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)


@dataclass
class SpecPreview:
    """Deliberately a SEPARATE type from SpecResult — a quick_guess() value
    is never written to st.session_state.spec_result or any product.* field,
    only shown in the UI as an unconfirmed candidate while lookup() (the
    unchanged, AI-backed "deep" path) runs in the background."""
    product_name_guess: str = ""
    brand_guess: str = ""
    model_guess: str = ""


def quick_guess(hits: list) -> SpecPreview:
    """LEVEL 1: no Claude, no page fetch — a rough heuristic straight off
    the top web_search.search() result's title, for an instant "candidate
    found (unconfirmed)" UI preview only. lookup() below remains the only
    function whose result is ever actually kept."""
    if not hits:
        return SpecPreview()
    title = (hits[0].get("title") or "").strip()
    if not title:
        return SpecPreview()
    words = title.split()
    brand_guess = words[0] if words else ""
    # First subsequent word containing a digit is usually a model number
    # (e.g. "Bosch MS8CM6110 Hand Blender" -> "MS8CM6110") — rough, but this
    # is preview-only and never written to a canonical field.
    model_guess = next((w for w in words[1:] if any(ch.isdigit() for ch in w)), "")
    return SpecPreview(product_name_guess=title, brand_guess=brand_guess, model_guess=model_guess)


def _gather_query_snippets(query: str, max_results: int = 3) -> Tuple[List[str], List[str]]:
    """One query's worth of search + page-fetch, gathered the exact same
    way lookup() always has (same SOURCE/PAGE CONTENT formatting, same
    "only count a source if its page actually returned text" rule) — pulled
    out so lookup() can run this once per query CONCURRENTLY instead of one
    query fully finishing before the next starts."""
    raw: List[str] = []
    sources: List[str] = []
    hits = web_search.search(query, max_results=max_results)
    urls = [h.get("href") or h.get("link") for h in hits if (h.get("href") or h.get("link"))]
    pages = web_search.fetch_pages_parallel(urls) if urls else {}
    for hit in hits:
        url = hit.get("href") or hit.get("link")
        title = hit.get("title", "")
        body = hit.get("body", "")
        raw.append(f"SOURCE: {title}\n{body}")
        if url:
            page_text = pages.get(url, "")
            if page_text:
                raw.append(f"PAGE CONTENT ({url}):\n{page_text}")
                sources.append(url)
    return raw, sources


def lookup(
    ean: str = "",
    asin: str = "",
    model_number: str = "",
    item_description: str = "",
) -> SpecResult:
    """Look up product specs from whichever identifiers are available.

    At least one of ean/asin/model_number/item_description should be given;
    all available ones are combined into the search query for best results.
    """
    primary = ean or asin or model_number or item_description
    if not primary.strip():
        return SpecResult()

    query_terms = " ".join(t for t in [ean, asin, model_number, item_description] if t.strip())
    queries = [
        f"{query_terms} specifications",
        f"{primary} what's in the box",
    ]

    # The two queries are independent (different search terms, no data
    # dependency between them) — run them concurrently, and fetch each
    # query's hit pages concurrently too (fetch_pages_parallel), instead of
    # one search-then-fetch-each-page-in-turn chain. Same queries, same
    # max_results, same page-fetch behavior/content as before — this only
    # changes the ORDER work happens in, not what's gathered.
    raw_snippets = []
    sources = []
    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        for raw, srcs in executor.map(_gather_query_snippets, queries):
            raw_snippets.extend(raw)
            sources.extend(srcs)

    if not raw_snippets:
        return SpecResult(sources=[])

    combined = "\n\n---\n\n".join(raw_snippets)[:12000]

    system = (
        "You extract structured product data from messy web search results "
        "about a consumer electronics item. The identifiers given to you "
        "(EAN/barcode, ASIN, description) may be INCORRECT or mismatched — "
        "they come from an unverified liquidation manifest, not a trusted "
        "catalog. Use your judgement; do not force an answer if the search "
        "results are inconsistent or clearly about a different product. "
        "Respond with STRICT JSON only, no markdown fences, matching this "
        "schema exactly:\n"
        '{"product_name": str, "brand": str, "model": str, "category": str, '
        '"power": str (e.g. "1200W" — the product\'s rated power/wattage, '
        'empty string if not found or not applicable to this product type), '
        '"spec_summary": str (2-4 sentences, plain English, key specs only), '
        '"box_contents": [str, ...] (standard included accessories/components)}\n'
        "If information is not present in the text, use your best general "
        "knowledge of this exact model, but never invent implausible specifics. "
        "All text must be in English."
    )
    user = (
        f"EAN/barcode: {ean or 'unknown'}\n"
        f"ASIN: {asin or 'unknown'}\n"
        f"Manual model number: {model_number or 'unknown'}\n"
        f"Manifest item description: {item_description or 'unknown'}\n\n"
        f"Raw web research:\n{combined}"
    )

    try:
        data = ai_client.ask_json(system, user)
    except Exception:
        return SpecResult(sources=sources)

    return SpecResult(
        product_name=data.get("product_name", ""),
        brand=data.get("brand", ""),
        model=data.get("model", ""),
        power=data.get("power", ""),
        category=data.get("category", ""),
        spec_summary=data.get("spec_summary", ""),
        box_contents=list(data.get("box_contents", []) or []),
        sources=sources,
    )
