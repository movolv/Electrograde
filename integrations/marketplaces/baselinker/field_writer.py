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
    product, field_name: str, config: dict, existing_listing_id: Optional[str] = None,
    field_mapping_rules: Optional[list] = None,
) -> dict:
    return mapper.build_payload(
        product, config, existing_listing_id=existing_listing_id, fields_send={field_name},
        field_mapping_rules=field_mapping_rules,
    )
