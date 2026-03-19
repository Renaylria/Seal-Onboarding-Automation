# Directive: Process SEAL Applicants

## Goal
Hourly: read new rows from the "Current Applicants" tab of the SEAL Applicants Google Sheet, classify each as Approved or Rejected, copy rows to the correct tab, add approved emails to the SEAL onboarding Google Group, and send approval or rejection emails.

## Inputs
- `config.yaml` — spreadsheet ID, column indices, rejection keywords, Google Group email, approval email template, rejection email template, test override
- Google Sheet: "SEAL Applicants" → tab "Current Applicants"
- OAuth credentials: `credentials.json` + `token.json`

## Script
`execution/process_applicants.py`

Run with:
```
python execution/process_applicants.py
```

## Logic Flow

1. **Load config** from `config.yaml`
2. **Authenticate** via OAuth (Google Sheets + Gmail + Admin SDK)
3. **Read** "Current Applicants" tab — all rows after header
4. **Deduplicate** — collect all emails already in "Approved" and "Rejected" tabs; skip any matching row
5. **Classify** each new row:
   - Column N contains a rejection keyword → **Rejected**
   - Column N is blank → **Skipped** (awaiting reviewer decision — not processed until status is set)
   - Column N has a non-blank value that is not a rejection keyword → **Approved**
6. **Append** approved rows to "Approved" tab; rejected rows to "Rejected" tab
7. **Create tabs** if they don't exist yet (copies header from Current Applicants)
8. **Verify column O header** ("Email Sent") exists in both Approved and Rejected tabs — warns if missing but does NOT create columns
9. **For each approved row:** add email to Google Group via Admin SDK
10. **Quality check** — cross-reference Approved and Rejected tabs; block email sending for any email that appears in both (flags for manual resolution)
11. **Scan Approved tab** for rows where column O is blank → send approval email → write timestamp to column O
12. **Scan Rejected tab** for rows where column O is blank → send rejection email → write timestamp to column O
12. **Ensure 10 empty rows** at the bottom of both tabs
13. **Log** all actions to `.tmp/process_applicants.log`

### Column O — "Email Sent" tracker
Column O acts as the authoritative record of whether an email has been sent for each row.

- **Blank** = email not yet sent → script will send it and write a timestamp (e.g. `Sent 2026-03-18 18:05`)
- **Populated** = email already sent → row is skipped every run

This design is **self-healing**: if a send fails, column O stays blank and the script retries automatically on the next hourly run. It also **backfills** any historical rows in Approved or Rejected that pre-date the email feature — they will receive emails on the next run after this code is deployed.

> ⚠️ **First production run note**: Any existing rows in the Approved or Rejected tabs with a blank column O will trigger emails. Keep `testing.test_email_override` set until you have manually pre-populated column O for historical rows you do *not* want emailed.

### Test override
When `testing.test_email_override` in `config.yaml` is set to a non-empty email address,
**all outgoing emails** (approval and rejection) are redirected to that address.
The real recipient's address is still used for `{email}` placeholder substitution and is
logged alongside the override address. Set to `""` to send to real recipients.

## Column Reference (0-indexed)
| Column | Index | Field |
|--------|-------|-------|
| A | 0 | Timestamp (Google Form) |
| B | 1 | Email address |
| C | 2 | Applicant name (default — adjust `name_column_index` in config) |
| N | 13 | Status / approval decision |
| O | 14 | Email Sent — written by script after successful send (`Sent YYYY-MM-DD HH:MM`) |

## Deduplication Strategy
A row is skipped if its email (column B) already appears in either the "Approved" or "Rejected" tab. This means:
- Safe to run as often as needed — no duplicate processing
- If a tab is manually cleared, re-processing will occur for those rows (acceptable/intentional)

## Required Google OAuth Scopes
These are set in the script and triggered on first auth run:
- `spreadsheets` — read/write Google Sheets
- `gmail.send` — send approval emails
- `admin.directory.group.member` — add members to Google Workspace group

**Admin SDK note:** Requires authentication as a Google Workspace admin (`admin@maxalton.com`). Group must be a Workspace-managed group (`onboarding@maxalton.com`). Standard `@googlegroups.com` groups are not supported by this API.

The script resolves the group email → Cloud Identity resource name once per run, then uses that to add members. A 409 error means the member is already in the group — logged as info, not an error.

## First-Time Setup
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Enable APIs: **Google Sheets API**, **Gmail API**, **Cloud Identity API**
3. Create OAuth 2.0 credentials (Desktop app) → download as `credentials.json`
4. Fill in `config.yaml` — spreadsheet ID, group email, sender email
5. Run the script once manually → browser will open for OAuth authorization
6. `token.json` is created automatically — subsequent runs are silent

## Scheduled Run
The script is set to run hourly via Claude Code's scheduled task system.
To run manually at any time: `python execution/process_applicants.py`

## Learnings & Edge Cases
- **409 on group add**: Member already in group — logged as info, not an error
- **Short rows**: Rows shorter than expected column count are padded with empty strings
- **Blank status**: Treated as Approved (no keyword match)
- **Empty rows in source tab**: Rows with no email in column B are silently skipped
- **Non-email values in column B**: Sheet contains annotation rows (e.g. "Added by A10", "Email" header artifacts) — script validates email format (must contain @) and skips non-email values
- **Duplicate emails in source sheet**: Same email can appear multiple times in Current Applicants — script deduplicates within each run, processing only the first occurrence
- **"Previously Departed" status**: Old relic of past SEAL processes, will not appear in future — treated as Approved (no rejection keyword match), which is correct
- **Test email override**: `testing.test_email_override` in `config.yaml` redirects all outgoing emails to the specified address. Set to `""` to go live. Both approval and rejection emails respect this setting.
- **Column O backfill**: On the first run after deploying column O tracking, all existing rows in Approved/Rejected with blank column O will trigger emails. Keep the test override set and pre-populate historical rows' column O (any non-blank value) before going live.
- **Failed send retry**: If the Gmail API call fails, column O stays blank and the row is retried automatically on the next run. No manual intervention needed.
