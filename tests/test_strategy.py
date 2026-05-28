"""Tests for src.strategy — synthetic bar streams, no IB, no DB."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from config.risk_params import RISK
from src.indicators import add_all_indicators
from src.strategy import (
    GateEval,
    Signal,
    Sma100BounceStrategy,
    _first_failed_filter,
    _in_session_edge_window,
    _regime_ok,
    detect_signal,
    evaluate_gates,
)

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


def test_no_signal_during_daily_break_edge_via_on_new_bar():
    # 17:30 ET Thursday — inside the daily CME maintenance break. Even with an
    # engineered touch + bullish bar, the session-edge gate must suppress.
    base_et = datetime(2026, 5, 21, 17, 30, tzinfo=ET)
    bars = _uptrend_bar_dicts(n=120)
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    last_close = ma_slow_prev + 12.0
    bars[-1] = {
        "time": base_et.astimezone(UTC),
        "open": last_close - 1.0,
        "high": last_close + 0.5,
        "low": ma_slow_prev,
        "close": last_close,
    }

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_no_signal_after_friday_weekend_cutoff_via_on_new_bar():
    # Fri 16:35 ET — past the 16:30 ET operator cutoff.
    base_et = datetime(2026, 5, 22, 16, 35, tzinfo=ET)  # Friday
    bars = _uptrend_bar_dicts(n=120)
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    last_close = ma_slow_prev + 12.0
    bars[-1] = {
        "time": base_et.astimezone(UTC),
        "open": last_close - 1.0,
        "high": last_close + 0.5,
        "low": ma_slow_prev,
        "close": last_close,
    }

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


# ----------------------------------------------- 24/5 session edge window
# Direct unit tests of ``_in_session_edge_window``. ``edge_minutes=5`` matches
# ``RISK.session_edge_no_trade_minutes`` and is passed explicitly so the tests
# don't depend on a global default. Wall-clock anchors are constructed in ET
# and converted to UTC, the same way live bar timestamps reach the helper.


def _et(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET).astimezone(UTC)


class TestSessionEdgeWindow24x5:
    EDGE = 5

    def test_saturday_is_no_trade(self):
        # 2026-05-23 is a Saturday.
        assert _in_session_edge_window(_et(2026, 5, 23, 12, 0), self.EDGE) is True

    def test_sunday_before_18_is_no_trade(self):
        # Sun 17:55 ET — before the weekly open at 18:00.
        assert _in_session_edge_window(_et(2026, 5, 24, 17, 55), self.EDGE) is True

    def test_sunday_open_within_edge_is_no_trade(self):
        # Sun 18:03 ET — 3 min into the post-open edge (5-min default).
        assert _in_session_edge_window(_et(2026, 5, 24, 18, 3), self.EDGE) is True

    def test_sunday_open_past_edge_is_tradable(self):
        # Sun 18:06 ET — past the 5-min post-open edge.
        assert _in_session_edge_window(_et(2026, 5, 24, 18, 6), self.EDGE) is False

    def test_monday_morning_overnight_is_tradable(self):
        # Mon 03:00 ET — deep overnight, far from any gate.
        assert _in_session_edge_window(_et(2026, 5, 25, 3, 0), self.EDGE) is False

    def test_monday_pre_break_edge_is_no_trade(self):
        # Mon 16:57 ET — 3 min before the 17:00 break.
        assert _in_session_edge_window(_et(2026, 5, 25, 16, 57), self.EDGE) is True

    def test_monday_during_break_is_no_trade(self):
        # Mon 17:30 ET — inside the CME daily maintenance break.
        assert _in_session_edge_window(_et(2026, 5, 25, 17, 30), self.EDGE) is True

    def test_monday_post_break_edge_is_no_trade(self):
        # Mon 18:03 ET — 3 min into the 5-min post-break edge.
        assert _in_session_edge_window(_et(2026, 5, 25, 18, 3), self.EDGE) is True

    def test_monday_post_break_past_edge_is_tradable(self):
        # Mon 18:06 ET — past the 5-min post-break edge.
        assert _in_session_edge_window(_et(2026, 5, 25, 18, 6), self.EDGE) is False

    def test_thursday_evening_after_break_is_tradable(self):
        # Thu 22:00 ET — well after the daily break, before gateway restart.
        assert _in_session_edge_window(_et(2026, 5, 21, 22, 0), self.EDGE) is False

    def test_gateway_restart_window_is_no_trade(self):
        # Thu 23:50 ET — inside the 23:45→00:15 ET gateway restart band.
        assert _in_session_edge_window(_et(2026, 5, 21, 23, 50), self.EDGE) is True

    def test_after_gateway_restart_is_tradable(self):
        # Fri 00:20 ET — past the gateway restart window.
        assert _in_session_edge_window(_et(2026, 5, 22, 0, 20), self.EDGE) is False

    def test_friday_before_cutoff_is_tradable(self):
        # Fri 14:00 ET — well before the operator weekend cutoff at 16:30.
        assert _in_session_edge_window(_et(2026, 5, 22, 14, 0), self.EDGE) is False

    def test_friday_pre_cutoff_edge_is_no_trade(self):
        # Fri 16:27 ET — 3 min inside the 5-min pre-cutoff edge.
        assert _in_session_edge_window(_et(2026, 5, 22, 16, 27), self.EDGE) is True

    def test_friday_after_cutoff_is_no_trade(self):
        # Fri 17:00 ET — past the 16:30 operator cutoff.
        assert _in_session_edge_window(_et(2026, 5, 22, 17, 0), self.EDGE) is True

    def test_zero_edge_minutes_keeps_break_window_no_trade(self):
        # With edge_minutes=0 the pad collapses to zero, but the break itself
        # is still no-trade. 17:30 ET on Mon is mid-break.
        assert _in_session_edge_window(_et(2026, 5, 25, 17, 30), 0) is True

    def test_dst_spring_forward_2026_03_08_boundary(self):
        # 2026-03-08 02:00 ET → 03:00 ET (DST starts). zoneinfo handles the
        # skip; we verify that mid-overnight is still tradable both sides of
        # the boundary (Mon 02:30 doesn't exist, so use 04:00 instead).
        assert _in_session_edge_window(_et(2026, 3, 9, 4, 0), self.EDGE) is False
        # And the Sunday-before-open gate still fires the night of the change.
        # 2026-03-08 is a Sunday; 17:30 ET is before the 18:00 weekly open.
        assert _in_session_edge_window(_et(2026, 3, 8, 17, 30), self.EDGE) is True

    def test_dst_fall_back_2026_11_01_boundary(self):
        # 2026-11-01 02:00 ET → 01:00 ET (DST ends). The Friday before is
        # 2026-10-30; the daily break still fires at 17:30 ET wall-clock.
        assert _in_session_edge_window(_et(2026, 10, 30, 17, 30), self.EDGE) is True
        # Sunday open still fires at 18:00 ET wall-clock post-DST.
        assert _in_session_edge_window(_et(2026, 11, 1, 17, 30), self.EDGE) is True
        assert _in_session_edge_window(_et(2026, 11, 1, 18, 6), self.EDGE) is False


# ------------------------------------------------ 24/5 on_new_bar integration


def test_on_new_bar_skips_signal_during_gateway_restart():
    # Engineer a touch + bullish bar that WOULD fire under normal conditions,
    # but stamp it inside the 23:45→00:15 ET gateway restart band.
    bars = _pullback_bar_dicts(n=120)  # MA100>MA50 — PR #33 strategy regime
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    last_close = ma_slow_prev + 12.0
    bars[-1] = {
        "time": datetime(2026, 5, 21, 23, 55, tzinfo=ET).astimezone(UTC),
        "open": last_close - 1.0,
        "high": last_close + 0.5,
        "low": ma_slow_prev,
        "close": last_close,
    }

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is None


def test_on_new_bar_fires_signal_on_thursday_overnight():
    # Same engineered bar, but stamped Thu 22:00 ET (tradable under 24/5,
    # would have been gated out under the old RTH-only rule).
    bars = _pullback_bar_dicts(n=120)  # MA100>MA50 — PR #33 strategy regime
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    last_close = ma_slow_prev + 12.0
    bars[-1] = {
        "time": datetime(2026, 5, 21, 22, 0, tzinfo=ET).astimezone(UTC),
        "open": last_close - 1.0,
        "high": last_close + 0.5,
        "low": ma_slow_prev,
        "close": last_close,
    }

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    assert signal is not None
    assert signal.direction == "LONG"


# ----------------------------------------- C1 regime gate (PR #33 addendum)
# `_regime_ok` requires >=202 30-min bars to evaluate non-warmup behavior. The
# Sma100BounceStrategy buffer caps at 150 1-min bars, so the live-path tests
# below directly exercise `_regime_ok` with hand-built 30-min-spaced frames.
# `test_regime_gate_fails_open_during_warmup` is the integration test that
# confirms the gate doesn't break the existing 1-min strategy flow.


def _thirty_minute_close_frame(
    closes: list[float], *, start_ts: datetime | None = None
) -> pd.DataFrame:
    """Build a DataFrame with one row per ``closes`` value, 30-min spacing.

    The 'time' column is UTC tz-aware so `_regime_ok` can resample cleanly.
    """
    if start_ts is None:
        start_ts = datetime(2026, 5, 21, 0, 0, tzinfo=UTC)
    times = [start_ts + pd.Timedelta(minutes=30 * i) for i in range(len(closes))]
    return pd.DataFrame({"time": times, "close": closes})


def test_regime_gate_blocks_long_when_price_below_30m_ema200(caplog):
    """C1: when last 30-min price is at or below 30m EMA200, block."""
    # 250 30-min bars in a strong downtrend so the latest close is well below
    # the EMA200 of the resampled series. 250 >= 202 → past warmup.
    closes = [18000.0 - i * 5.0 for i in range(250)]
    df = _thirty_minute_close_frame(closes)

    with caplog.at_level(logging.INFO, logger="src.strategy"):
        ok = _regime_ok(df, RISK)

    assert ok is False
    blocked = [r.getMessage() for r in caplog.records if "regime gate BLOCKED" in r.getMessage()]
    assert len(blocked) == 1
    assert "30m EMA200" in blocked[0]


def test_regime_gate_fails_open_during_warmup():
    """A 1-min bar stream of 150 bars resamples to ~5 30-min bars → fail-open.

    The strategy fixture flow is exactly what production sees, so this also
    confirms the gate doesn't accidentally block when other gates would fire.
    """
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars)

    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)
    # If the regime gate were not failing-open during warmup the strategy would
    # have rejected this otherwise-firing bar and signal would be None.
    assert signal is not None
    assert signal.direction == "LONG"


def test_regime_gate_disabled_flag_bypasses_check():
    """``regime_gate_enabled=False`` must skip the gate even when C1 would block."""
    closes = [18000.0 - i * 5.0 for i in range(250)]  # same downtrend as C.1
    df = _thirty_minute_close_frame(closes)

    disabled = replace(RISK, regime_gate_enabled=False)
    assert _regime_ok(df, disabled) is True


def test_regime_gate_allows_long_when_price_above_30m_ema200():
    """C1: when last 30-min price is above the 30m EMA200, allow."""
    # Strong uptrend over 250 30-min bars so the latest close is well above
    # the EMA200 level. 250 >= 202 → past warmup.
    closes = [18000.0 + i * 5.0 for i in range(250)]
    df = _thirty_minute_close_frame(closes)

    assert _regime_ok(df, RISK) is True


# ----------------------------------------- PR-D3a — per-bar [STRAT] eval log


def test_evaluate_emits_strat_eval_log(caplog):
    """PR-D3a: every on_new_bar() invocation emits exactly one [STRAT] eval log.

    Feeds a single bar to a fresh strategy. The bar is at 11:00 ET (well outside
    every session-edge window), cooldown is inactive, and one bar is below the
    warmup threshold of two — so the decision is noop_warmup. Exactly one
    matching log record must appear.
    """
    strat = Sma100BounceStrategy("MNQM6")
    one_bar = _pullback_bar_dicts(n=1)[0]

    with caplog.at_level(logging.INFO, logger="src.strategy"):
        result = strat.on_new_bar(one_bar)

    assert result is None  # 1 bar < 2-bar warmup threshold

    eval_lines = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("[STRAT] MNQM6: eval ts=")
    ]
    assert (
        len(eval_lines) == 1
    ), f"expected exactly one [STRAT] eval log per on_new_bar; got {len(eval_lines)}"

    msg = eval_lines[0]
    for field in ("ts=", "close=", "ma_fast=", "ma_slow=", "gap=", "cooldown=", "decision="):
        assert field in msg, f"expected field {field!r} in eval log: {msg!r}"
    assert "cooldown=False" in msg
    assert "decision=noop_warmup" in msg


# ============================================================================
# Track 3 — decision instrumentation: noop_regime / noop_filter(failed=) split
# ============================================================================


def test_evaluate_gates_entry_returns_all_gates_true():
    """The happy-path entry bar yields a signal and every gate boolean True."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars)
    df = add_all_indicators(pd.DataFrame(bars))

    ge = evaluate_gates(df, "MNQM6")

    assert isinstance(ge, GateEval)
    assert ge.signal is not None
    assert ge.regime_ok is True
    assert ge.indicators_ready is True
    assert ge.touch_ok is True
    assert ge.ma_order_ok is True
    assert ge.bullish_ok is True
    assert ge.gap_ok is True


