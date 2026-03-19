"""
slack_auth.py — Shared Slack Playwright authentication helper.

Handles the "We don't recognize this browser" verification challenge by
automatically fetching the 6-digit code from the sealonboardingauto@gmail.com
inbox via the Gmail API.

Also provides open_admin_panel() which handles the full SPA → admin popup flow
used by both invite, reactivate, and deactivate functions.

Usage:
    from slack_auth import slack_login, open_admin_panel

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        if not slack_login(page, context, log):
            # handle failure
            ...
        admin_page = open_admin_panel(page, context, log)
        if not admin_page:
            # handle failure
            ...
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google_auth_httplib2 import AuthorizedHttp
from proxy_http import make_http

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
CREDS = ROOT / "credentials.json"
TOKEN_SLACK_GMAIL = ROOT / "token_slack_gmail.json"
TMP = ROOT / ".tmp"
TMP.mkdir(exist_ok=True)
SESSION_FILE = TMP / "slack_browser_state.json"

SLACK_ADMIN_EMAIL = os.getenv("SLACK_ADMIN_EMAIL", "").strip()
SLACK_ADMIN_PASSWORD = os.getenv("SLACK_ADMIN_PASSWORD", "").strip()
SLACK_WORKSPACE = "sealuw.slack.com"

SCOPES_GMAIL_READ = ["https://www.googleapis.com/auth/gmail.readonly"]


def _get_gmail_credentials() -> Credentials | None:
    """Get OAuth credentials for sealonboardingauto@gmail.com (gmail.readonly)."""
    creds = None
    if TOKEN_SLACK_GMAIL.exists():
        creds = Credentials.from_authorized_user_file(
            str(TOKEN_SLACK_GMAIL), SCOPES_GMAIL_READ
        )
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_SLACK_GMAIL.write_text(creds.to_json())
    if creds and creds.valid:
        return creds
    if not CREDS.exists():
        return None
    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDS), SCOPES_GMAIL_READ
    )
    creds = flow.run_local_server(port=0, open_browser=False)
    TOKEN_SLACK_GMAIL.write_text(creds.to_json())
    return creds


def _fetch_slack_code(log: logging.Logger, max_wait: int = 30) -> str | None:
    """Fetch the latest Slack verification code from Gmail.

    Polls for up to `max_wait` seconds for a new message from Slack
    containing a 6-digit code.
    """
    creds = _get_gmail_credentials()
    if not creds:
        log.error("  [SlackAuth] No Gmail credentials for code retrieval")
        return None

    gmail = build(
        "gmail", "v1",
        http=AuthorizedHttp(creds, http=make_http()),
    )

    start = time.time()
    while time.time() - start < max_wait:
        results = (
            gmail.users()
            .messages()
            .list(
                userId="me",
                q="from:slack.com subject:\"Slack security code\" newer_than:1d",
                maxResults=1,
            )
            .execute()
        )
        messages = results.get("messages", [])
        if messages:
            msg = (
                gmail.users()
                .messages()
                .get(userId="me", id=messages[0]["id"], format="full")
                .execute()
            )
            snippet = msg.get("snippet", "")
            match = re.search(r"\b(\d{6})\b", snippet)
            if match:
                log.info("  [SlackAuth] Found verification code in email")
                return match.group(1)

            payload = msg.get("payload", {})
            body_data = ""
            if payload.get("body", {}).get("data"):
                body_data = base64.urlsafe_b64decode(
                    payload["body"]["data"]
                ).decode("utf-8", errors="replace")
            else:
                for part in payload.get("parts", []):
                    if part.get("mimeType", "").startswith("text/"):
                        data = part.get("body", {}).get("data", "")
                        if data:
                            body_data += base64.urlsafe_b64decode(
                                data
                            ).decode("utf-8", errors="replace")

            match = re.search(r"\b(\d{6})\b", body_data)
            if match:
                log.info("  [SlackAuth] Found verification code in email body")
                return match.group(1)

        time.sleep(3)

    log.error("  [SlackAuth] Timed out waiting for Slack verification code")
    return None


def _is_past_login(page) -> bool:
    """Return True if the page has moved past the sign-in page."""
    url = page.url.lower()
    return "sign_in" not in url and SLACK_WORKSPACE.split(".")[0] in url


def slack_login(page, context, log: logging.Logger) -> bool:
    """Log in to Slack via Playwright, handling browser verification automatically.

    Uses 'load' wait states (never 'networkidle') because Slack's SPA holds
    persistent WebSocket connections that prevent networkidle from firing.

    Args:
        page:    Playwright page object.
        context: Playwright browser context (for saving state).
        log:     Logger instance.

    Returns:
        True if login succeeded (page is now past the sign-in page).
    """
    if not SLACK_ADMIN_EMAIL or not SLACK_ADMIN_PASSWORD:
        log.error(
            "  [SlackAuth] SLACK_ADMIN_EMAIL / SLACK_ADMIN_PASSWORD not set"
        )
        return False

    # ── Try loading saved session ────────────────────────────────────────────
    if SESSION_FILE.exists():
        try:
            context.add_cookies(json.loads(SESSION_FILE.read_text()).get("cookies", []))
            log.info("  [SlackAuth] Loaded saved browser session")
        except Exception:
            pass

    # ── Navigate to sign-in page ─────────────────────────────────────────────
    page.goto(f"https://{SLACK_WORKSPACE}/sign_in_with_password",
              wait_until="load", timeout=30_000)
    page.wait_for_timeout(3000)

    # If saved session redirected us past login, we're done
    if _is_past_login(page):
        log.info("  [SlackAuth] Saved session still valid — already logged in")
        state = context.storage_state()
        SESSION_FILE.write_text(json.dumps(state))
        return True

    # ── Fill and submit login form ───────────────────────────────────────────
    email_input = page.query_selector('input[data-qa="login_email"]')
    if not email_input:
        log.error("  [SlackAuth] Login form not found on page")
        page.screenshot(path=str(TMP / "slack_auth_no_form.png"))
        return False

    email_input.fill(SLACK_ADMIN_EMAIL)
    page.click('input[type="password"]')
    page.keyboard.type(SLACK_ADMIN_PASSWORD, delay=50)

    sign_in_btn = (
        page.query_selector('button[data-qa="signin_button"]')
        or page.query_selector('button[type="submit"]')
    )
    if sign_in_btn:
        sign_in_btn.click()
    # Don't wait for load state — Slack does client-side redirects that
    # may never fire a new 'load' event. Just wait for the page to settle.
    page.wait_for_timeout(8000)

    # ── Check for browser verification challenge ────────────────────────────
    title = page.title().lower()
    if "authentication code" in title or "recognize" in title:
        log.info("  [SlackAuth] Browser verification required — fetching code from Gmail...")
        code = _fetch_slack_code(log)
        if not code:
            page.screenshot(path=str(TMP / "slack_auth_no_code.png"))
            return False

        inputs = page.query_selector_all(
            'input[type="text"], input[type="number"], input[inputmode="numeric"]'
        )
        if len(inputs) >= 6:
            for i, digit in enumerate(code):
                inputs[i].fill(digit)
        elif len(inputs) == 1:
            inputs[0].fill(code)
        else:
            page.keyboard.type(code, delay=50)

        page.wait_for_timeout(6000)

        if "sign_in" in page.url:
            err_title = page.title().lower()
            if "incorrect" in err_title or "authentication code" in err_title:
                log.error("  [SlackAuth] Verification code was rejected")
                page.screenshot(path=str(TMP / "slack_auth_code_rejected.png"))
                return False

    # ── Verify login succeeded ───────────────────────────────────────────────
    if "sign_in" in page.url:
        log.error("  [SlackAuth] Login failed — still on sign-in page")
        page.screenshot(path=str(TMP / "slack_auth_login_failed.png"))
        return False

    # ── Save session for future runs ─────────────────────────────────────────
    state = context.storage_state()
    SESSION_FILE.write_text(json.dumps(state))
    log.info("  [SlackAuth] Login successful — session saved")
    return True


def open_admin_panel(page, context, log: logging.Logger):
    """Navigate from logged-in Slack to the admin Manage Members panel.

    Handles: SPA boot → Admin sidebar → Manage members popup.
    Uses 'load' wait states throughout (never 'networkidle').

    Args:
        page:    Playwright page (must already be logged in via slack_login).
        context: Playwright browser context.
        log:     Logger instance.

    Returns:
        The admin panel page object, or None on failure.
    """
    # ── Load workspace SPA ────────────────────────────────────────────────────
    page.goto(f"https://{SLACK_WORKSPACE}/", wait_until="load", timeout=30_000)
    page.wait_for_timeout(10_000)

    # ── Click Admin sidebar button ────────────────────────────────────────────
    admin_clicked = False
    for btn in page.query_selector_all("button"):
        if btn.inner_text().strip() == "Admin":
            btn.click()
            admin_clicked = True
            break
    if not admin_clicked:
        log.error("  [SlackAuth] Could not find Admin button in sidebar")
        page.screenshot(path=str(TMP / "slack_no_admin_btn.png"))
        return None
    page.wait_for_timeout(2000)

    # ── Find Manage members link ──────────────────────────────────────────────
    manage_link = None
    for el in page.query_selector_all("a"):
        if "Manage members" in el.inner_text().strip():
            manage_link = el
            break
    if not manage_link:
        log.error("  [SlackAuth] Could not find Manage members link")
        page.screenshot(path=str(TMP / "slack_no_manage_link.png"))
        return None

    # ── Open admin panel (captures popup tab) ─────────────────────────────────
    admin_href = manage_link.get_attribute("href") or f"https://{SLACK_WORKSPACE}/admin"
    try:
        with context.expect_page(timeout=6000) as popup_info:
            manage_link.click()
        admin_page = popup_info.value
        admin_page.wait_for_load_state("load", timeout=30_000)
        admin_page.wait_for_timeout(8000)
    except Exception:
        admin_page = page
        page.goto(admin_href, wait_until="load", timeout=30_000)
        page.wait_for_timeout(8000)

    log.info("  [SlackAuth] Admin panel loaded: %s", admin_page.url)
    return admin_page
