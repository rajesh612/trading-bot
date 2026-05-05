#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

START_SRC="$SCRIPT_DIR/trading_bot_start.plist"
STOP_SRC="$SCRIPT_DIR/trading_bot_stop.plist"
START_DEST="$LAUNCH_AGENTS_DIR/trading_bot_start.plist"
STOP_DEST="$LAUNCH_AGENTS_DIR/trading_bot_stop.plist"

# ── macOS TCC guard ────────────────────────────────────────────────────────────
# When the project lives in ~/Documents, ~/Desktop, or ~/Downloads, macOS TCC
# (Privacy & Security) blocks launchd-spawned bash from reading scripts in those
# directories, producing: "bash: /path/script.sh: Operation not permitted".
# /bin/bash must hold Full Disk Access; otherwise launchd silently fails and
# (if KeepAlive is set) spams the log with EPERM errors every ThrottleInterval.
_check_fda() {
    case "$SCRIPT_DIR" in
        "$HOME/Documents/"*|"$HOME/Desktop/"*|"$HOME/Downloads/"*)
            echo ""
            echo "  ┌─────── IMPORTANT: macOS Full Disk Access required ────────────────┐"
            echo "  │   The project is inside ~/Documents (or ~/Desktop/~/Downloads).   │"
            echo "  │   launchd's bash process needs Full Disk Access to read scripts   │"
            echo "  │   from TCC-protected folders; without it every launch attempt     │"
            echo "  │   silently fails with 'Operation not permitted'.                  │"
            echo "  │                                                                   │"
            echo "  │   Grant FDA to /bin/bash (one-time):                              │"
            echo "  │    System Settings → Privacy & Security → Full Disk Access        │"
            echo "  │    Click '+' → Cmd+Shift+G → type /bin → select 'bash' → Open     │"
            echo "  │                                                                   │"
            echo "  │   To verify after granting:                                       │"
            echo "  │     launchctl start trading_bot_start                             │"
            echo "  │     sleep 5 && tail launchd_trading_bot.log                       │"
            echo "  └───────────────────────────────────────────────────────────────────┘"
            echo ""
            ;;
    esac
}

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

_check_fda
