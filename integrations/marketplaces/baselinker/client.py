"""BaseLinker (now Base.com) connector — the one integration that has to
actually work end-to-end this pass. Replaces the old app-wide
modules/baselinker_client.py: credentials/settings now come from a
per-company modules/integration_store.CompanyIntegration row instead of
process-global env vars, so two companies' BaseLinker accounts are fully
independent (including rate limiting, keyed per API token below, not one
shared process-global timestamp like the old module had).

API docs consulted: https://api.baselinker.com/index.php?method=addInventoryProduct
                     https://api.baselinker.com/index.php?method=getInventoryProductsList
                     https://api.baselinker.com/index.php?method=getInventories
Endpoint: POST https://api.baselinker.com/connector.php, auth via the
X-BLToken header. Rate limit: 100 requests/minute (documented) — we stay
under 90/min per token.

Expected shapes (validated in connect()):
  credentials = {"token": "..."}
  settings = {"inventory_id": "...", "category_id": "...",
              "price_group_id": "..." (optional), "warehouse_id": "..." (optional),
              "tax_rate": "23" (optional, default 23)}
"""
import json
import time
from typing import Optional, Set

import requests

from integrations.base import ConnectionTestResult, ConnectorActionResult, ImportedProductData, MarketplaceConnector
from integrations.marketplaces.baselinker import import_products, mapper, sync
from modules import sync_rules_store

API_URL = "https://api.baselinker.com/connector.php"
_MIN_CALL_INTERVAL = 60.0 / 90  # stay safely under the 100 req/min limit

_last_call_at: dict = {}  # keyed by API token, not one shared global — see module docstring


class BaseLinkerAPIError(RuntimeError):
    pass


def _rate_limit_wait(token: str) -> None:
    last = _last_call_at.get(token, 0.0)
    elapsed = time.time() - last
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)
    _last_call_at[token] = time.time()


