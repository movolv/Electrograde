"""Proves modules/category_mapping_store.py works correctly:
  - saved rules persist and load back exactly;
  - resolve() returns the mapped external_category_id for a given
    ElectroGrader category_id, None when nothing's mapped, and None for a
    blank electrograder_category_id (never crashes on an uncategorized
    product);
  - a re-save (upsert) replaces the whole rule set rather than merging;
  - full cross-tenant isolation, same convention as every other store.

Runs against a throwaway scratch PostgreSQL database, set *before*
importing any modules.*_store module — same convention as
scripts/verify_category_store.py.

    python scripts/verify_category_mapping_store.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("category_mapping_store")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import category_mapping_store, company_store  # noqa: E402

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

    company_a = company_store.create_company("Category Mapping Test Co A", user_limit=10)
    company_b = company_store.create_company("Category Mapping Test Co B", user_limit=10)

    print("-- no mapping saved yet --")
    check("get_mapping returns None before any save", category_mapping_store.get_mapping(company_a.id, "baselinker") is None)
    check("resolve() returns None with no mapping saved", category_mapping_store.resolve(company_a.id, "baselinker", "some-category-id") is None)
    check("resolve() returns None for a blank electrograder_category_id", category_mapping_store.resolve(company_a.id, "baselinker", "") is None)

    print("\n-- save + load --")
    mapping = category_mapping_store.CategoryMapping(
        company_id=company_a.id, integration_type="baselinker",
        rules=[
            category_mapping_store.CategoryMappingRule(
                electrograder_category_id="eg-cat-1", external_category_id="6479900", external_category_label="Haushaltsgeräte > Dampfbürsten",
            ),
            category_mapping_store.CategoryMappingRule(
                electrograder_category_id="eg-cat-2", external_category_id="6479905", external_category_label="Haushaltsgeräte > Sonstige",
            ),
        ],
    )
    category_mapping_store.upsert_mapping(mapping)
    reloaded = category_mapping_store.get_mapping(company_a.id, "baselinker")
    check("mapping loads back with the right rule count", reloaded is not None and len(reloaded.rules) == 2)
    check(
        "rule 1 round-trips exactly",
        reloaded.rules[0].electrograder_category_id == "eg-cat-1" and reloaded.rules[0].external_category_id == "6479900",
    )

    print("\n-- resolve() --")
    check("resolve() finds a mapped category", category_mapping_store.resolve(company_a.id, "baselinker", "eg-cat-1") == "6479900")
    check("resolve() finds the second mapped category", category_mapping_store.resolve(company_a.id, "baselinker", "eg-cat-2") == "6479905")
    check("resolve() returns None for an unmapped ElectroGrader category", category_mapping_store.resolve(company_a.id, "baselinker", "eg-cat-unmapped") is None)
    check("resolve() is scoped by integration_type", category_mapping_store.resolve(company_a.id, "some-other-integration", "eg-cat-1") is None)

    print("\n-- upsert REPLACES the whole rule set (not a merge) --")
    mapping.rules = [
        category_mapping_store.CategoryMappingRule(electrograder_category_id="eg-cat-3", external_category_id="9999999", external_category_label="New Category"),
    ]
    category_mapping_store.upsert_mapping(mapping)
    reloaded2 = category_mapping_store.get_mapping(company_a.id, "baselinker")
    check("re-save replaced rules (only 1 left)", reloaded2 is not None and len(reloaded2.rules) == 1)
    check("old rule 'eg-cat-1' no longer resolves", category_mapping_store.resolve(company_a.id, "baselinker", "eg-cat-1") is None)
    check("new rule 'eg-cat-3' resolves", category_mapping_store.resolve(company_a.id, "baselinker", "eg-cat-3") == "9999999")

    print("\n-- cross-tenant isolation --")
    check("company B has no mapping of its own yet", category_mapping_store.get_mapping(company_b.id, "baselinker") is None)
    check("company B's resolve() never sees company A's rules", category_mapping_store.resolve(company_b.id, "baselinker", "eg-cat-3") is None)
    category_mapping_store.upsert_mapping(category_mapping_store.CategoryMapping(
        company_id=company_b.id, integration_type="baselinker",
        rules=[category_mapping_store.CategoryMappingRule(electrograder_category_id="eg-cat-3", external_category_id="1111111", external_category_label="Company B's Own")],
    ))
    check(
        "company A's mapping for the SAME electrograder_category_id text is unaffected by company B's save",
        category_mapping_store.resolve(company_a.id, "baselinker", "eg-cat-3") == "9999999",
    )
    check("company B's own mapping resolves independently", category_mapping_store.resolve(company_b.id, "baselinker", "eg-cat-3") == "1111111")

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
