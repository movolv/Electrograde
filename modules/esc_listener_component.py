"""Tiny custom component: reports each Escape keypress back to Python, so
the Review & Export product card (plain Streamlit content, not inside the
grid's iframe) can close on Esc. Same hand-rolled component wire protocol
as review_table_component.py, just carrying a single timestamp value."""
from pathlib import Path

import streamlit.components.v1 as components

_FRONTEND_DIR = Path(__file__).resolve().parent / "esc_listener_frontend"

_component = components.declare_component("esc_listener", path=str(_FRONTEND_DIR))


def esc_listener(key: str = "esc_listener"):
    """Returns a new (truthy) value each time Escape is pressed, else the
    same value as before (falsy on the very first render)."""
    return _component(key=key, default=None)
