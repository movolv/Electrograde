"""Per-company daily quota for MANUAL product connection checks — the
"Check product connection" button in the Product List's integrations
dialog, which spends a real marketplace API call each time.

The quota exists to protect the external APIs (and this app's standing
of them) from an operator clicking through hundreds of products. It is a
COMPANY-WIDE total across every integration, not a per-integration
allowance: 30 BaseLinker + 20 WooCommerce + 15 eBay checks all draw from
the same 100.

Authoritative on the backend, never merely a disabled button: every check
goes through try_consume()'s single atomic statement, so N concurrent
requests (parallel tabs, rapid refreshes, a scripted client) can never
collectively exceed the limit — Postgres serializes the conflicting
upserts, and the WHERE guard on the DO UPDATE branch simply matches no
row once the count is at the limit, returning nothing.

Day boundaries are UTC, deliberately: a company-local date would need a
timezone per company (which this app doesn't model) and would make the
reset moment ambiguous for a company operating across zones.

Shares the same PostgreSQL database as every other modules/*_store.py
(modules/db.py, `?`-style params translated to `%s` by that module).
"""
import time
from typing import Tuple

from modules import db

# The ONLY definition of this number. A future plan/subscription tier
# would resolve its own value and pass it through limit_for_company()
# below — call sites must never inline a literal, so raising the cap for
# a tier stays a one-place change.
DAILY_PRODUCT_CONNECTION_CHECKS = 100


def limit_for_company(company_id: str) -> int:
    """This company's daily allowance. A flat constant today; the
    indirection is what lets Free/Pro/Enterprise tiers differ later
    without touching any caller."""
    return DAILY_PRODUCT_CONNECTION_CHECKS


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_check_usage (
            company_id TEXT NOT NULL,
            day TEXT NOT NULL,
            check_count INTEGER NOT NULL DEFAULT 0,
            updated_at DOUBLE PRECISION,
            PRIMARY KEY (company_id, day)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_integration_check_usage_company ON integration_check_usage(company_id)")
    conn.commit()
    return conn


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def get_usage(company_id: str) -> Tuple[int, int]:
    """(used_today, limit) — read-only, consumes nothing. For showing
    "12 / 100 used today" next to the button."""
    conn = _connect()
    row = conn.execute(
        "SELECT check_count FROM integration_check_usage WHERE company_id = ? AND day = ?",
        (company_id, _today()),
    ).fetchone()
    conn.close()
    return (row[0] if row else 0), limit_for_company(company_id)


def try_consume(company_id: str) -> Tuple[bool, int, int]:
    """Atomically claim one check for today. Returns
    (allowed, used_after, limit).

    The whole decision is ONE statement: the row is created at 1 if this
    is the day's first check, otherwise incremented — but the DO UPDATE
    branch carries a `WHERE check_count < limit` guard, so at the limit it
    updates nothing and RETURNING yields no row. That is what makes
    concurrent callers safe; a read-then-write pair (SELECT the count,
    decide, UPDATE) would let two requests both read 99 and both proceed.

    Call this BEFORE spending the API call, and only for calls that will
    actually be made — a request rejected here must never reach the
    marketplace.
    """
    limit = limit_for_company(company_id)
    conn = _connect()
    with conn:
        row = conn.execute(
            """INSERT INTO integration_check_usage (company_id, day, check_count, updated_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT (company_id, day) DO UPDATE
                   SET check_count = integration_check_usage.check_count + 1,
                       updated_at = EXCLUDED.updated_at
                   WHERE integration_check_usage.check_count < ?
               RETURNING check_count""",
            (company_id, _today(), time.time(), limit),
        ).fetchone()
    conn.close()
    if row is None:
        # DO UPDATE's guard matched nothing: already at the limit.
        return False, limit, limit
    return True, row[0], limit
