"""Admin-managed category catalog — the bounded, per-company taxonomy that
replaces free-text product.category (see modules/models.py's Product's
category_id field). Categories arrive from exactly three sources: manifest
subcategory import, AI spec lookup (constrained to pick from this catalog —
see modules/spec_lookup.py), and manual Admin creation (Settings ->
Categories) — nothing else is ever allowed to invent a category.

product.category_id is the identity reference (source of truth); product
.category is a denormalized display-text copy kept in sync by this module
whenever a category is renamed, or when products are moved off a category
being deleted (see rename_category()/move_products_and_delete_category()
below). The actual product-row rewrites live in modules/inventory_store.py
(count_products_by_category/reassign_category/rename_category_display_text)
since that module owns the `products` table — this module owns `categories`
only, same one-store-one-table convention as every other modules/*_store.py.

Deletion is a REAL delete (DELETE FROM categories), not a soft is_active
flip — a category still in use must have its products moved first via
move_products_and_delete_category(). `is_active` stays on the dataclass for
a possible future archive-without-deleting affordance, but nothing here
treats it as the deletion mechanism.
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules import db

SOURCE_MANUAL = "manual"
SOURCE_MANIFEST = "manifest"
SOURCE_SYSTEM = "system"


@dataclass
class Category:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company_id: str = ""
    name: str = ""
    parent_id: str = ""  # "" = root
    is_active: bool = True
    source: str = SOURCE_MANUAL  # "manual" | "manifest" | "system"
    created_at: float = 0.0
    updated_at: float = 0.0


def _normalize(name: str) -> str:
    return " ".join((name or "").strip().casefold().split())


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            name TEXT NOT NULL,
            parent_id TEXT NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_categories_company ON categories(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(company_id, parent_id)")
    conn.commit()
    return conn


_SELECT_COLS = "id, company_id, name, parent_id, is_active, source, created_at, updated_at"


def _row_to_category(r: tuple) -> Category:
    return Category(
        id=r[0], company_id=r[1], name=r[2], parent_id=r[3] or "",
        is_active=bool(r[4]), source=r[5] or SOURCE_MANUAL,
        created_at=r[6] or 0.0, updated_at=r[7] or 0.0,
    )


def list_categories(company_id: str, include_inactive: bool = False) -> List[Category]:
    conn = _connect()
    if include_inactive:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM categories WHERE company_id = ? ORDER BY name ASC",
            (company_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM categories WHERE company_id = ? AND is_active = TRUE ORDER BY name ASC",
            (company_id,),
        ).fetchall()
    conn.close()
    return [_row_to_category(r) for r in rows]


def get_category(category_id: str, company_id: str) -> Optional[Category]:
    conn = _connect()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM categories WHERE id = ? AND company_id = ?",
        (category_id, company_id),
    ).fetchone()
    conn.close()
    return _row_to_category(row) if row else None


def find_by_name(company_id: str, name: str, parent_id: Optional[str] = None) -> Optional[Category]:
    """Normalized (trim + casefold + whitespace-collapse) exact-match
    lookup — never fuzzy. `parent_id=None` (the default) searches anywhere
    in the company regardless of nesting; pass a specific parent_id
    (including "" for root) to scope the match to that level only — see
    find_or_create_by_name() for why the two callers need different scope."""
    normalized = _normalize(name)
    if not normalized:
        return None
    for c in list_categories(company_id, include_inactive=True):
        if parent_id is not None and c.parent_id != parent_id:
            continue
        if _normalize(c.name) == normalized:
            return c
    return None


def create_category(company_id: str, name: str, parent_id: str = "", source: str = SOURCE_MANUAL) -> Category:
    name = (name or "").strip()
    if not name:
        raise ValueError("Category name is required.")
    if parent_id:
        if get_category(parent_id, company_id) is None:
            raise ValueError("Parent category not found.")
    if find_by_name(company_id, name, parent_id=parent_id) is not None:
        raise ValueError(f"A category named {name!r} already exists at this level.")

    now = time.time()
    cat = Category(
        company_id=company_id, name=name, parent_id=parent_id,
        source=source, created_at=now, updated_at=now,
    )
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO categories (id, company_id, name, parent_id, is_active, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (cat.id, cat.company_id, cat.name, cat.parent_id, cat.is_active, cat.source, cat.created_at, cat.updated_at),
        )
    conn.close()
    return cat


def rename_category(category_id: str, company_id: str, new_name: str) -> Category:
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("Category name is required.")
    cat = get_category(category_id, company_id)
    if cat is None:
        raise ValueError("Category not found.")
    existing = find_by_name(company_id, new_name, parent_id=cat.parent_id)
    if existing is not None and existing.id != cat.id:
        raise ValueError(f"A category named {new_name!r} already exists at this level.")

    cat.name = new_name
    cat.updated_at = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE categories SET name = ?, updated_at = ? WHERE id = ? AND company_id = ?",
            (cat.name, cat.updated_at, cat.id, cat.company_id),
        )
    conn.close()

    from modules import inventory_store
    inventory_store.rename_category_display_text(company_id, category_id, new_name)
    return cat


def _is_descendant(by_id: dict, ancestor_id: str, node_id: str) -> bool:
    """True if node_id IS ancestor_id, or lives anywhere under it — walks
    up node_id's own parent chain looking for ancestor_id."""
    seen = set()
    current = by_id.get(node_id)
    while current is not None:
        if current.id == ancestor_id:
            return True
        if current.id in seen:
            break  # defensive: guards against a pre-existing corrupt cycle
        seen.add(current.id)
        current = by_id.get(current.parent_id) if current.parent_id else None
    return False


