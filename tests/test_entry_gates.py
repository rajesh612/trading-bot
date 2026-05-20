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
            self._now_blocked_time(), entries_this_cycle=0,
        )
        assert result.passed is False
        assert "time" in result.reason

    def test_per_cycle_cap_blocks(self, monkeypatch):
        """Gate 2 fires when entries_this_cycle >= MAX_ENTRIES_PER_CYCLE."""
        import config
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, set(), None,
            self._now_allowed(), entries_this_cycle=config.MAX_ENTRIES_PER_CYCLE,
        )
        assert result.passed is False
        assert "cap" in result.reason

    def test_daily_closed_blocks(self, monkeypatch):
        """Gate 3 fires when symbol is in daily_closed set."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, {"NVDA"}, None,
            self._now_allowed(), entries_this_cycle=0,
        )
        assert result.passed is False
        assert "closed today" in result.reason

    def test_position_already_open_blocks(self, monkeypatch):
        """Gate 3 fires when symbol already has an open position."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {"NVDA": {}}, set(), None,
            self._now_allowed(), entries_this_cycle=0,
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
            self._now_allowed(), entries_this_cycle=0,
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
            self._now_allowed(), entries_this_cycle=0,
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
            self._now_allowed(), entries_this_cycle=0,
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
            self._now_allowed(), entries_this_cycle=0,
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
            self._now_allowed(), entries_this_cycle=0,
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
        )
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _patch_calendars(self, monkeypatch):
        import calendars.earnings as ec
        import macro.economic_calendar as ecc
        monkeypatch.setattr(ec, "is_earnings_imminent", lambda *a, **kw: False)
        monkeypatch.setattr(ecc, "is_event_risk_window", lambda *a, **kw: (False, ""))

    # ── Gate ordering ─────────────────────────────────────────────────────────

    def test_time_gate_fires_before_cap(self, monkeypatch):
        """Time gate (1) fires before per-cycle cap (2) — both conditions present."""
        import config
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, set(), None,
            self._now_closed(),                        # outside window
            entries_this_cycle=config.MAX_ENTRIES_PER_CYCLE,  # cap also hit
        )
        assert result.passed is False
        assert "time" in result.reason                # time gate message, not cap

    def test_cap_fires_before_daily_closed(self, monkeypatch):
        """Cap (2) fires before daily-closed check (3)."""
        import config
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "NVDA", self._iv(), {}, {"NVDA"}, None,
            self._now_open(),
            entries_this_cycle=config.MAX_ENTRIES_PER_CYCLE,  # cap fires
        )
        assert result.passed is False
        assert "cap" in result.reason                  # not "closed today"

    def test_stop_hunt_fires_before_vwap_check(self, monkeypatch):
        """Stop-hunt (6) is checked before VWAP overextension (7)."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(stop_hunt_zone=True, current_price=120.0, vwap_std2_upper=100.0)
        result = check_all_gates("NVDA", iv, {}, set(), None, self._now_open(), 0)
        assert result.passed is False
        assert "stop-hunt" in result.reason            # not VWAP message

    def test_vwap_gate_fires_before_parabolic(self, monkeypatch):
        """VWAP overextension (7) is checked before parabolic (8)."""
        import config
        monkeypatch.setattr(config, "VWAP_OVEREXTENSION_GATE", True)
        monkeypatch.setattr(config, "PARABOLIC_ENTRY_BLOCK", True)
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        iv = self._iv(current_price=120.0, vwap_std2_upper=100.0, parabolic=True)
        result = check_all_gates("NVDA", iv, {}, set(), None, self._now_open(), 0)
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
            self._now_open(), entries_this_cycle=0,
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
            self._now_open(), entries_this_cycle=0,
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
            self._now_open(), entries_this_cycle=0,
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
            self._now_open(), entries_this_cycle=0,
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
            self._now_open(), entries_this_cycle=0,
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
            self._now(), entries_this_cycle=0,
        )
        assert result.passed is False
        assert "RSI" in result.reason

    def test_rsi_at_threshold_passes(self, monkeypatch):
        """RSI exactly at threshold passes — gate is strictly greater-than."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "AAPL", self._iv(rsi=72.0), {}, set(), None,
            self._now(), entries_this_cycle=0,
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
            self._now(), entries_this_cycle=0,
        )
        assert result.passed is False
        assert "Stochastic" in result.reason or "stoch" in result.reason.lower()

    def test_healthy_rsi_and_stoch_passes(self, monkeypatch):
        """RSI=60, stoch_d=70 — both below thresholds — gate passes."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        result = check_all_gates(
            "MSFT", self._iv(rsi=60.0, stoch_d=70.0), {}, set(), None,
            self._now(), entries_this_cycle=0,
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
            self._now(), entries_this_cycle=0,
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
            self._now(), entries_this_cycle=0,
        )
        assert result.passed is True

    def test_gap_up_skip_bypassed_when_prev_close_zero(self, monkeypatch):
        """prev_close=0.0 → gate is skipped entirely (doesn't block)."""
        self._patch_calendars(monkeypatch)
        from signals.entry_gates import check_all_gates
        # prev_close=0 → gate check skipped, should allow entry
        result = check_all_gates(
            "TEST", self._iv(prev_close=0.0, current_price=100.0), {}, set(), None,
            self._now(), entries_this_cycle=0,
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
        "AAPL", iv, {}, set(), None, _gates_now(), entries_this_cycle=0,
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
        "AAPL", iv, {}, set(), None, _gates_now(), entries_this_cycle=0,
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
        entries_this_cycle=0,
        sector_today={"XLK": config.SECTOR_ETF_SOFT_DROP_PCT - 0.5},
        symbol_sector_etf="XLK",
    )
    assert result.passed
    assert any("sector_weak" in f for f in result.flags)


@pytest.mark.unit
def test_price_cap_blocks_expensive_stock(monkeypatch):
    """Stock price above MAX_ENTRY_PRICE is hard-blocked."""
    monkeypatch.setattr(config, "MAX_ENTRY_PRICE", 400)
    _patch_cals(monkeypatch)
    from signals.entry_gates import check_all_gates
    iv = _gates_iv(current_price=450.0, vwap_std2_upper=500.0, prev_close=450.0)
    result = check_all_gates(
        "LITE", iv, {}, set(), None, _gates_now(), entries_this_cycle=0,
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
        "MRVL", iv, {}, set(), None, _gates_now(), entries_this_cycle=0,
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
        "BRK", iv, {}, set(), None, _gates_now(), entries_this_cycle=0,
    )
    assert "MAX_ENTRY_PRICE" not in result.reason
