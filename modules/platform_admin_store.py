"""Platform-level Super Admin persistence — deliberately NOT company-scoped
(no company_id column/filter anywhere in this module), unlike every other
modules/*_store.py table. Being a Super Admin is a fact about a User that
sits ABOVE the tenant/company layer entirely: it marks specific users
(via user_id) as platform administrators, independent of whatever
company-scoped role (admin/employee/reviewer) that same user also holds in
their own company. See modules/auth.py's is_super_admin()/
require_super_admin() for the actual authorization check, and
scripts/superadmin_cli.py for the only way to create the FIRST one (this
module is pure data access, no bootstrap/recovery policy lives here).

Shares the same PostgreSQL database as every other modules/*_store.py
(modules/db.py), same _connect()/CREATE TABLE IF NOT EXISTS pattern.
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules import db


@dataclass
class PlatformAdmin:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    user_id: str = ""
    is_active: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_admins (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL UNIQUE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION
        )
        """
    )
    conn.commit()
    return conn


def create(user_id: str) -> PlatformAdmin:
    """Raises on a duplicate user_id (UNIQUE constraint) — callers (the CLI
    bootstrap, the web "Grant Super Admin" action) are expected to check
    get_by_user_id() first if they want an idempotent create-or-reuse."""
    admin = PlatformAdmin(user_id=user_id)
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO platform_admins (id, user_id, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (admin.id, admin.user_id, admin.is_active, admin.created_at, admin.updated_at),
        )
    conn.close()
    return admin


def get_by_user_id(user_id: str) -> Optional[PlatformAdmin]:
    conn = _connect()
    row = conn.execute(
        "SELECT id, user_id, is_active, created_at, updated_at FROM platform_admins WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return PlatformAdmin(id=row[0], user_id=row[1], is_active=bool(row[2]), created_at=row[3] or 0.0, updated_at=row[4] or 0.0)


def list_all() -> List[PlatformAdmin]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, user_id, is_active, created_at, updated_at FROM platform_admins ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return [
        PlatformAdmin(id=r[0], user_id=r[1], is_active=bool(r[2]), created_at=r[3] or 0.0, updated_at=r[4] or 0.0)
        for r in rows
    ]


def list_active() -> List[PlatformAdmin]:
    return [a for a in list_all() if a.is_active]


def count_active() -> int:
    conn = _connect()
    row = conn.execute("SELECT COUNT(*) FROM platform_admins WHERE is_active = ?", (True,)).fetchone()
    conn.close()
    return int(row[0] or 0)


def set_active(admin_id: str, is_active: bool) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE platform_admins SET is_active = ?, updated_at = ? WHERE id = ?",
            (is_active, time.time(), admin_id),
        )
    conn.close()
