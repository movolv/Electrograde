"""Persistence for sync/models.SyncRecord — the CURRENT sync state of one
(company, product, connector, direction) tuple, upserted on every attempt.

Deliberately distinct from modules/integration_store.py's
`integration_sync_log` (an append-only HISTORY of attempts across a whole
integration, already built in Phase 1/2) — this table is per-PRODUCT
current state ("is product X currently synced with BaseLinker, in which
direction, and what happened last time"), not a log. The two coexist by
design, the same way integration_sync_log and sync_jobs already coexist
(state machine vs. history) elsewhere in this codebase.

Shares the same PostgreSQL database as every other modules/*_store.py (modules/db.py).
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules import db
from sync.status import DIRECTION_EXPORT, STATUS_PENDING


@dataclass
class SyncRecordRow:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    product_id: str = ""
    connector_name: str = ""
    direction: str = DIRECTION_EXPORT
    sync_status: str = STATUS_PENDING
    last_sync_time: float = 0.0
    error_message: str = ""
    external_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_records (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            connector_name TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'export',
            sync_status TEXT NOT NULL DEFAULT 'pending',
            last_sync_time DOUBLE PRECISION NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            external_id TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_records_unique "
        "ON sync_records(company_id, product_id, connector_name, direction)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_records_company ON sync_records(company_id, product_id)")
    conn.commit()
    return conn


_SELECT_COLS = (
    "id, company_id, product_id, connector_name, direction, sync_status, "
    "last_sync_time, error_message, external_id, created_at, updated_at"
)


def _row_to_record(r: tuple) -> SyncRecordRow:
    return SyncRecordRow(
        id=r[0], company_id=r[1], product_id=r[2], connector_name=r[3], direction=r[4],
        sync_status=r[5], last_sync_time=r[6] or 0.0, error_message=r[7] or "", external_id=r[8] or "",
        created_at=r[9] or 0.0, updated_at=r[10] or 0.0,
    )


def get_record(company_id: str, product_id: str, connector_name: str, direction: str) -> Optional[SyncRecordRow]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM sync_records "
        "WHERE company_id = ? AND product_id = ? AND connector_name = ? AND direction = ?",
        (company_id, product_id, connector_name, direction),
    ).fetchone()
    conn.close()
    return _row_to_record(row) if row else None


def list_records(company_id: str, product_id: str = "") -> List[SyncRecordRow]:
    conn = _connect()
    if product_id:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM sync_records WHERE company_id = ? AND product_id = ? "
            "ORDER BY updated_at DESC",
            (company_id, product_id),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM sync_records WHERE company_id = ? ORDER BY updated_at DESC",
            (company_id,),
        ).fetchall()
    conn.close()
    return [_row_to_record(r) for r in rows]


def upsert_record(record) -> None:
    """Accepts a sync.models.SyncRecord (or this module's own
    SyncRecordRow — same field shape) and upserts by
    (company_id, product_id, connector_name, direction)."""
    assert record.company_id, "SyncRecord.company_id must be set before saving."
    assert record.product_id, "SyncRecord.product_id must be set before saving."
    assert record.connector_name, "SyncRecord.connector_name must be set before saving."

    now = time.time()
    conn = _connect()
    existing = conn.execute(
        "SELECT id, created_at FROM sync_records "
        "WHERE company_id = ? AND product_id = ? AND connector_name = ? AND direction = ?",
        (record.company_id, record.product_id, record.connector_name, record.direction),
    ).fetchone()
    record_id = existing[0] if existing else record.id
    created_at = existing[1] if existing else now

    with conn:
        conn.execute(
            """INSERT INTO sync_records
               (id, company_id, product_id, connector_name, direction, sync_status,
                last_sync_time, error_message, external_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   company_id = EXCLUDED.company_id, product_id = EXCLUDED.product_id,
                   connector_name = EXCLUDED.connector_name, direction = EXCLUDED.direction,
                   sync_status = EXCLUDED.sync_status, last_sync_time = EXCLUDED.last_sync_time,
                   error_message = EXCLUDED.error_message, external_id = EXCLUDED.external_id,
                   created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at""",
            (
                record_id, record.company_id, record.product_id, record.connector_name, record.direction,
                record.sync_status, record.last_sync_time, record.error_message, record.external_id,
                created_at, now,
            ),
        )
    conn.close()
