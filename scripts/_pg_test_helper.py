"""Shared helper for verify_*.py scripts: a throwaway PostgreSQL database
per run, replacing the old tempfile.mkdtemp()/shutil.rmtree() SQLite
scratch-FILE pattern now that every modules/*_store.py talks to Postgres
(modules/db.py) instead of a single SQLite file. Same spirit — fully
isolated, disposable, cleaned up when the script exits — just backed by a
throwaway DATABASE instead of a throwaway file.

Connects as the local dev Postgres superuser to CREATE/DROP the scratch
database itself; override via ELECTROGRADER_PG_ADMIN_URL if your local
Postgres uses different credentials (defaults match the one this project's
migration set up locally — see modules/db.py's docstring).
"""
import os
import uuid

import psycopg

_ADMIN_URL = os.environ.get(
    "ELECTROGRADER_PG_ADMIN_URL",
    "postgresql://postgres:ElectroGrader2026_local@127.0.0.1/postgres",
)


def make_scratch_database(name_hint: str):
    """Creates a uniquely-named scratch database. Returns (database_url, drop_fn)
    — call drop_fn() when done (typically in a finally: block)."""
    db_name = f"electrograder_test_{name_hint}_{uuid.uuid4().hex[:8]}"
    admin_conn = psycopg.connect(_ADMIN_URL, autocommit=True)
    admin_conn.execute(f'CREATE DATABASE "{db_name}"')
    admin_conn.close()

    database_url = f"{_ADMIN_URL.rsplit('/', 1)[0]}/{db_name}"

    def drop():
        # modules/db.py's pool may still hold open connections to the
        # scratch database — DROP DATABASE can't run while any exist.
        from modules import db as _db
        _db.close_pool()
        conn = psycopg.connect(_ADMIN_URL, autocommit=True)
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        conn.close()

    return database_url, drop
