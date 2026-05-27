"""
Trading cycle unit tests — dead-day alert diagnostics.
Run with: pytest tests/test_trading_cycle.py -v -m unit
"""
from datetime import datetime

import pytest

import config
from utils.constants import ET


# ── Dead-day alert diagnostic message ────────────────────────────────────────

@pytest.mark.unit
class TestDeadDayAlertMessage:
    """Dead-day alert fires after DEAD_DAY_ALERT_CYCLES consecutive zero-candidate
    cycles and the message must include can_open / stale / MEDIUM floor context."""

    def _make_guard(self, can_open=True, stale_sources=None, penalty=0):
        from utils.data_health import StalenessGuard
        return StalenessGuard(
            composite_penalty=penalty,
            auto_execute_allowed=True,
            t2_allowed=True,
            swing_fallback_regime=None,
            can_open=can_open,
            claude_warnings=(),
            stale_sources=frozenset(stale_sources or []),
        )

    def test_dead_day_alert_contains_can_open_and_stale_and_floor(self, monkeypatch):
        """After threshold cycles with 0 candidates, Telegram message includes
        can_open status, stale source list, and the MEDIUM composite floor."""
        from unittest.mock import patch, MagicMock
        import trading.cycle as cycle_mod

        today_str = "2026-05-27"
        threshold = config.DEAD_DAY_ALERT_CYCLES
        monkeypatch.setitem(cycle_mod._zero_pass, "date", today_str)
        monkeypatch.setitem(cycle_mod._zero_pass, "count", threshold - 1)
        monkeypatch.setitem(cycle_mod._zero_pass, "alerted", False)

        guard = self._make_guard(
            can_open=True,
            stale_sources=["regime", "research"],
            penalty=10,
        )

        account = MagicMock()
        account.portfolio_value = "100000"
        account.buying_power    = "50000"
        account.last_equity     = "100000"

        cycle_result = MagicMock()
        cycle_result.candidates = 0
        cycle_result.entries    = 0
        cycle_result.closes     = 0
        cycle_result.skipped    = False

        sent_messages = []

        with patch("trading.cycle.AlpacaClient") as mock_alpaca_cls, \
             patch("utils.data_health.get_staleness_guard", return_value=guard), \
             patch("trading.cycle.load_macro_regime",
                   return_value={"regime": "NORMAL", "flash_crash_active": False}), \
             patch("trading.cycle.load_trading_params",
                   return_value={"entry_start_hour": 10, "entry_end_hour": 15,
                                 "high_vol_position_mult": 0.5, "rsi_buy_max_high_vol": 60}), \
             patch("trading.cycle.load_watch_list", return_value=["AAPL", "MSFT"]), \
             patch("trading.cycle.build_cycle_snapshot", return_value=MagicMock()), \
             patch("trading.cycle.TradingPipeline") as mock_pipeline_cls, \
             patch("trading.cycle.telegram_notify._send",
                   side_effect=lambda m: sent_messages.append(m)), \
             patch("trading.cycle.telegram_notify.notify_alert"), \
             patch("trading.cycle._run_intraday_retries"), \
             patch("anthropic.Anthropic"), \
             patch("trading.cycle.datetime") as mock_dt:

            mock_alpaca_cls.return_value.get_account.return_value = account
            mock_pipeline_cls.return_value.run.return_value = cycle_result

            # Freeze time to 11:00 inside entry window
            fake_now = datetime(2026, 5, 27, 11, 0, 0, tzinfo=ET)
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            cycle_mod.run_trading_cycle()

        assert len(sent_messages) == 1, "Expected exactly one Telegram message"
        msg = sent_messages[0]
        assert "can_open" in msg, f"'can_open' missing from: {msg}"
        assert "stale" in msg, f"'stale' missing from: {msg}"
        assert "MEDIUM floor" in msg, f"'MEDIUM floor' missing from: {msg}"
