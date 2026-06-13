# S14 — SPAC trust arbitrage — UNTESTABLE-HERE

**Verdict: UNTESTABLE-HERE (SPAC-data gate). Not run.**

The trade (buy below trust NAV, redeem or sell at deal/liquidation) needs per-SPAC trust
NAV per share, redemption deadlines, extension votes, and outcome timelines. Free sources
(EDGAR S-1/8-K parsing, community spreadsheets) are partial and dirty: trust values drift
with interest accrual and extensions, deadlines move via votes, and mislabeling either side
turns the "arb" into noise. A clean reconstruction is a data-engineering project on its
own, not a side quest of this gauntlet.

**Unlock:** a SPAC database with trust NAV + timeline fields (e.g. SPACInsider,
spactrack-class data). The strategy itself is then a simple episode eval.
