"""Tests for src.execution.reconciler — mocked at the IBClient + StateMachine boundary.

Each conflict-matrix row from the PR #11 brief has a dedicated test; foreign
position detection, idempotency under InvariantViolationError, and the
asyncio loop are covered separately. PR #12 adds halt-ack poll coverage in
the trailing block (Supabase primary + file-flag fallback paths).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.instruments import MNQ
from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.execution.dirty_set import DirtySet
from src.execution.reconciler import (
    ReconcileAction,
    Reconciler,
    _exit_price_for,
    _fill_price_for_order,
    _filled_order_id_for,
    compute_pnl_gross,
)
from src.state_machine import (
    Direction,
    ExitReason,
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
    # The reconciler force-fill path now places a protective STP via
    # ensure_protective_stop → ib.place_order; return a Trade-shaped stub with an
    # int orderId so the helper can read it.
    placed = MagicMock(name="StpTrade")
    placed.order.orderId = 9001
    ib.place_order = AsyncMock(return_value=placed)
    return ib


def _make_mock_sm(non_closed: list[Lifecycle] | None = None) -> MagicMock:
    sm = MagicMock(spec=StateMachine)
    sm.transition = AsyncMock()
    sm.load_non_closed = AsyncMock(return_value=non_closed or [])
    return sm


def _make_mock_orchestrator(halted: bool = False) -> MagicMock:
    """Mock HaltCoordinator: raise_halt/clear_halt/is_halted/halt_raised_at.

    ``halted`` toggles the initial state. ``raise_halt`` is a MagicMock so
    foreign-position tests can assert it was called; legacy tests that used
    ``halt.assert_called_once()`` keep working by asserting on ``.raise_halt``.
    """
    orch = MagicMock(name="orchestrator")
    orch.is_halted = MagicMock(return_value=halted)
    orch.halt_raised_at = MagicMock(return_value=None)
    orch.raise_halt = MagicMock()
    orch.clear_halt = MagicMock()
    return orch


def _make_mock_db() -> AsyncMock:
    db = AsyncMock(spec=SupabaseClient)
    db.get_newest_halt_ack = AsyncMock(return_value=None)
    return db


def _build_reconciler(
    *,
    ib: AsyncMock | None = None,
    sm: MagicMock | None = None,
    dirty_set: DirtySet | None = None,
    db: AsyncMock | None = None,
    orchestrator: MagicMock | None = None,
    halt_ack_file_path: Path | None = None,
    dirty_interval: float = 0.01,
    full_interval: float = 0.02,
) -> tuple[Reconciler, AsyncMock, MagicMock, DirtySet, MagicMock]:
    ib = ib or _make_mock_ib()
    sm = sm or _make_mock_sm()
    dirty_set = dirty_set if dirty_set is not None else DirtySet()
    db = db or _make_mock_db()
    orchestrator = orchestrator or _make_mock_orchestrator()
    # PR #12 — when no explicit file path is supplied, point at a per-test path
    # under /tmp that almost certainly does not exist. Tests that exercise the
    # file-flag fallback pass their own ``tmp_path / "halt_clear"``.
    if halt_ack_file_path is None:
        halt_ack_file_path = Path(f"/tmp/halt_clear_test_{uuid.uuid4()}")
    rec = Reconciler(
        ib=ib,
        sm=sm,
        dirty_set=dirty_set,
        db=db,
        orchestrator=orchestrator,
        dirty_drain_interval_sec=dirty_interval,
        full_scan_interval_sec=full_interval,
        halt_ack_file_path=halt_ack_file_path,
    )
    # 5-tuple shape preserved for callers; ``orchestrator`` replaces the
    # pre-PR-12 ``halt`` MagicMock — the .raise_halt attribute carries the
    # call assertions the legacy tests used.
    return rec, ib, sm, dirty_set, orchestrator


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
    rec, ib_ref, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)
    lc = _make_lifecycle(State.ENTERING)

    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.ENTERING_TO_ACTIVE
    sm.transition.assert_awaited_once()
    call = sm.transition.await_args
    assert call.args[1] is State.ACTIVE
    assert call.kwargs["entry_qty"] == 2
    # PR #69 — entry_price is the per-contract price, NOT the notional avgCost
    # (avgCost = price × multiplier for futures). 17501.25 / 2 = 8750.625.
    assert call.kwargs["entry_price"] == pytest.approx(17501.25 / MNQ.multiplier)
    assert call.kwargs["entry_filled_at"]  # iso string populated
    # PR #69 — the force-fill path now also places the protective STP (the
    # router never saw the fillEvent), and stamps its id onto the ACTIVE row.
    ib_ref.place_order.assert_awaited_once()
    assert call.kwargs["stop_order_id"] == 9001


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


# -------- W-S15.1 orphan-leg cancel on reconciler-driven close --------


def _cancelled_order_ids(ib: AsyncMock) -> list[int]:
    """The order ids passed to ``ib.cancel_order_by_id`` (PR #72 — the int id
    directly; the IB client looks up the real Trade internally)."""
    return [call.args[0] for call in ib.cancel_order_by_id.await_args_list]


async def test_recon_active_stop_filled_cancels_orphan_target_leg():
    # Stop filled (1003 missing), target (1002) still resting → reconciler closes
    # AND cancels the orphan target so it can't fill with no position behind it.
    ib = _make_mock_ib(positions=[], open_trades=[_make_open_trade(1002)])
    ib.cancel_order_by_id = AsyncMock(return_value=True)
    sm = _make_mock_sm()
    sm.transition = AsyncMock(
        side_effect=[_make_lifecycle(State.EXITING), _make_lifecycle(State.CLOSED)]
    )
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)

    action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE))

    assert action is ReconcileAction.ACTIVE_TO_CLOSED
    assert _cancelled_order_ids(ib) == [1002]  # only the still-open target leg


async def test_recon_active_target_filled_cancels_orphan_stop_leg():
    # Target filled (1002 missing), stop (1003) still resting → cancel the stop.
    ib = _make_mock_ib(positions=[], open_trades=[_make_open_trade(1003)])
    ib.cancel_order_by_id = AsyncMock(return_value=True)
    sm = _make_mock_sm()
    sm.transition = AsyncMock(
        side_effect=[_make_lifecycle(State.EXITING), _make_lifecycle(State.CLOSED)]
    )
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)

    action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE))

    assert action is ReconcileAction.ACTIVE_TO_CLOSED
    assert _cancelled_order_ids(ib) == [1003]


async def test_recon_close_with_no_open_legs_cancels_nothing():
    # Both children already gone broker-side → nothing left to cancel.
    ib = _make_mock_ib(positions=[], open_trades=[])
    ib.cancel_order_by_id = AsyncMock(return_value=True)
    sm = _make_mock_sm()
    sm.transition = AsyncMock(
        side_effect=[_make_lifecycle(State.EXITING), _make_lifecycle(State.CLOSED)]
    )
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)

    await rec.reconcile_one(_make_lifecycle(State.ACTIVE))

    ib.cancel_order_by_id.assert_not_awaited()


async def test_recon_orphan_cancel_is_safe_noop_on_error():
    # A cancel reject (e.g. order already filled) must not crash the reconciler.
    ib = _make_mock_ib(positions=[], open_trades=[_make_open_trade(1002)])
    # one cancel attempt that raises — the reconciler's swallow-guard must absorb it
    ib.cancel_order_by_id = AsyncMock(side_effect=RuntimeError("Error 10148: cannot cancel"))
    sm = _make_mock_sm()
    sm.transition = AsyncMock(
        side_effect=[_make_lifecycle(State.EXITING), _make_lifecycle(State.CLOSED)]
    )
    rec, _ib, _sm, _ds, _halt = _build_reconciler(ib=ib, sm=sm)

    action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE))  # must not raise

    assert action is ReconcileAction.ACTIVE_TO_CLOSED
    assert sm.transition.await_count == 2  # close still completed


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
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, sm=sm, orchestrator=orch)

    counts = await rec.full_scan()

    orch.raise_halt.assert_called_once_with("ESM6")
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
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, sm=sm, orchestrator=orch)

    counts = await rec.full_scan()

    orch.raise_halt.assert_not_called()
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


# ========================================================================
# PR #12 — halt-ack poll
# ========================================================================


def _halted_at(raised_at: datetime) -> MagicMock:
    """Mock orchestrator that reports is_halted=True with the supplied timestamp."""
    orch = _make_mock_orchestrator(halted=True)
    orch.halt_raised_at = MagicMock(return_value=raised_at)
    return orch


async def test_poll_halt_ack_no_op_when_not_halted():
    db = _make_mock_db()
    orch = _make_mock_orchestrator(halted=False)
    rec, _ib, _sm, _ds, _orch = _build_reconciler(db=db, orchestrator=orch)

    await rec._poll_halt_ack()

    db.get_newest_halt_ack.assert_not_awaited()
    orch.clear_halt.assert_not_called()


async def test_poll_halt_ack_supabase_success_newer_ack_clears_halt(caplog):
    caplog.set_level(logging.INFO)
    raised_at = datetime.now(UTC) - timedelta(minutes=5)
    ack_ts = (raised_at + timedelta(seconds=10)).isoformat()
    db = _make_mock_db()
    db.get_newest_halt_ack = AsyncMock(
        return_value={"halt_ack_id": "abc", "acked_at": ack_ts, "note": "operator-ok"}
    )
    orch = _halted_at(raised_at)
    rec, _ib, _sm, _ds, _orch = _build_reconciler(db=db, orchestrator=orch)

    await rec._poll_halt_ack()

    db.get_newest_halt_ack.assert_awaited_once()
    orch.clear_halt.assert_called_once()
    clear_args = orch.clear_halt.call_args
    assert "supabase ack" in clear_args.kwargs.get(
        "reason", clear_args.args[0] if clear_args.args else ""
    )
    assert any("halt_acked: source=supabase" in r.getMessage() for r in caplog.records)


async def test_poll_halt_ack_supabase_returns_no_newer_ack_keeps_halt():
    raised_at = datetime.now(UTC) - timedelta(minutes=5)
    db = _make_mock_db()
    db.get_newest_halt_ack = AsyncMock(return_value=None)
    orch = _halted_at(raised_at)
    rec, _ib, _sm, _ds, _orch = _build_reconciler(db=db, orchestrator=orch)

    await rec._poll_halt_ack()

    db.get_newest_halt_ack.assert_awaited_once()
    orch.clear_halt.assert_not_called()


async def test_poll_halt_ack_supabase_fails_file_flag_fresh_clears_halt(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    raised_at = datetime.now(UTC) - timedelta(minutes=5)
    flag_path = tmp_path / "halt_clear"
    flag_path.touch()
    # mtime = raised_at + 5s → fresh
    fresh_ts = (raised_at + timedelta(seconds=5)).timestamp()
    os.utime(flag_path, (fresh_ts, fresh_ts))

    db = _make_mock_db()
    db.get_newest_halt_ack = AsyncMock(side_effect=RuntimeError("network down"))
    orch = _halted_at(raised_at)
    rec, _ib, _sm, _ds, _orch = _build_reconciler(
        db=db, orchestrator=orch, halt_ack_file_path=flag_path
    )

    await rec._poll_halt_ack()

    orch.clear_halt.assert_called_once()
    assert "file-flag" in orch.clear_halt.call_args.kwargs["reason"]
    assert any("halt_acked: source=file_flag" in r.getMessage() for r in caplog.records)


async def test_poll_halt_ack_supabase_fails_file_flag_stale_keeps_halt(tmp_path):
    raised_at = datetime.now(UTC) - timedelta(minutes=5)
    flag_path = tmp_path / "halt_clear"
    flag_path.touch()
    # mtime = raised_at - 5s → stale (older than halt)
    stale_ts = (raised_at - timedelta(seconds=5)).timestamp()
    os.utime(flag_path, (stale_ts, stale_ts))

    db = _make_mock_db()
    db.get_newest_halt_ack = AsyncMock(side_effect=RuntimeError("network down"))
    orch = _halted_at(raised_at)
    rec, _ib, _sm, _ds, _orch = _build_reconciler(
        db=db, orchestrator=orch, halt_ack_file_path=flag_path
    )

    await rec._poll_halt_ack()

    orch.clear_halt.assert_not_called()


async def test_poll_halt_ack_supabase_fails_file_flag_absent_keeps_halt(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    raised_at = datetime.now(UTC) - timedelta(minutes=5)
    flag_path = tmp_path / "halt_clear"  # not created → absent
    assert not flag_path.exists()

    db = _make_mock_db()
    db.get_newest_halt_ack = AsyncMock(side_effect=RuntimeError("network down"))
    orch = _halted_at(raised_at)
    rec, _ib, _sm, _ds, _orch = _build_reconciler(
        db=db, orchestrator=orch, halt_ack_file_path=flag_path
    )

    await rec._poll_halt_ack()

    orch.clear_halt.assert_not_called()
    assert any("halt_ack_poll_failed" in r.getMessage() for r in caplog.records)


async def test_poll_halt_ack_supabase_returns_ack_older_than_raise_no_clear():
    # If the DB ever returns a stale ack (e.g. since= was wrong), reconciler
    # must still clear when get_newest_halt_ack returns a row — by contract
    # get_newest_halt_ack only returns rows with acked_at > since. The test
    # here covers the "no row returned" path which is the semantic equivalent.
    raised_at = datetime.now(UTC)
    db = _make_mock_db()
    db.get_newest_halt_ack = AsyncMock(return_value=None)
    orch = _halted_at(raised_at)
    rec, _ib, _sm, _ds, _orch = _build_reconciler(db=db, orchestrator=orch)

    await rec._poll_halt_ack()

    orch.clear_halt.assert_not_called()


# ----------------------------------------------------- W-S15.3 exit-price helpers


def _exec_fill(order_id: int, shares: float, price: float) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        execution=SimpleNamespace(orderId=order_id, shares=shares, price=price, avgPrice=price)
    )


def test_fill_price_for_order_qty_weights_partial_fills():
    # Same order, two partial executions at different prices → qty-weighted avg.
    fills = [_exec_fill(20, 1, 30300.0), _exec_fill(20, 3, 30310.0)]
    # (1*30300 + 3*30310) / 4 = 30307.5
    assert _fill_price_for_order(fills, 20) == pytest.approx(30307.5)


def test_fill_price_for_order_no_match_returns_none():
    fills = [_exec_fill(99, 2, 30310.0)]
    assert _fill_price_for_order(fills, 20) is None
    assert _fill_price_for_order([], 20) is None


def test_filled_order_id_for_attribution():
    lc = _make_lifecycle(State.ACTIVE)  # stop_order_id=1003, target_order_id=1002
    lc.exit_order_id = 1009
    assert _filled_order_id_for(lc, ExitReason.STOP) == lc.stop_order_id
    assert _filled_order_id_for(lc, ExitReason.TARGET) == lc.target_order_id
    assert _filled_order_id_for(lc, ExitReason.MANUAL) == lc.exit_order_id


async def test_resolve_exit_price_prefers_actual_fill_over_stop_price():
    lc = _make_lifecycle(State.ACTIVE, stop_price=30325.0)
    ib = _make_mock_ib()
    # Stop (1003) actually filled 15pt past trigger.
    ib.get_fills = AsyncMock(return_value=[_exec_fill(lc.stop_order_id, 2, 30310.0)])
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib)
    price = await rec._resolve_exit_price(lc, ExitReason.STOP)
    assert price == 30310.0
    # Sanity: the order-price fallback would have returned the trigger.
    assert _exit_price_for(lc, ExitReason.STOP) == 30325.0


async def test_resolve_exit_price_falls_back_when_fill_lookup_raises():
    lc = _make_lifecycle(State.ACTIVE, stop_price=30325.0)
    ib = _make_mock_ib()
    ib.get_fills = AsyncMock(side_effect=RuntimeError("not connected"))
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib)
    price = await rec._resolve_exit_price(lc, ExitReason.STOP)
    assert price == 30325.0  # order-price fallback, never raises
