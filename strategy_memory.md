# Trading Strategy Memory
*Last updated: 2026-05-07*

---

## General Rules

- Entry window: **10:00–15:00 ET** only
- Stop-loss: 3.0% | Take-profit: 5.0%
- Composite gate: **≥ 65** (below = skip or minimum size only)
- Post-15:00 entries: composite must be **≥ 65**, not standard threshold
- Do not chase: price > 3% above intraday VWAP → wait for pullback
- EOD force-close at 15:55; marginal conviction trades (composite < 65) rarely reach target
- Parabolic prior run (>40% recent) = **hard disqualifier or min size only**
- When reasoning layer explicitly flags momentum deterioration → **hard block or 50% size reduction minimum**

---

## Market Regime Playbook

### NORMAL (VIX < 90d avg, SPY neutral/positive)
- Position size: full | RSI gate: 50–65 | Composite ≥ 65
- Stop: 3.0% | Target: 5.0%
- After 2+ consecutive green days + SPY 5-day >+2%: treat as **overbought intraday risk** — require Volume Ratio ≥ 1.0x confirmation; neutral volume = skip
- VWAP + EMA 9/21 most reliable; require ≥ 4-indicator consensus

### HIGH_VOL (VIX > 90d avg or rising fast)
- Position size × 0.5 | RSI ≤ 65 | Composite > +20
- Stop: 2.5% | Target: 4.0%

### CRISIS (VIX > 35 or SPY < −2%)
- **No new entries.**

---

## Entry Filter Rules

### Time-of-Day
- Valid: **10:00–15:00 ET**
- Avoid first 30 min (gap noise) and last 5 min (force-close only)

### RSI Filter
- Momentum entries: RSI **50–65** *(tightened from 72 on 2026-05-07)*
- RSI > 65 = skip unless composite > +50 + extraordinary catalyst
- RSI > 72 = **hard skip**
- RSI < 40 + composite > +5 + volume > 1.5x = mean-reversion only
- **RSI delta (slope) is a hard filter**: delta ≤ −7 = skip or 50% size max, no exceptions

### Stochastic Filter (HARD VETO)
- Stochastic > 88 = **do not enter**

### VWAP Slope Filter
- Skip longs when slope < −0.2%
- Earnings exception: sentiment > 70 may override VWAP filter

### Composite Score Gate
- **≥ 65**: full-size entry (4-indicator consensus preferred)
- **40–64**: half-size only; requires Volume Ratio ≥ 1.0x + RSI delta positive
- **< 40**: **skip** — composite 26.85 (DRAM 2026-05-07) = confirmed trap
- Archetype benchmark: USAR 81.3 (VWAP + RSI + MACD + EMA all aligned = full size)

### Volume Confirmation (NEW — 2026-05-07)
- Neutral volume on ≥ 2 of 6 indicators in choppy/overbought conditions = **skip**
- Volume Ratio < 0.8x = reduce size 50%

### Sentiment Gate
- Sentiment < −10: require composite > +50

---

## Indicator Weights (Updated 2026-05-07)

| Indicator | Weight | Notes |
|---|---|---|
| RSI | **18.18%** | Core filter; 50–65 zone; delta is hard block |
| VWAP | **17.80%** | Primary anchor; false signal in overbought chop |
| EMA 9/21 Crossover | 12.52% | Confirming; insufficient alone in choppy days |
| Bollinger Bands | 11.05% | Neutrality on weak setups = mild warning |
| VWAP StdDev Bands | 7.19% | Slope direction is the signal |
| Daily Volume Trend | 6.08% | Confidence amplifier; neutral = caution flag |
| RSI Slope | **9.29%** | Elevated — best warning signal 2026-05-07 |
| Stochastic Oscillator | 5.39% | Hard veto >88 |
| MACD | 2.35% | Reduced — misleading in choppy intraday regimes |
| ATR | 3.59% | Risk-sizing only |
| OBV | 3.59% | Insufficient signal data |
| Volume Ratio | 2.97% | Neutral reading = caution; not directional primary |

---

## Per-Stock Notes

- **USAR**: +5.0% / +$243. Composite 81.3, all 4 core indicators aligned. **Archetype trade** — benchmark for full-size entry.
- **DRAM (2026-05-05)**: +0.14% / +$7. Composite 55.4, late entry 15:55, EOD force-close killed trade.
- **DRAM (2026-05-07)**: −3% stop-loss. Composite 26.85 (below gate), RSI delta −9.0 flagged in reasoning but ignored, +70% prior parabolic run active. **Classic process failure** — all red flags present, trade should not have been entered.
- **SYM (2026-05-07)**: −3% stop-loss. Composite 76.49 (high score insufficient alone); Volume neutral = early warning ignored. High composite ≠ momentum follow-through without volume.
- **ACHR (2026-05-07)**: −3% stop-loss. Composite 45.4, sentiment 55, neutral on 2/6 indicators. Weakest