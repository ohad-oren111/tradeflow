"""TradeFlow external watchdog — runs OUTSIDE Docker; survives container death.

Three modes (--mode):
- monitor: every 60s via cron, probes + alerts + auto-heals
- daily-report: once per weekday at 09:00 ET via cron, sends affirmative health summary
- self-test: manually invoked during install to verify alert path works

State persisted at ~/.tradeflow-watchdog/state.json:
- alert_history per alert type (for 15-min dedup)
- auto_heal_history (for 3-per-hour cap)
- last_restart_counts per container (for restart-loop delta detection)

This module is intentionally standalone: it does NOT import from src.*, comms.*,
dashboard.*, or config.* — the watchdog runs in a separate host venv
(~/tradeflow/.watchdog-venv/) and must survive any repo-layout churn.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from ib_async import IB

ENV_PATH = Path.home() / ".tradeflow-secrets" / ".env"
# load_dotenv is invoked from main() rather than at import — keeps tests that
# share a Python interpreter from picking up the operator's real environment.

LOGGER = logging.getLogger("tradeflow_watchdog")
STATE_DIR = Path.home() / ".tradeflow-watchdog"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "watchdog.log"

ALERT_DEDUP_MINUTES = 15
AUTO_HEAL_WINDOW_MINUTES = 60
AUTO_HEAL_MAX_ATTEMPTS = 3
RESTART_COUNT_DELTA_THRESHOLD = 3
# After the restart-loop alert is active, require this many consecutive cycles
# with delta=0 before clearing — a single stable cycle is not enough proof
# the loop is over. PR #19 Task F surfaced multiple ALERTs firing during one
# sustained event because the prior single-cycle clear was too eager.
RESTART_LOOP_STABLE_CYCLES_TO_CLEAR = 3
DISK_PCT_THRESHOLD = 85
MEM_PCT_THRESHOLD = 90
DASHBOARD_TIMEOUT_SEC = 5
SUPABASE_TIMEOUT_SEC = 5
IB_PROBE_TIMEOUT_SEC = 10
# W-S14.2 Track 5c — retry the IB-API probe a few times with backoff before
# declaring FAIL, so a transient farm flap mid-probe doesn't page a false-FAIL.
IB_PROBE_ATTEMPTS = 3
IB_PROBE_RETRY_BACKOFF_SEC = 2.0
AUTO_HEAL_WAIT_SEC = 90

ALERT_IB_API_DOWN = "ib_api_down"
ALERT_APP_RESTART_LOOP = "app_restart_loop"
ALERT_APP_EXITED = "app_exited"
ALERT_IBGW_DOWN = "ibgw_container_down"
ALERT_SUPABASE_DOWN = "supabase_unreachable"
ALERT_DASHBOARD_DOWN = "dashboard_unreachable"
ALERT_DISK_HIGH = "disk_usage_high"
ALERT_MEM_HIGH = "memory_usage_high"
ALERT_MANUAL_INTERVENTION = "manual_intervention_needed"


def now_utc() -> datetime:
    return datetime.now(UTC)


def _default_state() -> dict:
    return {
        "alert_history": {},
        "auto_heal_history": [],
        "last_restart_counts": {},
        "restart_loop_stable_cycles": 0,
    }


def load_state() -> dict:
    """Read state.json; return defaults if missing or unparseable."""
    if not STATE_FILE.exists():
        return _default_state()
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("[WATCHDOG] load_state: corrupt — %s; using defaults", exc)
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    data.setdefault("alert_history", {})
    data.setdefault("auto_heal_history", [])
    data.setdefault("last_restart_counts", {})
    data.setdefault("restart_loop_stable_cycles", 0)
    return data


def save_state(state: dict) -> None:
    """Atomic write: tmp file → fsync → rename."""
    STATE_DIR.mkdir(mode=0o700, exist_ok=True, parents=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    with open(tmp, "rb") as handle:
        os.fsync(handle.fileno())
    tmp.replace(STATE_FILE)


def should_send_alert(
    state: dict,
    alert_type: str,
    now_fn: Callable[[], datetime] = now_utc,
) -> bool:
    """True if alert never fired OR last fired more than ALERT_DEDUP_MINUTES ago."""
    last = state.get("alert_history", {}).get(alert_type)
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last) if isinstance(last, str) else last
    except (ValueError, TypeError):
        return True
    elapsed_min = (now_fn() - last_dt).total_seconds() / 60.0
    return elapsed_min > ALERT_DEDUP_MINUTES


def record_alert(
    state: dict,
    alert_type: str,
    now_fn: Callable[[], datetime] = now_utc,
) -> None:
    state.setdefault("alert_history", {})[alert_type] = now_fn().isoformat()


def clear_alert(state: dict, alert_type: str) -> None:
    state.get("alert_history", {}).pop(alert_type, None)


async def _probe_ib_api_async(
    host: str, port: int, client_id: int, timeout: float
) -> tuple[bool, str]:
    """Connect → reqCurrentTime → disconnect. Anything short of this is the §0.5.151 trap."""
    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync(host, port, clientId=client_id, timeout=timeout),
            timeout=timeout,
        )
        if not ib.isConnected():
            return (False, "connectAsync returned but isConnected=False")
        server_version = ib.client.serverVersion()
        if not server_version:
            return (False, "connected but missing server version")
        current_time = await asyncio.wait_for(ib.reqCurrentTimeAsync(), timeout=timeout)
        time_str = (
            current_time.isoformat() if hasattr(current_time, "isoformat") else str(current_time)
        )
        return (True, f"server_version={server_version} time={time_str}")
    except TimeoutError:
        return (False, "TimeoutError")
    except ConnectionRefusedError:
        return (False, "ConnectionRefusedError")
    except Exception as exc:  # noqa: BLE001
        return (False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:  # noqa: BLE001
            pass


def probe_ib_api(
    host: str,
    port: int,
    client_id: int,
    timeout: float = IB_PROBE_TIMEOUT_SEC,
    *,
    attempts: int = IB_PROBE_ATTEMPTS,
    retry_backoff_sec: float = IB_PROBE_RETRY_BACKOFF_SEC,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Sync wrapper around the async IB probe, with retry + linear backoff.

    W-S14.2 Track 5c: the IBKR data farm flaps several times a day; a single
    failed probe mid-flap previously declared a false FAIL. Retries up to
    ``attempts`` times (returning on the first success) so the daily report and
    monitor only declare IB down after the flap has had a chance to recover.
    Each attempt runs its own event loop.
    """
    last_detail = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            ok, detail = asyncio.run(_probe_ib_api_async(host, port, client_id, timeout))
        except Exception as exc:  # noqa: BLE001
            ok, detail = (False, f"unexpected: {type(exc).__name__}: {exc}")
        if ok:
            if attempt > 1:
                return (True, f"{detail} (recovered on attempt {attempt}/{attempts})")
            return (True, detail)
        last_detail = f"{detail} [attempt {attempt}/{attempts}]"
        if attempt < attempts:
            sleep_fn(retry_backoff_sec * attempt)
    return (False, last_detail)


