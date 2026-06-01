"""Periodic broker ↔ DB reconciliation for TradeFlow lifecycles.

Flavor 2 reconciler: re-checks broker reality on a dirty-set drain (30s) and a
full DB scan (5 min). Applies whatever state-machine transitions are needed
when broker reality and the ``lifecycles`` table diverge. Broker wins per
§0.5.98 — the DB is reconciled to match the broker, never the other way around.

The reconciler is a safety net under the event-driven router. It catches missed
fillEvents (network blips, IB Gateway restarts, races) and manual broker
actions. It also detects "foreign positions" (broker has a position with no
matching DB lifecycle) and asks the orchestrator to halt new entries until
restart — a real ack mechanism arrives in PR #12.

See PR #11's conflict-resolution table for the per-(state, broker) action
matrix; this module's :meth:`Reconciler.reconcile_one` implements it verbatim.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from config.instruments import MNQ
from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.execution.dirty_set import DirtySet
from src.execution.router import ensure_protective_stop
from src.state_machine import (
    Direction,
    ExitReason,
    InvariantViolationError,
    Lifecycle,
    State,
    StateMachine,
)

LOGGER = logging.getLogger(__name__)


class HaltCoordinator(Protocol):
    """Subset of ``Orchestrator`` the reconciler needs to raise + clear halts.

    Defined as a Protocol to avoid an import cycle with ``src.orchestrator``;
    tests pass a ``MagicMock`` that implements these four methods.
    """

    def raise_halt(self, symbol: str | None = None) -> None: ...
    def clear_halt(self, reason: str = "") -> None: ...
    def is_halted(self) -> bool: ...
    def halt_raised_at(self) -> datetime | None: ...


DEFAULT_HALT_ACK_FILE = Path("/tmp/halt_clear")


class ReconcileAction(StrEnum):
    NOOP = "noop"
    ENTERING_TO_ACTIVE = "entering_to_active"
    ENTERING_TO_CLOSED = "entering_to_closed"
    ACTIVE_TO_CLOSED = "active_to_closed"  # via EXITING
    EXITING_TO_CLOSED = "exiting_to_closed"
    FOREIGN_POSITION = "foreign_position"
    WARNING = "warning"  # logged anomaly, no transition
    RACE = "race"  # state machine guard fired (InvariantViolationError)


class Reconciler:
    """Periodic broker-vs-DB reconciliation. See module docstring."""

    def __init__(
        self,
        ib: IBClient,
        sm: StateMachine,
        dirty_set: DirtySet,
        db: SupabaseClient,
        orchestrator: HaltCoordinator,
        *,
        dirty_drain_interval_sec: float = 30.0,
        full_scan_interval_sec: float = 300.0,
        halt_ack_file_path: Path = DEFAULT_HALT_ACK_FILE,
    ) -> None:
        self._ib = ib
        self._sm = sm
        self._dirty_set = dirty_set
        self._db = db
        self._orchestrator = orchestrator
        self._dirty_drain_interval_sec = dirty_drain_interval_sec
        self._full_scan_interval_sec = full_scan_interval_sec
        self._halt_ack_file_path = halt_ack_file_path

    # --------------------------------------------------------------- per-lifecycle

    async def reconcile_one(self, lifecycle: Lifecycle) -> ReconcileAction:
        """Apply the conflict-resolution matrix to ``lifecycle``. Returns the
        action taken (NOOP / WARNING / one of the transition outcomes).

        Re-fetches broker state inside the call so dirty-set drains over many
        lifecycles see fresh broker data each iteration. Cheap because the
        underlying ``IB.positions()`` / ``IB.openTrades()`` calls hit the
        cached snapshot — not a fresh network round-trip.
        """
        current = State(lifecycle.state)
        positions = await self._ib.get_positions()
        open_trades = await self._ib.get_open_trades()

        broker_qty = _broker_qty_for(positions, lifecycle.symbol)
        has_position = broker_qty is not None and broker_qty != 0
        qty_matches = (
            has_position
            and lifecycle.entry_qty is not None
            and abs(broker_qty) == lifecycle.entry_qty  # type: ignore[arg-type]
        )

        entry_order_open = lifecycle.entry_order_id is not None and _order_in_open_trades(
            open_trades, lifecycle.entry_order_id
        )
        stop_order_open = lifecycle.stop_order_id is not None and _order_in_open_trades(
            open_trades, lifecycle.stop_order_id
        )
        target_order_open = lifecycle.target_order_id is not None and _order_in_open_trades(
            open_trades, lifecycle.target_order_id
        )
        exit_orders_open = stop_order_open or target_order_open

        try:
            if current is State.IDLE:
                return await self._reconcile_idle(lifecycle, has_position)
            if current is State.ENTERING:
                return await self._reconcile_entering(
                    lifecycle,
                    has_position=has_position,
                    qty_matches=qty_matches,
                    broker_qty=broker_qty,
                    positions=positions,
                    open_trades=open_trades,
                    entry_order_open=entry_order_open,
                )
            if current is State.ACTIVE:
                return await self._reconcile_active(
                    lifecycle,
                    has_position=has_position,
                    stop_order_open=stop_order_open,
                    target_order_open=target_order_open,
                )
            if current is State.EXITING:
                return await self._reconcile_exiting(
                    lifecycle,
                    has_position=has_position,
                    exit_orders_open=exit_orders_open,
                    stop_order_open=stop_order_open,
                    target_order_open=target_order_open,
                )
        except InvariantViolationError as exc:
            LOGGER.warning(
                "[RECON] race: %s — id=%s (transition skipped, state machine guard fired)",
                lifecycle.symbol,
                lifecycle.lifecycle_id,
                exc_info=False,
            )
            LOGGER.debug("[RECON] race detail: %s", exc)
            return ReconcileAction.RACE
        return ReconcileAction.NOOP

    # ------------------------------------------------------------------- per-state

    async def _reconcile_idle(self, lifecycle: Lifecycle, has_position: bool) -> ReconcileAction:
        if has_position:
            LOGGER.warning(
                "[RECON] %s: idle_with_position — id=%s broker has position but DB is IDLE",
                lifecycle.symbol,
                lifecycle.lifecycle_id,
            )
            return ReconcileAction.WARNING
        return ReconcileAction.NOOP

    async def _reconcile_entering(
        self,
        lifecycle: Lifecycle,
        *,
        has_position: bool,
        qty_matches: bool,
        broker_qty: int | None,
        positions: list[Any],
        open_trades: list[Any],
        entry_order_open: bool,
    ) -> ReconcileAction:
        # Position present (any qty) → transition to ACTIVE with broker-sourced fields.
        if has_position:
            avg_cost = _broker_avg_cost_for(positions, lifecycle.symbol)
            qty = abs(broker_qty) if broker_qty is not None else lifecycle.entry_qty or 0
            # avgCost is NOTIONAL for futures (price × multiplier). Store the
            # per-contract entry price, mirroring the router fill-price path —
            # otherwise pnl is computed off a 2× price (the b39a4def bug:
            # 60890.12 stored for a ~30445 fill).
            entry_price = avg_cost / MNQ.multiplier if MNQ.multiplier else avg_cost
            # §0.5.T5 — the router never saw the parent fillEvent on this path, so
            # it never placed the protective STP. Place it here (idempotent,
            # outsideRth=True) BEFORE the transition so stop_order_id lands in the
            # same ACTIVE row the normal router path produces. No completion path
            # may leave a position stop-less.
            stop_order_id = await self._ensure_protective_stop_on_force_fill(
                lifecycle, positions=positions, open_trades=open_trades, qty=qty
            )
            extra: dict[str, Any] = {}
            if stop_order_id is not None:
                extra["stop_order_id"] = stop_order_id
            await self._sm.transition(
                lifecycle,
                State.ACTIVE,
                reason="recon_entering_to_active",
                payload={"broker_qty": broker_qty, "qty_matches": qty_matches},
                entry_qty=qty,
                entry_price=entry_price,
                entry_filled_at=_now_iso(),
                **extra,
            )
            self._log_action(lifecycle, ReconcileAction.ENTERING_TO_ACTIVE)
            return ReconcileAction.ENTERING_TO_ACTIVE

        # No position + entry order still working → still in flight, leave alone.
        if entry_order_open:
            return ReconcileAction.NOOP

        # No position + no working entry order → entry never filled, close as MANUAL.
        now = _now_iso()
        await self._sm.transition(
            lifecycle,
            State.CLOSED,
            reason="recon_entering_to_closed_manual",
            payload={},
            entry_qty=0,
            entry_price=0.0,
            entry_filled_at=now,
            exit_qty=0,
            exit_price=0.0,
            exit_filled_at=now,
            exit_order_id=lifecycle.entry_order_id or 0,
            exit_reason=ExitReason.MANUAL.value,
            commission_total=0.0,
            pnl_gross=0.0,
            pnl_net=0.0,
        )
        self._log_action(lifecycle, ReconcileAction.ENTERING_TO_CLOSED)
        return ReconcileAction.ENTERING_TO_CLOSED

    async def _ensure_protective_stop_on_force_fill(
        self,
        lifecycle: Lifecycle,
        *,
        positions: list[Any],
        open_trades: list[Any],
        qty: int,
    ) -> int | None:
        """Place the protective STP for a reconciler force-filled entry (§0.5.T5).

        Uses the broker position's qualified contract (the reconciler keeps no
        contract cache of its own). Idempotent: if the lifecycle already tracks a
        STP still resting at the broker, places nothing. Returns the stop order id
        (existing or new), or ``None`` if no contract / stop_price is available.
        """
        position = _broker_position_for(positions, lifecycle.symbol)
        contract = getattr(position, "contract", None) if position is not None else None
        if contract is None:
            LOGGER.warning(
                "[RECON] %s: protective_stop_skipped — no broker contract for id=%s",
                lifecycle.symbol,
                lifecycle.lifecycle_id,
            )
            return None
        existing_stop_is_live = lifecycle.stop_order_id is not None and _order_in_open_trades(
            open_trades, lifecycle.stop_order_id
        )
        return await ensure_protective_stop(
            self._ib,
            contract,
            lifecycle,
            qty=qty,
            existing_stop_is_live=existing_stop_is_live,
            component="RECON",
            path="recon_force_fill",
        )

    async def _reconcile_active(
        self,
        lifecycle: Lifecycle,
        *,
        has_position: bool,
        stop_order_open: bool,
        target_order_open: bool,
    ) -> ReconcileAction:
        if has_position:
            return ReconcileAction.NOOP

        # Position is gone — walk ACTIVE → EXITING → CLOSED (state machine
        # forbids ACTIVE → CLOSED direct). Resolve exit_reason by which child
        # order is no longer working: the missing one is the one that filled.
        exit_reason, exit_order_id = _resolve_exit_attribution(
            lifecycle,
            stop_order_open=stop_order_open,
            target_order_open=target_order_open,
        )

        lc = await self._sm.transition(
            lifecycle,
            State.EXITING,
            reason="recon_active_to_exiting",
            payload={"exit_reason_hint": exit_reason.value},
            exit_order_id=exit_order_id,
        )
        await self._close_from_exiting(
            lc,
            exit_reason,
            stop_order_open=stop_order_open,
            target_order_open=target_order_open,
        )
        self._log_action(lifecycle, ReconcileAction.ACTIVE_TO_CLOSED)
        return ReconcileAction.ACTIVE_TO_CLOSED

    async def _reconcile_exiting(
        self,
        lifecycle: Lifecycle,
        *,
        has_position: bool,
        exit_orders_open: bool,
        stop_order_open: bool,
        target_order_open: bool,
    ) -> ReconcileAction:
        if has_position and exit_orders_open:
            # Still flat-pending — leave alone.
            return ReconcileAction.NOOP

        if has_position and not exit_orders_open:
            LOGGER.warning(
                "[RECON] %s: exiting_with_position_no_orders — id=%s (manual cancel?)",
                lifecycle.symbol,
                lifecycle.lifecycle_id,
            )
            return ReconcileAction.WARNING

        # Position is gone — close out.
        exit_reason, _exit_order_id = _resolve_exit_attribution(
            lifecycle,
            stop_order_open=stop_order_open,
            target_order_open=target_order_open,
        )
        await self._close_from_exiting(
            lifecycle,
            exit_reason,
            stop_order_open=stop_order_open,
            target_order_open=target_order_open,
        )
        self._log_action(lifecycle, ReconcileAction.EXITING_TO_CLOSED)
        return ReconcileAction.EXITING_TO_CLOSED

    # -------------------------------------------------------------- close helpers

    async def _close_from_exiting(
        self,
        lifecycle: Lifecycle,
        exit_reason: ExitReason,
        *,
        stop_order_open: bool = False,
        target_order_open: bool = False,
    ) -> Lifecycle:
        """Apply EXITING → CLOSED with broker-best-effort exit fields + pnl.

        W-S15.1 — before transitioning, cancel any bracket leg still working at
        the broker. This is the orphan-prevention safety net for the
        reconciler-driven close path: when the event-driven router misses an exit
        fillEvent (network blip, IB Gateway restart, or a container recreate that
        delivered the fill to the prior process), the position goes flat but the
        opposite (un-OCA'd) GTC leg is left resting with no position behind it.
        Cancelling happens FIRST so a transient transition failure leaves the
        lifecycle non-CLOSED and the next scan retries the cancel.
        """
        await self._cancel_open_legs(
            lifecycle,
            stop_order_open=stop_order_open,
            target_order_open=target_order_open,
        )
        qty = lifecycle.entry_qty or 0
        entry_price = lifecycle.entry_price or 0.0
        exit_price = await self._resolve_exit_price(lifecycle, exit_reason)
        commission_total = float(qty) * MNQ.commission_rt_usd
        pnl_gross = compute_pnl_gross(
            direction=Direction(lifecycle.direction),
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
        )
        pnl_net = pnl_gross - commission_total
        return await self._sm.transition(
            lifecycle,
            State.CLOSED,
            reason=f"recon_close:{exit_reason.value}",
            payload={"exit_reason": exit_reason.value},
            exit_qty=qty,
            exit_price=exit_price,
            exit_filled_at=_now_iso(),
            exit_reason=exit_reason.value,
            commission_total=commission_total,
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
        )

    async def _resolve_exit_price(
        self,
        lifecycle: Lifecycle,
        exit_reason: ExitReason,
    ) -> float:
        """W-S15.3 — exit price for a reconciler-driven close.

        Prefer the ACTUAL broker fill price of the filled leg; fall back to the
        order price (:func:`_exit_price_for`) only when no matching fill is
        visible. Before this fix the reconciler recorded the STOP/TARGET *order*
        price as the exit (e.g. a stop filled 15pt past its trigger logged the
        trigger price, ~$60 off on 2ct), biasing the forward P&L scorecard. This
        is accounting only — entry/sizing/SL-TP distances are unchanged; it
        affects only ``exit_price`` / ``pnl_*`` recorded at close.

        The fill lookup is best-effort: ``IB.fills()`` is an in-session cache, so
        after a reconnect that post-dated the fill it's empty and we use the
        order price (the prior behaviour — no regression). Never raises.
        """
        filled_order_id = _filled_order_id_for(lifecycle, exit_reason)
        if filled_order_id:
            try:
                fills = await self._ib.get_fills()
                actual = _fill_price_for_order(fills, filled_order_id)
                if actual is not None:
                    LOGGER.info(
                        "[RECON] %s: exit_price from broker fill — id=%s order=%s "
                        "fill=%.2f order_px=%.2f reason=%s",
                        lifecycle.symbol,
                        lifecycle.lifecycle_id,
                        filled_order_id,
                        actual,
                        _exit_price_for(lifecycle, exit_reason),
                        exit_reason.value,
                    )
                    return actual
            except Exception as exc:  # noqa: BLE001 — fill lookup is best-effort
                LOGGER.warning(
                    "[RECON] %s: fill_lookup_error — order=%s type=%s msg=%s "
                    "(falling back to order price)",
                    lifecycle.symbol,
                    filled_order_id,
                    type(exc).__name__,
                    exc,
                )
        return _exit_price_for(lifecycle, exit_reason)

    async def _cancel_open_legs(
        self,
        lifecycle: Lifecycle,
        *,
        stop_order_open: bool,
        target_order_open: bool,
    ) -> None:
        """W-S15.1 — cancel any bracket leg still working after the position has
        gone flat broker-side. Only the legs reported still-open by ``openTrades``
        are cancelled (the filled leg is already gone). Cancels via our own IB
        client — the same clientId that placed the orders, so it's a same-client
        cancel (never IB error 10147). Idempotent: errors are logged, never raised.
        """
        for is_open, oid, leg, px in (
            (stop_order_open, lifecycle.stop_order_id, "STP", lifecycle.stop_price),
            (target_order_open, lifecycle.target_order_id, "LMT", lifecycle.target_price),
        ):
            if not is_open or oid is None:
                continue
            try:
                await self._ib.cancel_order(_order_ref(oid))
                LOGGER.info(
                    "[RECON] %s: cancelled orphan %s @%s — id=%s position flat broker-side",
                    lifecycle.symbol,
                    leg,
                    f"{px:.2f}" if px is not None else "?",
                    lifecycle.lifecycle_id,
                )
            except Exception as exc:  # noqa: BLE001 — cancel is idempotent; never raise
                LOGGER.warning(
                    "[RECON] %s: cancel_orphan_error — leg=%s order=%s type=%s msg=%s",
                    lifecycle.symbol,
                    leg,
                    oid,
                    type(exc).__name__,
                    exc,
                )

    # -------------------------------------------------------------- drains + scan

    async def drain_dirty(self) -> dict[ReconcileAction, int]:
        """Drain the dirty set, reconcile each id. Returns counts by action.

        IDs missing from the DB (e.g. CLOSED races) are silently dropped — the
        state machine's load_non_closed query already filters them out.
        """
        ids = self._dirty_set.drain()
        counts: Counter[ReconcileAction] = Counter()
        if not ids:
            LOGGER.info("[RECON] tick: drain_complete — dirty_count=0 actions={}")
            return dict(counts)

        non_closed = await self._sm.load_non_closed()
        by_id = {lc.lifecycle_id: lc for lc in non_closed}
        for lifecycle_id in ids:
            lc = by_id.get(lifecycle_id)
            if lc is None:
                continue
            action = await self.reconcile_one(lc)
            counts[action] += 1
        LOGGER.info(
            "[RECON] tick: drain_complete — dirty_count=%s actions=%s",
            len(ids),
            dict(counts),
        )
        return dict(counts)

    async def full_scan(self) -> dict[ReconcileAction, int]:
        """Reconcile every non-CLOSED lifecycle in the DB. Also detect foreign
        broker positions (no matching DB lifecycle by symbol) and fire the halt
        callback.
        """
        non_closed = await self._sm.load_non_closed()
        counts: Counter[ReconcileAction] = Counter()
        for lc in non_closed:
            action = await self.reconcile_one(lc)
            counts[action] += 1

        # Foreign-position detection runs over the canonical portfolio() snapshot
        # (§0.5.T3). A broker position whose symbol matches no non-CLOSED
        # lifecycle is a foreign trade — halt entries to prevent compounding.
        portfolio = await self._ib.get_portfolio()
        tracked_symbols = {lc.symbol for lc in non_closed}
        for item in portfolio:
            contract = getattr(item, "contract", None)
            if contract is None:
                continue
            local = getattr(contract, "localSymbol", None)
            base = getattr(contract, "symbol", None)
            qty = getattr(item, "position", 0)
            if not qty:
                continue
            symbol = local or base
            if symbol is None:
                continue
            if symbol in tracked_symbols or base in tracked_symbols:
                continue
            LOGGER.warning(
                "[RECON] foreign_position: symbol=%s qty=%s — halting new entries",
                symbol,
                qty,
            )
            self._orchestrator.raise_halt(symbol)
            counts[ReconcileAction.FOREIGN_POSITION] += 1

        LOGGER.info(
            "[RECON] tick: full_scan_complete — non_closed=%s actions=%s",
            len(non_closed),
            dict(counts),
        )
        return dict(counts)

    # ---------------------------------------------------------------- halt-ack
    # PR #12 — poll Supabase first (primary), file flag second (fallback when
    # the network is down or the table doesn't exist). Cheap when not halted:
    # one boolean check + early return.

    async def _poll_halt_ack(self) -> None:
        """If currently halted, check Supabase + file-flag for a fresh ack."""
        if not self._orchestrator.is_halted():
            return
        raised_at = self._orchestrator.halt_raised_at()
        if raised_at is None:
            return
        try:
            ack = await self._db.get_newest_halt_ack(since=raised_at)
        except Exception as exc:
            LOGGER.warning("[RECON] halt_ack_poll_failed: %r — falling back to file flag", exc)
            file_ack_ts = self._read_file_ack_mtime()
            if file_ack_ts is not None and file_ack_ts > raised_at:
                LOGGER.info(
                    "[RECON] halt_acked: source=file_flag mtime=%s",
                    file_ack_ts.isoformat(),
                )
                self._orchestrator.clear_halt(reason=f"file-flag mtime={file_ack_ts.isoformat()}")
            return
        if ack is not None:
            LOGGER.info(
                "[RECON] halt_acked: source=supabase acked_at=%s note=%s",
                ack["acked_at"],
                ack.get("note"),
            )
            self._orchestrator.clear_halt(
                reason=f"supabase ack acked_at={ack['acked_at']} note={ack.get('note')}"
            )

    def _read_file_ack_mtime(self) -> datetime | None:
        path = self._halt_ack_file_path
        if not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    # ------------------------------------------------------------------- run loop

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        """Loop until ``stop_event``: drain every ``dirty_drain_interval_sec``;
        full-scan every ``full_scan_interval_sec`` (counted in drain ticks).

        Errors inside drain/full_scan are caught + logged so a transient broker
        error doesn't kill the loop. The next tick retries.
        """
        ticks_per_full_scan = max(
            1,
            int(round(self._full_scan_interval_sec / self._dirty_drain_interval_sec)),
        )
        tick = 0
        LOGGER.info(
            "[RECON] task_launched — drain_sec=%.1f full_scan_sec=%.1f",
            self._dirty_drain_interval_sec,
            self._full_scan_interval_sec,
        )
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._dirty_drain_interval_sec,
                )
                return
            except TimeoutError:
                pass

            try:
                await self.drain_dirty()
            except Exception as exc:
                LOGGER.error(
                    "[RECON] tick: drain_error — type=%s msg=%s",
                    type(exc).__name__,
                    exc,
                )

            try:
                await self._poll_halt_ack()
            except Exception as exc:
                LOGGER.error(
                    "[RECON] tick: halt_ack_error — type=%s msg=%s",
                    type(exc).__name__,
                    exc,
                )

            tick += 1
            if tick % ticks_per_full_scan == 0:
                try:
                    await self.full_scan()
                except Exception as exc:
                    LOGGER.error(
                        "[RECON] tick: full_scan_error — type=%s msg=%s",
                        type(exc).__name__,
                        exc,
                    )

    # ----------------------------------------------------------------- log helper

    @staticmethod
    def _log_action(lifecycle: Lifecycle, action: ReconcileAction) -> None:
        LOGGER.info(
            "[RECON] %s: reconcile_action — id=%s action=%s",
            lifecycle.symbol,
            lifecycle.lifecycle_id,
            action.value,
        )


# ------------------------------------------------------------------ module helpers


def compute_pnl_gross(
    *, direction: Direction, entry_price: float, exit_price: float, qty: int
) -> float:
    """LONG: (exit - entry) * qty * multiplier; SHORT mirrors. MNQ multiplier = 2.0.

    Mirrors the router's private ``_pnl_gross`` — kept here so the reconciler
    doesn't import from a sibling module's private surface. PR #11 scope says
    router.py and reconciler.py are both in-scope, so duplicating the formula
    is acceptable; consolidation can land in a later cleanup.
    """
    delta = exit_price - entry_price if direction is Direction.LONG else entry_price - exit_price
    return delta * qty * MNQ.multiplier


class _OrderIdRef:
    """Minimal duck-type for ``IBClient.cancel_order``: only ``orderId`` is read.

    Mirrors ``router._OrderIdRef`` deliberately — the reconciler avoids importing
    a sibling module's private surface (same convention as the duplicated
    :func:`compute_pnl_gross`).
    """

    orderId: int = 0  # noqa: N815 — IB-API attribute name, must match ib_async


def _order_ref(order_id: int) -> Any:
    ref = _OrderIdRef()
    ref.orderId = order_id
    return ref


def _broker_qty_for(positions: list[Any], symbol: str) -> int | None:
    """Return the broker-reported position quantity for ``symbol`` (None if absent)."""
    for pos in positions:
        contract = getattr(pos, "contract", None)
        if contract is None:
            continue
        local = getattr(contract, "localSymbol", None)
        base = getattr(contract, "symbol", None)
        if symbol not in {local, base}:
            continue
        try:
            return int(getattr(pos, "position", 0))
        except (TypeError, ValueError):
            return None
    return None


def _broker_avg_cost_for(positions: list[Any], symbol: str) -> float:
    """Return avgCost for the matching position; 0.0 if missing or unparseable."""
    for pos in positions:
        contract = getattr(pos, "contract", None)
        if contract is None:
            continue
        local = getattr(contract, "localSymbol", None)
        base = getattr(contract, "symbol", None)
        if symbol not in {local, base}:
            continue
        try:
            return float(getattr(pos, "avgCost", 0.0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _broker_position_for(positions: list[Any], symbol: str) -> Any | None:
    """Return the broker Position object for ``symbol`` (None if absent).

    Unlike :func:`_broker_avg_cost_for` this hands back the whole position so the
    caller can read its qualified ``contract`` for order placement.
    """
    for pos in positions:
        contract = getattr(pos, "contract", None)
        if contract is None:
            continue
        local = getattr(contract, "localSymbol", None)
        base = getattr(contract, "symbol", None)
        if symbol in {local, base}:
            return pos
    return None


def _order_in_open_trades(open_trades: list[Any], order_id: int) -> bool:
    for trade in open_trades:
        order = getattr(trade, "order", None)
        if order is None:
            continue
        oid = getattr(order, "orderId", None)
        if oid == order_id:
            return True
    return False


def _resolve_exit_attribution(
    lifecycle: Lifecycle,
    *,
    stop_order_open: bool,
    target_order_open: bool,
) -> tuple[ExitReason, int]:
    """Pick exit_reason + exit_order_id for a position that's gone broker-side.

    Heuristic with only ``openTrades()`` visibility (no fills() probe):
    - stop missing + target still open → STOP filled
    - target missing + stop still open → TARGET filled
    - both missing (or both somehow open) → MANUAL (and exit_order_id is 0,
      documented in the table as the sentinel for "real one isn't known")
    """
    stop_id = lifecycle.stop_order_id
    target_id = lifecycle.target_order_id

    if stop_id is not None and not stop_order_open and target_order_open:
        return ExitReason.STOP, stop_id
    if target_id is not None and not target_order_open and stop_order_open:
        return ExitReason.TARGET, target_id
    if stop_id is not None and target_id is None and not stop_order_open:
        return ExitReason.STOP, stop_id
    if target_id is not None and stop_id is None and not target_order_open:
        return ExitReason.TARGET, target_id
    # Both gone (or neither populated): fall through to MANUAL with 0 sentinel.
    return ExitReason.MANUAL, 0


def _exit_price_for(lifecycle: Lifecycle, exit_reason: ExitReason) -> float:
    """Order-price fallback when the reconciler can't see the actual fill.

    W-S15.3 — used only when :func:`_fill_price_for_order` finds no matching fill
    (e.g. the fill predates the current IB session). This deliberately records
    the STOP/TARGET *order* price; the slippage-accurate path is the fill lookup
    in :meth:`Reconciler._resolve_exit_price`.
    """
    if exit_reason is ExitReason.STOP and lifecycle.stop_price is not None:
        return float(lifecycle.stop_price)
    if exit_reason is ExitReason.TARGET and lifecycle.target_price is not None:
        return float(lifecycle.target_price)
    return float(lifecycle.entry_price or 0.0)


def _filled_order_id_for(lifecycle: Lifecycle, exit_reason: ExitReason) -> int | None:
    """The order id whose fill closed the position, by attribution.

    STOP → the protective stop; TARGET → the TP limit; MANUAL/EOD → the separate
    market exit (``exit_order_id``; 0/None sentinel when the reconciler couldn't
    attribute it). Mirror of :func:`_exit_price_for`'s leg selection.
    """
    if exit_reason is ExitReason.STOP:
        return lifecycle.stop_order_id
    if exit_reason is ExitReason.TARGET:
        return lifecycle.target_order_id
    return lifecycle.exit_order_id


def _fill_price_for_order(fills: list[Any], order_id: int) -> float | None:
    """Quantity-weighted average execution price of ``order_id`` across ``fills``.

    Returns ``None`` when no execution matches (the caller then uses the order
    price). Quantity-weighting handles partial fills correctly — a single order
    can fill in several executions at different prices.
    """
    total_qty = 0.0
    total_notional = 0.0
    for fill in fills:
        execution = getattr(fill, "execution", None)
        if execution is None:
            continue
        if getattr(execution, "orderId", None) != order_id:
            continue
        shares = float(getattr(execution, "shares", 0.0) or 0.0)
        price = float(
            getattr(execution, "price", 0.0) or getattr(execution, "avgPrice", 0.0) or 0.0
        )
        if shares and price:
            total_qty += shares
            total_notional += shares * price
    if total_qty > 0:
        return total_notional / total_qty
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
