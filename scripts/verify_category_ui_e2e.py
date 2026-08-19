"""UI-level end-to-end check of the category auto-promotion feature
(modules/category_store.py's promote_from_manifest_on_completion(), and
the company-level "Automatically save categories from completed products"
setting) — driven through the REAL Streamlit app via Playwright, not just
the underlying store functions (see scripts/verify_category_store.py for
the module-level equivalent of most of these same checks).

Fully self-contained and safe to re-run any number of times:
  - runs against a throwaway scratch PostgreSQL database (same
    scripts/_pg_test_helper.make_scratch_database convention as every
    other verify_*.py script) — never touches real company data;
  - creates its own company, admin user, draft products, and a synthetic
    test photo entirely in Python — no manifest file, fixture, or any
    other manually-prepared input is required;
  - launches its own `streamlit run app.py` subprocess on a free port
    (picked dynamically, no fixed-port collision risk) and tears it down
    in a finally: block, alongside dropping the scratch database.

Prerequisites (nothing else): a local Postgres reachable the same way
every other verify_*.py script expects (see scripts/_pg_test_helper.py /
ELECTROGRADER_PG_ADMIN_URL), and Playwright's Chromium browser installed
once via `playwright install chromium`.

Scenarios covered (see the approved category-catalog redesign):
  1. Setting OFF + manifest_subcategory -> completed (wizard Save Item)
     -> category is NOT created.
  2. Setting ON + manifest_subcategory -> completed (wizard Save Item)
     -> category IS created and linked.
  3. Setting ON + a MANUALLY picked category -> completed -> the manual
     pick wins; the manifest_subcategory text is never touched.
  4. The bulk "Change Status" -> completed dialog produces an identical
     result to the wizard's Save Item path (same underlying function).
  5. Blank manifest_subcategory -> nothing is created, even with the
     setting ON.
  6. A second product sharing the same manifest_subcategory text reuses
     the existing category — no duplicate — including across a
     completed -> in_progress -> completed repeat cycle.
  7. Turning the setting back OFF afterward does NOT delete any
     already-created categories.

    python scripts/verify_category_ui_e2e.py
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
    pass  # older Python / non-reconfigurable stream — screenshots/labels below avoid emoji in printed text anyway

from scripts._pg_test_helper import make_scratch_database  # noqa: E402

DATABASE_URL, drop_scratch_db = make_scratch_database("category_ui_e2e")
os.environ["ELECTROGRADER_DATABASE_URL"] = DATABASE_URL
os.environ.setdefault("ELECTROGRADER_ENCRYPTION_KEY", "kQ8h9ZqF3v1n7yB2xW6tR4mL0sD5cE8pJ9uK1oI3aF0=")

from modules import auth, category_store, company_store, inventory_store  # noqa: E402
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


ADMIN_EMAIL = "e2e-admin@test.local"
ADMIN_PASSWORD = "TestPassword123!"


def seed_test_data():
    """Everything this script needs, generated fresh in Python — no
    manifest file, fixture, or other manually-prepared input required."""
    company = company_store.create_company("UI E2E Test Co", user_limit=10)
    auth.register_user(
        company_id=company.id, name="E2E Admin", email=ADMIN_EMAIL, password=ADMIN_PASSWORD, role=auth.ROLE_ADMIN,
    )

    def seed_draft(sku, manifest_subcategory=""):
        p = Product(
            company_id=company.id, status="draft", sku=sku, manifest_import_id="e2e-import",
            manifest_subcategory=manifest_subcategory, manifest_item_description=f"Item {sku}",
        )
        inventory_store.save_product(p)
        return p

    products = {
        "a": seed_draft("E2E-A", "Test Cat Alpha"),          # 1: setting OFF
        "b": seed_draft("E2E-B", "Test Cat Beta"),           # 2: setting ON, auto-create
        "c": seed_draft("E2E-C", "Test Cat Gamma Ignored"),  # 3: manual pick wins
        "d": seed_draft("E2E-D", "Test Cat Delta"),          # 4: bulk change status
        "e": seed_draft("E2E-E", ""),                        # 5: blank subcategory
        "f": seed_draft("E2E-F", "Test Cat Beta"),           # 6: reuse, no duplicate
    }
    return company, products


def make_test_photo(tmp_dir: str) -> str:
    from PIL import Image

    path = os.path.join(tmp_dir, "e2e_test_photo.jpg")
    Image.new("RGB", (800, 600), color=(120, 160, 200)).save(path, "JPEG")
    return path


# --------------------------------------------------------- UI automation --
# A handful of Streamlit widgets in this app version render their native
# input (radio/checkbox) visually hidden off-screen for accessibility (a
# react-aria pattern) with a styled sibling as the real visual affordance
# — a plain Playwright .click() can't reach those (they report "outside
# the viewport" even after scrolling), so those specific ones dispatch a
# synthetic "click" DOM event instead. Comboboxes/buttons/text inputs are
# unaffected and use plain .click()/.fill().

def go_to_page(page, label: str) -> None:
    # Substring match (not exact=True): the sidebar's accessible names
    # carry an emoji prefix from i18n.py (e.g. "🆕 New Item") that's
    # fragile to reproduce exactly across encodings — a plain substring
    # like "New Item" is unambiguous against the small, fixed nav list.
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


def do_step3_category_and_next(page, pick_category_label=None) -> None:
    if pick_category_label:
        combo = page.get_by_role("combobox", name="Category", exact=True)
        combo.click()
        page.wait_for_timeout(300)
        page.get_by_role("option", name=pick_category_label, exact=True).click()
        page.wait_for_timeout(300)
    page.get_by_role("button", name="Next", exact=False).click()
    page.wait_for_timeout(800)


def click_next(page) -> None:
    page.get_by_role("button", name="Next", exact=False).click()
    page.wait_for_timeout(800)


def do_step6_save(page) -> None:
    page.get_by_role("button", name="Save item", exact=False).click()
    page.wait_for_timeout(1500)


def run_wizard_for(page, photo_path: str, sku: str, pick_category_label=None) -> None:
    go_to_page(page, "New Item")
    pick_manifest_item_and_advance_to_step2(page, sku)
    do_step2_photo_and_next(page, photo_path)
    do_step3_category_and_next(page, pick_category_label=pick_category_label)
    click_next(page)  # step 4 -> 5
    click_next(page)  # step 5 -> 6
    do_step6_save(page)


def set_auto_save_setting(page, enabled: bool) -> None:
    go_to_page(page, "Settings")
    page.get_by_role("tab", name="Categories", exact=False).click()
    page.wait_for_timeout(500)
    box = page.get_by_role("checkbox", name="Automatically save categories from completed products")
    if box.is_checked() != enabled:
        box.dispatch_event("click")
    page.get_by_role("tabpanel", name="Categories", exact=False).get_by_role("button", name="Save", exact=True).click()
    page.wait_for_timeout(800)


def set_status_filter_all(page) -> None:
    combo = page.get_by_role("combobox", name="Status", exact=True)
    if not combo.is_visible():
        page.get_by_text("Filter Products", exact=False).click()
        page.wait_for_timeout(400)
    if combo.input_value() == "All (incl. drafts)":
        return
    combo.click()
    page.wait_for_timeout(300)
    page.get_by_role("option", name="All (incl. drafts)", exact=True).click()
    page.wait_for_timeout(800)


def select_product_row(page, product_id: str) -> None:
    grid = page.frame_locator('iframe[title="modules.review_table_component.review_table"]')
    checkbox = grid.locator(f'[row-id="{product_id}"] input[type="checkbox"]').first
    checkbox.wait_for(state="visible", timeout=10000)
    for attempt in range(5):
        if checkbox.is_checked():
            return
        if attempt % 2 == 0:
            checkbox.click()
        else:
            checkbox.dispatch_event("click")
        page.wait_for_timeout(700)
    print(f"  [warn] checkbox for row-id={product_id} never reported checked after 5 attempts")


def bulk_change_status(page, product_ids, new_status: str = "completed") -> None:
    # Bounce through another page first — forces a fresh grid/iframe
    # instance instead of reusing a possibly-stale one from a previous
    # bulk operation in the same browser session (observed live: reusing
    # the same grid instance across two bulk operations in a row left the
    # selection state stuck at 0).
    go_to_page(page, "New Item")
    page.wait_for_timeout(400)
    go_to_page(page, "Product List")
    page.wait_for_timeout(1200)
    set_status_filter_all(page)
    page.wait_for_timeout(500)

    for pid in product_ids:
        select_product_row(page, pid)
    page.wait_for_timeout(800)

    page.get_by_role("button", name="Operations", exact=False).click()
    page.wait_for_timeout(300)
    page.get_by_role("button", name="Change Status", exact=False).click()
    page.wait_for_timeout(500)
    status_combo = page.get_by_role("combobox", name="New Status", exact=True)
    status_combo.click()
    page.wait_for_timeout(300)
    page.get_by_role("option", name=new_status, exact=True).click()
    page.wait_for_timeout(300)
    page.get_by_role("button", name="Apply Status Change", exact=True).click()
    page.wait_for_timeout(1500)

    # Dismiss the confirmation dialog before the next call — its own
    # "Close" text button, not the dialog's [X] icon (also accessible-
    # named "Close"), hence .last; Escape as a fallback either way.
    try:
        page.get_by_role("dialog").get_by_role("button", name="Close", exact=True).last.click(timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(400)
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    try:
        page.get_by_role("dialog").wait_for(state="hidden", timeout=5000)
    except Exception:
        pass


# ------------------------------------------------------------- scenarios --

def run_scenarios(page, company, products, photo_path: str) -> None:
    page.get_by_role("textbox", name="Email").fill(ADMIN_EMAIL)
    page.get_by_role("textbox", name="Password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Log in", exact=True).click()
    page.wait_for_timeout(1500)

    print("\n-- Scenario 1: setting OFF + manifest_subcategory -> completed -> NOT created --")
    run_wizard_for(page, photo_path, "E2E-A")
    reloaded_a = inventory_store.get_product(products["a"].id, company.id)
    cat_alpha = category_store.find_by_name(company.id, "Test Cat Alpha")
    check("scenario1: product reached completed status", reloaded_a is not None and reloaded_a.status == "completed")
    check("scenario1: no category_id assigned (setting was OFF)", reloaded_a is not None and reloaded_a.category_id == "")
    check("scenario1: 'Test Cat Alpha' was NOT created in the catalog", cat_alpha is None)

    print("\n-- Turning the auto-save setting ON via Settings -> Categories --")
    set_auto_save_setting(page, True)
    check("setting is ON in the DB after toggling via UI", company_store.get_company(company.id).auto_save_categories_from_completed is True)

    print("\n-- Scenario 2: setting ON + manifest_subcategory -> completed -> category CREATED --")
    run_wizard_for(page, photo_path, "E2E-B")
    reloaded_b = inventory_store.get_product(products["b"].id, company.id)
    cat_beta = category_store.find_by_name(company.id, "Test Cat Beta")
    check("scenario2: product reached completed status", reloaded_b is not None and reloaded_b.status == "completed")
    check("scenario2: category_id was assigned", reloaded_b is not None and bool(reloaded_b.category_id))
    check("scenario2: 'Test Cat Beta' now exists in the catalog", cat_beta is not None)
    check(
        "scenario2: product points at the newly-created category",
        cat_beta is not None and reloaded_b.category_id == cat_beta.id,
    )

    print("\n-- Scenario 3: setting ON + MANUALLY picked category -> completed -> manual pick wins --")
    run_wizard_for(page, photo_path, "E2E-C", pick_category_label="Test Cat Beta")
    reloaded_c = inventory_store.get_product(products["c"].id, company.id)
    cat_gamma = category_store.find_by_name(company.id, "Test Cat Gamma Ignored")
    check("scenario3: product reached completed status", reloaded_c is not None and reloaded_c.status == "completed")
    check(
        "scenario3: category_id is the MANUALLY picked one, not derived from manifest_subcategory",
        cat_beta is not None and reloaded_c is not None and reloaded_c.category_id == cat_beta.id,
    )
    check("scenario3: the ignored manifest subcategory was NEVER created", cat_gamma is None)

    print("\n-- Scenario 4: Bulk Change Status -> completed gives an identical result --")
    bulk_change_status(page, [products["d"].id], new_status="completed")
    reloaded_d = inventory_store.get_product(products["d"].id, company.id)
    cat_delta = category_store.find_by_name(company.id, "Test Cat Delta")
    check("scenario4: product reached completed status via bulk dialog", reloaded_d is not None and reloaded_d.status == "completed")
    check("scenario4: category_id was assigned", reloaded_d is not None and bool(reloaded_d.category_id))
    check("scenario4: 'Test Cat Delta' now exists in the catalog", cat_delta is not None)
    check(
        "scenario4: bulk path result matches the wizard path's shape (scenario 2)",
        cat_delta is not None and reloaded_d.category_id == cat_delta.id and reloaded_d.category == cat_delta.name,
    )

    print("\n-- Scenario 5: blank manifest_subcategory -> nothing created, even with setting ON --")
    before_count = len(category_store.list_categories(company.id, include_inactive=True))
    bulk_change_status(page, [products["e"].id], new_status="completed")
    after_count = len(category_store.list_categories(company.id, include_inactive=True))
    reloaded_e = inventory_store.get_product(products["e"].id, company.id)
    check("scenario5: product reached completed status", reloaded_e is not None and reloaded_e.status == "completed")
    check("scenario5: no category_id assigned (blank manifest_subcategory)", reloaded_e is not None and reloaded_e.category_id == "")
    check("scenario5: no new category was created at all", after_count == before_count)

    print("\n-- Scenario 6: another product, same manifest_subcategory -> reuses category, no duplicate --")
    before_count = len(category_store.list_categories(company.id, include_inactive=True))
    bulk_change_status(page, [products["f"].id], new_status="completed")
    after_count = len(category_store.list_categories(company.id, include_inactive=True))
    reloaded_f = inventory_store.get_product(products["f"].id, company.id)
    check("scenario6: product reached completed status", reloaded_f is not None and reloaded_f.status == "completed")
    check(
        "scenario6: reused the SAME 'Test Cat Beta' category as product B, no duplicate",
        cat_beta is not None and reloaded_f is not None and reloaded_f.category_id == cat_beta.id,
    )
    check("scenario6: catalog row count did NOT grow (no duplicate 'Test Cat Beta')", after_count == before_count)

    bulk_change_status(page, [products["f"].id], new_status="in_progress")
    bulk_change_status(page, [products["f"].id], new_status="completed")
    after_repeat_count = len(category_store.list_categories(company.id, include_inactive=True))
    reloaded_f2 = inventory_store.get_product(products["f"].id, company.id)
    check(
        "scenario6b: re-completing the SAME product again keeps the same category_id, no duplicate",
        reloaded_f2 is not None and cat_beta is not None and reloaded_f2.category_id == cat_beta.id,
    )
    check("scenario6b: catalog row count still unchanged after repeat completion", after_repeat_count == after_count)

    print("\n-- Scenario 7: turning the setting back OFF does NOT delete already-created categories --")
    before_off_count = len(category_store.list_categories(company.id, include_inactive=True))
    set_auto_save_setting(page, False)
    after_off_count = len(category_store.list_categories(company.id, include_inactive=True))
    check("scenario7: setting is OFF again in the DB", company_store.get_company(company.id).auto_save_categories_from_completed is False)
    check("scenario7: category row count unchanged by turning the setting off", after_off_count == before_off_count)
    check("scenario7: 'Test Cat Beta' still exists", category_store.find_by_name(company.id, "Test Cat Beta") is not None)
    check("scenario7: 'Test Cat Delta' still exists", category_store.find_by_name(company.id, "Test Cat Delta") is not None)


def main() -> int:
    print(f"Scratch DB: {DATABASE_URL}")
    company, products = seed_test_data()
    print(f"Seeded {len(products)} draft products for company {company.id!r}")

    with tempfile.TemporaryDirectory(prefix="eg_category_ui_e2e_") as tmp_dir:
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
                    run_scenarios(page, company, products, photo_path)
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
