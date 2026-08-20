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
from integrations.services.ai.client import AiServiceConnector
from integrations.services.deepl.client import DeepLConnector
from modules import audit_store, integration_store, sync_rules_store


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
    # Brand appearance, carried by the catalog entry itself so a new
    # integration brings its own look with it — nothing in Settings, the
    # Product List's Integrations column, or any future surface needs a
    # per-integration branch or its own copy of this. `brand_parts` is the
    # wordmark as ((text, css_color), ...) segments, used whenever no real
    # logo file exists at static/integration_logos/<type>.png;
    # `brand_weight` is its font-weight, `brand_short` the 1-2 letter
    # fallback for spaces too small for a wordmark (e.g. the compact icons
    # in the Product List grid).
    brand_parts: tuple = ()
    brand_weight: int = 700
    brand_short: str = ""


CONNECTORS = {
    "baselinker": BaselinkerConnector,
    "deepl": DeepLConnector,
    "openai": AiServiceConnector,
}

CATALOG = [
    CatalogEntry(
        "baselinker", integration_store.CATEGORY_MARKETPLACE, "BaseLinker", True,
        ui_group="ERP", description="Multichannel inventory & order sync (Base.com)",
        keywords=("base.com", "multichannel", "inventory", "orders", "sync"),
        brand_parts=(("base", "#111111"), (".", "#2f6fed")), brand_weight=800, brand_short="B",
    ),
    CatalogEntry(
        "ebay", integration_store.CATEGORY_MARKETPLACE, "eBay", False,
        ui_group="Marketplace", description="Sell on the eBay marketplace",
        keywords=("auction", "marketplace"),
        brand_parts=(("e", "#e53238"), ("b", "#0064d2"), ("a", "#f5af02"), ("y", "#86b817")),
        brand_weight=800, brand_short="eB",
    ),
    CatalogEntry(
        "amazon", integration_store.CATEGORY_MARKETPLACE, "Amazon", False,
        ui_group="Marketplace", description="Sell on the Amazon marketplace",
        keywords=("marketplace", "fba"),
        brand_parts=(("amazon", "#111111"),), brand_weight=700, brand_short="A",
    ),
    CatalogEntry(
        "allegro", integration_store.CATEGORY_MARKETPLACE, "Allegro", False,
        ui_group="Marketplace", description="Sell on Allegro, Poland's largest marketplace",
        keywords=("marketplace", "poland"),
        brand_parts=(("allegro", "#ff5a00"),), brand_weight=800, brand_short="Al",
    ),
    CatalogEntry(
        "tradera", integration_store.CATEGORY_MARKETPLACE, "Tradera", False,
        ui_group="Marketplace", description="Sell on Tradera, Sweden's largest marketplace",
        keywords=("marketplace", "sweden", "auction"),
        brand_parts=(("tradera", "#1a7a3c"),), brand_weight=800, brand_short="Tr",
    ),
    CatalogEntry(
        "woocommerce", integration_store.CATEGORY_MARKETPLACE, "WooCommerce", False,
        ui_group="Store", description="Sync products to your own WooCommerce store",
        keywords=("wordpress", "store", "ecommerce"),
        brand_parts=(("woo", "#7f54b3"),), brand_weight=800, brand_short="Wo",
    ),
    CatalogEntry(
        "deepl", integration_store.CATEGORY_SERVICE, "DeepL Translate", True,
        ui_group="Communication", description="Automatic listing translation",
        keywords=("translate", "language", "ai"),
        brand_parts=(("Deep", "#0f2b46"), ("L", "#0f6fff")), brand_weight=800, brand_short="DL",
    ),
    CatalogEntry(
        "openai", integration_store.CATEGORY_SERVICE, "AI Assistant", True,
        ui_group="AI", description="AI-assisted descriptions & automation",
        keywords=("ai", "assistant", "gpt", "translate", "translation"),
        brand_parts=(("AI Assistant", "#111111"),), brand_weight=700, brand_short="AI",
    ),
    CatalogEntry(
        "dhl", integration_store.CATEGORY_SERVICE, "DHL Shipping", False,
        ui_group="Shipping", description="DHL shipping labels & rates",
        keywords=("shipping", "courier", "labels"),
        brand_parts=(("DHL", "#d40511"),), brand_weight=900, brand_short="DH",
    ),
    CatalogEntry(
        "dpd", integration_store.CATEGORY_SERVICE, "DPD Shipping", False,
        ui_group="Shipping", description="DPD shipping labels & rates",
        keywords=("shipping", "courier", "labels"),
        brand_parts=(("DPD", "#dc0032"),), brand_weight=900, brand_short="DP",
    ),
]

_CATALOG_BY_TYPE = {entry.integration_type: entry for entry in CATALOG}


def _require_available(integration_type: str) -> CatalogEntry:
    entry = _CATALOG_BY_TYPE.get(integration_type)
    if entry is None or not entry.available:
        raise IntegrationNotAvailableError(f"'{integration_type}' is not available yet.")
    return entry


