"""Streamlit custom component for the Inventory page's data table — an AG
Grid instance parallel to modules/review_table_component.py (same
hand-rolled wire protocol, same look/feel), but deliberately its own,
separate component rather than a generalized/shared one: Inventory's
interaction model (single click opens the detail panel below, no bulk
selection) is different enough from Review & Export's (checkbox multi-select
for bulk BaseLinker export) that forcing one config-driven component to
cover both risked destabilizing the already-hardened Review & Export grid
for comparatively little payoff.
"""
from pathlib import Path

import streamlit.components.v1 as components

_FRONTEND_DIR = Path(__file__).resolve().parent / "inventory_table_frontend"

_component = components.declare_component("inventory_table", path=str(_FRONTEND_DIR))


def inventory_table(rows: list, selected_id: str = "", key: str = "inventory_table") -> dict | None:
    """`rows`: list of dicts with id/sku/title/triage/product_condition/location/price/
    baselinker. `selected_id`: id of the row currently shown in the detail
    panel below, if any — highlighted in the grid so it's clear which row
    that panel belongs to. Returns {"open_id": <id>} when a row is clicked
    or Enter is pressed on a focused row, else None.
    """
    return _component(rows=rows, selected_id=selected_id, key=key, default=None)
