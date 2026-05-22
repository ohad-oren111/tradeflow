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

import logging
from datetime import UTC, datetime
from typing import Any

from ib_async import Contract, Trade

from config.instruments import MNQ
from src.clients.ib_client import IBClient
from src.execution.bracket import build_bracket, build_protective_stop
from src.execution.dirty_set import DirtySet
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
        # PR #11 — opt-in dirty set so the reconciler can re-check lifecycles
        # touched by recent events. None when run in isolation (e.g. unit tests
        # that don't care about reconciliation).
        self._dirty_set = dirty_set

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

    def register_eod_exit(self, lifecycle: Lifecycle, order_id: int) -> None:
        """Route a forthcoming fill on ``order_id`` to ``lifecycle`` as EOD-closed.

        Called by :class:`EodForceClose` after placing the market exit so the
        downstream ``on_fill`` callback can stamp ``exit_reason='EOD'`` instead
        of misclassifying it as a TP/SL fill.
        """
        self._by_order_id[order_id] = lifecycle
        self._by_lifecycle_id[lifecycle.lifecycle_id] = lifecycle
        self._eod_orders.add(order_id)

    # ----------------------------------------------------------------- placement

    async def place_entry(self, signal: Signal, contract: Contract) -> Lifecycle:
        """Submit the entry bracket and transition IDLE → ENTERING.

        On any failure mid-sequence the router attempts a best-effort cancel of
        already-placed legs and transitions the lifecycle to CLOSED with
        exit_reason='MANUAL'. The PR description's "What could go wrong"
        section documents the assumption that IB will accept the cancel — the
        EOD force-close path is the backstop.
        """
        direction = Direction(signal.direction)
        qty = self._contracts_per_signal()

        lc = await self._sm.create_lifecycle(signal.instrument, self._strategy_name, direction)

        try:
            parent, tp_child = build_bracket(
                direction=direction,
                qty=qty,
                entry_type="MKT",
                entry_lmt_price=None,
                target_price=signal.target_price,
            )
            LOGGER.info(
                "[EXEC] %s: place_parent — direction=%s qty=%s type=MKT",
                signal.instrument,
                direction.value,
                qty,
            )
            parent_trade = await self._ib.place_order(contract, parent)
            parent_order_id = self._order_id_of(parent_trade)

            tp_child.parentId = parent_order_id
            LOGGER.info(
                "[EXEC] %s: place_tp — parent_id=%s lmt=%.2f",
                signal.instrument,
                parent_order_id,
                signal.target_price,
            )
            tp_trade = await self._ib.place_order(contract, tp_child)
            tp_order_id = self._order_id_of(tp_trade)
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
            stop_price=signal.stop_price,
            target_price=signal.target_price,
        )

        self._by_lifecycle_id[lc.lifecycle_id] = lc
        self._contracts[lc.lifecycle_id] = contract
        self._by_order_id[parent_order_id] = lc
        self._by_order_id[tp_order_id] = lc
        self._mark_dirty(lc.lifecycle_id)
        # PR #14 — operator alert (Telegram picks this up via the [ALERT] log handler).
        LOGGER.info(
            "[ALERT] entry_placed: symbol=%s direction=%s qty=%s target=%.2f stop=%.2f "
            "lifecycle_id=%s",
            signal.instrument,
            direction.value,
            qty,
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

        fill_qty, fill_price, fill_time = self._extract_fill(trade)

        # Place the protective STP synchronously per §0.5.T5.
        stp = build_protective_stop(
            direction=Direction(lc.direction),
            qty=fill_qty or self._contracts_per_signal(),
            stop_price=float(lc.stop_price) if lc.stop_price is not None else 0.0,
        )
        LOGGER.info(
            "[EXEC] %s: place_stp — lifecycle=%s stop=%.2f qty=%s",
            lc.symbol,
            lc.lifecycle_id,
            stp.auxPrice,
            stp.totalQuantity,
        )
        stp_trade = await self._ib.place_order(contract, stp)
        stp_order_id = self._order_id_of(stp_trade)

        lc = await self._sm.transition(
            lc,
            State.ACTIVE,
            reason="entry_filled",
            payload={"fill_price": fill_price, "fill_qty": fill_qty},
            entry_qty=fill_qty,
            entry_price=fill_price,
            entry_filled_at=fill_time,
            stop_order_id=stp_order_id,
        )

        self._by_lifecycle_id[lc.lifecycle_id] = lc
        self._by_order_id[stp_order_id] = lc
        self._mark_dirty(lc.lifecycle_id)

    async def _handle_exit_fill(
        self,
        lc: Lifecycle,
        trade: Trade,
        reason: ExitReason,
    ) -> None:
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
            "[ALERT] exit_filled: symbol=%s qty=%s exit_price=%.2f pnl_net=%.2f "
            "exit_reason=%s lifecycle_id=%s",
            lc.symbol,
            fill_qty,
            fill_price,
            pnl_net,
            reason.value,
            lc.lifecycle_id,
        )

    # ----------------------------------------------------------------- cancel

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
                await self._ib.cancel_order(_order_handle(oid))
                LOGGER.info(
                    "[EXEC] %s: cancel_order — lifecycle=%s order=%s",
                    lifecycle.symbol,
                    lifecycle.lifecycle_id,
                    oid,
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


def _pnl_gross(*, direction: Direction, entry_price: float, exit_price: float, qty: int) -> float:
    """LONG: (exit - entry) * qty * multiplier; SHORT mirrors. MNQ multiplier = 2.0."""
    delta = exit_price - entry_price if direction is Direction.LONG else entry_price - exit_price
    return delta * qty * MNQ.multiplier


def _order_handle(order_id: int) -> Any:
    """Cheap stand-in object for ``IB.cancelOrder``: the real API takes an Order,
    but only reads ``.orderId``. Avoids holding a long-lived Order reference in
    the router cache.
    """
    handle = _OrderIdRef()
    handle.orderId = order_id
    return handle


class _OrderIdRef:
    """Minimal duck-type for ``IB.cancelOrder``. ``orderId`` is the only field read."""

    orderId: int = 0  # noqa: N815 — IB-API attribute name, must match ib_async
