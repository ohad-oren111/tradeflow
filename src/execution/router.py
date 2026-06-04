"""Order placement and fill routing for TradeFlow.

Glues together :class:`Sma100BounceStrategy`, the :class:`StateMachine`, and
:class:`IBClient`. Designed to be tested at the IBClient/StateMachine boundary
— no live IB. ib_async's ``execDetailsEvent`` is registered by the orchestrator
and routed through :meth:`OrderRouter.on_fill`.

Per CLAUDE.md §0.5.T2 (option γ): the bracket pair is parent + TP child only.
The protective STP is placed inside the parent fillEvent handler so §0.5.T5
("never leave a futures position without a GTC stop on IBKR") holds end-to-end.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from ib_async import Contract, Order, Trade

from config.instruments import MNQ
from config.risk_params import RISK
from src.clients.ib_client import IBClient
from src.execution.bracket import (
    build_entry_oca_bracket,
    build_protective_stop,
)
from src.execution.dirty_set import DirtySet
from src.execution.trail_manager import compute_ratcheted_stop, should_hard_exit
from src.state_machine import (
    Direction,
    ExitReason,
    InvariantViolationError,
    Lifecycle,
    State,
    StateMachine,
)
from src.strategy import Signal

LOGGER = logging.getLogger(__name__)

# Order statuses that mean a freshly-placed order is live/resting at the broker
# (safe to hand the protective floor off to) vs. dead (rejected — e.g. Error 328).
_RESTING_STATUSES = frozenset({"PreSubmitted", "Submitted", "Filled"})
_DEAD_STATUSES = frozenset({"Cancelled", "ApiCancelled", "Inactive"})


def should_heal_target(exit_mode: str) -> bool:
    """Whether a MISSING take-profit (LMT TARGET) leg should be re-armed, by exit_mode.

    Pure helper (no IB calls, no state) so the reconciler's leg-heal decision is
    unit-testable with an explicit arg — mirrors the entry leg-shape decision that
    already lives in :func:`src.execution.bracket.build_entry_oca_bracket`.

    - ``"fixed"`` → True: the resting LMT IS the take-profit; if a redeploy dropped
      it the reconciler must re-place it (§0.5.T5 leg self-heal).
    - ``"trailing"`` → False: there is intentionally NO resting TARGET (the bar-close
      ratchet walks the STP). Re-arming a +offset LMT here would cap upside — exactly
      the §0.5.196 hybrid bracket this guards against. A missing STOP is still healed.
    """
    return exit_mode != "trailing"


def stop_leg_uses_oca(exit_mode: str) -> bool:
    """Whether the protective STP leg should carry an OCA group, by exit_mode.

    Pure helper (no IB calls, no state) shared by the entry-bracket builder and the
    reconciler's leg-heal so both place the STP with the SAME grouping.

    - ``"fixed"`` → True: the STP and the LMT take-profit form an OCA pair so a fill
      on one cancels the sibling broker-side.
    - ``"trailing"`` → False: the STP is the SOLE exit leg and the bar-close ratchet
      modifies it in place. IBKR rejects a modify of an OCA-grouped order with Error
      10326 and CANCELS it (STABILIZE-3), so a trailing STP must be ungrouped or the
      ratchet silently drops the protective stop. A single-member OCA group buys
      nothing here.
    """
    return exit_mode != "trailing"


def _oca_group_for(lifecycle_id: str) -> str:
    """Deterministic OCA group for a lifecycle's exit legs.

    Computed identically at entry (``place_entry``) and at the post-fill trailing
    handoff (``_handle_parent_fill``) so the standalone TRAIL can OCA-join the
    entry bracket's fixed STP without threading the group string through state.
    """
    return f"tf-exit-{lifecycle_id[:8]}"


async def _confirm_order_resting(
    trade: Trade,
    *,
    timeout_sec: float = 3.0,
    poll_sec: float = 0.25,
) -> bool:
    """Poll a freshly-placed order's status until it is resting or dead.

    Returns True once the broker reports a resting status (PreSubmitted/Submitted/
    Filled), False if it reaches a dead status (Cancelled/Inactive — e.g. an
    Error 328 rejection) or the bounded wait elapses without confirmation. The
    status check runs BEFORE any sleep, so a mock/already-resting Trade returns
    immediately (unit tests do not block).
    """
    waited = 0.0
    while True:
        status = getattr(getattr(trade, "orderStatus", None), "status", "") or ""
        if status in _RESTING_STATUSES:
            return True
        if status in _DEAD_STATUSES:
            return False
        if waited >= timeout_sec:
            return False
        await asyncio.sleep(poll_sec)
        waited += poll_sec


async def _confirm_stop_at_aux(
    ib: IBClient,
    order_id: int,
    expected_aux: float,
    *,
    timeout_sec: float = 2.0,
    poll_sec: float = 0.25,
    tol: float = 0.01,
) -> bool:
    """Confirm from BROKER TRUTH that the resting order ``order_id`` actually holds
    ``expected_aux`` (§0.5.98).

    STABILIZE-3 — a ratchet modifies an order that was ALREADY resting, so the
    Trade's status is "resting" the instant ``place_order`` returns, before any
    async rejection lands (the Error 10326 OCA-revision cancel arrived ~3 ms later
    and fooled the status-only :func:`_confirm_order_resting`). This re-reads the
    live order from ``IB.openTrades()`` and only returns True once its ``auxPrice``
    equals the requested level — so a modify that was silently cancelled (order
    gone) or never applied (stale aux) is caught instead of announced. Returns False
    on timeout / missing order / mismatch. Never raises."""
    waited = 0.0
    while True:
        order = await ib.find_open_order_by_id(order_id)
        aux = getattr(order, "auxPrice", None) if order is not None else None
        if aux is not None and abs(float(aux) - float(expected_aux)) <= tol:
            return True
        if waited >= timeout_sec:
            return False
        await asyncio.sleep(poll_sec)
        waited += poll_sec


@dataclass
class CloseResult:
    """Outcome of a single :meth:`OrderRouter.close_position` call.

    For ``status="exit_submitted"`` the fill is still in flight; ``pnl`` and
    ``fill_price`` arrive later via the existing ``[ALERT] exit_filled`` line
    from :meth:`OrderRouter._handle_exit_fill`. Pre-active synthesised closes
    return ``pnl=0.0`` because no broker fill occurs.
    """

    closed: bool
    symbol: str
    status: str  # "no_position" | "pre_active_closed" | "exit_submitted" | "already_exiting"
    close_reason: str | None = None
    pnl: float | None = None
    fill_price: float | None = None


class OrderRouter:
    """Places brackets, routes fills back into the state machine."""

    def __init__(
        self,
        ib: IBClient,
        sm: StateMachine,
        *,
        strategy_name: str,
        dirty_set: DirtySet | None = None,
    ) -> None:
        self._ib = ib
        self._sm = sm
        self._strategy_name = strategy_name
        # order_id → Lifecycle for fast lookup inside on_fill
        self._by_order_id: dict[int, Lifecycle] = {}
        # lifecycle_id → Lifecycle (canonical cache; refreshed on each transition)
        self._by_lifecycle_id: dict[str, Lifecycle] = {}
        # lifecycle_id → contract (needed for cancel/protective-stop placement)
        self._contracts: dict[str, Contract] = {}
        # order_ids whose fill should be attributed to EOD (vs natural TP/STOP).
        self._eod_orders: set[int] = set()
        # PR #16 — order_ids whose fill should be attributed to a manual close
        # (operator-initiated /flatten or /exit). Routed via ExitReason.MANUAL.
        self._manual_orders: set[int] = set()
        # PR #11 — opt-in dirty set so the reconciler can re-check lifecycles
        # touched by recent events. None when run in isolation (e.g. unit tests
        # that don't care about reconciliation).
        self._dirty_set = dirty_set
        # EXIT_MODE=trailing — per-position high-water mark (LONG: highest high
        # seen; SHORT: lowest low) used by the bar-close ratchet. In-memory and
        # reconstructed on recovery (seeded to entry); the resting STP itself is
        # broker-resident and survives a restart, so losing this counter can never
        # un-ratchet a stop — compute_ratcheted_stop only moves it toward price.
        self._highest: dict[str, float] = {}

    def _mark_dirty(self, lifecycle_id: str) -> None:
        if self._dirty_set is not None:
            self._dirty_set.add(lifecycle_id)

    # -------------------------------------------------------------- registration

    def register_recovered(self, lifecycle: Lifecycle, contract: Contract) -> None:
        """Re-attach a lifecycle loaded by the orchestrator at boot.

        Routing keys come from whichever ``*_order_id`` fields are populated;
        a CLOSED lifecycle is intentionally not registered (no inbound fills).
        """
        if lifecycle.state == State.CLOSED.value:
            return
        self._by_lifecycle_id[lifecycle.lifecycle_id] = lifecycle
        self._contracts[lifecycle.lifecycle_id] = contract
        for oid in (
            lifecycle.entry_order_id,
            lifecycle.target_order_id,
            lifecycle.stop_order_id,
        ):
            if oid is not None:
                self._by_order_id[oid] = lifecycle
        # Seed the high-water mark so a recovered ACTIVE position still ratchets.
        # PR-2 — prefer the PERSISTED peak (metadata.highest_price) so a restart
        # mid-trend resumes from the true high; fall back to entry. Clamped so it
        # is NEVER on the losing side of entry (LONG: >= entry; SHORT: <= entry) —
        # the ratchet only moves the stop toward price, so this can never un-ratchet.
        if lifecycle.state == State.ACTIVE.value and lifecycle.entry_price is not None:
            entry = float(lifecycle.entry_price)
            persisted = (lifecycle.metadata or {}).get("highest_price")
            if persisted is None:
                seed = entry
            elif Direction(lifecycle.direction) is Direction.LONG:
                seed = max(float(persisted), entry)
            else:
                seed = min(float(persisted), entry)
            self._highest[lifecycle.lifecycle_id] = seed

    def register_eod_exit(self, lifecycle: Lifecycle, order_id: int) -> None:
        """Route a forthcoming fill on ``order_id`` to ``lifecycle`` as EOD-closed.

        Called by :class:`EodForceClose` after placing the market exit so the
        downstream ``on_fill`` callback can stamp ``exit_reason='EOD'`` instead
        of misclassifying it as a TP/SL fill.
        """
        self._by_order_id[order_id] = lifecycle
        self._by_lifecycle_id[lifecycle.lifecycle_id] = lifecycle
        self._eod_orders.add(order_id)

    def register_manual_exit(self, lifecycle: Lifecycle, order_id: int) -> None:
        """PR #16 — sibling of :meth:`register_eod_exit` for operator-initiated closes.

        Routes the forthcoming fill via ``ExitReason.MANUAL`` so the lifecycle
        row's ``exit_reason`` ends up as MANUAL (not EOD / TARGET / STOP).
        """
        self._by_order_id[order_id] = lifecycle
        self._by_lifecycle_id[lifecycle.lifecycle_id] = lifecycle
        self._manual_orders.add(order_id)

    # ----------------------------------------------------------------- placement

    async def place_entry(
        self,
        signal: Signal,
        contract: Contract,
        *,
        setup_key: str | None = None,
    ) -> Lifecycle:
        """Submit the entry bracket and transition IDLE → ENTERING.

        On any failure mid-sequence the router attempts a best-effort cancel of
        already-placed legs and transitions the lifecycle to CLOSED with
        exit_reason='MANUAL'. The PR description's "What could go wrong"
        section documents the assumption that IB will accept the cancel — the
        EOD force-close path is the backstop.
        """
        direction = Direction(signal.direction)
        qty = self._contracts_per_signal()

        # PR-B — pass the per-bar setup_key (same-setup dedup) and this entry's qty
        # (aggregate contract cap) into the in-lock create gates. setup_key is None
        # when the caller is not the bar-dispatch path (e.g. direct integration use).
        lc = await self._sm.create_lifecycle(
            signal.instrument,
            self._strategy_name,
            direction,
            setup_key=setup_key,
            qty=qty,
        )

        # PR B — native server-side OCA bracket: entry parent + fixed-STP child +
        # trailing-TP child, all submitted atomically (transmit chains on the last
        # leg). The OCA group lets a fill on one exit leg cancel the sibling
        # broker-side, and the parentId linkage makes the protective STP a native
        # bracket leg that SURVIVES a client disconnect/redeploy (Task E.1 fix).
        oca_group = _oca_group_for(lc.lifecycle_id)
        try:
            parent, stop_child, tp_child = build_entry_oca_bracket(
                direction=direction,
                qty=qty,
                entry_type="MKT",
                entry_lmt_price=None,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                exit_mode=RISK.exit_mode,
                trail_offset=RISK.trail_offset_pts,
                entry_ref_price=signal.entry_price,
                oca_group=oca_group,
            )
            # PR-3 — the bracket SHAPE is the ground truth of which exit path this
            # entry takes: trailing → STP-only (tp_child is None, bar-ratchet walks
            # it); fixed → STP+LMT. Log it on EVERY entry so a mode anomaly (the
            # 16:53Z fixed-under-trailing case) is visible in the logs immediately.
            bracket_shape = "fixed" if tp_child is not None else "trailing"
            LOGGER.info(
                "[EXEC] %s: entry exit_mode=%s bracket=%s lifecycle=%s",
                signal.instrument,
                RISK.exit_mode,
                bracket_shape,
                lc.lifecycle_id,
            )
            # Guard: the built shape must match the resolved EXIT_MODE. A mismatch
            # means the mode and the bracket disagree (env/build drift) — surface it
            # LOUDLY but do NOT crash; the bracket is internally valid either way.
            if bracket_shape != RISK.exit_mode:
                LOGGER.warning(
                    "[EXEC] %s: exit_mode_mismatch — EXIT_MODE=%s but built a %s bracket "
                    "(tp_child=%s); entry proceeds, but the active mode is inconsistent — "
                    "investigate env/deploy. lifecycle=%s",
                    signal.instrument,
                    RISK.exit_mode,
                    bracket_shape,
                    "set" if tp_child is not None else "None",
                    lc.lifecycle_id,
                )
            LOGGER.info(
                "[EXEC] %s: place_parent — direction=%s qty=%s type=MKT exit_mode=%s oca=%s",
                signal.instrument,
                direction.value,
                qty,
                RISK.exit_mode,
                oca_group,
            )
            parent_trade = await self._ib.place_order(contract, parent)
            parent_order_id = self._order_id_of(parent_trade)

            # STABILIZE-5 — trailing mode returns stop_child=None: the protective STP
            # is NOT a native bracket child (a parentId-linked child is auto-OCA'd by
            # the gateway → Error 10326 on the ratchet's modify). It is placed
            # STANDALONE (parentId=0, ungrouped) on the parent fill in
            # _handle_parent_fill → ensure_protective_stop. Fixed mode still places the
            # STP+LMT OCA bracket children up-front here.
            stop_order_id: int | None = None
            if stop_child is not None:
                stop_child.parentId = parent_order_id
                LOGGER.info(
                    "[EXEC] %s: place_stop_child — parent_id=%s stp=%.2f (oca=%s)",
                    signal.instrument,
                    parent_order_id,
                    signal.stop_price,
                    oca_group,
                )
                stop_trade = await self._ib.place_order(contract, stop_child)
                stop_order_id = self._order_id_of(stop_trade)
            else:
                LOGGER.info(
                    "[EXEC] %s: trailing entry — protective STP deferred to standalone "
                    "post-fill placement (parentId=0, ungrouped; STABILIZE-5) lifecycle=%s",
                    signal.instrument,
                    lc.lifecycle_id,
                )

            # Fixed mode → a LMT take-profit child completes the OCA bracket.
            # Trailing mode → tp_child is None: the STP is the sole (transmitting)
            # exit leg, and the standalone TRAIL is placed post-fill (Error 328,
            # §0.5.190). target_order_id stays None until — if ever — a TP exists.
            tp_order_id: int | None = None
            if tp_child is not None:
                tp_child.parentId = parent_order_id
                LOGGER.info(
                    "[EXEC] %s: place_tp — parent_id=%s mode=%s target=%.2f trail_offset=%.2f",
                    signal.instrument,
                    parent_order_id,
                    RISK.exit_mode,
                    signal.target_price,
                    RISK.trail_offset_pts,
                )
                tp_trade = await self._ib.place_order(contract, tp_child)
                tp_order_id = self._order_id_of(tp_trade)
            else:
                LOGGER.info(
                    "[ROUTER] %s: entry_placed — bracket=STP-only exit_mode=%s entry=%.2f "
                    "(STP is sole exit leg; the bar-close ratchet walks it — §0.5.196)",
                    signal.instrument,
                    RISK.exit_mode,
                    signal.entry_price,
                )
        except Exception as exc:
            LOGGER.error(
                "[EXEC] %s: place_entry_failed — type=%s msg=%s",
                signal.instrument,
                type(exc).__name__,
                exc,
            )
            await self._close_pre_active(lc, signal)
            raise

        lc = await self._sm.transition(
            lc,
            State.ENTERING,
            reason="entry_submitted",
            payload={
                "entry_price_target": signal.entry_price,
                "stop_price": signal.stop_price,
                "target_price": signal.target_price,
                "adx": signal.adx_value,
            },
            entry_order_id=parent_order_id,
            target_order_id=tp_order_id,
            stop_order_id=stop_order_id,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
        )

        self._by_lifecycle_id[lc.lifecycle_id] = lc
        self._contracts[lc.lifecycle_id] = contract
        self._by_order_id[parent_order_id] = lc
        if tp_order_id is not None:
            self._by_order_id[tp_order_id] = lc
        if stop_order_id is not None:  # None in trailing — the STP is placed post-fill
            self._by_order_id[stop_order_id] = lc
        self._mark_dirty(lc.lifecycle_id)
        # PR #14 — operator alert (Telegram picks this up via the [ALERT] log handler).
        LOGGER.info(
            "[ALERT] entry_placed: symbol=%s direction=%s qty=%s entry=%.2f target=%.2f stop=%.2f "
            "lifecycle_id=%s",
            signal.instrument,
            direction.value,
            qty,
            signal.entry_price,
            signal.target_price,
            signal.stop_price,
            lc.lifecycle_id,
        )
        return lc

    async def _close_pre_active(self, lc: Lifecycle, signal: Signal) -> None:
        """Mark a pre-ACTIVE lifecycle as CLOSED after a placement failure.

        Pre-ACTIVE means: no entry fill confirmed yet, so entry_qty=0 and pnl=0.
        ENTERING → CLOSED is permitted by ALLOWED_TRANSITIONS; IDLE → CLOSED is
        also permitted (entry rejected before any leg landed).
        """
        try:
            now = datetime.now(UTC).isoformat()
            current = State(lc.state)
            updates: dict[str, Any] = {
                "entry_qty": 0,
                "entry_price": 0.0,
                "entry_filled_at": now,
                "exit_qty": 0,
                "exit_price": 0.0,
                "exit_filled_at": now,
                "exit_reason": ExitReason.MANUAL.value,
                "commission_total": 0.0,
                "pnl_gross": 0.0,
                "pnl_net": 0.0,
            }
            if current is State.IDLE:
                updates["entry_order_id"] = 0
                updates["exit_order_id"] = 0
            else:
                updates["exit_order_id"] = lc.entry_order_id or 0
            await self._sm.transition(
                lc,
                State.CLOSED,
                reason="entry_placement_failed",
                **updates,
            )
        except Exception as exc:
            LOGGER.error(
                "[EXEC] %s: close_pre_active_error — type=%s msg=%s",
                lc.symbol,
                type(exc).__name__,
                exc,
            )

    # --------------------------------------------------------------- fill routing

    async def on_fill(self, trade: Trade, fill: Any | None = None) -> None:
        """Async fillEvent handler. Routes by ``trade.order.orderId``.

        Idempotency: duplicate events for an already-CLOSED lifecycle raise
        ``InvariantViolationError`` from the state machine; we catch and log so
        the event handler never crashes the orchestrator loop.
        """
        order_id = self._order_id_of(trade)
        lc = self._by_order_id.get(order_id)
        if lc is None:
            LOGGER.debug("[EXEC] on_fill: no_lifecycle_for_order — id=%s", order_id)
            return

        # Refresh from cache so we always operate on the latest transition.
        lc = self._by_lifecycle_id.get(lc.lifecycle_id, lc)

        try:
            if order_id in self._eod_orders:
                await self._handle_exit_fill(lc, trade, ExitReason.EOD)
            elif order_id in self._manual_orders:
                await self._handle_exit_fill(lc, trade, ExitReason.MANUAL)
            elif order_id == lc.entry_order_id:
                await self._handle_parent_fill(lc, trade)
            elif order_id == lc.target_order_id:
                await self._handle_exit_fill(lc, trade, ExitReason.TARGET)
            elif order_id == lc.stop_order_id:
                await self._handle_exit_fill(lc, trade, ExitReason.STOP)
            else:
                LOGGER.debug(
                    "[EXEC] on_fill: unrouted_order — lifecycle=%s order=%s",
                    lc.lifecycle_id,
                    order_id,
                )
        except InvariantViolationError as exc:
            LOGGER.warning(
                "[EXEC] on_fill: idempotent_skip — lifecycle=%s order=%s msg=%s",
                lc.lifecycle_id,
                order_id,
                exc,
            )

    async def _handle_parent_fill(self, lc: Lifecycle, trade: Trade) -> None:
        contract = self._contracts.get(lc.lifecycle_id)
        if contract is None:
            LOGGER.error(
                "[EXEC] %s: parent_fill_no_contract — lifecycle=%s",
                lc.symbol,
                lc.lifecycle_id,
            )
            return

        # Recovery guard: trailing-mode high-water seeding fires ONLY on a genuine
        # new entry fill (lifecycle was ENTERING). A recovered/already-bracketed
        # position is registered as ACTIVE (and seeded in register_recovered) and
        # never routed here, so it keeps its existing stop untouched.
        was_entering = State(lc.state) is State.ENTERING

        fill_qty, fill_price, fill_time = self._extract_fill(trade)
        qty = fill_qty or self._contracts_per_signal()

        # Protective STP placement, by mode:
        #  - trailing (STABILIZE-5): place_entry placed NO bracket-child stop
        #    (lc.stop_order_id is None), so ensure_protective_stop places the STANDALONE
        #    STP here (parentId=0, ungrouped) — the gateway can't auto-OCA it, so the
        #    bar-close ratchet can modify it (Error 10326 designed out). This is the
        #    primary placement, synchronous in the fill callback so the naked window is
        #    bounded by one IB round-trip (§0.5.T5; reconciler leg-heal is the backstop).
        #  - fixed (PR B): the STP rests up-front as an OCA bracket child
        #    (lc.stop_order_id set), so ensure_protective_stop is an idempotent no-op
        #    (existing_stop_is_live=True → returns the existing id, no double-place).
        stop_already_resting = lc.stop_order_id is not None
        stp_order_id = await ensure_protective_stop(
            self._ib,
            contract,
            lc,
            qty=qty,
            existing_stop_is_live=stop_already_resting,
            component="ROUTER",
            path="parent_fill",
        )

        # Trailing mode — the entry bracket's fixed STP @ entry-stop_loss_pts IS
        # the stop the bar-close ratchet walks UP (SeanBot V3/V12). No separate
        # order is placed here; just seed the high-water mark to the fill price so
        # the first qualifying bar can ratchet. The STP stays the protective floor.
        final_stop_id = stp_order_id
        if RISK.exit_mode == "trailing" and was_entering:
            self._highest[lc.lifecycle_id] = float(fill_price)
            LOGGER.info(
                "[EXEC] %s: trailing_armed — fixed STP id=%s @entry-%.0f is the "
                "ratchet floor; highest seeded=%.2f lifecycle=%s",
                lc.symbol,
                stp_order_id,
                RISK.stop_loss_pts,
                fill_price,
                lc.lifecycle_id,
            )

        lc = await self._sm.transition(
            lc,
            State.ACTIVE,
            reason="entry_filled",
            payload={"fill_price": fill_price, "fill_qty": fill_qty},
            entry_qty=fill_qty,
            entry_price=fill_price,
            entry_filled_at=fill_time,
            stop_order_id=final_stop_id,
        )

        self._by_lifecycle_id[lc.lifecycle_id] = lc
        if final_stop_id is not None:
            self._by_order_id[final_stop_id] = lc
        self._mark_dirty(lc.lifecycle_id)

        # PR-2 — persist the initial high-water mark so a restart right after entry
        # resumes from it rather than reseeding to entry on the next recovery.
        if RISK.exit_mode == "trailing" and was_entering:
            await self._persist_highest(lc, float(fill_price))

    # --------------------------------------------- EXIT_MODE=trailing bar ratchet

    async def _persist_highest(self, lc: Lifecycle, highest: float) -> None:
        """Durably store the high-water mark (PR-2). Never raises into the caller:
        the in-memory ``_highest`` governs the live ratchet, so persistence is a
        best-effort durability improvement for restart recovery only."""
        try:
            await self._sm.persist_highest(lc, highest)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "[EXEC] %s: persist_highest_failed — type=%s msg=%s (in-memory peak kept)",
                lc.symbol,
                type(exc).__name__,
                exc,
            )

    async def _persist_stop_price(self, lc: Lifecycle, stop_price: float) -> None:
        """Durably store the ratcheted stop level (STABILIZE-3). Never raises into the
        caller: the broker-resident STP is the live protection, so persistence is a
        durability improvement for the reconciler's leg-heal source-of-truth only —
        but a failure here means a redeploy-dropped stop would heal at base, so it is
        logged loudly."""
        try:
            await self._sm.persist_stop_price(lc, stop_price)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "[EXEC] %s: persist_stop_price_failed — type=%s msg=%s "
                "(broker STP @%.2f still governs; heal source may be stale)",
                lc.symbol,
                type(exc).__name__,
                exc,
                stop_price,
            )

    def _active_trailing_lifecycles(self) -> list[Lifecycle]:
        """Every ACTIVE lifecycle to ratchet this bar (PR-A: N simultaneous, was ≤1).

        Each carries its own standalone parentId=0 STP and its own high-water mark
        (keyed by lifecycle_id in ``_highest`` / ``_contracts``), so the bar-close
        ratchet trails each independently. Insertion-ordered; at the current
        max_concurrent=1 this yields exactly the single lifecycle the prior
        singular helper returned — no behavior change."""
        return [lc for lc in self._by_lifecycle_id.values() if lc.state == State.ACTIVE.value]

    async def _broker_in_position(self, lc: Lifecycle) -> bool:
        """§0.5.98 — confirm the broker still holds the position in lc.direction
        before modifying its stop. Conservative: any error → False (don't touch)."""
        try:
            positions = await self._ib.get_positions()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "[EXEC] %s: verify_position_error — type=%s msg=%s (skip ratchet)",
                lc.symbol,
                type(exc).__name__,
                exc,
            )
            return False
        want_long = Direction(lc.direction) is Direction.LONG
        for p in positions:
            c = getattr(p, "contract", None)
            sym = getattr(c, "localSymbol", None) or getattr(c, "symbol", None)
            qty = float(getattr(p, "position", 0) or 0)
            if sym == lc.symbol and qty != 0 and ((qty > 0) == want_long):
                return True
        return False

    async def ratchet_stop_on_bar(
        self,
        *,
        bar_high: float | None,
        bar_low: float | None,
        bar_close: float | None,
    ) -> None:
        """SeanBot V3/V12 bar-close exit ratchet (EXIT_MODE=trailing only).

        For EVERY open ACTIVE lifecycle (PR-A: N simultaneous; was the single open
        position), updates its own high-water mark from this bar, walks its own
        resting GTC SELL STP UP only (cancel-free: modify-in-place via re-placeOrder
        — one resting stop the whole time, no naked window), and market-exits that
        position on the +hard_ceiling cap. A no-op in fixed mode or with no open
        position. NEVER raises into the bar loop; a failure on one lifecycle is
        isolated (per-lifecycle try/except) and leaves every stop live."""
        if RISK.exit_mode != "trailing":
            return
        for lc in self._active_trailing_lifecycles():
            try:
                await self._ratchet_one(lc, bar_high=bar_high, bar_low=bar_low, bar_close=bar_close)
            except Exception as exc:  # noqa: BLE001 — never break the bar feed; keep the stop
                LOGGER.error(
                    "[EXEC] %s: ratchet_error — lifecycle=%s type=%s msg=%s (stop unchanged)",
                    lc.symbol,
                    lc.lifecycle_id,
                    type(exc).__name__,
                    exc,
                )

    async def _ratchet_one(
        self,
        lc: Lifecycle,
        *,
        bar_high: float | None,
        bar_low: float | None,
        bar_close: float | None,
    ) -> None:
        direction = Direction(lc.direction)
        entry = lc.entry_price
        contract = self._contracts.get(lc.lifecycle_id)
        if entry is None or contract is None or bar_close is None:
            return

        # 1) Update the high-water mark (LONG: highest high; SHORT: lowest low).
        prior = self._highest.get(lc.lifecycle_id, float(entry))
        if direction is Direction.LONG:
            highest = max(prior, float(bar_high)) if bar_high is not None else prior
        else:
            highest = min(prior, float(bar_low)) if bar_low is not None else prior
        self._highest[lc.lifecycle_id] = highest
        # PR-2 — persist the peak whenever it ADVANCES (a new high/low), so a
        # restart resumes the ratchet from the true peak, not from entry.
        if highest != prior:
            await self._persist_highest(lc, highest)

        # 2) Hard ceiling (V3 +1000) → market-exit, regardless of the stop.
        if should_hard_exit(
            entry=float(entry),
            bar_close=float(bar_close),
            direction=direction,
            hard_ceiling_pts=RISK.hard_ceiling_pts,
        ):
            LOGGER.warning(
                "[EXEC] %s: hard_ceiling_hit — close=%.2f entry=%.2f ceiling=%.0f → market exit",
                lc.symbol,
                bar_close,
                entry,
                RISK.hard_ceiling_pts,
            )
            await self.close_position(lc.symbol, reason="hard_ceiling")
            return

        if lc.stop_order_id is None:
            LOGGER.warning(
                "[EXEC] %s: ratchet_no_stop_id — lifecycle=%s (#80 reconciler owns naked heal)",
                lc.symbol,
                lc.lifecycle_id,
            )
            return

        # 3) Read the broker's LIVE stop price as current_stop (§0.5.98 truth).
        order = await self._ib.find_open_order_by_id(lc.stop_order_id)
        current_stop = getattr(order, "auxPrice", None) if order is not None else None
        if current_stop is None and lc.stop_price is not None:
            current_stop = float(lc.stop_price)

        new_stop = compute_ratcheted_stop(
            float(entry),
            highest,
            float(current_stop) if current_stop is not None else None,
            direction=direction,
            stop_loss_pts=RISK.stop_loss_pts,
            lock_in_pts=RISK.lock_in_pts,
            trail_offset_pts=RISK.trail_offset_pts,
        )
        if new_stop is None:
            return  # no improvement → ratchet never moves the stop down

        # 4) §0.5.98 — confirm we are still in the position before touching the stop.
        if not await self._broker_in_position(lc):
            LOGGER.info(
                "[EXEC] %s: ratchet_skipped_not_in_position — lifecycle=%s",
                lc.symbol,
                lc.lifecycle_id,
            )
            return
        if order is None:
            LOGGER.error(
                "[EXEC] %s: ratchet_no_resting_stop — id=%s not live; cannot modify "
                "(#80 reconciler owns naked heal); STOP-REPORT",
                lc.symbol,
                lc.stop_order_id,
            )
            return

        # 5) Modify in place: mutate auxPrice + re-placeOrder the SAME order id
        # (ib_async order-modify) — one resting stop throughout, no naked window.
        if not getattr(contract, "exchange", None):
            contract.exchange = "CME"  # §0.5.192
        prior_aux = getattr(order, "auxPrice", None)
        order.auxPrice = float(new_stop)
        try:
            trade = await self._ib.place_order(contract, order)
        except Exception as exc:  # noqa: BLE001 — keep the prior stop; never raise
            LOGGER.error(
                "[EXEC] %s: ratchet_modify_failed — id=%s type=%s msg=%s "
                "(KEEP prior stop @%s; STOP-REPORT on Error 321/201/202)",
                lc.symbol,
                lc.stop_order_id,
                type(exc).__name__,
                exc,
                f"{prior_aux:.2f}" if prior_aux is not None else "?",
            )
            return
        if not await _confirm_order_resting(trade):
            status = getattr(getattr(trade, "orderStatus", None), "status", "?")
            LOGGER.error(
                "[EXEC] %s: ratchet_modify_not_resting — id=%s status=%s "
                "(prior stop @%s still governs; STOP-REPORT)",
                lc.symbol,
                lc.stop_order_id,
                status,
                f"{prior_aux:.2f}" if prior_aux is not None else "?",
            )
            return

        # 6) §0.5.98 broker truth — a modify acts on an ALREADY-resting order, so its
        # status reads "resting" before any async rejection lands. Confirm the broker
        # order ACTUALLY holds new_stop before announcing/persisting; otherwise the
        # alert + DB would claim a level the broker never accepted (the STABILIZE-3
        # bug: an Error 10326 OCA-revision cancel raised the in-memory/Telegram level
        # while the broker dropped the stop). Do NOT announce or persist on failure.
        if not await _confirm_stop_at_aux(self._ib, lc.stop_order_id, float(new_stop)):
            LOGGER.error(
                "[EXEC] %s: ratchet_modify_unconfirmed — id=%s broker did not confirm "
                "aux=%.2f (prior stop @%s governs; #80 reconciler owns naked heal); STOP-REPORT",
                lc.symbol,
                lc.stop_order_id,
                new_stop,
                f"{prior_aux:.2f}" if prior_aux is not None else "?",
            )
            return

        # 7) Persist the ratcheted level so the reconciler's leg-heal re-arms at the
        # RATCHETED stop, not the stale entry−75 base (never lowers the protection).
        updated = replace(lc, stop_price=float(new_stop))
        self._by_lifecycle_id[lc.lifecycle_id] = updated
        await self._persist_stop_price(updated, float(new_stop))
        LOGGER.info(
            "[ALERT] trailing_stop_ratcheted: symbol=%s stop_id=%s old=%s new=%.2f "
            "highest=%.2f entry=%.2f lifecycle_id=%s",
            lc.symbol,
            lc.stop_order_id,
            f"{prior_aux:.2f}" if prior_aux is not None else "?",
            new_stop,
            highest,
            entry,
            lc.lifecycle_id,
        )

    async def _handle_exit_fill(
        self,
        lc: Lifecycle,
        trade: Trade,
        reason: ExitReason,
    ) -> None:
        # W-S15.1 / PR B — cancel the resting sibling bracket leg(s) so nothing is
        # left working once this lifecycle reaches CLOSED. As of PR B the exit pair
        # is a native OCA group (ocaType=1), so the broker AUTO-cancels the
        # survivor on a fill — this explicit cancel is now a same-client backstop
        # that is a logged no-op when OCA already cleared the sibling. It still
        # matters for the legacy/recovered standalone-STP rows (#80 heal) and for
        # MANUAL/EOD market exits. An uncancelled SELL leg resting with no position
        # could flip the long-only bot SHORT if hit. Same-client cancel-by-id never
        # hits IB error 10147 (the foreign-client trap that defeats side-scripts).
        await self._cancel_sibling_legs(lc, reason, self._order_id_of(trade))

        fill_qty, fill_price, fill_time = self._extract_fill(trade)
        entry_qty = lc.entry_qty or fill_qty or self._contracts_per_signal()
        entry_price = lc.entry_price or 0.0
        commission_total = float(entry_qty) * MNQ.commission_rt_usd
        pnl_gross = _pnl_gross(
            direction=Direction(lc.direction),
            entry_price=entry_price,
            exit_price=fill_price,
            qty=entry_qty,
        )
        pnl_net = pnl_gross - commission_total

        # Go through EXITING first; ALLOWED_TRANSITIONS forbids ACTIVE → CLOSED.
        # If we're already EXITING (e.g. EOD pre-registered the exit order) the
        # state machine would raise on a no-op transition; skip in that case.
        if State(lc.state) is State.ACTIVE:
            lc = await self._sm.transition(
                lc,
                State.EXITING,
                reason=f"exit_filled:{reason.value}:in_progress",
                payload={"order_id": self._order_id_of(trade)},
                exit_order_id=self._order_id_of(trade),
            )
        lc = await self._sm.transition(
            lc,
            State.CLOSED,
            reason=f"exit_filled:{reason.value}",
            payload={"fill_price": fill_price, "fill_qty": fill_qty},
            exit_qty=fill_qty,
            exit_price=fill_price,
            exit_filled_at=fill_time,
            exit_reason=reason.value,
            commission_total=commission_total,
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
        )
        self._by_lifecycle_id[lc.lifecycle_id] = lc
        self._mark_dirty(lc.lifecycle_id)

        LOGGER.info(
            "[EXEC] %s: trade_closed — lifecycle=%s reason=%s pnl_net=%.2f",
            lc.symbol,
            lc.lifecycle_id,
            reason.value,
            pnl_net,
        )
        # PR #14 — operator alert sibling line.
        LOGGER.info(
            "[ALERT] exit_filled: symbol=%s qty=%s entry=%.2f exit_price=%.2f pnl_net=%.2f "
            "exit_reason=%s lifecycle_id=%s",
            lc.symbol,
            fill_qty,
            entry_price,
            fill_price,
            pnl_net,
            reason.value,
            lc.lifecycle_id,
        )

    # ----------------------------------------------------------------- cancel

    async def _cancel_sibling_legs(
        self,
        lc: Lifecycle,
        reason: ExitReason,
        filled_order_id: int,
    ) -> None:
        """W-S15.1 — idempotently cancel the bracket leg(s) left resting after an
        exit fill, in-process via our own IB client.

        - TARGET fill → cancel the protective STP.
        - STOP fill → cancel the TP LMT.
        - MANUAL / EOD market exit → cancel both remaining children (the prior
          :meth:`cancel_all_for` already handled them — these are safe no-ops).

        The leg that just filled (``filled_order_id``) is skipped. Cancelling an
        already-filled/already-cancelled order is a logged no-op — this method
        never raises, so a duplicate fill or a broker reject can't crash the
        fill-event loop.

        STABILIZE-5 NEVER-ORPHAN — for a trailing position the STP is now a STANDALONE
        order (parentId=0, ungrouped), so there is no OCA to auto-cancel it broker-side.
        This method is the event-driven never-orphan leg: a TARGET/MANUAL/EOD/hard-ceiling
        exit cancels the resting STP here (a STOP fill IS the STP, so nothing to cancel,
        and we must NOT re-arm — never-naked). The reconciler's ``_cancel_open_legs`` is
        the backstop for any exit fill this event-driven path misses.
        """
        if reason is ExitReason.TARGET:
            legs: list[tuple[int | None, str, float | None]] = [
                (lc.stop_order_id, "STP", lc.stop_price)
            ]
        elif reason is ExitReason.STOP:
            legs = [(lc.target_order_id, "LMT", lc.target_price)]
        else:  # MANUAL / EOD — exit is a separate MKT order; clear both children.
            legs = [
                (lc.stop_order_id, "STP", lc.stop_price),
                (lc.target_order_id, "LMT", lc.target_price),
            ]

        for oid, leg, px in legs:
            if oid is None or oid == filled_order_id:
                continue
            try:
                cancelled = await self._ib.cancel_order_by_id(oid)
            except Exception as exc:  # noqa: BLE001 — never raise into the fill-event loop
                LOGGER.warning(
                    "[ROUTER] %s: cancel_sibling_error — leg=%s order=%s type=%s msg=%s",
                    lc.lifecycle_id,
                    leg,
                    oid,
                    type(exc).__name__,
                    exc,
                )
                continue
            if cancelled:
                LOGGER.info(
                    "[ROUTER] %s: cancelled sibling %s order id=%s @%s — %s fill",
                    lc.lifecycle_id,
                    leg,
                    oid,
                    f"{px:.2f}" if px is not None else "?",
                    reason.value,
                )
            else:
                LOGGER.info(
                    "[ROUTER] %s: cancel skipped %s id=%s — not live (already gone) — %s fill",
                    lc.lifecycle_id,
                    leg,
                    oid,
                    reason.value,
                )

    async def cancel_all_for(self, lifecycle: Lifecycle) -> None:
        """Best-effort cancel of every working order_id on a lifecycle.

        Caller (EOD or manual flatten) is responsible for the subsequent
        transition. ``IB.cancelOrder`` is idempotent broker-side; we still skip
        None values to keep the log tidy.
        """
        for oid in (
            lifecycle.entry_order_id,
            lifecycle.target_order_id,
            lifecycle.stop_order_id,
        ):
            if oid is None:
                continue
            try:
                cancelled = await self._ib.cancel_order_by_id(oid)
                LOGGER.info(
                    "[EXEC] %s: cancel_order — lifecycle=%s order=%s issued=%s",
                    lifecycle.symbol,
                    lifecycle.lifecycle_id,
                    oid,
                    cancelled,
                )
            except Exception as exc:
                LOGGER.warning(
                    "[EXEC] %s: cancel_error — lifecycle=%s order=%s type=%s msg=%s",
                    lifecycle.symbol,
                    lifecycle.lifecycle_id,
                    oid,
                    type(exc).__name__,
                    exc,
                )

    # ----------------------------------------------------- manual close (PR #16)

    async def close_position(self, symbol: str, reason: str) -> CloseResult:
        """Close a single non-CLOSED lifecycle matching ``symbol``.

        Mirrors :meth:`EodForceClose._close_one` but parameterised by ``reason``
        (a free-form close discriminator e.g. ``manual_flatten`` /
        ``manual_exit_symbol``). ACTIVE positions are closed asynchronously —
        the MKT exit lands via the existing fill-event path which emits
        ``[ALERT] exit_filled`` with the realized PnL.

        Returns a :class:`CloseResult` summarising what was done; raises only on
        truly unexpected errors (caught + logged at the caller in
        :class:`Orchestrator`).
        """
        lifecycles = await self._sm.load_non_closed()
        matches = [lc for lc in lifecycles if lc.symbol == symbol]
        if not matches:
            LOGGER.info(
                "[EXEC] %s: close_skipped — no_position close_reason=%s",
                symbol,
                reason,
            )
            LOGGER.info(
                "[ALERT] exit_complete: symbol=%s status=no_position close_reason=%s",
                symbol,
                reason,
            )
            return CloseResult(
                closed=False, symbol=symbol, status="no_position", close_reason=reason
            )
        if len(matches) > 1:
            LOGGER.warning(
                "[EXEC] %s: multiple_non_closed_lifecycles — count=%d (acting on first)",
                symbol,
                len(matches),
            )
        lc = matches[0]
        contract = self._contracts.get(lc.lifecycle_id)
        if contract is None:
            LOGGER.error(
                "[EXEC] %s: close_no_contract — lifecycle=%s",
                symbol,
                lc.lifecycle_id,
            )
            return CloseResult(
                closed=False, symbol=symbol, status="no_contract_cached", close_reason=reason
            )

        await self.cancel_all_for(lc)
        current = State(lc.state)

        if current in (State.IDLE, State.ENTERING):
            await self._close_pre_active_manual(lc, reason)
            LOGGER.info(
                "[ALERT] exit_complete: symbol=%s status=pre_active_closed close_reason=%s",
                symbol,
                reason,
            )
            return CloseResult(
                closed=True,
                symbol=symbol,
                status="pre_active_closed",
                close_reason=reason,
                pnl=0.0,
                fill_price=0.0,
            )

        if current is State.ACTIVE:
            qty = lc.entry_qty or 0
            if qty <= 0:
                LOGGER.warning(
                    "[EXEC] %s: active_with_zero_qty — lifecycle=%s skipping market exit",
                    symbol,
                    lc.lifecycle_id,
                )
                LOGGER.info(
                    "[ALERT] exit_complete: symbol=%s status=zero_qty close_reason=%s",
                    symbol,
                    reason,
                )
                return CloseResult(
                    closed=False, symbol=symbol, status="zero_qty", close_reason=reason
                )
            order = _build_market_exit(Direction(lc.direction), qty)
            trade = await self._ib.place_order(contract, order)
            exit_order_id = self._order_id_of(trade)
            LOGGER.info(
                "[EXEC] %s: place_market_exit_manual — lifecycle=%s qty=%s order=%s "
                "close_reason=%s",
                symbol,
                lc.lifecycle_id,
                qty,
                exit_order_id,
                reason,
            )
            lc = await self._sm.transition(
                lc,
                State.EXITING,
                reason="manual_close",
                payload={"order_id": exit_order_id, "close_reason": reason},
                exit_order_id=exit_order_id,
            )
            self.register_manual_exit(lc, exit_order_id)
            LOGGER.info(
                "[ALERT] exit_complete: symbol=%s status=exit_submitted close_reason=%s",
                symbol,
                reason,
            )
            return CloseResult(
                closed=True,
                symbol=symbol,
                status="exit_submitted",
                close_reason=reason,
            )

        # current is State.EXITING — already in flight; cancel above is enough.
        LOGGER.info(
            "[EXEC] %s: close_already_exiting — lifecycle=%s no-op",
            symbol,
            lc.lifecycle_id,
        )
        LOGGER.info(
            "[ALERT] exit_complete: symbol=%s status=already_exiting close_reason=%s",
            symbol,
            reason,
        )
        return CloseResult(
            closed=True, symbol=symbol, status="already_exiting", close_reason=reason
        )

    async def _close_pre_active_manual(self, lc: Lifecycle, reason: str) -> None:
        """Synthesise a CLOSED row for an IDLE/ENTERING lifecycle with no fill.

        Mirrors :meth:`EodForceClose._close_pre_active` field-for-field but with
        ``exit_reason=MANUAL`` and ``close_reason`` carried in the event payload
        for discrimination. The state-machine invariant matrix requires every
        ``_EXIT_FIELDS`` + ``_PNL_FIELDS`` on CLOSED transition — synth zeros.
        """
        now = datetime.now(UTC).isoformat()
        current = State(lc.state)
        updates: dict[str, Any] = {
            "entry_qty": lc.entry_qty or 0,
            "entry_price": lc.entry_price or 0.0,
            "entry_filled_at": lc.entry_filled_at or now,
            "exit_qty": 0,
            "exit_price": 0.0,
            "exit_filled_at": now,
            "exit_reason": ExitReason.MANUAL.value,
            "commission_total": 0.0,
            "pnl_gross": 0.0,
            "pnl_net": 0.0,
        }
        if current is State.IDLE:
            updates["entry_order_id"] = lc.entry_order_id or 0
            updates["exit_order_id"] = lc.entry_order_id or 0
        else:
            updates["exit_order_id"] = lc.entry_order_id or 0
        try:
            await self._sm.transition(
                lc,
                State.CLOSED,
                reason="manual_pre_active",
                payload={"close_reason": reason},
                **updates,
            )
        except InvariantViolationError as exc:
            LOGGER.warning(
                "[EXEC] %s: pre_active_invariant — lifecycle=%s msg=%s",
                lc.symbol,
                lc.lifecycle_id,
                exc,
            )

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _contracts_per_signal() -> int:
        from config.risk_params import RISK

        return RISK.contracts_per_trade

    @staticmethod
    def _order_id_of(trade: Trade) -> int:
        order = getattr(trade, "order", None)
        if order is None:
            return 0
        return int(getattr(order, "orderId", 0) or 0)

    @staticmethod
    def _extract_fill(trade: Trade) -> tuple[int, float, str]:
        """Return ``(qty, avg_price, iso_time)`` from the last fill on the trade.

        Falls back to ``orderStatus`` aggregates and wall-clock when the
        ``fills`` list is empty (some IB simulators omit it).
        """
        fills = getattr(trade, "fills", None) or []
        if fills:
            last = fills[-1]
            execution = getattr(last, "execution", None)
            if execution is not None:
                qty = int(getattr(execution, "shares", 0) or 0)
                price = float(
                    getattr(execution, "avgPrice", None) or getattr(execution, "price", 0.0)
                )
                t = getattr(execution, "time", None)
                iso = t.isoformat() if hasattr(t, "isoformat") else datetime.now(UTC).isoformat()
                if qty and price:
                    return qty, price, iso
        status = getattr(trade, "orderStatus", None)
        qty = int(getattr(status, "filled", 0) or 0) if status else 0
        price = float(getattr(status, "avgFillPrice", 0.0) or 0.0) if status else 0.0
        return qty, price, datetime.now(UTC).isoformat()


