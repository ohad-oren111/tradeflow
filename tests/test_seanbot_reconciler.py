"""Tests for the app-side SeanBot reconciler (Track 4). No IB, no DB network,
no orders — fresh objects per test, decisions injected via a getter.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

from src.comparison.seanbot_reconciler import SeanbotReconciler, classify, nearest_decision


def _decision(ts: str, decision: str, *, failed: str | None = None, close=30310.0, sma100=30309.0):
    return {
        "ts": ts,
        "decision": decision,
        "failed": failed,
        "close": close,
        "sma100": sma100,
        "regime_ok": True,
        "touch_ok": decision != "noop_filter" or failed != "touch",
        "ma_order_ok": True,
        "bullish_ok": True,
        "gap_ok": True,
    }


def _entry(message_id: int = 1, ts: str = "2026-05-28T22:05:30+00:00") -> dict:
    return {
        "ts": ts,
        "channel": "Trading NQ Triggers",
        "message_id": message_id,
        "type": "entry",
        "direction": "long",
        "symbol": "MNQ",
        "price": 30301.75,
        "created_at": ts,
    }


def _getter(decisions: list[dict]):
    """Return a zero-arg decisions getter (factory avoids E731 lambda binding)."""

    def _get() -> list[dict]:
        return decisions

    return _get


def _reconciler(getter, tmp_path) -> SeanbotReconciler:
    return SeanbotReconciler(
        decisions_getter=getter, journal_path=str(tmp_path / "reconciliations.jsonl")
    )


# ----- nearest_decision -----------------------------------------------------


def test_nearest_decision_picks_smallest_nonneg_gap():
    from datetime import UTC, datetime

    sig = datetime(2026, 5, 28, 22, 5, 30, tzinfo=UTC)
    decisions = [
        _decision("2026-05-28T22:04:00+00:00", "noop_filter", failed="touch"),  # 90s
        _decision("2026-05-28T22:05:00+00:00", "long_signal"),  # 30s  <- nearest
        _decision("2026-05-28T22:06:00+00:00", "noop_regime"),  # future, excluded
    ]
    near = nearest_decision(decisions, sig, tolerance_sec=120)
    assert near["decision"] == "long_signal"


def test_nearest_decision_none_when_all_outside_tolerance():
    from datetime import UTC, datetime

    sig = datetime(2026, 5, 28, 22, 5, 30, tzinfo=UTC)
    decisions = [_decision("2026-05-28T22:00:00+00:00", "long_signal")]  # 330s ago
    assert nearest_decision(decisions, sig, tolerance_sec=120) is None


# ----- classify branches ----------------------------------------------------


def test_classify_agree_enter():
    rec = classify(_entry(), [_decision("2026-05-28T22:05:00+00:00", "long_signal")])
    assert rec["classification"] == "AGREE_ENTER"


def test_classify_miss_regime():
    rec = classify(_entry(), [_decision("2026-05-28T22:05:00+00:00", "noop_regime")])
    assert rec["classification"] == "MISS-regime"


def test_classify_miss_filter_touch():
    rec = classify(
        _entry(), [_decision("2026-05-28T22:05:00+00:00", "noop_filter", failed="touch")]
    )
    assert rec["classification"] == "MISS-filter:touch"
    assert "touch" in rec["justification"]


def test_classify_miss_session_edge():
    rec = classify(_entry(), [_decision("2026-05-28T22:05:00+00:00", "noop_session_edge")])
    assert rec["classification"] == "MISS-session-edge"


def test_classify_miss_no_bar_when_journal_empty():
    # The blind-feed detector — today's failure mode.
    rec = classify(_entry(), [])
    assert rec["classification"] == "MISS-NO-BAR"
    assert "stale" in rec["justification"] or "dead" in rec["justification"]


# ----- reconcile_row: alert + journal + dedup -------------------------------


def test_reconcile_row_emits_alert_and_record(tmp_path, caplog):
    getter = _getter([_decision("2026-05-28T22:05:00+00:00", "noop_filter", failed="touch")])
    rec = _reconciler(getter, tmp_path)
    with caplog.at_level(logging.INFO, logger="src.comparison.seanbot_reconciler"):
        out = rec.reconcile_row(_entry())

    assert out is not None
    alerts = [
        r.getMessage() for r in caplog.records if "[ALERT] seanbot_reconciliation" in r.getMessage()
    ]
    assert len(alerts) == 1
    assert "class=MISS-filter:touch" in alerts[0]
    assert "SeanBot ENTRY long MNQ @30301.75" in alerts[0]

    # record appended to the JSONL
    lines = (tmp_path / "reconciliations.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    saved = json.loads(lines[0])
    assert saved["classification"] == "MISS-filter:touch"
    assert saved["seanbot"]["message_id"] == 1
    assert "acknowledged_at" in saved


def test_reconcile_row_dedups_on_message_id(tmp_path):
    getter = _getter([_decision("2026-05-28T22:05:00+00:00", "long_signal")])
    rec = _reconciler(getter, tmp_path)
    assert rec.reconcile_row(_entry(message_id=7)) is not None
    assert rec.reconcile_row(_entry(message_id=7)) is None  # dedup


def test_reconcile_row_ignores_non_entry(tmp_path):
    getter = _getter([_decision("2026-05-28T22:05:00+00:00", "long_signal")])
    rec = _reconciler(getter, tmp_path)
    exit_row = {**_entry(), "type": "exit"}
    assert rec.reconcile_row(exit_row) is None


# ----- poll_once: cursor + no order/strategy side effects --------------------


async def test_poll_once_seeds_cursor_then_reconciles(tmp_path):
    getter = _getter([_decision("2026-05-28T22:05:00+00:00", "long_signal")])
    rec = _reconciler(getter, tmp_path)
    db = AsyncMock()
    db.select = AsyncMock(return_value=[_entry(message_id=42)])

    first = await rec.poll_once(db)  # seeds cursor, no query
    assert first == 0
    db.select.assert_not_awaited()

    second = await rec.poll_once(db)
    assert second == 1
    db.select.assert_awaited_once()
    # Read-only: never writes/orders via the db.
    db.insert.assert_not_called()
    db.upsert.assert_not_called()


async def test_poll_once_no_double_count_on_repeat(tmp_path):
    getter = _getter([_decision("2026-05-28T22:05:00+00:00", "long_signal")])
    rec = _reconciler(getter, tmp_path)
    db = AsyncMock()
    await rec.poll_once(db)  # seed cursor
    db.select = AsyncMock(return_value=[_entry(message_id=99)])
    assert await rec.poll_once(db) == 1
    # Same row redelivered -> deduped to 0.
    db.select = AsyncMock(return_value=[_entry(message_id=99)])
    assert await rec.poll_once(db) == 0
