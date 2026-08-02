"""Sync Ownership configuration — which system (ElectroGrader / the
connected marketplace / a human, "manual") is the master source of truth
for a given field, once real two-way sync exists. Purely configuration and
storage today: nothing reads this to make a real sync decision yet (see
sync/engine.py's get_export_field_owners() — a read-only helper a future
real bidirectional engine would consult, not called from any active export
path). integrations/marketplaces/baselinker/{mapper,client}.py are
completely untouched by this module.

Two tables:
  - sync_field_config: the actual per-(company, connector, field)
    ownership choice. get_field_owner() is the one guarantee this whole
    feature rests on — a saved row ALWAYS wins over
    field_registry.SYNC_OWNERSHIP_FIELDS' default_owner; the default is
    used only when no row exists at all yet.
  - sync_conflicts: schema-only prep for a future "the two sides disagree"
    record — nothing populates this automatically in this phase.

Shares the same SQLite file as modules/inventory_store.py.
"""
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from integrations.field_registry import SYNC_OWNERSHIP_FIELDS
from modules.inventory_store import DB_PATH


@dataclass
class SyncFieldConfig:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    connector_name: str = ""
    field_name: str = ""
    source_system: str = ""  # "electrograder" | "manual" | connector_name
    sync_enabled: bool = True
    # Which value wins when the NON-owning side changes a field, once real
    # two-way sync exists: "electrograder" | connector_name | "manual_review"
    # (the last one meaning "ask a human" rather than picking a side
    # automatically — the only value that is never a "mismatch" with
    # source_system, see is_conflicting_configuration() below).
    conflict_policy: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class SyncConflict:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    product_id: str = ""
    field_name: str = ""
    old_value: str = ""
    electrograder_value: str = ""
    baselinker_value: str = ""
    resolution: str = ""
    timestamp: float = 0.0


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_field_config (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            connector_name TEXT NOT NULL,
            field_name TEXT NOT NULL,
            source_system TEXT NOT NULL,
            sync_enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL,
            updated_at REAL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_field_config_unique "
        "ON sync_field_config(company_id, connector_name, field_name)"
    )

    # Migrate older DBs (initial Sync Ownership shipped this table without
    # conflict_policy).
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(sync_field_config)")}
    if "conflict_policy" not in existing_cols:
        conn.execute("ALTER TABLE sync_field_config ADD COLUMN conflict_policy TEXT NOT NULL DEFAULT ''")
        conn.commit()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT NOT NULL DEFAULT '',
            electrograder_value TEXT NOT NULL DEFAULT '',
            baselinker_value TEXT NOT NULL DEFAULT '',
            resolution TEXT NOT NULL DEFAULT '',
            timestamp REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_conflicts_company ON sync_conflicts(company_id, product_id)"
    )
    return conn


_FIELD_CONFIG_COLS = (
    "id, company_id, connector_name, field_name, source_system, sync_enabled, "
    "conflict_policy, created_at, updated_at"
)


def _row_to_field_config(r: tuple) -> SyncFieldConfig:
    return SyncFieldConfig(
        id=r[0], company_id=r[1], connector_name=r[2], field_name=r[3], source_system=r[4],
        sync_enabled=bool(r[5]), conflict_policy=r[6] or "", created_at=r[7] or 0.0, updated_at=r[8] or 0.0,
    )


def _resolve_default_owner(field_name: str, connector_name: str) -> str:
    default = SYNC_OWNERSHIP_FIELDS.get(field_name, {}).get("default_owner", "electrograder")
    return connector_name if default == "<connector>" else default


# Sync direction is deliberately NOT a stored column — every flow in the
# spec is 1:1 determined by ownership (owner=electrograder -> flows TO the
# connector; owner=connector -> flows FROM it; owner=manual -> nothing
# automatic), so storing it separately would let it drift out of sync with
# source_system. resolve_flow() is the single computation, always derived.
FLOW_EXPORT_ONLY = "export"  # ElectroGrader -> connector
FLOW_IMPORT_ONLY = "import"  # connector -> ElectroGrader
FLOW_MANUAL_ONLY = "manual"  # nothing changes automatically


