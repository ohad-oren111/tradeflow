"""Unit tests for scripts.tradeflow_watchdog.

Pure-function-only. No live container, no live IBKR, no live Telegram.

Mocking discipline (per project history of mocking traps):
- Fresh MagicMock per test (no shared state across tests).
- httpx mocked at response-level (MagicMock(status_code=..., text=..., json=lambda: ...)).
- subprocess.run mocked to return subprocess.CompletedProcess(...).
- ib_async.IB patched at the import site (scripts.tradeflow_watchdog.IB).
- Time-dependent assertions inject a now_fn() lambda — no freezegun/time_machine dep added.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from scripts import tradeflow_watchdog as wd

# ============================================================================
# helpers
# ============================================================================


def _fixed_now(year: int = 2026, month: int = 5, day: int = 26, hour: int = 14, minute: int = 0):
    fixed = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return lambda: fixed


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def tmp_state(monkeypatch, tmp_path: Path) -> Path:
    """Redirect STATE_DIR / STATE_FILE / LOG_FILE to a per-test temp dir.

    Without LOG_FILE redirect, configure_logging() on a CI runner fails because
    the real ~/.tradeflow-watchdog/ doesn't exist there.
    """
    monkeypatch.setattr(wd, "STATE_DIR", tmp_path)
    monkeypatch.setattr(wd, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(wd, "LOG_FILE", tmp_path / "watchdog.log")
    return tmp_path


# ============================================================================
# 1. load_state
# ============================================================================


def test_load_state_missing_returns_defaults(tmp_state):
    state = wd.load_state()
    assert state == {
        "alert_history": {},
        "auto_heal_history": [],
        "last_restart_counts": {},
        "restart_loop_stable_cycles": 0,
    }


def test_load_state_corrupt_json_returns_defaults(tmp_state, caplog):
    (tmp_state / "state.json").write_text("{this is not json")
    with caplog.at_level("WARNING"):
        state = wd.load_state()
    assert state == {
        "alert_history": {},
        "auto_heal_history": [],
        "last_restart_counts": {},
        "restart_loop_stable_cycles": 0,
    }
    assert any("corrupt" in r.message for r in caplog.records)


def test_load_state_valid_json_returns_parsed_and_fills_defaults(tmp_state):
    (tmp_state / "state.json").write_text(
        json.dumps({"alert_history": {"x": "2026-01-01T00:00:00+00:00"}})
    )
    state = wd.load_state()
    assert state["alert_history"] == {"x": "2026-01-01T00:00:00+00:00"}
    assert state["auto_heal_history"] == []
    assert state["last_restart_counts"] == {}


# ============================================================================
# 2. save_state — atomic write
# ============================================================================


def test_save_state_atomic_round_trip(tmp_state):
    payload = {
        "alert_history": {"k": "v"},
        "auto_heal_history": [{"at": "x"}],
        "last_restart_counts": {"c": 1},
    }
    wd.save_state(payload)
    assert (tmp_state / "state.json").exists()
    assert not (tmp_state / "state.tmp").exists()  # tmp cleaned up by rename
    reloaded = json.loads((tmp_state / "state.json").read_text())
    assert reloaded == payload


def test_save_state_overwrites_existing(tmp_state):
    (tmp_state / "state.json").write_text(json.dumps({"old": True}))
    wd.save_state({"alert_history": {}, "auto_heal_history": [], "last_restart_counts": {}})
    reloaded = json.loads((tmp_state / "state.json").read_text())
    assert "old" not in reloaded
    assert reloaded["alert_history"] == {}


# ============================================================================
# 3. should_send_alert — 15-min dedup
# ============================================================================


def test_should_send_alert_never_fired_returns_true():
    state = {"alert_history": {}}
    assert wd.should_send_alert(state, "ib_api_down", now_fn=_fixed_now()) is True


def test_should_send_alert_fired_recently_returns_false():
    now = _fixed_now()
    state = {"alert_history": {"ib_api_down": (now() - timedelta(minutes=5)).isoformat()}}
    assert wd.should_send_alert(state, "ib_api_down", now_fn=now) is False


def test_should_send_alert_fired_long_ago_returns_true():
    now = _fixed_now()
    state = {"alert_history": {"ib_api_down": (now() - timedelta(minutes=30)).isoformat()}}
    assert wd.should_send_alert(state, "ib_api_down", now_fn=now) is True


def test_should_send_alert_corrupt_timestamp_returns_true():
    state = {"alert_history": {"ib_api_down": "not-a-timestamp"}}
    assert wd.should_send_alert(state, "ib_api_down", now_fn=_fixed_now()) is True


# ============================================================================
# 4. record_alert / clear_alert round-trip
# ============================================================================


def test_alert_record_then_clear_round_trip():
    state = {"alert_history": {}}
    now = _fixed_now()
    wd.record_alert(state, "ib_api_down", now_fn=now)
    assert "ib_api_down" in state["alert_history"]
    assert wd.should_send_alert(state, "ib_api_down", now_fn=now) is False
    wd.clear_alert(state, "ib_api_down")
    assert "ib_api_down" not in state["alert_history"]
    assert wd.should_send_alert(state, "ib_api_down", now_fn=now) is True


# ============================================================================
# 5. probe_ib_api — mocked IB
# ============================================================================


def _ib_mock(
    *,
    connect_ok: bool = True,
    connected: bool = True,
    server_version: int = 178,
    time_ok: bool = True,
):
    """Build a fresh MagicMock IB instance with controllable async behaviour."""
    mock = MagicMock()
    if connect_ok:
        mock.connectAsync = AsyncMock(return_value=None)
    else:
        mock.connectAsync = AsyncMock(side_effect=TimeoutError())
    mock.isConnected.return_value = connected
    mock.client.serverVersion.return_value = server_version
    if time_ok:
        mock.reqCurrentTimeAsync = AsyncMock(return_value=datetime(2026, 5, 26, 13, 0, tzinfo=UTC))
    else:
        mock.reqCurrentTimeAsync = AsyncMock(side_effect=TimeoutError())
    return mock


def test_probe_ib_api_success(monkeypatch):
    mock_ib = _ib_mock()
    monkeypatch.setattr(wd, "IB", lambda: mock_ib)
    ok, detail = wd.probe_ib_api("127.0.0.1", 4002, 96, timeout=1.0)
    assert ok is True
    assert "server_version=178" in detail
    mock_ib.disconnect.assert_called_once()


def test_probe_ib_api_connect_timeout(monkeypatch):
    mock_ib = _ib_mock(connect_ok=False, connected=False)
    monkeypatch.setattr(wd, "IB", lambda: mock_ib)
    ok, detail = wd.probe_ib_api("127.0.0.1", 4002, 96, timeout=1.0, attempts=1)
    assert ok is False
    assert "TimeoutError" in detail


def test_probe_ib_api_reqcurrenttime_timeout(monkeypatch):
    mock_ib = _ib_mock(time_ok=False)
    monkeypatch.setattr(wd, "IB", lambda: mock_ib)
    ok, detail = wd.probe_ib_api("127.0.0.1", 4002, 96, timeout=1.0, attempts=1)
    assert ok is False
    assert "TimeoutError" in detail


def test_probe_ib_api_missing_server_version(monkeypatch):
    mock_ib = _ib_mock(server_version=0)
    monkeypatch.setattr(wd, "IB", lambda: mock_ib)
    ok, detail = wd.probe_ib_api("127.0.0.1", 4002, 96, timeout=1.0, attempts=1)
    assert ok is False
    assert "missing server version" in detail


def test_probe_ib_api_retries_then_succeeds(monkeypatch):
    """W-S14.2 Track 5c: a transient failure on attempt 1 recovers on attempt 2
    rather than declaring a false FAIL. Sleep is stubbed so the test is fast."""
    fail_ib = _ib_mock(connect_ok=False, connected=False)
    ok_ib = _ib_mock()
    ibs = iter([fail_ib, ok_ib])
    monkeypatch.setattr(wd, "IB", lambda: next(ibs))
    slept: list[float] = []
    ok, detail = wd.probe_ib_api(
        "127.0.0.1", 4002, 96, timeout=1.0, attempts=3, sleep_fn=slept.append
    )
    assert ok is True
    assert "recovered on attempt 2/3" in detail
    assert slept == [2.0]  # one backoff before the successful 2nd attempt


def test_probe_ib_api_all_attempts_fail(monkeypatch):
    """All attempts fail → FAIL with the attempt counter in the detail."""
    monkeypatch.setattr(wd, "IB", lambda: _ib_mock(connect_ok=False, connected=False))
    ok, detail = wd.probe_ib_api(
        "127.0.0.1", 4002, 96, timeout=1.0, attempts=3, sleep_fn=lambda _s: None
    )
    assert ok is False
    assert "attempt 3/3" in detail


# ============================================================================
# 6. probe_container — docker inspect
# ============================================================================


def _inspect_payload(
    status: str = "running", health: str = "healthy", restart_count: int = 0
) -> str:
    return json.dumps(
        [
            {
                "State": {
                    "Status": status,
                    "Health": {"Status": health} if health else None,
                    "StartedAt": "2026-05-26T10:00:00Z",
                },
                "RestartCount": restart_count,
            }
        ]
    )


def test_probe_container_running_healthy(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        MagicMock(return_value=_completed(stdout=_inspect_payload(restart_count=2))),
    )
    ok, detail = wd.probe_container("tradeflow-app")
    assert ok is True
    assert detail["status"] == "running"
    assert detail["health"] == "healthy"
    assert detail["restart_count"] == 2


def test_probe_container_running_unhealthy(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        MagicMock(return_value=_completed(stdout=_inspect_payload(health="unhealthy"))),
    )
    ok, detail = wd.probe_container("tradeflow-app")
    assert ok is False
    assert detail["health"] == "unhealthy"


def test_probe_container_exited(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        MagicMock(return_value=_completed(stdout=_inspect_payload(status="exited", health=""))),
    )
    ok, detail = wd.probe_container("tradeflow-app")
    assert ok is False
    assert detail["status"] == "exited"


def test_probe_container_inspect_failure(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        MagicMock(return_value=_completed(returncode=1, stderr="no such container")),
    )
    ok, detail = wd.probe_container("nonexistent")
    assert ok is False
    assert "inspect failed" in detail["error"]


# ============================================================================
# 7. probe_supabase — httpx
# ============================================================================


def test_probe_supabase_200_ok(monkeypatch):
    resp = MagicMock(status_code=200)
    monkeypatch.setattr(wd.httpx, "get", MagicMock(return_value=resp))
    ok, detail = wd.probe_supabase("https://x.supabase.co", "anon_key")
    assert ok is True
    assert "200" in detail


def test_probe_supabase_503_fails(monkeypatch):
    resp = MagicMock(status_code=503)
    monkeypatch.setattr(wd.httpx, "get", MagicMock(return_value=resp))
    ok, detail = wd.probe_supabase("https://x.supabase.co", "anon_key")
    assert ok is False
    assert "503" in detail


def test_probe_supabase_timeout(monkeypatch):
    monkeypatch.setattr(wd.httpx, "get", MagicMock(side_effect=httpx.TimeoutException("timed out")))
    ok, detail = wd.probe_supabase("https://x.supabase.co", "anon_key")
    assert ok is False
    assert "timeout" in detail


def test_probe_supabase_connect_error(monkeypatch):
    monkeypatch.setattr(wd.httpx, "get", MagicMock(side_effect=httpx.ConnectError("refused")))
    ok, detail = wd.probe_supabase("https://x.supabase.co", "anon_key")
    assert ok is False
    assert "ConnectError" in detail


def test_probe_supabase_missing_env_returns_false():
    ok, detail = wd.probe_supabase(None, None)
    assert ok is False
    assert "missing" in detail


# ============================================================================
# 8. probe_dashboard
# ============================================================================


def test_probe_dashboard_200(monkeypatch):
    monkeypatch.setattr(wd.httpx, "get", MagicMock(return_value=MagicMock(status_code=200)))
    ok, detail = wd.probe_dashboard()
    assert ok is True


def test_probe_dashboard_401_auth_gated(monkeypatch):
    monkeypatch.setattr(wd.httpx, "get", MagicMock(return_value=MagicMock(status_code=401)))
    ok, detail = wd.probe_dashboard()
    assert ok is True
    assert "401" in detail
    assert "auth-gated" in detail


def test_probe_dashboard_500_fails(monkeypatch):
    monkeypatch.setattr(wd.httpx, "get", MagicMock(return_value=MagicMock(status_code=500)))
    ok, detail = wd.probe_dashboard()
    assert ok is False


def test_probe_dashboard_connect_refused(monkeypatch):
    monkeypatch.setattr(wd.httpx, "get", MagicMock(side_effect=httpx.ConnectError("refused")))
    ok, detail = wd.probe_dashboard()
    assert ok is False
    assert "ConnectError" in detail


# ============================================================================
# 9. probe_disk
# ============================================================================


_DF_TEMPLATE = (
    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
    "/dev/sda1 100000 {used} 20000 {pct}% /\n"
)


def test_probe_disk_low_usage_ok(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        MagicMock(return_value=_completed(stdout=_DF_TEMPLATE.format(used=50000, pct=50))),
    )
    ok, detail = wd.probe_disk(paths=("/",))
    assert ok is True
    assert detail["/"] == 50


def test_probe_disk_above_threshold_fails(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        MagicMock(return_value=_completed(stdout=_DF_TEMPLATE.format(used=92000, pct=92))),
    )
    ok, detail = wd.probe_disk(paths=("/",))
    assert ok is False
    assert detail["/"] == 92


def test_probe_disk_at_threshold_boundary_ok(monkeypatch):
    """85% is the threshold — boundary should be inclusive (ok). Above 85 fails."""
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        MagicMock(return_value=_completed(stdout=_DF_TEMPLATE.format(used=85000, pct=85))),
    )
    ok, _ = wd.probe_disk(paths=("/",))
    assert ok is True


# ============================================================================
# 10. probe_memory
# ============================================================================


def _meminfo(total_kb: int = 8_000_000, available_kb: int = 4_000_000) -> str:
    return (
        f"MemTotal:       {total_kb} kB\n"
        "MemFree:        100000 kB\n"
        f"MemAvailable:   {available_kb} kB\n"
    )


def test_probe_memory_low_usage_ok(tmp_path: Path):
    f = tmp_path / "meminfo"
    f.write_text(_meminfo(total_kb=8_000_000, available_kb=6_000_000))  # 25% used
    ok, detail = wd.probe_memory(meminfo_path=str(f))
    assert ok is True
    assert detail["used_pct"] == 25.0


def test_probe_memory_above_threshold_fails(tmp_path: Path):
    f = tmp_path / "meminfo"
    f.write_text(_meminfo(total_kb=8_000_000, available_kb=400_000))  # 95% used
    ok, detail = wd.probe_memory(meminfo_path=str(f))
    assert ok is False
    assert detail["used_pct"] == 95.0


def test_probe_memory_missing_file_fails(tmp_path: Path):
    ok, detail = wd.probe_memory(meminfo_path=str(tmp_path / "does_not_exist"))
    assert ok is False
    assert "error" in detail


# ============================================================================
# 10b. PR-C hardening guards — docker disk, OOM proximity, restart drift, bar age
# ============================================================================


def _meminfo_swap(total_kb: int, available_kb: int, swap_total_kb: int, swap_free_kb: int) -> str:
    return (
        f"MemTotal:       {total_kb} kB\n"
        f"MemAvailable:   {available_kb} kB\n"
        f"SwapTotal:      {swap_total_kb} kB\n"
        f"SwapFree:       {swap_free_kb} kB\n"
    )


def test_size_to_gb_units():
    assert wd._size_to_gb("2.156GB") == pytest.approx(2.156)
    assert wd._size_to_gb("535.2MB") == pytest.approx(0.5352)
    assert wd._size_to_gb("0B") == 0.0
    assert wd._size_to_gb("garbage") == 0.0


def test_probe_docker_disk_ok_under_threshold_with_log_cap(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        lambda *a, **k: _completed(stdout="Images\t0B (0%)\nBuild Cache\t535.2MB (100%)\n"),
    )
    monkeypatch.setattr(wd, "_app_log_cap", lambda *a, **k: "10m")
    ok, detail = wd.probe_docker_disk()
    assert ok is True
    assert detail["reclaimable_gb"] == pytest.approx(0.54, abs=0.01)
    assert detail["app_log_max_size"] == "10m"


def test_probe_docker_disk_fails_when_reclaimable_high(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess, "run", lambda *a, **k: _completed(stdout="Build Cache\t20GB (100%)\n")
    )
    monkeypatch.setattr(wd, "_app_log_cap", lambda *a, **k: "10m")
    ok, detail = wd.probe_docker_disk()
    assert ok is False
    assert detail["reclaimable_gb"] == pytest.approx(20.0)


def test_probe_docker_disk_fails_when_log_cap_unset(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess, "run", lambda *a, **k: _completed(stdout="Images\t0B (0%)\n")
    )
    monkeypatch.setattr(wd, "_app_log_cap", lambda *a, **k: None)
    ok, detail = wd.probe_docker_disk()
    assert ok is False
    assert detail["app_log_max_size"] == "UNSET"


def test_probe_oom_proximity_ok(tmp_path: Path):
    f = tmp_path / "meminfo"
    f.write_text(_meminfo_swap(4_000_000, 1_500_000, 2_000_000, 1_900_000))  # 37% avail, 5% swap
    ok, detail = wd.probe_oom_proximity(meminfo_path=str(f))
    assert ok is True
    assert detail["available_pct"] == pytest.approx(37.5)
    assert detail["swap_used_pct"] == pytest.approx(5.0)


def test_probe_oom_proximity_fails_low_available(tmp_path: Path):
    f = tmp_path / "meminfo"
    f.write_text(_meminfo_swap(4_000_000, 300_000, 2_000_000, 2_000_000))  # 7.5% avail
    ok, detail = wd.probe_oom_proximity(meminfo_path=str(f))
    assert ok is False
    assert detail["available_pct"] == pytest.approx(7.5)


def test_probe_oom_proximity_fails_high_swap(tmp_path: Path):
    f = tmp_path / "meminfo"
    f.write_text(_meminfo_swap(4_000_000, 2_000_000, 2_000_000, 200_000))  # 50% avail, 90% swap
    ok, detail = wd.probe_oom_proximity(meminfo_path=str(f))
    assert ok is False
    assert detail["swap_used_pct"] == pytest.approx(90.0)


def test_probe_restart_drift_ok(monkeypatch):
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: _completed(stdout="0\n"))
    ok, detail = wd.probe_restart_drift(names=("c1", "c2"))
    assert ok is True
    assert detail["counts"] == {"c1": 0, "c2": 0}


def test_probe_restart_drift_fails_over_threshold(monkeypatch):
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: _completed(stdout="9\n"))
    ok, detail = wd.probe_restart_drift(names=("c1",))
    assert ok is False
    assert detail["counts"]["c1"] == 9


def test_probe_restart_drift_inspect_error_is_not_fatal(monkeypatch):
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: _completed(stdout="", returncode=1))
    ok, detail = wd.probe_restart_drift(names=("c1",))
    assert ok is True  # an inspect failure does not falsely trip the drift yellow
    assert detail["counts"]["c1"] == "err:inspect"


def test_parse_last_bar_age():
    assert wd._parse_last_bar_age("warmup=ready last_bar=42s commit=abc") == 42
    assert wd._parse_last_bar_age("no age here") is None


# ============================================================================
# 11. attempt_auto_heal — 3-per-hour cap
# ============================================================================


def test_attempt_auto_heal_first_attempt_calls_restart(monkeypatch):
    mock_run = MagicMock(return_value=_completed(returncode=0))
    monkeypatch.setattr(wd.subprocess, "run", mock_run)
    sleep_called: list[float] = []
    state: dict = {"auto_heal_history": []}
    ok, msg = wd.attempt_auto_heal(state, now_fn=_fixed_now(), sleep_fn=sleep_called.append)
    assert ok is True
    assert "attempt 1/3" in msg
    mock_run.assert_called_once_with(
        ["docker", "restart", "tradeflow-ib-gateway"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert sleep_called == [float(wd.AUTO_HEAL_WAIT_SEC)]
    assert len(state["auto_heal_history"]) == 1


def test_attempt_auto_heal_three_recent_attempts_blocks(monkeypatch):
    mock_run = MagicMock(return_value=_completed(returncode=0))
    monkeypatch.setattr(wd.subprocess, "run", mock_run)
    now = _fixed_now()
    state: dict = {
        "auto_heal_history": [
            {"at": (now() - timedelta(minutes=m)).isoformat(), "result": "restart_issued"}
            for m in (5, 25, 45)
        ]
    }
    ok, msg = wd.attempt_auto_heal(state, now_fn=now, sleep_fn=lambda _s: None)
    assert ok is False
    assert "max attempts exceeded" in msg
    mock_run.assert_not_called()


def test_attempt_auto_heal_old_attempts_evicted(monkeypatch):
    """Attempts older than the 60-min window are pruned; counter resets."""
    mock_run = MagicMock(return_value=_completed(returncode=0))
    monkeypatch.setattr(wd.subprocess, "run", mock_run)
    now = _fixed_now()
    state: dict = {
        "auto_heal_history": [
            {"at": (now() - timedelta(minutes=m)).isoformat(), "result": "restart_issued"}
            # all 3 entries > 60 min ago → window empty after prune
            for m in (90, 120, 150)
        ]
    }
    ok, msg = wd.attempt_auto_heal(state, now_fn=now, sleep_fn=lambda _s: None)
    assert ok is True
    assert "attempt 1/3" in msg
    mock_run.assert_called_once()


def test_attempt_auto_heal_restart_failure(monkeypatch):
    mock_run = MagicMock(return_value=_completed(returncode=1, stderr="docker daemon down"))
    monkeypatch.setattr(wd.subprocess, "run", mock_run)
    state: dict = {"auto_heal_history": []}
    ok, msg = wd.attempt_auto_heal(state, now_fn=_fixed_now(), sleep_fn=lambda _s: None)
    assert ok is False
    assert "docker restart failed" in msg


# ============================================================================
# 12. send_telegram — no parse_mode
# ============================================================================


def test_send_telegram_success_posts_correct_fields(monkeypatch):
    mock_post = MagicMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr(wd.httpx, "post", mock_post)
    ok = wd.send_telegram("hello", token="TOK", chat_id="CHAT")
    assert ok is True
    call_args = mock_post.call_args
    assert call_args.args[0] == "https://api.telegram.org/botTOK/sendMessage"
    data = call_args.kwargs["data"]
    assert data["chat_id"] == "CHAT"
    assert data["text"] == "hello"
    assert "parse_mode" not in data  # §0.5.143


def test_send_telegram_http_error_returns_false(monkeypatch):
    monkeypatch.setattr(wd.httpx, "post", MagicMock(side_effect=httpx.ConnectError("refused")))
    ok = wd.send_telegram("hi", token="TOK", chat_id="CHAT")
    assert ok is False


def test_send_telegram_non_200_returns_false(monkeypatch):
    resp = MagicMock(status_code=429, text="rate limited")
    monkeypatch.setattr(wd.httpx, "post", MagicMock(return_value=resp))
    ok = wd.send_telegram("hi", token="TOK", chat_id="CHAT")
    assert ok is False


def test_send_telegram_missing_creds_returns_false(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OPERATOR_CHAT_ID", raising=False)
    ok = wd.send_telegram("hi")
    assert ok is False


# ============================================================================
# 13. run_monitor integration — all-green and IB-fail paths
# ============================================================================


def _patch_all_green(monkeypatch):
    """Monkeypatch every probe to succeed; returns the telegram mock so the test can assert."""
    monkeypatch.setattr(wd, "probe_ib_api", lambda h, p, c: (True, "server_version=178"))
    monkeypatch.setattr(
        wd,
        "probe_container",
        lambda name: (True, {"status": "running", "health": "healthy", "restart_count": 1}),
    )
    monkeypatch.setattr(wd, "probe_supabase", lambda u, k: (True, "http 200"))
    monkeypatch.setattr(wd, "probe_dashboard", lambda: (True, "http 401 (auth-gated, server live)"))
    monkeypatch.setattr(wd, "probe_disk", lambda paths=None: (True, {"/": 20}))
    monkeypatch.setattr(wd, "probe_memory", lambda meminfo_path=None: (True, {"used_pct": 30.0}))
    mock_telegram = MagicMock(return_value=True)
    monkeypatch.setattr(wd, "send_telegram", mock_telegram)
    return mock_telegram


def test_run_monitor_all_green_sends_no_alerts(monkeypatch, tmp_state):
    mock_telegram = _patch_all_green(monkeypatch)
    rc = wd.run_monitor()
    assert rc == 0
    mock_telegram.assert_not_called()
    saved = json.loads((tmp_state / "state.json").read_text())
    assert saved["alert_history"] == {}


def test_run_monitor_ib_down_first_occurrence_alerts_and_auto_heals(monkeypatch, tmp_state):
    _patch_all_green(monkeypatch)
    monkeypatch.setattr(wd, "probe_ib_api", lambda h, p, c: (False, "TimeoutError"))
    auto_heal_calls: list[dict] = []

    def fake_auto_heal(state, now_fn=None, sleep_fn=None):
        auto_heal_calls.append({"state_id": id(state)})
        state.setdefault("auto_heal_history", []).append({"at": "now", "result": "restart_issued"})
        return (True, "docker restart issued (attempt 1/3)")

    monkeypatch.setattr(wd, "attempt_auto_heal", fake_auto_heal)
    mock_telegram = MagicMock(return_value=True)
    monkeypatch.setattr(wd, "send_telegram", mock_telegram)

    rc = wd.run_monitor()
    assert rc == 0
    assert len(auto_heal_calls) == 1
    # at least the IB alert + a post-heal status message should have been sent
    assert mock_telegram.call_count >= 1
    saved = json.loads((tmp_state / "state.json").read_text())
    assert wd.ALERT_IB_API_DOWN in saved["alert_history"]


def test_run_monitor_ib_recovery_clears_alert_and_notifies(monkeypatch, tmp_state):
    _patch_all_green(monkeypatch)
    seeded = {
        "alert_history": {wd.ALERT_IB_API_DOWN: "2026-01-01T00:00:00+00:00"},
        "auto_heal_history": [],
        "last_restart_counts": {},
    }
    (tmp_state / "state.json").write_text(json.dumps(seeded))
    mock_telegram = MagicMock(return_value=True)
    monkeypatch.setattr(wd, "send_telegram", mock_telegram)

    rc = wd.run_monitor()
    assert rc == 0
    saved = json.loads((tmp_state / "state.json").read_text())
    assert wd.ALERT_IB_API_DOWN not in saved["alert_history"]
    # RECOVERED message should have fired
    assert any("RECOVERED" in str(c) for c in mock_telegram.call_args_list)


# ============================================================================
# PR #31 additions
# ============================================================================


@pytest.fixture
def clean_logger():
    """Reset LOGGER.handlers around each test so configure_logging() can re-run."""
    original = list(wd.LOGGER.handlers)
    wd.LOGGER.handlers = []
    yield
    wd.LOGGER.handlers = original


# ---- Fix B: TTY-conditional StreamHandler -----------------------------------


def test_configure_logging_tty_adds_stream_handler(monkeypatch, tmp_state, clean_logger):
    monkeypatch.setattr(wd, "_is_tty", lambda: True)
    monkeypatch.delenv("WATCHDOG_FORCE_STREAM", raising=False)
    wd.configure_logging()
    types = [type(h) for h in wd.LOGGER.handlers]
    assert logging.StreamHandler in types


def test_configure_logging_no_tty_omits_stream_handler(monkeypatch, tmp_state, clean_logger):
    monkeypatch.setattr(wd, "_is_tty", lambda: False)
    monkeypatch.delenv("WATCHDOG_FORCE_STREAM", raising=False)
    wd.configure_logging()
    types = [type(h) for h in wd.LOGGER.handlers]
    # FileHandler is still added; only the plain StreamHandler is suppressed.
    assert logging.StreamHandler not in types
    assert any("TimedRotating" in t.__name__ for t in types)


def test_configure_logging_force_stream_env_overrides(monkeypatch, tmp_state, clean_logger):
    monkeypatch.setattr(wd, "_is_tty", lambda: False)
    monkeypatch.setenv("WATCHDOG_FORCE_STREAM", "1")
    wd.configure_logging()
    types = [type(h) for h in wd.LOGGER.handlers]
    assert logging.StreamHandler in types


# ---- Fix C: IB recovery log line --------------------------------------------


def test_run_monitor_ib_recovery_logs_info_line(monkeypatch, tmp_state, caplog):
    """Cycle-driven IB recovery must emit the canonical recovery: log line."""
    _patch_all_green(monkeypatch)
    seeded = {
        "alert_history": {wd.ALERT_IB_API_DOWN: "2026-01-01T00:00:00+00:00"},
        "auto_heal_history": [],
        "last_restart_counts": {},
    }
    (tmp_state / "state.json").write_text(json.dumps(seeded))
    monkeypatch.setattr(wd, "send_telegram", MagicMock(return_value=True))

    with caplog.at_level(logging.INFO, logger=wd.LOGGER.name):
        wd.run_monitor()

    msgs = [r.getMessage() for r in caplog.records]
    assert any("recovery: ib_api_down" in m for m in msgs), msgs


# ---- Fix D: app_restart_loop dedup -----------------------------------------


def _seed_restart_loop_alert(
    tmp_state: Path,
    *,
    stable_cycles: int,
    cur_count: int = 8,
) -> dict:
    """Seed state.json with an active app_restart_loop alert + stable counter."""
    seeded = {
        "alert_history": {
            wd.ALERT_APP_RESTART_LOOP: datetime.now(UTC).isoformat(),
        },
        "auto_heal_history": [],
        "last_restart_counts": {"tradeflow-app": cur_count},
        "restart_loop_stable_cycles": stable_cycles,
    }
    (tmp_state / "state.json").write_text(json.dumps(seeded))
    return seeded


def _patch_restart_loop_probes(monkeypatch, *, cur_count: int) -> MagicMock:
    """All green probes, with tradeflow-app's restart_count pinned."""
    monkeypatch.setattr(wd, "probe_ib_api", lambda h, p, c: (True, "server_version=178"))

    def app_probe(name):
        if name == "tradeflow-app":
            return (True, {"status": "running", "health": "healthy", "restart_count": cur_count})
        return (True, {"status": "running", "health": "healthy", "restart_count": 0})

    monkeypatch.setattr(wd, "probe_container", app_probe)
    monkeypatch.setattr(wd, "probe_supabase", lambda u, k: (True, "http 200"))
    monkeypatch.setattr(wd, "probe_dashboard", lambda: (True, "http 401"))
    monkeypatch.setattr(wd, "probe_disk", lambda paths=None: (True, {"/": 20}))
    monkeypatch.setattr(wd, "probe_memory", lambda meminfo_path=None: (True, {"used_pct": 30.0}))
    mock_telegram = MagicMock(return_value=True)
    monkeypatch.setattr(wd, "send_telegram", mock_telegram)
    return mock_telegram


