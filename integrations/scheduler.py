"""The sync scheduler/worker — Phase 1 of the Universal Integration
Configuration Framework's automation foundation.

Deliberately ZERO Streamlit dependency anywhere in this file. app.py wraps
run_forever() in a background thread via a @st.cache_resource singleton
(the same pattern as its existing _photo_executor()) purely as a
convenience for today's single-process deployment — but this module is
fully usable on its own:

    python -m integrations.scheduler

That standalone entry point is what proves this could move to its own
worker process, container, or queue-based system later without any change
to the database model — app.py's wrapper would just stop starting the
thread, and something else would run this file instead.

EXECUTORS starts EMPTY in Phase 1 — real per-integration auto-sync logic
(what BaseLinker/eBay/etc. actually DO when a job comes due) is explicit,
incremental future work. A job that comes due for an integration_type with
no registered executor resolves honestly to STATUS_SKIPPED, not a fake
success — see modules/sync_job_store.py's state machine.
"""
import logging
import time
from typing import Callable, Dict

from modules import integration_store, sync_job_store, sync_rules_store

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30
ORDER_SYNC_INTERVAL_SECONDS = 300  # 5 minutes — see poll_order_sync_once()

# integration_type -> callable(company_id, integration_type, job_type, payload).
# Raise on failure (feeds retry/backoff); return normally on success. Empty
# in Phase 1 — populated incrementally, integration by integration, later.
EXECUTORS: Dict[str, Callable] = {}

# Overridable by tests that need to fast-forward time without real sleeping.
_now = time.time

# In-memory, not DB-backed: this is one long-lived background thread for
# the whole process (see app.py's ensure_scheduler_running()), so a plain
# module variable is enough to keep the actual DELETE work to once/day
# instead of every 30s tick. A process restart just means it may prune a
# little earlier than a full 24h later — harmless, since pruning is
# idempotent (there's nothing left to delete twice).
_last_prune_at = 0.0
_PRUNE_INTERVAL_SECONDS = 86400  # once/day
_LOG_RETENTION_DAYS = 90


def poll_once() -> int:
    """Scans every company's enabled, non-manual sync rules and enqueues a
    product_export job for any that are due. Returns how many jobs were
    newly created. Cross-tenant by design (internal scheduler infra, not a
    user-facing query) — every job it creates still carries its own
    company_id and is processed/logged per-company."""
    enqueued = 0
    now = _now()
    for rule in sync_rules_store.list_active_rules():
        interval = sync_rules_store.FREQUENCY_INTERVAL_SECONDS.get(rule.frequency)
        if interval is None:
            continue
        if now - rule.last_enqueued_at < interval:
            continue

        job = sync_job_store.create_job(
            rule.company_id, rule.integration_type, sync_job_store.JOB_TYPE_PRODUCT_EXPORT,
            trigger=sync_job_store.TRIGGER_SCHEDULED, direction=rule.direction,
        )
        if job is not None:
            # Only advance the due-clock when a job was actually created.
            # If one was already active (still retrying, say), leave
            # last_enqueued_at alone so a fresh job gets created the moment
            # the in-flight one resolves, instead of waiting out a whole
            # extra interval on top of the retry delay.
            sync_rules_store.mark_enqueued(rule.company_id, rule.integration_type, now)
            enqueued += 1
    return enqueued


def _log_terminal(job: sync_job_store.SyncJob, status: str, error_message: str = "") -> None:
    integration_store.record_sync(
        job.company_id, job.integration_type, f"scheduled_{job.job_type}",
        status=status, error_message=error_message,
    )


def process_due_jobs(limit: int = 5) -> int:
    """Claims and executes up to `limit` due jobs (pending or past their
    retry backoff). Returns how many jobs were processed this call
    (including ones that ended up 'retrying' again, not just terminal
    ones)."""
    processed = 0
    for job in sync_job_store.claim_due_jobs(limit=limit):
        executor = EXECUTORS.get(job.integration_type)
        if executor is None:
            message = f"No sync executor registered for '{job.integration_type}' yet (Phase 1)."
            sync_job_store.mark_skipped(job.id, message)
            _log_terminal(job, integration_store.SYNC_STATUS_SKIPPED, message)
            processed += 1
            continue

        try:
            executor(job.company_id, job.integration_type, job.job_type, job.payload)
        except Exception as e:  # noqa: BLE001 - any executor failure must feed retry/backoff, never crash the loop
            status = sync_job_store.mark_failure(job.id, str(e), job.attempts, job.max_attempts)
            if status == sync_job_store.STATUS_ERROR:
                _log_terminal(job, integration_store.SYNC_STATUS_ERROR, str(e))
            # STATUS_RETRYING isn't terminal — no log row until it finally
            # succeeds or exhausts max_attempts.
        else:
            sync_job_store.mark_success(job.id)
            _log_terminal(job, integration_store.SYNC_STATUS_SUCCESS)
        processed += 1
    return processed


