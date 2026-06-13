"""Behaviour tests for tools/eval/shadow_review.py — the Phase-11 decision layer.

Deterministic, hand-built telemetry (no DB mocks). Pins the load-bearing decision
invariants on top of the shadow_ledger scorecard:
  1. The pre-committed decision bar: INSUFFICIENT below n, then verdict flips on the
     sign of the net once the bar is reached.
  2. Confidence tiers map n -> NOISE/THIN/EMERGING/FIRM.
  3. Per-day rows carry that day's stats AND a correct running cumulative.
  4. The feed-regression flag fires ONLY for MISS-NO-BAR dated at/after the #127 fix.
  5. The $ math is identical to shadow_ledger (we reuse its scorecard verbatim).
"""

from __future__ import annotations

from tools.eval.metrics import compute_stats
from tools.eval.shadow_ledger import build_shadow_trades
from tools.eval.shadow_review import (
    DECISION_BAR_N,
    FEED_FIX_TS,
    build_review,
    confidence_tier,
    daily_breakdown,
    decide,
    detect_feed_regression,
)


# --------------------------------------------------------------- helpers (telemetry)
def _entry(msg_id: int, ts: str, price: float = 30000.0) -> dict:
    return {"message_id": msg_id, "ts": ts, "type": "entry", "direction": "long", "price": price}


def _exit(msg_id: int, ts: str, pnl_points: float) -> dict:
    return {"message_id": msg_id, "ts": ts, "type": "exit", "pnl_points": pnl_points}


def _rec(msg_id: int, ts: str, classification: str, price: float = 30000.0) -> dict:
    return {
        "message_id": msg_id,
        "signal_ts": ts,
        "seanbot_type": "entry",
        "direction": "long",
        "price": price,
        "classification": classification,
    }


def _trades_from(pairs: list[tuple[int, str, float]], cls: str = "MISS-regime", qty: int = 2):
    """Build ShadowTrades via the REAL pipeline so the $ math matches shadow_ledger."""
    recs = [_rec(mid, ts, cls) for mid, ts, _ in pairs]
    realized = {mid: pnl for mid, _, pnl in pairs}
    trades, _ = build_shadow_trades(recs, realized, qty=qty)
    return trades


# ------------------------------------------------------------------ confidence tier
def test_confidence_tier_thresholds():
    assert confidence_tier(0) == "NOISE"
    assert confidence_tier(9) == "NOISE"
    assert confidence_tier(10) == "THIN"
    assert confidence_tier(DECISION_BAR_N - 1) == "THIN"
    assert confidence_tier(DECISION_BAR_N) == "EMERGING"
    assert confidence_tier(29) == "EMERGING"
    assert confidence_tier(30) == "FIRM"


# ----------------------------------------------------------- pre-committed decision
def test_verdict_insufficient_below_bar_even_when_positive():
    # 6 winners, clearly net-positive, but below the decision bar -> INSUFFICIENT.
    pairs = [(i, f"2026-06-0{i}T22:00:00+00:00", 50.0) for i in range(1, 7)]
    trades = _trades_from(pairs)
    v = decide(compute_stats(trades))
    assert v.label == "INSUFFICIENT"
    assert v.confidence == "NOISE"


def test_verdict_gate_too_strict_when_bar_met_and_net_positive():
    # DECISION_BAR_N winners -> net positive at/above the bar -> GATE-TOO-STRICT.
    pairs = [(i, f"2026-06-09T{i % 24:02d}:00:00+00:00", 40.0) for i in range(DECISION_BAR_N)]
    # make message_ids unique and timestamps strictly distinct
    pairs = [
        (i, f"2026-0{1 + i // 28}-{1 + i % 28:02d}T12:00:00+00:00", 40.0)
        for i in range(DECISION_BAR_N)
    ]
    trades = _trades_from(pairs)
    assert len(trades) == DECISION_BAR_N
    v = decide(compute_stats(trades))
    assert v.label == "GATE-TOO-STRICT"


