"""Tests for src.clients.ib_client.IBClient — mocked at the wrapper boundary."""

from __future__ import annotations

import logging
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.clients.ib_client import (
    BrokerExtendedOutageError,
    IBClient,
    _wire_bar_callback,
    connect_with_resilience,
)


async def test_connect_calls_connect_async_with_expected_args(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True

    client = IBClient(host="127.0.0.1", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    await client.connect()

    fake_ib.connectAsync.assert_awaited_once_with("127.0.0.1", 4002, clientId=1, timeout=10.0)
    assert client.is_connected is True


async def test_connect_passes_through_custom_timeout(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True

    client = IBClient(host="h", port=4002, client_id=7, ib_factory=lambda: fake_ib)
    await client.connect(timeout=15.0)

    fake_ib.connectAsync.assert_awaited_once_with("h", 4002, clientId=7, timeout=15.0)


async def test_get_positions_returns_list_when_connected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    fake_ib.positions.return_value = ["pos_a", "pos_b"]

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    result = await client.get_positions()

    assert result == ["pos_a", "pos_b"]
    fake_ib.positions.assert_called_once()


async def test_get_positions_raises_when_not_connected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = False

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    with pytest.raises(RuntimeError, match="not connected"):
        await client.get_positions()
    fake_ib.positions.assert_not_called()


async def test_get_portfolio_returns_list_when_connected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    fake_ib.portfolio.return_value = ["pf_item_a"]

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    result = await client.get_portfolio()

    assert result == ["pf_item_a"]
    fake_ib.portfolio.assert_called_once()


async def test_get_portfolio_raises_when_not_connected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = False

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    with pytest.raises(RuntimeError, match="not connected"):
        await client.get_portfolio()


async def test_get_open_trades_returns_list_when_connected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    fake_ib.openTrades.return_value = ["trade_1"]

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    result = await client.get_open_trades()

    assert result == ["trade_1"]
    fake_ib.openTrades.assert_called_once()


async def test_get_open_trades_raises_when_not_connected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = False

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    with pytest.raises(RuntimeError, match="not connected"):
        await client.get_open_trades()


# ------------------------------------------ PR 1: get_historical_bars (shadow)


async def test_get_historical_bars_returns_snapshot_not_keepuptodate(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    fake_ib.reqHistoricalDataAsync = AsyncMock(return_value=["b1", "b2", "b3"])

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    out = await client.get_historical_bars(MagicMock(localSymbol="MNQM6"))

    assert out == ["b1", "b2", "b3"]
    fake_ib.reqHistoricalDataAsync.assert_awaited_once()
    # A snapshot, NOT a live subscription — must not keepUpToDate.
    assert fake_ib.reqHistoricalDataAsync.await_args.kwargs["keepUpToDate"] is False


async def test_get_historical_bars_default_window_is_two_days(mock_ib_factory):
    # "2 D" (not "1 D"): spans the prior session so the warmup seed always clears
    # the 100-bar SMA100 minimum even ~1 min after the 18:00 ET Globex reopen.
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    fake_ib.reqHistoricalDataAsync = AsyncMock(return_value=[])

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    await client.get_historical_bars(MagicMock(localSymbol="MNQM6"))

    assert fake_ib.reqHistoricalDataAsync.await_args.kwargs["durationStr"] == "2 D"


async def test_get_historical_bars_duration_override_is_honored(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    fake_ib.reqHistoricalDataAsync = AsyncMock(return_value=[])

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    await client.get_historical_bars(MagicMock(localSymbol="MNQM6"), duration="5 D")

    assert fake_ib.reqHistoricalDataAsync.await_args.kwargs["durationStr"] == "5 D"


async def test_get_historical_bars_raises_when_not_connected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = False

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    with pytest.raises(RuntimeError, match="not connected"):
        await client.get_historical_bars(MagicMock())


# ----------------------------------------------- PR #72: cancel_order_by_id


def _trade_with_order(order_id: int, client_id: int = 1) -> MagicMock:
    order = MagicMock(name=f"Order<{order_id}>")
    order.orderId = order_id
    order.clientId = client_id
    trade = MagicMock(name=f"Trade<{order_id}>")
    trade.order = order
    return trade


async def test_cancel_order_by_id_cancels_real_order_with_clientid(mock_ib_factory):
    # The W-S15.1 fix: a REAL Order (carrying .clientId) is handed to cancelOrder,
    # not a bare orderId stub (which raised AttributeError inside ib_async).
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    trade = _trade_with_order(1003)
    fake_ib.openTrades.return_value = [_trade_with_order(1002), trade]

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    result = await client.cancel_order_by_id(1003)

    assert result is True
    fake_ib.cancelOrder.assert_called_once_with(trade.order)
    passed = fake_ib.cancelOrder.call_args.args[0]
    assert hasattr(passed, "clientId")  # would be False for the old _OrderIdRef stub
    assert passed is trade.order


async def test_cancel_order_by_id_noop_when_not_live(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    fake_ib.openTrades.return_value = [_trade_with_order(1002)]

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    result = await client.cancel_order_by_id(9999)  # not among live trades

    assert result is False
    fake_ib.cancelOrder.assert_not_called()


async def test_cancel_order_by_id_noop_when_not_connected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = False

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    result = await client.cancel_order_by_id(1003)  # must not raise

    assert result is False
    fake_ib.cancelOrder.assert_not_called()


async def test_cancel_order_by_id_never_raises_on_broker_error(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    trade = _trade_with_order(1003)
    fake_ib.openTrades.return_value = [trade]
    fake_ib.cancelOrder.side_effect = RuntimeError("Error 10148")  # 1 raising call

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    result = await client.cancel_order_by_id(1003)  # must not raise

    assert result is False


def test_disconnect_is_idempotent_when_already_disconnected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = False

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    client.disconnect()

    fake_ib.disconnect.assert_not_called()


def test_disconnect_invokes_underlying_when_connected(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True

    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    client.disconnect()

    fake_ib.disconnect.assert_called_once()


# ----------------------------------------------------------- resilience layer


async def test_connect_with_resilience_retries_gaierror(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    # side_effect: 3 gaierror failures then 1 success = 4 calls total.
    fake_ib.connectAsync.side_effect = [
        socket.gaierror(-3, "Temporary failure in name resolution"),
        socket.gaierror(-3, "Temporary failure in name resolution"),
        socket.gaierror(-3, "Temporary failure in name resolution"),
        None,
    ]

    result = await connect_with_resilience(
        "ib-gateway",
        4004,
        1,
        ib=fake_ib,
        max_attempts=10,
        backoff_initial_sec=0.01,
        backoff_max_sec=0.05,
        backoff_factor=1.1,
        jitter_pct=0.0,
        connect_timeout_sec=0.5,
    )

    assert result is fake_ib
    assert fake_ib.connectAsync.await_count == 4
    # All call args identical — clientId stable across retries.
    for call in fake_ib.connectAsync.await_args_list:
        assert call.args == ("ib-gateway", 4004)
        assert call.kwargs["clientId"] == 1


async def test_connect_with_resilience_retries_timeout(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    # side_effect: 1 TimeoutError then 1 success = 2 calls total.
    fake_ib.connectAsync.side_effect = [TimeoutError(), None]

    result = await connect_with_resilience(
        "h",
        4002,
        1,
        ib=fake_ib,
        max_attempts=5,
        backoff_initial_sec=0.01,
        backoff_max_sec=0.02,
        jitter_pct=0.0,
        connect_timeout_sec=0.5,
    )

    assert result is fake_ib
    assert fake_ib.connectAsync.await_count == 2


async def test_connect_with_resilience_raises_after_max_attempts(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = False
    # side_effect: always gaierror = exactly max_attempts calls.
    fake_ib.connectAsync.side_effect = socket.gaierror(-3, "unreachable")

    with pytest.raises(BrokerExtendedOutageError) as exc_info:
        await connect_with_resilience(
            "h",
            4002,
            1,
            ib=fake_ib,
            max_attempts=4,
            backoff_initial_sec=0.01,
            backoff_max_sec=0.02,
            jitter_pct=0.0,
            connect_timeout_sec=0.5,
        )

    assert exc_info.value.attempts == 4
    assert isinstance(exc_info.value.last_exc, socket.gaierror)
    assert fake_ib.connectAsync.await_count == 4


async def test_connect_with_resilience_propagates_non_transient_exceptions(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = False
    # side_effect: non-transient ValueError on first attempt = 1 call, no retry.
    fake_ib.connectAsync.side_effect = ValueError("bad credentials")

    with pytest.raises(ValueError, match="bad credentials"):
        await connect_with_resilience(
            "h",
            4002,
            1,
            ib=fake_ib,
            max_attempts=10,
            backoff_initial_sec=0.01,
            backoff_max_sec=0.02,
            jitter_pct=0.0,
            connect_timeout_sec=0.5,
        )

    assert fake_ib.connectAsync.await_count == 1


async def test_ibclient_connect_with_resilience_uses_wrapped_ib(mock_ib_factory):
    fake_ib = mock_ib_factory()
    fake_ib.isConnected.return_value = True
    # side_effect: 1 transient then success = 2 calls total via the IBClient method.
    fake_ib.connectAsync.side_effect = [TimeoutError(), None]

    client = IBClient(host="ib-gateway", port=4004, client_id=1, ib_factory=lambda: fake_ib)
    await client.connect_with_resilience(
        max_attempts=5,
        backoff_initial_sec=0.01,
        backoff_max_sec=0.02,
        jitter_pct=0.0,
        connect_timeout_sec=0.5,
    )

    assert fake_ib.connectAsync.await_count == 2
    # The IBClient method reuses self._ib — clientId carried through unchanged.
    for call in fake_ib.connectAsync.await_args_list:
        assert call.kwargs["clientId"] == 1


# ------------------------------------------------------------ bar callback wiring


class _FakeEvent:
    """Minimal stand-in for ib_async ``Event`` that captures ``+=`` handlers."""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeBars(list):
    """List subclass that mimics ``BarDataList`` shape (carries ``contract`` + ``updateEvent``)."""

    def __init__(self, items, contract=None) -> None:
        super().__init__(items)
        self.contract = contract
        self.updateEvent = _FakeEvent()


def _capture_adapter(bars: _FakeBars, callback) -> object:
    """Wire the adapter via the public API and return the handler that was installed."""
    _wire_bar_callback(bars, callback)
    assert len(bars.updateEvent.handlers) == 1, "expected exactly one adapter wired"
    return bars.updateEvent.handlers[0]


def test_adapter_logs_bar_on_new_bar(caplog):
    bar = MagicMock(close=21500.25, date="2026-05-27T20:30:00")
    contract = MagicMock(localSymbol="MNQM6")
    bars = _FakeBars([bar], contract=contract)
    callback = MagicMock(return_value=None)
    adapter = _capture_adapter(bars, callback)

    with caplog.at_level(logging.INFO, logger="src.clients.ib_client"):
        adapter(bars, True)

    bar_logs = [r for r in caplog.records if r.getMessage().startswith("[BAR]")]
    assert len(bar_logs) == 1
    message = bar_logs[0].getMessage()
    assert "MNQM6" in message
    assert "21500.25" in message
    assert "2026-05-27T20:30:00" in message


def test_adapter_no_log_when_no_new_bar(caplog):
    bar = MagicMock(close=21500.25, date="2026-05-27T20:30:00")
    contract = MagicMock(localSymbol="MNQM6")
    bars = _FakeBars([bar], contract=contract)
    callback = MagicMock(return_value=None)
    adapter = _capture_adapter(bars, callback)

    with caplog.at_level(logging.INFO, logger="src.clients.ib_client"):
        adapter(bars, False)

    bar_logs = [r for r in caplog.records if r.getMessage().startswith("[BAR]")]
    assert len(bar_logs) == 0
