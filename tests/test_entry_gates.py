"""
Entry-gate unit tests (signals/entry_gates.py).
Run with: pytest tests/test_entry_gates.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import types
from datetime import datetime

import pytest

import config


# ── Shared helpers ────────────────────────────────────────────────────────────
# Duplicated from test_signals.py intentionally — avoids cross-file helper deps.


def _gates_iv(**overrides):
    """Build a SimpleNamespace that passes every hard gate by default."""
    base = dict(
        current_price=100.0,
        vwap=99.0,
        vwap_std2_upper=110.0,
        stop_hunt_zone=False,
        parabolic=False,
        momentum_exhaustion=False,
        bot_herding_score=0,
        squeeze_setup=False,
        rsi=50.0,
        stoch_d=50.0,
        prev_close=100.0,
        relative_volume=1.0,
        or_position="outside",
        composite_score=80,  # passes ENTRY_LATE_COMPOSITE_MIN gate
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _gates_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))


def _patch_cals(monkeypatch):
    import calendars.earnings as ec
    import macro.economic_calendar as ecc
    monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
    monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))


# ── signals.entry_gates ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestEntryGates:
    """Tests for check_all_gates — the unified hard+soft gate runner."""

    def _now_allowed(self) -> datetime:
        """Return a tz-aware ET datetime in the middle of the trading window."""
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    def _now_blocked_time(self) -> datetime:
        """Return a tz-aware ET datetime outside the trading window (too early)."""
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 9, 45, 0, tzinfo=ZoneInfo("America/New_York"))

    @staticmethod
    def _iv(**overrides):
        """Build a minimal IndicatorValues-compatible namespace that passes all hard gates."""
        base = dict(
            current_price=100.0,
            vwap=99.0,
            vwap_std2_upper=110.0,   # price (100) < upper (110) → no overextension
            stop_hunt_zone=False,
            parabolic=False,
            momentum_exhaustion=False,
            bot_herding_score=0,
            squeeze_setup=False,
            rsi=50.0,       # below RSI_BUY_MAX (72)
            stoch_d=50.0,   # below STOCH_HARD_VETO_THRESHOLD (88)
            prev_close=100.0,  # no gap-up (100 / 100 = 1.0 = 0% gap)
            relative_volume=1.0,
            or_position="outside",
            composite_score=80,  # passes ENTRY_LATE_COMPOSITE_MIN gate
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _patch_calendars(self, monkeypatch):
        """Suppress real calendar I/O so gates don't raise or block."""
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    def test_time_gate_blocks_outside_window(self, monkeypatch):
        """Gate 1 fires before 10:00 ET."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, set(), None,
            self._now_blocked_time(),
        )
        assert result.passed is False
        assert "time" in result.reason

    def test_daily_closed_blocks(self, monkeypatch):
        """Gate 3 fires when symbol is in daily_closed set."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, {"NVDA"}, None,
            self._now_allowed(),
        )
        assert result.passed is False
        assert "closed today" in result.reason

    def test_position_already_open_blocks(self, monkeypatch):
        """Gate 3 fires when symbol already has an open position."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {"NVDA": {}}, set(), None,
            self._now_allowed(),
        )
        assert result.passed is False
        assert "already open" in result.reason

    def test_event_risk_window_blocks(self, monkeypatch):
        """Gate 5 fires during an economic event risk window."""
        import macro.economic_calendar as ecc
        import calendars.earnings as ec
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (True, "Non-Farm Payrolls"))
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, set(), None,
            self._now_allowed(),
        )
        assert result.passed is False
        assert "event risk" in result.reason

    def test_vwap_overextension_blocks(self, monkeypatch):
        """Gate 7 fires when price is above VWAP+2σ."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        # price=120 > vwap_std2_upper=100 → overextended
        iv = self._iv(current_price=120.0, vwap_std2_upper=100.0)
        result = check_all_gates(
            "NVDA", iv, {}, set(), None,
            self._now_allowed(),
        )
        assert result.passed is False
        assert "VWAP" in result.reason

    def test_parabolic_blocks(self, monkeypatch):
        """Gate 8 fires when parabolic flag is set and PARABOLIC_ENTRY_BLOCK is True."""
        import config
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(parabolic=True)
        result = check_all_gates(
            "NVDA", iv, {}, set(), None,
            self._now_allowed(),
        )
        assert result.passed is False
        assert "parabolic" in result.reason

    def test_all_gates_pass_returns_true(self, monkeypatch):
        """All gates clear → GateResult(passed=True, reason='')."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", True)
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, set(), None,
            self._now_allowed(),
        )
        assert result.passed is True
        assert result.reason == ""

    def test_soft_rs_gate_passes_with_flag(self, monkeypatch):
        """Weak RS ratio → passes but adds rs_weak flag."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", False)
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", False)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, set(), None,
            self._now_allowed(),
            spy_ret5d=10.0, symbol_ret5d=-5.0,  # badly underperforming
        )
        assert result.passed is True
        assert any("rs_weak" in f for f in result.flags)


