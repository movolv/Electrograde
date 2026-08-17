"""PostgreSQL-backed persistence for users and sessions. Same _connect()/
CREATE TABLE pattern as every other modules/*_store.py, sharing
modules/db.py's shared connection pool. Pure data access — password hashing,
login, and session-token semantics live in modules/auth.py, which is the
only module that should import this one directly for auth purposes.

Email is scoped per company, not globally unique: the same address can hold
independent accounts in different companies (see the UNIQUE(company_id,
email) index below), since two unrelated businesses could both have a
"john@example.com" with no connection to each other. Login therefore can't
look a user up by email alone without knowing which company first — see
get_users_by_email() and modules/auth.py's verify_login().
"""
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from modules import db


@dataclass
class User:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    name: str = ""
    email: str = ""  # always stored lower-cased by modules/auth.py
    password_hash: str = ""
    role: str = "employee"  # "admin" | "employee" | "reviewer"
    active: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = 0.0
    last_login_at: float = 0.0  # stamped by auth.verify_login() on success
    language: str = "en"  # UI language ("en" | "lv") — see modules/i18n.py
    failed_login_attempts: int = 0  # reset to 0 on any successful login — see modules/auth.py
    locked_until: float = 0.0  # 0.0 = not locked; a future unix timestamp = locked until then

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "User":
        known = {k: v for k, v in d.items() if k in User.__dataclass_fields__}
        return User(**known)


@dataclass
class SessionRow:
    token: str
    user_id: str
    created_at: float
    expires_at: float
    last_seen_at: float


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            email TEXT NOT NULL,
            created_at DOUBLE PRECISION,
            data TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_company_email ON users(company_id, email)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at DOUBLE PRECISION,
            expires_at DOUBLE PRECISION NOT NULL,
            last_seen_at DOUBLE PRECISION
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
    conn.commit()
    return conn


def create_user(user: User) -> None:
    """Raises psycopg.errors.UniqueViolation on a duplicate (company_id, email) —
    modules/auth.py.register_user() is expected to check first and raise a
    friendlier ValueError, but the DB-level constraint is the real backstop."""
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO users (id, company_id, email, created_at, data) VALUES (?, ?, ?, ?, ?)",
            (user.id, user.company_id, user.email, user.created_at, json.dumps(user.to_dict())),
        )
    conn.close()


def get_user_by_email(company_id: str, email: str) -> Optional[User]:
    """Scoped lookup — for checking "does this email already exist *within
    this company*" (registration) once the company is already known."""
    conn = _connect()
    row = conn.execute(
        "SELECT data FROM users WHERE company_id = ? AND email = ?", (company_id, email)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return User.from_dict(json.loads(row[0]))


def get_users_by_email(email: str) -> List[User]:
    """Unscoped — every account across every company with this email.
    Login only has email+password, not a company, so it must consider
    every matching account (see modules/auth.py.verify_login())."""
    conn = _connect()
    rows = conn.execute("SELECT data FROM users WHERE email = ?", (email,)).fetchall()
    conn.close()
    return [User.from_dict(json.loads(r[0])) for r in rows]


def get_user_by_id(user_id: str) -> Optional[User]:
    conn = _connect()
    row = conn.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return User.from_dict(json.loads(row[0]))


def list_users_for_company(company_id: str) -> List[User]:
    conn = _connect()
    rows = conn.execute(
        "SELECT data FROM users WHERE company_id = ? ORDER BY created_at ASC", (company_id,)
    ).fetchall()
    conn.close()
    return [User.from_dict(json.loads(r[0])) for r in rows]


def count_active_users_for_company(company_id: str) -> int:
    # 'active' isn't a flat column (it lives in the JSON blob like every
    # other User field except id/company_id/email) — filter in Python
    # rather than pretending SQLite can see inside the blob.
    return sum(1 for u in list_users_for_company(company_id) if u.active)


def update_user(user: User) -> None:
    """Scoped by (id, company_id) as defense-in-depth, even though every
    current caller already only ever operates on a User object it obtained
    from a company-scoped read (get_user_by_email/list_users_for_company)
    or the caller's own session — this is the one function that can flip
    `role`/`active`, so it gets the same belt-and-suspenders treatment
    every other store module's mutations already have."""
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE users SET email = ?, data = ? WHERE id = ? AND company_id = ?",
            (user.email, json.dumps(user.to_dict()), user.id, user.company_id),
        )
    conn.close()


def _hash_token(token: str) -> str:
    """Session tokens are secrets.token_urlsafe(32) — 256 bits of real
    entropy — so a fast, unsalted SHA-256 is fine here (unlike a password,
    there's no need for bcrypt-style deliberate slowness against a
    low-entropy secret). Only the hash is ever persisted; the raw token
    exists only in the browser cookie and this process's memory for the
    duration of one request."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session_row(token: str, user_id: str, expires_at: float) -> None:
    now = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_hash_token(token), user_id, now, expires_at, now),
        )
    conn.close()


def get_session(token: str) -> Optional[SessionRow]:
    conn = _connect()
    row = conn.execute(
        "SELECT token, user_id, created_at, expires_at, last_seen_at FROM sessions WHERE token = ?",
        (_hash_token(token),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return SessionRow(*row)


def delete_session(token: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (_hash_token(token),))
    conn.close()


def delete_sessions_for_user(user_id: str) -> int:
    """Real, immediate revocation — not just the passive protection
    validate_session() already provides by re-checking `active` on every
    call. Called when an admin deactivates a user, so the session row
    itself is gone, not just functionally inert until its 14-day TTL."""
    conn = _connect()
    with conn:
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        deleted = cur.rowcount
    conn.close()
    return deleted


def delete_expired_sessions() -> int:
    conn = _connect()
    with conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        deleted = cur.rowcount
    conn.close()
    return deleted
