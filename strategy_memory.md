# Trading Strategy Memory
*Last updated: 2026-05-19*

---

## General Rules

- Entry window: **10:00–15:00 ET** only
- Stop-loss: 3.0–4.0% | Take-profit: ATR-based (ATR% × 2.5, floor 2.0%, ceiling 12%)
- EOD force-close at 15:55 — do not enter after 14:55 unless composite ≥ 65
- Parabolic prior run (>40% recent) = hard disqualifier unless PEAD catalyst confirmed

---

## Market Regime Playbook

### NORMAL (VIX near or below 90d avg, SPY neutral/positive)
- Position size: full | RSI gate: 45–68 | Composite ≥ 50 (preferred full-size)
- Composite 25–49: half-size, requires RSI 45–65 + volume ≥ 0.9x
- Stop: 3.0% | Target: 5.0%
- After 2+ consecutive green days: volume ≥ 0.9x preferred, not required

### HIGH_VOL (VIX > 90d avg or rising fast)
- **Today's note**: VIX 19.22 vs 18.74 avg — marginally elevated, treat as NORMAL unless VIX accelerates >10% above avg
- Position size × 0.75 (relaxed from 0.5 — no loss data supporting full half-size penalty at this VIX level)
- RSI ≤ 68 | Composite > 35 | Stop: 2.5% | Target: 4.0%

### CRISIS (VIX > 35 or SPY < −2%)
- **No new entries.**

---

## Entry Filter Rules

### Composite Score Gate
- **≥ 65**: full-size entry
- **50–64**: full-size if volume ≥ 1.0x; half-size if volume 0.9–1.0x
- **25–49**: half-size; requires RSI 45–65 + sentiment not hard-negative + volume ≥ 0.9x
- **< 25**: skip — insufficient signal

### RSI Filter
- Preferred range: RSI **45–68**
- RSI 68–72: caution flag; enter at half-size if composite ≥ 50 *(relaxed — no loss data for RSI 68–72 block)*
- RSI > 72: hard skip (overbought, trust code gate)
- RSI < 40 + composite > 0 + volume > 1.5x: mean-reversion only, half-size max
- RSI delta ≤ −7: **soft caution only** — not a block; composite ≥ 50 + catalyst → enter half-size

### Stochastic Filter
- Stochastic > 97: hard veto (code gate — trust it)
- Stochastic 88–97: **soft caution only, not a block** *(relaxed — prior hard block at >88 contributed to 8 zero-trade days with no supporting loss data)*; require composite ≥ 45
- Stochastic 80–88: acceptable; prefer composite ≥ 40
- PEAD/earnings exception: composite ≥ 65 + confirmed beat → Stochastic up to 95 acceptable

### VWAP Filter
- VWAP slope < −0.2%: **hard block at pre-filter stage** — these stocks never reach Claude; do not adjust
- Price > VWAP+2σ: hard block (overextension — trust code gate)
- VWAP slope ≥ −0.2%: standard entry; rising VWAP preferred
- Earnings exception: not applicable (VWAP slope block is pre-Claude; cannot override here)

### Volume Confirmation
- Volume ratio < 0.8x: reduce size 50%
- Volume ratio 0.8–0.9x: acceptable for composite ≥ 40; caution flag
- Volume ratio ≥ 0.9x: standard entry
- 0.99x vs 1.0x boundary = noise, do not differentiate

### Sentiment Gate
- Sentiment < −15: hard block (code gate)
- Sentiment −15 to −5: require composite ≥ 35
- Sentiment < 0 + composite ≥ 50: acceptable with catalyst

---

## Per-Stock Notes

- **USAR**: +5.0% / +$243. Composite 81.3, 4-indicator consensus. Archetype full-size entry.
- **DRAM (2026-05-07)**: −3% SL. Composite 26.85, RSI delta −9.0, +70% parabolic. Lesson: composite < 25 = skip.
- **SYM (2026-05-07)**: −3% SL. Composite 76.49, neutral volume on 2/6 indicators. Lesson: high composite + weak volume = size down.

---

## Recent Lessons

- **Stochastic 88–93 hard block removed**: 8+ zero-trade days with no loss evidence at that range. In trending markets (AI_SEMI/BROAD_TECH both 100), momentum stocks naturally sit here. Blocking it is inaction, not safety.
- **HIGH_VOL size penalty relaxed at marginal VIX**: VIX 19.22 vs 18.74 avg is noise. Full half-size penalty requires VIX meaningfully elevated (>10% above avg or accelerating).
- **RSI 68–72 relaxed to caution**: No documented losses in that band. Hard skip reserved for RSI > 72 only.
- **Core principle**: Hard vetos require loss evidence, not theoretical risk. Single-trade loss = soft caution. Multiple losses = hard rule eligible.
- **Today (2026-05-15)**: Zero trades again. With AI_SEMI and BROAD_TECH both at 100, filtering out every candidate in this regime signals remaining rules may still be over-fitted. If candidates exist tomorrow, default toward entry at appropriate size rather than finding reasons to skip.
- **stop_hunt_zone gate fixed (2026-05-19)**: The gate had a mathematical defect — the 0.4% threshold with $0.50 spacing always fired for any stock above $62.50, blocking nearly every candidate regardless of actual stop-hunt risk. Fixed by changing threshold to 0.1% and using $1.00 boundaries. The gate now works selectively as intended. Do not treat stop_hunt_zone as a near-universal block.