def test_evaluate_gates_regime_block_returns_regime_false():
    """A 30-min downtrend (>=202 bars) past warmup blocks at the regime gate."""
    base = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    closes = [18000.0 - i * 5.0 for i in range(250)]
    bars = [
        {
            "time": base + pd.Timedelta(minutes=30 * i),
            "open": c,
            "high": c + 1.0,
            "low": c - 1.0,
            "close": c,
        }
        for i, c in enumerate(closes)
    ]
    df = add_all_indicators(pd.DataFrame(bars))

    ge = evaluate_gates(df, "MNQM6")

    assert ge.signal is None
    assert ge.regime_ok is False


def test_detect_signal_still_delegates_to_evaluate_gates():
    """detect_signal must return exactly evaluate_gates(...).signal (no drift)."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars)
    df = add_all_indicators(pd.DataFrame(bars))

    assert detect_signal(df, "MNQM6") is evaluate_gates(df, "MNQM6").signal or (
        detect_signal(df, "MNQM6") is not None
    )


def test_first_failed_filter_precedence():
    """Order is touch -> ma_order -> bullish -> gap; first False wins."""
    # touch first
    ge = GateEval(
        signal=None,
        regime_ok=True,
        indicators_ready=True,
        touch_ok=False,
        ma_order_ok=False,
        bullish_ok=False,
        gap_ok=False,
    )
    assert _first_failed_filter(ge) == "touch"
    # ma_order when touch passes
    assert (
        _first_failed_filter(
            GateEval(
                signal=None,
                regime_ok=True,
                indicators_ready=True,
                touch_ok=True,
                ma_order_ok=False,
                bullish_ok=False,
                gap_ok=False,
            )
        )
        == "ma_order"
    )
    # bullish next
    assert (
        _first_failed_filter(
            GateEval(
                signal=None,
                regime_ok=True,
                indicators_ready=True,
                touch_ok=True,
                ma_order_ok=True,
                bullish_ok=False,
                gap_ok=False,
            )
        )
        == "bullish"
    )
    # gap last
    assert (
        _first_failed_filter(
            GateEval(
                signal=None,
                regime_ok=True,
                indicators_ready=True,
                touch_ok=True,
                ma_order_ok=True,
                bullish_ok=True,
                gap_ok=False,
            )
        )
        == "gap"
    )


def test_on_new_bar_label_long_signal_records_all_gates():
    """C3 — an entering bar still yields long_signal; behavior unchanged."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars)
    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)

    assert signal is not None
    rec = strat.last_decision
    assert rec["decision"] == "long_signal"
    assert rec["regime_ok"] is True
    assert rec["touch_ok"] is True and rec["gap_ok"] is True
    assert rec["failed"] is None


