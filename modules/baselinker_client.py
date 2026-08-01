"""BaseLinker (now Base.com) API integration — optional, additive push of a
finished Product straight into a BaseLinker catalog, replacing the need for
manual CSV import + column mapping. modules/export.py's CSV/Excel export is
completely untouched and remains the default/fallback path; this is a
parallel option a user can ignore entirely if BASELINKER_API_TOKEN is unset.

API docs consulted: https://api.baselinker.com/index.php?method=addInventoryProduct
                     https://api.baselinker.com/index.php?method=getInventoryProductsList
Endpoint: POST https://api.baselinker.com/connector.php, auth via the
X-BLToken header. Rate limit: 100 requests/minute (documented).

Design notes:
  - Never invoked automatically — always an explicit user action from the
    Export tab, mirroring this project's "review before anything leaves the
    app" philosophy (same as spec lookup / grading / EAN-ASIN lookup all
    surfacing results before anything is saved).
  - Idempotent: a `marketplace_listings` row (modules/marketplace_store.py,
    marketplace="baselinker") records the external product_id after the
    first successful push; subsequent pushes UPDATE that same BaseLinker
    product instead of creating a duplicate. As a safety net for a reset/
    fresh local DB, an unsynced product is first checked against BaseLinker
    by SKU (`filter_sku`) before creating anything new.
  - Images are sent as inline base64 (BaseLinker's `images` parameter also
    accepts a public URL, but our photos are local-only) capped at the
    documented 2MB post-encoding limit; oversized photos are progressively
    downscaled/recompressed with Pillow until they fit.
  - The two required English description columns from modules/export.py
    (Product Description / Condition & Scratches Details) map onto
    BaseLinker's `text_fields.description` and `text_fields.description_extra1`
    respectively, preserving the same separation inside BaseLinker itself.
"""
import base64
import io
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from PIL import Image

from modules import marketplace_store

MARKETPLACE = "baselinker"
API_URL = "https://api.baselinker.com/connector.php"
MAX_IMAGE_BASE64_BYTES = 2 * 1024 * 1024  # BaseLinker's documented cap
_MIN_CALL_INTERVAL = 60.0 / 90  # stay safely under the 100 req/min limit

_last_call_at = 0.0


class BaseLinkerConfigError(RuntimeError):
    pass


class BaseLinkerAPIError(RuntimeError):
    pass


def get_config() -> dict:
    """Reads and validates required settings from the environment. Raises
    BaseLinkerConfigError (never silently no-ops) if anything required is
    missing — mirrors modules/ai_client.py's `_client()` pattern."""
    token = os.environ.get("BASELINKER_API_TOKEN")
    inventory_id = os.environ.get("BASELINKER_INVENTORY_ID")
    category_id = os.environ.get("BASELINKER_CATEGORY_ID")
    if not token:
        raise BaseLinkerConfigError("BASELINKER_API_TOKEN is not set. Add it to your .env file.")
    if not inventory_id:
        raise BaseLinkerConfigError("BASELINKER_INVENTORY_ID is not set. Add it to your .env file.")
    if not category_id:
        raise BaseLinkerConfigError("BASELINKER_CATEGORY_ID is not set. Add it to your .env file.")
    return {
        "token": token,
        "inventory_id": int(inventory_id),
        "category_id": int(category_id),
        "price_group_id": os.environ.get("BASELINKER_PRICE_GROUP_ID") or None,
        "warehouse_id": os.environ.get("BASELINKER_WAREHOUSE_ID") or None,
        "tax_rate": float(os.environ.get("BASELINKER_TAX_RATE", "23")),
    }


def is_configured() -> bool:
    try:
        get_config()
        return True
    except BaseLinkerConfigError:
        return False


def _rate_limit_wait() -> None:
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)
    _last_call_at = time.time()


def _call(method: str, parameters: dict, token: str) -> dict:
    _rate_limit_wait()
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


def encode_image(path: str, max_base64_bytes: int = MAX_IMAGE_BASE64_BYTES) -> str:
    """Reads an image file and returns BaseLinker's expected inline-image
    string (`"data:" + base64`), shrinking/recompressing it if needed to
    fit under the documented 2MB post-base64 size cap."""
    with open(path, "rb") as f:
        raw = f.read()

    img = None
    quality = 90
    while len(base64.b64encode(raw)) > max_base64_bytes:
        if img is None:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
        img = img.resize((max(1, int(img.width * 0.8)), max(1, int(img.height * 0.8))))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        raw = buf.getvalue()
        quality = max(40, quality - 10)

    return "data:" + base64.b64encode(raw).decode("ascii")


