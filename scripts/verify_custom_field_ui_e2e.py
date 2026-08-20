"""UI-level end-to-end check of the self-service Custom Fields feature
(modules/custom_field_store.py + Product.custom_fields +
modules/product_translation_store.py's per-language custom_fields column)
— driven through the REAL Streamlit app via Playwright, not just the
underlying store functions (see scripts/verify_custom_field_store.py for
the module-level equivalent of the CRUD/isolation/payload checks).

Fully self-contained and safe to re-run any number of times:
  - runs against a throwaway scratch PostgreSQL database (same
    scripts/_pg_test_helper.make_scratch_database convention as every
    other verify_*.py script) — never touches real company data;
  - creates its own company, admin user, and a single draft product
    entirely in Python — no manifest file or other manually-prepared
    input is required;
  - launches its own `streamlit run app.py` subprocess on a free port
    and tears it down in a finally: block, alongside dropping the
    scratch database.

Scenarios covered (see the approved Custom Fields plan, and the user's
explicit follow-up requirements: per-company deletion removes the field
from the product card, and values work across every content language,
including ones added in the future):
  1. Create a custom field ("Warranty Months") via Product List ->
     Product List Settings.
  2. It appears as an input on the New Item wizard's step 6 (manual-only
     details) for a brand-new product, and the entered value is saved
     onto the completed product.
  3. It appears (pre-filled with the saved value) on the Review & Export
     product card, in the primary (English) language tab; editing and
     saving there persists the new value.
  4. A second content language (Latvian) has its OWN, independently
     editable value for the same field — editing/saving the Latvian tab
     does not touch the English value, and vice versa (per-language
     storage, not a flat shared value).
  5. Deleting the field via Product List Settings makes it disappear
     from the product card immediately (no other code change).

Prerequisites (nothing else): a local Postgres reachable the same way
every other verify_*.py script expects (see scripts/_pg_test_helper.py /
ELECTROGRADER_PG_ADMIN_URL), and Playwright's Chromium browser installed
once via `playwright install chromium`.

    python scripts/verify_custom_field_ui_e2e.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass  # older Python / non-reconfigurable stream

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

DATABASE_URL, drop_scratch_db = make_scratch_database("custom_field_ui_e2e")
os.environ["ELECTROGRADER_DATABASE_URL"] = DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import auth, company_store, custom_field_store, inventory_store, product_translation_store  # noqa: E402
from modules.models import Product  # noqa: E402

_checks_passed = 0
_failures = []


def check(label: str, condition: bool) -> None:
    global _checks_passed
    if condition:
        _checks_passed += 1
    else:
        _failures.append(label)
    print(("  OK  " if condition else "  FAIL"), label)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


ADMIN_EMAIL = "cf-e2e-admin@test.local"
ADMIN_PASSWORD = "TestPassword123!"


def seed_test_data():
    company = company_store.create_company("Custom Field UI E2E Test Co", user_limit=10)
    auth.register_user(
        company_id=company.id, name="CF E2E Admin", email=ADMIN_EMAIL, password=ADMIN_PASSWORD, role=auth.ROLE_ADMIN,
    )
    product = Product(
        company_id=company.id, status="draft", sku="CF-E2E-A", manifest_import_id="cf-e2e-import",
        manifest_item_description="CF E2E test item", primary_language="en",
    )
    inventory_store.save_product(product)
    return company, product


def make_test_photo(tmp_dir: str) -> str:
    from PIL import Image

    path = os.path.join(tmp_dir, "cf_e2e_test_photo.jpg")
    Image.new("RGB", (800, 600), color=(90, 140, 180)).save(path, "JPEG")
    return path


# --------------------------------------------------------- UI automation --
# See scripts/verify_category_ui_e2e.py's comment block: some Streamlit
# widgets render their native input visually hidden off-screen for
# accessibility, so those use .dispatch_event("click") instead of .click().

def go_to_page(page, label: str) -> None:
    page.get_by_role("radio", name=label).dispatch_event("click")
    page.wait_for_timeout(700)


def pick_manifest_item_and_advance_to_step2(page, sku: str) -> None:
    combo = page.get_by_role("combobox", name="Pick a pending manifest item")
    combo.click()
    page.wait_for_timeout(300)
    page.get_by_role("option", name=f"SKU {sku} ", exact=False).first.click()
    page.wait_for_timeout(300)
    page.get_by_role("button", name="Use this item", exact=False).click()
    page.wait_for_timeout(1000)


def do_step2_photo_and_next(page, photo_path: str) -> None:
    page.locator('input[type="file"]').set_input_files(photo_path)
    next_btn = page.get_by_role("button", name="Next", exact=False)
    for _ in range(30):
        page.wait_for_timeout(500)
        if next_btn.is_enabled():
            break
    next_btn.click()
    page.wait_for_timeout(800)


def click_next(page) -> None:
    page.get_by_role("button", name="Next", exact=False).click()
    page.wait_for_timeout(800)


def go_to_product_list_settings(page) -> None:
    go_to_page(page, "Product List")
    page.get_by_role("tab", name="Product List Settings", exact=False).click()
    page.wait_for_timeout(500)


def create_custom_field(page, label: str) -> None:
    go_to_product_list_settings(page)
    page.get_by_role("textbox", name="Field name", exact=True).fill(label)
    page.get_by_role("button", name="+ Add field", exact=True).click()
    page.wait_for_timeout(800)


def delete_custom_field(page) -> None:
    go_to_product_list_settings(page)
    page.get_by_role("button", name="🗑️", exact=True).first.click()
    page.wait_for_timeout(400)
    page.get_by_role("button", name="🗑️ Delete", exact=True).click()
    page.wait_for_timeout(800)


def open_product_card(page, product_id: str) -> None:
    go_to_page(page, "Product List")
    page.get_by_role("tab", name="Product List", exact=False).first.click()
    page.wait_for_timeout(500)
    grid = page.frame_locator('iframe[title="modules.review_table_component.review_table"]')
    row = grid.locator(f'[row-id="{product_id}"]').first
    row.wait_for(state="visible", timeout=10000)
    row.dblclick()
    page.wait_for_timeout(1000)


def select_content_language(page, label: str) -> None:
    page.get_by_role("radio", name=label, exact=True).dispatch_event("click")
    page.wait_for_timeout(600)


def save_product_card(page) -> None:
    page.get_by_role("button", name="💾 Save", exact=True).click()
    page.wait_for_timeout(1200)


# ------------------------------------------------------------- scenarios --

def run_scenarios(page, company, product, photo_path: str) -> None:
    page.get_by_role("textbox", name="Email").fill(ADMIN_EMAIL)
    page.get_by_role("textbox", name="Password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Log in", exact=True).click()
    page.wait_for_timeout(1500)

    print("\n-- Scenario 1: create a custom field via Product List Settings --")
    create_custom_field(page, "Warranty Months")
    defs = custom_field_store.list_fields(company.id)
    check("scenario1: field was created in the store", any(d.label == "Warranty Months" for d in defs))
    warranty_key = next(d.key for d in defs if d.label == "Warranty Months")
    check("scenario1: field appears listed in the Settings UI", page.get_by_text("Warranty Months", exact=True).first.is_visible())

    print("\n-- Scenario 2: field appears in the New Item wizard and its value is saved --")
    go_to_page(page, "New Item")
    pick_manifest_item_and_advance_to_step2(page, "CF-E2E-A")
    do_step2_photo_and_next(page, photo_path)
    click_next(page)  # step3 category -> step4
    click_next(page)  # step4 -> step5
    click_next(page)  # step5 -> step6
    wizard_input = page.get_by_role("textbox", name="Warranty Months", exact=True)
    check("scenario2: custom field input is present on the wizard's manual-only step", wizard_input.is_visible())
    wizard_input.fill("12 months")
    page.get_by_role("button", name="Save item", exact=False).click()
    page.wait_for_timeout(1500)

    reloaded = inventory_store.get_product(product.id, company.id)
    check("scenario2: product reached completed status", reloaded is not None and reloaded.status == "completed")
    check(
        "scenario2: wizard-entered custom field value was saved onto the product",
        reloaded is not None and reloaded.custom_fields.get(warranty_key) == "12 months",
    )

    print("\n-- Scenario 3: field appears pre-filled on the Review & Export card, editable, saves --")
    open_product_card(page, product.id)
    card_input_en = page.get_by_role("textbox", name="Warranty Months", exact=True)
    check("scenario3: custom field input is present on the product card", card_input_en.is_visible())
    check("scenario3: it's pre-filled with the value saved from the wizard", card_input_en.input_value() == "12 months")
    card_input_en.fill("24 months")
    save_product_card(page)

    reloaded2 = inventory_store.get_product(product.id, company.id)
    check(
        "scenario3: edited value persisted to the product (primary/English content)",
        reloaded2 is not None and reloaded2.custom_fields.get(warranty_key) == "24 months",
    )

    print("\n-- Scenario 4: a second content language has its own, independent value --")
    # Seed the Latvian translation row directly (same convention as
    # scripts/verify_category_ui_e2e.py seeding draft products directly) —
    # equivalent to a human having already translated this product via the
    # existing Translate feature, without needing an OpenAI/DeepL key here.
    product_translation_store.upsert_translation(product_translation_store.ProductTranslation(
        product_id=product.id, company_id=company.id, language="lv",
        title="LV nosaukums", description="LV apraksts",
        custom_fields={warranty_key: "12 mēneši"}, translated_by="ai",
    ))

    open_product_card(page, product.id)
    select_content_language(page, "Latviešu")
    card_input_lv = page.get_by_role("textbox", name="Warranty Months", exact=True)
    check("scenario4: Latvian tab shows its OWN seeded value, not the English one", card_input_lv.input_value() == "12 mēneši")
    card_input_lv.fill("18 mēneši")
    save_product_card(page)

    lv_translation = product_translation_store.get_translation(product.id, "lv")
    check(
        "scenario4: edited Latvian value persisted to the per-language translation row",
        lv_translation is not None and lv_translation.custom_fields.get(warranty_key) == "18 mēneši",
    )
    reloaded3 = inventory_store.get_product(product.id, company.id)
    check(
        "scenario4: editing the Latvian tab did NOT touch the English (primary) value",
        reloaded3 is not None and reloaded3.custom_fields.get(warranty_key) == "24 months",
    )

    print("\n-- Scenario 5: deleting the field removes it from the product card immediately --")
    delete_custom_field(page)
    check("scenario5: field is gone from the store", all(d.label != "Warranty Months" for d in custom_field_store.list_fields(company.id)))

    open_product_card(page, product.id)
    check(
        "scenario5: custom field input no longer appears on the product card",
        page.get_by_role("textbox", name="Warranty Months", exact=True).count() == 0,
    )


def main() -> int:
    print(f"Scratch DB: {DATABASE_URL}")
    company, product = seed_test_data()
    print(f"Seeded 1 draft product for company {company.id!r}")

    with tempfile.TemporaryDirectory(prefix="eg_custom_field_ui_e2e_") as tmp_dir:
        photo_path = make_test_photo(tmp_dir)

        port = _free_port()
        base_url = f"http://localhost:{port}"
        env = os.environ.copy()
        proc = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", "app.py",
             "--server.headless", "true", "--server.port", str(port)],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen(base_url, timeout=1)
                    break
                except Exception:
                    time.sleep(1)
            else:
                raise RuntimeError("Streamlit server never came up")
            print("Streamlit server is up.")

            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch()
                except Exception as e:
                    raise RuntimeError(
                        "Could not launch Chromium — run `playwright install chromium` once, then retry."
                    ) from e
                try:
                    page = browser.new_page(viewport={"width": 1600, "height": 1200})
                    page.set_default_timeout(20000)
                    page.goto(base_url)
                    page.wait_for_load_state("networkidle")
                    run_scenarios(page, company, product, photo_path)
                finally:
                    browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    print(f"\n{_checks_passed} check(s) passed, {len(_failures)} failed.")
    if _failures:
        print("\nFAILED:")
        for f in _failures:
            print(f"  - {f}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    finally:
        drop_scratch_db()
    raise SystemExit(exit_code)
