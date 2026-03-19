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

| Status trigger | Destination sheet | Destination tab | Remove from group? |
|---|---|---|---|
| `gameover` | Clan Life AAD (`aad_sheet_id`) | Ex-Communicado | Yes |
| `ex-associate` | Clan Life AAD (`aad_sheet_id`) | Ex-Associate | Yes |
| `affiliate` | SEAL Clan Life (`clan_life_sheet_id`) | Affiliates | No |

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
| Email cell (col AP) is blank | Group removal step is skipped; the row is still written to the destination and deleted from Associates. |
| Destination tab missing | Created automatically before any writes. |
| Member not in Google Group (404) | Logged as info; not treated as an error. |
| Row too short to have a status value | Skipped silently. |
| No rows match any trigger | Script exits cleanly after logging "No rows to delete." |

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

_(Append new findings here as they are discovered.)_

- The Sheets API omits trailing empty cells within a row.  All column index
  accesses use `len(row) > col_index` guards to prevent IndexError on short rows.
- Protected rows in Associates (e.g. pinned header rows) will raise a 403 on
  deletion.  These are caught and logged; the rest of the batch continues.
