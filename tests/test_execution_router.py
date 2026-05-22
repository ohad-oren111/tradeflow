"""Tests for src.execution.router — mocked at the IBClient + StateMachine boundary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.instruments import MNQ
from src.clients.ib_client import IBClient
from src.execution.dirty_set import DirtySet
from src.execution.router import OrderRouter
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

    # IB.place_order returns the parent trade then the TP trade.
    # The exact orderIds become the entry_order_id / target_order_id assignment.
    ib.place_order = AsyncMock(
        side_effect=[
            _make_trade(2001),  # parent fill — exactly 1 call
            _make_trade(2002),  # TP child — exactly 1 call
        ]
    )

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    contract = _make_contract()
    signal = _make_signal()

    lc = await router.place_entry(signal, contract)

    assert lc is entering_lc
    assert ib.place_order.await_count == 2
    sm.create_lifecycle.assert_awaited_once_with("MNQM6", STRAT_NAME, Direction.LONG)
    transition_call = sm.transition.await_args
    assert transition_call.args[1] is State.ENTERING
    assert transition_call.kwargs["entry_order_id"] == 2001
    assert transition_call.kwargs["target_order_id"] == 2002
    assert transition_call.kwargs["stop_price"] == signal.stop_price
    assert transition_call.kwargs["target_price"] == signal.target_price


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
    # commission_total = qty * commission_rt_usd
    assert closed_call.kwargs["commission_total"] == pytest.approx(2 * MNQ.commission_rt_usd)


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
    ib.cancel_order = AsyncMock()

    await router.cancel_all_for(active_lc)

    assert ib.cancel_order.await_count == 3


async def test_cancel_all_for_skips_none_order_ids():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    idle_lc = _make_lifecycle(State.IDLE)  # all order ids None
    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    ib.cancel_order = AsyncMock()

    await router.cancel_all_for(idle_lc)
    ib.cancel_order.assert_not_awaited()


async def test_cancel_all_for_swallows_errors():
    ib = _make_mock_ib()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    # 3 cancel attempts; raise on the second to confirm subsequent ones still run.
    ib.cancel_order = AsyncMock(side_effect=[None, RuntimeError("cancel rejected"), None])

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    await router.cancel_all_for(active_lc)  # must not raise

    assert ib.cancel_order.await_count == 3


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
    ib.place_order = AsyncMock(side_effect=[_make_trade(2001), _make_trade(2002)])

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
    ib.place_order = AsyncMock(side_effect=[_make_trade(2001), _make_trade(2002)])

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)  # no dirty_set
    lc = await router.place_entry(_make_signal(), _make_contract())  # must not raise
    assert lc is entering_lc
