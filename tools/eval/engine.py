"""Bar-driven simulation engine — drives the REAL strategy + REAL exit with modeled fills.

This is the shared core of Phase 1 (backtest over saved history) and Phase 2
(synthetic scenarios). It imports and calls the production code directly:

* Entry:  ``src.strategy.evaluate_gates`` / ``_regime_ok`` (the real 30-min EMA200
          regime gate + touch/bullish/ma_order/gap gates), plus the real
          ``_in_session_edge_window`` no-trade windows and the 10-bar cooldown.
* Exit :  ``src.execution.trail_manager.compute_ratcheted_stop`` /
          ``should_hard_exit`` (the SeanBot V3/V12 bar-close ratchet: base
          entry-75, +50 lock-in, 150 trail, 1000 hard-ceiling).

Nothing here re-implements strategy or exit math — the engine only models the
parts the live system gets from the broker (fills, the resting stop's intrabar
trigger) and the parts it gets from the orchestrator loop (per-bar cadence,
single-position concurrency, EOD force-close).

FIDELITY: ``FastGateEntry`` mirrors ``Sma100BounceStrategy.on_new_bar``'s control
flow exactly but reads precomputed indicator columns + a rolling tail (so a
778k-bar backtest runs in ~20 min instead of ~3 h). ``test_eval_engine.py`` asserts
it produces byte-identical decisions to the real ``Sma100BounceStrategy`` object
over a sample window — that test is the proof the optimization is faithful.

MODELING ASSUMPTIONS (stated so the caveats in any report are honest):
  * Entry fills at the signal bar's close (the live MKT order fills ~1 tick later;
    round-trip slippage is folded into ``friction_pts``).
  * The protective stop is stop-MARKET (§0.5.206); when a bar's low <= the stop
    resting *during* that bar (set at the prior bar's close), the position exits
    AT the stop price. This is the pessimistic intrabar model — within one 1-min
    bar we cannot know whether the high or the stop printed first, so the stop
    (resting from the prior close) is checked before this bar's ratchet.
  * The ratchet walks on bar close (orchestrator dispatches it on each closed bar).
  * Single position at a time (live MAX_CONCURRENT=1): no new entry while in a trade.
  * Friction (§0.5.97 ~1.12 index pt / 2-lot round trip) is subtracted from the
    gross point move; the code-exact commission-only figure is reported alongside.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import pandas as pd

from config.instruments import MNQ
from config.risk_params import RiskParams
from src.execution.trail_manager import compute_ratcheted_stop, should_hard_exit
from src.state_machine import Direction
from src.strategy import (
    Sma100BounceStrategy,
    _in_session_edge_window,
    _parse_hhmm,
    evaluate_gates,
)

# §0.5.97 — round-trip friction for the 2-lot in index points (commission ~0.62pt
# + ~0.5pt modeled spread/slippage). The code's pnl_net charges commission only
# ($2.48 = 2 * commission_rt_usd); this is the more conservative fill model the
# Session 24 brief specifies. Both figures are reported.
DEFAULT_FRICTION_PTS = 1.12


# --------------------------------------------------------------------------- #
# Trade + result records
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str  # "stop" | "ratchet_stop" | "hard_ceiling" | "force_close"
    bars_held: int
    highest: float
    final_stop: float
    # P&L (LONG, qty/multiplier from config):
    gross_pts: float
    gross_usd: float
    net_usd: float  # friction-model net (DEFAULT_FRICTION_PTS)
    net_usd_commission_only: float  # code-exact (matches lifecycles.pnl_net)

    @property
    def is_win(self) -> bool:
        return self.net_usd > 0


@dataclass
class SimResult:
    trades: list[Trade] = field(default_factory=list)
    bars_seen: int = 0
    signals_raw: int = 0  # gate-level long_signal count (pre concurrency/cooldown)
    entries: int = 0
    blocked_regime: int = 0
    blocked_filter: int = 0
    blocked_session_edge: int = 0
    blocked_cooldown: int = 0
    blocked_in_position: int = 0


# --------------------------------------------------------------------------- #
# Entry drivers (both faithful to Sma100BounceStrategy.on_new_bar)
# --------------------------------------------------------------------------- #
class EntryDriver(Protocol):
    def step(self, seg: pd.DataFrame, j: int) -> tuple[str, object | None]:
        """Return (decision_label, Signal|None) for bar j of contiguous segment ``seg``."""
        ...

    def on_trade_closed(self) -> None: ...


def _decision_from_gate_eval(ge, in_warmup_buffer: bool) -> tuple[str, object | None]:
    """Translate a GateEval into on_new_bar's decision label (regime/warmup/filter/signal)."""
    if ge.signal is not None:
        return "long_signal", ge.signal
    if not ge.regime_ok:
        return "noop_regime", None
    if not ge.indicators_ready:
        return "noop_warmup", None
    return "noop_filter", None


