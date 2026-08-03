"""Bulk catalog import orchestration — generic across every marketplace
connector. Never knows which connector it's talking to beyond the
connector_name string: it goes through IntegrationManager.get(...) and
calls the universal connector.fetch_catalog() (integrations/base.py), the
same "orchestration reads the abstract interface, never a concrete
connector class" discipline as sync/engine.py's process_sync_queue()/
pull_product(). A future eBay/Amazon/Tradera connector needs zero changes
here — it only has to implement its own fetch_catalog().

Deliberately has NO filesystem/Streamlit dependency: image downloading is
injected via a `save_image` callback (see import_catalog()) so this module
never needs to know about UPLOAD_DIR/sku_folder_name — those conventions
live in app.py, the only place that already owns them.
"""
from modules import audit_store, inventory_store, marketplace_store
from modules.models import Product


def preview_import(company_id: str, connector_name: str) -> dict:
    """Fetches the connector's full catalog and splits it into "new"
    (no existing product with this SKU) vs "existing" (already imported or
    already present) — read-only, nothing is written."""
    from integrations.manager import IntegrationManager, IntegrationNotAvailableError, IntegrationNotConnectedError

    try:
        connector = IntegrationManager.get(company_id, connector_name)
    except (IntegrationNotConnectedError, IntegrationNotAvailableError):
        return {"new": [], "existing": []}

    catalog = connector.fetch_catalog()
    existing_skus = {p.sku for p in inventory_store.list_products(company_id) if p.sku}

    new_items = [item for item in catalog if item.sku not in existing_skus]
    existing_items = [item for item in catalog if item.sku in existing_skus]
    return {"new": new_items, "existing": existing_items}


def import_catalog(company_id: str, connector_name: str, user_id: str, save_image=None, on_progress=None) -> dict:
    """Imports every "new" item from preview_import() as a fresh, draft
    Product, linked to the connector's listing so Push/Pull/Sync buttons
    recognize it immediately. Idempotent — re-running skips anything
    whose SKU now exists (including products this same call just created,
    naturally, since each iteration re-checks against the DB via the
    initial preview_import() snapshot only — a SKU appearing twice in one
    catalog fetch would still only be created once, since existing_skus
    is checked once up front, not per item... see note below).

    `save_image(product, image_url) -> None`: optional callback that
    downloads and attaches one image to `product` (e.g. app.py's
    UPLOAD_DIR/sku_folder_name convention) — left as a no-op if omitted,
    so this function stays testable without any real file I/O.

    `on_progress(imported, skipped, error_count, total) -> None`: optional
    callback invoked after EVERY item in preview["new"] is processed
    (whether it succeeded, was a within-run duplicate, or errored) — lets a
    caller (e.g. app.py's background import worker) persist live progress
    without this module knowing anything about how/where that's displayed.
    `total` is always `len(preview["new"])`, constant across all calls.
    """
    preview = preview_import(company_id, connector_name)
    total_new = len(preview["new"])
    imported = 0
    skipped = len(preview["existing"])
    errors = []

    seen_skus_this_run = set()
    for item in preview["new"]:
        if not item.sku or item.sku in seen_skus_this_run:
            skipped += 1
            if on_progress is not None:
                on_progress(imported, skipped, len(errors), total_new)
            continue
        seen_skus_this_run.add(item.sku)

        try:
            product = Product(
                company_id=company_id, status="draft", sku=item.sku, name=item.name or item.sku,
                price=item.price, quantity=item.quantity or 1,
            )
            if item.barcode:
                product.ean = item.barcode
                product.ean_source = connector_name
                product.ean_status = "Found"
            inventory_store.save_product(product)

            if save_image is not None:
                for image_url in item.image_urls:
                    save_image(product, image_url)

            marketplace_store.upsert_listing(
                marketplace_store.MarketplaceListing(
                    product_id=product.id, marketplace=connector_name, company_id=company_id,
                    external_listing_id=item.external_id, status=marketplace_store.STATUS_LISTED,
                    price=product.price,
                )
            )
            audit_store.log_audit(
                company_id, user_id, "IMPORT_CATALOG", "product", product.id, f"from {connector_name}",
            )
            imported += 1
        except Exception as e:  # noqa: BLE001 - one bad item must not abort the whole import
            errors.append(f"{item.sku}: {e}")
        if on_progress is not None:
            on_progress(imported, skipped, len(errors), total_new)

    return {"imported": imported, "skipped_existing": skipped, "errors": errors}
