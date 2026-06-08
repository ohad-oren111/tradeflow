"""Data layer for the eval kit: load saved 1-min history (roll-handled) and build
synthetic bar sequences for the Phase 2 scenarios.

Real history: ``research/data/nq_1min_raw.csv`` (UTC-stamped, stitched front-month
NQ across quarterly contracts, 2024-03 → 2026-06). The strategy is point-based and
scale-invariant, so NQ bars exercise the identical MA100/MA50/regime logic the live
MNQ bot runs; only the dollar multiplier differs (applied via config.instruments.MNQ).

Roll handling: contract rolls inject a one-off price discontinuity at the boundary.
``roll_adjust`` detects the boundary gaps and back-adjusts the earlier contract's
prices (difference method) so the MA / regime series is continuous, mirroring how
the live bot — which always trades ONE front month and reseeds its buffer from
contiguous history — never sees a roll gap inside its rolling window. The number of
boundaries adjusted and the total offset are reported so the treatment is visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from src.execution.trail_manager import round_to_tick as _tick
from src.indicators import add_all_indicators

_ET = ZoneInfo("America/New_York")
DEFAULT_HISTORY = "/home/tradeflow/tradeflow/research/data/nq_1min_raw.csv"


# --------------------------------------------------------------------------- #
# Real history
# --------------------------------------------------------------------------- #
def load_history(path: str = DEFAULT_HISTORY) -> pd.DataFrame:
    """Load saved 1-min OHLCV into a frame with a UTC tz-aware ``time`` column.

    Accepts either the raw file (``ts`` column, UTC-explicit) or the ET-naive
    ``timestamp`` file (localized to America/New_York → UTC). Sorted, de-duped.
    """
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    if "ts" in cols:
        df = df.rename(columns={cols["ts"]: "time"})
        df["time"] = pd.to_datetime(df["time"], utc=True)
    elif "timestamp" in cols:
        df = df.rename(columns={cols["timestamp"]: "time"})
        t = pd.to_datetime(df["time"])
        if t.dt.tz is None:
            t = t.dt.tz_localize(_ET).dt.tz_convert("UTC")
        else:
            t = t.dt.tz_convert("UTC")
        df["time"] = t
    else:
        raise ValueError(f"no ts/timestamp column in {path}: {list(df.columns)}")
    df = df[["time", "open", "high", "low", "close", "volume"]].copy()
    df = df.dropna(subset=["open", "high", "low", "close"]).sort_values("time")
    df = df.drop_duplicates(subset="time", keep="last").reset_index(drop=True)
    return df


@dataclass
class RollInfo:
    boundaries: list  # list of (time, gap_pts) that were treated as rolls
    total_offset_pts: float


def _third_friday(year: int, month: int) -> datetime:
    d = datetime(year, month, 1, tzinfo=UTC)
    # weekday(): Mon=0..Sun=6; Friday=4. First Friday, then +2 weeks.
    first_friday = 1 + (4 - d.weekday()) % 7
    return datetime(year, month, first_friday + 14, tzinfo=UTC)


def _quarterly_roll_windows(
    start: datetime, end: datetime, *, roll_days_before: int = 8, pad_days: int = 6
) -> list[tuple[datetime, datetime]]:
    """Windows around each quarterly (Mar/Jun/Sep/Dec) roll: the front month rolls
    ~``roll_days_before`` days before the 3rd-Friday expiry; the window pads
    ``pad_days`` on each side to catch the actual roll session boundary."""
    windows = []
    for year in range(start.year, end.year + 1):
        for month in (3, 6, 9, 12):
            roll_day = _third_friday(year, month) - timedelta(days=roll_days_before)
            w0, w1 = roll_day - timedelta(days=pad_days), roll_day + timedelta(days=pad_days)
            if w1 >= start and w0 <= end:
                windows.append((w0, w1))
    return windows


def roll_adjust(df: pd.DataFrame, *, min_gap_minutes: int = 90) -> tuple[pd.DataFrame, RollInfo]:
    """Back-adjust QUARTERLY contract-roll discontinuities into a continuous series.

    Roll detection is restricted to the quarterly roll windows (Mar/Jun/Sep/Dec,
    ~8 days before the 3rd-Friday expiry) — a pure gap-size heuristic CANNOT
    separate a real roll from an ordinary weekend price move (both can be 50-400pt
    on this series: the largest session-boundary gaps here are Sunday-open weekend
    moves, NOT rolls). Within each roll window the single largest session-boundary
    gap (time delta >= ``min_gap_minutes``) is treated as the roll and back-adjusted
    (cumulative offset added to all EARLIER bars so the latest contract stays real —
    the difference / Panama method).

    CAVEAT (reported, not hidden): an overnight roll gap still mixes the calendar
    spread with the real overnight price move; without overlapping per-contract
    data the pure spread can't be isolated, so this slightly over-adjusts. Rolls
    touch <0.3% of bars, so the backtest is run BOTH ways and the delta reported to
    show immateriality — see backtest.py.
    """
    if df.empty:
        return df, RollInfo([], 0.0)
    out = df.copy().reset_index(drop=True)
    o = out["open"].to_numpy()
    c = out["close"].to_numpy()
    times = out["time"].tolist()
    windows = _quarterly_roll_windows(times[0], times[-1])
    # largest session-boundary gap within each quarterly window = the roll.
    roll_idx: list[tuple[int, float, datetime]] = []
    for w0, w1 in windows:
        best = None
        for i in range(1, len(out)):
            ti = times[i]
            if ti < w0 or ti > w1:
                continue
            if (ti - times[i - 1]).total_seconds() / 60.0 < min_gap_minutes:
                continue
            gap = float(o[i] - c[i - 1])
            if best is None or abs(gap) > abs(best[1]):
                best = (i, gap, ti)
        if best is not None:
            roll_idx.append(best)
    adj = out[["open", "high", "low", "close"]].to_numpy(copy=True)
    total = 0.0
    boundaries = []
    for i, gap, ts in sorted(roll_idx, key=lambda x: x[0], reverse=True):
        adj[:i, :] += gap
        total += gap
        boundaries.append((ts, gap))
    out[["open", "high", "low", "close"]] = adj
    return out, RollInfo(sorted(boundaries, key=lambda x: x[0]), total)


def to_segments(df: pd.DataFrame) -> list[pd.DataFrame]:
    """Whole history as ONE continuous segment with indicators (the faithful model:
    the live bot reseeds its rolling buffer from contiguous history after any gap,
    so a single continuous roll over the roll-adjusted series mirrors production —
    see engine.py). Returns a one-element list for symmetry with multi-segment use.
    """
    seg = add_all_indicators(df).reset_index(drop=True)
    return [seg]


# --------------------------------------------------------------------------- #
# Synthetic bar builders (Phase 2)
# --------------------------------------------------------------------------- #
def _session_start(weekday_anchor: datetime | None = None) -> datetime:
    """A mid-session, no-edge UTC start: Tuesday 14:00 UTC (10:00 ET EDT)."""
    return weekday_anchor or datetime(2025, 4, 1, 14, 0, tzinfo=UTC)


def bars_from_closes(
    closes: list[float],
    *,
    start_ts: datetime,
    spread: float = 0.5,
    green: bool = False,
    minute_step: int = 1,
) -> list[dict]:
    """Derive OHLC bars from a close path. Each bar's open = prior close; high/low
    bracket open & close by ``spread``. ``green`` forces close>=open on every bar."""
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        if green and c < o:
            o = c  # keep it green/flat
        hi = max(o, c) + spread
        lo = min(o, c) - spread
        bars.append(
            {
                "time": start_ts + timedelta(minutes=i * minute_step),
                "open": _tick(o),
                "high": _tick(hi),
                "low": _tick(lo),
                "close": _tick(c),
                "volume": 10.0,
            }
        )
        prev = c
    return bars


def make_entry_setup(
    base_price: float = 20000.0,
    *,
    n_warmup: int = 120,
    n_decline: int = 70,
    decline_per_bar: float = 0.1,
    start_ts: datetime | None = None,
) -> list[dict]:
    """A proven priming prefix that fires ONE real long_signal on its final bar.

    Recipe (validated against the real Sma100BounceStrategy): ``n_warmup`` flat bars
    to warm SMA100, ``n_decline`` gently-declining RED bars to set MA100>MA50 + a
    gap, then a single GREEN bar whose low sits in the touch band [MA100-15, MA100+5].
    The final bar's close is the entry price.
    """
    start = start_ts or _session_start()
    bars: list[dict] = []
    px = _tick(base_price)
    # warmup: flat
    for i in range(n_warmup):
        bars.append(
            {
                "time": start + timedelta(minutes=i),
                "open": px,
                "high": _tick(px + 0.25),
                "low": _tick(px - 0.25),
                "close": px,
                "volume": 10.0,
            }
        )
    # decline: GUARANTEED-red bars (open a fixed 2 ticks above close) so tick
    # rounding can't collapse them into dojis that fire mid-decline entries — the
    # prefix must fire exactly ONE signal, on the final green bar.
    idx = n_warmup
    for _ in range(n_decline):
        px -= decline_per_bar
        c = _tick(px)
        o = _tick(c + 0.5)
        bars.append(
            {
                "time": start + timedelta(minutes=idx),
                "open": o,
                "high": _tick(o + 0.05),
                "low": _tick(c - 0.15),
                "close": c,
                "volume": 10.0,
            }
        )
        idx += 1
    # entry bar: green, low dips into band, closes up
    bars.append(
        {
            "time": start + timedelta(minutes=idx),
            "open": _tick(px - 0.05),
            "high": _tick(px + 0.3),
            "low": _tick(px - 0.3),
            "close": _tick(px + 0.25),
            "volume": 10.0,
        }
    )
    return bars


def continuation(
    last_close: float,
    deltas: list[float],
    *,
    start_ts: datetime,
    spread: float = 1.0,
) -> list[dict]:
    """Append bars whose close walks by ``deltas`` (pts/bar). Negative deltas fall.

    high/low bracket each bar by ``spread`` around its open/close so an intrabar
    stop can trigger on the low. Used to drive run-ups, reversals, chop, etc.
    """
    closes = []
    p = last_close
    for d in deltas:
        p = p + d
        closes.append(round(p, 2))
    return bars_from_closes(closes, start_ts=start_ts, spread=spread)


def frame(bars: list[dict]) -> pd.DataFrame:
    """Bars → indicator-bearing segment frame (for FastGateEntry / the engine)."""
    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return add_all_indicators(df).reset_index(drop=True)
