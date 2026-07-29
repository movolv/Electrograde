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
            occurred_at REAL,
            description TEXT,
            cost REAL,
            technician TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_repair_events_product ON repair_events(product_id)")
    return conn


def add_repair_event(event: RepairEvent) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO repair_events
               (id, product_id, occurred_at, description, cost, technician)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event.id, event.product_id, event.occurred_at, event.description, event.cost, event.technician),
        )
    conn.close()


def list_repair_events(product_id: str) -> List[RepairEvent]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, product_id, occurred_at, description, cost, technician "
        "FROM repair_events WHERE product_id = ? ORDER BY occurred_at ASC",
        (product_id,),
    ).fetchall()
    conn.close()
    return [
        RepairEvent(id=r[0], product_id=r[1], occurred_at=r[2], description=r[3] or "", cost=r[4] or 0.0, technician=r[5] or "")
        for r in rows
    ]


def delete_repair_event(event_id: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM repair_events WHERE id = ?", (event_id,))
    conn.close()


def delete_repair_events_for_product(product_id: str) -> int:
    """Used when a product itself is deleted, to avoid orphaned repair rows."""
    conn = _connect()
    with conn:
        cur = conn.execute("DELETE FROM repair_events WHERE product_id = ?", (product_id,))
        count = cur.rowcount
    conn.close()
    return count


def total_repair_cost(product_id: str) -> float:
    conn = _connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM repair_events WHERE product_id = ?", (product_id,)
    ).fetchone()
    conn.close()
    return float(row[0] or 0.0)
