# Phase-20a — crypto funding-carry data sourcing

**Status: data landed + gate PASSED. The carry model was NOT run** (that is Phase-20b, a
separate eval PR). Research-only / data-only. No broker, no IB connection, no DB, no
secrets, no `src/**`/`config/**`, halt untouched. This lands the data the funding-carry
hypothesis needs — the first candidate from a structurally different category than the
Phase-15→19 price/calendar patterns (a carry / risk-premium with a named mechanical
source: leveraged perp longs persistently paying funding).

Regenerate (idempotent; needs network egress — run un-sandboxed):

```
python -m tools.eval.phase20_funding_carry.fetch_data
```

It overwrites the 24 CSVs atomically (tmp → rename) and **fails loudly (non-zero exit)**
on an unreachable source or an empty series — it never writes a partial/empty CSV.

---

## Task A — audit

### A.1 Loader schema (matched)
Mirrors the phase-19 daily schema (`research/data/phase19/*.csv`) and the
`tools/eval/phase16/dataset.py` → `tools/eval/data.load_history` family:
- **OHLCV** (`<COIN>_spot.csv`, `<COIN>_perp.csv`): `time,open,high,low,close,volume`,
  one UTC-midnight tz-aware row per trading day, sorted, de-duped, null-dropped.
- **Funding** (`<COIN>_funding.csv`): `time,funding_rate`, one row per funding interval,
  `time` UTC tz-aware.

### A.2 Source reachability (probed BEFORE building — Constraint 1)
| Source | Result | Use |
|---|---|---|
| Binance API `fapi.binance.com` | **HTTP 451** (geo-block, US IP) | unusable directly |
| Bybit `api.bybit.com` | **HTTP 403** (CloudFront geo-block) | unusable |
| Coinglass `open-api-v4.coinglass.com` | **HTTP 401** "API key missing" | skipped (no key invented, per brief) |
| OKX `www.okx.com` | **HTTP 200** reachable | rejected: `funding-rate-history` retains only ~3 months (≈277 intervals) — too shallow for a multi-year carry test |
| **`data.binance.vision`** (Binance public data dump) | **HTTP 200** reachable | **USED** |

**Venue used: Binance USDⓈ-M, via the public historical-data dump `data.binance.vision`.**
This is a static data CDN — NOT the geo-blocked trading API — so it is reachable from
this US (Hetzner/Ashburn) VPS, and it carries Binance funding + klines back to listing.
Funding carry is exchange-agnostic, and Binance is the canonical funding-carry venue;
keeping spot + perp + funding all on ONE venue yields a clean perp−spot basis.

Methods (monthly `.zip` archives, parsed in-memory):
- Funding: `…/futures/um/monthly/fundingRate/<SYM>/<SYM>-fundingRate-YYYY-MM.zip`
  (CSV cols `calc_time,funding_interval_hours,last_funding_rate`).
- Perp OHLCV: `…/futures/um/monthly/klines/<SYM>/1d/<SYM>-1d-YYYY-MM.zip`.
- Spot OHLCV: `…/spot/monthly/klines/<SYM>/1d/<SYM>-1d-YYYY-MM.zip`
  (kline cols `open_time,open,high,low,close,volume,close_time,quote_volume,…`).

### A.3 Units (§0.5.97, verified against the source files)
- **Funding rate = per-interval FRACTION** (Binance `last_funding_rate`; e.g.
  `-0.00012359` = −0.012359% per interval). **NOT** percent, **NOT** bps.
- **Funding interval = 8h** (modal `funding_interval_hours` = 8 for every coin landed;
  also recoverable from the `time`-column spacing).
- **Spot + perp both quoted in USDT** (pairs `<COIN>USDT`).
- **`volume` column = quote (USDT) notional** traded (Binance kline `quote_volume`,
  col 7) — a cross-series-comparable dollar volume.

Two source quirks handled in `fetch_data.py`:
- Some monthly files carry a header row, some don't → non-numeric first cell is skipped.
- Binance switched kline timestamps from ms to **microseconds** in 2025 → any epoch
  `>= 1e14` is divided by 1000 (`_to_ms`); otherwise a µs value parses as year ~56000.

---

## Task C — data gate (Phase-17 Stage-1 style)

**Venue: Binance USDⓈ-M (`data.binance.vision`). Funding interval: 8h. Monthly archives,
so every series ends at the last complete published month (2026-05-31); the current
partial month is intentionally excluded.**

