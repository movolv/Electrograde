"""SQLite-backed inventory persistence — the single source of truth. There
is no persistent Excel mirror anymore (removed: it was one shared file
across every company, a real cross-tenant exposure once more than one
company has data); Excel/CSV export is generated on demand from this
database instead — see modules/export.py.

Every row carries a `company_id` column (also duplicated inside the JSON
blob via Product.company_id) so a multi-company deployment can filter
per-tenant without a schema migration.

For fast search at scale, `ean`/`asin`/`model_number`/`model`/`brand`/`name`
are denormalized into their own indexed columns (kept in sync on every
save) instead of relying on scanning/deserializing the full JSON blob for
every row — see search_products().
"""
import json
import os
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

from modules.models import Product

# Overridable so scripts/verify_tenant_isolation.py (and any other script
# that needs a throwaway DB) can point every store module — they all import
# DB_PATH from here — at an isolated scratch file instead of the real one,
# just by setting this env var before any modules.*_store module is
# imported. No effect on the normal `streamlit run` path.
DB_PATH = Path(os.environ.get("ELECTROGRADER_DB_PATH") or (Path(__file__).resolve().parent.parent / "data" / "inventory.db"))

# Search result tiers, in priority order (see search_products()).
MATCH_TIER_SKU = "sku"
MATCH_TIER_EAN = "ean"
MATCH_TIER_ASIN = "asin"
MATCH_TIER_MODEL = "model"
MATCH_TIER_BRAND_NAME = "brand_name"

