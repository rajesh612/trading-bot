"""
Context builder, stable context cache, AE confidence tier/mode awareness,
and tier context block unit tests.
Run with: pytest tests/test_context_builder.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import pytest
from datetime import datetime


# ── TestTierContextBlocks ─────────────────────────────────────────────────────

class TestTierContextBlocks:
    def _make_iv(self, composite=55.0, rsi=62.0, stoch_k=70.0, stoch_d=68.0,
                 current_price=100.0, vwap=99.0, vwap_slope=0.003,
                 or_position="outside", relative_volume=1.4, atr=2.0):
        from unittest.mock import MagicMock
        iv = MagicMock()
        iv.composite_score = composite
        iv.rsi = rsi
        iv.stoch_k = stoch_k
        iv.stoch_d = stoch_d
        iv.current_price = current_price
        iv.vwap = vwap
        iv.vwap_slope = vwap_slope
        iv.or_position = or_position
        iv.relative_volume = relative_volume
        iv.atr = atr
        iv.signal = "BUY"
        return iv

    def _snap(self, regime="NORMAL"):
        from unittest.mock import MagicMock
        snap = MagicMock()
        snap.regime = regime
        snap.research = {}
        snap.intraday_news = {}
        return snap

    def _gate_result(self, flags=None):
        from signals.entry_gates import GateResult
        return GateResult(passed=True, reason="", flags=tuple(flags or []))

    @pytest.mark.unit
    def test_low_tier_contains_quality_gaps(self):
        from trading.context_builder import build_stock_block
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality("LOW", 28, frozenset(),
                                ("vol_weak(-12)", "sector_flat(-8)"), {})
        gate = self._gate_result(["volume_weak"])
        from zoneinfo import ZoneInfo
        block = build_stock_block(
            "RIVN", self._make_iv(composite=32.0), self._snap(),
            gate, quality, "DAY_TRADE",
            datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        )
        assert "vol_weak(-12)" in block
        assert "caution" in block.lower()
        assert "BB[" not in block
        assert "EMA9" not in block
        assert "MACD=" not in block

    @pytest.mark.unit
    def test_conviction_tier_contains_unlocks_not_quality_breakdown(self):
        from trading.context_builder import build_stock_block
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality("CONVICTION", 85, frozenset({"T2_CHOPPY", "SWING_CHOPPY"}),
                                (), {"composite": 25, "sentiment": 20})
        gate = self._gate_result()
        from zoneinfo import ZoneInfo
        block = build_stock_block(
            "NVDA", self._make_iv(composite=72.0), self._snap(),
            gate, quality, "PEAD",
            datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        )
        assert "unlocks=" in block
        assert "T2_CHOPPY" in block
        assert "composite(+25)" not in block
        assert "caution" not in block.lower()
        assert "BB[" not in block

    @pytest.mark.unit
    def test_medium_tier_shows_full_news(self):
        from trading.context_builder import build_stock_block
        from utils.ticker_quality import TickerQuality
        quality = TickerQuality("MEDIUM", 47, frozenset(), (), {})
        gate = self._gate_result()
        from unittest.mock import MagicMock
        snap = self._snap()
        snap.research = {"AAPL": {"sentiment_score": 35, "sentiment_label": "BULLISH",
                                   "summary": "Strong pipeline into Q3"}}
        from zoneinfo import ZoneInfo
        block = build_stock_block(
            "AAPL", self._make_iv(), snap, gate, quality, "DAY_TRADE",
            datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        )
        assert "Strong pipeline" in block


# ── Task 4: stable context cache regime invalidation ─────────────────────────

@pytest.mark.unit
class TestStableContextRegimeCache:
    def test_cache_hit_on_same_regime(self, monkeypatch):
        """Cache returns stored text when regime is unchanged and TTL not expired."""
        import time
        import trading.claude_caller as cc

        cc._stable_ctx_text = "cached-normal"
        cc._stable_ctx_time = time.monotonic()
        cc._stable_ctx_regime = "NORMAL"

        calls = []
        monkeypatch.setattr(cc, "build_stable_system_context", lambda *a, **kw: calls.append(1) or "rebuilt")

        result = cc._get_cached_stable_context({}, {}, {"regime": "NORMAL"})
        assert result == "cached-normal"
        assert len(calls) == 0, "should not rebuild on cache hit"

    def test_cache_invalidated_on_regime_change(self, monkeypatch):
        """Cache must rebuild when regime changes (noon refresh scenario)."""
        import time
        import trading.claude_caller as cc

        cc._stable_ctx_text = "cached-normal"
        cc._stable_ctx_time = time.monotonic()
        cc._stable_ctx_regime = "NORMAL"

        monkeypatch.setattr(cc, "build_stable_system_context",
                            lambda ri, fc, rm: f"rebuilt-{ri.get('regime')}")

        result = cc._get_cached_stable_context({}, {}, {"regime": "HIGH_VOL"})
        assert result == "rebuilt-HIGH_VOL"
        assert cc._stable_ctx_regime == "HIGH_VOL"


# ── Task 6: AE confidence tier-aware ─────────────────────────────────────────

@pytest.mark.unit
class TestAeConfidenceTierAware:
    def test_conviction_higher_than_low(self):
        from trading.pipeline import _compute_auto_execute_confidence
        conf_conv = _compute_auto_execute_confidence(70.0, 10, "NORMAL", "CONVICTION")
        conf_low  = _compute_auto_execute_confidence(70.0, 10, "NORMAL", "LOW")
        assert conf_conv > conf_low, f"CONVICTION ({conf_conv}) should > LOW ({conf_low})"

    def test_low_tier_capped(self):
        from trading.pipeline import _compute_auto_execute_confidence
        import config
        conf = _compute_auto_execute_confidence(90.0, 50, "BULL_TREND", "LOW")
        assert conf <= config.AUTO_EXECUTE_CONFIDENCE_LOW_CAP, (
            f"LOW tier capped at {config.AUTO_EXECUTE_CONFIDENCE_LOW_CAP}, got {conf}"
        )


# ── Task 16: context_builder uses actual mode for compute_tp_sl ───────────────

@pytest.mark.unit
class TestContextBuilderModeAware:
    def test_block_conviction_uses_actual_mode(self):
        """_block_conviction must pass actual mode to compute_tp_sl, not hardcoded DAY_TRADE."""
        from unittest.mock import MagicMock, patch
        from trading.context_builder import _block_conviction

        captured_modes = []

        def spy_compute(base_tp, base_sl, entry_time, tier, regime, mode="DAY_TRADE"):
            captured_modes.append(mode)
            return base_tp, base_sl

        snap = MagicMock()
        snap.regime = "NORMAL"
        quality = MagicMock()
        quality.tier = "CONVICTION"
        quality.unlocks = frozenset()
        quality.component_scores = {}
        gate_result = MagicMock()
        gate_result.flags = ()

        from zoneinfo import ZoneInfo
        entry_time = datetime(2026, 5, 29, 10, 30, tzinfo=ZoneInfo("America/New_York"))

        with patch("trading.context_builder.compute_tp_sl", spy_compute):
            _block_conviction(
                "AAPL", MagicMock(current_price=100.0, rsi=55.0, composite_score=70.0,
                                   signal="BUY"),
                snap, gate_result, quality,
                mode="PEAD", entry_time=entry_time,
                existing_pos=None, atr_suggested_tp_pct=3.0, intraday_news=None,
            )

        assert captured_modes and captured_modes[0] == "PEAD", (
            f"Expected PEAD passed to compute_tp_sl, got {captured_modes}"
        )


# ── Task 17: exit block includes mode_at_entry ────────────────────────────────

@pytest.mark.unit
class TestExitBlockModeAware:
    def test_exit_block_includes_mode_at_entry(self):
        """build_exit_stock_block must include mode_at_entry from position dict."""
        from unittest.mock import MagicMock
        from trading.context_builder import build_exit_stock_block

        pos = {
            "entry_price": 100.0, "qty": 10, "stop_loss_pct": 3.0,
            "entry_time": "2026-05-29T10:30:00",
            "mode_at_entry": "PEAD",
        }
        iv = MagicMock()
        iv.current_price = 103.0
        iv.composite_score = 65.0

        block = build_exit_stock_block("AAPL", iv, pos, research={})
        assert "PEAD" in block, f"mode_at_entry 'PEAD' not in exit block:\n{block}"

    def test_ae_confidence_higher_for_pead_than_day_trade(self):
        """PEAD mode AE confidence must exceed DAY_TRADE for same composite + sentiment."""
        from trading.pipeline import _compute_auto_execute_confidence

        conf_pead = _compute_auto_execute_confidence(65.0, 5, "NORMAL", "HIGH", "PEAD")
        conf_day  = _compute_auto_execute_confidence(65.0, 5, "NORMAL", "HIGH", "DAY_TRADE")
        assert conf_pead > conf_day, f"PEAD ({conf_pead}) should > DAY_TRADE ({conf_day})"


# ── TestContextSplit ──────────────────────────────────────────────────────────

@pytest.mark.unit
class TestContextSplit:
    """Verify stable vs. dynamic context separation for prompt caching."""

    _REGIME = {
        "regime": "NORMAL", "macro_health_score": 5,
        "vix_current": 18.0, "vix_90d_avg": 20.0, "vix_trend": "falling",
        "spy_5day_return": 1.2, "consecutive_green_days": 3,
        "consecutive_red_days": 0, "spy_5day_sign_changes": 1,
        "sector_scores": {"XLK": 10, "XLF": -5},
        "hyg_ret5d": 0.3, "iwm_ret5d": 0.8, "vix3m": 19.0,
    }
    _FRED = {"fed_direction": "paused", "cpi_trend": "stable", "unemployment_rising": False}
    _META = {"macro_shock_score": 10, "capitulation_candidates": []}

    def test_stable_context_contains_no_time(self):
        """build_stable_system_context must not embed a clock time string."""
        import re
        from trading.context_builder import build_stable_system_context
        text = build_stable_system_context(self._REGIME, self._FRED, self._META)
        assert not re.search(r"\d{2}:\d{2}", text), (
            "Stable context must not contain a time like '10:30' — it breaks prompt caching"
        )

    def test_stable_context_is_deterministic(self):
        """Same inputs produce identical output — required for cache hits."""
        from trading.context_builder import build_stable_system_context
        a = build_stable_system_context(self._REGIME, self._FRED, self._META)
        b = build_stable_system_context(self._REGIME, self._FRED, self._META)
        assert a == b

    def test_dynamic_context_contains_time(self):
        """build_dynamic_user_context must include the current HH:MM marker."""
        from trading.context_builder import build_dynamic_user_context
        from utils.constants import ET
        now = datetime(2024, 1, 15, 11, 30, 0, tzinfo=ET)
        text = build_dynamic_user_context(now)
        assert "11:30" in text


# ── strategy_memory + stock block tests ──────────────────────────────────────

@pytest.mark.unit
def test_strategy_memory_truncation_keeps_head_not_tail(tmp_path, monkeypatch):
    """build_cycle_snapshot must truncate strategy_memory from the head, not the tail."""
    import config
    from unittest.mock import patch
    from trading.cycle_context import build_cycle_snapshot

    long_memory = "# General Rules\n" + ("A" * 100 + "\n") * 20 + "# Tail Section\n" + ("B" * 100 + "\n") * 20
    mem_file = tmp_path / "strategy_memory.md"
    mem_file.write_text(long_memory)

    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", mem_file)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_MAX_CHARS", 500)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_MAX_AGE_DAYS", 365)

    import trading.cycle_context as _cc
    with patch("trading.cycle_context.load_macro_regime", return_value={"regime": "NORMAL"}), \
         patch("trading.cycle_context.load_trading_params", return_value={}), \
         patch("trading.cycle_context.load_json", return_value={}), \
         patch("trading.cycle_context._get_fred_cache", return_value={}), \
         patch("trading.cycle_context.load_intraday_news", return_value={}), \
         patch.object(_cc, "config", config):
        snap = build_cycle_snapshot(portfolio_gain_pct=0.0)

    assert snap.strategy_memory.startswith("# General Rules"), "Must keep head (rules section)"
    assert "Tail Section" not in snap.strategy_memory, "Must not keep tail"
    assert len(snap.strategy_memory) <= 500


@pytest.mark.unit
def test_stock_block_is_under_150_tokens():
    """build_stock_block must stay compact to conserve Claude budget."""
    from trading.context_builder import build_stock_block
    from unittest.mock import MagicMock, patch
    from signals.entry_gates import GateResult
    from utils.ticker_quality import TickerQuality
    from zoneinfo import ZoneInfo

    iv = MagicMock()
    iv.composite_score = 42.5; iv.signal = "BUY"
    iv.current_price = 100.50; iv.vwap = 100.20
    iv.rsi = 58.3; iv.rsi_slope = 2.1; iv.macd_hist = 0.0023
    iv.bb_lower = 99.0; iv.bb_upper = 102.0
    iv.ema9 = 100.2; iv.ema21 = 99.8; iv.atr = 0.05
    iv.stoch_k = 65.0; iv.stoch_d = 60.0
    iv.daily_volume_ratio = 1.2
    iv.vwap_slope = 0.023; iv.or_position = "outside"

    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.research = {"TEST": {
        "sentiment_score": 35, "sentiment_label": "BULLISH",
        "key_catalyst": "AI chip demand", "risk_flag": None,
        "summary": "",
    }}
    snap.intraday_news = {}
    quality = TickerQuality("MEDIUM", 50, frozenset(), (), {})
    gate = GateResult(passed=True, reason="", flags=())
    entry_time = datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York"))

    block = build_stock_block("TEST", iv, snap, gate, quality, "DAY_TRADE", entry_time)

    approx_tokens = len(block) / 4
    assert approx_tokens < 200, f"Stock block too large: ~{approx_tokens:.0f} tokens\n{block}"
    assert "ATR=" not in block, "ATR is not directional — remove from Claude block"
    assert "No summary" not in block, "Empty summary must be omitted"
    assert '<stock symbol="TEST"' in block, "Stock block must use XML wrapper"
    assert "OR=outside" in block, "Opening range position must be in block"


@pytest.mark.unit
def test_stock_block_marks_degraded_research():
    """A research_degraded symbol must show a degraded marker so Claude sees sentiment is blind."""
    from trading.context_builder import build_stock_block
    from unittest.mock import MagicMock
    from signals.entry_gates import GateResult
    from utils.ticker_quality import TickerQuality
    from zoneinfo import ZoneInfo

    iv = MagicMock()
    iv.composite_score = 55.0; iv.signal = "BUY"; iv.current_price = 100.0; iv.vwap = 99.0
    iv.rsi = 55.0; iv.vwap_slope = 0.05; iv.or_position = "outside"
    iv.stoch_k = 60.0; iv.stoch_d = 55.0
    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.research = {"TEST": {"sentiment_score": 0, "sentiment_label": "NEUTRAL",
                              "research_degraded": True, "summary": ""}}
    snap.intraday_news = {}
    quality = TickerQuality("MEDIUM", 50, frozenset(), (), {})
    gate = GateResult(passed=True, reason="", flags=())
    entry_time = datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York"))

    block = build_stock_block("TEST", iv, snap, gate, quality, "DAY_TRADE", entry_time)
    assert "degraded" in block.lower(), f"degraded research must be surfaced to Claude:\n{block}"


@pytest.mark.unit
def test_stock_block_includes_sl_tp_for_existing_position():
    """build_stock_block pos_block must include SL/TP so Claude can assess whether to hold."""
    from trading.context_builder import build_stock_block
    from unittest.mock import MagicMock
    from signals.entry_gates import GateResult
    from utils.ticker_quality import TickerQuality
    from zoneinfo import ZoneInfo

    iv = MagicMock()
    iv.composite_score = 30.0; iv.signal = "HOLD"
    iv.current_price = 105.0; iv.vwap = 104.0
    iv.rsi = 55.0; iv.rsi_slope = 0.5; iv.macd_hist = 0.001
    iv.bb_lower = 103.0; iv.bb_upper = 107.0
    iv.ema9 = 104.5; iv.ema21 = 104.0
    iv.stoch_k = 60.0; iv.stoch_d = 55.0
    iv.daily_volume_ratio = 1.0
    iv.vwap_slope = 0.0; iv.or_position = "unknown"

    existing_pos = {
        "entry_price": 100.0, "qty": 10,
        "stop_loss_pct": 3.5, "take_profit_pct": 6.0,
    }
    snap = MagicMock()
    snap.regime = "NORMAL"
    snap.research = {"TEST": {"sentiment_score": 10, "sentiment_label": "NEUTRAL", "key_catalyst": None, "risk_flag": None, "summary": ""}}
    snap.intraday_news = {}
    quality = TickerQuality("MEDIUM", 50, frozenset(), (), {})
    gate = GateResult(passed=True, reason="", flags=())
    entry_time = datetime(2026, 5, 21, 11, 30, tzinfo=ZoneInfo("America/New_York"))

    block = build_stock_block("TEST", iv, snap, gate, quality, "DAY_TRADE", entry_time,
                              existing_pos=existing_pos)

    assert "SL=3.5%" in block, f"SL missing from pos_block: {block}"
    assert "TP=6.0%" in block, f"TP missing from pos_block: {block}"


# ── P4-T5: _compute_auto_execute_confidence (scale) ──────────────────────────

@pytest.mark.unit
def test_auto_execute_confidence_scales_with_composite(monkeypatch):
    import config
    from trading.pipeline import _compute_auto_execute_confidence
    monkeypatch.setattr(config, "AUTO_EXECUTE_CONFIDENCE_BASE", 75)
    monkeypatch.setattr(config, "AUTO_EXECUTE_COMPOSITE_MIN", 55)
    conf = _compute_auto_execute_confidence(composite_score=60.0, intra_sentiment=30)
    assert 75 <= conf <= 100
    conf_low = _compute_auto_execute_confidence(composite_score=55.0, intra_sentiment=0)
    assert conf_low >= config.AUTO_EXECUTE_CONFIDENCE_BASE
    assert conf_low <= conf


# ── Task 11: auto-execute guard gate + intra_sent fix ────────────────────────

@pytest.mark.unit
def test_auto_execute_blocked_when_guard_disallows(monkeypatch):
    """_execute_auto_entries must not call execute_buy when auto_execute_allowed is False."""
    from trading.pipeline import _execute_auto_entries
    from unittest.mock import MagicMock, patch
    from utils.data_health import StalenessGuard

    stale_guard = StalenessGuard(
        composite_penalty=10, auto_execute_allowed=False,
        t2_allowed=False, swing_fallback_regime="NORMAL",
        can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
    )
    snap = MagicMock()
    snap.staleness_guard = stale_guard
    snap.research = {}
    snap.intraday_news = {}

    iv = MagicMock()
    iv.composite_score = 80.0
    iv.atr = 2.0
    iv.current_price = 100.0

    buy_called = []
    with patch("trading.pipeline.execute_buy", side_effect=lambda *a, **kw: buy_called.append(True)), \
         patch("trading.pipeline.compute_atr_tp_pct", return_value=5.0):
        result_entries, result_bp, result_batch = _execute_auto_entries(
            auto_execute=[("AAPL", iv, None, 0.0)],
            snap=snap,
            alpaca=MagicMock(),
            open_positions={},
            portfolio_value=100_000,
            buying_power=50_000,
            pos_mult=1.0,
            spy_ret5d=0.0,
            rsi_max=70,
            atr_tp_mult=2.0,
            can_open=True,
            now=MagicMock(),
            daily_pnl_pct=0.0,
        )
    assert buy_called == [], "execute_buy must not be called when auto_execute_allowed=False"
    assert result_entries == 0
    assert result_bp == 50_000
    assert result_batch == []


@pytest.mark.unit
def test_auto_execute_reads_symbol_intraday_sentiment_not_shock(monkeypatch):
    """_execute_auto_entries uses intraday_news[symbol] sentiment, not __meta__ shock_score."""
    from trading.pipeline import _execute_auto_entries
    from unittest.mock import MagicMock, patch
    from utils.data_health import FRESH_GUARD

    snap = MagicMock()
    snap.staleness_guard = FRESH_GUARD
    # __meta__.shock_score=99 — if intra_sent reads this it would be 99 (wrong)
    snap.research = {"__meta__": {"shock_score": 99}, "AAPL": {"sentiment_score": 80}}
    snap.intraday_news = {"AAPL": {"sentiment_score": 60}}
    snap.regime = "NORMAL"

    iv = MagicMock()
    iv.composite_score = 80.0
    iv.atr = 2.0
    iv.current_price = 100.0

    captured_sentiment = {}

    original_confidence = __import__("trading.pipeline", fromlist=["_compute_auto_execute_confidence"])._compute_auto_execute_confidence

    def spy_confidence(composite_score, intra_sentiment, regime="NORMAL", tier="MEDIUM", mode="DAY_TRADE"):
        captured_sentiment["intra_sentiment"] = intra_sentiment
        return original_confidence(composite_score, intra_sentiment, regime=regime, tier=tier, mode=mode)

    with patch("trading.pipeline._compute_auto_execute_confidence", side_effect=spy_confidence), \
         patch("trading.pipeline.execute_buy", return_value=None):
        _execute_auto_entries(
            auto_execute=[("AAPL", iv, None, 0.0)],
            snap=snap,
            alpaca=MagicMock(),
            open_positions={},
            portfolio_value=100_000,
            buying_power=50_000,
            pos_mult=1.0,
            spy_ret5d=0.0,
            rsi_max=70,
            atr_tp_mult=2.0,
            can_open=True,
            now=MagicMock(),
            daily_pnl_pct=0.0,
        )

    # intra_sent must come from intraday_news["AAPL"]["sentiment_score"]=60, NOT from __meta__.shock_score=99
    assert captured_sentiment.get("intra_sentiment") == 60, (
        f"Expected intra_sentiment=60 (from intraday_news), got {captured_sentiment.get('intra_sentiment')}"
    )
