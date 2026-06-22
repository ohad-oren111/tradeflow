"""TradeFlow entry point — boots the orchestrator from env, runs to SIGTERM.

Loads `.env` via `python-dotenv` (NOT bash `source` — §0.5.110 trap on .env
values containing whitespace). Constructs `IBClient` + `SupabaseClient` from
env vars, hands them to `Orchestrator`, runs the event loop.

PR #8 scope: wiring only. No trading logic, no order placement.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.orchestrator import Orchestrator

LOGGER = logging.getLogger("tradeflow.main")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required env var {name} is unset or empty")
    return value


def _build_orchestrator_from_env() -> Orchestrator:
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    client_id = int(os.environ.get("IBKR_CLIENT_ID", "1"))
    paper_account = _require_env("IBKR_PAPER_ACCOUNT")
    # Supabase config: code follows .env reality (SERVICE_ROLE_KEY, not SERVICE_ROLE).
    # Optional in PR #8 scope (no DB writes yet); warn loudly if missing so the
    # operator is reminded before PR #9 (state machine) starts calling .upsert/.insert.
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not supabase_key:
        LOGGER.warning(
            "[main] supabase: credentials missing — client instantiated as placeholder. "
            "DB writes will fail when state machine (PR #9+) calls .upsert/.insert. "
            "Populate SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env before then."
        )
    # Front-month contract. No auto-roll yet (orchestrator _build_contract:
    # "PR #11 will add contract-roll awareness" — unbuilt), so the traded
    # contract is pinned here and must be rolled by the operator each quarter
    # via the compose INSTRUMENT env. Default preserves the historical
    # hard-coded value so an unset env is a no-op.
    instrument = os.environ.get("INSTRUMENT", "MNQM6")
    interval = float(os.environ.get("ORCH_HEALTHCHECK_INTERVAL_SEC", "60"))
    reconnect_max_attempts = int(os.environ.get("IBKR_RECONNECT_MAX_ATTEMPTS", "30"))
    reconnect_backoff_initial = float(os.environ.get("IBKR_RECONNECT_BACKOFF_INITIAL_SEC", "2.0"))
    reconnect_backoff_max = float(os.environ.get("IBKR_RECONNECT_BACKOFF_MAX_SEC", "30.0"))
    reconnect_connect_timeout = float(os.environ.get("IBKR_RECONNECT_CONNECT_TIMEOUT_SEC", "20.0"))

    ib = IBClient(host=host, port=port, client_id=client_id)
    db = SupabaseClient(url=supabase_url, key=supabase_key)
    return Orchestrator(
        ib,
        db,
        paper_account=paper_account,
        instrument=instrument,
        healthcheck_interval=interval,
        reconnect_max_attempts=reconnect_max_attempts,
        reconnect_backoff_initial_sec=reconnect_backoff_initial,
        reconnect_backoff_max_sec=reconnect_backoff_max,
        reconnect_connect_timeout_sec=reconnect_connect_timeout,
    )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ORCH_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env_file = os.environ.get("ENV_FILE")
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()
    orch = _build_orchestrator_from_env()
    return asyncio.run(orch.run())


if __name__ == "__main__":
    sys.exit(main())
