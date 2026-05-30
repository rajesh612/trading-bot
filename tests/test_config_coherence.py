"""Config coherence tests — assert that key thresholds are self-consistent.

These tests do not test behaviour; they assert invariants between config values,
source-code structure, and module contracts so that silent regressions (e.g. stale
threshold set too low, safety rule leaked into quality router) are caught early.
"""
import ast
import pathlib
from unittest.mock import MagicMock, patch, call

import pytest

import config
from utils.data_health import FRESH_GUARD
from utils.regime_params import get_filter_params


# ---------------------------------------------------------------------------
# Test 1: regime staleness threshold covers the full trading session
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_regime_stale_threshold_covers_full_session():
    """regime fetched once at 8:55 AM; threshold must not expire before 3:55 PM (420 min)."""
    assert config.DATA_STALE_THRESHOLD_MINUTES["regime"] >= 420, (
        f"DATA_STALE_THRESHOLD_MINUTES['regime'] = "
        f"{config.DATA_STALE_THRESHOLD_MINUTES['regime']} — must be >= 420 to cover full session"
    )


# ---------------------------------------------------------------------------
# Test 2: DATA_REGIME_CANOPEN_MAX_MINUTES covers full session
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_can_open_max_covers_full_session():
    """Regime can_open window must span 8:55 AM → 3:55 PM (420 min)."""
    regime_fetch_minutes = 8 * 60 + 55    # 8:55 AM in minutes from midnight
    market_close_minutes = 15 * 60 + 55   # 3:55 PM in minutes from midnight
    required = market_close_minutes - regime_fetch_minutes  # 420

    assert config.DATA_REGIME_CANOPEN_MAX_MINUTES >= required, (
        f"DATA_REGIME_CANOPEN_MAX_MINUTES = {config.DATA_REGIME_CANOPEN_MAX_MINUTES} "
        f"— must be >= {required} to cover 8:55 AM through 3:55 PM"
    )


# ---------------------------------------------------------------------------
# Test 3: BULL_TREND / LOW composite_min is reachable by real intraday signals
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_bull_trend_low_composite_threshold_reachable():
    """BULL_TREND + LOW tier composite_min must be <= 65.

    Composite engine scores typically range -100 to +100 with mean ~0 intraday;
    threshold >65 means virtually nothing passes the pre-claude filter in practice.
    A threshold above 65 defeats the purpose of the quality router for that tier.
    """
    fp = get_filter_params("BULL_TREND", "LOW")
    assert fp.composite_min <= 65, (
        f"FilterParams(BULL_TREND, LOW).composite_min = {fp.composite_min} "
        f"— must be <= 65 to be reachable by real intraday composites"
    )


# ---------------------------------------------------------------------------
# Test 4: pre_claude_filter must not contain safety-gate function references
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_no_safety_gates_in_pre_claude_filter():
    """Rule 23: pre_claude_filter is a quality router only — no safety gate names allowed.

    We use AST Name parsing to catch accidental calls/imports of safety gate functions.
    Even indirect attribute access like iv.stop_hunt_zone would be caught — this is
    intentional. This is a regression test for the exact class of violation that
    occurred historically: safety rules leaking into the quality router.
    """
    src_path = pathlib.Path(__file__).parent.parent / "trading" / "pre_claude_filter.py"
    src = src_path.read_text()
    tree = ast.parse(src)

    # Collect every Name node (function calls, variable references, etc.)
    names_used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}

    forbidden = {"is_earnings_imminent", "is_event_risk_window", "stop_hunt_zone"}
    violations = names_used & forbidden

    assert not violations, (
        f"Rule 23 violation — safety gate(s) in pre_claude_filter: {violations}"
    )


