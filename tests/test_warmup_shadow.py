"""Tests for src.warmup_shadow.WarmupShadow — shadow SMA backfill, observe-only.

asyncio_mode=auto (pyproject) → async tests need no decorator, mirroring
tests/test_durable_decision_journal.py. No IB, no network — bars are plain stubs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from src.warmup_shadow import WarmupShadow


def _bars(closes: list[float]) -> list[Any]:
    return [SimpleNamespace(close=c) for c in closes]


def _fetch(bars: list[Any]) -> Callable[[], Awaitable[list[Any]]]:
    async def fetch() -> list[Any]:
        return bars

    return fetch


async def test_seed_from_computes_backfill_sma_from_history() -> None:
    sh = WarmupShadow()
    # 120 closes; the last 100 are 100.0 → sma100 == 100.0, last 50 → ma50 == 100.0
    await sh.seed_from(_fetch(_bars([50.0] * 20 + [100.0] * 100)))
    assert sh.seeded == 120
    out = sh.observe({"close": 100.0}, {"sma100": None}, bar_count=1)
    assert out is not None
    assert out["backfill_sma100"] == pytest.approx(100.0)
    assert out["backfill_ma50"] == pytest.approx(100.0)


async def test_seed_failure_is_swallowed_no_raise() -> None:
    sh = WarmupShadow()

    async def boom() -> list[Any]:
        raise RuntimeError("no historical entitlement")

    await sh.seed_from(boom)  # must NOT raise
    assert sh.seeded == 0
    # no seed → backfill not ready off a single live bar
    out = sh.observe({"close": 100.0}, {"sma100": None}, bar_count=1)
    assert out is not None
    assert out["backfill_sma100"] is None


async def test_observe_diff_vs_live_when_both_available() -> None:
    sh = WarmupShadow()
    await sh.seed_from(_fetch(_bars([100.0] * 100)))
    out = sh.observe({"close": 100.0}, {"sma100": 99.5}, bar_count=101)
    assert out is not None
    assert out["backfill_sma100"] == pytest.approx(100.0)
    assert out["diff"] == pytest.approx(0.5)  # backfill 100.0 - live 99.5


def test_observe_returns_only_measurements_never_a_signal() -> None:
    # Gate-unchanged guard: observe can NEVER hand back a trade signal/decision.
    sh = WarmupShadow()
    out = sh.observe({"close": 100.0}, {"sma100": None}, bar_count=1)
    assert out is not None
    assert set(out) == {"bar_count", "live_sma100", "backfill_sma100", "backfill_ma50", "diff"}
    assert "signal" not in out and "decision" not in out


def test_observe_never_raises_on_garbage() -> None:
    sh = WarmupShadow()
    assert sh.observe(None, None, None) is None
    assert sh.observe({"no_close": 1}, None, 5) is None
    assert sh.observe({"close": "not-a-number"}, None, 5) is None  # float() raises → swallowed


async def test_disabled_is_total_noop() -> None:
    sh = WarmupShadow(enabled=False)
    await sh.seed_from(_fetch(_bars([100.0] * 100)))
    assert sh.seeded == 0
    assert sh.observe({"close": 100.0}, {"sma100": 100.0}, bar_count=1) is None
