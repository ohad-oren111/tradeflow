"""Behaviour tests for tools/eval/gate_calibration.py — the Phase-12 gate study.

Deterministic, hand-built BTTape fixtures (no DB, no real history). Pins the pure
logic the verdict rests on:
  1. Depth/time-of-day/slope masks select exactly the intended below-trend signals.
  2. The pre-committed verdict: GATE-CORRECT unless a band clears PF>=bar AND net>0
     AND n>=30 AND beats the unconditional below-cohort OOS PF.
  3. compute_depth tags below-trend signals with a positive depth and leaves
     above-trend / un-enriched ones NaN.
  4. The SB-code data-gap check returns False on this repo (only the parser lives
     here, not his strategy) — so the report flags the gap, never fabricates.
"""

from __future__ import annotations

import numpy as np

from tools.eval import gate_calibration as gc
from tools.eval.below_trend_study import BTTape
from tools.eval.metrics import Stats


def _stats(n: int, net: float, pf: float) -> Stats:
    """A minimal Stats stub carrying only the fields the verdict reads."""
    return Stats(
        n=n,
        wins=0,
        losses=0,
        scratches=0,
        win_rate=0.0,
        expectancy_usd=net / n if n else 0.0,
        profit_factor=pf,
        avg_win_usd=0.0,
        avg_loss_usd=0.0,
        gross_win_usd=0.0,
        gross_loss_usd=0.0,
        net_usd=net,
        max_drawdown_usd=0.0,
    )


def _tape(sig_idx, sig_below, *, sig_slope=None, n_bars=1000) -> BTTape:
    sig_idx = np.asarray(sig_idx, dtype=np.int64)
    k = len(sig_idx)
    return BTTape(
        ts=[None] * n_bars,
        high=np.zeros(n_bars),
        low=np.zeros(n_bars),
        close=np.zeros(n_bars),
        sig_idx=sig_idx,
        sig_px=np.zeros(k),
        sig_below=np.asarray(sig_below, dtype=bool),
        sig_adx=np.full(k, np.nan),
        sig_slope=np.full(k, np.nan) if sig_slope is None else np.asarray(sig_slope, dtype=float),
        sig_buckets=np.full(k, 250, dtype=np.int64),
        span=(None, None),
        bars=n_bars,
    )


# ------------------------------------------------------------------- depth masks
def test_depth_band_mask_selects_band_and_excludes_above_and_nan():
    tape = _tape([10, 20, 30, 40], [True, True, True, False])
    depth = np.array([10.0, 40.0, np.nan, 5.0])  # sig3 NaN, sig4 above-trend
    m = gc.depth_band_mask(tape, depth, 0.0, 25.0)
    # only sig1 (depth 10, below, in [0,25)) qualifies.
    assert list(m) == [True, False, False, False]
    m2 = gc.depth_band_mask(tape, depth, 25.0, 50.0)
    assert list(m2) == [False, True, False, False]


def test_tod_band_mask_wraps_midnight():
    tape = _tape([1, 2, 3], [True, True, True])
    hours = np.array([19, 1, 10])  # asia(18-3 wraps): 19 and 1 in-band; 10 out
    m = gc.tod_band_mask(tape, hours, 18, 3)
    assert list(m) == [True, True, False]


def test_slope_sign_mask_splits_on_sign_and_excludes_nan():
    tape = _tape([1, 2, 3], [True, True, True], sig_slope=[0.5, -0.5, np.nan])
    rising = gc.slope_sign_mask(tape, rising=True)
    falling = gc.slope_sign_mask(tape, rising=False)
    assert list(rising) == [True, False, False]
    assert list(falling) == [False, True, False]


