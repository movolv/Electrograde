"""Proves the Two-Way Sync conflict/history engine
(modules/sync_ownership_store.py's conflict_policy additions,
modules/sync_log_store.py, sync/conflict_resolver.py) works correctly:
  - resolve_flow() derives the correct direction from ownership alone;
  - conflict_policy defaults match ownership (mirroring get_field_owner's
    existing default logic), with owner="manual" defaulting to
    "manual_review";
  - a saved conflict_policy ALWAYS wins over the computed default, in both
    directions, same guarantee as get_field_owner();
  - is_conflicting_configuration() flags a real ownership/policy mismatch
    but never flags "manual_review";
  - resolve_field_change()'s four decision paths: accepted, overridden
    (both directions), and pending_manual_review — each logged to
    sync_logs and (for the non-owner-changed cases) sync_conflicts;
  - full cross-tenant isolation on the new sync_logs table.

Runs against a throwaway scratch database (ELECTROGRADER_DB_PATH), set
*before* importing any modules.*_store / sync module — same convention as
scripts/verify_sync_ownership.py.

    python scripts/verify_two_way_sync.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCRATCH_DIR = tempfile.mkdtemp(prefix="electrograder_two_way_sync_test_")
os.environ["ELECTROGRADER_DB_PATH"] = os.path.join(_SCRATCH_DIR, "test.db")
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import company_store, sync_log_store, sync_ownership_store  # noqa: E402
from sync import conflict_resolver  # noqa: E402

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

    company_a = company_store.create_company("Two-Way Sync Test Co A", user_limit=10)
    company_b = company_store.create_company("Two-Way Sync Test Co B", user_limit=10)
    connector = "baselinker"

    # --------------------------------------------------------------- resolve_flow --
    print("-- resolve_flow() derives direction purely from ownership --")
    check("electrograder owner -> export", sync_ownership_store.resolve_flow("electrograder", connector) == "export")
    check("connector owner -> import", sync_ownership_store.resolve_flow(connector, connector) == "import")
    check("manual owner -> manual", sync_ownership_store.resolve_flow("manual", connector) == "manual")

    # -------------------------------------------------- conflict_policy defaults --
    print("\n-- conflict_policy default values (no saved config yet) --")
    check(
        "price (owner=electrograder) defaults to electrograder",
        sync_ownership_store.get_conflict_policy(company_a.id, connector, "price") == "electrograder",
    )
    check(
        "quantity (owner=connector) defaults to the connector",
        sync_ownership_store.get_conflict_policy(company_a.id, connector, "quantity") == connector,
    )
    sync_ownership_store.upsert_field_config(company_a.id, connector, "brand", "manual")
    check(
        "a field explicitly configured to owner=manual defaults its policy to manual_review",
        sync_ownership_store.get_conflict_policy(company_a.id, connector, "brand") == "manual_review",
    )

    # ---------------------------------------- saved conflict_policy always wins --
    print("\n-- saved conflict_policy ALWAYS wins over the computed default (both directions) --")
    sync_ownership_store.upsert_field_config(company_a.id, connector, "price", "electrograder", conflict_policy="manual_review")
    check(
        "price policy overridden AWAY from its default (electrograder) sticks",
        sync_ownership_store.get_conflict_policy(company_a.id, connector, "price") == "manual_review",
    )
    sync_ownership_store.upsert_field_config(company_a.id, connector, "price", "electrograder", conflict_policy="electrograder")
    check(
        "price policy explicitly re-saved back to its default value is still read as a real saved row",
        sync_ownership_store.get_conflict_policy(company_a.id, connector, "price") == "electrograder",
    )

    # ------------------------------------------------- is_conflicting_configuration --
    print("\n-- is_conflicting_configuration() --")
    check(
        "owner=electrograder + policy=connector -> mismatch",
        sync_ownership_store.is_conflicting_configuration("electrograder", connector) is True,
    )
    check(
        "owner=connector + policy=electrograder -> mismatch",
        sync_ownership_store.is_conflicting_configuration(connector, "electrograder") is True,
    )
    check(
        "owner matches policy -> no mismatch",
        sync_ownership_store.is_conflicting_configuration("electrograder", "electrograder") is False,
    )
    check(
        "policy=manual_review is never a mismatch, for any owner",
        sync_ownership_store.is_conflicting_configuration("electrograder", "manual_review") is False
        and sync_ownership_store.is_conflicting_configuration(connector, "manual_review") is False,
    )

    # ------------------------------------------------------ resolve_field_change --
    print("\n-- resolve_field_change(): all four decision paths --")

    # Path a: owner=electrograder, changed_in=electrograder -> accepted, no conflict.
    sync_ownership_store.upsert_field_config(company_a.id, connector, "name", "electrograder", conflict_policy="electrograder")
    result_a = conflict_resolver.resolve_field_change(
        company_a.id, "prod1", connector, "name", "electrograder", "New Title", "Old Title",
    )
    check("owner changes it -> accepted", result_a.resolution_action == conflict_resolver.ACTION_ACCEPTED)
    check("accepted -> applied_value is the owner's value", result_a.applied_value == "New Title")
    check("accepted -> no conflict created", result_a.conflict_created is False)

    # Path b: owner=electrograder, non-owner (connector) changes it, policy=electrograder -> overridden, keep ElectroGrader.
    sync_ownership_store.upsert_field_config(company_a.id, connector, "sku_field_b", "electrograder", conflict_policy="electrograder")
    result_b = conflict_resolver.resolve_field_change(
        company_a.id, "prod1", connector, "sku_field_b", connector, "EG-Value", "BL-Value",
    )
    check("non-owner changes it, policy=electrograder -> overridden", result_b.resolution_action == conflict_resolver.ACTION_OVERRIDDEN)
    check("overridden (policy=electrograder) -> applied_value is ElectroGrader's", result_b.applied_value == "EG-Value")
    check("overridden -> conflict created", result_b.conflict_created is True)

    # Path c: owner=electrograder, non-owner changes it, policy=connector -> overridden, keep connector value.
    sync_ownership_store.upsert_field_config(company_a.id, connector, "sku_field_c", "electrograder", conflict_policy=connector)
    result_c = conflict_resolver.resolve_field_change(
        company_a.id, "prod1", connector, "sku_field_c", connector, "EG-Value", "BL-Value",
    )
    check("non-owner changes it, policy=connector -> overridden", result_c.resolution_action == conflict_resolver.ACTION_OVERRIDDEN)
    check("overridden (policy=connector) -> applied_value is the connector's", result_c.applied_value == "BL-Value")

    # Path d: owner=electrograder, non-owner changes it, policy=manual_review -> pending, nothing applied.
    sync_ownership_store.upsert_field_config(company_a.id, connector, "sku_field_d", "electrograder", conflict_policy="manual_review")
    result_d = conflict_resolver.resolve_field_change(
        company_a.id, "prod1", connector, "sku_field_d", connector, "EG-Value", "BL-Value",
    )
    check("non-owner changes it, policy=manual_review -> pending", result_d.resolution_action == conflict_resolver.ACTION_PENDING_MANUAL_REVIEW)
    check("pending -> accepted is False (nothing applied)", result_d.accepted is False)
    check("pending -> applied_value is None", result_d.applied_value is None)
    check("pending -> conflict created", result_d.conflict_created is True)

    pending_conflicts = [c for c in sync_ownership_store.list_conflicts(company_a.id, "prod1") if c.field_name == "sku_field_d"]
    check("pending conflict has blank resolution (awaiting a human)", pending_conflicts and pending_conflicts[0].resolution == "")

    overridden_conflicts = [c for c in sync_ownership_store.list_conflicts(company_a.id, "prod1") if c.field_name == "sku_field_b"]
    check("overridden conflict records which policy auto-resolved it", overridden_conflicts and overridden_conflicts[0].resolution == "electrograder")

    logs_for_prod1 = sync_log_store.list_logs(company_a.id, "prod1")
    check("sync_logs recorded all 4 resolutions for prod1", len(logs_for_prod1) == 4)

    # ------------------------------------------------------------------- isolation --
    print("\n-- cross-tenant isolation (sync_logs) --")
    sync_log_store.record_log(company_b.id, "prodB", connector, "name", old_value="x", new_value="y", direction="export", source="electrograder")
    check("company A's logs unaffected by company B's log", len(sync_log_store.list_logs(company_a.id, "prod1")) == 4)
    check("company B sees only its own log", len(sync_log_store.list_logs(company_b.id)) == 1)
    check("company A sees none of company B's logs", len(sync_log_store.list_logs(company_a.id, "prodB")) == 0)

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
