"""Framework-agnostic authentication core — deliberately zero `import
streamlit` anywhere in this file, so it stays reusable as-is if/when a
future non-Streamlit backend replaces the current UI layer. app.py is the
only place that wires this into st.session_state/cookies (see
modules/auth_cookie.py for the Streamlit-specific cookie glue, kept
separate for the same reason).

Not a general-purpose auth library: this implements exactly what Phase 1 of
the multi-tenant plan calls for — password login, DB-backed sessions with a
fixed TTL, and a three-role permission check — nothing more.
"""
import secrets
import time
from typing import Optional

import bcrypt

from modules import audit_store, company_store, auth_store, platform_admin_store
from modules.auth_store import User

ROLE_ADMIN = "admin"
ROLE_EMPLOYEE = "employee"
ROLE_REVIEWER = "reviewer"
ALL_ROLES = (ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_REVIEWER)

SESSION_TTL_SECONDS = 14 * 24 * 3600  # 14 days, fixed (not sliding) — Phase 1 keeps this simple

LOCKOUT_THRESHOLD = 5  # consecutive failed attempts before a lockout
LOCKOUT_SECONDS = 15 * 60  # 15 minutes


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/legacy hash — never let a crafted value raise past auth.
        return False


def _company_is_active(company_id: str) -> bool:
    company = company_store.get_company(company_id)
    return company is not None and company.status == company_store.STATUS_ACTIVE


def register_user(
    company_id: str, name: str, email: str, password: str, role: str = ROLE_EMPLOYEE,
) -> User:
    """Raises ValueError on: unknown role, duplicate (company_id, email), or
    the company's user_limit already reached. Logs a CREATE_USER audit
    event on success — `actor_user_id` identifies who performed the
    registration (e.g. the admin creating a teammate), defaulting to the
    new user's own id for the bootstrap/self-serve case."""
    if role not in ALL_ROLES:
        raise ValueError(f"Unknown role: {role!r}")

    email = email.strip().lower()
    if auth_store.get_user_by_email(company_id, email):
        raise ValueError(f"A user with email {email!r} already exists in this company.")

    company = company_store.get_company(company_id)
    if company is None:
        raise ValueError(f"No such company: {company_id!r}")
    if auth_store.count_active_users_for_company(company_id) >= company.user_limit:
        raise ValueError(f"Company {company.name!r} has reached its user limit ({company.user_limit}).")

    user = User(
        company_id=company_id, name=name.strip(), email=email,
        password_hash=hash_password(password), role=role, active=True,
    )
    auth_store.create_user(user)
    audit_store.log_audit(company_id, user.id, "CREATE_USER", "user", user.id)
    return user


def verify_login(email: str, password: str) -> Optional[User]:
    """Email is scoped per company, not globally unique (the same address
    can be a separate, unrelated account in two different companies) — so
    this checks every account matching the address rather than assuming a
    single match, and returns the first whose password matches AND whose
    user+company are both active. Known, accepted Phase-1 edge case: if two
    different companies' users share both the same email and the same
    password, whichever is checked first wins — astronomically unlikely.

    Rate limiting is tracked PER USER ROW, not per raw email string — the
    same email can belong to unrelated users in different companies, and
    one company's failed attempts must never lock out a different
    company's account that happens to share the address. A locked
    candidate's password is never even checked (so failed attempts against
    an already-locked account don't extend the lockout or leak timing), and
    only a genuinely wrong password increments the counter — a correct
    password rejected for an inactive user/company doesn't count against
    it."""
    email = email.strip().lower()
    now = time.time()
    for user in auth_store.get_users_by_email(email):
        if not user.active:
            continue
        if user.locked_until > now:
            continue
        if not verify_password(password, user.password_hash):
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= LOCKOUT_THRESHOLD:
                user.locked_until = now + LOCKOUT_SECONDS
            auth_store.update_user(user)
            continue
        if not _company_is_active(user.company_id):
            continue
        user.failed_login_attempts = 0
        user.locked_until = 0.0
        user.last_login_at = now
        auth_store.update_user(user)
        audit_store.log_audit(user.company_id, user.id, "LOGIN", "user", user.id)
        return user
    return None