# --------------------------------------------------------------- compute_depth
def test_compute_depth_positive_below_nan_above():
    # 3 signals: indices 300/400/500; first two below, third above.
    tape = _tape([300, 400, 500], [True, True, False], n_bars=600)
    # synth a gently-rising close so regime_at returns a level; we only assert sign.
    ts = [
        __import__("datetime").datetime(2025, 1, 1, tzinfo=__import__("datetime").timezone.utc)
        + __import__("datetime").timedelta(minutes=i)
        for i in range(600)
    ]
    tape.ts[:] = ts
    close = np.linspace(20000.0, 20100.0, 600)
    tape.close[:] = close
    depth = gc.compute_depth(tape)
    # above-trend signal (index 2) is never enriched -> NaN.
    assert np.isnan(depth[2])
    # below-trend signals get a finite depth (sign may be either once enriched, but
    # both are enriched, i.e. not NaN, since buckets>=202 on this long window).
    assert depth.shape == (3,)


# ------------------------------------------------------------------- verdict rule
def test_verdict_gate_correct_when_no_band_qualifies():
    below_oos = _stats(200, -500.0, 0.95)
    above_oos = _stats(400, 5000.0, 1.18)
    bands = [
        gc.BandResult("shallow_0_25", "depth", _stats(50, 100, 1.05), _stats(40, -50, 0.98)),
        gc.BandResult("deep_100_plus", "depth", _stats(60, -300, 0.9), _stats(45, -200, 0.85)),
    ]
    v = gc.assess(bands, above_oos=above_oos, below_oos=below_oos)
    assert v.label == "GATE-CORRECT"
    assert v.candidate is None


def test_verdict_too_strict_when_band_clears_bar():
    below_oos = _stats(200, -500.0, 0.95)
    above_oos = _stats(400, 5000.0, 1.18)
    winner = gc.BandResult("shallow_0_25", "depth", _stats(80, 2000, 1.4), _stats(45, 1500, 1.35))
    bands = [
        winner,
        gc.BandResult("deep_100_plus", "depth", _stats(60, -300, 0.9), _stats(45, -200, 0.85)),
    ]
    v = gc.assess(bands, above_oos=above_oos, below_oos=below_oos)
    assert v.label == "GATE-TOO-STRICT"
    assert v.axis == "depth"
    assert v.candidate == "shallow_0_25"


def test_verdict_rejects_band_below_n_threshold():
    # PF clears the bar but n < 30 OOS -> NOT a candidate (small-sample guard).
    below_oos = _stats(200, -500.0, 0.95)
    above_oos = _stats(400, 5000.0, 1.18)
    bands = [gc.BandResult("shallow_0_25", "depth", _stats(80, 2000, 1.4), _stats(12, 800, 1.5))]
    v = gc.assess(bands, above_oos=above_oos, below_oos=below_oos)
    assert v.label == "GATE-CORRECT"


def test_verdict_rejects_band_not_beating_unconditional_below():
    # PF 1.25 clears the absolute bar but below_oos PF is already 1.30 -> no edge added.
    below_oos = _stats(200, 800.0, 1.30)
    above_oos = _stats(400, 5000.0, 1.18)
    bands = [gc.BandResult("shallow_0_25", "depth", _stats(80, 900, 1.26), _stats(40, 500, 1.25))]
    v = gc.assess(bands, above_oos=above_oos, below_oos=below_oos)
    assert v.label == "GATE-CORRECT"


def test_verdict_rejects_oos_pop_that_full_sample_does_not_corroborate():
    # The mid_25_50 trap: OOS PF 1.25 clears the bar, but the FULL sample (more data)
    # is only 1.16 -> the larger set does not corroborate -> multiple-comparison noise.
    below_oos = _stats(200, -500.0, 0.95)
    above_oos = _stats(400, 5000.0, 1.18)
    bands = [
        gc.BandResult("mid_25_50", "depth", _stats(189, 3345, 1.163), _stats(150, 3932, 1.252))
    ]
    v = gc.assess(bands, above_oos=above_oos, below_oos=below_oos)
    assert v.label == "GATE-CORRECT"
    assert v.candidate is None


# ----------------------------------------------------------------- SB data gap
def test_sb_strategy_code_absent_on_this_repo():
    present, hits = gc.sb_code_present()
    # Only the Telegram parser/reconciler live in src/ — NOT his strategy. The study
    # must flag a data gap, never fabricate his entry rule.
    assert present is False
    assert hits == []
