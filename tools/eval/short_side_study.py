"""Phase-7 (research, OFFLINE) — the SHORT-SIDE edge study (Session 24).

Question: a long-only MA100-bounce bot structurally cannot profit in a sustained
downtrend — it either sits out (the regime gate blocks below-30m-EMA200 longs) or
bleeds. Does the SYMMETRIC SHORT — sell SMA100 *rejections* in the DOWN-regime
(close < 30m EMA200) — have a real OUT-OF-SAMPLE edge (expectancy>0 AND PF>1.2),
walk-forward? This is precisely the regime the long bot sits out, and (per the v23
handoff) something SeanBot, also long-only, does NOT trade.

WHAT THIS IS — AND IS NOT
  * The entry here is a NEW, MIRRORED entry. **No production short path exists.**
    This study does NOT drive prod and makes NO claim about prod behavior. It
    mirrors the *documented long gates* (``strategy.evaluate_gates`` /
    ``_regime_ok``) with the inequalities flipped — nothing more.
  * The exit is the REAL, direction-aware exit. We do NOT reimplement it: we call
    ``trail_manager.compute_ratcheted_stop`` / ``should_hard_exit`` with
    ``direction=Direction.SHORT`` (stop ABOVE entry, ratchets DOWN, locks in as
    price falls). These functions already ship SHORT-symmetric in prod.

THE MIRROR (long gate -> short gate, inequalities flipped)
  long  : pullback DOWN to MA100 support inside an uptrend, bullish confirm
  short : rally  UP   to MA100 resistance inside a downtrend, bearish confirm
    ma_order : LONG  ma_slow > ma_fast  (MA100>MA50)  ->  SHORT ma_fast > ma_slow (MA50>MA100)
    touch    : LONG  low  in [ma_slow-15, ma_slow+buf] ->  SHORT high in [ma_slow-buf, ma_slow+15]
               (the asymmetric 15-pt overshoot band stays on the FAR side from the
                approaching price: below for a long's dip, above for a short's rally)
    confirm  : LONG  close >= open - tol (bullish/doji) -> SHORT close <= open + tol (bearish/doji)
    gap      : |ma_slow - ma_fast| >= min_gap            (symmetric; unchanged)
    regime   : LONG  blocked when price <= 30m EMA200    -> SHORT *traded* there (down-regime)
    signal   : LONG  entry=close stop=close-sl tgt=close+tp -> SHORT stop=close+sl tgt=close-tp

FIDELITY ANCHOR (mandatory — proves the mirror is correct)
  Entry+exit are DIRECTION-PARAMETERIZED in ONE code path. Run that path with
  ``direction=LONG`` and it must reproduce ``engine.simulate_segment`` (the real
  strategy + real exit) BYTE-FOR-BYTE. Only once LONG==engine do we trust the SHORT
  numbers. ``--validate`` asserts this on real data; ``test_short_side_study.py``
  asserts it on synthetic data in CI. (The LONG above-regime independent book is the
  ~PF 1.174 / n~1479-class anchor carried from the long backtest.)

PARTITIONS (short tape; built with the entry-regime gate OFF, then each signal is
tagged below/above the SAME 30m EMA200 ``_regime_ok`` uses)
  * DOWN-regime (primary)   : close <= 30m EMA200  (>=202 buckets) — "trade the regime
                              the long bot sits out". THE question.
  * above-regime (reference): close >  30m EMA200 (or warmup fail-open) — counter-trend
                              shorts; expected poor; reported for completeness.
  * down-DIRECTIONAL / CHOP : a CAUSAL split of the down set at the entry bar using the
                              1-min ADX(14) on the frame. directional = adx>=thr (riding a
                              strong down-leg); chop = adx<thr.

EXITS (both required by the brief; mirrored to SHORT)
  * TF current : stop75 / lock50 / trail150 (the live trailing-ratchet exit).
  * SB fast    : stop75 / lock15 / trail40  (SB-style fast lock + tight trail).

Each partition is an independent single-position book (single position, 10-bar
cooldown after a close, signals during a held position skipped, an unclosed position
at a range boundary dropped) — the honest standalone-strategy read.

WALK-FORWARD (no lookahead): rolling 6mo train -> 2mo test, step 2mo.
  * SELECTION fold : on train pick (adx_threshold, exit) maximizing train expectancy on
                     the down-DIRECTIONAL set (modest grid: 3 thresholds x 2 exits), score
                     that rule on the FOLLOWING test fold, pool the OOS trades. Reports
                     selection stability + a full-sample neighbor surface (plateau vs spike).
  * UNCONDITIONAL  : the down-regime set, fixed exit, no selection — the HONEST headline
                     (no in-sample bias).

CAVEATS (carried into every report, never hidden)
  * MODELED fills — entry at the signal-bar close; the protective stop fills AT the stop
    price intrabar (§0.5.206; pessimistic same-bar: stop checked before the ratchet).
    Measures LOGIC expectancy, not live order plumbing.
  * NEW MIRRORED ENTRY — there is no prod short path; this proves nothing about prod
    execution, only about the entry+exit LOGIC's symmetry.
  * Independent per-partition books (down-set not de-conflicted vs the above-set).
  * NQ 1-min bars (point-identical to MNQ; $2 multiplier for $ P&L). §0.5.97 friction.
  * The forward edge in the real regime is the one thing no backtest can prove —
    confirm forward (a separate AUDIT + paper window) before any prod short path.

  python -m tools.eval.short_side_study [--validate] [--limit N] [--rebuild-tape]
                                        [--adx-threshold 20]
"""

