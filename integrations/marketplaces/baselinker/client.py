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
from typing import Optional

import requests

from integrations.base import ConnectionTestResult, ConnectorActionResult, MarketplaceConnector
from integrations.marketplaces.baselinker import mapper

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


class BaselinkerConnector(MarketplaceConnector):
    integration_type = "baselinker"

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
            payload = mapper.build_payload(product, config, existing_listing_id=None)
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
            payload = mapper.build_payload(product, config, existing_listing_id=external_id)
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
