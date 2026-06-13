"""Phase-21 S05 DATA SOURCING — Binance COIN-margined delivery (quarterly) futures.

Downloads daily (1d) klines for every expired+listed BTCUSD_* / ETHUSD_* delivery
contract from data.binance.vision (the geo-reachable bulk host; binance.com API is
451-blocked from this VPS), via MONTHLY kline zips:

    data/futures/cm/monthly/klines/<SYM>/1d/<SYM>-1d-YYYY-MM.zip

Raw zips land in ``research/cache/phase21_s05/`` (NEVER committed). The committed
artifact is one lean table ``research/data/phase21/delivery_futures_daily.csv``:
    symbol, coin, expiry, date, close
(~10k rows). Spot legs reuse the on-disk phase20 dailies (PR #155).

Timestamp gotcha (§0.5.97, bitten in Phase-20a): kline open_time may be ms OR µs
depending on vintage — values > 1e15 are treated as µs.

Validation (fail loudly):
  * >= 18 expired contracts per coin (Stage-0 listed 25 BTC symbols, 2020->2026).
  * CONVERGENCE: for >= 90% of expired contracts the final traded close must be
    within 1.5% of that day's spot close (basis ~0 at expiry — if this fails the
    data, not the market, is wrong).
  * No duplicate (symbol, date) rows; closes positive.

Run:  .venv/bin/python -m tools.eval.phase21_gauntlet.S05_cash_and_carry.fetch_data
"""

from __future__ import annotations

import datetime as dt
import io
import os
import re
import sys
import time
import urllib.request
import zipfile

import pandas as pd

CACHE = "/home/tradeflow/tradeflow/research/cache/phase21_s05"
OUT_CSV = "/home/tradeflow/tradeflow/research/data/phase21/delivery_futures_daily.csv"
SPOT = {
    "BTC": "/home/tradeflow/tradeflow/research/data/phase20/BTC_spot.csv",
    "ETH": "/home/tradeflow/tradeflow/research/data/phase20/ETH_spot.csv",
}

_S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?{q}"
_DL = "https://data.binance.vision/{key}"
_SYM_RE = re.compile(r"^(BTC|ETH)USD_(\d{6})$")
TODAY = dt.date(2026, 6, 13)


def _get(url: str, *, retries: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TradeFlow research"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"unreachable: {url} ({last})")


def list_symbols() -> list[str]:
    xml = _get(_S3.format(q="delimiter=/&prefix=data/futures/cm/daily/klines/")).decode()
    prefixes = re.findall(r"<Prefix>data/futures/cm/daily/klines/([^<]+)/</Prefix>", xml)
    syms = sorted(p for p in prefixes if _SYM_RE.match(p))
    if len(syms) < 30:
        raise RuntimeError(f"only {len(syms)} delivery symbols listed — listing broken?")
    return syms


def list_monthly_keys(sym: str) -> list[str]:
    xml = _get(_S3.format(q=f"prefix=data/futures/cm/monthly/klines/{sym}/1d/")).decode()
    keys = re.findall(r"<Key>([^<]+\.zip)</Key>", xml)
    return [k for k in keys if not k.endswith(".CHECKSUM.zip")]


def read_kline_zip(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = zf.namelist()[0]
        df = pd.read_csv(zf.open(name), header=None)
    # header row variant: some vintages include a header line
    if isinstance(df.iloc[0, 0], str) and not str(df.iloc[0, 0]).isdigit():
        df = df.iloc[1:].reset_index(drop=True)
    ts = pd.to_numeric(df[0])
    unit = "us" if float(ts.iloc[0]) > 1e15 else "ms"
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(ts, unit=unit, utc=True).dt.normalize(),
            "close": pd.to_numeric(df[4]),
        }
    )
    return out


