"""PostgreSQL-backed inventory persistence — the single source of truth. There
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
from typing import Dict, List, Optional, Tuple

from modules import db, product_translation_store
from modules.models import Product

# Search result tiers, in priority order (see search_products()).
MATCH_TIER_SKU = "sku"
MATCH_TIER_EAN = "ean"
MATCH_TIER_ASIN = "asin"
MATCH_TIER_MODEL = "model"
MATCH_TIER_BRAND_NAME = "brand_name"

DEFAULT_SKU_RANGE_START = 2000


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT 'default',
            created_at DOUBLE PRECISION,
            data TEXT
        )
        """
    )

    # Migrate older DBs, adding denormalized search columns as needed.
    existing_cols = db.table_columns(conn, "products")
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
        "status": "TEXT",
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
            "OR grade IS NULL OR location IS NULL OR status IS NULL"
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
                "sku=?, manifest_import_id=?, triage_status=?, grade=?, location=?, status=? WHERE id=?",
                (
                    d.get("ean", ""), d.get("asin", ""), d.get("model_number", ""),
                    d.get("model", ""), d.get("brand", ""), d.get("name", ""),
                    d.get("sku", ""), d.get("manifest_import_id", ""), triage_status,
                    d.get("grade", ""), d.get("location", ""), d.get("status", ""), rid,
                ),
            )
        conn.commit()

    # Migrate older DBs: "grade" was renamed to "product_condition" (only
    # the name — the A/B/C/D scale itself is unchanged). Backfill straight
    # from the old `grade` column rather than the JSON blob, since it's
    # already kept in exact sync on every save — simpler than the usual
    # JSON-backfill pattern above. The old `grade` column is left in place,
    # unused, same "additive, never destructive" migration approach used
    # everywhere else in this codebase.
    if "product_condition" not in existing_cols:
        conn.execute("ALTER TABLE products ADD COLUMN product_condition TEXT")
        conn.execute("UPDATE products SET product_condition = grade WHERE grade IS NOT NULL")
        conn.commit()

    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_company ON products(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_asin ON products(asin)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_model_number ON products(model_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_manifest_import_id ON products(manifest_import_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_triage_status ON products(triage_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_product_condition ON products(product_condition)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_location ON products(location)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_status ON products(status)")

    # search_products()/list_products_paginated() filter sku/name/brand/
    # model/model_number/ean/asin/location with "LIKE '%text%'" — a plain
    # B-tree index (above) can't accelerate a leading-wildcard match, so
    # Postgres falls back to a sequential scan. pg_trgm's GIN indexes
    # below DO speed that up; the B-tree ones above stay (still used by
    # exact-match filters like status = ? / company_id = ?), these are
    # additive, not a replacement.
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_sku_trgm ON products USING gin (sku gin_trgm_ops)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_name_trgm ON products USING gin (name gin_trgm_ops)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_brand_trgm ON products USING gin (brand gin_trgm_ops)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_model_trgm ON products USING gin (model gin_trgm_ops)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_model_number_trgm "
        "ON products USING gin (model_number gin_trgm_ops)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_ean_trgm ON products USING gin (ean gin_trgm_ops)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_asin_trgm ON products USING gin (asin gin_trgm_ops)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_location_trgm ON products USING gin (location gin_trgm_ops)"
    )

    # Unlike SQLite (which auto-commits DDL), Postgres leaves CREATE
    # TABLE/INDEX/ALTER TABLE above uncommitted until this — without it,
    # a caller that never opens a "with conn:" block (e.g. a read-only
    # list/get function) would roll the schema setup back on conn.close().
    conn.commit()
    return conn


def _search_columns(p: Product) -> tuple:
    return (
        p.ean, p.asin, p.model_number, p.model, p.brand, p.name, p.sku, p.manifest_import_id,
        p.triage_status, p.product_condition, p.location, p.status,
    )


def _hydrate_primary_language_bulk(products: List[Product]) -> List[Product]:
    """The single join point between `products` and `product_translations`:
    overwrites each Product's name/product_description/condition_description/
    defects/box_contents/missing_components with its `primary_language`
    translation row (the actual source of truth for that content — see
    modules/product_translation_store.py). One bulk query regardless of how
    many products are passed in, so listing a whole company's inventory
    never does a per-row lookup. A product with no translation row yet
    (created before this feature, and not yet migrated) is left with
    whatever the JSON blob already had, so nothing regresses for
    unmigrated data."""
    if not products:
        return products
    by_product = product_translation_store.list_translations_for_products([p.id for p in products])
    for p in products:
        t = by_product.get(p.id, {}).get(p.primary_language)
        if t is not None:
            p.name = t.title
            p.product_description = t.description
            p.condition_description = t.condition_description
            p.defects = t.defects
            p.box_contents = t.box_contents
            p.missing_components = t.missing_components
    return products


def _hydrate_primary_language(product: Optional[Product]) -> Optional[Product]:
    if product is None:
        return None
    return _hydrate_primary_language_bulk([product])[0]


_LANGUAGE_CONTENT_KEYS = (
    "name", "product_description", "condition_description", "defects", "box_contents", "missing_components",
)


def _persist_dict(product: Product) -> dict:
    """`products.data` stays language-neutral — name/product_description/
    condition_description/defects live only in `product_translations`
    (see save_product()/_hydrate_primary_language_bulk() above), so they're
    dropped here rather than persisted twice with two different sources of
    truth."""
    d = product.to_dict()
    for key in _LANGUAGE_CONTENT_KEYS:
        d.pop(key, None)
    return d


def save_product(product: Product, translated_by: str = "manual") -> None:
    """Saves to PostgreSQL — the single source of truth. No Excel mirror to
    keep in sync anymore; export a spreadsheet on demand instead (see
    modules/export.py) if one is needed.

    `name`/`product_description`/`condition_description`/`defects` are
    persisted into `product_translations` (language=`product.primary_language`),
    not treated as authoritative in the `products` row itself — see
    modules/product_translation_store.py. `translated_by` records who/what
    produced this content; callers doing the very first AI generation pass
    an explicit `"ai"`, everything else (product-card edits, bulk edit,
    manifest import) defaults to `"manual"`.
    """
    assert product.company_id, "Product.company_id must be set before saving — never save an orphaned record."
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO products
               (id, company_id, created_at, ean, asin, model_number, model, brand, name, sku,
                manifest_import_id, triage_status, product_condition, location, status, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   company_id = EXCLUDED.company_id, created_at = EXCLUDED.created_at,
                   ean = EXCLUDED.ean, asin = EXCLUDED.asin, model_number = EXCLUDED.model_number,
                   model = EXCLUDED.model, brand = EXCLUDED.brand, name = EXCLUDED.name,
                   sku = EXCLUDED.sku, manifest_import_id = EXCLUDED.manifest_import_id,
                   triage_status = EXCLUDED.triage_status, product_condition = EXCLUDED.product_condition,
                   location = EXCLUDED.location, status = EXCLUDED.status, data = EXCLUDED.data""",
            (product.id, product.company_id, product.created_at, *_search_columns(product),
             json.dumps(_persist_dict(product))),
        )
    conn.close()
    product_translation_store.upsert_translation(product_translation_store.ProductTranslation(
        product_id=product.id, company_id=product.company_id, language=product.primary_language,
        title=product.name, description=product.product_description,
        condition_description=product.condition_description, defects=product.defects,
        box_contents=product.box_contents, missing_components=product.missing_components,
        spec_summary=product.spec_summary, functional_checklist=product.functional_checklist,
        product_condition_reasoning=product.product_condition_reasoning, match_notes=product.match_notes,
        color=product.color,
        translated_by=translated_by,
    ))


def save_products_bulk(products: List[Product]) -> None:
    """Saves many products in one transaction (e.g. a manifest import)."""
    if not products:
        return
    assert all(p.company_id for p in products), "Every Product.company_id must be set before saving."
    conn = _connect()
    with conn:
        conn.executemany(
            """INSERT INTO products
               (id, company_id, created_at, ean, asin, model_number, model, brand, name, sku,
                manifest_import_id, triage_status, product_condition, location, status, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   company_id = EXCLUDED.company_id, created_at = EXCLUDED.created_at,
                   ean = EXCLUDED.ean, asin = EXCLUDED.asin, model_number = EXCLUDED.model_number,
                   model = EXCLUDED.model, brand = EXCLUDED.brand, name = EXCLUDED.name,
                   sku = EXCLUDED.sku, manifest_import_id = EXCLUDED.manifest_import_id,
                   triage_status = EXCLUDED.triage_status, product_condition = EXCLUDED.product_condition,
                   location = EXCLUDED.location, status = EXCLUDED.status, data = EXCLUDED.data""",
            [
                (p.id, p.company_id, p.created_at, *_search_columns(p), json.dumps(_persist_dict(p)))
                for p in products
            ],
        )
    conn.close()
    product_translation_store.upsert_translations_bulk([
        product_translation_store.ProductTranslation(
            product_id=p.id, company_id=p.company_id, language=p.primary_language,
            title=p.name, description=p.product_description,
            condition_description=p.condition_description, defects=p.defects,
            box_contents=p.box_contents, missing_components=p.missing_components,
            spec_summary=p.spec_summary, functional_checklist=p.functional_checklist,
            product_condition_reasoning=p.product_condition_reasoning, match_notes=p.match_notes,
            color=p.color,
            translated_by="manual",
        )
        for p in products
    ])


def delete_product(product_id: str, company_id: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM products WHERE id = ? AND company_id = ?", (product_id, company_id))
    conn.close()
    product_translation_store.delete_translations_for_product(product_id, company_id)


def find_resumable_item(company_id: str) -> Optional[Product]:
    """The New Item wizard checkpoints status="in_progress" + wizard_step
    on every forward step (see app.py) so a lost session — most commonly a
    phone call interrupting mobile photo capture — has something durable
    to resume from. Returns the most recently created in_progress record,
    if any; list_products() already orders by created_at DESC."""
    items = list_products(company_id, status="in_progress")
    return items[0] if items else None


def revert_manifest_draft(product_id: str, company_id: str) -> None:
    """Undoes New Item wizard progress on a manifest-sourced item, back to
    exactly the pristine draft rows_to_draft_products() would have created
    — used when a user discards a resumable in-progress item rather than
    finishing it. Only the manifest-origin fields (plus sku/location,
    seeded once at import time) survive; every AI/manual field the wizard
    touched is dropped. A product with no manifest_import_id has no
    "draft" to revert to — callers should delete_product() it instead."""
    p = get_product(product_id, company_id)
    if p is None or not p.manifest_import_id:
        return
    fresh = Product(
        id=p.id, company_id=p.company_id, status="draft",
        sku=p.sku, location=p.location,
        manifest_import_id=p.manifest_import_id,
        manifest_target_no=p.manifest_target_no,
        manifest_subcategory=p.manifest_subcategory,
        asin=p.asin,
        manifest_barcode=p.manifest_barcode,
        manifest_item_description=p.manifest_item_description,
        manifest_qty=p.manifest_qty,
        manifest_weight_kg=p.manifest_weight_kg,
        quantity=p.manifest_qty if p.manifest_qty > 0 else 1,
        weight_kg=p.manifest_weight_kg if p.manifest_weight_kg > 0 else 0.0,
        created_at=p.created_at,
    )
    save_product(fresh)


def list_products_by_manifest(manifest_import_id: str, company_id: str) -> List[Product]:
    """All products (any status) linked to a given manifest batch."""
    conn = _connect()
    rows = conn.execute(
        "SELECT data FROM products WHERE manifest_import_id = ? AND company_id = ? "
        "ORDER BY created_at ASC",
        (manifest_import_id, company_id),
    ).fetchall()
    conn.close()
    return _hydrate_primary_language_bulk([Product.from_dict(json.loads(r[0])) for r in rows])


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
            "DELETE FROM products WHERE id = ? AND company_id = ?",
            [(p.id, company_id) for p in to_delete],
        )
    conn.close()
    for p in to_delete:
        product_translation_store.delete_translations_for_product(p.id, company_id)
    return len(to_delete)


def list_products(company_id: str, status: Optional[str] = None) -> List[Product]:
    conn = _connect()
    if status:
        rows = conn.execute(
            "SELECT data FROM products WHERE company_id = ? AND status = ? ORDER BY created_at DESC",
            (company_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT data FROM products WHERE company_id = ? ORDER BY created_at DESC",
            (company_id,),
        ).fetchall()
    conn.close()
    return _hydrate_primary_language_bulk([Product.from_dict(json.loads(r[0])) for r in rows])


def list_skus(company_id: str) -> set:
    """Just SKUs — no JSON deserialization, no full Product hydration.
    Existence-check use (e.g. catalog-import dedup), not for display."""
    conn = _connect()
    rows = conn.execute("SELECT sku FROM products WHERE company_id = ?", (company_id,)).fetchall()
    conn.close()
    return {r[0] for r in rows if r[0]}


def get_product(product_id: str, company_id: str) -> Optional[Product]:
    conn = _connect()
    row = conn.execute(
        "SELECT data FROM products WHERE id = ? AND company_id = ?", (product_id, company_id)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return _hydrate_primary_language(Product.from_dict(json.loads(row[0])))


def get_product_by_sku(company_id: str, sku: str) -> Optional[Product]:
    """Exact-match SKU lookup (same tier-1 logic search_products() uses),
    single-purpose for the Orders page's "click a SKU to open its product
    card" navigation — not a general search entry point."""
    if not sku:
        return None
    conn = _connect()
    row = conn.execute(
        "SELECT data FROM products WHERE UPPER(sku) = UPPER(?) AND company_id = ? LIMIT 1", (sku, company_id)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return _hydrate_primary_language(Product.from_dict(json.loads(row[0])))


def find_sku_conflicts(company_id: str, product_ids: List[str]) -> Dict[str, Product]:
    """For each given product id, if another product (different id) in the
    SAME company shares its SKU (case-insensitive, blank SKUs never
    conflict), returns {product_id: the_other_product}. Used by
    app.py's post-import SKU-conflict resolution dialog — manifest import
    deliberately no longer avoids/flags collisions before saving (see
    _start_manifest_import's docstring), so this is how they're found
    afterward, scoped to just the batch's own products rather than
    scanning the whole company."""
    conn = _connect()
    conflicts: Dict[str, Product] = {}
    for pid in product_ids:
        row = conn.execute(
            "SELECT p2.data FROM products p1 JOIN products p2 "
            "ON UPPER(p1.sku) = UPPER(p2.sku) AND p1.company_id = p2.company_id "
            "WHERE p1.id = ? AND p1.company_id = ? AND p2.id != ? AND p1.sku != ''",
            (pid, company_id, pid),
        ).fetchone()
        if row:
            conflicts[pid] = Product.from_dict(json.loads(row[0]))
    conn.close()
    return conflicts


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
        _run("model_number ILIKE ? OR model ILIKE ?", [like, like]),
        MATCH_TIER_MODEL,
    )
    _add(
        _run("brand ILIKE ? OR name ILIKE ?", [like, like]),
        MATCH_TIER_BRAND_NAME,
    )

    conn.close()
    _hydrate_primary_language_bulk([p for p, _tier in results])
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
    product_condition: Optional[str] = None,
    status: Optional[str] = None,
    exclude_status: Optional[str] = None,
    sku: Optional[str] = None,
    name: Optional[str] = None,
    brand: Optional[str] = None,
    model: Optional[str] = None,
    ean: Optional[str] = None,
    asin: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> Tuple[List[Product], int]:
    """The default Inventory-tab query: filtered on indexed columns, and
    always bounded by LIMIT/OFFSET so at most one page of rows is ever
    loaded into Python/the browser — the thing the plain list_products()
    loop-per-row rendering can't do at 1,000-20,000 rows.

    `sku`/`name`/`brand`/`model`/`ean`/`asin`/`location` are each an
    independent, lightweight substring match — every one supplied combines
    with AND (not OR), so e.g. sku="200" + brand="Ninja" only matches rows
    where BOTH are true. For a single specific lookup by exact SKU/EAN/ASIN
    across the *whole* inventory regardless of filters, use
    search_products() instead.

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
        where_clauses.append("location ILIKE ?")
        params.append(f"%{location.strip()}%")
    if product_condition:
        where_clauses.append("product_condition = ?")
        params.append(product_condition)
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if exclude_status:
        where_clauses.append("status != ?")
        params.append(exclude_status)
    for field_name, field_value in (
        ("sku", sku), ("name", name), ("brand", brand),
        ("model", model), ("ean", ean), ("asin", asin),
    ):
        if field_value and field_value.strip():
            where_clauses.append(f"{field_name} ILIKE ?")
            params.append(f"%{field_value.strip()}%")

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

    products = _hydrate_primary_language_bulk([Product.from_dict(json.loads(r[0])) for r in rows])
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
