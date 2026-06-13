# Phase-20c — BTC + ETH dedicated funding-carry validation (research-only)

**Verdict: `NONE-confirmed`.** BTC and ETH were given their fairest, most interpretable
funding-carry test — per-coin, two spike definitions, a pre-registered patient min-hold
variant — under the unchanged honest bar. **Zero** variants clear the spike-stripped
holdout (PF ≥ 1.30 **AND** n ≥ 200 **AND** each-year-positive **AND** DSR > 0). Funding
carry as a category is killed with eyes-open evidence: there **is** real normal-regime
majors carry, but it is too low-frequency to certify (n ≪ 200) **and not durable**
(2026 turns negative as post-ETF basis compresses).

Research-only / offline. Reads `research/data/phase20/{BTC,ETH}_{spot,perp,funding}.csv`
only. Does **not** connect to IB/broker, touch `src/**`/`config/**`, the halt, or wire
anything live — even on a PASS-for-majors.

```
python -m tools.eval.phase20c_majors_validation.run --prior-trials 70
```

---

## The 20b baseline this re-examines (PR #156)

| variant | train PF | stripped (train) | holdout PF | holdout-strip PF |
|---|---|---|---|---|
| C:BTC | 1.235 (n=125) | 0.006 | 0.397 | 0.000 |
| C:ETH | 1.580 (n=140) | 0.000 | 0.506 | 0.000 |

Both strip to ~0.00 pooled; both holdouts < 0.51. 20b's pooled read: "edge all
squeeze/delever tail comp." Phase-20c gives majors the per-coin + patient-hold fair shot
20b didn't, and produces the legible net-carry curve.

---

## Per-coin matrix — variant × spike-treatment × {train, holdout} (PF, n)

Holdout strip columns are the two pre-registered spike definitions; the **verdict rides on
these**. `incl` is diagnostic context.

### BTC
| rule | train incl PF (n) | HO incl PF (n) | HO delever PF (n) | HO topdecile PF (n) |
|---|---|---|---|---|
| A always-on | ∞ (1) | ∞ (1) | ∞ (1) | ∞ (1) |
| B positive-funding | 0.769 (281) | 0.240 (190) | 0.243 (190) | 0.089 (190) |
| C cost-threshold | 1.235 (125) | 0.397 (35) | 0.397 (35) | 0.000 (35) |
| **D min-hold-21** | **7.753 (54)** | 2.575 (32) | **2.688 (31)** | 1.499 (38) |

### ETH
| rule | train incl PF (n) | HO incl PF (n) | HO delever PF (n) | HO topdecile PF (n) |
|---|---|---|---|---|
| A always-on | ∞ (1) | ∞ (1) | ∞ (1) | ∞ (1) |
| B positive-funding | 0.995 (269) | 0.255 (204) | 0.258 (204) | 0.126 (204) |
| C cost-threshold | 1.580 (140) | 0.506 (35) | 0.506 (35) | 0.000 (35) |
| **D min-hold-21** | **7.609 (49)** | 2.546 (34) | **2.671 (33)** | 1.769 (37) |

**Why nothing clears** (every strip-holdout cell fails ≥1 gate):
- **A** — always-on is 1 episode/coin; PF is degenerate (no losers). Diagnostic only → see curve.
- **B / C** — stripped-holdout PF < 1.30 (carry churned away by costs / collapses on strip).
- **D** — the standout: stripped-holdout PF **2.6–2.7** (delever), DSR > 0, exp/ct > $5 — but
  fails on **two independent counts**: (1) **n = 31–34 ≪ 200** (majors funding carry cannot
  generate 200 independent episodes in 6 years — structural, not a data error); (2) **not
  each-year-positive** — 2026 is negative (BTC −$250, ETH −$276) as post-ETF basis compresses.

---

## Net-carry curves — the legible centerpiece

Per-year **signed funding accrual** for the always-on hold ($ on $10k notional/leg), for all
three treatments. `incl − strip` per year = exactly how much of that year's carry was spikes.
One round-trip cost for an always-on hold = **$28** total (negligible vs. annual carry).

### BTC (basis/price drift over the full hold: −$17)
| year | incl | delever-strip | topdecile-strip |
|---|---|---|---|
| 2020 | 1,724 | 1,826 | 652 |
| 2021 | 3,061 | 3,062 | 657 |
| 2022 | 416 | 452 | 456 |
| 2023 | 787 | 787 | 690 |
| **2024** | 1,196 | 1,192 | 801 |
| **2025** | 513 | 513 | 513 |
| **2026** | 36 | 36 | 36 |

