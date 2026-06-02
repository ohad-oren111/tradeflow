"""Tests for src.execution.bracket — pure construction, no mocks needed."""

from __future__ import annotations

import pytest

from src.execution.bracket import (
    build_bracket,
    build_entry_oca_bracket,
    build_protective_stop,
)
from src.state_machine import Direction

# --------------------------------------------------------------------- PR B —
# build_entry_oca_bracket: native server-side OCA bracket (parent + fixed STP +
# trailing/fixed TP). Replaces the standalone-STP entry design (Task E.1 fix).


def _oca(direction=Direction.LONG, qty=2, exit_mode="trailing", entry_ref=20000.0):
    return build_entry_oca_bracket(
        direction=direction,
        qty=qty,
        entry_type="MKT",
        entry_lmt_price=None,
        stop_price=entry_ref - 75.0,
        target_price=entry_ref + 150.0,
        exit_mode=exit_mode,
        trail_offset=150.0,
        entry_ref_price=entry_ref,
        oca_group="tf-exit-abc12345",
    )


def test_oca_bracket_returns_three_legs():
    result = _oca()
    assert len(result) == 3


def test_oca_trailing_places_fixed_stp_and_trailing_tp_in_one_oca_group():
    parent, stop_child, tp_child = _oca(exit_mode="trailing", entry_ref=20000.0)
    # Parent entry.
    assert parent.action == "BUY"
    assert parent.orderType == "MKT"
    assert parent.transmit is False
    # Fixed protective stop @ entry-75 — STP, never a TRAIL.
    assert stop_child.action == "SELL"
    assert stop_child.orderType == "STP"
    assert stop_child.auxPrice == 20000.0 - 75.0
    assert stop_child.transmit is False
    # Trailing take-profit — TRAIL with the offset as the trailing amount.
    assert tp_child.action == "SELL"
    assert tp_child.orderType == "TRAIL"
    assert tp_child.auxPrice == 150.0
    assert tp_child.transmit is True
    # Both exit legs share ONE OCA group with ocaType=1 (one fill cancels sibling).
    assert stop_child.ocaGroup == tp_child.ocaGroup == "tf-exit-abc12345"
    assert stop_child.ocaType == 1
    assert tp_child.ocaType == 1


def test_oca_fixed_stop_never_trails():
    # In BOTH modes the protective stop is a fixed STP — never a TRAIL.
    for mode in ("trailing", "fixed"):
        _parent, stop_child, _tp = _oca(exit_mode=mode)
        assert stop_child.orderType == "STP"
        assert stop_child.auxPrice == 20000.0 - 75.0


def test_oca_trailing_tp_initial_trigger_is_one_offset_below_entry_long():
    _parent, _stop, tp_child = _oca(
        direction=Direction.LONG, exit_mode="trailing", entry_ref=20000.0
    )
    # LONG sell-trail starts one full offset below the entry reference and ratchets up.
    assert tp_child.trailStopPrice == 20000.0 - 150.0


def test_oca_trailing_tp_initial_trigger_is_one_offset_above_entry_short():
    _parent, _stop, tp_child = _oca(
        direction=Direction.SHORT, exit_mode="trailing", entry_ref=20000.0
    )
    assert tp_child.action == "BUY"
    assert tp_child.trailStopPrice == 20000.0 + 150.0


def test_oca_fixed_mode_is_legacy_lmt_take_profit_no_regression():
    _parent, stop_child, tp_child = _oca(exit_mode="fixed", entry_ref=20000.0)
    # Fixed mode = legacy LMT take-profit, no TRAIL fields.
    assert tp_child.orderType == "LMT"
    assert tp_child.lmtPrice == 20000.0 + 150.0
    assert tp_child.transmit is True
    # Stop unchanged, still OCA-grouped with the LMT.
    assert stop_child.orderType == "STP"
    assert stop_child.ocaGroup == tp_child.ocaGroup


def test_oca_both_exit_legs_are_gtc_outside_rth():
    _parent, stop_child, tp_child = _oca(exit_mode="trailing")
    assert stop_child.tif == "GTC"
    assert tp_child.tif == "GTC"
    assert stop_child.outsideRth is True
    assert tp_child.outsideRth is True


def test_oca_transmit_chains_on_last_leg_only():
    parent, stop_child, tp_child = _oca()
    # Only the final leg (tp_child) transmits → the whole bracket submits atomically.
    assert parent.transmit is False
    assert stop_child.transmit is False
    assert tp_child.transmit is True


def test_oca_rejects_bad_exit_mode():
    with pytest.raises(ValueError, match="unsupported exit_mode"):
        build_entry_oca_bracket(
            direction=Direction.LONG,
            qty=2,
            entry_type="MKT",
            entry_lmt_price=None,
            stop_price=19925.0,
            target_price=20150.0,
            exit_mode="bogus",  # type: ignore[arg-type]
            trail_offset=150.0,
            entry_ref_price=20000.0,
            oca_group="tf-exit-x",
        )


