"""Pure ratchet logic for the SeanBot V3/V12 bot-walked stop (EXIT_MODE=trailing).

Replaces PR #91's standalone native IB TRAIL: that reproduced the 150-trail tail
but MISSED the +50 lock-in, the single biggest edge for this low-win-rate
strategy. Here the bot walks a single resting GTC SELL STP UP only, matching
SeanBot ``execution/executor.py`` ``check_exits`` (V3 + V12) bar-by-bar.

No IBKR imports — these are pure functions evaluated on each 1-min bar close.
The caller (``OrderRouter``) owns broker truth: it reads the resting stop's live
price as ``current_stop`` (§0.5.98), tracks ``highest`` per position, and only
modifies the stop when ``compute_ratcheted_stop`` returns a strictly-better price.

LONG ladder (SHORT is mirror-symmetric), with ``peak = highest - entry``:
    base            : sl = entry - stop_loss_pts                       (75)
    peak >= lock_in : sl = max(sl, entry + lock_in_pts)               (+50 lock)
    peak >= trail   : sl = max(sl, max(entry, highest - trail_offset_pts))
The trail overtakes the lock at ``peak >= lock_in + trail_offset_pts`` (≈ +200).
"""

from __future__ import annotations

from src.state_machine import Direction

TICK_SIZE = 0.25  # MNQ (config.instruments.MNQ.tick_size); a stop must be tick-aligned.


def round_to_tick(price: float, tick: float = TICK_SIZE) -> float:
    """Round ``price`` to the nearest ``tick`` (IBKR rejects off-tick stops)."""
    return round(round(price / tick) * tick, 2)


def _long_sl_target(
    entry: float,
    highest: float,
    *,
    stop_loss_pts: float,
    lock_in_pts: float,
    trail_offset_pts: float,
) -> float:
    """SeanBot LONG sl_target before the ratchet-up gate. Never below entry-SL."""
    peak = highest - entry
    sl_target = entry - stop_loss_pts
    if peak >= lock_in_pts:
        sl_target = max(sl_target, entry + lock_in_pts)
    if peak >= trail_offset_pts:
        sl_target = max(sl_target, max(entry, highest - trail_offset_pts))
    return sl_target


def compute_ratcheted_stop(
    entry: float,
    highest: float,
    current_stop: float | None,
    *,
    direction: Direction,
    stop_loss_pts: float,
    lock_in_pts: float,
    trail_offset_pts: float,
) -> float | None:
    """Return the new (tick-rounded) stop price if it should ratchet, else None.

    Ratchet is one-directional: for a LONG the stop only moves UP (toward price),
    for a SHORT only DOWN. ``current_stop`` is the broker's live stop price (None
    when unknown — then any computed target is proposed). Returns None when the
    target does not improve on ``current_stop`` (no broker call needed)."""
    if direction is Direction.LONG:
        target = round_to_tick(
            _long_sl_target(
                entry,
                highest,
                stop_loss_pts=stop_loss_pts,
                lock_in_pts=lock_in_pts,
                trail_offset_pts=trail_offset_pts,
            )
        )
        if current_stop is None or target > current_stop:
            return target
        return None

    # SHORT — mirror: lowest tracks the run, stop ratchets DOWN only.
    peak = entry - highest  # highest is the LOWEST price seen for a SHORT
    sl_target = entry + stop_loss_pts
    if peak >= lock_in_pts:
        sl_target = min(sl_target, entry - lock_in_pts)
    if peak >= trail_offset_pts:
        sl_target = min(sl_target, min(entry, highest + trail_offset_pts))
    target = round_to_tick(sl_target)
    if current_stop is None or target < current_stop:
        return target
    return None


def should_hard_exit(
    *,
    entry: float,
    bar_close: float,
    direction: Direction,
    hard_ceiling_pts: float,
) -> bool:
    """True when price has run ``hard_ceiling_pts`` in profit → market-exit (V3 cap).

    LONG: ``close >= entry + ceiling``; SHORT: ``close <= entry - ceiling``."""
    if direction is Direction.LONG:
        return bar_close >= entry + hard_ceiling_pts
    return bar_close <= entry - hard_ceiling_pts
