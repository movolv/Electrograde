"""ElectroGrader — mobile-first PWA for fast used-electronics grading,
inventory management and Baselinker-ready export.

Run with:  streamlit run app.py
"""
import concurrent.futures
import hashlib
import io
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import anthropic
import pandas as pd
import requests
from PIL import Image, ImageOps

from modules import (
    audit_store,
    auth,
    auth_cookie,
    auth_store,
    barcode_scanner,
    catalog_import_job_store,
    company_store,
    description_gen,
    export,
    field_mapping_store,
    i18n,
    identifier_lookup,
    image_pipeline,
    integration_store,
    inventory_store,
    lookup_cache_store,
    manifest_import,
    manifest_store,
    marketplace_store,
    order_store,
    platform_admin_store,
    pricing,
    product_change_log_store,
    product_translation_store,
    pwa,
    repair_store,
    spec_lookup,
    sync_job_store,
    sync_log_store,
    sync_ownership_store,
    sync_queue_store,
    sync_rules_store,
    translation_service,
    vision_grading,
    web_search,
)
from integrations import field_registry, scheduler as sync_scheduler
from integrations.base import ConnectorActionResult
from integrations.manager import CATALOG, IntegrationManager, IntegrationNotAvailableError, IntegrationNotConnectedError
from sync import catalog_import, change_detector, engine as sync_engine, service as sync_service
from sync.status import STATUS_DISABLED, STATUS_SUCCESS
from modules.models import Product
from modules.review_table_component import review_table
from modules.esc_listener_component import esc_listener

load_dotenv()

UPLOAD_DIR = Path(__file__).resolve().parent / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = Path(__file__).resolve().parent / "static"
THUMBS_DIR = STATIC_DIR / "thumbnails"
THUMB_MAX_DIM = 96

# Drop a {integration_type}.png/.svg here later to replace the CSS wordmark
# fallback below with a real logo — no code change needed (see
# _render_integration_logo in the Settings page).
INTEGRATION_LOGOS_DIR = STATIC_DIR / "integration_logos"

# review_table()'s column config for the Product List page — moved out of
# modules/review_table_frontend/index.html (was hardcoded JS) so that same
# grid component can be reused by other pages (e.g. Orders) with their own
# columns. Order matters: desktop's must-have set (photo, sku, name,
# quantity, price) comes first, left to right; product_condition/triage/
# location/baselinker/status/date follow as additional desktop-only
# columns. _PRODUCT_TABLE_MOBILE_FIELDS hides everything else below the
# responsive breakpoint, regardless of this order.
_PRODUCT_TABLE_COLUMNS = [
    {"field": "photo_url", "headerName": "Photo", "width": 110, "minWidth": 60, "maxWidth": 400, "type": "photo"},
    {"field": "sku", "headerName": "SKU", "width": 110, "minWidth": 90},
    {"field": "name", "headerName": "Product Name", "flex": 1, "minWidth": 160},
    {"field": "brand", "headerName": "Brand", "width": 110, "minWidth": 90},
    {"field": "quantity", "headerName": "Qty", "width": 90, "minWidth": 70, "type": "numeric"},
    {"field": "price", "headerName": "Price", "width": 100, "minWidth": 80, "type": "price"},
    {"field": "product_condition", "headerName": "Product Condition", "width": 130, "minWidth": 100},
    {"field": "triage", "headerName": "Triage", "width": 140, "minWidth": 110},
    {"field": "location", "headerName": "Location", "width": 110, "minWidth": 90},
    {"field": "baselinker", "headerName": "BaseLinker", "width": 120, "minWidth": 100},
    {"field": "status", "headerName": "Status", "width": 130, "minWidth": 110},
    {"field": "date", "headerName": "Date", "width": 120, "minWidth": 100},
]
_PRODUCT_TABLE_MOBILE_FIELDS = ["photo_url", "name", "price"]

# review_table()'s column config for the Orders page — see modules/models.py's
# Order dataclass and modules/order_store.py for where these fields come from.
_ORDER_TABLE_COLUMNS = [
    {"field": "order_number", "headerName": "Number", "width": 120, "minWidth": 100},
    {"field": "customer_name", "headerName": "Customer", "flex": 1, "minWidth": 140},
    {"field": "items_summary", "headerName": "Items", "flex": 1.4, "minWidth": 180},
    {"field": "price_total", "headerName": "Price", "width": 100, "minWidth": 80, "type": "price"},
    {"field": "shipping_method", "headerName": "Shipping", "width": 130, "minWidth": 100},
    {"field": "order_date_label", "headerName": "Date", "width": 120, "minWidth": 100},
    {"field": "status_label", "headerName": "Status", "width": 130, "minWidth": 110},
    {"field": "marketplace", "headerName": "Marketplace", "width": 130, "minWidth": 100},
]
_ORDER_TABLE_MOBILE_FIELDS = ["order_number", "customer_name", "price_total"]


def _ensure_thumbnail(image_path: str) -> Path | None:
    """Generates (and disk-caches) a small JPEG thumbnail for the given
    photo, written *inside* static/ so st.column_config.ImageColumn can load
    it over HTTP (it needs a fetchable URL; local disk paths / file:// URLs
    are blocked by browsers). An OS-level link from static/ pointing at
    data/uploads was tried first and rejected: Streamlit's static file
    server resolves symlinks/junctions and checks the *resolved* path is
    still under static/, so a link pointing outside it 400s. Writing a real
    (small, resized) file under static/thumbnails/ sidesteps that entirely.
    Returns None if the source is missing or generation fails — the Photo
    column then just renders blank for that row, never a hard error."""
    src = Path(image_path)
    if not src.exists():
        return None
    try:
        rel = src.resolve().relative_to(UPLOAD_DIR)
    except (ValueError, OSError):
        return None
    thumb = THUMBS_DIR / rel.parent / f"{rel.stem}_thumb.jpg"
    try:
        if thumb.exists() and thumb.stat().st_mtime >= src.stat().st_mtime:
            return thumb
        thumb.parent.mkdir(parents=True, exist_ok=True)
        img = Image.open(src)
        img = ImageOps.exif_transpose(img) or img
        img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM))
        img.convert("RGB").save(thumb, "JPEG", quality=70)
        return thumb
    except Exception:
        return None


CARD_PHOTOS_DIR = STATIC_DIR / "card_photos"
CARD_PHOTO_MAX_DIM = 480  # worth actually looking at, unlike the 96px grid icon above


def _ensure_card_photo(image_path: str) -> Path | None:
    """Same disk-caching pattern as _ensure_thumbnail (see its docstring for
    why this writes into static/ rather than linking to data/uploads), but
    at a size meant to be looked at in the Review & Export product card
    rather than a tiny grid-row icon — kept in its own cache directory since
    it targets a different resolution than THUMBS_DIR."""
    src = Path(image_path)
    if not src.exists():
        return None
    try:
        rel = src.resolve().relative_to(UPLOAD_DIR)
    except (ValueError, OSError):
        return None
    thumb = CARD_PHOTOS_DIR / rel.parent / f"{rel.stem}_card.jpg"
    try:
        if thumb.exists() and thumb.stat().st_mtime >= src.stat().st_mtime:
            return thumb
        thumb.parent.mkdir(parents=True, exist_ok=True)
        img = Image.open(src)
        img = ImageOps.exif_transpose(img) or img
        img.thumbnail((CARD_PHOTO_MAX_DIM, CARD_PHOTO_MAX_DIM))
        img.convert("RGB").save(thumb, "JPEG", quality=85)
        return thumb
    except Exception:
        return None


def _image_static_url(path: Path) -> str | None:
    """Converts a path under STATIC_DIR into the URL it's servable at.
    Leading slash matters: this URL is used from inside the review_table
    custom component's own iframe (a different path than the main page), so
    a relative URL would resolve against the wrong base and 404."""
    try:
        rel = path.resolve().relative_to(STATIC_DIR)
    except (ValueError, OSError):
        return None
    return f"/app/static/{rel.as_posix()}"


def _record_sync_field_changes(product, diffs: dict, user_id: str) -> None:
    """Feeds the Inventory edit form's per-field diffs into
    sync/change_detector.py's generic "product field changed" event —
    the ONE call site this phase wires it into (see change_detector.py's
    own docstring for why other save paths aren't touched here). Notifies
    every connected marketplace integration for this company, never
    hardcoding "baselinker" — a future second marketplace connector needs
    no change here."""
    connected = [
        i for i in integration_store.list_integrations(product.company_id)
        if i.integration_category == integration_store.CATEGORY_MARKETPLACE
        and i.status == integration_store.STATUS_CONNECTED
    ]
    if not connected:
        return
    for field_name, (old_value, new_value) in diffs.items():
        if old_value == new_value:
            continue
        for integration in connected:
            change_detector.record_and_enqueue(
                product.company_id, product.id, integration.integration_type, field_name,
                old_value, new_value, source_system=product_change_log_store.SOURCE_ELECTROGRADER,
                changed_by=f"user:{user_id}",
            )


def _save_imported_photo(product, image_url: str) -> None:
    """catalog_import.import_catalog()'s save_image callback — the one
    place bulk import touches the filesystem, reusing the exact same
    UPLOAD_DIR/sku_folder_name/_normalize_captured_photo convention as the
    Review & Export photo-upload block. Best-effort: a single bad image
    URL is skipped rather than failing the whole product import."""
    try:
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        norm = _normalize_captured_photo(resp.content)
    except Exception:
        return
    item_dir = UPLOAD_DIR / sku_folder_name(product.sku, product.id)
    item_dir.mkdir(parents=True, exist_ok=True)
    existing_nums = [
        int(m.group(1)) for f in item_dir.glob("photo_*.jpg")
        if (m := re.match(r"photo_(\d+)\.jpg$", f.name))
    ]
    next_num = (max(existing_nums) + 1) if existing_nums else 1
    fp = item_dir / f"photo_{next_num}.jpg"
    fp.write_bytes(norm)
    product.image_paths = product.image_paths + [str(fp)]
    inventory_store.save_product(product)


def _run_import_job(company_id: str, connector_name: str, user_id: str) -> None:
    """Runs entirely inside _import_executor()'s background thread — MUST
    NEVER call any st.* function (no Streamlit "ScriptRunContext" exists in
    a plain worker thread), only DB writes via catalog_import_job_store,
    the same rule _photo_executor()'s jobs already follow. Progress is
    persisted after every item so _render_import_progress() (running in a
    real Streamlit session) can poll it independently."""
    try:
        def _progress(imported, skipped, error_count, total):
            catalog_import_job_store.update_progress(company_id, connector_name, imported, skipped, error_count)

        result = catalog_import.import_catalog(
            company_id, connector_name, user_id, save_image=_save_imported_photo, on_progress=_progress,
        )
        catalog_import_job_store.finish_job(company_id, connector_name, success=True, errors=result["errors"])
    except Exception as e:  # noqa: BLE001 - a background thread must never crash silently
        catalog_import_job_store.finish_job(company_id, connector_name, success=False, errors=[str(e)])


@st.fragment(run_every=2)
def _render_import_progress(company_id: str, connector_name: str) -> None:
    """Auto-refreshing progress display for a running background import —
    same st.fragment(run_every=...) pattern as _render_photo_gallery(). A
    fragment reruns only itself, not the whole page, so this polls without
    disturbing anything else the user is doing."""
    job = catalog_import_job_store.get_job(company_id, connector_name)
    if job is None or job.status != catalog_import_job_store.STATUS_RUNNING:
        return
    st.progress(job.imported / job.total if job.total else 0.0)
    st.caption(T("settings.importing_progress", imported=job.imported, total=job.total, skipped=job.skipped, error_count=job.error_count))


_UNSAFE_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\s]+')

# Below this, a manifest-vs-photo match is flagged to the user as suspect.
MATCH_CONFIDENCE_WARNING_THRESHOLD = 60

REVIEW_CARD_MAX_PHOTOS = 16


def _enlarge_camera_preview():
    """st.camera_input's default layout is a short preview box with plain
    controls stacked below it — on a phone, other page content above it
    pushes the live view down to where the product often isn't fully
    visible before the shot is taken. This turns it into a near-fullscreen,
    camera-app-style view: the live preview fills almost the whole
    viewport, the switch-camera control sits top-right over the image, and
    the shutter is a plain circle overlaid near the bottom of the image
    itself — instead of ordinary widget controls stacked below a small
    preview.

    Deliberately NOT `position: fixed` covering the entire viewport: this
    wizard step has no dedicated "Back" control other than the sidebar nav
    and (on step 1) a manual-entry fallback below the preview — a truly
    fullscreen fixed overlay would visually bury both with no way out
    except taking a photo. Keeping the preview a very tall block in normal
    page flow means it's still just a scroll away, while looking and
    behaving like a fullscreen camera at a glance.

    Targets the widget's own data-testid hooks (stable across Streamlit's
    internal class-name changes), since camera_input's DOM structure isn't
    ours to control directly. Height is set in both `vh` and `dvh` (the
    latter wins where supported, ignored where not) because iOS Safari's
    address bar resizes the plain viewport — `dvh` tracks the actual
    visible area instead of pushing the shutter button off-screen.
    """
    _camera_css = """
        <style>
        div[data-testid="stCameraInput"] { position: relative !important; }
        div[data-testid="stCameraInput"] > label { display: none !important; }

        div[data-testid="stCameraInputWebcamStyledBox"] {
            width: 100% !important;
            height: 88vh !important;
            height: 88dvh !important;
            max-height: 88vh !important;
            max-height: 88dvh !important;
        }
        div[data-testid="stCameraInputWebcamComponent"] video {
            width: 100% !important;
            height: 100% !important;
            object-fit: cover !important;
        }

        /* Switch-camera control: pinned top-right, over the live image */
        div[data-testid="stCameraInputSwitchButton"] {
            position: absolute !important;
            top: 1rem !important;
            right: 1rem !important;
            z-index: 10 !important;
        }
        div[data-testid="stCameraInputSwitchButton"] button {
            background: rgba(0, 0, 0, 0.45) !important;
            border-radius: 50% !important;
            width: 3rem !important;
            height: 3rem !important;
        }

        /* Shutter: Streamlit puts data-testid directly on the <button>
        itself here (unlike the other hooks above, which are on a wrapping
        <div>) — no nested "button" descendant exists, so the selector
        targets the button element directly. Reused by Streamlit for both
        "Take Photo" and, after a capture, "Clear Photo" (its label isn't
        something a Python parameter can rename); font-size: 0 collapses
        the native label/icon to nothing regardless of color, and the
        ::after below draws our own label instead, the same for both
        states. */
        button[data-testid="stCameraInputButton"] {
            position: absolute !important;
            bottom: 1.25rem !important;
            left: 50% !important;
            transform: translateX(-50%) !important;
            z-index: 10 !important;
            width: 4.5rem !important;
            height: 4.5rem !important;
            border-radius: 50% !important;
            border: 4px solid white !important;
            background: rgba(255, 255, 255, 0.25) !important;
            font-size: 0 !important;
            line-height: 0 !important;
        }
        button[data-testid="stCameraInputButton"]::after {
            content: "__TAKE_PHOTO_LABEL__" !important;
            position: absolute !important;
            top: 50% !important;
            left: 50% !important;
            transform: translate(-50%, -50%) !important;
            font-size: 0.7rem !important;
            line-height: 1.1 !important;
            color: white !important;
            font-weight: 600 !important;
            white-space: nowrap !important;
        }
        </style>
        """
    _camera_css = _camera_css.replace("__TAKE_PHOTO_LABEL__", T("new_item.take_photo_shutter"))
    st.markdown(_camera_css, unsafe_allow_html=True)


@st.cache_resource
def _photo_executor() -> concurrent.futures.ThreadPoolExecutor:
    """One shared background worker pool for photo normalization, so
    capturing the next photo doesn't have to wait for the previous one to
    finish uploading/processing — st.cache_resource keeps a single
    instance alive across reruns instead of spawning a new pool every
    script run, and it's shared by every company/user in this process, not
    just one. This is CPU-bound Pillow work (EXIF transpose + resize), not
    I/O-bound, so more workers than cores just causes contention rather
    than helping — scaled to the machine's core count instead of a fixed
    small number, capped at 8 so photo processing alone can't starve the
    main Streamlit script threads and the sync scheduler thread of CPU."""
    return concurrent.futures.ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 2)))


