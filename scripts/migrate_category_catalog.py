"""One-off (idempotent — safe to re-run) migration: turns a company's
existing free-text product.category values into real Category Catalog
entries (modules/category_store.py) and points each affected product's new
category_id at the matching one.

Only considers products with status == "completed" — the same rule the
live app now applies (see app.py's _maybe_promote_manifest_category() and
the company-level "Automatically save categories from completed products"
setting): a category should only enter the catalog once a real, finished
product actually used it, never from an unverified draft/in-progress
manifest claim that might never be confirmed.

Default mode is AUDIT-ONLY and read-only — it never writes anything. Always
run it that way first and read the report before ever passing --apply:

    python scripts/migrate_category_catalog.py --company-id default
    python scripts/migrate_category_catalog.py            # every company

Only once the report has been reviewed:

    python scripts/migrate_category_catalog.py --company-id default --apply

--apply creates one ROOT-level Category (source="system") per
NORMALIZED-DISTINCT existing category value — "Coffee Machine" and
"coffee machine" collapse into a single entry, but "Coffee Machine" and
"Coffee Machines" do NOT (never fuzzy-merged; an admin sorts those by hand
afterward in Settings -> Categories — see the approved plan). Existing
AI-generated category text is the STARTING catalog, not something discarded
and waited out. Idempotent: re-running finds the categories already exist
(via category_store.find_or_create_by_name) and just re-confirms product
links; products with a blank category are left unmigrated and reported as
such.
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import category_store, company_store, inventory_store  # noqa: E402


def _normalize(name: str) -> str:
    return " ".join((name or "").strip().casefold().split())


def _audit_company(company_id: str) -> None:
    all_products = inventory_store.list_products(company_id)
    products = [p for p in all_products if p.status == "completed"]
    by_normalized = defaultdict(list)  # normalized -> [(original_text, product), ...]
    blank_count = 0
    for p in products:
        if not p.category or not p.category.strip():
            blank_count += 1
            continue
        by_normalized[_normalize(p.category)].append((p.category, p))

    print(
        f"\n=== Company {company_id!r}: {len(all_products)} products total, "
        f"{len(products)} completed, {blank_count} completed with no category ==="
    )
    if not by_normalized:
        print("  No non-blank category values found — nothing to migrate.")
        return

    print(f"  {len(by_normalized)} normalized-distinct category value(s):")
    for normalized, entries in sorted(by_normalized.items(), key=lambda kv: -len(kv[1])):
        originals = sorted({text for text, _p in entries})
        variant_note = f"  [{len(originals)} case/whitespace variants: {originals}]" if len(originals) > 1 else ""
        print(f"    - {originals[0]!r}: {len(entries)} product(s){variant_note}")

    # Flag genuinely-different-looking near-duplicates for a human to read
    # — never auto-merged, listed only as a hint. Crude token-overlap
    # heuristic (shares at least one significant word with another entry),
    # good enough for a human skim, not a decision-making mechanism.
    words_by_normalized = {n: set(n.split()) for n in by_normalized}
    flagged = set()
    keys = list(by_normalized.keys())
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if words_by_normalized[a] & words_by_normalized[b] and a != b:
                flagged.add(a)
                flagged.add(b)
    if flagged:
        print("\n  Possibly-related but NOT identical (never auto-merged — sort by hand if needed):")
        for n in sorted(flagged):
            print(f"    - {by_normalized[n][0][0]!r}")


def _apply_company(company_id: str) -> None:
    products = [p for p in inventory_store.list_products(company_id) if p.status == "completed"]
    created = 0
    migrated = 0
    unmigrated_blank = 0

    for p in products:
        if not p.category or not p.category.strip():
            unmigrated_blank += 1
            continue
        existing = category_store.find_by_name(company_id, p.category)
        category = existing or category_store.create_category(
            company_id, p.category.strip(), parent_id="", source=category_store.SOURCE_SYSTEM,
        )
        if existing is None:
            created += 1
        if p.category_id != category.id or p.category != category.name:
            p.category_id = category.id
            p.category = category.name
            inventory_store.save_product(p)
            migrated += 1

    print(
        f"Company {company_id!r}: {created} categor{'y' if created == 1 else 'ies'} created, "
        f"{migrated} completed product(s) updated, "
        f"{unmigrated_blank} completed product(s) left unmigrated (blank category)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company-id", default="", help="Limit to one company (default: every company).")
    parser.add_argument("--apply", action="store_true", help="Actually create categories and update products (default: audit-only, read-only).")
    args = parser.parse_args()

    company_ids = [args.company_id] if args.company_id else [c.id for c in company_store.list_companies()]
    if not company_ids:
        print("No companies found.")
        return 0

    if args.apply:
        for cid in company_ids:
            _apply_company(cid)
    else:
        print("AUDIT MODE (read-only) — pass --apply once you've reviewed this report.")
        for cid in company_ids:
            _audit_company(cid)
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    finally:
        from modules import db
        db.close_pool()
    raise SystemExit(exit_code)
