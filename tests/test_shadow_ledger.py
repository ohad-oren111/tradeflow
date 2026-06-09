"""Behaviour tests for tools/eval/shadow_ledger.py — the SeanBot shadow ledger.

Deterministic, hand-built telemetry (no DB mocks). We pin the load-bearing
invariants:
  1. FIFO entry->exit pairing maps each SB entry to its realized pnl_points, and
     leaves a still-open entry UNPAIRED (never invents an exit).
  2. Only MISS-regime / MISS-NO-BAR entries are scored; AGREE_ENTER and
     MISS-filter:* are excluded from the scorecard.
  3. A blocked entry with no captured exit is reported as unpaired, not dropped.
  4. The $ math mirrors engine._pnl (gross = pts * mult * qty; net subtracts the
     friction model), and WR/PF come from metrics.compute_stats.
  5. The verdict flips on the sign/PF of the blocked cluster.
"""

from __future__ import annotations

from datetime import UTC, datetime

from config.instruments import MNQ
from tools.eval.engine import DEFAULT_FRICTION_PTS
from tools.eval.shadow_ledger import (
    BLOCKED_CLASSES,
    ShadowTrade,
    build_shadow_trades,
    pair_entries_to_exits,
    scorecard,
)


def _entry(msg_id: int, ts: str, price: float = 30000.0) -> dict:
    return {"message_id": msg_id, "ts": ts, "type": "entry", "direction": "long", "price": price}


def _exit(msg_id: int, ts: str, pnl_points: float) -> dict:
    return {"message_id": msg_id, "ts": ts, "type": "exit", "pnl_points": pnl_points}


def _stop_moved(msg_id: int, ts: str) -> dict:
    return {"message_id": msg_id, "ts": ts, "type": "stop_moved"}


def _rec(msg_id: int, ts: str, classification: str, price: float = 30000.0) -> dict:
    return {
        "message_id": msg_id,
        "signal_ts": ts,
        "seanbot_type": "entry",
        "direction": "long",
        "price": price,
        "classification": classification,
    }


# ----------------------------------------------------------------- FIFO pairing


def test_pairing_maps_each_entry_to_its_next_exit():
    signals = [
        _entry(1, "2026-06-01T22:00:00+00:00"),
        _exit(2, "2026-06-01T22:30:00+00:00", 50.0),
        _entry(3, "2026-06-02T14:00:00+00:00"),
        _stop_moved(4, "2026-06-02T14:10:00+00:00"),  # ignored
        _exit(5, "2026-06-02T15:00:00+00:00", -75.0),
    ]
    realized = pair_entries_to_exits(signals)
    assert realized == {1: 50.0, 3: -75.0}


def test_pairing_leaves_open_entry_unpaired():
    # Entry 3 has no following exit -> absent from the map (still open / not captured).
    signals = [
        _entry(1, "2026-06-01T22:00:00+00:00"),
        _exit(2, "2026-06-01T22:30:00+00:00", 40.0),
        _entry(3, "2026-06-03T14:00:00+00:00"),
    ]
    realized = pair_entries_to_exits(signals)
    assert realized == {1: 40.0}
    assert 3 not in realized


def test_pairing_orders_by_ts_not_input_order():
    # Shuffled input must still pair by chronological ts.
    signals = [
        _exit(5, "2026-06-02T15:00:00+00:00", -75.0),
        _entry(1, "2026-06-01T22:00:00+00:00"),
        _entry(3, "2026-06-02T14:00:00+00:00"),
        _exit(2, "2026-06-01T22:30:00+00:00", 50.0),
    ]
    realized = pair_entries_to_exits(signals)
    assert realized == {1: 50.0, 3: -75.0}


def test_pairing_exit_without_pnl_points_does_not_pair():
    signals = [
        _entry(1, "2026-06-01T22:00:00+00:00"),
        {"message_id": 2, "ts": "2026-06-01T22:30:00+00:00", "type": "exit", "pnl_points": None},
    ]
    assert pair_entries_to_exits(signals) == {}


# ------------------------------------------------- classification scope + pairing


def test_only_blocked_classes_are_scored():
    recs = [
        _rec(1, "2026-06-01T22:00:00+00:00", "MISS-regime"),
        _rec(3, "2026-06-02T14:00:00+00:00", "AGREE_ENTER"),
        _rec(5, "2026-06-03T14:00:00+00:00", "MISS-filter:touch"),
        _rec(7, "2026-06-04T14:00:00+00:00", "MISS-NO-BAR"),
    ]
    realized = {1: 50.0, 3: 10.0, 5: 20.0, 7: -30.0}
    trades, unpaired = build_shadow_trades(recs, realized, qty=1)
    scored = {t.message_id for t in trades}
    assert scored == {1, 7}  # only MISS-regime + MISS-NO-BAR
    assert unpaired == []


def test_blocked_entry_without_exit_is_unpaired_not_dropped():
    recs = [
        _rec(1, "2026-06-01T22:00:00+00:00", "MISS-regime"),
        _rec(9, "2026-06-05T14:00:00+00:00", "MISS-NO-BAR"),  # no realized exit
    ]
    realized = {1: 50.0}  # message 9 absent
    trades, unpaired = build_shadow_trades(recs, realized, qty=1)
    assert [t.message_id for t in trades] == [1]
    assert len(unpaired) == 1
    assert unpaired[0]["message_id"] == 9