@st.cache_resource
def _import_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Shared background worker pool for bulk catalog imports (see
    _run_import_job() below), same st.cache_resource singleton pattern as
    _photo_executor() — one instance for the whole process, so the "Import
    N products" button can submit work and return immediately instead of
    blocking the browser session for however long the import takes.
    max_workers=4: this pool is shared across every company in a
    multi-tenant deployment, not just one — a single worker would
    serialize ALL companies' imports through one queue, so importing
    100k products for one company would make every other company wait.
    Per-(company, connector) duplicate-import prevention is a separate,
    already-existing guard (see _render_catalog_import_section, checks
    catalog_import_job_store's RUNNING status) and is unaffected by this
    number — it still only ever allows one run per company+connector."""
    return concurrent.futures.ThreadPoolExecutor(max_workers=4)


@st.cache_resource
def _lookup_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Shared background worker pool for Step 3's deep spec/identifier
    lookup (see _deep_enrich below) — same st.cache_resource singleton
    pattern as _photo_executor()/_import_executor(). This is I/O-bound work
    (waiting on DDGS/page fetches/the Claude API, not CPU), so — unlike
    _photo_executor — more workers than CPU cores doesn't cause contention;
    sized for many companies' "Fetch specs" clicks landing here concurrently
    at the 100-company scale target, same reasoning as _import_executor's
    4 (shared across every company, not just one)."""
    return concurrent.futures.ThreadPoolExecutor(max_workers=16)


@st.cache_resource
def ensure_scheduler_running() -> threading.Thread:
    """Starts the one background sync-scheduler thread for this process —
    st.cache_resource guarantees exactly one Thread ever gets created no
    matter how many concurrent sessions/reruns reach this line, the same
    mechanism as _photo_executor() above. Called unconditionally near the
    top of the script (before the login gate), not lazily from the Settings
    page, so it starts regardless of which page a user opens first.

    integrations/scheduler.py has zero Streamlit dependency of its own —
    this thread is purely today's convenience wrapper for a single-process
    deployment; the same module runs standalone via
    `python -m integrations.scheduler` with no Streamlit involved at all."""
    thread = threading.Thread(target=sync_scheduler.run_forever, daemon=True, name="sync-scheduler")
    thread.start()
    return thread


_PHOTO_MAX_DIM = 2400  # long-side cap in px — well above what any listing


def _normalize_captured_photo(raw_bytes: bytes) -> bytes:
    """Corrects orientation and caps resolution for a freshly captured/
    uploaded photo. Native camera photos (via st.file_uploader, see
    _style_photo_uploader below) store an EXIF orientation tag rather than
    physically rotating pixels — PIL (and therefore st.image, and Claude's
    vision API) ignores that tag by default, so a portrait photo taken
    normally renders sideways unless corrected here. Also downscales to
    `_PHOTO_MAX_DIM` on the long side: full sensor resolution (often
    10-12MP+, several MB) is far more than any marketplace listing needs
    and noticeably slows down upload/display/AI calls for no visible
    quality gain — still a large jump up from st.camera_input's ~400px
    output, just not needlessly huge. Done once here at capture time so
    every downstream consumer (gallery, background removal, AI grading,
    BaseLinker export) works with an already-correct image.
    """
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    if max(img.width, img.height) > _PHOTO_MAX_DIM:
        scale = _PHOTO_MAX_DIM / max(img.width, img.height)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


@st.fragment(run_every=1)
def _render_photo_gallery(product):
    """Background-processing-aware Step 2 gallery + SKU + nav controls.
    Resolves any _photo_executor jobs that finished since the last tick
    into captured_photos, shows a "Processing..." placeholder for ones
    still running, and — because this is an `st.fragment(run_every=1)` —
    reruns itself once a second independently of the rest of the page.
    That's what lets a photo finish uploading/normalizing and land in the
    gallery on its own, without the user needing to tap anything, while
    they're already back in the camera app taking the next shot.

    The SKU input and Back/Next buttons live in this same fragment (not
    just the image grid) so the "Next" button's disabled state and the
    "waiting for processing" caption also update on their own once the
    last background job finishes — otherwise they'd stay stuck showing
    stale state until some unrelated interaction forced a full rerun.
    `product` is the same object as st.session_state.product (not a
    copy), so mutating product.sku here persists normally."""
    for job_id, future in list(st.session_state.pending_photos.items()):
        if future.done():
            del st.session_state.pending_photos[job_id]
            try:
                st.session_state.captured_photos.append(future.result())
            except Exception as e:
                st.error(T("new_item.photo_processing_failed", error=e))

    photos = st.session_state.captured_photos
    pending_count = len(st.session_state.pending_photos)
    if not photos and not pending_count:
        return

    status = T("new_item.photos_captured", count=len(photos))
    if pending_count:
        status += " · " + T("new_item.still_processing", count=pending_count)
    st.write(f"**{status}**")

    cols = st.columns(4)
    for i, img_bytes in enumerate(photos):
        with cols[i % 4]:
            st.image(img_bytes, use_container_width=True)
            if i == 0:
                # Only the first (main listing) photo — this is the one
                # shown in search results/thumbnails, so it's the one
                # worth a clean white e-commerce background.
                if st.button(T("new_item.clean_background"), key="clean_bg_0", use_container_width=True):
                    with st.spinner(T("new_item.enhancing_photo")):
                        try:
                            enhanced, score, report = image_pipeline.process_image(img_bytes)
                        except image_pipeline.LowQualityImageError as e:
                            st.error(T("new_item.quality_check_failed", issues=" ".join(e.report.issues)))
                        except Exception as e:
                            st.error(T("new_item.background_removal_failed", error=e))
                        else:
                            st.session_state.captured_photos[0] = enhanced.jpeg_bytes
                            if enhanced.low_resolution:
                                st.session_state.photo0_low_res_warning = True
                            for warning in report.warnings:
                                st.toast(warning, icon="⚠️")
                            st.rerun(scope="fragment")
                if st.session_state.get("photo0_low_res_warning"):
                    st.caption(T("new_item.low_res_warning"))
            if st.button("🗑️", key=f"del_photo_{i}"):
                if i == 0:
                    st.session_state.photo0_low_res_warning = False
                st.session_state.captured_photos.pop(i)
                st.rerun(scope="fragment")

    for j in range(pending_count):
        with cols[(len(photos) + j) % 4]:
            st.info(T("new_item.processing_ellipsis"))

    st.divider()
    if product.sku:
        # Already assigned (manifest import auto-assigns SKU on import) —
        # nothing to enter here, never changed or generated by AI.
        st.write(f"**{T('common.sku')}:** {product.sku}")
        st.caption(T("new_item.sku_from_manifest"))
        sku_input = product.sku
        sku_missing = False
    else:
        sku_spacer, sku_col = st.columns([1, 1])
        with sku_col:
            sku_input = st.text_input(
                T("new_item.sku_required"),
                value=product.sku,
                placeholder=T("new_item.sku_placeholder"),
                help=T("new_item.sku_help"),
            )
            sku_missing = not sku_input.strip()
            if sku_missing:
                st.warning(T("new_item.sku_required_warning"))

    if pending_count:
        st.caption(T("new_item.waiting_for_processing"))

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button(T("common.back"), use_container_width=True):
            st.session_state.wizard_step = 1
            st.rerun()
    with col3:
        if st.button(T("common.next"), type="primary", use_container_width=True, disabled=not photos or sku_missing or bool(pending_count)):
            product.sku = sku_input.strip()
            st.session_state.wizard_step = 3
            st.rerun()


# --------------------------------------------- Step 3 Fast-First lookup --
#
# Level 0: lookup_cache_store — instant, cross-tenant cache hit.
# Level 1: _run_fast_layer — synchronous, ~1-3s target. Deterministic
#          (checksum + cross-source-agreement) EAN extraction straight from
#          already-fetched search snippets, no page fetch, no Claude.
# Level 2-4: _deep_enrich — submitted to _lookup_executor() and run in the
#          background. Calls the EXISTING, UNCHANGED find_identifiers()/
#          spec_lookup.lookup() — same queries, same page fetches, same
#          Claude calls, same fallback behavior as before this redesign.
#
# See the approved plan (purring-dancing-gem.md) for the full rationale.


@dataclass
class _FastLayerResult:
    preview: spec_lookup.SpecPreview
    has_candidate: bool
    ean_hits: list
    asin_hits: list


def _run_fast_layer(product: Product) -> _FastLayerResult:
    """LEVEL 1 — synchronous. Runs the deterministic EAN/ASIN search
    (identifier_lookup.fast_ensure_identifiers, which itself parallelizes
    its EAN vs ASIN searches) concurrently with a specs search used only
    for quick_guess()'s heuristic preview — so this whole call costs about
    one search's latency, not three run back-to-back."""
    brand = product.brand
    model = product.model or product.model_number
    product_name = product.name or product.manifest_item_description
    other_info = product.category or product.manifest_subcategory
    query_terms = " ".join(t for t in [brand, model, product_name, other_info] if t and t.strip())

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        ident_future = executor.submit(
            identifier_lookup.fast_ensure_identifiers,
            product, brand=brand, model=model, product_name=product_name, other_info=other_info,
        )
        specs_future = executor.submit(web_search.search, f"{query_terms} specifications") if query_terms else None

        ident_info = ident_future.result()
        spec_hits = specs_future.result() if specs_future is not None else []

    preview = spec_lookup.quick_guess(spec_hits or ident_info.get("ean_hits") or ident_info.get("asin_hits") or [])
    has_candidate = bool(
        preview.product_name_guess or preview.brand_guess
        or ident_info.get("ean_pending_candidate") or product.ean
    )
    return _FastLayerResult(
        preview=preview, has_candidate=has_candidate,
        ean_hits=ident_info.get("ean_hits", []), asin_hits=ident_info.get("asin_hits", []),
    )


def _deep_enrich(snapshot: dict, ean_hits: list, asin_hits: list):
    """LEVEL 2-4 — runs inside _lookup_executor()'s background thread.
    Works PURELY from the immutable `snapshot` dict — never touches a live
    Product object, so it's safe to run even after the user has moved on to
    a different item (see _resolve_lookup_enrichment for how the result is
    later, safely, applied). ean_hits/asin_hits (already fetched by Level 1)
    are accepted but not otherwise used here — spec_lookup.lookup() and
    find_identifiers() (both unchanged) redo their own full search, exactly
    as before this redesign; nothing about their search/fallback/Claude
    behavior is skipped or altered."""
    sr = spec_lookup.lookup(
        ean=snapshot["ean"] or snapshot["manifest_barcode"] or snapshot["scanned_barcode"],
        asin=snapshot["asin"],
        model_number=snapshot["model_number"],
        item_description=snapshot["manifest_item_description"],
    )
    # find_identifiers() is the existing, UNCHANGED, PURE function (does not
    # mutate its arguments) — safe to call from a background thread.
    # ensure_identifiers() (which DOES mutate a Product) is deliberately NOT
    # called here — it stays reserved for its one existing call site
    # (manifest post-import auto-lookup), unchanged.
    ident = identifier_lookup.find_identifiers(
        brand=sr.brand or snapshot["brand"],
        model=sr.model or snapshot["model"] or snapshot["model_number"],
        product_name=sr.product_name or snapshot["product_name"],
        other_info=sr.category or snapshot["category"],
        need_ean=not snapshot["ean"],
        need_asin=not snapshot["asin"],
    )
    return sr, ident


def _cache_entry_from(snapshot: dict, sr: "spec_lookup.SpecResult", ident: "identifier_lookup.IdentifierResult") -> lookup_cache_store.LookupCacheEntry:
    return lookup_cache_store.LookupCacheEntry(
        ean=ident.ean or snapshot["ean"],
        asin=ident.asin or snapshot["asin"],
        brand=sr.brand or snapshot["brand"],
        model=sr.model or snapshot["model"],
        product_name=sr.product_name or snapshot["product_name"],
        category=sr.category or snapshot["category"],
        power=sr.power,
        spec_summary=sr.spec_summary,
        box_contents=sr.box_contents,
        ean_confidence="high" if ident.ean else "",
        asin_confidence="high" if ident.asin else "",
        sources=sr.sources,
    )


def _spec_result_from_cache(cached: lookup_cache_store.LookupCacheEntry) -> "spec_lookup.SpecResult":
    return spec_lookup.SpecResult(
        product_name=cached.product_name, brand=cached.brand, model=cached.model,
        category=cached.category, power=cached.power, spec_summary=cached.spec_summary,
        box_contents=cached.box_contents, sources=["cache"],
    )


def _apply_cached_result(product: Product, cached: lookup_cache_store.LookupCacheEntry) -> None:
    """Same 'only fill blank fields' priority as everywhere else in this
    flow (NEPĀRKĀPJAMS rule 4) — a cache hit is real, previously-confirmed
    data, but still never overwrites something already on the product."""
    if not product.ean and cached.ean:
        product.ean = cached.ean
        product.ean_status = "Found"
        product.ean_source = "cache"
    if not product.asin and cached.asin:
        product.asin = cached.asin
        product.asin_status = "Found"
        product.asin_source = "cache"


def _resolve_lookup_enrichment():
    """Polls every in-flight background job in lookup_enrich_jobs (there
    can be more than one if the user moved on to a new item before an
    earlier job finished — see the lookup_enrich_jobs session_state
    comment). Called every rerun regardless of wizard step (the safety
    net), plus every second from _render_spec_lookup_status while Step 3 is
    on screen.

    UI state (lookup_stage/spec_result) is only ever touched for a job that
    still belongs to the CURRENTLY displayed product — a job left behind by
    an item the user has already saved/discarded is applied silently (DB
    reload + re-save, or dropped if never saved) without disturbing
    whatever the user is looking at now (NEPĀRKĀPJAMS rules 5 and 6)."""
    jobs = st.session_state.lookup_enrich_jobs
    if not jobs:
        return

    # Whether this call resolved a job for the product CURRENTLY on screen
    # — if so, a full rerun is forced below. Necessary specifically because
    # this function is also called every second from inside
    # _render_spec_lookup_status's own run_every tick: a fragment's
    # run_every auto-rerun is SCOPED to just that fragment, so setting
    # st.session_state.spec_result there updates the status text (which
    # lives inside the fragment) but does NOT re-execute the surrounding
    # Step 3 code that reads spec_result into the name/brand/model/EAN
    # form fields — those stayed rendered with their stale (pre-
    # resolution) values until some unrelated full rerun happened. Observed
    # live: status flipped to "Verified" but the fields under it stayed
    # blank. Forcing a full st.rerun() here (harmless to also do when
    # called from the top-level safety net, before Step 3's fields have
    # even rendered yet in that same pass) makes the whole page catch up
    # in one step.
    resolved_for_current = False

    still_pending = []
    for job in jobs:
        future = job["future"]
        if not future.done():
            still_pending.append(job)
            continue

        snap = job["snapshot"]
        current_product = st.session_state.product
        is_current = current_product.id == snap["product_id"] and current_product.company_id == snap["company_id"]

        try:
            sr, ident = future.result()
        except Exception as e:
            if is_current:
                st.session_state.lookup_stage = "done"
                st.session_state.lookup_enrich_error = str(e)
                resolved_for_current = True
            continue

        lookup_cache_store.upsert(_cache_entry_from(snap, sr, ident))

        if is_current:
            target = current_product
        else:
            # User has moved on to a different item. If this one was
            # already persisted, reload the authoritative DB copy (not
            # session state) and re-save it with the enrichment — never
            # silently lose it (NEPĀRKĀPJAMS rule 5). If it was never
            # saved, there's nothing to apply it to; the cache write above
            # is still useful for the next lookup of the same product.
            target = inventory_store.get_product(snap["product_id"], snap["company_id"])

        if target is not None:
            if not target.name:
                target.name = sr.product_name
            if not target.brand:
                target.brand = sr.brand
            if not target.model:
                target.model = sr.model
            if not target.category:
                target.category = sr.category
            if not target.power:
                target.power = sr.power
            if not target.spec_summary:
                target.spec_summary = sr.spec_summary
            if not target.box_contents:
                target.box_contents = sr.box_contents
            if not target.ean and ident.ean:
                target.ean = ident.ean
                target.ean_status = ident.ean_status
                target.ean_source = ident.ean_source
            if not target.asin and ident.asin:
                target.asin = ident.asin
                target.asin_status = ident.asin_status
                target.asin_source = ident.asin_source

            if is_current:
                if st.session_state.spec_result is None:
                    st.session_state.spec_result = sr
            else:
                inventory_store.save_product(target)

        if is_current:
            st.session_state.lookup_stage = "done"
            resolved_for_current = True

    st.session_state.lookup_enrich_jobs = still_pending

    if resolved_for_current:
        st.rerun()


def _ean_retry_search(snapshot: dict) -> "identifier_lookup.IdentifierResult":
    """Runs in _lookup_executor()'s background thread. A narrower, EAN-only
    re-attempt submitted from Step 4 ("Analyze photos") when the EAN is
    still blank after both Step 3's lookup AND the photo-decoded-barcode
    check — uses whatever brand/model/category is known BY THEN, which is
    often more accurate than what Step 3's own EAN search had to work with
    (Step 3's background enrichment has usually had time to resolve and
    correct brand/model by this point), combined with
    identifier_lookup.find_identifiers' retry_on_empty behavior."""
    return identifier_lookup.find_identifiers(
        brand=snapshot["brand"],
        model=snapshot["model"] or snapshot["model_number"],
        product_name=snapshot["product_name"],
        other_info=snapshot["category"],
        need_ean=True,
        need_asin=False,
    )


def _resolve_ean_retry():
    """Same product-identity-safety pattern as _resolve_lookup_enrichment
    (survives Next/Save/discard, reloads+resaves an already-persisted
    product, never touches a different current product), narrowed to the
    ean_retry_jobs list. Never overwrites an already-set EAN — by the time
    this resolves, that value could be a manual edit, Step 3's own result,
    or the Step 4 photo-decoded barcode, all of which take priority.
    Best-effort/silent on failure: this is already a last-resort retry, so
    a failure here just leaves the EAN exactly as it already was, with
    nothing new to tell the user."""
    jobs = st.session_state.ean_retry_jobs
    if not jobs:
        return

    resolved_for_current = False
    still_pending = []
    for job in jobs:
        future = job["future"]
        if not future.done():
            still_pending.append(job)
            continue

        snap = job["snapshot"]
        current_product = st.session_state.product
        is_current = current_product.id == snap["product_id"] and current_product.company_id == snap["company_id"]

        try:
            ident = future.result()
        except Exception:
            continue

        if ident.ean:
            lookup_cache_store.upsert(lookup_cache_store.LookupCacheEntry(
                ean=ident.ean, brand=snap["brand"], model=snap["model"],
                ean_confidence="high" if ident.ean_status == identifier_lookup.STATUS_FOUND else "",
            ))

        target = current_product if is_current else inventory_store.get_product(snap["product_id"], snap["company_id"])

        if target is not None and not target.ean and ident.ean:
            target.ean = ident.ean
            target.ean_status = ident.ean_status
            target.ean_source = ident.ean_source
            if is_current:
                resolved_for_current = True
            else:
                inventory_store.save_product(target)

    st.session_state.ean_retry_jobs = still_pending
    if resolved_for_current:
        st.rerun()


@st.fragment(run_every=1)
def _render_spec_lookup_status(product: Product):
    """Honest, non-final status while Level 2-4 runs in the background
    (NEPĀRKĀPJAMS rule 6) — same run_every=1 polling pattern as
    _render_photo_gallery.

    Called UNCONDITIONALLY on every Step 3 render (mirroring
    _render_photo_gallery, which is likewise always called and returns
    early internally) rather than only once lookup_stage != "idle". A
    fragment mounted for the FIRST time inside the very same script run
    that a full st.rerun() (from the "Fetch specs" button, outside any
    fragment) triggered was observed to never establish its client-side
    run_every auto-refresh timer — the status then froze on whatever it
    showed at that first render (e.g. "Candidate found (unconfirmed)")
    even though the background job went on to finish correctly seconds
    later; only an unrelated full rerun (e.g. a manual page reload) would
    reveal the real, already-resolved state. Mounting this fragment on
    every Step 3 visit — before lookup_stage ever leaves "idle" — gives it
    a chance to establish that timer well before any button-triggered full
    rerun happens, same as the proven-working Step 2 gallery."""
    _resolve_lookup_enrichment()
    stage = st.session_state.lookup_stage
    if stage == "idle":
        return
    if stage == "searching":
        st.info(T("new_item.stage_searching"))
    elif stage == "candidate_found":
        p = st.session_state.lookup_preview
        label = " ".join(t for t in [p.brand_guess, p.model_guess] if t) if p else ""
        st.info(T("new_item.candidate_found_with_label", label=label) if label else T("new_item.candidate_found"))
    elif stage in ("verifying", "enriching"):
        st.info(T("new_item.stage_verifying"))
    elif stage == "done":
        if st.session_state.lookup_enrich_error:
            st.warning(T("new_item.enrichment_failed", error=st.session_state.lookup_enrich_error))
        else:
            st.success(T("new_item.verified"))


def _style_photo_uploader():
    """Restyles Step 2's st.file_uploader into a single big branded
    "Take a Photo" button instead of Streamlit's default dashed dropzone +
    small grey "Browse files" button + drag-and-drop/size-limit text — none
    of that reads as an inviting call-to-action on a phone, and drag-and-
    drop is meaningless on a touchscreen anyway.

    Same technique as `_enlarge_camera_preview`'s shutter button: the
    native button label is collapsed to font-size 0 and a `::after`
    pseudo-element draws our own label instead, since the button's text
    isn't a Python-settable parameter on this widget.
    """
    _uploader_css = """
        <style>
        div[data-testid="stFileUploaderDropzoneInstructions"] {
            display: none !important;
        }
        /* The per-file name/size chip row: redundant once at least one
        photo exists, since our own gallery below already shows a
        thumbnail (and a delete button) for every captured photo. Hiding
        the individual stFileChip rows rather than their stFileChips
        container — that container also holds the "Add files" button
        (stBaseButton-borderlessIcon below), so hiding the whole
        container hides the button along with it. */
        div[data-testid="stFileChip"] {
            display: none !important;
        }
        div[data-testid="stFileChips"] {
            display: block !important;
            width: 100% !important;
        }
        section[data-testid="stFileUploaderDropzone"] {
            background: transparent !important;
            border: none !important;
            padding: 0 !important;
            display: block !important;
        }
        /* Streamlit swaps in a *different* button+wrapper once at least
        one file is already selected (a small icon-only "Add files"
        button, data-testid stBaseButton-borderlessIcon, replacing the
        empty-state stBaseButton-secondary) — both cases are styled
        identically below so the call-to-action stays equally big and
        obvious for the 2nd/3rd/... photo, not just the 1st. */
        section[data-testid="stFileUploaderDropzone"] > span,
        section[data-testid="stFileUploaderDropzone"] > div {
            display: block !important;
            width: 100% !important;
            height: auto !important;
            min-height: 3.5rem !important;
            max-height: none !important;
            overflow: visible !important;
        }
        section[data-testid="stFileUploaderDropzone"] button[data-testid="stBaseButton-secondary"],
        section[data-testid="stFileUploaderDropzone"] button[data-testid="stBaseButton-borderlessIcon"] {
            display: block !important;
            width: 100% !important;
            box-sizing: border-box !important;
            padding: 1.15rem 0.5rem !important;
            min-height: 3.5rem !important;
            max-height: none !important;
            background: #38bdf8 !important;
            border: none !important;
            border-radius: 0.75rem !important;
            font-size: 0 !important;
            line-height: 0 !important;
            position: relative !important;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25) !important;
        }
        section[data-testid="stFileUploaderDropzone"] button[data-testid="stBaseButton-secondary"] span[data-testid="stIconMaterial"],
        section[data-testid="stFileUploaderDropzone"] button[data-testid="stBaseButton-borderlessIcon"] span[data-testid="stIconMaterial"] {
            display: none !important;
        }
        section[data-testid="stFileUploaderDropzone"] button[data-testid="stBaseButton-secondary"]::after,
        section[data-testid="stFileUploaderDropzone"] button[data-testid="stBaseButton-borderlessIcon"]::after {
            position: absolute !important;
            top: 50% !important;
            left: 50% !important;
            transform: translate(-50%, -50%) !important;
            width: 90% !important;
            font-size: 1rem !important;
            line-height: 1.3 !important;
            color: #0b1120 !important;
            font-weight: 700 !important;
            white-space: normal !important;
            text-align: center !important;
        }
        section[data-testid="stFileUploaderDropzone"] button[data-testid="stBaseButton-secondary"]::after {
            content: "__TAKE_PHOTO_LABEL__" !important;
        }
        section[data-testid="stFileUploaderDropzone"] button[data-testid="stBaseButton-borderlessIcon"]::after {
            content: "__TAKE_ANOTHER_LABEL__" !important;
        }
        </style>
        """
    # CSS content: text can't come from an f-string here without escaping
    # every literal { } in the block above — substituting placeholder
    # tokens after the fact is simpler and just as safe.
    _uploader_css = (
        _uploader_css
        .replace("__TAKE_PHOTO_LABEL__", T("new_item.take_photo_label"))
        .replace("__TAKE_ANOTHER_LABEL__", T("new_item.take_another_label"))
    )
    st.markdown(_uploader_css, unsafe_allow_html=True)


def sku_folder_name(sku: str, product_id: str) -> str:
    """Filesystem-safe folder name built from the user-entered SKU.

    Falls back to the internal product id only if the sanitized SKU would be
    empty (should not normally happen since SKU is mandatory before saving).
    """
    safe = _UNSAFE_FOLDER_CHARS.sub("-", sku.strip()).strip("-.")
    return safe or product_id


st.set_page_config(
    page_title="ElectroGrader",
    page_icon="📱",
    # Every other page is deliberately narrow/mobile-first (photo capture,
    # single-column forms) — Product List (which now absorbs the old
    # Inventory, Review & Export, and CSV/Excel Export pages) and Orders
    # are the desktop-oriented, data-grid-heavy pages, so those two get the
    # wide layout (Orders' review_table() grid has 8 columns — "centered"'s
    # ~730px content width clipped the rightmost one out of view).
    # st.session_state already holds last run's
    # sidebar radio value (via its key="page" below) before the radio
    # widget itself re-renders, which is what makes a per-page layout
    # possible at all — set_page_config must be the first Streamlit call,
    # before the radio exists to read from directly.
    layout="wide" if st.session_state.get("page") in ("product_list", "orders") else "centered",
    initial_sidebar_state="collapsed",
)
pwa.inject_pwa_head()
ensure_scheduler_running()  # unconditional: starts regardless of which page/user loads first

# ---------------------------------------------------------------- language --
# Set up before auth resolves (below) so the login/register screen itself
# is translatable too, not just the app behind it — session-only until a
# real user is known (see the "_language_explicitly_set" re-adoption logic
# further down, right after current_user is resolved).
st.session_state.setdefault("language", i18n.DEFAULT_LANGUAGE)


def T(key: str, **kwargs) -> str:
    """Short call-site wrapper around i18n.t() — see modules/i18n.py's
    module docstring for the standing rule this exists to make easy to
    follow: every new UI string in this file goes through T("key"), never
    a raw literal, with both an "en" and "lv" entry added there."""
    return i18n.t(key, st.session_state.language, **kwargs)


# review_table()'s columns are module-level constants (defined above,
# before T() exists) so their headerName values stay literal English keys
# into TRANSLATIONS' "table_col.*" namespace — resolved fresh here on
# every run, after T() is available, rather than at module-definition time.
def _translated_columns(cols: list) -> list:
    return [{**c, "headerName": T(f"table_col.{c['field']}")} for c in cols]


# ---- Language selector — fixed top-right, next to Streamlit's own "⋮"
# menu/Deploy button (rendered by Streamlit's own app shell, not this
# script's normal layout flow, so a plain st.columns placement can't reach
# it — CSS position:fixed targeting this selectbox's own st-key-* container
# is what actually relocates it there; same "target by st-key-<key>" CSS
# scoping already used elsewhere in this file, e.g. the pagination buttons).
# Rendered on every page, including the pre-login screen, never per-page.
st.markdown(
    """
    <style>
    div[class*="st-key-lang_selector"] {
        position: fixed;
        top: 0.6rem;
        right: 4.2rem;
        z-index: 999999;
        width: 6.5rem;
    }
    div[class*="st-key-lang_selector"] div[data-baseweb="select"] {
        min-height: 2rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
_lang_options = list(i18n.LANGUAGES.keys())
_lang_choice = st.selectbox(
    T("topbar.language_label"),
    options=_lang_options,
    format_func=lambda code: i18n.LANGUAGES[code],
    index=_lang_options.index(st.session_state.language),
    key="lang_selector",
    label_visibility="collapsed",
)
if _lang_choice != st.session_state.language:
    st.session_state.language = _lang_choice
    st.session_state["_language_explicitly_set"] = True
    _logged_in_user = st.session_state.get("auth_user")
    if _logged_in_user is not None:
        _logged_in_user.language = _lang_choice
        auth_store.update_user(_logged_in_user)
    st.rerun()

# ------------------------------------------------------------------- auth --

def _resolve_current_user():
    """None if nobody's logged in. Checks the in-memory session first (free,
    true for every rerun within a live browser connection), then falls back
    to the session cookie (see modules/auth_cookie.py) for a brand-new
    connection — a hard refresh or a new tab — via a DB-backed session
    lookup that also re-validates the user and their company are still
    active on every check."""
    if st.session_state.get("auth_user") is not None:
        return st.session_state["auth_user"]
    token = st.context.cookies.get(auth_cookie.COOKIE_NAME)
    if token:
        user = auth.validate_session(token)
        if user is not None:
            st.session_state["auth_user"] = user
            st.session_state["auth_token"] = token
            return user
    return None


def _render_login_form():
    st.title("📱 ElectroGrader")

    mode = st.radio(
        "mode", [T("login.mode_login"), T("login.mode_register")],
        label_visibility="collapsed", horizontal=True,
    )

    if mode == T("login.mode_login"):
        st.subheader(T("login.mode_login"))
        with st.form("login_form"):
            email_in = st.text_input(T("common.email"))
            password_in = st.text_input(T("common.password"), type="password")
            if st.form_submit_button(T("login.mode_login"), type="primary", use_container_width=True):
                user = auth.verify_login(email_in, password_in)
                if user is None:
                    # A correct password for a still-PENDING company's account
                    # is a different situation than a wrong password — worth
                    # a distinct message, without changing verify_login()'s
                    # own pass/fail behavior at all (see
                    # auth.is_pending_company_login()'s docstring).
                    if auth.is_pending_company_login(email_in, password_in):
                        st.warning(T("login.pending_approval"))
                    else:
                        st.error(T("login.invalid_credentials"))
                else:
                    token = auth.create_session(user.id)
                    auth_cookie.set_session_cookie(token, auth.SESSION_TTL_SECONDS)
                    st.session_state["auth_user"] = user
                    st.session_state["auth_token"] = token
                    # The cookie is set via a small injected iframe's own script
                    # (see modules/auth_cookie.py) — an immediate st.rerun() can
                    # tear the page down before the browser has actually loaded
                    # and executed that script, silently dropping the cookie.
                    # A brief pause gives it time to run first.
                    time.sleep(0.35)
                    st.rerun()
        return

    # ---- Register a new company ----
    st.subheader(T("login.mode_register"))
    st.caption(T("login.register_caption"))
    with st.form("company_signup_form"):
        company_name_in = st.text_input(T("login.company_name"))
        plan_in = st.selectbox(
            T("login.plan"), ["Trial", "Standard"],
            format_func=lambda p: T(f"login.plan_{p.lower()}"),
            help=T("login.plan_help"),
        )
        admin_name_in = st.text_input(T("login.your_name"))
        admin_email_in = st.text_input(T("login.your_email"))
        admin_password_in = st.text_input(T("login.your_password"), type="password")
        if st.form_submit_button(T("login.register_button"), type="primary", use_container_width=True):
            if not company_name_in.strip() or not admin_name_in.strip() or not admin_email_in.strip() or not admin_password_in:
                st.error(T("login.all_fields_required"))
            elif company_store.get_company_by_slug(company_name_in):
                st.error(T("login.company_exists", name=company_name_in.strip()))
            else:
                try:
                    company = company_store.create_company(
                        name=company_name_in.strip(), plan=plan_in.lower(), status=company_store.STATUS_PENDING,
                    )
                    user = auth.register_user(
                        company_id=company.id, name=admin_name_in.strip(), email=admin_email_in.strip(),
                        password=admin_password_in, role=auth.ROLE_ADMIN,
                    )
                    audit_store.log_audit(company.id, user.id, "COMPANY_SIGNUP", "company", company.id)
                    st.success(T("login.registration_submitted"))
                except ValueError as e:
                    st.error(str(e))


current_user = _resolve_current_user()
if current_user is None:
    _render_login_form()
    st.stop()

current_company = company_store.get_company(current_user.company_id)

# ---------------------------------------------------------------- session --

def _init_state():
    defaults = {
        "wizard_step": 1,
        "product": Product(company_id=current_user.company_id),
        "captured_photos": [],       # list of raw bytes
        "pending_photos": {},        # job_id -> concurrent.futures.Future,
                                      # still-processing captures (see
                                      # _photo_executor) so the uploader
                                      # stays usable for the next shot
                                      # instead of blocking on this one
        "seen_photo_hashes": set(),  # content hashes already queued from
                                      # the Step 2 uploader — see its
                                      # comment for why this replaces
                                      # bumping the widget's key
        "photo_widget_seq": 0,       # bumped to reset st.file_uploader after use
        "camera_session_id": 0,      # bumped only on reset_wizard(); kept stable
                                     # across shots within a session so the
                                     # user's chosen facing mode (front/rear)
                                     # survives between photos
        "photo0_low_res_warning": False,  # set by the "Clean background"
                                           # button when the source photo
                                           # was too low-res to fill the
                                           # frame without blurring
        "spec_result": None,
        # Fast-First Step 3 lookup UI state — tied to whichever product is
        # CURRENTLY displayed (unlike lookup_enrich_jobs below, which
        # outlives it). "idle"|"searching"|"candidate_found"|"verifying"|
        # "enriching"|"done" — see _render_spec_lookup_status.
        "lookup_stage": "idle",
        "lookup_preview": None,     # spec_lookup.SpecPreview | None — unconfirmed, UI-only
        "lookup_enrich_error": None,
        # List of {"future": Future, "snapshot": dict} — NOT reset by
        # reset_wizard(). A background enrichment job survives moving on to
        # a different item (Next/Save/discard) so it can still finish and
        # update the right product later (or just populate the shared
        # cache) — see _resolve_lookup_enrichment. A list rather than a
        # single slot because a second lookup (for a new item) must never
        # clobber the handle to a still-running one from a previous item.
        "lookup_enrich_jobs": [],
        # Same shape/survival rules as lookup_enrich_jobs above, but for the
        # narrower Step 4 "still no EAN" background retry — see
        # _ean_retry_search/_resolve_ean_retry.
        "ean_retry_jobs": [],
        "grading_result": None,
        "price_estimate": None,
        "descriptions": None,
        # {field_key: {"source": str, "language": str, "result": str|list}} —
        # memoized live-translation cache for the New Item wizard, see
        # _wizard_translate()/_wizard_translate_list() below. Product-scoped,
        # reset on reset_wizard() same as spec_result/grading_result/descriptions.
        "wizard_translations": {},
        "wizard_translate_hint_shown": False,
        "manifest_df": None,
        "manifest_uploaded_name": None,
        # Review & Export page (merged Review + CSV/Excel Export) — single
        # shared session-state set; selection/open-card state persists
        # across pages of the paginated list (rex_selected_ids is NOT
        # limited to rex_current_page_ids), so bulk actions can span pages.
        "rex_selected_ids": set(),      # product ids checked for bulk export/download, across all pages
        "rex_open_product_id": None,    # product id whose card is open, or None for list view
        "rex_current_page_ids": [],     # ids on the currently-loaded page only, for Previous/Next within it
        "rex_export_requested": False,  # set inside the fragment, consumed outside it to open the dialog
        "rex_delete_requested": False,  # same pattern, opens the bulk-delete confirmation dialog
        "rex_bulk_edit_requested": False,  # same pattern, opens the Bulk Edit dialog
        "rex_status_requested": False,     # same pattern, opens the Change Status dialog
        "rex_translate_requested": False,  # same pattern, opens the bulk Translate dialog
        "rex_bulktranslate_preview": None,  # {"languages","provider","force","rows"} from the last Preview click
        "rex_bulkedit_preview": None,      # {"field","action","raw_value","rows"} from the last Preview click
        "rex_ops_popover_seq": 0,          # bumped to force the Operations popover closed when a dialog opens
        "rex_clear_seq": 0,             # bumped to tell the grid to deselect all (no remount needed)
        "rex_focus_id": "",             # product id the grid should scroll to/highlight on next render
        "rex_last_esc_value": None,     # last value seen from esc_listener(), to detect a new Escape press
        "rex_lightbox_index": 0,        # photo index shown in the enlarge lightbox
        "rex_page": 1,                  # current page number (1-based) of the paginated list
        "rex_search_sku": "",           # per-field search boxes, combined with AND
        "rex_search_name": "",
        "rex_search_brand": "",
        "rex_search_model": "",
        "rex_search_ean": "",
        "rex_search_asin": "",
        "rex_search_location": "",
        "rex_status_filter": "completed",  # eligibility filter: completed | all_except_draft | all
        "rex_triage_filter": "",
        "rex_condition_filter": "",
        "rex_batch_filter": "",
        "rex_exact_search": "",
        "rex_filters_open": False,      # whether the Filter Products panel is expanded
        # Orders page — same list/detail session-state shape as Review & Export above.
        "orders_open_id": None,         # order id whose detail view is open, or None for list view
        "orders_page": 1,               # current page number (1-based) of the paginated list
        "orders_search": "",
        "orders_marketplace_filter": "",
        "orders_status_filter": None,
        "orders_clear_seq": 0,          # bumped to tell the grid to deselect all (no remount needed)
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()

# Re-derived from the authenticated session on every single script run, not
# just seeded once — this is what makes company_id "never user-editable
# again" now that the free-text sidebar box is gone.
st.session_state["company_id"] = current_user.company_id
st.session_state["user_id"] = current_user.id
st.session_state["role"] = current_user.role
st.session_state["user_name"] = current_user.name

# Adopts the user's saved language preference the first time we know who
# they are, UNLESS they already explicitly picked one via the selector
# this session (pre- or post-login) — a mid-session switch must never be
# silently stomped back to whatever's saved in the DB.
if not st.session_state.get("_language_explicitly_set") and current_user.language != st.session_state.language:
    st.session_state.language = current_user.language
    st.rerun()


def reset_wizard():
    st.session_state.wizard_step = 1
    st.session_state.product = Product(company_id=st.session_state.company_id)
    st.session_state.captured_photos = []
    st.session_state.pending_photos = {}
    st.session_state.seen_photo_hashes = set()
    st.session_state.photo_widget_seq += 1
    st.session_state.camera_session_id += 1
    st.session_state.photo0_low_res_warning = False
    st.session_state.spec_result = None
    # Deliberately NOT reset here: lookup_enrich_jobs, ean_retry_jobs. A
    # background enrichment/retry started for the item just saved/
    # discarded may still be running — it must be left alone to finish and
    # resolve itself via the DB-reload path, not be silently dropped just
    # because the wizard moved on to a new item (NEPĀRKĀPJAMS rule 5).
    st.session_state.lookup_stage = "idle"
    st.session_state.lookup_preview = None
    st.session_state.lookup_enrich_error = None
    st.session_state.grading_result = None
    st.session_state.price_estimate = None
    st.session_state.descriptions = None
    st.session_state.wizard_translations = {}
    st.session_state.wizard_translate_hint_shown = False


def _ai_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _wizard_translate(field_key: str, source_text: str) -> str:
    """Live-translates one piece of AI-generated wizard text into
    `current_company.default_product_language`, memoized per `field_key`
    so re-rendering the same step on every rerun (every keystroke
    elsewhere on the page) never re-calls the provider for text that
    hasn't changed. Returns `source_text` unchanged — no provider call at
    all — when the company's default is English (the common case) or no
    translation provider is connected; a one-time hint is shown instead of
    per-field warnings. Never touches `product.*` or the database — see
    modules/translation_service.py's translate_text() for the actual
    provider call, and app.py's Save Item handler (Step 6) for where the
    accumulated cache finally gets persisted."""
    if not source_text or not current_company or current_company.default_product_language == "en":
        return source_text
    target_language = current_company.default_product_language
    cached = st.session_state.wizard_translations.get(field_key)
    if cached and cached["source"] == source_text and cached["language"] == target_language:
        return cached["result"]

    if not IntegrationManager.is_connected(current_company.id, current_company.translation_provider):
        if not st.session_state.wizard_translate_hint_shown:
            st.info(T(
                "new_item.connect_translation_provider_hint",
                language=company_store.CONTENT_LANGUAGES.get(target_language, target_language),
            ))
            st.session_state.wizard_translate_hint_shown = True
        return source_text

    try:
        result = translation_service.translate_text(
            source_text, "en", target_language, current_company.translation_provider, current_company.id,
        )
    except Exception:
        st.warning(T(
            "new_item.auto_translate_failed",
            language=company_store.CONTENT_LANGUAGES.get(target_language, target_language),
        ))
        return source_text

    st.session_state.wizard_translations[field_key] = {
        "source": source_text, "language": target_language, "result": result,
        "translated_by": current_company.translation_provider,
    }
    return result


def _wizard_translate_list(field_key: str, source_items: list) -> list:
    """List counterpart of _wizard_translate() — same memoization, one
    field_key covers the whole list (compared by value, not per-item)."""
    if not source_items or not current_company or current_company.default_product_language == "en":
        return source_items
    target_language = current_company.default_product_language
    cached = st.session_state.wizard_translations.get(field_key)
    if cached and cached["source"] == source_items and cached["language"] == target_language:
        return cached["result"]

    if not IntegrationManager.is_connected(current_company.id, current_company.translation_provider):
        if not st.session_state.wizard_translate_hint_shown:
            st.info(T(
                "new_item.connect_translation_provider_hint",
                language=company_store.CONTENT_LANGUAGES.get(target_language, target_language),
            ))
            st.session_state.wizard_translate_hint_shown = True
        return source_items

    try:
        result = translation_service.translate_list(
            source_items, "en", target_language, current_company.translation_provider, current_company.id,
        )
    except Exception:
        st.warning(T(
            "new_item.auto_translate_failed",
            language=company_store.CONTENT_LANGUAGES.get(target_language, target_language),
        ))
        return source_items

    st.session_state.wizard_translations[field_key] = {
        "source": list(source_items), "language": target_language, "result": result,
        "translated_by": current_company.translation_provider,
    }
    return result


def _wizard_commit_text(field_key: str, english_source: str, widget_value: str) -> str:
    """Called from a wizard step's Next/Save handler instead of assigning
    `product.<field> = widget_value.strip()` directly. If this field was
    shown translated (a cache entry exists for the company's current
    default language), the WIDGET's current text — which the user may
    have hand-edited — is captured back into the cache (flagged
    translated_by="manual" if it no longer matches what was auto-
    translated), and `english_source` (untouched) is what gets returned
    for `product.<field>` — the primary/English row must never be
    overwritten with translated text. If this field was never translated
    (English-default company, or provider unavailable when it was shown),
    returns `widget_value.strip()` — today's exact behavior."""
    cached = st.session_state.wizard_translations.get(field_key)
    if cached and current_company and cached["language"] == current_company.default_product_language:
        edited = widget_value.strip()
        if edited != cached["result"]:
            cached["result"] = edited
            cached["translated_by"] = "manual"
        return english_source.strip()
    return widget_value.strip()


def _wizard_commit_list(field_key: str, english_source: list, widget_lines: str) -> list:
    """List counterpart of _wizard_commit_text() — `widget_lines` is the
    raw newline-separated textarea content."""
    cached = st.session_state.wizard_translations.get(field_key)
    if cached and current_company and cached["language"] == current_company.default_product_language:
        edited = [l.strip() for l in widget_lines.splitlines() if l.strip()]
        if edited != cached["result"]:
            cached["result"] = edited
            cached["translated_by"] = "manual"
        return list(english_source)
    return [l.strip() for l in widget_lines.splitlines() if l.strip()]


def _render_column_mapping_ui(df: pd.DataFrame, key_prefix: str):
    """Shared by the initial import and the Replace flow. Returns
    (confirmed_map, missing_required_fields)."""
    auto_map = manifest_import.auto_detect_columns(list(df.columns))
    col_options = ["(none)"] + list(df.columns)
    confirmed_map = {}
    map_cols = st.columns(2)
    for i, (canonical, label) in enumerate(manifest_import.FIELD_LABELS.items()):
        default_col = auto_map.get(canonical, "(none)")
        default_idx = col_options.index(default_col) if default_col in col_options else 0
        required_marker = " *" if canonical in manifest_import.REQUIRED_FIELDS else ""
        with map_cols[i % 2]:
            picked = st.selectbox(
                f"{label}{required_marker}", col_options, index=default_idx,
                key=f"{key_prefix}_map_{canonical}",
            )
        if picked != "(none)":
            confirmed_map[canonical] = picked

    missing_required = [f for f in manifest_import.REQUIRED_FIELDS if f not in confirmed_map]
    if missing_required:
        missing_labels = [manifest_import.FIELD_LABELS[f] for f in missing_required]
        st.error(T("import_manifest.missing_mapping", labels=', '.join(missing_labels)))
    return confirmed_map, missing_required


@st.dialog(T("import_manifest.delete_batch_title"))
def _confirm_delete_batch_dialog(batch, linked_products):
    pending_count = sum(1 for p in linked_products if p.status == "draft")
    processed_count = len(linked_products) - pending_count
    st.warning(
        T("import_manifest.delete_batch_warning", filename=batch.filename, batch_id=batch.id, pending=pending_count)
        + (T("import_manifest.delete_batch_kept_note", processed=processed_count) if processed_count else "")
        + "\n\n" + T("import_manifest.cannot_be_undone")
    )
    confirm = st.checkbox(T("import_manifest.confirm_delete_checkbox"))
    col1, col2 = st.columns(2)
    with col1:
        if st.button(T("common.cancel"), use_container_width=True):
            st.rerun()
    with col2:
        if st.button(T("common.delete"), type="primary", disabled=not confirm, use_container_width=True):
            deleted_count = inventory_store.delete_products_by_manifest(
                batch.id, company_id=st.session_state.company_id
            )
            manifest_store.delete_batch(batch.id, st.session_state.company_id)
            audit_store.log_audit(st.session_state.company_id, current_user.id, "DELETE_MANIFEST", "manifest_batch", batch.id)
            st.success(T("import_manifest.deleted_batch_success", count=deleted_count))
            st.rerun()


# ------------------------------------------------------------------- nav --

st.sidebar.title("📱 ElectroGrader")
st.sidebar.caption(f"🏢 {current_company.name if current_company else current_user.company_id}")
st.sidebar.caption(f"👤 {current_user.name} · {T(f'role.{current_user.role}')}")
if st.sidebar.button(T("common.log_out"), use_container_width=True):
    auth.invalidate_session(st.session_state.get("auth_token", ""))
    auth_cookie.clear_session_cookie()
    for k in ("auth_user", "auth_token"):
        st.session_state.pop(k, None)
    time.sleep(0.35)  # let the cookie-clearing iframe script actually run first
    st.rerun()
st.sidebar.divider()

# Stable, language-independent identifiers — never rendered directly, only
# ever compared against (routing below) or looked up in PAGE_LABEL_KEYS for
# display. Introduced alongside the language selector: before this, the
# nav labels themselves (e.g. "🆕 New Item") WERE the routing values, which
# broke the instant a label's translated text changed with the language.
PAGE_NEW_ITEM = "new_item"
PAGE_IMPORT_MANIFEST = "import_manifest"
PAGE_PRODUCT_LIST = "product_list"
PAGE_ORDERS = "orders"
PAGE_MANAGE_USERS = "manage_users"
PAGE_SETTINGS = "settings"
PAGE_COMPANIES = "companies"
PAGE_LABEL_KEYS = {
    PAGE_NEW_ITEM: "nav.new_item",
    PAGE_IMPORT_MANIFEST: "nav.import_manifest",
    PAGE_PRODUCT_LIST: "nav.product_list",
    PAGE_ORDERS: "nav.orders",
    PAGE_MANAGE_USERS: "nav.manage_users",
    PAGE_SETTINGS: "nav.settings",
    PAGE_COMPANIES: "nav.companies",
}

_nav_pages = [PAGE_NEW_ITEM, PAGE_PRODUCT_LIST, PAGE_ORDERS]
if current_user.role == auth.ROLE_ADMIN:
    _nav_pages.insert(1, PAGE_IMPORT_MANIFEST)
    _nav_pages.append(PAGE_MANAGE_USERS)
    _nav_pages.append(PAGE_SETTINGS)
# Platform Super Admin — independent of current_user.role/company (see
# modules/auth.py's is_super_admin()); a user can be a plain "employee" in
# their own company and still be a platform Super Admin.
if auth.is_super_admin(current_user.id):
    _nav_pages.append(PAGE_COMPANIES)

# Programmatic cross-page navigation (e.g. the Orders page's "click a SKU
# to open its product card" link) can't just assign st.session_state.page
# directly — Streamlit raises StreamlitAPIException for writing to a
# widget-bound key AFTER that widget has already been instantiated once in
# the current run, which the *next* rerun's radio below always has been by
# the time a later button handler runs. Setting this indirection flag
# instead and consuming it here, before the radio widget exists this run,
# avoids that entirely.
if st.session_state.get("_pending_nav_page"):
    st.session_state["page"] = st.session_state.pop("_pending_nav_page")

page = st.sidebar.radio(
    T("nav.navigate_label"),
    _nav_pages,
    format_func=lambda k: T(PAGE_LABEL_KEYS[k]),
    label_visibility="collapsed",
    key="page",
)

if not _ai_configured():
    st.sidebar.warning(T("sidebar.ai_key_missing"))

# =========================================================== NEW ITEM ====
if page == PAGE_NEW_ITEM:
    st.title(T("nav.new_item"))
    steps = [T("new_item.step1"), T("new_item.step2"), T("new_item.step3"),
             T("new_item.step4"), T("new_item.step5"), T("new_item.step6")]
    st.progress((st.session_state.wizard_step - 1) / (len(steps) - 1), text=steps[st.session_state.wizard_step - 1])

    product: Product = st.session_state.product

    # Safety net: resolves any Step 3 background lookup jobs that finished
    # since the last rerun, regardless of which wizard step is on screen
    # right now — this is what lets background enrichment survive the user
    # clicking "Next" past Step 3 before it's done (NEPĀRKĀPJAMS rule 5).
    # Cheap no-op when lookup_enrich_jobs is empty.
    _resolve_lookup_enrichment()
    # Same safety net for the narrower Step 4+ EAN-only retry.
    _resolve_ean_retry()

    # ---- Step 1: Identify — from a manifest draft, or from scratch ----
    if st.session_state.wizard_step == 1:
        st.subheader(T("new_item.start_item"))
        source = st.radio(
            T("new_item.how_to_start"),
            [T("new_item.from_manifest"), T("new_item.from_scratch")],
            horizontal=False,
        )

        if source == T("new_item.from_manifest"):
            drafts = inventory_store.list_products(st.session_state.company_id, status="draft")
            if not drafts:
                st.info(T("new_item.no_pending_manifest_items"))
            else:
                search_query = st.text_input(
                    T("new_item.search_pending_items"),
                    placeholder=T("new_item.search_pending_items_placeholder"),
                )
                if search_query.strip():
                    q = search_query.strip().lower()
                    drafts = [
                        d for d in drafts
                        if q in (d.sku or "").lower()
                        or q in (d.manifest_target_no or "").lower()
                        or q in (d.asin or "").lower()
                        or q in (d.manifest_item_description or "").lower()
                    ]
                    st.caption(T("new_item.match_count", count=len(drafts)))

                if not drafts:
                    st.info(T("new_item.no_pending_items_match"))
                else:
                    options = {
                        f"SKU {d.sku or '?'} — {d.manifest_target_no or '(no target #)'} — "
                        f"ASIN {d.asin or '?'} — "
                        f"{(d.manifest_item_description or '(no description)')[:60]}": d.id
                        for d in drafts
                    }
                    chosen_label = st.selectbox(T("new_item.pick_pending_item"), list(options.keys()))
                    chosen = inventory_store.get_product(options[chosen_label], st.session_state.company_id)

                    st.write(f"**{T('common.sku')}:** {chosen.sku or '—'}")
                    st.write(f"**{T('new_item.item_description')}:** {chosen.manifest_item_description}")
                    cols = st.columns(2)
                    with cols[0]:
                        st.write(f"**ASIN:** {chosen.asin or '—'}")
                        st.write(f"**{T('new_item.ean_barcode')}:** {chosen.manifest_barcode or '—'}")
                    with cols[1]:
                        st.write(f"**{T('new_item.qty')}:** {chosen.manifest_qty}")
                        st.write(f"**{T('new_item.weight')}:** {chosen.manifest_weight_kg} kg")
                    st.caption(T("new_item.manifest_unverified_reminder"))

                    if st.button(T("new_item.use_this_item"), type="primary", use_container_width=True):
                        chosen.status = "in_progress"
                        st.session_state.product = chosen
                        st.session_state.wizard_step = 2
                        st.rerun()
        else:
            st.caption(T("new_item.scan_camera_hint"))
            _enlarge_camera_preview()
            shot = st.camera_input(T("new_item.scan_label"), key=f"barcode_cam_{st.session_state.camera_session_id}")

            decoded = []
            if shot is not None:
                decoded = barcode_scanner.decode_barcodes(shot.getvalue())
                if decoded:
                    st.success(T("new_item.decoded", codes=', '.join(decoded)))
                elif not barcode_scanner.zbar_available():
                    st.info(T("new_item.zbar_missing"))
                else:
                    st.info(T("new_item.no_barcode_detected"))

            default_val = decoded[0] if decoded else product.model_number
            manual = st.text_input(T("new_item.model_number_barcode"), value=default_val)

            col1, col2 = st.columns(2)
            with col2:
                if st.button(T("common.next"), type="primary", use_container_width=True, disabled=not manual.strip()):
                    product.model_number = manual.strip()
                    if decoded:
                        product.scanned_barcode = decoded[0]
                    # A hand-typed value here is often literally the EAN
                    # (e.g. when auto-decode isn't available and the user
                    # reads the barcode digits off the label themselves) —
                    # recognize it immediately instead of leaving it stuck
                    # in model_number where EAN search would never find it.
                    if not product.ean and identifier_lookup.looks_like_ean(product.model_number):
                        product.ean = product.model_number
                        product.ean_source = "manual entry"
                        product.ean_status = "Found"
                    product.company_id = st.session_state.company_id
                    product.status = "in_progress"
                    st.session_state.wizard_step = 2
                    st.rerun()

    # ---- Step 2: Multi-angle photo capture ----
    elif st.session_state.wizard_step == 2:
        st.subheader(T("new_item.capture_photos"))
        st.caption(T("new_item.capture_photos_caption"))

        # Deliberately st.file_uploader rather than st.camera_input: on a
        # phone this still opens the OS's own camera (tap "Take Photo" in
        # the picker sheet) with a live viewfinder exactly like any other
        # photo — it's just the phone's native camera screen rather than
        # one embedded in the page. The payoff is full sensor resolution
        # (camera_input instead grabs a frame from a low-res in-browser
        # video stream — a few hundred px on the long side, not enough to
        # background-remove or grade defects from without visible blur) and
        # the rear camera opens automatically every time, no manual switch.
        _style_photo_uploader()
        # A stable key (never bumped) is deliberate: each repeat trip
        # through "Take Photo" on a phone ADDS to this widget's own
        # accumulated file list rather than replacing it (that's how
        # Streamlit's multi-file uploader already behaves), so nothing
        # needs to be reset for the next shot to be accepted. Bumping the
        # key to reset it after each capture — the previous approach —
        # remounts the widget, and a photo submitted in the split second
        # before that remount finishes silently never reaches the app
        # (confirmed: three rapid-fire uploads, only two arrived). Content
        # hashes below dedupe against reprocessing the same files on every
        # rerun instead.
        uploaded = st.file_uploader(
            T("new_item.take_photo_or_choose"),
            type=["jpg", "jpeg", "png"],
            accept_multiple_files=True,
            key="photo_uploader",
            label_visibility="collapsed",
        )
        if uploaded:
            executor = _photo_executor()
            for f in uploaded:
                raw = f.getvalue()
                file_hash = hashlib.sha256(raw).hexdigest()
                if file_hash in st.session_state.seen_photo_hashes:
                    continue
                st.session_state.seen_photo_hashes.add(file_hash)
                if not st.session_state.captured_photos and not st.session_state.pending_photos:
                    st.session_state.photo0_low_res_warning = False
                job_id = uuid.uuid4().hex
                st.session_state.pending_photos[job_id] = executor.submit(_normalize_captured_photo, raw)

        _render_photo_gallery(product)

    # ---- Step 3: Web spec lookup (Fast-First) ----
    elif st.session_state.wizard_step == 3:
        st.subheader(T("new_item.specs_heading"))
        st.caption(T("new_item.specs_caption"))

        # Button only offered when idle/failed for THIS product — while a
        # lookup is in progress (searching/candidate_found/verifying/
        # enriching) it stays hidden so a second click can't submit a
        # duplicate background job for the same product. "done" with
        # spec_result still None means the background job errored out
        # (see _render_spec_lookup_status) — allow retrying in that case.
        show_button = st.session_state.spec_result is None and st.session_state.lookup_stage in ("idle", "done")
        if show_button:
            if st.button(T("new_item.fetch_specs"), type="primary", disabled=not _ai_configured()):
                st.session_state.lookup_stage = "searching"
                st.session_state.lookup_enrich_error = None
                cached = lookup_cache_store.get_best_match(
                    ean=product.ean or product.manifest_barcode or product.scanned_barcode,
                    asin=product.asin,
                    brand=product.brand,
                    model=product.model or product.model_number,
                    description=product.manifest_item_description,
                )
                if cached:
                    # LEVEL 0 — instant cache hit, no search at all.
                    _apply_cached_result(product, cached)
                    st.session_state.spec_result = _spec_result_from_cache(cached)
                    st.session_state.lookup_stage = "done"
                else:
                    with st.spinner(T("new_item.searching")):
                        fast = _run_fast_layer(product)
                    st.session_state.lookup_preview = fast.preview
                    st.session_state.lookup_stage = "candidate_found" if fast.has_candidate else "verifying"

                    # LEVEL 2-4 — background. The snapshot is an immutable
                    # dict, not the live `product` object, so the
                    # background thread never touches anything the user
                    # might already have moved past by the time it finishes
                    # (NEPĀRKĀPJAMS rule 5, see _resolve_lookup_enrichment).
                    snapshot = {
                        "product_id": product.id, "company_id": product.company_id,
                        "ean": product.ean, "asin": product.asin,
                        "brand": product.brand, "model": product.model,
                        "model_number": product.model_number, "product_name": product.name,
                        "manifest_barcode": product.manifest_barcode,
                        "scanned_barcode": product.scanned_barcode,
                        "manifest_item_description": product.manifest_item_description,
                        "category": product.category,
                    }
                    future = _lookup_executor().submit(_deep_enrich, snapshot, fast.ean_hits, fast.asin_hits)
                    st.session_state.lookup_enrich_jobs.append({"future": future, "snapshot": snapshot})
                st.rerun()
            st.caption(T("new_item.skip_manual_caption"))

        _render_spec_lookup_status(product)

        sr = st.session_state.spec_result
        name_val = sr.product_name if sr else product.name
        brand_val = sr.brand if sr else product.brand
        model_val = sr.model if sr else product.model
        category_val = sr.category if sr else product.category
        power_val = sr.power if sr else product.power
        spec_val_en = sr.spec_summary if sr else product.spec_summary
        spec_val = _wizard_translate("spec_summary", spec_val_en)
        box_val_en = sr.box_contents if sr and sr.box_contents else product.box_contents
        box_val = "\n".join(_wizard_translate_list("box_contents", box_val_en))

        name_in = st.text_input(T("common.product_name"), value=name_val)
        cols = st.columns(2)
        with cols[0]:
            brand_in = st.text_input(T("common.brand"), value=brand_val)
        with cols[1]:
            model_in = st.text_input(T("common.model"), value=model_val)
        cat_power_cols = st.columns(2)
        with cat_power_cols[0]:
            category_in = st.text_input(T("common.category"), value=category_val or "", placeholder=T("new_item.category_placeholder"))
        with cat_power_cols[1]:
            power_in = st.text_input(T("common.power"), value=power_val or "", placeholder="e.g. 1200W")
        spec_in = st.text_area(T("new_item.spec_summary"), value=spec_val, height=100)
        box_in = st.text_area(T("new_item.box_contents"), value=box_val, height=100)

        st.divider()
        st.markdown(f"**{T('new_item.ean_asin_id')}**")
        st.caption(T("new_item.ean_asin_caption"))
        id_cols = st.columns(2)
        with id_cols[0]:
            ean_in = st.text_input(T("new_item.ean_gtin"), value=product.ean)
            status_caption = product.ean_status or T("new_item.not_yet_checked")
            if product.ean_source:
                status_caption += f" — {product.ean_source}"
            st.caption(status_caption)
        with id_cols[1]:
            asin_in = st.text_input("ASIN", value=product.asin)
            status_caption = product.asin_status or T("new_item.not_yet_checked")
            if product.asin_source:
                status_caption += f" — {product.asin_source}"
            st.caption(status_caption)
            if product.asin_candidates:
                st.warning(T("new_item.other_asins_found", asins=', '.join(product.asin_candidates)))

        if sr and sr.sources:
            with st.expander(T("new_item.sources_used")):
                for s in sr.sources:
                    st.write(s)

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button(T("common.back"), use_container_width=True):
                st.session_state.wizard_step = 2
                st.rerun()
        with col3:
            if st.button(T("common.next"), type="primary", use_container_width=True):
                product.name = name_in.strip()
                product.brand = brand_in.strip()
                product.model = model_in.strip()
                product.category = category_in.strip()
                product.power = power_in.strip()
                product.spec_summary = _wizard_commit_text("spec_summary", spec_val_en, spec_in)
                product.box_contents = _wizard_commit_list("box_contents", box_val_en, box_in)

                if ean_in.strip() != product.ean:
                    product.ean = ean_in.strip()
                    product.ean_status = "Found" if ean_in.strip() else ""
                    product.ean_source = "manual" if ean_in.strip() else ""
                if asin_in.strip() != product.asin:
                    product.asin = asin_in.strip()
                    product.asin_status = "Found" if asin_in.strip() else ""
                    product.asin_source = "manual" if asin_in.strip() else ""
                    product.asin_candidates = []

                st.session_state.wizard_step = 4
                st.rerun()

    # ---- Step 4: Vision AI grading + manifest-vs-photo verification ----
    elif st.session_state.wizard_step == 4:
        st.subheader(T("new_item.ai_grading_heading"))

        if st.session_state.grading_result is None:
            if st.button(T("new_item.analyze_photos"), type="primary", disabled=not _ai_configured()):
                with st.spinner(T("new_item.inspecting_photos")):
                    decoded_barcodes = []
                    for img_bytes in st.session_state.captured_photos:
                        decoded_barcodes.extend(barcode_scanner.decode_barcodes(img_bytes))

                    # A barcode decoded straight from one of the item's own
                    # photos (e.g. a box/label shot among the front/back/
                    # sides photos from Step 2) is a direct physical read of
                    # the real EAN — more trustworthy than any web search
                    # result, and effectively free here since these photos
                    # are already being decoded for the manifest cross-check
                    # below. Only fills a still-blank EAN (existing data
                    # always wins), and only a checksum-valid candidate is
                    # ever accepted — pyzbar can occasionally misread a
                    # digit, and the checksum catches that the same way it
                    # catches a bad OCR'd number from a web page.
                    if not product.ean:
                        for code in decoded_barcodes:
                            if identifier_lookup.looks_like_ean(code) and identifier_lookup.validate_gtin_checksum(code):
                                product.ean = code
                                product.ean_status = identifier_lookup.STATUS_FOUND
                                product.ean_source = "scanned from photo"
                                break

                    # Still nothing — one more background attempt, now with
                    # whatever brand/model/category is known by this point
                    # (often more accurate than what was available when
                    # Step 3's own EAN search ran, since Step 3's background
                    # enrichment has usually resolved by now). Runs in
                    # _lookup_executor() same as Step 3 — doesn't block this
                    # spinner or anything after it.
                    if not product.ean:
                        already_pending = any(
                            j["snapshot"]["product_id"] == product.id and j["snapshot"]["company_id"] == product.company_id
                            for j in st.session_state.ean_retry_jobs
                        )
                        if not already_pending:
                            ean_retry_snapshot = {
                                "product_id": product.id, "company_id": product.company_id,
                                "brand": product.brand, "model": product.model,
                                "model_number": product.model_number, "product_name": product.name,
                                "category": product.category,
                            }
                            ean_retry_future = _lookup_executor().submit(_ean_retry_search, ean_retry_snapshot)
                            st.session_state.ean_retry_jobs.append({"future": ean_retry_future, "snapshot": ean_retry_snapshot})

                    try:
                        st.session_state.grading_result = vision_grading.grade_item(
                            st.session_state.captured_photos,
                            product.category,
                            product.box_contents,
                            expected_identity={
                                "brand": product.brand,
                                "model": product.model,
                                "product_name": product.name,
                                "category": product.category,
                            },
                            manifest_ean=product.manifest_barcode,
                            photo_decoded_barcodes=decoded_barcodes,
                        )
                    except (anthropic.RateLimitError, anthropic.OverloadedError):
                        st.error(T("new_item.ai_busy"))
                    except Exception as e:
                        st.error(T("new_item.photo_analysis_failed", error=e))
                if st.session_state.grading_result is not None:
                    st.rerun()
            st.caption(T("new_item.skip_assess_manually"))

        if not product.ean and any(
            j["snapshot"]["product_id"] == product.id and j["snapshot"]["company_id"] == product.company_id
            for j in st.session_state.ean_retry_jobs
        ):
            st.caption(T("new_item.ean_still_searching"))

        gr = st.session_state.grading_result
        condition_options = ["A", "B", "C", "D"]
        default_product_condition = gr.product_condition if gr and gr.product_condition in condition_options else "B"
        product_condition_in = st.selectbox(
            T("common.product_condition"), condition_options, index=condition_options.index(default_product_condition),
        )
        st.caption(vision_grading.PRODUCT_CONDITION_SCALE[product_condition_in])

        default_confidence = gr.product_condition_confidence if gr else product.product_condition_confidence
        confidence_in = st.slider(
            T("new_item.ai_confidence"),
            min_value=0,
            max_value=100,
            value=int(default_confidence),
            help=T("new_item.ai_confidence_help"),
        )
        st.caption(T("new_item.condition_confidence_caption", condition=product_condition_in, confidence=confidence_in))

        new_used_options = ["Used", "New"]
        new_used_labels = {"Used": T("new_item.used"), "New": T("new_item.new")}
        default_condition = gr.condition_type if gr and gr.condition_type in new_used_options else (
            product.condition_type if product.condition_type in new_used_options else "Used"
        )
        condition_in = st.selectbox(
            T("new_item.new_or_used"), new_used_options, index=new_used_options.index(default_condition),
            format_func=lambda v: new_used_labels[v],
        )

        default_color_en = (gr.color if gr and gr.color else "") or product.color
        default_color = _wizard_translate("color", default_color_en)
        color_in = st.text_input(
            T("common.color"), value=default_color,
            help=T("new_item.color_help"),
        )

        if gr and gr.product_condition_reasoning:
            st.info(_wizard_translate("product_condition_reasoning", gr.product_condition_reasoning))

        defects_val_en = gr.defects if gr else product.defects
        defects_val = "\n".join(_wizard_translate_list("defects", defects_val_en))
        missing_val_en = gr.missing_components if gr else product.missing_components
        missing_val = "\n".join(_wizard_translate_list("missing_components", missing_val_en))
        checklist_val_en = gr.functional_checklist if gr else product.functional_checklist
        checklist_val = "\n".join(_wizard_translate_list("functional_checklist", checklist_val_en))

        defects_in = st.text_area(T("new_item.defects_label"), value=defects_val, height=100)
        missing_in = st.text_area(T("new_item.missing_components_label"), value=missing_val, height=70)
        checklist_in = st.text_area(T("new_item.functional_checklist_label"), value=checklist_val, height=120)

        st.divider()
        st.markdown(f"**{T('new_item.manifest_vs_photo')}**")
        st.caption(T("new_item.manifest_vs_photo_caption"))

        match_options = ["YES", "NO", "UNKNOWN"]
        match_labels = {"YES": T("new_item.match_yes"), "NO": T("new_item.match_no"), "UNKNOWN": T("new_item.match_unknown")}
        default_match = gr.product_match if gr and gr.product_match in match_options else (
            product.product_match if product.product_match in match_options else "UNKNOWN"
        )
        match_in = st.selectbox(
            T("new_item.product_match"), match_options, index=match_options.index(default_match),
            format_func=lambda v: match_labels[v],
        )

        default_match_conf = gr.match_confidence if gr else product.match_confidence
        match_conf_in = st.slider(T("new_item.match_confidence"), min_value=0, max_value=100, value=int(default_match_conf))

        match_notes_val_en = gr.match_notes if gr else product.match_notes
        match_notes_val = _wizard_translate("match_notes", match_notes_val_en)
        match_notes_in = st.text_area(T("new_item.match_notes"), value=match_notes_val, height=70)

        if match_in == "NO" or match_conf_in < MATCH_CONFIDENCE_WARNING_THRESHOLD:
            st.warning(T("new_item.mismatch_warning"))

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button(T("common.back"), use_container_width=True):
                st.session_state.wizard_step = 3
                st.rerun()
        with col3:
            if st.button(T("common.next"), type="primary", use_container_width=True):
                product.product_condition = product_condition_in
                product.product_condition_confidence = int(confidence_in)
                product.product_condition_reasoning = gr.product_condition_reasoning if gr else ""
                product.condition_type = condition_in
                product.color = _wizard_commit_text("color", default_color_en, color_in)
                product.defects = _wizard_commit_list("defects", defects_val_en, defects_in)
                product.missing_components = _wizard_commit_list("missing_components", missing_val_en, missing_in)
                product.functional_checklist = _wizard_commit_list("functional_checklist", checklist_val_en, checklist_in)
                product.product_match = match_in
                product.match_confidence = int(match_conf_in)
                product.match_notes = _wizard_commit_text("match_notes", match_notes_val_en, match_notes_in)
                st.session_state.wizard_step = 5
                st.rerun()

    # ---- Step 5: Pricing + description generation ----
    elif st.session_state.wizard_step == 5:
        st.subheader(T("new_item.price_listing_copy"))

        if product.product_match == "NO" or product.match_confidence < MATCH_CONFIDENCE_WARNING_THRESHOLD:
            st.warning(T("new_item.mismatch_warning_short"))

        if st.session_state.price_estimate is None:
            if st.button(T("new_item.estimate_price")):
                with st.spinner(T("new_item.searching_comparable_prices")):
                    st.session_state.price_estimate = pricing.estimate_price(
                        f"{product.brand} {product.model} {product.name}".strip(), product.product_condition
                    )
                st.rerun()

        pe = st.session_state.price_estimate
        if pe:
            st.caption(pe.reasoning)
        default_price = (pe.suggested_price if pe and pe.suggested_price else product.price) or 0.0
        price_in = st.number_input(
            T("new_item.estimated_price"),
            min_value=0.0,
            value=float(default_price),
            step=1.0,
            help=T("new_item.estimated_price_help"),
        )

        st.divider()

        if st.session_state.descriptions is None:
            if st.button(T("new_item.generate_descriptions"), type="primary", disabled=not _ai_configured()):
                with st.spinner(T("new_item.writing_listing_copy")):
                    try:
                        st.session_state.descriptions = description_gen.generate_descriptions(
                            name=product.name,
                            brand=product.brand,
                            model=product.model,
                            category=product.category,
                            spec_summary=product.spec_summary,
                            box_contents=product.box_contents,
                            product_condition=product.product_condition,
                            condition_type=product.condition_type,
                            defects=product.defects,
                            missing_components=product.missing_components,
                        )
                    except (anthropic.RateLimitError, anthropic.OverloadedError):
                        st.error(T("new_item.ai_busy"))
                    except Exception as e:
                        st.error(T("new_item.description_generation_failed", error=e))
                if st.session_state.descriptions is not None:
                    st.rerun()
            st.caption(T("new_item.write_manually_caption"))

        desc = st.session_state.descriptions
        product_name_val_en = desc.product_name if desc else product.name
        product_name_val = _wizard_translate("name", product_name_val_en)
        product_desc_val_en = desc.product_description if desc else product.product_description
        product_desc_val = _wizard_translate("product_description", product_desc_val_en)
        condition_desc_val_en = desc.condition_description if desc else product.condition_description
        condition_desc_val = _wizard_translate("condition_description", condition_desc_val_en)

        product_name_in = st.text_input(T("new_item.listing_title"), value=product_name_val)
        product_desc_in = st.text_area(T("new_item.general_overview"), value=product_desc_val, height=150)
        condition_desc_in = st.text_area(
            T("new_item.condition_scratches_details"),
            value=condition_desc_val,
            height=150,
            max_chars=description_gen.MAX_CONDITION_DESCRIPTION_LEN,
        )

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button(T("common.back"), use_container_width=True):
                st.session_state.wizard_step = 4
                st.rerun()
        with col3:
            if st.button(T("common.next"), type="primary", use_container_width=True):
                product.price = float(price_in)
                product.price_reasoning = pe.reasoning if pe else ""
                committed_name = _wizard_commit_text("name", product_name_val_en, product_name_in)
                if committed_name:
                    product.name = committed_name
                product.product_description = _wizard_commit_text(
                    "product_description", product_desc_val_en, product_desc_in,
                )
                product.condition_description = _wizard_commit_text(
                    "condition_description", condition_desc_val_en, condition_desc_in,
                )
                st.session_state.wizard_step = 6
                st.rerun()

    # ---- Step 6: Manual-only details + review + save ----
    elif st.session_state.wizard_step == 6:
        st.subheader(T("new_item.manual_only_details"))
        st.caption(T("new_item.manual_only_caption"))

        location_in = st.text_input(T("new_item.location_shelf"), value=product.location)
        test_options = ["Not Tested", "Working", "Not Working"]
        test_labels = {
            "Not Tested": T("new_item.test_not_tested"),
            "Working": T("new_item.test_working"),
            "Not Working": T("new_item.test_not_working"),
        }
        default_test = product.functional_test_result if product.functional_test_result in test_options else "Not Tested"
        test_in = st.selectbox(
            T("new_item.functional_test_result"), test_options, index=test_options.index(default_test),
            format_func=lambda v: test_labels[v],
        )
        quantity_in = st.number_input(
            T("common.quantity"), min_value=1, value=int(product.quantity or 1), step=1,
            help=T("new_item.quantity_help"),
        )

        st.caption(T("new_item.box_dimensions_caption"))
        dim_cols = st.columns(3)
        with dim_cols[0]:
            length_in = st.number_input(T("new_item.box_length"), min_value=0.0, value=float(product.box_length_cm), step=0.5)
        with dim_cols[1]:
            width_in = st.number_input(T("new_item.box_width"), min_value=0.0, value=float(product.box_width_cm), step=0.5)
        with dim_cols[2]:
            height_in = st.number_input(T("new_item.box_height"), min_value=0.0, value=float(product.box_height_cm), step=0.5)

        price_override_in = st.number_input(
            T("new_item.final_price"),
            min_value=0.0,
            value=float(product.price),
            step=1.0,
        )

        st.divider()
        st.subheader(T("new_item.finalize_save"))
        st.write(f"**{T('common.sku')}:** {product.sku}")
        st.caption(T("new_item.sku_fixed_caption"))

        if product.product_match == "NO" or product.match_confidence < MATCH_CONFIDENCE_WARNING_THRESHOLD:
            st.warning(
                T("new_item.product_match_warning", match=product.product_match or 'UNKNOWN',
                  confidence=product.match_confidence, notes=product.match_notes)
            )

        st.write(f"**{T('new_item.summary')}**")
        st.json(
            {
                T("common.sku"): product.sku,
                T("common.name"): product.name,
                T("common.brand"): product.brand,
                T("common.model"): product.model,
                T("new_item.condition_col"): product.condition_type,
                T("common.product_condition"): product.product_condition,
                T("new_item.ai_confidence_col"): product.product_condition_confidence,
                T("new_item.product_match"): product.product_match,
                T("new_item.match_confidence_col"): product.match_confidence,
                T("common.price"): product.price,
                T("common.quantity"): int(quantity_in),
                T("new_item.photos_col"): len(st.session_state.captured_photos),
            }
        )

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button(T("common.back"), use_container_width=True):
                st.session_state.wizard_step = 5
                st.rerun()
        with col3:
            if st.button(T("new_item.save_item"), type="primary", use_container_width=True):
                product.location = location_in.strip()
                product.functional_test_result = test_in
                product.box_length_cm = float(length_in)
                product.box_width_cm = float(width_in)
                product.box_height_cm = float(height_in)
                product.price = float(price_override_in)
                product.quantity = int(quantity_in)
                product.status = "completed"
                product.review_status = "ready"
                product.company_id = st.session_state.company_id

                item_dir = UPLOAD_DIR / sku_folder_name(product.sku, product.id)
                item_dir.mkdir(parents=True, exist_ok=True)
                saved_paths = []
                for i, img_bytes in enumerate(st.session_state.captured_photos):
                    fp = item_dir / f"photo_{i+1}.jpg"
                    fp.write_bytes(img_bytes)
                    saved_paths.append(str(fp))
                product.image_paths = saved_paths

                inventory_store.save_product(
                    product,
                    translated_by="ai" if st.session_state.descriptions is not None else "manual",
                )

                # Persist whatever the wizard translated live (§3-5 of the
                # live-wizard-translation plan) as one additional row for
                # the company's default language — falls back to the
                # (untouched) English product.* value for any field the
                # cache never covered (empty source, provider unavailable
                # at that step, etc.), so the row is always complete.
                wt = st.session_state.wizard_translations
                if wt and current_company and current_company.default_product_language != product.primary_language:
                    target_lang = current_company.default_product_language
                    field_map = {"name": "title", "product_description": "description"}
                    kwargs = {
                        field_map.get(k, k): v["result"]
                        for k, v in wt.items() if v.get("language") == target_lang
                    }
                    any_manual = any(
                        v.get("translated_by") == "manual"
                        for v in wt.values() if v.get("language") == target_lang
                    )
                    product_translation_store.upsert_translation(product_translation_store.ProductTranslation(
                        product_id=product.id, company_id=product.company_id, language=target_lang,
                        title=kwargs.get("title", product.name),
                        description=kwargs.get("description", product.product_description),
                        condition_description=kwargs.get("condition_description", product.condition_description),
                        defects=kwargs.get("defects", product.defects),
                        box_contents=kwargs.get("box_contents", product.box_contents),
                        missing_components=kwargs.get("missing_components", product.missing_components),
                        spec_summary=kwargs.get("spec_summary", product.spec_summary),
                        functional_checklist=kwargs.get("functional_checklist", product.functional_checklist),
                        product_condition_reasoning=kwargs.get(
                            "product_condition_reasoning", product.product_condition_reasoning,
                        ),
                        match_notes=kwargs.get("match_notes", product.match_notes),
                        color=kwargs.get("color", product.color),
                        translated_by="manual" if any_manual else current_company.translation_provider,
                    ))

                audit_store.log_audit(product.company_id, current_user.id, "CREATE_PRODUCT", "product", product.id)
                st.success(T("new_item.saved_to_inventory", name=product.name or product.model_number))
                st.balloons()
                reset_wizard()

    st.divider()
    if st.button(T("new_item.start_over")):
        reset_wizard()
        st.rerun()

# ======================================================= IMPORT MANIFEST =
elif page == PAGE_IMPORT_MANIFEST:
    st.title(T("nav.import_manifest"))
    try:
        auth.require_role(current_user, auth.ROLE_ADMIN)
    except PermissionError:
        st.error(T("common.admins_only"))
        st.stop()
    st.caption(
        "Upload an Amazon liquidation manifest (.xlsx or .csv). Only these "
        "fields are imported: Target #, Subcategory, ASIN, EAN/Barcode, Item "
        "description, Qty, Weight (kg) — everything else (brand, model, "
        "product condition, price, descriptions...) is determined later by AI + photos, "
        "never assumed from the manifest. This is purely additive — it does "
        "not replace the existing manual scan/search flow in '🆕 New Item'."
    )

    uploaded = st.file_uploader(T("import_manifest.manifest_file"), type=["xlsx", "csv"])
    if uploaded is not None and st.session_state.manifest_uploaded_name != uploaded.name:
        df = manifest_import.read_table(uploaded.getvalue(), uploaded.name)
        st.session_state.manifest_df = df
        st.session_state.manifest_uploaded_name = uploaded.name

    df = st.session_state.manifest_df
    if df is not None:
        st.write(f"**{T('import_manifest.rows_found', count=len(df))}** {T('import_manifest.columns_in_file', columns=list(df.columns))}")
        st.markdown(f"**{T('import_manifest.confirm_mapping')}**")
        confirmed_map, missing_required = _render_column_mapping_ui(df, key_prefix="new")

        rows = manifest_import.extract_rows(df, confirmed_map)
        st.write(f"**{T('import_manifest.preview_rows', count=len(rows))}**")
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

        if st.button(T("import_manifest.import_batch_button"), type="primary", disabled=bool(missing_required) or not rows):
            batch = manifest_store.ManifestBatch(
                company_id=st.session_state.company_id,
                filename=uploaded.name if uploaded else st.session_state.manifest_uploaded_name,
                row_count=len(rows),
                column_map=confirmed_map,
                status=manifest_store.STATUS_PROCESSING,
            )
            manifest_store.save_batch(batch)  # visible as "Processing" immediately

            try:
                drafts = manifest_import.rows_to_draft_products(
                    rows, company_id=st.session_state.company_id, manifest_import_id=batch.id
                )

                skus = inventory_store.next_sku_batch(len(drafts), company_id=st.session_state.company_id)
                for d, sku in zip(drafts, skus):
                    d.sku = sku

                to_check = [d for d in drafts if identifier_lookup.needs_lookup(d)]
                if to_check and _ai_configured():
                    progress = st.progress(0.0, text=T("import_manifest.looking_up_identifiers", count=len(to_check)))
                    for i, d in enumerate(to_check):
                        identifier_lookup.ensure_identifiers(
                            d,
                            product_name=d.manifest_item_description,
                            other_info=d.manifest_subcategory,
                        )
                        progress.progress((i + 1) / len(to_check), text=T("import_manifest.checked_progress", done=i + 1, total=len(to_check)))
                    progress.empty()

                inventory_store.save_products_bulk(drafts)
                batch.status = manifest_store.STATUS_IMPORTED
                manifest_store.save_batch(batch)
                audit_store.log_audit(
                    st.session_state.company_id, current_user.id, "IMPORT_MANIFEST", "manifest_batch", batch.id,
                    f"{len(drafts)} item(s), file={batch.filename}",
                )

                st.success(T("import_manifest.batch_created", batch_id=batch.id, count=len(drafts), first_sku=skus[0], last_sku=skus[-1]))
                st.caption(T("import_manifest.process_hint"))
            except Exception as e:
                batch.status = manifest_store.STATUS_ERROR
                batch.error_message = str(e)
                manifest_store.save_batch(batch)
                st.error(T("import_manifest.import_failed", error=e))

            st.session_state.manifest_df = None
            st.session_state.manifest_uploaded_name = None

    st.divider()
    st.subheader(T("import_manifest.manifest_batches"))
    batches = manifest_store.list_batches(st.session_state.company_id)
    if not batches:
        st.caption(T("import_manifest.no_batches_yet"))
    else:
        status_badges = {
            manifest_store.STATUS_PROCESSING: T("import_manifest.status_processing"),
            manifest_store.STATUS_IMPORTED: T("import_manifest.status_imported"),
            manifest_store.STATUS_ERROR: T("import_manifest.status_error"),
        }
        for b in batches:
            linked = inventory_store.list_products_by_manifest(b.id, st.session_state.company_id)
            pending = sum(1 for p in linked if p.status == "draft")
            processed = len(linked) - pending
            badge = status_badges.get(b.status, b.status)
            uploaded_str = pd.to_datetime(b.imported_at, unit="s").strftime("%Y-%m-%d %H:%M")

            with st.container(border=True):
                st.write(f"**{b.filename}**")
                st.caption(
                    T("import_manifest.batch_summary_line", badge=badge, uploaded=uploaded_str,
                      row_count=b.row_count, linked=len(linked), pending=pending, processed=processed)
                )
                if b.status == manifest_store.STATUS_ERROR and b.error_message:
                    st.error(b.error_message)

                bcol1, bcol2, bcol3 = st.columns(3)
                with bcol1:
                    if st.button(T("import_manifest.view_button"), key=f"view_{b.id}", use_container_width=True):
                        st.session_state[f"show_view_{b.id}"] = not st.session_state.get(f"show_view_{b.id}", False)
                with bcol2:
                    if st.button(T("import_manifest.replace_button"), key=f"replace_{b.id}", use_container_width=True):
                        st.session_state[f"show_replace_{b.id}"] = not st.session_state.get(f"show_replace_{b.id}", False)
                with bcol3:
                    if st.button(T("common.delete"), key=f"delete_{b.id}", use_container_width=True):
                        _confirm_delete_batch_dialog(b, linked)

                if st.session_state.get(f"show_view_{b.id}"):
                    st.write(f"**{T('import_manifest.column_mapping_used')}**")
                    st.json(b.column_map)
                    st.write(f"**{T('import_manifest.linked_products')}**")
                    if linked:
                        view_df = pd.DataFrame(
                            [
                                {
                                    T("common.sku"): p.sku,
                                    T("common.status"): p.status,
                                    T("import_manifest.name_description_col"): p.name or p.manifest_item_description,
                                    "ASIN": p.asin,
                                    "EAN": p.manifest_barcode,
                                }
                                for p in linked
                            ]
                        )
                        st.dataframe(view_df, use_container_width=True)
                    else:
                        st.caption(T("import_manifest.no_linked_products"))

                if st.session_state.get(f"show_replace_{b.id}"):
                    st.write(f"**{T('import_manifest.upload_new_version')}**")
                    st.caption(T("import_manifest.replace_caption"))
                    replace_upload = st.file_uploader(
                        T("import_manifest.new_manifest_file"), type=["xlsx", "csv"], key=f"replace_upload_{b.id}"
                    )
                    if replace_upload is not None:
                        replace_df = manifest_import.read_table(replace_upload.getvalue(), replace_upload.name)
                        st.write(f"**{T('import_manifest.rows_found', count=len(replace_df))}**")
                        r_confirmed_map, r_missing = _render_column_mapping_ui(replace_df, key_prefix=f"replace_{b.id}")
                        r_rows = manifest_import.extract_rows(replace_df, r_confirmed_map)
                        st.write(f"**{T('import_manifest.preview_rows', count=len(r_rows))}**")
                        if r_rows:
                            st.dataframe(pd.DataFrame(r_rows), use_container_width=True)

                        if st.button(
                            T("import_manifest.confirm_replace"), type="primary", key=f"confirm_replace_{b.id}",
                            disabled=bool(r_missing) or not r_rows,
                        ):
                            try:
                                updated, new = manifest_import.sync_rows_to_products(
                                    r_rows, linked, company_id=st.session_state.company_id,
                                    manifest_import_id=b.id,
                                )
                                if new:
                                    new_skus = inventory_store.next_sku_batch(
                                        len(new), company_id=st.session_state.company_id
                                    )
                                    for d, sku in zip(new, new_skus):
                                        d.sku = sku

                                inventory_store.save_products_bulk(updated + new)
                                b.filename = replace_upload.name
                                b.row_count = len(r_rows)
                                b.column_map = r_confirmed_map
                                b.updated_at = time.time()
                                b.status = manifest_store.STATUS_IMPORTED
                                b.error_message = ""
                                manifest_store.save_batch(b)
                                audit_store.log_audit(
                                    st.session_state.company_id, current_user.id, "IMPORT_MANIFEST",
                                    "manifest_batch", b.id,
                                    f"replace: {len(updated)} updated, {len(new)} new, file={b.filename}",
                                )

                                st.success(T("import_manifest.replaced_success", updated=len(updated), new=len(new)))
                                st.session_state[f"show_replace_{b.id}"] = False
                                st.rerun()
                            except Exception as e:
                                b.status = manifest_store.STATUS_ERROR
                                b.error_message = str(e)
                                manifest_store.save_batch(b)
                                st.error(T("import_manifest.replace_failed", error=e))

# ============================================================ INVENTORY ==
elif page == PAGE_PRODUCT_LIST:
    REX_FILTER_DEFAULTS = {
        "rex_search_sku": "", "rex_search_name": "", "rex_search_brand": "",
        "rex_search_model": "", "rex_search_ean": "", "rex_search_asin": "",
        "rex_search_location": "", "rex_status_filter": "completed",
        "rex_triage_filter": "", "rex_condition_filter": "", "rex_batch_filter": "",
        "rex_exact_search": "",
    }

    st.title(T("nav.product_list"))
    st.caption(T("product_list.page_caption"))

    REVIEW_STATUS_LABELS = {
        "": T("common.all"),
        "ready": T("product_list.status_ready"),
        "edited": T("product_list.status_edited"),
        "exported": T("product_list.status_exported"),
        "failed": T("product_list.status_failed"),
    }
    CONDITION_OPTIONS = ["A", "B", "C", "D"]
    TRIAGE_LABELS = {
        "": T("common.all"),
        "testing_pending": T("product_list.triage_testing_pending"),
        "ready_for_sale": T("product_list.triage_ready_for_sale"),
        "needs_repair": T("product_list.triage_needs_repair"),
        "for_parts": T("product_list.triage_for_parts"),
        "written_off": T("product_list.triage_written_off"),
    }
    BULK_STATUS_OPTIONS = ["draft", "in_progress", "completed"]
    # Only fields that already exist on Product and make sense to overwrite
    # in bulk (never SKU/barcode/photos/AI-generated/per-product text —
    # those are always product-specific, never mass-edited).
    BULK_EDIT_FIELDS = {
        "price": T("common.price"),
        "category": T("common.category"),
        "brand": T("common.brand"),
        "product_condition": T("product_list.grade_condition"),
        "quantity": T("common.quantity"),
        "location": T("product_list.warehouse_shelf"),
    }
    PRICE_ACTIONS = {
        "set": T("product_list.set_price"), "inc_amount": T("product_list.increase_by_eur"),
        "dec_amount": T("product_list.decrease_by_eur"),
        "inc_pct": T("product_list.increase_by_pct"), "dec_pct": T("product_list.decrease_by_pct"),
    }
    QUANTITY_ACTIONS = {
        "set": T("product_list.set_quantity"), "inc_amount": T("product_list.increase_by"),
        "dec_amount": T("product_list.decrease_by"),
    }

    def _bulk_edit_compute(field: str, action: str, raw_value, current):
        """Pure — no side effects. Returns (new_value, error_or_None). Both
        the Preview table and the Apply loop call this exact function, so
        they can never disagree with each other."""
        if field == "price":
            try:
                v = float(raw_value)
            except (TypeError, ValueError):
                return None, T("product_list.invalid_number")
            new = {
                "set": v, "inc_amount": current + v, "dec_amount": current - v,
                "inc_pct": current * (1 + v / 100), "dec_pct": current * (1 - v / 100),
            }[action]
            new = round(new, 2)
            return (new, None) if new > 0 else (None, T("product_list.price_must_be_positive"))
        if field == "quantity":
            try:
                v = int(raw_value)
            except (TypeError, ValueError):
                return None, T("product_list.invalid_number")
            new = {"set": v, "inc_amount": current + v, "dec_amount": current - v}[action]
            return (new, None) if new >= 1 else (None, T("product_list.quantity_must_be_positive"))
        if field == "product_condition":
            return raw_value, None
        # category / brand / location: free-text "set" only
        v = (raw_value or "").strip()
        return (v, None) if v else (None, T("product_list.value_cannot_be_empty"))
    REX_PAGE_SIZE = 50
    # Server-side-paginated query (inventory_store.list_products_paginated) —
    # unlike the old two pages' plain list_products(company_id) + Python
    # filtering, this stays fast regardless of how many products a company
    # has, since at most one page of rows is ever loaded into Python or sent
    # to the browser's grid.
    REX_STATUS_FILTER_OPTIONS = {
        "completed": T("product_list.filter_completed_only"),
        "all_except_draft": T("product_list.filter_all_except_drafts"),
        "all": T("product_list.filter_all_incl_drafts"),
    }

    def _review_status_of(p: Product) -> str:
        return p.review_status or "ready"

    def _status_badge(p: Product) -> str:
        return T("product_list.draft_badge") if p.status == "draft" else REVIEW_STATUS_LABELS[_review_status_of(p)]

    def _rex_reset_page():
        st.session_state.rex_page = 1

    def _get_selected_products() -> list:
        """rex_selected_ids can span multiple pages of the list, so each
        selected id is looked up individually (fast, indexed by primary key)
        rather than assumed to be present in whatever page is currently
        loaded."""
        out = []
        for pid in st.session_state.rex_selected_ids:
            prod = inventory_store.get_product(pid, st.session_state.company_id)
            if prod is not None:
                out.append(prod)
        return out

    @st.dialog(T("product_list.export_dialog_title"))
    def _confirm_export_dialog(selected_products):
        # Destination is never hardcoded to BaseLinker — any connected
        # marketplace integration is a valid target (IntegrationManager.get
        # already accepts any integration_type). Same connected-integrations
        # filter as _record_sync_field_changes() above.
        connected = [
            i for i in integration_store.list_integrations(current_user.company_id)
            if i.integration_category == integration_store.CATEGORY_MARKETPLACE
            and i.status == integration_store.STATUS_CONNECTED
        ]
        if not connected:
            st.warning(T("product_list.no_connected_marketplaces"))
            if st.button(T("common.close"), use_container_width=True, key="rex_export_none_close"):
                st.rerun()
            return

        catalog_by_type = {c.integration_type: c for c in CATALOG}
        dest_options = [i.integration_type for i in connected]
        dest = st.selectbox(
            T("product_list.export_to"), dest_options,
            format_func=lambda t: catalog_by_type[t].display_name if t in catalog_by_type else t,
            key="rex_export_destination",
        )
        dest_name = catalog_by_type[dest].display_name if dest in catalog_by_type else dest

        st.write(T("product_list.export_confirm_text", count=len(selected_products), dest=dest_name))
        col1, col2 = st.columns(2)
        with col1:
            if st.button(T("common.cancel"), use_container_width=True, key="rex_export_cancel"):
                st.rerun()
        with col2:
            confirmed = st.button(
                T("product_list.export_button"), type="primary", use_container_width=True, key="rex_export_confirm"
            )
        if confirmed:
            results_box = st.container()
            progress = st.progress(0.0, text=T("product_list.starting"))
            ok_count, fail_count = 0, 0
            for i, p in enumerate(selected_products):
                progress.progress(
                    i / len(selected_products),
                    text=T("product_list.exporting_progress", sku=p.sku, done=i + 1, total=len(selected_products)),
                )
                try:
                    result = IntegrationManager.get(p.company_id, dest).export_product(p)
                except (IntegrationNotConnectedError, IntegrationNotAvailableError) as e:
                    result = ConnectorActionResult(success=False, message=str(e))
                if result.success:
                    p.review_status = "exported"
                    p.exported_at = time.time()
                    ok_count += 1
                    results_box.success(f"✅ {p.sku}: {result.message}")
                    audit_store.log_audit(
                        p.company_id, current_user.id, "EXPORT_INTEGRATION", "product", p.id,
                        f"integration={dest} external_id={result.external_id}",
                    )
                else:
                    p.review_status = "failed"
                    fail_count += 1
                    results_box.error(f"❌ {p.sku}: {result.message}")
                inventory_store.save_product(p)
                st.session_state.rex_selected_ids.discard(p.id)
            progress.progress(1.0, text=T("product_list.done"))
            integration_store.record_sync(
                current_user.company_id, dest, "bulk_export_summary",
                status=integration_store.SYNC_STATUS_SUCCESS if not fail_count else integration_store.SYNC_STATUS_ERROR,
                error_message=f"{ok_count} successful, {fail_count} failed" if fail_count else "",
            )
            st.markdown(f"**{T('product_list.exported_successfully', count=ok_count)}**")
            if fail_count:
                st.markdown(f"**{T('product_list.products_failed', count=fail_count)}**")
            if st.button(T("common.close"), use_container_width=True, key="rex_export_done"):
                st.session_state.rex_clear_seq += 1  # tell the grid to deselect all rows
                st.rerun()

    @st.dialog(T("product_list.delete_dialog_title"))
    def _confirm_delete_dialog(selected_products):
        st.warning(T("product_list.delete_confirm_text", count=len(selected_products)))
        col1, col2 = st.columns(2)
        with col1:
            if st.button(T("common.cancel"), use_container_width=True, key="rex_delete_cancel"):
                st.rerun()
        with col2:
            confirmed = st.button(
                T("common.delete"), type="primary", use_container_width=True, key="rex_delete_confirm"
            )
        if confirmed:
            for p in selected_products:
                inventory_store.delete_product(p.id, p.company_id)
                audit_store.log_audit(p.company_id, current_user.id, "DELETE_PRODUCT", "product", p.id)
                st.session_state.rex_selected_ids.discard(p.id)
            st.session_state.rex_clear_seq += 1  # tell the grid to deselect all rows
            st.success(T("product_list.deleted_success", count=len(selected_products)))
            if st.button(T("common.close"), use_container_width=True, key="rex_delete_done"):
                st.rerun()

    @st.dialog(T("product_list.bulk_edit_title"))
    def _confirm_bulk_edit_dialog(selected_products):
        st.write(f"**{T('product_list.bulk_edit_heading', count=len(selected_products))}**")
        field = st.selectbox(
            T("product_list.field_to_edit"), list(BULK_EDIT_FIELDS), format_func=lambda k: BULK_EDIT_FIELDS[k],
            key="rex_bulkedit_field",
        )

        # Each control is keyed by field, so switching the field dropdown
        # always lands on a fresh widget (no stale value carried over from
        # a previous field) without any manual reset code.
        if field in ("price", "quantity"):
            actions = PRICE_ACTIONS if field == "price" else QUANTITY_ACTIONS
            action = st.selectbox(
                T("product_list.action_label"), list(actions), format_func=lambda k: actions[k],
                key=f"rex_bulkedit_action_{field}",
            )
            raw_value = st.number_input(
                T("product_list.value_label"), step=0.5 if field == "price" else 1,
                key=f"rex_bulkedit_value_{field}",
            )
        elif field == "product_condition":
            action = "set"
            raw_value = st.selectbox(T("product_list.new_grade"), CONDITION_OPTIONS, key=f"rex_bulkedit_value_{field}")
        else:  # category / brand / location
            action = "set"
            raw_value = st.text_input(T("product_list.new_value"), key=f"rex_bulkedit_value_{field}")

        col1, col2 = st.columns(2)
        with col1:
            if st.button(T("common.cancel"), use_container_width=True, key="rex_bulkedit_cancel"):
                st.session_state.rex_bulkedit_preview = None
                st.rerun()
        with col2:
            if st.button(T("product_list.preview_changes"), type="primary", use_container_width=True, key="rex_bulkedit_preview_btn"):
                rows = []
                for p in selected_products:
                    current = getattr(p, field)
                    new_value, error = _bulk_edit_compute(field, action, raw_value, current)
                    rows.append({
                        "product_id": p.id, "sku": p.sku, "name": p.name,
                        "current": current, "new_value": new_value, "error": error,
                    })
                st.session_state.rex_bulkedit_preview = {
                    "field": field, "action": action, "raw_value": raw_value, "rows": rows,
                }
                st.rerun()

        preview = st.session_state.get("rex_bulkedit_preview")
        # Only trust a preview that matches exactly what's currently
        # selected above — if the user tweaked the field/action/value after
        # previewing, the stale preview is hidden until they preview again,
        # so Apply can never run against an out-of-date computation.
        if preview and preview["field"] == field and preview["action"] == action and preview["raw_value"] == raw_value:
            n_errors = sum(1 for r in preview["rows"] if r["error"])
            st.dataframe(
                [
                    {
                        T("product_list.product_col"): r["sku"] or r["name"] or r["product_id"],
                        T("product_list.current_col"): r["current"],
                        T("product_list.new_col"): r["new_value"] if not r["error"] else f"⚠ {r['error']}",
                    }
                    for r in preview["rows"]
                ],
                hide_index=True, use_container_width=True,
            )
            if n_errors:
                st.caption(T("product_list.skipped_due_to_errors", count=n_errors))

            acol1, acol2 = st.columns(2)
            with acol1:
                if st.button(T("common.cancel"), use_container_width=True, key="rex_bulkedit_preview_cancel"):
                    st.session_state.rex_bulkedit_preview = None
                    st.rerun()
            with acol2:
                apply_clicked = st.button(
                    T("product_list.apply_changes"), type="primary", use_container_width=True, key="rex_bulkedit_apply",
                )
            if apply_clicked:
                by_id = {p.id: p for p in selected_products}
                ok_count, fail_count = 0, 0
                for r in preview["rows"]:
                    if r["error"]:
                        fail_count += 1
                        continue
                    p = by_id[r["product_id"]]
                    old_value = getattr(p, field)
                    setattr(p, field, r["new_value"])
                    if field != "location":
                        _record_sync_field_changes(p, {field: (old_value, r["new_value"])}, current_user.id)
                    inventory_store.save_product(p)
                    audit_store.log_audit(
                        p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id,
                        f"{field} -> {r['new_value']} (bulk edit)",
                    )
                    ok_count += 1
                st.session_state.rex_bulkedit_preview = None
                st.session_state.rex_clear_seq += 1
                if fail_count:
                    st.success(T("product_list.bulk_edit_partial_success", ok=ok_count, fail=fail_count))
                else:
                    st.success(T("product_list.bulk_edit_success", count=ok_count))
                if st.button(T("common.close"), use_container_width=True, key="rex_bulkedit_done"):
                    st.rerun()

    _BULK_TRANSLATE_MAX_PRODUCTS = 300  # v1 runs synchronously in one request; see
    # integrations/scheduler.py / sync_queue_store.py for the background-job
    # pattern this should move to once real usage shows this cap is too low.

    def _bulk_translate_classify(product, language: str, force: bool) -> str:
        """create | update | skip_manual | skip_primary — computed from
        existing DB state only, no provider call, so Preview never spends
        API credits (mirrors _confirm_bulk_edit_dialog's preview/apply
        split above)."""
        if language == product.primary_language:
            return "skip_primary"
        existing = product_translation_store.get_translation(product.id, language)
        if existing is None:
            return "create"
        if existing.translated_by == "manual" and not force:
            return "skip_manual"
        return "update"

    @st.dialog(T("product_list.bulk_translate_title"))
    def _confirm_bulk_translate_dialog(selected_products):
        st.write(f"**{T('product_list.bulk_translate_heading', count=len(selected_products))}**")
        if len(selected_products) > _BULK_TRANSLATE_MAX_PRODUCTS:
            st.warning(T("product_list.bulk_translate_too_many", max=_BULK_TRANSLATE_MAX_PRODUCTS))
            return

        target_langs = st.multiselect(
            T("product_list.translate_target_languages"),
            options=list(company_store.CONTENT_LANGUAGES.keys()),
            format_func=lambda code: company_store.CONTENT_LANGUAGES[code],
            key="rex_bulktranslate_langs",
        )
        provider = st.radio(
            T("settings.translation_provider"), options=["deepl", "openai"],
            format_func=lambda p: {"deepl": "DeepL", "openai": "OpenAI"}[p],
            index=["deepl", "openai"].index(current_company.translation_provider)
            if current_company and current_company.translation_provider in ("deepl", "openai") else 0,
            key="rex_bulktranslate_provider", horizontal=True,
        )
        force = st.checkbox(T("product_list.retranslate_force"), key="rex_bulktranslate_force")

        col1, col2 = st.columns(2)
        with col1:
            if st.button(T("common.cancel"), use_container_width=True, key="rex_bulktranslate_cancel"):
                st.session_state.rex_bulktranslate_preview = None
                st.rerun()
        with col2:
            if st.button(
                T("product_list.preview_changes"), type="primary", use_container_width=True,
                key="rex_bulktranslate_preview_btn",
            ):
                rows = []
                for p in selected_products:
                    for lang in target_langs:
                        rows.append({
                            "product_id": p.id, "sku": p.sku, "name": p.name,
                            "language": lang, "action": _bulk_translate_classify(p, lang, force),
                        })
                st.session_state.rex_bulktranslate_preview = {
                    "languages": target_langs, "provider": provider, "force": force, "rows": rows,
                }
                st.rerun()

        preview = st.session_state.get("rex_bulktranslate_preview")
        if preview and preview["languages"] == target_langs and preview["provider"] == provider and preview["force"] == force:
            counts = {"create": 0, "update": 0, "skip_manual": 0, "skip_primary": 0}
            for r in preview["rows"]:
                counts[r["action"]] += 1
            st.caption(T(
                "product_list.bulk_translate_preview_summary",
                create=counts["create"], update=counts["update"], skip=counts["skip_manual"],
            ))
            actionable = [r for r in preview["rows"] if r["action"] in ("create", "update")]
            if not actionable:
                st.info(T("product_list.bulk_translate_nothing_to_do"))
            else:
                st.dataframe(
                    [
                        {
                            T("product_list.product_col"): r["sku"] or r["name"] or r["product_id"],
                            T("product_list.content_language"): company_store.CONTENT_LANGUAGES.get(r["language"], r["language"]),
                            T("product_list.action_label"): r["action"],
                        }
                        for r in preview["rows"]
                    ],
                    hide_index=True, use_container_width=True,
                )

                acol1, acol2 = st.columns(2)
                with acol1:
                    if st.button(T("common.cancel"), use_container_width=True, key="rex_bulktranslate_preview_cancel"):
                        st.session_state.rex_bulktranslate_preview = None
                        st.rerun()
                with acol2:
                    apply_clicked = st.button(
                        T("product_list.apply_changes"), type="primary", use_container_width=True,
                        key="rex_bulktranslate_apply",
                    )
                if apply_clicked:
                    by_id = {p.id: p for p in selected_products}
                    progress = st.progress(0.0)
                    ok_count, fail_count = 0, 0
                    for i, r in enumerate(actionable):
                        p = by_id[r["product_id"]]
                        try:
                            translation_service.translate_product(
                                p, target_language=r["language"], provider_type=provider,
                                company_id=p.company_id, translated_by=provider, force=force,
                            )
                            ok_count += 1
                        except Exception:
                            fail_count += 1
                        progress.progress((i + 1) / len(actionable))
                    st.session_state.rex_bulktranslate_preview = None
                    if fail_count:
                        st.success(T("product_list.bulk_translate_partial_success", ok=ok_count, fail=fail_count))
                    else:
                        st.success(T("product_list.bulk_translate_success", count=ok_count))
                    if st.button(T("common.close"), use_container_width=True, key="rex_bulktranslate_done"):
                        st.rerun()

    @st.dialog(T("product_list.change_status_title"))
    def _confirm_change_status_dialog(selected_products):
        st.write(T("product_list.change_status_heading", count=len(selected_products)))
        new_status = st.selectbox(T("product_list.new_status"), BULK_STATUS_OPTIONS, key="rex_status_value")
        col1, col2 = st.columns(2)
        with col1:
            if st.button(T("common.cancel"), use_container_width=True, key="rex_status_cancel"):
                st.rerun()
        with col2:
            confirmed = st.button(
                T("product_list.apply_status_change"), type="primary", use_container_width=True, key="rex_status_confirm"
            )
        if confirmed:
            for p in selected_products:
                p.status = new_status
                inventory_store.save_product(p)
                audit_store.log_audit(
                    p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id, f"status -> {new_status}",
                )
            st.session_state.rex_clear_seq += 1
            st.success(T("product_list.status_changed_success", count=len(selected_products)))
            if st.button(T("common.close"), use_container_width=True, key="rex_status_done"):
                st.rerun()

    @st.dialog(T("product_list.photo_dialog_title"), width="large")
    def _photo_lightbox_dialog(image_paths, start_index: int):
        idx = st.session_state.get("rex_lightbox_index", start_index)
        idx = max(0, min(idx, len(image_paths) - 1))
        img_path = image_paths[idx]
        if os.path.exists(img_path):
            st.image(img_path, use_container_width=True)
        st.caption(T("product_list.photo_counter", current=idx + 1, total=len(image_paths)))
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button(T("product_list.prev_photo"), disabled=idx <= 0, use_container_width=True, key="rex_lb_prev"):
                st.session_state.rex_lightbox_index = idx - 1
                st.rerun()
        with c2:
            if st.button(T("common.close"), use_container_width=True, key="rex_lb_close"):
                st.rerun()
        with c3:
            if st.button(
                T("product_list.next_photo"), disabled=idx >= len(image_paths) - 1,
                use_container_width=True, key="rex_lb_next",
            ):
                st.session_state.rex_lightbox_index = idx + 1
                st.rerun()

    @st.dialog(T("product_list.translate_dialog_title"))
    def _confirm_translate_dialog(product):
        st.write(T("product_list.translate_heading", name=product.name or product.sku))
        _existing_langs = {t.language for t in product_translation_store.list_translations(product.id)}
        _lang_choices = [
            code for code in company_store.CONTENT_LANGUAGES if code != product.primary_language
        ]
        target_langs = st.multiselect(
            T("product_list.translate_target_languages"),
            options=_lang_choices,
            format_func=lambda code: (
                f"{company_store.CONTENT_LANGUAGES[code]}"
                + (f" ({T('product_list.retranslate_suffix')})" if code in _existing_langs else "")
            ),
            key="rex_translate_langs",
        )
        provider = st.radio(
            T("settings.translation_provider"), options=["deepl", "openai"],
            format_func=lambda p: {"deepl": "DeepL", "openai": "OpenAI"}[p],
            index=["deepl", "openai"].index(current_company.translation_provider)
            if current_company and current_company.translation_provider in ("deepl", "openai") else 0,
            key="rex_translate_provider", horizontal=True,
        )
        force = st.checkbox(T("product_list.retranslate_force"), key="rex_translate_force")

        col1, col2 = st.columns(2)
        with col1:
            if st.button(T("common.cancel"), use_container_width=True, key="rex_translate_cancel"):
                st.rerun()
        with col2:
            confirmed = st.button(
                T("product_list.translate_action"), type="primary", use_container_width=True, key="rex_translate_confirm",
            )
        if confirmed:
            if not target_langs:
                st.warning(T("product_list.translate_select_language"))
            elif not IntegrationManager.is_connected(product.company_id, provider):
                st.warning(T("product_list.translate_provider_not_connected"))
            else:
                created, skipped = 0, 0
                for lang in target_langs:
                    try:
                        result = translation_service.translate_product(
                            product, target_language=lang, provider_type=provider,
                            company_id=product.company_id, translated_by=provider, force=force,
                        )
                    except Exception as e:
                        st.error(T("product_list.translate_failed", language=company_store.CONTENT_LANGUAGES[lang], error=e))
                        continue
                    if result is None:
                        skipped += 1
                    else:
                        created += 1
                if created:
                    st.success(T("product_list.translate_success", count=created))
                if skipped:
                    st.caption(T("product_list.translate_skipped_manual", count=skipped))
                if created:
                    st.rerun()

    def _render_product_card(p: Product, tier_label: str = ""):
        """The single detail view for a product — used both when a row is
        opened from the paginated list/card-view below, and inline (once
        per match, no separate open step) from the exact-lookup search
        further down. Combines the old Review & Export page's fully
        editable form (still the only place these fields are edited) with
        the old Inventory page's operational sections (quick actions,
        manifest info, repair history, marketplace/BaseLinker actions,
        financials) — Inventory used to re-display brand/model/category/
        EAN/condition/description/defects/etc a second time, read-only;
        that's dropped here since the editable form above already shows
        the same data live."""
        if tier_label:
            st.caption(T("product_list.exact_match", tier=tier_label))
        st.subheader(f"{p.sku} — {p.name or T('product_list.no_name')}")
        st.caption(_status_badge(p))
        st.divider()

        # -- Quick actions: triage status / location are the two things
        # someone changes constantly during daily inventory work, so they
        # auto-save on change rather than waiting for the Save button below
        # (which only covers the review/grading fields). Quantity is
        # deliberately NOT here — it lives only in the Pricing section
        # below now, to avoid two different Quantity controls on one card.
        triage_keys = [k for k in TRIAGE_LABELS if k]
        qa1, qa2, qa3 = st.columns([2, 2, 1])
        with qa1:
            new_triage = st.selectbox(
                T("product_list.triage_status"), options=triage_keys,
                index=triage_keys.index(p.triage_status) if p.triage_status in triage_keys else 0,
                format_func=lambda k: TRIAGE_LABELS[k],
                key=f"rex_triage_{p.id}",
            )
            if new_triage != p.triage_status:
                p.triage_status = new_triage
                inventory_store.save_product(p)
                audit_store.log_audit(p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id, "triage_status")
                st.rerun()
        with qa2:
            new_location = st.text_input(T("common.location"), value=p.location, key=f"rex_loc_{p.id}")
            if new_location != p.location:
                p.location = new_location
                inventory_store.save_product(p)
                audit_store.log_audit(p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id, "location")
                st.rerun()
        with qa3:
            st.write("")
            if st.button(T("common.delete"), key=f"rex_delete_{p.id}", use_container_width=True):
                inventory_store.delete_product(p.id, p.company_id)
                audit_store.log_audit(p.company_id, current_user.id, "DELETE_PRODUCT", "product", p.id)
                st.session_state.rex_open_product_id = None
                st.rerun()

        # Not wrapped in st.form: the Photos section below (per-photo
        # "Enlarge" buttons, its own upload control) needs regular
        # widgets, which st.form forbids — and it belongs right above
        # Save, not above these fields. Every widget gets an explicit
        # per-product key so Previous/Next don't leave a stale typed
        # value behind when the label text is identical across products.
        st.markdown(f"**{T('product_list.product_information')}**")
        st.caption(T("product_list.name_moved_caption"))
        pi1, pi2 = st.columns(2)
        with pi1:
            brand_in = st.text_input(T("common.brand"), value=p.brand, key=f"rex_brand_{p.id}")
            barcode_in = st.text_input(
                T("common.barcode"), value=p.ean or p.manifest_barcode or p.scanned_barcode,
                key=f"rex_barcode_{p.id}",
            )
        with pi2:
            model_in = st.text_input(T("common.model"), value=p.model, key=f"rex_model_{p.id}")
            category_in = st.text_input(T("common.category"), value=p.category, key=f"rex_category_{p.id}")
            power_in = st.text_input(T("common.power"), value=p.power, key=f"rex_power_{p.id}")
        product_condition_in = st.selectbox(
            T("common.product_condition"), CONDITION_OPTIONS,
            index=CONDITION_OPTIONS.index(p.product_condition) if p.product_condition in CONDITION_OPTIONS else 1,
            key=f"rex_condition_{p.id}",
        )

        st.markdown(f"**{T('product_list.pricing')}**")
        pr1, pr2 = st.columns(2)
        with pr1:
            price_in = st.number_input(
                T("product_list.price_eur"), min_value=0.0, value=float(p.price), step=1.0, key=f"rex_price_{p.id}",
            )
        with pr2:
            quantity_in = st.number_input(
                T("common.quantity"), min_value=1, value=int(p.quantity or 1), step=1, key=f"rex_qty_{p.id}",
            )

        # -- Content language: name/product_description/condition_description/
        # defects are the only fields that live per-language (see
        # modules/product_translation_store.py) — everything else on this
        # card (brand/model/price/quantity/etc.) is language-neutral and
        # always saves straight to the product regardless of which tab is
        # selected here.
        _translations_by_lang = {t.language: t for t in product_translation_store.list_translations(p.id)}
        _lang_tabs = [p.primary_language] + [l for l in _translations_by_lang if l != p.primary_language]
        _lang_state_key = f"rex_content_lang_{p.id}"
        _default_lang = (
            current_company.default_product_language
            if current_company and current_company.default_product_language in _lang_tabs
            else p.primary_language
        )
        if st.session_state.get(_lang_state_key) not in _lang_tabs:
            st.session_state[_lang_state_key] = _default_lang

        lang_col, translate_col = st.columns([4, 1])
        with lang_col:
            selected_lang = st.radio(
                T("product_list.content_language"), options=_lang_tabs,
                format_func=lambda code: company_store.CONTENT_LANGUAGES.get(code, code.upper()),
                key=_lang_state_key, horizontal=True,
            )
        with translate_col:
            st.write("")
            if st.button(T("product_list.translate_action"), key=f"rex_translate_btn_{p.id}", use_container_width=True):
                _confirm_translate_dialog(p)

        is_primary_tab = selected_lang == p.primary_language
        if is_primary_tab:
            name_val, desc_val, extra_val = p.name, p.product_description, p.condition_description
            defects_val_list = p.defects
            box_val_list = p.box_contents
            missing_val_list = p.missing_components
            checklist_val_list = p.functional_checklist
            reasoning_val = p.product_condition_reasoning
            match_notes_val = p.match_notes
        else:
            _t = _translations_by_lang.get(selected_lang)
            name_val = _t.title if _t else ""
            desc_val = _t.description if _t else ""
            extra_val = _t.condition_description if _t else ""
            defects_val_list = _t.defects if _t else []
            box_val_list = _t.box_contents if _t else []
            missing_val_list = _t.missing_components if _t else []
            checklist_val_list = _t.functional_checklist if _t else []
            reasoning_val = _t.product_condition_reasoning if _t else ""
            match_notes_val = _t.match_notes if _t else ""
            if _t is not None and _t.translated_by == "manual":
                st.caption(T("product_list.manually_edited_translation"))

        name_in = st.text_input(
            T("common.product_name"), value=name_val, key=f"rex_name_{p.id}_{selected_lang}",
        )
        st.markdown(f"**{T('common.product_description')}**")
        desc_in = st.text_area(
            T("common.product_description"), value=desc_val, height=120, key=f"rex_desc_{p.id}_{selected_lang}",
        )

        st.markdown(f"**{T('product_list.additional_description')}**")
        extra_in = st.text_area(
            T("new_item.condition_scratches_details"),
            value=extra_val, height=100, key=f"rex_extra_{p.id}_{selected_lang}",
        )

        st.markdown(f"**{T('product_list.defects')}**")
        defects_in = st.text_area(
            T("new_item.defects_label"), value="\n".join(defects_val_list), height=80,
            key=f"rex_defects_{p.id}_{selected_lang}",
        )

        st.markdown(f"**{T('product_list.missing_components')}**")
        missing_in = st.text_area(
            T("product_list.missing_components_label"), value="\n".join(missing_val_list), height=60,
            key=f"rex_missing_{p.id}_{selected_lang}",
        )

        st.markdown(f"**{T('product_list.box_contents')}**")
        box_in = st.text_area(
            T("product_list.box_contents_label"), value="\n".join(box_val_list), height=60,
            key=f"rex_box_{p.id}_{selected_lang}",
        )

        st.markdown(f"**{T('product_list.functional_checklist')}**")
        checklist_in = st.text_area(
            T("product_list.functional_checklist_label"), value="\n".join(checklist_val_list), height=80,
            key=f"rex_checklist_{p.id}_{selected_lang}",
        )

        if reasoning_val:
            st.caption(T("product_list.condition_reasoning", reasoning=reasoning_val))
        if match_notes_val:
            st.caption(T("new_item.match_notes") + f": {match_notes_val}")
        if p.price_reasoning:
            st.caption(T("product_list.price_reasoning", reasoning=p.price_reasoning))

        st.divider()
        st.markdown(f"**{T('product_list.photos')}** ({len(p.image_paths)}/{REVIEW_CARD_MAX_PHOTOS})")
        if p.image_paths:
            photo_cols = st.columns(4)
            for i, img_path in enumerate(p.image_paths):
                if os.path.exists(img_path):
                    with photo_cols[i % 4]:
                        # Fixed square box + object-fit:cover (via a plain
                        # <img>, not st.image — which just scales to the
                        # column width and leaves each photo's own aspect
                        # ratio intact) so every tile is the same size
                        # regardless of the source photo's shape, instead
                        # of a ragged grid of different-height images.
                        card_thumb = _ensure_card_photo(img_path)
                        thumb_url = _image_static_url(card_thumb) if card_thumb else None
                        if thumb_url:
                            st.markdown(
                                f'<img src="{thumb_url}" style="width:100%;aspect-ratio:1/1;'
                                f'object-fit:cover;border-radius:8px;display:block;" />',
                                unsafe_allow_html=True,
                            )
                        else:
                            st.image(img_path, use_container_width=True)
                        if st.button(T("product_list.enlarge"), key=f"rex_enlarge_{p.id}_{i}", use_container_width=True):
                            st.session_state.rex_lightbox_index = i
                            _photo_lightbox_dialog(p.image_paths, i)
        else:
            st.caption(T("product_list.no_photos"))

        remaining_slots = REVIEW_CARD_MAX_PHOTOS - len(p.image_paths)
        if remaining_slots > 0:
            with st.form(key=f"rex_photo_upload_{p.id}", clear_on_submit=True):
                new_photo_files = st.file_uploader(
                    T("product_list.add_photos", remaining=remaining_slots),
                    type=["jpg", "jpeg", "png"], accept_multiple_files=True,
                )
                if st.form_submit_button(T("product_list.add_photos_button")):
                    if not new_photo_files:
                        st.warning(T("product_list.no_photos_selected"))
                    else:
                        to_add = new_photo_files[:remaining_slots]
                        if len(new_photo_files) > remaining_slots:
                            st.warning(T("product_list.only_added_photos", remaining=remaining_slots, max=REVIEW_CARD_MAX_PHOTOS))
                        item_dir = UPLOAD_DIR / sku_folder_name(p.sku, p.id)
                        item_dir.mkdir(parents=True, exist_ok=True)
                        existing_nums = [
                            int(m.group(1)) for f in item_dir.glob("photo_*.jpg")
                            if (m := re.match(r"photo_(\d+)\.jpg$", f.name))
                        ]
                        next_num = (max(existing_nums) + 1) if existing_nums else 1
                        added_paths = []
                        for j, uf in enumerate(to_add):
                            norm = _normalize_captured_photo(uf.read())
                            fp = item_dir / f"photo_{next_num + j}.jpg"
                            fp.write_bytes(norm)
                            added_paths.append(str(fp))
                        p.image_paths = p.image_paths + added_paths
                        inventory_store.save_product(p)
                        audit_store.log_audit(
                            p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id,
                            f"added {len(to_add)} photo(s)",
                        )
                        st.success(T("product_list.added_photos_success", count=len(to_add)))
                        st.rerun()
        else:
            st.caption(T("product_list.max_photos_reached", max=REVIEW_CARD_MAX_PHOTOS))

        st.divider()
        if st.button(T("product_list.save_button"), type="primary", use_container_width=True, key=f"rex_save_{p.id}"):
            new_defects = [l.strip() for l in defects_in.splitlines() if l.strip()]
            new_missing = [l.strip() for l in missing_in.splitlines() if l.strip()]
            new_box = [l.strip() for l in box_in.splitlines() if l.strip()]
            new_checklist = [l.strip() for l in checklist_in.splitlines() if l.strip()]

            # name/desc/extra/defects/missing/box are compared against
            # name_val/desc_val/extra_val/defects_val_list/missing_val_list/
            # box_val_list (this tab's ORIGINAL values, whichever language
            # that is) — never against p.name/p.product_description/etc.
            # directly, which would spuriously read as "changed" whenever a
            # non-primary-language tab is open (its text never equals the
            # English p.* values).
            current_barcode = p.ean or p.manifest_barcode or p.scanned_barcode
            language_content_changed = (
                name_in.strip() != name_val
                or desc_in.strip() != desc_val
                or extra_in.strip() != extra_val
                or new_defects != defects_val_list
                or new_missing != missing_val_list
                or new_box != box_val_list
                or new_checklist != checklist_val_list
            )
            changed = (
                language_content_changed
                or brand_in.strip() != p.brand
                or model_in.strip() != p.model
                or category_in.strip() != p.category
                or barcode_in.strip() != current_barcode
                or product_condition_in != p.product_condition
                or float(price_in) != float(p.price)
                or int(quantity_in) != p.quantity
            )

            # Snapshot pre-change values for the fields the real
            # two-way sync change-detector cares about (SYNC_OWNERSHIP_FIELDS'
            # keys) — captured before reassignment below so the event
            # layer can log/enqueue an accurate old -> new diff per field.
            # name/product_description only reflect a real diff when the
            # PRIMARY-language tab is the one being saved — the sync-ownership
            # feature syncs primary/English content, never a translation.
            _sync_field_diffs = {
                "name": (p.name, name_in.strip() if is_primary_tab else p.name),
                "brand": (p.brand, brand_in.strip()),
                "model": (p.model, model_in.strip()),
                "category": (p.category, category_in.strip()),
                "power": (p.power, power_in.strip()),
                "barcode": (current_barcode, barcode_in.strip()),
                "product_condition": (p.product_condition, product_condition_in),
                "price": (p.price, float(price_in)),
                "quantity": (p.quantity, int(quantity_in)),
                "product_description": (p.product_description, desc_in.strip() if is_primary_tab else p.product_description),
            }

            p.brand = brand_in.strip()
            p.model = model_in.strip()
            p.category = category_in.strip()
            p.power = power_in.strip()
            if barcode_in.strip() != current_barcode:
                p.ean = barcode_in.strip()
                p.ean_source = "manual"
                p.ean_status = "Found" if barcode_in.strip() else ""
            p.product_condition = product_condition_in
            p.price = float(price_in)
            p.quantity = int(quantity_in)
            if changed:
                p.review_status = "edited"

            if is_primary_tab:
                p.name = name_in.strip()
                p.product_description = desc_in.strip()
                p.condition_description = extra_in.strip()
                p.defects = new_defects
                p.missing_components = new_missing
                p.box_contents = new_box
                p.functional_checklist = new_checklist
                inventory_store.save_product(p)  # upserts the primary-language row too
            else:
                inventory_store.save_product(p)  # brand/price/etc. above; primary-language row re-saved unchanged
                if language_content_changed:
                    # spec_summary/product_condition_reasoning/match_notes aren't
                    # edited via any widget on this card — carried through
                    # unchanged from the existing row so this upsert (a full
                    # column replace) never silently wipes them.
                    _existing_t = _translations_by_lang.get(selected_lang)
                    product_translation_store.upsert_translation(product_translation_store.ProductTranslation(
                        product_id=p.id, company_id=p.company_id, language=selected_lang,
                        title=name_in.strip(), description=desc_in.strip(),
                        condition_description=extra_in.strip(), defects=new_defects,
                        box_contents=new_box, missing_components=new_missing,
                        functional_checklist=new_checklist,
                        spec_summary=_existing_t.spec_summary if _existing_t else "",
                        product_condition_reasoning=_existing_t.product_condition_reasoning if _existing_t else "",
                        match_notes=_existing_t.match_notes if _existing_t else "",
                        translated_by="manual",
                    ))
            if changed:
                audit_store.log_audit(p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id)
                _record_sync_field_changes(p, _sync_field_diffs, current_user.id)
            st.session_state.rex_open_product_id = None
            st.session_state.rex_focus_id = p.id  # scroll/highlight this row back in the list
            st.success(T("product_list.saved"))
            st.rerun()

        # -- Manifest info (from the old Inventory page, unchanged) --
        with st.expander(T("product_list.manifest_info"), expanded=False):
            if p.manifest_import_id:
                st.write(f"**{T('product_list.manifest_item_description')}:**", p.manifest_item_description or "—")
                st.write(
                    f"**{T('product_list.manifest_target_no')}:** {p.manifest_target_no or '—'}  •  "
                    f"**{T('product_list.subcategory')}:** {p.manifest_subcategory or '—'}"
                )
                st.write(f"**{T('product_list.manifest_asin')}:** {p.asin or '—'}  •  **{T('product_list.manifest_barcode')}:** {p.manifest_barcode or '—'}")
                st.write(f"**{T('new_item.qty')}:** {p.manifest_qty}  •  **{T('new_item.weight')}:** {p.manifest_weight_kg} kg")
                st.caption(T("product_list.batch_label", batch_id=p.manifest_import_id))
            else:
                st.caption(T("product_list.manually_entered"))

        # -- Repair History (from the old Inventory page, unchanged) --
        events = repair_store.list_repair_events(p.id, p.company_id)
        repair_total = repair_store.total_repair_cost(p.id, p.company_id)
        with st.expander(T("product_list.repair_history_title", count=len(events), total=f"{repair_total:.2f}")):
            for e in events:
                rc1, rc2 = st.columns([5, 1])
                with rc1:
                    when = time.strftime("%Y-%m-%d", time.localtime(e.occurred_at))
                    st.write(f"**{when}** — {e.description or T('product_list.no_description')} — €{e.cost:.2f}"
                             + (f" — {e.technician}" if e.technician else ""))
                with rc2:
                    if st.button("🗑️", key=f"delrepair_{e.id}"):
                        repair_store.delete_repair_event(e.id, p.company_id)
                        st.rerun()
            with st.form(key=f"addrepair_{p.id}", clear_on_submit=True):
                rf1, rf2, rf3 = st.columns([3, 1, 1])
                with rf1:
                    r_desc = st.text_input(T("common.description"), key=f"rdesc_{p.id}")
                with rf2:
                    r_cost = st.number_input(T("product_list.cost"), min_value=0.0, step=0.5, key=f"rcost_{p.id}")
                with rf3:
                    r_tech = st.text_input(T("product_list.technician"), key=f"rtech_{p.id}")
                if st.form_submit_button(T("product_list.add_repair_entry")):
                    if r_desc.strip():
                        repair_store.add_repair_event(
                            repair_store.RepairEvent(
                                product_id=p.id, company_id=p.company_id, description=r_desc.strip(),
                                cost=r_cost, technician=r_tech.strip(),
                            )
                        )
                        st.rerun()
                    else:
                        st.warning(T("product_list.description_required"))

        # -- Sales & Listings (from the old Inventory page — the
        # read-only Price/Description/Condition re-display that used to
        # sit at the top of this expander is dropped: the editable form
        # above already covers those fields live) --
        with st.expander(T("product_list.sales_listings"), expanded=False):
            listings = marketplace_store.list_listings(p.id, p.company_id)
            if listings:
                for listing in listings:
                    st.write(
                        f"**{listing.marketplace}:** {listing.status}"
                        + (f" — #{listing.external_listing_id}" if listing.external_listing_id else "")
                        + (f" — {listing.url}" if listing.url else "")
                    )
            else:
                st.caption(T("product_list.not_listed_yet"))

            if IntegrationManager.is_connected(p.company_id, "baselinker"):
                bl_col1, bl_col2, bl_col3, bl_col4 = st.columns(4)
                with bl_col1:
                    if st.button(T("product_list.preview_export"), key=f"preview_{p.id}", use_container_width=True):
                        st.session_state[f"show_export_preview_{p.id}"] = True
                with bl_col2:
                    if st.button(T("product_list.push_to_baselinker"), key=f"push_{p.id}", use_container_width=True):
                        result = IntegrationManager.get(p.company_id, "baselinker").export_product(p)
                        if result.success:
                            audit_store.log_audit(
                                p.company_id, current_user.id, "EXPORT_BASELINKER", "product", p.id,
                                f"baselinker_product_id={result.external_id}",
                            )
                            st.success(T("product_list.pushed_success", external_id=result.external_id))
                            st.rerun()
                        else:
                            st.error(result.message)
                with bl_col3:
                    if st.button(T("product_list.sync_now"), key=f"sync_now_{p.id}", use_container_width=True):
                        st.session_state[f"sync_now_results_{p.id}"] = sync_service.run_manual_sync(
                            p.company_id, p, "baselinker",
                        )
                with bl_col4:
                    if st.button(T("product_list.pull_now"), key=f"pull_now_{p.id}", use_container_width=True):
                        st.session_state[f"pull_now_results_{p.id}"] = sync_engine.pull_product(
                            p.company_id, p, "baselinker",
                        )
                        st.rerun()

                if st.session_state.get(f"sync_now_results_{p.id}"):
                    # export is real (delegates to the same export_product()
                    # path as the Push button above); import honestly reports
                    # "disabled" since no connector implements pulling data
                    # FROM BaseLinker through this specific path yet — real
                    # Pull is the dedicated "⬇️ Pull now" button below, which
                    # goes through sync/engine.py's pull_product() +
                    # Conflict Resolver instead of this generic sync() entry
                    # point. Never fakes a result either way.
                    with st.expander(T("product_list.sync_now_result"), expanded=True):
                        for rec in st.session_state[f"sync_now_results_{p.id}"]:
                            if rec.sync_status == STATUS_SUCCESS:
                                st.success(f"{rec.direction.capitalize()}: ✅ {T('product_list.success_word')}")
                            elif rec.sync_status == STATUS_DISABLED:
                                st.info(f"{rec.direction.capitalize()}: ⏸️ {rec.error_message or T('product_list.disabled_word')}")
                            else:
                                st.error(f"{rec.direction.capitalize()}: ❌ {rec.error_message}")
                        if st.button(T("common.close"), key=f"close_sync_now_{p.id}"):
                            st.session_state[f"sync_now_results_{p.id}"] = None
                            st.rerun()

                if st.session_state.get(f"pull_now_results_{p.id}") is not None:
                    with st.expander(T("product_list.pull_now_result"), expanded=True):
                        resolutions = st.session_state[f"pull_now_results_{p.id}"]
                        if not resolutions:
                            st.caption(T("product_list.nothing_to_pull"))
                        for res in resolutions:
                            if res.resolution_action == "accepted":
                                st.success(T("product_list.field_accepted", field=res.field_name, value=res.applied_value))
                            elif res.resolution_action == "overridden":
                                st.warning(T("product_list.field_conflict", field=res.field_name, value=res.applied_value))
                            else:
                                st.info(T("product_list.field_pending_review", field=res.field_name))
                        if st.button(T("common.close"), key=f"close_pull_now_{p.id}"):
                            st.session_state[f"pull_now_results_{p.id}"] = None
                            st.rerun()

                if st.session_state.get(f"show_export_preview_{p.id}"):
                    # Built from the exact same connector.export_product() code
                    # path (mapper.build_payload) — can never drift from what a
                    # real push actually sends.
                    payload = IntegrationManager.get(p.company_id, "baselinker").preview_payload(p)
                    text_fields = payload.get("text_fields", {})
                    prices = payload.get("prices") or {}
                    stock = payload.get("stock") or {}
                    with st.expander(T("product_list.export_preview_title"), expanded=True):
                        st.caption(T("product_list.export_preview_caption"))
                        _empty_label = T("product_list.empty_dash")
                        _excluded_label = T("product_list.excluded_dash")
                        st.write(f"**{T('product_list.title_label')}:**", text_fields["name"] or _empty_label if "name" in text_fields else _excluded_label)
                        st.write(
                            f"**{T('common.description')}:**",
                            text_fields["description"] or _empty_label if "description" in text_fields else _excluded_label,
                        )
                        st.write(
                            f"**{T('product_list.additional_description')}:**",
                            text_fields["description_extra1"] if "description_extra1" in text_fields else _excluded_label,
                        )
                        st.write(f"**{T('common.sku')}:**", payload.get("sku", "—"))
                        st.write(f"**{T('common.barcode')}:**", payload.get("ean") or _excluded_label)
                        st.write(f"**{T('product_list.category_id')}:**", payload.get("category_id", "—"))
                        st.write(f"**{T('common.price')}:**", next(iter(prices.values()), None) or T("product_list.excluded_or_no_price"))
                        st.write(f"**{T('common.quantity')}:**", next(iter(stock.values()), _excluded_label))
                        st.write(f"**{T('product_list.images_label')}:**", T("product_list.images_included", count=payload.get('_preview_image_count', 0)))
                        if st.button(T("product_list.close_preview"), key=f"close_preview_{p.id}"):
                            st.session_state[f"show_export_preview_{p.id}"] = False
                            st.rerun()

        # -- Financials (from the old Inventory page, unchanged) --
        with st.expander(T("product_list.financials"), expanded=False):
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                new_purchase = st.number_input(
                    T("product_list.purchase_price_allocated"), min_value=0.0, step=0.5,
                    value=p.purchase_price_allocated, key=f"purchase_{p.id}",
                )
                if new_purchase != p.purchase_price_allocated:
                    p.purchase_price_allocated = new_purchase
                    inventory_store.save_product(p)
                    audit_store.log_audit(
                        p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id, "purchase_price_allocated",
                    )
                    st.rerun()
            with fc2:
                st.metric(T("product_list.repair_cost"), f"€{repair_total:.2f}")
            with fc3:
                profit = p.price - p.purchase_price_allocated - repair_total
                st.metric(T("product_list.profit_metric"), f"€{profit:.2f}")

        st.divider()

    # ---------------------------------------------------- FILTER + QUERY --
    # A small toggle button replaces what used to be three separate,
    # always-visible bars (Status+Search, a "More filters" expander, and a
    # separate Exact-lookup box) — one filter entry point instead of three.
    # Deliberately a plain st.button + st.container (not st.popover):
    # st.popover ties the open panel's width to the trigger button's width
    # and floats it *over* the table as an overlay. This instead renders
    # inline in the normal page flow when open — full container width
    # (matching the table) with no CSS needed, and the table is pushed
    # down below it rather than covered, while the trigger stays a small
    # button either way.
    if st.button(T("product_list.filter_products") if not st.session_state.rex_filters_open else T("product_list.filter_products_open")):
        st.session_state.rex_filters_open = not st.session_state.rex_filters_open
        st.rerun()

    # Streamlit drops a widget's session_state entry once that widget
    # stops appearing in the script's element tree for a run — which is
    # exactly what happens to every field below every time the panel
    # collapses. So the *persistent* value for each field lives in the
    # plain (non-widget) rex_search_sku/rex_status_filter/etc. keys
    # (REX_FILTER_DEFAULTS' keys), while each widget below uses its own
    # separate "_w"-suffixed key that only exists while the panel is
    # open. These two small helpers read the persistent value in as the
    # widget's starting value, and write the widget's return value back
    # to the persistent key on every render — so the persistent key
    # survives the widget disappearing when the panel closes, and
    # "Clear filters" (further down) can safely overwrite the persistent
    # keys directly since no widget is ever bound to them.
    def _synced_text_input(label: str, state_key: str, **kwargs):
        val = st.text_input(label, value=st.session_state[state_key], key=f"{state_key}_w", **kwargs)
        if val != st.session_state[state_key]:
            st.session_state[state_key] = val
            st.session_state.rex_page = 1
        return val

    def _synced_selectbox(label: str, options: list, state_key: str, **kwargs):
        cur = st.session_state[state_key]
        idx = options.index(cur) if cur in options else 0
        val = st.selectbox(label, options, index=idx, key=f"{state_key}_w", **kwargs)
        if val != st.session_state[state_key]:
            st.session_state[state_key] = val
            st.session_state.rex_page = 1
        return val

    # Fetched unconditionally (cheap query) since batch_options is needed
    # below for the "Active filters" caption even when the panel is closed,
    # not just while the selectbox itself is on screen.
    batches = manifest_store.list_batches(st.session_state.company_id)
    batch_options = {"": T("product_list.all_batches")}
    batch_options.update({b.id: f"{b.filename} ({b.id})" for b in batches})

    if st.session_state.rex_filters_open:
        with st.container(border=True):
            st.markdown(f"**{T('product_list.search_by_field')}**")
            sf1, sf2, sf3, sf4 = st.columns(4)
            with sf1:
                _synced_text_input(T("common.sku"), "rex_search_sku")
                _synced_text_input(T("common.location"), "rex_search_location")
            with sf2:
                _synced_text_input(T("common.name"), "rex_search_name")
                _synced_text_input("EAN", "rex_search_ean")
            with sf3:
                _synced_text_input(T("common.brand"), "rex_search_brand")
                _synced_text_input("ASIN", "rex_search_asin")
            with sf4:
                _synced_text_input(T("common.model"), "rex_search_model")

            st.markdown(f"**{T('product_list.more_filters')}**")
            mf1, mf2, mf3, mf4 = st.columns(4)
            with mf1:
                _synced_selectbox(
                    T("common.status"), list(REX_STATUS_FILTER_OPTIONS), "rex_status_filter",
                    format_func=lambda k: REX_STATUS_FILTER_OPTIONS[k],
                )
            with mf2:
                _synced_selectbox(
                    T("product_list.triage_status"), [k for k in TRIAGE_LABELS], "rex_triage_filter",
                    format_func=lambda k: TRIAGE_LABELS[k],
                )
            with mf3:
                _synced_selectbox(
                    T("common.product_condition"), [""] + CONDITION_OPTIONS, "rex_condition_filter",
                    format_func=lambda k: k or T("common.all"),
                )
            with mf4:
                _synced_selectbox(
                    T("product_list.manifest_batch"), list(batch_options), "rex_batch_filter",
                    format_func=lambda k: batch_options[k],
                )

            st.divider()
            # Fundamentally different from the per-field substring boxes
            # above: this is the old Inventory page's tiered *exact*-match
            # lookup (inventory_store.search_products — exact SKU, then
            # EAN, then ASIN, then model, then brand/name substring)
            # across the WHOLE inventory regardless of the filters above,
            # not just the current page. Matches are inherently few
            # (usually 0-3), so rendering them directly bypasses
            # pagination entirely — no scale concern.
            _synced_text_input(
                T("product_list.exact_lookup_label"),
                "rex_exact_search",
                placeholder="e.g. 2001, 0194252057338, B08ASIN123, iPhone 12, Apple, A2172",
                help=T("product_list.exact_lookup_help"),
            )

            st.divider()
            fb1, fb2 = st.columns(2)
            with fb1:
                if st.button(T("product_list.set_filters"), type="primary", use_container_width=True, key="rex_set_filters_btn"):
                    st.session_state.rex_filters_open = False
                    st.rerun()
            with fb2:
                if st.button(T("product_list.clear_filters"), use_container_width=True, key="rex_clear_filters_btn"):
                    # Safe to set these directly: no widget is bound to
                    # these exact keys (widgets use the separate "_w"
                    # keys above), so there's no "already instantiated
                    # this run" conflict.
                    for _k, _v in REX_FILTER_DEFAULTS.items():
                        st.session_state[_k] = _v
                    st.session_state.rex_filters_open = False
                    st.session_state.rex_page = 1
                    st.rerun()

    # These persistent keys survive the panel closing (see the sync
    # helpers above), so filters keep applying to the query below
    # regardless of whether the panel is currently open or collapsed.
    sku_in = st.session_state.rex_search_sku
    name_in = st.session_state.rex_search_name
    brand_in = st.session_state.rex_search_brand
    model_in = st.session_state.rex_search_model
    ean_in = st.session_state.rex_search_ean
    asin_in = st.session_state.rex_search_asin
    location_in = st.session_state.rex_search_location
    status_filter = st.session_state.rex_status_filter
    triage_filter = st.session_state.rex_triage_filter
    condition_filter = st.session_state.rex_condition_filter
    batch_filter = st.session_state.rex_batch_filter
    exact_query = st.session_state.rex_exact_search

    _status_kwargs = {}
    if status_filter == "completed":
        _status_kwargs["status"] = "completed"
    elif status_filter == "all_except_draft":
        _status_kwargs["exclude_status"] = "draft"
    # "all": no status filter at all
    if triage_filter:
        _status_kwargs["triage_status"] = triage_filter
    if condition_filter:
        _status_kwargs["product_condition"] = condition_filter
    if batch_filter:
        _status_kwargs["manifest_import_id"] = batch_filter

    # Fetched unconditionally (cheap, indexed, LIMIT-bounded query) right
    # here — even in exact-lookup mode where it isn't displayed — so
    # rex_current_page_ids and total_count are already available for the
    # Operations/Download toolbar immediately below, without needing a
    # deferred-container trick or an st.fragment boundary (a container
    # written into from inside a fragment turned out not to reposition
    # reliably in the real browser, even though it looked correct in
    # headless tests).
    products_page, total_count = inventory_store.list_products_paginated(
        st.session_state.company_id,
        sku=sku_in, name=name_in, brand=brand_in, model=model_in,
        ean=ean_in, asin=asin_in, location=location_in,
        page=st.session_state.rex_page,
        page_size=REX_PAGE_SIZE,
        **_status_kwargs,
    )
    st.session_state.rex_current_page_ids = [p.id for p in products_page]
    total_pages = max(1, (total_count + REX_PAGE_SIZE - 1) // REX_PAGE_SIZE)

    def _render_numbered_pagination(key_prefix: str = "rex_pg"):
        # Centered "windowed" numbered pagination — 3 pages before and
        # after the current one, plus page 1 and the last page always
        # visible, with "…" filling any gap. Rendered both above and below
        # the table; key_prefix keeps the two instances' widget keys apart.
        cur = st.session_state.rex_page
        window = 3
        start_w = max(1, cur - window)
        end_w = min(total_pages, cur + window)
        pages = sorted(set([1, total_pages] + list(range(start_w, end_w + 1))))

        cells = ["prev"]
        prev_p = None
        for p in pages:
            if prev_p is not None and p - prev_p > 1:
                cells.append("ellipsis")
            cells.append(p)
            prev_p = p
        cells.append("next")

        # Flat, borderless look for the page-number/arrow buttons (no box
        # around each digit), with nowrap + a min-width sized for up to
        # 3-digit page numbers so "100" etc. never wraps onto two lines.
        # Targets by key prefix (all these buttons use "rex_pg_*" keys, via
        # Streamlit's "st-key-<key>" container class), not globally — other
        # buttons on the page keep their normal look.
        st.markdown(
            """
            <style>
            div[class*="st-key-rex_pg_"] button {
                border: none !important;
                background: transparent !important;
                box-shadow: none !important;
                white-space: nowrap !important;
                min-width: 2.6em !important;
                padding: 2px 4px !important;
            }
            div[class*="st-key-rex_pg_"] button:hover:not(:disabled) {
                background: rgba(128, 128, 128, 0.15) !important;
                border-radius: 6px !important;
            }
            div[class*="st-key-rex_pg_"] button[kind="primary"] {
                background: rgba(56, 189, 248, 0.18) !important;
                border-radius: 6px !important;
                font-weight: 700 !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        _left, mid, _right = st.columns([2, 3, 2])
        with mid:
            cols = st.columns(len(cells))
            for col, cell in zip(cols, cells):
                with col:
                    if cell == "prev":
                        if st.button("‹", disabled=cur <= 1, use_container_width=True, key=f"{key_prefix}_prev"):
                            st.session_state.rex_page -= 1
                            st.rerun()
                    elif cell == "next":
                        if st.button("›", disabled=cur >= total_pages, use_container_width=True, key=f"{key_prefix}_next"):
                            st.session_state.rex_page += 1
                            st.rerun()
                    elif cell == "ellipsis":
                        st.markdown(
                            "<div style='text-align:center;padding-top:6px;'>…</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        if cell == cur:
                            st.button(
                                str(cell), type="primary", disabled=True,
                                use_container_width=True, key=f"{key_prefix}_{cell}",
                            )
                        elif st.button(str(cell), use_container_width=True, key=f"{key_prefix}_{cell}"):
                            st.session_state.rex_page = cell
                            st.rerun()

    _show_list_toolbar = (
        not exact_query.strip() and total_count > 0 and st.session_state.rex_open_product_id is None
    )
    if _show_list_toolbar:
        # Fold the grid's most recently reported click into
        # rex_selected_ids BEFORE computing n_selected / rendering
        # Operations+Download, so their disabled state and the
        # "Selected: N" count reflect *this* click immediately instead of
        # lagging one click behind. Streamlit pre-populates
        # session_state["rex_table"] before the script runs on the very
        # rerun that value change triggered, so it's already fresh here.
        # _rex_skip_table_merge guards against re-applying a now-stale
        # report right after Clear Selection / bulk status change, which
        # set rex_selected_ids directly and haven't heard back from the
        # grid's frontend yet.
        _prior_table_result = st.session_state.get("rex_table")
        if _prior_table_result and not st.session_state.pop("_rex_skip_table_merge", False):
            _page_ids = set(st.session_state.rex_current_page_ids)
            _new_sel = set(_prior_table_result.get("selected_ids") or [])
            st.session_state.rex_selected_ids = (
                (st.session_state.rex_selected_ids - _page_ids) | _new_sel
            )
            if (
                _prior_table_result.get("open_id")
                and _prior_table_result["open_id"] != st.session_state.rex_open_product_id
            ):
                st.session_state.rex_open_product_id = _prior_table_result["open_id"]
                st.rerun()

        n_selected = len(st.session_state.rex_selected_ids)
        # Operations/Download sit at the LEFT, directly under the Filter
        # Products button (which is also left-aligned) — the count caption
        # takes the remaining space on the right instead of pushing these
        # two to the far side of the (wide-layout) table.
        top2, top3, top1 = st.columns([1, 1, 3])
        with top2:
            # key includes rex_ops_popover_seq: st.popover has no direct
            # "close programmatically" API, but a popover keeps its open/
            # closed UI state tied to its key across reruns — remounting it
            # under a new key (bumped by every button below that opens a
            # dialog) forces a fresh, closed popover instead of it staying
            # open behind the dialog that was just triggered.
            with st.popover(
                T("product_list.operations"), disabled=n_selected == 0, use_container_width=True,
                key=f"rex_ops_popover_{st.session_state.rex_ops_popover_seq}",
            ):
                # Flat, borderless look for this list of actions (Export,
                # Delete Products, more to come) — no button box around
                # each one, just left-aligned text with a subtle hover
                # highlight. Same technique as the pagination digits
                # above, targeted by the "rex_op_*" key prefix so it
                # doesn't affect other buttons (Apply, Clear Selection).
                # aria-label selector below must match whatever T()
                # currently renders for the "⚙️ Operations" trigger label
                # (Streamlit sets aria-label from that same text) — built
                # with an f-string so it still targets the right popover
                # in Latvian, not just English.
                st.markdown(
                    f"""
                    <style>
                    div[class*="st-key-rex_op_"] button {{
                        border: none !important;
                        background: transparent !important;
                        box-shadow: none !important;
                        text-align: left !important;
                        justify-content: flex-start !important;
                        padding: 6px 4px !important;
                    }}
                    /* text-align/justify-content on the <button> alone
                    doesn't left-align its label — Streamlit wraps the
                    label in its own centered flex div/p inside the
                    button, which needs the same override or it keeps
                    winning. */
                    div[class*="st-key-rex_op_"] button > div,
                    div[class*="st-key-rex_op_"] button p {{
                        justify-content: flex-start !important;
                        text-align: left !important;
                        width: 100% !important;
                    }}
                    div[class*="st-key-rex_op_"] button:hover:not(:disabled) {{
                        background: rgba(128, 128, 128, 0.15) !important;
                        border-radius: 6px !important;
                    }}
                    /* The popover PANEL itself (not just the buttons in
                    it) still has Streamlit's default bordered/shadowed
                    box look. It renders through a React portal, so it's
                    not a DOM descendant of anything keyed above — matched
                    instead by its aria-label, which Streamlit sets to the
                    same text as the "⚙️ Operations" trigger, so this only
                    ever targets this one popover (not "⬇️ Download" or any
                    other). Result: a plain floating list of words, no box. */
                    div[data-testid="stPopoverBody"][aria-label="{T('product_list.operations')}"] {{
                        border: none !important;
                        box-shadow: none !important;
                        border-radius: 0 !important;
                    }}
                    /* Uniform gap between every word in the list — the
                    st.divider() lines previously used to separate groups
                    added their own (larger, inconsistent) margin, making
                    some pairs look closer together than others. */
                    div[class*="st-key-rex_op_"] {{
                        margin: 2px 0 !important;
                    }}
                    </style>
                    """,
                    unsafe_allow_html=True,
                )
                can_export = current_user.role in (auth.ROLE_ADMIN, auth.ROLE_REVIEWER)
                if st.button(T("product_list.bulk_edit"), use_container_width=True, key="rex_op_bulk_edit"):
                    st.session_state.rex_bulk_edit_requested = True
                    st.session_state.rex_ops_popover_seq += 1
                    st.rerun()
                if st.button(T("product_list.change_status"), use_container_width=True, key="rex_op_change_status"):
                    st.session_state.rex_status_requested = True
                    st.session_state.rex_ops_popover_seq += 1
                    st.rerun()
                if st.button(T("product_list.translate_action"), use_container_width=True, key="rex_op_translate"):
                    st.session_state.rex_translate_requested = True
                    st.session_state.rex_bulktranslate_preview = None  # start each open clean
                    st.session_state.rex_ops_popover_seq += 1
                    st.rerun()
                if st.button(
                    T("product_list.export_button"), disabled=not can_export, use_container_width=True,
                    key="rex_op_export",
                ):
                    st.session_state.rex_export_requested = True
                    st.session_state.rex_ops_popover_seq += 1
                    st.rerun()
                if st.button(
                    T("product_list.delete_products"), disabled=not can_export, use_container_width=True,
                    key="rex_op_delete",
                ):
                    st.session_state.rex_delete_requested = True
                    st.session_state.rex_ops_popover_seq += 1
                    st.rerun()
                if not can_export:
                    st.caption(T("product_list.admins_reviewers_only"))
                if st.button(T("product_list.clear_selection"), use_container_width=True, key="rex_clear_selection"):
                    st.session_state.rex_selected_ids = set()
                    st.session_state.rex_clear_seq += 1  # tell the grid to deselect all rows
                    st.session_state["_rex_skip_table_merge"] = True
                    st.rerun()
        with top3:
            with st.popover(T("product_list.download"), disabled=n_selected == 0, use_container_width=True):
                _dl_products = _get_selected_products() if n_selected else []
                st.download_button(
                    T("product_list.download_excel"),
                    data=export.to_excel_bytes(_dl_products) if _dl_products else b"",
                    file_name="baselinker_export.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    disabled=n_selected == 0,
                    use_container_width=True,
                )
                st.download_button(
                    T("product_list.download_csv"),
                    data=export.to_csv_bytes(_dl_products) if _dl_products else b"",
                    file_name="baselinker_export.csv",
                    mime="text/csv",
                    disabled=n_selected == 0,
                    use_container_width=True,
                )
                st.caption(T("product_list.image_links_note"))
        with top1:
            st.caption(T("product_list.total_count_selected", total=total_count, selected=n_selected))

    _active_filters = []
    if status_filter != "completed":
        _active_filters.append(REX_STATUS_FILTER_OPTIONS[status_filter])
    for _field_label, _field_val in (
        (T("common.sku"), sku_in), (T("common.name"), name_in), (T("common.brand"), brand_in), (T("common.model"), model_in),
        ("EAN", ean_in), ("ASIN", asin_in), (T("common.location"), location_in),
    ):
        if _field_val.strip():
            _active_filters.append(f"{_field_label}~“{_field_val.strip()}”")
    if triage_filter:
        _active_filters.append(TRIAGE_LABELS[triage_filter])
    if condition_filter:
        _active_filters.append(T("product_list.condition_filter_label", condition=condition_filter))
    if batch_filter:
        _active_filters.append(batch_options[batch_filter])
    if exact_query.strip():
        _active_filters.append(T("product_list.exact_lookup_active", query=exact_query.strip()))
    st.caption(T("product_list.active_filters", filters=" · ".join(_active_filters)) if _active_filters else T("product_list.no_filters_applied"))

    if exact_query.strip():
        exact_results = inventory_store.search_products(exact_query.strip(), st.session_state.company_id)
        st.caption(T("product_list.match_count", count=len(exact_results), query=exact_query.strip()))
        tier_labels = {
            inventory_store.MATCH_TIER_SKU: "SKU",
            inventory_store.MATCH_TIER_EAN: "EAN",
            inventory_store.MATCH_TIER_ASIN: "ASIN",
            inventory_store.MATCH_TIER_MODEL: T("common.model").lower(),
            inventory_store.MATCH_TIER_BRAND_NAME: T("product_list.brand_name_tier"),
        }
        for p, tier in exact_results:
            _render_product_card(p, tier_labels.get(tier, ""))

    else:
        if total_count == 0:
            st.info(T("product_list.no_products_match_filter"))

        elif st.session_state.rex_open_product_id is None:
            # -------------------------------------------------------- LIST VIEW --
            if not products_page:
                st.info(T("product_list.no_products_on_page"))
            else:
                table_rows = []
                for p in products_page:
                    photo_url = None
                    if p.image_paths:
                        thumb = _ensure_thumbnail(p.image_paths[0])
                        if thumb:
                            photo_url = _image_static_url(thumb)
                    listing = marketplace_store.get_listing(p.id, "baselinker", p.company_id)
                    table_rows.append({
                        "id": p.id,
                        "photo_url": photo_url,
                        "sku": p.sku or "(none)",
                        "name": p.name or p.manifest_item_description or p.model_number or p.id,
                        "brand": p.brand or "—",
                        "product_condition": p.product_condition or "—",
                        "triage": TRIAGE_LABELS.get(p.triage_status, p.triage_status),
                        "location": p.location or "—",
                        "quantity": p.quantity or 1,
                        "price": p.price or 0,
                        "baselinker": listing.status if listing else marketplace_store.STATUS_NOT_LISTED,
                        "status": _status_badge(p),
                        "date": (
                            time.strftime("%Y-%m-%d", time.localtime(p.exported_at))
                            if p.exported_at else "—"
                        ),
                    })

                focus_id = st.session_state.rex_focus_id
                st.session_state.rex_focus_id = ""  # one-shot: only focus once

                _render_numbered_pagination()

                # The return value here isn't merged into rex_selected_ids
                # again — that already happened at the top of this run
                # (see _show_list_toolbar above), reading the exact same
                # underlying session_state["rex_table"] value. Merging
                # twice would be harmless (idempotent) but redundant.
                review_table(
                    rows=table_rows,
                    columns=_translated_columns(_PRODUCT_TABLE_COLUMNS),
                    mobile_fields=_PRODUCT_TABLE_MOBILE_FIELDS,
                    state_key="products",
                    focus_id=focus_id,
                    clear_seq=st.session_state.rex_clear_seq,
                    key="rex_table",
                )
                st.caption(T("product_list.column_sort_note"))
                _render_numbered_pagination(key_prefix="rex_pg_b")

            if st.session_state.rex_export_requested:
                st.session_state.rex_export_requested = False
                selected_products = _get_selected_products()
                if selected_products:
                    _confirm_export_dialog(selected_products)

            if st.session_state.rex_delete_requested:
                st.session_state.rex_delete_requested = False
                selected_products = _get_selected_products()
                if selected_products:
                    _confirm_delete_dialog(selected_products)

            if st.session_state.rex_bulk_edit_requested:
                st.session_state.rex_bulk_edit_requested = False
                st.session_state.rex_bulkedit_preview = None  # start each open clean
                selected_products = _get_selected_products()
                if selected_products:
                    _confirm_bulk_edit_dialog(selected_products)

            if st.session_state.rex_status_requested:
                st.session_state.rex_status_requested = False
                selected_products = _get_selected_products()
                if selected_products:
                    _confirm_change_status_dialog(selected_products)

            if st.session_state.rex_translate_requested:
                st.session_state.rex_translate_requested = False
                selected_products = _get_selected_products()
                if selected_products:
                    _confirm_bulk_translate_dialog(selected_products)

        else:
            # -------------------------------------------------------- CARD VIEW --
            esc_value = esc_listener(key="rex_esc")
            if esc_value is not None and esc_value != st.session_state.rex_last_esc_value:
                st.session_state.rex_last_esc_value = esc_value
                st.session_state.rex_open_product_id = None
                st.rerun()

            pid = st.session_state.rex_open_product_id
            p = inventory_store.get_product(pid, st.session_state.company_id)
            if p is None:
                st.warning(T("product_list.product_not_found"))
                if st.button(T("common.back_to_list")):
                    st.session_state.rex_open_product_id = None
                    st.rerun()
            else:
                # Previous/Next only walks the currently-loaded page (~50 rows)
                # — jumping to another page's products isn't supported from the
                # card view in this iteration; go back to the list to change page.
                ordered_ids = st.session_state.rex_current_page_ids or [p.id]
                if pid not in ordered_ids:
                    ordered_ids = [pid] + ordered_ids
                cur_idx = ordered_ids.index(pid)

                nav1, nav2, nav3, nav4 = st.columns([1.3, 1, 1, 1])
                with nav1:
                    if st.button(T("common.back_to_list"), use_container_width=True):
                        st.session_state.rex_open_product_id = None
                        st.rerun()
                with nav2:
                    if st.button(T("product_list.previous_product"), disabled=cur_idx <= 0, use_container_width=True):
                        st.session_state.rex_open_product_id = ordered_ids[cur_idx - 1]
                        st.rerun()
                with nav3:
                    if st.button(T("product_list.next_product"), disabled=cur_idx >= len(ordered_ids) - 1, use_container_width=True):
                        st.session_state.rex_open_product_id = ordered_ids[cur_idx + 1]
                        st.rerun()
                with nav4:
                    st.caption(f"{cur_idx + 1} / {len(ordered_ids)}")

                _render_product_card(p)

# =========================================================== MANAGE USERS =
# ================================================================ ORDERS ==
elif page == PAGE_ORDERS:
    ORDERS_PAGE_SIZE = 50

    st.title(T("nav.orders"))
    st.caption(T("orders.page_caption"))

    if st.session_state.orders_open_id is None:
        # -------------------------------------------------------- LIST VIEW --
        search_col, marketplace_col, refresh_col = st.columns([2, 1, 1])
        with search_col:
            orders_search_in = st.text_input(
                T("common.search"), value=st.session_state.orders_search,
                placeholder=T("orders.search_placeholder"),
                key="orders_search_input", label_visibility="collapsed",
            )
        with marketplace_col:
            orders_marketplace_in = st.text_input(
                T("orders.marketplace"), value=st.session_state.orders_marketplace_filter,
                placeholder=T("orders.marketplace"), key="orders_marketplace_input", label_visibility="collapsed",
            )
        with refresh_col:
            if st.button(T("common.refresh"), use_container_width=True):
                st.rerun()

        if (
            orders_search_in != st.session_state.orders_search
            or orders_marketplace_in != st.session_state.orders_marketplace_filter
        ):
            st.session_state.orders_search = orders_search_in
            st.session_state.orders_marketplace_filter = orders_marketplace_in
            st.session_state.orders_page = 1
            st.rerun()

        orders_page_rows, orders_total = order_store.list_orders_paginated(
            st.session_state.company_id,
            marketplace=st.session_state.orders_marketplace_filter or None,
            status_id=st.session_state.orders_status_filter,
            search=st.session_state.orders_search or None,
            page=st.session_state.orders_page,
            page_size=ORDERS_PAGE_SIZE,
        )
        orders_total_pages = max(1, (orders_total + ORDERS_PAGE_SIZE - 1) // ORDERS_PAGE_SIZE)

        if not orders_page_rows:
            st.info(T("orders.no_orders_yet"))
        else:
            table_rows = []
            for o in orders_page_rows:
                table_rows.append({
                    "id": o.id,
                    "order_number": o.order_number or T("orders.none_placeholder"),
                    "customer_name": o.customer_name or "—",
                    "items_summary": o.items_summary or "—",
                    "price_total": o.price_total or 0,
                    "shipping_method": o.shipping_method or "—",
                    "order_date_label": (
                        time.strftime("%Y-%m-%d", time.localtime(o.order_date)) if o.order_date else "—"
                    ),
                    "status_label": o.status_label or "—",
                    "marketplace": o.marketplace or "—",
                })

            orders_table_result = review_table(
                rows=table_rows,
                columns=_translated_columns(_ORDER_TABLE_COLUMNS),
                mobile_fields=_ORDER_TABLE_MOBILE_FIELDS,
                state_key="orders",
                clear_seq=st.session_state.orders_clear_seq,
                key="orders_table",
            )
            if (
                orders_table_result
                and orders_table_result.get("open_id")
                and orders_table_result["open_id"] != st.session_state.orders_open_id
            ):
                st.session_state.orders_open_id = orders_table_result["open_id"]
                st.rerun()

            # Simple previous/next pagination — Orders has no bulk-action
            # toolbar to anchor Product List's fuller numbered widget against.
            pg_prev, pg_label, pg_next = st.columns([1, 2, 1])
            with pg_prev:
                if st.button(T("product_list.prev_simple"), disabled=st.session_state.orders_page <= 1, use_container_width=True):
                    st.session_state.orders_page -= 1
                    st.rerun()
            with pg_label:
                st.markdown(
                    f"<div style='text-align:center;padding-top:6px;'>"
                    f"{T('orders.page_of', page=st.session_state.orders_page, total_pages=orders_total_pages, count=orders_total)}</div>",
                    unsafe_allow_html=True,
                )
            with pg_next:
                if st.button(
                    T("product_list.next_simple"), disabled=st.session_state.orders_page >= orders_total_pages,
                    use_container_width=True,
                ):
                    st.session_state.orders_page += 1
                    st.rerun()

    else:
        # ------------------------------------------------------ DETAIL VIEW --
        order = order_store.get_order(st.session_state.orders_open_id, st.session_state.company_id)
        if order is None:
            st.warning(T("orders.order_not_found"))
            if st.button(T("common.back_to_list")):
                st.session_state.orders_open_id = None
                st.rerun()
        else:
            if st.button(T("common.back_to_list")):
                st.session_state.orders_open_id = None
                st.rerun()

            st.subheader(T("orders.order_heading", ref=order.order_number or order.external_order_id))
            _source_caption = " · ".join(x for x in [order.marketplace, order.order_source] if x)
            if _source_caption:
                st.caption(_source_caption)

            info_col, ship_col = st.columns(2)
            with info_col:
                st.markdown(f"**{T('orders.customer')}**")
                st.write(order.customer_name or "—")
                if order.email:
                    st.write(f"✉️ {order.email}")
                if order.phone:
                    st.write(f"📞 {order.phone}")
                if order.customer_comments:
                    st.markdown(f"**{T('orders.customer_comments')}**")
                    st.write(order.customer_comments)
            with ship_col:
                st.markdown(f"**{T('common.status')}**")
                st.write(order.status_label or "—")
                st.markdown(f"**{T('orders.order_date')}**")
                st.write(
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(order.order_date))
                    if order.order_date else "—"
                )
                st.markdown(f"**{T('orders.shipping_method')}**")
                st.write(order.shipping_method or "—")

            def _render_order_address(addr):
                lines = [
                    addr.full_name, addr.company, addr.address,
                    " ".join(x for x in [addr.postcode, addr.city] if x), addr.state, addr.country,
                ]
                rendered = False
                for line in lines:
                    if line:
                        st.write(line)
                        rendered = True
                if not rendered:
                    st.write("—")

            addr_col1, addr_col2 = st.columns(2)
            with addr_col1:
                st.markdown(f"**{T('orders.delivery_address')}**")
                _render_order_address(order.delivery_address)
            with addr_col2:
                st.markdown(f"**{T('orders.invoice_address')}**")
                _render_order_address(order.invoice_address)

            st.divider()
            st.markdown(f"**{T('orders.items')}**")
            if not order.items:
                st.caption(T("orders.no_item_detail"))
            for item in order.items:
                item_cols = st.columns([3, 2, 1, 1, 1])
                with item_cols[0]:
                    st.write(item.name or "—")
                with item_cols[1]:
                    if item.sku:
                        linked_product = inventory_store.get_product_by_sku(st.session_state.company_id, item.sku)
                        if linked_product:
                            if st.button(item.sku, key=f"order_item_sku_{order.id}_{item.sku}"):
                                st.session_state["_pending_nav_page"] = PAGE_PRODUCT_LIST
                                st.session_state.rex_open_product_id = linked_product.id
                                st.rerun()
                        else:
                            st.write(item.sku)
                    else:
                        st.write("—")
                with item_cols[2]:
                    st.write(f"{item.quantity}x")
                with item_cols[3]:
                    st.write(f"{item.price:.2f} {order.currency}".strip())
                with item_cols[4]:
                    st.write(f"{item.price * item.quantity:.2f} {order.currency}".strip())

            st.divider()
            st.markdown(f"**{T('orders.total', amount=f'{order.price_total:.2f}', currency=order.currency)}**".strip())

elif page == PAGE_MANAGE_USERS:
    st.title(T("nav.manage_users"))
    try:
        auth.require_role(current_user, auth.ROLE_ADMIN)
    except PermissionError:
        st.error(T("common.admins_only"))
        st.stop()

    st.caption(T("manage_users.users_in", company=current_company.name if current_company else current_user.company_id))

    users = auth_store.list_users_for_company(current_user.company_id)
    for u in users:
        c1, c2, c3, c4 = st.columns([2, 3, 1.5, 1.5])
        with c1:
            st.write(u.name)
        with c2:
            st.write(u.email)
        with c3:
            st.write(T(f"role.{u.role}"))
        with c4:
            toggle_label = T("manage_users.deactivate") if u.active else T("manage_users.activate")
            disabled = u.id == current_user.id  # can't deactivate yourself
            if st.button(toggle_label, key=f"user_toggle_{u.id}", disabled=disabled, use_container_width=True):
                u.active = not u.active
                auth_store.update_user(u)
                if not u.active:
                    audit_store.log_audit(current_user.company_id, current_user.id, "DEACTIVATE_USER", "user", u.id)
                else:
                    audit_store.log_audit(
                        current_user.company_id, current_user.id, "UPDATE_USER", "user", u.id, "reactivated",
                    )
                st.rerun()
        if not u.active:
            st.caption(T("manage_users.inactive"))
        st.divider()

    st.subheader(T("manage_users.add_user"))
    with st.form("create_user_form", clear_on_submit=True):
        new_name = st.text_input(T("common.name"))
        new_email = st.text_input(T("common.email"))
        new_password = st.text_input(T("common.password"), type="password")
        new_role = st.selectbox(
            T("common.role"), auth.ALL_ROLES, index=auth.ALL_ROLES.index(auth.ROLE_EMPLOYEE),
            format_func=lambda r: T(f"role.{r}"),
        )
        if st.form_submit_button(T("manage_users.add_user_button"), type="primary"):
            if not new_name.strip() or not new_email.strip() or not new_password:
                st.warning(T("manage_users.all_fields_required"))
            else:
                try:
                    auth.register_user(
                        company_id=current_user.company_id,
                        name=new_name, email=new_email, password=new_password, role=new_role,
                    )
                    st.success(T("manage_users.added_user", email=new_email))
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

elif page == PAGE_SETTINGS:
    st.title(T("nav.settings"))
    try:
        auth.require_role(current_user, auth.ROLE_ADMIN)
    except PermissionError:
        st.error(T("common.admins_only"))
        st.stop()

    tab_integrations, tab_translation = st.tabs(
        [T("settings.integrations_tab"), T("settings.translation_tab")]
    )

    with tab_integrations:
        _company_label = current_company.name if current_company else current_user.company_id
        _catalog_by_type = {e.integration_type: e for e in CATALOG}

        st.markdown(
            """
            <style>
            div[class*="st-key-integration_card_"], div[class*="st-key-catalog_card_"] {
                border-radius: 12px !important;
                box-shadow: 0 1px 3px rgba(0,0,0,0.08);
                transition: box-shadow 0.15s ease, transform 0.15s ease;
            }
            div[class*="st-key-integration_card_"]:hover, div[class*="st-key-catalog_card_"]:hover {
                box-shadow: 0 6px 16px rgba(0,0,0,0.14);
                transform: translateY(-2px);
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        # ---- status/health (derived from existing data, no new DB columns) --
        _HEALTH_STYLES = {
            "connected": (T("settings.health_connected"), "#16a34a", "🟢"),
            "attention": (T("settings.health_attention"), "#eab308", "🟡"),
            "failed": (T("settings.health_failed"), "#dc2626", "🔴"),
            "never_synced": (T("settings.health_never_synced"), "#9ca3af", "⚪"),
        }

        def _integration_health(record) -> tuple:
            if record is None or record.status == integration_store.STATUS_ERROR:
                return _HEALTH_STYLES["failed"]
            if not record.last_sync_at:
                return _HEALTH_STYLES["never_synced"]
            latest = integration_store.list_sync_log(record.company_id, record.integration_type, limit=1)
            if latest and latest[0].status == integration_store.SYNC_STATUS_ERROR:
                return _HEALTH_STYLES["attention"]
            return _HEALTH_STYLES["connected"]

        def _integration_account_label(integration_type: str, record) -> str:
            if record and integration_type == "baselinker" and record.settings.get("inventory_id"):
                return T("settings.inventory_label", inventory_id=record.settings['inventory_id'])
            return ""

        # ---- logo: real file if dropped in later, styled wordmark otherwise --
        _LOGO_STYLES = {
            "baselinker": {"parts": [("base", "#111111"), (".", "#2f6fed")], "weight": 800},
            "ebay": {"parts": [("e", "#e53238"), ("b", "#0064d2"), ("a", "#f5af02"), ("y", "#86b817")], "weight": 800},
            "amazon": {"parts": [("amazon", "#111111")], "weight": 700},
            "allegro": {"parts": [("allegro", "#ff5a00")], "weight": 800},
            "tradera": {"parts": [("tradera", "#1a7a3c")], "weight": 800},
            "woocommerce": {"parts": [("woo", "#7f54b3")], "weight": 800},
            "deepl": {"parts": [("Deep", "#0f2b46"), ("L", "#0f6fff")], "weight": 800},
            "openai": {"parts": [("AI Assistant", "#111111")], "weight": 700},
            "dhl": {"parts": [("DHL", "#d40511")], "weight": 900},
            "dpd": {"parts": [("DPD", "#dc0032")], "weight": 900},
        }

        def _render_integration_logo(integration_type: str, height_px: int = 40) -> None:
            logo_path = INTEGRATION_LOGOS_DIR / f"{integration_type}.png"
            if logo_path.exists():
                st.image(str(logo_path), width=height_px * 3)
                return
            style = _LOGO_STYLES.get(integration_type)
            parts = style["parts"] if style else [(integration_type.replace("_", " ").title(), "#6b7280")]
            weight = style["weight"] if style else 600
            spans = "".join(f'<span style="color:{color};">{text}</span>' for text, color in parts)
            # Brand wordmarks are colored for a light background (matching how
            # every one of these logos is normally displayed) — always render
            # them inside a white box so they stay legible regardless of the
            # app's own (often dark) theme.
            st.markdown(
                f'<div style="background:#ffffff; border-radius:8px; padding:8px 12px; '
                f'display:inline-block;"><div style="font-size:{height_px * 0.5}px; '
                f"font-weight:{weight}; font-family:-apple-system,BlinkMacSystemFont,"
                f"'Segoe UI',sans-serif; line-height:1.2; white-space:nowrap;\">{spans}</div></div>",
                unsafe_allow_html=True,
            )

        @st.dialog(T("settings.disconnect_dialog_title"))
        def _confirm_disconnect_integration_dialog(integration_type: str, display_name: str):
            st.warning(T("settings.disconnect_warning", name=display_name, company=_company_label))
            confirm = st.checkbox(T("settings.disconnect_confirm_checkbox"), key=f"disconnect_confirm_{integration_type}")
            dcol1, dcol2 = st.columns(2)
            with dcol1:
                if st.button(T("common.cancel"), use_container_width=True, key=f"disconnect_cancel_{integration_type}"):
                    st.rerun()
            with dcol2:
                if st.button(
                    T("settings.disconnect_button"), type="primary", disabled=not confirm,
                    use_container_width=True, key=f"disconnect_go_{integration_type}",
                ):
                    IntegrationManager.disconnect(current_user.company_id, integration_type, user_id=current_user.id)
                    st.success(T("settings.disconnected_success", name=display_name))
                    st.rerun()

        def _render_integration_activity(integration_type: str) -> None:
            entries = integration_store.list_sync_log(current_user.company_id, integration_type, limit=10)
            with st.expander(T("settings.recent_activity")):
                if not entries:
                    st.caption(T("settings.no_activity_yet"))
                for e in entries:
                    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.created_at)) if e.created_at else "—"
                    if e.status == integration_store.SYNC_STATUS_SUCCESS:
                        icon = "✅"
                    elif e.status == integration_store.SYNC_STATUS_SKIPPED:
                        icon = "⏭️"
                    else:
                        icon = "❌"
                    line = f"{icon} {ts} — {e.action}"
                    if e.product_id:
                        line += f" ({T('settings.product_word')} {e.product_id})"
                    if e.error_message:
                        line += f": {e.error_message}"
                    st.caption(line)

        def _render_integration_test_and_disconnect(entry, connected: bool) -> None:
            if not connected:
                return
            tcol1, tcol2 = st.columns(2)
            with tcol1:
                if st.button(T("settings.test_connection"), key=f"test_{entry.integration_type}", use_container_width=True):
                    result = IntegrationManager.test(current_user.company_id, entry.integration_type)
                    (st.success if result.success else st.error)(result.message)
            with tcol2:
                if st.button(T("settings.disconnect_button"), key=f"disconnect_open_{entry.integration_type}", use_container_width=True):
                    _confirm_disconnect_integration_dialog(entry.integration_type, entry.display_name)

        def _render_catalog_import_section(entry) -> None:
            """Generic bulk catalog import — works for ANY connected
            marketplace connector via the universal fetch_catalog()
            interface (integrations/base.py). Nothing here is BaseLinker-
            specific; a future eBay/Amazon/Tradera connector shows up here
            automatically the moment it implements fetch_catalog().

            The actual import runs in the background (_import_executor()) —
            clicking "Import" starts the job and returns immediately, so
            this browser session (and every other user) stays fully usable
            while it runs. Progress persists in catalog_import_job_store,
            not session_state, so navigating away and back still shows the
            current state correctly."""
            st.divider()
            st.markdown(f"**{T('settings.import_catalog_heading', name=entry.display_name)}**")
            st.caption(T("settings.import_catalog_caption", name=entry.display_name))

            job = catalog_import_job_store.get_job(current_user.company_id, entry.integration_type)
            if job is not None and job.status == catalog_import_job_store.STATUS_RUNNING:
                _render_import_progress(current_user.company_id, entry.integration_type)
                return

            if job is not None and job.status in (catalog_import_job_store.STATUS_SUCCESS, catalog_import_job_store.STATUS_FAILED):
                if job.errors:
                    st.warning(T("settings.last_import_errors", imported=job.imported, skipped=job.skipped, error_count=job.error_count, errors='; '.join(job.errors[:5])))
                else:
                    st.success(T("settings.last_import_success", imported=job.imported, skipped=job.skipped))

            preview_key = f"catalog_import_preview_{entry.integration_type}"
            if st.button(T("settings.preview_import"), key=f"catalog_import_preview_btn_{entry.integration_type}"):
                st.session_state[preview_key] = catalog_import.preview_import(
                    current_user.company_id, entry.integration_type,
                )

            preview = st.session_state.get(preview_key)
            if preview is not None:
                n_new, n_existing = len(preview["new"]), len(preview["existing"])
                if n_new == 0 and n_existing == 0:
                    st.info(T("settings.nothing_to_import", name=entry.display_name))
                else:
                    st.write(T("settings.import_summary", new=n_new, existing=n_existing))
                    if n_new > 0 and st.button(T("settings.import_n_products", count=n_new), key=f"catalog_import_run_{entry.integration_type}", type="primary"):
                        catalog_import_job_store.start_job(current_user.company_id, entry.integration_type, n_new)
                        _import_executor().submit(
                            _run_import_job, current_user.company_id, entry.integration_type, current_user.id,
                        )
                        st.session_state[preview_key] = None
                        st.rerun()

        def _render_baselinker_settings(entry, record) -> None:
            connected = record is not None and record.status == integration_store.STATUS_CONNECTED
            has_credentials = record is not None and bool(record.credentials)
            if record and record.last_sync_at:
                st.caption(T("settings.last_sync", ts=time.strftime('%Y-%m-%d %H:%M', time.localtime(record.last_sync_at))))
            settings = record.settings if record else {}
            with st.form(f"integration_form_{entry.integration_type}"):
                token = st.text_input(
                    T("settings.api_token"), type="password",
                    placeholder=T("settings.leave_blank_current") if has_credentials else "",
                    help=T("settings.baselinker_token_help"),
                )
                inventory_id = st.text_input(T("settings.inventory_id"), value=settings.get("inventory_id") or "")
                category_id = st.text_input(T("settings.category_id_field"), value=settings.get("category_id") or "")
                price_group_id = st.text_input(T("settings.price_group_id"), value=settings.get("price_group_id") or "")
                warehouse_id = st.text_input(T("settings.warehouse_id"), value=settings.get("warehouse_id") or "")
                tax_rate = st.text_input(T("settings.tax_rate"), value=settings.get("tax_rate") or "23")
                _export_lang_options = [""] + list(company_store.CONTENT_LANGUAGES.keys())
                _current_export_lang = settings.get("export_language") or ""
                export_language = st.selectbox(
                    T("settings.export_language"),
                    options=_export_lang_options,
                    format_func=lambda code: (
                        T("settings.export_language_use_default") if code == ""
                        else company_store.CONTENT_LANGUAGES[code]
                    ),
                    index=_export_lang_options.index(_current_export_lang) if _current_export_lang in _export_lang_options else 0,
                    help=T("settings.export_language_help"),
                )
                submitted = st.form_submit_button(T("settings.save_test_connection"), type="primary")
                if submitted:
                    if not token and not has_credentials:
                        st.warning(T("settings.api_token_required"))
                    elif not inventory_id.strip() or not category_id.strip():
                        st.warning(T("settings.inventory_category_required"))
                    else:
                        creds = {"token": token} if token else dict(record.credentials)
                        new_settings = {
                            "inventory_id": inventory_id.strip(),
                            "category_id": category_id.strip(),
                            "price_group_id": price_group_id.strip(),
                            "warehouse_id": warehouse_id.strip(),
                            "tax_rate": tax_rate.strip() or "23",
                            "export_language": export_language,
                        }
                        result = IntegrationManager.connect(
                            current_user.company_id, entry.integration_type, creds, new_settings,
                            user_id=current_user.id,
                        )
                        (st.success if result.success else st.error)(result.message)
                        st.rerun()

        def _render_deepl_settings(entry, record) -> None:
            connected = record is not None and record.status == integration_store.STATUS_CONNECTED
            has_credentials = record is not None and bool(record.credentials)
            if record and record.last_sync_at:
                st.caption(T("settings.last_sync", ts=time.strftime('%Y-%m-%d %H:%M', time.localtime(record.last_sync_at))))
            with st.form(f"integration_form_{entry.integration_type}"):
                api_key = st.text_input(
                    T("settings.api_key"), type="password",
                    placeholder=T("settings.leave_blank_current") if has_credentials else "",
                )
                submitted = st.form_submit_button(T("settings.save_test_connection"), type="primary")
                if submitted:
                    if not api_key and not has_credentials:
                        st.warning(T("settings.api_key_required"))
                    else:
                        creds = {"api_key": api_key} if api_key else dict(record.credentials)
                        result = IntegrationManager.connect(
                            current_user.company_id, entry.integration_type, creds, {},
                            user_id=current_user.id,
                        )
                        (st.success if result.success else st.error)(result.message)
                        st.rerun()

        def _render_openai_settings(entry, record) -> None:
            connected = record is not None and record.status == integration_store.STATUS_CONNECTED
            has_credentials = record is not None and bool(record.credentials)
            if record and record.last_sync_at:
                st.caption(T("settings.last_sync", ts=time.strftime('%Y-%m-%d %H:%M', time.localtime(record.last_sync_at))))
            with st.form(f"integration_form_{entry.integration_type}"):
                api_key = st.text_input(
                    T("settings.api_key"), type="password",
                    placeholder=T("settings.leave_blank_current") if has_credentials else "",
                )
                submitted = st.form_submit_button(T("settings.save_test_connection"), type="primary")
                if submitted:
                    if not api_key and not has_credentials:
                        st.warning(T("settings.api_key_required"))
                    else:
                        creds = {"api_key": api_key} if api_key else dict(record.credentials)
                        result = IntegrationManager.connect(
                            current_user.company_id, entry.integration_type, creds, {},
                            user_id=current_user.id,
                        )
                        (st.success if result.success else st.error)(result.message)
                        st.rerun()

        _INTEGRATION_SETTINGS_RENDERERS = {
            "baselinker": _render_baselinker_settings,
            "deepl": _render_deepl_settings,
            "openai": _render_openai_settings,
        }

        _UI_GROUPS = ["Marketplace", "Store", "Shipping", "ERP", "Accounting", "Payments", "AI", "Communication", "Other"]

        # ---- Synchronization / Field Mapping / Automation tab registries --
        # Data-sent/received field lists now come from integrations/
        # field_registry.py — the single shared source of truth for every
        # ElectroGrader field any integration could sync (Synchronization
        # checkboxes, Field Mapping's "Source field" dropdown, Preview all
        # read the same dicts, so a future integration never needs its own
        # copy of this list).
        _SYNC_FIELDS_SENT = [(k, v["label"]) for k, v in field_registry.SYNCABLE_FIELDS.items()]
        _SYNC_FIELDS_RECEIVED = [(k, v["label"]) for k, v in field_registry.RECEIVABLE_FIELDS.items()]
        _FREQUENCY_OPTIONS = [
            (sync_rules_store.FREQUENCY_MANUAL, T("settings.freq_manual")),
            (sync_rules_store.FREQUENCY_EVERY_15_MIN, T("settings.freq_15min")),
            (sync_rules_store.FREQUENCY_HOURLY, T("settings.freq_hourly")),
            (sync_rules_store.FREQUENCY_DAILY, T("settings.freq_daily")),
        ]
        _DIRECTION_OPTIONS = [
            (sync_rules_store.DIRECTION_PUSH, T("settings.dir_push")),
            (sync_rules_store.DIRECTION_PULL, T("settings.dir_pull")),
            (sync_rules_store.DIRECTION_TWO_WAY, T("settings.dir_two_way")),
        ]
        _CONFLICT_OPTIONS = [
            (sync_rules_store.CONFLICT_KEEP_LOCAL, T("settings.conflict_keep_local")),
            (sync_rules_store.CONFLICT_KEEP_REMOTE, T("settings.conflict_keep_remote")),
            (sync_rules_store.CONFLICT_ASK_USER, T("settings.conflict_ask_me")),
        ]
        # (trigger_event, job_type, checkbox label) — Phase 1's fixed starter
        # set; the underlying storage (SyncRule.automation_triggers) is a
        # JSON list, not fixed columns, so more triggers can be added later
        # without a migration.
        _AUTOMATION_TRIGGER_DEFAULTS = [
            ("product_completed", sync_job_store.JOB_TYPE_PRODUCT_EXPORT, T("settings.trigger_product_completed")),
            ("product_updated", sync_job_store.JOB_TYPE_PRODUCT_EXPORT, T("settings.trigger_product_updated")),
            ("stock_changed", sync_job_store.JOB_TYPE_LISTING_END, T("settings.trigger_stock_changed")),
        ]

        def _render_synchronization_tab(entry, rule: sync_rules_store.SyncRule) -> None:
            implemented = IntegrationManager.get_implemented_sync_fields(entry.integration_type)
            st.caption(T("settings.sync_tab_caption"))
            st.markdown(f"**{T('settings.data_sent')}**")
            send_cols = st.columns(3)
            new_fields_send = []
            for i, (key, label) in enumerate(_SYNC_FIELDS_SENT):
                with send_cols[i % 3]:
                    checkbox_label = label if key in implemented else f"{label} _({T('settings.not_yet_applied')})_"
                    if st.checkbox(
                        checkbox_label, value=key in rule.fields_send,
                        key=f"sync_send_{entry.integration_type}_{key}",
                    ):
                        new_fields_send.append(key)

            st.markdown(f"**{T('settings.data_received')}**")
            recv_cols = st.columns(3)
            new_fields_receive = []
            for i, (key, label) in enumerate(_SYNC_FIELDS_RECEIVED):
                with recv_cols[i % 3]:
                    if st.checkbox(
                        label, value=key in rule.fields_receive, key=f"sync_recv_{entry.integration_type}_{key}",
                    ):
                        new_fields_receive.append(key)

            st.divider()
            st.markdown(f"**{T('settings.sync_rules_heading')}**")
            freq_keys = [k for k, _ in _FREQUENCY_OPTIONS]
            frequency = st.selectbox(
                T("settings.frequency"), freq_keys,
                index=freq_keys.index(rule.frequency) if rule.frequency in freq_keys else 0,
                format_func=lambda k: dict(_FREQUENCY_OPTIONS)[k], key=f"sync_freq_{entry.integration_type}",
            )
            if frequency != sync_rules_store.FREQUENCY_MANUAL:
                st.caption(T("settings.phase1_automation_note"))
            dir_keys = [k for k, _ in _DIRECTION_OPTIONS]
            direction = st.selectbox(
                T("settings.direction"), dir_keys,
                index=dir_keys.index(rule.direction) if rule.direction in dir_keys else 0,
                format_func=lambda k: dict(_DIRECTION_OPTIONS)[k], key=f"sync_dir_{entry.integration_type}",
            )
            conflict_keys = [k for k, _ in _CONFLICT_OPTIONS]
            conflict_handling = st.selectbox(
                T("settings.conflict_handling"), conflict_keys,
                index=conflict_keys.index(rule.conflict_handling) if rule.conflict_handling in conflict_keys else 0,
                format_func=lambda k: dict(_CONFLICT_OPTIONS)[k], key=f"sync_conflict_{entry.integration_type}",
            )

            if st.button(T("settings.save_sync_settings"), key=f"sync_save_{entry.integration_type}", type="primary"):
                rule.fields_send = new_fields_send
                rule.fields_send_configured = True  # a real Save always counts as "configured",
                # even if the admin deliberately leaves every box unchecked.
                rule.fields_receive = new_fields_receive
                rule.frequency = frequency
                rule.direction = direction
                rule.conflict_handling = conflict_handling
                sync_rules_store.upsert_rule(rule)
                st.success(T("product_list.saved"))
                st.rerun()

        def _render_field_mapping_tab(entry, mapping: field_mapping_store.FieldMapping) -> None:
            target_fields = IntegrationManager.get_supported_target_fields(entry.integration_type)
            if not target_fields:
                st.info(T("settings.no_mappable_fields", name=entry.display_name))
                return

            st.caption(T("settings.field_mapping_caption"))
            search = st.text_input(T("settings.search_mapping_rules"), key=f"mapping_search_{entry.integration_type}")
            source_field_keys = [k for k, _ in _SYNC_FIELDS_SENT]

            df = pd.DataFrame(
                [
                    {
                        "Source field": r.source_field, "Source value": r.source_value,
                        "Target field": r.target_field, "Target value": r.target_value,
                        "Target label": r.target_label,
                    }
                    for r in mapping.rules
                ],
                columns=["Source field", "Source value", "Target field", "Target value", "Target label"],
            )

            q = search.strip().lower()
            if q:
                mask = df.apply(lambda row: q in " ".join(str(v) for v in row).lower(), axis=1)
                st.dataframe(df[mask], use_container_width=True, hide_index=True)
                st.caption(T("settings.clear_search_to_edit"))
                return

            edited = st.data_editor(
                df, num_rows="dynamic", use_container_width=True, hide_index=True,
                key=f"mapping_editor_{entry.integration_type}",
                column_config={
                    "Source field": st.column_config.SelectboxColumn(options=source_field_keys, required=True),
                    "Target field": st.column_config.SelectboxColumn(options=list(target_fields.keys()), required=True),
                },
            )
            if st.button(T("settings.save_field_mapping"), key=f"mapping_save_{entry.integration_type}", type="primary"):
                mapping.rules = [
                    field_mapping_store.FieldMappingRule(
                        source_field=row.get("Source field") or "", source_value=row.get("Source value") or "",
                        target_field=row.get("Target field") or "", target_value=row.get("Target value") or "",
                        target_label=row.get("Target label") or "",
                    )
                    for _, row in edited.iterrows()
                    if row.get("Source field") and row.get("Target field")
                ]
                field_mapping_store.upsert_mapping(mapping)
                st.success(T("product_list.saved"))
                st.rerun()

        def _render_sync_ownership_tab(entry) -> None:
            st.caption(T("settings.ownership_caption"))
            source_options = ["electrograder", entry.integration_type, "manual"]
            source_labels = {
                "electrograder": "ElectroGrader", entry.integration_type: entry.display_name, "manual": T("settings.manual_word"),
            }
            policy_options = ["electrograder", entry.integration_type, "manual_review"]
            policy_labels = {
                "electrograder": "ElectroGrader", entry.integration_type: entry.display_name,
                "manual_review": T("settings.manual_review"),
            }
            config = sync_ownership_store.list_field_config(current_user.company_id, entry.integration_type)

            # Field Sync Status: most recent sync_logs entry per field, so
            # each row can show "Last sync" / "Status" next to its ownership
            # controls (spec's Field Sync Status mockup) — one query for all
            # 12 fields rather than one per row.
            _latest_log_by_field: dict = {}
            for log in sync_log_store.list_logs(current_user.company_id, connector_name=entry.integration_type, limit=200):
                if log.field_name not in _latest_log_by_field:
                    _latest_log_by_field[log.field_name] = log

            _group_labels = {"Product Content": T("settings.group_product_content"), "Sales Data": T("settings.group_sales_data")}
            new_choices = {}
            for group in ("Product Content", "Sales Data"):
                st.markdown(f"**{_group_labels[group]}**")
                fields_in_group = [
                    (key, meta) for key, meta in field_registry.SYNC_OWNERSHIP_FIELDS.items() if meta["group"] == group
                ]
                for key, meta in fields_in_group:
                    row_col1, row_col2, row_col3, row_col4, row_col5 = st.columns([2, 2, 2, 1, 2])
                    current_cfg = config[key]
                    with row_col1:
                        st.write(meta["label"])
                    with row_col2:
                        chosen = st.selectbox(
                            meta["label"], source_options,
                            index=source_options.index(current_cfg.source_system)
                            if current_cfg.source_system in source_options else 0,
                            format_func=lambda k: source_labels[k],
                            key=f"ownership_source_{entry.integration_type}_{key}",
                            label_visibility="collapsed",
                        )
                    with row_col3:
                        chosen_policy = st.selectbox(
                            f"{meta['label']} conflict policy", policy_options,
                            index=policy_options.index(current_cfg.conflict_policy)
                            if current_cfg.conflict_policy in policy_options else 0,
                            format_func=lambda k: policy_labels[k],
                            key=f"ownership_policy_{entry.integration_type}_{key}",
                            label_visibility="collapsed",
                        )
                    with row_col4:
                        enabled = st.checkbox(
                            T("settings.enabled_word"), value=current_cfg.sync_enabled,
                            key=f"ownership_enabled_{entry.integration_type}_{key}",
                        )
                    with row_col5:
                        last_log = _latest_log_by_field.get(key)
                        if last_log is None:
                            st.caption(T("settings.last_sync_dash"))
                        else:
                            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_log.created_at))
                            status_label = T("settings.pending_review") if last_log.new_value == "(pending review)" else T("settings.success_word")
                            st.caption(T("settings.last_sync_status", ts=ts, status=status_label))
                    if sync_ownership_store.is_conflicting_configuration(chosen, chosen_policy):
                        st.warning(T("settings.conflict_policy_warning", policy=policy_labels[chosen_policy], owner=source_labels[chosen]))
                    new_choices[key] = (chosen, chosen_policy, enabled)

            if st.button(T("settings.save_sync_ownership"), key=f"ownership_save_{entry.integration_type}", type="primary"):
                for field_name, (source_system, conflict_policy, sync_enabled) in new_choices.items():
                    sync_ownership_store.upsert_field_config(
                        current_user.company_id, entry.integration_type, field_name, source_system,
                        sync_enabled, conflict_policy,
                    )
                audit_store.log_audit(
                    current_user.company_id, current_user.id, "UPDATE_SYNC_OWNERSHIP",
                    "integration", entry.integration_type,
                )
                st.success(T("product_list.saved"))
                st.rerun()

        def _render_automation_tab(entry, rule: sync_rules_store.SyncRule) -> None:
            st.caption(T("settings.automation_future_work_note"))
            triggers_by_event = {t.get("trigger_event"): t for t in rule.automation_triggers}
            new_triggers = []
            for trigger_event, job_type, label in _AUTOMATION_TRIGGER_DEFAULTS:
                existing = triggers_by_event.get(trigger_event, {})
                enabled = st.checkbox(
                    label, value=bool(existing.get("enabled", False)),
                    key=f"automation_{entry.integration_type}_{trigger_event}",
                )
                new_triggers.append({"trigger_event": trigger_event, "job_type": job_type, "enabled": enabled})

            if st.button(T("settings.save_automation_settings"), key=f"automation_save_{entry.integration_type}", type="primary"):
                rule.automation_triggers = new_triggers
                sync_rules_store.upsert_rule(rule)
                st.success(T("product_list.saved"))
                st.rerun()

            st.divider()
            st.markdown(f"**{T('settings.automatic_sync_heading')}**")
            st.caption(T("settings.automatic_sync_caption", name=entry.display_name))
            auto_enabled = st.checkbox(
                T("settings.enable_automatic_sync"), value=rule.auto_sync_enabled,
                key=f"auto_sync_enabled_{entry.integration_type}",
            )
            auto_col1, auto_col2 = st.columns(2)
            with auto_col1:
                push_interval = st.number_input(
                    T("settings.push_interval"), min_value=10, value=rule.push_interval_seconds or 60,
                    key=f"push_interval_{entry.integration_type}",
                )
            with auto_col2:
                pull_interval = st.number_input(
                    T("settings.pull_interval"), min_value=10, value=rule.pull_interval_seconds or 300,
                    key=f"pull_interval_{entry.integration_type}",
                )
            if st.button(T("settings.save_auto_sync_settings"), key=f"auto_sync_save_{entry.integration_type}"):
                rule.auto_sync_enabled = auto_enabled
                rule.push_interval_seconds = int(push_interval)
                rule.pull_interval_seconds = int(pull_interval)
                sync_rules_store.upsert_rule(rule)
                audit_store.log_audit(
                    current_user.company_id, current_user.id, "UPDATE_AUTO_SYNC_SETTINGS",
                    "integration", entry.integration_type,
                    f"auto_sync_enabled={auto_enabled}",
                )
                st.success(T("product_list.saved"))
                st.rerun()

        def _render_logs_tab(entry) -> None:
            _render_integration_activity(entry.integration_type)
            st.markdown(f"**{T('settings.scheduled_sync_jobs')}**")
            jobs = sync_job_store.list_jobs(current_user.company_id, entry.integration_type, limit=10)
            if not jobs:
                st.caption(T("settings.no_scheduled_jobs"))
            job_icons = {
                sync_job_store.STATUS_SUCCESS: "✅", sync_job_store.STATUS_ERROR: "❌",
                sync_job_store.STATUS_SKIPPED: "⏭️", sync_job_store.STATUS_RETRYING: "🔁",
                sync_job_store.STATUS_PENDING: "⏳", sync_job_store.STATUS_RUNNING: "⚙️",
            }
            for j in jobs:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(j.created_at)) if j.created_at else "—"
                icon = job_icons.get(j.status, "•")
                st.caption(f"{icon} {ts} — {j.job_type} ({j.status}, attempt {j.attempts}/{j.max_attempts})")

            st.markdown(f"**{T('settings.field_sync_history')}**")
            logs = sync_log_store.list_logs(current_user.company_id, connector_name=entry.integration_type, limit=10)
            if not logs:
                st.caption(T("settings.no_field_sync_history"))
            for log in logs:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(log.created_at)) if log.created_at else "—"
                st.caption(
                    f"{ts} — {log.field_name}: {log.old_value or '—'} → {log.new_value or '—'} "
                    f"({T('settings.from_source', source=log.source, direction=log.direction)})"
                )

            st.markdown(f"**{T('settings.pending_conflicts')}**")
            pending = [c for c in sync_ownership_store.list_conflicts(current_user.company_id) if not c.resolution]
            if not pending:
                st.caption(T("settings.no_pending_conflicts"))
            for c in pending:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.timestamp)) if c.timestamp else "—"
                st.caption(
                    f"{ts} — product {c.product_id}, field {c.field_name}: "
                    f"ElectroGrader={c.electrograder_value or '—'} vs {entry.display_name}={c.baselinker_value or '—'}"
                )

        def _render_catalog_card(entry) -> None:
            with st.container(border=True, key=f"catalog_card_{entry.integration_type}"):
                _render_integration_logo(entry.integration_type)
                st.markdown(f"**{entry.display_name}**")
                st.caption(entry.description)
                if not entry.available:
                    st.caption(T("settings.coming_soon"))
                    return
                record = integration_store.get_integration(current_user.company_id, entry.integration_type)
                if record is not None and record.status == integration_store.STATUS_CONNECTED:
                    st.caption(T("settings.connected_check"))
                btn_label = T("settings.edit_word") if record is not None else T("settings.connect_word")
                if st.button(btn_label, key=f"catalog_btn_{entry.integration_type}", use_container_width=True):
                    st.session_state.settings_open_integration = entry.integration_type
                    st.rerun()

        # _UI_GROUPS/entry.ui_group values (Marketplace/Store/Shipping/...)
        # come from integrations/manager.py's CATALOG data and stay literal
        # English there — this map translates only the on-screen label,
        # never the comparison value itself.
        _UI_GROUP_LABELS = {
            "All": T("settings.category_all"), "Marketplace": T("settings.category_marketplace"),
            "Store": T("settings.category_store"), "Shipping": T("settings.category_shipping"),
            "ERP": "ERP", "Accounting": T("settings.category_accounting"),
            "Payments": T("settings.category_payments"), "AI": "AI",
            "Communication": T("settings.category_communication"), "Other": T("settings.category_other"),
        }

        @st.dialog(T("settings.add_integration_title"), width="large")
        def _add_integration_dialog():
            search = st.text_input(T("settings.search_integrations"), key="add_integration_search")
            selected_group = st.pills(
                T("settings.category_label"), ["All"] + _UI_GROUPS, selection_mode="single",
                default="All", key="add_integration_group", format_func=lambda g: _UI_GROUP_LABELS.get(g, g),
            ) or "All"

            q = search.strip().lower()

            def _matches(entry) -> bool:
                if selected_group != "All" and entry.ui_group != selected_group:
                    return False
                if not q:
                    return True
                haystack = " ".join([entry.integration_type, entry.display_name, *entry.keywords]).lower()
                return q in haystack

            filtered = [e for e in CATALOG if _matches(e)]
            if not filtered:
                st.info(T("settings.no_integrations_match"))
            for group in _UI_GROUPS:
                group_entries = [e for e in filtered if e.ui_group == group]
                if not group_entries:
                    continue
                st.markdown(f"**{_UI_GROUP_LABELS.get(group, group)}**")
                cols = st.columns(4)
                for i, entry in enumerate(group_entries):
                    with cols[i % 4]:
                        _render_catalog_card(entry)

        open_type = st.session_state.get("settings_open_integration", "")
        open_entry = _catalog_by_type.get(open_type) if open_type else None
        if open_type and (open_entry is None or not open_entry.available):
            st.session_state.settings_open_integration = ""
            open_type, open_entry = "", None

        if open_entry is not None:
            # ---- B) Full configuration page (not a dialog — deliberately, so
            # General/Synchronization/Field Mapping/Automation/Logs each have
            # room as real tabs, with space for more (Webhooks/Analytics) later
            # without another navigation rework) --
            if st.button(T("settings.back_to_integrations"), key="settings_back"):
                st.session_state.settings_open_integration = ""
                st.rerun()
            record = integration_store.get_integration(current_user.company_id, open_entry.integration_type)
            connected = record is not None and record.status == integration_store.STATUS_CONNECTED
            rule = sync_rules_store.get_rule(current_user.company_id, open_entry.integration_type) or \
                sync_rules_store.SyncRule(company_id=current_user.company_id, integration_type=open_entry.integration_type)
            mapping = field_mapping_store.get_mapping(current_user.company_id, open_entry.integration_type) or \
                field_mapping_store.FieldMapping(company_id=current_user.company_id, integration_type=open_entry.integration_type)

            head1, head2 = st.columns([1, 6])
            with head1:
                _render_integration_logo(open_entry.integration_type, height_px=32)
            with head2:
                st.markdown(f"### {open_entry.display_name}")
                if record is not None:
                    label, color, dot = _integration_health(record)
                    st.markdown(f"<span style='color:{color};'>{dot} {label}</span>", unsafe_allow_html=True)
                else:
                    st.caption(T("settings.not_connected_yet"))
            st.divider()

            tab_general, tab_sync, tab_mapping, tab_ownership, tab_automation, tab_logs = st.tabs(
                [T("settings.tab_general"), T("settings.tab_synchronization"), T("settings.tab_field_mapping"),
                 T("settings.tab_sync_ownership"), T("settings.tab_automation"), T("settings.tab_logs")]
            )
            with tab_general:
                _INTEGRATION_SETTINGS_RENDERERS[open_entry.integration_type](open_entry, record)
                _render_integration_test_and_disconnect(open_entry, connected)
                if connected and open_entry.integration_category == integration_store.CATEGORY_MARKETPLACE:
                    _render_catalog_import_section(open_entry)
            with tab_sync:
                _render_synchronization_tab(open_entry, rule)
            with tab_mapping:
                _render_field_mapping_tab(open_entry, mapping)
            with tab_ownership:
                _render_sync_ownership_tab(open_entry)
            with tab_automation:
                _render_automation_tab(open_entry, rule)
            with tab_logs:
                _render_logs_tab(open_entry)

        else:
            # ---- A) Dashboard: only connected integrations, as cards --
            head_col1, head_col2 = st.columns([4, 1])
            with head_col1:
                st.caption(T("settings.connect_marketplaces_caption", company=_company_label))
            with head_col2:
                if st.button(T("settings.add_integration"), use_container_width=True, key="open_add_integration"):
                    _add_integration_dialog()

            connected_records = [
                r for r in integration_store.list_integrations(current_user.company_id)
                if r.status in (integration_store.STATUS_CONNECTED, integration_store.STATUS_ERROR)
            ]

            st.divider()

            if not connected_records:
                empty_cols = st.columns([1, 2, 1])
                with empty_cols[1]:
                    st.markdown(
                        "<div style='text-align:center; padding:2rem 0;'>"
                        "<div style='font-size:3rem;'>🧩</div>"
                        f"<h3>{T('settings.no_integrations_connected')}</h3>"
                        f"<p style='color:#6b7280;'>{T('settings.no_integrations_connected_caption')}</p></div>",
                        unsafe_allow_html=True,
                    )
                    if st.button(
                        T("settings.add_integration"), use_container_width=True, type="primary", key="empty_add_integration",
                    ):
                        _add_integration_dialog()
            else:
                cols = st.columns(3)
                for i, record in enumerate(connected_records):
                    entry = _catalog_by_type.get(record.integration_type)
                    if entry is None:
                        continue
                    with cols[i % 3]:
                        with st.container(border=True, key=f"integration_card_{record.integration_type}"):
                            _render_integration_logo(record.integration_type)
                            st.markdown(f"**{entry.display_name}**")
                            label, color, dot = _integration_health(record)
                            st.markdown(f"<span style='color:{color};'>{dot} {label}</span>", unsafe_allow_html=True)
                            account_label = _integration_account_label(record.integration_type, record)
                            if account_label:
                                st.caption(account_label)
                            st.caption(
                                T("settings.last_synced", ts=time.strftime('%Y-%m-%d %H:%M', time.localtime(record.last_sync_at)))
                                if record.last_sync_at else T("settings.health_never_synced")
                            )
                            if st.button(T("settings.edit_word"), key=f"card_edit_{record.integration_type}", use_container_width=True):
                                st.session_state.settings_open_integration = record.integration_type
                                st.rerun()
                            if st.button(
                                T("settings.disconnect_word"), key=f"card_disconnect_{record.integration_type}",
                                use_container_width=True,
                            ):
                                _confirm_disconnect_integration_dialog(record.integration_type, entry.display_name)

    with tab_translation:
        st.caption(T("settings.translation_tab_caption"))

        _lang_options = list(company_store.CONTENT_LANGUAGES.keys())
        _provider_options = ["deepl", "openai"]
        _provider_labels = {"deepl": "DeepL", "openai": "OpenAI"}

        with st.form("company_translation_settings_form"):
            new_default_lang = st.selectbox(
                T("settings.default_product_language"),
                options=_lang_options,
                format_func=lambda code: company_store.CONTENT_LANGUAGES[code],
                index=_lang_options.index(current_company.default_product_language)
                if current_company and current_company.default_product_language in _lang_options else 0,
                help=T("settings.default_product_language_help"),
            )
            new_provider = st.selectbox(
                T("settings.translation_provider"),
                options=_provider_options,
                format_func=lambda p: _provider_labels[p],
                index=_provider_options.index(current_company.translation_provider)
                if current_company and current_company.translation_provider in _provider_options else 0,
            )
            submitted = st.form_submit_button(T("common.save"), type="primary")
            if submitted and current_company:
                current_company.default_product_language = new_default_lang
                current_company.translation_provider = new_provider
                company_store.update_company(current_company)
                st.success(T("settings.translation_settings_saved"))
                st.rerun()

        if current_company and current_company.default_product_language != "en":
            if not IntegrationManager.is_connected(current_company.id, current_company.translation_provider):
                st.warning(T(
                    "settings.translation_provider_not_connected",
                    provider=_provider_labels.get(current_company.translation_provider, current_company.translation_provider),
                ))
            else:
                st.caption(T("settings.translation_provider_connected"))

