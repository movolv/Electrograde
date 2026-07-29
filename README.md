# ElectroGrader

A mobile-first Streamlit PWA for grading used electronics, building inventory,
and exporting Baselinker-ready listings — optimized for iPhone.

Two ways to start an item:
- **From an Amazon liquidation manifest** — bulk-import Target #/ASIN/EAN/
  description/Qty/Weight, then process each item individually.
- **From scratch** — scan a barcode/model label or type it in manually.

Either way, the same pipeline follows: **capture photos → auto-fetch specs →
AI vision grading + manifest-vs-photo verification → price + AI copywriting
→ manual-only details → save → export**.

## Architecture

```
electro-grader-pwa/
├── app.py                     # Main Streamlit app (wizard UI + nav)
├── modules/
│   ├── models.py               # Product dataclass (see "Data model" below)
│   ├── inventory_store.py      # SQLite persistence (data/inventory.db), company_id-scoped
│   ├── excel_autosave.py       # Auto-mirrors full inventory to data/inventory.xlsx
│   ├── manifest_import.py      # Manifest file parsing + column-mapping + draft creation
│   ├── manifest_store.py       # Manifest "batch" (shipment) records, own SQLite table
│   ├── barcode_scanner.py      # pyzbar barcode/QR decoding
│   ├── identifier_lookup.py    # Automatic EAN/ASIN discovery (never invents, never overwrites)
│   ├── spec_lookup.py          # ddgs web search + scrape + AI structuring
│   ├── ai_client.py            # Anthropic Claude wrapper (text + vision)
│   ├── vision_grading.py       # Claude vision -> grade/defects/checklist + identity verification
│   ├── pricing.py              # Web price search + grade multiplier
│   ├── description_gen.py      # AI listing copy (2 separate English columns)
│   ├── export.py                # Baselinker Excel/CSV export
│   ├── baselinker_client.py     # Optional: direct BaseLinker API push (create/update + photos)
│   └── pwa.py                    # Injects manifest/meta tags into page <head>
├── static/
│   ├── manifest.json            # PWA manifest (the *app's* manifest — not to be confused
│   │                             # with an Amazon liquidation manifest file)
│   ├── sw.js                    # Minimal service worker (app-shell cache)
│   └── icons/icon-192.png, icon-512.png
├── scripts/generate_icons.py    # Regenerate placeholder icons
├── .streamlit/config.toml       # enableStaticServing, dark theme
├── data/                        # SQLite DB + saved photos (gitignored)
├── requirements.txt
└── .env.example
```

Everything is modular by design — each concern (manifest parsing, barcode
decoding, spec lookup, grading, pricing, copywriting, export) lives in its
own file so you can swap implementations (e.g. a different AI provider, or a
paid pricing API) without touching the rest.

## Data model

`modules/models.py`'s `Product` dataclass is grouped by *who's allowed to
write what*, and is designed to support three things without further
restructuring: Baselinker export, a future API integration (it's a flat,
JSON-serializable record), and multi-company use:

| Group | Fields | Who writes it |
|---|---|---|
| **Manifest** (unverified claim) | `manifest_import_id`, `manifest_target_no`, `manifest_subcategory`, `asin`, `manifest_barcode`, `manifest_item_description`, `manifest_qty`, `manifest_weight_kg` | Import Manifest step only, once |
| **Identifiers** | `scanned_barcode` (decoded from a photo), `model_number` (typed, from-scratch flow) | Photo capture / from-scratch step |
| **AI-filled** (editable after) | `brand`, `model`, `name`, `category`, `condition_type`, `grade`, `grade_confidence`, `grade_reasoning`, `price`, `price_reasoning`, `product_description`, `condition_description`, `spec_summary`, `box_contents`, `missing_components`, `defects`, `functional_checklist` | Spec lookup + vision grading + pricing + copywriting; human can edit every field afterward |
| **Verification** | `product_match` (YES/NO), `match_confidence` (0-100), `match_notes` | Vision grading step — compares the claimed identity against what photos actually show |
| **Manual-only** (AI never touches these) | `sku`, `location`, `functional_test_result`, `box_length_cm`, `box_width_cm`, `box_height_cm` | Human only |
| **Bookkeeping** | `id`, `company_id`, `status` (`draft`/`in_progress`/`completed`), `image_paths`, `created_at` | System |

## 1. Prerequisites

- Python 3.10–3.12 recommended.
- An **Anthropic API key** (used for spec structuring, vision grading, and
  description generation) — https://console.anthropic.com/
- `pyzbar` needs the native **zbar** library to actually decode barcodes:
  - Windows: the `pyzbar` PyPI wheel bundles the required DLLs — normally
    nothing extra to install.
  - macOS: `brew install zbar`
  - Linux (Debian/Ubuntu): `sudo apt-get install libzbar0`
  - If zbar isn't available, the app **still works** — it just falls back to
    manual model-number entry (barcode auto-decode is a convenience, not a
    hard requirement).
