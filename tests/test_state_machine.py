"""Tests for src.state_machine — mocked at the SupabaseClient/IBClient boundary."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.state_machine import (
    Direction,
    InvariantViolationError,
    Lifecycle,
    State,
    StateMachine,
)

ENTRY_FILLED = "2026-05-21T15:00:00+00:00"
EXIT_FILLED = "2026-05-21T15:30:00+00:00"


def _make_mock_db() -> AsyncMock:
    """Fresh AsyncMock(spec=SupabaseClient). Per-test return_values must be set."""
    db = AsyncMock(spec=SupabaseClient)
    db.insert_lifecycle = AsyncMock(return_value=[{}])
    db.update_lifecycle = AsyncMock(return_value=[{}])
    db.select_lifecycles_non_closed = AsyncMock(return_value=[])
    db.select_lifecycles_non_closed_for = AsyncMock(return_value=[])
    db.insert_lifecycle_event = AsyncMock(return_value=[{}])
    return db


def _make_mock_ib() -> AsyncMock:
    """Fresh AsyncMock(spec=IBClient). Per-test broker state must be set."""
    ib = AsyncMock(spec=IBClient)
    ib.get_positions = AsyncMock(return_value=[])
    ib.get_open_trades = AsyncMock(return_value=[])
    return ib


def _make_lifecycle(state: State, **overrides: Any) -> Lifecycle:
    base = Lifecycle(
        lifecycle_id=str(uuid.uuid4()),
        symbol="MNQM6",
        strategy="sma100_bounce",
        direction=Direction.LONG.value,
        state=state.value,
    )
    if state in (
        State.ENTERING,
        State.ACTIVE,
        State.EXITING,
        State.CLOSED,
    ):
        base.entry_order_id = 10001
    if state in (State.ACTIVE, State.EXITING, State.CLOSED):
        base.entry_qty = 2
        base.entry_price = 17500.25
        base.entry_filled_at = ENTRY_FILLED
    if state in (State.EXITING, State.CLOSED):
        base.exit_order_id = 10002
    if state is State.CLOSED:
        base.exit_qty = 2
        base.exit_price = 17525.00
        base.exit_filled_at = EXIT_FILLED
        base.exit_reason = "TARGET"
        base.commission_total = 2.50
        base.pnl_gross = 49.50
        base.pnl_net = 47.00
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ----------------------------------------------------------------- allowed paths


async def test_allowed_transition_idle_to_entering_succeeds():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.IDLE)

    updated = await sm.transition(lc, State.ENTERING, reason="entry_submitted", entry_order_id=42)

    assert updated.state == State.ENTERING.value
    assert updated.entry_order_id == 42
    db.update_lifecycle.assert_awaited_once()


async def test_allowed_transition_entering_to_active_succeeds():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.ENTERING)

    updated = await sm.transition(
        lc,
        State.ACTIVE,
        reason="entry_filled",
        entry_qty=2,
        entry_price=17500.25,
        entry_filled_at=ENTRY_FILLED,
    )

    assert updated.state == State.ACTIVE.value
    assert updated.entry_qty == 2


async def test_allowed_transition_active_to_exiting_succeeds():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.ACTIVE)

    updated = await sm.transition(lc, State.EXITING, reason="exit_submitted", exit_order_id=10002)

    assert updated.state == State.EXITING.value


async def test_allowed_transition_exiting_to_closed_succeeds():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.EXITING)

    updated = await sm.transition(
        lc,
        State.CLOSED,
        reason="exit_filled",
        exit_qty=2,
        exit_price=17525.00,
        exit_filled_at=EXIT_FILLED,
        exit_reason="TARGET",
        commission_total=2.50,
        pnl_gross=49.50,
        pnl_net=47.00,
    )

    assert updated.state == State.CLOSED.value
    assert updated.pnl_net == 47.00


async def test_allowed_transition_idle_to_closed_succeeds():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.IDLE)
    # IDLE → CLOSED requires all CLOSED-required fields populated.
    updated = await sm.transition(
        lc,
        State.CLOSED,
        reason="cancelled_pre_entry",
        entry_order_id=10001,
        entry_qty=0,
        entry_price=0.0,
        entry_filled_at=ENTRY_FILLED,
        exit_order_id=10001,
        exit_qty=0,
        exit_price=0.0,
        exit_filled_at=EXIT_FILLED,
        exit_reason="MANUAL",
        commission_total=0.0,
        pnl_gross=0.0,
        pnl_net=0.0,
    )

    assert updated.state == State.CLOSED.value


async def test_allowed_transition_entering_to_closed_succeeds():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.ENTERING)
    # ENTERING → CLOSED — entry order rejected/cancelled before fill.
    updated = await sm.transition(
        lc,
        State.CLOSED,
        reason="entry_rejected",
        entry_qty=0,
        entry_price=0.0,
        entry_filled_at=ENTRY_FILLED,
        exit_order_id=lc.entry_order_id,
        exit_qty=0,
        exit_price=0.0,
        exit_filled_at=EXIT_FILLED,
        exit_reason="MANUAL",
        commission_total=0.0,
        pnl_gross=0.0,
        pnl_net=0.0,
    )

    assert updated.state == State.CLOSED.value


# ---------------------------------------------------------------- disallowed paths


async def test_disallowed_transition_active_to_closed_raises():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.ACTIVE)

    with pytest.raises(InvariantViolationError, match="disallowed transition ACTIVE"):
        await sm.transition(lc, State.CLOSED, reason="x")


async def test_disallowed_transition_exiting_to_active_raises():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.EXITING)

    with pytest.raises(InvariantViolationError, match="disallowed transition EXITING"):
        await sm.transition(lc, State.ACTIVE, reason="x")


async def test_disallowed_transition_closed_to_any_raises():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.CLOSED)

    for target in (State.IDLE, State.ENTERING, State.ACTIVE, State.EXITING):
        with pytest.raises(InvariantViolationError, match="disallowed transition CLOSED"):
            await sm.transition(lc, target, reason="x")


# ------------------------------------------------------------------- invariants


async def test_invariant_active_requires_entry_filled_at():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.ENTERING)

    with pytest.raises(InvariantViolationError, match="entry_filled_at"):
        # Missing entry_filled_at — ACTIVE requires it
        await sm.transition(
            lc,
            State.ACTIVE,
            reason="entry_filled",
            entry_qty=2,
            entry_price=17500.25,
        )


async def test_invariant_closed_requires_pnl_net():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.EXITING)

    with pytest.raises(InvariantViolationError, match="pnl_net"):
        await sm.transition(
            lc,
            State.CLOSED,
            reason="exit_filled",
            exit_qty=2,
            exit_price=17525.00,
            exit_filled_at=EXIT_FILLED,
            exit_reason="TARGET",
            commission_total=2.50,
            pnl_gross=49.50,
            # pnl_net intentionally omitted
        )


async def test_invariant_idle_rejects_entry_fields():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    # Lifecycle in ENTERING with entry_order_id set; cannot demote to IDLE
    # because IDLE invariants forbid entry_order_id being populated. (Also,
    # ENTERING → IDLE is not in ALLOWED_TRANSITIONS — that's the first guard.)
    lc = _make_lifecycle(State.ENTERING)

    with pytest.raises(InvariantViolationError, match="disallowed transition ENTERING"):
        await sm.transition(lc, State.IDLE, reason="x")


# ------------------------------------------------------------- create_lifecycle


async def test_create_lifecycle_rejects_when_existing_non_closed():
    db, ib = _make_mock_db(), _make_mock_ib()
    db.select_lifecycles_non_closed_for = AsyncMock(
        return_value=[{"lifecycle_id": "abc", "state": "ACTIVE"}]
    )
    sm = StateMachine(db=db, ib=ib)

    with pytest.raises(InvariantViolationError, match="non-CLOSED lifecycle already exists"):
        await sm.create_lifecycle("MNQM6", "sma100_bounce", Direction.LONG)

    db.insert_lifecycle.assert_not_awaited()


async def test_create_lifecycle_inserts_idle_row():
    db, ib = _make_mock_db(), _make_mock_ib()
    new_id = str(uuid.uuid4())
    db.insert_lifecycle = AsyncMock(
        return_value=[
            {
                "lifecycle_id": new_id,
                "symbol": "MNQM6",
                "strategy": "sma100_bounce",
                "direction": "LONG",
                "state": "IDLE",
                "metadata": {},
            }
        ]
    )
    sm = StateMachine(db=db, ib=ib)

    lc = await sm.create_lifecycle("MNQM6", "sma100_bounce", Direction.LONG)

    assert lc.lifecycle_id == new_id
    assert lc.state == State.IDLE.value
    db.insert_lifecycle.assert_awaited_once()
    db.insert_lifecycle_event.assert_awaited_once()
    event_payload = db.insert_lifecycle_event.await_args.args[0]
    assert event_payload["to_state"] == "IDLE"
    assert event_payload["from_state"] is None


# ---------------------------------------------------------------- event + identity


async def test_transition_inserts_lifecycle_event_row():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.IDLE)

    await sm.transition(lc, State.ENTERING, reason="entry_submitted", entry_order_id=42)

    db.insert_lifecycle_event.assert_awaited_once()
    event_payload = db.insert_lifecycle_event.await_args.args[0]
    assert event_payload["lifecycle_id"] == lc.lifecycle_id
    assert event_payload["from_state"] == "IDLE"
    assert event_payload["to_state"] == "ENTERING"
    assert event_payload["reason"] == "entry_submitted"


async def test_transition_preserves_lifecycle_id_across_states():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.IDLE)
    original_id = lc.lifecycle_id

    lc = await sm.transition(lc, State.ENTERING, reason="entry_submitted", entry_order_id=42)
    lc = await sm.transition(
        lc,
        State.ACTIVE,
        reason="entry_filled",
        entry_qty=2,
        entry_price=17500.25,
        entry_filled_at=ENTRY_FILLED,
    )
    lc = await sm.transition(lc, State.EXITING, reason="exit_submitted", exit_order_id=10002)

    assert lc.lifecycle_id == original_id
    # update_lifecycle called once per transition with the stable id.
    for call in db.update_lifecycle.await_args_list:
        assert call.args[0] == original_id


async def test_transition_update_payload_only_contains_state_and_field_updates():
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.IDLE)

    await sm.transition(lc, State.ENTERING, reason="entry_submitted", entry_order_id=42)

    update_call = db.update_lifecycle.await_args
    assert update_call.args[0] == lc.lifecycle_id
    assert update_call.args[1] == {"state": "ENTERING", "entry_order_id": 42}


# ------------------------------------------------- PR-2 — persist_highest (durable)


async def test_persist_highest_merges_into_metadata_without_clobbering():
    """persist_highest PATCHes metadata.highest_price (not a state transition) and
    preserves any other metadata keys; the in-memory lifecycle is updated too."""
    db, ib = _make_mock_db(), _make_mock_ib()
    sm = StateMachine(db=db, ib=ib)
    lc = _make_lifecycle(State.ACTIVE, metadata={"foo": "bar"})

    await sm.persist_highest(lc, 20120.5)

    db.update_lifecycle.assert_awaited_once()
    lifecycle_id_arg, updates = db.update_lifecycle.await_args.args
    assert lifecycle_id_arg == lc.lifecycle_id
    assert updates == {"metadata": {"foo": "bar", "highest_price": 20120.5}}
    assert lc.metadata["highest_price"] == 20120.5  # in-memory cache updated
