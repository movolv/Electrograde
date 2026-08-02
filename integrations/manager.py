"""Single entry point app.py uses to reach any integration —
IntegrationManager.get(company_id, "baselinker").export_product(product) —
so nothing in app.py ever special-cases a specific marketplace/service.
Resolves a company's stored, decrypted credentials (modules/integration_store)
into a live connector instance (integrations/base.py) built fresh per call.

CATALOG drives Settings -> Integrations: every integration this app could
ever support is listed (including ones with no connector class yet), with
`available=False` entries rendered as visible-but-disabled "Coming soon".
CONNECTORS only needs entries for the ones actually available.
"""
from dataclasses import dataclass
from typing import Optional

from integrations.base import ConnectionTestResult, IntegrationConnector
from integrations.marketplaces.baselinker.client import BaselinkerConnector
from integrations.services.deepl.client import DeepLConnector
from modules import audit_store, integration_store


class IntegrationNotAvailableError(RuntimeError):
    """Raised for an integration_type with no connector built yet (or not
    yet flipped to available in CATALOG) — a "coming soon" entry."""


class IntegrationNotConnectedError(RuntimeError):
    """Raised when a company hasn't connected (or has disconnected) an
    otherwise-available integration."""


@dataclass(frozen=True)
class CatalogEntry:
    integration_type: str
    integration_category: str  # "marketplace" | "service" — drives connector typing, not UI
    display_name: str
    available: bool
    ui_group: str = "Other"  # finer-grained UI filter chip, e.g. "Marketplace"/"ERP"/"Shipping"
    description: str = ""  # one-line catalog blurb
    keywords: tuple = ()  # extra search terms beyond display_name


CONNECTORS = {
    "baselinker": BaselinkerConnector,
    "deepl": DeepLConnector,
}

CATALOG = [
    CatalogEntry(
        "baselinker", integration_store.CATEGORY_MARKETPLACE, "BaseLinker", True,
        ui_group="ERP", description="Multichannel inventory & order sync (Base.com)",
        keywords=("base.com", "multichannel", "inventory", "orders", "sync"),
    ),
    CatalogEntry(
        "ebay", integration_store.CATEGORY_MARKETPLACE, "eBay", False,
        ui_group="Marketplace", description="Sell on the eBay marketplace",
        keywords=("auction", "marketplace"),
    ),
    CatalogEntry(
        "amazon", integration_store.CATEGORY_MARKETPLACE, "Amazon", False,
        ui_group="Marketplace", description="Sell on the Amazon marketplace",
        keywords=("marketplace", "fba"),
    ),
    CatalogEntry(
        "allegro", integration_store.CATEGORY_MARKETPLACE, "Allegro", False,
        ui_group="Marketplace", description="Sell on Allegro, Poland's largest marketplace",
        keywords=("marketplace", "poland"),
    ),
    CatalogEntry(
        "tradera", integration_store.CATEGORY_MARKETPLACE, "Tradera", False,
        ui_group="Marketplace", description="Sell on Tradera, Sweden's largest marketplace",
        keywords=("marketplace", "sweden", "auction"),
    ),
    CatalogEntry(
        "woocommerce", integration_store.CATEGORY_MARKETPLACE, "WooCommerce", False,
        ui_group="Store", description="Sync products to your own WooCommerce store",
        keywords=("wordpress", "store", "ecommerce"),
    ),
    CatalogEntry(
        "deepl", integration_store.CATEGORY_SERVICE, "DeepL Translate", True,
        ui_group="Communication", description="Automatic listing translation",
        keywords=("translate", "language", "ai"),
    ),
    CatalogEntry(
        "openai", integration_store.CATEGORY_SERVICE, "AI Assistant", False,
        ui_group="AI", description="AI-assisted descriptions & automation",
        keywords=("ai", "assistant", "gpt"),
    ),
    CatalogEntry(
        "dhl", integration_store.CATEGORY_SERVICE, "DHL Shipping", False,
        ui_group="Shipping", description="DHL shipping labels & rates",
        keywords=("shipping", "courier", "labels"),
    ),
    CatalogEntry(
        "dpd", integration_store.CATEGORY_SERVICE, "DPD Shipping", False,
        ui_group="Shipping", description="DPD shipping labels & rates",
        keywords=("shipping", "courier", "labels"),
    ),
]

