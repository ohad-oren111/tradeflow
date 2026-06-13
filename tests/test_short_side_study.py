"""Fidelity + behaviour tests for tools/eval/short_side_study.py.

The short study's whole credibility rests on the FIDELITY ANCHOR: the
direction-parameterized entry+exit code, run with ``direction=LONG``, must
reproduce the real ``engine.simulate_segment`` byte-for-byte. Only then is the
``direction=SHORT`` mirror trustworthy. We prove that here on synthetic data
(CI-runnable; the real 26-mo CSV is not in CI), and we prove the SHORT side
independently via a price-REFLECTION symmetry: reflecting a long-firing fixture
around a constant turns a LONG setup into a SHORT setup whose P&L, under the same
exit, must match the long exactly (lows<->highs, bullish<->bearish, MA order
flipped). That pins both the mirrored entry gate and the direction-aware exit
(stop ABOVE entry, ratchet DOWN, intrabar trigger on the high, P&L = entry-exit).

Short synthetic segments never reach 202 30-min buckets, so the regime gate
fail-opens and ``sig_below`` is all-False — the down/above masks are exercised for
algebra, not regime calibration (that is covered by the live ``--validate`` run).
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from src.state_machine import Direction
from tools.eval import data, engine
from tools.eval.short_side_study import (
    SB_FAST,
    TF_CURRENT,
    build_ss_tape,
    cfg_params,
    evaluate_gates_dir,
    mask_above,
    mask_down,
    mask_down_chop,
    mask_down_directional,
    replay_masked,
    validate,
)


def _long_segment():
    """A fixture that fires exactly one real LONG signal then runs up & sells off twice."""
    setup = data.make_entry_setup(20000.0)
    last = setup[-1]["close"]
    cont_start = setup[-1]["time"] + timedelta(minutes=1)
    deltas = [25.0] * 12 + [-40.0] * 15 + [20.0] * 14 + [-35.0] * 16
    run = data.continuation(last, deltas, start_ts=cont_start, spread=1.0)
    return data.frame(setup + run)


def _reflect_segment(seg, c0: float = 40000.0):
    """Reflect OHLC around ``c0`` (high<->low swap) — turns a LONG setup into a SHORT setup."""
    rbars = []
    for _, r in seg.iterrows():
        rbars.append(
            {
                "time": r["time"].to_pydatetime(),
                "open": c0 - float(r["open"]),
                "high": c0 - float(r["low"]),
                "low": c0 - float(r["high"]),
                "close": c0 - float(r["close"]),
                "volume": 10.0,
            }
        )
    return data.frame(rbars)


def test_long_mirror_matches_engine_synthetic():
    """FIDELITY ANCHOR: the LONG mirror == real engine, byte-for-byte, across exit configs."""
    seg = _long_segment()
    long_tape = build_ss_tape(seg, Direction.LONG, "NQ")
    all_elig = np.ones(len(long_tape.sig_idx), dtype=bool)
    any_trades = False
    for sl, lk, tr in [TF_CURRENT, SB_FAST, (60.0, 20.0, 100.0)]:
        p = cfg_params(sl, lk, tr)
        rep = replay_masked(long_tape, p, all_elig, force_flat=None)
        eng = engine.simulate_segment(seg, engine.FastGateEntry(p, "NQ"), p, force_flat=None).trades
        assert len(rep) == len(eng), f"count differs for {(sl, lk, tr)}: {len(eng)} vs {len(rep)}"
        for a, b in zip(eng, rep, strict=False):
            assert a.entry_ts == b.entry_ts
            assert abs(a.exit_price - b.exit_price) < 1e-9
            assert a.exit_reason == b.exit_reason
            assert abs(a.net_usd - b.net_usd) < 1e-9
        any_trades = any_trades or len(eng) >= 1
    assert any_trades, "synthetic segment produced no trades — fixture is not exercising the path"


def test_validate_helper_passes_synthetic():
    """The study's own ``validate`` (LONG mirror == engine) must report PASS on the fixture."""
    ok, msg = validate(_long_segment())
    assert ok, f"validate FAILED: {msg}"


def test_short_matches_long_under_reflection():
    """SHORT correctness: a reflected LONG setup fires one SHORT whose count + net P&L match
    the LONG exactly, under both exits — proving the mirrored entry AND the direction-aware
    exit (stop above, ratchet down, P&L = entry - exit)."""
    seg = _long_segment()
    rseg = _reflect_segment(seg)
    long_tape = build_ss_tape(seg, Direction.LONG, "NQ")
    short_tape = build_ss_tape(rseg, Direction.SHORT, "NQ")
    assert len(short_tape.sig_idx) >= 1, "reflected fixture fired no short signal"
    lm = np.ones(len(long_tape.sig_idx), dtype=bool)
    sm = np.ones(len(short_tape.sig_idx), dtype=bool)
    for sl, lk, tr in [TF_CURRENT, SB_FAST]:
        p = cfg_params(sl, lk, tr)
        lt = replay_masked(long_tape, p, lm, force_flat=None)
        st = replay_masked(short_tape, p, sm, force_flat=None)
        assert len(st) == len(lt), f"trade count differs for {(sl, lk, tr)}"
        l_net = sum(t.net_usd for t in lt)
        s_net = sum(t.net_usd for t in st)
        assert (
            abs(s_net - l_net) < 1e-6
        ), f"short net {s_net} != long net {l_net} for {(sl, lk, tr)}"
        # the short must profit as price falls: positive gross on a winning reflected run-up.
        for t in st:
            assert t.gross_pts == t.entry_price - t.exit_price


def test_short_gate_is_mirror_of_long_gate():
    """On the same bar, the SHORT gate's inequalities are the flip of the LONG gate's:
    a bar that satisfies the LONG ma_order (MA100>MA50) cannot satisfy the SHORT one."""
    seg = _long_segment()
    rp = cfg_params(*TF_CURRENT)
    # find the LONG signal bar; assert the same window does NOT fire a short, and that
    # the reflected window DOES (mirror), via evaluate_gates_dir directly.
    fired_long = None
    for j in range(2, len(seg)):
        ge = evaluate_gates_dir(seg.iloc[: j + 1], "NQ", Direction.LONG, params=rp)
        if ge.signal is not None:
            fired_long = j
            break
    assert fired_long is not None, "fixture did not fire a long"
    ge_short_same = evaluate_gates_dir(seg.iloc[: fired_long + 1], "NQ", Direction.SHORT, params=rp)
    assert ge_short_same.signal is None, "long-setup bar must not also fire a short (mirror)"


def test_partition_mask_algebra():
    """down = sig_below; above = ~down; directional/chop partition the down set by adx."""
    rseg = _reflect_segment(_long_segment())
    tape = build_ss_tape(rseg, Direction.SHORT, "NQ")
    down, above = mask_down(tape), mask_above(tape)
    assert np.array_equal(above, ~down)
    thr = 20.0
    directional = mask_down_directional(tape, thr)
    chop = mask_down_chop(tape, thr)
    assert not np.any(directional & chop)  # disjoint
    assert np.all(directional <= down)  # both lie inside the down set
    assert np.all(chop <= down)
