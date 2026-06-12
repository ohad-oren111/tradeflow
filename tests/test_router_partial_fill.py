"""PR #150 — partial-fill full-fill gate on the entry arm path (§0.5.227).

Regression coverage for HANDOFF_v27 §5: a 2-lot MKT entry filled as two 1-lot
partial executions ~50 ms apart, ``execDetailsEvent`` fired once per execution,
and BOTH callbacks ran a full arm + ENTERING→ACTIVE transition + DB PATCH
concurrently. The stale ``entry_qty=1`` write landed last, so the foreign-position
guard read intended=1 vs broker=2 and flattened a real contract.

Two cases, exactly as the brief requires:
  1. Two 1-lot partials of a 2-lot order (the v27 race) → exactly ONE arm, ONE DB
     write, ``entry_qty == 2``, stop qty == 2, ratchet bound to the live stop id,
     and NO foreign-position delta (intended_net == broker_net → nothing to flatten).
  2. A stuck partial that never completes → the router NEVER arms it (full-fill
     gate), and the reconciler force-fill backstop arms a protective stop from
     BROKER TRUTH (qty=1, the real held size), never a stale DB qty, never a flatten.

Mocked at the wrapper boundary (``OrderRouter.on_fill`` / the reconciler's
``reconcile_one``), NOT the raw ib_async exec callback — a single full-fill test
would pass while prod still races, so the partial sequence is modelled explicitly
by mutating the SAME Trade between callbacks (as ib_async does).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.risk_params import RISK
from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.execution.dirty_set import DirtySet
from src.execution.reconciler import ReconcileAction, Reconciler, foreign_quantity
from src.execution.router import OrderRouter
from src.state_machine import Direction, Lifecycle, State, StateMachine

pytestmark = pytest.mark.asyncio

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
        lc.target_order_id = None  # trailing: no resting TARGET
        lc.stop_price = 29421.88  # entry − 75
        lc.target_price = None
    if state in (State.ACTIVE, State.EXITING, State.CLOSED):
        lc.entry_qty = 2
        lc.entry_price = 29496.88
        lc.entry_filled_at = "2026-06-11T22:06:00+00:00"
        lc.stop_order_id = 1003
    for k, v in overrides.items():
        setattr(lc, k, v)
    return lc


def _make_contract() -> SimpleNamespace:
    return SimpleNamespace(localSymbol="MNQM6", symbol="MNQ", exchange="CME")


def _make_exec(shares: int, price: float) -> SimpleNamespace:
    """A concrete ib_async-shaped Fill (``fill.execution`` carries shares/price/time)."""
    return SimpleNamespace(
        execution=SimpleNamespace(
            shares=shares,
            price=price,
            avgPrice=price,
            time=datetime(2026, 6, 11, 22, 6, tzinfo=UTC),
        )
    )


def _make_entry_trade(order_id: int, total_qty: int) -> SimpleNamespace:
    """A working entry Trade with NO executions yet. ``totalQuantity`` is the size
    we submitted (a real broker order always carries it). ``fills`` is mutated
    between callbacks to model ib_async appending each partial execution to the
    SAME Trade; ``orderStatus`` mirrors the broker's running filled/remaining."""
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id, totalQuantity=total_qty),
        fills=[],
        orderStatus=SimpleNamespace(status="PreSubmitted", filled=0, remaining=total_qty),
    )


def _apply_partial(trade: SimpleNamespace, shares: int, price: float, total_qty: int) -> None:
    """Append a partial execution to ``trade`` and update its orderStatus, exactly
    as ib_async does before re-firing execDetailsEvent for the next execution."""
    trade.fills.append(_make_exec(shares, price))
    filled = sum(int(f.execution.shares) for f in trade.fills)
    trade.orderStatus.filled = filled
    trade.orderStatus.remaining = max(0, total_qty - filled)
    trade.orderStatus.status = "Filled" if trade.orderStatus.remaining == 0 else "Submitted"


def _make_router_ib() -> AsyncMock:
    ib = AsyncMock(spec=IBClient)
    ib.is_connected = True
    # ensure_protective_stop reads open trades for the cancel-before-arm sweep.
    ib.get_open_trades = AsyncMock(return_value=[])
    ib.get_positions = AsyncMock(
        return_value=[SimpleNamespace(contract=_make_contract(), position=2, avgCost=29496.88 * 2)]
    )
    # find_open_order_by_id is consulted by the ratchet; not exercised at arm time.
    ib.find_open_order_by_id = AsyncMock(return_value=None)
    placed = SimpleNamespace(order=SimpleNamespace(orderId=175))
    ib.place_order = AsyncMock(return_value=placed)
    return ib


def _make_router_sm(active_lc: Lifecycle) -> MagicMock:
    sm = MagicMock(spec=StateMachine)
    sm.transition = AsyncMock(return_value=active_lc)
    sm.persist_highest = AsyncMock()
    sm.persist_stop_price = AsyncMock()
    sm.load_non_closed = AsyncMock(return_value=[])
    return sm