def resolve_flow(source_system: str, connector_name: str) -> str:
    if source_system == "electrograder":
        return FLOW_EXPORT_ONLY
    if source_system == "manual":
        return FLOW_MANUAL_ONLY
    return FLOW_IMPORT_ONLY  # source_system == connector_name


CONFLICT_MANUAL_REVIEW = "manual_review"


def _conflict_policy_for_owner(source_system: str) -> str:
    """Maps a concrete owner value to its sensible default conflict policy:
    the owner's own side wins by default, except "manual" (nobody
    auto-owns it, so any conflict needs a human) which maps to
    CONFLICT_MANUAL_REVIEW rather than nonsensically defaulting to a side
    that doesn't own the field."""
    return CONFLICT_MANUAL_REVIEW if source_system == "manual" else source_system


def _resolve_default_conflict_policy(field_name: str, connector_name: str) -> str:
    """The default conflict policy for a field that has NO saved
    sync_field_config row at all yet — derived from the field's registry
    default_owner (SYNC_OWNERSHIP_FIELDS), via _conflict_policy_for_owner().
    Distinct from upsert_field_config's blank-conflict_policy fallback,
    which must instead default from the source_system actually being
    saved right now, not the field's registry default (they can differ,
    e.g. saving brand -> manual while brand's registry default is
    electrograder)."""
    return _conflict_policy_for_owner(_resolve_default_owner(field_name, connector_name))


def is_conflicting_configuration(owner: str, conflict_policy: str) -> bool:
    """True when conflict_policy names a concrete side that DISAGREES with
    who owns the field — e.g. owner=electrograder but conflict_policy lets
    the connector's changes win outright. Not forbidden (the UI only warns,
    never blocks), but worth surfacing since it's easy to configure by
    accident. conflict_policy == CONFLICT_MANUAL_REVIEW is never a
    mismatch, for any owner — deferring to a human is always a safe,
    non-contradictory choice."""
    if conflict_policy == CONFLICT_MANUAL_REVIEW:
        return False
    return conflict_policy != owner


def get_field_owner(company_id: str, connector_name: str, field_name: str) -> str:
    """The one guarantee this feature rests on: a saved sync_field_config
    row ALWAYS wins over SYNC_OWNERSHIP_FIELDS' default_owner. The default
    is only ever used when no row exists yet for this
    (company, connector, field) — never as an override of a real choice."""
    conn = _connect()
    row = conn.execute(
        "SELECT source_system FROM sync_field_config WHERE company_id = ? AND connector_name = ? AND field_name = ?",
        (company_id, connector_name, field_name),
    ).fetchone()
    conn.close()
    if row is not None:
        return row[0]
    return _resolve_default_owner(field_name, connector_name)


def get_conflict_policy(company_id: str, connector_name: str, field_name: str) -> str:
    """Same guarantee as get_field_owner(): a saved sync_field_config row's
    conflict_policy ALWAYS wins over the computed default — the default is
    only used when no row exists yet. The one difference: a row saved
    BEFORE this column existed has conflict_policy == "" (the ALTER TABLE
    migration default) rather than no row at all, and that blank also
    falls through to the default — same backward-compat shape as
    fields_send_configured elsewhere in this codebase, since an old row
    never made a real choice for this column."""
    conn = _connect()
    row = conn.execute(
        "SELECT conflict_policy FROM sync_field_config WHERE company_id = ? AND connector_name = ? AND field_name = ?",
        (company_id, connector_name, field_name),
    ).fetchone()
    conn.close()
    if row is not None and row[0]:
        return row[0]
    return _resolve_default_conflict_policy(field_name, connector_name)