def _call(method: str, parameters: dict, token: str) -> dict:
    _rate_limit_wait(token)
    resp = requests.post(
        API_URL,
        headers={"X-BLToken": token},
        data={"method": method, "parameters": json.dumps(parameters)},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "ERROR":
        raise BaseLinkerAPIError(f"{method} failed: {data.get('error_message', data)}")
    return data


def get_product_data(product_ids: list, config: dict) -> dict:
    """Real getInventoryProductsData call — returns the raw "products" dict
    keyed by product_id, each holding prices/stock/etc. for the configured
    inventory_id. The one real API call both get_product() and
    get_inventory() (spec-named aliases) are built on — BaseLinker has no
    separate single-product-fetch or stock-only endpoint worth using
    instead."""
    data = _call(
        "getInventoryProductsData",
        {"inventory_id": config["inventory_id"], "products": [int(p) for p in product_ids]},
        config["token"],
    )
    return data.get("products", {}) or {}


def get_inventory(product_ids: list, config: dict) -> dict:
    """Spec-named alias for get_product_data() — same real call."""
    return get_product_data(product_ids, config)


def list_all_product_ids(config: dict) -> list:
    """Paginates getInventoryProductsList (page=1,2,...) until a page comes
    back with no products, collecting every product_id for this
    inventory_id — the same real endpoint _find_existing_by_sku() already
    uses (there, narrowed with filter_sku; here, unfiltered)."""
    ids: list = []
    page = 1
    while True:
        data = _call(
            "getInventoryProductsList",
            {"inventory_id": config["inventory_id"], "page": page},
            config["token"],
        )
        products = data.get("products") or {}
        if not products:
            break
        ids.extend(str(pid) for pid in products)
        page += 1
    return ids


def get_orders(config: dict, date_confirmed_from: float) -> list:
    """Real getOrders call. Prepared but not wired into any Pull decision
    this pass — no SYNC_OWNERSHIP_FIELDS field maps to order data yet."""
    data = _call(
        "getOrders", {"date_confirmed_from": int(date_confirmed_from)}, config["token"],
    )
    return data.get("orders", []) or []


class BaselinkerConnector(MarketplaceConnector):
    integration_type = "baselinker"

    # Phase 1: just the dropdown source for Settings -> Field Mapping.
    # Nothing reads/applies these yet — mapper.py's build_payload() is
    # unchanged; real field-mapping application is Phase 2 work.
    SUPPORTED_TARGET_FIELDS = {
        "condition_id": "Condition",
        "category_id": "Category",
    }

    # Fields whose Synchronization-tab checkbox actually gates something in
    # mapper.build_payload() today. "sku" is deliberately absent — it's
    # always sent regardless of toggle state (see mapper.py), so its
    # checkbox wouldn't do anything; "brand"/"model"/"category"/"grade"/
    # "defects" have no payload destination yet (category stays the single
    # global category_id setting; grade/defects mapping-application is
    # future work) — see integrations/field_registry.py for the full field
    # list these are a subset of.
    IMPLEMENTED_SYNC_FIELDS = {
        "name", "product_description", "condition_description",
        "image_paths", "price", "quantity", "barcode",
    }
    DEFAULT_SYNC_FIELDS = [
        "name", "product_description", "condition_description",
        "image_paths", "price", "quantity", "sku", "barcode",
    ]

    def _resolve_fields_send(self) -> Optional[Set[str]]:
        """None => mapper.build_payload() sends everything (legacy
        behavior) — used both for companies with no SyncRule at all, and
        for one whose Synchronization tab has never actually been saved
        (fields_send_configured=False), so an empty/never-touched row can
        never accidentally suppress real fields."""
        rule = sync_rules_store.get_rule(self.company_id, self.integration_type)
        if rule is None or not rule.fields_send_configured:
            return None
        return set(rule.fields_send)

    def preview_payload(self, product) -> dict:
        """Same build_payload() call the real push uses, minus actually
        base64-encoding images (wasteful for a preview) — a synthetic
        `_preview_image_count` key tells the UI how many WOULD be sent
        instead."""
        config = self._config()
        fields_send = self._resolve_fields_send()
        payload = mapper.build_payload(
            product, config, existing_listing_id=None, fields_send=fields_send, include_images=False,
        )
        wants_images = fields_send is None or "image_paths" in fields_send
        payload["_preview_image_count"] = len(product.image_paths) if wants_images and product.image_paths else 0
        return payload

    def _config(self) -> dict:
        settings = self.settings
        return {
            "token": self.credentials.get("token", ""),
            "inventory_id": int(settings["inventory_id"]),
            "category_id": int(settings["category_id"]),
            "price_group_id": settings.get("price_group_id") or None,
            "warehouse_id": settings.get("warehouse_id") or None,
            "tax_rate": float(settings.get("tax_rate") or 23),
        }

    def connect(self) -> ConnectionTestResult:
        if not self.credentials.get("token"):
            return ConnectionTestResult(False, "API token is required.")
        if not self.settings.get("inventory_id"):
            return ConnectionTestResult(False, "Inventory ID is required.")
        if not self.settings.get("category_id"):
            return ConnectionTestResult(False, "Category ID is required.")
        try:
            self._config()
        except (KeyError, ValueError) as e:
            return ConnectionTestResult(False, f"Invalid configuration: {e}")
        return ConnectionTestResult(True, "Configuration looks valid.")

    def test_connection(self) -> ConnectionTestResult:
        pre = self.connect()
        if not pre.success:
            return pre
        config = self._config()
        try:
            data = _call("getInventories", {}, config["token"])
        except (BaseLinkerAPIError, requests.RequestException) as e:
            return ConnectionTestResult(False, f"Could not reach BaseLinker: {e}")

        inventories = {str(inv.get("inventory_id")) for inv in data.get("inventories", [])}
        if str(config["inventory_id"]) not in inventories:
            return ConnectionTestResult(
                False, f"Token is valid, but inventory_id {config['inventory_id']} was not found on this account."
            )
        return ConnectionTestResult(True, "Connected — token and inventory ID are valid.")

    def _find_existing_by_sku(self, sku: str, config: dict) -> Optional[str]:
        """Safety net: before creating, check whether BaseLinker already has
        this SKU (e.g. if the local DB was ever reset) so a push updates
        instead of duplicating. Kept here rather than in the generic
        export_product() since it's a BaseLinker-specific fallback, not
        every marketplace needs (or can do) a SKU lookup."""
        if not sku:
            return None
        try:
            data = _call(
                "getInventoryProductsList",
                {"inventory_id": config["inventory_id"], "filter_sku": sku},
                config["token"],
            )
        except BaseLinkerAPIError:
            return None
        for product_id in (data.get("products") or {}):
            return str(product_id)
        return None

    def create_product(self, product) -> ConnectorActionResult:
        config = self._config()
        found_id = self._find_existing_by_sku(product.sku, config)
        if found_id:
            return self.update_product(product, found_id)

        try:
            payload = mapper.build_payload(
                product, config, existing_listing_id=None, fields_send=self._resolve_fields_send(),
            )
            data = _call("addInventoryProduct", payload, config["token"])
        except (BaseLinkerAPIError, requests.RequestException) as e:
            return ConnectorActionResult(success=False, message=str(e))

        return ConnectorActionResult(
            success=True, external_id=str(data.get("product_id", "")),
            message="Created.", data={"warnings": data.get("warnings", {}) or {}},
        )

    def update_product(self, product, external_id: str) -> ConnectorActionResult:
        config = self._config()
        try:
            payload = mapper.build_payload(
                product, config, existing_listing_id=external_id, fields_send=self._resolve_fields_send(),
            )
            data = _call("addInventoryProduct", payload, config["token"])
        except (BaseLinkerAPIError, requests.RequestException) as e:
            return ConnectorActionResult(success=False, external_id=external_id, message=str(e))

        return ConnectorActionResult(
            success=True, external_id=str(data.get("product_id", external_id)),
            message="Updated.", data={"warnings": data.get("warnings", {}) or {}},
        )

    def delete_product(self, external_id: str) -> ConnectorActionResult:
        config = self._config()
        try:
            _call("deleteInventoryProduct", {"product_id": int(external_id)}, config["token"])
        except (BaseLinkerAPIError, requests.RequestException) as e:
            return ConnectorActionResult(success=False, external_id=external_id, message=str(e))
        return ConnectorActionResult(success=True, external_id=external_id, message="Deleted.")

    def sync_inventory(self, product, external_id: str) -> ConnectorActionResult:
        # BaseLinker's addInventoryProduct is a full upsert — no separate
        # stock-only endpoint worth using here.
        return self.update_product(product, external_id)

    # ---- Real two-way sync (Push/Pull) --------------------------------

    def pull_state(self, product) -> dict:
        """Real Pull: fetch BaseLinker's current operational state
        (quantity/price/status only — see field_reader.py) for this
        product's existing listing. {} if there's no listing yet (nothing
        to pull) or the listing lookup fails."""
        from modules import marketplace_store

        listing = marketplace_store.get_listing(product.id, self.integration_type, self.company_id)
        if listing is None or not listing.external_listing_id:
            return {}
        config = self._config()
        try:
            raw = sync.pull_state(listing.external_listing_id, config)
        except (BaseLinkerAPIError, requests.RequestException):
            return {}
        return raw

    def push_field(self, product, field_name: str, existing_listing_id: str = "") -> ConnectorActionResult:
        """Real single-field Push — used by sync/engine.py's
        process_sync_queue() instead of a full export_product() so a
        Sync Queue row only sends the one field it recorded."""
        from modules import marketplace_store

        config = self._config()
        listing_id = existing_listing_id
        if not listing_id:
            listing = marketplace_store.get_listing(product.id, self.integration_type, self.company_id)
            listing_id = listing.external_listing_id if listing else ""
        if not listing_id:
            return ConnectorActionResult(success=False, message="No existing BaseLinker listing to push a field to.")

        try:
            return sync.push_field(product, field_name, listing_id, config)
        except (BaseLinkerAPIError, requests.RequestException) as e:
            return ConnectorActionResult(success=False, external_id=listing_id, message=str(e))

    def fetch_catalog(self) -> list:
        """Real bulk catalog fetch — List[ImportedProductData] for every
        product in this account's configured inventory_id."""
        try:
            return import_products.fetch_all_products(self._config())
        except (BaseLinkerAPIError, requests.RequestException):
            return []