def get_lockout_remaining_seconds(email: str) -> Optional[float]:
    """UI-messaging helper only, mirrors is_pending_company_login()'s
    shape below — tells the caller whether any account matching this
    email is currently locked out, and for how much longer, without
    changing verify_login()'s own return contract. Checked independently
    of the password: a locked account shows the lockout message regardless
    of what password was just typed, since only revealing it for a
    correct password would leak which password is right."""
    email = email.strip().lower()
    now = time.time()
    remaining = None
    for user in auth_store.get_users_by_email(email):
        if user.active and user.locked_until > now:
            candidate = user.locked_until - now
            if remaining is None or candidate > remaining:
                remaining = candidate
    return remaining


def create_session(user_id: str) -> str:
    auth_store.delete_expired_sessions()  # opportunistic housekeeping — no scheduler in this project
    token = secrets.token_urlsafe(32)
    auth_store.create_session_row(token, user_id, expires_at=time.time() + SESSION_TTL_SECONDS)
    return token


def validate_session(token: str) -> Optional[User]:
    """Re-reads the live user (and their company's status) on every call
    rather than trusting anything cached on the session row, so revoking a
    user or suspending their company takes effect on the very next check —
    not just once their session happens to expire."""
    if not token:
        return None
    session = auth_store.get_session(token)
    if session is None or session.expires_at < time.time():
        return None
    user = auth_store.get_user_by_id(session.user_id)
    if user is None or not user.active:
        return None
    if not _company_is_active(user.company_id):
        return None
    return user


def invalidate_session(token: str) -> None:
    session = auth_store.get_session(token) if token else None
    if session is not None:
        user = auth_store.get_user_by_id(session.user_id)
        if user is not None:
            audit_store.log_audit(user.company_id, user.id, "LOGOUT", "user", user.id)
    if token:
        auth_store.delete_session(token)


def require_role(user: User, *allowed_roles: str) -> None:
    """Raises PermissionError if user.role isn't one of allowed_roles — the
    caller (always Streamlit UI code) decides how to present that (an
    st.error + st.stop(), typically)."""
    if user.role not in allowed_roles:
        raise PermissionError(f"Role {user.role!r} is not permitted here (need one of {allowed_roles!r}).")


# ---------------------------------------------------------------------------
# Platform layer — Super Admin authorization. Deliberately layered ON TOP of
# everything above rather than woven into it: being a Super Admin is an
# independent fact about a User (a row in platform_admin_store), never a
# company-scoped `role` value, and never bypasses company_id tenant scoping
# anywhere else in the codebase. See scripts/superadmin_cli.py for the only
# sanctioned way to create/recover the first one, and app.py's "🏢
# Companies" page for the only UI surface that checks this.
# ---------------------------------------------------------------------------

def is_super_admin(user_id: str) -> bool:
    admin = platform_admin_store.get_by_user_id(user_id)
    return admin is not None and admin.is_active


def require_super_admin(user: User) -> None:
    """Raises PermissionError if the user isn't an active platform Super
    Admin — same calling convention as require_role() above."""
    if not is_super_admin(user.id):
        raise PermissionError("Super Admin access required.")


def is_pending_company_login(email: str, password: str) -> bool:
    """For a nicer login-page message only — tells the caller whether a
    failed login was specifically because the account's company is still
    PENDING approval (rather than a wrong password), WITHOUT changing
    verify_login()'s own behavior/return contract at all. Deliberately
    duplicates verify_login()'s password-check loop rather than modifying
    it — this is a read-only, UI-messaging concern, not an auth decision."""
    email = email.strip().lower()
    for user in auth_store.get_users_by_email(email):
        if not user.active or not verify_password(password, user.password_hash):
            continue
        company = company_store.get_company(user.company_id)
        if company is not None and company.status == company_store.STATUS_PENDING:
            return True
    return False
