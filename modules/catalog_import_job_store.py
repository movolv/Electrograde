"""Progress tracking for a bulk catalog import (sync/catalog_import.py) run
in the background (app.py's _import_executor() thread pool). One row per
(company_id, connector_name) — a new import overwrites the previous run's
final state, matching modules/sync_rules_store.py's one-row-per-pair
pattern, since only one import job per connector makes sense at a time.

Thread-safe by construction: every function opens and closes its own
SQLite connection (the same convention as every other modules/*_store.py),
so the background worker thread writing progress and the main Streamlit
thread reading it never share a connection.

Shares the same SQLite file as modules/inventory_store.py.
"""
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import List, Optional

from modules.inventory_store import DB_PATH

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

_MAX_STORED_ERRORS = 10


@dataclass
class CatalogImportJob:
    company_id: str = ""
    connector_name: str = ""
    status: str = STATUS_RUNNING
    total: int = 0
    imported: int = 0
    skipped: int = 0
    error_count: int = 0
    errors: List[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_import_jobs (
            company_id TEXT NOT NULL,
            connector_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            total INTEGER NOT NULL DEFAULT 0,
            imported INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            errors TEXT NOT NULL DEFAULT '[]',
            started_at REAL,
            finished_at REAL,
            PRIMARY KEY (company_id, connector_name)
        )
        """
    )
    return conn


_SELECT_COLS = (
    "company_id, connector_name, status, total, imported, skipped, error_count, errors, "
    "started_at, finished_at"
)


def _row_to_job(r: tuple) -> CatalogImportJob:
    return CatalogImportJob(
        company_id=r[0], connector_name=r[1], status=r[2], total=r[3] or 0, imported=r[4] or 0,
        skipped=r[5] or 0, error_count=r[6] or 0, errors=json.loads(r[7]) if r[7] else [],
        started_at=r[8] or 0.0, finished_at=r[9] or 0.0,
    )


def get_job(company_id: str, connector_name: str) -> Optional[CatalogImportJob]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM catalog_import_jobs WHERE company_id = ? AND connector_name = ?",
        (company_id, connector_name),
    ).fetchone()
    conn.close()
    return _row_to_job(row) if row else None


def start_job(company_id: str, connector_name: str, total: int) -> None:
    now = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO catalog_import_jobs
               (company_id, connector_name, status, total, imported, skipped, error_count, errors,
                started_at, finished_at)
               VALUES (?, ?, ?, ?, 0, 0, 0, '[]', ?, NULL)""",
            (company_id, connector_name, STATUS_RUNNING, total, now),
        )
    conn.close()


def update_progress(company_id: str, connector_name: str, imported: int, skipped: int, error_count: int) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            """UPDATE catalog_import_jobs SET imported = ?, skipped = ?, error_count = ?
               WHERE company_id = ? AND connector_name = ?""",
            (imported, skipped, error_count, company_id, connector_name),
        )
    conn.close()


def finish_job(company_id: str, connector_name: str, success: bool, errors: Optional[List[str]] = None) -> None:
    stored_errors = (errors or [])[:_MAX_STORED_ERRORS]
    conn = _connect()
    with conn:
        conn.execute(
            """UPDATE catalog_import_jobs SET status = ?, errors = ?, error_count = ?, finished_at = ?
               WHERE company_id = ? AND connector_name = ?""",
            (
                STATUS_SUCCESS if success else STATUS_FAILED, json.dumps(stored_errors), len(errors or []),
                time.time(), company_id, connector_name,
            ),
        )
    conn.close()
