"""FastAPI endpoint tests for the dashboard server.

TestClient is sync — these tests are sync. Env vars set per test via
monkeypatch.setenv to avoid leakage. Fresh MagicMock per test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from dashboard.server import create_app

_TEST_USER = "test_ohad"
_TEST_PASS = "test_password_xyz"


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", _TEST_USER)
    monkeypatch.setenv("DASHBOARD_PASSWORD", _TEST_PASS)


def _make_orch() -> MagicMock:
    orch = MagicMock(name="Orchestrator")
    orch._paper_account = "DUQ331660"
    orch._instrument = "MNQM6"
    orch.is_halted = MagicMock(return_value=False)
    orch.halt_raised_at = MagicMock(return_value=None)
    orch._safe_server_version = MagicMock(return_value="178")
    orch._ib = MagicMock()
    orch._ib.get_account_summary = AsyncMock(
        return_value={
            "NetLiquidation": 1000085.80,
            "AvailableFunds": 950000.0,
            "BuyingPower": 4750000.0,
        }
    )
    orch._ib.get_portfolio = AsyncMock(return_value=[])
    orch._ib.get_open_trades = AsyncMock(return_value=[])
    # PR #70 — the trade-log / daily-P&L views read lifecycles via the
    # orchestrator's service-role SupabaseClient. Default to an empty result;
    # per-test overrides set a fake payload.
    orch._db = MagicMock(name="SupabaseClient")
    orch._db.select = AsyncMock(return_value=[])
    return orch


def test_create_app_raises_if_username_missing(monkeypatch):
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", _TEST_PASS)

    with pytest.raises(RuntimeError, match="DASHBOARD_USERNAME"):
        create_app(_make_orch())


def test_create_app_raises_if_password_missing(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", _TEST_USER)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        create_app(_make_orch())


def test_index_requires_auth(_env):
    client = TestClient(create_app(_make_orch()))
    r = client.get("/")
    assert r.status_code == 401


def test_index_returns_html_with_four_panel_containers_when_auth_provided(_env):
    client = TestClient(create_app(_make_orch()))
    r = client.get("/", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "/panel/status" in body
    assert "/panel/account" in body
    assert "/panel/positions" in body
    assert "/panel/working_orders" in body


def test_panel_status_requires_auth(_env):
    client = TestClient(create_app(_make_orch()))
    r = client.get("/panel/status")
    assert r.status_code == 401


def test_panel_account_renders_netliq_correctly(_env):
    client = TestClient(create_app(_make_orch()))
    r = client.get("/panel/account", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "$" in body
    assert "1000085.80" in body


def test_panel_positions_handles_empty(_env):
    client = TestClient(create_app(_make_orch()))
    r = client.get("/panel/positions", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "No open positions" in r.text


def test_panel_working_orders_handles_empty(_env):
    client = TestClient(create_app(_make_orch()))
    r = client.get("/panel/working_orders", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "No working orders" in r.text


def test_healthz_requires_auth(_env):
    """Default behavior: /healthz inherits the global auth dependency.

    Docker HEALTHCHECK can supply auth via curl -u; we accept the small
    Docker overhead for the simpler "all routes gated" guarantee.
    """
    client = TestClient(create_app(_make_orch()))
    r = client.get("/healthz")
    assert r.status_code == 401
    r2 = client.get("/healthz", auth=(_TEST_USER, _TEST_PASS))
    assert r2.status_code == 200
    assert r2.json() == {"status": "ok"}


def test_no_mutation_endpoints_exist(_env):
    """PR #18 is read-only. PR #19 will add the kill switch (POST endpoints)."""
    client = TestClient(create_app(_make_orch()))
    for path in ("/api/flatten", "/api/halt", "/api/exit", "/"):
        r = client.post(path, auth=(_TEST_USER, _TEST_PASS))
        assert r.status_code in (404, 405), f"POST {path} returned {r.status_code}"


# ----------------------------------- Live page: nav + recent-trades summary


