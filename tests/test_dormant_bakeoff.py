"""Behaviour + math tests for tools/eval/dormant_bakeoff.py (Phase-15 research).

The bake-off has no production path to anchor against, so its credibility rests on
(a) the COST math being exactly the brief's spec, (b) the candidate state machines
firing on a controlled tape that forces each path, and (c) the verdict logic being
the literal go/no-go bar. We prove each on hand-built fixtures (CI-runnable; the real
27-mo CSV is not needed for these). For the entry/exit machines we INJECT the
indicator columns (sma_100/atr) and a ready Regime30m so the test controls the exact
geometry instead of fighting a 6,060-bar regime warmup — run_candidate* read those
columns directly (they do not recompute them), so the injection is faithful.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from tools.eval.dormant_bakeoff import (
    COMMISSION_PER_SIDE,
    MULT,
    TICK,
    BakeTrade,
    Regime30m,
    _rsi,
    _single_year_dependence,
    _verdict,
    build_15m,
    build_regime_30m,
    run_candidate1,
    run_candidate2,
    stats,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _frame(rows: list[dict], *, start: datetime, sma100=None, atr=None) -> pd.DataFrame:
    """Build a 1-min frame from explicit OHLC dicts (keys o/h/l/c, optional v).
    Optionally inject constant sma_100 / atr columns (what run_candidate1 reads)."""
    n = len(rows)
    df = pd.DataFrame(
        {
            "time": [start + timedelta(minutes=i) for i in range(n)],
            "open": [r["o"] for r in rows],
            "high": [r["h"] for r in rows],
            "low": [r["l"] for r in rows],
            "close": [r["c"] for r in rows],
            "volume": [r.get("v", 10.0) for r in rows],
        }
    )
    df["time"] = pd.to_datetime(df["time"], utc=True)
    if sma100 is not None:
        df["sma_100"] = float(sma100)
    if atr is not None:
        df["atr"] = float(atr)
    return df


def _all_valid_regime(n: int, *, ema: float, falling: bool, below: bool) -> Regime30m:
    """A ready Regime30m of length n: every bar armed; bclose set above/below ema."""
    bclose = ema - 50.0 if below else ema + 50.0
    return Regime30m(
        ema=np.full(n, ema),
        bclose=np.full(n, bclose),
        falling=np.full(n, falling, dtype=bool),
        valid=np.ones(n, dtype=bool),
    )


# --------------------------------------------------------------------------- #
# COST MODEL — exactly the brief spec ($0.31/side + 1-tick / 2-tick conditional)
# --------------------------------------------------------------------------- #
def _trade(gross_pts, *, side="short", er=10.0, era=5.0, xr=10.0, xra=5.0):
    """A bare BakeTrade with controllable fill-bar ranges/atr for slippage tests."""
    t0 = datetime(2025, 1, 7, 15, 0, tzinfo=UTC)
    return BakeTrade(
        cand="t",
        side=side,
        entry_ts=t0,
        exit_ts=t0 + timedelta(minutes=5),
        entry_px=100.0,
        exit_px=100.0 - gross_pts if side == "short" else 100.0 + gross_pts,
        gross_pts=gross_pts,
        init_risk_pts=5.0,
        exit_reason="x",
        depth_pts=10.0,
        entry_range=er,
        entry_atr1m=era,
        exit_range=xr,
        exit_atr1m=xra,
    )


def test_cost_commission_both_sides_zero_slippage():
    # fixed 0-tick slippage: net == gross*MULT - 2*commission_per_side.
    t = _trade(10.0)
    expected = 10.0 * MULT - 2.0 * COMMISSION_PER_SIDE
    assert abs(t.net_usd(fixed_ticks=0) - expected) < 1e-9


def test_cost_fixed_tick_slippage():
    # fixed 2-tick/side: subtract (2+2)*TICK points before the multiplier.
    t = _trade(10.0)
    expected = (10.0 - 4 * TICK) * MULT - 2.0 * COMMISSION_PER_SIDE
    assert abs(t.net_usd(fixed_ticks=2) - expected) < 1e-9


def test_cost_conditional_slippage_calm_vs_capitulation():
    # calm: both fill bars range <= 2*ATR -> 1 tick/side (2 ticks round trip).
    calm = _trade(10.0, er=5.0, era=5.0, xr=5.0, xra=5.0)  # range 5 < 2*5=10
    calm_exp = (10.0 - 2 * TICK) * MULT - 2.0 * COMMISSION_PER_SIDE
    assert abs(calm.net_usd() - calm_exp) < 1e-9
    # capitulation: BOTH fill bars range > 2*ATR -> 2 ticks/side (4 ticks RT).
    wild = _trade(10.0, er=21.0, era=5.0, xr=21.0, xra=5.0)  # range 21 > 2*5=10
    wild_exp = (10.0 - 4 * TICK) * MULT - 2.0 * COMMISSION_PER_SIDE
    assert abs(wild.net_usd() - wild_exp) < 1e-9
    # the conditional model must equal the 1-tick fixed model when calm.
    assert abs(calm.net_usd() - calm.net_usd(fixed_ticks=1)) < 1e-9


def test_cost_commission_variant():
    t = _trade(10.0)
    base = t.net_usd(fixed_ticks=0)
    hi = t.net_usd(fixed_ticks=0, commission_per_side=0.62)
    # higher commission must reduce net by exactly 2*(0.62-0.31).
    assert abs((base - hi) - 2 * (0.62 - 0.31)) < 1e-9


def test_realized_r_short_stop_is_minus_one_target_is_plus_two():
    # init_risk_pts = 5; a full stop loss for a short = -5 pts gross -> -1R.
    loss = _trade(-5.0)
    assert abs(loss.realized_r - (-1.0)) < 1e-9
    win = _trade(10.0)  # +10 pts on 5-pt risk -> +2R
    assert abs(win.realized_r - 2.0) < 1e-9


# --------------------------------------------------------------------------- #
# 30m REGIME precompute — matches an independent resample+ewm, causal + warmup
# --------------------------------------------------------------------------- #
def test_build_regime_30m_matches_recompute_and_warmup():
    # 6,300 1-min bars declining slowly -> >202 30m buckets, falling EMA.
    n = 6300
    start = datetime(2025, 1, 6, 0, 0, tzinfo=UTC)
    closes = 20000.0 - np.arange(n) * 0.05
    df = pd.DataFrame(
        {
            "time": pd.to_datetime([start + timedelta(minutes=i) for i in range(n)], utc=True),
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "volume": 10.0,
        }
    )
    reg = build_regime_30m(df)

    # independent recompute (the gate's exact resample + EMA).
    s = df.set_index("time")["close"]
    b30 = s.resample("30min").last().dropna()
    ema = b30.ewm(span=200, adjust=False).mean().to_numpy()
    end_ns = (b30.index + pd.Timedelta(minutes=30)).asi8
    bar_ns = pd.DatetimeIndex(df["time"]).asi8

    # warmup: the first bar maps to no completed bucket -> invalid, NaN ema.
    assert not reg.valid[0]
    assert np.isnan(reg.ema[0])
    # a late bar (well past 202 buckets) is valid and equals the completed-bucket EMA.
    j = n - 1
    pos = int(np.searchsorted(end_ns, bar_ns[j], side="right")) - 1
    assert reg.valid[j]
    assert abs(reg.ema[j] - ema[pos]) < 1e-6
    # falling: a strictly declining series -> EMA falling where valid.
    assert reg.falling[j]
    # valid must turn on no earlier than the 202nd completed bucket's end.
    first_valid = int(np.argmax(reg.valid))
    assert reg.valid[first_valid]
    assert int(np.searchsorted(end_ns, bar_ns[first_valid], side="right")) - 1 >= 201


# --------------------------------------------------------------------------- #
# RSI(2) + Bollinger lower (candidate-2 building blocks)
# --------------------------------------------------------------------------- #
def test_rsi2_all_gains_is_100_all_losses_is_0():
    up = pd.Series([10.0, 11, 12, 13, 14, 15])
    down = pd.Series([15.0, 14, 13, 12, 11, 10])
    assert _rsi(up, 2).iloc[-1] > 99.0
    assert _rsi(down, 2).iloc[-1] < 1.0


def test_build_15m_aggregates_and_bb_lower():
    # 30 flat-ish 15m buckets then nothing fancy: assert OHLC roll-up + BB present.
    start = datetime(2025, 4, 1, 13, 30, tzinfo=UTC)
    rows = []
    for b in range(30):
        for _k in range(15):
            px = 1000.0 - b  # gentle decline, one level per bucket
            rows.append({"o": px, "h": px + 0.5, "l": px - 0.5, "c": px})
    df = _frame(rows, start=start)
    g = build_15m(df)
    assert len(g) == 30
    # bucket 0 close == last 1-min close of that bucket (= its level).
    assert abs(g["close"].iloc[0] - 1000.0) < 1e-9
    # BB lower is NaN until 20 buckets are seen, then finite.
    assert np.isnan(g["bb_lower"].iloc[18])
    assert not np.isnan(g["bb_lower"].iloc[20])


# --------------------------------------------------------------------------- #
# CANDIDATE 1 — forced short: stretch -> pullback -> confirm -> target (+2R)
# --------------------------------------------------------------------------- #
def _cand1_rows():
    # sma_100=100, atr=10 injected: band=[99,102.5], stretch<=92.5, 0.25*atr=2.5.
    rows = [{"o": 95, "h": 96, "l": 94, "c": 95} for _ in range(5)]
    rows.append({"o": 95, "h": 91, "l": 89, "c": 90})  # 5: stretch (close 90 <= 92.5)
    rows += [{"o": 95, "h": 96, "l": 94, "c": 95} for _ in range(5)]  # 6..10 (high<99)
    rows.append({"o": 95, "h": 101, "l": 98, "c": 99.5})  # 11: pullback (pb_low98 pb_high101)
    rows.append({"o": 99.5, "h": 98, "l": 96, "c": 97})  # 12: confirm (close97<98 and <100)
    rows.append({"o": 96, "h": 97, "l": 95, "c": 96})  # 13: ENTRY at open 96
    rows.append({"o": 96, "h": 96, "l": 80, "c": 82})  # 14: low 80 <= target 81 -> target
    rows += [{"o": 90, "h": 91, "l": 89, "c": 90} for _ in range(4)]
    return rows


def test_candidate1_forces_short_target_at_plus_2r():
    start = datetime(2025, 4, 1, 14, 0, tzinfo=UTC)  # a Tuesday (no session-flat)
    df = _frame(_cand1_rows(), start=start, sma100=100.0, atr=10.0)
    reg = _all_valid_regime(len(df), ema=200.0, falling=True, below=True)
    trades = run_candidate1(df, reg)
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "short"
    assert t.exit_reason == "target"
    assert abs(t.entry_px - 96.0) < 1e-9
    assert abs(t.init_risk_pts - 7.5) < 1e-9  # stop 103.5 - entry 96
    assert abs(t.exit_px - 81.0) < 1e-9  # target = entry - 2R = 96 - 15
    assert abs(t.gross_pts - 15.0) < 1e-9  # short profit = entry - exit
    assert abs(t.realized_r - 2.0) < 1e-9


def test_candidate1_no_short_without_falling_regime():
    # same tape, but EMA not falling -> the regime condition blocks every entry.
    start = datetime(2025, 4, 1, 14, 0, tzinfo=UTC)
    df = _frame(_cand1_rows(), start=start, sma100=100.0, atr=10.0)
    reg = _all_valid_regime(len(df), ema=200.0, falling=False, below=True)
    assert run_candidate1(df, reg) == []


def test_candidate1_rejects_out_of_band_risk():
    # widen the stop far above entry so R > 1.5*ATR (=15) -> rejected, no trade.
    rows = _cand1_rows()
    rows[11] = {"o": 95, "h": 130, "l": 98, "c": 99.5}  # pullback high 130 -> stop ~132.5
    start = datetime(2025, 4, 1, 14, 0, tzinfo=UTC)
    df = _frame(rows, start=start, sma100=100.0, atr=10.0)
    reg = _all_valid_regime(len(df), ema=200.0, falling=True, below=True)
    # R = 132.5 - 96 = 36.5 > 15 -> reject; no other pullback fires -> 0 trades.
    assert run_candidate1(df, reg) == []


# --------------------------------------------------------------------------- #
# CANDIDATE 2 — forced capitulation long: cliff bucket triggers BB+RSI, target hit
# --------------------------------------------------------------------------- #
def _cand2_rows():
    # 21 gentle-decline buckets (RSI2~0 throughout) + a deep cliff bucket that breaks
    # the lower Bollinger band, then rising 1-min bars that hit the long target.
    rows: list[dict] = []
    for b in range(21):
        lvl = 1000.0 - 0.5 * b
        for _ in range(15):
            rows.append({"o": lvl, "h": lvl + 0.3, "l": lvl - 0.3, "c": lvl})
    # cliff bucket (index 21): collapse to 960 -> well below the 2.5σ lower band.
    for _ in range(15):
        rows.append({"o": 960.0, "h": 990.0, "l": 958.0, "c": 960.0})
    # post-entry 1-min bars: rip UP so the long target is hit and the stop never is.
    rows += [{"o": 960.0, "h": 1500.0, "l": 959.0, "c": 1200.0} for _ in range(6)]
    return rows


def test_candidate2_forces_capitulation_long_target():
    start = datetime(2025, 4, 1, 13, 30, tzinfo=UTC)  # 09:30 EDT; cliff closes ~15:00 ET
    df = _frame(_cand2_rows(), start=start, atr=2.0)
    reg = _all_valid_regime(len(df), ema=100000.0, falling=False, below=True)  # 1m close < EMA
    trades = run_candidate2(df, reg)
    assert len(trades) >= 1
    t = trades[0]
    assert t.side == "long"
    assert t.exit_reason == "target"
    assert t.exit_px > t.entry_px  # long winner
    assert t.init_risk_pts > 0  # = 1.5 * ATR14(15m)


def test_candidate2_no_signal_when_above_trend():
    # regime says 1m close NOT below EMA (ema tiny) -> no capitulation longs allowed.
    start = datetime(2025, 4, 1, 13, 30, tzinfo=UTC)
    df = _frame(_cand2_rows(), start=start, atr=2.0)
    reg = Regime30m(
        ema=np.full(len(df), 1.0),
        bclose=np.full(len(df), 2.0),
        falling=np.zeros(len(df), bool),
        valid=np.ones(len(df), bool),
    )
    assert run_candidate2(df, reg) == []


# --------------------------------------------------------------------------- #
# VERDICT + single-year dependence
# --------------------------------------------------------------------------- #
def _wintrade(year, pnl_pts):
    t0 = datetime(year, 6, 2, 15, 0, tzinfo=UTC)
    return BakeTrade(
        cand="v",
        side="long",
        entry_ts=t0,
        exit_ts=t0 + timedelta(minutes=5),
        entry_px=100.0,
        exit_px=100.0 + pnl_pts,
        gross_pts=pnl_pts,
        init_risk_pts=10.0,
        exit_reason="target" if pnl_pts > 0 else "stop",
        depth_pts=10.0,
        entry_range=1.0,
        entry_atr1m=5.0,
        exit_range=1.0,
        exit_atr1m=5.0,
    )


def test_verdict_fail_on_empty():
    passed, text = _verdict([])
    assert not passed
    assert "NO TRADES" in text


def test_single_year_dependence_flags_one_carrying_year():
    # 2024 huge net, 2025 net negative -> dropping 2024 makes the rest <= 0 -> dependent.
    trades = [_wintrade(2024, 50.0) for _ in range(10)] + [_wintrade(2025, -5.0) for _ in range(5)]
    s = stats(trades)
    years = {
        2024: stats([t for t in trades if t.year == 2024]),
        2025: stats([t for t in trades if t.year == 2025]),
    }
    dep, _note = _single_year_dependence(years, s)
    assert dep is True


def test_single_year_dependence_clears_when_spread():
    trades = [_wintrade(2024, 30.0) for _ in range(5)] + [_wintrade(2025, 30.0) for _ in range(5)]
    s = stats(trades)
    years = {
        2024: stats([t for t in trades if t.year == 2024]),
        2025: stats([t for t in trades if t.year == 2025]),
    }
    dep, _note = _single_year_dependence(years, s)
    assert dep is False
