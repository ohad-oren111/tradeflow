"""In-process state machine for TradeFlow trade lifecycles.

Owns the IDLE → ENTERING → ACTIVE → EXITING → CLOSED transitions, persists each
transition to Supabase (``lifecycles`` row update + ``lifecycle_events`` audit
row), and exposes recovery helpers used by the orchestrator at boot.

PR #9 scope is plumbing only — no order placement, no strategy logic. The
state machine validates transitions and per-state field invariants and raises
``InvariantViolationError`` on misuse so bugs fail loud in dev.

The "at most one non-CLOSED lifecycle per (symbol, strategy)" invariant is
enforced in code via probe-before-insert (see ``create_lifecycle``) — NOT via a
DB UNIQUE constraint. Botty's G105/G106 root cause was identity coupling via
such a constraint; TradeFlow schema explicitly omits it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any

from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient

LOGGER = logging.getLogger(__name__)


class State(StrEnum):
    IDLE = "IDLE"
    ENTERING = "ENTERING"
    ACTIVE = "ACTIVE"
    EXITING = "EXITING"
    CLOSED = "CLOSED"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExitReason(StrEnum):
    TARGET = "TARGET"
    STOP = "STOP"
    EOD = "EOD"
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"


class InvariantViolationError(Exception):
    """Raised when a transition or per-state field invariant is violated."""


@dataclass
class Lifecycle:
    """Mirrors the ``lifecycles`` table. Nullable columns use ``Optional``."""

    lifecycle_id: str
    symbol: str
    strategy: str
    direction: str
    state: str

    entry_qty: int | None = None
    entry_price: float | None = None
    entry_filled_at: str | None = None
    entry_order_id: int | None = None
    stop_order_id: int | None = None
    target_order_id: int | None = None
    stop_price: float | None = None
    target_price: float | None = None

    exit_qty: int | None = None
    exit_price: float | None = None
    exit_filled_at: str | None = None
    exit_order_id: int | None = None
    exit_reason: str | None = None

    commission_total: float | None = None
    pnl_gross: float | None = None
    pnl_net: float | None = None

    metadata: dict[str, Any] = field(default_factory=dict)

    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Lifecycle:
        """Build a Lifecycle from a PostgREST row dict, ignoring unknown keys."""
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in row.items() if k in known}
        if "metadata" not in kwargs or kwargs["metadata"] is None:
            kwargs["metadata"] = {}
        return cls(**kwargs)


# Fields that the per-state invariants check. Centralised so the matrix in the
# PR brief and the code stay legible side-by-side.
_ENTRY_FIELDS = ("entry_qty", "entry_price", "entry_filled_at", "entry_order_id")
_EXIT_FIELDS = ("exit_qty", "exit_price", "exit_filled_at", "exit_order_id")
_PNL_FIELDS = ("commission_total", "pnl_gross", "pnl_net")


class StateMachine:
    """Async state machine bound to a SupabaseClient and IBClient.

    All transitions go through ``transition``. ``create_lifecycle`` is the only
    insert path. ``load_non_closed`` and ``boot_time_broker_check`` are the
    recovery helpers called by the orchestrator at startup.
    """

    ALLOWED_TRANSITIONS: dict[State, set[State]] = {
        State.IDLE: {State.ENTERING, State.CLOSED},
        State.ENTERING: {State.ACTIVE, State.CLOSED},
        State.ACTIVE: {State.EXITING},
        State.EXITING: {State.CLOSED},
        State.CLOSED: set(),
    }

    def __init__(self, db: SupabaseClient, ib: IBClient) -> None:
        self._db = db
        self._ib = ib

    # ------------------------------------------------------------------ create

    async def create_lifecycle(
        self,
        symbol: str,
        strategy: str,
        direction: Direction | str,
    ) -> Lifecycle:
        """Insert a new IDLE lifecycle. Rejects if a non-CLOSED row already exists
        for ``(symbol, strategy)`` (concurrency invariant enforced in code, not DB)."""
        dir_value = direction.value if isinstance(direction, Direction) else direction
        existing = await self._db.select_lifecycles_non_closed_for(symbol, strategy)
        if existing:
            raise InvariantViolationError(
                f"non-CLOSED lifecycle already exists for symbol={symbol} "
                f"strategy={strategy} (count={len(existing)}) — refusing to create"
            )

        payload = {
            "symbol": symbol,
            "strategy": strategy,
            "direction": dir_value,
            "state": State.IDLE.value,
            "metadata": {},
        }
        LOGGER.info(
            "[STATE] %s: create_lifecycle — strategy=%s direction=%s",
            symbol,
            strategy,
            dir_value,
        )
        rows = await self._db.insert_lifecycle(payload)
        row = rows[0] if isinstance(rows, list) else rows
        lc = Lifecycle.from_row(row)
        await self._db.insert_lifecycle_event(
            {
                "lifecycle_id": lc.lifecycle_id,
                "from_state": None,
                "to_state": State.IDLE.value,
                "reason": "create_lifecycle",
                "payload": {},
            }
        )
        return lc

    # -------------------------------------------------------------- transition

    async def transition(
        self,
        lifecycle: Lifecycle,
        new_state: State | str,
        reason: str,
        payload: dict[str, Any] | None = None,
        **field_updates: Any,
    ) -> Lifecycle:
        """Validate + apply a state transition and persist it.

        Best-effort two-step write: PATCH ``lifecycles`` then INSERT
        ``lifecycle_events``. Not atomic without a postgres function; the
        orchestrator tolerates a stale event row only on crash, which is
        acceptable for this PR's scope (no $ at risk).
        """
        target = new_state if isinstance(new_state, State) else State(new_state)
        current = State(lifecycle.state)

        if target not in self.ALLOWED_TRANSITIONS[current]:
            raise InvariantViolationError(
                f"disallowed transition {current.value} → {target.value} "
                f"for lifecycle_id={lifecycle.lifecycle_id}"
            )

        updated = replace(lifecycle, state=target.value, **field_updates)
        self._validate_invariants(updated, target)

        update_payload: dict[str, Any] = {"state": target.value}
        update_payload.update(field_updates)
        LOGGER.info(
            "[STATE] %s: transition — id=%s from=%s to=%s reason=%s",
            updated.symbol,
            updated.lifecycle_id,
            current.value,
            target.value,
            reason,
        )
        await self._db.update_lifecycle(lifecycle.lifecycle_id, update_payload)
        await self._db.insert_lifecycle_event(
            {
                "lifecycle_id": lifecycle.lifecycle_id,
                "from_state": current.value,
                "to_state": target.value,
                "reason": reason,
                "payload": payload or {},
            }
        )
        return updated

    async def persist_highest(self, lifecycle: Lifecycle, highest: float) -> None:
        """Durably store the per-position high-water mark in ``metadata.highest_price``.

        PR-2 — the EXIT_MODE=trailing ratchet tracks ``highest`` in-memory; this
        persists it (no schema migration: ``metadata`` is an existing JSONB column)
        so a restart mid-trend resumes from the true peak instead of reseeding to
        entry. NOT a state transition (ACTIVE→ACTIVE) — a plain PATCH of the
        ``metadata`` column, merged so no other key is clobbered. Mutates the
        in-memory ``lifecycle.metadata`` so the router's cache stays current."""
        meta = dict(lifecycle.metadata or {})
        meta["highest_price"] = float(highest)
        await self._db.update_lifecycle(lifecycle.lifecycle_id, {"metadata": meta})
        lifecycle.metadata = meta

    # ----------------------------------------------------------------- recovery

    async def load_non_closed(self) -> list[Lifecycle]:
        """Return every lifecycle with state != CLOSED."""
        rows = await self._db.select_lifecycles_non_closed()
        return [Lifecycle.from_row(r) for r in rows]

    async def boot_time_broker_check(self, lifecycle: Lifecycle) -> State | None:
        """Return the state the lifecycle SHOULD be in based on broker reality.

        Read-only: uses ``positions()`` + ``openTrades()`` only — safe under
        the current IBC Read-Only API setting (§0.5.123). Does NOT apply the
        transition; caller decides whether to call ``transition``.

        Returns ``None`` if broker reality matches the current DB state.
        """
        current = State(lifecycle.state)
        positions = await self._ib.get_positions()
        open_trades = await self._ib.get_open_trades()
        has_position = _broker_position_exists(positions, lifecycle.symbol)
        has_open_order = _broker_open_order_exists(open_trades, lifecycle.symbol)

        if current is State.IDLE:
            return None
        if current is State.ENTERING:
            if has_position:
                return State.ACTIVE
            if not has_open_order:
                return State.CLOSED
            return None
        if current is State.ACTIVE:
            if not has_position:
                return State.CLOSED
            return None
        if current is State.EXITING:
            if not has_position:
                return State.CLOSED
            return None
        return None

    # ---------------------------------------------------------------- invariants

    def _validate_invariants(self, lifecycle: Lifecycle, new_state: State) -> None:
        """Enforce the per-state field NULL/NOT-NULL matrix.

        Raises ``InvariantViolationError`` on mismatch. ``stop_*`` / ``target_*``
        fields stay optional in every state — strategy code owns those.
        """
        d = asdict(lifecycle)

        if new_state is State.IDLE:
            _require_all_null(d, _ENTRY_FIELDS + _EXIT_FIELDS + _PNL_FIELDS, new_state)
            return

        if new_state is State.ENTERING:
            _require_not_null(d, ("entry_order_id",), new_state)
            _require_all_null(
                d,
                ("entry_filled_at",) + _EXIT_FIELDS + _PNL_FIELDS,
                new_state,
            )
            return

        if new_state is State.ACTIVE:
            _require_not_null(d, _ENTRY_FIELDS, new_state)
            _require_all_null(d, _EXIT_FIELDS + _PNL_FIELDS, new_state)
            return

        if new_state is State.EXITING:
            _require_not_null(d, _ENTRY_FIELDS + ("exit_order_id",), new_state)
            _require_all_null(
                d,
                ("exit_filled_at", "exit_price", "exit_qty") + _PNL_FIELDS,
                new_state,
            )
            return

        if new_state is State.CLOSED:
            _require_not_null(
                d,
                _ENTRY_FIELDS + _EXIT_FIELDS + ("exit_reason",) + _PNL_FIELDS,
                new_state,
            )
            return


