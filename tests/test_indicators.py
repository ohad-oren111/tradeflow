"""Tests for src.indicators — pure pandas/numpy, no mocks."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators import add_adx, add_all_indicators, add_atr, add_sma


def _ohlc_frame(closes: list[float]) -> pd.DataFrame:
    """Build a minimal OHLC frame from a list of closes; bar range = ±1."""
    return pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
        }
    )


def test_add_sma_matches_rolling_mean():
    df = _ohlc_frame(list(range(1, 21)))  # closes 1..20
    out = add_sma(df, period=5)
    # First 4 SMA values are NaN; 5th equals mean(1..5) = 3.0
    assert pd.isna(out["sma_5"].iloc[3])
    assert out["sma_5"].iloc[4] == 3.0
    assert out["sma_5"].iloc[-1] == 18.0  # mean(16..20)


def test_add_sma_does_not_mutate_input():
    df = _ohlc_frame([10.0, 11.0, 12.0])
    snapshot = df.copy()
    add_sma(df, period=2)
    pd.testing.assert_frame_equal(df, snapshot)


def test_add_atr_produces_atr_column():
    closes = [100.0 + i * 0.5 for i in range(30)]
    df = _ohlc_frame(closes)
    out = add_atr(df, period=14)
    assert "atr" in out.columns
    assert "tr" in out.columns
    # ATR cannot be NaN past the first bar (EWM with adjust=False seeds with first TR).
    assert not pd.isna(out["atr"].iloc[5])
    assert (out["atr"].dropna() > 0).all()


def test_add_adx_higher_for_strong_trend_than_choppy():
    trending_closes = [100.0 + i * 1.0 for i in range(60)]
    choppy_closes = [100.0 + ((-1) ** i) * 0.1 for i in range(60)]

    trend_adx = add_adx(_ohlc_frame(trending_closes), period=14)["adx"].iloc[-1]
    chop_adx = add_adx(_ohlc_frame(choppy_closes), period=14)["adx"].iloc[-1]

    assert trend_adx > chop_adx
    assert trend_adx >= 20.0  # strong trend should clear standard threshold


def test_add_adx_handles_flat_prices_without_dividing_by_zero():
    flat_closes = [100.0] * 30
    out = add_adx(_ohlc_frame(flat_closes), period=14)
    # ADX should be defined (0 or NaN) — never inf or raised.
    assert np.isfinite(out["adx"].fillna(0)).all()


def test_add_all_indicators_populates_alias_columns():
    closes = [100.0 + i * 0.25 for i in range(120)]
    out = add_all_indicators(_ohlc_frame(closes))
    for col in ("sma_50", "sma_100", "atr", "adx", "ma_fast", "ma_slow"):
        assert col in out.columns, f"missing column {col}"
    # ma_fast aliases sma_50; ma_slow aliases sma_100.
    pd.testing.assert_series_equal(out["ma_fast"], out["sma_50"], check_names=False)
    pd.testing.assert_series_equal(out["ma_slow"], out["sma_100"], check_names=False)


def test_add_all_indicators_last_bar_has_populated_ma_after_100_bars():
    closes = [100.0 + i * 0.25 for i in range(120)]
    out = add_all_indicators(_ohlc_frame(closes))
    last = out.iloc[-1]
    assert not pd.isna(last["ma_fast"])
    assert not pd.isna(last["ma_slow"])
    assert not pd.isna(last["adx"])
