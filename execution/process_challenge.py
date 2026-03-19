#!/usr/bin/env python3
"""
process_challenge.py — SEAL Applicant Challenge Stage 3 Processing

Monitors the 'Applicants' tab of the SEAL Applicant Challenge sheet for rows
where column P equals "stage 3". For each newly qualifying row:
  1. Copies the row to the 'Added to SEAL Life' tab (same sheet)
  2. Copies the row to the 'Associates' tab of the SEAL Clan Life sheet
  3. Adds the email (column AP) to the seal-active@maxalton.com Workspace group
  4. Handles Slack membership:
       - New members   → sends a workspace invite via users.admin.invite
       - Returning members (deactivated account) → reactivates via API,
         falling back to Playwright GUI automation if the API is unavailable
  5. Deletes the processed row from the Applicants tab

Deduplication: emails already present in 'Added to SEAL Life' are skipped,
ensuring no row is processed more than once across runs.

Requires in .env:
    SLACK_USER_TOKEN     — xoxp- user token with admin, users:read, users:read.email scopes
    SLACK_ADMIN_EMAIL    — admin account email (used by Playwright fallback)
    SLACK_ADMIN_PASSWORD — admin account password (used by Playwright fallback)

Usage:
    python execution/process_challenge.py
"""

import os
import re
import sys
import logging
from pathlib import Path

import requests as http_requests
from dotenv import load_dotenv

load_dotenv()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

import yaml
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from playwright.sync_api import sync_playwright

# ── Slack credentials (from .env) ──────────────────────────────────────────────
SLACK_USER_TOKEN     = os.getenv("SLACK_USER_TOKEN", "").strip()
SLACK_ADMIN_EMAIL    = os.getenv("SLACK_ADMIN_EMAIL", "").strip()
SLACK_ADMIN_PASSWORD = os.getenv("SLACK_ADMIN_PASSWORD", "").strip()
SLACK_WORKSPACE      = "sealuw.slack.com"

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent.parent
CONFIG      = ROOT / "config.yaml"
TOKEN_GMAIL = ROOT / "token_gmail.json"   # sealdirector@gmail.com — sheets
TOKEN_ADMIN = ROOT / "token_admin.json"   # admin@maxalton.com — group management
CREDS       = ROOT / "credentials.json"
TMP              = ROOT / ".tmp"
LOG_FILE         = TMP / "process_challenge.log"

# ── OAuth Scopes ───────────────────────────────────────────────────────────────
SCOPES_GMAIL = ["https://www.googleapis.com/auth/spreadsheets"]
SCOPES_ADMIN = ["https://www.googleapis.com/auth/admin.directory.group.member"]


# ══════════════════════════════════════════════════════════════════════════════
# Auth
# ══════════════════════════════════════════════════════════════════════════════

def get_credentials(scopes: list, token_path: Path, hint: str = "") -> Credentials:
    """Load or refresh OAuth credentials. Opens browser on first run."""
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if hint:
                print(f"\n>>> Sign in as: {hint}\n")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), scopes)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return creds


# ══════════════════════════════════════════════════════════════════════════════
# Sheets helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_sheet_data(svc, spreadsheet_id: str, tab: str) -> list:
    """Return all rows from a tab as a list of lists."""
    result = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'"
    ).execute()
    return result.get("values", [])


def ensure_tab_exists(svc, spreadsheet_id: str, tab: str, header: list | None = None):
    """Create a tab if it doesn't already exist, optionally writing a header row."""
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab not in existing:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]}
        ).execute()
        if header:
            svc.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{tab}'!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [header]}
            ).execute()


def append_rows(svc, spreadsheet_id: str, tab: str, rows: list):
    """Append rows to a tab, inserting new rows below existing content.
    Anchoring to A1 ensures the API always starts from column A regardless
    of how far right existing data extends in the sheet."""
    svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


def write_to_next_blank_row(svc, spreadsheet_id: str, tab: str, rows: list,
                             start_row: int, log) -> int:
    """Write rows to the first blank row in column A at or below start_row (1-indexed).
    Uses update (not append) to avoid overwriting existing rows.
    Returns the 1-indexed row number where writing began."""
    # Read column A to locate the first blank row >= start_row
    result = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A:A"
    ).execute()
    col_a = result.get("values", [])

    next_row = start_row  # default: use start_row if everything is blank
    for i in range(start_row - 1, len(col_a)):
        if not col_a[i] or not str(col_a[i][0]).strip():
            next_row = i + 1   # 1-indexed
            break
    else:
        # All rows from start_row downward have data — append after last
        next_row = max(len(col_a) + 1, start_row)

    svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A{next_row}",
        valueInputOption="USER_ENTERED",
        body={"values": rows}
    ).execute()
    log.info(f"  [Sheets] Wrote {len(rows)} row(s) to '{tab}' starting at row {next_row}")
    return next_row