def test_app_restart_loop_persists_after_single_stable_cycle(monkeypatch, tmp_state):
    """One delta=0 cycle is NOT enough to clear the alert — the prior bug."""
    _seed_restart_loop_alert(tmp_state, stable_cycles=0, cur_count=8)
    mock_telegram = _patch_restart_loop_probes(monkeypatch, cur_count=8)

    wd.run_monitor()

    saved = json.loads((tmp_state / "state.json").read_text())
    assert wd.ALERT_APP_RESTART_LOOP in saved["alert_history"], "alert cleared too eagerly"
    assert saved["restart_loop_stable_cycles"] == 1
    # No RECOVERED Telegram for restart-loop after a single stable cycle.
    msgs = [str(c) for c in mock_telegram.call_args_list]
    assert not any("restart loop stopped" in m for m in msgs), msgs


def test_app_restart_loop_clears_after_three_stable_cycles(monkeypatch, tmp_state):
    """At stable_cycles=2 going to 3, the alert clears and RECOVERED fires once."""
    _seed_restart_loop_alert(tmp_state, stable_cycles=2, cur_count=8)
    mock_telegram = _patch_restart_loop_probes(monkeypatch, cur_count=8)

    wd.run_monitor()

    saved = json.loads((tmp_state / "state.json").read_text())
    assert wd.ALERT_APP_RESTART_LOOP not in saved["alert_history"]
    assert saved["restart_loop_stable_cycles"] == 0
    msgs = [str(c) for c in mock_telegram.call_args_list]
    assert sum("restart loop stopped" in m for m in msgs) == 1, msgs


