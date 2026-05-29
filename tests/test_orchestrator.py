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

    assert mock_ib.connect_with_resilience.await_count == 1
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
    mock_ib.connect_with_resilience.side_effect = RuntimeError("boom — connect failed")  # one call
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


async def test_orchestrator_survives_transient_disconnect_in_healthcheck(caplog):
    """PR-A — gateway-restart resilience.

    Healthcheck raises TimeoutError once (mid-loop). Orchestrator MUST
    NOT exit; instead, it calls connect_with_resilience to recover,
    emits the [ALERT] reconnect_recovered line, and keeps looping until
    stop_event is set.
    """
    caplog.set_level(logging.INFO)
    mock_ib = _make_mock_ib()
    # side_effect: 1 TimeoutError on first healthcheck, then datetimes forever.
    fake_time = MagicMock(name="datetime")
    fake_time.strftime = MagicMock(return_value="2026-05-27T16:00:00Z")
    mock_ib._ib.reqCurrentTimeAsync = AsyncMock(side_effect=[TimeoutError(), fake_time, fake_time])
    mock_db = _make_mock_db()
    orch = Orchestrator(
        mock_ib,
        mock_db,
        paper_account="DUQ1234567",
        healthcheck_interval=0.01,
        reconnect_max_attempts=3,
        reconnect_backoff_initial_sec=0.001,
        reconnect_backoff_max_sec=0.002,
        reconnect_connect_timeout_sec=0.1,
    )

    async def stopper():
        # Wait for at least one transient + recovery + one more healthcheck.
        for _ in range(40):
            await asyncio.sleep(0)
        if orch._stop_event is not None:
            orch._stop_event.set()

    with patch.object(orch, "_install_signal_handlers"):
        exit_code = await asyncio.wait_for(asyncio.gather(orch.run(), stopper()), timeout=3.0)

    # gather returns a list — first element is orch.run()'s exit code.
    assert exit_code[0] == 0, "orchestrator must not exit non-zero on a transient disconnect"
    # Startup connect (1) + recovery connect (1) = >= 2 calls.
    assert mock_ib.connect_with_resilience.await_count >= 2
    transient_logs = [
        r for r in caplog.records if "[ORCH] healthcheck: transient_disconnect" in r.getMessage()
    ]
    assert transient_logs, "expected transient_disconnect log line"
    recovered_logs = [r for r in caplog.records if "[ALERT] reconnect_recovered" in r.getMessage()]
    assert recovered_logs, "expected [ALERT] reconnect_recovered alert line"


async def test_resilient_reconnect_rearms_bar_subscription(caplog):
    """Track 2 — the keepUpToDate bar sub dies with the dropped socket and is
    NOT carried across an ib_async reconnect. After a successful resilient
    reconnect the orchestrator must re-arm it (re-invoke subscribe_bars) so
    [BAR] resumes without a manual restart (the "Peer closed connection" mode).
    """
    caplog.set_level(logging.INFO)
    mock_ib = _make_mock_ib()
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567")
    orch._loop = asyncio.get_running_loop()

    await orch._resilient_reconnect()

    mock_ib.connect_with_resilience.assert_awaited_once()
    mock_ib.subscribe_bars.assert_awaited_once()  # the re-arm
    assert any(
        "[ORCH] bar_subscription re-armed after socket reconnect" in r.getMessage()
        for r in caplog.records
    )
    assert any("[ALERT] reconnect_recovered" in r.getMessage() for r in caplog.records)


async def test_resilient_reconnect_skips_rearm_when_strategy_disabled():
    """With strategy disabled there is no bar sub to re-arm — reconnect only."""
    mock_ib = _make_mock_ib()
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567", enable_strategy=False)
    orch._loop = asyncio.get_running_loop()

    await orch._resilient_reconnect()

    mock_ib.connect_with_resilience.assert_awaited_once()
    mock_ib.subscribe_bars.assert_not_awaited()


# ============================================================================
# PR #12 — halt API (raise_halt / clear_halt / is_halted / halt_raised_at)
# ============================================================================