# ── signals.entry_gates: integration (multi-gate sequences) ──────────────────

@pytest.mark.unit
class TestCheckAllGates:
    """Integration tests: verifies gate priority ordering and multi-gate behavior."""

    def _now_open(self) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    def _now_closed(self) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 9, 40, 0, tzinfo=ZoneInfo("America/New_York"))

    @staticmethod
    def _iv(**overrides):
        base = dict(
            current_price=100.0,
            vwap=99.0,
            vwap_std2_upper=110.0,
            stop_hunt_zone=False,
            parabolic=False,
            momentum_exhaustion=False,
            bot_herding_score=0,
            squeeze_setup=False,
            rsi=50.0,
            stoch_d=50.0,
            prev_close=100.0,
            relative_volume=1.0,
            or_position="outside",
            composite_score=80,  # passes ENTRY_LATE_COMPOSITE_MIN gate
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _patch_calendars(self, monkeypatch):
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    # ── Gate ordering ─────────────────────────────────────────────────────────

    def test_stop_hunt_fires_before_vwap_check(self, monkeypatch):
        """Stop-hunt (6) is checked before VWAP overextension (7)."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(stop_hunt_zone=True, current_price=120.0, vwap_std2_upper=100.0)
        result = check_all_gates("NVDA", iv, {}, set(), None, self._now_open())
        assert result.passed is False
        assert "stop-hunt" in result.reason            # not VWAP message

    def test_stop_hunt_not_vetoed_in_bull_trend(self, monkeypatch):
        """Stop-hunt zone is a soft flag (not hard veto) in BULL_TREND — momentum regime."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", False)
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", False)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(stop_hunt_zone=True, rsi=55.0, stoch_d=50.0)
        result = check_all_gates("IBM", iv, {}, set(), None, self._now_open(),
                                 regime="BULL_TREND")
        assert result.passed is True

    def test_stop_hunt_still_vetoed_in_choppy(self, monkeypatch):
        """Stop-hunt zone remains a hard veto in CHOPPY — mean-reversion risk."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", False)
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", False)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(stop_hunt_zone=True, rsi=55.0, stoch_d=50.0)
        result = check_all_gates("IBM", iv, {}, set(), None, self._now_open(),
                                 regime="CHOPPY", mode="SWING")
        assert result.passed is False
        assert "stop-hunt" in result.reason

    def test_vwap_gate_fires_before_parabolic(self, monkeypatch):
        """VWAP overextension (7) is checked before parabolic (8)."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", True)
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(current_price=120.0, vwap_std2_upper=100.0, parabolic=True)
        result = check_all_gates("NVDA", iv, {}, set(), None, self._now_open())
        assert result.passed is False
        assert "VWAP" in result.reason                 # not parabolic

    # ── Hard gate short-circuits soft gates ───────────────────────────────────

    def test_hard_gate_produces_no_flags(self, monkeypatch):
        """When a hard gate blocks, flags list is empty (soft gates never run)."""
        import config
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        # Parabolic hard veto + weak RS (would be soft flag if we got there)
        iv = self._iv(parabolic=True)
        result = check_all_gates(
            "NVDA", iv, {}, set(), None,
            self._now_open(),
            spy_ret5d=10.0, symbol_ret5d=-5.0,         # weak RS — soft gate
        )
        assert result.passed is False
        assert result.flags == ()                      # no soft flags collected

    # ── Soft gates accumulate ─────────────────────────────────────────────────

    def test_multiple_soft_flags_accumulate(self, monkeypatch):
        """Weak RS + unavailable MTF bars both add flags; symbol still passes."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", False)
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", False)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, set(),
            None,                                      # None → mtf_unavailable flag
            self._now_open(),
            spy_ret5d=10.0, symbol_ret5d=-5.0,         # weak RS flag
        )
        assert result.passed is True
        assert len(result.flags) >= 1                  # at least rs_weak

    def test_clean_symbol_no_flags(self, monkeypatch):
        """Fully clean symbol — passed=True, reason='', flags=()."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", True)
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, set(), None,
            self._now_open(),
        )
        assert result.passed is True
        assert result.reason == ""
        assert result.flags == ()

    # ── Immutability ──────────────────────────────────────────────────────────

    def test_gate_result_is_frozen(self, monkeypatch):
        """GateResult is a frozen dataclass — mutation raises AttributeError."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, set(), None,
            self._now_open(),
        )
        with pytest.raises((AttributeError, TypeError)):
            result.passed = False  # type: ignore[misc]

    # ── Specific gate reasons ─────────────────────────────────────────────────

    def test_gate_result_reason_populated_on_block(self, monkeypatch):
        """Every blocking gate populates reason with a non-empty string."""
        import config
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(parabolic=True), {}, set(), None,
            self._now_open(),
        )
        assert result.passed is False
        assert len(result.reason) > 0


@pytest.mark.unit
class TestRsiStochGates:
    """RSI and Stochastic hard gates in check_all_gates()."""

    def _now(self) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    @staticmethod
    def _iv(**overrides):
        base = dict(
            current_price=100.0,
            vwap=99.0,
            vwap_std2_upper=110.0,
            stop_hunt_zone=False,
            parabolic=False,
            momentum_exhaustion=False,
            bot_herding_score=0,
            squeeze_setup=False,
            rsi=50.0,
            stoch_d=50.0,
            prev_close=100.0,
            relative_volume=1.0,
            or_position="outside",
            composite_score=80,  # passes ENTRY_LATE_COMPOSITE_MIN gate
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _patch_calendars(self, monkeypatch):
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    def test_rsi_above_threshold_blocks(self, monkeypatch):
        """RSI > RSI_BUY_MAX fires the gate."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "AAPL", self._iv(rsi=73.0), {}, set(), None,
            self._now(),
        )
        assert result.passed is False
        assert "RSI" in result.reason

    def test_rsi_at_threshold_passes(self, monkeypatch):
        """RSI exactly at threshold passes — gate is strictly greater-than."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "AAPL", self._iv(rsi=72.0), {}, set(), None,
            self._now(),
        )
        assert result.passed is True

    def test_stoch_above_threshold_blocks(self, monkeypatch):
        """Stochastic %D > STOCH_HARD_VETO_THRESHOLD fires the gate."""
        self._patch_calendars(monkeypatch)
        import config
        from signals.entry_gates import check_all_gates
        above_threshold = config.STOCH_HARD_VETO_THRESHOLD + 1.0
        result = check_all_gates(
            "NVDA", self._iv(stoch_d=above_threshold), {}, set(), None,
            self._now(),
        )
        assert result.passed is False
        assert "Stochastic" in result.reason or "stoch" in result.reason.lower()

    def test_healthy_rsi_and_stoch_passes(self, monkeypatch):
        """RSI=60, stoch_d=70 — both below thresholds — gate passes."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "MSFT", self._iv(rsi=60.0, stoch_d=70.0), {}, set(), None,
            self._now(),
        )
        assert result.passed is True


