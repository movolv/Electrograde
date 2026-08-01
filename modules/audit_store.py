"""Minimal audit log — a stable foundation for later, not a full audit
system. Records only the handful of events called out for Phase 1 (LOGIN,
LOGOUT, CREATE_USER, DEACTIVATE_USER, DELETE_PRODUCT, DELETE_MANIFEST); no
UI reads this table yet. Same _connect()/CREATE TABLE pattern as every other
modules/*_store.py, sharing modules/inventory_store.py's DB_PATH.
"""
import sqlite3
import time
import uuid

from modules.inventory_store import DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id TEXT NOT NULL DEFAULT '',
            created_at REAL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_company ON audit_log(company_id)")
    return conn


def log_audit(company_id: str, user_id: str, action: str, entity: str, entity_id: str = "") -> None:
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO audit_log (id, company_id, user_id, action, entity, entity_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], company_id, user_id, action, entity, entity_id, time.time()),
        )
    conn.close()
