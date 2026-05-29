"""Tests for budget_guard, entry_exhausted_flag, circuit breaker TTL, and auto-execute."""
import json
import time
from datetime import datetime
from unittest.mock import patch, MagicMock
import pytest

from utils.constants import ET


# ─── budget_guard ────────────────────────────────────────────────────────────

class TestBudgetGuard:

    def _now_et(self, hour, minute=0):
        return datetime(2026, 5, 13, hour, minute, tzinfo=ET)

    @pytest.mark.unit
    def test_rollover_applied_at_1230(self):
        from trading.budget_guard import apply_exit_rollover
        b = {
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.0,
        }
        result = apply_exit_rollover(b, self._now_et(12, 30))
        assert result is True
        assert b["entry_ceiling_usd"] == pytest.approx(0.85, abs=0.01)
        assert b["_rollover_applied"] is True

    @pytest.mark.unit
    def test_rollover_not_applied_twice(self):
        from trading.budget_guard import apply_exit_rollover
        b = {
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.0,
            "_rollover_applied": True,
        }
        result = apply_exit_rollover(b, self._now_et(12, 30))
        assert result is False
        assert b["entry_ceiling_usd"] == 0.70

    @pytest.mark.unit
    def test_rollover_not_applied_wrong_time(self):
        from trading.budget_guard import apply_exit_rollover
        b = {"entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30, "exit_spent_usd": 0.0}
        result = apply_exit_rollover(b, self._now_et(11, 0))
        assert result is False

    @pytest.mark.unit
    def test_rollover_skipped_if_exit_already_spent(self):
        from trading.budget_guard import apply_exit_rollover
        b = {
            "entry_ceiling_usd": 0.70,
            "exit_ceiling_usd": 0.30,
            "exit_spent_usd": 0.30,
        }
        result = apply_exit_rollover(b, self._now_et(12, 30))
        assert result is False


# ─── entry_exhausted_flag day-reset (Task 2: Fix 1) ──────────────────────────

class TestEntryExhaustedFlagReset:

    def setup_method(self):
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = False

    def teardown_method(self):
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = False

    @pytest.mark.unit
    def test_flag_syncs_to_false_on_fresh_day_budget(self, tmp_path):
        """Stale True flag is cleared when disk budget shows entry_exhausted=False."""
        from unittest.mock import patch
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True  # stale from yesterday

        today = datetime.now(ET).strftime("%Y-%m-%d")
        budget = {
            "date": today, "calls_made": 0, "entry_calls": 0, "exit_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "estimated_cost_usd": 0.0,
            "entry_spent_usd": 0.0, "exit_spent_usd": 0.0,
            "ceiling_usd": 1.0,
            "entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30,
            "ceiling_mode": "normal", "entry_exhausted": False,
            "exit_exhausted": False, "budget_exhausted": False, "escalated": False,
        }
        budget_file = tmp_path / "api_budget.json"
        budget_file.write_text(json.dumps(budget))

        with patch("trading.risk_manager.config") as mc:
            mc.API_BUDGET_FILE = budget_file
            mc.ENTRY_BUDGET_RATIO = 0.7
            mc.EXIT_BUDGET_RATIO  = 0.3
            mc.API_BUDGET_WARNING_PCT = 0.8
            mc.ENTRY_BUDGET_WARNING_PCT = 0.75
            result = risk_manager.check_entry_budget(0.001, "NORMAL")

        assert result is True
        assert risk_manager._entry_exhausted_flag is False

    @pytest.mark.unit
    def test_flag_stays_true_when_disk_entry_exhausted(self, tmp_path):
        """Flag is not cleared when disk budget confirms entry_exhausted=True."""
        from unittest.mock import patch
        from trading import risk_manager
        risk_manager._entry_exhausted_flag = True

        today = datetime.now(ET).strftime("%Y-%m-%d")
        budget = {
            "date": today, "calls_made": 5, "entry_calls": 5, "exit_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "estimated_cost_usd": 0.70,
            "entry_spent_usd": 0.70, "exit_spent_usd": 0.0,
            "ceiling_usd": 1.0,
            "entry_ceiling_usd": 0.70, "exit_ceiling_usd": 0.30,
            "ceiling_mode": "normal", "entry_exhausted": True,
            "exit_exhausted": False, "budget_exhausted": False, "escalated": False,
        }
        budget_file = tmp_path / "api_budget.json"
        budget_file.write_text(json.dumps(budget))

        with patch("trading.risk_manager.config") as mc:
            mc.API_BUDGET_FILE = budget_file
            mc.ENTRY_BUDGET_RATIO = 0.7
            mc.EXIT_BUDGET_RATIO  = 0.3
            result = risk_manager.check_entry_budget(0.001, "NORMAL")

        assert result is False
        assert risk_manager._entry_exhausted_flag is True


# ─── Circuit breaker TTL + exponential backoff (Task 3: Fix 2) ───────────────

class TestCircuitBreakerTTL:

    def setup_method(self):
        from trading import claude_caller
        claude_caller._CLAUDE_DISABLED.clear()
        claude_caller._CLAUDE_DISABLED_UNTIL = 0.0
        claude_caller._CLAUDE_DISABLED_REASON = ""

    def teardown_method(self):
        from trading import claude_caller
        claude_caller._CLAUDE_DISABLED.clear()
        claude_caller._CLAUDE_DISABLED_UNTIL = 0.0
        claude_caller._CLAUDE_DISABLED_REASON = ""

    def _make_snap(self):
        """Minimal CycleSnapshot-like mock."""
        snap = MagicMock()
        snap.regime = "NORMAL"
        snap.portfolio_gain_pct = 0.0
        snap.research = {}
        snap.research_meta = {}
        snap.regime_info = {}
        snap.fred_cache = {}
        snap.weights = {}
        snap.strategy_memory = ""
        snap.params = {}
        return snap

    def _make_exc(self, status_code, message="error"):
        """Build a minimal APIStatusError without calling its real __init__."""
        import anthropic
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = status_code
        exc.message = message
        return exc

    @pytest.mark.unit
    def test_429_sets_5min_ttl(self):
        """HTTP 429 trips circuit breaker with ~300s TTL."""
        from trading import claude_caller
        from trading.pre_claude_filter import FilterResult
        snap = self._make_snap()

        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.compute_atr_tp_pct", return_value=5.0), \
             patch("trading.claude_caller.telegram_notify"):
            mock_ai = MagicMock()
            mock_ai.messages.create.side_effect = self._make_exc(429, "rate limit exceeded")
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None, FilterResult("CLAUDE", ""))], snap, mock_ai)

        assert claude_caller._CLAUDE_DISABLED.is_set()
        ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        assert 290 <= ttl <= 310, f"Expected ~300s TTL, got {ttl:.1f}s"

    @pytest.mark.unit
    def test_402_sets_30min_ttl_and_telegrams(self):
        """HTTP 402 trips circuit breaker with ~1800s TTL and sends Telegram."""
        from trading import claude_caller
        from trading.pre_claude_filter import FilterResult
        snap = self._make_snap()

        t_before = time.monotonic()
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.compute_atr_tp_pct", return_value=5.0), \
             patch("trading.claude_caller.telegram_notify") as mock_tg:
            mock_ai = MagicMock()
            mock_ai.messages.create.side_effect = self._make_exc(402, "billing error")
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None, FilterResult("CLAUDE", ""))], snap, mock_ai)

        assert claude_caller._CLAUDE_DISABLED.is_set()
        ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        assert 1790 <= ttl <= 1810, f"Expected ~1800s TTL, got {ttl:.1f}s"
        mock_tg.notify_claude_disabled.assert_called_once()

    @pytest.mark.unit
    def test_circuit_breaker_clears_after_ttl_expires(self):
        """When TTL is expired, _ask_claude_batch clears the event and allows the call."""
        from trading import claude_caller

        # Set circuit breaker with already-expired TTL
        claude_caller._CLAUDE_DISABLED.set()
        claude_caller._CLAUDE_DISABLED_UNTIL = time.monotonic() - 1.0
        claude_caller._CLAUDE_DISABLED_REASON = "stale reason"

        mock_ai = MagicMock()
        mock_ai.messages.create.return_value = MagicMock(
            content=[MagicMock(text='{"TEST": {"action": "HOLD", "confidence": 0, '
                                    '"reasoning": "ok", "stop_loss_pct": 4.0, '
                                    '"take_profit_pct": 5.0, "indicator_signals": {}}}')],
            usage=MagicMock(input_tokens=100, output_tokens=50,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

        snap = self._make_snap()
        from trading.pre_claude_filter import FilterResult
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.risk_manager.record_usage"), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.compute_atr_tp_pct", return_value=5.0):
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None, FilterResult("CLAUDE", ""))], snap, mock_ai)

        assert not claude_caller._CLAUDE_DISABLED.is_set()
        assert claude_caller._CLAUDE_DISABLED_REASON == ""  # reason cleared on TTL expiry
        mock_ai.messages.create.assert_called_once()

    @pytest.mark.unit
    def test_backoff_doubles_remaining_on_probe_failure(self):
        """Re-trip while prior TTL has remaining time doubles the effective TTL."""
        from trading import claude_caller

        # Simulate a situation where the circuit was cleared for a probe but
        # 200s of TTL still remain in _CLAUDE_DISABLED_UNTIL from the previous trip.
        claude_caller._CLAUDE_DISABLED_UNTIL = time.monotonic() + 200.0
        # Event is NOT set (probe in progress)

        snap = self._make_snap()
        t_before = time.monotonic()
        from trading.pre_claude_filter import FilterResult
        with patch("trading.claude_caller.risk_manager.check_entry_budget", return_value=True), \
             patch("trading.claude_caller.build_stock_block", return_value="block"), \
             patch("trading.claude_caller._get_cached_stable_context", return_value="ctx"), \
             patch("trading.claude_caller.build_dynamic_user_context", return_value="dyn"), \
             patch("trading.claude_caller.compute_atr_tp_pct", return_value=5.0), \
             patch("trading.claude_caller.telegram_notify"):
            mock_ai = MagicMock()
            mock_ai.messages.create.side_effect = self._make_exc(429, "rate limit")
            claude_caller._ask_claude_batch([("TEST", MagicMock(), None, FilterResult("CLAUDE", ""))], snap, mock_ai)

        new_ttl = claude_caller._CLAUDE_DISABLED_UNTIL - t_before
        # remaining was ~200s; doubled = 400s > base 300s → new_ttl should be ~400s
        assert new_ttl >= 390, f"Expected TTL >= 390s (double of ~200s remaining), got {new_ttl:.1f}s"
        assert new_ttl <= 1800, "TTL should not exceed 30 min cap"


