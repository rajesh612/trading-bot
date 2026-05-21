"""
Two-tranche scaled entry unit tests.
Run with: pytest tests/test_tranche2.py -v -m unit
No network calls — all external dependencies are mocked.
"""
from __future__ import annotations

import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config

ET = ZoneInfo("America/New_York")


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _t2_iv(**overrides):
    """IndicatorValues-compatible namespace that passes all T2 gates by default."""
    base = dict(
        current_price=95.0,
        rsi=40.0,
        composite_score=55.0,
        atr=1.5,
        bot_herding_score=0,
        squeeze_setup=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _t2_pos(**overrides):
    """Open position dict that passes all T2 structural gates by default."""
    from datetime import timedelta
    entry_time = (datetime.now(ET) - timedelta(minutes=65)).isoformat()
    base = dict(
        tranche_2_triggered=False,
        tranche_1_entry=100.0,
        tranche_1_qty=60,
        qty=60,
        entry_price=100.0,
        entry_time=entry_time,
        stop_loss_pct=4.0,
        take_profit_pct=5.0,
        order_id="bracket-123",
        mode_at_entry="DAY_TRADE",
        regime_at_entry="NORMAL",
        atr_pct=1.5,
    )
    base.update(overrides)
    return base


def _t2_now(hour=11, minute=0):
    return datetime(2026, 5, 21, hour, minute, 0, tzinfo=ET)


# ── Task 2: place_stop_order ──────────────────────────────────────────────────

@pytest.mark.unit
class TestPlaceStopOrder:
    def test_place_stop_order_calls_submit_and_returns_id(self, monkeypatch):
        """place_stop_order submits a STOP order and returns a dict with stop_order_id."""
        import types as _t
        from broker.alpaca_client import AlpacaClient

        fake_order = _t.SimpleNamespace(id="stop-abc-123")
        fake_trading = _t.SimpleNamespace(submit_order=lambda req: fake_order)

        monkeypatch.setattr(AlpacaClient, "__init__", lambda self: None)
        client = AlpacaClient.__new__(AlpacaClient)
        client._trading = fake_trading

        result = client.place_stop_order("AAPL", 100, 87.50)

        assert result["stop_order_id"] == "stop-abc-123"
        assert result["symbol"] == "AAPL"
        assert result["stop_price"] == 87.50

    def test_place_stop_order_propagates_exception(self, monkeypatch):
        """place_stop_order lets Alpaca SDK exceptions propagate (caller handles them)."""
        import types as _t
        from broker.alpaca_client import AlpacaClient

        def _boom(req): raise RuntimeError("API error")
        fake_trading = _t.SimpleNamespace(submit_order=_boom)

        monkeypatch.setattr(AlpacaClient, "__init__", lambda self: None)
        client = AlpacaClient.__new__(AlpacaClient)
        client._trading = fake_trading

        with pytest.raises(RuntimeError, match="API error"):
            client.place_stop_order("AAPL", 100, 87.50)


# ── Task 3: check_tranche2_gates ─────────────────────────────────────────────

@pytest.mark.unit
class TestCheckTranche2Gates:
    """One test per gate — pass path and fail path for each."""

    def _call(self, iv=None, pos=None, now=None, regime="NORMAL",
              sentiment=30.0, sector_chg=0.0, major_event=False, monkeypatch=None):
        from signals.tranche2_gates import check_tranche2_gates
        import calendars.earnings as ec
        if monkeypatch:
            monkeypatch.setattr(ec, "is_earnings_imminent", lambda sym, days=1: False)
        return check_tranche2_gates(
            symbol="AAPL",
            iv=iv or _t2_iv(current_price=95.0),
            pos=pos or _t2_pos(),
            now=now or _t2_now(),
            regime=regime,
            sentiment_score=sentiment,
            sector_etf_chg=sector_chg,
            major_event_today=major_event,
        )

    def test_all_gates_pass(self, monkeypatch):
        result = self._call(monkeypatch=monkeypatch)
        assert result.passed is True
        assert result.reason == ""

    # Gate 1: already triggered
    def test_gate1_already_triggered(self, monkeypatch):
        pos = _t2_pos(tranche_2_triggered=True)
        result = self._call(pos=pos, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "triggered" in result.reason

    # Gate 2: time cutoff
    def test_gate2_cutoff_blocked_at_2pm(self, monkeypatch):
        result = self._call(now=_t2_now(hour=14, minute=0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "cutoff" in result.reason

    def test_gate2_passes_before_2pm(self, monkeypatch):
        result = self._call(now=_t2_now(hour=13, minute=59), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 3: regime
    def test_gate3_crisis_blocked(self, monkeypatch):
        result = self._call(regime="CRISIS", monkeypatch=monkeypatch)
        assert result.passed is False
        assert "regime" in result.reason

    def test_gate3_choppy_blocked(self, monkeypatch):
        result = self._call(regime="CHOPPY", monkeypatch=monkeypatch)
        assert result.passed is False

    def test_gate3_normal_passes(self, monkeypatch):
        result = self._call(regime="NORMAL", monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 4: major event
    def test_gate4_major_event_blocked(self, monkeypatch):
        result = self._call(major_event=True, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "event" in result.reason

    # Gate 5: min hold time
    def test_gate5_hold_time_blocked_when_too_fresh(self, monkeypatch):
        from datetime import timedelta
        ref = _t2_now()
        fresh_entry = (ref - timedelta(minutes=30)).isoformat()
        pos = _t2_pos(entry_time=fresh_entry)
        result = self._call(pos=pos, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "hold" in result.reason

    def test_gate5_hold_time_passes_at_60min(self, monkeypatch):
        from datetime import timedelta
        ref = _t2_now()
        old_entry = (ref - timedelta(minutes=61)).isoformat()
        pos = _t2_pos(entry_time=old_entry)
        result = self._call(pos=pos, monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 6: price drop window
    def test_gate6_drop_too_small_blocked(self, monkeypatch):
        # price at 97 → only 3% below T1 entry of 100 → below DROP_MIN (4%)
        result = self._call(iv=_t2_iv(current_price=97.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "drop" in result.reason

    def test_gate6_drop_too_large_blocked(self, monkeypatch):
        # price at 92 → 8% below T1 entry of 100 → above DROP_MAX (7%)
        result = self._call(iv=_t2_iv(current_price=92.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "drop" in result.reason

    def test_gate6_drop_in_window_passes(self, monkeypatch):
        # price at 95 → 5% below T1 entry of 100 → within 4–7%
        result = self._call(iv=_t2_iv(current_price=95.0), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 7: RSI
    def test_gate7_high_rsi_blocked(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=95.0, rsi=50.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "rsi" in result.reason.lower()

    def test_gate7_rsi_at_max_passes(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=95.0, rsi=45.0), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 8: composite
    def test_gate8_low_composite_blocked(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=95.0, rsi=40.0, composite_score=35.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "composite" in result.reason

    def test_gate8_composite_at_min_passes(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=95.0, rsi=40.0, composite_score=40.0), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 9: earnings imminent
    def test_gate9_earnings_imminent_blocked(self, monkeypatch):
        import calendars.earnings as ec
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda sym, days=1: True)
        from signals.tranche2_gates import check_tranche2_gates
        result = check_tranche2_gates(
            symbol="AAPL", iv=_t2_iv(current_price=95.0),
            pos=_t2_pos(), now=_t2_now(), regime="NORMAL",
            sentiment_score=30.0, sector_etf_chg=0.0, major_event_today=False,
        )
        assert result.passed is False
        assert "earnings" in result.reason

    # Gate 10: sentiment floor
    def test_gate10_low_sentiment_blocked(self, monkeypatch):
        result = self._call(sentiment=15.0, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "sentiment" in result.reason

    def test_gate10_sentiment_at_min_passes(self, monkeypatch):
        result = self._call(sentiment=20.0, monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 11: sector health
    def test_gate11_sector_down_blocked(self, monkeypatch):
        result = self._call(sector_chg=-1.1, monkeypatch=monkeypatch)
        assert result.passed is False
        assert "sector" in result.reason

    def test_gate11_sector_at_threshold_passes(self, monkeypatch):
        result = self._call(sector_chg=-1.0, monkeypatch=monkeypatch)
        assert result.passed is True
