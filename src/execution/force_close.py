"""End-of-day force-close. Fires at 3:58pm America/New_York every weekday.

§0.5.T5 forbids leaving a futures position without a GTC stop; this scheduler
goes further and flat-closes every non-CLOSED lifecycle 2 minutes before the
RTH close so the bot is fully flat overnight. PR #10 is paper-only — the
behaviour is the same in live, but the rule is enforced first in paper.

Idempotency: every fire reloads non-CLOSED lifecycles from Supabase. If a
prior fire already closed all positions, the second fire observes an empty
list and no-ops. Crash + restart near 3:58pm therefore does not double-cancel.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ib_async import Contract, Order

from src.execution.router import OrderRouter
from src.state_machine import (
    Direction,
    ExitReason,
    InvariantViolationError,
    Lifecycle,
    State,
    StateMachine,
)

LOGGER = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_DEFAULT_EOD_HOUR_ET = 15
_DEFAULT_EOD_MINUTE_ET = 58


class EodForceClose:
    """One-shot scheduler that flat-closes every weekday at 3:58pm ET."""

    def __init__(
        self,
        router: OrderRouter,
        sm: StateMachine,
        *,
        contract: Contract,
        eod_hour_et: int = _DEFAULT_EOD_HOUR_ET,
        eod_minute_et: int = _DEFAULT_EOD_MINUTE_ET,
    ) -> None:
        self._router = router
        self._sm = sm
        self._contract = contract
        self._hour = eod_hour_et
        self._minute = eod_minute_et

    def next_trigger_at(self, now_utc: datetime | None = None) -> datetime:
        """Return the next datetime (UTC) at which the EOD task should fire.

        Skips weekends. If the next 3:58pm ET on a weekday is already in the
        past relative to ``now_utc``, returns the same time on the following
        weekday.
        """
        if now_utc is None:
            now_utc = datetime.now(UTC)
        now_et = now_utc.astimezone(_ET)
        candidate_et = now_et.replace(hour=self._hour, minute=self._minute, second=0, microsecond=0)
        if candidate_et <= now_et:
            candidate_et = candidate_et + timedelta(days=1)
        while candidate_et.weekday() >= 5:
            candidate_et = candidate_et + timedelta(days=1)
        return candidate_et.astimezone(UTC)

    async def fire_once(self) -> int:
        """Flat-close every non-CLOSED lifecycle. Returns the count processed."""
        lifecycles = await self._sm.load_non_closed()
        if not lifecycles:
            LOGGER.info("[EOD] fire: no_open_lifecycles")
            return 0

        processed = 0
        for lc in lifecycles:
            try:
                await self._close_one(lc)
                processed += 1
            except Exception as exc:
                LOGGER.error(
                    "[EOD] fire: close_error — lifecycle=%s type=%s msg=%s",
                    lc.lifecycle_id,
                    type(exc).__name__,
                    exc,
                )
        LOGGER.info("[EOD] fire: complete — processed=%s", processed)
        return processed

    async def _close_one(self, lc: Lifecycle) -> None:
        current = State(lc.state)
        await self._router.cancel_all_for(lc)

        if current in (State.IDLE, State.ENTERING):
            # No fill confirmed — synthesise a clean CLOSED row.
            await self._close_pre_active(lc)
            return

        if current is State.ACTIVE:
            qty = lc.entry_qty or 0
            if qty <= 0:
                LOGGER.warning(
                    "[EOD] %s: active_with_zero_qty — lifecycle=%s skipping market exit",
                    lc.symbol,
                    lc.lifecycle_id,
                )
                return
            order = _build_market_exit(Direction(lc.direction), qty)
            trade = await self._router._ib.place_order(self._contract, order)
            exit_order_id = OrderRouter._order_id_of(trade)
            LOGGER.info(
                "[EOD] %s: place_market_exit — lifecycle=%s qty=%s order=%s",
                lc.symbol,
                lc.lifecycle_id,
                qty,
                exit_order_id,
            )
            lc = await self._sm.transition(
                lc,
                State.EXITING,
                reason="eod_force_close",
                payload={"order_id": exit_order_id},
                exit_order_id=exit_order_id,
            )
            # Tell the router to route the upcoming fill as EOD-closed.
            self._router.register_eod_exit(lc, exit_order_id)
            return

        if current is State.EXITING:
            LOGGER.info(
                "[EOD] %s: already_exiting — lifecycle=%s no-op",
                lc.symbol,
                lc.lifecycle_id,
            )

    async def _close_pre_active(self, lc: Lifecycle) -> None:
        now = datetime.now(UTC).isoformat()
        current = State(lc.state)
        updates: dict[str, object] = {
            "entry_qty": lc.entry_qty or 0,
            "entry_price": lc.entry_price or 0.0,
            "entry_filled_at": lc.entry_filled_at or now,
            "exit_qty": 0,
            "exit_price": 0.0,
            "exit_filled_at": now,
            "exit_reason": ExitReason.EOD.value,
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
            await self._sm.transition(lc, State.CLOSED, reason="eod_pre_active", **updates)
        except InvariantViolationError as exc:
            LOGGER.warning(
                "[EOD] %s: pre_active_invariant — lifecycle=%s msg=%s",
                lc.symbol,
                lc.lifecycle_id,
                exc,
            )

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        """Loop until ``stop_event`` is set: sleep to next fire, then fire once."""
        while not stop_event.is_set():
            now_utc = datetime.now(UTC)
            next_at = self.next_trigger_at(now_utc)
            wait = max(0.0, (next_at - now_utc).total_seconds())
            LOGGER.info(
                "[EOD] scheduled: next_fire=%s wait_sec=%.1f",
                next_at.astimezone(_ET).isoformat(),
                wait,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
                return
            except TimeoutError:
                pass
            try:
                await self.fire_once()
            except Exception as exc:
                LOGGER.error(
                    "[EOD] fire: top_level_error — type=%s msg=%s",
                    type(exc).__name__,
                    exc,
                )
            # Avoid a tight loop if next_trigger_at returns now+0.
            await asyncio.sleep(1.0)


def _build_market_exit(direction: Direction, qty: int) -> Order:
    o = Order()
    o.action = "SELL" if direction is Direction.LONG else "BUY"
    o.totalQuantity = qty
    o.orderType = "MKT"
    o.tif = "DAY"
    o.transmit = True
    return o
