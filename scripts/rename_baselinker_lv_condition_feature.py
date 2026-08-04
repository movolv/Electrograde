"""One-time data fix: renames the Latvian custom feature key "Pakāpes"
(found in every affected product's text_fields["features|lv"] dict, coming
from the supplier/BaseLinker data feed, never written by ElectroGrader's
own export) to "Preces stāvoklis" — matching the naming already present on
a handful of products, and matching ElectroGrader's own "Grade" ->
"Product Condition" rename.

Safety: never sends a partial text_fields payload. Each product's FULL
current text_fields is fetched first, only the one key inside features|lv
is renamed (value untouched), every other key/language is copied through
unchanged, then the complete text_fields is sent back — so this can never
wipe out name/description/other-language features regardless of whether
BaseLinker's API merges or replaces text_fields on write.

Dry-run by default — prints exactly what would change, writes nothing.
Pass --execute to actually write to BaseLinker.

    python scripts/rename_baselinker_lv_condition_feature.py            # dry-run
    python scripts/rename_baselinker_lv_condition_feature.py --execute  # real writes
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from modules import integration_store  # noqa: E402
from integrations.marketplaces.baselinker import client  # noqa: E402

OLD_KEY = "Pakāpes"
NEW_KEY = "Preces stāvoklis"
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
          f"renaming '{OLD_KEY}' -> '{NEW_KEY}' in features|lv\n")

    ids = client.list_all_product_ids(config)
    print(f"Scanning {len(ids)} products...\n")

    matched = 0
    updated = 0
    errors = []

    for batch in chunks(ids, BATCH_SIZE):
        data = client.get_product_data(batch, config)
        for pid, raw in data.items():
            tf = raw.get("text_fields") or {}
            features_lv = tf.get("features|lv") or {}
            if OLD_KEY not in features_lv:
                continue

            matched += 1
            value = features_lv[OLD_KEY]
            print(f"  {pid}: {OLD_KEY}={value!r} -> {NEW_KEY}={value!r}")

            if not args.execute:
                continue

            new_features_lv = dict(features_lv)
            new_features_lv[NEW_KEY] = new_features_lv.pop(OLD_KEY)
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

        print(f"  ...progress: {matched} matched so far")

    print(f"\n{matched} product(s) matched '{OLD_KEY}'.")
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
