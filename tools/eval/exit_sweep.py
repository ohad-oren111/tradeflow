"""Phase-6 (research, OFFLINE) — exit-parameter sweep + WALK-FORWARD validation.

Question: does an SB-style faster-lock exit lift expectancy AND PF over the §7 bar
(expectancy>0 AND PF>1.2) on real 26-month history — OUT-OF-SAMPLE, robustly?

It drives the REAL exit (``trail_manager.compute_ratcheted_stop`` / ``should_hard_exit``
via ``engine._process_exit_bar``) parameterized by ``RiskParams``. It does NOT
re-implement any exit logic. Entry is the REAL strategy gate stream (regime ON, live
config); since the entry gates do not read the swept exit knobs (stop_loss/lock_in/
trail_offset only populate the Signal's stop field + the exit), the entry signal tape
is INVARIANT across the grid and is built ONCE, then each exit config is replayed
trade-by-trade. The replay is asserted byte-identical to the real ``simulate_segment``
on the baseline before any sweep number is trusted (``--validate``).

CAVEATS (carried into every report, never hidden):
  * MODELED fills — entry at signal-bar close; protective stop fills AT the stop price
    intrabar (§0.5.206). Measures the entry+exit LOGIC's expectancy, not live plumbing.
  * TF-own-entry path only (the SeanBot 2nd entry path is not modeled here).
  * NQ 1-min bars (point-identical to MNQ; $2 multiplier for $ P&L). §0.5.97 friction.
  * Sweep is MODEST by design — a large grid manufactures an overfit winner by multiple
    comparisons. Walk-forward + neighbor-plateau checks guard against luck.
  * The forward edge in the real regime is the one thing no backtest can prove.

  python -m tools.eval.exit_sweep [--validate] [--limit N] [--rebuild-tape]
"""

from __future__ import annotations

import argparse
import pickle
import time as _time
from dataclasses import dataclass, replace

import numpy as np

from config.risk_params import RiskParams
from tools.eval import data, engine
from tools.eval.backtest import friday_force_flat
from tools.eval.engine import Trade, _OpenPosition, _pnl, _process_exit_bar
from tools.eval.metrics import Stats, compute_stats

TAPE_CACHE = "/tmp/tf_exit_tape.pkl"

# --- the sweep grid (economically-motivated SB-style fast-lock; kept MODEST) ---
GRID_LOCK_IN = (10.0, 20.0, 30.0, 50.0)
GRID_TRAIL = (30.0, 50.0, 75.0, 100.0, 150.0)
GRID_STOP = (50.0, 60.0, 75.0)

# Baseline = current live trailing config (the PF~1.174 sanity anchor).
BASE_STOP, BASE_LOCK, BASE_TRAIL = 75.0, 50.0, 150.0


def entry_params() -> RiskParams:
    """Live entry config: regime gate ON, trailing exit. Exit knobs at baseline
    (they don't affect the entry tape, but we keep them explicit)."""
    return RiskParams(
        regime_gate_enabled=True,
        exit_mode="trailing",
        stop_loss_pts=BASE_STOP,
        lock_in_pts=BASE_LOCK,
        trail_offset_pts=BASE_TRAIL,
    )


def cfg_params(stop_loss: float, lock_in: float, trail: float) -> RiskParams:
    return replace(
        entry_params(), stop_loss_pts=stop_loss, lock_in_pts=lock_in, trail_offset_pts=trail
    )


# --------------------------------------------------------------------------- #
# Entry tape — built ONCE from the real gate stream
# --------------------------------------------------------------------------- #
@dataclass
class Tape:
    ts: list  # list[datetime], per bar
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    sig_idx: np.ndarray  # bar indices where the real gate fired long_signal
    sig_px: np.ndarray  # entry price (= bar close) at each sig_idx
    span: tuple
    bars: int


def build_tape(seg, params: RiskParams, instrument: str = "NQ") -> Tape:
    """Run the REAL FastGateEntry over every bar (cooldown never armed → the pure
    base decision), recording the long_signal bar indices + entry prices. This is
    the one expensive pass; everything downstream replays cheaply off it."""
    drv = engine.FastGateEntry(params, instrument)  # never call on_trade_closed → cooldown stays 0
    n = len(seg)
    sig_idx: list[int] = []
    sig_px: list[float] = []
    for j in range(n):
        decision, signal = drv.step(seg, j)
        if decision == "long_signal" and signal is not None:
            sig_idx.append(j)
            sig_px.append(float(signal.entry_price))
    ts = [t.to_pydatetime() if hasattr(t, "to_pydatetime") else t for t in seg["time"].tolist()]
    return Tape(
        ts=ts,
        high=seg["high"].to_numpy(dtype=float),
        low=seg["low"].to_numpy(dtype=float),
        close=seg["close"].to_numpy(dtype=float),
        sig_idx=np.asarray(sig_idx, dtype=np.int64),
        sig_px=np.asarray(sig_px, dtype=float),
        span=(ts[0], ts[-1]),
        bars=n,
    )


