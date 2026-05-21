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
