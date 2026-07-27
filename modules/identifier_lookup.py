"""Automatic EAN/GTIN and Amazon ASIN discovery.

Runs automatically — never behind a dedicated "search" button of its own:
  - Right after a manifest import, for any row missing EAN or ASIN.
  - Right after spec lookup for a manually-started item (once brand/model/
    name become known) — see app.py Step 3.

Hard rules, enforced here rather than left to the AI's discretion:
  - NEVER invents/generates an EAN or ASIN — a value is only ever returned if
    it was explicitly present in real web search results, and passes a basic
    shape check (EAN: 8/12/13/14 numeric digits; ASIN: 10 alphanumeric chars).
  - NEVER overwrites an EAN/ASIN that's already populated (from the
    manifest, a physical scan, or a prior lookup) — `ensure_identifiers`
    only ever fills in what's currently blank.
  - NEVER auto-picks a single "best guess" ASIN when multiple plausible
    candidates exist (or even a single low-confidence one) — `asin` is only
    ever set when exactly one candidate was found with high confidence.
    Ambiguous candidates are kept in `asin_candidates` for a human to
    confirm and for search/future use, never promoted to the definitive
    field on their own. ASIN is treated as optional/best-effort throughout —
    it is intentionally left out of the Baselinker export (modules/export.py)
    since it isn't a required listing field.
  - Always records where a found value came from (`*_source`) and a
    status: "Found" | "Not Found" | "Needs Verification".

Fetches full page text for each search result (via modules/web_search.py),
not just the short DuckDuckGo snippet — the EAN/ASIN is frequently present
on the page itself but absent from the snippet, so this materially
improves the hit rate over snippet-only matching.
"""
from dataclasses import dataclass, field
from typing import List

from modules import ai_client, web_search
from modules.models import Product

STATUS_FOUND = "Found"
STATUS_NOT_FOUND = "Not Found"
STATUS_NEEDS_VERIFICATION = "Needs Verification"

_VALID_EAN_LENGTHS = (8, 12, 13, 14)


def looks_like_ean(value: str) -> bool:
    """True if value has the shape of a real EAN/GTIN (8/12/13/14 digits).
    Used both here and by app.py's manual-entry step, so a barcode typed by
    hand (e.g. when zbar/auto-decode isn't available) is recognized as an
    EAN immediately rather than sitting unrecognized in model_number."""
    return bool(value) and value.isdigit() and len(value) in _VALID_EAN_LENGTHS


@dataclass
class IdentifierResult:
    ean: str = ""
    ean_status: str = ""
    ean_source: str = ""
    asin: str = ""
    asin_status: str = ""
    asin_source: str = ""
    asin_candidates: List[str] = field(default_factory=list)


