#!/usr/bin/env python3
"""
error_notify.py — TEMPORARY: Send error notification email via Gmail API.

This script is a temporary addition for debugging. To remove:
  1. Delete this file
  2. Remove the error_notify calls from run_all.sh
  3. Remove the NOTIFY_EMAIL variable from run_all.sh

Usage:
    python3 error_notify.py <script_name> <error_message>
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEMPORARY — error notification to harrisnakajima@gmail.com
# Remove this file when no longer needed.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import sys
import base64
from pathlib import Path
from datetime import datetime
from email.message import EmailMessage

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from proxy_http import make_http

ROOT = Path(__file__).resolve().parent.parent
TOKEN = ROOT / "token_applicants.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

NOTIFY_EMAIL = "harrisnakajima@gmail.com"  # TEMPORARY recipient
SENDER = "admin@maxalton.com"


def send_error_email(script_name: str, error_msg: str):
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    gmail = build("gmail", "v1", http=AuthorizedHttp(creds, http=make_http()))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = EmailMessage()
    msg["To"] = NOTIFY_EMAIL
    msg["From"] = SENDER
    msg["Subject"] = f"[SEAL Onboarding] Error in {script_name}"
    msg.set_content(
        f"An error occurred in the SEAL onboarding automation.\n\n"
        f"Script:    {script_name}\n"
        f"Time:      {now}\n"
        f"Machine:   Sudoku.local\n\n"
        f"Error output:\n"
        f"{'─' * 60}\n"
        f"{error_msg}\n"
        f"{'─' * 60}\n\n"
        f"Check the logs at:\n"
        f"  ~/Projects/SEAL ONBOARDING AUTOMATION V1/.tmp/\n"
    )

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"[error_notify] Sent error notification to {NOTIFY_EMAIL}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 error_notify.py <script_name> <error_message>")
        sys.exit(1)
    send_error_email(sys.argv[1], sys.argv[2])
