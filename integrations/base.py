"""Shared interfaces every marketplace/service connector implements.
app.py and integrations/manager.py only ever talk to these — never to a
concrete connector class directly — so adding eBay/Amazon/DeepL-translate/
etc. later means adding a class here-shaped, not touching call sites.

IntegrationConnector
  -> MarketplaceConnector (BaseLinker, eBay, Amazon, Allegro, Tradera, WooCommerce, ...)
  -> ServiceConnector     (DeepL, AI, shipping, ...)

A connector is constructed fresh per call (company_id, credentials,
settings passed in from modules/integration_store.py's decrypted row) —
it holds no state beyond that, so there's nothing to leak between
requests/companies.
"""
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ConnectionTestResult:
    success: bool
    message: str = ""


@dataclass
class ConnectorActionResult:
    success: bool
    external_id: str = ""
    message: str = ""
    data: dict = field(default_factory=dict)


class IntegrationConnector(ABC):
    """Base for every connector, marketplace or service alike."""

    integration_type: str = ""
    integration_category: str = ""

    def __init__(self, company_id: str, credentials: dict, settings: Optional[dict] = None):
        self.company_id = company_id
        self.credentials = credentials or {}
        self.settings = settings or {}

    @abstractmethod
    def connect(self) -> ConnectionTestResult:
        """Offline validation only — required fields present, well-formed.
        Called before test_connection() by manager.connect() so a typo'd
        field fails fast without spending an API call."""
        raise NotImplementedError

    @abstractmethod
    def test_connection(self) -> ConnectionTestResult:
        """One real, cheap, read-only API call proving the credentials
        actually work against the live service."""
        raise NotImplementedError

    def disconnect(self) -> None:
        """No-op by default — most integrations have nothing to tell the
        remote side when disconnecting locally. Override only if a
        connector's API has a real "revoke" step."""
        return None


