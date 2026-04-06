# Directive: Onboarding Group Cleanup (`process_onboarding_cleanup.py`)

## Purpose

Automatically remove departed SEAL members from the `onboarding@maxalton.com`
Google Group.  The script cross-references Ex-Communicado and Ex-Associate tabs
in Clan Life AAD against the Associates tab in SEAL Clan Life to ensure members
who have returned are not incorrectly removed.

---

## Data Sources

| Source | Sheet ID (config key) | Tab(s) | Column |
|---|---|---|---|
| Clan Life AAD | `clan_cleanup.aad_sheet_id` | Ex-Communicado, Ex-Associate | AP (index 41) |
| SEAL Clan Life | `clan_cleanup.clan_life_sheet_id` | Associates | AP (index 41) |

Data rows begin at row 14 (`start_row` = 14; rows 1-13 are header/sample rows).

---

## Logic Flow

### Step 1: Gather departed emails
Read all emails from column AP of the **Ex-Communicado** and **Ex-Associate**
tabs in Clan Life AAD.  These are members who have left the lab.

### Step 2: Filter already-processed emails
Load `onboarding_cleanup_processed.json` (project root).  Any email already in
this set is skipped — it was handled on a previous run.

### Step 3: Check for exceptions
Read all emails from column AP of the **Associates** tab in SEAL Clan Life.
If a departed email also appears here, the member has returned and is still
active — skip removal.  These exceptions are logged and marked as processed.

### Step 4: Remove from onboarding group
For each remaining email, remove from `onboarding@maxalton.com` via the
Admin SDK.  A 404 (member not in group) is logged as info and treated as
success — the member was already removed.

### Step 5: Update tracking file
Mark all successfully processed emails (removed + exceptions) in the tracking
file.  Failed removals are **not** marked — they will be retried on the next run.

---

## Tracking File

- **Path**: `onboarding_cleanup_processed.json` (project root)
- **Format**: JSON array of lowercase email strings, sorted alphabetically
- **Behaviour**:
  - Emails successfully removed from the group → added to the set
  - Emails found as exceptions (still in Associates) → added to the set
  - Emails that failed removal (API error) → NOT added; retried next run

This ensures only genuinely new entries in the AAD tabs are processed each run,
keeping API calls minimal as the tabs grow.

---

## Google Group Removal

- Target group: `onboarding_group_email` (config: `onboarding@maxalton.com`)
- Uses the same `token_admin.json` (admin@maxalton.com) credentials as
  `process_clan_cleanup.py`
- 404 responses (member not in group) are logged as info, not errors
- Unexpected API errors are logged and the email is left unprocessed for retry

---

## Sheets Access

This script uses **read-only** Sheets scope (`spreadsheets.readonly`).  It never
writes to, creates tabs in, or deletes rows from any Google Sheet.

---

## Edge Cases

| Scenario | Behaviour |
|---|---|
| Email in AAD but also in Associates | Exception — skipped and marked processed |
| Email not in onboarding group (404) | Logged as info; marked processed |
| API error on group removal | Logged as error; NOT marked processed (retried next run) |
| Tracking file missing or corrupt | Treated as empty set — all AAD emails processed from scratch |
| No new emails to process | Script exits cleanly after logging "No new departed emails" |
| AAD tab missing | Script crashes with a clear error (tab should always exist) |

---

## How to Run

```
python execution/process_onboarding_cleanup.py
```

Logs are written to `.tmp/process_onboarding_cleanup.log` and mirrored to stdout.

Runs as the **5th and final script** in `run_all.sh`, after `process_slack_audit.py`.
Must run after `process_clan_cleanup.py` so that newly departed members are already
written to the AAD tabs before this script scans them.

---

## Configuration

Parameters live in `config.yaml` under `onboarding_cleanup`.  AAD and Clan Life
sheet IDs / tab names are reused from the `clan_cleanup` key.

| Key | Purpose |
|---|---|
| `email_column_index` | 0-based column index for email (AP = 41) |
| `start_row` | First data row (1-indexed); rows above are skipped |
| `onboarding_group_email` | Google Group to remove members from |

---

## Rate Limiting

- All Google API calls use `sheets_retry.retry_execute()` with exponential
  backoff on 429 errors (5s → 10s → 20s → 40s, up to 4 retries)
- The script runs sequentially after the other four scripts in `run_all.sh`,
  providing natural stagger time between API-heavy operations
- Sheets reads are batched per-tab (one API call per tab, not per row)
- Group removals are individual API calls (Admin SDK does not support batch
  member deletion)

---

## Learnings / Known Constraints

*(None yet — update as issues are discovered.)*