def test_app_restart_loop_counter_resets_on_re_escalation(monkeypatch, tmp_state):
    """Mid-cooldown new escalation: counter must reset to 0; alert deduped."""
    _seed_restart_loop_alert(tmp_state, stable_cycles=2, cur_count=5)
    # cur_count jumps from 5 → 11 → delta = 6 (≥ threshold)
    mock_telegram = _patch_restart_loop_probes(monkeypatch, cur_count=11)

    wd.run_monitor()

    saved = json.loads((tmp_state / "state.json").read_text())
    assert saved["restart_loop_stable_cycles"] == 0
    # dedup still suppresses the resend within the 15-min window, so no new
    # ALERT message; but the alert remains active in state.
    assert wd.ALERT_APP_RESTART_LOOP in saved["alert_history"]
    msgs = [str(c) for c in mock_telegram.call_args_list]
    assert not any("restart loop stopped" in m for m in msgs), msgs


def test_app_restart_loop_intermediate_stable_cycle_logs_progress(monkeypatch, tmp_state, caplog):
    """During the cooldown, watchdog logs the stable-cycle count for observability."""
    _seed_restart_loop_alert(tmp_state, stable_cycles=1, cur_count=8)
    _patch_restart_loop_probes(monkeypatch, cur_count=8)

    with caplog.at_level(logging.INFO, logger=wd.LOGGER.name):
        wd.run_monitor()

    msgs = [r.getMessage() for r in caplog.records]
    assert any("app_restart_loop: stable cycle 2/" in m for m in msgs), msgs


