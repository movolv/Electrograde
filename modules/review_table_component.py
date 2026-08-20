"""Custom Streamlit component wrapping AG Grid Community — originally built
for the Review & Export product list (replacing st.dataframe there because
Streamlit's native grid only reports clicks on its checkbox column, verified
directly: clicking at seven different x-positions across a row, only the
checkbox column produced any event Python could see, which rules out
double-click-anywhere, keyboard nav, sort/filter UI, and column reorder),
generalized to a second caller (the Orders page) via the `columns`/
`mobile_fields`/`state_key` params below — see review_table_frontend/index.html's
own comments for how column definitions now flow from Python into the grid
instead of being hardcoded there.

No Node/npm/React build step: the frontend (review_table_frontend/index.html)
is a single self-contained file that loads AG Grid from a CDN and speaks
Streamlit's component wire protocol (window.postMessage) directly.
"""
from pathlib import Path

import streamlit.components.v1 as components

_FRONTEND_DIR = Path(__file__).resolve().parent / "review_table_frontend"

_component = components.declare_component("review_table", path=str(_FRONTEND_DIR))


def review_table(
    rows: list,
    columns: list,
    mobile_fields: list,
    state_key: str,
    focus_id: str = "",
    clear_seq: int = 0,
    mobile_labels: dict | None = None,
    key: str = "review_table",
) -> dict | None:
    """rows: list of dicts, each with (at least) the fields named in `columns`
    plus an "id".
    columns: list of {"field", "headerName", "width"?, "minWidth"?,
    "maxWidth"?, "flex"?, "type"?} dicts, in display order. `type` is one of
    "photo" | "price" | "numeric" | "text" (default) — resolved to a real
    cell renderer/formatter in JS, since a Python function obviously can't
    cross the wire. See modules/review_table_frontend/index.html's
    CELL_TYPE_EXTRAS.
    mobile_fields: subset of `columns`' field names kept visible below the
    responsive breakpoint (~700px) — everything else collapses away.
    state_key: distinguishes this caller's saved column-width/order
    preference (browser localStorage) from any other page reusing this same
    component — e.g. "products" vs "orders". Two callers must never share
    a state_key.
    focus_id: a row id to scroll to/highlight on this render (e.g. just
    saved), or "" for none.
    clear_seq: bump this to make the grid deselect all rows (e.g. after
    "Clear Selection" or a successful export) without remounting it.
    mobile_labels: {"stock", "price", "status", "integrations"} -> already-
    translated row labels for the stacked mobile card (only used by callers
    whose columns include a "product_info" cell — see index.html's
    buildResponsiveColumnDefs); passing translated text in keeps every
    user-visible string in modules/i18n.py rather than hardcoded in JS.
    Returns None until the frontend first reports back, then a dict:
    {"selected_ids": [...], "open_id": str | None,
     "integrations_id": str | None} — the last asks the caller to open its
    integrations dialog for that row."""
    return _component(
        rows=rows, column_defs=columns, mobile_fields=mobile_fields, state_key=state_key,
        focus_id=focus_id, clear_seq=clear_seq, mobile_labels=mobile_labels or {},
        key=key, default=None,
    )
