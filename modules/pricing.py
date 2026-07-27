"""Resale price estimation.

Best-effort approach with no paid market-data API required:
1. Search the web for used-market listings of this exact model.
2. Extract plausible price mentions from snippets via regex.
3. Take the median of a sane range, then apply a grade multiplier.

This is a heuristic, not a guarantee — always shown as editable in the UI.
"""
import re
import statistics
from dataclasses import dataclass, field
from typing import List, Optional

from modules import web_search

GRADE_MULTIPLIER = {
    "A": 1.00,
    "B": 0.85,
    "C": 0.70,
    "D": 0.50,
}

PRICE_RE = re.compile(r"[$€£]\s?(\d{1,4}(?:[.,]\d{2})?)")


@dataclass
class PriceEstimate:
    base_market_price: Optional[float] = None
    suggested_price: Optional[float] = None
    currency: str = "$"
    sample_count: int = 0
    reasoning: str = ""


def _search_prices(model_name: str, max_results: int = 8) -> List[float]:
    prices = []
    query = f"{model_name} used price sold"
    for hit in web_search.search(query, max_results=max_results):
        body = hit.get("body", "") + " " + hit.get("title", "")
        for m in PRICE_RE.findall(body):
            try:
                val = float(m.replace(",", ""))
                if 5 <= val <= 5000:
                    prices.append(val)
            except ValueError:
                continue
    return prices


def estimate_price(model_name: str, grade: str) -> PriceEstimate:
    prices = _search_prices(model_name)
    grade = (grade or "B").upper()
    multiplier = GRADE_MULTIPLIER.get(grade, 0.85)

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

    prices.sort()
    # Trim extreme outliers if we have enough samples.
    if len(prices) >= 5:
        trimmed = prices[1:-1]
    else:
        trimmed = prices
    base = statistics.median(trimmed)
    suggested = round(base * multiplier, 2)

    return PriceEstimate(
        base_market_price=round(base, 2),
        suggested_price=suggested,
        sample_count=len(prices),
        reasoning=(
            f"Based on {len(prices)} web price mentions for '{model_name}', "
            f"median reference price is ~${base:.2f}. Applied grade-{grade} "
            f"multiplier ({multiplier:.2f}) => suggested ${suggested:.2f}."
        ),
    )
