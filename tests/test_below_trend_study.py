"""Fidelity + behaviour tests for tools/eval/below_trend_study.py.

The study's speed trick is the precompute-once tagged tape + cheap ``replay_masked``.
That is only trustworthy if ``replay_masked`` (all signals eligible, TF-current exit)
reproduces the real ``engine.simulate_segment`` exactly — the same invariant the full
run asserts with ``--validate`` on real data. We also pin the +50-TP what-if cap and
the partition-mask algebra so a refactor can't silently change the research result.

Synthetic data keeps this CI-runnable (the real 26-mo CSV is not in CI). Short
synthetic segments never reach 202 30-min buckets, so the regime gate fail-opens and
``sig_below`` is all-False there — the masks are exercised for algebra, not regime
calibration (that is covered by the live ``--validate`` run).
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from tools.eval import data, engine
from tools.eval.below_trend_study import (
    SB_FAST,
    TF_CURRENT,
    TP_WHATIF_PTS,
    build_bt_tape,
    cfg_params,
    mask_above,
    mask_below,
    mask_below_chop,
    mask_below_trend,
    replay_masked,
)


def _synthetic_segment():
    setup = data.make_entry_setup(20000.0)
    last = setup[-1]["close"]
    cont_start = setup[-1]["time"] + timedelta(minutes=1)
    # run up (ratchet) then sell off (stop out), twice, so >1 trade + cooldown fires.
    deltas = [25.0] * 12 + [-40.0] * 15 + [20.0] * 14 + [-35.0] * 16
    run = data.continuation(last, deltas, start_ts=cont_start, spread=1.0)
    return data.frame(setup + run)


def test_masked_replay_matches_engine_synthetic():
    """All-eligible masked replay == real engine, byte-for-byte, across exit configs."""
    seg = _synthetic_segment()
    tape = build_bt_tape(seg, "NQ")
    all_elig = np.ones(len(tape.sig_idx), dtype=bool)
    any_trades = False
    for sl, lk, tr in [TF_CURRENT, SB_FAST, (60.0, 20.0, 100.0)]:
        p = cfg_params(sl, lk, tr)
        rep = replay_masked(tape, p, all_elig, force_flat=None)
        eng = engine.simulate_segment(seg, engine.FastGateEntry(p, "NQ"), p, force_flat=None).trades
        assert len(rep) == len(eng), f"count differs for {(sl, lk, tr)}: {len(eng)} vs {len(rep)}"
        for a, b in zip(eng, rep, strict=False):
            assert a.entry_ts == b.entry_ts
            assert abs(a.exit_price - b.exit_price) < 1e-9
            assert a.exit_reason == b.exit_reason
            assert abs(a.net_usd - b.net_usd) < 1e-9
        any_trades = any_trades or len(eng) >= 1
    assert any_trades, "synthetic segment produced no trades — fixture is not exercising the path"


def test_tp_whatif_caps_a_runner():
    """The +50-TP what-if must cap a winner at entry+50 and tag it 'take_profit'."""
    seg = _synthetic_segment()
    tape = build_bt_tape(seg, "NQ")
    all_elig = np.ones(len(tape.sig_idx), dtype=bool)
    p = cfg_params(*TF_CURRENT)
    capped = replay_masked(tape, p, all_elig, tp_pts=TP_WHATIF_PTS, force_flat=None)
    tp_trades = [t for t in capped if t.exit_reason == "take_profit"]
    assert tp_trades, "the +25*12 run-up should trigger at least one +50 TP exit"
    for t in tp_trades:
        assert abs(t.exit_price - (t.entry_price + TP_WHATIF_PTS)) < 1e-9


def test_partition_mask_algebra():
    """above = ~below; below-chop and below-trend partition the below set by adx."""
    seg = _synthetic_segment()
    tape = build_bt_tape(seg, "NQ")
    above, below = mask_above(tape), mask_below(tape)
    assert np.array_equal(above, ~below)
    thr = 20.0
    chop = mask_below_chop(tape, thr)
    trend = mask_below_trend(tape, thr)
    # chop and trend are disjoint and both lie inside the below set (NaN adx -> neither).
    assert not np.any(chop & trend)
    assert np.all(chop <= below)
    assert np.all(trend <= below)
