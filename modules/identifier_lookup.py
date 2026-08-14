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
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Tuple

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


def _gather_variant_snippets(variants: List[str]) -> Tuple[List[str], List[str]]:
    """One need's (EAN's or ASIN's) worth of search-with-fallback + page-
    fetch, gathered the exact same way find_identifiers() always has (same
    SOURCE/PAGE CONTENT formatting, same "source counted whenever a URL was
    present, whether or not its page fetch succeeded" rule) — pulled out so
    find_identifiers() can run the EAN and ASIN groups CONCURRENTLY instead
    of one fully finishing before the other starts."""
    raw: List[str] = []
    sources: List[str] = []
    hits: List[dict] = []
    for q in variants:
        hits = web_search.search(q, retry_on_empty=True)
        if hits:
            break
    urls = [h.get("href") or h.get("link", "") for h in hits if (h.get("href") or h.get("link", ""))]
    pages = web_search.fetch_pages_parallel(urls) if urls else {}
    for hit in hits:
        url = hit.get("href") or hit.get("link", "")
        body = hit.get("body", "")
        title = hit.get("title", "")
        raw.append(f"SOURCE: {title} ({url})\n{body}")
        if url:
            page_text = pages.get(url, "")
            if page_text:
                raw.append(f"PAGE CONTENT ({url}):\n{page_text}")
            sources.append(url)
    return raw, sources


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

    # retry_on_empty=True (inside _gather_variant_snippets): this whole
    # function only ever runs in a non-blocking context (Step 3's
    # background deep enrichment, or the manifest bulk-import job) — never
    # synchronously in front of a waiting user — so it's worth absorbing
    # ddgs's ~50% empty-result rate (confirmed by direct measurement: the
    # same query can return 0 hits on one call and 5 on an identical retry,
    # because its "auto" backend randomly varies which underlying engines
    # it tries) with a couple of extra attempts, for a real improvement in
    # EAN/ASIN find-rate. The Fast-First Level 1 layer
    # (fast_ensure_identifiers, below) deliberately does NOT do this — that
    # one IS synchronous/user-facing, where a fast, bounded response
    # matters more than squeezing out a few extra percentage points of
    # find-rate.
    #
    # The EAN need and the ASIN need are independent searches (different
    # query terms, no data dependency) — run them concurrently rather than
    # one fully finishing before the other starts, same reasoning as
    # spec_lookup.lookup()'s two queries. Fetches the actual pages, not
    # just search snippets — the EAN/ASIN is often on the page but not in
    # the short snippet DuckDuckGo shows.
    raw_snippets = []
    sources = []
    with ThreadPoolExecutor(max_workers=len(query_variants)) as executor:
        for raw, srcs in executor.map(_gather_variant_snippets, query_variants):
            raw_snippets.extend(raw)
            sources.extend(srcs)

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
        # validate_gtin_checksum: right shape (8/12/13/14 digits) is not
        # enough — a page's listed "EAN" can itself be a typo/OCR error/
        # truncation, and until this check was added here, that malformed
        # value was accepted as-is (Claude's prompt says "never invent",
        # but that's a soft instruction, not a guarantee against garbage
        # data already present in the source text). Confirmed live: a real
        # 30-product run returned one checksum-invalid "EAN" that this
        # check would have caught. Same GS1 check-digit algorithm already
        # used by the Level 1 fast layer below — a checksum-invalid value
        # is treated exactly like "nothing found", never surfaced as if it
        # were a real barcode.
        if ean_val and len(ean_val) in _VALID_EAN_LENGTHS and validate_gtin_checksum(ean_val):
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


# ---------------------------------------------------------------------------
# Fast-First Level 1 layer (New Item Step 3 redesign) — additive only.
# Everything above this line (find_identifiers, ensure_identifiers) is the
# unchanged "deep" path: still the only way an ASIN or a single-source EAN
# ever gets confirmed. Everything below is a deterministic, non-AI, no-page-
# fetch layer that scans search-result snippets already in hand for an EAN
# that's confident enough to skip the deep path entirely.
# ---------------------------------------------------------------------------

