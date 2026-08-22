"""Builds the BaseLinker payload for pushing exactly ONE field — the
counterpart to field_reader.py's read side. Deliberately does NOT
reimplement mapper.py's field-name -> payload-key logic (that would be a
second place for the same mapping to drift out of sync); instead it
delegates straight to the already-proven mapper.build_payload() with
fields_send narrowed to a single field, which already includes the
required structural fields (sku/category_id/tax_rate) and the correct key
for every syncable field.
"""
from typing import Optional

from integrations.marketplaces.baselinker import mapper


def build_partial_payload(
    product, field_name: str, config: dict, export_content: dict,
    existing_listing_id: Optional[str] = None,
    field_mapping_rules: Optional[list] = None,
    inventory_default_language: str = "",
    redirectable_fields: Optional[set] = None,
) -> dict:
    """`export_content` is BaselinkerConnector._resolve_export_content()'s
    output, passed in whole — the SAME resolved language and strings the
    full export uses.

    It used to be omitted entirely, which was silently wrong twice over:
    build_payload()'s `language` defaulted to "en", so every single-field
    push wrote name|en / description|en / features|en regardless of the
    company's chosen language; and `title`/`description` defaulted to "",
    so pushing "name" sent `product.model_number or product.sku` instead of
    the real title, and pushing "product_description" sent "" — actively
    blanking a description that existed. `language` is now a required
    argument of build_payload(), so this can no longer drift back.
    """
    return mapper.build_payload(
        product, config, export_content["language"],
        existing_listing_id=existing_listing_id, fields_send={field_name},
        title=export_content["title"], description=export_content["description"],
        condition_description=export_content["condition_description"],
        color=export_content["color"],
        field_mapping_rules=field_mapping_rules,
        redirectable_fields=redirectable_fields,
        inventory_default_language=inventory_default_language,
    )
