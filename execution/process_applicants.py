#!/usr/bin/env python3
"""
process_applicants.py — SEAL Applicant Processing

Reads "Current Applicants" from the SEAL Applicants Google Sheet, classifies
each new row as Approved or Rejected based on column N status keywords, copies
rows to the appropriate tab, adds approved emails to Google Group, and sends
approval emails.

Deduplication: rows whose email already appears in the Approved or Rejected tab
are skipped, ensuring no duplicate processing across runs.

Usage:
    python execution/process_applicants.py
"""

import re
import sys
import logging
import base64
from datetime import datetime
from pathlib import Path
from email.message import EmailMessage

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def col_to_letter(n: int) -> str:
    """Convert a 0-based column index to a spreadsheet column letter (A, B, …, Z, AA, AB, …)."""
    result, n = "", n + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result

import yaml
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parent.parent
CONFIG     = ROOT / "config.yaml"
TOKEN_GMAIL = ROOT / "token_applicants.json"  # sealdirector@gmail.com — sheets + gmail.send
# Separate from token_gmail.json (used by process_challenge/clan_cleanup for sheets-only)
# so scope differences between scripts never cause one to silently strip the other's grants.
TOKEN_ADMIN = ROOT / "token_admin.json"   # admin@maxalton.com — group management
CREDS      = ROOT / "credentials.json"
TMP        = ROOT / ".tmp"
LOG_FILE   = TMP / "process_applicants.log"

# ── OAuth Scopes ───────────────────────────────────────────────────────────────
# sealdirector@gmail.com — reads/writes sheet and sends email
SCOPES_GMAIL = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]
# admin@maxalton.com — manages Google Workspace group membership
SCOPES_ADMIN = [
    "https://www.googleapis.com/auth/admin.directory.group.member",
]


# ══════════════════════════════════════════════════════════════════════════════
# Auth
# ══════════════════════════════════════════════════════════════════════════════