def main() -> int:
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    syms = list_symbols()
    print(f"[DATA] {len(syms)} delivery symbols (BTC+ETH quarterlies)")

    all_rows = []
    for sym in syms:
        m = _SYM_RE.match(sym)
        coin = m.group(1)
        yymmdd = m.group(2)
        expiry = dt.date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
        keys = list_monthly_keys(sym)
        frames = []
        for key in keys:
            cache_path = os.path.join(CACHE, os.path.basename(key))
            if os.path.exists(cache_path):
                raw = open(cache_path, "rb").read()
            else:
                raw = _get(_DL.format(key=key))
                with open(cache_path, "wb") as fh:
                    fh.write(raw)
                time.sleep(0.15)
            frames.append(read_kline_zip(raw))
        if not frames:
            print(f"[DATA] {sym}: NO monthly files — skipped")
            continue
        df = pd.concat(frames, ignore_index=True).sort_values("date")
        df = df.drop_duplicates(subset="date", keep="last")
        # Two data artifacts of Binance CM delivery klines, both fixed by keeping
        # only rows STRICTLY BEFORE the settlement day:
        #  (1) Post-expiry tail: Binance keeps emitting daily rows for an expired
        #      symbol with volume=0 and the close frozen at the settlement price
        #      (verified: BTCUSD_200925 → 10687.6 @ vol 0 for months past its
        #      2020-09-25 expiry).
        #  (2) Settlement-day partial bar: COIN-M quarterlies settle ~08:00 UTC on
        #      the expiry day, so that day's daily bar closes at the 08:00
        #      settlement while the SPOT daily bar closes at 24:00 UTC — up to 16h
        #      apart, injecting a spurious basis (verified: BTCUSD_210625 expiry
        #      bar 622k vol shows +8.55% vs a true ±0.3% on every full bar to
        #      expiry-eve). The last FULL trading bar (expiry-eve) is time-aligned
        #      with spot and converges to <0.6% across all 46 expired contracts.
        exp_ts = pd.Timestamp(expiry, tz="UTC")
        df = df[df["date"] < exp_ts].reset_index(drop=True)
        if df.empty:
            print(f"[DATA] {sym}: no rows before expiry {expiry} — skipped")
            continue
        df["symbol"], df["coin"], df["expiry"] = sym, coin, exp_ts
        all_rows.append(df)
        print(
            f"[DATA] {sym}: {len(df)} days {df['date'].iloc[0].date()}..{df['date'].iloc[-1].date()}"
        )

    full = pd.concat(all_rows, ignore_index=True)
    if (full["close"] <= 0).any():
        raise RuntimeError("non-positive closes present")
    if full.duplicated(subset=["symbol", "date"]).any():
        raise RuntimeError("duplicate (symbol, date) rows")

    # convergence check on expired contracts
    spot = {}
    for coin, path in SPOT.items():
        s = pd.read_csv(path)
        s["time"] = pd.to_datetime(s["time"], utc=True).dt.normalize()
        spot[coin] = s.set_index("time")["close"].astype(float)
    expired, ok = 0, 0
    per_coin: dict[str, int] = {}
    for sym, g in full.groupby("symbol"):
        exp = g["expiry"].iloc[0]
        if exp.date() > TODAY:
            continue
        expired += 1
        coin = g["coin"].iloc[0]
        per_coin[coin] = per_coin.get(coin, 0) + 1
        last_row = g.sort_values("date").iloc[-1]
        sp = spot[coin].get(last_row["date"])
        if sp is not None and abs(float(last_row["close"]) / float(sp) - 1.0) < 0.015:
            ok += 1
    for coin in ("BTC", "ETH"):
        if per_coin.get(coin, 0) < 18:
            raise RuntimeError(f"{coin}: only {per_coin.get(coin, 0)} expired contracts (<18)")
    conv = ok / expired
    if conv < 0.90:
        raise RuntimeError(f"convergence only {ok}/{expired} ({conv:.0%}) — data wrong")

    out = full[["symbol", "coin", "expiry", "date", "close"]].sort_values(["symbol", "date"])
    out.to_csv(OUT_CSV, index=False)
    print(
        f"[DATA] wrote {OUT_CSV}: {len(out)} rows, {full['symbol'].nunique()} symbols "
        f"({per_coin}); convergence {ok}/{expired} ({conv:.0%})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
