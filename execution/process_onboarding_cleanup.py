"""
process_onboarding_cleanup.py
-----------------------------
Removes departed SEAL members from the onboarding@maxalton.com Google Group.

Logic:
  1. Read all emails (col AP) from Ex-Communicado and Ex-Associate tabs in
     Clan Life AAD.
  2. Filter out emails already processed (tracked in onboarding_cleanup_processed.json).
  3. Check each new email against the Associates tab of SEAL Clan Life (col AP).
     If a match is found, the member has returned — skip removal.
  4. Remove remaining emails from onboarding@maxalton.com.
  5. Record processed emails in the tracking file.

Run:
  python execution/process_onboarding_cleanup.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

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
from sheets_retry import retry_execute

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent.parent
CONFIG      = ROOT / "config.yaml"
TOKEN_GMAIL = ROOT / "token_gmail.json"   # sealscripting@gmail.com — Sheets access
TOKEN_ADMIN = ROOT / "token_admin.json"   # admin@maxalton.com     — Group management
CREDS       = ROOT / "credentials.json"
TMP         = ROOT / ".tmp"
LOG_FILE    = TMP / "process_onboarding_cleanup.log"
PROCESSED_FILE = ROOT / "onboarding_cleanup_processed.json"

# ── OAuth Scopes ───────────────────────────────────────────────────────────────
SCOPES_GMAIL = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SCOPES_ADMIN = ["https://www.googleapis.com/auth/admin.directory.group.member"]


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

    logger = logging.getLogger("onboarding_cleanup")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


# ══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════════════════════

def get_credentials(scopes: list[str], token_path: Path, hint: str) -> Credentials:
    """Load/refresh OAuth credentials.  Opens the browser for first-run consent."""
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
    """Return all rows from *tab* as a list of lists."""
    result = retry_execute(
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=tab)
    )
    return result.get("values", [])


def load_processed_emails() -> set[str]:
    """Load the set of previously processed emails from the tracking file."""
    if not PROCESSED_FILE.exists():
        return set()
    try:
        with open(PROCESSED_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {e.lower() for e in data}
    except (json.JSONDecodeError, TypeError):
        return set()


def save_processed_emails(emails: set[str]) -> None:
    """Save the set of processed emails to the tracking file."""
    with open(PROCESSED_FILE, "w", encoding="utf-8") as fh:
        json.dump(sorted(emails), fh, indent=2)


def extract_emails_from_tab(
    svc, spreadsheet_id: str, tab: str, email_col: int,
    start_row: int, log: logging.Logger,
) -> set[str]:
    """Read all non-empty emails from a tab's email column (at or below start_row)."""
    rows = get_sheet_data(svc, spreadsheet_id, tab)
    emails: set[str] = set()
    data_start_idx = start_row - 1  # 0-based

    for list_idx, row in enumerate(rows):
        if list_idx < data_start_idx:
            continue
        if len(row) > email_col:
            email = row[email_col].strip()
            if email and "@" in email and " " not in email:
                emails.add(email.lower())
            elif email:
                log.warning("  Skipping invalid email in '%s' row %d: %s", tab, list_idx + 1, email)

    log.info("  Found %d email(s) in '%s'", len(emails), tab)
    return emails


def remove_from_google_group(
    admin_svc,
    group_email: str,
    member_email: str,
    log: logging.Logger,
) -> bool:
    """Remove *member_email* from the Google Group *group_email*.

    Returns True if the member was removed or was already not in the group.
    Returns False on unexpected errors.
    """
    try:
        retry_execute(admin_svc.members().delete(
            groupKey=group_email, memberKey=member_email
        ))
        log.info("  Removed %s from group %s", member_email, group_email)
        return True
    except HttpError as exc:
        if exc.resp.status == 404:
            log.info(
                "  %s was not a member of %s (404) — skipping",
                member_email, group_email,
            )
            return True
        log.error(
            "  Failed to remove %s from %s: %s",
            member_email, group_email, exc,
        )
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    log = _setup_logging()
    log.info("=== Onboarding Cleanup started ===")
    set_live("checking", "Scanning for onboarding group cleanup")
    log_run_start("process_onboarding_cleanup")

    rl = RunLogger("process_onboarding_cleanup", log)
    rl.__enter__()
    try:
        _run_onboarding_cleanup(log, rl)
    except Exception as exc:
        import traceback as tb
        rl.log_error("UNKNOWN", str(exc), tb.format_exc())
        raise
    finally:
        rl.__exit__(None, None, None)


