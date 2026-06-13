"""Batch-1 strategy families — single-instrument (NQ == MNQ point-identical).

CITATION POLICY (operator decision 2026-06-11): the texts are not on the VPS
(copyright; docs/research/book_principles.md). Each family states the CANONICAL
published formulation and cites author/work/chapter; every citation is flagged
``to-verify-against-source`` and NO page numbers are invented. Formulas are the
standard textbook versions of these rules, adapted to a 1-min futures tape where the
original is daily — the adaptation is stated per family.

Each ``Family`` yields one-or-more ``(label, target_pos, ExitSpec)`` variants over a
SMALL grid (few free params = anti-overfit; Pardo, Davey). ``target_pos`` is +1/-1/0
desired position from CLOSED bar t; the engine acts at t+1 open.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .engine import ExitSpec

Variant = tuple[str, np.ndarray, ExitSpec]


@dataclass
class Family:
    key: str
    name: str
    citation: str  # author, work, chapter — flagged to-verify-against-source
    build: Callable[[pd.DataFrame], list[Variant]]
    side: str = "both"


# --------------------------------------------------------------------------- #
# signal helpers
# --------------------------------------------------------------------------- #
def _rising_edge(cond: pd.Series) -> pd.Series:
    """A momentary +True only on the bar the condition FIRST becomes true."""
    return cond & ~cond.shift(1, fill_value=False)


def _momentary(long_trig: pd.Series, short_trig: pd.Series) -> np.ndarray:
    pos = np.zeros(len(long_trig), dtype=float)
    pos[long_trig.to_numpy()] = 1.0
    pos[short_trig.to_numpy()] = -1.0
    return pos


def _opening_range(df: pd.DataFrame, minutes: int) -> tuple[pd.Series, pd.Series]:
    """Per-session opening-range high/low over the first ``minutes`` RTH minutes,
    broadcast to every bar of that session (NaN until the range is complete)."""
    in_or = df["rth"] & (df["minutes_into_rth"] >= 0) & (df["minutes_into_rth"] < minutes)
    grp = df["rth_date"]
    orh = df["high"].where(in_or).groupby(grp).transform("max")
    orl = df["low"].where(in_or).groupby(grp).transform("min")
    return orh, orl


# Session-relative columns (sess_open, prev_close, gap_fill, day_up) are added once by
# dataset.add_session_levels, so the engine always sees them — families just reference them.


# --------------------------------------------------------------------------- #
# 1. Carver EWMAC trend (vol-scaled), multiple speeds
# --------------------------------------------------------------------------- #
def _build_ewmac(df: pd.DataFrame) -> list[Variant]:
    """EWMAC(n,4n): position = sign(EMA_n - EMA_4n) of price, held until the crossover
    flips; protective vol stop at K*ATR. Carver's canonical fast/slow speed set
    {(16,64),(32,128),(64,256)} (Carver, *Advanced Futures Trading Strategies* 2023 /
    *Systematic Trading* 2015 — EWMAC/trend chapter; to-verify-against-source). Adapted
    to 1-min bars (the rule is scale-free); continuous reversal modelled as flip-exit."""
    out: list[Variant] = []
    for fast, slow in ((16, 64), (32, 128), (64, 256)):
        raw = df[f"ema{fast}"] - df[f"ema{slow}"]
        pos = np.sign(raw.fillna(0.0)).to_numpy()
        spec = ExitSpec(stop_atr_mult=8.0, atr_col="atr60", exit_on_flip=True, cooldown_bars=1)
        out.append((f"ewmac{fast}_{slow}", pos, spec))
    return out


# --------------------------------------------------------------------------- #
# 2. Time-series momentum (TSMOM)
# --------------------------------------------------------------------------- #
def _build_tsmom(df: pd.DataFrame) -> list[Variant]:
    """Position = sign of the trailing k-bar return (Moskowitz, Ooi & Pedersen, "Time
    Series Momentum", JFE 2012; Carver — to-verify-against-source). Held until the sign
    flips; vol stop at K*ATR. Daily original adapted to 1-min lookbacks (minutes)."""
    out: list[Variant] = []
    for k in (60, 120, 240):
        ret = df["close"] - df["close"].shift(k)
        pos = np.sign(ret.fillna(0.0)).to_numpy()
        spec = ExitSpec(stop_atr_mult=8.0, atr_col="atr60", exit_on_flip=True, cooldown_bars=1)
        out.append((f"tsmom{k}", pos, spec))
    return out


# --------------------------------------------------------------------------- #
# 3/4. Opening-range breakout (ORB), with and without a trend regime filter
# --------------------------------------------------------------------------- #
def _build_orb(df: pd.DataFrame) -> list[Variant]:
    """Break of the first-R-minute RTH opening range; intraday, flat by close; vol stop
    (Crabel, *Day Trading with Short Term Price Patterns and Opening Range Breakout*;
    Davey, *Building Winning Algorithmic Trading Systems* — to-verify-against-source)."""
    out: list[Variant] = []
    for r in (15, 30):
        orh, orl = _opening_range(df, r)
        ready = df["rth"] & (df["minutes_into_rth"] >= r)
        long_b = _rising_edge(ready & (df["close"] > orh))
        short_b = _rising_edge(ready & (df["close"] < orl))
        pos = _momentary(long_b, short_b)
        spec = ExitSpec(stop_atr_mult=2.0, atr_col="atr14", intraday_only=True, cooldown_bars=3)
        out.append((f"orb{r}", pos, spec))
    return out


def _build_orb_regime(df: pd.DataFrame) -> list[Variant]:
    """ORB but only WITH the prevailing trend (close vs EMA256): long breakouts in an
    uptrend, short in a downtrend. Regime-filtered breakout (Davey; standard trend
    filter — to-verify-against-source)."""
    out: list[Variant] = []
    up = df["close"] > df["ema256"]
    for r in (15, 30):
        orh, orl = _opening_range(df, r)
        ready = df["rth"] & (df["minutes_into_rth"] >= r)
        long_b = _rising_edge(ready & (df["close"] > orh) & up)
        short_b = _rising_edge(ready & (df["close"] < orl) & ~up)
        pos = _momentary(long_b, short_b)
        spec = ExitSpec(stop_atr_mult=2.0, atr_col="atr14", intraday_only=True, cooldown_bars=3)
        out.append((f"orbreg{r}", pos, spec))
    return out


# --------------------------------------------------------------------------- #
# 5. Bollinger-band mean reversion
# --------------------------------------------------------------------------- #
def _build_bollinger(df: pd.DataFrame) -> list[Variant]:
    """Fade a close beyond the 20/2 Bollinger band; exit at the mean (Chan,
    *Algorithmic Trading: Winning Strategies and Their Rationale* / *Quantitative
    Trading*, Bollinger mean-reversion — to-verify-against-source). Plain and a
    chop-only (ADX14<25) variant (mean-reversion works in range, not trend)."""
    out: list[Variant] = []
    long_raw = df["close"] < df["bb_dn"]
    short_raw = df["close"] > df["bb_up"]
    base = ExitSpec(
        stop_atr_mult=3.0, atr_col="atr14", target_col="bb_mid", max_hold_bars=120, cooldown_bars=3
    )
    out.append(("boll_plain", _momentary(_rising_edge(long_raw), _rising_edge(short_raw)), base))
    chop = df["adx14"] < 25
    out.append(
        (
            "boll_chop",
            _momentary(_rising_edge(long_raw & chop), _rising_edge(short_raw & chop)),
            base,
        )
    )
    return out


# --------------------------------------------------------------------------- #
# 6. RSI(2) mean reversion (Connors)
# --------------------------------------------------------------------------- #
def _build_rsi2(df: pd.DataFrame) -> list[Variant]:
    """2-period-RSI reversion: buy deep oversold in an uptrend, sell deep overbought in
    a downtrend; exit at the mean (Connors & Alvarez, *Short Term Trading Strategies
    That Work*, RSI-2; Chan references — to-verify-against-source). Trend filter =
    EMA256 (Connors' 200-period MA analogue)."""
    out: list[Variant] = []
    up = df["close"] > df["ema256"]
    for lo, hi in ((5, 95), (10, 90)):
        long_raw = (df["rsi2"] < lo) & up
        short_raw = (df["rsi2"] > hi) & ~up
        pos = _momentary(_rising_edge(long_raw), _rising_edge(short_raw))
        spec = ExitSpec(
            stop_atr_mult=3.0,
            atr_col="atr14",
            target_col="bb_mid",
            max_hold_bars=120,
            cooldown_bars=3,
        )
        out.append((f"rsi2_{lo}", pos, spec))
    return out


# --------------------------------------------------------------------------- #
# 7. Opening gap fade
# --------------------------------------------------------------------------- #
def _build_gap_fade(df: pd.DataFrame) -> list[Variant]:
    """Fade an RTH opening gap back toward the prior close (gap up -> short, gap down ->
    long; target = prior-session close = gap fill); intraday, flat by close (Chan,
    intraday seasonality / gap reversion; classic gap-fade — to-verify-against-source).
    Gap size threshold in ATR multiples."""
    out: list[Variant] = []
    gap = df["sess_open"] - df["prev_close"]
    at_open = df["rth"] & (df["minutes_into_rth"] >= 0) & (df["minutes_into_rth"] <= 5)
    for g in (1.0, 2.0):
        thresh = g * df["atr14"]
        long_raw = at_open & (gap < -thresh)  # gapped down -> buy
        short_raw = at_open & (gap > thresh)  # gapped up -> sell
        pos = _momentary(_rising_edge(long_raw), _rising_edge(short_raw))
        spec = ExitSpec(
            stop_atr_mult=2.0,
            atr_col="atr14",
            target_col="gap_fill",
            intraday_only=True,
            cooldown_bars=3,
        )
        out.append((f"gapfade{int(g)}", pos, spec))
    return out


# --------------------------------------------------------------------------- #
# 8. Range-compression breakout (NR / Crabel)
# --------------------------------------------------------------------------- #
def _build_nr_breakout(df: pd.DataFrame) -> list[Variant]:
    """After a range CONTRACTION (bar range < f * 20-bar mean range), trade the break of
    that bar's high/low — volatility expansion follows compression (Crabel, *Day Trading
    with Short Term Price Patterns and Opening Range Breakout*, NR/contraction-expansion
    — to-verify-against-source). Intraday, vol stop, time-boxed."""
    out: list[Variant] = []
    for f in (0.5, 0.7):
        compressed = df["range"] < f * df["range_pct20"]
        prior_comp = compressed.shift(1, fill_value=False)
        long_raw = prior_comp & (df["close"] > df["high"].shift(1))
        short_raw = prior_comp & (df["close"] < df["low"].shift(1))
        pos = _momentary(_rising_edge(long_raw), _rising_edge(short_raw))
        spec = ExitSpec(stop_atr_mult=2.0, atr_col="atr14", max_hold_bars=240, cooldown_bars=3)
        out.append((f"nrbo{int(f * 10)}", pos, spec))
    return out


# --------------------------------------------------------------------------- #
# 9. VWAP reversion
# --------------------------------------------------------------------------- #
def _build_vwap_rev(df: pd.DataFrame) -> list[Variant]:
    """Fade a stretch away from the session VWAP (long when close < VWAP - m*ATR, short
    above); exit back at VWAP; intraday (Chan, intraday mean-reversion to VWAP; standard
    — to-verify-against-source)."""
    out: list[Variant] = []
    for m in (1.0, 1.5):
        stretch = m * df["atr14"]
        long_raw = df["rth"] & (df["close"] < df["vwap"] - stretch)
        short_raw = df["rth"] & (df["close"] > df["vwap"] + stretch)
        pos = _momentary(_rising_edge(long_raw), _rising_edge(short_raw))
        spec = ExitSpec(
            stop_atr_mult=2.0,
            atr_col="atr14",
            target_col="vwap",
            intraday_only=True,
            max_hold_bars=120,
            cooldown_bars=3,
        )
        out.append((f"vwaprev{int(m * 10)}", pos, spec))
    return out


# --------------------------------------------------------------------------- #
# 10. Intraday time-of-day momentum (seasonality)
# --------------------------------------------------------------------------- #
def _build_tod(df: pd.DataFrame) -> list[Variant]:
    """Time-of-day seasonality: at a fixed ET hour, take the day's prevailing direction
    (close vs RTH open) into the close — the late-session momentum/close effect (Chan,
    intraday seasonality — to-verify-against-source). Exploratory; flat by close."""
    out: list[Variant] = []
    day_up = df["day_up"]
    et_min = df["et"].dt.hour * 60 + df["et"].dt.minute
    for hh in (14 * 60, 15 * 60):  # 14:00, 15:00 ET trigger
        at_t = df["rth"] & (et_min >= hh) & (et_min < hh + 1)
        long_raw = at_t & day_up
        short_raw = at_t & ~day_up
        pos = _momentary(_rising_edge(long_raw), _rising_edge(short_raw))
        spec = ExitSpec(stop_atr_mult=3.0, atr_col="atr14", intraday_only=True, cooldown_bars=3)
        out.append((f"tod{hh // 60}", pos, spec))
    return out


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def build_registry() -> list[Family]:
    return [
        Family(
            "ewmac",
            "Carver EWMAC trend (vol-scaled, 3 speeds)",
            "Carver, Advanced Futures Trading Strategies 2023 / Systematic Trading 2015"
            " — EWMAC ch. [to-verify]",
            _build_ewmac,
            "both",
        ),
        Family(
            "tsmom",
            "Time-series momentum (3 lookbacks)",
            "Moskowitz/Ooi/Pedersen, Time Series Momentum, JFE 2012; Carver [to-verify]",
            _build_tsmom,
            "both",
        ),
        Family(
            "orb",
            "Opening-range breakout",
            "Crabel, ORB patterns; Davey, Building Winning Algorithmic Trading Systems [to-verify]",
            _build_orb,
            "both",
        ),
        Family(
            "orbreg",
            "Opening-range breakout + trend regime",
            "Davey, regime-filtered breakout [to-verify]",
            _build_orb_regime,
            "both",
        ),
        Family(
            "boll",
            "Bollinger-band mean reversion",
            "Chan, Algorithmic Trading / Quantitative Trading — Bollinger MR [to-verify]",
            _build_bollinger,
            "both",
        ),
        Family(
            "rsi2",
            "RSI(2) mean reversion (Connors)",
            "Connors & Alvarez, Short Term Trading Strategies That Work — RSI-2 [to-verify]",
            _build_rsi2,
            "both",
        ),
        Family(
            "gapfade",
            "Opening gap fade",
            "Chan, intraday seasonality / gap reversion [to-verify]",
            _build_gap_fade,
            "both",
        ),
        Family(
            "nrbo",
            "Range-compression breakout (NR)",
            "Crabel, contraction-expansion / NR patterns [to-verify]",
            _build_nr_breakout,
            "both",
        ),
        Family(
            "vwaprev",
            "VWAP reversion",
            "Chan, intraday VWAP mean-reversion [to-verify]",
            _build_vwap_rev,
            "both",
        ),
        Family(
            "tod",
            "Intraday time-of-day momentum",
            "Chan, intraday seasonality [to-verify]",
            _build_tod,
            "both",
        ),
    ]
