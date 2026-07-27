"""SQLite-backed inventory persistence.

Every row carries a `company_id` column (also duplicated inside the JSON
blob via Product.company_id) so a future multi-company deployment can filter
per-tenant without a schema migration — today everything just uses the
"default" company.

For fast search at scale, `ean`/`asin`/`model_number`/`model`/`brand`/`name`
are denormalized into their own indexed columns (kept in sync on every
save) instead of relying on scanning/deserializing the full JSON blob for
every row — see search_products().
"""
import json
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

from modules import excel_autosave
from modules.models import Product

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "inventory.db"

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
            "OR sku IS NULL OR manifest_import_id IS NULL"
        ).fetchall()
        for rid, data_json in rows:
            d = json.loads(data_json)
            conn.execute(
                "UPDATE products SET ean=?, asin=?, model_number=?, model=?, brand=?, name=?, "
                "sku=?, manifest_import_id=? WHERE id=?",
                (
                    d.get("ean", ""), d.get("asin", ""), d.get("model_number", ""),
                    d.get("model", ""), d.get("brand", ""), d.get("name", ""),
                    d.get("sku", ""), d.get("manifest_import_id", ""), rid,
                ),
            )
        conn.commit()

    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_company ON products(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_asin ON products(asin)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_model_number ON products(model_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_manifest_import_id ON products(manifest_import_id)")
    return conn


def _search_columns(p: Product) -> tuple:
    return (p.ean, p.asin, p.model_number, p.model, p.brand, p.name, p.sku, p.manifest_import_id)


def save_product(product: Product) -> Optional[str]:
    """Saves to SQLite (source of truth) and mirrors to Excel.

    Returns None on full success, or a warning string if the Excel mirror
    could not be written (e.g. the file is open elsewhere) — the SQLite save
    itself always completes regardless.
    """
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO products
               (id, company_id, created_at, ean, asin, model_number, model, brand, name, sku, manifest_import_id, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product.id, product.company_id, product.created_at, *_search_columns(product),
             json.dumps(product.to_dict())),
        )
    conn.close()
    return excel_autosave.sync_excel(list_products())


def save_products_bulk(products: List[Product]) -> Optional[str]:
    """Saves many products in one transaction (e.g. a manifest import) and
    syncs the Excel mirror once at the end instead of once per row."""
    if not products:
        return None
    conn = _connect()
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO products
               (id, company_id, created_at, ean, asin, model_number, model, brand, name, sku, manifest_import_id, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (p.id, p.company_id, p.created_at, *_search_columns(p), json.dumps(p.to_dict()))
                for p in products
            ],
        )
    conn.close()
    return excel_autosave.sync_excel(list_products())


def delete_product(product_id: str) -> Optional[str]:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.close()
    return excel_autosave.sync_excel(list_products())


def list_products_by_manifest(manifest_import_id: str, company_id: Optional[str] = None) -> List[Product]:
    """All products (any status) linked to a given manifest batch."""
    conn = _connect()
    if company_id:
        rows = conn.execute(
            "SELECT data FROM products WHERE manifest_import_id = ? AND company_id = ? "
            "ORDER BY created_at ASC",
            (manifest_import_id, company_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT data FROM products WHERE manifest_import_id = ? ORDER BY created_at ASC",
            (manifest_import_id,),
        ).fetchall()
    conn.close()
    return [Product.from_dict(json.loads(r[0])) for r in rows]


def delete_products_by_manifest(
    manifest_import_id: str,
    company_id: Optional[str] = None,
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

    # Bulk delete + a single Excel resync at the end, rather than looping
    # delete_product() (which would resync the whole file once per row —
    # fine for one item, far too slow for a manifest of hundreds).
    conn = _connect()
    with conn:
        conn.executemany(
            "DELETE FROM products WHERE id = ?", [(p.id,) for p in to_delete]
        )
    conn.close()
    excel_autosave.sync_excel(list_products())
    return len(to_delete)


def list_products(company_id: Optional[str] = None) -> List[Product]:
    conn = _connect()
    if company_id:
        rows = conn.execute(
            "SELECT data FROM products WHERE company_id = ? ORDER BY created_at DESC",
            (company_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT data FROM products ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [Product.from_dict(json.loads(r[0])) for r in rows]


def get_product(product_id: str) -> Optional[Product]:
    conn = _connect()
    row = conn.execute(
        "SELECT data FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return Product.from_dict(json.loads(row[0]))


def search_products(query: str, company_id: Optional[str] = None) -> List[Tuple[Product, str]]:
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
    company_sql = " AND company_id = ?" if company_id else ""
    company_params = [company_id] if company_id else []

    def _run(where_sql: str, where_params: list) -> List[Product]:
        sql = f"SELECT data FROM products WHERE ({where_sql}){company_sql} ORDER BY created_at DESC"
        rows = conn.execute(sql, where_params + company_params).fetchall()
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
    company_id: Optional[str] = None,
    start_if_empty: int = DEFAULT_SKU_RANGE_START,
) -> List[str]:
    """Returns `count` new sequential numeric SKUs as strings, continuing
    from one past the highest purely-numeric SKU currently in use (scoped
    to company_id if given) — or starting at `start_if_empty` if none exist
    yet. Non-numeric SKUs (manual entries like "Test1") are ignored when
    computing the next number, so auto-assigned and hand-typed SKUs can
    coexist without colliding."""
    conn = _connect()
    if company_id:
        rows = conn.execute("SELECT sku FROM products WHERE company_id = ?", (company_id,)).fetchall()
    else:
        rows = conn.execute("SELECT sku FROM products").fetchall()
    conn.close()

    numeric_skus = [int(r[0]) for r in rows if r[0] and r[0].isdigit()]
    next_start = (max(numeric_skus) + 1) if numeric_skus else start_if_empty
    return [str(next_start + i) for i in range(count)]
