"""Tests for src.strategy — synthetic bar streams, no IB, no DB."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from config.risk_params import RISK
from src.indicators import add_all_indicators
from src.strategy import Signal, Sma100BounceStrategy, detect_signal

ET = ZoneInfo("America/New_York")


def _uptrend_bar_dicts(n: int = 120, *, start: float = 17400.0, step: float = 0.5) -> list[dict]:
    """Generate n monotonic OHLC bars stamped at 1-minute intervals during RTH.

    Default ``step=+0.5`` produces an uptrend where ``MA50 > MA100`` (recent
    50 bars are higher than the longer 100-bar window). Pass ``step=-0.5`` for
    a "pullback" series where ``MA100 > MA50`` — the regime the post-PR-33
    strategy requires for a LONG signal. Bars start at 11:00 ET so they are
    well outside the session-edge windows used by the strategy.
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


def _pullback_bar_dicts(n: int = 120, *, start: float = 17900.0) -> list[dict]:
    """Monotonic decline so ``MA100 > MA50`` — the regime PR #33 requires."""
    return _uptrend_bar_dicts(n=n, start=start, step=-0.5)


def _engineer_fire_bar(bars: list[dict], *, low_offset_from_ma_slow: float = 0.0) -> float:
    """Replace ``bars[-1]`` with a bullish touch bar and return its close price.

    Computes ``ma_slow`` from ``bars[:-1]`` via ``add_all_indicators`` so the
    engineered ``low`` lands at ``ma_slow + low_offset_from_ma_slow`` (default
    0 → exact touch). Close is set above ``ma_slow`` so the bar is bullish.
    """
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    last_close = ma_slow_prev + 12.0
    bars[-1] = {
        "time": bars[-1]["time"],
        "open": last_close - 1.0,
        "high": last_close + 0.5,
        "low": ma_slow_prev + low_offset_from_ma_slow,
        "close": last_close,
    }
    return last_close


def _stuff_strategy(strat: Sma100BounceStrategy, bars: list[dict]) -> Signal | None:
    """Feed every bar but the last into the strategy without triggering a signal
    check at warmup. Return the result of feeding the LAST bar.
    """
    last_result: Signal | None = None
    for bar in bars:
        last_result = strat.on_new_bar(bar)
    return last_result


def test_long_signal_fires_when_ma_slow_above_fast_with_touch_and_bullish():
    """PR #33 happy path: MA100>MA50 (pullback), touch, bullish, gap>=0.5."""
    bars = _pullback_bar_dicts(n=120)
    last_close = _engineer_fire_bar(bars)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)

    assert signal is not None
    assert signal.direction == "LONG"
    assert signal.entry_price == last_close
    assert signal.stop_price == last_close - RISK.stop_loss_pts
    assert signal.target_price == last_close + RISK.take_profit_pts
    assert signal.adx_value == 0.0  # PR #33: ADX no longer computed


