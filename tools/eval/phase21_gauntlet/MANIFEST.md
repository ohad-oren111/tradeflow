# Phase-21 "The Gauntlet" — MANIFEST (standing work-order state)

**This file is the single source of truth for the Phase-21 multi-session work order.**
A fresh session resumes with ONLY this file + the standing work order: take the next
`PENDING` strategy in run-order, set it `IN-PROGRESS`, run it under the global honest-eval
rules, set it `DONE: <verdict>`, update `CUMULATIVE_TRIALS`, ship the PR, repeat.

```
CUMULATIVE_TRIALS: 92
```

(Starts at 78 after Phase-20c. Every eval passes the CURRENT value as `--prior-trials`,
then adds its own pre-registered variant count and writes the new value back here.)

## Run order (testable strategies only)

S01 → S02 → S03 → S04 → S05 → S06 → S09 → S10 → S11 → S12

Front-loaded by data cost (S01–S06 = on-disk/cheap-fetch; S09–S12 = the heavy EDGAR block,
S09 first because S10–S12 reuse its infra; S12 last per work order). S07/S08 and S13–S18
were closed at Stage-0 as UNTESTABLE-HERE (see table + STOP.md files).

## Status table

| ID | Name | Feasibility (Stage-0) | Status | Verdict | Headline | PR | Notes |
|----|------|----------------------|--------|---------|----------|----|-------|
| S01 | Pre-FOMC announcement drift | TESTABLE — Fed site 200 OK; SPX_daily.csv on disk (1970→) | DONE | NONE | champ T2_sched train PF 1.905 (n209) / HO PF 1.305 (n50); DSR 0.836<0.95; 4 fails: each-year (7/26 neg), DSR, placebo margin 0.085<0.10, crisis-strip HO 1.257; cost-cliff 1.5×; recency PASSED (decayed not dead) | S01 PR | 296 announcements 1994→2026 landed in research/data/phase21/fomc_dates.csv (259 sched/37 unsched); effect real in-era, uncertifiable today; +3 trials (78→81) |
| S02 | Dealer gamma/OPEX flows (calendar form) | TESTABLE — deterministic 3rd-Friday dates + SPX on disk | DONE | NONE | champ long_opex_week train PF 1.182<1.30 / HO 1.104; placebo Fridays BEAT it (1.405); recency 0.89-1.02; regime: pure above-trend beta; short_post_week HO 0.577 | S02 PR | Decisive NONE on the calendar form; gamma MECHANISM untested (unlock: options OI history); +3 trials (81→84) |
| S03 | VIX short-vol roll-yield | TESTABLE-PROXY — Yahoo ^VIX (1990→), ^VIX3M (Jul-2006→), SVXY (Oct-2011→, incl Feb-2018); VXX only 2018-series | DONE | NONE | champ contango (VIX<VIX3M): train PF 2.895/HO 2.937, DSR 0.98, ALL 5 kills pass (dodged Volmageddon +$63k AND COVID −$8k; no cost cliff @8×) — but n 58/34≪200 (structural) + 4 neg years | S03 PR | **Strongest raw result of P15–21.** n<200 STOP rule applied — not forced. Unlock noted: VIX futures curve history 2004→ would triple the era (different, heavier study). +4 trials (84→88) |
| S04 | Treasury auction concession/reversal | TESTABLE — fiscaldata auctions_query full history 1979→ (11,006 auctions, tenors/bid-to-cover/yields); phase19 UST10Y on disk; Yahoo ^FVX/^TYX OK | DONE | NONE | champ reversal_T3: train PF 1.451 (n717, real in-era) → HO 0.756 (SIGN FLIP); DSR 0.037; 2022+ QT era 0.584 (thesis REFUTED); 30Y leg neg; all 5 kills fail | S04 PR | 1,208 nominal 5/10/30Y auctions landed (2Y/3Y/7Y excluded, no free proxy); published-then-arbitraged shape; +4 trials (88→92) |
| S05 | Crypto cash-and-carry basis | TESTABLE, n-risk — data.binance.vision CM delivery quarterlies confirmed (BTCUSD_200925→260925, 25 symbols ≈21 expired; ETH expected same); spot on disk | PENDING | — | — | — | Episodes ≈40–90 << 200 structurally → NEAR-MISS ceiling; report honestly, don't inflate |
| S06 | Crypto liquidation/OI snapback | TESTABLE-PROXY — futures/um/daily/metrics zips confirmed (probed 2021-06-01 + 2026-06-10, both 200); funding+prices on disk | PENDING | — | — | — | No true liquidation feed; OI-drop+funding<0 signature proxy (label it); pin earliest metrics date during eval |
| S07 | Commodity carry/dynamic roll (COT) | UNTESTABLE-HERE — COT zips fine (200, 2.3MB) but NO free curve history: Yahoo delists expired contract months (CLZ20.NYM = "No data found"); Stooq is JS-challenge-walled | DONE | UNTESTABLE-HERE | curve-data gate | Stage-0 | Unlock: paid historical futures curves (Norgate/CSI/databento). See S07_commodity_carry/STOP.md |
| S08 | Commodity index-roll spread | UNTESTABLE-HERE — same curve dependency as S07 | DONE | UNTESTABLE-HERE | curve-data gate | Stage-0 | Falls with S07. See S08_index_roll_spread/STOP.md |
| S09 | SEC Form 4 insider cluster-buy | TESTABLE-HEAVY — form.idx OK (126,273 Form-4 in 2024Q1 sample), company_tickers.json OK, etiquette verified | PENDING | — | — | — | The prize; budget multiple sessions; universe cap pre-registered BEFORE price fetch; excess-vs-SPX scoring |
| S10 | Micro-cap PEAD (announcement-reaction) | TESTABLE-HEAVY — shares S09 infra; 10-K/Q+8-K ≈23k/qtr in sample | PENDING | — | — | — | Surprise proxied by announcement-day market-adjusted return (label proxy); 75bp/side micro-cap cost-cliff is THE test |
| S11 | NT 10-K/10-Q late-filing short | CONDITIONAL by construction — NT filings confirmed in index (391 in 2024Q1) | PENDING | — | — | — | Borrow unknowable → deliverable IS the breakeven-borrow rate; ceiling verdict CONDITIONAL |
| S12 | IPO lockup-expiration short | CONDITIONAL-HEAVY — 424B4 confirmed in index (101 in 2024Q1) | PENDING | — | — | — | 180-day lockup assumption documented + hand-check error rate on 20; run LAST (lowest priority of EDGAR block) |
| S13 | Merger arb (small/complex cash) | UNTESTABLE-HERE — no free historical deals DB (terms/closes/breaks); EDGAR DEFM14A outcome-labeling beyond honest scope | DONE | UNTESTABLE-HERE | deals-data gate | Stage-0 | Unlock: a deals dataset. See S13_merger_arb/STOP.md |
| S14 | SPAC trust arbitrage | UNTESTABLE-HERE — free SPAC universe/trust-NAV/timeline sources partial+dirty | DONE | UNTESTABLE-HERE | SPAC-data gate | Stage-0 | Unlock: SPAC database. See S14_spac_trust/STOP.md |
| S15 | Spinoff/forced index unwind | UNTESTABLE-HERE — Form 10 parsing doable but outcome/index-membership labeling not free | DONE | UNTESTABLE-HERE | membership-data gate | Stage-0 | Unlock: spinoff + index-membership data. See S15_spinoff_unwind/STOP.md |
| S16 | VIX ETP EOD rebalance → SPX reversal | UNTESTABLE-HERE — effect lives in the last 30 min; daily data cannot see it | DONE | UNTESTABLE-HERE | granularity gate | Stage-0 | Unlock: intraday SPX/ES. (nq_1min.csv exists on disk but is the WRONG instrument; work order forbids a proxy here.) See S16_vix_eod_rebalance/STOP.md |
| S17 | Closing-auction imbalance/reversion | UNTESTABLE-HERE — auction imbalance feeds are proprietary | DONE | UNTESTABLE-HERE | feed gate | Stage-0 | Unlock: imbalance feed (NYSE/Nasdaq). See S17_closing_auction/STOP.md |
| S18 | Russell reconstitution | UNTESTABLE-HERE — 1 event/yr → n≈40 max, structurally fails n≥200 | DONE | UNTESTABLE-HERE | n gate | Stage-0 | Not run, per work order. See S18_russell_recon/STOP.md |

