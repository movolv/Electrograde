"""Resale price estimation.

Two signals, tried in order:
1. This company's own past sale prices for the same brand+model, in the
   same currency (modules/order_store.find_historical_sale_prices) — free,
   zero scraping noise, and already this company's real market. Used
   whenever there's enough history to trust.
2. Otherwise, a best-effort web search for used-market listings, cached
   cross-tenant by brand+model+currency for PRICE_CACHE_TTL_SECONDS (see
   modules/price_cache_store.py) so the same model isn't re-searched from
   scratch every time — by this company or any other. Prices are extracted
   by asking Claude to read the actual search snippets/page text and
   report only prices that are (a) explicitly in EUR and (b) for this same
   product, not an accessory/case/different variant/unrelated item — see
   _extract_prices_ai(). A plain regex pass over the same text is kept as a
   fallback for when the AI call itself fails (no API key configured,
   rate-limited, network error): strictly worse at judging relevance, but
   still correctly EUR-only, so the feature degrades instead of going dark.

Either way, the final number is a heuristic, not a guarantee — always
shown as editable in the UI.
"""
import re
import statistics
from dataclasses import dataclass
from typing import List, Optional

from modules import ai_client, order_store, price_cache_store, web_search

PRODUCT_CONDITION_MULTIPLIER = {
    "A": 1.00,
    "B": 0.85,
    "C": 0.70,
    "D": 0.50,
}

# This app has no per-company currency setting (yet) and the UI is
# Latvia-first (see modules/i18n.py's "new_item.estimated_price" label) —
# EUR is the one target every listing price is meant to be in. Both the
# web-search extraction below and order_store.find_historical_sale_prices
# are hard-restricted to this currency specifically so a $ or £ listing
# never gets silently averaged in as if it were the same amount of money,
# which is what the unrestricted [$€£] regex used to do.
TARGET_CURRENCY_CODE = "EUR"
TARGET_CURRENCY_SYMBOL = "€"

# At least this many of the company's own past sales are required before
# trusting their median over a fresh web search — 1-2 data points is too
# thin to call a "market price" (could be a one-off clearance sale).
MIN_HISTORICAL_SAMPLES = 3

# Matches "€150", "€ 150.00", "150€", or "150 EUR" — deliberately NOT $/£,
# see TARGET_CURRENCY_CODE above. Two separate capture groups (prefix vs.
# suffix form) rather than one, so the group that actually matched can be
# picked out below.
PRICE_RE = re.compile(
    r"€\s?(\d{1,5}(?:[.,]\d{3})*(?:[.,]\d{2})?)"
    # \b only makes sense after "EUR" (word characters) — € itself is not a
    # word character, so `(?:€|EUR)\b` would never match the €-suffix case
    # at all (no word-to-non-word transition straight after a symbol).
    r"|(\d{1,5}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s?(?:€|EUR\b)",
    re.IGNORECASE,
)


def _parse_price_str(raw: str) -> Optional[float]:
    """Handles both thousand-separator conventions ("1,234.56" and
    "1.234,56") plus the plain European "150,00" form — the previous
    version just stripped every comma, which silently turned "150,00" into
    1500.0, a 10x pricing error with no error raised anywhere."""
    s = raw.strip()
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")  # "1.234,56" -> "1234.56"
        else:
            s = s.replace(",", "")  # "1,234.56" -> "1234.56"
    elif "," in s:
        if re.match(r"^\d+,\d{2}$", s):
            s = s.replace(",", ".")  # "150,00" -> "150.00"
        else:
            s = s.replace(",", "")  # "1,234" -> "1234"
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class PriceEstimate:
    base_market_price: Optional[float] = None
    suggested_price: Optional[float] = None
    currency: str = TARGET_CURRENCY_SYMBOL
    sample_count: int = 0
    reasoning: str = ""


