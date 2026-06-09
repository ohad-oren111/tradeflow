"""Fidelity + behaviour tests for tools/eval/portfolio_study.py.

The portfolio simulator's whole credibility rests on ONE invariant: at N=1, M=2 with
no overlays it must reproduce the validated single-position ``engine.simulate_segment``
(regime ON) byte-for-byte. If that holds, the multi-position numbers are trustworthy;
if it drifts, every Part-A/Part-B figure is suspect. This is the same anchor the full
run asserts with ``--validate`` on the real 26-mo CSV — here on synthetic data so it is
CI-runnable (the real CSV is not in CI).

We also pin: (1) per-contract P&L scales linearly in M (M=2 net == 2x M=1 net, same
trades); (2) raising N never produces FEWER entries (concurrency only adds capacity);
(3) the daily-loss-cap overlay actually suppresses entries after a capped day; (4) the
verdict helper does not fire on a degenerate/empty pooled set.

Short synthetic segments never reach 202 30-min buckets, so the regime gate fail-opens
(allows) — the anchor is exercised for the entry/exit/cooldown/concurrency algebra, not
regime calibration (that is covered by the live ``--validate`` run).
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from tools.eval import data
from tools.eval.below_trend_study import _month_bounds
from tools.eval.engine import Trade
from tools.eval.portfolio_study import (
    TF_TRAIL,
    PortfolioConfig,
    PortfolioTape,
    build_tape,
    compute_portfolio_metrics,
    simulate_portfolio,
    validate,
    verdict_beats_sb,
)


def _two_losers_one_day_tape() -> PortfolioTape:
    """Hand-built tape: two same-day signals (bars 0 and 20, beyond cooldown=10), price
    set so every open position stops out at entry-75 the next bar. Deterministic — lets us
    test the daily-loss-cap mechanic without depending on the gate firing twice."""
    from datetime import UTC, datetime, timedelta

    n = 60
    base = 20000.0
    t0 = datetime(2025, 4, 1, 14, 0, tzinfo=UTC)
    ts = [t0 + timedelta(minutes=i) for i in range(n)]
    close = np.full(n, base, dtype=float)
    high = np.full(n, base, dtype=float)
    low = np.full(n, base - 100.0, dtype=float)  # < entry-75 -> always trips the stop
    atr = np.zeros(n, dtype=float)
    sig_idx = np.asarray([0, 20], dtype=np.int64)
    sig_px = np.asarray([base, base], dtype=float)
    return PortfolioTape(
        ts=ts,
        high=high,
        low=low,
        close=close,
        atr=atr,
        sig_idx=sig_idx,
        sig_px=sig_px,
        span=(ts[0], ts[-1]),
        bars=n,
    )


def _serial_losers_tape(n_signals: int = 12, gap: int = 15) -> PortfolioTape:
    """N=1-friendly tape: ``n_signals`` signals each ``gap`` bars apart (> cooldown=10), every
    one stopping out at a loss the next bar. Lets us test the kill-switch halt deterministically
    (>= 10 consecutive losses)."""
    from datetime import UTC, datetime, timedelta

    base = 20000.0
    sig_bars = [i * gap for i in range(n_signals)]
    n = sig_bars[-1] + 5
    t0 = datetime(2025, 4, 1, 14, 0, tzinfo=UTC)
    ts = [t0 + timedelta(minutes=i) for i in range(n)]
    close = np.full(n, base, dtype=float)
    high = np.full(n, base, dtype=float)
    low = np.full(n, base - 100.0, dtype=float)  # always trips entry-75 stop -> every trade loses
    atr = np.zeros(n, dtype=float)
    sig_idx = np.asarray(sig_bars, dtype=np.int64)
    sig_px = np.asarray([base] * n_signals, dtype=float)
    return PortfolioTape(
        ts=ts,
        high=high,
        low=low,
        close=close,
        atr=atr,
        sig_idx=sig_idx,
        sig_px=sig_px,
        span=(ts[0], ts[-1]),
        bars=n,
    )


def _synthetic_segment():
    setup = data.make_entry_setup(20000.0)
    last = setup[-1]["close"]
    cont_start = setup[-1]["time"] + timedelta(minutes=1)
    # run up (ratchet) then sell off (stop out), twice, so >1 trade + cooldown fires.
    deltas = [25.0] * 12 + [-40.0] * 15 + [20.0] * 14 + [-35.0] * 16
    run = data.continuation(last, deltas, start_ts=cont_start, spread=1.0)
    return data.frame(setup + run)


def test_fidelity_anchor_matches_engine_synthetic():
    """N=1, M=2, no overlays == real engine.simulate_segment, byte-for-byte."""
    seg = _synthetic_segment()
    tape = build_tape(seg, "NQ")
    ok, msg = validate(seg, tape)
    assert ok, f"fidelity anchor drifted: {msg}"


def test_contracts_scale_pnl_linearly():
    """Same N=1 book at M=2 nets exactly 2x the M=1 book (same trades, doubled size)."""
    seg = _synthetic_segment()
    tape = build_tape(seg, "NQ")
    one = simulate_portfolio(
        tape, PortfolioConfig("m1", max_positions=1, contracts=1, exit_cfg=TF_TRAIL)
    )
    two = simulate_portfolio(
        tape, PortfolioConfig("m2", max_positions=1, contracts=2, exit_cfg=TF_TRAIL)
    )
    assert len(one.trades) == len(two.trades) >= 1
    for a, b in zip(one.trades, two.trades, strict=False):
        assert a.entry_ts == b.entry_ts
        assert abs(a.exit_price - b.exit_price) < 1e-9
        # friction is PER contract, so the doubled book is exactly 2x net.
        assert abs(b.net_usd - 2.0 * a.net_usd) < 1e-6


def test_more_concurrency_never_fewer_entries():
    """Raising N only ADDS slot capacity — entries taken is monotonic non-decreasing."""
    seg = _synthetic_segment()
    tape = build_tape(seg, "NQ")
    n1 = simulate_portfolio(
        tape, PortfolioConfig("n1", max_positions=1, contracts=2, exit_cfg=TF_TRAIL)
    )
    n3 = simulate_portfolio(
        tape, PortfolioConfig("n3", max_positions=3, contracts=2, exit_cfg=TF_TRAIL)
    )
    assert n3.n_signals_taken >= n1.n_signals_taken
    assert n3.max_concurrent_seen >= n1.max_concurrent_seen


def test_daily_loss_cap_blocks_after_a_capped_day():
    """After the first same-day loss trips the cap, later same-day signals are skipped."""
    tape = _two_losers_one_day_tape()
    uncapped = simulate_portfolio(
        tape, PortfolioConfig("u", max_positions=3, contracts=2, exit_cfg=TF_TRAIL)
    )
    capped = simulate_portfolio(
        tape,
        PortfolioConfig(
            "c", max_positions=3, contracts=2, exit_cfg=TF_TRAIL, daily_loss_cap_usd=1.0
        ),
    )
    assert uncapped.n_signals_taken == 2  # both same-day signals fire when uncapped
    assert capped.n_signals_taken == 1  # the 2nd is blocked once the day is capped


def test_metrics_are_finite_and_consistent():
    """compute_portfolio_metrics returns coherent fields on a real (synthetic) book."""
    seg = _synthetic_segment()
    tape = build_tape(seg, "NQ")
    res = simulate_portfolio(
        tape, PortfolioConfig("n3", max_positions=3, contracts=2, exit_cfg=TF_TRAIL)
    )
    m = compute_portfolio_metrics(res, start_capital=25_000.0)
    assert m.n_trades == len(res.trades)
    assert m.maxdd_mtm_usd >= 0.0
    assert m.maxdd_realized_usd >= 0.0
    # net is the sum of per-trade net_usd.
    assert abs(m.net_usd - sum(t.net_usd for t in res.trades)) < 1e-6


def test_kill_switch_halts_then_disable_lets_it_run():
    """halt@10 consecutive losses halts an enabled book; disabling it runs the full tape."""
    tape = _serial_losers_tape(n_signals=12, gap=15)
    on = simulate_portfolio(
        tape,
        PortfolioConfig(
            "on", max_positions=1, contracts=2, exit_cfg=TF_TRAIL, kill_switch_enabled=True
        ),
    )
    off = simulate_portfolio(
        tape,
        PortfolioConfig(
            "off", max_positions=1, contracts=2, exit_cfg=TF_TRAIL, kill_switch_enabled=False
        ),
    )
    assert on.halted_at is not None  # the 10-consecutive-loss halt fired
    assert on.n_signals_taken == 10  # stops taking after the 10th loss
    assert off.halted_at is None  # disabled -> no premature halt
    assert off.n_signals_taken == 12  # every signal taken


def test_verdict_does_not_fire_on_degenerate_pool():
    """An empty / zero-trade pooled OOS must NOT be reported as 'beats SB'."""
    empty: list[Trade] = []
    # base degenerate (no trades) -> no overlay can legitimately 'beat' it.
    assert verdict_beats_sb(empty, {"vol-scaled": empty, "daily-cap": empty}) is None


def test_month_bounds_partition_is_contiguous_and_complete():
    """_month_bounds over the portfolio tape covers every bar with no gaps/overlaps."""
    seg = _synthetic_segment()
    tape = build_tape(seg, "NQ")
    months = _month_bounds(tape)
    assert months
    assert months[0][1] == 0
    assert months[-1][2] == len(tape.ts)
    for (_, _, end), (_, nxt_start, _) in zip(months, months[1:], strict=False):
        assert end == nxt_start  # contiguous, no gap or overlap
