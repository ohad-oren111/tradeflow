"""Phase-19 DATA SOURCING (research-only, data-only) — land decades of daily history.

Sources, validates, and writes the long-history daily series the (shelved) Phase-19
month-end ES/ZN rebalance test needs. It does **not** run any model — that is a separate
follow-up PR. No broker, no DB, no secrets, no live code.

WHAT IT LANDS (research/data/phase19/):
  * ``SPX_daily.csv``                 — S&P 500 cash PRICE index (ES proxy). Real OHLCV.
  * ``IEF_daily.csv``                 — iShares 7-10Y Treasury, dividend/split-ADJUSTED
                                        close (total-return ZN proxy). Clean primary.
  * ``UST10Y_pricereturn_daily.csv``  — 10Y constant-maturity yield converted to a bond
                                        PRICE-return index (futures-faithful ZN proxy,
                                        long history). See ``yield_to_price_index``.

SOURCE (all Yahoo v8 chart JSON — keyless):
  * ``^GSPC`` S&P 500 index, ``IEF`` ETF, ``^TNX`` CBOE 10Y T-Note YIELD index.
  * FRED ``DGS10`` was the FIRST choice for the yield leg (Constraint 5b) but
    ``fred.stlouisfed.org`` is UNREACHABLE from this VPS (curl times out, sandbox and
    not). ``^TNX`` is the SAME underlying — the 10Y constant-maturity Treasury yield,
    quoted directly in percent (verified: last ~4.55 == 4.55%, 1970 ~7.86 == 7.86%) —
    so it is a faithful, reachable substitute. Documented in DATA.md (§0.5.97).

BOND LEG IS PRICE/RETURN, NEVER YIELD (Constraint 2): the ``^TNX`` yield is converted to
a bond-price return via a first-order modified-duration approximation; raw yield is never
stored or fed as a price. IEF is already a price/return series.

RUN (needs network egress; the sandbox proxy rate-limits Yahoo, so run un-sandboxed):
    python -m tools.eval.phase19_monthend_eszn.fetch_data
Re-runnable / idempotent: it overwrites the three CSVs. It FAILS LOUDLY (non-zero exit)
if any source is unreachable — it never writes a partial or empty CSV.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pandas as pd

OUT_DIR = "/home/tradeflow/tradeflow/research/data/phase19"

# Yahoo v8 chart endpoint. period1=0 (1970 epoch) .. a far-future period2 == full history.
_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1=0&period2={p2}&interval=1d&events=div%2Csplit"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"

# Modified duration for the yield->price conversion. A 10Y constant-maturity note / ZN CTD
# sits in the ~6.5-8 range (Constraint 2); 7.0 is a defensible mid-point. NOTE: duration is
# applied as a CONSTANT multiplier, so its exact value scales the bond-return MAGNITUDE only
# — it cannot change the SIGN or TIMING of the month-end signal. Convexity / carry are
# ignored (first-order proxy, fine for small daily dy over a 5-8 day window). Source for the
# range: CME "Understanding Treasury Futures" / standard 10Y note duration ~ 7-8.
MODIFIED_DURATION = 7.0


def _log(series: str, action: str, reason: str) -> None:
    print(f"[DATA] {series}: {action} — {reason}")


def _fetch_url(url: str, *, retries: int = 4) -> bytes:
    """GET ``url`` with a browser UA + exponential backoff. Raise loudly on failure."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:  # noqa: PERF203
            last = exc
            wait = 2.0 * attempt
            _log(
                "http", f"retry {attempt}/{retries} in {wait:.0f}s", f"{type(exc).__name__}: {exc}"
            )
            time.sleep(wait)
    raise RuntimeError(f"source unreachable after {retries} tries: {url} (last: {last})")


@dataclass
class Quote:
    dates: list[dt.date]
    open: list[float | None]
    high: list[float | None]
    low: list[float | None]
    close: list[float | None]
    volume: list[float | None]
    adjclose: list[float | None]


