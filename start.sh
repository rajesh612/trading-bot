#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PID_FILE="$SCRIPT_DIR/trading_bot.pid"
LOG_FILE="$SCRIPT_DIR/trading_bot.log"

# Create venv and install deps if missing
if [ ! -d "$VENV_DIR" ]; then
    echo "[start.sh] Creating virtual environment…"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip -q
    "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
    echo "[start.sh] Dependencies installed."
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
# Stdout/stderr intentionally discarded — _file_handler in main.py writes directly
# to trading_bot.log. Redirecting stdout here too would double every log line.
nohup "$VENV_DIR/bin/python3" main.py > /dev/null 2>&1 &
echo $! > "$PID_FILE"
echo "[start.sh] Bot started (PID $!). Logs: $LOG_FILE"
