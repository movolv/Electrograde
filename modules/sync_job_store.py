"""The sync job queue — a universal ACTION model, not just "export."
integrations/scheduler.py (a Streamlit-independent module) is the only
thing that drives this state machine; this module is pure persistence.

Phase 1 only ever creates JOB_TYPE_PRODUCT_EXPORT jobs (the only action
integration_sync_rules' `frequency` describes), but the schema already
supports the other job types listed below so a future phase can start
enqueuing them without a migration.

State machine: pending -> running -> success | error | retrying (with
attempts/max_attempts/next_attempt_at backoff) | skipped_not_implemented
(the honest outcome when a job comes due for an integration_type with no
executor registered in integrations/scheduler.py's EXECUTORS dict yet).

A partial unique index enforces "at most one active (pending/running/
retrying) job per (company_id, integration_type, job_type)" at the DB
layer — cheap today (one process, one worker thread), and exactly the
guard that matters the day a second worker/process exists.

Shares the same PostgreSQL database as every other modules/*_store.py (modules/db.py).
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import psycopg

from modules import db

JOB_TYPE_PRODUCT_EXPORT = "product_export"
JOB_TYPE_IMAGE_UPLOAD = "image_upload"
JOB_TYPE_PRICE_UPDATE = "price_update"
JOB_TYPE_INVENTORY_SYNC = "inventory_sync"
JOB_TYPE_ORDER_IMPORT = "order_import"
JOB_TYPE_LISTING_UPDATE = "listing_update"
JOB_TYPE_LISTING_END = "listing_end"

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
TRIGGER_EVENT = "event"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_RETRYING = "retrying"
STATUS_SKIPPED = "skipped_not_implemented"

_ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_RETRYING)


@dataclass
class SyncJob:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    integration_type: str = ""
    job_type: str = JOB_TYPE_PRODUCT_EXPORT
    trigger: str = TRIGGER_SCHEDULED
    direction: str = "push"
    status: str = STATUS_PENDING
    attempts: int = 0
    max_attempts: int = 3
    next_attempt_at: float = 0.0
    error_message: str = ""
    payload: dict = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_jobs (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            integration_type TEXT NOT NULL,
            job_type TEXT NOT NULL DEFAULT 'product_export',
            trigger TEXT NOT NULL DEFAULT 'scheduled',
            direction TEXT NOT NULL DEFAULT 'push',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION,
            started_at DOUBLE PRECISION,
            finished_at DOUBLE PRECISION
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_jobs_claimable ON sync_jobs(status, next_attempt_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_jobs_company ON sync_jobs(company_id, integration_type, created_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_jobs_one_active "
        "ON sync_jobs(company_id, integration_type, job_type) "
        "WHERE status IN ('pending', 'running', 'retrying')"
    )
    conn.commit()
    return conn


_SELECT_COLS = (
    "id, company_id, integration_type, job_type, trigger, direction, status, attempts, "
    "max_attempts, next_attempt_at, error_message, payload, created_at, updated_at, "
    "started_at, finished_at"
)


def _row_to_job(r: tuple) -> SyncJob:
    return SyncJob(
        id=r[0], company_id=r[1], integration_type=r[2], job_type=r[3], trigger=r[4],
        direction=r[5], status=r[6], attempts=r[7], max_attempts=r[8], next_attempt_at=r[9] or 0.0,
        error_message=r[10] or "", payload=json.loads(r[11]) if r[11] else {},
        created_at=r[12] or 0.0, updated_at=r[13] or 0.0, started_at=r[14] or 0.0, finished_at=r[15] or 0.0,
    )


def has_active_job(company_id: str, integration_type: str, job_type: str) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM sync_jobs WHERE company_id = ? AND integration_type = ? AND job_type = ? "
        f"AND status IN ({','.join('?' * len(_ACTIVE_STATUSES))}) LIMIT 1",
        (company_id, integration_type, job_type, *_ACTIVE_STATUSES),
    ).fetchone()
    conn.close()
    return row is not None


def create_job(
    company_id: str, integration_type: str, job_type: str = JOB_TYPE_PRODUCT_EXPORT,
    trigger: str = TRIGGER_SCHEDULED, direction: str = "push", max_attempts: int = 3,
    payload: Optional[dict] = None,
) -> Optional[SyncJob]:
    """Returns None (no-op) if an active job already exists for this
    (company, integration_type, job_type) — the partial unique index is the
    real guarantee; this check just avoids a noisy IntegrityError in the
    common case."""
    if has_active_job(company_id, integration_type, job_type):
        return None
    now = time.time()
    job = SyncJob(
        company_id=company_id, integration_type=integration_type, job_type=job_type,
        trigger=trigger, direction=direction, status=STATUS_PENDING, max_attempts=max_attempts,
        next_attempt_at=now, payload=payload or {}, created_at=now, updated_at=now,
    )
    conn = _connect()
    try:
        with conn:
            conn.execute(
                """INSERT INTO sync_jobs
                   (id, company_id, integration_type, job_type, trigger, direction, status,
                    attempts, max_attempts, next_attempt_at, error_message, payload,
                    created_at, updated_at, started_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.id, job.company_id, job.integration_type, job.job_type, job.trigger,
                    job.direction, job.status, job.attempts, job.max_attempts, job.next_attempt_at,
                    job.error_message, json.dumps(job.payload), job.created_at, job.updated_at,
                    job.started_at or None, job.finished_at or None,
                ),
            )
    except psycopg.IntegrityError:
        conn.close()
        return None
    conn.close()
    return job


