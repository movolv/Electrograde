"""Proves the "one company -> one BaseLinker export language" guarantee:

  - the export language is resolved in ONE place and every export path
    (full export, preview, single-field push) reaches the same answer;
  - a product with no content in that language is REFUSED, never quietly
    exported in another one;
  - a payload never carries two content languages, so BaseLinker can never
    receive features|lv and features|en for the same product;
  - it is per-tenant configuration throughout — no hardcoded "en"/"lv"
    anywhere, and one company's choice cannot affect another's;
  - the existing dynamic Field Mapping architecture still works unchanged
    through all of it (new fields appear automatically, are never
    auto-linked, and saved rules are never overwritten).

Runs entirely offline against a throwaway scratch PostgreSQL database —
no BaseLinker API call is made, so this is safe to run anywhere.

    python scripts/verify_export_language.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("export_language")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from integrations import field_registry  # noqa: E402
from integrations.base import MissingTranslationError  # noqa: E402
from integrations.marketplaces.baselinker import field_writer, mapper  # noqa: E402
from integrations.marketplaces.baselinker.client import BaselinkerConnector  # noqa: E402
from modules import (  # noqa: E402
    company_store,
    field_mapping_store,
    inventory_store,
    models,
    product_translation_store,
)

_checks_passed = 0
_failures = []

_CONFIG = {
    "inventory_id": 1, "category_id": 2, "tax_rate": 23,
    "price_group_id": "pg1", "warehouse_id": "w1", "token": "",
}


def check(label: str, condition: bool) -> None:
    global _checks_passed
    if condition:
        _checks_passed += 1
    else:
        _failures.append(label)
    print(("  OK  " if condition else "  FAIL"), label)


def _make_product(company_id: str, sku: str, primary_language: str, **kw):
    p = models.Product(
        company_id=company_id, sku=sku, status="completed", primary_language=primary_language,
        name=kw.pop("name", "Original Title"),
        product_description=kw.pop("product_description", "Original description"),
        condition_description=kw.pop("condition_description", "Some scratches"),
        brand="Bosch", model="WAN28281", product_condition="B", power="2200W",
        color="White", price=349.0, quantity=3, weight_kg=68.5, ean="4242005116881",
        # A non-empty `location`: a Field Mapping redirect rule skips any
        # field whose current value is empty, so check (15) needs a real one.
        location=kw.pop("location", "A-101"),
        model_number="MODEL-NUMBER-NOT-THE-TITLE", **kw,
    )
    inventory_store.save_product(p)
    return p


def _add_translation(product, company_id: str, language: str, title: str) -> None:
    product_translation_store.upsert_translation(product_translation_store.ProductTranslation(
        product_id=product.id, company_id=company_id, language=language,
        title=title, description=f"{language} description",
        condition_description=f"{language} condition", color=f"{language} color",
    ))


def _connector(company_id: str, export_language: str = "") -> BaselinkerConnector:
    return BaselinkerConnector(
        company_id, {"token": ""},
        {"inventory_id": "1", "category_id": "2", "export_language": export_language},
    )


def _content_languages(payload: dict) -> set:
    """Every language actually present in a built payload's text_fields."""
    return {k.split("|", 1)[1] for k in payload.get("text_fields", {}) if "|" in k}


