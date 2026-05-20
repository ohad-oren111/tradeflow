"""Tests for src.clients.ib_client.IBClient — mocked at the wrapper boundary."""

from __future__ import annotations

import pytest

from src.clients.ib_client import IBClient


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
