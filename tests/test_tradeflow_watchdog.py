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
    """Redirect STATE_DIR / STATE_FILE to a per-test temp dir."""
    monkeypatch.setattr(wd, "STATE_DIR", tmp_path)
    monkeypatch.setattr(wd, "STATE_FILE", tmp_path / "state.json")
    return tmp_path


# ============================================================================
# 1. load_state
# ============================================================================


def test_load_state_missing_returns_defaults(tmp_state):
    state = wd.load_state()
    assert state == {"alert_history": {}, "auto_heal_history": [], "last_restart_counts": {}}


def test_load_state_corrupt_json_returns_defaults(tmp_state, caplog):
    (tmp_state / "state.json").write_text("{this is not json")
    with caplog.at_level("WARNING"):
        state = wd.load_state()
    assert state == {"alert_history": {}, "auto_heal_history": [], "last_restart_counts": {}}
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
    ok, detail = wd.probe_ib_api("127.0.0.1", 4002, 96, timeout=1.0)
    assert ok is False
    assert "TimeoutError" in detail


def test_probe_ib_api_reqcurrenttime_timeout(monkeypatch):
    mock_ib = _ib_mock(time_ok=False)
    monkeypatch.setattr(wd, "IB", lambda: mock_ib)
    ok, detail = wd.probe_ib_api("127.0.0.1", 4002, 96, timeout=1.0)
    assert ok is False
    assert "TimeoutError" in detail


def test_probe_ib_api_missing_server_version(monkeypatch):
    mock_ib = _ib_mock(server_version=0)
    monkeypatch.setattr(wd, "IB", lambda: mock_ib)
    ok, detail = wd.probe_ib_api("127.0.0.1", 4002, 96, timeout=1.0)
    assert ok is False
    assert "missing server version" in detail


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
