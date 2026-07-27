"""ElectroGrader — mobile-first PWA for fast used-electronics grading,
inventory management and Baselinker-ready export.

Run with:  streamlit run app.py
"""
import os
import re
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import pandas as pd

from modules import (
    barcode_scanner,
    baselinker_client,
    description_gen,
    export,
    identifier_lookup,
    inventory_store,
    manifest_import,
    manifest_store,
    pricing,
    pwa,
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
        "photo_widget_seq": 0,       # bumped to reset st.camera_input
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
    st.session_state.photo_widget_seq += 1
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
            shot = st.camera_input("Scan label", key=f"barcode_cam_{st.session_state.photo_widget_seq}")

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
            "to cross-check against the manifest during grading. On phones, "
            "tap the switch-camera icon in the widget for the rear camera if "
            "it opens on the front camera."
        )

        shot = st.camera_input("Take a photo", key=f"photo_cam_{st.session_state.photo_widget_seq}")
        if shot is not None:
            st.session_state.captured_photos.append(shot.getvalue())
            st.session_state.photo_widget_seq += 1
            st.rerun()

        uploaded = st.file_uploader(
            "Or add photos from library",
            type=["jpg", "jpeg", "png"],
            accept_multiple_files=True,
            key=f"uploader_{st.session_state.photo_widget_seq}",
        )
        if uploaded:
            for f in uploaded:
                st.session_state.captured_photos.append(f.getvalue())
            st.session_state.photo_widget_seq += 1
            st.rerun()

        photos = st.session_state.captured_photos
        if photos:
            st.write(f"**{len(photos)} photo(s) captured**")
            cols = st.columns(4)
            for i, img_bytes in enumerate(photos):
                with cols[i % 4]:
                    st.image(img_bytes, use_container_width=True)
                    if st.button("🗑️", key=f"del_photo_{i}"):
                        st.session_state.captured_photos.pop(i)
                        st.rerun()

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

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ Back", use_container_width=True):
                st.session_state.wizard_step = 1
                st.rerun()
        with col3:
            if st.button("Next ➜", type="primary", use_container_width=True, disabled=not photos or sku_missing):
                product.sku = sku_input.strip()
                st.session_state.wizard_step = 3
                st.rerun()

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
                        category=product.category,
                        spec_summary=product.spec_summary,
                        box_contents=product.box_contents,
                        grade=product.grade,
                        defects=product.defects,
                        missing_components=product.missing_components,
                    )
                st.rerun()
            st.caption("Or write both descriptions manually below.")

        desc = st.session_state.descriptions
        product_desc_val = desc.product_description if desc else product.product_description
        condition_desc_val = desc.condition_description if desc else product.condition_description

        product_desc_in = st.text_area("Product Description (general overview)", value=product_desc_val, height=150)
        condition_desc_in = st.text_area("Condition & Scratches Details", value=condition_desc_val, height=150)

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ Back", use_container_width=True):
                st.session_state.wizard_step = 4
                st.rerun()
        with col3:
            if st.button("Next ➜", type="primary", use_container_width=True):
                product.price = float(price_in)
                product.price_reasoning = pe.reasoning if pe else ""
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
    all_products = inventory_store.list_products(st.session_state.company_id)
    drafts = [p for p in all_products if p.status == "draft"]
    processed = [p for p in all_products if p.status != "draft"]

    st.caption(
        f"{len(processed)} processed item(s)  •  {len(drafts)} pending manifest draft(s) "
        f"for company '{st.session_state.company_id}'"
    )

    if not processed and not drafts:
        st.info("No items yet — add one from 'New Item' or '📥 Import Manifest'.")

    show_drafts = st.checkbox("Show pending manifest drafts too", value=False)
    products = processed + drafts if show_drafts else processed

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

    def _render_compact(p: Product):
        badge = "📥 DRAFT" if p.status == "draft" else f"{p.grade or '?'} ({p.grade_confidence}%)"
        title = p.name or p.manifest_item_description or p.model_number or p.asin or p.id
        with st.expander(f"{badge}  •  {title}  —  ${p.price:.2f}"):
            _render_images(p)
            st.write(f"**SKU:** {p.sku or '(not yet assigned)'}")
            st.write(f"**Brand / Model:** {p.brand or '—'} / {p.model or '—'}")
            st.write(f"**Category / Condition:** {p.category or '—'} / {p.condition_type or '—'}")
            st.write(f"**ASIN / EAN:** {p.asin or '—'} / {p.ean or p.manifest_barcode or p.scanned_barcode or '—'}")
            if p.status != "draft":
                st.write(f"**Product match:** {p.product_match or 'UNKNOWN'} ({p.match_confidence}%)")
                if p.match_notes:
                    st.caption(p.match_notes)
                st.write(f"**Location:** {p.location or '—'}  •  **Functional test:** {p.functional_test_result or '—'}")
                st.write(f"**Product Description:** {p.product_description}")
                st.write(f"**Condition & Scratches Details:** {p.condition_description}")
            else:
                st.write(f"**Manifest item description:** {p.manifest_item_description}")
                st.write(f"**Qty:** {p.manifest_qty}  •  **Weight:** {p.manifest_weight_kg} kg")
            _delete_button(p)

    def _render_full_detail(p: Product, tier_label: str):
        badge = "📥 DRAFT" if p.status == "draft" else f"{p.grade or '?'} ({p.grade_confidence}%)"
        title = p.name or p.manifest_item_description or p.model_number or p.asin or p.id
        st.markdown(f"### 🎯 Exact {tier_label} match — {badge} • {title}")
        _render_images(p)

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
            st.write(f"**Price:** ${p.price:.2f}")
            st.write(f"**Location:** {p.location or '—'}")
            st.write(f"**Functional test:** {p.functional_test_result or '—'}")

        if p.status != "draft":
            st.write(f"**Product match:** {p.product_match or 'UNKNOWN'} ({p.match_confidence}%)")
            if p.match_notes:
                st.caption(p.match_notes)
            st.write("**Spec summary:**", p.spec_summary or "—")
            st.write("**Box contents:**", ", ".join(p.box_contents) or "—")
            st.write("**Missing components:**", ", ".join(p.missing_components) or "—")
            st.write("**Defects:**", ", ".join(p.defects) or "—")
            st.write("**Functional test checklist:**", ", ".join(p.functional_checklist) or "—")
            st.write("**Product Description:**", p.product_description or "—")
            st.write("**Condition & Scratches Details:**", p.condition_description or "—")
            box_dims = ", ".join(
                f"{v:g} cm" for v in (p.box_length_cm, p.box_width_cm, p.box_height_cm) if v
            )
            st.write("**Box dimensions:**", box_dims or "—")
        else:
            st.write("**Manifest item description:**", p.manifest_item_description or "—")
            st.write(f"**Manifest Target #:** {p.manifest_target_no or '—'}  •  **Subcategory:** {p.manifest_subcategory or '—'}")
            st.write(f"**Qty:** {p.manifest_qty}  •  **Weight:** {p.manifest_weight_kg} kg")

        _delete_button(p)
        st.divider()

    search_query = st.text_input(
        "🔍 Search by EAN, ASIN, Product Name, Brand, or Model",
        placeholder="e.g. 0194252057338, B08ASIN123, iPhone 12, Apple, A2172",
        help="Priority: exact EAN match, then exact ASIN match, then model number, "
        "then brand/product name. Works for manifest-imported and manually-added "
        "items alike.",
    )

    if search_query.strip():
        results = inventory_store.search_products(search_query.strip(), st.session_state.company_id)
        if not show_drafts:
            results = [(p, tier) for p, tier in results if p.status != "draft"]
        st.caption(f"{len(results)} match(es) for '{search_query.strip()}'")

        tier_labels = {
            inventory_store.MATCH_TIER_EAN: "EAN",
            inventory_store.MATCH_TIER_ASIN: "ASIN",
            inventory_store.MATCH_TIER_MODEL: "model",
            inventory_store.MATCH_TIER_BRAND_NAME: "brand/name",
        }
        for p, tier in results:
            if tier in (inventory_store.MATCH_TIER_EAN, inventory_store.MATCH_TIER_ASIN):
                _render_full_detail(p, tier_labels[tier])
            else:
                _render_compact(p)
    else:
        for p in products:
            _render_compact(p)

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
                        action = "UPDATE existing" if p.baselinker_product_id else "CREATE new"
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
                            inventory_store.save_product(p)
                            results_box.success(
                                f"✅ {p.sku}: {result.message} "
                                f"(BaseLinker product_id: {result.baselinker_product_id})"
                            )
                            if result.warnings:
                                results_box.warning(f"{p.sku} warnings: {result.warnings}")
                        else:
                            results_box.error(f"❌ {p.sku}: {result.message}")
                    progress.progress(1.0, text="Done.")
