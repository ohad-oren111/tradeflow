"""Tests for the bar-liveness watchdog wired into ``src.orchestrator``.

Covers:
- staleness threshold + cooldown + session-edge suppression in
  ``Orchestrator._watchdog_check_bar_liveness``
- ``_on_new_bar`` keeping the last-bar timestamp + recovery log fresh
- ``_count_live_bars_last_60m`` ring-buffer accounting
- ``live_bars_60m`` plumbed through ``get_broker_status_summary``
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.orchestrator import (
    _WATCHDOG_ALERT_COOLDOWN_SEC,
    _WATCHDOG_FEED_HEAL_COOLDOWN_SEC,
    _WATCHDOG_STALE_THRESHOLD_SEC,
    Orchestrator,
)

# A weekday timestamp inside CME RTH that is not in any edge window.
# 2026-05-04 is a Monday; 14:00 UTC = 10:00 EDT (May → daylight saving, -04:00).
_IN_SESSION = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
# 2026-05-02 is a Saturday — always in the edge window.
_SATURDAY = datetime(2026, 5, 2, 14, 0, tzinfo=UTC)


def _make_orch() -> Orchestrator:
    """Minimal orchestrator instance for sync state assertions.

    No IO is exercised; the IBClient + SupabaseClient mocks only need to exist
    so ``Orchestrator.__init__`` doesn't blow up. We never call ``run()``.
    """
    mock_ib = AsyncMock(spec=IBClient)
    mock_ib._host = "127.0.0.1"
    mock_ib._port = 4002
    mock_ib._client_id = 1
    mock_ib._ib = MagicMock(name="raw_IB")
    mock_db = AsyncMock(spec=SupabaseClient)
    return Orchestrator(mock_ib, mock_db, paper_account="DUQ1234567")


# --------------------------------------------------------- watchdog suppression


def test_watchdog_silent_before_subscribe(caplog):
    caplog.set_level(logging.WARNING)
    orch = _make_orch()
    assert orch._last_bar_at is None
    orch._watchdog_check_bar_liveness()
    assert not any("[WATCHDOG]" in r.getMessage() for r in caplog.records)


def test_watchdog_silent_when_recent_bar(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    orch = _make_orch()
    orch._last_bar_at = _IN_SESSION - timedelta(seconds=30)
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(_IN_SESSION),
    )
    orch._watchdog_check_bar_liveness()
    assert not any("[WATCHDOG]" in r.getMessage() for r in caplog.records)


def test_watchdog_silent_during_edge_window(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    orch = _make_orch()
    # Bar last seen 1 hour ago — well past threshold — but now is Saturday,
    # so the watchdog should suppress.
    orch._last_bar_at = _SATURDAY - timedelta(hours=1)
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(_SATURDAY),
    )
    orch._watchdog_check_bar_liveness()
    assert not any("[WATCHDOG]" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------- watchdog fires


def test_watchdog_alerts_when_stale_during_session(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    orch = _make_orch()
    orch._last_bar_at = _IN_SESSION - timedelta(seconds=_WATCHDOG_STALE_THRESHOLD_SEC + 60)
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(_IN_SESSION),
    )
    orch._watchdog_check_bar_liveness()

    warn = [r for r in caplog.records if "[WATCHDOG] no live bar" in r.getMessage()]
    alert = [r for r in caplog.records if "[ALERT] watchdog_stale_bars" in r.getMessage()]
    assert len(warn) == 1, "expected one [WATCHDOG] WARN line"
    assert len(alert) == 1, "expected one [ALERT] line for telegram"
    assert orch._last_bar_alert_at == _IN_SESSION


def test_watchdog_cooldown_suppresses_second_alert(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    orch = _make_orch()
    orch._last_bar_at = _IN_SESSION - timedelta(seconds=_WATCHDOG_STALE_THRESHOLD_SEC + 60)
    orch._last_bar_alert_at = _IN_SESSION - timedelta(seconds=60)
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(_IN_SESSION),
    )
    orch._watchdog_check_bar_liveness()
    assert not any("[WATCHDOG]" in r.getMessage() for r in caplog.records)


def test_watchdog_re_alerts_after_cooldown(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    orch = _make_orch()
    orch._last_bar_at = _IN_SESSION - timedelta(hours=1)
    orch._last_bar_alert_at = _IN_SESSION - timedelta(seconds=_WATCHDOG_ALERT_COOLDOWN_SEC + 60)
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(_IN_SESSION),
    )
    orch._watchdog_check_bar_liveness()
    warn = [r for r in caplog.records if "[WATCHDOG] no live bar" in r.getMessage()]
    assert len(warn) == 1


# --------------------------------------------------------- on_new_bar


def test_on_new_bar_updates_state(monkeypatch):
    orch = _make_orch()
    orch._strategy = MagicMock()
    orch._strategy.on_new_bar = MagicMock(return_value=None)
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(_IN_SESSION),
    )
    orch._on_new_bar({"close": 100.0})
    assert orch._last_bar_at == _IN_SESSION
    assert list(orch._bar_timestamps) == [_IN_SESSION]


def test_on_new_bar_logs_recovery_when_was_alerted(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    orch = _make_orch()
    orch._strategy = MagicMock()
    orch._strategy.on_new_bar = MagicMock(return_value=None)
    orch._last_bar_alert_at = _IN_SESSION - timedelta(minutes=10)
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(_IN_SESSION),
    )
    orch._on_new_bar({"close": 100.0})
    recovery = [r for r in caplog.records if "[WATCHDOG] bar feed recovered" in r.getMessage()]
    alert = [r for r in caplog.records if "[ALERT] watchdog_bar_recovered" in r.getMessage()]
    assert recovery and alert
    assert orch._last_bar_alert_at is None


def test_on_new_bar_records_ts_even_when_strategy_raises(monkeypatch):
    orch = _make_orch()
    orch._strategy = MagicMock()
    orch._strategy.on_new_bar = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(_IN_SESSION),
    )
    orch._on_new_bar({"close": 100.0})
    assert orch._last_bar_at == _IN_SESSION
    assert len(orch._bar_timestamps) == 1


# --------------------------------------------------------- live_bars_60m


def test_count_live_bars_last_60m_prunes_old(monkeypatch):
    orch = _make_orch()
    now = _IN_SESSION
    # 3 bars inside the window, 2 just outside.
    orch._bar_timestamps.extend(
        [
            now - timedelta(minutes=120),
            now - timedelta(minutes=61),
            now - timedelta(minutes=59),
            now - timedelta(minutes=30),
            now - timedelta(seconds=5),
        ]
    )
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(now),
    )
    assert orch._count_live_bars_last_60m() == 3
    # After call, prune should have removed the two old entries.
    assert len(orch._bar_timestamps) == 3


def test_count_live_bars_empty():
    orch = _make_orch()
    assert orch._count_live_bars_last_60m() == 0


async def test_status_summary_includes_live_bars_60m(monkeypatch):
    orch = _make_orch()
    orch._ib.get_positions = AsyncMock(return_value=[])
    orch._ib.get_open_trades = AsyncMock(return_value=[])
    orch._ib._ib.accountSummaryAsync = AsyncMock(return_value=[])
    orch._bar_timestamps.extend(
        [_IN_SESSION - timedelta(minutes=30), _IN_SESSION - timedelta(seconds=10)]
    )
    monkeypatch.setattr(
        "src.orchestrator.datetime",
        _FrozenDatetime(_IN_SESSION),
    )
    summary = await orch.get_broker_status_summary()
    assert summary["live_bars_60m"] == 2


# --------------------------------------------------------- helpers


class _FrozenDatetime:
    """Drop-in replacement for ``datetime`` that returns a frozen ``now``.

    Only ``now`` is overridden; class-method/constructor access is delegated
    to the real ``datetime`` so call sites like ``datetime.fromisoformat`` keep
    working under ``monkeypatch.setattr('src.orchestrator.datetime', ...)``.
    """

    def __init__(self, frozen: datetime) -> None:
        self._frozen = frozen

    def now(self, tz=None):
        if tz is None:
            return self._frozen.replace(tzinfo=None)
        return self._frozen.astimezone(tz)

    def __getattr__(self, name: str):
        return getattr(datetime, name)


# ----------------------------------- PR D — stale-feed self-heal (resubscribe)


def test_watchdog_returns_true_when_stale_during_session(monkeypatch):
    orch = _make_orch()
    orch._last_bar_at = _IN_SESSION - timedelta(seconds=_WATCHDOG_STALE_THRESHOLD_SEC + 60)
    monkeypatch.setattr("src.orchestrator.datetime", _FrozenDatetime(_IN_SESSION))
    assert orch._watchdog_check_bar_liveness() is True


def test_watchdog_returns_false_when_fresh(monkeypatch):
    orch = _make_orch()
    orch._last_bar_at = _IN_SESSION - timedelta(seconds=3)
    monkeypatch.setattr("src.orchestrator.datetime", _FrozenDatetime(_IN_SESSION))
    assert orch._watchdog_check_bar_liveness() is False


def test_watchdog_returns_false_in_edge_window(monkeypatch):
    orch = _make_orch()
    orch._last_bar_at = _SATURDAY - timedelta(hours=2)
    monkeypatch.setattr("src.orchestrator.datetime", _FrozenDatetime(_SATURDAY))
    assert orch._watchdog_check_bar_liveness() is False


def test_watchdog_returns_false_before_first_bar():
    orch = _make_orch()
    assert orch._last_bar_at is None
    assert orch._watchdog_check_bar_liveness() is False


async def test_maybe_heal_resubscribes_when_stale(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    orch = _make_orch()
    orch._start_bar_subscription = AsyncMock()
    monkeypatch.setattr("src.orchestrator.datetime", _FrozenDatetime(_IN_SESSION))

    await orch._maybe_heal_stale_feed()

    orch._start_bar_subscription.assert_awaited_once()
    assert orch._last_feed_heal_at == _IN_SESSION
    assert any("[FEED] stale-feed self-heal" in r.getMessage() for r in caplog.records)
    assert any("[ALERT] feed_self_heal_resubscribe" in r.getMessage() for r in caplog.records)


async def test_maybe_heal_respects_cooldown(monkeypatch):
    orch = _make_orch()
    orch._start_bar_subscription = AsyncMock()
    # Last heal was 60s ago — inside the cooldown → no resubscribe.
    orch._last_feed_heal_at = _IN_SESSION - timedelta(seconds=60)
    monkeypatch.setattr("src.orchestrator.datetime", _FrozenDatetime(_IN_SESSION))

    await orch._maybe_heal_stale_feed()

    orch._start_bar_subscription.assert_not_awaited()


async def test_maybe_heal_fires_again_after_cooldown(monkeypatch):
    orch = _make_orch()
    orch._start_bar_subscription = AsyncMock()
    orch._last_feed_heal_at = _IN_SESSION - timedelta(seconds=_WATCHDOG_FEED_HEAL_COOLDOWN_SEC + 60)
    monkeypatch.setattr("src.orchestrator.datetime", _FrozenDatetime(_IN_SESSION))

    await orch._maybe_heal_stale_feed()

    orch._start_bar_subscription.assert_awaited_once()


async def test_maybe_heal_never_raises_on_resubscribe_failure(monkeypatch):
    orch = _make_orch()
    orch._start_bar_subscription = AsyncMock(side_effect=RuntimeError("subscribe boom"))
    monkeypatch.setattr("src.orchestrator.datetime", _FrozenDatetime(_IN_SESSION))

    # Must not raise — self-heal failure can't crash the healthcheck loop.
    await orch._maybe_heal_stale_feed()
    assert orch._last_feed_heal_at == _IN_SESSION
