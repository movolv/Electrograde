"""Pure payload-building for BaseLinker's addInventoryProduct — no network
calls, no DB lookups (existing_listing_id is always passed in by the
caller), so this stays fully unit-testable on its own. Ported from the old
modules/baselinker_client.py, which this integration replaces.
"""
import base64
import io
from typing import Optional

from PIL import Image

MAX_IMAGE_BASE64_BYTES = 2 * 1024 * 1024  # BaseLinker's documented cap


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


def build_payload(
    product, config: dict, include_images: bool = True, existing_listing_id: Optional[str] = None,
) -> dict:
    """Builds the addInventoryProduct parameters for one Product."""
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
