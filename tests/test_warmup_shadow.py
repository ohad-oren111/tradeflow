"""Tests for src.warmup_shadow — live-only shadow SMA + seed sanity helpers.

asyncio_mode=auto (pyproject) → async tests need no decorator. No IB, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.warmup_shadow import (
    WarmupShadow,
    hist_bars_to_dicts,
    validate_seed,
)

# ----------------------------------------------------- live-only shadow


def test_observe_live_sma_none_before_warm_backfill_shown() -> None:
    sh = WarmupShadow()
    out = sh.observe({"close": 100.0}, {"sma100": 99.5})
    assert out is not None
    assert out["live_sma100"] is None  # 1 live bar < 100 → live-only SMA not ready
    assert out["backfill_sma100"] == pytest.approx(99.5)  # strategy's seeded SMA
    assert out["diff"] is None


def test_observe_live_only_sma_and_diff_vs_backfill() -> None:
    sh = WarmupShadow()
    out = None
    for _ in range(100):  # 100 live bars of 100.0 → live-only SMA == 100.0
        out = sh.observe({"close": 100.0}, {"sma100": 99.5})
    assert out is not None
    assert out["live_sma100"] == pytest.approx(100.0)
    assert out["backfill_sma100"] == pytest.approx(99.5)
    assert out["diff"] == pytest.approx(-0.5)  # backfill - live = 99.5 - 100.0
    assert sh.live_bars == 100


def test_observe_never_raises_on_garbage() -> None:
    sh = WarmupShadow()
    assert sh.observe(None, None) is None
    assert sh.observe({"no_close": 1}, None) is None
    assert sh.observe({"close": "x"}, {"sma100": 1.0}) is None


def test_observe_disabled_is_noop() -> None:
    sh = WarmupShadow(enabled=False)
    assert sh.observe({"close": 100.0}, {"sma100": 100.0}) is None
    assert sh.live_bars == 0


# ----------------------------------------------------- validate_seed (fail-safe)


def test_validate_seed_accepts_sane() -> None:
    ok, reason, sma = validate_seed([{"close": 30000.0}] * 100, needed=100)
    assert ok
    assert reason == "ok"
    assert sma == pytest.approx(30000.0)


def test_validate_seed_rejects_short() -> None:
    ok, reason, sma = validate_seed([{"close": 30000.0}] * 50, needed=100)
    assert not ok
    assert "only 50 bars" in reason
    assert sma is None


def test_validate_seed_rejects_absurd_distance() -> None:
    # last close 30000 but the window is dominated by a notional ~60000 → reject.
    seed = [{"close": 60000.0}] * 99 + [{"close": 30000.0}]
    ok, reason, sma = validate_seed(seed, needed=100)
    assert not ok
    assert "from last price" in reason


# ----------------------------------------------------- hist_bars_to_dicts


def test_hist_bars_to_dicts_converts_bardata() -> None:
    bars = [
        SimpleNamespace(
            date=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            open=30000.0,
            high=30010.0,
            low=29990.0,
            close=30005.0,
            volume=42,
        )
    ]
    out = hist_bars_to_dicts(bars)
    assert len(out) == 1
    assert out[0]["close"] == 30005.0
    assert out[0]["open"] == 30000.0
    assert out[0]["low"] == 29990.0
    assert out[0]["time"] == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def test_hist_bars_to_dicts_skips_closeless_bars() -> None:
    out = hist_bars_to_dicts([SimpleNamespace(close=None), SimpleNamespace(close=30000.0)])
    assert len(out) == 1
