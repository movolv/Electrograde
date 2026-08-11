"""Proves the real BaseLinker two-way sync implementation
(field_reader.py/field_writer.py/sync.py/change_detector.py/engine.py's
process_sync_queue()+pull_product()/scheduler.py's poll_two_way_sync_once())
works correctly end-to-end, WITHOUT any real network calls — BaseLinker's
HTTP layer (client._call) is monkeypatched so the real client/field_reader/
field_writer/sync.py/engine.py code actually executes against canned
responses, the same "mock at the transport boundary, not the business
logic" approach used by scripts/verify_sync_engine.py's fake connector,
just one layer lower since this pass tests the REAL connector's own code.

Runs against a throwaway scratch PostgreSQL database (ELECTROGRADER_DATABASE_URL), set
*before* importing any modules.*_store / integrations / sync module — same
convention as every other verify_*.py script in this repo.

    python scripts/verify_baselinker_realtime_sync.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("baselinker_realtime_sync")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import (  # noqa: E402
    company_store, inventory_store, integration_store, marketplace_store,
    product_change_log_store, sync_log_store, sync_ownership_store, sync_queue_store, sync_rules_store,
)
from modules.models import Product  # noqa: E402
from integrations.manager import IntegrationManager  # noqa: E402
from integrations.marketplaces.baselinker import client as bl_client  # noqa: E402
from sync import change_detector, conflict_resolver  # noqa: E402
from sync import engine as sync_engine  # noqa: E402
from integrations import scheduler  # noqa: E402

_checks_passed = 0
_failures = []


def check(label: str, condition: bool) -> None:
    global _checks_passed
    if condition:
        _checks_passed += 1
    else:
        _failures.append(label)
    print(("  OK  " if condition else "  FAIL"), label)


# ------------------------------------------------------------------ fake HTTP --
_FAKE_PULL_RESPONSE = {"12345": {"stock": {"1": 3}, "prices": {"1": 90.0}}}
_calls = []


def _fake_call(method, parameters, token):
    _calls.append((method, parameters))
    if method == "addInventoryProduct":
        return {"status": "SUCCESS", "product_id": parameters.get("product_id") or 12345, "warnings": {}}
    if method == "getInventoryProductsData":
        return {"status": "SUCCESS", "products": _FAKE_PULL_RESPONSE}
    raise AssertionError(f"Unexpected method in test: {method}")


bl_client._call = _fake_call


def main() -> int:
    print(f"Scratch DB: {_DATABASE_URL}\n")

    company = company_store.create_company("Realtime Sync Test Co", user_limit=10)
    company_b = company_store.create_company("Realtime Sync Test Co B", user_limit=10)

    integration_store.upsert_integration(
        integration_store.CompanyIntegration(
            company_id=company.id, integration_type="baselinker", integration_category="marketplace",
            status=integration_store.STATUS_CONNECTED,
            credentials={"token": "fake-token"},
            settings={"inventory_id": "1", "category_id": "1", "price_group_id": "1", "warehouse_id": "1"},
        )
    )

    product = Product(company_id=company.id, sku="TEST-SKU-1", name="Test Product", price=100.0, quantity=10)
    inventory_store.save_product(product)
    marketplace_store.upsert_listing(
        marketplace_store.MarketplaceListing(
            product_id=product.id, marketplace="baselinker", company_id=company.id,
            external_listing_id="12345", status=marketplace_store.STATUS_LISTED, price=product.price,
        )
    )

    # --------------------------------------------------------------------- Push --
    print("-- Push: local, electrograder-owned field change --")
    enqueued = change_detector.record_and_enqueue(
        company.id, product.id, "baselinker", "price", 100.0, 110.0,
        source_system=product_change_log_store.SOURCE_ELECTROGRADER, changed_by="user:tester",
    )
    check("price (electrograder-owned) change gets enqueued", enqueued is True)
    changes = product_change_log_store.list_changes(company.id, product.id)
    check("product_change_log recorded the price change", any(c.field_name == "price" for c in changes))
    check("product_change_log entry has source_system=electrograder", changes[0].source_system == "electrograder")

    queued = sync_queue_store.list_queue(company.id, product.id)
    check("sync_queue has one pending item for price", len([q for q in queued if q.field_name == "price"]) == 1)

    product.price = 110.0
    inventory_store.save_product(product)
    processed = sync_engine.process_sync_queue()
    check("process_sync_queue() processed the queued item", processed >= 1)

    queued_after = sync_queue_store.list_queue(company.id, product.id)
    price_item = next(q for q in queued_after if q.field_name == "price")
    check("sync_queue item ended success", price_item.status == sync_queue_store.STATUS_SUCCESS)
    check("addInventoryProduct was actually called with a price-scoped payload", any(
        m == "addInventoryProduct" and "prices" in p for m, p in _calls
    ))
    push_logs = sync_log_store.list_logs(company.id, product.id, connector_name="baselinker")
    check("sync_logs recorded the push", any(l.field_name == "price" and l.source == "electrograder" for l in push_logs))

    # ------------------------------------------------------- Push: non-owned field --
    print("\n-- Push: non-owned field change is logged but NOT enqueued --")
    enqueued_qty = change_detector.record_and_enqueue(
        company.id, product.id, "baselinker", "quantity", 10, 999,
        source_system=product_change_log_store.SOURCE_ELECTROGRADER, changed_by="user:tester",
    )
    check("quantity (baselinker-owned) local edit is NOT auto-enqueued", enqueued_qty is False)
    check(
        "...but IS still logged to product_change_log for visibility",
        any(c.field_name == "quantity" and c.new_value == "999" for c in product_change_log_store.list_changes(company.id, product.id)),
    )

    # --------------------------------------------------------------------- Pull --
    print("\n-- Pull: BaseLinker-owned quantity changes remotely --")
    product.quantity = 10  # reset (previous section didn't actually apply 999 — never owned by electrograder)
    inventory_store.save_product(product)
    resolutions = sync_engine.pull_product(company.id, product, "baselinker")
    by_field = {r.field_name: r for r in resolutions}

    check("pull_state returned quantity/price/status only", set(by_field) == {"quantity", "price", "status"})
    check("quantity (baselinker-owned): accepted", by_field["quantity"].resolution_action == conflict_resolver.ACTION_ACCEPTED)
    check("quantity (baselinker-owned): applied_value is the remote value", by_field["quantity"].applied_value == 3)

    reloaded = inventory_store.get_product(product.id, company.id)
    check("quantity was actually written back to the product", reloaded.quantity == 3)
    check(
        "product_change_log has the pull-sourced quantity change",
        any(c.field_name == "quantity" and c.source_system == "baselinker" and c.changed_by == "system:pull_sync"
            for c in product_change_log_store.list_changes(company.id, product.id)),
    )

    print("\n-- Pull: status is decided but NEVER written to Product.status --")
    check("status: accepted (baselinker owns it)", by_field["status"].resolution_action == conflict_resolver.ACTION_ACCEPTED)
    check(
        "Product.status (grading workflow field) untouched by pull",
        reloaded.status == product.status and reloaded.status != "active" and reloaded.status != "sold",
    )

    print("\n-- Pull: conflict on an electrograder-owned field (price) --")
    check("price: overridden (non-owner/BaseLinker changed it)", by_field["price"].resolution_action == conflict_resolver.ACTION_OVERRIDDEN)
    check("price: conflict_policy=electrograder means local value wins", by_field["price"].applied_value == 110.0)
    reloaded2 = inventory_store.get_product(product.id, company.id)
    check("Product.price was NOT overwritten by BaseLinker's 90.0", reloaded2.price == 110.0)

    # ------------------------------------------------------------- Pull safety --
    print("\n-- Pull safety: empty/missing remote values never overwrite valid data --")
    from integrations.marketplaces.baselinker import field_reader

    config = {"warehouse_id": "1", "price_group_id": "1"}
    empty_raw = {"stock": {"1": ""}, "prices": {"1": None}}
    result_empty = field_reader.read_remote_state(empty_raw, config)
    check("empty stock string is omitted, not applied as 0 or blank", "quantity" not in result_empty)
    check("None price is omitted", "price" not in result_empty)

    zero_raw = {"stock": {"1": 0}, "prices": {}}
    result_zero = field_reader.read_remote_state(zero_raw, config)
    check("quantity=0 IS treated as a real, meaningful value", result_zero.get("quantity") == 0)
    check("quantity=0 derives status=sold", result_zero.get("status") == "sold")

    # ----------------------------------------------------- auto_sync_enabled gate --
    print("\n-- auto_sync_enabled safety gate --")
    rule_off = sync_rules_store.get_rule(company.id, "baselinker") or sync_rules_store.SyncRule(
        company_id=company.id, integration_type="baselinker",
    )
    sync_rules_store.upsert_rule(rule_off)
    check("auto_sync_enabled defaults to False", rule_off.auto_sync_enabled is False)
    processed_off = scheduler.poll_two_way_sync_once()
    check("poll_two_way_sync_once() does nothing while auto_sync_enabled=False", processed_off == 0)

    rule_on = sync_rules_store.get_rule(company.id, "baselinker")
    rule_on.auto_sync_enabled = True
    rule_on.push_interval_seconds = 1
    rule_on.pull_interval_seconds = 1
    sync_rules_store.upsert_rule(rule_on)
    processed_on = scheduler.poll_two_way_sync_once()
    check("poll_two_way_sync_once() processes a company that explicitly opted in", processed_on > 0)

    # ------------------------------------------------------------------- isolation --
    print("\n-- cross-tenant isolation (sync_queue, product_change_log) --")
    sync_queue_store.enqueue(company_b.id, "prodB", "baselinker", "price", old_value="1", new_value="2")
    check("company A's queue unaffected by company B's item", len(sync_queue_store.list_queue(company.id)) >= 1)
    check("company B sees only its own queue item", len(sync_queue_store.list_queue(company_b.id)) == 1)
    product_change_log_store.record_change(
        company_b.id, "prodB", "price", "1", "2", source_system=product_change_log_store.SOURCE_ELECTROGRADER,
    )
    check("company A sees none of company B's change log", len(product_change_log_store.list_changes(company.id, "prodB")) == 0)
    check("company B sees its own change log", len(product_change_log_store.list_changes(company_b.id, "prodB")) == 1)

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
