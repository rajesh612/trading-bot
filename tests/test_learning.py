"""
Learning agent unit tests.
Run with: pytest tests/test_learning.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import pytest


# ── learning_agent: threshold clamping ───────────────────────────────────────

@pytest.mark.unit
class TestClampThreshold:
    """learning_agent._clamp_threshold hard-bound clamping."""

    def test_within_bounds_unchanged(self):
        from learning_agent import _clamp_threshold
        assert _clamp_threshold("stop_loss_pct", 4.0) == 4.0

    def test_below_lower_bound_clamped_up(self):
        from learning_agent import _clamp_threshold
        # stop_loss_pct lower bound is 1.5
        result = _clamp_threshold("stop_loss_pct", 0.5)
        assert result == 1.5

    def test_above_upper_bound_clamped_down(self):
        from learning_agent import _clamp_threshold
        # stop_loss_pct upper bound is 12.0
        result = _clamp_threshold("stop_loss_pct", 20.0)
        assert result == 12.0

    def test_integer_key_returns_int(self):
        from learning_agent import _clamp_threshold
        # rsi_buy_max bounds: (30, 80) — both int
        result = _clamp_threshold("rsi_buy_max", 45)
        assert isinstance(result, int)
        assert result == 45

    def test_unknown_key_returns_value_unchanged(self):
        from learning_agent import _clamp_threshold
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
        from learning_agent import _aggregate_by_regime
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
        from learning_agent import _aggregate_by_regime
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
        from learning_agent import _aggregate_by_regime
        trades = [
            self._make_trade("A", 1.0, 10.0, mode="DAY_TRADE"),
            self._make_trade("B", 2.0, 20.0, mode="HYBRID"),
        ]
        result = _aggregate_by_regime(trades)
        assert result["by_mode"]["DAY_TRADE"]["count"] == 1
        assert result["by_mode"]["HYBRID"]["count"] == 1

    def test_missing_regime_falls_back_to_unknown(self):
        from learning_agent import _aggregate_by_regime
        trades = [{"symbol": "X", "pnl_pct": 1.0, "pnl_dollar": 10.0}]
        result = _aggregate_by_regime(trades)
        assert "UNKNOWN" in result["by_regime"]


# ── learning_agent: loss source classification ────────────────────────────────

@pytest.mark.unit
class TestClassifyLossSource:
    """learning_agent._classify_loss_source classification priority."""

    def test_earnings_close_reason(self):
        from learning_agent import _classify_loss_source
        trade = {"close_reason": "earnings_gap_down", "pnl_pct": -5.0, "entry_indicators": {}}
        assert _classify_loss_source(trade) == "earnings_surprise"

    def test_crisis_regime_at_entry(self):
        from learning_agent import _classify_loss_source
        trade = {"close_reason": "monitor_stop_loss", "regime_at_entry": "CRISIS",
                 "pnl_pct": -3.0, "entry_indicators": {}}
        assert _classify_loss_source(trade) == "market_structure"

    def test_flash_crash_close_reason(self):
        from learning_agent import _classify_loss_source
        trade = {"close_reason": "flash_crash", "pnl_pct": -4.0, "entry_indicators": {}}
        assert _classify_loss_source(trade) == "market_structure"

    def test_momentum_exhaustion_at_entry(self):
        from learning_agent import _classify_loss_source
        trade = {
            "close_reason": "monitor_stop_loss",
            "regime_at_entry": "NORMAL",
            "entry_indicators": {"momentum_exhaustion": True},
            "pnl_pct": -2.0,
        }
        assert _classify_loss_source(trade) == "momentum_exhaustion"

    def test_negative_sentiment_sector_narrative(self):
        from learning_agent import _classify_loss_source
        trade = {
            "close_reason": "monitor_stop_loss",
            "regime_at_entry": "NORMAL",
            "entry_indicators": {},
            "entry_sentiment_score": -30,
            "pnl_pct": -1.5,
        }
        assert _classify_loss_source(trade) == "sector_narrative"

    def test_unknown_when_no_signal(self):
        from learning_agent import _classify_loss_source
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
    def test_load_todays_closed_trades_missing_file(self, tmp_path):
        import config
        config.TRADE_JOURNAL_FILE = tmp_path / "trade_journal.json"
        from learning_agent import _load_todays_closed_trades
        assert _load_todays_closed_trades() == []

    def test_load_todays_closed_trades_filters_by_today(self, tmp_path):
        import config, json
        from datetime import date
        config.TRADE_JOURNAL_FILE = tmp_path / "trade_journal.json"
        today = date.today().isoformat()
        journal = [
            {"type": "CLOSE", "timestamp": f"{today}T10:30:00", "symbol": "NVDA",
             "pnl_pct": 2.0, "pnl_dollar": 100.0, "close_reason": "tp"},
            {"type": "CLOSE", "timestamp": "2020-01-01T10:30:00", "symbol": "AAPL",
             "pnl_pct": -1.0, "pnl_dollar": -50.0, "close_reason": "sl"},
            {"type": "BUY", "timestamp": f"{today}T09:30:00", "symbol": "AMD"},
        ]
        config.TRADE_JOURNAL_FILE.write_text(json.dumps(journal))
        from learning_agent import _load_todays_closed_trades
        result = _load_todays_closed_trades()
        assert len(result) == 1
        assert result[0]["symbol"] == "NVDA"

    def test_save_and_load_weights(self, tmp_path):
        import config
        config.INDICATOR_WEIGHTS_FILE = tmp_path / "weights.json"
        from learning_agent import _save_weights, _load_weights
        weights = {"RSI": 20.0, "MACD": 15.0}
        _save_weights(weights)
        loaded = _load_weights()
        assert loaded == weights

    def test_save_trading_params_adds_last_updated(self, tmp_path):
        import config, json
        from datetime import date
        config.TRADING_PARAMS_FILE = tmp_path / "trading_params.json"
        from learning_agent import _save_trading_params
        _save_trading_params({"stop_loss_pct": 4.0})
        saved = json.loads(config.TRADING_PARAMS_FILE.read_text())
        assert saved["last_updated"] == date.today().isoformat()


# ── macro_regime: load_trading_params ────────────────────────────────────────

@pytest.mark.unit
class TestLoadTradingParamsInMacroRegime:
    def test_returns_config_defaults_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", tmp_path / "trading_params.json")
        from macro_regime import load_trading_params
        params = load_trading_params()
        import config as cfg
        assert params["entry_start_hour"] == cfg.ENTRY_START_HOUR
        assert params["rsi_buy_max"] == cfg.RSI_BUY_MAX
        assert params["stop_loss_pct"] == cfg.STOP_LOSS_PCT

    def test_file_values_override_defaults(self, tmp_path, monkeypatch):
        params_file = tmp_path / "trading_params.json"
        params_file.write_text('{"rsi_buy_max": 60, "entry_start_hour": 11}')
        monkeypatch.setattr("config.TRADING_PARAMS_FILE", params_file)
        from macro_regime import load_trading_params
        params = load_trading_params()
        assert params["rsi_buy_max"] == 60
        assert params["entry_start_hour"] == 11

    def test_load_macro_regime_returns_regime_key(self, tmp_path, monkeypatch):
        from macro_regime import load_macro_regime
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
        from datetime import date
        ts = date.today().isoformat() if today else "2020-01-01"
        return {"action": action, "pnl_pct": pnl_pct, "timestamp": f"{ts}T10:00:00"}

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
            "action": action,
            "regime_at_entry": regime,
            "pnl_pct": pnl_pct,
        }

    def test_empty_trades_returns_empty(self):
        """Empty trade list should return empty dict."""
        from learning_agent import _compute_regime_winrate
        assert _compute_regime_winrate([]) == {}

    def test_single_win(self):
        """Single winning trade should have 100% win rate."""
        from learning_agent import _compute_regime_winrate
        result = _compute_regime_winrate([self._trade("BULL_TREND", 2.0)])
        assert result == {"BULL_TREND": pytest.approx(1.0)}

    def test_single_loss(self):
        """Single losing trade should have 0% win rate."""
        from learning_agent import _compute_regime_winrate
        result = _compute_regime_winrate([self._trade("CHOPPY", -1.5)])
        assert result == {"CHOPPY": pytest.approx(0.0)}

    def test_mixed_regime_winrate(self):
        """Multiple trades across regimes should compute per-regime rates."""
        from learning_agent import _compute_regime_winrate
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
        from learning_agent import _compute_regime_winrate
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
        from learning_agent import _clamp_threshold
        result = _clamp_threshold("stop_loss_pct", 4.0)
        assert isinstance(result, float)

    def test_clamp_at_lower_boundary_returns_lower(self):
        from learning_agent import _clamp_threshold
        # stop_loss_pct lower bound is 1.5 — pass exactly 1.5
        result = _clamp_threshold("stop_loss_pct", 1.5)
        assert result == pytest.approx(1.5)

    def test_clamp_at_upper_boundary_returns_upper(self):
        from learning_agent import _clamp_threshold
        # stop_loss_pct upper bound is 12.0 — pass exactly 12.0
        result = _clamp_threshold("stop_loss_pct", 12.0)
        assert result == pytest.approx(12.0)

    def test_clamp_int_key_below_lower_returns_int(self):
        from learning_agent import _clamp_threshold
        # swing_max_days bounds: (3, 7) — both int
        result = _clamp_threshold("swing_max_days", 1)
        assert result == 3
        assert isinstance(result, int)

    def test_clamp_invalid_value_returns_unchanged(self):
        from learning_agent import _clamp_threshold
        result = _clamp_threshold("stop_loss_pct", "not_a_number")
        assert result == "not_a_number"

    # ── _aggregate_by_regime pnl totals ───────────────────────────────────

    def test_aggregate_total_pnl_dollar_summed_correctly(self):
        from learning_agent import _aggregate_by_regime
        trades = [
            self._trade("A", pnl_pct=2.0, pnl_dollar=100.0, regime="NORMAL"),
            self._trade("B", pnl_pct=-1.0, pnl_dollar=-40.0, regime="NORMAL"),
        ]
        result = _aggregate_by_regime(trades)
        normal = result["by_regime"]["NORMAL"]
        assert normal["total_pnl_dollar"] == pytest.approx(60.0)

    def test_aggregate_empty_trades_returns_empty_dicts(self):
        from learning_agent import _aggregate_by_regime
        result = _aggregate_by_regime([])
        assert result["by_regime"] == {}
        assert result["by_mode"] == {}

    def test_aggregate_avg_pnl_pct_calculated(self):
        from learning_agent import _aggregate_by_regime
        trades = [
            self._trade("A", pnl_pct=4.0, pnl_dollar=80.0, regime="BULL_TREND"),
            self._trade("B", pnl_pct=2.0, pnl_dollar=40.0, regime="BULL_TREND"),
        ]
        result = _aggregate_by_regime(trades)
        bt = result["by_regime"]["BULL_TREND"]
        assert bt["avg_pnl_pct"] == pytest.approx(3.0)

    # ── _classify_loss_source additional branches ─────────────────────────

    def test_classify_fundamental_close_reason(self):
        from learning_agent import _classify_loss_source
        trade = self._trade(close_reason="guidance_cut", pnl_pct=-3.0)
        assert _classify_loss_source(trade) == "fundamental"

    def test_classify_macro_pressure_high_shock_score(self):
        from learning_agent import _classify_loss_source
        trade = self._trade(
            close_reason="monitor_stop_loss",
            pnl_pct=-2.0,
            entry_indicators={"entry_macro_shock_score": 65},
        )
        assert _classify_loss_source(trade) == "macro_pressure"

    def test_classify_crash_recovery_is_market_structure(self):
        from learning_agent import _classify_loss_source
        trade = self._trade(close_reason="sl_hit", pnl_pct=-4.0)
        trade["regime_at_entry"] = "CRASH_RECOVERY"
        assert _classify_loss_source(trade) == "market_structure"

    # ── _compute_regime_winrate ────────────────────────────────────────────

    def test_winrate_ignores_trades_without_regime(self):
        from learning_agent import _compute_regime_winrate
        trades = [
            {"action": "CLOSE", "pnl_pct": 2.0},           # no regime_at_entry
            {"action": "CLOSE", "regime_at_entry": "NORMAL", "pnl_pct": 3.0},  # counted
        ]
        result = _compute_regime_winrate(trades)
        assert "NORMAL" in result
        assert result["NORMAL"] == pytest.approx(1.0)
        assert len(result) == 1  # trade without regime not counted

    def test_winrate_multiple_regimes_independent(self):
        from learning_agent import _compute_regime_winrate
        trades = [
            {"action": "CLOSE", "regime_at_entry": "BULL_TREND", "pnl_pct": 2.0},
            {"action": "CLOSE", "regime_at_entry": "BULL_TREND", "pnl_pct": -1.0},
            {"action": "CLOSE", "regime_at_entry": "CHOPPY",     "pnl_pct": 1.0},
            {"action": "CLOSE", "regime_at_entry": "CHOPPY",     "pnl_pct": 1.0},
        ]
        result = _compute_regime_winrate(trades)
        assert result["BULL_TREND"] == pytest.approx(0.5)
        assert result["CHOPPY"] == pytest.approx(1.0)
