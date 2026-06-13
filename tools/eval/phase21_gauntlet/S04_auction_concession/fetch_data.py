"""Phase-21 S04 DATA SOURCING — Treasury auction history + 5Y/30Y price-return series.

Lands three lean files (research-only, data-only):

  * ``research/data/phase21/auctions.csv`` — every NOMINAL coupon auction (Note/Bond,
    TIPS and FRNs excluded) from api.fiscaldata.treasury.gov ``auctions_query``
    (full history to 1979; Stage-0 verified 11,006 total records incl. bills).
    Columns: auction_date, security_type, security_term, tenor_group in {5Y,10Y,30Y}
    (year-part mapping pre-registered below; terms without a free index proxy are
    EXCLUDED and counted).
  * ``research/data/phase21/FVX_pricereturn_daily.csv`` — ^FVX 5Y CMT yield ->
    bond price-return index, D_mod 4.5 (5Y note duration ~4.2-4.7).
  * ``research/data/phase21/TYX_pricereturn_daily.csv`` — ^TYX 30Y CMT yield ->
    price-return index, D_mod 17.0 (30Y bond duration ~15-19).

The 10Y leg reuses ``research/data/phase19/UST10Y_pricereturn_daily.csv`` (on disk,
^TNX, D_mod 7.0). Phase-19's constant-duration argument applies to all three: D_mod
is a constant multiplier, so its exact value scales return MAGNITUDE only — it cannot
change the sign or timing of an auction-day signal. Raw yield is never stored as price.

Tenor mapping (pre-registered): year-part of security_term ->
  {4, 5} -> 5Y leg | {9, 10} -> 10Y leg | {19, 20, 29, 30} -> 30Y leg
  (reopenings carry the ORIGINAL term, e.g. "9-Year 11-Month" -> 10Y).
  2Y/3Y/7Y auctions are EXCLUDED — no free CMT index proxy on Yahoo (^IRX is 13-week).

Validation (fail loudly): >=1000 mapped nominal coupon auctions (first run found 2753
raw Note/Bond of which 1128 are 2Y/3Y/7Y -> 1208 mapped; the original >=2500 guess was
wrong and corrected); 10Y auctions present every
year 1980..2025; ^FVX/^TYX last closes in [0.3, 20] percent; TYX-index daily returns
correlate > 0.6 with the on-disk TNX-derived 10Y index (same asset class).

Run:  .venv/bin/python -m tools.eval.phase21_gauntlet.S04_auction_concession.fetch_data
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request

import pandas as pd

OUT_DIR = "/home/tradeflow/tradeflow/research/data/phase21"
UST10Y_CSV = "/home/tradeflow/tradeflow/research/data/phase19/UST10Y_pricereturn_daily.csv"

_FISCAL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
    "/v1/accounting/od/auctions_query"
    "?fields=auction_date,security_type,security_term,floating_rate,"
    "inflation_index_security"
    "&filter=security_type:in:(Note,Bond)"
    "&page[size]=10000&page[number]={page}&sort=auction_date"
)
_YAHOO = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{sym}" "?period1=0&period2={p2}&interval=1d"
)
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"

D_MOD = {"FVX": 4.5, "TYX": 17.0}
TENOR_MAP = {4: "5Y", 5: "5Y", 9: "10Y", 10: "10Y", 19: "30Y", 20: "30Y", 29: "30Y", 30: "30Y"}


def _get(url: str, *, retries: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"unreachable after {retries}: {url} ({last})")


def fetch_auctions() -> pd.DataFrame:
    rows = []
    page = 1
    while True:
        payload = json.loads(_get(_FISCAL.format(page=page)))
        data = payload.get("data", [])
        rows.extend(data)
        total_pages = int(payload["meta"]["total-pages"])
        print(f"[DATA] auctions page {page}/{total_pages}: +{len(data)} rows")
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.5)
    df = pd.DataFrame(rows)
    n_raw = len(df)
    # nominal coupons only
    df = df[(df["floating_rate"] != "Yes") & (df["inflation_index_security"] != "Yes")]

    # tenor mapping by the YEAR part of security_term ("9-Year 11-Month" -> 9)
    def _year_part(term: str) -> int | None:
        m = re.match(r"^(\d+)-Year", str(term))
        return int(m.group(1)) if m else None

    df = df.copy()
    df["year_part"] = df["security_term"].map(_year_part)
    df["tenor_group"] = df["year_part"].map(TENOR_MAP)
    n_unmapped = int(df["tenor_group"].isna().sum())
    df = df.dropna(subset=["tenor_group"])
    df["auction_date"] = pd.to_datetime(df["auction_date"])
    df = df.sort_values("auction_date").reset_index(drop=True)
    print(
        f"[DATA] auctions: {n_raw} raw Note/Bond -> {len(df)} nominal mapped "
        f"({n_unmapped} excluded: 2Y/3Y/7Y or unparsable — no free index proxy)"
    )
    return df[["auction_date", "security_type", "security_term", "tenor_group"]]


def fetch_yield_index(symbol: str, key: str) -> pd.DataFrame:
    """^FVX/^TYX yield (percent) -> price-return index, phase19 yield_to_price_index."""
    p2 = int((dt.datetime.now(dt.UTC) + dt.timedelta(days=2)).timestamp())
    payload = json.loads(_get(_YAHOO.format(sym=urllib.request.quote(symbol), p2=p2)))
    r = payload["chart"]["result"][0]
    ts = r["timestamp"]
    closes = r["indicators"]["quote"][0]["close"]
    pairs = [
        (dt.datetime.fromtimestamp(t, dt.UTC).date(), c)
        for t, c in zip(ts, closes, strict=True)
        if c is not None
    ]
    pairs.sort(key=lambda x: x[0])
    last_y = float(pairs[-1][1])
    if not 0.3 <= last_y <= 20.0:
        raise RuntimeError(f"{symbol} last yield {last_y} outside [0.3,20] — units wrong")
    dmod = D_MOD[key]
    rows, prev_y, p = [], None, 100.0
    for d, y in pairs:
        if prev_y is not None:
            p *= 1.0 + (-dmod * (y - prev_y) / 100.0)
        rows.append(
            {
                "time": pd.Timestamp(d, tz="UTC"),
                "open": p,
                "high": p,
                "low": p,
                "close": p,
                "volume": 0.0,
            }
        )
        prev_y = y
    df = pd.DataFrame(rows).drop_duplicates(subset="time", keep="last").reset_index(drop=True)
    print(
        f"[DATA] {symbol}: {len(df)} rows {df['time'].iloc[0].date()}.."
        f"{df['time'].iloc[-1].date()} D_mod {dmod} last_yield {last_y:.2f}%"
    )
    return df


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    auctions = fetch_auctions()
    fvx = fetch_yield_index("^FVX", "FVX")
    time.sleep(1.0)
    tyx = fetch_yield_index("^TYX", "TYX")

    # validations
    if len(auctions) < 1000:
        raise RuntimeError(f"only {len(auctions)} mapped auctions — expected >=1000")
    tens = auctions[auctions["tenor_group"] == "10Y"]
    years = set(tens["auction_date"].dt.year)
    missing = [y for y in range(1980, 2026) if y not in years]
    if missing:
        raise RuntimeError(f"10Y auctions missing in years {missing} — pull incomplete")
    ust = pd.read_csv(UST10Y_CSV)
    ust["time"] = pd.to_datetime(ust["time"], utc=True)
    a = tyx.set_index("time")["close"].pct_change()
    b = ust.set_index("time")["close"].pct_change()
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    corr = float(j.iloc[:, 0].corr(j.iloc[:, 1]))
    if corr < 0.6:
        raise RuntimeError(f"TYX-index vs 10Y-index return corr {corr:.3f} < 0.6")

    auctions.to_csv(f"{OUT_DIR}/auctions.csv", index=False)
    fvx.to_csv(f"{OUT_DIR}/FVX_pricereturn_daily.csv", index=False)
    tyx.to_csv(f"{OUT_DIR}/TYX_pricereturn_daily.csv", index=False)
    by = auctions.groupby("tenor_group").size().to_dict()
    print(f"[DATA] wrote 3 files | tenor counts {by} | TYX/10Y corr {corr:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
