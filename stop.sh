#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/trading_bot.pid"
LOCK_FILE="$SCRIPT_DIR/trading_bot.lock"

# Kill all running bot instances (handles launchd respawn + manual start races)
KILLED=0
while IFS= read -r pid; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "[stop.sh] Stopping bot (PID $pid)…"
        kill -TERM "$pid" 2>/dev/null || true
        KILLED=$((KILLED + 1))
    fi
# Match both "python3 main.py" (Linux) and "Python main.py" (macOS Python.framework)
done < <(pgrep -f "[Pp]ython.* main\.py" 2>/dev/null || true)

if [ "$KILLED" -gt 0 ]; then
    sleep 2
    # Force-kill anything still alive
    pkill -KILL -f "python3 main\.py" 2>/dev/null || true
    echo "[stop.sh] Bot stopped ($KILLED instance(s) terminated)."
else
    echo "[stop.sh] No running bot instances found."
fi

rm -f "$PID_FILE" "$LOCK_FILE"