async def ensure_protective_stop(
    ib: IBClient,
    contract: Contract,
    lifecycle: Lifecycle,
    *,
    qty: int,
    existing_stop_is_live: bool,
    component: str,
    path: str,
) -> int | None:
    """Idempotently guarantee a broker-resident protective STP for ``lifecycle``.

    Called from BOTH entry-completion paths — the router parent-fill handler
    (:meth:`OrderRouter._handle_parent_fill`) and the reconciler force-fill
    (``recon_entering_to_active``) — so neither path can leave a position
    stop-less (§0.5.T5). Built via :func:`build_protective_stop`, which sets
    ``outsideRth=True`` so the stop is live during the overnight Globex session.

    Idempotent: when ``existing_stop_is_live`` is True (a STP for this lifecycle
    already rests at the broker) it returns the existing id and places nothing,
    so concurrent completion paths never double-place. Returns the stop order id
    (existing or newly placed), or ``None`` when ``stop_price`` is unknown.
    """
    if existing_stop_is_live and lifecycle.stop_order_id is not None:
        LOGGER.info(
            "[%s] %s: protective STP already resting — id=%s (no-op) — %s",
            component,
            lifecycle.symbol,
            lifecycle.stop_order_id,
            path,
        )
        return lifecycle.stop_order_id
    if lifecycle.stop_price is None:
        LOGGER.warning(
            "[%s] %s: protective_stop_skipped — no stop_price on lifecycle id=%s — %s",
            component,
            lifecycle.symbol,
            lifecycle.lifecycle_id,
            path,
        )
        return None
    stp = build_protective_stop(
        direction=Direction(lifecycle.direction),
        qty=qty,
        stop_price=float(lifecycle.stop_price),
    )
    stp_trade = await ib.place_order(contract, stp)
    order = getattr(stp_trade, "order", None)
    raw_oid = getattr(order, "orderId", 0) if order is not None else 0
    try:
        stop_order_id = int(raw_oid or 0)
    except (TypeError, ValueError):
        stop_order_id = 0
    LOGGER.info(
        "[%s] %s: placed protective STP @%.2f outsideRth=True qty=%s id=%s — %s",
        component,
        lifecycle.symbol,
        float(lifecycle.stop_price),
        qty,
        stop_order_id,
        path,
    )
    return stop_order_id


def _pnl_gross(*, direction: Direction, entry_price: float, exit_price: float, qty: int) -> float:
    """LONG: (exit - entry) * qty * multiplier; SHORT mirrors. MNQ multiplier = 2.0."""
    delta = exit_price - entry_price if direction is Direction.LONG else entry_price - exit_price
    return delta * qty * MNQ.multiplier


def _build_market_exit(direction: Direction, qty: int) -> Order:
    """Build a DAY MKT order that flat-closes a ``direction`` position of size ``qty``.

    Duplicates :func:`src.execution.force_close._build_market_exit` deliberately
    — force_close.py is in MUST-NOT-MODIFY scope for PR #16 and the helper is
    private. Keep both in sync.
    """
    o = Order()
    o.action = "SELL" if direction is Direction.LONG else "BUY"
    o.totalQuantity = qty
    o.orderType = "MKT"
    o.tif = "DAY"
    o.transmit = True
    return o
