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
    "grade": {"label": "Grade", "type": "text"},
    "image_paths": {"label": "Images", "type": "images"},
    "price": {"label": "Price", "type": "number"},
    "quantity": {"label": "Quantity", "type": "number"},
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
