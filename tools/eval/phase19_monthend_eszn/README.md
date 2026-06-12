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

---

# Phase-19 RUN — verdict (PR #154, supersedes the data-gate STOP above)

**Verdict: NONE.** The pre-registered month-end ES/ZN rebalancing model, run on the decades
of daily history landed in PR #153 and scored through the SAME honest harness
(`tools/eval/phase16/{metrics,gates,costs}.py`) that returned NONE for every Phase-15..18
candidate, **does not clear the bar.** No variant clears the TRAIN gate, so the PRIMARY
holdout was sealed for the gate and the candidate is rejected on multiple independent grounds.
`run.py` is the eval; `results.json` is the machine-readable detail. Research-only — nothing
was wired live; the bot stayed FLAT + HALTED.

### Why NONE (the binding facts)
1. **No variant clears TRAIN PF ≥ 1.30.** Over the 1970–2007 train era (455 mo) the best of
   the 12 variants is **ZN:reversal at PF 1.236 < 1.30** (the pre-registered champion = max
   train PF). All 12 fail; 7 are net-negative on train.
2. **The apparent edge is a holdout-era REGIME ARTIFACT, not a tradable signal.** Holdout
   (2008–2026) PFs (1.24–1.46) are *uniformly higher* than train PFs (0.81–1.24). The signal
   shows nothing in 1970–2007 and only "works" post-2008 — exactly the pattern split-once is
   designed to catch. Selecting on the strong holdout would be textbook OOS overfitting.
3. **Each-year gate fails badly** — the champion loses money in 14 of 38 train years and 7 of
   19 holdout years. Not a persistent calendar effect.
4. **Kill tests fail** (see below): placebo not beaten, breadth collapses, recency < 1.30,
   and the holdout "edge" sits exactly on the cost knife-edge.

### Per-variant table — PRIMARY (SPX + UST10Y, 1970–2026) — verdict series
TRAIN gate = PF≥1.30 ∧ exp/ct≥$5 ∧ n≥200 ∧ each-year+. **0 / 12 pass.**

| Variant | Train PF | Train n | OOS PF | OOS n | Train gate |
|---|---|---|---|---|---|
| ES:pressure | 1.149 | 453 | 1.382 | 222 | FAIL (PF, each-yr) |
| ES:reversal | 0.884 | 453 | 1.417 | 221 | FAIL |
| ES:combined | 1.009 | 906 | 1.399 | 443 | FAIL |
| ZN:pressure | 0.807 | 453 | 1.242 | 222 | FAIL |
| **ZN:reversal (champion)** | **1.236** | 453 | **1.245** | 221 | **FAIL (PF 1.236<1.30, each-yr)** |
| ZN:combined | 0.983 | 906 | 1.243 | 443 | FAIL |
| PAIR_VW:pressure (primary pair) | 1.033 | 450 | 1.457 | 222 | FAIL |
| PAIR_VW:reversal | 0.884 | 450 | 1.434 | 221 | FAIL |
| PAIR_VW:combined | 0.959 | 900 | 1.446 | 443 | FAIL |
| PAIR_DN:pressure | 1.045 | 453 | 1.435 | 222 | FAIL |
| PAIR_DN:reversal | 0.884 | 453 | 1.420 | 221 | FAIL |
| PAIR_DN:combined | 0.964 | 906 | 1.427 | 443 | FAIL |

Champion deflated Sharpe = **0.420** (« DSR_MIN 0.95), SR0 (E[max] over 40 trials) = 0.088,
Bonferroni p = 1.0. *(OOS PFs are reported as a diagnostic; the verdict rests on the
pre-registered champion, and the deflator's `n_trials = 28 prior + 12 in-eval = 40` already
penalises for the whole 12-variant search.)*

### Per-variant table — IEF cross-check (SPX + IEF, 2002–2026; robustness only, <200 mo OOS)
Confirms the primary read: the apparent post-2008 strength concentrates in the **equity leg**
(ES/PAIR PF ~1.3–1.45), while the **bond leg is flat** (ZN PF ~1.0–1.1). It is NOT a clean
month-end ES/ZN rebalancing mechanism. Does NOT carry the verdict (Constraint 3).

| Variant | Full PF (n) | 2008+ PF (n) |
|---|---|---|
| ES:pressure | 1.457 (285) | 1.421 (222) |
| ES:reversal | 1.378 (284) | 1.484 (221) |
| ZN:pressure | 1.021 (285) | 1.157 (222) |
| ZN:reversal | 1.027 (284) | 1.083 (221) |
| PAIR_VW:pressure | 1.424 (282) | 1.471 (222) |
| PAIR_DN:reversal | 1.304 (284) | 1.407 (221) |

