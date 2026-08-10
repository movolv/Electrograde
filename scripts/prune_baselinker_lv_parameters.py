"""One-time data cleanup: keeps only an explicit whitelist of Latvian
feature parameters in each product's text_fields["features|lv"], deleting
every other key. Requested after the two condition-key rename passes, to
reduce clutter down to the attributes that actually matter for these
products' listings.

KEEP list confirmed interactively with the user, including two spelling
corrections found by checking real data before running ("Sūcējspēja" not
"Sūcējspēks") and additions requested mid-review ("Darbības ilgums",
"Žāvēšana ar karstu gaisu", "Tehnoloģija").

Same safety rule as both earlier passes: never sends a partial text_fields
payload. Each product's FULL current text_fields is fetched, only
features|lv is replaced with a filtered copy (every other language/field
untouched), then the complete text_fields is sent back.

Dry-run by default — prints exactly what would be deleted, writes nothing.
Pass --execute to actually write to BaseLinker.

    python scripts/prune_baselinker_lv_parameters.py            # dry-run
    python scripts/prune_baselinker_lv_parameters.py --execute  # real writes
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from modules import integration_store  # noqa: E402
from integrations.marketplaces.baselinker import client  # noqa: E402

KEEP = {
    "Zīmols", "Modelis", "Preces stāvoklis", "EAN", "Stāvokļa parādītais nosaukums",
    "Jauda", "Ražotāja numurs", "Ietilpība", "Produkta veids", "Sūcējspēja", "Spriegums",
    "Darbības ilgums", "Žāvēšana ar karstu gaisu", "Tehnoloģija",
}
BATCH_SIZE = 100


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually write to BaseLinker (default: dry-run only).")
    parser.add_argument("--company-id", default="default")
    args = parser.parse_args()

    rec = integration_store.get_integration(args.company_id, "baselinker")
    if rec is None or not rec.credentials.get("token"):
        print(f"No connected BaseLinker integration for company '{args.company_id}'.")
        return 1
    config = {"inventory_id": rec.settings["inventory_id"], "token": rec.credentials["token"]}

    print(f"{'EXECUTING (real writes)' if args.execute else 'DRY RUN (no writes)'} — "
          f"pruning features|lv down to: {sorted(KEEP)}\n")

    ids = client.list_all_product_ids(config)
    print(f"Scanning {len(ids)} products...\n")

    products_changed = 0
    entries_deleted = 0
    updated = 0
    errors = []

    for batch in chunks(ids, BATCH_SIZE):
        data = client.get_product_data(batch, config)
        for pid, raw in data.items():
            tf = raw.get("text_fields") or {}
            features_lv = tf.get("features|lv") or {}
            to_delete = [k for k in features_lv if k not in KEEP]
            if not to_delete:
                continue

            products_changed += 1
            entries_deleted += len(to_delete)
            sku = raw.get("sku", "")
            print(f"  {pid} (sku={sku!r}): deleting {to_delete}")

            if not args.execute:
                continue

            new_features_lv = {k: v for k, v in features_lv.items() if k in KEEP}
            new_tf = dict(tf)
            new_tf["features|lv"] = new_features_lv

            try:
                client._call(
                    "addInventoryProduct",
                    {"inventory_id": config["inventory_id"], "product_id": int(pid), "text_fields": new_tf},
                    config["token"],
                )
                updated += 1
            except Exception as e:  # noqa: BLE001 - one bad product must not abort the whole run
                errors.append(f"{pid}: {e}")
                print(f"    ERROR: {e}")

        print(f"  ...progress: {products_changed} products with deletions so far")

    print(f"\n{products_changed} product(s) had at least one parameter to delete "
          f"({entries_deleted} total parameter entries).")
    if args.execute:
        print(f"{updated} product(s) successfully updated, {len(errors)} error(s).")
        if errors:
            print("\nErrors:")
            for e in errors:
                print(f"  - {e}")
    else:
        print("Dry run only — nothing was written. Re-run with --execute to apply.")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
