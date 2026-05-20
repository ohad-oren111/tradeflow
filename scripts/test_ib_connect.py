"""TradeFlow smoke test — connect to IB Gateway, read portfolio + trades, disconnect.

Run from the repo root:

    ENV_FILE=~/.tradeflow-secrets/.env python3 scripts/test_ib_connect.py

Exits 0 on success, non-zero on any failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys

# Repo root on sys.path so ``from src.clients...`` works when invoked as a script.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.clients.ib_client import IBClient  # noqa: E402

LOGGER = logging.getLogger("tradeflow.smoke")


async def main() -> int:
    env_file = os.environ.get("ENV_FILE")
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    client_id = int(os.environ.get("IBKR_CLIENT_ID_SMOKE", "99"))

    client = IBClient(host=host, port=port, client_id=client_id)
    try:
        await client.connect(timeout=15.0)
        portfolio = await client.get_portfolio()
        trades = await client.get_open_trades()
        LOGGER.info(
            "[smoke] PASS — portfolio_items=%s open_trades=%s",
            len(portfolio),
            len(trades),
        )
        return 0
    except Exception as e:
        LOGGER.error("[smoke] FAIL — %s: %s", type(e).__name__, e)
        return 1
    finally:
        client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(asyncio.run(main()))
