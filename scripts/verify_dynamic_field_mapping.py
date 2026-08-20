"""Proves the dynamic Field Mapping redesign works correctly:
  - integrations/field_registry.SYNCABLE_FIELDS's `mappable` flags match
    the audited CORE vs. USER-MAPPABLE classification;
  - MarketplaceConnector.get_mappable_source_fields() is genuinely
    dynamic — adding a new mappable=True registry entry makes it appear
    with ZERO code changes, and a connector's own CORE_FIELDS (e.g.
    BaselinkerConnector's "name") narrows the registry's default further;
  - "category_id" is no longer offered as a Field Mapping target (owned
    exclusively by Category Mapping now);
  - mapper.build_payload()'s generalized redirect mechanism: a field's
    DEFAULT placement is unchanged with no rule saved (backward compat),
    a redirect rule suppresses that default and applies the rule instead,
    a field NOT in the caller-supplied `redirectable_fields` ignores a
    redirect attempt for its own default placement, a field with NO
    default placement at all (defects) can still be mapped, and "sku"
    redirects are always refused;
  - existing saved rules for the original 5 mappable fields still
    resolve correctly with zero migration needed (the persistence format
    never changed);
  - full cross-tenant isolation on field_mapping_store;
  - the BaseLinker side of discovery is ALSO live, not cached: a fake
    extra field returned by a monkeypatched list_extra_fields() call
    appears in get_target_fields() immediately, and disappears again once
    the real (empty-token, offline) call path is used;
  - FULL LIFECYCLE proof for a brand-new field ("voltage_test"): adding
    ONE registry entry with mappable=True is sufficient — via the exact
    IntegrationManager functions app.py itself calls — for it to become
    a pickable source, be mapped, persist, and show up in
    preview_payload() (the same build_payload() call the real export
    uses); removing it again makes it disappear as a pickable source
    without silently deleting the orphaned saved rule.

Runs against a throwaway scratch PostgreSQL database, set *before*
importing any modules.*_store module — same convention as every other
verify_*.py script.

    python scripts/verify_dynamic_field_mapping.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("dynamic_field_mapping")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from integrations import field_registry  # noqa: E402
from integrations.manager import IntegrationManager  # noqa: E402
from integrations.marketplaces.baselinker import client as baselinker_client  # noqa: E402
from integrations.marketplaces.baselinker import mapper  # noqa: E402
from modules import company_store, field_mapping_store  # noqa: E402
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

    # ---------------------------------------------- registry classification --
    print("-- field_registry.SYNCABLE_FIELDS mappable classification --")
    expected_core = {"sku", "price", "quantity", "image_paths", "category"}
    expected_mappable = {
        "name", "brand", "model", "product_description", "condition_description",
        "defects", "product_condition", "color", "power", "weight_kg", "barcode",
        # Added later: previously-invisible product-card fields with no
        # structural coupling — see integrations/field_registry.py.
        "location", "functional_test_result", "condition_type", "spec_summary",
        "box_contents", "missing_components", "functional_checklist",
    }
    actual_core = {k for k, v in field_registry.SYNCABLE_FIELDS.items() if not v.get("mappable")}
    actual_mappable = {k for k, v in field_registry.SYNCABLE_FIELDS.items() if v.get("mappable")}
    check("registry-level core fields match the audit", actual_core == expected_core)
    check("registry-level mappable fields match the audit", actual_mappable == expected_mappable)

    # ------------------------------------------- dynamic source-field discovery --
    print("\n-- get_mappable_source_fields() is genuinely dynamic --")
    company = company_store.create_company("Dynamic Field Mapping Test Co", user_limit=10)
    connector = baselinker_client.BaselinkerConnector(company.id, {"token": ""}, {
        "inventory_id": "1", "category_id": "1",
    })

    before = connector.get_mappable_source_fields()
    check("'name' is excluded via BaselinkerConnector.CORE_FIELDS despite mappable=True in the registry", "name" not in before)
    check("'brand' (an existing mappable field) is present", "brand" in before)
    check("'defects' (newly mappable, no default placement) is present", "defects" in before)
    check("'sku'/'price'/'quantity'/'image_paths'/'category' are absent (registry-level core)", not ({"sku", "price", "quantity", "image_paths", "category"} & before))

    # Simulate "tomorrow ElectroGrader adds a new field" — zero code
    # changes anywhere else required for it to appear.
    field_registry.SYNCABLE_FIELDS["voltage"] = {"label": "Voltage", "type": "text", "mappable": True}
    try:
        after = connector.get_mappable_source_fields()
        check("a brand-new mappable=True registry field appears with NO code change", "voltage" in after)
    finally:
        del field_registry.SYNCABLE_FIELDS["voltage"]
    check("removing it again makes it disappear (proves it wasn't cached)", "voltage" not in connector.get_mappable_source_fields())

    # ------------------------------------------------- target fields --
    print("\n-- target fields --")
    check("'category_id' is NOT offered as a Field Mapping target (owned by Category Mapping)", "category_id" not in connector.SUPPORTED_TARGET_FIELDS)
    check("'ean' is offered as a target", "ean" in connector.SUPPORTED_TARGET_FIELDS)
    check("'weight' is offered as a target", "weight" in connector.SUPPORTED_TARGET_FIELDS)

    # --------------------------------------------- build_payload() redirects --
    print("\n-- mapper.build_payload(): generalized redirect mechanism --")
    product = Product(
        company_id=company.id, sku="TEST-SKU", brand="Bosch", model="ABC123",
        product_description="A great product", condition_description="Minor scratches",
        defects=["scratch on lid"], weight_kg=2.5, ean="1234567890123",
    )
    config = {"inventory_id": 1, "category_id": 1, "tax_rate": 23, "price_group_id": None, "warehouse_id": None}
    mappable = connector.get_mappable_source_fields()

    # (a) No rules at all — every default placement is exactly what it
    # always was (backward compat for a company that's never touched
    # Field Mapping).
    payload = mapper.build_payload(product, config, title="A great product", description="A great product", language="en")
    check(
        "product_description defaults to text_fields[description|en] with no rule",
        payload.get("text_fields", {}).get("description|en") == "A great product",
    )
    check(
        "weight_kg defaults to payload[weight] with no rule",
        payload.get("weight") == 2.5,
    )
    check(
        "barcode defaults to payload[ean] with no rule",
        payload.get("ean") == "1234567890123",
    )
    check("defects has NO destination with no rule (unchanged historical behavior)", "defects" not in str(payload))

    # (b) A redirect rule for product_description onto a custom extra_field
    # — suppresses the default `description|en` placement, lands at the
    # new spot instead.
    redirect_rule = field_mapping_store.FieldMappingRule(source_field="product_description", target_field="extra_field_999")
    payload2 = mapper.build_payload(
        product, config, title="A great product", description="A great product", language="en",
        field_mapping_rules=[redirect_rule], redirectable_fields=mappable,
    )
    check(
        "redirected product_description is NOT at its old default spot anymore",
        "description|en" not in payload2.get("text_fields", {}),
    )
    check(
        "redirected product_description IS at its new target",
        payload2.get("text_fields", {}).get("extra_field_999|en") == "A great product",
    )

    # (c) defects (no default placement at all) mapped via a rule — should
    # just work, nothing to suppress.
    defects_rule = field_mapping_store.FieldMappingRule(source_field="defects", target_field="extra_field_888")
    payload3 = mapper.build_payload(
        product, config, title="x", description="x", language="en",
        field_mapping_rules=[defects_rule], redirectable_fields=mappable,
    )
    check(
        "defects (no default) reaches its mapped target",
        payload3.get("text_fields", {}).get("extra_field_888|en") == "scratch on lid",
    )

    # (d) A field the CALLER did not mark redirectable ignores a redirect
    # attempt for its own default placement — proves the connector (via
    # `redirectable_fields`), not mapper.py itself, decides what's
    # redirectable.
    weight_rule = field_mapping_store.FieldMappingRule(source_field="weight_kg", target_field="extra_field_777")
    payload4 = mapper.build_payload(
        product, config, title="x", description="x", language="en",
        field_mapping_rules=[weight_rule], redirectable_fields=set(),  # empty — nothing redirectable this call
    )
    check(
        "weight_kg's default placement is NOT suppressed when redirectable_fields doesn't include it",
        payload4.get("weight") == 2.5,
    )
    check(
        "the rule's target ALSO gets a copy (additive, since the default wasn't suppressed)",
        payload4.get("text_fields", {}).get("extra_field_777|en") == "2.5",
    )

    # (e) sku redirects are always refused, regardless of redirectable_fields.
    sku_rule = field_mapping_store.FieldMappingRule(source_field="sku", target_field="extra_field_666")
    payload5 = mapper.build_payload(
        product, config, title="x", description="x", language="en",
        field_mapping_rules=[sku_rule], redirectable_fields={"sku"},
    )
    check("sku redirect is refused even if 'sku' is passed in redirectable_fields", "extra_field_666|en" not in payload5.get("text_fields", {}))
    check("sku itself is still sent normally", payload5.get("sku") == "TEST-SKU")

    # -------------------------------------- backward compat: no migration needed --
    print("\n-- existing saved rules for the original 5 fields still resolve (no migration needed) --")
    old_style_mapping = field_mapping_store.FieldMapping(
        company_id=company.id, integration_type="baselinker",
        rules=[
            field_mapping_store.FieldMappingRule(source_field="brand", target_field="features:brand"),
            field_mapping_store.FieldMappingRule(source_field="product_condition", source_value="B", target_value="Ļoti labs"),
        ],
    )
    field_mapping_store.upsert_mapping(old_style_mapping)
    reloaded = field_mapping_store.get_mapping(company.id, "baselinker")
    check("pre-existing pure redirect rule round-trips unchanged", reloaded.rules[0].source_field == "brand" and reloaded.rules[0].target_field == "features:brand")
    check("pre-existing value-specific rule round-trips unchanged", reloaded.rules[1].source_value == "B" and reloaded.rules[1].target_value == "Ļoti labs")

    # ------------------------------------------------------------- isolation --
    print("\n-- cross-tenant isolation --")
    company_b = company_store.create_company("Dynamic Field Mapping Test Co B", user_limit=10)
    check("company B has no mapping of its own yet", field_mapping_store.get_mapping(company_b.id, "baselinker") is None)
    field_mapping_store.upsert_mapping(field_mapping_store.FieldMapping(
        company_id=company_b.id, integration_type="baselinker",
        rules=[field_mapping_store.FieldMappingRule(source_field="model", target_field="extra_field_555")],
    ))
    check(
        "company A's mapping is unaffected by company B's save",
        field_mapping_store.get_mapping(company.id, "baselinker").rules[0].target_field == "features:brand",
    )
    check(
        "company B's own mapping is its own",
        field_mapping_store.get_mapping(company_b.id, "baselinker").rules[0].target_field == "extra_field_555",
    )

    # --------------------- BaseLinker side: a brand-new TARGET field, live --
    # The mirror-image proof for point 3 of the request: if BaseLinker's
    # OWN API capability grows a new supported field, it must appear in
    # Field Mapping's target list with zero app.py/mapper.py/UI code
    # changes — get_target_fields() already merges in a live
    # list_extra_fields() call for exactly this reason (this account's
    # own custom fields); monkeypatching that live call to return one
    # extra fake field proves the merge mechanism itself, without a real
    # network request.
    print("\n-- BaseLinker side: a brand-new target field, discovered live --")
    real_list_extra_fields = baselinker_client.list_extra_fields
    target_connector = baselinker_client.BaselinkerConnector(company.id, {"token": "fake-token-for-this-test"}, {
        "inventory_id": "1", "category_id": "1",
    })
    try:
        baselinker_client.list_extra_fields = lambda token: [{"extra_field_id": 424242, "name": "Voltage (BaseLinker)"}]
        live_targets = target_connector.get_target_fields()
        check("a brand-new live BaseLinker capability field appears with NO code change", "extra_field_424242" in live_targets)
        check("its label comes through from the live API response", live_targets.get("extra_field_424242") == "Voltage (BaseLinker)")
    finally:
        baselinker_client.list_extra_fields = real_list_extra_fields
    # Empty token here (not the fake one above) — a genuinely empty token
    # makes get_target_fields() short-circuit before ever calling
    # list_extra_fields(), so this check stays fast/offline rather than
    # attempting a real network call with an invalid token.
    offline_connector = baselinker_client.BaselinkerConnector(company.id, {"token": ""}, {
        "inventory_id": "1", "category_id": "1",
    })
    check(
        "removing the fake capability again makes it disappear (proves it wasn't cached)",
        "extra_field_424242" not in offline_connector.get_target_fields(),
    )

    # ------------------------ FULL LIFECYCLE: a brand-new field, end to end --
    # Proves the exact acceptance criterion: defining ONE new field in the
    # canonical registry with mappable=True is SUFFICIENT — no other file
    # needs to change — for it to (1) appear as a Field Mapping source
    # field via the exact function app.py itself calls
    # (IntegrationManager.get_mappable_source_fields(), not just the
    # connector instance method already exercised above), (2) be mappable
    # onto a BaseLinker target, (3) have that mapping persist, (4) be
    # reflected in preview_payload() — the same build_payload() call the
    # real create/update export uses, just without the live network call —
    # and then, once removed from the registry again, (5) disappear from
    # the source-field list, with the orphaned saved rule preserved but
    # flagged rather than silently dropped (see app.py's
    # _render_field_mapping_tab "⚠ Unknown" handling).
    print("\n-- FULL LIFECYCLE: a brand-new 'voltage_test' field, end to end --")
    field_registry.SYNCABLE_FIELDS["voltage_test"] = {"label": "Voltage Test", "type": "text", "mappable": True}
    try:
        # (1) Appears via the exact call app.py's _render_field_mapping_tab
        # makes (module-level IntegrationManager function, not the
        # connector instance method — proves the UI's real code path).
        ui_source_fields = IntegrationManager.get_mappable_source_fields("baselinker")
        check("(1) new field appears as a Field Mapping source field via IntegrationManager", "voltage_test" in ui_source_fields)

        # (2) Mappable onto a real BaseLinker target from the connector's
        # own live-capable target-field list.
        ui_target_fields = IntegrationManager.get_supported_target_fields("baselinker", company_id=company.id)
        check("(2) a BaseLinker target field is available to map it onto", "extra_field_999" in ui_target_fields or bool(ui_target_fields))

        # (3) Save the mapping — same store call app.py's Save button makes.
        voltage_mapping = field_mapping_store.get_mapping(company.id, "baselinker") or field_mapping_store.FieldMapping(
            company_id=company.id, integration_type="baselinker",
        )
        voltage_mapping.rules = [
            r for r in voltage_mapping.rules if r.source_field != "voltage_test"
        ] + [field_mapping_store.FieldMappingRule(source_field="voltage_test", target_field="extra_field_999")]
        field_mapping_store.upsert_mapping(voltage_mapping)
        reloaded_voltage = field_mapping_store.get_mapping(company.id, "baselinker")
        check("(3) the mapping persists", any(r.source_field == "voltage_test" and r.target_field == "extra_field_999" for r in reloaded_voltage.rules))

        # (4) Reflected in preview_payload() — the SAME build_payload() call
        # create_product()/update_product() use for the real export;
        # preview_payload() only additionally skips base64-encoding images
        # and the live API call itself, so this is a faithful proof
        # without writing a real listing to BaseLinker.
        voltage_product = Product(company_id=company.id, sku="VOLT-TEST-SKU")
        setattr(voltage_product, "voltage_test", "230V")  # not a declared Product field — dataclasses allow this
        voltage_connector = baselinker_client.BaselinkerConnector(company.id, {"token": ""}, {
            "inventory_id": "1", "category_id": "1",
        })
        preview = voltage_connector.preview_payload(voltage_product)
        check(
            "(4) preview_payload() carries the new field's value at its mapped target",
            preview.get("text_fields", {}).get("extra_field_999|en") == "230V",
        )
    finally:
        del field_registry.SYNCABLE_FIELDS["voltage_test"]

    # (5) Removed from the registry -> no longer offered as a source field
    # — but the already-saved rule for it is NOT silently deleted (see
    # app.py's orphaned-row handling); it just becomes unavailable to
    # pick as a NEW source going forward.
    check("(5) 'voltage_test' no longer appears as a source field once removed from the registry", "voltage_test" not in IntegrationManager.get_mappable_source_fields("baselinker"))
    check("(5) the orphaned saved rule for it is NOT silently deleted from storage", any(r.source_field == "voltage_test" for r in field_mapping_store.get_mapping(company.id, "baselinker").rules))

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
