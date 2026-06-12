"""Phase-20a — crypto funding-carry data sourcing (research-only, data-only).

Sources daily spot + perp OHLCV and perp funding-rate history for the majors + alt
basket, normalises to the phase-19 loader schema, and lands flat CSVs under
``research/data/phase20/``. It does NOT run the carry model (Phase-20b) and never
connects to IB / the broker / a DB — it only downloads PUBLIC crypto market-data files
(permitted; that is the entire point of this task).

Venue: **Binance** via its public historical-data dump ``data.binance.vision``.
Rationale (Constraint 1 / §0.5.97, reachability probed first):
  * Binance trading API ``fapi.binance.com`` geo-blocks this US (Hetzner/Ashburn) VPS
    (HTTP 451); Bybit ``api.bybit.com`` likewise (HTTP 403); Coinglass needs an API key
    (HTTP 401 — not invented, per the brief).
  * OKX ``www.okx.com`` IS reachable, BUT its public ``funding-rate-history`` endpoint
    only retains ~3 months (≈277 intervals) — far short of the multi-year depth a carry
    backtest needs.
  * ``data.binance.vision`` is reachable from this US VPS (it is a static data CDN, NOT
    the geo-blocked trading API) and carries Binance USDⓈ-M funding + klines back to
    listing (BTC funding from 2020-01). It is the canonical funding-carry venue, and
    keeping spot + perp + funding all on ONE venue gives a clean perp-spot basis.

Units (§0.5.97, verified against the source files):
  * Funding rate = per-interval FRACTION (Binance ``last_funding_rate``; e.g. 0.0001 =
    0.01% per interval). Interval is in the file's ``funding_interval_hours`` column
    (predominantly 8h; Binance moved some symbols to 4h in places) — encoded by the
    timestamp spacing in the landed ``time`` column; the modal interval is reported.
  * Spot (``<SYM>``) and perp (USDⓈ-M ``<SYM>``) both quoted in USDT.
  * Candle ``volume`` column = quote (USDT) notional traded (Binance kline
    ``quote_volume``, col 7), a cross-series-comparable choice.

Re-runnable + idempotent: overwrites the CSVs atomically (tmp → rename) and FAILS LOUDLY
(non-zero exit) on an unreachable source or an empty series — never lands a silent
partial CSV. Binance Vision publishes MONTHLY archives, so each series ends at the last
complete published month (the current partial month is intentionally not included).

    python -m tools.eval.phase20_funding_carry.fetch_data
"""

from __future__ import annotations

import io
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

OUT_DIR = "/home/tradeflow/tradeflow/research/data/phase20"
BASE = "https://data.binance.vision/data"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
_START_Y, _START_M = 2019, 9  # Binance USDⓈ-M futures launched 2019-09; scan from here
_SLEEP = 0.03  # light politeness on the static CDN

# Coin universe (Constraint 5 — operator-set: majors + alt basket). Binance USDT pairs.
MAJORS = ["BTC", "ETH"]
ALTS = ["SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK"]
COINS = MAJORS + ALTS
MIN_FUNDING_ROWS = 200  # gate: drop a coin with too little funding history to be useful


def _log(series: str, action: str, reason: str) -> None:
    print(f"[DATA] {series}: {action} — {reason}")


def _fetch_zip_csv(url: str, *, retries: int = 4) -> list[str] | None:
    """Download a Binance Vision monthly .zip, return its CSV lines.

    Returns ``None`` on a clean 404 (that month simply isn't published — skip it).
    Raises loudly on any other/persistent network failure (never a silent partial).
    """
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            z = zipfile.ZipFile(io.BytesIO(raw))
            return z.read(z.namelist()[0]).decode().splitlines()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
        except (urllib.error.URLError, TimeoutError, zipfile.BadZipFile) as exc:  # noqa: PERF203
            last = exc
        wait = 2.0 * attempt
        _log("http", f"retry {attempt}/{retries} in {wait:.0f}s", f"{type(last).__name__}: {last}")
        time.sleep(wait)
    raise RuntimeError(f"source unreachable after {retries} tries: {url} (last: {last})")


def _months() -> list[tuple[int, int]]:
    """All (year, month) from the scan start through the current month (inclusive)."""
    now = datetime.now(UTC)
    out: list[tuple[int, int]] = []
    y, m = _START_Y, _START_M
    while (y, m) <= (now.year, now.month):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _is_data_row(tokens: list[str]) -> bool:
    """Skip header lines (Binance Vision adds a header to some monthly files, not all)."""
    return bool(tokens) and tokens[0].strip().lstrip("-").isdigit()