def move_category(category_id: str, company_id: str, new_parent_id: str) -> Category:
    cat = get_category(category_id, company_id)
    if cat is None:
        raise ValueError("Category not found.")
    new_parent_id = new_parent_id or ""
    if new_parent_id:
        if new_parent_id == category_id:
            raise ValueError("A category cannot be its own parent.")
        if get_category(new_parent_id, company_id) is None:
            raise ValueError("Parent category not found.")
        by_id = {c.id: c for c in list_categories(company_id, include_inactive=True)}
        if _is_descendant(by_id, category_id, new_parent_id):
            raise ValueError("Cannot move a category under one of its own descendants.")
    if find_by_name(company_id, cat.name, parent_id=new_parent_id) is not None:
        raise ValueError(f"A category named {cat.name!r} already exists at the destination.")

    cat.parent_id = new_parent_id
    cat.updated_at = time.time()
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE categories SET parent_id = ?, updated_at = ? WHERE id = ? AND company_id = ?",
            (cat.parent_id, cat.updated_at, cat.id, cat.company_id),
        )
    conn.close()
    return cat


def count_products_using(category_id: str, company_id: str) -> int:
    from modules import inventory_store
    return inventory_store.count_products_by_category(company_id, category_id)


def delete_category(category_id: str, company_id: str) -> None:
    """A real delete — see this module's docstring. Raises ValueError if
    the category still has products or subcategories attached; the UI
    routes the products case to move_products_and_delete_category()
    instead of showing this error raw (see app.py's Settings -> Categories
    tab)."""
    cat = get_category(category_id, company_id)
    if cat is None:
        raise ValueError("Category not found.")
    if count_products_using(category_id, company_id) > 0:
        raise ValueError("This category still has products assigned to it.")
    children = [c for c in list_categories(company_id, include_inactive=True) if c.parent_id == category_id]
    if children:
        raise ValueError("This category still has subcategories — delete or move those first.")
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM categories WHERE id = ? AND company_id = ?", (category_id, company_id))
    conn.close()