# ─── auto-execute outside budget gate (Task 5: Fix 5) ───────────────────────

class TestAutoExecuteWhenBudgetExhausted:

    def _make_iv(self, symbol, composite, rsi=55.0, stoch_d=50.0, current_price=100.0):
        from tests.test_signals import make_full_iv
        return make_full_iv(
            symbol=symbol, composite_score=composite, rsi=rsi,
            stoch_d=stoch_d, current_price=current_price,
        )

    def _snap(self, research=None, intraday_news=None):
        from trading.cycle_context import CycleSnapshot
        return CycleSnapshot(
            regime_info={"regime": "NORMAL", "macro_health_score": 0, "spy_5day_return": 0.0},
            regime="NORMAL",
            fred_cache={},
            research=research or {"AUTO": {"sentiment_score": 25}},
            research_meta={"macro_shock_score": 0},
            strategy_memory="",
            weights={},
            params={
                "rsi_buy_max": 72, "rsi_buy_max_high_vol": 65,
                "high_vol_position_mult": 0.5,
                "entry_start_hour": 10, "entry_end_hour": 15,
            },
            portfolio_gain_pct=0.0,
            intraday_news=intraday_news or {},
        )

    @pytest.mark.unit
    def test_auto_execute_passes_actual_symbol_ret5d(self):
        """Auto-execute must thread the real 5d return into execute_buy, not hardcode 0.0."""
        from trading.pipeline import TradingPipeline
        from trading import risk_manager

        captured = {}

        def fake_execute_buy(**kwargs):
            captured["symbol_ret5d"] = kwargs.get("symbol_ret5d")
            return None

        composite = 76.0
        iv = self._make_iv("AUTO", composite=composite)
        snap = self._snap(
            research={"AUTO": {"sentiment_score": 25}},
            intraday_news={"AUTO": {"shock_score": 0, "sentiment_score": 25}},
        )
        # _fetch_symbol_data returns (symbol, iv, existing_pos, symbol_ret5d)
        with patch("trading.pipeline._fetch_symbol_data", return_value=("AUTO", iv, None, -3.5)), \
             patch("trading.pipeline.execute_buy", side_effect=fake_execute_buy) as mock_buy, \
             patch("trading.pipeline.load_open_positions", return_value={}), \
             patch("trading.pipeline.load_daily_closed", return_value=set()), \
             patch("trading.pipeline._ask_claude_batch", return_value={}), \
             patch("trading.pipeline._ask_claude_exit_batch", return_value={}):
            TradingPipeline().run(
                symbols=["AUTO"], snap=snap,
                ai=MagicMock(), now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
                alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                can_open=True,
            )
        mock_buy.assert_called_once()
        assert captured.get("symbol_ret5d") == -3.5, (
            f"Expected symbol_ret5d=-3.5, got {captured.get('symbol_ret5d')}"
        )

    @pytest.mark.unit
    def test_auto_execute_runs_when_budget_exhausted(self):
        """AUTO_EXECUTE candidates are collected and execute_buy is called even when entry budget exhausted."""
        from trading.pipeline import TradingPipeline
        from trading import risk_manager

        # composite=76, sentiment=25 → NORMAL+MEDIUM tier, AE threshold=75 (55+20), sentiment_min=20 → AUTO_EXECUTE
        composite = 76.0
        sentiment = 25
        iv = self._make_iv("AUTO", composite=composite)
        snap = self._snap(
            research={"AUTO": {"sentiment_score": sentiment}},
            intraday_news={"AUTO": {"shock_score": 0, "sentiment_score": sentiment}},
        )

        risk_manager._entry_exhausted_flag = True
        try:
            with patch("trading.pipeline._fetch_symbol_data", return_value=("AUTO", iv, None, 0.0)), \
                 patch("trading.pipeline._ask_claude_batch") as mock_claude, \
                 patch("trading.pipeline.execute_buy", return_value=None) as mock_buy, \
                 patch("trading.pipeline.load_open_positions", return_value={}), \
                 patch("trading.pipeline.load_daily_closed", return_value=set()), \
                 patch("trading.pipeline._ask_claude_exit_batch", return_value={}):
                TradingPipeline().run(
                    symbols=["AUTO"], snap=snap,
                    ai=MagicMock(), now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
                    alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                    can_open=True,
                )
            mock_claude.assert_not_called()
            mock_buy.assert_called_once()
        finally:
            risk_manager._entry_exhausted_flag = False

    @pytest.mark.unit
    def test_claude_candidates_blocked_when_budget_exhausted(self):
        """Symbols that route to Claude are held (not collected) when entry budget exhausted."""
        from trading.pipeline import TradingPipeline
        from trading import risk_manager

        # composite=42, sentiment=5 → passes all gates but not AUTO_EXECUTE threshold
        iv = self._make_iv("HOLD_ME", composite=42.0)
        snap = self._snap(
            research={"HOLD_ME": {"sentiment_score": 5}},
            intraday_news={"HOLD_ME": {"shock_score": 0, "sentiment_score": 5}},
        )

        risk_manager._entry_exhausted_flag = True
        try:
            with patch("trading.pipeline._fetch_symbol_data", return_value=("HOLD_ME", iv, None, 0.0)), \
                 patch("trading.pipeline._ask_claude_batch") as mock_claude, \
                 patch("trading.pipeline.load_open_positions", return_value={}), \
                 patch("trading.pipeline.load_daily_closed", return_value=set()), \
                 patch("trading.pipeline._ask_claude_exit_batch", return_value={}):
                TradingPipeline().run(
                    symbols=["HOLD_ME"], snap=snap,
                    ai=MagicMock(), now=datetime(2026, 5, 13, 10, 30, tzinfo=ET),
                    alpaca=MagicMock(), portfolio_value=10000, buying_power=5000,
                    can_open=True,
                )
            mock_claude.assert_not_called()
        finally:
            risk_manager._entry_exhausted_flag = False