@dataclass
class PushResult:
    success: bool
    baselinker_product_id: str = ""
    message: str = ""
    warnings: dict = field(default_factory=dict)


def find_existing_product_id_by_sku(sku: str, config: dict) -> Optional[str]:
    """Safety net: before creating, check whether BaseLinker already has
    this SKU (e.g. if the local DB was ever reset) so a push updates
    instead of duplicating."""
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
    products = data.get("products") or {}
    for product_id in products:
        return str(product_id)
    return None


def build_payload(
    product, config: dict, include_images: bool = True, existing_listing_id: Optional[str] = None
) -> dict:
    """Builds the addInventoryProduct parameters for one Product. No network
    calls of its own (aside from the marketplace_store lookup below), so
    it's fully unit-testable. `existing_listing_id`, when the caller already
    resolved one (push_product does, after its SKU-fallback check), takes
    priority over looking it up fresh — this avoids a stale/missing lookup
    right after a SKU-fallback match that hasn't been persisted yet."""
    payload = {
        "inventory_id": config["inventory_id"],
        "sku": product.sku,
        "category_id": config["category_id"],
        "tax_rate": config["tax_rate"],
        "text_fields": {
            "name": product.name or product.model_number or product.sku,
            "description": product.product_description or "",
            "description_extra1": (
                f"Condition & Scratches Details: {product.condition_description}"
                if product.condition_description
                else ""
            ),
        },
    }

    if existing_listing_id is None:
        existing = marketplace_store.get_listing(product.id, MARKETPLACE, product.company_id)
        existing_listing_id = existing.external_listing_id if existing else ""
    if existing_listing_id:
        payload["product_id"] = int(existing_listing_id)

    ean = product.ean or product.manifest_barcode or product.scanned_barcode
    if ean:
        payload["ean"] = ean

    if config.get("price_group_id") and product.price:
        payload["prices"] = {config["price_group_id"]: product.price}

    if config.get("warehouse_id"):
        payload["stock"] = {config["warehouse_id"]: product.quantity or 1}

    for api_field, product_attr in (
        ("length", "box_length_cm"),
        ("width", "box_width_cm"),
        ("height", "box_height_cm"),
    ):
        val = getattr(product, product_attr)
        if val:
            payload[api_field] = val
    if product.manifest_weight_kg:
        payload["weight"] = product.manifest_weight_kg

    if include_images and product.image_paths:
        payload["images"] = {
            str(i): encode_image(p) for i, p in enumerate(product.image_paths[:16])
        }

    return payload


def push_product(product) -> PushResult:
    """Creates or updates one product in BaseLinker. Never called
    automatically — always an explicit user action (see app.py's Export
    tab). Persists the resulting listing itself (via marketplace_store) on
    success — the caller does not need to separately save the product."""
    try:
        config = get_config()
    except BaseLinkerConfigError as e:
        return PushResult(success=False, message=str(e))

    existing = marketplace_store.get_listing(product.id, MARKETPLACE, product.company_id)
    existing_id = existing.external_listing_id if existing else ""
    if not existing_id:
        found_id = find_existing_product_id_by_sku(product.sku, config)
        if found_id:
            existing_id = found_id

    try:
        payload = build_payload(product, config, existing_listing_id=existing_id)
        data = _call("addInventoryProduct", payload, config["token"])
    except BaseLinkerAPIError as e:
        return PushResult(success=False, message=str(e))
    except Exception as e:
        return PushResult(success=False, message=f"Unexpected error: {e}")

    listing_id = str(data.get("product_id", existing_id))
    marketplace_store.upsert_listing(
        marketplace_store.MarketplaceListing(
            product_id=product.id,
            marketplace=MARKETPLACE,
            company_id=product.company_id,
            external_listing_id=listing_id,
            status=marketplace_store.STATUS_LISTED,
            price=product.price,
            last_synced_at=time.time(),
        )
    )

    return PushResult(
        success=True,
        baselinker_product_id=listing_id,
        message="Pushed successfully.",
        warnings=data.get("warnings", {}) or {},
    )