# =========================================================== COMPANIES ===
elif page == PAGE_COMPANIES:
    st.title(T("nav.companies"))
    # Guards direct page-state navigation too, not just the nav menu itself
    # (auth.is_super_admin() gates whether "🏢 Companies" even appears in
    # _nav_pages above — this re-checks independently, same defense-in-
    # depth pattern as every other role-gated page in this file).
    try:
        auth.require_super_admin(current_user)
    except PermissionError:
        st.error(T("companies.super_admins_only"))
        st.stop()

    st.caption(T("companies.page_caption"))

    all_companies = company_store.list_companies()
    pending = [c for c in all_companies if c.status == company_store.STATUS_PENDING]
    active = [c for c in all_companies if c.status == company_store.STATUS_ACTIVE]
    suspended = [c for c in all_companies if c.status == company_store.STATUS_SUSPENDED]

    def _company_line(c):
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
        st.write(T("companies.company_line", name=c.name, plan=c.plan, users=c.user_limit, products=c.product_limit, created=created))

    if pending:
        st.subheader(T("companies.pending_approval"))
        for c in pending:
            with st.container(border=True):
                _company_line(c)
                admins = [u for u in auth_store.list_users_for_company(c.id) if u.role == auth.ROLE_ADMIN]
                admin_label = ", ".join(f"{u.name} <{u.email}>" for u in admins) or T("companies.no_admin_found")
                st.caption(T("companies.admin_label", admin=admin_label))
                col1, col2 = st.columns(2)
                with col1:
                    if st.button(T("companies.approve"), key=f"approve_{c.id}", use_container_width=True):
                        c.status = company_store.STATUS_ACTIVE
                        c.updated_at = time.time()
                        company_store.update_company(c)
                        audit_store.log_audit(c.id, current_user.id, "COMPANY_APPROVED", "company", c.id)
                        st.rerun()
                with col2:
                    if st.button(T("companies.reject"), key=f"reject_{c.id}", use_container_width=True):
                        c.status = company_store.STATUS_SUSPENDED
                        c.updated_at = time.time()
                        company_store.update_company(c)
                        audit_store.log_audit(c.id, current_user.id, "COMPANY_DISABLED", "company", c.id, "rejected at signup")
                        st.rerun()
        st.divider()

    st.subheader(T("companies.active_companies"))
    if not active:
        st.caption(T("companies.none"))
    for c in active:
        with st.container(border=True):
            _company_line(c)
            if st.button(T("companies.suspend"), key=f"suspend_{c.id}"):
                c.status = company_store.STATUS_SUSPENDED
                c.updated_at = time.time()
                company_store.update_company(c)
                audit_store.log_audit(c.id, current_user.id, "COMPANY_DISABLED", "company", c.id)
                st.rerun()

    st.subheader(T("companies.suspended_companies"))
    if not suspended:
        st.caption(T("companies.none"))
    for c in suspended:
        with st.container(border=True):
            _company_line(c)
            if st.button(T("companies.reactivate"), key=f"reactivate_{c.id}"):
                c.status = company_store.STATUS_ACTIVE
                c.updated_at = time.time()
                company_store.update_company(c)
                audit_store.log_audit(c.id, current_user.id, "COMPANY_APPROVED", "company", c.id, "reactivated")
                st.rerun()

    st.divider()
    st.subheader(T("companies.platform_admins"))
    platform_admins = platform_admin_store.list_all()
    active_admin_count = platform_admin_store.count_active()
    for pa in platform_admins:
        pa_user = auth_store.get_user_by_id(pa.user_id)
        label = f"{pa_user.name} <{pa_user.email}>" if pa_user else T("companies.missing_user", user_id=pa.user_id)
        col1, col2 = st.columns([3, 1])
        with col1:
            st.write(f"{label} — {T('companies.active_word') if pa.is_active else T('companies.inactive_word')}")
        with col2:
            if pa.is_active:
                # Hard-blocked in this UI if it would leave zero active
                # Super Admins — the platform invariant (count >= 1) is
                # enforced here unconditionally, same as the CLI's
                # `disable` command (scripts/superadmin_cli.py).
                disable_blocked = active_admin_count <= 1
                if st.button(T("companies.disable"), key=f"disable_admin_{pa.id}", disabled=disable_blocked, use_container_width=True):
                    platform_admin_store.set_active(pa.id, False)
                    audit_store.log_audit(
                        pa_user.company_id if pa_user else "unknown", current_user.id,
                        "SUPERADMIN_DISABLED", "user", pa.user_id,
                    )
                    st.rerun()
                if disable_blocked:
                    st.caption(T("companies.cant_disable_only_admin"))
            else:
                if st.button(T("companies.enable"), key=f"enable_admin_{pa.id}", use_container_width=True):
                    platform_admin_store.set_active(pa.id, True)
                    audit_store.log_audit(
                        pa_user.company_id if pa_user else "unknown", current_user.id,
                        "SUPERADMIN_ENABLED", "user", pa.user_id,
                    )
                    st.rerun()

    st.markdown(f"**{T('companies.grant_super_admin')}**")
    st.caption(T("companies.grant_super_admin_caption"))
    with st.form("grant_super_admin_form", clear_on_submit=True):
        grant_email = st.text_input(T("companies.existing_user_email"))
        if st.form_submit_button(T("companies.grant_super_admin")):
            matches = auth_store.get_users_by_email(grant_email.strip().lower())
            if not matches:
                st.error(T("companies.no_user_found"))
            else:
                target_user = matches[0]
                if len(matches) > 1:
                    st.warning(T("companies.multiple_accounts_warning", company_id=target_user.company_id))
                existing = platform_admin_store.get_by_user_id(target_user.id)
                if existing and existing.is_active:
                    st.info(T("companies.already_super_admin", email=target_user.email))
                elif existing:
                    platform_admin_store.set_active(existing.id, True)
                    audit_store.log_audit(target_user.company_id, current_user.id, "SUPERADMIN_ENABLED", "user", target_user.id)
                    st.success(T("companies.reactivated_super_admin", email=target_user.email))
                    st.rerun()
                else:
                    platform_admin_store.create(target_user.id)
                    audit_store.log_audit(target_user.company_id, current_user.id, "PLATFORM_ROLE_CHANGED", "user", target_user.id, "granted Super Admin")
                    st.success(T("companies.now_super_admin", email=target_user.email))
                    st.rerun()
