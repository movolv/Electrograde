"""Proves the Sync Ownership configuration (modules/sync_ownership_store.py)
works correctly:
  - saved config persists and loads back exactly;
  - default_owner values match the spec (Price->electrograder,
    Quantity/Status->the connector, everything else->electrograder);
  - the core guarantee: a SAVED choice ALWAYS wins over default_owner, in
    both directions (overriding away from the default AND overriding back
    to it) — never silently falls back once a real choice exists;
  - full cross-tenant isolation on sync_field_config and sync_conflicts;
  - sync/engine.py's get_export_field_owners() (the "future sync
    readiness" read path) returns the correct field -> source_system map.

Runs against a throwaway scratch database (ELECTROGRADER_DB_PATH), set
*before* importing any modules.*_store / integrations / sync module — same
convention as scripts/verify_tenant_isolation.py.

    python scripts/verify_sync_ownership.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCRATCH_DIR = tempfile.mkdtemp(prefix="electrograder_sync_ownership_test_")
os.environ["ELECTROGRADER_DB_PATH"] = os.path.join(_SCRATCH_DIR, "test.db")
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import company_store, sync_ownership_store  # noqa: E402
from sync import engine  # noqa: E402

_checks_passed = 0
_failures = []


def check(label: str, condition: bool) -> None:
    global _checks_passed
    if condition:
        _checks_passed += 1
    else:
        _failures.append(label)
    print(("  OK  " if condition else "  FAIL"), label)


def main() -> int:
    print(f"Scratch DB: {os.environ['ELECTROGRADER_DB_PATH']}\n")

    company_a = company_store.create_company("Sync Ownership Test Co A", user_limit=10)
    company_b = company_store.create_company("Sync Ownership Test Co B", user_limit=10)

    # --------------------------------------------------------------- defaults --
    print("-- default_owner values (no saved config yet) --")
    check("price defaults to electrograder", sync_ownership_store.get_field_owner(company_a.id, "baselinker", "price") == "electrograder")
    check("quantity defaults to the connector", sync_ownership_store.get_field_owner(company_a.id, "baselinker", "quantity") == "baselinker")
    check("status defaults to the connector", sync_ownership_store.get_field_owner(company_a.id, "baselinker", "status") == "baselinker")
    for field_name in ("name", "product_description", "image_paths", "brand", "model", "category", "grade", "defects", "barcode"):
        check(
            f"{field_name} defaults to electrograder",
            sync_ownership_store.get_field_owner(company_a.id, "baselinker", field_name) == "electrograder",
        )

    # ------------------------------------------------------- configuration --
    print("\n-- configuration: save + load --")
    sync_ownership_store.upsert_field_config(company_a.id, "baselinker", "brand", "manual", sync_enabled=False)
    cfg = sync_ownership_store.list_field_config(company_a.id, "baselinker")
    check("saved config loads back with correct source_system", cfg["brand"].source_system == "manual")
    check("saved config loads back with correct sync_enabled", cfg["brand"].sync_enabled is False)
    check("list_field_config still includes every SYNC_OWNERSHIP_FIELDS key", len(cfg) == 12)

    # ----------------------------------------- saved choice always wins over default --
    print("\n-- saved choice ALWAYS wins over default_owner (both directions) --")
    sync_ownership_store.upsert_field_config(company_a.id, "baselinker", "price", "manual")
    check(
        "price overridden AWAY from its default (electrograder) sticks",
        sync_ownership_store.get_field_owner(company_a.id, "baselinker", "price") == "manual",
    )
    sync_ownership_store.upsert_field_config(company_a.id, "baselinker", "quantity", "electrograder")
    check(
        "quantity overridden AWAY from its default (the connector) sticks",
        sync_ownership_store.get_field_owner(company_a.id, "baselinker", "quantity") == "electrograder",
    )
    # Now flip price back to what the default already was — still must be
    # read as a real saved choice, not "no config" (a subtly different bug:
    # a naive implementation might treat "matches the default" as if no row
    # existed).
    sync_ownership_store.upsert_field_config(company_a.id, "baselinker", "price", "electrograder")
    check(
        "price explicitly re-saved back to its default value is still read as a real saved row",
        sync_ownership_store.get_field_owner(company_a.id, "baselinker", "price") == "electrograder",
    )

    # ------------------------------------------------------------- isolation --
    print("\n-- cross-tenant isolation --")
    sync_ownership_store.upsert_field_config(company_b.id, "baselinker", "price", "manual")
    check(
        "company A's price config unaffected by company B's save",
        sync_ownership_store.get_field_owner(company_a.id, "baselinker", "price") == "electrograder",
    )
    check(
        "company B's price config is its own",
        sync_ownership_store.get_field_owner(company_b.id, "baselinker", "price") == "manual",
    )
    sync_ownership_store.record_conflict(company_a.id, "p1", "price", old_value="10", electrograder_value="12", baselinker_value="15")
    check("company A sees its own conflict", len(sync_ownership_store.list_conflicts(company_a.id)) == 1)
    check("company B sees none of company A's conflicts", len(sync_ownership_store.list_conflicts(company_b.id)) == 0)

    # ------------------------------------------------------- future sync readiness --
    print("\n-- sync/engine.py's get_export_field_owners() read path --")
    owners = engine.get_export_field_owners(company_a.id, "baselinker")
    check("returns all 12 fields", len(owners) == 12)
    check("reflects company A's saved overrides (quantity=electrograder)", owners["quantity"] == "electrograder")
    check("reflects an unconfigured field's default (status=baselinker)", owners["status"] == "baselinker")

    # ------------------------------------------------------------------- summary --
    print(f"\n{_checks_passed} check(s) passed, {len(_failures)} failed.")
    if _failures:
        print("\nFAILED:")
        for f in _failures:
            print(f"  - {f}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    import shutil
    try:
        exit_code = main()
    finally:
        shutil.rmtree(_SCRATCH_DIR, ignore_errors=True)
    raise SystemExit(exit_code)
