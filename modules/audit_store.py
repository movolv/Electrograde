"""Minimal audit log — a stable foundation for later, not a full audit
system. Records the key lifecycle events across products, manifests,
BaseLinker exports, and users/sessions (see the ACTION_* constants used
throughout app.py/modules/auth.py); no UI reads this table yet. Same
_connect()/CREATE TABLE pattern as every other modules/*_store.py, sharing
modules/db.py's shared connection pool.

Every row is tenant-scoped via company_id — there is no cross-company
audit view, matching the rest of this codebase's isolation model.
"""
import time
import uuid

from modules import db


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION
        )
        """
    )

    # Migrate older DBs (Phase 1 shipped this table without `details`).
    existing_cols = db.table_columns(conn, "audit_log")
    if "details" not in existing_cols:
        conn.execute("ALTER TABLE audit_log ADD COLUMN details TEXT NOT NULL DEFAULT ''")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_company ON audit_log(company_id)")
    conn.commit()
    return conn


def log_audit(
    company_id: str, user_id: str, action: str, entity: str, entity_id: str = "", details: str = "",
) -> None:
    """`details`: a short human-readable note — e.g. which field changed, or
    a product/batch name — not a full diff. Keep it brief; this is meant to
    give a human enough context to understand the row at a glance, not to
    reconstruct exact before/after state."""
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO audit_log (id, company_id, user_id, action, entity, entity_id, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], company_id, user_id, action, entity, entity_id, details, time.time()),
        )
    conn.close()