from __future__ import annotations

import argparse
import pickle
import time as _time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config.risk_params import RiskParams
from src.execution.trail_manager import compute_ratcheted_stop, should_hard_exit
from src.state_machine import Direction
from src.strategy import (
    GateEval,
    Signal,
    _in_session_edge_window,
    _parse_hhmm,
    _regime_ok,
)
from tools.eval import data, engine
from tools.eval.backtest import friday_force_flat
from tools.eval.below_trend_study import (
    REGIME_BUFFER,
    REGIME_MIN_BUCKETS,
    _month_bounds,
    regime_at,
)
from tools.eval.engine import Trade, _OpenPosition
from tools.eval.metrics import Stats, compute_stats

TAPE_CACHE = "/tmp/tf_short_side_tape.pkl"

_TOUCH_OVERSHOOT_PTS = 15.0  # strategy._TOUCH_LOWER_BAND_PTS — the asymmetric far-side band

# Exit configs (stop_loss, lock_in, trail_offset) — mirrored to SHORT in the exit fns.
TF_CURRENT = (75.0, 50.0, 150.0)  # live trailing-ratchet exit
SB_FAST = (75.0, 15.0, 40.0)  # SB-style fast lock + tight trail

# Walk-forward ADX-directional grid (kept MODEST — multiple-comparison guard).
ADX_GRID = (15.0, 20.0, 25.0)
DEFAULT_ADX_THRESHOLD = 20.0  # Wilder's classic "trend vs range" line


def cfg_params(stop_loss: float, lock_in: float, trail: float) -> RiskParams:
    """A trailing-exit RiskParams at the given knobs. The entry-tape is built with the
    regime gate OFF (tagged separately), so the regime flag here is irrelevant."""
    return RiskParams(
        regime_gate_enabled=False,
        exit_mode="trailing",
        stop_loss_pts=stop_loss,
        lock_in_pts=lock_in,
        trail_offset_pts=trail,
    )


def entry_params_regime_off() -> RiskParams:
    """Entry config with the regime gate OFF so ALL mirrored short signals fire."""
    return cfg_params(*TF_CURRENT)


# --------------------------------------------------------------------------- #
# Direction-parameterized entry gate — the MIRROR (LONG branch == strategy.evaluate_gates)
# --------------------------------------------------------------------------- #
def evaluate_gates_dir(
    df: pd.DataFrame,
    instrument: str,
    direction: Direction,
    *,
    params: RiskParams,
    buffer_pts: float | None = None,
    min_gap_pts: float | None = None,
) -> GateEval:
    """Direction-symmetric mirror of ``strategy.evaluate_gates``.

    For ``direction=LONG`` every computed boolean and the emitted Signal are
    IDENTICAL to ``strategy.evaluate_gates`` — that identity is what the fidelity
    anchor (``validate``) proves byte-for-byte against ``engine.simulate_segment``.
    For ``direction=SHORT`` the inequalities are flipped per the module docstring;
    nothing else changes.

    Regime is decoupled (the tape is built regime-OFF and tagged afterwards), so the
    real ``_regime_ok`` is still called for fidelity but resolves True when the flag is
    off — mirroring ``evaluate_gates`` exactly.
    """
    if len(df) < 2:
        return GateEval(signal=None, regime_ok=True, indicators_ready=False)

    rp = params
    if not _regime_ok(df, rp):
        return GateEval(signal=None, regime_ok=False, indicators_ready=False)

    buf = buffer_pts if buffer_pts is not None else rp.ma_touch_buffer_pts
    gap_min = min_gap_pts if min_gap_pts is not None else rp.ma_min_gap_pts
    sl_pts = rp.stop_loss_pts
    tp_pts = rp.take_profit_pts

    bar = df.iloc[-1]
    if pd.isna(bar["ma_fast"]) or pd.isna(bar["ma_slow"]):
        return GateEval(signal=None, regime_ok=True, indicators_ready=False)

    ma_fast = float(bar["ma_fast"])
    ma_slow = float(bar["ma_slow"])
    ma_gap = abs(ma_slow - ma_fast)
    o = float(bar["open"])
    l_ = float(bar["low"])
    h_ = float(bar["high"])
    c = float(bar["close"])
    bull_tol = rp.ma_bullish_tolerance_pts
    gap_ok = ma_gap >= gap_min

    if direction is Direction.LONG:
        # ---- exact replica of strategy.evaluate_gates (the fidelity contract) ----
        ma_order_ok = ma_slow > ma_fast
        touch_lower = ma_slow - _TOUCH_OVERSHOOT_PTS
        touch_upper = ma_slow + buf
        touch_ok = (l_ >= touch_lower) and (l_ <= touch_upper)
        confirm_ok = c >= o - bull_tol
        dir_str = "LONG"
        stop_price = c - sl_pts
        target_price = c + tp_pts
    else:
        # ---- the MIRROR: rally into MA100 resistance, bearish confirm ----
        ma_order_ok = ma_fast > ma_slow
        touch_lower = ma_slow - buf
        touch_upper = ma_slow + _TOUCH_OVERSHOOT_PTS
        touch_ok = (h_ >= touch_lower) and (h_ <= touch_upper)
        confirm_ok = c <= o + bull_tol
        dir_str = "SHORT"
        stop_price = c + sl_pts
        target_price = c - tp_pts

    if not (ma_order_ok and touch_ok and confirm_ok and gap_ok):
        return GateEval(
            signal=None,
            regime_ok=True,
            indicators_ready=True,
            ma_order_ok=ma_order_ok,
            touch_ok=touch_ok,
            bullish_ok=confirm_ok,
            gap_ok=gap_ok,
            ma_fast=ma_fast,
            ma_slow=ma_slow,
            ma_gap=ma_gap,
        )

    signal = Signal(
        instrument=instrument,
        direction=dir_str,
        entry_price=c,
        stop_price=stop_price,
        target_price=target_price,
        ma_fast_value=ma_fast,
        ma_slow_value=ma_slow,
        ma_gap=ma_gap,
        adx_value=0.0,
        timestamp=None,
    )
    return GateEval(
        signal=signal,
        regime_ok=True,
        indicators_ready=True,
        ma_order_ok=True,
        touch_ok=True,
        bullish_ok=True,
        gap_ok=True,
        ma_fast=ma_fast,
        ma_slow=ma_slow,
        ma_gap=ma_gap,
    )


