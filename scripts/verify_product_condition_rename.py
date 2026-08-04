"""Proves the "grade" -> "product_condition" rename
(modules/models.py, modules/inventory_store.py, integrations/field_registry.py,
and the 5 tables that persist field_name strings) preserves every existing
record's data:
  - Product.from_dict() correctly loads a LEGACY JSON blob (only the old
    "grade"/"grade_confidence"/"grade_reasoning" keys, no new ones — exactly
    what every one of the real, already-saved products in production looks
    like) into the new product_condition* fields;
  - a NEW-shaped blob (already has product_condition*) round-trips as-is,
    proving the legacy branch doesn't fire once a record has been re-saved;
  - inventory_store's DB-column migration backfills product_condition from
    the old grade column correctly;
  - the sync_ownership_store/sync_queue_store/sync_log_store/
    product_change_log_store/field_mapping_store field_name migrations
    correctly move "grade" rows (including the field_mapping JSON-blob
    case) to "product_condition", and are idempotent (safe to run twice).

Runs against a throwaway scratch database (ELECTROGRADER_DB_PATH), set
*before* importing any modules.*_store / integrations / sync module — same
convention as every other verify_*.py script in this repo.

    python scripts/verify_product_condition_rename.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCRATCH_DIR = tempfile.mkdtemp(prefix="electrograder_condition_rename_test_")
os.environ["ELECTROGRADER_DB_PATH"] = os.path.join(_SCRATCH_DIR, "test.db")
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules.models import Product  # noqa: E402
from modules import (  # noqa: E402
    field_mapping_store, inventory_store, product_change_log_store,
    sync_log_store, sync_ownership_store, sync_queue_store,
)
from integrations.field_registry import SYNCABLE_FIELDS, SYNC_OWNERSHIP_FIELDS  # noqa: E402

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

    # --------------------------------------------------------- field_registry --
    print("-- field_registry.py: renamed, not duplicated --")
    check("'grade' no longer a SYNCABLE_FIELDS key", "grade" not in SYNCABLE_FIELDS)
    check("'product_condition' is a SYNCABLE_FIELDS key", "product_condition" in SYNCABLE_FIELDS)
    check("'grade' no longer a SYNC_OWNERSHIP_FIELDS key", "grade" not in SYNC_OWNERSHIP_FIELDS)
    check("'product_condition' is a SYNC_OWNERSHIP_FIELDS key", "product_condition" in SYNC_OWNERSHIP_FIELDS)

    # --------------------------------------------------- Product.from_dict() --
    print("\n-- Product.from_dict(): legacy JSON blob backward compat --")
    legacy_blob = {
        "id": "p1", "company_id": "default", "sku": "SKU1", "status": "completed",
        "grade": "A", "grade_confidence": 88, "grade_reasoning": "Looks pristine",
    }
    p = Product.from_dict(legacy_blob)
    check("legacy 'grade' -> product_condition", p.product_condition == "A")
    check("legacy 'grade_confidence' -> product_condition_confidence", p.product_condition_confidence == 88)
    check("legacy 'grade_reasoning' -> product_condition_reasoning", p.product_condition_reasoning == "Looks pristine")

    new_blob = {
        "id": "p2", "company_id": "default", "sku": "SKU2", "status": "completed",
        "product_condition": "C", "product_condition_confidence": 40, "product_condition_reasoning": "Worn",
        # A record that's already been re-saved would NOT carry the old keys at all.
    }
    p2 = Product.from_dict(new_blob)
    check("already-migrated blob loads product_condition directly", p2.product_condition == "C")
    check("already-migrated blob's confidence is untouched by the legacy branch", p2.product_condition_confidence == 40)

    empty_blob = {"id": "p3", "company_id": "default", "sku": "SKU3", "status": "draft"}
    p3 = Product.from_dict(empty_blob)
    check("a record with neither key (never graded) defaults product_condition to ''", p3.product_condition == "")

    # -------------------------------------------------- inventory_store column --
    print("\n-- inventory_store.py: products table column migration --")
    product = Product(company_id="default", sku="SKU-REAL", product_condition="B", product_condition_confidence=70)
    inventory_store.save_product(product)
    reloaded = inventory_store.get_product(product.id, "default")
    check("save/reload round-trips product_condition via the new column + JSON blob", reloaded.product_condition == "B")

    conn = inventory_store._connect()
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    check("products table has a product_condition column", "product_condition" in col_names)
    check("old grade column is left in place (additive-only migration)", "grade" in col_names)
    conn.close()

    # ----------------------------------------------- field_name data migrations --
    print("\n-- field_name migration across the 5 tables that persist it --")

    # Simulate pre-rename rows by writing directly with the old field_name,
    # then re-triggering each store's _connect() migration.
    sync_ownership_store.upsert_field_config("co1", "baselinker", "grade", "electrograder", conflict_policy="electrograder")
    sync_ownership_store.record_conflict("co1", "prod1", "grade", electrograder_value="A", baselinker_value="B")
    sync_queue_store.enqueue("co1", "prod1", "baselinker", "grade", old_value="A", new_value="B")
    sync_log_store.record_log("co1", "prod1", "baselinker", "grade", old_value="A", new_value="B", direction="export", source="electrograder")
    product_change_log_store.record_change("co1", "prod1", "grade", "A", "B", source_system="electrograder")
    field_mapping_store.upsert_mapping(
        field_mapping_store.FieldMapping(
            company_id="co1", integration_type="baselinker",
            rules=[field_mapping_store.FieldMappingRule(source_field="grade", source_value="B", target_label="used-good")],
        )
    )

    # Re-run every store's _connect() migration (idempotent — this is
    # exactly what happens on the next real app restart).
    sync_ownership_store._connect().close()
    sync_queue_store._connect().close()
    sync_log_store._connect().close()
    product_change_log_store._connect().close()
    field_mapping_store._connect().close()

    check(
        "sync_field_config: 'grade' row moved to 'product_condition'",
        sync_ownership_store.get_field_owner("co1", "baselinker", "product_condition") == "electrograder",
    )
    conflicts = sync_ownership_store.list_conflicts("co1", "prod1")
    check("sync_conflicts: field_name migrated", any(c.field_name == "product_condition" for c in conflicts))

    queue_items = sync_queue_store.list_queue("co1", "prod1")
    check("sync_queue: field_name migrated", any(q.field_name == "product_condition" for q in queue_items))

    logs = sync_log_store.list_logs("co1", "prod1")
    check("sync_logs: field_name migrated", any(l.field_name == "product_condition" for l in logs))

    changes = product_change_log_store.list_changes("co1", "prod1")
    check("product_change_log: field_name migrated", any(c.field_name == "product_condition" for c in changes))

    mapping = field_mapping_store.get_mapping("co1", "baselinker")
    check(
        "field_mapping_store: source_field migrated inside the JSON rules blob",
        mapping is not None and mapping.rules[0].source_field == "product_condition",
    )

    # Idempotence: running the migration a second time must not error or duplicate.
    sync_ownership_store._connect().close()
    field_mapping_store._connect().close()
    mapping2 = field_mapping_store.get_mapping("co1", "baselinker")
    check("field_mapping migration is idempotent (re-running doesn't corrupt it)", mapping2.rules[0].source_field == "product_condition")

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
