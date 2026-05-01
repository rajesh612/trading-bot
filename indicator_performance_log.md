# Indicator Performance Log

*Daily entries appended automatically by the Learning Agent after market close.*

---

## 2026-04-23

**Regime:** NORMAL | VIX=19.1 (90d avg=19.6) | SPY=-0.06%
**Trades:** 6 total | W:3 L:3 | Win rate: 50.0% | P&L: $-2547.43
**Market conditions:** mixed | Regime impact: VIX at 19.1 (NORMAL regime, below 90d avg of 19.64) with rising VIX and near-flat SPY (-0.06%) created a mild risk-off undercurrent. Mean-reversion setups on oversold stocks (SBSW) worked marginally, but momentum/dip-buy trades (PARR, AMD, CCL) failed as the subtle bearish drift punished entries against prevailing downward pressure. Rising VIX warrants increased caution even in NORMAL regime.

### Indicator Performance

- **VWAP**: accuracy=17% | weight 20.0% → 17.71% (-3%) | VWAP was marked correct only once (AMD) out of 6 trade instances, with all other entries below VWAP failing to bounce as expected — the indicator provided poor directional guidance today.
- **RSI**: accuracy=50% | weight 12.0% → 14.58% (+2%) | RSI was correct in the SBSW trades (deep oversold RSI ~24 led to small profitable mean-reversion) but incorrect in PARR, AMD, and CCL, suggesting oversold RSI is useful only when paired with genuine volume confirmation.
- **Volume Ratio**: accuracy=50% | weight 12.0% → 14.58% (+2%) | Volume was correct in SBSW (4.82x ratio aligned with winning trades) but incorrect in PARR (low 0.71x) and AMD (low 0.71x) where low volume foreshadowed weakness — high volume ratio was a reliable positive signal today.
- **MACD**: accuracy=0% | weight 10.0% → 7.29% (-3%) | MACD was marked incorrect across all 6 trade instances without a single correct signal, consistently showing bearish histograms that correctly warned against entries but were overridden by sentiment.
- **EMA 9/21 Crossover**: accuracy=0% | weight 10.0% → 8.33% (-2%) | EMA was incorrect in all 6 instances, with bearish EMA alignment (price below EMA9/EMA21 in most cases) proving to be a valid warning that was ignored in favour of sentiment-driven entries.
- **Bollinger Bands**: accuracy=8% | weight 10.0% → 9.38% (-1%) | BB was incorrect in 5 of 6 instances and neutral in 1; entries at or near lower BB did not trigger mean-reversion bounces in today's weak market, reducing BB's utility as a standalone entry trigger.
- **VWAP StdDev Bands**: accuracy=17% | weight 10.0% → 9.38% (-1%) | VWAP StdDev Bands contributed to the VWAP signal set and similarly failed — entries at the lower 2-sigma band (CCL, SBSW) did not reliably bounce, warranting slight weight reduction.
- **ATR**: accuracy=50% | weight 8.0% → 8.33% (0%) | ATR provided reasonable volatility context but PARR's very low ATR (0.147) relative to the catastrophic -50% move suggests a data/pricing anomaly rather than ATR failure; keeping weight unchanged pending investigation.
- **Stochastic Oscillator**: accuracy=33% | weight 5.0% → 7.29% (+2%) | Stochastic %K at near-zero extremes (SBSW: 0.0, CCL: 7.0) was partially predictive — SBSW profited while CCL lost, but stochastic correctly identified the most extreme oversold condition (SBSW) that did bounce.
- **OBV**: accuracy=33% | weight 3.0% → 3.12% (0%) | OBV was negative for AMD and CCL (bearish accumulation) which correctly foreshadowed losses, and positive for SBSW which won — signal was present but not prominently weighted in decisions; no change warranted.

### Param Changes

