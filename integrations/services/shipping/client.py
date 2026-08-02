"""Not implemented yet — structural placeholder only.

When shipping support is built (e.g. DHL/DPD label/rate lookups), this
becomes a `ShippingServiceConnector(ServiceConnector)` (see
integrations/base.py) overriding calculate() for rate quotes and/or
process() for label generation, registered in integrations/manager.py's
CONNECTORS dict per carrier (e.g. "dhl", "dpd").
"""