def _run_onboarding_cleanup(log, rl):
    # ── Load config ───────────────────────────────────────────────────────────
    with open(CONFIG, encoding="utf-8") as fh:
        cfg_root = yaml.safe_load(fh)
    cfg = cfg_root["onboarding_cleanup"]
    cc  = cfg_root["clan_cleanup"]  # reuse AAD / Clan Life IDs

    aad_sheet_id       = cc["aad_sheet_id"]
    ex_communicado_tab = cc["ex_communicado_tab"]
    ex_associate_tab   = cc["ex_associate_tab"]
    clan_life_sheet_id = cc["clan_life_sheet_id"]
    associates_tab     = cc["associates_tab"]
    email_col          = cfg["email_column_index"]       # 41 (AP)
    start_row          = cfg["start_row"]                # 14
    onboarding_group   = cfg["onboarding_group_email"]   # onboarding@maxalton.com

    # ── Authenticate ──────────────────────────────────────────────────────────
    log.info("Authenticating (Sheets — read-only)…")
    gmail_creds = get_credentials(SCOPES_GMAIL, TOKEN_GMAIL, "sealscripting@gmail.com")
    sheets_svc  = build("sheets", "v4", http=AuthorizedHttp(gmail_creds, http=make_http()))

    log.info("Authenticating (Admin SDK)…")
    admin_creds = get_credentials(SCOPES_ADMIN, TOKEN_ADMIN, "admin@maxalton.com")
    admin_svc   = build("admin", "directory_v1", http=AuthorizedHttp(admin_creds, http=make_http()))

    # ── Step 1: Gather departed emails from AAD ───────────────────────────────
    log.info("Reading Ex-Communicado tab from Clan Life AAD…")
    ex_comm_emails = extract_emails_from_tab(
        sheets_svc, aad_sheet_id, ex_communicado_tab, email_col, start_row, log,
    )

    log.info("Reading Ex-Associate tab from Clan Life AAD…")
    ex_assoc_emails = extract_emails_from_tab(
        sheets_svc, aad_sheet_id, ex_associate_tab, email_col, start_row, log,
    )

    departed_emails = ex_comm_emails | ex_assoc_emails
    log.info("Total departed emails (Ex-Communicado + Ex-Associate): %d", len(departed_emails))

    # ── Step 2: Filter out already-processed emails ───────────────────────────
    processed = load_processed_emails()
    new_emails = departed_emails - processed
    log.info("New emails to process (not previously handled): %d", len(new_emails))

    if not new_emails:
        log.info("No new departed emails to process.")
        log_run_msg("Onboarding cleanup: no new emails")
        log_result("empty")
        rl.set_rows_processed("0 new")
        set_live("idle", "Onboarding cleanup complete — nothing to do")
        log.info("=== Onboarding Cleanup complete ===")
        return

    # ── Step 3: Check exceptions in SEAL Clan Life Associates ─────────────────
    log.info("Reading Associates tab from SEAL Clan Life for exceptions…")
    active_emails = extract_emails_from_tab(
        sheets_svc, clan_life_sheet_id, associates_tab, email_col, start_row, log,
    )

    exceptions = new_emails & active_emails
    if exceptions:
        log.info(
            "Exceptions found (%d) — these emails are back in Associates, skipping: %s",
            len(exceptions), ", ".join(sorted(exceptions)),
        )

    emails_to_remove = new_emails - active_emails
    log.info("Emails to remove from onboarding group: %d", len(emails_to_remove))

    # ── Step 4: Remove from onboarding Google Group ───────────────────────────
    removed_count = 0
    failed_emails: list[str] = []

    for email in sorted(emails_to_remove):
        set_live("removing", "Removing from onboarding group", email=email, step="group")
        success = remove_from_google_group(admin_svc, onboarding_group, email, log)
        if success:
            removed_count += 1
            log_event("remove", email, "REMOVED", reason="Onboarding group cleanup")
            rl.add_note(f"Removed from onboarding group: {email}")
        else:
            failed_emails.append(email)

    # ── Step 5: Update tracking file ──────────────────────────────────────────
    # Mark all new emails as processed (including exceptions and failures)
    # - Exceptions: processed = True (they were checked; re-checking each run is wasteful)
    # - Failed removals: NOT marked processed — will be retried on next run
    newly_processed = (emails_to_remove - set(failed_emails)) | exceptions
    processed |= newly_processed
    save_processed_emails(processed)
    log.info("Tracking file updated: %d total processed emails", len(processed))

    # ── Summary ───────────────────────────────────────────────────────────────
    summary_parts = []
    if removed_count:
        summary_parts.append(f"{removed_count} removed")
        rl.add_action(f"{removed_count} removed from onboarding group")
    if exceptions:
        summary_parts.append(f"{len(exceptions)} exception(s) skipped")
        rl.add_action(f"{len(exceptions)} exception(s) — still in Associates")
    if failed_emails:
        summary_parts.append(f"{len(failed_emails)} failed")
        rl.log_error(
            "GROUP_001",
            f"Failed to remove {len(failed_emails)} email(s) from onboarding group: "
            + ", ".join(failed_emails),
        )

    summary = "; ".join(summary_parts) if summary_parts else "0 actions"
    rl.set_rows_processed(f"{len(new_emails)} new ({summary})")
    log_run_msg(f"Onboarding cleanup: {summary}")
    log_result("processed")
    set_live("idle", "Onboarding cleanup complete")
    log.info("=== Onboarding Cleanup complete ===")


if __name__ == "__main__":
    main()