- entry_end_hour: 15 → 14
- rsi_buy_max: 45 → 40
- rsi_buy_max_high_vol: 35 → 32
- vwap_slope_min: -0.3 → -0.2
- stop_loss_pct: 4.0 → 3.0

### Strategy Learnings

- Sentiment-driven overrides of deeply negative composite scores (-6.24, -14.2, -3.53) produced the worst outcomes — PARR's -50% move is a catastrophic example of ignoring a consensus bearish technical signal in favour of analyst upgrades.
- PARR's -50% intraday move on a stop-loss trigger warrants urgent investigation for a data/pricing error or corporate event (halving, reverse split, or bad tick); the entry price of $64.03 vs exit of $31.84 is not consistent with normal stop-loss execution.
- Low volume ratio entries (AMD: 0.71x, PARR: 0.71x) consistently failed — a hard filter requiring volume_ratio >= 0.85 before entry should be considered to avoid low-conviction dip-buys.
- Rising VIX (even within NORMAL regime) combined with bearish MACD and EMA alignment across multiple names signals a weak intraday trend environment — on such days, tightening rsi_buy_max and vwap_slope_min filters reduces poor entries.
- SBSW's three small profitable exits validate that extreme oversold conditions (RSI ~24, Stoch ~0) plus very high volume ratio (4.82x) can generate reliable mean-reversion scalps even when composite scores are deeply negative.
- Entries where composite_score is below -10 should require at minimum a positive volume_ratio (>2x) AND an RSI below 25 to justify override — single-factor news catalysts alone are insufficient.
- Tightening entry_end_hour to 14 is supported by the fact that all losing trades were closed around 13:36 after entering late, while SBSW's winning exits completed by 14:03 — earlier cutoffs preserve capital in afternoon drift conditions.
- Stop-loss of 4% allowed AMD and CCL to breach stops cleanly, but PARR's -50% suggests stop-loss enforcement or order execution needs review — consider tightening stop_loss_pct to 3% for normal regime entries with negative composite scores.

### Per-Stock Notes

- **PARR**: CRITICAL: -50.28% loss on a supposed 4% stop-loss is anomalous — investigate for a corporate event (reverse split, halt, bad tick) at the time of exit. Entry was deeply flawed: composite score -6.24, below VWAP, low volume, bearish MACD and EMA; Goldman upgrade sentiment alone should not have triggered a long.
- **AMD**: Small manageable loss (-$22); VWAP was the one correct signal (price above VWAP at entry). Negative MACD histogram and low volume ratio were reliable warnings that were partially heeded via moderate sizing. Entry composite of 9.91 was misleadingly positive given the bearish sub-indicators.
- **CCL**: RSI at 22 and Stoch %K at 7 did not produce the expected oversold bounce — all indicators were incorrect or neutral. Price entering at the exact lower 2-sigma VWAP band with a negative composite (-3.53) and negative VWAP slope was a marginal setup; entry should have been skipped.
- **SBSW**: The only net-positive symbol today. Three winning exits (bracket legs) on a small mean-reversion play validated by extreme RSI (23.8), zero Stochastic, and very high volume ratio (4.82x). The VWAP slope at -0.22 was within the current filter (-0.3) but just barely; tightening vwap_slope_min to -0.2 would have still allowed this trade.

---

## 2026-04-30

**Regime:** NORMAL | VIX=17.4 (90d avg=19.8) | SPY=+0.38%
**Trades:** 16 total | W:7 L:9 | Win rate: 43.8% | P&L: $-1106.07
**Market conditions:** mixed | Regime impact: Normal VIX at 17.37 (below 90-day avg of 19.77) with a mildly positive SPY day (+0.38%) suggested a benign environment, but the session was choppy with divergent outcomes across names — momentum setups worked for some (POWL, TTMI, WLDN) but failed for others (FN was a catastrophic outlier, CRDO reversed despite clean technicals). The low-vol regime did not provide the broad momentum lift one might expect, suggesting stock-specific catalysts dominated over macro tailwinds.

