"""Proves — not just assumes — that one company's session can never read or
write another company's data through any public store-module function.

This is the real safety net for the whole multi-tenant design: SQLite has
no FK constraints anywhere in this codebase's convention, so nothing in the
schema itself stops a future function from being added without a
`company_id` filter. Re-run this any time a new store function is added,
not just once.

Runs against a throwaway scratch PostgreSQL database (via the
ELECTROGRADER_DATABASE_URL env var override in modules/db.py), set *before*
importing any modules.*_store module — modules/db.py's connection pool is
bound at first use, so setting the env var after import would silently
miss it.

    python scripts/verify_tenant_isolation.py
"""
import copy
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("isolation")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

import psycopg  # noqa: E402

from modules import auth, auth_store, company_store, inventory_store  # noqa: E402
from modules import manifest_store, marketplace_store, repair_store  # noqa: E402
from modules import integration_store, product_translation_store  # noqa: E402
from modules.models import Product  # noqa: E402
from integrations import manager as integration_manager  # noqa: E402

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

    # ---------------------------------------------------------- companies --
    print("-- companies & users --")
    company_a = company_store.create_company("Company A", user_limit=10)
    company_b = company_store.create_company("Company B", user_limit=10)

    admin_a = auth.register_user(company_a.id, "Admin A", "admin@companya.test", "PasswordA1!", role=auth.ROLE_ADMIN)
    admin_b = auth.register_user(company_b.id, "Admin B", "admin@companyb.test", "PasswordB1!", role=auth.ROLE_ADMIN)

    login_a = auth.verify_login("admin@companya.test", "PasswordA1!")
    login_b = auth.verify_login("admin@companyb.test", "PasswordB1!")
    check("login A resolves to company A", login_a is not None and login_a.company_id == company_a.id)
    check("login B resolves to company B", login_b is not None and login_b.company_id == company_b.id)
    check("login A with B's password fails", auth.verify_login("admin@companya.test", "PasswordB1!") is None)

    token_a = auth.create_session(admin_a.id)
    token_b = auth.create_session(admin_b.id)
    resolved_a = auth.validate_session(token_a)
    resolved_b = auth.validate_session(token_b)
    check("session A resolves to admin A only", resolved_a is not None and resolved_a.id == admin_a.id)
    check("session B resolves to admin B only", resolved_b is not None and resolved_b.id == admin_b.id)
    check("session A does not resolve to admin B", resolved_a is None or resolved_a.id != admin_b.id)

    print("\n-- auth_store: user updates must stay company-scoped --")
    forged = copy.deepcopy(admin_a)
    forged.company_id = company_b.id  # as if a caller mixed up which company this row belongs to
    forged.name = "Forged Name"
    auth_store.update_user(forged)
    check(
        "update_user(A's user id, wrong company=B) does not rename A's account",
        auth_store.get_user_by_id(admin_a.id).name == "Admin A",
    )
    legit = copy.deepcopy(admin_a)
    legit.name = "Admin A Renamed"
    auth_store.update_user(legit)
    check(
        "update_user(A's user id, correct company=A) does apply",
        auth_store.get_user_by_id(admin_a.id).name == "Admin A Renamed",
    )

    # -------------------------------------------------------- seed data --
    print("\n-- seeding one row per tenant-scoped table, per company --")
    product_a = Product(company_id=company_a.id, sku="A-SKU-1", name="Product A")
    product_b = Product(company_id=company_b.id, sku="B-SKU-1", name="Product B")
    inventory_store.save_product(product_a)
    inventory_store.save_product(product_b)

    batch_a = manifest_store.ManifestBatch(company_id=company_a.id, filename="a.csv")
    batch_b = manifest_store.ManifestBatch(company_id=company_b.id, filename="b.csv")
    manifest_store.save_batch(batch_a)
    manifest_store.save_batch(batch_b)

    listing_a = marketplace_store.MarketplaceListing(
        product_id=product_a.id, marketplace="baselinker", company_id=company_a.id,
        external_listing_id="ext-a", status=marketplace_store.STATUS_LISTED,
    )
    listing_b = marketplace_store.MarketplaceListing(
        product_id=product_b.id, marketplace="baselinker", company_id=company_b.id,
        external_listing_id="ext-b", status=marketplace_store.STATUS_LISTED,
    )
    marketplace_store.upsert_listing(listing_a)
    marketplace_store.upsert_listing(listing_b)

    repair_a = repair_store.RepairEvent(product_id=product_a.id, company_id=company_a.id, description="fix A", cost=10.0)
    repair_b = repair_store.RepairEvent(product_id=product_b.id, company_id=company_b.id, description="fix B", cost=20.0)
    repair_store.add_repair_event(repair_a)
    repair_store.add_repair_event(repair_b)

    translation_a = product_translation_store.ProductTranslation(
        product_id=product_a.id, company_id=company_a.id, language="en", title="Product A EN",
    )
    translation_b = product_translation_store.ProductTranslation(
        product_id=product_b.id, company_id=company_b.id, language="en", title="Product B EN",
    )
    product_translation_store.upsert_translation(translation_a)
    product_translation_store.upsert_translation(translation_b)

    # Dedicated draft products linked to each company's manifest batch, used
    # only by the delete_products_by_manifest cross-tenant test below — kept
    # separate from product_a/product_b so this doesn't change what any
    # existing check above is exercising.
    draft_a = Product(company_id=company_a.id, sku="A-DRAFT-1", name="Draft A", manifest_import_id=batch_a.id, status="draft")
    draft_b = Product(company_id=company_b.id, sku="B-DRAFT-1", name="Draft B", manifest_import_id=batch_b.id, status="draft")
    inventory_store.save_product(draft_a)
    inventory_store.save_product(draft_b)
    print("  seeded.")

    # ------------------------------------------- cross-tenant access checks --
    print("\n-- inventory_store: cross-tenant access must be impossible --")
    check(
        "get_product(A's id, company=B) -> None despite the row existing",
        inventory_store.get_product(product_a.id, company_b.id) is None,
    )
    check(
        "get_product(A's id, company=A) -> found",
        inventory_store.get_product(product_a.id, company_a.id) is not None,
    )
    inventory_store.delete_product(product_a.id, company_b.id)
    check(
        "delete_product(A's id, company=B) does not delete it",
        inventory_store.get_product(product_a.id, company_a.id) is not None,
    )
    check(
        "list_products(company=B) never contains A's product",
        product_a.id not in {p.id for p in inventory_store.list_products(company_b.id)},
    )
    check(
        "search_products('Product A', company=B) finds nothing",
        len(inventory_store.search_products("Product A", company_b.id)) == 0,
    )
    check(
        "search_products('Product A', company=A) finds it",
        any(p.id == product_a.id for p, _ in inventory_store.search_products("Product A", company_a.id)),
    )
    check(
        "list_products_by_manifest(A's batch, company=B) empty",
        len(inventory_store.list_products_by_manifest(batch_a.id, company_b.id)) == 0,
    )
    skus_b = inventory_store.next_sku_batch(1, company_b.id)
    check("next_sku_batch(company=B) doesn't error / returns a value", len(skus_b) == 1)
    products_b, total_b = inventory_store.list_products_paginated(company_b.id)
    check(
        "list_products_paginated(company=B) never contains A's product",
        product_a.id not in {p.id for p in products_b},
    )
    check(
        "delete_product(A's id, company=B) does not cascade-delete A's translation row",
        product_translation_store.get_translation(product_a.id, "en") is not None,
    )
    deleted_count = inventory_store.delete_products_by_manifest(batch_a.id, company_b.id)
    check(
        "delete_products_by_manifest(A's batch, company=B) deletes nothing",
        deleted_count == 0 and inventory_store.get_product(draft_a.id, company_a.id) is not None,
    )

    print("\n-- product_translation_store: cross-tenant access must be impossible --")
    check(
        "get_translation(A's product) still returns A's own title",
        product_translation_store.get_translation(product_a.id, "en").title == "Product A EN",
    )
    check(
        "get_translation(B's product) still returns B's own title, not A's",
        product_translation_store.get_translation(product_b.id, "en").title == "Product B EN",
    )
    product_translation_store.delete_translation(product_a.id, company_b.id, "en")
    check(
        "delete_translation(A's product, company=B) does not delete it",
        product_translation_store.get_translation(product_a.id, "en") is not None,
    )
    product_translation_store.delete_translations_for_product(product_a.id, company_b.id)
    check(
        "delete_translations_for_product(A's product, company=B) does not delete it",
        product_translation_store.get_translation(product_a.id, "en") is not None,
    )

    print("\n-- manifest_store: cross-tenant access must be impossible --")
    check(
        "get_batch(A's id, company=B) -> None despite the row existing",
        manifest_store.get_batch(batch_a.id, company_b.id) is None,
    )
    check(
        "get_batch(A's id, company=A) -> found",
        manifest_store.get_batch(batch_a.id, company_a.id) is not None,
    )
    check(
        "list_batches(company=B) never contains A's batch",
        batch_a.id not in {b.id for b in manifest_store.list_batches(company_b.id)},
    )
    manifest_store.delete_batch(batch_a.id, company_b.id)
    check(
        "delete_batch(A's id, company=B) does not delete it",
        manifest_store.get_batch(batch_a.id, company_a.id) is not None,
    )

    print("\n-- marketplace_store: cross-tenant access must be impossible --")
    check(
        "get_listing(A's product, company=B) -> None despite the row existing",
        marketplace_store.get_listing(product_a.id, "baselinker", company_b.id) is None,
    )
    check(
        "get_listing(A's product, company=A) -> found",
        marketplace_store.get_listing(product_a.id, "baselinker", company_a.id) is not None,
    )
    check(
        "list_listings(A's product, company=B) empty",
        len(marketplace_store.list_listings(product_a.id, company_b.id)) == 0,
    )
    marketplace_store.delete_listings_for_product(product_a.id, company_b.id)
    check(
        "delete_listings_for_product(A's product, company=B) does not delete it",
        marketplace_store.get_listing(product_a.id, "baselinker", company_a.id) is not None,
    )

    print("\n-- repair_store: cross-tenant access must be impossible --")
    check(
        "list_repair_events(A's product, company=B) empty despite the row existing",
        len(repair_store.list_repair_events(product_a.id, company_b.id)) == 0,
    )
    check(
        "list_repair_events(A's product, company=A) -> found",
        len(repair_store.list_repair_events(product_a.id, company_a.id)) == 1,
    )
    check(
        "total_repair_cost(A's product, company=B) == 0 despite A having real cost",
        repair_store.total_repair_cost(product_a.id, company_b.id) == 0.0,
    )
    check(
        "total_repair_cost(A's product, company=A) == 10.0",
        repair_store.total_repair_cost(product_a.id, company_a.id) == 10.0,
    )
    repair_store.delete_repair_event(repair_a.id, company_b.id)
    check(
        "delete_repair_event(A's event, company=B) does not delete it",
        len(repair_store.list_repair_events(product_a.id, company_a.id)) == 1,
    )
    repair_store.delete_repair_events_for_product(product_a.id, company_b.id)
    check(
        "delete_repair_events_for_product(A's product, company=B) does not delete it",
        len(repair_store.list_repair_events(product_a.id, company_a.id)) == 1,
    )

    print("\n-- integration_store: cross-tenant access must be impossible --")
    secret_a, secret_b = "SUPER-SECRET-TOKEN-A", "SUPER-SECRET-TOKEN-B"
    integration_a = integration_store.CompanyIntegration(
        company_id=company_a.id, integration_type="baselinker",
        integration_category=integration_store.CATEGORY_MARKETPLACE,
        status=integration_store.STATUS_CONNECTED,
        credentials={"token": secret_a}, settings={"inventory_id": "1"},
    )
    integration_b = integration_store.CompanyIntegration(
        company_id=company_b.id, integration_type="baselinker",
        integration_category=integration_store.CATEGORY_MARKETPLACE,
        status=integration_store.STATUS_CONNECTED,
        credentials={"token": secret_b}, settings={"inventory_id": "2"},
    )
    integration_store.upsert_integration(integration_a)
    integration_store.upsert_integration(integration_b)
    integration_store.record_sync(company_a.id, "baselinker", "export", product_id=product_a.id, status="success")
    integration_store.record_sync(company_b.id, "baselinker", "export", product_id=product_b.id, status="success")

    check(
        "get_integration(company=A) returns A's own credentials, not B's",
        integration_store.get_integration(company_a.id, "baselinker").credentials.get("token") == secret_a,
    )
    check(
        "get_integration(company=B) returns B's own credentials, not A's",
        integration_store.get_integration(company_b.id, "baselinker").credentials.get("token") == secret_b,
    )
    check(
        "list_integrations(company=B) never contains A's row",
        all(rec.company_id == company_b.id for rec in integration_store.list_integrations(company_b.id)),
    )
    check(
        "list_sync_log(company=B) never contains A's product_id",
        product_a.id not in {e.product_id for e in integration_store.list_sync_log(company_b.id)},
    )
    check(
        "list_sync_log(company=A) contains A's product_id",
        product_a.id in {e.product_id for e in integration_store.list_sync_log(company_a.id)},
    )
    integration_store.disconnect_integration(company_b.id, "baselinker")
    check(
        "disconnect_integration(company=B) does not affect A's connection",
        integration_store.get_integration(company_a.id, "baselinker").status == integration_store.STATUS_CONNECTED,
    )
    check(
        "disconnect_integration(company=B) clears only B's credentials",
        integration_store.get_integration(company_b.id, "baselinker").credentials == {},
    )

    print("\n-- integrations.manager: connector resolution must stay company-scoped --")
    try:
        integration_manager.get(company_b.id, "baselinker")
        manager_get_b_raised = False
    except integration_manager.IntegrationNotConnectedError:
        manager_get_b_raised = True
    check(
        "manager.get(company=B, 'baselinker') raises IntegrationNotConnectedError after B disconnected",
        manager_get_b_raised,
    )
    connector_a = integration_manager.get(company_a.id, "baselinker")
    check(
        "manager.get(company=A, 'baselinker') still resolves A's own connector with A's credentials",
        connector_a.credentials.get("token") == secret_a,
    )

    print("\n-- integration_store: credentials must be encrypted at rest, not just query-filtered --")
    raw_conn = psycopg.connect(_DATABASE_URL)
    raw_rows = raw_conn.execute("SELECT company_id, credentials FROM company_integrations").fetchall()
    raw_conn.close()
    check("at least one company_integrations row exists to check", len(raw_rows) > 0)
    check(
        "raw credentials column never contains company A's plaintext token",
        all(secret_a not in (row[1] or "") for row in raw_rows),
    )
    check(
        "raw credentials column never contains company B's plaintext token",
        all(secret_b not in (row[1] or "") for row in raw_rows),
    )

    # ------------------------------------------------------ rate limiting --
    print("\n-- login rate limiting: repeated failures lock out only the targeted account --")
    rl_user = auth.register_user(company_a.id, "RL Victim", "victim@companya.test", "CorrectPass1!", role=auth.ROLE_EMPLOYEE)
    for _ in range(auth.LOCKOUT_THRESHOLD):
        auth.verify_login("victim@companya.test", "WrongPassword!")
    check(
        f"{auth.LOCKOUT_THRESHOLD} wrong passwords locks out the account",
        auth.get_lockout_remaining_seconds("victim@companya.test") is not None,
    )
    check(
        "locked account rejects even the correct password",
        auth.verify_login("victim@companya.test", "CorrectPass1!") is None,
    )
    check(
        "a different company's unrelated account is unaffected by A's lockout",
        auth.verify_login("admin@companyb.test", "PasswordB1!") is not None,
    )

    locked_row = auth_store.get_user_by_email(company_a.id, "victim@companya.test")
    locked_row.locked_until = time.time() - 1  # simulate the lockout window having elapsed
    auth_store.update_user(locked_row)
    unlocked_login = auth.verify_login("victim@companya.test", "CorrectPass1!")
    check(
        "once the lockout window elapses, the correct password succeeds again",
        unlocked_login is not None and unlocked_login.id == rl_user.id,
    )
    check(
        "a successful login resets the failure counter",
        auth_store.get_user_by_id(rl_user.id).failed_login_attempts == 0,
    )

    # --------------------------------------------------- company suspension --
    print("\n-- company suspension revokes access for ALL its users --")
    company_a.status = company_store.STATUS_SUSPENDED
    company_store.update_company(company_a)
    check("suspended company's session is invalidated", auth.validate_session(token_a) is None)
    check("suspended company's login is rejected", auth.verify_login("admin@companya.test", "PasswordA1!") is None)
    check("company B is unaffected", auth.validate_session(token_b) is not None)
    company_a.status = company_store.STATUS_ACTIVE
    company_store.update_company(company_a)

    # ------------------------------------------------ photo folder scoping --
    print("\n-- app.py: uploaded-photo folder paths must be scoped by company_id (static check) --")
    # app.py can't be safely imported outside a live Streamlit ScriptRunContext
    # (it renders UI at module scope and reads st.session_state on import), so
    # this is a source-level regression check rather than a dynamic one: it
    # fails loudly if a future edit removes company_id from any of the three
    # places a product's upload folder is built, re-introducing the
    # cross-tenant photo-folder-collision bug fixed in this phase (two
    # different companies' auto-numbered SKUs both start at 2000, so without
    # this fix they resolved to the exact same data/uploads/<sku>/ folder,
    # silently colliding — and overwriting each other's photo — when brand
    # and model also matched).
    _app_py_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    with open(_app_py_path, "r", encoding="utf-8") as f:
        _app_source_lines = f.read().splitlines()
    _item_dir_lines = [l for l in _app_source_lines if "UPLOAD_DIR /" in l and "sku_folder_name(" in l]
    check("found the expected number of UPLOAD_DIR item_dir call sites", len(_item_dir_lines) == 3)
    check(
        "every UPLOAD_DIR item_dir call site scopes the path by company_id",
        all(".company_id" in l for l in _item_dir_lines),
    )

    # ------------------------------------------------------------- summary --
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
