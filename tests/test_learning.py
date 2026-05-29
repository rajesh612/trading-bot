"""
Learning agent unit tests.
Run with: pytest tests/test_learning.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import pytest
import config


# ── learning_agent: threshold clamping ───────────────────────────────────────

@pytest.mark.unit
class TestClampThreshold:
    """learning_agent._clamp_threshold hard-bound clamping."""

    def test_within_bounds_unchanged(self):
        from learning.trade_analysis import _clamp_threshold
        assert _clamp_threshold("stop_loss_pct", 4.0) == 4.0

    def test_below_lower_bound_clamped_up(self):
        from learning.trade_analysis import _clamp_threshold
        # stop_loss_pct lower bound is 1.5
        result = _clamp_threshold("stop_loss_pct", 0.5)
        assert result == 1.5

    def test_above_upper_bound_clamped_down(self):
        from learning.trade_analysis import _clamp_threshold
        # stop_loss_pct upper bound is 12.0
        result = _clamp_threshold("stop_loss_pct", 20.0)
        assert result == 12.0

    def test_integer_key_returns_int(self):
        from learning.trade_analysis import _clamp_threshold
        # rsi_buy_max bounds: (30, 80) — both int
        result = _clamp_threshold("rsi_buy_max", 45)
        assert isinstance(result, int)
        assert result == 45

    def test_unknown_key_returns_value_unchanged(self):
        from learning.trade_analysis import _clamp_threshold
        assert _clamp_threshold("nonexistent_key", 99.9) == 99.9


# ── learning_agent: regime aggregation ───────────────────────────────────────

@pytest.mark.unit
class TestAggregateByRegime:
    """learning_agent._aggregate_by_regime grouping logic."""

    @staticmethod
    def _make_trade(symbol, pnl_pct, pnl_dollar, regime="NORMAL", mode="DAY_TRADE"):
        return {
            "symbol": symbol,
            "pnl_pct": pnl_pct,
            "pnl_dollar": pnl_dollar,
            "regime_at_entry": regime,
            "mode_at_entry": mode,
            "close_reason": "monitor_stop_loss",
            "entry_sentiment_score": 0,
        }

    def test_groups_by_regime(self):
        from learning.trade_analysis import _aggregate_by_regime
        trades = [
            self._make_trade("A", 2.0, 20.0, regime="NORMAL"),
            self._make_trade("B", -1.0, -10.0, regime="NORMAL"),
            self._make_trade("C", 3.0, 30.0, regime="CHOPPY"),
        ]
        result = _aggregate_by_regime(trades)
        assert "NORMAL" in result["by_regime"]
        assert "CHOPPY" in result["by_regime"]
        assert result["by_regime"]["NORMAL"]["count"] == 2
        assert result["by_regime"]["CHOPPY"]["count"] == 1

    def test_win_rate_calculation(self):
        from learning.trade_analysis import _aggregate_by_regime
        trades = [
            self._make_trade("A",  2.0,  20.0, regime="NORMAL"),
            self._make_trade("B",  1.0,  10.0, regime="NORMAL"),
            self._make_trade("C", -1.0, -10.0, regime="NORMAL"),
        ]
        result = _aggregate_by_regime(trades)
        nr = result["by_regime"]["NORMAL"]
        assert nr["winners"] == 2
        assert nr["losers"] == 1
        assert nr["win_rate_pct"] == pytest.approx(66.7, abs=0.1)

    def test_groups_by_mode(self):
        from learning.trade_analysis import _aggregate_by_regime
        trades = [
            self._make_trade("A", 1.0, 10.0, mode="DAY_TRADE"),
            self._make_trade("B", 2.0, 20.0, mode="HYBRID"),
        ]
        result = _aggregate_by_regime(trades)
        assert result["by_mode"]["DAY_TRADE"]["count"] == 1
        assert result["by_mode"]["HYBRID"]["count"] == 1

    def test_missing_regime_falls_back_to_unknown(self):
        from learning.trade_analysis import _aggregate_by_regime
        trades = [{"symbol": "X", "pnl_pct": 1.0, "pnl_dollar": 10.0}]
        result = _aggregate_by_regime(trades)
        assert "UNKNOWN" in result["by_regime"]


# ── learning_agent: loss source classification ────────────────────────────────

@pytest.mark.unit
class TestClassifyLossSource:
    """learning_agent._classify_loss_source classification priority."""

    def test_earnings_close_reason(self):
        from learning.trade_analysis import _classify_loss_source
        trade = {"close_reason": "earnings_gap_down", "pnl_pct": -5.0, "entry_indicators": {}}
        assert _classify_loss_source(trade) == "earnings_surprise"

    def test_crisis_regime_at_entry(self):
        from learning.trade_analysis import _classify_loss_source
        trade = {"close_reason": "monitor_stop_loss", "regime_at_entry": "CRISIS",
                 "pnl_pct": -3.0, "entry_indicators": {}}
        assert _classify_loss_source(trade) == "market_structure"

    def test_flash_crash_close_reason(self):
        from learning.trade_analysis import _classify_loss_source
        trade = {"close_reason": "flash_crash", "pnl_pct": -4.0, "entry_indicators": {}}
        assert _classify_loss_source(trade) == "market_structure"

    def test_momentum_exhaustion_at_entry(self):
        from learning.trade_analysis import _classify_loss_source
        trade = {
            "close_reason": "monitor_stop_loss",
            "regime_at_entry": "NORMAL",
            "entry_indicators": {"momentum_exhaustion": True},
            "pnl_pct": -2.0,
        }
        assert _classify_loss_source(trade) == "momentum_exhaustion"

    def test_negative_sentiment_sector_narrative(self):
        from learning.trade_analysis import _classify_loss_source
        trade = {
            "close_reason": "monitor_stop_loss",
            "regime_at_entry": "NORMAL",
            "entry_indicators": {},
            "entry_sentiment_score": -30,
            "pnl_pct": -1.5,
        }
        assert _classify_loss_source(trade) == "sector_narrative"

    def test_unknown_when_no_signal(self):
        from learning.trade_analysis import _classify_loss_source
        trade = {
            "close_reason": "monitor_stop_loss",
            "regime_at_entry": "NORMAL",
            "entry_indicators": {},
            "entry_sentiment_score": 5,
            "pnl_pct": -1.0,
        }
        assert _classify_loss_source(trade) == "unknown"


# ── learning_agent: file I/O helpers ─────────────────────────────────────────

@pytest.mark.unit
class TestLearningAgentHelpers:
    def test_load_todays_closed_trades_missing_file(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "trade_journal.json")
        from learning.data import _load_todays_closed_trades
        assert _load_todays_closed_trades() == []

    def test_load_todays_closed_trades_filters_by_today(self, tmp_path, monkeypatch):
        import config, json
        from datetime import datetime
        from utils.constants import ET
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "trade_journal.json")
        today = datetime.now(ET).date().isoformat()
        journal = [
            {"type": "CLOSE", "timestamp": f"{today}T10:30:00", "symbol": "NVDA",
             "pnl_pct": 2.0, "pnl_dollar": 100.0, "close_reason": "tp"},
            {"type": "CLOSE", "timestamp": "2020-01-01T10:30:00", "symbol": "AAPL",
             "pnl_pct": -1.0, "pnl_dollar": -50.0, "close_reason": "sl"},
            {"type": "BUY", "timestamp": f"{today}T09:30:00", "symbol": "AMD"},
        ]
        (tmp_path / "trade_journal.json").write_text(json.dumps(journal))
        from learning.data import _load_todays_closed_trades
        result = _load_todays_closed_trades()
        assert len(result) == 1
        assert result[0]["symbol"] == "NVDA"

    def test_save_and_load_weights(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", tmp_path / "weights.json")
        from learning.data import _save_weights, _load_weights
        weights = {"RSI": 20.0, "MACD": 15.0}
        _save_weights(weights)
        loaded = _load_weights()
        assert loaded == weights

    def test_save_trading_params_adds_last_updated(self, tmp_path, monkeypatch):
        import config, json
        from datetime import datetime
        from utils.constants import ET
        params_file = tmp_path / "trading_params.json"
        monkeypatch.setattr(config, "TRADING_PARAMS_FILE", params_file)
        from learning.data import _save_trading_params
        _save_trading_params({"stop_loss_pct": 4.0})
        saved = json.loads(params_file.read_text())
        assert saved["last_updated"] == datetime.now(ET).date().isoformat()


# ── macro_regime: load_trading_params ────────────────────────────────────────

@pytest.mark.unit
class TestLoadTradingParamsInMacroRegime:
    def test_returns_config_defaults_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", tmp_path / "trading_params.json")
        from macro.regime import load_trading_params
        params = load_trading_params()
        import config as cfg
        assert params["entry_start_hour"] == cfg.ENTRY_START_HOUR
        assert params["rsi_buy_max"] == cfg.RSI_BUY_MAX
        assert params["stop_loss_pct"] == cfg.STOP_LOSS_PCT

    def test_file_values_override_defaults(self, tmp_path, monkeypatch):
        params_file = tmp_path / "trading_params.json"
        params_file.write_text('{"rsi_buy_max": 60, "entry_start_hour": 11}')
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", params_file)
        from macro.regime import load_trading_params
        params = load_trading_params()
        assert params["rsi_buy_max"] == 60
        assert params["entry_start_hour"] == 11

    def test_load_macro_regime_returns_regime_key(self, tmp_path, monkeypatch):
        from macro.regime import load_macro_regime
        result = load_macro_regime()
        assert "regime" in result


# ── main.py: capability summary ──────────────────────────────────────────────

@pytest.mark.unit
class TestCapabilitySummary:
    def test_degraded_items_identified_when_keys_missing(self, monkeypatch):
        """_log_capability_summary returns list of degraded integration names."""
        import config as cfg
        monkeypatch.setattr(cfg, "FRED_API_KEY", "")
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "")
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "user@example.com")
        monkeypatch.setattr(cfg, "EMAIL_APP_PASSWORD", "pass")
        monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setattr(cfg, "TELEGRAM_CHAT_ID", "123")
        from main import _log_capability_summary
        degraded = _log_capability_summary()
        assert any("FRED" in item for item in degraded)
        assert any("Polygon" in item for item in degraded)
        assert not any("Email" in item for item in degraded)

    def test_no_degraded_when_all_keys_present(self, monkeypatch, tmp_path):
        import config as cfg
        from pathlib import Path
        monkeypatch.setattr(cfg, "FRED_API_KEY", "abc")
        monkeypatch.setattr(cfg, "POLYGON_API_KEY", "xyz")
        monkeypatch.setattr(cfg, "EMAIL_SENDER", "user@example.com")
        monkeypatch.setattr(cfg, "EMAIL_APP_PASSWORD", "pass")
        monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setattr(cfg, "TELEGRAM_CHAT_ID", "123")
        # Point FRED_CACHE_FILE at a non-existent tmp path so no real file is read
        monkeypatch.setattr(cfg, "FRED_CACHE_FILE", tmp_path / "fred_cache.json")
        from main import _log_capability_summary
        degraded = _log_capability_summary()
        assert degraded == []


# ── trading_agent: progressive loss reduction ─────────────────────────────────

@pytest.mark.unit
class TestProgressiveLoss:
    def _journal_entry(self, action: str, pnl_pct: float, today: bool = True) -> dict:
        """Create a synthetic journal entry with optional date override."""
        from datetime import datetime
        from utils.constants import ET
        ts = datetime.now(ET).date().isoformat() if today else "2020-01-01"
        return {"type": action, "pnl_pct": pnl_pct, "timestamp": f"{ts}T10:00:00"}

    def test_zero_losses_returns_full_multiplier(self):
        """With zero losses, multiplier should be 1.0."""
        from trading.position_sizer import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(0) == pytest.approx(1.0)

    def test_one_loss_reduces_size(self):
        """One loss: 1.0 - 0.2 * 1 = 0.8."""
        from trading.position_sizer import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(1) == pytest.approx(0.80)

    def test_two_losses_reduces_further(self):
        """Two losses: 1.0 - 0.2 * 2 = 0.6."""
        from trading.position_sizer import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(2) == pytest.approx(0.60)

    def test_three_losses_hits_floor(self):
        """Three losses: 1.0 - 0.2 * 3 = 0.4 = PROGRESSIVE_LOSS_FLOOR."""
        from trading.position_sizer import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(3) == pytest.approx(0.40)

    def test_four_losses_capped_at_floor(self):
        """Four losses: 1.0 - 0.2 * 4 = 0.2 < floor 0.4 → capped at 0.4."""
        from trading.position_sizer import _progressive_loss_multiplier
        assert _progressive_loss_multiplier(4) == pytest.approx(0.40)

    def _write_journal(self, tmp_path, entries):
        import json, config
        p = tmp_path / "trade_journal.json"
        p.write_text(json.dumps(entries))
        return p

    def test_count_losses_empty_journal(self, tmp_path, monkeypatch):
        """Empty journal should return zero losses."""
        import config
        p = self._write_journal(tmp_path, [])
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", p)
        from trading.journal import count_todays_losses
        assert count_todays_losses() == 0

    def test_count_losses_counts_only_today(self, tmp_path, monkeypatch):
        """Count only CLOSE entries from today with negative pnl_pct."""
        import config
        entries = [
            self._journal_entry("CLOSE", -1.5, today=True),
            self._journal_entry("CLOSE", -2.0, today=False),  # yesterday
        ]
        p = self._write_journal(tmp_path, entries)
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", p)
        from trading.journal import count_todays_losses
        assert count_todays_losses() == 1

    def test_count_losses_ignores_wins(self, tmp_path, monkeypatch):
        """Ignore positive pnl_pct and non-CLOSE entries."""
        import config
        entries = [
            self._journal_entry("CLOSE", 2.0, today=True),   # win — ignore
            self._journal_entry("CLOSE", -1.5, today=True),   # loss — count
            self._journal_entry("BUY", -0.5, today=True),     # BUY entry — ignore
        ]
        p = self._write_journal(tmp_path, entries)
        monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", p)
        from trading.journal import count_todays_losses
        assert count_todays_losses() == 1


# ── learning_agent: regime win-rate computation ───────────────────────────────

@pytest.mark.unit
class TestRegimeWinRate:
    def _trade(self, regime: str, pnl_pct: float, action: str = "CLOSE") -> dict:
        """Helper to construct a trade dict with minimal fields."""
        return {
            "type": action,
            "regime_at_entry": regime,
            "pnl_pct": pnl_pct,
        }

    def test_empty_trades_returns_empty(self):
        """Empty trade list should return empty dict."""
        from learning.trade_analysis import _compute_regime_winrate
        assert _compute_regime_winrate([]) == {}

    def test_single_win(self):
        """Single winning trade should have 100% win rate."""
        from learning.trade_analysis import _compute_regime_winrate
        result = _compute_regime_winrate([self._trade("BULL_TREND", 2.0)])
        assert result == {"BULL_TREND": pytest.approx(1.0)}

    def test_single_loss(self):
        """Single losing trade should have 0% win rate."""
        from learning.trade_analysis import _compute_regime_winrate
        result = _compute_regime_winrate([self._trade("CHOPPY", -1.5)])
        assert result == {"CHOPPY": pytest.approx(0.0)}

    def test_mixed_regime_winrate(self):
        """Multiple trades across regimes should compute per-regime rates."""
        from learning.trade_analysis import _compute_regime_winrate
        trades = [
            self._trade("BULL_TREND", 2.0),      # win
            self._trade("BULL_TREND", -1.0),     # loss
            self._trade("CHOPPY", -2.0),         # loss
        ]
        result = _compute_regime_winrate(trades)
        assert result["BULL_TREND"] == pytest.approx(0.5)
        assert result["CHOPPY"] == pytest.approx(0.0)

    def test_ignores_buy_entries(self):
        """Only CLOSE entries should be counted; BUY entries ignored."""
        from learning.trade_analysis import _compute_regime_winrate
        trades = [
            self._trade("BULL_TREND", 2.0, action="BUY"),   # ignored
            self._trade("BULL_TREND", 2.0, action="CLOSE"),  # counted (win)
        ]
        result = _compute_regime_winrate(trades)
        assert result["BULL_TREND"] == pytest.approx(1.0)  # only 1 trade counted


# ── learning_agent: extended coverage ────────────────────────────────────────

@pytest.mark.unit
class TestLearningAgentOrchestration:
    """Extended coverage for learning_agent pure helpers."""

    @staticmethod
    def _trade(symbol="AAPL", pnl_pct=1.0, pnl_dollar=50.0,
               regime="NORMAL", mode="DAY_TRADE",
               close_reason="tp", entry_sentiment_score=0,
               entry_indicators=None):
        return {
            "symbol": symbol,
            "pnl_pct": pnl_pct,
            "pnl_dollar": pnl_dollar,
            "regime_at_entry": regime,
            "mode_at_entry": mode,
            "close_reason": close_reason,
            "entry_sentiment_score": entry_sentiment_score,
            "entry_indicators": entry_indicators or {},
        }

    # ── _clamp_threshold type contracts ───────────────────────────────────

    def test_clamp_float_key_returns_float(self):
        from learning.trade_analysis import _clamp_threshold
        result = _clamp_threshold("stop_loss_pct", 4.0)
        assert isinstance(result, float)

    def test_clamp_at_lower_boundary_returns_lower(self):
        from learning.trade_analysis import _clamp_threshold
        # stop_loss_pct lower bound is 1.5 — pass exactly 1.5
        result = _clamp_threshold("stop_loss_pct", 1.5)
        assert result == pytest.approx(1.5)

    def test_clamp_at_upper_boundary_returns_upper(self):
        from learning.trade_analysis import _clamp_threshold
        # stop_loss_pct upper bound is 12.0 — pass exactly 12.0
        result = _clamp_threshold("stop_loss_pct", 12.0)
        assert result == pytest.approx(12.0)

    def test_clamp_int_key_below_lower_returns_int(self):
        from learning.trade_analysis import _clamp_threshold
        # swing_max_days bounds: (3, 7) — both int
        result = _clamp_threshold("swing_max_days", 1)
        assert result == 3
        assert isinstance(result, int)

    def test_clamp_invalid_value_returns_unchanged(self):
        from learning.trade_analysis import _clamp_threshold
        result = _clamp_threshold("stop_loss_pct", "not_a_number")
        assert result == "not_a_number"

    # ── _aggregate_by_regime pnl totals ───────────────────────────────────

    def test_aggregate_total_pnl_dollar_summed_correctly(self):
        from learning.trade_analysis import _aggregate_by_regime
        trades = [
            self._trade("A", pnl_pct=2.0, pnl_dollar=100.0, regime="NORMAL"),
            self._trade("B", pnl_pct=-1.0, pnl_dollar=-40.0, regime="NORMAL"),
        ]
        result = _aggregate_by_regime(trades)
        normal = result["by_regime"]["NORMAL"]
        assert normal["total_pnl_dollar"] == pytest.approx(60.0)

    def test_aggregate_empty_trades_returns_empty_dicts(self):
        from learning.trade_analysis import _aggregate_by_regime
        result = _aggregate_by_regime([])
        assert result["by_regime"] == {}
        assert result["by_mode"] == {}

    def test_aggregate_avg_pnl_pct_calculated(self):
        from learning.trade_analysis import _aggregate_by_regime
        trades = [
            self._trade("A", pnl_pct=4.0, pnl_dollar=80.0, regime="BULL_TREND"),
            self._trade("B", pnl_pct=2.0, pnl_dollar=40.0, regime="BULL_TREND"),
        ]
        result = _aggregate_by_regime(trades)
        bt = result["by_regime"]["BULL_TREND"]
        assert bt["avg_pnl_pct"] == pytest.approx(3.0)

    # ── _classify_loss_source additional branches ─────────────────────────

    def test_classify_fundamental_close_reason(self):
        from learning.trade_analysis import _classify_loss_source
        trade = self._trade(close_reason="guidance_cut", pnl_pct=-3.0)
        assert _classify_loss_source(trade) == "fundamental"

    def test_classify_macro_pressure_high_shock_score(self):
        from learning.trade_analysis import _classify_loss_source
        trade = self._trade(
            close_reason="monitor_stop_loss",
            pnl_pct=-2.0,
            entry_indicators={"entry_macro_shock_score": 65},
        )
        assert _classify_loss_source(trade) == "macro_pressure"

    def test_classify_crash_recovery_is_market_structure(self):
        from learning.trade_analysis import _classify_loss_source
        trade = self._trade(close_reason="sl_hit", pnl_pct=-4.0)
        trade["regime_at_entry"] = "CRASH_RECOVERY"
        assert _classify_loss_source(trade) == "market_structure"

    # ── _compute_regime_winrate ────────────────────────────────────────────

    def test_winrate_ignores_trades_without_regime(self):
        from learning.trade_analysis import _compute_regime_winrate
        trades = [
            {"type": "CLOSE", "pnl_pct": 2.0},           # no regime_at_entry
            {"type": "CLOSE", "regime_at_entry": "NORMAL", "pnl_pct": 3.0},  # counted
        ]
        result = _compute_regime_winrate(trades)
        assert "NORMAL" in result
        assert result["NORMAL"] == pytest.approx(1.0)
        assert len(result) == 1  # trade without regime not counted

    def test_winrate_multiple_regimes_independent(self):
        from learning.trade_analysis import _compute_regime_winrate
        trades = [
            {"type": "CLOSE", "regime_at_entry": "BULL_TREND", "pnl_pct": 2.0},
            {"type": "CLOSE", "regime_at_entry": "BULL_TREND", "pnl_pct": -1.0},
            {"type": "CLOSE", "regime_at_entry": "CHOPPY",     "pnl_pct": 1.0},
            {"type": "CLOSE", "regime_at_entry": "CHOPPY",     "pnl_pct": 1.0},
        ]
        result = _compute_regime_winrate(trades)
        assert result["BULL_TREND"] == pytest.approx(0.5)
        assert result["CHOPPY"] == pytest.approx(1.0)


# ── learning/agent.py: prompt content checks ─────────────────────────────────

@pytest.mark.unit
def test_learning_agent_rsi_prompt_range_allows_momentum_entries():
    """Learning prompt must allow rsi_buy_max up to 80 so trending stocks aren't blocked."""
    import learning.agent as la
    import inspect
    src = inspect.getsource(la)
    assert "30–50" not in src, "Learning prompt must not restrict rsi_buy_max to 30–50"
    assert "25–40" not in src, "Learning prompt must not restrict rsi_buy_max_high_vol to 25–40"
    assert "40–80" in src or "40-80" in src, "Learning prompt must use range 40–80 for rsi_buy_max"


