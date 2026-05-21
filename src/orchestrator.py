"""Long-running orchestrator for TradeFlow.

Owns the lifetime of an ``IBClient`` and a ``SupabaseClient``, runs a periodic
IBKR healthcheck, and exits cleanly on SIGTERM. PR #10 adds the first end-to-end
trading path: bar subscription → strategy → bracket placement → EOD force-close.

§0.5.T1 — single IBKR client id (orchestrator uses ``IBKR_CLIENT_ID``).
§0.5.T4 — kill switch is NOT in this PR; clean exit code is 0.
§0.5.T5 — bracket pair is parent+TP; protective STP is placed by the router
inside the parent fillEvent handler before the lifecycle reaches ACTIVE.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import UTC, datetime
from types import FrameType
from typing import Any

from ib_async import Contract, Future

from config.instruments import MNQ
from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.execution.force_close import EodForceClose
from src.execution.router import OrderRouter
from src.state_machine import Lifecycle, State, StateMachine
from src.strategy import STRATEGY_NAME, Signal, Sma100BounceStrategy

LOGGER = logging.getLogger(__name__)


class Orchestrator:
    """Owns IB + DB lifetimes, runs a periodic healthcheck, exits on SIGTERM."""

    def __init__(
        self,
        ib: IBClient,
        db: SupabaseClient,
        *,
        paper_account: str,
        healthcheck_interval: float = 60.0,
        state_machine: StateMachine | None = None,
        instrument: str = "MNQM6",
        bar_size: str = "1 min",
        enable_strategy: bool = True,
    ) -> None:
        self._ib = ib
        self._db = db
        self._paper_account = paper_account
        self._healthcheck_interval = healthcheck_interval
        self._sm: StateMachine = state_machine or StateMachine(db=db, ib=ib)
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started_at: float | None = None
        self._instrument = instrument
        self._bar_size = bar_size
        self._enable_strategy = enable_strategy
        self._contract: Contract = _build_contract(instrument)
        self._strategy = Sma100BounceStrategy(instrument)
        self._router = OrderRouter(ib=ib, sm=self._sm, strategy_name=STRATEGY_NAME)
        self._eod = EodForceClose(self._router, self._sm, contract=self._contract)
        self._background_tasks: list[asyncio.Task[Any]] = []
        self._bars: Any = None

    async def run(self) -> int:
        """Start orchestrator, loop on healthcheck, return process exit code."""
        self._stop_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._started_at = time.monotonic()
        self._install_signal_handlers()

        exit_code = 0
        try:
            await self._startup()
            while not self._stop_event.is_set():
                await self._healthcheck_once()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._healthcheck_interval,
                    )
                except TimeoutError:
                    continue
        except Exception as exc:
            LOGGER.error(
                "[ORCH] shutdown: exception — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
            exit_code = 1
        finally:
            await self._shutdown(exit_code)

        return exit_code

    async def _startup(self) -> None:
        import os

        pid = os.getpid()
        LOGGER.info(
            "[ORCH] startup: begin — pid=%s healthcheck_interval=%s",
            pid,
            self._healthcheck_interval,
        )
        LOGGER.info(
            "[ORCH] startup: ib_connecting — host=%s port=%s client_id=%s",
            getattr(self._ib, "_host", "?"),
            getattr(self._ib, "_port", "?"),
            getattr(self._ib, "_client_id", "?"),
        )
        await self._ib.connect()
        server_version = self._safe_server_version()
        LOGGER.info("[ORCH] startup: ib_connected — server_version=%s", server_version)

        summary = await self._ib._ib.accountSummaryAsync(self._paper_account)
        net_liq = self._extract_net_liquidation(summary)
        if net_liq is None:
            raise RuntimeError(
                f"NetLiquidation row missing from accountSummaryAsync for account "
                f"{self._account_prefix(self._paper_account)} — refusing to start"
            )
        LOGGER.info(
            "[ORCH] startup: account_bound — prefix=%s net_liq=%s",
            self._account_prefix(self._paper_account),
            net_liq,
        )
        await self._recover_state()
        if self._enable_strategy:
            self._wire_fill_event()
            await self._start_bar_subscription()
            self._launch_background_tasks()

    async def _recover_state(self) -> None:
        """Load non-CLOSED lifecycles and reconcile with broker reality.

        One-shot at startup. Uses read-only IB API calls (positions, openTrades)
        — safe under IBC Read-Only API mode (§0.5.123). Applies any transitions
        that the state machine deems necessary based on broker drift, then
        re-registers each lifecycle with the router so subsequent fillEvents
        route correctly.
        """
        lifecycles = await self._sm.load_non_closed()
        LOGGER.info("[ORCH] state: recovery_loaded — count=%s", len(lifecycles))
        transitions_applied = 0
        for lc in lifecycles:
            target = await self._sm.boot_time_broker_check(lc)
            if target is not None:
                field_updates = await self._broker_field_updates_for(lc, target)
                lc = await self._sm.transition(
                    lc,
                    target,
                    reason="boot_time_broker_check",
                    payload={"prior_state": lc.state},
                    **field_updates,
                )
                LOGGER.info(
                    "[ORCH] state: recovery_transition — id=%s to=%s",
                    lc.lifecycle_id,
                    target.value,
                )
                transitions_applied += 1
            if lc.state != State.CLOSED.value:
                self._router.register_recovered(lc, self._contract)
        LOGGER.info(
            "[ORCH] state: recovery_complete — transitions_applied=%s",
            transitions_applied,
        )

    def _wire_fill_event(self) -> None:
        """Register :meth:`OrderRouter.on_fill` as the global execDetails handler.

        Uses the synchronous ``call_soon_threadsafe`` bridge so the async router
        method is scheduled on the orchestrator's loop, not the IB thread.
        """
        raw_ib = getattr(self._ib, "_ib", None)
        if raw_ib is None:
            return
        event = getattr(raw_ib, "execDetailsEvent", None)
        if event is None:
            return
        try:
            event += self._on_exec_details
            LOGGER.info("[ORCH] startup: fill_event_wired")
        except Exception as exc:
            LOGGER.warning(
                "[ORCH] startup: fill_event_wire_failed — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )

    def _on_exec_details(self, trade: Any, fill: Any) -> None:
        if self._loop is None:
            return
        coro = self._router.on_fill(trade, fill)
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _start_bar_subscription(self) -> None:
        try:
            self._bars = await self._ib.subscribe_bars(
                self._contract,
                bar_size=self._bar_size,
                on_new_bar=self._on_new_bar,
            )
            LOGGER.info(
                "[STRAT] %s: bar_subscription started — bar_size=%s",
                self._instrument,
                self._bar_size,
            )
        except Exception as exc:
            LOGGER.warning(
                "[ORCH] startup: bar_subscription_failed — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )

    def _on_new_bar(self, bar: dict) -> None:
        try:
            signal_or_none = self._strategy.on_new_bar(bar)
        except Exception as exc:
            LOGGER.error(
                "[STRAT] %s: on_new_bar_error — type=%s msg=%s",
                self._instrument,
                type(exc).__name__,
                exc,
            )
            return
        if signal_or_none is None:
            return
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._handle_signal(signal_or_none), self._loop)

    async def _handle_signal(self, signal: Signal) -> None:
        try:
            await self._router.place_entry(signal, self._contract)
        except Exception as exc:
            LOGGER.error(
                "[EXEC] %s: place_entry_dispatch_failed — type=%s msg=%s",
                signal.instrument,
                type(exc).__name__,
                exc,
            )

    def _launch_background_tasks(self) -> None:
        assert self._stop_event is not None
        eod_task = asyncio.create_task(
            self._eod.run_until_stopped(self._stop_event),
            name="tradeflow-eod-force-close",
        )
        self._background_tasks.append(eod_task)
        LOGGER.info("[EOD] task_launched — name=tradeflow-eod-force-close")

    async def _broker_field_updates_for(
        self, lifecycle: Lifecycle, target: State
    ) -> dict[str, Any]:
        # Best-effort re-bind to broker reality on ENTERING → ACTIVE drift.
        # entry_filled_at uses wall-clock; reqExecutions probe for exact fill
        # timestamp lands in PR #10 reconciliation.
        if State(lifecycle.state) is State.ENTERING and target is State.ACTIVE:
            positions = await self._ib.get_positions()
            qty, avg_cost = _broker_qty_and_avg_cost(positions, lifecycle.symbol)
            return {
                "entry_qty": qty,
                "entry_price": avg_cost,
                "entry_filled_at": datetime.now(UTC).isoformat(),
            }
        return {}

    async def _healthcheck_once(self) -> None:
        ib_time = await self._ib._ib.reqCurrentTimeAsync()
        LOGGER.info("[ORCH] healthcheck: ok — ib_time=%s", self._format_ib_time(ib_time))

    async def _shutdown(self, exit_code: int) -> None:
        await self._cancel_background_tasks()
        try:
            self._ib.disconnect()
            LOGGER.info("[ORCH] shutdown: ib_disconnected")
        except Exception as exc:
            LOGGER.warning(
                "[ORCH] shutdown: ib_disconnect_error — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
        try:
            await self._db.close()
            LOGGER.info("[ORCH] shutdown: db_closed")
        except Exception as exc:
            LOGGER.warning(
                "[ORCH] shutdown: db_close_error — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
        duration = (
            f"{time.monotonic() - self._started_at:.1f}" if self._started_at is not None else "?"
        )
        LOGGER.info(
            "[ORCH] shutdown: done — exit_code=%s duration_sec=%s",
            exit_code,
            duration,
        )

    async def _cancel_background_tasks(self) -> None:
        if not self._background_tasks:
            return
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        for task in self._background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                LOGGER.warning(
                    "[ORCH] shutdown: background_task_error — name=%s type=%s msg=%s",
                    task.get_name(),
                    type(exc).__name__,
                    exc,
                )
        self._background_tasks.clear()

    def _install_signal_handlers(self) -> None:
        # asyncio.add_signal_handler does not work in Docker pid=1 / minimal
        # images. Use signal.signal + call_soon_threadsafe to set the event.
        try:
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)
        except ValueError:
            # signal.signal must be called from main thread; tests may bypass.
            LOGGER.debug("[ORCH] startup: signal_handlers_skipped — not_main_thread")

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        name = (
            signal.Signals(signum).name
            if signum in signal.Signals.__members__.values()
            else str(signum)
        )
        LOGGER.info("[ORCH] shutdown: signal_received — signal=%s", name)
        if self._stop_event is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._stop_event.set)

    def _safe_server_version(self) -> str:
        try:
            return str(self._ib._ib.client.serverVersion())
        except Exception:
            return "unknown"

    @staticmethod
    def _account_prefix(account: str) -> str:
        return account[:3] if account else "?"

    @staticmethod
    def _extract_net_liquidation(summary: list[Any]) -> str | None:
        for row in summary:
            tag = getattr(row, "tag", None)
            if tag == "NetLiquidation":
                return getattr(row, "value", None)
        return None

    @staticmethod
    def _format_ib_time(ib_time: Any) -> str:
        try:
            return ib_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        except AttributeError:
            return str(ib_time)


def _build_contract(instrument: str) -> Contract:
    """Build an ib_async ``Contract`` for ``instrument`` (e.g. ``"MNQM6"``).

    Resolution rule: a localSymbol that begins with ``MNQ`` is the front-month
    MNQ future. PR #11 will add contract-roll awareness; for PR #10 the
    instrument is supplied externally by the orchestrator's caller.
    """
    if instrument.startswith(MNQ.symbol):
        return Future(
            symbol=MNQ.symbol,
            exchange=MNQ.exchange,
            currency=MNQ.currency,
            localSymbol=instrument,
        )
    c = Contract()
    c.symbol = instrument
    c.secType = "FUT"
    c.exchange = MNQ.exchange
    c.currency = MNQ.currency
    return c


def _broker_qty_and_avg_cost(positions: list[Any], symbol: str) -> tuple[int, float]:
    """Return (qty, avg_cost) for the matching position; (0, 0.0) if missing.

    avgCost is coerced defensively — recovery must not crash on a mock or a
    None value; falls back to 0.0 and lets the next-tick reconciliation probe
    correct the price.
    """
    for pos in positions:
        contract = getattr(pos, "contract", None)
        if contract is None:
            continue
        local = getattr(contract, "localSymbol", None)
        base = getattr(contract, "symbol", None)
        if symbol not in {local, base}:
            continue
        try:
            qty = int(getattr(pos, "position", 0))
        except (TypeError, ValueError):
            qty = 0
        try:
            avg_cost = float(getattr(pos, "avgCost", 0.0))
        except (TypeError, ValueError):
            avg_cost = 0.0
        return qty, avg_cost
    return 0, 0.0
