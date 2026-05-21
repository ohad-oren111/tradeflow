"""Tests for orchestrator boot-time lifecycle recovery + broker-drift check.

Exercises StateMachine.load_non_closed, StateMachine.boot_time_broker_check,
and Orchestrator._recover_state in isolation from IB/DB transport.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.orchestrator import Orchestrator
from src.state_machine import Lifecycle, State, StateMachine

ENTRY_FILLED = "2026-05-21T15:00:00+00:00"


def _make_mock_db() -> AsyncMock:
    db = AsyncMock(spec=SupabaseClient)
    db.insert_lifecycle = AsyncMock(return_value=[{}])
    db.update_lifecycle = AsyncMock(return_value=[{}])
    db.select_lifecycles_non_closed = AsyncMock(return_value=[])
    db.select_lifecycles_non_closed_for = AsyncMock(return_value=[])
    db.insert_lifecycle_event = AsyncMock(return_value=[{}])
    return db


def _make_mock_ib_for_recovery(
    positions: list | None = None,
    open_trades: list | None = None,
    net_liq: str = "1000000.00",
) -> AsyncMock:
    """Mock IBClient with the read-only methods used during recovery + startup."""
    ib = AsyncMock(spec=IBClient)
    ib._host = "127.0.0.1"
    ib._port = 4002
    ib._client_id = 1
    ib.get_positions = AsyncMock(return_value=positions or [])
    ib.get_open_trades = AsyncMock(return_value=open_trades or [])
    raw_ib = MagicMock(name="raw_IB")
    av = MagicMock(name="AccountValue")
    av.tag = "NetLiquidation"
    av.value = net_liq
    raw_ib.accountSummaryAsync = AsyncMock(return_value=[av])
    raw_ib.client = MagicMock()
    raw_ib.client.serverVersion = MagicMock(return_value=178)
    fake_time = MagicMock(name="datetime")
    fake_time.strftime = MagicMock(return_value="2026-05-21T15:00:00Z")
    raw_ib.reqCurrentTimeAsync = AsyncMock(return_value=fake_time)
    ib._ib = raw_ib
    ib.disconnect = MagicMock(return_value=None)
    return ib


def _make_position(symbol: str, qty: int) -> MagicMock:
    pos = MagicMock(name="Position")
    pos.contract = MagicMock()
    pos.contract.localSymbol = symbol
    pos.contract.symbol = symbol[:3] if len(symbol) > 3 else symbol
    pos.position = qty
    return pos


def _make_open_trade(symbol: str) -> MagicMock:
    trade = MagicMock(name="Trade")
    trade.contract = MagicMock()
    trade.contract.localSymbol = symbol
    trade.contract.symbol = symbol[:3] if len(symbol) > 3 else symbol
    return trade


def _make_lifecycle_row(state: State, symbol: str = "MNQM6") -> dict:
    base = {
        "lifecycle_id": str(uuid.uuid4()),
        "symbol": symbol,
        "strategy": "sma100_bounce",
        "direction": "LONG",
        "state": state.value,
        "metadata": {},
    }
    if state in (State.ENTERING, State.ACTIVE, State.EXITING):
        base["entry_order_id"] = 10001
    if state in (State.ACTIVE, State.EXITING):
        base["entry_qty"] = 2
        base["entry_price"] = 17500.25
        base["entry_filled_at"] = ENTRY_FILLED
    if state is State.EXITING:
        base["exit_order_id"] = 10002
    return base


# -------------------------------------------------------------- load_non_closed


async def test_load_non_closed_returns_only_open_lifecycles():
    db, ib = _make_mock_db(), _make_mock_ib_for_recovery()
    db.select_lifecycles_non_closed = AsyncMock(
        return_value=[
            _make_lifecycle_row(State.ACTIVE),
            _make_lifecycle_row(State.ENTERING),
        ]
    )
    sm = StateMachine(db=db, ib=ib)

    lifecycles = await sm.load_non_closed()

    assert len(lifecycles) == 2
    assert {lc.state for lc in lifecycles} == {"ACTIVE", "ENTERING"}


# ---------------------------------------------------------- boot_time_broker_check


async def test_boot_time_broker_check_entering_with_position_returns_active():
    db, ib = _make_mock_db(), _make_mock_ib_for_recovery(positions=[_make_position("MNQM6", 2)])
    sm = StateMachine(db=db, ib=ib)
    lc = Lifecycle.from_row(_make_lifecycle_row(State.ENTERING))

    target = await sm.boot_time_broker_check(lc)

    assert target is State.ACTIVE


async def test_boot_time_broker_check_entering_with_no_position_no_order_returns_closed():
    db, ib = _make_mock_db(), _make_mock_ib_for_recovery()
    sm = StateMachine(db=db, ib=ib)
    lc = Lifecycle.from_row(_make_lifecycle_row(State.ENTERING))

    target = await sm.boot_time_broker_check(lc)

    assert target is State.CLOSED


async def test_boot_time_broker_check_active_with_no_position_returns_closed():
    db, ib = _make_mock_db(), _make_mock_ib_for_recovery()
    sm = StateMachine(db=db, ib=ib)
    lc = Lifecycle.from_row(_make_lifecycle_row(State.ACTIVE))

    target = await sm.boot_time_broker_check(lc)

    assert target is State.CLOSED


async def test_boot_time_broker_check_state_matches_broker_returns_none():
    db, ib = _make_mock_db(), _make_mock_ib_for_recovery(positions=[_make_position("MNQM6", 2)])
    sm = StateMachine(db=db, ib=ib)
    lc = Lifecycle.from_row(_make_lifecycle_row(State.ACTIVE))

    target = await sm.boot_time_broker_check(lc)

    assert target is None


async def test_boot_time_broker_check_entering_with_open_order_no_position_returns_none():
    """Still in-flight: order working at broker but no fill yet."""
    db, ib = _make_mock_db(), _make_mock_ib_for_recovery(open_trades=[_make_open_trade("MNQM6")])
    sm = StateMachine(db=db, ib=ib)
    lc = Lifecycle.from_row(_make_lifecycle_row(State.ENTERING))

    target = await sm.boot_time_broker_check(lc)

    assert target is None


async def test_boot_time_broker_check_exiting_with_no_position_returns_closed():
    db, ib = _make_mock_db(), _make_mock_ib_for_recovery()
    sm = StateMachine(db=db, ib=ib)
    lc = Lifecycle.from_row(_make_lifecycle_row(State.EXITING))

    target = await sm.boot_time_broker_check(lc)

    assert target is State.CLOSED


# --------------------------------------------------------- Orchestrator wiring


async def test_recovery_applies_transitions_for_drift(caplog):
    caplog.set_level(logging.INFO)
    db = _make_mock_db()
    row = _make_lifecycle_row(State.ENTERING)
    db.select_lifecycles_non_closed = AsyncMock(return_value=[row])
    ib = _make_mock_ib_for_recovery(positions=[_make_position("MNQM6", 2)])

    orch = Orchestrator(ib, db, paper_account="DUQ1234567", healthcheck_interval=10.0)

    async def stopper():
        for _ in range(20):
            await asyncio.sleep(0)
        if orch._stop_event is not None:
            orch._stop_event.set()

    with patch.object(orch, "_install_signal_handlers"):
        await asyncio.wait_for(asyncio.gather(orch.run(), stopper()), timeout=2.0)

    # One transition applied: ENTERING → ACTIVE based on broker position.
    db.update_lifecycle.assert_awaited_once()
    update_call = db.update_lifecycle.await_args
    assert update_call.args[0] == row["lifecycle_id"]
    assert update_call.args[1]["state"] == "ACTIVE"


async def test_recovery_logs_count_and_transitions_applied(caplog):
    caplog.set_level(logging.INFO)
    db = _make_mock_db()
    db.select_lifecycles_non_closed = AsyncMock(return_value=[_make_lifecycle_row(State.ACTIVE)])
    # ACTIVE with position present → no drift → no transition.
    ib = _make_mock_ib_for_recovery(positions=[_make_position("MNQM6", 2)])

    orch = Orchestrator(ib, db, paper_account="DUQ1234567", healthcheck_interval=10.0)

    async def stopper():
        for _ in range(20):
            await asyncio.sleep(0)
        if orch._stop_event is not None:
            orch._stop_event.set()

    with patch.object(orch, "_install_signal_handlers"):
        await asyncio.wait_for(asyncio.gather(orch.run(), stopper()), timeout=2.0)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("[ORCH] state: recovery_loaded — count=1" in m for m in msgs)
    assert any("[ORCH] state: recovery_complete — transitions_applied=0" in m for m in msgs)
    db.update_lifecycle.assert_not_awaited()


async def test_recovery_logs_empty_count_when_no_lifecycles(caplog):
    caplog.set_level(logging.INFO)
    db = _make_mock_db()
    ib = _make_mock_ib_for_recovery()

    orch = Orchestrator(ib, db, paper_account="DUQ1234567", healthcheck_interval=10.0)

    async def stopper():
        for _ in range(20):
            await asyncio.sleep(0)
        if orch._stop_event is not None:
            orch._stop_event.set()

    with patch.object(orch, "_install_signal_handlers"):
        await asyncio.wait_for(asyncio.gather(orch.run(), stopper()), timeout=2.0)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("[ORCH] state: recovery_loaded — count=0" in m for m in msgs)
    assert any("[ORCH] state: recovery_complete — transitions_applied=0" in m for m in msgs)


async def test_recovery_does_not_call_ib_on_empty_lifecycle_list():
    """Empty recovery skips broker probes — important during cold-start."""
    db = _make_mock_db()
    ib = _make_mock_ib_for_recovery()

    orch = Orchestrator(ib, db, paper_account="DUQ1234567", healthcheck_interval=10.0)

    async def stopper():
        for _ in range(10):
            await asyncio.sleep(0)
        if orch._stop_event is not None:
            orch._stop_event.set()

    with patch.object(orch, "_install_signal_handlers"):
        await asyncio.wait_for(asyncio.gather(orch.run(), stopper()), timeout=2.0)

    ib.get_positions.assert_not_awaited()
    ib.get_open_trades.assert_not_awaited()


@pytest.fixture(autouse=True)
def _stable_event_loop():
    """No-op — placeholder to keep this file's fixture surface explicit."""
    yield
