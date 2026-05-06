# Trading Strategy Memory
*Last updated: 2026-05-05*

---

## General Rules

- Hard dollar cap: **$500 max loss per trade** — non-negotiable regardless of composite score.
- Stochastic > 88 at entry = **hard veto**. No override.
- Sentiment < −10 requires composite > +50 to enter.
- Stocks up > 200% YTD: apply "sell-the-news" caution flag.
- PARR 2026-04-23: −50% anomaly (bad tick likely) — avoid until investigated.
- Entry metadata must persist across restarts — missing data = unattributable losses (RBLX, RIVN 2026-05-05).
- **Late entries (after 15:00):** Require composite ≥ 65 (up from 55). EOD force-close kills marginal-conviction trades (DRAM: 55.4 composite, +0.14% only).

---

## Market Regime Playbook

### NORMAL (VIX < 90d avg, SPY neutral/positive)
- Entry window: **10:00–15:00 ET**
- Stop-loss: 3.0% | Take-profit: 5.0%
- Low-VIX trending days: VWAP + EMA 9/21 are most reliable — lean on these two.
- Diversify across names; stock-specific catalysts dominate even on calm days.

### HIGH_VOL (VIX > 90d avg or rising fast)
- Position size × 0.5 | RSI gate ≤ 65 | Composite > +20 required
- Stop-loss: 2.5% | Take-profit: 4.0%

### CRISIS (VIX > 35 or SPY < −2%)
- **No new entries.**

---

## Entry Filter Rules

### Time-of-Day
- Valid window: **10:00–15:00 ET**
- Avoid first 30 min (gap noise) and last 5 min (forced exits only).
- After 15:00: composite must be ≥ 65, not standard threshold.

### RSI Filter
- Momentum entries: RSI **50–72**
- High-vol regime: RSI ≤ 65
- RSI > 72 = skip unless composite > +50 + extraordinary catalyst
- RSI > 75 = hard skip
- RSI < 40 + composite > +5 + volume > 1.5x = mean-reversion only

### Stochastic Filter (HARD VETO)
- Stochastic > 88 = **do not enter**, no exceptions.

### VWAP Slope Filter
- Skip longs when slope < −0.2%
- Slope −0.2% to 0% = neutral, tradeable with other confirmation
- **Earnings exception**: sentiment > 70 may override VWAP filter (TTMI validated).

### Composite Score Gate
- Score > +15: BUY candidate — evaluate filters
- Score −10 to +15: HOLD unless exceptional catalyst
- Score < −10: skip
- **High-conviction benchmark**: USAR 81.3 composite (VWAP + RSI + MACD + EMA all aligned) = archetype full take-profit exit. Prioritize ≥ 4-indicator consensus.

### Sentiment Gate
- Sentiment < −10: require composite > +50.

---

## Momentum Playbook

1. Entry trigger: composite > +20, RSI 50–72, EMA9 > EMA21, VWAP slope > 0
2. Ideal entry: price at or near VWAP or VWAP +1σ pullback
3. Do not chase: price > 3% above intraday VWAP → wait for pullback
4. Volume: prefer ≥ 0.8x
5. Stop-loss: 3–4% | Take-profit: 5–8%
6. MACD alone does **not** veto entries when VWAP + EMA + BB + Volume confirm (DRAM: MACD wrong, trade profitable — composite correctly absorbed disagreement).

---

## Indicator Weights (Updated 2026-05-05)

| Indicator | Weight | Notes |
|---|---|---|
| VWAP | **20.80%** | Primary anchor; 100% accuracy 2026-05-05 |
| RSI | 16.18% | Core entry criterion; 50–72 momentum zone |
| EMA 9/21 Crossover | **14.52%** | 100% accuracy 2026-05-05; elevated |
| Daily Volume Trend | 8.08% | Confidence amplifier only |
| Bollinger Bands | 9.05% | Confirming filter; not primary signal |
| VWAP StdDev Bands | 7.19% | Slope direction is the signal |
| RSI Slope | 6.29% | Rising slope amplifies bullish case |
| Stochastic Oscillator | 5.39% | Hard veto at >88 |
| MACD | **4.35%** | Reduced — lags in momentum/low-vol regimes |
| ATR | 3.59% | Risk-sizing only, not directional |
| OBV | 3.59% | Insufficient recent signal data |
| Volume Ratio | **0.97%** | Minimal signal value; confidence amplifier only |

---

## Per-Stock Notes

- **USAR**: +5.0% / +$243 full take-profit. Composite 81.3, VWAP + RSI + MACD + EMA all aligned. **Archetype high-conviction trade** — use as benchmark.
- **DRAM**: +0.14% / +$7. Composite 55.4, MACD incorrect, entered 15:55 → EOD force-close. Borderline conviction + late entry = missed target. Apply post-15:00 composite ≥ 65 rule going forward.
- **RBLX**: −$149 (−3.06%). No entry data (startup restore). Root cause unknown — flag