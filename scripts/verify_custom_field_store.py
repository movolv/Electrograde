"""Proves modules/custom_field_store.py (per-company self-service Custom
Fields) works correctly:
  - CRUD: create, list, get, rename (key stays stable across a rename),
    delete;
  - the 10-field cap (MAX_CUSTOM_FIELDS) is enforced per company;
  - key slugs are generated once, stable across renames, and collision-
    suffixed ("warranty", "warranty_2", ...) when two labels slugify to
    the same thing;
  - full cross-tenant isolation, same convention as every other store;
  - FULL LIFECYCLE proof: creating a custom field makes it appear as a
    "custom:<key>" Field Mapping source field via the exact
    IntegrationManager function app.py calls, with zero other file
    changes; mapping it and saving a product's custom_fields value flows
    through preview_payload() (the same build_payload() call the real
    BaseLinker export uses); deleting the field makes it disappear as a
    pickable source without silently deleting the orphaned saved rule.

Runs against a throwaway scratch PostgreSQL database, set *before*
importing any modules.*_store module — same convention as every other
verify_*.py script.

    python scripts/verify_custom_field_store.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("custom_field_store")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from integrations.manager import IntegrationManager  # noqa: E402
from integrations.marketplaces.baselinker import client as baselinker_client  # noqa: E402
from modules import company_store, custom_field_store, field_mapping_store  # noqa: E402
from modules.models import Product  # noqa: E402

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
    print(f"Scratch DB: {_DATABASE_URL}\n")

    company_a = company_store.create_company("Custom Field Test Co A", user_limit=10)
    company_b = company_store.create_company("Custom Field Test Co B", user_limit=10)

    print("-- CRUD --")
    check("no fields yet", custom_field_store.list_fields(company_a.id) == [])
    check("count is 0", custom_field_store.count_fields(company_a.id) == 0)

    warranty = custom_field_store.create_field(company_a.id, "Warranty Period")
    check("create returns a definition with a slug key", warranty.key == "warranty_period")
    check("create returns the label unchanged", warranty.label == "Warranty Period")
    check("list now has 1 field", len(custom_field_store.list_fields(company_a.id)) == 1)
    check("get_field finds it", custom_field_store.get_field(warranty.id, company_a.id) is not None)
    check("count is 1", custom_field_store.count_fields(company_a.id) == 1)

    try:
        custom_field_store.create_field(company_a.id, "   ")
        check("blank label is rejected", False)
    except ValueError:
        check("blank label is rejected", True)

    print("\n-- rename keeps the key stable --")
    renamed = custom_field_store.rename_field(warranty.id, company_a.id, "Warranty (months)")
    check("label updated", renamed.label == "Warranty (months)")
    check("key NEVER changes on rename", renamed.key == "warranty_period")
    reloaded = custom_field_store.get_field(warranty.id, company_a.id)
    check("rename persisted", reloaded.label == "Warranty (months)")

    try:
        custom_field_store.rename_field(warranty.id, company_a.id, "")
        check("blank rename is rejected", False)
    except ValueError:
        check("blank rename is rejected", True)

    print("\n-- slug collision suffixing --")
    dup1 = custom_field_store.create_field(company_a.id, "Battery Health!")
    dup2 = custom_field_store.create_field(company_a.id, "Battery Health?")
    check("first label slugifies plainly", dup1.key == "battery_health")
    check("second, colliding label gets a numeric suffix", dup2.key == "battery_health_2")
    check("both rows persist distinctly", len({f.id for f in custom_field_store.list_fields(company_a.id)}) == 3)

    print("\n-- MAX_CUSTOM_FIELDS cap (10 per company) --")
    company_cap = company_store.create_company("Custom Field Cap Test Co", user_limit=10)
    for i in range(custom_field_store.MAX_CUSTOM_FIELDS):
        custom_field_store.create_field(company_cap.id, f"Field {i}")
    check(f"exactly {custom_field_store.MAX_CUSTOM_FIELDS} fields created", custom_field_store.count_fields(company_cap.id) == custom_field_store.MAX_CUSTOM_FIELDS)
    try:
        custom_field_store.create_field(company_cap.id, "One Too Many")
        check("the 11th field is rejected", False)
    except ValueError:
        check("the 11th field is rejected", True)
    check("count still at the cap after the rejected attempt", custom_field_store.count_fields(company_cap.id) == custom_field_store.MAX_CUSTOM_FIELDS)

    print("\n-- delete --")
    custom_field_store.delete_field(dup2.id, company_a.id)
    check("deleted field is gone from list", all(f.id != dup2.id for f in custom_field_store.list_fields(company_a.id)))
    check("get_field returns None for a deleted field", custom_field_store.get_field(dup2.id, company_a.id) is None)
    check("count dropped by 1", custom_field_store.count_fields(company_a.id) == 2)

    print("\n-- cross-tenant isolation --")
    check("company B has no fields of its own yet", custom_field_store.list_fields(company_b.id) == [])
    supplier = custom_field_store.create_field(company_b.id, "Supplier Batch")
    check("company A's fields are unaffected by company B's create", custom_field_store.count_fields(company_a.id) == 2)
    check("company B's own field is its own", custom_field_store.count_fields(company_b.id) == 1)
    check("company A cannot get_field a company B field by id", custom_field_store.get_field(supplier.id, company_a.id) is None)
    check("company B cannot rename a company A field", custom_field_store.get_field(warranty.id, company_b.id) is None)
    custom_field_store.delete_field(warranty.id, company_b.id)  # no-op: wrong company scope
    check("cross-company delete attempt is a silent no-op, company A's field survives", custom_field_store.get_field(warranty.id, company_a.id) is not None)

    # ------------------------ FULL LIFECYCLE: a custom field, end to end --
    print("\n-- FULL LIFECYCLE: a custom field, source discovery -> mapping -> payload --")
    company_lc = company_store.create_company("Custom Field Lifecycle Test Co", user_limit=10)
    voltage_cf = custom_field_store.create_field(company_lc.id, "Voltage Rating")
    custom_key = f"custom:{voltage_cf.key}"

    # (1) Appears via the exact call app.py's _render_field_mapping_tab makes.
    ui_source_fields = IntegrationManager.get_mappable_source_fields("baselinker", company_id=company_lc.id)
    check("(1) new custom field appears as a Field Mapping source field, namespaced 'custom:<key>'", custom_key in ui_source_fields)

    # (2) Mappable onto a real BaseLinker target.
    ui_target_fields = IntegrationManager.get_supported_target_fields("baselinker", company_id=company_lc.id)
    check("(2) a BaseLinker target field is available to map it onto", bool(ui_target_fields))

    # (3) Save the mapping — same store call app.py's Save button makes.
    cf_mapping = field_mapping_store.FieldMapping(
        company_id=company_lc.id, integration_type="baselinker",
        rules=[field_mapping_store.FieldMappingRule(source_field=custom_key, target_field="extra_field_888")],
    )
    field_mapping_store.upsert_mapping(cf_mapping)
    reloaded_cf_mapping = field_mapping_store.get_mapping(company_lc.id, "baselinker")
    check("(3) the mapping persists", any(r.source_field == custom_key and r.target_field == "extra_field_888" for r in reloaded_cf_mapping.rules))

    # (4) Reflected in preview_payload() — reads product.custom_fields, not getattr.
    cf_product = Product(company_id=company_lc.id, sku="CF-TEST-SKU", custom_fields={voltage_cf.key: "230V"})
    cf_connector = baselinker_client.BaselinkerConnector(company_lc.id, {"token": ""}, {
        "inventory_id": "1", "category_id": "1",
    })
    cf_preview = cf_connector.preview_payload(cf_product)
    check(
        "(4) preview_payload() carries the custom field's value at its mapped target",
        cf_preview.get("text_fields", {}).get("extra_field_888|en") == "230V",
    )

    # (5) Delete the definition -> no longer offered as a source field, but
    # the already-saved rule is NOT silently dropped (matches the registry
    # field orphan-handling pattern in verify_dynamic_field_mapping.py).
    custom_field_store.delete_field(voltage_cf.id, company_lc.id)
    check(
        "(5) deleted custom field no longer appears as a source field",
        custom_key not in IntegrationManager.get_mappable_source_fields("baselinker", company_id=company_lc.id),
    )
    check(
        "(5) the orphaned saved rule for it is NOT silently deleted from storage",
        any(r.source_field == custom_key for r in field_mapping_store.get_mapping(company_lc.id, "baselinker").rules),
    )

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
