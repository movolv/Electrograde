"""Baselinker-ready Excel/CSV export.

Produces one row per product with two separate, strictly-English
description columns as required:
  - "Product Description"
  - "Condition & Scratches Details"

Also includes "AI Confidence %" immediately after "Grade" — the AI's own
confidence (0-100) in the assigned grade, based on photo quality, defect
clarity, and how cleanly the item matches the grade criteria (see
modules/vision_grading.py for how this is computed) — plus manifest
traceability (Target #), automatic EAN discovery status (see
modules/identifier_lookup.py — "Found"/"Not Found"/"Needs Verification", so
an unverified identifier is never silently exported as if it were certain),
and the manifest-vs-photo verification result, so a mismatch is never
silently exported either.

Note: ASIN is intentionally NOT included here. It's optional/best-effort
data (often ambiguous — see modules/identifier_lookup.py, which deliberately
never auto-picks one candidate among several) kept in the internal database
for search and future use, not a required Baselinker listing field.

Note on images: Baselinker's importer expects publicly reachable image
URLs. Since photos here are captured on-device, this export stores the
local file paths/names in "Image Links" — you must upload these files
somewhere public (Baselinker media, S3, etc.) and swap in the resulting
URLs before/officially during import, or use Baselinker's own image
upload step after importing the row data.
"""
from io import BytesIO
from typing import List

import pandas as pd

from modules.models import Product

COLUMNS = [
    "SKU",
    "Name",
    "Brand",
    "Model",
    "Category",
    "Condition",
    "EAN/Barcode",
    "EAN Status",
    "Manifest Target #",
    "Grade",
    "AI Confidence %",
    "Product Match",
    "Match Confidence %",
    "Price",
    "Quantity",
    "Location",
    "Functional Test Result",
    "Box Dimensions (L x W x H cm)",
    "Image Links",
    "Product Description",
    "Condition & Scratches Details",
    "Functional Test Checklist",
    "Missing Components",
]


def products_to_dataframe(products: List[Product]) -> pd.DataFrame:
    rows = []
    for p in products:
        box_dims = (
            f"{p.box_length_cm:g} x {p.box_width_cm:g} x {p.box_height_cm:g}"
            if (p.box_length_cm or p.box_width_cm or p.box_height_cm)
            else ""
        )
        rows.append(
            {
                "SKU": p.sku or p.id,
                "Name": p.name,
                "Brand": p.brand,
                "Model": p.model,
                "Category": p.category,
                "Condition": p.condition_type,
                "EAN/Barcode": p.ean or p.manifest_barcode or p.scanned_barcode,
                "EAN Status": p.ean_status,
                "Manifest Target #": p.manifest_target_no,
                "Grade": p.grade,
                "AI Confidence %": p.grade_confidence,
                "Product Match": p.product_match,
                "Match Confidence %": p.match_confidence,
                "Price": p.price,
                "Quantity": p.quantity or 1,
                "Location": p.location,
                "Functional Test Result": p.functional_test_result,
                "Box Dimensions (L x W x H cm)": box_dims,
                "Image Links": "; ".join(p.image_paths),
                "Product Description": p.product_description,
                "Condition & Scratches Details": p.condition_description,
                "Functional Test Checklist": "; ".join(p.functional_checklist),
                "Missing Components": "; ".join(p.missing_components),
            }
        )
    return pd.DataFrame(rows, columns=COLUMNS)


_TEXT_FORMAT_COLUMNS = {"EAN/Barcode", "Manifest Target #"}


def to_excel_bytes(products: List[Product]) -> bytes:
    df = products_to_dataframe(products)
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Baselinker Import")
        ws = writer.sheets["Baselinker Import"]
        for col_cells in ws.columns:
            length = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
            ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 12), 60)
            if col_cells[0].value in _TEXT_FORMAT_COLUMNS:
                for cell in col_cells[1:]:
                    cell.number_format = "@"
    return buf.getvalue()


def to_csv_bytes(products: List[Product]) -> bytes:
    df = products_to_dataframe(products)
    return df.to_csv(index=False).encode("utf-8-sig")
