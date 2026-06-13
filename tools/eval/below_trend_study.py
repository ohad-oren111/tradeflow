"""Phase-7 (research, OFFLINE) — the BELOW-TREND long edge study.

Question (Session 24): do the below-30m-EMA200 long entries the regime gate
currently BLOCKS (the trades SeanBot profits from in chop) have a real
OUT-OF-SAMPLE edge — unconditionally, or only filtered to chop — and does a fast
SB-style exit change the answer?

It drives the **real** strategy gates with the regime gate **OFF** (so the
below-trend entries fire), tags each entry at fill time as ABOVE / BELOW the same
30-min EMA200 that ``strategy._regime_ok`` uses, and runs each partition through
the **real** exit (``engine._process_exit_bar`` -> ``trail_manager.compute_ratcheted_stop``
/ ``should_hard_exit``). Nothing re-implements strategy or exit math; the only
modeled-and-labelled add-on is the fixed +50 take-profit what-if (a research cap on
top of the real ratchet, loudly flagged).

PARTITIONS
  * above-trend (allowed)  : close >  30m EMA200  (or warmup fail-open) — the gate's
                             allowed set; its independent book is the sanity anchor.
  * below-trend (blocked)  : close <= 30m EMA200 with >=202 buckets — "what SB does".
  * below-CHOP / below-TREND: a CAUSAL split of the below-trend set at the entry bar
                             only, using the 1-min ADX(14) already on the frame
                             (add_all_indicators). chop = adx < threshold (ranging,
                             mean-reversion-friendly); trend = adx >= threshold (a
                             strong directional down-leg — catching a falling knife).

EXITS (each partition is run through all three)
  * TF current : stop75 / lock50 / trail150 (the live trailing-ratchet exit).
  * SB fast    : stop75 / lock15 / trail40  (SB-style fast lock + tight trail).
  * +50 TP     : the real ratchet PLUS a fixed +50 take-profit cap (LABELLED what-if;
                 the only non-prod exit math here — an extra modeling caveat applies).

Each partition is simulated as its OWN independent single-position book (single
position, 10-bar cooldown after a close, signals during a held position skipped,
an unclosed position at a range boundary dropped) — i.e. "if you ONLY traded these
entries, like SB unconditionally / like SB only in chop". This is the honest
standalone-strategy read; it is NOT how live TF would interleave the two sets.

WALK-FORWARD (no lookahead): rolling 6mo train -> 2mo test, step 2mo. On each train
window pick the (chop_threshold, exit) maximizing expectancy on the below-CHOP set
(modest grid: 3 thresholds x 2 exits); evaluate that rule on the FOLLOWING test
window's below-chop set; aggregate the pooled OOS trades. The UNCONDITIONAL
below-trend set is reported full-sample (no parameter selected -> no in-sample bias).

CAVEATS (carried into every report, never hidden):
  * MODELED fills — entry at signal-bar close; protective stop fills AT the stop price
    intrabar (§0.5.206). The +50 TP what-if additionally assumes the limit fills the
    instant the bar high touches entry+50 (optimistic on fills, pessimistic on a
    same-bar stop+TP collision: the stop is checked first). Measures LOGIC expectancy,
    not live plumbing.
  * TF-own-entry path only (the SeanBot 2nd entry path is not modeled).
  * NQ 1-min bars (point-identical to MNQ; $2 multiplier for $ P&L). §0.5.97 friction.
  * Independent per-partition books: the below-set's trades are NOT de-conflicted
    against the above-set's (they can overlap in time) — this scores each set's
    standalone edge, not a combined live book.
  * The forward edge in the real regime is the one thing no backtest can prove.

  python -m tools.eval.below_trend_study [--validate] [--limit N] [--rebuild-tape]
                                         [--chop-threshold 20] [--adx-proxy adx|slope]
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
from tools.eval import data, engine
from tools.eval.backtest import friday_force_flat
from tools.eval.engine import Trade, _OpenPosition, _pnl, _process_exit_bar
from tools.eval.metrics import Stats, compute_stats

TAPE_CACHE = "/tmp/tf_below_trend_tape.pkl"

# Regime gate constants — MIRROR strategy._regime_ok / _BAR_BUFFER_MAX exactly.
REGIME_BUFFER = 7000
REGIME_MIN_BUCKETS = 202
REGIME_EMA_SPAN = 200

# Exit configs (stop_loss, lock_in, trail_offset).
TF_CURRENT = (75.0, 50.0, 150.0)  # live trailing-ratchet exit
SB_FAST = (75.0, 15.0, 40.0)  # SB-style fast lock + tight trail
TP_WHATIF_PTS = 50.0  # fixed +50 take-profit cap (LABELLED what-if)

# Walk-forward chop-threshold grid (kept MODEST — multiple-comparison guard).
CHOP_GRID = (15.0, 20.0, 25.0)
DEFAULT_CHOP_THRESHOLD = 20.0  # Wilder's classic "trend vs range" line


def cfg_params(stop_loss: float, lock_in: float, trail: float) -> RiskParams:
    """A trailing-exit RiskParams at the given knobs (regime flag is irrelevant to the
    exit — the entry tape is built once with regime OFF)."""
    return RiskParams(
        regime_gate_enabled=False,
        exit_mode="trailing",
        stop_loss_pts=stop_loss,
        lock_in_pts=lock_in,
        trail_offset_pts=trail,
    )


def entry_params_regime_off() -> RiskParams:
    """Entry config with the regime gate OFF so below-trend signals fire."""
    return cfg_params(*TF_CURRENT)


# --------------------------------------------------------------------------- #
# Regime tagging — mirrors strategy._regime_ok exactly (window, resample, EMA200)
# --------------------------------------------------------------------------- #
def regime_at(close_window: pd.Series) -> tuple[bool, int, float | None, float | None]:
    """Replicate ``_regime_ok`` on a time-indexed close window ending at the entry bar.

    Returns ``(is_below, buckets, ema200_level, slope_per_bucket)``:
      * ``is_below`` — True iff there are >=202 valid 30-min buckets AND the last
        close <= the 30-min EMA200 (i.e. the gate WOULD have blocked this long).
        Warmup (<202 buckets) -> fail-open -> NOT below (the gate allows it).
      * ``slope_per_bucket`` — secondary causal trend proxy: EMA200 change per 30-min
        bucket over the last 6 buckets (~3h), or None if too few buckets.
    """
    bars_30m = close_window.resample("30min").last().dropna()
    buckets = int(len(bars_30m))
    if buckets < REGIME_MIN_BUCKETS:
        return False, buckets, None, None
    ema = bars_30m.ewm(span=REGIME_EMA_SPAN, adjust=False).mean()
    level = float(ema.iloc[-1])
    last_price = float(close_window.iloc[-1])
    slope = None
    if len(ema) >= 7:
        slope = (float(ema.iloc[-1]) - float(ema.iloc[-7])) / 6.0
    return (last_price <= level), buckets, level, slope


# --------------------------------------------------------------------------- #
# The tagged below-trend tape
# --------------------------------------------------------------------------- #
@dataclass
class BTTape:
    ts: list  # list[datetime], per bar
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    sig_idx: np.ndarray  # bar indices where the REAL gate (regime OFF) fired long_signal
    sig_px: np.ndarray  # entry price (= bar close) at each sig_idx
    sig_below: np.ndarray  # bool: entry was at/below the 30m EMA200 (gate would block)
    sig_adx: np.ndarray  # 1-min ADX(14) at the entry bar (causal chop/trend proxy)
    sig_slope: np.ndarray  # 30m EMA200 slope/bucket at entry (causal, secondary proxy)
    sig_buckets: np.ndarray  # valid 30m buckets seen at entry (diagnostics)
    span: tuple
    bars: int


def build_bt_tape(seg: pd.DataFrame, instrument: str = "NQ") -> BTTape:
    """Run the REAL FastGateEntry with regime OFF over every bar, record every
    long_signal, then tag each at fill time as below/above the 30m EMA200 (the same
    window/resample ``_regime_ok`` uses) and capture its causal ADX + EMA slope."""
    params = entry_params_regime_off()
    drv = engine.FastGateEntry(params, instrument)  # never on_trade_closed -> cooldown stays 0
    n = len(seg)
    sig_idx: list[int] = []
    sig_px: list[float] = []
    for j in range(n):
        decision, signal = drv.step(seg, j)
        if decision == "long_signal" and signal is not None:
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

    return BTTape(
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
        span=(ts[0], ts[-1]),
        bars=n,
    )


def save_tape(tape: BTTape, path: str) -> None:
    with open(path, "wb") as fh:
        pickle.dump(vars(tape), fh)


def load_tape(path: str) -> BTTape:
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    return BTTape(**d) if isinstance(d, dict) else d


# --------------------------------------------------------------------------- #
# The +50 TP what-if exit step (real ratchet + a labelled fixed-TP cap)
# --------------------------------------------------------------------------- #
def _process_exit_bar_tp(
    pos: _OpenPosition,
    params: RiskParams,
    *,
    ts,
    high: float,
    low: float,
    close: float,
    tp_pts: float,
    force_flat,
) -> tuple[float, str] | None:
    """``engine._process_exit_bar`` + a fixed +tp_pts take-profit cap (LABELLED what-if).

    Order: (1) intrabar protective-stop (pessimistic: stop checked before TP on a
    same-bar collision); (2) high-water mark; (3) TP cap — high >= entry+tp -> exit AT
    entry+tp (modeled limit fill); (4) hard ceiling; (5) ratchet; (6) force-flat.
    Uses the REAL compute_ratcheted_stop / should_hard_exit; only the TP cap is added.
    """
    if low <= pos.current_stop:
        return pos.current_stop, (
            "ratchet_stop" if pos.current_stop > pos.entry_price - params.stop_loss_pts else "stop"
        )
    pos.highest = max(pos.highest, high)
    tp_price = pos.entry_price + tp_pts
    if high >= tp_price:
        return tp_price, "take_profit"
    if should_hard_exit(
        entry=pos.entry_price,
        bar_close=close,
        direction=Direction.LONG,
        hard_ceiling_pts=params.hard_ceiling_pts,
    ):
        return close, "hard_ceiling"
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
    if force_flat is not None and force_flat(ts):
        return close, "force_close"
    return None


# --------------------------------------------------------------------------- #
# Masked replay — an independent single-position book over an ELIGIBLE signal subset
# --------------------------------------------------------------------------- #
def replay_masked(
    tape: BTTape,
    params: RiskParams,
    eligible: np.ndarray,
    *,
    lo: int = 0,
    hi: int | None = None,
    qty: int = 2,
    tp_pts: float | None = None,
    friction_pts: float = engine.DEFAULT_FRICTION_PTS,
    force_flat=friday_force_flat,
) -> list[Trade]:
    """Replay ONLY the eligible signals as a standalone single-position book.

    ``eligible`` is a bool mask aligned to ``tape.sig_idx``. Faithful to
    ``engine.simulate_segment`` semantics for the eligible subset: single position,
    cooldown after a close, signals during a held position / cooldown skipped, an
    unclosed position at the range boundary dropped. When ``tp_pts`` is set the
    +TP what-if stepper is used; otherwise the REAL ``_process_exit_bar``.
    """
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
        pos = _OpenPosition(
            entry_ts=tape.ts[e], entry_price=epx, highest=epx, current_stop=epx - sl, bars_held=0
        )
        closed = None
        j = e + 1
        while j < hi:
            if tp_pts is None:
                c = _process_exit_bar(
                    pos,
                    params,
                    ts=tape.ts[j],
                    high=float(tape.high[j]),
                    low=float(tape.low[j]),
                    close=float(tape.close[j]),
                    force_flat=force_flat,
                )
            else:
                c = _process_exit_bar_tp(
                    pos,
                    params,
                    ts=tape.ts[j],
                    high=float(tape.high[j]),
                    low=float(tape.low[j]),
                    close=float(tape.close[j]),
                    tp_pts=tp_pts,
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
        gp, gu, nf, nc = _pnl(pos.entry_price, exit_price, friction_pts, qty)
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
# Eligibility masks
# --------------------------------------------------------------------------- #
def mask_above(tape: BTTape) -> np.ndarray:
    return ~tape.sig_below


def mask_below(tape: BTTape) -> np.ndarray:
    return tape.sig_below.copy()


def mask_below_chop(tape: BTTape, threshold: float) -> np.ndarray:
    """below-trend AND ranging (adx < threshold). NaN adx -> excluded (not chop)."""
    adx = tape.sig_adx
    return tape.sig_below & (adx < threshold) & ~np.isnan(adx)


def mask_below_trend(tape: BTTape, threshold: float) -> np.ndarray:
    """below-trend AND directional (adx >= threshold)."""
    adx = tape.sig_adx
    return tape.sig_below & (adx >= threshold) & ~np.isnan(adx)


# --------------------------------------------------------------------------- #
# Validation — the ALL-signals masked replay MUST equal the real engine (regime OFF)
# --------------------------------------------------------------------------- #
def validate(seg: pd.DataFrame, tape: BTTape) -> tuple[bool, str]:
    """With every signal eligible and TF-current exit, replay_masked must reproduce
    ``engine.simulate_segment`` (regime OFF) byte-for-byte — the trust anchor."""
    bp = cfg_params(*TF_CURRENT)
    eng = engine.simulate_segment(
        seg, engine.FastGateEntry(bp, "NQ"), bp, force_flat=friday_force_flat
    ).trades
    all_elig = np.ones(len(tape.sig_idx), dtype=bool)
    rep = replay_masked(tape, bp, all_elig)
    if len(eng) != len(rep):
        return False, f"trade count differs: engine={len(eng)} replay={len(rep)}"
    en = sum(t.net_usd for t in eng)
    rn = sum(t.net_usd for t in rep)
    mism = 0
    for a, b in zip(eng, rep, strict=False):
        if (
            a.entry_ts != b.entry_ts
            or abs(a.exit_price - b.exit_price) > 1e-6
            or a.exit_reason != b.exit_reason
        ):
            mism += 1
    ok = mism == 0 and abs(en - rn) < 1e-3
    return ok, f"n={len(eng)} engine_net={en:.2f} replay_net={rn:.2f} per_trade_mismatch={mism}"


# --------------------------------------------------------------------------- #
# Walk-forward over the below-CHOP set (joint chop-threshold x exit selection)
# --------------------------------------------------------------------------- #
def _month_bounds(tape: BTTape) -> list[tuple[str, int, int]]:
    months: list[tuple[str, int, int]] = []
    cur = None
    start = 0
    for j, t in enumerate(tape.ts):
        key = t.strftime("%Y-%m")
        if cur is None:
            cur = key
        elif key != cur:
            months.append((cur, start, j))
            cur, start = key, j
    if cur is not None:
        months.append((cur, start, len(tape.ts)))
    return months


# rule = (chop_threshold, exit_label); exit_label in {"TF", "SB"}
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


def walk_forward_chop(
    tape: BTTape,
    *,
    train_months: int = 6,
    test_months: int = 2,
    step_months: int = 2,
    min_train_n: int = 12,
) -> tuple[list[FoldResult], list[Trade]]:
    """Rolling train->test. On train, pick (chop_threshold, exit) with best train
    expectancy on the below-CHOP set (guarded by min_train_n); evaluate on the
    FOLLOWING test fold's below-chop set; pool the OOS test trades."""
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
        for thr in CHOP_GRID:
            elig = mask_below_chop(tape, thr)
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
        elig = mask_below_chop(tape, thr)
        test_trades = replay_masked(tape, _exit_for(ex), elig, lo=te0, hi=te1)
        test_stats = compute_stats(test_trades)
        oos.extend(test_trades)
        folds.append(
            FoldResult(train_label, test_label, thr, ex, train_stats, test_stats, len(test_trades))
        )
        m += step_months
    return folds, oos


