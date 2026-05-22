"""Tests for comms.telegram — httpx mocked at the wrapper boundary."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from comms.telegram import (
    ALERT_PREFIX,
    TelegramAlerter,
    TelegramAlertHandler,
)

# -------------------------------------------------------------------- factories


def _make_mock_http() -> MagicMock:
    """Mock httpx.AsyncClient with awaitable get/post returning a status-shaped response."""
    http = MagicMock(name="httpx.AsyncClient")
    http.get = AsyncMock()
    http.post = AsyncMock()
    http.aclose = AsyncMock()
    return http


def _fake_resp(status_code: int = 200, body: object = None, text: str = "") -> MagicMock:
    r = MagicMock(name="Response")
    r.status_code = status_code
    r.json = MagicMock(return_value=body if body is not None else {})
    r.text = text
    r.raise_for_status = MagicMock(return_value=None)
    if status_code >= 400:
        r.raise_for_status = MagicMock(side_effect=RuntimeError(f"http {status_code}"))
    return r


def _make_coordinator(
    *,
    halted: bool = False,
    raised_at: datetime | None = None,
    status_summary: dict | None = None,
    insert_ack_row: dict | None = None,
) -> MagicMock:
    c = MagicMock(name="coordinator")
    c.is_halted = MagicMock(return_value=halted)
    c.halt_raised_at = MagicMock(return_value=raised_at)
    c.raise_halt = MagicMock()
    c.clear_halt = MagicMock()
    c.get_broker_status_summary = AsyncMock(
        return_value=status_summary
        or {
            "positions": [],
            "open_trades_count": 0,
            "account": "DUQ",
            "net_liq": "1000000.00",
        }
    )
    c.insert_halt_ack = AsyncMock(
        return_value=insert_ack_row or {"halt_ack_id": "abcd1234efgh", "acked_at": "now"}
    )
    return c


def _build_alerter(
    *,
    http: MagicMock | None = None,
    coordinator: MagicMock | None = None,
    queue_max: int = 500,
    dedup_window_sec: float = 30.0,
    chat_id: int = 12345,
) -> tuple[TelegramAlerter, MagicMock, MagicMock]:
    http = http or _make_mock_http()
    coordinator = coordinator or _make_coordinator()
    alerter = TelegramAlerter(
        bot_token="test-token",
        operator_chat_id=chat_id,
        coordinator=coordinator,
        http_client=http,
        queue_max=queue_max,
        dedup_window_sec=dedup_window_sec,
    )
    return alerter, http, coordinator


# ============================================================================
# AlertHandler
# ============================================================================


async def test_alert_handler_filters_non_alert_records():
    queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    handler = TelegramAlertHandler(queue, loop)
    record = logging.LogRecord("x", logging.INFO, "p", 0, "[ORCH] healthcheck: ok", None, None)

    handler.emit(record)
    await asyncio.sleep(0)

    assert queue.empty()


async def test_alert_handler_enqueues_alert_records():
    queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    handler = TelegramAlertHandler(queue, loop)
    record = logging.LogRecord(
        "x", logging.INFO, "p", 0, "[ALERT] halt_raised: symbol=MNQM6", None, None
    )

    handler.emit(record)
    await asyncio.sleep(0)  # let call_soon_threadsafe deliver

    msg = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert "[ALERT] halt_raised: symbol=MNQM6" in msg


async def test_alert_handler_drops_oldest_when_queue_full(caplog):
    caplog.set_level(logging.WARNING)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    loop = asyncio.get_running_loop()
    handler = TelegramAlertHandler(queue, loop)
    queue.put_nowait("[ALERT] first: x")  # fill it

    record = logging.LogRecord("x", logging.INFO, "p", 0, "[ALERT] second: y", None, None)
    handler.emit(record)
    await asyncio.sleep(0)

    # Queue should now hold the newer message; oldest dropped.
    assert queue.qsize() == 1
    msg = queue.get_nowait()
    assert "second" in msg
    assert any("alert_queue_full" in r.getMessage() for r in caplog.records)


# ============================================================================
# parse + dedup
# ============================================================================


def test_parse_alert_key_extracts_event_type():
    assert TelegramAlerter._parse_alert_key("[ALERT] halt_raised: symbol=MNQM6") == "halt_raised"
    assert TelegramAlerter._parse_alert_key("[ALERT] entry_placed: symbol=X") == "entry_placed"
    assert TelegramAlerter._parse_alert_key("no alert here") is None


def test_is_dup_returns_false_first_time():
    alerter, _http, _c = _build_alerter()
    assert alerter._is_dup("halt_raised", now=100.0) is False


def test_is_dup_returns_true_within_window():
    alerter, _http, _c = _build_alerter(dedup_window_sec=30.0)
    alerter._is_dup("halt_raised", now=100.0)  # seed

    assert alerter._is_dup("halt_raised", now=110.0) is True


def test_is_dup_returns_false_after_window():
    alerter, _http, _c = _build_alerter(dedup_window_sec=30.0)
    alerter._is_dup("halt_raised", now=100.0)

    # 31s later — outside the window
    assert alerter._is_dup("halt_raised", now=131.0) is False


# ============================================================================
# _send
# ============================================================================


async def test_send_returns_true_on_200():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    alerter, _http, _c = _build_alerter(http=http)

    assert await alerter._send("hello") is True
    http.post.assert_awaited_once()
    call = http.post.call_args
    assert "sendMessage" in call.args[0]
    assert call.kwargs["json"]["text"] == "hello"


async def test_send_returns_false_on_non_200(caplog):
    caplog.set_level(logging.WARNING)
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=400, text="bad chat_id")
    alerter, _http, _c = _build_alerter(http=http)

    assert await alerter._send("hello") is False
    assert any("send_failed" in r.getMessage() for r in caplog.records)


async def test_send_returns_false_on_exception(caplog):
    caplog.set_level(logging.WARNING)
    http = _make_mock_http()
    http.post.side_effect = RuntimeError("connection refused")  # one call
    alerter, _http, _c = _build_alerter(http=http)

    assert await alerter._send("hello") is False
    assert any("send_exception" in r.getMessage() for r in caplog.records)


# ============================================================================
# command handlers
# ============================================================================


async def test_handle_status_calls_coordinator_and_sends():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    raised = datetime(2026, 5, 22, 15, 0, tzinfo=UTC)
    coord = _make_coordinator(
        halted=True,
        raised_at=raised,
        status_summary={
            "positions": [{"symbol": "MNQM6", "qty": 2, "avg_cost": 17500.0}],
            "open_trades_count": 2,
            "account": "DUQ",
            "net_liq": "987654.32",
        },
    )
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    await alerter._handle_status()

    coord.get_broker_status_summary.assert_awaited_once()
    coord.is_halted.assert_called_once()
    coord.halt_raised_at.assert_called_once()
    http.post.assert_awaited_once()
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "halted: YES" in sent_text
    assert "MNQM6" in sent_text
    assert "987654.32" in sent_text


async def test_handle_halt_calls_raise_halt_with_symbol():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    await alerter._handle_halt("MNQM6 manual probe")

    coord.raise_halt.assert_called_once_with(symbol="MNQM6")
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "Halt raised for MNQM6" in sent_text
    assert "manual probe" in sent_text


async def test_handle_halt_without_symbol_replies_usage():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    await alerter._handle_halt("")

    coord.raise_halt.assert_not_called()
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "Usage" in sent_text


async def test_handle_ack_calls_insert_halt_ack():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator(
        insert_ack_row={"halt_ack_id": "ffeeddcc-aabb-1122", "acked_at": "now"}
    )
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    await alerter._handle_ack("operator probe note")

    coord.insert_halt_ack.assert_awaited_once_with(note="operator probe note")
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "Ack inserted" in sent_text
    assert "ffeeddcc" in sent_text


async def test_unauthorized_chat_id_gets_unauthorized_reply(caplog):
    caplog.set_level(logging.WARNING)
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord, chat_id=12345)

    update = {
        "update_id": 1,
        "message": {"chat": {"id": 99999}, "text": "/status"},
    }
    await alerter._handle_update(update)

    # raise_halt / get_broker_status_summary must not be touched.
    coord.get_broker_status_summary.assert_not_awaited()
    coord.raise_halt.assert_not_called()
    # Reply went out with Unauthorized.
    http.post.assert_awaited()
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert sent_text == "Unauthorized."
    assert any("unauthorized_command" in r.getMessage() for r in caplog.records)


async def test_unknown_command_replies_help():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord, chat_id=12345)

    update = {"update_id": 2, "message": {"chat": {"id": 12345}, "text": "/foo"}}
    await alerter._handle_update(update)

    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "Unknown command" in sent_text


# ============================================================================
# alert_loop dedup integration
# ============================================================================


async def test_alert_loop_dedups_duplicates_in_window():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    alerter, _http, _c = _build_alerter(http=http, dedup_window_sec=30.0)
    # Two identical alerts within the window — only the first should POST.
    alerter._queue.put_nowait("[ALERT] halt_raised: symbol=MNQM6")
    alerter._queue.put_nowait("[ALERT] halt_raised: symbol=MNQM6")

    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.wait_for(asyncio.gather(alerter.alert_loop(stop), stopper()), timeout=2.0)

    assert http.post.await_count == 1


async def test_alert_loop_survives_telegram_outage():
    """Telegram down: _send returns False repeatedly; loop logs + sleeps but never crashes."""
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=503, text="upstream down")
    alerter, _http, _c = _build_alerter(http=http, dedup_window_sec=0.0)
    alerter._queue.put_nowait("[ALERT] entry_placed: symbol=MNQM6")

    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.05)
        stop.set()

    # Must not raise. The loop sleeps on failure; we don't assert post count here
    # because backoff timing makes the count nondeterministic in 50ms.
    await asyncio.wait_for(asyncio.gather(alerter.alert_loop(stop), stopper()), timeout=2.0)


# ============================================================================
# poll_updates + format
# ============================================================================


async def test_poll_updates_advances_offset_on_results():
    http = _make_mock_http()
    http.get.return_value = _fake_resp(
        status_code=200,
        body={
            "ok": True,
            "result": [{"update_id": 100, "message": {}}, {"update_id": 101, "message": {}}],
        },
    )
    alerter, _http, _c = _build_alerter(http=http)
    assert alerter._update_offset == 0

    results = await alerter._poll_updates()

    assert len(results) == 2
    assert alerter._update_offset == 102  # last update_id + 1


async def test_poll_updates_returns_empty_on_ok_false():
    http = _make_mock_http()
    http.get.return_value = _fake_resp(status_code=200, body={"ok": False})
    alerter, _http, _c = _build_alerter(http=http)

    results = await alerter._poll_updates()
    assert results == []
    assert alerter._update_offset == 0


def test_format_alert_strips_prefix_and_decorates():
    formatted = TelegramAlerter._format_alert("[ALERT] entry_placed: symbol=MNQM6 qty=2")
    assert formatted.startswith("🤖 TradeFlow — ")
    assert ALERT_PREFIX not in formatted
    assert "entry_placed" in formatted


# Allow pytest to pick up the async tests without explicit marker (asyncio_mode=auto).
pytest_plugins: list[str] = []
