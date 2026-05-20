"""Regime and trade-mode string constants.

Import these instead of bare string literals to prevent silent typo bugs
and enable O(1) frozenset membership tests.
"""

CRISIS         = "CRISIS"
CRASH_RECOVERY = "CRASH_RECOVERY"
BULL_TREND     = "BULL_TREND"
CHOPPY         = "CHOPPY"
DEFENSIVE      = "DEFENSIVE"
NORMAL         = "NORMAL"
OPPORTUNITY    = "OPPORTUNITY"
CAPITULATION   = "CAPITULATION"
# HIGH_VOL: returned by backtest/data.py simplified classifier but never by live classify_regime()
HIGH_VOL       = "HIGH_VOL"
# BULL_TREND_PARABOLIC: legacy constant — never emitted by live classify_regime(),
# present only in _VALID_REGIMES validation and _SL_TP_TABLE for historical coverage.
# Not included in VALID.
BULL_TREND_PARABOLIC = "BULL_TREND_PARABOLIC"

VALID = frozenset({
    CRISIS, CRASH_RECOVERY, BULL_TREND, CHOPPY,
    DEFENSIVE, NORMAL, OPPORTUNITY, CAPITULATION,
})
ALLOW_ENTRY    = VALID - {CRISIS}
HIGH_VOL_GATES = frozenset({HIGH_VOL, DEFENSIVE})
SIZE_REDUCE    = frozenset({HIGH_VOL, DEFENSIVE, CHOPPY})
SWING_ELIGIBLE = frozenset({BULL_TREND, OPPORTUNITY, CAPITULATION, CRASH_RECOVERY, NORMAL})