def test_default_state_includes_stable_cycles_counter():
    s = wd._default_state()
    assert s["restart_loop_stable_cycles"] == 0


def test_load_state_backfills_stable_cycles_on_old_state(tmp_state):
    """Migration: pre-PR-31 state files (no restart_loop_stable_cycles) get 0."""
    (tmp_state / "state.json").write_text(
        json.dumps({"alert_history": {}, "auto_heal_history": [], "last_restart_counts": {}})
    )
    state = wd.load_state()
    assert state["restart_loop_stable_cycles"] == 0


# ============================================================================
# W-S14.2 Track 5c — daily report TRADING section + Fix 5 logging
# ============================================================================


def test_run_daily_report_includes_trading_section(monkeypatch, caplog):
    captured: dict = {}
    monkeypatch.setattr(wd, "send_telegram", lambda msg, *a, **k: captured.update(msg=msg) or True)
    monkeypatch.setattr(wd, "_ib_env", lambda: ("h", 4002, 96))
    monkeypatch.setattr(wd, "_supabase_env", lambda: ("https://x", "key"))
    monkeypatch.setattr(wd, "probe_ib_api", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(wd, "probe_container", lambda name: (True, {"restart_count": 0}))
    monkeypatch.setattr(wd, "probe_supabase", lambda *a, **k: (True, "http 200"))
    monkeypatch.setattr(wd, "probe_dashboard", lambda *a, **k: (True, "http 200"))
    monkeypatch.setattr(wd, "probe_disk", lambda *a, **k: (True, {"/": 13}))
    monkeypatch.setattr(wd, "probe_memory", lambda *a, **k: (True, {"used_pct": 30.0}))
    monkeypatch.setattr(wd, "probe_docker_disk", lambda *a, **k: (True, {"reclaimable_gb": 0.5}))
    monkeypatch.setattr(wd, "probe_oom_proximity", lambda *a, **k: (True, {"available_pct": 40.0}))
    monkeypatch.setattr(wd, "probe_restart_drift", lambda *a, **k: (True, {"counts": {}}))
    monkeypatch.setattr(wd, "_last_app_log_line", lambda: "tail")
    monkeypatch.setattr(wd, "_count_lifecycles_today", lambda u, k: "2")
    monkeypatch.setattr(wd, "_realized_pnl_today", lambda u, k: "$599.52 (1 closed)")
    monkeypatch.setattr(
        wd, "_open_position_summary", lambda u, k: "MNQM6 LONG x2 @30413.75 (ACTIVE)"
    )
    monkeypatch.setattr(
        wd, "_seanbot_scorecard_today", lambda u, k: "3 entries → 1 AGREE, 2 MISS-filter:touch"
    )

    with caplog.at_level(logging.INFO, logger="tradeflow_watchdog"):
        rc = wd.run_daily_report()

    assert rc == 0
    msg = captured["msg"]
    assert "TRADING:" in msg
    assert "Realized P&L today: $599.52 (1 closed)" in msg
    assert "Open position (DB view): MNQM6 LONG x2 @30413.75 (ACTIVE)" in msg
    assert "SeanBot scorecard: 3 entries → 1 AGREE, 2 MISS-filter:touch" in msg
    assert "Lifecycles today: 2" in msg
    # Fix 5 — the lifecycle-count query result is logged (0 vs failure distinguishable).
    assert any("lifecycles_today_query=2" in r.getMessage() for r in caplog.records)


# ============================================================================
# W-S14.2 Track 5c fix — data reads bypass RLS via the service-role key
# ============================================================================


def test_supabase_data_env_prefers_service_role(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service")
    url, key = wd._supabase_data_env()
    assert url == "https://x"
    assert key == "service"  # NOT anon — RLS deny-all returns [] to anon


def test_supabase_data_env_falls_back_to_anon(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    _, key = wd._supabase_data_env()
    assert key == "anon"


def test_daily_report_uses_service_role_for_data_reads(monkeypatch):
    """The lifecycle/P&L/position helpers must receive the service-role key."""
    seen_keys: list = []
    monkeypatch.setattr(wd, "send_telegram", lambda *a, **k: True)
    monkeypatch.setattr(wd, "_ib_env", lambda: ("h", 4002, 96))
    monkeypatch.setattr(wd, "_supabase_env", lambda: ("https://x", "anon"))
    monkeypatch.setattr(wd, "_supabase_data_env", lambda: ("https://x", "service"))
    monkeypatch.setattr(wd, "probe_ib_api", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(wd, "probe_container", lambda name: (True, {"restart_count": 0}))
    monkeypatch.setattr(wd, "probe_supabase", lambda *a, **k: (True, "http 200"))
    monkeypatch.setattr(wd, "probe_dashboard", lambda *a, **k: (True, "http 200"))
    monkeypatch.setattr(wd, "probe_disk", lambda *a, **k: (True, {"/": 13}))
    monkeypatch.setattr(wd, "probe_memory", lambda *a, **k: (True, {"used_pct": 30.0}))
    monkeypatch.setattr(wd, "probe_docker_disk", lambda *a, **k: (True, {"reclaimable_gb": 0.5}))
    monkeypatch.setattr(wd, "probe_oom_proximity", lambda *a, **k: (True, {"available_pct": 40.0}))
    monkeypatch.setattr(wd, "probe_restart_drift", lambda *a, **k: (True, {"counts": {}}))
    monkeypatch.setattr(wd, "_last_app_log_line", lambda: "tail")
    monkeypatch.setattr(wd, "_seanbot_scorecard_today", lambda u, k: "0 entries today")
    monkeypatch.setattr(wd, "_count_lifecycles_today", lambda u, k: seen_keys.append(k) or "2")
    monkeypatch.setattr(wd, "_realized_pnl_today", lambda u, k: seen_keys.append(k) or "$0")
    monkeypatch.setattr(wd, "_open_position_summary", lambda u, k: seen_keys.append(k) or "FLAT")

    assert wd.run_daily_report() == 0
    assert seen_keys == ["service", "service", "service"]  # never the anon key


# ============================================================================
# W-S15.2 — SeanBot scorecard reads the durable signal_reconciliations table
# ============================================================================


def test_seanbot_scorecard_aggregates_supabase_rows(monkeypatch):
    """Counts must match the legacy JSONL aggregation for the same input:
    AGREE_ENTER -> AGREE; MISS-* grouped and sorted."""
    rows = [
        {"classification": "AGREE_ENTER", "signal_ts": "2026-05-29T14:00:00+00:00"},
        {"classification": "MISS-filter:touch", "signal_ts": "2026-05-29T14:01:00+00:00"},
        {"classification": "MISS-filter:touch", "signal_ts": "2026-05-29T14:02:00+00:00"},
    ]
    resp = MagicMock(status_code=200, json=lambda: rows)
    monkeypatch.setattr(wd.httpx, "get", MagicMock(return_value=resp))
    out = wd._seanbot_scorecard_today("https://x", "service")
    assert out == "3 entries → 1 AGREE, 2 MISS-filter:touch"


def test_seanbot_scorecard_empty_table_is_zero_not_error(monkeypatch):
    resp = MagicMock(status_code=200, json=lambda: [])
    monkeypatch.setattr(wd.httpx, "get", MagicMock(return_value=resp))
    assert wd._seanbot_scorecard_today("https://x", "service") == "0 entries today"


def test_seanbot_scorecard_missing_creds_returns_placeholder(monkeypatch):
    # No network call when creds are absent.
    monkeypatch.setattr(
        wd.httpx, "get", MagicMock(side_effect=AssertionError("should not be called"))
    )
    assert wd._seanbot_scorecard_today(None, None) == "?"


def test_seanbot_scorecard_http_error_is_unavailable(monkeypatch):
    resp = MagicMock(status_code=500, json=lambda: [])
    monkeypatch.setattr(wd.httpx, "get", MagicMock(return_value=resp))
    assert wd._seanbot_scorecard_today("https://x", "service") == "(unavailable: http 500)"


# --------------------------------------------------- W-S15.3 Track E readiness


def test_deployed_commit_from_env_extracts_short_hash():
    env = ["PATH=/usr/bin", "TRADEFLOW_COMMIT=94245ad1163bf2446fc204d", "TZ=UTC"]
    assert wd._deployed_commit_from_env(env) == "94245ad1"


def test_deployed_commit_from_env_missing_returns_unknown():
    assert wd._deployed_commit_from_env(["PATH=/usr/bin"]) == "unknown"
    assert wd._deployed_commit_from_env(["TRADEFLOW_COMMIT="]) == "unknown"


def test_parse_digest_readiness_extracts_fragment():
    logs = (
        "2026-05-29 18:00 INFO [DIGEST] hourly_session_digest: \U0001f4ca TradeFlow hourly "
        "17:00-18:00Z | pos=FLAT | evals=60 | feed OK | "
        "warmup=ready (120 bars) last_bar=4s commit=94245ad1\n"
        "2026-05-29 18:01 INFO [STRAT] eval decision=noop_warmup\n"
    )
    assert wd._parse_digest_readiness(logs) == (
        "warmup=ready (120 bars) last_bar=4s commit=94245ad1"
    )


def test_parse_digest_readiness_takes_latest_digest():
    logs = (
        "[DIGEST] hourly_session_digest: ... "
        "warmup=warming 10/100 bars (~90m) last_bar=2s commit=aaa\n"
        "[DIGEST] hourly_session_digest: ... warmup=ready (110 bars) last_bar=1s commit=bbb\n"
    )
    out = wd._parse_digest_readiness(logs)
    assert "ready (110 bars)" in out
    assert "warming" not in out


def test_parse_digest_readiness_no_digest_placeholder():
    assert "no digest yet" in wd._parse_digest_readiness("some unrelated log line\n")


def test_count_reconnect_events_counts_both_markers():
    # Lines were demoted from [ALERT] to [FEED] (episode-scoped alerting); the
    # daily-report canary now counts the [FEED] variants.
    logs = (
        "[FEED] reconnect_recovered: elapsed_sec=3.1 (socket only — bars unconfirmed)\n"
        "noise\n"
        "[FEED] reconnect_recovered: elapsed_sec=5.0 (socket only — bars unconfirmed)\n"
        "[FEED] bar_sub_resubscribed_after_farm_flap: elapsed_sec=2.0\n"
    )
    assert wd._count_reconnect_events(logs) == (2, 1)


def test_count_reconnect_events_none():
    assert wd._count_reconnect_events("quiet logs\n") == (0, 0)


# ============================================================================
# feed-episode gateway-restart handoff (silent-feed-leak escalation)
# ============================================================================


def test_detect_feed_gateway_restart_needed_true_when_handoff_open():
    logs = (
        "some app noise\n"
        "[FEED] feed_episode_gateway_restart_needed — app self-heals exhausted (5 cycles)\n"
        "more noise\n"
    )
    assert wd.detect_feed_gateway_restart_needed(logs) is True


def test_detect_feed_gateway_restart_needed_false_when_recovered_after():
    logs = (
        "[FEED] feed_episode_gateway_restart_needed — exhausted\n"
        "[ALERT] feed_episode_recovered\n"  # recovery AFTER the handoff → resolved
    )
    assert wd.detect_feed_gateway_restart_needed(logs) is False


def test_detect_feed_gateway_restart_needed_false_when_no_marker():
    assert wd.detect_feed_gateway_restart_needed("nothing relevant here\n") is False


def test_detect_feed_reopened_after_recovery_is_true():
    # open -> recovered -> open again + handoff again => still needs restart.
    logs = (
        "[FEED] feed_episode_gateway_restart_needed — first episode\n"
        "[ALERT] feed_episode_recovered\n"
        "[FEED] feed_episode_gateway_restart_needed — second episode\n"
    )
    assert wd.detect_feed_gateway_restart_needed(logs) is True


def _handoff_logs() -> str:
    return "[FEED] feed_episode_gateway_restart_needed — app self-heals exhausted (5 cycles)\n"


def test_handle_feed_stale_triggers_gateway_restart_exactly_once():
    """A detected handoff issues exactly one gateway-restart auto-heal + one alert."""
    state: dict = {}
    auto_heal = MagicMock(return_value=(True, "docker restart issued (attempt 1/3)"))
    send = MagicMock(return_value=True)

    status = wd.handle_feed_stale_episode(
        state,
        _handoff_logs(),
        auto_heal=auto_heal,
        send=send,
        now_fn=_fixed_now(),
    )

    assert status == "gateway_restart_issued"
    auto_heal.assert_called_once_with(state)
    # Exactly one Telegram message (the stale/restarting alert).
    assert send.call_count == 1
    assert "Restarting ib-gateway" in send.call_args[0][0]
    assert wd.ALERT_BAR_FEED_STALE in state["alert_history"]


def test_handle_feed_stale_dedupes_alert_but_still_heals():
    """A second cycle within the dedup window must NOT re-alert, but still heals
    (the 3/hr auto-heal cap — not the alert — bounds restarts)."""
    now = _fixed_now()
    state: dict = {"alert_history": {wd.ALERT_BAR_FEED_STALE: now().isoformat()}}
    auto_heal = MagicMock(return_value=(True, "docker restart issued (attempt 2/3)"))
    send = MagicMock(return_value=True)

    status = wd.handle_feed_stale_episode(
        state, _handoff_logs(), auto_heal=auto_heal, send=send, now_fn=now
    )

    assert status == "gateway_restart_issued"
    auto_heal.assert_called_once()
    send.assert_not_called()  # deduped within ALERT_DEDUP_MINUTES


def _exhausted_auto_heal() -> MagicMock:
    return MagicMock(
        return_value=(False, "max attempts exceeded (3/3 in last 60min) — manual intervention")
    )


def test_handle_feed_stale_pages_expiry_suspected_when_gateway_healthy():
    # PR #171 (a): stale + gateway healthy + restart exhausted → the terminal page
    # NAMES contract expiry as the suspected cause (a restart can't fix that).
    state: dict = {}
    auto_heal = _exhausted_auto_heal()
    send = MagicMock(return_value=True)

    status = wd.handle_feed_stale_episode(
        state,
        _handoff_logs(),
        auto_heal=auto_heal,
        send=send,
        gateway_healthy=True,
        now_fn=_fixed_now(),
    )

    assert status == "manual_intervention_expiry"
    # Two messages: the restart alert + the MANUAL INTERVENTION page.
    assert send.call_count == 2
    manual = [c[0][0] for c in send.call_args_list if "MANUAL INTERVENTION" in c[0][0]]
    assert len(manual) == 1
    assert "contract expiry suspected" in manual[0]
    assert "front-month" in manual[0]
    assert wd.ALERT_MANUAL_INTERVENTION in state["alert_history"]


def test_handle_feed_stale_generic_manual_when_gateway_not_healthy():
    # PR #171 (b): a different terminal cause (gateway not healthy) → GENERIC page,
    # NOT the expiry-suspected hint (false hints erode channel trust).
    state: dict = {}
    auto_heal = _exhausted_auto_heal()
    send = MagicMock(return_value=True)

    status = wd.handle_feed_stale_episode(
        state,
        _handoff_logs(),
        auto_heal=auto_heal,
        send=send,
        gateway_healthy=False,
        now_fn=_fixed_now(),
    )

    assert status == "manual_intervention"
    manual = [c[0][0] for c in send.call_args_list if "MANUAL INTERVENTION" in c[0][0]]
    assert len(manual) == 1
    assert "contract expiry suspected" not in manual[0]


def test_handle_feed_stale_terminal_page_deduped_once_per_episode():
    # PR #171 (c): the terminal page is de-duped — a second cycle within the dedup
    # window must NOT re-page (one terminal alert per episode).
    now = _fixed_now()
    state: dict = {
        "alert_history": {
            wd.ALERT_BAR_FEED_STALE: now().isoformat(),
            wd.ALERT_MANUAL_INTERVENTION: now().isoformat(),
        }
    }
    auto_heal = _exhausted_auto_heal()
    send = MagicMock(return_value=True)

    status = wd.handle_feed_stale_episode(
        state, _handoff_logs(), auto_heal=auto_heal, send=send, gateway_healthy=True, now_fn=now
    )

    assert status == "manual_intervention_expiry"
    send.assert_not_called()  # both stale + manual alerts already deduped this episode


def test_handle_feed_stale_clears_state_on_recovery_no_telegram():
    """When the marker is gone (episode resolved) prior stale state is cleared
    silently — the app owns the feed_episode_recovered Telegram."""
    state: dict = {"alert_history": {wd.ALERT_BAR_FEED_STALE: _fixed_now()().isoformat()}}
    send = MagicMock(return_value=True)
    auto_heal = MagicMock()

    status = wd.handle_feed_stale_episode(
        state, "healthy logs, no marker\n", auto_heal=auto_heal, send=send, now_fn=_fixed_now()
    )

    assert status == "feed_recovered_cleared"
    auto_heal.assert_not_called()
    send.assert_not_called()
    assert wd.ALERT_BAR_FEED_STALE not in state.get("alert_history", {})


def test_handle_feed_stale_noop_when_feed_healthy():
    state: dict = {}
    send = MagicMock()
    auto_heal = MagicMock()
    status = wd.handle_feed_stale_episode(
        state, "all good\n", auto_heal=auto_heal, send=send, now_fn=_fixed_now()
    )
    assert status == "feed_ok"
    auto_heal.assert_not_called()
    send.assert_not_called()