class FastGateEntry:
    """Mirrors Sma100BounceStrategy.on_new_bar on precomputed-indicator segments.

    ``seg`` is a contiguous bar segment whose indicator columns (ma_fast/ma_slow
    from add_all_indicators) are already computed; ``time`` is a UTC tz-aware
    column. Only the regime resample tail and a few scalars are computed per bar,
    so this is ~1.4 ms/bar vs ~13 ms/bar for rebuilding the frame each step.
    """

    def __init__(self, params: RiskParams, instrument: str, buffer_size: int = 7000) -> None:
        self._p = params
        self._instrument = instrument
        self._buf = buffer_size
        self._cooldown = 0
        # Mirror Sma100BounceStrategy.__init__'s parsed session-edge times.
        self._daily_break = (
            _parse_hhmm(params.daily_break_start_et),
            _parse_hhmm(params.daily_break_end_et),
        )
        from datetime import time as _t

        self._gateway_restart = (
            _parse_hhmm(params.gateway_restart_start_et),
            _parse_hhmm(params.gateway_restart_end_et),
        )
        self._weekend_cutoff = (
            params.weekend_flat_cutoff_weekday,
            _t(params.weekend_flat_cutoff_hour_et, params.weekend_flat_cutoff_minute_et),
        )
        self._sunday_open = _parse_hhmm(params.sunday_open_et)

    def on_trade_closed(self) -> None:
        self._cooldown = self._p.cooldown_bars

    def step(self, seg: pd.DataFrame, j: int) -> tuple[str, object | None]:
        ts = seg["time"].iloc[j]
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        # 1) cooldown (decrement, suppress) — on_new_bar checks this first.
        if self._cooldown > 0:
            self._cooldown -= 1
            return "noop_cooldown", None
        # 2) session-edge no-trade window.
        if _in_session_edge_window(
            ts,
            self._p.session_edge_no_trade_minutes,
            daily_break=self._daily_break,
            gateway_restart=self._gateway_restart,
            weekend_cutoff=self._weekend_cutoff,
            sunday_open=self._sunday_open,
        ):
            return "noop_session_edge", None
        # 3) buffer warmup (deque length, capped at buffer_size).
        lo = max(0, j + 1 - self._buf)
        if (j - lo + 1) < 2:
            return "noop_warmup", None
        tail = seg.iloc[lo : j + 1]
        ge = evaluate_gates(tail, self._instrument, params=self._p)
        return _decision_from_gate_eval(ge, in_warmup_buffer=False)


class RealStrategyEntry:
    """Wraps the real Sma100BounceStrategy object (max fidelity, slower).

    Feeds each bar's raw OHLCV dict through ``on_new_bar`` — exactly the live path.
    Used by the synthetic scenarios (short sequences) and the fidelity test.
    """

    def __init__(self, params: RiskParams, instrument: str, buffer_size: int = 7000) -> None:
        self._strat = Sma100BounceStrategy(instrument, params=params, buffer_size=buffer_size)

    def on_trade_closed(self) -> None:
        self._strat.mark_trade_closed()

    def step(self, seg: pd.DataFrame, j: int) -> tuple[str, object | None]:
        row = seg.iloc[j]
        bar = {
            "time": (
                row["time"].to_pydatetime()
                if isinstance(row["time"], pd.Timestamp)
                else row["time"]
            ),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0.0)) if "volume" in seg.columns else 0.0,
        }
        signal = self._strat.on_new_bar(bar)
        decision = (self._strat.last_decision or {}).get("decision", "noop_warmup")
        return decision, signal


# --------------------------------------------------------------------------- #
# The exit model (real trail_manager functions)
# --------------------------------------------------------------------------- #
@dataclass
class _OpenPosition:
    entry_ts: datetime
    entry_price: float
    highest: float
    current_stop: float
    bars_held: int = 0


