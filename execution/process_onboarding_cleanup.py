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
from datetime import date
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
SCOPES_GMAIL = ["https://www.googleapis.com/auth/spreadsheets"]
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
# Applicant Challenge stale-row cleanup
# ══════════════════════════════════════════════════════════════════════════════

# Google Sheets serial-date epoch: 1899-12-30 is serial 0.
_SHEETS_EPOCH_ORDINAL = date(1899, 12, 30).toordinal()


def _get_applicants_sheet_id(svc, spreadsheet_id: str, tab_name: str) -> int:
    """Resolve the numeric sheetId for *tab_name* (required for deleteDimension)."""
    meta = retry_execute(svc.spreadsheets().get(spreadsheetId=spreadsheet_id))
    for s in meta.get("sheets", []):
        props = s.get("properties", {})
        if props.get("title") == tab_name:
            return props["sheetId"]
    raise RuntimeError(f"Tab '{tab_name}' not found in spreadsheet {spreadsheet_id}")


def cleanup_stale_applicants(sheets_svc, cfg: dict, log: logging.Logger, rl) -> None:
    """Delete rows from the Applicants tab whose Check-In Date (col F) is stale.

    A row is deleted when:
      - its nickname (col A) or full-name (col B) column is non-empty, AND
      - its check-in-date cell is blank OR more than max_age_days old.

    Rows with no name content are treated as empty template rows and skipped.
    """
    spreadsheet_id = cfg["applicant_challenge_sheet_id"]
    tab            = cfg["applicants_tab"]
    checkin_col    = cfg["checkin_column_index"]      # 5 (F)
    nick_col       = cfg["nickname_column_index"]     # 0 (A)
    name_col       = cfg["fullname_column_index"]     # 1 (B)
    start_row      = cfg["start_row"]                 # 10 (1-indexed)
    max_age_days   = cfg["max_age_days"]              # 7

    log.info("Scanning Applicant Challenge 'Applicants' tab for stale rows…")

    sheet_id = _get_applicants_sheet_id(sheets_svc, spreadsheet_id, tab)

    # Read with UNFORMATTED_VALUE so dates come back as Sheets serial numbers.
    result = retry_execute(
        sheets_svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=tab,
            valueRenderOption="UNFORMATTED_VALUE",
        )
    )
    rows = result.get("values", [])

    today_serial = date.today().toordinal() - _SHEETS_EPOCH_ORDINAL
    data_start_idx = start_row - 1  # 0-based

    # Collect (row_index_0based, reason, label) for rows to delete.
    to_delete: list[tuple[int, str, str]] = []
    for idx, row in enumerate(rows):
        if idx < data_start_idx:
            continue

        nickname = (row[nick_col].strip() if len(row) > nick_col and isinstance(row[nick_col], str) else
                    (str(row[nick_col]).strip() if len(row) > nick_col else ""))
        fullname = (row[name_col].strip() if len(row) > name_col and isinstance(row[name_col], str) else
                    (str(row[name_col]).strip() if len(row) > name_col else ""))
        if not nickname and not fullname:
            continue  # empty template row — leave alone

        label = nickname or fullname

        checkin_val = row[checkin_col] if len(row) > checkin_col else ""
        if checkin_val == "" or checkin_val is None:
            to_delete.append((idx, "blank check-in date", label))
            continue

        if isinstance(checkin_val, (int, float)):
            age_days = today_serial - int(checkin_val)
            if age_days > max_age_days:
                to_delete.append((idx, f"{age_days} days old", label))
        else:
            # Non-numeric, non-empty (e.g. stray text) — leave it alone, surface warning.
            log.warning("  Row %d has non-numeric check-in value %r — skipping", idx + 1, checkin_val)

    if not to_delete:
        log.info("No stale applicant rows found.")
        rl.add_note("Applicant cleanup: 0 rows removed")
        return

    log.info("Found %d stale applicant row(s) to delete:", len(to_delete))
    for idx, reason, label in to_delete:
        log.info("  Row %d — %s (%s)", idx + 1, label, reason)

    # Build deleteDimension requests bottom-up so indices stay valid.
    requests = []
    for idx, _reason, _label in sorted(to_delete, key=lambda t: t[0], reverse=True):
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": idx,
                    "endIndex": idx + 1,
                }
            }
        })

    retry_execute(
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        )
    )

    log.info("Deleted %d stale applicant row(s)", len(to_delete))
    rl.add_action(f"{len(to_delete)} stale applicant row(s) deleted")
    for _idx, reason, label in to_delete:
        log_event("remove", label, "REMOVED", reason=f"Applicant cleanup: {reason}")


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
    log.info("Authenticating (Sheets)…")
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

        # Still run applicant-challenge cleanup even if no new departed emails.
        try:
            applicant_cfg = cfg_root.get("applicant_challenge_cleanup")
            if applicant_cfg:
                set_live("checking", "Scanning Applicant Challenge for stale rows")
                cleanup_stale_applicants(sheets_svc, applicant_cfg, log, rl)
        except Exception as exc:
            import traceback as tb
            log.error("Applicant cleanup failed: %s\n%s", exc, tb.format_exc())
            rl.log_error("APPLICANT_CLEANUP_001", f"Applicant cleanup failed: {exc}")

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

    # ── Step 6: Applicant Challenge stale-row cleanup ─────────────────────────
    set_live("checking", "Scanning Applicant Challenge for stale rows")
    try:
        applicant_cfg = cfg_root.get("applicant_challenge_cleanup")
        if applicant_cfg:
            cleanup_stale_applicants(sheets_svc, applicant_cfg, log, rl)
        else:
            log.info("applicant_challenge_cleanup not configured — skipping")
    except Exception as exc:
        import traceback as tb
        log.error("Applicant cleanup failed: %s\n%s", exc, tb.format_exc())
        rl.log_error("APPLICANT_CLEANUP_001", f"Applicant cleanup failed: {exc}")

    set_live("idle", "Onboarding cleanup complete")
    log.info("=== Onboarding Cleanup complete ===")


if __name__ == "__main__":
    main()
