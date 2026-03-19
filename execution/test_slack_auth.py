"""
test_slack_auth.py — Test the full Slack login flow via slack_auth.slack_login().

Launches a headless Playwright browser, attempts login, handles verification
code auto-fetch from Gmail, and reports success/failure.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from playwright.sync_api import sync_playwright
from slack_auth import slack_login

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test_slack_auth")

print("Starting Slack login test...\n")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()

    success = slack_login(page, context, log)

    print(f"\nFinal URL: {page.url}")
    print(f"Page title: {page.title()}")

    # Take a screenshot for verification
    tmp = Path(__file__).resolve().parent.parent / ".tmp"
    tmp.mkdir(exist_ok=True)
    screenshot = tmp / "slack_auth_test_result.png"
    page.screenshot(path=str(screenshot))
    print(f"Screenshot saved: {screenshot}")

    browser.close()

if success:
    print("\nSUCCESS: Slack login worked!")
else:
    print("\nFAILED: Slack login did not succeed. Check screenshot and logs above.")
    sys.exit(1)
