"""Tests for src.execution.trail_manager — pure SeanBot V3/V12 ratchet logic."""

from __future__ import annotations

from src.execution.trail_manager import (
    compute_ratcheted_stop,
    round_to_tick,
    should_hard_exit,
)
from src.state_machine import Direction

# stop_loss_pts=75, lock_in_pts=50, trail_offset_pts=150 (the live defaults).
_KW = {"stop_loss_pts": 75.0, "lock_in_pts": 50.0, "trail_offset_pts": 150.0}


def _long(entry, highest, current_stop):
    return compute_ratcheted_stop(entry, highest, current_stop, direction=Direction.LONG, **_KW)


def _short(entry, lowest, current_stop):
    return compute_ratcheted_stop(entry, lowest, current_stop, direction=Direction.SHORT, **_KW)


# ------------------------------------------------------------------- LONG ladder


def test_base_stop_is_entry_minus_75():
    # peak 0 → base = entry - stop_loss_pts; proposed when there is no stop yet.
    assert _long(20000.0, 20000.0, None) == 19925.0


def test_peak_49_does_not_lock_in():
    # peak 49 < 50 → still base; with the stop already at base there is no ratchet.
    assert _long(20000.0, 20049.0, 19925.0) is None
    # raw target (no current stop) is still the base stop, NOT a lock.
    assert _long(20000.0, 20049.0, None) == 19925.0


def test_peak_50_locks_in_entry_plus_50():
    # V12 lock-in: the first +50 of run guarantees a min +50 win.
    assert _long(20000.0, 20050.0, 19925.0) == 20050.0


def test_peak_150_still_lock_in_trail_below_lock():
    # highest=entry+150 → trail target = max(entry, highest-150) = entry < lock(+50).
    assert _long(20000.0, 20150.0, 19925.0) == 20050.0


def test_peak_201_trail_overtakes_lock():
    # highest=entry+201 → trail = highest-150 = entry+51 > lock entry+50.
    assert _long(20000.0, 20201.0, 20050.0) == 20051.0


def test_long_stop_never_decreases():
    # computed target (entry+50) is below the current stop → no ratchet.
    assert _long(20000.0, 20050.0, 20060.0) is None


# ------------------------------------------------------------------ SHORT mirror


def test_short_locks_in_entry_minus_50():
    # SHORT: lowest=entry-50 → peak 50 → lock at entry-50 (a min +50 win).
    assert _short(20000.0, 19950.0, 20075.0) == 19950.0


def test_short_stop_never_increases():
    # SHORT ratchets DOWN only; a higher target than the current stop is rejected.
    assert _short(20000.0, 19950.0, 19940.0) is None


# ------------------------------------------------------------- tick + hard exit


def test_round_to_tick_aligns_to_quarter_point():
    assert round_to_tick(20050.13) == 20050.25
    assert round_to_tick(20050.10) == 20050.0


def test_hard_exit_long_at_ceiling():
    assert (
        should_hard_exit(
            entry=20000.0, bar_close=21000.0, direction=Direction.LONG, hard_ceiling_pts=1000.0
        )
        is True
    )
    assert (
        should_hard_exit(
            entry=20000.0, bar_close=20999.0, direction=Direction.LONG, hard_ceiling_pts=1000.0
        )
        is False
    )


def test_hard_exit_short_at_ceiling():
    assert (
        should_hard_exit(
            entry=20000.0, bar_close=19000.0, direction=Direction.SHORT, hard_ceiling_pts=1000.0
        )
        is True
    )
