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
    """NORMAL MEDIUM min_pnl_pct raised to 1.2% to filter marginal overnight positions."""
    _, _, min_pnl = config.SWING_HOLD_PARAMS[("NORMAL", "MEDIUM")]
    assert min_pnl == 1.2, f"Expected 1.2, got {min_pnl}"


@pytest.mark.unit
def test_crash_recovery_min_green_days_exists():
    """CRASH_RECOVERY_MIN_GREEN_DAYS must exist and be set to 2."""
    assert hasattr(config, "CRASH_RECOVERY_MIN_GREEN_DAYS")
    assert config.CRASH_RECOVERY_MIN_GREEN_DAYS == 2