def test_no_signal_when_ma_fast_above_ma_slow_inverted_regime():
    """Uptrend (MA50>MA100) is now the BLOCKED regime — opposite of pre-PR-33.

    Pre-PR-33 the bot required ``ma_fast > ma_slow``; SeanBot's reference is
    ``ma_slow > ma_fast`` (pullback). With an uptrend fixture and an engineered
    touch+bullish bar that would have FIRED pre-PR-33, the signal must now be
    suppressed by the ma_order gate.
    """
    bars = _uptrend_bar_dicts(n=120)
    _engineer_fire_bar(bars)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_no_signal_when_ma_gap_below_threshold():
    """Default ``ma_min_gap_pts=0.5`` blocks near-flat MA pairs."""
    bars = _uptrend_bar_dicts(n=120, step=0.001)
    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_signal_fires_when_ma_gap_at_0_5_boundary():
    """Gap exactly at the threshold must PASS (``>=``)."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars)
    # Measure gap on the FULL engineered series — the engineered last bar shifts
    # the MAs vs bars[:-1], and the strategy reads the gap from the last row.
    df_full = add_all_indicators(pd.DataFrame(bars))
    measured_gap = abs(float(df_full["ma_slow"].iloc[-1]) - float(df_full["ma_fast"].iloc[-1]))

    strat = Sma100BounceStrategy("MNQM6", params=replace(RISK, ma_min_gap_pts=measured_gap))
    signal = _stuff_strategy(strat, bars)
    assert signal is not None
    assert signal.direction == "LONG"


def test_no_signal_when_candle_is_bearish():
    """All gates pass except bullish — confirms the bullish leg gates correctly."""
    bars = _pullback_bar_dicts(n=120)
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


def test_no_signal_when_low_too_far_below_ma_slow():
    """Windowed touch lower bound: ``low`` 20 pts below ``MA100`` must block."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars, low_offset_from_ma_slow=-20.0)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_signal_fires_when_low_at_ma_slow_minus_10():
    """Low 10 pts below ``MA100`` is inside the [-15, +5] window — fires."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars, low_offset_from_ma_slow=-10.0)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is not None
    assert signal.direction == "LONG"


def test_signal_fires_when_low_just_above_ma_slow():
    """Low 3 pts above ``MA100`` is inside the [-15, +5] window — fires."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars, low_offset_from_ma_slow=3.0)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is not None
    assert signal.direction == "LONG"


def test_no_signal_when_low_above_ma_slow_plus_buffer():
    """Low more than ``ma_touch_buffer_pts`` above ``MA100`` is outside the window."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars, low_offset_from_ma_slow=10.0)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_decision_trace_logged_on_block(caplog):
    """Every blocked bar emits a DECISION_TRACE line at DEBUG with all 4 gate results."""
    bars = _uptrend_bar_dicts(n=120)  # MA50>MA100 → ma_order blocks
    _engineer_fire_bar(bars)

    strat = Sma100BounceStrategy("MNQM6")
    with caplog.at_level(logging.DEBUG, logger="src.strategy"):
        signal = _stuff_strategy(strat, bars)

    assert signal is None
    trace_lines = [r.getMessage() for r in caplog.records if "DECISION_TRACE" in r.getMessage()]
    assert len(trace_lines) >= 1
    last = trace_lines[-1]
    assert "LONG blocked" in last
    assert "ma_order(MA100>MA50)=FAIL" in last
    assert "touch=" in last
    assert "bullish=" in last
    assert "gap=" in last


def test_no_signal_within_session_edge_open_window():
    """Bars inside the 5-min RTH open edge are gated regardless of strategy state."""
    base_et = datetime(2026, 5, 21, 9, 35, tzinfo=ET)
    bars = _uptrend_bar_dicts(n=120)
    bars[-1] = dict(bars[-1])
    bars[-1]["time"] = base_et.astimezone(UTC)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_no_signal_within_session_edge_close_window():
    """Bars inside the 5-min RTH close edge are gated regardless of strategy state."""
    base_et = datetime(2026, 5, 21, 15, 58, tzinfo=ET)
    bars = _uptrend_bar_dicts(n=120)
    bars[-1] = dict(bars[-1])
    bars[-1]["time"] = base_et.astimezone(UTC)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_cooldown_suppresses_signal_for_configured_bars():
    """After mark_trade_closed, the next N bars are suppressed regardless of fire conditions."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars)

    short_cooldown = replace(RISK, cooldown_bars=3)
    strat = Sma100BounceStrategy("MNQM6", params=short_cooldown)
    for bar in bars[:-1]:
        strat.on_new_bar(bar)
    strat.mark_trade_closed()

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


def test_mark_trade_closed_logs_cooldown_bars():
    strat = Sma100BounceStrategy("MNQM6")
    assert strat._cooldown_bars_remaining == 0  # type: ignore[attr-defined]
    strat.mark_trade_closed()
    assert strat._cooldown_bars_remaining == RISK.cooldown_bars  # type: ignore[attr-defined]
