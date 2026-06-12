# Phase-19 — Month-end ES/ZN rebalance edge test — **STOPPED AT THE DATA GATE (no run)**

**Verdict: NOT RUN — data-gate failure.** This phase was the first hypothesis of the
*reopened* edge search (after Phase-15→18 closed NONE). It did **not** produce a
PASS/NONE verdict because the honest harness's data prerequisite is not met. Per the
pre-registered STOP rule (Constraint 3 / Task A.2: *"if data is NOT available or fails
the gate, STOP and report — do not fabricate or substitute a proxy silently"*), no model
was written. There is intentionally **no `run.py` and no `results.json`** in this
directory — nothing ran, and emitting them would misrepresent a run that did not happen.

Operator decision (2026-06-12): **shelve and document the STOP.** The Phase-15→18
campaign verdict (NONE / founding edge retired) is unchanged.

---

## Hypothesis (recorded for a future phase, if data is ever sourced)

Month-end equity/bond **rebalancing pressure**: benchmarked institutions (60/40,
target-date funds) rebalance to fixed weights near month-end. When equities outperform
bonds intra-month they become overweight, forcing month-end equity *selling* + bond
*buying* concentrated in the final business days, with a reversal early the next month.
The losing side is the mandate-constrained allocator rebalancing on the calendar
regardless of price. Independent model pass rated it Stack 5 / Ease 5 / Durability 4.

### Pre-registered variant set (Constraint-5 — frozen before any look, **unrun**)
- **Leg attribution:** ES-leg alone, ZN-leg alone, ES/ZN pair (vol-weighted primary;
  dollar-neutral robustness).
- **Windows:** pressure leg (enter T−5 close → exit T0 close, T0 = last business day of
  month); reversal leg (enter T0 close → exit T+3 close).
- **Signal:** `S(month)` = standardized (point-in-time, expanding-window z-score, NO
  look-ahead) intra-month cumulative ES-return minus ZN-return through T−5. Leg direction
  = `sign(S)` (S>0 → overweight equity → rebalance sells equity / buys bond); reversal
  leg flips it.
- **Honest bar (reused, unmodified):** `tools/eval/phase16/{costs,metrics,gates}.py` —
  TRAIN+HOLDOUT split at 2025-09-01, real costs, deflated Sharpe + Bonferroni over the
  cumulative trial count, **PF ≥ 1.30 OOS, exp/ct ≥ $5, n ≥ 200, each-year+, DSR ≥ 0.95**.

---

## Task-A audit (the part that actually ran — read-only)

### A.1 — Harness reuse points (confirmed present, ready to reuse if data ever lands)
- Split: `tools/eval/phase16/dataset.py` — fixed `TRAIN … 2025-09-01 / HOLDOUT
  2025-09-01 … 2026-07-01` (split-once, never tuned).
- Costs: `tools/eval/phase16/costs.py` — `CostModel` ($0.62/side commission) + `Slippage`
  (1-tick conditional adverse). **Instantiated with MNQ economics by default** — a
  Phase-19 run would have to pass verified ES/ZN tick/multiplier (below), not the MNQ
  numbers (§0.5.97).
- Deflators: `tools/eval/phase16/metrics.py` — `deflated_sharpe` + `bonferroni_haircut`,
  multiple-testing parameter = cumulative `n_trials` (28 after Phase-18; a Phase-19 run
  would pass `--prior-trials 28`).
- Gates: `tools/eval/phase16/gates.py` — the pre-registered bar above, committed before
  results. **Phase-18 is the apples-to-apples precedent** (drives real prod code, scores
  with this same layer).

### A.2 — Data availability: **NOT AVAILABLE → STOP** (two independent, decisive grounds)

**(a) Wrong instruments — ES and ZN do not exist in the pipeline.**
A search of `research/data/` for `es_*` / `zn_*` series returns nothing. The only daily
instruments present are the Phase-17 **micro** basket
(`research/data/phase17/<sym>_daily.csv`: MES, MGC, MHG, MCL, M6E, M6A, M6B, MBT, 10Y).
The two nearest are **not** the requested contracts:

| Requested | On disk | Why it is not a substitute |
|---|---|---|
| **ES** (E-mini S&P 500, $50/pt) | **MES** (Micro E-mini, $5/pt) | Different multiplier; a 1/10-size micro, not ES. |
| **ZN** (10Y T-Note, bond **price**, 32nds, $1,000/pt) | **"10Y"** = Micro 10-Year **Yield** future (priced ~4.46 *yield*, $1,000/yield-pt) | A *yield* instrument, economically inverted vs a bond-price future. Fundamentally not ZN. |

Constraint 3 forbids silently substituting these proxies; §0.5.97 forbids deriving
ES/ZN specs from the micro/MNQ numbers. So the micros cannot stand in for ES/ZN.

**(b) Span is fatal for a monthly strategy — independent of (a).**
The basket's common window is rates-bound to **~25 months** (`10Y_daily.csv`:
2024-05-01 → 2026-06-11, 540 daily rows; MES reaches 2023-06 ≈ 36mo but the panel
intersects to the shortest member). A month-end calendar strategy fires **~1 event /
month ⇒ ~25–36 trades per leg total.** The honest bar requires **n ≥ 200**. That is a
~6–8× shortfall: the train/holdout split (~16 train / ~9 holdout events) is statistically
meaningless and **auto-fails on trade count before any edge question is even asked.**