# --------------------------------------------------------------- module helpers


def _require_not_null(
    row: dict[str, Any],
    fields: tuple[str, ...],
    new_state: State,
) -> None:
    missing = [f for f in fields if row.get(f) is None]
    if missing:
        raise InvariantViolationError(f"{new_state.value} requires non-null fields: {missing}")


def _require_all_null(
    row: dict[str, Any],
    fields: tuple[str, ...],
    new_state: State,
) -> None:
    unexpected = [f for f in fields if row.get(f) is not None]
    if unexpected:
        raise InvariantViolationError(
            f"{new_state.value} requires null fields, got values for: {unexpected}"
        )


def _broker_position_exists(positions: list[Any], symbol: str) -> bool:
    """True if the broker reports a non-zero position for ``symbol``.

    Matches against ``contract.localSymbol`` first (futures use contract-month
    form, e.g. ``MNQM6``), falling back to ``contract.symbol`` for spot/equity.
    """
    for pos in positions:
        contract = getattr(pos, "contract", None)
        if contract is None:
            continue
        local = getattr(contract, "localSymbol", None)
        base = getattr(contract, "symbol", None)
        if symbol not in {local, base}:
            continue
        qty = getattr(pos, "position", 0)
        if qty:
            return True
    return False


def _broker_open_order_exists(open_trades: list[Any], symbol: str) -> bool:
    """True if there is an open trade (working order) for ``symbol``."""
    for trade in open_trades:
        contract = getattr(trade, "contract", None)
        if contract is None:
            continue
        local = getattr(contract, "localSymbol", None)
        base = getattr(contract, "symbol", None)
        if symbol in {local, base}:
            return True
    return False
