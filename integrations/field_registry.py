"""Single source of truth for every ElectroGrader product field a
marketplace/service integration could sync — used by the Settings ->
Integrations Synchronization tab's checkboxes, Export Preview, Field
Mapping's "Source field" dropdown, and (future) connector validation.
Adding a field here makes it available everywhere at once; no integration's
UI code ever lists field names/labels directly.

`type` is a light rendering/validation hint (e.g. Preview formats an
"images" field as a count, not raw text) — not a strict schema.

Whether a given connector's payload actually HONORS a field's toggle is a
separate, connector-owned concern — see MarketplaceConnector.
IMPLEMENTED_SYNC_FIELDS / DEFAULT_SYNC_FIELDS in integrations/base.py.
"""

SYNCABLE_FIELDS = {
    # `mappable` (added for the dynamic Field Mapping redesign — see
    # integrations/base.py's MarketplaceConnector.get_mappable_source_fields()
    # and integrations/marketplaces/baselinker/mapper.py's docstring):
    # whether this field COULD ever be a Field Mapping "Source field", at
    # the registry (cross-connector) level. False here means every
    # marketplace integration needs special structural handling for this
    # field (an identity key, a media pipeline, a keyed sub-structure tied
    # to per-account config, or — for "category" — an entirely separate,
    # dedicated mapping feature, see modules/category_mapping_store.py) —
    # never a plain "redirect this to another named field" candidate. A
    # concrete connector MAY further narrow True down (see
    # BaselinkerConnector.CORE_FIELDS for e.g. "name", a BaseLinker-
    # specific mandatory-field/primary-language-fallback coupling that
    # doesn't apply to every platform), but never widen a False here back
    # to mappable — this registry's False is a floor, not a suggestion.
    "name": {"label": "Product title", "type": "text", "mappable": True},
    "brand": {"label": "Brand", "type": "text", "mappable": True},
    "model": {"label": "Model", "type": "text", "mappable": True},
    "product_description": {"label": "Description", "type": "text", "mappable": True},
    "condition_description": {"label": "Additional description", "type": "text", "mappable": True},
    "defects": {"label": "Defects", "type": "list", "mappable": True},
    "product_condition": {"label": "Product Condition", "type": "text", "mappable": True},
    "color": {"label": "Color", "type": "text", "mappable": True},
    "power": {"label": "Power", "type": "text", "mappable": True},
    "image_paths": {"label": "Images", "type": "images", "mappable": False},  # media pipeline, not a field placement
    "price": {"label": "Price", "type": "number", "mappable": False},  # tied to a per-account price_group_id, not a named field
    "quantity": {"label": "Quantity", "type": "number", "mappable": False},  # tied to a per-account warehouse_id, not a named field
    "weight_kg": {"label": "Weight (kg)", "type": "number", "mappable": True},
    "sku": {"label": "SKU", "type": "identifier", "mappable": False},  # identity/dedup key, never redirectable
    "category": {"label": "Category", "type": "text", "mappable": False},  # owned by Category Mapping, not Field Mapping
    "barcode": {"label": "Barcode", "type": "identifier", "mappable": True},
}

RECEIVABLE_FIELDS = {
    "categories": {"label": "Categories", "type": "text"},
    "orders": {"label": "Orders", "type": "text"},
    "stock_changes": {"label": "Stock changes", "type": "text"},
    "sales_status": {"label": "Sales status", "type": "text"},
    "listing_status": {"label": "Listing status", "type": "text"},
}

# Sync Ownership: which SYSTEM is the master/source-of-truth for a given
# field, once real two-way sync exists (modules/sync_ownership_store.py,
# Settings -> Integrations -> [connector] -> "Sync Ownership" tab). A
# deliberately separate concern from SYNCABLE_FIELDS above (which is about
# whether ElectroGrader SENDS a field at all) — ownership is about who
# WINS when both sides could plausibly have a value. "status" is a new
# concept here (the marketplace listing/sale status) with no equivalent in
# SYNCABLE_FIELDS/RECEIVABLE_FIELDS.
#
# default_owner is either "electrograder", "manual", or the sentinel
# "<connector>" (meaning "whichever connector this config is for" — e.g.
# "baselinker" — resolved by modules/sync_ownership_store.get_field_owner(),
# never hardcoded to one integration here).
SYNC_OWNERSHIP_FIELDS = {
    "name": {"label": "Title", "group": "Product Content", "default_owner": "electrograder"},
    "product_description": {"label": "Description", "group": "Product Content", "default_owner": "electrograder"},
    "image_paths": {"label": "Images", "group": "Product Content", "default_owner": "electrograder"},
    "brand": {"label": "Brand", "group": "Product Content", "default_owner": "electrograder"},
    "model": {"label": "Model", "group": "Product Content", "default_owner": "electrograder"},
    "category": {"label": "Category", "group": "Product Content", "default_owner": "electrograder"},
    "product_condition": {"label": "Product Condition", "group": "Product Content", "default_owner": "electrograder"},
    "color": {"label": "Color", "group": "Product Content", "default_owner": "electrograder"},
    "power": {"label": "Power", "group": "Product Content", "default_owner": "electrograder"},
    "defects": {"label": "Defects", "group": "Product Content", "default_owner": "electrograder"},
    "barcode": {"label": "Barcode", "group": "Product Content", "default_owner": "electrograder"},
    "price": {"label": "Price", "group": "Sales Data", "default_owner": "electrograder"},
    "weight_kg": {"label": "Weight (kg)", "group": "Sales Data", "default_owner": "electrograder"},
    "quantity": {"label": "Quantity", "group": "Sales Data", "default_owner": "<connector>"},
    "status": {"label": "Status", "group": "Sales Data", "default_owner": "<connector>"},
}

SOURCE_ELECTROGRADER = "electrograder"
SOURCE_MANUAL = "manual"
