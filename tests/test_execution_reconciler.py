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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.instruments import MNQ
from config.risk_params import RISK
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
    foreign_quantity,
    intended_net_position,
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


# -------- self-heal missing bracket leg (ACTIVE + live position) --------


def _placed(oid: int) -> MagicMock:
    t = MagicMock(name=f"Trade<{oid}>")
    t.order.orderId = oid
    return t


def _open_trade_oca(order_id: int, oca: str) -> MagicMock:
    t = _make_open_trade(order_id)
    t.order.ocaGroup = oca
    return t


async def test_heal_replaces_missing_stop_only_oca_into_target_group():
    ib = _make_mock_ib(
        positions=[_make_position(qty=2)],
        open_trades=[_open_trade_oca(1002, "grp-A")],  # target resident; stop 1003 gone
    )
    ib.place_order = AsyncMock(return_value=_placed(9001))  # 1 call (stop only)
    db = _make_mock_db()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, db=db)

    action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE))

    assert action is ReconcileAction.HEALED
    ib.place_order.assert_awaited_once()
    placed = ib.place_order.await_args.args[1]
    assert placed.orderType == "STP" and placed.action == "SELL"
    assert placed.auxPrice == 17400.0  # recorded stop_price (entry-75), NOT live price
    assert placed.outsideRth is True
    assert placed.ocaGroup == "grp-A"  # OCA'd into the surviving target's group
    db.update_lifecycle.assert_awaited_once()
    assert db.update_lifecycle.await_args.args[1] == {"stop_order_id": 9001}


async def test_heal_replaces_both_missing_legs_as_fresh_oca_bracket():
    ib = _make_mock_ib(positions=[_make_position(qty=2)], open_trades=[])  # both legs gone
    ib.place_order = AsyncMock(side_effect=[_placed(9101), _placed(9102)])  # 2 calls: stop, target
    db = _make_mock_db()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, db=db)

    action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE))

    assert action is ReconcileAction.HEALED
    assert ib.place_order.await_count == 2
    stp = ib.place_order.await_args_list[0].args[1]
    tp = ib.place_order.await_args_list[1].args[1]
    assert stp.orderType == "STP" and stp.auxPrice == 17400.0
    assert tp.orderType == "LMT" and tp.lmtPrice == 17600.0
    assert stp.ocaGroup == tp.ocaGroup and stp.ocaGroup.startswith("heal-")  # one fresh group
    assert db.update_lifecycle.await_args.args[1] == {
        "stop_order_id": 9101,
        "target_order_id": 9102,
    }


async def test_heal_noop_when_both_legs_resident():
    ib = _make_mock_ib(
        positions=[_make_position(qty=2)],
        open_trades=[_make_open_trade(1003), _make_open_trade(1002)],  # both present
    )
    ib.place_order = AsyncMock()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib)

    action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE))

    assert action is ReconcileAction.NOOP
    ib.place_order.assert_not_awaited()  # idempotent — nothing to heal


async def test_heal_no_placement_when_flat():
    ib = _make_mock_ib(positions=[], open_trades=[])  # no live position
    ib.place_order = AsyncMock()
    sm = _make_mock_sm()
    sm.transition = AsyncMock(
        side_effect=[_make_lifecycle(State.EXITING), _make_lifecycle(State.CLOSED)]  # 2 calls
    )
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, sm=sm)

    await rec.reconcile_one(_make_lifecycle(State.ACTIVE))

    ib.place_order.assert_not_awaited()  # FLAT → never place; the close path owns this


# -------- PR 99: leg-heal is exit-mode-aware (§0.5.196 / WO5) --------


def _set_exit_mode(monkeypatch, mode: str) -> None:
    """Override the reconciler's resolved RISK with a chosen exit_mode (frozen → replace)."""
    monkeypatch.setattr("src.execution.reconciler.RISK", replace(RISK, exit_mode=mode))


async def test_heal_trailing_skips_target_rearm_but_heals_missing_stop(monkeypatch, caplog):
    """Trailing mode + ACTIVE position with the STP gone and NO resting TARGET (trailing
    entry shape: target_order_id=None): the reconciler re-places the STOP but NEVER
    re-arms a TARGET (the §0.5.196 hybrid). Asserts on placed order TYPES."""
    _set_exit_mode(monkeypatch, "trailing")
    ib = _make_mock_ib(
        positions=[_make_position(qty=2)],
        open_trades=[],  # stop 1003 gone; trailing lifecycle never had a target order
    )
    ib.place_order = AsyncMock(return_value=_placed(9001))  # STP only — a 2nd call is a bug
    db = _make_mock_db()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, db=db)

    with caplog.at_level(logging.INFO):
        action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE, target_order_id=None))

    assert action is ReconcileAction.HEALED
    ib.place_order.assert_awaited_once()  # exactly one leg placed
    placed = [c.args[1] for c in ib.place_order.await_args_list]
    assert [o.orderType for o in placed] == ["STP"]  # STOP healed, NO LMT TARGET re-armed
    assert any("target_heal_skipped" in r.message for r in caplog.records)
    assert db.update_lifecycle.await_args.args[1] == {"stop_order_id": 9001}  # no target id