def get_credentials(scopes: list, token_path: Path, hint: str = "") -> Credentials:
    """Load or refresh OAuth credentials. Opens browser on first run.
    hint: shown in log so user knows which account to sign into.
    """
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

    Uses values().update() with an explicit A{row} range instead of values().append()
    so the destination is always column A — append() can place data to the right of
    an existing table when the sheet has wide data in other columns.

    Returns the 1-indexed row number where writing began.
    """
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
        # All rows from start_row downward have data — write after last
        next_row = max(len(col_a) + 1, start_row)

    svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A{next_row}",
        valueInputOption="USER_ENTERED",
        body={"values": rows}
    ).execute()
    log.info(f"  [Sheets] Wrote {len(rows)} row(s) to '{tab}' starting at row {next_row}")
    return next_row



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
# Classification
# ══════════════════════════════════════════════════════════════════════════════

def is_rejected(status: str, keywords: list) -> bool:
    """Return True if status contains any rejection keyword (case-insensitive)."""
    low = status.strip().lower()
    return any(kw.strip().lower() in low for kw in keywords)


# ══════════════════════════════════════════════════════════════════════════════
# Google Group (Admin SDK — works for Google Workspace groups)
# ══════════════════════════════════════════════════════════════════════════════

def add_to_google_group(admin_svc, group_email: str, member_email: str, log):
    """Add member_email to a Workspace Google Group via Admin SDK Directory API."""
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
# Email
# ══════════════════════════════════════════════════════════════════════════════

def send_email(gmail_svc, sender: str, recipient: str, name: str,
               subject: str, body_template: str, log,
               test_override: str = "") -> bool:
    """Send an email via Gmail API using the configured template.

    Args:
        gmail_svc:      Authenticated Gmail API client.
        sender:         From address (e.g. admin@maxalton.com).
        recipient:      Real recipient email address.
        name:           Recipient name used in {name} placeholder.
        subject:        Email subject line.
        body_template:  Body string supporting {name} and {email} placeholders.
        log:            Logger instance.
        test_override:  When non-empty, redirects the email to this address
                        instead of the real recipient (for testing). Set via
                        testing.test_email_override in config.yaml.

    Returns:
        True if the email was sent successfully, False otherwise.
        The caller should only mark column O as sent when True is returned.
    """
    actual_to = test_override if test_override else recipient
    try:
        class SafeDict(dict):
            """Return unknown keys unchanged so stray braces in user data never crash."""
            def __missing__(self, key):
                return "{" + key + "}"
        body = body_template.format_map(SafeDict(name=name or recipient, email=recipient))
        msg = EmailMessage()
        msg["To"] = actual_to
        msg["From"] = sender
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        gmail_svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        if test_override:
            log.info(f"  [Email] Sent to: {actual_to} (test override - real recipient: {recipient})")
        else:
            log.info(f"  [Email] Sent to: {actual_to}")
        return True
    except Exception as e:
        log.error(f"  [Email] Failed for {actual_to}: {e}")
        return False


def get_rows_needing_email(svc, spreadsheet_id: str, tab: str,
                           email_col: int, name_col: int,
                           email_sent_col: int,
                           whitelist: set | None = None) -> list:
    """Return rows in a tab that have not yet had an email sent.

    Scans every data row (skipping the header) and returns those where
    the email_sent column (column O) is blank or missing.  This covers
    both rows just appended this run and any historical rows that were
    added before the email feature existed.

    Args:
        svc:            Authenticated Sheets API client.
        spreadsheet_id: Google Sheet ID.
        tab:            Tab name to scan (e.g. "Approved" or "Rejected").
        email_col:      0-based column index for email address.
        name_col:       0-based column index for recipient name.
        email_sent_col: 0-based column index for the "Email Sent" marker (column O).
        whitelist:      When non-empty, only rows whose email (lowercased) is in this
                        set are returned. Non-whitelisted rows are left untouched
                        (column O stays blank) until the whitelist is cleared.

    Returns:
        List of (row_number_1indexed, email, name) tuples — one per unsent row.
    """
    try:
        data = get_sheet_data(svc, spreadsheet_id, tab)
    except HttpError:
        return []

    result = []
    for i, row in enumerate(data):
        if i == 0:
            continue  # skip header row
        row_number = i + 1  # 1-indexed for Sheets API
        if len(row) <= email_col or not row[email_col].strip():
            continue
        email = row[email_col].strip()
        if not EMAIL_RE.match(email):
            continue
        # Only include rows where the sent marker is blank or absent
        sent = row[email_sent_col].strip() if len(row) > email_sent_col else ""
        if sent:
            continue
        # Enforce whitelist when active
        if whitelist and email.lower() not in whitelist:
            continue
        name = row[name_col].strip() if len(row) > name_col else ""
        result.append((row_number, email, name))
    return result


def batch_mark_emails_sent(svc, spreadsheet_id: str,
                           marks: list, email_sent_col: int, log):
    """Write sent timestamps to column O for all successfully sent emails in one API call.

    Batching all writes into a single batchUpdate avoids the Sheets API
    write-quota limit (60 writes/minute) that fires when marking rows individually.

    Args:
        svc:            Authenticated Sheets API client.
        spreadsheet_id: Google Sheet ID.
        marks:          List of (tab, row_number_1indexed) tuples to mark.
        email_sent_col: 0-based column index to write into (column O = 14).
        log:            Logger instance.
    """
    if not marks:
        return
    col_letter = col_to_letter(email_sent_col)
    timestamp = datetime.now().strftime("Sent %Y-%m-%d %H:%M")
    data = [
        {"range": f"'{tab}'!{col_letter}{row_number}", "values": [[timestamp]]}
        for tab, row_number in marks
    ]
    try:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data}
        ).execute()
        log.info(f"  [Sheets] Marked {len(marks)} row(s) as email sent in column {col_letter}")
    except Exception as e:
        log.error(f"  [Sheets] Failed to batch-mark email sent: {e}")


def ensure_column_header(svc, spreadsheet_id: str, tab: str,
                         col_idx: int, header_text: str, log):
    """Write a header label to the specified column in row 1 if it is blank.

    Args:
        svc:            Authenticated Sheets API client.
        spreadsheet_id: Google Sheet ID.
        tab:            Tab name.
        col_idx:        0-based column index (e.g. 14 for column O).
        header_text:    Label to write (e.g. "Email Sent").
        log:            Logger instance.
    """
    try:
        data = get_sheet_data(svc, spreadsheet_id, tab)
    except HttpError:
        return
    header_row = data[0] if data else []
    current = header_row[col_idx].strip() if len(header_row) > col_idx else ""
    if not current:
        col_letter = col_to_letter(col_idx)
        cell = f"'{tab}'!{col_letter}1"
        svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell,
            valueInputOption="USER_ENTERED",
            body={"values": [[header_text]]}
        ).execute()
        log.info(f"  [Sheets] Set column {col_letter} header in '{tab}' to '{header_text}'")


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
    log.info("SEAL applicant processing - run started")

    # Load config
    with open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    s                = cfg["sheets"]
    sid              = s["spreadsheet_id"]
    current_tab      = s["current_applicants_tab"]
    approved_tab     = s["approved_tab"]
    rejected_tab     = s["rejected_tab"]
    email_col        = s["email_column_index"]
    status_col       = s["status_column_index"]
    name_col         = s["name_column_index"]
    email_sent_col   = s["email_sent_column_index"]
    start_row        = s["start_row"]
    keywords         = cfg["rejection_keywords"]
    group_email      = cfg["google_group"]["email"]
    email_cfg        = cfg["email"]
    rejection_email_cfg = cfg["rejection_email"]
    test_override    = cfg.get("testing", {}).get("test_email_override", "").strip()
    test_whitelist   = {
        e.strip().lower()
        for e in cfg.get("testing", {}).get("test_whitelist", [])
        if e.strip()
    }

    if test_override:
        log.info(f"TEST MODE: all outgoing emails will be sent to {test_override}")
    if test_whitelist:
        log.info(f"TEST WHITELIST active: emails will only be sent to {sorted(test_whitelist)}")

    # Build API clients — two separate auth accounts
    gmail_creds = get_credentials(SCOPES_GMAIL, TOKEN_GMAIL, hint="sealdirector@gmail.com")
    admin_creds = get_credentials(SCOPES_ADMIN, TOKEN_ADMIN, hint="admin@maxalton.com")
    sheets_svc  = build("sheets", "v4", credentials=gmail_creds)
    gmail_svc   = build("gmail", "v1", credentials=gmail_creds)
    admin_svc   = build("admin", "directory_v1", credentials=admin_creds)

    # Read source data
    all_rows = get_sheet_data(sheets_svc, sid, current_tab)
    if len(all_rows) < 2:
        log.info("No data rows in Current Applicants. Nothing to do.")
        return

    header    = all_rows[0]
    data_rows = all_rows[1:]

    # Collect already-processed emails (deduplication)
    processed = (
        get_emails_in_tab(sheets_svc, sid, approved_tab, email_col)
        | get_emails_in_tab(sheets_svc, sid, rejected_tab, email_col)
    )
    log.info(f"Previously processed emails: {len(processed)}")

    # Ensure destination tabs exist (creates them with header if missing)
    ensure_tab_exists(sheets_svc, sid, approved_tab, header)
    ensure_tab_exists(sheets_svc, sid, rejected_tab, header)

    # Ensure column O is labelled in both tabs
    for tab in [approved_tab, rejected_tab]:
        ensure_column_header(sheets_svc, sid, tab, email_sent_col, "Email Sent", log)

    # Classify new rows
    new_approved = []   # list of (row, email, name)
    new_rejected = []
    seen_this_run = set()

    for row in data_rows:
        max_col = max(email_col, status_col, name_col)
        padded  = row + [""] * max(0, max_col + 1 - len(row))

        email = padded[email_col].strip()
        if not email:
            continue                              # skip blank rows
        if not EMAIL_RE.match(email):
            log.debug(f"Skipping non-email value in column B: '{email}'")
            continue                              # skip header artifacts / annotation rows
        if email.lower() in processed:
            continue                              # already handled in a prior run
        if email.lower() in seen_this_run:
            log.warning(f"Duplicate in source sheet, skipping second occurrence: {email}")
            continue                              # duplicate row in current sheet
        seen_this_run.add(email.lower())

        status = padded[status_col].strip()
        name   = padded[name_col].strip() if len(padded) > name_col else ""

        if not status or is_rejected(status, keywords):
            new_rejected.append((row, email, name))
            log.info(f"REJECTED  {email}  (status='{status}')")
        else:
            new_approved.append((row, email, name))
            log.info(f"APPROVED  {email}  (status='{status}')")

    log.info(f"New this run -> approved: {len(new_approved)}, rejected: {len(new_rejected)}")

    # ── Write new rows to destination tabs ────────────────────────────────────
    # Strip column O from source rows before writing — source rows may carry
    # a non-blank column O value (e.g. from a previous backfill run), which
    # would make the email scanner think the email was already sent.
    def clear_col(rows, col_idx):
        result = []
        for r in rows:
            r = list(r)
            if len(r) > col_idx:
                r[col_idx] = ""
            result.append(r)
        return result

    if new_approved:
        write_to_next_blank_row(sheets_svc, sid, approved_tab,
                                clear_col([r for r, _, _ in new_approved], email_sent_col),
                                start_row, log)
        for _, email, name in new_approved:
            add_to_google_group(admin_svc, group_email, email, log)

    if new_rejected:
        write_to_next_blank_row(sheets_svc, sid, rejected_tab,
                                clear_col([r for r, _, _ in new_rejected], email_sent_col),
                                start_row, log)

    # ── Send emails: scan both tabs for rows with blank column O ───────────────
    # This handles both rows just appended this run AND any historical rows that
    # were added before the email feature existed (self-healing backfill).
    # All column O marks are batched into a single API call per tab to stay
    # within the Sheets write-quota limit (60 writes/minute).
    for tab, subject, body in [
        (approved_tab,  email_cfg["subject"],            email_cfg["body"]),
        (rejected_tab,  rejection_email_cfg["subject"],  rejection_email_cfg["body"]),
    ]:
        pending = get_rows_needing_email(
            sheets_svc, sid, tab, email_col, name_col, email_sent_col,
            whitelist=test_whitelist or None
        )
        if pending:
            log.info(f"  [Email] {len(pending)} unsent row(s) found in '{tab}'")
        marks = []
        for row_number, email, name in pending:
            sent = send_email(
                gmail_svc, email_cfg["sender"], email, name,
                subject, body, log, test_override=test_override
            )
            if sent:
                marks.append((tab, row_number))
        batch_mark_emails_sent(sheets_svc, sid, marks, email_sent_col, log)

    log.info("Run complete.")


if __name__ == "__main__":
    main()