@pytest.mark.unit
class TestGapUpGate:
    """Gate 12: gap-up skip — blocks entries when current price > prev_close * (1 + GAP_SKIP_PCT)."""

    def _now(self) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    @staticmethod
    def _iv(**overrides):
        """Build IndicatorValues-compatible namespace with all hard gates passing."""
        base = dict(
            current_price=100.0,
            vwap=99.0,
            vwap_std2_upper=110.0,
            stop_hunt_zone=False,
            parabolic=False,
            momentum_exhaustion=False,
            bot_herding_score=0,
            squeeze_setup=False,
            rsi=50.0,
            stoch_d=50.0,
            prev_close=100.0,
            relative_volume=1.0,
            or_position="outside",
            composite_score=80,  # passes ENTRY_LATE_COMPOSITE_MIN gate
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _patch_calendars(self, monkeypatch):
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    def test_gap_up_skip_blocks_entry_when_gap_exceeds_threshold(self, monkeypatch):
        """prev_close=100, current_price=104 (4% > 3%) → gate fails with message."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        # prev_close=100, current=104 → 4% gap up, exceeds 3% threshold
        result = check_all_gates(
            "TEST", self._iv(prev_close=100.0, current_price=104.0), {}, set(), None,
            self._now(),
        )
        assert result.passed is False
        assert "gap-up skip" in result.reason

    def test_gap_up_skip_allows_entry_when_gap_within_threshold(self, monkeypatch):
        """prev_close=100, current_price=102 (2% < 3%) → gate passes."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        # prev_close=100, current=102 → 2% gap up, within 3% threshold
        result = check_all_gates(
            "TEST", self._iv(prev_close=100.0, current_price=102.0), {}, set(), None,
            self._now(),
        )
        assert result.passed is True

    def test_gap_up_skip_bypassed_when_prev_close_zero(self, monkeypatch):
        """prev_close=0.0 → gate is skipped entirely (doesn't block)."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        # prev_close=0 → gate check skipped, should allow entry
        result = check_all_gates(
            "TEST", self._iv(prev_close=0.0, current_price=100.0), {}, set(), None,
            self._now(),
        )
        assert result.passed is True


# ── signals.entry_gates.effective_composite_gate ─────────────────────────────

@pytest.mark.unit
class TestEffectiveCompositeGate:
    """effective_composite_gate is the single source of truth for composite thresholds."""

    def _iv(self, *, squeeze_setup=False, bot_herding_score=0):
        from unittest.mock import MagicMock
        iv = MagicMock()
        iv.squeeze_setup = squeeze_setup
        iv.bot_herding_score = bot_herding_score
        return iv

    def test_high_vol_regime_returns_35(self, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_HIGH_VOL", 35)
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "HIGH_VOL") == 35

    def test_defensive_regime_returns_35(self, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_HIGH_VOL", 35)
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "DEFENSIVE") == 35

    def test_squeeze_setup_returns_12(self):
        from signals.entry_gates import effective_composite_gate
        iv = self._iv(squeeze_setup=True)
        assert effective_composite_gate(iv, "NORMAL") == 12

    def test_herding_returns_25(self):
        from signals.entry_gates import effective_composite_gate
        iv = self._iv(bot_herding_score=60)  # BOT_HERD_ALERT_SCORE = 60
        assert effective_composite_gate(iv, "NORMAL") == 25

    def test_choppy_returns_35(self, monkeypatch):
        monkeypatch.setattr(config, "CHOPPY_COMPOSITE_GATE", 35)
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "CHOPPY") == 35

    def test_normal_returns_30(self, monkeypatch):
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 30)
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "NORMAL") == 30

    def test_high_vol_overrides_squeeze(self, monkeypatch):
        """HIGH_VOL safety takes priority over squeeze setup."""
        monkeypatch.setattr(config, "COMPOSITE_MIN_HIGH_VOL", 35)
        from signals.entry_gates import effective_composite_gate
        iv = self._iv(squeeze_setup=True)
        assert effective_composite_gate(iv, "HIGH_VOL") == 35

    def test_normal_regime_uses_params_composite_min(self):
        """params['composite_min_normal'] overrides config.COMPOSITE_MIN_NORMAL in NORMAL."""
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        params = {"composite_min_normal": 18}
        assert effective_composite_gate(iv, "NORMAL", params=params) == 18

    def test_normal_regime_falls_back_to_config_when_no_params(self):
        """Without params, normal regime returns config.COMPOSITE_MIN_NORMAL."""
        import config
        from signals.entry_gates import effective_composite_gate
        iv = self._iv()
        assert effective_composite_gate(iv, "NORMAL") == config.COMPOSITE_MIN_NORMAL

    @pytest.mark.unit
    def test_effective_gate_bull_trend_returns_45(self, monkeypatch):
        """BULL_TREND regime uses COMPOSITE_MIN_BULL_TREND (45), not NORMAL (50)."""
        monkeypatch.setattr(config, "COMPOSITE_MIN_BULL_TREND", 45)
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        from signals.entry_gates import effective_composite_gate
        iv = self._iv(squeeze_setup=False, bot_herding_score=0)
        result = effective_composite_gate(iv, "BULL_TREND")
        assert result == 45

    @pytest.mark.unit
    def test_effective_gate_bull_trend_not_normal(self, monkeypatch):
        """BULL_TREND must not fall through to NORMAL threshold."""
        monkeypatch.setattr(config, "COMPOSITE_MIN_BULL_TREND", 45)
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        from signals.entry_gates import effective_composite_gate
        iv = self._iv(squeeze_setup=False, bot_herding_score=0)
        normal_gate = effective_composite_gate(iv, "NORMAL")
        bull_gate   = effective_composite_gate(iv, "BULL_TREND")
        assert bull_gate < normal_gate

    @pytest.mark.unit
    def test_stale_guard_adds_composite_penalty(self, monkeypatch):
        """Stale regime guard adds DATA_COMPOSITE_PENALTY['regime'] to threshold."""
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        from signals.entry_gates import effective_composite_gate
        from utils.data_health import StalenessGuard
        stale_guard = StalenessGuard(
            composite_penalty=10, auto_execute_allowed=False,
            t2_allowed=False, swing_fallback_regime="NORMAL",
            can_open=True, claude_warnings=(), stale_sources=frozenset({"regime"}),
        )
        iv = self._iv()   # use existing _iv() helper in the test class
        result = effective_composite_gate(iv, "NORMAL", staleness_guard=stale_guard)
        assert result == 60   # 50 base + 10 penalty

    @pytest.mark.unit
    def test_fresh_guard_adds_no_penalty(self, monkeypatch):
        """FRESH_GUARD adds zero penalty — baseline unchanged."""
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        from signals.entry_gates import effective_composite_gate
        from utils.data_health import FRESH_GUARD
        iv = self._iv()
        result = effective_composite_gate(iv, "NORMAL", staleness_guard=FRESH_GUARD)
        assert result == 50   # no penalty


# ── Module-level gate config and stoch threshold tests ───────────────────────

@pytest.mark.unit
def test_stoch_hard_veto_threshold_is_97():
    assert config.STOCH_HARD_VETO_THRESHOLD == 97


@pytest.mark.unit
def test_stoch_soft_flag_threshold_exists_and_is_below_hard_veto():
    assert hasattr(config, "STOCH_SOFT_FLAG_THRESHOLD")
    assert config.STOCH_SOFT_FLAG_THRESHOLD < config.STOCH_HARD_VETO_THRESHOLD


@pytest.mark.unit
def test_daily_loss_warn_pct_is_below_max_daily_loss():
    assert hasattr(config, "DAILY_LOSS_WARN_PCT")
    assert 0.01 < config.DAILY_LOSS_WARN_PCT < config.MAX_DAILY_LOSS_PCT


@pytest.mark.unit
def test_stoch_soft_flag_when_between_thresholds(monkeypatch):
    """Stoch at STOCH_SOFT_FLAG_THRESHOLD produces stoch_elevated flag and still passes."""
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(stoch_d=float(config.STOCH_SOFT_FLAG_THRESHOLD))
    result = check_all_gates(
        "AAPL", iv, {}, set(), None, _gates_now(),
    )
    assert result.passed
    assert any("stoch_elevated" in f for f in result.flags)


@pytest.mark.unit
def test_stoch_above_hard_veto_still_blocks(monkeypatch):
    """Stoch above STOCH_HARD_VETO_THRESHOLD must still hard-block."""
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(stoch_d=float(config.STOCH_HARD_VETO_THRESHOLD) + 1)
    result = check_all_gates(
        "AAPL", iv, {}, set(), None, _gates_now(),
    )
    assert not result.passed
    assert "Stochastic hard veto" in result.reason


@pytest.mark.unit
def test_sector_etf_soft_gate_flags_weak_sector(monkeypatch):
    """Gate 16: sector ETF drop below SECTOR_ETF_SOFT_DROP_PCT appends sector_weak flag."""
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv()
    result = check_all_gates(
        "AAPL", iv, {}, set(), None, _gates_now(),
        sector_today={"XLK": config.SECTOR_ETF_SOFT_DROP_PCT - 0.5},
        symbol_sector_etf="XLK",
    )
    assert result.passed
    assert any("sector_weak" in f for f in result.flags)


@pytest.mark.unit
class TestCorrelationHardGate:
    def _make_quality(self, tier="MEDIUM"):
        from utils.ticker_quality import TickerQuality
        return TickerQuality(tier=tier, score=50, unlocks=frozenset(), quality_gaps=(), component_scores={})

    def test_hard_gate_blocks_in_normal(self, monkeypatch):
        """NORMAL regime: correlation guard must be a hard block."""
        from signals.entry_gates import check_all_gates
        monkeypatch.setattr(
            "signals.entry_gates.passes_correlation_guard",
            lambda symbol, positions: False,
        )
        iv = _gates_iv(composite_score=80, rsi=55.0)
        _patch_cals(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=iv, open_positions={"MSFT": {}, "GOOGL": {}},
            daily_closed=set(), bars_15min=None, now=_gates_now(),
            regime="NORMAL", quality=self._make_quality(),
        )
        assert not result.passed, "correlation guard should be a hard block in NORMAL"
        assert "sector" in result.reason.lower()

    def test_soft_flag_in_bull_trend(self, monkeypatch):
        """BULL_TREND regime: correlation guard must remain soft (not block)."""
        from signals.entry_gates import check_all_gates
        monkeypatch.setattr(
            "signals.entry_gates.passes_correlation_guard",
            lambda symbol, positions: False,
        )
        iv = _gates_iv(composite_score=80, rsi=55.0)
        _patch_cals(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=iv, open_positions={},
            daily_closed=set(), bars_15min=None, now=_gates_now(),
            regime="BULL_TREND", quality=self._make_quality(),
        )
        assert result.passed, f"BULL_TREND should not hard-block on correlation (got: {result.reason})"
        assert "sector_limit_soft" in result.flags, "BULL_TREND must inform Claude via sector_limit_soft flag"

    def test_soft_flag_in_opportunity(self, monkeypatch):
        """OPPORTUNITY regime: correlation guard must remain soft."""
        from signals.entry_gates import check_all_gates
        monkeypatch.setattr(
            "signals.entry_gates.passes_correlation_guard",
            lambda symbol, positions: False,
        )
        iv = _gates_iv(composite_score=80, rsi=55.0)
        _patch_cals(monkeypatch)
        result = check_all_gates(
            symbol="AAPL", iv=iv, open_positions={},
            daily_closed=set(), bars_15min=None, now=_gates_now(),
            regime="OPPORTUNITY", quality=self._make_quality(),
        )
        assert result.passed, f"OPPORTUNITY should not hard-block on correlation (got: {result.reason})"
        assert "sector_limit_soft" in result.flags, "OPPORTUNITY must inform Claude via sector_limit_soft flag"


@pytest.mark.unit
def test_price_cap_blocks_expensive_stock(monkeypatch):
    """Stock price above MAX_ENTRY_PRICE is hard-blocked."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 400)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=450.0, vwap_std2_upper=500.0, prev_close=450.0)
    result = check_all_gates(
        "LITE", iv, {}, set(), None, _gates_now(),
    )
    assert result.passed is False
    assert "MAX_ENTRY_PRICE" in result.reason


@pytest.mark.unit
def test_price_cap_allows_stock_at_limit(monkeypatch):
    """Stock price exactly at MAX_ENTRY_PRICE passes the gate."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 400)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=400.0, vwap_std2_upper=450.0, prev_close=400.0)
    result = check_all_gates(
        "MRVL", iv, {}, set(), None, _gates_now(),
    )
    assert "MAX_ENTRY_PRICE" not in result.reason


@pytest.mark.unit
def test_price_cap_zero_disables_gate(monkeypatch):
    """MAX_ENTRY_PRICE = 0 disables the price cap entirely."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=2000.0, vwap_std2_upper=2500.0, prev_close=2000.0)
    result = check_all_gates(
        "BRK", iv, {}, set(), None, _gates_now(),
    )
    assert "MAX_ENTRY_PRICE" not in result.reason


def _market_open() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(2026, 5, 20, 10, 30, tzinfo=ZoneInfo("America/New_York"))


# ── Task 4: sector sentiment soft flag ───────────────────────────────────────

@pytest.mark.unit
def test_sector_soft_flag_when_negative(monkeypatch):
    """Sector sentiment below SECTOR_ENTRY_MIN_SCORE adds a soft flag, not a hard block."""
    monkeypatch.setattr(config, "SECTOR_ENTRY_MIN_SCORE", 0)
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=100.0)
    result = check_all_gates(
        "NVDA", iv, {}, set(), None, _market_open(),
        sector_sentiment_score=-15.0,
    )
    assert result.passed
    assert any("sector_sentiment" in f for f in result.flags)


