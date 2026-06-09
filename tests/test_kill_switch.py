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


# --------------------------------------------- PR #126: cluster-mode accounting
# Default OFF must be byte-identical to per-trade counting (regression guard); ON
# collapses correlated stop-clusters (entries within the window) into one loss
# event so a concurrent book's correlated cluster does not trip the single-
# position-tuned halt (the §4 kill-switch trap).


def test_cluster_mode_off_is_identical():
    # The regression guard: a serial-loss stream halts at exactly 10 whether or not
    # entry-bar info is passed, as long as cluster_mode stays OFF (the default).
    pnls = [-1.0] * 10
    entry_bars = list(range(10))  # all on distinct bars
    off_default = evaluate_triggers(pnls, 0.0, None, **_KW)
    off_with_bars = evaluate_triggers(
        pnls, 0.0, None, **_KW, cluster_mode=False, entry_bars_newest_first=entry_bars
    )
    assert off_default.action == "pause" and off_default.reason == "consecutive_losses"
    assert off_with_bars.action == "pause" and off_with_bars.reason == "consecutive_losses"
    # 9 serial losses still only NOTIFY in both — no off-by-one drift from the new arg.
    assert evaluate_triggers([-1.0] * 9, 0.0, None, **_KW).action == "notify"
    assert (
        evaluate_triggers(
            [-1.0] * 9, 0.0, None, **_KW, cluster_mode=False, entry_bars_newest_first=list(range(9))
        ).action
        == "notify"
    )


def test_cluster_mode_collapses_correlated_stops():
    # 12 losses that per-trade mode would HALT (>=10). They are 3 clusters of 4,
    # each cluster's 4 entries on the SAME bar (correlated stop-out of a concurrent
    # book). With cluster_mode ON they collapse to 3 loss EVENTS → below halt 10 and
    # below warn 6 → OK, where per-trade mode pauses.
    pnls = [-1.0] * 12
    entry_bars = [
        30,
        30,
        30,
        30,
        20,
        20,
        20,
        20,
        10,
        10,
        10,
        10,
    ]  # newest-first, 3 same-bar clusters
    on = evaluate_triggers(
        pnls,
        0.0,
        None,
        **_KW,
        cluster_mode=True,
        cluster_window_bars=1,
        entry_bars_newest_first=entry_bars,
    )
    off = evaluate_triggers(pnls, 0.0, None, **_KW)
    assert off.action == "pause"  # per-trade: 12 >= 10
    assert on.action == "ok"  # clustered: 3 events < warn 6


def test_cluster_mode_collapsed_events_still_trip_when_enough_clusters():
    # 10 DISTINCT-bar losses (no two entries within the window) form 10 separate
    # loss events even ON — so a genuinely serial losing streak still halts.
    pnls = [-1.0] * 10
    entry_bars = [100, 90, 80, 70, 60, 50, 40, 30, 20, 10]  # all > window apart
    on = evaluate_triggers(
        pnls,
        0.0,
        None,
        **_KW,
        cluster_mode=True,
        cluster_window_bars=1,
        entry_bars_newest_first=entry_bars,
    )
    assert on.action == "pause" and on.reason == "consecutive_losses"


def test_cluster_mode_without_entry_bars_falls_back_to_per_trade(caplog):
    import logging

    caplog.set_level(logging.WARNING)
    # Flag ON but no entry-bar info → must behave EXACTLY like per-trade counting
    # (and warn loudly that the plumbing is missing).
    v = evaluate_triggers(
        [-1.0] * 10, 0.0, None, **_KW, cluster_mode=True, entry_bars_newest_first=None
    )
    assert v.action == "pause" and v.reason == "consecutive_losses"
    assert any("cluster mode set but no entry-bar info" in r.getMessage() for r in caplog.records)


def test_cluster_collapse_helper_sums_and_breaks_on_win():
    from src.execution.kill_switch import _collapse_loss_clusters

    # newest-first: [loss, loss(same bar)] then a WIN then [loss(distinct)].
    pnls = [-2.0, -3.0, 5.0, -4.0]
    entry_bars = [50, 50, 40, 30]
    out = _collapse_loss_clusters(pnls, entry_bars, window_bars=1)
    # First two losses (same bar) collapse to -5.0; the win passes; the last loss alone.
    assert out == [-5.0, 5.0, -4.0]


def test_cluster_collapse_helper_misaligned_inputs_are_unchanged():
    from src.execution.kill_switch import _collapse_loss_clusters

    pnls = [-1.0, -1.0, -1.0]
    entry_bars = [10, 10]  # wrong length → conservative: return unchanged
    assert _collapse_loss_clusters(pnls, entry_bars, window_bars=1) == pnls


def test_cluster_mode_default_field_off_on_riskparams():
    # Task F parity: the live Config (RiskParams) carries the flag, defaulted OFF.
    assert RISK.kill_switch_cluster_mode is False
    assert RISK.cluster_window_bars == 1


