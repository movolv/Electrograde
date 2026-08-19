"""Proves the Category Catalog (modules/category_store.py) works correctly:
  - create/rename/move/delete + find_or_create_by_name, with the exact
    normalized (never fuzzy) duplicate-name matching rules;
  - move_category rejects self-parenting and cycles;
  - delete_category is a REAL delete (not a soft is_active flip), blocked
    while products or subcategories are still attached;
  - move_products_and_delete_category moves every affected product's
    category_id AND display-text category, then deletes — guarded against
    destination_id == id and a nonexistent/foreign-company destination;
  - rename_category propagates the new name onto every product's
    product.category display copy;
  - full cross-tenant isolation;
  - count_products_using() accuracy against modules/inventory_store.py's
    denormalized category_id column;
  - manifest import (modules/manifest_import.py) never touches the
    Category Catalog anymore — only manifest_subcategory is captured;
  - promote_from_manifest_on_completion() — the sole remaining automatic
    catalog-write path, shared identically by both "product became
    completed" call sites (New Item wizard's Save Item, and the bulk
    Change Status dialog): off by default, only fires once completed,
    never overrides an existing category_id, never fires on a blank
    manifest_subcategory, and is idempotent across multiple products
    sharing the same subcategory text.

Runs against a throwaway scratch PostgreSQL database, set *before* importing
any modules.*_store module — same convention as scripts/verify_sync_ownership.py.

    python scripts/verify_category_store.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("category_store")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import category_store, company_store, inventory_store  # noqa: E402
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


def _make_product(company_id: str, category_id: str = "", category: str = "") -> Product:
    p = Product(company_id=company_id, sku=f"sku-{os.urandom(4).hex()}", category_id=category_id, category=category)
    inventory_store.save_product(p)
    return p


def main() -> int:
    print(f"Scratch DB: {_DATABASE_URL}\n")

    company_a = company_store.create_company("Category Store Test Co A", user_limit=10)
    company_b = company_store.create_company("Category Store Test Co B", user_limit=10)

    # ------------------------------------------------------- create/list --
    print("-- create root + subcategory --")
    kitchen = category_store.create_category(company_a.id, "Kitchen Appliances")
    coffee = category_store.create_category(company_a.id, "Coffee Machines", parent_id=kitchen.id)
    check("root category created with empty parent_id", kitchen.parent_id == "")
    check("subcategory created under the right parent", coffee.parent_id == kitchen.id)
    check("list_categories returns both", len(category_store.list_categories(company_a.id)) == 2)

    print("\n-- duplicate-name rejection (exact + normalized) --")
    try:
        category_store.create_category(company_a.id, "Kitchen Appliances")
        check("exact duplicate root name rejected", False)
    except ValueError:
        check("exact duplicate root name rejected", True)
    try:
        category_store.create_category(company_a.id, "  kitchen appliances  ")
        check("case/whitespace-normalized duplicate rejected", False)
    except ValueError:
        check("case/whitespace-normalized duplicate rejected", True)
    # Same name under a DIFFERENT parent is allowed.
    electronics = category_store.create_category(company_a.id, "Electronics")
    accessories_kitchen = category_store.create_category(company_a.id, "Accessories", parent_id=kitchen.id)
    accessories_electronics = category_store.create_category(company_a.id, "Accessories", parent_id=electronics.id)
    check(
        "same name allowed under two different parents",
        accessories_kitchen.id != accessories_electronics.id,
    )

    # ------------------------------------------------------------- move --
    print("\n-- move_category --")
    try:
        category_store.move_category(kitchen.id, company_a.id, kitchen.id)
        check("self-parent rejected", False)
    except ValueError:
        check("self-parent rejected", True)
    try:
        category_store.move_category(kitchen.id, company_a.id, coffee.id)
        check("cycle (move under own descendant) rejected", False)
    except ValueError:
        check("cycle (move under own descendant) rejected", True)
    moved = category_store.move_category(accessories_electronics.id, company_a.id, "")
    check("legit move to root succeeds", moved.parent_id == "")

    # --------------------------------------------------- find_or_create --
    print("\n-- find_or_create_by_name --")
    first = category_store.find_or_create_by_name(company_a.id, "Blenders")
    second = category_store.find_or_create_by_name(company_a.id, "  blenders  ")
    check("find_or_create_by_name is idempotent (normalized match)", first.id == second.id)
    check("find_or_create_by_name never creates a 3rd row", len(category_store.list_categories(company_a.id)) == 6)
    # An admin moves it under a parent — a later find_or_create with the
    # same name (root-scoped call) must reuse it, not spawn a root duplicate.
    category_store.move_category(first.id, company_a.id, kitchen.id)
    third = category_store.find_or_create_by_name(company_a.id, "Blenders")
    check("find_or_create_by_name still finds a since-moved category", third.id == first.id)

    # ------------------------------------------------ rename propagation --
    print("\n-- rename_category propagates to product.category --")
    p1 = _make_product(company_a.id, category_id=coffee.id, category=coffee.name)
    p2 = _make_product(company_a.id, category_id=coffee.id, category=coffee.name)
    category_store.rename_category(coffee.id, company_a.id, "Espresso Machines")
    reloaded1 = inventory_store.get_product(p1.id, company_a.id)
    reloaded2 = inventory_store.get_product(p2.id, company_a.id)
    check("product 1's display-text category updated by rename", reloaded1.category == "Espresso Machines")
    check("product 2's display-text category updated by rename", reloaded2.category == "Espresso Machines")
    check("product's category_id unchanged by rename", reloaded1.category_id == coffee.id)

    # ----------------------------------------------------- product count --
    print("\n-- count_products_using --")
    check("count_products_using reflects both products", category_store.count_products_using(coffee.id, company_a.id) == 2)
    check("count_products_using is 0 for an unused category", category_store.count_products_using(kitchen.id, company_a.id) == 0)

    # ------------------------------------------------------------ delete --
    print("\n-- delete_category --")
    try:
        category_store.delete_category(coffee.id, company_a.id)
        check("delete blocked while products are attached", False)
    except ValueError:
        check("delete blocked while products are attached", True)
    empty_cat = category_store.create_category(company_a.id, "Temp Empty Category")
    category_store.delete_category(empty_cat.id, company_a.id)
    check("delete succeeds for a 0-product category", category_store.get_category(empty_cat.id, company_a.id) is None)

    parent_with_child = category_store.create_category(company_a.id, "Has A Child")
    child = category_store.create_category(company_a.id, "The Child", parent_id=parent_with_child.id)
    try:
        category_store.delete_category(parent_with_child.id, company_a.id)
        check("delete blocked while a subcategory is attached", False)
    except ValueError:
        check("delete blocked while a subcategory is attached", True)
    category_store.delete_category(child.id, company_a.id)

    # ------------------------------------ move_products_and_delete_category --
    print("\n-- move_products_and_delete_category --")
    try:
        category_store.move_products_and_delete_category(coffee.id, company_a.id, coffee.id)
        check("destination == source rejected", False)
    except ValueError:
        check("destination == source rejected", True)
    try:
        category_store.move_products_and_delete_category(coffee.id, company_a.id, "does-not-exist")
        check("nonexistent destination rejected", False)
    except ValueError:
        check("nonexistent destination rejected", True)
    other_company_cat = category_store.create_category(company_b.id, "Company B Category")
    try:
        category_store.move_products_and_delete_category(coffee.id, company_a.id, other_company_cat.id)
        check("cross-company destination rejected", False)
    except ValueError:
        check("cross-company destination rejected", True)

    moved_count = category_store.move_products_and_delete_category(coffee.id, company_a.id, kitchen.id)
    check("move_products_and_delete_category reports 2 products moved", moved_count == 2)
    check("source category actually deleted", category_store.get_category(coffee.id, company_a.id) is None)
    reloaded1 = inventory_store.get_product(p1.id, company_a.id)
    reloaded2 = inventory_store.get_product(p2.id, company_a.id)
    check("product 1 now points at the destination category_id", reloaded1.category_id == kitchen.id)
    check("product 1's display-text category matches the destination", reloaded1.category == kitchen.name)
    check("product 2 now points at the destination category_id", reloaded2.category_id == kitchen.id)
    check(
        "count_products_using on the destination reflects the move",
        category_store.count_products_using(kitchen.id, company_a.id) == 2,
    )

    # ------------------------------------------------------------- isolation --
    print("\n-- cross-tenant isolation --")
    cat_b = category_store.create_category(company_b.id, "Kitchen Appliances")  # same name, different company — must NOT collide
    check("same name in a different company doesn't collide", cat_b.id != kitchen.id)
    check("company A never sees company B's categories", all(c.company_id == company_a.id for c in category_store.list_categories(company_a.id)))
    check("company B never sees company A's categories", all(c.company_id == company_b.id for c in category_store.list_categories(company_b.id)))
    check("get_category scoped to the right company returns None for another company's id", category_store.get_category(kitchen.id, company_b.id) is None)
    check("count_products_using scoped correctly", category_store.count_products_using(kitchen.id, company_b.id) == 0)

    # ---------------------------------------- manifest import: no catalog touch --
    print("\n-- manifest import never touches the Category Catalog --")
    from modules import manifest_import

    before_count = len(category_store.list_categories(company_a.id, include_inactive=True))
    rows = [{"item_description": "Some Blender", "subcategory": "Never Seen Before Subcat", "qty": "1"}]
    drafts = manifest_import.rows_to_draft_products(rows, company_id=company_a.id)
    inventory_store.save_products_bulk(drafts)
    after_count = len(category_store.list_categories(company_a.id, include_inactive=True))
    draft = drafts[0]
    check("manifest_subcategory is captured on the draft", draft.manifest_subcategory == "Never Seen Before Subcat")
    check("draft has NO category_id after import", draft.category_id == "")
    check("draft has NO category text after import", draft.category == "")
    check("Category Catalog row count unchanged by import", after_count == before_count)

    # ------------------------- promote_from_manifest_on_completion (both completion paths) --
    # New Item wizard's "Save Item" step and the bulk "Change Status" dialog
    # both call category_store.promote_from_manifest_on_completion() with
    # nothing else in between — testing the function IS testing both paths,
    # since their behavior is identical by construction (see app.py).
    print("\n-- promote_from_manifest_on_completion (both 'became completed' call sites) --")

    company_a2 = company_store.get_company(company_a.id)
    check("auto_save_categories_from_completed defaults to False", company_a2.auto_save_categories_from_completed is False)

    # 1. Setting OFF (default): completing a draft must NOT touch the catalog.
    draft.status = "completed"
    acted = category_store.promote_from_manifest_on_completion(draft)
    check("no-op when the company setting is OFF", acted is False)
    check("category_id still empty when the setting is OFF", draft.category_id == "")

    # 2. Turn the setting ON, try again — this simulates BOTH the wizard's
    #    Save Item step and the bulk Change Status dialog, since both just
    #    set product.status = "completed" then call this same function.
    company_a2.auto_save_categories_from_completed = True
    company_store.update_company(company_a2)
    acted = category_store.promote_from_manifest_on_completion(draft)
    check("acts when the setting is ON and status is completed", acted is True)
    check("category_id now set", bool(draft.category_id))
    promoted_cat = category_store.get_category(draft.category_id, company_a.id)
    check("promoted category name matches manifest_subcategory", promoted_cat is not None and promoted_cat.name == "Never Seen Before Subcat")
    check("draft.category display text matches", draft.category == "Never Seen Before Subcat")
    inventory_store.save_product(draft)  # persist, as app.py does right after calling this

    # 3. Idempotency across "both paths": a second product completed with
    #    the SAME manifest_subcategory (simulating the other UI path) must
    #    reuse the same category, never create a duplicate.
    rows2 = [{"item_description": "Another Blender", "subcategory": "Never Seen Before Subcat", "qty": "1"}]
    draft2 = manifest_import.rows_to_draft_products(rows2, company_id=company_a.id)[0]
    inventory_store.save_product(draft2)
    draft2.status = "completed"
    acted2 = category_store.promote_from_manifest_on_completion(draft2)
    check("second product (other completion path) also acts", acted2 is True)
    check("second product reuses the SAME category, no duplicate created", draft2.category_id == draft.category_id)
    inventory_store.save_product(draft2)
    check(
        "count_products_using reflects both completed products",
        category_store.count_products_using(draft.category_id, company_a.id) == 2,
    )

    # 4. A manual/AI-confirmed category_id already set must never be overridden.
    manual_cat = category_store.create_category(company_a.id, "Manually Chosen Category")
    rows3 = [{"item_description": "Third Item", "subcategory": "Should Be Ignored", "qty": "1"}]
    draft3 = manifest_import.rows_to_draft_products(rows3, company_id=company_a.id)[0]
    draft3.category_id = manual_cat.id
    draft3.category = manual_cat.name
    draft3.status = "completed"
    acted3 = category_store.promote_from_manifest_on_completion(draft3)
    check("no-op when a category_id is already set (manual pick wins)", acted3 is False)
    check("manual category_id untouched", draft3.category_id == manual_cat.id)

    # 5. Not-yet-completed products must never trigger it, even with the setting ON.
    rows4 = [{"item_description": "Fourth Item", "subcategory": "In Progress Subcat", "qty": "1"}]
    draft4 = manifest_import.rows_to_draft_products(rows4, company_id=company_a.id)[0]
    draft4.status = "in_progress"
    acted4 = category_store.promote_from_manifest_on_completion(draft4)
    check("no-op for a non-completed product even with the setting ON", acted4 is False)

    # 6. Blank manifest_subcategory must never trigger it.
    draft5 = Product(company_id=company_a.id, status="completed", sku="no-subcat-sku")
    acted5 = category_store.promote_from_manifest_on_completion(draft5)
    check("no-op when manifest_subcategory is blank", acted5 is False)

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
