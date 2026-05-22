"""Tests for src.execution.force_close — mocked at the router + state machine boundary."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from src.execution.force_close import EodForceClose
from src.execution.router import OrderRouter
from src.state_machine import (
    Direction,
    Lifecycle,
    State,
    StateMachine,
)

ET = ZoneInfo("America/New_York")


# -------------------------------------------------------------------- factories


def _make_lifecycle(state: State, **overrides: Any) -> Lifecycle:
    lc = Lifecycle(
        lifecycle_id=str(uuid.uuid4()),
        symbol="MNQM6",
        strategy="sma100_bounce",
        direction=Direction.LONG.value,
        state=state.value,
    )
    if state in (State.ENTERING, State.ACTIVE, State.EXITING, State.CLOSED):
        lc.entry_order_id = 1001
    if state in (State.ACTIVE, State.EXITING, State.CLOSED):
        lc.entry_qty = 2
        lc.entry_price = 17500.0
        lc.entry_filled_at = "2026-05-21T15:00:00+00:00"
        lc.stop_order_id = 1003
        lc.target_order_id = 1002
    for k, v in overrides.items():
        setattr(lc, k, v)
    return lc


def _make_mock_router() -> MagicMock:
    router = MagicMock(spec=OrderRouter)
    router.cancel_all_for = AsyncMock()
    router.register_eod_exit = MagicMock()
    router._ib = MagicMock()
    router._ib.place_order = AsyncMock()
    return router


def _make_mock_sm() -> MagicMock:
    sm = MagicMock(spec=StateMachine)
    sm.transition = AsyncMock()
    sm.load_non_closed = AsyncMock(return_value=[])
    return sm


def _make_trade(order_id: int) -> MagicMock:
    trade = MagicMock(name=f"Trade<{order_id}>")
    trade.order = MagicMock()
    trade.order.orderId = order_id
    return trade


def _make_contract() -> MagicMock:
    c = MagicMock(name="Contract")
    c.localSymbol = "MNQM6"
    return c


# ---------------------------------------------------------- next_trigger_at


def test_next_trigger_at_returns_today_when_before_3_58pm_et():
    router = _make_mock_router()
    sm = _make_mock_sm()
    eod = EodForceClose(router, sm, contract=_make_contract())

    now_et = datetime(2026, 5, 21, 10, 0, tzinfo=ET)  # Thursday morning
    next_at = eod.next_trigger_at(now_et.astimezone(UTC))

    next_et = next_at.astimezone(ET)
    assert next_et.date() == now_et.date()
    assert next_et.hour == 15
    assert next_et.minute == 58


def test_next_trigger_at_rolls_to_next_weekday_when_after_3_58pm():
    router = _make_mock_router()
    sm = _make_mock_sm()
    eod = EodForceClose(router, sm, contract=_make_contract())

    now_et = datetime(2026, 5, 21, 17, 0, tzinfo=ET)  # Thursday evening
    next_at = eod.next_trigger_at(now_et.astimezone(UTC))

    next_et = next_at.astimezone(ET)
    assert next_et.weekday() == 4  # Friday
    assert next_et.hour == 15
    assert next_et.minute == 58


def test_next_trigger_at_skips_weekend():
    router = _make_mock_router()
    sm = _make_mock_sm()
    eod = EodForceClose(router, sm, contract=_make_contract())

    now_et = datetime(2026, 5, 22, 17, 0, tzinfo=ET)  # Friday evening
    next_at = eod.next_trigger_at(now_et.astimezone(UTC))

    next_et = next_at.astimezone(ET)
    assert next_et.weekday() == 0  # Monday
    assert next_et.hour == 15
    assert next_et.minute == 58


def test_next_trigger_at_uses_zoneinfo_for_dst():
    """Both DST-active (summer) and DST-inactive (winter) produce 3:58pm ET local."""
    router = _make_mock_router()
    sm = _make_mock_sm()
    eod = EodForceClose(router, sm, contract=_make_contract())

    summer_now_et = datetime(2026, 7, 15, 10, 0, tzinfo=ET)  # DST active
    winter_now_et = datetime(2026, 1, 15, 10, 0, tzinfo=ET)  # DST inactive

    summer_next_et = eod.next_trigger_at(summer_now_et.astimezone(UTC)).astimezone(ET)
    winter_next_et = eod.next_trigger_at(winter_now_et.astimezone(UTC)).astimezone(ET)

    assert summer_next_et.hour == 15 and summer_next_et.minute == 58
    assert winter_next_et.hour == 15 and winter_next_et.minute == 58


# ---------------------------------------------------------------- fire_once


async def test_fire_once_no_open_lifecycles_is_noop():
    router = _make_mock_router()
    sm = _make_mock_sm()
    sm.load_non_closed = AsyncMock(return_value=[])
    eod = EodForceClose(router, sm, contract=_make_contract())

    count = await eod.fire_once()

    assert count == 0
    router.cancel_all_for.assert_not_awaited()
    router._ib.place_order.assert_not_awaited()
    sm.transition.assert_not_awaited()


async def test_fire_once_active_lifecycle_places_market_exit_and_transitions_to_exiting():
    router = _make_mock_router()
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    sm.load_non_closed = AsyncMock(return_value=[active_lc])
    exiting_lc = _make_lifecycle(State.EXITING)
    sm.transition = AsyncMock(return_value=exiting_lc)  # 1 call expected
    router._ib.place_order = AsyncMock(return_value=_make_trade(5001))  # 1 call expected

    contract = _make_contract()
    eod = EodForceClose(router, sm, contract=contract)

    count = await eod.fire_once()

    assert count == 1
    router.cancel_all_for.assert_awaited_once_with(active_lc)
    router._ib.place_order.assert_awaited_once()
    # The contract argument to place_order must be the EOD contract.
    args, kwargs = router._ib.place_order.await_args
    assert args[0] is contract
    market_exit = args[1]
    assert market_exit.orderType == "MKT"
    assert market_exit.action == "SELL"  # LONG → SELL to flatten

    sm.transition.assert_awaited_once()
    transition_call = sm.transition.await_args
    assert transition_call.args[1] is State.EXITING
    assert transition_call.kwargs["exit_order_id"] == 5001

    router.register_eod_exit.assert_called_once_with(exiting_lc, 5001)


async def test_fire_once_idempotent_when_called_twice_with_same_state():
    """Second fire should observe the already-CLOSED state and no-op."""
    router = _make_mock_router()
    sm = _make_mock_sm()
    # First call returns one ACTIVE lifecycle; second returns empty list
    # (Supabase already reflects the post-transition CLOSED rows).
    active_lc = _make_lifecycle(State.ACTIVE)
    sm.load_non_closed = AsyncMock(side_effect=[[active_lc], []])  # 2 calls
    sm.transition = AsyncMock(return_value=_make_lifecycle(State.EXITING))
    router._ib.place_order = AsyncMock(return_value=_make_trade(5001))

    eod = EodForceClose(router, sm, contract=_make_contract())

    first = await eod.fire_once()
    second = await eod.fire_once()

    assert first == 1
    assert second == 0
    # Only the first fire placed an exit order.
    router._ib.place_order.assert_awaited_once()
    sm.transition.assert_awaited_once()


async def test_fire_once_idle_lifecycle_closes_pre_active_without_market_order():
    router = _make_mock_router()
    sm = _make_mock_sm()
    idle_lc = _make_lifecycle(State.IDLE)
    sm.load_non_closed = AsyncMock(return_value=[idle_lc])
    sm.transition = AsyncMock(return_value=_make_lifecycle(State.CLOSED))

    eod = EodForceClose(router, sm, contract=_make_contract())
    count = await eod.fire_once()

    assert count == 1
    router._ib.place_order.assert_not_awaited()
    sm.transition.assert_awaited_once()
    args = sm.transition.await_args
    assert args.args[1] is State.CLOSED
    assert args.kwargs["exit_reason"] == "EOD"


async def test_fire_once_already_exiting_is_noop_after_cancel():
    router = _make_mock_router()
    sm = _make_mock_sm()
    exiting_lc = _make_lifecycle(State.EXITING)
    sm.load_non_closed = AsyncMock(return_value=[exiting_lc])

    eod = EodForceClose(router, sm, contract=_make_contract())
    count = await eod.fire_once()

    assert count == 1
    router.cancel_all_for.assert_awaited_once_with(exiting_lc)
    router._ib.place_order.assert_not_awaited()
    sm.transition.assert_not_awaited()


# ------------------------------------------------------------ run_until_stopped


async def test_run_until_stopped_exits_when_stop_event_set_during_sleep():
    """Smoke: setting stop_event while the loop is awaiting next_trigger should
    cause :meth:`run_until_stopped` to return cleanly."""
    router = _make_mock_router()
    sm = _make_mock_sm()
    eod = EodForceClose(router, sm, contract=_make_contract())

    stop = asyncio.Event()

    async def setter():
        await asyncio.sleep(0)  # yield once so run_until_stopped starts its wait
        stop.set()

    await asyncio.wait_for(asyncio.gather(eod.run_until_stopped(stop), setter()), timeout=2.0)
    # fire_once should NOT have been triggered (we exited before next_trigger fired).
    sm.load_non_closed.assert_not_awaited()
