"""The Sync Queue — a durable middle layer between a detected local field
change and its actual BaseLinker API push. Retry/backoff mirrors
modules/sync_job_store.py's already-proven formula exactly (same claim-
then-process pattern, same 60s/120s/240s...capped-at-15min backoff) — that
module itself is untouched; this is a parallel table for field-level Push
work, not a job-type addition to it (sync_jobs is about whole-product
product_export jobs; sync_queue is about individual field changes).

Shares the same PostgreSQL database as every other modules/*_store.py (modules/db.py).
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules import db

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

_ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PROCESSING)


@dataclass
class SyncQueueItem:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    product_id: str = ""
    connector_name: str = ""
    field_name: str = ""
    old_value: str = ""
    new_value: str = ""
    direction: str = "export"
    status: str = STATUS_PENDING
    attempts: int = 0
    max_attempts: int = 3
    next_attempt_at: float = 0.0
    error_message: str = ""
    created_at: float = 0.0
    processed_at: float = 0.0


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_queue (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            connector_name TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT NOT NULL DEFAULT '',
            new_value TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT 'export',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION,
            processed_at DOUBLE PRECISION
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_queue_claimable ON sync_queue(status, next_attempt_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_queue_company ON sync_queue(company_id, product_id)")

    # Migrate older DBs: "grade" field was renamed to "product_condition"
    # (integrations/field_registry.py) — keep queued rows pointed at the
    # field's current name.
    conn.execute("UPDATE sync_queue SET field_name = 'product_condition' WHERE field_name = 'grade'")
    conn.commit()
    return conn


_SELECT_COLS = (
    "id, company_id, product_id, connector_name, field_name, old_value, new_value, direction, "
    "status, attempts, max_attempts, next_attempt_at, error_message, created_at, processed_at"
)


def _row_to_item(r: tuple) -> SyncQueueItem:
    return SyncQueueItem(
        id=r[0], company_id=r[1], product_id=r[2], connector_name=r[3], field_name=r[4],
        old_value=r[5] or "", new_value=r[6] or "", direction=r[7], status=r[8],
        attempts=r[9], max_attempts=r[10], next_attempt_at=r[11] or 0.0, error_message=r[12] or "",
        created_at=r[13] or 0.0, processed_at=r[14] or 0.0,
    )


def enqueue(
    company_id: str, product_id: str, connector_name: str, field_name: str,
    old_value: str = "", new_value: str = "", direction: str = "export", max_attempts: int = 3,
) -> SyncQueueItem:
    now = time.time()
    item = SyncQueueItem(
        company_id=company_id, product_id=product_id, connector_name=connector_name, field_name=field_name,
        old_value=str(old_value), new_value=str(new_value), direction=direction, status=STATUS_PENDING,
        max_attempts=max_attempts, next_attempt_at=now, created_at=now,
    )
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO sync_queue
               (id, company_id, product_id, connector_name, field_name, old_value, new_value, direction,
                status, attempts, max_attempts, next_attempt_at, error_message, created_at, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.id, item.company_id, item.product_id, item.connector_name, item.field_name,
                item.old_value, item.new_value, item.direction, item.status, item.attempts,
                item.max_attempts, item.next_attempt_at, item.error_message, item.created_at, None,
            ),
        )
    conn.close()
    return item


def claim_pending(limit: int = 10) -> List[SyncQueueItem]:
    """Same claim-then-process pattern as sync_job_store.claim_due_jobs():
    atomically flips each due row to 'processing' via a conditional UPDATE
    + rowcount check."""
    now = time.time()
    conn = _connect()
    candidates = conn.execute(
        "SELECT id FROM sync_queue WHERE status = ? AND next_attempt_at <= ? ORDER BY next_attempt_at ASC LIMIT ?",
        (STATUS_PENDING, now, limit),
    ).fetchall()

    claimed: List[SyncQueueItem] = []
    for (item_id,) in candidates:
        with conn:
            cur = conn.execute(
                "UPDATE sync_queue SET status = ? WHERE id = ? AND status = ? AND next_attempt_at <= ?",
                (STATUS_PROCESSING, item_id, STATUS_PENDING, now),
            )
            if cur.rowcount == 1:
                row = conn.execute(f"SELECT {_SELECT_COLS} FROM sync_queue WHERE id = ?", (item_id,)).fetchone()
                if row:
                    claimed.append(_row_to_item(row))
    conn.close()
    return claimed


def mark_success(item_id: str) -> None:
    now = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE sync_queue SET status = ?, processed_at = ? WHERE id = ?",
            (STATUS_SUCCESS, now, item_id),
        )
    conn.close()


def mark_failed(item_id: str, error_message: str, attempts: int, max_attempts: int) -> str:
    """Same backoff formula as sync_job_store.mark_failure(): 60s, 120s,
    240s... capped at 15min, then terminal 'failed' once max_attempts is
    exhausted. Returns the resulting status."""
    now = time.time()
    new_attempts = attempts + 1
    if new_attempts < max_attempts:
        backoff_seconds = min(900, 60 * (2 ** (new_attempts - 1)))
        status = STATUS_PENDING
        next_attempt_at = now + backoff_seconds
    else:
        status = STATUS_FAILED
        next_attempt_at = now

    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE sync_queue SET status = ?, attempts = ?, next_attempt_at = ?, error_message = ?, "
            "processed_at = ? WHERE id = ?",
            (status, new_attempts, next_attempt_at, error_message, now if status == STATUS_FAILED else None, item_id),
        )
    conn.close()
    return status


def list_queue(company_id: str, product_id: str = "", limit: int = 50) -> List[SyncQueueItem]:
    conn = _connect()
    if product_id:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM sync_queue WHERE company_id = ? AND product_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (company_id, product_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM sync_queue WHERE company_id = ? ORDER BY created_at DESC LIMIT ?",
            (company_id, limit),
        ).fetchall()
    conn.close()
    return [_row_to_item(r) for r in rows]
