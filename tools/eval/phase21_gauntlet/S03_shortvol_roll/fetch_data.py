"""Phase-21 S03 DATA SOURCING — VIX complex daily history (research-only, data-only).

Lands four lean daily CSVs in ``research/data/phase21/`` from the Yahoo v8 chart
endpoint (phase19 fetch pattern, keyless):

  * ``VIX_daily.csv``   — ^VIX spot index level (1990->). SIGNAL ONLY, not tradeable.
  * ``VIX3M_daily.csv`` — ^VIX3M 3-month VIX index (2006->, ex-VXV). SIGNAL ONLY.
  * ``SVXY_daily.csv``  — SVXY inverse-VIX-futures ETF, dividend/split-ADJUSTED close
                          (2011-10->). THE tradeable short-vol proxy: the ETP does the
                          futures rolling internally. NOTE (honest label): SVXY was
                          -1.0x until 2018-02-27 and -0.5x after (post-Volmageddon
                          deleverage). The series reflects the ETP AS IT TRADED;
                          results apply to that realized product, not a constant
                          -1x exposure.
  * ``VXX_daily.csv``   — VXX long-VIX ETN, adjusted close (current series 2018->;
                          the original 2009 ETN is NOT on Yahoo under this ticker).
                          Robustness cross-check only.

Validation (fail loudly):
  * units: ^VIX last close in [5, 100]; ^VIX/^VIX3M overlap return corr > 0.7.
  * SVXY must SHOW Volmageddon: close(2018-02-06) / close(2018-02-02) < 0.25
    (the ~90% collapse). If adjustment ever hides this, STOP — the whole eval
    would be fantasy.
  * spans: SVXY history must start <= 2012-01-01 and reach >= 2026-05-01.

Run:  .venv/bin/python -m tools.eval.phase21_gauntlet.S03_shortvol_roll.fetch_data
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import pandas as pd

OUT_DIR = "/home/tradeflow/tradeflow/research/data/phase21"
_YAHOO = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    "?period1=0&period2={p2}&interval=1d&events=div%2Csplit"
)
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"


def _fetch(symbol: str, *, retries: int = 4) -> dict:
    p2 = int((dt.datetime.now(dt.UTC) + dt.timedelta(days=2)).timestamp())
    url = _YAHOO.format(sym=urllib.parse.quote(symbol), p2=p2)
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            result = payload.get("chart", {}).get("result")
            if not result:
                raise RuntimeError(f"no chart result for {symbol}")
            return result[0]
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"unreachable after {retries} tries: {symbol} ({last})")


def _to_df(r: dict, *, use_adjclose: bool) -> pd.DataFrame:
    ts = r["timestamp"]
    q = r["indicators"]["quote"][0]
    adj_block = r["indicators"].get("adjclose")
    adj = adj_block[0]["adjclose"] if adj_block else [None] * len(ts)
    rows = []
    for i, t in enumerate(ts):
        c = adj[i] if use_adjclose else q["close"][i]
        if c is None:
            continue
        rows.append(
            {
                "time": pd.Timestamp(dt.datetime.fromtimestamp(t, dt.UTC).date(), tz="UTC"),
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": (q["volume"][i] or 0.0),
            }
        )
    df = pd.DataFrame(rows).sort_values("time")
    df = df.drop_duplicates(subset="time", keep="last").reset_index(drop=True)
    return df


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    specs = [
        ("^VIX", "VIX_daily.csv", False),
        ("^VIX3M", "VIX3M_daily.csv", False),
        ("SVXY", "SVXY_daily.csv", True),
        ("VXX", "VXX_daily.csv", True),
    ]
    frames: dict[str, pd.DataFrame] = {}
    for sym, fname, adj in specs:
        df = _to_df(_fetch(sym), use_adjclose=adj)
        frames[fname] = df
        span = f"{df['time'].iloc[0].date()}..{df['time'].iloc[-1].date()}"
        print(f"[DATA] {sym}: {len(df)} rows {span} last_close {df['close'].iloc[-1]:.2f}")
        time.sleep(1.0)

    vix = frames["VIX_daily.csv"]
    vix3m = frames["VIX3M_daily.csv"]
    svxy = frames["SVXY_daily.csv"]

    # units: VIX level sanity
    last_vix = float(vix["close"].iloc[-1])
    if not 5.0 <= last_vix <= 100.0:
        raise RuntimeError(f"^VIX last close {last_vix} outside [5,100] — units wrong")
    # signal coherence: VIX vs VIX3M daily-change corr on overlap
    a = vix.set_index("time")["close"].pct_change()
    b = vix3m.set_index("time")["close"].pct_change()
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    corr = float(j.iloc[:, 0].corr(j.iloc[:, 1]))
    if corr < 0.7:
        raise RuntimeError(f"VIX/VIX3M return corr {corr:.3f} < 0.7 — series disagree")
    # Volmageddon must be visible in SVXY
    s = svxy.set_index("time")["close"]
    feb02 = s.loc["2018-02-02"]
    feb06 = s.loc["2018-02-06"]
    ratio = float(feb06 / feb02)
    if ratio > 0.25:
        raise RuntimeError(f"SVXY Feb-2018 collapse NOT visible (ratio {ratio:.3f}) — STOP")
    # spans
    if svxy["time"].iloc[0] > pd.Timestamp("2012-01-01", tz="UTC"):
        raise RuntimeError("SVXY history starts too late")
    if svxy["time"].iloc[-1] < pd.Timestamp("2026-05-01", tz="UTC"):
        raise RuntimeError("SVXY history ends too early")

    for fname, df in frames.items():
        df.to_csv(f"{OUT_DIR}/{fname}", index=False)
    print(
        f"[DATA] wrote 4 CSVs to {OUT_DIR} | VIX/VIX3M corr {corr:.3f} | "
        f"SVXY Volmageddon ratio {ratio:.3f} (collapse visible — adjustment honest)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
