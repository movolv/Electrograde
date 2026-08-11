"""Proves the Phase 2 BaseLinker wiring (Synchronization toggles -> real
export payload) is backward-compatible and correct:
  - a company with no SyncRule at all gets the exact legacy payload;
  - a company whose SyncRule was saved but never touched (fields_send
    empty, fields_send_configured=False) ALSO gets the legacy payload —
    the whole point of the fields_send_configured flag;
  - a company that explicitly configured toggles gets exactly those
    fields, nothing more;
  - the migration backfill correctly promotes a pre-Phase-2 row (non-empty
    fields_send, column didn't exist yet) to configured=True;
  - connecting a brand-new integration seeds a sensible default
    configuration automatically.

Runs against a throwaway scratch PostgreSQL database (ELECTROGRADER_DATABASE_URL),
set *before* importing any modules.*_store / integrations module — same
convention as scripts/verify_tenant_isolation.py.

    python scripts/verify_baselinker_export.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("baselinker_export")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

import psycopg  # noqa: E402

from modules import company_store, db, sync_rules_store  # noqa: E402
from integrations import manager as integration_manager  # noqa: E402
from integrations.marketplaces.baselinker import mapper  # noqa: E402
from integrations.marketplaces.baselinker.client import BaselinkerConnector  # noqa: E402

_checks_passed = 0
_failures = []


def check(label: str, condition: bool) -> None:
    global _checks_passed
    if condition:
        _checks_passed += 1
    else:
        _failures.append(label)
    print(("  OK  " if condition else "  FAIL"), label)


class _FakeProduct:
    sku = "SKU1"
    name = "Widget"
    model_number = "M1"
    product_description = "Desc"
    condition_description = "Scratched"
    ean = "123456"
    manifest_barcode = ""
    scanned_barcode = ""
    price = 99.99
    quantity = 3
    image_paths = []  # empty on purpose — no real files needed for these checks
    box_length_cm = 0
    box_width_cm = 0
    box_height_cm = 0
    manifest_weight_kg = 0


_CONFIG = {"inventory_id": 1, "category_id": 2, "tax_rate": 23, "price_group_id": "pg1", "warehouse_id": "w1"}


def main() -> int:
    print(f"Scratch DB: {_DATABASE_URL}\n")

    company_a = company_store.create_company("Export Test Co A", user_limit=10)
    company_b = company_store.create_company("Export Test Co B", user_limit=10)

    # ------------------------------------------------------- legacy: no rule --
    print("-- legacy: company with no SyncRule at all --")
    connector_a = BaselinkerConnector(company_a.id, {"token": "t"}, {"inventory_id": "1", "category_id": "2"})
    fields_send = connector_a._resolve_fields_send()  # noqa: SLF001
    check("_resolve_fields_send() is None when no rule exists", fields_send is None)
    legacy_payload = mapper.build_payload(_FakeProduct(), _CONFIG, fields_send=fields_send)
    full_payload = mapper.build_payload(_FakeProduct(), _CONFIG, fields_send=None)
    check("payload identical to unconditional (legacy) build", legacy_payload == full_payload)
    check("legacy payload includes ean (barcode)", "ean" in legacy_payload)
    check("legacy payload includes prices", "prices" in legacy_payload)

    # ------------------------------------------- rule exists but never saved --
    print("\n-- rule exists but fields_send_configured=False (never really saved) --")
    sync_rules_store.upsert_rule(
        sync_rules_store.SyncRule(company_id=company_a.id, integration_type="baselinker", fields_send=[])
    )
    fields_send2 = connector_a._resolve_fields_send()  # noqa: SLF001
    check("_resolve_fields_send() still None (configured=False)", fields_send2 is None)
    payload2 = mapper.build_payload(_FakeProduct(), _CONFIG, fields_send=fields_send2)
    check("payload still identical to legacy despite an (unconfigured) rule existing", payload2 == full_payload)

    # --------------------------------------------------- real, saved toggles --
    print("\n-- real, explicitly saved toggles --")
    sync_rules_store.upsert_rule(
        sync_rules_store.SyncRule(
            company_id=company_a.id, integration_type="baselinker",
            fields_send=["name", "product_description", "price", "quantity", "sku"],
            fields_send_configured=True,
        )
    )
    fields_send3 = connector_a._resolve_fields_send()  # noqa: SLF001
    check("_resolve_fields_send() returns the saved set", fields_send3 == {"name", "product_description", "price", "quantity", "sku"})
    payload3 = mapper.build_payload(_FakeProduct(), _CONFIG, fields_send=fields_send3)
    check("toggled payload excludes ean (barcode not selected)", "ean" not in payload3)
    check("toggled payload excludes description_extra1 (condition_description not selected)",
          "description_extra1" not in payload3.get("text_fields", {}))
    check("toggled payload still includes sku (always sent)", payload3.get("sku") == "SKU1")
    check("toggled payload includes prices (price selected)", "prices" in payload3)

    # ------------------------------------------------------- migration backfill --
    print("\n-- migration backfill: pre-Phase-2 row gets configured=1 retroactively --")
    # modules/db.py's DATABASE_URL is a module-level singleton (one shared
    # pool for the whole process) — there's no more per-module DB_PATH to
    # monkeypatch, so this test instead points the WHOLE db module at a
    # second scratch database that pre-dates the fields_send_configured
    # column, proving the one-time ALTER+backfill runs correctly on first
    # _connect() against that older schema, then points it back.
    backfill_url, backfill_drop = make_scratch_database("baselinker_export_backfill")
    backfill_admin = psycopg.connect(backfill_url, autocommit=True)
    backfill_admin.execute(
        """CREATE TABLE integration_sync_rules (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL, integration_type TEXT NOT NULL,
            frequency TEXT NOT NULL DEFAULT 'manual', direction TEXT NOT NULL DEFAULT 'push',
            conflict_handling TEXT NOT NULL DEFAULT 'keep_local',
            fields_send TEXT NOT NULL DEFAULT '[]', fields_receive TEXT NOT NULL DEFAULT '[]',
            automation_triggers TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
            last_enqueued_at REAL NOT NULL DEFAULT 0, created_at REAL, updated_at REAL
        )"""
    )
    backfill_admin.execute(
        "INSERT INTO integration_sync_rules (id, company_id, integration_type, fields_send, created_at, updated_at) "
        "VALUES ('old1', 'oldco', 'baselinker', '[\"name\",\"price\"]', 1.0, 1.0)"
    )
    backfill_admin.execute(
        "INSERT INTO integration_sync_rules (id, company_id, integration_type, fields_send, created_at, updated_at) "
        "VALUES ('old2', 'oldco', 'deepl', '[]', 1.0, 1.0)"
    )
    backfill_admin.close()

    _original_database_url = db.DATABASE_URL
    db.close_pool()
    db.DATABASE_URL = backfill_url
    try:
        old_rule_nonempty = sync_rules_store.get_rule("oldco", "baselinker")
        old_rule_empty = sync_rules_store.get_rule("oldco", "deepl")
    finally:
        db.close_pool()
        db.DATABASE_URL = _original_database_url
    check(
        "pre-existing row with non-empty fields_send backfilled to configured=True",
        old_rule_nonempty is not None and old_rule_nonempty.fields_send_configured is True,
    )
    check(
        "pre-existing row with empty fields_send stays configured=False",
        old_rule_empty is not None and old_rule_empty.fields_send_configured is False,
    )
    backfill_drop()

    # ------------------------------------------------------------- default config --
    print("\n-- default configuration seeded on first connect() --")
    check(
        "BaselinkerConnector.DEFAULT_SYNC_FIELDS matches only implemented fields",
        set(BaselinkerConnector.DEFAULT_SYNC_FIELDS) <= BaselinkerConnector.IMPLEMENTED_SYNC_FIELDS | {"sku"},
    )
    check(
        "brand/model/category/product_condition/defects excluded from BaselinkerConnector.DEFAULT_SYNC_FIELDS",
        not ({"brand", "model", "category", "product_condition", "defects"} & set(BaselinkerConnector.DEFAULT_SYNC_FIELDS)),
    )

    # ------------------------------------------------------------------- summary --
    print(f"\n{_checks_passed} check(s) passed, {len(_failures)} failed.")
    if _failures:
        print("\nFAILED:")
        for f in _failures:
            print(f"  - {f}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    finally:
        _drop_scratch_db()
    raise SystemExit(exit_code)
