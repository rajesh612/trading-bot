# strategy_memory.md
*Last updated: 2026-06-02*

---

## General Rules

- **Position sizing**:
  - Composite ≥ 50 (NORMAL): full size
  - Composite 25–49: half size
  - Composite < 25: skip
  - HIGH_VOL regime: multiply by 0.75 (only if VIX >10% above 90d avg or accelerating)
- **Stop-loss**: 4.0% ceiling. Do not tighten — lower SL roughly doubles hits with worse net P&L.
- **Take-profit**: 5.0% (ATR multiplier 2.5x as guide)
- **Hard vetos require loss evidence, not theoretical risk.** Single-trade loss = soft caution only.
- **Volume ratio gates**:
  - < 0.8x: require composite ≥ 45
  - 0.8–0.9x: acceptable at composite ≥ 40; add caution flag
  - ≥ 0.9x: standard entry
- **Sentiment gate**:
  - Sentiment < −15: hard block
  - Sentiment −15 to −5: require composite ≥ 35
  - Sentiment < 0 + composite ≥ 50: acceptable with catalyst
- **Sentiment-driven composite inflation**: Require corroborating technical signals when sentiment score > 50 and near-term revenue impact is limited.
- **MACD histogram**: Soft flag only, not hard block. Crossover setups (positive at entry) remain valid.
- **Momentum-reversal exit**: If day-trade is down 0.5–1.5% AND MACD histogram was positive at entry but is now negative → close early, reason `momentum_reversal`.
- **Stop-hunt gate**: Threshold 0.1% with $1.00 boundaries. Selective — not a near-universal block.
- **Restored positions (startup)**: Positions lacking original entry metadata (composite score, indicator signals, regime) must be flagged immediately and closed at open, not carried to EOD. These introduce unquantifiable risk and produce unusable performance data.

---

## Market Regime Playbook

| Regime | VIX Condition | Action |
|---|---|---|
| BULL_TREND | VIX < 90d avg, macro ≥ 60 | Full rules, default toward entry on qualified candidates |
| NORMAL | VIX near avg, mixed signals | Standard filters, composite ≥ 50 for full size |
| HIGH_VOL | VIX >10% above 90d avg or accelerating | 0.75x size multiplier, raise composite bar to 55 |
| CRISIS | VIX spike + HYG collapse | Halt new longs; cash default |

- **Current (2026-06-02)**: BULL_TREND. VIX 16.04, rising, 90d avg 17.47. 4 consecutive green days.
- **Rising VIX within BULL_TREND**: Monitor HYG trend. HYG 5d −0.41% = subtle credit stress. Require higher composite for sentiment-heavy setups. If VIX crosses above 90d avg, tighten rsi_buy_max_high_vol toward 55–60 and apply 0.75x multiplier.
- **Consecutive green days ≥ 3**: Valid entries still exist; avoid chasing extended RSI. rsi_buy_max = 65.
- **AI_SEMI + BROAD_TECH both at 100**: Default toward entry rather than finding reasons to skip.

---

## Entry Filter Rules

- **Time window**: 10:00–15:00 ET
- **Late entry floor**: composite ≥ 68 after 10:00 AM (WR 63.9% vs 59.2% for prior 11 AM / ≥ 72 rule)
- **RSI**:
  - rsi_buy_max = 65 (NORMAL and HIGH_VOL)
  - RSI 68–72: caution flag, not hard skip
  - RSI > 72: hard skip
  - Choppy market rsi_max = 60
- **VWAP slope**: vwap_slope_min = −0.2 (entries below flat VWAP require strong composite)
- **Stochastic 88–93**: No longer a hard block. In trending markets (AI_SEMI/BROAD_TECH 100), momentum stocks naturally sit here.
- **Composite minimum**: 50 for full-size NORMAL entries

---

## Indicator Weights

| Indicator | Weight (%) |
|---|---|
| RSI | 19.37 |
| VWAP | 18.96 |
| EMA 9/21 Crossover | 13.34 |
| Bollinger Bands | 11.76 |
| RSI Slope | 9.89 |
| VWAP StdDev Bands | 7.65 |
| Daily Volume Trend | 6.48 |
| Stochastic Oscillator | 5.74 |
| OBV