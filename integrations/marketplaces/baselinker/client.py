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
from integrations.marketplaces.baselinker import import_products, mapper, order_mapper, sync
from modules import company_store, product_translation_store, sync_rules_store

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
    """Real getOrders call, paginated via id_from — BaseLinker returns at
    most 100 orders per call, same loop-until-empty-page shape as
    list_all_product_ids() above. date_confirmed_from bounds the pull to
    orders confirmed at/after that unix timestamp; id_from then advances
    strictly by the highest order_id seen so a single sync run never
    re-fetches or skips a page even if new orders land mid-pull."""
    orders: list = []
    id_from = 0
    while True:
        data = _call(
            "getOrders",
            {"date_confirmed_from": int(date_confirmed_from), "id_from": id_from},
            config["token"],
        )
        page = data.get("orders", []) or []
        if not page:
            break
        orders.extend(page)
        id_from = max(o.get("order_id", 0) for o in page) + 1
        if len(page) < 100:
            break
    return orders


def get_order_statuses(config: dict) -> dict:
    """Real getOrderStatusList call — maps order_status_id -> the
    customer-facing status label. Fetched fresh on every order sync (one
    cheap call) rather than cached, since a company can rename its statuses
    in BaseLinker at any time."""
    data = _call("getOrderStatusList", {}, config["token"])
    return {s["id"]: (s.get("name_for_customer") or s.get("name", "")) for s in (data.get("statuses") or [])}


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
    # checkbox wouldn't do anything. "brand"/"model"/"product_condition"/
    # "color"/"power" land in text_fields["features|en"] (BaseLinker's
    # "Information -> Parameters" section); "category"/"defects" still have
    # no payload destination (category stays the single global category_id
    # setting; defects mapping-application is future work) — see
    # integrations/field_registry.py for the full field list these are a
    # subset of.
    IMPLEMENTED_SYNC_FIELDS = {
        "name", "product_description", "condition_description",
        "image_paths", "price", "quantity", "barcode",
        "brand", "model", "product_condition", "color", "power",
    }
    DEFAULT_SYNC_FIELDS = [
        "name", "product_description", "condition_description",
        "image_paths", "price", "quantity", "sku", "barcode",
        "brand", "model", "product_condition", "color", "power",
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
        ec = self._resolve_export_content(product)
        payload = mapper.build_payload(
            product, config, existing_listing_id=None, fields_send=fields_send, include_images=False,
            title=ec["title"], description=ec["description"], condition_description=ec["condition_description"],
            language=ec["language"], primary_language=ec["primary_language"], primary_title=ec["primary_title"],
            primary_description=ec["primary_description"], primary_condition_description=ec["primary_condition_description"],
                color=ec["color"],
        )
        wants_images = fields_send is None or "image_paths" in fields_send
        payload["_preview_image_count"] = len(product.image_paths) if wants_images and product.image_paths else 0
        payload["_language_fallback_warning"] = ec["warning"]
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

    def _resolve_export_content(self, product) -> dict:
        """Resolves which language to export in — this integration's own
        `export_language` setting if set, else the company's
        `default_product_language` — and fetches that
        modules/product_translation_store row. Falls back to the product's
        primary_language (with a warning) if no translation exists yet for
        the requested language, rather than ever silently mislabeling
        English text as another language.

        Also fetches the product's primary-language content whenever it
        differs from the resolved export language — BaseLinker requires a
        name under its own account default catalog language regardless of
        what else is sent (see mapper.build_payload()'s `primary_language`
        params), and primary_language is the one language guaranteed to
        exist for every product.

        This is the ONLY place in the BaseLinker connector that decides a
        language or reads a translation — mapper.build_payload() just
        places the strings it's given; it never calls a translation
        provider or resolves a language itself.
        """
        export_language = self.settings.get("export_language") or ""
        if not export_language:
            company = company_store.get_company(self.company_id)
            export_language = company.default_product_language if company else product.primary_language

        translation = product_translation_store.get_translation(product.id, export_language)
        warning = None
        if translation is None and export_language != product.primary_language:
            warning = (
                f"No {export_language!r} translation exists for this product yet — "
                f"exported in {product.primary_language!r} instead. "
                f"Use the product's Translate action to add one."
            )
            export_language = product.primary_language
            translation = product_translation_store.get_translation(product.id, export_language)

        if translation is None:
            # No translation row at all (shouldn't happen post-migration) —
            # fall back to whatever is directly on the Product rather than
            # exporting nothing.
            title, description, condition_description = product.name, product.product_description, product.condition_description
            color = product.color
        else:
            title, description, condition_description = translation.title, translation.description, translation.condition_description
            # color is a deliberate exception to "parameter values are never
            # translated" (confirmed with the user) — falls back to the
            # untranslated product.color if this translation predates the
            # color field being added, rather than exporting a blank color.
            color = translation.color or product.color

        primary_language, primary_title, primary_description, primary_condition_description = "", "", "", ""
        if export_language != product.primary_language:
            primary = product_translation_store.get_translation(product.id, product.primary_language)
            primary_language = product.primary_language
            if primary is not None:
                primary_title, primary_description, primary_condition_description = (
                    primary.title, primary.description, primary.condition_description
                )
            else:
                primary_title, primary_description, primary_condition_description = (
                    product.name, product.product_description, product.condition_description
                )

        return {
            "title": title, "description": description, "condition_description": condition_description,
            "language": export_language, "warning": warning, "color": color,
            "primary_language": primary_language, "primary_title": primary_title,
            "primary_description": primary_description, "primary_condition_description": primary_condition_description,
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

        ec = self._resolve_export_content(product)
        try:
            payload = mapper.build_payload(
                product, config, existing_listing_id=None, fields_send=self._resolve_fields_send(),
                title=ec["title"], description=ec["description"], condition_description=ec["condition_description"],
                language=ec["language"], primary_language=ec["primary_language"], primary_title=ec["primary_title"],
                primary_description=ec["primary_description"], primary_condition_description=ec["primary_condition_description"],
                color=ec["color"],
            )
            data = _call("addInventoryProduct", payload, config["token"])
        except (BaseLinkerAPIError, requests.RequestException) as e:
            return ConnectorActionResult(success=False, message=str(e))

        return ConnectorActionResult(
            success=True, external_id=str(data.get("product_id", "")),
            message="Created." + (f" Warning: {ec['warning']}" if ec["warning"] else ""),
            data={"warnings": data.get("warnings", {}) or {}},
        )

    def update_product(self, product, external_id: str) -> ConnectorActionResult:
        config = self._config()
        ec = self._resolve_export_content(product)
        try:
            payload = mapper.build_payload(
                product, config, existing_listing_id=external_id, fields_send=self._resolve_fields_send(),
                title=ec["title"], description=ec["description"], condition_description=ec["condition_description"],
                language=ec["language"], primary_language=ec["primary_language"], primary_title=ec["primary_title"],
                primary_description=ec["primary_description"], primary_condition_description=ec["primary_condition_description"],
                color=ec["color"],
            )
            if "text_fields" in payload:
                payload["text_fields"] = self._merge_text_fields(external_id, payload["text_fields"], config)
            data = _call("addInventoryProduct", payload, config["token"])
        except (BaseLinkerAPIError, requests.RequestException) as e:
            return ConnectorActionResult(success=False, external_id=external_id, message=str(e))

        return ConnectorActionResult(
            success=True, external_id=str(data.get("product_id", external_id)),
            message="Updated." + (f" Warning: {ec['warning']}" if ec["warning"] else ""),
            data={"warnings": data.get("warnings", {}) or {}},
        )

    def _merge_text_fields(self, external_id: str, new_fields: dict, config: dict) -> dict:
        """BaseLinker's addInventoryProduct REPLACES text_fields wholesale on
        write, it does not merge — confirmed by this repo's own one-off fix
        scripts (scripts/prune_baselinker_lv_parameters.py etc.), which all
        fetch-full/patch-one-key/send-full-back for exactly this reason.
        `new_fields` here only ever contains the handful of keys
        ElectroGrader itself controls (name/description/description_extra1/
        features|en — see mapper.build_payload()), so a shallow overlay onto
        the product's CURRENT live text_fields can never touch name|lv,
        features|pl, description|es, etc. — every other language/key an
        earlier system (or a human, directly in BaseLinker) set stays
        exactly as it was. On any fetch failure, falls back to sending
        new_fields as-is (today's pre-merge behavior) rather than blocking
        the whole update."""
        try:
            existing = get_product_data([external_id], config)
            current = (existing.get(str(external_id)) or {}).get("text_fields") or {}
        except (BaseLinkerAPIError, requests.RequestException):
            return new_fields
        return {**current, **new_fields}

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

    # ---- Order sync -----------------------------------------------------

    def fetch_orders(self, since: float) -> list:
        """Real order fetch — List[modules.models.Order], normalized via
        order_mapper.map_order(). [] on any API failure, same honest-empty/
        never-raise convention as fetch_catalog() above (the scheduler's
        poll_order_sync_once() treats [] as "nothing new this cycle", not
        an error worth surfacing)."""
        config = self._config()
        try:
            raw_orders = get_orders(config, date_confirmed_from=since)
            status_labels = get_order_statuses(config)
        except (BaseLinkerAPIError, requests.RequestException):
            return []
        return [order_mapper.map_order(raw, status_labels, self.company_id) for raw in raw_orders]
