"""CI-safe invariant tests for the Phase-16 discovery harness.

All synthetic (hand-built tapes) — no dependency on the saved 1-min csv or any prod
path. Pins the things a wrong number would hide: the cost/slippage math, the
pessimistic-fill ordering (stop-before-target, gap-through-at-open, next-bar-open
entry = no lookahead), the exit primitives, the deflated-Sharpe / multiple-testing
deflators, the gate logic, and the fixed calendar split boundaries.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.eval.phase16.costs import CostModel, Slippage
from tools.eval.phase16.engine import ExitSpec, run_backtest
from tools.eval.phase16.gates import check_holdout, check_train
from tools.eval.phase16.metrics import (
    Stats,
    bonferroni_haircut,
    compute_stats,
    deflated_sharpe,
    expected_max_sharpe,
    norm_cdf,
    norm_ppf,
)


def _tape(opens, highs, lows, closes, *, start="2025-01-02 15:00"):
    n = len(opens)
    t = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "time": t,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1] * n,
            "year": [2025] * n,
            "rth": [True] * n,
            "rth_date": [pd.Timestamp("2025-01-02")] * n,
        }
    )


# --------------------------------------------------------------------------- #
# Cost / slippage
# --------------------------------------------------------------------------- #
def test_slippage_adverse_direction():
    s = Slippage()  # entry 1t, target 0.5t, tick 0.25
    assert s.adjust_entry(100.0, 1) == pytest.approx(100.25)  # long pays up
    assert s.adjust_entry(100.0, -1) == pytest.approx(99.75)  # short pays down
    # long exit (sell) receives less; target = 0.5 tick = 0.125
    assert s.adjust_exit(100.0, 1, "target") == pytest.approx(99.875)
    assert s.adjust_exit(100.0, -1, "target") == pytest.approx(100.125)
    assert s.adjust_exit(100.0, 1, "stop") == pytest.approx(99.75)  # 1 tick


def test_net_pnl_commission_charged_both_sides():
    c = CostModel()
    # long 100 -> 110, 1 ct: 10pt * $2 = $20 gross - $1.24 commission
    assert c.net_pnl_usd(100.0, 110.0, 1, 1) == pytest.approx(20.0 - 1.24)
    # short 110 -> 100: +10pt
    assert c.net_pnl_usd(110.0, 100.0, -1, 1) == pytest.approx(20.0 - 1.24)


def test_uniform_slippage_zero_is_frictionless_price():
    s = Slippage.uniform(0.0)
    assert s.adjust_entry(100.0, 1) == 100.0
    assert s.adjust_exit(100.0, 1, "stop") == 100.0


# --------------------------------------------------------------------------- #
# Engine — pessimistic fills
# --------------------------------------------------------------------------- #
def test_entry_is_next_bar_open_no_lookahead():
    # signal set on bar index 2 -> entry must be at bar index 3 open
    df = _tape(
        [10, 10, 10, 50, 50, 50],
        [10, 10, 10, 50, 60, 50],
        [10, 10, 10, 50, 50, 50],
        [10, 10, 10, 50, 50, 50],
    )
    pos = np.zeros(6)
    pos[2] = 1
    tr = run_backtest(
        df, pos, ExitSpec(stop_pts=100, target_pts=5, cooldown_bars=1), slip=Slippage.uniform(0)
    )
    assert len(tr) == 1
    assert tr.iloc[0]["entry_px"] == 50  # bar-3 open, frictionless


def test_stop_checked_before_target_same_bar():
    # entry bar1 (open 100); bar2 has BOTH stop (low<=90) and target (high>=110) -> STOP wins
    df = _tape(
        [100, 100, 100, 100], [100, 100, 130, 100], [100, 100, 70, 100], [100, 100, 100, 100]
    )
    pos = np.zeros(4)
    pos[0] = 1
    tr = run_backtest(
        df, pos, ExitSpec(stop_pts=10, target_pts=10, cooldown_bars=1), slip=Slippage.uniform(0)
    )
    assert tr.iloc[0]["exit_reason"] == "stop"
    assert tr.iloc[0]["exit_px"] == pytest.approx(90.0)  # entry100 - 10


def test_gap_through_stop_fills_at_open():
    # entry bar1 (open 100, stop 90); bar2 GAPS open to 80 (below the stop) -> fill at 80
    df = _tape([100, 100, 80, 80], [100, 100, 85, 85], [100, 100, 78, 78], [100, 100, 80, 80])
    pos = np.zeros(4)
    pos[0] = 1
    tr = run_backtest(df, pos, ExitSpec(stop_pts=10, cooldown_bars=1), slip=Slippage.uniform(0))
    assert tr.iloc[0]["exit_reason"] == "stop"
    assert tr.iloc[0]["exit_px"] == pytest.approx(80.0)  # gap-through at open, worse than 90


def test_short_trade_symmetry():
    # short at 100, target 10 down -> exit ~90
    df = _tape([100, 100, 90], [100, 100, 92], [100, 95, 88], [100, 96, 90])
    pos = np.zeros(3)
    pos[0] = -1
    tr = run_backtest(
        df, pos, ExitSpec(stop_pts=20, target_pts=10, cooldown_bars=1), slip=Slippage.uniform(0)
    )
    assert tr.iloc[0]["direction"] == -1
    assert tr.iloc[0]["exit_reason"] == "target"
    assert tr.iloc[0]["pnl_usd"] > 0


def test_exit_on_flip():
    # persistent long signal that flips to -1 -> exit at the flip bar's open (market)
    df = _tape([100] * 5, [101] * 5, [99] * 5, [100] * 5)
    pos = np.array([1, 1, -1, -1, -1.0])
    tr = run_backtest(
        df,
        pos,
        ExitSpec(stop_pts=100, exit_on_flip=True, cooldown_bars=1),
        slip=Slippage.uniform(0),
    )
    # entered bar1 (from pos[0]=1), pos[2]=-1 -> flip exit at bar3 open
    assert tr.iloc[0]["exit_reason"] == "flip"


def test_time_stop():
    df = _tape([100] * 8, [101] * 8, [99] * 8, [100] * 8)
    pos = np.zeros(8)
    pos[0] = 1
    tr = run_backtest(
        df, pos, ExitSpec(stop_pts=100, max_hold_bars=3, cooldown_bars=1), slip=Slippage.uniform(0)
    )
    assert tr.iloc[0]["exit_reason"] == "time"
    assert tr.iloc[0]["bars_held"] == 3


def test_meanrev_target_col():
    # long entered below the band; exit when price reaches the live mean column
    df = _tape([90, 90, 95, 100], [90, 92, 96, 101], [90, 89, 94, 99], [90, 90, 95, 100])
    df["bb_mid"] = [100, 100, 100, 100]
    pos = np.zeros(4)
    pos[0] = 1
    tr = run_backtest(
        df,
        pos,
        ExitSpec(stop_pts=50, target_col="bb_mid", cooldown_bars=1),
        slip=Slippage.uniform(0),
    )
    assert tr.iloc[0]["exit_reason"] == "meanrev"
    assert tr.iloc[0]["exit_px"] == pytest.approx(100.0)


def test_session_close_flat():
    df = _tape([100] * 4, [101] * 4, [99] * 4, [100] * 4)
    df["rth"] = [True, True, True, True]
    pos = np.zeros(4)
    pos[0] = 1
    tr = run_backtest(
        df,
        pos,
        ExitSpec(stop_pts=100, intraday_only=True, cooldown_bars=1),
        slip=Slippage.uniform(0),
    )
    # last RTH bar of the (single) session forces a flat
    assert tr.iloc[0]["exit_reason"] == "session_close"


# --------------------------------------------------------------------------- #
# Metrics + deflators
# --------------------------------------------------------------------------- #
def test_norm_cdf_ppf_roundtrip():
    for p in (0.01, 0.1, 0.5, 0.9, 0.99):
        assert norm_cdf(norm_ppf(p)) == pytest.approx(p, abs=1e-6)
    assert norm_cdf(0.0) == pytest.approx(0.5)


def test_expected_max_sharpe_grows_with_trials():
    v = 0.01
    e3 = expected_max_sharpe(3, v)
    e40 = expected_max_sharpe(40, v)
    assert e40 > e3 > 0  # more trials -> higher expected max under the null


def test_deflated_sharpe_drops_with_more_trials():
    # same observed Sharpe deflates HARDER after more trials
    dsr3, sr0_3 = deflated_sharpe(0.1, 500, 0.0, 3.0, 3, 0.01)
    dsr40, sr0_40 = deflated_sharpe(0.1, 500, 0.0, 3.0, 40, 0.01)
    assert sr0_40 > sr0_3
    assert dsr40 < dsr3


def test_bonferroni_haircut_inflates_p():
    p, p_adj = bonferroni_haircut(0.1, 400, 40)
    assert p_adj >= p
    assert p_adj == pytest.approx(min(1.0, p * 40))


def test_compute_stats_basic():
    trades = pd.DataFrame(
        {
            "pnl_usd": [10.0, -5.0, 20.0, -5.0],
            "exit_time": pd.date_range("2025-01-02", periods=4, freq="1D", tz="UTC"),
            "year": [2025, 2025, 2025, 2025],
            "bars_held": [1, 1, 1, 1],
        }
    )
    s = compute_stats(trades)
    assert s.n_trades == 4
    assert s.profit_factor == pytest.approx(30.0 / 10.0)
    assert s.expectancy_usd == pytest.approx(5.0)
    assert s.win_rate == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def _stats(pf, exp, n, years):
    return Stats(n, 0.5, pf, exp, exp * n, 1.0, 0.05, 0.0, 3.0, -100.0, years, {})


def test_train_gate_requires_all():
    good = _stats(1.4, 7.0, 250, {2024: 100.0, 2025: 50.0})
    assert check_train(good).passed
    assert not check_train(_stats(1.2, 7.0, 250, {2024: 1.0})).passed  # PF low
    assert not check_train(_stats(1.4, 3.0, 250, {2024: 1.0})).passed  # exp low
    assert not check_train(_stats(1.4, 7.0, 100, {2024: 1.0})).passed  # n low
    assert not check_train(_stats(1.4, 7.0, 250, {2024: -1.0})).passed  # a losing year


def test_holdout_gate_degradation_cap_and_dsr():
    train = _stats(1.6, 8.0, 300, {2024: 1.0, 2025: 1.0})
    ok = _stats(1.5, 8.0, 200, {2025: 1.0, 2026: 1.0})
    assert check_holdout(train, ok, dsr=0.99).passed
    # holdout PF collapses below 0.75*train -> fail even with good DSR
    bad = _stats(1.05, 8.0, 200, {2025: 1.0, 2026: 1.0})
    assert not check_holdout(train, bad, dsr=0.99).passed
    # good holdout but DSR below bar -> fail
    assert not check_holdout(train, ok, dsr=0.5).passed