def get_branding(integration_type: str) -> dict:
    """Everything any UI needs to DRAW an integration — display name (also
    its tooltip/accessible label), wordmark segments, font weight, and a
    short 1-2 letter fallback. The single source of truth for integration
    appearance, so adding an integration to CATALOG is enough for it to
    render correctly everywhere (Settings cards, the Product List's
    Integrations column, anything added later) with no per-integration
    branch anywhere.

    An unknown/never-cataloged integration_type still returns something
    usable — a title-cased name and neutral grey wordmark — rather than
    raising, so a listing row referencing a since-removed integration
    degrades gracefully instead of breaking the page that renders it."""
    entry = _CATALOG_BY_TYPE.get(integration_type)
    if entry is None:
        label = integration_type.replace("_", " ").title()
        return {
            "integration_type": integration_type, "display_name": label,
            "parts": [[label, "#6b7280"]], "weight": 600,
            "short": (integration_type[:2] or "?").upper(),
        }
    parts = [[text, color] for text, color in entry.brand_parts] or [[entry.display_name, "#6b7280"]]
    return {
        "integration_type": entry.integration_type,
        "display_name": entry.display_name,
        "parts": parts,
        "weight": entry.brand_weight,
        "short": entry.brand_short or entry.display_name[:2].upper(),
    }


def get_supported_target_fields(integration_type: str, company_id: str = "") -> dict:
    """Field Mapping tab's 'Target field' dropdown source. {} for service
    connectors (e.g. DeepL) or unrecognized types — the UI treats that as
    "nothing to map for this integration."

    With `company_id` and an actually-connected integration, builds a real
    connector instance and calls its get_target_fields() — for BaseLinker
    this fetches the company's own custom "extra fields" live (see
    BaselinkerConnector.get_target_fields), not just the static fields
    every company would otherwise be limited to. Without `company_id` (or
    if not connected, or the live call fails), falls back to the
    connector CLASS's static STRUCTURAL_TARGET_FIELDS +
    SUPPORTED_TARGET_FIELDS (the exact same two dicts get_target_fields()
    itself starts from before merging in anything live) — never just
    SUPPORTED_TARGET_FIELDS alone, or a not-yet-connected company would
    see its own default mappings (brand -> features:brand, etc.) resolve
    to "unknown" purely because the class-level structural targets were
    left out of this fallback. Never a hardcoded per-integration branch
    here, and never an exception raised up to the UI just because a live
    fetch didn't work."""
    connector_cls = CONNECTORS.get(integration_type)
    if connector_cls is None:
        return {}
    if company_id:
        try:
            if is_connected(company_id, integration_type):
                return get(company_id, integration_type).get_target_fields()
        except Exception:
            pass  # fall through to the static class-level lists below
    return {
        **dict(getattr(connector_cls, "STRUCTURAL_TARGET_FIELDS", {})),
        **dict(getattr(connector_cls, "SUPPORTED_TARGET_FIELDS", {})),
    }


def get_external_categories(integration_type: str, company_id: str) -> list:
    """Category Mapping tab's dropdown source — [{"id", "label"}, ...],
    always a live call (see MarketplaceConnector.get_external_categories'
    docstring on why no caching). [] for a not-connected integration, an
    unrecognized type, or any live-call failure — the UI treats that as
    "nothing to map yet", never an exception bubbling up."""
    connector_cls = CONNECTORS.get(integration_type)
    if connector_cls is None or not company_id:
        return []
    try:
        if is_connected(company_id, integration_type):
            return get(company_id, integration_type).get_external_categories()
    except Exception:
        pass
    return []


def get_default_structural_mapping(integration_type: str) -> dict:
    """SYNCABLE_FIELDS key -> the connector's own STRUCTURAL_TARGET_FIELDS
    key it lands at TODAY with no rule saved — see
    BaselinkerConnector.DEFAULT_STRUCTURAL_MAPPING. Used to pre-fill the
    Field Mapping tab's editable table with ordinary, changeable rows the
    first time a company opens it, instead of either an empty table or a
    separate read-only section — see app.py's _render_field_mapping_tab.
    Always the static class-level dict (never a live call): which
    structural field something lands at by default doesn't depend on the
    connected account, so there's nothing live to fetch here."""
    connector_cls = CONNECTORS.get(integration_type)
    if connector_cls is None:
        return {}
    return dict(getattr(connector_cls, "DEFAULT_STRUCTURAL_MAPPING", {}))


def get_mappable_source_fields(integration_type: str, company_id: str = "") -> set:
    """Field Mapping tab's 'Source field' dropdown source — every
    integrations/field_registry.SYNCABLE_FIELDS key with mappable=True,
    minus this connector class's own CORE_FIELDS (see
    MarketplaceConnector.get_mappable_source_fields()'s docstring — same
    computation, done here at the class level since CORE_FIELDS never
    depends on a live connection/company, so no connector instance is
    needed), PLUS — when `company_id` is given — that company's own
    self-service custom fields (modules/custom_field_store.py), namespaced
    "custom:<key>". A plain DB read, not a live API call, so this stays
    available even for a not-yet-connected integration. Fields excluded
    either way have a fixed, hardcoded destination: offering them as a
    pickable source would be a dead end."""
    from integrations import field_registry
    from modules import custom_field_store

    connector_cls = CONNECTORS.get(integration_type)
    if connector_cls is None:
        return set()
    registry_mappable = {k for k, v in field_registry.SYNCABLE_FIELDS.items() if v.get("mappable")}
    result = registry_mappable - getattr(connector_cls, "CORE_FIELDS", set())
    if company_id:
        result |= {f"custom:{d.key}" for d in custom_field_store.list_fields(company_id)}
    return result


