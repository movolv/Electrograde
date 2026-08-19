"""Persistent, cross-tenant cache of web-search-derived market price
estimates — same architectural reasoning as modules/lookup_cache_store.py:
a used "iPhone 13 128GB"'s market price in EUR is the same fact regardless
of which company is asking, so sharing this cache means the FIRST company
to price a given model saves every other company that later handles the
same model from repeating the same web search + page fetches + Claude call,
not just a per-request speedup. At the 100 companies x 100k products scale
target this meaningfully cuts total outbound search/AI load, and reduces
how often modules/web_search.py's shared DDGS rate limit gets hit.

Deliberately NOT the same table as lookup_cache_store, despite the near-
identical shape: that table's rows (EAN, spec, box contents) are treated as
permanent facts with no expiry, while a market price genuinely goes stale
over time — see PRICE_CACHE_TTL_SECONDS below. Mixing the two would mean
either specs incorrectly expiring or prices incorrectly never refreshing.

Deliberately NOT company-scoped either — see modules/lookup_cache_store.py's
docstring for the same reasoning; scripts/verify_tenant_isolation.py
intentionally does not apply here. Only ever populated from public web-
search results — no company_id, SKU, or other tenant data is ever written.
"""
import json
import time
from dataclasses import dataclass, field
from typing import List, Optional

from modules import db

# Used-market prices drift with supply/demand (and, for electronics
# especially, newer-model releases pulling older-model resale prices down
# over time) — a month-old cached price is still a reasonable starting
# point, but much older than that risks being meaningfully wrong. Short
# enough to stay honest, long enough that the common case (a company
# pricing several units of the same model, or re-opening the wizard on a
# similar item days later) hits the cache instead of re-searching.
PRICE_CACHE_TTL_SECONDS = 14 * 24 * 3600  # 14 days


@dataclass
class PriceCacheEntry:
    cache_key: str = ""
    brand: str = ""
    model: str = ""
    prices: List[float] = field(default_factory=list)
    currency: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split()) if value else ""


def _key(brand: str, model: str, currency: str) -> str:
    return f"bm:{_norm(brand)}|{_norm(model)}|{_norm(currency)}"


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_cache (
            cache_key TEXT PRIMARY KEY,
            brand TEXT,
            model TEXT,
            prices TEXT,
            currency TEXT,
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION
        )
        """
    )
    conn.commit()
    return conn


def get(brand: str, model: str, currency: str) -> Optional[PriceCacheEntry]:
    """Returns the cached entry only if it's still within
    PRICE_CACHE_TTL_SECONDS — an expired row is treated exactly like a
    miss (the caller re-searches and overwrites it via upsert()); this
    never hands back a stale price silently."""
    if not brand or not model:
        return None
    conn = _connect()
    row = conn.execute(
        "SELECT cache_key, brand, model, prices, currency, created_at, updated_at "
        "FROM price_cache WHERE cache_key = ?",
        (_key(brand, model, currency),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    if time.time() - (row[6] or 0.0) > PRICE_CACHE_TTL_SECONDS:
        return None
    return PriceCacheEntry(
        cache_key=row[0], brand=row[1] or "", model=row[2] or "",
        prices=json.loads(row[3]) if row[3] else [],
        currency=row[4] or "", created_at=row[5] or 0.0, updated_at=row[6] or 0.0,
    )


def upsert(brand: str, model: str, currency: str, prices: List[float]) -> None:
    """Only ever called with a non-empty `prices` — see
    modules/pricing.py's estimate_price(), which deliberately never caches
    an empty/not-found result: modules/web_search.py's own DDGS backend is
    documented as returning zero results on roughly half of all calls
    purely by chance (which underlying engine subset it tried), so caching
    "nothing found" for PRICE_CACHE_TTL_SECONDS would just as often be
    caching a fluke as caching a real fact."""
    if not brand or not model or not prices:
        return
    now = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO price_cache (cache_key, brand, model, prices, currency, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (cache_key) DO UPDATE SET
                   prices = EXCLUDED.prices, updated_at = EXCLUDED.updated_at""",
            (_key(brand, model, currency), brand, model, json.dumps(prices), currency, now, now),
        )
    conn.close()
