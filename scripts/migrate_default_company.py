"""One-off (idempotent — safe to re-run) migration: creates the 'default'
company record that all 242 pre-existing product/manifest rows already
implicitly belong to (their company_id column has always defaulted to the
literal string 'default'), and registers one bootstrap admin so there's a
way to log into the newly-gated app at all.

Run once, from the project root, after modules/company_store.py,
modules/auth_store.py, and modules/auth.py exist but before app.py's login
gate is wired in:

    python scripts/migrate_default_company.py --admin-email you@example.com --admin-password "..." --admin-name "Your Name"

Credentials are never hardcoded here — always supplied via CLI args (or the
BOOTSTRAP_ADMIN_EMAIL/BOOTSTRAP_ADMIN_PASSWORD/BOOTSTRAP_ADMIN_NAME env vars
as a fallback, e.g. for a non-interactive deploy step).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import auth, auth_store, company_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-email", default=os.environ.get("BOOTSTRAP_ADMIN_EMAIL"))
    parser.add_argument("--admin-password", default=os.environ.get("BOOTSTRAP_ADMIN_PASSWORD"))
    parser.add_argument("--admin-name", default=os.environ.get("BOOTSTRAP_ADMIN_NAME", "Admin"))
    args = parser.parse_args()

    if not args.admin_email or not args.admin_password:
        parser.error(
            "--admin-email and --admin-password are required (or set "
            "BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD)."
        )

    company = company_store.get_company(company_store.DEFAULT_COMPANY_ID)
    if company is None:
        # Generous limits: this company already holds every pre-existing
        # record, so it must never be blocked by a low default.
        company = company_store.create_company(
            name="Default Company (legacy data)",
            plan="legacy",
            user_limit=1000,
            product_limit=1_000_000,
            company_id=company_store.DEFAULT_COMPANY_ID,
        )
        print(f"Created company {company.id!r} ({company.name!r}).")
    else:
        print(f"Company {company.id!r} already exists — skipping creation.")

    existing = auth_store.get_user_by_email(company.id, args.admin_email.strip().lower())
    if existing:
        print(f"Admin {args.admin_email!r} already exists in company {company.id!r} — nothing to do.")
        return 0

    user = auth.register_user(
        company_id=company.id,
        name=args.admin_name,
        email=args.admin_email,
        password=args.admin_password,
        role=auth.ROLE_ADMIN,
    )
    print(f"Registered bootstrap admin {user.email!r} (id={user.id!r}) in company {company.id!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
