"""TradeFlow entry point — boots the orchestrator from env, runs to SIGTERM.

Loads `.env` via `python-dotenv` (NOT bash `source` — §0.5.110 trap on .env
values containing whitespace). Constructs `IBClient` + `SupabaseClient` from
env vars, hands them to `Orchestrator`, runs the event loop.

PR #8 scope: wiring only. No trading logic, no order placement.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from datetime import UTC, datetime

from dotenv import load_dotenv
from ib_async import Future

from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.instruments.front_month import (
    MNQ_CURRENCY,
    MNQ_EXCHANGE,
    MNQ_SYMBOL,
    FrontMonthRoller,
    resolve_front_month,
)
from src.orchestrator import Orchestrator

LOGGER = logging.getLogger("tradeflow.main")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required env var {name} is unset or empty")
    return value


def _build_orchestrator_from_env(instrument: str | None = None) -> Orchestrator:
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
    # Front-month contract is resolved dynamically by the caller (PR #170) and
    # passed in — no longer a static env pin. INSTRUMENT, if set, is an override
    # the caller honors; otherwise the live chain is authoritative. The env
    # fallback below only applies when called WITHOUT an explicit instrument
    # (the wiring unit tests); _run() always passes a resolved symbol.
    if instrument is None:
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


def _mnq_future() -> Future:
    return Future(symbol=MNQ_SYMBOL, exchange=MNQ_EXCHANGE, currency=MNQ_CURRENCY)


async def _resolve_boot_instrument(
    *, host: str, port: int, buffer_days: int, attempts: int = 5
) -> str:
    """Resolve the active MNQ front month at boot from the live chain (PR #170).

    Uses a short-lived, read-only IB connection on a dedicated client id (never the
    trading id). Retries a few times because the gateway may still be coming up.
    Raises if the chain can't be resolved — the container then restarts and retries,
    exactly as a gateway-down boot already does (we never start on a guessed
    contract)."""
    client_id = int(os.environ.get("IBKR_CLIENT_ID_ROLL", "95"))
    last_err: str = "unknown"
    for attempt in range(1, attempts + 1):
        client = IBClient(host=host, port=port, client_id=client_id)
        try:
            await client.connect(timeout=15.0)
            resolved = await resolve_front_month(
                lambda c=client: c._ib.reqContractDetailsAsync(_mnq_future()),
                buffer_days=buffer_days,
                now=datetime.now(UTC),
            )
            if resolved is not None:
                symbol, dte = resolved
                LOGGER.info("[ROLL] resolved front-month=%s dte=%d at boot", symbol, dte)
                return symbol
            last_err = "no qualifying front month in chain"
        except Exception as exc:  # noqa: BLE001 — retry transient boot/gateway races
            last_err = f"{type(exc).__name__}: {exc}"
            LOGGER.warning(
                "[ROLL] boot resolve attempt %d/%d failed — %s", attempt, attempts, last_err
            )
        finally:
            client.disconnect()
        await asyncio.sleep(min(2 * attempt, 10))
    raise RuntimeError(
        f"[ROLL] boot front-month resolution failed after {attempts} attempts: {last_err}"
    )


async def _front_month_roll_loop(
    orch: Orchestrator, *, override: str | None, buffer_days: int, interval_sec: int
) -> None:
    """Daily front-month check on the running bot. Rolls FLAT-only via a graceful
    self-restart (SIGTERM) so boot re-resolves AND re-seeds through the proven
    path. Read-only against the broker; reuses the orchestrator's connected client
    for contract details + positions."""
    roller = FrontMonthRoller(
        resolver=lambda: resolve_front_month(
            lambda: orch._ib._ib.reqContractDetailsAsync(_mnq_future()),
            buffer_days=buffer_days,
            now=datetime.now(UTC),
        ),
        current_instrument=lambda: orch._instrument,
        get_positions=orch._ib.get_positions,
        request_roll_restart=lambda: signal.raise_signal(signal.SIGTERM),
        override=override,
    )
    # Wait until the orchestrator's client is connected + boot has settled.
    while not orch._ib.is_connected:
        await asyncio.sleep(5)
    await asyncio.sleep(60)
    while True:
        with contextlib.suppress(Exception):
            await roller.check_once()
        await asyncio.sleep(interval_sec)


async def _run() -> int:
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    buffer_days = int(os.environ.get("ROLL_BUFFER_DAYS", "8"))
    roll_interval = int(os.environ.get("ROLL_CHECK_INTERVAL_SEC", "21600"))  # 6h

    override = os.environ.get("INSTRUMENT") or None
    if override:
        LOGGER.info("[ROLL] override — INSTRUMENT=%s pinned; auto-roll disabled", override)
        instrument = override
    else:
        instrument = await _resolve_boot_instrument(host=host, port=port, buffer_days=buffer_days)

    orch = _build_orchestrator_from_env(instrument)
    roll_task = asyncio.create_task(
        _front_month_roll_loop(
            orch, override=override, buffer_days=buffer_days, interval_sec=roll_interval
        ),
        name="tradeflow-front-month-roll",
    )
    try:
        return await orch.run()
    finally:
        roll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await roll_task


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
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
