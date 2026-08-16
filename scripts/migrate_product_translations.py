"""One-time (idempotent — safe to re-run) backfill: gives every existing
product a `product_translations` row for its `primary_language` (see
modules/product_translation_store.py, modules/inventory_store.py's
_hydrate_primary_language_bulk()/save_product()).

Before this migration, name/product_description/condition_description/
defects lived only in the `products` JSON blob. inventory_store.py now
treats `product_translations` as the source of truth for those four
fields, only falling back to the (still-present, un-stripped) blob values
for a product that has no translation row yet — which is exactly what
lets this script read each product's current content normally via
inventory_store.list_products() and write it out as a `language="en",
translated_by="ai"` row (matching reality: every product in this app has
only ever been AI-generated in English so far).

Skips any product that already has a row for its primary_language, so
re-running after new products have been created (which already write
their own row via save_product()) is a fast no-op for them.

    python scripts/migrate_product_translations.py            # dry-run, prints counts
    python scripts/migrate_product_translations.py --execute  # writes for real
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from modules import company_store, inventory_store, product_translation_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Actually write rows (default: dry-run).")
    args = parser.parse_args()

    companies = company_store.list_companies()
    print(f"Found {len(companies)} companies.")

    total_migrated = 0
    total_skipped = 0
    for company in companies:
        products = inventory_store.list_products(company.id)
        for p in products:
            existing = product_translation_store.get_translation(p.id, p.primary_language)
            if existing is not None:
                total_skipped += 1
                continue
            total_migrated += 1
            if args.execute:
                product_translation_store.upsert_translation(product_translation_store.ProductTranslation(
                    product_id=p.id, company_id=p.company_id, language=p.primary_language,
                    title=p.name, description=p.product_description,
                    condition_description=p.condition_description, defects=p.defects,
                    translated_by="ai",
                ))

    verb = "Migrated" if args.execute else "Would migrate"
    print(f"{verb} {total_migrated} product(s); {total_skipped} already had a translation row.")
    if not args.execute:
        print("Dry-run only — pass --execute to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