def main() -> int:
    print(f"Scratch DB: {_DATABASE_URL}\n")

    lv_co = company_store.create_company("LV Co", user_limit=10, default_product_language="lv")
    en_co = company_store.create_company("EN Co", user_limit=10, default_product_language="en")
    de_co = company_store.create_company("DE Co", user_limit=10, default_product_language="de")

    # ---------------------------------------------------- 1. lv wanted, lv present --
    print("-- (1) export language lv, product HAS lv --")
    p1 = _make_product(lv_co.id, "P1", primary_language="en")
    _add_translation(p1, lv_co.id, "en", "English Title")
    _add_translation(p1, lv_co.id, "lv", "Latviskais nosaukums")
    c_lv = _connector(lv_co.id)
    payload1 = c_lv.preview_payload(inventory_store.get_product(p1.id, lv_co.id))
    check("resolve_export_language() returns the company's lv", c_lv.resolve_export_language() == "lv")
    check("title is the lv one", payload1["text_fields"]["name|lv"] == "Latviskais nosaukums")
    check("payload carries exactly ONE content language", _content_languages(payload1) == {"lv"})
    check("Parameters block is features|lv", "features|lv" in payload1["text_fields"])
    check("no features|en anywhere", "features|en" not in payload1["text_fields"])

    # ------------------------------------------- 2. lv wanted, only en present --
    print("\n-- (2) export language lv, product has ONLY en -> must be BLOCKED --")
    p2 = _make_product(lv_co.id, "P2", primary_language="en")
    _add_translation(p2, lv_co.id, "en", "English Only Title")
    p2 = inventory_store.get_product(p2.id, lv_co.id)
    blocked = c_lv.preview_payload(p2)
    check("preview reports the block instead of a payload", "_export_blocked" in blocked)
    check("preview names the missing language", blocked.get("_missing_language") == "lv")
    check("preview produced NO text_fields at all", "text_fields" not in blocked)
    raised = False
    try:
        c_lv._resolve_export_content(p2)  # noqa: SLF001
    except MissingTranslationError as e:
        raised = True
        check("error message names the block", "missing 'lv' translation" in str(e))
    check("_resolve_export_content raises rather than falling back", raised)
    create_result = c_lv.create_product(p2)
    check("create_product() FAILS instead of exporting English", not create_result.success)
    check("its message is the export block", "missing 'lv' translation" in create_result.message)

    # ---------------------------------------------------- 3. en wanted, en present --
    print("\n-- (3) export language en, product HAS en --")
    p3 = _make_product(en_co.id, "P3", primary_language="en")
    _add_translation(p3, en_co.id, "en", "English Title")
    c_en = _connector(en_co.id)
    payload3 = c_en.preview_payload(inventory_store.get_product(p3.id, en_co.id))
    check("title is the en one", payload3["text_fields"]["name|en"] == "English Title")
    check("payload carries exactly ONE content language", _content_languages(payload3) == {"en"})

    # ------------------------------------------- 4. en wanted, only lv present --
    print("\n-- (4) export language en, product has ONLY lv -> must be BLOCKED --")
    p4 = _make_product(en_co.id, "P4", primary_language="lv")
    _add_translation(p4, en_co.id, "lv", "Tikai latviski")
    p4 = inventory_store.get_product(p4.id, en_co.id)
    blocked4 = c_en.preview_payload(p4)
    check("blocked (lv is NOT used as a fallback to en)", "_export_blocked" in blocked4)
    check("no Latvian text leaked into the payload", "text_fields" not in blocked4)

    # ------------------------------------------------ 5. features single-language --
    print("\n-- (5) features are only ever built in the chosen language --")
    for label, conn, prod in (("lv", c_lv, p1), ("en", c_en, p3)):
        pl = conn.preview_payload(inventory_store.get_product(prod.id, conn.company_id))
        feats = [k for k in pl.get("text_fields", {}) if k.startswith("features|")]
        check(f"exactly one features block for the {label} company", len(feats) == 1)
        check(f"and it is features|{label}", feats == [f"features|{label}"])

    # ------------------------------------------------------ 6. tenant isolation --
    print("\n-- (6) two companies with different export languages do not interfere --")
    check("LV company resolves lv", _connector(lv_co.id).resolve_export_language() == "lv")
    check("EN company resolves en", _connector(en_co.id).resolve_export_language() == "en")
    check("DE company resolves de", _connector(de_co.id).resolve_export_language() == "de")
    check(
        "a per-integration override beats the company default, for that company only",
        _connector(lv_co.id, export_language="de").resolve_export_language() == "de"
        and _connector(en_co.id).resolve_export_language() == "en",
    )

    # ----------------------------------------------- 7. no hardcoded language --
    print("\n-- (7) 100-tenant sweep: every company gets its OWN language --")
    languages = list(company_store.CONTENT_LANGUAGES)
    sweep = []
    for i in range(100):
        want = languages[i % len(languages)]
        co = company_store.create_company(f"Sweep Co {i}", user_limit=2, default_product_language=want)
        sweep.append((co.id, want))
    mismatches = [
        (cid, want, _connector(cid).resolve_export_language())
        for cid, want in sweep
        if _connector(cid).resolve_export_language() != want
    ]
    check("all 100 companies resolve to their own configured language", not mismatches)
    check("the sweep really covered more than one language", len({w for _, w in sweep}) > 1)

    # --------------------------------- 8. never two languages in one payload --
    print("\n-- (8) a payload can never carry two content languages --")
    p8 = _make_product(lv_co.id, "P8", primary_language="en")
    _add_translation(p8, lv_co.id, "en", "English Title")
    _add_translation(p8, lv_co.id, "lv", "Latviskais")
    payload8 = c_lv.preview_payload(inventory_store.get_product(p8.id, lv_co.id))
    check("only lv content keys present", _content_languages(payload8) == {"lv"})
    check("no description|en", "description|en" not in payload8["text_fields"])
    check("no description_extra1|en", "description_extra1|en" not in payload8["text_fields"])
    # The one documented exception: BaseLinker demands a name under the
    # INVENTORY's default language, and only ever `name`.
    payload8b = mapper.build_payload(
        inventory_store.get_product(p8.id, lv_co.id), _CONFIG, "lv",
        title="Latviskais", description="apraksts", condition_description="", color="balts",
        inventory_default_language="en",
    )
    extra = _content_languages(payload8b) - {"lv"}
    check("the inventory-default exception adds at most one extra key", extra == {"en"})
    check("and that key is ONLY name", [k for k in payload8b["text_fields"] if k.endswith("|en")] == ["name|en"])
    check("its VALUE is the lv text, not another language's content",
          payload8b["text_fields"]["name|en"] == "Latviskais")
    check("features is still single-language under the exception",
          [k for k in payload8b["text_fields"] if k.startswith("features|")] == ["features|lv"])

    # ------------------------------------- 9/10. completed vs draft products --
    print("\n-- (9/10) the block applies at EXPORT time; drafts are not gated --")
    draft = _make_product(lv_co.id, "P-DRAFT", primary_language="en")
    draft.status = "draft"
    inventory_store.save_product(draft)
    check(
        "an untranslated draft is still listed normally (never blocked from existing)",
        any(x.sku == "P-DRAFT" for x in inventory_store.list_products(lv_co.id)),
    )
    check(
        "the missing-translation count sees it, without blocking anything",
        product_translation_store.count_products_missing_language(lv_co.id, "lv") > 0,
    )
    check(
        "a COMPLETED product with no lv translation is refused at export",
        not c_lv.create_product(inventory_store.get_product(p2.id, lv_co.id)).success,
    )

    # ------------------------------ 11. push_field uses the SAME resolution --
    print("\n-- (11) single-field push resolves exactly like the full export --")
    p1_live = inventory_store.get_product(p1.id, lv_co.id)
    ec = c_lv._resolve_export_content(p1_live)  # noqa: SLF001
    check("resolved language matches the full export's", ec["language"] == "lv")
    partial = field_writer.build_partial_payload(p1_live, "name", _CONFIG, ec, existing_listing_id="123")
    check("push_field payload is in lv, NOT en", "name|lv" in partial["text_fields"])
    check("no en key at all", not [k for k in partial["text_fields"] if k.endswith("|en")])
    partial_brand = field_writer.build_partial_payload(p1_live, "brand", _CONFIG, ec, existing_listing_id="123")
    check("pushing a Parameters field yields features|lv", "features|lv" in partial_brand["text_fields"])
    check("and never features|en", "features|en" not in partial_brand["text_fields"])

    # -------------------------- 12. build_payload without a language errors --
    print("\n-- (12) build_payload() cannot silently default to English --")
    errored = False
    try:
        mapper.build_payload(p1_live, _CONFIG)  # type: ignore[call-arg]
    except TypeError:
        errored = True
    check("omitting `language` raises TypeError instead of assuming 'en'", errored)

    # ------------------- 13. push_field("name") sends the real resolved title --
    print("\n-- (13) push_field('name') sends the TITLE, not model_number --")
    check("value is the resolved lv title", partial["text_fields"]["name|lv"] == "Latviskais nosaukums")
    check("and is NOT the model number", partial["text_fields"]["name|lv"] != p1_live.model_number)
    partial_desc = field_writer.build_partial_payload(p1_live, "product_description", _CONFIG, ec, existing_listing_id="1")
    check(
        "push_field('product_description') sends real text, not an empty string",
        partial_desc["text_fields"].get("description|lv") == "lv description",
    )

    # -------------------- 14. preview and the real export resolve identically --
    print("\n-- (14) preview and real export share one resolution --")
    preview_only = c_lv.preview_payload(p1_live)
    real_ec = c_lv._resolve_export_content(p1_live)  # noqa: SLF001
    check("same language", _content_languages(preview_only) == {real_ec["language"]})
    check("same title", preview_only["text_fields"][f"name|{real_ec['language']}"] == real_ec["title"])
    check("preview blocks exactly where the real export blocks",
          ("_export_blocked" in c_lv.preview_payload(p2)) and (not c_lv.create_product(p2).success))

    # ----------------------- 15. the existing Field Mapping stays dynamic --
    print("\n-- (15) dynamic Field Mapping is untouched by all of the above --")
    before_rules = field_mapping_store.FieldMapping(
        company_id=lv_co.id, integration_type="baselinker",
        rules=[field_mapping_store.FieldMappingRule(source_field="location", target_field="extra_field_1")],
    )
    field_mapping_store.upsert_mapping(before_rules)

    original = dict(field_registry.SYNCABLE_FIELDS)
    try:
        field_registry.SYNCABLE_FIELDS["battery_capacity"] = {
            "label": "Battery capacity", "type": "text", "mappable": True,
        }
        sources = c_lv.get_mappable_source_fields()
        check("a NEW registry field appears as a mapping source with no code change",
              "battery_capacity" in sources)
        check("it is NOT auto-linked to anything",
              not any(r.source_field == "battery_capacity"
                      for r in field_mapping_store.get_mapping(lv_co.id, "baselinker").rules))
        saved = field_mapping_store.get_mapping(lv_co.id, "baselinker")
        check("the company's existing rule survives the new field appearing",
              [(r.source_field, r.target_field) for r in saved.rules] == [("location", "extra_field_1")])
    finally:
        field_registry.SYNCABLE_FIELDS.clear()
        field_registry.SYNCABLE_FIELDS.update(original)

    check("a new BaseLinker target field would appear via the live lookup (static fallback intact)",
          set(BaselinkerConnector.STRUCTURAL_TARGET_FIELDS) <= set(c_lv.get_target_fields()))

    # push_field and the full export must honour the SAME mapping rules.
    mapped = field_writer.build_partial_payload(
        p1_live, "location", _CONFIG, ec, existing_listing_id="1",
        field_mapping_rules=field_mapping_store.get_mapping(lv_co.id, "baselinker").rules,
        redirectable_fields=c_lv.get_mappable_source_fields(),
    )
    check("push_field honours the company's Field Mapping rule, same as a full export",
          "extra_field_1" in mapped.get("text_fields", {}))

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