# ── Circuit breaker: 400 "usage limits" ──────────────────────────────────────

def _make_usage_limit_exc(message: str = "You have reached your specified API usage limits.") -> "anthropic.APIStatusError":
    """Build a real anthropic.APIStatusError with a 400 status and the given message."""
    import anthropic
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    return anthropic.APIStatusError(message, response=resp, body=None)


@pytest.mark.unit
def test_usage_limit_400_trips_circuit_breaker_and_notifies(monkeypatch):
    """A 400 'usage limits' error must trip the CB for CLAUDE_CB_LIMIT_TTL and call notify_claude_disabled."""
    import config
    from unittest.mock import MagicMock
    from trading.claude_caller import _ask_claude_batch, _CLAUDE_DISABLED, _CLAUDE_LOCK
    import trading.claude_caller as cc

    # Reset circuit breaker state before the test
    with _CLAUDE_LOCK:
        _CLAUDE_DISABLED.clear()
        cc._CLAUDE_DISABLED_UNTIL = 0.0
        cc._CLAUDE_DISABLED_REASON = ""

    exc = _make_usage_limit_exc(
        "You have reached your specified API usage limits. "
        "You will regain access on 2026-06-01 at 00:00 UTC."
    )

    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.regime_info = {}
    snap.strategy_memory = ""
    snap.research = {}
    snap.research_meta = {}
    snap.fred_cache = {}
    snap.weights = {}
    snap.params = {}
    snap.portfolio_gain_pct = 0.0
    snap.intraday_news = {}

    monkeypatch.setattr("trading.claude_caller.build_stock_block", lambda *a, **kw: "BLOCK")
    monkeypatch.setattr("trading.claude_caller._get_cached_stable_context", lambda *a, **kw: "CTX")
    monkeypatch.setattr("trading.claude_caller.build_dynamic_user_context", lambda now, **kw: "DYN")
    monkeypatch.setattr("trading.claude_caller.risk_manager.check_entry_budget", lambda cost, regime: True)
    monkeypatch.setattr("trading.claude_caller.risk_manager.record_usage", lambda **kw: None)

    notify_calls = []
    monkeypatch.setattr("trading.claude_caller.telegram_notify.notify_claude_disabled",
                        lambda reason: notify_calls.append(reason))

    monkeypatch.setattr(config, "CLAUDE_CB_LIMIT_TTL", 86400.0)

    ai_mock = MagicMock()
    ai_mock.messages.create.side_effect = exc

    from trading.pre_claude_filter import FilterResult
    iv = MagicMock()
    iv.composite_score = 60.0
    iv.rsi = 55.0
    iv.current_price = 100.0
    iv.atr = 1.5

    result = _ask_claude_batch([("AAPL", iv, None, FilterResult("CLAUDE", ""))], snap, ai_mock)

    # Circuit breaker must be set
    assert _CLAUDE_DISABLED.is_set(), "Circuit breaker was not tripped for usage-limit 400"

    # TTL must be approximately CLAUDE_CB_LIMIT_TTL (within 5s of setup time)
    import time
    remaining = cc._CLAUDE_DISABLED_UNTIL - time.monotonic()
    assert remaining > 86400.0 - 5, f"Expected ~86400s remaining, got {remaining:.1f}s"

    # Telegram must have been notified
    assert notify_calls, "notify_claude_disabled was not called for usage-limit 400"
    assert "usage limit" in notify_calls[0].lower()

    # Result must be all HOLDs
    assert result == {"AAPL": cc._default_hold("AAPL")}

    # Cleanup: reset CB so other tests are not affected
    with _CLAUDE_LOCK:
        _CLAUDE_DISABLED.clear()
        cc._CLAUDE_DISABLED_UNTIL = 0.0
        cc._CLAUDE_DISABLED_REASON = ""