class DirGateEntry:
    """Direction-parameterized clone of ``engine.FastGateEntry``.

    Control flow (cooldown -> session-edge -> buffer-warmup -> gate eval) is byte-for-byte
    ``FastGateEntry``; the ONLY change is it calls :func:`evaluate_gates_dir` with an
    explicit ``direction``. With ``direction=LONG`` it is behaviourally identical to
    ``FastGateEntry`` (the fidelity-anchor guarantee)."""

    def __init__(
        self,
        params: RiskParams,
        instrument: str,
        direction: Direction,
        buffer_size: int = 7000,
    ) -> None:
        self._p = params
        self._instrument = instrument
        self._direction = direction
        self._buf = buffer_size
        self._cooldown = 0
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
        if self._cooldown > 0:
            self._cooldown -= 1
            return "noop_cooldown", None
        if _in_session_edge_window(
            ts,
            self._p.session_edge_no_trade_minutes,
            daily_break=self._daily_break,
            gateway_restart=self._gateway_restart,
            weekend_cutoff=self._weekend_cutoff,
            sunday_open=self._sunday_open,
        ):
            return "noop_session_edge", None
        lo = max(0, j + 1 - self._buf)
        if (j - lo + 1) < 2:
            return "noop_warmup", None
        tail = seg.iloc[lo : j + 1]
        ge = evaluate_gates_dir(tail, self._instrument, self._direction, params=self._p)
        if ge.signal is not None:
            return "entry_signal", ge.signal
        if not ge.regime_ok:
            return "noop_regime", None
        if not ge.indicators_ready:
            return "noop_warmup", None
        return "noop_filter", None


# --------------------------------------------------------------------------- #
# Direction-parameterized exit + P&L (mirror of engine._process_exit_bar / _pnl)
# --------------------------------------------------------------------------- #
def _pnl_dir(
    entry: float, exit_price: float, direction: Direction, friction_pts: float, qty: int
) -> tuple[float, float, float, float]:
    """(gross_pts, gross_usd, net_friction, net_commission) for either direction."""
    gross_pts = (exit_price - entry) if direction is Direction.LONG else (entry - exit_price)
    gross_usd = gross_pts * engine.MNQ.multiplier * qty
    net_friction = gross_usd - friction_pts * engine.MNQ.multiplier * qty
    net_commission = gross_usd - qty * engine.MNQ.commission_rt_usd
    return gross_pts, gross_usd, net_friction, net_commission


