"""Platform Super Admin recovery tool — the ONE path back into ElectroGrader
that works entirely through OS/server access, independent of the normal web
login. If Super Admin access is ever lost (forgotten password, lost email,
a broken web session, or the account accidentally disabled), this is how
you get back in — never by editing the database by hand.

Being a Super Admin is a platform-level fact about a User (a row in
modules/platform_admin_store.py), independent of whatever company-scoped
role (admin/employee/reviewer) that same user also holds — see
modules/auth.py's is_super_admin()/require_super_admin(). This script only
ever writes to the `companies`, `users`, and `platform_admins` tables —
never to products/inventory/marketplace/sync/audit-history data.

Commands:
    python scripts/superadmin_cli.py list
        Read-only, always safe. Lists every Super Admin account.

    python scripts/superadmin_cli.py create [--email E] [--name N]
        Bootstraps the ElectroGrader company (if it doesn't exist yet) and
        the FIRST Super Admin. Idempotent: re-running it while an active
        Super Admin already exists is a clean, reported REFUSAL — there is
        no flag to override this. Once one exists, additional Super Admins
        are only ever granted from the web app's Companies page by an
        existing Super Admin, never from this command. This command's only
        job is recovering from ZERO active Super Admins.

    python scripts/superadmin_cli.py reset --email E [--new-email NEW] [--email-only]
        Resets an EXISTING Super Admin's password (prompted via getpass,
        never echoed/logged) without needing the old one. --new-email also
        changes their login email. --email-only skips the password prompt
        and only changes the email.

    python scripts/superadmin_cli.py enable --email E
    python scripts/superadmin_cli.py disable --email E
        Reactivate/deactivate a Super Admin. `disable` REFUSES (exit 1, no
        override flag) if it would drop the active count to zero — the
        platform invariant (active Super Admin count >= 1) is enforced
        here unconditionally, matching the identical hard block in the web
        UI. The only sanctioned way back in from a true zero-active state
        is `create`.

All commands accept --user-id as an alternative to --email, for the rare
case where the same email address is used by more than one Super Admin
account across different companies (run `list` first to see this).

Never prints a password. Never logs a password. Every state-changing
action is written to the existing audit log (modules/audit_store.py) with
actor "SYSTEM_RECOVERY" — there is no authenticated app session in a CLI
context, so that literal is the auditable marker for "done via the
recovery CLI, not the web app".
"""
import argparse
import getpass
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from modules import auth, auth_store, audit_store, company_store, db, platform_admin_store  # noqa: E402

ELECTROGRADER_COMPANY_SLUG = "electrograder"
SYSTEM_ACTOR = "SYSTEM_RECOVERY"


