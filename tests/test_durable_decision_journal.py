"""Tests for the durable TF decision journal writer (PR-D3c) —
``src.comparison.decision_journal.DecisionJournal``.

Distinct from ``tests/test_decision_journal.py`` (which covers the orchestrator's
in-memory Track-3 ring + the inspect_decisions replay tool). No IB, no DB
network, no orders — fresh mocks per test, decisions injected as plain dicts.

asyncio_mode=auto (pyproject) → async tests need no decorator, mirroring
tests/test_seanbot_reconciler.py.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

from src.comparison.decision_journal import (
    _DECISIONS_ON_CONFLICT,
    _DECISIONS_TABLE,
    DecisionJournal,
    decision_to_row,
)


def _enter(ts: str = "2026-05-28T22:05:30+00:00") -> dict:
    """A long_signal decision — touch-band passed, all gates OK."""
    return {
        "ts": ts,
        "decision": "long_signal",
        "failed": None,
        "close": 30310.0,
        "sma100": 30309.0,
        "regime_ok": True,
        "touch_ok": True,
        "ma_order_ok": True,
        "bullish_ok": True,
        "gap_ok": True,
    }


def _near_miss(ts: str = "2026-05-28T22:06:30+00:00", failed: str = "gap") -> dict:
    """A touch-band near-miss — price touched the band but one gate blocked."""
    return {
        "ts": ts,
        "decision": "noop_filter",
        "failed": failed,
        "close": 30308.0,
        "sma100": 30309.0,
        "regime_ok": True,
        "touch_ok": True,
        "ma_order_ok": True,
        "bullish_ok": True,
        "gap_ok": failed != "gap",
    }


def _warmup(ts: str = "2026-05-28T22:07:30+00:00") -> dict:
    """A warmup bar — indicators not ready, touch_ok is None."""
    return {
        "ts": ts,
        "decision": "noop_warmup",
        "failed": None,
        "close": 30300.0,
        "sma100": None,
        "regime_ok": None,
        "touch_ok": None,
        "ma_order_ok": None,
        "bullish_ok": None,
        "gap_ok": None,
    }


def test_capture_buffers_entry_and_near_miss_drops_warmup() -> None:
    journal = DecisionJournal()
    journal.capture(_enter(), symbol="MNQM6", bar_count=120)
    journal.capture(_near_miss(), symbol="MNQM6", bar_count=121)
    journal.capture(_warmup(), symbol="MNQM6", bar_count=50)
    # Also a touch-failed filter bar must be dropped (touch_ok is False).
    journal.capture(
        {**_near_miss(failed="touch"), "touch_ok": False}, symbol="MNQM6", bar_count=122
    )
    assert journal.buffered == 2


def test_decision_to_row_maps_symbol_bar_count_and_raw() -> None:
    d = _enter()
    row = decision_to_row(d, symbol="MNQM6", bar_count=120)
    assert row["symbol"] == "MNQM6"
    assert row["bar_count"] == 120
    assert row["decision_ts"] == d["ts"]
    assert row["decision"] == "long_signal"
    assert row["failed_gate"] is None
    assert row["sma100"] == 30309.0
    assert row["raw"] == d  # full forward-proof dump


async def test_flush_upserts_with_conflict_key_and_clears_buffer() -> None:
    journal = DecisionJournal()
    journal.capture(_enter(), symbol="MNQM6", bar_count=120)
    journal.capture(_near_miss(), symbol="MNQM6", bar_count=121)

    mock_db = MagicMock()
    mock_db.upsert = AsyncMock(return_value=[{"id": 1}, {"id": 2}])  # 1 call

    flushed = await journal.flush(mock_db)

    assert flushed == 2
    assert journal.buffered == 0
    # Assert on the strategy_decisions call by table name, not call index.
    decision_calls = [
        c for c in mock_db.upsert.await_args_list if c.args and c.args[0] == _DECISIONS_TABLE
    ]
    assert len(decision_calls) == 1
    table, payload = decision_calls[0].args
    assert table == _DECISIONS_TABLE
    assert isinstance(payload, list) and len(payload) == 2
    assert decision_calls[0].kwargs["on_conflict"] == _DECISIONS_ON_CONFLICT
    assert _DECISIONS_ON_CONFLICT == "symbol,decision_ts"


async def test_flush_empty_buffer_is_noop() -> None:
    journal = DecisionJournal()
    mock_db = MagicMock()
    mock_db.upsert = AsyncMock(return_value=True)  # must not be called
    flushed = await journal.flush(mock_db)
    assert flushed == 0
    mock_db.upsert.assert_not_awaited()


async def test_flush_swallows_db_failure_and_preserves_buffer(caplog) -> None:
    journal = DecisionJournal()
    journal.capture(_enter(), symbol="MNQM6", bar_count=120)

    mock_db = MagicMock()
    mock_db.upsert = AsyncMock(side_effect=RuntimeError("supabase down"))  # 1 call, raises

    with caplog.at_level(logging.WARNING):
        flushed = await journal.flush(mock_db)  # must NOT raise

    assert flushed == 0
    assert journal.buffered == 1  # buffer preserved for the next tick
    assert any("flush failed" in r.message for r in caplog.records)


def test_buffer_cap_drops_oldest_when_exceeded() -> None:
    journal = DecisionJournal(buffer_max=3)
    for i in range(5):
        ts = f"2026-05-28T22:0{i}:00+00:00"
        journal.capture(_enter(ts=ts), symbol="MNQM6", bar_count=100 + i)
    assert journal.buffered == 3
    # Oldest two (i=0,1) were dropped; the buffer holds i=2,3,4.
    remaining = list(journal._buffer)
    assert remaining[0]["decision_ts"] == "2026-05-28T22:02:00+00:00"
    assert remaining[-1]["decision_ts"] == "2026-05-28T22:04:00+00:00"


async def test_dropped_count_reset_after_successful_flush() -> None:
    journal = DecisionJournal(buffer_max=2)
    for i in range(4):  # 2 dropped
        journal.capture(_enter(ts=f"2026-05-28T22:0{i}:00+00:00"), symbol="MNQM6", bar_count=i)
    assert journal._dropped_since_flush == 2

    mock_db = MagicMock()
    mock_db.upsert = AsyncMock(return_value=[{"id": 1}, {"id": 2}])  # 1 call
    await journal.flush(mock_db)
    assert journal._dropped_since_flush == 0