def _to_ms(v: int) -> int:
    """Normalise an epoch timestamp to milliseconds. Binance Vision switched kline
    timestamps from ms (13-digit, ~1.7e12) to MICROSECONDS (16-digit, ~1.7e15) in 2025;
    a µs value read as ms lands in the year ~56000. Anything >= 1e14 is µs → //1000."""
    return v // 1000 if v >= 10**14 else v


# --------------------------------------------------------------------------- #
# Funding
# --------------------------------------------------------------------------- #
def fetch_funding(sym: str) -> tuple[pd.DataFrame, Counter]:
    """Full perp funding history for ``<SYM>`` → (DataFrame[time, funding_rate], intervals)."""
    rows: list[dict] = []
    intervals: Counter = Counter()
    for y, m in _months():
        url = f"{BASE}/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{y:04d}-{m:02d}.zip"
        lines = _fetch_zip_csv(url)
        if lines is None:
            continue
        for ln in lines:
            tok = ln.split(",")
            if not _is_data_row(tok):
                continue
            rows.append({"time": _to_ms(int(tok[0])), "funding_rate": float(tok[2])})
            if len(tok) > 1 and tok[1].strip().isdigit():
                intervals[int(tok[1])] += 1
        time.sleep(_SLEEP)
    if not rows:
        raise RuntimeError(f"no funding history for {sym}")
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="time", keep="last").sort_values("time").reset_index(drop=True)
    _log(
        f"{sym}/funding",
        "fetched",
        f"{len(df)} intervals {df['time'].iloc[0]}..{df['time'].iloc[-1]}",
    )
    return df[["time", "funding_rate"]], intervals


# --------------------------------------------------------------------------- #
# Candles (spot + perp)
# --------------------------------------------------------------------------- #
def fetch_klines(sym: str, *, spot: bool) -> pd.DataFrame:
    """Full daily OHLCV → DataFrame[time, open, high, low, close, volume].

    ``volume`` = quote (USDT) notional (Binance kline col 7, ``quote_volume``).
    """
    seg = "spot" if spot else "futures/um"
    label = f"{sym}/{'spot' if spot else 'perp'}"
    rows: list[dict] = []
    for y, m in _months():
        url = f"{BASE}/{seg}/monthly/klines/{sym}/1d/{sym}-1d-{y:04d}-{m:02d}.zip"
        lines = _fetch_zip_csv(url)
        if lines is None:
            continue
        for ln in lines:
            tok = ln.split(",")
            if not _is_data_row(tok):
                continue
            rows.append(
                {
                    "time": _to_ms(int(tok[0])),
                    "open": float(tok[1]),
                    "high": float(tok[2]),
                    "low": float(tok[3]),
                    "close": float(tok[4]),
                    "volume": float(tok[7]),  # quote_volume = USDT notional
                }
            )
        time.sleep(_SLEEP)
    if not rows:
        raise RuntimeError(f"no {label} klines")
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="time", keep="last").sort_values("time").reset_index(drop=True)
    _log(label, "fetched", f"{len(df)} daily bars {df['time'].iloc[0]}..{df['time'].iloc[-1]}")
    return df[["time", "open", "high", "low", "close", "volume"]]


# --------------------------------------------------------------------------- #
# Validation + write
# --------------------------------------------------------------------------- #
@dataclass
class CoinResult:
    coin: str
    is_major: bool
    spot: pd.DataFrame
    perp: pd.DataFrame
    funding: pd.DataFrame
    modal_interval_h: int = 8
    basis_mean_pct: float = 0.0
    basis_absmean_pct: float = 0.0
    notes: list[str] = field(default_factory=list)


def _assert_clean(df: pd.DataFrame, name: str) -> None:
    """Monotonic increasing, de-duplicated, no nulls."""
    if df.empty:
        raise RuntimeError(f"{name}: empty frame")
    if not df["time"].is_monotonic_increasing:
        raise RuntimeError(f"{name}: time not monotonic increasing")
    if df["time"].duplicated().any():
        raise RuntimeError(f"{name}: duplicate timestamps")
    if df.isnull().any().any():
        raise RuntimeError(f"{name}: null values present")


