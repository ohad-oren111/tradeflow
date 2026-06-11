# Phase-17 — Multi-Market Daily Trend (the breadth thesis) — running results ledger

Research-only (CLAUDE.md §0.5.208 MEASURE lane). Drives **no** prod path. Multi-session.
REPORT autonomy. `tools/eval/phase17/` only; data on disk at
`research/data/phase17/` (untracked, like the NQ tape).

**THESIS:** single-instrument intraday is picked-over (Phase-15/16 = NONE). Test whether
ONE robust slow-trend signal (Carver EWMAC, vol-scaled) across a DIVERSIFIED, low-
correlation micro-futures basket clears the bar where 1-min single-symbol did not. The
edge — if any — is the diversification, not any one market.

Pipeline (two staged gates):
- **STAGE 1 — DATA (this doc).** Backfill DAILY bars (read-only), build continuous
  back-adjusted (Panama) series per market, DATA-QA, report → **STOP for operator review**.
- **STAGE 2 — STRATEGY (only after greenlight).** One EWMAC signal, vol-targeted risk
  parity, real per-market costs, train/holdout split + pre-registered gates + deflated
  Sharpe over cumulative trials. NONE remains a valid verdict.

---

## STAGE 1 — DATA  ·  Session 29 (2026-06-11)  ·  VERDICT: **PASS (9/9) — STOP for review**

All nine core markets cleared the structural DATA-QA gates. Data written to
`research/data/phase17/<sym>_daily.csv` (+ `<sym>_rolls.csv`). **No strategy was run.**

### The basket (specs RESOLVED from IBKR, not memory — §0.5.97)

Probed read-only (clientId 117) against the paper gateway. `tick_value = multiplier ×
min_tick`. Commission per side is **NOT yet fixed** — IBKR micro commissions vary by
product/tier; Stage 2 must specify each from the real fee schedule (`to-verify`).

| sym | exch  | bucket | multiplier | min_tick | tick_value | note |
|-----|-------|--------|-----------:|---------:|-----------:|------|
| MES | CME   | equity   | 5      | 0.25   | $1.250 | Micro E-mini S&P 500 (one equity — MES≈MNQ≈M2K) |
| MGC | COMEX | metals   | 10     | 0.1    | $1.000 | Micro Gold |
| MHG | COMEX | metals   | 2500   | 0.0005 | $1.250 | Micro Copper |
| MCL | NYMEX | energy   | 100    | 0.01   | $1.000 | Micro WTI Crude |
| M6E | CME   | fx       | 12500  | 0.0001 | $1.250 | Micro EUR/USD |
| M6A | CME   | fx       | 10000  | 0.0001 | $1.000 | Micro AUD/USD |
| M6B | CME   | fx       | 6250   | 0.0001 | $0.625 | Micro GBP/USD |
| MBT | CME   | crypto   | 0.1    | 5.0    | $0.500 | Micro Bitcoin |
| 10Y | CBOT  | rates    | 1000   | 0.001  | $1.000 | Micro 10Y **Yield** (see caveat) |

**Not found as micros (dropped):** SIL (no micro silver), MNG (no micro NatGas on NYMEX).
**Optional adds (available, but correlated — see matrix):** MET (Micro Ether, ≈MBT),
M2K (Micro Russell, ≈MES).

### Method (continuous back-adjusted series)

1. **Per-contract daily fetch.** `reqContractDetails(includeExpired=True)` → the full
   chain; fetch each in-window contract's daily bars (`1 Y` ending at its expiry,
   `whatToShow=TRADES`, `useRTH=False`). Read-only, dedicated clientId.
2. **Liquidity filter** drops dead serial months (peak vol << basket peak).
3. **Front/back pointer roll** — advance only to the IMMEDIATE NEXT liquid contract, on a
   volume crossover within 75d of expiry or a 7d buffer backstop. (This replaced a first
   "max-volume within 9mo, monotonic" rule that let a noisy deep-month volume print
   ratchet **MCL** ~9 months off the true front → a −4.73% endpoint error. Fixed; MCL
   endpoint drift is now 0.00%. See `stitch.py`; self-test `python -m tools.eval.phase17.stitch`.)
4. **Panama back-adjust** (difference method, same convention as `tools.eval.data.roll_adjust`):
   newest→oldest, shift earlier bars by `new_close − old_close` at each roll so the latest
   contract stays broker-true and history is continuous. Volume never adjusted.