def _regex_extract_prices(hits: List[dict]) -> List[float]:
    """The original extraction method — kept only as a fallback for when
    _extract_prices_ai() can't run at all (see module docstring). Cannot
    tell a price for THIS product apart from one for an accessory, a
    different variant, or an unrelated item mentioned on the same page;
    _extract_prices_ai() is what actually fixes that."""
    prices = []
    for hit in hits:
        body = hit.get("body", "") + " " + hit.get("title", "")
        for prefix, suffix in PRICE_RE.findall(body):
            val = _parse_price_str(prefix or suffix)
            if val is not None and 5 <= val <= 5000:
                prices.append(val)
    return prices


def _extract_prices_ai(model_name: str, raw_snippets: List[str]) -> List[float]:
    """Same "read the real text, extract only what's explicitly stated"
    pattern already proven in modules/identifier_lookup.find_identifiers()
    (ai_client.ask_json over search snippets + fetched page text). Fixes
    what a regex fundamentally cannot: whether a price on the page is
    actually for THIS product (not an accessory, a different storage/color
    variant, or an unrelated item the page happens to also mention)."""
    combined = "\n\n---\n\n".join(raw_snippets)[:16000]
    system = (
        "You extract resale/market prices for a SPECIFIC consumer electronics "
        "product from web search results.\n\n"
        "CRITICAL RULES:\n"
        "- Only include a price for THIS EXACT product/model — never an "
        "accessory, case, bundle, spare part, or a clearly different model/"
        "storage/color variant.\n"
        f"- Only include a price explicitly stated in EUR ({TARGET_CURRENCY_SYMBOL} "
        "or the word 'EUR'/'euro') in the text below. Ignore prices in any "
        "other currency entirely (e.g. $, £, USD, GBP, PLN) even if the "
        "number looks similar — never convert or guess at an exchange rate.\n"
        "- Ignore shipping costs, discounts/coupons, and 'was €X now €Y' "
        "strike-through prices — only the item's actual asking/sale price.\n"
        "- Never invent, round, or estimate a price that is not explicitly "
        "written in the text.\n\n"
        "Respond with STRICT JSON only, no markdown fences, matching exactly:\n"
        '{"prices": [number, ...]}  (empty list if none qualify)'
    )
    user = f"Product: {model_name}\n\nWeb search results:\n{combined}"
    try:
        # FAST_MODEL: this is a narrow, bounded extraction (read the text,
        # pull out a short list of numbers matching explicit rules) — not
        # open-ended writing or judgment-heavy grading, so a Haiku-tier
        # model is measurably faster here without giving up accuracy on
        # this specific shape of task. See ai_client.FAST_MODEL.
        data = ai_client.ask_json(system, user, model=ai_client.FAST_MODEL)
    except Exception:
        return []

    out = []
    for p in data.get("prices", []) or []:
        try:
            val = float(p)
        except (TypeError, ValueError):
            continue
        if 5 <= val <= 5000:
            out.append(val)
    return out


