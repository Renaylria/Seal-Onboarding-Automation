#!/usr/bin/env python3
"""
test_slack_reactivate.py -- Reactivate a deactivated Slack member via admin panel.

Confirmed working flow:
  1. Login at sealuw.slack.com
  2. Load app.slack.com SPA to establish session
  3. Admin -> Manage members popup -> sealuw.slack.com/admin
  4. Click Filter, JS-click 'Inactive', close filter
  5. Search for email -> member row shows up
  6. Click '...' action menu -> Activate account -> Save

Usage:
    python execution/test_slack_reactivate.py deactivated@example.com
"""

import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

SLACK_ADMIN_EMAIL    = os.getenv("SLACK_ADMIN_EMAIL", "").strip()
SLACK_ADMIN_PASSWORD = os.getenv("SLACK_ADMIN_PASSWORD", "").strip()
SLACK_WORKSPACE      = "sealuw.slack.com"

TMP = Path(__file__).resolve().parent.parent / ".tmp"
TMP.mkdir(exist_ok=True)

TARGET_EMAIL = sys.argv[1] if len(sys.argv) > 1 else ""

print("Admin email : " + SLACK_ADMIN_EMAIL)
print("Password    : " + ("*" * len(SLACK_ADMIN_PASSWORD)))
print("Target email: " + TARGET_EMAIL)
print()

def snap(page, name):
    page.screenshot(path=str(TMP / name), full_page=True)
    print("  [screenshot] " + name)

def page_summary(page):
    buttons = [b.inner_text().strip() for b in page.query_selector_all("button")
               if b.inner_text().strip()][:10]
    inputs  = [(i.get_attribute("data-qa") or i.get_attribute("placeholder")
                or i.get_attribute("type") or "?")
               for i in page.query_selector_all("input")][:6]
    print("  Buttons : " + str(buttons))
    print("  Inputs  : " + str(inputs))

