"""Per-company, per-integration field-mapping configuration — how an
ElectroGrader field/value (e.g. Product Condition "B") translates into a specific
external platform's technical field/value (e.g. condition_id="used_good"),
plus a human-readable label for display ("Used - Good").

Phase 1: exactly one ACTIVE mapping per (company_id, integration_type),
identified by `profile_name="default"` — but profile_name is a real column
with a unique index on all three, so a future multi-profile UI (several
named, switchable mapping sets) is a pure addition, not a schema rewrite.

Pure storage — nothing reads `rules` yet to actually transform outgoing
payloads; integrations/marketplaces/baselinker/mapper.py is untouched in
this phase. Shares the same SQLite file as modules/inventory_store.py.
"""
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules.inventory_store import DB_PATH

DEFAULT_PROFILE = "default"


@dataclass
class FieldMappingRule:
    source_field: str = ""  # ElectroGrader field key, e.g. "product_condition"
    source_value: str = ""  # optional specific value, e.g. "B" — "" means "the whole field"
    target_field: str = ""  # external platform's technical field key, e.g. "condition_id"
    target_value: str = ""  # external platform's technical value, e.g. "used_good"
    target_label: str = ""  # human-readable, e.g. "Used - Good"


@dataclass
class FieldMapping:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    integration_type: str = ""
    profile_name: str = DEFAULT_PROFILE
    rules: List[FieldMappingRule] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_field_mappings (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            integration_type TEXT NOT NULL,
            profile_name TEXT NOT NULL DEFAULT 'default',
            rules TEXT NOT NULL DEFAULT '[]',
            created_at REAL,
            updated_at REAL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_mappings_unique "
        "ON integration_field_mappings(company_id, integration_type, profile_name)"
    )

    # Migrate older DBs: "grade" field was renamed to "product_condition"
    # (integrations/field_registry.py). Unlike the other sync tables, a
    # saved mapping rule lives inside the `rules` JSON blob, not a plain
    # column — a SQL UPDATE can't reach into it, so this rewrites each
    # affected row's JSON in Python. Idempotent: after the first run, no
    # row has a "grade" source_field left to find.
    for row_id, raw_rules in conn.execute(
        "SELECT id, rules FROM integration_field_mappings WHERE rules LIKE '%\"grade\"%'"
    ).fetchall():
        rules = json.loads(raw_rules) if raw_rules else []
        changed = False
        for rule in rules:
            if rule.get("source_field") == "grade":
                rule["source_field"] = "product_condition"
                changed = True
        if changed:
            with conn:
                conn.execute(
                    "UPDATE integration_field_mappings SET rules = ? WHERE id = ?",
                    (json.dumps(rules), row_id),
                )
    return conn


_SELECT_COLS = "id, company_id, integration_type, profile_name, rules, created_at, updated_at"


def _row_to_mapping(r: tuple) -> FieldMapping:
    raw_rules = json.loads(r[4]) if r[4] else []
    return FieldMapping(
        id=r[0], company_id=r[1], integration_type=r[2], profile_name=r[3] or DEFAULT_PROFILE,
        rules=[FieldMappingRule(**rr) for rr in raw_rules],
        created_at=r[5] or 0.0, updated_at=r[6] or 0.0,
    )


def get_mapping(
    company_id: str, integration_type: str, profile_name: str = DEFAULT_PROFILE,
) -> Optional[FieldMapping]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM integration_field_mappings "
        "WHERE company_id = ? AND integration_type = ? AND profile_name = ?",
        (company_id, integration_type, profile_name),
    ).fetchone()
    conn.close()
    return _row_to_mapping(row) if row else None


def upsert_mapping(mapping: FieldMapping) -> FieldMapping:
    assert mapping.company_id, "FieldMapping.company_id must be set before saving."
    assert mapping.integration_type, "FieldMapping.integration_type must be set before saving."
    mapping.profile_name = mapping.profile_name or DEFAULT_PROFILE

    now = time.time()
    conn = _connect()
    existing = conn.execute(
        "SELECT id, created_at FROM integration_field_mappings "
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
            """INSERT OR REPLACE INTO integration_field_mappings
               (id, company_id, integration_type, profile_name, rules, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                mapping.id, mapping.company_id, mapping.integration_type, mapping.profile_name,
                json.dumps([vars(r) for r in mapping.rules]), mapping.created_at, mapping.updated_at,
            ),
        )
    conn.close()
    return mapping