- **"Clean background"** (Step 2) is backed by `modules/image_pipeline/`
  (see its own `README.md` for the full design) — needs `rembg` +
  `onnxruntime` + `opencv-python-headless` + `scipy` (all in
  `requirements.txt`) and an internet connection the *first* time it's
  used — it downloads a ~180MB segmentation model to `~/.u2net/` on first
  run, then works offline/instantly after that.

## 2. Setup

```bash
cd electro-grader-pwa
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# then edit .env and paste your ANTHROPIC_API_KEY
```

## 3. Run locally

```bash
streamlit run app.py
```

Open the printed local URL (usually `http://localhost:8501`) in a desktop
browser to test the wizard end-to-end (camera capture will use your webcam).

## 4. Set up for iPhone use (camera + "Add to Home Screen")

Two things matter for iPhone:

1. **HTTPS is required** for Safari to allow camera access (`st.camera_input`)
   on any host other than `localhost`.
2. **A stable, repeatedly-reachable URL** is needed for "Add to Home Screen"
   to make sense (otherwise you'd re-add it every time the tunnel URL changes).

Pick one:

### Option A — Quick testing: Cloudflare Tunnel or ngrok (free, temporary URL)

```bash
# after `streamlit run app.py` is already running on port 8501
cloudflared tunnel --url http://localhost:8501
# or:
ngrok http 8501
```

Open the resulting `https://...` URL on your iPhone (same or different
network — both tunnels work over the internet, not just LAN).

### Option B — Permanent: Streamlit Community Cloud (free, recommended)

1. Push this project to a GitHub repo (remember `.env` is gitignored — set
   `ANTHROPIC_API_KEY` as a "Secret" in the Streamlit Cloud app settings
   instead).
2. Deploy at https://share.streamlit.io — you get a permanent
   `https://your-app.streamlit.app` URL.
3. Open that URL on your iPhone.

### Add to Home Screen (do this once you have an https:// URL open in Safari)

1. Open the app URL in **Safari** on iPhone (must be Safari, not Chrome, for
   this to work on iOS).
2. Tap the **Share** icon (square with an arrow) in the bottom toolbar.
3. Scroll down and tap **"Add to Home Screen."**
4. Tap **Add**. The ElectroGrader icon now appears on your Home Screen and
   launches full-screen, without Safari's address bar/toolbar.

Camera permission: the first time you use the camera from the installed
app, iOS will prompt for camera access — allow it.

**Step 2 (product photos) uses `st.file_uploader`, not `st.camera_input`:**
tapping it opens the phone's own native camera app (via the OS's picker
sheet) rather than an in-page live video widget. This was a deliberate
switch away from `st.camera_input` — full sensor resolution matters here,
since both the AI grading (`vision_grading.py`, spotting small defects) and
the "Clean background" cutout tool (`modules/image_pipeline/`) need
enough source detail to work with; `st.camera_input` instead grabs a frame
from a low-res in-browser video stream (measured as low as ~400×500px on a
modern phone, vs. several thousand px from the native camera app), which
reads as visibly blurry once cropped/enlarged for a cutout. The trade-off:
the live preview happens in the native camera app's own screen rather than
embedded in the page — you still see exactly what you're photographing,
it's just not inline. In exchange, the rear camera opens automatically
every time (no manual switch-camera tap needed, unlike `st.camera_input`,
which we found always restarts on the front camera on iPhone/Android with
no Python-level way to change that — confirmed by inspecting Streamlit's
installed frontend bundle; a `components.html`-injected `getUserMedia`
override was also tried and worked in isolated testing but didn't render
at all on iOS Safari in practice, so that approach was abandoned).

**Step 1 (barcode/label scan) still uses `st.camera_input`** — its live
in-page preview is worth keeping there, since lining up a barcode in frame
benefits more from instant visual feedback than from resolution (barcode
decoding needs the barcode sharp and legible, not the whole photo
high-res). If you'd rather have that step also open the native camera app,
swap `st.camera_input` for
`st.file_uploader(label, type=["jpg","jpeg","png"])` at its call site in
`app.py`.

## 5. Using the app

**Company field (sidebar)** — a plain text box, default `"default"`. Every
item and manifest batch is tagged with whatever's typed here, and every list/
search/export is scoped to it. This is the seam for future multi-company use
— today everyone just leaves it as `"default"`.

### Import Manifest tab

1. Upload a `.xlsx` or `.csv` liquidation manifest.
2. Only 7 fields are ever read: **Target #, Subcategory, ASIN, EAN/Barcode,
   Item description, Qty, Weight (kg)**. Everything else in the file is
   ignored — brand, model, condition, price etc. are always determined later
   by AI + photos, never taken from the manifest.
