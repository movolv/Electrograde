"""Pure payload-building for BaseLinker's addInventoryProduct — no network
calls, no DB lookups (existing_listing_id is always passed in by the
caller), so this stays fully unit-testable on its own. Ported from the old
modules/baselinker_client.py, which this integration replaces.
"""
import base64
import io
from typing import Optional, Set

from PIL import Image

MAX_IMAGE_BASE64_BYTES = 2 * 1024 * 1024  # BaseLinker's documented cap

# Every text_fields key this module writes (name/description/
# description_extra1/features) is suffixed with this language — deliberately
# NEVER the bare/unsuffixed key, which BaseLinker resolves to the
# inventory's own "default catalog language" (this account's is German —
# confirmed via getInventories — so a bare "name"/"description" silently
# landed under the German tab regardless of what language the text actually
# was, while features already used the explicit "|en" suffix; that
# inconsistency is exactly what this constant fixes). Single change point if
# a future company needs a different content language.
CONTENT_LANGUAGE = "en"


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
    fields_send: Optional[Set[str]] = None,
) -> dict:
    """Builds the addInventoryProduct parameters for one Product.

    `fields_send=None` (the default) sends everything unconditionally —
    the exact behavior this function always had, still used for companies
    with no Synchronization configuration saved yet (see
    BaselinkerConnector._resolve_fields_send()). When a real set is given,
    only the integrations/field_registry.SYNCABLE_FIELDS keys it contains
    are included, for every field that already has a real destination
    here. `sku` is deliberately never gated — it's a structural product
    identifier (used for BaseLinker's own SKU-fallback matching and our
    de-dup logic), not optional content, so it's always sent regardless of
    `fields_send`. brand/model/product_condition/color/power all land in
    `text_fields["features|en"]` (BaseLinker's "Information -> Parameters"
    section) — see the `features` dict built below. category/defects still
    have no destination here — nothing to gate for those yet, see
    BaselinkerConnector.IMPLEMENTED_SYNC_FIELDS.
    """
    def _wanted(field_key: str) -> bool:
        return fields_send is None or field_key in fields_send

    payload = {
        "inventory_id": config["inventory_id"],
        "sku": product.sku,
        "category_id": config["category_id"],
        "tax_rate": config["tax_rate"],
    }

    text_fields = {}
    if _wanted("name"):
        text_fields[f"name|{CONTENT_LANGUAGE}"] = product.name or product.model_number or product.sku
    if _wanted("product_description"):
        text_fields[f"description|{CONTENT_LANGUAGE}"] = product.product_description or ""
    if _wanted("condition_description") and product.condition_description:
        text_fields[f"description_extra1|{CONTENT_LANGUAGE}"] = (
            f"Condition & Scratches Details: {product.condition_description}"
        )

    if existing_listing_id:
        payload["product_id"] = int(existing_listing_id)

    ean = product.ean or product.manifest_barcode or product.scanned_barcode

    # BaseLinker's "Information -> Parameters" section, same CONTENT_LANGUAGE
    # as name/description above. ElectroGrader is the source of truth for
    # these six (see integrations/field_registry.py's SYNC_OWNERSHIP_FIELDS),
    # so the whole dict is rebuilt fresh every call rather than patched —
    # BaselinkerConnector.update_product()'s _merge_text_fields() is what
    # keeps this from clobbering the product's OTHER language keys
    # (features|lv, name|fr, etc.) that live outside ElectroGrader's control.
    features = {}
    if _wanted("brand") and product.brand:
        features["Brand"] = product.brand
    if _wanted("model") and product.model:
        features["Model"] = product.model
    if _wanted("product_condition") and product.product_condition:
        features["Condition"] = product.product_condition
    if _wanted("color") and product.color:
        features["Color"] = product.color
    if _wanted("power") and product.power:
        features["Power"] = product.power
    if _wanted("barcode") and ean:
        features["EAN"] = ean
    if features:
        text_fields[f"features|{CONTENT_LANGUAGE}"] = features

    if text_fields:
        payload["text_fields"] = text_fields

    if _wanted("barcode") and ean:
        payload["ean"] = ean

    if _wanted("price") and config.get("price_group_id") and product.price:
        payload["prices"] = {config["price_group_id"]: product.price}

    if _wanted("quantity") and config.get("warehouse_id"):
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

    if _wanted("image_paths") and include_images and product.image_paths:
        payload["images"] = {
            str(i): encode_image(p) for i, p in enumerate(product.image_paths[:16])
        }

    return payload