### ETH (basis/price drift over the full hold: −$123)
| year | incl | delever-strip | topdecile-strip |
|---|---|---|---|
| 2020 | 2,749 | 2,753 | 939 |
| 2021 | 3,754 | 3,767 | 815 |
| 2022 | 79 | 119 | 246 |
| 2023 | 826 | 826 | 739 |
| **2024** | 1,300 | 1,303 | 964 |
| **2025** | 493 | 493 | 493 |
| **2026** | 16 | 16 | 20 |

(Holdout = 2024–2026, bold.)

### The two-strip comparison (Constraint 3 — the point)
- **delever-strip ≈ incl** every year → the named-crisis windows contribute almost nothing to
  the carry. Majors carry is **not** crash insurance.
- **topdecile-strip ≪ incl** in the high-carry years (2020/2021) but **≈ incl** in the
  low-carry holdout years → the carry lives in the **elevated-but-non-crisis** top decile of
  funding magnitude. This is **elevated-funding carry**, a real economic phenomenon — exactly
  the distinction 20b's pooled "all tail" scalar buried.
- Rule D echoes this: **delever-strip HO PF (2.688) ≫ topdecile-strip HO PF (1.499)** — the
  patient hold's edge survives surgical strip but is mostly the elevated-funding decile.

### One-line read per coin
- **BTC** — holdout normal-regime carry after cost: delever **$1,740** / topdecile **$1,349**
  (cost $28). **There IS positive net normal-regime carry** — but it is collapsing year over
  year (2024 $1.2k → 2025 $0.5k → 2026 ~$36) and is too thin/non-durable to certify.
- **ETH** — holdout normal-regime carry after cost: delever **$1,812** / topdecile **$1,476**.
  Same shape: real positive carry, same 2024→2026 collapse, same fail-to-certify.

**Synthesis:** Not "all tail" (20b's pooled read was too harsh) — majors carry is genuine,
normal-regime, and elevated-funding-driven. But it is (a) **too low-frequency** to clear
n ≥ 200, and (b) **decaying into 2026** so it fails each-year-positive. Both are
disqualifying under the unchanged honest bar. → **NONE-confirmed**, eyes-open.

---

## Honest bar (unchanged) + deflation

- Gates (imported from `phase16`): PF ≥ 1.30 · exp/ct ≥ $5 · n ≥ 200 · each-year-positive ·
  holdout degradation cap (HO PF ≥ 0.75·train) · DSR ≥ 0.95.
- **Verdict rule**: PASS-for-majors iff some (coin, rule) on a **spike-stripped holdout** has
  PF ≥ 1.30 **AND** n ≥ 200 **AND** each-year+ **AND** DSR > 0 (post deflation). Else NONE.
- **Deflation**: `--prior-trials 70` (cumulative after 20b) **+ 8 in-eval variants**
  (2 coins × 4 rules; treatments are diagnostic lenses, periods are eval not search — the
  same not-multiply-by-strip convention 20b used) = **n_trials 78**. sr_var 0.01233,
  sr0 (E[max Sharpe]) 0.271. Rule-D DSR ≈ 0.21 (> 0 but ≪ 0.95) — high-PF/low-n is precisely
  the regime the deflated Sharpe is built to distrust.

## Costs (imported, §0.5.97 — not re-derived)
Taker 0.05%/fill · 4 fills/round-trip · slip 2 bp/fill (majors) · $10k/leg — imported from
`tools.eval.phase20_funding_carry.run`. Per-coin rule-C threshold = amortized round-trip
cost over the 21-interval break-even horizon (also imported).

## Integrity assertions (all hold)
- **Trade unit = episode, not interval**: timing-rule (B/C/D) episodes 1,448 vs 14,058
  funding intervals (ratio 0.103) — n ≪ intervals.
- **Signed funding included**: the always-on hold accrues **1,968** negative-funding intervals
  (paid funding is in the ledger; no positive-only cherry-pick).
- **Costs applied**: gross ≠ net on every ledger (probe BTC:C train gross ≠ net).
- **Holdout sealed**: verdict rides on the stripped holdout; both strips run the same masking
  path; the decile threshold matches 20b for comparability (noted in-sample quantile; the
  delever strip has no such dependency).

## Cumulative trials after this eval: **78** (next phase: `--prior-trials 78`).

---

## What I got wrong
Nothing material. One honesty note: rule D's stripped-holdout PF (2.6–2.7) is eye-catching
but rests on n = 31–34 and fails each-year-positive (2026 negative) — I report it as a
**near-miss that the n and durability gates correctly reject**, not as a survivor. The
`topdecile` decile threshold is computed over the full coin series (matching 20b) — a mild
in-sample quantile; the surgical `delever` strip carries the conservative read and has no
such dependency.
