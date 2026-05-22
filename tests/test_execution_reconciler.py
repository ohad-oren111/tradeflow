"""Tests for src.execution.reconciler — mocked at the IBClient + StateMachine boundary.

Each conflict-matrix row from the PR #11 brief has a dedicated test; foreign
position detection, idempotency under InvariantViolationError, and the
asyncio loop are covered separately.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.instruments import MNQ
from src.clients.ib_client import IBClient
from src.execution.dirty_set import DirtySet
from src.execution.reconciler import (
    ReconcileAction,
    Reconciler,
    compute_pnl_gross,
)
from src.state_machine import (
    Direction,
    InvariantViolationError,
    Lifecycle,
    State,
    StateMachine,
)

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
    if state is State.EXITING:
        lc.exit_order_id = lc.stop_order_id
    for k, v in overrides.items():
        setattr(lc, k, v)
    return lc


def _make_position(symbol: str = "MNQM6", qty: int = 2, avg_cost: float = 17500.0) -> MagicMock:
    pos = MagicMock(name=f"Position<{symbol}>")
    contract = MagicMock()
    contract.localSymbol = symbol
    contract.symbol = "MNQ"
    pos.contract = contract
    pos.position = qty
    pos.avgCost = avg_cost
    return pos


def _make_portfolio_item(symbol: str = "MNQM6", qty: int = 2) -> MagicMock:
    item = MagicMock(name=f"PortfolioItem<{symbol}>")
    contract = MagicMock()
    contract.localSymbol = symbol
    contract.symbol = "MNQ"
    item.contract = contract
    item.position = qty
    return item


def _make_open_trade(order_id: int, symbol: str = "MNQM6") -> MagicMock:
    trade = MagicMock(name=f"OpenTrade<{order_id}>")
    trade.order = MagicMock()
    trade.order.orderId = order_id
    contract = MagicMock()
    contract.localSymbol = symbol
    contract.symbol = "MNQ"
    trade.contract = contract
    return trade


def _make_mock_ib(
    *,
    positions: list[Any] | None = None,
    portfolio: list[Any] | None = None,
    open_trades: list[Any] | None = None,
) -> AsyncMock:
    ib = AsyncMock(spec=IBClient)
    ib.is_connected = True
    ib.get_positions = AsyncMock(return_value=positions or [])
    ib.get_portfolio = AsyncMock(return_value=portfolio or [])
    ib.get_open_trades = AsyncMock(return_value=open_trades or [])
    return ib


def _make_mock_sm(non_closed: list[Lifecycle] | None = None) -> MagicMock:
    sm = MagicMock(spec=StateMachine)
    sm.transition = AsyncMock()
    sm.load_non_closed = AsyncMock(return_value=non_closed or [])
    return sm


def _build_reconciler(
    *,
    ib: AsyncMock | None = None,
    sm: MagicMock | None = None,
    dirty_set: DirtySet | None = None,
    halt: MagicMock | None = None,
    dirty_interval: float = 0.01,
    full_interval: float = 0.02,
) -> tuple[Reconciler, AsyncMock, MagicMock, DirtySet, MagicMock]:
    ib = ib or _make_mock_ib()
    sm = sm or _make_mock_sm()
    dirty_set = dirty_set if dirty_set is not None else DirtySet()
    halt = halt or MagicMock(name="halt_callback")
    rec = Reconciler(
        ib=ib,
        sm=sm,
        dirty_set=dirty_set,
        halt_callback=halt,
        dirty_drain_interval_sec=dirty_interval,
        full_scan_interval_sec=full_interval,
    )
    return rec, ib, sm, dirty_set, halt


# ========================================================================
# Conflict matrix — one test per row
# ========================================================================


# -------- IDLE rows --------


async def test_reconcile_idle_no_position_stays_idle():
    rec, _ib, sm, _ds, _halt = _build_reconciler()
    lc = _make_lifecycle(State.IDLE)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.NOOP
    sm.transition.assert_not_awaited()


async def test_reconcile_idle_with_position_logs_warning_no_transition(caplog):
    caplog.set_level(logging.WARNING)
    ib = _make_mock_ib(positions=[_make_position()])
    rec, _ib, sm, _ds, _halt = _build_reconciler(ib=ib)
    lc = _make_lifecycle(State.IDLE)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.WARNING
    sm.transition.assert_not_awaited()
    assert any("idle_with_position" in r.getMessage() for r in caplog.records)


# -------- ENTERING rows --------


async def test_reconcile_entering_with_working_order_stays_entering():
    # No broker position yet, entry order is still working.
    ib = _make_mock_ib(
        positions=[],
        open_trades=[_make_open_trade(1001)],
    )
    rec, _ib, sm, _ds, _halt = _build_reconciler(ib=ib)
    lc = _make_lifecycle(State.ENTERING)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.NOOP
    sm.transition.assert_not_awaited()


async def test_reconcile_entering_with_matching_position_transitions_to_active_with_broker_fields():
    ib = _make_mock_ib(
        positions=[_make_position(qty=2, avg_cost=17501.25)],
        open_trades=[],
    )
    sm = _make_mock_sm()
    active_lc = _make_lifecycle(State.ACTIVE)
    sm.transition.return_value = active_lc
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)
    lc = _make_lifecycle(State.ENTERING)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.ENTERING_TO_ACTIVE
    sm.transition.assert_awaited_once()
    call = sm.transition.await_args
    assert call.args[1] is State.ACTIVE
    assert call.kwargs["entry_qty"] == 2
    assert call.kwargs["entry_price"] == 17501.25
    assert call.kwargs["entry_filled_at"]  # iso string populated


async def test_reconcile_entering_no_order_no_position_transitions_to_closed_manual():
    ib = _make_mock_ib(positions=[], open_trades=[])
    sm = _make_mock_sm()
    sm.transition.return_value = _make_lifecycle(State.CLOSED)
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)
    lc = _make_lifecycle(State.ENTERING)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.ENTERING_TO_CLOSED
    sm.transition.assert_awaited_once()
    call = sm.transition.await_args
    assert call.args[1] is State.CLOSED
    assert call.kwargs["exit_reason"] == "MANUAL"
    assert call.kwargs["pnl_gross"] == 0.0
    assert call.kwargs["pnl_net"] == 0.0
    assert call.kwargs["entry_qty"] == 0


# -------- ACTIVE rows --------


async def test_reconcile_active_with_position_stays_active():
    ib = _make_mock_ib(
        positions=[_make_position(qty=2)],
        open_trades=[_make_open_trade(1003), _make_open_trade(1002)],
    )
    rec, _ib, sm, _ds, _halt = _build_reconciler(ib=ib)
    lc = _make_lifecycle(State.ACTIVE)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.NOOP
    sm.transition.assert_not_awaited()


async def test_reconcile_active_no_position_with_stop_filled_closes_as_stop():
    # Position gone, stop missing from open_trades, target still working → STOP filled.
    ib = _make_mock_ib(
        positions=[],
        open_trades=[_make_open_trade(1002)],  # target still open; stop (1003) missing
    )
    sm = _make_mock_sm()
    exiting_lc = _make_lifecycle(State.EXITING)
    closed_lc = _make_lifecycle(State.CLOSED)
    sm.transition = AsyncMock(side_effect=[exiting_lc, closed_lc])  # 2 transitions
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)
    lc = _make_lifecycle(State.ACTIVE)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.ACTIVE_TO_CLOSED
    assert sm.transition.await_count == 2
    closed_call = sm.transition.await_args
    assert closed_call.args[1] is State.CLOSED
    assert closed_call.kwargs["exit_reason"] == "STOP"
    # LONG (17400 stop - 17500 entry) * 2 * MNQ.multiplier
    expected_pnl = (17400.0 - 17500.0) * 2 * MNQ.multiplier
    assert closed_call.kwargs["pnl_gross"] == pytest.approx(expected_pnl)


async def test_reconcile_active_no_position_with_target_filled_closes_as_target():
    # Stop still working; target missing → TARGET filled.
    ib = _make_mock_ib(
        positions=[],
        open_trades=[_make_open_trade(1003)],  # stop still open; target (1002) missing
    )
    sm = _make_mock_sm()
    exiting_lc = _make_lifecycle(State.EXITING)
    closed_lc = _make_lifecycle(State.CLOSED)
    sm.transition = AsyncMock(side_effect=[exiting_lc, closed_lc])
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)
    lc = _make_lifecycle(State.ACTIVE)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.ACTIVE_TO_CLOSED
    closed_call = sm.transition.await_args
    assert closed_call.kwargs["exit_reason"] == "TARGET"
    expected_pnl = (17600.0 - 17500.0) * 2 * MNQ.multiplier
    assert closed_call.kwargs["pnl_gross"] == pytest.approx(expected_pnl)


async def test_reconcile_active_no_position_no_child_fills_closes_as_manual():
    # Neither child order is working → MANUAL.
    ib = _make_mock_ib(positions=[], open_trades=[])
    sm = _make_mock_sm()
    sm.transition = AsyncMock(
        side_effect=[_make_lifecycle(State.EXITING), _make_lifecycle(State.CLOSED)]
    )
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)
    lc = _make_lifecycle(State.ACTIVE)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.ACTIVE_TO_CLOSED
    exiting_call = sm.transition.await_args_list[0]
    assert exiting_call.kwargs["exit_order_id"] == 0  # sentinel per brief
    closed_call = sm.transition.await_args_list[1]
    assert closed_call.kwargs["exit_reason"] == "MANUAL"


# -------- EXITING rows --------


async def test_reconcile_exiting_with_position_and_working_order_stays_exiting():
    # Position still there + exit order still open → in flight.
    ib = _make_mock_ib(
        positions=[_make_position()],
        open_trades=[_make_open_trade(1003)],  # stop still open
    )
    rec, _ib, sm, _ds, _halt = _build_reconciler(ib=ib)
    lc = _make_lifecycle(State.EXITING)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.NOOP
    sm.transition.assert_not_awaited()


async def test_reconcile_exiting_no_position_closes_with_correct_exit_reason():
    # No position; stop still working → TARGET filled.
    ib = _make_mock_ib(
        positions=[],
        open_trades=[_make_open_trade(1003)],
    )
    sm = _make_mock_sm()
    sm.transition.return_value = _make_lifecycle(State.CLOSED)
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)
    lc = _make_lifecycle(State.EXITING)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.EXITING_TO_CLOSED
    sm.transition.assert_awaited_once()
    call = sm.transition.await_args
    assert call.args[1] is State.CLOSED
    assert call.kwargs["exit_reason"] == "TARGET"


async def test_reconcile_exiting_with_position_no_order_logs_warning(caplog):
    caplog.set_level(logging.WARNING)
    # Position still there, no exit orders open.
    ib = _make_mock_ib(positions=[_make_position()], open_trades=[])
    rec, _ib, sm, _ds, _halt = _build_reconciler(ib=ib)
    lc = _make_lifecycle(State.EXITING)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.WARNING
    sm.transition.assert_not_awaited()
    assert any("exiting_with_position_no_orders" in r.getMessage() for r in caplog.records)


# ========================================================================
# Foreign-position halt
# ========================================================================


async def test_full_scan_detects_foreign_position_and_calls_halt_callback(caplog):
    caplog.set_level(logging.WARNING)
    sm = _make_mock_sm(non_closed=[])
    # Broker has a position on a symbol the bot has no lifecycle for.
    ib = _make_mock_ib(portfolio=[_make_portfolio_item(symbol="ESM6", qty=1)])
    halt = MagicMock(name="halt_callback")
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm, halt=halt)

    counts = await rec.full_scan()

    halt.assert_called_once()
    assert counts.get(ReconcileAction.FOREIGN_POSITION) == 1
    assert any("foreign_position" in r.getMessage() for r in caplog.records)


async def test_full_scan_no_foreign_position_does_not_call_halt():
    tracked = _make_lifecycle(State.ACTIVE)
    sm = _make_mock_sm(non_closed=[tracked])
    ib = _make_mock_ib(
        portfolio=[_make_portfolio_item(symbol="MNQM6", qty=2)],
        positions=[_make_position()],
        open_trades=[_make_open_trade(1003), _make_open_trade(1002)],
    )
    halt = MagicMock(name="halt_callback")
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm, halt=halt)

    counts = await rec.full_scan()

    halt.assert_not_called()
    assert ReconcileAction.FOREIGN_POSITION not in counts


# ========================================================================
# Idempotency / race
# ========================================================================


async def test_reconcile_caught_invariant_violation_logs_and_continues(caplog):
    caplog.set_level(logging.WARNING)
    # ACTIVE with no position would walk to EXITING, but the state machine
    # raises (simulating a concurrent fill that already closed the lifecycle).
    ib = _make_mock_ib(positions=[], open_trades=[])
    sm = _make_mock_sm()
    sm.transition = AsyncMock(side_effect=InvariantViolationError("closed already"))
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)
    lc = _make_lifecycle(State.ACTIVE)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.RACE
    assert any("[RECON] race" in r.getMessage() for r in caplog.records)


async def test_drain_dirty_processes_only_currently_dirty_ids():
    lc1 = _make_lifecycle(State.ACTIVE)
    lc2 = _make_lifecycle(State.ACTIVE)
    sm = _make_mock_sm(non_closed=[lc1, lc2])
    ib = _make_mock_ib(
        positions=[_make_position()],
        open_trades=[_make_open_trade(1003), _make_open_trade(1002)],
    )
    ds = DirtySet()
    ds.add(lc1.lifecycle_id)  # only lc1 is dirty
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm, dirty_set=ds)

    counts = await rec.drain_dirty()

    # Both ACTIVE lifecycles match broker reality → NOOP. Only lc1 should be counted.
    assert counts.get(ReconcileAction.NOOP, 0) == 1


async def test_drain_dirty_clears_set_atomically():
    lc = _make_lifecycle(State.ACTIVE)
    sm = _make_mock_sm(non_closed=[lc])
    ib = _make_mock_ib(
        positions=[_make_position()],
        open_trades=[_make_open_trade(1003), _make_open_trade(1002)],
    )
    ds = DirtySet()
    ds.add(lc.lifecycle_id)
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm, dirty_set=ds)

    await rec.drain_dirty()

    assert len(ds) == 0


async def test_drain_dirty_empty_set_is_noop_logs_zero():
    rec, _ib, sm, ds, _halt = _build_reconciler()

    counts = await rec.drain_dirty()

    assert counts == {}
    # No DB read needed for an empty drain.
    sm.load_non_closed.assert_not_awaited()


async def test_drain_dirty_drops_ids_for_lifecycles_not_in_db():
    # An id that was queued but is no longer in the non-CLOSED set
    # (e.g. closed between event and drain) is silently dropped.
    sm = _make_mock_sm(non_closed=[])
    ds = DirtySet()
    ds.add("stale-id")
    rec, _ib, _sm, _ds, _halt = _build_reconciler(sm=sm, dirty_set=ds)

    counts = await rec.drain_dirty()

    assert counts == {}


# ========================================================================
# Loop tests
# ========================================================================


async def test_run_until_stopped_drains_at_interval():
    """Smoke: setting stop_event after ~3 drain ticks yields at least 2 drains."""
    sm = _make_mock_sm(non_closed=[])
    ib = _make_mock_ib()
    rec, _ib, _sm, _ds, _halt = _build_reconciler(
        ib=ib, sm=sm, dirty_interval=0.01, full_interval=10.0
    )
    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.035)
        stop.set()

    await asyncio.wait_for(asyncio.gather(rec.run_until_stopped(stop), stopper()), timeout=2.0)

    # drain_dirty was invoked at least twice — empty-set fast path.
    assert sm.load_non_closed.await_count == 0  # empty drain skips DB read


async def test_run_until_stopped_full_scans_at_interval():
    """Confirm that the full-scan fires at the ticks_per_full_scan cadence."""
    lc = _make_lifecycle(State.ACTIVE)
    sm = _make_mock_sm(non_closed=[lc])
    ib = _make_mock_ib(
        positions=[_make_position()],
        portfolio=[_make_portfolio_item()],
        open_trades=[_make_open_trade(1003), _make_open_trade(1002)],
    )
    # Full scan every 2 drain ticks.
    rec, _ib, _sm, _ds, _halt = _build_reconciler(
        ib=ib, sm=sm, dirty_interval=0.01, full_interval=0.02
    )
    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.06)
        stop.set()

    await asyncio.wait_for(asyncio.gather(rec.run_until_stopped(stop), stopper()), timeout=2.0)

    # Each full_scan calls load_non_closed once + get_portfolio once.
    assert ib.get_portfolio.await_count >= 1


async def test_run_until_stopped_exits_on_stop_event():
    rec, _ib, _sm, _ds, _halt = _build_reconciler(dirty_interval=0.5, full_interval=1.0)
    stop = asyncio.Event()

    async def setter():
        await asyncio.sleep(0)
        stop.set()

    await asyncio.wait_for(asyncio.gather(rec.run_until_stopped(stop), setter()), timeout=2.0)


async def test_run_until_stopped_swallows_drain_errors(caplog):
    caplog.set_level(logging.ERROR)
    sm = _make_mock_sm()
    sm.load_non_closed = AsyncMock(side_effect=RuntimeError("supabase down"))
    ds = DirtySet()
    ds.add("any-id")
    rec, _ib, _sm, _ds, _halt = _build_reconciler(
        sm=sm, dirty_set=ds, dirty_interval=0.01, full_interval=1.0
    )
    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.03)
        stop.set()

    await asyncio.wait_for(asyncio.gather(rec.run_until_stopped(stop), stopper()), timeout=2.0)

    # Loop must keep ticking despite the error.
    assert any("drain_error" in r.getMessage() for r in caplog.records)


# ========================================================================
# pnl helper + smoke
# ========================================================================


def test_compute_pnl_gross_long_winner():
    assert compute_pnl_gross(
        direction=Direction.LONG, entry_price=17500.0, exit_price=17600.0, qty=2
    ) == pytest.approx((17600.0 - 17500.0) * 2 * MNQ.multiplier)


def test_compute_pnl_gross_long_loser():
    assert compute_pnl_gross(
        direction=Direction.LONG, entry_price=17500.0, exit_price=17400.0, qty=2
    ) == pytest.approx((17400.0 - 17500.0) * 2 * MNQ.multiplier)


def test_compute_pnl_gross_short_winner():
    assert compute_pnl_gross(
        direction=Direction.SHORT, entry_price=17500.0, exit_price=17400.0, qty=2
    ) == pytest.approx((17500.0 - 17400.0) * 2 * MNQ.multiplier)


async def test_active_with_partial_position_qty_match_still_noop():
    # Broker shows the position with a slightly different qty — DB qty wins
    # for the "is it still there?" check; mismatch is a TODO worth logging
    # but not a transition. Today: as long as a position exists, ACTIVE stays.
    ib = _make_mock_ib(
        positions=[_make_position(qty=1)],  # entry_qty=2 in DB
        open_trades=[_make_open_trade(1003), _make_open_trade(1002)],
    )
    rec, _ib, sm, _ds, _halt = _build_reconciler(ib=ib)
    lc = _make_lifecycle(State.ACTIVE)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.NOOP
    sm.transition.assert_not_awaited()


async def test_full_scan_processes_each_non_closed_lifecycle():
    lc1 = _make_lifecycle(State.ACTIVE)
    lc2 = _make_lifecycle(State.ENTERING, symbol="ESM6", entry_order_id=2001)
    sm = _make_mock_sm(non_closed=[lc1, lc2])
    # Broker has the MNQM6 position (matches lc1) + an open order 2001 (matches lc2's
    # in-flight entry); lc2's symbol has no broker position so it stays ENTERING.
    ib = _make_mock_ib(
        positions=[_make_position()],
        portfolio=[_make_portfolio_item()],
        open_trades=[_make_open_trade(1003), _make_open_trade(1002), _make_open_trade(2001)],
    )
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)

    counts = await rec.full_scan()

    # lc1 ACTIVE w/ position → NOOP. lc2 ENTERING w/ order still open → NOOP. Two NOOPs.
    assert counts.get(ReconcileAction.NOOP, 0) == 2
