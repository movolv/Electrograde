"""Persists manifest import batches ("shipments") as their own entity.

Every '📥 Import Manifest' action creates one ManifestBatch with a unique
id; every draft Product created from that import carries
`manifest_import_id` pointing back to it — so imported items are always
traceable to exactly which manifest file/run they came from (see
modules/inventory_store.py's list_products_by_manifest()/
delete_products_by_manifest()).

Shares the same SQLite file as modules/inventory_store.py (data/inventory.db)
but in its own table. Kept deliberately simple/flat (dataclass + a thin
CRUD layer) so future extensions — multi-manifest management, import
history, product sync — have an obvious place to add fields/functions
without restructuring what's here.
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from modules import db

STATUS_PROCESSING = "processing"
STATUS_IMPORTED = "imported"
STATUS_ERROR = "error"


@dataclass
class ManifestBatch:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    company_id: str = "default"
    filename: str = ""
    imported_at: float = field(default_factory=time.time)
    updated_at: float = 0.0  # set on replace; 0 means "never replaced"
    row_count: int = 0
    column_map: dict = field(default_factory=dict)
    status: str = STATUS_PROCESSING
    error_message: str = ""


def _connect():
    conn = db.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manifest_batches (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT 'default',
            imported_at DOUBLE PRECISION,
            data TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_manifest_batches_company ON manifest_batches(company_id)"
    )
    conn.commit()
    return conn


def save_batch(batch: ManifestBatch) -> None:
    """Upsert — used both for the initial import and every subsequent
    update (status changes, replace)."""
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO manifest_batches (id, company_id, imported_at, data) VALUES (?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   company_id = EXCLUDED.company_id, imported_at = EXCLUDED.imported_at, data = EXCLUDED.data""",
            (
                batch.id,
                batch.company_id,
                batch.imported_at,
                json.dumps(
                    {
                        "id": batch.id,
                        "company_id": batch.company_id,
                        "filename": batch.filename,
                        "imported_at": batch.imported_at,
                        "updated_at": batch.updated_at,
                        "row_count": batch.row_count,
                        "column_map": batch.column_map,
                        "status": batch.status,
                        "error_message": batch.error_message,
                    }
                ),
            ),
        )
    conn.close()


def list_batches(company_id: str) -> List[ManifestBatch]:
    conn = _connect()
    rows = conn.execute(
        "SELECT data FROM manifest_batches WHERE company_id = ? ORDER BY imported_at DESC",
        (company_id,),
    ).fetchall()
    conn.close()
    return [_from_row(r[0]) for r in rows]


def get_batch(batch_id: str, company_id: str) -> Optional[ManifestBatch]:
    conn = _connect()
    row = conn.execute(
        "SELECT data FROM manifest_batches WHERE id = ? AND company_id = ?", (batch_id, company_id)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return _from_row(row[0])


def delete_batch(batch_id: str, company_id: str) -> None:
    """Deletes only the batch record itself. Deleting the products linked
    to it (or deciding whether to) is modules/inventory_store.py's
    delete_products_by_manifest() — kept separate so callers explicitly
    choose what happens to linked products rather than it being implicit."""
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM manifest_batches WHERE id = ? AND company_id = ?", (batch_id, company_id))
    conn.close()


def _from_row(data_json: str) -> ManifestBatch:
    d = json.loads(data_json)
    known = {k: v for k, v in d.items() if k in ManifestBatch.__dataclass_fields__}
    return ManifestBatch(**known)