def test_oca_trailing_rejects_non_positive_offset():
    with pytest.raises(ValueError, match="trail_offset must be positive"):
        build_entry_oca_bracket(
            direction=Direction.LONG,
            qty=2,
            entry_type="MKT",
            entry_lmt_price=None,
            stop_price=19925.0,
            target_price=20150.0,
            exit_mode="trailing",
            trail_offset=0.0,
            entry_ref_price=20000.0,
            oca_group="tf-exit-x",
        )


def test_long_market_parent_has_transmit_false():
    parent, _tp = build_bracket(
        direction=Direction.LONG,
        qty=2,
        entry_type="MKT",
        entry_lmt_price=None,
        target_price=17600.0,
    )
    assert parent.action == "BUY"
    assert parent.totalQuantity == 2
    assert parent.orderType == "MKT"
    assert parent.transmit is False


def test_long_market_tp_child_has_transmit_true_lmt_at_target():
    _parent, tp = build_bracket(
        direction=Direction.LONG,
        qty=2,
        entry_type="MKT",
        entry_lmt_price=None,
        target_price=17650.0,
    )
    assert tp.action == "SELL"
    assert tp.totalQuantity == 2
    assert tp.orderType == "LMT"
    assert tp.lmtPrice == 17650.0
    assert tp.transmit is True


def test_bracket_tp_child_is_gtc_so_it_survives_daily_break_and_overnight():
    # Under 24/5 trading the position can be open across the CME maintenance
    # break and the next trading day; the TP leg MUST be GTC or it expires at
    # the close of the daily session. Paired with the GTC STP from
    # build_protective_stop so both protective legs persist.
    _parent_long, tp_long = build_bracket(
        direction=Direction.LONG,
        qty=2,
        entry_type="MKT",
        entry_lmt_price=None,
        target_price=17600.0,
    )
    _parent_short, tp_short = build_bracket(
        direction=Direction.SHORT,
        qty=1,
        entry_type="MKT",
        entry_lmt_price=None,
        target_price=17400.0,
    )
    assert tp_long.tif == "GTC"
    assert tp_short.tif == "GTC"


def test_long_lmt_parent_carries_lmt_price():
    parent, _tp = build_bracket(
        direction=Direction.LONG,
        qty=1,
        entry_type="LMT",
        entry_lmt_price=17500.25,
        target_price=17600.0,
    )
    assert parent.orderType == "LMT"
    assert parent.lmtPrice == 17500.25
    assert parent.transmit is False


def test_lmt_parent_requires_lmt_price():
    with pytest.raises(ValueError, match="LMT.*requires"):
        build_bracket(
            direction=Direction.LONG,
            qty=1,
            entry_type="LMT",
            entry_lmt_price=None,
            target_price=17600.0,
        )


def test_qty_must_be_positive():
    with pytest.raises(ValueError, match="qty must be positive"):
        build_bracket(
            direction=Direction.LONG,
            qty=0,
            entry_type="MKT",
            entry_lmt_price=None,
            target_price=17600.0,
        )


def test_protective_stop_long_is_sell_stp_gtc_parent_zero():
    stp = build_protective_stop(
        direction=Direction.LONG,
        qty=2,
        stop_price=17400.0,
    )
    assert stp.action == "SELL"
    assert stp.totalQuantity == 2
    assert stp.orderType == "STP"
    assert stp.auxPrice == 17400.0
    assert stp.tif == "GTC"
    assert stp.parentId == 0
    assert stp.transmit is True


def test_protective_stop_short_is_buy():
    stp = build_protective_stop(
        direction=Direction.SHORT,
        qty=1,
        stop_price=17700.0,
    )
    assert stp.action == "BUY"
    assert stp.auxPrice == 17700.0
    assert stp.tif == "GTC"


def test_protective_stop_rejects_non_positive_qty():
    with pytest.raises(ValueError, match="qty must be positive"):
        build_protective_stop(direction=Direction.LONG, qty=0, stop_price=17400.0)


def test_short_bracket_has_sell_parent_buy_tp():
    parent, tp = build_bracket(
        direction=Direction.SHORT,
        qty=1,
        entry_type="MKT",
        entry_lmt_price=None,
        target_price=17400.0,
    )
    assert parent.action == "SELL"
    assert tp.action == "BUY"


def test_bracket_returns_two_legs_not_three():
    # Option γ — STP is NOT part of the bracket pair (separate GTC stop per §0.5.T2).
    result = build_bracket(
        direction=Direction.LONG,
        qty=2,
        entry_type="MKT",
        entry_lmt_price=None,
        target_price=17600.0,
    )
    assert len(result) == 2


def test_unsupported_entry_type_rejected():
    with pytest.raises(ValueError, match="unsupported entry_type"):
        build_bracket(
            direction=Direction.LONG,
            qty=1,
            entry_type="STP",  # type: ignore[arg-type]
            entry_lmt_price=None,
            target_price=17600.0,
        )