def _process_exit_bar_dir(
    pos: _OpenPosition,
    params: RiskParams,
    direction: Direction,
    *,
    ts,
    high: float,
    low: float,
    close: float,
    force_flat,
) -> tuple[float, str] | None:
    """One bar of the REAL direction-aware exit. Returns (exit_price, reason) or None.

    LONG branch is byte-for-byte ``engine._process_exit_bar``. SHORT is the mirror:
    the resting stop is ABOVE entry and triggers on the bar HIGH; the extreme tracked
    in ``pos.highest`` is the LOWEST price seen; the ratchet walks the stop DOWN.
    Uses the REAL ``compute_ratcheted_stop`` / ``should_hard_exit`` (direction-aware).
    """
    sl = params.stop_loss_pts
    if direction is Direction.LONG:
        if low <= pos.current_stop:
            return pos.current_stop, (
                "ratchet_stop" if pos.current_stop > pos.entry_price - sl else "stop"
            )
        pos.highest = max(pos.highest, high)
        if should_hard_exit(
            entry=pos.entry_price,
            bar_close=close,
            direction=Direction.LONG,
            hard_ceiling_pts=params.hard_ceiling_pts,
        ):
            return close, "hard_ceiling"
    else:
        # SHORT: protective stop rests ABOVE entry; intrabar trigger on the high.
        if high >= pos.current_stop:
            return pos.current_stop, (
                "ratchet_stop" if pos.current_stop < pos.entry_price + sl else "stop"
            )
        # extreme-favorable excursion for a SHORT is the LOWEST price (stored in .highest).
        pos.highest = min(pos.highest, low)
        if should_hard_exit(
            entry=pos.entry_price,
            bar_close=close,
            direction=Direction.SHORT,
            hard_ceiling_pts=params.hard_ceiling_pts,
        ):
            return close, "hard_ceiling"

    new_stop = compute_ratcheted_stop(
        pos.entry_price,
        pos.highest,
        pos.current_stop,
        direction=direction,
        stop_loss_pts=params.stop_loss_pts,
        lock_in_pts=params.lock_in_pts,
        trail_offset_pts=params.trail_offset_pts,
    )
    if new_stop is not None:
        pos.current_stop = new_stop

    if force_flat is not None and force_flat(ts):
        return close, "force_close"
    return None


# --------------------------------------------------------------------------- #
# The tagged short tape
# --------------------------------------------------------------------------- #
@dataclass
class SSTape:
    ts: list  # list[datetime], per bar
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    sig_idx: np.ndarray  # bar indices where the mirrored gate (regime OFF) fired
    sig_px: np.ndarray  # entry price (= bar close) at each sig_idx
    sig_below: np.ndarray  # bool: entry was at/below the 30m EMA200 (the down-regime)
    sig_adx: np.ndarray  # 1-min ADX(14) at the entry bar (causal directional proxy)
    sig_slope: np.ndarray  # 30m EMA200 slope/bucket at entry (causal, secondary proxy)
    sig_buckets: np.ndarray  # valid 30m buckets seen at entry (diagnostics)
    direction: str  # "LONG" or "SHORT" (which mirror produced this tape)
    span: tuple
    bars: int


def build_ss_tape(seg: pd.DataFrame, direction: Direction, instrument: str = "NQ") -> SSTape:
    """Run the mirrored ``DirGateEntry`` with the regime gate OFF over every bar, record
    every signal, then tag each at fill time below/above the SAME 30m EMA200 ``_regime_ok``
    uses and capture its causal ADX + EMA slope."""
    params = entry_params_regime_off()
    drv = DirGateEntry(params, instrument, direction)  # never on_trade_closed -> cooldown 0
    n = len(seg)
    sig_idx: list[int] = []
    sig_px: list[float] = []
    for j in range(n):
        decision, signal = drv.step(seg, j)
        if decision == "entry_signal" and signal is not None:
            sig_idx.append(j)
            sig_px.append(float(signal.entry_price))

    ts = [t.to_pydatetime() if hasattr(t, "to_pydatetime") else t for t in seg["time"].tolist()]
    close_arr = seg["close"].to_numpy(dtype=float)
    adx_arr = seg["adx"].to_numpy(dtype=float) if "adx" in seg.columns else np.full(n, np.nan)
    time_index = pd.DatetimeIndex(seg["time"])

    below_l: list[bool] = []
    adx_l: list[float] = []
    slope_l: list[float] = []
    buckets_l: list[int] = []
    for e in sig_idx:
        lo = max(0, e + 1 - REGIME_BUFFER)
        win = pd.Series(close_arr[lo : e + 1], index=time_index[lo : e + 1])
        is_below, buckets, _level, slope = regime_at(win)
        below_l.append(bool(is_below))
        buckets_l.append(buckets)
        slope_l.append(slope if slope is not None else float("nan"))
        adx_l.append(float(adx_arr[e]) if not np.isnan(adx_arr[e]) else float("nan"))

    return SSTape(
        ts=ts,
        high=seg["high"].to_numpy(dtype=float),
        low=seg["low"].to_numpy(dtype=float),
        close=close_arr,
        sig_idx=np.asarray(sig_idx, dtype=np.int64),
        sig_px=np.asarray(sig_px, dtype=float),
        sig_below=np.asarray(below_l, dtype=bool),
        sig_adx=np.asarray(adx_l, dtype=float),
        sig_slope=np.asarray(slope_l, dtype=float),
        sig_buckets=np.asarray(buckets_l, dtype=np.int64),
        direction=str(direction.value),
        span=(ts[0], ts[-1]),
        bars=n,
    )


def save_tape(tape: SSTape, path: str) -> None:
    with open(path, "wb") as fh:
        pickle.dump(vars(tape), fh)


def load_tape(path: str) -> SSTape:
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    return SSTape(**d) if isinstance(d, dict) else d


