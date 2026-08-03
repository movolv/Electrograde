"""BaseLinker's implementation of the universal bulk-catalog-import data
fetch (integrations.base.MarketplaceConnector.fetch_catalog()). Pure data
retrieval only — no DB, no filesystem, no image downloading (that's
sync/catalog_import.py's + app.py's job, one layer up, the same "API client
stays a pure client" discipline as every other module in this package).

client.py imports this module (BaselinkerConnector.fetch_catalog()
delegates here), so client.py itself must be imported lazily inside each
function to avoid a circular import at load time — same convention as
sync.py.
"""
from integrations.base import ImportedProductData

_BATCH_SIZE = 100  # how many product_ids per getInventoryProductsData call


def _extract_image_urls(raw_product: dict) -> list:
    """BaseLinker's getInventoryProductsData returns "images" as a dict of
    slot -> value; on read, that value is normally a hosted URL (base64 is
    only ever used on the addInventoryProduct WRITE side, in mapper.py's
    encode_image()). Handled defensively for both shapes in case the real
    account's data differs from documentation — validated live against the
    real account during verification."""
    images = raw_product.get("images") or {}
    urls = []
    for value in images.values():
        if not value:
            continue
        if isinstance(value, str):
            urls.append(value)
    return urls


def fetch_all_products(config: dict) -> list:
    """Returns List[ImportedProductData] for every product in this
    BaseLinker inventory_id."""
    from integrations.marketplaces.baselinker import client

    all_ids = client.list_all_product_ids(config)
    results: list = []
    for start in range(0, len(all_ids), _BATCH_SIZE):
        batch_ids = all_ids[start:start + _BATCH_SIZE]
        products = client.get_product_data(batch_ids, config)
        for product_id, raw_product in products.items():
            text_fields = raw_product.get("text_fields") or {}
            prices = raw_product.get("prices") or {}
            stock = raw_product.get("stock") or {}

            price = 0.0
            if config.get("price_group_id") is not None:
                price = float(prices.get(str(config["price_group_id"])) or 0.0)

            quantity = 1
            if config.get("warehouse_id") is not None:
                quantity = int(stock.get(str(config["warehouse_id"])) or 0)

            results.append(ImportedProductData(
                external_id=str(product_id),
                sku=raw_product.get("sku") or "",
                name=text_fields.get("name") or raw_product.get("sku") or str(product_id),
                price=price,
                quantity=quantity,
                barcode=raw_product.get("ean") or "",
                image_urls=_extract_image_urls(raw_product),
            ))
    return results
