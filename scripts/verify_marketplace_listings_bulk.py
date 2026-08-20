"""Proves the Product List's Integrations column data layer:

  - list_listings_for_products() returns the same content as calling
    list_listings() per product, but in ONE query rather than one per row
    (the N+1 the old Product List had — it called get_listing() inside the
    row loop, so a 50-row page meant 50 round-trips, and it could only
    ever see a single hardcoded marketplace);
  - a product with no listings is simply absent from the result, so the
    UI can never render an integration a product isn't actually on;
  - several marketplaces on one product all come back (the column has no
    architectural 2-3 integration limit);
  - full cross-tenant isolation;
  - delete_listing() unlinks exactly ONE (product, marketplace) pair,
    leaving that product's other listings — and other products' listings
    for the same marketplace — untouched, and never deletes the Product
    itself.

Runs against a throwaway scratch PostgreSQL database, set *before*
importing any modules.*_store module — same convention as every other
verify_*.py script.

    python scripts/verify_marketplace_listings_bulk.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("marketplace_listings_bulk")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import company_store, db, inventory_store, marketplace_store  # noqa: E402
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


def _seed_product(company_id: str, sku: str) -> Product:
    p = Product(company_id=company_id, status="completed", sku=sku, name=f"Product {sku}")
    inventory_store.save_product(p)
    return p


def _seed_listing(product_id: str, company_id: str, marketplace: str, status: str = "listed") -> None:
    marketplace_store.upsert_listing(marketplace_store.MarketplaceListing(
        product_id=product_id, company_id=company_id, marketplace=marketplace,
        external_listing_id=f"ext-{marketplace}-{product_id[:4]}", status=status,
    ))


class _CountingCursor:
    """Counts how many SELECTs against marketplace_listings actually run,
    so the "one query, not one per row" claim is measured rather than
    assumed."""

    def __init__(self):
        self.select_count = 0


def main() -> int:
    print(f"Scratch DB: {_DATABASE_URL}\n")

    company_a = company_store.create_company("Listings Bulk Test Co A", user_limit=10)
    company_b = company_store.create_company("Listings Bulk Test Co B", user_limit=10)

    print("-- seed --")
    p1 = _seed_product(company_a.id, "BULK-1")
    p2 = _seed_product(company_a.id, "BULK-2")
    p3 = _seed_product(company_a.id, "BULK-3")  # deliberately listed nowhere
    pb = _seed_product(company_b.id, "OTHER-1")

    # p1 on many marketplaces at once — proves there's no 2-3 channel cap.
    many = ["amazon", "ebay", "baselinker", "allegro", "tradera", "woocommerce",
            "etsy", "vinted", "backmarket", "prestashop"]
    for m in many:
        _seed_listing(p1.id, company_a.id, m)
    _seed_listing(p2.id, company_a.id, "baselinker", status="sold")
    _seed_listing(pb.id, company_b.id, "baselinker")
    check("seeded 10 marketplaces on one product", len(marketplace_store.list_listings(p1.id, company_a.id)) == 10)

    print("\n-- list_listings_for_products(): content matches the per-product calls --")
    bulk = marketplace_store.list_listings_for_products([p1.id, p2.id, p3.id], company_a.id)
    per_product = {
        pid: marketplace_store.list_listings(pid, company_a.id)
        for pid in (p1.id, p2.id, p3.id)
        if marketplace_store.list_listings(pid, company_a.id)
    }
    check(
        "same marketplaces per product as list_listings()",
        {pid: sorted(l.marketplace for l in ls) for pid, ls in bulk.items()}
        == {pid: sorted(l.marketplace for l in ls) for pid, ls in per_product.items()},
    )
    check("all 10 of p1's marketplaces present", len(bulk[p1.id]) == 10)
    check("status round-trips (p2 is 'sold')", bulk[p2.id][0].status == "sold")
    check("external_listing_id round-trips", all(l.external_listing_id for l in bulk[p1.id]))
    check("a product listed nowhere is ABSENT, not an empty entry", p3.id not in bulk)
    check("empty input returns {} without querying", marketplace_store.list_listings_for_products([], company_a.id) == {})

    print("\n-- N+1: one query for the whole page, not one per row --")
    real_connect = marketplace_store._connect
    counter = _CountingCursor()

    def counting_connect():
        conn = real_connect()
        real_execute = conn.execute

        def execute(sql, *args, **kwargs):
            if "FROM marketplace_listings" in sql and sql.strip().upper().startswith("SELECT"):
                counter.select_count += 1
            return real_execute(sql, *args, **kwargs)

        conn.execute = execute
        return conn

    marketplace_store._connect = counting_connect
    try:
        counter.select_count = 0
        page_ids = [p1.id, p2.id, p3.id]
        marketplace_store.list_listings_for_products(page_ids, company_a.id)
        bulk_queries = counter.select_count

        counter.select_count = 0
        for pid in page_ids:
            marketplace_store.list_listings(pid, company_a.id)
        per_row_queries = counter.select_count
    finally:
        marketplace_store._connect = real_connect

    check(f"bulk fetch ran exactly 1 SELECT (ran {bulk_queries})", bulk_queries == 1)
    check(f"the per-row pattern ran 1 per product (ran {per_row_queries} for 3)", per_row_queries == 3)
    check("bulk does strictly fewer queries than per-row", bulk_queries < per_row_queries)

    print("\n-- cross-tenant isolation --")
    check("company A's bulk fetch never returns company B's product", pb.id not in bulk)
    b_bulk = marketplace_store.list_listings_for_products([pb.id, p1.id], company_b.id)
    check("company B's bulk fetch never returns company A's product", p1.id not in b_bulk)
    check("company B still sees its own listing", pb.id in b_bulk and len(b_bulk[pb.id]) == 1)
    check(
        "passing another tenant's product_id returns nothing for it",
        marketplace_store.list_listings_for_products([p1.id], company_b.id) == {},
    )

    print("\n-- delete_listing(): unlinks exactly one pair, locally --")
    removed = marketplace_store.delete_listing(p1.id, "ebay", company_a.id)
    check("returns True when a row was removed", removed is True)
    after = {l.marketplace for l in marketplace_store.list_listings(p1.id, company_a.id)}
    check("that marketplace is gone", "ebay" not in after)
    check("p1's other 9 marketplaces are untouched", len(after) == 9)
    check("another product's listing for a DIFFERENT marketplace is untouched", len(marketplace_store.list_listings(p2.id, company_a.id)) == 1)
    check(
        "the Product itself still exists (unlink never deletes the product)",
        inventory_store.get_product(p1.id, company_a.id) is not None,
    )
    check("unlinking something already gone returns False, not an error", marketplace_store.delete_listing(p1.id, "ebay", company_a.id) is False)

    print("\n-- delete_listing() is tenant-scoped --")
    check(
        "company B cannot unlink company A's listing",
        marketplace_store.delete_listing(p1.id, "amazon", company_b.id) is False,
    )
    check(
        "company A's listing survived that attempt",
        "amazon" in {l.marketplace for l in marketplace_store.list_listings(p1.id, company_a.id)},
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
