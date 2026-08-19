"""Resale price estimation.

Two signals, tried in order:
1. This company's own past sale prices for the same brand+model, in the
   same currency (modules/order_store.find_historical_sale_prices) — free,
   zero scraping noise, and already this company's real market. Used
   whenever there's enough history to trust.
2. Otherwise, a best-effort web search for used-market listings, with
   prices extracted from result snippets via regex.

Either way, the final number is a heuristic, not a guarantee — always
shown as editable in the UI.
"""
import re
import statistics
from dataclasses import dataclass
from typing import List, Optional

from modules import order_store, web_search

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


def _search_prices(model_name: str, max_results: int = 8) -> List[float]:
    prices = []
    query = f"{model_name} used price EUR"
    # retry_on_empty: this project's own measurements (see web_search.py)
    # found DDGS returns 0 results on roughly half of all calls purely from
    # which underlying engine subset it happened to try — worth the extra
    # latency here since this only runs once per "Estimate market price"
    # click, not in a tight per-row loop.
    for hit in web_search.search(query, max_results=max_results, retry_on_empty=True):
        body = hit.get("body", "") + " " + hit.get("title", "")
        for prefix, suffix in PRICE_RE.findall(body):
            val = _parse_price_str(prefix or suffix)
            if val is not None and 5 <= val <= 5000:
                prices.append(val)
    return prices


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

    prices = _search_prices(model_name)
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
