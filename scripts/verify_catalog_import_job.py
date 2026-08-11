"""Proves the background-import progress tracking
(modules/catalog_import_job_store.py + sync/catalog_import.py's
on_progress callback) works correctly:
  - start_job/update_progress/finish_job/get_job persist and return the
    expected state at each stage;
  - import_catalog()'s on_progress callback fires once per processed item
    with correct, monotonically-increasing counts and a constant total;
  - full cross-tenant isolation on the new table.

Runs against a throwaway scratch PostgreSQL database (ELECTROGRADER_DATABASE_URL), set
*before* importing any modules.*_store / integrations / sync module — same
convention as every other verify_*.py script in this repo.

    python scripts/verify_catalog_import_job.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

_DATABASE_URL, _drop_scratch_db = make_scratch_database("catalog_import_job")
os.environ["ELECTROGRADER_DATABASE_URL"] = _DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import catalog_import_job_store as job_store  # noqa: E402
from modules import company_store, integration_store  # noqa: E402
from integrations import manager as integration_manager  # noqa: E402
from integrations.base import ConnectionTestResult, ConnectorActionResult, ImportedProductData, MarketplaceConnector  # noqa: E402
from sync import catalog_import  # noqa: E402

_checks_passed = 0
_failures = []


def check(label: str, condition: bool) -> None:
    global _checks_passed
    if condition:
        _checks_passed += 1
    else:
        _failures.append(label)
    print(("  OK  " if condition else "  FAIL"), label)


class _FakeMarketplaceConnector(MarketplaceConnector):
    integration_type = "test_fake_mp_job"

    def connect(self):
        return ConnectionTestResult(True, "ok")

    def test_connection(self):
        return ConnectionTestResult(True, "ok")

    def create_product(self, product):
        return ConnectorActionResult(success=True, external_id="fake-ext", message="Created.")

    def update_product(self, product, external_id):
        return ConnectorActionResult(success=True, external_id=external_id, message="Updated.")

    def delete_product(self, external_id):
        return ConnectorActionResult(success=True, external_id=external_id, message="Deleted.")

    def sync_inventory(self, product, external_id):
        return self.update_product(product, external_id)

    def fetch_catalog(self):
        return [
            ImportedProductData(external_id=f"ext-{i}", sku=f"JOB-SKU-{i}", name=f"Job Product {i}", price=1.0, quantity=1)
            for i in range(1, 4)
        ]


integration_manager.CONNECTORS["test_fake_mp_job"] = _FakeMarketplaceConnector
integration_manager._CATALOG_BY_TYPE["test_fake_mp_job"] = integration_manager.CatalogEntry(  # noqa: SLF001
    "test_fake_mp_job", "marketplace", "Test Fake Marketplace Job", True,
)


def main() -> int:
    print(f"Scratch DB: {_DATABASE_URL}\n")

    company_a = company_store.create_company("Import Job Test Co A", user_limit=10)
    company_b = company_store.create_company("Import Job Test Co B", user_limit=10)

    integration_store.upsert_integration(
        integration_store.CompanyIntegration(
            company_id=company_a.id, integration_type="test_fake_mp_job", integration_category="marketplace",
            status=integration_store.STATUS_CONNECTED, credentials={"token": "x"}, settings={},
        )
    )

    # -------------------------------------------------------- store basics --
    print("-- catalog_import_job_store basics --")
    check("no job yet -> get_job returns None", job_store.get_job(company_a.id, "test_fake_mp_job") is None)

    job_store.start_job(company_a.id, "test_fake_mp_job", total=3)
    job = job_store.get_job(company_a.id, "test_fake_mp_job")
    check("start_job: status=running", job.status == job_store.STATUS_RUNNING)
    check("start_job: total set, imported/skipped start at 0", job.total == 3 and job.imported == 0 and job.skipped == 0)

    job_store.update_progress(company_a.id, "test_fake_mp_job", imported=1, skipped=0, error_count=0)
    job = job_store.get_job(company_a.id, "test_fake_mp_job")
    check("update_progress: imported reflected", job.imported == 1)
    check("update_progress: status stays running", job.status == job_store.STATUS_RUNNING)

    job_store.finish_job(company_a.id, "test_fake_mp_job", success=True, errors=[])
    job = job_store.get_job(company_a.id, "test_fake_mp_job")
    check("finish_job(success): status=success", job.status == job_store.STATUS_SUCCESS)
    check("finish_job: finished_at is set", job.finished_at > 0)

    job_store.start_job(company_a.id, "test_fake_mp_job", total=5)
    job_store.finish_job(company_a.id, "test_fake_mp_job", success=False, errors=["SKU-X: boom"])
    job = job_store.get_job(company_a.id, "test_fake_mp_job")
    check("a new start_job overwrites the previous run's final state", job.total == 5)
    check("finish_job(failure): status=failed", job.status == job_store.STATUS_FAILED)
    check("finish_job: errors stored", job.errors == ["SKU-X: boom"])

    # ---------------------------------------------------- on_progress callback --
    print("\n-- import_catalog()'s on_progress callback --")
    calls = []
    result = catalog_import.import_catalog(
        company_a.id, "test_fake_mp_job", "tester",
        on_progress=lambda imported, skipped, error_count, total: calls.append((imported, skipped, error_count, total)),
    )
    check("on_progress called once per item (3 items)", len(calls) == 3)
    check("total is constant across all calls", all(c[3] == 3 for c in calls))
    check("imported count increases monotonically to the final total", [c[0] for c in calls] == [1, 2, 3])
    check("final on_progress call matches import_catalog()'s own return value", calls[-1][0] == result["imported"])

    # ------------------------------------------------------------------- isolation --
    print("\n-- cross-tenant isolation --")
    job_store.start_job(company_b.id, "test_fake_mp_job", total=9)
    job_a = job_store.get_job(company_a.id, "test_fake_mp_job")
    job_b = job_store.get_job(company_b.id, "test_fake_mp_job")
    check("company A's job unaffected by company B's start_job", job_a.total == 5)
    check("company B has its own independent job", job_b.total == 9 and job_b.status == job_store.STATUS_RUNNING)

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
        _drop_scratch_db()
    raise SystemExit(exit_code)