### DATA-QA table (all PASS)

Per-contract fetch reaches ~0.5–1y **before** the includeExpired chain's earliest expiry
(each contract carries a year of its own history), so spans run to 2023-06/09 for the
older markets — longer than the ~24mo the chain alone implies.

| sym | span | bars | contracts | rolls | last_close(adj) | live CONTFUT | drift | max\|daily ret\| (date) | worst roll-date move |
|-----|------|-----:|----:|----:|----:|----:|----:|----|----|
| MES | 2023-09-22..2026-06-11 | 759 | 9  | 8  | 5710→7412.5 | 7411.5 | +0.01% | 8.6% (2025-04-09 tariff) | 3.0σ |
| MGC | 2023-06-30..2026-06-11 | 753 | 12 | 11 | 4225.1 | 4225.1 | +0.00% | 11.9% (2026-01-30) | 5.2σ |
| MHG | 2023-06-30..2026-06-11 | 753 | 11 | 10 | 6.4045 | 6.3985 | +0.09% | 23.6% (2025-07-31 Cu tariff) | 2.3σ |
| MCL | 2023-09-22..2026-06-11 | 693 | 14 | 13 | 86.15 | 86.15 | +0.00% | 19.7% (2026-04-08 oil shock) | 4.9σ |
| M6E | 2023-12-19..2026-06-11 | 634 | 10 | 9  | 1.1623 | 1.158 | +0.37% | 2.2% | 1.0σ |
| M6A | 2023-12-19..2026-06-11 | 634 | 9  | 8  | 0.7049 | 0.7049 | +0.00% | 5.1% | 1.8σ |
| M6B | 2023-12-19..2026-06-11 | 634 | 10 | 9  | 1.3415 | 1.3418 | -0.02% | 1.6% | 1.1σ |
| MBT | 2023-06-30..2026-06-11 | 756 | 25 | 24 | 63770 | 63750 | +0.03% | 14.0% | 2.1σ |
| 10Y | 2024-05-01..2026-06-11 | 539 | 25 | 24 | 4.461 | 4.461 | +0.00% | 4.7% | 1.6σ |

Gates per series (all green): bars≥400 · no dup/NaN dates · monotonic · rolls present ·
**no residual roll spike (<6σ)** · no unexplained price spike (bucket-aware threshold) ·
max date-gap ≤6d (long weekends) · **broker-truth endpoint drift <2%** (all ≤0.37%).

### Data-quality caveats (honest)

- **MCL (crude) — near-expiry intraday print quality.** One clear bad-tick bar
  (2026-03-09: High 108.7 / Low 70.4 on an 84.0 close = 46% intraday range — a thin
  near-expiry print, 3 days before I roll off that contract) plus 9 bars with >15% range.
  **The corruption is in intraday H/L only — the CLOSES are broker-true** (endpoint drift
  0.00%; the −13%/+11%/+19.7% close moves are the *real* spring-2026 oil shock,
  corroborated by the 19.7% close day on 04-08). **Carver EWMAC trend AND Carver
  vol-targeting both use close-to-close**, so the bad H/L does NOT touch the Stage-2
  signal. It WOULD inflate an ATR-based vol estimate → Stage 2 should use close-to-close
  vol (Carver-standard), not ATR. Optional further clean-up: roll crude ~3d earlier.
- **10Y/2YY/30Y are priced in YIELD**, not bond price (last 10Y = 4.461 = % yield). A
  trend on a yield series is valid but **economically inverted vs a bond-price trend** —
  Stage 2 must decide sign convention (trend-follow yield, or negate to proxy price).
- Rates micros only **listed 2024-05** → 10Y is the binding constraint on a *common*
  basket window (~25 months). Per-market history is longer (gold/crypto ~3y).

### Diversification preview (the thesis precondition — descriptive, NOT the strategy)

Daily log-return correlation, common window **2024-05-02 .. 2026-06-11 (n=537)**:

```
        MES    MGC    MHG    MCL    M6E    M6A    M6B    MBT    10Y
 MES   1.00   0.13   0.27   0.02   0.02   0.46   0.20   0.44   0.04
 MGC   0.13   1.00   0.40   0.01   0.33   0.40   0.34   0.11  -0.14
 MHG   0.27   0.40   1.00   0.06   0.26   0.43   0.31   0.12  -0.07
 MCL   0.02   0.01   0.06   1.00  -0.22  -0.03  -0.17  -0.02   0.32
 M6E   0.02   0.33   0.26  -0.22   1.00   0.64   0.80   0.07  -0.34
 M6A   0.46   0.40   0.43  -0.03   0.64   1.00   0.68   0.24  -0.15
 M6B   0.20   0.34   0.31  -0.17   0.80   0.68   1.00   0.18  -0.31
 MBT   0.44   0.11   0.12  -0.02   0.07   0.24   0.18   1.00   0.05
 10Y   0.04  -0.14  -0.07   0.32  -0.34  -0.15  -0.31   0.05   1.00
```

