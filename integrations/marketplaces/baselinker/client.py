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
from dataclasses import dataclass
from typing import Optional, Set

import requests

from integrations.base import (
    CHECK_AUTH_ERROR,
    CHECK_NOT_FOUND,
    CHECK_SUCCESS,
    CHECK_TEMPORARY_ERROR,
    ConnectionTestResult,
    ConnectorActionResult,
    ImportedProductData,
    MarketplaceConnector,
    MissingTranslationError,
    ProductConnectionCheck,
)
from integrations.marketplaces.baselinker import import_products, mapper, order_mapper, sync
from modules import category_mapping_store, company_store, field_mapping_store, product_translation_store, sync_rules_store

API_URL = "https://api.baselinker.com/connector.php"
_MIN_CALL_INTERVAL = 60.0 / 90  # stay safely under the 100 req/min limit

_last_call_at: dict = {}  # keyed by API token, not one shared global — see module docstring


class BaseLinkerAPIError(RuntimeError):
    """An API-level error: the request reached BaseLinker and it answered
    with status=ERROR. `code` carries BaseLinker's own error_code (e.g.
    "ERROR_AUTH_TOKEN") so callers can tell a credentials problem from an
    ordinary failure without pattern-matching the human-readable message."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code or ""

    def is_auth_error(self) -> bool:
        upper = f"{self.code} {self}".upper()
        return "AUTH" in upper or "TOKEN" in upper


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
        raise BaseLinkerAPIError(
            f"{method} failed: {data.get('error_message', data)}",
            code=str(data.get("error_code") or ""),
        )
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


# ---- Settings-page lookups ---------------------------------------------
# Used only by app.py's Settings -> Integrations BaseLinker form to let a
# company pick inventory/category/price-group/warehouse by name instead of
# typing a raw BaseLinker-internal ID — see PHASE plan "token -> fetch ->
# pick". Every function here takes a raw token directly (not a `config`
# dict) since these all run BEFORE inventory_id/category_id are known —
# that's the whole point. Errors (bad token, network) propagate as
# BaseLinkerAPIError/requests.RequestException; the Settings page catches
# and displays them rather than these functions swallowing anything.

@dataclass
class TokenVerification:
    """Outcome of verify_token() below. Kept in this module rather than
    integrations/base.py because it is shaped by BaseLinker's own account
    model (`inventory_count`), exactly like the list_*() helpers around it;
    promote it to a shared dataclass the day a second connector needs the
    same shape, not before.

    `status` is one of integrations/base.py's CHECK_* constants, reusing the
    same three-way split check_product_connection() already draws — see
    verify_token() for why a bare success/failure boolean is the wrong shape
    here."""
    status: str                 # CHECK_SUCCESS / CHECK_AUTH_ERROR / CHECK_TEMPORARY_ERROR
    message: str = ""           # the API's own error text, for the UI to show verbatim
    inventory_count: int = 0    # only meaningful on CHECK_SUCCESS — proof the token really opened the account


def verify_token(token: str) -> TokenVerification:
    """Checks ONLY whether this API token works — one cheap, read-only
    getInventories call, needing no inventory_id/category_id/price group.

    That "needs nothing else" part is the whole point, and what separates
    this from BaselinkerConnector.test_connection(): that one can only run
    once the entire Settings form is filled in and submitted, so it cannot
    tell somebody whose TOKEN is wrong from somebody who merely picked the
    wrong inventory — and until this existed there was no way at all to ask
    "is my key even valid?" without first getting through inventory and
    category selection.

    Splits failure three ways rather than returning a bare bool, because
    the three demand opposite responses: BaseLinker actively REFUSED the
    credentials (retrying is pointless — the token needs replacing), we
    never reached BaseLinker at all (network/DNS/timeout/5xx — the token
    may well be perfect and the right move is to try again), or it worked.
    Collapsing the first two into one "failed" is precisely what leaves
    someone stuck with a connection they cannot diagnose.

    Never raises: every failure path is reported as a value, since the only
    caller is a UI button whose entire job is to explain what went wrong.
    """
    token = (token or "").strip()
    if not token:
        return TokenVerification(status=CHECK_AUTH_ERROR, message="No API token provided.")
    try:
        data = _call("getInventories", {}, token)
    except BaseLinkerAPIError as e:
        # Reached BaseLinker and it answered with status=ERROR. Only a
        # credentials-shaped error is the token's fault; anything else
        # (rate limit, account restriction) is "try again", not "your key
        # is wrong" — mislabeling those would send the user off replacing
        # a token that was fine.
        return TokenVerification(
            status=CHECK_AUTH_ERROR if e.is_auth_error() else CHECK_TEMPORARY_ERROR,
            message=str(e),
        )
    except requests.RequestException as e:
        # Never got an answer — says nothing about the token's validity.
        return TokenVerification(status=CHECK_TEMPORARY_ERROR, message=str(e))
    return TokenVerification(
        status=CHECK_SUCCESS, inventory_count=len(data.get("inventories") or []),
    )


def list_inventories(token: str) -> list:
    """Real getInventories call. Each returned dict also carries
    `price_groups`/`warehouses` (lists of IDs valid for that inventory) so
    the Settings page can locally filter list_price_groups()/
    list_warehouses()'s global results down to what's relevant, without a
    second round-trip per inventory."""
    data = _call("getInventories", {}, token)
    return data.get("inventories", []) or []


# {(token, inventory_id): default_language} — the inventory's default
# catalog language changes about never, but every export needs it, so it
# is fetched once per process instead of adding a getInventories round
# trip to each one.
_default_language_cache: dict = {}


def inventory_default_language(token: str, inventory_id) -> str:
    """The catalog language this inventory treats as its default — the one
    BaseLinker demands a product name under (see mapper.build_payload()'s
    `inventory_default_language`).

    Returns "" if it can't be determined (no token, API down, inventory not
    on this account). Callers must treat that as "unknown" and carry on;
    an export must never fail because this piece of metadata was
    unavailable."""
    key = (token, str(inventory_id))
    if key in _default_language_cache:
        return _default_language_cache[key]
    if not token:
        return ""
    try:
        inventories = list_inventories(token)
    except (BaseLinkerAPIError, requests.RequestException):
        return ""  # deliberately not cached — a transient failure should be retried
    result = ""
    for inv in inventories:
        if str(inv.get("inventory_id")) == str(inventory_id):
            result = str(inv.get("default_language") or "")
            break
    _default_language_cache[key] = result
    return result


def list_price_groups(token: str) -> list:
    """Real getInventoryPriceGroups call — global (not inventory-scoped);
    the Settings page filters by the chosen inventory's own price_groups
    list from list_inventories() above."""
    data = _call("getInventoryPriceGroups", {}, token)
    return data.get("price_groups", []) or []


def list_warehouses(token: str) -> list:
    """Real getInventoryWarehouses call — global (not inventory-scoped);
    filtered the same way as list_price_groups() above."""
    data = _call("getInventoryWarehouses", {}, token)
    return data.get("warehouses", []) or []


def list_categories(token: str, inventory_id: int) -> list:
    """Real getInventoryCategories call, scoped to one inventory. Each
    category carries `parent_id` (0 = root) so the Settings page can render
    a breadcrumb-style hierarchy instead of a flat, ambiguous ID list."""
    data = _call("getInventoryCategories", {"inventory_id": int(inventory_id)}, token)
    return data.get("categories", []) or []


def list_extra_fields(token: str) -> list:
    """Real getInventoryExtraFields call — this account's own custom
    product fields (e.g. "Modelis", "Plaukts"; confirmed live against a
    real connected account, no parameters needed — the call is account-
    wide, not scoped to one inventory_id, unlike list_categories above).
    Each entry carries `extra_field_id`/`name`; BaselinkerConnector.
    get_target_fields() turns these into Field Mapping's "Target field"
    dropdown options so a company can map onto ITS OWN fields instead of
    only the two generic ones (condition_id/category_id) every company
    used to be limited to."""
    data = _call("getInventoryExtraFields", {}, token)
    return data.get("extra_fields", []) or []


class BaselinkerConnector(MarketplaceConnector):
    integration_type = "baselinker"

    # Static fallback for get_target_fields() below — real top-level
    # BaseLinker fields this module has a DEFAULT placement for (ean,
    # weight) or no default placement for at all (condition_id — nothing
    # sends this unless a rule targets it). Used when there's no live
    # token to call with (offline preview, disconnected state) or the API
    # call itself fails. Deliberately does NOT include "category_id" —
    # that's owned exclusively by the separate Category Mapping feature
    # (see modules/category_mapping_store.py's _resolve_category_id());
    # letting a Field Mapping rule also target category_id would let it
    # silently overwrite what Category Mapping already resolved.
    SUPPORTED_TARGET_FIELDS = {
        "condition_id": "Condition",
        "ean": "EAN",
        "weight": "Weight (kg)",
    }

    # Named per-language text_fields slots ("text:<slot>" — see mapper.py's
    # _apply_field_mapping_rules) and the shared "Information -> Parameters"
    # block ("features:<field>", text_fields["features|<lang>"]) — every
    # SYNCABLE_FIELDS key with a DEFAULT placement inside one of these two
    # shared blocks. Redirecting a field e.g. onto its OWN
    # "features:brand"/"text:description" target is a deliberate no-op
    # (put it back where it already was); redirecting onto an
    # extra_field_<id> instead moves it out of the shared block entirely.
    STRUCTURAL_TARGET_FIELDS = {
        "features:brand": "Parameters → Brand",
        "features:model": "Parameters → Model",
        "features:product_condition": "Parameters → Condition",
        "features:color": "Parameters → Color",
        "features:power": "Parameters → Power",
        "text:description": "Description",
        "text:description_extra1": "Additional Description",
    }

    # integrations/field_registry.SYNCABLE_FIELDS key -> the target-field
    # key (STRUCTURAL_TARGET_FIELDS or SUPPORTED_TARGET_FIELDS) it lands
    # at TODAY with no rule saved — used ONLY to pre-fill the Field
    # Mapping tab's editable table with "what's already happening" as
    # ordinary, changeable rows the first time a company opens it (see
    # app.py's _render_field_mapping_tab), not to drive mapper.py itself
    # (that still has its own hardcoded defaults, unconditionally correct
    # with zero saved rules — see that module's docstring on why: a
    # company who never opens this tab must see ZERO behavior change).
    # "defects" has no entry: it has no default placement at all today
    # (nothing sends it unless a rule explicitly maps it) — see mapper.py.
    DEFAULT_STRUCTURAL_MAPPING = {
        "brand": "features:brand",
        "model": "features:model",
        "product_condition": "features:product_condition",
        "color": "features:color",
        "power": "features:power",
        "product_description": "text:description",
        "condition_description": "text:description_extra1",
        "barcode": "ean",
        "weight_kg": "weight",
    }

    # "name" is the one field_registry.SYNCABLE_FIELDS entry (mappable=True
    # at the registry level) that THIS connector still keeps core: BaseLinker
    # rejects the whole addInventoryProduct call if the account's default
    # catalog language has no name, and build_payload() duplicates it under
    # `primary_language` specifically to guarantee that — a mandatory-field
    # + dual-language-fallback coupling no other field carries, and specific
    # to BaseLinker's own validation behavior, not a universal ElectroGrader
    # rule (see MarketplaceConnector.CORE_FIELDS' docstring). See
    # get_mappable_source_fields() (inherited, unchanged) for how this
    # combines with field_registry's own registry-level exclusions
    # (sku/price/quantity/image_paths/category) to produce the Field
    # Mapping tab's actual "Source field" list.
    CORE_FIELDS = {"name"}

    def get_target_fields(self) -> dict:
        """STRUCTURAL_TARGET_FIELDS + SUPPORTED_TARGET_FIELDS, extended
        with this account's own custom fields (list_extra_fields,
        confirmed live against a real connected account) — e.g.
        "Modelis"/"Plaukts" — so a company can map onto BaseLinker's real
        fields OR its own custom ones, not a generic two-field list every
        company was stuck with before. Falls back to the static dicts
        alone on any failure (no token yet, API error, rate limit) — the
        Field Mapping tab must still render, just without the live
        fields."""
        fields = {**self.STRUCTURAL_TARGET_FIELDS, **self.SUPPORTED_TARGET_FIELDS}
        token = self.credentials.get("token", "")
        if not token:
            return fields
        try:
            for f in list_extra_fields(token):
                fields[f"extra_field_{f['extra_field_id']}"] = f.get("name") or f"Extra field {f['extra_field_id']}"
        except Exception:
            pass  # static fields alone are still a usable dropdown
        return fields

    # Fields whose Synchronization-tab checkbox actually gates something in
    # mapper.build_payload() today. "sku" is deliberately absent — it's
    # always sent regardless of toggle state (see mapper.py), so its
    # checkbox wouldn't do anything. "brand"/"model"/"product_condition"/
    # "color"/"power" land in text_fields["features|en"] (BaseLinker's
    # "Information -> Parameters" section) BY DEFAULT, and are also the
    # five fields a saved Field Mapping rule can redirect elsewhere instead
    # (see STRUCTURAL_TARGET_FIELDS above); "category"/"defects" still have
    # no payload destination (category stays the single global category_id
    # setting; defects mapping-application is future work) — see
    # integrations/field_registry.py for the full field list these are a
    # subset of.
    IMPLEMENTED_SYNC_FIELDS = {
        "name", "product_description", "condition_description",
        "image_paths", "price", "quantity", "weight_kg", "barcode",
        "brand", "model", "product_condition", "color", "power",
    }
    DEFAULT_SYNC_FIELDS = [
        "name", "product_description", "condition_description",
        "image_paths", "price", "quantity", "weight_kg", "sku", "barcode",
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

    def _field_mapping_rules(self) -> list:
        """This company's saved Settings -> Field Mapping rules, or [] if
        none saved yet — passed into mapper.build_payload() as plain data
        (mapper.py stays DB-free; see its docstring) so a company's own
        target-field/value overrides are actually applied to every real
        payload (create/update/preview), not just stored inert as before."""
        mapping = field_mapping_store.get_mapping(self.company_id, self.integration_type)
        return mapping.rules if mapping else []

    def preview_payload(self, product) -> dict:
        """Same build_payload() call the real push uses, minus actually
        base64-encoding images (wasteful for a preview) — a synthetic
        `_preview_image_count` key tells the UI how many WOULD be sent
        instead."""
        config = self._config(product)
        fields_send = self._resolve_fields_send()
        try:
            ec = self._resolve_export_content(product)
        except MissingTranslationError as e:
            # Preview must SHOW the block, not crash and not paper over it
            # with some other language — it is the same resolution the real
            # export uses, so it has to reach the same verdict.
            return {"_export_blocked": str(e), "_missing_language": e.language}
        payload = mapper.build_payload(
            product, config, ec["language"], existing_listing_id=None, fields_send=fields_send,
            include_images=False,
            title=ec["title"], description=ec["description"],
            condition_description=ec["condition_description"],
            color=ec["color"], field_mapping_rules=self._field_mapping_rules(),
            redirectable_fields=self.get_mappable_source_fields(),
            inventory_default_language=inventory_default_language(
                config["token"], config["inventory_id"]),
        )
        wants_images = fields_send is None or "image_paths" in fields_send
        payload["_preview_image_count"] = len(product.image_paths) if wants_images and product.image_paths else 0
        return payload

    def _config(self, product=None) -> dict:
        settings = self.settings
        return {
            "token": self.credentials.get("token", ""),
            "inventory_id": int(settings["inventory_id"]),
            "category_id": self._resolve_category_id(product, int(settings["category_id"])),
            "price_group_id": settings.get("price_group_id") or None,
            "warehouse_id": settings.get("warehouse_id") or None,
            "tax_rate": float(settings.get("tax_rate") or 23),
        }

    def _resolve_category_id(self, product, default_category_id: int) -> int:
        """This company's saved Category Mapping (see
        modules/category_mapping_store.py) for `product.category_id`, if
        any — else the single default BaseLinker category every product
        used to be limited to (Settings' own "token -> fetch -> pick"
        category_id). `product=None` (connection tests, no product in
        context yet) always uses the default."""
        if product is None or not getattr(product, "category_id", ""):
            return default_category_id
        mapped = category_mapping_store.resolve(self.company_id, self.integration_type, product.category_id)
        if not mapped:
            return default_category_id
        try:
            return int(mapped)
        except (TypeError, ValueError):
            return default_category_id

    def get_external_categories(self) -> list:
        """This account's real BaseLinker category list, live (never
        cached) — see integrations/base.py's docstring on why. `name`
        already carries the full breadcrumb path from BaseLinker itself
        (confirmed live: e.g. "Haushaltsgeräte/Kleingeräte Küche/
        Wasserkocher" — BaseLinker bakes hierarchy into the name string
        rather than a real parent-child tree), displayed with " > "
        separators to match this app's own Category Catalog breadcrumb
        style."""
        token = self.credentials.get("token", "")
        if not token:
            return []
        try:
            cats = list_categories(token, int(self.settings["inventory_id"]))
        except Exception:
            return []
        return [
            {"id": str(c["category_id"]), "label": (c.get("name") or str(c["category_id"])).replace("/", " > ")}
            for c in cats
        ]

    def _resolve_export_content(self, product) -> dict:
        """The ONE content/language resolution every export path shares —
        full export, single-field push, automatic sync and preview.

        The language comes from resolve_export_language() (inherited, see
        integrations/base.py): the integration's own `export_language`, else
        the company's `default_product_language`. Nothing here consults the
        product to decide a language.

        There is NO fallback to another language. If the product has no
        content in the resolved language, this raises
        MissingTranslationError and the export is refused. It used to
        silently switch `export_language` to the product's own
        primary_language, which changed not just the text but the whole
        payload's language — `features|lv` became `features|en`, with
        translated parameter NAMES to match. BaseLinker treats a
        differently-named parameter as a different parameter, so a catalog
        exported that way splits into two unrelated parameter sets, a
        little more with every product. Refusing is visible; mislabelling
        is not.

        mapper.build_payload() only ever places the strings handed to it —
        it never resolves a language or reads a translation itself.
        """
        export_language = self.resolve_export_language()
        if not export_language:
            raise MissingTranslationError("", getattr(product, "sku", ""))

        translation = product_translation_store.get_translation(product.id, export_language)
        if translation is None:
            if export_language == product.primary_language:
                # Same language, different storage: inventory_store hydrates
                # a Product's name/description FROM the primary-language row,
                # so a product predating that table still carries its
                # primary-language content on the object itself. Reading it
                # here is not a language fallback — it is the requested
                # language, just not in the usual place.
                return {
                    "title": product.name, "description": product.product_description,
                    "condition_description": product.condition_description,
                    "language": export_language, "color": product.color,
                }
            raise MissingTranslationError(export_language, getattr(product, "sku", ""))

        return {
            "title": translation.title,
            "description": translation.description,
            "condition_description": translation.condition_description,
            "language": export_language,
            # color is a deliberate exception to "parameter values are never
            # translated" (confirmed with the user) — falls back to the
            # untranslated product.color if this translation predates the
            # color field being added, rather than exporting a blank color.
            "color": translation.color or product.color,
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
        config = self._config(product)
        found_id = self._find_existing_by_sku(product.sku, config)
        if found_id:
            return self.update_product(product, found_id)

        try:
            ec = self._resolve_export_content(product)
            payload = mapper.build_payload(
                product, config, ec["language"], existing_listing_id=None,
                fields_send=self._resolve_fields_send(),
                title=ec["title"], description=ec["description"],
                condition_description=ec["condition_description"],
                color=ec["color"], field_mapping_rules=self._field_mapping_rules(),
                redirectable_fields=self.get_mappable_source_fields(),
                inventory_default_language=inventory_default_language(
                    config["token"], config["inventory_id"]),
            )
            data = _call("addInventoryProduct", payload, config["token"])
        except MissingTranslationError as e:
            return ConnectorActionResult(success=False, message=str(e))
        except (BaseLinkerAPIError, requests.RequestException) as e:
            return ConnectorActionResult(success=False, message=str(e))

        return ConnectorActionResult(
            success=True, external_id=str(data.get("product_id", "")), message="Created.",
            data={"warnings": data.get("warnings", {}) or {}},
        )

    def update_product(self, product, external_id: str) -> ConnectorActionResult:
        config = self._config(product)
        try:
            ec = self._resolve_export_content(product)
            payload = mapper.build_payload(
                product, config, ec["language"], existing_listing_id=external_id,
                fields_send=self._resolve_fields_send(),
                title=ec["title"], description=ec["description"],
                condition_description=ec["condition_description"],
                color=ec["color"], field_mapping_rules=self._field_mapping_rules(),
                redirectable_fields=self.get_mappable_source_fields(),
                inventory_default_language=inventory_default_language(
                    config["token"], config["inventory_id"]),
            )
            if "text_fields" in payload:
                payload["text_fields"] = self._merge_text_fields(external_id, payload["text_fields"], config)
            data = _call("addInventoryProduct", payload, config["token"])
        except MissingTranslationError as e:
            return ConnectorActionResult(success=False, external_id=external_id, message=str(e))
        except (BaseLinkerAPIError, requests.RequestException) as e:
            return ConnectorActionResult(success=False, external_id=external_id, message=str(e))

        return ConnectorActionResult(
            success=True, external_id=str(data.get("product_id", external_id)), message="Updated.",
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

    def check_product_connection(self, external_id: str) -> ProductConnectionCheck:
        """Real getInventoryProductsData call for this one product id.

        BaseLinker answers a lookup for a non-existent product with a
        SUCCESSFUL response whose "products" map simply omits it — so an
        empty/missing entry is the NOT_FOUND signal, and every exception
        path below stays firmly on the "couldn't verify" side. That split
        matters: only NOT_FOUND is offered to the user as grounds for
        removing the association."""
        if not external_id:
            return ProductConnectionCheck(
                status=CHECK_NOT_FOUND, external_id="",
                message="This listing has no BaseLinker product id stored.",
            )
        try:
            products = get_product_data([external_id], self._config())
        except BaseLinkerAPIError as e:
            # Reached BaseLinker; it refused. Credentials problems are
            # their own outcome — retrying won't help, the integration
            # settings need attention.
            return ProductConnectionCheck(
                status=CHECK_AUTH_ERROR if e.is_auth_error() else CHECK_TEMPORARY_ERROR,
                message=str(e), external_id=external_id,
            )
        except requests.RequestException as e:
            # Timeout, DNS/connection failure, or a 5xx via
            # raise_for_status() — the product's existence is simply
            # unknown, never "gone".
            return ProductConnectionCheck(
                status=CHECK_TEMPORARY_ERROR, message=str(e), external_id=external_id,
            )
        except (TypeError, ValueError) as e:
            # A malformed id that never formed a real request.
            return ProductConnectionCheck(
                status=CHECK_TEMPORARY_ERROR, message=str(e), external_id=external_id,
            )

        if str(external_id) in {str(k) for k in (products or {})}:
            return ProductConnectionCheck(status=CHECK_SUCCESS, external_id=external_id)
        return ProductConnectionCheck(status=CHECK_NOT_FOUND, external_id=external_id)

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
            # Same resolution as create_product()/update_product()/
            # preview_payload() — one language decision per company, shared
            # by every path that can reach BaseLinker.
            ec = self._resolve_export_content(product)
            return sync.push_field(
                product, field_name, listing_id, config, ec,
                field_mapping_rules=self._field_mapping_rules(),
                inventory_default_language=inventory_default_language(
                    config["token"], config["inventory_id"]),
                redirectable_fields=self.get_mappable_source_fields(),
            )
        except MissingTranslationError as e:
            return ConnectorActionResult(success=False, external_id=listing_id, message=str(e))
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
