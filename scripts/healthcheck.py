"""TradeFlow IB Gateway healthcheck — TCP probe + ib_async connect probe.

Two-layer design:
  - TCP probe — fast, catches "container not booted" / "port closed".
  - ib_async probe — deeper, validates the API handshake actually completes.

Designed to run on a schedule (cron / future orchestrator) every ~5 min from
the VPS host. Exits 0 healthy, non-zero on any probe failure with a one-line
stderr explanation already emitted via LOGGER.error.

The docker-compose healthcheck block uses a shell-only TCP probe; this script
is the deeper companion probe and is not wired into the compose healthcheck.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import socket
import sys
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.clients.ib_client import IBClient  # noqa: E402

LOGGER = logging.getLogger("tradeflow.healthcheck")


def tcp_probe(host: str, port: int, timeout: float = 2.0) -> tuple[bool, float]:
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency_ms = (time.monotonic() - start) * 1000
            LOGGER.info(
                "[healthcheck] tcp probe — host=%s port=%s result=open latency_ms=%.1f",
                host,
                port,
                latency_ms,
            )
            return True, latency_ms
    except OSError as e:
        latency_ms = (time.monotonic() - start) * 1000
        LOGGER.error(
            "[healthcheck] tcp probe FAIL — host=%s port=%s err=%s latency_ms=%.1f",
            host,
            port,
            e,
            latency_ms,
        )
        return False, latency_ms


async def ib_probe_with_client(client: IBClient) -> tuple[bool, float]:
    """Run an ib_async connect → get_portfolio → disconnect cycle on an IBClient.

    Split out so tests can inject a mock client without touching real network.
    """
    start = time.monotonic()
    try:
        await client.connect(timeout=10.0)
        portfolio = await client.get_portfolio()
        latency_ms = (time.monotonic() - start) * 1000
        LOGGER.info(
            "[healthcheck] ib_async probe — connected=true portfolio_items=%s latency_ms=%.1f",
            len(portfolio),
            latency_ms,
        )
        return True, latency_ms
    except Exception as e:
        latency_ms = (time.monotonic() - start) * 1000
        LOGGER.error(
            "[healthcheck] ib_async probe FAIL — %s: %s latency_ms=%.1f",
            type(e).__name__,
            e,
            latency_ms,
        )
        return False, latency_ms
    finally:
        client.disconnect()


async def main() -> int:
    env_file = os.environ.get("ENV_FILE")
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    client_id = int(os.environ.get("IBKR_CLIENT_ID_HEALTHCHECK", "98"))

    LOGGER.info("[healthcheck] start — host=%s port=%s", host, port)

    tcp_ok, tcp_ms = tcp_probe(host, port)
    if not tcp_ok:
        return 1

    client = IBClient(host=host, port=port, client_id=client_id)
    ib_ok, ib_ms = await ib_probe_with_client(client)
    if not ib_ok:
        return 2

    LOGGER.info(
        "[healthcheck] all probes PASS — tcp=%.1fms ib=%.1fms total=%.1fms",
        tcp_ms,
        ib_ms,
        tcp_ms + ib_ms,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(asyncio.run(main()))
