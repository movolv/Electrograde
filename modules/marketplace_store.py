"""Per-marketplace listing status — one row per (product, marketplace)
pair, since a product can be listed on several channels (BaseLinker, eBay,
WooCommerce, ...) at once, each with its own external ID/status/price.

This is the channel-agnostic replacement for the old BaseLinker-specific
`Product.baselinker_product_id`/`baselinker_synced_at` fields (modules/
models.py): integrations/marketplaces/baselinker/client.py (via
integrations/base.py's MarketplaceConnector.export_product()) reads/writes
rows here with marketplace="baselinker" — an eBay/WooCommerce connector
writes marketplace="ebay"/"woocommerce" rows to this SAME table, no schema
change.

Shares the same PostgreSQL database as every other modules/*_store.py (modules/db.py).
"""
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from modules import db
from modules.models import Product

STATUS_NOT_LISTED = "not_listed"
STATUS_LISTED = "listed"
STATUS_SOLD = "sold"
STATUS_REMOVED = "removed"
STATUS_ERROR = "error"


@dataclass
class MarketplaceListing:
    product_id: str = ""
    marketplace: str = ""  # "baselinker" | "ebay" | "woocommerce" | ...
    company_id: str = ""
    external_listing_id: str = ""
    status: str = STATUS_NOT_LISTED
    price: float = 0.0
    url: str = ""
    last_synced_at: float = 0.0
    error_message: str = ""


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS marketplace_listings (
            product_id TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            company_id TEXT NOT NULL DEFAULT 'default',
            external_listing_id TEXT,
            status TEXT NOT NULL DEFAULT 'not_listed',
            price DOUBLE PRECISION,
            url TEXT,
            last_synced_at DOUBLE PRECISION,
            error_message TEXT,
            PRIMARY KEY (product_id, marketplace)
        )
        """
    )

    # Migrate older DBs: this table predates company_id entirely, so — same
    # ALTER TABLE + backfill pattern as modules/inventory_store.py — add the
    # column and backfill existing rows from their owning product's
    # company_id (the only source of truth available for legacy rows; a
    # listing can't outlive/precede the product it belongs to).
    existing_cols = db.table_columns(conn, "marketplace_listings")
    if "company_id" not in existing_cols:
        conn.execute("ALTER TABLE marketplace_listings ADD COLUMN company_id TEXT NOT NULL DEFAULT 'default'")
        conn.execute(
            "UPDATE marketplace_listings SET company_id = ("
            "  SELECT products.company_id FROM products WHERE products.id = marketplace_listings.product_id"
            ") WHERE EXISTS (SELECT 1 FROM products WHERE products.id = marketplace_listings.product_id)"
        )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_marketplace_listings_product ON marketplace_listings(product_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_marketplace_listings_company ON marketplace_listings(company_id)")
    conn.commit()
    return conn


def _row_to_listing(r: tuple) -> MarketplaceListing:
    return MarketplaceListing(
        product_id=r[0], marketplace=r[1], company_id=r[2] or "",
        external_listing_id=r[3] or "",
        status=r[4] or STATUS_NOT_LISTED, price=r[5] or 0.0, url=r[6] or "",
        last_synced_at=r[7] or 0.0, error_message=r[8] or "",
    )


_SELECT_COLS = (
    "product_id, marketplace, company_id, external_listing_id, status, price, url, "
    "last_synced_at, error_message"
)


def upsert_listing(listing: MarketplaceListing) -> None:
    assert listing.company_id, "MarketplaceListing.company_id must be set before saving."
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO marketplace_listings
               (product_id, marketplace, company_id, external_listing_id, status, price, url,
                last_synced_at, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (product_id, marketplace) DO UPDATE SET
                   company_id = EXCLUDED.company_id, external_listing_id = EXCLUDED.external_listing_id,
                   status = EXCLUDED.status, price = EXCLUDED.price, url = EXCLUDED.url,
                   last_synced_at = EXCLUDED.last_synced_at, error_message = EXCLUDED.error_message""",
            (
                listing.product_id, listing.marketplace, listing.company_id, listing.external_listing_id,
                listing.status, listing.price, listing.url, listing.last_synced_at,
                listing.error_message,
            ),
        )
    conn.close()


