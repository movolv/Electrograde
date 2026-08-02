"""Field-level sync history — a granular record of what happened to ONE
field on ONE product on ONE sync decision (accepted / overridden /
pending-review), distinct from both:
  - modules/sync_record_store.py's sync_records: current STATE per
    (product, connector, direction), not history;
  - modules/integration_store.py's integration_sync_log: whole-integration
    attempt log (export/test_connection/etc.), not field-level.

Written by sync/conflict_resolver.py's resolve_field_change() — nothing in
today's live app calls that function yet (no real BaseLinker incoming-change
API exists), so this table is normally empty in production, same as
sync_conflicts before it.

Shares the same SQLite file as modules/inventory_store.py.
"""
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import List

from modules.inventory_store import DB_PATH


@dataclass
class SyncLogEntry:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    product_id: str = ""
    connector_name: str = ""
    field_name: str = ""
    old_value: str = ""
    new_value: str = ""
    direction: str = ""  # sync_ownership_store.FLOW_* or a conflict_policy value
    source: str = ""  # who made the change: "electrograder" | connector_name
    created_at: float = 0.0


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_logs (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            connector_name TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT NOT NULL DEFAULT '',
            new_value TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            created_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_logs_company ON sync_logs(company_id, product_id)"
    )
    return conn


def record_log(
    company_id: str, product_id: str, connector_name: str, field_name: str,
    old_value: str = "", new_value: str = "", direction: str = "", source: str = "",
) -> None:
    assert company_id, "company_id is required."
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO sync_logs
               (id, company_id, product_id, connector_name, field_name, old_value, new_value,
                direction, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex[:12], company_id, product_id, connector_name, field_name,
                str(old_value), str(new_value), direction, source, time.time(),
            ),
        )
    conn.close()


def list_logs(company_id: str, product_id: str = "", connector_name: str = "", limit: int = 50) -> List[SyncLogEntry]:
    conn = _connect()
    query = (
        "SELECT id, company_id, product_id, connector_name, field_name, old_value, new_value, "
        "direction, source, created_at FROM sync_logs WHERE company_id = ?"
    )
    params: list = [company_id]
    if product_id:
        query += " AND product_id = ?"
        params.append(product_id)
    if connector_name:
        query += " AND connector_name = ?"
        params.append(connector_name)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [
        SyncLogEntry(
            id=r[0], company_id=r[1], product_id=r[2], connector_name=r[3], field_name=r[4],
            old_value=r[5] or "", new_value=r[6] or "", direction=r[7] or "", source=r[8] or "",
            created_at=r[9] or 0.0,
        )
        for r in rows
    ]
