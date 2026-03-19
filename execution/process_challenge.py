#!/usr/bin/env python3
"""
process_challenge.py — SEAL Applicant Challenge Stage 3 Processing

Monitors the 'Applicants' tab of the SEAL Applicant Challenge sheet for rows
where column P equals "stage 3". For each newly qualifying row:
  1. Copies the row to the 'Associates' tab of the SEAL Clan Life sheet
  2. Adds the email (column AP) to the seal-active@maxalton.com Workspace group
  3. Handles Slack membership:
       - New members   → sends a workspace invite via the admin panel GUI
       - Returning members (deactivated account) → reactivates via API,
         falling back to Playwright GUI automation if the API is unavailable
  4. Copies the row to the 'Added to SEAL Life' tab (commit marker — written last)
  5. Deletes the processed row from the Applicants tab

Deduplication uses a two-phase check against both destination tabs:
  - Email in 'Added to SEAL Life'           → fully committed, skip all steps
  - Email in Associates but NOT in commit tab → partial failure; resume from step 2
  - Email in neither                         → full processing
This makes every re-run safe and idempotent even after a mid-run crash.

Requires in .env:
    SLACK_USER_TOKEN     — xoxp- user token with admin, users:read, users:read.email scopes
    SLACK_ADMIN_EMAIL    — admin account email (used by Playwright fallback)
    SLACK_ADMIN_PASSWORD — admin account password (used by Playwright fallback)

Usage:
    python execution/process_challenge.py
"""

import copy
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
from google_auth_httplib2 import AuthorizedHttp
from proxy_http import make_http
from tui_status import set_live, log_run_start, log_run_msg, log_event, log_result
from run_logger import RunLogger

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

    # ── Verify write ──────────────────────────────────────────────────────
    verify = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A{next_row}:A{next_row + len(rows) - 1}"
    ).execute()
    written = verify.get("values", [])
    if len(written) != len(rows):
        log.error(
            f"  [Sheets] VERIFY FAILED: expected {len(rows)} row(s) at '{tab}'!A{next_row}, "
            f"but read back {len(written)}. Data may not have been written correctly."
        )
    else:
        log.info(f"  [Sheets] Verified {len(written)} row(s) written to '{tab}'")

    return next_row


def find_next_blank_row(svc, spreadsheet_id: str, tab: str, start_row: int) -> int:
    """Return the 1-indexed first blank row in column A at or below start_row."""
    result = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A:A"
    ).execute()
    col_a = result.get("values", [])
    for i in range(start_row - 1, len(col_a)):
        if not col_a[i] or not str(col_a[i][0]).strip():
            return i + 1
    return max(len(col_a) + 1, start_row)