@pytest.mark.unit
def test_plain_400_does_not_trip_circuit_breaker(monkeypatch):
    """A plain 400 (malformed request, not usage limits) must NOT trip the CB or send Telegram."""
    import anthropic
    import httpx
    from unittest.mock import MagicMock
    from trading.claude_caller import _ask_claude_batch, _CLAUDE_DISABLED, _CLAUDE_LOCK
    import trading.claude_caller as cc

    # Reset circuit breaker state before the test
    with _CLAUDE_LOCK:
        _CLAUDE_DISABLED.clear()
        cc._CLAUDE_DISABLED_UNTIL = 0.0
        cc._CLAUDE_DISABLED_REASON = ""

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    plain_400 = anthropic.APIStatusError("Invalid request: bad JSON body", response=resp, body=None)

    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.regime_info = {}
    snap.strategy_memory = ""
    snap.research = {}
    snap.research_meta = {}
    snap.fred_cache = {}
    snap.weights = {}
    snap.params = {}
    snap.portfolio_gain_pct = 0.0
    snap.intraday_news = {}

    monkeypatch.setattr("trading.claude_caller.build_stock_block", lambda *a, **kw: "BLOCK")
    monkeypatch.setattr("trading.claude_caller._get_cached_stable_context", lambda *a, **kw: "CTX")
    monkeypatch.setattr("trading.claude_caller.build_dynamic_user_context", lambda now, **kw: "DYN")
    monkeypatch.setattr("trading.claude_caller.risk_manager.check_entry_budget", lambda cost, regime: True)
    monkeypatch.setattr("trading.claude_caller.risk_manager.record_usage", lambda **kw: None)

    notify_calls = []
    monkeypatch.setattr("trading.claude_caller.telegram_notify.notify_claude_disabled",
                        lambda reason: notify_calls.append(reason))

    ai_mock = MagicMock()
    ai_mock.messages.create.side_effect = plain_400

    from trading.pre_claude_filter import FilterResult
    iv = MagicMock()
    iv.composite_score = 60.0
    iv.rsi = 55.0
    iv.current_price = 100.0
    iv.atr = 1.5

    result = _ask_claude_batch([("AAPL", iv, None, FilterResult("CLAUDE", ""))], snap, ai_mock)

    # Circuit breaker must NOT be set for a plain 400
    assert not _CLAUDE_DISABLED.is_set(), "Circuit breaker was incorrectly tripped for plain 400"

    # Telegram must NOT be notified
    assert not notify_calls, f"notify_claude_disabled was unexpectedly called: {notify_calls}"

    # Result must still be HOLD (graceful degradation)
    assert result == {"AAPL": cc._default_hold("AAPL")}