@pytest.mark.unit
def test_zero_trade_day_updates_trading_params(monkeypatch, tmp_path):
    """On a zero-trade day the learning agent must still update trading_params.json
    via deterministic single-param relaxation (no Claude call).
    """
    from unittest.mock import MagicMock, patch
    from utils.io_utils import save_json, load_json

    # Seed stale params with values below their upper bounds so relaxation fires on any weekday
    stale_params = {
        "entry_start_hour": 10, "entry_end_hour": 15,
        "rsi_buy_max": 70, "rsi_buy_max_high_vol": 65,
        "vwap_slope_min": -0.3, "high_vol_position_mult": 0.5,
        "stop_loss_pct": 3.0, "take_profit_pct": 5.0,
        "composite_min_normal": 20,  # below upper bound of 30 — ensures Tuesday relaxation fires
    }
    params_file = tmp_path / "trading_params.json"
    monkeypatch.setattr(config, "TRADING_PARAMS_FILE", params_file)
    monkeypatch.setattr(config, "LEARNING_ZERO_TRADE_RSI_STEP", 2)
    save_json(params_file, stale_params)

    with patch("learning.agent._load_todays_closed_trades", return_value=[]), \
         patch("anthropic.Anthropic") as MockAI:
        MockAI.return_value.messages.create.return_value = MagicMock()
        from learning.agent import run_learning_agent
        run_learning_agent()
        # Deterministic path — Claude must NOT be called
        MockAI.return_value.messages.create.assert_not_called()

    updated = load_json(params_file, {})
    # At least one param must have been relaxed
    changed = [k for k in stale_params if updated.get(k) != stale_params[k]]
    assert len(changed) >= 1, "Zero-trade day must update at least one trading param"


