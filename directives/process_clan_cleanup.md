# Directive: Clan Life Cleanup (`process_clan_cleanup.py`)

## Purpose

Automatically route departing or reclassified SEAL members out of the Associates
tab of the SEAL Clan Life Google Sheet.  The script reads column K for status
triggers, copies the row to the correct destination tab, removes the member from
the active Google Group where required, and deletes the row from Associates.
This keeps Associates clean and removes access for members who are no longer active.

---

## Source

| Field | Value |
|---|---|
| Sheet | SEAL Clan Life (`clan_life_sheet_id` in config) |
| Tab | Associates (`associates_tab` in config) |
| Status column | Column K (0-based index 10, `status_column_index`) |
| Email column | Column AP (0-based index 41, `email_column_index`) |
| Data rows | Row 14 onward (`start_row` = 14; rows 1–13 are sample/header rows) |

---

## Status Routing

The script performs a **case-insensitive `startswith` match** on the column K value.

| Status trigger | Destination sheet | Destination tab | Remove from group? | Deactivate Slack? |
|---|---|---|---|---|
| `gameover` | Clan Life AAD (`aad_sheet_id`) | Ex-Communicado | Yes | Yes |
| `ex-associate` | Clan Life AAD (`aad_sheet_id`) | Ex-Associate | Yes | Yes |
| `affiliate` | SEAL Clan Life (`clan_life_sheet_id`) | Affiliates | No | No |

Rows with a blank or unrecognised status value in column K are ignored.

---

## Google Group Removal

- Applies only to **GameOver** and **Ex-Associate** rows.
- The member's email is read from **column AP** (index 41).
- The group is `active_group_email` (config: `seal-active@maxalton.com`).
- A 404 response (member not in the group) is logged as info and skipped —
  it is not treated as an error.
- If the email cell is blank, group removal is skipped with a warning.

---

## Slack Deactivation

Applies only to **GameOver** and **Ex-Associate** rows, immediately after group removal.

### Decision tree (`handle_slack_deactivate`)

| Condition | Action |
|---|---|
| `SLACK_USER_TOKEN` not set | Skip with warning |
| Email not found in workspace | Log info — nothing to deactivate |
| Account already deactivated | Log info — no action needed |
| Account active | Deactivate (API → Playwright fallback) |

### API path (`slack_deactivate_api`)

Tries `users.admin.setInactive`.  This endpoint requires the legacy `client` scope
which is not available on modern Slack apps (returns `missing_scope` / `needed: client`).
Expected to fail; Playwright is the working path.

### Playwright path (`slack_deactivate_playwright`)

Confirmed working flow:
1. Login at `sealuw.slack.com/sign_in_with_password`
2. Load workspace SPA to establish the full session
3. Click Admin sidebar → Manage members (opens popup at `sealuw.slack.com/admin`)
4. Search for target email — **no Inactive filter** (user is active)
5. Click `data-qa="table_row_actions_button"` (the `...` row action button)
6. Click **"Deactivate account"** from the dropdown
7. Click the confirm button in the deactivation modal
   — button is labelled **"Deactivate"** (contrast with reactivation which uses **"Save"**)

### Test script

```
python execution/test_slack_deactivate.py active@example.com
```

Screenshots written to `.tmp/deactivate_*.png` for debugging.

---

## Write Behaviour

- Destination tabs are created automatically if they do not exist.
- Rows are written using `write_to_next_blank_row`: the script reads column A
  of the destination tab, finds the first blank row at or below `start_row`,
  and writes there using `values().update()`.  This avoids appending below
  existing data and keeps row layout predictable.

---

## Deduplication

Row deletion from Associates is the deduplication mechanism.  There is no
separate JSON tracking file.  Once a row is successfully written to its
destination and deleted from Associates, it will not be processed again on
the next run.

All writes happen before any deletions.  Deletions are processed in reverse
row order so that earlier indices remain valid as rows are removed.

---

## Edge Cases

| Scenario | Behaviour |
|---|---|
| Row is protected in Associates | `delete_rows` catches the 403 HttpError, logs a warning, and continues. The row remains in Associates. |
| Email cell (col AP) is blank | Group removal and Slack deactivation are skipped; the row is still written to the destination and deleted from Associates. |
| Destination tab missing | Created automatically before any writes. |
| Member not in Google Group (404) | Logged as info; not treated as an error. |
| Row too short to have a status value | Skipped silently. |
| No rows match any trigger | Script exits cleanly after logging "No rows to delete." |
| Slack user not found | Logged as info — member never had a Slack account; no action taken. |
| Slack account already deactivated | Logged as info — idempotent; no action taken. |
| `SLACK_USER_TOKEN` not set | Slack step skipped with a warning; all other steps proceed normally. |

---

## How to Run

```
python execution/process_clan_cleanup.py
```

Logs are written to `.tmp/process_clan_cleanup.log` and mirrored to stdout.

On first run, two browser windows will open for OAuth consent:
1. `sealdirector@gmail.com` — Sheets read/write access
2. `admin@maxalton.com` — Admin SDK group membership access

Tokens are cached in `token_gmail.json` and `token_admin.json` at the project
root.  Subsequent runs refresh automatically without opening a browser.

---

## Configuration

All parameters live in `config.yaml` under the `clan_cleanup` key.

| Key | Purpose |
|---|---|
| `clan_life_sheet_id` | Spreadsheet ID for SEAL Clan Life |
| `associates_tab` | Name of the Associates tab |
| `status_column_index` | 0-based column index for status (K = 10) |
| `email_column_index` | 0-based column index for member email (AP = 41) |
| `start_row` | First data row number (1-indexed); rows above are skipped |
| `aad_sheet_id` | Spreadsheet ID for Clan Life AAD |
| `ex_communicado_tab` | Tab name for GameOver destination |
| `ex_associate_tab` | Tab name for Ex-Associate destination |
| `affiliates_tab` | Tab name for Affiliate destination (in Clan Life) |
| `active_group_email` | Google Group email for removal |
| `status_triggers` | Human-readable labels; not read by the script directly |

To change which keywords trigger each route, update the `startswith` strings
inside `process_clan_cleanup.py` (the `status.startswith(...)` calls in `main`).

---

## Learnings / Known Constraints

- The Sheets API omits trailing empty cells within a row.  All column index
  accesses use `len(row) > col_index` guards to prevent IndexError on short rows.
- Protected rows in Associates (e.g. pinned header rows) will raise a 403 on
  deletion.  These are caught and logged; the rest of the batch continues.
- `users.admin.setInactive` (Slack deactivation API) requires the legacy `client`
  scope — not available on modern Slack apps.  Playwright is the working path.
- The deactivation confirmation modal button is **"Deactivate"** (not "Save", "Confirm",
  or "Yes").  Contrast with the reactivation modal in process_challenge.py which uses
  **"Save"**.
- No Inactive filter is needed when searching for a member to deactivate — they are
  still active at that point.  The filter is only needed for reactivation.
