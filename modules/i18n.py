"""UI-string translation for the ElectroGrader interface — English/Latvian
only, and deliberately scoped to the app's own chrome (labels, buttons,
captions, static messages). Never used for actual data (product names,
customer names, SKUs, BaseLinker parameter values) or text produced by an
external system/AI call — those stay whatever language they already are.

Framework-agnostic (no `streamlit` import, same discipline as
modules/auth.py) — app.py owns st.session_state.language and wraps t() in
a short T(key, **kwargs) closure for its ~500 call sites; this module is
just the dict + lookup, so it stays trivially unit-testable on its own.

Standing rule for this project: every new UI string added to app.py must
get both an "en" and "lv" entry here and be rendered via T("key"), never a
raw literal passed straight to an st.* call.
"""

LANGUAGES = {"en": "English", "lv": "Latviešu"}
DEFAULT_LANGUAGE = "en"

# Keys are dot-namespaced by page/section. "common.*" holds strings reused
# verbatim across multiple pages (Save/Cancel/Brand/Model/etc.) so those
# aren't duplicated dozens of times below.
TRANSLATIONS: dict = {
    # ---- common (reused across pages) ----
    "common.save": {"en": "Save", "lv": "Saglabāt"},
    "common.cancel": {"en": "Cancel", "lv": "Atcelt"},
    "common.delete": {"en": "🗑️ Delete", "lv": "🗑️ Dzēst"},
    "common.back": {"en": "⬅ Back", "lv": "⬅ Atpakaļ"},
    "common.back_to_list": {"en": "← Back to list", "lv": "← Atpakaļ uz sarakstu"},
    "common.next": {"en": "Next ➜", "lv": "Tālāk ➜"},
    "common.close": {"en": "Close", "lv": "Aizvērt"},
    "common.brand": {"en": "Brand", "lv": "Zīmols"},
    "common.model": {"en": "Model", "lv": "Modelis"},
    "common.category": {"en": "Category", "lv": "Kategorija"},
    "common.power": {"en": "Power", "lv": "Jauda"},
    "common.color": {"en": "Color", "lv": "Krāsa"},
    "common.price": {"en": "Price", "lv": "Cena"},
    "common.quantity": {"en": "Quantity", "lv": "Daudzums"},
    "common.location": {"en": "Location", "lv": "Atrašanās vieta"},
    "common.status": {"en": "Status", "lv": "Statuss"},
    "common.sku": {"en": "SKU", "lv": "SKU"},
    "common.barcode": {"en": "Barcode", "lv": "Svītrkods"},
    "common.product_name": {"en": "Product Name", "lv": "Produkta nosaukums"},
    "common.product_condition": {"en": "Product Condition", "lv": "Preces stāvoklis"},
    "common.description": {"en": "Description", "lv": "Apraksts"},
    "common.email": {"en": "Email", "lv": "E-pasts"},
    "common.password": {"en": "Password", "lv": "Parole"},
    "common.name": {"en": "Name", "lv": "Vārds"},
    "common.role": {"en": "Role", "lv": "Loma"},
    "common.log_out": {"en": "Log out", "lv": "Izrakstīties"},
    "common.refresh": {"en": "🔄 Refresh", "lv": "🔄 Atsvaidzināt"},
    "common.search": {"en": "Search", "lv": "Meklēt"},
    "common.all": {"en": "All", "lv": "Visi"},
    "common.none_dash": {"en": "—", "lv": "—"},
    "common.admins_only": {"en": "Admins only.", "lv": "Tikai administratoriem."},
    "common.admins_reviewers_only": {"en": "Only Admins and Reviewers can do this.", "lv": "To drīkst darīt tikai administratori un pārbaudītāji."},

    # ---- login / register (pre-auth screen) ----
    "login.mode_login": {"en": "Log in", "lv": "Ieiet"},
    "login.mode_register": {"en": "Register a new company", "lv": "Reģistrēt jaunu uzņēmumu"},
    "login.pending_approval": {
        "en": "Your company registration is pending approval. You'll be able to log in once it's approved.",
        "lv": "Tava uzņēmuma reģistrācija gaida apstiprinājumu. Varēsi ieiet, tiklīdz tā būs apstiprināta.",
    },
    "login.invalid_credentials": {"en": "Invalid email or password.", "lv": "Nepareizs e-pasts vai parole."},
    "login.locked_out": {
        "en": "Too many failed attempts. Try again in {minutes} minute(s).",
        "lv": "Pārāk daudz neveiksmīgu mēģinājumu. Mēģini vēlreiz pēc {minutes} minūtes(-ēm).",
    },
    "login.register_caption": {
        "en": "A platform Super Admin reviews and approves new companies before you can log in.",
        "lv": "Platformas Super Admin izskata un apstiprina jaunus uzņēmumus, pirms vari ieiet.",
    },
    "login.company_name": {"en": "Company name", "lv": "Uzņēmuma nosaukums"},
    "login.plan": {"en": "Plan", "lv": "Plāns"},
    "login.plan_trial": {"en": "Trial", "lv": "Izmēģinājuma"},
    "login.plan_standard": {"en": "Standard", "lv": "Standarta"},
    "login.plan_help": {
        "en": "No plan-specific limits yet — this just records your choice for later.",
        "lv": "Pagaidām plānam nav ierobežojumu — tas tikai saglabā tavu izvēli vēlākai izmantošanai.",
    },
    "login.your_name": {"en": "Your name", "lv": "Tavs vārds"},
    "login.your_email": {"en": "Your email", "lv": "Tavs e-pasts"},
    "login.your_password": {"en": "Your password", "lv": "Tava parole"},
    "login.register_button": {"en": "Register", "lv": "Reģistrēties"},
    "login.all_fields_required": {"en": "All fields are required.", "lv": "Visi lauki ir obligāti."},
    "login.company_exists": {
        "en": "A company named '{name}' already exists. Please choose a different name.",
        "lv": "Uzņēmums ar nosaukumu '{name}' jau eksistē. Lūdzu, izvēlies citu nosaukumu.",
    },
    "login.registration_submitted": {
        "en": "Registration submitted — pending approval. You'll be able to log in once approved.",
        "lv": "Reģistrācija nosūtīta — gaida apstiprinājumu. Varēsi ieiet, tiklīdz tā būs apstiprināta.",
    },

    # ---- New Item wizard ----
    "new_item.step1": {"en": "1. Identify", "lv": "1. Identificēt"},
    "new_item.step2": {"en": "2. Photos", "lv": "2. Fotogrāfijas"},
    "new_item.step3": {"en": "3. Specs", "lv": "3. Specifikācijas"},
    "new_item.step4": {"en": "4. Grading", "lv": "4. Vērtēšana"},
    "new_item.step5": {"en": "5. Price & Copy", "lv": "5. Cena un apraksts"},
    "new_item.step6": {"en": "6. Save", "lv": "6. Saglabāt"},
    "new_item.start_item": {"en": "Start this item", "lv": "Sāc šo produktu"},
    "new_item.how_to_start": {"en": "How do you want to start?", "lv": "Kā vēlies sākt?"},
    "new_item.from_manifest": {"en": "📥 From a pending manifest item", "lv": "📥 No gaidoša manifesta ieraksta"},
    "new_item.from_scratch": {"en": "🆕 From scratch (scan/manual)", "lv": "🆕 No jauna (skenēt/manuāli)"},
    "new_item.no_pending_manifest_items": {
        "en": "No pending manifest items for this company. Use '📥 Import Manifest' "
              "to add some, or start from scratch instead.",
        "lv": "Šim uzņēmumam nav gaidošu manifesta ierakstu. Izmanto '📥 Importēt manifestu', "
              "lai tos pievienotu, vai sāc no jauna.",
    },
    "new_item.search_pending_items": {
        "en": "🔍 Search pending items (SKU, Target #, ASIN, or description)",
        "lv": "🔍 Meklēt gaidošos ierakstus (SKU, mērķa Nr., ASIN vai apraksts)",
    },
    "new_item.search_pending_items_placeholder": {
        "en": "e.g. 2005, T-9001, B08N5WRWNW, hand blender",
        "lv": "piem., 2005, T-9001, B08N5WRWNW, rokas blenderis",
    },
    "new_item.match_count": {"en": "{count} match(es)", "lv": "{count} atbilstība(-as)"},
    "new_item.no_pending_items_match": {"en": "No pending items match that search.", "lv": "Nav ierakstu, kas atbilst šim meklējumam."},
    "new_item.pick_pending_item": {"en": "Pick a pending manifest item", "lv": "Izvēlies gaidošu manifesta ierakstu"},
    "new_item.item_description": {"en": "Item description", "lv": "Preces apraksts"},
    "new_item.ean_barcode": {"en": "EAN/Barcode", "lv": "EAN/Svītrkods"},
    "new_item.qty": {"en": "Qty", "lv": "Daudzums"},
    "new_item.weight": {"en": "Weight", "lv": "Svars"},
    "new_item.manifest_unverified_reminder": {
        "en": "Reminder: this manifest data is an unverified claim — later steps "
              "will independently check it against the actual photos.",
        "lv": "Atgādinājums: šie manifesta dati ir nepārbaudīts apgalvojums — vēlākie "
              "soļi to neatkarīgi pārbaudīs pret faktiskajām fotogrāfijām.",
    },
    "new_item.use_this_item": {"en": "Use this item ➜", "lv": "Izmantot šo preci ➜"},
    "new_item.scan_camera_hint": {
        "en": "Point the camera at the barcode or model-number sticker. "
              "On phones, tap the switch-camera icon in the widget to use "
              "the rear camera if it opens on the front camera.",
        "lv": "Vērs kameru pret svītrkodu vai modeļa numura uzlīmi. "
              "Telefonos pieskaries kameras maiņas ikonai, lai izmantotu "
              "aizmugurējo kameru, ja atveras priekšējā.",
    },
    "new_item.scan_label": {"en": "Scan label", "lv": "Skenēt uzlīmi"},
    "new_item.decoded": {"en": "Decoded: {codes}", "lv": "Atkodēts: {codes}"},
    "new_item.zbar_missing": {
        "en": "Barcode auto-decode isn't available on this install (zbar missing) — enter the model number manually below.",
        "lv": "Svītrkoda automātiskā atkodēšana šajā instalācijā nav pieejama (trūkst zbar) — ievadi modeļa numuru manuāli zemāk.",
    },
    "new_item.no_barcode_detected": {
        "en": "No barcode detected in frame — try again or enter manually below.",
        "lv": "Kadrā nav atrasts svītrkods — mēģini vēlreiz vai ievadi manuāli zemāk.",
    },
    "new_item.model_number_barcode": {"en": "Model number / barcode (edit if needed)", "lv": "Modeļa numurs / svītrkods (rediģē, ja nepieciešams)"},
    "new_item.capture_photos": {"en": "Capture photos", "lv": "Uzņem fotogrāfijas"},
    "new_item.capture_photos_caption": {
        "en": "Take front, back, sides, and close-ups of any scratches/defects. "
              "If a barcode/label is visible in any photo, it will also be used "
              "to cross-check against the manifest during grading.",
        "lv": "Uzņem priekšpusi, aizmuguri, sānus un tuvplānus jebkuriem skrāpējumiem/defektiem. "
              "Ja kādā fotogrāfijā redzams svītrkods/uzlīme, tas tiks izmantots arī, "
              "lai vērtēšanas laikā salīdzinātu ar manifestu.",
    },
    "new_item.take_photo_or_choose": {"en": "Take a photo or choose from library", "lv": "Uzņem fotogrāfiju vai izvēlies no bibliotēkas"},

    "new_item.specs_heading": {"en": "Specifications & box contents", "lv": "Specifikācijas un komplektācija"},
    "new_item.specs_caption": {"en": "Automated web lookup — review and edit before continuing.", "lv": "Automātiska meklēšana internetā — pārbaudi un rediģē pirms turpināt."},
    "new_item.fetch_specs": {"en": "🔎 Fetch specs from the web", "lv": "🔎 Iegūt specifikācijas no interneta"},
    "new_item.searching": {"en": "Searching...", "lv": "Meklē..."},
    "new_item.skip_manual_caption": {"en": "Or skip and fill the fields in manually below.", "lv": "Vai izlaist un aizpildīt laukus manuāli zemāk."},
    "new_item.category_placeholder": {"en": "e.g. Smartphone, Laptop, Headphones", "lv": "piem., viedtālrunis, klēpjdators, austiņas"},
    "new_item.spec_summary": {"en": "Spec summary", "lv": "Specifikāciju kopsavilkums"},
    "new_item.box_contents": {"en": "Standard box contents (one per line)", "lv": "Standarta komplektācija (katrs vienumā jaunā rindā)"},
    "new_item.ean_asin_id": {"en": "EAN / ASIN identification", "lv": "EAN / ASIN identifikācija"},
    "new_item.ean_asin_caption": {
        "en": "Filled automatically (never overwrites an existing value, never invents "
              "one) as soon as specs are fetched above. Correct manually if needed.",
        "lv": "Aizpildās automātiski (nekad nepārraksta esošu vērtību, neizdomā "
              "jaunu), tiklīdz specifikācijas iegūtas augstāk. Labo manuāli, ja nepieciešams.",
    },
    "new_item.ean_gtin": {"en": "EAN / GTIN", "lv": "EAN / GTIN"},
    "new_item.not_yet_checked": {"en": "Not yet checked", "lv": "Vēl nav pārbaudīts"},
    "new_item.other_asins_found": {
        "en": "Other possible ASINs found — please verify: {asins}",
        "lv": "Atrasti citi iespējamie ASIN — lūdzu, pārbaudi: {asins}",
    },
    "new_item.sources_used": {"en": "Sources used", "lv": "Izmantotie avoti"},
    "new_item.ai_grading_heading": {"en": "AI condition grading", "lv": "AI stāvokļa vērtēšana"},
    "new_item.analyze_photos": {"en": "🧠 Analyze photos", "lv": "🧠 Analizēt fotogrāfijas"},
    "new_item.inspecting_photos": {
        "en": "Inspecting photos for defects, missing parts, and identity match...",
        "lv": "Pārbauda fotogrāfijas — defekti, trūkstošas daļas, atbilstība...",
    },

    "new_item.ai_busy": {"en": "AI service is busy right now. Please wait a moment and try again.", "lv": "AI serviss šobrīd ir aizņemts. Lūdzu, uzgaidi un mēģini vēlreiz."},
    "new_item.photo_analysis_failed": {"en": "Photo analysis failed: {error}", "lv": "Fotogrāfiju analīze neizdevās: {error}"},
    "new_item.skip_assess_manually": {"en": "Or skip and assess condition manually below.", "lv": "Vai izlaist un novērtēt stāvokli manuāli zemāk."},
    "new_item.ean_still_searching": {
        "en": "🔎 Still looking for the EAN in the background — will fill in automatically if found.",
        "lv": "🔎 Fonā joprojām meklē EAN — automātiski aizpildīsies, ja atradīsies.",
    },
    "new_item.ai_confidence": {"en": "AI confidence (%)", "lv": "AI pārliecība (%)"},
    "new_item.ai_confidence_help": {
        "en": "AI's confidence in the assigned product condition, based on photo "
              "quality, clarity of visible defects, certainty about box completeness, "
              "and how cleanly the item matches the A/B/C/D criteria. Lower photo "
              "quality or borderline cases should produce a lower number.",
        "lv": "AI pārliecība par piešķirto preces stāvokli, balstoties uz fotogrāfiju "
              "kvalitāti, redzamo defektu skaidrību, pārliecību par komplektācijas "
              "pilnīgumu un cik precīzi prece atbilst A/B/C/D kritērijiem. Zemāka foto "
              "kvalitāte vai robežgadījumi dod zemāku skaitli.",
    },
    "new_item.condition_confidence_caption": {
        "en": "Product Condition: {condition}  •  AI confidence: {confidence}%",
        "lv": "Preces stāvoklis: {condition}  •  AI pārliecība: {confidence}%",
    },
    "new_item.new_or_used": {"en": "New / Used", "lv": "Jauns / Lietots"},
    "new_item.used": {"en": "Used", "lv": "Lietots"},
    "new_item.new": {"en": "New", "lv": "Jauns"},
    "new_item.color_help": {"en": "Determined from the photos by AI — correct manually if needed.", "lv": "Noteikts no fotogrāfijām ar AI — labo manuāli, ja nepieciešams."},
    "new_item.defects_label": {"en": "Defects (one per line)", "lv": "Defekti (katrs jaunā rindā)"},
    "new_item.missing_components_label": {"en": "Missing components (one per line)", "lv": "Trūkstošās sastāvdaļas (katra jaunā rindā)"},
    "new_item.functional_checklist_label": {"en": "Functional test checklist (one per line)", "lv": "Funkcionālās pārbaudes saraksts (katrs jaunā rindā)"},
    "new_item.manifest_vs_photo": {"en": "Manifest vs. photo verification", "lv": "Manifesta un fotogrāfiju salīdzinājums"},
    "new_item.manifest_vs_photo_caption": {
        "en": "AI never assumes the manifest is correct — this compares what's "
              "actually visible in the photos (brand, model, product type, any "
              "visible barcode) against the claimed identity above.",
        "lv": "AI nekad nepieņem, ka manifests ir pareizs — šis salīdzina to, kas "
              "faktiski redzams fotogrāfijās (zīmols, modelis, produkta veids, "
              "jebkurš redzamais svītrkods) ar iepriekš norādīto identitāti.",
    },
    "new_item.product_match": {"en": "Product match", "lv": "Preces atbilstība"},
    "new_item.match_yes": {"en": "YES", "lv": "JĀ"},
    "new_item.match_no": {"en": "NO", "lv": "NĒ"},
    "new_item.match_unknown": {"en": "UNKNOWN", "lv": "NEZINĀMS"},
    "new_item.match_confidence": {"en": "Match confidence (%)", "lv": "Atbilstības pārliecība (%)"},
    "new_item.match_notes": {"en": "Match notes", "lv": "Piezīmes par atbilstību"},
    "new_item.mismatch_warning": {
        "en": "⚠️ Possible mismatch between the manifest data and the photographed "
              "item. Double-check the photos and manifest details before proceeding.",
        "lv": "⚠️ Iespējama neatbilstība starp manifesta datiem un nofotografēto "
              "preci. Pārbaudi fotogrāfijas un manifesta datus, pirms turpini.",
    },
    "new_item.price_listing_copy": {"en": "Price & listing copy", "lv": "Cena un sludinājuma teksts"},
    "new_item.mismatch_warning_short": {
        "en": "⚠️ This item was flagged as a possible manifest/photo mismatch in the previous step.",
        "lv": "⚠️ Šī prece iepriekšējā solī tika atzīmēta kā iespējama manifesta/fotogrāfiju neatbilstība.",
    },
    "new_item.estimate_price": {"en": "💲 Estimate market price", "lv": "💲 Novērtēt tirgus cenu"},
    "new_item.searching_comparable_prices": {"en": "Searching for comparable prices...", "lv": "Meklē salīdzināmas cenas..."},
    "new_item.estimated_price": {"en": "Estimated average selling price ($)", "lv": "Aptuvenā vidējā pārdošanas cena (€)"},
    "new_item.estimated_price_help": {
        "en": "AI-suggested market price. You can correct it here, or override it again in the final step.",
        "lv": "AI ieteiktā tirgus cena. Vari to labot šeit vai vēlreiz pēdējā solī.",
    },

    "new_item.generate_descriptions": {"en": "✍️ Generate English descriptions", "lv": "✍️ Ģenerēt aprakstus"},
    "new_item.writing_listing_copy": {"en": "Writing listing copy...", "lv": "Raksta sludinājuma tekstu..."},
    "new_item.description_generation_failed": {"en": "Description generation failed: {error}", "lv": "Apraksta ģenerēšana neizdevās: {error}"},
    "new_item.write_manually_caption": {"en": "Or write everything manually below.", "lv": "Vai raksti visu manuāli zemāk."},
    "new_item.auto_translate_failed": {
        "en": "Could not auto-translate into {language} — the English version was saved. "
              "You can retry from the product's Translate action.",
        "lv": "Neizdevās automātiski iztulkot uz {language} — saglabāta angļu valodas versija. "
              "Var mēģināt vēlreiz, izmantojot produkta tulkošanas darbību.",
    },
    "new_item.connect_translation_provider_hint": {
        "en": "{language} translation requires a connected translation provider. Go to Settings → Translation.",
        "lv": "{language} tulkošanai nepieciešams pievienots tulkošanas pakalpojums. Dodies uz Iestatījumi → Tulkošana.",
    },
    "new_item.listing_title": {"en": "Product Name (listing title)", "lv": "Produkta nosaukums (sludinājuma virsraksts)"},
    "new_item.general_overview": {"en": "Product Description (general overview)", "lv": "Produkta apraksts (vispārējs pārskats)"},
    "new_item.condition_scratches_details": {"en": "Additional Description (Condition & Scratches Details)", "lv": "Papildu apraksts (stāvoklis un skrāpējumu detaļas)"},
    "new_item.manual_only_details": {"en": "Manual-only details", "lv": "Tikai manuāli aizpildāmi dati"},
    "new_item.manual_only_caption": {"en": "AI cannot reliably determine these — fill them in by hand.", "lv": "AI nevar droši noteikt šos datus — aizpildi tos pats."},
    "new_item.location_shelf": {"en": "Location / shelf position", "lv": "Atrašanās vieta / plaukta pozīcija"},
    "new_item.functional_test_result": {"en": "Functional test result", "lv": "Funkcionālās pārbaudes rezultāts"},
    "new_item.test_not_tested": {"en": "Not Tested", "lv": "Nav pārbaudīts"},
    "new_item.test_working": {"en": "Working", "lv": "Darbojas"},
    "new_item.test_not_working": {"en": "Not Working", "lv": "Nedarbojas"},
    "new_item.quantity_help": {"en": "Number of units this listing represents. Defaults to 1.", "lv": "Cik vienību pārstāv šis ieraksts. Noklusējums ir 1."},
    "new_item.box_dimensions_caption": {"en": "Box dimensions (for courier/shipping calculations)", "lv": "Kastes izmēri (kurjera/piegādes aprēķiniem)"},
    "new_item.box_length": {"en": "Box length (cm)", "lv": "Kastes garums (cm)"},
    "new_item.box_width": {"en": "Box width (cm)", "lv": "Kastes platums (cm)"},
    "new_item.box_height": {"en": "Box height (cm)", "lv": "Kastes augstums (cm)"},
    "new_item.final_price": {"en": "Final selling price ($) — correct here if needed", "lv": "Galīgā pārdošanas cena (€) — labo šeit, ja nepieciešams"},
    "new_item.finalize_save": {"en": "Finalize & save to inventory", "lv": "Pabeigt un saglabāt inventārā"},
    "new_item.sku_fixed_caption": {
        "en": "SKU was set manually in step 2 and stays fixed — it is never changed or generated by AI.",
        "lv": "SKU tika iestatīts manuāli 2. solī un paliek nemainīgs — to nekad nemaina vai neģenerē AI.",
    },
    "new_item.product_match_warning": {
        "en": "⚠️ Product match: {match} ({confidence}% confidence). {notes}",
        "lv": "⚠️ Preces atbilstība: {match} ({confidence}% pārliecība). {notes}",
    },
    "new_item.summary": {"en": "Summary", "lv": "Kopsavilkums"},
    "new_item.condition_col": {"en": "Condition", "lv": "Stāvoklis"},
    "new_item.ai_confidence_col": {"en": "AI Confidence %", "lv": "AI pārliecība %"},
    "new_item.match_confidence_col": {"en": "Match Confidence %", "lv": "Atbilstības pārliecība %"},
    "new_item.photos_col": {"en": "Photos", "lv": "Fotogrāfijas"},
    "new_item.save_item": {"en": "✅ Save item", "lv": "✅ Saglabāt preci"},
    "new_item.saved_to_inventory": {"en": "Saved '{name}' to inventory.", "lv": "'{name}' saglabāts inventārā."},
    "new_item.start_over": {"en": "🔄 Start over / discard this item", "lv": "🔄 Sākt no jauna / atmest šo preci"},

    "new_item.photos_captured": {"en": "{count} photo(s) captured", "lv": "Uzņemtas {count} fotogrāfija(s)"},
    "new_item.still_processing": {"en": "⏳ {count} still processing...", "lv": "⏳ {count} vēl apstrādājas..."},
    "new_item.clean_background": {"en": "🧼 Clean background", "lv": "🧼 Attīrīt fonu"},
    "new_item.enhancing_photo": {
        "en": "Enhancing photo — first run downloads the model (~170MB) and may take a minute...",
        "lv": "Uzlabo fotogrāfiju — pirmajā reizē lejupielādē modeli (~170MB), var aizņemt minūti...",
    },
    "new_item.quality_check_failed": {
        "en": "Photo didn't pass the quality check: {issues} Please retake it (better focus/lighting, or closer up).",
        "lv": "Fotogrāfija neizturēja kvalitātes pārbaudi: {issues} Lūdzu, uzņem no jauna (labāks fokuss/apgaismojums vai tuvāk).",
    },
    "new_item.background_removal_failed": {"en": "Background removal failed: {error}", "lv": "Fona noņemšana neizdevās: {error}"},
    "new_item.low_res_warning": {
        "en": "⚠️ Source photo resolution was too low to fill the "
              "frame without blurring — the product was kept at its "
              "native sharpness instead. For a bigger, crisper result, "
              "retake this photo closer up and in better focus.",
        "lv": "⚠️ Avota fotogrāfijas izšķirtspēja bija par zemu, lai aizpildītu "
              "kadru bez izplūšanas — produkts saglabāts oriģinālajā asumā. "
              "Lielākam, asākam rezultātam uzņem šo fotogrāfiju no jauna tuvāk un labākā fokusā.",
    },
    "new_item.processing_ellipsis": {"en": "⏳ Processing...", "lv": "⏳ Apstrādā..."},
    "new_item.sku_from_manifest": {"en": "Assigned automatically from the manifest import.", "lv": "Piešķirts automātiski no manifesta importa."},
    "new_item.sku_required": {"en": "SKU *", "lv": "SKU *"},
    "new_item.sku_placeholder": {"en": "Enter SKU before continuing", "lv": "Ievadi SKU, pirms turpini"},
    "new_item.sku_help": {
        "en": "Entered manually — never changed or generated by AI. "
              "This SKU will be linked to all AI analysis results and the Excel record for this item.",
        "lv": "Ievadīts manuāli — AI to nekad nemaina un neģenerē. "
              "Šis SKU tiks piesaistīts visiem AI analīzes rezultātiem un šīs preces Excel ierakstam.",
    },
    "new_item.sku_required_warning": {
        "en": "⚠️ SKU is required before you can continue to analysis.",
        "lv": "⚠️ SKU ir obligāts, pirms vari turpināt uz analīzi.",
    },
    "new_item.waiting_for_processing": {
        "en": "⏳ Waiting for photo processing to finish before continuing...",
        "lv": "⏳ Gaida fotogrāfiju apstrādes pabeigšanu, pirms turpināt...",
    },

    "new_item.stage_searching": {"en": "🔎 Searching...", "lv": "🔎 Meklē..."},
    "new_item.candidate_found": {"en": "Candidate found (unconfirmed)", "lv": "Atrasts kandidāts (nepapstiprināts)"},
    "new_item.candidate_found_with_label": {"en": "Candidate found (unconfirmed): {label}", "lv": "Atrasts kandidāts (nepapstiprināts): {label}"},
    "new_item.stage_verifying": {"en": "⏳ Verifying / enriching specifications...", "lv": "⏳ Pārbauda / papildina specifikācijas..."},
    "new_item.enrichment_failed": {"en": "Background enrichment failed: {error}", "lv": "Fona datu papildināšana neizdevās: {error}"},
    "new_item.verified": {"en": "✅ Verified", "lv": "✅ Pārbaudīts"},
    "new_item.take_photo_label": {"en": "📷  Take a Photo or Choose from Library", "lv": "📷  Uzņem fotogrāfiju vai izvēlies no bibliotēkas"},
    "new_item.take_another_label": {"en": "📷  Take Another Photo", "lv": "📷  Uzņem vēl vienu fotogrāfiju"},
    "new_item.take_photo_shutter": {"en": "Take Photo", "lv": "Uzņemt"},

    # ---- Import Manifest ----
    "import_manifest.page_caption": {
        "en": "Upload an Amazon liquidation manifest (.xlsx or .csv). Importable fields: Target #, "
              "Subcategory, ASIN, EAN/Barcode, Item description, Qty, Weight (kg), SKU, Shelf location "
              "— only Item description is required, the rest can be left unmapped if your file doesn't "
              "have them (a missing ASIN/EAN is looked up automatically after import). Everything else "
              "(brand, model, product condition, price, descriptions...) is determined later by AI + "
              "photos, never assumed from the manifest. This is purely additive — it does not replace "
              "the existing manual scan/search flow in '🆕 New Item'.",
        "lv": "Augšupielādē Amazon likvidācijas manifestu (.xlsx vai .csv). Importējamie lauki: Target #, "
              "Subcategory, ASIN, EAN/Barcode, Item description, Qty, Weight (kg), SKU, Vieta plauktā "
              "— obligāts ir tikai Item description, pārējos var atstāt nesasaistītus, ja tavā failā to "
              "nav (trūkstošs ASIN/EAN pēc importa tiek meklēts automātiski). Viss pārējais (zīmols, "
              "modelis, stāvoklis, cena, apraksti...) tiek noteikts vēlāk ar AI + fotogrāfijām, nekad "
              "netiek pieņemts no manifesta. Tas ir tikai papildinošs — neaizstāj esošo manuālo "
              "skenēšanas/meklēšanas plūsmu '🆕 Jauns produkts'.",
    },
    "import_manifest.manifest_file": {"en": "Manifest file", "lv": "Manifesta fails"},
    "import_manifest.rows_found": {"en": "{count} row(s) found.", "lv": "Atrastas {count} rinda(s)."},
    "import_manifest.columns_in_file": {"en": "Columns in file: {columns}", "lv": "Kolonnas failā: {columns}"},
    "import_manifest.confirm_mapping": {
        "en": "Confirm column mapping — auto-detected where possible; adjust any that are wrong.",
        "lv": "Apstiprini kolonnu sasaisti — noteikta automātiski, kur iespējams; labo, kas nepareizi.",
    },
    "import_manifest.preview_rows": {"en": "Preview ({count} row(s) with the confirmed mapping):", "lv": "Priekšskatījums ({count} rinda(s) ar apstiprināto sasaisti):"},
    "import_manifest.import_batch_button": {"en": "✅ Import as new manifest batch", "lv": "✅ Importēt kā jaunu manifesta partiju"},
    "import_manifest.status_processing_detail": {"en": "🔄 Processing {done}/{total} — currently: {item}", "lv": "🔄 Apstrādā {done}/{total} — pašlaik: {item}"},
    "import_manifest.refresh_progress": {"en": "🔄 Refresh progress", "lv": "🔄 Atsvaidzināt progresu"},
    "import_manifest.progress_dialog_title": {"en": "Manifest import", "lv": "Manifesta imports"},
    "import_manifest.batch_started": {
        "en": "Imported manifest batch **{batch_id}** — {count} item(s) saved.",
        "lv": "Importēta manifesta partija **{batch_id}** — saglabātas {count} preces.",
    },
    "import_manifest.sku_conflicts_badge": {"en": "⚠️ {count} SKU conflict(s)", "lv": "⚠️ {count} SKU sadursme(-es)"},
    "import_manifest.sku_conflicts_intro": {
        "en": "{count} item(s) from this manifest were saved with a SKU that already belongs to another product. Resolve each one below.",
        "lv": "{count} šī manifesta prece(-es) tika saglabāta(-s) ar SKU, kas jau pieder citam produktam. Atrisini katru zemāk.",
    },
    "import_manifest.conflict_from_manifest": {"en": "From this manifest", "lv": "No šī manifesta"},
    "import_manifest.conflict_already_in_inventory": {"en": "Already in inventory", "lv": "Jau inventārā"},
    "import_manifest.same_product_delete": {"en": "✅ Same product — delete duplicate", "lv": "✅ Tā pati prece — dzēst dublikātu"},
    "import_manifest.different_product_assign": {"en": "🔀 Assign", "lv": "🔀 Piešķirt"},
    "import_manifest.new_sku_label": {"en": "New SKU", "lv": "Jauns SKU"},
    "import_manifest.confirm_delete_duplicate_warning": {
        "en": "Delete the just-imported duplicate **{name}**? This cannot be undone.",
        "lv": "Dzēst tikko importēto dublikātu **{name}**? Šo nevar atsaukt.",
    },
    "import_manifest.confirm_delete_duplicate": {"en": "Confirm delete", "lv": "Apstiprināt dzēšanu"},
    "import_manifest.sku_required": {"en": "Enter a SKU.", "lv": "Ievadi SKU."},
    "import_manifest.sku_still_conflicts": {"en": "SKU {sku} also already exists — pick a different one.", "lv": "SKU {sku} arī jau eksistē — izvēlies citu."},
    "import_manifest.resolve_conflicts_button": {"en": "Resolve SKU conflicts", "lv": "Atrisināt SKU sadursmes"},
    "import_manifest.process_hint": {
        "en": "Go to '🆕 New Item' → '📥 From a pending manifest item' to process them one by one.",
        "lv": "Ej uz '🆕 Jauns produkts' → '📥 No gaidoša manifesta ieraksta', lai apstrādātu tās pa vienai.",
    },
    "import_manifest.import_failed": {"en": "Import failed: {error}", "lv": "Imports neizdevās: {error}"},
    "import_manifest.manifest_batches": {"en": "Manifest batches", "lv": "Manifesta partijas"},
    "import_manifest.no_batches_yet": {"en": "No manifest batches imported yet for this company.", "lv": "Šim uzņēmumam vēl nav importētu manifesta partiju."},
    "import_manifest.status_processing": {"en": "🔄 Processing", "lv": "🔄 Apstrādā"},
    "import_manifest.status_imported": {"en": "✅ Imported", "lv": "✅ Importēts"},
    "import_manifest.status_error": {"en": "❌ Error", "lv": "❌ Kļūda"},
    "import_manifest.batch_summary_line": {
        "en": "{badge}  •  Uploaded {uploaded}  •  {row_count} product(s) in file  •  {linked} linked ({pending} pending, {processed} processed)",
        "lv": "{badge}  •  Augšupielādēts {uploaded}  •  {row_count} produkts(-i) failā  •  {linked} saistīti ({pending} gaida, {processed} apstrādāti)",
    },
    "import_manifest.linked_summary_cell": {
        "en": "{linked} ({pending} pending, {processed} processed)",
        "lv": "{linked} ({pending} gaida, {processed} apstrādāti)",
    },
    "import_manifest.replace_button": {"en": "🔄 Replace", "lv": "🔄 Aizstāt"},
    "import_manifest.column_mapping_used": {"en": "Column mapping used:", "lv": "Izmantotā kolonnu sasaiste:"},
    "import_manifest.linked_products": {"en": "Linked products:", "lv": "Saistītie produkti:"},
    "import_manifest.name_description_col": {"en": "Name / Description", "lv": "Nosaukums / apraksts"},
    "import_manifest.no_linked_products": {"en": "No products linked to this batch.", "lv": "Šai partijai nav saistītu produktu."},
    "import_manifest.upload_new_version": {"en": "Upload a new version of this manifest", "lv": "Augšupielādē jaunu šī manifesta versiju"},
    "import_manifest.replace_caption": {
        "en": "Rows matching an existing linked product (by Target #, ASIN, or EAN) "
              "are updated in place — SKU, photos, grading, and any other work already "
              "done are never touched. Rows that don't match anything become new pending "
              "items. No duplicates are created.",
        "lv": "Rindas, kas atbilst jau saistītam produktam (pēc mērķa Nr., ASIN vai EAN), "
              "tiek atjauninātas uz vietas — SKU, fotogrāfijas, vērtējums un cits jau paveiktais "
              "darbs netiek skarts. Rindas, kas nekam neatbilst, kļūst par jauniem gaidošiem "
              "ierakstiem. Dublikāti netiek izveidoti.",
    },
    "import_manifest.new_manifest_file": {"en": "New manifest file", "lv": "Jauns manifesta fails"},
    "import_manifest.confirm_replace": {"en": "✅ Confirm replace", "lv": "✅ Apstiprināt aizstāšanu"},
    "import_manifest.replaced_success": {
        "en": "Replaced: {updated} product(s) updated in place, {new} new product(s) added. No duplicates created.",
        "lv": "Aizstāts: {updated} produkts(-i) atjaunināti uz vietas, {new} jauns(-i) produkts(-i) pievienoti. Dublikāti nav izveidoti.",
    },
    "import_manifest.replace_failed": {"en": "Replace failed: {error}", "lv": "Aizstāšana neizdevās: {error}"},

    "import_manifest.missing_mapping": {"en": "Missing required mapping for: {labels}", "lv": "Trūkst obligātās sasaistes: {labels}"},
    "import_manifest.delete_batch_kept_note": {
        "en": "**{processed} already-processed product(s) will be kept** "
              "in your inventory — they'll just lose their manifest reference. ",
        "lv": "**{processed} jau apstrādātais(-ie) produkts(-i) tiks saglabāts(-i)** "
              "tavā inventārā — tie vienkārši zaudēs manifesta atsauci. ",
    },
    "import_manifest.cannot_be_undone": {"en": "This cannot be undone.", "lv": "Šo darbību nevar atsaukt."},
    "import_manifest.operations": {"en": "⚙️ Operations", "lv": "⚙️ Darbības"},
    "import_manifest.delete_manifests_op": {"en": "🗑️ Delete manifest(s)", "lv": "🗑️ Dzēst manifestu(s)"},
    "import_manifest.clear_selection": {"en": "Clear selection", "lv": "Notīrīt atlasi"},
    "import_manifest.selected_count": {"en": "{selected} of {total} selected", "lv": "Atlasīti {selected} no {total}"},
    "import_manifest.back_to_manifests": {"en": "← Back to manifests", "lv": "← Atpakaļ uz manifestiem"},
    "import_manifest.delete_batches_title": {"en": "Delete manifest batch(es)?", "lv": "Dzēst manifesta partiju(as)?"},
    "import_manifest.delete_batches_warning": {
        "en": "This will permanently delete **{count} manifest batch(es)** and their "
              "**{pending} still-pending (unprocessed) product(s)**.\n\n",
        "lv": "Šī darbība neatgriezeniski dzēsīs **{count} manifesta partiju(as)** un to "
              "**{pending} vēl gaidošo(-s) (neapstrādāto(-s)) produktu(-us)**.\n\n",
    },
    "import_manifest.confirm_delete_batches_checkbox": {"en": "I understand, delete the selected batch(es)", "lv": "Es saprotu, dzēst atlasīto(-ās) partiju(as)"},
    "import_manifest.deleted_batches_success": {"en": "Deleted {batches} batch(es) and {count} pending product(s).", "lv": "Dzēstas {batches} partija(as) un {count} gaidošais(-ie) produkts(-i)."},

    # ---- Product List ----
    "product_list.page_caption": {
        "en": "Review AI-generated info, manage inventory, and export to BaseLinker or download a spreadsheet — all from one list.",
        "lv": "Pārbaudi AI ģenerēto informāciju, pārvaldi inventāru un eksportē uz BaseLinker vai lejupielādē izklājlapu — viss vienā sarakstā.",
    },
    "product_list.status_ready": {"en": "✅ Ready", "lv": "✅ Gatavs"},
    "product_list.status_edited": {"en": "✏️ Edited", "lv": "✏️ Rediģēts"},
    "product_list.status_exported": {"en": "📤 Exported", "lv": "📤 Eksportēts"},
    "product_list.status_failed": {"en": "❌ Failed", "lv": "❌ Neizdevās"},
    "product_list.triage_testing_pending": {"en": "🔍 Testing pending", "lv": "🔍 Gaida pārbaudi"},
    "product_list.triage_ready_for_sale": {"en": "✅ Ready for sale", "lv": "✅ Gatavs pārdošanai"},
    "product_list.triage_needs_repair": {"en": "🔧 Needs repair", "lv": "🔧 Nepieciešams remonts"},
    "product_list.triage_for_parts": {"en": "♻️ For parts", "lv": "♻️ Rezerves daļām"},
    "product_list.triage_written_off": {"en": "❌ Written off", "lv": "❌ Norakstīts"},
    "product_list.grade_condition": {"en": "Grade / Condition", "lv": "Vērtējums / stāvoklis"},
    "product_list.warehouse_shelf": {"en": "Warehouse / Shelf", "lv": "Noliktava / plaukts"},
    "product_list.set_price": {"en": "Set price", "lv": "Iestatīt cenu"},
    "product_list.increase_by_eur": {"en": "Increase by €", "lv": "Palielināt par €"},
    "product_list.decrease_by_eur": {"en": "Decrease by €", "lv": "Samazināt par €"},
    "product_list.increase_by_pct": {"en": "Increase by %", "lv": "Palielināt par %"},
    "product_list.decrease_by_pct": {"en": "Decrease by %", "lv": "Samazināt par %"},
    "product_list.set_quantity": {"en": "Set quantity", "lv": "Iestatīt daudzumu"},
    "product_list.increase_by": {"en": "Increase by", "lv": "Palielināt par"},
    "product_list.decrease_by": {"en": "Decrease by", "lv": "Samazināt par"},
    "product_list.invalid_number": {"en": "Invalid number", "lv": "Nederīgs skaitlis"},
    "product_list.price_must_be_positive": {"en": "Price must stay above €0", "lv": "Cenai jābūt lielākai par €0"},
    "product_list.quantity_must_be_positive": {"en": "Quantity must stay at least 1", "lv": "Daudzumam jābūt vismaz 1"},
    "product_list.value_cannot_be_empty": {"en": "Value cannot be empty", "lv": "Vērtība nedrīkst būt tukša"},
    "product_list.filter_completed_only": {"en": "✅ Completed only", "lv": "✅ Tikai pabeigtie"},
    "product_list.filter_all_except_drafts": {"en": "All except drafts", "lv": "Visi, izņemot melnrakstus"},
    "product_list.filter_all_incl_drafts": {"en": "All (incl. drafts)", "lv": "Visi (ieskaitot melnrakstus)"},
    "product_list.draft_badge": {"en": "📥 DRAFT", "lv": "📥 MELNRAKSTS"},
    "product_list.export_dialog_title": {"en": "Export selected products", "lv": "Eksportēt atlasītos produktus"},
    "product_list.no_connected_marketplaces": {
        "en": "No connected marketplace integrations. Go to Settings → Integrations to connect one (e.g. BaseLinker).",
        "lv": "Nav savienotu tirdzniecības vietu integrāciju. Ej uz Iestatījumi → Integrācijas, lai savienotu vienu (piem., BaseLinker).",
    },
    "product_list.export_to": {"en": "Export to", "lv": "Eksportēt uz"},
    "product_list.export_confirm_text": {"en": "Export **{count}** selected product(s) to **{dest}**?", "lv": "Eksportēt **{count}** atlasīto(-s) produktu(-s) uz **{dest}**?"},
    "product_list.export_button": {"en": "📤 Export", "lv": "📤 Eksportēt"},
    "product_list.starting": {"en": "Starting...", "lv": "Sāk..."},
    "product_list.exporting_progress": {"en": "Exporting {sku} ({done}/{total})...", "lv": "Eksportē {sku} ({done}/{total})..."},
    "product_list.done": {"en": "Done.", "lv": "Gatavs."},
    "product_list.exported_successfully": {"en": "{count} products exported successfully", "lv": "{count} produkti veiksmīgi eksportēti"},
    "product_list.products_failed": {"en": "{count} product(s) failed", "lv": "{count} produkts(-i) neizdevās"},
    "product_list.delete_dialog_title": {"en": "Delete selected products?", "lv": "Dzēst atlasītos produktus?"},
    "product_list.delete_confirm_text": {
        "en": "Permanently delete **{count}** selected product(s)? This cannot be undone.",
        "lv": "Neatgriezeniski dzēst **{count}** atlasīto(-s) produktu(-us)? Šo darbību nevar atsaukt.",
    },

    "product_list.deleted_success": {"en": "Deleted {count} product(s).", "lv": "Dzēsts(-i) {count} produkts(-i)."},
    "product_list.bulk_edit_title": {"en": "Bulk Edit", "lv": "Masveida rediģēšana"},
    "product_list.bulk_edit_heading": {"en": "Bulk Edit — {count} selected products", "lv": "Masveida rediģēšana — {count} atlasīti produkti"},
    "product_list.field_to_edit": {"en": "Field to edit", "lv": "Rediģējamais lauks"},
    "product_list.action_label": {"en": "Action", "lv": "Darbība"},
    "product_list.value_label": {"en": "Value", "lv": "Vērtība"},
    "product_list.new_grade": {"en": "New Grade", "lv": "Jaunais vērtējums"},
    "product_list.new_value": {"en": "New value", "lv": "Jaunā vērtība"},
    "product_list.preview_changes": {"en": "Preview Changes", "lv": "Priekšskatīt izmaiņas"},
    "product_list.product_col": {"en": "Product", "lv": "Produkts"},
    "product_list.current_col": {"en": "Current", "lv": "Pašreizējais"},
    "product_list.new_col": {"en": "New", "lv": "Jaunais"},
    "product_list.skipped_due_to_errors": {"en": "{count} product(s) will be skipped due to the errors shown above.", "lv": "{count} produkts(-i) tiks izlaists(-i) iepriekš redzamo kļūdu dēļ."},
    "product_list.apply_changes": {"en": "Apply Changes", "lv": "Pielietot izmaiņas"},
    "product_list.bulk_edit_partial_success": {"en": "{ok} products updated successfully. {fail} products could not be updated.", "lv": "{ok} produkti veiksmīgi atjaunināti. {fail} produktus nevarēja atjaunināt."},
    "product_list.bulk_edit_success": {"en": "Successfully updated {count} products.", "lv": "Veiksmīgi atjaunināti {count} produkti."},
    "product_list.change_status_title": {"en": "Change Status", "lv": "Mainīt statusu"},
    "product_list.change_status_heading": {"en": "Change status for **{count}** selected products", "lv": "Mainīt statusu **{count}** atlasītiem produktiem"},
    "product_list.new_status": {"en": "New Status", "lv": "Jaunais statuss"},
    "product_list.apply_status_change": {"en": "Apply Status Change", "lv": "Pielietot statusa maiņu"},
    "product_list.status_changed_success": {"en": "Successfully changed status for {count} products.", "lv": "Statuss veiksmīgi mainīts {count} produktiem."},
    "product_list.photo_dialog_title": {"en": "Photo", "lv": "Fotogrāfija"},
    "product_list.photo_counter": {"en": "Photo {current} / {total}", "lv": "Fotogrāfija {current} / {total}"},
    "product_list.prev_photo": {"en": "◀ Prev photo", "lv": "◀ Iepriekšējā"},
    "product_list.next_photo": {"en": "Next photo ▶", "lv": "Nākamā ▶"},

    "product_list.exact_match": {"en": "🎯 Exact {tier} match", "lv": "🎯 Precīza {tier} atbilstība"},
    "product_list.no_name": {"en": "(no name)", "lv": "(bez nosaukuma)"},
    "product_list.triage_status": {"en": "Triage status", "lv": "Šķirošanas statuss"},
    "product_list.product_information": {"en": "Product Information", "lv": "Produkta informācija"},
    "product_list.name_moved_caption": {
        "en": "Product name moved below, next to the content-language selector.",
        "lv": "Produkta nosaukums pārvietots zemāk, blakus valodas izvēlei.",
    },
    "product_list.content_language": {"en": "Content language", "lv": "Satura valoda"},
    "product_list.translate_action": {"en": "🌐 Translate", "lv": "🌐 Tulkot"},
    "product_list.manually_edited_translation": {
        "en": "This translation was manually edited — re-translating won't overwrite it unless forced.",
        "lv": "Šis tulkojums ir manuāli rediģēts — atkārtota tulkošana to nepārrakstīs, ja vien nav piespiedu režīms.",
    },
    "product_list.translate_dialog_title": {"en": "Translate product", "lv": "Tulkot produktu"},
    "product_list.translate_heading": {"en": "Translate **{name}**", "lv": "Tulkot **{name}**"},
    "product_list.translate_target_languages": {"en": "Target language(s)", "lv": "Mērķa valoda(-as)"},
    "product_list.retranslate_suffix": {"en": "already translated", "lv": "jau iztulkots"},
    "product_list.retranslate_force": {
        "en": "Re-translate existing translations (including manually-edited ones)",
        "lv": "Tulkot atkārtoti esošos tulkojumus (arī manuāli rediģētos)",
    },
    "product_list.translate_select_language": {"en": "Select at least one target language.", "lv": "Izvēlies vismaz vienu mērķa valodu."},
    "product_list.translate_provider_not_connected": {
        "en": "Connect this translation provider in Settings → Translation first.",
        "lv": "Vispirms pievieno šo tulkošanas pakalpojumu sadaļā Iestatījumi → Tulkošana.",
    },
    "product_list.translate_failed": {"en": "Translation to {language} failed: {error}", "lv": "Tulkošana uz {language} neizdevās: {error}"},
    "product_list.translate_success": {"en": "Translated {count} language(s).", "lv": "Iztulkots {count} valoda(-s)."},
    "product_list.bulk_translate_title": {"en": "Translate products", "lv": "Tulkot produktus"},
    "product_list.bulk_translate_heading": {"en": "Translate {count} product(s)", "lv": "Tulkot {count} produktu(-us)"},
    "product_list.bulk_translate_too_many": {
        "en": "Select at most {max} products for bulk translate at once.",
        "lv": "Izvēlies ne vairāk kā {max} produktus vienlaicīgai tulkošanai.",
    },
    "product_list.bulk_translate_preview_summary": {
        "en": "{create} to create, {update} to update, {skip} skipped (manually edited).",
        "lv": "{create} jāizveido, {update} jāatjaunina, {skip} izlaisti (manuāli rediģēti).",
    },
    "product_list.bulk_translate_nothing_to_do": {
        "en": "Nothing to translate — every selected product/language pair is either the primary "
              "language or already manually translated.",
        "lv": "Nav ko tulkot — katrs izvēlētais produkta/valodas pāris ir vai nu oriģinālvaloda, "
              "vai jau manuāli iztulkots.",
    },
    "product_list.bulk_translate_success": {"en": "Translated {count} row(s).", "lv": "Iztulkots(-as) {count} rinda(-s)."},
    "product_list.bulk_translate_partial_success": {
        "en": "Translated {ok} row(s); {fail} failed.", "lv": "Iztulkots(-as) {ok} rinda(-s); {fail} neizdevās.",
    },
    "product_list.translate_skipped_manual": {
        "en": "{count} translation(s) skipped — already manually edited.",
        "lv": "{count} tulkojums(-i) izlaisti — jau manuāli rediģēti.",
    },
    "product_list.pricing": {"en": "Pricing", "lv": "Cenu noteikšana"},
    "product_list.price_eur": {"en": "Price (€)", "lv": "Cena (€)"},
    "product_list.additional_description": {"en": "Additional Description", "lv": "Papildu apraksts"},
    "product_list.defects": {"en": "Defects", "lv": "Defekti"},
    "product_list.missing_components": {"en": "Missing Components", "lv": "Trūkstošās sastāvdaļas"},
    "product_list.missing_components_label": {"en": "Missing Components (one per line)", "lv": "Trūkstošās sastāvdaļas (katra jaunā rindā)"},
    "product_list.box_contents": {"en": "Box Contents", "lv": "Komplektācija"},
    "product_list.box_contents_label": {"en": "Box Contents (one per line)", "lv": "Komplektācija (katrs vienums jaunā rindā)"},
    "product_list.functional_checklist": {"en": "Functional Checklist", "lv": "Funkcionālais saraksts"},
    "product_list.functional_checklist_label": {"en": "Functional Checklist (one per line)", "lv": "Funkcionālais saraksts (katrs vienums jaunā rindā)"},
    "product_list.condition_reasoning": {"en": "Product Condition reasoning: {reasoning}", "lv": "Preces stāvokļa pamatojums: {reasoning}"},
    "product_list.price_reasoning": {"en": "Price reasoning: {reasoning}", "lv": "Cenas pamatojums: {reasoning}"},
    "product_list.photos": {"en": "Photos", "lv": "Fotogrāfijas"},
    "product_list.enlarge": {"en": "🔍 Enlarge", "lv": "🔍 Palielināt"},
    "product_list.no_photos": {"en": "No photos.", "lv": "Nav fotogrāfiju."},
    "product_list.add_photos": {"en": "➕ Add photos (up to {remaining} more)", "lv": "➕ Pievienot fotogrāfijas (vēl līdz {remaining})"},
    "product_list.add_photos_button": {"en": "Add photos", "lv": "Pievienot fotogrāfijas"},
    "product_list.no_photos_selected": {"en": "No photos selected.", "lv": "Nav izvēlētas fotogrāfijas."},
    "product_list.only_added_photos": {
        "en": "Only added {remaining} photo(s) — {max} photo maximum reached.",
        "lv": "Pievienots(-i) tikai {remaining} fotogrāfija(s) — sasniegts maksimums ({max}).",
    },
    "product_list.added_photos_success": {"en": "Added {count} photo(s).", "lv": "Pievienota(-s) {count} fotogrāfija(s)."},
    "product_list.max_photos_reached": {"en": "Maximum of {max} photos reached.", "lv": "Sasniegts maksimums — {max} fotogrāfijas."},
    "product_list.save_button": {"en": "💾 Save", "lv": "💾 Saglabāt"},

    "product_list.saved": {"en": "Saved.", "lv": "Saglabāts."},
    "product_list.manifest_info": {"en": "📋 Manifest info", "lv": "📋 Manifesta informācija"},
    "product_list.manifest_item_description": {"en": "Manifest item description", "lv": "Manifesta preces apraksts"},
    "product_list.manifest_target_no": {"en": "Manifest Target #", "lv": "Manifesta mērķa Nr."},
    "product_list.subcategory": {"en": "Subcategory", "lv": "Apakškategorija"},
    "product_list.manifest_asin": {"en": "Manifest ASIN", "lv": "Manifesta ASIN"},
    "product_list.manifest_barcode": {"en": "Manifest barcode", "lv": "Manifesta svītrkods"},
    "product_list.batch_label": {"en": "Batch: {batch_id}", "lv": "Partija: {batch_id}"},
    "product_list.manually_entered": {"en": "Manually entered — no manifest origin.", "lv": "Ievadīts manuāli — bez manifesta izcelsmes."},
    "product_list.repair_history_title": {"en": "🛠️ Repair History ({count}) — €{total} total", "lv": "🛠️ Remontu vēsture ({count}) — kopā €{total}"},
    "product_list.no_description": {"en": "(no description)", "lv": "(bez apraksta)"},
    "product_list.cost": {"en": "Cost", "lv": "Izmaksas"},
    "product_list.technician": {"en": "Technician", "lv": "Tehniķis"},
    "product_list.add_repair_entry": {"en": "➕ Add repair entry", "lv": "➕ Pievienot remonta ierakstu"},
    "product_list.description_required": {"en": "Description is required.", "lv": "Apraksts ir obligāts."},
    "product_list.sales_listings": {"en": "🛒 Sales & Listings", "lv": "🛒 Pārdošana un sludinājumi"},
    "product_list.not_listed_yet": {"en": "Not listed on any marketplace yet.", "lv": "Vēl nav publicēts nevienā tirdzniecības vietā."},
    "product_list.preview_export": {"en": "🔍 Preview export", "lv": "🔍 Priekšskatīt eksportu"},
    "product_list.push_to_baselinker": {"en": "📤 Push to BaseLinker", "lv": "📤 Sūtīt uz BaseLinker"},
    "product_list.pushed_success": {"en": "Pushed — BaseLinker product_id: {external_id}", "lv": "Nosūtīts — BaseLinker product_id: {external_id}"},
    "product_list.sync_now": {"en": "🔄 Sync now", "lv": "🔄 Sinhronizēt tagad"},
    "product_list.pull_now": {"en": "⬇️ Pull now", "lv": "⬇️ Ievilkt tagad"},
    "product_list.sync_now_result": {"en": "🔄 Sync now — result", "lv": "🔄 Sinhronizēt tagad — rezultāts"},
    "product_list.success_word": {"en": "success", "lv": "veiksmīgi"},
    "product_list.disabled_word": {"en": "disabled", "lv": "atspējots"},

    "product_list.pull_now_result": {"en": "⬇️ Pull now — result", "lv": "⬇️ Ievilkt tagad — rezultāts"},
    "product_list.nothing_to_pull": {"en": "Nothing to pull — no BaseLinker listing yet, or BaseLinker returned no data.", "lv": "Nav ko ievilkt — vēl nav BaseLinker ieraksta, vai BaseLinker neatgrieza datus."},
    "product_list.field_accepted": {"en": "{field}: ✅ accepted — {value}", "lv": "{field}: ✅ pieņemts — {value}"},
    "product_list.field_conflict": {"en": "{field}: ⚠️ conflict, resolved to {value}", "lv": "{field}: ⚠️ konflikts, atrisināts uz {value}"},
    "product_list.field_pending_review": {"en": "{field}: ⏸️ pending manual review", "lv": "{field}: ⏸️ gaida manuālu pārbaudi"},
    "product_list.export_preview_title": {"en": "🔍 BaseLinker export preview", "lv": "🔍 BaseLinker eksporta priekšskatījums"},
    "product_list.export_preview_caption": {
        "en": "\"(excluded)\" means this field's toggle is off in Synchronization — "
              "an empty value shown without that note just means the product itself "
              "has nothing there yet.",
        "lv": "\"(izslēgts)\" nozīmē, ka šī lauka slēdzis Sinhronizācijā ir izslēgts — "
              "tukša vērtība bez šīs piezīmes vienkārši nozīmē, ka produktam vēl nav "
              "šīs informācijas.",
    },
    "product_list.empty_dash": {"en": "— (empty)", "lv": "— (tukšs)"},
    "product_list.excluded_dash": {"en": "— (excluded)", "lv": "— (izslēgts)"},
    "product_list.title_label": {"en": "Title", "lv": "Virsraksts"},
    "product_list.category_id": {"en": "Category ID", "lv": "Kategorijas ID"},
    "product_list.excluded_or_no_price": {"en": "— (excluded or no price set)", "lv": "— (izslēgts vai cena nav iestatīta)"},
    "product_list.images_label": {"en": "Images", "lv": "Attēli"},
    "product_list.images_included": {"en": "{count} included", "lv": "iekļauti {count}"},
    "product_list.close_preview": {"en": "Close preview", "lv": "Aizvērt priekšskatījumu"},
    "product_list.financials": {"en": "💰 Financials", "lv": "💰 Finanses"},
    "product_list.purchase_price_allocated": {"en": "Purchase price (allocated)", "lv": "Iepirkuma cena (piešķirtā)"},
    "product_list.repair_cost": {"en": "Repair cost", "lv": "Remonta izmaksas"},
    "product_list.profit_metric": {"en": "Profit (selling − purchase − repairs)", "lv": "Peļņa (pārdošana − iepirkums − remonti)"},
    "product_list.filter_products": {"en": "🔍 Filter Products", "lv": "🔍 Filtrēt produktus"},
    "product_list.filter_products_open": {"en": "🔍 Filter Products ▲", "lv": "🔍 Filtrēt produktus ▲"},
    "product_list.all_batches": {"en": "All batches", "lv": "Visas partijas"},
    "product_list.search_by_field": {"en": "Search by field (all combine together — narrows on every non-empty box)", "lv": "Meklēt pēc lauka (visi apvienojas — sašaurina pēc katras aizpildītās ailes)"},
    "product_list.more_filters": {"en": "More filters", "lv": "Vairāk filtru"},
    "product_list.manifest_batch": {"en": "Manifest batch", "lv": "Manifesta partija"},
    "product_list.exact_lookup_label": {"en": "🔍 Exact lookup by SKU, EAN, ASIN, Product Name, Brand, or Model", "lv": "🔍 Precīza meklēšana pēc SKU, EAN, ASIN, nosaukuma, zīmola vai modeļa"},
    "product_list.exact_lookup_help": {
        "en": "Searches the WHOLE inventory regardless of the filters above. "
              "Priority: exact SKU match, then exact EAN, then exact ASIN, then "
              "model number, then brand/product name. Works for manifest-imported "
              "and manually-added items alike.",
        "lv": "Meklē VISĀ inventārā neatkarīgi no augstāk esošajiem filtriem. "
              "Prioritāte: precīza SKU atbilstība, tad precīzs EAN, tad precīzs ASIN, "
              "tad modeļa numurs, tad zīmols/nosaukums. Strādā gan manifestā importētiem, "
              "gan manuāli pievienotiem produktiem.",
    },

    "product_list.set_filters": {"en": "✅ Set filters", "lv": "✅ Uzstādīt filtrus"},
    "product_list.clear_filters": {"en": "✖ Clear filters", "lv": "✖ Notīrīt filtrus"},

    "product_list.operations": {"en": "⚙️ Operations", "lv": "⚙️ Darbības"},
    "product_list.bulk_edit": {"en": "✏️ Bulk Edit", "lv": "✏️ Masveida rediģēšana"},
    "product_list.change_status": {"en": "🔄 Change Status", "lv": "🔄 Mainīt statusu"},
    "product_list.delete_products": {"en": "🗑️ Delete Products", "lv": "🗑️ Dzēst produktus"},
    "product_list.admins_reviewers_only": {"en": "Only Admins and Reviewers can export or delete.", "lv": "Tikai administratori un pārbaudītāji var eksportēt vai dzēst."},
    "product_list.clear_selection": {"en": "✖ Clear Selection", "lv": "✖ Notīrīt atlasi"},
    "product_list.download": {"en": "⬇️ Download", "lv": "⬇️ Lejupielādēt"},
    "product_list.download_excel": {"en": "⬇️ Download Excel (.xlsx)", "lv": "⬇️ Lejupielādēt Excel (.xlsx)"},
    "product_list.download_csv": {"en": "⬇️ Download CSV", "lv": "⬇️ Lejupielādēt CSV"},
    "product_list.image_links_note": {
        "en": "Note: 'Image Links' currently contains local file "
              "paths. Baselinker's importer needs public image "
              "URLs — upload the photos to hosting (or "
              "Baselinker's own media manager) and substitute the "
              "URLs, or attach photos manually per listing after "
              "import.",
        "lv": "Piezīme: 'Image Links' šobrīd satur lokālus faila "
              "ceļus. Baselinker importētājam vajag publiski pieejamas "
              "attēlu URL adreses — augšupielādē fotogrāfijas hostingā "
              "(vai Baselinker paša mediju pārvaldniekā) un aizstāj "
              "URL, vai pievieno fotogrāfijas manuāli katram ierakstam "
              "pēc importa.",
    },
    "product_list.total_count_selected": {"en": "{total} product(s) total — Selected: {selected}", "lv": "Kopā {total} produkts(-i) — Atlasīts: {selected}"},
    "product_list.condition_filter_label": {"en": "condition {condition}", "lv": "stāvoklis {condition}"},
    "product_list.exact_lookup_active": {"en": "exact lookup “{query}”", "lv": "precīza meklēšana “{query}”"},
    "product_list.active_filters": {"en": "Active: {filters}", "lv": "Aktīvi: {filters}"},
    "product_list.no_filters_applied": {"en": "No filters applied — showing completed products only.", "lv": "Filtri nav pielietoti — tiek rādīti tikai pabeigtie produkti."},
    "product_list.match_count": {"en": "{count} match(es) for '{query}'", "lv": "{count} atbilstība(-as) '{query}'"},
    "product_list.brand_name_tier": {"en": "brand/name", "lv": "zīmols/nosaukums"},
    "product_list.no_products_match_filter": {"en": "No products match this filter yet.", "lv": "Šim filtram vēl nav atbilstošu produktu."},
    "product_list.no_products_on_page": {"en": "No products on this page.", "lv": "Šajā lapā nav produktu."},

    "product_list.column_sort_note": {
        "en": "Column sort in the table above only applies to the "
              "current page — use the filters above the list to "
              "narrow across the whole inventory.",
        "lv": "Kolonnu kārtošana augstāk redzamajā tabulā attiecas tikai uz "
              "pašreizējo lapu — izmanto filtrus virs saraksta, lai "
              "sašaurinātu meklējumu visā inventārā.",
    },
    "product_list.product_not_found": {"en": "Product not found — it may have been deleted.", "lv": "Produkts nav atrasts — iespējams, tas ir dzēsts."},
    "product_list.previous_product": {"en": "◀ Previous Product", "lv": "◀ Iepriekšējais produkts"},
    "product_list.next_product": {"en": "Next Product ▶", "lv": "Nākamais produkts ▶"},

    # ---- Orders ----
    "orders.page_caption": {
        "en": "Orders pulled in from your connected marketplaces — read-only here, synced automatically in the background.",
        "lv": "Pasūtījumi, kas ievilkti no taviem savienotajiem tirdzniecības kanāliem — šeit tikai skatāmi, sinhronizējas automātiski fonā.",
    },
    "orders.search_placeholder": {"en": "Order number, customer name, or SKU", "lv": "Pasūtījuma numurs, klienta vārds vai SKU"},
    "orders.marketplace": {"en": "Marketplace", "lv": "Tirdzniecības vieta"},
    "orders.no_orders_yet": {
        "en": "No orders yet. Connect a marketplace integration in Settings -> Integrations to start syncing orders here.",
        "lv": "Vēl nav pasūtījumu. Savieno tirdzniecības vietas integrāciju sadaļā Iestatījumi -> Integrācijas, lai sāktu sinhronizēt pasūtījumus šeit.",
    },
    "orders.none_placeholder": {"en": "(none)", "lv": "(nav)"},
    "orders.page_of": {"en": "Page {page} of {total_pages} — {count} order(s)", "lv": "Lapa {page} no {total_pages} — {count} pasūtījums(-i)"},
    "orders.order_not_found": {"en": "Order not found — it may have been removed.", "lv": "Pasūtījums nav atrasts — iespējams, tas ir dzēsts."},
    "orders.order_heading": {"en": "Order {ref}", "lv": "Pasūtījums {ref}"},
    "orders.customer": {"en": "Customer", "lv": "Klients"},
    "orders.customer_comments": {"en": "Customer comments", "lv": "Klienta piezīmes"},
    "orders.order_date": {"en": "Order date", "lv": "Pasūtījuma datums"},
    "orders.shipping_method": {"en": "Shipping method", "lv": "Piegādes metode"},
    "orders.delivery_address": {"en": "Delivery address", "lv": "Piegādes adrese"},
    "orders.invoice_address": {"en": "Invoice address", "lv": "Rēķina adrese"},
    "orders.items": {"en": "Items", "lv": "Preces"},
    "orders.no_item_detail": {"en": "No item detail available for this order.", "lv": "Šim pasūtījumam nav pieejama preču informācija."},
    "orders.total": {"en": "Total: {amount} {currency}", "lv": "Kopā: {amount} {currency}"},
    "product_list.prev_simple": {"en": "‹ Previous", "lv": "‹ Iepriekšējā"},
    "product_list.next_simple": {"en": "Next ›", "lv": "Nākamā ›"},

    # ---- table_col.* — AG Grid column headers (Product List + Orders) ----
    "table_col.photo_url": {"en": "Photo", "lv": "Foto"},
    "table_col.sku": {"en": "SKU", "lv": "SKU"},
    "table_col.name": {"en": "Product Name", "lv": "Produkta nosaukums"},
    "table_col.brand": {"en": "Brand", "lv": "Zīmols"},
    "table_col.quantity": {"en": "Qty", "lv": "Daudz."},
    "table_col.price": {"en": "Price", "lv": "Cena"},
    "table_col.product_condition": {"en": "Product Condition", "lv": "Stāvoklis"},
    "table_col.triage": {"en": "Triage", "lv": "Šķirošana"},
    "table_col.location": {"en": "Location", "lv": "Vieta"},
    "table_col.baselinker": {"en": "BaseLinker", "lv": "BaseLinker"},
    "table_col.status": {"en": "Status", "lv": "Statuss"},
    "table_col.date": {"en": "Date", "lv": "Datums"},
    "table_col.order_number": {"en": "Number", "lv": "Numurs"},
    "table_col.customer_name": {"en": "Customer", "lv": "Klients"},
    "table_col.items_summary": {"en": "Items", "lv": "Preces"},
    "table_col.price_total": {"en": "Price", "lv": "Cena"},
    "table_col.shipping_method": {"en": "Shipping", "lv": "Piegāde"},
    "table_col.order_date_label": {"en": "Date", "lv": "Datums"},
    "table_col.status_label": {"en": "Status", "lv": "Statuss"},
    "table_col.marketplace": {"en": "Marketplace", "lv": "Tirdz. vieta"},
    "table_col.filename": {"en": "File", "lv": "Fails"},
    "table_col.row_count": {"en": "Rows", "lv": "Rindas"},
    "table_col.linked_summary": {"en": "Linked", "lv": "Saistīti"},

    # ---- Manage Users ----
    "manage_users.users_in": {"en": "Users in {company}", "lv": "Lietotāji uzņēmumā {company}"},
    "manage_users.deactivate": {"en": "Deactivate", "lv": "Deaktivizēt"},
    "manage_users.activate": {"en": "Activate", "lv": "Aktivizēt"},
    "manage_users.inactive": {"en": "Inactive", "lv": "Neaktīvs"},
    "manage_users.add_user": {"en": "Add a user", "lv": "Pievienot lietotāju"},
    "manage_users.add_user_button": {"en": "➕ Add user", "lv": "➕ Pievienot lietotāju"},
    "manage_users.all_fields_required": {"en": "Name, email, and password are all required.", "lv": "Vārds, e-pasts un parole ir obligāti."},
    "manage_users.added_user": {"en": "Added {email}.", "lv": "Pievienots {email}."},

    # ---- Settings / Integrations ----
    "settings.integrations_tab": {"en": "🔌 Integrations", "lv": "🔌 Integrācijas"},
    "settings.translation_tab": {"en": "🌐 Translation", "lv": "🌐 Tulkošana"},
    "settings.translation_tab_caption": {
        "en": "Controls which language new products are shown/translated into by default, "
              "and which service performs the translation. Marketplace exports can override "
              "this per integration — see that integration's settings.",
        "lv": "Nosaka, kādā valodā jauni produkti pēc noklusējuma tiek rādīti/tulkoti, un kurš "
              "pakalpojums veic tulkošanu. Katrai tirdzniecības vietas integrācijai to var "
              "pārrakstīt atsevišķi — skatiet attiecīgās integrācijas iestatījumus.",
    },
    "settings.default_product_language": {"en": "Default product language", "lv": "Noklusējuma produktu valoda"},
    "settings.default_product_language_help": {
        "en": "New products are auto-translated into this language right after AI generation.",
        "lv": "Jauni produkti tiek automātiski iztulkoti šajā valodā uzreiz pēc AI ģenerēšanas.",
    },
    "settings.translation_provider": {"en": "Translation provider", "lv": "Tulkošanas pakalpojums"},
    "settings.translation_settings_saved": {"en": "Translation settings saved.", "lv": "Tulkošanas iestatījumi saglabāti."},
    "settings.translation_provider_not_connected": {
        "en": "{provider} is not connected yet — new products will stay in their original "
              "language until you connect it below.",
        "lv": "{provider} vēl nav pievienots — jauni produkti paliks oriģinālvalodā, kamēr "
              "to nepievienosiet zemāk.",
    },
    "settings.translation_provider_connected": {
        "en": "Translation provider connected.", "lv": "Tulkošanas pakalpojums pievienots.",
    },
    "settings.health_connected": {"en": "Connected", "lv": "Savienots"},
    "settings.health_attention": {"en": "Needs attention", "lv": "Nepieciešama uzmanība"},
    "settings.health_failed": {"en": "Connection failed", "lv": "Savienojums neizdevās"},
    "settings.health_never_synced": {"en": "Never synchronized", "lv": "Nekad nav sinhronizēts"},
    "settings.inventory_label": {"en": "Inventory {inventory_id}", "lv": "Inventārs {inventory_id}"},
    "settings.disconnect_dialog_title": {"en": "Disconnect integration?", "lv": "Atvienot integrāciju?"},
    "settings.disconnect_warning": {
        "en": "This disconnects **{name}** for {company}. Stored "
              "credentials are deleted (other settings are kept); pushing products "
              "through it will stop working until reconnected. This cannot be undone.",
        "lv": "Šī darbība atvienos **{name}** uzņēmumam {company}. Saglabātie "
              "akreditācijas dati tiek dzēsti (citi iestatījumi paliek); produktu sūtīšana "
              "caur to pārstās darboties, kamēr netiks savienota no jauna. Šo darbību nevar atsaukt.",
    },
    "settings.disconnect_confirm_checkbox": {"en": "I understand, disconnect", "lv": "Es saprotu, atvienot"},
    "settings.disconnect_button": {"en": "🔌 Disconnect", "lv": "🔌 Atvienot"},
    "settings.disconnected_success": {"en": "{name} disconnected.", "lv": "{name} atvienots."},
    "settings.recent_activity": {"en": "Recent activity", "lv": "Nesenā aktivitāte"},
    "settings.no_activity_yet": {"en": "No activity yet.", "lv": "Vēl nav aktivitātes."},
    "settings.product_word": {"en": "product", "lv": "produkts"},
    "settings.test_connection": {"en": "🔄 Test connection", "lv": "🔄 Pārbaudīt savienojumu"},
    "settings.import_catalog_heading": {"en": "📥 Import catalog from {name}", "lv": "📥 Importēt katalogu no {name}"},
    "settings.import_catalog_caption": {
        "en": "One-time (but safely re-runnable) import of your existing {name} products into "
              "ElectroGrader, so you don't have to re-enter them by hand. Imported products start as drafts "
              "and need review (Product Condition/Description) before they're complete. Runs in the background — "
              "feel free to keep using the app while it imports.",
        "lv": "Vienreizējs (bet droši atkārtojams) tavu esošo {name} produktu imports uz "
              "ElectroGrader, lai nevajadzētu tos ievadīt no jauna. Importētie produkti sākas kā melnraksti "
              "un jāpārbauda (stāvoklis/apraksts), pirms tie ir pabeigti. Darbojas fonā — "
              "vari turpināt lietot aplikāciju importēšanas laikā.",
    },
    "settings.last_import_errors": {
        "en": "Last import: {imported} imported, {skipped} skipped, {error_count} error(s): {errors}",
        "lv": "Pēdējais imports: {imported} importēti, {skipped} izlaisti, {error_count} kļūda(-s): {errors}",
    },
    "settings.last_import_success": {
        "en": "Last import: {imported} product(s) imported, {skipped} skipped (already existed).",
        "lv": "Pēdējais imports: {imported} produkts(-i) importēti, {skipped} izlaisti (jau eksistēja).",
    },
    "settings.preview_import": {"en": "🔍 Preview import", "lv": "🔍 Priekšskatīt importu"},
    "settings.nothing_to_import": {
        "en": "Nothing to import — either {name} has no products, or this connector doesn't support catalog import yet.",
        "lv": "Nav ko importēt — vai nu {name} nav produktu, vai šis savienotājs vēl neatbalsta kataloga importu.",
    },
    "settings.import_summary": {"en": "**{new}** new product(s) to import, **{existing}** already exist (will be skipped).", "lv": "**{new}** jauns(-i) produkts(-i) importēšanai, **{existing}** jau eksistē (tiks izlaisti)."},
    "settings.import_n_products": {"en": "📥 Import {count} products", "lv": "📥 Importēt {count} produktus"},

    "settings.last_sync": {"en": "Last sync: {ts}", "lv": "Pēdējā sinhronizācija: {ts}"},
    "settings.api_token": {"en": "API token", "lv": "API atslēga"},
    "settings.leave_blank_current": {"en": "Leave blank to keep current", "lv": "Atstāj tukšu, lai saglabātu pašreizējo"},
    "settings.baselinker_token_help": {"en": "Account & other -> My account -> API in the BaseLinker/Base.com panel.", "lv": "Konts un cits -> Mans konts -> API BaseLinker/Base.com panelī."},
    "settings.inventory_id": {"en": "Inventory ID", "lv": "Inventāra ID"},
    "settings.category_id_field": {"en": "Category ID", "lv": "Kategorijas ID"},
    "settings.price_group_id": {"en": "Price group ID (optional)", "lv": "Cenu grupas ID (nav obligāts)"},
    "settings.warehouse_id": {"en": "Warehouse ID (optional)", "lv": "Noliktavas ID (nav obligāts)"},
    "settings.fetch_options": {"en": "🔍 Fetch options", "lv": "🔍 Ielādēt opcijas"},
    "settings.refetch_options": {"en": "🔄 Re-fetch options", "lv": "🔄 Ielādēt opcijas no jauna"},
    "settings.fetching_options": {"en": "Fetching account data from BaseLinker…", "lv": "Ielādē konta datus no BaseLinker…"},
    "settings.fetch_options_help": {"en": "Enter your API token above first, then fetch your inventories, categories, price groups and warehouses by name — no need to know their raw IDs.", "lv": "Vispirms augšā ievadi savu API atslēgu, tad ielādē savus inventārus, kategorijas, cenu grupas un noliktavas pēc nosaukuma — nav jāzina to skaitliskie ID."},
    "settings.fetch_options_failed": {"en": "Could not fetch account data: {error}", "lv": "Neizdevās ielādēt konta datus: {error}"},
    "settings.select_inventory": {"en": "Inventory", "lv": "Inventārs"},
    "settings.load_categories": {"en": "📂 Load categories", "lv": "📂 Ielādēt kategorijas"},
    "settings.select_category": {"en": "Category", "lv": "Kategorija"},
    "settings.select_price_group": {"en": "Price group", "lv": "Cenu grupa"},
    "settings.select_warehouse": {"en": "Warehouse", "lv": "Noliktava"},
    "settings.option_none": {"en": "— none —", "lv": "— nav —"},
    "settings.load_categories_first": {"en": "Pick an inventory and click \"Load categories\" to continue.", "lv": "Izvēlies inventāru un spied \"Ielādēt kategorijas\", lai turpinātu."},
    "settings.tax_rate": {"en": "Tax rate % (optional)", "lv": "Nodokļa likme % (nav obligāts)"},
    "settings.export_language": {"en": "Export language", "lv": "Eksporta valoda"},
    "settings.export_language_use_default": {"en": "Use company default", "lv": "Izmantot uzņēmuma noklusējumu"},
    "settings.export_language_help": {
        "en": "Overrides the company's default product language for this integration only "
              "— leave as \"Use company default\" unless this channel needs a different language.",
        "lv": "Pārraksta uzņēmuma noklusējuma produktu valodu tikai šai integrācijai — atstāj "
              "\"Izmantot uzņēmuma noklusējumu\", ja vien šim kanālam nav nepieciešama cita valoda.",
    },
    "settings.save_test_connection": {"en": "💾 Save & test connection", "lv": "💾 Saglabāt un pārbaudīt savienojumu"},
    "settings.api_token_required": {"en": "API token is required.", "lv": "API atslēga ir obligāta."},
    "settings.inventory_category_required": {"en": "Inventory ID and Category ID are required.", "lv": "Inventāra ID un kategorijas ID ir obligāti."},
    "settings.api_key": {"en": "API key", "lv": "API atslēga"},
    "settings.api_key_required": {"en": "API key is required.", "lv": "API atslēga ir obligāta."},
    "settings.freq_manual": {"en": "Manual", "lv": "Manuāli"},
    "settings.freq_15min": {"en": "Every 15 minutes", "lv": "Ik pēc 15 minūtēm"},
    "settings.freq_hourly": {"en": "Hourly", "lv": "Katru stundu"},
    "settings.freq_daily": {"en": "Daily", "lv": "Katru dienu"},
    "settings.dir_push": {"en": "ElectroGrader → Platform", "lv": "ElectroGrader → Platforma"},
    "settings.dir_pull": {"en": "Platform → ElectroGrader", "lv": "Platforma → ElectroGrader"},
    "settings.dir_two_way": {"en": "Two-way", "lv": "Divvirzienu"},
    "settings.conflict_keep_local": {"en": "Keep ElectroGrader data", "lv": "Saglabāt ElectroGrader datus"},
    "settings.conflict_keep_remote": {"en": "Keep external platform data", "lv": "Saglabāt ārējās platformas datus"},
    "settings.conflict_ask_me": {"en": "Ask me", "lv": "Jautāt man"},
    "settings.trigger_product_completed": {
        "en": "When a product is marked completed → export it automatically",
        "lv": "Kad produkts atzīmēts kā pabeigts → eksportēt automātiski",
    },
    "settings.trigger_product_updated": {
        "en": "When a product's price/description/photos/quantity changes → update the listing",
        "lv": "Kad mainās produkta cena/apraksts/fotogrāfijas/daudzums → atjaunināt ierakstu",
    },
    "settings.trigger_stock_changed": {
        "en": "When stock reaches zero → end the listing",
        "lv": "Kad krājums sasniedz nulli → pabeigt ierakstu",
    },
    "settings.sync_tab_caption": {"en": "Choose what ElectroGrader sends to / receives from this integration, and how often.", "lv": "Izvēlies, ko ElectroGrader sūta uz / saņem no šīs integrācijas, un cik bieži."},
    "settings.data_sent": {"en": "Data sent from ElectroGrader", "lv": "Dati, ko sūta ElectroGrader"},
    "settings.not_yet_applied": {"en": "not yet applied to export", "lv": "vēl netiek pielietots eksportā"},
    "settings.data_received": {"en": "Data received from external platform", "lv": "Dati, kas saņemti no ārējās platformas"},
    "settings.sync_rules_heading": {"en": "Synchronization rules", "lv": "Sinhronizācijas noteikumi"},
    "settings.frequency": {"en": "Frequency", "lv": "Biežums"},
    "settings.phase1_automation_note": {
        "en": "⏭️ Phase 1: scheduled runs are queued and logged, but no integration has real "
              "automatic sync wired up yet — expect a \"Skipped (not implemented yet)\" entry "
              "in Logs each time, until that integration's automation ships.",
        "lv": "⏭️ 1. fāze: plānotie palaišanas tiek ierindoti un reģistrēti, bet nevienai integrācijai "
              "vēl nav pievienota reāla automātiskā sinhronizācija — sagaidi ierakstu "
              "\"Izlaists (vēl nav ieviests)\" žurnālos, kamēr tas netiks ieviests.",
    },
    "settings.direction": {"en": "Direction", "lv": "Virziens"},
    "settings.conflict_handling": {"en": "Conflict handling", "lv": "Konfliktu risināšana"},
    "settings.save_sync_settings": {"en": "💾 Save synchronization settings", "lv": "💾 Saglabāt sinhronizācijas iestatījumus"},
    "settings.no_mappable_fields": {"en": "{name} has no mappable fields yet.", "lv": "{name} vēl nav sasaistāmu lauku."},
    "settings.field_mapping_caption": {
        "en": "Map ElectroGrader data onto this integration's technical fields. One active configuration per integration.",
        "lv": "Sasaisti ElectroGrader datus ar šīs integrācijas tehniskajiem laukiem. Viena aktīva konfigurācija katrai integrācijai.",
    },
    "settings.search_mapping_rules": {"en": "🔍 Search mapping rules...", "lv": "🔍 Meklēt sasaistes noteikumus..."},

    "settings.clear_search_to_edit": {"en": "Clear the search box to edit and save mappings.", "lv": "Notīri meklēšanas lauku, lai rediģētu un saglabātu sasaistes."},
    "settings.save_field_mapping": {"en": "💾 Save field mapping", "lv": "💾 Saglabāt lauku sasaisti"},
    "settings.ownership_caption": {
        "en": "Which system is the master source of truth for each field. Read by the real Push/Pull "
              "sync engine (manual “Sync now”/“Pull now”, and Automatic Sync once enabled "
              "in the Automation tab) to decide what wins on a conflict.",
        "lv": "Kura sistēma ir galvenais uzticamais avots katram laukam. To izlasa reālais Push/Pull "
              "sinhronizācijas dzinējs (manuālais \"Sinhronizēt tagad\"/\"Ievilkt tagad\", un Automātiskā "
              "sinhronizācija, ja ieslēgta Automatizācijas cilnē), lai izlemtu, kas uzvar konflikta gadījumā.",
    },
    "settings.manual_word": {"en": "Manual", "lv": "Manuāli"},
    "settings.manual_review": {"en": "Manual review", "lv": "Manuāla pārbaude"},
    "settings.group_product_content": {"en": "Product Content", "lv": "Produkta saturs"},
    "settings.group_sales_data": {"en": "Sales Data", "lv": "Pārdošanas dati"},
    "settings.enabled_word": {"en": "Enabled", "lv": "Iespējots"},
    "settings.last_sync_dash": {"en": "Last sync: —", "lv": "Pēdējā sinhronizācija: —"},
    "settings.pending_review": {"en": "Pending review", "lv": "Gaida pārbaudi"},
    "settings.success_word": {"en": "Success", "lv": "Veiksmīgi"},
    "settings.last_sync_status": {"en": "Last sync: {ts}  \nStatus: {status}", "lv": "Pēdējā sinhronizācija: {ts}  \nStatuss: {status}"},
    "settings.conflict_policy_warning": {
        "en": "Conflict policy allows {policy} changes although this field is owned by {owner}.",
        "lv": "Konfliktu politika atļauj {policy} izmaiņas, kaut arī šo lauku pārvalda {owner}.",
    },
    "settings.save_sync_ownership": {"en": "💾 Save Sync Ownership", "lv": "💾 Saglabāt sinhronizācijas piederību"},
    "settings.automation_future_work_note": {"en": "These event triggers are saved but not executed automatically yet — future work.", "lv": "Šie notikumu trigeri tiek saglabāti, bet vēl netiek izpildīti automātiski — nākotnes darbs."},
    "settings.save_automation_settings": {"en": "💾 Save automation settings", "lv": "💾 Saglabāt automatizācijas iestatījumus"},
    "settings.automatic_sync_heading": {"en": "Automatic Sync (real-time push/pull)", "lv": "Automātiskā sinhronizācija (reāllaika push/pull)"},
    "settings.automatic_sync_caption": {
        "en": "When enabled, ElectroGrader will automatically push/pull changes to/from {name} in the background, on the intervals below.",
        "lv": "Kad ieslēgts, ElectroGrader automātiski sūtīs/ievilks izmaiņas uz/no {name} fonā, ar zemāk norādītajiem intervāliem.",
    },
    "settings.enable_automatic_sync": {"en": "Enable automatic sync", "lv": "Ieslēgt automātisko sinhronizāciju"},
    "settings.push_interval": {"en": "Push interval (seconds)", "lv": "Push intervāls (sekundēs)"},
    "settings.pull_interval": {"en": "Pull interval (seconds)", "lv": "Pull intervāls (sekundēs)"},
    "settings.save_auto_sync_settings": {"en": "💾 Save Automatic Sync settings", "lv": "💾 Saglabāt automātiskās sinhronizācijas iestatījumus"},
    "settings.scheduled_sync_jobs": {"en": "Scheduled sync jobs", "lv": "Plānotie sinhronizācijas uzdevumi"},
    "settings.no_scheduled_jobs": {"en": "No scheduled jobs yet.", "lv": "Vēl nav plānotu uzdevumu."},

    "settings.field_sync_history": {"en": "Field sync history", "lv": "Lauku sinhronizācijas vēsture"},
    "settings.no_field_sync_history": {"en": "No field-level sync history yet — populated once real two-way sync detects a change.", "lv": "Vēl nav lauku sinhronizācijas vēstures — tā parādīsies, tiklīdz reālā divvirzienu sinhronizācija konstatēs izmaiņas."},
    "settings.from_source": {"en": "from {source}, {direction}", "lv": "no {source}, {direction}"},
    "settings.pending_conflicts": {"en": "Pending conflicts", "lv": "Neatrisinātie konflikti"},
    "settings.no_pending_conflicts": {"en": "No pending conflicts.", "lv": "Nav neatrisinātu konfliktu."},
    "settings.coming_soon": {"en": "🔒 Coming Soon", "lv": "🔒 Drīzumā"},
    "settings.connected_check": {"en": "✅ Connected", "lv": "✅ Savienots"},
    "settings.edit_word": {"en": "Edit", "lv": "Rediģēt"},
    "settings.connect_word": {"en": "Connect", "lv": "Savienot"},
    "settings.disconnect_word": {"en": "Disconnect", "lv": "Atvienot"},
    "settings.category_all": {"en": "All", "lv": "Visas"},
    "settings.category_marketplace": {"en": "Marketplace", "lv": "Tirdzniecības vieta"},
    "settings.category_store": {"en": "Store", "lv": "Veikals"},
    "settings.category_shipping": {"en": "Shipping", "lv": "Piegāde"},
    "settings.category_accounting": {"en": "Accounting", "lv": "Grāmatvedība"},
    "settings.category_payments": {"en": "Payments", "lv": "Maksājumi"},
    "settings.category_communication": {"en": "Communication", "lv": "Komunikācija"},
    "settings.category_other": {"en": "Other", "lv": "Cits"},
    "settings.add_integration_title": {"en": "Add Integration", "lv": "Pievienot integrāciju"},
    "settings.search_integrations": {"en": "🔍 Search integrations...", "lv": "🔍 Meklēt integrācijas..."},
    "settings.category_label": {"en": "Category", "lv": "Kategorija"},
    "settings.no_integrations_match": {"en": "No integrations match your search.", "lv": "Nav integrāciju, kas atbilst meklējumam."},
    "settings.back_to_integrations": {"en": "← Back to Integrations", "lv": "← Atpakaļ uz integrācijām"},
    "settings.not_connected_yet": {"en": "⚪ Not connected yet", "lv": "⚪ Vēl nav savienots"},
    "settings.tab_general": {"en": "General", "lv": "Vispārīgi"},
    "settings.tab_synchronization": {"en": "Synchronization", "lv": "Sinhronizācija"},
    "settings.tab_field_mapping": {"en": "Field Mapping", "lv": "Lauku sasaiste"},
    "settings.tab_sync_ownership": {"en": "Sync Ownership", "lv": "Sinhronizācijas piederība"},
    "settings.tab_automation": {"en": "Automation", "lv": "Automatizācija"},
    "settings.tab_logs": {"en": "Logs", "lv": "Žurnāli"},
    "settings.connect_marketplaces_caption": {"en": "Connect marketplaces and external services for {company}.", "lv": "Savieno tirdzniecības vietas un ārējos servisus uzņēmumam {company}."},
    "settings.add_integration": {"en": "➕ Add Integration", "lv": "➕ Pievienot integrāciju"},
    "settings.no_integrations_connected": {"en": "No integrations connected yet", "lv": "Vēl nav savienotu integrāciju"},
    "settings.no_integrations_connected_caption": {
        "en": "Connect a marketplace or service to start syncing products automatically.",
        "lv": "Savieno tirdzniecības vietu vai servisu, lai sāktu automātiski sinhronizēt produktus.",
    },
    "settings.last_synced": {"en": "Last synced: {ts}", "lv": "Pēdējoreiz sinhronizēts: {ts}"},

    # ---- Companies (Super Admin) ----
    "companies.super_admins_only": {"en": "Super Admins only.", "lv": "Tikai Super Admin lietotājiem."},
    "companies.page_caption": {
        "en": "Company metadata only — this page never shows another company's products, inventory, or business data.",
        "lv": "Tikai uzņēmuma metadati — šī lapa nekad nerāda cita uzņēmuma produktus, inventāru vai biznesa datus.",
    },
    "companies.company_line": {
        "en": "**{name}**  ·  plan: {plan}  ·  users: {users}  ·  products: {products}  ·  created: {created}",
        "lv": "**{name}**  ·  plāns: {plan}  ·  lietotāji: {users}  ·  produkti: {products}  ·  izveidots: {created}",
    },
    "companies.pending_approval": {"en": "🟡 Pending approval", "lv": "🟡 Gaida apstiprinājumu"},
    "companies.no_admin_found": {"en": "(no admin found)", "lv": "(administrators nav atrasts)"},
    "companies.admin_label": {"en": "Admin: {admin}", "lv": "Administrators: {admin}"},
    "companies.approve": {"en": "✅ Approve", "lv": "✅ Apstiprināt"},
    "companies.reject": {"en": "❌ Reject", "lv": "❌ Noraidīt"},
    "companies.active_companies": {"en": "🟢 Active companies", "lv": "🟢 Aktīvie uzņēmumi"},
    "companies.none": {"en": "None.", "lv": "Nav."},
    "companies.suspend": {"en": "⏸ Suspend", "lv": "⏸ Apturēt"},
    "companies.suspended_companies": {"en": "🔴 Suspended companies", "lv": "🔴 Apturētie uzņēmumi"},
    "companies.reactivate": {"en": "▶ Reactivate", "lv": "▶ Atjaunot"},
    "companies.platform_admins": {"en": "Platform Admins", "lv": "Platformas administratori"},
    "companies.missing_user": {"en": "(missing user {user_id})", "lv": "(trūkst lietotāja {user_id})"},
    "companies.active_word": {"en": "active", "lv": "aktīvs"},
    "companies.inactive_word": {"en": "inactive", "lv": "neaktīvs"},
    "companies.disable": {"en": "Disable", "lv": "Deaktivizēt"},
    "companies.cant_disable_only_admin": {"en": "Can't disable the only active Super Admin.", "lv": "Nevar deaktivizēt vienīgo aktīvo Super Admin."},
    "companies.enable": {"en": "Enable", "lv": "Aktivizēt"},
    "companies.grant_super_admin": {"en": "Grant Super Admin", "lv": "Piešķirt Super Admin"},
    "companies.grant_super_admin_caption": {
        "en": "The user must already have a regular account in some company — this never creates a new user.",
        "lv": "Lietotājam jau jābūt parastam kontam kādā uzņēmumā — šis nekad neizveido jaunu lietotāju.",
    },
    "companies.existing_user_email": {"en": "Existing user's email", "lv": "Esošā lietotāja e-pasts"},
    "companies.no_user_found": {"en": "No existing user found with that email.", "lv": "Ar šo e-pastu lietotājs nav atrasts."},
    "companies.multiple_accounts_warning": {
        "en": "Multiple accounts share this email across companies — granting to company {company_id!r}.",
        "lv": "Vairāki konti dažādos uzņēmumos koplieto šo e-pastu — piešķir uzņēmumam {company_id!r}.",
    },
    "companies.already_super_admin": {"en": "{email} is already an active Super Admin.", "lv": "{email} jau ir aktīvs Super Admin."},
    "companies.reactivated_super_admin": {"en": "Reactivated {email} as a Super Admin.", "lv": "{email} atjaunots kā Super Admin."},
    "companies.now_super_admin": {"en": "{email} is now a Super Admin.", "lv": "{email} tagad ir Super Admin."},

    "settings.importing_progress": {"en": "Importing… {imported}/{total} done, {skipped} skipped, {error_count} error(s).", "lv": "Importē… {imported}/{total} pabeigts, {skipped} izlaisti, {error_count} kļūda(-s)."},
    "new_item.photo_processing_failed": {"en": "Photo processing failed: {error}", "lv": "Fotogrāfijas apstrāde neizdevās: {error}"},

    # ---- roles (display only — never the underlying role value) ----
    "role.admin": {"en": "admin", "lv": "administrators"},
    "role.employee": {"en": "employee", "lv": "darbinieks"},
    "role.reviewer": {"en": "reviewer", "lv": "pārbaudītājs"},

    # ---- top bar / language selector ----
    "topbar.language_label": {"en": "🌐 Language", "lv": "🌐 Valoda"},

    # ---- sidebar nav ----
    "nav.new_item": {"en": "🆕 New Item", "lv": "🆕 Jauns produkts"},
    "nav.import_manifest": {"en": "📥 Import Manifest", "lv": "📥 Importēt manifestu"},
    "nav.product_list": {"en": "🗂️ Product List", "lv": "🗂️ Produktu saraksts"},
    "nav.orders": {"en": "📦 Orders", "lv": "📦 Pasūtījumi"},
    "nav.manage_users": {"en": "👥 Manage Users", "lv": "👥 Pārvaldīt lietotājus"},
    "nav.settings": {"en": "⚙️ Settings", "lv": "⚙️ Iestatījumi"},
    "nav.companies": {"en": "🏢 Companies", "lv": "🏢 Uzņēmumi"},
    "nav.navigate_label": {"en": "Navigate", "lv": "Navigācija"},
    "sidebar.ai_key_missing": {
        "en": "ANTHROPIC_API_KEY is not set — spec structuring, vision grading and "
              "description generation will be unavailable until you add it (see README).",
        "lv": "ANTHROPIC_API_KEY nav iestatīts — specifikāciju noteikšana, vizuālā "
              "vērtēšana un aprakstu ģenerēšana nebūs pieejama, kamēr to nepievienosi "
              "(skatīt README).",
    },
}


def t(key: str, lang: str, **kwargs) -> str:
    """Falls back to English, then to the raw key itself, if a translation
    or the key is missing — never raises, never shows a blank label."""
    entry = TRANSLATIONS.get(key, {})
    template = entry.get(lang) or entry.get(DEFAULT_LANGUAGE) or key
    return template.format(**kwargs) if kwargs else template
