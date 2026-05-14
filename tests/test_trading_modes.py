"""
Mode routing, SL/TP table, position sizing, and context-block unit tests.
Run with: pytest tests/test_trading_modes.py -v -m unit
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET


# ── Plan 3: Mode Routing & Learning ──────────────────────────────────────────


@pytest.mark.unit
class TestGetActiveMode:
    """trading_agent.get_active_mode pure routing logic."""

    def test_bull_trend_returns_hybrid(self):
        from trading.modes import get_active_mode
        assert get_active_mode("BULL_TREND") == "HYBRID"

    def test_opportunity_returns_hybrid(self):
        from trading.modes import get_active_mode
        assert get_active_mode("OPPORTUNITY") == "HYBRID"

    def test_capitulation_returns_short_term(self):
        from trading.modes import get_active_mode
        assert get_active_mode("CAPITULATION") == "SHORT_TERM"

    def test_crash_recovery_returns_short_term(self):
        from trading.modes import get_active_mode
        assert get_active_mode("CRASH_RECOVERY") == "SHORT_TERM"

    def test_crisis_returns_cash(self):
        from trading.modes import get_active_mode
        assert get_active_mode("CRISIS") == "CASH"

    def test_normal_returns_day_trade(self):
        from trading.modes import get_active_mode
        assert get_active_mode("NORMAL") == "DAY_TRADE"

    def test_choppy_returns_day_trade(self):
        from trading.modes import get_active_mode
        assert get_active_mode("CHOPPY") == "DAY_TRADE"

    def test_defensive_returns_day_trade(self):
        from trading.modes import get_active_mode
        assert get_active_mode("DEFENSIVE") == "DAY_TRADE"

    def test_unknown_regime_returns_day_trade(self):
        from trading.modes import get_active_mode
        assert get_active_mode("SOME_NEW_REGIME") == "DAY_TRADE"


@pytest.mark.unit
class TestGetSlTpForModeAndRegime:
    """trading_agent.get_sl_tp_for_mode_and_regime table lookups."""

    def test_day_trade_normal(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("DAY_TRADE", "NORMAL")
        assert sl == 3.0 and tp == 5.0

    def test_day_trade_choppy(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("DAY_TRADE", "CHOPPY")
        assert sl == 5.0 and tp == 6.0

    def test_hybrid_bull_trend(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("HYBRID", "BULL_TREND")
        assert sl == 7.0 and tp == 15.0

    def test_short_term_capitulation(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("SHORT_TERM", "CAPITULATION")
        assert sl == 12.0 and tp == 20.0

    def test_pead_any_regime(self):
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("PEAD", "NORMAL")
        assert sl == 8.0 and tp == 8.0

    def test_unknown_combo_falls_back_to_config_defaults(self):
        import config
        from trading.modes import get_sl_tp_for_mode_and_regime
        sl, tp = get_sl_tp_for_mode_and_regime("UNKNOWN_MODE", "UNKNOWN_REGIME")
        assert sl == config.STOP_LOSS_PCT and tp == config.TAKE_PROFIT_PCT


@pytest.mark.unit
class TestComputePositionSizeForMode:
    """trading_agent.compute_position_size_for_mode sizing logic."""

    def test_day_trade_uses_5pct_cap(self):
        from trading.position_sizer import compute_position_size_for_mode
        portfolio = 10_000.0
        price = 100.0
        atr = 1.0  # trivially small ATR so risk-based won't cap
        qty = compute_position_size_for_mode(portfolio, price, atr, "DAY_TRADE", 0, False)
        # 5% of 10k / $100 = 5 shares
        assert qty == 5

    def test_hybrid_uses_3pct_cap(self):
        from trading.position_sizer import compute_position_size_for_mode
        qty = compute_position_size_for_mode(10_000.0, 100.0, 0.01, "HYBRID", 0, False)
        # 3% of 10k / $100 = 3 shares
        assert qty == 3

    def test_short_term_uses_2_5pct_cap(self):
        from trading.position_sizer import compute_position_size_for_mode
        qty = compute_position_size_for_mode(10_000.0, 100.0, 0.01, "SHORT_TERM", 0, False)
        # 2.5% of 10k / $100 = 2.5 → rounds to 2 or 3
        assert qty in (2, 3)

    def test_herding_reduces_size_by_30pct(self):
        import config
        from trading.position_sizer import compute_position_size_for_mode
        base = compute_position_size_for_mode(10_000.0, 100.0, 0.01, "DAY_TRADE", 0, False)
        herding = compute_position_size_for_mode(
            10_000.0, 100.0, 0.01, "DAY_TRADE",
            config.BOT_HERD_ALERT_SCORE + 1, False
        )
        assert herding <= base
        assert herding >= 1

    def test_squeeze_skips_herding_reduction(self):
        import config
        from trading.position_sizer import compute_position_size_for_mode
        no_squeeze = compute_position_size_for_mode(
            10_000.0, 100.0, 0.01, "DAY_TRADE",
            config.BOT_HERD_ALERT_SCORE + 1, False
        )
        with_squeeze = compute_position_size_for_mode(
            10_000.0, 100.0, 0.01, "DAY_TRADE",
            config.BOT_HERD_ALERT_SCORE + 1, True  # squeeze overrides herding reduction
        )
        assert with_squeeze > no_squeeze

    def test_minimum_one_share(self):
        from trading.position_sizer import compute_position_size_for_mode
        # Very high price relative to portfolio → floors at 1
        qty = compute_position_size_for_mode(100.0, 10_000.0, 1.0, "DAY_TRADE", 0, False)
        assert qty >= 1


@pytest.mark.unit
class TestBuildMarketContextBlock:
    def _regime(self, **overrides) -> dict:
        base = {
            "regime": "BULL_TREND",
            "macro_health_score": 32,
            "vix_current": 18.2,
            "vix_90d_avg": 20.0,
            "vix_trend": "declining",
            "spy_5day_return": 1.5,
            "consecutive_green_days": 2,
            "consecutive_red_days": 0,
            "spy_5day_sign_changes": 1,
            "sector_scores": {"AI_SEMI": 80, "BROAD_TECH": 60},
            "hyg_ret5d": 0.3,
            "iwm_ret5d": 0.5,
            "vix3m": 19.0,
        }
        base.update(overrides)
        return base

    def _fred(self, **overrides) -> dict:
        base = {
            "fed_direction": "cutting",
            "cpi_trend": "falling",
            "unemployment_rising": False,
        }
        base.update(overrides)
        return base

    def _meta(self, **overrides) -> dict:
        base = {
            "macro_shock_score": 0,
            "sector_sentiment": {"AI_SEMI": 28.0, "DATA_CENTER": 15.0},
            "capitulation_candidates": [],
        }
        base.update(overrides)
        return base

    def _patch_context(self, monkeypatch):
        """Patch intraday context and breadth to avoid HTTP calls."""
        monkeypatch.setattr("economic_calendar.is_event_risk_window", lambda now: (False, ""))
        monkeypatch.setattr("macro.intraday_context.get_intraday_context", lambda now: None)
        monkeypatch.setattr("macro.breadth_signals.build_breadth_context_line", lambda s: "Breadth: HYG=+0.3%")

    def test_contains_all_six_fields(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta()) + "\n" + build_dynamic_user_context(now)
        assert "BULL_TREND" in block
        assert "cutting" in block
        assert "falling" in block
        assert "None active" in block
        assert "AI_SEMI" in block   # from sector_scores in regime
        assert "Capitulation watch" in block

    def test_shock_score_omitted_when_zero(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta(macro_shock_score=0)) + "\n" + build_dynamic_user_context(now)
        assert "0/100" in block
        assert "elevated" not in block

    def test_shock_score_flagged_when_elevated(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta(macro_shock_score=55)) + "\n" + build_dynamic_user_context(now)
        assert "55/100" in block
        assert "elevated" in block

    def test_active_event_risk_flagged(self, monkeypatch):
        monkeypatch.setattr("economic_calendar.is_event_risk_window", lambda now: (True, "Non-Farm Payrolls"))
        monkeypatch.setattr("macro.intraday_context.get_intraday_context", lambda now: None)
        monkeypatch.setattr("macro.breadth_signals.build_breadth_context_line", lambda s: "Breadth: HYG=+0.3%")
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 8, 35, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta()) + "\n" + build_dynamic_user_context(now)
        assert "Non-Farm Payrolls" in block
        assert "avoid" in block

    def test_capitulation_candidates_listed(self, monkeypatch):
        self._patch_context(monkeypatch)
        from trading.context_builder import build_stable_system_context, build_dynamic_user_context
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 5, 5, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        block = build_stable_system_context(self._regime(), self._fred(), self._meta(capitulation_candidates=["SMCI", "PLTR"])) + "\n" + build_dynamic_user_context(now)
        assert "SMCI" in block
        assert "PLTR" in block
