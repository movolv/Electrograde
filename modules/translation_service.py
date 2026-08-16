"""Single orchestration point for turning a product's primary-language
content into another language — used by the auto-translate-on-generate
flow (app.py's New Item wizard), the per-product Translate dialog, and the
bulk Translate operation. No other code in this app is allowed to call a
translation provider directly; everything goes through `translate_text()`/
`translate_list()`/`translate_product()` here, so manual-translation
protection (below) is enforced exactly once, everywhere.

Only ever touches eleven fields: title, description, condition_description,
defects, box_contents, missing_components, spec_summary,
functional_checklist, product_condition_reasoning, match_notes, color (see
modules/product_translation_store.py) — free-form descriptive text/lists,
plus `color` as a deliberate, confirmed exception (a descriptive word, not
an identifier — translated everywhere including BaseLinker export, unlike
every other parameter value). Brand, model, SKU, EAN, barcode, power, and
every other technical specification value are never passed to a provider
— this module has no code path that could reach them; they simply aren't
inputs to this function.
Parameter *labels* (Brand/Model/Condition/Color/Power/EAN) are translated
separately, statically, via integrations/field_labels_i18n.py — never
through a provider call.
"""
from typing import List, Optional

from integrations import manager as integration_manager
from modules import product_translation_store


def translate_text(text: str, source_language: str, target_language: str, provider_type: str, company_id: str) -> str:
    """Pure passthrough to the connected provider — no DB access, no
    caching. app.py's New Item wizard uses this (via its own
    session-state-memoizing helper) to show translated text live as each
    field is generated, without writing a product_translations row on
    every rerun; translate_product() below uses it too, for the one real
    DB-writing translate call."""
    if not text:
        return text
    connector = integration_manager.get(company_id, provider_type)
    return connector.translate(text, source_language, target_language)


def translate_list(items: List[str], source_language: str, target_language: str, provider_type: str, company_id: str) -> List[str]:
    """Translates each item individually — never joined-then-split, safer
    against a provider merging/reordering lines."""
    return [translate_text(item, source_language, target_language, provider_type, company_id) for item in (items or [])]


def translate_product(
    product,
    target_language: str,
    provider_type: str,
    company_id: str,
    translated_by: str,
    force: bool = False,
) -> Optional[product_translation_store.ProductTranslation]:
    """Translates `product`'s primary-language content into
    `target_language` via the company's connected `provider_type`
    ("deepl" | "openai") and saves it as a `product_translations` row.

    Returns the saved row, or `None` if nothing was done — either because
    `target_language` is already the product's primary language (nothing
    to translate), or because a translation already exists there with
    `translated_by == "manual"` and `force` is not `True` (never silently
    overwrite a human edit — see the bulk Translate dialog's default
    "skip manual" behavior).

    Raises whatever the underlying connector raises (network error, rate
    limit, missing/invalid credentials) — callers must catch and degrade
    gracefully (e.g. the generation flow falls back to showing the primary
    language with a "connect a translation provider" hint rather than
    blocking product creation).
    """
    if target_language == product.primary_language:
        return product_translation_store.get_translation(product.id, target_language)

    existing = product_translation_store.get_translation(product.id, target_language)
    if existing is not None and existing.translated_by == "manual" and not force:
        return None

    source_language = product.primary_language

    def _text(value: str) -> str:
        return translate_text(value, source_language, target_language, provider_type, company_id)

    def _list(values: List[str]) -> List[str]:
        return translate_list(values, source_language, target_language, provider_type, company_id)

    translation = product_translation_store.ProductTranslation(
        product_id=product.id,
        company_id=company_id,
        language=target_language,
        title=_text(product.name),
        description=_text(product.product_description),
        condition_description=_text(product.condition_description),
        defects=_list(product.defects),
        box_contents=_list(product.box_contents),
        missing_components=_list(product.missing_components),
        spec_summary=_text(product.spec_summary),
        functional_checklist=_list(product.functional_checklist),
        product_condition_reasoning=_text(product.product_condition_reasoning),
        match_notes=_text(product.match_notes),
        color=_text(product.color),
        translated_by=translated_by,
    )
    product_translation_store.upsert_translation(translation)
    return translation