def copy_rows_with_formatting(svc, src_sid: str, src_tab: str,
                               src_row_indices: list,
                               dst_sid: str, dst_tab: str,
                               dst_start_row: int,
                               blank_cols: list,
                               log) -> None:
    """Copy rows from src to dst preserving all formatting and hyperlinks.

    Uses spreadsheets.get(includeGridData=True) to read full CellData —
    this captures fonts, colours, alignment, and both formula-based and
    rich-text hyperlinks that values().get() silently drops.

    Writes via updateCells so formatting is applied cell-by-cell in the
    destination rather than overwriting the entire sheet.

    blank_cols: 0-based column indices to clear in each copied row
                (e.g. [15] to blank column P before formula propagation).
    """
    # ── 1. Read source rows with full cell data ────────────────────────────
    src_result = svc.spreadsheets().get(
        spreadsheetId=src_sid,
        ranges=[f"'{src_tab}'"],
        includeGridData=True,
        fields="sheets.data.rowData,sheets.properties.title"
    ).execute()

    all_row_data = []
    for sheet in src_result.get("sheets", []):
        if sheet["properties"]["title"] == src_tab:
            all_row_data = sheet["data"][0].get("rowData", [])
            break

    # ── 2. Extract requested rows and blank specified columns ──────────────
    rows_to_write = []
    for idx in src_row_indices:
        rd = copy.deepcopy(all_row_data[idx]) if idx < len(all_row_data) else {}
        cells = rd.get("values", [])
        for col in blank_cols:
            if col < len(cells):
                cells[col] = {}   # empty CellData clears the cell
        rd["values"] = cells
        rows_to_write.append(rd)

    # ── 3. Write to destination with full formatting ───────────────────────
    # Get sheetId and columnCount together to avoid an extra API call.
    # columnCount is used to trim source rows that are wider than the
    # destination sheet (updateCells rejects writes beyond the last column).
    dst_meta = svc.spreadsheets().get(spreadsheetId=dst_sid).execute()
    dst_sheet_id = None
    dst_col_count = None
    for s in dst_meta["sheets"]:
        if s["properties"]["title"] == dst_tab:
            dst_sheet_id = s["properties"]["sheetId"]
            dst_col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if dst_sheet_id is None:
        raise ValueError(f"Tab '{dst_tab}' not found in spreadsheet {dst_sid}")

    # Trim each row to the destination column count
    for rd in rows_to_write:
        rd["values"] = rd.get("values", [])[:dst_col_count]

    requests = [
        {
            "updateCells": {
                "rows": [rd],
                "fields": "userEnteredValue,userEnteredFormat,textFormatRuns",
                "start": {
                    "sheetId": dst_sheet_id,
                    "rowIndex": dst_start_row - 1 + i,   # 0-indexed
                    "columnIndex": 0
                }
            }
        }
        for i, rd in enumerate(rows_to_write)
    ]
    svc.spreadsheets().batchUpdate(
        spreadsheetId=dst_sid,
        body={"requests": requests}
    ).execute()
    log.info(f"  [Sheets] Copied {len(rows_to_write)} row(s) with formatting "
             f"to '{dst_tab}' starting at row {dst_start_row}")

    # ── Verify write (use wide range since column A may be empty) ────────
    verify = svc.spreadsheets().values().get(
        spreadsheetId=dst_sid,
        range=f"'{dst_tab}'!A{dst_start_row}:AP{dst_start_row + len(rows_to_write) - 1}"
    ).execute()
    written = verify.get("values", [])
    if len(written) != len(rows_to_write):
        log.error(
            f"  [Sheets] VERIFY FAILED: expected {len(rows_to_write)} row(s) at "
            f"'{dst_tab}' rows {dst_start_row}-{dst_start_row + len(rows_to_write) - 1}, "
            f"but read back {len(written)}"
        )
    else:
        log.info(f"  [Sheets] Verified {len(written)} row(s) copied to '{dst_tab}'")


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
    log.info(f"  [Sheets] Copied column P formula from row {source_row} -> rows {dest_start_row}-{dest_start_row + num_rows - 1}")


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


