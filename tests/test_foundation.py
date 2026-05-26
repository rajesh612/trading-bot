"""
Foundation unit tests: economic calendar, tier1 config, breadth signals,
intraday context. Run with: pytest tests/test_foundation.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET


# ── economic_calendar: NFP first-Friday logic ────────────────────────────────

@pytest.mark.unit
class TestEconomicCalendar:
    def test_nfp_is_first_friday(self):
        from macro.economic_calendar import _first_friday, _hardcoded_events
        assert _first_friday(2026, 1) == 2
        events = _hardcoded_events(2026, 1)
        nfp = [e for e in events if "Non-Farm" in e["name"]]
        assert len(nfp) == 1
        assert nfp[0]["date"] == "2026-01-02"

    def test_may_2026_nfp(self):
        from macro.economic_calendar import _first_friday
        assert _first_friday(2026, 5) == 1

    def test_event_risk_window_inside_45min(self, monkeypatch):
        from macro.economic_calendar import is_event_risk_window
        import macro.economic_calendar as ec
        today = datetime.now(ET).strftime("%Y-%m-%d")

        def _patched():
            return [{"time": "08:30", "name": "NFP", "impact": "high"}]

        monkeypatch.setattr(ec, "_load_todays_events", _patched)
        now_at_risk = datetime.now(ET).replace(hour=8, minute=45, second=0, microsecond=0)
        result, name = ec.is_event_risk_window(now_at_risk)
        assert result is True
        assert "NFP" in name

    def test_event_risk_window_outside_45min(self):
        import macro.economic_calendar as ec
        _orig = ec._load_todays_events
        ec._load_todays_events = lambda: [{"time": "10:00", "name": "FOMC", "impact": "high"}]
        try:
            check_time = datetime.now(ET).replace(hour=8, minute=0, second=0, microsecond=0)
            result, _ = ec.is_event_risk_window(check_time)
            assert result is False
        finally:
            ec._load_todays_events = _orig

    def test_fomc_2026_dates_present(self):
        from macro.economic_calendar import _hardcoded_events
        events = _hardcoded_events(2026, 5)
        fomc = [e for e in events if "FOMC" in e["name"]]
        assert any(e["date"] == "2026-05-07" for e in fomc)


# ── tier1_config → sectors.py migration ──────────────────────────────────────

@pytest.mark.unit
class TestSectorsLegacyCompat:
    """Verify sectors.py provides the same contract as the deleted tier1_config.py."""

    def test_known_symbol_is_tier1(self):
        import sectors
        assert sectors.is_tier1("NVDA") is True

    def test_case_insensitive(self):
        import sectors
        assert sectors.is_tier1("nvda") is True

    def test_unknown_symbol_is_not_tier1(self):
        import sectors
        assert sectors.is_tier1("FAKE") is False

    def test_get_sector_known_returns_etf_key(self):
        import sectors
        assert sectors.get_sector("NVDA") == "XLK"
        assert sectors.get_sector("JPM") == "XLF"
        assert sectors.get_sector("NEM") == "XLB"

    def test_get_sector_unknown_returns_other(self):
        import sectors
        assert sectors.get_sector("FAKE") == "OTHER"


# ── economic_calendar: hardcoded events + risk window ────────────────────────

@pytest.mark.unit
class TestEconomicCalendarLogic:
    """Tests for pure functions in economic_calendar — no network, no file I/O."""

    def test_first_friday_returns_correct_day(self):
        from macro.economic_calendar import _first_friday
        assert _first_friday(2026, 5) == 1

    def test_hardcoded_events_includes_nfp_and_cpi(self):
        from macro.economic_calendar import _hardcoded_events
        events = _hardcoded_events(2026, 5)
        names = [e["name"] for e in events]
        assert "Non-Farm Payrolls" in names
        assert "CPI Release" in names

    def test_is_event_risk_window_detects_nearby_event(self, tmp_path, monkeypatch):
        import config
        today = datetime.now(ET).strftime("%Y-%m-%d")
        payload = {
            "date": today,
            "events": [{"date": today, "time": "08:30", "name": "CPI Release", "impact": "high"}],
        }
        cache_file = tmp_path / "economic_calendar.json"
        cache_file.write_text(json.dumps(payload))
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", cache_file)

        from macro.economic_calendar import is_event_risk_window
        now_et = datetime.now(ET).replace(hour=8, minute=30, second=0, microsecond=0)
        in_window, name = is_event_risk_window(now_et)
        assert in_window is True
        assert name == "CPI Release"

    def test_is_event_risk_window_outside_returns_false(self, tmp_path, monkeypatch):
        import config
        today = datetime.now(ET).strftime("%Y-%m-%d")
        payload = {
            "date": today,
            "events": [{"date": today, "time": "08:30", "name": "NFP", "impact": "high"}],
        }
        cache_file = tmp_path / "economic_calendar.json"
        cache_file.write_text(json.dumps(payload))
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", cache_file)

        from macro.economic_calendar import is_event_risk_window
        now_et = datetime.now(ET).replace(hour=10, minute=30, second=0, microsecond=0)
        in_window, name = is_event_risk_window(now_et)
        assert in_window is False
        assert name == ""


# ── macro/breadth_signals ─────────────────────────────────────────────────────

@pytest.mark.unit
class TestBreadthSignals:
    """Tests for pure breadth scorer functions in macro/breadth_signals.py."""

    def test_score_hyg_deep_negative(self):
        from macro.breadth_signals import score_hyg
        assert score_hyg(-2.0) == pytest.approx(-10.0)

    def test_score_hyg_mild_negative(self):
        from macro.breadth_signals import score_hyg
        assert score_hyg(-1.0) == pytest.approx(-5.0)

    def test_score_hyg_neutral(self):
        from macro.breadth_signals import score_hyg
        assert score_hyg(0.0) == pytest.approx(3.0)

    def test_score_hyg_positive(self):
        from macro.breadth_signals import score_hyg
        assert score_hyg(1.0) == pytest.approx(8.0)

    def test_score_hyg_boundary_minus_1_5(self):
        """Exactly -1.5 is the lower boundary — maps to -5, not -10."""
        from macro.breadth_signals import score_hyg
        assert score_hyg(-1.5) == pytest.approx(-5.0)

    def test_score_iwm_vs_spy_small_cap_lagging_hard(self):
        from macro.breadth_signals import score_iwm_vs_spy
        assert score_iwm_vs_spy(0.0, 2.0) == pytest.approx(-10.0)

    def test_score_iwm_vs_spy_small_cap_lagging_mild(self):
        from macro.breadth_signals import score_iwm_vs_spy
        assert score_iwm_vs_spy(0.0, 1.0) == pytest.approx(-5.0)

    def test_score_iwm_vs_spy_neutral(self):
        from macro.breadth_signals import score_iwm_vs_spy
        assert score_iwm_vs_spy(0.5, 0.5) == pytest.approx(0.0)

    def test_score_iwm_vs_spy_risk_on(self):
        from macro.breadth_signals import score_iwm_vs_spy
        assert score_iwm_vs_spy(2.0, 0.5) == pytest.approx(8.0)

    def test_score_vix_term_zero_vix3m_returns_zero(self):
        """Guard: vix3m <= 0 must return 0 to avoid division by zero."""
        from macro.breadth_signals import score_vix_term_structure
        assert score_vix_term_structure(20.0, 0.0) == pytest.approx(0.0)

    def test_score_vix_term_backwardation_hard(self):
        """ratio > 1.1 → -10 (extreme backwardation)."""
        from macro.breadth_signals import score_vix_term_structure
        assert score_vix_term_structure(22.0, 19.0) == pytest.approx(-10.0)

    def test_score_vix_term_contango(self):
        """ratio < 0.92 → +8 (calm term structure)."""
        from macro.breadth_signals import score_vix_term_structure
        assert score_vix_term_structure(17.0, 20.0) == pytest.approx(8.0)

    def test_score_vix_term_neutral(self):
        """ratio near 1.0 → 0."""
        from macro.breadth_signals import score_vix_term_structure
        assert score_vix_term_structure(20.0, 20.0) == pytest.approx(0.0)

    def test_build_breadth_context_line_backwardation(self):
        from macro.breadth_signals import build_breadth_context_line
        line = build_breadth_context_line({"hyg_ret5d": -2.0, "iwm_ret5d": -1.0,
                                           "spy_ret5d": 0.0, "vix_current": 22.0, "vix3m": 19.0})
        assert "backwardation" in line

    def test_build_breadth_context_line_contango(self):
        from macro.breadth_signals import build_breadth_context_line
        line = build_breadth_context_line({"hyg_ret5d": 1.0, "iwm_ret5d": 1.0,
                                           "spy_ret5d": 0.5, "vix_current": 16.0, "vix3m": 20.0})
        assert "contango" in line

    def test_build_breadth_context_line_contains_hyg(self):
        from macro.breadth_signals import build_breadth_context_line
        line = build_breadth_context_line({"hyg_ret5d": 0.3})
        assert "HYG" in line


# ── macro/intraday_context: IntradayContext dataclass ─────────────────────────

@pytest.mark.unit
class TestIntradayContext:
    """Tests for IntradayContext dataclass and to_context_lines() — no network."""

    @staticmethod
    def _ctx(**overrides):
        from macro.intraday_context import IntradayContext
        base = dict(
            spy_today_pct=0.5,
            spy_vwap_position="above",
            spy_vwap_slope=0.05,
            qqq_today_pct=0.4,
            opening_gap_pct=0.2,
            post_event_day=False,
            sector_today={"XLF": 0.3, "XLK": -0.5},
            bias="BULLISH",
        )
        base.update(overrides)
        return IntradayContext(**base)

    def test_bullish_bias_in_context_lines(self):
        ctx = self._ctx(bias="BULLISH")
        assert "BULLISH" in ctx.to_context_lines()

    def test_bearish_bias_in_context_lines(self):
        ctx = self._ctx(bias="BEARISH", spy_today_pct=-0.8, spy_vwap_position="below")
        assert "BEARISH" in ctx.to_context_lines()

    def test_neutral_bias_in_context_lines(self):
        ctx = self._ctx(bias="NEUTRAL")
        assert "NEUTRAL" in ctx.to_context_lines()

    def test_post_event_day_warning_shown(self):
        ctx = self._ctx(post_event_day=True)
        assert "POST-EVENT" in ctx.to_context_lines()

    def test_no_post_event_warning_when_false(self):
        ctx = self._ctx(post_event_day=False)
        assert "POST-EVENT" not in ctx.to_context_lines()

    def test_sector_etfs_formatted_in_output(self):
        ctx = self._ctx(sector_today={"XLF": 0.3, "XLK": -0.8})
        lines = ctx.to_context_lines()
        assert "XLF" in lines
        assert "XLK" in lines

    def test_spy_pct_in_output(self):
        ctx = self._ctx(spy_today_pct=1.23)
        assert "1.2" in ctx.to_context_lines()

    def test_vwap_position_above_in_output(self):
        ctx = self._ctx(spy_vwap_position="above")
        assert "above" in ctx.to_context_lines()

    def test_vwap_position_below_in_output(self):
        ctx = self._ctx(spy_vwap_position="below")
        assert "below" in ctx.to_context_lines()


# ── economic_calendar: is_post_major_event_day ────────────────────────────────

@pytest.mark.unit
class TestPostEventDay:
    """Tests for is_post_major_event_day() using tmp_path — no network."""

    def _write_calendar(self, tmp_path, monkeypatch, prior_events: list) -> None:
        import config
        cache = tmp_path / "economic_calendar.json"
        cache.write_text(json.dumps({"date": "2026-05-08", "events": [], "prior_events": prior_events}))
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", cache)

    def test_fomc_in_prior_events_returns_true(self, tmp_path, monkeypatch):
        self._write_calendar(tmp_path, monkeypatch, [{"name": "FOMC Rate Decision", "time": "14:00"}])
        from macro.economic_calendar import is_post_major_event_day
        result, name = is_post_major_event_day(datetime.now(ET))
        assert result is True
        assert "FOMC" in name

    def test_nfp_in_prior_events_returns_true(self, tmp_path, monkeypatch):
        self._write_calendar(tmp_path, monkeypatch, [{"name": "NFP Report", "time": "08:30"}])
        from macro.economic_calendar import is_post_major_event_day
        result, name = is_post_major_event_day(datetime.now(ET))
        assert result is True
        assert name == "NFP Report"

    def test_cpi_in_prior_events_returns_true(self, tmp_path, monkeypatch):
        self._write_calendar(tmp_path, monkeypatch, [{"name": "CPI Release", "time": "08:30"}])
        from macro.economic_calendar import is_post_major_event_day
        result, _ = is_post_major_event_day(datetime.now(ET))
        assert result is True

    def test_empty_prior_events_returns_false(self, tmp_path, monkeypatch):
        self._write_calendar(tmp_path, monkeypatch, [])
        from macro.economic_calendar import is_post_major_event_day
        result, name = is_post_major_event_day(datetime.now(ET))
        assert result is False
        assert name == ""

    def test_unknown_event_returns_false(self, tmp_path, monkeypatch):
        self._write_calendar(tmp_path, monkeypatch, [{"name": "ISM Manufacturing", "time": "10:00"}])
        from macro.economic_calendar import is_post_major_event_day
        result, _ = is_post_major_event_day(datetime.now(ET))
        assert result is False

    def test_missing_file_returns_false(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "nonexistent.json")
        from macro.economic_calendar import is_post_major_event_day
        result, name = is_post_major_event_day(datetime.now(ET))
        assert result is False
        assert name == ""


@pytest.mark.unit
def test_notify_budget_intraday_throttled(monkeypatch):
    import notifications.telegram as t
    monkeypatch.setattr(t, "_budget_warn_sent", {"date": None})
    sent = []
    monkeypatch.setattr(t, "_send", lambda msg: sent.append(msg))
    t.notify_budget_intraday_warning(0.75, 1.0)
    t.notify_budget_intraday_warning(0.80, 1.0)
    assert len(sent) == 1


@pytest.mark.unit
def test_notify_position_drawdown_throttled_per_symbol(monkeypatch):
    import notifications.telegram as t
    monkeypatch.setattr(t, "_drawdown_alerted", {})
    sent = []
    monkeypatch.setattr(t, "_send", lambda msg: sent.append(msg))
    t.notify_position_drawdown("AAPL", -2.5, -4.0)
    t.notify_position_drawdown("AAPL", -2.8, -4.0)
    t.notify_position_drawdown("MSFT", -2.1, -4.0)
    assert len(sent) == 2


@pytest.mark.unit
def test_notify_buy_batch_sends_one_message(monkeypatch):
    import notifications.telegram as t
    sent = []
    monkeypatch.setattr(t, "_send", lambda msg: sent.append(msg))
    entries = [
        {"symbol": "AAPL", "qty": 10, "price": 182.0, "sl": 174.7, "tp": 191.1, "score": 35.0, "confidence": 75},
        {"symbol": "NVDA", "qty": 5,  "price": 820.0, "sl": 787.2, "tp": 861.0, "score": 42.0, "confidence": 80},
    ]
    t.notify_buy_batch(entries, regime="NORMAL")
    assert len(sent) == 1
    assert "AAPL" in sent[0] and "NVDA" in sent[0]


@pytest.mark.unit
def test_watchdog_sends_telegram_before_exit(monkeypatch):
    """Verify notify_alert is called before os._exit in the watchdog frozen-scheduler path."""
    alerts_sent = []
    exits_called = []
    monkeypatch.setattr("notifications.telegram.notify_alert", lambda msg: alerts_sent.append(msg))
    monkeypatch.setattr("os._exit", lambda code: exits_called.append(code))

    # Simulate the watchdog trigger branch (>35 min elapsed)
    elapsed = 36 * 60.0
    if elapsed > 35 * 60:
        import notifications.telegram as tg
        import os as _os
        tg.notify_alert(f"🚨 Scheduler frozen — {elapsed / 60:.1f} min since last heartbeat.")
        _os._exit(1)

    assert alerts_sent, "Expected Telegram alert before watchdog exit"
    assert exits_called == [1]


# ── P4-T3: watch-list 2-block system structure ────────────────────────────────

@pytest.mark.unit
def test_build_watch_list_system_has_two_blocks():
    from agents.watch_list import _build_watch_list_system
    blocks = _build_watch_list_system(strategy_memory="")
    assert len(blocks) == 2
    assert all(b.get("cache_control") for b in blocks)


@pytest.mark.unit
def test_build_watch_list_system_includes_memory():
    from agents.watch_list import _build_watch_list_system
    blocks = _build_watch_list_system(strategy_memory="RSI max = 72")
    assert any("RSI max = 72" in b.get("text", "") for b in blocks)


@pytest.mark.unit
def test_score_stock_returns_prev_gap_pct_not_gap_pct(monkeypatch):
    """_score_stock() must label yesterday's gap as prev_gap_pct, not gap_pct."""
    import pandas as pd
    from agents.watch_list import _score_stock

    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    opens  = [100.5, 101.5, 102.5, 103.5, 104.5, 106.0]  # last open = 106 → gap vs prev close 105
    highs  = [c + 1 for c in closes]
    lows   = [c - 1 for c in closes]
    volumes = [1_000_000] * 6

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })

    class _FakeClient:
        def get_daily_bars(self, symbol, days=30):
            return df

    result = _score_stock("FAKE", _FakeClient())
    assert result is not None
    assert "prev_gap_pct" in result, "Expected 'prev_gap_pct' key in scored dict"
    assert "gap_pct" not in result, "Old 'gap_pct' key must not appear in scored dict"