## Stage-0 probe evidence (2026-06-13, this VPS)

- federalreserve.gov/monetarypolicy/fomccalendars.htm → HTTP 200.
- Yahoo v8 chart (phase19 fetch pattern): ^VIX firstTrade 1990; ^VIX3M 2006-07; SVXY 2011-10; VXX 2018-01 (current series only); CLZ26.NYM live OK but **CLZ20.NYM → "No data found, symbol may be delisted"** (expired months are purged → no honest curve history).
- Stooq (clz20.f) → JavaScript browser-verification wall → not scriptable.
- api.fiscaldata.treasury.gov `/v1/accounting/od/auctions_query` → 200; first record auction_date **1979-10-31**; total-count **11,006**; fields incl. security_term, bid_to_cover_ratio, high_yield, auction_date. (TreasuryDirect TA_WS reachable but ignores historical filters.)
- data.binance.vision S3 listing → CM delivery quarterlies confirmed: ADAUSD_200925… and **BTCUSD_200925 → BTCUSD_260925 (25 symbols)**. UM daily metrics zips → 200 for 2021-06-01 and 2026-06-10.
- CFTC deacot2024.zip → 200 (2.3MB) — fine, but moot for S07 given the curve gate.
- SEC EDGAR full-index/2024/QTR1/form.idx → 200, 57.8MB; counts: Form 4 = 126,273; NT 10-K/Q = 391; 424B4 = 101; 10-K/10-Q/8-K = 23,049. company_tickers.json → 200.
- Known geo-blocks (carried): binance.com API 451, Bybit 403, FRED unreachable. data.binance.vision + Yahoo + SEC + fiscaldata all work.

## Final Synthesis (after all 18 DONE)

`RATINGS.md` + `ratings.json` per the work order: full 20-row rating table (18 + funding
carry + month-end ES/ZN), ranked tiers, cumulative honest accounting, single best next
action. Then mark this file `GAUNTLET COMPLETE`.

## Gauntlet status: IN PROGRESS — 12/18 DONE, next up S05
