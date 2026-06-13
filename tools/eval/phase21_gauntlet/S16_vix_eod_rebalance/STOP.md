# S16 — VIX ETP end-of-day rebalance → SPX reversal — UNTESTABLE-HERE

**Verdict: UNTESTABLE-HERE (granularity gate). Not run.**

The documented effect is concentrated in the last ~30 minutes of the session (ETP issuers
hedging vega into the close) with a partial overnight/next-open reversal. Daily OHLC
cannot see a 30-minute window: close-to-close returns mix the effect with the whole day's
flow, and "high/low" carry no timestamps. Any daily-bar version would be testing a
different hypothesis. Per the work order: no proxy.

Note for the record: `research/data/nq_1min.csv` (26 months of NQ 1-min) exists on disk,
but NQ is the wrong instrument for a VIX-ETP-flow effect on SPX/ES, and 26 months is thin
for an EOD seasonal; the work order explicitly forbids a proxy here, so none was run.

**Unlock:** intraday (1-min or 5-min) SPX or ES history covering 2012→now (the ETP era).
