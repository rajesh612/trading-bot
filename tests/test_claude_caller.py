"""
Claude caller unit tests: system prompt structure, cache_control, ATR-TP injection.
Run with: pytest tests/test_claude_caller.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import pytest


# ── P4-T2: system prompt structure ───────────────────────────────────────────

def _make_fake_ai(calls: list, response_text: str):
    from unittest.mock import MagicMock
    def fake_create(**kwargs):
        calls.append(kwargs)
        r = MagicMock()
        block = MagicMock(text=response_text)
        block.type = "text"
        r.content = [block]
        r.usage = MagicMock(input_tokens=100, output_tokens=20,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return r
    ai_mock = MagicMock()
    ai_mock.messages.create.side_effect = fake_create
    return ai_mock


@pytest.mark.unit
def test_exit_batch_system_prompt_has_cache_control(monkeypatch):
    """Exit-batch system blocks must NOT have cache_control.

    10-min trading cycles always exceed Anthropic's 5-min cache TTL, so
    cache_control on exit-batch calls causes write surcharges with zero reads.
    """
    from trading.claude_caller import _ask_claude_exit_batch
    from unittest.mock import MagicMock

    calls = []
    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.regime_info = {"vix_current": 15}
    snap.strategy_memory = ""
    snap.research = {}
    snap.portfolio_gain_pct = 0.0

    iv = MagicMock()
    iv.composite_score = 20.0
    iv.rsi = 55.0
    iv.current_price = 100.0

    monkeypatch.setattr("trading.claude_caller.build_exit_stock_block",
                        lambda sym, iv, pos, research, **kw: f"EXIT BLOCK {sym}")
    monkeypatch.setattr("trading.claude_caller.risk_manager.check_exit_budget",
                        lambda cost, count, regime: True)
    monkeypatch.setattr("trading.claude_caller.risk_manager.record_usage",
                        lambda **kw: None)

    _ask_claude_exit_batch(
        [("AAPL", iv, {"entry_price": 102.0})], snap,
        _make_fake_ai(calls, '{"AAPL": {"action": "HOLD", "reasoning": "ok"}}'),
    )

    assert calls, "Claude was not called"
    system_blocks = calls[0].get("system", [])
    assert system_blocks, "No system blocks in exit-batch call"
    assert not any(
        isinstance(b, dict) and b.get("cache_control")
        for b in system_blocks
    ), "exit-batch system blocks must not have cache_control (TTL 5min < 10min cycle)"


@pytest.mark.unit
def test_entry_batch_system_prompt_has_cache_control(monkeypatch, tmp_path):
    """Entry-batch system blocks must NOT have cache_control.

    10-min trading cycles always exceed Anthropic's 5-min cache TTL, so
    cache_control on entry-batch calls causes write surcharges with zero reads.
    """
    from trading.claude_caller import _ask_claude_batch
    from unittest.mock import MagicMock

    calls = []
    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.regime_info = {"vix_current": 15, "spy_5day_return": 0.1}
    snap.strategy_memory = ""
    snap.research = {}
    snap.research_meta = {}
    snap.fred_cache = {}
    snap.weights = {}
    snap.params = {}

    iv = MagicMock()
    iv.composite_score = 60.0
    iv.rsi = 55.0
    iv.current_price = 100.0
    iv.atr = 1.5

    monkeypatch.setattr("trading.claude_caller.build_stock_block",
                        lambda sym, iv, snap, **kw: f"BLOCK {sym}")
    monkeypatch.setattr("trading.claude_caller.build_stable_system_context",
                        lambda *a: "STABLE CTX")
    monkeypatch.setattr("trading.claude_caller.build_dynamic_user_context",
                        lambda now, **kw: "DYNAMIC CTX")
    monkeypatch.setattr("trading.claude_caller.risk_manager.check_entry_budget",
                        lambda cost, regime: True)
    monkeypatch.setattr("trading.claude_caller.risk_manager.record_usage",
                        lambda **kw: None)

    from trading.pre_claude_filter import FilterResult
    _ask_claude_batch(
        [("AAPL", iv, None, FilterResult("CLAUDE", ""))], snap,
        _make_fake_ai(calls, '{"AAPL": {"action": "HOLD", "confidence": 50, '
                             '"reasoning": "ok", "stop_loss_pct": 4.0, '
                             '"take_profit_pct": 5.0, "indicator_signals": {}}}'),
    )

    assert calls, "Claude was not called"
    system_blocks = calls[0].get("system", [])
    assert system_blocks, "No system blocks in entry-batch call"
    cached = [b for b in system_blocks if isinstance(b, dict) and b.get("cache_control")]
    assert len(cached) >= 1, (
        f"Expected ≥1 system block with cache_control for Anthropic prompt caching, got 0"
    )


# ── P4-T5: ATR-TP injection into Claude context ──────────────────────────────

@pytest.mark.unit
def test_atr_tp_injected_into_claude_context():
    """build_stock_block emits ATR-TP line when atr_suggested_tp_pct is provided."""
    from trading.context_builder import build_stock_block
    from unittest.mock import MagicMock
    from signals.entry_gates import GateResult
    from utils.ticker_quality import TickerQuality
    from datetime import datetime
    from zoneinfo import ZoneInfo

    iv = MagicMock()
    iv.composite_score = 42.5; iv.signal = "BUY"
    iv.current_price = 100.0; iv.vwap = 99.5
    iv.rsi = 58.0; iv.rsi_slope = 1.0; iv.macd_hist = 0.002
    iv.bb_lower = 98.0; iv.bb_upper = 102.0
    iv.ema9 = 99.8; iv.ema21 = 99.5; iv.atr = 2.0
    iv.stoch_k = 60.0; iv.stoch_d = 58.0
    iv.daily_volume_ratio = 1.2
    iv.vwap_slope = 0.0; iv.or_position = "unknown"

    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.research = {"TEST": {"sentiment_score": 10, "sentiment_label": "NEUTRAL",
                              "key_catalyst": None, "risk_flag": None, "summary": ""}}
    snap.intraday_news = {}
    quality = TickerQuality("MEDIUM", 50, frozenset(), (), {})
    gate = GateResult(passed=True, reason="", flags=())
    entry_time = datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York"))

    block = build_stock_block("TEST", iv, snap, gate, quality, "DAY_TRADE", entry_time,
                              atr_suggested_tp_pct=4.5)

    assert "ATR-TP=4.5%" in block


@pytest.mark.unit
def test_claude_caller_passes_atr_tp_to_stock_block(monkeypatch, tmp_path):
    """_ask_claude_batch passes atr_suggested_tp_pct to build_stock_block."""
    import config
    from trading import claude_caller
    from trading.claude_caller import _ask_claude_batch
    from trading.cycle_context import CycleSnapshot
    from indicators.values import IndicatorValues

    captured = {}

    def mock_build_stock_block(sym, iv, snap, gate_result, quality, mode, entry_time,
                               existing_pos=None, atr_suggested_tp_pct=None, **kw):
        captured["atr_suggested_tp_pct"] = atr_suggested_tp_pct
        return f"=={sym}=="

    monkeypatch.setattr("trading.claude_caller.build_stock_block", mock_build_stock_block)

    # Also patch out the API call and budget check so we don't hit network
    monkeypatch.setattr("trading.claude_caller.risk_manager.check_entry_budget", lambda cost, regime: True)

    class FakeResponse:
        class usage:
            input_tokens = 0
            output_tokens = 0
            cache_creation_input_tokens = 0
            cache_read_input_tokens = 0
        content = [type("C", (), {"type": "text", "text": '{"TEST": {"action": "HOLD", "confidence": 50, "reasoning": "test", "stop_loss_pct": 4.0, "take_profit_pct": 5.0, "indicator_signals": {}}}'})]

    class FakeAI:
        class messages:
            @staticmethod
            def create(**kwargs):
                return FakeResponse()

    # Build a minimal IndicatorValues with atr=2.0, current_price=100.0
    iv = IndicatorValues.__new__(IndicatorValues)
    iv.atr = 2.0
    iv.current_price = 100.0
    # Fill remaining required fields with dummy values
    for field in ["composite_score", "signal", "rsi", "rsi_slope", "macd_hist",
                  "bb_lower", "bb_upper", "ema9", "ema21", "stoch_k", "stoch_d",
                  "vwap", "daily_volume_ratio"]:
        setattr(iv, field, 0.0)
    iv.signal = "HOLD"

    snap = CycleSnapshot.__new__(CycleSnapshot)
    snap.params = {"atr_tp_multiplier": 2.0}
    snap.research = {}
    snap.research_meta = {}
    snap.fred_cache = {}
    snap.regime_info = {}
    snap.regime = "NORMAL"
    snap.weights = {}
    snap.strategy_memory = ""
    snap.intraday_news = {}
    snap.portfolio_gain_pct = 0.0

    monkeypatch.setattr("trading.claude_caller._get_cached_stable_context", lambda *a, **kw: "ctx")
    monkeypatch.setattr("trading.claude_caller.build_dynamic_user_context", lambda now, **kw: "dynamic")

    from trading.pre_claude_filter import FilterResult
    _ask_claude_batch([("TEST", iv, None, FilterResult("CLAUDE", ""))], snap, FakeAI())

    # atr=2.0, price=100.0, mult=2.0 → atr_pct=2.0% → raw=4.0% → clamped to 4.0
    assert abs(captured.get("atr_suggested_tp_pct", -1) - 4.0) < 0.01