def _make_orch_for_halt_tests() -> Orchestrator:
    return Orchestrator(
        _make_mock_ib(),
        _make_mock_db(),
        paper_account="DUQ1234567",
        healthcheck_interval=10.0,
    )


def test_raise_halt_sets_flag_and_timestamp(caplog):
    caplog.set_level(logging.WARNING)
    orch = _make_orch_for_halt_tests()

    assert orch.is_halted() is False
    assert orch.halt_raised_at() is None

    orch.raise_halt("MNQM6")

    assert orch.is_halted() is True
    raised = orch.halt_raised_at()
    assert raised is not None
    # Timestamp must be tz-aware and recent.
    from datetime import UTC, datetime, timedelta

    assert raised.tzinfo is not None
    assert datetime.now(UTC) - raised < timedelta(seconds=5)
    assert any("[ORCH] halt_raised" in r.getMessage() for r in caplog.records)


def test_clear_halt_resets_flag_and_timestamp(caplog):
    caplog.set_level(logging.INFO)
    orch = _make_orch_for_halt_tests()
    orch.raise_halt("ESM6")
    assert orch.is_halted() is True

    orch.clear_halt(reason="operator ack via supabase")

    assert orch.is_halted() is False
    assert orch.halt_raised_at() is None
    assert any("[ORCH] halt_cleared" in r.getMessage() for r in caplog.records)


def test_clear_halt_when_not_halted_is_noop():
    orch = _make_orch_for_halt_tests()
    assert orch.is_halted() is False

    orch.clear_halt(reason="should be a no-op")  # must not raise

    assert orch.is_halted() is False
    assert orch.halt_raised_at() is None


def test_raise_halt_twice_is_idempotent_keeps_first_timestamp(caplog):
    caplog.set_level(logging.INFO)
    orch = _make_orch_for_halt_tests()
    orch.raise_halt("MNQM6")
    first_ts = orch.halt_raised_at()
    assert first_ts is not None

    orch.raise_halt("MNQM6")  # second call must NOT bump the timestamp

    assert orch.halt_raised_at() == first_ts
    assert any("halt_already_raised" in r.getMessage() for r in caplog.records)


async def test_handle_trade_signal_drops_when_halted(caplog):
    caplog.set_level(logging.WARNING)
    orch = _make_orch_for_halt_tests()
    orch.raise_halt("MNQM6")

    sig = MagicMock(name="Signal")
    sig.direction = "LONG"
    sig.instrument = "MNQM6"
    await orch._handle_trade_signal(sig)

    # Router should NOT have been asked to place an entry while halted.
    assert any("signal: dropped" in r.getMessage() for r in caplog.records)


# ============================================================================
# PR #16 — flatten_all / exit_symbol
# ============================================================================


def _make_orch_for_close_tests() -> Orchestrator:
    """Fresh orchestrator with router + sm replaced by mocks at the boundary."""
    from src.execution.router import OrderRouter
    from src.state_machine import StateMachine

    orch = Orchestrator(
        _make_mock_ib(),
        _make_mock_db(),
        paper_account="DUQ1234567",
        healthcheck_interval=10.0,
    )
    # Replace router and state machine with mocks so we can assert at the
    # close_position boundary without exercising real IB / DB code paths.
    orch._router = MagicMock(spec=OrderRouter)
    orch._router.close_position = AsyncMock()
    orch._sm = MagicMock(spec=StateMachine)
    orch._sm.load_non_closed = AsyncMock(return_value=[])
    return orch


def _make_mock_lifecycle(symbol: str = "MNQM6"):
    """Lifecycle stub for load_non_closed return values."""
    lc = MagicMock(name=f"Lifecycle<{symbol}>")
    lc.symbol = symbol
    return lc


