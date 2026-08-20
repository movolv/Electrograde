"""Per-company self-service custom product fields — lets a company define
its own extra product-card fields (e.g. "Warranty Period", "Supplier
Batch") without any code change, up to MAX_CUSTOM_FIELDS per company.

A definition only ever holds `key` (a stable slug, generated once and
NEVER changed even if `label` is renamed afterward — so an existing Field
Mapping rule or already-saved product values never silently break) and
`label` (the editable display name). The VALUES themselves live on each
Product, in `Product.custom_fields: Dict[str, str]` (see modules/models.py)
— never here; this store is definitions only, same "definitions are
runtime capability, values are product data" split already used
throughout this codebase.

A custom field automatically becomes a Field Mapping "Source field"
candidate the moment it's created — see
integrations/base.py's MarketplaceConnector.get_mappable_source_fields()
and integrations/manager.py's module-level counterpart, both of which
merge in this company's own custom fields (namespaced "custom:<key>", so
a company's custom key can never collide with a real Product attribute
name) on top of the registry-driven set — no other file needs to change.

Deletion is real (DELETE FROM custom_field_definitions), not a soft flip.
If products already have a value stored under a deleted field's key, that
value is NOT retroactively scrubbed — it simply stops being editable via
the (now-gone) product-card input, and a Field Mapping rule that still
references it shows as "⚠ Unknown" (the same orphaned-field handling
already built for every other kind of field) rather than silently
vanishing.
"""
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules import db

MAX_CUSTOM_FIELDS = 10


@dataclass
class CustomFieldDefinition:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    key: str = ""    # stable slug — identity, never changes after creation
    label: str = ""  # display name — freely renamable
    created_at: float = 0.0
    updated_at: float = 0.0


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_field_definitions (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            key TEXT NOT NULL,
            label TEXT NOT NULL,
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_custom_fields_unique_key "
        "ON custom_field_definitions(company_id, key)"
    )
    conn.commit()
    return conn


_SELECT_COLS = "id, company_id, key, label, created_at, updated_at"


def _row_to_field(r: tuple) -> CustomFieldDefinition:
    return CustomFieldDefinition(id=r[0], company_id=r[1], key=r[2], label=r[3], created_at=r[4] or 0.0, updated_at=r[5] or 0.0)


def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or "field"


def list_fields(company_id: str) -> List[CustomFieldDefinition]:
    conn = _connect()
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM custom_field_definitions WHERE company_id = ? ORDER BY created_at ASC",
        (company_id,),
    ).fetchall()
    conn.close()
    return [_row_to_field(r) for r in rows]


def get_field(field_id: str, company_id: str) -> Optional[CustomFieldDefinition]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM custom_field_definitions WHERE id = ? AND company_id = ?",
        (field_id, company_id),
    ).fetchone()
    conn.close()
    return _row_to_field(row) if row else None


def count_fields(company_id: str) -> int:
    conn = _connect()
    n = conn.execute(
        "SELECT COUNT(*) FROM custom_field_definitions WHERE company_id = ?", (company_id,),
    ).fetchone()[0]
    conn.close()
    return n


def create_field(company_id: str, label: str) -> CustomFieldDefinition:
    label = (label or "").strip()
    if not label:
        raise ValueError("Field name is required.")
    if count_fields(company_id) >= MAX_CUSTOM_FIELDS:
        raise ValueError(f"This company already has the maximum of {MAX_CUSTOM_FIELDS} custom fields.")

    existing_keys = {f.key for f in list_fields(company_id)}
    base_slug = _slugify(label)
    slug = base_slug
    suffix = 2
    while slug in existing_keys:
        slug = f"{base_slug}_{suffix}"
        suffix += 1

    now = time.time()
    definition = CustomFieldDefinition(company_id=company_id, key=slug, label=label, created_at=now, updated_at=now)
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO custom_field_definitions (id, company_id, key, label, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (definition.id, definition.company_id, definition.key, definition.label, definition.created_at, definition.updated_at),
        )
    conn.close()
    return definition


def rename_field(field_id: str, company_id: str, new_label: str) -> CustomFieldDefinition:
    new_label = (new_label or "").strip()
    if not new_label:
        raise ValueError("Field name is required.")
    definition = get_field(field_id, company_id)
    if definition is None:
        raise ValueError("Custom field not found.")
    definition.label = new_label
    definition.updated_at = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE custom_field_definitions SET label = ?, updated_at = ? WHERE id = ? AND company_id = ?",
            (definition.label, definition.updated_at, definition.id, definition.company_id),
        )
    conn.close()
    return definition


def delete_field(field_id: str, company_id: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM custom_field_definitions WHERE id = ? AND company_id = ?", (field_id, company_id))
    conn.close()
