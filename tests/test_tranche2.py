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
        current_price=97.5,
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
            iv=iv or _t2_iv(current_price=97.5),
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
        # price at 99 → only 1% below T1 entry of 100 → below DROP_MIN (1.5%)
        result = self._call(iv=_t2_iv(current_price=99.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "drop" in result.reason

    def test_gate6_drop_too_large_blocked(self, monkeypatch):
        # price at 96 → 4% below T1 entry of 100 → above DROP_MAX (3.5%)
        result = self._call(iv=_t2_iv(current_price=96.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "drop" in result.reason

    def test_gate6_drop_in_window_passes(self, monkeypatch):
        # price at 97.5 → 2.5% below T1 entry of 100 → within 1.5–3.5%
        result = self._call(iv=_t2_iv(current_price=97.5), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 7: RSI
    def test_gate7_high_rsi_blocked(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=97.5, rsi=65.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "rsi" in result.reason.lower()

    def test_gate7_rsi_at_max_passes(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=97.5, rsi=60.0), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 8: composite
    def test_gate8_low_composite_blocked(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=97.5, rsi=50.0, composite_score=5.0), monkeypatch=monkeypatch)
        assert result.passed is False
        assert "composite" in result.reason

    def test_gate8_composite_at_min_passes(self, monkeypatch):
        result = self._call(iv=_t2_iv(current_price=97.5, rsi=50.0, composite_score=10.0), monkeypatch=monkeypatch)
        assert result.passed is True

    # Gate 9: earnings imminent
    def test_gate9_earnings_imminent_blocked(self, monkeypatch):
        import calendars.earnings as ec
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda sym, days=1: True)
        from signals.tranche2_gates import check_tranche2_gates
        result = check_tranche2_gates(
            symbol="AAPL", iv=_t2_iv(current_price=97.5),
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


# ── Task 4: T1 60% sizing ────────────────────────────────────────────────────

@pytest.mark.unit
class TestT1Sizing:
    """execute_buy() places T1 at 60% of computed size and records tranche fields."""

    def _make_iv(self):
        from indicators.values import IndicatorValues
        return IndicatorValues(
            symbol="AAPL", timestamp="2026-05-21T11:00:00",
            current_price=100.0, vwap=99.0,
            vwap_std1_upper=102.0, vwap_std1_lower=98.0,
            vwap_std2_upper=115.0, vwap_std2_lower=85.0,
            rsi=50.0, macd=0.5, macd_signal=0.3, macd_hist=0.2,
            bb_upper=105.0, bb_mid=100.0, bb_lower=95.0,
            ema9=100.5, ema21=99.5, atr=1.5,
            volume_ratio=1.2, stoch_k=50.0, stoch_d=50.0, obv=10000.0,
            vwap_slope=0.01, rsi_slope=0.0,
            daily_volume_ratio=1.0, daily_volume_trend=0.0, daily_volume_mean=1_000_000.0,
            composite_score=55.0, signal="BUY", confidence=80.0,
            parabolic=False, momentum_exhaustion=False, stop_hunt_zone=False,
            bot_herding_score=0, squeeze_setup=False, prev_close=99.0,
            relative_volume=1.1, or_position="outside",
        )

    def _setup_mocks(self, monkeypatch, tmp_path):
        import trading.executor as exe
        import trading.risk_manager as rm
        import trading.journal as jrn
        import notifications.telegram as tg
        from signals.entry_gates import GateResult

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")
        monkeypatch.setattr(config, "ALPACA_API_KEY", "test")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test")
        monkeypatch.setattr(config, "TRANCHE1_SIZE_PCT", 0.60)
        monkeypatch.setattr(exe, "check_all_gates", lambda *a, **kw: GateResult(passed=True))
        monkeypatch.setattr(exe, "effective_composite_gate", lambda *a, **kw: 30)
        # Patch directly on executor module (functions are imported by name, not accessed via module)
        monkeypatch.setattr(exe, "compute_position_size_for_mode", lambda *a, **kw: 100)
        monkeypatch.setattr(exe, "apply_regime_multiplier", lambda qty, mult: qty)
        monkeypatch.setattr(exe, "apply_sentiment_multiplier", lambda qty, s: qty)
        monkeypatch.setattr(rm, "correct_bracket_after_fill", lambda *a, **kw: None)
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)
        monkeypatch.setattr(jrn, "append_trade", lambda entry: None)

    def _call_execute_buy(self, exe, alpaca, iv):
        return exe.execute_buy(
            symbol="AAPL", iv=iv,
            decision={"action": "BUY", "confidence": 80, "stop_loss_pct": 4.0,
                      "take_profit_pct": 5.0, "reasoning": "test"},
            open_positions={}, research={}, alpaca=alpaca,
            portfolio_value=100_000.0, now=_t2_now(), regime="NORMAL",
            regime_info={"regime": "NORMAL"}, params={},
            pos_mult=1.0, spy_ret5d=0.0, symbol_ret5d=0.0, rsi_max=70.0,
            entries_this_cycle=0,
        )

    def test_execute_buy_sizes_t1_at_60pct(self, monkeypatch, tmp_path):
        """execute_buy returns position_data with qty = 60% of full computed size."""
        import trading.executor as exe
        import types as _t
        self._setup_mocks(monkeypatch, tmp_path)

        placed_qty = {}
        def _fake_bracket(symbol, qty, entry_price, stop_loss_pct, take_profit_pct):
            placed_qty["qty"] = qty
            return {"id": "ord-1", "stop_loss_price": 96.0, "take_profit_price": 105.0}

        fake_alpaca = _t.SimpleNamespace(
            place_bracket_order=_fake_bracket,
            _trading=_t.SimpleNamespace(),
        )

        result = self._call_execute_buy(exe, fake_alpaca, self._make_iv())

        assert result is not None
        assert placed_qty["qty"] == 60  # 60% of 100

    def test_execute_buy_records_tranche_fields(self, monkeypatch, tmp_path):
        """execute_buy writes tranche_2_triggered, tranche_1_qty, tranche_1_entry, stop_order_id."""
        import trading.executor as exe
        import types as _t
        self._setup_mocks(monkeypatch, tmp_path)

        fake_alpaca = _t.SimpleNamespace(
            place_bracket_order=lambda symbol, qty, entry_price, stop_loss_pct, take_profit_pct: {
                "id": "ord-1", "stop_loss_price": 96.0, "take_profit_price": 105.0,
            },
            _trading=_t.SimpleNamespace(),
        )

        result = self._call_execute_buy(exe, fake_alpaca, self._make_iv())

        assert result["tranche_2_triggered"] is False
        assert result["tranche_1_qty"] == 60
        assert result["tranche_1_entry"] == 100.0


# ── Task 5: execute_tranche2_addon ───────────────────────────────────────────

@pytest.mark.unit
class TestExecuteTranche2Addon:
    """Verify blended math, position update, journal entry, stop order placement."""

    def _setup_common(self, monkeypatch, tmp_path):
        import trading.executor as exe
        import trading.journal as jrn
        import notifications.telegram as tg

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")
        monkeypatch.setattr(config, "TRANCHE2_SIZE_PCT", 0.40)
        monkeypatch.setattr(config, "TRANCHE2_SL_PCT", 5.5)
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)
        return exe, jrn, tg

    def _make_alpaca(self, t2_fill=95.0, stop_order_id="stop-789"):
        import types as _t
        calls = {}

        def _cancel(order_id):
            calls["cancelled"] = order_id

        def _market_order(symbol, qty, side):
            calls["market"] = {"symbol": symbol, "qty": qty, "side": side}
            return {"id": "mkt-456", "fill_price": t2_fill}

        def _stop_order(symbol, qty, stop_price):
            calls["stop"] = {"symbol": symbol, "qty": qty, "stop_price": stop_price}
            return {"stop_order_id": stop_order_id}

        def _latest_price(symbol):
            return t2_fill

        fake = _t.SimpleNamespace(
            cancel_order=_cancel,
            place_market_order=_market_order,
            place_stop_order=_stop_order,
            get_latest_price=_latest_price,
        )
        return fake, calls

    def test_blended_entry_math(self, monkeypatch, tmp_path):
        """Blended entry = weighted average of T1 and T2 fill prices."""
        exe, jrn, _ = self._setup_common(monkeypatch, tmp_path)
        monkeypatch.setattr(jrn, "append_trade", lambda e: None)

        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60, entry_price=100.0)
        alpaca, _ = self._make_alpaca(t2_fill=95.0)

        result = exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL",
            research={"AAPL": {"sentiment_score": 30}},
            alpaca=alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        assert result is not None
        t2_qty = result["tranche_2_qty"]
        # blended = (60*100 + t2_qty*95) / (60 + t2_qty)
        expected_entry = (60 * 100.0 + t2_qty * 95.0) / (60 + t2_qty)
        assert abs(result["entry_price"] - expected_entry) < 0.01
        assert result["qty"] == 60 + t2_qty
        assert result["tranche_2_triggered"] is True

    def test_blended_sl_set_below_t2_fill(self, monkeypatch, tmp_path):
        """Stop loss price is 5.5% below T2 fill price."""
        exe, jrn, _ = self._setup_common(monkeypatch, tmp_path)
        monkeypatch.setattr(jrn, "append_trade", lambda e: None)

        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60, entry_price=100.0)
        alpaca, _ = self._make_alpaca(t2_fill=95.0)

        result = exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL",
            research={"AAPL": {"sentiment_score": 30}},
            alpaca=alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        expected_sl = 95.0 * (1 - 5.5 / 100)
        assert abs(result["stop_loss_price"] - expected_sl) < 0.01

    def test_cancel_called_before_market_order(self, monkeypatch, tmp_path):
        """Original bracket is cancelled before T2 market order is placed."""
        exe, jrn, _ = self._setup_common(monkeypatch, tmp_path)
        monkeypatch.setattr(jrn, "append_trade", lambda e: None)

        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60,
                      entry_price=100.0, order_id="bracket-xyz")
        alpaca, calls = self._make_alpaca(t2_fill=95.0)

        exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL", research={},
            alpaca=alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        assert calls.get("cancelled") == "bracket-xyz"
        assert "market" in calls

    def test_journal_entry_written_with_type_tranche2_add(self, monkeypatch, tmp_path):
        """execute_tranche2_addon writes a TRANCHE2_ADD journal entry."""
        exe, jrn, _ = self._setup_common(monkeypatch, tmp_path)

        journal_entries = []
        monkeypatch.setattr(jrn, "append_trade", lambda e: journal_entries.append(e))

        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60, entry_price=100.0)
        alpaca, _ = self._make_alpaca(t2_fill=95.0)

        exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL", research={},
            alpaca=alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        assert len(journal_entries) == 1
        assert journal_entries[0]["type"] == "TRANCHE2_ADD"
        assert journal_entries[0]["symbol"] == "AAPL"

    def test_stop_order_failure_does_not_abort(self, monkeypatch, tmp_path):
        """If place_stop_order raises, T2 continues and stop_order_id is None."""
        import types as _t
        exe, jrn, _ = self._setup_common(monkeypatch, tmp_path)
        monkeypatch.setattr(jrn, "append_trade", lambda e: None)

        fake_alpaca = _t.SimpleNamespace(
            cancel_order=lambda oid: None,
            place_market_order=lambda symbol, qty, side: {"id": "mkt-1", "fill_price": 95.0},
            place_stop_order=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("stop order failed")),
            get_latest_price=lambda s: 95.0,
        )

        pos = _t2_pos(tranche_1_qty=60, tranche_1_entry=100.0, qty=60, entry_price=100.0)
        result = exe.execute_tranche2_addon(
            symbol="AAPL", pos=pos, regime="NORMAL", research={},
            alpaca=fake_alpaca, portfolio_value=100_000.0,
            now=_t2_now(), suppress_notify=True,
        )

        assert result is not None
        assert result["stop_order_id"] is None
        assert result["stop_order_id"] is None


