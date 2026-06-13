# S07 — Commodity carry / dynamic roll (COT-filtered) — UNTESTABLE-HERE

**Verdict: UNTESTABLE-HERE (curve-data gate). Not run.**

The strategy needs multi-year history of individual futures contract months (the curve)
to compute backwardation/contango. Stage-0 probes (2026-06-13):

- **Yahoo**: live/forward months work (`CLZ26.NYM` → full chart JSON), but **expired
  months are purged** — `CLZ20.NYM` → `"No data found, symbol may be delisted"`. Without
  expired months there is no historical curve, only the current snapshot. A backtest on
  forward-months-only would be survivorship-shaped garbage; refusing to run it.
- **Stooq** (the only other free candidate probed): `stooq.com/q/d/l/?s=clz20.f` returns a
  JavaScript browser-verification challenge — not honestly scriptable from this VPS.
- **CFTC COT** (the filter leg): fine — `deacot2024.zip` → HTTP 200, 2.3MB, full history
  series exist per year. The filter is NOT the gate; the curve is.

**Unlock:** any paid historical futures database with per-contract-month daily settles
(Norgate, CSI, databento, Barchart). With curves landed, the pre-registered spec is in the
Phase-21 work order (long backwardated / short contangoed, COT-filtered, monthly rebalance,
n ≈ 12/yr × 20 commodities × 20yr).