### Indicator Performance

- **VWAP**: accuracy=67% | weight 20.0% → 19.44% (+1%) | VWAP was 'correct' in 6 of 9 scored trades, with the incorrect calls (TTMI, MU, KGC) all still producing mixed-to-positive outcomes, suggesting VWAP remains the strongest single filter but is not infallible when strong catalysts override price-vs-VWAP positioning.
- **RSI**: accuracy=89% | weight 15.0% → 16.67% (+3%) | RSI was 'correct' in 8 of 9 scored trades, making it the most consistently reliable indicator today — the sole incorrect signal (CRDO) still showed a textbook RSI setup that was overridden by adverse price action, reinforcing RSI's edge.
- **Daily Volume Trend**: accuracy=55% | weight 10.0% → 8.33% (-1%) | Volume was frequently 'neutral' in scored trades and where it was 'correct' it did not strongly differentiate winners from losers, limiting its incremental value today.
- **MACD**: accuracy=44% | weight 9.0% → 6.48% (-2%) | MACD was 'incorrect' in 4 of 9 scored trades (VISN, GOOGL, LITE, and CRDO had mixed/incorrect readings), and the incorrect signals appeared across both winning and losing trades, reducing its reliability signal today.
- **EMA 9/21 Crossover**: accuracy=89% | weight 12.0% → 12.96% (+2%) | EMA 9/21 crossover was 'correct' in 8 of 9 scored trades, consistently aligning with price direction — its near-perfect read today supports a modest weight increase.
- **Bollinger Bands**: accuracy=60% | weight 9.0% → 8.33% (0%) | BB was frequently 'neutral' (4 of 9 scored trades) and 'correct' in 4 cases, offering limited directional edge today — no adjustment warranted given insufficient signal strength.
- **VWAP StdDev Bands**: accuracy=50% | weight 8.0% → 7.41% (0%) | Insufficient direct signal data in today's scored trades to assess VWAP StdDev Bands independently — maintaining current weight.
- **ATR**: accuracy=50% | weight 4.0% → 3.7% (0%) | ATR is a risk-sizing input rather than a directional signal and cannot be evaluated from today's trade outcomes alone — no change warranted.
- **Stochastic Oscillator**: accuracy=40% | weight 8.0% → 5.56% (-2%) | Extended Stochastic readings (91+ in KGC, VISN) were flagged in reasoning but entries were still taken — in both cases the trades underperformed, suggesting overbought Stochastic should be treated as a harder veto filter.
- **OBV**: accuracy=50% | weight 4.0% → 3.7% (0%) | OBV was not explicitly reported in indicator signals and cannot be evaluated from today's data — holding current weight.
- **RSI Slope**: accuracy=78% | weight 6.0% → 6.48% (+1%) | Positive RSI slope was cited as supportive across multiple winning trades (POWL, TTMI, MU, KGC) and the one clear loser (CRDO) also had a favorable slope at entry, but the broader correlation with wins is encouraging.
- **Volume Ratio**: accuracy=70% | weight 0.0% → 0.93% (+1%) | Volume Ratio (currently at 0 weight) showed 'correct' signals in TTMI and AU among scored trades — elevating slightly to give it meaningful signal weight given its directional correlation today.

### Param Changes

- rsi_buy_max: 75 → 72
- vwap_slope_min: -0.3 → -0.2

### Strategy Learnings