# --------------------------------------------------------------------------- #
# Masked replay — an independent single-position book over an ELIGIBLE signal subset
# --------------------------------------------------------------------------- #
def replay_masked(
    tape: SSTape,
    params: RiskParams,
    eligible: np.ndarray,
    *,
    lo: int = 0,
    hi: int | None = None,
    qty: int = 2,
    friction_pts: float = engine.DEFAULT_FRICTION_PTS,
    force_flat=friday_force_flat,
) -> list[Trade]:
    """Replay ONLY the eligible signals as a standalone single-position book, faithful
    to ``engine.simulate_segment`` semantics (single position, cooldown after a close,
    signals during a held position / cooldown skipped, an unclosed position at the range
    boundary dropped). Direction comes from ``tape.direction``."""
    direction = Direction(tape.direction)
    hi = tape.bars if hi is None else hi
    sl = params.stop_loss_pts
    cooldown = params.cooldown_bars
    trades: list[Trade] = []
    idx_arr = tape.sig_idx
    px_arr = tape.sig_px
    n_sig = len(idx_arr)
    i = int(np.searchsorted(idx_arr, lo, side="left"))
    next_allowed = lo
    while i < n_sig:
        e = int(idx_arr[i])
        if e >= hi:
            break
        if e < next_allowed or not bool(eligible[i]):
            i += 1
            continue
        epx = float(px_arr[i])
        base_stop = epx - sl if direction is Direction.LONG else epx + sl
        pos = _OpenPosition(
            entry_ts=tape.ts[e], entry_price=epx, highest=epx, current_stop=base_stop, bars_held=0
        )
        closed = None
        j = e + 1
        while j < hi:
            c = _process_exit_bar_dir(
                pos,
                params,
                direction,
                ts=tape.ts[j],
                high=float(tape.high[j]),
                low=float(tape.low[j]),
                close=float(tape.close[j]),
                force_flat=force_flat,
            )
            pos.bars_held += 1
            if c is not None:
                closed = (c[0], c[1], j)
                break
            j += 1
        if closed is None:
            break  # open position at range end -> dropped (engine convention)
        exit_price, reason, jclose = closed
        gp, gu, nf, nc = _pnl_dir(pos.entry_price, exit_price, direction, friction_pts, qty)
        trades.append(
            Trade(
                entry_ts=pos.entry_ts,
                entry_price=pos.entry_price,
                exit_ts=tape.ts[jclose],
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
        next_allowed = jclose + 1 + cooldown
        while i < n_sig and int(idx_arr[i]) < next_allowed:
            i += 1
    return trades


# --------------------------------------------------------------------------- #
# Eligibility masks (down-regime = primary; above-regime = reference)
# --------------------------------------------------------------------------- #
def mask_down(tape: SSTape) -> np.ndarray:
    """DOWN-regime shorts (close <= 30m EMA200) — the regime the long bot sits out."""
    return tape.sig_below.copy()


def mask_above(tape: SSTape) -> np.ndarray:
    """above-regime (counter-trend) shorts — reference set."""
    return ~tape.sig_below


def mask_down_directional(tape: SSTape, threshold: float) -> np.ndarray:
    """down-regime AND directional (adx >= threshold) — riding a strong down-leg.
    NaN adx -> excluded."""
    adx = tape.sig_adx
    return tape.sig_below & (adx >= threshold) & ~np.isnan(adx)


def mask_down_chop(tape: SSTape, threshold: float) -> np.ndarray:
    """down-regime AND ranging (adx < threshold) — for completeness."""
    adx = tape.sig_adx
    return tape.sig_below & (adx < threshold) & ~np.isnan(adx)


# --------------------------------------------------------------------------- #
# Fidelity anchor — LONG mirror MUST equal the real engine, byte-for-byte
# --------------------------------------------------------------------------- #
def validate(seg: pd.DataFrame) -> tuple[bool, str]:
    """Build the LONG tape via the direction-parameterized code, replay every signal with
    the TF-current exit, and assert it reproduces ``engine.simulate_segment`` (regime OFF)
    byte-for-byte. This is the trust anchor: LONG==engine => the SHORT mirror is correct.
    Also checks the SB-fast exit for robustness."""
    long_tape = build_ss_tape(seg, Direction.LONG, "NQ")
    all_elig = np.ones(len(long_tape.sig_idx), dtype=bool)
    msgs: list[str] = []
    ok_all = True
    for cfg, label in ((TF_CURRENT, "TF"), (SB_FAST, "SB")):
        bp = cfg_params(*cfg)
        eng = engine.simulate_segment(
            seg, engine.FastGateEntry(bp, "NQ"), bp, force_flat=friday_force_flat
        ).trades
        rep = replay_masked(long_tape, bp, all_elig)
        mism = 0
        for a, b in zip(eng, rep, strict=False):
            if (
                a.entry_ts != b.entry_ts
                or abs(a.exit_price - b.exit_price) > 1e-6
                or a.exit_reason != b.exit_reason
            ):
                mism += 1
        en = sum(t.net_usd for t in eng)
        rn = sum(t.net_usd for t in rep)
        ok = len(eng) == len(rep) and mism == 0 and abs(en - rn) < 1e-3
        ok_all = ok_all and ok
        msgs.append(
            f"[{label}] n_engine={len(eng)} n_replay={len(rep)} "
            f"engine_net={en:.2f} replay_net={rn:.2f} mismatch={mism} -> {'OK' if ok else 'FAIL'}"
        )
    return ok_all, "  ".join(msgs)


# --------------------------------------------------------------------------- #
# Walk-forward
# --------------------------------------------------------------------------- #
def _exit_for(label: str) -> RiskParams:
    return cfg_params(*(TF_CURRENT if label == "TF" else SB_FAST))


@dataclass
class FoldResult:
    train_label: str
    test_label: str
    sel_threshold: float
    sel_exit: str
    train: Stats
    test: Stats
    n_test_trades: int


def walk_forward_select(
    tape: SSTape,
    *,
    train_months: int = 6,
    test_months: int = 2,
    step_months: int = 2,
    min_train_n: int = 12,
) -> tuple[list[FoldResult], list[Trade]]:
    """Rolling train->test. On train, pick (adx_threshold, exit) with best train expectancy
    on the down-DIRECTIONAL set (guarded by min_train_n); evaluate that rule on the FOLLOWING
    test fold's down-directional set; pool the OOS test trades."""
    months = _month_bounds(tape)
    n_months = len(months)
    folds: list[FoldResult] = []
    oos: list[Trade] = []
    m = 0
    while m + train_months + test_months <= n_months:
        tr0, tr1 = months[m][1], months[m + train_months - 1][2]
        te0, te1 = months[m + train_months][1], months[m + train_months + test_months - 1][2]
        train_label = f"{months[m][0]}..{months[m + train_months - 1][0]}"
        test_label = (
            f"{months[m + train_months][0]}..{months[m + train_months + test_months - 1][0]}"
        )
        best = None  # (key, threshold, exit_label, train_stats)
        for thr in ADX_GRID:
            elig = mask_down_directional(tape, thr)
            for ex in ("TF", "SB"):
                p = _exit_for(ex)
                tt = replay_masked(tape, p, elig, lo=tr0, hi=tr1)
                s = compute_stats(tt)
                if s.n < min_train_n:
                    continue
                key = (
                    s.expectancy_usd,
                    s.profit_factor if s.profit_factor != float("inf") else 1e9,
                )
                if best is None or key > best[0]:
                    best = (key, thr, ex, s)
        if best is None:
            m += step_months
            continue
        _key, thr, ex, train_stats = best
        elig = mask_down_directional(tape, thr)
        test_trades = replay_masked(tape, _exit_for(ex), elig, lo=te0, hi=te1)
        test_stats = compute_stats(test_trades)
        oos.extend(test_trades)
        folds.append(
            FoldResult(train_label, test_label, thr, ex, train_stats, test_stats, len(test_trades))
        )
        m += step_months
    return folds, oos


def walk_forward_unconditional(
    tape: SSTape,
    exit_label: str,
    *,
    train_months: int = 6,
    test_months: int = 2,
    step_months: int = 2,
) -> list[Trade]:
    """Pool the down-regime (UNCONDITIONAL, no adx filter) test-fold trades under a FIXED
    exit — the honest "short the regime the long bot sits out" OOS read (no selection)."""
    months = _month_bounds(tape)
    n_months = len(months)
    elig = mask_down(tape)
    p = _exit_for(exit_label)
    oos: list[Trade] = []
    m = 0
    while m + train_months + test_months <= n_months:
        te0 = months[m + train_months][1]
        te1 = months[m + train_months + test_months - 1][2]
        oos.extend(replay_masked(tape, p, elig, lo=te0, hi=te1))
        m += step_months
    return oos


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _fmt(s: Stats) -> str:
    pf = f"{s.profit_factor:.3f}" if s.profit_factor != float("inf") else "inf"
    return (
        f"n={s.n:<5} win={s.win_rate * 100:5.1f}% exp=${s.expectancy_usd:7.2f} "
        f"PF={pf:>6} net=${s.net_usd:10.2f} maxDD=${s.max_drawdown_usd:9.2f}"
    )


def _stats_for(tape: SSTape, mask: np.ndarray, exit_label: str) -> Stats:
    return compute_stats(replay_masked(tape, _exit_for(exit_label), mask))


def build_report(
    short_tape: SSTape, long_tape: SSTape, validate_msg: str, *, adx_threshold: float
) -> str:
    lines: list[str] = []
    add = lines.append
    below = short_tape.sig_below
    n_sig = len(short_tape.sig_idx)
    n_down = int(below.sum())
    n_above = n_sig - n_down
    n_warm = int((short_tape.sig_buckets < REGIME_MIN_BUCKETS).sum())

    add("=" * 96)
    add("PHASE 7 (research) — SHORT-SIDE EDGE STUDY (mirrored entry + REAL direction-aware exit)")
    add("=" * 96)
    add("  NEW MIRRORED ENTRY — no prod short path exists; this does NOT drive prod. It mirrors")
    add("  the documented long gates with the inequalities flipped. Exit = the REAL")
    add("  trail_manager with direction=SHORT. Modeled fills. Forward confirmation still required.")
    add("")
    add(f"data span : {short_tape.span[0]}  ->  {short_tape.span[1]}")
    add(f"bars      : {short_tape.bars:,}   mirrored short signals (regime OFF) : {n_sig:,}")
    add(
        f"partition : DOWN-regime/primary={n_down:,}  above-regime/reference={n_above:,}  "
        f"(of which {n_warm:,} above were 30m-warmup fail-opens)"
    )
    add(f"directional: 1-min ADX(14) at entry (causal); directional=adx>={adx_threshold:.0f}")
    add(f"exits     : TF={TF_CURRENT}  SB-fast={SB_FAST}")
    add("")

    # ---- FIDELITY ANCHOR ----
    add("-- FIDELITY ANCHOR: direction-parameterized mirror, direction=LONG == real engine ----")
    add(f"  {validate_msg}")
    long_above_tf = compute_stats(replay_masked(long_tape, _exit_for("TF"), ~long_tape.sig_below))
    add(f"  LONG above-regime independent book (TF exit) : {_fmt(long_above_tf)}")
    add("  (the long anchor: ~PF 1.174 / n~1479-class; the regime-ON whole-book number is the")
    add("   formal anchor printed by `backtest --regime on`. LONG==engine byte-for-byte above")
    add("   is the proof the SHORT numbers below are a faithful mirror.)")
    add("")

    # ---- DOWN-regime UNCONDITIONAL (full sample = honest, no params selected) ----
    add("-- DOWN-REGIME SHORTS, UNCONDITIONAL (full sample; no adx filter; 'short the regime")
    add("   the long bot sits out') --")
    for label, cfg in (("TF current", "TF"), ("SB fast  ", "SB")):
        add(f"  {label} : {_fmt(_stats_for(short_tape, mask_down(short_tape), cfg))}")
    add("")

    # ---- above-regime (counter-trend) reference ----
    add("-- ABOVE-REGIME (counter-trend) SHORTS — reference, expected poor --")
    for label, cfg in (("TF current", "TF"), ("SB fast  ", "SB")):
        add(f"  {label} : {_fmt(_stats_for(short_tape, mask_above(short_tape), cfg))}")
    add("")

    # ---- DIRECTIONAL vs CHOP split (descriptive, full sample) ----
    add(f"-- DOWN-REGIME DIRECTIONAL vs CHOP (adx>={adx_threshold:.0f}=directional) — full --")
    dir_m = mask_down_directional(short_tape, adx_threshold)
    chop_m = mask_down_chop(short_tape, adx_threshold)
    add(f"  n: down-directional={int(dir_m.sum())}  down-chop={int(chop_m.sum())}")
    for ex_label, ex in (("TF current", "TF"), ("SB fast", "SB")):
        add(f"  {ex_label:<11} {'down-DIRECTIONAL':<18} {_fmt(_stats_for(short_tape, dir_m, ex))}")
        add(f"  {ex_label:<11} {'down-CHOP':<18} {_fmt(_stats_for(short_tape, chop_m, ex))}")
    add("")

    # ---- Robustness surface: down-directional full sample across thresholds x exits ----
    add("-- ROBUSTNESS: down-DIRECTIONAL full-sample exp/PF across thresholds (plateau=real) --")
    add(f"  {'thr':>4}  {'exit':>4}  {'n':>5} {'win%':>6} {'exp$':>8} {'PF':>7} {'net$':>11}")
    for thr in ADX_GRID:
        dm = mask_down_directional(short_tape, thr)
        for ex in ("TF", "SB"):
            s = _stats_for(short_tape, dm, ex)
            pf = f"{s.profit_factor:.3f}" if s.profit_factor != float("inf") else "inf"
            add(
                f"  {thr:>4.0f}  {ex:>4}  {s.n:>5} {s.win_rate * 100:>5.1f}% "
                f"{s.expectancy_usd:>8.2f} {pf:>7} {s.net_usd:>11.2f}"
            )
    add("")

    # ---- WALK-FORWARD (down-directional, joint threshold x exit selection) ----
    add(
        "-- WALK-FORWARD down-DIRECTIONAL (train=6mo -> test=2mo, step=2mo; pick best train exp) --"
    )
    folds, oos = walk_forward_select(short_tape)
    add(
        f"  {'train':>16} {'test':>16}  {'sel':>9} {'tr.exp$':>8} {'tr.PF':>6}  "
        f"||  {'te.n':>5} {'te.win%':>7} {'te.exp$':>8} {'te.PF':>6}"
    )
    for f in folds:
        trpf = f"{f.train.profit_factor:.2f}" if f.train.profit_factor != float("inf") else "inf"
        tepf = f"{f.test.profit_factor:.2f}" if f.test.profit_factor != float("inf") else "inf"
        sel = f"{f.sel_threshold:.0f}/{f.sel_exit}"
        add(
            f"  {f.train_label:>16} {f.test_label:>16}  {sel:>9} "
            f"{f.train.expectancy_usd:>8.2f} {trpf:>6}  ||  {f.test.n:>5} "
            f"{f.test.win_rate * 100:>6.1f}% {f.test.expectancy_usd:>8.2f} {tepf:>6}"
        )
    oos_stats = compute_stats(oos)
    add("")
    add("  AGGREGATE OUT-OF-SAMPLE (down-directional, all test folds pooled):")
    add(f"    {_fmt(oos_stats)}")
    sel_counts: dict = {}
    for f in folds:
        k = f"{f.sel_threshold:.0f}/{f.sel_exit}"
        sel_counts[k] = sel_counts.get(k, 0) + 1
    add(f"    selected-rule frequency: {dict(sorted(sel_counts.items(), key=lambda x: -x[1]))}")
    add("")

    # ---- UNCONDITIONAL down-regime walk-forward OOS (no selection) — THE HEADLINE ----
    add("-- WALK-FORWARD down-regime UNCONDITIONAL OOS (fixed exit, no adx filter) — HEADLINE --")
    uncond_tf = compute_stats(walk_forward_unconditional(short_tape, "TF"))
    uncond_sb = compute_stats(walk_forward_unconditional(short_tape, "SB"))
    add(f"  TF current  pooled OOS : {_fmt(uncond_tf)}")
    add(f"  SB fast     pooled OOS : {_fmt(uncond_sb)}")
    add("")

    # ---- VERDICT ----
    add("-- VERDICT ----------")
    sel_clears = oos_stats.expectancy_usd > 0 and oos_stats.profit_factor > 1.2
    uncond_clears = (uncond_tf.expectancy_usd > 0 and uncond_tf.profit_factor > 1.2) or (
        uncond_sb.expectancy_usd > 0 and uncond_sb.profit_factor > 1.2
    )
    add(
        f"  down-DIRECTIONAL walk-forward OOS clears exp>0 AND PF>1.2 : "
        f"{'YES' if sel_clears else 'NO'} "
        f"(OOS exp=${oos_stats.expectancy_usd:.2f} PF={oos_stats.profit_factor:.3f} "
        f"n={oos_stats.n})"
    )
    add(
        f"  down-regime UNCONDITIONAL OOS clears the bar              : "
        f"{'YES' if uncond_clears else 'NO'} "
        f"(TF exp=${uncond_tf.expectancy_usd:.2f}/PF {uncond_tf.profit_factor:.3f}; "
        f"SB exp=${uncond_sb.expectancy_usd:.2f}/PF {uncond_sb.profit_factor:.3f})"
    )
    add(f"  LONG above-regime anchor (independent book, TF exit)      : {_fmt(long_above_tf)}")
    add("")
    add("-- CAVEATS ----------")
    add("  * NEW MIRRORED ENTRY — no prod short path; proves nothing about prod execution, only")
    add("    the entry+exit LOGIC's symmetry. The exit IS the real direction-aware trail_manager.")
    add("  * Modeled fills (stop fills AT price intrabar; same-bar stop checked before ratchet).")
    add("    LOGIC expectancy, not plumbing.")
    add("  * Independent per-partition books (down-set not de-conflicted vs the above-set).")
    add("  * NQ≈MNQ points, $2 mult; §0.5.97 friction. Grid kept modest (multiple-comparison).")
    add("  * Walk-forward is the honest read; in-sample is upward-biased. The forward edge in the")
    add(
        "    REAL regime is unprovable here — confirm forward (separate AUDIT + paper) before prod."
    )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap bars (smoke)")
    ap.add_argument("--rebuild-tape", action="store_true")
    ap.add_argument(
        "--validate", action="store_true", help="assert LONG mirror == engine (fidelity anchor)"
    )
    ap.add_argument("--adx-threshold", type=float, default=DEFAULT_ADX_THRESHOLD)
    ap.add_argument("--tape-cache", default=TAPE_CACHE)
    args = ap.parse_args()

    df = data.load_history()
    if args.limit:
        df = df.iloc[: args.limit].reset_index(drop=True)
    seg = data.to_segments(df)[0]

    # SHORT tape (cached); LONG tape rebuilt for the anchor (fast — signals only).
    short_tape = None
    if not args.rebuild_tape and not args.limit:
        try:
            short_tape = load_tape(args.tape_cache)
            if short_tape.bars != len(seg) or short_tape.direction != "SHORT":
                short_tape = None
        except (OSError, pickle.PickleError, TypeError, AttributeError):
            short_tape = None
    if short_tape is None:
        t0 = _time.perf_counter()
        short_tape = build_ss_tape(seg, Direction.SHORT, "NQ")
        print(
            f"[tape] built SHORT {short_tape.bars:,} bars, {len(short_tape.sig_idx):,} signals "
            f"({int(short_tape.sig_below.sum()):,} down-regime) in {_time.perf_counter() - t0:.0f}s"
        )
        if not args.limit:
            save_tape(short_tape, args.tape_cache)

    t1 = _time.perf_counter()
    long_tape = build_ss_tape(seg, Direction.LONG, "NQ")
    print(
        f"[tape] built LONG  {long_tape.bars:,} bars, {len(long_tape.sig_idx):,} signals "
        f"in {_time.perf_counter() - t1:.0f}s (fidelity anchor)"
    )

    vmsg = "skipped (pass --validate)"
    if args.validate:
        ok, vmsg = validate(seg)
        vmsg = ("PASS  " if ok else "FAIL  ") + vmsg
        if not ok:
            raise SystemExit(f"[validate] LONG mirror != engine — ABORT study. {vmsg}")

    print(build_report(short_tape, long_tape, vmsg, adx_threshold=args.adx_threshold))


if __name__ == "__main__":
    main()
