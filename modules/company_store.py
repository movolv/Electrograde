"""PostgreSQL-backed company (tenant) persistence — the root of the multi-tenant
model. Every other tenant-scoped table (products, users, manifest batches,
repair events, marketplace listings) points at a row here via company_id.

Shares the same PostgreSQL database as every other modules/*_store.py
(modules/db.py, connection string via ELECTROGRADER_DATABASE_URL) but in
its own table, following the identical _connect()/CREATE TABLE IF NOT
EXISTS/CREATE INDEX IF NOT EXISTS
pattern used throughout modules/*_store.py — no ORM, no FK constraints (this
project enforces tenant scoping at the application layer everywhere, not in
the schema; see scripts/verify_tenant_isolation.py).
"""
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from modules import db

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"

DEFAULT_COMPANY_ID = "default"  # matches the SQLite column default already
                                 # on every pre-existing products/manifest_
                                 # batches row, so migrating existing data
                                 # means inserting one row here — nothing
                                 # else needs to change.


@dataclass
class Company:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    plan: str = "free"
    status: str = STATUS_ACTIVE
    user_limit: int = 5
    product_limit: int = 500
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Company":
        known = {k: v for k, v in d.items() if k in Company.__dataclass_fields__}
        return Company(**known)


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY,
            created_at DOUBLE PRECISION,
            data TEXT
        )
        """
    )
    conn.commit()
    return conn


def create_company(
    name: str,
    plan: str = "free",
    status: str = STATUS_ACTIVE,
    user_limit: int = 5,
    product_limit: int = 500,
    company_id: Optional[str] = None,
) -> Company:
    """`company_id` is normally left auto-generated; only the migration
    script passes an explicit one (DEFAULT_COMPANY_ID), to line up with
    data already tagged company_id='default' before this table existed."""
    company = Company(
        id=company_id or uuid.uuid4().hex[:12],
        name=name, plan=plan, status=status,
        user_limit=user_limit, product_limit=product_limit,
    )
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO companies (id, created_at, data) VALUES (?, ?, ?)",
            (company.id, company.created_at, json.dumps(company.to_dict())),
        )
    conn.close()
    return company


def get_company(company_id: str) -> Optional[Company]:
    conn = _connect()
    row = conn.execute(
        "SELECT data FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return Company.from_dict(json.loads(row[0]))


def list_companies() -> List[Company]:
    conn = _connect()
    rows = conn.execute("SELECT data FROM companies ORDER BY created_at ASC").fetchall()
    conn.close()
    return [Company.from_dict(json.loads(r[0])) for r in rows]


def update_company(company: Company) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE companies SET data = ? WHERE id = ?",
            (json.dumps(company.to_dict()), company.id),
        )
    conn.close()