- **mean |pairwise corr| = 0.244** → the basket is genuinely low-correlation. The breadth
  thesis precondition HOLDS. MCL (≈0 vs all) and 10Y (negative vs FX/gold) are the best
  diversifiers; MES-MBT (0.44) and MES-M6A (0.46) are mild risk-on co-moves.
- **The 3 FX micros are one tight cluster** (M6E-M6B 0.80, M6A-M6B 0.68, M6E-M6A 0.64 —
  the shared USD factor). For risk parity this matters: 3 FX at independent weights would
  over-allocate the portfolio to a single USD bet. **Decision for Stage 2:** down-weight
  the FX cluster (treat as ~1 bucket) or keep only 2 of the 3.

### Honest limitations (carry into Stage 2 — NONE remains a valid verdict)

- **Small sample.** Daily × 9 markets × ~25mo common window ≈ 537 common bars. A SLOW
  EWMAC (e.g. 64/256-day) burns 256 bars warming up → ~280 tradeable, at maybe 3–8 round
  trips per market over the window → an order of ~30–70 portfolio trades total. This is
  **directional evidence, not a decades-validated edge.** The deflated-Sharpe over the
  cumulative trial count (Phase-16 left us at 22 prior trials) will be demanding on so few
  trades — expect the gate to be hard to clear, and that is the correct, honest bar.
- Single ~25mo regime (one rate-cut cycle, one crypto bull leg, two tariff shocks) — not
  multi-cycle. Per-market longer history (to 2023-06) can lengthen the per-market view but
  not the *common* portfolio window (rates-bound).
- Paper-account feed; near-expiry crude print quality as above.

### Decisions requested before Stage 2 (the review gate)

1. **FX cluster** — down-weight to ~1 bucket, or drop one of M6E/M6A/M6B?
2. **Rates** — keep 10Y (accept the ~25mo common window + yield-pricing sign), or drop
   rates to lengthen the window using the longer-history markets?
3. **Optional adds** — include MET (crypto) / M2K (equity) despite ≈0.8 / high
   within-bucket correlation, or leave the 9-core basket?
4. **MCL vol** — confirm close-to-close vol (sidesteps the bad H/L), default yes.

---

## STAGE 2 — STRATEGY  ·  Session 30 (2026-06-11)  ·  VERDICT: **NONE**

GREENLIT after the Stage-1 review. One Carver EWMAC trend signal, vol-targeted, across the
9-market basket; anti-overfit (search train only, holdout touched once, deflated over the
cumulative campaign trials). Code: `tools/eval/phase17/` (`signal.py` `portfolio.py`
`costs17.py` `gates17.py` `run_strategy.py`); reproduce with
`python -m tools.eval.phase17.run_strategy --prior-trials 24`.

### Pre-registered gates (committed in `gates17.py` BEFORE results)

- **TRAIN** (~2024-05..2025-08): portfolio **PF ≥ 1.30** net; **profitable each** train year.
- **HOLDOUT** (~2025-09..2026-06, touched ONCE): **PF ≥ 1.30** net; profitable each holdout
  year; **PF ≥ 0.75 × train PF** (degradation cap).
- **DEFLATION** (cumulative trials): net **per-day Sharpe survives the haircut**
  (SR_hat > SR0 = E[max Sharpe | N trials], Bailey & López de Prado); DSR + Bonferroni reported.

### Method (Carver EWMAC, verbatim — *Systematic Trading* ch.7/App.B, to-verify)

- Signal on the **back-adjusted Panama series** (not the raw front): raw = EWMA(fast) −
  EWMA(slow); **price vol = EWMA stdev of daily price changes** (span 32, in Carver's 25–36d
  band — NOT a simple MA); forecast = scalar × raw/price_vol with the scalar fit on TRAIN so
  mean|forecast| ≈ 10, **capped ±20**.
- Three **well-separated** speeds only (adjacent are ~0.9 corr, not independent): **16/64,
  32/128, 64/256**.