# ---------------------------------------------------------------------------
# Test 5: _run_intraday_retries is called BEFORE get_staleness_guard
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_guard_built_after_intraday_retry():
    """Staleness guard must be built after intraday retries so it reflects refreshed data.

    This is a regression test for the guard-built-before-retry ordering bug. The
    behavioral consequence (stale guard ignoring a successful retry) is unobservable
    without running real data fetches, so we verify the contract structurally.
    Note: cycle.py imports get_staleness_guard inside the function body at runtime,
    not at module level — so patching the source module (utils.data_health) is correct.
    """
    call_order: list[str] = []

    # Build a minimal mock account
    mock_account = MagicMock()
    mock_account.portfolio_value = "100000"
    mock_account.buying_power = "50000"
    mock_account.last_equity = "99000"

    mock_alpaca = MagicMock()
    mock_alpaca.get_account.return_value = mock_account

    regime_info = {
        "regime": "NORMAL",
        "flash_crash_active": False,
        "vix_current": 15.0,
        "macro_health_score": 50,
    }

    trading_params = {
        "entry_start_hour": 10,
        "entry_end_hour": 15,
        "rsi_buy_max": 74,
        "rsi_buy_max_high_vol": 65,
        "high_vol_position_mult": 0.8,
        "composite_min_normal": 30,
    }

    mock_pipeline = MagicMock()
    mock_pipeline_result = MagicMock()
    mock_pipeline_result.candidates = 1
    mock_pipeline_result.entries = 0
    mock_pipeline_result.closes = 0
    mock_pipeline_result.skipped = False
    mock_pipeline.run.return_value = mock_pipeline_result

    def record_retry(*_args, **_kwargs):
        call_order.append("retry")

    def record_guard(*_args, **_kwargs):
        call_order.append("guard")
        return FRESH_GUARD

    with (
        patch("trading.cycle.AlpacaClient", return_value=mock_alpaca),
        patch("trading.cycle.load_macro_regime", return_value=regime_info),
        patch("trading.cycle.load_trading_params", return_value=trading_params),
        patch("trading.cycle.load_watch_list", return_value=["AAPL", "MSFT"]),
        patch("trading.cycle.build_cycle_snapshot", return_value=MagicMock()),
        patch("trading.cycle.TradingPipeline", return_value=mock_pipeline),
        patch("trading.cycle._run_intraday_retries", side_effect=record_retry),
        patch("utils.data_health.get_staleness_guard", side_effect=record_guard),
        patch("trading.cycle.telegram_notify", MagicMock()),
        patch("utils.metrics.record_cycle_metric"),
        patch("anthropic.Anthropic"),
    ):
        from trading.cycle import run_trading_cycle
        run_trading_cycle()

    assert "retry" in call_order, "_run_intraday_retries was never called"
    assert "guard" in call_order, "get_staleness_guard was never called"
    assert call_order.index("retry") < call_order.index("guard"), (
        f"Expected retry before guard but got order: {call_order}"
    )


@pytest.mark.unit
class TestFredSchemaContract:
    def test_consumed_keys_exist_in_schema(self):
        """All keys context_builder reads from fred_cache must exist in the FRED output schema.
        cpi_trend is a legacy fallback intentionally excluded from the schema."""
        from macro.fred_cache import FRED_SCHEMA_KEYS
        from trading.context_builder import FRED_KEYS_CONSUMED
        legacy_fallbacks = {"cpi_trend"}   # read as fallback; not in schema by design
        missing = FRED_KEYS_CONSUMED - FRED_SCHEMA_KEYS - legacy_fallbacks
        assert not missing, f"context_builder reads keys not produced by FRED: {missing}"


@pytest.mark.unit
def test_regime_heat_ceiling_covers_all_regimes():
    """REGIME_HEAT_CEILING must have an entry for every regime in REGIMES tuple."""
    import config
    from utils.regime_params import REGIMES
    missing = set(REGIMES) - set(config.REGIME_HEAT_CEILING.keys())
    assert not missing, f"REGIME_HEAT_CEILING missing entries for: {missing}"


@pytest.mark.unit
def test_swing_health_min_pnl_is_negative():
    """SWING_HEALTH_MIN_PNL_PCT must be negative — it is a loss threshold."""
    import config
    assert config.SWING_HEALTH_MIN_PNL_PCT < 0, (
        f"SWING_HEALTH_MIN_PNL_PCT={config.SWING_HEALTH_MIN_PNL_PCT} must be < 0"
    )


@pytest.mark.unit
def test_burst_cap_greater_than_one():
    """MAX_ENTRIES_PER_CYCLE_BURST must be > 1 — the point of the change."""
    import config
    assert config.MAX_ENTRIES_PER_CYCLE_BURST > 1, (
        f"MAX_ENTRIES_PER_CYCLE_BURST={config.MAX_ENTRIES_PER_CYCLE_BURST} — expected > 1"
    )


@pytest.mark.unit
def test_backtest_swing_composite_min_retired():
    """BACKTEST_SWING_COMPOSITE_MIN must be removed — callers use get_swing_gate_params()."""
    assert not hasattr(config, "BACKTEST_SWING_COMPOSITE_MIN"), (
        "BACKTEST_SWING_COMPOSITE_MIN still exists — delete it from config.py"
    )


