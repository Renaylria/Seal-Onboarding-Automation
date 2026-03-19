#!/usr/bin/env python3
"""
test_slack_deactivate.py -- Deactivate an active Slack member via admin panel.

Mirrors test_slack_reactivate.py but in reverse:
  1. Login at sealuw.slack.com
  2. Load app.slack.com SPA to establish session
  3. Admin -> Manage members popup -> sealuw.slack.com/admin
  4. Search for email (no Inactive filter — target is an active member)
  5. Click '...' action menu -> Deactivate account
  6. Confirm the deactivation dialog

Usage:
    python execution/test_slack_deactivate.py active@example.com
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

    snap(admin_page, "deactivate_03_admin_members.png")
    page_summary(admin_page)

    if not TARGET_EMAIL:
        print("\nNo target email -- stopping here (dry run).")
        browser.close()
        sys.exit(0)

    # ── Step 4: Search for target email ──────────────────────────────────────
    # No Inactive filter needed — target is an active member
    print("Step 4: Searching for " + TARGET_EMAIL + "...")
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

    snap(admin_page, "deactivate_04_search_results.png")
    page_summary(admin_page)

    # ── Step 5: Click the '...' action menu on the member row ─────────────────
    print("Step 5: Clicking '...' action button on member row...")
    snap(admin_page, "deactivate_05a_before_dots.png")

    three_dot = (
        admin_page.query_selector('[data-qa="table_row_actions_button"]')
        or admin_page.query_selector('[aria-label*="Actions for"]')
    )
    if three_dot:
        aria = three_dot.get_attribute("aria-label") or ""
        print("  Found action button: " + aria)
        three_dot.click()
        time.sleep(2)
        snap(admin_page, "deactivate_05b_action_menu.png")

        # Collect all menu items for inspection
        menu_items = [(el.inner_text().strip(), el.get_attribute("data-qa") or "")
                      for el in admin_page.query_selector_all(
                          "button, [role='menuitem'], [role='option'], li"
                      ) if el.inner_text().strip()]
        print("  Menu items: " + str(menu_items[:15]))

        # Find Deactivate button in the action dropdown
        deactivate_btn = admin_page.query_selector('[data-qa="deactivate_member_button"]')
        if not deactivate_btn:
            for el in admin_page.query_selector_all("button, [role='menuitem'], li"):
                txt = el.inner_text().strip().lower()
                if "deactivate account" in txt or txt == "deactivate":
                    deactivate_btn = el
                    print("  Found deactivate button: " + repr(el.inner_text().strip()))
                    break
    else:
        print("  ERROR: '...' action button not found")
        all_btns = [(b.get_attribute("data-qa") or "", b.get_attribute("aria-label") or "",
                     b.inner_text().strip()[:30])
                    for b in admin_page.query_selector_all("button")]
        print("  All buttons: " + str(all_btns[:20]))
        deactivate_btn = None

    if deactivate_btn:
        print("  Clicking 'Deactivate account'...")
        deactivate_btn.click()
        time.sleep(2)
        snap(admin_page, "deactivate_06_after_deactivate_click.png")
        page_summary(admin_page)

        # ── Step 6: Handle confirmation modal ─────────────────────────────────
        # Slack shows a confirmation dialog.
        # The confirm button is labelled "Deactivate" — NOT "Save" or "Confirm".
        # (Contrast with the reactivation modal which uses "Save".)
        print("Step 6: Handling confirmation modal...")

        confirm_btn = None
        # Try known data-qa values first
        confirm_btn = (
            admin_page.query_selector('[data-qa="deactivate_confirm_button"]')
            or admin_page.query_selector('[data-qa="confirm_button"]')
        )
        if not confirm_btn:
            for btn in admin_page.query_selector_all("button"):
                txt = btn.inner_text().strip().lower()
                if txt in ("deactivate", "deactivate account", "confirm", "yes", "remove"):
                    confirm_btn = btn
                    print("  Found confirm button: " + repr(btn.inner_text().strip()))
                    break

        if confirm_btn:
            confirm_btn.click()
            time.sleep(2)
            snap(admin_page, "deactivate_07_confirmed.png")
            print("\n  SUCCESS: " + TARGET_EMAIL + " deactivated!")
        else:
            snap(admin_page, "deactivate_06b_no_confirm_btn.png")
            print("\n  ERROR: Confirmation button not found in deactivation modal.")
            print("  Check deactivate_06_after_deactivate_click.png and deactivate_06b_no_confirm_btn.png")
    else:
        print("\n  Deactivate button NOT found. Check screenshots in .tmp/")

    browser.close()
    print("Done.")
