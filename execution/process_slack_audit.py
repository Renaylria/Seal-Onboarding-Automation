#!/usr/bin/env python3
"""
process_slack_audit.py — SEAL Slack Access Audit

Compares the Associates tab of the SEAL Clan Life sheet against the Slack
workspace member list.  For each Associate whose email is missing or
deactivated in Slack, the script restores access:
  - Not found in Slack       → invite via Playwright admin panel
  - Found but deactivated    → reactivate via API, fallback to Playwright

This script MUST run AFTER the three main pipeline scripts (clan_cleanup,
applicants, challenge) so that the Associates tab reflects the current state
— departing members already removed, new members already added.

Requires in .env:
    SLACK_USER_TOKEN     — xoxp- user token with admin, users:read, users:read.email scopes
    SLACK_ADMIN_EMAIL    — admin account email (used by Playwright fallback)
    SLACK_ADMIN_PASSWORD — admin account password (used by Playwright fallback)

Usage:
    python execution/process_slack_audit.py
"""

import os
import re
import sys
import logging
from pathlib import Path

import requests as http_requests
import yaml
from dotenv import load_dotenv

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google_auth_httplib2 import AuthorizedHttp
from proxy_http import make_http
from tui_status import set_live, log_run_start, log_run_msg, log_event, log_result
from run_logger import RunLogger
from sheets_retry import retry_execute

from playwright.sync_api import sync_playwright

# Ground-truth verification (shared module in sudoku-blueprint)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "sudoku-blueprint"))
from verify import verify_slack_active

load_dotenv()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ── Slack credentials (from .env) ──────────────────────────────────────────────
SLACK_USER_TOKEN     = os.getenv("SLACK_USER_TOKEN", "").strip()
SLACK_ADMIN_EMAIL    = os.getenv("SLACK_ADMIN_EMAIL", "").strip()
SLACK_ADMIN_PASSWORD = os.getenv("SLACK_ADMIN_PASSWORD", "").strip()
SLACK_WORKSPACE      = "sealuw.slack.com"

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent.parent
CONFIG      = ROOT / "config.yaml"
TOKEN_GMAIL = ROOT / "token_gmail.json"   # sealscripting@gmail.com — sheets
CREDS       = ROOT / "credentials.json"
TMP         = ROOT / ".tmp"
LOG_FILE    = TMP / "process_slack_audit.log"

# ── OAuth Scopes ───────────────────────────────────────────────────────────────
SCOPES_GMAIL = ["https://www.googleapis.com/auth/spreadsheets"]


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
    result = retry_execute(svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'"
    ))
    return result.get("values", [])


def get_associates_emails(svc, spreadsheet_id: str, tab: str,
                          email_col: int, start_row: int, log) -> list[str]:
    """Extract valid email addresses from the Associates tab.

    Returns a deduplicated list of lowercase email strings, skipping
    header/sample rows (before start_row) and blank/invalid cells.
    """
    rows = get_sheet_data(svc, spreadsheet_id, tab)
    emails = []
    seen = set()
    for i, row in enumerate(rows):
        # start_row is 1-based (row 14 = index 13)
        if i < start_row - 1:
            continue
        if len(row) <= email_col:
            continue
        email = row[email_col].strip().lower()
        if not email or not EMAIL_RE.match(email):
            continue
        if email in seen:
            continue
        seen.add(email)
        emails.append(email)
    log.info(f"  [Sheets] Found {len(emails)} unique Associate email(s)")
    return emails


# ══════════════════════════════════════════════════════════════════════════════
# Slack — full member list via API
# ══════════════════════════════════════════════════════════════════════════════

def get_all_slack_members(log) -> dict[str, dict] | None:
    """Fetch all Slack workspace members (active + deactivated).

    Returns a dict keyed by lowercase email → {id, deleted, real_name},
    or None if the API call fails.
    """
    if not SLACK_USER_TOKEN:
        log.error("  [Slack] SLACK_USER_TOKEN not set")
        return None

    members = {}
    cursor = None
    page = 0
    while True:
        page += 1
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
            return None

        for m in data.get("members", []):
            profile = m.get("profile", {})
            email = profile.get("email", "").strip().lower()
            if email:
                members[email] = {
                    "id": m["id"],
                    "deleted": m.get("deleted", False),
                    "real_name": profile.get("real_name", m.get("real_name", "?")),
                }

        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    log.info(f"  [Slack] Fetched {len(members)} Slack member(s) across {page} page(s)")
    return members


# ══════════════════════════════════════════════════════════════════════════════
# Slack — invite / reactivate (reused from process_challenge.py patterns)
# ══════════════════════════════════════════════════════════════════════════════

def slack_reactivate_api(user_id: str, log) -> bool:
    """Attempt to reactivate a deactivated Slack user via the unofficial
    users.admin.setActive endpoint."""
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
            admin_page.screenshot(path=str(TMP / f"slack_audit_invite_debug_{email.split('@')[0]}.png"))
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
            admin_page.screenshot(path=str(TMP / f"slack_audit_invite_debug_{email.split('@')[0]}.png"))
            browser.close()
            return False

        email_field.click(force=True)
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
            admin_page.screenshot(path=str(TMP / f"slack_audit_invite_debug_{email.split('@')[0]}.png"))
            browser.close()
            return False

        if send_btn.evaluate("b => b.disabled"):
            log.warning(f"  [Slack] Send disabled for {email} — possibly already a member")
            admin_page.screenshot(path=str(TMP / f"slack_audit_invite_disabled_{email.split('@')[0]}.png"))
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
            admin_page.screenshot(path=str(TMP / f"slack_audit_debug_{email.split('@')[0]}.png"))
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
            admin_page.screenshot(path=str(TMP / f"slack_audit_debug_{email.split('@')[0]}.png"))
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
            admin_page.screenshot(path=str(TMP / f"slack_audit_debug_{email.split('@')[0]}.png"))
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
            admin_page.screenshot(path=str(TMP / f"slack_audit_no_save_{email.split('@')[0]}.png"))
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