def find_identifiers(
    brand: str = "",
    model: str = "",
    product_name: str = "",
    other_info: str = "",
    need_ean: bool = True,
    need_asin: bool = True,
) -> IdentifierResult:
    """Web-searches for the EAN/ASIN of a product identified by whichever of
    brand/model/product_name/other_info are available. Model number is
    treated as the strongest signal. Returns blank/"Not Found" fields rather
    than guessing when nothing solid turns up."""
    # De-duplicate overlapping words (e.g. product_name already containing
    # brand+model, like "Bosch MS8CM6110 Hand Blender") — a query with a
    # repeated phrase like "Bosch MS8CM6110 Bosch MS8CM6110 Hand Blender"
    # was observed to return zero DuckDuckGo results even though the
    # product is real and findable; deduping words fixes this.
    seen_words = set()
    ordered_words = []
    for part in (brand, model, product_name, other_info):
        if not part or not part.strip():
            continue
        for word in part.strip().split():
            key = word.lower()
            if key not in seen_words:
                seen_words.add(key)
                ordered_words.append(word)
    query_terms = " ".join(ordered_words)
    if not query_terms or not (need_ean or need_asin):
        result = IdentifierResult()
        if need_ean:
            result.ean_status = STATUS_NOT_FOUND
        if need_asin:
            result.asin_status = STATUS_NOT_FOUND
        return result

    # Each need gets a primary query plus a shorter fallback — observed in
    # practice that an extra keyword (e.g. "GTIN") can flip a query from
    # several real results to zero on DuckDuckGo's scraped search for no
    # predictable reason, so a bounded retry with simpler wording recovers
    # cases the primary phrasing alone would miss.
    query_variants = []
    if need_ean:
        query_variants.append([f"{query_terms} EAN GTIN barcode", f"{query_terms} EAN barcode"])
    if need_asin:
        query_variants.append([f"{query_terms} amazon ASIN", f"{query_terms} amazon"])

    raw_snippets = []
    sources = []
    for variants in query_variants:
        hits = []
        for q in variants:
            hits = web_search.search(q)
            if hits:
                break
        for hit in hits:
            url = hit.get("href") or hit.get("link", "")
            body = hit.get("body", "")
            title = hit.get("title", "")
            raw_snippets.append(f"SOURCE: {title} ({url})\n{body}")
            if url:
                # Fetch the actual page, not just the search snippet — the
                # EAN/ASIN is often on the page but not in the short
                # snippet DuckDuckGo shows (this was the main gap before:
                # only snippets were used here, unlike spec_lookup.py).
                page_text = web_search.fetch_page_text(url)
                if page_text:
                    raw_snippets.append(f"PAGE CONTENT ({url}):\n{page_text}")
                sources.append(url)

    result = IdentifierResult()
    if not raw_snippets:
        if need_ean:
            result.ean_status = STATUS_NOT_FOUND
        if need_asin:
            result.asin_status = STATUS_NOT_FOUND
        return result

    combined = "\n\n---\n\n".join(raw_snippets)[:16000]

    system = (
        "You identify a consumer electronics product's EAN/GTIN barcode "
        "and/or Amazon ASIN from web search snippets. Prioritize the given "
        "MODEL NUMBER as the most precise identifying signal; brand and "
        "product name are secondary confirmation.\n\n"
        "CRITICAL RULES:\n"
        "- NEVER invent or guess a plausible-looking EAN or ASIN. Only "
        "report a value if it is EXPLICITLY present in the provided search "
        "text below.\n"
        "- An EAN/GTIN is an 8, 12, 13, or 14-digit numeric code. An ASIN is "
        "a 10-character Amazon code (letters and digits, often starting "
        "with 'B0').\n"
        "- If multiple different plausible ASINs appear for what might be "
        "this exact product (e.g. different color/storage/region variants), "
        "list them all as candidates instead of confidently picking one.\n\n"
        "Respond with STRICT JSON only, no markdown fences, matching exactly:\n"
        '{"ean": str (empty string if not explicitly found), '
        '"ean_confidence": "high"|"low"|"none", '
        '"asin": str (empty string if not explicitly found; your single best '
        'pick if one clearly stands out), '
        '"asin_candidates": [str, ...] (other plausible ASINs, only if '
        'genuinely ambiguous), '
        '"asin_confidence": "high"|"low"|"none"}'
    )
    user = (
        f"Brand: {brand or 'unknown'}\n"
        f"Model: {model or 'unknown'}\n"
        f"Product name: {product_name or 'unknown'}\n"
        f"Other info: {other_info or 'none'}\n\n"
        f"Web search results:\n{combined}"
    )

    try:
        data = ai_client.ask_json(system, user)
    except Exception:
        if need_ean:
            result.ean_status = STATUS_NOT_FOUND
        if need_asin:
            result.asin_status = STATUS_NOT_FOUND
        return result

    source_note = f"web search: {', '.join(sources[:3])}" if sources else "web search"

    if need_ean:
        ean_val = "".join(ch for ch in str(data.get("ean", "")) if ch.isdigit())
        ean_conf = data.get("ean_confidence", "none")
        if ean_val and len(ean_val) in _VALID_EAN_LENGTHS:
            result.ean = ean_val
            result.ean_source = source_note
            result.ean_status = STATUS_FOUND if ean_conf == "high" else STATUS_NEEDS_VERIFICATION
        else:
            result.ean_status = STATUS_NOT_FOUND

    if need_asin:
        asin_val = str(data.get("asin", "")).strip().upper()
        asin_conf = data.get("asin_confidence", "none")
        raw_candidates = [str(c).strip().upper() for c in (data.get("asin_candidates") or []) if str(c).strip()]

        def _valid_asin(v: str) -> bool:
            return len(v) == 10 and v.isalnum()

        all_candidates = []
        if _valid_asin(asin_val):
            all_candidates.append(asin_val)
        for c in raw_candidates:
            if _valid_asin(c) and c not in all_candidates:
                all_candidates.append(c)

        if not all_candidates:
            result.asin_status = STATUS_NOT_FOUND
        elif len(all_candidates) == 1 and asin_conf == "high":
            # Exactly one plausible ASIN, confidently identified — safe to
            # auto-fill as the definitive value.
            result.asin = all_candidates[0]
            result.asin_source = source_note
            result.asin_status = STATUS_FOUND
        else:
            # Multiple candidates, or a single low-confidence one — never
            # auto-pick a "best guess". Keep every candidate available for
            # search/future use, but leave the definitive ASIN for a human
            # to confirm.
            result.asin_candidates = all_candidates
            result.asin_status = STATUS_NEEDS_VERIFICATION
            result.asin_source = source_note

    return result


