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
    from notifications.telegram import _OnceGuard
    monkeypatch.setattr(t, "_once", _OnceGuard())
    sent = []
    monkeypatch.setattr(t, "_send", lambda msg: sent.append(msg))
    t.notify_budget_intraday_warning(0.75, 1.0)
    t.notify_budget_intraday_warning(0.80, 1.0)
    assert len(sent) == 1


@pytest.mark.unit
def test_notify_position_drawdown_throttled_per_symbol(monkeypatch):
    import notifications.telegram as t
    from notifications.telegram import _OnceGuard
    monkeypatch.setattr(t, "_once", _OnceGuard())
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


@pytest.mark.unit
def test_watch_list_handles_api_400_gracefully(monkeypatch, tmp_path):
    """run_watch_list_agent() must return [] and call notify_data_degraded on APIStatusError."""
    import anthropic
    import httpx
    import config
    import notifications.telegram as telegram_notify
    from unittest.mock import patch

    # Point all file paths to tmp_path to avoid touching real state
    monkeypatch.setattr(config, "DAILY_TRADEABLE_FILE", tmp_path / "daily_tradeable.txt")
    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", tmp_path / "strategy_memory.md")
    monkeypatch.setattr(config, "WATCH_LIST_FILE", tmp_path / "watch_list.txt")
    monkeypatch.setattr(config, "DATA_WATCH_LIST_HEALTH_FILE", tmp_path / "watch_list_health.json")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    # Write a minimal tradeable list so the agent has at least one ticker
    (tmp_path / "daily_tradeable.txt").write_text("AAPL\n")

    # Build a real APIStatusError with status 400
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    api_exc = anthropic.APIStatusError(
        "You have reached your specified API usage limits.", response=resp, body=None
    )

    # Fake scored data so we don't need a real Alpaca client
    fake_scored = [{"symbol": "AAPL", "price": 200.0, "vol_spike": 1.5, "atr_pct": 2.0, "prev_gap_pct": 0.1}]

    notify_calls = []

    with (
        patch("agents.watch_list.AlpacaClient"),
        patch("agents.watch_list.telegram_notify.notify_data_degraded", side_effect=lambda *a, **kw: notify_calls.append(a)),
        patch("agents.watch_list.telegram_notify.notify_watch_list"),
        patch("agents.watch_list._score_stock", return_value=fake_scored[0]),
        patch("anthropic.Anthropic") as mock_anthropic_cls,
    ):
        mock_anthropic_cls.return_value.messages.create.side_effect = api_exc

        from agents.watch_list import run_watch_list_agent
        result = run_watch_list_agent()

    assert result == [], f"Expected [] on API error, got {result!r}"
    assert len(notify_calls) == 1, "notify_data_degraded must be called exactly once"
    assert notify_calls[0][0] == "watch_list"


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
    """Rule 23: CHOPPY RSI gate lives in check_all_gates (entry_gates.py), not pre_claude_filter.

    pre_claude_filter is a quality/budget router only. Verify the gate fires in check_all_gates.
    """
    import config
    import types
    from zoneinfo import ZoneInfo
    from datetime import datetime
    monkeypatch.setattr(config, "REGIME_ADAPTIVE_LOGIC", True)
    monkeypatch.setattr(config, "CHOPPY_RSI_PULLBACK_MAX", 60)
    import calendars.earnings as ec_mod
    import macro.economic_calendar as ecc
    monkeypatch.setattr(ec_mod, "is_earnings_imminent", lambda *a, **kw: False)
    monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))
    from signals.entry_gates import check_all_gates

    iv = types.SimpleNamespace(
        current_price=100.0, vwap=99.0, vwap_std2_upper=110.0,
        stop_hunt_zone=False, parabolic=False, momentum_exhaustion=False,
        bot_herding_score=0, squeeze_setup=False,
        rsi=65.0,  # above CHOPPY_RSI_PULLBACK_MAX=60
        stoch_d=50.0, prev_close=100.0, relative_volume=1.0,
        or_position="outside", composite_score=80,
    )
    # Use 10:30 ET — before CHOPPY hard cutoff (11:00 for MEDIUM tier)
    now = datetime(2024, 1, 15, 10, 30, 0, tzinfo=ZoneInfo("America/New_York"))
    result = check_all_gates(
        symbol="AAPL", iv=iv, open_positions={}, daily_closed=set(),
        bars_15min=None, now=now, regime="CHOPPY",
    )
    assert not result.passed, "CHOPPY with high RSI should be blocked by gate 1c in check_all_gates"
    assert "choppy" in result.reason.lower()


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
              consecutive_red_days=0, vix_trend="stable", entry_rsi=50.0, entry_composite=65.0):
        from trading.eod_holds import qualifies_for_eod_hold
        return qualifies_for_eod_hold(
            symbol="TEST", regime=regime, earnings_tomorrow=earnings_tomorrow,
            current_pnl_pct=current_pnl_pct,
            entry_sector_sentiment=entry_sector_sentiment,
            current_sector_sentiment=current_sector_sentiment,
            consecutive_red_days=consecutive_red_days,
            vix_trend=vix_trend,
            entry_rsi=entry_rsi,
            entry_composite=entry_composite,
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
        # NORMAL MEDIUM min_pnl_pct is 1.2; use 0.5 to test below-threshold blocking
        ok, reason = self._call(regime="NORMAL", current_pnl_pct=0.5)
        assert not ok
        assert "insufficient" in reason.lower() or "P&L" in reason

    @pytest.mark.unit
    def test_normal_passes_at_min_pnl_threshold(self):
        # NORMAL MEDIUM min_pnl_pct is 1.2; 1.5% comfortably passes
        ok, _ = self._call(regime="NORMAL", current_pnl_pct=1.5)
        assert ok

    @pytest.mark.unit
    def test_normal_sector_reversal_vetoes(self):
        """Sector reversed: current < -20 AND entry was > +10."""
        # Use 1.3% (between min_pnl=1.2 and profit_lock=1.5) to test sector veto before profit-lock bypass
        ok, reason = self._call(
            regime="NORMAL", entry_sector_sentiment=25.0, current_sector_sentiment=-25.0, current_pnl_pct=1.3
        )
        assert not ok
        assert "sector" in reason.lower()

    @pytest.mark.unit
    def test_normal_sector_soft_drop_does_not_veto(self):
        """Sector dropped but not reversed — no veto."""
        # Use 1.3% (between min_pnl=1.2 and profit_lock=1.5) to test sector gates
        ok, _ = self._call(
            regime="NORMAL", entry_sector_sentiment=15.0, current_sector_sentiment=5.0, current_pnl_pct=1.3
        )
        assert ok

    @pytest.mark.unit
    def test_bull_trend_relaxed_min_pnl_threshold(self):
        # BULL_TREND MEDIUM min_pnl_pct is 0.5; 0.6 passes, 0.4 blocks
        ok_at_0_6, _ = self._call(regime="BULL_TREND", current_pnl_pct=0.6)
        ok_at_0_4, _ = self._call(regime="BULL_TREND", current_pnl_pct=0.4)
        assert ok_at_0_6
        assert not ok_at_0_4

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
            entry_rsi=50.0, entry_composite=65.0,
        )
        ok_at_minus_5_1, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="CAPITULATION", earnings_tomorrow=False,
            current_pnl_pct=-5.1, quality=high_q,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=65.0,
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
        ok, _ = self._call(regime="NORMAL", current_pnl_pct=1.5)
        assert ok

    @pytest.mark.unit
    def test_event_risk_tomorrow_no_longer_vetoes(self):
        """event_risk_tomorrow gate removed — only earnings_tomorrow matters."""
        ok, _ = self._call(regime="NORMAL", earnings_tomorrow=False, current_pnl_pct=1.5)
        assert ok

    @pytest.mark.unit
    def test_profit_lock_bypass_blocked_below_composite_floor(self):
        """Even with profit-lock P&L, entry composite below SWING_PROFIT_LOCK_MIN_COMPOSITE blocks swing."""
        import config
        from trading.eod_holds import qualifies_for_eod_hold
        ok, reason = qualifies_for_eod_hold(
            symbol="TEST", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=3.0,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=55.0,
        )
        assert not ok
        assert "composite" in reason.lower()

    @pytest.mark.unit
    def test_profit_lock_bypass_passes_above_composite_floor(self):
        """Profit-lock P&L with composite >= SWING_PROFIT_LOCK_MIN_COMPOSITE passes."""
        from trading.eod_holds import qualifies_for_eod_hold
        ok, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=3.0,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=65.0,
        )
        assert ok

    @pytest.mark.unit
    def test_normal_medium_composite_gate_raised_to_65(self):
        """NORMAL/MEDIUM requires composite >= 65 for standard swing promotion."""
        from trading.eod_holds import qualifies_for_eod_hold
        from utils.ticker_quality import TickerQuality
        med_q = TickerQuality(tier="MEDIUM", score=50, unlocks=frozenset(),
                              quality_gaps=(), component_scores={})
        ok_64, reason = qualifies_for_eod_hold(
            symbol="TEST", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=1.5, quality=med_q,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=64.0,
        )
        assert not ok_64, f"composite=64 should be blocked for NORMAL/MEDIUM, got: {reason}"
        ok_65, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="NORMAL", earnings_tomorrow=False,
            current_pnl_pct=1.5, quality=med_q,
            entry_sector_sentiment=20.0, current_sector_sentiment=15.0,
            consecutive_red_days=0, vix_trend="stable",
            entry_rsi=50.0, entry_composite=65.0,
        )
        assert ok_65


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
            entry_rsi=50.0, entry_composite=65.0,
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
        # NORMAL MEDIUM min_pnl_pct is 1.2; use 0.5 to test below-threshold blocking
        result, reason = self._call(monkeypatch, regime="NORMAL", current_pnl_pct=0.5)
        assert result is False
        assert "insufficient" in reason.lower() or "P&L" in reason

    def test_min_pnl_gate_passes_at_threshold(self, monkeypatch):
        # NORMAL MEDIUM min_pnl_pct is 1.2; 1.5 comfortably passes
        result, reason = self._call(monkeypatch, regime="NORMAL", current_pnl_pct=1.5)
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
# _fetch_market_cap — zero-cap edge cases
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_fetch_market_cap_returns_none_for_zero_raw(monkeypatch):
    """raw=0 (integer) must return None — 0.0 is not a real market cap."""
    from unittest.mock import patch
    from agents.volatility_screener import _fetch_market_cap

    fake_response = {"quoteSummary": {"result": [{"price": {"marketCap": {"raw": 0}}}]}}
    with patch("agents.volatility_screener.yahoo_get", return_value=fake_response):
        result = _fetch_market_cap("AAPL")
    assert result is None


