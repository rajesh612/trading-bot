# Trading Strategy Memory
*Last updated: 2026-04-30*

---

## General Rules

- VWAP is the primary intraday anchor. Prefer longs above VWAP.
- Exit before 3:55 PM ET — no overnight holds.
- Never override a composite score below −10 on a single catalyst alone.
- **Hard position cap**: max loss per trade = $500 (dollar stop). FN lost −$2,531 in a single position — this rule is non-negotiable regardless of composite score.
- Stochastic > 88 at entry = **hard veto**. Do not override with reasoning. VISN and KGC both underperformed when this was ignored.
- Sentiment score < −10 requires composite > +50 to enter. AU's bearish sentiment (−12) was more predictive than its +57 composite.
- Stocks up > 200% YTD: apply "sell-the-news" caution flag — weight prior gains as a negative composite modifier. SNDK confirmed this risk.
- PARR 2026-04-23: −50% anomaly, likely bad tick. Avoid until investigated.

**RSI is directional, not mean-reversion:**
- RSI 50–72 = prime momentum buy zone (confirms uptrend)
- RSI > 72 = caution; > 75 = avoid new longs
- Do NOT require RSI < 40 for momentum entries

---

## Market Regime Playbook

### NORMAL (VIX < 90d avg, SPY neutral/positive)
- Entry window: **10:00 AM – 15:00 PM ET**
- Stop-loss: 3.0% | Take-profit: 5.0%
- Note: Low-VIX days can still be choppy with stock-specific catalysts dominating — do not assume broad momentum lift. Diversify across names.

### HIGH_VOL (VIX > 90d avg or rising fast)
- Position size × 0.5 | RSI gate tightens to 65 | Composite > +20 required
- Stop-loss: 2.5% | Take-profit: 4.0%

### CRISIS (VIX > 35 or SPY < −2%)
- No new entries.

---

## Entry Filter Rules

### Time-of-Day
- Valid entry window: **10:00 AM – 15:00 PM ET**
- Avoid first 30 min (gap noise) and last 5 min (forced exits only)

### RSI Filter
- Momentum entries (EMA9 > EMA21, VWAP slope > 0): RSI **50–72**
- High-vol regime: RSI ≤ 65
- RSI > 72 = skip unless extraordinary catalyst + composite > +50
- RSI < 40 + composite > +5 + volume > 1.5x = valid mean-reversion only

### Stochastic Filter (HARD VETO)
- Stochastic > 88 at entry = **do not enter**, regardless of composite score or catalyst

### VWAP Slope Filter
- Skip longs when VWAP slope < −0.2% (tightened from −0.3%)
- Slope −0.2% to 0% = neutral, tradeable with other confirmation
- **Earnings catalyst exception**: sentiment > 70 may warrant VWAP filter relaxation (TTMI validated: below VWAP but blowout earnings = winner)

### Composite Score Gate
- Score > +15: BUY candidate — evaluate catalyst and filters
- Score −10 to +15: HOLD unless exceptional catalyst
- Score < −10: skip

### Sentiment Gate
- Sentiment < −10: require composite > +50 to proceed

---

## Momentum Playbook

For stocks with strong multi-day trends (up > 10% in 5 days, strong catalyst):
1. Entry trigger: composite > +20, RSI 50–72, EMA9 > EMA21, VWAP slope > 0
2. Ideal entry: price at or near VWAP or VWAP +1σ pullback
3. Do not chase: price > 3% above intraday VWAP → wait for pullback
4. Volume: prefer ≥ 0.8x; paper trading fills regardless
5. Stop-loss: 3–4% | Take-profit: 5–8%

---

## Indicator Weights (Updated 2026-04-30)

| Indicator | Weight | Notes |
|---|---|---|
| VWAP | 19.44% | Primary anchor; 67% accuracy today |
| RSI | 16.67% | 89% accuracy — core entry criterion |
| EMA 9/21 Crossover | 12.96% | 89% accuracy — core entry criterion |
| Daily Volume Trend | 8.33% | Confidence amplifier only |
| Bollinger Bands | 8.33% | Neutral signal; trend confirmation only |
| VWAP StdDev Bands | 7.41% | Slope direction is the signal |
| MACD | 6.48% | Reduced — adds noise in low-vol trending regimes |
| RSI Slope | 6.48% | Rising slope amplifies bullish case |
| Stochastic Oscillator | 5.56% | Now a hard veto at >88, not just a note |
| ATR | 3.70% | Risk-sizing only, not directional |
| OBV | 3.70% | Holding; insufficient recent data |
| Volume Ratio | 0.93% | Slight signal value confirmed; confidence amplifier |

---

## Per-Stock Notes

- **POWL**: Double win day (+8.54%, +4.30%). Strong momentum stock — reliable on momentum playbook rules.
- **TTMI**: +6.12% despite below-VWAP entry. Blowout earnings (record revenue, sentiment >70) overrides VWAP filter. Earnings catalyst exception validated.
- **FN**: Catastrophic −49% / −$2,531. No entry data. Gap-down or extreme position size. Hard dollar stop ($500/trade) would have capped