# ============================================================== _entry_fill_progress


async def test_progress_partial_is_not_full_when_filled_lt_total():
    trade = _make_entry_trade(1001, total_qty=2)
    _apply_partial(trade, 1, 29482.25, total_qty=2)  # first 1-lot partial
    is_full, filled, intended = OrderRouter._entry_fill_progress(trade)
    assert (is_full, filled, intended) == (False, 1, 2)


async def test_progress_full_when_all_executions_present():
    trade = _make_entry_trade(1001, total_qty=2)
    _apply_partial(trade, 1, 29482.25, total_qty=2)
    _apply_partial(trade, 1, 29511.50, total_qty=2)  # second 1-lot partial → complete
    is_full, filled, intended = OrderRouter._entry_fill_progress(trade)
    assert (is_full, filled, intended) == (True, 2, 2)


async def test_progress_full_via_remaining_when_total_unknown():
    # No totalQuantity → fall back to orderStatus.remaining<=0.
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=1001),
        fills=[],
        orderStatus=SimpleNamespace(status="Filled", filled=2, remaining=0),
    )
    is_full, _filled, intended = OrderRouter._entry_fill_progress(trade)
    assert is_full is True
    assert intended is None


async def test_progress_partial_via_remaining_when_total_unknown():
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=1001),
        fills=[],
        orderStatus=SimpleNamespace(status="Submitted", filled=1, remaining=1),
    )
    is_full, filled, intended = OrderRouter._entry_fill_progress(trade)
    assert is_full is False
    # filled is read from orderStatus.filled when the fills list is empty.
    assert (filled, intended) == (1, None)


async def test_progress_fail_open_on_minimal_trade():
    # No qty signal at all (minimal fixture / sim) → fail open so a single
    # simulated fill still arms (preserves pre-gate unit-fixture behaviour).
    trade = SimpleNamespace(order=SimpleNamespace(orderId=1001), fills=[])
    is_full, filled, intended = OrderRouter._entry_fill_progress(trade)
    assert (is_full, filled, intended) == (True, 0, None)


# ============================================================== the v27 race (router)


async def test_two_partials_arm_exactly_once_with_aggregate_qty(monkeypatch, caplog):
    """The 2026-06-11 case: a 2-lot entry fills as two 1-lot executions ~50 ms apart.
    on_fill fires once per execution. Assert: the partial is SKIPPED (no arm), the
    full fill arms EXACTLY once, with entry_qty == 2 (aggregate), one protective stop
    of qty 2, the ratchet seeded/bound, and NO foreign delta."""
    monkeypatch.setattr("src.execution.router.RISK", replace(RISK, exit_mode="trailing"))
    caplog.set_level(logging.INFO)

    entering_lc = _make_lifecycle(State.ENTERING)
    active_lc = _make_lifecycle(State.ACTIVE, lifecycle_id=entering_lc.lifecycle_id)
    ib = _make_router_ib()
    sm = _make_router_sm(active_lc)

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[entering_lc.entry_order_id] = entering_lc  # type: ignore[index]
    router._by_lifecycle_id[entering_lc.lifecycle_id] = entering_lc
    router._contracts[entering_lc.lifecycle_id] = _make_contract()

    trade = _make_entry_trade(entering_lc.entry_order_id, total_qty=2)  # type: ignore[arg-type]

    # --- callback 1: first 1-lot partial (remaining=1) → must be SKIPPED ---
    _apply_partial(trade, 1, 29482.25, total_qty=2)
    await router.on_fill(trade, None)
    sm.transition.assert_not_awaited()  # no arm on the partial
    ib.place_order.assert_not_awaited()  # no protective stop placed on the partial
    assert any("entry_partial_fill_skipped" in r.getMessage() for r in caplog.records)

    # --- callback 2: second 1-lot partial completes the order (remaining=0) → ARM ---
    _apply_partial(trade, 1, 29511.50, total_qty=2)
    await router.on_fill(trade, None)

    # exactly ONE arm (ENTERING→ACTIVE) → one DB write, with the AGGREGATE qty.
    sm.transition.assert_awaited_once()
    call = sm.transition.await_args
    assert call.args[1] is State.ACTIVE
    assert call.kwargs["entry_qty"] == 2  # aggregate, not the stale 1
    # share-weighted vwap of the two 1-lot fills.
    assert call.kwargs["entry_price"] == pytest.approx((29482.25 + 29511.50) / 2)

    # exactly ONE protective stop, sized to the full position (qty=2).
    ib.place_order.assert_awaited_once()
    placed_order = ib.place_order.await_args.args[1]
    assert placed_order.totalQuantity == 2

    # ratchet seeded + bound to the live stop id (id 175 from the placement stub).
    assert router._highest[entering_lc.lifecycle_id] == pytest.approx((29482.25 + 29511.50) / 2)
    assert router._by_order_id[175].lifecycle_id == entering_lc.lifecycle_id

    # NO foreign-position trigger: the DB now records entry_qty=2 == broker net 2, so
    # the reconciler's foreign guard sees zero delta (the v27 flatten cannot fire).
    assert foreign_quantity(broker_net=2, intended_net=2) == 0


