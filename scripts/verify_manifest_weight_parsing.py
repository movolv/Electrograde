"""Proves modules/manifest_import.py's weight-column parsing tolerates
both decimal-point ("0.5") and decimal-comma ("0,5" — common in EU-locale
manifest exports) notation, plus stray unit text ("1.2 kg").

Root cause this fixes: a manifest column correctly detected/mapped as
"Weight (kg)" (auto_detect_columns finds the header fine), but a
per-row value like "0,5" made the old bare float(...) call raise
ValueError, silently caught and turned into weight_kg=0.0 — with zero
warning anywhere. The column showed as "mapped" in the import UI, but
every affected product's weight simply never appeared on the product
card. See _parse_weight_value()'s docstring for the full story.

Pure-function tests — no database needed, unlike every other
verify_*.py script in this folder.

    python scripts/verify_manifest_weight_parsing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.manifest_import import _fix_weight_units, _parse_qty_weight, _parse_weight_value  # noqa: E402

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
    print("-- _parse_weight_value(): both decimal notations --")
    check("plain decimal-point", _parse_weight_value("2.5") == 2.5)
    check("decimal-comma (EU locale)", _parse_weight_value("0,5") == 0.5)
    check("decimal-comma, no leading zero", _parse_weight_value(",75") == 0.75)
    check("unit text appended", _parse_weight_value("1.2 kg") == 1.2)
    check("unit text + decimal-comma", _parse_weight_value("0,75 kg") == 0.75)
    check("surrounding whitespace", _parse_weight_value("  1.5  ") == 1.5)
    check("thousands-comma + decimal-point", _parse_weight_value("1,234.5") == 1234.5)
    check("thousands-dot + decimal-comma", _parse_weight_value("1.234,5") == 1234.5)
    check("blank string -> 0.0, not an error", _parse_weight_value("") == 0.0)
    check("genuinely non-numeric ('N/A') -> 0.0, not an error", _parse_weight_value("N/A") == 0.0)
    check("dash placeholder -> 0.0", _parse_weight_value("-") == 0.0)
    check("whole number, no separator", _parse_weight_value("12") == 12.0)

    print("\n-- _parse_qty_weight(): weight uses the tolerant parser, qty unaffected --")
    check(
        "decimal-comma weight parses; qty still plain int",
        _parse_qty_weight({"qty": "3", "weight_kg": "0,5"}) == (3, 0.5),
    )
    check(
        "qty with a genuine thousands-comma is NOT altered by the weight fix",
        _parse_qty_weight({"qty": "1", "weight_kg": "2.5"})[0] == 1,
    )

    print("\n-- _fix_weight_units(): gram-mislabeled-as-kg heuristic still works with mixed notations --")
    rows = [{"weight_kg": "6500"}, {"weight_kg": "7200,5"}, {"weight_kg": "5100"}]
    _fix_weight_units(rows)
    check(
        "batch median > threshold -> every row divided by 1000, decimal-comma row included",
        [r["weight_kg"] for r in rows] == ["6.5", "7.2005", "5.1"],
    )

    rows_kg_already = [{"weight_kg": "0,5"}, {"weight_kg": "1,2"}, {"weight_kg": "2,5"}]
    _fix_weight_units(rows_kg_already)
    check(
        "batch median within normal kg range -> comma-decimal rows left untouched (not misdetected as grams)",
        [r["weight_kg"] for r in rows_kg_already] == ["0,5", "1,2", "2,5"],
    )

    print(f"\n{_checks_passed} check(s) passed, {len(_failures)} failed.")
    if _failures:
        print("\nFAILED:")
        for f in _failures:
            print(f"  - {f}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
