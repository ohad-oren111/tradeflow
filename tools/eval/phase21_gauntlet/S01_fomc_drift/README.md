# S01 — Pre-FOMC announcement drift — VERDICT: NONE

## Verdict block

```
VERDICT:   NONE
CHAMPION:  T2_sched (long SPX close T-2 -> close T, scheduled meetings only)
TRAIN:     PF 1.905 | exp $216.72/ct | n 209 | 1994->2020   <- the documented effect IS here
HOLDOUT:   PF 1.305 | n 50 | 2020->2026-06
DSR:       0.836 (< 0.95, n_trials = 78 prior + 3 = 81)     <- FAIL
BINDING FAILS (4 independent):
  1. train each-year-positive: 7 of 26 years negative (1994, 2002, 2005, 2010, 2011, 2016, 2018)
  2. deflation: DSR 0.836 < 0.95 (Bonferroni p 0.062)
  3. K1 placebo: holdout PF 1.305 vs weekday-matched non-FOMC placebo 1.22 — margin
     0.085 < 0.10. OOS, an FOMC-day long barely beats ANY-day long in the 2020-26 tape.
  4. K2 crisis-strip: stripping GFC+COVID episodes (8) drops holdout PF to 1.257 < 1.30.
ALSO: cost-cliff at 1.5x (thin per-event edge on a 2-day ES round trip)
PASSED: K3 recency (PF 1.32 on 2015+, 1.305 on 2020+) — decayed but not dead.
```

## What was tested (pre-registered in run.py before results)

Daily close-to-close proxy of the Lucca-Moench pre-FOMC drift: long 1 ES (SPX x $50)
into the scheduled announcement-day close. 3 variants (T-1->T, T-2->T, T-1->T incl.
unscheduled as lookahead-tainted robustness); champion = max train PF among the two
scheduled variants. Split TRAIN 1994->2020 (n 209 >= 200) / HOLDOUT 2020->2026.
Phase-16 gates verbatim; ES costs = PR#154-verified specs ($2.05/side + 1-tick slip).

## Data

- `research/data/phase21/fomc_dates.csv` — 296 announcements 1994-02-04 -> 2026-04-29
  (259 scheduled / 37 unscheduled), parsed from federalreserve.gov only
  (`fetch_fomc_dates.py`; historical pages 1994-2020 + current calendar 2021->).
  Validated: anchor dates (1994-02-04, 2008-12-16, 2017-02-01 cross-month,
  2020-03-15 unscheduled, 2020-03-18 cancelled-absent), 7-9 scheduled/yr cadence,
  spot-checks (liftoff 2015-12-16, 2001 intermeeting cuts unscheduled).
- SPX: `research/data/phase19/SPX_daily.csv` (on disk, PR #153).

## Honest reading

The drift was REAL in its documented era — our train window reproduces it at PF 1.9,
which is a useful harness-sanity result. But it does not certify TODAY: the OOS edge
over a generic long-the-tape placebo is 0.085 PF points, it needs the GFC/COVID
rebound meetings to clear the bar, annual consistency was never there (7 losing years
in train), and after 81 cumulative trials the deflated Sharpe says this could be
selection. The per-event gross edge is also small enough that 1.5x assumed costs
kills it. Classic post-publication decay shape (consistent with the literature on the
drift weakening after 2015).

## What I got wrong

- First parse attempt missed the slash month-span form ("April/May 30-1") used on
  2013+ historical pages — fetcher failed loudly (by design) and was fixed; no silent
  data loss.
- The placebo PF (1.22 on 2020-26 daily longs) is itself a reminder that ANY long-SPX
  episode study in this tape needs a beta benchmark; the K1 design did its job.