def claim_due_jobs(limit: int = 5) -> List[SyncJob]:
    """Claims up to `limit` jobs that are due (pending/retrying with
    next_attempt_at <= now), atomically flipping each to 'running' via a
    conditional UPDATE + rowcount check — the standard claim pattern, exact
    even in this single-worker app, and the only thing that stays correct
    if a second worker/process is ever added later."""
    now = time.time()
    conn = _connect()
    candidates = conn.execute(
        "SELECT id FROM sync_jobs WHERE status IN (?, ?) AND next_attempt_at <= ? "
        "ORDER BY next_attempt_at ASC LIMIT ?",
        (STATUS_PENDING, STATUS_RETRYING, now, limit),
    ).fetchall()

    claimed: List[SyncJob] = []
    for (job_id,) in candidates:
        with conn:
            cur = conn.execute(
                "UPDATE sync_jobs SET status = ?, started_at = ?, updated_at = ? "
                "WHERE id = ? AND status IN (?, ?) AND next_attempt_at <= ?",
                (STATUS_RUNNING, now, now, job_id, STATUS_PENDING, STATUS_RETRYING, now),
            )
            if cur.rowcount == 1:
                row = conn.execute(f"SELECT {_SELECT_COLS} FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()
                if row:
                    claimed.append(_row_to_job(row))
    conn.close()
    return claimed


def mark_success(job_id: str) -> None:
    now = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE sync_jobs SET status = ?, updated_at = ?, finished_at = ? WHERE id = ?",
            (STATUS_SUCCESS, now, now, job_id),
        )
    conn.close()


def mark_skipped(job_id: str, message: str = "") -> None:
    now = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE sync_jobs SET status = ?, error_message = ?, updated_at = ?, finished_at = ? WHERE id = ?",
            (STATUS_SKIPPED, message, now, now, job_id),
        )
    conn.close()


def mark_failure(job_id: str, error_message: str, attempts: int, max_attempts: int) -> str:
    """Increments the retry/backoff state. Returns the resulting status
    ('retrying' or 'error') so the caller can decide what to log."""
    now = time.time()
    new_attempts = attempts + 1
    if new_attempts < max_attempts:
        backoff_seconds = min(900, 60 * (2 ** (new_attempts - 1)))  # 60s, 120s, 240s... capped at 15min
        status = STATUS_RETRYING
        next_attempt_at = now + backoff_seconds
    else:
        status = STATUS_ERROR
        next_attempt_at = now

    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE sync_jobs SET status = ?, attempts = ?, next_attempt_at = ?, error_message = ?, "
            "updated_at = ?, finished_at = ? WHERE id = ?",
            (
                status, new_attempts, next_attempt_at, error_message, now,
                now if status == STATUS_ERROR else None, job_id,
            ),
        )
    conn.close()
    return status


def list_jobs(company_id: str, integration_type: str = "", limit: int = 20) -> List[SyncJob]:
    conn = _connect()
    if integration_type:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM sync_jobs WHERE company_id = ? AND integration_type = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (company_id, integration_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM sync_jobs WHERE company_id = ? ORDER BY created_at DESC LIMIT ?",
            (company_id, limit),
        ).fetchall()
    conn.close()
    return [_row_to_job(r) for r in rows]
