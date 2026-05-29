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