@pytest.mark.unit
def test_swing_hold_params_normal_medium_min_pnl():
    """NORMAL MEDIUM min_pnl_pct lowered to 0.5% to match BULL_TREND/MEDIUM threshold.
    Prior 1.2% was blocking low-gain positions that succeed as swings (88%+ swing WR)."""
    _, _, min_pnl = config.SWING_HOLD_PARAMS[("NORMAL", "MEDIUM")]
    assert min_pnl == 0.5, f"Expected 0.5, got {min_pnl}"


@pytest.mark.unit
def test_crash_recovery_min_green_days_exists():
    """CRASH_RECOVERY_MIN_GREEN_DAYS must exist and be set to 2."""
    assert hasattr(config, "CRASH_RECOVERY_MIN_GREEN_DAYS")
    assert config.CRASH_RECOVERY_MIN_GREEN_DAYS == 2


@pytest.mark.unit
def test_swing_gate_params_fallback_is_normal_medium():
    """Unknown regime/tier combo falls back to NORMAL MEDIUM values."""
    from utils.regime_params import get_swing_gate_params
    sgp = get_swing_gate_params("UNKNOWN_REGIME", "UNKNOWN_TIER")
    assert sgp.composite_min == 58
    assert sgp.rsi_ceiling == 70


@pytest.mark.unit
def test_swing_gate_table_covers_all_swing_eligible_regimes():
    """All swing-eligible regimes have entries for all 4 tiers."""
    from utils.regime_params import get_swing_gate_params
    swing_eligible = ["BULL_TREND", "NORMAL", "OPPORTUNITY", "CRASH_RECOVERY", "CAPITULATION"]
    tiers = ["CONVICTION", "HIGH", "MEDIUM", "LOW"]
    for regime in swing_eligible:
        for tier in tiers:
            sgp = get_swing_gate_params(regime, tier)
            assert sgp.composite_min > 0, f"{regime}/{tier} composite_min is 0"
            assert sgp.rsi_ceiling > 0, f"{regime}/{tier} rsi_ceiling is 0"


@pytest.mark.unit
def test_swing_gate_params_bull_trend_more_permissive_than_normal():
    """BULL_TREND thresholds are lower (more permissive) than NORMAL for the same tier."""
    from utils.regime_params import get_swing_gate_params
    for tier in ["CONVICTION", "HIGH", "MEDIUM", "LOW"]:
        bull = get_swing_gate_params("BULL_TREND", tier)
        normal = get_swing_gate_params("NORMAL", tier)
        assert bull.composite_min <= normal.composite_min, f"{tier}: BULL_TREND composite not <= NORMAL"
        assert bull.rsi_ceiling >= normal.rsi_ceiling, f"{tier}: BULL_TREND rsi_ceiling not >= NORMAL"


@pytest.mark.unit
def test_swing_gate_normal_medium_composite_floor_at_most_60():
    """NORMAL/MEDIUM swing gate composite_min must be ≤ 60."""
    from utils.regime_params import get_swing_gate_params
    sgp = get_swing_gate_params("NORMAL", "MEDIUM")
    assert sgp.composite_min <= 60, (
        f"NORMAL/MEDIUM swing gate composite_min={sgp.composite_min} > 60 — "
        "swing gate too restrictive; 88%+ WR justifies lower floor"
    )


@pytest.mark.unit
def test_swing_gate_bull_trend_medium_composite_floor_at_most_50():
    """BULL_TREND/MEDIUM swing gate composite_min must be ≤ 50."""
    from utils.regime_params import get_swing_gate_params
    sgp = get_swing_gate_params("BULL_TREND", "MEDIUM")
    assert sgp.composite_min <= 50, (
        f"BULL_TREND/MEDIUM swing gate composite_min={sgp.composite_min} > 50"
    )


@pytest.mark.unit
def test_entry_late_composite_min_at_least_swing_gate_medium():
    """ENTRY_LATE_COMPOSITE_MIN must be >= NORMAL/MEDIUM swing gate composite_min.
    Late entries below swing quality are noise — they won't run to TP before EOD."""
    from utils.regime_params import get_swing_gate_params
    sgp = get_swing_gate_params("NORMAL", "MEDIUM")
    assert config.ENTRY_LATE_COMPOSITE_MIN >= sgp.composite_min, (
        f"ENTRY_LATE_COMPOSITE_MIN={config.ENTRY_LATE_COMPOSITE_MIN} < "
        f"NORMAL/MEDIUM swing gate={sgp.composite_min}"
    )


