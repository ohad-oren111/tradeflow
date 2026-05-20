"""Async wrapper around ib_async.IB for TradeFlow.

Phase 1 PR 2 scope: connect/disconnect lifecycle and read-only state probes.
No order placement, no strategy, no reconcile logic — those land in PR 3+.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ib_async import IB, PortfolioItem, Position, Trade

LOGGER = logging.getLogger(__name__)


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