def _slack_invite_single(email: str, log) -> bool:
    """Single attempt to invite via admin panel GUI."""
    from slack_auth import slack_login, open_admin_panel

    if not SLACK_ADMIN_EMAIL or not SLACK_ADMIN_PASSWORD:
        log.error("  [Slack] SLACK_ADMIN_EMAIL / SLACK_ADMIN_PASSWORD not set")
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        if not slack_login(page, context, log):
            browser.close()
            return False

        admin_page = open_admin_panel(page, context, log)
        if not admin_page:
            browser.close()
            return False

        # ── Click "Invite People" ──────────────────────────────────────────
        invite_btn = None
        for btn in admin_page.query_selector_all("button"):
            if "invite people" in btn.inner_text().strip().lower():
                invite_btn = btn
                break
        if not invite_btn:
            log.error("  [Slack] Could not find Invite People button")
            admin_page.screenshot(path=str(TMP / f"slack_invite_debug_{email.split('@')[0]}.png"))
            browser.close()
            return False

        invite_btn.click()
        admin_page.wait_for_timeout(2000)

        # ── Fill email in contenteditable "To:" field ──────────────────────
        email_field = (
            admin_page.query_selector('[data-qa="invite_modal_select-input"]')
            or admin_page.query_selector('[contenteditable="true"]')
        )
        if not email_field:
            log.error(f"  [Slack] Could not find email input for {email}")
            admin_page.screenshot(path=str(TMP / f"slack_invite_debug_{email.split('@')[0]}.png"))
            browser.close()
            return False

        email_field.click()
        admin_page.wait_for_timeout(500)
        admin_page.keyboard.type(email, delay=30)
        admin_page.keyboard.press("Tab")
        admin_page.wait_for_timeout(1500)

        # ── Click Send ─────────────────────────────────────────────────────
        send_btn = None
        for btn in admin_page.query_selector_all("button"):
            if btn.inner_text().strip().lower() == "send":
                send_btn = btn
                break
        if not send_btn:
            log.error(f"  [Slack] Could not find Send button for {email}")
            admin_page.screenshot(path=str(TMP / f"slack_invite_debug_{email.split('@')[0]}.png"))
            browser.close()
            return False

        if send_btn.evaluate("b => b.disabled"):
            log.warning(f"  [Slack] Send disabled for {email} — possibly already a member")
            admin_page.screenshot(path=str(TMP / f"slack_invite_disabled_{email.split('@')[0]}.png"))
            browser.close()
            return False

        send_btn.click()
        admin_page.wait_for_timeout(2000)
        log.info(f"  [Slack] Invite sent via Playwright: {email}")
        browser.close()
        return True


def slack_invite_playwright(email: str, log, max_retries: int = 3) -> bool:
    """Invite a new member with automatic retry on failure."""
    from slack_auth import SESSION_FILE as _SESSION_FILE

    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"  [Slack] Invite attempt {attempt}/{max_retries} for {email}")
            if _slack_invite_single(email, log):
                return True
        except Exception as e:
            log.error(f"  [Slack] Invite error for {email} (attempt {attempt}): {e}")
        if attempt < max_retries:
            log.info("  [Slack] Clearing session and retrying...")
            _SESSION_FILE.unlink(missing_ok=True)

    log.error(f"  [Slack] All {max_retries} invite attempts failed for {email}")
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


def _slack_reactivate_single(email: str, log) -> bool:
    """Single attempt to reactivate a deactivated Slack member via admin panel GUI."""
    from slack_auth import slack_login, open_admin_panel

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        if not slack_login(page, context, log):
            browser.close()
            return False

        admin_page = open_admin_panel(page, context, log)
        if not admin_page:
            browser.close()
            return False

        # ── Apply Inactive billing status filter via JavaScript ────────────
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
                for (const inp of container.querySelectorAll('input[type="checkbox"]')) {
                    const lbl = document.querySelector('label[for="' + inp.id + '"]');
                    if (lbl && lbl.textContent.trim() === 'Inactive') {
                        inp.click();
                        return 'checkbox clicked: ' + inp.id;
                    }
                }
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
        admin_page.keyboard.press("Escape")
        admin_page.wait_for_timeout(1000)

        # ── Search for the target email ───────────────────────────────────
        search = admin_page.query_selector(
            'input[data-qa="workspace-members__table-header-search_input"], '
            'input[placeholder*="ilter by name"], input[type="search"]'
        )
        if not search:
            log.error(f"  [Slack] Could not find search input for {email}")
            admin_page.screenshot(path=str(TMP / f"slack_debug_{email.split('@')[0]}.png"))
            browser.close()
            return False

        search.fill(email)
        admin_page.wait_for_timeout(5000)

        # ── Hover over row to reveal action button ────────────────────────
        name_cell = admin_page.query_selector(
            '[data-qa-column="workspace-members_table_real_name"]'
        )
        if name_cell:
            box = name_cell.bounding_box()
            if box:
                admin_page.mouse.move(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
                admin_page.wait_for_timeout(1500)

        # ── Click the '...' row action button ─────────────────────────────
        action_btn = admin_page.query_selector('[data-qa="table_row_actions_button"]')
        if not action_btn:
            log.error(f"  [Slack] Could not find row action button for {email}")
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
            log.error(f"  [Slack] Could not find Activate button for {email}")
            admin_page.screenshot(path=str(TMP / f"slack_debug_{email.split('@')[0]}.png"))
            browser.close()
            return False

        activate_btn.click()
        admin_page.wait_for_timeout(2000)

        # ── Handle confirmation modal (radio → Save) ──────────────────────
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
            log.error(f"  [Slack] Could not find Save button for {email}")
            admin_page.screenshot(path=str(TMP / f"slack_no_save_{email.split('@')[0]}.png"))
            browser.close()
            return False

        save_btn.click()
        admin_page.wait_for_timeout(2000)
        log.info(f"  [Slack] Reactivated via Playwright: {email}")
        browser.close()
        return True


def slack_reactivate_playwright(email: str, log, max_retries: int = 3) -> bool:
    """Reactivate a deactivated Slack member with automatic retry."""
    from slack_auth import SESSION_FILE as _SESSION_FILE

    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"  [Slack] Reactivation attempt {attempt}/{max_retries} for {email}")
            if _slack_reactivate_single(email, log):
                return True
        except Exception as e:
            log.error(f"  [Slack] Reactivation error for {email} (attempt {attempt}): {e}")
        if attempt < max_retries:
            log.info("  [Slack] Clearing session and retrying...")
            _SESSION_FILE.unlink(missing_ok=True)

    log.error(f"  [Slack] All {max_retries} reactivation attempts failed for {email}")
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
    set_live("checking", "Scanning challenge sheet")
    log_run_start("process_challenge")

    rl = RunLogger("process_challenge", log)
    rl.__enter__()
    try:
        _run_challenge(log, rl)
    except Exception as exc:
        import traceback
        rl.log_error("UNKNOWN", str(exc), traceback.format_exc())
        raise
    finally:
        rl.__exit__(None, None, None)


