# S03 — VIX short-vol roll-yield (ETP proxy) — VERDICT: NONE

## Verdict block

```
VERDICT:   NONE (structural — NOT a dead mechanism; see honest reading)
CHAMPION:  contango (long SVXY while VIX < VIX3M; risk-off when curve inverts)
TRAIN:     PF 2.895 | exp $7,518/episode | n 58  | 2011-10->2020
HOLDOUT:   PF 2.937 | exp $4,464/episode | n 34  | 2020->2026-06
DSR:       0.980 (>= 0.95, n_trials = 84 prior + 4 = 88)   <- CLEARS deflation
BINDING STRUCTURAL FAILS:
  1. n: 58 train / 34 holdout episodes << 200. Structural, flagged upfront: the
     filter stays on for months at a time -> ~7 episodes/yr. Cannot be fixed
     without changing the trade unit (per-day rows = the n-inflation trap; refused).
  2. each-year-positive: 2011(-), 2019(-) in train; 2020(-), 2022(-) in holdout.
ALL FIVE KILL TESTS PASS:
  K1 tail-shape: worst 90d window = 6.2% of lifetime PnL. Crisis PnL: Volmageddon
     +$62.7k, COVID -$7.9k, Aug-2024 +$107.8k — the filter EXITED before each crash
     (curve inversion precedes the blow-up) and re-entered after.
  K2 recency: PF 2.844 (2015+), 2.937 (2020+).
  K3 monotonicity: PnL scales with entry contango depth (rho 0.3, top>bottom).
  K4 cost-cliff: NONE — holdout PF never crosses 1.30 even at 8x costs.
  K5 VXX cross-check: short-VXX same filter PF 2.008, +$4,025/episode (consistent).
```

## What was tested (pre-registered in run.py before results)

TESTABLE-PROXY, labeled: SVXY (the ETP does the futures rolling; -1x until
2018-02-27, -0.5x after — the series is the product as it traded). 4 filter
variants (contango / VIX<20 / both / deep-contango), $100k notional/episode,
5bp/side, episode = one filter-on run. Split 2020-01-01; $10/episode expectancy
bar (stricter than the $5 phase16 bar, scaled for notional).

## Honest reading

This is the strongest raw result of Phases 15-21: PF ~2.9 on BOTH sides of the
split with all three vol catastrophes inside the sample, deflation-clearing, and
cost-insensitive. The mechanism is coherent and documented (VIX term-structure
inversion is a leading risk-off signal; the roll yield pays only in contango).

It still cannot be certified at this bar, and the reasons are structural, not
cosmetic: ~92 episodes in 15 years is a sample on which PF 2.9 can be luck shaped
by a handful of +$100k runs (win rate is only ~40% — the PnL is concentrated in
long winners), and 4 negative calendar years mean the equity curve regularly
spends a year underwater — which the each-year gate exists to catch. The n<200
STOP rule from Phase-20c applies verbatim: don't force a pass; report the ceiling.

Manifest note for the final synthesis: of everything tested, this is the one
where MORE data genuinely exists as an unlock — VIX futures curve history
(2004->) would triple the episode era and allow a constant -1x synthetic roll
(vs the ETP's leverage drift). That is a different, heavier study; noted, not run.

## What I got wrong

- I expected the each-year gate to fail on 2018/2020 (crash years). It failed on
  2019/2022 instead — whipsaw years, not crash years: the filter's cost is chop
  re-entries, not blow-ups. The K1 result (filter dodged both crashes) was the
  genuine surprise of this eval and cuts AGAINST my prior that short-vol = picking
  up pennies in front of the steamroller; the curve-inversion gate changes the
  shape. The n and yearly-consistency fails stand regardless.
