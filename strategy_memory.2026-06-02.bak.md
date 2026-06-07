# strategy_memory.md
*Last updated: 2026-06-01*

---

## General Rules

- **Position sizing**:
  - Composite ≥ 50 (NORMAL): full size
  - Composite 25–49: half size
  - Composite < 25: skip
  - HIGH_VOL regime: multiply by 0.75 (only if VIX meaningfully elevated: >10% above 90d avg or accelerating)
- **Stop-loss**: 4.0% ceiling. Do not tighten — lower SL roughly doubles hits with worse net P&L.
- **Take-profit**: 5.0% (ATR multiplier 2.5x as guide)
- **Hard vetos require loss evidence, not theoretical risk.** Single-trade loss = soft caution only.
- **Volume ratio gates**:
  - < 0.8x: require composite ≥ 45
  - 0.8–0.9x: acceptable at composite ≥ 40; add caution flag
  - ≥ 0.9x: standard entry; 0.99x vs 1.0x = noise
- **Sentiment gate**:
  - Sentiment < −15: hard block
  - Sentiment −15 to −5: require composite ≥ 35
  - Sentiment < 0 + composite ≥ 50: acceptable with catalyst
- **Sentiment-driven composite inflation**: Narrative/sentiment catalysts can inflate scores without price follow-through. Require corroborating technical signals when sentiment score > 50 and near-term revenue impact is limited.
- **MACD histogram**: Soft flag only, not hard block. `macd_bearish(x)` flagged for day-trade entries. Crossover setups (positive at entry) remain valid.
- **Momentum-reversal exit**: If day-trade is down 0.5–1.5% AND MACD histogram was positive at entry but is now negative → close early, reason `momentum_reversal`.
- **Stop-hunt gate**: Threshold 0.1% with $1.00 boundaries. Selective — not a near-universal block.

---

## Market Regime Playbook

| Regime | VIX Condition | Action |
|---|---|---|
| BULL_TREND | VIX < 90d avg, macro ≥ 60 | Full rules, default toward entry on qualified candidates |
| NORMAL | VIX near avg, mixed signals | Standard filters, composite ≥ 50 for full size |
| HIGH_VOL | VIX >10% above 90d avg or accelerating | 0.75x size multiplier, raise composite bar to 55 |
| CRISIS | VIX spike + HYG collapse | Halt new longs; cash default |

- **Rising VIX within BULL_TREND** (current: VIX 15.9, rising, 90d avg 17.59): Monitor HYG trend. Weak HYG 5d return (−0.21%) signals subtle credit stress — thesis_break exits more likely. Require higher composite for sentiment-heavy setups.
- **Consecutive green days ≥ 3**: Valid entries still exist; avoid chasing extended RSI. rsi_buy_max = 65.
- In AI_SEMI/BROAD_TECH both at 100 regimes: default toward entry rather than finding reasons to skip.

---

## Entry Filter Rules

- **Time window**: 10:00–15:00 ET
- **Late entry floor**: composite ≥ 68 after 10:00 AM (confirmed optimal vs prior 11 AM / ≥ 72 rule; WR 63.9% vs 59.2%)
- **RSI**:
  - rsi_buy_max = 65 (NORMAL and HIGH_VOL)
  - RSI 68–72: caution flag, not hard skip
  - RSI > 72: hard skip
  - Choppy market rsi_max = 60
- **VWAP slope**: vwap_slope_min = −0.2 (entries below flat VWAP require strong composite)
- **Stochastic 88–93**: No longer a hard block. In trending markets (AI_SEMI/BROAD_TECH 100), momentum stocks naturally sit here.
- **Composite minimum**: 50 for full-size NORMAL entries; position sizing guardrails functioning correctly (UBER half-size at 45.82 limited loss to −$10.8)

---

## Per-Stock Notes

- **USAR**: +5.0% / +$243. Composite 81.3, 4-indicator consensus. Archetype full-size entry.
- **DRAM (2026-05-07)**: −3% SL. Composite 26.85, RSI delta −9.0, +70% parabolic. Lesson: composite < 25 = skip.
- **SYM (2026-05-07)**: −3% SL. Composite 76.49, neutral volume on 2/6 indicators. Lesson: high composite + weak volu