3. **Confirm the column mapping** — each of the 7 fields gets a dropdown of
   the file's actual columns, pre-selected via best-guess header matching
   (handles variants like "Target Nr.", "EAN / Barcode", "Item Description").
   Fix any that guessed wrong before importing.
4. Click **"Import as new manifest batch"** — this creates one **manifest
   batch** (a "shipment") with its own ID, and one draft `Product` per row,
   all linked to that batch via `manifest_import_id`. Past batches are listed
   at the bottom of the page with a running count of how many of their items
   are still pending.
5. Nothing here touches your existing scan/manual-entry flow — it's purely
   additive. Once imported, items are also searchable in the Inventory tab
   by EAN/barcode, ASIN, name, or model (see below).

### New Item tab (6-step wizard)

1. *Identify* — either **pick a pending manifest item** from the dropdown
   (shows its ASIN/EAN/description/qty/weight), or **start from scratch**
   by scanning a barcode/model label (auto-decoded) or typing it manually.
2. *Photos* — capture front/back/sides/defect close-ups. Add as many as you
   need; delete any with the 🗑️ button. The first photo (the one used as
   the main listing thumbnail) gets a **"🧼 Clean background"** button,
   backed by `modules/image_pipeline/` — a local, no-per-photo-API-cost
   pipeline (`rembg` + classical OpenCV/PIL processing) that detects the
   product, straightens minor in-plane tilt, crops to it, lightly softens
   small glare hot-spots, freshens lighting/contrast, and recomposites it
   centered on a pure white e-commerce background with a soft drop
   shadow. It never enlarges past what the source resolution supports
   (upscaling a low-res crop just introduces blur, not real detail), and
   runs an automatic quality check (sharpness/exposure/centering/
   framing) — a photo that fails the check is rejected with a specific
   reason instead of silently producing a bad result; see
   `modules/image_pipeline/README.md` for the full pipeline design,
   scope, and known limitations (it does **not** truly remove
   reflections — see that doc before expecting it to). If a barcode/
   label ends up in any photo, it's later decoded and used as a hard
   cross-check against the manifest's claimed EAN. **SKU is required
   here** before continuing — entered manually, never touched by AI, and
   carried through everything from this point on (photo folder name, AI
   results, Excel export).
3. *Specs* — "Fetch specs from the web" using whichever identifiers exist
   (EAN, ASIN, manifest description, or typed model number) to fill in
   brand/model/name/category/spec summary/box contents; all editable.
4. *Grading* — "Analyze photos" for an AI-assigned grade (A–D) with a
   confidence %, New/Used condition, defects, missing components, and a
   functional test checklist — **plus manifest-vs-photo verification**:
   `Product match` (YES/NO), `Match confidence %`, and notes on what was
   compared (brand/model/product type/any visible barcode). If a barcode
   decoded from a photo doesn't match the manifest's EAN, the match is
   forced to NO regardless of what the AI itself judged. A mismatch or low
   confidence (<60%) shows a warning banner. Everything here is editable.
5. *Price & Copy* — "Estimate market price" and "Generate English
   descriptions" for the two required description columns; everything is
   editable.
6. *Manual-only details & Save* — location/shelf position, functional test
   result (Working/Not Working/Not Tested), box dimensions (for courier
   calculations), and a final price correction if needed — none of these
   are ever touched by AI. Saving marks the item `completed`.