def fetch_yahoo(symbol: str) -> Quote:
    """Fetch the full daily history for ``symbol`` from the Yahoo v8 chart endpoint."""
    p2 = int((dt.datetime.now(dt.UTC) + dt.timedelta(days=2)).timestamp())
    url = _YAHOO.format(sym=urllib.parse.quote(symbol), p2=p2)
    _log(symbol, "fetch", url)
    payload = json.loads(_fetch_url(url))
    result = payload.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"no chart result for {symbol}: {payload.get('chart', {}).get('error')}")
    r = result[0]
    ts: list[int] = r["timestamp"]
    q = r["indicators"]["quote"][0]
    adj_block = r["indicators"].get("adjclose")
    adj = adj_block[0]["adjclose"] if adj_block else [None] * len(ts)
    dates = [dt.datetime.fromtimestamp(t, dt.UTC).date() for t in ts]
    _log(symbol, "fetched", f"{len(ts)} raw rows {dates[0]}..{dates[-1]}")
    return Quote(dates, q["open"], q["high"], q["low"], q["close"], q["volume"], adj)


def _clean_rows(rows: list[dict]) -> pd.DataFrame:
    """Sort by date, drop null-close, dedupe (keep last). One UTC-midnight ``time`` col
    matching the phase-17 loader schema (time, open, high, low, close, volume)."""
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["close"])
    df["time"] = pd.to_datetime(df["date"]).dt.tz_localize("UTC").dt.normalize()
    df = df.drop(columns=["date"])
    df = df.sort_values("time").drop_duplicates(subset="time", keep="last").reset_index(drop=True)
    return df[["time", "open", "high", "low", "close", "volume"]]


def build_spx(q: Quote) -> pd.DataFrame:
    """S&P 500 cash index — real OHLCV, the ES price-return proxy (Constraint 3)."""
    rows = [
        {"date": d, "open": o, "high": h, "low": low, "close": c, "volume": v or 0.0}
        for d, o, h, low, c, v in zip(
            q.dates, q.open, q.high, q.low, q.close, q.volume, strict=True
        )
    ]
    df = _clean_rows(rows)
    _log("SPX", "built", f"{len(df)} rows, equity PRICE index (not total-return)")
    return df


def build_ief(q: Quote) -> pd.DataFrame:
    """IEF dividend/split-ADJUSTED close — total-return ZN proxy. Single meaningful value
    (the adjusted close), so OHLC = adjclose per Constraint 1; volume carried real."""
    rows = [
        {"date": d, "open": a, "high": a, "low": a, "close": a, "volume": v or 0.0}
        for d, a, v in zip(q.dates, q.adjclose, q.volume, strict=True)
    ]
    df = _clean_rows(rows)
    _log("IEF", "built", f"{len(df)} rows, dividend/split-adjusted close (total-return)")
    return df


def yield_to_price_index(q: Quote, *, base: float = 100.0) -> pd.DataFrame:
    """Convert a 10Y constant-maturity YIELD series (percent) to a bond PRICE-return index.

    price_return_t  ~=  -D_mod * dy_decimal        (first-order modified-duration approx)
    P_t             =   P_{t-1} * (1 + price_return_t),   P_0 = base

    ``q.close`` is the yield in PERCENT (e.g. 4.55). dy in percentage points is divided by
    100 to a decimal. The resulting index RISES when yield FALLS (a bond rally) — the
    correct futures-long direction (Constraint 2). Raw yield is never stored as a price.
    """
    pairs = [(d, c) for d, c in zip(q.dates, q.close, strict=True) if c is not None]
    pairs.sort(key=lambda x: x[0])
    rows: list[dict] = []
    prev_y: float | None = None
    p = base
    for d, y in pairs:
        if prev_y is not None:
            dy_dec = (y - prev_y) / 100.0
            p *= 1.0 + (-MODIFIED_DURATION * dy_dec)
        rows.append({"date": d, "open": p, "high": p, "low": p, "close": p, "volume": 0.0})
        prev_y = y
    df = _clean_rows(rows)
    _log(
        "UST10Y",
        "built",
        f"{len(df)} rows, bond PRICE-return index (D_mod={MODIFIED_DURATION}); up as yield falls",
    )
    return df


# --------------------------------------------------------------------------- #
# Data gate (Phase-17 Stage-1 style) — validate + report; raise on hard failure
# --------------------------------------------------------------------------- #
N_MONTHS_MIN = 200


def _months(df: pd.DataFrame) -> int:
    p = df["time"].dt.tz_localize(None).dt.to_period("M")
    return int(p.nunique())


def _max_gap_days(df: pd.DataFrame) -> int:
    return int(df["time"].diff().dt.days.max())


