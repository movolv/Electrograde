"""The "Sync now" button's backend (app.py's Inventory detail view) — the
one place that decides WHAT a manual, on-demand two-way sync attempt
actually does: run the real, proven export, then attempt the (today,
honestly unimplemented) import. A future scheduled-sync executor would
likely call sync/engine.py directly with a single direction instead of
going through this service, since "try both directions" is specifically a
manual-trigger UX choice, not a generic sync primitive.
"""
from typing import List

from modules import marketplace_store
from sync import engine
from sync.models import SyncRecord
from sync.status import DIRECTION_EXPORT, DIRECTION_IMPORT


def run_manual_sync(company_id: str, product, connector_name: str) -> List[SyncRecord]:
    """Returns [export_record, import_record]. Export is real (delegates to
    the already-proven export_product() path). Import intentionally comes
    back DISABLED today — see integrations/base.py's import_product()."""
    export_record = engine.sync_product(company_id, product, connector_name, direction=DIRECTION_EXPORT)

    existing_listing = marketplace_store.get_listing(product.id, connector_name, company_id)
    external_id = export_record.external_id or (existing_listing.external_listing_id if existing_listing else "")

    import_record = engine.sync_product(
        company_id, product, connector_name, direction=DIRECTION_IMPORT, external_id=external_id,
    )
    return [export_record, import_record]
