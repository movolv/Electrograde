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
from dataclasses import dataclass, field
from typing import List

from modules import ai_client, web_search


@dataclass
class SpecResult:
    product_name: str = ""
    brand: str = ""
    model: str = ""
    category: str = ""
    spec_summary: str = ""
    box_contents: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)


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

    raw_snippets = []
    sources = []
    for q in queries:
        for hit in web_search.search(q, max_results=3):
            url = hit.get("href") or hit.get("link")
            title = hit.get("title", "")
            body = hit.get("body", "")
            raw_snippets.append(f"SOURCE: {title}\n{body}")
            if url:
                page_text = web_search.fetch_page_text(url)
                if page_text:
                    raw_snippets.append(f"PAGE CONTENT ({url}):\n{page_text}")
                    sources.append(url)

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
        category=data.get("category", ""),
        spec_summary=data.get("spec_summary", ""),
        box_contents=list(data.get("box_contents", []) or []),
        sources=sources,
    )