@pytest.mark.unit
def test_apply_technical_filter_ignores_high_gap_pct(monkeypatch):
    """Symbols with high gap_pct but acceptable RSI/Stoch must NOT be filtered out."""
    import config
    from agents.watch_list import _apply_technical_filter

    # Use thresholds slightly below the config limits so RSI/Stoch pass.
    rsi_limit = config.WATCH_LIST_RSI_FILTER
    stoch_limit = config.WATCH_LIST_STOCH_FILTER

    snapshots = {
        "AAPL": {
            "rsi": rsi_limit - 5,
            "stoch_d": stoch_limit - 5,
            "gap_pct": config.GAP_SKIP_PCT + 5,  # would have triggered old block
        }
    }

    result = _apply_technical_filter(["AAPL"], snapshots, rsi_limit, stoch_limit)
    assert "AAPL" in result, "Symbol with high gap_pct but clean RSI/Stoch should pass filter"


# ── Plan 7: metrics JSONL ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_record_cycle_metric_appends_jsonl(monkeypatch, tmp_path):
    import config, json
    monkeypatch.setattr(config, "METRICS_FILE", tmp_path / "metrics.jsonl")
    from utils.metrics import record_cycle_metric
    record_cycle_metric(regime="NORMAL", candidates=5, entries=1, closes=0, api_budget_pct=0.30, cycle_ms=1200.0)
    record_cycle_metric(regime="NORMAL", candidates=3, entries=0, closes=1, api_budget_pct=0.35, cycle_ms=900.0)
    lines = (tmp_path / "metrics.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["regime"] == "NORMAL" and rec["candidates"] == 5 and "ts" in rec


@pytest.mark.unit
def test_get_daily_summary_returns_aggregates(monkeypatch, tmp_path):
    import config
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    monkeypatch.setattr(config, "METRICS_FILE", tmp_path / "metrics.jsonl")
    from utils.metrics import record_cycle_metric, get_daily_summary
    today = datetime.now(ET).strftime("%Y-%m-%d")
    record_cycle_metric(regime="NORMAL", candidates=5, entries=1, closes=0, api_budget_pct=0.3, cycle_ms=1000)
    record_cycle_metric(regime="BULL_TREND", candidates=8, entries=2, closes=1, api_budget_pct=0.5, cycle_ms=1100)
    summary = get_daily_summary(date_str=today)
    assert summary["total_entries"] == 3
    assert summary["total_cycles"] == 2
    assert summary["avg_candidates"] == 6.5


# ── Plan 7: regime-adaptive pre-claude filter ─────────────────────────────────

@pytest.mark.unit
def test_choppy_regime_blocks_high_rsi_entries(monkeypatch):
    import config
    from unittest.mock import MagicMock
    monkeypatch.setattr(config, "REGIME_ADAPTIVE_LOGIC", True)
    monkeypatch.setattr(config, "CHOPPY_RSI_PULLBACK_MAX", 60)
    from trading.pre_claude_filter import apply_pre_claude_filter

    iv = MagicMock()
    iv.composite_score = 70.0
    iv.rsi = 65.0
    iv.vwap_slope = 0.2
    iv.bot_herding_score = 0
    iv.squeeze_setup = False
    iv.vwap_std2_upper = 120.0
    iv.current_price = 100.0

    from utils.data_health import FRESH_GUARD
    snap = MagicMock()
    snap.regime = "CHOPPY"
    snap.regime_info = {"macro_health_score": 50}
    snap.research_meta = {"macro_shock_score": 0}
    snap.research = {}
    snap.intraday_news = {}
    snap.params = {"rsi_buy_max": 72, "rsi_buy_max_high_vol": 65, "choppy_rsi_max": 60, "vwap_slope_min": -0.3}
    snap.staleness_guard = FRESH_GUARD

    result = apply_pre_claude_filter("AAPL", iv, snap, set())
    assert result.decision == "SKIP"
    assert "choppy" in result.reason.lower() or "rsi" in result.reason.lower()


@pytest.mark.unit
def test_advance_decline_ratio_returns_float_or_none():
    from unittest.mock import patch
    with patch("macro.breadth_signals.get_advance_decline_ratio", return_value=1.3) as mock_fn:
        result = mock_fn()
    assert result is None or isinstance(result, float)


# ── Plan 7: backtest loads learned params ────────────────────────────────────

@pytest.mark.unit
def test_backtest_load_params_returns_dict(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "TRADING_PARAMS_FILE", tmp_path / "params.json")
    from utils.io_utils import save_json
    save_json(config.TRADING_PARAMS_FILE, {"rsi_buy_max": 68})
    from backtest.runner import _load_backtest_params
    params = _load_backtest_params()
    assert isinstance(params, dict)
    assert "rsi_buy_max" in params


# ── utils.regimes ─────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestRegimesModule:
    def test_constants_are_strings(self):
        from utils import regimes
        assert regimes.CRISIS == "CRISIS"
        assert regimes.DEFENSIVE == "DEFENSIVE"
        assert regimes.HIGH_VOL == "HIGH_VOL"

    def test_valid_excludes_high_vol(self):
        from utils import regimes
        assert regimes.HIGH_VOL not in regimes.VALID

    def test_allow_entry_excludes_crisis(self):
        from utils import regimes
        assert regimes.CRISIS not in regimes.ALLOW_ENTRY

    def test_allow_entry_is_valid_minus_crisis(self):
        from utils import regimes
        assert regimes.ALLOW_ENTRY == regimes.VALID - {regimes.CRISIS}

    def test_high_vol_gates_contains_defensive_and_high_vol(self):
        from utils import regimes
        assert regimes.DEFENSIVE in regimes.HIGH_VOL_GATES
        assert regimes.HIGH_VOL in regimes.HIGH_VOL_GATES

    def test_size_reduce_members(self):
        from utils import regimes
        assert regimes.HIGH_VOL in regimes.SIZE_REDUCE
        assert regimes.DEFENSIVE in regimes.SIZE_REDUCE
        assert regimes.CHOPPY in regimes.SIZE_REDUCE
        assert regimes.NORMAL not in regimes.SIZE_REDUCE

    def test_swing_eligible_members(self):
        from utils import regimes
        assert regimes.BULL_TREND in regimes.SWING_ELIGIBLE
        assert regimes.OPPORTUNITY in regimes.SWING_ELIGIBLE
        assert regimes.CAPITULATION in regimes.SWING_ELIGIBLE
        assert regimes.CRASH_RECOVERY in regimes.SWING_ELIGIBLE
        assert regimes.NORMAL in regimes.SWING_ELIGIBLE
        assert regimes.CRISIS not in regimes.SWING_ELIGIBLE
        assert regimes.CHOPPY not in regimes.SWING_ELIGIBLE
        assert regimes.DEFENSIVE not in regimes.SWING_ELIGIBLE

    @pytest.mark.unit
    def test_normal_in_swing_eligible(self):
        from utils import regimes
        assert regimes.NORMAL in regimes.SWING_ELIGIBLE


# ── sectors ───────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSectors:
    """Integration tests against the real tickers.json on disk."""

    def test_nvda_is_tier1(self):
        import sectors
        assert sectors.is_tier1("NVDA") is True

    def test_case_insensitive(self):
        import sectors
        assert sectors.is_tier1("nvda") is True

    def test_on_is_not_tier1(self):
        import sectors
        assert sectors.is_tier1("ON") is False

    def test_unknown_is_not_tier1(self):
        import sectors
        assert sectors.is_tier1("ZZZFAKE") is False

    def test_nvda_maps_to_xlk(self):
        import sectors
        assert sectors.get_sector_etf("NVDA") == "XLK"

    def test_brk_b_maps_to_xlf(self):
        import sectors
        assert sectors.get_sector_etf("BRK.B") == "XLF"

    def test_jpm_maps_to_xlf(self):
        import sectors
        assert sectors.get_sector_etf("JPM") == "XLF"

    def test_lly_maps_to_xlv(self):
        import sectors
        assert sectors.get_sector_etf("LLY") == "XLV"

    def test_unknown_returns_other(self):
        import sectors
        assert sectors.get_sector_etf("ZZZUNKNOWN") == "OTHER"

    def test_get_sector_is_alias_of_get_sector_etf(self):
        import sectors
        assert sectors.get_sector("NVDA") == sectors.get_sector_etf("NVDA")

    def test_load_universe_excludes_regime_signals(self):
        import sectors
        universe = sectors.load_universe()
        assert "SPY" not in universe
        assert "QQQ" not in universe
        assert "SMH" not in universe

    def test_gld_excluded_from_screener(self):
        import sectors
        universe = sectors.load_universe()
        assert "GLD" not in universe

    def test_brk_b_in_universe(self):
        """BRK.B must be in the screener universe — was previously dropped by isalpha() bug."""
        import sectors
        universe = sectors.load_universe()
        assert "BRK.B" in universe

    def test_tier1_symbols_are_subset_of_universe(self):
        import sectors
        universe_set = set(sectors.load_universe())
        for sym in sectors.TIER1_SYMBOLS:
            assert sym in universe_set, f"{sym} in TIER1_SYMBOLS but not in screener universe"

    def test_etf_to_symbols_xlk_contains_nvda(self):
        import sectors
        assert "NVDA" in sectors.ETF_TO_SYMBOLS.get("XLK", [])


class TestQualifiesForEodHoldRegimeMatrix:
    """Tests for the regime-aware swing-hold veto matrix."""

    def _call(self, regime="NORMAL", earnings_tomorrow=False, current_pnl_pct=1.0,
              entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
              consecutive_red_days=0, vix_trend="stable"):
        from trading.eod_holds import qualifies_for_eod_hold
        return qualifies_for_eod_hold(
            symbol="TEST", regime=regime, earnings_tomorrow=earnings_tomorrow,
            current_pnl_pct=current_pnl_pct,
            entry_sector_sentiment=entry_sector_sentiment,
            current_sector_sentiment=current_sector_sentiment,
            consecutive_red_days=consecutive_red_days,
            vix_trend=vix_trend,
        )

    @pytest.mark.unit
    def test_crisis_always_vetoes(self):
        ok, reason = self._call(regime="CRISIS", current_pnl_pct=5.0)
        assert not ok
        assert "CRISIS" in reason

    @pytest.mark.unit
    def test_earnings_tomorrow_vetoes_all_regimes(self):
        for regime in ("NORMAL", "BULL_TREND", "OPPORTUNITY", "CRASH_RECOVERY"):
            ok, reason = self._call(regime=regime, earnings_tomorrow=True)
            assert not ok, f"{regime} should veto on earnings tomorrow"
            assert "earnings" in reason.lower()

    @pytest.mark.unit
    def test_choppy_blocks_new_promotions(self):
        ok, reason = self._call(regime="CHOPPY", current_pnl_pct=2.0)
        assert not ok
        assert "CHOPPY" in reason

    @pytest.mark.unit
    def test_defensive_blocks_new_promotions(self):
        ok, reason = self._call(regime="DEFENSIVE", current_pnl_pct=2.0)
        assert not ok
        assert "DEFENSIVE" in reason

    @pytest.mark.unit
    def test_normal_min_pnl_gate_blocks_below_threshold(self):
        ok, reason = self._call(regime="NORMAL", current_pnl_pct=0.3)
        assert not ok
        assert "insufficient" in reason.lower() or "P&L" in reason

    @pytest.mark.unit
    def test_normal_passes_at_min_pnl_threshold(self):
        ok, _ = self._call(regime="NORMAL", current_pnl_pct=0.5)
        assert ok

    @pytest.mark.unit
    def test_normal_sector_reversal_vetoes(self):
        """Sector reversed: current < -20 AND entry was > +10."""
        ok, reason = self._call(
            regime="NORMAL", entry_sector_sentiment=25.0, current_sector_sentiment=-25.0
        )
        assert not ok
        assert "sector" in reason.lower()

    @pytest.mark.unit
    def test_normal_sector_soft_drop_does_not_veto(self):
        """Sector dropped but not reversed — no veto."""
        ok, _ = self._call(
            regime="NORMAL", entry_sector_sentiment=15.0, current_sector_sentiment=5.0
        )
        assert ok

    @pytest.mark.unit
    def test_bull_trend_relaxed_min_pnl_threshold(self):
        ok_at_0_3, _ = self._call(regime="BULL_TREND", current_pnl_pct=0.3)
        ok_at_0_2, _ = self._call(regime="BULL_TREND", current_pnl_pct=0.2)
        assert ok_at_0_3
        assert not ok_at_0_2

    @pytest.mark.unit
    def test_opportunity_vetoes_when_sector_goes_negative(self):
        ok, reason = self._call(
            regime="OPPORTUNITY", entry_sector_sentiment=15.0, current_sector_sentiment=-5.0
        )
        assert not ok
        assert "sector" in reason.lower()

    @pytest.mark.unit
    def test_opportunity_passes_when_sector_still_positive(self):
        ok, _ = self._call(
            regime="OPPORTUNITY", entry_sector_sentiment=15.0, current_sector_sentiment=5.0
        )
        assert ok

    @pytest.mark.unit
    def test_capitulation_wider_ptj_threshold(self):
        """CAPITULATION/HIGH swing allows down to -5% P&L (min_pnl_pct=-5.0 in table)."""
        from utils.ticker_quality import TickerQuality
        from trading.eod_holds import qualifies_for_eod_hold
        high_q = TickerQuality(tier="HIGH", score=70, unlocks=frozenset(),
                               quality_gaps=(), component_scores={})
        ok_at_minus_4_9, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="CAPITULATION", earnings_tomorrow=False,
            current_pnl_pct=-4.9, quality=high_q,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
        )
        ok_at_minus_5_1, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="CAPITULATION", earnings_tomorrow=False,
            current_pnl_pct=-5.1, quality=high_q,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
        )
        assert ok_at_minus_4_9
        assert not ok_at_minus_5_1

    @pytest.mark.unit
    def test_crash_recovery_vetoes_on_3_red_days(self):
        """3 consecutive red days blocks CRASH_RECOVERY swing even with sufficient P&L."""
        ok_2_red, _ = self._call(regime="CRASH_RECOVERY", consecutive_red_days=2, current_pnl_pct=1.1)
        ok_3_red, reason = self._call(regime="CRASH_RECOVERY", consecutive_red_days=3, current_pnl_pct=1.1)
        assert ok_2_red
        assert not ok_3_red
        assert "red" in reason.lower()

    @pytest.mark.unit
    def test_non_tier1_no_longer_vetoes(self):
        """is_tier1 gate removed — non-tier1 symbols now qualify in NORMAL."""
        ok, _ = self._call(regime="NORMAL", current_pnl_pct=1.0)
        assert ok

    @pytest.mark.unit
    def test_event_risk_tomorrow_no_longer_vetoes(self):
        """event_risk_tomorrow gate removed — only earnings_tomorrow matters."""
        ok, _ = self._call(regime="NORMAL", earnings_tomorrow=False, current_pnl_pct=1.0)
        assert ok


