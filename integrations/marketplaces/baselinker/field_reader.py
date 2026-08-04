"""Translates one BaseLinker product's raw getInventoryProductsData entry
into ElectroGrader's field vocabulary — pure function, no network/DB access.

Scope is deliberately narrow: BaseLinker is the operational/channel system,
never the product-intelligence one. This function ONLY ever returns
quantity/price/status — never name/description/product_condition/defects/images/etc.,
even if BaseLinker's raw response happened to contain something under
those keys. That's not a filter applied after the fact; those keys are
simply never read here, so there's no path by which a title/description
value from BaseLinker could reach the rest of the sync pipeline.
"""
from typing import Optional


def _clean(value):
    """Empty remote value != valid update, unless it's a meaningful zero.
    Returns None for anything that should be treated as "not provided"
    (missing key, None, empty/whitespace-only string) so the caller can
    omit it entirely — never passed through as an instruction to blank out
    an existing ElectroGrader value. Numeric 0 (e.g. quantity=0, a real
    "sold out" signal) is NOT treated as empty."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def read_remote_state(raw_product: dict, config: dict) -> dict:
    """raw_product: one entry from getInventoryProductsData's "products"
    dict (already looked up by product_id). config: the connector's
    _config() dict (needs warehouse_id/price_group_id to know which
    sub-value to read). Returns only the keys that had a real, non-empty
    value — e.g. {"quantity": 5, "price": 110.0, "status": "active"}."""
    result: dict = {}

    warehouse_id = config.get("warehouse_id")
    if warehouse_id is not None:
        stock = raw_product.get("stock") or {}
        quantity = _clean(stock.get(str(warehouse_id)))
        if quantity is not None:
            result["quantity"] = int(quantity)
            # "status" has no dedicated BaseLinker inventory field — derived
            # from quantity as a documented simplification (0 -> sold,
            # >0 -> active), not read from any raw_product key directly.
            result["status"] = "active" if int(quantity) > 0 else "sold"

    price_group_id = config.get("price_group_id")
    if price_group_id is not None:
        prices = raw_product.get("prices") or {}
        price = _clean(prices.get(str(price_group_id)))
        if price is not None:
            result["price"] = float(price)

    return result
