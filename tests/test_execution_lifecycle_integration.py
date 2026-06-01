"""End-to-end lifecycle integration tests (W-S15.3 Track B + D).

Unlike test_execution_router.py / test_execution_reconciler.py — which mock the
StateMachine — these drive the REAL OrderRouter + Reconciler + StateMachine
wiring through full lifecycles, mocking ONLY the two external boundaries:

  * IBClient  → an in-memory ``_FakeIB`` (order placement, cancels, fills)
  * Supabase  → an in-memory ``_FakeDB`` (the StateMachine's persistence)

This exercises the real W-S15.1 sibling-cancel call path and the W-S15.3
fill-derived exit-price path that isolated unit mocks can't reach. Three exit
routes are covered:

  1. TARGET fill (router fillEvent)  → protective STP cancelled, CLOSED.
  2. STOP fill   (router fillEvent)  → TP LMT cancelled, CLOSED.
  3. missed event (reconciler close) → position flat broker-side with no router
     fillEvent → reconciler cancels the surviving leg AND records the *actual*
     broker fill price (slippage-accurate), not the order price.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from config.instruments import MNQ
from config.risk_params import RISK
from src.execution.bracket import build_bracket, build_protective_stop
from src.execution.dirty_set import DirtySet
from src.execution.reconciler import Reconciler
from src.execution.router import OrderRouter, ensure_protective_stop
from src.state_machine import Direction, ExitReason, State, StateMachine
from src.strategy import Signal

STRAT_NAME = "sma100_bounce"
SYMBOL = "MNQM6"


# --------------------------------------------------------------- in-memory fakes


class _FakeDB:
    """Minimal in-memory stand-in for SupabaseClient — the StateMachine's 5 calls."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._seq = 0

    async def select_lifecycles_non_closed_for(self, symbol: str, strategy: str) -> list[dict]:
        return [
            r
            for r in self.rows.values()
            if r["symbol"] == symbol and r["strategy"] == strategy and r["state"] != "CLOSED"
        ]

    async def insert_lifecycle(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        self._seq += 1
        lcid = f"lc-{self._seq:04d}-00000000-0000-0000-0000-000000000000"
        full = {
            **row,
            "lifecycle_id": lcid,
            "created_at": "2026-05-29T00:00:00+00:00",
            "updated_at": "2026-05-29T00:00:00+00:00",
        }
        self.rows[lcid] = full
        return [full]

    async def update_lifecycle(self, lifecycle_id: str, updates: dict[str, Any]) -> list[dict]:
        self.rows[lifecycle_id].update(updates)
        return [self.rows[lifecycle_id]]

    async def select_lifecycles_non_closed(self) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["state"] != "CLOSED"]

    async def insert_lifecycle_event(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        self.events.append(row)
        return [row]


class _FakeIB:
    """Stateful in-memory IBClient. Tracks placed + cancelled orders and fills."""

    def __init__(self) -> None:
        self.is_connected = True
        self._next_oid = 0
        self.placed: list[tuple[int, Any]] = []
        self.cancelled: list[int] = []
        # PR #72 — the order objects handed to the cancel path, so tests can
        # assert a REAL Order (with .clientId) was passed, not a bare stub.
        self.cancelled_orders: list[Any] = []
        self._live: dict[int, Any] = {}
        self.positions: list[Any] = []
        self.open_trades: list[Any] = []
        self.fills: list[Any] = []

    async def place_order(self, contract: Any, order: Any) -> Any:
        self._next_oid += 1
        oid = self._next_oid
        with contextlib.suppress(Exception):
            order.orderId = oid
            order.clientId = 1  # real orders carry the placing client's id
        self.placed.append((oid, order))
        self._live[oid] = order  # live until cancelled (for cancel_order_by_id lookup)
        return SimpleNamespace(order=order, fills=[], contract=contract)

    async def cancel_order_by_id(self, order_id: int) -> bool:
        order = self._live.pop(order_id, None)
        if order is None:
            return False
        self.cancelled.append(order_id)
        self.cancelled_orders.append(order)
        return True

    async def get_positions(self) -> list[Any]:
        return self.positions

    async def get_portfolio(self) -> list[Any]:
        return []

    async def get_open_trades(self) -> list[Any]:
        return self.open_trades

    async def get_fills(self) -> list[Any]:
        return self.fills


# --------------------------------------------------------------------- factories


def _make_signal(entry: float, target: float, stop: float) -> Signal:
    return Signal(
        instrument=SYMBOL,
        direction="LONG",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        ma_fast_value=entry - 20,
        ma_slow_value=entry - 50,
        ma_gap=30.0,
        adx_value=25.0,
        timestamp=datetime.now(UTC),
    )


def _make_contract() -> MagicMock:
    c = MagicMock(name="Contract")
    c.localSymbol = SYMBOL
    c.symbol = "MNQ"
    return c


def _fill_trade(order_id: int, qty: int, price: float) -> SimpleNamespace:
    """A Trade carrying one execution — drives router.on_fill."""
    execution = SimpleNamespace(
        orderId=order_id, shares=qty, price=price, avgPrice=price, time=datetime.now(UTC)
    )
    fill = SimpleNamespace(execution=execution)
    return SimpleNamespace(order=SimpleNamespace(orderId=order_id), fills=[fill])


def _open_trade(order_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id),
        contract=SimpleNamespace(localSymbol=SYMBOL, symbol="MNQ"),
    )


