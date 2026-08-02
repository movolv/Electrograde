"""Per-company, per-integration sync configuration — Phase 1 of the
Universal Integration Configuration Framework: what gets synced, how often,
which direction, how conflicts are handled, and which automation triggers
are (nominally) enabled.

This is pure storage. Nothing in this module (or anywhere in Phase 1)
reads `fields_send`/`fields_receive`/`automation_triggers` to actually
change real export/sync behavior — integrations/marketplaces/baselinker/
{mapper,client}.py are untouched. `frequency`/`direction`/`conflict_handling`
ARE read by integrations/scheduler.py to decide *when* to enqueue a job,
but not to decide *what* that job does (the job body itself is still a
Phase 2+ concern, per integration).

One row per (company_id, integration_type) — same shape as
modules/integration_store.py's CompanyIntegration. Shares the same SQLite
file as modules/inventory_store.py.
"""
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules.inventory_store import DB_PATH

FREQUENCY_MANUAL = "manual"
FREQUENCY_EVERY_15_MIN = "every_15_min"
FREQUENCY_HOURLY = "hourly"
FREQUENCY_DAILY = "daily"

FREQUENCY_INTERVAL_SECONDS = {
    FREQUENCY_EVERY_15_MIN: 900,
    FREQUENCY_HOURLY: 3600,
    FREQUENCY_DAILY: 86400,
}

DIRECTION_PUSH = "push"  # ElectroGrader -> platform
DIRECTION_PULL = "pull"  # platform -> ElectroGrader
DIRECTION_TWO_WAY = "two_way"

CONFLICT_KEEP_LOCAL = "keep_local"
CONFLICT_KEEP_REMOTE = "keep_remote"
CONFLICT_ASK_USER = "ask_user"


@dataclass
class SyncRule:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    integration_type: str = ""
    frequency: str = FREQUENCY_MANUAL
    direction: str = DIRECTION_PUSH
    conflict_handling: str = CONFLICT_KEEP_LOCAL
    fields_send: List[str] = field(default_factory=list)
    fields_receive: List[str] = field(default_factory=list)
    # Each entry: {"trigger_event": str, "job_type": str, "enabled": bool} —
    # see integrations/scheduler.py's job_type constants. Phase 1: saved
    # only, never read to actually fire anything.
    automation_triggers: List[dict] = field(default_factory=list)
    enabled: bool = True
    last_enqueued_at: float = 0.0  # last time the scheduler enqueued a job
    # for this rule — distinct from company_integrations.last_sync_at
    # (which only moves on a *successful* sync). Keeping them separate means
    # a failing rule still waits out its full interval before retrying,
    # instead of being re-enqueued on every poll tick.
    created_at: float = 0.0
    updated_at: float = 0.0


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_sync_rules (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            integration_type TEXT NOT NULL,
            frequency TEXT NOT NULL DEFAULT 'manual',
            direction TEXT NOT NULL DEFAULT 'push',
            conflict_handling TEXT NOT NULL DEFAULT 'keep_local',
            fields_send TEXT NOT NULL DEFAULT '[]',
            fields_receive TEXT NOT NULL DEFAULT '[]',
            automation_triggers TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            last_enqueued_at REAL NOT NULL DEFAULT 0,
            created_at REAL,
            updated_at REAL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_rules_unique "
        "ON integration_sync_rules(company_id, integration_type)"
    )
    return conn


_SELECT_COLS = (
    "id, company_id, integration_type, frequency, direction, conflict_handling, "
    "fields_send, fields_receive, automation_triggers, enabled, last_enqueued_at, "
    "created_at, updated_at"
)


def _row_to_rule(r: tuple) -> SyncRule:
    return SyncRule(
        id=r[0], company_id=r[1], integration_type=r[2],
        frequency=r[3] or FREQUENCY_MANUAL, direction=r[4] or DIRECTION_PUSH,
        conflict_handling=r[5] or CONFLICT_KEEP_LOCAL,
        fields_send=json.loads(r[6]) if r[6] else [],
        fields_receive=json.loads(r[7]) if r[7] else [],
        automation_triggers=json.loads(r[8]) if r[8] else [],
        enabled=bool(r[9]), last_enqueued_at=r[10] or 0.0,
        created_at=r[11] or 0.0, updated_at=r[12] or 0.0,
    )


def get_rule(company_id: str, integration_type: str) -> Optional[SyncRule]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM integration_sync_rules WHERE company_id = ? AND integration_type = ?",
        (company_id, integration_type),
    ).fetchone()
    conn.close()
    return _row_to_rule(row) if row else None


def list_rules(company_id: str) -> List[SyncRule]:
    conn = _connect()
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM integration_sync_rules WHERE company_id = ? ORDER BY integration_type ASC",
        (company_id,),
    ).fetchall()
    conn.close()
    return [_row_to_rule(r) for r in rows]


def list_active_rules() -> List[SyncRule]:
    """Cross-tenant on purpose — this is the scheduler's own due-scan
    (integrations/scheduler.py), internal infra, not a user-facing query.
    Every result is still processed and logged per its own company_id."""
    conn = _connect()
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM integration_sync_rules "
        "WHERE enabled = 1 AND frequency != 'manual'"
    ).fetchall()
    conn.close()
    return [_row_to_rule(r) for r in rows]


def upsert_rule(rule: SyncRule) -> SyncRule:
    """Preserves `id`/`created_at` of an existing row for the same
    (company_id, integration_type) pair rather than minting a new id."""
    assert rule.company_id, "SyncRule.company_id must be set before saving."
    assert rule.integration_type, "SyncRule.integration_type must be set before saving."

    now = time.time()
    conn = _connect()
    existing = conn.execute(
        "SELECT id, created_at, last_enqueued_at FROM integration_sync_rules "
        "WHERE company_id = ? AND integration_type = ?",
        (rule.company_id, rule.integration_type),
    ).fetchone()
    if existing:
        rule.id, rule.created_at, rule.last_enqueued_at = existing[0], existing[1] or now, existing[2] or 0.0
    else:
        rule.created_at = now
    rule.updated_at = now

    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO integration_sync_rules
               (id, company_id, integration_type, frequency, direction, conflict_handling,
                fields_send, fields_receive, automation_triggers, enabled, last_enqueued_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule.id, rule.company_id, rule.integration_type, rule.frequency, rule.direction,
                rule.conflict_handling, json.dumps(rule.fields_send or []),
                json.dumps(rule.fields_receive or []), json.dumps(rule.automation_triggers or []),
                int(rule.enabled), rule.last_enqueued_at, rule.created_at, rule.updated_at,
            ),
        )
    conn.close()
    return rule


def mark_enqueued(company_id: str, integration_type: str, when: Optional[float] = None) -> None:
    """Called by integrations/scheduler.py right after it creates a new
    sync_job for this rule — advances the due-clock regardless of whether
    that job ultimately succeeds, so a failing/skipped integration still
    waits out its full interval instead of being re-enqueued every tick."""
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE integration_sync_rules SET last_enqueued_at = ? "
            "WHERE company_id = ? AND integration_type = ?",
            (when if when is not None else time.time(), company_id, integration_type),
        )
    conn.close()
