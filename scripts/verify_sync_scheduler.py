"""Proves the sync-job-queue/scheduler foundation (Phase 1 of the Universal
Integration Configuration Framework) actually works end-to-end — happy
path, the honest "no executor yet" outcome, retry/backoff math, the
one-active-job-per-(company,integration,job_type) guard, and cross-tenant
isolation of the three new tables. Registers dummy test executors directly
on integrations.scheduler.EXECUTORS — the real (empty) Phase 1 registry
that app.py uses is never touched by this script.

Runs against a throwaway scratch database (ELECTROGRADER_DB_PATH), set
*before* importing any modules.*_store / integrations.scheduler module —
same convention as scripts/verify_tenant_isolation.py.

    python scripts/verify_sync_scheduler.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCRATCH_DIR = tempfile.mkdtemp(prefix="electrograder_scheduler_test_")
os.environ["ELECTROGRADER_DB_PATH"] = os.path.join(_SCRATCH_DIR, "scheduler_test.db")
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import company_store, integration_store  # noqa: E402
from modules import sync_job_store as jobs  # noqa: E402
from modules import sync_rules_store as rules  # noqa: E402
from integrations import manager as integration_manager  # noqa: E402
from integrations import scheduler  # noqa: E402

_checks_passed = 0
_failures = []


def check(label: str, condition: bool) -> None:
    global _checks_passed
    if condition:
        _checks_passed += 1
    else:
        _failures.append(label)
    print(("  OK  " if condition else "  FAIL"), label)


def main() -> int:
    print(f"Scratch DB: {os.environ['ELECTROGRADER_DB_PATH']}\n")

    company_a = company_store.create_company("Scheduler Test Co A", user_limit=10)
    company_b = company_store.create_company("Scheduler Test Co B", user_limit=10)

    # ------------------------------------------------------------- happy path --
    print("-- happy path: due rule -> pending job -> success -> logged --")
    scheduler.EXECUTORS["test_dummy"] = lambda company_id, integration_type, job_type, payload: None

    rule_a = rules.SyncRule(company_id=company_a.id, integration_type="test_dummy", frequency=rules.FREQUENCY_EVERY_15_MIN)
    rules.upsert_rule(rule_a)

    enqueued = scheduler.poll_once()
    check("poll_once() enqueues exactly one job for the due rule", enqueued == 1)
    job_list = jobs.list_jobs(company_a.id, "test_dummy")
    check("exactly one pending job exists after poll_once()", len(job_list) == 1 and job_list[0].status == jobs.STATUS_PENDING)

    processed = scheduler.process_due_jobs()
    check("process_due_jobs() processes the job", processed == 1)
    job_list = jobs.list_jobs(company_a.id, "test_dummy")
    check("job resolved to success", job_list[0].status == jobs.STATUS_SUCCESS)
    log = integration_store.list_sync_log(company_a.id, "test_dummy")
    check(
        "exactly one SUCCESS row landed in integration_sync_log",
        len(log) == 1 and log[0].status == integration_store.SYNC_STATUS_SUCCESS,
    )
    check(
        "SyncRule.last_enqueued_at advanced",
        rules.get_rule(company_a.id, "test_dummy").last_enqueued_at > 0,
    )

    # ------------------------------------------------------ skipped_not_implemented --
    print("\n-- skipped_not_implemented: due rule with no registered executor --")
    rule_baselinker = rules.SyncRule(company_id=company_a.id, integration_type="baselinker", frequency=rules.FREQUENCY_HOURLY)
    rules.upsert_rule(rule_baselinker)
    scheduler.poll_once()
    scheduler.process_due_jobs()
    bl_jobs = jobs.list_jobs(company_a.id, "baselinker")
    check(
        "job resolves to skipped_not_implemented (no executor registered)",
        len(bl_jobs) == 1 and bl_jobs[0].status == jobs.STATUS_SKIPPED,
    )
    check("attempts NOT incremented for a skipped job (it's not a failed attempt)", bl_jobs[0].attempts == 0)
    bl_log = integration_store.list_sync_log(company_a.id, "baselinker")
    check(
        "exactly one SKIPPED row landed in integration_sync_log",
        len(bl_log) == 1 and bl_log[0].status == integration_store.SYNC_STATUS_SKIPPED,
    )

    # -------------------------------------------------------------- retry/backoff --
    print("\n-- retry/backoff: always-failing executor, max_attempts=2 --")

    def _flaky(company_id, integration_type, job_type, payload):
        raise RuntimeError("boom")

    scheduler.EXECUTORS["flaky"] = _flaky
    job1 = jobs.create_job(company_b.id, "flaky", jobs.JOB_TYPE_PRODUCT_EXPORT, max_attempts=2)
    check("flaky job created", job1 is not None)

    claimed = jobs.claim_due_jobs()
    check("flaky job claimed", len(claimed) == 1)
    status1 = jobs.mark_failure(claimed[0].id, "boom", claimed[0].attempts, claimed[0].max_attempts)
    check("1st failure -> retrying (attempts=1 < max_attempts=2)", status1 == jobs.STATUS_RETRYING)
    after1 = jobs.list_jobs(company_b.id, "flaky")[0]
    check("attempts == 1 after first failure", after1.attempts == 1)
    check("next_attempt_at pushed into the future (~60s backoff)", after1.next_attempt_at > time.time() + 30)

    # Force the retry to be immediately claimable for the test instead of
    # really sleeping ~60s.
    conn_fix_next_attempt = jobs._connect()  # noqa: SLF001 - test-only direct poke, not part of the public API
    with conn_fix_next_attempt:
        conn_fix_next_attempt.execute("UPDATE sync_jobs SET next_attempt_at = ? WHERE id = ?", (time.time(), after1.id))
    conn_fix_next_attempt.close()

    claimed2 = jobs.claim_due_jobs()
    check("retrying job reclaimed after backoff window", len(claimed2) == 1)
    status2 = jobs.mark_failure(claimed2[0].id, "boom again", claimed2[0].attempts, claimed2[0].max_attempts)
    check("2nd failure -> error (attempts=2 >= max_attempts=2)", status2 == jobs.STATUS_ERROR)
    final = jobs.list_jobs(company_b.id, "flaky")[0]
    check("final status is error, attempts capped at max_attempts", final.status == jobs.STATUS_ERROR and final.attempts == 2)

    # ---------------------------------------------------------- double-enqueue guard --
    print("\n-- double-enqueue guard: same due rule polled twice in a row --")
    rule_c = rules.SyncRule(company_id=company_a.id, integration_type="test_dummy2", frequency=rules.FREQUENCY_EVERY_15_MIN)
    scheduler.EXECUTORS["test_dummy2"] = lambda *a: None
    rules.upsert_rule(rule_c)
    scheduler.poll_once()
    scheduler.poll_once()  # second call before the first job is processed
    check(
        "only one sync_jobs row exists despite polling twice",
        len(jobs.list_jobs(company_a.id, "test_dummy2")) == 1,
    )
    # different job_type for the same integration must NOT be blocked by the active one
    other_job = jobs.create_job(company_a.id, "test_dummy2", jobs.JOB_TYPE_PRICE_UPDATE)
    check(
        "a different job_type for the same integration can be enqueued concurrently",
        other_job is not None,
    )

    # ------------------------------------------------------- get_supported_target_fields --
    print("\n-- get_supported_target_fields --")
    bl_fields = integration_manager.get_supported_target_fields("baselinker")
    check("baselinker declares condition_id/category_id target fields", set(bl_fields) == {"condition_id", "category_id"})
    check("deepl (a ServiceConnector) has no target fields", integration_manager.get_supported_target_fields("deepl") == {})
    check("unknown integration_type returns {}", integration_manager.get_supported_target_fields("nonexistent") == {})

    # ------------------------------------------------------------- tenant isolation --
    print("\n-- cross-tenant isolation: sync_rules / sync_jobs / field_mappings --")
    from modules import field_mapping_store as mapping_store

    rules.upsert_rule(rules.SyncRule(company_id=company_b.id, integration_type="isolation_check", frequency=rules.FREQUENCY_DAILY))
    check(
        "list_rules(company=A) never contains company B's isolation_check rule",
        "isolation_check" not in {r.integration_type for r in rules.list_rules(company_a.id)},
    )
    check(
        "list_rules(company=B) contains its own isolation_check rule",
        "isolation_check" in {r.integration_type for r in rules.list_rules(company_b.id)},
    )

    jobs.create_job(company_b.id, "isolation_check", jobs.JOB_TYPE_PRODUCT_EXPORT)
    check(
        "list_jobs(company=A) never contains company B's isolation_check job",
        "isolation_check" not in {j.integration_type for j in jobs.list_jobs(company_a.id)},
    )
    check(
        "list_jobs(company=B) contains its own isolation_check job",
        "isolation_check" in {j.integration_type for j in jobs.list_jobs(company_b.id)},
    )

    mapping_store.upsert_mapping(
        mapping_store.FieldMapping(
            company_id=company_b.id, integration_type="isolation_check",
            rules=[mapping_store.FieldMappingRule(source_field="grade", source_value="B", target_label="secret-b")],
        )
    )
    check(
        "get_mapping(company=A, isolation_check) is None despite B having one",
        mapping_store.get_mapping(company_a.id, "isolation_check") is None,
    )
    check(
        "get_mapping(company=B, isolation_check) returns B's own mapping",
        mapping_store.get_mapping(company_b.id, "isolation_check") is not None,
    )

    # ------------------------------------------------------------------- summary --
    print(f"\n{_checks_passed} check(s) passed, {len(_failures)} failed.")
    if _failures:
        print("\nFAILED:")
        for f in _failures:
            print(f"  - {f}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    finally:
        shutil.rmtree(_SCRATCH_DIR, ignore_errors=True)
    raise SystemExit(exit_code)
