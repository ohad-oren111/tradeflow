"""Tests for src.execution.kill_switch — halt-on-loss/drawdown circuit breaker.

asyncio_mode=auto (pyproject) → async tests need no decorator. Mocked at the
DB / injected-callable boundary; fresh mocks per test.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.risk_params import RISK
from src.execution.kill_switch import (
    KillSwitch,
    _sum_pnl_since,
    evaluate_triggers,
)

_KW = {"max_consecutive_losses": 6, "daily_dd_pct": 0.08, "weekly_dd_pct": 0.15}


# ------------------------------------------------ pure trigger logic


def test_consecutive_losses_trips_at_exactly_six():
    v = evaluate_triggers([-1.0] * 6, 0.0, 0.0, 0.0, **_KW)
    assert v.tripped and v.reason == "consecutive_losses"


def test_five_losses_does_not_trip():
    v = evaluate_triggers([-1.0] * 5, 0.0, 0.0, 0.0, **_KW)
    assert not v.tripped


def test_six_losses_but_most_recent_is_a_win_does_not_trip():
    # newest-first: a win at the front breaks the streak.
    v = evaluate_triggers([1.0] + [-1.0] * 6, 0.0, 0.0, 0.0, **_KW)
    assert not v.tripped


def test_daily_dd_trips_at_boundary():
    # 8% of 50_000 = 4_000; realized_today == -4_000 trips (inclusive <=).
    v = evaluate_triggers([], -4000.0, -4000.0, 50_000.0, **_KW)
    assert v.tripped and v.reason == "daily_dd"


def test_daily_dd_just_inside_boundary_no_trip():
    v = evaluate_triggers([], -3999.99, -3999.99, 50_000.0, **_KW)
    assert not v.tripped


def test_weekly_dd_trips_when_daily_ok():
    # daily -100 (ok vs -4000); weekly -7500 == 15% of 50_000 → weekly trips.
    v = evaluate_triggers([], -100.0, -7500.0, 50_000.0, **_KW)
    assert v.tripped and v.reason == "weekly_dd"


def test_dd_skipped_when_equity_base_unknown():
    # equity 0 → DD triggers skipped; no consecutive streak → no trip.
    v = evaluate_triggers([-1.0, 1.0, -1.0], -9_999_999.0, -9_999_999.0, 0.0, **_KW)
    assert not v.tripped


def test_sum_pnl_since_filters_by_exit_date():
    now = datetime.now(UTC)
    rows = [
        {"pnl_net": -100.0, "exit_filled_at": now.isoformat()},
        {"pnl_net": -50.0, "exit_filled_at": (now - timedelta(days=3)).isoformat()},
        {"pnl_net": -7.0, "exit_filled_at": None},  # null ts excluded
    ]
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    assert _sum_pnl_since(rows, day_start) == pytest.approx(-100.0)


# ------------------------------------------------ KillSwitch.poll_once


def _db(rows: list[dict]) -> MagicMock:
    db = MagicMock(name="SupabaseClient")
    db.select = AsyncMock(return_value=rows)
    return db


def _build(
    *, db: MagicMock | None = None, halted: bool = False, params=RISK, equity: float = 1_000_000.0
):
    raised: list[str] = []
    flattened: list[bool] = []

    async def flatten():
        flattened.append(True)

    async def equity_base():
        return equity

    ks = KillSwitch(
        db=db if db is not None else _db([]),
        is_halted=lambda: halted,
        raise_halt=lambda reason: raised.append(reason),
        flatten=flatten,
        equity_base=equity_base,
        params=params,
    )
    return ks, raised, flattened


async def test_poll_disabled_takes_no_action():
    ks, raised, flat = _build(params=replace(RISK, kill_switch_enabled=False))
    v = await ks.poll_once()
    assert not v.tripped
    assert raised == [] and flat == []


async def test_poll_already_halted_does_not_reflatten():
    ks, raised, flat = _build(halted=True, db=_db([{"pnl_net": -1.0, "exit_filled_at": None}] * 6))
    await ks.poll_once()
    assert raised == [] and flat == []  # waits for the manual reset; no double-act


async def test_poll_trips_on_six_losses_halts_and_flattens():
    ks, raised, flat = _build(db=_db([{"pnl_net": -1.0, "exit_filled_at": None} for _ in range(6)]))
    v = await ks.poll_once()
    assert v.tripped and v.reason == "consecutive_losses"
    assert len(raised) == 1 and raised[0] == "kill_switch:consecutive_losses"
    assert flat == [True]  # the safe cancel+market-exit flatten path was invoked


async def test_poll_no_trip_leaves_halt_untouched():
    ks, raised, flat = _build(db=_db([{"pnl_net": 5.0, "exit_filled_at": None}]))
    v = await ks.poll_once()
    assert not v.tripped
    assert raised == [] and flat == []  # kill switch NEVER clears/raises without a trip


async def test_poll_fail_safe_halts_on_evaluator_error_without_raising():
    db = MagicMock()
    db.select = AsyncMock(side_effect=RuntimeError("supabase down"))  # 1 call, raises
    ks, raised, flat = _build(db=db)
    v = await ks.poll_once()  # must NOT raise
    assert v.tripped and v.reason == "error"
    assert len(raised) == 1 and raised[0] == "kill_switch:evaluator_error"
    assert flat == []  # fail-safe blocks entries; does not act on an unknown position


async def test_poll_net_liq_blip_does_not_crash_uses_consec_only():
    # equity_base raises → DD skipped this poll, consecutive-loss still evaluated.
    db = _db([{"pnl_net": -1.0, "exit_filled_at": None} for _ in range(6)])

    async def boom():
        raise RuntimeError("net-liq timeout")

    raised: list[str] = []

    async def flatten():
        raised.append("flatten")

    ks = KillSwitch(
        db=db,
        is_halted=lambda: False,
        raise_halt=lambda r: raised.append(r),
        flatten=flatten,
        equity_base=boom,
    )
    v = await ks.poll_once()  # must NOT raise despite the net-liq failure
    assert v.tripped and v.reason == "consecutive_losses"
