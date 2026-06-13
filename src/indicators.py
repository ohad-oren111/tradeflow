"""Technical indicators used by the MA50/MA100 bounce + ADX filter strategy.

Ported verbatim from SeanBot. Pandas/numpy only — no third-party TA library —
so the formulas are visible at the call site for diagnosis.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def add_sma(df: pd.DataFrame, period: int, col: str = "close") -> pd.DataFrame:
    out = df.copy()
    out[f"sma_{period}"] = out[col].rolling(window=period).mean()
    return out


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    out = df.copy()
    high = out["high"]
    low = out["low"]
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    out["tr"] = tr
    out["atr"] = tr.ewm(span=period, adjust=False).mean()
    return out


def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    out = df.copy()
    if "atr" not in out.columns:
        out = add_atr(out, period)
    high = out["high"]
    low = out["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=out.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=out.index,
    )
    atr_s = out["tr"].ewm(span=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr_s)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr_s)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    out["adx"] = dx.ewm(span=period, adjust=False).mean()
    return out


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = add_atr(out)
    out = add_sma(out, 50)
    out = add_sma(out, 100)
    out = add_adx(out)
    out["ma_fast"] = out["sma_50"]
    out["ma_slow"] = out["sma_100"]
    return out
