# Directive: SEAL Applicant Challenge — Stage 3 Processing

## Purpose
Monitor the SEAL Applicant Challenge sheet for students who have completed Step 1 of onboarding (signalled by "stage 3" in column P). When detected, propagate them to the SEAL Clan Life sheet and the active Google Group.

## Trigger
Run hourly via Windows Task Scheduler alongside `process_applicants.py`.

## Inputs
| Source | Sheet | Tab | Key Columns |
|--------|-------|-----|-------------|
| SEAL Applicant Challenge | `1tVHLoybyghVJo5w93UmeG5dBSpHqMSeq7ce1t7hnBYo` | Applicants | P (stage), AP (email) |

## Outputs
| Destination | Sheet | Tab |
|-------------|-------|-----|
| Stage 3 archive | SEAL Applicant Challenge (same sheet) | Added to SEAL Life |
| Clan roster | SEAL Clan Life `1k19sS9NfwlVfG7GCCf18LO69pr4reSOTN6v1lY2nykQ` | Associates |
| Google Group | seal-active@maxalton.com | — |

## Processing Logic
1. Read all rows from the **Applicants** tab
2. Skip rows where column P does not equal "stage 3" (case-insensitive)
3. Skip rows whose column AP email already appears in "Added to SEAL Life" (deduplication)
4. For each new qualifying row:
   - Append to **Added to SEAL Life** tab
   - Append to **Associates** tab in SEAL Clan Life
   - Add email to **seal-active@maxalton.com** via Admin SDK
   - Handle Slack membership (see Slack section below)
5. Delete the processed row from the **Applicants** tab

## Authentication
Reuses the same token files as `process_applicants.py`:
- `token_gmail.json` — sealdirector@gmail.com (Sheets read/write)
- `token_admin.json` — admin@maxalton.com (Admin SDK group management)

No additional OAuth setup needed.

## Configuration
All parameters live in `config.yaml` under the `challenge:` key. Edit there — no code changes needed.

## Slack Membership

### Purpose
When a student reaches stage 3, they need access to the SEAL Slack workspace (`sealuw.slack.com`).
The script handles two cases:

| Scenario | Action |
|---|---|
| Email not found in Slack | New member — invite via Playwright admin panel (`slack_invite_playwright`) |
| Email found, account deactivated | Returning member — reactivate (API → Playwright fallback) |
| Email found, account active | Already in workspace — log info, skip |

### Slack Token Setup (one-time)
1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch
2. Name it (e.g. "SEAL Onboarding Bot"), select the `sealuw` workspace
3. Under **OAuth & Permissions** → **User Token Scopes**, add:
   - `admin` (enables invite and reactivation endpoints)
   - `users:read`
   - `users:read.email`
4. Click **Install to Workspace** → authorize as a workspace admin
5. Copy the **User OAuth Token** (starts with `xoxp-`)
6. Add to `.env`:
   ```
   SLACK_USER_TOKEN=xoxp-your-token-here
   SLACK_ADMIN_EMAIL=admin@sealuw...
   SLACK_ADMIN_PASSWORD=your-password
   ```

### New-Member Invites: Playwright-only
The `users.admin.invite` API endpoint requires the legacy `client` scope (`needed: client` error on test). This scope is not available on modern Slack apps. New-member invites are handled entirely by `slack_invite_playwright`:
1. Opens the admin panel popup (same login/SPA/popup flow as reactivation)
2. Clicks **"Invite People"** button
3. Clicks `[data-qa="invite_modal_select-input"]` (contenteditable div) and types the email, then presses Tab to create the email chip
4. Clicks **"Send"** (exact text match)
5. If Send is disabled, logs a warning and saves a debug screenshot — a disabled Send indicates Slack flagged the address (already a member, blocked domain, etc.)

### Reactivation: Two-Tier Approach
- **Tier 1 (API)**: Attempts `users.admin.setActive` with the user token. Works on some free workspaces.
- **Tier 2 (Playwright fallback)**: If the API returns an error, Playwright automates the Slack admin panel GUI.
- On Playwright failure, a debug screenshot is saved to `.tmp/slack_debug_<username>.png` for diagnosis.

### Confirmed Playwright Flow (7 steps)
The following flow was validated end-to-end. Each decision reflects a hard-won debugging lesson.

1. **Login** at `sealuw.slack.com/sign_in_with_password`
   - Use **minimal** `p.chromium.launch(headless=True)` with no extra args and `browser.new_context()` with no modifications. Anti-detection args (`--disable-blink-features=AutomationControlled`) and custom contexts break React rendering — the login form inputs never appear.
   - Use `fill()` for the email field and `keyboard.type(password, delay=50)` for the password field (`fill()` alone doesn't fire React's `onChange` on the password field).
   - Wait with `wait_for_load_state("networkidle")` after page load, then `wait_for_load_state("networkidle")` again after submit.

2. **Load workspace SPA** by navigating to `sealuw.slack.com/`
   - After password login the browser lands at `ssb/redirect`, which does NOT establish the full session required by the admin panel. Visiting the workspace root triggers the redirect to `app.slack.com/client/...` and boots the SPA.
   - Use `wait_until="load"` (not `"networkidle"` — the SPA holds persistent WebSocket connections that prevent `networkidle` from ever firing).
   - Wait an additional 10 seconds (`wait_for_timeout(10000)`) for the SPA to fully initialize.