def gate_series(name: str, df: pd.DataFrame, *, is_yield_priced: bool) -> dict:
    """Continuity + units sanity for one series. Raises on a structural failure."""
    if df.empty:
        raise RuntimeError(f"{name}: empty series — refusing to write")
    if not df["time"].is_monotonic_increasing:
        raise RuntimeError(f"{name}: dates not monotonic")
    if df["time"].duplicated().any():
        raise RuntimeError(f"{name}: duplicate dates")
    last_level = float(df["close"].iloc[-1])
    # A bond PRICE/return level is an index near `base` (~100s), NEVER a ~4-5 yield.
    if is_yield_priced and last_level < 20.0:
        raise RuntimeError(
            f"{name}: close looks like a YIELD ({last_level}), not a price — Constraint-2 violation"
        )
    span = (df["time"].iloc[0].date(), df["time"].iloc[-1].date())
    info = {
        "name": name,
        "rows": len(df),
        "start": str(span[0]),
        "end": str(span[1]),
        "months": _months(df),
        "max_gap_days": _max_gap_days(df),
        "last_close": round(last_level, 4),
    }
    _log(
        name,
        "GATE",
        f"{info['rows']} rows {info['start']}..{info['end']} | {info['months']} months "
        f"| max_gap {info['max_gap_days']}d | last_close {info['last_close']}",
    )
    return info


def overlap_months(a: pd.DataFrame, b: pd.DataFrame) -> int:
    lo = max(a["time"].min(), b["time"].min()).tz_localize(None)
    hi = min(a["time"].max(), b["time"].max()).tz_localize(None)
    months = pd.period_range(lo.to_period("M"), hi.to_period("M"), freq="M")
    return len(months)


def return_corr(a: pd.DataFrame, b: pd.DataFrame) -> float:
    """Daily close-to-close return correlation over the common dates (sanity: two bond
    proxies of the same exposure should be strongly positive)."""
    ra = a.set_index("time")["close"].pct_change()
    rb = b.set_index("time")["close"].pct_change()
    j = pd.concat([ra, rb], axis=1, join="inner").dropna()
    return float(j.iloc[:, 0].corr(j.iloc[:, 1]))


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)

    spx = build_spx(fetch_yahoo("^GSPC"))
    ief = build_ief(fetch_yahoo("IEF"))
    ust = yield_to_price_index(fetch_yahoo("^TNX"))

    gates = [
        gate_series("SPX", spx, is_yield_priced=False),
        gate_series("IEF", ief, is_yield_priced=True),
        gate_series("UST10Y", ust, is_yield_priced=True),
    ]

    ov_ief = overlap_months(spx, ief)
    ov_ust = overlap_months(spx, ust)
    corr = return_corr(ief, ust)
    _log("overlap", "SPX∩IEF", f"{ov_ief} months (need >= {N_MONTHS_MIN})")
    _log("overlap", "SPX∩UST10Y", f"{ov_ust} months (need >= {N_MONTHS_MIN})")
    _log("bond-corr", "IEF vs UST10Y daily returns", f"{corr:.3f} (same exposure → should be +)")

    if ov_ief < N_MONTHS_MIN and ov_ust < N_MONTHS_MIN:
        raise RuntimeError(
            f"NO overlap reaches {N_MONTHS_MIN} mo (IEF {ov_ief}, UST10Y {ov_ust}) — gate FAIL"
        )
    if corr < 0.3:
        raise RuntimeError(f"IEF/UST10Y return corr {corr:.3f} too low — bond proxies disagree")

    spx.to_csv(f"{OUT_DIR}/SPX_daily.csv", index=False)
    ief.to_csv(f"{OUT_DIR}/IEF_daily.csv", index=False)
    ust.to_csv(f"{OUT_DIR}/UST10Y_pricereturn_daily.csv", index=False)
    _log("write", "wrote 3 CSVs", OUT_DIR)

    print("\n=== DATA-GATE SUMMARY ===")
    for g in gates:
        print(
            f"  {g['name']:<7} {g['rows']:>6} rows  {g['start']}..{g['end']}  {g['months']:>4} mo"
        )
    print(f"  overlap SPX∩IEF    = {ov_ief} months {'PASS' if ov_ief >= N_MONTHS_MIN else 'FAIL'}")
    print(f"  overlap SPX∩UST10Y = {ov_ust} months {'PASS' if ov_ust >= N_MONTHS_MIN else 'FAIL'}")
    print(f"  IEF vs UST10Y daily-return corr = {corr:.3f}")
    print("  MODEL NOT RUN — data sourcing only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