def poll_two_way_sync_once() -> int:
    """Real two-way sync automation's own due-scan — completely separate
    from poll_once()/process_due_jobs() above (the old product_export job
    path, untouched). Only ever looks at rows with the NEW
    auto_sync_enabled=1 gate (modules/sync_rules_store.list_auto_sync_rules(),
    default False for every company including ones with the old enabled=1
    flag already set) — a company that hasn't explicitly opted in here is
    completely unaffected by this function's existence. Returns how many
    due (push-interval-or-pull-interval-elapsed) rules were processed this
    tick."""
    from modules import marketplace_store, sync_rules_store
    from sync import engine as sync_engine

    processed = 0
    now = _now()
    for rule in sync_rules_store.list_auto_sync_rules():
        if now - rule.last_push_at >= rule.push_interval_seconds:
            sync_engine.process_sync_queue()
            sync_rules_store.mark_pushed(rule.company_id, rule.integration_type, now)
            processed += 1

        if now - rule.last_pull_at >= rule.pull_interval_seconds:
            for product in marketplace_store.list_products_with_listing(rule.company_id, rule.integration_type):
                sync_engine.pull_product(rule.company_id, product, rule.integration_type)
            sync_rules_store.mark_pulled(rule.company_id, rule.integration_type, now)
            processed += 1
    return processed


def poll_order_sync_once() -> int:
    """Order sync's own due-scan — connector-agnostic (loops every
    marketplace CatalogEntry, never hardcodes "baselinker") and uses its own
    orders_last_sync_at cursor, completely independent of last_sync_at
    (the product/catalog export cursor poll_once()/poll_two_way_sync_once()
    use) — see modules/integration_store.py's CompanyIntegration docstring.
    Only BaseLinker's connector overrides fetch_orders() non-trivially
    today; every other connector's default [] just means this is a no-op
    for it, same honest-empty convention as fetch_catalog(). Returns how
    many companies were actually synced this tick."""
    from modules import order_store, sync_job_store
    from integrations import manager

    synced = 0
    now = _now()
    for entry in manager.CATALOG:
        if entry.integration_category != integration_store.CATEGORY_MARKETPLACE or not entry.available:
            continue
        for record in integration_store.list_connected_companies(entry.integration_type):
            if now - record.orders_last_sync_at < ORDER_SYNC_INTERVAL_SECONDS:
                continue

            job = sync_job_store.create_job(
                record.company_id, entry.integration_type, sync_job_store.JOB_TYPE_ORDER_IMPORT,
                trigger=sync_job_store.TRIGGER_SCHEDULED, direction="pull",
            )
            if job is None:
                continue  # an order_import job for this company/integration is already in flight

            try:
                connector = manager.get(record.company_id, entry.integration_type)
                orders = connector.fetch_orders(since=record.orders_last_sync_at)
                for order in orders:
                    searchable_skus = " ".join(i.sku for i in order.items if i.sku)
                    order_store.upsert_order(order, searchable_skus=searchable_skus)
            except Exception as e:  # noqa: BLE001 - one company's failure must never kill the scheduler tick
                sync_job_store.mark_failure(job.id, str(e), job.attempts, job.max_attempts)
                _log_terminal(job, integration_store.SYNC_STATUS_ERROR, str(e))
                continue

            sync_job_store.mark_success(job.id)
            _log_terminal(job, integration_store.SYNC_STATUS_SUCCESS)
            integration_store.touch_orders_last_sync(record.company_id, entry.integration_type)
            synced += 1
    return synced


def prune_old_logs_once(days: int = _LOG_RETENTION_DAYS) -> int:
    """Deletes audit_log/sync_logs/integration_sync_log rows older than
    `days`, at most once per _PRUNE_INTERVAL_SECONDS — without this, all
    three grow forever (see modules/audit_store.py, sync_log_store.py,
    integration_store.py's prune_*() functions this calls). Returns 0
    without touching the DB at all when called again before the interval
    has elapsed, so it's cheap to call on every scheduler tick."""
    global _last_prune_at
    now = _now()
    if now - _last_prune_at < _PRUNE_INTERVAL_SECONDS:
        return 0
    from modules import audit_store, integration_store, sync_log_store

    total = (
        audit_store.prune_older_than(days)
        + sync_log_store.prune_older_than(days)
        + integration_store.prune_sync_log_older_than(days)
    )
    _last_prune_at = now
    if total:
        logger.info("Pruned %d log row(s) older than %d days", total, days)
    return total


def run_forever(poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
    """The background loop. A tick failing outright (e.g. a transient DB
    hiccup) is logged and swallowed — one bad tick must never kill the
    whole scheduler thread for the rest of the process's life."""
    logger.info("Sync scheduler starting (poll_interval=%ss)", poll_interval)
    while True:
        try:
            poll_once()
            process_due_jobs()
            poll_two_way_sync_once()
            poll_order_sync_once()
            prune_old_logs_once()
        except Exception:
            logger.exception("Sync scheduler tick failed")
        time.sleep(poll_interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