@pytest.mark.unit
def test_sector_soft_flag_absent_when_positive(monkeypatch):
    """No sector flag when sector sentiment is non-negative."""
    monkeypatch.setattr(config, "SECTOR_ENTRY_MIN_SCORE", 0)
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=100.0)
    result = check_all_gates(
        "NVDA", iv, {}, set(), None, _market_open(),
        sector_sentiment_score=20.0,
    )
    assert not any("sector_sentiment" in f for f in result.flags)


@pytest.mark.unit
def test_news_quality_soft_flag_for_swing_candidate(monkeypatch):
    """News sentiment below SWING_NEWS_MIN_SCORE adds soft flag when swing_eligible=True."""
    monkeypatch.setattr(config, "SWING_NEWS_MIN_SCORE", 50)
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=100.0)
    result = check_all_gates(
        "NVDA", iv, {}, set(), None, _market_open(),
        swing_eligible=True, sentiment_score=30.0,
    )
    assert result.passed
    assert any("swing_news" in f for f in result.flags)


@pytest.mark.unit
def test_news_quality_flag_skipped_for_day_trade(monkeypatch):
    """No news flag when swing_eligible=False even if sentiment is low."""
    monkeypatch.setattr(config, "SWING_NEWS_MIN_SCORE", 50)
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=100.0)
    result = check_all_gates(
        "NVDA", iv, {}, set(), None, _market_open(),
        swing_eligible=False, sentiment_score=10.0,
    )
    assert not any("swing_news" in f for f in result.flags)


