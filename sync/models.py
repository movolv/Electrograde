"""Pure domain model for one sync attempt's outcome — no DB, no Streamlit,
no knowledge of any specific marketplace. Persistence lives in
modules/sync_record_store.py (this codebase's convention: modules/ = state,
sync/ + integrations/ = orchestration); this module is just the shape.
"""
import time
import uuid
from dataclasses import dataclass, field

from sync.status import DIRECTION_EXPORT, STATUS_DISABLED, STATUS_FAILED, STATUS_PENDING, STATUS_SUCCESS


@dataclass
class SyncRecord:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    product_id: str = ""
    connector_name: str = ""  # e.g. "baselinker" — matches integration_type elsewhere
    direction: str = DIRECTION_EXPORT  # DIRECTION_EXPORT | DIRECTION_IMPORT
    sync_status: str = STATUS_PENDING  # STATUS_PENDING|SUCCESS|FAILED|DISABLED
    last_sync_time: float = 0.0
    error_message: str = ""
    external_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def mark(self, success: bool, message: str = "", external_id: str = "", disabled: bool = False) -> None:
        """Convenience mutator used by sync/engine.py right before
        persisting — keeps the status/timestamp/error bookkeeping in one
        place instead of repeated at every call site."""
        if disabled:
            self.sync_status = STATUS_DISABLED
        else:
            self.sync_status = STATUS_SUCCESS if success else STATUS_FAILED
        self.error_message = "" if success else message
        if external_id:
            self.external_id = external_id
        self.last_sync_time = time.time()
        self.updated_at = self.last_sync_time
