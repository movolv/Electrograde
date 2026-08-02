"""Not implemented yet — structural placeholder only.

When eBay support is built, this becomes an `EbayConnector(MarketplaceConnector)`
(see integrations/base.py) following the same shape as
integrations/marketplaces/baselinker/client.py: connect()/test_connection()
for OAuth token validation, create_product()/update_product()/
delete_product()/sync_inventory() against eBay's Inventory API, registered
in integrations/manager.py's CONNECTORS dict as "ebay" and flipped to
available=True in CATALOG. Until then, "eBay" appears in Settings ->
Integrations as a visible, non-clickable "Coming soon" entry.
"""
