"""Telegram alerter + command bot for TradeFlow operator notification.

Outbound: registers as a ``logging.Handler`` filtered for ``[ALERT]`` records,
parses the event_type out of each message, posts to the operator chat via
``httpx``. Bounded queue drops oldest if Telegram is unreachable >5min so the
trading code never blocks on Telegram availability.

Inbound: long-polls ``getUpdates``, dispatches ``/status``, ``/halt``, and
``/ack`` against an :class:`OperatorCoordinator` Protocol that ``Orchestrator``
implements. Single operator chat_id whitelist from env; foreign chat_ids get a
one-line "Unauthorized." reply and a WARNING log.

PR #14 scope. NOT in this PR: ``/eod``, ``/positions``, ``/healthcheck``,
multi-operator support, webhooks, persistent dedup across restart, per-event
filtering by operator preference. See PR #15+ backlog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from src.orchestrator import ExitResult, FlattenResult

LOGGER = logging.getLogger(__name__)

ALERT_PREFIX = "[ALERT]"
ALERT_QUEUE_MAX = 500
DEDUP_WINDOW_SEC = 30.0
TELEGRAM_API = "https://api.telegram.org"
LONG_POLL_TIMEOUT_SEC = 30
RETRY_BACKOFF_BASE_SEC = 2.0
RETRY_BACKOFF_MAX_SEC = 60.0
# PR #16 — staged operator action expires after this many seconds without /confirm.
PENDING_ACTION_TTL_SEC = 60.0


@dataclass
class PendingAction:
    """A /flatten or /exit awaiting operator /confirm. Single-slot, in-memory."""

    kind: str  # "flatten" | "exit"
    symbol: str | None  # None for flatten; uppercase symbol for exit
    requested_at: datetime  # tz-aware UTC


class OperatorCoordinator(Protocol):
    """Subset of :class:`Orchestrator` the command handlers need.

    Defined as a Protocol so ``comms.telegram`` does not import
    ``src.orchestrator`` (would create a cycle). Tests pass a ``MagicMock``
    implementing these five methods.
    """

    def is_halted(self) -> bool: ...
    def halt_raised_at(self) -> datetime | None: ...
    def raise_halt(self, symbol: str | None = None) -> None: ...
    def clear_halt(self, reason: str = "") -> None: ...
    async def get_broker_status_summary(self) -> dict[str, Any]: ...
    async def insert_halt_ack(self, note: str) -> dict[str, Any]: ...

    # PR #16 — manual close commands.
    async def flatten_all(self) -> FlattenResult: ...
    async def exit_symbol(self, symbol: str) -> ExitResult: ...


class TelegramAlertHandler(logging.Handler):
    """``logging.Handler`` that filters for ``[ALERT]`` records and enqueues."""

    def __init__(self, queue: asyncio.Queue[str], loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(level=logging.INFO)
        self._queue = queue
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if ALERT_PREFIX not in msg:
            return
        # Loop closed mid-shutdown is a benign race; drop silently.
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._enqueue, msg)

    def _enqueue(self, msg: str) -> None:
        try:
            self._queue.put_nowait(msg)
            return
        except asyncio.QueueFull:
            LOGGER.warning("[telegram] alert_queue_full — dropping oldest")
        with contextlib.suppress(asyncio.QueueEmpty):
            self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(msg)


class TelegramAlerter:
    """Outbound alerter + inbound command bot.

    Lifecycle: instantiate in :meth:`Orchestrator.__init__`; call
    :meth:`install_handler` once after the event loop is up to register the
    logging handler; launch :meth:`alert_loop` + :meth:`command_loop` as
    background tasks in :meth:`Orchestrator._launch_background_tasks`.
    """

    def __init__(
        self,
        bot_token: str,
        operator_chat_id: int,
        coordinator: OperatorCoordinator,
        *,
        http_client: httpx.AsyncClient | None = None,
        queue_max: int = ALERT_QUEUE_MAX,
        dedup_window_sec: float = DEDUP_WINDOW_SEC,
    ) -> None:
        self._bot_token = bot_token
        self._operator_chat_id = operator_chat_id
        self._coordinator = coordinator
        self._http = http_client or httpx.AsyncClient(timeout=35.0)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_max)
        self._dedup_window_sec = dedup_window_sec
        self._recent: deque[tuple[str, float]] = deque(maxlen=200)
        self._update_offset = 0
        self._handler: TelegramAlertHandler | None = None
        # PR #16 — single-slot pending action awaiting /confirm. Lost on restart;
        # operator can re-issue. Subsequent /flatten or /exit overwrites freely.
        self._pending_action: PendingAction | None = None

    @property
    def queue(self) -> asyncio.Queue[str]:
        return self._queue

    def install_handler(self, loop: asyncio.AbstractEventLoop, root_logger: logging.Logger) -> None:
        """Register the alert handler on ``root_logger``. Idempotent."""
        if self._handler is not None:
            return
        self._handler = TelegramAlertHandler(self._queue, loop)
        root_logger.addHandler(self._handler)
        LOGGER.info("[telegram] handler_installed: queue_max=%d", self._queue.maxsize)

    # ----------------------------------------------------------------- dedup

    def _is_dup(self, alert_key: str, now: float) -> bool:
        """Return True if ``alert_key`` was emitted within ``dedup_window_sec``."""
        cutoff = now - self._dedup_window_sec
        while self._recent and self._recent[0][1] < cutoff:
            self._recent.popleft()
        for key, _ts in self._recent:
            if key == alert_key:
                return True
        self._recent.append((alert_key, now))
        return False

    @staticmethod
    def _parse_alert_key(msg: str) -> str | None:
        """Extract the event_type from a ``[ALERT] event_type: ...`` message."""
        m = re.search(r"\[ALERT\]\s+([a-z_]+):", msg)
        return m.group(1) if m else None

    # -------------------------------------------------------------- outbound

    async def _send(self, text: str) -> bool:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        try:
            r = await self._http.post(
                url,
                json={
                    "chat_id": self._operator_chat_id,
                    "text": text,
                },
            )
            if r.status_code == 200:
                return True
            LOGGER.warning("[telegram] send_failed: status=%d body=%s", r.status_code, r.text[:200])
            return False
        except Exception as exc:
            LOGGER.warning("[telegram] send_exception: %r", exc)
            return False

    async def _send_to(self, chat_id: int | None, text: str) -> None:
        if chat_id is None:
            return
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        # Best-effort reply to unauthorized chat_ids; failures are non-fatal.
        with contextlib.suppress(Exception):
            await self._http.post(url, json={"chat_id": chat_id, "text": text})

    @staticmethod
    def _format_alert(msg: str) -> str:
        body = msg.replace(f"{ALERT_PREFIX} ", "", 1)
        # PR #15 — plain text only; legacy Markdown italicizes `_word_` and eats
        # underscores in Python identifiers like `open_trades` / `net_liq`. See
        # handoff §0.5.143.
        return f"🤖 TradeFlow — {body}"

    async def alert_loop(self, stop_event: asyncio.Event) -> None:
        """Drain the alert queue → POST to Telegram. Runs until ``stop_event``."""
        LOGGER.info("[telegram] alert_loop: task_launched")
        backoff = RETRY_BACKOFF_BASE_SEC
        try:
            while not stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                key = self._parse_alert_key(msg) or "unknown"
                if self._is_dup(key, time.monotonic()):
                    LOGGER.debug("[telegram] alert_deduped: key=%s", key)
                    continue
                ok = await self._send(self._format_alert(msg))
                if not ok:
                    # Cooperative backoff — bail early if stop_event fires.
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                        return
                    except TimeoutError:
                        pass
                    backoff = min(backoff * 2, RETRY_BACKOFF_MAX_SEC)
                else:
                    backoff = RETRY_BACKOFF_BASE_SEC
        finally:
            LOGGER.info("[telegram] alert_loop: task_exited")

    # --------------------------------------------------------------- inbound

    async def command_loop(self, stop_event: asyncio.Event) -> None:
        """Long-poll ``getUpdates`` → dispatch slash commands. Runs until stopped."""
        LOGGER.info("[telegram] command_loop: task_launched")
        backoff = RETRY_BACKOFF_BASE_SEC
        try:
            while not stop_event.is_set():
                try:
                    updates = await self._poll_updates()
                except Exception as exc:
                    LOGGER.warning("[telegram] poll_exception: %r — backoff=%.1fs", exc, backoff)
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                        return
                    except TimeoutError:
                        pass
                    backoff = min(backoff * 2, RETRY_BACKOFF_MAX_SEC)
                    continue
                backoff = RETRY_BACKOFF_BASE_SEC
                for update in updates:
                    await self._handle_update(update)
        finally:
            LOGGER.info("[telegram] command_loop: task_exited")

    async def _poll_updates(self) -> list[dict[str, Any]]:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/getUpdates"
        params: dict[str, Any] = {
            "offset": self._update_offset,
            "timeout": LONG_POLL_TIMEOUT_SEC,
            "allowed_updates": ["message"],
        }
        r = await self._http.get(url, params=params, timeout=LONG_POLL_TIMEOUT_SEC + 5)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return []
        results: list[dict[str, Any]] = data.get("result", []) or []
        if results:
            self._update_offset = results[-1]["update_id"] + 1
        return results

    async def _handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if chat_id != self._operator_chat_id:
            LOGGER.warning("[telegram] unauthorized_command: chat_id=%s text=%r", chat_id, text)
            await self._send_to(chat_id, "Unauthorized.")
            return
        if not text.startswith("/"):
            return
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower()
        try:
            if cmd == "/status":
                await self._handle_status()
            elif cmd == "/halt":
                await self._handle_halt(rest)
            elif cmd == "/ack":
                await self._handle_ack(rest)
            elif cmd == "/flatten":
                await self._handle_flatten(rest)
            elif cmd == "/exit":
                await self._handle_exit(rest)
            elif cmd == "/confirm":
                await self._handle_confirm(rest)
            else:
                await self._send(
                    f"Unknown command: {cmd}\n"
                    "Available: /status, /halt SYMBOL [reason], /ack [reason], "
                    "/flatten, /exit SYMBOL, /confirm"
                )
        except Exception as exc:
            LOGGER.exception("[telegram] command_handler_failed: cmd=%s", cmd)
            await self._send(f"Command failed: {exc!r}")

    # ----------------------------------------------------------- command impls

    async def _handle_status(self) -> None:
        summary = await self._coordinator.get_broker_status_summary()
        halted = self._coordinator.is_halted()
        raised = self._coordinator.halt_raised_at()
        lines = ["Status", f"halted: {'YES' if halted else 'no'}"]
        if halted and raised is not None:
            age = datetime.now(UTC) - raised
            lines.append(f"halt_raised_at: {raised.isoformat()} (age={age})")
        lines.extend(
            [
                f"positions: {summary.get('positions', [])}",
                f"open_trades: {summary.get('open_trades_count', 0)}",
                f"account: {summary.get('account', '?')}",
                f"net_liq: ${summary.get('net_liq', '?')}",
            ]
        )
        await self._send("\n".join(lines))

    async def _handle_halt(self, rest: str) -> None:
        parts = rest.split(maxsplit=1)
        symbol = parts[0] if parts else None
        reason = parts[1] if len(parts) > 1 else "operator request via telegram"
        if not symbol:
            await self._send("Usage: /halt SYMBOL [reason]")
            return
        self._coordinator.raise_halt(symbol=symbol)
        await self._send(f"Halt raised for {symbol}. reason: {reason}")

    async def _handle_ack(self, rest: str) -> None:
        note = rest.strip() or "operator ack via telegram"
        row = await self._coordinator.insert_halt_ack(note=note)
        ack_id = str(row.get("halt_ack_id", "?"))[:8]
        await self._send(f"Ack inserted (halt_ack_id={ack_id}…). Reconciler will clear within 30s.")

    # ----------------------------------------------------- manual close (PR #16)

    async def _handle_flatten(self, rest: str) -> None:
        del rest  # /flatten takes no args; trailing text ignored.
        self._pending_action = PendingAction(
            kind="flatten",
            symbol=None,
            requested_at=datetime.now(UTC),
        )
        await self._send(
            "Flatten ALL positions staged.\n"
            "Reply /confirm within 60s to execute.\n"
            "Any other command overwrites or expires this."
        )

    async def _handle_exit(self, rest: str) -> None:
        parts = rest.strip().split()
        if not parts:
            await self._send("Usage: /exit SYMBOL\nExample: /exit MNQM6")
            return
        symbol = parts[0].upper()
        self._pending_action = PendingAction(
            kind="exit",
            symbol=symbol,
            requested_at=datetime.now(UTC),
        )
        await self._send(
            f"Exit {symbol} staged.\n"
            "Reply /confirm within 60s to execute.\n"
            "Any other command overwrites or expires this."
        )

    async def _handle_confirm(self, rest: str) -> None:
        del rest  # /confirm takes no args.
        pending = self._pending_action
        if pending is None:
            await self._send("No pending action.")
            return
        age = (datetime.now(UTC) - pending.requested_at).total_seconds()
        if age > PENDING_ACTION_TTL_SEC:
            self._pending_action = None
            await self._send(
                f"Pending action expired ({int(age)}s old). Re-issue /flatten or /exit."
            )
            return
        # Clear BEFORE dispatch so a double /confirm is a no-op even if the
        # coordinator call is slow. Idempotency by construction.
        self._pending_action = None
        if pending.kind == "flatten":
            result = await self._coordinator.flatten_all()
            await self._send(self._format_flatten_result(result))
        else:  # "exit"
            assert pending.symbol is not None
            result = await self._coordinator.exit_symbol(pending.symbol)
            await self._send(self._format_exit_result(result))

    @staticmethod
    def _format_flatten_result(result: FlattenResult) -> str:
        closed_count = sum(1 for c in result.closed if c.closed)
        lines = [
            f"Flatten complete: {closed_count} closed / "
            f"{len(result.requested_symbols)} requested"
        ]
        for c in result.closed:
            if c.closed:
                if c.pnl is not None:
                    lines.append(f"  {c.symbol}: {c.status} pnl=${c.pnl:.2f}")
                else:
                    lines.append(f"  {c.symbol}: {c.status}")
            else:
                lines.append(f"  {c.symbol}: skipped ({c.status})")
        return "\n".join(lines)

    @staticmethod
    def _format_exit_result(result: ExitResult) -> str:
        c = result.result
        if not c.closed:
            return f"Exit {result.symbol} skipped: {c.status}"
        if c.pnl is not None:
            return f"Exit {result.symbol} {c.status}: pnl=${c.pnl:.2f}"
        return f"Exit {result.symbol} {c.status}"

    async def aclose(self) -> None:
        await self._http.aclose()
