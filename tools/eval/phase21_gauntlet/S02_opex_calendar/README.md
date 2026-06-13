# S02 — OPEX calendar effect — VERDICT: NONE

## Verdict block

```
VERDICT:   NONE (decisive — nothing close)
CHAMPION:  long_opex_week (long SPX 2nd-Friday close -> 3rd-Friday/OPEX close)
TRAIN:     PF 1.182 | n 336 | 1984->2012      < 1.30 -> holdout SEALED for gating
HOLDOUT:   PF 1.104 | n 173 | 2012->2026
DSR:       0.637 (n_trials = 81 prior + 3 = 84)
ALL FIVE kill tests fail or are moot:
  K1 placebo:    non-OPEX Fridays (1st/2nd/4th pooled) holdout PF 1.405 — the placebo
                 BEATS the real anchor. OPEX week is a BELOW-average long week OOS.
  K2 recency:    PF 1.021 (2015+), 0.892 (2020+) — dead in the modern gamma era.
  K3 regime:     exp +$156/episode above 200d SMA, -$134 below — pure trend beta.
  K4 crisis-strip: stripped train 1.287 / holdout 1.211, both < 1.30.
  K5 cost-cliff: 0.5x — under the bar even at HALF assumed costs.
OTHER VARIANTS: short_post_week holdout PF 0.577 (actively destructive);
                combined holdout PF 0.803.
```

## What was tested (pre-registered in run.py before results)

The CALENDAR pattern around monthly option expiration (3rd Friday), honest-labeled:
the dealer-gamma MECHANISM was never testable here (needs options OI history). 3
variants (long OPEX week / short post-OPEX week / combined), 509 expiration months
1984->2026, phase16 gates + ES costs, split 2012-01-01.

## Honest reading

There is no OPEX-week calendar edge on SPX at this bar, and the kill battery shows
the residual long-OPEX-week PnL is just above-trend market beta concentrated in the
pre-2012 tape. The post-OPEX-week weakness documented in older literature is real
enough that BEING SHORT it still loses money (PF 0.577 OOS) — the weakness is too
small to overcome costs and the tape's upward drift. The gamma-mechanism hypothesis
remains untested (and untestable here) — that unlock is options OI history, noted in
the manifest; nothing in the calendar shadow of it suggests urgency.

## What I got wrong

- Nothing material to flag: the eval ran exactly as registered, first pass. The one
  judgment call was pooling 1st/2nd/4th Fridays as the placebo rather than testing
  each separately — pooling is the stronger (harder-to-pass) comparator and it
  decided nothing that the other four failures didn't already decide.