# ── Task 6: _check_tranche2_triggers ─────────────────────────────────────────

@pytest.mark.unit
class TestCheckTranche2Triggers:
    """Monitor triggers T2 when gates pass; idempotency sentinel blocks re-fire."""

    def _write_positions(self, tmp_path, pos_dict):
        from utils.io_utils import save_json
        path = tmp_path / "open_positions.json"
        save_json(path, pos_dict)
        return path

    def test_trigger_fires_when_gates_pass(self, monkeypatch, tmp_path):
        """_check_tranche2_triggers calls execute_tranche2_addon when all gates pass."""
        import trading.monitor as mon
        import signals.tranche2_gates as t2g
        import trading.executor as exe
        import notifications.telegram as tg
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)

        from datetime import timedelta
        old_entry = (datetime.now(ET) - timedelta(minutes=65)).isoformat()
        pos = _t2_pos(entry_time=old_entry, tranche_2_triggered=False)
        open_positions = {"AAPL": pos}

        op_file = self._write_positions(tmp_path, open_positions)
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", op_file)
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")

        monkeypatch.setattr(mon, "_try_compute_iv", lambda sym, alpaca: _t2_iv(current_price=95.0))
        # Patch on the monitor module (imported by name at module level)
        monkeypatch.setattr(mon, "check_tranche2_gates", lambda **kw: t2g.T2GateResult(True))
        monkeypatch.setattr(mon, "get_intraday_bars", lambda sym, lookback=2: None)

        called_with = {}
        def _fake_addon(**kw):
            called_with.update(kw)
            return {**pos, "tranche_2_triggered": True, "qty": 100, "entry_price": 98.0}
        monkeypatch.setattr(exe, "execute_tranche2_addon", _fake_addon)

        import types as _t
        fake_alpaca = _t.SimpleNamespace(
            get_portfolio_value=lambda: 100_000.0,
            get_latest_price=lambda sym: 100.0,
        )

        mon._check_tranche2_triggers(open_positions, fake_alpaca, _t2_now())
        assert called_with.get("symbol") == "AAPL"

    def test_sentinel_prevents_double_trigger(self, monkeypatch, tmp_path):
        """_check_tranche2_triggers skips positions with tranche_2_triggered=True."""
        import trading.monitor as mon
        import trading.executor as exe
        import notifications.telegram as tg
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)

        pos = _t2_pos(tranche_2_triggered=True)
        open_positions = {"AAPL": pos}

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")
        monkeypatch.setattr(mon, "_try_compute_iv", lambda sym, alpaca: _t2_iv(current_price=95.0))
        monkeypatch.setattr(mon, "get_intraday_bars", lambda sym, lookback=2: None)

        addon_called = []
        monkeypatch.setattr(exe, "execute_tranche2_addon", lambda **kw: addon_called.append(kw))

        import types as _t
        fake_alpaca = _t.SimpleNamespace(
            get_portfolio_value=lambda: 100_000.0,
            get_latest_price=lambda sym: 100.0,
        )

        mon._check_tranche2_triggers(open_positions, fake_alpaca, _t2_now())
        assert len(addon_called) == 0

    def test_iv_none_skips_symbol(self, monkeypatch, tmp_path):
        """_check_tranche2_triggers skips symbol when IV computation fails."""
        import trading.monitor as mon
        import trading.executor as exe
        import notifications.telegram as tg
        monkeypatch.setattr(tg, "notify_buy", lambda **kw: None)

        pos = _t2_pos(tranche_2_triggered=False)
        open_positions = {"AAPL": pos}

        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", tmp_path / "op.json")
        monkeypatch.setattr(config, "RESEARCH_REPORT_FILE", tmp_path / "rr.json")
        monkeypatch.setattr(config, "ECONOMIC_CALENDAR_FILE", tmp_path / "ec.json")
        monkeypatch.setattr(mon, "_try_compute_iv", lambda sym, alpaca: None)
        monkeypatch.setattr(mon, "get_intraday_bars", lambda sym, lookback=2: None)

        addon_called = []
        monkeypatch.setattr(exe, "execute_tranche2_addon", lambda **kw: addon_called.append(kw))

        import types as _t
        fake_alpaca = _t.SimpleNamespace(
            get_portfolio_value=lambda: 100_000.0,
            get_latest_price=lambda sym: 100.0,
        )

        mon._check_tranche2_triggers(open_positions, fake_alpaca, _t2_now())
        assert len(addon_called) == 0

    def test_sector_etf_chg_passed_to_gate(self, monkeypatch):
        """Monitor passes real sector ETF % change (not always 0.0) to check_tranche2_gates."""
        import trading.monitor as mon
        import sectors as _sectors
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import types, pandas as pd
        import config

        ET = ZoneInfo("America/New_York")
        now = datetime(2026, 5, 21, 11, 0, tzinfo=ET)
        captured = {}

        def fake_gate(symbol, iv, pos, now, regime, sentiment_score, sector_etf_chg, major_event_today):
            captured["sector_etf_chg"] = sector_etf_chg
            from signals.tranche2_gates import T2GateResult
            return T2GateResult(False, "test skip")

        def fake_iv(sym, alpaca):
            return _t2_iv(current_price=97.5)

        def fake_price(sym):
            return 104.0

        def fake_bars(sym, lookback=2, timeframe_min=5):
            idx = pd.to_datetime(["2026-05-21 09:30:00-04:00", "2026-05-21 09:35:00-04:00"])
            return pd.DataFrame(
                {"open": [100.0, 101.0], "high": [101.0, 102.0],
                 "low": [99.0, 100.0], "close": [101.0, 102.0], "volume": [1000, 1000]},
                index=idx,
            )

        monkeypatch.setattr(mon, "_try_compute_iv", fake_iv)
        monkeypatch.setattr(_sectors, "get_sector_etf", lambda sym: "XLK")
        monkeypatch.setattr(mon, "get_intraday_bars", fake_bars)
        monkeypatch.setattr(mon, "check_tranche2_gates", fake_gate)

        pos = _t2_pos()
        open_positions = {"AAPL": pos}
        fake_alpaca = types.SimpleNamespace(
            get_latest_price=fake_price,
            get_portfolio_value=lambda: 100_000.0,
        )

        mon._check_tranche2_triggers(open_positions, fake_alpaca, now)

        # (104 - 100) / 100 * 100 = 4.0%
        assert "sector_etf_chg" in captured
        assert abs(captured["sector_etf_chg"] - 4.0) < 0.5


