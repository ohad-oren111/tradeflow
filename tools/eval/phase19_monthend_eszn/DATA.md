# Phase-19 — data sourcing (decades of S&P 500 + 10Y Treasury daily history)

**Status: data landed + gate PASSED. The month-end model was NOT run** (separate follow-up
PR). Research-only / data-only. No broker, no DB, no secrets, no `src/**`/`config/**`, halt
untouched. This revives the Phase-19 hypothesis that
[`README.md`](README.md) STOPPED at the data gate (PR #152).

Regenerate (idempotent; needs network egress — the sandbox proxy rate-limits Yahoo, so run
un-sandboxed):

```
python -m tools.eval.phase19_monthend_eszn.fetch_data
```

It overwrites the three CSVs and **fails loudly (non-zero exit)** if any source is
unreachable — it never writes a partial/empty CSV.

---

## What landed (`research/data/phase19/`)

Schema matches the phase-16/17 daily loader (`tools/eval/phase16/dataset.py` →
`tools/eval/data.load_history`; `tools/eval/phase17/dataset.py::_load_one`):
`time,open,high,low,close,volume`, one UTC-midnight tz-aware row per trading day, sorted,
null-close dropped, de-duped.

| File | Series | Role | Units / construction |
|---|---|---|---|
| `SPX_daily.csv` | S&P 500 cash index (`^GSPC`) | **ES proxy** | Real OHLCV, **price index** (not total-return) — correct for a futures proxy (Constraint 3). |
| `IEF_daily.csv` | iShares 7–10Y Treasury ETF | **ZN proxy (clean primary)** | Dividend/split-**adjusted close** (total-return). Single meaningful value → OHLC = adjclose (Constraint 1); real volume. |
| `UST10Y_pricereturn_daily.csv` | 10Y CMT yield → bond **price-return** index | **ZN proxy (long history, futures-faithful)** | Built from `^TNX` yield via modified-duration (below). **Price index, NOT yield** (Constraint 2). OHLC = close, volume = 0. |

---

## Task A — audit

**A.1 Loader schema (matched):** the daily loaders read a `time` column (parsed to UTC,
normalised to date) and require `close`; the canonical OHLCV columns are
`time, open, high, low, close, volume` (verified against `research/data/phase17/MES_daily.csv`,
which adds a futures-only `active_expiry` column we omit — cash/ETF series have no expiry).

**A.2 Sources + reachability (probed from the VPS):**
- **Yahoo v8 chart JSON** (`query1.finance.yahoo.com/v8/finance/chart/<sym>`) — keyless,
  reachable (un-sandboxed; the sandbox egress proxy returns "Edge: Too Many Requests").
  Used for all three series via `period1=0` (full history).
- **Stooq** (`^spx`, `ief.us`) — **unusable headless**: returns a JavaScript proof-of-work
  bot wall, not CSV.
- **FRED `DGS10`** (`fred.stlouisfed.org/graph/fredgraph.csv`) — the brief's first choice for
  the yield leg (Constraint 5b). **Unreachable from this VPS**: `curl` times out repeatedly,
  sandboxed and not (firewalled host). Substituted with `^TNX` (next line).

**A.3 Units / mapping (verified, §0.5.97):**
- `^GSPC` = S&P 500 **price index** level (1970 ≈ 93, 2026 ≈ 7427). → ES return proxy.
- `IEF` adjusted close = Treasury **total-return** level (2002 ≈ 40.6 adj, 2026 ≈ 94.2). →
  ZN proxy; the TR-vs-price nuance is immaterial over a 5–8-day month-end window (Constraint 3).
- `^TNX` = CBOE **10-Year constant-maturity Treasury YIELD**, quoted **directly in percent**
  (verified: 1970 ≈ 7.86 → 7.86%, 2026-06 ≈ 4.55 → 4.55%; **not** ×10). Same underlying as
  FRED `DGS10`. A yield, so it is **converted to a bond price-return** (never stored as price).

---

## Bond yield → price-return conversion (Constraint 2 — the load-bearing step)

`^TNX` is a yield; the eventual strategy trades ZN (bond **price**). Conversion, first-order
modified duration:

```
price_return_t  ≈  −D_mod · Δy_decimal           Δy_decimal = (y_t − y_{t−1}) / 100
P_t             =  P_{t−1} · (1 + price_return_t)  P_0 = 100
```

- **`D_mod = 7.0`** — a 10Y constant-maturity note / ZN CTD sits in the ~6.5–8 range (CME
  *Understanding Treasury Futures*; standard 10Y note duration ~7–8). **Duration enters as a
  constant multiplier, so its exact value scales the bond-return MAGNITUDE only — it cannot
  change the SIGN or TIMING of the month-end signal.** Convexity and carry/roll-down are
  ignored (first-order proxy; fine for small daily Δy over a short window).
- The index **rises when yield falls** (a bond rally) — the correct futures-long direction.
- **Cross-check:** `UST10Y` daily returns vs `IEF` daily returns correlate **0.947** over
  their 2002–2026 overlap — the two independent ZN proxies describe the same exposure.

---

## Task C — data-gate results (Phase-17 Stage-1 style)

| Series | Rows | Span | Months | Max gap | Last close | Units sanity |
|---|---|---|---|---|---|---|
| SPX | 14,233 | 1970-01-02 → 2026-06-12 | **678** | 7 d | 7426.81 | equity index level ✓ |
| IEF | 6,007 | 2002-07-30 → 2026-06-12 | **288** | 5 d | 94.22 | price level (≫20) → not yield ✓ |
| UST10Y | 14,133 | 1970-01-02 → 2026-06-12 | **678** | 7 d | 107.76 | price index (base 100) → not yield ✓ |

- **Continuity:** dates monotonic increasing, no duplicates, no NaN closes. Max gaps (5–7 d)
  are holiday/closure-adjacent weekends (e.g. long weekends; week-long exchange closures in
  the historical record) — no unexplained multi-day gaps.
- **n_monthly_events (the gate that killed PR #152):** a month-end strategy fires ~1 event/month,
  so usable events ≈ overlapping months:
  - **SPX ∩ IEF = 288 months ≥ 200 → PASS** (clean ~24-yr window).
  - **SPX ∩ UST10Y = 678 months ≥ 200 → PASS** (long ~56-yr robustness window).
  - Either window clears the honest **n ≥ 200** bar **with room for a train/holdout split**
    (288 → e.g. ~190 train / ~98 holdout; 678 → far more). The n<200 blocker from PR #152 is
    resolved.
- **Bond-proxy agreement:** IEF vs UST10Y return corr = **0.947**.

**Gate verdict: PASS for both equity-bond pairings.** Model not run.

---

## Task E — out-of-scope note (flag only; do NOT change here)

The campaign's fixed TRAIN/HOLDOUT split lives in `tools/eval/phase16/dataset.py`
(`TRAIN_END = "2025-09-01"`), calibrated for the short ~27-month NQ 1-min tape. **It is wrong
for decades of monthly data** — with this much history the follow-up eval should redefine the
split (e.g. TRAIN through ~2013 / HOLDOUT after, or a walk-forward), and recount the
multiple-testing trial budget. **This PR does not touch that split** — it is a note for the
follow-up month-end eval brief.

---

## Honest limitations

- **TR vs price (IEF):** IEF is total-return (coupons reinvested); ZN futures price excludes
  coupon. Immaterial within a 5–8-day window; the long-history `UST10Y` price-return series is
  the futures-faithful cross-check.
- **Duration proxy (UST10Y):** first-order, constant `D_mod`, no convexity/carry — magnitude
  approximation only; signal sign/timing unaffected (above).
- **`^TNX` substitution for FRED `DGS10`:** same 10Y CMT yield concept, different vendor (CBOE
  vs FRED) because FRED is unreachable from the VPS. Verified units match (percent).
- **Cash-index vs futures basis:** the ES/ZN basis (carry) is ~constant within a month-end
  window, so price-return proxies are appropriate; the eval should still apply real ES/ZN
  costs + a month-end-close slippage term (follow-up brief).
