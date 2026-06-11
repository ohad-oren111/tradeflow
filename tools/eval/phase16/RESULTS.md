# Phase-16 — Wide Strategy Discovery — running results ledger

Research-only (CLAUDE.md §0.5.208 MEASURE lane). Drives **no** prod path. Multi-session
campaign; one batch per session, full table (incl. failures) reported each batch.

## Protocol (fixed, pre-registered — see `gates.py`, `dataset.py`)

- **Split once.** TRAIN `2024-03-01..2025-09-01` (519,267 bars) — search here ONLY.
  HOLDOUT `2025-09-01..2026-07-01` (266,969 bars; tape ends 2026-06-02) — touched ONCE
  per train-survivor, ever.
- **Gates.** TRAIN: PF≥1.30, exp/ct≥$5, n≥200, profitable each train year. HOLDOUT:
  PF≥1.30, exp/ct≥$5, each holdout year +, PF≥0.75×train. DEFLATION: Deflated Sharpe
  Ratio ≥0.95 over the **cumulative** trial count (this batch + all prior — pass
  `--prior-trials`).
- **Costs.** $0.62/side commission + conditional slippage (entry/stop/market 1 tick,
  limit target ½ tick). Pessimistic fills: next-bar open, stop-before-target,
  gap-through fills at the bar open. 0/1/2-tick sensitivity reported on survivors.
- **Citation policy** (operator, 2026-06-11): texts are not on the VPS (copyright);
  each family cites the canonical formulation by author/work/chapter, flagged
  `to-verify-against-source`. No page numbers invented.

Run: `python -m tools.eval.phase16.run_batch --out /tmp/phase16_batchN.txt [--prior-trials K]`

## Campaign trial ledger (for cumulative deflation)

| Batch | Families | Variants (trials) | Cumulative trials | Train-survivors | Cleared |
|------:|---------:|------------------:|------------------:|-----------------|---------|
| 1     | 10       | 22                | 22                | none            | none    |

## Batch 1 (Session 28, 2026-06-11) — single-instrument, NQ==MNQ

**VERDICT: NONE.** No family clears even the structural TRAIN gates, so the holdout was
**not touched** (zero holdout budget spent). Honest finding, consistent with BT-1 ("no
candidate clears OOS PF≥1.20") and Phase-12 GATE-CORRECT (mean-reversion has no robust
edge here) and Phase-15 (NONE-YET).

Train champion per family (full grid logged in `/tmp/phase16_batch1.txt`):

| family  | champ        |     n |   PF | exp$/ct |    net$ | yrs | train gate |
|---------|--------------|------:|-----:|--------:|--------:|-----|------------|
| ewmac   | ewmac64_256  |  2762 | 1.12 |   5.57  | +15,371 | +/+ | FAIL (PF)  |
| tsmom   | tsmom240     | 12387 | 0.90 |  −1.77  | −21,921 | −/− | FAIL       |
| orb     | orb15        |  1236 | 1.06 |   2.97  |  +3,675 | +/+ | FAIL       |
| orbreg  | orbreg15     |  1221 | 1.06 |   3.28  |  +4,003 | +/+ | FAIL       |
| boll    | boll_chop    | 15525 | 0.88 |  −1.46  | −22,644 | −/− | FAIL       |
| rsi2    | rsi2_5       | 10905 | 0.86 |  −1.45  | −15,795 | −/− | FAIL       |
| gapfade | gapfade2     |   259 | 1.29 |  10.53  |  +2,727 | +/+ | FAIL (PF)  |
| nrbo    | nrbo7        |  6621 | 1.00 |   0.11  |    +749 | −/+ | FAIL       |
| vwaprev | vwaprev15    |  3220 | 0.97 |  −0.89  |  −2,867 | −/+ | FAIL       |
| tod     | tod14        |   364 | 1.16 |   7.16  |  +2,606 | +/+ | FAIL (PF)  |

### Reads
- **Mean-reversion (boll, rsi2, vwaprev) is net-NEGATIVE after costs on 1-min** —
  commission + slippage swamp the thin reversion edge. Corroborates the regime-gate /
  below-trend studies: no robust MR edge on this tape.
- **Momentum/trend (ewmac, tsmom):** fast TSMOM bleeds (−$1.7/ct); only the SLOWEST
  EWMAC (64/256) is net-positive (+$5.6/ct) but PF 1.12 ≪ 1.30. Trend exists but costs
  + 1-min noise erode it below the bar.
- **Three families fail ONLY the PF≥1.30 bar:** `ewmac64_256` (1.12), `gapfade2`
  (1.29, n=259), `tod14` (1.16). NOT tuned to force a pass (the brief's failure mode).
  Even `gapfade2`'s per-trade Sharpe 0.086 is barely above the deflation benchmark
  E[max SR]=0.072 over just 22 trials — it would not survive cumulative deflation.
- **`gapfade` is the only marginal lead** worth a pre-registered, deflation-aware look
  in a later batch (more gap variants / a directional-bias filter) — via the protocol,
  never by loosening the gate.

## Backlog (future batches — families not yet tried)

- **AFML** triple-barrier labeling + meta-labeling on a base momentum signal (pure
  numpy logistic meta-label; no sklearn).
- **Carver** carry (front/back calendar yield), fast/slow momentum blend (multi-speed
  forecast combination), vol-target sizing overlay.
- **Chan pair / stat-arb** MNQ–MES: Engle-Granger cointegration + rolling-OLS / Kalman
  dynamic hedge (BLOCKED on MES backfill — fetch via clientId-99, in progress).
- **Classic:** prev-day-high/low breakout, range-compression swing (multi-day NR),
  intraday seasonality by ET-hour bucket, ORB with ATR-target exits, momentum-of-VWAP.