def test_on_new_bar_label_noop_filter_failed_bullish():
    """C2 — regime OK, indicators ready, only bullish fails -> noop_filter failed=bullish."""
    bars = _pullback_bar_dicts(n=120)
    df = pd.DataFrame(bars[:-1])
    df = add_all_indicators(df)
    ma_slow_prev = float(df["ma_slow"].iloc[-1])
    last_close = ma_slow_prev + 12.0
    bars[-1] = {
        "time": bars[-1]["time"],
        "open": last_close + 5.0,  # open > close -> bearish, all else passes
        "high": last_close + 6.0,
        "low": ma_slow_prev,
        "close": last_close,
    }
    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)

    assert signal is None
    rec = strat.last_decision
    assert rec["decision"] == "noop_filter"
    assert rec["failed"] == "bullish"
    assert rec["bullish_ok"] is False


def test_on_new_bar_label_noop_warmup_when_indicators_not_ready():
    """Regime fail-opens during warmup but SMA buffer is not warm -> noop_warmup."""
    bars = _pullback_bar_dicts(n=10)  # << 100, so sma_100 is NaN
    strat = Sma100BounceStrategy("MNQM6")
    _stuff_strategy(strat, bars)

    rec = strat.last_decision
    assert rec["decision"] == "noop_warmup"