class MarketplaceConnector(IntegrationConnector):
    """A channel a Product can be listed/sold on."""

    integration_category = "marketplace"

    # Technical target-field key -> human-readable label, for the Settings
    # -> Integrations -> Field Mapping tab's "Target field" dropdown (see
    # integrations/manager.get_supported_target_fields()). Empty in the
    # base class; each concrete connector declares its own (different
    # platforms use different technical field names for the same concept —
    # e.g. BaseLinker's condition_id vs. eBay's condition). app.py reads
    # this off the connector class, never hardcodes a specific integration's
    # field names, so adding a new marketplace never touches the Field
    # Mapping UI itself.
    SUPPORTED_TARGET_FIELDS: dict = {}

    # Which integrations/field_registry.SYNCABLE_FIELDS keys this
    # connector's payload-building ACTUALLY gates on today — i.e. whose
    # checkbox state in the Synchronization tab has a real effect. Empty in
    # the base class; a field absent from this set still shows up in the
    # UI (so it can be saved for a future phase) but app.py renders a
    # "(not yet applied to export)" note next to it, computed generically
    # from whichever connector is open — never hardcoded per integration.
    IMPLEMENTED_SYNC_FIELDS: set = set()

    # Subset of IMPLEMENTED_SYNC_FIELDS (by convention, though not
    # enforced) turned ON by default the first time a company connects this
    # integration — see integrations/manager.py's connect(). Empty in the
    # base class.
    DEFAULT_SYNC_FIELDS: list = []

    def preview_payload(self, product) -> dict:
        """What export_product() would actually send, built via the exact
        same code path as the real push (never a separate summary that
        could drift) — used by Settings -> Integrations' "Preview export".
        {} means this connector has nothing preview-able yet; app.py treats
        that as "no preview available" rather than an error."""
        return {}

    @abstractmethod
    def create_product(self, product) -> ConnectorActionResult:
        raise NotImplementedError

    @abstractmethod
    def update_product(self, product, external_id: str) -> ConnectorActionResult:
        raise NotImplementedError

    @abstractmethod
    def delete_product(self, external_id: str) -> ConnectorActionResult:
        raise NotImplementedError

    @abstractmethod
    def sync_inventory(self, product, external_id: str) -> ConnectorActionResult:
        raise NotImplementedError

    def export_product(self, product) -> ConnectorActionResult:
        """The one call site app.py actually uses:
        IntegrationManager.get(company_id, "baselinker").export_product(product).
        Every marketplace connector gets this for free — decide create vs.
        update from the existing marketplace_store listing, persist the
        result, and log the attempt. Individual connectors only implement
        the four primitives above."""
        from modules import integration_store, marketplace_store

        existing = marketplace_store.get_listing(product.id, self.integration_type, self.company_id)
        existing_id = existing.external_listing_id if existing else ""

        try:
            if existing_id:
                result = self.update_product(product, existing_id)
            else:
                result = self.create_product(product)
        except Exception as e:
            integration_store.record_sync(
                self.company_id, self.integration_type, "export", product_id=product.id,
                status=integration_store.SYNC_STATUS_ERROR, error_message=str(e),
            )
            return ConnectorActionResult(success=False, message=f"Unexpected error: {e}")

        if result.success:
            marketplace_store.upsert_listing(
                marketplace_store.MarketplaceListing(
                    product_id=product.id,
                    marketplace=self.integration_type,
                    company_id=self.company_id,
                    external_listing_id=result.external_id or existing_id,
                    status=marketplace_store.STATUS_LISTED,
                    price=product.price,
                    last_synced_at=time.time(),
                )
            )
            integration_store.record_sync(
                self.company_id, self.integration_type, "export", product_id=product.id,
                external_id=result.external_id or existing_id, status=integration_store.SYNC_STATUS_SUCCESS,
            )
        else:
            integration_store.record_sync(
                self.company_id, self.integration_type, "export", product_id=product.id,
                status=integration_store.SYNC_STATUS_ERROR, error_message=result.message,
            )

        return result

    # ---- Phase 3.3 Connector Interface aliases --------------------------
    # Purely additive convenience names matching the generic
    # connect/test_connection/build_payload/validate/export/sync shape a
    # future connector (eBay/Amazon/etc.) is expected to implement — every
    # one of these just delegates to the existing, proven methods above.
    # Nothing here changes create_product/update_product/delete_product/
    # sync_inventory/export_product/preview_payload or any of their call
    # sites (app.py, Review & Export) — this is a second name for the same
    # thing, not a replacement.

    def validate(self) -> ConnectionTestResult:
        """Alias for connect() (offline field validation)."""
        return self.connect()

    def build_payload(self, product) -> dict:
        """Alias for preview_payload()."""
        return self.preview_payload(product)

    def export(self, product) -> ConnectorActionResult:
        """Alias for export_product()."""
        return self.export_product(product)

    def import_product(self, external_id: str) -> ConnectorActionResult:
        """The reverse of export_product() — pulls remote state (quantity/
        price/status) FROM this marketplace INTO ElectroGrader. Phase 3.2 is
        architecture-only: no connector implements this yet. Returns an
        honest "not implemented" failure rather than a fake success or a
        silent no-op — sync/engine.py records this as STATUS_DISABLED, not
        STATUS_FAILED, since nothing actually went wrong. A future connector
        overrides this method once a real pull-from-marketplace API call is
        wired up."""
        return ConnectorActionResult(
            success=False, message=f"Import sync not implemented yet for '{self.integration_type}'.",
        )

    def sync(self, product, external_id: str = "", direction: str = "export") -> ConnectorActionResult:
        """Universal two-way entry point — sync/engine.py's sync_product()
        calls this rather than export_product()/import_product() directly,
        so the Sync Engine never needs to know which direction maps to
        which underlying method."""
        if direction == "import":
            return self.import_product(external_id)
        return self.export_product(product)

    # ---- Real two-way sync (Push/Pull) --------------------------------

    def pull_state(self, product) -> dict:
        """Fetches this connector's current operational field values
        (quantity/price/status — never product-intelligence fields) for an
        existing listing. {} means "nothing pull-able yet" — the default
        for a connector that hasn't implemented real Pull."""
        return {}

    def push_field(self, product, field_name: str, existing_listing_id: str = "") -> ConnectorActionResult:
        """Pushes exactly one field's current value. Default delegates to
        a full export_product() — a safe fallback for a connector with no
        granular single-field push of its own."""
        return self.export_product(product)


class ServiceConnector(IntegrationConnector):
    """A non-marketplace external service (translation, AI, shipping...).
    Each method raises NotImplementedError by default; a concrete service
    only overrides the ones it actually offers (DeepL overrides
    translate() only)."""

    integration_category = "service"

    def translate(self, text: str, target_lang: str, source_lang: Optional[str] = None) -> str:
        raise NotImplementedError(f"{self.integration_type} does not support translate()")

    def process(self, *args, **kwargs):
        raise NotImplementedError(f"{self.integration_type} does not support process()")

    def calculate(self, *args, **kwargs):
        raise NotImplementedError(f"{self.integration_type} does not support calculate()")