def save_tape(tape: Tape, path: str) -> None:
    """Persist the tape as a plain dict (module-independent — survives being loaded
    from a different ``__main__`` than the one that built it)."""
    with open(path, "wb") as fh:
        pickle.dump(vars(tape), fh)


def load_tape(path: str) -> Tape:
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    return Tape(**d) if isinstance(d, dict) else d


# --------------------------------------------------------------------------- #
# Cheap trade-by-trade replay (cost ∝ in-position bars only)
# --------------------------------------------------------------------------- #
def replay(
    tape: Tape,
    params: RiskParams,
    *,
    lo: int = 0,
    hi: int | None = None,
    qty: int = 2,
    friction_pts: float = engine.DEFAULT_FRICTION_PTS,
    force_flat=friday_force_flat,
) -> list[Trade]:
    """Replay one exit config over bar range [lo, hi). Faithful to
    ``engine.simulate_segment``: single position, 10-bar cooldown after a close,
    signals during a held position / cooldown are skipped, an unclosed position at
    the range boundary is DROPPED (matches the engine, which records no trade for a
    still-open position at segment end). Validated against the engine on the baseline.
    """
    hi = tape.bars if hi is None else hi
    sl = params.stop_loss_pts
    cooldown = params.cooldown_bars
    trades: list[Trade] = []
    idx_arr = tape.sig_idx
    px_arr = tape.sig_px
    # first signal at/after lo
    i = int(np.searchsorted(idx_arr, lo, side="left"))
    next_allowed = lo
    n_sig = len(idx_arr)
    while i < n_sig:
        e = int(idx_arr[i])
        if e < next_allowed:
            i += 1
            continue
        if e >= hi:
            break
        epx = float(px_arr[i])
        pos = _OpenPosition(
            entry_ts=tape.ts[e], entry_price=epx, highest=epx, current_stop=epx - sl, bars_held=0
        )
        closed = None
        j = e + 1
        while j < hi:
            c = _process_exit_bar(
                pos,
                params,
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
            break  # open position at range end → dropped (engine convention)
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
        # advance past signals inside the just-held trade + cooldown
        while i < n_sig and int(idx_arr[i]) < next_allowed:
            i += 1
    return trades


# --------------------------------------------------------------------------- #
# Validation — the replay MUST equal the real engine on the baseline
# --------------------------------------------------------------------------- #
def validate(seg, tape: Tape) -> tuple[bool, str]:
    bp = cfg_params(BASE_STOP, BASE_LOCK, BASE_TRAIL)
    eng = engine.simulate_segment(
        seg, engine.FastGateEntry(bp, "NQ"), bp, force_flat=friday_force_flat
    ).trades
    rep = replay(tape, bp)
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
# Walk-forward
# --------------------------------------------------------------------------- #
def _month_bounds(tape: Tape) -> list[tuple[str, int, int]]:
    """Contiguous (YYYY-MM, lo, hi) bar ranges per calendar month."""
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


@dataclass
class FoldResult:
    train_label: str
    test_label: str
    sel_stop: float
    sel_lock: float
    sel_trail: float
    train: Stats
    test: Stats
    n_test_trades: int


def walk_forward(
    tape: Tape,
    grid: list[tuple[float, float, float]],
    *,
    train_months: int = 6,
    test_months: int = 2,
    step_months: int = 2,
    min_train_n: int = 30,
) -> tuple[list[FoldResult], list[Trade]]:
    """Rolling train→test. On each train window pick the config with best train
    expectancy (guarded by min_train_n), evaluate it on the FOLLOWING test window,
    aggregate the OOS test trades. Selection criterion = expectancy_usd (the §7
    economic objective), tie-break profit_factor."""
    months = _month_bounds(tape)
    n_months = len(months)
    folds: list[FoldResult] = []
    oos_trades: list[Trade] = []
    m = 0
    while m + train_months + test_months <= n_months:
        tr0, tr1 = months[m][1], months[m + train_months - 1][2]
        te0, te1 = months[m + train_months][1], months[m + train_months + test_months - 1][2]
        train_label = f"{months[m][0]}..{months[m + train_months - 1][0]}"
        test_label = (
            f"{months[m + train_months][0]}..{months[m + train_months + test_months - 1][0]}"
        )
        best = None  # (expectancy, pf, cfg, stats)
        for sl, lk, tr in grid:
            p = cfg_params(sl, lk, tr)
            tt = replay(tape, p, lo=tr0, hi=tr1)
            s = compute_stats(tt)
            if s.n < min_train_n:
                continue
            key = (s.expectancy_usd, s.profit_factor if s.profit_factor != float("inf") else 1e9)
            if best is None or key > best[0]:
                best = (key, (sl, lk, tr), s)
        if best is None:  # no config met the guard → skip fold
            m += step_months
            continue
        (sl, lk, tr) = best[1]
        train_stats = best[2]
        p = cfg_params(sl, lk, tr)
        test_trades = replay(tape, p, lo=te0, hi=te1)
        test_stats = compute_stats(test_trades)
        oos_trades.extend(test_trades)
        folds.append(
            FoldResult(
                train_label, test_label, sl, lk, tr, train_stats, test_stats, len(test_trades)
            )
        )
        m += step_months
    return folds, oos_trades


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _fmt_stats(s: Stats) -> str:
    pf = f"{s.profit_factor:.3f}" if s.profit_factor != float("inf") else "inf"
    return (
        f"n={s.n:<5} win={s.win_rate*100:5.1f}% exp=${s.expectancy_usd:7.2f} "
        f"PF={pf:>6} net=${s.net_usd:10.2f} maxDD=${s.max_drawdown_usd:9.2f}"
    )


def build_report(tape: Tape, validate_msg: str) -> str:
    lines: list[str] = []
    add = lines.append
    n_cfg = len(GRID_STOP) * len(GRID_LOCK_IN) * len(GRID_TRAIL)
    add("=" * 92)
    add("PHASE 6 (research) — EXIT-PARAMETER SWEEP + WALK-FORWARD (real exit, modeled fills)")
    add("=" * 92)
    add(f"data span : {tape.span[0]}  ->  {tape.span[1]}")
    add(f"bars      : {tape.bars:,}   gate signals : {len(tape.sig_idx):,}")
    add(
        f"grid      : stop{list(GRID_STOP)} x lock{list(GRID_LOCK_IN)} x "
        f"trail{list(GRID_TRAIL)} = {n_cfg} configs"
    )
    add(f"validate  : {validate_msg}")
    add("")

    # ---- baseline anchor ----
    base = cfg_params(BASE_STOP, BASE_LOCK, BASE_TRAIL)
    base_stats = compute_stats(replay(tape, base))
    add("-- BASELINE ANCHOR (live: stop75 / lock50 / trail150, regime ON) ----------")
    add(f"  {_fmt_stats(base_stats)}")
    add("  (sanity: must reproduce ~PF 1.174 / ~+$19 expectancy from the Phase-1 backtest)")
    add("")

    # ---- full-sample in-sample sweep ----
    rows = []
    for sl in GRID_STOP:
        for lk in GRID_LOCK_IN:
            for tr in GRID_TRAIL:
                s = compute_stats(replay(tape, cfg_params(sl, lk, tr)))
                rows.append((sl, lk, tr, s))
    rows_by_pf = sorted(
        rows,
        key=lambda r: (r[3].profit_factor if r[3].profit_factor != float("inf") else 1e9),
        reverse=True,
    )
    add("-- FULL-SAMPLE (IN-SAMPLE) SWEEP — top 12 by PF ----------")
    add(
        f"  {'stop':>4} {'lock':>4} {'trail':>5}   {'n':>5} {'win%':>6} "
        f"{'exp$':>8} {'PF':>7} {'net$':>11} {'maxDD$':>10}"
    )
    for sl, lk, tr, s in rows_by_pf[:12]:
        pf = f"{s.profit_factor:.3f}" if s.profit_factor != float("inf") else "inf"
        star = "  <= baseline" if (sl, lk, tr) == (BASE_STOP, BASE_LOCK, BASE_TRAIL) else ""
        add(
            f"  {sl:>4.0f} {lk:>4.0f} {tr:>5.0f}   {s.n:>5} {s.win_rate*100:>5.1f}% "
            f"{s.expectancy_usd:>8.2f} {pf:>7} {s.net_usd:>11.2f} "
            f"{s.max_drawdown_usd:>10.2f}{star}"
        )
    add("")
    clears_in = [r for r in rows if r[3].expectancy_usd > 0 and r[3].profit_factor > 1.2]
    add(f"  configs clearing expectancy>0 AND PF>1.2 IN-SAMPLE: {len(clears_in)}/{len(rows)}")
    best_in = rows_by_pf[0]
    add(
        f"  best in-sample: stop{best_in[0]:.0f}/lock{best_in[1]:.0f}/"
        f"trail{best_in[2]:.0f} -> {_fmt_stats(best_in[3])}"
    )
    add("")

    # ---- robustness: neighbor plateau around the best in-sample config ----
    add("-- ROBUSTNESS: PF surface around best in-sample (plateau=real, spike=luck) ----------")
    bsl, _blk, _btr, _ = best_in
    lookup = {(sl, lk, tr): s for sl, lk, tr, s in rows}
    add(f"  fixing stop={bsl:.0f}; rows=lock_in, cols=trail_offset; cell=PF(n)")
    add("   lock\\trail " + " ".join(f"{t:>10.0f}" for t in GRID_TRAIL))
    for lk in GRID_LOCK_IN:
        cells = []
        for tr in GRID_TRAIL:
            s = lookup.get((bsl, lk, tr))
            if s is None:
                cells.append(f"{'-':>10}")
            else:
                pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "inf"
                cells.append(f"{pf:>5}({s.n:>3})")
        add(f"   {lk:>9.0f} " + " ".join(cells))
    add("")

    # ---- walk-forward ----
    grid = [(sl, lk, tr) for sl in GRID_STOP for lk in GRID_LOCK_IN for tr in GRID_TRAIL]
    folds, oos = walk_forward(tape, grid)
    add("-- WALK-FORWARD (rolling train=6mo -> test=2mo, step=2mo; pick=best train exp) ----------")
    add(
        f"  {'train':>16} {'test':>16}  {'sel':>9} {'tr.PF':>6} {'tr.exp$':>8}  "
        f"||  {'te.n':>5} {'te.win%':>7} {'te.exp$':>8} {'te.PF':>6}"
    )
    for f in folds:
        trpf = f"{f.train.profit_factor:.2f}" if f.train.profit_factor != float("inf") else "inf"
        tepf = f"{f.test.profit_factor:.2f}" if f.test.profit_factor != float("inf") else "inf"
        sel = f"{f.sel_stop:.0f}/{f.sel_lock:.0f}/{f.sel_trail:.0f}"
        add(
            f"  {f.train_label:>16} {f.test_label:>16}  {sel:>9} {trpf:>6} "
            f"{f.train.expectancy_usd:>8.2f}  ||  {f.test.n:>5} "
            f"{f.test.win_rate*100:>6.1f}% {f.test.expectancy_usd:>8.2f} {tepf:>6}"
        )
    oos_stats = compute_stats(oos)
    add("")
    add("  AGGREGATE OUT-OF-SAMPLE (all test folds pooled):")
    add(f"    {_fmt_stats(oos_stats)}")
    in_pf = best_in[3].profit_factor
    add(
        f"    in->out degradation: best-in-sample PF {in_pf:.3f} -> "
        f"walk-forward OOS PF {oos_stats.profit_factor:.3f} "
        f"(Δ {in_pf - oos_stats.profit_factor:+.3f})"
    )
    sel_counts: dict = {}
    for f in folds:
        k = f"{f.sel_stop:.0f}/{f.sel_lock:.0f}/{f.sel_trail:.0f}"
        sel_counts[k] = sel_counts.get(k, 0) + 1
    add(f"    selected-config frequency: {dict(sorted(sel_counts.items(), key=lambda x: -x[1]))}")
    add("")

    # ---- verdict ----
    add("-- VERDICT ----------")
    oos_clears = oos_stats.expectancy_usd > 0 and oos_stats.profit_factor > 1.2
    add(
        f"  walk-forward OOS clears expectancy>0 AND PF>1.2 : {'YES' if oos_clears else 'NO'} "
        f"(OOS exp=${oos_stats.expectancy_usd:.2f}, PF={oos_stats.profit_factor:.3f})"
    )
    add(f"  in-sample configs over the §7 bar               : {len(clears_in)}/{len(rows)}")
    add("")
    add("-- CAVEATS ----------")
    add("  * Modeled fills (stop fills AT price intrabar); logic expectancy, not live plumbing.")
    add("  * TF-own-entry path only; §0.5.97 friction; NQ≈MNQ points, $2 mult.")
    add("  * Grid kept modest; walk-forward is the honest read — in-sample PF is upward-biased.")
    add("  * Forward edge in the real regime is unprovable here — confirm forward against §7.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap bars (smoke)")
    ap.add_argument("--rebuild-tape", action="store_true")
    ap.add_argument("--validate", action="store_true", help="assert replay==engine on baseline")
    ap.add_argument("--tape-cache", default=TAPE_CACHE)
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
        tape = build_tape(seg, entry_params(), "NQ")
        print(
            f"[tape] built {tape.bars:,} bars, {len(tape.sig_idx):,} signals "
            f"in {_time.perf_counter() - t0:.0f}s"
        )
        if not args.limit:
            save_tape(tape, args.tape_cache)

    vmsg = "skipped (pass --validate)"
    if args.validate:
        ok, vmsg = validate(seg, tape)
        vmsg = ("PASS " if ok else "FAIL ") + vmsg
        if not ok:
            raise SystemExit(f"[validate] replay != engine — ABORT sweep. {vmsg}")

    print(build_report(tape, vmsg))


if __name__ == "__main__":
    main()
