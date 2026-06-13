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
    flatten_result: object | None = None,
    exit_result: object | None = None,
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
    # PR #16 — manual close commands. Tests that need specific values inject
    # them; otherwise a benign empty-flatten / no-position-exit is returned.
    c.flatten_all = AsyncMock(
        return_value=flatten_result if flatten_result is not None else _default_flatten_result()
    )
    c.exit_symbol = AsyncMock(
        return_value=exit_result if exit_result is not None else _default_exit_result("MNQM6")
    )
    return c


def _default_flatten_result():
    """Empty FlattenResult; importable without dragging Orchestrator into test deps."""
    from src.orchestrator import FlattenResult

    return FlattenResult(requested_symbols=[], closed=[])


def _default_exit_result(symbol: str):
    """No-position ExitResult for ``symbol``."""
    from src.execution.router import CloseResult
    from src.orchestrator import ExitResult

    return ExitResult(
        symbol=symbol,
        result=CloseResult(
            closed=False, symbol=symbol, status="no_position", close_reason="manual_exit_symbol"
        ),
    )


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


# ============================================================================
# PR #16 — /flatten /exit /confirm
# ============================================================================


def _make_close_result(
    *,
    closed: bool = True,
    symbol: str = "MNQM6",
    status: str = "exit_submitted",
    close_reason: str = "manual_exit_symbol",
    pnl: float | None = None,
):
    from src.execution.router import CloseResult

    return CloseResult(
        closed=closed,
        symbol=symbol,
        status=status,
        close_reason=close_reason,
        pnl=pnl,
    )


def _make_flatten_result(symbols: list[str], close_results: list):
    from src.orchestrator import FlattenResult

    return FlattenResult(requested_symbols=list(symbols), closed=list(close_results))


def _make_exit_result(symbol: str, close_result):
    from src.orchestrator import ExitResult

    return ExitResult(symbol=symbol, result=close_result)


async def test_handle_flatten_stages_pending_action_and_replies():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    assert alerter._pending_action is None
    await alerter._handle_flatten("")

    assert alerter._pending_action is not None
    assert alerter._pending_action.kind == "flatten"
    assert alerter._pending_action.symbol is None
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "Flatten ALL" in sent_text
    assert "/confirm within 60s" in sent_text
    coord.flatten_all.assert_not_awaited()


async def test_handle_exit_parses_symbol_and_stages():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    await alerter._handle_exit("mnqm6")  # lowercase — should be uppercased

    assert alerter._pending_action is not None
    assert alerter._pending_action.kind == "exit"
    assert alerter._pending_action.symbol == "MNQM6"
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "Exit MNQM6 staged" in sent_text
    coord.exit_symbol.assert_not_awaited()


async def test_handle_exit_rejects_missing_symbol():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    await alerter._handle_exit("")

    assert alerter._pending_action is None
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "Usage: /exit SYMBOL" in sent_text
    coord.exit_symbol.assert_not_awaited()


async def test_handle_confirm_no_pending_replies_no_action():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    assert alerter._pending_action is None
    await alerter._handle_confirm("")

    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert sent_text == "No pending action."
    coord.flatten_all.assert_not_awaited()
    coord.exit_symbol.assert_not_awaited()


async def test_handle_confirm_expired_clears_and_replies():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    # Stage an exit, then back-date its timestamp to 65s ago.
    from datetime import timedelta

    from comms.telegram import PendingAction

    alerter._pending_action = PendingAction(
        kind="exit",
        symbol="MNQM6",
        requested_at=datetime.now(UTC) - timedelta(seconds=65),
    )

    await alerter._handle_confirm("")

    assert alerter._pending_action is None
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "expired" in sent_text
    coord.exit_symbol.assert_not_awaited()


async def test_handle_confirm_flatten_dispatches_and_clears():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    flatten_res = _make_flatten_result(
        ["MNQM6"],
        [_make_close_result(closed=True, status="exit_submitted", close_reason="manual_flatten")],
    )
    coord = _make_coordinator(flatten_result=flatten_res)
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    from comms.telegram import PendingAction

    alerter._pending_action = PendingAction(
        kind="flatten",
        symbol=None,
        requested_at=datetime.now(UTC),
    )

    await alerter._handle_confirm("")

    coord.flatten_all.assert_awaited_once_with()
    coord.exit_symbol.assert_not_awaited()
    assert alerter._pending_action is None
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "Flatten complete" in sent_text
    assert "MNQM6" in sent_text


