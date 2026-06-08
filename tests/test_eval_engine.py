"""Tests for tools/eval/engine.py.

The load-bearing test is FIDELITY: ``FastGateEntry`` (the optimized backtest driver)
must produce byte-identical decisions and trades to the REAL ``Sma100BounceStrategy``
object driven via ``RealStrategyEntry``. If that holds, the backtest measures the
real strategy, not a re-implementation.

The drivers diverge ONLY past a 7000-bar buffer (FastGateEntry slices the last 7000,
matching deque(maxlen=7000); SMA windows are 50/100 << 7000 so they are identical, and
the regime gate resamples the same last-7000 tail). All fixtures here stay < 7000 bars,
where the two are provably identical bar-for-bar. The unused ewm columns (atr/adx) are
not read by evaluate_gates, so precomputing them over the whole frame is irrelevant.
"""

from __future__ import annotations

from datetime import timedelta

from config.risk_params import RiskParams
from tools.eval import data, engine


def _decisions(driver, seg) -> list[str]:
    out = []
    for j in range(len(seg)):
        dec, _ = driver.step(seg, j)
        out.append(dec)
    return out


def _synthetic_segment():
    setup = data.make_entry_setup(20000.0)
    last = setup[-1]["close"]
    cont_start = setup[-1]["time"] + timedelta(minutes=1)
    run = data.continuation(last, [25.0] * 12 + [-40.0] * 15, start_ts=cont_start, spread=1.0)
    return data.frame(setup + run)


def test_fast_vs_real_decisions_synthetic():
    params = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
    seg = _synthetic_segment()
    fast = _decisions(engine.FastGateEntry(params, "NQ"), seg)
    real = _decisions(engine.RealStrategyEntry(params, "NQ"), seg)
    assert fast == real
    assert "long_signal" in fast


def test_fast_vs_real_full_sim_identical():
    params = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
    seg = _synthetic_segment()
    rf = engine.simulate_segment(seg, engine.FastGateEntry(params, "NQ"), params)
    rr = engine.simulate_segment(seg, engine.RealStrategyEntry(params, "NQ"), params)
    assert len(rf.trades) == len(rr.trades) >= 1
    for a, b in zip(rf.trades, rr.trades, strict=False):
        assert a.entry_price == b.entry_price
        assert a.exit_price == b.exit_price
        assert a.exit_reason == b.exit_reason
        assert abs(a.net_usd - b.net_usd) < 1e-9


def test_fast_vs_real_decisions_realdata():
    # First ~700 real NQ bars, regime ON — proves the fast gate == on_new_bar on
    # actual market data (well within the 7000-bar identical region). Skipped when
    # the saved history isn't present (e.g. CI, where research/ is untracked).
    import os

    import pytest

    if not os.path.exists(data.DEFAULT_HISTORY):
        pytest.skip("saved history not present (research/ is untracked)")
    params = RiskParams(regime_gate_enabled=True, exit_mode="trailing")
    df = data.load_history().iloc[:700].reset_index(drop=True)
    from src.indicators import add_all_indicators

    seg = add_all_indicators(df).reset_index(drop=True)
    fast = _decisions(engine.FastGateEntry(params, "NQ"), seg)
    real = _decisions(engine.RealStrategyEntry(params, "NQ"), seg)
    assert fast == real


def test_exit_base_stop():
    # entry then a straight drop > 75pt → exit at entry-75, reason "stop".
    params = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
    setup = data.make_entry_setup(20000.0)
    entry = setup[-1]["close"]
    cont = data.continuation(
        entry, [-20.0] * 6, start_ts=setup[-1]["time"] + timedelta(minutes=1), spread=1.0
    )
    res = engine.simulate_segment(
        data.frame(setup + cont), engine.FastGateEntry(params, "NQ"), params
    )
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert abs(t.exit_price - (entry - params.stop_loss_pts)) < 0.01
    assert t.net_usd < 0


def test_exit_hard_ceiling():
    params = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
    setup = data.make_entry_setup(20000.0)
    entry = setup[-1]["close"]
    cont = data.continuation(
        entry, [60.0] * 22, start_ts=setup[-1]["time"] + timedelta(minutes=1), spread=1.0
    )
    res = engine.simulate_segment(
        data.frame(setup + cont), engine.FastGateEntry(params, "NQ"), params
    )
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "hard_ceiling"
    assert (t.exit_price - entry) >= params.hard_ceiling_pts


def test_exit_ratchet_walks_profit():
    params = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
    setup = data.make_entry_setup(20000.0)
    entry = setup[-1]["close"]
    cont = data.continuation(
        entry,
        [70.0] * 9 + [-120.0] * 12,
        start_ts=setup[-1]["time"] + timedelta(minutes=1),
        spread=0.75,
    )
    res = engine.simulate_segment(
        data.frame(setup + cont), engine.FastGateEntry(params, "NQ"), params
    )
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "ratchet_stop"
    assert t.final_stop > entry + params.trail_offset_pts  # trail engaged
    assert t.highest > t.final_stop  # gave back from the peak
    assert t.net_usd > 0


def test_pnl_formula_matches_config():
    # gross = (exit-entry)*2*2.0; friction model subtracts 1.12*4; commission model 2*1.24.
    gp, gu, nf, nc = engine._pnl(20000.0, 20050.0, friction_pts=1.12, qty=2)
    assert gp == 50.0
    assert gu == 50.0 * 2.0 * 2  # $200
    assert abs(nf - (200.0 - 1.12 * 2.0 * 2)) < 1e-9  # $195.52
    assert abs(nc - (200.0 - 2 * 1.24)) < 1e-9  # $197.52
