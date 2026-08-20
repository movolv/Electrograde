"""Per-language product content — title/description/condition_description/
defects/box_contents/missing_components, one row per (product_id, language). This is the single source of
truth for that content; `modules/inventory_store.py` hydrates a `Product`
object's `.name`/`.product_description`/`.condition_description`/`.defects`
from the row matching `product.primary_language` on every read, and writes
back here (not into the `products` JSON blob) on every save — see that
module's `_hydrate_primary_language_bulk()`/`save_product()`.

`en`/`lv`/`de`/... are peers here — nothing in this module (or anywhere
downstream) may assume `en` is present or special. It's just whatever
language `description_gen.py` happened to generate into for a given
product (recorded as that product's `primary_language`), not an
architectural rule.

`translated_by` records provenance: "ai" (the original AI-authored
content, in `primary_language`), "deepl" / "openai" (machine-translated by
that provider), "manual" (a human wrote or edited this language's text
directly). `modules/translation_service.py` uses `translated_by == "manual"`
as the signal to never silently overwrite a human edit.

Shares the same PostgreSQL database as every other modules/*_store.py
(modules/db.py, `?`-style params translated to `%s` by that module).
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from modules import db


@dataclass
class ProductTranslation:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    product_id: str = ""
    company_id: str = ""
    language: str = ""
    title: str = ""
    description: str = ""
    condition_description: str = ""
    defects: List[str] = field(default_factory=list)
    box_contents: List[str] = field(default_factory=list)
    missing_components: List[str] = field(default_factory=list)
    spec_summary: str = ""
    functional_checklist: List[str] = field(default_factory=list)
    product_condition_reasoning: str = ""
    match_notes: str = ""
    color: str = ""  # deliberate exception to "parameter values are never translated"
    # (see integrations/field_registry.py/field_labels_i18n.py for the rest of that
    # rule) — confirmed with the user: unlike brand/model/power/EAN, color is a
    # descriptive word, not an identifier, so it's translated everywhere including
    # BaseLinker export. See integrations/marketplaces/baselinker/client.py's
    # _resolve_export_content().
    # Self-service custom field VALUES (modules/custom_field_store.py holds
    # the per-company field DEFINITIONS), keyed by each definition's stable
    # `key` — one value per language, same as title/description/etc., so a
    # custom field works in every content language a company has, including
    # ones added after the field itself was created. {} for a language a
    # company never filled this in for — never an error, just empty.
    custom_fields: Dict[str, str] = field(default_factory=dict)
    translated_by: str = "manual"  # "ai" | "deepl" | "openai" | "manual"
    created_at: float = 0.0
    updated_at: float = 0.0


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_translations (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            company_id TEXT NOT NULL,
            language TEXT NOT NULL,
            title TEXT,
            description TEXT,
            condition_description TEXT,
            defects TEXT,
            translated_by TEXT,
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION,
            UNIQUE (product_id, language)
        )
        """
    )
    existing_cols = db.table_columns(conn, "product_translations")
    for col in (
        "box_contents", "missing_components", "spec_summary",
        "functional_checklist", "product_condition_reasoning", "match_notes", "color",
        "custom_fields",
    ):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE product_translations ADD COLUMN {col} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_translations_product ON product_translations(product_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_translations_company ON product_translations(company_id)")
    conn.commit()
    return conn


def _row_to_translation(row) -> ProductTranslation:
    (id_, product_id, company_id, language, title, description, condition_description,
     defects_json, translated_by, created_at, updated_at, box_contents_json, missing_components_json,
     spec_summary, functional_checklist_json, product_condition_reasoning, match_notes, color,
     custom_fields_json) = row
    return ProductTranslation(
        id=id_, product_id=product_id, company_id=company_id, language=language,
        title=title or "", description=description or "", condition_description=condition_description or "",
        defects=json.loads(defects_json) if defects_json else [],
        box_contents=json.loads(box_contents_json) if box_contents_json else [],
        missing_components=json.loads(missing_components_json) if missing_components_json else [],
        spec_summary=spec_summary or "",
        functional_checklist=json.loads(functional_checklist_json) if functional_checklist_json else [],
        product_condition_reasoning=product_condition_reasoning or "",
        match_notes=match_notes or "",
        color=color or "",
        custom_fields=json.loads(custom_fields_json) if custom_fields_json else {},
        translated_by=translated_by or "manual",
        created_at=created_at or 0.0, updated_at=updated_at or 0.0,
    )


_COLUMNS = (
    "id, product_id, company_id, language, title, description, condition_description, "
    "defects, translated_by, created_at, updated_at, box_contents, missing_components, "
    "spec_summary, functional_checklist, product_condition_reasoning, match_notes, color, "
    "custom_fields"
)


def get_translation(product_id: str, language: str) -> Optional[ProductTranslation]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM product_translations WHERE product_id = ? AND language = ?",
        (product_id, language),
    ).fetchone()
    conn.close()
    return _row_to_translation(row) if row else None