def _broker_fill(order_id: int, qty: int, price: float) -> SimpleNamespace:
    """A Fill on IB.fills() — drives the reconciler's _resolve_exit_price."""
    return SimpleNamespace(
        execution=SimpleNamespace(orderId=order_id, shares=qty, price=price, avgPrice=price)
    )


# ------------------------------------------------------------------------- wiring


def _build() -> tuple[_FakeIB, _FakeDB, StateMachine, OrderRouter, Reconciler, DirtySet]:
    ib = _FakeIB()
    db = _FakeDB()
    sm = StateMachine(db, ib)  # type: ignore[arg-type]
    dirty = DirtySet()
    router = OrderRouter(ib, sm, strategy_name=STRAT_NAME, dirty_set=dirty)  # type: ignore[arg-type]
    orchestrator = MagicMock()
    orchestrator.is_halted.return_value = False
    orchestrator.halt_raised_at.return_value = None
    reconciler = Reconciler(ib, sm, dirty, db, orchestrator)  # type: ignore[arg-type]
    return ib, db, sm, router, reconciler, dirty


async def _enter_to_active(
    ib: _FakeIB, router: OrderRouter, *, entry: float, target: float, stop: float
) -> Any:
    """place_entry → parent fill → ACTIVE. Returns the live cached lifecycle.

    Order ids assigned by _FakeIB in sequence: parent=1, tp=2, protective stp=3.
    """
    signal = _make_signal(entry, target, stop)
    contract = _make_contract()
    lc = await router.place_entry(signal, contract)
    # Parent (entry) fill — order id 1.
    await router.on_fill(_fill_trade(lc.entry_order_id, RISK.contracts_per_trade, entry))
    return router._by_lifecycle_id[lc.lifecycle_id]


# --------------------------------------------------------------------- scenario 1


async def test_target_fill_cancels_protective_stop_and_closes():
    ib, db, sm, router, _recon, _dirty = _build()
    lc = await _enter_to_active(ib, router, entry=30400.0, target=30550.0, stop=30325.0)

    assert State(lc.state) is State.ACTIVE
    assert lc.target_order_id == 2
    assert lc.stop_order_id == 3  # protective STP placed on parent fill

    # TARGET (TP LMT, order id 2) fills at the limit.
    await router.on_fill(_fill_trade(lc.target_order_id, RISK.contracts_per_trade, 30550.0))

    closed = db.rows[lc.lifecycle_id]
    assert closed["state"] == State.CLOSED.value
    assert closed["exit_reason"] == ExitReason.TARGET.value
    assert closed["exit_price"] == 30550.0
    # The protective STP (3) was cancelled; the filled TP (2) was not re-cancelled.
    assert 3 in ib.cancelled
    assert 2 not in ib.cancelled


# --------------------------------------------------------------------- scenario 2


