"""Product-level field change history — "what changed, and which system
produced it." Distinct from modules/sync_log_store.py's sync_logs (which
records a SYNC DECISION outcome: accepted/overridden/pending) — this table
records the underlying field CHANGE EVENT itself, whichever system it
happened on, before any sync decision is even made about it.

`source_system`: which SYSTEM the change happened on — "electrograder" or
"baselinker" — always one of exactly these two, making it immediately
clear which side produced the last change. `changed_by` is the finer-
grained actor within that system (e.g. "user:<id>", "system:pull_sync";
future: "system:manifest_import"/"system:grading"/"system:ai_processing"/
"system:bulk_update").

Shares the same SQLite file as modules/inventory_store.py.
"""
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import List

from modules.inventory_store import DB_PATH

SOURCE_ELECTROGRADER = "electrograder"
SOURCE_BASELINKER = "baselinker"


@dataclass
class ProductChangeEntry:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    product_id: str = ""
    field_name: str = ""
    old_value: str = ""
    new_value: str = ""
    source_system: str = ""
    changed_by: str = ""
    created_at: float = 0.0


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_change_log (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT NOT NULL DEFAULT '',
            new_value TEXT NOT NULL DEFAULT '',
            source_system TEXT NOT NULL DEFAULT '',
            changed_by TEXT NOT NULL DEFAULT '',
            created_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_change_log_company ON product_change_log(company_id, product_id)"
    )

    # Migrate older DBs: "grade" field was renamed to "product_condition"
    # (integrations/field_registry.py) — keep history rows pointed at the
    # field's current name.
    conn.execute("UPDATE product_change_log SET field_name = 'product_condition' WHERE field_name = 'grade'")
    conn.commit()
    return conn


def record_change(
    company_id: str, product_id: str, field_name: str, old_value: str, new_value: str,
    source_system: str, changed_by: str = "",
) -> None:
    assert company_id, "company_id is required."
    assert source_system in (SOURCE_ELECTROGRADER, SOURCE_BASELINKER), \
        f"source_system must be {SOURCE_ELECTROGRADER!r} or {SOURCE_BASELINKER!r}, got {source_system!r}."
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO product_change_log
               (id, company_id, product_id, field_name, old_value, new_value, source_system,
                changed_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex[:12], company_id, product_id, field_name, str(old_value), str(new_value),
                source_system, changed_by, time.time(),
            ),
        )
    conn.close()


def list_changes(company_id: str, product_id: str = "", limit: int = 50) -> List[ProductChangeEntry]:
    conn = _connect()
    if product_id:
        rows = conn.execute(
            "SELECT id, company_id, product_id, field_name, old_value, new_value, source_system, "
            "changed_by, created_at FROM product_change_log WHERE company_id = ? AND product_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (company_id, product_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, company_id, product_id, field_name, old_value, new_value, source_system, "
            "changed_by, created_at FROM product_change_log WHERE company_id = ? ORDER BY created_at DESC LIMIT ?",
            (company_id, limit),
        ).fetchall()
    conn.close()
    return [
        ProductChangeEntry(
            id=r[0], company_id=r[1], product_id=r[2], field_name=r[3], old_value=r[4] or "",
            new_value=r[5] or "", source_system=r[6] or "", changed_by=r[7] or "", created_at=r[8] or 0.0,
        )
        for r in rows
    ]
