# Phase-18 — TF Foundational Edge Audit — results

Research-only (CLAUDE.md §0.5.208). Drives the **real prod entry+exit code**, changes
**no** prod path, no deploy. `tools/eval/phase18/` only. REPORT autonomy.

**THE QUESTION:** does TF's *actual gated strategy* have a demonstrated edge over the full
~27-month tape, under the **same honest protocol** that returned NONE for every
Phase-15/16/17 candidate? Test the assumption — don't defend it.

## VERDICT: **DOES NOT CLEAR** the pre-registered bar (but it is *not* net-negative)

TF's gated strategy is **mildly profitable and positive every calendar year**, but its
profit factor (train **1.126**, full-tape **1.176**) sits **below the PF ≥ 1.30 bar** that
every other phase was held to. By its own honest protocol it has **no *demonstrated* edge**
at the standard we set — though, unlike the Phase-15/16/17 candidates (which were
net-*negative* after costs), TF's strategy at least makes money. The distinction matters:
**"does not clear the 1.30 bar" ≠ "no edge whatsoever."** It is a thin, real, ~1.18-PF edge
that fails a demanding-but-pre-committed standard.

---

## What ran (fidelity)

Drove the **live prod code** bar-by-bar over the saved tape — not a re-implementation:

- **Entry/exit:** `tools.eval.engine.FastGateEntry` → `src.strategy.evaluate_gates` /
  `_regime_ok` (SMA100-bounce long + 30m-EMA200 regime gate) and
  `src.execution.trail_manager.compute_ratcheted_stop` / `should_hard_exit`
  (base entry−75, +50 lock-in, 150 trail, 1000 hard-ceiling). `test_eval_engine.py`
  asserts `FastGateEntry` is byte-identical to the real `Sma100BounceStrategy`.
- **Live config, verified against the running container 2026-06-11:** `exit_mode=trailing`,
  `regime_gate_enabled=true`, stop 75 / lock-in 50 / trail 150 / hard-ceiling 1000 /
  cooldown 10.
- **Tape:** `research/data/nq_1min_raw.csv`, 786,236 1-min NQ bars, 2024-03-13 → 2026-06-02,
  Panama roll-adjusted (8 quarterly boundaries) — the same tape every other phase used.
- **Costs (identical honest layer to Phase-16/17):** `$0.62/side` commission + a
  **pessimistic 1-tick adverse slippage on every fill** (entry, stop, market-out), at
  **1 contract** so expectancy IS `$/contract`. Engine fills entry at the signal-bar close
  and the protective stop AT the stop price intrabar (§0.5.206).
- **Split-once, same boundary as Phase-16/17:** TRAIN `[2024-03 .. 2025-08]`, HOLDOUT
  `[2025-09 .. 2026-06]`, bucketed by **entry** time (one continuous tape — live never
  restarts at the split).
- **Pre-registered bar (committed in `phase16/gates.py` before results):** TRAIN PF ≥ 1.30,
  exp/ct ≥ $5, n ≥ 200, profitable each train year; HOLDOUT same + degradation cap
  (HO PF ≥ 0.75 × train); deflation over **cumulative 28 trials** (prior 27 + this 1).

**Anchor reconciliation (harness validity):** full-tape gated **n=1486 / PF 1.176** matches
the recorded Session-24 anchor (**n=1479 / PF 1.174**); the Phase-16 cost decomposition
(`$1.24 comm + 2×1-tick = $2.24/ct RT`) equals the anchor's blended `1.12 pt / 2-lot` friction
(`$2.24/ct`) — same magnitude, independently derived, so the two harnesses agree.

---

## Claim (a) — does the GATED strategy clear the bar?

| slice | n | PF | exp/ct | net (1ct) | win% | maxDD | per-year (net / n) |
|---|--:|--:|--:|--:|--:|--:|---|
| **GATED full** | 1486 | **1.176** | $9.79 | +$14,549 | 62.8% | −$2,175 | 24:+2,202/445 · 25:+7,623/650 · 26:+4,724/391 |
| **GATED train** | 894 | **1.126** | $7.33 | +$6,549 | 61.2% | −$2,175 | 24:+2,202/445 · 25:+4,348/449 |
| **GATED holdout** | 592 | **1.258** | $13.51 | +$7,999 | 65.2% | −$1,623 | 25:+3,275/201 · 26:+4,724/391 |

**TRAIN GATE: FAIL** — fails on **PF 1.126 < 1.30** alone (exp/ct $7.33 ≥ $5 ✓, n 894 ≥ 200 ✓,
profitable each train year ✓). Per protocol the **holdout was not touched** by the gate
decision (preserved). Bonferroni-haircut cross-check `p = 1.0` over 28 cumulative trials —
the per-trade Sharpe is far too small to survive any multiple-testing correction.

**Two honest nuances (neither rescues the verdict, both worth stating):**

1. **No overfit collapse.** The holdout PF (**1.258**) is *higher* than train (1.126), and
   every year is positive. The strategy does not fall apart out-of-sample — it is simply
   *uniformly thin*, train and holdout alike. So "fails the bar" here means "genuinely
   ~1.2-PF everywhere," not "looked good in-sample then died."