async def test_stop_fill_cancels_sibling_via_real_order_with_clientid():
    """PR #72 bug-reproduction: the sibling cancel must hand ``IB.cancelOrder`` a
    REAL Order carrying ``.clientId`` — the W-S15.1 ``_OrderIdRef`` stub (orderId
    only) raised AttributeError. Pre-fix this assertion fails (no clientId)."""
    ib, db, sm, router, _recon, _dirty = _build()
    lc = await _enter_to_active(ib, router, entry=30400.0, target=30550.0, stop=30325.0)

    await router.on_fill(_fill_trade(lc.stop_order_id, RISK.contracts_per_trade, 30325.0))

    # The resting TP sibling was cancelled through our in-process path...
    assert lc.target_order_id in ib.cancelled
    # ...with a real Order object (carries clientId), not a bare orderId stub.
    assert ib.cancelled_orders, "no cancelled order captured"
    cancelled = ib.cancelled_orders[-1]
    assert hasattr(cancelled, "clientId")
    assert cancelled.clientId == 1
    assert getattr(cancelled, "orderId", None) == lc.target_order_id


async def test_stop_fill_cancels_target_and_closes():
    ib, db, sm, router, _recon, _dirty = _build()
    lc = await _enter_to_active(ib, router, entry=30400.0, target=30550.0, stop=30325.0)

    # STOP (protective STP, order id 3) fills at the stop price.
    await router.on_fill(_fill_trade(lc.stop_order_id, RISK.contracts_per_trade, 30325.0))

    closed = db.rows[lc.lifecycle_id]
    assert closed["state"] == State.CLOSED.value
    assert closed["exit_reason"] == ExitReason.STOP.value
    assert closed["exit_price"] == 30325.0
    # The resting TP LMT (2) was cancelled; the filled STP (3) was not.
    assert 2 in ib.cancelled
    assert 3 not in ib.cancelled


# --------------------------------------------------------------------- scenario 3


async def test_missed_stop_event_reconciler_closes_cancels_leg_and_uses_fill_price():
    """Router missed the STOP fillEvent → reconciler closes, cancels the resting
    TP, and records the ACTUAL slipped fill price (W-S15.3), not the stop price."""
    ib, db, sm, router, recon, _dirty = _build()
    lc = await _enter_to_active(ib, router, entry=30400.0, target=30550.0, stop=30325.0)

    # Broker reality: position flat (stop filled, never routed), TP still resting.
    ib.positions = []
    ib.open_trades = [_open_trade(lc.target_order_id)]  # TP (2) still open
    # The stop (order 3) actually filled 15pt BELOW its 30325 trigger (slippage).
    slipped = 30310.0
    ib.fills = [_broker_fill(lc.stop_order_id, RISK.contracts_per_trade, slipped)]

    active = (await sm.load_non_closed())[0]
    action = await recon.reconcile_one(active)

    closed = db.rows[lc.lifecycle_id]
    assert closed["state"] == State.CLOSED.value
    assert closed["exit_reason"] == ExitReason.STOP.value
    # Slippage-accurate: records the actual fill (30310), NOT the stop price (30325).
    assert closed["exit_price"] == slipped
    expected_pnl_gross = (slipped - 30400.0) * RISK.contracts_per_trade * MNQ.multiplier
    assert closed["pnl_gross"] == pytest.approx(expected_pnl_gross)
    assert closed["pnl_net"] == pytest.approx(
        expected_pnl_gross - RISK.contracts_per_trade * MNQ.commission_rt_usd
    )
    # The surviving TP (2) was cancelled by the reconciler — no orphan left.
    assert lc.target_order_id in ib.cancelled
    assert action.value == "active_to_closed"


async def test_missed_stop_event_clean_fill_unchanged():
    """A clean (no-slippage) stop fill records the stop price exactly — the fix
    does not perturb the no-slippage case."""
    ib, db, sm, router, recon, _dirty = _build()
    lc = await _enter_to_active(ib, router, entry=30400.0, target=30550.0, stop=30325.0)

    ib.positions = []
    ib.open_trades = [_open_trade(lc.target_order_id)]
    # Filled exactly at the 30325 stop trigger — no slippage.
    ib.fills = [_broker_fill(lc.stop_order_id, RISK.contracts_per_trade, 30325.0)]

    active = (await sm.load_non_closed())[0]
    await recon.reconcile_one(active)

    closed = db.rows[lc.lifecycle_id]
    assert closed["exit_price"] == 30325.0