**Inventory tab** — browse, search, review, and delete items.
- A checkbox reveals still-pending manifest drafts alongside completed items.
- **Search** by EAN/barcode, ASIN, product name, or model — this is
  additive to (doesn't replace) the manual scan/search flow in New Item.

> **Auto-saved Excel:** every time an item is saved or deleted, the full
> inventory (all fields, not just the Baselinker subset) is also rewritten
> to `data/inventory.xlsx` automatically — no button needed. SQLite
> (`data/inventory.db`) remains the source of truth; the `.xlsx` is a
> convenience mirror kept in sync with it.

**Export tab** — select completed items (pending drafts are excluded) and
download a Baselinker-ready **Excel or CSV** file with columns: `SKU, Name,
Brand, Model, Category, Condition, EAN/Barcode, EAN Status, Manifest
Target #, Grade, AI Confidence %, Product Match, Match Confidence %, Price,
Location, Functional Test Result, Box Dimensions (L x W x H cm), Image
Links, Product Description, Condition & Scratches Details, Functional Test
Checklist, Missing Components`. The two description columns are always
generated strictly in English regardless of source-page language. (ASIN is
intentionally excluded — see `modules/identifier_lookup.py`'s
never-auto-pick rule; it stays in the internal database only.)

> **Image hosting note (CSV/Excel path only):** Baselinker's CSV importer
> expects public image URLs. This app stores captured photos locally and
> puts their file paths in "Image Links". Before importing into Baselinker,
> either (a) upload those photo files somewhere public (S3, Baselinker's
> own media manager, etc.) and swap in the URLs, or (b) import the row data
> first and attach photos manually per listing afterward — or use the API
> push option below, which sends photos directly, no public URL needed.

### Optional: direct BaseLinker API push (no CSV, no column mapping)

Alongside the CSV/Excel download buttons, the Export tab also offers
**"📤 Push directly to BaseLinker via API"** — an *additional* option, not a
replacement. It creates or updates each selected product straight in your
BaseLinker/Base.com catalog via their JSON API (`modules/baselinker_client.py`),
including photos (sent as inline base64 — no public URL hosting needed at
all), with zero manual column mapping since the code maps each field once,
directly, by name.

**Setup** (all in `.env`, see `.env.example`):
1. Generate a token in the BaseLinker/Base.com panel: *Account & other → My
   account → API*.
2. Find your `inventory_id` via the panel or the `getInventories` API method.
3. Create (or pick) a category and note its `category_id` — required per
   product; there's no "uncategorized" fallback.
4. Set `BASELINKER_API_TOKEN`, `BASELINKER_INVENTORY_ID`,
   `BASELINKER_CATEGORY_ID`. Optionally set `BASELINKER_PRICE_GROUP_ID` /
   `BASELINKER_WAREHOUSE_ID` if you want price/stock sent automatically, and
   `BASELINKER_TAX_RATE` (default `23`).

Leave all of these unset and the button stays disabled with a setup hint —
CSV/Excel export keeps working exactly as before either way.

**How it stays safe:**
- **Idempotent, not duplicate-prone**: the first successful push stores
  BaseLinker's own `product_id` on the `Product` record
  (`baselinker_product_id` / `baselinker_synced_at` in `modules/models.py`);
  every push after that **updates** the same BaseLinker product instead of
  creating a new one. As an extra safety net, a not-yet-synced product is
  first checked against BaseLinker by SKU (`filter_sku`) before creating
  anything, in case the local database was ever reset.
- **Preview before push**: an expander shows exactly what will be sent
  (name, SKU, EAN, price, photo count, create-vs-update) for every selected
  item before the button is pressed — nothing goes out unreviewed.
- **Per-item results, never silent**: each push reports success (with the
  resulting BaseLinker `product_id`), warnings, or errors individually —
  a failure on one item doesn't hide the others' results.
- **Test small first**: the item multiselect above already lets you select
  just one or two products — do that before pushing a whole batch, and
  check the BaseLinker panel afterward to confirm they landed as expected.

## 6. Customization points

- **Swap the AI provider**: edit `modules/ai_client.py` only — every other
  module calls through it.
- **Swap the search backend**: `modules/spec_lookup.py`, `modules/pricing.py`,
  and `modules/identifier_lookup.py` use `ddgs` (free, no API key — this is
  the actively-maintained successor to the now-defunct `duckduckgo-search`
  package; if search results ever silently come back empty, check whether
  `ddgs` itself has since been deprecated the same way and needs updating
  again). For more reliable results, swap in SerpAPI/Google Custom Search/a
  marketplace API.
- **Change grading scale or multipliers**: `modules/vision_grading.py`
  (`GRADE_SCALE`) and `modules/pricing.py` (`GRADE_MULTIPLIER`).
- **Rebrand icons**: replace `static/icons/icon-192.png` / `icon-512.png`
  with your own 192×192 / 512×512 PNGs (same filenames), or edit and rerun
  `python scripts/generate_icons.py`.

## 7. Known limitations

- Web scraping for specs/pricing is best-effort; results vary by product and
  site availability — always shown as editable, never auto-committed blind.
- The service worker only caches static app-shell assets (icon/manifest),
  not the live Streamlit UI — an internet connection is required to use the
  app (it launches like a native app, it doesn't work offline).
- `pyzbar` decode quality depends on photo focus/lighting; manual entry is
  always available as a fallback.
- The manifest-vs-photo match-confidence warning threshold (60%) is a fixed
  constant (`MATCH_CONFIDENCE_WARNING_THRESHOLD` in `app.py`) — adjust it
  there if you want stricter/looser flagging.
- `company_id` is a free-text scoping field, not real authentication — it
  prevents cross-company data from showing up in the same list/search/export,
  but anyone using the app can type any company name. A real multi-tenant
  deployment would add login + server-side company assignment on top of this.
- One manifest row currently becomes exactly one draft `Product`, regardless
  of its `Qty` value — `manifest_qty` is stored for reference but doesn't
  auto-multiply into N separate items.
