"""Fidelity test for tools/eval/exit_sweep.py.

The sweep's speed trick is the precompute-once entry tape + cheap trade-by-trade
``replay``. That is only trustworthy if ``replay`` reproduces the real
``engine.simulate_segment`` exactly. This asserts byte-identical trades (entry/exit/
reason/net) on a synthetic segment across several exit configs — the CI guard for the
invariant the full run validates on real data (1,479 trades, 0 mismatch).

Synthetic data keeps this CI-runnable (the real 26-mo CSV is not in CI).
"""

from __future__ import annotations

from datetime import timedelta

from config.risk_params import RiskParams
from tools.eval import data, engine
from tools.eval.exit_sweep import build_tape, replay


def _synthetic_segment():
    setup = data.make_entry_setup(20000.0)
    last = setup[-1]["close"]
    cont_start = setup[-1]["time"] + timedelta(minutes=1)
    # run up (ratchet) then sell off (stop out), twice, so >1 trade + cooldown fires.
    deltas = [25.0] * 12 + [-40.0] * 15 + [20.0] * 14 + [-35.0] * 16
    run = data.continuation(last, deltas, start_ts=cont_start, spread=1.0)
    return data.frame(setup + run)


def _params(sl, lk, tr):
    return RiskParams(
        regime_gate_enabled=False,  # synthetic data won't pass the 30m regime gate
        exit_mode="trailing",
        stop_loss_pts=sl,
        lock_in_pts=lk,
        trail_offset_pts=tr,
    )


def test_replay_matches_engine_synthetic():
    seg = _synthetic_segment()
    any_trades = False
    for sl, lk, tr in [(75.0, 50.0, 150.0), (50.0, 30.0, 30.0), (60.0, 20.0, 100.0)]:
        p = _params(sl, lk, tr)
        tape = build_tape(seg, p, "NQ")
        rep = replay(tape, p, force_flat=None)
        eng = engine.simulate_segment(seg, engine.FastGateEntry(p, "NQ"), p, force_flat=None).trades
        assert len(rep) == len(eng), f"count differs for {(sl, lk, tr)}: {len(eng)} vs {len(rep)}"
        for a, b in zip(eng, rep, strict=False):
            assert a.entry_ts == b.entry_ts
            assert abs(a.exit_price - b.exit_price) < 1e-9
            assert a.exit_reason == b.exit_reason
            assert abs(a.net_usd - b.net_usd) < 1e-9
        any_trades = any_trades or len(eng) >= 1
    assert any_trades, "synthetic segment produced no trades — fixture is not exercising the path"
