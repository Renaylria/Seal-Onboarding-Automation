#!/bin/bash
# run_all.sh — Run all three SEAL onboarding scripts sequentially.
# Called by cron every hour.

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/execution"

# Activate virtual environment
source "$DIR/venv/bin/activate"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEMPORARY: Error notification to harrisnakajima@gmail.com
# To remove: delete the notify_error function and the
# "|| notify_error" suffixes from the python3 lines below,
# and delete execution/error_notify.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ERRORS=""

notify_error() {
    local script="$1"
    local err="$2"
    ERRORS="$ERRORS\n[$script] $err"
}

# ── Monthly log rotation ──────────────────────────────────────────────
# On the 1st of each month, archive current logs and start fresh.
MONTH_TAG=$(date +%Y-%m)
ROTATION_MARKER="$DIR/.tmp/.rotated_$MONTH_TAG"
if [ ! -f "$ROTATION_MARKER" ]; then
    for logfile in "$DIR/.tmp/cron.log" "$DIR/.tmp/process_applicants.log" \
                   "$DIR/.tmp/process_challenge.log" "$DIR/.tmp/process_clan_cleanup.log"; do
        if [ -f "$logfile" ]; then
            mv "$logfile" "${logfile%.log}_$(date -v-1d +%Y-%m 2>/dev/null || date +%Y-%m).log.bak"
        fi
    done
    touch "$ROTATION_MARKER"
fi

echo "===== $(date) ===== run_all.sh started =====" >> "$DIR/.tmp/cron.log"

# Run order matters:
#   1. clan_cleanup  — remove departing members from Associates first
#   2. applicants    — process new applicants
#   3. challenge     — promote stage 3 to Associates (dedup checks Associates,
#                      so cleanup must run first to avoid false "already there")

OUTPUT=$(python3 process_clan_cleanup.py 2>&1)
EXIT_CODE=$?
echo "$OUTPUT" >> "$DIR/.tmp/cron.log"
[ $EXIT_CODE -ne 0 ] && notify_error "process_clan_cleanup.py" "$OUTPUT"

OUTPUT=$(python3 process_applicants.py 2>&1)
EXIT_CODE=$?
echo "$OUTPUT" >> "$DIR/.tmp/cron.log"
[ $EXIT_CODE -ne 0 ] && notify_error "process_applicants.py" "$OUTPUT"

OUTPUT=$(python3 process_challenge.py 2>&1)
EXIT_CODE=$?
echo "$OUTPUT" >> "$DIR/.tmp/cron.log"
[ $EXIT_CODE -ne 0 ] && notify_error "process_challenge.py" "$OUTPUT"

# TEMPORARY: Send error email if any script failed
if [ -n "$ERRORS" ]; then
    python3 error_notify.py "run_all.sh" "$(echo -e "$ERRORS")" >> "$DIR/.tmp/cron.log" 2>&1
fi

echo "===== $(date) ===== run_all.sh finished =====" >> "$DIR/.tmp/cron.log"