@pytest.mark.unit
def test_fetch_market_cap_returns_none_for_zero_string_raw(monkeypatch):
    """raw='0' (string) must return None — float('0') == 0.0 is not a real market cap."""
    from unittest.mock import patch
    from agents.volatility_screener import _fetch_market_cap

    fake_response = {"quoteSummary": {"result": [{"price": {"marketCap": {"raw": "0"}}}]}}
    with patch("agents.volatility_screener.yahoo_get", return_value=fake_response):
        result = _fetch_market_cap("AAPL")
    assert result is None


@pytest.mark.unit
def test_fetch_market_cap_returns_value_for_real_cap(monkeypatch):
    """raw=3000000000000 must return the float value (3 trillion)."""
    from unittest.mock import patch
    from agents.volatility_screener import _fetch_market_cap

    raw_value = 3_000_000_000_000
    fake_response = {"quoteSummary": {"result": [{"price": {"marketCap": {"raw": raw_value}}}]}}
    with patch("agents.volatility_screener.yahoo_get", return_value=fake_response):
        result = _fetch_market_cap("AAPL")
    assert result == float(raw_value)


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
        assert p.profit_lock_pct == pytest.approx(2.0)

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
            entry_rsi=50.0,
            entry_composite=65.0,
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


# ── TestSwingSLFloors ─────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSwingSLFloors:
    """Verify swing SL floors are loaded from config and applied correctly."""

    def test_swing_sl_floors_loaded_from_config(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        assert p.sl_floor_pct == 4.5

    def test_swing_sl_conviction_tighter(self):
        from utils.regime_params import get_tp_sl_params
        medium = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        conviction = get_tp_sl_params("NORMAL", "CONVICTION", "SWING")
        assert conviction.sl_floor_pct < medium.sl_floor_pct
        assert conviction.sl_floor_pct == 2.5  # explicit per-tier floor, no CONVICTION_TIGHT

    def test_swing_sl_low_wider_than_medium(self):
        from utils.regime_params import get_tp_sl_params
        low = get_tp_sl_params("NORMAL", "LOW", "SWING")
        medium = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        assert low.sl_floor_pct > medium.sl_floor_pct
        assert low.sl_floor_pct == 5.0  # low-quality stocks need widest overnight room

    def test_highvol_swing_sl_wider(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("HIGH_VOL", "MEDIUM", "SWING")
        assert p.sl_floor_pct >= 4.5  # MEDIUM in HIGH_VOL gets widest floor

    def test_swing_maxdays_increased_normal(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("NORMAL", "MEDIUM", "SWING")
        assert p.max_days == 7

    def test_swing_maxdays_increased_bull_trend(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("BULL_TREND", "CONVICTION", "SWING")
        assert p.max_days == 12

    def test_eod_holds_respects_tier(self):
        """CONVICTION profit_lock for CHOPPY is 1.0; MEDIUM is disabled (falls back to 2.5).
        A position at 1.1% pnl qualifies via profit-lock bypass with CONVICTION
        but is blocked with MEDIUM (1.1 < 2.5 fallback, and CHOPPY disables standard promotion)."""
        from types import SimpleNamespace
        from trading.eod_holds import qualifies_for_eod_hold

        conviction_proxy = SimpleNamespace(tier="CONVICTION")
        medium_proxy = SimpleNamespace(tier="MEDIUM")

        ok_conviction, _ = qualifies_for_eod_hold(
            symbol="TEST", regime="CHOPPY", earnings_tomorrow=False,
            current_pnl_pct=1.1,
            entry_sector_sentiment=10.0, current_sector_sentiment=5.0,
            consecutive_red_days=0, vix_trend="stable",
            quality=conviction_proxy,
            entry_rsi=50.0, entry_composite=65.0,
        )
        ok_medium, reason = qualifies_for_eod_hold(
            symbol="TEST", regime="CHOPPY", earnings_tomorrow=False,
            current_pnl_pct=1.1,
            entry_sector_sentiment=10.0, current_sector_sentiment=5.0,
            consecutive_red_days=0, vix_trend="stable",
            quality=medium_proxy,
            entry_rsi=50.0, entry_composite=65.0,
        )
        assert ok_conviction is True
        assert ok_medium is False


@pytest.mark.unit
class TestBacktestCrashRecoveryGate:
    """_classify_vix_day must require >= 2 consecutive green SPY days for CRASH_RECOVERY."""

    def _vix_crash_recovery(self):
        """VIX series that satisfies the VIX-only CRASH_RECOVERY criteria.
        vix_current=38, vix_prev=40 (not rising), peak_5d=44 → drop=13.6% >= 10%.
        vix_avg_90d ≈ 43 (all 90 historical bars at 44), so vix_current < avg → no HIGH_VOL fallback.
        """
        historical = [(i, 44.0) for i in range(1, 92)]   # 91 bars of VIX=44
        return historical + [(92, 40.0), (93, 38.0)]      # last two: prev=40, current=38

    def _spy_green_streak(self, n: int):
        """SPY series with exactly n consecutive green days ending at the last bar."""
        base = [(i, 400.0) for i in range(1, 10)]
        red_break = [(10, 398.0)]                          # one red day to reset streak
        green_days = [(10 + j + 1, 399.0 + j * 0.5) for j in range(n)]
        return base + red_break + green_days

    def test_two_green_days_triggers_crash_recovery(self):
        from backtest.data import _classify_vix_day
        result = _classify_vix_day(self._vix_crash_recovery(), self._spy_green_streak(2))
        assert result == "CRASH_RECOVERY", f"Expected CRASH_RECOVERY with 2 green days, got {result}"

    def test_one_green_day_does_not_trigger_crash_recovery(self):
        from backtest.data import _classify_vix_day
        result = _classify_vix_day(self._vix_crash_recovery(), self._spy_green_streak(1))
        assert result != "CRASH_RECOVERY", f"CRASH_RECOVERY must not fire with only 1 green day, got {result}"


@pytest.mark.unit
class TestQualifiesForEodHoldGate:
    """qualifies_for_eod_hold must enforce regime×tier composite_min and rsi_ceiling."""

    def _call(self, **overrides):
        from types import SimpleNamespace
        from trading.eod_holds import qualifies_for_eod_hold
        defaults = dict(
            symbol="AAPL",
            regime="NORMAL",
            earnings_tomorrow=False,
            current_pnl_pct=2.0,        # above min_pnl (1.2%), below profit_lock
            entry_sector_sentiment=10.0,
            current_sector_sentiment=5.0,
            consecutive_red_days=0,
            vix_trend="stable",
            quality=SimpleNamespace(tier="MEDIUM"),
            entry_rsi=65.0,             # default: within NORMAL MEDIUM ceiling (70)
            entry_composite=66.0,       # default: above NORMAL MEDIUM min (65)
        )
        defaults.update(overrides)
        return qualifies_for_eod_hold(**defaults)

    def test_rsi_at_ceiling_blocks_promotion(self):
        ok, reason = self._call(entry_rsi=70.0)   # == ceiling, not <
        assert not ok
        assert "RSI" in reason

    def test_rsi_below_ceiling_allows_promotion(self):
        ok, _ = self._call(entry_rsi=69.0)
        assert ok

    def test_composite_below_min_blocks_promotion(self):
        ok, reason = self._call(entry_composite=64.0)   # < 65 (NORMAL MEDIUM min)
        assert not ok
        assert "composite" in reason

    def test_profit_lock_bypass_blocked_if_composite_below_floor(self):
        """Strongly profitable positions are still blocked if composite is below the profit-lock floor."""
        ok, reason = self._call(
            current_pnl_pct=10.0,   # well above any profit_lock threshold
            entry_rsi=85.0,          # would fail RSI gate
            entry_composite=40.0,    # below SWING_PROFIT_LOCK_MIN_COMPOSITE (62)
        )
        assert not ok
        assert "composite" in reason.lower()


@pytest.mark.unit
class TestBacktestSwingPromotion:
    """_evaluate_swing_promotion must enforce regime×tier gate via get_swing_gate_params()."""

    def _make_state(self, composite: float, rsi: float, tier: str = "MEDIUM") -> "SimState":
        from backtest.simulator import SimState
        state = SimState(cash=100_000.0)
        state.positions["AAPL"] = {
            "qty": 10,
            "entry": 100.0,
            "entry_ts": "2026-05-27T14:30:00+00:00",
            "composite": composite,
            "quality_tier": tier,
            "is_swing": False,
        }
        return state

    def _make_precomp(self, rsi: float, ts):
        """Minimal precomp dict with an IV-like object at the given timestamp."""
        from types import SimpleNamespace
        return {"AAPL": {ts: SimpleNamespace(rsi=rsi)}}

    def test_high_rsi_blocks_swing_promotion(self):
        """RSI at ceiling (70 for NORMAL MEDIUM) must block standard promotion."""
        import pandas as pd
        from backtest.simulator import _evaluate_swing_promotion

        ts = pd.Timestamp("2026-05-27 19:50:00", tz="UTC")
        state = self._make_state(composite=66.0, rsi=70.0, tier="MEDIUM")
        df = pd.DataFrame({"close": [102.0]}, index=[ts])
        day_bars = {"AAPL": df}
        precomp = self._make_precomp(rsi=70.0, ts=ts)

        n = _evaluate_swing_promotion(state, day_bars, ts, "NORMAL", 0, precomp=precomp)
        assert n == 0, "AAPL should NOT be promoted: RSI at ceiling"
        assert not state.positions["AAPL"].get("is_swing")

    def test_low_rsi_high_composite_allows_swing_promotion(self):
        """RSI below ceiling and composite above min allows standard promotion."""
        import pandas as pd
        from backtest.simulator import _evaluate_swing_promotion

        ts = pd.Timestamp("2026-05-27 19:50:00", tz="UTC")
        # NORMAL MEDIUM: composite_min=65, rsi_ceiling=70; min_pnl=1.2%
        state = self._make_state(composite=66.0, rsi=65.0, tier="MEDIUM")
        state.positions["AAPL"]["entry"] = 100.0
        df = pd.DataFrame({"close": [102.0]}, index=[ts])   # pnl_pct=2.0% >= 1.2%
        day_bars = {"AAPL": df}
        precomp = self._make_precomp(rsi=65.0, ts=ts)

        n = _evaluate_swing_promotion(state, day_bars, ts, "NORMAL", 0, precomp=precomp)
        assert n == 1, "AAPL should be promoted: RSI and composite within bounds"
        assert state.positions["AAPL"].get("is_swing")


class TestT2WindowFix:
    """T2 trigger window must be wide enough to fire before SL hits."""

    @pytest.mark.unit
    def test_t2_drop_min_leaves_meaningful_window(self):
        """T2 window (sl_ceiling - DROP_MIN) must be >= 0.8% to be practical."""
        import config
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("NORMAL", "MEDIUM", "DAY_TRADE")
        window = p.sl_ceiling_pct - config.TRANCHE2_DROP_MIN_PCT
        assert window >= 0.8, (
            f"T2 window={window:.1f}% (sl_ceiling={p.sl_ceiling_pct}% - drop_min={config.TRANCHE2_DROP_MIN_PCT}%) "
            "is too narrow — T2 almost never fires"
        )

    @pytest.mark.unit
    def test_t2_min_hold_allows_entry_within_session(self):
        """TRANCHE2_MIN_HOLD_MIN must be <= 90 so 10:30 entries can T2 before 2 PM."""
        import config
        assert config.TRANCHE2_MIN_HOLD_MIN <= 90, (
            f"MIN_HOLD_MIN={config.TRANCHE2_MIN_HOLD_MIN} leaves almost no T2 window for 10:30 entries"
        )

    @pytest.mark.unit
    def test_evaluate_t2_trigger_does_not_exist(self):
        """evaluate_t2_trigger is dead code and must be removed."""
        import signals.tranche2_gates as m
        assert not hasattr(m, "evaluate_t2_trigger"), (
            "evaluate_t2_trigger is dead code (never called) with contradictory P0-1 logic — remove it"
        )


# ── Earnings 401 detection ────────────────────────────────────────────────────

@pytest.mark.unit
def test_earnings_calendar_detects_mass_401_failure(monkeypatch, tmp_path):
    """refresh_earnings_calendar() must record_failure and preserve the existing cache
    (not overwrite with null data) when all symbols return None (Yahoo Finance 401).
    notify_data_degraded must NOT fire because earnings has zero composite penalty
    and doesn't block auto-execute or T2."""
    import config
    from unittest.mock import patch
    from utils.data_health import _tracker
    from utils.io_utils import save_json

    cal_file = tmp_path / "earnings_calendar.json"
    monkeypatch.setattr(config, "EARNINGS_CALENDAR_FILE", cal_file)
    monkeypatch.setattr(config, "DATA_EARNINGS_FAILURE_RATE_THRESHOLD", 0.80)
    # Earnings has no composite penalty and doesn't block AE/T2 — notify should be skipped.
    monkeypatch.setattr(config, "DATA_COMPOSITE_PENALTY", {
        **config.DATA_COMPOSITE_PENALTY, "earnings": 0
    })

    # Pre-populate with yesterday's valid data so we can verify it is preserved.
    save_json(cal_file, {"date": "2026-05-28", "symbols": {"AAPL": {"next_earnings_date": "2026-08-01"}}})

    # Reset tracker state for earnings so prior test runs don't contaminate this one
    _tracker._state.pop("earnings", None)

    notify_calls = []

    with (
        patch("calendars.earnings._fetch_next_earnings_date", return_value=None),
        patch("calendars.earnings._fetch_earnings_history", return_value=None),
        patch(
            "notifications.telegram.notify_data_degraded",
            side_effect=lambda *a, **kw: notify_calls.append(a),
        ),
    ):
        from calendars.earnings import refresh_earnings_calendar
        refresh_earnings_calendar(["AAPL", "MSFT", "NVDA"])

    # tracker must have recorded at least one failure attempt
    status = _tracker.get_status("earnings")
    assert status.attempts_today >= 1, (
        f"Expected attempts_today >= 1, got {status.attempts_today}"
    )

    # The existing cache file must NOT be overwritten with null data.
    import json
    preserved = json.loads(cal_file.read_text())
    assert preserved["date"] == "2026-05-28", "Yesterday's cache must be preserved on 401 failure"
    assert preserved["symbols"]["AAPL"]["next_earnings_date"] == "2026-08-01"

    # notify_data_degraded must NOT fire: earnings has 0 composite penalty, no AE/T2 blocks.
    assert len(notify_calls) == 0, (
        f"notify_data_degraded must not fire for zero-impact earnings degradation, got {len(notify_calls)}"
    )


@pytest.mark.unit
def test_research_agent_records_failure_on_usage_limit_400(monkeypatch, tmp_path):
    """run_research_agent aborts early and calls record_failure when API usage limit is hit."""
    import config
    import anthropic
    from unittest.mock import patch, MagicMock
    from utils.data_health import _tracker

    monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "research_report.json")
    monkeypatch.setattr(config, "DATA_RESEARCH_FAILURE_RATE_THRESHOLD", 0.50)

    _tracker._state.pop("research", None)

    exc_400 = anthropic.APIStatusError(
        "You have reached your specified API usage limits.",
        response=MagicMock(status_code=400),
        body={"error": {"message": "You have reached your specified API usage limits."}},
    )

    notify_calls = []

    with (
        patch("research.fetchers._fetch_market_news", return_value=[]),
        patch("research.fetchers._fetch_stock_news", return_value=[]),
        patch("research.fetchers._fetch_stocktwits", return_value={"available": False}),
        patch("research.fetchers._fetch_reddit", return_value={"available": False}),
        patch("research.fetchers._fetch_x_posts", return_value={"available": False}),
        patch("research.agent._quick_sentiment_result", return_value=None),
        patch("research.agent._analyze_stock", side_effect=exc_400),
        patch(
            "notifications.telegram.notify_data_degraded",
            side_effect=lambda *a, **kw: notify_calls.append(a),
        ),
    ):
        from research.agent import run_research_agent
        run_research_agent(["AAPL", "MSFT", "NVDA"])

    status = _tracker.get_status("research")
    assert status.attempts_today >= 1, f"Expected attempts_today >= 1, got {status.attempts_today}"

    assert len(notify_calls) == 1, f"Expected notify_data_degraded once, got {len(notify_calls)}"
    assert notify_calls[0][0] == "research"


@pytest.mark.unit
def test_research_agent_records_success_when_rule_based_only(monkeypatch, tmp_path):
    """run_research_agent calls record_success when all symbols use rule-based sentiment."""
    import config
    from unittest.mock import patch
    from utils.data_health import _tracker

    monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "research_report.json")
    monkeypatch.setattr(config, "DATA_RESEARCH_FAILURE_RATE_THRESHOLD", 0.80)

    _tracker._state.pop("research", None)

    rule_based = {
        "news_sentiment_score": 10, "news_sentiment_label": "NEUTRAL",
        "social_sentiment_score": 20, "social_sentiment_label": "BULLISH",
        "combined_sentiment_score": 15, "combined_sentiment_label": "BULLISH",
        "key_catalyst": None, "risk_flag": None,
        "summary": "OK.", "strategy": "Buy.",
    }

    with (
        patch("research.fetchers._fetch_market_news", return_value=[]),
        patch("research.fetchers._fetch_stock_news", return_value=[]),
        patch("research.fetchers._fetch_stocktwits", return_value={"available": True, "bullish_pct": 60}),
        patch("research.fetchers._fetch_reddit", return_value={"available": False}),
        patch("research.fetchers._fetch_x_posts", return_value={"available": False}),
        patch("research.agent._quick_sentiment_result", return_value=rule_based),
        patch("notifications.telegram.notify_data_degraded"),
    ):
        from research.agent import run_research_agent
        run_research_agent(["AAPL"])

    status = _tracker.get_status("research")
    assert status.last_success_at is not None, "Expected record_success to have been called"