@pytest.mark.unit
class TestHighImpactEventTagsConsistency:
    def test_high_impact_event_tags_in_config(self):
        import config
        assert hasattr(config, "HIGH_IMPACT_EVENT_TAGS"), \
            "HIGH_IMPACT_EVENT_TAGS must be defined in config.py"
        assert isinstance(config.HIGH_IMPACT_EVENT_TAGS, frozenset)
        # Must include all tags from both original callers
        for tag in {"FOMC", "NFP", "CPI", "PCE", "JOLTS", "PPI", "GDP"}:
            assert tag in config.HIGH_IMPACT_EVENT_TAGS, \
                f"{tag} missing from HIGH_IMPACT_EVENT_TAGS"

    def test_cycle_context_uses_config_constant(self):
        import trading.cycle_context as cc
        src = open(cc.__file__).read()
        assert "HIGH_IMPACT_EVENT_TAGS" in src, \
            "cycle_context must reference HIGH_IMPACT_EVENT_TAGS"
        assert '{"FOMC"' not in src and "{'FOMC'" not in src, \
            "cycle_context must not define its own local event set"

    def test_monitor_uses_config_constant(self):
        import trading.monitor as mon
        src = open(mon.__file__).read()
        assert "HIGH_IMPACT_EVENT_TAGS" in src, \
            "monitor must reference HIGH_IMPACT_EVENT_TAGS"
        assert '{"FOMC"' not in src and "{'FOMC'" not in src, \
            "monitor must not define its own local event set"


# ---------------------------------------------------------------------------
# Test N: all recurring notify_* functions must use _once.should_send()
# ---------------------------------------------------------------------------

# Functions that are legitimately exempt from the _once.should_send() requirement.
# These fire exactly once per triggering event by construction (buy, close, startup,
# EOD summary) or use a disk-based sentinel because they need to survive bot restarts.
# Adding a new notify_* to this list requires a comment explaining why it doesn't need
# in-memory dedup; anything NOT here must call _once.should_send() before _send().
_NOTIFY_DEDUP_EXEMPT: frozenset[str] = frozenset({
    # --- lifecycle: fire once at bot start/stop ---
    "notify_startup",
    "notify_degraded_integrations",
    "notify_shutdown",
    # --- trade events: fire once per trade by construction ---
    "notify_buy",
    "notify_close",
    "notify_tranche2_add",
    "notify_buy_batch",
    # --- one-off Claude/budget events: triggered by state change, not a loop ---
    "notify_claude_disabled",
    "notify_budget_escalation",
    "notify_budget_warning",
    "notify_job_failure",
    "notify_file_corruption",
    "notify_alert",
    "notify_correlated_exposure",
    # --- regime events: fire at most once per regime transition ---
    "notify_regime_change",
    "notify_flash_crash",
    # --- EOD: fire once at 4:20 PM ---
    "notify_eod_summary",
    "notify_no_trades_midday",
    # --- disk-deduped (intentionally NOT _once — must survive bot restarts) ---
    "notify_stale_research",      # uses STALE_RESEARCH_ALERTED_FILE sentinel
    "notify_watch_list",          # uses WATCH_LIST_NOTIF_FILE sentinel
    # --- internal implementation helpers (not public notify_* entry points) ---
    "_notify_stale_research_impl",
})


@pytest.mark.unit
def test_recurring_notify_functions_use_once_guard():
    """Rule 33: every recurring notify_* in telegram.py must call _once.should_send().

    Uses AST inspection to catch new notifications added without a dedup guard.
    Fires without data, in CI, before any import — no side effects.

    To add a new notify_* function:
      - If it fires from a monitor/cycle loop → add _once.should_send() guard.
      - If it fires exactly once per event → add its name to _NOTIFY_DEDUP_EXEMPT above
        with a comment explaining why it doesn't need dedup.
    """
    src_path = pathlib.Path(__file__).parent.parent / "notifications" / "telegram.py"
    src = src_path.read_text()
    tree = ast.parse(src)

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("notify_") and node.name != "_notify_stale_research_impl":
            continue
        if node.name in _NOTIFY_DEDUP_EXEMPT:
            continue

        # Function is a recurring notify_* — must call _once.should_send somewhere.
        func_src = ast.get_source_segment(src, node) or ""
        if "_once.should_send" not in func_src:
            violations.append(node.name)

    assert not violations, (
        f"Rule 33 violation — notify_* functions missing _once.should_send() guard:\n"
        f"  {violations}\n"
        f"Add `if not _once.should_send('scope', key): return` before `_send(...)`\n"
        f"or add the function name to _NOTIFY_DEDUP_EXEMPT with a justification comment."
    )


