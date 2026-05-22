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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ib_async import Contract, Order, Trade

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

    def register_manual_exit(self, lifecycle: Lifecycle, order_id: int) -> None:
        """PR #16 — sibling of :meth:`register_eod_exit` for operator-initiated closes.

        Routes the forthcoming fill via ``ExitReason.MANUAL`` so the lifecycle
        row's ``exit_reason`` ends up as MANUAL (not EOD / TARGET / STOP).
        """
        self._by_order_id[order_id] = lifecycle
        self._by_lifecycle_id[lifecycle.lifecycle_id] = lifecycle
        self._manual_orders.add(order_id)

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