- **Vol-targeted sizing**: position = forecast/10 × (vol_target / instrument $ vol) × bucket
  weight × IDM. **Risk-parity by bucket** — each of the 6 buckets carries one risk unit, so
  the **3 FX micros together carry ONE bucket** (1/18 each) and the 2 metals one (1/12 each):
  the operator's "treat FX as ~1 bucket" with **no contract dropped**. IDM = 1/√(w'Cw),
  capped 2.5, fit on train. **PF/Sharpe are invariant to vol_target & IDM** (gross and
  turnover-cost both scale linearly) — the verdict rests on the signal, weights, correlations
  and the cost/vol friction ratio, not account size.
- **10Y sign**: priced in YIELD, so the signal/vol/P&L are built on the **negated proxy
  `price = -yield`** — a falling-yield (rising-bond) trend → positive forecast → long bond.
  Proven both ways on the champion: most-long-bond date 2025-11-26 (pos +0.955, trailing
  yield −0.386%); most-short-bond date 2026-06-10 (pos −0.724, trailing yield +0.127%).
- **MCL vol** is close-to-close only (sidesteps the bad near-expiry intraday H/L).
- **Costs** (`to-verify` per market): conservative all-in $0.52/side micro commission
  ($2.50/side MBT) + 1-tick slippage on close-to-close turnover. 0/1/2-tick sensitivity run.

### Result

Common window **2024-05-01..2026-06-11 = 538 bars** (TRAIN 342 / HOLDOUT 196). All three
speeds are **net-negative on TRAIN** and fail the gate at pass 1:

| speed | train net | train PF | train Sharpe | each-year |
|-------|----------:|---------:|-------------:|-----------|
| 16/64  | −$1,601 | 0.891 | −0.67 | −/− |
| 32/128 | −$1,119 | 0.920 | −0.49 | +/− |
| **64/256** (champ) | **−$247** | **0.978** | **−0.12** | +/− |

Champion 64/256 holdout (touched once): PF **1.061**, Sharpe 0.36, net +$490, but **2026 is
negative** and PF is far under 1.30. **Deflation KILLS it**: train SR/day −0.0076 < SR0
**0.0360** (E[max] over **27 naive** cumulative trials; effective 25.25 at empirical EWMAC
ρ=0.70) → DSR 0.210, Bonferroni p 1.0.

**Per-market (champion):** only **MGC/gold clears** (PF 1.329, Sharpe 1.49, +$1,753). MCL is
the worst drag (PF 0.782, Sharpe −1.10 — choppy crude), the FX cluster whipsaws net-negative,
10Y −$234. Cost-**insensitive** (train PF 0.980→0.976 across 0→2 ticks) — costs are not what
sank it; the **trend edge simply isn't there** on this basket over this single ~25-month
regime. Mean |pairwise corr| on the traded window = **0.247** (basket IS genuinely
diversified — the thesis precondition held; the breadth just had nothing to diversify).

### Honest read

This is **DIRECTIONAL evidence over one ~25-month regime with a low trade count**, not a
decades-validated edge — and it points the same way as Phase-15/16: **NONE**. Only one of
nine markets (gold) trended cleanly enough for a slow EWMAC; diversification cannot
manufacture an edge that the constituents don't carry. **Not tuned to force a pass.** The
breadth thesis is **not refuted in general** (different/longer regime, more markets, or a
faster overlay could differ) — but on this basket and window it does not clear the bar.

---

## Campaign trial ledger (for cumulative deflation, continued from Phase-16)

| Phase | Stage | Trials this stage | Cumulative trials | Verdict |
|------:|-------|------------------:|------------------:|---------|
| 16 B1 | strategy | 22 | 22 | NONE |
| 17 S1 | data     | 0  | 22 | DATA PASS (no strategy run) |
| 17 S2 | strategy | 3 (naive; 1.43 eff @ρ0.55) | 27 naive / ~25.4 eff | NONE |

Stage-2 used `--prior-trials 24` (Phase-16 = 22 + Phase-15 = 2 dormant candidates), the
conservative base. Cumulative **naive 27** (deflated against this, the higher count);
**effective ~25.4** reported as a cross-check (the 3 EWMAC speeds are ~0.55–0.70 correlated,
so they are *fewer* than 3 independent trials — collapsing them would *understate* the
correction, hence we deflate against naive). A future batch should pass `--prior-trials 27`.
