# Directive: SEAL Slack Access Audit

## Purpose
Audit the SEAL Clan Life Associates tab against the Slack workspace member list. Any Associate who is missing from Slack or has a deactivated account gets their access restored (invite or reactivate). This ensures every active SEAL member has Slack access.

## Trigger
Run hourly via `run_all.sh` as the **4th and final step** — after `process_clan_cleanup`, `process_applicants`, and `process_challenge`. This ordering is critical: departing members must be removed from Associates before the audit runs, otherwise the audit could restore access to accounts that should be deactivated.

## Inputs
| Source | Sheet | Tab | Key Columns |
|--------|-------|-----|-------------|
| SEAL Clan Life | `1k19sS9NfwlVfG7GCCf18LO69pr4reSOTN6v1lY2nykQ` | Associates | AP (email, index 41) |
| Slack API | `users.list` via `SLACK_USER_TOKEN` | — | email, deleted status |

## Outputs
| Destination | Description |
|-------------|-------------|
| Slack workspace | Invites or reactivations for Associates missing from Slack |
| Automation Log | Run Log row + Error Log rows (if any) via `RunLogger` |

## Processing Logic
1. Read all emails from the **Associates** tab (rows 14+, column AP)
2. Fetch the full Slack workspace member list via `users.list` API (single paginated call, includes deactivated accounts)
3. Compare each Associate email against Slack:

| Scenario | Action |
|----------|--------|
| Email not found in Slack | New member — invite via Playwright admin panel |
| Email found, account deactivated | Returning member — reactivate (API first, Playwright fallback) |
| Email found, account active | No action needed |

4. Verify each restore action via `verify_slack_active`
5. Log summary to Automation Log

## Authentication
- `token_gmail.json` — sealscripting@gmail.com (Sheets read)
- `SLACK_USER_TOKEN` in `.env` — Slack user token with `admin`, `users:read`, `users:read.email` scopes
- `SLACK_ADMIN_EMAIL` / `SLACK_ADMIN_PASSWORD` in `.env` — for Playwright fallback

## Configuration
Uses existing `config.yaml` values under the `challenge:` key:
- `clan_life_sheet_id`, `associates_tab`, `email_column_index`, `associates_start_row`

No new config entries needed.

## Slack Restore Methods

### Invite (new members)
Uses Playwright to open the Slack admin panel → "Invite People" → enter email → Send. Retries up to 3 times with session reset between attempts.

### Reactivate (returning members)
1. First tries `users.admin.setActive` API (fast, no browser)
2. Falls back to Playwright: admin panel → Filter by Inactive → search email → Actions → Activate Account → Save

### Learnings
- The `users.list` API call is fast (a few seconds) and fetches all members including deactivated ones in a single paginated call. This avoids per-email lookups.
- Playwright is only needed when there are actual mismatches to fix, so most hourly runs complete in seconds (API-only).
- The script reuses the same Playwright patterns from `process_challenge.py` for invites and reactivations.

## Error Handling
- If the Slack API call fails entirely, the audit aborts and logs an error (does not fall through to Playwright for the full member list).
- Individual invite/reactivate failures are logged per-email and the audit continues with the next mismatch.
- If any restores fail, the run status is set to WARNING.