def copy_formula_down(svc, spreadsheet_id: str, tab: str,
                      source_row: int, dest_start_row: int,
                      num_rows: int, col_idx: int, log):
    """Copy the formula from source_row into dest rows using copyPaste PASTE_FORMULA.
    The Sheets API auto-adjusts all relative cell references to match the new row."""
    sheet_id = get_sheet_id(svc, spreadsheet_id, tab)
    svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"copyPaste": {
            "source": {
                "sheetId": sheet_id,
                "startRowIndex": source_row - 1,      # 0-indexed
                "endRowIndex": source_row,
                "startColumnIndex": col_idx,
                "endColumnIndex": col_idx + 1
            },
            "destination": {
                "sheetId": sheet_id,
                "startRowIndex": dest_start_row - 1,  # 0-indexed
                "endRowIndex": dest_start_row - 1 + num_rows,
                "startColumnIndex": col_idx,
                "endColumnIndex": col_idx + 1
            },
            "pasteType": "PASTE_FORMULA"
        }}]}
    ).execute()
    log.info(f"  [Sheets] Copied column P formula from row {source_row} → rows {dest_start_row}-{dest_start_row + num_rows - 1}")


def get_sheet_id(svc, spreadsheet_id: str, tab: str) -> int:
    """Return the integer sheetId (gid) for a named tab."""
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            return s["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab}' not found in spreadsheet {spreadsheet_id}")


def delete_rows(svc, spreadsheet_id: str, tab: str, row_indices: list, log):
    """Delete rows by 0-based index. Deletes in reverse order to avoid index shifting."""
    sheet_id = get_sheet_id(svc, spreadsheet_id, tab)
    requests = [
        {"deleteDimension": {"range": {
            "sheetId": sheet_id,
            "dimension": "ROWS",
            "startIndex": idx,
            "endIndex": idx + 1
        }}}
        for idx in sorted(row_indices, reverse=True)
    ]
    svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests}
    ).execute()
    log.info(f"  [Sheets] Deleted {len(requests)} row(s) from '{tab}'")


def get_emails_in_tab(svc, spreadsheet_id: str, tab: str, email_col: int) -> set:
    """Return lowercase email set from a specific column in a tab."""
    try:
        data = get_sheet_data(svc, spreadsheet_id, tab)
        return {
            row[email_col].strip().lower()
            for row in data[1:]          # skip header
            if len(row) > email_col and row[email_col].strip()
        }
    except HttpError:
        return set()


# ══════════════════════════════════════════════════════════════════════════════
# Google Group
# ══════════════════════════════════════════════════════════════════════════════