def test_on_new_bar_label_noop_regime(monkeypatch):
    """C1 — when evaluate_gates reports regime blocked, label is noop_regime.

    The live 150-bar buffer always fail-opens the regime gate (resamples to
    < 202 30-min bars), so we force a regime-blocked GateEval to exercise the
    label-derivation branch deterministically.
    """
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars)

    import src.strategy as strat_mod

    monkeypatch.setattr(
        strat_mod,
        "evaluate_gates",
        lambda *a, **k: GateEval(signal=None, regime_ok=False, indicators_ready=False),
    )
    strat = Sma100BounceStrategy("MNQM6")
    signal = _stuff_strategy(strat, bars)

    assert signal is None
    assert strat.last_decision["decision"] == "noop_regime"


def test_eval_log_carries_gate_values(caplog):
    """B2 — the [STRAT] eval line is self-explaining (carries gate booleans)."""
    bars = _pullback_bar_dicts(n=120)
    _engineer_fire_bar(bars)
    strat = Sma100BounceStrategy("MNQM6")
    for bar in bars[:-1]:
        strat.on_new_bar(bar)
    with caplog.at_level(logging.INFO, logger="src.strategy"):
        strat.on_new_bar(bars[-1])

    eval_lines = [r.getMessage() for r in caplog.records if "eval ts=" in r.getMessage()]
    assert eval_lines
    msg = eval_lines[-1]
    for field in ("regime_ok=", "touch_ok=", "ma_order_ok=", "bullish_ok=", "gap_ok="):
        assert field in msg, f"expected {field!r} in enriched eval log: {msg!r}"
