"""Amazon liquidation manifest import.

The manifest is treated purely as a *starting point* — only the 7 required
fields are pulled in; everything else (brand, model, product condition, price, etc.) is
determined later by AI analysis + human review, never assumed from the
manifest. See modules/vision_grading.py for how the manifest's claims get
cross-checked against the photographed item.

Supports .xlsx and .csv. Column names are auto-matched case-insensitively
against common header variants, but the UI always lets the user review and
override the mapping before anything is imported — see
`auto_detect_columns` / `extract_rows` below, used separately so the app can
render an editable confirmation step in between.
"""
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import pandas as pd

from modules.models import Product

# Canonical field -> list of header variants we'll match against (lowercased,
# whitespace/punctuation-insensitive comparison — see _normalize_header).
FIELD_HEADER_ALIASES: Dict[str, List[str]] = {
    "target_no": ["target nr", "target no", "target number", "target #", "target"],
    "subcategory": ["subcategory", "sub category", "sub-category"],
    "asin": ["asin"],
    "ean_barcode": ["ean / barcode", "ean/barcode", "ean", "barcode", "upc", "upc/ean"],
    "item_description": ["item description", "description", "product description", "item name"],
    "qty": ["qty", "quantity", "units"],
    "weight_kg": ["weight (kg)", "weight kg", "weight", "item weight (kg)"],
    "sku": ["sku", "seller sku", "sku #"],
    "location": ["location", "shelf", "shelf location", "bin", "bin location", "warehouse location"],
}

FIELD_LABELS: Dict[str, str] = {
    "target_no": "Target #",
    "subcategory": "Subcategory",
    "asin": "ASIN",
    "ean_barcode": "EAN / Barcode",
    "item_description": "Item description",
    "qty": "Qty",
    "weight_kg": "Weight (kg)",
    "sku": "SKU",
    "location": "Shelf location",
}

# Only item_description is truly indispensable — without it there's nothing
# to identify or search for. target_no/asin/ean_barcode are deliberately
# NOT required: a manifest missing them is exactly what the identifier-
# lookup step (find_identifiers(), run automatically after import) exists
# to fill in — requiring them up front would defeat that feature and block
# real manifests that simply don't carry every column.
REQUIRED_FIELDS = ["item_description"]


def _normalize_header(h: str) -> str:
    return "".join(ch for ch in str(h).lower().strip() if ch.isalnum())


