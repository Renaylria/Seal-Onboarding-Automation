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
TIMEOUT=600  # 10 minutes per script

notify_error() {
    local script="$1"
    local err="$2"
    ERRORS="$ERRORS\n[$script] $err"
}

# Run a script with a 10-minute timeout. Kills the process if it hangs.
run_with_timeout() {
    local script="$1"
    python3 "$script" &
    local pid=$!
    local elapsed=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        elapsed=$((elapsed + 1))
        if [ $elapsed -ge $TIMEOUT ]; then
            echo "TIMEOUT: $script exceeded ${TIMEOUT}s — killing PID $pid" >&2
            kill "$pid" 2>/dev/null
            sleep 2
            kill -9 "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null
            return 124  # same as GNU timeout exit code
        fi
    done
    wait "$pid"
    return $?
}

# ── Monthly log rotation ──────────────────────────────────────────────
# On the 1st of each month, archive current logs and start fresh.
MONTH_TAG=$(date +%Y-%m)
ROTATION_MARKER="$DIR/.tmp/.rotated_$MONTH_TAG"
if [ ! -f "$ROTATION_MARKER" ]; then
    for logfile in "$DIR/.tmp/cron.log" "$DIR/.tmp/process_applicants.log" \
                   "$DIR/.tmp/process_challenge.log" "$DIR/.tmp/process_clan_cleanup.log" \
                   "$DIR/.tmp/process_slack_audit.log"; do
        if [ -f "$logfile" ]; then
            mv "$logfile" "${logfile%.log}_$(date -v-1d +%Y-%m 2>/dev/null || date +%Y-%m).log.bak"
        fi
    done
    touch "$ROTATION_MARKER"
fi

# ── PID overlap guard ────────────────────────────────────────────────
PIDFILE="$DIR/.tmp/run_all.pid"
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "===== $(date) ===== run_all.sh SKIPPED (previous run PID $OLD_PID still alive) =====" >> "$DIR/.tmp/cron.log"
        exit 0
    fi
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

echo "===== $(date) ===== run_all.sh started =====" >> "$DIR/.tmp/cron.log"

# Run order matters:
#   1. clan_cleanup  — remove departing members from Associates first
#   2. applicants    — process new applicants
#   3. challenge     — promote stage 3 to Associates (dedup checks Associates,
#                      so cleanup must run first to avoid false "already there")
#   4. slack_audit   — audit Associates vs Slack (must run AFTER all three
#                      above so that Associates is fully up-to-date)

for script in process_clan_cleanup.py process_applicants.py process_challenge.py process_slack_audit.py; do
    OUTPUT=$(run_with_timeout "$script" 2>&1)
    EXIT_CODE=$?
    echo "$OUTPUT" >> "$DIR/.tmp/cron.log"
    if [ $EXIT_CODE -eq 124 ]; then
        notify_error "$script" "KILLED: exceeded ${TIMEOUT}s timeout"
        echo "TIMEOUT: $script killed after ${TIMEOUT}s" >> "$DIR/.tmp/cron.log"
    elif [ $EXIT_CODE -ne 0 ]; then
        notify_error "$script" "$OUTPUT"
    fi
done

# TEMPORARY: Send error email if any script failed
if [ -n "$ERRORS" ]; then
    python3 error_notify.py "run_all.sh" "$(echo -e "$ERRORS")" >> "$DIR/.tmp/cron.log" 2>&1
fi

echo "===== $(date) ===== run_all.sh finished =====" >> "$DIR/.tmp/cron.log"