def get_implemented_sync_fields(integration_type: str) -> set:
    """Which integrations/field_registry.SYNCABLE_FIELDS keys this
    integration's connector actually gates on today — the Synchronization
    tab uses this (never a hardcoded per-integration list) to caption any
    field whose checkbox doesn't yet do anything real."""
    connector_cls = CONNECTORS.get(integration_type)
    if connector_cls is None:
        return set()
    return set(getattr(connector_cls, "IMPLEMENTED_SYNC_FIELDS", set()))


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

    # Seed a sensible default Synchronization configuration the first time
    # this company connects this integration_type — universal (reads
    # whatever the connector class itself declares), never special-cased
    # per integration. Only fires when no SyncRule exists at all yet, so
    # reconnecting an already-configured integration never overwrites a
    # company's real choices.
    if sync_rules_store.get_rule(company_id, integration_type) is None:
        default_fields = list(getattr(connector_cls, "DEFAULT_SYNC_FIELDS", []))
        sync_rules_store.upsert_rule(
            sync_rules_store.SyncRule(
                company_id=company_id, integration_type=integration_type,
                fields_send=default_fields, fields_send_configured=bool(default_fields),
            )
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


CHECK_LIMIT_REACHED = "limit_reached"  # quota exhausted; no API call was made
CHECK_NOT_LINKED = "not_linked"        # no listing row for this (product, integration)


def check_product_connection(company_id: str, integration_type: str, product_id: str):
    """Manual "does this product still exist over there?" check for ONE
    product on ONE integration. Returns a ProductConnectionCheck whose
    status is a CHECK_* constant from integrations/base.py, or one of the
    two module-level ones above.

    Order of operations matters:
      1. resolve the listing WITHIN this company (a product_id belonging
         to another tenant simply has no listing here, so a caller can
         never check — or spend quota on — someone else's product);
      2. claim one unit of this company's daily quota, atomically;
      3. only then spend the API call;
      4. on SUCCESS, stamp last_verified_at.

    The quota is claimed before the call and never refunded on failure:
    a timeout still cost the marketplace a request, which is exactly what
    the limit exists to bound. A request rejected at step 2 never reaches
    the marketplace at all.

    Never deletes the association, whatever the outcome — that stays a
    deliberate user action (see marketplace_store.delete_listing())."""
    from integrations.base import (
        CHECK_AUTH_ERROR,
        CHECK_SUCCESS,
        CHECK_TEMPORARY_ERROR,
        ProductConnectionCheck,
    )
    from modules import integration_check_quota_store, marketplace_store

    listing = marketplace_store.get_listing(product_id, integration_type, company_id)
    if listing is None:
        return ProductConnectionCheck(status=CHECK_NOT_LINKED, message="No listing for this integration.")

    # Resolve the connector BEFORE claiming quota. A listing can outlive
    # its integration (disconnected since, or a type that isn't available
    # in this build), and a request that can never reach the marketplace
    # must not burn one of the company's daily checks.
    try:
        connector = get(company_id, integration_type)
    except IntegrationNotConnectedError as e:
        return ProductConnectionCheck(
            status=CHECK_AUTH_ERROR, external_id=listing.external_listing_id, message=str(e),
        )
    except IntegrationNotAvailableError as e:
        return ProductConnectionCheck(
            status=CHECK_TEMPORARY_ERROR, external_id=listing.external_listing_id, message=str(e),
        )

    allowed, used, limit = integration_check_quota_store.try_consume(company_id)
    if not allowed:
        return ProductConnectionCheck(
            status=CHECK_LIMIT_REACHED, external_id=listing.external_listing_id,
            message=f"{used}/{limit}",
        )

    result = connector.check_product_connection(listing.external_listing_id)

    if result.status == CHECK_SUCCESS:
        marketplace_store.mark_verified(product_id, integration_type, company_id)

    integration_store.record_sync(
        company_id, integration_type, "check_product_connection", product_id=product_id,
        status=(
            integration_store.SYNC_STATUS_SUCCESS if result.status == CHECK_SUCCESS
            else integration_store.SYNC_STATUS_ERROR
        ),
        error_message="" if result.status == CHECK_SUCCESS else f"{result.status}: {result.message}",
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
    get_default_structural_mapping = staticmethod(get_default_structural_mapping)
    get_mappable_source_fields = staticmethod(get_mappable_source_fields)
    get_implemented_sync_fields = staticmethod(get_implemented_sync_fields)
    get_external_categories = staticmethod(get_external_categories)
    get_branding = staticmethod(get_branding)
    check_product_connection = staticmethod(check_product_connection)
