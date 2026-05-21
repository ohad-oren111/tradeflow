"""Tests for src.orchestrator.Orchestrator — mocked at the IBClient/SupabaseClient boundary."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import signal
from unittest.mock import AsyncMock, MagicMock, patch

from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.orchestrator import Orchestrator

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _make_account_value(tag: str, value: str) -> MagicMock:
    av = MagicMock(name=f"AccountValue<{tag}>")
    av.tag = tag
    av.value = value
    av.currency = "USD"
    return av


def _make_mock_ib(net_liq: str = "1000000.00") -> AsyncMock:
    """Fresh AsyncMock(spec=IBClient) plus a mock _ib for raw API calls."""
    mock_ib = AsyncMock(spec=IBClient)
    mock_ib._host = "127.0.0.1"
    mock_ib._port = 4002
    mock_ib._client_id = 1
    raw_ib = MagicMock(name="raw_IB")
    raw_ib.accountSummaryAsync = AsyncMock(
        return_value=[_make_account_value("NetLiquidation", net_liq)]
    )
    # reqCurrentTimeAsync is sync in ib_async but returns Awaitable[datetime];
    # for testing we substitute an AsyncMock returning a datetime-like object.
    fake_time = MagicMock(name="datetime")
    fake_time.strftime = MagicMock(return_value="2026-05-21T15:00:00Z")
    raw_ib.reqCurrentTimeAsync = AsyncMock(return_value=fake_time)
    raw_ib.client = MagicMock()
    raw_ib.client.serverVersion = MagicMock(return_value=178)
    mock_ib._ib = raw_ib
    mock_ib.disconnect = MagicMock(return_value=None)
    return mock_ib


def _make_mock_db() -> AsyncMock:
    return AsyncMock(spec=SupabaseClient)


async def test_run_calls_connect_then_disconnect(caplog):
    caplog.set_level(logging.INFO)
    mock_ib = _make_mock_ib()
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567", healthcheck_interval=10.0)

    async def stopper():
        # Yield a few times to let _startup + first healthcheck run, then signal stop.
        for _ in range(20):
            await asyncio.sleep(0)
        if orch._stop_event is not None:
            orch._stop_event.set()

    with patch.object(orch, "_install_signal_handlers"):
        await asyncio.wait_for(asyncio.gather(orch.run(), stopper()), timeout=2.0)

    assert mock_ib.connect.await_count == 1
    assert mock_ib.disconnect.call_count == 1
    mock_db.close.assert_awaited_once()


async def test_startup_logs_account_binding(caplog):
    caplog.set_level(logging.INFO)
    mock_ib = _make_mock_ib(net_liq="987654.32")
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ7654321", healthcheck_interval=10.0)

    async def stopper():
        for _ in range(20):
            await asyncio.sleep(0)
        if orch._stop_event is not None:
            orch._stop_event.set()

    with patch.object(orch, "_install_signal_handlers"):
        await asyncio.wait_for(asyncio.gather(orch.run(), stopper()), timeout=2.0)

    bind_logs = [r for r in caplog.records if "[ORCH] startup: account_bound" in r.getMessage()]
    assert bind_logs, "expected [ORCH] startup: account_bound log"
    msg = bind_logs[0].getMessage()
    assert "prefix=DUQ" in msg
    assert "987654.32" in msg


async def test_healthcheck_loop_runs_n_iterations():
    mock_ib = _make_mock_ib()
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567", healthcheck_interval=0.01)

    async def stopper():
        await asyncio.sleep(0.04)
        if orch._stop_event is not None:
            orch._stop_event.set()

    with patch.object(orch, "_install_signal_handlers"):
        await asyncio.wait_for(asyncio.gather(orch.run(), stopper()), timeout=2.0)

    assert mock_ib._ib.reqCurrentTimeAsync.await_count >= 2


async def test_sigterm_triggers_shutdown():
    mock_ib = _make_mock_ib()
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567", healthcheck_interval=10.0)
    # Mimic the in-run state without actually running.
    orch._stop_event = asyncio.Event()
    orch._loop = asyncio.get_running_loop()

    assert not orch._stop_event.is_set()
    orch._handle_signal(signal.SIGTERM, None)
    # call_soon_threadsafe schedules; yield once so the callback runs.
    await asyncio.sleep(0)
    assert orch._stop_event.is_set()


async def test_exception_in_run_loop_disconnects_ib(caplog):
    caplog.set_level(logging.INFO)
    mock_ib = _make_mock_ib()
    mock_ib.connect.side_effect = RuntimeError("boom — connect failed")  # one call
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567", healthcheck_interval=10.0)

    with patch.object(orch, "_install_signal_handlers"):
        exit_code = await asyncio.wait_for(orch.run(), timeout=2.0)

    assert exit_code == 1
    mock_ib.disconnect.assert_called_once()
    mock_db.close.assert_awaited_once()


def test_main_module_smoke():
    # main() calls asyncio.run() internally, so this test MUST be sync — running
    # an async test would put us inside an existing loop and asyncio.run() would
    # raise "cannot be called from a running event loop".
    import main as main_module

    fake_orch = MagicMock(name="Orchestrator")
    fake_orch.run = AsyncMock(return_value=0)
    with (
        patch.object(main_module, "_build_orchestrator_from_env", return_value=fake_orch),
        patch.object(main_module, "load_dotenv", return_value=False),
    ):
        rc = main_module.main()

    assert rc == 0
    fake_orch.run.assert_awaited_once()


async def test_account_summary_no_net_liquidation_raises():
    mock_ib = _make_mock_ib()
    # accountSummaryAsync returns a row with no NetLiquidation tag — exactly one call.
    mock_ib._ib.accountSummaryAsync = AsyncMock(
        return_value=[_make_account_value("BuyingPower", "500000")]
    )
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567", healthcheck_interval=10.0)

    with patch.object(orch, "_install_signal_handlers"):
        exit_code = await asyncio.wait_for(orch.run(), timeout=2.0)

    assert exit_code == 1
    # NetLiquidation missing should still trigger clean shutdown of both clients
    mock_ib.disconnect.assert_called_once()
    mock_db.close.assert_awaited_once()


def test_compose_app_service_defined():
    # Structural string check — PyYAML not in dev deps; intent is a cheap probe
    # that the new service block was wired in, not a full schema validation.
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "tradeflow-app:" in compose_text
    assert "container_name: tradeflow-app" in compose_text
    assert "build:" in compose_text
    assert "depends_on:" in compose_text
    assert "- ib-gateway" in compose_text


def test_dockerfile_exists():
    assert (
        REPO_ROOT / "Dockerfile"
    ).is_file(), "Dockerfile required for tradeflow-app build context"


async def test_signal_handler_noop_before_run():
    # _handle_signal called before run() should not crash even though
    # _stop_event/_loop are None.
    mock_ib = _make_mock_ib()
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567", healthcheck_interval=10.0)
    orch._handle_signal(signal.SIGTERM, None)  # must not raise
