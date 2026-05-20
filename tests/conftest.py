"""Shared pytest fixtures for the TradeFlow test suite.

Fixtures return *factories* (callables) or *fresh instances* so each test gets
its own mock state — never a module-level shared MagicMock.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_ib_factory() -> Callable[[], MagicMock]:
    """Returns a factory that produces a fresh mock IB instance per call."""

    def factory() -> MagicMock:
        ib = MagicMock(name="IB")
        ib.connectAsync = AsyncMock(return_value=None)
        ib.disconnect = MagicMock(return_value=None)
        ib.isConnected = MagicMock(return_value=False)
        ib.positions = MagicMock(return_value=[])
        ib.portfolio = MagicMock(return_value=[])
        ib.openTrades = MagicMock(return_value=[])
        ib.client = MagicMock()
        ib.client.serverVersion = MagicMock(return_value=176)
        return ib

    return factory


@pytest.fixture
def mock_httpx_client() -> MagicMock:
    """Fresh mock httpx.AsyncClient per test. Configure return values per test."""
    client = MagicMock(name="httpx.AsyncClient")
    client.get = AsyncMock()
    client.post = AsyncMock()
    client.aclose = AsyncMock()
    return client
