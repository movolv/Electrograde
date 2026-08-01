"""Repair history — one row per repair event, since a product can be
repaired more than once over its life. Deliberately its own table rather
than a field on Product (see modules/models.py), because this is a genuine
one-to-many relationship.

Shares the same SQLite file as modules/inventory_store.py.
"""
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules.inventory_store import DB_PATH


@dataclass
class RepairEvent:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    product_id: str = ""
    company_id: str = ""
    occurred_at: float = field(default_factory=time.time)
    description: str = ""
    cost: float = 0.0
    technician: str = ""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repair_events (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            company_id TEXT NOT NULL DEFAULT 'default',
            occurred_at REAL,
            description TEXT,
            cost REAL,
            technician TEXT
        )
        """
    )

    # Migrate older DBs: this table predates company_id entirely — same
    # ALTER TABLE + backfill pattern as modules/inventory_store.py and
    # modules/marketplace_store.py, backfilling from the owning product's
    # company_id (the only source of truth available for legacy rows).
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(repair_events)")}
    if "company_id" not in existing_cols:
        conn.execute("ALTER TABLE repair_events ADD COLUMN company_id TEXT NOT NULL DEFAULT 'default'")
        conn.execute(
            "UPDATE repair_events SET company_id = ("
            "  SELECT products.company_id FROM products WHERE products.id = repair_events.product_id"
            ") WHERE EXISTS (SELECT 1 FROM products WHERE products.id = repair_events.product_id)"
        )
        conn.commit()

    conn.execute("CREATE INDEX IF NOT EXISTS idx_repair_events_product ON repair_events(product_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_repair_events_company ON repair_events(company_id)")
    return conn


def add_repair_event(event: RepairEvent) -> None:
    assert event.company_id, "RepairEvent.company_id must be set before saving."
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO repair_events
               (id, product_id, company_id, occurred_at, description, cost, technician)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event.id, event.product_id, event.company_id, event.occurred_at,
             event.description, event.cost, event.technician),
        )
    conn.close()


def list_repair_events(product_id: str, company_id: str) -> List[RepairEvent]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, product_id, company_id, occurred_at, description, cost, technician "
        "FROM repair_events WHERE product_id = ? AND company_id = ? ORDER BY occurred_at ASC",
        (product_id, company_id),
    ).fetchall()
    conn.close()
    return [
        RepairEvent(
            id=r[0], product_id=r[1], company_id=r[2] or "", occurred_at=r[3],
            description=r[4] or "", cost=r[5] or 0.0, technician=r[6] or "",
        )
        for r in rows
    ]


def delete_repair_event(event_id: str, company_id: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM repair_events WHERE id = ? AND company_id = ?", (event_id, company_id))
    conn.close()


def delete_repair_events_for_product(product_id: str, company_id: str) -> int:
    """Used when a product itself is deleted, to avoid orphaned repair rows."""
    conn = _connect()
    with conn:
        cur = conn.execute(
            "DELETE FROM repair_events WHERE product_id = ? AND company_id = ?", (product_id, company_id)
        )
        count = cur.rowcount
    conn.close()
    return count


def total_repair_cost(product_id: str, company_id: str) -> float:
    conn = _connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM repair_events WHERE product_id = ? AND company_id = ?",
        (product_id, company_id),
    ).fetchone()
    conn.close()
    return float(row[0] or 0.0)
