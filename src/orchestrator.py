"""Long-running orchestrator for TradeFlow.

Owns the lifetime of an ``IBClient`` and a ``SupabaseClient``, runs a periodic
IBKR healthcheck, and exits cleanly on SIGTERM. PR #8 is wiring only — no
trading logic, no order placement, no strategy. The state machine lands in PR #9.

§0.5.T1 — single IBKR client id (orchestrator uses ``IBKR_CLIENT_ID``).
§0.5.T4 — kill switch is NOT in this PR; clean exit code is 0.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from types import FrameType
from typing import Any

from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient

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
    ) -> None:
        self._ib = ib
        self._db = db
        self._paper_account = paper_account
        self._healthcheck_interval = healthcheck_interval
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started_at: float | None = None

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

    async def _healthcheck_once(self) -> None:
        ib_time = await self._ib._ib.reqCurrentTimeAsync()
        LOGGER.info("[ORCH] healthcheck: ok — ib_time=%s", self._format_ib_time(ib_time))

    async def _shutdown(self, exit_code: int) -> None:
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
