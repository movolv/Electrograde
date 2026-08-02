"""One-off (idempotent — safe to re-run) migration: seeds a
company_integrations row for BaseLinker from the legacy app-wide
BASELINKER_* env vars, so the one company that was actually using them
before the Universal Integration Architecture doesn't lose its working
config. Only ever writes a row if none exists yet for the target company —
never overwrites an edit made later through Settings -> Integrations.

Run once, from the project root, after modules/integration_store.py and
integrations/ exist:

    python scripts/migrate_baselinker_env_to_integration.py --company-id default

Requires ELECTROGRADER_ENCRYPTION_KEY to already be set (integration_store
encrypts credentials on write) and BASELINKER_API_TOKEN/INVENTORY_ID/
CATEGORY_ID to be present in the environment (e.g. via .env) — if they're
not set, there's nothing to migrate and the script exits cleanly.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from integrations.manager import IntegrationManager  # noqa: E402
from modules import integration_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", default="default")
    args = parser.parse_args()

    existing = integration_store.get_integration(args.company_id, "baselinker")
    if existing is not None:
        print(
            f"Company {args.company_id!r} already has a BaseLinker integration "
            f"(status={existing.status!r}) — nothing to migrate."
        )
        return 0

    token = os.environ.get("BASELINKER_API_TOKEN")
    inventory_id = os.environ.get("BASELINKER_INVENTORY_ID")
    category_id = os.environ.get("BASELINKER_CATEGORY_ID")
    if not (token and inventory_id and category_id):
        print("BASELINKER_API_TOKEN / BASELINKER_INVENTORY_ID / BASELINKER_CATEGORY_ID "
              "are not all set — nothing to migrate.")
        return 0

    credentials = {"token": token}
    settings = {
        "inventory_id": inventory_id,
        "category_id": category_id,
        "price_group_id": os.environ.get("BASELINKER_PRICE_GROUP_ID") or "",
        "warehouse_id": os.environ.get("BASELINKER_WAREHOUSE_ID") or "",
        "tax_rate": os.environ.get("BASELINKER_TAX_RATE", "23"),
    }

    result = IntegrationManager.connect(args.company_id, "baselinker", credentials, settings)
    if result.success:
        print(f"Migrated legacy BaseLinker config to company {args.company_id!r}: {result.message}")
        return 0

    # Persist regardless — goal is "don't lose the existing working config",
    # not "re-validate it right now" (e.g. the account/network could be
    # temporarily unreachable at migration time).
    print(f"WARNING: test_connection failed ({result.message}) — saving the legacy config anyway, "
          f"marked as needing attention.")
    integration_store.upsert_integration(
        integration_store.CompanyIntegration(
            company_id=args.company_id, integration_type="baselinker",
            integration_category=integration_store.CATEGORY_MARKETPLACE,
            status=integration_store.STATUS_ERROR,
            credentials=credentials, settings=settings,
        )
    )
    print(f"Migrated legacy BaseLinker config to company {args.company_id!r} (status=error — check Settings).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