def test_index_renders_full_nav_and_recent_trades_panel(_env):
    client = TestClient(create_app(_make_orch()))
    r = client.get("/", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    for href in (
        'href="/"',
        'href="/trades"',
        'href="/pnl"',
        'href="/scoreboard"',
        'href="/divergence"',
    ):
        assert href in body, f"nav link missing: {href}"
    assert 'hx-get="/panel/recent_trades"' in body  # trades visible on the Live page


def test_panel_recent_trades_requires_auth(_env):
    client = TestClient(create_app(_make_orch()))
    assert client.get("/panel/recent_trades").status_code == 401


def test_panel_recent_trades_renders_rows_and_latest_pnl(_env):
    orch = _make_orch()
    log_rows = [
        {
            "lifecycle_id": "a",
            "direction": "LONG",
            "symbol": "MNQM6",
            "entry_price": 30559.25,
            "exit_price": 30328.62,
            "entry_qty": 2,
            "pnl_net": -924.98,
            "exit_reason": "STOP",
            "state": "CLOSED",
            "entry_filled_at": "2026-06-01T04:16:06+00:00",
            "exit_filled_at": "2026-06-01T13:30:00+00:00",
        }
    ]
    daily_rows = [
        {"pnl_net": -924.98, "exit_filled_at": "2026-06-01T13:30:00+00:00", "state": "CLOSED"}
    ]
    # 2 calls: trade_log (limit 10) then daily_pnl.
    orch._db.select = AsyncMock(side_effect=[log_rows, daily_rows])
    client = TestClient(create_app(orch))
    r = client.get("/panel/recent_trades", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "30559.25" in body  # entry price
    assert "-924.98" in body  # realized pnl
    assert "STOP" in body
    assert "Latest realized" in body  # today's figure
    assert "2026-06-01" in body
    assert "DB view" in body  # caveat


def test_panel_recent_trades_empty_no_crash(_env):
    client = TestClient(create_app(_make_orch()))  # default select -> []
    r = client.get("/panel/recent_trades", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "No trades yet" in r.text


def test_panel_recent_trades_read_error_no_500(_env):
    orch = _make_orch()
    orch._db.select = AsyncMock(side_effect=RuntimeError("supabase down"))  # raises on each call
    client = TestClient(create_app(orch))
    r = client.get("/panel/recent_trades", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "Failed to load" in r.text


# --------------------------------------------------- PR #70: trade log + P&L


def test_trades_requires_auth(_env):
    client = TestClient(create_app(_make_orch()))
    assert client.get("/trades").status_code == 401


def test_pnl_requires_auth(_env):
    client = TestClient(create_app(_make_orch()))
    assert client.get("/pnl").status_code == 401


def test_trades_renders_closed_and_open_rows(_env):
    orch = _make_orch()
    orch._db.select = AsyncMock(
        return_value=[
            {
                "lifecycle_id": "aaaa1111",
                "direction": "LONG",
                "symbol": "MNQM6",
                "entry_price": 30559.25,
                "exit_price": 30328.62,
                "entry_qty": 2,
                "pnl_net": -924.98,
                "exit_reason": "STOP",
                "state": "CLOSED",
                "entry_filled_at": "2026-06-01T04:16:06+00:00",
                "exit_filled_at": "2026-06-01T13:30:00+00:00",
            },
            {
                "lifecycle_id": "bbbb2222",
                "direction": "LONG",
                "symbol": "MNQM6",
                "entry_price": 30444.75,
                "exit_price": None,
                "entry_qty": 2,
                "pnl_net": None,
                "exit_reason": None,
                "state": "ACTIVE",
                "entry_filled_at": "2026-06-01T13:36:19+00:00",
                "exit_filled_at": None,
            },
        ]
    )
    client = TestClient(create_app(orch))
    r = client.get("/trades", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "30559.25" in body  # closed-row entry price mapped
    assert "-924.98" in body  # closed-row realized pnl
    assert "STOP" in body  # exit reason
    assert "OPEN" in body  # the ACTIVE lifecycle renders as OPEN, no pnl
    assert "DB view" in body  # accuracy caveat present


def test_pnl_aggregates_by_utc_day_open_contributes_zero(_env):
    orch = _make_orch()
    orch._db.select = AsyncMock(
        return_value=[
            # 2026-05-30: one win (+100) + one loss (-40) → sum 60
            {"pnl_net": 100.0, "exit_filled_at": "2026-05-30T15:00:00+00:00", "state": "CLOSED"},
            {"pnl_net": -40.0, "exit_filled_at": "2026-05-30T18:00:00+00:00", "state": "CLOSED"},
            # 2026-05-31: one loss (-25)
            {"pnl_net": -25.0, "exit_filled_at": "2026-05-31T14:00:00+00:00", "state": "CLOSED"},
            # an open lifecycle (no exit) contributes 0 realized — excluded
            {"pnl_net": None, "exit_filled_at": None, "state": "ACTIVE"},
        ]
    )
    client = TestClient(create_app(orch))
    r = client.get("/pnl", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "2026-05-30" in body
    assert "2026-05-31" in body
    assert "60.00" in body  # day-1 realized sum (100 - 40)
    assert "-25.00" in body  # day-2 realized sum
    assert "35.00" in body  # running total through day-2 (60 - 25)
    assert "DB view" in body


def test_trades_handles_empty_without_crash(_env):
    client = TestClient(create_app(_make_orch()))  # default select → []
    r = client.get("/trades", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "No trades yet" in r.text


def test_pnl_read_error_surfaces_not_500(_env):
    orch = _make_orch()
    orch._db.select = AsyncMock(side_effect=RuntimeError("supabase down"))  # 1 call
    client = TestClient(create_app(orch))
    r = client.get("/pnl", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "Failed to load" in r.text


# ----------------------------------------- PR #71: TF-vs-SeanBot scoreboard


def test_scoreboard_requires_auth(_env):
    client = TestClient(create_app(_make_orch()))
    assert client.get("/scoreboard").status_code == 401


def test_scoreboard_seanbot_dollars_from_points(_env):
    orch = _make_orch()
    # 2 select calls: lifecycles (TF, none) then seanbot_signals (one exit).
    orch._db.select = AsyncMock(
        side_effect=[
            [],
            [{"type": "exit", "pnl_points": 48, "contracts": 2, "ts": "2026-05-31T22:52:05+00:00"}],
        ]
    )
    client = TestClient(create_app(orch))
    r = client.get("/scoreboard", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "192.00" in body  # 48pt × $2 × 2ct
    assert "2026-05-31" in body
    assert "SeanBot leads" in body  # TF 0 vs SeanBot +192 → SeanBot ahead
    assert "Different books" in body  # caveat


def test_scoreboard_compares_two_days_with_winner_and_cumulative(_env):
    orch = _make_orch()
    tf_rows = [
        {"state": "CLOSED", "pnl_net": 100.0, "exit_filled_at": "2026-05-30T15:00:00+00:00"},
        {"state": "CLOSED", "pnl_net": -30.0, "exit_filled_at": "2026-05-31T15:00:00+00:00"},
    ]
    sb_rows = [
        {
            "type": "exit",
            "pnl_points": 20,
            "contracts": 2,
            "ts": "2026-05-30T16:00:00+00:00",
        },  # $80
        {
            "type": "exit",
            "pnl_points": -10,
            "contracts": 2,
            "ts": "2026-05-31T16:00:00+00:00",
        },  # -$40
    ]
    orch._db.select = AsyncMock(side_effect=[tf_rows, sb_rows])  # 2 calls: lifecycles, seanbot
    client = TestClient(create_app(orch))
    r = client.get("/scoreboard", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    # day 05-30: TF 100 vs SB 80 → TF +20; day 05-31: TF -30 vs SB -40 → TF +10 (lost less)
    assert "100.00" in body and "80.00" in body
    assert "-30.00" in body and "-40.00" in body
    # cumulative TF 70 vs SB 40 → TF leads by 30
    assert "TF leads by $30.00" in body


def test_scoreboard_one_sided_day_renders_other_side_zero(_env):
    orch = _make_orch()
    orch._db.select = AsyncMock(
        side_effect=[
            [{"state": "CLOSED", "pnl_net": 50.0, "exit_filled_at": "2026-06-01T15:00:00+00:00"}],
            [],  # SeanBot silent that day → $0, must not crash
        ]
    )
    client = TestClient(create_app(orch))
    r = client.get("/scoreboard", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "2026-06-01" in body
    assert "50.00" in body
    assert "0.00" in body  # SeanBot side rendered as zero


def test_scoreboard_read_error_surfaces_not_500(_env):
    orch = _make_orch()
    orch._db.select = AsyncMock(side_effect=RuntimeError("supabase down"))  # 1 call (TF read)
    client = TestClient(create_app(orch))
    r = client.get("/scoreboard", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "Failed to load" in r.text


# --------------------------------------- PR 2: decision divergence panel

_DIV_TF = [
    {"decision_ts": "2026-06-01T13:00:00+00:00", "decision": "long_signal", "failed_gate": None},
    {
        "decision_ts": "2026-06-01T13:05:00+00:00",
        "decision": "noop_filter",
        "failed_gate": "ma_order",
    },
    {"decision_ts": "2026-06-01T13:10:00+00:00", "decision": "long_signal", "failed_gate": None},
    {"decision_ts": "2026-06-01T13:15:00+00:00", "decision": "noop_filter", "failed_gate": "gap"},
]
_DIV_SB = [
    {
        "ts": "2026-06-01T13:00:30+00:00",
        "type": "entry",
    },  # → agree-enter (matches 13:00 long_signal)
    {"ts": "2026-06-01T13:05:45+00:00", "type": "entry"},  # → SeanBot-only, TF skipped on ma_order
    {
        "ts": "2026-06-01T14:00:00+00:00",
        "type": "entry",
    },  # → SeanBot-only, no TF decision in window
]


def test_divergence_requires_auth(_env):
    client = TestClient(create_app(_make_orch()))
    assert client.get("/divergence").status_code == 401


def test_divergence_classify_counts_and_gate_breakdown():
    from dashboard.divergence import _classify

    d = _classify(_DIV_TF, _DIV_SB, 120.0)
    assert d.agree_enter == 1  # 13:00 long_signal ↔ 13:00:30 entry
    assert d.seanbot_only == 2  # the ma_order skip + the no-TF-record 14:00 entry
    assert d.seanbot_only_no_tf_record == 1
    assert d.tf_only == 1  # 13:10 long_signal, no SeanBot entry on the bar
    assert d.agree_skip == 1  # 13:15 noop_filter, SeanBot also didn't enter
    assert [(g.gate, g.count) for g in d.skip_gate_breakdown] == [("ma_order", 1)]


def test_divergence_route_renders_classification(_env):
    orch = _make_orch()
    # 2 calls: strategy_decisions then seanbot_signals.
    orch._db.select = AsyncMock(side_effect=[_DIV_TF, _DIV_SB])
    client = TestClient(create_app(orch))
    r = client.get("/divergence", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "agree-enter" in body
    assert "SeanBot-only" in body
    assert "TF-only" in body
    assert "ma_order" in body  # gate breakdown rendered
    assert "no TF decision in window" in body  # the 14:00 entry note
    assert "Sparse until forward decisions accumulate" in body  # caveat


def test_divergence_empty_data_no_crash(_env):
    orch = _make_orch()
    orch._db.select = AsyncMock(side_effect=[[], []])  # 2 calls, both empty
    client = TestClient(create_app(orch))
    r = client.get("/divergence", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "accumulating" in r.text


def test_divergence_read_error_surfaces_not_500(_env):
    orch = _make_orch()
    orch._db.select = AsyncMock(side_effect=RuntimeError("supabase down"))  # 1 call (TF read)
    client = TestClient(create_app(orch))
    r = client.get("/divergence", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "Failed to load" in r.text