_GTIN_CANDIDATE_RE = re.compile(r"\b\d{8}\b|\b\d{12,14}\b")
_EAN_CONTEXT_RE = re.compile(r"\b(ean|gtin|upc|barcode)\b", re.IGNORECASE)
_CONTEXT_WINDOW = 40  # chars either side of a candidate, for context matching

# An ASIN has no checksum to validate against (unlike a GTIN), so a regex
# hit here is NEVER auto-filled into product.asin — see
# _extract_asin_preview_candidate below, which stays strictly preview-only.
_ASIN_CANDIDATE_RE = re.compile(r"\b(?:B0[A-Z0-9]{8}|[A-Z0-9]{10})\b")
_ASIN_CONTEXT_RE = re.compile(r"\b(asin|amazon)\b", re.IGNORECASE)


def validate_gtin_checksum(digits: str) -> bool:
    """GS1 check-digit algorithm — identical for every valid GTIN length
    (8/12/13/14): starting from the digit immediately left of the check
    digit and moving left, alternate weights 3,1,3,1... Deterministic, no
    network/AI call. Necessary but NOT sufficient on its own for high
    confidence — see fast_find_ean, which additionally requires cross-source
    agreement (NEPĀRKĀPJAMS rule 3)."""
    if not (digits.isdigit() and len(digits) in _VALID_EAN_LENGTHS):
        return False
    body, check = digits[:-1], int(digits[-1])
    total, weight = 0, 3
    for ch in reversed(body):
        total += int(ch) * weight
        weight = 1 if weight == 3 else 3
    return (10 - total % 10) % 10 == check


def extract_ean_candidates(text: str) -> List[str]:
    """Pulls checksum-valid GTIN-shaped numbers out of free text (a search
    snippet OR page text) — no network, no AI. Candidates found near an
    "EAN"/"GTIN"/"UPC"/"barcode" word are returned first (far less likely to
    be a price/SKU/phone-number false positive), followed by any other
    checksum-valid candidate."""
    if not text:
        return []
    near_context: List[str] = []
    elsewhere: List[str] = []
    seen = set()
    for m in _GTIN_CANDIDATE_RE.finditer(text):
        digits = m.group(0)
        if digits in seen or not validate_gtin_checksum(digits):
            continue
        seen.add(digits)
        window = text[max(0, m.start() - _CONTEXT_WINDOW):m.end() + _CONTEXT_WINDOW]
        (near_context if _EAN_CONTEXT_RE.search(window) else elsewhere).append(digits)
    return near_context + elsewhere


def fast_find_ean(hits: list) -> Tuple[str, str, int]:
    """LEVEL 1: scans search-result hits already in hand (title+body from
    web_search.search) — no page fetch, no Claude. Returns
    (ean, confidence, n_sources):
      - "high": the same checksum-valid candidate appears in >=2 distinct
        result sources — auto-fillable (NEPĀRKĀPJAMS rule 3).
      - "medium": exactly one candidate, seen in only one source — NOT
        auto-fillable, only a pending-candidate preview.
      - "conflict": more than one distinct checksum-valid candidate found,
        with no single one backed by >=2 sources — NOT auto-fillable.
      - "": nothing found.
    """
    candidate_sources: dict = {}
    for i, hit in enumerate(hits or []):
        text = f"{hit.get('title', '')} {hit.get('body', '')}"
        candidates = extract_ean_candidates(text)
        if not candidates:
            continue
        # Only the best (context-prioritized) candidate per hit counts as
        # that hit's "vote" — otherwise one snippet containing several
        # unrelated checksum-valid numbers could fake multi-source agreement
        # for more than one of them.
        best = candidates[0]
        source_key = hit.get("href") or hit.get("link") or str(i)
        candidate_sources.setdefault(best, set()).add(source_key)

    if not candidate_sources:
        return "", "", 0

    agreed = [d for d, srcs in candidate_sources.items() if len(srcs) >= 2]
    if len(agreed) == 1:
        ean = agreed[0]
        return ean, "high", len(candidate_sources[ean])
    if len(agreed) > 1:
        return "", "conflict", 0
    if len(candidate_sources) == 1:
        only = next(iter(candidate_sources))
        return only, "medium", len(candidate_sources[only])
    return "", "conflict", 0


