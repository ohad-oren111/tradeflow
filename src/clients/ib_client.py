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

from ib_async import IB, BarDataList, Contract, Fill, Order, PortfolioItem, Position, Trade

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
# several times a day with some combination of 2103 (usfarm broken), 2105
# (ushmds broken), and 10182 (failed to request live updates), recovers ~1s
# later with 2104/2106, but the keepUpToDate bar subscription dies and is never
# re-armed — the gateway socket stays UP throughout, so the orchestrator's
# socket-level reconnect (PR-A) does not fire.
#
# W-S14.2 Track 3 (PR-R1 follow-up): detection is "ANY of these codes", not the
# full trio. The 04:03 UTC flap that left the feed blind for 37 min was
# 2105 + 10182 with NO 2103, so the all-of-trio guard never completed and the
# resubscribe never fired. Any single farm-broken/lost-updates code now arms the
# debounced resubscribe; the 30s idempotency guard collapses a multi-code flap
# to exactly one re-arm.
_FARM_FLAP_CODES: frozenset[int] = frozenset({2103, 2105, 10182})
# Wait this long after a flap code so the 2104/2106 recovery lands before we
# re-request live updates into a still-broken farm.
_FARM_FLAP_DEBOUNCE_SEC = 3.0
# Skip a resubscribe if the previous one fired this recently — collapses a
# multi-code flap (and rapid-fire flaps) to a single re-arm.
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
        self._last_resubscribe_monotonic: float | None = None
        self._farm_flap_loop: asyncio.AbstractEventLoop | None = None
        self._farm_flap_resubscribe: Callable[[], Awaitable[None]] | None = None
        # FEED-GAP FIX — the most recent live keepUpToDate BarDataList. Tracked so a
        # resubscribe can CANCEL it before re-requesting; re-issuing
        # reqHistoricalData(keepUpToDate=True) while the prior subscription is still
        # registered makes the gateway cancel the NEW query (Error 162, seeded=0) so
        # the live feed never resumes (the 2026-06-07 reopen: 5.5h dark — every
        # same-socket self-heal resubscribe was a no-op). Cleared on disconnect (the
        # handle is bound to the socket that dropped).
        self._active_bar_sub: BarDataList | None = None

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
        # FEED-GAP FIX — the bar subscription is bound to the socket that is going
        # away; drop the stale handle so a post-reconnect resubscribe does not try
        # to cancel a reqId the fresh socket never issued.
        self._active_bar_sub = None
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

    async def get_fills(self) -> list[Fill]:
        """Return the session's executions/fills (``IB.fills()``).

        W-S15.3 — the reconciler uses this to recover the ACTUAL execution price
        of a bracket leg when the event-driven router missed the fillEvent. Each
        ``Fill`` carries an ``execution`` (with ``orderId`` / ``shares`` /
        ``price`` / ``avgPrice``). ``IB.fills()`` is the in-session cache populated
        by ``execDetailsEvent``; it holds the current session's fills only, so it
        is empty after a reconnect that post-dates the fill — callers MUST fall
        back to the order price in that case (never raise).
        """
        if not self.is_connected:
            raise RuntimeError("not connected — call connect() first")
        fills = self._ib.fills()
        LOGGER.info("[ib_client] fills — count=%s", len(fills))
        return list(fills)

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

    async def cancel_order_by_id(self, order_id: int) -> bool:
        """Cancel a working order by id, looked up from live trades. Never raises.

        ``IB.cancelOrder`` reads ``clientId`` / ``permId`` off the order, so it must
        be handed a REAL ``Order`` — a bare ``orderId``-only stub raises
        ``AttributeError`` (the W-S15.1 bug). We find the live ``Trade`` whose
        ``order.orderId == order_id`` among ``IB.openTrades()`` (same client that
        placed the brackets) and cancel THAT order. If the id is not among live
        orders (already filled or cancelled) it is a logged no-op. Returns whether
        a cancel was issued, so callers can never break the fill/reconcile loop.
        """
        if not self.is_connected:
            LOGGER.warning("[ib_client] cancel_order_by_id skipped id=%s — not connected", order_id)
            return False
        try:
            for trade in self._ib.openTrades():
                order = getattr(trade, "order", None)
                if order is not None and getattr(order, "orderId", None) == order_id:
                    self._ib.cancelOrder(order)
                    LOGGER.info(
                        "[ib_client] cancel_order_by_id — order_id=%s clientId=%s",
                        order_id,
                        getattr(order, "clientId", None),
                    )
                    return True
            LOGGER.info(
                "[ib_client] cancel_order_by_id skipped id=%s — not live (already gone)",
                order_id,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — must never raise into fill/reconcile
            LOGGER.warning(
                "[ib_client] cancel_order_by_id error id=%s type=%s msg=%s",
                order_id,
                type(exc).__name__,
                exc,
            )
            return False

    async def find_open_order_by_id(self, order_id: int) -> Order | None:
        """Return the LIVE ``Order`` for ``order_id`` from ``IB.openTrades()``, or None.

        The returned object is the real ib_async ``Order`` (carrying clientId /
        permId / current ``auxPrice``), so the caller can read the broker's actual
        resting stop price (§0.5.98 broker-truth) and modify it in place by mutating
        the order and re-submitting via :meth:`place_order` (ib_async treats a
        re-``placeOrder`` of the same orderId as an order-modify — one resting stop,
        no naked window). Returns None when the id is not among live orders (filled
        / cancelled) or on any error; never raises into the bar loop.
        """
        if not self.is_connected:
            LOGGER.warning(
                "[ib_client] find_open_order_by_id skipped id=%s — not connected", order_id
            )
            return None
        try:
            for trade in self._ib.openTrades():
                order = getattr(trade, "order", None)
                if order is not None and getattr(order, "orderId", None) == order_id:
                    return order
            return None
        except Exception as exc:  # noqa: BLE001 — must never raise into the bar loop
            LOGGER.warning(
                "[ib_client] find_open_order_by_id error id=%s type=%s msg=%s",
                order_id,
                type(exc).__name__,
                exc,
            )
            return None

    async def get_historical_bars(
        self,
        contract: Contract,
        *,
        bar_size: str = "1 min",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        # "5 D" (not "1 D"): right after the 18:00 ET Globex reopen "1 D" returns
        # only the current session's bars (~99 one-min bars ~98 min in), one short
        # of the 100 the SMA100 warmup seed needs. "5 D" spans prior session(s)
        # INCLUDING across the weekend, so a Sunday-evening restart right after the
        # reopen (foreseeable under our unstable API) still clears 100 — where "2 D"
        # would span only Saturday (no bars) + minutes of Sunday. "5 D" is the safe
        # DEFAULT: a much longer 1-min-bar request risks an IB duration/pacing
        # rejection. The regime-armable boot seed passes an explicit larger window
        # ("10 D") and falls back to this "5 D" on rejection (orchestrator
        # _fetch_warmup_seed); the validate_seed >=100 fail-safe rejects any short
        # result regardless of duration.
        duration: str = "5 D",
    ) -> list[Any]:
        """One-shot historical bars (``keepUpToDate=False``) — for warmup backfill.

        Distinct from :meth:`subscribe_bars` (a live ``keepUpToDate`` subscription):
        this is a plain snapshot the caller computes a shadow SMA from. Raises if
        not connected / on a request error — the caller (warmup-shadow seed) wraps
        it never-raise so a fetch failure can't block boot or trading.
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
            keepUpToDate=False,
        )
        LOGGER.info(
            "[ib_client] get_historical_bars — symbol=%s bar_size=%s bars=%s",
            getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "?"),
            bar_size,
            len(bars),
        )
        return list(bars)

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
        # FEED-GAP FIX — cancel any prior keepUpToDate subscription FIRST so this
        # re-request replaces the wedged stream instead of colliding with it. A
        # duplicate keepUpToDate request for the same contract makes the gateway
        # cancel the new query (Error 162, seeded=0) and the live feed never
        # resumes — the silent-feed-leak class where every same-socket self-heal
        # resubscribe is a no-op (see the 2026-06-07 reopen, 5.5h dark).
        self._cancel_active_bar_sub(reason="resubscribe")
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
        self._active_bar_sub = bars
        LOGGER.info(
            "[ib_client] subscribe_bars — symbol=%s bar_size=%s seeded=%s",
            getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "?"),
            bar_size,
            len(bars),
        )
        if on_new_bar is not None:
            _wire_bar_callback(bars, on_new_bar)
        return bars

    def _cancel_active_bar_sub(self, *, reason: str) -> None:
        """Cancel the tracked keepUpToDate bar subscription, if any.

        FEED-GAP FIX — must run before every resubscribe so a fresh
        ``reqHistoricalData(keepUpToDate=True)`` does not layer on top of a
        still-registered (wedged) subscription and get cancelled by the gateway
        (Error 162, seeded=0). Never raises — a failed cancel must not block the
        resubscribe that follows it.
        """
        prev = self._active_bar_sub
        if prev is None:
            return
        self._active_bar_sub = None
        try:
            self._ib.cancelHistoricalData(prev)
            LOGGER.info(
                "[ib_client] bar_sub state: active -> cancelled — reason=%s "
                "(freeing slot before resubscribe)",
                reason,
            )
        except Exception as exc:  # noqa: BLE001 — cancel must never block a resubscribe
            LOGGER.info(
                "[ib_client] bar_sub prior-cancel skipped — reason=%s type=%s msg=%s",
                reason,
                type(exc).__name__,
                exc,
            )

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
            "[ib_client] farm_flap_watch armed — codes=%s (any) debounce=%.0fs guard=%.0fs",
            sorted(_FARM_FLAP_CODES),
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
        """``ib.errorEvent`` handler — ANY farm-flap code arms the resubscribe.

        Signature mirrors ib_async's ``errorEvent(reqId, errorCode, errorString,
        contract)`` (dispatched positionally). W-S14.2 Track 3: any single code
        in ``_FARM_FLAP_CODES`` triggers the debounced resubscribe (the prior
        all-of-trio guard missed real flaps that lacked a 2103). The 30s
        idempotency guard, claimed synchronously, collapses the remaining codes
        of a multi-code flap — and rapid-fire flaps — to exactly one resubscribe.
        """
        if error_code not in _FARM_FLAP_CODES:
            return
        now = time.monotonic()
        last = self._last_resubscribe_monotonic
        if last is not None and (now - last) < _FARM_FLAP_RESUB_GUARD_SEC:
            LOGGER.info(
                "[ib_client] farm_flap resubscribe skipped — code=%s, last resub %.1fs ago "
                "< %.0fs guard",
                error_code,
                now - last,
                _FARM_FLAP_RESUB_GUARD_SEC,
            )
            return
        # Claim the slot now (synchronous) so the other codes of the same flap
        # (and a second flap within the guard) are deduped before this completes.
        self._last_resubscribe_monotonic = now
        LOGGER.warning(
            "[ib_client] farm_flap detected — code=%s; resubscribing after %.0fs debounce",
            error_code,
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
        # Log-only (no [ALERT]) — a routine farm-flap auto-resubscribe is not an
        # operator-facing event. If it fails to restore bars the orchestrator's
        # feed-episode watchdog opens an episode + alerts. The host watchdog still
        # parses this line as a flap canary for the daily report.
        LOGGER.info(
            "[FEED] bar_sub_resubscribed_after_farm_flap: elapsed_sec=%.1f",
            elapsed,
        )


def _wire_bar_callback(bars: BarDataList, on_new_bar: BarCallback) -> None:
    """Attach ``on_new_bar`` to ``bars.updateEvent``, delivering the just-CLOSED
    SETTLED bar (§7.4 fix). Supports both sync and async callback forms.

    On ``has_new_bar`` ib_async has just APPENDED the newly-opened bar
    (``wrapper.historicalDataUpdate``: a new-timestamp bar does ``bars.append(bar)``
    then emits ``updateEvent(bars, True)``). So at that instant ``bars_obj[-1]`` is
    the just-OPENED forming bar — opening tick only — and ``bars_obj[-2]`` is the
    just-CLOSED SETTLED bar. We deliver ``bars_obj[-2]``: the correct completed-candle
    OHLC for BOTH the entry gate and the trailing-exit ratchet. Reading ``[-1]`` made
    TF evaluate (and ratchet on) opening-tick noise — the STABILIZE-1 root cause.

    Monotonic guard: ``last_date`` seeds from the initial historical fill's last bar
    (already in the strategy's warmup seed) so the seed/live boundary bar is never
    double-processed, and any same-/older-timestamp re-fire is dropped. Every
    ``BarData.date`` is a parsed ``datetime`` (``wrapper.historicalData`` and
    ``historicalDataUpdate`` both call ``parseIBDatetime``), so the compare is safe.
    """
    import asyncio
    import inspect

    is_coro = inspect.iscoroutinefunction(on_new_bar)
    state: dict[str, Any] = {"last_date": bars[-1].date if len(bars) else None}

    def _adapter(bars_obj: BarDataList, has_new_bar: bool) -> None:
        # Need >=2 bars: [-2] is the settled bar, [-1] the just-opened forming one.
        if not has_new_bar or len(bars_obj) < 2:
            return
        settled = bars_obj[-2]
        last_date = state["last_date"]
        if last_date is not None and settled.date <= last_date:
            return  # seed-boundary bar already seeded, or an out-of-order re-fire
        state["last_date"] = settled.date
        LOGGER.info(
            "[BAR] %s: settled — close=%.2f ts=%s",
            getattr(getattr(bars_obj, "contract", None), "localSymbol", None) or "?",
            settled.close,
            settled.date,
        )
        payload: dict[str, Any] = {
            "time": getattr(settled, "date", None),
            "open": getattr(settled, "open", None),
            "high": getattr(settled, "high", None),
            "low": getattr(settled, "low", None),
            "close": getattr(settled, "close", None),
            "volume": getattr(settled, "volume", None),
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