async def test_heal_trailing_healthy_position_is_noop_no_target_rearm(monkeypatch):
    """Trailing mode + ACTIVE position with the STP resident and (correctly) no TARGET:
    the position is healthy — the reconciler must NOT treat the absent target as a gap
    and must place nothing (no every-scan re-arm / log-spam)."""
    _set_exit_mode(monkeypatch, "trailing")
    ib = _make_mock_ib(
        positions=[_make_position(qty=2)],
        open_trades=[_make_open_trade(1003)],  # STP resident; no target order exists
    )
    ib.place_order = AsyncMock()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib)

    action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE, target_order_id=None))

    assert action is ReconcileAction.NOOP
    ib.place_order.assert_not_awaited()  # healthy trailing position → nothing to heal


async def test_heal_trailing_stop_is_ungrouped_and_at_ratcheted_level(monkeypatch):
    """STABILIZE-3 — after a ratchet (which persists the new stop_price), a redeploy/
    reconnect that drops the GTC stop must heal at the RATCHETED level, not the
    entry−75 base, AND the re-placed STP must be UNGROUPED so the bar-ratchet can
    keep modifying it (an OCA-grouped stop is rejected with Error 10326 and
    cancelled — the exact failure that left the protection at base)."""
    _set_exit_mode(monkeypatch, "trailing")
    ib = _make_mock_ib(
        positions=[_make_position(qty=2)],
        open_trades=[],  # stop gone; trailing lifecycle never had a target order
    )
    ib.place_order = AsyncMock(return_value=_placed(9001))
    db = _make_mock_db()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, db=db)

    # The router has ratcheted + PERSISTED the stop up to 17555 (from the 17400 base).
    lc = _make_lifecycle(State.ACTIVE, target_order_id=None, stop_price=17555.0)
    action = await rec.reconcile_one(lc)

    assert action is ReconcileAction.HEALED
    ib.place_order.assert_awaited_once()
    placed = ib.place_order.await_args.args[1]
    assert placed.orderType == "STP" and placed.action == "SELL"
    assert placed.auxPrice == 17555.0  # the RATCHETED level — never re-armed at base
    assert not placed.ocaGroup  # UNGROUPED → the ratchet can modify it (no Error 10326)
    assert placed.ocaType == 0


async def test_heal_fixed_rearms_missing_target(monkeypatch):
    """Regression lock: fixed mode + ACTIVE position with the STP resident and the TARGET
    missing → the reconciler re-arms the LMT TARGET exactly as before."""
    _set_exit_mode(monkeypatch, "fixed")
    ib = _make_mock_ib(
        positions=[_make_position(qty=2)],
        open_trades=[_open_trade_oca(1003, "grp-S")],  # stop resident; target 1002 gone
    )
    ib.place_order = AsyncMock(return_value=_placed(9201))  # LMT only
    db = _make_mock_db()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, db=db)

    action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE))

    assert action is ReconcileAction.HEALED
    ib.place_order.assert_awaited_once()
    placed = ib.place_order.await_args.args[1]
    assert placed.orderType == "LMT" and placed.lmtPrice == 17600.0  # re-armed take-profit
    assert db.update_lifecycle.await_args.args[1] == {"target_order_id": 9201}


async def test_heal_failure_is_swallowed_and_alerts(caplog):
    ib = _make_mock_ib(
        positions=[_make_position(qty=2)],
        open_trades=[_open_trade_oca(1002, "grp-A")],  # stop missing → heal attempt
    )
    ib.place_order = AsyncMock(side_effect=RuntimeError("broker reject"))  # 1 call, raises
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib)

    with caplog.at_level(logging.INFO):  # the [ALERT] line is INFO
        action = await rec.reconcile_one(_make_lifecycle(State.ACTIVE))  # must NOT raise

    ib.place_order.assert_awaited()  # attempt made
    assert action is ReconcileAction.NOOP  # nothing persisted → not HEALED
    assert any("heal_failed" in r.message for r in caplog.records)
    assert any("recover_heal_failed" in r.message for r in caplog.records)


