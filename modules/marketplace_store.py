"""Per-marketplace listing status — one row per (product, marketplace)
pair, since a product can be listed on several channels (BaseLinker, eBay,
WooCommerce, ...) at once, each with its own external ID/status/price.

This is the channel-agnostic replacement for the old BaseLinker-specific
`Product.baselinker_product_id`/`baselinker_synced_at` fields (modules/
models.py): modules/baselinker_client.py reads/writes rows here with
marketplace="baselinker" — a future eBay/WooCommerce client would write
marketplace="ebay"/"woocommerce" rows to this SAME table, no schema change.

Shares the same SQLite file as modules/inventory_store.py.
"""
import sqlite3
import time
from dataclasses import dataclass, field
from typing import List, Optional

from modules.inventory_store import DB_PATH

STATUS_NOT_LISTED = "not_listed"
STATUS_LISTED = "listed"
STATUS_SOLD = "sold"
STATUS_REMOVED = "removed"
STATUS_ERROR = "error"


@dataclass
class MarketplaceListing:
    product_id: str = ""
    marketplace: str = ""  # "baselinker" | "ebay" | "woocommerce" | ...
    external_listing_id: str = ""
    status: str = STATUS_NOT_LISTED
    price: float = 0.0
    url: str = ""
    last_synced_at: float = 0.0
    error_message: str = ""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS marketplace_listings (
            product_id TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            external_listing_id TEXT,
            status TEXT NOT NULL DEFAULT 'not_listed',
            price REAL,
            url TEXT,
            last_synced_at REAL,
            error_message TEXT,
            PRIMARY KEY (product_id, marketplace)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_marketplace_listings_product ON marketplace_listings(product_id)")
    return conn


def _row_to_listing(r: tuple) -> MarketplaceListing:
    return MarketplaceListing(
        product_id=r[0], marketplace=r[1], external_listing_id=r[2] or "",
        status=r[3] or STATUS_NOT_LISTED, price=r[4] or 0.0, url=r[5] or "",
        last_synced_at=r[6] or 0.0, error_message=r[7] or "",
    )


def upsert_listing(listing: MarketplaceListing) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO marketplace_listings
               (product_id, marketplace, external_listing_id, status, price, url, last_synced_at, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                listing.product_id, listing.marketplace, listing.external_listing_id,
                listing.status, listing.price, listing.url, listing.last_synced_at,
                listing.error_message,
            ),
        )
    conn.close()


def get_listing(product_id: str, marketplace: str) -> Optional[MarketplaceListing]:
    conn = _connect()
    row = conn.execute(
        "SELECT product_id, marketplace, external_listing_id, status, price, url, last_synced_at, error_message "
        "FROM marketplace_listings WHERE product_id = ? AND marketplace = ?",
        (product_id, marketplace),
    ).fetchone()
    conn.close()
    return _row_to_listing(row) if row else None


def list_listings(product_id: str) -> List[MarketplaceListing]:
    conn = _connect()
    rows = conn.execute(
        "SELECT product_id, marketplace, external_listing_id, status, price, url, last_synced_at, error_message "
        "FROM marketplace_listings WHERE product_id = ? ORDER BY marketplace ASC",
        (product_id,),
    ).fetchall()
    conn.close()
    return [_row_to_listing(r) for r in rows]


def delete_listings_for_product(product_id: str) -> int:
    """Used when a product itself is deleted, to avoid orphaned listing rows."""
    conn = _connect()
    with conn:
        cur = conn.execute("DELETE FROM marketplace_listings WHERE product_id = ?", (product_id,))
        count = cur.rowcount
    conn.close()
    return count
