"""Universal per-company integration config — one row per (company,
integration_type) pair, replacing the old app-wide BASELINKER_* env vars
with something that actually fits the multi-tenant model: each company
connects its own BaseLinker/DeepL/etc. account from Settings ->
Integrations, and integrations/manager.py resolves the right connector +
credentials for whichever company_id is asking.

Two tables:
  - company_integrations: current connection state + encrypted credentials.
    `credentials` is Fernet ciphertext (modules/crypto.py) — this module is
    the ONLY place that ever calls encrypt_dict/decrypt_dict, so no caller
    can forget to encrypt a secret before it hits disk. `settings` is plain
    JSON (non-secret config like inventory_id/category_id).
  - integration_sync_log: append-only record of actual sync attempts
    (export/test/etc.), separate from the general-purpose audit_log
    (modules/audit_store.py), which only gets CONNECT_INTEGRATION /
    DISCONNECT_INTEGRATION admin-action entries. This table answers "why is
    this integration in an error state" with real per-attempt detail;
    audit_log answers "who changed the connection and when."

CompanyIntegration.credentials is declared repr=False so an accidental
`print(record)` / `st.write(record)` omits it — the one blanket guard
against leaking a decrypted token. It does NOT protect against code that
explicitly reads `record.credentials["token"]` into a log line; see
scripts/verify_tenant_isolation.py for the raw-column proof that
encryption-at-rest actually happens regardless of that gap.

Shares the same SQLite file as modules/inventory_store.py.
"""
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules import crypto
from modules.inventory_store import DB_PATH

CATEGORY_MARKETPLACE = "marketplace"
CATEGORY_SERVICE = "service"

STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
STATUS_ERROR = "error"

SYNC_STATUS_SUCCESS = "success"
SYNC_STATUS_ERROR = "error"


@dataclass
class CompanyIntegration:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    integration_type: str = ""  # "baselinker" | "deepl" | ...
    integration_category: str = ""  # CATEGORY_MARKETPLACE | CATEGORY_SERVICE
    status: str = STATUS_DISCONNECTED
    credentials: dict = field(default_factory=dict, repr=False)
    settings: dict = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    last_sync_at: float = 0.0


@dataclass
class SyncLogEntry:
    id: str = ""
    company_id: str = ""
    integration_type: str = ""
    action: str = ""  # "export" | "test_connection" | ...
    product_id: str = ""
    external_id: str = ""
    status: str = ""  # SYNC_STATUS_SUCCESS | SYNC_STATUS_ERROR
    error_message: str = ""
    created_at: float = 0.0


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_integrations (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            integration_type TEXT NOT NULL,
            integration_category TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'disconnected',
            credentials TEXT NOT NULL DEFAULT '',
            settings TEXT NOT NULL DEFAULT '{}',
            created_at REAL,
            updated_at REAL,
            last_sync_at REAL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_company_integrations_unique "
        "ON company_integrations(company_id, integration_type)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_sync_log (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            integration_type TEXT NOT NULL,
            action TEXT NOT NULL,
            product_id TEXT NOT NULL DEFAULT '',
            external_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            created_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_integration_sync_log_company "
        "ON integration_sync_log(company_id, integration_type)"
    )
    return conn


_SELECT_COLS = (
    "id, company_id, integration_type, integration_category, status, credentials, "
    "settings, created_at, updated_at, last_sync_at"
)


def _row_to_integration(r: tuple) -> CompanyIntegration:
    import json

    return CompanyIntegration(
        id=r[0], company_id=r[1], integration_type=r[2], integration_category=r[3] or "",
        status=r[4] or STATUS_DISCONNECTED,
        credentials=crypto.decrypt_dict(r[5]) if r[5] else {},
        settings=json.loads(r[6]) if r[6] else {},
        created_at=r[7] or 0.0, updated_at=r[8] or 0.0, last_sync_at=r[9] or 0.0,
    )