def _run_challenge(log, rl):
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
    sheets_svc  = build("sheets", "v4", http=AuthorizedHttp(gmail_creds, http=make_http()))
    admin_svc   = build("admin", "directory_v1", http=AuthorizedHttp(admin_creds, http=make_http()))

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

    # Find all stage 3 rows
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

    log.info(f"Stage 3 rows found: {len(new_rows)}")

    # ── Dedup: classify rows by how far they got in a previous run ────────────
    # Dedup: only Associates matters — if the email is already there, skip.
    # "Added to SEAL Life" is record-keeping only; it does NOT block processing.
    already_committed     = get_emails_in_tab(sheets_svc, challenge_sid, added_tab, email_col)
    already_in_associates = get_emails_in_tab(sheets_svc, clan_sid, associates_tab, email_col)

    skip_rows    = []  # already in Associates — nothing to do except delete from Applicants
    full_rows    = []  # not in Associates — full processing required

    for item in new_rows:
        i, row, email = item
        if email.lower() in already_in_associates:
            log.info(f"SKIP     {email}  (already in Associates — no action needed)")
            skip_rows.append(item)
        else:
            # Not in Associates yet — full processing required.
            # "Added to SEAL Life" is record-keeping only; it does NOT block processing.
            if email.lower() in already_committed:
                log.info(f"PROCESS  {email}  (in '{added_tab}' for record-keeping, but not in Associates)")
            full_rows.append(item)

    log.info(
        f"Categorised: {len(full_rows)} to process | {len(skip_rows)} skip (already in Associates)"
    )

    STAGE_COL = 15  # column P
    committed_indices = set()   # track rows whose commit marker was verified
    failed_indices = set()      # track rows that failed verification

    def _verify_write(svc, sid, tab, start_row, count, label):
        """Read back after write; return True if verified.
        Uses a wide range (A:AP) since column A may be empty in some rows."""
        verify = svc.spreadsheets().values().get(
            spreadsheetId=sid,
            range=f"'{tab}'!A{start_row}:AP{start_row + count - 1}"
        ).execute()
        written = verify.get("values", [])
        if len(written) != count:
            log.error(
                f"  [Sheets] VERIFY FAILED ({label}): expected {count} row(s) at "
                f"'{tab}' rows {start_row}-{start_row + count - 1}, but read back {len(written)}"
            )
            return False
        log.info(f"  [Sheets] Verified {count} row(s) for {label}")
        return True

    # ── Full processing ───────────────────────────────────────────────────────
    # Order: Associates → Group → Slack → Added to SEAL Life (commit marker last).
    # If the script crashes before the commit write, next run finds the email in
    # Associates and routes it through the partial-resume path instead.
    if full_rows:
        src_indices = [i for i, _, _ in full_rows]

        # 1. Associates (Clan Life sheet)
        dest_row = find_next_blank_row(sheets_svc, clan_sid, associates_tab, associates_start)
        copy_rows_with_formatting(sheets_svc, challenge_sid, applicants_tab, src_indices,
                                  clan_sid, associates_tab, dest_row, [STAGE_COL], log)
        # Copy column P formula from the row above into each newly written row
        if dest_row > 1:
            copy_formula_down(sheets_svc, clan_sid, associates_tab,
                              source_row=dest_row - 1,
                              dest_start_row=dest_row,
                              num_rows=len(full_rows),
                              col_idx=STAGE_COL,
                              log=log)

        # 2. Group + Slack (per-email)
        for _, row, email in full_rows:
            set_live("adding", "Adding to group + Slack", email=email, step="group")
            add_to_google_group(admin_svc, active_group, email, log)
            set_live("adding", "Inviting to Slack", email=email, step="slack")
            handle_slack(email, log)
            log_event("add", email, "STAGE3_ADDED")
            # Extract name from row for notes (column index 1 is typically the name)
            row_name = row[1].strip() if len(row) > 1 else ""
            rl.add_note(f"STAGE 3 → Associates: {row_name} ({email})")

        # 3. Added to SEAL Life — commit marker, written last
        added_next_row = find_next_blank_row(sheets_svc, challenge_sid, added_tab, 1)
        copy_rows_with_formatting(sheets_svc, challenge_sid, applicants_tab, src_indices,
                                  challenge_sid, added_tab, added_next_row, [], log)

        # Verify commit marker
        if _verify_write(sheets_svc, challenge_sid, added_tab, added_next_row, len(full_rows), "commit marker"):
            committed_indices.update(src_indices)
        else:
            failed_indices.update(src_indices)

    # ── Delete processed rows from Applicants ─────────────────────────────────
    # Only delete rows whose Associates write was verified (tracked via commit
    # marker verification), plus skip rows (already in Associates).
    # Never delete rows that failed verification.
    safe_to_delete = [i for i, _, _ in skip_rows]  # already in Associates
    safe_to_delete += [i for i in committed_indices]
    if failed_indices:
        log.error(
            f"  [Sheets] {len(failed_indices)} row(s) NOT deleted — commit marker "
            f"verification failed. Will retry on next run."
        )
    rows_to_delete = safe_to_delete
    if rows_to_delete:
        try:
            delete_rows(sheets_svc, challenge_sid, applicants_tab, rows_to_delete, log)
        except HttpError as e:
            log.warning(
                f"  [Sheets] Could not delete rows from '{applicants_tab}' (protected?): {e.reason}"
            )

    if not new_rows:
        log_run_msg("No stage 3 applicants found")
        log_result("empty")
        rl.set_rows_processed("0 stage 3")
    else:
        log_run_msg(f"Challenge: {len(full_rows)} processed, {len(skip_rows)} skipped")
        log_result("processed")
        rl.set_rows_processed(f"{len(full_rows)} processed, {len(skip_rows)} skipped")
        if full_rows:
            rl.add_action(f"{len(full_rows)} added to Associates + Group + Slack")
        if failed_indices:
            rl.log_error("SHEET_003", f"{len(failed_indices)} commit marker verification(s) failed")
    set_live("idle", "Challenge processing complete")
    log.info("Run complete.")


if __name__ == "__main__":
    main()
