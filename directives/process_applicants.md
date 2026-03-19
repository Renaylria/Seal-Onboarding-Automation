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
   - Column N does not match any keyword (including blank) → **Approved**
6. **Append** approved rows to "Approved" tab; rejected rows to "Rejected" tab
7. **Create tabs** if they don't exist yet (copies header from Current Applicants)
8. **Ensure 10 empty rows** at the bottom of both Approved and Rejected tabs
9. **For each approved row:**
   - Add email (column B) to Google Group via Admin SDK
   - Send approval email via Gmail API (`email.subject` / `email.body` from config)
10. **For each rejected row:**
    - Send rejection email via Gmail API (`rejection_email.subject` / `rejection_email.body` from config)
11. **Log** all actions to `.tmp/process_applicants.log`

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
