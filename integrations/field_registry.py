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
    "name": {"label": "Product title", "type": "text"},
    "brand": {"label": "Brand", "type": "text"},
    "model": {"label": "Model", "type": "text"},
    "product_description": {"label": "Description", "type": "text"},
    "condition_description": {"label": "Additional description", "type": "text"},
    "defects": {"label": "Defects", "type": "list"},
    "product_condition": {"label": "Product Condition", "type": "text"},
    "color": {"label": "Color", "type": "text"},
    "power": {"label": "Power", "type": "text"},
    "image_paths": {"label": "Images", "type": "images"},
    "price": {"label": "Price", "type": "number"},
    "quantity": {"label": "Quantity", "type": "number"},
    "weight_kg": {"label": "Weight (kg)", "type": "number"},
    "sku": {"label": "SKU", "type": "identifier"},
    "category": {"label": "Category", "type": "text"},
    "barcode": {"label": "Barcode", "type": "identifier"},
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