def move_products_and_delete_category(category_id: str, company_id: str, destination_id: str) -> int:
    """The guarded two-step used when a category being deleted still has
    products: reassigns every one of them (category_id AND the display-
    text category field) to `destination_id`, then deletes the now-empty
    category. Returns the number of products moved."""
    if destination_id == category_id:
        raise ValueError("Choose a different destination category.")
    cat = get_category(category_id, company_id)
    if cat is None:
        raise ValueError("Category not found.")
    destination = get_category(destination_id, company_id)
    if destination is None:
        raise ValueError("Destination category not found.")
    if not destination.is_active:
        raise ValueError("Destination category is not active.")
    children = [c for c in list_categories(company_id, include_inactive=True) if c.parent_id == category_id]
    if children:
        raise ValueError("This category still has subcategories — move or delete those first.")

    from modules import inventory_store
    moved = inventory_store.reassign_category(company_id, category_id, destination_id, destination.name)

    if count_products_using(category_id, company_id) > 0:
        raise RuntimeError("Products still reference this category after the move — aborting delete.")

    conn = _connect()
    with conn:
        conn.execute("DELETE FROM categories WHERE id = ? AND company_id = ?", (category_id, company_id))
    conn.close()
    return moved


def find_or_create_by_name(company_id: str, name: str, parent_id: str = "", source: str = SOURCE_MANIFEST) -> Category:
    """The ONLY place a category is ever created automatically (manifest
    import, and AI category resolution — see modules/spec_lookup.py).
    Exact normalized-name match, never fuzzy — near-duplicate names
    ("Hand Blender" vs. "Immersion Blender") deliberately become separate
    entries; an admin sorts/merges them by hand in Settings -> Categories.

    For root-level callers (parent_id="" — the manifest/AI case; this
    catalog never guesses a parent from source data), matches ANY existing
    category with this name anywhere in the company, not just root ones —
    an admin may have since moved a manifest-created category under a
    parent, and re-importing the same manifest value must keep reusing
    that category rather than spawning a second root-level duplicate next
    to it."""
    match = find_by_name(company_id, name, parent_id=(parent_id if parent_id else None))
    if match is not None:
        return match
    return create_category(company_id, name, parent_id=parent_id, source=source)


def promote_from_manifest_on_completion(product) -> bool:
    """The ONLY remaining automatic Category Catalog write path (see the
    approved redesign — modules/manifest_import.py's _apply_manifest_fields
    no longer touches the catalog at import time, since most manifest
    subcategories never end up on a finished product). Called by app.py
    right before saving a product whose status just became "completed" —
    the New Item wizard's Save Item step, and the bulk Change Status
    dialog are the only two places a product's status ever becomes
    "completed"; both call this exact same function, so its behavior is
    identical regardless of which UI path triggered it.

    Mutates product.category_id/product.category in place when it acts;
    returns whether it did. No-ops unless ALL of:
      - product.status == "completed";
      - the product doesn't already have a category_id (a manual/AI-
        confirmed dropdown pick always wins and is never overridden here);
      - product.manifest_subcategory is non-empty — the sole candidate
        source; an unconfirmed AI suggestion is never auto-saved (AI is
        only ever allowed to pick an EXISTING catalog entry, see
        modules/spec_lookup.py, never to create one);
      - the product's own company has opted in via
        Company.auto_save_categories_from_completed (default False, see
        modules/company_store.py)."""
    if product.status != "completed" or product.category_id:
        return False
    if not product.manifest_subcategory or not product.manifest_subcategory.strip():
        return False

    from modules import company_store

    company = company_store.get_company(product.company_id)
    if company is None or not company.auto_save_categories_from_completed:
        return False

    category = find_or_create_by_name(
        product.company_id, product.manifest_subcategory, source=SOURCE_MANIFEST,
    )
    product.category_id = category.id
    product.category = category.name
    return True


def build_tree(categories: List[Category]) -> List[dict]:
    """Flattens into depth-first order — each entry:
    {"id", "name", "parent_id", "depth", "label"}, where `label` is the
    full "Parent / Child" breadcrumb. Pure helper, no DB access."""
    by_parent: dict = {}
    for c in categories:
        by_parent.setdefault(c.parent_id, []).append(c)
    for kids in by_parent.values():
        kids.sort(key=lambda c: c.name.lower())

    out: List[dict] = []

    def walk(parent_id: str, depth: int, parent_label: str):
        for c in by_parent.get(parent_id, []):
            label = c.name if not parent_label else f"{parent_label} / {c.name}"
            out.append({"id": c.id, "name": c.name, "parent_id": c.parent_id, "depth": depth, "label": label})
            walk(c.id, depth + 1, label)

    walk("", 0, "")
    return out