def _assert_funding_units(df: pd.DataFrame, coin: str) -> None:
    """Funding must be a small per-interval FRACTION (not % or bps)."""
    absrate = df["funding_rate"].abs()
    if absrate.max() >= 0.5:
        raise RuntimeError(f"{coin}: funding_rate max {absrate.max()} — not a fraction (units?)")
    if float(absrate.median()) >= 0.01:
        raise RuntimeError(f"{coin}: funding_rate median {absrate.median()} too large — units?")


def _basis(spot: pd.DataFrame, perp: pd.DataFrame) -> tuple[float, float]:
    """(mean signed, mean abs) of (perp-spot)/spot in % over the common daily index."""
    j = spot[["time", "close"]].merge(
        perp[["time", "close"]], on="time", suffixes=("_spot", "_perp")
    )
    rel = (j["close_perp"] - j["close_spot"]) / j["close_spot"] * 100.0
    return float(rel.mean()), float(rel.abs().mean())


def _atomic_write(df: pd.DataFrame, path: str) -> None:
    tmp = f"{path}.tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def process_coin(coin: str, *, is_major: bool) -> CoinResult | None:
    """Fetch + validate + write one coin. Return None (dropped) if funding too short."""
    sym = f"{coin}USDT"
    funding, intervals = fetch_funding(sym)
    if len(funding) < MIN_FUNDING_ROWS:
        _log(coin, "DROPPED", f"{len(funding)} funding intervals < {MIN_FUNDING_ROWS} min")
        return None
    spot = fetch_klines(sym, spot=True)
    perp = fetch_klines(sym, spot=False)

    _assert_clean(funding, f"{coin}/funding")
    _assert_clean(spot, f"{coin}/spot")
    _assert_clean(perp, f"{coin}/perp")
    _assert_funding_units(funding, coin)
    b_mean, b_abs = _basis(spot, perp)
    modal = intervals.most_common(1)[0][0] if intervals else 8

    _atomic_write(spot, f"{OUT_DIR}/{coin}_spot.csv")
    _atomic_write(perp, f"{OUT_DIR}/{coin}_perp.csv")
    _atomic_write(funding, f"{OUT_DIR}/{coin}_funding.csv")
    _log(
        coin,
        "landed",
        f"spot={len(spot)} perp={len(perp)} funding={len(funding)} "
        f"modal_interval={modal}h basis_mean={b_mean:+.4f}% basis_abs={b_abs:.4f}%",
    )
    return CoinResult(coin, is_major, spot, perp, funding, modal, b_mean, b_abs)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    _log("phase20", "start", f"venue=Binance(data.binance.vision) coins={COINS}")
    landed: list[CoinResult] = []
    dropped: list[str] = []
    for coin in COINS:
        res = process_coin(coin, is_major=coin in MAJORS)
        if res is None:
            dropped.append(coin)
        else:
            landed.append(res)

    if not landed:
        raise SystemExit("[DATA] STOP: no coin produced usable data")

    print("\n[DATA] ===== GATE SUMMARY (venue=Binance USDⓈ-M via data.binance.vision) =====")
    total_funding = 0
    for r in landed:
        total_funding += len(r.funding)
        fspan = f"{r.funding['time'].iloc[0].date()}..{r.funding['time'].iloc[-1].date()}"
        sspan = f"{r.spot['time'].iloc[0].date()}..{r.spot['time'].iloc[-1].date()}"
        pspan = f"{r.perp['time'].iloc[0].date()}..{r.perp['time'].iloc[-1].date()}"
        tag = "major" if r.is_major else "alt"
        print(
            f"[DATA] {r.coin:<5} ({tag}): funding={len(r.funding)}@{r.modal_interval_h}h [{fspan}] "
            f"spot={len(r.spot)} [{sspan}] perp={len(r.perp)} [{pspan}] "
            f"basis_mean={r.basis_mean_pct:+.4f}% basis_abs={r.basis_absmean_pct:.4f}%"
        )
    print(f"[DATA] TOTAL funding intervals landed: {total_funding} across {len(landed)} coins")
    if dropped:
        print(f"[DATA] DROPPED (insufficient funding history): {dropped}")
    print("[DATA] MODEL NOT RUN — Phase-20a is data-only; the carry eval is Phase-20b.")


if __name__ == "__main__":
    sys.exit(main())