- The FN position (-$2,531 / -49%) is a catastrophic outlier that single-handedly caused today's net loss despite 7 other profitable trades — a hard position-size cap or max-dollar-loss-per-trade rule is urgently needed regardless of composite score.
- MACD 'incorrect' signals did not prevent wins (GOOGL, LITE both profitable despite wrong MACD) — MACD may be adding noise rather than signal in low-vol trending regimes and its weight reduction is warranted.
- Overbought Stochastic (>88) at entry correlated with underperformance in VISN and was flagged but overridden in KGC — this should be implemented as a hard filter veto, not just a note in reasoning.
- TTMI succeeded despite VWAP being 'incorrect' (price below VWAP) because a blowout earnings catalyst was strong enough to override the technical headwind — strong fundamental catalysts (sentiment >70) may warrant VWAP filter relaxation.
- RSI and EMA 9/21 were the two most accurate indicators across all scored trades today (both ~89%) — these two should form the core technical entry criteria.
- Restored startup trades with no entry data (CAAP, FN, SKYW, WLDN, POWL, GM) represent a risk management gap — position data at startup should carry forward original entry parameters including risk limits to prevent uncontrolled drawdowns.
- CRDO presented a textbook setup (composite +42, bullish news, all technicals aligned) yet hit stop-loss — even high-quality signals fail in choppy intraday conditions, reinforcing the need for portfolio-level diversification rather than concentrated bets.
- The mildly bullish macro environment (SPY +0.38%, VIX below 90-day avg) suggested a trend day but intraday reversals were common — slight tightening of vwap_slope_min to -0.2 would have filtered some of the weaker setups.

### Per-Stock Notes

- **CAAP**: Profitable take-profit at +5.49% but no entry data available for indicator analysis — positive outcome.
- **FN**: Catastrophic -49% stop-loss hit; with no entry data this cannot be diagnosed, but a loss of this magnitude on a single position suggests either extreme gap-down event or position sizing far too large — implement hard dollar stop per position.
- **SKYW**: -8.13% stop-loss with no entry data; second-largest dollar loser of the day, reinforcing concentration risk in startup-restored positions.
- **WLDN**: Clean +6.64% take-profit on a restored position — no entry data to analyze.
- **POWL**: Double win day (+8.54% and +4.30%) on two separate POWL positions — strong momentum stock that delivered on both take-profit and bracket targets.
- **CRDO**: Textbook setup that failed — composite +42, bullish AI news, clean RSI/VWAP/EMA alignment, yet stopped out at -3%. Suggests even high-conviction setups in individual names can reverse sharply; consider reducing position size when RSI >58 in volatile individual names.
- **TTMI**: Best risk-adjusted new trade of the day (+6.12%) despite VWAP being 'incorrect' — blowout earnings catalyst (record revenue) was strong enough to override the below-VWAP entry, validating that earnings momentum can supersede VWAP filter.
- **VISN**: High composite (+49.4) and strong catalyst failed to generate profit (closed -1.69% at EOD) — RSI at 70 and Stochastic at 91.8 at entry flagged as overextended; overbought entry timing eroded the catalyst edge.
- **GOOGL**: Modest +1.91% EOD close on earnings beat setup — earnings catalyst trades tend to mean-revert intraday; consider earlier take-profit targets on post-earnings plays.
- **GM**: Small +1.70% EOD gain on restored position — no entry data for analysis.
- **LITE**: Near-breakeven (-0.11%) on a well-scored setup (composite +36.7, clean technicals) — PT upgrade catalysts may not provide enough intraday thrust; sector headwind from Corning may have capped upside.
- **SNDK**: Flat (-0.49%) despite all indicators aligned and composite +43.5 — 'sell-the-news' risk on a 348% YTD stock materialized as warned in reasoning; extreme prior gains are a reliable caution flag that should be weighted more heavily.
- **AU**: -2.97% EOD on gold miner despite strong momentum setup (composite +57.2, all indicators correct) — bearish sentiment score of -12 proved more predictive than the technical setup; future entries with negative sentiment should require higher composite score threshold.
- **MU**: Near-flat (-0.19%) on a solid setup — AI/HBM catalyst and strong technicals kept it from losing but macro overhang limited upside; reasonable outcome given mixed signals.
- **KGC**: Near-flat (-0.38%) on gold miner with overbought Stochastic (92.8) at entry — the overbought warning materialized as predicted; reinforces Stochastic >90 as a hard veto filter.

---
