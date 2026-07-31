"""Streamlit custom component for the CSV/Excel Export page's data table —
an AG Grid instance parallel to modules/review_table_component.py and
modules/inventory_table_component.py (same hand-rolled wire protocol, same
look/feel), kept as its own separate component rather than a
generalized/shared one for the same reason as the Inventory one: a config-
driven single component covering three different column sets and
interaction quirks risked destabilizing the already-hardened Review &
Export grid for little payoff.

Interaction model is the closest of the three to Review & Export's: a
checkbox column with a this-page/all-pages "select all" menu (this page's
whole job is picking a subset of products to export), and a double-click
(or Enter) opens a read-only detail view of a single product — unlike
Review & Export's card, that view has no Save button; editing already lives
on the Review & Export page.
"""
from pathlib import Path

import streamlit.components.v1 as components

_FRONTEND_DIR = Path(__file__).resolve().parent / "export_table_frontend"

_component = components.declare_component("export_table", path=str(_FRONTEND_DIR))


def export_table(rows: list, clear_seq: int = 0, key: str = "export_table") -> dict | None:
    """Returns {"selected_ids": [...], "open_id": <id> | None} whenever the
    selection changes or a row is opened. `clear_seq`: bump this int to tell
    the grid to deselect all rows (e.g. after a "Clear Selection" button)."""
    return _component(rows=rows, clear_seq=clear_seq, key=key, default=None)
