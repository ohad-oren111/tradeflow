"""Phase-16 dataset layer: load the saved 1-min tape, roll-adjust, tag sessions,
add the indicator columns the families need, and apply the ONE fixed calendar split.

Reuses the proven ``tools.eval.data`` loaders (``load_history`` + Panama ``roll_adjust``)
so roll handling matches every other eval phase. Everything else (session tags,
indicators) is self-contained vectorised pandas — no prod-strategy import, so a NEW
candidate family can never accidentally drive the live ``Sma100BounceStrategy``.

SPLIT-ONCE boundary (anti-overfit protocol step 1 — committed, never tuned):
  TRAIN   = 2024-03-01 .. 2025-08-31   (search here ONLY)
  HOLDOUT = 2025-09-01 .. 2026-06-30   (touched ONCE per train-survivor, ever)
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tools.eval.data import load_history, roll_adjust

_ET = ZoneInfo("America/New_York")

DEFAULT_HISTORY = "/home/tradeflow/tradeflow/research/data/nq_1min_raw.csv"

# The single, pre-committed calendar split. Strings are UTC-naive dates; comparison
# is against the tz-aware UTC ``time`` column via pandas Timestamps.
TRAIN_START = "2024-03-01"
TRAIN_END = "2025-09-01"  # exclusive
HOLDOUT_START = "2025-09-01"
HOLDOUT_END = "2026-07-01"  # exclusive (tape ends 2026-06-02)


# --------------------------------------------------------------------------- #
# Session tags + indicators
# --------------------------------------------------------------------------- #
def tag_sessions(df: pd.DataFrame) -> pd.DataFrame:
    """Add ET-clock columns: ``et``, ``rth`` (09:30-16:00 ET cash session, Mon-Fri),
    ``rth_date`` (the ET calendar date of the cash session), ``minutes_into_rth``,
    and ``year`` (ET) for the per-year gate. ``time`` must be tz-aware UTC."""
    out = df.copy()
    et = out["time"].dt.tz_convert(_ET)
    out["et"] = et
    mins = et.dt.hour * 60 + et.dt.minute
    is_weekday = et.dt.weekday < 5
    out["rth"] = is_weekday & (mins >= 9 * 60 + 30) & (mins < 16 * 60)
    out["minutes_into_rth"] = (mins - (9 * 60 + 30)).where(out["rth"], other=-1)
    out["rth_date"] = et.dt.normalize().dt.tz_localize(None)
    out["year"] = et.dt.year
    return out


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def _rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average (RMA) — used for RSI/ATR/ADX, the canonical smoothing."""
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add the causal indicator columns the families read. All are computed from
    CLOSED bars only (no centering, no forward fill) so there is no lookahead — the
    engine additionally acts on bar t's signal at t+1 open."""
    out = df.copy()
    c = out["close"]
    h = out["high"]
    low = out["low"]

    # --- True range / ATR (Wilder) ---
    prev_c = c.shift(1)
    tr = pd.concat([(h - low), (h - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
    out["tr"] = tr
    out["atr14"] = _rma(tr, 14)
    out["atr60"] = _rma(tr, 60)

    # --- RSI (Wilder, 2 and 14) ---
    delta = c.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    for n in (2, 14):
        rs = _rma(up, n) / _rma(down, n)
        out[f"rsi{n}"] = 100.0 - 100.0 / (1.0 + rs)

    # --- Bollinger (20, 2 sigma) ---
    ma20 = c.rolling(20, min_periods=20).mean()
    sd20 = c.rolling(20, min_periods=20).std(ddof=0)
    out["bb_mid"] = ma20
    out["bb_up"] = ma20 + 2.0 * sd20
    out["bb_dn"] = ma20 - 2.0 * sd20

    # --- EWMA pairs for EWMAC trend (Carver speeds) ---
    for span in (8, 16, 32, 64, 128, 256):
        out[f"ema{span}"] = _ema(c, span)

    # --- ADX(14) (Wilder) for regime/chop filters ---
    up_move = h.diff()
    dn_move = -low.diff()
    plus_dm = ((up_move > dn_move) & (up_move > 0)) * up_move.clip(lower=0.0)
    minus_dm = ((dn_move > up_move) & (dn_move > 0)) * dn_move.clip(lower=0.0)
    atr_for_di = _rma(tr, 14)
    plus_di = 100.0 * _rma(plus_dm, 14) / atr_for_di
    minus_di = 100.0 * _rma(minus_dm, 14) / atr_for_di
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    out["adx14"] = _rma(dx, 14)

    # --- NR7 / range compression: today's bar range vs the rolling range ---
    rng = h - low
    out["range"] = rng
    out["range_pct20"] = rng.rolling(20, min_periods=20).mean()

    return out


def add_session_levels(df: pd.DataFrame) -> pd.DataFrame:
    """Add session-relative levels the intraday families read as exit targets/filters:
    ``sess_open`` (today's RTH open), ``prev_close`` (prior RTH session's last close),
    ``gap_fill`` (= ``prev_close``, the gap-fade target), ``day_up`` (close > sess_open).
    All causal: each references only completed prior-session data or the current open."""
    out = df.copy()
    first_open = out["open"].where(out["rth"] & (out["minutes_into_rth"] == 0))
    sess_open_by_day = first_open.groupby(out["rth_date"]).first()
    out["sess_open"] = out["rth_date"].map(sess_open_by_day)

    last_close_by_day = out["close"].where(out["rth"]).groupby(out["rth_date"]).last()
    prev_by_day = last_close_by_day.shift(1)
    out["prev_close"] = out["rth_date"].map(prev_by_day)
    out["gap_fill"] = out["prev_close"]
    out["day_up"] = out["close"] > out["sess_open"]
    return out


def add_session_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Add an RTH-session VWAP (reset each ``rth_date``). Causal: VWAP at bar t uses
    bars up to and including t, which the engine only ACTS on at t+1, so no lookahead."""
    out = df.copy()
    tp = (out["high"] + out["low"] + out["close"]) / 3.0
    pv = tp * out["volume"].fillna(0.0)
    grp = out["rth_date"].where(out["rth"], other=pd.NaT)
    cum_pv = pv.where(out["rth"], other=0.0).groupby(grp).cumsum()
    cum_v = out["volume"].fillna(0.0).where(out["rth"], other=0.0).groupby(grp).cumsum()
    out["vwap"] = (cum_pv / cum_v.replace(0.0, np.nan)).where(out["rth"])
    return out


# --------------------------------------------------------------------------- #
# Load + split
# --------------------------------------------------------------------------- #
@dataclass
class Dataset:
    full: pd.DataFrame  # roll-adjusted, tagged, indicators added
    roll_boundaries: int
    roll_offset_pts: float

    def train(self) -> pd.DataFrame:
        m = (self.full["time"] >= pd.Timestamp(TRAIN_START, tz="UTC")) & (
            self.full["time"] < pd.Timestamp(TRAIN_END, tz="UTC")
        )
        return self.full[m].reset_index(drop=True)

    def holdout(self) -> pd.DataFrame:
        m = (self.full["time"] >= pd.Timestamp(HOLDOUT_START, tz="UTC")) & (
            self.full["time"] < pd.Timestamp(HOLDOUT_END, tz="UTC")
        )
        return self.full[m].reset_index(drop=True)


def load_dataset(path: str = DEFAULT_HISTORY, *, roll: bool = True) -> Dataset:
    """Load → roll-adjust → tag sessions → add indicators → return a ``Dataset`` with
    ``train()`` / ``holdout()`` accessors over the single fixed calendar split."""
    df = load_history(path)
    nb, off = 0, 0.0
    if roll:
        df, info = roll_adjust(df)
        nb, off = len(info.boundaries), info.total_offset_pts
    df = tag_sessions(df)
    df = add_indicators(df)
    df = add_session_levels(df)
    df = add_session_vwap(df)
    return Dataset(full=df.reset_index(drop=True), roll_boundaries=nb, roll_offset_pts=off)
