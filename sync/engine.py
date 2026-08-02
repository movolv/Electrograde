"""The Sync Engine — the one place that turns a connector's sync() call
into a persisted sync/models.SyncRecord. This is the only module in sync/
that imports from integrations/ (integrations/ never imports from sync/ —
no circularity: integrations/ stays a self-contained lower layer, sync/
sits above it as orchestration, matching this codebase's existing
modules/ vs. integrations/ layering convention).

A future scheduled-sync executor (integrations/scheduler.py's EXECUTORS
registry, still deliberately empty since Phase 1) would call
sync_product() here rather than duplicating this logic — that's the
"Scheduled Sync Job -> Sync Engine" connection point the Phase 3.2 spec
asked to prepare.
"""
from integrations.manager import IntegrationManager, IntegrationNotAvailableError, IntegrationNotConnectedError
from modules import sync_record_store
from sync.models import SyncRecord
from sync.status import DIRECTION_EXPORT


def sync_product(
    company_id: str, product, connector_name: str, direction: str = DIRECTION_EXPORT, external_id: str = "",
) -> SyncRecord:
    """Resolves the connector, calls its universal sync() entry point, and
    persists the outcome. Never raises — connection problems (not
    connected / integration not available) come back as a DISABLED record,
    exactly like an unimplemented import direction does, since both are
    "nothing to do here yet" rather than a real error mid-sync."""
    record = SyncRecord(company_id=company_id, product_id=product.id, connector_name=connector_name, direction=direction)

    try:
        connector = IntegrationManager.get(company_id, connector_name)
    except (IntegrationNotConnectedError, IntegrationNotAvailableError) as e:
        record.mark(success=False, message=str(e), disabled=True)
        sync_record_store.upsert_record(record)
        return record

    result = connector.sync(product, external_id=external_id, direction=direction)
    is_not_implemented = not result.success and "not implemented" in result.message.lower()
    record.mark(success=result.success, message=result.message, external_id=result.external_id, disabled=is_not_implemented)
    sync_record_store.upsert_record(record)
    return record


def get_export_field_owners(company_id: str, connector_name: str) -> dict:
    """What a future real bidirectional Sync Engine would consult BEFORE
    deciding, per field, which side's value wins — read-only, and not
    called from any active export path today (build_payload()/
    export_product() remain completely unaware this exists). Sync Ownership
    configuration (modules/sync_ownership_store.py) is the actual source of
    truth; this is just the one place a future engine would read it from,
    proving the "field -> source_system" read path works end-to-end."""
    from integrations.field_registry import SYNC_OWNERSHIP_FIELDS
    from modules import sync_ownership_store

    return {
        field_name: sync_ownership_store.get_field_owner(company_id, connector_name, field_name)
        for field_name in SYNC_OWNERSHIP_FIELDS
    }