def walk_forward_unconditional(
    tape: BTTape,
    exit_label: str,
    *,
    train_months: int = 6,
    test_months: int = 2,
    step_months: int = 2,
) -> list[Trade]:
    """Pool the below-trend (UNCONDITIONAL, no chop filter) test-fold trades under a
    FIXED exit — the honest "trade like SB unconditionally" OOS read (no selection)."""
    months = _month_bounds(tape)
    n_months = len(months)
    elig = mask_below(tape)
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


def _stats_for(tape: BTTape, mask: np.ndarray, exit_label_or_cfg, *, tp_pts=None) -> Stats:
    p = (
        exit_label_or_cfg
        if isinstance(exit_label_or_cfg, RiskParams)
        else _exit_for(exit_label_or_cfg)
    )
    return compute_stats(replay_masked(tape, p, mask, tp_pts=tp_pts))


def build_report(tape: BTTape, validate_msg: str, *, chop_threshold: float) -> str:
    lines: list[str] = []
    add = lines.append
    below = tape.sig_below
    n_sig = len(tape.sig_idx)
    n_below = int(below.sum())
    n_above = n_sig - n_below
    n_warm = int((tape.sig_buckets < REGIME_MIN_BUCKETS).sum())

    add("=" * 94)
    add("PHASE 7 (research) — BELOW-TREND LONG EDGE STUDY (real strategy + exit, modeled fills)")
    add("=" * 94)
    add(f"data span : {tape.span[0]}  ->  {tape.span[1]}")
    add(f"bars      : {tape.bars:,}   regime-OFF gate signals : {n_sig:,}")
    add(
        f"partition : above/allowed={n_above:,}  below/blocked={n_below:,}  "
        f"(of which {n_warm:,} above were 30m-warmup fail-opens)"
    )
    add(f"chop proxy: 1-min ADX(14) at entry (causal); chop = adx < {chop_threshold:.0f}")
    add(f"exits     : TF={TF_CURRENT}  SB-fast={SB_FAST}  +50TP what-if (labelled)")
    add(f"validate  : {validate_msg}")
    add("")

    # ---- ANCHOR ----
    add("-- ANCHOR: above-trend (allowed) independent book, TF-current exit ----------")
    above_tf = _stats_for(tape, mask_above(tape), "TF")
    add(f"  above-trend / TF exit : {_fmt(above_tf)}")
    add("  (sanity: near the regime-ON backtest ~PF 1.174 / ~+$19; it is the INDEPENDENT")
    add("   above-book, so it differs slightly from the interleaved live book — the regime-ON")
    add("   whole-book number is the formal anchor, printed by `backtest --regime on`.)")
    add("")

    # ---- UNCONDITIONAL below-trend (full sample = honest, no params selected) ----
    add("-- BELOW-TREND UNCONDITIONAL (full sample; no chop filter; 'trade like SB always') --")
    for label, cfg in (("TF current", "TF"), ("SB fast  ", "SB")):
        add(f"  {label} : {_fmt(_stats_for(tape, mask_below(tape), cfg))}")
    add("  +50 TP what-if (base ratchet + fixed +50 cap):")
    add(f"     TF base : {_fmt(_stats_for(tape, mask_below(tape), 'TF', tp_pts=TP_WHATIF_PTS))}")
    add(f"     SB base : {_fmt(_stats_for(tape, mask_below(tape), 'SB', tp_pts=TP_WHATIF_PTS))}")
    add("")

    # ---- CHOP vs TREND split (descriptive, full sample, all three exits) ----
    add(f"-- BELOW-TREND CHOP vs TREND split (adx<{chop_threshold:.0f} = chop) — full sample --")
    chop_m = mask_below_chop(tape, chop_threshold)
    trend_m = mask_below_trend(tape, chop_threshold)
    add(f"  n: below-chop={int(chop_m.sum())}  below-trend(directional)={int(trend_m.sum())}")
    add(f"  {'exit':<14} {'segment':<20} {'stats'}")
    for ex_label, ex in (("TF current", "TF"), ("SB fast", "SB")):
        add(f"  {ex_label:<14} {'below-CHOP':<20} {_fmt(_stats_for(tape, chop_m, ex))}")
        add(f"  {ex_label:<14} {'below-TREND(dir)':<20} {_fmt(_stats_for(tape, trend_m, ex))}")
    tp_chop = _stats_for(tape, chop_m, "TF", tp_pts=TP_WHATIF_PTS)
    tp_trend = _stats_for(tape, trend_m, "TF", tp_pts=TP_WHATIF_PTS)
    add(f"  {'+50 TP (TF)':<14} {'below-CHOP':<20} {_fmt(tp_chop)}")
    add(f"  {'+50 TP (TF)':<14} {'below-TREND(dir)':<20} {_fmt(tp_trend)}")
    add("")

    # ---- Robustness: below-chop full-sample surface across thresholds x exits ----
    add("-- ROBUSTNESS: below-CHOP full-sample expectancy/PF across thresholds (plateau=real) --")
    add(f"  {'thr':>4}  {'exit':>4}  {'n':>5} {'win%':>6} {'exp$':>8} {'PF':>7} {'net$':>11}")
    for thr in CHOP_GRID:
        cm = mask_below_chop(tape, thr)
        for ex in ("TF", "SB"):
            s = _stats_for(tape, cm, ex)
            pf = f"{s.profit_factor:.3f}" if s.profit_factor != float("inf") else "inf"
            add(
                f"  {thr:>4.0f}  {ex:>4}  {s.n:>5} {s.win_rate * 100:>5.1f}% "
                f"{s.expectancy_usd:>8.2f} {pf:>7} {s.net_usd:>11.2f}"
            )
    add("")

    # ---- WALK-FORWARD (chop-filtered, joint threshold x exit selection) ----
    add("-- WALK-FORWARD below-CHOP (train=6mo -> test=2mo, step=2mo; pick best train exp) --")
    folds, oos = walk_forward_chop(tape)
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
    add("  AGGREGATE OUT-OF-SAMPLE (below-chop, all test folds pooled):")
    add(f"    {_fmt(oos_stats)}")
    sel_counts: dict = {}
    for f in folds:
        k = f"{f.sel_threshold:.0f}/{f.sel_exit}"
        sel_counts[k] = sel_counts.get(k, 0) + 1
    add(f"    selected-rule frequency: {dict(sorted(sel_counts.items(), key=lambda x: -x[1]))}")
    add("")

    # ---- UNCONDITIONAL below-trend walk-forward OOS (no selection) ----
    add("-- WALK-FORWARD below-trend UNCONDITIONAL OOS (fixed exit, no chop filter) ----------")
    for ex_label, ex in (("TF current", "TF"), ("SB fast", "SB")):
        us = compute_stats(walk_forward_unconditional(tape, ex))
        add(f"  {ex_label:<12} pooled OOS : {_fmt(us)}")
    add("")

    # ---- VERDICT ----
    add("-- VERDICT ----------")
    chop_clears = oos_stats.expectancy_usd > 0 and oos_stats.profit_factor > 1.2
    uncond_tf = compute_stats(walk_forward_unconditional(tape, "TF"))
    uncond_sb = compute_stats(walk_forward_unconditional(tape, "SB"))
    uncond_clears = (uncond_tf.expectancy_usd > 0 and uncond_tf.profit_factor > 1.2) or (
        uncond_sb.expectancy_usd > 0 and uncond_sb.profit_factor > 1.2
    )
    add(
        f"  below-CHOP walk-forward OOS clears exp>0 AND PF>1.2 : "
        f"{'YES' if chop_clears else 'NO'} "
        f"(OOS exp=${oos_stats.expectancy_usd:.2f}, PF="
        f"{oos_stats.profit_factor:.3f}, n={oos_stats.n})"
    )
    add(
        f"  below-trend UNCONDITIONAL OOS clears the bar         : "
        f"{'YES' if uncond_clears else 'NO'} "
        f"(TF exp=${uncond_tf.expectancy_usd:.2f}/PF {uncond_tf.profit_factor:.3f}; "
        f"SB exp=${uncond_sb.expectancy_usd:.2f}/PF {uncond_sb.profit_factor:.3f})"
    )
    add(f"  above-trend anchor (independent book, TF exit)       : {_fmt(above_tf)}")
    add("")
    add("-- CAVEATS ----------")
    add("  * Modeled fills (stop fills AT price intrabar); the +50 TP what-if also assumes the")
    add("    limit fills the instant the bar high touches entry+50 (optimistic) and loses a")
    add("    same-bar stop+TP collision to the stop (pessimistic). LOGIC expectancy, not plumbing.")
    add(
        "  * Independent per-partition books (not de-conflicted vs the above-set); standalone edge."
    )
    add("  * TF-own-entry path only; §0.5.97 friction; NQ≈MNQ points, $2 mult.")
    add("  * Grid kept modest; walk-forward is the honest read — in-sample is upward-biased.")
    add("  * The forward edge in the real regime is unprovable here — confirm forward against §7.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# SECONDARY (tiny-n, directional) — captured-SeanBot below-trend cross-check
# --------------------------------------------------------------------------- #
def sb_cross_check(df: pd.DataFrame, *, from_json: bool = True) -> str:
    """Tag the captured ``seanbot_signals`` LONG entries below/above the SAME 30m EMA200
    and run the below-trend SB subset through TF-current + SB-fast exits (independent
    per-entry). Compares DIRECTION to the 26-month below-trend study. Tiny-n, directional
    only — NOT a verdict. Offline by default (cached JSON; no Supabase call)."""
    from tools.eval.sb_exit_probe import eval_entry_independent, load_sb_entries

    sb = load_sb_entries(from_json)
    ts = [t.to_pydatetime() if hasattr(t, "to_pydatetime") else t for t in df["time"].tolist()]
    arrays = (
        ts,
        df["high"].to_numpy(float),
        df["low"].to_numpy(float),
        df["close"].to_numpy(float),
    )
    close_arr = df["close"].to_numpy(float)
    time_index = pd.DatetimeIndex(df["time"])
    hist_times = np.array([t.timestamp() for t in ts])
    hist_start, hist_end = ts[0], ts[-1]
    max_gap_sec = 180.0

    below_tf, below_sb, above_tf = [], [], []
    n_below = n_above = n_warm = deferred = 0
    from datetime import datetime

    for r in sb:
        ets = datetime.fromisoformat(r["ts"])
        if ets < hist_start or ets > hist_end:
            deferred += 1
            continue
        k = int(np.searchsorted(hist_times, ets.timestamp(), side="left"))
        if k >= len(ts) or abs(ts[k].timestamp() - ets.timestamp()) > max_gap_sec:
            deferred += 1
            continue
        lo = max(0, k + 1 - REGIME_BUFFER)
        win = pd.Series(close_arr[lo : k + 1], index=time_index[lo : k + 1])
        is_below, buckets, _level, _slope = regime_at(win)
        if buckets < REGIME_MIN_BUCKETS:
            n_warm += 1
        px = float(r["price"]) if r.get("price") is not None else float(close_arr[k])
        if is_below:
            n_below += 1
            tb = eval_entry_independent(arrays, k, px, cfg_params(*TF_CURRENT))
            ts_ = eval_entry_independent(arrays, k, px, cfg_params(*SB_FAST))
            if tb is not None:
                below_tf.append(tb)
            if ts_ is not None:
                below_sb.append(ts_)
        else:
            n_above += 1
            ta = eval_entry_independent(arrays, k, px, cfg_params(*TF_CURRENT))
            if ta is not None:
                above_tf.append(ta)

    lines: list[str] = []
    add = lines.append
    add("=" * 94)
    add("SECONDARY — captured-SeanBot below-trend cross-check (tiny-n, DIRECTIONAL only)")
    add("=" * 94)
    add(f"SB long entries captured : {len(sb)}  (entry @ SB reported price; independent exit)")
    add(
        f"in-history & tagged      : below={n_below}  above={n_above}  "
        f"(warmup fail-open within={n_warm})  deferred(no bars/gap)={deferred}"
    )
    add("")
    add("-- SB BELOW-TREND subset through each exit (independent per-entry) ----------")
    add(f"  TF current : {_fmt(compute_stats(below_tf))}")
    add(f"  SB fast    : {_fmt(compute_stats(below_sb))}")
    add("-- SB ABOVE-TREND subset (reference) ----------")
    add(f"  TF current : {_fmt(compute_stats(above_tf))}")
    add("")
    add("-- DIRECTION vs the 26-month below-trend study ----------")
    add("  The 26-month below-trend set has NO robust OOS edge (PF ~1.03-1.04, OOS chop NEGATIVE).")
    add("  This captured SB below-trend subset is tiny-n; read ONLY whether it points the same way")
    add("  (thin/negative, not a clean PF>1.2 edge). It cannot confirm/overturn the main verdict.")
    add("")
    add("-- CAVEATS ----------")
    add("  * n is TINY (~weeks of capture; only the in-history portion simulable) — DIRECTIONAL.")
    add("  * Independent per-entry exit (no concurrency); §0.5.97 friction; modeled fills.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap bars (smoke)")
    ap.add_argument("--rebuild-tape", action="store_true")
    ap.add_argument(
        "--validate", action="store_true", help="assert masked-replay==engine (regime OFF)"
    )
    ap.add_argument("--chop-threshold", type=float, default=DEFAULT_CHOP_THRESHOLD)
    ap.add_argument("--tape-cache", default=TAPE_CACHE)
    ap.add_argument(
        "--sb-cross-check",
        action="store_true",
        help="append the tiny-n captured-SeanBot below-trend cross-check (offline, cached JSON)",
    )
    args = ap.parse_args()

    df = data.load_history()
    if args.limit:
        df = df.iloc[: args.limit].reset_index(drop=True)
    seg = data.to_segments(df)[0]

    tape = None
    if not args.rebuild_tape and not args.limit:
        try:
            tape = load_tape(args.tape_cache)
            if tape.bars != len(seg):
                tape = None
        except (OSError, pickle.PickleError, TypeError, AttributeError):
            tape = None
    if tape is None:
        t0 = _time.perf_counter()
        tape = build_bt_tape(seg, "NQ")
        print(
            f"[tape] built {tape.bars:,} bars, {len(tape.sig_idx):,} signals "
            f"({int(tape.sig_below.sum()):,} below-trend) in {_time.perf_counter() - t0:.0f}s"
        )
        if not args.limit:
            save_tape(tape, args.tape_cache)

    vmsg = "skipped (pass --validate)"
    if args.validate:
        ok, vmsg = validate(seg, tape)
        vmsg = ("PASS " if ok else "FAIL ") + vmsg
        if not ok:
            raise SystemExit(f"[validate] masked-replay != engine — ABORT study. {vmsg}")

    print(build_report(tape, vmsg, chop_threshold=args.chop_threshold))

    if args.sb_cross_check:
        print("")
        print(sb_cross_check(df, from_json=True))


if __name__ == "__main__":
    main()
