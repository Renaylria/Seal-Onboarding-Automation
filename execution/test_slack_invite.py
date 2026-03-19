#!/usr/bin/env python3
"""
test_slack_invite.py -- Verify the Slack API lookup code paths.

Tests:
  1. auth.test        — token is valid and connected to the right workspace
  2. lookupByEmail    — active user found via fast path
  3. lookupByEmail    — unknown email correctly returns not-found (triggers new-invite path)
  4. users.admin.invite — confirms the API is broken (missing 'client' scope); documents
                          why slack_invite_playwright is used instead

Usage:
    python execution/test_slack_invite.py [known_active_email]

known_active_email must be an email that already exists as an active member in
the workspace (used to verify lookupByEmail works). Required — no default.
For a full end-to-end Playwright invite test use test_slack_invite_playwright.py.
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN", "").strip()
if len(sys.argv) < 2:
    print("Usage: python execution/test_slack_invite.py <known_active_email>")
    print("  known_active_email — an email already active in the workspace (for lookup test)")
    sys.exit(1)

KNOWN_EMAIL      = sys.argv[1]
FAKE_EMAIL       = "does-not-exist-xyzzy-12345@example-nonexistent.invalid"

PASS = "  PASS"
FAIL = "  FAIL"


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"{status}  {label}{suffix}")
    return condition


def api_get(endpoint, **params):
    return requests.get(
        f"https://slack.com/api/{endpoint}",
        params=params,
        headers={"Authorization": f"Bearer {SLACK_USER_TOKEN}"},
        timeout=15,
    ).json()


def api_post(endpoint, **data):
    return requests.post(
        f"https://slack.com/api/{endpoint}",
        data={"token": SLACK_USER_TOKEN, **data},
        timeout=15,
    ).json()


# ── Guard ──────────────────────────────────────────────────────────────────────
if not SLACK_USER_TOKEN:
    print("ERROR: SLACK_USER_TOKEN is not set in .env")
    sys.exit(1)

print(f"Token   : {SLACK_USER_TOKEN[:12]}...")
print(f"Known email : {KNOWN_EMAIL}")
print()

all_passed = True

# ── Test 1: auth.test ──────────────────────────────────────────────────────────
print("Test 1: Token validity (auth.test)")
r = api_get("auth.test")
ok = r.get("ok", False)
if not check("auth.test ok", ok, r.get("error", "")):
    all_passed = False
    print("  Cannot continue — token is invalid.")
    sys.exit(1)
print(f"         user={r.get('user')}  team={r.get('team')}")
print()

# ── Test 2: lookupByEmail — known active user ──────────────────────────────────
print(f"Test 2: lookupByEmail for known active user ({KNOWN_EMAIL})")
r = api_get("users.lookupByEmail", email=KNOWN_EMAIL)
if r.get("ok"):
    user = r["user"]
    uid  = user["id"]
    deleted = user.get("deleted", False)
    check("User found",    True,    f"id={uid}")
    check("Account active (deleted=False)", not deleted, f"deleted={deleted}")
    if deleted:
        all_passed = False
        print(f"  WARNING: {KNOWN_EMAIL} is still deactivated in Slack!")
else:
    err = r.get("error", "unknown")
    check("User found", False, err)
    all_passed = False
    if err == "users_not_found":
        print(f"  {KNOWN_EMAIL} not found via fast path — may need users.list fallback")
    else:
        print(f"  Unexpected error: {err}")
print()

# ── Test 3: lookupByEmail — unknown email (new-invite path) ───────────────────
print(f"Test 3: lookupByEmail for unknown email (should return users_not_found)")
r = api_get("users.lookupByEmail", email=FAKE_EMAIL)
err = r.get("error", "")
if check("users_not_found returned", err == "users_not_found", f"error={err!r}"):
    print("         New-invite path would trigger correctly for this email.")
else:
    all_passed = False
print()

# ── Test 4: users.admin.invite — confirm it requires 'client' scope ────────────
print(f"Test 4: users.admin.invite -> expect missing_scope (legacy client scope required)")
r = api_post("users.admin.invite", email=KNOWN_EMAIL)
err = r.get("error", "")
needed = r.get("needed", "")
if err == "missing_scope" and needed == "client":
    check("missing_scope / needed=client confirmed (expected)", True,
          "invite API unavailable — slack_invite_playwright is used instead")
elif r.get("ok") or err == "already_in_team":
    check("Invite API unexpectedly works on this workspace", True,
          "slack_invite_user could be used as a faster alternative")
else:
    check(f"invite API returned unexpected error", False, f"error={err!r} needed={needed!r}")
    all_passed = False
print()

# ── Summary ────────────────────────────────────────────────────────────────────
if all_passed:
    print("API lookup tests passed.")
    print("New-member invites: use test_slack_invite_playwright.py for full Playwright test.")
else:
    print("One or more tests FAILED. Review output above.")
    sys.exit(1)