# ── P4-T4: _handle_zero_trade_day ─────────────────────────────────────────────

@pytest.mark.unit
def test_zero_trade_day_relaxes_one_param(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TRADING_PARAMS_FILE", tmp_path / "params.json")
    from utils.io_utils import save_json
    save_json(config.TRADING_PARAMS_FILE, {
        "rsi_buy_max": 72, "composite_min_normal": 20,
        "entry_start_hour": 10, "entry_end_hour": 15,
        "high_vol_position_mult": 0.5, "rsi_buy_max_high_vol": 65,
        "vwap_slope_min": -0.3,
    })
    from learning.agent import _handle_zero_trade_day
    _handle_zero_trade_day(params_path=config.TRADING_PARAMS_FILE)


@pytest.mark.unit
def test_zero_trade_day_changes_at_most_one_param(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TRADING_PARAMS_FILE", tmp_path / "params.json")
    monkeypatch.setattr(config, "LEARNING_ZERO_TRADE_RSI_STEP", 2)
    from utils.io_utils import save_json, load_json
    original = {"rsi_buy_max": 70, "composite_min_normal": 22, "vwap_slope_min": -0.3,
                "entry_start_hour": 10, "entry_end_hour": 15,
                "high_vol_position_mult": 0.5, "rsi_buy_max_high_vol": 65}
    save_json(config.TRADING_PARAMS_FILE, original)
    from learning.agent import _handle_zero_trade_day
    _handle_zero_trade_day(params_path=config.TRADING_PARAMS_FILE)
    updated = load_json(config.TRADING_PARAMS_FILE, {})
    changed = [k for k in original if updated.get(k) != original[k]]
    assert len(changed) <= 1, f"Too many params changed: {changed}"


# ── Plan 6: rolling trades + weight clamping + memory versioning ─────────────

@pytest.mark.unit
def test_learning_config_constants_exist():
    assert hasattr(config, "LEARNING_ROLLING_DAYS") and config.LEARNING_ROLLING_DAYS >= 3
    assert hasattr(config, "INDICATOR_WEIGHT_MAX_MULT") and config.INDICATOR_WEIGHT_MAX_MULT > 1.0
    assert hasattr(config, "INDICATOR_WEIGHT_MIN_MULT") and 0 < config.INDICATOR_WEIGHT_MIN_MULT < 1.0
    assert hasattr(config, "STRATEGY_MEMORY_BACKUP_MAX") and config.STRATEGY_MEMORY_BACKUP_MAX >= 3


@pytest.mark.unit
def test_load_rolling_trades_returns_last_n_days(monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from utils.io_utils import save_json
    ET = ZoneInfo("America/New_York")
    monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "journal.json")
    monkeypatch.setattr(config, "LEARNING_ROLLING_DAYS", 3)
    now = datetime.now(ET)
    trades = [
        {"type": "CLOSE", "symbol": "AAPL", "timestamp": (now - timedelta(days=1)).isoformat(), "pnl_pct": 1.5},
        {"type": "CLOSE", "symbol": "MSFT", "timestamp": (now - timedelta(days=2)).isoformat(), "pnl_pct": -0.8},
        {"type": "CLOSE", "symbol": "NVDA", "timestamp": (now - timedelta(days=5)).isoformat(), "pnl_pct": 2.1},
        {"type": "BUY",   "symbol": "AMD",  "timestamp": (now - timedelta(days=1)).isoformat(), "pnl_pct": None},
    ]
    save_json(config.TRADE_JOURNAL_FILE, trades)
    from learning.data import _load_rolling_trades
    result = _load_rolling_trades(days=3)
    symbols = [t["symbol"] for t in result]
    assert "AAPL" in symbols and "MSFT" in symbols
    assert "NVDA" not in symbols
    assert "AMD" not in symbols


@pytest.mark.unit
def test_load_rolling_trades_empty_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "journal.json")
    from learning.data import _load_rolling_trades
    result = _load_rolling_trades(days=5)
    assert result == []


@pytest.mark.unit
def test_indicator_weight_clamped_at_max_mult(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", tmp_path / "weights.json")
    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MAX_MULT", 2.0)
    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MIN_MULT", 0.3)
    from utils.io_utils import save_json, load_json
    defaults = {"rsi": 1.0, "macd": 1.0}
    save_json(config.INDICATOR_WEIGHTS_FILE, defaults)
    from learning.trade_analysis import _apply_weight_update
    _apply_weight_update({"rsi": 5.0}, defaults)
    result = load_json(config.INDICATOR_WEIGHTS_FILE, {})
    assert result["rsi"] <= defaults["rsi"] * config.INDICATOR_WEIGHT_MAX_MULT


@pytest.mark.unit
def test_indicator_weight_clamped_at_min_mult(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", tmp_path / "weights.json")
    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MAX_MULT", 2.0)
    monkeypatch.setattr(config, "INDICATOR_WEIGHT_MIN_MULT", 0.3)
    from utils.io_utils import save_json, load_json
    defaults = {"rsi": 1.0}
    save_json(config.INDICATOR_WEIGHTS_FILE, defaults)
    from learning.trade_analysis import _apply_weight_update
    _apply_weight_update({"rsi": 0.01}, defaults)
    result = load_json(config.INDICATOR_WEIGHTS_FILE, {})
    assert result["rsi"] >= defaults["rsi"] * config.INDICATOR_WEIGHT_MIN_MULT


@pytest.mark.unit
def test_strategy_memory_backup_created_on_write(monkeypatch, tmp_path):
    mem_file = tmp_path / "strategy_memory.md"
    mem_file.write_text("old rules")
    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", mem_file)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_BACKUP_MAX", 7)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_MAX_CHARS", 10000)
    from learning.agent import _write_strategy_memory
    _write_strategy_memory("new rules")
    bak_files = list(tmp_path.glob("strategy_memory.*.bak.md"))
    assert bak_files, "Expected at least one .bak file after write"
    assert mem_file.read_text() == "new rules"


@pytest.mark.unit
def test_strategy_memory_backup_pruned_to_max(monkeypatch, tmp_path):
    mem_file = tmp_path / "strategy_memory.md"
    mem_file.write_text("rules")
    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", mem_file)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_BACKUP_MAX", 3)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_MAX_CHARS", 10000)
    for i in range(5):
        (tmp_path / f"strategy_memory.2026-05-{10+i}.bak.md").write_text(f"backup {i}")
    from learning.agent import _write_strategy_memory
    _write_strategy_memory("new rules")
    bak_files = sorted(tmp_path.glob("strategy_memory.*.bak.md"))
    assert len(bak_files) <= config.STRATEGY_MEMORY_BACKUP_MAX


# ── Phase 3: ATR-TP multiplier grid search ───────────────────────────────────

@pytest.mark.unit
class TestTuneAtrTpMultiplier:
    """_tune_atr_tp_multiplier: grid-search for best-Sharpe ATR-TP multiplier."""

    @staticmethod
    def _trade(atr_pct: float, pnl_pct: float) -> dict:
        return {"atr_pct": atr_pct, "pnl_pct": pnl_pct}

    def test_returns_none_when_too_few_annotated_trades(self):
        from learning.trade_analysis import _tune_atr_tp_multiplier
        trades = [self._trade(2.0, 1.5) for _ in range(14)]
        assert _tune_atr_tp_multiplier(trades) is None

    def test_returns_none_when_no_atr_pct_field(self):
        from learning.trade_analysis import _tune_atr_tp_multiplier
        trades = [{"pnl_pct": 2.0} for _ in range(20)]
        assert _tune_atr_tp_multiplier(trades) is None

    def test_returns_float_with_enough_annotated_trades(self):
        from learning.trade_analysis import _tune_atr_tp_multiplier
        trades = [self._trade(2.0, 3.0) for _ in range(20)]
        result = _tune_atr_tp_multiplier(trades)
        assert isinstance(result, float)

    def test_grid_search_picks_best_sharpe(self):
        """atr_pct=4%, pnl=6%: mult=1.5 captures sim_tp=6% (first grid entry to reach max mean)."""
        from learning.trade_analysis import _tune_atr_tp_multiplier
        # mult=1.0  → sim_tp=4.0%, pnl=6.0 ≥ 4.0 → capped at 4.0, mean=4.0
        # mult=1.25 → sim_tp=5.0%, pnl=6.0 ≥ 5.0 → capped at 5.0, mean=5.0
        # mult=1.5  → sim_tp=6.0%, pnl=6.0 ≥ 6.0 → capped at 6.0, mean=6.0  ← wins (first)
        # mult≥1.75 → sim_tp>6.0%, pnl=6.0 < sim_tp → sim_pnl=6.0, mean=6.0  (ties, no update)
        trades = [self._trade(4.0, 6.0) for _ in range(20)]
        assert _tune_atr_tp_multiplier(trades) == pytest.approx(1.5)

    def test_result_clamped_to_config_bounds(self, monkeypatch):
        """Result is always clamped to [ATR_TP_MULT_MIN, ATR_TP_MULT_MAX]."""
        from learning.trade_analysis import _tune_atr_tp_multiplier
        monkeypatch.setattr(config, "ATR_TP_MULT_MIN", 1.5)
        # pnl_pct=1.0 never reaches ATR_TP_FLOOR_PCT=1.5%, so all mults give same outcome;
        # first grid entry (1.0) wins raw, then clamped up to ATR_TP_MULT_MIN=1.5
        trades = [self._trade(0.5, 1.0) for _ in range(20)]
        result = _tune_atr_tp_multiplier(trades)
        assert result >= config.ATR_TP_MULT_MIN
        assert result <= config.ATR_TP_MULT_MAX

    def test_falls_back_to_mean_when_std_zero(self):
        """std=0 across all trades must not raise; function returns a valid float."""
        from learning.trade_analysis import _tune_atr_tp_multiplier
        trades = [self._trade(0.3, 0.8) for _ in range(20)]
        result = _tune_atr_tp_multiplier(trades)
        assert isinstance(result, float)


@pytest.mark.unit
def test_atr_pct_preserved_in_rolling_trades(monkeypatch, tmp_path):
    """atr_pct field must survive _load_rolling_trades key-filtering."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from utils.io_utils import save_json
    ET = ZoneInfo("America/New_York")
    monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", tmp_path / "journal.json")
    now = datetime.now(ET)
    save_json(config.TRADE_JOURNAL_FILE, [{
        "type": "CLOSE", "symbol": "AAPL",
        "timestamp": (now - timedelta(days=1)).isoformat(),
        "pnl_pct": 2.5, "atr_pct": 3.2,
    }])
    from learning.data import _load_rolling_trades
    result = _load_rolling_trades(days=3)
    assert len(result) == 1
    assert result[0].get("atr_pct") == pytest.approx(3.2)


@pytest.mark.unit
def test_learning_agent_tunes_atr_multiplier(monkeypatch, tmp_path):
    """run_learning_agent writes atr_tp_multiplier to trading_params when rolling window has 15+ annotated trades."""
    import json as _json
    from datetime import datetime
    from unittest.mock import MagicMock, patch
    from utils.io_utils import save_json, load_json

    params_file = tmp_path / "trading_params.json"
    weights_file = tmp_path / "weights.json"
    journal_file = tmp_path / "journal.json"
    perf_log = tmp_path / "perf.md"
    memory_file = tmp_path / "strategy_memory.md"

    monkeypatch.setattr(config, "TRADING_PARAMS_FILE", params_file)
    monkeypatch.setattr(config, "INDICATOR_WEIGHTS_FILE", weights_file)
    monkeypatch.setattr(config, "TRADE_JOURNAL_FILE", journal_file)
    monkeypatch.setattr(config, "INDICATOR_PERFORMANCE_LOG", perf_log)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_FILE", memory_file)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_MAX_CHARS", 10000)
    monkeypatch.setattr(config, "STRATEGY_MEMORY_BACKUP_MAX", 3)

    save_json(params_file, {"atr_tp_multiplier": 2.0, "rsi_buy_max": 65})
    save_json(weights_file, {"rsi": 10.0, "macd": 10.0})

    from utils.constants import ET
    today = datetime.now(ET).date().isoformat()
    save_json(journal_file, [{
        "type": "CLOSE", "timestamp": f"{today}T11:00:00", "symbol": "AAPL",
        "pnl_pct": 2.0, "pnl_dollar": 100.0, "close_reason": "tp",
        "regime_at_entry": "NORMAL", "mode_at_entry": "DAY_TRADE",
        "entry_sentiment_score": 10, "atr_pct": 4.0,
    }])

    annotated = [{"atr_pct": 4.0, "pnl_pct": 6.0, "pnl_dollar": 100.0}] * 20

    analysis_json = _json.dumps({
        "total_trades": 1, "winning_trades": 1, "losing_trades": 0,
        "total_pnl_dollar": 100.0, "win_rate_pct": 100.0,
        "market_conditions": "trending", "regime_impact": "positive",
        "indicator_performance": {
            "rsi": {"accuracy_pct": 80, "weight_adjustment": 1, "notes": "good"}
        },
        "suggested_params": {
            "rsi_buy_max": 65, "entry_start_hour": 10, "entry_end_hour": 15,
            "rsi_buy_max_high_vol": 50, "vwap_slope_min": -0.3,
            "high_vol_position_mult": 0.5, "stop_loss_pct": 3.0, "take_profit_pct": 5.0,
        },
        "strategy_learnings": ["lesson"],
        "per_stock_notes": {"AAPL": "good"},
    })
    mock_analysis = MagicMock()
    mock_analysis.content = [MagicMock(text=analysis_json)]
    mock_analysis.usage = MagicMock(
        input_tokens=100, output_tokens=50,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    mock_memory = MagicMock()
    mock_memory.content = [MagicMock(text="# Memory\nrules")]
    mock_memory.usage = MagicMock(
        input_tokens=100, output_tokens=50,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )

    with patch("learning.agent._load_rolling_trades", return_value=annotated), \
         patch("anthropic.Anthropic") as MockAI, \
         patch("trading.risk_manager.record_usage"), \
         patch("macro.regime.load_macro_regime", return_value={
             "regime": "NORMAL", "vix_current": 20.0, "vix_90d_avg": 18.0,
             "spy_5day_return": 0.5, "health_score": 75,
         }), \
         patch("macro.regime.load_trading_params", return_value={"rsi_buy_max": 65}):
        MockAI.return_value.messages.create.side_effect = [mock_analysis, mock_memory]
        from learning.agent import run_learning_agent
        run_learning_agent()

    saved = load_json(params_file, {})
    assert "atr_tp_multiplier" in saved
    assert config.ATR_TP_MULT_MIN <= saved["atr_tp_multiplier"] <= config.ATR_TP_MULT_MAX


class TestAggregateTier:
    @pytest.mark.unit
    def test_tier_aggregation_win_rate(self):
        from learning.trade_analysis import _aggregate_by_tier
        trades = [
            {"quality_tier": "HIGH", "pnl_pct": 2.5, "atr_pct": 1.2, "exit_reason": "TP"},
            {"quality_tier": "HIGH", "pnl_pct": -1.0, "atr_pct": 1.1, "exit_reason": "SL"},
            {"quality_tier": "MEDIUM", "pnl_pct": 0.8, "atr_pct": 0.9, "exit_reason": "TP"},
        ]
        result = _aggregate_by_tier(trades)
        assert result["HIGH"]["win_rate"] == pytest.approx(0.5)
        assert result["HIGH"]["count"] == 2
        assert result["MEDIUM"]["count"] == 1

    @pytest.mark.unit
    def test_force_close_rate_computed(self):
        from learning.trade_analysis import _aggregate_by_tier
        trades = [
            {"quality_tier": "LOW", "pnl_pct": 0.3, "atr_pct": 0.8, "exit_reason": "FORCE_CLOSE"},
            {"quality_tier": "LOW", "pnl_pct": 1.0, "atr_pct": 0.9, "exit_reason": "TP"},
        ]
        result = _aggregate_by_tier(trades)
        assert result["LOW"]["force_close_rate"] == pytest.approx(0.5)

    @pytest.mark.unit
    def test_most_constrained_param(self):
        from learning.trade_analysis import _most_constrained_param, _ZERO_TRADE_BOUNDS
        params = {}
        for key, (lo, hi) in _ZERO_TRADE_BOUNDS.items():
            params[key] = hi  # all at max restriction
        result = _most_constrained_param(params)
        assert result in _ZERO_TRADE_BOUNDS

    @pytest.mark.unit
    def test_weights_sum_to_100_after_clamping(self, monkeypatch):
        """Weights must sum to 100 even after per-weight clamping."""
        import config
        monkeypatch.setattr(config, "INDICATOR_WEIGHT_MIN_MULT", 0.3)
        monkeypatch.setattr(config, "INDICATOR_WEIGHT_MAX_MULT", 1.2)  # tight ceiling triggers clamp

        # 10 equal weights; push one up so it gets clamped
        weights = {f"ind_{i}": 10.0 for i in range(10)}
        analysis = {
            "indicator_performance": {
                "ind_0": {"weight_adjustment": 5.0},   # 15 → clamped to 12
                "ind_1": {"weight_adjustment": -5.0},  # 5 → ok
            }
        }

        new_weights = dict(weights)
        for indicator, data in analysis.get("indicator_performance", {}).items():
            if indicator in new_weights:
                new_weights[indicator] = max(1.0, new_weights[indicator] + float(data.get("weight_adjustment", 0)))

        total = sum(new_weights.values())
        new_weights = {k: round(v / total * 100, 2) for k, v in new_weights.items()}

        equal_share = 100.0 / len(new_weights)
        lo = equal_share * config.INDICATOR_WEIGHT_MIN_MULT
        hi = equal_share * config.INDICATOR_WEIGHT_MAX_MULT
        for key in new_weights:
            new_weights[key] = round(max(lo, min(hi, new_weights[key])), 4)

        # Re-normalise (the fix being tested)
        _post_clamp_total = sum(new_weights.values())
        if _post_clamp_total > 0:
            new_weights = {k: round(v / _post_clamp_total * 100, 4) for k, v in new_weights.items()}

        total_after = sum(new_weights.values())
        assert abs(total_after - 100.0) < 0.05, (
            f"Weights sum to {total_after:.4f}, expected ~100"
        )
