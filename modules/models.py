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
from typing import List
import time
import uuid


@dataclass
class Product:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = "default"
    status: str = "draft"  # draft (manifest, unprocessed) -> in_progress -> completed
    sku: str = ""  # manual-only, mandatory before save, never written by AI

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
    category: str = ""
    condition_type: str = ""  # "New" | "Used"
    grade: str = ""
    grade_confidence: int = 0
    grade_reasoning: str = ""
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

    # -- MANUAL-ONLY (AI cannot reliably determine these) --
    location: str = ""
    functional_test_result: str = ""  # "Working" | "Not Working" | "Not Tested"
    box_length_cm: float = 0.0
    box_width_cm: float = 0.0
    box_height_cm: float = 0.0

    # -- media / bookkeeping --
    image_paths: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    # -- BASELINKER SYNC (idempotency tracking for the optional API push) --
    baselinker_product_id: str = ""  # set after first successful push; presence means "update" not "create"
    baselinker_synced_at: float = 0.0  # last successful push timestamp

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
        return Product(**known)