async def test_duplicate_full_fill_callback_is_idempotent(monkeypatch):
    """A re-fired FULL-fill callback (IB duplicate) must NOT place a second stop or
    race a second DB write — the single-shot ENTERING guard drops it."""
    monkeypatch.setattr("src.execution.router.RISK", replace(RISK, exit_mode="trailing"))

    entering_lc = _make_lifecycle(State.ENTERING)
    active_lc = _make_lifecycle(State.ACTIVE, lifecycle_id=entering_lc.lifecycle_id)
    ib = _make_router_ib()
    sm = _make_router_sm(active_lc)

    router = OrderRouter(ib=ib, sm=sm, strategy_name=STRAT_NAME)
    router._by_order_id[entering_lc.entry_order_id] = entering_lc  # type: ignore[index]
    router._by_lifecycle_id[entering_lc.lifecycle_id] = entering_lc
    router._contracts[entering_lc.lifecycle_id] = _make_contract()

    trade = _make_entry_trade(entering_lc.entry_order_id, total_qty=2)  # type: ignore[arg-type]
    _apply_partial(trade, 1, 29482.25, total_qty=2)
    _apply_partial(trade, 1, 29511.50, total_qty=2)

    await router.on_fill(trade, None)  # first full fill → arms (cache flips to ACTIVE)
    await router.on_fill(trade, None)  # duplicate full fill → idempotent no-op

    sm.transition.assert_awaited_once()
    ib.place_order.assert_awaited_once()


# ========================================= stuck-partial backstop (reconciler force-fill)


def _make_recon_ib(positions: list[Any], open_trades: list[Any] | None = None) -> AsyncMock:
    ib = AsyncMock(spec=IBClient)
    ib.is_connected = True
    ib.get_positions = AsyncMock(return_value=positions)
    ib.get_portfolio = AsyncMock(return_value=[])
    ib.get_open_trades = AsyncMock(return_value=open_trades or [])
    placed = SimpleNamespace(order=SimpleNamespace(orderId=9001))
    ib.place_order = AsyncMock(return_value=placed)
    return ib


def _make_recon_sm(active_lc: Lifecycle) -> MagicMock:
    sm = MagicMock(spec=StateMachine)
    sm.transition = AsyncMock(return_value=active_lc)
    sm.load_non_closed = AsyncMock(return_value=[])
    return sm


def _make_recon_db() -> AsyncMock:
    db = AsyncMock(spec=SupabaseClient)
    db.select_lifecycles_non_closed = AsyncMock(return_value=[])
    return db


def _make_position(qty: int, avg_cost: float) -> SimpleNamespace:
    return SimpleNamespace(contract=_make_contract(), position=qty, avgCost=avg_cost)


async def test_stuck_partial_backstop_arms_from_broker_truth():
    """A 1-of-2 partial that never completes: the router's full-fill gate means
    _handle_parent_fill never fires, so the lifecycle stays ENTERING. The reconciler
    force-fill backstop must arm a protective stop from BROKER TRUTH (qty=1, the real
    held size) and NEVER flatten the real contract."""
    # Broker truth: 1 contract actually held (the second lot never filled).
    ib = _make_recon_ib(positions=[_make_position(qty=1, avg_cost=29482.25 * 2)])
    entering_lc = _make_lifecycle(State.ENTERING)
    active_lc = _make_lifecycle(State.ACTIVE, lifecycle_id=entering_lc.lifecycle_id, entry_qty=1)
    sm = _make_recon_sm(active_lc)
    db = _make_recon_db()
    orch = MagicMock(name="orchestrator")
    orch.is_halted = MagicMock(return_value=False)

    rec = Reconciler(
        ib=ib,
        sm=sm,
        dirty_set=DirtySet(),
        db=db,
        orchestrator=orch,
        router=None,
        halt_ack_file_path=Path(f"/tmp/halt_clear_test_{uuid.uuid4()}"),
    )

    action = await rec.reconcile_one(entering_lc)

    # Force-filled to ACTIVE from broker truth — the position is NOT left unarmed.
    assert action is ReconcileAction.ENTERING_TO_ACTIVE
    call = sm.transition.await_args
    assert call.args[1] is State.ACTIVE
    # entry_qty is the BROKER-held size (1), not the stale DB/default intent (2).
    assert call.kwargs["entry_qty"] == 1
    # a protective stop was placed (id from the placement stub) — never naked (§0.5.T5).
    ib.place_order.assert_awaited_once()
    assert call.kwargs["stop_order_id"] == 9001
    # never a flatten: once armed at broker-truth qty=1, intended_net==broker_net=1,
    # so the foreign guard sees zero delta — the real contract is never sold off.
    assert foreign_quantity(broker_net=1, intended_net=1) == 0