def list_field_config(company_id: str, connector_name: str) -> Dict[str, SyncFieldConfig]:
    """Every field in SYNC_OWNERSHIP_FIELDS, filled in from saved rows
    where they exist and from safe defaults where they don't — the shape
    the Sync Ownership tab's table renders directly."""
    conn = _connect()
    rows = conn.execute(
        f"SELECT {_FIELD_CONFIG_COLS} FROM sync_field_config WHERE company_id = ? AND connector_name = ?",
        (company_id, connector_name),
    ).fetchall()
    conn.close()
    saved = {r[3]: _row_to_field_config(r) for r in rows}  # field_name -> config

    result: Dict[str, SyncFieldConfig] = {}
    for field_name in SYNC_OWNERSHIP_FIELDS:
        if field_name in saved:
            cfg = saved[field_name]
            if not cfg.conflict_policy:  # pre-migration row — same fallback as get_conflict_policy()
                cfg.conflict_policy = _resolve_default_conflict_policy(field_name, connector_name)
            result[field_name] = cfg
        else:
            result[field_name] = SyncFieldConfig(
                company_id=company_id, connector_name=connector_name, field_name=field_name,
                source_system=_resolve_default_owner(field_name, connector_name), sync_enabled=True,
                conflict_policy=_resolve_default_conflict_policy(field_name, connector_name),
            )
    return result


def upsert_field_config(
    company_id: str, connector_name: str, field_name: str, source_system: str, sync_enabled: bool = True,
    conflict_policy: str = "",
) -> None:
    """conflict_policy defaults to "" (meaning "resolve a sensible default
    for the source_system being saved") purely so existing call sites
    (scripts/verify_sync_ownership.py, already shipped) that don't pass it
    keep working unchanged — a real save from the UI always passes an
    explicit choice."""
    assert company_id, "company_id is required."
    assert connector_name, "connector_name is required."
    assert field_name, "field_name is required."

    if not conflict_policy:
        conflict_policy = _conflict_policy_for_owner(source_system)

    now = time.time()
    conn = _connect()
    existing = conn.execute(
        "SELECT id, created_at FROM sync_field_config WHERE company_id = ? AND connector_name = ? AND field_name = ?",
        (company_id, connector_name, field_name),
    ).fetchone()
    record_id = existing[0] if existing else uuid.uuid4().hex[:12]
    created_at = existing[1] if existing else now

    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO sync_field_config
               (id, company_id, connector_name, field_name, source_system, sync_enabled, conflict_policy,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record_id, company_id, connector_name, field_name, source_system, int(sync_enabled),
                conflict_policy, created_at, now,
            ),
        )
    conn.close()


def record_conflict(
    company_id: str, product_id: str, field_name: str, old_value: str = "",
    electrograder_value: str = "", baselinker_value: str = "", resolution: str = "",
) -> None:
    """Schema-only prep (no automatic conflict detection calls this yet) —
    a future real bidirectional sync would call this when both sides have
    a differing value for a field."""
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO sync_conflicts
               (id, company_id, product_id, field_name, old_value, electrograder_value, baselinker_value,
                resolution, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex[:12], company_id, product_id, field_name, old_value,
                electrograder_value, baselinker_value, resolution, time.time(),
            ),
        )
    conn.close()


def list_conflicts(company_id: str, product_id: str = "") -> List[SyncConflict]:
    conn = _connect()
    if product_id:
        rows = conn.execute(
            "SELECT id, company_id, product_id, field_name, old_value, electrograder_value, baselinker_value, "
            "resolution, timestamp FROM sync_conflicts WHERE company_id = ? AND product_id = ? "
            "ORDER BY timestamp DESC",
            (company_id, product_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, company_id, product_id, field_name, old_value, electrograder_value, baselinker_value, "
            "resolution, timestamp FROM sync_conflicts WHERE company_id = ? ORDER BY timestamp DESC",
            (company_id,),
        ).fetchall()
    conn.close()
    return [
        SyncConflict(
            id=r[0], company_id=r[1], product_id=r[2], field_name=r[3], old_value=r[4] or "",
            electrograder_value=r[5] or "", baselinker_value=r[6] or "", resolution=r[7] or "",
            timestamp=r[8] or 0.0,
        )
        for r in rows
    ]