# ---------------------------------------------------------------------------
# Rule 34 (new): param tables must honour monkeypatch — no frozen module-level
# constants that silently ignore config overrides after import.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_filter_params_composite_min_reflects_monkeypatched_config(monkeypatch):
    """get_filter_params must re-read config so monkeypatch.setattr works in tests.

    The _FILTER_TABLE was previously built at module import time, so patching
    config.COMPOSITE_MIN_NORMAL in a test had no effect on get_filter_params.
    This caused tests that changed the composite floor to silently test nothing.
    """
    import config
    from utils.regime_params import get_filter_params
    original = config.COMPOSITE_MIN_NORMAL
    monkeypatch.setattr(config, "COMPOSITE_MIN_NORMAL", original + 20)
    p = get_filter_params("NORMAL", "MEDIUM")
    assert p.composite_min != original, (
        f"get_filter_params returned composite_min={p.composite_min} "
        f"which equals the OLD value {original} — the table was frozen at import. "
        f"Convert _FILTER_TABLE to a per-call builder (like get_entry_params)."
    )


@pytest.mark.unit
def test_high_vol_medium_entry_gates_stricter_than_normal():
    """HIGH_VOL/MEDIUM must have stricter late_composite (>=78) and
    earlier/equal hard_close_hour vs NORMAL/MEDIUM.
    HIGH_VOL Jan-Feb 2026 generated approx -$2.9k — tighter gates reduce this drag."""
    from utils.regime_params import get_day_trade_params
    hv = get_day_trade_params("HIGH_VOL", "MEDIUM")
    nm = get_day_trade_params("NORMAL", "MEDIUM")
    assert hv.late_composite >= 78, (
        f"HIGH_VOL/MEDIUM late_composite={hv.late_composite} < 78"
    )
    assert hv.hard_close_hour <= nm.hard_close_hour, (
        f"HIGH_VOL/MEDIUM hard_close_hour={hv.hard_close_hour} must be <= NORMAL/MEDIUM={nm.hard_close_hour}"
    )
    assert hv.late_composite > nm.late_composite, (
        f"HIGH_VOL/MEDIUM late_composite={hv.late_composite} must be > NORMAL/MEDIUM={nm.late_composite}"
    )


@pytest.mark.unit
def test_normal_medium_swing_promote_min_pnl_at_most_0_5():
    """NORMAL/MEDIUM swing promotion min_pnl_pct must be <= 0.5%.
    BULL_TREND/MEDIUM is 0.5%; NORMAL should match given 88%+ swing WR.
    Prior 1.2% was blocking low-gain positions that succeed as swings."""
    assert ("NORMAL", "MEDIUM") in config.SWING_HOLD_PARAMS, \
        "SWING_HOLD_PARAMS missing ('NORMAL', 'MEDIUM') key"
    _max_days, _profit_lock, min_pnl = config.SWING_HOLD_PARAMS[("NORMAL", "MEDIUM")]
    assert min_pnl <= 0.5, (
        f"NORMAL/MEDIUM swing min_pnl_pct={min_pnl} > 0.5 — "
        "too restrictive; 88%+ swing WR justifies lower threshold"
    )


@pytest.mark.unit
def test_gate_params_stoch_veto_reflects_monkeypatched_config(monkeypatch):
    """get_gate_params stoch_hard_veto must track config.STOCH_HARD_VETO_THRESHOLD.

    Previously the _GATE_TABLE was frozen at import, so patching the threshold
    in tests did not affect the veto value seen by the filter — tests were silently
    passing without exercising the intended code path.
    """
    import config
    from utils.regime_params import get_gate_params
    monkeypatch.setattr(config, "STOCH_HARD_VETO_THRESHOLD", 85)
    gp = get_gate_params("NORMAL", "MEDIUM")
    assert gp.stoch_hard_veto == 85, (
        f"get_gate_params returned stoch_hard_veto={gp.stoch_hard_veto} "
        f"but config was patched to 85 — the table was frozen at import."
    )