@pytest.mark.unit
def test_usage_limit_400_exit_batch_trips_circuit_breaker(monkeypatch):
    """A 400 'usage limits' error in _ask_claude_exit_batch must trip the CB and notify Telegram."""
    import config
    from unittest.mock import MagicMock
    from trading.claude_caller import _ask_claude_exit_batch, _CLAUDE_DISABLED, _CLAUDE_LOCK
    import trading.claude_caller as cc

    # Reset circuit breaker state before the test
    with _CLAUDE_LOCK:
        _CLAUDE_DISABLED.clear()
        cc._CLAUDE_DISABLED_UNTIL = 0.0
        cc._CLAUDE_DISABLED_REASON = ""

    exc = _make_usage_limit_exc(
        "You have reached your specified API usage limits. "
        "You will regain access on 2026-06-01 at 00:00 UTC."
    )

    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.regime_info = {"vix_current": 18.0}
    snap.strategy_memory = ""
    snap.research = {}
    snap.research_meta = {}
    snap.fred_cache = {}
    snap.weights = {}
    snap.params = {}
    snap.portfolio_gain_pct = 0.0
    snap.intraday_news = {}

    monkeypatch.setattr("trading.claude_caller.build_exit_stock_block", lambda *a, **kw: "EXIT_BLOCK")
    monkeypatch.setattr("trading.claude_caller.risk_manager.check_exit_budget", lambda cost, count, regime: True)
    monkeypatch.setattr("trading.claude_caller.risk_manager.record_usage", lambda **kw: None)

    notify_calls = []
    monkeypatch.setattr(
        "trading.claude_caller.telegram_notify.notify_claude_disabled",
        lambda reason: notify_calls.append(reason),
    )

    monkeypatch.setattr(config, "CLAUDE_CB_LIMIT_TTL", 86400.0)

    ai_mock = MagicMock()
    ai_mock.messages.create.side_effect = exc

    iv = MagicMock()
    iv.composite_score = 55.0
    iv.current_price = 100.0
    pos = {"entry_price": 98.0, "stop_loss_price": 94.0, "take_profit_price": 103.0}

    result = _ask_claude_exit_batch([("AAPL", iv, pos)], snap, ai_mock)

    # Circuit breaker must be set
    assert _CLAUDE_DISABLED.is_set(), "Circuit breaker was not tripped for usage-limit 400 in exit batch"

    # TTL must be approximately CLAUDE_CB_LIMIT_TTL
    import time
    remaining = cc._CLAUDE_DISABLED_UNTIL - time.monotonic()
    assert remaining > 86400.0 - 5, f"Expected ~86400s remaining, got {remaining:.1f}s"

    # Telegram must have been notified
    assert notify_calls, "notify_claude_disabled was not called for usage-limit 400 in exit batch"
    assert "usage limit" in notify_calls[0].lower()

    # Result must be HOLD
    assert result == {"AAPL": cc._default_hold("AAPL")}

    # Cleanup
    with _CLAUDE_LOCK:
        _CLAUDE_DISABLED.clear()
        cc._CLAUDE_DISABLED_UNTIL = 0.0
        cc._CLAUDE_DISABLED_REASON = ""


