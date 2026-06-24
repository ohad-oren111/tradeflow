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
from config.risk_params import RISK
from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.execution.bracket import build_bracket, build_protective_stop
from src.execution.dirty_set import DirtySet
from src.execution.router import _build_market_exit as build_market_exit
from src.execution.router import (
    cancel_prior_protective_stops,
    ensure_protective_stop,
    should_heal_target,
    stop_leg_uses_oca,
)
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


class RatchetArmer(Protocol):
    """Subset of ``OrderRouter`` the reconciler needs to arm the bar-close ratchet
    on the force-fill path. Same loose-coupling rationale as ``HaltCoordinator``:
    a Protocol keeps the reconciler decoupled from the concrete router and lets
    tests pass a ``MagicMock``. ``register_recovered`` registers the ACTIVE
    in-memory copy + contract and seeds the high-water mark."""

    def register_recovered(self, lifecycle: Lifecycle, contract: Any) -> None: ...


DEFAULT_HALT_ACK_FILE = Path("/tmp/halt_clear")


class ReconcileAction(StrEnum):
    NOOP = "noop"
    ENTERING_TO_ACTIVE = "entering_to_active"
    ENTERING_TO_CLOSED = "entering_to_closed"
    ACTIVE_TO_CLOSED = "active_to_closed"  # via EXITING
    EXITING_TO_CLOSED = "exiting_to_closed"
    FOREIGN_POSITION = "foreign_position"
    FLATTENED = "flattened"  # STABILIZE-4: auto-flattened a confirmed-foreign position
    ORPHAN_SWEPT = (
        "orphan_swept"  # PR-2 §0.5.207: cancelled an untracked resting stop on a flat book
    )
    HEALED = "healed"  # ACTIVE+position: re-placed a missing bracket leg (stop/target)
    WARNING = "warning"  # logged anomaly, no transition
    RACE = "race"  # state machine guard fired (InvariantViolationError)


