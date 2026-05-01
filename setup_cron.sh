#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_LOG="$SCRIPT_DIR/cron_trading_bot.log"
BLOCK_START="# trading-bot-begin"
BLOCK_END="# trading-bot-end"

_remove_block() {
    local current
    current=$(crontab -l 2>/dev/null || true)
    if [ -n "$current" ]; then
        echo "$current" | sed "/$BLOCK_START/,/$BLOCK_END/d" | crontab - 2>/dev/null || true
    fi
}

if [ "${1}" = "uninstall" ]; then
    echo "[setup_cron] Removing trading bot cron jobs..."
    _remove_block
    echo "[setup_cron] Done. Bot will no longer auto-start or auto-stop."
    exit 0
fi

echo "[setup_cron] Installing trading bot cron jobs..."
_remove_block

(crontab -l 2>/dev/null || true; cat <<EOF
$BLOCK_START
CRON_TZ=America/New_York
30 8 * * 1-5 $SCRIPT_DIR/start.sh >> $CRON_LOG 2>&1
30 16 * * 1-5 $SCRIPT_DIR/stop.sh >> $CRON_LOG 2>&1
$BLOCK_END
EOF
) | crontab -

echo "[setup_cron] Installed."
echo "  Auto-start : 8:30 AM ET (America/New_York), Monday-Friday"
echo "  Auto-stop  : 4:30 PM ET (America/New_York), Monday-Friday"
echo "  Timezone   : CRON_TZ=America/New_York (DST-aware, does not affect other cron jobs)"
echo "  Logs: $CRON_LOG"
echo ""
echo "  To verify:    crontab -l"
echo "  To uninstall: ./setup_cron.sh uninstall"
