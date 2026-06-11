"""Generic, single-position, pessimistic-fill event backtest engine for Phase-16.

It does NOT import any prod strategy. A family supplies, per bar t (a CLOSED bar):
  * ``target_pos`` — the position the rule WANTS after bar t (+1 long / -1 short / 0
    flat). The engine acts on it at bar t+1 OPEN (no same-bar fill, no lookahead;
    structurally avoids the live forming-bar bug §0.5.198).
  * an ``ExitSpec`` — how an open trade is managed.

Pessimistic-fill conventions (documented so they are auditable; mirror BT-1 / the
live ratcheted stop):
  * Entry: market at next-bar OPEN, +1 tick adverse (``Slippage.entry_ticks``).
  * Protective stop: checked INTRABAR every bar (BEFORE any target — "stop wins").
    A bar that GAPS through the stop fills at the bar OPEN (worse), else at the stop.
  * Fixed target / mean-revert target: checked intrabar AFTER the stop; a limit-style
    fill (``target_ticks`` slippage); gap-through fills at the open.
  * Signal-driven outs (flip / time-stop / session-close / hard-ceiling): market
    fills, checked AFTER the resting stop/target, so a same-bar stop binds first.

This ordering can only make a trade look WORSE than reality, never better.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .costs import CostModel, Slippage


@dataclass(frozen=True)
class TrailSpec:
    """Live-mirroring ratcheted trailing stop (never loosens). Points from entry."""

    lock_in: float = 50.0  # peak gain at/above which the stop locks to entry+lock_in
    trail_trigger: float = 50.0  # peak gain at/above which the stop trails the peak
    trail_offset: float = 150.0  # stop = peak - trail_offset once triggered
    hard_ceiling: float = 1000.0  # market-exit if peak gain reaches this


@dataclass(frozen=True)
class ExitSpec:
    stop_pts: float | None = None  # fixed protective-stop distance (points)
    stop_atr_mult: float | None = None  # OR vol-scaled: stop = mult * ATR-at-entry
    atr_col: str = "atr60"  # which ATR column feeds the vol stop
    target_pts: float | None = None  # fixed take-profit distance (points)
    target_col: str | None = None  # OR mean-revert: exit when price reaches this live col
    trail: TrailSpec | None = None  # trend ratchet
    exit_on_flip: bool = False  # exit when target_pos flips off the position direction
    max_hold_bars: int | None = None  # time stop
    intraday_only: bool = False  # flat at the last RTH bar of the session
    cooldown_bars: int = 5


@dataclass
class _Pos:
    direction: int
    entry_px: float  # slippage-adjusted
    entry_idx: int
    stop: float
    target: float | None
    peak: float


def run_backtest(
    df: pd.DataFrame,
    target_pos: np.ndarray,
    spec: ExitSpec,
    *,
    costs: CostModel | None = None,
    slip: Slippage | None = None,
    contracts: int = 1,
) -> pd.DataFrame:
    """Replay ``target_pos`` over ``df`` under ``spec``; return a trade ledger frame."""
    costs = costs or CostModel()
    slip = slip or Slippage()
    n = len(df)
    target_pos = np.asarray(target_pos, dtype=float)

    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    times = df["time"].to_numpy()
    years = df["year"].to_numpy() if "year" in df else np.zeros(n, int)
    rth = df["rth"].to_numpy(bool) if "rth" in df else np.ones(n, bool)
    atr = df[spec.atr_col].to_numpy(float) if spec.stop_atr_mult else None
    tcol = df[spec.target_col].to_numpy(float) if spec.target_col else None

    # last RTH bar per session (for flat-by-close)
    last_rth = np.zeros(n, bool)
    if spec.intraday_only and "rth_date" in df:
        rth_idx = np.where(rth)[0]
        if len(rth_idx):
            grp = df["rth_date"].to_numpy()[rth_idx]
            last_in_grp = pd.Series(rth_idx).groupby(grp).max().to_numpy()
            last_rth[last_in_grp] = True

    trades: list[dict] = []
    pos: _Pos | None = None
    last_exit_i = -(10**9)

    def close(p: _Pos, raw_px: float, i: int, kind: str, reason: str) -> None:
        fill = slip.adjust_exit(raw_px, p.direction, kind)
        pnl = costs.net_pnl_usd(p.entry_px, fill, p.direction, contracts)
        trades.append(
            {
                "entry_time": times[p.entry_idx],
                "exit_time": times[i],
                "year": int(years[i]),
                "direction": p.direction,
                "entry_px": p.entry_px,
                "exit_px": fill,
                "pnl_usd": pnl,
                "bars_held": i - p.entry_idx,
                "exit_reason": reason,
            }
        )

    for i in range(1, n):
        if pos is not None:
            d = pos.direction
            # 1) protective stop (intrabar, gap-through at open)
            if pos.stop is not None:
                if d == 1 and low[i] <= pos.stop:
                    raw = o[i] if o[i] < pos.stop else pos.stop
                    close(pos, raw, i, "stop", "stop")
                    pos = None
                    last_exit_i = i
                    continue
                if d == -1 and h[i] >= pos.stop:
                    raw = o[i] if o[i] > pos.stop else pos.stop
                    close(pos, raw, i, "stop", "stop")
                    pos = None
                    last_exit_i = i
                    continue
            # 2) fixed target (intrabar)
            if pos.target is not None:
                if d == 1 and h[i] >= pos.target:
                    raw = o[i] if o[i] > pos.target else pos.target
                    close(pos, raw, i, "target", "target")
                    pos = None
                    last_exit_i = i
                    continue
                if d == -1 and low[i] <= pos.target:
                    raw = o[i] if o[i] < pos.target else pos.target
                    close(pos, raw, i, "target", "target")
                    pos = None
                    last_exit_i = i
                    continue
            # 3) mean-revert target (intrabar, live column at bar i)
            if tcol is not None and not np.isnan(tcol[i]):
                tgt = tcol[i]
                if d == 1 and h[i] >= tgt:
                    raw = o[i] if o[i] > tgt else tgt
                    close(pos, raw, i, "target", "meanrev")
                    pos = None
                    last_exit_i = i
                    continue
                if d == -1 and low[i] <= tgt:
                    raw = o[i] if o[i] < tgt else tgt
                    close(pos, raw, i, "target", "meanrev")
                    pos = None
                    last_exit_i = i
                    continue
            # 4) peak + hard ceiling (trailing)
            pos.peak = max(pos.peak, h[i]) if d == 1 else min(pos.peak, low[i])
            peak_gain = (pos.peak - pos.entry_px) * d
            if spec.trail is not None and peak_gain >= spec.trail.hard_ceiling:
                close(pos, c[i], i, "market", "hard_ceiling")
                pos = None
                last_exit_i = i
                continue
            # 5) flip (market at open, from the prior closed bar's signal)
            if spec.exit_on_flip and (target_pos[i - 1] * d) <= 0:
                close(pos, o[i], i, "market", "flip")
                pos = None
                last_exit_i = i
                continue
            # 6) time stop (market at open)
            if spec.max_hold_bars is not None and (i - pos.entry_idx) >= spec.max_hold_bars:
                close(pos, o[i], i, "market", "time")
                pos = None
                last_exit_i = i
                continue
            # 7) session-close flat (market at close of last RTH bar)
            if spec.intraday_only and last_rth[i]:
                close(pos, c[i], i, "market", "session_close")
                pos = None
                last_exit_i = i
                continue
            # 8) ratchet the stop for the NEXT bar (never loosen)
            if spec.trail is not None and pos.stop is not None:
                cand = pos.stop
                if peak_gain >= spec.trail.lock_in:
                    lock = pos.entry_px + spec.trail.lock_in * d
                    cand = max(cand, lock) if d == 1 else min(cand, lock)
                if peak_gain >= spec.trail.trail_trigger:
                    trail = pos.peak - spec.trail.trail_offset * d
                    cand = max(cand, trail) if d == 1 else min(cand, trail)
                pos.stop = cand
            continue

        # FLAT — enter at THIS bar's open from the PRIOR bar's desired position
        s = target_pos[i - 1]
        if s == 0:
            continue
        s = int(np.sign(s))
        if (i - last_exit_i) < spec.cooldown_bars:
            continue
        if spec.intraday_only and (not rth[i] or last_rth[i]):
            continue  # only open inside RTH, never on the last bar
        entry = slip.adjust_entry(o[i], s)
        if spec.stop_atr_mult and atr is not None:
            a = atr[i]
            stop = None if np.isnan(a) else entry - spec.stop_atr_mult * a * s
        elif spec.stop_pts is not None:
            stop = entry - spec.stop_pts * s
        else:
            stop = None
        target = entry + spec.target_pts * s if spec.target_pts is not None else None
        pos = _Pos(direction=s, entry_px=entry, entry_idx=i, stop=stop, target=target, peak=entry)

    return pd.DataFrame(trades)
