"""Behaviour tests for tools/eval/concurrency_replay.py — Phase-13 cluster replay.

Deterministic, hand-built engine.Trade lists (no sim, no history). Pins the pure
analysis logic + that we drive the REAL kill-switch functions:
  1. correlated_stop_clusters groups >=2 losers exiting the SAME bar (and ignores
     single losers / winners).
  2. entry_gaps_minutes uses the REAL _entry_bar_minutes derivation.
  3. collapse_coverage / peak_streak_by_window call the REAL _collapse_loss_clusters
     and produce the expected merge/streak on a constructed correlated cluster.
  4. The three-point verdict: STACK + COLLAPSE-FIRES pass; the window=1 point is a
     CHALLENGE when the collapsed peak streak is still >= halt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tools.eval import concurrency_replay as cr
from tools.eval.engine import Trade


def _trade(entry_min: int, exit_min: int, net: float) -> Trade:
    """A minimal Trade with the fields the analysis reads (entry_ts/exit_ts/net_usd)."""
    base = datetime(2025, 1, 1, tzinfo=UTC)
    entry = base + timedelta(minutes=entry_min)
    exit_ = base + timedelta(minutes=exit_min)
    return Trade(
        entry_ts=entry,
        entry_price=20000.0,
        exit_ts=exit_,
        exit_price=19990.0 if net < 0 else 20010.0,
        exit_reason="stop" if net < 0 else "ratchet_stop",
        bars_held=exit_min - entry_min,
        highest=20010.0,
        final_stop=19990.0,
        gross_pts=net / 2.0,
        gross_usd=net,
        net_usd=net,
        net_usd_commission_only=net,
    )


# ------------------------------------------------------- correlated_stop_clusters
def test_clusters_group_same_bar_losers_only():
    trades = [
        _trade(0, 100, -50.0),  # A: loss exits @100
        _trade(1, 100, -60.0),  # B: loss exits @100  -> cluster with A
        _trade(2, 100, +40.0),  # winner @100 -> excluded
        _trade(5, 200, -30.0),  # lone loser @200 -> not a cluster (only 1)
    ]
    clusters = cr.correlated_stop_clusters(trades)
    assert len(clusters) == 1
    assert {t.net_usd for t in clusters[0]} == {-50.0, -60.0}
    # members sorted by entry time.
    assert clusters[0][0].entry_ts < clusters[0][1].entry_ts


def test_entry_gaps_use_real_entry_bar_minutes():
    # entries 1 min apart -> gap 1.0 (via the REAL _entry_bar_minutes epoch/60).
    cluster = [_trade(0, 100, -50.0), _trade(1, 100, -60.0)]
    gaps = cr.entry_gaps_minutes(cluster)
    assert gaps == [1.0]
    # entries 7 min apart -> gap 7.0
    cluster2 = [_trade(0, 100, -50.0), _trade(7, 100, -60.0)]
    assert cr.entry_gaps_minutes(cluster2) == [7.0]


# ----------------------------------------------------------- max consecutive run
def test_max_consecutive_loss_run():
    assert cr.max_consecutive_loss_run([-1, -1, 5, -1, -1, -1, 2]) == 3
    assert cr.max_consecutive_loss_run([1, 2, 3]) == 0
    assert cr.max_consecutive_loss_run([]) == 0


# --------------------------------------------------- REAL collapse on a cluster
def test_collapse_coverage_merges_adjacent_entry_losses():
    # Two losers with entries 1 min apart, exiting same bar -> window>=1 merges them
    # (1 loss-event collapsed); a third loser far away stays separate.
    trades = [
        _trade(0, 100, -50.0),
        _trade(1, 100, -60.0),
        _trade(500, 600, -40.0),
    ]
    cov = cr.collapse_coverage(trades, windows=(1,))
    # 3 losses -> the adjacent pair becomes 1 -> 2 losses remain -> merged == 1.
    assert cov[1] == 1


def test_peak_streak_drops_when_cluster_collapses():
    # Build a chronological 3-loss run where two share a 1-min entry gap; collapsing
    # them shortens the consecutive-loss run.
    trades = [
        _trade(0, 100, -50.0),  # loss
        _trade(1, 101, -60.0),  # loss, entry 1 min from prior
        _trade(2, 102, -40.0),  # loss, entry 1 min from prior
        _trade(300, 400, +80.0),  # winner breaks the run
    ]
    peak = cr.peak_streak_by_window(trades, windows=(1,))
    assert peak["per_trade"] == 3
    # all three entries within 1 min chain -> collapse to a single loss event.
    assert peak["w1"] == 1


# ------------------------------------------------------------- verdict assembly
def test_assess_points_stack_and_collapse_pass_window_challenge():
    pts = cr.assess_points(
        max_concurrent=2,
        bars_two_open=1000,
        n_target=2,
        collapse_w1=222,
        peak={"per_trade": 11, "w1": 10},
        halt_threshold=10,
        window=1,
        frac_within_window=0.526,
    )
    by = {p.name.split()[0]: p for p in pts}
    assert by["STACK"].passed is True
    assert by["COLLAPSE-FIRES"].passed is True
    # peak still >= halt (10) -> window=1 does NOT defuse -> CHALLENGE (not passed).
    assert pts[2].passed is False
    assert "CHALLENGED" in pts[2].name


def test_assess_points_window_validated_when_peak_below_halt():
    pts = cr.assess_points(
        max_concurrent=2,
        bars_two_open=1000,
        n_target=2,
        collapse_w1=300,
        peak={"per_trade": 11, "w1": 8},
        halt_threshold=10,
        window=1,
        frac_within_window=0.9,
    )
    assert pts[2].passed is True
    assert "VALIDATED" in pts[2].name


def test_candidate_window_picks_first_below_halt():
    # per-trade trips; w1/w2 still >=10; w5 drops to 9 -> candidate is 5.
    peak = {"per_trade": 11, "w1": 10, "w2": 10, "w5": 9, "w8": 8, "w15": 7}
    assert cr.candidate_window(peak, 10) == 5
    # none below halt -> None
    peak2 = {"per_trade": 12, "w1": 11, "w2": 11, "w5": 10, "w8": 10, "w15": 10}
    assert cr.candidate_window(peak2, 10) is None