def _prompt_password(prompt: str) -> str:
    """getpass.getpass() reads directly from the console on Windows
    (msvcrt), not from sys.stdin — piped/redirected input (automation,
    tests) hangs forever rather than being read. Real interactive terminal
    use still gets secure, non-echoed input; non-interactive stdin falls
    back to a plain line read, same portable pattern most CLI tools use
    for this exact platform gap. Never echoed by this function either way
    — nothing here prints what was read."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    sys.stderr.write(prompt)
    return sys.stdin.readline().rstrip("\n")


def _get_or_create_electrograder_company():
    company = company_store.get_company_by_slug(ELECTROGRADER_COMPANY_SLUG)
    if company is not None:
        return company, False
    company = company_store.create_company(
        name="ElectroGrader", plan="platform", status=company_store.STATUS_ACTIVE,
        user_limit=1000, product_limit=1_000_000, slug=ELECTROGRADER_COMPANY_SLUG,
    )
    audit_store.log_audit(company.id, SYSTEM_ACTOR, "COMPANY_CREATED", "company", company.id, "bootstrap")
    return company, True


def _admin_emails(admins) -> str:
    out = []
    for pa in admins:
        u = auth_store.get_user_by_id(pa.user_id)
        out.append(u.email if u else f"(missing user {pa.user_id})")
    return ", ".join(out) or "(none)"


def _find_admin_user(email: str, user_id: str = None):
    """Returns (User_or_None, ambiguous: bool). ambiguous=True means more
    than one Super Admin account shares this email across different
    companies — caller should re-run with --user-id instead of guessing."""
    if user_id:
        u = auth_store.get_user_by_id(user_id)
        if u and platform_admin_store.get_by_user_id(u.id):
            return u, False
        return None, False
    candidates = [u for u in auth_store.get_users_by_email(email.strip().lower()) if platform_admin_store.get_by_user_id(u.id)]
    if len(candidates) > 1:
        return None, True
    return (candidates[0], False) if candidates else (None, False)


def _print_ambiguous(email: str) -> None:
    print(f"Multiple Super Admin accounts share the email {email!r} across different companies:")
    for u in auth_store.get_users_by_email(email.strip().lower()):
        if platform_admin_store.get_by_user_id(u.id):
            print(f"  user_id={u.id}  company_id={u.company_id}")
    print("Re-run with --user-id to disambiguate.")


def cmd_list(args) -> int:
    admins = platform_admin_store.list_all()
    if not admins:
        print("No Super Admins exist yet. Run 'create' to bootstrap the first one.")
        return 0
    print(f"{'EMAIL':40s} {'NAME':25s} {'ACTIVE':8s} CREATED")
    for pa in admins:
        u = auth_store.get_user_by_id(pa.user_id)
        email = u.email if u else "(missing user!)"
        name = u.name if u else "-"
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(pa.created_at))
        print(f"{email:40s} {name:25s} {'yes' if pa.is_active else 'no':8s} {created}")
    return 0


def cmd_create(args) -> int:
    company, created = _get_or_create_electrograder_company()
    print(f"{'Created' if created else 'Using existing'} company {company.name!r} (id={company.id!r}, slug={company.slug!r}).")

    active = platform_admin_store.list_active()
    if active:
        print(f"Refusing: an active Super Admin already exists ({_admin_emails(active)}).")
        print("To add another Super Admin, log in as an existing one and use the")
        print("'Grant Super Admin' action on the Companies page in the web app.")
        print("This command only recovers from ZERO active Super Admins.")
        return 1

    email = (args.email or input("Super Admin email: ")).strip().lower()
    name = (args.name or input("Super Admin name: ")).strip()
    if not email or not name:
        print("Email and name are required.")
        return 1
    password = _prompt_password("New Super Admin password: ")
    password2 = _prompt_password("Confirm password: ")
    if not password:
        print("Password must not be empty.")
        return 1
    if password != password2:
        print("Passwords do not match.")
        return 1

    user = auth_store.get_user_by_email(company.id, email)
    if user is None:
        try:
            user = auth.register_user(company_id=company.id, name=name, email=email, password=password, role=auth.ROLE_ADMIN)
        except ValueError as e:
            print(f"Could not create user: {e}")
            return 1
        print(f"Created user {user.email!r} (id={user.id!r}) as Company Admin of {company.name!r}.")
    else:
        print(f"User {user.email!r} already exists in {company.name!r} — promoting to Super Admin (password left unchanged).")

    existing_pa = platform_admin_store.get_by_user_id(user.id)
    if existing_pa is not None:
        if not existing_pa.is_active:
            platform_admin_store.set_active(existing_pa.id, True)
            audit_store.log_audit(company.id, SYSTEM_ACTOR, "SUPERADMIN_ENABLED", "user", user.id, "reactivated via bootstrap")
            print(f"Reactivated existing Super Admin record for {user.email!r}.")
        else:
            print(f"{user.email!r} is already an active Super Admin — nothing to do.")
    else:
        platform_admin_store.create(user.id)
        audit_store.log_audit(company.id, SYSTEM_ACTOR, "SUPERADMIN_CREATED", "user", user.id)
        print(f"{user.email!r} is now a Super Admin.")

    audit_store.log_audit(company.id, SYSTEM_ACTOR, "BOOTSTRAP_EXECUTED", "company", company.id)
    print("Done.")
    return 0


def cmd_reset(args) -> int:
    user, ambiguous = _find_admin_user(args.email, args.user_id)
    if ambiguous:
        _print_ambiguous(args.email)
        return 1
    if user is None:
        print(f"No Super Admin found with email {args.email!r}.")
        return 1

    changed = []
    old_email = user.email
    if args.new_email:
        new_email = args.new_email.strip().lower()
        if new_email != user.email and auth_store.get_user_by_email(user.company_id, new_email):
            print(f"Email {new_email!r} is already in use in that account's company.")
            return 1
        user.email = new_email
        changed.append("email")

    if not args.email_only:
        password = _prompt_password("New password: ")
        password2 = _prompt_password("Confirm password: ")
        if not password:
            print("Password must not be empty.")
            return 1
        if password != password2:
            print("Passwords do not match.")
            return 1
        user.password_hash = auth.hash_password(password)
        changed.append("password")

    if not changed:
        print("Nothing to change — provide a new password (default) and/or --new-email.")
        return 1

    user.updated_at = time.time()
    auth_store.update_user(user)

    if "password" in changed:
        audit_store.log_audit(user.company_id, SYSTEM_ACTOR, "SUPERADMIN_PASSWORD_RESET", "user", user.id)
    if "email" in changed:
        audit_store.log_audit(user.company_id, SYSTEM_ACTOR, "SUPERADMIN_EMAIL_CHANGED", "user", user.id, f"{old_email} -> {user.email}")
    audit_store.log_audit(user.company_id, SYSTEM_ACTOR, "RECOVERY_EXECUTED", "user", user.id, ",".join(changed))
    print(f"Updated: {', '.join(changed)}.")
    return 0


def cmd_enable(args) -> int:
    user, ambiguous = _find_admin_user(args.email, args.user_id)
    if ambiguous:
        _print_ambiguous(args.email)
        return 1
    if user is None:
        print(f"No Super Admin found with email {args.email!r}.")
        return 1
    pa = platform_admin_store.get_by_user_id(user.id)
    if pa.is_active:
        print(f"{user.email!r} is already an active Super Admin.")
        return 0
    platform_admin_store.set_active(pa.id, True)
    audit_store.log_audit(user.company_id, SYSTEM_ACTOR, "SUPERADMIN_ENABLED", "user", user.id)
    audit_store.log_audit(user.company_id, SYSTEM_ACTOR, "RECOVERY_EXECUTED", "user", user.id, "enable")
    print(f"{user.email!r} is now an active Super Admin.")
    return 0


def cmd_disable(args) -> int:
    user, ambiguous = _find_admin_user(args.email, args.user_id)
    if ambiguous:
        _print_ambiguous(args.email)
        return 1
    if user is None:
        print(f"No Super Admin found with email {args.email!r}.")
        return 1
    pa = platform_admin_store.get_by_user_id(user.id)
    if not pa.is_active:
        print(f"{user.email!r} is already inactive.")
        return 0
    if platform_admin_store.count_active() <= 1:
        print("Refusing: this is the only active Super Admin. Disabling it would leave the")
        print("platform with zero active Super Admins, which is never allowed — not even")
        print("from this CLI, and there is no override flag. If you are trying to recover")
        print("access to THIS account, use 'reset' instead of 'disable'.")
        return 1
    platform_admin_store.set_active(pa.id, False)
    audit_store.log_audit(user.company_id, SYSTEM_ACTOR, "SUPERADMIN_DISABLED", "user", user.id)
    audit_store.log_audit(user.company_id, SYSTEM_ACTOR, "RECOVERY_EXECUTED", "user", user.id, "disable")
    print(f"{user.email!r} is now inactive.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all Super Admins (read-only, always safe).")

    p_create = sub.add_parser("create", help="Bootstrap ElectroGrader company + the FIRST Super Admin. Refuses if one already exists.")
    p_create.add_argument("--email")
    p_create.add_argument("--name")

    p_reset = sub.add_parser("reset", help="Reset an existing Super Admin's password and/or email.")
    p_reset.add_argument("--email", required=True)
    p_reset.add_argument("--user-id")
    p_reset.add_argument("--new-email")
    p_reset.add_argument("--email-only", action="store_true", help="Only change --new-email, skip the password prompt.")

    p_enable = sub.add_parser("enable", help="Reactivate a disabled Super Admin.")
    p_enable.add_argument("--email", required=True)
    p_enable.add_argument("--user-id")

    p_disable = sub.add_parser("disable", help="Deactivate a Super Admin. Refuses if it's the only active one.")
    p_disable.add_argument("--email", required=True)
    p_disable.add_argument("--user-id")

    args = parser.parse_args()
    handlers = {
        "list": cmd_list, "create": cmd_create, "reset": cmd_reset,
        "enable": cmd_enable, "disable": cmd_disable,
    }
    try:
        return handlers[args.command](args)
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
