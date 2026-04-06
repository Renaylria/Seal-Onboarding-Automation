#!/usr/bin/env python3
"""
Refresh a Google OAuth token — terminal-only, no browser needed on this machine.

Usage:  python3 refresh_gmail_token.py [token_file]
        Defaults to token_gmail.json if no argument given.

Flow:
  1. Prints an auth URL — open it on any device (phone, laptop).
  2. Sign in with the correct Google account.
  3. After granting access, the browser will redirect to a localhost URL
     that won't load (that's expected). Copy the FULL URL from the address
     bar and paste it back here.
  4. The script exchanges the code and saves the new token.
"""
import os, sys
from urllib.parse import urlparse, parse_qs

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "execution")

from google_auth_oauthlib.flow import InstalledAppFlow

token_file = sys.argv[1] if len(sys.argv) > 1 else "token_gmail.json"

# Account hints so the user knows which account to sign into
account_hints = {
    "token_gmail.json": "sealscripting@gmail.com",
    "token_applicants.json": "sealscripting@gmail.com",
    "token_admin.json": "admin@maxalton.com",
}

scopes_map = {
    "token_gmail.json": ["https://www.googleapis.com/auth/spreadsheets"],
    "token_applicants.json": [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/gmail.send",
    ],
    "token_admin.json": [
        "https://www.googleapis.com/auth/admin.directory.group.member",
    ],
}
scopes = scopes_map.get(token_file, ["https://www.googleapis.com/auth/spreadsheets"])

REDIRECT_URI = "http://localhost:8090/"

flow = InstalledAppFlow.from_client_secrets_file(
    "credentials.json", scopes, redirect_uri=REDIRECT_URI
)
auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")

hint = account_hints.get(token_file, "")
print(f"\n{'='*60}")
print(f"  Refreshing: {token_file}")
if hint:
    print(f"  Sign in as: {hint}")
print(f"{'='*60}")
print(f"\n1) Open this URL in any browser:\n")
print(f"   {auth_url}\n")
print(f"2) Sign in and grant access.")
print(f"3) The browser will redirect to a page that won't load.")
print(f"   Copy the FULL URL from the address bar and paste it below.\n")

redirect_url = input("Paste the redirect URL here: ").strip()

# Extract the authorization code from the redirect URL
parsed = urlparse(redirect_url)
code = parse_qs(parsed.query).get("code", [None])[0]
if not code:
    print("ERROR: Could not find 'code' parameter in the URL.")
    print("Make sure you copied the full URL from the browser address bar.")
    sys.exit(1)

flow.fetch_token(code=code)
creds = flow.credentials

with open(token_file, "w") as f:
    f.write(creds.to_json())
print(f"\n{token_file} saved!")