# ── Task 7: backtest simulator T2 ────────────────────────────────────────────

@pytest.mark.unit
class TestSimTranche2:
    """_sim_apply_tranche2 updates position dict with blended entry and SL."""

    def test_sim_apply_tranche2_updates_entry_and_sl(self, monkeypatch):
        """After T2, position entry = weighted avg, sl = 5.5% below bar low."""
        monkeypatch.setattr(config, "TRANCHE2_SIZE_PCT", 0.40)
        monkeypatch.setattr(config, "TRANCHE2_SL_PCT", 5.5)
        monkeypatch.setattr(config, "TRANCHE1_SIZE_PCT", 0.60)
        monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.005)
        monkeypatch.setattr(config, "ATR_FALLBACK_PROXY_PCT", 0.02)
        monkeypatch.setattr(config, "MAX_POSITION_PCT", 0.05)

        from backtest.simulator import _sim_apply_tranche2

        # T1: 60 shares @ $100, bar low = $95 (5% drop — in window)
        pos = {
            "entry": 100.0, "qty": 60, "tranche_1_entry": 100.0,
            "tranche_2_triggered": False, "sl": 96.0, "tp": 105.0,
        }
        _sim_apply_tranche2(pos, bar_low=95.0, t1_qty=60, portfolio_value=100_000.0)

        assert pos["tranche_2_triggered"] is True
        t2_qty = pos["tranche_2_qty"]
        # blended = (60*100 + t2_qty*95) / (60+t2_qty)
        expected_entry = (60 * 100.0 + t2_qty * 95.0) / (60 + t2_qty)
        assert abs(pos["entry"] - expected_entry) < 0.01
        # SL = 95 * (1 - 5.5/100) = 89.775
        expected_sl = 95.0 * (1 - 5.5 / 100)
        assert abs(pos["sl"] - expected_sl) < 0.01

    def test_sim_tranche2_gates_pass_returns_true_in_window(self, monkeypatch):
        """_sim_tranche2_gates_pass returns True when drop is in 4–7% window."""
        monkeypatch.setattr(config, "TRANCHE2_DROP_MIN_PCT", 4.0)
        monkeypatch.setattr(config, "TRANCHE2_DROP_MAX_PCT", 7.0)
        monkeypatch.setattr(config, "TRANCHE2_RSI_MAX", 45)
        monkeypatch.setattr(config, "TRANCHE2_COMPOSITE_MIN", 40)
        monkeypatch.setattr(config, "TRANCHE2_CUTOFF_HOUR", 14)
        monkeypatch.setattr(config, "TRANCHE2_MIN_HOLD_MIN", 60)

        from backtest.simulator import _sim_tranche2_gates_pass
        from datetime import timedelta

        pos = {
            "tranche_1_entry": 100.0,
            "entry_time": (datetime.now(ET) - timedelta(minutes=65)).isoformat(),
            "tranche_2_triggered": False,
        }
        iv = _t2_iv(current_price=95.0, rsi=40.0, composite_score=50.0)
        now = _t2_now(hour=11)

        assert _sim_tranche2_gates_pass(pos, iv, drop_pct=5.0, regime="NORMAL", now=now) is True

    def test_sim_tranche2_gates_fail_above_drop_max(self, monkeypatch):
        """_sim_tranche2_gates_pass returns False when drop > DROP_MAX."""
        monkeypatch.setattr(config, "TRANCHE2_DROP_MIN_PCT", 4.0)
        monkeypatch.setattr(config, "TRANCHE2_DROP_MAX_PCT", 7.0)
        monkeypatch.setattr(config, "TRANCHE2_RSI_MAX", 45)
        monkeypatch.setattr(config, "TRANCHE2_COMPOSITE_MIN", 40)
        monkeypatch.setattr(config, "TRANCHE2_CUTOFF_HOUR", 14)
        monkeypatch.setattr(config, "TRANCHE2_MIN_HOLD_MIN", 60)

        from backtest.simulator import _sim_tranche2_gates_pass
        from datetime import timedelta

        pos = {
            "tranche_1_entry": 100.0,
            "entry_time": (datetime.now(ET) - timedelta(minutes=65)).isoformat(),
            "tranche_2_triggered": False,
        }
        iv = _t2_iv(current_price=92.0, rsi=40.0, composite_score=50.0)
        now = _t2_now(hour=11)

        assert _sim_tranche2_gates_pass(pos, iv, drop_pct=8.0, regime="NORMAL", now=now) is False


