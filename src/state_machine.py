"""In-process state machine for TradeFlow trade lifecycles.

Owns the IDLE → ENTERING → ACTIVE → EXITING → CLOSED transitions, persists each
transition to Supabase (``lifecycles`` row update + ``lifecycle_events`` audit
row), and exposes recovery helpers used by the orchestrator at boot.

PR #9 scope is plumbing only — no order placement, no strategy logic. The
state machine validates transitions and per-state field invariants and raises
``InvariantViolationError`` on misuse so bugs fail loud in dev.

Concurrency (PR-B) allows up to ``max_concurrent`` non-CLOSED lifecycles per
(symbol, strategy), but never two brackets for the SAME triggering bar. This is
enforced defence-in-depth (see ``create_lifecycle``): an in-process
``asyncio.Lock`` + a count gate + a same-setup dedup (an in-lock probe plus a
PARTIAL UNIQUE INDEX on ``(symbol, strategy, (metadata->>'setup_key'))
WHERE state <> 'CLOSED'``, migration ``20260605000000_lifecycles_setup_key_dedup``,
which REPLACES the PR-A ≤1 index) + an aggregate contract cap. This is deliberately
NOT the full natural-key UNIQUE constraint that caused Botty's G105/G106 identity
coupling: the surrogate ``lifecycle_id`` UUID stays the primary key and the index
is PARTIAL, so unlimited CLOSED rows (full history + re-entry after close) remain
per (symbol, strategy) — only the live-position-per-setup subset is constrained.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any

import httpx

from config.risk_params import RISK
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

    # Q4b — ADDITIVE broker-truth columns, backfilled by the reconciler from the
    # fills' commissionReport (async-arriving). NOT part of _PNL_FIELDS, so they
    # are unconstrained in every state and never gate an invariant. The kill-switch
    # still consumes pnl_net only — these never change its input.
    commission_broker: float | None = None
    realized_pnl_broker: float | None = None

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

    def __init__(
        self,
        db: SupabaseClient,
        ib: IBClient,
        *,
        max_concurrent: int = RISK.max_concurrent,
        aggregate_contract_cap: int = RISK.aggregate_contract_cap,
    ) -> None:
        self._db = db
        self._ib = ib
        # PR-A/PR-B — max non-CLOSED lifecycles allowed per (symbol, strategy).
        # PR-B default is 3 (RISK.max_concurrent) → up to 3 distinct-setup positions
        # stack; the count gate in create_lifecycle rejects the (max+1)th. Injected
        # (mirrors KillSwitch's ``params``) so tests can exercise N without mutating
        # the frozen RISK dataclass.
        self._max_concurrent = max_concurrent
        # PR-B — aggregate contract cap across ALL open positions (sum of entry_qty
        # over every non-CLOSED lifecycle + this entry's qty). 0 = disabled. Default
        # 8 (RISK.aggregate_contract_cap); non-binding while max_concurrent×size ≤ 8.
        self._aggregate_contract_cap = aggregate_contract_cap
        # GATE-1: serialises the create_lifecycle probe→insert critical section so
        # the two on-loop entry tasks (strategy bar-eval + SeanBot-trigger) cannot
        # interleave at the await boundary and both insert. The DB partial unique
        # index is the cross-process/cross-restart backstop behind this.
        self._create_lock = asyncio.Lock()

    # ------------------------------------------------------------------ create

    async def create_lifecycle(
        self,
        symbol: str,
        strategy: str,
        direction: Direction | str,
        *,
        setup_key: str | None = None,
        qty: int | None = None,
    ) -> Lifecycle:
        """Insert a new IDLE lifecycle, enforcing the concurrency guards.

        Three in-lock gates run before the insert (GATE-1 lock held throughout, so
        the strategy bar-eval loop and the SeanBot-trigger task — which interleave
        at the ``await`` boundary — see a consistent view):

          1. COUNT GATE (PR-A): reject the (max_concurrent+1)th non-CLOSED lifecycle
             for ``(symbol, strategy)``.
          2. SAME-SETUP DEDUP (PR-B): reject if a non-CLOSED lifecycle for
             ``(symbol, strategy)`` already carries this ``setup_key``. ``setup_key``
             is a per-triggering-bar identifier both entry paths compute identically
             (orchestrator ``_handle_trade_signal`` from ``last_decision``); without
             it the two paths reacting to the SAME bar would each pass the count gate
             (count < max) and create a SECOND bracket for one market moment — the
             2026-06-01 double-entry (lifecycles c06ed026 + 347d5a12, 124ms apart).
             A ``None`` key (degenerate: no settled bar) skips dedup — the count gate
             and lock still apply.
          3. AGGREGATE CONTRACT CAP (PR-B): reject if the summed ``entry_qty`` over
             ALL non-CLOSED lifecycles + this entry's ``qty`` would exceed
             ``aggregate_contract_cap`` (0 = disabled). A non-binding backstop while
             max_concurrent × size ≤ cap.

        DB BACKSTOP — a PARTIAL UNIQUE INDEX
        ``(symbol, strategy, (metadata->>'setup_key')) WHERE state <> 'CLOSED'``
        makes the DB reject a racing / cross-process / cross-restart second insert
        for the SAME setup with 23505 → PostgREST 409, caught here and surfaced as
        the same ``InvariantViolationError`` the in-lock dedup raises (one exception
        type for callers). See migration
        ``20260605000000_lifecycles_setup_key_dedup`` (replaces the ≤1 index)."""
        dir_value = direction.value if isinstance(direction, Direction) else direction
        async with self._create_lock:
            existing = await self._db.select_lifecycles_non_closed_for(symbol, strategy)
            # PR-A — count gate vs ``max_concurrent``. The exception message keeps the
            # "non-CLOSED lifecycle already exists" substring so callers (orchestrator
            # suppression) and tests are unchanged.
            if len(existing) >= self._max_concurrent:
                LOGGER.info(
                    "[STATE] %s: entry gated — %d/%d concurrent",
                    symbol,
                    len(existing),
                    self._max_concurrent,
                )
                raise InvariantViolationError(
                    f"non-CLOSED lifecycle already exists for symbol={symbol} "
                    f"strategy={strategy} (count={len(existing)}/{self._max_concurrent}) "
                    "— refusing to create"
                )

            # PR-B — same-setup dedup. If any open lifecycle for (symbol, strategy)
            # already carries this setup_key, a sibling entry path already took this
            # bar: reject BEFORE any order is placed (no second bracket). Only fires
            # for a real key — a None key cannot collapse distinct entries.
            if setup_key is not None:
                for row in existing:
                    meta = row.get("metadata") or {}
                    if isinstance(meta, dict) and meta.get("setup_key") == setup_key:
                        LOGGER.info(
                            "[STATE] %s: entry deduped — same setup %s already open",
                            symbol,
                            setup_key,
                        )
                        raise InvariantViolationError(
                            f"non-CLOSED lifecycle already exists for symbol={symbol} "
                            f"strategy={strategy} setup_key={setup_key} (same-setup) "
                            "— refusing to create"
                        )

            # PR-B — aggregate contract cap (0 = disabled). Sum entry_qty over ALL
            # non-CLOSED lifecycles (filled positions carry it; IDLE/ENTERING None→0)
            # and add this entry's intended qty. Broker truth for FILLED contracts
            # equals this sum (entry_qty is stamped from the fill), so no broker
            # round-trip is taken inside the lock.
            if self._aggregate_contract_cap > 0:
                all_open = await self._db.select_lifecycles_non_closed()
                open_contracts = sum(int(r.get("entry_qty") or 0) for r in all_open)
                projected = open_contracts + int(qty or 0)
                if projected > self._aggregate_contract_cap:
                    LOGGER.info(
                        "[STATE] %s: entry gated — aggregate cap %d/%d contracts",
                        symbol,
                        projected,
                        self._aggregate_contract_cap,
                    )
                    raise InvariantViolationError(
                        f"aggregate contract cap exceeded for symbol={symbol} "
                        f"strategy={strategy} (projected={projected}/"
                        f"{self._aggregate_contract_cap}) — refusing to create"
                    )

            metadata: dict[str, Any] = {}
            if setup_key is not None:
                metadata["setup_key"] = setup_key
            payload = {
                "symbol": symbol,
                "strategy": strategy,
                "direction": dir_value,
                "state": State.IDLE.value,
                "metadata": metadata,
            }
            LOGGER.info(
                "[STATE] %s: create_lifecycle — strategy=%s direction=%s",
                symbol,
                strategy,
                dir_value,
            )
            try:
                rows = await self._db.insert_lifecycle(payload)
            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp is not None and resp.status_code == 409:
                    LOGGER.warning(
                        "[STATE] %s: create rejected — same setup %s already open " "(db-unique)",
                        symbol,
                        setup_key,
                    )
                    raise InvariantViolationError(
                        f"non-CLOSED lifecycle already exists for symbol={symbol} "
                        f"strategy={strategy} setup_key={setup_key} (db-unique) "
                        "— refusing to create"
                    ) from exc
                raise
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

    async def persist_stop_price(self, lifecycle: Lifecycle, stop_price: float) -> None:
        """Durably store the ratcheted protective-stop level in ``stop_price``.

        STABILIZE-3 — the EXIT_MODE=trailing ratchet walks the resting broker STP UP
        each bar; this persists the new level so the reconciler's leg-heal re-arms a
        redeploy-dropped stop at the RATCHETED level, not the stale entry−75 base
        (the bug behind a stop that announced a level it didn't hold). NOT a state
        transition (ACTIVE→ACTIVE) — a plain PATCH of the ``stop_price`` column.
        Mutates the in-memory ``lifecycle.stop_price`` so the router's cache stays
        current. The invariant (a ratcheted stop is never LOWERED) is enforced by the
        caller: ``compute_ratcheted_stop`` only moves the stop toward price."""
        await self._db.update_lifecycle(lifecycle.lifecycle_id, {"stop_price": float(stop_price)})
        lifecycle.stop_price = float(stop_price)

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