# ------------------ Task E (PR — entry-bar plumbing): the LIVE poller path
# These exercise KillSwitch._evaluate end-to-end (wrapper-level db.select mock):
# the new code reads entry_filled_at, derives epoch-minute bars aligned 1:1 with
# the pnls, and feeds evaluate_triggers. With the flag OFF (today) the bars are
# ignored ⇒ byte-identical; ON, a correlated book collapses but a single-position
# book stays inert.


def _cluster_rows(loss_count: int, entry_iso: str, *, exit_iso: str | None = None) -> list[dict]:
    """``loss_count`` losing CLOSED rows that all entered at ``entry_iso`` (one
    correlated cluster). Mirrors the live lifecycles columns the poller selects."""
    xt = exit_iso if exit_iso is not None else entry_iso
    return [
        {"pnl_net": -1.0, "exit_filled_at": xt, "entry_filled_at": entry_iso}
        for _ in range(loss_count)
    ]


async def test_poll_cluster_mode_off_ignores_supplied_entry_bars():
    # Regression guard: 12 same-instant (correlated) losses that WOULD collapse if
    # the flag were ON. With cluster_mode OFF (today's default) the entry bars are
    # ignored → 12 consecutive losses → PAUSE, byte-identical to the no-bar path.
    base = datetime.now(UTC)
    rows = _cluster_rows(12, base.isoformat())
    ks, raised, flat = _build(db=_db(rows))  # default RISK → cluster_mode False
    v = await ks.poll_once()
    assert v.action == "pause" and v.reason == "consecutive_losses"
    assert raised == ["kill_switch:consecutive_losses"]
    assert flat == [True]
    # The plumbing extends the query to fetch the entry timestamp IN-MODULE.
    call = ks._db.select.await_args
    assert "entry_filled_at" in call.kwargs["columns"]


async def test_poll_cluster_mode_on_collapses_correlated_book():
    # 12 losses in 3 same-instant clusters of 4 (a concurrent book's correlated
    # stop-outs, clusters 60 min apart). cluster_mode ON collapses each cluster to
    # ONE loss event → 3 events < warn 6 → OK, where per-trade mode PAUSES at 10.
    base = datetime.now(UTC)
    rows: list[dict] = []
    for cluster_offset in (0, 60, 120):  # newest cluster first
        ts = (base - timedelta(minutes=cluster_offset)).isoformat()
        rows.extend(_cluster_rows(4, ts))
    params = replace(RISK, kill_switch_cluster_mode=True)
    ks, raised, flat = _build(db=_db(rows), params=params)
    v = await ks.poll_once()
    assert v.action == "ok"  # 3 collapsed events < warn 6
    assert raised == [] and flat == []


async def test_poll_cluster_mode_on_single_position_shape_is_inert():
    # 10 losses each entered 5 min apart (today's single-position book — entries
    # never overlap). No two entries fall within the 1-bar window → 10 separate
    # events even ON → still PAUSE, IDENTICAL to per-trade. Proves the flag is inert
    # for today's non-concurrent book.
    base = datetime.now(UTC)
    rows = [
        {
            "pnl_net": -1.0,
            "exit_filled_at": base.isoformat(),
            "entry_filled_at": (base - timedelta(minutes=5 * i)).isoformat(),
        }
        for i in range(10)
    ]
    params = replace(RISK, kill_switch_cluster_mode=True)
    ks, raised, flat = _build(db=_db(rows), params=params)
    v = await ks.poll_once()
    assert v.action == "pause" and v.reason == "consecutive_losses"
    assert raised == ["kill_switch:consecutive_losses"]
    assert flat == [True]


async def test_poll_cluster_mode_on_null_entry_bars_fall_back_conservatively():
    # cluster_mode ON but every row's entry_filled_at is null → all entry bars are
    # None → NO loss is merged (conservative #129 contract) → 10 losses still PAUSE,
    # exactly as per-trade counting would (never hides a loss it can't prove correlated).
    base = datetime.now(UTC)
    rows = [
        {"pnl_net": -1.0, "exit_filled_at": base.isoformat(), "entry_filled_at": None}
        for _ in range(10)
    ]
    params = replace(RISK, kill_switch_cluster_mode=True)
    ks, raised, flat = _build(db=_db(rows), params=params)
    v = await ks.poll_once()
    assert v.action == "pause" and v.reason == "consecutive_losses"
    assert raised == ["kill_switch:consecutive_losses"]


def test_entry_bar_minutes_unit_and_none_fallback():
    # The unit derivation: ISO entry ts → epoch-MINUTES (float); two entries 30s
    # apart differ by 0.5 (< window 1 ⇒ same bar); missing/garbage ts → None.
    from src.execution.kill_switch import _entry_bar_minutes

    base = datetime.now(UTC)
    a = _entry_bar_minutes(base.isoformat())
    b = _entry_bar_minutes((base + timedelta(seconds=30)).isoformat())
    assert a is not None and b is not None
    assert abs(b - a) == pytest.approx(0.5)
    assert _entry_bar_minutes(None) is None
    assert _entry_bar_minutes("not-a-timestamp") is None