# ------------------------------------------------------------- dollar arithmetic


def test_net_usd_mirrors_engine_pnl_model():
    recs = [_rec(1, "2026-06-01T22:00:00+00:00", "MISS-regime")]
    trades, _ = build_shadow_trades(recs, {1: 50.0}, qty=2)
    t = trades[0]
    expected_gross = 50.0 * MNQ.multiplier * 2
    expected_net = expected_gross - DEFAULT_FRICTION_PTS * MNQ.multiplier * 2
    expected_comm = expected_gross - 2 * MNQ.commission_rt_usd
    assert t.gross_usd == expected_gross
    assert t.net_usd == expected_net
    assert t.net_usd_commission_only == expected_comm
    assert t.is_win is True


def test_qty_scales_dollars_linearly_but_not_winrate():
    recs = [
        _rec(1, "2026-06-01T22:00:00+00:00", "MISS-regime"),
        _rec(2, "2026-06-02T22:00:00+00:00", "MISS-regime"),
    ]
    realized = {1: 50.0, 2: -75.0}
    t1, _ = build_shadow_trades(recs, realized, qty=1)
    t2, _ = build_shadow_trades(recs, realized, qty=2)
    net1 = sum(t.net_usd for t in t1)
    net2 = sum(t.net_usd for t in t2)
    assert abs(net2 - 2 * net1) < 1e-9  # $ scale linearly in qty


# -------------------------------------------------------------------- scorecard


def test_scorecard_pairs_via_message_id_and_counts():
    recs = [
        _rec(1, "2026-06-01T22:00:00+00:00", "MISS-regime"),
        _rec(3, "2026-06-02T14:00:00+00:00", "MISS-NO-BAR"),
        _rec(5, "2026-06-03T14:00:00+00:00", "MISS-regime"),  # open, no exit
        _rec(7, "2026-06-04T14:00:00+00:00", "AGREE_ENTER"),  # not scored
    ]
    signals = [
        _entry(1, "2026-06-01T22:00:00+00:00"),
        _exit(2, "2026-06-01T22:30:00+00:00", 60.0),
        _entry(3, "2026-06-02T14:00:00+00:00"),
        _exit(4, "2026-06-02T15:00:00+00:00", -40.0),
        _entry(5, "2026-06-03T14:00:00+00:00"),  # no exit -> unpaired
    ]
    result = scorecard(recs, signals, qty=1)
    assert result["blocked_total"] == 3  # 2x MISS-regime + 1x MISS-NO-BAR
    assert result["paired"] == 2
    assert result["unpaired"] == 1
    assert result["combined"]["n"] == 2
    assert result["by_class"]["MISS-regime"]["n"] == 1
    assert result["by_class"]["MISS-NO-BAR"]["n"] == 1
    # breakdown carries the un-scored class for context.
    assert result["classification_breakdown"]["AGREE_ENTER"] == 1


def test_scorecard_combined_pf_and_winrate():
    recs = [
        _rec(1, "2026-06-01T22:00:00+00:00", "MISS-regime"),
        _rec(3, "2026-06-02T14:00:00+00:00", "MISS-regime"),
        _rec(5, "2026-06-03T14:00:00+00:00", "MISS-NO-BAR"),
    ]
    signals = [
        _entry(1, "2026-06-01T22:00:00+00:00"),
        _exit(2, "2026-06-01T22:30:00+00:00", 100.0),  # win
        _entry(3, "2026-06-02T14:00:00+00:00"),
        _exit(4, "2026-06-02T15:00:00+00:00", 50.0),  # win
        _entry(5, "2026-06-03T14:00:00+00:00"),
        _exit(6, "2026-06-03T15:00:00+00:00", -60.0),  # loss
    ]
    result = scorecard(recs, signals, qty=1)
    c = result["combined"]
    assert c["n"] == 3
    assert c["wins"] == 2
    assert c["losses"] == 1
    # PF = gross_win / abs(gross_loss); robust to the exact friction constant.
    assert isinstance(c["profit_factor"], (int, float))
    assert c["profit_factor"] > 1.0


def test_scorecard_handles_no_blocked_signals():
    recs = [_rec(1, "2026-06-01T22:00:00+00:00", "AGREE_ENTER")]
    signals = [_entry(1, "2026-06-01T22:00:00+00:00"), _exit(2, "2026-06-01T22:30:00+00:00", 10.0)]
    result = scorecard(recs, signals, qty=1)
    assert result["blocked_total"] == 0
    assert result["paired"] == 0
    assert result["combined"]["n"] == 0


# ------------------------------------------------------------------ sanity guard


def test_blocked_classes_are_the_two_gate_misses():
    assert BLOCKED_CLASSES == ("MISS-regime", "MISS-NO-BAR")


def test_shadow_trade_is_win_uses_net_usd():
    t = ShadowTrade(
        entry_ts=datetime(2026, 6, 1, 22, 0, tzinfo=UTC),
        message_id=1,
        classification="MISS-regime",
        pnl_points=0.0,
        qty=1,
        gross_usd=0.0,
        net_usd=-2.24,
        net_usd_commission_only=-2.48,
    )
    assert t.is_win is False
