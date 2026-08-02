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
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import pandas as pd
from PIL import Image, ImageOps

from modules import (
    audit_store,
    auth,
    auth_cookie,
    auth_store,
    barcode_scanner,
    company_store,
    description_gen,
    export,
    field_mapping_store,
    identifier_lookup,
    image_pipeline,
    integration_store,
    inventory_store,
    manifest_import,
    manifest_store,
    marketplace_store,
    pricing,
    pwa,
    repair_store,
    spec_lookup,
    sync_job_store,
    sync_rules_store,
    vision_grading,
)
from integrations import field_registry, scheduler as sync_scheduler
from integrations.base import ConnectorActionResult
from integrations.manager import CATALOG, IntegrationManager, IntegrationNotAvailableError, IntegrationNotConnectedError
from modules.models import Product
from modules.review_table_component import review_table
from modules.inventory_table_component import inventory_table
from modules.export_table_component import export_table
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
    st.markdown(
        """
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
            content: "Take Photo" !important;
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
        """,
        unsafe_allow_html=True,
    )


@st.cache_resource
def _photo_executor() -> concurrent.futures.ThreadPoolExecutor:
    """One shared background worker pool for photo normalization, so
    capturing the next photo doesn't have to wait for the previous one to
    finish uploading/processing — st.cache_resource keeps a single
    instance alive across reruns instead of spawning a new pool every
    script run. Modest worker count: this is CPU-bound Pillow work
    (EXIF transpose + resize), not I/O-bound, so more workers than cores
    just causes contention rather than helping."""
    return concurrent.futures.ThreadPoolExecutor(max_workers=2)


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
                st.error(f"Photo processing failed: {e}")

    photos = st.session_state.captured_photos
    pending_count = len(st.session_state.pending_photos)
    if not photos and not pending_count:
        return

    status = f"**{len(photos)} photo(s) captured**"
    if pending_count:
        status += f" · ⏳ {pending_count} still processing..."
    st.write(status)

    cols = st.columns(4)
    for i, img_bytes in enumerate(photos):
        with cols[i % 4]:
            st.image(img_bytes, use_container_width=True)
            if i == 0:
                # Only the first (main listing) photo — this is the one
                # shown in search results/thumbnails, so it's the one
                # worth a clean white e-commerce background.
                if st.button("🧼 Clean background", key="clean_bg_0", use_container_width=True):
                    with st.spinner("Enhancing photo — first run downloads the model (~170MB) and may take a minute..."):
                        try:
                            enhanced, score, report = image_pipeline.process_image(img_bytes)
                        except image_pipeline.LowQualityImageError as e:
                            st.error(
                                "Photo didn't pass the quality check: "
                                + " ".join(e.report.issues)
                                + " Please retake it (better focus/lighting, or closer up)."
                            )
                        except Exception as e:
                            st.error(f"Background removal failed: {e}")
                        else:
                            st.session_state.captured_photos[0] = enhanced.jpeg_bytes
                            if enhanced.low_resolution:
                                st.session_state.photo0_low_res_warning = True
                            for warning in report.warnings:
                                st.toast(warning, icon="⚠️")
                            st.rerun(scope="fragment")
                if st.session_state.get("photo0_low_res_warning"):
                    st.caption(
                        "⚠️ Source photo resolution was too low to fill the "
                        "frame without blurring — the product was kept at its "
                        "native sharpness instead. For a bigger, crisper result, "
                        "retake this photo closer up and in better focus."
                    )
            if st.button("🗑️", key=f"del_photo_{i}"):
                if i == 0:
                    st.session_state.photo0_low_res_warning = False
                st.session_state.captured_photos.pop(i)
                st.rerun(scope="fragment")

    for j in range(pending_count):
        with cols[(len(photos) + j) % 4]:
            st.info("⏳ Processing...")

    st.divider()
    if product.sku:
        # Already assigned (manifest import auto-assigns SKU on import) —
        # nothing to enter here, never changed or generated by AI.
        st.write(f"**SKU:** {product.sku}")
        st.caption("Assigned automatically from the manifest import.")
        sku_input = product.sku
        sku_missing = False
    else:
        sku_spacer, sku_col = st.columns([1, 1])
        with sku_col:
            sku_input = st.text_input(
                "SKU *",
                value=product.sku,
                placeholder="Enter SKU before continuing",
                help="Entered manually — never changed or generated by AI. "
                "This SKU will be linked to all AI analysis results and the Excel record for this item.",
            )
            sku_missing = not sku_input.strip()
            if sku_missing:
                st.warning("⚠️ SKU is required before you can continue to analysis.")

    if pending_count:
        st.caption("⏳ Waiting for photo processing to finish before continuing...")

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("⬅ Back", use_container_width=True):
            st.session_state.wizard_step = 1
            st.rerun()
    with col3:
        if st.button("Next ➜", type="primary", use_container_width=True, disabled=not photos or sku_missing or bool(pending_count)):
            product.sku = sku_input.strip()
            st.session_state.wizard_step = 3
            st.rerun()


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
    st.markdown(
        """
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
            content: "📷  Take a Photo or Choose from Library" !important;
        }
        section[data-testid="stFileUploaderDropzone"] button[data-testid="stBaseButton-borderlessIcon"]::after {
            content: "📷  Take Another Photo" !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


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
    # single-column forms) — Review & Export, Inventory, and CSV/Excel
    # Export are the three desktop-oriented, data-grid-heavy pages, so they
    # get the wide layout. st.session_state already holds last run's
    # sidebar radio value (via its key="page" below) before the radio
    # widget itself re-renders, which is what makes a per-page layout
    # possible at all — set_page_config must be the first Streamlit call,
    # before the radio exists to read from directly.
    layout="wide" if st.session_state.get("page") in (
        "🔍 Review & Export", "📦 Inventory", "📤 CSV/Excel Export",
    ) else "centered",
    initial_sidebar_state="collapsed",
)
pwa.inject_pwa_head()
ensure_scheduler_running()  # unconditional: starts regardless of which page/user loads first

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
    st.subheader("Log in")
    with st.form("login_form"):
        email_in = st.text_input("Email")
        password_in = st.text_input("Password", type="password")
        if st.form_submit_button("Log in", type="primary", use_container_width=True):
            user = auth.verify_login(email_in, password_in)
            if user is None:
                st.error("Invalid email or password.")
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
        "grading_result": None,
        "price_estimate": None,
        "descriptions": None,
        "manifest_df": None,
        "manifest_uploaded_name": None,
        "review_selected_ids": set(),   # product ids checked for bulk export
        "review_open_product_id": None,  # product id whose card is open, or None for list view
        "review_filtered_ids_cache": [],  # ids currently eligible for the list, for Previous/Next
        "review_export_requested": False,  # set inside the fragment, consumed outside it to open the dialog
        "review_clear_seq": 0,          # bumped to tell the grid to deselect all (no remount needed)
        "review_focus_id": "",          # product id the grid should scroll to/highlight on next render
        "review_last_esc_value": None,  # last value seen from esc_listener(), to detect a new Escape press
        "export_selected_ids": set(),   # product ids checked for CSV/Excel export
        "export_open_product_id": None,  # product id whose read-only detail view is open, or None for list view
        "export_filtered_ids_cache": [],  # ids currently eligible for the list, for Previous/Next
        "export_clear_seq": 0,          # bumped to tell the grid to deselect all (no remount needed)
        "export_last_esc_value": None,  # last value seen from esc_listener(), to detect a new Escape press
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
    st.session_state.grading_result = None
    st.session_state.price_estimate = None
    st.session_state.descriptions = None


def _ai_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


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
        st.error(f"Missing required mapping for: {', '.join(missing_labels)}")
    return confirmed_map, missing_required


@st.dialog("Delete manifest batch?")
def _confirm_delete_batch_dialog(batch, linked_products):
    pending_count = sum(1 for p in linked_products if p.status == "draft")
    processed_count = len(linked_products) - pending_count
    st.warning(
        f"This will permanently delete the manifest batch record for "
        f"**{batch.filename}** (`{batch.id}`) and its "
        f"**{pending_count} still-pending (unprocessed) product(s)**.\n\n"
        + (
            f"**{processed_count} already-processed product(s) will be kept** "
            "in your inventory — they'll just lose their manifest reference. "
            if processed_count
            else ""
        )
        + "\n\nThis cannot be undone."
    )
    confirm = st.checkbox("I understand, delete this batch")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Cancel", use_container_width=True):
            st.rerun()
    with col2:
        if st.button("🗑️ Delete", type="primary", disabled=not confirm, use_container_width=True):
            deleted_count = inventory_store.delete_products_by_manifest(
                batch.id, company_id=st.session_state.company_id
            )
            manifest_store.delete_batch(batch.id, st.session_state.company_id)
            audit_store.log_audit(st.session_state.company_id, current_user.id, "DELETE_MANIFEST", "manifest_batch", batch.id)
            st.success(f"Deleted batch and {deleted_count} pending product(s).")
            st.rerun()


# ------------------------------------------------------------------- nav --

st.sidebar.title("📱 ElectroGrader")
st.sidebar.caption(f"🏢 {current_company.name if current_company else current_user.company_id}")
st.sidebar.caption(f"👤 {current_user.name} · {current_user.role}")
if st.sidebar.button("Log out", use_container_width=True):
    auth.invalidate_session(st.session_state.get("auth_token", ""))
    auth_cookie.clear_session_cookie()
    for k in ("auth_user", "auth_token"):
        st.session_state.pop(k, None)
    time.sleep(0.35)  # let the cookie-clearing iframe script actually run first
    st.rerun()
st.sidebar.divider()

_nav_pages = ["🆕 New Item", "📦 Inventory", "🔍 Review & Export", "📤 CSV/Excel Export"]
if current_user.role == auth.ROLE_ADMIN:
    _nav_pages.insert(1, "📥 Import Manifest")
    _nav_pages.append("👥 Manage Users")
    _nav_pages.append("⚙️ Settings")
page = st.sidebar.radio(
    "Navigate",
    _nav_pages,
    label_visibility="collapsed",
    key="page",
)

if not _ai_configured():
    st.sidebar.warning(
        "ANTHROPIC_API_KEY is not set — spec structuring, vision grading and "
        "description generation will be unavailable until you add it (see README)."
    )

# =========================================================== NEW ITEM ====
if page == "🆕 New Item":
    st.title("🆕 New Item")
    steps = ["1. Identify", "2. Photos", "3. Specs", "4. Grading", "5. Price & Copy", "6. Save"]
    st.progress((st.session_state.wizard_step - 1) / (len(steps) - 1), text=steps[st.session_state.wizard_step - 1])

    product: Product = st.session_state.product

    # ---- Step 1: Identify — from a manifest draft, or from scratch ----
    if st.session_state.wizard_step == 1:
        st.subheader("Start this item")
        source = st.radio(
            "How do you want to start?",
            ["📥 From a pending manifest item", "🆕 From scratch (scan/manual)"],
            horizontal=False,
        )

        if source.startswith("📥"):
            drafts = [
                p for p in inventory_store.list_products(st.session_state.company_id)
                if p.status == "draft"
            ]
            if not drafts:
                st.info(
                    "No pending manifest items for this company. Use '📥 Import Manifest' "
                    "to add some, or start from scratch instead."
                )
            else:
                search_query = st.text_input(
                    "🔍 Search pending items (SKU, Target #, ASIN, or description)",
                    placeholder="e.g. 2005, T-9001, B08N5WRWNW, hand blender",
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
                    st.caption(f"{len(drafts)} match(es)")

                if not drafts:
                    st.info("No pending items match that search.")
                else:
                    options = {
                        f"SKU {d.sku or '?'} — {d.manifest_target_no or '(no target #)'} — "
                        f"ASIN {d.asin or '?'} — "
                        f"{(d.manifest_item_description or '(no description)')[:60]}": d.id
                        for d in drafts
                    }
                    chosen_label = st.selectbox("Pick a pending manifest item", list(options.keys()))
                    chosen = inventory_store.get_product(options[chosen_label], st.session_state.company_id)

                    st.write(f"**SKU:** {chosen.sku or '—'}")
                    st.write(f"**Item description:** {chosen.manifest_item_description}")
                    cols = st.columns(2)
                    with cols[0]:
                        st.write(f"**ASIN:** {chosen.asin or '—'}")
                        st.write(f"**EAN/Barcode:** {chosen.manifest_barcode or '—'}")
                    with cols[1]:
                        st.write(f"**Qty:** {chosen.manifest_qty}")
                        st.write(f"**Weight:** {chosen.manifest_weight_kg} kg")
                    st.caption(
                        "Reminder: this manifest data is an unverified claim — later steps "
                        "will independently check it against the actual photos."
                    )

                    if st.button("Use this item ➜", type="primary", use_container_width=True):
                        chosen.status = "in_progress"
                        st.session_state.product = chosen
                        st.session_state.wizard_step = 2
                        st.rerun()
        else:
            st.caption(
                "Point the camera at the barcode or model-number sticker. "
                "On phones, tap the switch-camera icon in the widget to use "
                "the rear camera if it opens on the front camera."
            )
            _enlarge_camera_preview()
            shot = st.camera_input("Scan label", key=f"barcode_cam_{st.session_state.camera_session_id}")

            decoded = []
            if shot is not None:
                decoded = barcode_scanner.decode_barcodes(shot.getvalue())
                if decoded:
                    st.success(f"Decoded: {', '.join(decoded)}")
                elif not barcode_scanner.zbar_available():
                    st.info("Barcode auto-decode isn't available on this install (zbar missing) — enter the model number manually below.")
                else:
                    st.info("No barcode detected in frame — try again or enter manually below.")

            default_val = decoded[0] if decoded else product.model_number
            manual = st.text_input("Model number / barcode (edit if needed)", value=default_val)

            col1, col2 = st.columns(2)
            with col2:
                if st.button("Next ➜", type="primary", use_container_width=True, disabled=not manual.strip()):
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
        st.subheader("Capture photos")
        st.caption(
            "Take front, back, sides, and close-ups of any scratches/defects. "
            "If a barcode/label is visible in any photo, it will also be used "
            "to cross-check against the manifest during grading."
        )

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
            "Take a photo or choose from library",
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

    # ---- Step 3: Web spec lookup ----
    elif st.session_state.wizard_step == 3:
        st.subheader("Specifications & box contents")
        st.caption("Automated web lookup — review and edit before continuing.")

        if st.session_state.spec_result is None:
            if st.button("🔎 Fetch specs from the web", type="primary", disabled=not _ai_configured()):
                with st.spinner("Searching and reading sources..."):
                    sr = spec_lookup.lookup(
                        ean=product.ean or product.manifest_barcode or product.scanned_barcode,
                        asin=product.asin,
                        model_number=product.model_number,
                        item_description=product.manifest_item_description,
                    )
                    st.session_state.spec_result = sr
                # Automatic EAN/ASIN discovery — no separate button, never
                # overwrites anything already known (e.g. from the manifest).
                with st.spinner("Checking EAN/ASIN..."):
                    identifier_lookup.ensure_identifiers(
                        product,
                        brand=sr.brand,
                        model=sr.model,
                        product_name=sr.product_name,
                        other_info=sr.category,
                    )
                st.rerun()
            st.caption("Or skip and fill the fields in manually below.")

        sr = st.session_state.spec_result
        name_val = sr.product_name if sr else product.name
        brand_val = sr.brand if sr else product.brand
        model_val = sr.model if sr else product.model
        category_val = sr.category if sr else product.category
        spec_val = sr.spec_summary if sr else product.spec_summary
        box_val = "\n".join(sr.box_contents) if sr and sr.box_contents else "\n".join(product.box_contents)

        name_in = st.text_input("Product name", value=name_val)
        cols = st.columns(2)
        with cols[0]:
            brand_in = st.text_input("Brand", value=brand_val)
        with cols[1]:
            model_in = st.text_input("Model", value=model_val)
        category_in = st.text_input("Category", value=category_val or "", placeholder="e.g. Smartphone, Laptop, Headphones")
        spec_in = st.text_area("Spec summary", value=spec_val, height=100)
        box_in = st.text_area("Standard box contents (one per line)", value=box_val, height=100)

        st.divider()
        st.markdown("**EAN / ASIN identification**")
        st.caption(
            "Filled automatically (never overwrites an existing value, never invents "
            "one) as soon as specs are fetched above. Correct manually if needed."
        )
        id_cols = st.columns(2)
        with id_cols[0]:
            ean_in = st.text_input("EAN / GTIN", value=product.ean)
            status_caption = product.ean_status or "Not yet checked"
            if product.ean_source:
                status_caption += f" — {product.ean_source}"
            st.caption(status_caption)
        with id_cols[1]:
            asin_in = st.text_input("ASIN", value=product.asin)
            status_caption = product.asin_status or "Not yet checked"
            if product.asin_source:
                status_caption += f" — {product.asin_source}"
            st.caption(status_caption)
            if product.asin_candidates:
                st.warning(f"Other possible ASINs found — please verify: {', '.join(product.asin_candidates)}")

        if sr and sr.sources:
            with st.expander("Sources used"):
                for s in sr.sources:
                    st.write(s)

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ Back", use_container_width=True):
                st.session_state.wizard_step = 2
                st.rerun()
        with col3:
            if st.button("Next ➜", type="primary", use_container_width=True):
                product.name = name_in.strip()
                product.brand = brand_in.strip()
                product.model = model_in.strip()
                product.category = category_in.strip()
                product.spec_summary = spec_in.strip()
                product.box_contents = [l.strip() for l in box_in.splitlines() if l.strip()]

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
        st.subheader("AI condition grading")

        if st.session_state.grading_result is None:
            if st.button("🧠 Analyze photos", type="primary", disabled=not _ai_configured()):
                with st.spinner("Inspecting photos for defects, missing parts, and identity match..."):
                    decoded_barcodes = []
                    for img_bytes in st.session_state.captured_photos:
                        decoded_barcodes.extend(barcode_scanner.decode_barcodes(img_bytes))

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
                st.rerun()
            st.caption("Or skip and grade manually below.")

        gr = st.session_state.grading_result
        grade_options = ["A", "B", "C", "D"]
        default_grade = gr.grade if gr and gr.grade in grade_options else "B"
        grade_in = st.selectbox("Grade", grade_options, index=grade_options.index(default_grade))
        st.caption(vision_grading.GRADE_SCALE[grade_in])

        default_confidence = gr.grade_confidence if gr else product.grade_confidence
        confidence_in = st.slider(
            "AI confidence (%)",
            min_value=0,
            max_value=100,
            value=int(default_confidence),
            help="AI's confidence in the assigned grade, based on photo quality, "
            "clarity of visible defects, certainty about box completeness, and "
            "how cleanly the item matches the A/B/C/D criteria. Lower photo "
            "quality or borderline cases should produce a lower number.",
        )
        st.caption(f"Grade: {grade_in}  •  AI confidence: {confidence_in}%")

        condition_options = ["Used", "New"]
        default_condition = gr.condition_type if gr and gr.condition_type in condition_options else (
            product.condition_type if product.condition_type in condition_options else "Used"
        )
        condition_in = st.selectbox("New / Used", condition_options, index=condition_options.index(default_condition))

        if gr and gr.grade_reasoning:
            st.info(gr.grade_reasoning)

        defects_val = "\n".join(gr.defects) if gr else "\n".join(product.defects)
        missing_val = "\n".join(gr.missing_components) if gr else "\n".join(product.missing_components)
        checklist_val = "\n".join(gr.functional_checklist) if gr else "\n".join(product.functional_checklist)

        defects_in = st.text_area("Defects (one per line)", value=defects_val, height=100)
        missing_in = st.text_area("Missing components (one per line)", value=missing_val, height=70)
        checklist_in = st.text_area("Functional test checklist (one per line)", value=checklist_val, height=120)

        st.divider()
        st.markdown("**Manifest vs. photo verification**")
        st.caption(
            "AI never assumes the manifest is correct — this compares what's "
            "actually visible in the photos (brand, model, product type, any "
            "visible barcode) against the claimed identity above."
        )

        match_options = ["YES", "NO", "UNKNOWN"]
        default_match = gr.product_match if gr and gr.product_match in match_options else (
            product.product_match if product.product_match in match_options else "UNKNOWN"
        )
        match_in = st.selectbox("Product match", match_options, index=match_options.index(default_match))

        default_match_conf = gr.match_confidence if gr else product.match_confidence
        match_conf_in = st.slider("Match confidence (%)", min_value=0, max_value=100, value=int(default_match_conf))

        match_notes_val = gr.match_notes if gr else product.match_notes
        match_notes_in = st.text_area("Match notes", value=match_notes_val, height=70)

        if match_in == "NO" or match_conf_in < MATCH_CONFIDENCE_WARNING_THRESHOLD:
            st.warning(
                "⚠️ Possible mismatch between the manifest data and the photographed "
                "item. Double-check the photos and manifest details before proceeding."
            )

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ Back", use_container_width=True):
                st.session_state.wizard_step = 3
                st.rerun()
        with col3:
            if st.button("Next ➜", type="primary", use_container_width=True):
                product.grade = grade_in
                product.grade_confidence = int(confidence_in)
                product.grade_reasoning = gr.grade_reasoning if gr else ""
                product.condition_type = condition_in
                product.defects = [l.strip() for l in defects_in.splitlines() if l.strip()]
                product.missing_components = [l.strip() for l in missing_in.splitlines() if l.strip()]
                product.functional_checklist = [l.strip() for l in checklist_in.splitlines() if l.strip()]
                product.product_match = match_in
                product.match_confidence = int(match_conf_in)
                product.match_notes = match_notes_in.strip()
                st.session_state.wizard_step = 5
                st.rerun()

    # ---- Step 5: Pricing + description generation ----
    elif st.session_state.wizard_step == 5:
        st.subheader("Price & listing copy")

        if product.product_match == "NO" or product.match_confidence < MATCH_CONFIDENCE_WARNING_THRESHOLD:
            st.warning("⚠️ This item was flagged as a possible manifest/photo mismatch in the previous step.")

        if st.session_state.price_estimate is None:
            if st.button("💲 Estimate market price"):
                with st.spinner("Searching for comparable prices..."):
                    st.session_state.price_estimate = pricing.estimate_price(
                        f"{product.brand} {product.model} {product.name}".strip(), product.grade
                    )
                st.rerun()

        pe = st.session_state.price_estimate
        if pe:
            st.caption(pe.reasoning)
        default_price = (pe.suggested_price if pe and pe.suggested_price else product.price) or 0.0
        price_in = st.number_input(
            "Estimated average selling price ($)",
            min_value=0.0,
            value=float(default_price),
            step=1.0,
            help="AI-suggested market price. You can correct it here, or override it again in the final step.",
        )

        st.divider()

        if st.session_state.descriptions is None:
            if st.button("✍️ Generate English descriptions", type="primary", disabled=not _ai_configured()):
                with st.spinner("Writing listing copy..."):
                    st.session_state.descriptions = description_gen.generate_descriptions(
                        name=product.name,
                        brand=product.brand,
                        model=product.model,
                        category=product.category,
                        spec_summary=product.spec_summary,
                        box_contents=product.box_contents,
                        grade=product.grade,
                        condition_type=product.condition_type,
                        defects=product.defects,
                        missing_components=product.missing_components,
                    )
                st.rerun()
            st.caption("Or write everything manually below.")

        desc = st.session_state.descriptions
        product_name_val = desc.product_name if desc else product.name
        product_desc_val = desc.product_description if desc else product.product_description
        condition_desc_val = desc.condition_description if desc else product.condition_description

        product_name_in = st.text_input("Product Name (listing title)", value=product_name_val)
        product_desc_in = st.text_area("Product Description (general overview)", value=product_desc_val, height=150)
        condition_desc_in = st.text_area(
            "Additional Description (Condition & Scratches Details)",
            value=condition_desc_val,
            height=150,
            max_chars=description_gen.MAX_CONDITION_DESCRIPTION_LEN,
        )

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ Back", use_container_width=True):
                st.session_state.wizard_step = 4
                st.rerun()
        with col3:
            if st.button("Next ➜", type="primary", use_container_width=True):
                product.price = float(price_in)
                product.price_reasoning = pe.reasoning if pe else ""
                if product_name_in.strip():
                    product.name = product_name_in.strip()
                product.product_description = product_desc_in.strip()
                product.condition_description = condition_desc_in.strip()
                st.session_state.wizard_step = 6
                st.rerun()

    # ---- Step 6: Manual-only details + review + save ----
    elif st.session_state.wizard_step == 6:
        st.subheader("Manual-only details")
        st.caption("AI cannot reliably determine these — fill them in by hand.")

        location_in = st.text_input("Location / shelf position", value=product.location)
        test_options = ["Not Tested", "Working", "Not Working"]
        default_test = product.functional_test_result if product.functional_test_result in test_options else "Not Tested"
        test_in = st.selectbox("Functional test result", test_options, index=test_options.index(default_test))
        quantity_in = st.number_input(
            "Quantity", min_value=1, value=int(product.quantity or 1), step=1,
            help="Number of units this listing represents. Defaults to 1.",
        )

        st.caption("Box dimensions (for courier/shipping calculations)")
        dim_cols = st.columns(3)
        with dim_cols[0]:
            length_in = st.number_input("Box length (cm)", min_value=0.0, value=float(product.box_length_cm), step=0.5)
        with dim_cols[1]:
            width_in = st.number_input("Box width (cm)", min_value=0.0, value=float(product.box_width_cm), step=0.5)
        with dim_cols[2]:
            height_in = st.number_input("Box height (cm)", min_value=0.0, value=float(product.box_height_cm), step=0.5)

        price_override_in = st.number_input(
            "Final selling price ($) — correct here if needed",
            min_value=0.0,
            value=float(product.price),
            step=1.0,
        )

        st.divider()
        st.subheader("Finalize & save to inventory")
        st.write(f"**SKU:** {product.sku}")
        st.caption("SKU was set manually in step 2 and stays fixed — it is never changed or generated by AI.")

        if product.product_match == "NO" or product.match_confidence < MATCH_CONFIDENCE_WARNING_THRESHOLD:
            st.warning(
                f"⚠️ Product match: {product.product_match or 'UNKNOWN'} "
                f"({product.match_confidence}% confidence). {product.match_notes}"
            )

        st.write("**Summary**")
        st.json(
            {
                "SKU": product.sku,
                "Name": product.name,
                "Brand": product.brand,
                "Model": product.model,
                "Condition": product.condition_type,
                "Grade": product.grade,
                "AI Confidence %": product.grade_confidence,
                "Product Match": product.product_match,
                "Match Confidence %": product.match_confidence,
                "Price": product.price,
                "Quantity": int(quantity_in),
                "Photos": len(st.session_state.captured_photos),
            }
        )

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ Back", use_container_width=True):
                st.session_state.wizard_step = 5
                st.rerun()
        with col3:
            if st.button("✅ Save item", type="primary", use_container_width=True):
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

                inventory_store.save_product(product)
                audit_store.log_audit(product.company_id, current_user.id, "CREATE_PRODUCT", "product", product.id)
                st.success(f"Saved '{product.name or product.model_number}' to inventory.")
                st.balloons()
                reset_wizard()

    st.divider()
    if st.button("🔄 Start over / discard this item"):
        reset_wizard()
        st.rerun()

# ======================================================= IMPORT MANIFEST =
elif page == "📥 Import Manifest":
    st.title("📥 Import Manifest")
    try:
        auth.require_role(current_user, auth.ROLE_ADMIN)
    except PermissionError:
        st.error("Admins only.")
        st.stop()
    st.caption(
        "Upload an Amazon liquidation manifest (.xlsx or .csv). Only these "
        "fields are imported: Target #, Subcategory, ASIN, EAN/Barcode, Item "
        "description, Qty, Weight (kg) — everything else (brand, model, "
        "grade, price, descriptions...) is determined later by AI + photos, "
        "never assumed from the manifest. This is purely additive — it does "
        "not replace the existing manual scan/search flow in '🆕 New Item'."
    )

    uploaded = st.file_uploader("Manifest file", type=["xlsx", "csv"])
    if uploaded is not None and st.session_state.manifest_uploaded_name != uploaded.name:
        df = manifest_import.read_table(uploaded.getvalue(), uploaded.name)
        st.session_state.manifest_df = df
        st.session_state.manifest_uploaded_name = uploaded.name

    df = st.session_state.manifest_df
    if df is not None:
        st.write(f"**{len(df)} row(s) found.** Columns in file: {list(df.columns)}")
        st.markdown("**Confirm column mapping** — auto-detected where possible; adjust any that are wrong.")
        confirmed_map, missing_required = _render_column_mapping_ui(df, key_prefix="new")

        rows = manifest_import.extract_rows(df, confirmed_map)
        st.write(f"**Preview ({len(rows)} row(s) with the confirmed mapping):**")
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

        if st.button("✅ Import as new manifest batch", type="primary", disabled=bool(missing_required) or not rows):
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
                    progress = st.progress(0.0, text=f"Looking up missing EAN/ASIN for {len(to_check)} item(s)...")
                    for i, d in enumerate(to_check):
                        identifier_lookup.ensure_identifiers(
                            d,
                            product_name=d.manifest_item_description,
                            other_info=d.manifest_subcategory,
                        )
                        progress.progress((i + 1) / len(to_check), text=f"Checked {i + 1}/{len(to_check)} item(s)...")
                    progress.empty()

                inventory_store.save_products_bulk(drafts)
                batch.status = manifest_store.STATUS_IMPORTED
                manifest_store.save_batch(batch)
                audit_store.log_audit(
                    st.session_state.company_id, current_user.id, "IMPORT_MANIFEST", "manifest_batch", batch.id,
                    f"{len(drafts)} item(s), file={batch.filename}",
                )

                st.success(
                    f"Created manifest batch **{batch.id}** with {len(drafts)} item(s), "
                    f"assigned SKU **{skus[0]}** to **{skus[-1]}**."
                )
                st.caption("Go to '🆕 New Item' → '📥 From a pending manifest item' to process them one by one.")
            except Exception as e:
                batch.status = manifest_store.STATUS_ERROR
                batch.error_message = str(e)
                manifest_store.save_batch(batch)
                st.error(f"Import failed: {e}")

            st.session_state.manifest_df = None
            st.session_state.manifest_uploaded_name = None

    st.divider()
    st.subheader("Manifest batches")
    batches = manifest_store.list_batches(st.session_state.company_id)
    if not batches:
        st.caption("No manifest batches imported yet for this company.")
    else:
        status_badges = {
            manifest_store.STATUS_PROCESSING: "🔄 Processing",
            manifest_store.STATUS_IMPORTED: "✅ Imported",
            manifest_store.STATUS_ERROR: "❌ Error",
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
                    f"{badge}  •  Uploaded {uploaded_str}  •  {b.row_count} product(s) in file  •  "
                    f"{len(linked)} linked ({pending} pending, {processed} processed)"
                )
                if b.status == manifest_store.STATUS_ERROR and b.error_message:
                    st.error(b.error_message)

                bcol1, bcol2, bcol3 = st.columns(3)
                with bcol1:
                    if st.button("👁️ View", key=f"view_{b.id}", use_container_width=True):
                        st.session_state[f"show_view_{b.id}"] = not st.session_state.get(f"show_view_{b.id}", False)
                with bcol2:
                    if st.button("🔄 Replace", key=f"replace_{b.id}", use_container_width=True):
                        st.session_state[f"show_replace_{b.id}"] = not st.session_state.get(f"show_replace_{b.id}", False)
                with bcol3:
                    if st.button("🗑️ Delete", key=f"delete_{b.id}", use_container_width=True):
                        _confirm_delete_batch_dialog(b, linked)

                if st.session_state.get(f"show_view_{b.id}"):
                    st.write("**Column mapping used:**")
                    st.json(b.column_map)
                    st.write("**Linked products:**")
                    if linked:
                        view_df = pd.DataFrame(
                            [
                                {
                                    "SKU": p.sku,
                                    "Status": p.status,
                                    "Name / Description": p.name or p.manifest_item_description,
                                    "ASIN": p.asin,
                                    "EAN": p.manifest_barcode,
                                }
                                for p in linked
                            ]
                        )
                        st.dataframe(view_df, use_container_width=True)
                    else:
                        st.caption("No products linked to this batch.")

                if st.session_state.get(f"show_replace_{b.id}"):
                    st.write("**Upload a new version of this manifest**")
                    st.caption(
                        "Rows matching an existing linked product (by Target #, ASIN, or EAN) "
                        "are updated in place — SKU, photos, grading, and any other work already "
                        "done are never touched. Rows that don't match anything become new pending "
                        "items. No duplicates are created."
                    )
                    replace_upload = st.file_uploader(
                        "New manifest file", type=["xlsx", "csv"], key=f"replace_upload_{b.id}"
                    )
                    if replace_upload is not None:
                        replace_df = manifest_import.read_table(replace_upload.getvalue(), replace_upload.name)
                        st.write(f"**{len(replace_df)} row(s) found.**")
                        r_confirmed_map, r_missing = _render_column_mapping_ui(replace_df, key_prefix=f"replace_{b.id}")
                        r_rows = manifest_import.extract_rows(replace_df, r_confirmed_map)
                        st.write(f"**Preview ({len(r_rows)} row(s)):**")
                        if r_rows:
                            st.dataframe(pd.DataFrame(r_rows), use_container_width=True)

                        if st.button(
                            "✅ Confirm replace", type="primary", key=f"confirm_replace_{b.id}",
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

                                st.success(
                                    f"Replaced: {len(updated)} product(s) updated in place, "
                                    f"{len(new)} new product(s) added. No duplicates created."
                                )
                                st.session_state[f"show_replace_{b.id}"] = False
                                st.rerun()
                            except Exception as e:
                                b.status = manifest_store.STATUS_ERROR
                                b.error_message = str(e)
                                manifest_store.save_batch(b)
                                st.error(f"Replace failed: {e}")

# ============================================================ INVENTORY ==
elif page == "📦 Inventory":
    st.title("📦 Inventory")

    TRIAGE_LABELS = {
        "": "All",
        "testing_pending": "🔍 Testing pending",
        "ready_for_sale": "✅ Ready for sale",
        "needs_repair": "🔧 Needs repair",
        "for_parts": "♻️ For parts",
        "written_off": "❌ Written off",
    }

    def _render_images(p: Product):
        cols = st.columns(4)
        for i, img_path in enumerate(p.image_paths[:4]):
            if os.path.exists(img_path):
                with cols[i % 4]:
                    st.image(img_path, use_container_width=True)

    def _delete_button(p: Product):
        if st.button("🗑️ Delete", key=f"del_{p.id}"):
            inventory_store.delete_product(p.id, p.company_id)
            audit_store.log_audit(p.company_id, current_user.id, "DELETE_PRODUCT", "product", p.id)
            st.rerun()

    def _render_full_detail(p: Product, tier_label: str = ""):
        badge = "📥 DRAFT" if p.status == "draft" else f"{p.grade or '?'} ({p.grade_confidence}%)"
        title = p.name or p.manifest_item_description or p.model_number or p.asin or p.id
        header = f"🎯 Exact {tier_label} match — {badge} • {title}" if tier_label else f"{badge} • {title}"
        st.markdown(f"### {header}")
        _render_images(p)

        # -- Header: quick actions (triage status / location are the two
        # things someone changes constantly during daily work, so they're
        # editable right here rather than buried in a section below) --
        triage_keys = [k for k in TRIAGE_LABELS if k]
        qa1, qa2, qa3, qa4 = st.columns([2, 2, 1, 1])
        with qa1:
            new_triage = st.selectbox(
                "Triage status", options=triage_keys,
                index=triage_keys.index(p.triage_status) if p.triage_status in triage_keys else 0,
                format_func=lambda k: TRIAGE_LABELS[k],
                key=f"triage_{p.id}",
            )
            if new_triage != p.triage_status:
                p.triage_status = new_triage
                inventory_store.save_product(p)
                audit_store.log_audit(p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id, "triage_status")
                st.rerun()
        with qa2:
            new_location = st.text_input("Location", value=p.location, key=f"loc_{p.id}")
            if new_location != p.location:
                p.location = new_location
                inventory_store.save_product(p)
                audit_store.log_audit(p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id, "location")
                st.rerun()
        with qa3:
            new_quantity = st.number_input(
                "Quantity", min_value=1, value=int(p.quantity or 1), step=1, key=f"qty_{p.id}",
            )
            if int(new_quantity) != p.quantity:
                p.quantity = int(new_quantity)
                inventory_store.save_product(p)
                audit_store.log_audit(p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id, "quantity")
                st.rerun()
        with qa4:
            st.write("")
            _delete_button(p)

        with st.expander("📋 Manifest info", expanded=False):
            if p.manifest_import_id:
                st.write("**Manifest item description:**", p.manifest_item_description or "—")
                st.write(
                    f"**Manifest Target #:** {p.manifest_target_no or '—'}  •  "
                    f"**Subcategory:** {p.manifest_subcategory or '—'}"
                )
                st.write(f"**Manifest ASIN:** {p.asin or '—'}  •  **Manifest barcode:** {p.manifest_barcode or '—'}")
                st.write(f"**Qty:** {p.manifest_qty}  •  **Weight:** {p.manifest_weight_kg} kg")
                st.caption(f"Batch: {p.manifest_import_id}")
            else:
                st.caption("Manually entered — no manifest origin.")

        with st.expander("🏷️ Product Info", expanded=True):
            c1, c2, c3 = st.columns(3)
            with c1:
                st.write(f"**SKU:** {p.sku or '(not yet assigned)'}")
                st.write(f"**Brand:** {p.brand or '—'}")
                st.write(f"**Model:** {p.model or p.model_number or '—'}")
                st.write(f"**Category:** {p.category or '—'}")
            with c2:
                st.write(f"**EAN:** {p.ean or '—'}  ({p.ean_status or 'not checked'})")
                st.write(f"**ASIN:** {p.asin or '—'}  ({p.asin_status or 'not checked'})")
                if p.asin_candidates:
                    st.caption(f"Other possible ASINs: {', '.join(p.asin_candidates)}")
                st.write(f"**Condition:** {p.condition_type or '—'}")
            with c3:
                st.write(f"**Grade:** {p.grade or '—'} ({p.grade_confidence}%)")
                st.write(f"**Functional test:** {p.functional_test_result or '—'}")
                box_dims = ", ".join(
                    f"{v:g} cm" for v in (p.box_length_cm, p.box_width_cm, p.box_height_cm) if v
                )
                st.write(f"**Box dimensions:** {box_dims or '—'}")

        with st.expander("🔍 Testing & Defects", expanded=(p.status != "draft")):
            if p.status != "draft":
                st.write(f"**Product match:** {p.product_match or 'UNKNOWN'} ({p.match_confidence}%)")
                if p.match_notes:
                    st.caption(p.match_notes)
                st.write("**Spec summary:**", p.spec_summary or "—")
                st.write("**Box contents:**", ", ".join(p.box_contents) or "—")
                st.write("**Missing components:**", ", ".join(p.missing_components) or "—")
                st.write("**Defects:**", ", ".join(p.defects) or "—")
                st.write("**Functional test checklist:**", ", ".join(p.functional_checklist) or "—")
            else:
                st.caption("Not yet tested — still a pending manifest draft.")

        events = repair_store.list_repair_events(p.id, p.company_id)
        repair_total = repair_store.total_repair_cost(p.id, p.company_id)
        with st.expander(f"🛠️ Repair History ({len(events)}) — ${repair_total:.2f} total"):
            for e in events:
                rc1, rc2 = st.columns([5, 1])
                with rc1:
                    when = time.strftime("%Y-%m-%d", time.localtime(e.occurred_at))
                    st.write(f"**{when}** — {e.description or '(no description)'} — ${e.cost:.2f}"
                             + (f" — {e.technician}" if e.technician else ""))
                with rc2:
                    if st.button("🗑️", key=f"delrepair_{e.id}"):
                        repair_store.delete_repair_event(e.id, p.company_id)
                        st.rerun()
            with st.form(key=f"addrepair_{p.id}", clear_on_submit=True):
                rf1, rf2, rf3 = st.columns([3, 1, 1])
                with rf1:
                    r_desc = st.text_input("Description", key=f"rdesc_{p.id}")
                with rf2:
                    r_cost = st.number_input("Cost", min_value=0.0, step=0.5, key=f"rcost_{p.id}")
                with rf3:
                    r_tech = st.text_input("Technician", key=f"rtech_{p.id}")
                if st.form_submit_button("➕ Add repair entry"):
                    if r_desc.strip():
                        repair_store.add_repair_event(
                            repair_store.RepairEvent(
                                product_id=p.id, company_id=p.company_id, description=r_desc.strip(),
                                cost=r_cost, technician=r_tech.strip(),
                            )
                        )
                        st.rerun()
                    else:
                        st.warning("Description is required.")

        with st.expander("🛒 Sales & Listings", expanded=False):
            st.write("**Product Description:**", p.product_description or "—")
            st.write("**Condition & Scratches Details:**", p.condition_description or "—")
            st.write(f"**Price:** ${p.price:.2f}")
            if p.price_reasoning:
                st.caption(p.price_reasoning)

            listings = marketplace_store.list_listings(p.id, p.company_id)
            if listings:
                for listing in listings:
                    st.write(
                        f"**{listing.marketplace}:** {listing.status}"
                        + (f" — #{listing.external_listing_id}" if listing.external_listing_id else "")
                        + (f" — {listing.url}" if listing.url else "")
                    )
            else:
                st.caption("Not listed on any marketplace yet.")

            if IntegrationManager.is_connected(p.company_id, "baselinker"):
                bl_col1, bl_col2 = st.columns(2)
                with bl_col1:
                    if st.button("🔍 Preview export", key=f"preview_{p.id}", use_container_width=True):
                        st.session_state[f"show_export_preview_{p.id}"] = True
                with bl_col2:
                    if st.button("📤 Push to BaseLinker", key=f"push_{p.id}", use_container_width=True):
                        result = IntegrationManager.get(p.company_id, "baselinker").export_product(p)
                        if result.success:
                            audit_store.log_audit(
                                p.company_id, current_user.id, "EXPORT_BASELINKER", "product", p.id,
                                f"baselinker_product_id={result.external_id}",
                            )
                            st.success(f"Pushed — BaseLinker product_id: {result.external_id}")
                            st.rerun()
                        else:
                            st.error(result.message)

                if st.session_state.get(f"show_export_preview_{p.id}"):
                    # Built from the exact same connector.export_product() code
                    # path (mapper.build_payload) — can never drift from what a
                    # real push actually sends.
                    payload = IntegrationManager.get(p.company_id, "baselinker").preview_payload(p)
                    text_fields = payload.get("text_fields", {})
                    prices = payload.get("prices") or {}
                    stock = payload.get("stock") or {}
                    with st.expander("🔍 BaseLinker export preview", expanded=True):
                        st.caption(
                            "\"(excluded)\" means this field's toggle is off in Synchronization — "
                            "an empty value shown without that note just means the product itself "
                            "has nothing there yet."
                        )
                        st.write("**Title:**", text_fields["name"] or "— (empty)" if "name" in text_fields else "— (excluded)")
                        st.write(
                            "**Description:**",
                            text_fields["description"] or "— (empty)" if "description" in text_fields else "— (excluded)",
                        )
                        st.write(
                            "**Additional description:**",
                            text_fields["description_extra1"] if "description_extra1" in text_fields else "— (excluded)",
                        )
                        st.write("**SKU:**", payload.get("sku", "—"))
                        st.write("**Barcode:**", payload.get("ean") or "— (excluded)")
                        st.write("**Category ID:**", payload.get("category_id", "—"))
                        st.write("**Price:**", next(iter(prices.values()), None) or "— (excluded or no price set)")
                        st.write("**Quantity:**", next(iter(stock.values()), "— (excluded)"))
                        st.write("**Images:**", f"{payload.get('_preview_image_count', 0)} included")
                        if st.button("Close preview", key=f"close_preview_{p.id}"):
                            st.session_state[f"show_export_preview_{p.id}"] = False
                            st.rerun()

        with st.expander("💰 Financials", expanded=False):
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                new_purchase = st.number_input(
                    "Purchase price (allocated)", min_value=0.0, step=0.5,
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
                st.metric("Repair cost", f"${repair_total:.2f}")
            with fc3:
                profit = p.price - p.purchase_price_allocated - repair_total
                st.metric("Profit (selling − purchase − repairs)", f"${profit:.2f}")

        st.divider()

    search_query = st.text_input(
        "🔍 Exact lookup by SKU, EAN, ASIN, Product Name, Brand, or Model",
        placeholder="e.g. 2001, 0194252057338, B08ASIN123, iPhone 12, Apple, A2172",
        help="Searches the WHOLE inventory regardless of the filters below. "
        "Priority: exact SKU match, then exact EAN, then exact ASIN, then "
        "model number, then brand/product name. Works for manifest-imported "
        "and manually-added items alike.",
    )

    if search_query.strip():
        results = inventory_store.search_products(search_query.strip(), st.session_state.company_id)
        st.caption(f"{len(results)} match(es) for '{search_query.strip()}'")

        tier_labels = {
            inventory_store.MATCH_TIER_SKU: "SKU",
            inventory_store.MATCH_TIER_EAN: "EAN",
            inventory_store.MATCH_TIER_ASIN: "ASIN",
            inventory_store.MATCH_TIER_MODEL: "model",
            inventory_store.MATCH_TIER_BRAND_NAME: "brand/name",
        }
        for p, tier in results:
            _render_full_detail(p, tier_labels.get(tier, ""))
    else:
        st.divider()

        # Manifest batch is the one filter left on the Python side — it's
        # not a displayed column, so there's nothing for the grid itself to
        # filter on. Triage/Grade/Location used to be separate dropdowns
        # here too; now that they're real grid columns, the grid's own
        # per-column filters (and the quick-search box) cover that,
        # matching how Review & Export's grid works.
        batches = manifest_store.list_batches(st.session_state.company_id)
        batch_options = {"": "All batches"}
        batch_options.update({b.id: f"{b.filename} ({b.id})" for b in batches})
        batch_filter = st.selectbox(
            "Manifest batch", options=list(batch_options.keys()),
            format_func=lambda k: batch_options[k], key="inv_filter_batch",
        )

        if batch_filter:
            products = inventory_store.list_products_by_manifest(batch_filter, st.session_state.company_id)
        else:
            products = inventory_store.list_products(st.session_state.company_id)

        st.caption(f"{len(products)} item(s)")

        if not products:
            st.info(
                "No items match these filters. Try widening them, or add "
                "one from 'New Item' / '📥 Import Manifest'."
            )
        else:
            table_rows = []
            for p in products:
                listing = marketplace_store.get_listing(p.id, "baselinker", p.company_id)
                table_rows.append({
                    "id": p.id,
                    "sku": p.sku or "(none)",
                    "title": p.name or p.manifest_item_description or p.model_number or p.id,
                    "triage": TRIAGE_LABELS.get(p.triage_status, p.triage_status),
                    "grade": p.grade or "—",
                    "location": p.location or "—",
                    "price": p.price,
                    "baselinker": listing.status if listing else marketplace_store.STATUS_NOT_LISTED,
                })

            result = inventory_table(
                rows=table_rows,
                selected_id=st.session_state.get("inv_selected_id", ""),
                key="inventory_table",
            )
            if result and result.get("open_id") and result["open_id"] != st.session_state.get("inv_selected_id"):
                # Without the rerun, this render already passed the *old*
                # selected_id into the grid (computed above before this
                # click's result was known) — the row highlight would always
                # lag one click behind. Rerunning immediately means the very
                # next render carries the freshly updated id.
                st.session_state.inv_selected_id = result["open_id"]
                st.rerun()

        selected_id = st.session_state.get("inv_selected_id")
        if selected_id:
            selected_product = inventory_store.get_product(selected_id, st.session_state.company_id)
            if selected_product:
                st.divider()
                if st.button("✖ Close detail view"):
                    del st.session_state["inv_selected_id"]
                    st.rerun()
                _render_full_detail(selected_product)
            else:
                del st.session_state["inv_selected_id"]

# ================================================== REVIEW & EXPORT =====
elif page == "🔍 Review & Export":
    st.title("🔍 Review & Export")
    st.caption(
        "The mandatory checkpoint before anything reaches BaseLinker — "
        "review AI-generated info, fix anything needed, then export only "
        "what you've selected."
    )

    REVIEW_STATUS_LABELS = {
        "": "All",
        "ready": "✅ Ready",
        "edited": "✏️ Edited",
        "exported": "📤 Exported",
        "failed": "❌ Failed",
    }
    GRADE_OPTIONS = ["A", "B", "C", "D"]

    def _review_status_of(p: Product) -> str:
        return p.review_status or "ready"

    all_products = inventory_store.list_products(st.session_state.company_id)
    review_products = [p for p in all_products if p.status == "completed"]
    review_by_id = {p.id: p for p in review_products}

    @st.dialog("Export selected products to BaseLinker?")
    def _confirm_export_dialog(selected_products):
        st.write(f"Export **{len(selected_products)}** selected product(s) to BaseLinker?")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Cancel", use_container_width=True, key="review_export_cancel"):
                st.rerun()
        with col2:
            confirmed = st.button(
                "📤 Export", type="primary", use_container_width=True, key="review_export_confirm"
            )
        if confirmed:
            results_box = st.container()
            progress = st.progress(0.0, text="Starting...")
            ok_count, fail_count = 0, 0
            for i, p in enumerate(selected_products):
                progress.progress(
                    i / len(selected_products),
                    text=f"Exporting {p.sku} ({i + 1}/{len(selected_products)})...",
                )
                try:
                    result = IntegrationManager.get(p.company_id, "baselinker").export_product(p)
                except (IntegrationNotConnectedError, IntegrationNotAvailableError) as e:
                    result = ConnectorActionResult(success=False, message=str(e))
                if result.success:
                    p.review_status = "exported"
                    p.exported_at = time.time()
                    ok_count += 1
                    results_box.success(f"✅ {p.sku}: {result.message}")
                    audit_store.log_audit(
                        p.company_id, current_user.id, "EXPORT_BASELINKER", "product", p.id,
                        f"baselinker_product_id={result.external_id}",
                    )
                else:
                    p.review_status = "failed"
                    fail_count += 1
                    results_box.error(f"❌ {p.sku}: {result.message}")
                inventory_store.save_product(p)
                st.session_state.review_selected_ids.discard(p.id)
            progress.progress(1.0, text="Done.")
            integration_store.record_sync(
                current_user.company_id, "baselinker", "bulk_export_summary",
                status=integration_store.SYNC_STATUS_SUCCESS if not fail_count else integration_store.SYNC_STATUS_ERROR,
                error_message=f"{ok_count} successful, {fail_count} failed" if fail_count else "",
            )
            st.markdown(f"**{ok_count} products exported successfully**")
            if fail_count:
                st.markdown(f"**{fail_count} product(s) failed**")
            if st.button("Close", use_container_width=True, key="review_export_done"):
                st.session_state.review_clear_seq += 1  # tell the grid to deselect all rows
                st.rerun()

    @st.dialog("Photo", width="large")
    def _photo_lightbox_dialog(image_paths, start_index: int):
        idx = st.session_state.get("review_lightbox_index", start_index)
        idx = max(0, min(idx, len(image_paths) - 1))
        img_path = image_paths[idx]
        if os.path.exists(img_path):
            st.image(img_path, use_container_width=True)
        st.caption(f"Photo {idx + 1} / {len(image_paths)}")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("◀ Prev photo", disabled=idx <= 0, use_container_width=True, key="review_lb_prev"):
                st.session_state.review_lightbox_index = idx - 1
                st.rerun()
        with c2:
            if st.button("Close", use_container_width=True, key="review_lb_close"):
                st.rerun()
        with c3:
            if st.button(
                "Next photo ▶", disabled=idx >= len(image_paths) - 1,
                use_container_width=True, key="review_lb_next",
            ):
                st.session_state.review_lightbox_index = idx + 1
                st.rerun()

    if not review_products:
        st.info("No completed items yet — finish some in 🆕 New Item first.")

    elif st.session_state.review_open_product_id is None:
        # -------------------------------------------------------- LIST VIEW --

        @st.fragment
        def _review_list_fragment():
            # Sorting/filtering/search are handled inside the grid itself now
            # (quick-filter box + per-column filters/sort) — Python only
            # decides which products are *eligible* to appear at all.
            filtered = review_products
            st.session_state.review_filtered_ids_cache = [p.id for p in filtered]
            st.caption(f"{len(filtered)} product(s) total")

            if not filtered:
                st.session_state.review_selected_ids = set()
                return

            table_rows = []
            for p in filtered:
                photo_url = None
                if p.image_paths:
                    thumb = _ensure_thumbnail(p.image_paths[0])
                    if thumb:
                        photo_url = _image_static_url(thumb)
                table_rows.append({
                    "id": p.id,
                    "photo_url": photo_url,
                    "sku": p.sku,
                    "name": p.name,
                    "grade": p.grade or "—",
                    "quantity": p.quantity or 1,
                    "price": p.price or 0,
                    "status": REVIEW_STATUS_LABELS[_review_status_of(p)],
                    "date": (
                        time.strftime("%Y-%m-%d", time.localtime(p.exported_at))
                        if p.exported_at else "—"
                    ),
                })

            focus_id = st.session_state.review_focus_id
            st.session_state.review_focus_id = ""  # one-shot: only focus once

            result = review_table(
                rows=table_rows,
                focus_id=focus_id,
                clear_seq=st.session_state.review_clear_seq,
                key="review_table",
            )
            if result:
                st.session_state.review_selected_ids = set(result.get("selected_ids") or [])
                if result.get("open_id"):
                    st.session_state.review_open_product_id = result["open_id"]
                    st.rerun()  # escape fragment scope so the card view (below) renders

            n_selected = len(st.session_state.review_selected_ids)
            st.caption(f"Selected: {n_selected} product(s)")

            can_export = current_user.role in (auth.ROLE_ADMIN, auth.ROLE_REVIEWER)
            if not can_export:
                st.caption("Only Admins and Reviewers can export to BaseLinker.")

            action_col1, action_col2 = st.columns([2, 1])
            with action_col1:
                if st.button(
                    f"📤 Export Selected ({n_selected})",
                    type="primary",
                    disabled=n_selected == 0 or not can_export,
                    use_container_width=True,
                    key="review_export_selected_btn",
                ):
                    st.session_state.review_export_requested = True
                    st.rerun()  # escape fragment scope so the dialog (below, outside the fragment) opens
            with action_col2:
                if n_selected > 0:
                    if st.button("✖ Clear Selection", use_container_width=True, key="review_clear_selection"):
                        st.session_state.review_selected_ids = set()
                        st.session_state.review_clear_seq += 1  # tell the grid to deselect all rows
                        st.rerun()

        _review_list_fragment()

        if st.session_state.review_export_requested:
            st.session_state.review_export_requested = False
            selected_products = [
                review_by_id[pid] for pid in st.session_state.review_selected_ids
                if pid in review_by_id
            ]
            if selected_products:
                _confirm_export_dialog(selected_products)

    else:
        # -------------------------------------------------------- CARD VIEW --
        esc_value = esc_listener(key="review_esc")
        if esc_value is not None and esc_value != st.session_state.review_last_esc_value:
            st.session_state.review_last_esc_value = esc_value
            st.session_state.review_open_product_id = None
            st.rerun()

        pid = st.session_state.review_open_product_id
        p = review_by_id.get(pid)
        if p is None:
            st.warning("Product not found — it may have been deleted.")
            if st.button("← Back to list"):
                st.session_state.review_open_product_id = None
                st.rerun()
        else:
            ordered_ids = st.session_state.review_filtered_ids_cache or [x.id for x in review_products]
            if pid not in ordered_ids:
                ordered_ids = [pid] + ordered_ids
            cur_idx = ordered_ids.index(pid)

            nav1, nav2, nav3, nav4 = st.columns([1.3, 1, 1, 1])
            with nav1:
                if st.button("← Back to list", use_container_width=True):
                    st.session_state.review_open_product_id = None
                    st.rerun()
            with nav2:
                if st.button("◀ Previous Product", disabled=cur_idx <= 0, use_container_width=True):
                    st.session_state.review_open_product_id = ordered_ids[cur_idx - 1]
                    st.rerun()
            with nav3:
                if st.button("Next Product ▶", disabled=cur_idx >= len(ordered_ids) - 1, use_container_width=True):
                    st.session_state.review_open_product_id = ordered_ids[cur_idx + 1]
                    st.rerun()
            with nav4:
                st.caption(f"{cur_idx + 1} / {len(ordered_ids)}")

            st.subheader(f"{p.sku} — {p.name or '(no name)'}")
            st.caption(REVIEW_STATUS_LABELS[_review_status_of(p)])
            st.divider()

            # Not wrapped in st.form: the Photos section below (per-photo
            # "Enlarge" buttons, its own upload control) needs regular
            # widgets, which st.form forbids — and it belongs right above
            # Save, not above these fields. Every widget gets an explicit
            # per-product key so Previous/Next don't leave a stale typed
            # value behind when the label text is identical across products.
            st.markdown("**Product Information**")
            name_in = st.text_input("Product Name", value=p.name, key=f"review_name_{p.id}")
            pi1, pi2 = st.columns(2)
            with pi1:
                brand_in = st.text_input("Brand", value=p.brand, key=f"review_brand_{p.id}")
                barcode_in = st.text_input(
                    "Barcode", value=p.ean or p.manifest_barcode or p.scanned_barcode,
                    key=f"review_barcode_{p.id}",
                )
            with pi2:
                model_in = st.text_input("Model", value=p.model, key=f"review_model_{p.id}")
                category_in = st.text_input("Category", value=p.category, key=f"review_category_{p.id}")
            grade_in = st.selectbox(
                "Grade", GRADE_OPTIONS,
                index=GRADE_OPTIONS.index(p.grade) if p.grade in GRADE_OPTIONS else 1,
                key=f"review_grade_{p.id}",
            )

            st.markdown("**Pricing**")
            pr1, pr2 = st.columns(2)
            with pr1:
                price_in = st.number_input(
                    "Price ($)", min_value=0.0, value=float(p.price), step=1.0, key=f"review_price_{p.id}",
                )
            with pr2:
                quantity_in = st.number_input(
                    "Quantity", min_value=1, value=int(p.quantity or 1), step=1, key=f"review_qty_{p.id}",
                )

            st.markdown("**Product Description**")
            desc_in = st.text_area(
                "Product Description", value=p.product_description, height=120, key=f"review_desc_{p.id}",
            )

            st.markdown("**Additional Description**")
            extra_in = st.text_area(
                "Additional Description (Condition & Scratches Details)",
                value=p.condition_description, height=100, key=f"review_extra_{p.id}",
            )

            st.markdown("**Defects**")
            defects_in = st.text_area(
                "Defects (one per line)", value="\n".join(p.defects), height=80, key=f"review_defects_{p.id}",
            )

            st.markdown("**Missing Components**")
            missing_in = st.text_area(
                "Missing Components (one per line)", value="\n".join(p.missing_components), height=60,
                key=f"review_missing_{p.id}",
            )

            st.markdown("**Box Contents**")
            box_in = st.text_area(
                "Box Contents (one per line)", value="\n".join(p.box_contents), height=60,
                key=f"review_box_{p.id}",
            )

            st.markdown("**Functional Checklist**")
            checklist_in = st.text_area(
                "Functional Checklist (one per line)", value="\n".join(p.functional_checklist), height=80,
                key=f"review_checklist_{p.id}",
            )

            if p.grade_reasoning:
                st.caption(f"Grade reasoning: {p.grade_reasoning}")
            if p.price_reasoning:
                st.caption(f"Price reasoning: {p.price_reasoning}")

            st.divider()
            st.markdown(f"**Photos** ({len(p.image_paths)}/{REVIEW_CARD_MAX_PHOTOS})")
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
                            if st.button("🔍 Enlarge", key=f"review_enlarge_{p.id}_{i}", use_container_width=True):
                                st.session_state.review_lightbox_index = i
                                _photo_lightbox_dialog(p.image_paths, i)
            else:
                st.caption("No photos.")

            remaining_slots = REVIEW_CARD_MAX_PHOTOS - len(p.image_paths)
            if remaining_slots > 0:
                with st.form(key=f"review_photo_upload_{p.id}", clear_on_submit=True):
                    new_photo_files = st.file_uploader(
                        f"➕ Add photos (up to {remaining_slots} more)",
                        type=["jpg", "jpeg", "png"], accept_multiple_files=True,
                    )
                    if st.form_submit_button("Add photos"):
                        if not new_photo_files:
                            st.warning("No photos selected.")
                        else:
                            to_add = new_photo_files[:remaining_slots]
                            if len(new_photo_files) > remaining_slots:
                                st.warning(
                                    f"Only added {remaining_slots} photo(s) — "
                                    f"{REVIEW_CARD_MAX_PHOTOS} photo maximum reached."
                                )
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
                            st.success(f"Added {len(to_add)} photo(s).")
                            st.rerun()
            else:
                st.caption(f"Maximum of {REVIEW_CARD_MAX_PHOTOS} photos reached.")

            st.divider()
            if st.button("💾 Save", type="primary", use_container_width=True, key=f"review_save_{p.id}"):
                new_defects = [l.strip() for l in defects_in.splitlines() if l.strip()]
                new_missing = [l.strip() for l in missing_in.splitlines() if l.strip()]
                new_box = [l.strip() for l in box_in.splitlines() if l.strip()]
                new_checklist = [l.strip() for l in checklist_in.splitlines() if l.strip()]

                current_barcode = p.ean or p.manifest_barcode or p.scanned_barcode
                changed = (
                    name_in.strip() != p.name
                    or brand_in.strip() != p.brand
                    or model_in.strip() != p.model
                    or category_in.strip() != p.category
                    or barcode_in.strip() != current_barcode
                    or grade_in != p.grade
                    or float(price_in) != float(p.price)
                    or int(quantity_in) != p.quantity
                    or desc_in.strip() != p.product_description
                    or extra_in.strip() != p.condition_description
                    or new_defects != p.defects
                    or new_missing != p.missing_components
                    or new_box != p.box_contents
                    or new_checklist != p.functional_checklist
                )

                p.name = name_in.strip()
                p.brand = brand_in.strip()
                p.model = model_in.strip()
                p.category = category_in.strip()
                if barcode_in.strip() != current_barcode:
                    p.ean = barcode_in.strip()
                    p.ean_source = "manual"
                    p.ean_status = "Found" if barcode_in.strip() else ""
                p.grade = grade_in
                p.price = float(price_in)
                p.quantity = int(quantity_in)
                p.product_description = desc_in.strip()
                p.condition_description = extra_in.strip()
                p.defects = new_defects
                p.missing_components = new_missing
                p.box_contents = new_box
                p.functional_checklist = new_checklist
                if changed:
                    p.review_status = "edited"

                inventory_store.save_product(p)
                if changed:
                    audit_store.log_audit(p.company_id, current_user.id, "UPDATE_PRODUCT", "product", p.id)
                st.session_state.review_open_product_id = None
                st.session_state.review_focus_id = p.id  # scroll/highlight this row back in the list
                st.success("Saved.")
                st.rerun()

# =============================================================== EXPORT ==
elif page == "📤 CSV/Excel Export":
    st.title("📤 CSV/Excel Export")
    st.caption(
        "Spreadsheet export for manual/CSV import into other tools. To push "
        "directly to BaseLinker, use 🔍 Review & Export instead."
    )
    all_products = inventory_store.list_products(st.session_state.company_id)
    exportable = [p for p in all_products if p.status != "draft"]
    exportable_by_id = {p.id: p for p in exportable}

    if not exportable:
        st.info("No completed items to export yet (pending manifest drafts are excluded).")

    elif st.session_state.export_open_product_id is None:
        # -------------------------------------------------------- LIST VIEW --

        @st.fragment
        def _export_list_fragment():
            st.session_state.export_filtered_ids_cache = [p.id for p in exportable]

            table_rows = []
            for p in exportable:
                table_rows.append({
                    "id": p.id,
                    "sku": p.sku or "(none)",
                    "name": p.name or p.manifest_item_description or p.model_number or p.id,
                    "brand": p.brand or "—",
                    "grade": p.grade or "—",
                    "price": p.price,
                    "quantity": p.quantity or 1,
                })

            result = export_table(
                rows=table_rows,
                clear_seq=st.session_state.export_clear_seq,
                key="export_table",
            )
            if result:
                st.session_state.export_selected_ids = set(result.get("selected_ids") or [])
                if result.get("open_id"):
                    st.session_state.export_open_product_id = result["open_id"]
                    st.rerun()  # escape fragment scope so the detail view (below) renders

            n_selected = len(st.session_state.export_selected_ids)
            st.caption(f"Selected: {n_selected} product(s)")
            selected_products = [
                exportable_by_id[pid] for pid in st.session_state.export_selected_ids
                if pid in exportable_by_id
            ]

            dl1, dl2, dl3 = st.columns([1, 1, 1])
            with dl1:
                st.download_button(
                    "⬇️ Download Excel (.xlsx)",
                    data=export.to_excel_bytes(selected_products) if selected_products else b"",
                    file_name="baselinker_export.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    disabled=n_selected == 0,
                    use_container_width=True,
                )
            with dl2:
                st.download_button(
                    "⬇️ Download CSV",
                    data=export.to_csv_bytes(selected_products) if selected_products else b"",
                    file_name="baselinker_export.csv",
                    mime="text/csv",
                    disabled=n_selected == 0,
                    use_container_width=True,
                )
            with dl3:
                if n_selected > 0:
                    if st.button("✖ Clear Selection", use_container_width=True, key="export_clear_selection"):
                        st.session_state.export_selected_ids = set()
                        st.session_state.export_clear_seq += 1  # tell the grid to deselect all rows
                        st.rerun()

            st.caption(
                "Note: 'Image Links' currently contains local file paths. "
                "Baselinker's importer needs public image URLs — upload the "
                "photos to hosting (or Baselinker's own media manager) and "
                "substitute the URLs, or attach photos manually per listing "
                "after import."
            )

        _export_list_fragment()

    else:
        # -------------------------------------------------------- DETAIL VIEW --
        # Read-only — editing already lives on 🔍 Review & Export; this page
        # is purely for picking which products go into the downloaded file,
        # with a way to inspect any one of them in full before deciding.
        esc_value = esc_listener(key="export_esc")
        if esc_value is not None and esc_value != st.session_state.export_last_esc_value:
            st.session_state.export_last_esc_value = esc_value
            st.session_state.export_open_product_id = None
            st.rerun()

        pid = st.session_state.export_open_product_id
        p = exportable_by_id.get(pid)
        if p is None:
            st.warning("Product not found — it may have been deleted.")
            if st.button("← Back to list"):
                st.session_state.export_open_product_id = None
                st.rerun()
        else:
            ordered_ids = st.session_state.export_filtered_ids_cache or [x.id for x in exportable]
            if pid not in ordered_ids:
                ordered_ids = [pid] + ordered_ids
            cur_idx = ordered_ids.index(pid)

            nav1, nav2, nav3, nav4 = st.columns([1.3, 1, 1, 1])
            with nav1:
                if st.button("← Back to list", use_container_width=True):
                    st.session_state.export_open_product_id = None
                    st.rerun()
            with nav2:
                if st.button("◀ Previous Product", disabled=cur_idx <= 0, use_container_width=True):
                    st.session_state.export_open_product_id = ordered_ids[cur_idx - 1]
                    st.rerun()
            with nav3:
                if st.button("Next Product ▶", disabled=cur_idx >= len(ordered_ids) - 1, use_container_width=True):
                    st.session_state.export_open_product_id = ordered_ids[cur_idx + 1]
                    st.rerun()
            with nav4:
                st.caption(f"{cur_idx + 1} / {len(ordered_ids)}")

            st.subheader(f"{p.sku} — {p.name or '(no name)'}")
            st.caption("Read-only preview of everything that goes into the exported file.")
            st.divider()

            # Built from the exact same function that generates the actual
            # download, so this view can never drift out of sync with what
            # really ends up in the file.
            row = export.products_to_dataframe([p]).iloc[0].to_dict()

            LONG_FIELDS = {
                "Image Links", "Product Description", "Condition & Scratches Details",
                "Functional Test Checklist", "Missing Components",
            }
            short_fields = [c for c in export.COLUMNS if c not in LONG_FIELDS]
            sf_cols = st.columns(2)
            for i, field in enumerate(short_fields):
                with sf_cols[i % 2]:
                    st.write(f"**{field}:** {row.get(field) or '—'}")

            st.divider()
            for field in export.COLUMNS:
                if field in LONG_FIELDS:
                    st.markdown(f"**{field}**")
                    st.text(row.get(field) or "—")

# =========================================================== MANAGE USERS =
elif page == "👥 Manage Users":
    st.title("👥 Manage Users")
    try:
        auth.require_role(current_user, auth.ROLE_ADMIN)
    except PermissionError:
        st.error("Admins only.")
        st.stop()

    st.caption(f"Users in {current_company.name if current_company else current_user.company_id}")

    users = auth_store.list_users_for_company(current_user.company_id)
    for u in users:
        c1, c2, c3, c4 = st.columns([2, 3, 1.5, 1.5])
        with c1:
            st.write(u.name)
        with c2:
            st.write(u.email)
        with c3:
            st.write(u.role)
        with c4:
            toggle_label = "Deactivate" if u.active else "Activate"
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
            st.caption("Inactive")
        st.divider()

    st.subheader("Add a user")
    with st.form("create_user_form", clear_on_submit=True):
        new_name = st.text_input("Name")
        new_email = st.text_input("Email")
        new_password = st.text_input("Password", type="password")
        new_role = st.selectbox("Role", auth.ALL_ROLES, index=auth.ALL_ROLES.index(auth.ROLE_EMPLOYEE))
        if st.form_submit_button("➕ Add user", type="primary"):
            if not new_name.strip() or not new_email.strip() or not new_password:
                st.warning("Name, email, and password are all required.")
            else:
                try:
                    auth.register_user(
                        company_id=current_user.company_id,
                        name=new_name, email=new_email, password=new_password, role=new_role,
                    )
                    st.success(f"Added {new_email}.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

elif page == "⚙️ Settings":
    st.title("⚙️ Settings")
    try:
        auth.require_role(current_user, auth.ROLE_ADMIN)
    except PermissionError:
        st.error("Admins only.")
        st.stop()

    (tab_integrations,) = st.tabs(["🔌 Integrations"])

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
            "connected": ("Connected", "#16a34a", "🟢"),
            "attention": ("Needs attention", "#eab308", "🟡"),
            "failed": ("Connection failed", "#dc2626", "🔴"),
            "never_synced": ("Never synchronized", "#9ca3af", "⚪"),
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
                return f"Inventory {record.settings['inventory_id']}"
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

        @st.dialog("Disconnect integration?")
        def _confirm_disconnect_integration_dialog(integration_type: str, display_name: str):
            st.warning(
                f"This disconnects **{display_name}** for {_company_label}. Stored "
                "credentials are deleted (other settings are kept); pushing products "
                "through it will stop working until reconnected. This cannot be undone."
            )
            confirm = st.checkbox("I understand, disconnect", key=f"disconnect_confirm_{integration_type}")
            dcol1, dcol2 = st.columns(2)
            with dcol1:
                if st.button("Cancel", use_container_width=True, key=f"disconnect_cancel_{integration_type}"):
                    st.rerun()
            with dcol2:
                if st.button(
                    "🔌 Disconnect", type="primary", disabled=not confirm,
                    use_container_width=True, key=f"disconnect_go_{integration_type}",
                ):
                    IntegrationManager.disconnect(current_user.company_id, integration_type, user_id=current_user.id)
                    st.success(f"{display_name} disconnected.")
                    st.rerun()

        def _render_integration_activity(integration_type: str) -> None:
            entries = integration_store.list_sync_log(current_user.company_id, integration_type, limit=10)
            with st.expander("Recent activity"):
                if not entries:
                    st.caption("No activity yet.")
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
                        line += f" (product {e.product_id})"
                    if e.error_message:
                        line += f": {e.error_message}"
                    st.caption(line)

        def _render_integration_test_and_disconnect(entry, connected: bool) -> None:
            if not connected:
                return
            tcol1, tcol2 = st.columns(2)
            with tcol1:
                if st.button("🔄 Test connection", key=f"test_{entry.integration_type}", use_container_width=True):
                    result = IntegrationManager.test(current_user.company_id, entry.integration_type)
                    (st.success if result.success else st.error)(result.message)
            with tcol2:
                if st.button("🔌 Disconnect", key=f"disconnect_open_{entry.integration_type}", use_container_width=True):
                    _confirm_disconnect_integration_dialog(entry.integration_type, entry.display_name)

        def _render_baselinker_settings(entry, record) -> None:
            connected = record is not None and record.status == integration_store.STATUS_CONNECTED
            has_credentials = record is not None and bool(record.credentials)
            if record and record.last_sync_at:
                st.caption(f"Last sync: {time.strftime('%Y-%m-%d %H:%M', time.localtime(record.last_sync_at))}")
            settings = record.settings if record else {}
            with st.form(f"integration_form_{entry.integration_type}"):
                token = st.text_input(
                    "API token", type="password",
                    placeholder="Leave blank to keep current" if has_credentials else "",
                    help="Account & other -> My account -> API in the BaseLinker/Base.com panel.",
                )
                inventory_id = st.text_input("Inventory ID", value=settings.get("inventory_id") or "")
                category_id = st.text_input("Category ID", value=settings.get("category_id") or "")
                price_group_id = st.text_input("Price group ID (optional)", value=settings.get("price_group_id") or "")
                warehouse_id = st.text_input("Warehouse ID (optional)", value=settings.get("warehouse_id") or "")
                tax_rate = st.text_input("Tax rate % (optional)", value=settings.get("tax_rate") or "23")
                submitted = st.form_submit_button("💾 Save & test connection", type="primary")
                if submitted:
                    if not token and not has_credentials:
                        st.warning("API token is required.")
                    elif not inventory_id.strip() or not category_id.strip():
                        st.warning("Inventory ID and Category ID are required.")
                    else:
                        creds = {"token": token} if token else dict(record.credentials)
                        new_settings = {
                            "inventory_id": inventory_id.strip(),
                            "category_id": category_id.strip(),
                            "price_group_id": price_group_id.strip(),
                            "warehouse_id": warehouse_id.strip(),
                            "tax_rate": tax_rate.strip() or "23",
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
                st.caption(f"Last sync: {time.strftime('%Y-%m-%d %H:%M', time.localtime(record.last_sync_at))}")
            with st.form(f"integration_form_{entry.integration_type}"):
                api_key = st.text_input(
                    "API key", type="password",
                    placeholder="Leave blank to keep current" if has_credentials else "",
                )
                submitted = st.form_submit_button("💾 Save & test connection", type="primary")
                if submitted:
                    if not api_key and not has_credentials:
                        st.warning("API key is required.")
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
            (sync_rules_store.FREQUENCY_MANUAL, "Manual"),
            (sync_rules_store.FREQUENCY_EVERY_15_MIN, "Every 15 minutes"),
            (sync_rules_store.FREQUENCY_HOURLY, "Hourly"),
            (sync_rules_store.FREQUENCY_DAILY, "Daily"),
        ]
        _DIRECTION_OPTIONS = [
            (sync_rules_store.DIRECTION_PUSH, "ElectroGrader → Platform"),
            (sync_rules_store.DIRECTION_PULL, "Platform → ElectroGrader"),
            (sync_rules_store.DIRECTION_TWO_WAY, "Two-way"),
        ]
        _CONFLICT_OPTIONS = [
            (sync_rules_store.CONFLICT_KEEP_LOCAL, "Keep ElectroGrader data"),
            (sync_rules_store.CONFLICT_KEEP_REMOTE, "Keep external platform data"),
            (sync_rules_store.CONFLICT_ASK_USER, "Ask me"),
        ]
        # (trigger_event, job_type, checkbox label) — Phase 1's fixed starter
        # set; the underlying storage (SyncRule.automation_triggers) is a
        # JSON list, not fixed columns, so more triggers can be added later
        # without a migration.
        _AUTOMATION_TRIGGER_DEFAULTS = [
            ("product_completed", sync_job_store.JOB_TYPE_PRODUCT_EXPORT,
             "When a product is marked completed → export it automatically"),
            ("product_updated", sync_job_store.JOB_TYPE_PRODUCT_EXPORT,
             "When a product's price/description/photos/quantity changes → update the listing"),
            ("stock_changed", sync_job_store.JOB_TYPE_LISTING_END,
             "When stock reaches zero → end the listing"),
        ]

        def _render_synchronization_tab(entry, rule: sync_rules_store.SyncRule) -> None:
            implemented = IntegrationManager.get_implemented_sync_fields(entry.integration_type)
            st.caption("Choose what ElectroGrader sends to / receives from this integration, and how often.")
            st.markdown("**Data sent from ElectroGrader**")
            send_cols = st.columns(3)
            new_fields_send = []
            for i, (key, label) in enumerate(_SYNC_FIELDS_SENT):
                with send_cols[i % 3]:
                    checkbox_label = label if key in implemented else f"{label} _(not yet applied to export)_"
                    if st.checkbox(
                        checkbox_label, value=key in rule.fields_send,
                        key=f"sync_send_{entry.integration_type}_{key}",
                    ):
                        new_fields_send.append(key)

            st.markdown("**Data received from external platform**")
            recv_cols = st.columns(3)
            new_fields_receive = []
            for i, (key, label) in enumerate(_SYNC_FIELDS_RECEIVED):
                with recv_cols[i % 3]:
                    if st.checkbox(
                        label, value=key in rule.fields_receive, key=f"sync_recv_{entry.integration_type}_{key}",
                    ):
                        new_fields_receive.append(key)

            st.divider()
            st.markdown("**Synchronization rules**")
            freq_keys = [k for k, _ in _FREQUENCY_OPTIONS]
            frequency = st.selectbox(
                "Frequency", freq_keys,
                index=freq_keys.index(rule.frequency) if rule.frequency in freq_keys else 0,
                format_func=lambda k: dict(_FREQUENCY_OPTIONS)[k], key=f"sync_freq_{entry.integration_type}",
            )
            if frequency != sync_rules_store.FREQUENCY_MANUAL:
                st.caption(
                    "⏭️ Phase 1: scheduled runs are queued and logged, but no integration has real "
                    "automatic sync wired up yet — expect a \"Skipped (not implemented yet)\" entry "
                    "in Logs each time, until that integration's automation ships."
                )
            dir_keys = [k for k, _ in _DIRECTION_OPTIONS]
            direction = st.selectbox(
                "Direction", dir_keys,
                index=dir_keys.index(rule.direction) if rule.direction in dir_keys else 0,
                format_func=lambda k: dict(_DIRECTION_OPTIONS)[k], key=f"sync_dir_{entry.integration_type}",
            )
            conflict_keys = [k for k, _ in _CONFLICT_OPTIONS]
            conflict_handling = st.selectbox(
                "Conflict handling", conflict_keys,
                index=conflict_keys.index(rule.conflict_handling) if rule.conflict_handling in conflict_keys else 0,
                format_func=lambda k: dict(_CONFLICT_OPTIONS)[k], key=f"sync_conflict_{entry.integration_type}",
            )

            if st.button("💾 Save synchronization settings", key=f"sync_save_{entry.integration_type}", type="primary"):
                rule.fields_send = new_fields_send
                rule.fields_send_configured = True  # a real Save always counts as "configured",
                # even if the admin deliberately leaves every box unchecked.
                rule.fields_receive = new_fields_receive
                rule.frequency = frequency
                rule.direction = direction
                rule.conflict_handling = conflict_handling
                sync_rules_store.upsert_rule(rule)
                st.success("Saved.")
                st.rerun()

        def _render_field_mapping_tab(entry, mapping: field_mapping_store.FieldMapping) -> None:
            target_fields = IntegrationManager.get_supported_target_fields(entry.integration_type)
            if not target_fields:
                st.info(f"{entry.display_name} has no mappable fields yet.")
                return

            st.caption(
                "Map ElectroGrader data onto this integration's technical fields. "
                "One active configuration per integration."
            )
            search = st.text_input("🔍 Search mapping rules...", key=f"mapping_search_{entry.integration_type}")
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
                st.caption("Clear the search box to edit and save mappings.")
                return

            edited = st.data_editor(
                df, num_rows="dynamic", use_container_width=True, hide_index=True,
                key=f"mapping_editor_{entry.integration_type}",
                column_config={
                    "Source field": st.column_config.SelectboxColumn(options=source_field_keys, required=True),
                    "Target field": st.column_config.SelectboxColumn(options=list(target_fields.keys()), required=True),
                },
            )
            if st.button("💾 Save field mapping", key=f"mapping_save_{entry.integration_type}", type="primary"):
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
                st.success("Saved.")
                st.rerun()

        def _render_automation_tab(entry, rule: sync_rules_store.SyncRule) -> None:
            st.caption("These actions are saved but not executed automatically yet — real automation is future work.")
            triggers_by_event = {t.get("trigger_event"): t for t in rule.automation_triggers}
            new_triggers = []
            for trigger_event, job_type, label in _AUTOMATION_TRIGGER_DEFAULTS:
                existing = triggers_by_event.get(trigger_event, {})
                enabled = st.checkbox(
                    label, value=bool(existing.get("enabled", False)),
                    key=f"automation_{entry.integration_type}_{trigger_event}",
                )
                new_triggers.append({"trigger_event": trigger_event, "job_type": job_type, "enabled": enabled})

            if st.button("💾 Save automation settings", key=f"automation_save_{entry.integration_type}", type="primary"):
                rule.automation_triggers = new_triggers
                sync_rules_store.upsert_rule(rule)
                st.success("Saved.")
                st.rerun()

        def _render_logs_tab(entry) -> None:
            _render_integration_activity(entry.integration_type)
            st.markdown("**Scheduled sync jobs**")
            jobs = sync_job_store.list_jobs(current_user.company_id, entry.integration_type, limit=10)
            if not jobs:
                st.caption("No scheduled jobs yet.")
            job_icons = {
                sync_job_store.STATUS_SUCCESS: "✅", sync_job_store.STATUS_ERROR: "❌",
                sync_job_store.STATUS_SKIPPED: "⏭️", sync_job_store.STATUS_RETRYING: "🔁",
                sync_job_store.STATUS_PENDING: "⏳", sync_job_store.STATUS_RUNNING: "⚙️",
            }
            for j in jobs:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(j.created_at)) if j.created_at else "—"
                icon = job_icons.get(j.status, "•")
                st.caption(f"{icon} {ts} — {j.job_type} ({j.status}, attempt {j.attempts}/{j.max_attempts})")

        def _render_catalog_card(entry) -> None:
            with st.container(border=True, key=f"catalog_card_{entry.integration_type}"):
                _render_integration_logo(entry.integration_type)
                st.markdown(f"**{entry.display_name}**")
                st.caption(entry.description)
                if not entry.available:
                    st.caption("🔒 Coming Soon")
                    return
                record = integration_store.get_integration(current_user.company_id, entry.integration_type)
                if record is not None and record.status == integration_store.STATUS_CONNECTED:
                    st.caption("✅ Connected")
                btn_label = "Edit" if record is not None else "Connect"
                if st.button(btn_label, key=f"catalog_btn_{entry.integration_type}", use_container_width=True):
                    st.session_state.settings_open_integration = entry.integration_type
                    st.rerun()

        @st.dialog("Add Integration", width="large")
        def _add_integration_dialog():
            search = st.text_input("🔍 Search integrations...", key="add_integration_search")
            selected_group = st.pills(
                "Category", ["All"] + _UI_GROUPS, selection_mode="single",
                default="All", key="add_integration_group",
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
                st.info("No integrations match your search.")
            for group in _UI_GROUPS:
                group_entries = [e for e in filtered if e.ui_group == group]
                if not group_entries:
                    continue
                st.markdown(f"**{group}**")
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
            if st.button("← Back to Integrations", key="settings_back"):
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
                    st.caption("⚪ Not connected yet")
            st.divider()

            tab_general, tab_sync, tab_mapping, tab_automation, tab_logs = st.tabs(
                ["General", "Synchronization", "Field Mapping", "Automation", "Logs"]
            )
            with tab_general:
                _INTEGRATION_SETTINGS_RENDERERS[open_entry.integration_type](open_entry, record)
                _render_integration_test_and_disconnect(open_entry, connected)
            with tab_sync:
                _render_synchronization_tab(open_entry, rule)
            with tab_mapping:
                _render_field_mapping_tab(open_entry, mapping)
            with tab_automation:
                _render_automation_tab(open_entry, rule)
            with tab_logs:
                _render_logs_tab(open_entry)

        else:
            # ---- A) Dashboard: only connected integrations, as cards --
            head_col1, head_col2 = st.columns([4, 1])
            with head_col1:
                st.caption(f"Connect marketplaces and external services for {_company_label}.")
            with head_col2:
                if st.button("➕ Add Integration", use_container_width=True, key="open_add_integration"):
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
                        "<h3>No integrations connected yet</h3>"
                        "<p style='color:#6b7280;'>Connect a marketplace or service to start "
                        "syncing products automatically.</p></div>",
                        unsafe_allow_html=True,
                    )
                    if st.button(
                        "➕ Add Integration", use_container_width=True, type="primary", key="empty_add_integration",
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
                                f"Last synced: {time.strftime('%Y-%m-%d %H:%M', time.localtime(record.last_sync_at))}"
                                if record.last_sync_at else "Never synchronized"
                            )
                            if st.button("Edit", key=f"card_edit_{record.integration_type}", use_container_width=True):
                                st.session_state.settings_open_integration = record.integration_type
                                st.rerun()
                            if st.button(
                                "Disconnect", key=f"card_disconnect_{record.integration_type}",
                                use_container_width=True,
                            ):
                                _confirm_disconnect_integration_dialog(record.integration_type, entry.display_name)
