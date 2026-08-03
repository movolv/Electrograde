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
from modules import product_change_log_store, sync_queue_store, sync_record_store
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


def process_sync_queue(limit: int = 10) -> int:
    """Claims pending sync_queue rows (same claim-then-process pattern as
    integrations/scheduler.py's process_due_jobs()) and pushes each one via
    the connector's push_field(). Cross-tenant (scheduler infra) — every
    item still carries its own company_id."""
    from modules import inventory_store, sync_log_store

    processed = 0
    for item in sync_queue_store.claim_pending(limit=limit):
        try:
            connector = IntegrationManager.get(item.company_id, item.connector_name)
        except (IntegrationNotConnectedError, IntegrationNotAvailableError) as e:
            sync_queue_store.mark_failed(item.id, str(e), item.attempts, item.max_attempts)
            processed += 1
            continue

        product = inventory_store.get_product(item.product_id, item.company_id)
        if product is None:
            sync_queue_store.mark_failed(item.id, "Product no longer exists.", item.attempts, item.max_attempts)
            processed += 1
            continue

        result = connector.push_field(product, item.field_name)
        if result.success:
            sync_queue_store.mark_success(item.id)
            sync_log_store.record_log(
                item.company_id, item.product_id, item.connector_name, item.field_name,
                old_value=item.old_value, new_value=item.new_value, direction="export", source="electrograder",
            )
        else:
            sync_queue_store.mark_failed(item.id, result.message, item.attempts, item.max_attempts)
        processed += 1
    return processed


# Fields it's actually safe to write back onto a Product from a Pull —
# deliberately NOT the full SYNC_OWNERSHIP_FIELDS set. Product.status is the
# grading workflow field (draft/in_progress/completed); the marketplace
# listing/sale "status" concept in SYNC_OWNERSHIP_FIELDS has no
# corresponding Product attribute, so it's decided/logged for visibility
# but never written back — writing it would silently corrupt the grading
# workflow's own status field.
_PULL_APPLICABLE_FIELDS = {"quantity", "price"}


def pull_product(company_id: str, product, connector_name: str) -> list:
    """connector.pull_state(product) returns only BaseLinker-owned
    operational fields (quantity/price/status — see field_reader.py's
    scope). Each one is decided via conflict_resolver.resolve_field_change()
    — an accepted/overridden result is only written back to the Product
    (and logged to product_change_log with source_system="baselinker") when
    the field is in _PULL_APPLICABLE_FIELDS; a field with no safe Product
    attribute (status) is still resolved and sync-logged for visibility,
    just never applied. Returns the list of FieldChangeResolution."""
    from modules import inventory_store, product_change_log_store
    from sync import conflict_resolver

    try:
        connector = IntegrationManager.get(company_id, connector_name)
    except (IntegrationNotConnectedError, IntegrationNotAvailableError):
        return []

    remote_state = connector.pull_state(product)
    resolutions = []
    for field_name, remote_value in remote_state.items():
        local_value = getattr(product, field_name, None)
        resolution = conflict_resolver.resolve_field_change(
            company_id, product.id, connector_name, field_name,
            changed_in=connector_name, electrograder_value=local_value, connector_value=remote_value,
        )
        resolutions.append(resolution)

        if not resolution.accepted or field_name not in _PULL_APPLICABLE_FIELDS:
            continue
        if resolution.applied_value == local_value:
            continue

        setattr(product, field_name, resolution.applied_value)
        inventory_store.save_product(product)
        product_change_log_store.record_change(
            company_id, product.id, field_name, str(local_value), str(resolution.applied_value),
            source_system=product_change_log_store.SOURCE_BASELINKER, changed_by="system:pull_sync",
        )
    return resolutions
