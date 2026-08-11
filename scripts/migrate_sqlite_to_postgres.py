"""One-time data migration: copies every row from the legacy SQLite file
(data/inventory.db) into the new PostgreSQL database (modules/db.py). Run
once, after the PostgreSQL schema already exists (i.e. after the app / any
store module has been imported at least once against ELECTROGRADER_DATABASE_URL,
since each modules/*_store.py's _connect() creates its own tables).

Column-name-based (not positional) so table column ORDER differences
between the old SQLite schema and the freshly-created Postgres schema
don't matter — only that both sides have a matching set of column names,
which they do (every store module's CREATE TABLE/ALTER TABLE was ported
statement-for-statement; see modules/db.py's docstring).

Usage:
    python3 scripts/migrate_sqlite_to_postgres.py
Reads the SQLite source from ELECTROGRADER_DB_PATH (default
data/inventory.db) and writes to ELECTROGRADER_DATABASE_URL (or
DATABASE_URL) — same env var conventions as the rest of the app.
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import db  # noqa: E402

SQLITE_PATH = os.environ.get("ELECTROGRADER_DB_PATH") or str(
    Path(__file__).resolve().parent.parent / "data" / "inventory.db"
)

# Order doesn't matter for correctness (no DB-level FK constraints in this
# codebase — see modules/company_store.py's docstring), but companies/users
# first reads more sensibly in the printed log.
TABLES = [
    "companies", "users", "sessions", "products", "manifest_batches",
    "repair_events", "marketplace_listings", "audit_log",
    "company_integrations", "integration_sync_log", "integration_field_mappings",
    "sync_jobs", "sync_queue", "sync_records", "sync_logs", "sync_field_config",
    "sync_conflicts", "integration_sync_rules", "product_change_log",
    "catalog_import_jobs",
]


def migrate_table(sqlite_conn: sqlite3.Connection, pg_conn, table: str) -> tuple:
    cur = sqlite_conn.execute(f"SELECT * FROM {table}")
    col_names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    if not rows:
        return 0, 0

    placeholders = ", ".join(["?"] * len(col_names))
    col_list = ", ".join(col_names)
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

    with pg_conn:
        pg_conn.executemany(insert_sql, [tuple(r) for r in rows])

    count = pg_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return len(rows), count


def main():
    if not Path(SQLITE_PATH).exists():
        print(f"SQLite source not found at {SQLITE_PATH} — nothing to migrate.")
        return

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    pg_conn = db.connect()

    print(f"Source: {SQLITE_PATH}")
    print(f"Destination: {db.DATABASE_URL}\n")

    total_source, total_dest = 0, 0
    failures = []
    for table in TABLES:
        try:
            existing = pg_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if existing:
                print(f"{table}: SKIPPED (destination already has {existing} row(s) — not overwriting)")
                continue
            src_count, dest_count = migrate_table(sqlite_conn, pg_conn, table)
            total_source += src_count
            total_dest += dest_count
            status = "OK" if src_count == dest_count else "MISMATCH"
            print(f"{table}: {src_count} -> {dest_count} ({status})")
            if src_count != dest_count:
                failures.append(table)
        except Exception as e:
            print(f"{table}: FAILED — {e}")
            failures.append(table)

    sqlite_conn.close()
    pg_conn.close()
    db.close_pool()

    print(f"\nTotal rows migrated: {total_source} -> {total_dest}")
    if failures:
        print(f"Tables with problems: {failures}")
        sys.exit(1)
    print("Migration completed successfully.")


if __name__ == "__main__":
    main()