def js_click_by_text(page, text):
    """JS-click the first element whose exact text matches, anywhere in DOM."""
    result = page.evaluate("""
        (targetText) => {
            const all = Array.from(document.querySelectorAll('*'));
            const el = all.find(e =>
                e.childElementCount === 0 && e.textContent.trim() === targetText
            );
            if (el) { el.click(); return el.tagName + ' ' + (el.className || ''); }
            return null;
        }
    """, text)
    return result


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()

    # ── Step 1: Login ─────────────────────────────────────────────────────────
    print("Step 1: Logging in...")
    page.goto(f"https://{SLACK_WORKSPACE}/sign_in_with_password")
    page.wait_for_load_state("networkidle")
    page.fill('input[data-qa="login_email"]', SLACK_ADMIN_EMAIL)
    page.click('input[type="password"]')
    page.keyboard.type(SLACK_ADMIN_PASSWORD, delay=50)
    btn = (page.query_selector('button[data-qa="signin_button"]')
           or page.query_selector('button[type="submit"]'))
    btn.click()
    page.wait_for_load_state("networkidle", timeout=30_000)
    time.sleep(4)
    if "sign_in" in page.url:
        print("  ERROR: Login failed")
        sys.exit(1)
    print("  LOGIN OK: " + page.url)

    # ── Step 2: Load workspace SPA ────────────────────────────────────────────
    print("Step 2: Loading workspace SPA...")
    page.goto(f"https://{SLACK_WORKSPACE}/", wait_until="load", timeout=30_000)
    time.sleep(10)
    print("  URL: " + page.url)

    # ── Step 3: Open Admin -> Manage members (new tab) ─────────────────────────
    print("Step 3: Opening Manage members popup...")
    for btn in page.query_selector_all("button"):
        if btn.inner_text().strip() == "Admin":
            btn.click()
            break
    time.sleep(2)

    manage_link = None
    for el in page.query_selector_all("a"):
        if "Manage members" in el.inner_text().strip():
            manage_link = el
            break
    if not manage_link:
        print("  ERROR: Manage members link not found")
        sys.exit(1)

    admin_href = manage_link.get_attribute("href") or f"https://{SLACK_WORKSPACE}/admin"
    try:
        with context.expect_page(timeout=6000) as popup_info:
            manage_link.click()
        admin_page = popup_info.value
        admin_page.wait_for_load_state("load", timeout=30_000)
        time.sleep(8)
        print("  Popup URL: " + admin_page.url)
    except PWTimeout:
        print("  No popup -- navigating directly to " + admin_href)
        admin_page = page
        page.goto(admin_href, wait_until="load", timeout=30_000)
        time.sleep(8)
        print("  URL: " + admin_page.url)

    snap(admin_page, "test_03_admin_members.png")
    page_summary(admin_page)

    if not TARGET_EMAIL:
        print("\nNo target email -- stopping here.")
        browser.close()
        sys.exit(0)

    # ── Step 4: Apply Inactive filter via JS-click ────────────────────────────
    print("Step 4: Applying 'Inactive' billing status filter...")

    # Click the Filter button to open the filter panel
    filter_btn = None
    for btn in admin_page.query_selector_all("button"):
        if btn.inner_text().strip() == "Filter":
            filter_btn = btn
            break
    if filter_btn:
        filter_btn.click()
        time.sleep(2)

        # JS-click the Inactive option (bypasses ReactModal intercept issues)
        result = admin_page.evaluate("""
            () => {
                // Find the Inactive checkbox in the filter popover
                // It's either a checkbox or label with text 'Inactive'
                const popover = document.querySelector(
                    '.c-data_table_header_multi_filter__pop_over_body, ' +
                    '[class*="multi_filter"], .c-popover'
                );
                const container = popover || document;
                const inputs = Array.from(container.querySelectorAll('input[type="checkbox"]'));
                // Try to find by associated label text
                for (const inp of inputs) {
                    const label = document.querySelector('label[for="' + inp.id + '"]');
                    const txt = label ? label.textContent.trim() : '';
                    if (txt === 'Inactive') {
                        inp.click();
                        return 'clicked checkbox id=' + inp.id;
                    }
                }
                // Fallback: find any element with exact text 'Inactive'
                const all = Array.from(document.querySelectorAll('label, span, div'));
                const el = all.find(e =>
                    e.childElementCount === 0 && e.textContent.trim() === 'Inactive'
                );
                if (el) { el.click(); return 'clicked element: ' + el.tagName; }
                return null;
            }
        """)
        print("  JS click result: " + str(result))
        time.sleep(2)

        snap(admin_page, "test_04a_filter_inactive.png")

        # Close filter by clicking Filter button again (toggle) or press Escape
        admin_page.keyboard.press("Escape")
        time.sleep(1)
        snap(admin_page, "test_04b_filter_closed.png")
        page_summary(admin_page)
    else:
        print("  Filter button not found -- skipping inactive filter")

    # ── Step 5: Search for target email ──────────────────────────────────────
    print("Step 5: Searching for " + TARGET_EMAIL + "...")
    search = (
        admin_page.query_selector('input[data-qa="workspace-members__table-header-search_input"]')
        or admin_page.query_selector('input[placeholder*="ilter by name"]')
        or admin_page.query_selector('input[type="search"]')
        or admin_page.query_selector('input[type="text"]')
    )
    if search:
        search.fill(TARGET_EMAIL)
        time.sleep(5)
    else:
        print("  WARNING: No search input found")

    snap(admin_page, "test_05_search_results.png")
    page_summary(admin_page)

    # ── Step 6: Click the '...' action menu on the member row ─────────────────
    # The action button data-qa is "table_row_actions_button"
    # aria-label is "Actions for <member name>"
    print("Step 6: Clicking '...' action button on member row...")
    snap(admin_page, "test_06a_before_dots.png")

    three_dot = (
        admin_page.query_selector('[data-qa="table_row_actions_button"]')
        or admin_page.query_selector('[aria-label*="Actions for"]')
    )
    if three_dot:
        aria = three_dot.get_attribute("aria-label") or ""
        print("  Found action button: " + aria)
        three_dot.click()
        time.sleep(2)
        snap(admin_page, "test_06b_action_menu.png")

        # Collect all menu items
        menu_items = [(el.inner_text().strip(), el.get_attribute("data-qa") or "")
                      for el in admin_page.query_selector_all(
                          "button, [role='menuitem'], [role='option'], li"
                      ) if el.inner_text().strip()]
        print("  Menu items: " + str(menu_items[:15]))

        # Find Activate/Reactivate button — Slack uses "Activate account" in this menu
        reactivate_btn = admin_page.query_selector('[data-qa="reactivate_member_button"]')
        if not reactivate_btn:
            for el in admin_page.query_selector_all("button, [role='menuitem'], li"):
                txt = el.inner_text().strip().lower()
                if "reactivate" in txt or "activate account" in txt or txt == "activate":
                    reactivate_btn = el
                    print("  Found activate button: " + repr(el.inner_text().strip()))
                    break
    else:
        print("  ERROR: '...' action button not found")
        all_btns = [(b.get_attribute("data-qa") or "", b.get_attribute("aria-label") or "", b.inner_text().strip()[:30])
                    for b in admin_page.query_selector_all("button")]
        print("  All buttons: " + str(all_btns[:20]))
        reactivate_btn = None

    if reactivate_btn:
        print("  Clicking 'Activate account'...")
        reactivate_btn.click()
        time.sleep(2)
        snap(admin_page, "test_07_after_reactivate.png")

        # ── Step 7: Handle the "Activate account" confirmation modal ──────────
        # Slack shows a modal with:
        #   - Radio button: "Regular Member" (must be selected)
        #   - Buttons: "Cancel" and "Save"
        # The confirm button text is "Save" — NOT "Activate", "Confirm", or "Yes".
        print("Step 7: Handling confirmation modal...")

        # Select the "Regular Member" radio button (may be unselected by default)
        radio = admin_page.query_selector('input[type="radio"]')
        if radio:
            radio.click()
            time.sleep(0.5)
            print("  Selected Regular Member radio")
        else:
            print("  No radio button found -- skipping")

        # Find and click the "Save" button
        save_btn = None
        # Try data-qa first
        save_btn = (
            admin_page.query_selector('[data-qa="activate_confirm_button"]')
            or admin_page.query_selector('[data-qa="reactivate_confirm_button"]')
            or admin_page.query_selector('[data-qa="save_button"]')
        )
        if not save_btn:
            for btn in admin_page.query_selector_all("button"):
                txt = btn.inner_text().strip().lower()
                if txt == "save":
                    save_btn = btn
                    break
        if not save_btn:
            # Broader fallback
            for btn in admin_page.query_selector_all("button"):
                txt = btn.inner_text().strip().lower()
                if txt in ("save", "confirm", "activate", "reactivate", "yes"):
                    save_btn = btn
                    break

        if save_btn:
            print("  Found Save button: " + repr(save_btn.inner_text().strip()))
            save_btn.click()
            time.sleep(2)
            snap(admin_page, "test_08_confirmed.png")
            print("\n  SUCCESS: " + TARGET_EMAIL + " reactivated!")
        else:
            snap(admin_page, "test_07b_no_save_btn.png")
            print("\n  ERROR: 'Save' button not found in confirmation modal.")
            print("  Check test_07_after_reactivate.png and test_07b_no_save_btn.png")
    else:
        print("\n  Reactivate button NOT found. Check screenshots in .tmp/")

    browser.close()
    print("Done.")