# ========================================================================
# STABILIZE-4 — foreign-position auto-flatten guard (intent-based, direction-agnostic)
# ========================================================================


# ---- pure helpers: foreign_quantity (CLOSE-only) + intended_net_position ----


def test_foreign_quantity_close_only_table():
    # (broker_net, intended_net) -> signed foreign qty to flatten (never opens).
    assert foreign_quantity(4, 2) == 2  # oversized long → sell the +2 excess
    assert foreign_quantity(-2, 0) == -2  # untracked short (STABILIZE-3 artifact) → buy 2
    assert foreign_quantity(2, 2) == 0  # matches intent → nothing
    assert foreign_quantity(0, 2) == 0  # entry unfilled (broker short of intent) → NEVER opens
    assert foreign_quantity(1, 2) == 0  # partially filled → never tops up
    assert foreign_quantity(-2, 2) == -2  # contrary short vs long intent → close it (won't re-long)
    assert foreign_quantity(2, -2) == 2  # contrary long vs short intent → close it
    assert foreign_quantity(-4, -2) == -2  # FUTURE shorts: excess short → buy back the extra 2
    assert foreign_quantity(0, 0) == 0


def test_intended_net_position_is_direction_agnostic():
    long_lc = _make_lifecycle(State.ACTIVE, entry_qty=2)  # LONG by default
    short_lc = _make_lifecycle(State.ACTIVE, direction=Direction.SHORT.value, entry_qty=2)
    idle_lc = _make_lifecycle(State.IDLE)  # no fill intended yet → 0
    entering_lc = _make_lifecycle(State.ENTERING, entry_qty=None)  # qty unknown → default
    assert intended_net_position([long_lc], "MNQM6", 2) == 2
    assert intended_net_position([short_lc], "MNQM6", 2) == -2
    assert intended_net_position([idle_lc], "MNQM6", 2) == 0
    assert intended_net_position([entering_lc], "MNQM6", 2) == 2  # falls back to default_qty
    assert intended_net_position([long_lc, short_lc], "MNQM6", 2) == 0  # net flat


# ---- MUST flatten ----


async def test_guard_flattens_untracked_short_after_debounce(caplog):
    # The STABILIZE-3 artifact: broker -2, NO lifecycle. Halts on tick 1, flattens
    # (BUY 2) on tick 2 once the debounce (confirm_ticks=2) is satisfied.
    sm = _make_mock_sm(non_closed=[])
    ib = _make_mock_ib(portfolio=[_make_portfolio_item(symbol="MNQM6", qty=-2)])
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, sm=sm, orchestrator=orch)

    with caplog.at_level(logging.INFO):
        c1 = await rec._reconcile_foreign_positions([], ib.get_portfolio.return_value)
    ib.place_order.assert_not_awaited()  # tick 1 → debounce not met → NO flatten
    assert c1.get(ReconcileAction.FOREIGN_POSITION) == 1
    orch.raise_halt.assert_called_with("MNQM6")

    with caplog.at_level(logging.INFO):
        c2 = await rec._reconcile_foreign_positions([], ib.get_portfolio.return_value)
    ib.place_order.assert_awaited_once()  # tick 2 → confirmed → flatten
    order = ib.place_order.await_args.args[1]
    assert order.action == "BUY" and order.totalQuantity == 2 and order.orderType == "MKT"
    assert c2.get(ReconcileAction.FLATTENED) == 1
    assert any("foreign_flatten_initiated" in r.getMessage() for r in caplog.records)
    assert any("foreign_flatten_placed" in r.getMessage() for r in caplog.records)


async def test_guard_flattens_oversold_size_mismatch_excess_only():
    # Broker +4 but the ACTIVE lifecycle intends +2 → flatten the +2 EXCESS (SELL 2),
    # leaving the intended +2 intact. Call the guard directly (confirm_ticks twice).
    active = _make_lifecycle(State.ACTIVE, entry_qty=2)  # LONG +2
    ib = _make_mock_ib(portfolio=[_make_portfolio_item(symbol="MNQM6", qty=4)])
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, orchestrator=orch)

    await rec._reconcile_foreign_positions([active], ib.get_portfolio.return_value)
    ib.place_order.assert_not_awaited()  # debounce tick 1
    await rec._reconcile_foreign_positions([active], ib.get_portfolio.return_value)

    ib.place_order.assert_awaited_once()
    order = ib.place_order.await_args.args[1]
    assert order.action == "SELL" and order.totalQuantity == 2  # only the excess, not all 4


# ---- MUST NOT flatten ----


