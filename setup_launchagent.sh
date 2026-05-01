#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

START_SRC="$SCRIPT_DIR/trading_bot_start.plist"
STOP_SRC="$SCRIPT_DIR/trading_bot_stop.plist"
START_DEST="$LAUNCH_AGENTS_DIR/trading_bot_start.plist"
STOP_DEST="$LAUNCH_AGENTS_DIR/trading_bot_stop.plist"

_remove_old_plist() {
    local old="$LAUNCH_AGENTS_DIR/com.tradingbot.plist"
    if [ -f "$old" ]; then
        launchctl unload "$old" 2>/dev/null || true
        rm -f "$old"
    fi
}

if [ "${1}" = "uninstall" ]; then
    echo "[setup_launchagent] Uninstalling LaunchAgents..."
    launchctl unload "$START_DEST" 2>/dev/null || true
    launchctl unload "$STOP_DEST" 2>/dev/null || true
    rm -f "$START_DEST" "$STOP_DEST"
    _remove_old_plist
    echo "[setup_launchagent] Done. Bot will no longer auto-start or auto-stop."
    exit 0
fi

echo "[setup_launchagent] Installing Trading Bot LaunchAgents..."
_remove_old_plist

sed "s|PLACEHOLDER_PROJECT_DIR|$SCRIPT_DIR|g" "$START_SRC" > "$START_DEST"
sed "s|PLACEHOLDER_PROJECT_DIR|$SCRIPT_DIR|g" "$STOP_SRC"  > "$STOP_DEST"

launchctl unload "$START_DEST" 2>/dev/null || true
launchctl unload "$STOP_DEST"  2>/dev/null || true
launchctl load "$START_DEST"
launchctl load "$STOP_DEST"

echo "[setup_launchagent] Installed."
echo "  Auto-start : 5:30 AM PT (8:30 AM ET), Monday–Friday"
echo "  Auto-stop  : 1:30 PM PT (4:30 PM ET), Monday–Friday"
echo "  If laptop is asleep at the scheduled time, launchd fires on next wake."
echo "  Logs: $SCRIPT_DIR/launchd_trading_bot.log"
echo ""
echo "  To uninstall: ./setup_launchagent.sh uninstall"