2. **Deflated-Sharpe footnote.** The full DSR needs a within-batch Sharpe-variance pool;
   TF's live config is a *single pre-registered* strategy (not a grid winner), so the
   Bonferroni haircut is the right standalone deflator. It says the same thing: not significant.

> **==> CLAIM (a): the gated strategy DOES NOT CLEAR the pre-registered bar.**

---

## Claim (b) — what does the GATE add? (entry vs gate vs neither)

### b1) Full-system P&L, gate ON vs OFF

| config | n | PF | exp/ct | net | win% | maxDD |
|---|--:|--:|--:|--:|--:|--:|
| **GATED (gate ON)** | 1486 | **1.176** | $9.79 | +$14,549 | 62.8% | **−$2,175** |
| **UNGATED (gate OFF)** | 2723 | 1.121 | $7.08 | **+$19,289** | 61.0% | −$3,847 |

The gate **improves quality** (PF 1.121 → 1.176, exp/ct $7.08 → $9.79) and **halves the
drawdown** (−$3,847 → −$2,175), at the cost of **total dollars** (it takes ~half as many
trades, so +$14.5k vs +$19.3k). The gate is *risk-adjusted accretive, dollar-dilutive* —
exactly what a regime filter should do. It is **not** the bottleneck.

### b2) UNGATED trades partitioned by the REAL regime-at-entry (live `_regime_ok` on the same 7000-bar tail)

| cohort | n | PF | exp/ct | net | win% | maxDD | per-year (net / n) |
|---|--:|--:|--:|--:|--:|--:|---|
| **above-trend** | 1399 | **1.204** | $11.22 | +$15,703 | 63.3% | −$2,012 | 24:+3,366 · 25:+6,622 · **26:+5,714** |
| **below-trend** | 1324 | 1.044 | $2.71 | +$3,586 | 58.6% | −$4,712 | 24:+3,286 · 25:+1,403 · **26:−1,102** |

The decomposition is clean:

- **Above-trend (what the gate keeps)** is the best cohort — PF 1.204, $11.22/ct, positive
  every year *including 2026*, smallest drawdown.
- **Below-trend (what the gate removes)** is marginal and **decaying** — PF 1.044, $2.71/ct,
  and it goes **net-negative in 2026** (+$3,286 → +$1,403 → −$1,102) while carrying the
  **largest drawdown of any cohort** (−$4,712). The gate is correctly trimming the weak,
  high-variance, decaying tail.

> **==> CLAIM (b): the limiting factor is the ENTRY, not the gate.** The gate is doing its
> job (concentrates on the better regime, +0.16 PF and half the DD). But **even the gate's
> favoured cohort tops out at PF 1.204 — still below 1.30.** The SMA100-bounce *entry itself*
> only generates a ~1.2-PF edge even in its best regime; no gate can manufacture an edge the
> entry does not contain. The edge is real and positive, but thin — *the entry, sharpened by
> the gate, is not strong enough to clear the bar after real costs.*

---

## What this means

- **Held to its own standard, TF's foundational strategy has no demonstrated edge.** It
  clears none of PF≥1.30 / deflation; it clears exp≥$5, n≥200, and each-year-positive.
- **But it is genuinely profitable** (+$9.79/ct full tape, every year green, no OOS
  collapse) — materially better than every Phase-15/16/17 candidate, which were all
  net-negative. TF is a *thin-but-real ~1.18-PF* strategy that fails a *demanding*
  pre-committed bar, not a broken one.
- **The gate is exonerated; the entry is the ceiling.** If we ever want to clear 1.30, the
  work is on the **entry** (signal selectivity / a better-than-SMA100-bounce trigger), not
  on the regime filter — which is already adding what it can.
- **The one thing no backtest settles** is the forward edge in the live regime (§0.5.220).
  But nothing here earns TF the benefit of the doubt that every other candidate was denied:
  by the protocol, the answer is *does not clear*.

---

## Caveats (not hidden)

- 1-min NQ bars (point-identical to MNQ; $2 multiplier for $). TF-own-entry path only — the
  SeanBot-trigger replicate path (now OFF) is not modeled.
- Modeled fills: entry at signal-bar close, stop AT stop intrabar — assumes the exit
  mechanically works (Phase-3 proved the live plumbing separately). The +1-tick-per-fill
  slippage on top of that is the pessimistic honesty layer.
- The regime EMA200 is the live lightly-warmed ~234-bucket EMA (recomputed over the trailing
  ≤7000-bar window each bar), exactly as live — not a fully-converged one.
- Single position (live MAX_CONCURRENT=1 for the gated path); gated/ungated trade *sets*
  differ via concurrency, so b1 is a system comparison and b2 (regime-at-entry partition of
  the ungated run) is the cleaner entry-edge isolation.

---

### Reproduce

```
/home/tradeflow/tradeflow/.venv/bin/python -m tools.eval.phase18.run --prior-trials 27 \
  --out tools/eval/phase18/RESULTS.txt
```

Verbatim console report: `tools/eval/phase18/RESULTS.txt`.
