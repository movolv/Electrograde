"""PostgreSQL-backed order persistence — the local mirror of orders pulled
from any connected marketplace integration (see integrations/base.py's
MarketplaceConnector.fetch_orders() and integrations/scheduler.py's
poll_order_sync_once()). Deliberately integration-agnostic: this module
never imports a connector or assumes BaseLinker — every row is already a
normalized modules.models.Order by the time it reaches upsert_order().

Same shape as modules/inventory_store.py: a handful of denormalized columns
for fast filter/sort/search, plus one JSON `data` blob (a full Order.to_dict())
for the detail view. Every function is scoped by `company_id` — orders carry
customer PII (name/address/email/phone), so tenant isolation here matters
more than almost anywhere else in the app.
"""
import json
import uuid
from typing import List, Optional, Tuple

from modules import db
from modules.models import Order

_SELECT_COLS = (
    "id, company_id, integration_type, marketplace, external_order_id, order_number, "
    "order_source, customer_name, items_summary, item_count, price_total, currency, "
    "shipping_method, order_date, status_id, status_label, status_updated_at, data, "
    "created_at, updated_at"
)


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            integration_type TEXT NOT NULL,
            marketplace TEXT,
            external_order_id TEXT NOT NULL,
            order_number TEXT,
            order_source TEXT,
            customer_name TEXT,
            items_summary TEXT,
            item_count INTEGER,
            searchable_skus TEXT,
            price_total NUMERIC,
            currency TEXT,
            shipping_method TEXT,
            order_date DOUBLE PRECISION,
            status_id INTEGER,
            status_label TEXT,
            status_updated_at DOUBLE PRECISION,
            data TEXT NOT NULL,
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION,
            UNIQUE (company_id, integration_type, external_order_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_company_date ON orders(company_id, order_date DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_company_status ON orders(company_id, status_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_company_customer ON orders(company_id, customer_name)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_company_order_number ON orders(company_id, order_number)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_company_skus ON orders(company_id, searchable_skus)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_company_marketplace ON orders(company_id, marketplace)")

    # Same pg_trgm rationale as inventory_store.py: search()'s ILIKE
    # '%text%' filters need a leading-wildcard-capable index, which a plain
    # B-tree can't provide.
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_order_number_trgm ON orders USING gin (order_number gin_trgm_ops)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_customer_name_trgm ON orders USING gin (customer_name gin_trgm_ops)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_searchable_skus_trgm ON orders USING gin (searchable_skus gin_trgm_ops)"
    )

    # One row per order line item, kept in sync with `orders` by
    # upsert_order()/delete_order() below. Exists solely so
    # find_historical_sale_prices() (modules/pricing.py's own-sales-history
    # price signal) can do an indexed ILIKE over item names instead of
    # decoding every order's JSON `data` blob in Python — at the 100
    # companies x 100k products scale target, a company can accumulate a
    # comparable number of historical order lines, and "load every order,
    # filter in Python" would not hold up at that volume (see
    # feedback_electrograder_scale_target). Deliberately NOT a foreign key
    # to orders (this project enforces tenant scoping at the application
    # layer, not the schema, same as everywhere else in modules/*_store.py).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            name TEXT,
            sku TEXT,
            ean TEXT,
            quantity INTEGER,
            price NUMERIC,
            currency TEXT,
            order_date DOUBLE PRECISION
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_order_items_company_order ON order_items(company_id, order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_order_items_company_sku ON order_items(company_id, sku)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_order_items_name_trgm ON order_items USING gin (name gin_trgm_ops)"
    )

    conn.commit()
    return conn


def _row_to_order(r: tuple) -> Order:
    return Order.from_dict(json.loads(r[17]))


def upsert_order(order: Order, searchable_skus: str = "") -> None:
    """Insert or update by (company_id, integration_type, external_order_id)
    — the stable identity a source system's order keeps across re-syncs.
    `searchable_skus` is a separate param (not derived here) since it's
    purely a search-index concern the caller (order_mapper) already computed
    while building the Order."""
    assert order.company_id, "Order.company_id must be set before saving — never save an orphaned record."
    assert order.integration_type, "Order.integration_type must be set."
    assert order.external_order_id, "Order.external_order_id must be set."
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO orders
               (id, company_id, integration_type, marketplace, external_order_id, order_number,
                order_source, customer_name, items_summary, item_count, searchable_skus,
                price_total, currency, shipping_method, order_date, status_id, status_label,
                status_updated_at, data, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (company_id, integration_type, external_order_id) DO UPDATE SET
                   marketplace = EXCLUDED.marketplace, order_number = EXCLUDED.order_number,
                   order_source = EXCLUDED.order_source, customer_name = EXCLUDED.customer_name,
                   items_summary = EXCLUDED.items_summary, item_count = EXCLUDED.item_count,
                   searchable_skus = EXCLUDED.searchable_skus, price_total = EXCLUDED.price_total,
                   currency = EXCLUDED.currency, shipping_method = EXCLUDED.shipping_method,
                   order_date = EXCLUDED.order_date, status_id = EXCLUDED.status_id,
                   status_label = EXCLUDED.status_label, status_updated_at = EXCLUDED.status_updated_at,
                   data = EXCLUDED.data, updated_at = EXCLUDED.updated_at""",
            (
                order.id, order.company_id, order.integration_type, order.marketplace,
                order.external_order_id, order.order_number, order.order_source, order.customer_name,
                order.items_summary, order.item_count, searchable_skus, order.price_total,
                order.currency, order.shipping_method, order.order_date, order.status_id,
                order.status_label, order.status_updated_at, json.dumps(order.to_dict()),
                order.created_at, order.updated_at,
            ),
        )
        # Re-derive order_items from scratch on every upsert (delete+insert,
        # not a diff) — cheap (an order rarely has more than a handful of
        # lines) and correct even when a re-sync changes the item list,
        # unlike trying to reconcile old vs. new rows individually.
        conn.execute("DELETE FROM order_items WHERE company_id = ? AND order_id = ?", (order.company_id, order.id))
        for item in order.items:
            if item.price <= 0:
                continue  # zero/negative-price lines (bundled freebies, adjustments) would skew the price signal
            conn.execute(
                """INSERT INTO order_items
                   (id, company_id, order_id, name, sku, ean, quantity, price, currency, order_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex[:12], order.company_id, order.id, item.name, item.sku, item.ean,
                    item.quantity, item.price, order.currency, order.order_date,
                ),
            )
    conn.close()


def list_orders_paginated(
    company_id: str,
    marketplace: Optional[str] = None,
    status_id: Optional[int] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> Tuple[List[Order], int]:
    """The Orders page's default query: filtered on indexed columns, always
    bounded by LIMIT/OFFSET so at most one page of rows is ever loaded —
    same shape as inventory_store.list_products_paginated(). `search`
    matches order_number/customer_name/searchable_skus (ILIKE, any one
    hit qualifies); marketplace/status_id are exact-match filters, combined
    with AND when both given."""
    where_clauses = ["company_id = ?"]
    params: list = [company_id]

    if marketplace:
        where_clauses.append("marketplace = ?")
        params.append(marketplace)
    if status_id is not None:
        where_clauses.append("status_id = ?")
        params.append(status_id)
    if search and search.strip():
        like = f"%{search.strip()}%"
        where_clauses.append("(order_number ILIKE ? OR customer_name ILIKE ? OR searchable_skus ILIKE ?)")
        params.extend([like, like, like])

    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    conn = _connect()
    total = conn.execute(f"SELECT COUNT(*) FROM orders {where_sql}", params).fetchone()[0]

    page = max(1, page)
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM orders {where_sql} ORDER BY order_date DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    conn.close()
    return [_row_to_order(r) for r in rows], total


def get_order(order_id: str, company_id: str) -> Optional[Order]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM orders WHERE id = ? AND company_id = ?", (order_id, company_id)
    ).fetchone()
    conn.close()
    return _row_to_order(row) if row else None


def delete_order(order_id: str, company_id: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM orders WHERE id = ? AND company_id = ?", (order_id, company_id))
        conn.execute("DELETE FROM order_items WHERE order_id = ? AND company_id = ?", (order_id, company_id))
    conn.close()


def find_historical_sale_prices(
    company_id: str, brand: str, model: str, currency: str, max_results: int = 20,
) -> List[float]:
    """The strongest pricing signal available for a new item: this same
    company's own confirmed past sale prices of the same brand+model, in
    the same currency — zero cost, zero web-scraping noise, and already in
    this company's actual market. See modules/pricing.py's estimate_price(),
    which tries this before falling back to a web search.

    Matches on order_items.name (the marketplace listing title at time of
    sale — reliably contains brand+model, since that's exactly what
    integrations/marketplaces/*/mapper.py puts there) via the trigram-
    indexed ILIKE below, NOT by decoding every order's JSON blob — see
    idx_order_items_name_trgm in _connect(). `currency` is required and
    exact-matched (not defaulted/guessed) so a company with mixed-currency
    order history never has its own numbers silently averaged together
    across currencies, which was the exact class of bug this whole feature
    exists to avoid in the web-search fallback."""
    brand = (brand or "").strip()
    model = (model or "").strip()
    currency = (currency or "").strip()
    if not currency or (not brand and not model):
        return []

    where = ["company_id = ?", "currency = ?"]
    params: list = [company_id, currency]
    if brand:
        where.append("name ILIKE ?")
        params.append(f"%{brand}%")
    if model:
        where.append("name ILIKE ?")
        params.append(f"%{model}%")

    conn = _connect()
    rows = conn.execute(
        f"SELECT price FROM order_items WHERE {' AND '.join(where)} ORDER BY order_date DESC LIMIT ?",
        params + [max_results],
    ).fetchall()
    conn.close()
    return [float(r[0]) for r in rows]


def count_orders(company_id: str) -> int:
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) FROM orders WHERE company_id = ?", (company_id,)).fetchone()[0]
    conn.close()
    return total
