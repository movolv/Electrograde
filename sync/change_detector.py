"""Generic "a product field changed" event layer — the entry point any
part of ElectroGrader can call to record a field change and, if warranted,
enqueue a real Push. record_and_enqueue() takes only primitive arguments
and is not tied to any particular UI form or code path: a future call site
(manifest import, grading pipeline, AI processing, bulk updates) can call
the exact same function without any change here. This phase wires it into
exactly ONE call site (Inventory's product-edit save) to keep regression
risk low — that scope decision lives in app.py, not in this module.
"""
from modules import product_change_log_store, sync_ownership_store, sync_queue_store


def record_and_enqueue(
    company_id: str, product_id: str, connector_name: str, field_name: str,
    old_value, new_value, source_system: str, changed_by: str = "",
) -> bool:
    """Records the change to product_change_log always. Additionally
    enqueues a real Sync Queue Push ONLY when the field's owner is
    ElectroGrader itself AND the change originated on ElectroGrader's side
    — i.e. a legitimate owner-side edit. A change to a BaseLinker-owned or
    Manual field is still logged (for visibility) but never auto-enqueued,
    since pushing a non-owned field's value would silently overwrite the
    side that's actually supposed to be authoritative for it — the same
    guarantee sync/conflict_resolver.py already enforces for the reverse
    (Pull) direction. Returns True if a Sync Queue item was enqueued."""
    product_change_log_store.record_change(
        company_id, product_id, field_name, str(old_value), str(new_value),
        source_system=source_system, changed_by=changed_by,
    )

    if source_system != product_change_log_store.SOURCE_ELECTROGRADER:
        return False

    owner = sync_ownership_store.get_field_owner(company_id, connector_name, field_name)
    if owner != "electrograder":
        return False

    sync_queue_store.enqueue(
        company_id, product_id, connector_name, field_name,
        old_value=str(old_value), new_value=str(new_value), direction="export",
    )
    return True
