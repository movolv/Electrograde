"""BaseLinker webhook payload parsing — prepared, NOT activated.

A real webhook receiver needs a stable, public HTTPS URL registered in
BaseLinker's own dashboard. Today's deployment runs behind an ephemeral
cloudflared quick-tunnel URL that changes on every restart, and Streamlit
has no practical way to expose an arbitrary custom POST route alongside its
own app — so a real webhook endpoint is not this pass's work. The scheduler
(integrations/scheduler.py's poll_two_way_sync_once()) covers Pull via
polling instead, matching how the spec itself describes Pull as an interval
job, not a push callback.

This module holds only the pure, testable parsing function a future real
HTTP endpoint would call once that infrastructure exists.
"""


def parse_webhook_payload(raw: dict) -> dict:
    """Normalizes BaseLinker's webhook JSON shape into
    {"product_id": ..., "field": ..., "value": ...}. Not called from
    anywhere in the live app yet."""
    return {
        "product_id": str(raw.get("product_id", "")),
        "field": raw.get("field", ""),
        "value": raw.get("value"),
    }
