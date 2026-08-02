"""Proves the Phase 3.2/3.3 Sync Engine architecture prep is correct and
doesn't touch the real, proven BaseLinker export path:
  - sync/engine.py's sync_product(direction="export") produces the exact
    same outcome as calling connector.export_product() directly — the
    Engine only delegates, it never reimplements export logic;
  - sync_product(direction="import") comes back honestly DISABLED (no
    connector implements pulling data FROM a marketplace yet);
  - sync/mapper.py's CommonExportModel is genuinely marketplace-agnostic
    (no BaseLinker-specific key ever appears in it);
  - the new sync_records table is fully tenant-isolated.

Runs against a throwaway scratch database (ELECTROGRADER_DB_PATH), set
*before* importing any modules.*_store / integrations / sync module — same
convention as scripts/verify_tenant_isolation.py.

    python scripts/verify_sync_engine.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCRATCH_DIR = tempfile.mkdtemp(prefix="electrograder_sync_engine_test_")
os.environ["ELECTROGRADER_DB_PATH"] = os.path.join(_SCRATCH_DIR, "test.db")
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import company_store, integration_store, sync_record_store  # noqa: E402
from integrations import manager as integration_manager  # noqa: E402
from integrations.base import ConnectionTestResult, ConnectorActionResult, MarketplaceConnector  # noqa: E402
from sync import engine, service  # noqa: E402
from sync.mapper import to_common_export_model  # noqa: E402
from sync.status import DIRECTION_EXPORT, DIRECTION_IMPORT, STATUS_DISABLED  # noqa: E402


class _FakeMarketplaceConnector(MarketplaceConnector):
    """A dummy connector registered only inside this script — never
    touches the real BaselinkerConnector or makes any network call.
    Proves the Engine's delegation logic in isolation from real API
    connectivity, matching this codebase's established test convention
    (scripts/verify_sync_scheduler.py's dummy executors)."""

    integration_type = "test_fake_mp"

    def connect(self):
        return ConnectionTestResult(True, "ok")

    def test_connection(self):
        return ConnectionTestResult(True, "ok")

    def create_product(self, product):
        return ConnectorActionResult(success=True, external_id="fake-ext-1", message="Created.")

    def update_product(self, product, external_id):
        return ConnectorActionResult(success=True, external_id=external_id, message="Updated.")

    def delete_product(self, external_id):
        return ConnectorActionResult(success=True, external_id=external_id, message="Deleted.")

    def sync_inventory(self, product, external_id):
        return self.update_product(product, external_id)


integration_manager.CONNECTORS["test_fake_mp"] = _FakeMarketplaceConnector
integration_manager._CATALOG_BY_TYPE["test_fake_mp"] = integration_manager.CatalogEntry(  # noqa: SLF001
    "test_fake_mp", "marketplace", "Test Fake Marketplace", True,
)

_checks_passed = 0
_failures = []


def check(label: str, condition: bool) -> None:
    global _checks_passed
    if condition:
        _checks_passed += 1
    else:
        _failures.append(label)
    print(("  OK  " if condition else "  FAIL"), label)


class _FakeProduct:
    id = "p1"
    company_id = "c1"
    sku = "SKU1"
    name = "Widget"
    model_number = "M1"
    brand = "Acme"
    model = "X100"
    category = "Coffee Machines"
    grade = "B"
    defects = ["Minor scratches"]
    product_description = "Desc"
    condition_description = "Scratched"
    ean = "123456"
    manifest_barcode = ""
    scanned_barcode = ""
    price = 99.99
    quantity = 3
    image_paths = []
    box_length_cm = 0
    box_width_cm = 0
    box_height_cm = 0
    manifest_weight_kg = 0


def main() -> int:
    print(f"Scratch DB: {os.environ['ELECTROGRADER_DB_PATH']}\n")

    company_a = company_store.create_company("Sync Engine Test Co A", user_limit=10)
    company_b = company_store.create_company("Sync Engine Test Co B", user_limit=10)

    # ------------------------------------------------- no connection at all --
    print("-- sync_product() with no connected integration --")
    rec = engine.sync_product(company_a.id, _FakeProduct(), "test_fake_mp", direction=DIRECTION_EXPORT)
    check("export with no connection -> DISABLED (not connected)", rec.sync_status == STATUS_DISABLED)
    check(
        "SyncRecord persisted",
        sync_record_store.get_record(company_a.id, "p1", "test_fake_mp", DIRECTION_EXPORT) is not None,
    )

    # ------------------------------------------- engine delegates, doesn't reimplement --
    print("\n-- engine.sync_product(export) delegates to the exact same connector.export_product() --")
    integration_store.upsert_integration(
        integration_store.CompanyIntegration(
            company_id=company_a.id, integration_type="test_fake_mp",
            integration_category=integration_store.CATEGORY_MARKETPLACE,
            status=integration_store.STATUS_CONNECTED,
            credentials={"token": "t"}, settings={},
        )
    )
    connector = integration_manager.get(company_a.id, "test_fake_mp")
    direct_result = connector.export_product(_FakeProduct())
    engine_record = engine.sync_product(company_a.id, _FakeProduct(), "test_fake_mp", direction=DIRECTION_EXPORT)
    check(
        "engine's outcome (success/failure) matches a direct export_product() call",
        engine_record.sync_status == ("success" if direct_result.success else "failed"),
    )
    check("engine persisted the same external_id the direct call returned", engine_record.external_id == direct_result.external_id)

    # ------------------------------------------------------- import direction --
    print("\n-- sync_product(import) for a connected marketplace with no import_product() override --")
    import_record = engine.sync_product(company_a.id, _FakeProduct(), "test_fake_mp", direction=DIRECTION_IMPORT)
    check("import direction -> DISABLED (not implemented yet)", import_record.sync_status == STATUS_DISABLED)
    check("honest error message mentions 'not implemented'", "not implemented" in import_record.error_message.lower())

    # ------------------------------------------------------- run_manual_sync --
    print("\n-- service.run_manual_sync() returns both directions --")
    records = service.run_manual_sync(company_a.id, _FakeProduct(), "test_fake_mp")
    check("run_manual_sync returns exactly 2 records", len(records) == 2)
    check("first is export, second is import", records[0].direction == DIRECTION_EXPORT and records[1].direction == DIRECTION_IMPORT)

    # ------------------------------------------------------- CommonExportModel --
    print("\n-- CommonExportModel is marketplace-agnostic --")
    cem = to_common_export_model(_FakeProduct())
    cem_keys = set(vars(cem).keys()) | set(cem.attributes.keys())
    baselinker_specific_terms = {"inventory_id", "category_id", "text_fields", "description_extra1", "ean", "baselinker"}
    check(
        "no BaseLinker-specific key ever appears in CommonExportModel",
        not (cem_keys & baselinker_specific_terms),
    )
    check("attributes dict carries brand/model/category/grade/defects structured, not prose", cem.attributes == {
        "brand": "Acme", "model": "X100", "category": "Coffee Machines", "grade": "B", "defects": ["Minor scratches"],
    })
    check("title/description/price/quantity/sku/barcode carried over correctly", (
        cem.title == "Widget" and cem.description == "Desc" and cem.price == 99.99
        and cem.quantity == 3 and cem.sku == "SKU1" and cem.barcode == "123456"
    ))

    # ------------------------------------------------------------- tenant isolation --
    print("\n-- sync_records: cross-tenant isolation --")
    engine.sync_product(company_b.id, _FakeProduct(), "test_fake_mp", direction=DIRECTION_EXPORT)
    check(
        "list_records(company=A) never contains company B's record for the same product_id",
        all(r.company_id == company_a.id for r in sync_record_store.list_records(company_a.id, "p1")),
    )
    check(
        "list_records(company=B) never contains company A's record",
        all(r.company_id == company_b.id for r in sync_record_store.list_records(company_b.id, "p1")),
    )
    check(
        "get_record(company=B, ...) returns B's own row, not A's",
        sync_record_store.get_record(company_b.id, "p1", "test_fake_mp", DIRECTION_EXPORT).company_id == company_b.id,
    )

    # ------------------------------------------------------------------- summary --
    print(f"\n{_checks_passed} check(s) passed, {len(_failures)} failed.")
    if _failures:
        print("\nFAILED:")
        for f in _failures:
            print(f"  - {f}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    import shutil
    try:
        exit_code = main()
    finally:
        shutil.rmtree(_SCRATCH_DIR, ignore_errors=True)
    raise SystemExit(exit_code)
