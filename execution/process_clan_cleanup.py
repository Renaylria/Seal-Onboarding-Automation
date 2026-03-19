"""
process_clan_cleanup.py
-----------------------
Monitors the "Associates" tab of the SEAL Clan Life Google Sheet for rows
where column K contains a status trigger.  Based on the status, it routes
rows to destination tabs, removes members from the Google Group where
applicable, and deletes the processed rows from Associates.

Status routing (case-insensitive startswith):
  "gameover"      → Clan Life AAD "Ex-Communicado" tab + remove from group + delete row
  "ex-associate"  → Clan Life AAD "Ex-Associate" tab  + remove from group + delete row
  "affiliate"     → SEAL Clan Life "Affiliates" tab   + NO group removal  + delete row

Run:
  python execution/process_clan_cleanup.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

import requests as http_requests
import yaml
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from playwright.sync_api import sync_playwright

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent.parent
CONFIG      = ROOT / "config.yaml"
TOKEN_GMAIL = ROOT / "token_gmail.json"   # sealdirector@gmail.com — Sheets access
TOKEN_ADMIN = ROOT / "token_admin.json"   # admin@maxalton.com     — Group management
CREDS       = ROOT / "credentials.json"
TMP                    = ROOT / ".tmp"
LOG_FILE               = TMP / "process_clan_cleanup.log"
PENDING_DEACTIVATE_FILE = TMP / "slack_deactivate_pending.json"

# ── OAuth Scopes ───────────────────────────────────────────────────────────────
SCOPES_GMAIL = ["https://www.googleapis.com/auth/spreadsheets"]
SCOPES_ADMIN = ["https://www.googleapis.com/auth/admin.directory.group.member"]

# ── Slack credentials (from .env) ──────────────────────────────────────────────
SLACK_USER_TOKEN     = os.getenv("SLACK_USER_TOKEN", "").strip()
SLACK_ADMIN_EMAIL    = os.getenv("SLACK_ADMIN_EMAIL", "").strip()
SLACK_ADMIN_PASSWORD = os.getenv("SLACK_ADMIN_PASSWORD", "").strip()
SLACK_WORKSPACE      = "sealuw.slack.com"
EMAIL_RE             = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ══════════════════════════════════════════════════════════════════════════════
# Logging setup
# ══════════════════════════════════════════════════════════════════════════════

def _setup_logging() -> logging.Logger:
    TMP.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    stream_handler.setFormatter(fmt)

    logger = logging.getLogger("clan_cleanup")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


# ══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════════════════════

def get_credentials(scopes: list[str], token_path: Path, hint: str) -> Credentials:
    """Load/refresh OAuth credentials.  Opens the browser for first-run consent.

    Args:
        scopes:     List of OAuth scope strings required for these credentials.
        token_path: Path to the cached token JSON file (created after first auth).
        hint:       Human-readable label used in log/error messages (e.g. account email).

    Returns:
        A valid, refreshed Credentials object.
    """
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), scopes)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())

    return creds


def get_sheet_data(svc, spreadsheet_id: str, tab: str) -> list[list]:
    """Return all rows from *tab* as a list of lists.

    Trailing empty cells within a row are omitted by the Sheets API; callers
    must handle rows that are shorter than the maximum expected column index.

    Args:
        svc:            An authenticated Google Sheets service object.
        spreadsheet_id: The spreadsheet ID string.
        tab:            The tab (sheet) name to read.

    Returns:
        A list of rows; each row is a list of cell values (strings).
    """
    result = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=tab)
        .execute()
    )
    return result.get("values", [])


def ensure_tab_exists(svc, spreadsheet_id: str, tab: str) -> None:
    """Create *tab* in *spreadsheet_id* if it does not already exist.

    Args:
        svc:            An authenticated Google Sheets service object.
        spreadsheet_id: The spreadsheet ID string.
        tab:            The tab name to ensure exists.
    """
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab in existing:
        return

    body = {"requests": [{"addSheet": {"properties": {"title": tab}}}]}
    svc.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()


def write_to_next_blank_row(
    svc,
    spreadsheet_id: str,
    tab: str,
    rows: list[list],
    start_row: int,
    log: logging.Logger,
) -> None:
    """Append *rows* to *tab* starting at the first blank row >= *start_row*.

    Reads column A of *tab* to locate the first truly empty row at or after
    *start_row* (1-indexed), then writes using values().update() rather than
    values().append() so that the destination range is exact and predictable.

    Args:
        svc:            An authenticated Google Sheets service object.
        spreadsheet_id: The spreadsheet ID string.
        tab:            The tab name to write into.
        rows:           A list of row lists (each inner list is one row of values).
        start_row:      The first row number (1-indexed) that is eligible as a
                        write target; rows above this are treated as headers/samples.
        log:            Logger instance for status messages.
    """
    # Read column A to find the first blank row at or below start_row
    col_a_range = f"'{tab}'!A:A"
    result = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=col_a_range)
        .execute()
    )
    col_a: list[list] = result.get("values", [])

    # col_a is 0-indexed; row numbers are 1-indexed
    next_row = start_row  # default: write at start_row if everything above is blank
    for row_idx in range(start_row - 1, len(col_a)):
        cell_value = col_a[row_idx][0].strip() if col_a[row_idx] else ""
        if cell_value == "":
            next_row = row_idx + 1  # convert 0-index → 1-index
            break
    else:
        # All cells in col A from start_row onward are non-empty
        next_row = len(col_a) + 1

    # Expand the grid if next_row + len(rows) exceeds the current row limit
    sheet_meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in sheet_meta["sheets"]:
        if s["properties"]["title"] == tab:
            current_rows = s["properties"]["gridProperties"]["rowCount"]
            needed = next_row + len(rows) - 1
            if needed > current_rows:
                sheet_id = s["properties"]["sheetId"]
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": [{"appendDimension": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "length": needed - current_rows + 50  # add buffer
                    }}]}
                ).execute()
                log.info("  Expanded '%s' grid to %d rows", tab, needed + 50)
            break

    write_range = f"'{tab}'!A{next_row}"
    body = {"values": rows}
    svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=write_range,
        valueInputOption="USER_ENTERED",
        body=body,
    ).execute()
    log.info("  Wrote %d row(s) to '%s'!A%d", len(rows), tab, next_row)


def get_sheet_id(svc, spreadsheet_id: str, tab: str) -> int:
    """Return the integer sheetId for a named tab.

    Args:
        svc:            An authenticated Google Sheets service object.
        spreadsheet_id: The spreadsheet ID string.
        tab:            The tab name to look up.

    Returns:
        The integer sheetId.

    Raises:
        ValueError: If the tab is not found in the spreadsheet.
    """
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] == tab:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab}' not found in spreadsheet {spreadsheet_id}")


def delete_rows(
    svc,
    spreadsheet_id: str,
    tab: str,
    row_indices: list[int],
    log: logging.Logger,
) -> None:
    """Delete rows by 0-based index from *tab*, processing in reverse order.

    Processes deletions from the highest index to the lowest so that earlier
    indices remain valid as rows are removed.  Protected-cell HttpErrors are
    caught and logged as warnings rather than crashing the script.

    Args:
        svc:            An authenticated Google Sheets service object.
        spreadsheet_id: The spreadsheet ID string.
        tab:            The tab name containing the rows to delete.
        row_indices:    List of 0-based row indices to delete.
        log:            Logger instance for status and warning messages.

    Raises:
        HttpError: Re-raised for any HttpError that is not a protected-cell (403)
                   condition.
    """
    sheet_id = get_sheet_id(svc, spreadsheet_id, tab)

    for idx in sorted(row_indices, reverse=True):
        request = {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": idx,
                    "endIndex": idx + 1,
                }
            }
        }
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [request]},
            ).execute()
            log.info("  Deleted row index %d from '%s'", idx, tab)
        except HttpError as exc:
            if exc.resp.status == 403:
                log.warning(
                    "  Row %d in '%s' is protected — skipping deletion: %s",
                    idx,
                    tab,
                    exc,
                )
            else:
                raise


def remove_from_google_group(
    admin_svc,
    group_email: str,
    member_email: str,
    log: logging.Logger,
) -> None:
    """Remove *member_email* from the Google Group *group_email*.

    A 404 response (member not in the group) is handled gracefully and logged
    as an informational message rather than an error.

    Args:
        admin_svc:    An authenticated Admin SDK Directory service object.
        group_email:  The email address of the Google Group.
        member_email: The email address of the member to remove.
        log:          Logger instance for status messages.
    """
    try:
        admin_svc.members().delete(
            groupKey=group_email, memberKey=member_email
        ).execute()
        log.info("  Removed %s from group %s", member_email, group_email)
    except HttpError as exc:
        if exc.resp.status == 404:
            log.info(
                "  %s was not a member of %s (404) — skipping",
                member_email,
                group_email,
            )
        else:
            raise


# ══════════════════════════════════════════════════════════════════════════════
# Slack
# ══════════════════════════════════════════════════════════════════════════════

def slack_lookup_user(email: str, log: logging.Logger) -> tuple[str | None, bool]:
    """Look up a Slack user by email.

    First tries users.lookupByEmail (fast path, active users only).
    If not found, paginates users.list which includes deactivated accounts.

    Args:
        email: The email address to search for.
        log:   Logger instance.

    Returns:
        (user_id, is_deactivated) if found, or (None, False) if not found
        or if SLACK_USER_TOKEN is not configured.
    """
    if not SLACK_USER_TOKEN:
        return None, False

    # Fast path — active users only
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
        log.error("  [Slack] Lookup error for %s: %s", email, data.get("error"))
        return None, False

    # Fallback — paginate to find deactivated accounts
    log.info(
        "  [Slack] %s not found via lookupByEmail — scanning full user list", email
    )
    target = email.strip().lower()
    cursor = None
    while True:
        params: dict = {"limit": 200}
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
            log.error("  [Slack] users.list error: %s", data.get("error"))
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


def slack_deactivate_api(user_id: str, log: logging.Logger) -> bool:
    """Attempt to deactivate a Slack user via the unofficial users.admin.setInactive
    endpoint.  Mirrors slack_reactivate_api from process_challenge.py.

    This endpoint requires the legacy 'client' scope which is unavailable on modern
    Slack apps (returns missing_scope / needed: client).  Returns False so the caller
    falls back to Playwright.

    Args:
        user_id: The Slack user ID (e.g. "U012AB3CD").
        log:     Logger instance.

    Returns:
        True if deactivation succeeded, False otherwise.
    """
    resp = http_requests.post(
        "https://slack.com/api/users.admin.setInactive",
        data={"token": SLACK_USER_TOKEN, "user": user_id},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        log.info("  [Slack] Deactivated via API: %s", user_id)
        return True
    log.warning(
        "  [Slack] API deactivation unavailable (%s) — falling back to Playwright",
        data.get("error"),
    )
    return False


def slack_deactivate_playwright(email: str, log: logging.Logger) -> bool:
    """Deactivate an active Slack member via the admin panel GUI.

    Mirrors slack_reactivate_playwright from process_challenge.py but for removal:
      1. Login at sealuw.slack.com/sign_in_with_password
      2. Load workspace SPA to establish the full session
      3. Click Admin sidebar → Manage members (opens popup at sealuw.slack.com/admin)
      4. Search for target email (no Inactive filter — user is active)
      5. Click data-qa="table_row_actions_button" (the '...' row action button)
      6. Click 'Deactivate account' from the dropdown
      7. Click the confirm button in the deactivation modal
         (labelled "Deactivate" — NOT "Save" like in the reactivation modal)

    Args:
        email: The email address of the Slack member to deactivate.
        log:   Logger instance.

    Returns:
        True if deactivation succeeded, False otherwise.
    """
    if not SLACK_ADMIN_EMAIL or not SLACK_ADMIN_PASSWORD:
        log.error(
            "  [Slack] SLACK_ADMIN_EMAIL / SLACK_ADMIN_PASSWORD not set "
            "— cannot deactivate via Playwright"
        )
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            # ── Sign in ───────────────────────────────────────────────────────
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
            page.wait_for_load_state("networkidle", timeout=30_000)
            page.wait_for_timeout(4000)

            if "sign_in" in page.url:
                log.error(
                    "  [Slack] Playwright login failed — check "
                    "SLACK_ADMIN_EMAIL / SLACK_ADMIN_PASSWORD in .env"
                )
                page.screenshot(path=str(TMP / "slack_deactivate_login_failed.png"))
                browser.close()
                return False

            # ── Load workspace SPA ────────────────────────────────────────────
            page.goto(f"https://{SLACK_WORKSPACE}/", wait_until="load", timeout=30_000)
            page.wait_for_timeout(10_000)

            # ── Open Manage members via Admin sidebar ─────────────────────────
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
                    path=str(TMP / f"slack_deactivate_debug_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            admin_href = (
                manage_link.get_attribute("href") or f"https://{SLACK_WORKSPACE}/admin"
            )
            try:
                from playwright.sync_api import TimeoutError as PWTimeout
                with context.expect_page(timeout=6000) as popup_info:
                    manage_link.click()
                admin_page = popup_info.value
                admin_page.wait_for_load_state("load", timeout=30_000)
                admin_page.wait_for_timeout(8000)
            except Exception:
                admin_page = page
                page.goto(admin_href, wait_until="load", timeout=30_000)
                page.wait_for_timeout(8000)

            # ── Search for the target email (no Inactive filter needed) ───────
            search = admin_page.query_selector(
                'input[data-qa="workspace-members__table-header-search_input"], '
                'input[placeholder*="ilter by name"], input[type="search"]'
            )
            if not search:
                log.error(
                    "  [Slack] Playwright could not find search input for %s", email
                )
                admin_page.screenshot(
                    path=str(TMP / f"slack_deactivate_debug_{email.split('@')[0]}.png")
                )
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
                log.error(
                    "  [Slack] Playwright could not find row action button for %s", email
                )
                admin_page.screenshot(
                    path=str(TMP / f"slack_deactivate_debug_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            action_btn.click()
            admin_page.wait_for_timeout(2000)

            # ── Click 'Deactivate account' from the dropdown ──────────────────
            deactivate_btn = admin_page.query_selector(
                '[data-qa="deactivate_member_button"]'
            )
            if not deactivate_btn:
                for el in admin_page.query_selector_all(
                    "button, [role='menuitem'], li"
                ):
                    txt = el.inner_text().strip().lower()
                    if ("deactivate account" in txt or txt == "deactivate"
                            or "revoke invitation" in txt):
                        deactivate_btn = el
                        break

            if not deactivate_btn:
                log.error(
                    "  [Slack] Playwright could not find Deactivate button for %s", email
                )
                admin_page.screenshot(
                    path=str(TMP / f"slack_deactivate_debug_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            deactivate_btn.click()
            admin_page.wait_for_timeout(2000)

            # ── Handle confirmation modal ─────────────────────────────────────
            # Slack shows a confirmation dialog.
            # The confirm button is labelled "Deactivate" — NOT "Save" like
            # in the reactivation modal.
            confirm_btn = (
                admin_page.query_selector('[data-qa="deactivate_confirm_button"]')
                or admin_page.query_selector('[data-qa="confirm_button"]')
            )
            if not confirm_btn:
                for btn in admin_page.query_selector_all("button"):
                    txt = btn.inner_text().strip().lower()
                    if txt in ("deactivate", "deactivate account", "confirm",
                               "yes", "remove", "revoke", "revoke invitation"):
                        confirm_btn = btn
                        break

            if not confirm_btn:
                log.error(
                    "  [Slack] Playwright could not find confirmation button "
                    "in deactivation modal for %s",
                    email,
                )
                admin_page.screenshot(
                    path=str(TMP / f"slack_deactivate_no_confirm_{email.split('@')[0]}.png")
                )
                browser.close()
                return False

            confirm_btn.click()
            admin_page.wait_for_timeout(2000)

            log.info("  [Slack] Removed from workspace via Playwright: %s", email)
            browser.close()
            return True

    except Exception as exc:
        log.error("  [Slack] Playwright deactivation failed for %s: %s", email, exc)
        return False


def load_pending_deactivations() -> list[str]:
    """Return emails that failed Slack deactivation in a previous run."""
    if not PENDING_DEACTIVATE_FILE.exists():
        return []
    try:
        data = json.loads(PENDING_DEACTIVATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_pending_deactivations(emails: list[str]) -> None:
    """Persist the pending deactivation list to disk."""
    TMP.mkdir(parents=True, exist_ok=True)
    PENDING_DEACTIVATE_FILE.write_text(json.dumps(emails, indent=2), encoding="utf-8")


def add_pending_deactivation(email: str) -> None:
    """Queue an email for retry on the next run (deduplicates)."""
    pending = load_pending_deactivations()
    if email.lower() not in [e.lower() for e in pending]:
        pending.append(email)
    save_pending_deactivations(pending)


def remove_pending_deactivation(email: str) -> None:
    """Remove a successfully deactivated email from the retry queue."""
    pending = load_pending_deactivations()
    pending = [e for e in pending if e.lower() != email.lower()]
    save_pending_deactivations(pending)


def handle_slack_deactivate(email: str, log: logging.Logger) -> None:
    """Deactivate a Slack member. Skips if already deactivated or not found.

    Decision tree:
      - Token not set              → skip with warning
      - User not found in Slack    → log info, nothing to do
      - Account already deactivated → log info, nothing to do
      - Account active             → deactivate (API first, then Playwright fallback)

    Args:
        email: The email address of the member to deactivate.
        log:   Logger instance.
    """
    if not SLACK_USER_TOKEN:
        log.warning("  [Slack] SLACK_USER_TOKEN not set — skipping Slack deactivation")
        return

    user_id, is_deactivated = slack_lookup_user(email, log)

    if user_id is None:
        log.info("  [Slack] %s not found in workspace — no deactivation needed", email)
        return

    if is_deactivated:
        log.info("  [Slack] %s is already deactivated — no action needed", email)
        return

    # User is active — deactivate
    log.info("  [Slack] Deactivating active account for %s", email)
    if not slack_deactivate_api(user_id, log):
        success = slack_deactivate_playwright(email, log)
        if success:
            remove_pending_deactivation(email)
        else:
            log.warning(
                "  [Slack] Deactivation failed for %s — queued for retry next run", email
            )
            add_pending_deactivation(email)
    else:
        remove_pending_deactivation(email)


# ══════════════════════════════════════════════════════════════════════════════
# Main orchestration
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    log = _setup_logging()
    log.info("=== Clan Cleanup started ===")

    # ── Load config ───────────────────────────────────────────────────────────
    with open(CONFIG, encoding="utf-8") as fh:
        cfg_root = yaml.safe_load(fh)
    cfg = cfg_root["clan_cleanup"]

    clan_life_sheet_id    = cfg["clan_life_sheet_id"]
    associates_tab        = cfg["associates_tab"]
    status_col            = cfg["status_column_index"]   # 10  (K)
    email_col             = cfg["email_column_index"]    # 41  (AP)
    aad_sheet_id          = cfg["aad_sheet_id"]
    ex_communicado_tab    = cfg["ex_communicado_tab"]
    ex_associate_tab      = cfg["ex_associate_tab"]
    affiliates_tab        = cfg["affiliates_tab"]
    active_group_email    = cfg["active_group_email"]
    start_row             = cfg["start_row"]             # 14

    # ── Authenticate ──────────────────────────────────────────────────────────
    log.info("Authenticating (Sheets)…")
    gmail_creds = get_credentials(SCOPES_GMAIL, TOKEN_GMAIL, "sealdirector@gmail.com")
    sheets_svc  = build("sheets", "v4", credentials=gmail_creds)

    log.info("Authenticating (Admin SDK)…")
    admin_creds = get_credentials(SCOPES_ADMIN, TOKEN_ADMIN, "admin@maxalton.com")
    admin_svc   = build("admin", "directory_v1", credentials=admin_creds)

    # ── Retry previously failed Slack deactivations ───────────────────────────
    pending = load_pending_deactivations()
    if pending:
        log.info("Retrying %d previously failed Slack deactivation(s)…", len(pending))
        for email in list(pending):
            log.info("  Retrying: %s", email)
            user_id, is_deactivated = slack_lookup_user(email, log)
            if user_id is None:
                log.info(
                    "  [Slack] %s no longer in workspace — removing from retry queue", email
                )
                remove_pending_deactivation(email)
            elif is_deactivated:
                log.info(
                    "  [Slack] %s is already deactivated — removing from retry queue", email
                )
                remove_pending_deactivation(email)
            else:
                success = slack_deactivate_playwright(email, log)
                if success:
                    log.info("  [Slack] Retry succeeded for %s", email)
                    remove_pending_deactivation(email)
                else:
                    log.warning(
                        "  [Slack] Retry failed again for %s — will try next run", email
                    )

    # ── Read Associates tab ───────────────────────────────────────────────────
    log.info("Reading Associates tab from SEAL Clan Life sheet…")
    all_rows = get_sheet_data(sheets_svc, clan_life_sheet_id, associates_tab)
    log.info("  Total rows read (including headers): %d", len(all_rows))

    # ── Ensure destination tabs exist ─────────────────────────────────────────
    for sheet_id, tab in [
        (aad_sheet_id,        ex_communicado_tab),
        (aad_sheet_id,        ex_associate_tab),
        (clan_life_sheet_id,  affiliates_tab),
    ]:
        ensure_tab_exists(sheets_svc, sheet_id, tab)

    # ── Classify rows ─────────────────────────────────────────────────────────
    # Rows are 0-indexed in the list; spreadsheet rows are 1-indexed.
    # Data rows begin at start_row (1-indexed), so list index = start_row - 1.
    gameover_rows:     list[tuple[int, list]] = []
    ex_associate_rows: list[tuple[int, list]] = []
    affiliate_rows:    list[tuple[int, list]] = []

    data_start_idx = start_row - 1  # 0-based list index of the first data row

    for list_idx, row in enumerate(all_rows):
        if list_idx < data_start_idx:
            continue  # skip header / sample rows

        if len(row) <= status_col:
            continue  # row too short to have a status value

        status = row[status_col].strip().lower()

        if status.startswith("gameover"):
            gameover_rows.append((list_idx, row))
        elif status.startswith("ex-associate"):
            ex_associate_rows.append((list_idx, row))
        elif status.startswith("affiliate"):
            affiliate_rows.append((list_idx, row))

    log.info(
        "Classified: %d GameOver | %d Ex-Associate | %d Affiliate",
        len(gameover_rows),
        len(ex_associate_rows),
        len(affiliate_rows),
    )

    # ── Collect row indices for deletion (processed after all writes) ─────────
    rows_to_delete: list[int] = []

    # ── Process GameOver rows ─────────────────────────────────────────────────
    if gameover_rows:
        log.info("Processing GameOver rows → %s", ex_communicado_tab)
        write_to_next_blank_row(
            sheets_svc,
            aad_sheet_id,
            ex_communicado_tab,
            [r for _, r in gameover_rows],
            start_row,
            log,
        )
        for list_idx, row in gameover_rows:
            member_email = row[email_col].strip() if len(row) > email_col else ""
            if member_email:
                remove_from_google_group(admin_svc, active_group_email, member_email, log)
                handle_slack_deactivate(member_email, log)
            else:
                log.warning(
                    "  Row %d has no email in column AP — skipping group removal and Slack deactivation",
                    list_idx + 1,
                )
            rows_to_delete.append(list_idx)

    # ── Process Ex-Associate rows ─────────────────────────────────────────────
    if ex_associate_rows:
        log.info("Processing Ex-Associate rows → %s", ex_associate_tab)
        write_to_next_blank_row(
            sheets_svc,
            aad_sheet_id,
            ex_associate_tab,
            [r for _, r in ex_associate_rows],
            start_row,
            log,
        )
        for list_idx, row in ex_associate_rows:
            member_email = row[email_col].strip() if len(row) > email_col else ""
            if member_email:
                remove_from_google_group(admin_svc, active_group_email, member_email, log)
                handle_slack_deactivate(member_email, log)
            else:
                log.warning(
                    "  Row %d has no email in column AP — skipping group removal and Slack deactivation",
                    list_idx + 1,
                )
            rows_to_delete.append(list_idx)

    # ── Process Affiliate rows ────────────────────────────────────────────────
    if affiliate_rows:
        log.info("Processing Affiliate rows → %s", affiliates_tab)
        write_to_next_blank_row(
            sheets_svc,
            clan_life_sheet_id,
            affiliates_tab,
            [r for _, r in affiliate_rows],
            start_row,
            log,
        )
        for list_idx, _ in affiliate_rows:
            rows_to_delete.append(list_idx)
        # No group removal for affiliates

    # ── Delete processed rows from Associates ─────────────────────────────────
    if rows_to_delete:
        log.info("Deleting %d processed row(s) from Associates…", len(rows_to_delete))
        delete_rows(sheets_svc, clan_life_sheet_id, associates_tab, rows_to_delete, log)
    else:
        log.info("No rows to delete.")

    log.info("=== Clan Cleanup complete ===")


if __name__ == "__main__":
    main()