_CATALOG_BY_TYPE = {entry.integration_type: entry for entry in CATALOG}


def _require_available(integration_type: str) -> CatalogEntry:
    entry = _CATALOG_BY_TYPE.get(integration_type)
    if entry is None or not entry.available:
        raise IntegrationNotAvailableError(f"'{integration_type}' is not available yet.")
    return entry


def get_supported_target_fields(integration_type: str) -> dict:
    """Field Mapping tab's 'Target field' dropdown source — reads whatever
    the connector class itself declares (MarketplaceConnector.
    SUPPORTED_TARGET_FIELDS), never a hardcoded per-integration branch here.
    {} for service connectors (e.g. DeepL) or unrecognized types — the UI
    treats that as "nothing to map for this integration."""
    connector_cls = CONNECTORS.get(integration_type)
    if connector_cls is None:
        return {}
    return dict(getattr(connector_cls, "SUPPORTED_TARGET_FIELDS", {}))


def is_connected(company_id: str, integration_type: str) -> bool:
    record = integration_store.get_integration(company_id, integration_type)
    return record is not None and record.status == integration_store.STATUS_CONNECTED


def get(company_id: str, integration_type: str) -> IntegrationConnector:
    """Builds a live connector for an already-connected integration.
    Raises IntegrationNotAvailableError / IntegrationNotConnectedError
    rather than returning None, so a careless caller can't silently no-op —
    callers that need a soft check should call is_connected() first."""
    _require_available(integration_type)
    record = integration_store.get_integration(company_id, integration_type)
    if record is None or record.status != integration_store.STATUS_CONNECTED:
        raise IntegrationNotConnectedError(f"'{integration_type}' is not connected for this company.")
    connector_cls = CONNECTORS[integration_type]
    return connector_cls(company_id, record.credentials, record.settings)


def connect(
    company_id: str, integration_type: str, credentials: dict, settings: dict, user_id: str = "",
) -> ConnectionTestResult:
    """Validates offline, then does one real test_connection() call, and
    only persists (encrypted, via integration_store) on success — a failed
    attempt never overwrites a working existing connection."""
    entry = _require_available(integration_type)
    connector_cls = CONNECTORS[integration_type]
    connector = connector_cls(company_id, credentials, settings)

    offline = connector.connect()
    if not offline.success:
        return offline

    result = connector.test_connection()
    if not result.success:
        integration_store.record_sync(
            company_id, integration_type, "test_connection",
            status=integration_store.SYNC_STATUS_ERROR, error_message=result.message,
        )
        return result

    integration_store.upsert_integration(
        integration_store.CompanyIntegration(
            company_id=company_id, integration_type=integration_type,
            integration_category=entry.integration_category,
            status=integration_store.STATUS_CONNECTED,
            credentials=credentials, settings=settings,
        )
    )
    integration_store.record_sync(
        company_id, integration_type, "test_connection", status=integration_store.SYNC_STATUS_SUCCESS,
    )
    if user_id:
        audit_store.log_audit(
            company_id, user_id, "CONNECT_INTEGRATION", "integration", integration_type, details=result.message,
        )
    return result


def disconnect(company_id: str, integration_type: str, user_id: str = "") -> None:
    integration_store.disconnect_integration(company_id, integration_type)
    if user_id:
        audit_store.log_audit(company_id, user_id, "DISCONNECT_INTEGRATION", "integration", integration_type)


def test(company_id: str, integration_type: str) -> ConnectionTestResult:
    """Re-tests an already-connected integration (the Settings page's "Test
    connection" button) without touching stored credentials."""
    connector = get(company_id, integration_type)
    result = connector.test_connection()
    integration_store.record_sync(
        company_id, integration_type, "test_connection",
        status=integration_store.SYNC_STATUS_SUCCESS if result.success else integration_store.SYNC_STATUS_ERROR,
        error_message="" if result.success else result.message,
    )
    return result


class IntegrationManager:
    """Thin staticmethod façade over the module-level functions above,
    purely so call sites can spell it IntegrationManager.get(...) — every
    other store/service in this codebase is function-based; this class adds
    no logic of its own."""

    get = staticmethod(get)
    connect = staticmethod(connect)
    disconnect = staticmethod(disconnect)
    test = staticmethod(test)
    is_connected = staticmethod(is_connected)
    get_supported_target_fields = staticmethod(get_supported_target_fields)