def test_verdict_gate_earning_its_keep_when_bar_met_and_net_negative():
    # DECISION_BAR_N losers -> net negative at/above the bar -> GATE-EARNING-ITS-KEEP.
    pairs = [
        (i, f"2026-0{1 + i // 28}-{1 + i % 28:02d}T12:00:00+00:00", -30.0)
        for i in range(DECISION_BAR_N)
    ]
    trades = _trades_from(pairs)
    v = decide(compute_stats(trades))
    assert v.label == "GATE-EARNING-ITS-KEEP"


# ---------------------------------------------------------------- per-day cumulative
def test_daily_breakdown_groups_and_accumulates():
    pairs = [
        (1, "2026-06-07T22:00:00+00:00", 50.0),  # day A win
        (2, "2026-06-07T23:00:00+00:00", -75.0),  # day A loss
        (3, "2026-06-08T14:00:00+00:00", 40.0),  # day B win
    ]
    trades = _trades_from(pairs)
    rows = daily_breakdown(trades)
    assert [r.day.isoformat() for r in rows] == ["2026-06-07", "2026-06-08"]
    # Day A alone: 2 trades; cumulative after A: 2 trades.
    assert rows[0].day_stats.n == 2
    assert rows[0].cum_stats.n == 2
    # Day B alone: 1 trade; cumulative after B: 3 trades.
    assert rows[1].day_stats.n == 1
    assert rows[1].cum_stats.n == 3
    # Cumulative net is monotonic accumulation of all trades.
    assert abs(rows[1].cum_stats.net_usd - sum(t.net_usd for t in trades)) < 1e-9


# ------------------------------------------------------------ feed-regression flag
def test_feed_regression_flags_only_post_fix_no_bar():
    before = FEED_FIX_TS.isoformat()  # exactly at the fix counts as post-fix (>=)
    recs = [
        _rec(1, "2026-06-01T22:00:00+00:00", "MISS-NO-BAR"),  # pre-fix: explained by old bug
        _rec(2, before, "MISS-NO-BAR"),  # at the fix instant: regression
        _rec(3, "2026-06-10T10:00:00+00:00", "MISS-NO-BAR"),  # post-fix: regression
        _rec(4, "2026-06-10T11:00:00+00:00", "MISS-regime"),  # not a no-bar, ignored
    ]
    fr = detect_feed_regression(recs)
    assert fr.total_no_bar == 3
    assert fr.is_regression is True
    assert {r["message_id"] for r in fr.post_fix} == {2, 3}


def test_feed_regression_clean_when_all_no_bar_pre_fix():
    recs = [
        _rec(1, "2026-06-01T22:00:00+00:00", "MISS-NO-BAR"),
        _rec(2, "2026-06-05T22:00:00+00:00", "MISS-NO-BAR"),
    ]
    fr = detect_feed_regression(recs)
    assert fr.total_no_bar == 2
    assert fr.is_regression is False
    assert fr.post_fix == []


# ----------------------------------------------------------------- full review wire
def test_build_review_matches_scorecard_dollars():
    recs = [
        _rec(1, "2026-06-07T22:00:00+00:00", "MISS-regime"),
        _rec(3, "2026-06-08T14:00:00+00:00", "MISS-NO-BAR"),
    ]
    signals = [
        _entry(1, "2026-06-07T22:00:00+00:00"),
        _exit(2, "2026-06-07T22:30:00+00:00", 50.0),
        _entry(3, "2026-06-08T14:00:00+00:00"),
        _exit(4, "2026-06-08T15:00:00+00:00", -30.0),
    ]
    review = build_review(recs, signals, qty=2)
    sc = review["scorecard"]
    # combined_stats is computed over the SAME trades the scorecard paired.
    assert review["combined_stats"].n == sc["paired"]
    assert abs(review["combined_stats"].net_usd - sc["combined"]["net_usd"]) < 1e-9
    # n=2 -> below the bar -> INSUFFICIENT.
    assert review["verdict"].label == "INSUFFICIENT"


def test_build_review_handles_empty():
    review = build_review([], [], qty=2)
    assert review["combined_stats"].n == 0
    assert review["daily"] == []
    assert review["verdict"].label == "INSUFFICIENT"
    assert review["feed_regression"].is_regression is False
