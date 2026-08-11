"""Shared PostgreSQL connection layer for every modules/*_store.py file —
replaces the previous one-SQLite-file-for-the-whole-app setup (which
serialized every write, across every company, behind a single-file lock)
with a real multi-writer database, while keeping every existing call site
in the 17 store modules unchanged.

Each store module still does exactly what it always did:
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO x (...) VALUES (?, ?)", (a, b))
    conn.close()

- db.connect() checks out a connection from a shared pool instead of
  opening a new one each time — the pool (not each call site) is what
  actually gives concurrent writers room to work without blocking each
  other, and is why this migration needed more than a connection-string
  swap.
- The wrapper's .execute()/.executemany() rewrite "?" placeholders to
  psycopg's "%s" before delegating, so none of the hundreds of existing
  parameterized queries needed to change.
- Its .close() returns the connection to the pool instead of really
  closing it, so every existing "conn.close()" call still does the right
  thing. `with conn:` and .commit()/.rollback() delegate straight to the
  underlying psycopg connection, which has the same commit-on-success/
  rollback-on-exception behavior as sqlite3.Connection's context manager
  — this preserves the couple of places (modules/sync_job_store.py,
  modules/sync_queue_store.py) that rely on a multi-statement block
  committing atomically as one transaction.
"""
import logging
import os
import re
from typing import Optional

from psycopg_pool import ConnectionPool

# Read-only call sites throughout this codebase call conn.execute(...)
# directly (no "with conn:") and then conn.close() — under sqlite3 that
# was a no-op; under psycopg it leaves an open (nothing-written) read
# transaction, which the pool silently rolls back on putconn(). Harmless,
# but psycopg_pool logs it at WARNING on every single one, which would
# spam the log constantly — quieted to ERROR so only real problems show.
logging.getLogger("psycopg.pool").setLevel(logging.ERROR)

# Deliberately NOT resolved here at import time: app.py imports this
# module (transitively, via modules/*_store.py) BEFORE it calls
# load_dotenv() — reading the env var eagerly at import would permanently
# bake in "" (nothing set yet), even once .env loads moments later. Left
# None here; _get_pool() resolves it lazily on first real use instead,
# by which point .env has loaded. Still a plain module attribute (not a
# function) so verify_*.py scripts can monkeypatch it directly, same as
# before.
DATABASE_URL: Optional[str] = None

_pool: Optional[ConnectionPool] = None

# SQLite's "?" positional placeholder -> psycopg's "%s". Safe to apply to
# every query unconditionally: nothing in this codebase's SQL contains a
# literal "?" outside a placeholder position.
_PLACEHOLDER_RE = re.compile(r"\?")


def _translate(sql: str) -> str:
    return _PLACEHOLDER_RE.sub("%s", sql)


class _CursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self):
        return self._cursor.rowcount


class _ConnectionWrapper:
    """Wraps a pooled psycopg connection so every existing store-module
    call site (conn.execute(...), with conn:, conn.commit(), conn.close())
    keeps working unchanged."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool
        self._conn = None  # set only after getconn() succeeds — see __del__
        self._conn = pool.getconn()

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        cur.execute(_translate(sql), params)
        return _CursorWrapper(cur)

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(_translate(sql), list(seq_of_params))
        return _CursorWrapper(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._conn is not None:
            conn, self._conn = self._conn, None
            self._pool.putconn(conn)

    def __del__(self):
        # Safety net: every existing store-module call site does
        # "conn = _connect(); ...; conn.close()" with no try/finally — a
        # pattern inherited from SQLite, where a leaked connection was
        # harmless (sqlite3.Connection.__del__ just closes the local file
        # handle, and SQLite has no fixed-size pool to exhaust). Under a
        # bounded Postgres pool, an uncaught exception between _connect()
        # and .close() would otherwise permanently strand one connection
        # out of the pool instead of just that one call failing — enough
        # of those over time (network hiccups, bad data, anything that
        # raises) and the whole pool exhausts, so every DB call in the
        # app starts timing out until the process is restarted. This
        # returns it on garbage collection instead, same safety sqlite3
        # already gave this exact call pattern for free.
        self.close()

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._conn.__exit__(exc_type, exc_val, exc_tb)


def _get_pool() -> ConnectionPool:
    global _pool, DATABASE_URL
    if _pool is None:
        if not DATABASE_URL:
            DATABASE_URL = os.environ.get("ELECTROGRADER_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not DATABASE_URL:
            raise RuntimeError(
                "ELECTROGRADER_DATABASE_URL (or DATABASE_URL) is not set — "
                "point it at a postgresql://user:password@host/dbname connection string."
            )
        # Postgres's own max_connections defaults to 100 — 25 leaves plenty
        # of headroom for verify_*.py scripts, pgAdmin, and any other tool
        # connecting to the same server at the same time.
        _pool = ConnectionPool(DATABASE_URL, min_size=2, max_size=25, open=True)
    return _pool


def connect() -> _ConnectionWrapper:
    return _ConnectionWrapper(_get_pool())


def close_pool() -> None:
    """One-shot scripts (migration/verify scripts, not the long-running
    Streamlit server) should call this before exiting — otherwise the
    pool's background worker threads can raise a harmless-but-noisy
    error while the interpreter is tearing down."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def table_columns(conn, table_name: str) -> set:
    """Replaces SQLite's `PRAGMA table_info(x)` column introspection used
    by every store module's additive-migration pattern (check which
    columns already exist before ALTER TABLE ADD COLUMN)."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        (table_name,),
    ).fetchall()
    return {r[0] for r in rows}
