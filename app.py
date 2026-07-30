"""ElectroGrader — mobile-first PWA for fast used-electronics grading,
inventory management and Baselinker-ready export.

Run with:  streamlit run app.py
"""
import concurrent.futures
import hashlib
import io
import math
import os
import re
import time
import uuid
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import pandas as pd
from PIL import Image, ImageOps

from modules import (
    barcode_scanner,
    baselinker_client,
    description_gen,
    export,
    identifier_lookup,
    image_pipeline,
    inventory_store,
    manifest_import,
    manifest_store,
    marketplace_store,
    pricing,
    pwa,
    repair_store,
    spec_lookup,
    vision_grading,
)
from modules.models import Product

load_dotenv()

UPLOAD_DIR = Path(__file__).resolve().parent / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_UNSAFE_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\s]+')

# Below this, a manifest-vs-photo match is flagged to the user as suspect.
MATCH_CONFIDENCE_WARNING_THRESHOLD = 60


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
    layout="centered",
    initial_sidebar_state="collapsed",
)
pwa.inject_pwa_head()

# ---------------------------------------------------------------- session --

def _init_state():
    defaults = {
        "company_id": "default",
        "wizard_step": 1,
        "product": Product(),
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
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


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
            manifest_store.delete_batch(batch.id)
            st.success(f"Deleted batch and {deleted_count} pending product(s).")
            st.rerun()


# ------------------------------------------------------------------- nav --

st.sidebar.title("📱 ElectroGrader")
st.sidebar.text_input(
    "Company",
    key="company_id",
    help="Scopes inventory per business. Each company only sees its own items — "
    "groundwork for future multi-company use.",
)
page = st.sidebar.radio(
    "Navigate",
    ["🆕 New Item", "📥 Import Manifest", "📦 Inventory", "📤 Export"],
    label_visibility="collapsed",
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
                    chosen = inventory_store.get_product(options[chosen_label])

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
                product.status = "completed"
                product.company_id = st.session_state.company_id

                item_dir = UPLOAD_DIR / sku_folder_name(product.sku, product.id)
                item_dir.mkdir(parents=True, exist_ok=True)
                saved_paths = []
                for i, img_bytes in enumerate(st.session_state.captured_photos):
                    fp = item_dir / f"photo_{i+1}.jpg"
                    fp.write_bytes(img_bytes)
                    saved_paths.append(str(fp))
                product.image_paths = saved_paths

                excel_warning = inventory_store.save_product(product)
                st.success(f"Saved '{product.name or product.model_number}' to inventory.")
                if excel_warning:
                    st.warning(excel_warning)
                st.balloons()
                reset_wizard()

    st.divider()
    if st.button("🔄 Start over / discard this item"):
        reset_wizard()
        st.rerun()

# ======================================================= IMPORT MANIFEST =
elif page == "📥 Import Manifest":
    st.title("📥 Import Manifest")
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

                excel_warning = inventory_store.save_products_bulk(drafts)
                batch.status = manifest_store.STATUS_IMPORTED
                manifest_store.save_batch(batch)

                st.success(
                    f"Created manifest batch **{batch.id}** with {len(drafts)} item(s), "
                    f"assigned SKU **{skus[0]}** to **{skus[-1]}**."
                )
                if excel_warning:
                    st.warning(excel_warning)
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

                                excel_warning = inventory_store.save_products_bulk(updated + new)
                                b.filename = replace_upload.name
                                b.row_count = len(r_rows)
                                b.column_map = r_confirmed_map
                                b.updated_at = time.time()
                                b.status = manifest_store.STATUS_IMPORTED
                                b.error_message = ""
                                manifest_store.save_batch(b)

                                st.success(
                                    f"Replaced: {len(updated)} product(s) updated in place, "
                                    f"{len(new)} new product(s) added. No duplicates created."
                                )
                                if excel_warning:
                                    st.warning(excel_warning)
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
    INVENTORY_PAGE_SIZE = 50

    def _render_images(p: Product):
        cols = st.columns(4)
        for i, img_path in enumerate(p.image_paths[:4]):
            if os.path.exists(img_path):
                with cols[i % 4]:
                    st.image(img_path, use_container_width=True)

    def _delete_button(p: Product):
        if st.button("🗑️ Delete", key=f"del_{p.id}"):
            excel_warning = inventory_store.delete_product(p.id)
            if excel_warning:
                st.warning(excel_warning)
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
        qa1, qa2, qa3 = st.columns([2, 2, 1])
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
                st.rerun()
        with qa2:
            new_location = st.text_input("Location", value=p.location, key=f"loc_{p.id}")
            if new_location != p.location:
                p.location = new_location
                inventory_store.save_product(p)
                st.rerun()
        with qa3:
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

        events = repair_store.list_repair_events(p.id)
        repair_total = repair_store.total_repair_cost(p.id)
        with st.expander(f"🛠️ Repair History ({len(events)}) — ${repair_total:.2f} total"):
            for e in events:
                rc1, rc2 = st.columns([5, 1])
                with rc1:
                    when = time.strftime("%Y-%m-%d", time.localtime(e.occurred_at))
                    st.write(f"**{when}** — {e.description or '(no description)'} — ${e.cost:.2f}"
                             + (f" — {e.technician}" if e.technician else ""))
                with rc2:
                    if st.button("🗑️", key=f"delrepair_{e.id}"):
                        repair_store.delete_repair_event(e.id)
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
                                product_id=p.id, description=r_desc.strip(),
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

            listings = marketplace_store.list_listings(p.id)
            if listings:
                for listing in listings:
                    st.write(
                        f"**{listing.marketplace}:** {listing.status}"
                        + (f" — #{listing.external_listing_id}" if listing.external_listing_id else "")
                        + (f" — {listing.url}" if listing.url else "")
                    )
            else:
                st.caption("Not listed on any marketplace yet.")

            if baselinker_client.is_configured():
                if st.button("📤 Push to BaseLinker", key=f"push_{p.id}"):
                    result = baselinker_client.push_product(p)
                    if result.success:
                        st.success(f"Pushed — BaseLinker product_id: {result.baselinker_product_id}")
                        st.rerun()
                    else:
                        st.error(result.message)

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

        batches = manifest_store.list_batches(st.session_state.company_id)
        batch_options = {"": "All batches"}
        batch_options.update({b.id: f"{b.filename} ({b.id})" for b in batches})
        locations = inventory_store.distinct_locations(st.session_state.company_id)

        f1, f2, f3, f4 = st.columns(4)
        with f1:
            triage_filter = st.selectbox(
                "Triage status", options=list(TRIAGE_LABELS.keys()),
                format_func=lambda k: TRIAGE_LABELS[k], key="inv_filter_triage",
            )
        with f2:
            batch_filter = st.selectbox(
                "Manifest batch", options=list(batch_options.keys()),
                format_func=lambda k: batch_options[k], key="inv_filter_batch",
            )
        with f3:
            location_filter = st.selectbox(
                "Location", options=[""] + locations,
                format_func=lambda k: k or "All", key="inv_filter_location",
            )
        with f4:
            grade_filter = st.selectbox(
                "Grade", options=["", "A", "B", "C", "D"],
                format_func=lambda k: k or "All", key="inv_filter_grade",
            )

        table_search = st.text_input(
            "Filter within results (substring match on SKU / name / brand / model)",
            placeholder="e.g. iPhone, Bosch, Test...",
            key="inv_filter_search",
        )

        # Any filter change resets to page 1 — otherwise a narrower filter
        # could land on a now-nonexistent page.
        filter_key = (triage_filter, batch_filter, location_filter, grade_filter, table_search)
        if st.session_state.get("inv_filter_key") != filter_key:
            st.session_state.inv_filter_key = filter_key
            st.session_state.inv_page = 1
        inv_page = st.session_state.get("inv_page", 1)

        products, total = inventory_store.list_products_paginated(
            company_id=st.session_state.company_id,
            triage_status=triage_filter or None,
            manifest_import_id=batch_filter or None,
            location=location_filter or None,
            grade=grade_filter or None,
            search=table_search or None,
            page=inv_page,
            page_size=INVENTORY_PAGE_SIZE,
        )
        total_pages = max(1, math.ceil(total / INVENTORY_PAGE_SIZE))
        inv_page = min(inv_page, total_pages)

        st.caption(f"{total} item(s) match — page {inv_page} of {total_pages}")

        if not products:
            st.info(
                "No items match these filters. Try widening them, or add "
                "one from 'New Item' / '📥 Import Manifest'."
            )
        else:
            table_rows = []
            for p in products:
                listing = marketplace_store.get_listing(p.id, baselinker_client.MARKETPLACE)
                table_rows.append({
                    "SKU": p.sku or "(none)",
                    "Title": p.name or p.manifest_item_description or p.model_number or p.id,
                    "Triage": TRIAGE_LABELS.get(p.triage_status, p.triage_status),
                    "Grade": p.grade or "—",
                    "Location": p.location or "—",
                    "Price": p.price,
                    "BaseLinker": listing.status if listing else marketplace_store.STATUS_NOT_LISTED,
                })
            table_df = pd.DataFrame(table_rows)

            event = st.dataframe(
                table_df,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key=f"inv_table_page{inv_page}",
            )

            nav1, nav2, nav3 = st.columns([1, 2, 1])
            with nav1:
                if st.button("⬅️ Previous", disabled=(inv_page <= 1), use_container_width=True):
                    st.session_state.inv_page = inv_page - 1
                    st.rerun()
            with nav2:
                st.write(f"Page {inv_page} of {total_pages}")
            with nav3:
                if st.button("Next ➡️", disabled=(inv_page >= total_pages), use_container_width=True):
                    st.session_state.inv_page = inv_page + 1
                    st.rerun()

            selected_rows = event.selection["rows"] if event is not None else []
            if selected_rows:
                # Track by product id, not row position: a change made in the
                # detail view below (e.g. triage status) can move the item
                # out of the current filter/page on rerun, which would leave
                # a row *index* pointing at a different product entirely (or
                # out of range). Re-fetching fresh by id sidesteps that.
                st.session_state.inv_selected_id = products[selected_rows[0]].id

        selected_id = st.session_state.get("inv_selected_id")
        if selected_id:
            selected_product = inventory_store.get_product(selected_id)
            if selected_product:
                st.divider()
                if st.button("✖ Close detail view"):
                    del st.session_state["inv_selected_id"]
                    st.rerun()
                _render_full_detail(selected_product)
            else:
                del st.session_state["inv_selected_id"]

# =============================================================== EXPORT ==
else:
    st.title("📤 Baselinker Export")
    all_products = inventory_store.list_products(st.session_state.company_id)
    exportable = [p for p in all_products if p.status != "draft"]

    if not exportable:
        st.info("No completed items to export yet (pending manifest drafts are excluded).")
    else:
        options = {f"{p.sku or p.id} — {p.name or p.model_number}": p.id for p in exportable}
        selected_labels = st.multiselect("Items to export", list(options.keys()), default=list(options.keys()))
        selected_ids = {options[l] for l in selected_labels}
        selected_products = [p for p in exportable if p.id in selected_ids]

        if selected_products:
            df = export.products_to_dataframe(selected_products)
            st.dataframe(df, use_container_width=True)

            col1, col2 = st.columns(2)
            with col1:
                st.download_button(
                    "⬇️ Download Excel (.xlsx)",
                    data=export.to_excel_bytes(selected_products),
                    file_name="baselinker_export.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            with col2:
                st.download_button(
                    "⬇️ Download CSV",
                    data=export.to_csv_bytes(selected_products),
                    file_name="baselinker_export.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

            st.caption(
                "Note: 'Image Links' currently contains local file paths. "
                "Baselinker's importer needs public image URLs — upload the "
                "photos to hosting (or Baselinker's own media manager) and "
                "substitute the URLs, or attach photos manually per listing "
                "after import."
            )

            st.divider()
            st.subheader("📤 Push directly to BaseLinker via API")
            st.caption(
                "An additional option alongside the CSV/Excel export above — "
                "not a replacement. Creates or updates each selected product "
                "directly in your BaseLinker catalog, including photos, with "
                "no CSV/column mapping step. Test with just a few products "
                "before relying on this as your main workflow."
            )

            if not baselinker_client.is_configured():
                st.info(
                    "Not set up yet — add BASELINKER_API_TOKEN, "
                    "BASELINKER_INVENTORY_ID and BASELINKER_CATEGORY_ID to "
                    "your .env file to enable this (see .env.example)."
                )
            else:
                with st.expander("Preview what will be sent"):
                    config = baselinker_client.get_config()
                    for p in selected_products:
                        listing = marketplace_store.get_listing(p.id, baselinker_client.MARKETPLACE)
                        action = "UPDATE existing" if (listing and listing.external_listing_id) else "CREATE new"
                        st.write(
                            f"**{p.sku}** ({action}) — {p.name or p.model_number or '(no name)'} — "
                            f"${p.price:.2f} — {len(p.image_paths)} photo(s) — "
                            f"EAN: {p.ean or p.manifest_barcode or '—'}"
                        )

                if st.button(
                    f"📤 Push {len(selected_products)} item(s) to BaseLinker",
                    type="primary",
                    use_container_width=True,
                ):
                    results_box = st.container()
                    progress = st.progress(0.0, text="Starting...")
                    for i, p in enumerate(selected_products):
                        progress.progress(
                            i / len(selected_products),
                            text=f"Pushing {p.sku} ({i + 1}/{len(selected_products)})...",
                        )
                        result = baselinker_client.push_product(p)
                        if result.success:
                            results_box.success(
                                f"✅ {p.sku}: {result.message} "
                                f"(BaseLinker product_id: {result.baselinker_product_id})"
                            )
                            if result.warnings:
                                results_box.warning(f"{p.sku} warnings: {result.warnings}")
                        else:
                            results_box.error(f"❌ {p.sku}: {result.message}")
                    progress.progress(1.0, text="Done.")
