"""Proves the manual "Check product connection" feature end to end at the
data/orchestration layer (see scripts/verify_product_connection_ui_e2e.py
for the dialog itself).

Covers, in the order of the approved spec:
  - the four distinct check outcomes — SUCCESS / NOT_FOUND /
    TEMPORARY_ERROR / AUTH_ERROR — and specifically that a timeout, a
    network failure or an HTTP 5xx is NEVER reported as NOT_FOUND;
  - NOT_FOUND does not delete the association (only the user does);
  - a successful check stamps last_verified_at; a failed one leaves the
    previous value alone;
  - the per-company daily quota: enforced in the backend, shared across
    ALL integrations rather than per-integration, isolated between
    companies, not bypassable by concurrent requests, and — once
    exhausted — rejecting the request WITHOUT calling the marketplace;
  - removing an integration unlinks exactly one (product, marketplace)
    pair and leaves the product, the other listings and the company's
    integration config intact;
  - multi-tenant isolation on every operation, including that another
    company's product_id can neither be checked nor spend this company's
    quota.

Every marketplace call is a monkeypatched fake — this script never
touches a real API. A counter proves when a call was and wasn't made.

Runs against a throwaway scratch PostgreSQL database, set *before*
importing any modules.*_store module — same convention as every other
verify_*.py script.

    python scripts/verify_product_connection_check.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("product_connection_check")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

import requests  # noqa: E402

from integrations import base as integration_base, manager  # noqa: E402
from integrations.marketplaces.baselinker import client as baselinker_client  # noqa: E402
from modules import (  # noqa: E402
    company_store,
    integration_check_quota_store,
    integration_store,
    inventory_store,
    marketplace_store,
)
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


# --------------------------------------------------------------- fixtures --

class _ApiCounter:
    def __init__(self):
        self.calls = 0

    def reset(self):
        self.calls = 0


_api = _ApiCounter()


def _install_fake_api(behaviour):
    """Replaces the ONE real network function the BaseLinker connector's
    check goes through. `behaviour` receives the requested ids and either
    returns a products dict or raises."""

    def fake_get_product_data(product_ids, config):
        _api.calls += 1
        return behaviour(product_ids)

    baselinker_client.get_product_data = fake_get_product_data


_real_get_product_data = baselinker_client.get_product_data


def _connect_baselinker(company_id: str) -> None:
    """Registers a connected BaseLinker integration for this company
    directly in the store, so manager.get() can build a connector without
    a real handshake."""
    integration_store.upsert_integration(integration_store.CompanyIntegration(
        company_id=company_id,
        integration_type="baselinker",
        integration_category=integration_store.CATEGORY_MARKETPLACE,
        status=integration_store.STATUS_CONNECTED,
        credentials={"token": "fake-token"},
        settings={"inventory_id": "1", "category_id": "1"},
    ))


def _seed_listing(product_id: str, company_id: str, marketplace: str, external_id: str) -> None:
    marketplace_store.upsert_listing(marketplace_store.MarketplaceListing(
        product_id=product_id, company_id=company_id, marketplace=marketplace,
        external_listing_id=external_id, status="listed",
    ))


def main() -> int:
    print(f"Scratch DB: {_DATABASE_URL}\n")

    company_a = company_store.create_company("Conn Check Co A", user_limit=10)
    company_b = company_store.create_company("Conn Check Co B", user_limit=10)
    _connect_baselinker(company_a.id)
    _connect_baselinker(company_b.id)

    pa = Product(company_id=company_a.id, status="completed", sku="CC-A", name="Checked Product A")
    inventory_store.save_product(pa)
    _seed_listing(pa.id, company_a.id, "baselinker", "555001")

    pb = Product(company_id=company_b.id, status="completed", sku="CC-B", name="Other Tenant Product")
    inventory_store.save_product(pb)
    _seed_listing(pb.id, company_b.id, "baselinker", "555002")

    p_unlinked = Product(company_id=company_a.id, status="completed", sku="CC-NONE", name="Not Integrated")
    inventory_store.save_product(p_unlinked)

    # ------------------------------------------------------- SUCCESS path --
    print("-- SUCCESS: external product exists --")
    _install_fake_api(lambda ids: {str(ids[0]): {"name": "still here"}})
    _api.reset()
    before_verified = marketplace_store.get_listing(pa.id, "baselinker", company_a.id).last_verified_at
    res = manager.check_product_connection(company_a.id, "baselinker", pa.id)
    check("status is SUCCESS", res.status == integration_base.CHECK_SUCCESS)
    check("exactly one API call was made", _api.calls == 1)
    check("last_verified_at was 0 before the check", before_verified == 0.0)
    after = marketplace_store.get_listing(pa.id, "baselinker", company_a.id)
    check("last_verified_at is now set", after.last_verified_at > 0)
    check("association still present", after is not None)

    # ------------------------------------------------------ NOT_FOUND path --
    print("\n-- NOT_FOUND: the API answers, product is genuinely gone --")
    verified_before_notfound = after.last_verified_at
    _install_fake_api(lambda ids: {})  # successful response, product absent
    _api.reset()
    res = manager.check_product_connection(company_a.id, "baselinker", pa.id)
    check("status is NOT_FOUND", res.status == integration_base.CHECK_NOT_FOUND)
    check("an API call was made", _api.calls == 1)
    still = marketplace_store.get_listing(pa.id, "baselinker", company_a.id)
    check("association was NOT auto-deleted", still is not None)
    check("external id untouched", still.external_listing_id == "555001")
    check(
        "last_verified_at NOT overwritten by a failed check",
        still.last_verified_at == verified_before_notfound,
    )

    # ------------------------------------------- TEMPORARY_ERROR variants --
    print("\n-- TEMPORARY_ERROR: never mistaken for NOT_FOUND --")

    def _raise(exc):
        def _b(ids):
            raise exc
        return _b

    for label, exc in (
        ("timeout", requests.Timeout("timed out")),
        ("network/connection error", requests.ConnectionError("no route to host")),
        ("HTTP 5xx", requests.HTTPError("500 Server Error")),
    ):
        _install_fake_api(_raise(exc))
        res = manager.check_product_connection(company_a.id, "baselinker", pa.id)
        check(f"{label} -> TEMPORARY_ERROR (not NOT_FOUND)", res.status == integration_base.CHECK_TEMPORARY_ERROR)

    _install_fake_api(_raise(baselinker_client.BaseLinkerAPIError("getInventoryProductsData failed: server busy", code="ERROR_UNKNOWN")))
    res = manager.check_product_connection(company_a.id, "baselinker", pa.id)
    check("non-auth API error -> TEMPORARY_ERROR", res.status == integration_base.CHECK_TEMPORARY_ERROR)
    check(
        "association survives every temporary failure",
        marketplace_store.get_listing(pa.id, "baselinker", company_a.id) is not None,
    )

    # --------------------------------------------------- AUTHENTICATION --
    print("\n-- AUTH_ERROR: kept distinct from both of the above --")
    _install_fake_api(_raise(baselinker_client.BaseLinkerAPIError(
        "getInventoryProductsData failed: invalid token", code="ERROR_AUTH_TOKEN")))
    res = manager.check_product_connection(company_a.id, "baselinker", pa.id)
    check("auth failure -> AUTH_ERROR", res.status == integration_base.CHECK_AUTH_ERROR)
    check(
        "association survives an auth failure",
        marketplace_store.get_listing(pa.id, "baselinker", company_a.id) is not None,
    )

    # ------------------------------------------------------ not linked --
    print("\n-- a product with no listing --")
    _api.reset()
    res = manager.check_product_connection(company_a.id, "baselinker", p_unlinked.id)
    check("unlinked product -> NOT_LINKED", res.status == manager.CHECK_NOT_LINKED)
    check("no API call was made for an unlinked product", _api.calls == 0)

    # ------------------------------------------------ tenant isolation --
    print("\n-- multi-tenant isolation --")
    _api.reset()
    res = manager.check_product_connection(company_a.id, "baselinker", pb.id)
    check("company A cannot check company B's product", res.status == manager.CHECK_NOT_LINKED)
    check("no API call was spent on another tenant's product", _api.calls == 0)

    used_a, _ = integration_check_quota_store.get_usage(company_a.id)
    used_b, _ = integration_check_quota_store.get_usage(company_b.id)
    check("company B's quota untouched by company A's checks", used_b == 0)
    check("company A's own quota did advance", used_a > 0)

    print("\n-- a request that can never reach the marketplace costs no quota --")
    # A listing whose integration isn't connected/available in this build:
    # the connector is resolved before the quota is claimed, so the check
    # fails without spending one of the company's daily allowance.
    p_stale = Product(company_id=company_a.id, status="completed", sku="CC-STALE", name="Stale Integration")
    inventory_store.save_product(p_stale)
    _seed_listing(p_stale.id, company_a.id, "woocommerce", "888001")
    _api.reset()
    quota_before = integration_check_quota_store.get_usage(company_a.id)[0]
    res = manager.check_product_connection(company_a.id, "woocommerce", p_stale.id)
    quota_after = integration_check_quota_store.get_usage(company_a.id)[0]
    check("an unavailable integration returns a non-crashing result", res.status in (
        integration_base.CHECK_TEMPORARY_ERROR, integration_base.CHECK_AUTH_ERROR,
    ))
    check("it never reports NOT_FOUND", res.status != integration_base.CHECK_NOT_FOUND)
    check("no API call was made", _api.calls == 0)
    check("and no quota was consumed", quota_after == quota_before)
    check(
        "the association is left alone",
        marketplace_store.get_listing(p_stale.id, "woocommerce", company_a.id) is not None,
    )

    # ------------------------------------------------------ daily quota --
    print("\n-- daily quota: backend-enforced, company-wide, no API call once exhausted --")
    company_q = company_store.create_company("Conn Check Quota Co", user_limit=10)
    _connect_baselinker(company_q.id)
    limit = integration_check_quota_store.DAILY_PRODUCT_CONNECTION_CHECKS
    check("the limit constant is 100", limit == 100)

    pq = Product(company_id=company_q.id, status="completed", sku="CC-Q", name="Quota Product")
    inventory_store.save_product(pq)
    _seed_listing(pq.id, company_q.id, "baselinker", "777001")

    _install_fake_api(lambda ids: {str(ids[0]): {"name": "ok"}})
    _api.reset()
    for _ in range(limit):
        manager.check_product_connection(company_q.id, "baselinker", pq.id)
    used, _ = integration_check_quota_store.get_usage(company_q.id)
    check(f"exactly {limit} checks consumed the whole allowance", used == limit)

    # The pool is company-wide, not per-integration: the quota store is
    # keyed by company alone, so checks against different marketplaces
    # draw from the same allowance. Asserted directly on the store (the
    # only place that pooling exists) so it holds for every integration
    # added later, including ones with no connector in this build yet.
    company_pool = company_store.create_company("Conn Check Pool Co", user_limit=10)
    for _ in range(4):
        integration_check_quota_store.try_consume(company_pool.id)   # as if BaseLinker
    for _ in range(6):
        integration_check_quota_store.try_consume(company_pool.id)   # as if WooCommerce
    pooled_used, _ = integration_check_quota_store.get_usage(company_pool.id)
    check("checks across different integrations share ONE company-wide pool", pooled_used == 10)

    calls_before_rejection = _api.calls
    res = manager.check_product_connection(company_q.id, "baselinker", pq.id)
    check("the next check is rejected", res.status == manager.CHECK_LIMIT_REACHED)
    check("a rejected check makes NO marketplace API call", _api.calls == calls_before_rejection)
    used_after, _ = integration_check_quota_store.get_usage(company_q.id)
    check("a rejected check does not inflate the counter past the limit", used_after == limit)

    # ------------------------------------------- concurrency: no bypass --
    print("\n-- concurrent requests cannot exceed the limit --")
    company_c = company_store.create_company("Conn Check Concurrency Co", user_limit=10)
    granted = []
    lock = threading.Lock()

    def worker():
        allowed, _used, _limit = integration_check_quota_store.try_consume(company_c.id)
        with lock:
            granted.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(limit + 25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed_count = sum(1 for g in granted if g)
    used_c, _ = integration_check_quota_store.get_usage(company_c.id)
    check(f"{limit + 25} parallel claims granted exactly {limit}", allowed_count == limit)
    check("stored counter equals the limit, never above it", used_c == limit)

    # ------------------------------------------------- remove integration --
    print("\n-- remove integration --")
    _install_fake_api(lambda ids: {str(ids[0]): {"name": "ok"}})
    p_multi = Product(company_id=company_a.id, status="completed", sku="CC-MULTI", name="Multi Listed")
    inventory_store.save_product(p_multi)
    for mk, ext in (("baselinker", "9001"), ("ebay", "9002"), ("amazon", "9003")):
        _seed_listing(p_multi.id, company_a.id, mk, ext)

    removed = marketplace_store.delete_listing(p_multi.id, "ebay", company_a.id)
    remaining = {l.marketplace for l in marketplace_store.list_listings(p_multi.id, company_a.id)}
    check("delete_listing reports success", removed is True)
    check("only that one association is gone", remaining == {"baselinker", "amazon"})
    check("the product itself still exists", inventory_store.get_product(p_multi.id, company_a.id) is not None)
    check(
        "the company's integration config is untouched",
        integration_store.get_integration(company_a.id, "ebay") is None
        and integration_store.get_integration(company_a.id, "baselinker") is not None,
    )
    check(
        "company B cannot remove company A's association",
        marketplace_store.delete_listing(p_multi.id, "baselinker", company_b.id) is False,
    )
    check(
        "and it survived that attempt",
        marketplace_store.get_listing(p_multi.id, "baselinker", company_a.id) is not None,
    )

    # ---------------------------------- Product List render makes no calls --
    print("\n-- rendering the list makes no marketplace API calls --")
    _api.reset()
    page_ids = [pa.id, p_multi.id, p_unlinked.id]
    listings_by_product = marketplace_store.list_listings_for_products(page_ids, company_a.id)
    check("bulk listing fetch made zero API calls", _api.calls == 0)
    check("an integrated product yields its icons", len(listings_by_product.get(p_multi.id, [])) == 2)
    check("a non-integrated product yields none", p_unlinked.id not in listings_by_product)

    # -------------------------------- existing Integration Test untouched --
    print("\n-- the existing Integration Test still works --")
    check("manager.test is still callable", callable(manager.test))
    check(
        "connector still exposes test_connection()",
        callable(getattr(manager.get(company_a.id, "baselinker"), "test_connection", None)),
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
        baselinker_client.get_product_data = _real_get_product_data
        _drop_scratch_db()
    raise SystemExit(exit_code)
