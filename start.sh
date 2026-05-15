#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PID_FILE="$SCRIPT_DIR/trading_bot.pid"
LOG_FILE="$SCRIPT_DIR/logs/trading_bot.log"

HASH_FILE="$VENV_DIR/requirements.hash"
REQ_HASH="$(md5 -q "$SCRIPT_DIR/requirements.txt" 2>/dev/null || md5sum "$SCRIPT_DIR/requirements.txt" | awk '{print $1}')"

if [ ! -d "$VENV_DIR" ]; then
    echo "[start.sh] Creating virtual environment…"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip -q
    "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
    echo "$REQ_HASH" > "$HASH_FILE"
    echo "[start.sh] Dependencies installed."
elif [ ! -f "$HASH_FILE" ] || [ "$REQ_HASH" != "$(cat "$HASH_FILE")" ]; then
    echo "[start.sh] requirements.txt changed — syncing dependencies…"
    "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
    echo "$REQ_HASH" > "$HASH_FILE"
    echo "[start.sh] Dependencies synced."
fi

cd "$SCRIPT_DIR"

# ── Called by launchd (KeepAlive=false): run in foreground, launchd owns the process ──
if [ "${1}" = "--foreground" ]; then
    exec "$VENV_DIR/bin/python3" main.py
fi

# ── Called manually from terminal: background with nohup ──
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "[start.sh] Bot already running (PID $(cat "$PID_FILE"))."
    exit 0
fi

echo "[start.sh] Starting trading bot in background…"
# stdout appended to log so pre-logger crashes are visible; _file_handler also writes there (duplicate lines are acceptable at startup)
nohup "$VENV_DIR/bin/python3" main.py >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "[start.sh] Bot started (PID $!). Logs: $LOG_FILE"
