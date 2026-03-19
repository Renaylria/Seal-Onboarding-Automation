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
from pathlib import Path
from email.message import EmailMessage

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

import yaml
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parent.parent
CONFIG     = ROOT / "config.yaml"
TOKEN_GMAIL = ROOT / "token_gmail.json"   # sealdirector@gmail.com — sheets + gmail
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


def append_rows(svc, spreadsheet_id: str, tab: str, rows: list):
    """Append rows to a tab, inserting new rows below existing content."""
    svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


def ensure_min_empty_rows(svc, spreadsheet_id: str, tab: str, num_cols: int, min_empty: int):
    """Guarantee at least min_empty blank rows at the bottom of a tab."""
    data = get_sheet_data(svc, spreadsheet_id, tab)

    # Count trailing empty rows
    trailing = 0
    for row in reversed(data):
        if any(str(c).strip() for c in row):
            break
        trailing += 1

    needed = min_empty - trailing
    if needed > 0:
        append_rows(svc, spreadsheet_id, tab, [[""] * num_cols for _ in range(needed)])


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
               test_override: str = ""):
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
    """
    actual_to = test_override if test_override else recipient
    try:
        body = body_template.format(name=name or recipient, email=recipient)
        msg = EmailMessage()
        msg["To"] = actual_to
        msg["From"] = sender
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        gmail_svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        if test_override:
            log.info(f"  [Email] Sent to: {actual_to} (test override — real recipient: {recipient})")
        else:
            log.info(f"  [Email] Sent to: {actual_to}")
    except Exception as e:
        log.error(f"  [Email] Failed for {actual_to}: {e}")


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

    s              = cfg["sheets"]
    sid            = s["spreadsheet_id"]
    current_tab    = s["current_applicants_tab"]
    approved_tab   = s["approved_tab"]
    rejected_tab   = s["rejected_tab"]
    email_col      = s["email_column_index"]
    status_col     = s["status_column_index"]
    name_col       = s["name_column_index"]
    min_empty      = s["min_empty_rows"]
    keywords       = cfg["rejection_keywords"]
    group_email    = cfg["google_group"]["email"]
    email_cfg      = cfg["email"]
    rejection_email_cfg = cfg["rejection_email"]
    test_override  = cfg.get("testing", {}).get("test_email_override", "").strip()

    if test_override:
        log.info(f"TEST MODE: all outgoing emails will be sent to {test_override}")

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
    num_cols  = len(header)

    # Collect already-processed emails (deduplication)
    processed = (
        get_emails_in_tab(sheets_svc, sid, approved_tab, email_col)
        | get_emails_in_tab(sheets_svc, sid, rejected_tab, email_col)
    )
    log.info(f"Previously processed emails: {len(processed)}")

    # Ensure destination tabs exist (creates them with header if missing)
    ensure_tab_exists(sheets_svc, sid, approved_tab, header)
    ensure_tab_exists(sheets_svc, sid, rejected_tab, header)

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

        if is_rejected(status, keywords):
            new_rejected.append((row, email, name))
            log.info(f"REJECTED  {email}  (status='{status}')")
        else:
            new_approved.append((row, email, name))
            log.info(f"APPROVED  {email}  (status='{status}')")

    log.info(f"New this run → approved: {len(new_approved)}, rejected: {len(new_rejected)}")

    # ── Write approved rows + side effects ────────────────────────────────────
    if new_approved:
        append_rows(sheets_svc, sid, approved_tab, [r for r, _, _ in new_approved])
        for _, email, name in new_approved:
            add_to_google_group(admin_svc, group_email, email, log)
            send_email(gmail_svc, email_cfg["sender"], email, name,
                       email_cfg["subject"], email_cfg["body"], log,
                       test_override=test_override)

    # ── Write rejected rows + send rejection email ─────────────────────────────
    if new_rejected:
        append_rows(sheets_svc, sid, rejected_tab, [r for r, _, _ in new_rejected])
        for _, email, name in new_rejected:
            send_email(gmail_svc, email_cfg["sender"], email, name,
                       rejection_email_cfg["subject"], rejection_email_cfg["body"], log,
                       test_override=test_override)

    # ── Maintain 10 empty rows in both tabs ────────────────────────────────────
    for tab in [approved_tab, rejected_tab]:
        try:
            ensure_min_empty_rows(sheets_svc, sid, tab, num_cols, min_empty)
        except Exception as e:
            log.error(f"Failed to pad empty rows in '{tab}': {e}")

    log.info("Run complete.")


if __name__ == "__main__":
    main()
