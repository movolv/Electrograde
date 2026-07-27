"""Keeps a full mirror of the inventory in an .xlsx file, auto-regenerated
on every save/delete — in addition to (not instead of) the SQLite DB, which
stays the source of truth. This file contains ALL product fields EXCEPT
ASIN — the ASIN fields (asin, asin_status, asin_source, asin_candidates)
are intentionally excluded from every human-facing Excel file (this one and
the Baselinker export in modules/export.py) since ASIN is optional/
best-effort and often ambiguous (see modules/identifier_lookup.py). They
still live in the SQLite DB and stay fully available there for
search_products() and future API/feature use — just not in a spreadsheet
anyone opens.
"""
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd

from modules.models import Product

EXCEL_PATH = Path(__file__).resolve().parent.parent / "data" / "inventory.xlsx"

COLUMNS = [
    "id",
    "company_id",
    "status",
    "sku",
    "manifest_import_id",
    "manifest_target_no",
    "manifest_subcategory",
    "manifest_barcode",
    "manifest_item_description",
    "manifest_qty",
    "manifest_weight_kg",
    "scanned_barcode",
    "model_number",
    "ean",
    "ean_status",
    "ean_source",
    "brand",
    "model",
    "name",
    "category",
    "condition_type",
    "grade",
    "grade_confidence",
    "grade_reasoning",
    "product_match",
    "match_confidence",
    "match_notes",
    "price",
    "price_reasoning",
    "product_description",
    "condition_description",
    "spec_summary",
    "box_contents",
    "missing_components",
    "defects",
    "functional_checklist",
    "location",
    "functional_test_result",
    "box_length_cm",
    "box_width_cm",
    "box_height_cm",
    "image_paths",
    "baselinker_product_id",
    "baselinker_synced_at",
    "created_at",
]

_LIST_FIELDS = {"box_contents", "missing_components", "defects", "functional_checklist", "image_paths"}

# Columns that must always render as literal text in Excel — never a number
# — so barcodes/IDs never get mangled into scientific notation or lose
# leading zeros if someone opens/edits the file.
_TEXT_FORMAT_COLUMNS = {"ean", "manifest_barcode", "scanned_barcode", "manifest_target_no", "asin", "baselinker_product_id"}


def _row(p: Product) -> dict:
    d = p.to_dict()
    row = {}
    for col in COLUMNS:
        val = d.get(col, "")
        if col in _LIST_FIELDS:
            val = "; ".join(val or [])
        elif col in ("created_at", "baselinker_synced_at"):
            val = datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S") if val else ""
        row[col] = val
    return row


def sync_excel(products: List[Product]) -> Optional[str]:
    """Rewrite data/inventory.xlsx from the current full product list.

    Returns None on success, or a human-readable warning string if the file
    could not be written (most commonly: it's currently open in Excel). The
    SQLite DB is the source of truth and is never affected by this failing —
    the next successful save/delete will resync the file automatically.
    """
    EXCEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([_row(p) for p in products], columns=COLUMNS)

    try:
        with pd.ExcelWriter(EXCEL_PATH, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Inventory")
            ws = writer.sheets["Inventory"]
            for col_cells in ws.columns:
                length = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
                ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 60)
                header = col_cells[0].value
                if header in _TEXT_FORMAT_COLUMNS:
                    for cell in col_cells[1:]:
                        cell.number_format = "@"
    except PermissionError:
        return (
            f"Could not update {EXCEL_PATH.name} — it looks like the file is "
            "currently open in Excel (or another program). Close it and the "
            "next save/delete will resync it automatically. Your item was "
            "still saved to the database."
        )
    return None
