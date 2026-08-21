"""PostgreSQL-backed company (tenant) persistence — the root of the multi-tenant
model. Every other tenant-scoped table (products, users, manifest batches,
repair events, marketplace listings) points at a row here via company_id.

Shares the same PostgreSQL database as every other modules/*_store.py
(modules/db.py, connection string via ELECTROGRADER_DATABASE_URL) but in
its own table, following the identical _connect()/CREATE TABLE IF NOT
EXISTS/CREATE INDEX IF NOT EXISTS
pattern used throughout modules/*_store.py — no ORM, no FK constraints (this
project enforces tenant scoping at the application layer everywhere, not in
the schema; see scripts/verify_tenant_isolation.py).
"""
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from modules import db

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_PENDING = "pending"  # awaiting Super Admin approval — see modules/auth.py
                             # is_pending_company_login() and app.py's Companies page.
                             # verify_login()/validate_session() already refuse
                             # any non-ACTIVE status, so a PENDING company is
                             # blocked from login with zero changes to either.

# Curated set offered in Settings -> Translation's "Default product
# language" dropdown — deliberately small to start, easy to extend later
# since actual translation is generic via any modules/translation_service.py
# provider (DeepL/OpenAI). "en" has no translation cost (it's whatever
# description_gen.py already generates); "de"/"lv" require a connected
# provider (see Company.translation_provider below).
CONTENT_LANGUAGES = {"en": "English", "de": "German", "lv": "Latviešu"}

DEFAULT_COMPANY_ID = "default"  # matches the SQLite column default already
                                 # on every pre-existing products/manifest_
                                 # batches row, so migrating existing data
                                 # means inserting one row here — nothing
                                 # else needs to change.


@dataclass
class Company:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    slug: str = ""  # url/comparison-safe form of name, auto-derived at creation
                     # if not given — see _slugify()/get_company_by_slug() below.
                     # Old rows loaded via from_dict() before this field existed
                     # just get "" (unknown-key filtering already handles this;
                     # no migration needed, same mechanism as every other
                     # dataclass field added to this JSON-blob-stored model).
    plan: str = "free"
    status: str = STATUS_ACTIVE
    user_limit: int = 5
    product_limit: int = 500
    created_at: float = field(default_factory=time.time)
    updated_at: float = 0.0

    # -- CONTENT TRANSLATION (see modules/translation_service.py) --
    default_product_language: str = "en"  # which product_translations language the
    # product card shows/generates into by default, and the fallback export
    # language for any marketplace integration that doesn't set its own
    # override (CompanyIntegration.settings["export_language"]). NOT the
    # same as a product's primary_language (modules/models.py) — that's the
    # language content was originally AI-authored in and never changes;
    # this is a display/translation-target preference that can change
    # anytime without touching existing products.
    translation_provider: str = "deepl"  # "deepl" | "openai" — which connected
    # service modules/translation_service.py uses by default.

    # -- CATEGORY CATALOG (see modules/category_store.py) --
    # Opt-in, OFF by default: when a product's status becomes "completed"
    # and it has no category_id yet (a user's own manual/AI-confirmed pick
    # always wins and is never touched), its manifest_subcategory is
    # resolved into a real Category Catalog entry (find-or-create) — see
    # app.py's _maybe_promote_manifest_category(). Manifest import itself
    # never touches the catalog; this is the only automatic path left, and
    # only ever fires for a genuinely finished product.
    auto_save_categories_from_completed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Company":
        known = {k: v for k, v in d.items() if k in Company.__dataclass_fields__}
        return Company(**known)


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY,
            created_at DOUBLE PRECISION,
            data TEXT
        )
        """
    )
    conn.commit()
    return conn


def _slugify(name: str) -> str:
    """Lowercase, non-alphanumeric runs collapsed to a single hyphen — used
    for a comparison-safe duplicate-name check (get_company_by_slug) that's
    stronger than a plain lowercase compare: "Electro Shop" and
    "electro-shop" collide here too, not just exact-case duplicates."""
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def create_company(
    name: str,
    plan: str = "free",
    status: str = STATUS_ACTIVE,
    user_limit: int = 5,
    product_limit: int = 500,
    company_id: Optional[str] = None,
    slug: Optional[str] = None,
    default_product_language: str = "",
) -> Company:
    """`company_id` is normally left auto-generated; only the migration
    script passes an explicit one (DEFAULT_COMPANY_ID), to line up with
    data already tagged company_id='default' before this table existed.
    `slug`: purely additive optional param (auto-derived from `name` if not
    given) — every existing call site keeps working unchanged.

    `default_product_language`: settable at creation, which is the only
    moment it is free. Changed later, it retroactively splits the catalog —
    every product authored before the switch has no content in the new
    language, so each one silently exports in its original language instead
    (see integrations/marketplaces/baselinker/client.py's
    _resolve_export_content()), and a marketplace ends up holding two
    unrelated sets of parameter names. Choosing it here, before the company
    owns a single product, is the one path with nothing to migrate.
    Defaults to "" meaning "leave the Company default" (currently "en"), so
    every existing caller behaves exactly as before."""
    now = time.time()
    company = Company(
        id=company_id or uuid.uuid4().hex[:12],
        name=name, slug=slug or _slugify(name), plan=plan, status=status,
        user_limit=user_limit, product_limit=product_limit,
        created_at=now, updated_at=now,
    )
    if default_product_language:
        company.default_product_language = default_product_language
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO companies (id, created_at, data) VALUES (?, ?, ?)",
            (company.id, company.created_at, json.dumps(company.to_dict())),
        )
    conn.close()
    return company


def get_company_by_slug(name_or_slug: str) -> Optional[Company]:
    """Case/separator-insensitive duplicate-name check for the company
    signup flow — see app.py's registration form. Accepts either a raw
    company name ("Electro Shop") or an already-slugified string
    ("electro-shop") interchangeably — both are run through the same
    _slugify() before comparing, so callers never need to slugify
    themselves (idempotent: slugifying an already-slugified string is a
    no-op)."""
    target = _slugify(name_or_slug)
    return next((c for c in list_companies() if c.slug == target), None)


def get_company(company_id: str) -> Optional[Company]:
    conn = _connect()
    row = conn.execute(
        "SELECT data FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return Company.from_dict(json.loads(row[0]))


def list_companies() -> List[Company]:
    conn = _connect()
    rows = conn.execute("SELECT data FROM companies ORDER BY created_at ASC").fetchall()
    conn.close()
    return [Company.from_dict(json.loads(r[0])) for r in rows]


def update_company(company: Company) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE companies SET data = ? WHERE id = ?",
            (json.dumps(company.to_dict()), company.id),
        )
    conn.close()
