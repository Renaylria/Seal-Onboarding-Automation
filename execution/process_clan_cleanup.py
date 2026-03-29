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
from google_auth_httplib2 import AuthorizedHttp
from proxy_http import make_http
from tui_status import set_live, log_run_start, log_run_msg, log_event, log_result
from run_logger import RunLogger
from playwright.sync_api import sync_playwright

# Ground-truth verification (shared module in sudoku-blueprint)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "sudoku-blueprint"))
from verify import verify_group_not_member, verify_slack_deactivated

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


def _find_next_blank_row(
    svc, spreadsheet_id: str, tab: str, start_row: int
) -> int:
    """Return the 1-indexed first blank row in column A at or below start_row."""
    col_a_range = f"'{tab}'!A:A"
    result = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=col_a_range)
        .execute()
    )
    col_a: list[list] = result.get("values", [])
    for row_idx in range(start_row - 1, len(col_a)):
        cell_value = col_a[row_idx][0].strip() if col_a[row_idx] else ""
        if cell_value == "":
            return row_idx + 1
    return max(len(col_a) + 1, start_row)


def write_rows_with_formatting(
    svc,
    src_spreadsheet_id: str,
    src_tab: str,
    src_row_indices: list[int],
    dst_spreadsheet_id: str,
    dst_tab: str,
    start_row: int,
    log: logging.Logger,
) -> int:
    """Copy rows from source to destination preserving all formatting.

    Uses spreadsheets.get(includeGridData=True) to read full CellData and
    updateCells to write — this preserves fonts, colours, alignment, and
    hyperlinks that values().update() silently drops.

    Returns the 1-indexed row number where writing began.
    """
    import copy

    next_row = _find_next_blank_row(svc, dst_spreadsheet_id, dst_tab, start_row)

    # ── Read source rows with full cell data ──────────────────────────────
    src_result = svc.spreadsheets().get(
        spreadsheetId=src_spreadsheet_id,
        ranges=[f"'{src_tab}'"],
        includeGridData=True,
        fields="sheets.data.rowData,sheets.properties.title"
    ).execute()

    all_row_data = []
    for sheet in src_result.get("sheets", []):
        if sheet["properties"]["title"] == src_tab:
            all_row_data = sheet["data"][0].get("rowData", [])
            break

    rows_to_write = []
    for idx in src_row_indices:
        rd = copy.deepcopy(all_row_data[idx]) if idx < len(all_row_data) else {}
        rows_to_write.append(rd)

    # ── Get destination sheet metadata ────────────────────────────────────
    dst_meta = svc.spreadsheets().get(spreadsheetId=dst_spreadsheet_id).execute()
    dst_sheet_id = None
    dst_col_count = None
    dst_row_count = None
    for s in dst_meta["sheets"]:
        if s["properties"]["title"] == dst_tab:
            dst_sheet_id = s["properties"]["sheetId"]
            dst_col_count = s["properties"]["gridProperties"]["columnCount"]
            dst_row_count = s["properties"]["gridProperties"]["rowCount"]
            break
    if dst_sheet_id is None:
        raise ValueError(f"Tab '{dst_tab}' not found in spreadsheet {dst_spreadsheet_id}")

    # ── Expand grid if needed ─────────────────────────────────────────────
    needed = next_row + len(rows_to_write) - 1
    if needed > dst_row_count:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=dst_spreadsheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": dst_sheet_id,
                "dimension": "ROWS",
                "length": needed - dst_row_count + 50
            }}]}
        ).execute()
        log.info("  Expanded '%s' grid to %d rows", dst_tab, needed + 50)

    # ── Trim rows to destination column count ─────────────────────────────
    for rd in rows_to_write:
        rd["values"] = rd.get("values", [])[:dst_col_count]

    # ── Write with full formatting ────────────────────────────────────────
    requests_body = [
        {
            "updateCells": {
                "rows": [rd],
                "fields": "userEnteredValue,userEnteredFormat,textFormatRuns",
                "start": {
                    "sheetId": dst_sheet_id,
                    "rowIndex": next_row - 1 + i,
                    "columnIndex": 0
                }
            }
        }
        for i, rd in enumerate(rows_to_write)
    ]
    svc.spreadsheets().batchUpdate(
        spreadsheetId=dst_spreadsheet_id,
        body={"requests": requests_body}
    ).execute()
    log.info("  Wrote %d row(s) with formatting to '%s'!A%d", len(rows_to_write), dst_tab, next_row)

    # ── Verify write ──────────────────────────────────────────────────────
    verify = svc.spreadsheets().values().get(
        spreadsheetId=dst_spreadsheet_id,
        range=f"'{dst_tab}'!A{next_row}:A{next_row + len(rows_to_write) - 1}"
    ).execute()
    written = verify.get("values", [])
    if len(written) != len(rows_to_write):
        log.error(
            "  [Sheets] VERIFY FAILED: expected %d row(s) at '%s'!A%d, "
            "but read back %d",
            len(rows_to_write), dst_tab, next_row, len(written),
        )
    else:
        log.info("  [Sheets] Verified %d row(s) written to '%s'", len(written), dst_tab)

    return next_row


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

    # Batch all deletions into a single API call (avoids 429 rate limits).
    # Sorted in reverse so that earlier indices remain valid as rows are removed.
    requests_body = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": idx,
                    "endIndex": idx + 1,
                }
            }
        }
        for idx in sorted(row_indices, reverse=True)
    ]
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests_body},
        ).execute()
        log.info(
            "  Deleted %d row(s) from '%s' (indices: %s)",
            len(row_indices), tab, sorted(row_indices, reverse=True),
        )
    except HttpError as exc:
        if exc.resp.status == 403:
            log.warning(
                "  Some rows in '%s' are protected — skipping deletion: %s",
                tab, exc,
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

def slack_lookup_user(email: str, log: logging.Logger) -> tuple[str | None, bool, bool]:
    """Look up a Slack user by email.

    First tries users.lookupByEmail (fast path, active users only).
    If not found, paginates users.list which includes deactivated accounts.

    Args:
        email: The email address to search for.
        log:   Logger instance.

    Returns:
        (user_id, is_deactivated, api_failed) — api_failed is True when
        the lookup could not complete due to a token/API error (callers
        should fall through to Playwright).
    """
    if not SLACK_USER_TOKEN:
        return None, False, True

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
        return user["id"], user.get("deleted", False), False
    if data.get("error") != "users_not_found":
        log.error("  [Slack] Lookup error for %s: %s", email, data.get("error"))
        return None, False, True

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
            return None, False, True
        for member in data.get("members", []):
            profile = member.get("profile", {})
            member_email = profile.get("email", "").strip().lower()
            if member_email == target:
                return member["id"], member.get("deleted", False), False
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    return None, False, False


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


class _AdminAccountError(Exception):
    """Raised when trying to deactivate a Workspace Admin — stops retries."""
    pass


def _slack_deactivate_single(email: str, log: logging.Logger) -> bool:
    """Single attempt to deactivate a Slack member via the admin panel GUI.

    Uses shared slack_login + open_admin_panel, then:
      1. Search for target email (no Inactive filter — user is active)
      2. Hover over row to reveal the '...' action button
      3. Click '...' → 'Deactivate account'
      4. Confirm in the deactivation modal (button labelled "Deactivate")
    """
    from slack_auth import slack_login, open_admin_panel

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        # ── Sign in ───────────────────────────────────────────────────────
        if not slack_login(page, context, log):
            browser.close()
            return False

        # ── Open admin panel ──────────────────────────────────────────────
        admin_page = open_admin_panel(page, context, log)
        if not admin_page:
            browser.close()
            return False

        # ── Search for the target email ───────────────────────────────────
        search = admin_page.query_selector(
            'input[data-qa="workspace-members__table-header-search_input"], '
            'input[placeholder*="ilter by name"], input[type="search"]'
        )
        if not search:
            log.error("  [Slack] Could not find search input for %s", email)
            admin_page.screenshot(
                path=str(TMP / f"slack_deactivate_debug_{email.split('@')[0]}.png")
            )
            browser.close()
            return False

        search.fill(email)
        admin_page.wait_for_timeout(5000)

        # ── Hover over the member row to reveal the '...' button ──────────
        # The action button only renders on hover. Use mouse.move on the
        # name cell (which has a bounding box) to trigger the CSS hover state.
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

        # ── Check if user is an admin (no action button available) ─────────
        is_admin = admin_page.evaluate("""
            () => {
                const row = document.querySelector('[data-qa="workspace-members_table_data_table_row"]');
                return row ? row.textContent.includes('Workspace Admin') || row.textContent.includes('Workspace Owner') : false;
            }
        """)
        if is_admin:
            log.error(
                "  [Slack] %s is a Workspace Admin — cannot deactivate via "
                "admin panel. Requires manual action by another admin.", email
            )
            admin_page.screenshot(
                path=str(TMP / f"slack_deactivate_admin_{email.split('@')[0]}.png")
            )
            browser.close()
            # Return special sentinel to stop retries
            raise _AdminAccountError(email)

        # ── Click the '...' row action button ─────────────────────────────
        action_btn = admin_page.query_selector('[data-qa="table_row_actions_button"]')
        if not action_btn:
            log.error("  [Slack] Could not find row action button for %s", email)
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
            log.error("  [Slack] Could not find Deactivate button for %s", email)
            admin_page.screenshot(
                path=str(TMP / f"slack_deactivate_debug_{email.split('@')[0]}.png")
            )
            browser.close()
            return False

        deactivate_btn.click()
        admin_page.wait_for_timeout(2000)

        # ── Handle confirmation modal ─────────────────────────────────────
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
                "  [Slack] Could not find confirmation button for %s", email
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


def slack_deactivate_playwright(
    email: str, log: logging.Logger, max_retries: int = 3
) -> bool:
    """Deactivate a Slack member with automatic retry on failure.

    Retries up to max_retries times, clearing the saved browser session
    between attempts to force a fresh login.
    """
    from slack_auth import SESSION_FILE as _SESSION_FILE

    for attempt in range(1, max_retries + 1):
        try:
            log.info(
                "  [Slack] Deactivation attempt %d/%d for %s",
                attempt, max_retries, email,
            )
            if _slack_deactivate_single(email, log):
                return True
        except _AdminAccountError:
            log.error(
                "  [Slack] %s is a Workspace Admin — cannot deactivate automatically. "
                "Stopping retries. A human must handle this manually.", email,
            )
            return False
        except Exception as exc:
            log.error(
                "  [Slack] Playwright deactivation error for %s (attempt %d): %s",
                email, attempt, exc,
            )

        if attempt < max_retries:
            log.info("  [Slack] Clearing session and retrying...")
            _SESSION_FILE.unlink(missing_ok=True)

    log.error(
        "  [Slack] All %d deactivation attempts failed for %s", max_retries, email
    )
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

    user_id, is_deactivated, api_failed = slack_lookup_user(email, log)

    if api_failed:
        # API lookup failed (token revoked/expired) — fall through to Playwright
        log.warning("  [Slack] API lookup failed for %s — falling back to Playwright", email)
        success = slack_deactivate_playwright(email, log)
        if not success:
            add_pending_deactivation(email)
        else:
            remove_pending_deactivation(email)
        return

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
            # Check if it was an admin account (no point retrying)
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
    set_live("checking", "Scanning clan life sheet")
    log_run_start("process_clan_cleanup")

    rl = RunLogger("process_clan_cleanup", log)
    rl.__enter__()
    try:
        _run_clan_cleanup(log, rl)
    except Exception as exc:
        import traceback as tb
        rl.log_error("UNKNOWN", str(exc), tb.format_exc())
        raise
    finally:
        rl.__exit__(None, None, None)


def _run_clan_cleanup(log, rl):
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
    slack_skip_emails     = {e.lower() for e in cfg.get("slack_skip_emails", [])}

    # ── Authenticate ──────────────────────────────────────────────────────────
    log.info("Authenticating (Sheets)…")
    gmail_creds = get_credentials(SCOPES_GMAIL, TOKEN_GMAIL, "sealdirector@gmail.com")
    sheets_svc  = build("sheets", "v4", http=AuthorizedHttp(gmail_creds, http=make_http()))

    log.info("Authenticating (Admin SDK)…")
    admin_creds = get_credentials(SCOPES_ADMIN, TOKEN_ADMIN, "admin@maxalton.com")
    admin_svc   = build("admin", "directory_v1", http=AuthorizedHttp(admin_creds, http=make_http()))

    # ── Retry previously failed Slack deactivations ───────────────────────────
    pending = load_pending_deactivations()
    if pending:
        log.info("Retrying %d previously failed Slack deactivation(s)…", len(pending))
        # Remove skip-listed emails from pending queue
        for skip_email in [e for e in pending if e.lower() in slack_skip_emails]:
            log.info("  [Slack] Removing %s from pending queue (in slack_skip_emails)", skip_email)
            remove_pending_deactivation(skip_email)
        pending = [e for e in pending if e.lower() not in slack_skip_emails]
        for email in list(pending):
            log.info("  Retrying: %s", email)
            user_id, is_deactivated, api_failed = slack_lookup_user(email, log)
            if api_failed:
                log.warning("  [Slack] API lookup failed — retrying %s via Playwright", email)
                success = slack_deactivate_playwright(email, log)
                if success:
                    remove_pending_deactivation(email)
                continue
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
                else:
                    log.warning(
                        "  [Slack] Retry failed for %s — removing from queue "
                        "(error logged for human review)", email
                    )
                # Either way, remove from pending to avoid infinite retry loops.
                # Successful deactivations and permanent failures (admin accounts)
                # should not be retried.
                remove_pending_deactivation(email)

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

    grade_col = 12  # Column M — grade/designation (e.g. "6. Ex-Associate")

    for list_idx, row in enumerate(all_rows):
        if list_idx < data_start_idx:
            continue  # skip header / sample rows

        if len(row) <= status_col:
            continue  # row too short to have a status value

        status = row[status_col].strip().lower()
        grade = row[grade_col].strip().lower() if len(row) > grade_col else ""

        # Check both col K (status/ladder) and col M (grade) for triggers
        if status.startswith("gameover") or "gameover" in grade.replace(" ", "").replace("-", ""):
            gameover_rows.append((list_idx, row))
        elif status.startswith("ex-associate") or "ex-associate" in grade.replace(" ", "-"):
            ex_associate_rows.append((list_idx, row))
        elif status.startswith("affiliate") or "affiliate" in grade:
            affiliate_rows.append((list_idx, row))

    log.info(
        "Classified: %d GameOver | %d Ex-Associate | %d Affiliate",
        len(gameover_rows),
        len(ex_associate_rows),
        len(affiliate_rows),
    )

    # ── Collect row indices for deletion (only after verified writes) ────────
    rows_to_delete: list[int] = []
    write_failures: list[int] = []

    def _write_and_track(
        src_indices: list[int],
        dst_sid: str,
        dst_tab: str,
        category: str,
    ) -> bool:
        """Write rows with formatting and return True if verified."""
        wrote_row = write_rows_with_formatting(
            sheets_svc,
            clan_life_sheet_id, associates_tab,
            src_indices,
            dst_sid, dst_tab,
            start_row, log,
        )
        # Verification is done inside write_rows_with_formatting.
        # Double-check by reading back column A at the write location.
        verify = sheets_svc.spreadsheets().values().get(
            spreadsheetId=dst_sid,
            range=f"'{dst_tab}'!A{wrote_row}:A{wrote_row + len(src_indices) - 1}"
        ).execute()
        written = verify.get("values", [])
        if len(written) != len(src_indices):
            log.error(
                "  [Sheets] %s write to '%s' NOT verified — "
                "skipping source row deletion to prevent data loss",
                category, dst_tab,
            )
            return False
        return True

    # ── Process GameOver rows ─────────────────────────────────────────────────
    if gameover_rows:
        log.info("Processing GameOver rows → %s", ex_communicado_tab)
        src_indices = [idx for idx, _ in gameover_rows]
        verified = _write_and_track(src_indices, aad_sheet_id, ex_communicado_tab, "GameOver")

        for list_idx, row in gameover_rows:
            member_email = row[email_col].strip() if len(row) > email_col else ""
            member_name = row[1].strip() if len(row) > 1 else ""
            if member_email:
                set_live("removing", "GameOver — removing", email=member_email, step="group")
                remove_from_google_group(admin_svc, active_group_email, member_email, log)
                gv = verify_group_not_member(admin_svc, active_group_email, member_email)
                log.info("  [Verify] Group removal: %s", gv.detail)
                if member_email.lower() in slack_skip_emails:
                    log.info("  [Slack] SKIPPING deactivation for %s (in slack_skip_emails)", member_email)
                else:
                    set_live("removing", "GameOver — deactivating Slack", email=member_email, step="slack")
                    handle_slack_deactivate(member_email, log)
                    if SLACK_USER_TOKEN:
                        sv = verify_slack_deactivated(member_email, SLACK_USER_TOKEN, "bearer")
                        log.info("  [Verify] Slack deactivated: %s", sv.detail)
                log_event("remove", member_email, "REMOVED", reason="GameOver flag")
                rl.add_note(f"GAMEOVER → Ex-Communicado: {member_name} ({member_email})")
            else:
                log.warning(
                    "  Row %d has no email in column AP — skipping group removal and Slack deactivation",
                    list_idx + 1,
                )
            if verified:
                rows_to_delete.append(list_idx)
            else:
                write_failures.append(list_idx)

    # ── Process Ex-Associate rows ─────────────────────────────────────────────
    if ex_associate_rows:
        log.info("Processing Ex-Associate rows → %s", ex_associate_tab)
        src_indices = [idx for idx, _ in ex_associate_rows]
        verified = _write_and_track(src_indices, aad_sheet_id, ex_associate_tab, "Ex-Associate")

        for list_idx, row in ex_associate_rows:
            member_email = row[email_col].strip() if len(row) > email_col else ""
            member_name = row[1].strip() if len(row) > 1 else ""
            if member_email:
                set_live("removing", "Ex-Associate — removing", email=member_email, step="group")
                remove_from_google_group(admin_svc, active_group_email, member_email, log)
                gv = verify_group_not_member(admin_svc, active_group_email, member_email)
                log.info("  [Verify] Group removal: %s", gv.detail)
                if member_email.lower() in slack_skip_emails:
                    log.info("  [Slack] SKIPPING deactivation for %s (in slack_skip_emails)", member_email)
                else:
                    set_live("removing", "Ex-Associate — deactivating Slack", email=member_email, step="slack")
                    handle_slack_deactivate(member_email, log)
                    if SLACK_USER_TOKEN:
                        sv = verify_slack_deactivated(member_email, SLACK_USER_TOKEN, "bearer")
                        log.info("  [Verify] Slack deactivated: %s", sv.detail)
                log_event("remove", member_email, "REMOVED", reason="Ex-Associate")
                rl.add_note(f"EX-ASSOCIATE → Ex-Associate tab: {member_name} ({member_email})")
            else:
                log.warning(
                    "  Row %d has no email in column AP — skipping group removal and Slack deactivation",
                    list_idx + 1,
                )
            if verified:
                rows_to_delete.append(list_idx)
            else:
                write_failures.append(list_idx)

    # ── Process Affiliate rows ────────────────────────────────────────────────
    if affiliate_rows:
        log.info("Processing Affiliate rows → %s", affiliates_tab)
        src_indices = [idx for idx, _ in affiliate_rows]
        verified = _write_and_track(src_indices, clan_life_sheet_id, affiliates_tab, "Affiliate")

        for list_idx, row in affiliate_rows:
            member_email = row[email_col].strip() if len(row) > email_col else ""
            member_name = row[1].strip() if len(row) > 1 else ""
            if member_email:
                rl.add_note(f"AFFILIATE → Affiliates tab: {member_name} ({member_email})")
            if verified:
                rows_to_delete.append(list_idx)
            else:
                write_failures.append(list_idx)
        # No group removal for affiliates

    # ── Report write failures ─────────────────────────────────────────────────
    if write_failures:
        log.error(
            "  [Sheets] %d row(s) NOT deleted from Associates due to unverified "
            "destination writes: row indices %s",
            len(write_failures), write_failures,
        )

    # ── Delete processed rows from Associates (only verified writes) ──────────
    if rows_to_delete:
        log.info("Deleting %d verified row(s) from Associates…", len(rows_to_delete))
        delete_rows(sheets_svc, clan_life_sheet_id, associates_tab, rows_to_delete, log)
    else:
        log.info("No rows to delete.")

    total_processed = len(gameover_rows) + len(ex_associate_rows) + len(affiliate_rows)
    if total_processed == 0:
        log_run_msg("Clan cleanup: no status triggers found")
        log_result("empty")
        rl.set_rows_processed("0 triggers")
    else:
        log_run_msg(f"Clan cleanup: {len(gameover_rows)} GameOver, {len(ex_associate_rows)} Ex-Assoc, {len(affiliate_rows)} Affiliate")
        log_result("processed")
        rl.set_rows_processed(f"{len(gameover_rows)} GameOver, {len(ex_associate_rows)} Ex-Assoc, {len(affiliate_rows)} Affiliate")
        if gameover_rows:
            rl.add_action(f"{len(gameover_rows)} GameOver → Ex-Communicado")
        if ex_associate_rows:
            rl.add_action(f"{len(ex_associate_rows)} Ex-Associate → Ex-Associate tab")
        if affiliate_rows:
            rl.add_action(f"{len(affiliate_rows)} Affiliate → Affiliates tab")
    if write_failures:
        rl.log_error("SHEET_003", f"{len(write_failures)} unverified write(s) — rows not deleted")
    set_live("idle", "Clan cleanup complete")
    log.info("=== Clan Cleanup complete ===")


if __name__ == "__main__":
    main()