def handle_slack_restore(email: str, slack_member: dict | None, log) -> str:
    """Restore Slack access for an Associate.

    Args:
        email:        The Associate's email address.
        slack_member: The member dict from get_all_slack_members (None if not found).
        log:          Logger instance.

    Returns:
        A short status string: "invited", "reactivated", "failed", or "skipped".
    """
    if slack_member is None:
        # Not in Slack at all — send invite
        log.info(f"  [Audit] {email} not found in Slack — sending invite")
        if slack_invite_playwright(email, log):
            return "invited"
        return "failed"

    if slack_member["deleted"]:
        # Deactivated — reactivate
        log.info(
            f"  [Audit] {email} is deactivated in Slack "
            f"({slack_member['real_name']}, {slack_member['id']}) — reactivating"
        )
        if slack_reactivate_api(slack_member["id"], log):
            return "reactivated"
        if slack_reactivate_playwright(email, log):
            return "reactivated"
        return "failed"

    # Should not reach here (caller only sends mismatches), but be safe
    return "skipped"


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
    log.info("SEAL Slack audit - run started")
    set_live("checking", "Running Slack audit")
    log_run_start("process_slack_audit")

    rl = RunLogger("process_slack_audit", log)
    rl.__enter__()
    try:
        _run_audit(log, rl)
    except Exception as exc:
        import traceback
        rl.log_error("UNKNOWN", str(exc), traceback.format_exc())
        raise
    finally:
        rl.__exit__(None, None, None)


def _run_audit(log, rl):
    # ── Load config ───────────────────────────────────────────────────────────
    with open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    c               = cfg["challenge"]
    clan_sid        = c["clan_life_sheet_id"]
    associates_tab  = c["associates_tab"]
    email_col       = c["email_column_index"]       # 41 (column AP)
    start_row       = c["associates_start_row"]      # 14

    # ── Authenticate (Sheets) ─────────────────────────────────────────────────
    log.info("Authenticating (Sheets)…")
    creds = get_credentials(SCOPES_GMAIL, TOKEN_GMAIL, "sealscripting@gmail.com")
    sheets_svc = build(
        "sheets", "v4",
        http=AuthorizedHttp(creds, http=make_http()),
    )

    # ── Step 1: Get all Associate emails ──────────────────────────────────────
    set_live("checking", "Reading Associates tab")
    associate_emails = get_associates_emails(
        sheets_svc, clan_sid, associates_tab, email_col, start_row, log
    )
    if not associate_emails:
        log.info("No Associates found — nothing to audit")
        rl.set_rows_processed("0 associates")
        rl.add_action("No associates to audit")
        return

    # ── Step 2: Get all Slack members ─────────────────────────────────────────
    set_live("checking", "Fetching Slack member list")
    slack_members = get_all_slack_members(log)
    if slack_members is None:
        log.error("Failed to fetch Slack member list — aborting audit")
        rl.log_error("SLACK_001", "Failed to fetch Slack member list via API")
        return

    # ── Step 3: Compare — find mismatches ─────────────────────────────────────
    missing = []      # not in Slack at all
    deactivated = []  # in Slack but deleted=True
    active = []       # in Slack and active — no action needed

    for email in associate_emails:
        member = slack_members.get(email)
        if member is None:
            missing.append(email)
        elif member["deleted"]:
            deactivated.append(email)
        else:
            active.append(email)

    log.info(
        f"Audit results: {len(active)} active | "
        f"{len(deactivated)} deactivated | {len(missing)} missing"
    )

    mismatches = missing + deactivated
    if not mismatches:
        log.info("All Associates have active Slack access — no action needed")
        rl.set_rows_processed(f"{len(associate_emails)} associates audited")
        rl.add_action("All associates have active Slack access")
        return

    # ── Step 4: Restore access for each mismatch ─────────────────────────────
    invited = 0
    reactivated = 0
    failed = 0

    for email in mismatches:
        member = slack_members.get(email)
        set_live("fixing", "Restoring Slack access", email=email)
        result = handle_slack_restore(email, member, log)

        if result == "invited":
            invited += 1
            rl.add_note(f"INVITED: {email} (not found in Slack)")
        elif result == "reactivated":
            reactivated += 1
            rl.add_note(f"REACTIVATED: {email}")
        elif result == "failed":
            failed += 1
            rl.add_note(f"FAILED: {email}")
            rl.log_error(
                "AUDIT_RESTORE",
                f"Failed to restore Slack access for {email}",
            )

        # Verify after action
        if result in ("invited", "reactivated") and SLACK_USER_TOKEN:
            sv = verify_slack_active(email, SLACK_USER_TOKEN, "bearer")
            log.info(f"  [Verify] Slack active for {email}: {sv.detail}")

    # ── Summary ───────────────────────────────────────────────────────────────
    summary_parts = []
    if invited:
        summary_parts.append(f"{invited} invited")
    if reactivated:
        summary_parts.append(f"{reactivated} reactivated")
    if failed:
        summary_parts.append(f"{failed} failed")

    action_summary = f"Audit: {len(associate_emails)} associates checked — " + ", ".join(summary_parts)
    log.info(action_summary)

    rl.set_rows_processed(f"{len(associate_emails)} associates audited")
    rl.add_action(action_summary)

    if failed:
        rl.set_status("WARNING")


if __name__ == "__main__":
    main()