def probe_container(name: str) -> tuple[bool, dict]:
    """docker inspect <name>. ok = (Status=running AND Health=healthy)."""
    res = subprocess.run(
        ["docker", "inspect", name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if res.returncode != 0:
        return (False, {"error": "inspect failed", "stderr": res.stderr.strip()[:200]})
    try:
        data = json.loads(res.stdout)[0]
    except (json.JSONDecodeError, IndexError) as exc:
        return (False, {"error": f"parse: {exc}"})
    state_blk = data.get("State", {}) or {}
    status = state_blk.get("Status", "?")
    health = (state_blk.get("Health") or {}).get("Status", "?")
    restart_count = int(data.get("RestartCount", 0))
    ok = status == "running" and health == "healthy"
    return (
        ok,
        {
            "status": status,
            "health": health,
            "restart_count": restart_count,
            "started_at": state_blk.get("StartedAt", ""),
        },
    )


def probe_supabase(url: str | None, key: str | None) -> tuple[bool, str]:
    """GET lifecycles?select=*&limit=1 — pattern from scripts/_probe_supabase.py."""
    if not url or not key:
        return (False, "SUPABASE_URL or key missing from env")
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/rest/v1/lifecycles",
            params={"select": "*", "limit": "1"},
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            timeout=SUPABASE_TIMEOUT_SEC,
        )
    except httpx.TimeoutException:
        return (False, "timeout")
    except httpx.HTTPError as exc:
        return (False, f"{type(exc).__name__}: {exc}")
    if resp.status_code == 200:
        return (True, "http 200")
    return (False, f"http {resp.status_code}")


def probe_dashboard() -> tuple[bool, str]:
    """GET dashboard root. 200 OR 401 prove the server is up; everything else = down."""
    try:
        resp = httpx.get("http://127.0.0.1:8080/", timeout=DASHBOARD_TIMEOUT_SEC)
    except httpx.TimeoutException:
        return (False, "timeout")
    except httpx.HTTPError as exc:
        return (False, f"{type(exc).__name__}: {exc}")
    if resp.status_code == 200:
        return (True, "http 200")
    if resp.status_code == 401:
        return (True, "http 401 (auth-gated, server live)")
    return (False, f"http {resp.status_code}")


def probe_disk(
    paths: tuple[str, ...] = ("/", "/var/lib/docker"),
) -> tuple[bool, dict]:
    """df -P each path that exists. Fail if any > DISK_PCT_THRESHOLD."""
    details: dict[str, Any] = {}
    worst = 0
    for path in paths:
        if not Path(path).exists():
            continue
        res = subprocess.run(
            ["df", "-P", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            details[path] = "df failed"
            continue
        lines = res.stdout.strip().splitlines()
        if len(lines) < 2:
            details[path] = "no data"
            continue
        try:
            used_pct = int(lines[1].split()[4].rstrip("%"))
        except (ValueError, IndexError):
            details[path] = "parse failed"
            continue
        details[path] = used_pct
        worst = max(worst, used_pct)
    ok = worst <= DISK_PCT_THRESHOLD
    return (ok, details)


def probe_memory(meminfo_path: str | Path = "/proc/meminfo") -> tuple[bool, dict]:
    """Compute used% = (MemTotal - MemAvailable) / MemTotal from /proc/meminfo."""
    try:
        text = Path(meminfo_path).read_text()
    except OSError as exc:
        return (False, {"error": str(exc)})
    kv: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        parts = val.strip().split()
        if not parts:
            continue
        try:
            kv[key] = int(parts[0])
        except ValueError:
            continue
    total = kv.get("MemTotal", 0)
    available = kv.get("MemAvailable", 0)
    if total <= 0:
        return (False, {"error": "MemTotal not parseable"})
    used_pct = round(100.0 * (total - available) / total, 1)
    ok = used_pct <= MEM_PCT_THRESHOLD
    return (ok, {"used_pct": used_pct, "total_kb": total, "available_kb": available})


def send_telegram(
    message: str,
    token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """POST sendMessage. NO parse_mode (§0.5.143). Plain text only."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_OPERATOR_CHAT_ID")
    if not token or not chat_id:
        LOGGER.error(
            "[WATCHDOG] send_telegram: missing TELEGRAM_BOT_TOKEN or TELEGRAM_OPERATOR_CHAT_ID"
        )
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        LOGGER.error("[WATCHDOG] send_telegram: %s: %s", type(exc).__name__, exc)
        return False
    if resp.status_code == 200:
        return True
    LOGGER.error(
        "[WATCHDOG] send_telegram: http %s body=%s",
        resp.status_code,
        resp.text[:200],
    )
    return False


def attempt_auto_heal(
    state: dict,
    now_fn: Callable[[], datetime] = now_utc,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Restart ib-gateway if < AUTO_HEAL_MAX_ATTEMPTS in last AUTO_HEAL_WINDOW_MINUTES."""
    now = now_fn()
    cutoff = now - timedelta(minutes=AUTO_HEAL_WINDOW_MINUTES)
    history = state.setdefault("auto_heal_history", [])
    recent: list[dict] = []
    for entry in history:
        try:
            at_dt = datetime.fromisoformat(entry["at"])
            if at_dt >= cutoff:
                recent.append(entry)
        except (ValueError, KeyError, TypeError):
            continue
    state["auto_heal_history"] = recent
    if len(recent) >= AUTO_HEAL_MAX_ATTEMPTS:
        return (
            False,
            f"max attempts exceeded ({len(recent)}/{AUTO_HEAL_MAX_ATTEMPTS} in last "
            f"{AUTO_HEAL_WINDOW_MINUTES}min) — manual intervention needed",
        )
    attempt_num = len(recent) + 1
    LOGGER.info(
        "[WATCHDOG] auto_heal: attempting docker restart tradeflow-ib-gateway "
        "(attempt %d/%d in 60-min window)",
        attempt_num,
        AUTO_HEAL_MAX_ATTEMPTS,
    )
    res = subprocess.run(
        ["docker", "restart", "tradeflow-ib-gateway"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode != 0:
        recent.append(
            {"at": now.isoformat(), "result": f"restart_failed:{res.stderr.strip()[:100]}"}
        )
        return (False, f"docker restart failed: {res.stderr.strip()[:200]}")
    LOGGER.info(
        "[WATCHDOG] auto_heal: waiting %ds for IB Gateway to come back...", AUTO_HEAL_WAIT_SEC
    )
    sleep_fn(float(AUTO_HEAL_WAIT_SEC))
    recent.append({"at": now.isoformat(), "result": "restart_issued"})
    return (True, f"docker restart issued (attempt {attempt_num}/{AUTO_HEAL_MAX_ATTEMPTS})")


def _ib_env() -> tuple[str, int, int]:
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    client_id = int(os.environ.get("IBKR_CLIENT_ID_WATCHDOG", "96"))
    return (host, port, client_id)


def _supabase_env() -> tuple[str | None, str | None]:
    """Prefer anon key (least privilege); fall back to service-role if anon missing.

    Correct for ``probe_supabase`` (only needs a 200 — RLS deny-all still answers
    200 with an empty body), but NOT for data reads — see ``_supabase_data_env``.
    """
    return (
        os.environ.get("SUPABASE_URL"),
        os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
    )


def _supabase_data_env() -> tuple[str | None, str | None]:
    """Env for data reads (lifecycle count, realized P&L, open position).

    W-S14.2 Track 5c fix: these tables are RLS deny-all, and the anon key sees an
    empty result set (HTTP 200, ``[]``) — which silently reports 0 lifecycles /
    $0 P&L / FLAT even when there IS activity. Data reads MUST use the
    service-role key, which bypasses RLS. Falls back to anon only if service-role
    is absent (degrades to the old, visibly-empty behavior rather than crashing).
    """
    return (
        os.environ.get("SUPABASE_URL"),
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY"),
    )


def _handle_probe(
    state: dict,
    alert_type: str,
    ok: bool,
    detail: str,
    alert_msg: str,
    recovery_msg: str,
) -> None:
    """Standard alert/recovery transition handler for a single probe."""
    if not ok:
        if should_send_alert(state, alert_type):
            send_telegram(alert_msg)
            record_alert(state, alert_type)
            LOGGER.info(
                "[WATCHDOG] alert: %s — sending Telegram message (first occurrence)",
                alert_type,
            )
    else:
        if alert_type in state.get("alert_history", {}):
            send_telegram(recovery_msg)
            clear_alert(state, alert_type)
            LOGGER.info("[WATCHDOG] recovery: %s — sending Telegram message", alert_type)


def run_monitor() -> int:
    """Probe everything; alert/heal/recover; persist state."""
    state = load_state()
    host, port, client_id = _ib_env()

    ok_ib, detail_ib = probe_ib_api(host, port, client_id)
    LOGGER.info("[WATCHDOG] probe_ib_api: %s — %s", "ok" if ok_ib else "fail", detail_ib)
    if not ok_ib:
        if should_send_alert(state, ALERT_IB_API_DOWN):
            send_telegram(f"ALERT: IB API unreachable. Detail: {detail_ib}")
            record_alert(state, ALERT_IB_API_DOWN)
            LOGGER.info(
                "[WATCHDOG] alert: %s — sending Telegram message (first occurrence)",
                ALERT_IB_API_DOWN,
            )
        heal_ok, heal_msg = attempt_auto_heal(state)
        LOGGER.info("[WATCHDOG] auto_heal: %s", heal_msg)
        if not heal_ok and "max attempts exceeded" in heal_msg:
            if should_send_alert(state, ALERT_MANUAL_INTERVENTION):
                send_telegram(
                    f"MANUAL INTERVENTION NEEDED: IB API down and auto-heal exhausted "
                    f"({AUTO_HEAL_MAX_ATTEMPTS} attempts in last {AUTO_HEAL_WINDOW_MINUTES}min). "
                    f"Run: docker restart tradeflow-ib-gateway"
                )
                record_alert(state, ALERT_MANUAL_INTERVENTION)
        elif heal_ok:
            ok_ib2, detail_ib2 = probe_ib_api(host, port, client_id)
            if ok_ib2:
                LOGGER.info("[WATCHDOG] auto_heal: succeeded — IB API responsive")
                send_telegram(f"RECOVERED: IB API reachable after auto-heal — {detail_ib2}")
                clear_alert(state, ALERT_IB_API_DOWN)
                clear_alert(state, ALERT_MANUAL_INTERVENTION)
            else:
                LOGGER.info(
                    "[WATCHDOG] auto_heal: restart issued but probe still failing — %s",
                    detail_ib2,
                )
    else:
        if ALERT_IB_API_DOWN in state.get("alert_history", {}):
            send_telegram(f"RECOVERED: IB API reachable — {detail_ib}")
            clear_alert(state, ALERT_IB_API_DOWN)
            clear_alert(state, ALERT_MANUAL_INTERVENTION)
            LOGGER.info("[WATCHDOG] recovery: ib_api_down — sending Telegram message")

    ok_app, app_detail = probe_container("tradeflow-app")
    LOGGER.info(
        "[WATCHDOG] probe_container: tradeflow-app — %s (%s)",
        "ok" if ok_app else "fail",
        app_detail,
    )
    _handle_probe(
        state,
        ALERT_APP_EXITED,
        ok_app,
        str(app_detail),
        f"ALERT: tradeflow-app container unhealthy. {app_detail}",
        "RECOVERED: tradeflow-app container is healthy again",
    )

    last_counts = state.setdefault("last_restart_counts", {})
    cur_rc = int(app_detail.get("restart_count", 0)) if isinstance(app_detail, dict) else 0
    prev_rc = int(last_counts.get("tradeflow-app", cur_rc))
    delta = cur_rc - prev_rc
    stable_cycles = int(state.get("restart_loop_stable_cycles", 0))
    if delta >= RESTART_COUNT_DELTA_THRESHOLD:
        if should_send_alert(state, ALERT_APP_RESTART_LOOP):
            send_telegram(
                f"ALERT: tradeflow-app restart loop — {delta} restarts since last cycle "
                f"(count={cur_rc})"
            )
            record_alert(state, ALERT_APP_RESTART_LOOP)
        # Loop is active — any progress toward clear is invalidated.
        state["restart_loop_stable_cycles"] = 0
    elif delta == 0 and ALERT_APP_RESTART_LOOP in state.get("alert_history", {}):
        stable_cycles += 1
        state["restart_loop_stable_cycles"] = stable_cycles
        if stable_cycles >= RESTART_LOOP_STABLE_CYCLES_TO_CLEAR:
            send_telegram("RECOVERED: tradeflow-app restart loop stopped")
            clear_alert(state, ALERT_APP_RESTART_LOOP)
            state["restart_loop_stable_cycles"] = 0
            LOGGER.info("[WATCHDOG] recovery: app_restart_loop — sending Telegram message")
        else:
            LOGGER.info(
                "[WATCHDOG] app_restart_loop: stable cycle %d/%d (not yet clearing)",
                stable_cycles,
                RESTART_LOOP_STABLE_CYCLES_TO_CLEAR,
            )
    # delta > 0 but below threshold: leave alert state and stable_cycles untouched.
    last_counts["tradeflow-app"] = cur_rc

    ok_ibgw, ibgw_detail = probe_container("tradeflow-ib-gateway")
    LOGGER.info(
        "[WATCHDOG] probe_container: tradeflow-ib-gateway — %s (%s)",
        "ok" if ok_ibgw else "fail",
        ibgw_detail,
    )
    _handle_probe(
        state,
        ALERT_IBGW_DOWN,
        ok_ibgw,
        str(ibgw_detail),
        f"ALERT: tradeflow-ib-gateway container unhealthy. {ibgw_detail}",
        "RECOVERED: tradeflow-ib-gateway container is healthy again",
    )

    sb_url, sb_key = _supabase_env()
    ok_sb, sb_detail = probe_supabase(sb_url, sb_key)
    LOGGER.info("[WATCHDOG] probe_supabase: %s — %s", "ok" if ok_sb else "fail", sb_detail)
    _handle_probe(
        state,
        ALERT_SUPABASE_DOWN,
        ok_sb,
        sb_detail,
        f"ALERT: Supabase unreachable — {sb_detail}",
        "RECOVERED: Supabase reachable again",
    )

    ok_dash, dash_detail = probe_dashboard()
    LOGGER.info("[WATCHDOG] probe_dashboard: %s — %s", "ok" if ok_dash else "fail", dash_detail)
    _handle_probe(
        state,
        ALERT_DASHBOARD_DOWN,
        ok_dash,
        dash_detail,
        f"ALERT: dashboard unreachable — {dash_detail}",
        "RECOVERED: dashboard reachable again",
    )

    ok_disk, disk_detail = probe_disk()
    LOGGER.info("[WATCHDOG] probe_disk: %s — %s", "ok" if ok_disk else "fail", disk_detail)
    _handle_probe(
        state,
        ALERT_DISK_HIGH,
        ok_disk,
        str(disk_detail),
        f"ALERT: disk usage above {DISK_PCT_THRESHOLD}% — {disk_detail}",
        "RECOVERED: disk usage back below threshold",
    )

    ok_mem, mem_detail = probe_memory()
    LOGGER.info("[WATCHDOG] probe_memory: %s — %s", "ok" if ok_mem else "fail", mem_detail)
    _handle_probe(
        state,
        ALERT_MEM_HIGH,
        ok_mem,
        str(mem_detail),
        f"ALERT: memory usage above {MEM_PCT_THRESHOLD}% — {mem_detail}",
        "RECOVERED: memory usage back below threshold",
    )

    all_green = ok_ib and ok_app and ok_ibgw and ok_sb and ok_dash and ok_disk and ok_mem
    LOGGER.info("[WATCHDOG] monitor: complete — all_green=%s", str(all_green).lower())
    save_state(state)
    return 0


def _count_lifecycles_today(url: str | None, key: str | None) -> str:
    """Today's lifecycle count from Supabase, or '?' on any error."""
    if not url or not key:
        return "?"
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/rest/v1/lifecycles",
            params={"select": "lifecycle_id", "created_at": f"gte.{today_iso}T00:00:00Z"},
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}"
    if resp.status_code == 200:
        try:
            return str(len(resp.json()))
        except ValueError:
            return "parse_err"
    return f"http {resp.status_code}"


def _realized_pnl_today(url: str | None, key: str | None) -> str:
    """Sum pnl_net of lifecycles CLOSED today (UTC) from Supabase, or '?' on error."""
    if not url or not key:
        return "?"
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/rest/v1/lifecycles",
            params={
                "select": "pnl_net",
                "state": "eq.CLOSED",
                "updated_at": f"gte.{today_iso}T00:00:00Z",
            },
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}"
    if resp.status_code != 200:
        return f"http {resp.status_code}"
    try:
        rows = resp.json()
        total = sum(float(r.get("pnl_net") or 0.0) for r in rows)
        return f"${total:.2f} ({len(rows)} closed)"
    except (ValueError, TypeError):
        return "parse_err"


def _open_position_summary(url: str | None, key: str | None) -> str:
    """Non-CLOSED lifecycles from Supabase (DB-sourced). 'FLAT' if none, '?' on error.

    The broker is the true source for positions (§0.5.98); the standalone
    watchdog reports the DB view as a health signal, labelled as such.
    """
    if not url or not key:
        return "?"
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/rest/v1/lifecycles",
            params={
                "select": "symbol,direction,entry_qty,entry_price,state",
                "state": "neq.CLOSED",
            },
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}"
    if resp.status_code != 200:
        return f"http {resp.status_code}"
    try:
        rows = resp.json()
    except ValueError:
        return "parse_err"
    if not rows:
        return "FLAT"
    parts = [
        f"{r.get('symbol')} {r.get('direction')} x{r.get('entry_qty')} "
        f"@{r.get('entry_price')} ({r.get('state')})"
        for r in rows
    ]
    return "; ".join(parts)


def _seanbot_scorecard_today(url: str | None, key: str | None) -> str:
    """AGREE/MISS scorecard for today's SeanBot entries from the durable
    Supabase ``signal_reconciliations`` table (W-S15.2 — survives container
    recreates, unlike the old ephemeral JSONL journal).

    Reads with the service-role key (RLS deny-all; anon sees ``[]``). Empty
    result → "0 entries today"; an error → "(unavailable: ...)". Never raises.
    """
    if not url or not key:
        return "?"
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/rest/v1/signal_reconciliations",
            params={
                "select": "classification,signal_ts",
                "signal_ts": f"gte.{today_iso}T00:00:00Z",
            },
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        return f"(unavailable: {type(exc).__name__})"
    if resp.status_code != 200:
        return f"(unavailable: http {resp.status_code})"
    try:
        rows = resp.json()
    except ValueError:
        return "(unavailable: parse_err)"
    agree = 0
    miss: dict[str, int] = {}
    total = 0
    for rec in rows:
        total += 1
        cls = str(rec.get("classification", ""))
        if cls.startswith("AGREE"):
            agree += 1
        elif cls.startswith("MISS"):
            miss[cls] = miss.get(cls, 0) + 1
    if total == 0:
        return "0 entries today"
    miss_str = ", ".join(f"{n} {cls}" for cls, n in sorted(miss.items()))
    return f"{total} entries → {agree} AGREE" + (f", {miss_str}" if miss_str else "")


def _parse_iso_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 string to a UTC-aware datetime, or None on any failure."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _fetch_supabase_rows_today(
    url: str,
    key: str,
    table: str,
    select: str,
    ts_col: str,
    today_iso: str,
) -> list[dict] | None:
    """GET today's rows from ``table`` filtered on ``ts_col >= today`` (service-role).

    Returns the parsed rows, or None on any HTTP/transport/parse error so the
    caller can render "(unavailable)" rather than a misleading empty result.
    """
    try:
        resp = httpx.get(
            f"{url.rstrip('/')}/rest/v1/{table}",
            params={"select": select, ts_col: f"gte.{today_iso}T00:00:00Z"},
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            timeout=5.0,
        )
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200:
        return None
    try:
        rows = resp.json()
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def _tf_vs_seanbot_today(url: str | None, key: str | None, *, tolerance_sec: float = 120.0) -> str:
    """TF-vs-SeanBot AGREE / TF-only / SeanBot-only digest for today (PR-D3c).

    Joins the two durable directions:
      * ``strategy_decisions`` — TF's own entries + near-misses (TF→SeanBot).
      * ``signal_reconciliations`` — SeanBot signals + TF's decision (SeanBot→TF).

    AGREE and SeanBot-only come straight from the reconciliation classifications
    (SeanBot signalled; TF entered = AGREE, TF blocked = MISS-<gate>). TF-only is
    a TF ``long_signal`` whose bar ts is not within ``tolerance_sec`` of any
    SeanBot signal — TF entered where SeanBot was silent.

    Reads with the service-role key (RLS deny-all; anon sees ``[]``). Both tables
    empty today → "0 — no comparable decisions today"; an error →
    "(unavailable: ...)". Never raises.
    """
    if not url or not key:
        return "?"
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    decisions = _fetch_supabase_rows_today(
        url, key, "strategy_decisions", "decision,decision_ts", "decision_ts", today_iso
    )
    recons = _fetch_supabase_rows_today(
        url, key, "signal_reconciliations", "classification,signal_ts", "signal_ts", today_iso
    )
    if decisions is None or recons is None:
        return "(unavailable: query error)"
    if not decisions and not recons:
        return "0 — no comparable decisions today"

    agree = sum(1 for r in recons if str(r.get("classification", "")).startswith("AGREE"))
    miss: dict[str, int] = {}
    for r in recons:
        cls = str(r.get("classification", ""))
        if cls.startswith("MISS"):
            miss[cls] = miss.get(cls, 0) + 1
    sb_only = sum(miss.values())

    sb_times = [dt for dt in (_parse_iso_ts(r.get("signal_ts")) for r in recons) if dt]
    tf_only = 0
    for d in decisions:
        if d.get("decision") != "long_signal":
            continue
        dt = _parse_iso_ts(d.get("decision_ts"))
        if dt is None:
            continue
        if not any(abs((dt - s).total_seconds()) <= tolerance_sec for s in sb_times):
            tf_only += 1

    miss_str = ", ".join(f"{n} {cls}" for cls, n in sorted(miss.items()))
    out = f"{agree} AGREE, {tf_only} TF-only, {sb_only} SeanBot-only"
    return out + (f" ({miss_str})" if miss_str else "")


def _last_app_log_line() -> str:
    """Tail tradeflow-app to one line; truncate; return placeholder on any error."""
    try:
        res = subprocess.run(
            ["docker", "logs", "tradeflow-app", "--tail", "1"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"(unavailable: {type(exc).__name__})"
    output = (res.stdout or res.stderr or "").strip()
    if not output:
        return "(no log line)"
    return output.splitlines()[-1][:120]


# ----------------------------------------------------- W-S15.3 Track E readiness


def _deployed_commit_from_env(env_lines: list[str]) -> str:
    """Pull the short ``TRADEFLOW_COMMIT`` from a list of ``KEY=value`` env strings."""
    for line in env_lines:
        if line.startswith("TRADEFLOW_COMMIT="):
            val = line.split("=", 1)[1].strip()
            return (val or "unknown")[:8]
    return "unknown"


def _deployed_commit(container: str = "tradeflow-app") -> str:
    """Read the baked ``TRADEFLOW_COMMIT`` from the running container.

    Uses ``docker inspect`` Config.Env (``docker exec env`` is harness-blocked).
    Returns 'unknown' / '(unavailable…)' on any error — never raises.
    """
    try:
        res = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
                container,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"(unavailable: {type(exc).__name__})"
    if res.returncode != 0:
        return "unknown"
    return _deployed_commit_from_env(res.stdout.splitlines())


def _app_recent_logs(tail: int = 1000) -> str:
    """Tail tradeflow-app logs; '' on error (callers degrade gracefully)."""
    try:
        res = subprocess.run(
            ["docker", "logs", "tradeflow-app", "--tail", str(tail)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return f"{res.stdout or ''}\n{res.stderr or ''}"


def _parse_digest_readiness(log_text: str) -> str:
    """Extract the readiness fragment from the most recent hourly digest line.

    The orchestrator appends ``warmup=… last_bar=… commit=…`` (W-S15.3 Track E)
    to ``[ALERT] hourly_session_digest``. Returns a placeholder when no digest is
    present (e.g. fresh boot) or the digest predates this feature.
    """
    latest = ""
    for line in log_text.splitlines():
        if "hourly_session_digest:" in line:
            latest = line
    if not latest:
        return "(no digest yet — fresh boot or warmup pending)"
    idx = latest.find("warmup=")
    if idx == -1:
        return "(digest predates readiness block)"
    return latest[idx:].strip()


def _count_reconnect_events(log_text: str) -> tuple[int, int]:
    """``(reconnect_recovered, farm-flap resubscribe)`` counts in the available tail.

    Best-effort: the json-file driver keeps ~3×10m, so this is 'recent', not
    all-time-since-boot. Surfaced as a flap canary, not an exact metric.
    """
    reconnects = log_text.count("[ALERT] reconnect_recovered")
    flaps = log_text.count("[ALERT] bar_sub_resubscribed_after_farm_flap")
    return reconnects, flaps


async def _broker_resting_orders_async(
    host: str, port: int, client_id: int, timeout: float
) -> tuple[bool, str]:
    """Connect → count ALL resting orders + live positions → disconnect.

    The orphan canary: when flat we expect 0 resting orders. ``reqAllOpenOrders``
    (not just this client's openTrades) so an orphan placed under any clientId is
    visible.
    """
    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync(host, port, clientId=client_id, timeout=timeout),
            timeout=timeout,
        )
        if not ib.isConnected():
            return (False, "connectAsync returned but isConnected=False")
        orders = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), timeout=timeout)
        positions = await asyncio.wait_for(ib.reqPositionsAsync(), timeout=timeout)
        n_orders = len(orders)
        live = [p for p in positions if getattr(p, "position", 0)]
        if not live:
            pos_str = "FLAT"
        else:
            pos_str = "+".join(
                f"{getattr(getattr(p, 'contract', None), 'localSymbol', '?')}"
                f"x{int(getattr(p, 'position', 0))}"
                for p in live
            )
        if n_orders == 0 and not live:
            canary = "OK"
        elif live:
            canary = "orders+position"
        else:
            canary = "ORPHAN? resting orders with no position"
        return (True, f"{n_orders} resting / pos={pos_str} [{canary}]")
    except TimeoutError:
        return (False, "TimeoutError")
    except Exception as exc:  # noqa: BLE001
        return (False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _broker_resting_orders(
    host: str, port: int, client_id: int, timeout: float = IB_PROBE_TIMEOUT_SEC
) -> str:
    """Sync wrapper for the resting-order orphan canary. Never raises."""
    try:
        ok, detail = asyncio.run(_broker_resting_orders_async(host, port, client_id, timeout))
    except Exception as exc:  # noqa: BLE001
        return f"(probe error: {type(exc).__name__})"
    return detail if ok else f"(unavailable: {detail})"


def run_daily_report() -> int:
    """Build a structured all-probes-plus-extras report and Telegram it. Never deduped."""
    host, port, client_id = _ib_env()
    sb_url, sb_key = _supabase_env()

    probes: list[tuple[str, bool, str]] = []
    ok_ib, detail_ib = probe_ib_api(host, port, client_id)
    probes.append(("IB API", ok_ib, detail_ib))
    ok_app, app_detail = probe_container("tradeflow-app")
    probes.append(("tradeflow-app", ok_app, str(app_detail)))
    ok_ibgw, ibgw_detail = probe_container("tradeflow-ib-gateway")
    probes.append(("tradeflow-ib-gateway", ok_ibgw, str(ibgw_detail)))
    ok_sb, sb_detail = probe_supabase(sb_url, sb_key)
    probes.append(("Supabase", ok_sb, sb_detail))
    ok_dash, dash_detail = probe_dashboard()
    probes.append(("Dashboard", ok_dash, dash_detail))
    ok_disk, disk_detail = probe_disk()
    probes.append(("Disk", ok_disk, str(disk_detail)))
    ok_mem, mem_detail = probe_memory()
    probes.append(("Memory", ok_mem, str(mem_detail)))

    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    # Data reads bypass RLS via the service-role key (anon sees an empty set).
    data_url, data_key = _supabase_data_env()
    lifecycles_today = _count_lifecycles_today(data_url, data_key)
    # Fix 5 — LOG the lifecycle-count query result so a future "0" is
    # distinguishable from a transient failure (err:/http) in the watchdog log.
    LOGGER.info("[WATCHDOG] daily_report: lifecycles_today_query=%s", lifecycles_today)
    realized_pnl = _realized_pnl_today(data_url, data_key)
    open_pos = _open_position_summary(data_url, data_key)
    # W-S15.2 — scorecard now reads the durable signal_reconciliations table
    # (service-role) instead of the ephemeral in-container JSONL journal.
    seanbot_scorecard = _seanbot_scorecard_today(data_url, data_key)
    # PR-D3c — TF→SeanBot divergence: joins durable strategy_decisions with
    # signal_reconciliations to report AGREE / TF-only / SeanBot-only.
    tf_vs_seanbot = _tf_vs_seanbot_today(data_url, data_key)
    app_rc = app_detail.get("restart_count", "?") if isinstance(app_detail, dict) else "?"
    last_log = _last_app_log_line()
    # W-S15.3 Track E — readiness signals. Host-side / broker-side only; the
    # in-process warmup + feed detail is surfaced from the latest hourly digest.
    deployed_commit = _deployed_commit()
    recent_logs = _app_recent_logs()
    digest_readiness = _parse_digest_readiness(recent_logs)
    reconnects, flaps = _count_reconnect_events(recent_logs)
    resting_orders = _broker_resting_orders(host, port, client_id)
    green = sum(1 for _, ok, _ in probes if ok)
    total = len(probes)

    lines: list[str] = []
    lines.append(f"TradeFlow Daily Report ({today_iso} UTC)")
    lines.append(f"Health: {green}/{total} green")
    lines.append("")
    for name, ok, detail in probes:
        mark = "OK" if ok else "FAIL"
        lines.append(f"- {name}: {mark} — {detail}")
    lines.append("")
    lines.append("TRADING:")
    lines.append(f"- Lifecycles today: {lifecycles_today}")
    lines.append(f"- Realized P&L today: {realized_pnl}")
    lines.append(f"- Open position (DB view): {open_pos}")
    lines.append(f"- SeanBot scorecard: {seanbot_scorecard}")
    lines.append(f"- TF vs SeanBot (today): {tf_vs_seanbot}")
    lines.append("")
    lines.append("READINESS:")
    lines.append(f"- Deployed commit: {deployed_commit}")
    lines.append(f"- App restarts since boot: {app_rc}")
    lines.append(f"- Reconnects / farm-flap resubs (recent logs): {reconnects} / {flaps}")
    lines.append(f"- Broker resting orders (orphan canary): {resting_orders}")
    lines.append(f"- Warmup / feed (last digest): {digest_readiness}")
    lines.append("")
    lines.append(f"App last log: {last_log}")
    msg = "\n".join(lines)

    sent = send_telegram(msg)
    LOGGER.info(
        "[WATCHDOG] daily_report: %s green=%d/%d sent=%s",
        today_iso,
        green,
        total,
        sent,
    )
    return 0


def run_self_test() -> int:
    """Send a Telegram test message including the script SHA so operator confirms version."""
    sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    hostname = os.uname().nodename
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    msg = f"TradeFlow watchdog self-test from VPS {hostname} at {ts}. " f"Script sha256 {sha}."
    LOGGER.info("[WATCHDOG] self_test: dispatching message to Telegram")
    sent = send_telegram(msg)
    LOGGER.info("[WATCHDOG] self_test: sent=%s", sent)
    return 0 if sent else 1


def _is_tty() -> bool:
    """True when stderr is attached to a terminal — i.e. running interactively."""
    return sys.stderr.isatty()


def configure_logging() -> None:
    STATE_DIR.mkdir(mode=0o700, exist_ok=True, parents=True)
    if LOGGER.handlers:
        return
    file_handler = TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(file_handler)
    # StreamHandler is added only for interactive invocations, or when the
    # escape-hatch env var is set. Under cron, the crontab template already
    # redirects stdout/stderr into watchdog.log via `>> ... 2>&1`, so adding
    # the StreamHandler too would write every line twice.
    if _is_tty() or os.environ.get("WATCHDOG_FORCE_STREAM") == "1":
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.addHandler(stream_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeFlow external watchdog")
    parser.add_argument(
        "--mode",
        choices=("monitor", "daily-report", "self-test"),
        required=True,
    )
    args = parser.parse_args(argv)
    load_dotenv(ENV_PATH)
    configure_logging()
    try:
        if args.mode == "monitor":
            return run_monitor()
        if args.mode == "daily-report":
            return run_daily_report()
        return run_self_test()
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("[WATCHDOG] %s: unhandled exception — %s", args.mode, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