async def test_flatten_all_with_no_positions_returns_empty_result(caplog):
    caplog.set_level(logging.INFO)
    orch = _make_orch_for_close_tests()
    orch._sm.load_non_closed = AsyncMock(return_value=[])  # exactly one call expected

    result = await orch.flatten_all()

    assert result.requested_symbols == []
    assert result.closed == []
    orch._router.close_position.assert_not_awaited()
    complete_logs = [r for r in caplog.records if "[ALERT] flatten_complete" in r.getMessage()]
    assert len(complete_logs) == 1
    assert "closed=0 total=0" in complete_logs[0].getMessage()


async def test_flatten_all_calls_close_position_per_symbol(caplog):
    caplog.set_level(logging.INFO)
    from src.execution.router import CloseResult

    orch = _make_orch_for_close_tests()
    orch._sm.load_non_closed = AsyncMock(
        return_value=[_make_mock_lifecycle("MNQM6"), _make_mock_lifecycle("ESM6")]
    )
    # Two close_position calls expected — one per symbol.
    orch._router.close_position = AsyncMock(
        side_effect=[
            CloseResult(
                closed=True,
                symbol="MNQM6",
                status="exit_submitted",
                close_reason="manual_flatten",
            ),
            CloseResult(
                closed=True,
                symbol="ESM6",
                status="exit_submitted",
                close_reason="manual_flatten",
            ),
        ]
    )

    result = await orch.flatten_all()

    assert result.requested_symbols == ["MNQM6", "ESM6"]
    assert len(result.closed) == 2
    assert orch._router.close_position.await_count == 2
    # All close_position calls carry reason="manual_flatten".
    for call in orch._router.close_position.await_args_list:
        assert call.kwargs.get("reason") == "manual_flatten"
    complete_logs = [r for r in caplog.records if "[ALERT] flatten_complete" in r.getMessage()]
    assert len(complete_logs) == 1
    assert "closed=2 total=2" in complete_logs[0].getMessage()


async def test_exit_symbol_with_no_position_returns_close_result_no_position():
    from src.execution.router import CloseResult

    orch = _make_orch_for_close_tests()
    orch._router.close_position = AsyncMock(
        return_value=CloseResult(
            closed=False,
            symbol="MNQM6",
            status="no_position",
            close_reason="manual_exit_symbol",
        )
    )

    result = await orch.exit_symbol("MNQM6")

    assert result.symbol == "MNQM6"
    assert result.result.closed is False
    assert result.result.status == "no_position"
    orch._router.close_position.assert_awaited_once_with("MNQM6", reason="manual_exit_symbol")


async def test_exit_symbol_happy_path_emits_alert(caplog):
    caplog.set_level(logging.INFO)
    from src.execution.router import CloseResult

    orch = _make_orch_for_close_tests()
    orch._router.close_position = AsyncMock(
        return_value=CloseResult(
            closed=True,
            symbol="MNQM6",
            status="exit_submitted",
            close_reason="manual_exit_symbol",
        )
    )

    await orch.exit_symbol("MNQM6")

    requested_logs = [r for r in caplog.records if "[ALERT] exit_requested" in r.getMessage()]
    assert len(requested_logs) == 1
    assert "symbol=MNQM6" in requested_logs[0].getMessage()
    # exit_symbol must NOT double-log [ALERT] exit_complete — that's the
    # router's job (and not captured here because the router is mocked).


async def test_flatten_all_continues_if_one_close_fails(caplog):
    caplog.set_level(logging.ERROR)
    from src.execution.router import CloseResult

    orch = _make_orch_for_close_tests()
    orch._sm.load_non_closed = AsyncMock(
        return_value=[_make_mock_lifecycle("MNQM6"), _make_mock_lifecycle("ESM6")]
    )
    # First raises, second returns OK. Exactly two calls expected.
    orch._router.close_position = AsyncMock(
        side_effect=[
            RuntimeError("place_order failed"),
            CloseResult(
                closed=True,
                symbol="ESM6",
                status="exit_submitted",
                close_reason="manual_flatten",
            ),
        ]
    )

    result = await orch.flatten_all()

    assert orch._router.close_position.await_count == 2
    assert len(result.closed) == 2
    # First lifecycle reflected as error, second as closed=True.
    assert result.closed[0].closed is False
    assert result.closed[0].status == "error"
    assert result.closed[1].closed is True
    error_logs = [r for r in caplog.records if "[ORCH] flatten_all: close_error" in r.getMessage()]
    assert len(error_logs) == 1