# ── Task 5: major_event_today hard gate ───────────────────────────────────────

@pytest.mark.unit
def test_event_day_blocks_swing_entry_in_normal(monkeypatch):
    """Major event today blocks new swing-eligible entries."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=100.0)
    result = check_all_gates(
        "NVDA", iv, {}, set(), None, _market_open(),
        swing_eligible=True, major_event_today=True, mode="DAY_TRADE",
    )
    assert not result.passed
    assert "major economic event" in result.reason.lower()


@pytest.mark.unit
def test_event_day_allows_day_trade_when_not_swing_eligible(monkeypatch):
    """Major event today does NOT block when swing_eligible=False."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=100.0)
    result = check_all_gates(
        "NVDA", iv, {}, set(), None, _market_open(),
        swing_eligible=False, major_event_today=True, mode="DAY_TRADE",
    )
    assert "major economic event" not in result.reason


@pytest.mark.unit
def test_event_day_no_block_when_no_event(monkeypatch):
    """No block when major_event_today=False."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=100.0)
    result = check_all_gates(
        "NVDA", iv, {}, set(), None, _market_open(),
        swing_eligible=True, major_event_today=False,
    )
    assert "major economic event" not in result.reason


@pytest.mark.unit
def test_event_day_does_not_block_catalyst_mode(monkeypatch):
    """CATALYST mode bypasses the event-day swing-eligible gate (post-event plays)."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 0)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=100.0)
    result = check_all_gates(
        "NVDA", iv, {}, set(), None, _market_open(),
        swing_eligible=True, major_event_today=True, mode="CATALYST",
    )
    assert "major economic event" not in result.reason