def read_table(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Reads the raw uploaded file into a DataFrame of strings, unmodified
    beyond stripping header whitespace — no column matching happens here."""
    if filename.lower().endswith(".csv"):
        df = pd.read_csv(BytesIO(file_bytes), dtype=str)
    else:
        df = pd.read_excel(BytesIO(file_bytes), dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def auto_detect_columns(columns: List[str]) -> Dict[str, str]:
    """Best-guess {canonical_field: actual_column_name} mapping. Always
    meant to be shown to the user for confirmation/override, never applied
    blindly."""
    normalized_lookup = {_normalize_header(c): c for c in columns}
    result = {}
    for canonical, aliases in FIELD_HEADER_ALIASES.items():
        for alias in aliases:
            norm_alias = _normalize_header(alias)
            if norm_alias in normalized_lookup:
                result[canonical] = normalized_lookup[norm_alias]
                break
    return result


def extract_rows(df: pd.DataFrame, column_map: Dict[str, str]) -> List[dict]:
    """Extracts only the canonical fields present in column_map (a
    user-confirmed mapping) from the raw dataframe. Blank rows are skipped."""
    rows = []
    for _, r in df.iterrows():
        row = {}
        for canonical, actual_col in column_map.items():
            val = r.get(actual_col, "")
            row[canonical] = "" if pd.isna(val) else str(val).strip()
        if any(row.values()):
            rows.append(row)
    _fix_weight_units(rows)
    return rows


# Above this median, a manifest's "weight (kg)" column is almost certainly
# actually reporting grams — no batch of consumer electronics/appliances has
# a typical per-item weight of 50+ kg. Observed directly: one real manifest
# had weights like 219-22199 "kg" (median ~6500) for items like blenders and
# grills, which only makes sense as grams (0.22-22.2 kg). Detecting this from
# the whole batch's median, rather than any single row, avoids ambiguity —
# an individual value like "850" alone could plausibly be either unit, but a
# manifest-wide median of ~6500 cannot be a real kg reading.
_GRAMS_MISLABELED_AS_KG_MEDIAN_THRESHOLD = 50.0


def _fix_weight_units(rows: List[dict]) -> None:
    """Mutates rows in place: if the batch's median weight_kg value implies
    the column is actually in grams, convert every row's weight_kg to kg."""
    values = []
    for row in rows:
        try:
            w = float(row.get("weight_kg", "") or 0)
        except ValueError:
            continue
        if w > 0:
            values.append(w)

    if not values:
        return

    values.sort()
    median = values[len(values) // 2]
    if median <= _GRAMS_MISLABELED_AS_KG_MEDIAN_THRESHOLD:
        return

    for row in rows:
        try:
            w = float(row.get("weight_kg", "") or 0)
        except ValueError:
            continue
        if w > 0:
            row["weight_kg"] = str(w / 1000.0)


def _parse_qty_weight(row: dict) -> Tuple[int, float]:
    try:
        qty = int(float(row.get("qty", "") or "0"))
    except ValueError:
        qty = 0
    try:
        weight_kg = float(row.get("weight_kg", "") or "0")
    except ValueError:
        weight_kg = 0.0
    return qty, weight_kg


def _apply_manifest_fields(product: Product, row: dict) -> None:
    """Sets/refreshes ONLY the manifest-origin field group (see
    modules/models.py's grouping) on an existing Product — never touches
    sku, status, or any AI/manual work already done on it."""
    qty, weight_kg = _parse_qty_weight(row)
    product.manifest_target_no = row.get("target_no", "")
    product.manifest_subcategory = row.get("subcategory", "")
    product.asin = row.get("asin", "")
    product.manifest_barcode = row.get("ean_barcode", "")
    product.manifest_item_description = row.get("item_description", "")
    product.manifest_qty = qty
    product.manifest_weight_kg = weight_kg


def rows_to_draft_products(
    rows: List[dict],
    company_id: str = "default",
    manifest_import_id: Optional[str] = None,
) -> List[Product]:
    """Create one draft Product per manifest row. Only the 7 manifest
    fields are populated — everything AI/human-filled starts blank."""
    import_id = manifest_import_id or uuid.uuid4().hex[:10]
    products = []
    for row in rows:
        p = Product(company_id=company_id, status="draft", manifest_import_id=import_id)
        _apply_manifest_fields(p, row)
        # Seed the authoritative `quantity` from the manifest's claimed qty,
        # once, at creation — falls back to the dataclass default (1) if the
        # manifest didn't specify one. Only done here (not inside
        # _apply_manifest_fields, which sync_rows_to_products() also calls on
        # already-in-progress products) so a later re-sync of the same batch
        # never clobbers a quantity a human has since corrected.
        if p.manifest_qty > 0:
            p.quantity = p.manifest_qty
        # Same one-time-at-creation treatment for a manifest-provided SKU
        # (blank if the file had no SKU column/value for this row — the
        # caller then falls back to inventory_store.next_sku_batch() for
        # exactly those). _apply_manifest_fields() itself must never touch
        # sku, since it also runs against already-existing matched products
        # in the Replace flow (see its own docstring).
        p.sku = row.get("sku", "")
        # Same reasoning for a manifest-provided shelf location: seeded once
        # at creation only, never touched by _apply_manifest_fields() so a
        # Replace-sync of an already-placed product never overwrites a
        # human's since-corrected shelf location.
        p.location = row.get("location", "")
        products.append(p)
    return products


def check_sku_collisions(rows: List[dict], company_id: str) -> Dict[int, str]:
    """Returns {row_index: sku} for every row whose manifest-provided SKU
    already belongs to another product in this SAME company's inventory
    (tenant-scoped via inventory_store.list_skus(company_id) — a SKU
    colliding in a different company is not a collision at all). Read-only;
    used by the Import/Replace preview to flag rows before anything is
    saved, and again at save time to know which rows must fall back to a
    fresh auto-generated SKU instead of the manifest's own value."""
    from modules import inventory_store

    # Uppercased for comparison — SKU lookups elsewhere in this codebase
    # (e.g. inventory_store.get_product_by_sku()) are case-insensitive, so
    # collision detection matches that convention rather than being
    # stricter (and missing a real collision) or looser than the rest of
    # the app.
    existing = {s.upper() for s in inventory_store.list_skus(company_id)}
    collisions: Dict[int, str] = {}
    for i, row in enumerate(rows):
        sku = (row.get("sku") or "").strip()
        if sku and sku.upper() in existing:
            collisions[i] = sku
    return collisions


def sync_rows_to_products(
    rows: List[dict],
    existing_products: List[Product],
    company_id: str = "default",
    manifest_import_id: Optional[str] = None,
) -> Tuple[List[Product], List[Product]]:
    """Used by the "Replace" flow: matches a re-uploaded manifest's rows
    against the products already linked to this batch, so re-importing an
    updated version of the same manifest UPDATES matching rows in place
    instead of creating duplicates. Matching priority: Target # (most
    specific to a single manifest row), then ASIN, then EAN — the same
    "most precise identifier first" principle used throughout this project.

    Only the manifest-origin fields are refreshed on a match; SKU, status,
    and any AI/manual work already done on that product are left untouched.
    Rows that don't match anything existing become new draft products.
    Existing products not matched by any row in the new file are left as
    they are (not deleted) — this function only ever updates or adds.

    Returns (updated_products, new_products).
    """
    import_id = manifest_import_id or uuid.uuid4().hex[:10]

    by_target_no = {p.manifest_target_no: p for p in existing_products if p.manifest_target_no}
    by_asin = {p.asin: p for p in existing_products if p.asin}
    by_ean = {p.manifest_barcode: p for p in existing_products if p.manifest_barcode}

    matched_ids = set()
    updated: List[Product] = []
    unmatched_rows: List[dict] = []

    for row in rows:
        target_no = row.get("target_no", "")
        asin = row.get("asin", "")
        ean = row.get("ean_barcode", "")

        match = None
        if target_no and target_no in by_target_no:
            match = by_target_no[target_no]
        elif asin and asin in by_asin:
            match = by_asin[asin]
        elif ean and ean in by_ean:
            match = by_ean[ean]

        if match is not None and match.id not in matched_ids:
            matched_ids.add(match.id)
            _apply_manifest_fields(match, row)
            updated.append(match)
        else:
            unmatched_rows.append(row)

    new_products = rows_to_draft_products(unmatched_rows, company_id=company_id, manifest_import_id=import_id)
    return updated, new_products
