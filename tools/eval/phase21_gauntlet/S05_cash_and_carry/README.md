# S05 — Crypto cash-and-carry basis — VERDICT: NONE

## Verdict block

```
VERDICT:   NONE — real, mechanically-sound carry that is STRUCTURALLY uncertifiable
           (fails the n>=200 gate AND deflated-Sharpe; not a rescuable near-miss).
CHAMPION:  roll60 (enter the last bar >= 60 cd before expiry IF annualized basis
           beats the cost hurdle; fully cost-derived, no tuning)
TRAIN:     PF 163.249 | n 26 | expiry < 2024-01-01 | net +$30,989 | win 92% | med ann carry 3.16%
HOLDOUT:   PF  34.178 | n 20 | expiry >=2024-01-01 | net +$18,580 | win 90% | med ann carry 6.69%
DSR:       0.0  (n_trials = 92 prior + 4 = 96)  <- the gate that pushes NEAR-MISS -> NONE
KILLS (ALL FIVE PASS — the edge is real and clean):
  K1 post-ETF holdout: PF 34.178, median ann net carry +6.69% (carry SURVIVED the
                       spot-ETF basis compression — unlike Phase-20c funding carry).
  K2 crisis-strip:     strip COVID / May-2021 delever / FTX -> full-sample PF 69.883,
                       +$968/episode. Normal-regime carry clears costs on its own.
  K3 monotonicity:     rho 1.0, quantile means [52, 463, 935, 1338, 2713]. PnL scales
                       monotonically with entry basis — the mechanical-arb fingerprint.
  K4 cost-cliff:       holdout PF stays >= 1.3 out to 7.0x costs. Enormous margin.
  K5 convergence:      max |F_last - S_last|/S_in = 0.75%. Data integrity confirmed.
OTHER VARIANTS (all profitable train+holdout, EVERY year):
  thr5  inf PF / inf PF  (100% win, 0 losers in 6 yrs) | med ann carry 5.1% / 7.5%
  thr10 inf PF / inf PF  (100% win)                    | med ann carry 9.9% / 11.2%
  thr15 inf PF / inf PF  (100% win)                    | med ann carry 14.6% / 14.8%
```

## What was tested (pre-registered in run.py before results)

Delta-neutral cash-and-carry on Binance COIN-margined dated quarterly futures:
long spot + short the dated future, hold to (near) convergence, capture the entry
basis. 50 contracts (23 BTC + 23 ETH expired, 2020-06 -> 2026-05; 4 still live),
daily closes from data.binance.vision; spot legs reuse the on-disk Phase-20
dailies. 4 entry variants (thr5/thr10/thr15 = first day annualized basis >= 5/10/15%;
roll60 = cost-derived roll), one episode per contract, split BY EXPIRY at 2024-01-01
(the post-spot-ETF compression era is the OOS test — the Phase-20c lesson). Costs:
Phase-20b constants verbatim ($280/RT at $100k notional). Deflation `--prior-trials 92`.

## Honest reading

This is the cleanest **mechanical** result of the whole P15-21 gauntlet, and it
still does not certify — for a structural reason, not a statistical one.

The carry is real: a positive-basis future held to delivery converges to spot, so
the entry basis is captured near-deterministically. That is why the threshold
variants posted a perfect record — **zero losing episodes in six years** — and why
monotonicity is a textbook rho = 1.0. Unlike the Phase-20c funding carry (which
inverted post-ETF), this dated-basis carry **survived** the 2024+ compression: the
holdout still paid a +6.7% median annualized net carry and cleared every kill test
out to 7x costs.

It fails anyway because the universe is structurally tiny. Crypto quarterly futures
expire four times a year; across all of history there are only ~21 expired BTC and
~21 expired ETH contracts, and one-episode-per-contract caps the sample at n = 14-26
per variant — far below the n >= 200 gate. The deflated Sharpe then finishes the
job: against 96 cumulative trials with a SR-variance pool of 0.45, the
expected-max-Sharpe from luck alone is 1.69, and the pre-registered champion (roll60,
per-trade SR 0.944) cannot clear it -> DSR 0.0. Two structural gates fail (n AND
DSR), so this is NOT a one-gate NEAR-MISS; the honest verdict is NONE.

The economic story is the standard one: this is a well-known, capacity-constrained,
crowded basis trade whose return is the compensation for tying up capital and bearing
the convexity/liquidation risk of an inverse contract (convexity is labeled-ignored
here). It is genuine and it persists — but this harness cannot, and should not,
certify a strategy on 20 holdout observations no matter how clean they look.

Consistent with the Stage-0 flag and with S03 (VIX contango, also real-but-n<200):
the honest n-gate STOP is applied, not forced. **Cumulative trials now 96.**

## What I got wrong

- The fetch's convergence integrity gate failed at 52% on the first run, and the
  first instinct ("data wrong") was itself wrong. Probing the raw klines showed two
  benign Binance export artifacts, both invisible until inspected bar-by-bar:
  (1) an expired symbol keeps emitting **frozen zero-volume daily rows** at the
  settlement price for months (BTCUSD_200925 -> 10687.6 @ vol 0 into 2021), and
  (2) the **settlement-day bar is partial** — COIN-M quarterlies settle ~08:00 UTC,
  so the expiry-day daily close is the 08:00 price while spot's daily close is 24:00,
  a ~16h gap that manufactured a fake +8.55% "basis" on the final bar (verified on
  BTCUSD_210625: ±0.3% on every full bar, then +8.55% on the 622k-vol settlement
  stub). Fix: keep only bars **strictly before** the settlement day -> 46/46
  contracts converge to < 0.6%. This was a methodology bug fixed at the data-integrity
  gate, BEFORE any PnL was computed (no peeking), and it makes the result *more*
  conservative (it leaves the last ~0.3% of basis on the table). Both run.py and
  fetch_data.py carry the label.
- The Stage-0 note predicted a NEAR-MISS ceiling. The realized ceiling is one notch
  lower (NONE), because the deflated Sharpe — not just the n-gate — fails once the
  inf-PF threshold variants are deprioritized and the lower-SR roll60 becomes the
  pre-registered champion. Reported as-is; the champion rule was not changed to
  rescue the verdict.