DEFAULT_SKU_RANGE_START = 2000


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT 'default',
            created_at REAL,
            data TEXT
        )
        """
    )

    # Migrate older DBs, adding denormalized search columns as needed.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    new_cols = {
        "company_id": "TEXT NOT NULL DEFAULT 'default'",
        "ean": "TEXT",
        "asin": "TEXT",
        "model_number": "TEXT",
        "model": "TEXT",
        "brand": "TEXT",
        "name": "TEXT",
        "sku": "TEXT",
        "manifest_import_id": "TEXT",
        "triage_status": "TEXT",
        "grade": "TEXT",
        "location": "TEXT",
    }
    added_any = False
    for col, decl in new_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE products ADD COLUMN {col} {decl}")
            added_any = True

    if added_any:
        # Backfill newly-added columns from the JSON blob for pre-existing rows.
        rows = conn.execute(
            "SELECT id, data FROM products WHERE ean IS NULL OR asin IS NULL "
            "OR model_number IS NULL OR model IS NULL OR brand IS NULL OR name IS NULL "
            "OR sku IS NULL OR manifest_import_id IS NULL OR triage_status IS NULL "
            "OR grade IS NULL OR location IS NULL"
        ).fetchall()
        for rid, data_json in rows:
            d = json.loads(data_json)
            # Product.from_dict() carries the triage_status backward-compat
            # default (old completed records -> "ready_for_sale") — reuse it
            # here so the denormalized column matches what the JSON blob
            # would resolve to, instead of duplicating that inference logic.
            triage_status = Product.from_dict(d).triage_status
            conn.execute(
                "UPDATE products SET ean=?, asin=?, model_number=?, model=?, brand=?, name=?, "
                "sku=?, manifest_import_id=?, triage_status=?, grade=?, location=? WHERE id=?",
                (
                    d.get("ean", ""), d.get("asin", ""), d.get("model_number", ""),
                    d.get("model", ""), d.get("brand", ""), d.get("name", ""),
                    d.get("sku", ""), d.get("manifest_import_id", ""), triage_status,
                    d.get("grade", ""), d.get("location", ""), rid,
                ),
            )
        conn.commit()

    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_company ON products(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_asin ON products(asin)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_model_number ON products(model_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_manifest_import_id ON products(manifest_import_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_triage_status ON products(triage_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_grade ON products(grade)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_location ON products(location)")
    return conn


def _search_columns(p: Product) -> tuple:
    return (
        p.ean, p.asin, p.model_number, p.model, p.brand, p.name, p.sku, p.manifest_import_id,
        p.triage_status, p.grade, p.location,
    )


def save_product(product: Product) -> None:
    """Saves to SQLite — the single source of truth. No Excel mirror to
    keep in sync anymore; export a spreadsheet on demand instead (see
    modules/export.py) if one is needed."""
    assert product.company_id, "Product.company_id must be set before saving — never save an orphaned record."
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO products
               (id, company_id, created_at, ean, asin, model_number, model, brand, name, sku,
                manifest_import_id, triage_status, grade, location, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product.id, product.company_id, product.created_at, *_search_columns(product),
             json.dumps(product.to_dict())),
        )
    conn.close()


def save_products_bulk(products: List[Product]) -> None:
    """Saves many products in one transaction (e.g. a manifest import)."""
    if not products:
        return
    assert all(p.company_id for p in products), "Every Product.company_id must be set before saving."
    conn = _connect()
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO products
               (id, company_id, created_at, ean, asin, model_number, model, brand, name, sku,
                manifest_import_id, triage_status, grade, location, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (p.id, p.company_id, p.created_at, *_search_columns(p), json.dumps(p.to_dict()))
                for p in products
            ],
        )
    conn.close()


def delete_product(product_id: str, company_id: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM products WHERE id = ? AND company_id = ?", (product_id, company_id))
    conn.close()


def list_products_by_manifest(manifest_import_id: str, company_id: str) -> List[Product]:
    """All products (any status) linked to a given manifest batch."""
    conn = _connect()
    rows = conn.execute(
        "SELECT data FROM products WHERE manifest_import_id = ? AND company_id = ? "
        "ORDER BY created_at ASC",
        (manifest_import_id, company_id),
    ).fetchall()
    conn.close()
    return [Product.from_dict(json.loads(r[0])) for r in rows]


def delete_products_by_manifest(
    manifest_import_id: str,
    company_id: str,
    only_drafts: bool = True,
) -> int:
    """Deletes products linked to a manifest batch. By default only
    still-unprocessed ("draft") products are removed — already-completed
    items (photographed, AI-graded, priced) represent real work and are
    left untouched even when their source manifest is deleted, unless the
    caller explicitly opts in with only_drafts=False. Returns the number of
    products actually deleted."""
    products = list_products_by_manifest(manifest_import_id, company_id)
    to_delete = [p for p in products if not only_drafts or p.status == "draft"]
    if not to_delete:
        return 0

    # Bulk delete in one transaction rather than looping delete_product().
    conn = _connect()
    with conn:
        conn.executemany(
            "DELETE FROM products WHERE id = ?", [(p.id,) for p in to_delete]
        )
    conn.close()
    return len(to_delete)


def list_products(company_id: str) -> List[Product]:
    conn = _connect()
    rows = conn.execute(
        "SELECT data FROM products WHERE company_id = ? ORDER BY created_at DESC",
        (company_id,),
    ).fetchall()
    conn.close()
    return [Product.from_dict(json.loads(r[0])) for r in rows]


def get_product(product_id: str, company_id: str) -> Optional[Product]:
    conn = _connect()
    row = conn.execute(
        "SELECT data FROM products WHERE id = ? AND company_id = ?", (product_id, company_id)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return Product.from_dict(json.loads(row[0]))


def search_products(query: str, company_id: str) -> List[Tuple[Product, str]]:
    """Fast, indexed, priority-ordered search across the whole inventory.

    Priority (matches earlier tiers are returned first; a product appears
    only once, tagged with the highest tier it matched):
      1. Exact SKU match (case-insensitive)
      2. Exact EAN match
      3. Exact ASIN match (case-insensitive)
      4. Model number / model match (substring, case-insensitive)
      5. Brand or product name match (substring, case-insensitive)

    Works identically for manifest-imported and manually-entered products,
    since both populate the same denormalized sku/ean/asin/model_number/
    model/brand/name columns. Runs as indexed SQL WHERE clauses rather than
    loading and filtering every row in Python, so it stays fast as the
    inventory grows.
    """
    q = query.strip()
    if not q:
        return []

    conn = _connect()

    def _run(where_sql: str, where_params: list) -> List[Product]:
        sql = f"SELECT data FROM products WHERE ({where_sql}) AND company_id = ? ORDER BY created_at DESC"
        rows = conn.execute(sql, where_params + [company_id]).fetchall()
        return [Product.from_dict(json.loads(r[0])) for r in rows]

    like = f"%{q}%"
    seen_ids = set()
    results: List[Tuple[Product, str]] = []

    def _add(products: List[Product], tier: str):
        for p in products:
            if p.id not in seen_ids:
                seen_ids.add(p.id)
                results.append((p, tier))

    _add(_run("UPPER(sku) = UPPER(?)", [q]), MATCH_TIER_SKU)
    _add(_run("ean = ?", [q]), MATCH_TIER_EAN)
    _add(_run("UPPER(asin) = UPPER(?)", [q]), MATCH_TIER_ASIN)
    _add(
        _run("model_number LIKE ? COLLATE NOCASE OR model LIKE ? COLLATE NOCASE", [like, like]),
        MATCH_TIER_MODEL,
    )
    _add(
        _run("brand LIKE ? COLLATE NOCASE OR name LIKE ? COLLATE NOCASE", [like, like]),
        MATCH_TIER_BRAND_NAME,
    )

    conn.close()
    return results


def next_sku_batch(
    count: int,
    company_id: str,
    start_if_empty: int = DEFAULT_SKU_RANGE_START,
) -> List[str]:
    """Returns `count` new sequential numeric SKUs as strings, continuing
    from one past the highest purely-numeric SKU currently in use for this
    company — or starting at `start_if_empty` if none exist yet. Non-numeric
    SKUs (manual entries like "Test1") are ignored when computing the next
    number, so auto-assigned and hand-typed SKUs can coexist without
    colliding."""
    conn = _connect()
    rows = conn.execute("SELECT sku FROM products WHERE company_id = ?", (company_id,)).fetchall()
    conn.close()

    numeric_skus = [int(r[0]) for r in rows if r[0] and r[0].isdigit()]
    next_start = (max(numeric_skus) + 1) if numeric_skus else start_if_empty
    return [str(next_start + i) for i in range(count)]


def list_products_paginated(
    company_id: str,
    triage_status: Optional[str] = None,
    manifest_import_id: Optional[str] = None,
    location: Optional[str] = None,
    grade: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> Tuple[List[Product], int]:
    """The default Inventory-tab query: filtered on indexed columns, and
    always bounded by LIMIT/OFFSET so at most one page of rows is ever
    loaded into Python/the browser — the thing the plain list_products()
    loop-per-row rendering can't do at 1,000-20,000 rows.

    `search` is a lightweight substring match (sku/name/brand/model/ean/
    asin) for narrowing within the current filters — for a single specific
    lookup by exact SKU/EAN/ASIN across the *whole* inventory regardless of
    filters, use search_products() instead.

    Filters combine with AND; any left as None is skipped entirely (not
    turned into an "IS NULL" or "= ''" clause). Returns (rows_for_this_page,
    total_matching_row_count) so the caller can render "page X of Y".
    """
    where_clauses = ["company_id = ?"]
    params: list = [company_id]

    if triage_status:
        where_clauses.append("triage_status = ?")
        params.append(triage_status)
    if manifest_import_id:
        where_clauses.append("manifest_import_id = ?")
        params.append(manifest_import_id)
    if location:
        where_clauses.append("location = ?")
        params.append(location)
    if grade:
        where_clauses.append("grade = ?")
        params.append(grade)
    if search and search.strip():
        like = f"%{search.strip()}%"
        where_clauses.append(
            "(sku LIKE ? COLLATE NOCASE OR name LIKE ? COLLATE NOCASE OR brand LIKE ? COLLATE NOCASE "
            "OR model LIKE ? COLLATE NOCASE OR model_number LIKE ? COLLATE NOCASE "
            "OR ean = ? OR UPPER(asin) = UPPER(?))"
        )
        params.extend([like, like, like, like, like, search.strip(), search.strip()])

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    conn = _connect()
    total = conn.execute(f"SELECT COUNT(*) FROM products {where_sql}", params).fetchone()[0]

    page = max(1, page)
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT data FROM products {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    conn.close()

    products = [Product.from_dict(json.loads(r[0])) for r in rows]
    return products, total


def distinct_locations(company_id: str) -> List[str]:
    """Non-empty distinct location values currently in use, for populating
    a filter dropdown — cheap thanks to the indexed `location` column."""
    conn = _connect()
    rows = conn.execute(
        "SELECT DISTINCT location FROM products WHERE company_id = ? AND location IS NOT NULL AND location != '' "
        "ORDER BY location ASC",
        (company_id,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]