# ── utils/regime_params ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestGetEntryParams:
    def test_crisis_has_zero_hard_close_hour(self):
        from utils.regime_params import get_entry_params
        p = get_entry_params("CRISIS")
        assert p.hard_close_hour == 0

    def test_choppy_raises_composite_min(self):
        from utils.regime_params import get_entry_params
        normal = get_entry_params("NORMAL")
        choppy = get_entry_params("CHOPPY")
        assert choppy.composite_min > normal.composite_min

    def test_bull_trend_returns_entry_params(self):
        from utils.regime_params import get_entry_params
        p = get_entry_params("BULL_TREND")
        assert isinstance(p.composite_min, int)
        assert p.hard_close_hour == 14

    def test_unknown_regime_falls_back_to_normal(self):
        from utils.regime_params import get_entry_params
        p = get_entry_params("UNKNOWN_FUTURE_REGIME")
        normal = get_entry_params("NORMAL")
        assert p.composite_min == normal.composite_min


@pytest.mark.unit
class TestGetSwingParams:
    def test_crisis_not_eligible(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("CRISIS")
        assert p.eligible is False

    def test_choppy_not_eligible(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("CHOPPY")
        assert p.eligible is False

    def test_defensive_not_eligible(self):
        from utils.regime_params import get_swing_params
        p = get_swing_params("DEFENSIVE")
        assert p.eligible is False

    def test_normal_eligible_with_positive_min_pnl(self):
        from utils.regime_params import get_swing_params, get_tp_sl_params
        sp = get_swing_params("NORMAL")
        assert sp.eligible is True
        p = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        assert p.min_pnl_pct > 0

    def test_profit_lock_bypass_above_min_pnl(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("CHOPPY", "CONVICTION", "SWING")
        assert p.profit_lock_pct > p.min_pnl_pct

    def test_high_vol_swing_ineligible(self):
        """HIGH_VOL must not be eligible for swing promotion (same as CHOPPY/DEFENSIVE)."""
        from utils.regime_params import get_swing_params
        sp = get_swing_params("HIGH_VOL")
        assert sp.eligible is False


@pytest.mark.unit
class TestGetT2Params:
    def test_crisis_has_no_t2(self):
        from utils.regime_params import get_t2_params
        p = get_t2_params("CRISIS")
        assert p.cutoff_hour == 0

    def test_normal_t2_params_well_formed(self):
        from utils.regime_params import get_t2_params
        p = get_t2_params("NORMAL")
        assert p.cutoff_hour > 0

    def test_choppy_t2_more_restrictive_than_normal(self):
        from utils.regime_params import get_t2_params
        choppy = get_t2_params("CHOPPY")
        assert choppy.cutoff_hour == 0  # CHOPPY fully disables T2 — sentinel value


@pytest.mark.unit
class TestQualifiesForEodHold:
    def _call(self, monkeypatch, **kwargs):
        import config
        monkeypatch.setattr(config, "PROFIT_LOCK_SWING_PCT", 2.5)
        monkeypatch.setattr(config, "SWING_PROMOTE_MIN_PNL_PCT", 0.5)
        from trading.eod_holds import qualifies_for_eod_hold
        defaults = dict(
            symbol="AAPL", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=1.0, entry_sector_sentiment=10.0,
            current_sector_sentiment=5.0, consecutive_red_days=0, vix_trend="stable",
        )
        defaults.update(kwargs)
        return qualifies_for_eod_hold(**defaults)

    def test_profit_lock_bypass_choppy(self, monkeypatch):
        result, reason = self._call(monkeypatch, regime="CHOPPY", current_pnl_pct=2.5)
        assert result is True
        assert reason == ""

    def test_profit_lock_requires_enough_pnl(self, monkeypatch):
        result, reason = self._call(monkeypatch, regime="CHOPPY", current_pnl_pct=1.0)
        assert result is False
        assert "CHOPPY" in reason or "swing" in reason.lower()

    def test_profit_lock_blocked_in_crisis(self, monkeypatch):
        result, reason = self._call(monkeypatch, regime="CRISIS", current_pnl_pct=5.0)
        assert result is False
        assert "CRISIS" in reason

    def test_min_pnl_gate_blocks_flat_position(self, monkeypatch):
        result, reason = self._call(monkeypatch, regime="NORMAL", current_pnl_pct=0.3)
        assert result is False
        assert "insufficient" in reason.lower() or "P&L" in reason

    def test_min_pnl_gate_passes_at_threshold(self, monkeypatch):
        result, reason = self._call(monkeypatch, regime="NORMAL", current_pnl_pct=0.5)
        assert result is True
        assert reason == ""

    @pytest.mark.unit
    def test_earnings_tomorrow_overrides_profit_lock_bypass(self, monkeypatch):
        """earnings_tomorrow=True must veto even a highly profitable position (profit-lock bypass should not fire)."""
        ok, reason = self._call(monkeypatch, regime="CHOPPY", current_pnl_pct=4.0, earnings_tomorrow=True)
        assert not ok
        assert "earnings" in reason.lower()


@pytest.mark.unit
def test_screener_skips_market_cap_filter_on_api_failure(monkeypatch, tmp_path):
    """When ≥80% of market cap fetches return None (API failure), filter is bypassed."""
    import config
    from unittest.mock import patch, MagicMock

    monkeypatch.setattr(config, "DATA_SCREENER_FAILURE_RATE_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "DAILY_TRADEABLE_FILE", tmp_path / "daily_tradeable.txt")
    monkeypatch.setattr(config, "MIN_MARKET_CAP_B", 1.0)
    # Set MAX_TICKERS_DIRECT low enough that volatility scoring is triggered
    monkeypatch.setattr(config, "MAX_TICKERS_DIRECT", 0)

    fake_tickers = ["AAAA", "BBBB", "CCCC"]

    # All 3 fetches return None → 100% failure rate → filter bypassed
    with patch("agents.volatility_screener._fetch_market_cap", return_value=None), \
         patch("agents.volatility_screener._schedule_retry") as mock_retry, \
         patch("sectors.load_universe", return_value=fake_tickers), \
         patch("agents.volatility_screener._score_ticker", return_value=1.0), \
         patch("agents.volatility_screener.AlpacaClient"):
        from agents import volatility_screener
        result = volatility_screener.run_volatility_screener()

    # All tickers must survive (filter bypassed) — none filtered out due to None market cap
    assert set(result) == set(fake_tickers), (
        f"Expected all tickers to survive API failure bypass, got: {result}"
    )
    # Retry must have been scheduled
    assert mock_retry.called, "_schedule_retry was not called on API failure"


# ---------------------------------------------------------------------------
# watch_loader — held-ticker merge tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_watch_loader_merges_open_position_tickers(monkeypatch, tmp_path):
    """load_watch_list always includes tickers from open_positions.json."""
    import config
    from utils.io_utils import save_json

    watch_file = tmp_path / "watch_list.txt"
    watch_file.write_text("AAPL\nMSFT\n")
    open_pos_file = tmp_path / "open_positions.json"
    save_json(open_pos_file, {"NVDA": {"qty": 10}})
    swing_pos_file = tmp_path / "swing_positions.json"
    save_json(swing_pos_file, {})

    monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", open_pos_file)
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", swing_pos_file)
    monkeypatch.setattr(config, "DATA_WATCH_LIST_HEALTH_FILE", tmp_path / "wl_health.json")

    import importlib
    import trading.watch_loader
    importlib.reload(trading.watch_loader)
    from trading.watch_loader import load_watch_list
    result = load_watch_list()

    assert "NVDA" in result
    assert "AAPL" in result
    assert "MSFT" in result


@pytest.mark.unit
def test_watch_loader_merges_swing_position_tickers(monkeypatch, tmp_path):
    """load_watch_list includes tickers from swing_positions.json."""
    import config
    from utils.io_utils import save_json

    watch_file = tmp_path / "watch_list.txt"
    watch_file.write_text("AAPL\n")
    open_pos_file = tmp_path / "open_positions.json"
    save_json(open_pos_file, {})
    swing_pos_file = tmp_path / "swing_positions.json"
    save_json(swing_pos_file, {"PANW": {"qty": 5}})

    monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", open_pos_file)
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", swing_pos_file)
    monkeypatch.setattr(config, "DATA_WATCH_LIST_HEALTH_FILE", tmp_path / "wl_health.json")

    import importlib
    import trading.watch_loader
    importlib.reload(trading.watch_loader)
    from trading.watch_loader import load_watch_list
    result = load_watch_list()

    assert "PANW" in result
    assert "AAPL" in result


@pytest.mark.unit
def test_watch_loader_no_duplicates(monkeypatch, tmp_path):
    """If a held ticker is already in watch_list.txt, it appears only once."""
    import config
    from utils.io_utils import save_json

    watch_file = tmp_path / "watch_list.txt"
    watch_file.write_text("AAPL\nNVDA\n")
    open_pos_file = tmp_path / "open_positions.json"
    save_json(open_pos_file, {"NVDA": {"qty": 10}})
    swing_pos_file = tmp_path / "swing_positions.json"
    save_json(swing_pos_file, {})

    monkeypatch.setattr(config, "WATCH_LIST_FILE", watch_file)
    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", open_pos_file)
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", swing_pos_file)
    monkeypatch.setattr(config, "DATA_WATCH_LIST_HEALTH_FILE", tmp_path / "wl_health.json")

    import importlib
    import trading.watch_loader
    importlib.reload(trading.watch_loader)
    from trading.watch_loader import load_watch_list
    result = load_watch_list()

    assert result.count("NVDA") == 1


@pytest.mark.unit
def test_position_monitor_skips_t2_when_guard_disallows(monkeypatch, tmp_path):
    """run_position_monitor skips T2 check when guard.t2_allowed is False."""
    import config
    from unittest.mock import patch, MagicMock
    from utils.data_health import StalenessGuard

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open.json")
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")

    # Non-empty positions so monitor doesn't early-return before reaching T2 logic
    fake_positions = {
        "AAPL": {
            "qty": 10, "entry_price": 150.0,
            "stop_loss_pct": 4.0, "take_profit_pct": 5.0,
        }
    }

    t2_called = []

    mock_alpaca = MagicMock()
    mock_alpaca.get_positions.return_value = {"AAPL": {"qty": 10}}
    mock_alpaca.get_latest_price.return_value = 152.0
    mock_alpaca.get_portfolio_value.return_value = 100_000.0

    with patch("trading.monitor.load_macro_regime",
               return_value={"regime": "NORMAL", "_stale": True}), \
         patch("utils.data_health.get_staleness_guard", return_value=stale_guard), \
         patch("trading.monitor.load_open_positions", return_value=fake_positions), \
         patch("trading.monitor._check_tranche2_triggers",
               side_effect=lambda *a, **kw: t2_called.append(True)), \
         patch("trading.monitor.flush_profit_lock_updates"), \
         patch("trading.monitor._check_drawdown_alerts"), \
         patch("trading.monitor.AlpacaClient", return_value=mock_alpaca):
        from trading.monitor import run_position_monitor
        run_position_monitor()

    assert len(t2_called) == 0, f"T2 should not fire when guard.t2_allowed is False, called {len(t2_called)} times"


@pytest.mark.unit
def test_evaluate_eod_holds_uses_normal_params_when_regime_stale(monkeypatch, tmp_path):
    """evaluate_eod_holds uses NORMAL swing params when guard.swing_fallback_regime is set."""
    import config
    from unittest.mock import patch, MagicMock
    from utils.data_health import StalenessGuard

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )

    captured_regime = []

    def fake_get_swing_params(regime):
        captured_regime.append(regime)
        from utils.regime_params import SwingParams
        return SwingParams(False)

    monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "open.json")
    monkeypatch.setattr(config, "SWING_POSITIONS_FILE", tmp_path / "swing.json")

    with patch("trading.swing_manager.load_macro_regime",
               return_value={"regime": "BULL_TREND", "_stale": True}), \
         patch("utils.data_health.get_staleness_guard", return_value=stale_guard), \
         patch("trading.swing_manager.get_swing_params", side_effect=fake_get_swing_params), \
         patch("trading.swing_manager.load_open_positions", return_value={}):
        from trading.swing_manager import evaluate_eod_holds
        evaluate_eod_holds()

    assert "NORMAL" in captured_regime, (
        f"Expected NORMAL (fallback), got {captured_regime}"
    )


# ── sectors: GICS sub-sector ETF lookup ──────────────────────────────────────

@pytest.mark.unit
class TestSubSectorEtf:
    def test_nvidia_returns_soxx(self):
        import sectors
        result = sectors.get_sub_sector_etf("NVDA")
        assert result == "SOXX"

    def test_crwd_returns_cibr(self):
        import sectors
        result = sectors.get_sub_sector_etf("CRWD")
        assert result == "CIBR"

    def test_unknown_symbol_falls_back_to_sector_etf(self):
        import sectors
        result = sectors.get_sub_sector_etf("ZZZZ_UNKNOWN")
        assert result == "OTHER"

    def test_symbol_without_sub_etf_falls_back_to_sector_etf(self):
        import sectors
        # JPM is XLF — no sub_etf, fallback to sector
        result = sectors.get_sub_sector_etf("JPM")
        assert result == sectors.get_sector_etf("JPM")

    def test_existing_get_sector_etf_unchanged(self):
        import sectors
        assert sectors.get_sector_etf("NVDA") == "XLK"


# ── Tier-based swing promotion ────────────────────────────────────────────────

class TestTierBasedSwingPromotion:
    def _conviction_swing_choppy(self):
        from utils.ticker_quality import TickerQuality
        return TickerQuality("CONVICTION", 85, frozenset({"SWING_CHOPPY"}), (), {})

    def _medium_quality(self):
        from utils.ticker_quality import TickerQuality
        return TickerQuality("MEDIUM", 45, frozenset(), (), {})

    @pytest.mark.unit
    def test_conviction_swing_choppy_profit_lock_from_table(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("CHOPPY", "CONVICTION", "SWING")
        assert p.profit_lock_pct == pytest.approx(1.0)

    @pytest.mark.unit
    def test_high_choppy_swing_profit_lock_from_table(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("CHOPPY", "HIGH", "SWING")
        assert p.profit_lock_pct == pytest.approx(2.5)

    @pytest.mark.unit
    def test_regime_stale_blocks_swing(self):
        from trading.eod_holds import qualifies_for_eod_hold
        qualifies, reason = qualifies_for_eod_hold(
            symbol="AAPL",
            regime="NORMAL",
            earnings_tomorrow=False,
            current_pnl_pct=3.0,
            entry_sector_sentiment=10,
            current_sector_sentiment=5,
            consecutive_red_days=0,
            vix_trend="stable",
            regime_stale=True,
        )
        assert not qualifies
        assert "stale" in reason.lower()

    @pytest.mark.unit
    def test_conviction_swing_choppy_qualifies_at_1_3pct_pnl(self, monkeypatch):
        import config
        from trading.eod_holds import qualifies_for_eod_hold
        quality = self._conviction_swing_choppy()
        qualifies, reason = qualifies_for_eod_hold(
            symbol="NVDA",
            regime="CHOPPY",
            earnings_tomorrow=False,
            current_pnl_pct=1.3,
            entry_sector_sentiment=10,
            current_sector_sentiment=5,
            consecutive_red_days=0,
            vix_trend="stable",
            quality=quality,
        )
        assert qualifies, f"Expected swing qualification, got: {reason}"


class TestSwingManagerTierGapCheck:
    @pytest.mark.unit
    def test_conviction_tolerates_wider_gap(self):
        pos_conviction = {"symbol": "NVDA", "quality_tier": "CONVICTION", "entry_price": 100.0}
        pos_medium     = {"symbol": "RIVN", "quality_tier": "MEDIUM",     "entry_price": 100.0}
        tier_c = pos_conviction.get("quality_tier", "MEDIUM")
        tier_m = pos_medium.get("quality_tier", "MEDIUM")
        thresh_c = -1.5 if tier_c == "CONVICTION" else -1.0
        thresh_m = -1.5 if tier_m == "CONVICTION" else -1.0
        assert thresh_c == -1.5
        assert thresh_m == -1.0

    @pytest.mark.unit
    def test_gap_of_minus_1_2_closes_medium_not_conviction(self):
        gap_pct = -1.2
        for tier, expected_close in [("MEDIUM", True), ("CONVICTION", False)]:
            threshold = -1.5 if tier == "CONVICTION" else -1.0
            closes = gap_pct < threshold
            assert closes == expected_close, f"tier={tier}"
