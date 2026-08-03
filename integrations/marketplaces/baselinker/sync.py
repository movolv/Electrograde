"""BaseLinker-specific Push/Pull orchestration — the glue between the raw
API client, the field_reader/field_writer translation layer, and
client.py's BaselinkerConnector methods. Still no DB access here (that's
sync/engine.py's job, one layer up); this module only knows how to talk to
BaseLinker and translate its data, not what ElectroGrader should do with
the result.
"""
from integrations.base import ConnectorActionResult
from integrations.marketplaces.baselinker import field_reader, field_writer

# client.py imports this module (its pull_state()/push_field() connector
# methods delegate here), so this module must import client.py lazily,
# inside each function, to avoid a circular import at module load time.


def pull_state(external_id: str, config: dict) -> dict:
    """Fetches and translates BaseLinker's current operational state for
    one listing. Returns {} if BaseLinker has no data for this product_id
    (e.g. deleted on their side) — an empty dict is "nothing to pull",
    never treated as "clear every field"."""
    from integrations.marketplaces.baselinker import client

    products = client.get_product_data([external_id], config)
    raw_product = products.get(str(external_id)) or products.get(external_id)
    if not raw_product:
        return {}
    return field_reader.read_remote_state(raw_product, config)


def push_field(product, field_name: str, existing_listing_id: str, config: dict) -> ConnectorActionResult:
    """Pushes exactly one field's current value to an existing BaseLinker
    listing (addInventoryProduct, scoped via fields_send to this field)."""
    from integrations.marketplaces.baselinker import client

    payload = field_writer.build_partial_payload(product, field_name, config, existing_listing_id=existing_listing_id)
    data = client._call("addInventoryProduct", payload, config["token"])
    return ConnectorActionResult(
        success=True, external_id=str(data.get("product_id", existing_listing_id)),
        message=f"Pushed {field_name}.", data={"warnings": data.get("warnings", {}) or {}},
    )
