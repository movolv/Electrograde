"""Per-company, per-integration CATEGORY mapping — how one ElectroGrader
Category Catalog entry (modules/category_store.py's Category.id, the
stable master identifier) maps onto one external platform's own category
id (e.g. BaseLinker's numeric category id from getInventoryCategories).

Deliberately a SEPARATE store from modules/field_mapping_store.py's
generic field-level mapping: a category mapping is always exactly one
ElectroGrader category -> one external category id (1:1, id-to-id, never
text comparison), never a source_value-gated rule — a different shape
that doesn't fit FieldMappingRule.

Pure storage — this module only persists `rules`; applying the resolved
external category id to an outgoing payload is each connector's own job
(see integrations/marketplaces/baselinker/client.py's
_resolve_category_id()). Shares the same PostgreSQL database as every
other modules/*_store.py (modules/db.py).
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules import db

DEFAULT_PROFILE = "default"


@dataclass
class CategoryMappingRule:
    electrograder_category_id: str = ""
    external_category_id: str = ""  # the external platform's own raw category id, as a string
    # Display-only cache of the external category's label at save time —
    # so the mapping table can still show something meaningful even if a
    # later live re-fetch fails or that category was since renamed/removed
    # on the external platform's side; never used to resolve the mapping
    # itself (external_category_id is the only thing that matters there).
    external_category_label: str = ""


@dataclass
class CategoryMapping:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    integration_type: str = ""
    profile_name: str = DEFAULT_PROFILE
    rules: List[CategoryMappingRule] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_category_mappings (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            integration_type TEXT NOT NULL,
            profile_name TEXT NOT NULL DEFAULT 'default',
            rules TEXT NOT NULL DEFAULT '[]',
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_category_mappings_unique "
        "ON integration_category_mappings(company_id, integration_type, profile_name)"
    )
    conn.commit()
    return conn


_SELECT_COLS = "id, company_id, integration_type, profile_name, rules, created_at, updated_at"


def _row_to_mapping(r: tuple) -> CategoryMapping:
    raw_rules = json.loads(r[4]) if r[4] else []
    return CategoryMapping(
        id=r[0], company_id=r[1], integration_type=r[2], profile_name=r[3] or DEFAULT_PROFILE,
        rules=[CategoryMappingRule(**rr) for rr in raw_rules],
        created_at=r[5] or 0.0, updated_at=r[6] or 0.0,
    )


def get_mapping(
    company_id: str, integration_type: str, profile_name: str = DEFAULT_PROFILE,
) -> Optional[CategoryMapping]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM integration_category_mappings "
        "WHERE company_id = ? AND integration_type = ? AND profile_name = ?",
        (company_id, integration_type, profile_name),
    ).fetchone()
    conn.close()
    return _row_to_mapping(row) if row else None


def upsert_mapping(mapping: CategoryMapping) -> CategoryMapping:
    assert mapping.company_id, "CategoryMapping.company_id must be set before saving."
    assert mapping.integration_type, "CategoryMapping.integration_type must be set before saving."
    mapping.profile_name = mapping.profile_name or DEFAULT_PROFILE

    now = time.time()
    conn = _connect()
    existing = conn.execute(
        "SELECT id, created_at FROM integration_category_mappings "
        "WHERE company_id = ? AND integration_type = ? AND profile_name = ?",
        (mapping.company_id, mapping.integration_type, mapping.profile_name),
    ).fetchone()
    if existing:
        mapping.id, mapping.created_at = existing[0], existing[1] or now
    else:
        mapping.created_at = now
    mapping.updated_at = now

    with conn:
        conn.execute(
            """INSERT INTO integration_category_mappings
               (id, company_id, integration_type, profile_name, rules, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   company_id = EXCLUDED.company_id, integration_type = EXCLUDED.integration_type,
                   profile_name = EXCLUDED.profile_name, rules = EXCLUDED.rules,
                   created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at""",
            (
                mapping.id, mapping.company_id, mapping.integration_type, mapping.profile_name,
                json.dumps([vars(r) for r in mapping.rules]), mapping.created_at, mapping.updated_at,
            ),
        )
    conn.close()
    return mapping


def resolve(company_id: str, integration_type: str, electrograder_category_id: str) -> Optional[str]:
    """The one lookup a connector needs at export time: this
    ElectroGrader category's mapped external_category_id, or None if
    nothing's mapped (or the product has no category_id at all) — the
    caller falls back to whatever default it already had."""
    if not electrograder_category_id:
        return None
    mapping = get_mapping(company_id, integration_type)
    if mapping is None:
        return None
    for rule in mapping.rules:
        if rule.electrograder_category_id == electrograder_category_id:
            return rule.external_category_id or None
    return None
