"""
setup_slack_gmail_token.py — One-time OAuth setup for sealonboardingauto@gmail.com.

Creates token_slack_gmail.json with gmail.readonly scope, used by slack_auth.py
to auto-fetch Slack browser verification codes.

Two-phase remote-friendly flow (no PKCE, no local server):
  Phase 1: python3 execution/setup_slack_gmail_token.py url
            → prints the auth URL to open in your browser
  Phase 2: python3 execution/setup_slack_gmail_token.py exchange <CODE>
            → exchanges the auth code for a token and saves it
"""

import json
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from oauthlib.oauth2 import WebApplicationClient
import requests

ROOT = Path(__file__).resolve().parent.parent
CREDS = ROOT / "credentials.json"
TOKEN_SLACK_GMAIL = ROOT / "token_slack_gmail.json"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def _load_client_config():
    data = json.loads(CREDS.read_text())
    cfg = data.get("installed") or data.get("web")
    return cfg["client_id"], cfg["client_secret"]


def cmd_url():
    client_id, _ = _load_client_config()
    client = WebApplicationClient(client_id)
    url = client.prepare_request_uri(
        AUTH_ENDPOINT,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        access_type="offline",
        prompt="consent",
    )
    print("=" * 70)
    print("Open this URL in your browser and sign in as sealonboardingauto@gmail.com:\n")
    print(url)
    print("\n" + "=" * 70)
    print("\nAfter authorizing, Google will display an authorization code.")
    print("Then run:")
    print(f"  python3 execution/setup_slack_gmail_token.py exchange <CODE>")


def cmd_exchange(code: str):
    client_id, client_secret = _load_client_config()

    resp = requests.post(TOKEN_ENDPOINT, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })

    if resp.status_code != 200:
        print(f"ERROR: Token exchange failed ({resp.status_code})")
        print(resp.text)
        return

    token_data = resp.json()
    creds = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri=TOKEN_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    TOKEN_SLACK_GMAIL.write_text(creds.to_json())
    print(f"Token saved to: {TOKEN_SLACK_GMAIL}")
    print(f"Valid: {creds.valid}")
    print(f"Has refresh token: {bool(creds.refresh_token)}")
    print("slack_auth.py can now auto-fetch Slack verification codes from Gmail.")


def cmd_check():
    if not TOKEN_SLACK_GMAIL.exists():
        print("No token file found. Run 'url' first.")
        return
    creds = Credentials.from_authorized_user_file(str(TOKEN_SLACK_GMAIL), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_SLACK_GMAIL.write_text(creds.to_json())
    print(f"Valid: {creds.valid}")
    print(f"Expired: {creds.expired}")
    print(f"Has refresh token: {bool(creds.refresh_token)}")


def main():
    if not CREDS.exists():
        print(f"ERROR: credentials.json not found at {CREDS}")
        return

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 execution/setup_slack_gmail_token.py url")
        print("  python3 execution/setup_slack_gmail_token.py exchange <CODE>")
        print("  python3 execution/setup_slack_gmail_token.py check")
        return

    cmd = sys.argv[1]
    if cmd == "url":
        cmd_url()
    elif cmd == "exchange":
        if len(sys.argv) < 3:
            print("ERROR: Provide the authorization code as an argument")
            return
        cmd_exchange(sys.argv[2])
    elif cmd == "check":
        cmd_check()
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
