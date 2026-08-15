"""Pure translation of one raw BaseLinker getOrders() order dict into
modules.models.Order — no network calls, no DB lookups, so this stays fully
unit-testable on its own (same rationale as mapper.py's build_payload()).

Field mapping confirmed against BaseLinker's own getOrders API docs
(https://api.baselinker.com/index.php?method=getOrders) and
getOrderStatusList (https://api.baselinker.com/index.php?method=getOrderStatusList).
"""
from modules.models import Order, OrderAddress, OrderItem


def _address(raw: dict, prefix: str) -> OrderAddress:
    return OrderAddress(
        full_name=raw.get(f"{prefix}_fullname", ""),
        company=raw.get(f"{prefix}_company", ""),
        address=raw.get(f"{prefix}_address", ""),
        city=raw.get(f"{prefix}_city", ""),
        postcode=raw.get(f"{prefix}_postcode", ""),
        state=raw.get(f"{prefix}_state", ""),
        country=raw.get(f"{prefix}_country", ""),
        country_code=raw.get(f"{prefix}_country_code", ""),
    )


def _items(raw_products: list) -> list:
    items = []
    for p in raw_products or []:
        try:
            quantity = int(p.get("quantity", 1) or 1)
        except (TypeError, ValueError):
            quantity = 1
        try:
            price = float(p.get("price_brutto", 0) or 0)
        except (TypeError, ValueError):
            price = 0.0
        items.append(OrderItem(name=p.get("name", ""), sku=p.get("sku", ""), ean=p.get("ean", ""),
                                quantity=quantity, price=price))
    return items


def _items_summary(items: list, limit: int = 3) -> str:
    parts = [f"{i.quantity}x {i.name}" for i in items[:limit] if i.name]
    summary = ", ".join(parts)
    if len(items) > limit:
        summary += f", +{len(items) - limit} more"
    return summary


def _price_total(raw: dict, items: list) -> float:
    try:
        delivery_price = float(raw.get("delivery_price", 0) or 0)
    except (TypeError, ValueError):
        delivery_price = 0.0
    items_total = sum(i.price * i.quantity for i in items)
    return round(items_total + delivery_price, 2)


def searchable_skus(order: Order) -> str:
    """Space-joined item SKUs — a separate return value, not stored on the
    dataclass itself, since it's purely an order_store.py search-index
    concern (see order_store.upsert_order())."""
    return " ".join(i.sku for i in order.items if i.sku)


def map_order(raw: dict, status_labels: dict, company_id: str) -> Order:
    items = _items(raw.get("products"))
    order_status_id = int(raw.get("order_status_id", 0) or 0)

    return Order(
        company_id=company_id,
        integration_type="baselinker",
        marketplace=raw.get("order_source", ""),
        external_order_id=str(raw.get("order_id", "")),
        order_number=str(raw.get("shop_order_id") or raw.get("order_id", "")),
        order_source=raw.get("order_source_info", ""),
        customer_name=raw.get("delivery_fullname") or raw.get("invoice_fullname", ""),
        email=raw.get("email", ""),
        phone=raw.get("phone", ""),
        items=items,
        item_count=len(items),
        items_summary=_items_summary(items),
        price_total=_price_total(raw, items),
        currency=raw.get("currency", ""),
        shipping_method=raw.get("delivery_method", ""),
        delivery_address=_address(raw, "delivery"),
        invoice_address=_address(raw, "invoice"),
        order_date=float(raw.get("date_add", 0) or 0),
        status_id=order_status_id,
        status_label=status_labels.get(order_status_id, ""),
        status_updated_at=float(raw.get("date_in_status", 0) or 0),
        customer_comments=raw.get("user_comments", ""),
        created_at=float(raw.get("date_add", 0) or 0),
    )