async def test_handle_confirm_exit_dispatches_with_symbol():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    exit_res = _make_exit_result(
        "MNQM6",
        _make_close_result(closed=True, status="exit_submitted", close_reason="manual_exit_symbol"),
    )
    coord = _make_coordinator(exit_result=exit_res)
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    from comms.telegram import PendingAction

    alerter._pending_action = PendingAction(
        kind="exit",
        symbol="MNQM6",
        requested_at=datetime.now(UTC),
    )

    await alerter._handle_confirm("")

    coord.exit_symbol.assert_awaited_once_with("MNQM6")
    coord.flatten_all.assert_not_awaited()
    assert alerter._pending_action is None
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert "Exit MNQM6" in sent_text


async def test_handle_confirm_idempotent_double_call_no_op():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    flatten_res = _make_flatten_result([], [])  # empty positions
    coord = _make_coordinator(flatten_result=flatten_res)
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    from comms.telegram import PendingAction

    alerter._pending_action = PendingAction(
        kind="flatten",
        symbol=None,
        requested_at=datetime.now(UTC),
    )

    await alerter._handle_confirm("")  # first confirm — dispatches flatten_all
    await alerter._handle_confirm("")  # second confirm — no-op

    coord.flatten_all.assert_awaited_once_with()
    # Second reply must be "No pending action."
    last_text = http.post.call_args_list[-1].kwargs["json"]["text"]
    assert last_text == "No pending action."


async def test_handle_flatten_overwrites_previous_pending_exit():
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    flatten_res = _make_flatten_result([], [])
    coord = _make_coordinator(flatten_result=flatten_res)
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    await alerter._handle_exit("MNQM6")
    assert alerter._pending_action is not None
    assert alerter._pending_action.kind == "exit"

    await alerter._handle_flatten("")
    assert alerter._pending_action is not None
    assert alerter._pending_action.kind == "flatten"
    assert alerter._pending_action.symbol is None

    await alerter._handle_confirm("")
    coord.flatten_all.assert_awaited_once_with()
    coord.exit_symbol.assert_not_awaited()


async def test_unauthorized_chat_id_cannot_stage_flatten(caplog):
    caplog.set_level(logging.WARNING)
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    coord = _make_coordinator()
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord, chat_id=12345)

    update = {
        "update_id": 1,
        "message": {"chat": {"id": 99999}, "text": "/flatten"},
    }
    await alerter._handle_update(update)

    assert alerter._pending_action is None
    coord.flatten_all.assert_not_awaited()
    sent_text = http.post.call_args.kwargs["json"]["text"]
    assert sent_text == "Unauthorized."
    assert any("unauthorized_command" in r.getMessage() for r in caplog.records)


async def test_no_parse_mode_in_any_new_reply():
    """Regression guard for §0.5.143: no Telegram reply may set parse_mode."""
    http = _make_mock_http()
    http.post.return_value = _fake_resp(status_code=200)
    flatten_res = _make_flatten_result(
        ["MNQM6"],
        [_make_close_result(closed=True, status="exit_submitted", close_reason="manual_flatten")],
    )
    exit_res = _make_exit_result(
        "MNQM6",
        _make_close_result(closed=True, status="exit_submitted", close_reason="manual_exit_symbol"),
    )
    coord = _make_coordinator(flatten_result=flatten_res, exit_result=exit_res)
    alerter, _http, _c = _build_alerter(http=http, coordinator=coord)

    # Exercise every new handler at least once.
    await alerter._handle_flatten("")
    await alerter._handle_exit("MNQM6")
    await alerter._handle_exit("")  # usage path
    await alerter._handle_confirm("")  # confirm the exit
    await alerter._handle_flatten("")  # re-stage
    await alerter._handle_confirm("")  # confirm the flatten
    await alerter._handle_confirm("")  # no pending

    for call in http.post.call_args_list:
        assert "parse_mode" not in call.kwargs.get(
            "json", {}
        ), f"parse_mode leaked into Telegram payload: {call.kwargs.get('json')}"
        assert (
            "parse_mode" not in call.kwargs
        ), f"parse_mode passed as kwarg to http.post: {call.kwargs}"


# Allow pytest to pick up the async tests without explicit marker (asyncio_mode=auto).
pytest_plugins: list[str] = []