**Re-fetch does not rescue it.** Per the Phase-17 probe note (`tools/eval/phase17/markets.py`),
IBKR `includeExpired=True` only resolves contract definitions back to ~2024-06, so an
IB-sourced ES/ZN Panama stitch is also span-bound to ~24 months. And this PR is forbidden
from connecting to the broker at all (Constraint 4).

**Structural meta-finding for the operator:** a **monthly-frequency** calendar strategy
cannot produce 200 trades without ~17 years (200 months) of history. The IB-obtainable
futures span tops out near ~24 months. So even with full-size ES/ZN fetched from IB, this
candidate **cannot clear the n ≥ 200 honest bar as written** — apples-to-apples against
the Phase-15→18 NONEs is not achievable on IB data alone. Clearing it would require a
*different data source* (decades of cash S&P + Treasury-index / bond-index daily history,
freely available outside IB) — which is a separate, operator-authorized sourcing task,
not something this PR may improvise.

### A.3 — Verified ES / ZN specs (CME — recorded for §0.5.97 compliance if data is sourced)

| Contract | Tick size | Point value | Tick value | Notes |
|---|---|---|---|---|
| **ES** (E-mini S&P 500) | 0.25 index pt | **$50 / pt** | $12.50 / tick | cash-settled, quarterly |
| **ZN** (10Y T-Note) | ½ of 1/32 = 0.015625 | **$1,000 / pt** | $15.625 / tick | $100k face, 32nds quote |

Sources: CME [E-mini S&P 500 contract specs](https://www.cmegroup.com/markets/equities/sp/e-mini-sandp500.contractSpecs.html),
CME [10-Year T-Note contract specs](https://www.cmegroup.com/markets/interest-rates/us-treasury/10-year-us-treasury-note.contractSpecs.html).
Round-trip commission would need the account's real ES/ZN fee schedule (the micro
schedule does not apply) before any honest-cost run.

---

## Offline / scope guarantee

Nothing in this directory connects to the broker, reads secrets, touches a DB, or
modifies any `src/**` / `config/**` / compose / image / halt. This is a documentation-only
STOP note. The bot was FLAT + HALTED throughout (halt raised 2026-06-11 22:08:50Z, never
cleared) and was not touched.

## What I got wrong

- The brief named the directory `phase19_monthend_esz n` (with a stray space); a space in
  a path is clearly a typo, so this dir is `phase19_monthend_eszn`.
- I expected the Phase-17 daily basket might cover ES/ZN; it does not — it is micros
  (MES) plus a *yield*-priced 10Y micro, and the span is ~25mo. The data-gate STOP is the
  honest outcome, not a workaround.