# ── Fix 1: stop_order_id cancelled on close ───────────────────────────────────

@pytest.mark.unit
class TestStopOrderCancelledOnClose:
    """close_position cancels the T2 stop order before closing the position."""

    def test_close_position_cancels_stop_order_id(self, monkeypatch, tmp_path):
        """If stop_order_id is set in pos, close_position cancels it before closing."""
        import types as _t
        from trading.closer import close_position
        from utils.io_utils import save_json
        import notifications.telegram as tg
        import trading.journal as jrn
        monkeypatch.setattr(tg, "notify_close", lambda **kw: None)
        monkeypatch.setattr(jrn, "append_trade", lambda e: None)
        monkeypatch.setattr(jrn, "record_daily_closed", lambda s: None)
        op_file = tmp_path / "op.json"
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", op_file)
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")

        cancelled = []
        def _cancel(order_id):
            cancelled.append(order_id)

        fake_alpaca = _t.SimpleNamespace(
            close_position=lambda sym: None,
            cancel_order=_cancel,
            get_latest_price=lambda sym: 98.0,
        )

        pos = _t2_pos(stop_order_id="stop-live-789", entry_price=100.0, qty=100)
        open_positions = {"AAPL": pos}
        save_json(op_file, open_positions)

        close_position("AAPL", fake_alpaca, open_positions, 98.0, None, "monitor_stop_loss")

        assert "stop-live-789" in cancelled

    def test_close_position_skips_cancel_when_no_stop_order(self, monkeypatch, tmp_path):
        """If stop_order_id is None, close_position does not call cancel_order."""
        import types as _t
        from trading.closer import close_position
        from utils.io_utils import save_json
        import notifications.telegram as tg
        import trading.journal as jrn
        monkeypatch.setattr(tg, "notify_close", lambda **kw: None)
        monkeypatch.setattr(jrn, "append_trade", lambda e: None)
        monkeypatch.setattr(jrn, "record_daily_closed", lambda s: None)
        op_file = tmp_path / "op.json"
        monkeypatch.setattr(config, "OPEN_POSITIONS_FILE", op_file)
        monkeypatch.setattr(config, "DAILY_CLOSED_FILE", tmp_path / "dc.json")
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "tj.json")

        cancel_called = []
        fake_alpaca = _t.SimpleNamespace(
            close_position=lambda sym: None,
            cancel_order=lambda oid: cancel_called.append(oid),
            get_latest_price=lambda sym: 98.0,
        )

        # pos with no stop_order_id (regular T1-only position)
        pos = _t2_pos(stop_order_id=None, entry_price=100.0, qty=60)
        open_positions = {"AAPL": pos}
        save_json(op_file, open_positions)

        close_position("AAPL", fake_alpaca, open_positions, 98.0, None, "monitor_stop_loss")

        assert len(cancel_called) == 0
