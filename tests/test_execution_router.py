"""Tests for src.execution.router — mocked at the IBClient + StateMachine boundary."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.instruments import MNQ
from config.risk_params import RISK
from src.clients.ib_client import IBClient
from src.execution.dirty_set import DirtySet
from src.execution.router import OrderRouter, ensure_protective_stop
from src.state_machine import (
    Direction,
    InvariantViolationError,
    Lifecycle,
    State,
    StateMachine,
)
from src.strategy import Signal

STRAT_NAME = "sma100_bounce"


# -------------------------------------------------------------------- factories


def _make_lifecycle(state: State, **overrides: Any) -> Lifecycle:
    lc = Lifecycle(
        lifecycle_id=str(uuid.uuid4()),
        symbol="MNQM6",
        strategy=STRAT_NAME,
        direction=Direction.LONG.value,
        state=state.value,
    )
    if state in (State.ENTERING, State.ACTIVE, State.EXITING, State.CLOSED):
        lc.entry_order_id = 1001
        lc.target_order_id = 1002
        lc.stop_price = 17400.0
        lc.target_price = 17600.0
    if state in (State.ACTIVE, State.EXITING, State.CLOSED):
        lc.entry_qty = 2
        lc.entry_price = 17500.0
        lc.entry_filled_at = "2026-05-21T15:00:00+00:00"
        lc.stop_order_id = 1003
    for k, v in overrides.items():
        setattr(lc, k, v)
    return lc


def _make_mock_ib() -> AsyncMock:
    ib = AsyncMock(spec=IBClient)
    ib.is_connected = True
    return ib


def _make_mock_sm() -> MagicMock:
    """Use MagicMock not AsyncMock(spec=StateMachine) — easier to coerce returns
    per-call for create_lifecycle/transition. AsyncMock methods built manually.
    """
    sm = MagicMock(spec=StateMachine)
    sm.create_lifecycle = AsyncMock()
    sm.transition = AsyncMock()
    sm.load_non_closed = AsyncMock(return_value=[])
    sm.persist_highest = AsyncMock()  # PR-2 — durable high-water mark
    sm.persist_stop_price = AsyncMock()  # STABILIZE-3 — durable ratcheted stop
    return sm


def _make_signal(target: float = 17600.0, stop: float = 17400.0) -> Signal:
    return Signal(
        instrument="MNQM6",
        direction="LONG",
        entry_price=17500.0,
        stop_price=stop,
        target_price=target,
        ma_fast_value=17510.0,
        ma_slow_value=17480.0,
        ma_gap=30.0,
        adx_value=25.0,
        timestamp=datetime.now(UTC),
    )


def _make_trade(order_id: int) -> MagicMock:
    trade = MagicMock(name=f"Trade<{order_id}>")
    trade.order = MagicMock()
    trade.order.orderId = order_id
    trade.fills = []
    return trade


def _make_trade_with_fill(order_id: int, qty: int, price: float) -> MagicMock:
    trade = _make_trade(order_id)
    fill = MagicMock(name="Fill")
    fill.execution = MagicMock()
    fill.execution.shares = qty
    fill.execution.avgPrice = price
    fill.execution.time = datetime(2026, 5, 21, 15, 0, tzinfo=UTC)
    trade.fills = [fill]
    return trade


def _make_trade_with_status(order_id: int, status: str) -> MagicMock:
    """Trade whose orderStatus.status is set, for _confirm_order_resting() —
    'PreSubmitted'/'Submitted' read as resting, 'Inactive'/'Cancelled' as dead."""
    trade = _make_trade(order_id)
    trade.orderStatus = MagicMock()
    trade.orderStatus.status = status
    return trade


def _make_contract() -> MagicMock:
    c = MagicMock(name="Contract")
    c.localSymbol = "MNQM6"
    c.symbol = "MNQ"
    return c


# -------------------------------------------------------------------- placement


async def test_place_entry_happy_path_creates_lifecycle_and_transitions_to_entering():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    idle_lc = _make_lifecycle(State.IDLE)
    entering_lc = _make_lifecycle(State.ENTERING)
    sm.create_lifecycle.return_value = idle_lc
    sm.transition.return_value = entering_lc

    # PR B — IB.place_order returns parent → stop child → TP child (3 calls).
    # The orderIds become entry_order_id / stop_order_id / target_order_id.
    ib.place_order = AsyncMock(
        side_effect=[
            _make_trade(2001),  # parent
            _make_trade(2002),  # fixed STP child
            _make_trade(2003),  # trailing/fixed TP child
        ]
    )

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    contract = _make_contract()
    signal = _make_signal()

    lc = await router.place_entry(signal, contract)

    assert lc is entering_lc
    assert ib.place_order.await_count == 3
    # PR-B — place_entry now threads the per-bar setup_key (None when not dispatched
    # via _handle_trade_signal, as here) and this entry's qty into create_lifecycle
    # (same-setup dedup + aggregate contract cap).
    sm.create_lifecycle.assert_awaited_once_with(
        "MNQM6", STRAT_NAME, Direction.LONG, setup_key=None, qty=2
    )
    transition_call = sm.transition.await_args
    assert transition_call.args[1] is State.ENTERING
    assert transition_call.kwargs["entry_order_id"] == 2001
    assert transition_call.kwargs["stop_order_id"] == 2002
    assert transition_call.kwargs["target_order_id"] == 2003
    assert transition_call.kwargs["stop_price"] == signal.stop_price
    assert transition_call.kwargs["target_price"] == signal.target_price

    # The two exit legs were placed as ONE native OCA group, both children
    # parented to the entry — the disconnect-survival fix (Task E.1).
    placed_orders = [call.args[1] for call in ib.place_order.await_args_list]
    parent_o, stop_o, tp_o = placed_orders
    assert stop_o.orderType == "STP"
    assert stop_o.parentId == 2001
    assert tp_o.parentId == 2001
    assert stop_o.ocaGroup == tp_o.ocaGroup
    assert stop_o.ocaGroup.startswith("tf-exit-")
    assert stop_o.ocaType == 1 and tp_o.ocaType == 1


async def test_on_fill_parent_does_not_replace_stop_when_oca_child_already_rests():
    # PR B happy path — the protective STP was placed up-front as an OCA bracket
    # child, so lc.stop_order_id is already set at ENTERING. The parent-fill
    # handler must NOT place a second stop (ensure_protective_stop no-op).
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    entering_lc = _make_lifecycle(State.ENTERING, stop_order_id=2002)
    active_lc = _make_lifecycle(State.ACTIVE)
    sm.transition.return_value = active_lc

    ib.place_order = AsyncMock(return_value=_make_trade(9999))

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[entering_lc.entry_order_id] = entering_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[entering_lc.lifecycle_id] = entering_lc
    router._contracts[entering_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(entering_lc.entry_order_id, qty=2, price=17500.5)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    # No new STP placed — the resting OCA child is reused.
    ib.place_order.assert_not_awaited()
    transition_call = sm.transition.await_args
    assert transition_call.args[1] is State.ACTIVE
    assert transition_call.kwargs["stop_order_id"] == 2002


async def test_place_entry_mid_sequence_failure_closes_lifecycle_as_manual():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    idle_lc = _make_lifecycle(State.IDLE)
    sm.create_lifecycle.return_value = idle_lc
    closed_lc = _make_lifecycle(State.CLOSED)
    sm.transition.return_value = closed_lc

    # First placeOrder succeeds (parent), second raises — exactly two calls.
    ib.place_order = AsyncMock(
        side_effect=[
            _make_trade(2001),
            RuntimeError("broker reject — INVALID_LMT"),
        ]
    )

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    with pytest.raises(RuntimeError, match="broker reject"):
        await router.place_entry(_make_signal(), _make_contract())

    # close_pre_active fires exactly once with CLOSED + MANUAL.
    sm.transition.assert_awaited_once()
    args = sm.transition.await_args
    assert args.args[1] is State.CLOSED
    assert args.kwargs["exit_reason"] == "MANUAL"


# --------------------------------------------------------------------- on_fill


async def test_on_fill_parent_places_stp_and_transitions_to_active():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    entering_lc = _make_lifecycle(State.ENTERING)
    active_lc = _make_lifecycle(State.ACTIVE)
    sm.transition.return_value = active_lc

    # STP placement returns its own trade — one call expected during on_fill.
    ib.place_order = AsyncMock(return_value=_make_trade(3001))

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[entering_lc.entry_order_id] = entering_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[entering_lc.lifecycle_id] = entering_lc
    router._contracts[entering_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(entering_lc.entry_order_id, qty=2, price=17500.5)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    ib.place_order.assert_awaited_once()
    transition_call = sm.transition.await_args
    assert transition_call.args[1] is State.ACTIVE
    assert transition_call.kwargs["entry_qty"] == 2
    assert transition_call.kwargs["entry_price"] == 17500.5
    assert transition_call.kwargs["stop_order_id"] == 3001


async def test_on_fill_target_transitions_to_exiting_then_closed_with_target():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)

    # Two transitions: ACTIVE→EXITING (returns exiting lc), EXITING→CLOSED (returns closed lc).
    exiting_lc = _make_lifecycle(State.EXITING)
    closed_lc = _make_lifecycle(State.CLOSED)
    sm.transition = AsyncMock(side_effect=[exiting_lc, closed_lc])  # 2 calls

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[active_lc.target_order_id] = active_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[active_lc.lifecycle_id] = active_lc
    router._contracts[active_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(active_lc.target_order_id, qty=2, price=17600.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    assert sm.transition.await_count == 2
    closed_call = sm.transition.await_args
    assert closed_call.args[1] is State.CLOSED
    assert closed_call.kwargs["exit_reason"] == "TARGET"
    # pnl_gross = (17600 - 17500) * 2 * MNQ.multiplier (2.0) = 400.0
    assert closed_call.kwargs["pnl_gross"] == pytest.approx(
        (17600.0 - 17500.0) * 2 * MNQ.multiplier
    )
    # W-S14.2 Track 4b: commission is charged on BOTH sides — qty * RT-per-contract
    # (= qty * 2 * per-side). pnl_net = gross - commission.
    assert closed_call.kwargs["commission_total"] == pytest.approx(2 * MNQ.commission_rt_usd)
    assert closed_call.kwargs["commission_total"] == pytest.approx(
        2 * 2 * MNQ.commission_per_side_usd
    )
    assert closed_call.kwargs["pnl_net"] == pytest.approx(
        closed_call.kwargs["pnl_gross"] - closed_call.kwargs["commission_total"]
    )


async def test_closed_round_trip_nets_broker_commission_example():
    """W-S14.2 Track 4b broker-match: a 2-ct LONG closed at +150.5 pts nets
    $599.52 (gross 602.00 - both-sides commission 2.48), NOT the old $600.76."""
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    active_lc.entry_price = 30257.5  # the live CLOSED lifecycle c44c5f95
    exiting_lc = _make_lifecycle(State.EXITING)
    closed_lc = _make_lifecycle(State.CLOSED)
    sm.transition = AsyncMock(side_effect=[exiting_lc, closed_lc])

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[active_lc.target_order_id] = active_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[active_lc.lifecycle_id] = active_lc
    router._contracts[active_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(active_lc.target_order_id, qty=2, price=30408.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    closed_call = sm.transition.await_args
    assert closed_call.kwargs["pnl_gross"] == pytest.approx(602.00)
    assert closed_call.kwargs["commission_total"] == pytest.approx(2.48)
    assert closed_call.kwargs["pnl_net"] == pytest.approx(599.52)


async def test_on_fill_stop_transitions_to_closed_with_stop_reason():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    exiting_lc = _make_lifecycle(State.EXITING)
    closed_lc = _make_lifecycle(State.CLOSED)
    sm.transition = AsyncMock(side_effect=[exiting_lc, closed_lc])  # 2 calls

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[active_lc.stop_order_id] = active_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[active_lc.lifecycle_id] = active_lc
    router._contracts[active_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(active_lc.stop_order_id, qty=2, price=17425.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    closed_call = sm.transition.await_args
    assert closed_call.kwargs["exit_reason"] == "STOP"
    # Losing trade: (17425 - 17500) * 2 * 2.0 = -300
    assert closed_call.kwargs["pnl_gross"] == pytest.approx(-300.0)


async def test_on_fill_unknown_order_is_noop():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)

    trade = _make_trade(99999)
    await router.on_fill(trade, None)  # must not raise

    sm.transition.assert_not_awaited()
    ib.place_order.assert_not_awaited()


async def test_on_fill_idempotent_when_state_machine_rejects_duplicate():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    # First transition raises InvariantViolationError to simulate a duplicate
    # fill arriving after CLOSED — exactly one transition call expected.
    sm.transition = AsyncMock(
        side_effect=InvariantViolationError("disallowed transition CLOSED → EXITING")
    )

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[active_lc.target_order_id] = active_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[active_lc.lifecycle_id] = active_lc
    router._contracts[active_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(active_lc.target_order_id, qty=2, price=17600.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)  # must not raise


async def test_register_eod_exit_routes_fill_as_eod():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    exiting_lc = _make_lifecycle(State.EXITING)
    exiting_lc_after = _make_lifecycle(State.EXITING)
    closed_lc = _make_lifecycle(State.CLOSED)
    # EXITING state — first transition skipped (already EXITING); only CLOSED.
    sm.transition = AsyncMock(return_value=closed_lc)  # exactly 1 call

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_lifecycle_id[exiting_lc.lifecycle_id] = exiting_lc_after
    router.register_eod_exit(exiting_lc_after, order_id=4001)

    trade = _make_trade_with_fill(4001, qty=2, price=17480.0)
    await router.on_fill(trade, None)

    sm.transition.assert_awaited_once()
    closed_call = sm.transition.await_args
    assert closed_call.args[1] is State.CLOSED
    assert closed_call.kwargs["exit_reason"] == "EOD"


# ------------------------------------------------------------------ cancel_all


async def test_cancel_all_for_calls_ib_cancel_per_non_null_order_id():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    # ACTIVE lifecycle has entry_order_id, target_order_id, stop_order_id all populated.
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    ib.cancel_order_by_id = AsyncMock(return_value=True)

    await router.cancel_all_for(active_lc)

    assert ib.cancel_order_by_id.await_count == 3


async def test_cancel_all_for_skips_none_order_ids():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    idle_lc = _make_lifecycle(State.IDLE)  # all order ids None
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    ib.cancel_order_by_id = AsyncMock(return_value=True)

    await router.cancel_all_for(idle_lc)
    ib.cancel_order_by_id.assert_not_awaited()


async def test_cancel_all_for_swallows_errors():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    # 3 cancel attempts; raise on the second to confirm subsequent ones still run.
    # 3 cancel attempts; raise on the second to confirm the router's swallow-guard
    # keeps the subsequent ones running.
    ib.cancel_order_by_id = AsyncMock(side_effect=[True, RuntimeError("cancel rejected"), True])

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    await router.cancel_all_for(active_lc)  # must not raise

    assert ib.cancel_order_by_id.await_count == 3


async def test_manual_flatten_trailing_cancels_standalone_stop_then_market_exits():
    # STABILIZE-5: a manual /flatten on a trailing ACTIVE position (standalone STP
    # 1003, NO resting TARGET) must cancel the standalone STP — so no SELL leg is left
    # resting once the position is flattened — then place the market exit. There is no
    # OCA to auto-cancel the stop, so close_position's cancel_all_for is the guarantee.
    ib = _make_mock_ib()
    ib.cancel_order_by_id = AsyncMock(return_value=True)
    ib.place_order = AsyncMock(return_value=_make_trade(7001))  # the MKT exit
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE, target_order_id=None)  # trailing shape
    sm.load_non_closed = AsyncMock(return_value=[active_lc])
    sm.transition.return_value = _make_lifecycle(State.EXITING, target_order_id=None)

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._contracts[active_lc.lifecycle_id] = _make_contract()

    result = await router.close_position("MNQM6", reason="manual_flatten")

    assert result.status == "exit_submitted"
    # The standalone STP was cancelled (no orphan); the None target is skipped.
    assert active_lc.stop_order_id in _cancelled_order_ids(ib)
    # A market exit order was placed to flatten.
    exit_order = ib.place_order.await_args.args[1]
    assert exit_order.orderType == "MKT"
    assert exit_order.action == "SELL"  # closing a LONG


# ------------------------------------------------ W-S15.1 sibling-cancel on exit


def _cancelled_order_ids(ib: AsyncMock) -> list[int]:
    """The order ids passed to ``ib.cancel_order_by_id``, in call order.

    ``cancel_order_by_id`` takes the int order id directly (PR #72 — it looks up
    the real ``Trade`` internally), so ``call.args[0]`` is the id itself.
    """
    return [call.args[0] for call in ib.cancel_order_by_id.await_args_list]


async def test_target_fill_cancels_resting_stop_sibling():
    # W-S15.1: a TARGET (LMT) fill must cancel the standalone protective STP so
    # no SELL leg is left resting with no position behind it.
    ib = _make_mock_ib()
    ib.cancel_order_by_id = AsyncMock(return_value=True)
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)  # target=1002, stop=1003
    sm.transition = AsyncMock(
        side_effect=[_make_lifecycle(State.EXITING), _make_lifecycle(State.CLOSED)]
    )

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[active_lc.target_order_id] = active_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[active_lc.lifecycle_id] = active_lc
    router._contracts[active_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(active_lc.target_order_id, qty=2, price=17600.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    # Exactly the STP (1003) cancelled; the filled TP (1002) is NOT re-cancelled.
    assert _cancelled_order_ids(ib) == [active_lc.stop_order_id]


async def test_stop_fill_cancels_resting_target_sibling():
    # W-S15.1: a STOP fill must cancel the resting TP LMT.
    ib = _make_mock_ib()
    ib.cancel_order_by_id = AsyncMock(return_value=True)
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    sm.transition = AsyncMock(
        side_effect=[_make_lifecycle(State.EXITING), _make_lifecycle(State.CLOSED)]
    )

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[active_lc.stop_order_id] = active_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[active_lc.lifecycle_id] = active_lc
    router._contracts[active_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(active_lc.stop_order_id, qty=2, price=17400.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    assert _cancelled_order_ids(ib) == [active_lc.target_order_id]


async def test_manual_market_exit_cancels_both_remaining_children():
    # W-S15.1: a MANUAL/EOD market exit (separate order) clears BOTH children.
    ib = _make_mock_ib()
    ib.cancel_order_by_id = AsyncMock(return_value=True)
    sm = _make_mock_sm()
    exiting_lc = _make_lifecycle(State.EXITING)  # target=1002, stop=1003
    sm.transition = AsyncMock(
        return_value=_make_lifecycle(State.CLOSED)
    )  # 1 call (already EXITING)

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_lifecycle_id[exiting_lc.lifecycle_id] = exiting_lc
    router.register_manual_exit(exiting_lc, order_id=5001)
    router._contracts[exiting_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(5001, qty=2, price=17500.0)
    await router.on_fill(trade, None)

    # Both bracket children cancelled; the MKT exit order (5001) is not.
    assert set(_cancelled_order_ids(ib)) == {
        exiting_lc.target_order_id,
        exiting_lc.stop_order_id,
    }


async def test_sibling_cancel_is_safe_noop_when_order_already_gone():
    # W-S15.1: an already-filled/cancelled sibling is a no-op (cancel_order_by_id
    # returns False when the id isn't among live trades) — must not raise.
    ib = _make_mock_ib()
    ib.cancel_order_by_id = AsyncMock(return_value=False)  # not live / already gone
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    sm.transition = AsyncMock(
        side_effect=[_make_lifecycle(State.EXITING), _make_lifecycle(State.CLOSED)]
    )

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[active_lc.target_order_id] = active_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[active_lc.lifecycle_id] = active_lc
    router._contracts[active_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(active_lc.target_order_id, qty=2, price=17600.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)  # must not raise

    # Cancel was attempted, the close still completed (CLOSED transition ran).
    assert ib.cancel_order_by_id.await_count == 1
    assert sm.transition.await_args.args[1] is State.CLOSED


# -------------------------------------------------------------------- recovery


def test_register_recovered_skips_closed_lifecycles():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    closed_lc = _make_lifecycle(State.CLOSED)
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)

    router.register_recovered(closed_lc, _make_contract())

    assert closed_lc.lifecycle_id not in router._by_lifecycle_id


def test_register_recovered_indexes_by_every_non_null_order_id():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)

    router.register_recovered(active_lc, _make_contract())

    assert router._by_lifecycle_id[active_lc.lifecycle_id] is active_lc
    assert router._by_order_id[active_lc.entry_order_id] is active_lc  # type: ignore[index]
    assert router._by_order_id[active_lc.target_order_id] is active_lc  # type: ignore[index]
    assert router._by_order_id[active_lc.stop_order_id] is active_lc  # type: ignore[index]


# ---------------------------------------------------------------- dirty_set hook


async def test_place_entry_marks_lifecycle_dirty():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    idle_lc = _make_lifecycle(State.IDLE)
    entering_lc = _make_lifecycle(State.ENTERING)
    sm.create_lifecycle.return_value = idle_lc
    sm.transition.return_value = entering_lc
    ib.place_order = AsyncMock(
        side_effect=[_make_trade(2001), _make_trade(2002), _make_trade(2003)]
    )

    dirty = DirtySet()
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME, dirty_set=dirty)
    await router.place_entry(_make_signal(), _make_contract())

    assert entering_lc.lifecycle_id in dirty


async def test_on_fill_parent_marks_lifecycle_dirty():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    entering_lc = _make_lifecycle(State.ENTERING)
    active_lc = _make_lifecycle(State.ACTIVE)
    sm.transition.return_value = active_lc
    ib.place_order = AsyncMock(return_value=_make_trade(3001))

    dirty = DirtySet()
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME, dirty_set=dirty)
    router._by_order_id[entering_lc.entry_order_id] = entering_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[entering_lc.lifecycle_id] = entering_lc
    router._contracts[entering_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(entering_lc.entry_order_id, qty=2, price=17500.5)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    assert active_lc.lifecycle_id in dirty


async def test_on_fill_exit_marks_lifecycle_dirty():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    exiting_lc = _make_lifecycle(State.EXITING)
    closed_lc = _make_lifecycle(State.CLOSED)
    sm.transition = AsyncMock(side_effect=[exiting_lc, closed_lc])

    dirty = DirtySet()
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME, dirty_set=dirty)
    router._by_order_id[active_lc.target_order_id] = active_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[active_lc.lifecycle_id] = active_lc
    router._contracts[active_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(active_lc.target_order_id, qty=2, price=17600.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    assert closed_lc.lifecycle_id in dirty


async def test_router_without_dirty_set_is_safe_to_use():
    """Back-compat: omit dirty_set → no-op _mark_dirty, no crash."""
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    idle_lc = _make_lifecycle(State.IDLE)
    entering_lc = _make_lifecycle(State.ENTERING)
    sm.create_lifecycle.return_value = idle_lc
    sm.transition.return_value = entering_lc
    ib.place_order = AsyncMock(
        side_effect=[_make_trade(2001), _make_trade(2002), _make_trade(2003)]
    )

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)  # no dirty_set
    lc = await router.place_entry(_make_signal(), _make_contract())  # must not raise
    assert lc is entering_lc


# ------------------------------------- EXIT_MODE=trailing bar-close ratchet (V3/V12)
# The bot walks the single resting GTC SELL STP UP only on each closed bar: +50
# lock-in, then trail at +200; modify-in-place (re-placeOrder same id); verify-LONG
# from the broker before touching the stop; never lowers it; market-exit at +1000.


def _trailing(monkeypatch):
    monkeypatch.setattr("src.execution.router.RISK", replace(RISK, exit_mode="trailing"))


def _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=19925.0) -> Lifecycle:
    lc = _make_lifecycle(State.ACTIVE)
    lc.entry_price = entry
    lc.stop_order_id = stop_order_id
    lc.stop_price = stop_price
    return lc


def _stop_order(order_id: int, aux: float) -> MagicMock:
    o = MagicMock(name=f"Order<{order_id}>")
    o.orderId = order_id
    o.auxPrice = aux
    return o


def _position(symbol="MNQM6", qty=2.0) -> MagicMock:
    p = MagicMock(name="Position")
    p.contract = MagicMock()
    p.contract.localSymbol = symbol
    p.contract.symbol = "MNQ"
    p.position = qty
    return p


def _wire_active(router: OrderRouter, lc: Lifecycle, *, seed_highest: float | None = None):
    router._by_lifecycle_id[lc.lifecycle_id] = lc
    router._contracts[lc.lifecycle_id] = _make_contract()
    if seed_highest is not None:
        router._highest[lc.lifecycle_id] = seed_highest


async def test_ratchet_locks_in_at_plus_50(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=19925.0)
    ib.find_open_order_by_id = AsyncMock(return_value=_stop_order(2002, 19925.0))
    ib.get_positions = AsyncMock(return_value=[_position("MNQM6", 2.0)])
    ib.place_order = AsyncMock(return_value=_make_trade_with_status(2002, "PreSubmitted"))
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc, seed_highest=20000.0)

    # Bar runs to +50 (high 20050) → lock-in to entry+50.
    await router.ratchet_stop_on_bar(bar_high=20050.0, bar_low=20030.0, bar_close=20040.0)

    ib.get_positions.assert_awaited()  # verify-LONG before touching the stop
    ib.place_order.assert_awaited_once()  # modify-in-place
    modified = ib.place_order.await_args.args[1]
    assert modified.orderId == 2002  # SAME order id → one resting stop (no naked window)
    assert modified.auxPrice == 20050.0  # entry + lock_in_pts (50)
    # in-memory stop_price cached after the confirmed modify.
    assert router._by_lifecycle_id[lc.lifecycle_id].stop_price == 20050.0


async def test_ratchet_issues_broker_modify_and_persists_ratcheted_level(monkeypatch):
    # STABILIZE-3 (the money gate): a ratchet decision must ACTUALLY issue a broker
    # modify carrying the new aux AND persist the ratcheted level — not merely update
    # in-memory state / emit an alert. (The losing trade announced +50 while the
    # broker stop stayed at base because the modify was silently rejected.)
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=19925.0)
    live = _stop_order(2002, 19925.0)
    ib.find_open_order_by_id = AsyncMock(return_value=live)
    ib.get_positions = AsyncMock(return_value=[_position()])
    ib.place_order = AsyncMock(return_value=_make_trade_with_status(2002, "PreSubmitted"))
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc, seed_highest=20000.0)

    await router.ratchet_stop_on_bar(bar_high=20050.0, bar_low=20030.0, bar_close=20040.0)

    # A broker modify was ISSUED, same order id, with the new aux (broker-side change).
    ib.place_order.assert_awaited_once()
    modified = ib.place_order.await_args.args[1]
    assert modified.orderId == 2002
    assert modified.auxPrice == 20050.0  # entry + lock_in_pts (50)
    # The ratcheted level is PERSISTED so the reconciler's leg-heal reads it (not base).
    sm.persist_stop_price.assert_awaited_once()
    _persisted_lc, persisted_stop = sm.persist_stop_price.await_args.args
    assert persisted_stop == 20050.0


async def test_ratchet_modify_unconfirmed_does_not_announce_or_persist(monkeypatch):
    # STABILIZE-3 regression for the real bleed: a modify that the broker SILENTLY
    # cancels (Error 10326 "OCA group revision is not allowed") leaves the resting
    # order gone. The bot must NOT update its cached/persisted stop — it must never
    # claim a protective level the broker does not hold. Broker truth (a re-read of
    # the live order) is the gate, not the Trade's stale "resting" status.
    _trailing(monkeypatch)
    monkeypatch.setattr("src.execution.router.asyncio.sleep", AsyncMock())  # no real delay
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=19925.0)
    live = _stop_order(2002, 19925.0)
    calls = {"n": 0}

    async def _find(_order_id):  # 1st read → live order; confirm re-read → GONE (cancelled)
        calls["n"] += 1
        return live if calls["n"] == 1 else None

    ib.find_open_order_by_id = AsyncMock(side_effect=_find)
    ib.get_positions = AsyncMock(return_value=[_position()])
    ib.place_order = AsyncMock(return_value=_make_trade_with_status(2002, "PreSubmitted"))
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc, seed_highest=20000.0)

    await router.ratchet_stop_on_bar(bar_high=20050.0, bar_low=20030.0, bar_close=20040.0)

    ib.place_order.assert_awaited_once()  # the modify was attempted
    sm.persist_stop_price.assert_not_awaited()  # but NOT persisted (broker never confirmed)
    # Cache keeps the prior stop — no phantom +50 the broker isn't holding.
    assert router._by_lifecycle_id[lc.lifecycle_id].stop_price == 19925.0


async def test_ratchet_does_not_lower_the_stop(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=20050.0)
    # Broker stop already at the lock (+50); highest already 20060.
    ib.find_open_order_by_id = AsyncMock(return_value=_stop_order(2002, 20050.0))
    ib.get_positions = AsyncMock(return_value=[_position()])
    ib.place_order = AsyncMock()
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc, seed_highest=20060.0)

    # A lower bar (high 20055 < prior 20060) → computed target (20050) not > current.
    await router.ratchet_stop_on_bar(bar_high=20055.0, bar_low=20020.0, bar_close=20030.0)

    ib.place_order.assert_not_awaited()  # never lowers / re-submits
    ib.get_positions.assert_not_awaited()  # short-circuits before the verify step


async def test_ratchet_modify_failure_keeps_prior_stop(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=19925.0)
    ib.find_open_order_by_id = AsyncMock(return_value=_stop_order(2002, 19925.0))
    ib.get_positions = AsyncMock(return_value=[_position()])
    ib.place_order = AsyncMock(side_effect=RuntimeError("Error 321 reject"))  # 1 call, raises
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc, seed_highest=20000.0)

    await router.ratchet_stop_on_bar(
        bar_high=20050.0, bar_low=20030.0, bar_close=20040.0
    )  # no raise

    # The prior stop price is unchanged in the cache (modify did not land).
    assert router._by_lifecycle_id[lc.lifecycle_id].stop_price == 19925.0


async def test_ratchet_noop_in_fixed_mode(monkeypatch):
    monkeypatch.setattr("src.execution.router.RISK", replace(RISK, exit_mode="fixed"))
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc()
    ib.find_open_order_by_id = AsyncMock()
    ib.place_order = AsyncMock()
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc, seed_highest=20000.0)

    await router.ratchet_stop_on_bar(bar_high=20050.0, bar_low=20030.0, bar_close=20040.0)

    ib.find_open_order_by_id.assert_not_awaited()  # fixed mode → never ratchets
    ib.place_order.assert_not_awaited()


async def test_recovered_active_seeds_highest_and_ratchets(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=19925.0)
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)

    router.register_recovered(lc, _make_contract())  # seeds highest to entry
    assert router._highest[lc.lifecycle_id] == 20000.0

    ib.find_open_order_by_id = AsyncMock(return_value=_stop_order(2002, 19925.0))
    ib.get_positions = AsyncMock(return_value=[_position()])
    ib.place_order = AsyncMock(return_value=_make_trade_with_status(2002, "Submitted"))
    await router.ratchet_stop_on_bar(bar_high=20055.0, bar_low=20030.0, bar_close=20045.0)

    ib.place_order.assert_awaited_once()
    assert (
        ib.place_order.await_args.args[1].auxPrice == 20050.0
    )  # recovered position still ratchets


async def test_hard_ceiling_triggers_market_close(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(entry=20000.0)
    ib.find_open_order_by_id = AsyncMock()
    ib.place_order = AsyncMock()
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc, seed_highest=20000.0)
    router.close_position = AsyncMock(return_value=None)  # spy on the market exit

    # close >= entry + hard_ceiling_pts (1000) → market exit.
    await router.ratchet_stop_on_bar(bar_high=21010.0, bar_low=20990.0, bar_close=21005.0)

    router.close_position.assert_awaited_once()
    ib.find_open_order_by_id.assert_not_awaited()  # hard exit short-circuits the stop ratchet


# -------------------------------------- PR-2: persist the high-water mark (durable)
# `highest` is persisted to metadata.highest_price on each advance + at the fill seed;
# recovery seeds from the persisted peak (clamped to never sit losing-side of entry).


async def test_ratchet_persists_highest_when_it_advances(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=19925.0)
    ib.find_open_order_by_id = AsyncMock(return_value=_stop_order(2002, 19925.0))
    ib.get_positions = AsyncMock(return_value=[_position()])
    ib.place_order = AsyncMock(return_value=_make_trade_with_status(2002, "PreSubmitted"))
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc, seed_highest=20000.0)

    # A new high (20050 > seed 20000) → highest advances → persisted durably.
    await router.ratchet_stop_on_bar(bar_high=20050.0, bar_low=20030.0, bar_close=20040.0)

    sm.persist_highest.assert_awaited_once()
    persisted_lc, persisted_high = sm.persist_highest.await_args.args
    assert persisted_lc is lc and persisted_high == 20050.0


async def test_ratchet_does_not_persist_when_highest_unchanged(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=20050.0)
    ib.find_open_order_by_id = AsyncMock(return_value=_stop_order(2002, 20050.0))
    ib.place_order = AsyncMock()
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc, seed_highest=20060.0)

    # A lower bar (high 20055 < prior 20060) → no new high → no persist.
    await router.ratchet_stop_on_bar(bar_high=20055.0, bar_low=20020.0, bar_close=20030.0)

    sm.persist_highest.assert_not_awaited()


async def test_recovery_seeds_highest_from_persisted_metadata(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=20050.0)
    lc.metadata = {"highest_price": 20120.0}  # peak persisted before the restart
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)

    router.register_recovered(lc, _make_contract())
    assert router._highest[lc.lifecycle_id] == 20120.0  # resumes from the true peak

    # And it ratchets off the persisted peak: peak 120 → lock entry+50.
    ib.find_open_order_by_id = AsyncMock(return_value=_stop_order(2002, 20050.0))
    ib.get_positions = AsyncMock(return_value=[_position()])
    ib.place_order = AsyncMock(return_value=_make_trade_with_status(2002, "Submitted"))
    # A flat bar below the peak keeps highest=20120; current stop already at lock →
    # no further ratchet, but the seed proves recovery used the persisted value.
    await router.ratchet_stop_on_bar(bar_high=20100.0, bar_low=20090.0, bar_close=20095.0)
    assert router._highest[lc.lifecycle_id] == 20120.0


async def test_recovery_absent_persisted_falls_back_to_entry(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(entry=20000.0)
    lc.metadata = {}  # nothing persisted
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)

    router.register_recovered(lc, _make_contract())
    assert router._highest[lc.lifecycle_id] == 20000.0  # entry fallback


async def test_recovery_persisted_below_entry_clamped_to_entry(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc = _active_long_lc(entry=20000.0)
    lc.metadata = {"highest_price": 19950.0}  # impossibly below entry for a LONG peak
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)

    router.register_recovered(lc, _make_contract())
    # Clamped to entry so it can NEVER seed a peak that would un-ratchet the stop.
    assert router._highest[lc.lifecycle_id] == 20000.0


# ----------------------------------------- PR-A — ratchet/recovery over N positions


async def test_ratchet_trails_two_lifecycles_independently_on_their_own_highest(monkeypatch):
    # PR-A — with two simultaneous open positions, the bar-close ratchet must walk
    # EACH lifecycle's own standalone STP off ITS own high-water mark, not a single
    # shared position. Same bar, different seeded peaks → different ratcheted levels,
    # exactly one broker modify per lifecycle.
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc_a = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=19925.0)
    lc_b = _active_long_lc(stop_order_id=3003, entry=20000.0, stop_price=19925.0)
    order_a = _stop_order(2002, 19925.0)
    order_b = _stop_order(3003, 19925.0)
    by_id = {2002: order_a, 3003: order_b}

    async def _find(order_id):  # live STP per id; the same object is mutated by the modify
        return by_id.get(order_id)

    ib.find_open_order_by_id = AsyncMock(side_effect=_find)
    ib.get_positions = AsyncMock(return_value=[_position("MNQM6", 4.0)])  # both legs in-position
    ib.place_order = AsyncMock(return_value=_make_trade_with_status(0, "PreSubmitted"))
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    # Distinct seeded peaks: A at +100 (→ lock-in 20050), B at +300 (→ trail 20150).
    _wire_active(router, lc_a, seed_highest=20100.0)
    _wire_active(router, lc_b, seed_highest=20300.0)

    # Bar high 20050 is below BOTH seeded peaks → each keeps its own highest and so
    # computes off its OWN peak (proves no shared-bar / single-position coupling).
    await router.ratchet_stop_on_bar(bar_high=20050.0, bar_low=20030.0, bar_close=20040.0)

    # One broker modify per lifecycle, each carrying ITS own ratcheted aux.
    assert ib.place_order.await_count == 2
    moved = {call.args[1].orderId: call.args[1].auxPrice for call in ib.place_order.await_args_list}
    assert moved == {2002: 20050.0, 3003: 20150.0}
    # Each lifecycle's cached stop advanced to its own independent level.
    assert router._by_lifecycle_id[lc_a.lifecycle_id].stop_price == 20050.0
    assert router._by_lifecycle_id[lc_b.lifecycle_id].stop_price == 20150.0
    assert sm.persist_stop_price.await_count == 2


async def test_ratchet_isolates_a_per_lifecycle_failure(monkeypatch):
    # PR-A — a failure ratcheting one lifecycle must NOT stop the others (per-lc
    # try/except). lc_a's modify raises; lc_b must still ratchet to its own level.
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    lc_a = _active_long_lc(stop_order_id=2002, entry=20000.0, stop_price=19925.0)
    lc_b = _active_long_lc(stop_order_id=3003, entry=20000.0, stop_price=19925.0)
    order_a = _stop_order(2002, 19925.0)
    order_b = _stop_order(3003, 19925.0)
    by_id = {2002: order_a, 3003: order_b}

    async def _find(order_id):  # live STP per id
        return by_id.get(order_id)

    async def _place(_contract, order):  # lc_a's modify fails; lc_b's succeeds
        if order.orderId == 2002:
            raise RuntimeError("Error 321 reject")
        return _make_trade_with_status(order.orderId, "PreSubmitted")

    ib.find_open_order_by_id = AsyncMock(side_effect=_find)
    ib.get_positions = AsyncMock(return_value=[_position("MNQM6", 4.0)])
    ib.place_order = AsyncMock(side_effect=_place)
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    _wire_active(router, lc_a, seed_highest=20100.0)
    _wire_active(router, lc_b, seed_highest=20300.0)

    await router.ratchet_stop_on_bar(bar_high=20050.0, bar_low=20030.0, bar_close=20040.0)

    # lc_a kept its prior stop (modify failed); lc_b still ratcheted to its level.
    assert router._by_lifecycle_id[lc_a.lifecycle_id].stop_price == 19925.0
    assert router._by_lifecycle_id[lc_b.lifecycle_id].stop_price == 20150.0


def test_register_recovered_arms_each_active_lifecycle():
    # PR-A — boot recovery must arm the ratchet for EVERY recovered ACTIVE lifecycle
    # (N, not one): each gets its own high-water seed + contract + cache entry.
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    lc_a = _active_long_lc(stop_order_id=2002, entry=20000.0)
    lc_a.metadata = {"highest_price": 20120.0}
    lc_b = _active_long_lc(stop_order_id=3003, entry=21000.0)
    lc_b.metadata = {"highest_price": 21080.0}

    router.register_recovered(lc_a, _make_contract())
    router.register_recovered(lc_b, _make_contract())

    assert router._highest[lc_a.lifecycle_id] == 20120.0  # each off its OWN peak
    assert router._highest[lc_b.lifecycle_id] == 21080.0
    assert {lc_a.lifecycle_id, lc_b.lifecycle_id} <= set(router._by_lifecycle_id)
    assert {lc_a.lifecycle_id, lc_b.lifecycle_id} <= set(router._contracts)


# -------------------------------------- PR-3: per-entry exit-mode log + mismatch guard


async def test_place_entry_logs_exit_mode_and_bracket_shape(caplog):
    """PR-3: every entry logs `entry exit_mode=<mode> bracket=<shape>` so the active
    exit path is visible in the logs (no broker probe needed). Fixed mode → fixed."""
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    sm.create_lifecycle.return_value = _make_lifecycle(State.IDLE)
    sm.transition.return_value = _make_lifecycle(State.ENTERING)
    ib.place_order = AsyncMock(
        side_effect=[_make_trade(2001), _make_trade(2002), _make_trade(2003)]  # 3 legs (fixed)
    )
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)

    with caplog.at_level(logging.INFO, logger="src.execution.router"):
        await router.place_entry(_make_signal(), _make_contract())

    entry_logs = [r.getMessage() for r in caplog.records if "entry exit_mode=" in r.getMessage()]
    assert entry_logs, "expected a per-entry [EXEC] entry exit_mode=... log"
    assert "exit_mode=fixed" in entry_logs[0] and "bracket=fixed" in entry_logs[0]
    # Consistent case → no mismatch warning.
    assert not [r for r in caplog.records if "exit_mode_mismatch" in r.getMessage()]


async def test_place_entry_exit_mode_mismatch_warns_but_does_not_crash(monkeypatch, caplog):
    """PR-3 guard: if the built bracket SHAPE disagrees with the resolved EXIT_MODE
    (env/build drift), log a loud exit_mode_mismatch WARNING but still place the
    entry — surface the anomaly, never crash."""
    _trailing(monkeypatch)  # RISK.exit_mode = trailing

    from ib_async import Order

    from src.execution import router as router_mod

    def _fake_build(**kwargs):
        # Return a FIXED-shaped bracket (tp_child set) under trailing → mismatch.
        return Order(), Order(), Order()

    monkeypatch.setattr(router_mod, "build_entry_oca_bracket", _fake_build)

    ib = _make_mock_ib()
    sm = _make_mock_sm()
    sm.create_lifecycle.return_value = _make_lifecycle(State.IDLE)
    entering_lc = _make_lifecycle(State.ENTERING)
    sm.transition.return_value = entering_lc
    ib.place_order = AsyncMock(
        side_effect=[_make_trade(2001), _make_trade(2002), _make_trade(2003)]
    )
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)

    with caplog.at_level(logging.WARNING, logger="src.execution.router"):
        lc = await router.place_entry(_make_signal(), _make_contract())  # must NOT raise

    assert lc is entering_lc  # entry still proceeds
    mismatch = [r.getMessage() for r in caplog.records if "exit_mode_mismatch" in r.getMessage()]
    assert mismatch, "expected an exit_mode_mismatch WARNING"
    assert "EXIT_MODE=trailing" in mismatch[0] and "fixed bracket" in mismatch[0]


# --------------------------- PR 99: entry bracket shape honors exit_mode (§0.5.196)


async def test_place_entry_trailing_places_parent_only_stp_deferred(monkeypatch):
    """STABILIZE-5: trailing entry places the parent ALONE — NO bracket-child STP and
    no LMT TARGET. The protective STP is deferred to a standalone post-fill placement
    (parentId=0), so place_entry makes exactly ONE place_order call and the ENTERING
    transition records both stop_order_id and target_order_id as None."""
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    sm.create_lifecycle.return_value = _make_lifecycle(State.IDLE)
    sm.transition.return_value = _make_lifecycle(
        State.ENTERING, stop_order_id=None, target_order_id=None
    )
    # Exactly ONE leg in trailing (the parent). A 2nd place_order would raise
    # StopIteration — proving neither a bracket-child STP nor an LMT is placed here.
    ib.place_order = AsyncMock(side_effect=[_make_trade(2001)])

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    await router.place_entry(_make_signal(), _make_contract())

    assert ib.place_order.await_count == 1  # parent only
    parent = ib.place_order.await_args_list[0].args[1]
    assert parent.orderType == "MKT"
    assert parent.transmit is True  # parent is the sole transmitting leg
    transition = sm.transition.await_args
    assert transition.kwargs["stop_order_id"] is None  # STP placed post-fill, standalone
    assert transition.kwargs["target_order_id"] is None  # no resting TARGET in trailing


async def test_on_fill_trailing_places_standalone_ungrouped_stp(monkeypatch):
    """STABILIZE-5: on the trailing parent fill (lc.stop_order_id is None), the router
    places the protective STP STANDALONE — parentId=0, NO ocaGroup, stop-MARKET — so
    the gateway can't auto-OCA it and the bar-close ratchet can modify it freely."""
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    # Trailing ENTERING row has NO stop_order_id (deferred to this fill handler).
    entering_lc = _make_lifecycle(State.ENTERING, stop_order_id=None, target_order_id=None)
    active_lc = _make_lifecycle(State.ACTIVE)
    active_lc.lifecycle_id = entering_lc.lifecycle_id
    sm.transition.return_value = active_lc
    ib.place_order = AsyncMock(return_value=_make_trade(3001))  # the standalone STP

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[entering_lc.entry_order_id] = entering_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[entering_lc.lifecycle_id] = entering_lc
    router._contracts[entering_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(entering_lc.entry_order_id, qty=2, price=20000.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    # Exactly one STP placed, standalone + ungrouped.
    ib.place_order.assert_awaited_once()
    stp = ib.place_order.await_args.args[1]
    assert stp.orderType == "STP"  # stop-MARKET, never STP LMT
    assert stp.parentId == 0  # standalone — gateway can't auto-OCA it
    assert not stp.ocaGroup
    assert getattr(stp, "ocaType", 0) == 0
    # The new standalone STP id lands on the ACTIVE transition.
    assert sm.transition.await_args.kwargs["stop_order_id"] == 3001


async def test_place_entry_fixed_places_stp_and_lmt():
    """Regression lock: fixed-mode entry still places the full {STP, LMT} OCA bracket
    (parent + STP + LMT). Default RISK.exit_mode is 'fixed' — no monkeypatch needed."""
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    sm.create_lifecycle.return_value = _make_lifecycle(State.IDLE)
    sm.transition.return_value = _make_lifecycle(State.ENTERING)
    ib.place_order = AsyncMock(
        side_effect=[_make_trade(2001), _make_trade(2002), _make_trade(2003)]
    )

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    await router.place_entry(_make_signal(), _make_contract())

    assert ib.place_order.await_count == 3  # parent + STP + LMT
    types = [call.args[1].orderType for call in ib.place_order.await_args_list]
    assert types.count("STP") == 1 and types.count("LMT") == 1  # full bracket
    assert sm.transition.await_args.kwargs["target_order_id"] == 2003


async def test_on_fill_trailing_seeds_and_persists_highest(monkeypatch):
    _trailing(monkeypatch)
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    # STABILIZE-5: trailing ENTERING row has no stop_order_id (the standalone STP is
    # placed on this fill); highest seeding is independent of that placement.
    entering_lc = _make_lifecycle(State.ENTERING, stop_order_id=None, target_order_id=None)
    active_lc = _make_lifecycle(State.ACTIVE)
    active_lc.lifecycle_id = entering_lc.lifecycle_id  # same lifecycle (mirror real transition)
    sm.transition.return_value = active_lc
    ib.place_order = AsyncMock(return_value=_make_trade(9999))  # the standalone STP

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[entering_lc.entry_order_id] = entering_lc  # type: ignore[arg-type]
    router._by_lifecycle_id[entering_lc.lifecycle_id] = entering_lc
    router._contracts[entering_lc.lifecycle_id] = _make_contract()

    trade = _make_trade_with_fill(entering_lc.entry_order_id, qty=2, price=20000.0)  # type: ignore[arg-type]
    await router.on_fill(trade, None)

    # The fill seeds the in-memory peak AND persists it durably for restart recovery.
    assert router._highest[entering_lc.lifecycle_id] == 20000.0
    sm.persist_highest.assert_awaited_once()
    assert sm.persist_highest.await_args.args[1] == 20000.0


# ----------------------------------------- force-fill STP exchange (Error 321)


def _make_contract_with_exchange(exchange: str | None) -> MagicMock:
    """A bare contract whose ``exchange`` is set EXPLICITLY (a plain MagicMock
    auto-creates a truthy attr, masking the missing-exchange case). ``""`` mirrors
    the IB.positions() force-fill contract that triggered Error 321."""
    c = _make_contract()
    c.exchange = exchange
    return c


async def test_ensure_protective_stop_defaults_missing_exchange_to_cme():
    # Force-fill-style contract (from IB.positions()) carries no exchange → the STP
    # placed on it was cancelled by IBKR Error 321. The fix normalizes it to CME
    # BEFORE place_order, so the order that reaches the broker is valid.
    ib = _make_mock_ib()
    ib.place_order = AsyncMock(return_value=_make_trade(154))
    contract = _make_contract_with_exchange("")
    lifecycle = _make_lifecycle(State.ACTIVE)

    stop_id = await ensure_protective_stop(
        ib,
        contract,
        lifecycle,
        qty=2,
        existing_stop_is_live=False,
        component="RECON",
        path="recon_force_fill",
    )

    ib.place_order.assert_awaited_once()
    placed_contract = ib.place_order.await_args.args[0]
    assert placed_contract.exchange == "CME"  # no Error 321 — broker accepts it
    assert stop_id == 154


async def test_ensure_protective_stop_preserves_existing_exchange():
    # Idempotent: a contract that already carries an exchange is left untouched
    # (never override). GLOBEX is a sentinel distinct from CME to prove no clobber.
    ib = _make_mock_ib()
    ib.place_order = AsyncMock(return_value=_make_trade(160))
    contract = _make_contract_with_exchange("GLOBEX")
    lifecycle = _make_lifecycle(State.ACTIVE)

    await ensure_protective_stop(
        ib,
        contract,
        lifecycle,
        qty=2,
        existing_stop_is_live=False,
        component="RECON",
        path="recon_force_fill",
    )

    placed_contract = ib.place_order.await_args.args[0]
    assert placed_contract.exchange == "GLOBEX"  # unchanged


async def test_ensure_protective_stop_returns_live_placed_id_for_caller_to_cache():
    # The id the caller caches (and the ratchet then tracks) is the orderId of the
    # order that was actually placed — i.e. a LIVE resting id from the start, not a
    # dead/stale id. With the exchange defaulted, that placement is not rejected.
    ib = _make_mock_ib()
    placed_trade = _make_trade(207)
    ib.place_order = AsyncMock(return_value=placed_trade)
    contract = _make_contract_with_exchange(None)
    lifecycle = _make_lifecycle(State.ACTIVE)

    stop_id = await ensure_protective_stop(
        ib,
        contract,
        lifecycle,
        qty=2,
        existing_stop_is_live=False,
        component="RECON",
        path="recon_force_fill",
    )

    assert stop_id == placed_trade.order.orderId == 207
