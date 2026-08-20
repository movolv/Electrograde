"""Data model for a graded electronics item.

Designed to support three future needs without further restructuring:
  1. Baselinker CSV/Excel export (see modules/export.py)
  2. A future API integration (this dataclass is a flat, JSON-serializable
     record via to_dict()/from_dict())
  3. Multi-company use with separate inventories (`company_id` scopes every
     record; modules/inventory_store.py filters by it)

Fields are grouped by origin/ownership so it's always clear who is allowed
to write to what:
  - MANIFEST fields are imported once from an Amazon liquidation manifest
    and are treated as unverified claims, never blindly trusted.
  - IDENTIFIERS are read during the photo-capture step (a physically
    scanned barcode), used to cross-check the manifest.
  - IDENTIFIER LOOKUP fields track `ean`/`asin` auto-discovery (see
    modules/identifier_lookup.py): a unified EAN value (from manifest, a
    physical scan, or a web search — first one found wins, never
    overwritten afterward), plus a status/source/candidates trail so a
    human can tell what to double-check.
  - AI fields are filled by spec lookup / vision grading — all editable by
    a human afterward, but never auto-written by manifest import itself.
  - VERIFICATION fields record whether the AI thinks the photographed item
    actually matches the manifest's claimed identity.
  - MANUAL-ONLY fields are things AI cannot reliably determine and are
    never touched by any AI function.
"""
from dataclasses import dataclass, field, asdict
from typing import Dict, List
import time
import uuid


@dataclass
class Product:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = "default"
    status: str = "draft"  # draft (manifest, unprocessed) -> in_progress -> completed
    sku: str = ""  # manual-only, mandatory before save, never written by AI
    # Which New Item wizard step this record was last checkpointed at
    # (1-6), while status == "in_progress" — lets a lost session (e.g. a
    # phone call interrupting mobile photo capture) resume exactly where
    # it left off instead of losing the work. Meaningless once status is
    # "draft" or "completed".
    wizard_step: int = 0

    # -- MANIFEST (imported as-is, read-only origin data; not authoritative) --
    manifest_import_id: str = ""
    manifest_target_no: str = ""
    manifest_subcategory: str = ""
    asin: str = ""
    manifest_barcode: str = ""  # EAN/Barcode as claimed by the manifest
    manifest_item_description: str = ""
    manifest_qty: int = 0
    manifest_weight_kg: float = 0.0

    # -- IDENTIFIERS (blank-start flow / physical verification) --
    scanned_barcode: str = ""  # decoded from a captured photo via pyzbar
    model_number: str = ""  # manual/typed lookup key (blank-start flow)

    # -- IDENTIFIER LOOKUP (unified EAN + ASIN auto-discovery) --
    ean: str = ""  # the product's canonical EAN/GTIN, once known
    ean_status: str = ""  # "Found" | "Not Found" | "Needs Verification"
    ean_source: str = ""  # e.g. "manifest", "scanned barcode", "web search: ...", "manual"
    asin_status: str = ""  # "Found" | "Not Found" | "Needs Verification"
    asin_source: str = ""
    asin_candidates: List[str] = field(default_factory=list)  # other plausible ASINs if ambiguous

    # -- AI-FILLED (editable afterward by a human) --
    brand: str = ""
    model: str = ""
    name: str = ""
    category: str = ""  # denormalized display-text copy — see category_id below
    # Identity reference into modules/category_store.py's per-company
    # Category Catalog — the source of truth. `category` above is kept as a
    # display/export-compatible copy, always written together with this
    # field (see category_store.rename_category() for how a later rename
    # keeps the two in sync); never set one without the other in new code.
    category_id: str = ""
    power: str = ""  # e.g. "1200W" — product's power/wattage rating
    condition_type: str = ""  # "New" | "Used"
    color: str = ""  # e.g. "Black" — determined from photos by vision_grading.grade_item()
    product_condition: str = ""  # "A" | "B" | "C" | "D" — quality grade (distinct from condition_type above)
    product_condition_confidence: int = 0
    product_condition_reasoning: str = ""
    price: float = 0.0
    price_reasoning: str = ""
    product_description: str = ""
    condition_description: str = ""
    spec_summary: str = ""
    box_contents: List[str] = field(default_factory=list)
    missing_components: List[str] = field(default_factory=list)
    defects: List[str] = field(default_factory=list)
    functional_checklist: List[str] = field(default_factory=list)

    # -- VERIFICATION (manifest claim vs. what photos actually show) --
    product_match: str = ""  # "YES" | "NO" | "" (not yet checked)
    match_confidence: int = 0
    match_notes: str = ""

    # -- TRIAGE (disposition after testing — manual-only, distinct from
    # `status` which tracks pipeline stage, and from `product_condition`
    # which is cosmetic condition A-D) --
    triage_status: str = "testing_pending"
    # "testing_pending" | "ready_for_sale" | "needs_repair" | "for_parts" | "written_off"

    # -- REVIEW WORKFLOW (Review & Export screen gate — distinct from
    # `status`/`triage_status` above and from marketplace_store, which
    # tracks per-marketplace listing state, not this app's review gate) --
    review_status: str = ""  # "" | "ready" | "edited" | "exported" | "failed"
    exported_at: float = 0.0  # last successful BaseLinker export timestamp

    # -- MANUAL-ONLY (AI cannot reliably determine these) --
    location: str = ""
    functional_test_result: str = ""  # "Working" | "Not Working" | "Not Tested"
    box_length_cm: float = 0.0
    box_width_cm: float = 0.0
    box_height_cm: float = 0.0
    # Authoritative, human-editable shipping weight — distinct from
    # manifest_weight_kg above (an unverified manifest claim), same
    # relationship as quantity vs. manifest_qty. Seeded from
    # manifest_weight_kg at manifest-import time when the manifest mapped a
    # weight column, but editable afterward regardless of origin. This is
    # the value every export/API path (BaseLinker included) actually sends.
    weight_kg: float = 0.0
    purchase_price_allocated: float = 0.0  # this item's share of the manifest/lot cost, for profit calc
    quantity: int = 1  # unit count this record represents; always >= 1, editable everywhere. Not
    # the same as `manifest_qty` above (an unverified manifest claim) — this is the
    # authoritative, human-confirmed count used by every export/API path.

    # -- CUSTOM FIELDS (self-service, per-company — see
    # modules/custom_field_store.py for the field DEFINITIONS; this is
    # only the VALUES, keyed by each definition's stable `key`) --
    custom_fields: Dict[str, str] = field(default_factory=dict)

    # -- media / bookkeeping --
    image_paths: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    # -- BASELINKER SYNC (idempotency tracking for the optional API push) --
    baselinker_product_id: str = ""  # set after first successful push; presence means "update" not "create"
    baselinker_synced_at: float = 0.0  # last successful push timestamp

    # -- CONTENT LANGUAGE (see modules/product_translation_store.py) --
    primary_language: str = "en"  # language name/product_description/condition_description/
    # defects were originally AI-authored in (whatever description_gen.py produced at
    # creation time — today always "en", but never assume that elsewhere). Set once,
    # never changed afterward. The company's default_product_language (display/export
    # preference) is a completely separate setting — see modules/company_store.py.

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Product":
        known = {k: v for k, v in d.items() if k in Product.__dataclass_fields__}
        if "status" not in known:
            # Records saved before the draft/in_progress/completed status
            # concept existed (pre manifest-import feature) were always
            # fully processed items — never default them to "draft".
            known["status"] = "completed"
        if "triage_status" not in known and known.get("status") == "completed":
            # Records saved before triage_status existed but already fully
            # processed were, by definition, treated as sellable at the time
            # — never default a real completed item to "testing_pending".
            known["triage_status"] = "ready_for_sale"
        if not known.get("review_status") and known.get("status") == "completed":
            # Records saved before the Review & Export gate existed but
            # already fully processed are ready for that screen now.
            known["review_status"] = "ready"
        if "product_condition" not in known and "grade" in d:
            # Records saved before the "grade" -> "product_condition" field
            # rename (the JSON blob still has the old key names) — translate
            # here so nothing is silently lost on next load. Never removed:
            # any record saved before this point in time keeps needing this.
            known["product_condition"] = d.get("grade", "")
            known["product_condition_confidence"] = d.get("grade_confidence", 0)
            known["product_condition_reasoning"] = d.get("grade_reasoning", "")
        return Product(**known)


