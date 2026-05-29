"""Tests for trading/monitor.py — position monitoring and exit strategies."""
from datetime import datetime

import pytest


@pytest.mark.unit
class TestMiddayProfitFloor:
    """run_position_monitor locks gains ≥1% for DAY_TRADE positions after 2 PM in NORMAL regime."""

    def _make_position(self, entry_time_str: str, mode_at_entry: str = "DAY_TRADE",
                       is_swing: bool = False) -> dict:
        return {
            "entry_price": 100.0,
            "qty": 10,
            "stop_loss_pct": 4.0,
            "take_profit_pct": 5.0,
            "entry_time": entry_time_str,
            "mode_at_entry": mode_at_entry,
            "is_swing": is_swing,
        }

    def _run_monitor(self, monkeypatch, *, regime: str, now_hour: int, now_minute: int,
                     pnl_pct: float, hold_hours: float, is_swing: bool = False,
                     mode_at_entry: str = "DAY_TRADE"):
        """Shared test driver — returns the mock close_position."""
        from unittest.mock import MagicMock, patch
        from zoneinfo import ZoneInfo

        ET_zone = ZoneInfo("America/New_York")
        now = datetime(2026, 5, 26, now_hour, now_minute, 0, tzinfo=ET_zone)
        from datetime import timedelta
        entry_dt = now - timedelta(hours=hold_hours)
        entry_time_str = entry_dt.isoformat()

        entry_price = 100.0
        current_price = entry_price * (1 + pnl_pct / 100)

        position = self._make_position(entry_time_str, mode_at_entry=mode_at_entry,
                                       is_swing=is_swing)
        position["entry_price"] = entry_price

        mock_alpaca = MagicMock()
        mock_alpaca.get_positions.return_value = {"MSFT": MagicMock()}
        mock_alpaca.get_latest_price.return_value = current_price

        mock_close = MagicMock()

        with patch("trading.monitor.load_open_positions", return_value={"MSFT": position}), \
             patch("trading.monitor.AlpacaClient", return_value=mock_alpaca), \
             patch("trading.monitor.load_macro_regime", return_value={"regime": regime}), \
             patch("trading.monitor.close_position", mock_close), \
             patch("trading.monitor.datetime") as mock_dt, \
             patch("trading.monitor.flush_profit_lock_updates"), \
             patch("trading.monitor._apply_profit_lock", side_effect=lambda pos, _: pos), \
             patch("trading.monitor._try_compute_iv", return_value=None), \
             patch("trading.monitor._check_drawdown_alerts"), \
             patch("utils.data_health.get_staleness_guard", side_effect=Exception("skip")):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            from trading.monitor import run_position_monitor
            run_position_monitor()

        return mock_close

    def test_triggers_on_qualifying_position(self, monkeypatch):
        """NORMAL regime, +1.5% gain, held 2.5h, 2:30 PM → midday_profit_floor fires."""
        mock_close = self._run_monitor(
            monkeypatch,
            regime="NORMAL",
            now_hour=14,
            now_minute=30,
            pnl_pct=1.5,
            hold_hours=2.5,
        )
        mock_close.assert_called_once()
        _, _, _, _, _, reason = mock_close.call_args[0]
        assert reason == "midday_profit_floor"

    def test_does_not_trigger_before_exit_hour(self, monkeypatch):
        """1:30 PM (before MIDDAY_EXIT_HOUR=14) — must NOT fire."""
        mock_close = self._run_monitor(
            monkeypatch,
            regime="NORMAL",
            now_hour=13,
            now_minute=30,
            pnl_pct=1.5,
            hold_hours=2.5,
        )
        mock_close.assert_not_called()

    def test_does_not_trigger_for_swing_positions(self, monkeypatch):
        """is_swing=True — midday profit-floor is day-trade only, must NOT fire."""
        mock_close = self._run_monitor(
            monkeypatch,
            regime="NORMAL",
            now_hour=14,
            now_minute=30,
            pnl_pct=1.5,
            hold_hours=2.5,
            is_swing=True,
        )
        mock_close.assert_not_called()

    def test_does_not_trigger_after_max_hour(self, monkeypatch):
        """Should NOT fire at or after MIDDAY_EXIT_MAX_HOUR (3 PM)."""
        import config
        import trading.monitor as mon
        monkeypatch.setattr(config, "MIDDAY_PROFIT_FLOOR_PCT", 1.0)
        monkeypatch.setattr(config, "MIDDAY_EXIT_HOUR", 14)
        monkeypatch.setattr(config, "MIDDAY_EXIT_MAX_HOUR", 15)
        monkeypatch.setattr(config, "MIDDAY_EXIT_MIN_HOLD_H", 2.0)

        # now = 3:00 PM, position up 1.5%, held 2.5h — qualifies on everything except hour
        mock_close = self._run_monitor(
            monkeypatch,
            regime="NORMAL",
            now_hour=15,
            now_minute=0,
            pnl_pct=1.5,
            hold_hours=2.5,
        )
        mock_close.assert_not_called()