def add_to_google_group(admin_svc, group_email: str, member_email: str, log):
    """Add member_email to a Workspace Google Group via Admin SDK."""
    try:
        admin_svc.members().insert(
            groupKey=group_email,
            body={"email": member_email, "role": "MEMBER"}
        ).execute()
        log.info(f"  [Group] Added: {member_email}")
    except HttpError as e:
        if e.resp.status == 409:
            log.info(f"  [Group] Already a member: {member_email}")
        else:
            log.error(f"  [Group] Failed to add {member_email}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Slack
# ══════════════════════════════════════════════════════════════════════════════

def slack_lookup_user(email: str, log) -> tuple:
    """Look up a Slack user by email.

    First tries users.lookupByEmail (fast, active users only).
    If not found, falls back to paginating users.list which includes
    deactivated accounts (deleted=True).

    Returns (user_id, is_deactivated) if found, or (None, False) if not found
    or if the token is not configured.
    """
    if not SLACK_USER_TOKEN:
        return None, False

    # Fast path — works for active users
    resp = http_requests.get(
        "https://slack.com/api/users.lookupByEmail",
        params={"email": email},
        headers={"Authorization": f"Bearer {SLACK_USER_TOKEN}"},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        user = data["user"]
        return user["id"], user.get("deleted", False)
    if data.get("error") != "users_not_found":
        log.error(f"  [Slack] Lookup error for {email}: {data.get('error')}")
        return None, False

    # Fallback — paginate users.list to catch deactivated accounts
    log.info(f"  [Slack] {email} not found via lookupByEmail — scanning full user list for deactivated account")
    target = email.strip().lower()
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = http_requests.get(
            "https://slack.com/api/users.list",
            params=params,
            headers={"Authorization": f"Bearer {SLACK_USER_TOKEN}"},
            timeout=15,
        )
        data = resp.json()
        if not data.get("ok"):
            log.error(f"  [Slack] users.list error: {data.get('error')}")
            return None, False
        for member in data.get("members", []):
            profile = member.get("profile", {})
            member_email = profile.get("email", "").strip().lower()
            if member_email == target:
                return member["id"], member.get("deleted", False)
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    return None, False


def slack_invite_user(email: str, log) -> bool:
    """Attempt to invite a new user via the unofficial users.admin.invite API.

    NOTE: This endpoint requires the legacy 'client' scope which is not
    available on modern Slack apps (returns missing_scope / needed: client).
    This function is kept for reference but slack_invite_playwright is used
    instead in production.
    """
    resp = http_requests.post(
        "https://slack.com/api/users.admin.invite",
        data={"token": SLACK_USER_TOKEN, "email": email},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        log.info(f"  [Slack] Invite sent to: {email}")
        return True
    err = data.get("error", "unknown")
    if err == "already_in_team":
        log.info(f"  [Slack] {email} is already in the team — no invite needed")
        return True
    log.error(f"  [Slack] Invite failed for {email}: {err}")
    return False


def slack_invite_playwright(email: str, log) -> bool:
    """Invite a new member to the Slack workspace via the admin panel GUI.

    The users.admin.invite API endpoint requires the legacy 'client' scope
    which is not available on modern Slack apps.  This function uses the admin
    panel's 'Invite People' modal instead.

    Confirmed working flow (dry-run verified):
      1. Login at sealuw.slack.com/sign_in_with_password (minimal browser config)
      2. Load workspace SPA (sealuw.slack.com/ -> redirects to app.slack.com)
      3. Open admin popup via context.expect_page()
      4. Click 'Invite People' button
      5. Click data-qa="invite_modal_select-input" (contenteditable div),
         type the email, press Tab to confirm the chip
      6. Click 'Send' (exact text match; button is disabled if email already a member)
    """
    if not SLACK_ADMIN_EMAIL or not SLACK_ADMIN_PASSWORD:
        log.error(
            "  [Slack] SLACK_ADMIN_EMAIL / SLACK_ADMIN_PASSWORD not set "
            "— cannot invite via Playwright"
        )
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            # ── Login (same as reactivate) ─────────────────────────────────────
            page.goto(f"https://{SLACK_WORKSPACE}/sign_in_with_password")
            page.wait_for_load_state("networkidle")
            page.fill('input[data-qa="login_email"]', SLACK_ADMIN_EMAIL)
            page.click('input[type="password"]')
            page.keyboard.type(SLACK_ADMIN_PASSWORD, delay=50)
            sign_in_btn = (
                page.query_selector('button[data-qa="signin_button"]')
                or page.query_selector('button[type="submit"]')
            )
            sign_in_btn.click()
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(4000)

            if "sign_in" in page.url:
                log.error(
                    "  [Slack] Playwright login failed — check "
                    "SLACK_ADMIN_EMAIL / SLACK_ADMIN_PASSWORD in .env"
                )
                browser.close()
                return False

            # ── Load workspace SPA (same as reactivate) ────────────────────────
            page.goto(f"https://{SLACK_WORKSPACE}/", wait_until="load", timeout=30000)
            page.wait_for_timeout(10000)

            # ── Open admin panel popup (same as reactivate) ────────────────────
            for btn in page.query_selector_all("button"):
                if btn.inner_text().strip() == "Admin":
                    btn.click()
                    break
            page.wait_for_timeout(2000)

            manage_link = None
            for el in page.query_selector_all("a"):
                if "Manage members" in el.inner_text().strip():
                    manage_link = el
                    break

            if not manage_link:
                log.error("  [Slack] Playwright could not find Manage members link")
                page.screenshot(
                    path=str(TMP / f"slack_invite_debug_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            admin_href = manage_link.get_attribute("href") or f"https://{SLACK_WORKSPACE}/admin"
            try:
                from playwright.sync_api import TimeoutError as PWTimeout
                with context.expect_page(timeout=6000) as popup_info:
                    manage_link.click()
                admin_page = popup_info.value
                admin_page.wait_for_load_state("load", timeout=30000)
                admin_page.wait_for_timeout(8000)
            except Exception:
                admin_page = page
                page.goto(admin_href, wait_until="load", timeout=30000)
                page.wait_for_timeout(8000)

            # ── Click "Invite People" ──────────────────────────────────────────
            invite_btn = None
            for btn in admin_page.query_selector_all("button"):
                if "invite people" in btn.inner_text().strip().lower():
                    invite_btn = btn
                    break

            if not invite_btn:
                log.error("  [Slack] Playwright could not find Invite People button")
                admin_page.screenshot(
                    path=str(TMP / f"slack_invite_debug_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            invite_btn.click()
            admin_page.wait_for_timeout(2000)

            # ── Fill in the email in the contenteditable "To:" field ───────────
            # data-qa="invite_modal_select-input" confirmed via dry-run inspection
            email_field = (
                admin_page.query_selector('[data-qa="invite_modal_select-input"]')
                or admin_page.query_selector('[contenteditable="true"]')
            )
            if not email_field:
                log.error(
                    f"  [Slack] Playwright could not find email input in invite dialog "
                    f"for {email}"
                )
                admin_page.screenshot(
                    path=str(TMP / f"slack_invite_debug_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            email_field.click()
            admin_page.wait_for_timeout(500)
            admin_page.keyboard.type(email, delay=30)
            admin_page.keyboard.press("Tab")   # confirm the email chip
            admin_page.wait_for_timeout(1500)

            # ── Click Send ─────────────────────────────────────────────────────
            send_btn = None
            for btn in admin_page.query_selector_all("button"):
                if btn.inner_text().strip().lower() == "send":
                    send_btn = btn
                    break

            if not send_btn:
                log.error(f"  [Slack] Playwright could not find Send button for {email}")
                admin_page.screenshot(
                    path=str(TMP / f"slack_invite_debug_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            # A disabled Send means Slack flagged the email (e.g. already a member)
            if send_btn.evaluate("b => b.disabled"):
                log.warning(
                    f"  [Slack] Invite Send button is disabled for {email} "
                    "— possibly already a member or invalid email"
                )
                admin_page.screenshot(
                    path=str(TMP / f"slack_invite_disabled_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            send_btn.click()
            admin_page.wait_for_timeout(2000)

            log.info(f"  [Slack] Invite sent via Playwright: {email}")
            browser.close()
            return True

    except Exception as e:
        log.error(f"  [Slack] Playwright invite failed for {email}: {e}")
        return False


def slack_reactivate_api(user_id: str, log) -> bool:
    """Attempt to reactivate a deactivated Slack user via the unofficial
    users.admin.setActive endpoint.  Works on some free workspaces with an
    admin-scoped user token; returns False if the endpoint is unsupported so
    the caller can fall back to Playwright."""
    resp = http_requests.post(
        "https://slack.com/api/users.admin.setActive",
        data={"token": SLACK_USER_TOKEN, "user": user_id},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        log.info(f"  [Slack] Reactivated via API: {user_id}")
        return True
    log.warning(
        f"  [Slack] API reactivation unavailable ({data.get('error')}) "
        "— falling back to Playwright"
    )
    return False


def slack_reactivate_playwright(email: str, log) -> bool:
    """Reactivate a deactivated Slack member via the admin panel GUI.

    Confirmed working flow (discovered through extensive debugging):
      1. Login at sealuw.slack.com/sign_in_with_password (minimal browser config)
      2. Load workspace SPA (sealuw.slack.com/ → redirects to app.slack.com)
         to establish the full session required by the admin panel
      3. Click Admin sidebar button → Manage members (opens popup at sealuw.slack.com/admin)
         Note: /admin/deactivated_members causes a React crash in headless mode;
         the Members page at /admin works fine and supports filtering
      4. Apply 'Inactive' billing status filter via JavaScript (filter panel uses
         ReactModal which intercepts Playwright's native click — JS bypasses it)
      5. Search for target email — deactivated member appears in filtered results
      6. Click data-qa="table_row_actions_button" (the '...' row action button)
      7. Click 'Activate account' from the dropdown
      8. In the confirmation modal: select 'Regular Member' radio, click 'Save'
         (The confirm button is labeled "Save" — not "Activate" or "Confirm")
    """
    if not SLACK_ADMIN_EMAIL or not SLACK_ADMIN_PASSWORD:
        log.error(
            "  [Slack] SLACK_ADMIN_EMAIL / SLACK_ADMIN_PASSWORD not set "
            "— cannot reactivate via Playwright"
        )
        return False
    try:
        with sync_playwright() as p:
            # Minimal launch — anti-detection args break React rendering on login page
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            # ── Sign in ───────────────────────────────────────────────────────
            page.goto(f"https://{SLACK_WORKSPACE}/sign_in_with_password")
            page.wait_for_load_state("networkidle")

            # fill() works for email (no React event issues on that field)
            page.fill('input[data-qa="login_email"]', SLACK_ADMIN_EMAIL)

            # keyboard.type() required for password — fill() doesn't trigger React onChange
            page.click('input[type="password"]')
            page.keyboard.type(SLACK_ADMIN_PASSWORD, delay=50)

            sign_in_btn = (
                page.query_selector('button[data-qa="signin_button"]')
                or page.query_selector('button[type="submit"]')
            )
            sign_in_btn.click()
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(4000)

            if "sign_in" in page.url:
                log.error(
                    "  [Slack] Playwright login failed — still on sign-in page. "
                    "Check SLACK_ADMIN_EMAIL / SLACK_ADMIN_PASSWORD in .env"
                )
                page.screenshot(path=str(TMP / "slack_login_failed.png"))
                browser.close()
                return False

            # ── Load workspace SPA to establish full session ───────────────────
            # After password login the browser is at ssb/redirect.
            # The admin panel needs the full workspace SPA session — visiting the
            # workspace root triggers the required session initialization.
            # Using wait_until="load" because the SPA never reaches networkidle
            # (it holds persistent WebSocket connections).
            page.goto(f"https://{SLACK_WORKSPACE}/", wait_until="load", timeout=30000)
            page.wait_for_timeout(10000)                    # let SPA fully boot

            # ── Open Manage members via Admin sidebar (opens as popup tab) ────
            # Click the Admin button in the sidebar
            for btn in page.query_selector_all("button"):
                if btn.inner_text().strip() == "Admin":
                    btn.click()
                    break
            page.wait_for_timeout(2000)

            # Find the Manage members link (href → sealuw.slack.com/admin)
            manage_link = None
            for el in page.query_selector_all("a"):
                if "Manage members" in el.inner_text().strip():
                    manage_link = el
                    break

            if not manage_link:
                log.error("  [Slack] Playwright could not find Manage members link")
                page.screenshot(path=str(TMP / f"slack_debug_{email.split('@')[0]}.png"))
                browser.close()
                return False

            admin_href = manage_link.get_attribute("href") or f"https://{SLACK_WORKSPACE}/admin"

            # Slack opens admin in a new tab — capture the popup
            try:
                from playwright.sync_api import TimeoutError as PWTimeout
                with context.expect_page(timeout=6000) as popup_info:
                    manage_link.click()
                admin_page = popup_info.value
                admin_page.wait_for_load_state("load", timeout=30000)
                admin_page.wait_for_timeout(8000)
            except Exception:
                # No popup — navigate in same page
                admin_page = page
                page.goto(admin_href, wait_until="load", timeout=30000)
                page.wait_for_timeout(8000)

            # ── Apply Inactive billing status filter via JavaScript ────────────
            # The filter panel uses ReactModal which intercepts Playwright's native
            # click on labels — JS evaluation bypasses this restriction.
            for btn in admin_page.query_selector_all("button"):
                if btn.inner_text().strip() == "Filter":
                    btn.click()
                    break
            admin_page.wait_for_timeout(2000)

            result = admin_page.evaluate("""
                () => {
                    const popover = document.querySelector(
                        '.c-data_table_header_multi_filter__pop_over_body, ' +
                        '[class*="multi_filter"], .c-popover'
                    );
                    const container = popover || document;
                    // Try checkbox by associated label
                    for (const inp of container.querySelectorAll('input[type="checkbox"]')) {
                        const lbl = document.querySelector('label[for="' + inp.id + '"]');
                        if (lbl && lbl.textContent.trim() === 'Inactive') {
                            inp.click();
                            return 'checkbox clicked: ' + inp.id;
                        }
                    }
                    // Fallback: leaf-node element with exact text 'Inactive'
                    for (const el of document.querySelectorAll('*')) {
                        if (el.childElementCount === 0 && el.textContent.trim() === 'Inactive') {
                            el.click();
                            return 'element clicked: ' + el.tagName;
                        }
                    }
                    return null;
                }
            """)
            log.info(f"  [Slack] Inactive filter JS result: {result}")
            admin_page.wait_for_timeout(2000)
            admin_page.keyboard.press("Escape")             # close filter panel
            admin_page.wait_for_timeout(1000)

            # ── Search for the target email ───────────────────────────────────
            search = admin_page.query_selector(
                'input[data-qa="workspace-members__table-header-search_input"], '
                'input[placeholder*="ilter by name"], input[type="search"]'
            )
            if not search:
                log.error(f"  [Slack] Playwright could not find search input for {email}")
                admin_page.screenshot(path=str(TMP / f"slack_debug_{email.split('@')[0]}.png"))
                browser.close()
                return False

            search.fill(email)
            admin_page.wait_for_timeout(5000)

            # ── Click the '...' row action button ────────────────────────────
            action_btn = (
                admin_page.query_selector('[data-qa="table_row_actions_button"]')
                or admin_page.query_selector('[aria-label*="Actions for"]')
            )
            if not action_btn:
                log.error(f"  [Slack] Playwright could not find row action button for {email}")
                admin_page.screenshot(path=str(TMP / f"slack_debug_{email.split('@')[0]}.png"))
                browser.close()
                return False

            action_btn.click()
            admin_page.wait_for_timeout(2000)

            # ── Click 'Activate account' from the dropdown ────────────────────
            activate_btn = None
            for el in admin_page.query_selector_all("button, [role='menuitem'], li"):
                txt = el.inner_text().strip().lower()
                if "activate account" in txt or txt == "activate" or "reactivate" in txt:
                    activate_btn = el
                    break

            if not activate_btn:
                log.error(f"  [Slack] Playwright could not find Activate button for {email}")
                admin_page.screenshot(path=str(TMP / f"slack_debug_{email.split('@')[0]}.png"))
                browser.close()
                return False

            activate_btn.click()
            admin_page.wait_for_timeout(2000)

            # ── Handle "Activate account" confirmation modal ───────────────────
            # Slack shows a modal: choose account type (radio: "Regular Member")
            # then click "Save".  The confirm button text is "Save" — not
            # "Activate", "Confirm", or "Yes".
            radio = admin_page.query_selector('input[type="radio"]')
            if radio:
                radio.click()
                admin_page.wait_for_timeout(500)

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
                for btn in admin_page.query_selector_all("button"):
                    txt = btn.inner_text().strip().lower()
                    if txt in ("confirm", "activate", "reactivate", "yes"):
                        save_btn = btn
                        break

            if not save_btn:
                log.error(
                    f"  [Slack] Playwright could not find Save button in "
                    f"confirmation modal for {email}"
                )
                admin_page.screenshot(
                    path=str(TMP / f"slack_no_save_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            save_btn.click()
            admin_page.wait_for_timeout(2000)

            log.info(f"  [Slack] Reactivated via Playwright: {email}")
            browser.close()
            return True

    except Exception as e:
        log.error(f"  [Slack] Playwright reactivation failed for {email}: {e}")
        return False


def handle_slack(email: str, log):
    """Invite a new member or reactivate a returning member in the Slack workspace.

    Decision tree:
      - Token not set                   → skip with warning
      - User not found in Slack         → new member, invite via Playwright admin panel
      - User found, account deactivated → returning member, reactivate (API → Playwright)
      - User found, account active      → already in workspace, no action

    Note: users.admin.invite API requires the legacy 'client' scope (not available on
    modern Slack apps).  New-member invites use slack_invite_playwright instead.
    """
    if not SLACK_USER_TOKEN:
        log.warning("  [Slack] SLACK_USER_TOKEN not set — skipping Slack step")
        return

    user_id, is_deactivated = slack_lookup_user(email, log)

    if user_id is None:
        # Brand-new Slack user — invite via admin panel GUI
        slack_invite_playwright(email, log)
    elif is_deactivated:
        # Returning member — reactivate, then invite (invite re-enables access)
        log.info(f"  [Slack] Detected deactivated account for {email} — reactivating")
        if not slack_reactivate_api(user_id, log):
            slack_reactivate_playwright(email, log)
    else:
        log.info(f"  [Slack] {email} is already an active Slack member — no action needed")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    TMP.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ]
    )
    log = logging.getLogger(__name__)
    log.info("=" * 60)
    log.info("SEAL challenge processing - run started")

    # Load config
    with open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    c               = cfg["challenge"]
    challenge_sid   = c["applicant_challenge_sheet_id"]
    applicants_tab  = c["applicants_tab"]
    stage_col       = c["stage_column_index"]
    stage_trigger   = c["stage_trigger"].strip().lower()
    email_col       = c["email_column_index"]
    added_tab       = c["added_to_seal_life_tab"]
    clan_sid           = c["clan_life_sheet_id"]
    associates_tab     = c["associates_tab"]
    associates_start   = c["associates_start_row"]
    active_group       = c["active_group_email"]

    # Build API clients — reuse same token files as process_applicants.py
    gmail_creds = get_credentials(SCOPES_GMAIL, TOKEN_GMAIL, hint="sealdirector@gmail.com")
    admin_creds = get_credentials(SCOPES_ADMIN, TOKEN_ADMIN, hint="admin@maxalton.com")
    sheets_svc  = build("sheets", "v4", credentials=gmail_creds)
    admin_svc   = build("admin", "directory_v1", credentials=admin_creds)

    # Read source data
    all_rows = get_sheet_data(sheets_svc, challenge_sid, applicants_tab)
    if len(all_rows) < 2:
        log.info("No data rows in Applicants tab. Nothing to do.")
        return

    header    = all_rows[0]
    data_rows = all_rows[1:]
    num_cols  = len(header)

    # Ensure destination tabs exist
    ensure_tab_exists(sheets_svc, challenge_sid, added_tab, header)
    ensure_tab_exists(sheets_svc, clan_sid, associates_tab, header)

    # Find all stage 3 rows — deletion after processing is the dedup mechanism.
    # Students may reapply, so no email-based dedup is applied here.
    new_rows = []

    for i, row in enumerate(all_rows):
        # Pad row so column indices are safe
        padded = row + [""] * max(0, max(stage_col, email_col) + 1 - len(row))

        stage = padded[stage_col].strip().lower()
        if not stage.startswith(stage_trigger):
            continue                              # not stage 3 yet

        email = padded[email_col].strip()
        if not email or not EMAIL_RE.match(email):
            continue

        new_rows.append((i, row, email))
        log.info(f"STAGE 3   {email}")

    log.info(f"New stage 3 rows this run: {len(new_rows)}")

    # Process each qualifying row
    if new_rows:
        rows_only = [r for _, r, _ in new_rows]

        # 1. Copy to 'Added to SEAL Life' (same sheet)
        append_rows(sheets_svc, challenge_sid, added_tab, rows_only)
        log.info(f"  [Sheets] Copied {len(rows_only)} row(s) to '{added_tab}'")

        # 2. Write to next blank row in 'Associates' in SEAL Clan Life sheet.
        #    Column P (index 15) is blanked — formula is copied from the row above instead.
        STAGE_COL = 15  # column P
        associates_rows = [
            r[:STAGE_COL] + [""] + r[STAGE_COL + 1:] if len(r) > STAGE_COL else r
            for r in rows_only
        ]
        dest_row = write_to_next_blank_row(sheets_svc, clan_sid, associates_tab,
                                           associates_rows, associates_start, log)
        # Copy the column P formula from the row above into each newly written row
        if dest_row > 1:
            copy_formula_down(sheets_svc, clan_sid, associates_tab,
                              source_row=dest_row - 1,
                              dest_start_row=dest_row,
                              num_rows=len(associates_rows),
                              col_idx=15,
                              log=log)

        # 3. Add each email to the active Google Group
        for _, _, email in new_rows:
            add_to_google_group(admin_svc, active_group, email, log)

        # 4. Handle Slack membership — invite new members, reactivate returning ones
        for _, _, email in new_rows:
            handle_slack(email, log)

        # 5. Delete processed rows from Applicants tab (reverse order avoids index shifting)
        # Note: will fail gracefully if the tab has protected ranges — remove sheet
        # protection via Data > Protect sheets and ranges to enable this step.
        row_indices = [i for i, _, _ in new_rows]
        try:
            delete_rows(sheets_svc, challenge_sid, applicants_tab, row_indices, log)
        except HttpError as e:
            log.warning(f"  [Sheets] Could not delete rows from '{applicants_tab}' (protected?): {e.reason}")

    log.info("Run complete.")


if __name__ == "__main__":
    main()