def _extract_asin_preview_candidate(hits: list):
    """Best-effort, UNVALIDATED ASIN guess for UI preview only. An ASIN has
    no checksum (unlike a GTIN), so there is no deterministic way to gain
    confidence in one the way fast_find_ean can for an EAN — this is never
    written to product.asin; that stays reserved for find_identifiers'
    AI-confirmed, single-candidate result, exactly as today."""
    for hit in hits or []:
        text = f"{hit.get('title', '')} {hit.get('body', '')}"
        for m in _ASIN_CANDIDATE_RE.finditer(text):
            val = m.group(0)
            window = text[max(0, m.start() - _CONTEXT_WINDOW):m.end() + _CONTEXT_WINDOW]
            if _ASIN_CONTEXT_RE.search(window):
                return val
    return None


def fast_ensure_identifiers(
    product: Product,
    brand: str = "",
    model: str = "",
    product_name: str = "",
    other_info: str = "",
) -> dict:
    """LEVEL 1 orchestrator: fast, deterministic, non-AI EAN/ASIN pass.
    Mutates product.ean ONLY if it's currently blank AND fast_find_ean
    returns "high" confidence (>=2 independent sources agree) — the same
    "never overwrite existing data" priority as ensure_identifiers, plus the
    stricter high-confidence bar from NEPĀRKĀPJAMS rule 3. Never mutates
    product.asin (no deterministic ASIN confidence signal exists — that
    stays exclusively the deep/AI path's job, unchanged).

    Returns a dict describing what still needs the deep path, plus the
    already-fetched search hits so Level 2 doesn't repeat the same
    web_search.search() calls:
        {"need_deep_ean", "need_deep_asin",
         "ean_pending_candidate", "asin_pending_candidate",
         "ean_hits", "asin_hits"}
    """
    result = {
        "need_deep_ean": not product.ean,
        "need_deep_asin": not product.asin,
        "ean_pending_candidate": None,
        "asin_pending_candidate": None,
        "ean_hits": [],
        "asin_hits": [],
    }

    # Same already-known-physical-value promotion as ensure_identifiers, so
    # the fast layer never searches the web for something already on hand.
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
            product.ean = product.model_number
            product.ean_source = "manual entry"
            product.ean_status = STATUS_FOUND
    result["need_deep_ean"] = not product.ean

    if not (result["need_deep_ean"] or result["need_deep_asin"]):
        return result

    query_terms = " ".join(t for t in [brand, model, product_name, other_info] if t and t.strip())
    if not query_terms:
        return result

    # EAN and ASIN searches are independent — run them concurrently rather
    # than one after the other, so this whole Level 1 pass costs roughly one
    # search's latency, not two (part of what makes the ~1-3s TTFUR target
    # achievable; see app.py's _run_fast_layer, which also runs the specs
    # search concurrently with this whole function).
    with ThreadPoolExecutor(max_workers=2) as executor:
        ean_future = executor.submit(web_search.search, f"{query_terms} EAN GTIN barcode") if result["need_deep_ean"] else None
        asin_future = executor.submit(web_search.search, f"{query_terms} amazon ASIN") if result["need_deep_asin"] else None

        if ean_future is not None:
            ean_hits = ean_future.result()
            result["ean_hits"] = ean_hits
            ean, confidence, n_sources = fast_find_ean(ean_hits)
            if confidence == "high":
                product.ean = ean
                product.ean_status = STATUS_FOUND
                product.ean_source = f"fast web search ({n_sources} sources agree)"
                result["need_deep_ean"] = False
            elif confidence == "medium":
                result["ean_pending_candidate"] = ean

        if asin_future is not None:
            asin_hits = asin_future.result()
            result["asin_hits"] = asin_hits
            result["asin_pending_candidate"] = _extract_asin_preview_candidate(asin_hits)

    return result
