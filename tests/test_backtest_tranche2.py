"""
Backtest tranche-2 and data-resilience unit tests.
Run with: pytest tests/test_backtest_tranche2.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import pytest

from utils.constants import ET


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


# ---------------------------------------------------------------------------
# volatility_screener — market cap filter bypass + zero-cap edge cases
# ---------------------------------------------------------------------------

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