3. **Open Manage members via Admin sidebar** (captures the new popup tab)
   - Click the `"Admin"` button in the sidebar, then find the `"Manage members"` anchor link.
   - This link opens `sealuw.slack.com/admin` in a **new browser tab**. Use `context.expect_page()` to capture it.
   - **Do NOT navigate directly** to `/admin/members` or `/admin/deactivated_members` — without the SPA context these URLs trigger a React error boundary ("There's been a glitch").
   - Wait `wait_for_load_state("load")` + 8 seconds on the popup.

4. **Apply Inactive billing status filter via JavaScript**
   - Click the `"Filter"` button to open the filter panel, then use `admin_page.evaluate()` to call `el.click()` on the `"Inactive"` label/checkbox.
   - Native Playwright `.click()` fails with a "ReactModalPortal intercepts pointer events" error. JavaScript evaluation bypasses Playwright's actionability checks entirely.
   - Press `Escape` to close the filter panel after selection.

5. **Search for the target email**
   - Use `input[data-qa="workspace-members__table-header-search_input"]` (or fallback to `input[placeholder*="ilter by name"]`).
   - Wait 5 seconds after `fill()` for the table to re-render with filtered results.

6. **Click the `...` row action button**
   - Selector: `[data-qa="table_row_actions_button"]` (fallback: `[aria-label*="Actions for"]`).

7. **Click "Activate account" from the dropdown**
   - Slack's menu item text is **"Activate account"** — NOT "Reactivate". Search for `"activate account"` (case-insensitive) in `button, [role='menuitem'], li` elements.

8. **Handle the "Activate account" confirmation modal**
   - After clicking "Activate account", Slack shows a confirmation modal titled "Activate account" with:
     - A **"Regular Member"** radio button (the only account type option on the free plan)
     - A **"Save"** button and a "Cancel" button
   - Click the radio button to select "Regular Member", then click **"Save"**.
   - **Critical**: the confirm button says **"Save"** — NOT "Activate", "Confirm", "Yes", or "Reactivate". Any code that doesn't match "Save" will silently fail to complete the activation.

### First-time Playwright run
Install the Playwright browser binaries once:
```
playwright install chromium
```

## Edge Cases & Learnings
- Column AP (index 41) is the email column — it is far right and rows may be shorter than expected; the script pads rows before indexing
- "Added to SEAL Life" tab is created automatically if it does not exist (header row copied from Applicants tab)
- "Associates" tab in SEAL Clan Life is also created automatically if absent
- `seal-active@maxalton.com` must be a Google Workspace group managed by admin@maxalton.com
- **Slack token not set**: If `SLACK_USER_TOKEN` is missing from `.env`, the Slack step is skipped with a warning — all other steps still run normally
- **`already_in_team` on invite**: Treated as success (member is already in the workspace)

### Playwright-specific Learnings
- **Anti-detection args break React**: Do not pass `--disable-blink-features=AutomationControlled`, custom user agents, or `add_init_script()` — they prevent Slack's React login form from rendering (inputs appear as 0 in the DOM).
- **OneTrust cookie banner**: Slack's login page displays a cookie consent overlay, but `fill()` accesses DOM elements directly and works through it — no need to dismiss OneTrust.
- **`networkidle` never fires on workspace root**: The SPA holds persistent WebSocket connections. Always use `wait_until="load"` for workspace/admin pages.
- **Direct `/admin` URL navigation causes React crash**: Only `sealuw.slack.com/admin` opened as a popup (new tab) from the SPA sidebar works correctly. Direct `page.goto("/admin/...")` hits a React error boundary because the SPA context is missing.
- **ReactModal blocks native Playwright click**: Filter checkboxes inside the filter panel are wrapped in a `ReactModalPortal` that intercepts pointer events. Use `page.evaluate()` with `el.click()` in JavaScript instead.
- **Button text is "Activate account"**: The action menu item for reactivating a deactivated user says "Activate account", not "Reactivate". Always match case-insensitively with `"activate account" in txt.lower()`.
- **`data-qa="table_row_actions_button"`**: Reliable selector for the `...` row action button on member rows in the admin panel.
- **Confirmation modal "Save" button**: After clicking "Activate account" from the dropdown, a modal appears asking you to choose account type (only "Regular Member" on free plan) and click **"Save"**. This is easy to miss — the button text is "Save", not any activation-related word. Missing this click means the activation is never submitted and the account stays deactivated even though the script appears to succeed.
- **Invite "To:" field is contenteditable, not `<input>`**: `[data-qa="invite_modal_select-input"]` is a `<div contenteditable="true">`. Use `.click()` + `keyboard.type()` + `keyboard.press("Tab")` to create the email chip. Standard `fill()` does not work on contenteditable elements.
- **users.admin.invite requires legacy `client` scope**: This unofficial API endpoint returns `{"error": "missing_scope", "needed": "client"}` on modern Slack apps regardless of what scopes are granted. Do not use it — use `slack_invite_playwright` instead.