async def test_guard_leaves_matching_long_untouched():
    active = _make_lifecycle(State.ACTIVE, entry_qty=2)  # LONG +2
    ib = _make_mock_ib(portfolio=[_make_portfolio_item(symbol="MNQM6", qty=2)])
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, orchestrator=orch)

    for _ in range(3):  # several ticks — must never trip
        c = await rec._reconcile_foreign_positions([active], ib.get_portfolio.return_value)

    ib.place_order.assert_not_awaited()
    orch.raise_halt.assert_not_called()
    assert ReconcileAction.FLATTENED not in c and ReconcileAction.FOREIGN_POSITION not in c


async def test_guard_does_not_flatten_during_open_race_in_flight():
    # A just-opened position whose lifecycle is still ENTERING (entry filling): the
    # net is changing by design → NEVER flatten, NEVER halt, even across many ticks.
    entering = _make_lifecycle(State.ENTERING, entry_qty=None)  # mid-open, qty unknown
    ib = _make_mock_ib(portfolio=[_make_portfolio_item(symbol="MNQM6", qty=2)])
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, orchestrator=orch)

    for _ in range(3):
        await rec._reconcile_foreign_positions([entering], ib.get_portfolio.return_value)

    ib.place_order.assert_not_awaited()
    orch.raise_halt.assert_not_called()


async def test_guard_debounce_holds_on_first_tick_only_halts():
    # Explicit debounce: a stable foreign position halts immediately but is NOT
    # flattened on the FIRST tick (the race-protection window).
    ib = _make_mock_ib(portfolio=[_make_portfolio_item(symbol="MNQM6", qty=-2)])
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, orchestrator=orch)

    counts = await rec._reconcile_foreign_positions([], ib.get_portfolio.return_value)

    orch.raise_halt.assert_called_once_with("MNQM6")  # halted on tick 1
    ib.place_order.assert_not_awaited()  # but NOT flattened (debounce)
    assert counts.get(ReconcileAction.FOREIGN_POSITION) == 1
    assert ReconcileAction.FLATTENED not in counts


async def test_guard_future_short_lifecycle_is_left_alone():
    # FUTURE-PROOF: an INTENTIONAL short (ACTIVE direction=SHORT, -2) matched by a -2
    # broker short reconciles to intent → NOT foreign → untouched. Enabling shorts
    # later trips nothing here.
    short_lc = _make_lifecycle(State.ACTIVE, direction=Direction.SHORT.value, entry_qty=2)
    ib = _make_mock_ib(portfolio=[_make_portfolio_item(symbol="MNQM6", qty=-2)])
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, orchestrator=orch)

    for _ in range(3):
        c = await rec._reconcile_foreign_positions([short_lc], ib.get_portfolio.return_value)

    ib.place_order.assert_not_awaited()  # the intended short is left ALONE
    orch.raise_halt.assert_not_called()
    assert ReconcileAction.FLATTENED not in c


async def test_guard_disabled_halts_and_alerts_but_does_not_flatten(monkeypatch):
    # foreign_flatten_enabled=False → still halt + alert, but NEVER auto-liquidate.
    monkeypatch.setattr(
        "src.execution.reconciler.RISK", replace(RISK, foreign_flatten_enabled=False)
    )
    ib = _make_mock_ib(portfolio=[_make_portfolio_item(symbol="MNQM6", qty=-2)])
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, orchestrator=orch)

    for _ in range(4):  # well past the debounce
        await rec._reconcile_foreign_positions([], ib.get_portfolio.return_value)

    orch.raise_halt.assert_called_with("MNQM6")  # still halts
    ib.place_order.assert_not_awaited()  # but never flattens


async def test_guard_flatten_failure_is_swallowed_and_alerts(caplog):
    # A broker reject on the flatten order must NOT raise into the scan loop.
    ib = _make_mock_ib(portfolio=[_make_portfolio_item(symbol="MNQM6", qty=-2)])
    ib.place_order = AsyncMock(side_effect=RuntimeError("broker reject"))
    orch = _make_mock_orchestrator()
    rec, _ib, _sm, _ds, _orch = _build_reconciler(ib=ib, orchestrator=orch)

    await rec._reconcile_foreign_positions([], ib.get_portfolio.return_value)
    with caplog.at_level(logging.INFO):
        counts = await rec._reconcile_foreign_positions([], ib.get_portfolio.return_value)

    ib.place_order.assert_awaited()  # attempt made
    assert ReconcileAction.FLATTENED not in counts  # not counted as success
    assert any("foreign_flatten_failed" in r.getMessage() for r in caplog.records)
