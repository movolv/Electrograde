"""Per-language labels for the small, fixed set of parameter/feature NAMES
BaseLinker's "Information -> Parameters" section receives (see
integrations/marketplaces/baselinker/mapper.py's `features` dict) — e.g.
English "Power" becomes Latvian "Jauda".

This is a static, framework-agnostic dict (same shape as modules/i18n.py,
but with zero Streamlit/session coupling — mapper.py is deliberately
"pure, no DB/session access", so it can import this directly). It is NOT
run through a translation provider: these six keys are a fixed vocabulary,
identical across every product, so calling DeepL/OpenAI per product for
them would be wasteful and could drift inconsistently between products.
Only the six PARAMETER NAMES below are translated this way — the VALUES
next to them (product.brand, product.power, etc.) are never touched by
this module or by any translation provider; they're read straight off the
Product exactly as authored.
"""

DEFAULT_LANGUAGE = "en"

LABELS = {
    "brand": {"en": "Brand", "de": "Marke", "lv": "Zīmols"},
    "model": {"en": "Model", "de": "Modell", "lv": "Modelis"},
    # "Product Condition"/"Preces stāvoklis", not a bare "Condition"/
    # "Stāvoklis": this is the QUALITY GRADE (A/B/C/D), and the shorter
    # wording read as the separate New/Used field on the marketplace side.
    # German is deliberately left as-is — only these two were requested.
    # Renaming a parameter does not rename it on listings already
    # exported; those keep the old name on BaseLinker until re-exported.
    "product_condition": {"en": "Product Condition", "de": "Zustand", "lv": "Preces stāvoklis"},
    "color": {"en": "Color", "de": "Farbe", "lv": "Krāsa"},
    "power": {"en": "Power", "de": "Leistung", "lv": "Jauda"},
    "barcode": {"en": "EAN", "de": "EAN", "lv": "EAN"},
    "condition_scratches_details": {
        "en": "Condition & Scratches Details",
        "de": "Zustand & Kratzer-Details",
        "lv": "Stāvoklis un skrāpējumu detaļas",
    },
}


def label(field_key: str, language: str) -> str:
    entry = LABELS.get(field_key, {})
    return entry.get(language) or entry.get(DEFAULT_LANGUAGE) or field_key
