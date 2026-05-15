# Trading Strategy Memory
*Last updated: 2026-05-15*

---

## General Rules

- Entry window: **10:00–15:00 ET** only
- Stop-loss: 3.0–4.0% | Take-profit: 5.0%
- EOD force-close at 15:55 — do not enter after 14:55 unless composite ≥ 65
- Parabolic prior run (>40% recent) = hard disqualifier unless PEAD catalyst confirmed

---

## Market Regime Playbook

### NORMAL (VIX near or below 90d avg, SPY neutral/positive)
- Position size: full | RSI gate: 45–68 | Composite ≥ 50 (preferred full-size)
- Composite 25–49: half-size only, requires volume ≥ 0.9x + RSI 45–65
- Stop: 3.0% | Target: 5.0%
- After 2+ consecutive green days: prefer volume ≥ 0.9x but do not hard-block on volume alone

### HIGH_VOL (VIX > 90d avg or rising fast)
- Position size × 0.5 | RSI ≤ 65 | Composite > 35
- Stop: 2.5% | Target: 4.0%

### CRISIS (VIX > 35 or SPY < −2%)
- **No new entries.**

---

## Entry Filter Rules

### Composite Score Gate
- **≥ 65**: full-size entry (4-indicator consensus preferred)
- **50–64**: full-size if volume ≥ 1.0x; half-size if volume 0.9–1.0x
- **25–49**: half-size only; requires RSI 45–65 + sentiment not hard-negative + volume ≥ 0.9x
- **< 25**: skip — insufficient signal

### RSI Filter
- Preferred range: RSI **45–68**
- RSI > 68: skip unless composite ≥ 55 + confirmed strong catalyst (earnings beat, PEAD)
- RSI > 72: hard skip (code gate applies — trust the system)
- RSI < 40 + composite > 0 + volume > 1.5x: mean-reversion only, half-size max
- RSI delta (slope): delta ≤ −7 is a **soft caution signal only** — do not hard-block; for composite ≥ 50 + catalyst, enter at half-size

### Stochastic Filter
- Stochastic > 93: code hard veto applies — trust the system
- Stochastic 80–93: caution; prefer composite ≥ 45 before entering
- PEAD/strong earnings exception: composite ≥ 65 + confirmed earnings beat → Stochastic up to 95 acceptable

### VWAP Filter
- VWAP slope < −0.2%: soft warning — use judgment, not a hard block
- Price > VWAP+2σ: code hard block applies (overextension) — trust the system
- Earnings exception: sentiment > 70 may override VWAP slope filter

### Volume Confirmation
- Volume ratio < 0.8x: reduce size 50%
- Volume ratio 0.8–0.9x: acceptable for composite ≥ 40; caution flag only
- Volume ratio ≥ 0.9x: standard entry
- Do NOT hard-block on 0.99x vs 1.0x — that boundary is noise

### Sentiment Gate
- Sentiment < −15: hard block (code gate applies)
- Sentiment −15 to −5: require composite ≥ 35
- Sentiment < 0 + composite ≥ 50: acceptable with catalyst

---

## Per-Stock Notes

- **USAR**: +5.0% / +$243. Composite 81.3, 4-indicator consensus. Archetype full-size entry.
- **DRAM (2026-05-07)**: −3% SL. Composite 26.85, RSI delta −9.0, +70% parabolic. Composite < 25 = skip.
- **SYM (2026-05-07)**: −3% SL. Composite 76.49 but neutral volume on 2/6 indicators. High composite alone insufficient.

---

## Lessons (2026-05-08 to 2026-05-15)

- 8 zero-trade days: overfitted rules (composite < 40 skip + RSI delta hard block + Stochastic > 88) combined to block every setup
- In trending markets, high-composite stocks have elevated Stochastic and volume near 1.0x — hard-blocking those is not safety, it is inaction
- Single-trade loss evidence = soft caution only; hard blocks require multiple examples
- Composite 25–49 = Claude discretion, not auto-skip