# ============================================================================
# PR-D2 — bar subscription requests extended-hours bars for the 24/5 strategy
# ============================================================================


async def test_subscribe_bars_called_with_use_rth_false():
    """The 24/5 strategy must receive bars outside RTH (09:30–16:00 ET).

    Regression for the silent mismatch shipped alongside PR #32 (24/5 session
    boundaries): the bar subscription kept the wrapper default use_rth=True,
    starving the scanner overnight.
    """
    mock_ib = _make_mock_ib()
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567")

    await orch._start_bar_subscription()

    assert mock_ib.subscribe_bars.await_count == 1
    kwargs = mock_ib.subscribe_bars.await_args.kwargs
    assert kwargs.get("use_rth") is False, (
        f"bar subscription must request extended-hours (24/5 CME) bars; "
        f"got use_rth={kwargs.get('use_rth')!r}"
    )


async def test_bar_subscription_log_includes_use_rth(caplog):
    """Startup log must surface the hours mode for production debugging."""
    caplog.set_level(logging.INFO)
    mock_ib = _make_mock_ib()
    mock_db = _make_mock_db()
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567")

    await orch._start_bar_subscription()

    start_logs = [
        r.getMessage()
        for r in caplog.records
        if "[STRAT]" in r.getMessage() and "bar_subscription started" in r.getMessage()
    ]
    assert start_logs, "expected [STRAT] bar_subscription started log line"
    assert (
        "use_rth=False" in start_logs[0]
    ), f"start log must include hours mode; got: {start_logs[0]!r}"


# ============================================================================
# W-S14.2 Track 5a — hourly session-status digest (pure builder)
# ============================================================================

from datetime import UTC as _UTC  # noqa: E402
from datetime import datetime as _dt  # noqa: E402

from src.orchestrator import build_hourly_digest  # noqa: E402


def _digest_window():
    start = _dt(2026, 5, 29, 14, 0, tzinfo=_UTC)
    end = _dt(2026, 5, 29, 15, 0, tzinfo=_UTC)
    return start, end


def test_build_hourly_digest_counts_decisions_and_gate_breakdown():
    start, end = _digest_window()
    decisions = (
        [{"decision": "noop_filter", "failed": "touch"}] * 45
        + [{"decision": "noop_filter", "failed": "bullish"}] * 10
        + [{"decision": "noop_filter", "failed": "ma_order"}] * 3
        + [{"decision": "noop_session_edge"}] * 2
    )
    line = build_hourly_digest(
        window_start=start,
        window_end=end,
        decisions=decisions,
        reconciliations=[],
        pos_str="FLAT",
        feed_ok=True,
        suppressed_count=0,
    )
    assert "TradeFlow hourly 14:00–15:00Z" in line
    assert "pos=FLAT" in line
    assert "evals=60" in line
    assert "long_signal=0" in line
    assert "noop_filter=58" in line
    assert "touch=45 bullish=10 ma_order=3 gap=0" in line
    assert "edge=2" in line
    assert "feed OK" in line


def test_build_hourly_digest_seanbot_scorecard_and_suppressed():
    start, end = _digest_window()
    recon = [
        {"classification": "AGREE_ENTER"},
        {"classification": "MISS-filter:touch"},
        {"classification": "MISS-filter:touch"},
    ]
    line = build_hourly_digest(
        window_start=start,
        window_end=end,
        decisions=[{"decision": "long_signal"}],
        reconciliations=recon,
        pos_str="MNQM6x2",
        feed_ok=False,
        suppressed_count=4,
    )
    assert "long_signal=1" in line
    assert "SeanBot: 3 entries → 1 AGREE" in line
    assert "2 MISS-filter:touch" in line
    assert "suppressed_in_position=4" in line
    assert "feed STALE" in line
    assert "pos=MNQM6x2" in line