# OCA type for re-placed legs — matches the bracket's existing ocaType (CANCEL_WITH_BLOCK).
_HEAL_OCA_TYPE = 3


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
        router: RatchetArmer | None = None,
        dirty_drain_interval_sec: float = 30.0,
        full_scan_interval_sec: float = 300.0,
        halt_ack_file_path: Path = DEFAULT_HALT_ACK_FILE,
    ) -> None:
        self._ib = ib
        self._sm = sm
        self._dirty_set = dirty_set
        self._db = db
        self._orchestrator = orchestrator
        # GATE-ZERO — used to arm the bar-close ratchet on a force-filled entry
        # (the router only seeds its ratchet on the fillEvent it sees; see
        # _reconcile_entering). Optional so existing test construction is unchanged.
        self._router = router
        self._dirty_drain_interval_sec = dirty_drain_interval_sec
        self._full_scan_interval_sec = full_scan_interval_sec
        self._halt_ack_file_path = halt_ack_file_path
        # STABILIZE-4 — per-symbol foreign-position debounce: symbol → (foreign_qty,
        # consecutive_tick_count). A confirmed-foreign position is only auto-flattened
        # once the SAME foreign qty persists for `foreign_flatten_confirm_ticks` ticks.
        self._foreign_streak: dict[str, tuple[int, int]] = {}

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
                    positions=positions,
                    open_trades=open_trades,
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
            # PR #158 — size from THIS lifecycle's own fill, NEVER the account net
            # (``broker_qty``). IBKR reports one NET position per contract, so with
            # ≥2 concurrent same-symbol lifecycles the force-filling leg would claim
            # the whole book (the 2026-06-12 naked-orphan: net=4 written as one
            # lifecycle's entry_qty → oversold at EOD → SHORT 2). ``broker_qty`` stays
            # for presence/direction only (``has_position`` above).
            qty = await self._force_fill_qty(lifecycle, broker_qty=broker_qty)
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
            active = await self._sm.transition(
                lifecycle,
                State.ACTIVE,
                reason="recon_entering_to_active",
                payload={"broker_qty": broker_qty, "qty_matches": qty_matches},
                entry_qty=qty,
                entry_price=entry_price,
                entry_filled_at=_now_iso(),
                **extra,
            )
            # GATE-ZERO — arm the bar-close ratchet on the force-fill path.
            # The router seeds its high-water mark + registers the ACTIVE in-memory
            # copy ONLY in _handle_parent_fill (the fillEvent it sees). When the router
            # MISSES the parent fill and we force-fill the entry here, that seeding
            # never runs: the standalone STP (placed above, STABILIZE-5) is modifiable
            # but the ratchet stays UN-ARMED, so _active_trailing_lifecycle() never
            # selects the position and the stop never walks — it round-trips to base
            # even after a +50 run-up (lifecycle e912f9c2 rode +110.31 and exited at
            # base −$363). Adopt the now-ACTIVE position into the router's ratchet,
            # reusing the recovery primitive (registers the ACTIVE copy + contract +
            # seeds _highest to entry). Trailing-only: fixed mode never ratchets.
            if (
                self._router is not None
                and RISK.exit_mode == "trailing"
                and active is not None
                and getattr(active, "state", None) == State.ACTIVE.value
            ):
                position = _broker_position_for(positions, lifecycle.symbol)
                contract = getattr(position, "contract", None) if position is not None else None
                if contract is not None:
                    self._router.register_recovered(active, contract)
                    LOGGER.info(
                        "[RECON] %s: trailing_armed_on_force_fill — adopted into router "
                        "ratchet (highest seeded=entry=%.2f) lifecycle=%s",
                        lifecycle.symbol,
                        entry_price,
                        lifecycle.lifecycle_id,
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

    async def _force_fill_qty(self, lifecycle: Lifecycle, *, broker_qty: int | None) -> int:
        """Per-lifecycle fill size for the force-fill path (PR #158).

        The protective-stop size + recorded ``entry_qty`` for a reconciler
        force-fill MUST equal THIS lifecycle's own filled quantity, never the
        account NET (``broker_qty``). IBKR reports one NET position per contract,
        so with ≥2 concurrent same-symbol lifecycles the net spans every open
        lifecycle — sizing one leg off it claims the whole book (2026-06-12:
        net=4 stamped as a single lifecycle's entry_qty → oversold at EOD →
        naked SHORT 2). Same family as #141/#151, now on the force-fill leg.

        Source order:
          1. this entry order's summed executions (cumQty — robust to partials);
          2. fallback ``min(|net|, intended)`` — never attribute more than this
             lifecycle intended. ``entry_qty`` is None during ENTERING (it is
             stamped only on the fill), so the intended size is the configured
             per-trade qty.

        ``broker_qty`` is used here ONLY as the fallback cap + the audit log — it
        never sizes the stop directly.
        """
        net = abs(broker_qty) if broker_qty is not None else None
        # entry_qty is None during ENTERING (stamped only on the fill) → fall back to
        # the configured per-trade size as the intended cap.
        intended = (
            int(lifecycle.entry_qty) if lifecycle.entry_qty else int(RISK.contracts_per_trade)
        )
        order_qty: int | None = None
        if lifecycle.entry_order_id is not None:
            try:
                fills = await self._ib.get_fills()
                order_qty = _filled_qty_for_order(fills, lifecycle.entry_order_id)
            except Exception as exc:  # noqa: BLE001 — fill lookup is best-effort
                LOGGER.warning(
                    "[RECON] %s: force_fill_qty_lookup_error — id=%s type=%s msg=%s "
                    "(falling back to min(net,intended))",
                    lifecycle.symbol,
                    lifecycle.lifecycle_id,
                    type(exc).__name__,
                    exc,
                )
        if order_qty:
            qty, source = order_qty, "order_cumqty"
        elif net is not None:
            qty, source = min(net, intended), "min(net,intended)"
        else:
            qty, source = intended, "intended"
        LOGGER.info(
            "[RECON] %s: force_fill_qty — lifecycle=%s qty=%s source=%s "
            "(net=%s not used for sizing)",
            lifecycle.symbol,
            lifecycle.lifecycle_id,
            qty,
            source,
            net,
        )
        return qty

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
        sibling_stop_ids = await self._sibling_stop_ids(lifecycle.lifecycle_id)
        return await ensure_protective_stop(
            self._ib,
            contract,
            lifecycle,
            qty=qty,
            existing_stop_is_live=existing_stop_is_live,
            component="RECON",
            path="recon_force_fill",
            sibling_stop_ids=sibling_stop_ids,
        )

    async def _sibling_stop_ids(self, current_lifecycle_id: str) -> set[int] | None:
        """Stop order ids tracked by OTHER non-closed lifecycles — PROTECTED from the
        cancel-before-arm sweep so an N=2 same-symbol book never cancels the still-open
        leg's stop (PR-143). Returns ``None`` on a DB error so the caller cancels only
        the lifecycle's own tracked stop and never an order that might be a sibling's.
        """
        try:
            rows = await self._db.select_lifecycles_non_closed()
        except Exception as exc:  # noqa: BLE001 — never raise into the reconcile loop
            LOGGER.warning(
                "[RECON] sibling_stop_ids_unavailable — %s: %s (cancel own stop only)",
                type(exc).__name__,
                exc,
            )
            return None
        ids: set[int] = set()
        for row in rows:
            if row.get("lifecycle_id") == current_lifecycle_id:
                continue
            sid = row.get("stop_order_id")
            if sid is not None:
                ids.add(int(sid))
        return ids

    async def _heal_missing_legs(
        self,
        lifecycle: Lifecycle,
        *,
        positions: list[Any],
        open_trades: list[Any],
        stop_order_open: bool,
        target_order_open: bool,
    ) -> bool:
        """Re-place any missing bracket leg for an ACTIVE-with-position lifecycle so
        a redeploy-dropped GTC leg never leaves the position naked (§0.5.T5).

        Uses the RECORDED ``stop_price`` (= entry−75) / ``target_price`` (= entry+150),
        never live price. New leg(s) are OCA-linked to the surviving leg's group (or a
        fresh group if both are gone) so a fill on either cancels the sibling
        broker-side. Persists the new order id(s). Best-effort + never raises; returns
        whether any leg was re-placed.
        """
        position = _broker_position_for(positions, lifecycle.symbol)
        contract = getattr(position, "contract", None) if position is not None else None
        if contract is None:
            LOGGER.warning(
                "[RECOVER] %s: heal_skipped — no broker contract for id=%s",
                lifecycle.symbol,
                lifecycle.lifecycle_id,
            )
            return False
        # IB.positions() contracts omit exchange → Error 321 on placement.
        if not getattr(contract, "exchange", ""):
            contract.exchange = "CME"
        try:
            qty = abs(int(getattr(position, "position", 0))) or int(lifecycle.entry_qty or 0)
        except (TypeError, ValueError):
            qty = int(lifecycle.entry_qty or 0)
        if qty <= 0:
            LOGGER.warning(
                "[RECOVER] %s: heal_skipped — qty unknown for id=%s",
                lifecycle.symbol,
                lifecycle.lifecycle_id,
            )
            return False

        # OCA group: link new leg(s) to the surviving leg; fresh group if both gone.
        surviving_oca = None
        if target_order_open:
            surviving_oca = _oca_group_of(open_trades, lifecycle.target_order_id)
        elif stop_order_open:
            surviving_oca = _oca_group_of(open_trades, lifecycle.stop_order_id)
        oca_group = surviving_oca or f"heal-{lifecycle.lifecycle_id[:8]}"

        direction = Direction(lifecycle.direction)
        updates: dict[str, Any] = {}
        try:
            if not stop_order_open and lifecycle.stop_price is not None:
                # ``stop_price`` is the RATCHETED level in trailing mode (the router
                # persists it on every ratchet, STABILIZE-3), so the heal re-arms at
                # the true protective floor, NEVER the stale entry−75 base.
                stp = build_protective_stop(
                    direction=direction, qty=qty, stop_price=float(lifecycle.stop_price)
                )
                # STABILIZE-3 — match the entry leg's grouping: a trailing STP must be
                # UNGROUPED so the bar-close ratchet can modify it (an OCA-grouped stop
                # is rejected with Error 10326 and cancelled). Only fixed-mode stops
                # join an OCA group (paired with the LMT take-profit).
                heal_oca = stop_leg_uses_oca(RISK.exit_mode)
                if heal_oca:
                    stp.ocaGroup = oca_group
                    stp.ocaType = _HEAL_OCA_TYPE
                # Cancel-before-arm (PR-143): an untracked sibling stop (the 06-10
                # orphan) may rest for this position even though the tracked leg is
                # gone — cancel it before re-placing so the heal can't create a second
                # resting stop. N=2-safe via the protected sibling-id set.
                heal_sibling_ids = await self._sibling_stop_ids(lifecycle.lifecycle_id)
                await cancel_prior_protective_stops(
                    self._ib,
                    symbol=lifecycle.symbol,
                    exit_action="SELL" if direction is Direction.LONG else "BUY",
                    own_stop_id=lifecycle.stop_order_id,
                    open_trades=open_trades,
                    sibling_stop_ids=heal_sibling_ids,
                    component="RECOVER",
                    path="heal_missing_stop",
                )
                trade = await self._ib.place_order(contract, stp)
                updates["stop_order_id"] = _order_id_of(trade)
                LOGGER.warning(
                    "[RECOVER] %s: stop missing — re-placed STP @%.2f oca=%s id=%s",
                    lifecycle.lifecycle_id,
                    float(lifecycle.stop_price),
                    oca_group if heal_oca else "none(trailing)",
                    updates["stop_order_id"],
                )
            if not target_order_open and lifecycle.target_price is not None:
                # Exit-mode-aware (§0.5.196 / WO5): in trailing mode there is NO
                # resting TARGET to heal — the bar-close ratchet walks the STP, and
                # re-arming a +offset LMT would cap upside (the hybrid-bracket bug).
                # The missing STOP above is still healed in both modes (§0.5.T5).
                if not should_heal_target(RISK.exit_mode):
                    LOGGER.info(
                        "[RECONCILER] %s: target_heal_skipped — exit_mode=%s "
                        "(no resting TARGET in trailing; bar-ratchet walks the STP)",
                        lifecycle.symbol,
                        RISK.exit_mode,
                    )
                else:
                    _parent, tp = build_bracket(
                        direction=direction,
                        qty=qty,
                        entry_type="MKT",
                        entry_lmt_price=None,
                        target_price=float(lifecycle.target_price),
                    )
                    tp.parentId = 0  # standalone — not stitched to a (non-existent) parent
                    tp.ocaGroup = oca_group
                    tp.ocaType = _HEAL_OCA_TYPE
                    trade = await self._ib.place_order(contract, tp)
                    updates["target_order_id"] = _order_id_of(trade)
                    LOGGER.warning(
                        "[RECOVER] %s: target missing — re-placed LMT @%.2f oca=%s id=%s",
                        lifecycle.lifecycle_id,
                        float(lifecycle.target_price),
                        oca_group,
                        updates["target_order_id"],
                    )
        except Exception as exc:  # noqa: BLE001 — never raise into the reconcile loop
            LOGGER.error(
                "[RECOVER] %s: heal_failed — %s: %s (position may be unprotected)",
                lifecycle.lifecycle_id,
                type(exc).__name__,
                exc,
            )
            LOGGER.info("[ALERT] recover_heal_failed: id=%s", lifecycle.lifecycle_id)

        if updates:
            try:
                await self._db.update_lifecycle(lifecycle.lifecycle_id, updates)
            except Exception as exc:  # noqa: BLE001 — order placed; DB persist is best-effort
                LOGGER.warning(
                    "[RECOVER] %s: persist_failed — %s: %s (new oid not saved to DB)",
                    lifecycle.lifecycle_id,
                    type(exc).__name__,
                    exc,
                )
        return bool(updates)

    async def _reconcile_active(
        self,
        lifecycle: Lifecycle,
        *,
        has_position: bool,
        stop_order_open: bool,
        target_order_open: bool,
        positions: list[Any],
        open_trades: list[Any],
    ) -> ReconcileAction:
        if has_position:
            # Self-heal: a live position whose protective leg is gone (e.g. a GTC
            # stop that didn't survive a redeploy) is unprotected — re-place any
            # missing bracket leg. Idempotent: both legs resident → nothing to do.
            # Exit-mode-aware (§0.5.196): in trailing mode there is intentionally no
            # resting TARGET (the bar-ratchet walks the STP), so a "missing" target is
            # expected — treat it as healthy here so a healthy trailing position isn't
            # re-healed (and target_heal_skipped-logged) every scan.
            target_healthy = target_order_open or not should_heal_target(RISK.exit_mode)
            if not (stop_order_open and target_healthy):
                healed = await self._heal_missing_legs(
                    lifecycle,
                    positions=positions,
                    open_trades=open_trades,
                    stop_order_open=stop_order_open,
                    target_order_open=target_order_open,
                )
                if healed:
                    return ReconcileAction.HEALED
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

    async def _backfill_broker_pnl(self, *, limit: int = 50) -> int:
        """Q4b — patch IBKR broker-truth P&L onto recently-CLOSED lifecycles.

        ADDITIVE + best-effort: writes ``commission_broker`` (Σ round-trip
        commissionReport.commission over entry+exit fills) and
        ``realized_pnl_broker`` (Σ commissionReport.realizedPNL over the exit
        fills) for CLOSED lifecycles that don't have them yet. ``commissionReport``
        is delivered asynchronously *after* the fill (it is zero-filled inline), so
        capturing it here on the periodic scan — rather than at close time — is
        what makes it reliable. ``IB.fills()`` is an in-session cache, so rows
        whose fills predate a reconnect stay NULL (the dashboard then falls back to
        the computed estimate). NEVER touches pnl_net / the kill-switch input, and
        never raises into the scan loop.
        """
        try:
            rows = await self._db.select(
                "lifecycles",
                filters={
                    "state": "eq.CLOSED",
                    "realized_pnl_broker": "is.null",
                    "exit_order_id": "not.is.null",
                    "order": "updated_at.desc",
                    "limit": str(limit),
                },
                columns="lifecycle_id,entry_order_id,exit_order_id,pnl_net",
            )
        except Exception as exc:  # noqa: BLE001 — observability, never break the scan
            LOGGER.debug("[RECON] broker_pnl backfill: select skipped — %r", exc)
            return 0
        if not rows:
            return 0
        try:
            fills = await self._ib.get_fills()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("[RECON] broker_pnl backfill: get_fills skipped — %r", exc)
            return 0
        patched = 0
        for row in rows:
            exit_id = row.get("exit_order_id")
            if exit_id is None:
                continue
            entry_id = row.get("entry_order_id")
            commission_ids = {int(x) for x in (entry_id, exit_id) if x is not None}
            commission, realized = _broker_pnl_for_orders(fills, commission_ids, {int(exit_id)})
            if commission is None and realized is None:
                continue  # commissionReport not arrived / fill not in session cache
            updates: dict[str, Any] = {}
            if commission is not None:
                updates["commission_broker"] = round(commission, 4)
            if realized is not None:
                updates["realized_pnl_broker"] = round(realized, 4)
            try:
                await self._db.update_lifecycle(str(row["lifecycle_id"]), updates)
                patched += 1
                LOGGER.info(
                    "[RECON] broker_pnl backfilled — id=%s commission=%s realized=%s",
                    row.get("lifecycle_id"),
                    updates.get("commission_broker"),
                    updates.get("realized_pnl_broker"),
                )
                # Q-E watcher (3): on the FIRST real round-trip that backfills a broker
                # realized P&L, auto-compare it to the fixed-rate estimate and log the
                # realizedPNL-semantics verdict — discharges the #179 live-verification
                # without anyone polling. Log-only; never affects pnl_net / kill-switch.
                if "realized_pnl_broker" in updates:
                    verdict, detail = broker_pnl_semantics_verdict(
                        updates["realized_pnl_broker"], row.get("pnl_net")
                    )
                    LOGGER.info(
                        "[WATCH] broker_pnl_semantics: id=%s realized_broker=%s pnl_net_est=%s "
                        "%s — verdict=%s",
                        row.get("lifecycle_id"),
                        updates["realized_pnl_broker"],
                        row.get("pnl_net"),
                        detail,
                        verdict,
                    )
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug(
                    "[RECON] broker_pnl patch skipped — id=%s %r", row.get("lifecycle_id"), exc
                )
        return patched

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

        STABILIZE-5 NEVER-ORPHAN — this is the reconciler half of the cancel that the
        OCA group used to do broker-side. A trailing-mode protective STP is now a
        STANDALONE order (parentId=0, ungrouped — so the bar-ratchet can modify it),
        which means a fill on the *position-closing* path no longer auto-cancels it.
        When a tracked position goes flat by any path other than the STP itself (a
        TARGET/manual/opposite/EOD fill, or a fill the router missed), the STP is left
        resting with no position behind it — a SELL STP that could re-open a short if
        hit. This is reached for the standalone STP via the flat-position close path
        (``_reconcile_active`` / ``_reconcile_exiting`` → ``_close_from_exiting``): the
        leg is cancelled here because ``stop_order_open`` is broker-truth (driven by
        ``openTrades``), not by exit attribution. The router's ``_cancel_sibling_legs``
        is the event-driven half (cancels on the exit fill the router DOES see).
        """
        for is_open, oid, leg, px in (
            (stop_order_open, lifecycle.stop_order_id, "STP", lifecycle.stop_price),
            (target_order_open, lifecycle.target_order_id, "LMT", lifecycle.target_price),
        ):
            if not is_open or oid is None:
                continue
            try:
                cancelled = await self._ib.cancel_order_by_id(oid)
            except Exception as exc:  # noqa: BLE001 — never raise into the reconcile loop
                LOGGER.warning(
                    "[RECON] %s: cancel_orphan_error — leg=%s order=%s type=%s msg=%s",
                    lifecycle.symbol,
                    leg,
                    oid,
                    type(exc).__name__,
                    exc,
                )
                continue
            if cancelled:
                LOGGER.info(
                    "[RECON] %s: cancelled orphan %s order id=%s @%s — position flat broker-side",
                    lifecycle.symbol,
                    leg,
                    oid,
                    f"{px:.2f}" if px is not None else "?",
                )
            else:
                LOGGER.info(
                    "[RECON] %s: cancel skipped %s id=%s — not live (already gone)",
                    lifecycle.symbol,
                    leg,
                    oid,
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

        # Foreign-position detection + auto-flatten guard (STABILIZE-4) over the
        # canonical portfolio() snapshot (§0.5.T3). Intent-based + direction-agnostic.
        portfolio = await self._ib.get_portfolio()
        foreign_counts = await self._reconcile_foreign_positions(non_closed, portfolio)
        counts.update(foreign_counts)

        # NEVER-ORPHAN broker-truth backstop (PR-2 / §0.5.207): cancel any resting
        # protective order on a symbol that is BOTH broker-flat AND un-intended by
        # any non-CLOSED lifecycle. _cancel_open_legs only cancels the ONE tracked
        # stop_order_id, so an untracked sibling stop (the 2026-06-10 orphan) survives;
        # this sweeps it before it can fire into a naked position.
        positions = await self._ib.get_positions()
        open_trades = await self._ib.get_open_trades()
        swept = await self._sweep_orphan_protective_orders(non_closed, open_trades, positions)
        if swept:
            counts[ReconcileAction.ORPHAN_SWEPT] += swept

        # Q4b — additive broker-truth P&L backfill (best-effort, off the order
        # path). commissionReport arrives async after the fill, so we patch the
        # broker columns onto recently-CLOSED lifecycles here, where the fill
        # cache has had time to populate. Never touches pnl_net / the kill switch.
        backfilled = await self._backfill_broker_pnl()

        LOGGER.info(
            "[RECON] tick: full_scan_complete — non_closed=%s actions=%s broker_pnl_backfilled=%s",
            len(non_closed),
            dict(counts),
            backfilled,
        )
        # Self-heal summary (proves the missing-leg recovery path ran this scan).
        active = sum(1 for lc in non_closed if lc.state == State.ACTIVE.value)
        LOGGER.info(
            "[RECOVER] scan: %d active, %d healed",
            active,
            counts.get(ReconcileAction.HEALED, 0),
        )
        return dict(counts)

    async def _reconcile_foreign_positions(
        self, non_closed: list[Lifecycle], portfolio: list[Any]
    ) -> Counter[ReconcileAction]:
        """STABILIZE-4 — reconcile every broker position against tracked INTENT and
        auto-flatten the unaccounted-for delta once it is PERSISTENTLY foreign.

        Direction-agnostic by design: "foreign" = broker exposure not backed by a
        non-CLOSED lifecycle (no lifecycle, wrong side, or oversized). An INTENTIONAL
        short carries its own SHORT lifecycle → reconciles to intent → never foreign,
        so enabling shorts later needs ZERO change here. CLOSE-only: the guard never
        opens/increases a position (an unfilled entry, where broker < intent, is the
        entry path's job — :func:`foreign_quantity` returns 0 there).

        Safety: a confirmed-foreign position is flattened only after the SAME foreign
        qty persists for ``foreign_flatten_confirm_ticks`` consecutive full scans
        (debounce) AND is NOT explained by an in-flight ENTERING/EXITING lifecycle
        for that symbol. When uncertain (in-flight transition) → no halt, no flatten;
        wait. Halt fires immediately on a stable foreign position; the auto-flatten
        is gated behind the debounce + the ``foreign_flatten_enabled`` knob.
        """
        counts: Counter[ReconcileAction] = Counter()
        default_qty = int(RISK.contracts_per_trade)

        # Net broker position per symbol (sum across items) + a contract for placement.
        broker_net: dict[str, int] = {}
        broker_contract: dict[str, Any] = {}
        for item in portfolio:
            contract = getattr(item, "contract", None)
            if contract is None:
                continue
            qty = getattr(item, "position", 0)
            if not qty:
                continue
            symbol = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None)
            if symbol is None:
                continue
            try:
                signed = int(qty)
            except (TypeError, ValueError):
                continue
            broker_net[symbol] = broker_net.get(symbol, 0) + signed
            broker_contract.setdefault(symbol, contract)

        for symbol, b_net in broker_net.items():
            i_net = intended_net_position(non_closed, symbol, default_qty)
            fq = foreign_quantity(b_net, i_net)
            if fq == 0:
                # Fully reconciled to intent → clear any pending debounce.
                self._foreign_streak.pop(symbol, None)
                continue

            # In-flight transition (entry filling / exit closing) → the net is changing
            # as designed. Do NOT halt or flatten; wait for it to settle (§ race guard).
            if _symbol_in_flight(non_closed, symbol):
                LOGGER.info(
                    "[RECON] foreign_skip_in_flight: symbol=%s broker_net=%s intended_net=%s "
                    "foreign_qty=%s (ENTERING/EXITING lifecycle present — not flattening)",
                    symbol,
                    b_net,
                    i_net,
                    fq,
                )
                self._foreign_streak.pop(symbol, None)
                continue

            # PR-3 entry-settle grace — a just-filled multi-lot entry can settle across
            # several partial executions, so broker_net may momentarily exceed the
            # recorded entry_qty (the 2026-06-10 trap: a 2-lot fill counted as 1 →
            # the second real contract read as "foreign"). If the newest non-CLOSED
            # lifecycle for this symbol filled within the settle grace, wait — never
            # flatten a contract belonging to an actively-settling TF entry (§0.5.207).
            settle_sec = float(getattr(RISK, "foreign_flatten_entry_settle_sec", 0.0) or 0.0)
            if settle_sec > 0 and _symbol_recently_filled(non_closed, symbol, settle_sec):
                LOGGER.info(
                    "[RECON] foreign_skip_entry_settle: symbol=%s broker_net=%s intended_net=%s "
                    "foreign_qty=%s (entry filled < %.0fs ago — letting it settle)",
                    symbol,
                    b_net,
                    i_net,
                    fq,
                    settle_sec,
                )
                self._foreign_streak.pop(symbol, None)
                continue

            # Stable foreign exposure → halt immediately (compounding guard, as before).
            counts[ReconcileAction.FOREIGN_POSITION] += 1
            self._orchestrator.raise_halt(symbol)

            prev = self._foreign_streak.get(symbol)
            streak = prev[1] + 1 if prev is not None and prev[0] == fq else 1
            self._foreign_streak[symbol] = (fq, streak)
            LOGGER.warning(
                "[RECON] foreign_position: symbol=%s broker_net=%s intended_net=%s "
                "foreign_qty=%s streak=%s/%s — halting new entries",
                symbol,
                b_net,
                i_net,
                fq,
                streak,
                RISK.foreign_flatten_confirm_ticks,
            )
            LOGGER.info(
                "[ALERT] foreign_position_detected: symbol=%s broker=%s intended=%s "
                "foreign=%s streak=%s/%s",
                symbol,
                b_net,
                i_net,
                fq,
                streak,
                RISK.foreign_flatten_confirm_ticks,
            )

            if not RISK.foreign_flatten_enabled:
                continue  # auto-liquidation disabled → halt + alert only
            if streak < RISK.foreign_flatten_confirm_ticks:
                continue  # not yet confirmed across the debounce window → wait

            # CONFIRMED-foreign across the debounce → flatten the unaccounted delta.
            flattened = await self._flatten_foreign(symbol, fq, broker_contract.get(symbol))
            if flattened:
                counts[ReconcileAction.FLATTENED] += 1
            # Re-evaluate from broker truth next scan (the market exit fills ~instantly).
            self._foreign_streak.pop(symbol, None)

        return counts

    async def _sweep_orphan_protective_orders(
        self,
        non_closed: list[Lifecycle],
        open_trades: list[Any],
        positions: list[Any],
    ) -> int:
        """NEVER-ORPHAN broker-truth backstop (PR-2 / §0.5.207).

        A resting protective order (STP / STP LMT / LMT) on a symbol that is BOTH
        broker-FLAT and un-intended by any non-CLOSED lifecycle has nothing behind
        it — a SELL STP that re-opens a short if hit. The 2026-06-10 orphan: a
        position carried TWO protective stops (one armed by the router parent-fill,
        one by a reconciler re-arm) but the lifecycle tracks a SINGLE stop_order_id,
        so the un-tracked sibling was never cancelled — it sat resting 36 min, fired
        @28988.5, and opened a naked −1 SHORT the bot then had to flatten. The
        event-driven ``_cancel_sibling_legs`` and the reconciler ``_cancel_open_legs``
        both cancel only ``lifecycle.stop_order_id`` (+ target), so they CANNOT reach
        an untracked sibling. This sweep does, from broker truth.

        Concurrency-safe (N=2): gated on the symbol being broker-flat AND
        ``intended_net_position == 0``, so it never cancels a stop backing a live
        position (incl. the still-open leg of an N=2 book) or a working entry on an
        ENTERING lifecycle (which intends a position). Only orders CLAIMED by no
        non-CLOSED lifecycle are swept; entries are MKT, so the STP/STP LMT/LMT
        filter never touches a working entry. Cancel via our own clientId (no IB
        error 10147). Never raises into the scan loop.
        """
        claimed: set[int] = set()
        for lc in non_closed:
            for oid in (lc.entry_order_id, lc.stop_order_id, lc.target_order_id):
                if oid is not None:
                    claimed.add(int(oid))

        broker_net: dict[str, int] = {}
        for pos in positions:
            contract = getattr(pos, "contract", None)
            sym = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None)
            if sym is None:
                continue
            broker_net[sym] = broker_net.get(sym, 0) + int(getattr(pos, "position", 0) or 0)

        default_qty = int(RISK.contracts_per_trade)
        swept = 0
        for trade in open_trades:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            if order is None or contract is None:
                continue
            order_type = str(getattr(order, "orderType", "") or "")
            if order_type not in ("STP", "STP LMT", "LMT"):
                continue  # protective/exit legs only — entries are MKT (race-safe)
            oid = getattr(order, "orderId", None)
            sym = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None)
            if oid is None or sym is None or int(oid) in claimed:
                continue
            if broker_net.get(sym, 0) != 0:
                continue  # a live position may back this leg — leave it
            if intended_net_position(non_closed, sym, default_qty) != 0:
                continue  # a non-CLOSED lifecycle intends a position — leave it
            try:
                cancelled = await self._ib.cancel_order_by_id(int(oid))
            except Exception as exc:  # noqa: BLE001 — never raise into the reconcile loop
                LOGGER.warning(
                    "[RECON] orphan_sweep_error — order=%s sym=%s type=%s msg=%s",
                    oid,
                    sym,
                    type(exc).__name__,
                    exc,
                )
                continue
            if cancelled:
                swept += 1
                LOGGER.warning(
                    "[RECON] orphan_sweep: cancelled untracked resting %s order=%s sym=%s — "
                    "flat book, no non-CLOSED lifecycle claims it (NEVER-ORPHAN §0.5.207)",
                    order_type,
                    oid,
                    sym,
                )
        return swept

    async def _flatten_foreign(self, symbol: str, foreign_qty: int, contract: Any) -> bool:
        """Flatten a CONFIRMED-foreign delta at market via the shared market-exit
        builder (the same primitive EOD / manual-close use — never a hand-rolled raw
        order). ``foreign_qty`` > 0 → broker too long → SELL; < 0 → too short → BUY.
        Alerts BEFORE and AFTER. Never raises into the scan loop."""
        if contract is None:
            LOGGER.error("[RECON] foreign_flatten_skipped: symbol=%s — no broker contract", symbol)
            LOGGER.info("[ALERT] foreign_flatten_failed: symbol=%s reason=no_contract", symbol)
            return False
        if not getattr(contract, "exchange", ""):
            contract.exchange = "CME"  # §0.5.192 — position-derived contracts omit exchange
        qty = abs(int(foreign_qty))
        close_dir = Direction.LONG if foreign_qty > 0 else Direction.SHORT
        order = build_market_exit(close_dir, qty)
        LOGGER.warning(
            "[ALERT] foreign_flatten_initiated: symbol=%s foreign_qty=%s action=%s qty=%s",
            symbol,
            foreign_qty,
            order.action,
            qty,
        )
        try:
            trade = await self._ib.place_order(contract, order)
        except Exception as exc:  # noqa: BLE001 — never raise into the reconcile loop
            LOGGER.error(
                "[RECON] foreign_flatten_failed: symbol=%s type=%s msg=%s",
                symbol,
                type(exc).__name__,
                exc,
            )
            LOGGER.info("[ALERT] foreign_flatten_failed: symbol=%s msg=%s", symbol, exc)
            return False
        order_id = _order_id_of(trade)
        LOGGER.warning(
            "[ALERT] foreign_flatten_placed: symbol=%s action=%s qty=%s order=%s",
            symbol,
            order.action,
            qty,
            order_id,
        )
        return True

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


def _signed_intended_qty(lifecycle: Lifecycle, default_qty: int) -> int:
    """Signed position a non-CLOSED lifecycle INTENDS to hold (+long / -short).

    IDLE → 0 (no fill intended yet). ENTERING / ACTIVE / EXITING → ±intended size.
    Uses ``entry_qty`` when known, else ``default_qty`` (entry_qty is only stamped on
    the fill, so an ENTERING leg falls back to the configured per-trade size).
    Direction-agnostic — reads ``lifecycle.direction``, never assumes long.
    """
    if lifecycle.state not in (
        State.ENTERING.value,
        State.ACTIVE.value,
        State.EXITING.value,
    ):
        return 0
    qty = int(lifecycle.entry_qty) if lifecycle.entry_qty else int(default_qty)
    sign = 1 if Direction(lifecycle.direction) is Direction.LONG else -1
    return sign * qty


def intended_net_position(lifecycles: list[Lifecycle], symbol: str, default_qty: int) -> int:
    """Net position implied by tracked intent for ``symbol`` (sum of signed lifecycle
    intents). Direction-agnostic; an intentional short contributes a negative qty."""
    return sum(_signed_intended_qty(lc, default_qty) for lc in lifecycles if lc.symbol == symbol)


def foreign_quantity(broker_net: int, intended_net: int) -> int:
    """Signed quantity NOT backed by intent — the delta to flatten toward intent.

    Direction-agnostic and CLOSE-ONLY: never returns a value that would OPEN or
    increase exposure, so a position SHORT OF intent (an unfilled / partially filled
    entry) yields 0 — re-establishing intent is the entry path's job, not the guard's.

    Examples (long & short symmetric)::

        broker +4, intended +2 → +2   (sell 2: oversold/oversized excess)
        broker -2, intended  0 → -2   (buy 2: untracked short, the STABILIZE-3 artifact)
        broker +2, intended +2 →  0   (matches intent)
        broker  0, intended +2 →  0   (entry not filled yet — never opens)
        broker -2, intended +2 → -2   (buy 2: close the contrary short; guard won't re-long)
        broker -4, intended -2 → -2   (buy 2: reduce excess short — FUTURE shorts)
    """
    if broker_net == 0:
        return 0
    same_direction = intended_net != 0 and (broker_net > 0) == (intended_net > 0)
    if not same_direction:
        return broker_net  # no intent in broker's direction → the whole position is foreign
    if abs(broker_net) <= abs(intended_net):
        return 0  # within intent (possibly still filling) → nothing to flatten
    return broker_net - intended_net  # same sign → only the excess beyond intent


def _symbol_in_flight(lifecycles: list[Lifecycle], symbol: str) -> bool:
    """True if any lifecycle for ``symbol`` is mid-transition (ENTERING/EXITING) — its
    broker net is changing by design, so the guard must wait rather than flatten."""
    return any(
        lc.symbol == symbol and lc.state in (State.ENTERING.value, State.EXITING.value)
        for lc in lifecycles
    )


def _symbol_recently_filled(lifecycles: list[Lifecycle], symbol: str, settle_sec: float) -> bool:
    """True if any non-CLOSED lifecycle for ``symbol`` filled within ``settle_sec``
    seconds (PR-3 entry-settle grace — a multi-lot entry may still be settling its
    partial executions). Unparseable/absent ``entry_filled_at`` is ignored (treated
    as not-recent — the guard's other layers still apply)."""
    now = datetime.now(UTC)
    for lc in lifecycles:
        if lc.symbol != symbol or not lc.entry_filled_at:
            continue
        try:
            filled = datetime.fromisoformat(str(lc.entry_filled_at))
        except (TypeError, ValueError):
            continue
        if filled.tzinfo is None:
            filled = filled.replace(tzinfo=UTC)
        if (now - filled).total_seconds() <= settle_sec:
            return True
    return False


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


def _oca_group_of(open_trades: list[Any], order_id: int | None) -> str | None:
    """The ocaGroup of the resting order ``order_id`` (None if absent/ungrouped)."""
    if order_id is None:
        return None
    for trade in open_trades:
        order = getattr(trade, "order", None)
        if order is not None and getattr(order, "orderId", None) == order_id:
            return getattr(order, "ocaGroup", None) or None
    return None


def _order_id_of(trade: Any) -> int:
    order = getattr(trade, "order", None)
    if order is None:
        return 0
    try:
        return int(getattr(order, "orderId", 0) or 0)
    except (TypeError, ValueError):
        return 0


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


def _broker_pnl_for_orders(
    fills: list[Any],
    commission_order_ids: set[int],
    realized_order_ids: set[int],
) -> tuple[float | None, float | None]:
    """Q4b — sum IBKR ``commissionReport`` over matching fills (pure, testable).

    ``commission`` is summed over fills whose ``execution.orderId`` is in
    ``commission_order_ids`` (entry+exit → true round-trip commission, reported as
    a positive cost); ``realizedPNL`` over ``realized_order_ids`` (the exit/closing
    leg). Returns ``(None, None)`` for whichever number the broker has not yet
    reported — ``commissionReport`` is zero-filled until it arrives asynchronously,
    so a zero is treated as "not yet populated" (a genuine break-even round-trip
    therefore stays NULL and falls back to the computed estimate — acceptable).
    """
    commission = 0.0
    realized = 0.0
    saw_commission = False
    saw_realized = False
    for fill in fills:
        execution = getattr(fill, "execution", None)
        report = getattr(fill, "commissionReport", None)
        if execution is None or report is None:
            continue
        oid = getattr(execution, "orderId", None)
        if oid in commission_order_ids:
            c = getattr(report, "commission", None)
            if c:  # non-zero ⇒ broker has reported this fill's commission
                commission += abs(float(c))
                saw_commission = True
        if oid in realized_order_ids:
            r = getattr(report, "realizedPNL", None)
            if r is not None and float(r) != 0.0:
                realized += float(r)
                saw_realized = True
    return (commission if saw_commission else None, realized if saw_realized else None)


def broker_pnl_semantics_verdict(
    realized_broker: float | None, pnl_net_est: float | None
) -> tuple[str, str]:
    """Q-E watcher (3): classify IBKR realizedPNL vs the fixed-rate pnl_net estimate.

    Returns ``(verdict, detail)``. The verdict discharges the #179 owed live-
    verification on the next real round-trip: if the broker's realized P&L tracks the
    estimate closely, the fixed-commission model is faithful; a consistent gap tells
    the operator which way realizedPNL's semantics diverge (e.g. it's gross vs net of
    commission, or carries a fee the estimate omits). Pure — no IO.
    """
    if realized_broker is None or pnl_net_est is None:
        return "INDETERMINATE", "missing one side"
    rb = float(realized_broker)
    est = float(pnl_net_est)
    abs_delta = rb - est
    denom = max(abs(rb), abs(est), 1.0)
    rel = abs_delta / denom
    detail = f"abs_delta={abs_delta:+.2f} rel={rel*100:+.1f}%"
    if abs(abs_delta) <= 2.0 or abs(rel) <= 0.02:
        return "ESTIMATE-FAITHFUL", detail  # within ~1 commission tick / 2%
    if rb > est:
        return "BROKER-HIGHER", detail  # estimate is conservative (over-charges cost)
    return "BROKER-LOWER", detail  # estimate too rosy — realized pays more cost/slippage


def _filled_qty_for_order(fills: list[Any], order_id: int) -> int | None:
    """Total filled quantity (summed execution shares) for ``order_id`` (PR #158).

    This single entry order's cumQty — robust to a multi-lot entry that fills as
    several partial executions. Returns ``None`` when no execution matches (the
    caller then uses its ``min(net, intended)`` fallback). Mirror of
    :func:`_fill_price_for_order`'s per-order execution scan.
    """
    total = 0.0
    for fill in fills:
        execution = getattr(fill, "execution", None)
        if execution is None:
            continue
        if getattr(execution, "orderId", None) != order_id:
            continue
        shares = float(getattr(execution, "shares", 0.0) or 0.0)
        if shares:
            total += shares
    if total > 0:
        return int(round(total))
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
