"""Pure payload-building for BaseLinker's addInventoryProduct — no network
calls, no DB lookups (existing_listing_id is always passed in by the
caller), so this stays fully unit-testable on its own. Ported from the old
modules/baselinker_client.py, which this integration replaces.
"""
import base64
import io
from typing import Optional, Set

from PIL import Image

from integrations import field_labels_i18n

MAX_IMAGE_BASE64_BYTES = 2 * 1024 * 1024  # BaseLinker's documented cap

# Every text_fields key this module writes (name/description/
# description_extra1/features) is suffixed with the caller-supplied
# `language` — deliberately NEVER a bare/unsuffixed key, which BaseLinker
# resolves to the inventory's own "default catalog language" (this
# account's is German — confirmed via getInventories — so a bare
# "name"/"description" silently landed under the German tab regardless of
# what language the text actually was, while features already used an
# explicit suffix; that inconsistency is exactly what the explicit
# `language` param fixes). `language` must always match the language the
# `title`/`description`/`condition_description` strings actually are —
# see integrations/marketplaces/baselinker/client.py, which resolves the
# export language and fetches the matching modules/product_translation_store
# row BEFORE calling build_payload(); this module never translates or
# looks anything up itself, only places already-resolved strings.


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
    title: str = "", description: str = "", condition_description: str = "", language: str = "en",
    primary_language: str = "", primary_title: str = "", primary_description: str = "",
    primary_condition_description: str = "", color: str = "",
) -> dict:
    """Builds the addInventoryProduct parameters for one Product.

    `title`/`description`/`condition_description`/`language` are the
    already-resolved export content — this function never reads
    `product.name`/`.product_description`/`.condition_description`
    directly and never calls a translation provider or looks up a
    `product_translations` row itself (see client.py, which resolves the
    export language, fetches the matching translation, and passes plain
    strings in here — this module stays "pure": no DB access, no network).

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
    `text_fields["features|{language}"]` (BaseLinker's "Information ->
    Parameters" section) — see the `features` dict built below.
    category/defects still have no destination here — nothing to gate for
    those yet, see BaselinkerConnector.IMPLEMENTED_SYNC_FIELDS.
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
        text_fields[f"name|{language}"] = title or product.model_number or product.sku
    if _wanted("product_description"):
        text_fields[f"description|{language}"] = description or ""
    if _wanted("condition_description") and condition_description:
        text_fields[f"description_extra1|{language}"] = (
            f"Condition & Scratches Details: {condition_description}"
        )

    # BaseLinker requires a non-empty name (and, empirically, rejects the
    # whole addInventoryProduct call with "Product name not provided" if
    # it's missing) under whichever language the INVENTORY ACCOUNT itself
    # is configured with as its default catalog language — independent of
    # which language(s) text_fields otherwise contains. We don't track that
    # per-account setting ourselves, but `primary_language` (the product's
    # AI-authored original) is the one language guaranteed to exist for
    # every product, so it's always included too whenever the requested
    # export `language` differs — a small safety duplication, not a
    # second "real" export language.
    if primary_language and primary_language != language:
        if _wanted("name"):
            text_fields[f"name|{primary_language}"] = primary_title or product.model_number or product.sku
        if _wanted("product_description"):
            text_fields[f"description|{primary_language}"] = primary_description or ""
        if _wanted("condition_description") and primary_condition_description:
            text_fields[f"description_extra1|{primary_language}"] = (
                f"Condition & Scratches Details: {primary_condition_description}"
            )

    if existing_listing_id:
        payload["product_id"] = int(existing_listing_id)

    ean = product.ean or product.manifest_barcode or product.scanned_barcode

    # BaseLinker's "Information -> Parameters" section, same `language` as
    # name/description above. ElectroGrader is the source of truth for
    # these six (see integrations/field_registry.py's SYNC_OWNERSHIP_FIELDS),
    # so the whole dict is rebuilt fresh every call rather than patched —
    # BaselinkerConnector.update_product()'s _merge_text_fields() is what
    # keeps this from clobbering the product's OTHER language keys
    # (features|lv, name|fr, etc.) that live outside ElectroGrader's control.
    # Only the parameter NAMES (labels) are localized, via the static
    # field_labels_i18n dict — the VALUES below (product.brand, product.power,
    # etc.) are read straight off Product, exactly as always, never
    # translated by anything.
    features = {}
    if _wanted("brand") and product.brand:
        features[field_labels_i18n.label("brand", language)] = product.brand
    if _wanted("model") and product.model:
        features[field_labels_i18n.label("model", language)] = product.model
    if _wanted("product_condition") and product.product_condition:
        features[field_labels_i18n.label("product_condition", language)] = product.product_condition
    if _wanted("color") and color:
        # Deliberate exception to "parameter values are never translated"
        # (see the docstring above and modules/translation_service.py) —
        # `color` is resolved by the caller (client.py's
        # _resolve_export_content()), already in the export `language`.
        features[field_labels_i18n.label("color", language)] = color
    if _wanted("power") and product.power:
        features[field_labels_i18n.label("power", language)] = product.power
    if _wanted("barcode") and ean:
        features[field_labels_i18n.label("barcode", language)] = ean
    if features:
        text_fields[f"features|{language}"] = features

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
    # product.weight_kg is the authoritative, human-editable value (seeded
    # from manifest_weight_kg at import time when the manifest had it, but
    # editable afterward regardless of origin — see modules/models.py).
    # manifest_weight_kg itself is deliberately never sent: it's an
    # unverified manifest claim, same reasoning as quantity vs. manifest_qty.
    if _wanted("weight_kg") and product.weight_kg:
        payload["weight"] = product.weight_kg

    if _wanted("image_paths") and include_images and product.image_paths:
        payload["images"] = {
            str(i): encode_image(p) for i, p in enumerate(product.image_paths[:16])
        }

    return payload
