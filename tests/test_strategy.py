"""Tests for src.strategy — synthetic bar streams, no IB, no DB."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from config.risk_params import RISK
from src.indicators import add_all_indicators
from src.strategy import Signal, Sma100BounceStrategy, detect_signal

ET = ZoneInfo("America/New_York")


def _uptrend_bar_dicts(n: int = 120, *, start: float = 17400.0, step: float = 0.5) -> list[dict]:
    """Generate n uptrend OHLC bars stamped at 1-minute intervals during RTH.

    Bars start at 11:00 ET so they are well outside the session-edge windows
    used by the strategy (5min from open/close by default).
    """
    base_et = datetime(2026, 5, 21, 11, 0, tzinfo=ET)
    bars: list[dict] = []
    for i in range(n):
        close = start + i * step
        bars.append(
            {
                "time": (base_et + pd.Timedelta(minutes=i)).astimezone(UTC),
                "open": close - 0.25,
                "high": close + 0.25,
                "low": close - 0.5,
                "close": close,
            }
        )
    return bars


def _stuff_strategy(strat: Sma100BounceStrategy, bars: list[dict]) -> Signal | None:
    """Feed every bar but the last into the strategy without triggering a signal
    check at warmup. Return the result of feeding the LAST bar.
    """
    last_result: Signal | None = None
    for bar in bars:
        last_result = strat.on_new_bar(bar)
    return last_result


def test_long_signal_fires_on_touch_with_bullish_close():
    # 120 uptrend bars to warm MA100; then engineer a "touch + bullish close" bar.
    bars = _uptrend_bar_dicts(n=120)
    # Replace the last bar so its low touches ma_slow and close > open.
    # Build the indicator frame to learn ma_slow at the second-to-last bar.
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    # Engineer last bar: low <= ma_slow + buffer, close > open, bullish.
    last_close = ma_slow_prev + 12.0  # above ma_slow so trend persists
    bars[-1] = {
        "time": bars[-1]["time"],
        "open": last_close - 1.0,
        "high": last_close + 0.5,
        "low": ma_slow_prev,  # touches MA100
        "close": last_close,
    }

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)

    assert signal is not None
    assert signal.direction == "LONG"
    assert signal.entry_price == last_close
    assert signal.stop_price == last_close - RISK.stop_loss_pts
    assert signal.target_price == last_close + RISK.take_profit_pts


def test_no_signal_when_adx_below_threshold():
    # Generate uptrend with ADX > 20 expected; then override risk params with
    # a high adx threshold to gate out the signal.
    bars = _uptrend_bar_dicts(n=120)
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    last_close = ma_slow_prev + 12.0
    bars[-1] = {
        "time": bars[-1]["time"],
        "open": last_close - 1.0,
        "high": last_close + 0.5,
        "low": ma_slow_prev,
        "close": last_close,
    }
    high_adx_params = replace(RISK, adx_min_threshold=99.0)

    strat = Sma100BounceStrategy("MNQM6", params=high_adx_params)
    signal = _stuff_strategy(strat, bars)

    assert signal is None


def test_no_signal_when_ma_gap_below_threshold():
    bars = _uptrend_bar_dicts(n=120, step=0.001)  # very flat → ma_gap tiny
    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_no_signal_when_candle_is_bearish():
    bars = _uptrend_bar_dicts(n=120)
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    last_close = ma_slow_prev + 12.0
    bars[-1] = {
        "time": bars[-1]["time"],
        "open": last_close + 5.0,  # open > close → bearish
        "high": last_close + 6.0,
        "low": ma_slow_prev,
        "close": last_close,
    }
    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_no_signal_when_downtrend():
    bars = _uptrend_bar_dicts(n=120, start=17900.0, step=-0.5)  # downtrend
    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None  # SHORT branch deferred


def test_no_signal_within_session_edge_open_window():
    # Build a bar at 09:35 ET — within the 5-min open edge by default.
    base_et = datetime(2026, 5, 21, 9, 35, tzinfo=ET)
    # Warm MA100 with prior bars (also inside edge — but the gate only looks
    # at the LAST bar's timestamp via on_new_bar; we want the LAST bar inside
    # the edge window).
    bars = _uptrend_bar_dicts(n=120)
    bars[-1] = dict(bars[-1])
    bars[-1]["time"] = base_et.astimezone(UTC)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_no_signal_within_session_edge_close_window():
    base_et = datetime(2026, 5, 21, 15, 58, tzinfo=ET)  # within 5min of 16:00
    bars = _uptrend_bar_dicts(n=120)
    bars[-1] = dict(bars[-1])
    bars[-1]["time"] = base_et.astimezone(UTC)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_cooldown_suppresses_signal_for_configured_bars():
    bars = _uptrend_bar_dicts(n=120)
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    last_close = ma_slow_prev + 12.0
    bars[-1] = {
        "time": bars[-1]["time"],
        "open": last_close - 1.0,
        "high": last_close + 0.5,
        "low": ma_slow_prev,
        "close": last_close,
    }

    short_cooldown = replace(RISK, cooldown_bars=3)
    strat = Sma100BounceStrategy("MNQM6", params=short_cooldown)
    # Feed warmup bars without arming cooldown.
    for bar in bars[:-1]:
        strat.on_new_bar(bar)
    strat.mark_trade_closed()

    # Next cooldown bars should suppress.
    suppressed = strat.on_new_bar(bars[-1])
    assert suppressed is None
    for _ in range(short_cooldown.cooldown_bars - 1):
        suppressed = strat.on_new_bar(bars[-1])
        assert suppressed is None


def test_less_than_two_bars_returns_none():
    df = pd.DataFrame([{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}])
    df = add_all_indicators(df)
    result = detect_signal(df, "MNQM6")
    assert result is None


def test_detect_signal_returns_none_on_nan_ma():
    bars = _uptrend_bar_dicts(n=10)  # too few for ma_slow=100
    df = add_all_indicators(pd.DataFrame(bars))
    assert pd.isna(df["ma_slow"].iloc[-1])
    assert detect_signal(df, "MNQM6") is None


def test_detect_signal_treats_nan_adx_as_zero_then_gates():
    # Build a 2-bar frame with explicit fields so ma_fast/ma_slow are usable
    # and adx is NaN. The default RISK.adx_min_threshold=20.0 will gate it out.
    row = {
        "open": 100.0,
        "high": 100.5,
        "low": 95.0,
        "close": 100.5,
        "ma_fast": 100.5,
        "ma_slow": 95.0,
        "adx": float("nan"),
    }
    df = pd.DataFrame([dict(row), dict(row)])
    result = detect_signal(df, "MNQM6")
    assert result is None  # NaN adx → 0 → below threshold


def test_mark_trade_closed_logs_cooldown_bars():
    strat = Sma100BounceStrategy("MNQM6")
    assert strat._cooldown_bars_remaining == 0  # type: ignore[attr-defined]
    strat.mark_trade_closed()
    assert strat._cooldown_bars_remaining == RISK.cooldown_bars  # type: ignore[attr-defined]
