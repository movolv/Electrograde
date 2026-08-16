"""Persistent, cross-tenant product-lookup cache — part of the New Item
Step 3 "Fast-First" lookup architecture (see identifier_lookup.fast_find_ean,
spec_lookup.quick_guess, and the wizard flow in app.py).

Deliberately NOT company-scoped, unlike every other modules/*_store.py table.
A product's EAN/brand/model/specifications are universal facts (the same
physical product looks identical to every company that lists it), not
private tenant data — so scripts/verify_tenant_isolation.py intentionally
does not apply to this table. At the 100-company scale target, sharing this
cache means the FIRST company to look up a given model saves every other
company that later handles the same model from repeating the same web
search + Claude call at all, which is a meaningful reduction in total
outbound search/AI load across the whole system, not just a per-request
speedup.

Only ever populated from data already found via public web search — no
company_id, SKU, warehouse, pricing, notes, auth, or session data is ever
written here (see NEPĀRKĀPJAMI rule 7 in the approved plan).

cache_key is the PRIMARY KEY, so every lookup is an exact-key read — the
table can grow to millions of rows without a slower lookup path.
"""
import json
import time
from dataclasses import dataclass, field
from typing import List, Optional

from modules import db


@dataclass
class LookupCacheEntry:
    cache_key: str = ""
    ean: str = ""
    asin: str = ""
    brand: str = ""
    model: str = ""
    product_name: str = ""
    category: str = ""
    power: str = ""
    spec_summary: str = ""
    box_contents: List[str] = field(default_factory=list)
    ean_confidence: str = ""
    asin_confidence: str = ""
    sources: List[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split()) if value else ""


def _keys_for(ean: str = "", asin: str = "", brand: str = "", model: str = "", description: str = "") -> List[str]:
    """Priority order: an exact identifier (ean/asin) is trusted over a
    brand+model pair, which is trusted over a free-text description match —
    each later key is a progressively weaker signal that the cached entry
    is really the same product."""
    keys = []
    if ean:
        keys.append(f"ean:{_norm(ean)}")
    if asin:
        keys.append(f"asin:{_norm(asin)}")
    if brand and model:
        keys.append(f"bm:{_norm(brand)}|{_norm(model)}")
    if description:
        keys.append(f"desc:{_norm(description)}")
    return keys


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lookup_cache (
            cache_key TEXT PRIMARY KEY,
            ean TEXT,
            asin TEXT,
            brand TEXT,
            model TEXT,
            product_name TEXT,
            category TEXT,
            power TEXT,
            spec_summary TEXT,
            box_contents TEXT,
            ean_confidence TEXT,
            asin_confidence TEXT,
            sources TEXT,
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION
        )
        """
    )
    # Migrate older DBs (this table predates the `power` field).
    if "power" not in db.table_columns(conn, "lookup_cache"):
        conn.execute("ALTER TABLE lookup_cache ADD COLUMN power TEXT")
    conn.commit()
    return conn


def get_best_match(ean: str = "", asin: str = "", brand: str = "", model: str = "", description: str = "") -> Optional[LookupCacheEntry]:
    """Checks candidate cache keys in priority order (ean: > asin: >
    bm:{brand}|{model} > desc:{description}), returning the first hit."""
    keys = _keys_for(ean, asin, brand, model, description)
    if not keys:
        return None
    conn = _connect()
    row = None
    for key in keys:
        row = conn.execute(
            "SELECT cache_key, ean, asin, brand, model, product_name, category, "
            "power, spec_summary, box_contents, ean_confidence, asin_confidence, sources, "
            "created_at, updated_at FROM lookup_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row:
            break
    conn.close()
    if not row:
        return None
    return LookupCacheEntry(
        cache_key=row[0], ean=row[1] or "", asin=row[2] or "", brand=row[3] or "",
        model=row[4] or "", product_name=row[5] or "", category=row[6] or "",
        power=row[7] or "", spec_summary=row[8] or "",
        box_contents=json.loads(row[9]) if row[9] else [],
        ean_confidence=row[10] or "", asin_confidence=row[11] or "",
        sources=json.loads(row[12]) if row[12] else [],
        created_at=row[13] or 0.0, updated_at=row[14] or 0.0,
    )


def upsert(entry: LookupCacheEntry) -> None:
    """Writes the entry under EVERY derived key it qualifies for (ean/asin/
    brand+model) — maximizes the chance a future lookup with only a subset
    of these signals still gets a cache hit."""
    keys = []
    if entry.ean:
        keys.append(f"ean:{_norm(entry.ean)}")
    if entry.asin:
        keys.append(f"asin:{_norm(entry.asin)}")
    if entry.brand and entry.model:
        keys.append(f"bm:{_norm(entry.brand)}|{_norm(entry.model)}")
    if not keys:
        return

    now = time.time()
    conn = _connect()
    with conn:
        for key in keys:
            conn.execute(
                """INSERT INTO lookup_cache
                   (cache_key, ean, asin, brand, model, product_name, category, power,
                    spec_summary, box_contents, ean_confidence, asin_confidence,
                    sources, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (cache_key) DO UPDATE SET
                       ean = EXCLUDED.ean, asin = EXCLUDED.asin, brand = EXCLUDED.brand,
                       model = EXCLUDED.model, product_name = EXCLUDED.product_name,
                       category = EXCLUDED.category, power = EXCLUDED.power,
                       spec_summary = EXCLUDED.spec_summary,
                       box_contents = EXCLUDED.box_contents,
                       ean_confidence = EXCLUDED.ean_confidence,
                       asin_confidence = EXCLUDED.asin_confidence,
                       sources = EXCLUDED.sources, updated_at = EXCLUDED.updated_at""",
                (
                    key, entry.ean, entry.asin, entry.brand, entry.model,
                    entry.product_name, entry.category, entry.power, entry.spec_summary,
                    json.dumps(entry.box_contents), entry.ean_confidence,
                    entry.asin_confidence, json.dumps(entry.sources),
                    entry.created_at or now, now,
                ),
            )
    conn.close()
