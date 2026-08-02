"""Sync Engine Logic — decides what happens when a field's value changed on
ONE side (ElectroGrader or the connector) and might disagree with the
other. This is the "Step 1/2/3" pipeline: receive a change, check
ownership, apply the rule. Pure decision-making — it never calls a
connector or makes a network request; the caller (today: only
scripts/verify_two_way_sync.py; in the future: a real BaseLinker
incoming-change poller/webhook, once import_product() is genuinely
implemented for a connector) is responsible for actually knowing both
sides' current values. Nothing in the live app calls this yet — the same
"prepared, not wired" precedent as sync/engine.py's
get_export_field_owners().

Belongs in sync/ (not integrations/) because it orchestrates ABOVE a single
connector: it reads Sync Ownership configuration and writes sync_logs/
sync_conflicts, neither of which integrations/ knows about.
"""
from dataclasses import dataclass
from typing import Optional

from modules import sync_log_store, sync_ownership_store
from modules.sync_ownership_store import CONFLICT_MANUAL_REVIEW

ACTION_ACCEPTED = "accepted"
ACTION_OVERRIDDEN = "overridden"
ACTION_PENDING_MANUAL_REVIEW = "pending_manual_review"


@dataclass
class FieldChangeResolution:
    field_name: str
    accepted: bool  # was a value actually applied (True for accepted/overridden, False for pending review)
    resolution_action: str  # ACTION_ACCEPTED | ACTION_OVERRIDDEN | ACTION_PENDING_MANUAL_REVIEW
    applied_value: Optional[object]
    conflict_created: bool


def resolve_field_change(
    company_id: str, product_id: str, connector_name: str, field_name: str,
    changed_in: str, electrograder_value, connector_value,
) -> FieldChangeResolution:
    """changed_in: which side actually made the change — "electrograder" or
    connector_name. electrograder_value/connector_value: both sides'
    CURRENT values (the caller already knows these; this function doesn't
    fetch anything)."""
    owner = sync_ownership_store.get_field_owner(company_id, connector_name, field_name)

    if changed_in == owner:
        # The legitimate owner made the change — accept it outright, no conflict.
        applied = electrograder_value if changed_in == "electrograder" else connector_value
        direction = sync_ownership_store.resolve_flow(owner, connector_name)
        sync_log_store.record_log(
            company_id, product_id, connector_name, field_name,
            old_value=str(connector_value if changed_in == "electrograder" else electrograder_value),
            new_value=str(applied), direction=direction, source=changed_in,
        )
        return FieldChangeResolution(field_name, True, ACTION_ACCEPTED, applied, False)

    # The non-owning side changed the field — consult conflict_policy.
    policy = sync_ownership_store.get_conflict_policy(company_id, connector_name, field_name)

    if policy == CONFLICT_MANUAL_REVIEW:
        sync_ownership_store.record_conflict(
            company_id, product_id, field_name,
            electrograder_value=str(electrograder_value), baselinker_value=str(connector_value), resolution="",
        )
        sync_log_store.record_log(
            company_id, product_id, connector_name, field_name,
            old_value=str(electrograder_value if changed_in != "electrograder" else connector_value),
            new_value="(pending review)", direction=policy, source=changed_in,
        )
        return FieldChangeResolution(field_name, False, ACTION_PENDING_MANUAL_REVIEW, None, True)

    applied = electrograder_value if policy == "electrograder" else connector_value
    sync_ownership_store.record_conflict(
        company_id, product_id, field_name,
        electrograder_value=str(electrograder_value), baselinker_value=str(connector_value), resolution=policy,
    )
    sync_log_store.record_log(
        company_id, product_id, connector_name, field_name,
        old_value=str(electrograder_value if policy != "electrograder" else connector_value),
        new_value=str(applied), direction=policy, source=changed_in,
    )
    return FieldChangeResolution(field_name, True, ACTION_OVERRIDDEN, applied, True)
