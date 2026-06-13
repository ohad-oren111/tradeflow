# S04 — Treasury auction concession / reversal — VERDICT: NONE

## Verdict block

```
VERDICT:   NONE (decisive — OOS sign flip)
CHAMPION:  reversal_T3 (long duration auction-close -> close T+3, pooled 5/10/30Y)
TRAIN:     PF 1.451 | n 717 | 1980->2015     (the documented effect, visible in-era)
HOLDOUT:   PF 0.756 | n 486 | 2015->2026     <- the edge INVERTED out of sample
DSR:       0.037 (n_trials = 88 prior + 4 = 92)
KILLS (all fail):
  K1 placebo:        real holdout 0.756 vs 7-td-earlier placebo 0.81 — no event lift.
  K2 tenor breadth:  5Y +$89, 10Y +$30, 30Y -$50 per episode — not cross-tenor.
  K3 recency:        PF 0.756 (2015+), 0.584 (2022+). The QT-era thesis predicted
                     STRONGEST here; the data says WORST here. Thesis refuted, not
                     just unconfirmed.
  K4 crisis-strip:   stripped holdout 0.752 (crises were not the problem).
  K5 cost-cliff:     0.5x (moot).
OTHER VARIANTS: concession_T1 0.857/0.937, concession_T2 0.992/1.225 (the only
leg positive OOS, but train PF 0.992 = noise), combined 1.182/0.819.
```

## What was tested (pre-registered in run.py before results)

The auction-cycle concession/reversal on daily yield-derived price indexes
(5Y=^FVX D4.5, 10Y=^TNX D7.0 on disk, 30Y=^TYX D17.0; D_mod constant = magnitude
only). 1,208 nominal coupon auctions 1979->2026 from fiscaldata (2Y/3Y/7Y excluded
— no free index proxy; counted in fetch). 4 variants, ZF/ZN/ZB futures costs,
split 2015-01-01.

## Honest reading

The post-auction reversal was real in the 1980-2014 tape (PF 1.45 on n=717 — too
big to be noise) and is GONE-to-inverted since 2015: exactly the
published-then-arbitraged pattern (the dealer-balance-sheet literature documenting
it dates to 2010-2013). The 2022+ QT-era number (0.584) actively refutes the
"supply era makes it stronger" story — if anything, auction days now carry
post-auction WEAKNESS, consistent with the market having moved the concession
earlier/elsewhere. Nothing here is rescuable by variant search, and the one
OOS-positive leg (concession_T2 holdout 1.225) has a train PF of 0.992 — a
coin-flip in-sample that happened to land well OOS; certifying it would be exactly
the selection error this harness exists to prevent.

## What I got wrong

- The fetch validation expected >=2500 mapped auctions; reality is 1,208 (2Y/3Y/7Y
  are the most frequent tenors and are excluded for lack of a free index proxy).
  The script failed loudly and the threshold was corrected with the reason
  documented — no silent data shrink.
- I pre-registered "QT era should be strongest" as the K3 rationale; the result
  is the opposite. Recorded as a refutation, which is information: the next
  duration-supply hypothesis should not assume the 2010-era mechanism survived.
