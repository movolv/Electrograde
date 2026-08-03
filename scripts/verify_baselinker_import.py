"""Proves the bulk catalog import feature
(sync/catalog_import.py + integrations/base.py's ImportedProductData/
fetch_catalog() + BaseLinker's real fetch_catalog() implementation) works
correctly:
  - sync/catalog_import.py's orchestration is genuinely connector-agnostic
    — proven against a throwaway fake connector that knows nothing about
    BaseLinker (same fake-connector convention as verify_sync_engine.py),
    so a future eBay/Amazon/Tradera connector needs zero changes there;
  - imported products land as status="draft" with correct sku/name/price/
    quantity, a linked marketplace_listing (so Push/Pull/Sync recognize
    them immediately), and an image-save callback invoked per image URL;
  - re-running import is idempotent — no duplicates;
  - BaseLinker's real fetch_catalog() (client._call mocked at the HTTP
    boundary, same convention as verify_baselinker_realtime_sync.py)
    paginates getInventoryProductsList and parses getInventoryProductsData
    correctly;
  - full cross-tenant isolation.

Runs against a throwaway scratch database (ELECTROGRADER_DB_PATH), set
*before* importing any modules.*_store / integrations / sync module — same
convention as every other verify_*.py script in this repo.

    python scripts/verify_baselinker_import.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCRATCH_DIR = tempfile.mkdtemp(prefix="electrograder_import_test_")
os.environ["ELECTROGRADER_DB_PATH"] = os.path.join(_SCRATCH_DIR, "test.db")
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import (  # noqa: E402
    company_store, inventory_store, integration_store, marketplace_store,
)
from integrations import manager as integration_manager  # noqa: E402
from integrations.base import (  # noqa: E402
    ConnectionTestResult, ConnectorActionResult, ImportedProductData, MarketplaceConnector,
)
from integrations.marketplaces.baselinker import client as bl_client, import_products  # noqa: E402
from sync import catalog_import  # noqa: E402

_checks_passed = 0
_failures = []


def check(label: str, condition: bool) -> None:
    global _checks_passed
    if condition:
        _checks_passed += 1
    else:
        _failures.append(label)
    print(("  OK  " if condition else "  FAIL"), label)


# ---------------------------------------------------------- fake connector --
class _FakeMarketplaceConnector(MarketplaceConnector):
    """Registered only inside this script — proves catalog_import.py's
    orchestration is connector-agnostic (never mentions BaseLinker)."""

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

    def fetch_catalog(self):
        return [
            ImportedProductData(external_id="ext-1", sku="FAKE-SKU-1", name="Fake Widget", price=9.99, quantity=5, image_urls=["http://example.test/1.jpg"]),
            ImportedProductData(external_id="ext-2", sku="FAKE-SKU-2", name="Fake Gadget", price=19.99, quantity=2),
        ]


integration_manager.CONNECTORS["test_fake_mp"] = _FakeMarketplaceConnector
integration_manager._CATALOG_BY_TYPE["test_fake_mp"] = integration_manager.CatalogEntry(  # noqa: SLF001
    "test_fake_mp", "marketplace", "Test Fake Marketplace", True,
)


def main() -> int:
    print(f"Scratch DB: {os.environ['ELECTROGRADER_DB_PATH']}\n")

    company_a = company_store.create_company("Import Test Co A", user_limit=10)
    company_b = company_store.create_company("Import Test Co B", user_limit=10)

    integration_store.upsert_integration(
        integration_store.CompanyIntegration(
            company_id=company_a.id, integration_type="test_fake_mp", integration_category="marketplace",
            status=integration_store.STATUS_CONNECTED, credentials={"token": "x"}, settings={},
        )
    )

    # ------------------------------------------------- connector-agnostic --
    print("-- catalog_import.py orchestration (fake connector, no BaseLinker knowledge) --")
    preview1 = catalog_import.preview_import(company_a.id, "test_fake_mp")
    check("preview: both fake products are 'new'", len(preview1["new"]) == 2)
    check("preview: nothing 'existing' yet", len(preview1["existing"]) == 0)

    saved_images = []
    result = catalog_import.import_catalog(
        company_a.id, "test_fake_mp", "tester-user", save_image=lambda p, url: saved_images.append((p.sku, url)),
    )
    check("import: 2 imported", result["imported"] == 2)
    check("import: 0 skipped", result["skipped_existing"] == 0)
    check("import: no errors", result["errors"] == [])
    check("image callback invoked once (only product 1 has an image url)", saved_images == [("FAKE-SKU-1", "http://example.test/1.jpg")])

    products = inventory_store.list_products(company_a.id)
    check("2 products created", len(products) == 2)
    check("imported products are status=draft", all(p.status == "draft" for p in products))
    by_sku = {p.sku: p for p in products}
    check("name/price/quantity correct", by_sku["FAKE-SKU-1"].name == "Fake Widget" and by_sku["FAKE-SKU-1"].price == 9.99 and by_sku["FAKE-SKU-1"].quantity == 5)

    listing = marketplace_store.get_listing(by_sku["FAKE-SKU-1"].id, "test_fake_mp", company_a.id)
    check("marketplace_listing created with correct external_id", listing is not None and listing.external_listing_id == "ext-1")

    # ------------------------------------------------------------- idempotent --
    print("\n-- idempotence --")
    preview2 = catalog_import.preview_import(company_a.id, "test_fake_mp")
    check("preview after import: 0 new (already imported)", len(preview2["new"]) == 0)
    check("preview after import: 2 existing", len(preview2["existing"]) == 2)
    result2 = catalog_import.import_catalog(company_a.id, "test_fake_mp", "tester-user")
    check("re-running import creates 0 new products", result2["imported"] == 0)
    check("re-running import skips both as existing", result2["skipped_existing"] == 2)
    check("still exactly 2 products (no duplicates)", len(inventory_store.list_products(company_a.id)) == 2)

    # ------------------------------------------------------------------- isolation --
    print("\n-- cross-tenant isolation --")
    check("company B sees no products (isolated from A's import)", len(inventory_store.list_products(company_b.id)) == 0)
    preview_b = catalog_import.preview_import(company_b.id, "test_fake_mp")
    check("company B has no connection to test_fake_mp -> preview returns nothing", preview_b == {"new": [], "existing": []})

    # ------------------------------------------------- real BaseLinker fetch_catalog --
    print("\n-- BaseLinker's real fetch_catalog() (mocked HTTP) --")
    _pages = {
        1: {"status": "SUCCESS", "products": {"111": {}, "112": {}}},
        2: {"status": "SUCCESS", "products": {}},
    }
    _data_response = {
        "status": "SUCCESS",
        "products": {
            "111": {
                "sku": "BL-SKU-1", "ean": "1234567890123",
                "text_fields": {"name": "BaseLinker Product One"},
                "prices": {"1": 42.5}, "stock": {"1": 7},
                "images": {"1": "https://cdn.example.test/img1.jpg", "2": "https://cdn.example.test/img2.jpg"},
            },
            "112": {
                "sku": "BL-SKU-2", "ean": "",
                "text_fields": {"name": "BaseLinker Product Two"},
                "prices": {"1": 15.0}, "stock": {"1": 0},
                "images": {},
            },
        },
    }

    def _fake_bl_call(method, parameters, token):
        if method == "getInventoryProductsList":
            return _pages[parameters["page"]]
        if method == "getInventoryProductsData":
            return _data_response
        raise AssertionError(f"Unexpected method: {method}")

    bl_client._call = _fake_bl_call
    config = {"inventory_id": "1", "price_group_id": "1", "warehouse_id": "1", "token": "fake"}

    ids = bl_client.list_all_product_ids(config)
    check("list_all_product_ids paginates across pages and stops at empty page", ids == ["111", "112"])

    catalog = import_products.fetch_all_products(config)
    check("fetch_all_products returns 2 ImportedProductData", len(catalog) == 2)
    item1 = next(i for i in catalog if i.sku == "BL-SKU-1")
    check("real BaseLinker item: name parsed from text_fields", item1.name == "BaseLinker Product One")
    check("real BaseLinker item: price parsed from configured price_group_id", item1.price == 42.5)
    check("real BaseLinker item: quantity parsed from configured warehouse_id", item1.quantity == 7)
    check("real BaseLinker item: barcode parsed from ean", item1.barcode == "1234567890123")
    check("real BaseLinker item: image URLs collected", set(item1.image_urls) == {"https://cdn.example.test/img1.jpg", "https://cdn.example.test/img2.jpg"})
    item2 = next(i for i in catalog if i.sku == "BL-SKU-2")
    check("real BaseLinker item with quantity=0 parsed correctly (not dropped)", item2.quantity == 0)

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
