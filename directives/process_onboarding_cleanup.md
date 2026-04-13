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

### Step 6: Applicant Challenge stale-row cleanup
Scan the **Applicants** tab of SEAL Applicant Challenge
(`1tVHLoybyghVJo5w93UmeG5dBSpHqMSeq7ce1t7hnBYo`) starting at `start_row` (row
10) and delete any row where:

- column A (nickname) or column B (full name) is non-empty (i.e. it's a real
  applicant row, not an empty template row), AND
- column F (Check-In Date) is blank, OR more than `max_age_days` (7) old.

Column F is displayed as `m/d` but stored as a Google Sheets serial number
(days since 1899-12-30). Values are read with `UNFORMATTED_VALUE` so the
script can compute age arithmetically: `age_days = today_serial - f_serial`,
delete when `age_days > 7`. Exactly 7 days old is kept.

Rows where both A and B are blank are treated as empty template rows and
left alone — this protects the pre-filled template rows at the bottom of the
tab as well as the header block above row 10.

Deletes are batched into a single `batchUpdate` call with `deleteDimension`
requests sorted bottom-up so row indices remain valid during the batch.

Failures in this step are logged but do not fail the whole script (the group
cleanup steps above are the primary responsibility of this script).

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

This script uses **read/write** Sheets scope (`spreadsheets`). The Google Group
cleanup steps are read-only, but the Applicant Challenge stale-row cleanup
(Step 6) deletes rows from the Applicants tab via `batchUpdate` →
`deleteDimension`. It reuses `token_gmail.json`, which already has write scope
from `process_challenge.py`.

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

Applicant Challenge cleanup parameters live under `applicant_challenge_cleanup`:

| Key | Purpose |
|---|---|
| `applicant_challenge_sheet_id` | SEAL Applicant Challenge spreadsheet ID |
| `applicants_tab` | Tab name — `Applicants` |
| `checkin_column_index` | 0-based col index for Check-In Date (F = 5) |
| `nickname_column_index` / `fullname_column_index` | 0-based indices used to detect "real applicant" rows (A = 0, B = 1) |
| `start_row` | First data row (1-indexed); 10 for this tab |
| `max_age_days` | Rows older than this are deleted (7) |

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