@dataclass
class OrderAddress:
    full_name: str = ""
    company: str = ""
    address: str = ""
    city: str = ""
    postcode: str = ""
    state: str = ""
    country: str = ""
    country_code: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "OrderAddress":
        known = {k: v for k, v in (d or {}).items() if k in OrderAddress.__dataclass_fields__}
        return OrderAddress(**known)


@dataclass
class OrderItem:
    name: str = ""
    sku: str = ""
    ean: str = ""
    quantity: int = 1
    price: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "OrderItem":
        known = {k: v for k, v in (d or {}).items() if k in OrderItem.__dataclass_fields__}
        return OrderItem(**known)


@dataclass
class Order:
    """A pulled-in order from a connected marketplace/service integration —
    see integrations/base.py's MarketplaceConnector.fetch_orders() and
    modules/order_store.py. Deliberately integration-agnostic: nothing here
    (or in order_store.py, or the Orders page) ever assumes BaseLinker —
    that mapping lives entirely in each connector's own order_mapper.py.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    integration_type: str = ""  # which ElectroGrader connector synced this: "baselinker", later "woocommerce"...
    marketplace: str = ""  # normalized channel code for filtering/the grid column: "amazon", "allegro", "shop"...
    external_order_id: str = ""  # source system's stable order id (upsert key, with company_id+integration_type)
    order_number: str = ""  # "Number (in shop)" — the shop-visible order number
    order_source: str = ""  # human-readable source description (e.g. BaseLinker's order_source_info)
    customer_name: str = ""
    email: str = ""
    phone: str = ""
    items: List[OrderItem] = field(default_factory=list)
    item_count: int = 0
    items_summary: str = ""  # e.g. "3x USB Cable, 1x Charger" — for the list column
    price_total: float = 0.0
    currency: str = ""
    shipping_method: str = ""
    delivery_address: OrderAddress = field(default_factory=OrderAddress)
    invoice_address: OrderAddress = field(default_factory=OrderAddress)
    order_date: float = 0.0
    status_id: int = 0
    status_label: str = ""
    status_updated_at: float = 0.0
    customer_comments: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Order":
        known = {k: v for k, v in d.items() if k in Order.__dataclass_fields__}
        if "items" in known:
            known["items"] = [OrderItem.from_dict(i) for i in (known["items"] or [])]
        if "delivery_address" in known:
            known["delivery_address"] = OrderAddress.from_dict(known["delivery_address"])
        if "invoice_address" in known:
            known["invoice_address"] = OrderAddress.from_dict(known["invoice_address"])
        return Order(**known)
