"""Carver EWMAC trend signal — verbatim from the framework (Carver, *Systematic Trading*
2015 ch.7 / App.B; *Advanced Futures Trading Strategies* 2023 — to-verify-against-source).

Pipeline, per market, on the BACK-ADJUSTED signed price series (NOT the raw front
contract — Carver: raw price makes vol jumpy and discards roll-down; the Panama series is
the correct input):

  1. raw EWMAC      = EWMA(price, fast) - EWMA(price, slow)
  2. price vol      = EWMA stdev of DAILY PRICE CHANGES (EWMA, ~25-36d span; NOT a simple
                      MA — Carver explicitly rejects MA vol as too jumpy). In PRICE points.
  3. vol-normalized = raw / price_vol            (a dimensionless forecast, ~N(0, .))
  4. forecast       = clip(forecast_scalar * vol_normalized, -CAP, +CAP), where the scalar
                      is chosen so mean|forecast| ~= 10 (Carver's standard forecast scale),
                      and CAP = 20.

The forecast scalar is estimated on the TRAIN slice ONLY (pooled across markets, per
speed) and then applied unchanged to the holdout — no peeking. ``price_vol`` is reused as
the sizing volatility by the portfolio layer (one vol estimate drives both the forecast
denominator and the position size, exactly as Carver specifies).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

FORECAST_CAP = 20.0
TARGET_ABS_FORECAST = 10.0
VOL_SPAN = 32  # EWMA span for the price-change vol (within Carver's ~25-36 day band)
VOL_FLOOR_FRAC = 1e-9  # guard against div-by-zero on a dead series


@dataclass(frozen=True)
class Speed:
    fast: int
    slow: int

    @property
    def label(self) -> str:
        return f"{self.fast}/{self.slow}"


# The three WELL-SEPARATED speeds (Carver's canonical set). Adjacent speeds are ~0.9
# correlated and are NOT independent trials, so we test only these three.
SPEEDS: list[Speed] = [Speed(16, 64), Speed(32, 128), Speed(64, 256)]


def ewma(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def price_vol(price: pd.Series, span: int = VOL_SPAN) -> pd.Series:
    """EWMA standard deviation of daily PRICE CHANGES (price points), Carver's sizing vol.

    Uses ``ewm(...).std()`` on the first difference — an exponentially-weighted (not
    simple-MA) estimate, per Carver. ``min_periods=span`` so the early, undersmoothed
    estimates are NaN rather than noisy."""
    dchg = price.diff()
    return dchg.ewm(span=span, adjust=False, min_periods=span).std()


def raw_ewmac(price: pd.Series, speed: Speed) -> pd.Series:
    return ewma(price, speed.fast) - ewma(price, speed.slow)


def vol_normalized_forecast(price: pd.Series, speed: Speed) -> tuple[pd.Series, pd.Series]:
    """Return ``(vol_normalized_raw, price_vol)`` — the pre-scalar forecast and the sizing
    vol, both on the FULL series so warm-up uses pre-window history."""
    pv = price_vol(price)
    raw = raw_ewmac(price, speed)
    pv_safe = pv.where(pv.abs() > VOL_FLOOR_FRAC)
    return raw / pv_safe, pv


def estimate_forecast_scalar(norm_by_symbol: dict[str, pd.Series], train_mask_by_symbol) -> float:
    """One forecast scalar per speed, estimated on TRAIN across all markets so that
    mean(|scalar * vol_normalized|) ~= 10. Pooled (Carver estimates the scalar across
    instruments for a given rule), and applied unchanged to the holdout."""
    pooled = []
    for sym, norm in norm_by_symbol.items():
        m = train_mask_by_symbol[sym]
        vals = norm[m].abs().dropna()
        pooled.append(vals)
    allabs = pd.concat(pooled) if pooled else pd.Series(dtype=float)
    mean_abs = float(allabs.mean()) if len(allabs) else 0.0
    if mean_abs <= 0:
        return 0.0
    return TARGET_ABS_FORECAST / mean_abs


def scaled_forecast(vol_normalized: pd.Series, scalar: float) -> pd.Series:
    """Apply the forecast scalar and cap at +/-20 (Carver)."""
    return np.clip(vol_normalized * scalar, -FORECAST_CAP, FORECAST_CAP)