def list_translations(product_id: str) -> List[ProductTranslation]:
    conn = _connect()
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM product_translations WHERE product_id = ? ORDER BY language ASC",
        (product_id,),
    ).fetchall()
    conn.close()
    return [_row_to_translation(r) for r in rows]


def list_translations_for_products(product_ids: List[str]) -> Dict[str, Dict[str, ProductTranslation]]:
    """Bulk fetch for list/search views — one query instead of N, keyed
    {product_id: {language: ProductTranslation}}. Used by
    inventory_store._hydrate_primary_language_bulk() so listing a page (or
    an entire company's inventory) never does a per-row translation
    lookup."""
    if not product_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in product_ids)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM product_translations WHERE product_id IN ({placeholders})",
        list(product_ids),
    ).fetchall()
    conn.close()
    out: Dict[str, Dict[str, ProductTranslation]] = {}
    for row in rows:
        t = _row_to_translation(row)
        out.setdefault(t.product_id, {})[t.language] = t
    return out


def upsert_translation(t: ProductTranslation) -> None:
    assert t.product_id and t.company_id and t.language, (
        "ProductTranslation.product_id/company_id/language must be set before saving."
    )
    now = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            f"""INSERT INTO product_translations
                ({_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (product_id, language) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    condition_description = EXCLUDED.condition_description, defects = EXCLUDED.defects,
                    box_contents = EXCLUDED.box_contents, missing_components = EXCLUDED.missing_components,
                    spec_summary = EXCLUDED.spec_summary, functional_checklist = EXCLUDED.functional_checklist,
                    product_condition_reasoning = EXCLUDED.product_condition_reasoning,
                    match_notes = EXCLUDED.match_notes, color = EXCLUDED.color,
                    custom_fields = EXCLUDED.custom_fields,
                    translated_by = EXCLUDED.translated_by, updated_at = EXCLUDED.updated_at""",
            (
                t.id, t.product_id, t.company_id, t.language, t.title, t.description,
                t.condition_description, json.dumps(t.defects or []), t.translated_by,
                t.created_at or now, now, json.dumps(t.box_contents or []), json.dumps(t.missing_components or []),
                t.spec_summary, json.dumps(t.functional_checklist or []), t.product_condition_reasoning, t.match_notes,
                t.color, json.dumps(t.custom_fields or {}),
            ),
        )
    conn.close()


def upsert_translations_bulk(translations: List[ProductTranslation]) -> None:
    if not translations:
        return
    now = time.time()
    conn = _connect()
    with conn:
        conn.executemany(
            f"""INSERT INTO product_translations
                ({_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (product_id, language) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    condition_description = EXCLUDED.condition_description, defects = EXCLUDED.defects,
                    box_contents = EXCLUDED.box_contents, missing_components = EXCLUDED.missing_components,
                    spec_summary = EXCLUDED.spec_summary, functional_checklist = EXCLUDED.functional_checklist,
                    product_condition_reasoning = EXCLUDED.product_condition_reasoning,
                    match_notes = EXCLUDED.match_notes, color = EXCLUDED.color,
                    custom_fields = EXCLUDED.custom_fields,
                    translated_by = EXCLUDED.translated_by, updated_at = EXCLUDED.updated_at""",
            [
                (
                    t.id, t.product_id, t.company_id, t.language, t.title, t.description,
                    t.condition_description, json.dumps(t.defects or []), t.translated_by,
                    t.created_at or now, now, json.dumps(t.box_contents or []), json.dumps(t.missing_components or []),
                    t.spec_summary, json.dumps(t.functional_checklist or []), t.product_condition_reasoning, t.match_notes,
                    t.color, json.dumps(t.custom_fields or {}),
                )
                for t in translations
            ],
        )
    conn.close()


def delete_translation(product_id: str, company_id: str, language: str) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            "DELETE FROM product_translations WHERE product_id = ? AND company_id = ? AND language = ?",
            (product_id, company_id, language),
        )
    conn.close()


def delete_translations_for_product(product_id: str, company_id: str) -> None:
    """Called when a product itself is deleted (see inventory_store.delete_product())
    so translation rows never outlive their product. Scoped by company_id
    even though the product_id itself is already unique — defense in
    depth, matching every other delete in this codebase, since a caller
    passing the wrong company's product_id here must never be able to
    delete another tenant's translation rows."""
    conn = _connect()
    with conn:
        conn.execute(
            "DELETE FROM product_translations WHERE product_id = ? AND company_id = ?",
            (product_id, company_id),
        )
    conn.close()
