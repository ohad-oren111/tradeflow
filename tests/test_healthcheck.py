"""Tests for scripts.healthcheck — TCP and ib_async probes mocked at the wrapper boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.healthcheck import ib_probe_with_client, tcp_probe


def test_tcp_probe_success_returns_true_and_latency():
    fake_sock = MagicMock()
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=False)

    with patch("scripts.healthcheck.socket.create_connection", return_value=fake_sock) as p:
        ok, latency_ms = tcp_probe("127.0.0.1", 4002, timeout=1.0)

    assert ok is True
    assert latency_ms >= 0.0
    p.assert_called_once_with(("127.0.0.1", 4002), timeout=1.0)


def test_tcp_probe_failure_returns_false():
    with patch(
        "scripts.healthcheck.socket.create_connection",
        side_effect=TimeoutError("timed out"),
    ):
        ok, latency_ms = tcp_probe("127.0.0.1", 4002, timeout=0.1)

    assert ok is False
    assert latency_ms >= 0.0


def test_tcp_probe_failure_on_connection_refused():
    with patch(
        "scripts.healthcheck.socket.create_connection",
        side_effect=ConnectionRefusedError("refused"),
    ):
        ok, _ = tcp_probe("127.0.0.1", 4002, timeout=0.1)
    assert ok is False


async def test_ib_probe_with_client_success():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(return_value=None)
    fake_client.get_portfolio = AsyncMock(return_value=["item_a", "item_b"])
    fake_client.disconnect = MagicMock(return_value=None)

    ok, latency_ms = await ib_probe_with_client(fake_client)

    assert ok is True
    assert latency_ms >= 0.0
    fake_client.connect.assert_awaited_once_with(timeout=10.0)
    fake_client.get_portfolio.assert_awaited_once()
    fake_client.disconnect.assert_called_once()


async def test_ib_probe_with_client_failure_still_disconnects():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(side_effect=TimeoutError("boom"))
    fake_client.get_portfolio = AsyncMock()
    fake_client.disconnect = MagicMock(return_value=None)

    ok, _ = await ib_probe_with_client(fake_client)

    assert ok is False
    fake_client.get_portfolio.assert_not_awaited()
    fake_client.disconnect.assert_called_once()


async def test_ib_probe_with_client_get_portfolio_failure():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(return_value=None)
    fake_client.get_portfolio = AsyncMock(side_effect=RuntimeError("api error"))
    fake_client.disconnect = MagicMock(return_value=None)

    ok, _ = await ib_probe_with_client(fake_client)

    assert ok is False
    fake_client.disconnect.assert_called_once()


@pytest.mark.parametrize("host,port", [("127.0.0.1", 4002), ("ib-gateway", 4002)])
def test_tcp_probe_passes_through_host_port(host, port):
    with patch(
        "scripts.healthcheck.socket.create_connection",
        side_effect=ConnectionRefusedError("refused"),
    ) as p:
        tcp_probe(host, port, timeout=0.1)
    p.assert_called_once_with((host, port), timeout=0.1)
