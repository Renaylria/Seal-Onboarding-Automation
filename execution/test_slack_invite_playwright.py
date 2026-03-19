#!/usr/bin/env python3
"""
test_slack_invite_playwright.py -- Explore (and optionally execute) the
Slack admin panel 'Invite People' flow.

Usage:
    python execution/test_slack_invite_playwright.py          # explore only
    python execution/test_slack_invite_playwright.py email@example.com  # actually send

With no argument the script opens the invite form, dumps its elements, takes
screenshots, and exits WITHOUT clicking Send -- safe to run at any time.
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
DRY_RUN = not TARGET_EMAIL

print("Admin email : " + SLACK_ADMIN_EMAIL)
print("Target email: " + (TARGET_EMAIL or "(none -- dry run, will not send)"))
print()


def snap(page, name):
    page.screenshot(path=str(TMP / name), full_page=True)
    print("  [screenshot] " + name)


def dump_elements(page, label):
    buttons = [(b.inner_text().strip()[:40], b.get_attribute("data-qa") or "")
               for b in page.query_selector_all("button") if b.inner_text().strip()]
    inputs  = [(i.get_attribute("type") or "?",
                i.get_attribute("placeholder") or i.get_attribute("data-qa") or "?",
                i.get_attribute("name") or "")
               for i in page.query_selector_all("input")]
    print(f"  [{label}] Buttons: " + str(buttons[:15]))
    print(f"  [{label}] Inputs : " + str(inputs[:10]))


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
    print("  OK: " + page.url)

    # ── Step 2: Load workspace SPA ────────────────────────────────────────────
    print("Step 2: Loading workspace SPA...")
    page.goto(f"https://{SLACK_WORKSPACE}/", wait_until="load", timeout=30_000)
    time.sleep(10)
    print("  URL: " + page.url)

    # ── Step 3: Open Admin -> Manage members (new tab) ─────────────────────────
    print("Step 3: Opening admin panel popup...")
    for b in page.query_selector_all("button"):
        if b.inner_text().strip() == "Admin":
            b.click()
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

    try:
        with context.expect_page(timeout=6000) as popup_info:
            manage_link.click()
        admin_page = popup_info.value
        admin_page.wait_for_load_state("load", timeout=30_000)
        time.sleep(8)
        print("  Popup URL: " + admin_page.url)
    except PWTimeout:
        print("  No popup -- navigating directly")
        admin_page = page
        page.goto(f"https://{SLACK_WORKSPACE}/admin", wait_until="load", timeout=30_000)
        time.sleep(8)

    snap(admin_page, "invite_01_admin_members.png")
    dump_elements(admin_page, "admin page")

    # ── Step 4: Click "Invite People" ─────────────────────────────────────────
    print("Step 4: Clicking 'Invite People'...")
    invite_btn = None
    for b in admin_page.query_selector_all("button"):
        if "invite people" in b.inner_text().strip().lower():
            invite_btn = b
            break

    if not invite_btn:
        print("  ERROR: 'Invite People' button not found")
        snap(admin_page, "invite_01b_no_button.png")
        sys.exit(1)

    print("  Found: " + repr(invite_btn.inner_text().strip()))
    invite_btn.click()
    time.sleep(3)
    snap(admin_page, "invite_02_invite_dialog.png")
    dump_elements(admin_page, "invite dialog")

    # ── Step 5: Inspect the form ───────────────────────────────────────────────
    print("Step 5: Inspecting invite form elements...")
    # Standard inputs
    for el in admin_page.query_selector_all("input, textarea"):
        t  = el.get_attribute("type") or "text"
        ph = el.get_attribute("placeholder") or ""
        dq = el.get_attribute("data-qa") or ""
        print(f"  input  type={t!r}  placeholder={ph!r}  data-qa={dq!r}")

    # Contenteditable / textbox elements (Slack often uses these for multi-email fields)
    for el in admin_page.query_selector_all('[contenteditable="true"], [role="textbox"]'):
        ph  = el.get_attribute("placeholder") or el.get_attribute("aria-label") or ""
        dq  = el.get_attribute("data-qa") or ""
        tag = el.evaluate("e => e.tagName")
        print(f"  contenteditable  tag={tag}  placeholder={ph!r}  data-qa={dq!r}")

    # All buttons
    for b in admin_page.query_selector_all("button"):
        txt = b.inner_text().strip()
        dq  = b.get_attribute("data-qa") or ""
        if txt:
            print(f"  button  text={txt!r}  data-qa={dq!r}")

    if DRY_RUN:
        print("\nDry run -- stopping before Send. Check invite_02_invite_dialog.png")
        browser.close()
        sys.exit(0)

    # ── Step 6: Fill in email and send ────────────────────────────────────────
    print(f"Step 6: Filling in {TARGET_EMAIL} and sending...")
    # The "To:" field is a contenteditable div (data-qa confirmed in dry run)
    email_input = (
        admin_page.query_selector('[data-qa="invite_modal_select-input"]')
        or admin_page.query_selector('[contenteditable="true"]')
        or admin_page.query_selector('[role="textbox"]')
    )
    if not email_input:
        print("  ERROR: no email input found in invite dialog")
        snap(admin_page, "invite_02b_no_input.png")
        browser.close()
        sys.exit(1)

    # contenteditable divs don't support fill() -- click then type
    email_input.click()
    time.sleep(0.5)
    admin_page.keyboard.type(TARGET_EMAIL, delay=30)
    # Press Enter/Tab to confirm the email chip (Slack may require this)
    admin_page.keyboard.press("Tab")
    time.sleep(1)
    snap(admin_page, "invite_03_email_filled.png")

    # Find Send button
    send_btn = None
    for b in admin_page.query_selector_all("button"):
        txt = b.inner_text().strip().lower()
        dq  = b.get_attribute("data-qa") or ""
        if txt == "send":           # exact match — confirmed in dry run
            send_btn = b
            print("  Send button found: data-qa=" + repr(dq))
            break
    if not send_btn:
        # Broader fallback
        for b in admin_page.query_selector_all("button"):
            txt = b.inner_text().strip().lower()
            dq  = b.get_attribute("data-qa") or ""
            if ("send" in txt or "invite" in txt) and "cancel" not in txt:
                send_btn = b
                print("  Send button (fallback): " + repr(b.inner_text().strip()) + "  data-qa=" + repr(dq))
                break

    if not send_btn:
        print("  ERROR: Send/Invite button not found")
        snap(admin_page, "invite_03b_no_send.png")
        browser.close()
        sys.exit(1)

    send_btn.click()
    time.sleep(3)
    snap(admin_page, "invite_04_after_send.png")
    dump_elements(admin_page, "after send")
    print("\nSUCCESS: invite flow completed for " + TARGET_EMAIL)

    browser.close()
    print("Done.")
