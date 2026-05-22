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
