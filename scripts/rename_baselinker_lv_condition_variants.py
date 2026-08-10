"""One-time data fix, round 2: normalizes every inconsistent Latvian
condition-key variant found in text_fields["features|lv"]
("Klase", "Stāvoklis", "Produkta stāvoklis", "Grādi", and the
capitalization variant "Preces Stāvoklis") to a single, consistent
"Preces stāvoklis" key — value (A/B/C/D) always preserved.

Handles the two known edge cases found during investigation:
  - products with TWO condition-like keys at once (e.g. both "Klase" and
    "Stāvoklis" with the same value) — both old keys are removed, replaced
    by one "Preces stāvoklis" entry;
  - products with NO condition value in features|lv at all — skipped and
    reported separately, never guessed at.

Same safety rule as the first rename pass: never sends a partial
text_fields payload. Each product's FULL current text_fields is fetched,
only the features|lv sub-dict's condition key(s) are touched, and the
complete text_fields is sent back unchanged otherwise.

Dry-run by default — prints exactly what would change, writes nothing.
Pass --execute to actually write to BaseLinker.

    python scripts/rename_baselinker_lv_condition_variants.py            # dry-run
    python scripts/rename_baselinker_lv_condition_variants.py --execute  # real writes
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from modules import integration_store  # noqa: E402
from integrations.marketplaces.baselinker import client  # noqa: E402

TARGET_KEY = "Preces stāvoklis"
OLD_VARIANT_KEYS = ["Klase", "Stāvoklis", "Produkta stāvoklis", "Grādi", "Preces Stāvoklis"]
VALID_VALUES = {"A", "B", "C", "D"}
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
          f"normalizing {OLD_VARIANT_KEYS} -> '{TARGET_KEY}' in features|lv\n")

    ids = client.list_all_product_ids(config)
    print(f"Scanning {len(ids)} products...\n")

    matched = 0
    updated = 0
    skipped_no_value = []
    errors = []

    for batch in chunks(ids, BATCH_SIZE):
        data = client.get_product_data(batch, config)
        for pid, raw in data.items():
            tf = raw.get("text_fields") or {}
            features_lv = tf.get("features|lv") or {}

            found_keys = [k for k in OLD_VARIANT_KEYS if k in features_lv]
            if TARGET_KEY in features_lv and not found_keys:
                continue  # already correct, nothing to do

            if not found_keys:
                if TARGET_KEY not in features_lv:
                    skipped_no_value.append((pid, raw.get("sku", "")))
                continue

            value = features_lv[found_keys[0]]
            if value.strip().upper() not in VALID_VALUES:
                skipped_no_value.append((pid, raw.get("sku", ""), f"non-A-D value: {value!r}"))
                continue

            matched += 1
            print(f"  {pid} (sku={raw.get('sku', '')!r}): {found_keys} = {value!r} -> {TARGET_KEY}={value!r}")

            if not args.execute:
                continue

            new_features_lv = dict(features_lv)
            for k in found_keys:
                new_features_lv.pop(k, None)
            new_features_lv[TARGET_KEY] = value
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

    print(f"\n{matched} product(s) matched an old condition-key variant.")
    if args.execute:
        print(f"{updated} product(s) successfully updated, {len(errors)} error(s).")
        if errors:
            print("\nErrors:")
            for e in errors:
                print(f"  - {e}")
    else:
        print("Dry run only — nothing was written. Re-run with --execute to apply.")

    print(f"\nSkipped (no valid A/B/C/D condition value found): {len(skipped_no_value)}")
    for entry in skipped_no_value:
        print(f"  {entry}")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
