"""Custom Streamlit component wrapping AG Grid Community for the Review &
Export product list — replaces st.dataframe there because Streamlit's
native grid only reports clicks on its checkbox column (verified directly:
clicking at seven different x-positions across a row, only the checkbox
column produced any event Python could see), which rules out
double-click-anywhere, keyboard nav, sort/filter UI, and column reorder.

No Node/npm/React build step: the frontend (review_table_frontend/index.html)
is a single self-contained file that loads AG Grid from a CDN and speaks
Streamlit's component wire protocol (window.postMessage) directly.
"""
from pathlib import Path

import streamlit.components.v1 as components

_FRONTEND_DIR = Path(__file__).resolve().parent / "review_table_frontend"

_component = components.declare_component("review_table", path=str(_FRONTEND_DIR))


def review_table(
    rows: list, focus_id: str = "", clear_seq: int = 0, key: str = "review_table"
) -> dict | None:
    """rows: list of dicts, each with id/photo_url/sku/name/grade/status/date.
    focus_id: product id to scroll to/highlight on this render (e.g. just
    saved in the card), or "" for none.
    clear_seq: bump this to make the grid deselect all rows (e.g. after
    "Clear Selection" or a successful export) without remounting it.
    Returns None until the frontend first reports back, then a dict:
    {"selected_ids": [...], "open_id": str | None}."""
    return _component(rows=rows, focus_id=focus_id, clear_seq=clear_seq, key=key, default=None)