@pytest.mark.unit
class TestAEConfidenceRegimeAnchor:
    def test_composite_bonus_uses_regime_min_not_global(self, monkeypatch):
        """AE confidence bonus must be positive when composite is above regime composite_min."""
        import config
        from trading.pipeline import _compute_auto_execute_confidence
        from utils.regime_params import get_entry_params
        regime = "BULL_TREND"
        regime_min = get_entry_params(regime).composite_min
        # Score 5 above regime_min should give a positive bonus
        score = regime_min + 5
        conf = _compute_auto_execute_confidence(score, 10, regime=regime)
        base = config.AUTO_EXECUTE_CONFIDENCE_BASE
        assert conf > base, f"confidence {conf} should exceed base {base} when composite > regime_min"

    def test_composite_at_regime_min_gives_zero_bonus(self):
        """composite_score exactly at regime_min → composite_bonus = 0."""
        from trading.pipeline import _compute_auto_execute_confidence
        from utils.regime_params import get_entry_params
        regime = "NORMAL"
        regime_min = get_entry_params(regime).composite_min
        conf = _compute_auto_execute_confidence(float(regime_min), 0, regime=regime)
        import config
        assert conf == config.AUTO_EXECUTE_CONFIDENCE_BASE, \
            "composite exactly at regime_min should give zero bonus"
