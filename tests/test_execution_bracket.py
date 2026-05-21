"""Tests for src.execution.bracket — pure construction, no mocks needed."""

from __future__ import annotations

import pytest

from src.execution.bracket import build_bracket, build_protective_stop
from src.state_machine import Direction


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
