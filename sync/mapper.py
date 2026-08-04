"""The "Common Export Model" — a marketplace-independent representation of
a Product. Nothing in this file knows BaseLinker (or any other platform)
exists; that's the whole point (Phase 3.3's "Common Export Model" layer,
sitting between ElectroGrader's own Product and any specific
MarketplaceConnector). A future connector consumes a CommonExportModel
instead of a raw Product, so ElectroGrader's data model never becomes
implicitly tied to one marketplace's field names.

Not wired into integrations/marketplaces/baselinker/mapper.py yet —
that module's build_payload() still takes a raw Product directly and stays
completely untouched (deliberate, see the Phase 3.2/3.3 plan: don't touch
the one proven, real export path this pass).
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class CommonExportModel:
    title: str = ""
    description: str = ""
    images: List[str] = field(default_factory=list)
    price: float = 0.0
    quantity: int = 1
    barcode: str = ""
    sku: str = ""
    # Structured, not prose — brand/model/category/product_condition/defects are real
    # attributes a connector maps individually (to a features dict, a
    # dedicated column, a description slot, whatever that platform needs),
    # never pre-flattened into a sentence here.
    attributes: dict = field(default_factory=dict)


def to_common_export_model(product) -> CommonExportModel:
    return CommonExportModel(
        title=product.name or product.model_number or product.sku,
        description=product.product_description or "",
        images=list(product.image_paths or []),
        price=product.price,
        quantity=product.quantity or 1,
        barcode=product.ean or product.manifest_barcode or product.scanned_barcode,
        sku=product.sku,
        attributes={
            "brand": product.brand,
            "model": product.model,
            "category": product.category,
            "product_condition": product.product_condition,
            "defects": list(product.defects or []),
        },
    )
