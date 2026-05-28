"""Async wrapper around ib_async.IB for TradeFlow.

Phase 1 PR 2 scope: connect/disconnect lifecycle and read-only state probes.
PR #10 additively layers order placement (place_order / cancel_order) and the
bar subscription helper. Existing method signatures are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ib_async import IB, BarDataList, Contract, Order, PortfolioItem, Position, Trade

LOGGER = logging.getLogger(__name__)

BarCallback = Callable[[dict], Awaitable[None]] | Callable[[dict], None]

# Transient exceptions worth retrying. ConnectionError is the parent of
# ConnectionRefusedError / ConnectionResetError / BrokenPipeError, which is
# what ib_async surfaces when the gateway socket closes mid-handshake or
# during a scheduled restart.
_TRANSIENT_CONNECT_EXC: tuple[type[BaseException], ...] = (
    TimeoutError,
    socket.gaierror,
    ConnectionError,
)

# Farm-flap auto-resubscribe (§0.5.181). The IBKR market-data farm drops
# several times a day with a 2103 (usfarm broken) + 2105 (ushmds broken) +
# 10182 (failed to request live updates) trio, recovers ~1s later with
# 2104/2106, but the keepUpToDate bar subscription dies with the 10182 and is
# never re-armed — the gateway socket stays UP throughout, so the orchestrator's
# socket-level reconnect (PR-A) does not fire. We watch ib.errorEvent for the
# trio and re-invoke the subscribe path after a short debounce.
_FARM_FLAP_TRIO_CODES: tuple[int, ...] = (2103, 2105, 10182)
# All three codes must land within this window to count as one flap.
_FARM_FLAP_WINDOW_SEC = 5.0
# Wait this long after the trio so the 2104/2106 recovery lands before we
# re-request live updates into a still-broken farm.
_FARM_FLAP_DEBOUNCE_SEC = 3.0
# Skip a resubscribe if the previous one fired this recently — collapses
# rapid-fire flaps to a single re-arm.
_FARM_FLAP_RESUB_GUARD_SEC = 30.0


class BrokerExtendedOutageError(Exception):
    """Raised when :func:`connect_with_resilience` exhausts ``max_attempts``.

    The orchestrator treats this as the signal to clean-exit so docker can
    recreate the container as a last resort. Carries ``attempts`` and
    ``elapsed_sec`` for the shutdown log line.
    """

    def __init__(self, attempts: int, elapsed_sec: float, last_exc: BaseException) -> None:
        super().__init__(
            f"broker connect exhausted after {attempts} attempts in "
            f"{elapsed_sec:.1f}s — last error: {type(last_exc).__name__}: {last_exc}"
        )
        self.attempts = attempts
        self.elapsed_sec = elapsed_sec
        self.last_exc = last_exc


async def connect_with_resilience(
    host: str,
    port: int,
    client_id: int,
    *,
    ib: IB | None = None,
    max_attempts: int = 30,
    backoff_initial_sec: float = 2.0,
    backoff_max_sec: float = 30.0,
    backoff_factor: float = 1.5,
    jitter_pct: float = 0.2,
    connect_timeout_sec: float = 20.0,
) -> IB:
    """Connect to IB Gateway with DNS-aware exponential backoff.

    Retries on socket.gaierror (DNS), TimeoutError, ConnectionRefusedError,
    ConnectionResetError, and the broader ConnectionError family that
    ib_async surfaces when the gateway socket closes. Other exceptions
    propagate unchanged on the first attempt.

    Raises BrokerExtendedOutageError if max_attempts is exhausted.
    """
    if ib is None:
        ib = IB()
    started = time.monotonic()
    backoff = backoff_initial_sec
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await ib.connectAsync(host, port, clientId=client_id, timeout=connect_timeout_sec)
        except _TRANSIENT_CONNECT_EXC as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            jitter = 1.0 + random.uniform(-jitter_pct, jitter_pct)
            sleep_for = min(backoff * jitter, backoff_max_sec)
            LOGGER.warning(
                "[CONN] reconnect attempt %d/%d backoff=%.1fs reason=%s — %s",
                attempt,
                max_attempts,
                sleep_for,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * backoff_factor, backoff_max_sec)
            continue
        elapsed = time.monotonic() - started
        server_version: Any = "unknown"
        try:
            if ib.isConnected():
                server_version = ib.client.serverVersion()
        except Exception:
            pass
        LOGGER.info(
            "[CONN] connected — server_version=%s client_id=%s elapsed=%.1fs attempts=%d",
            server_version,
            client_id,
            elapsed,
            attempt,
        )
        return ib
    elapsed = time.monotonic() - started
    assert last_exc is not None
    LOGGER.error(
        "[CONN] extended_outage — attempts=%d elapsed=%.1fs last=%s",
        max_attempts,
        elapsed,
        type(last_exc).__name__,
    )
    raise BrokerExtendedOutageError(max_attempts, elapsed, last_exc)


class IBClient:
    """Thin async wrapper around ``ib_async.IB``.

    Caller owns the connect/disconnect lifecycle. The ``ib_factory`` parameter
    exists so tests inject a mock IB instance at the wrapper boundary, instead
    of patching the raw library chain.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        ib_factory: Callable[[], IB] = IB,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib: IB = ib_factory()
        # Farm-flap auto-resubscribe state (§0.5.181). Armed lazily via
        # arm_farm_flap_watch() after the first subscribe_bars; left dormant
        # (resubscribe callback None) until then.
        self._flap_trio_seen: dict[int, float] = {}
        self._last_resubscribe_monotonic: float | None = None
        self._farm_flap_loop: asyncio.AbstractEventLoop | None = None
        self._farm_flap_resubscribe: Callable[[], Awaitable[None]] | None = None

    async def connect(self, timeout: float = 10.0) -> None:
        """Connect to IB Gateway. Raises whatever ib_async raises on failure."""
        LOGGER.info(
            "[ib_client] connect attempt — host=%s port=%s client_id=%s",
            self._host,
            self._port,
            self._client_id,
        )
        await self._ib.connectAsync(
            self._host, self._port, clientId=self._client_id, timeout=timeout
        )
        server_version = self._ib.client.serverVersion() if self._ib.isConnected() else "unknown"
        LOGGER.info("[ib_client] connected — server_version=%s", server_version)

    async def connect_with_resilience(
        self,
        *,
        max_attempts: int = 30,
        backoff_initial_sec: float = 2.0,
        backoff_max_sec: float = 30.0,
        backoff_factor: float = 1.5,
        jitter_pct: float = 0.2,
        connect_timeout_sec: float = 20.0,
    ) -> None:
        """Retry-aware connect against the wrapped IB instance.

        Reuses the existing ``self._ib`` so listeners / event subscriptions
        wired previously on the instance survive a reconnect. The wrapped
        socket is replaced on a successful ``connectAsync``.
        """
        await connect_with_resilience(
            self._host,
            self._port,
            self._client_id,
            ib=self._ib,
            max_attempts=max_attempts,
            backoff_initial_sec=backoff_initial_sec,
            backoff_max_sec=backoff_max_sec,
            backoff_factor=backoff_factor,
            jitter_pct=jitter_pct,
            connect_timeout_sec=connect_timeout_sec,
        )

    def disconnect(self) -> None:
        """Disconnect from IB Gateway. No-op when already disconnected."""
        if self._ib.isConnected():
            LOGGER.info("[ib_client] disconnect — host=%s port=%s", self._host, self._port)
            self._ib.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._ib.isConnected()

    async def get_positions(self) -> list[Position]:
        """Return current positions from the cached snapshot.

        Note: standing rule §0.5.T3 prefers ``portfolio()`` for runtime reconcile
        because it carries marketPrice / unrealizedPnL. Use ``get_portfolio()``
        unless you specifically need the lighter ``Position`` shape.
        """
        if not self.is_connected:
            raise RuntimeError("not connected — call connect() first")
        positions = self._ib.positions()
        LOGGER.info("[ib_client] positions — count=%s", len(positions))
        return positions

    async def get_portfolio(self) -> list[PortfolioItem]:
        """Return current portfolio items (positions + marketPrice + unrealizedPnL).

        Canonical reconcile path per §0.5.T3.
        """
        if not self.is_connected:
            raise RuntimeError("not connected — call connect() first")
        items = self._ib.portfolio()
        LOGGER.info("[ib_client] portfolio — count=%s", len(items))
        return items

    async def get_open_trades(self) -> list[Trade]:
        """Return currently open trades (orders not yet filled or cancelled)."""
        if not self.is_connected:
            raise RuntimeError("not connected — call connect() first")
        trades = self._ib.openTrades()
        LOGGER.info("[ib_client] open_trades — count=%s", len(trades))
        return trades

    async def get_account_summary(self, account: str = "") -> dict[str, float]:
        """Return NetLiquidation / AvailableFunds / BuyingPower keyed by tag."""
        if not self.is_connected:
            raise RuntimeError("not connected — call connect() first")
        items = await self._ib.accountSummaryAsync(account)
        wanted = {"NetLiquidation", "AvailableFunds", "BuyingPower"}
        out: dict[str, float] = {}
        for item in items:
            tag = getattr(item, "tag", None)
            if tag in wanted:
                try:
                    out[tag] = float(getattr(item, "value", "") or 0.0)
                except (TypeError, ValueError):
                    LOGGER.warning(
                        "[ib_client] account_summary: parse_failed — tag=%s value=%s",
                        tag,
                        getattr(item, "value", None),
                    )
        return out

    # ---------------------------------------------------------- order placement
    # PR #10 additions. ``IB.placeOrder`` and ``IB.cancelOrder`` are sync in
    # ib_async; we wrap them in async methods so callers can ``await`` uniformly
    # and the test suite can mock at this boundary instead of the raw IB.

    async def place_order(self, contract: Contract, order: Order) -> Trade:
        """Submit ``order`` for ``contract``. Returns the ``Trade`` immediately;
        fills arrive via the ``execDetailsEvent`` / ``fillEvent`` callbacks the
        orchestrator wires after connect().
        """
        if not self.is_connected:
            raise RuntimeError("not connected — call connect() first")
        trade = self._ib.placeOrder(contract, order)
        LOGGER.info(
            "[ib_client] place_order — symbol=%s action=%s qty=%s type=%s order_id=%s",
            getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "?"),
            order.action,
            order.totalQuantity,
            order.orderType,
            getattr(trade.order, "orderId", "?"),
        )
        return trade

    async def cancel_order(self, order: Order) -> None:
        """Cancel a working ``order``. Idempotent broker-side."""
        if not self.is_connected:
            raise RuntimeError("not connected — call connect() first")
        order_id = getattr(order, "orderId", None)
        LOGGER.info("[ib_client] cancel_order — order_id=%s", order_id)
        self._ib.cancelOrder(order)

    async def subscribe_bars(
        self,
        contract: Contract,
        *,
        bar_size: str = "1 min",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        duration: str = "1 D",
        on_new_bar: BarCallback | None = None,
    ) -> BarDataList:
        """Subscribe to live bars via reqHistoricalDataAsync(keepUpToDate=True).

        ``on_new_bar`` (if provided) is invoked for each new bar with a dict
        ``{time, open, high, low, close, volume}``. Sync callbacks are
        permitted; async callbacks are scheduled as tasks. Returns the
        ``BarDataList`` so the caller can attach additional callbacks if needed.
        """
        if not self.is_connected:
            raise RuntimeError("not connected — call connect() first")
        bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=2,
            keepUpToDate=True,
        )
        LOGGER.info(
            "[ib_client] subscribe_bars — symbol=%s bar_size=%s seeded=%s",
            getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "?"),
            bar_size,
            len(bars),
        )
        if on_new_bar is not None:
            _wire_bar_callback(bars, on_new_bar)
        return bars

    # ------------------------------------------------- farm-flap auto-resubscribe
    # §0.5.181 — re-arm the keepUpToDate bar subscription after the IBKR
    # market-data farm flaps. Detection lives here (it owns the raw ``ib``
    # errorEvent); the resubscribe itself re-invokes the orchestrator's existing
    # subscribe callable so the resolved front-month contract + args are reused.

    def arm_farm_flap_watch(
        self,
        loop: asyncio.AbstractEventLoop,
        resubscribe: Callable[[], Awaitable[None]],
    ) -> None:
        """Watch ``ib.errorEvent`` for the 2103/2105/10182 farm-flap trio.

        ``resubscribe`` is an idempotent async callable (the orchestrator's
        ``_start_bar_subscription``) that re-requests live bars with the
        retained contract + args. Wired once, after the initial subscribe.
        """
        self._farm_flap_loop = loop
        self._farm_flap_resubscribe = resubscribe
        self._ib.errorEvent += self._on_ib_error_farm_flap
        LOGGER.info(
            "[ib_client] farm_flap_watch armed — trio=%s window=%.0fs debounce=%.0fs guard=%.0fs",
            _FARM_FLAP_TRIO_CODES,
            _FARM_FLAP_WINDOW_SEC,
            _FARM_FLAP_DEBOUNCE_SEC,
            _FARM_FLAP_RESUB_GUARD_SEC,
        )

    def _on_ib_error_farm_flap(
        self,
        req_id: int,
        error_code: int,
        error_string: str,
        contract: Contract | None = None,
    ) -> None:
        """``ib.errorEvent`` handler. Completes the trio, then claims+schedules.

        Signature mirrors ib_async's ``errorEvent(reqId, errorCode, errorString,
        contract)`` (dispatched positionally). Tracks the partial 2103/2105/10182
        sequence in a short rolling window. On a completed trio it claims the
        idempotency slot synchronously (so rapid-fire flaps collapse to one
        resubscribe without racing on the debounce) and schedules the re-arm.
        """
        if error_code not in _FARM_FLAP_TRIO_CODES:
            return
        now = time.monotonic()
        # Drop partials older than the window so a stale 2103 + a fresh 2105
        # cannot falsely complete a trio.
        self._flap_trio_seen = {
            code: ts
            for code, ts in self._flap_trio_seen.items()
            if now - ts <= _FARM_FLAP_WINDOW_SEC
        }
        self._flap_trio_seen[error_code] = now
        if not all(code in self._flap_trio_seen for code in _FARM_FLAP_TRIO_CODES):
            return
        # Completed trio — reset the partial tracker for the next flap.
        self._flap_trio_seen.clear()
        last = self._last_resubscribe_monotonic
        if last is not None and (now - last) < _FARM_FLAP_RESUB_GUARD_SEC:
            LOGGER.info(
                "[ib_client] farm_flap resubscribe skipped — last resub %.1fs ago < %.0fs guard",
                now - last,
                _FARM_FLAP_RESUB_GUARD_SEC,
            )
            return
        # Claim the slot now (synchronous) so a second trio within the guard
        # window is deduped even before this resubscribe completes.
        self._last_resubscribe_monotonic = now
        LOGGER.warning(
            "[ib_client] farm_flap detected — trio 2103/2105/10182 within %.0fs; "
            "resubscribing after %.0fs debounce",
            _FARM_FLAP_WINDOW_SEC,
            _FARM_FLAP_DEBOUNCE_SEC,
        )
        self._schedule_farm_flap_resubscribe(detected_monotonic=now)

    def _schedule_farm_flap_resubscribe(self, *, detected_monotonic: float) -> None:
        loop = self._farm_flap_loop
        if loop is None or self._farm_flap_resubscribe is None:
            return
        loop.create_task(self._farm_flap_resubscribe_after_debounce(detected_monotonic))

    async def _farm_flap_resubscribe_after_debounce(self, detected_monotonic: float) -> None:
        await asyncio.sleep(_FARM_FLAP_DEBOUNCE_SEC)
        resubscribe = self._farm_flap_resubscribe
        if resubscribe is None:
            return
        try:
            await resubscribe()
        except Exception as exc:
            LOGGER.warning(
                "[ib_client] farm_flap resubscribe failed — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
            return
        elapsed = time.monotonic() - detected_monotonic
        LOGGER.info(
            "[ORCH] bar_subscription auto-resubscribed after farm-flap — elapsed_sec=%.1f",
            elapsed,
        )
        LOGGER.info(
            "[ALERT] bar_sub_resubscribed_after_farm_flap: elapsed_sec=%.1f",
            elapsed,
        )


def _wire_bar_callback(bars: BarDataList, on_new_bar: BarCallback) -> None:
    """Attach ``on_new_bar`` to ``bars.updateEvent``, supporting both sync and async forms."""
    import asyncio
    import inspect

    is_coro = inspect.iscoroutinefunction(on_new_bar)

    def _adapter(bars_obj: BarDataList, has_new_bar: bool) -> None:
        if not has_new_bar or not bars_obj:
            return
        last = bars_obj[-1]
        LOGGER.info(
            "[BAR] %s: new — close=%.2f ts=%s",
            getattr(getattr(bars_obj, "contract", None), "localSymbol", None) or "?",
            last.close,
            last.date,
        )
        payload: dict[str, Any] = {
            "time": getattr(last, "date", None),
            "open": getattr(last, "open", None),
            "high": getattr(last, "high", None),
            "low": getattr(last, "low", None),
            "close": getattr(last, "close", None),
            "volume": getattr(last, "volume", None),
        }
        result = on_new_bar(payload)
        if is_coro and result is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(result)
            except RuntimeError:
                # No running loop (rare; shouldn't happen under the orchestrator).
                LOGGER.warning("[ib_client] on_new_bar coroutine discarded — no loop")

    bars.updateEvent += _adapter