def _search_prices(model_name: str, max_results: int = 5) -> List[float]:
    # max_results=5 (was 8): the full page for each hit gets fetched below,
    # not just its search snippet — with 8, a single slow/unresponsive
    # source site can dominate this whole call's latency (each fetch has an
    # 8s timeout, run in parallel, so the tail is bounded by the SLOWEST of
    # however many URLs there are), and the larger combined text also costs
    # more time in the AI extraction call. 5 sources is still comfortably
    # enough for a median, and directly shortens both the fetch tail and
    # the AI call.
    query = f"{model_name} used price EUR"
    # retry_on_empty: this project's own measurements (see web_search.py)
    # found DDGS returns 0 results on roughly half of all calls purely from
    # which underlying engine subset it happened to try — worth the extra
    # latency here since this only runs once per "Estimate market price"
    # click, not in a tight per-row loop.
    hits = web_search.search(query, max_results=max_results, retry_on_empty=True)
    if not hits:
        return []

    # Fetch the actual pages, not just DuckDuckGo's short snippets — the
    # same reasoning as identifier_lookup.py's _gather_variant_snippets():
    # a search snippet is often too short to contain (or clearly attribute)
    # a price, while the full listing page usually does.
    urls = [h.get("href") or h.get("link", "") for h in hits if (h.get("href") or h.get("link", ""))]
    pages = web_search.fetch_pages_parallel(urls) if urls else {}
    raw_snippets = []
    for hit in hits:
        url = hit.get("href") or hit.get("link", "")
        raw_snippets.append(f"SOURCE: {hit.get('title', '')} ({url})\n{hit.get('body', '')}")
        page_text = pages.get(url, "") if url else ""
        if page_text:
            raw_snippets.append(f"PAGE CONTENT ({url}):\n{page_text}")

    ai_prices = _extract_prices_ai(model_name, raw_snippets)
    if ai_prices:
        return ai_prices
    # AI extraction found nothing usable, or the call itself failed (no API
    # key, rate limit, network error) — fall back rather than reporting "no
    # price data" when the search itself actually returned real results.
    return _regex_extract_prices(hits)


def _estimate_from_prices(prices: List[float], multiplier: float) -> tuple:
    prices = sorted(prices)
    # Trim extreme outliers if we have enough samples.
    trimmed = prices[1:-1] if len(prices) >= 5 else prices
    base = statistics.median(trimmed)
    return base, round(base * multiplier, 2)


def estimate_price(
    model_name: str, product_condition: str, company_id: str = "", brand: str = "", model: str = "",
) -> PriceEstimate:
    product_condition = (product_condition or "B").upper()
    multiplier = PRODUCT_CONDITION_MULTIPLIER.get(product_condition, 0.85)

    if company_id and (brand or model):
        hist_prices = order_store.find_historical_sale_prices(
            company_id, brand, model, currency=TARGET_CURRENCY_CODE,
        )
        if len(hist_prices) >= MIN_HISTORICAL_SAMPLES:
            base, suggested = _estimate_from_prices(hist_prices, multiplier)
            return PriceEstimate(
                base_market_price=round(base, 2),
                suggested_price=suggested,
                sample_count=len(hist_prices),
                reasoning=(
                    f"Based on {len(hist_prices)} of your own past sales matching '{model_name}', "
                    f"median sale price is ~{TARGET_CURRENCY_SYMBOL}{base:.2f}. Applied "
                    f"product-condition-{product_condition} multiplier ({multiplier:.2f}) => "
                    f"suggested {TARGET_CURRENCY_SYMBOL}{suggested:.2f}."
                ),
            )

    cached = price_cache_store.get(brand, model, TARGET_CURRENCY_CODE) if (brand and model) else None
    if cached is not None:
        prices = cached.prices
    else:
        prices = _search_prices(model_name)
        # Only cache a real find — see price_cache_store.upsert()'s
        # docstring on why an empty/not-found result is never cached (DDGS
        # itself is flaky enough that "nothing found" is often a fluke, not
        # a fact worth remembering for two weeks).
        if brand and model and prices:
            price_cache_store.upsert(brand, model, TARGET_CURRENCY_CODE, prices)

    if not prices:
        return PriceEstimate(
            base_market_price=None,
            suggested_price=None,
            sample_count=0,
            reasoning=(
                "No market price data could be found automatically. "
                "Please enter a price manually."
            ),
        )

    base, suggested = _estimate_from_prices(prices, multiplier)
    return PriceEstimate(
        base_market_price=round(base, 2),
        suggested_price=suggested,
        sample_count=len(prices),
        reasoning=(
            f"Based on {len(prices)} web price mentions for '{model_name}', "
            f"median reference price is ~{TARGET_CURRENCY_SYMBOL}{base:.2f}. Applied "
            f"product-condition-{product_condition} multiplier ({multiplier:.2f}) => "
            f"suggested {TARGET_CURRENCY_SYMBOL}{suggested:.2f}."
        ),
    )
