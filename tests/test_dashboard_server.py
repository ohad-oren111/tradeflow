"""FastAPI endpoint tests for the dashboard server.

TestClient is sync — these tests are sync. Env vars set per test via
monkeypatch.setenv to avoid leakage. Fresh MagicMock per test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from dashboard.scoreboard import _aggregate_seanbot_daily, _dedup_seanbot_exits
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


def test_healthz_is_public_liveness_probe(_env):
    """Q4: /healthz is exempt from the global auth dependency so Docker/uptime can
    probe liveness WITHOUT the operator's basic-auth creds. Everything else stays
    gated (asserted by the per-route *_requires_auth tests)."""
    client = TestClient(create_app(_make_orch()))
    r = client.get("/healthz")  # no auth
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    # auth supplied is still fine (probe is path-exempt, not creds-dependent)
    r2 = client.get("/healthz", auth=(_TEST_USER, _TEST_PASS))
    assert r2.status_code == 200


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
    # 05-31 has no operator anchor → estimate day → headline is hedged + flagged
    # not authoritative (Q4 honesty pass), not the confident "leads by".
    assert "SeanBot is ahead by" in body  # TF 0 vs SeanBot +192 → SeanBot ahead
    assert "not authoritative" in body  # untrusted-comparison banner/headline
    assert "Different books" in body  # caveat


def test_scoreboard_compares_two_days_with_winner_and_cumulative():
    # Per-day winner + cumulative-delta logic. Asserted on rows directly (not the
    # rendered headline) because the always-present operator anchors dominate the
    # global totals — the per-day comparison must still be exact on un-anchored days.
    from dashboard.scoreboard import _build_scoreboard

    board = _build_scoreboard(
        tf_by_day={"2026-05-30": 100.0, "2026-05-31": -30.0},
        sb_estimate_by_day={"2026-05-30": 80.0, "2026-05-31": -40.0},
    )
    by_day = {r.day: r for r in board.rows}
    d30 = by_day["2026-05-30"]
    assert d30.tf_pnl == 100.0 and d30.sb_pnl == 80.0
    assert d30.delta == 20.0 and d30.winner == "TF"  # TF +20
    d31 = by_day["2026-05-31"]
    assert d31.tf_pnl == -30.0 and d31.sb_pnl == -40.0
    assert d31.delta == 10.0 and d31.winner == "TF"  # lost less → TF +10
    # cumulative delta advances by 05-31's daily delta from the prior day
    assert d31.delta_cum - d30.delta_cum == pytest.approx(10.0, abs=0.005)


def test_scoreboard_headline_honest_about_estimates():
    """Q4 honesty pass: the cumulative 'leads by $X' headline is only authoritative
    when EVERY SeanBot day is an operator anchor. An estimate day hedges the verb
    and flags it 'not authoritative', and the staleness fields are populated."""
    from dashboard.scoreboard import _build_scoreboard
    from dashboard.seanbot_authoritative import AUTHORITATIVE_SEANBOT_DAILY_PNL

    # All-anchor scoreboard (no estimate day): confident "leads by", trustworthy.
    anchored = _build_scoreboard(tf_by_day={}, sb_estimate_by_day={})
    assert anchored.has_estimate is False
    assert anchored.headline_trustworthy is True
    assert "leads by" in anchored.headline
    assert "not authoritative" not in anchored.headline
    assert anchored.latest_anchor_day == max(AUTHORITATIVE_SEANBOT_DAILY_PNL)

    # Add an un-anchored estimate day → headline must hedge + flag.
    estimated = _build_scoreboard(tf_by_day={}, sb_estimate_by_day={"2026-06-20": 500.0})
    assert estimated.has_estimate is True
    assert estimated.headline_trustworthy is False
    assert estimated.estimate_day_count == 1
    assert "is ahead by" in estimated.headline
    assert "not authoritative" in estimated.headline


def test_scoreboard_one_sided_day_renders_other_side_zero(_env):
    orch = _make_orch()
    # Use 2026-05-30 — a day with NO authoritative anchor, so an empty SeanBot
    # capture set must render as a $0 estimate (not crash, not anchor-overridden).
    orch._db.select = AsyncMock(
        side_effect=[
            [{"state": "CLOSED", "pnl_net": 50.0, "exit_filled_at": "2026-05-30T15:00:00+00:00"}],
            [],  # SeanBot silent that day → $0 estimate, must not crash
        ]
    )
    client = TestClient(create_app(orch))
    r = client.get("/scoreboard", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "2026-05-30" in body
    assert "50.00" in body
    assert "0.00" in body  # SeanBot side rendered as zero
    assert "est." in body  # un-anchored day flagged as estimate


def test_scoreboard_read_error_surfaces_not_500(_env):
    orch = _make_orch()
    orch._db.select = AsyncMock(side_effect=RuntimeError("supabase down"))  # 1 call (TF read)
    client = TestClient(create_app(orch))
    r = client.get("/scoreboard", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    assert "Failed to load" in r.text


# ------------------------- PR #100 / PR-4: SeanBot exit dedup (§7.3)
# SeanBot now runs a CONCURRENT book, so "same price within the window" no longer
# implies the same close. We collapse only IDENTICAL re-announcements (same price
# AND same pnl_points); distinct concurrent closes (same price, different pnl) are
# retained. Pure-helper tests use explicit args, fresh fixtures, assert aggregates.


def test_dedup_retains_distinct_pnl_same_price_concurrent_closes():
    # PR-4: the real 2026-06-10 04:19:11 multi-flatten — three DISTINCT concurrent
    # positions closed @28954.88 with pnl −357/−341/−334 (each entered at a different
    # price). These are NOT one re-announced close; all three must be RETAINED.
    rows = [
        {
            "type": "exit",
            "ts": "2026-06-10T04:19:11+00:00",
            "message_id": 293,
            "price": 28954.88,
            "pnl_points": -357.0,
            "contracts": 2,
        },
        {
            "type": "exit",
            "ts": "2026-06-10T04:19:11+00:00",
            "message_id": 294,
            "price": 28954.88,
            "pnl_points": -341.0,
            "contracts": 2,
        },
        {
            "type": "exit",
            "ts": "2026-06-10T04:19:11+00:00",
            "message_id": 295,
            "price": 28954.88,
            "pnl_points": -334.0,
            "contracts": 2,
        },
    ]
    kept = _dedup_seanbot_exits(rows)
    assert len(kept) == 3  # all three distinct concurrent closes survive
    assert {r["message_id"] for r in kept} == {293, 294, 295}
    # all three counted: (−357−341−334) pt × $2 × 2 ct = −$4,128.00 (was −$1,336 collapsed)
    by_day = _aggregate_seanbot_daily(rows)
    assert by_day["2026-06-10"] == pytest.approx(-4128.0, abs=0.05)


def test_dedup_retains_drifting_pnl_same_price_burst():
    # The real 2026-06-01 13:22 cluster @30365.25 (−127/−101/−94) drifts in pnl, so
    # under the concurrent-book policy it is NO LONGER collapsed (a pnl difference is
    # treated as a distinct close). The stated tradeoff: a recompute-drift
    # re-announcement is over-counted rather than risk hiding a real concurrent loss.
    rows = [
        {
            "type": "exit",
            "ts": "2026-06-01T13:22:18+00:00",
            "message_id": 152,
            "price": 30365.25,
            "pnl_points": -127.0,
            "contracts": 2,
        },
        {
            "type": "exit",
            "ts": "2026-06-01T13:22:53+00:00",
            "message_id": 153,
            "price": 30365.25,
            "pnl_points": -101.0,
            "contracts": 2,
        },
        {
            "type": "exit",
            "ts": "2026-06-01T13:22:53+00:00",
            "message_id": 154,
            "price": 30365.25,
            "pnl_points": -94.0,
            "contracts": 2,
        },
    ]
    assert len(_dedup_seanbot_exits(rows)) == 3


def test_dedup_exact_duplicate_counted_once():
    # The real 2026-06-02 19:57:45/19:58:05 @30694.25 exact +49/+49 pair.
    rows = [
        {
            "type": "exit",
            "ts": "2026-06-02T19:57:45+00:00",
            "message_id": 208,
            "price": 30694.25,
            "pnl_points": 49.0,
            "contracts": 2,
        },
        {
            "type": "exit",
            "ts": "2026-06-02T19:58:05+00:00",
            "message_id": 209,
            "price": 30694.25,
            "pnl_points": 49.0,
            "contracts": 2,
        },
    ]
    assert len(_dedup_seanbot_exits(rows)) == 1
    by_day = _aggregate_seanbot_daily(rows)
    assert by_day["2026-06-02"] == pytest.approx(196.0, abs=0.05)  # +49 × $2 × 2 ct, once


def test_dedup_keeps_distinct_and_far_apart_same_price_exits():
    # Different prices, plus the SAME price > window apart (genuinely two closes)
    # must NOT collapse — only tight same-price bursts are re-announcements.
    rows = [
        {
            "type": "exit",
            "ts": "2026-06-01T08:00:00+00:00",
            "message_id": 1,
            "price": 30500.0,
            "pnl_points": -75.0,
            "contracts": 2,
        },
        {
            "type": "exit",
            "ts": "2026-06-01T12:00:00+00:00",
            "message_id": 2,
            "price": 30400.0,
            "pnl_points": -75.0,
            "contracts": 2,
        },
        {
            "type": "exit",
            "ts": "2026-06-01T18:00:00+00:00",
            "message_id": 3,
            "price": 30500.0,
            "pnl_points": 40.0,
            "contracts": 2,
        },  # same px, 10h later
    ]
    assert len(_dedup_seanbot_exits(rows)) == 3


def test_aggregate_jun01_anchor_six_true_exits_within_tolerance():
    # Six distinct true exits whose realized $ total the operator's trusted
    # 2026-06-01 anchor (−$866.72). A 7th row duplicates the first close (same
    # price within window) and MUST NOT change the total — dedup counts it once.
    # contracts=2 × multiplier 2.0 ⇒ $4 per point; −216.68 pt ⇒ −$866.72.
    base = [
        ("2026-06-01T07:50:00+00:00", 30506.75, -36.0),
        ("2026-06-01T08:16:00+00:00", 30482.75, -36.0),
        ("2026-06-01T08:19:00+00:00", 30481.0, -36.0),
        ("2026-06-01T12:25:00+00:00", 30423.25, -36.0),
        ("2026-06-01T13:22:00+00:00", 30365.25, -36.0),
        ("2026-06-01T19:50:00+00:00", 30563.0, -36.68),
    ]
    rows = [
        {"type": "exit", "ts": ts, "message_id": i, "price": px, "pnl_points": pts, "contracts": 2}
        for i, (ts, px, pts) in enumerate(base)
    ]
    # duplicate re-announcement of the first close (same price, +30s) — dedup
    # keeps the later (settled) row, so it carries that close's settled −36.0 pt.
    rows.append(
        {
            "type": "exit",
            "ts": "2026-06-01T07:50:30+00:00",
            "message_id": 99,
            "price": 30506.75,
            "pnl_points": -36.0,
            "contracts": 2,
        }
    )
    by_day = _aggregate_seanbot_daily(rows)
    assert by_day["2026-06-01"] == pytest.approx(-866.72, abs=0.05)


def test_aggregate_excludes_non_exit_rows():
    # entry / stop_moved rows must never contribute to realized P&L.
    rows = [
        {
            "type": "entry",
            "ts": "2026-06-01T07:00:00+00:00",
            "message_id": 1,
            "price": 30600.0,
            "pnl_points": None,
            "contracts": 2,
        },
        {
            "type": "stop_moved",
            "ts": "2026-06-01T07:30:00+00:00",
            "message_id": 2,
            "price": 30600.0,
            "pnl_points": 50.0,
            "contracts": 2,
        },
        {
            "type": "exit",
            "ts": "2026-06-01T08:00:00+00:00",
            "message_id": 3,
            "price": 30525.0,
            "pnl_points": -75.0,
            "contracts": 2,
        },
    ]
    by_day = _aggregate_seanbot_daily(rows)
    # only the single exit: −75 × $2 × 2 = −$300; the +50 stop_moved is excluded.
    assert by_day == {"2026-06-01": pytest.approx(-300.0, abs=0.05)}


# ----------------- DASH-FIX: authoritative SeanBot daily P&L + comparison chart
# SeanBot posts no daily-summary alert and capture began mid-2026-05-28, so the
# per-exit reconstruction cannot reproduce the operator's trusted anchors. The
# scoreboard prefers operator-trusted figures (dashboard.seanbot_authoritative)
# per day and flags estimate-fallback days.


def test_authoritative_overrides_lossy_estimate_for_anchored_day():
    from dashboard.scoreboard import _build_scoreboard

    # 2026-06-01 is an operator anchor (−866.72). Even a wildly different per-exit
    # estimate for that day must be ignored in favour of the anchor.
    board = _build_scoreboard(
        tf_by_day={"2026-06-01": -100.0},
        sb_estimate_by_day={"2026-06-01": -1884.0},  # the lossy over-capture
    )
    row = next(r for r in board.rows if r.day == "2026-06-01")
    assert row.sb_pnl == pytest.approx(-866.72, abs=0.005)
    assert row.sb_is_estimate is False


def test_unanchored_day_falls_back_to_estimate_and_is_flagged():
    from dashboard.scoreboard import _build_scoreboard

    # 2026-05-29 has no anchor → use the per-exit estimate, flagged as estimate.
    board = _build_scoreboard(
        tf_by_day={},
        sb_estimate_by_day={"2026-05-29": 1004.0},
    )
    row = next(r for r in board.rows if r.day == "2026-05-29")
    assert row.sb_pnl == pytest.approx(1004.0, abs=0.005)
    assert row.sb_is_estimate is True
    assert board.has_estimate is True


def test_anchor_day_with_no_tf_and_no_captures_still_appears():
    from dashboard.scoreboard import _build_scoreboard

    # 2026-05-26 (+2108.18) has zero TF rows and zero captures — it must still
    # appear, sourced purely from the authoritative anchor.
    board = _build_scoreboard(tf_by_day={}, sb_estimate_by_day={})
    days = {r.day: r for r in board.rows}
    assert "2026-05-26" in days
    assert days["2026-05-26"].sb_pnl == pytest.approx(2108.18, abs=0.005)
    assert days["2026-05-26"].sb_is_estimate is False


def test_chart_geometry_endpoints_track_cumulative():
    from dashboard.scoreboard import _build_scoreboard

    board = _build_scoreboard(
        tf_by_day={"2026-05-30": 100.0, "2026-05-31": -40.0},
        sb_estimate_by_day={"2026-05-30": 20.0, "2026-05-31": -10.0},
    )
    c = board.chart
    assert c is not None
    # One marker per row (anchors are always present, so don't hard-code a count).
    assert len(c.tf_points) == len(board.rows)
    assert len(c.sb_points) == len(board.rows)
    # Chart is oldest→newest, so the last marker equals the newest row (rows[0]).
    newest = board.rows[0]
    assert c.tf_points[-1][2] == pytest.approx(newest.tf_cum, abs=0.005)
    assert c.sb_points[-1][2] == pytest.approx(newest.sb_cum, abs=0.005)
    # polylines are space-separated "x,y" pairs, one per row.
    assert len(c.tf_polyline.split()) == len(board.rows)
    assert len(c.sb_polyline.split()) == len(board.rows)


def test_scoreboard_route_renders_chart_and_estimate_marker(_env):
    orch = _make_orch()
    # TF on an anchored day; SeanBot captures empty (anchor wins) plus an
    # un-anchored day from the estimate path.
    tf_rows = [
        {"state": "CLOSED", "pnl_net": -100.0, "exit_filled_at": "2026-06-01T15:00:00+00:00"},
    ]
    sb_rows = [
        {"type": "exit", "pnl_points": 48, "contracts": 2, "ts": "2026-05-31T22:52:05+00:00"},
    ]
    orch._db.select = AsyncMock(side_effect=[tf_rows, sb_rows])
    client = TestClient(create_app(orch))
    r = client.get("/scoreboard", auth=(_TEST_USER, _TEST_PASS))
    assert r.status_code == 200
    body = r.text
    assert "<svg" in body and "polyline" in body  # comparison chart rendered
    assert "-866.72" in body  # 2026-06-01 anchor used, not an estimate
    assert "192.00" in body  # 2026-05-31 estimate (48pt × $2 × 2ct)
    assert "est." in body  # the un-anchored day flagged


def test_authoritative_module_matches_trusted_anchors():
    from dashboard.seanbot_authoritative import authoritative_pnl

    trusted = {
        "2026-05-26": 2108.18,
        "2026-05-27": 1380.94,
        "2026-05-28": -616.20,
        "2026-06-01": -866.72,
        "2026-06-02": -202.96,
    }
    for day, val in trusted.items():
        assert authoritative_pnl(day) == pytest.approx(val, abs=0.005)
    assert authoritative_pnl("2026-05-29") is None  # un-anchored → None


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
