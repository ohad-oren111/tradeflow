"""PR C — bar-gap detection after reconnect/resubscribe.

Covers the pure gap maths, the strategy buffer invalidate/last_bar_time hooks,
and the orchestrator's _handle_post_resubscribe_gap / _reseed_strategy_after_gap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.orchestrator import Orchestrator, _bar_gap_count, _bar_size_seconds
from src.strategy import Sma100BounceStrategy

# --------------------------------------------------------------------- helpers


def _bars(n: int, *, start: datetime, step_min: float = 1.0, close: float = 20000.0) -> list[dict]:
    out = []
    for i in range(n):
        t = start + timedelta(minutes=step_min * i)
        out.append(
            {
                "time": t,
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 100,
            }
        )
    return out


def _hist_bars(n: int, *, start: datetime, close: float = 20000.0) -> list[SimpleNamespace]:
    """ib_async BarData-like objects (.date/.open/.high/.low/.close/.volume)."""
    out = []
    for i in range(n):
        t = start + timedelta(minutes=i)
        out.append(
            SimpleNamespace(
                date=t, open=close, high=close + 1, low=close - 1, close=close, volume=100
            )
        )
    return out


def _make_orch() -> Orchestrator:
    ib = AsyncMock(spec=IBClient)
    db = AsyncMock(spec=SupabaseClient)
    return Orchestrator(ib, db, paper_account="DUQ1234567")


# ----------------------------------------------------------------- _bar_size_seconds


def test_bar_size_seconds_parses_common_units():
    assert _bar_size_seconds("1 min") == 60.0
    assert _bar_size_seconds("5 mins") == 300.0
    assert _bar_size_seconds("30 secs") == 30.0
    assert _bar_size_seconds("1 hour") == 3600.0


def test_bar_size_seconds_defaults_to_one_minute_on_garbage():
    assert _bar_size_seconds("") == 60.0
    assert _bar_size_seconds("weird") == 60.0
    assert _bar_size_seconds("abc min") == 60.0


# -------------------------------------------------------------------- _bar_gap_count


def test_gap_count_contiguous_is_zero():
    t0 = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    assert _bar_gap_count(t0, t0 + timedelta(minutes=1), 60.0) == 0


def test_gap_count_one_missing_bar():
    t0 = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    assert _bar_gap_count(t0, t0 + timedelta(minutes=2), 60.0) == 1


def test_gap_count_multiple_missing_bars():
    t0 = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    assert _bar_gap_count(t0, t0 + timedelta(minutes=10), 60.0) == 9


def test_gap_count_clamps_negative_and_duplicate():
    t0 = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    assert _bar_gap_count(t0, t0, 60.0) == 0  # duplicate
    assert _bar_gap_count(t0, t0 - timedelta(minutes=5), 60.0) == 0  # out of order


# ----------------------------------------------------------- strategy buffer hooks


def test_last_bar_time_none_when_empty():
    strat = Sma100BounceStrategy("MNQM6")
    assert strat.last_bar_time is None


def test_last_bar_time_returns_most_recent():
    strat = Sma100BounceStrategy("MNQM6")
    start = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    strat.seed_bars(_bars(5, start=start))
    assert strat.last_bar_time == start + timedelta(minutes=4)


def test_invalidate_clears_buffer():
    strat = Sma100BounceStrategy("MNQM6")
    strat.seed_bars(_bars(120, start=datetime(2026, 6, 2, 10, 0, tzinfo=UTC)))
    assert strat.bar_count == 120
    strat.invalidate()
    assert strat.bar_count == 0
    assert strat.last_bar_time is None


# --------------------------------------------- _handle_post_resubscribe_gap (sync)


def test_contiguous_first_bar_does_not_invalidate():
    orch = _make_orch()
    start = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    orch._strategy.seed_bars(_bars(120, start=start))
    last = start + timedelta(minutes=119)
    # Next bar is exactly one minute later → contiguous.
    next_bar = {
        "time": last + timedelta(minutes=1),
        "open": 20000,
        "high": 20001,
        "low": 19999,
        "close": 20000,
    }
    assert orch._handle_post_resubscribe_gap(next_bar) is False
    assert orch._strategy.bar_count == 120  # buffer intact


def test_gap_beyond_tolerance_invalidates_buffer():
    orch = _make_orch()
    orch._loop = None  # no loop → invalidate but don't schedule the async re-seed
    start = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    orch._strategy.seed_bars(_bars(120, start=start))
    last = start + timedelta(minutes=119)
    # 10-minute jump → 9 missing bars > tolerance(1) → invalidate.
    gapped = {
        "time": last + timedelta(minutes=10),
        "open": 20000,
        "high": 20001,
        "low": 19999,
        "close": 20000,
    }
    assert orch._handle_post_resubscribe_gap(gapped) is True
    assert orch._strategy.bar_count == 0  # buffer invalidated
    assert orch._reseed_in_progress is False  # cleared on the no-loop path


def test_gap_exactly_at_tolerance_is_allowed():
    orch = _make_orch()
    start = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    orch._strategy.seed_bars(_bars(120, start=start))
    last = start + timedelta(minutes=119)
    # 2-minute jump → 1 missing bar == tolerance(1) → allowed (boundary).
    boundary = {
        "time": last + timedelta(minutes=2),
        "open": 20000,
        "high": 20001,
        "low": 19999,
        "close": 20000,
    }
    assert orch._handle_post_resubscribe_gap(boundary) is False
    assert orch._strategy.bar_count == 120


def test_empty_buffer_skips_gap_check():
    orch = _make_orch()
    assert orch._strategy.bar_count == 0
    bar = {
        "time": datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
        "open": 20000,
        "high": 1,
        "low": 1,
        "close": 20000,
    }
    assert orch._handle_post_resubscribe_gap(bar) is False


# ------------------------------------------------ _reseed_strategy_after_gap (async)


async def test_reseed_after_gap_refills_buffer_and_clears_flag():
    orch = _make_orch()
    orch._reseed_in_progress = True
    orch._strategy.invalidate()  # gapped buffer already cleared
    start = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
    orch._ib.get_historical_bars = AsyncMock(return_value=_hist_bars(150, start=start))

    await orch._reseed_strategy_after_gap(gap_bars=9)

    assert orch._strategy.bar_count == 150  # re-seeded from history
    assert orch._reseed_in_progress is False  # flag cleared in finally


async def test_reseed_failure_is_fail_safe():
    orch = _make_orch()
    orch._reseed_in_progress = True
    orch._strategy.invalidate()
    orch._ib.get_historical_bars = AsyncMock(side_effect=RuntimeError("IB pacing"))

    await orch._reseed_strategy_after_gap(gap_bars=9)

    # Fail-safe: buffer stays empty (live re-warm), flag cleared, no raise.
    assert orch._strategy.bar_count == 0
    assert orch._reseed_in_progress is False


async def test_reseed_short_backfill_rejected_keeps_buffer_empty():
    orch = _make_orch()
    orch._reseed_in_progress = True
    orch._strategy.invalidate()
    # Only 40 bars (<100) → validate_seed rejects → buffer stays empty.
    orch._ib.get_historical_bars = AsyncMock(
        return_value=_hist_bars(40, start=datetime(2026, 6, 2, 9, 0, tzinfo=UTC))
    )

    await orch._reseed_strategy_after_gap(gap_bars=9)

    assert orch._strategy.bar_count == 0
    assert orch._reseed_in_progress is False