### Five kill tests (on the champion, ZN:reversal)
1. **Concentration / placebo — FAIL.** Real month-end holdout PF **1.245** vs the identical
   rule on mid-month pseudo-events **1.107**: the month-end anchor barely separates from a
   random mid-month date, and the real PF doesn't even clear 1.30. The mechanism is not
   month-end-specific.
2. **Monotonicity — PASS.** Mean P&L scales with |S| quantile (bottom −236 → top +367,
   rank-ρ 0.70). The one test it passes: bigger overweight ⇒ bigger move — but that alone
   isn't enough when (1)/(3)/(4) fail.
3. **Breadth — FAIL.** Drop the best 5% of months and holdout PF collapses to **0.933 < 1.0**.
   The "edge" is a handful of outliers (GFC/COVID/2022 stress months), not breadth.
4. **Recency — FAIL.** 2015→present PF **1.189 < 1.30** (n=137). The effect has decayed in the
   most recent decade (arbitraged away, as month-end-anomaly literature predicts).
5. **Cost sensitivity — cost-cliff = 1.0×.** Champion holdout PF crosses below 1.30 at the
   FULL honest ES/ZN cost (PF 1.245 at 1.0×; 1.303 at 0.5×). Even the regime-artifact "edge"
   is knife-edge to costs.

### Cost bridge (verified, §0.5.97 — NOT the MNQ numbers)
Specs from CME (PR #152): **ES** 0.25 tick / $50 pt ($12.50/tick); **ZN** 0.015625 tick /
$1,000 pt ($15.625/tick). Commission = IBKR published US-futures FIXED tier (same vendor as
the MNQ $0.62/side tier in `config/instruments.py`): **ES ≈ $2.05/side** (IBKR ~$0.85 + CME
e-mini ~$1.18 + NFA ~$0.02 → RT $4.10), **ZN ≈ $1.45/side** (IBKR ~$0.85 + CBOT ~$0.58 + NFA
→ RT $2.90). RT-with-1-tick-each-way: ES $29.10, ZN $34.15. Applied per the Phase-16
`CostModel`+`Slippage` (1-tick aggressive on entry + market exit), which on proxy close levels
equals Constraint-4's return-drag/notional. The account's exact ES/ZN tier was not
broker-probed (no-broker PR) — the cost-cliff bounds the sensitivity. Sizing: ES 1 contract
(notional = SPX×$50); ZN priced per contract (proxy is a base-100 price-return index → $/pt =
$1,000 gives a ~$100k notional, ~13% below a real ~$115k ZN, i.e. slightly conservative on the
bond-leg return-drag). Pairs: VW = inverse-expanding-$-vol weight (point-in-time); DN = equal
$ notional.

### Integrity assertions (Task C — all hold)
- **No look-ahead:** `S(M)` is an expanding z-score over months strictly < M; `s_raw(M)` reads
  only closes ≤ T−5; entry is at the T−5 close on which the signal is observed
  (decision-at-close). Asserted programmatically (`_assert_no_lookahead`).
- **Holdout sealed:** champion pre-registered as max-train-PF before any holdout judgement;
  no parameter selected on holdout.
- **Costs applied:** gross ES-pressure Σ $107,200 ≠ net $87,557 (costs bite).
- **Placebo wired:** mid-month pseudo-event runs the identical machinery (kill test 1).

### Trial-budget note (Task E — recommend, do NOT change here)
The deflator was fed `n_trials = 28 (cumulative prior) + 12 (this eval) = 40`. As the reopened
edge search runs more *hypotheses*, the multiple-testing burden is cross-hypothesis, not just
in-eval. The campaign should track a single cumulative trial counter spanning ALL reopened
hypotheses (Phase-19 = 12, the next phase += its variant count) and feed THAT to every
deflator — otherwise each phase under-penalises by ignoring the others. Cumulative count after
Phase-19 = **40**; the next phase should pass `--prior-trials 40`. (Recommendation only.)

### Scope guarantee
This was an offline eval. It did NOT connect to the broker, modify `src/**`/`config/**`, touch
the halt, or wire anything live — even though it returned NONE. The bot was FLAT + HALTED
throughout (halt raised 2026-06-11 22:08:50Z, never cleared) and was not touched. A PASS would
not have auto-advanced to live either; forward paper-trading is a separate operator decision.

### What I got wrong (run phase)
- The DATA.md table labelled IEF the "clean primary" bond proxy, but the brief's Constraint 3
  is authoritative: the verdict rides on the long SPX+UST10Y series (n≥200) and IEF is the
  cross-check. I followed the brief.
- Nothing else material. The model ran as pre-registered; the NONE is robust across all 12
  variants, both bond proxies, and four of five kill tests.
