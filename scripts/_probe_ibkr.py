"""TradeFlow IBKR paper-state probe — used by scripts/health_snapshot.sh.

Reads positions + openTrades + serverVersion via ib_async. Exits 0 on success
with a one-line JSON-shaped summary on stdout. Intended to be called from the
VPS host, not from inside the container. Resolves env from ~/.tradeflow-secrets/.env
via python-dotenv (NEVER source the file in bash — line-17 quote trap).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.clients.ib_client import IBClient  # noqa: E402

LOGGER = logging.getLogger("tradeflow.probe.ibkr")


async def main() -> int:
    env_file = os.environ.get("ENV_FILE") or "/home/tradeflow/.tradeflow-secrets/.env"
    load_dotenv(env_file)

    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    client_id = int(os.environ.get("IBKR_CLIENT_ID_PROBE", "97"))

    client = IBClient(host=host, port=port, client_id=client_id)
    try:
        await client.connect(timeout=15.0)
        server_version = client._ib.client.serverVersion()
        positions = await client.get_positions()
        open_trades = await client.get_open_trades()
        portfolio = await client.get_portfolio()
        print(
            "[probe_ibkr] PASS — "
            f"serverVersion={server_version} positions={len(positions)} "
            f"openTrades={len(open_trades)} portfolio={len(portfolio)}"
        )
        return 0
    except Exception as e:
        print(f"[probe_ibkr] FAIL — {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(asyncio.run(main()))