# ── Task 8: dynamic GateParams per regime×tier + get_effective_profit_lock ────

@pytest.mark.unit
class TestDynamicGateParams:
    def _conviction_quality(self, unlocks=None):
        from utils.ticker_quality import TickerQuality
        return TickerQuality(
            tier="CONVICTION", score=85,
            unlocks=frozenset(unlocks or {"T2_CHOPPY", "SWING_CHOPPY"}),
            quality_gaps=(), component_scores={},
        )

    @pytest.mark.unit
    def test_gate_params_bull_trend_higher_stoch_soft(self):
        # BULL_TREND uses higher stoch_soft_flag (88) vs NORMAL (82)
        from utils.regime_params import get_gate_params
        bull_gp = get_gate_params("BULL_TREND", "CONVICTION")
        normal_gp = get_gate_params("NORMAL", "MEDIUM")
        assert bull_gp.stoch_soft_flag > normal_gp.stoch_soft_flag

    @pytest.mark.unit
    def test_staleness_guard_composite_penalty_applied(self, monkeypatch):
        from signals.entry_gates import effective_composite_gate
        from unittest.mock import MagicMock
        guard = MagicMock()
        guard.composite_penalty = 10
        iv = MagicMock()
        iv.composite_score = 55.0
        iv.squeeze_setup = False
        iv.bot_herding_score = 0
        monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", 50)
        result = effective_composite_gate(iv, "NORMAL", staleness_guard=guard)
        assert result == 50 + 10

    @pytest.mark.unit
    def test_choppy_conviction_swing_profit_lock(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("CHOPPY", "CONVICTION", "SWING")
        assert p.profit_lock_pct == pytest.approx(1.0)

    @pytest.mark.unit
    def test_normal_high_swing_profit_lock(self):  # noqa: keep ordering
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("NORMAL", "HIGH", "SWING")
        assert p.profit_lock_pct == pytest.approx(1.2)

    @pytest.mark.unit
    def test_choppy_high_swing_profit_lock(self):
        from utils.regime_params import get_tp_sl_params
        p = get_tp_sl_params("CHOPPY", "HIGH", "SWING")
        assert p.profit_lock_pct == pytest.approx(2.0)


@pytest.mark.unit
class TestMACDMomentumGate:
    """Gate 1c: day-trade entries blocked when MACD histogram is negative."""

    def _now(self):
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 10, 30, 0, tzinfo=ZoneInfo("America/New_York"))

    def _patch(self, monkeypatch):
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    def test_negative_macd_hist_soft_flags_day_trade(self, monkeypatch):
        """MACD histogram < 0 → soft flag in DAY_TRADE mode (not hard block)."""
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = _gates_iv(macd_hist=-0.05)
        result = check_all_gates("AAPL", iv, {}, set(), None, self._now(), mode="DAY_TRADE")
        assert result.passed is True
        assert any("macd" in f.lower() for f in result.flags)

    def test_positive_macd_hist_no_flag(self, monkeypatch):
        """MACD histogram > 0 → no macd flag in DAY_TRADE mode."""
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = _gates_iv(macd_hist=0.10)
        result = check_all_gates("AAPL", iv, {}, set(), None, self._now(), mode="DAY_TRADE")
        assert not any("macd" in f.lower() for f in result.flags)

    def test_macd_soft_flag_not_added_in_swing_mode(self, monkeypatch):
        """MACD soft flag does not apply to SWING mode."""
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = _gates_iv(macd_hist=-0.10)
        result = check_all_gates("AAPL", iv, {}, set(), None, self._now(), mode="SWING")
        assert not any("macd" in f.lower() for f in result.flags)

    def test_zero_macd_hist_no_flag(self, monkeypatch):
        """MACD histogram = 0 is not flagged (only strictly negative triggers soft flag)."""
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = _gates_iv(macd_hist=0.0)
        result = check_all_gates("AAPL", iv, {}, set(), None, self._now(), mode="DAY_TRADE")
        assert not any("macd" in f.lower() for f in result.flags)