def get_integration(company_id: str, integration_type: str) -> Optional[CompanyIntegration]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM company_integrations WHERE company_id = ? AND integration_type = ?",
        (company_id, integration_type),
    ).fetchone()
    conn.close()
    return _row_to_integration(row) if row else None


def list_integrations(company_id: str) -> List[CompanyIntegration]:
    conn = _connect()
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM company_integrations WHERE company_id = ? ORDER BY integration_type ASC",
        (company_id,),
    ).fetchall()
    conn.close()
    return [_row_to_integration(r) for r in rows]


def upsert_integration(integration: CompanyIntegration) -> CompanyIntegration:
    """Encrypts `credentials` on the way in. Preserves `created_at` /
    `id` of an existing row for the same (company_id, integration_type) pair
    rather than minting a new id every save."""
    import json

    assert integration.company_id, "CompanyIntegration.company_id must be set before saving."
    assert integration.integration_type, "CompanyIntegration.integration_type must be set before saving."

    now = time.time()
    conn = _connect()
    existing = conn.execute(
        "SELECT id, created_at FROM company_integrations WHERE company_id = ? AND integration_type = ?",
        (integration.company_id, integration.integration_type),
    ).fetchone()
    if existing:
        integration.id, integration.created_at = existing[0], existing[1] or now
    else:
        integration.created_at = now
    integration.updated_at = now

    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO company_integrations
               (id, company_id, integration_type, integration_category, status, credentials,
                settings, created_at, updated_at, last_sync_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                integration.id, integration.company_id, integration.integration_type,
                integration.integration_category, integration.status,
                crypto.encrypt_dict(integration.credentials) if integration.credentials else "",
                json.dumps(integration.settings or {}),
                integration.created_at, integration.updated_at, integration.last_sync_at,
            ),
        )
    conn.close()
    return integration


def disconnect_integration(company_id: str, integration_type: str) -> None:
    """Clears credentials (nothing left to decrypt/leak) but keeps
    `settings` so reconnecting doesn't lose non-secret config the admin
    already filled in."""
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE company_integrations SET credentials = '', status = ?, updated_at = ? "
            "WHERE company_id = ? AND integration_type = ?",
            (STATUS_DISCONNECTED, time.time(), company_id, integration_type),
        )
    conn.close()


def touch_last_sync(company_id: str, integration_type: str) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE company_integrations SET last_sync_at = ? WHERE company_id = ? AND integration_type = ?",
            (time.time(), company_id, integration_type),
        )
    conn.close()


def record_sync(
    company_id: str, integration_type: str, action: str, product_id: str = "",
    external_id: str = "", status: str = SYNC_STATUS_SUCCESS, error_message: str = "",
) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO integration_sync_log
               (id, company_id, integration_type, action, product_id, external_id, status,
                error_message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex[:12], company_id, integration_type, action, product_id,
                external_id, status, error_message, time.time(),
            ),
        )
    conn.close()
    if status == SYNC_STATUS_SUCCESS:
        touch_last_sync(company_id, integration_type)


def list_sync_log(company_id: str, integration_type: str = "", limit: int = 50) -> List[SyncLogEntry]:
    conn = _connect()
    if integration_type:
        rows = conn.execute(
            "SELECT id, company_id, integration_type, action, product_id, external_id, status, "
            "error_message, created_at FROM integration_sync_log "
            "WHERE company_id = ? AND integration_type = ? ORDER BY created_at DESC LIMIT ?",
            (company_id, integration_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, company_id, integration_type, action, product_id, external_id, status, "
            "error_message, created_at FROM integration_sync_log "
            "WHERE company_id = ? ORDER BY created_at DESC LIMIT ?",
            (company_id, limit),
        ).fetchall()
    conn.close()
    return [
        SyncLogEntry(
            id=r[0], company_id=r[1], integration_type=r[2], action=r[3], product_id=r[4] or "",
            external_id=r[5] or "", status=r[6] or "", error_message=r[7] or "", created_at=r[8] or 0.0,
        )
        for r in rows
    ]