| Coin | Tier | Funding intervals | Funding span | Spot bars | Perp bars | Price span | basis mean | basis abs |
|---|---|---|---|---|---|---|---|---|
| BTC | major | **7029** | 2020-01-01 → 2026-05-31 | 2465 | 2343 | 2019-09 → 2026-05 | −0.0150% | 0.0484% |
| ETH | major | **7029** | 2020-01-01 → 2026-05-31 | 2465 | 2343 | 2019-09 → 2026-05 | −0.0086% | 0.0520% |
| SOL | alt | **6334** | 2020-09-13 → 2026-05-31 | 2120 | 2081 | 2020-08 → 2026-05 | −0.0311% | 0.0788% |
| XRP | alt | **7013** | 2020-01-06 → 2026-05-31 | 2465 | 2333 | 2019-09 → 2026-05 | −0.0163% | 0.0708% |
| BNB | alt | **6908** | 2020-02-10 → 2026-05-31 | 2465 | 2303 | 2019-09 → 2026-05 | −0.0079% | 0.0625% |
| DOGE | alt | **6455** | 2020-07-10 → 2026-05-31 | 2465 | 2152 | 2019-09 → 2026-05 | −0.0140% | 0.0657% |
| AVAX | alt | **6232** | 2020-09-22 → 2026-05-31 | 2078 | 2077 | 2020-09 → 2026-05 | −0.0218% | 0.0744% |
| LINK | alt | **6980** | 2020-01-17 → 2026-05-31 | 2465 | 2327 | 2019-09 → 2026-05 | −0.0111% | 0.0660% |

**Totals: 53,980 funding intervals across 8 coins. 0 dropped.**

- **Funding-interval count:** every coin clears the honest **n ≥ 200** bar by ~30× (the
  high-frequency win over month-end: ~3 intervals/day × ~5–6 years). This is the binding
  improvement over Phase-19, where a monthly cadence capped usable events.
- **Continuity:** each series is monotonic-increasing, de-duplicated, no null OHLC/rate
  (asserted in `_assert_clean`; the script exits non-zero otherwise). Daily price has the
  expected one-row-per-UTC-day cadence; funding is 8h.
- **Units sanity:** funding `|rate|` median ≪ 0.01 and max < 0.5 (asserted) — a small
  fraction, not %/bps. Sample BTC funding rows: `2020-01-01 00:00:00+00:00,-0.00012359`.
  Sample BTC perp OHLCV: `2020-01-01 00:00:00+00:00,7189.43,7260.43,7170.15,7197.57,…`.
- **Basis sanity:** perp−spot mean basis is −0.03%..−0.008% and mean-abs basis
  0.05%..0.08% across all coins — tiny, so the delta-neutral (long spot / short perp)
  assumption the carry trade relies on holds; the spot and perp legs track within a few
  bps. (Spot history starts slightly before perp for several coins — spot listed first;
  the carry trade only spans the overlapping window, which the eval will intersect.)

**Gate verdict: PASS for all 8 coins.** Model not run.

---

## Task E — out-of-scope note (flag for the Phase-20b eval brief; do NOT solve here)

The carry eval will need to specify (not in this PR):
- **Trade definition:** enter the delta-neutral carry (long spot, short perp) on a
  funding-rate threshold, hold while funding stays positive (longs pay shorts), exit on a
  flip / threshold-cross; direction reverses for persistently-negative funding.
- **Cost model:** BOTH legs pay a taker fee on entry AND exit (spot taker + perp taker,
  ×2 round-trips for the pair), plus perp-leg slippage; the funding earned must clear that
  four-fee drag. Use Binance's real spot + USDⓈ-M taker tiers (verify from source, §0.5.97).
- **Spike-strip kill test:** funding spikes (e.g. squeeze events) can dominate the carry
  P&L; strip the top funding-spike intervals and confirm the edge survives on the body.
- **Deflation:** Phase-20b passes `--prior-trials 40` (cumulative after Phase-19), so
  funding carry must clear a HIGHER multiple-testing bar than month-end did.
- **Interval handling:** funding is 8h here; if any sub-window used a different interval,
  reconcile via the `time`-column spacing (the landed timestamps encode it).

---

## Offline / scope guarantee

Nothing here connects to IB / the broker / a DB, reads secrets, or modifies any
`src/**` / `config/**` / compose / image / halt. It only downloads public Binance
historical-data files over HTTPS and writes CSVs. The live bot was untouched throughout.

## What I got wrong

- First built against OKX REST (reachable), but its public `funding-rate-history` only
  retains ~3 months (277 intervals) — far too shallow. Switched to `data.binance.vision`
  (public dump, US-reachable, full multi-year history) for all three series.
- Hit a units trap: Binance moved kline timestamps to microseconds in 2025, so recent
  bars parsed as year ~56000 until I normalised ms-vs-µs (`_to_ms`). Funding `calc_time`
  stayed ms; klines did not — the kind of source drift §0.5.97 exists to catch.