@pytest.mark.unit
class TestChoppyRsiGate:
    """Gate 1c: CHOPPY regime RSI pullback gate in check_all_gates (Rule 23 SoT)."""

    def _make_quality(self, tier="MEDIUM"):
        from utils.ticker_quality import TickerQuality
        return TickerQuality(tier=tier, score=50, unlocks=frozenset(), quality_gaps=(), component_scores={})

    def _now(self):
        # Use 10:30 ET — before CHOPPY/MEDIUM hard cutoff (11:00) so timing gates don't interfere
        from zoneinfo import ZoneInfo
        return datetime(2024, 1, 15, 10, 30, 0, tzinfo=ZoneInfo("America/New_York"))

    def _patch(self, monkeypatch):
        import calendars.earnings as ec_mod
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec_mod, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    def test_choppy_high_rsi_blocks_entry(self, monkeypatch):
        """CHOPPY RSI gate in check_all_gates blocks high-RSI entries in CHOPPY regime."""
        monkeypatch.setattr(config, "REGIME_ADAPTIVE_LOGIC", True)
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        # RSI=72 > CHOPPY_RSI_PULLBACK_MAX=60 → should be hard-blocked
        iv = _gates_iv(rsi=72.0)
        result = check_all_gates(
            "AAPL", iv, {}, set(), None, self._now(), regime="CHOPPY",
            quality=self._make_quality(),
        )
        assert not result.passed, "CHOPPY with high RSI should be blocked by gate 1c"
        assert "choppy" in result.reason.lower()

    def test_choppy_low_rsi_passes(self, monkeypatch):
        """CHOPPY RSI gate allows low-RSI entries in CHOPPY regime."""
        monkeypatch.setattr(config, "REGIME_ADAPTIVE_LOGIC", True)
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        # RSI=55 < CHOPPY_RSI_PULLBACK_MAX=60 → gate 1c must pass
        iv = _gates_iv(rsi=55.0)
        result = check_all_gates(
            "AAPL", iv, {}, set(), None, self._now(), regime="CHOPPY",
            quality=self._make_quality(),
        )
        assert result.passed, f"CHOPPY with RSI=55 should pass gate 1c, got: {result.reason}"

    def test_regime_adaptive_logic_off_skips_choppy_gate(self, monkeypatch):
        """CHOPPY RSI gate is skipped when REGIME_ADAPTIVE_LOGIC=False.

        Uses RSI=65 — above CHOPPY_RSI_PULLBACK_MAX (60) but below the general RSI_BUY_MAX (72).
        When REGIME_ADAPTIVE_LOGIC=False, gate 1c is bypassed so RSI=65 must not trigger a block
        from the CHOPPY-specific gate.
        """
        monkeypatch.setattr(config, "REGIME_ADAPTIVE_LOGIC", False)
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        # RSI=65 is above CHOPPY_RSI_PULLBACK_MAX=60 but below RSI_BUY_MAX=72
        # With REGIME_ADAPTIVE_LOGIC=False, gate 1c must not fire
        iv = _gates_iv(rsi=65.0)
        result = check_all_gates(
            "AAPL", iv, {}, set(), None, self._now(), regime="CHOPPY",
            quality=self._make_quality(),
        )
        # Gate must not block on the CHOPPY-specific RSI reason
        assert "choppy" not in result.reason.lower(), (
            f"Gate 1c must be bypassed when REGIME_ADAPTIVE_LOGIC=False, got: {result.reason}"
        )

    def test_choppy_rsi_at_boundary_passes(self, monkeypatch):
        """RSI exactly at rsi_max boundary passes the gate (strict > comparison)."""
        monkeypatch.setattr(config, "REGIME_ADAPTIVE_LOGIC", True)
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        # RSI exactly at rsi_max=60 — gate uses iv.rsi > ep.rsi_max, so equal passes
        iv = _gates_iv(rsi=float(config.CHOPPY_RSI_PULLBACK_MAX))
        result = check_all_gates(
            "AAPL", iv, {}, set(), None, self._now(), regime="CHOPPY",
            quality=self._make_quality(),
        )
        assert result.passed, f"RSI at exact boundary should pass (> not >=), got: {result.reason}"

    def test_choppy_gate_not_applied_in_normal_regime(self, monkeypatch):
        """Gate 1c must not fire in NORMAL regime even with high RSI (rsi_max gate handles it)."""
        monkeypatch.setattr(config, "REGIME_ADAPTIVE_LOGIC", True)
        self._patch(monkeypatch)
        from signals.entry_gates import check_all_gates
        # RSI=62 would fail CHOPPY gate but should not trigger it in NORMAL
        iv = _gates_iv(rsi=62.0)
        result = check_all_gates(
            "AAPL", iv, {}, set(), None, self._now(), regime="NORMAL",
            quality=self._make_quality(),
        )
        # May fail due to RSI > rsi_max in NORMAL, but reason must not say "choppy"
        if not result.passed:
            assert "choppy" not in result.reason.lower(), (
                f"Gate 1c must not fire in NORMAL regime, got: {result.reason}"
            )