def needs_lookup(product: Product) -> bool:
    """True if ensure_identifiers would actually need to perform a web
    search for this product. Mirrors ensure_identifiers' own
    manifest/scan-promotion logic without mutating anything — used to size
    progress bars accurately instead of over-counting rows that already
    have a usable EAN via manifest_barcode/scanned_barcode."""
    has_ean = bool(product.ean or product.manifest_barcode or product.scanned_barcode)
    has_asin = bool(product.asin)
    return not has_ean or not has_asin


def ensure_identifiers(
    product: Product,
    brand: str = "",
    model: str = "",
    product_name: str = "",
    other_info: str = "",
) -> Product:
    """Fills product.ean/asin ONLY if currently blank. Never overwrites an
    existing value. Mutates and returns the same product for convenience."""
    # Step 1: promote an already-known physical/manifest value into the
    # unified `ean` field before ever considering a web search.
    if not product.ean:
        if product.manifest_barcode:
            product.ean = product.manifest_barcode
            product.ean_source = "manifest"
            product.ean_status = STATUS_FOUND
        elif product.scanned_barcode:
            product.ean = product.scanned_barcode
            product.ean_source = "scanned barcode"
            product.ean_status = STATUS_FOUND
        elif looks_like_ean(product.model_number):
            # A hand-typed barcode (e.g. when zbar/auto-decode wasn't
            # available) ends up in model_number, not scanned_barcode —
            # recognize it as an EAN anyway rather than losing it.
            product.ean = product.model_number
            product.ean_source = "manual entry"
            product.ean_status = STATUS_FOUND

    need_ean = not product.ean
    need_asin = not product.asin

    if not (need_ean or need_asin):
        return product

    result = find_identifiers(
        brand=brand or product.brand,
        model=model or product.model or product.model_number,
        product_name=product_name or product.name or product.manifest_item_description,
        other_info=other_info or product.category or product.manifest_subcategory,
        need_ean=need_ean,
        need_asin=need_asin,
    )

    if need_ean and not product.ean:
        product.ean = result.ean
        product.ean_status = result.ean_status or STATUS_NOT_FOUND
        product.ean_source = result.ean_source

    if need_asin and not product.asin:
        product.asin = result.asin
        product.asin_status = result.asin_status or STATUS_NOT_FOUND
        product.asin_source = result.asin_source
        product.asin_candidates = result.asin_candidates

    return product
