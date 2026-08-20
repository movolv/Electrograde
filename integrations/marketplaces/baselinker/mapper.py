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

# Which SYNCABLE_FIELDS keys a saved Settings -> Field Mapping rule is
# allowed to redirect away from their default spot is no longer a
# hardcoded constant here — it's passed in per-call as
# `redirectable_fields` (see build_payload() below), computed by the
# CALLER from BaselinkerConnector.get_mappable_source_fields() (itself
# derived from integrations/field_registry.SYNCABLE_FIELDS's `mappable`
# flag, minus this connector's own CORE_FIELDS) — see the approved
# dynamic Field Mapping redesign. mapper.py stays fully DB-free/pure:
# it never imports field_registry or reaches for a connector itself,
# only ever acts on whatever set the caller hands it.


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
    field_mapping_rules: Optional[list] = None,
    redirectable_fields: Optional[Set[str]] = None,
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
    `fields_send`. "category" has no destination here at all — it's owned
    exclusively by the separate Category Mapping feature, resolved into
    `config["category_id"]` before this function is ever called (see
    BaselinkerConnector._resolve_category_id()).

    `field_mapping_rules` (default None -> no-op, unchanged behavior) is a
    plain list of modules.field_mapping_store.FieldMappingRule — this stays
    "pure" per the note above by never fetching them itself; client.py
    resolves the company's saved Settings -> Field Mapping rules and passes
    them in as data, same pattern as `title`/`description`/etc.

    `redirectable_fields` (default None -> treated as empty, nothing
    redirectable) is the set of SYNCABLE_FIELDS keys THIS connector
    currently allows a rule to fully redirect — computed by the caller via
    BaselinkerConnector.get_mappable_source_fields() (itself derived from
    integrations/field_registry.SYNCABLE_FIELDS's `mappable` flag; see the
    approved dynamic Field Mapping redesign). This function never
    hardcodes which fields those are — it only ever asks "is this
    field_key IN the set the caller gave me", so it stays correct as that
    set grows without needing its own changes.

    A rule with an empty `source_value` (a "redirect", not a value
    translation) on a field_key IN `redirectable_fields` SKIPS that
    field's default placement below entirely — the rule fully owns where
    it goes instead, applied at the very end by
    _apply_field_mapping_rules(). Without this, "remapping" a field in
    Settings -> Field Mapping would just add a second copy at the new
    location while the old default entry kept showing it too, which is
    not what a user asking to move a field expects. A value-specific rule
    (non-empty source_value) never suppresses the default — it only adds
    an extra placement for that one matching value, leaving every other
    value exactly where it already was. Every field NOT in
    `redirectable_fields` (name, price, quantity, box dimensions, sku,
    image_paths, category) ignores redirects entirely for its own default
    placement — only a rule's presence in the "additive, same spot as
    always" sense (value-specific) can add a second copy elsewhere for
    those, and "sku" additionally refuses ANY rule outright (see
    _apply_field_mapping_rules).
    """
    redirects = {r.source_field for r in (field_mapping_rules or []) if r.source_field and not r.source_value}
    redirectable = redirectable_fields or set()

    def _wanted(field_key: str) -> bool:
        return fields_send is None or field_key in fields_send

    def _default_wanted(field_key: str) -> bool:
        """Gate for a redirectable field's OWN default placement: both the
        existing Synchronization on/off check AND "no rule has fully
        redirected this field elsewhere". A field outside
        `redirectable_fields` can never appear in `redirects` in a way
        that matters here (nothing in the UI lets a rule target one), so
        this is a safe no-op gate for those — equivalent to plain
        `_wanted`."""
        return _wanted(field_key) and not (field_key in redirectable and field_key in redirects)

    payload = {
        "inventory_id": config["inventory_id"],
        "sku": product.sku,
        "category_id": config["category_id"],
        "tax_rate": config["tax_rate"],
    }

    text_fields = {}
    if _wanted("name"):  # never redirectable — see CORE_FIELDS
        text_fields[f"name|{language}"] = title or product.model_number or product.sku
    if _default_wanted("product_description"):
        text_fields[f"description|{language}"] = description or ""
    if _default_wanted("condition_description") and condition_description:
        label = field_labels_i18n.label("condition_scratches_details", language)
        text_fields[f"description_extra1|{language}"] = f"{label}: {condition_description}"

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
        if _default_wanted("product_description"):
            text_fields[f"description|{primary_language}"] = primary_description or ""
        if _default_wanted("condition_description") and primary_condition_description:
            primary_label = field_labels_i18n.label("condition_scratches_details", primary_language)
            text_fields[f"description_extra1|{primary_language}"] = (
                f"{primary_label}: {primary_condition_description}"
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
    # translated by anything. brand/model/product_condition/color/power use
    # `_default_wanted` (not plain `_wanted`) since these are redirectable
    # out of this block entirely — see `redirectable_fields`/`redirects`
    # above. barcode's Parameters entry stays unconditional (`_wanted`, not
    # `_default_wanted`): only its own top-level `ean` placement below is
    # the "real" barcode field a rule can redirect; this Parameters copy is
    # just a convenience duplicate BaseLinker also shows there, unaffected
    # by where the "real" placement goes.
    features = {}
    if _default_wanted("brand") and product.brand:
        features[field_labels_i18n.label("brand", language)] = product.brand
    if _default_wanted("model") and product.model:
        features[field_labels_i18n.label("model", language)] = product.model
    if _default_wanted("product_condition") and product.product_condition:
        features[field_labels_i18n.label("product_condition", language)] = product.product_condition
    if _default_wanted("color") and color:
        # Deliberate exception to "parameter values are never translated"
        # (see the docstring above and modules/translation_service.py) —
        # `color` is resolved by the caller (client.py's
        # _resolve_export_content()), already in the export `language`.
        features[field_labels_i18n.label("color", language)] = color
    if _default_wanted("power") and product.power:
        features[field_labels_i18n.label("power", language)] = product.power
    if _wanted("barcode") and ean:
        features[field_labels_i18n.label("barcode", language)] = ean
    if features:
        text_fields[f"features|{language}"] = features

    if text_fields:
        payload["text_fields"] = text_fields

    if _default_wanted("barcode") and ean:
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
    if _default_wanted("weight_kg") and product.weight_kg:
        payload["weight"] = product.weight_kg

    if _wanted("image_paths") and include_images and product.image_paths:
        payload["images"] = {
            str(i): encode_image(p) for i, p in enumerate(product.image_paths[:16])
        }

    if field_mapping_rules:
        _apply_field_mapping_rules(payload, text_fields, product, field_mapping_rules, language)
        if text_fields:
            payload["text_fields"] = text_fields  # re-assign: a rule may add the FIRST text_fields entry

    return payload


def _apply_field_mapping_rules(
    payload: dict, text_fields: dict, product, rules: list, language: str,
) -> None:
    """Overlays a company's saved Settings -> Field Mapping rules on top of
    the default payload already built above — lets a company redirect any
    field_registry.SYNCABLE_FIELDS entry with mappable=True (minus this
    connector's own CORE_FIELDS — see get_mappable_source_fields()) onto
    ITS OWN BaseLinker target (a specific extra_field_<id>, confirmed via
    BaselinkerConnector.get_target_fields() against a real account's
    getInventoryExtraFields; a named per-language text slot; the shared
    Parameters block; or one of the remaining static top-level fields this
    module has no default placement for, e.g. condition_id) and/or
    translate a specific value (e.g. product_condition "B" -> "Ļoti
    labs"). Deliberately applied LAST: a matching rule always wins over
    whatever this module already placed by default. Fully generic over
    `rule.source_field` — this function itself never hardcodes which
    fields are allowed; that's enforced upstream by what the Field Mapping
    UI ever lets a company save a rule for in the first place (see
    app.py's _render_field_mapping_tab).

    A rule with source_value == "" matches the field regardless of its
    current value — a pure redirect (its default placement, if it had
    one, was already skipped above via `redirects`/`_default_wanted`, see
    build_payload() — only matters for fields in `redirectable_fields`;
    a field with no default placement at all, e.g. "defects", has nothing
    to skip either way). A non-empty source_value only applies when the
    field's actual current value equals it exactly — a value translation
    for that one specific value, leaving every other value on that field
    to whatever a DIFFERENT rule (or the default placement) already gave
    it. "sku" redirects are always refused (BaseLinker's own de-dup/SKU-
    fallback matching depends on it always landing at the top-level "sku"
    key — see BaselinkerConnector._find_existing_by_sku()) — this is the
    one field-specific exception left in this function, since it's a
    structural identity guarantee, not a mapping preference.

    List-valued fields (e.g. defects) are joined with "; " — BaseLinker's
    text fields are plain strings, there's no native list type to hand
    them a Python list directly.

    target_field starting with "extra_field_" writes into `text_fields`
    under BaseLinker's own extra_field_<id>|<language> convention; one
    starting with "features:" (BaselinkerConnector.STRUCTURAL_TARGET_FIELDS
    — what the Field Mapping table's default preview rows for brand/model/
    product_condition/color/power point at, e.g. "features:brand", so
    saving them unchanged is a genuine no-op back to the same spot they
    already occupy) writes into the SAME shared Parameters block as the
    default placement above, under that field's own i18n label; one
    starting with "text:" (e.g. "text:description",
    "text:description_extra1" — product_description's/
    condition_description's own default slots) writes into `text_fields`
    under that exact BaseLinker text_fields key, same "no rule = no
    change" no-op reasoning as "features:"; any other target_field
    (condition_id, ean, weight, or any future plain top-level key) is
    written directly onto the top-level payload dict."""
    for rule in rules:
        if not rule.source_field or not rule.target_field or rule.source_field == "sku":
            continue
        if rule.source_field.startswith("custom:"):
            # A company's own self-service custom field (see
            # modules/custom_field_store.py) — its VALUE lives in
            # Product.custom_fields, keyed by the definition's stable
            # `key` (everything after the "custom:" prefix), never as a
            # real Product attribute.
            custom_key = rule.source_field.split(":", 1)[1]
            current = (product.custom_fields or {}).get(custom_key)
        else:
            current = getattr(product, rule.source_field, None)
        if current is None or current == "" or current == []:
            continue
        current_str = "; ".join(str(v) for v in current) if isinstance(current, list) else str(current)
        if not current_str:
            continue

        if rule.source_value and rule.source_value != current_str:
            continue
        value = rule.target_value or rule.target_label or current_str
        target = rule.target_field
        if target.startswith("extra_field_"):
            text_fields[f"{target}|{language}"] = value
        elif target.startswith("features:"):
            label_field = target.split(":", 1)[1]
            label = field_labels_i18n.label(label_field, language)
            text_fields.setdefault(f"features|{language}", {})[label] = value
        elif target.startswith("text:"):
            slot = target.split(":", 1)[1]
            text_fields[f"{slot}|{language}"] = value
        else:
            payload[target] = value