def _pnl(
    entry: float, exit_price: float, friction_pts: float, qty: int
) -> tuple[float, float, float, float]:
    """(gross_pts, gross_usd, net_usd_friction, net_usd_commission_only) for a LONG."""
    gross_pts = exit_price - entry
    gross_usd = gross_pts * MNQ.multiplier * qty
    net_friction = gross_usd - friction_pts * MNQ.multiplier * qty
    net_commission = gross_usd - qty * MNQ.commission_rt_usd
    return gross_pts, gross_usd, net_friction, net_commission


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def simulate_segment(
    seg: pd.DataFrame,
    driver: EntryDriver,
    params: RiskParams,
    *,
    qty: int = 2,
    friction_pts: float = DEFAULT_FRICTION_PTS,
    force_flat: Callable[[datetime], bool] | None = None,
    result: SimResult | None = None,
) -> SimResult:
    """Drive one CONTIGUOUS bar segment through the real entry + exit code.

    ``seg`` must carry columns: time (UTC tz-aware), open, high, low, close, and
    (for FastGateEntry) the add_all_indicators columns. ``force_flat(ts)`` returns
    True when an open position must be EOD/weekend-flattened at this bar's close.
    Accumulates into ``result`` (created if None) so a multi-segment backtest can
    share one result. Stop ratchet/hard-ceiling use the real trail_manager.
    """
    res = result if result is not None else SimResult()
    pos: _OpenPosition | None = None
    n = len(seg)
    sl = params.stop_loss_pts
    for j in range(n):
        res.bars_seen += 1
        row = seg.iloc[j]
        ts = row["time"].to_pydatetime() if isinstance(row["time"], pd.Timestamp) else row["time"]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        # The entry driver runs EVERY bar (its buffer must stay warm exactly like
        # the live on_new_bar, which appends every bar regardless of position).
        decision, signal = driver.step(seg, j)
        if decision == "long_signal":
            res.signals_raw += 1

        if pos is not None:
            # ---- in a position: run the real exit; ignore any entry signal ----
            if decision == "long_signal":
                res.blocked_in_position += 1
            closed = _process_exit_bar(
                pos, params, ts=ts, high=high, low=low, close=close, force_flat=force_flat
            )
            pos.bars_held += 1
            if closed is not None:
                exit_price, reason = closed
                gp, gu, nf, nc = _pnl(pos.entry_price, exit_price, friction_pts, qty)
                res.trades.append(
                    Trade(
                        entry_ts=pos.entry_ts,
                        entry_price=pos.entry_price,
                        exit_ts=ts,
                        exit_price=exit_price,
                        exit_reason=reason,
                        bars_held=pos.bars_held,
                        highest=pos.highest,
                        final_stop=pos.current_stop,
                        gross_pts=gp,
                        gross_usd=gu,
                        net_usd=nf,
                        net_usd_commission_only=nc,
                    )
                )
                pos = None
                driver.on_trade_closed()
            continue

        # ---- flat: tally the no-entry reason, or open on a signal ----
        if decision == "noop_regime":
            res.blocked_regime += 1
        elif decision == "noop_filter":
            res.blocked_filter += 1
        elif decision == "noop_session_edge":
            res.blocked_session_edge += 1
        elif decision == "noop_cooldown":
            res.blocked_cooldown += 1
        elif decision == "long_signal" and signal is not None:
            entry_price = float(signal.entry_price)  # == bar close
            pos = _OpenPosition(
                entry_ts=ts,
                entry_price=entry_price,
                highest=entry_price,  # router seeds _highest to the fill price
                current_stop=entry_price - sl,  # base protective stop, post-fill
                bars_held=0,
            )
            res.entries += 1
    return res


def _process_exit_bar(
    pos: _OpenPosition,
    params: RiskParams,
    *,
    ts: datetime,
    high: float,
    low: float,
    close: float,
    force_flat: Callable[[datetime], bool] | None,
) -> tuple[float, str] | None:
    """One bar of the real exit. Returns (exit_price, reason) if closed, else None.

    Order (faithful to router._ratchet_one + the resting GTC stop):
      1. The stop resting DURING this bar is ``pos.current_stop`` (set at the prior
         bar's close). If low <= it → exit AT the stop (intrabar, §0.5.206).
      2. Update the high-water mark from this bar's high.
      3. Hard-ceiling on this bar's close → market exit at close.
      4. Ratchet the stop up (compute_ratcheted_stop); store for the next bar.
      5. EOD/weekend force-close at this bar's close, if predicate fires.
    """
    # 1) intrabar protective-stop trigger (prior-bar stop governs during the bar).
    if low <= pos.current_stop:
        return pos.current_stop, (
            "ratchet_stop" if pos.current_stop > pos.entry_price - params.stop_loss_pts else "stop"
        )

    # 2) high-water mark.
    pos.highest = max(pos.highest, high)

    # 3) hard ceiling (V3 +1000) — market exit at close.
    if should_hard_exit(
        entry=pos.entry_price,
        bar_close=close,
        direction=Direction.LONG,
        hard_ceiling_pts=params.hard_ceiling_pts,
    ):
        return close, "hard_ceiling"

    # 4) ratchet the stop up on bar close.
    new_stop = compute_ratcheted_stop(
        pos.entry_price,
        pos.highest,
        pos.current_stop,
        direction=Direction.LONG,
        stop_loss_pts=params.stop_loss_pts,
        lock_in_pts=params.lock_in_pts,
        trail_offset_pts=params.trail_offset_pts,
    )
    if new_stop is not None:
        pos.current_stop = new_stop

    # 5) EOD / weekend force-close (flatten at the close of the trigger bar).
    if force_flat is not None and force_flat(ts):
        return close, "force_close"

    return None
