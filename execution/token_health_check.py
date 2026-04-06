#!/usr/bin/env python3
"""
token_health_check.py — Pre-flight check for all OAuth tokens.

Tests each token by attempting a refresh. If any token is invalid,
sends an alert email (if possible) and exits with code 1 so
run_all.sh can abort the pipeline early.

Usage:
    python3 token_health_check.py
    Exit code 0 = all tokens healthy
    Exit code 1 = one or more tokens are bad (details on stdout)
"""

import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

ROOT = Path(__file__).resolve().parent.parent

TOKENS = {
    "token_gmail.json": "Sheets (sealscripting)",
    "token_applicants.json": "Sheets + Gmail (sealscripting)",
    "token_admin.json": "Admin API (admin@maxalton.com)",
}

def check_token(token_path: Path, label: str) -> str | None:
    """Try to load and refresh a token. Returns error string or None if healthy."""
    if not token_path.exists():
        return f"MISSING: {token_path.name} does not exist"
    try:
        creds = Credentials.from_authorized_user_file(str(token_path))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif not creds.refresh_token:
            return f"NO REFRESH TOKEN: {token_path.name} has no refresh_token"
        # Token loaded and is valid (or was refreshed successfully)
        # Save back in case it was refreshed
        if creds.token:
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        return None
    except Exception as e:
        return f"INVALID: {token_path.name} — {e}"


def main():
    failures = []
    for filename, label in TOKENS.items():
        token_path = ROOT / filename
        error = check_token(token_path, label)
        if error:
            failures.append((label, error))
            print(f"  FAIL  {label}: {error}")
        else:
            print(f"  OK    {label}")

    if failures:
        print(f"\n{len(failures)} token(s) failed health check. Pipeline will not run.")
        # Try to send alert email using error_notify (may itself fail if
        # token_applicants.json is the broken one)
        try:
            from error_notify import send_error_email
            detail = "\n".join(f"- {label}: {err}" for label, err in failures)
            send_error_email(
                "token_health_check",
                f"OAuth token health check failed. The pipeline was NOT run.\n\n{detail}\n\n"
                f"Re-authorize tokens with:\n"
                f"  python3 refresh_gmail_token.py <token_file>\n"
            )
        except Exception:
            print("  (Could not send alert email — token_applicants.json may also be broken)")
        sys.exit(1)
    else:
        print("\nAll tokens healthy.")
        sys.exit(0)


if __name__ == "__main__":
    main()