def get_listing(product_id: str, marketplace: str, company_id: str) -> Optional[MarketplaceListing]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM marketplace_listings "
        "WHERE product_id = ? AND marketplace = ? AND company_id = ?",
        (product_id, marketplace, company_id),
    ).fetchone()
    conn.close()
    return _row_to_listing(row) if row else None


def list_listings(product_id: str, company_id: str) -> List[MarketplaceListing]:
    conn = _connect()
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM marketplace_listings "
        "WHERE product_id = ? AND company_id = ? ORDER BY marketplace ASC",
        (product_id, company_id),
    ).fetchall()
    conn.close()
    return [_row_to_listing(r) for r in rows]


def list_listings_for_products(product_ids: List[str], company_id: str) -> Dict[str, List[MarketplaceListing]]:
    """Bulk counterpart to list_listings() — ONE query for a whole page of
    products instead of one per row, keyed {product_id: [MarketplaceListing, ...]}.

    Backs the Product List's Integrations column. Rendering a 50-row page
    used to call get_listing() once per row (one DB round-trip each, and
    hardcoded to a single marketplace) — exactly the N+1 pattern
    list_products_with_listing() below already exists to avoid elsewhere.
    Same bulk shape as product_translation_store.list_translations_for_products().

    Only products that ACTUALLY have a listing row appear as keys: a
    product listed nowhere is simply absent, never an empty-but-present
    entry, so callers can't accidentally render an integration a product
    isn't really on. Company-scoped, so one tenant's listings can never
    surface in another's list."""
    if not product_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in product_ids)
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM marketplace_listings "
        f"WHERE company_id = ? AND product_id IN ({placeholders}) ORDER BY marketplace ASC",
        [company_id] + list(product_ids),
    ).fetchall()
    conn.close()
    out: Dict[str, List[MarketplaceListing]] = {}
    for r in rows:
        listing = _row_to_listing(r)
        out.setdefault(listing.product_id, []).append(listing)
    return out


def delete_listing(product_id: str, marketplace: str, company_id: str) -> bool:
    """Unlinks ONE product from ONE marketplace — deletes this row and
    nothing else.

    LOCAL ONLY, deliberately and permanently: this never calls the
    marketplace's API and never touches the Product itself. The case it
    exists for is "the listing is already gone on the portal's side, and
    ElectroGrader's record of it is now stale" — a remote delete would at
    best be a no-op and at worst remove a listing the user still wants.
    Re-exporting the product later simply creates a fresh row.

    Returns True if a row was actually removed (False if there was
    nothing to unlink — not an error)."""
    conn = _connect()
    with conn:
        cur = conn.execute(
            "DELETE FROM marketplace_listings WHERE product_id = ? AND marketplace = ? AND company_id = ?",
            (product_id, marketplace, company_id),
        )
        removed = cur.rowcount > 0
    conn.close()
    return removed


def delete_listings_for_product(product_id: str, company_id: str) -> int:
    """Used when a product itself is deleted, to avoid orphaned listing rows."""
    conn = _connect()
    with conn:
        cur = conn.execute(
            "DELETE FROM marketplace_listings WHERE product_id = ? AND company_id = ?",
            (product_id, company_id),
        )
        count = cur.rowcount
    conn.close()
    return count


def list_products_with_listing(company_id: str, marketplace: str) -> List[Product]:
    """Products that actually have a real external listing for this
    marketplace — one indexed JOIN instead of loading every product for
    the company and checking each one's listing individually (the old
    N+1 pattern integrations/scheduler.py's pull-sync used to use, which
    meant one DB round-trip per product on top of loading the whole
    company's catalog into memory)."""
    conn = _connect()
    rows = conn.execute(
        """SELECT p.data FROM products p
           JOIN marketplace_listings m ON m.product_id = p.id AND m.company_id = p.company_id
           WHERE p.company_id = ? AND m.marketplace = ?
             AND m.external_listing_id IS NOT NULL AND m.external_listing_id != ''""",
        (company_id, marketplace),
    ).fetchall()
    conn.close()
    return [Product.from_dict(json.loads(r[0])) for r in rows]