async def test_reconciler_falls_back_to_order_price_when_no_fill_visible():
    """If IB.fills() has no matching execution (e.g. fill predates the session),
    the reconciler falls back to the STOP/TARGET order price — prior behaviour,
    no regression."""
    ib, db, sm, router, recon, _dirty = _build()
    lc = await _enter_to_active(ib, router, entry=30400.0, target=30550.0, stop=30325.0)

    ib.positions = []
    ib.open_trades = [_open_trade(lc.target_order_id)]
    ib.fills = []  # no executions visible this session

    active = (await sm.load_non_closed())[0]
    await recon.reconcile_one(active)

    closed = db.rows[lc.lifecycle_id]
    # Falls back to the stop order price.
    assert closed["exit_price"] == 30325.0


# ----------------------------------------------------- PR #69: protective stop


def _position(qty: int, avg_cost: float) -> SimpleNamespace:
    """A broker Position. ``avg_cost`` is NOTIONAL (per-contract price × mult)."""
    return SimpleNamespace(
        contract=SimpleNamespace(localSymbol=SYMBOL, symbol="MNQ"),
        position=qty,
        avgCost=avg_cost,
    )


def _lc_stub(stop_price: float | None, stop_order_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        direction="LONG",
        stop_price=stop_price,
        stop_order_id=stop_order_id,
        symbol=SYMBOL,
        lifecycle_id="lc-stub",
    )


def test_protective_stop_built_with_outsidertth():
    # The STP must be eligible overnight (Globex), not RTH-only.
    stp = build_protective_stop(direction=Direction.LONG, qty=2, stop_price=30325.0)
    assert stp.orderType == "STP"
    assert stp.outsideRth is True


def test_bracket_tp_built_with_outsidertth():
    _parent, tp = build_bracket(
        direction=Direction.LONG,
        qty=2,
        entry_type="MKT",
        entry_lmt_price=None,
        target_price=30550.0,
    )
    assert tp.outsideRth is True


async def test_ensure_protective_stop_places_outsidertth_when_absent():
    ib = _FakeIB()
    lc = _lc_stub(stop_price=30325.0, stop_order_id=None)

    result = await ensure_protective_stop(
        ib,  # type: ignore[arg-type]
        _make_contract(),
        lc,  # type: ignore[arg-type]
        qty=2,
        existing_stop_is_live=False,
        component="ROUTER",
        path="unit",
    )

    assert len(ib.placed) == 1
    _oid, order = ib.placed[0]
    assert order.orderType == "STP"
    assert order.auxPrice == 30325.0
    assert order.outsideRth is True
    assert result == _oid


async def test_ensure_protective_stop_idempotent_when_stop_already_live():
    ib = _FakeIB()
    lc = _lc_stub(stop_price=30325.0, stop_order_id=77)

    result = await ensure_protective_stop(
        ib,  # type: ignore[arg-type]
        _make_contract(),
        lc,  # type: ignore[arg-type]
        qty=2,
        existing_stop_is_live=True,
        component="RECON",
        path="unit",
    )

    # No second stop placed; the existing id is returned.
    assert ib.placed == []
    assert result == 77


async def test_reconciler_force_fill_places_protective_stop_and_per_contract_entry():
    """The bug: a parent fill the router missed → reconciler force-fills ACTIVE
    but (pre-PR-69) placed NO stop and stored the NOTIONAL avgCost as entry."""
    ib, db, sm, router, recon, _dirty = _build()
    signal = _make_signal(entry=30445.0, target=30595.0, stop=30370.0)
    contract = _make_contract()
    lc = await router.place_entry(signal, contract)  # ENTERING; parent=1, tp=2
    assert State(lc.state) is State.ENTERING

    # Broker shows the position filled; the router never saw the fillEvent.
    notional = 30445.0 * MNQ.multiplier
    ib.positions = [_position(RISK.contracts_per_trade, notional)]
    ib.open_trades = []

    active = (await sm.load_non_closed())[0]
    action = await recon.reconcile_one(active)

    assert action.value == "entering_to_active"
    row = db.rows[lc.lifecycle_id]
    assert row["state"] == State.ACTIVE.value
    # Per-contract entry price, NOT the notional avgCost.
    assert row["entry_price"] == pytest.approx(30445.0)
    # A protective STP was placed (previously skipped on this path) with
    # outsideRth=True, and its id stamped onto the ACTIVE row.
    stps = [order for _oid, order in ib.placed if getattr(order, "orderType", None) == "STP"]
    assert len(stps) == 1
    assert stps[0].outsideRth is True
    assert stps[0].auxPrice == 30370.0
    assert row["stop_order_id"] is not None
