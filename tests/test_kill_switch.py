"""Tests for src.execution.kill_switch — tiered halt-on-loss/drawdown breaker (PR A).

asyncio_mode=auto (pyproject) → async tests need no decorator. Mocked at the
DB / injected-callable boundary; fresh mocks per test.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from config.risk_params import RISK
from src.execution.kill_switch import (
    KillSwitch,
    _sum_pnl_since,
    evaluate_triggers,
)

# Default tier kwargs for the pure-function tests.
_KW = {"warn_consec_losses": 6, "halt_consec_losses": 10, "max_drawdown_pct": 33.0}


# ------------------------------------------------ pure trigger logic — tiers


def test_ten_losses_pauses():
    v = evaluate_triggers([-1.0] * 10, 0.0, None, **_KW)
    assert v.action == "pause" and v.reason == "consecutive_losses"
    assert v.tripped is True


def test_nine_losses_notifies_not_pauses():
    v = evaluate_triggers([-1.0] * 9, 0.0, None, **_KW)
    assert v.action == "notify" and v.reason == "consecutive_losses_warning"
    assert v.tripped is False


def test_six_losses_is_the_warn_boundary():
    v = evaluate_triggers([-1.0] * 6, 0.0, None, **_KW)
    assert v.action == "notify"


def test_five_losses_is_ok():
    v = evaluate_triggers([-1.0] * 5, 0.0, None, **_KW)
    assert v.action == "ok"


def test_recent_win_breaks_the_streak():
    # newest-first: a win at the front resets the streak → ok despite 10 prior losses.
    v = evaluate_triggers([1.0] + [-1.0] * 10, 0.0, None, **_KW)
    assert v.action == "ok"


def test_halt_takes_precedence_over_warn():
    # 12 losses is in both tiers — the hard halt wins.
    v = evaluate_triggers([-1.0] * 12, 0.0, None, **_KW)
    assert v.action == "pause" and v.reason == "consecutive_losses"


# ------------------------------------------------ pure trigger logic — drawdown


def test_drawdown_pauses_at_boundary():
    # 33% of 50_000 = 16_500; realized == -16_500 pauses (inclusive <=).
    v = evaluate_triggers([], -16_500.0, 50_000.0, **_KW)
    assert v.action == "pause" and v.reason == "drawdown"


def test_drawdown_just_inside_boundary_is_ok():
    v = evaluate_triggers([], -16_499.99, 50_000.0, **_KW)
    assert v.action == "ok"


def test_drawdown_inert_when_allocation_unset():
    # allocation None → DD brake inert; no streak → ok even at a massive loss.
    v = evaluate_triggers([], -9_999_999.0, None, **_KW)
    assert v.action == "ok"


def test_drawdown_inert_when_allocation_zero():
    v = evaluate_triggers([], -9_999_999.0, 0.0, **_KW)
    assert v.action == "ok"


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


def _build(*, db: MagicMock | None = None, halted: bool = False, params=RISK):
    raised: list[str] = []
    flattened: list[bool] = []

    async def flatten():
        flattened.append(True)

    async def equity_base():
        return 1_000_000.0

    ks = KillSwitch(
        db=db if db is not None else _db([]),
        is_halted=lambda: halted,
        raise_halt=lambda reason: raised.append(reason),
        flatten=flatten,
        equity_base=equity_base,
        params=params,
    )
    return ks, raised, flattened


def _losses(n: int) -> list[dict]:
    now = datetime.now(UTC).isoformat()
    return [{"pnl_net": -1.0, "exit_filled_at": now} for _ in range(n)]


async def test_poll_disabled_takes_no_action():
    ks, raised, flat = _build(params=replace(RISK, kill_switch_enabled=False))
    v = await ks.poll_once()
    assert v.action == "ok"
    assert raised == [] and flat == []


async def test_poll_already_halted_does_not_reflatten():
    ks, raised, flat = _build(halted=True, db=_db(_losses(10)))
    await ks.poll_once()
    assert raised == [] and flat == []


async def test_poll_pauses_on_ten_losses_halts_and_flattens():
    ks, raised, flat = _build(db=_db(_losses(10)))
    v = await ks.poll_once()
    assert v.action == "pause" and v.reason == "consecutive_losses"
    assert raised == ["kill_switch:consecutive_losses"]
    assert flat == [True]


async def test_poll_six_losses_notifies_without_halting():
    ks, raised, flat = _build(db=_db(_losses(6)))
    v = await ks.poll_once()
    assert v.action == "notify"
    assert raised == [] and flat == []  # NOTIFY tier never pauses or flattens


async def test_poll_warn_notification_is_idempotent():
    ks, raised, flat = _build(db=_db(_losses(7)))
    v1 = await ks.poll_once()
    v2 = await ks.poll_once()
    assert v1.action == "notify" and v2.action == "notify"
    # Idempotent: still no halt; the alert is fired once (tracked internally).
    assert raised == [] and flat == []
    assert ks._warn_notified is True


async def test_poll_warn_resets_after_recovery():
    # 6 losses → notify (armed). Then a win → ok (re-armed). Then 6 again → notify.
    ks, raised, _flat = _build(db=_db(_losses(6)))
    await ks.poll_once()
    assert ks._warn_notified is True
    # Recovery: most recent trade is a win → streak 0 → ok → re-arm.
    win_rows = [{"pnl_net": 5.0, "exit_filled_at": datetime.now(UTC).isoformat()}] + _losses(6)
    ks._db.select = AsyncMock(return_value=win_rows)
    v = await ks.poll_once()
    assert v.action == "ok"
    assert ks._warn_notified is False


async def test_poll_no_streak_takes_no_action():
    ks, raised, flat = _build(db=_db([{"pnl_net": 5.0, "exit_filled_at": None}]))
    v = await ks.poll_once()
    assert v.action == "ok"
    assert raised == [] and flat == []


async def test_poll_drawdown_pauses_with_allocation_set():
    # Explicit epoch an hour ago so the loss (now) lands after it deterministically.
    epoch = datetime.now(UTC) - timedelta(hours=1)
    params = replace(
        RISK,
        kill_switch_allocation_usd=50_000.0,
        kill_switch_max_drawdown_pct=33.0,
        kill_switch_pnl_epoch=epoch.isoformat(),
    )
    rows = [{"pnl_net": -20_000.0, "exit_filled_at": datetime.now(UTC).isoformat()}]
    ks, raised, flat = _build(db=_db(rows), params=params)
    v = await ks.poll_once()
    assert v.action == "pause" and v.reason == "drawdown"
    assert raised == ["kill_switch:drawdown"]
    assert flat == [True]


async def test_poll_drawdown_inert_when_allocation_unset():
    # Default RISK has allocation unset → a huge loss does NOT pause via drawdown.
    rows = [{"pnl_net": -500_000.0, "exit_filled_at": datetime.now(UTC).isoformat()}]
    ks, raised, flat = _build(db=_db(rows))
    v = await ks.poll_once()
    assert v.action == "ok"
    assert raised == [] and flat == []


async def test_poll_pre_epoch_losses_excluded_from_drawdown():
    # Epoch is now; a loss 2 days ago is before the epoch → not counted for DD.
    epoch = datetime.now(UTC)
    params = replace(
        RISK,
        kill_switch_allocation_usd=50_000.0,
        kill_switch_max_drawdown_pct=33.0,
        kill_switch_pnl_epoch=epoch.isoformat(),
    )
    old_loss = (epoch - timedelta(days=2)).isoformat()
    rows = [{"pnl_net": -20_000.0, "exit_filled_at": old_loss}]
    ks, raised, flat = _build(db=_db(rows), params=params)
    v = await ks.poll_once()
    assert v.action == "ok"  # the pre-epoch loss doesn't count
    assert raised == [] and flat == []


async def test_poll_fail_safe_halts_on_evaluator_error_without_raising():
    db = MagicMock()
    db.select = AsyncMock(side_effect=RuntimeError("supabase down"))
    ks, raised, flat = _build(db=db)
    v = await ks.poll_once()  # must NOT raise
    assert v.action == "pause" and v.reason == "error"
    assert raised == ["kill_switch:evaluator_error"]
    assert flat == []  # fail-safe blocks entries; does not act on an unknown position


# ----------------------------- transient evaluator-fault tolerance (handoff v17)


async def test_poll_single_transient_error_does_not_halt():
    # One Supabase httpx.ReadTimeout — counter increments, NO halt (default max 3).
    db = MagicMock()
    db.select = AsyncMock(side_effect=httpx.ReadTimeout("supabase blip"))
    ks, raised, flat = _build(db=db)
    v = await ks.poll_once()  # must NOT raise, must NOT halt
    assert v.action == "ok" and v.reason == "transient_error"
    assert raised == [] and flat == []
    assert ks._consec_eval_errors == 1


async def test_poll_max_consecutive_transient_errors_halts_on_the_max_th():
    db = MagicMock()
    # 3 consecutive transient timeouts (default KILL_SWITCH_MAX_CONSEC_EVAL_ERRORS=3).
    db.select = AsyncMock(
        side_effect=[  # 3 elements — one per poll; halt expected on the 3rd
            httpx.ReadTimeout("blip 1"),
            httpx.ConnectTimeout("blip 2"),
            httpx.ReadTimeout("blip 3"),
        ]
    )
    ks, raised, flat = _build(db=db)
    v1 = await ks.poll_once()
    v2 = await ks.poll_once()
    assert v1.action == "ok" and v2.action == "ok"  # 1/3 and 2/3 tolerated
    assert raised == []
    v3 = await ks.poll_once()  # 3/3 — fail-safe halt
    assert v3.action == "pause" and v3.reason == "error"
    assert raised == ["kill_switch:evaluator_error"]
    assert flat == []  # fail-safe blocks entries; takes no position action


async def test_poll_transient_then_clean_poll_resets_counter():
    db = MagicMock()
    db.select = AsyncMock(
        side_effect=[  # 4 elements: 2 timeouts, one clean poll (empty rows), 1 timeout
            httpx.ReadTimeout("blip 1"),
            httpx.ReadTimeout("blip 2"),
            [],  # clean poll → counter resets to 0
            httpx.ReadTimeout("blip 3"),
        ]
    )
    ks, raised, flat = _build(db=db)
    await ks.poll_once()  # 1/3
    await ks.poll_once()  # 2/3
    assert ks._consec_eval_errors == 2
    v_clean = await ks.poll_once()  # clean → reset
    assert v_clean.action == "ok"
    assert ks._consec_eval_errors == 0
    v_after = await ks.poll_once()  # a fresh single transient does NOT halt
    assert v_after.action == "ok" and v_after.reason == "transient_error"
    assert raised == [] and flat == []
    assert ks._consec_eval_errors == 1


async def test_poll_non_transient_logic_error_halts_on_first_occurrence():
    # A KeyError is a code bug, not a network blip — it must halt immediately,
    # never ride the transient tolerance.
    db = MagicMock()
    db.select = AsyncMock(side_effect=KeyError("pnl_net"))
    ks, raised, flat = _build(db=db)
    v = await ks.poll_once()
    assert v.action == "pause" and v.reason == "error"
    assert raised == ["kill_switch:evaluator_error"]  # halted on the FIRST hit
    assert ks._consec_eval_errors == 0  # not counted as transient


def test_max_consec_eval_errors_defaults_to_three_not_zero():
    # Unset env → default 3 (a 0 default would fail-safe halt on the first blip).
    assert RISK.kill_switch_max_consec_eval_errors == 3
    assert RISK.kill_switch_max_consec_eval_errors > 0


# ----------------------------- PR-A — consecutive losses are time-ordered over N


async def test_evaluate_orders_closes_globally_by_time_across_lifecycles():
    # PR-A — with N simultaneous positions the consecutive-loss streak must be
    # computed over ALL lifecycles' closes in TIME order, not per-symbol. The
    # evaluator selects every CLOSED row ordered by exit_filled_at desc with NO
    # symbol filter, so two symbols' closes interleave correctly: a WIN that closed
    # between two losses (newest-first) breaks the streak.
    now = datetime.now(UTC)
    rows = [
        {"pnl_net": -1.0, "exit_filled_at": (now - timedelta(seconds=10)).isoformat()},  # loss
        {"pnl_net": 5.0, "exit_filled_at": (now - timedelta(seconds=20)).isoformat()},  # win
        {"pnl_net": -1.0, "exit_filled_at": (now - timedelta(seconds=30)).isoformat()},  # loss
    ]
    db = _db(rows)
    ks, raised, flat = _build(db=db)

    verdict = await ks.poll_once()

    # Leading run is a single loss (broken by the interleaved win) → ok, no halt.
    assert verdict.action == "ok"
    assert raised == [] and flat == []
    # Query is global + time-ordered: CLOSED, exit_filled_at.desc, NO symbol filter.
    call = db.select.await_args
    assert call.args[0] == "lifecycles"
    filters = call.kwargs["filters"]
    assert filters["state"] == "eq.CLOSED"
    assert filters["order"] == "exit_filled_at.desc"
    assert "symbol" not in filters
