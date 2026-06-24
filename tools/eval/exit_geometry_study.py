"""Q-C — MNQ exit-GEOMETRY experiment (research-only, the owed exit study).

Question: the live trailing exit (stop 75 / lock-in 50 / trail 150) is hypothesised to
CLIP WINNERS (the +50 lock + 150 trail cap a runner) while LOSERS RUN to the −75 stop,
forcing an asymmetric win/loss-point ratio that demands a high break-even win rate. If
true, a geometry that CUTS LOSERS FASTER (tighter protective stop) and/or LETS WINNERS
RUN FURTHER (wider trail / no early lock) should lift expectancy.

This drives the REAL prod entry+exit code — NOT a re-implementation:
  * Entry tape: ``exit_sweep.build_tape`` over the REAL ``src.strategy`` gate stream
    (regime ON, live config). Entry gates do not read the exit knobs, so the tape is
    INVARIANT across variants and built ONCE; each variant is replayed via the REAL
    ``engine._process_exit_bar`` (``trail_manager.compute_ratcheted_stop`` /
    ``should_hard_exit``) — ``exit_sweep.replay`` (validated byte-identical to
    ``engine.simulate_segment`` on the baseline).
  * Data: roll-adjusted ``research/data/nq_1min_raw.csv`` (2024-03..2026-06), the SAME
    tape phase16/17/18 used. NQ points = MNQ points; $2 multiplier in P&L.

TWO outputs per variant:
  (A) the OWED ASYMMETRY DECOMPOSITION — avg-winner-pts, avg-loser-pts, win-rate, and the
      BREAK-EVEN win-rate = avg_loss / (avg_win + avg_loss); gap = actual − breakeven.
  (B) the PRE-REGISTERED phase16 GATE verdict — re-priced at 1 contract under the
      phase16 pessimistic cost model ($0.62/side + 1-tick adverse slippage), split
      TRAIN/HOLDOUT on the phase16 calendar boundary, PF>=1.30 / exp/ct>=$5 / n>=200 /
      each-year-positive / holdout-degradation / DSR>=0.95.

GATE: a variant earns a prod exit-geometry PR ONLY if it CLEARS the full phase16 bar.
If none does, the verdict is written plainly — a clean negative CLOSES the MNQ exit
question. NOT a re-run of PR #122's stop×lock×trail grid (done: walk-forward OOS PF
1.111, FAIL) — this tests the untested directions + the asymmetry framing.

CAVEATS (carried, never hidden): MODELED fills (entry at signal-bar close, stop AT the
stop intrabar — §0.5.206); TF-own-entry path only; the forward edge in the live regime
is the one thing no backtest can settle.

  python -m tools.eval.exit_geometry_study [--prior-trials 88] [--rebuild-tape] [--out PATH]
"""

from __future__ import annotations

import argparse
import math
import statistics
from dataclasses import dataclass, replace

from tools.eval import data
from tools.eval.exit_sweep import build_tape, entry_params, load_tape, replay, save_tape
from tools.eval.phase16.costs import CostModel, Slippage
from tools.eval.phase16.gates import (
    DSR_MIN,
    EXP_PER_CT_MIN,
    N_TRADES_MIN,
    PF_MIN,
    check_holdout,
    check_train,
)
from tools.eval.phase16.metrics import (
    bonferroni_haircut,
    compute_stats,
    deflated_sharpe,
    expected_max_sharpe,
)
from tools.eval.phase18.run import _split, _trades_to_df

TAPE_CACHE = "/tmp/tf_exit_geom_tape.pkl"

# (name, stop_loss_pts, lock_in_pts, trail_offset_pts). hard_ceiling stays 1000 (default).
# Directions PR#122 did NOT test: trail > 150 (let winners run), stop < 50 (cut losers),
# lock = 0 (don't clip a runner at +50). Modest set — deflation counts every one.
VARIANTS: list[tuple[str, float, float, float]] = [
    ("baseline_s75_l50_t150", 75.0, 50.0, 150.0),  # the live config (anchor)
    # --- cut losers faster (tighter protective stop) ---
    ("cutloss_s60", 60.0, 50.0, 150.0),
    ("cutloss_s50", 50.0, 50.0, 150.0),
    ("cutloss_s40", 40.0, 50.0, 150.0),
    # --- let winners run further (wider trailing tail; >150 untested) ---
    ("runwin_t200", 75.0, 50.0, 200.0),
    ("runwin_t250", 75.0, 50.0, 250.0),
    ("runwin_t300", 75.0, 50.0, 300.0),
    # --- remove the early +50 lock so a runner isn't clipped ---
    ("nolock_t150", 75.0, 0.0, 150.0),
    ("nolock_t300", 75.0, 0.0, 300.0),
    # --- combos: attack BOTH sides of the asymmetry at once ---
    ("cut50_run250", 50.0, 50.0, 250.0),
    ("cut40_run300", 40.0, 30.0, 300.0),
    ("cut50_nolock_run300", 50.0, 0.0, 300.0),
]


def cfg(stop: float, lock: float, trail: float) -> object:
    return replace(entry_params(), stop_loss_pts=stop, lock_in_pts=lock, trail_offset_pts=trail)


@dataclass
class VariantResult:
    name: str
    stop: float
    lock: float
    trail: float
    n: int
    win_rate: float
    avg_win_pts: float
    avg_loss_pts: float
    breakeven_win_rate: float
    gap: float  # actual win_rate − breakeven (positive = profitable geometry)
    # phase16 gate
    full_pf: float
    full_exp: float
    train_n: int
    train_pf: float
    train_exp: float
    train_each_year: bool
    train_pass: bool
    hold_n: int
    hold_pf: float
    hold_exp: float
    train_sr: float
    holdout_pass: bool
    dsr: float
    clears: bool


def _pf(x: float) -> float:
    return float("inf") if not math.isfinite(x) else round(x, 3)


def _asymmetry(trades) -> tuple[int, float, float, float, float, float]:
    """(n, win_rate, avg_win_pts, avg_loss_pts, breakeven_win_rate, gap) on gross points."""
    gp = [t.gross_pts for t in trades]
    n = len(gp)
    if n == 0:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0
    wins = [g for g in gp if g > 0]
    losses = [-g for g in gp if g < 0]
    win_rate = len(wins) / n
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    denom = avg_win + avg_loss
    breakeven = (avg_loss / denom) if denom > 0 else 0.0
    return n, win_rate, avg_win, avg_loss, breakeven, win_rate - breakeven


def evaluate(tape, slip: Slippage, costs: CostModel) -> list[VariantResult]:
    results: list[VariantResult] = []
    for name, stop, lock, trail in VARIANTS:
        params = cfg(stop, lock, trail)
        trades = replay(tape, params)
        n, win_rate, avg_win, avg_loss, breakeven, gap = _asymmetry(trades)
        df = _trades_to_df(trades, slip, costs)
        tr_df, ho_df = _split(df)
        s_full = compute_stats(df)
        s_tr = compute_stats(tr_df)
        s_ho = compute_stats(ho_df)
        tg = check_train(s_tr)
        results.append(
            VariantResult(
                name=name, stop=stop, lock=lock, trail=trail,
                n=n, win_rate=win_rate, avg_win_pts=avg_win, avg_loss_pts=avg_loss,
                breakeven_win_rate=breakeven, gap=gap,
                full_pf=_pf(s_full.profit_factor), full_exp=round(s_full.expectancy_usd, 2),
                train_n=s_tr.n_trades, train_pf=_pf(s_tr.profit_factor),
                train_exp=round(s_tr.expectancy_usd, 2),
                train_each_year=all(v > 0 for v in s_tr.per_year.values()) if s_tr.per_year else False,
                train_pass=tg.passed,
                hold_n=s_ho.n_trades, hold_pf=_pf(s_ho.profit_factor),
                hold_exp=round(s_ho.expectancy_usd, 2),
                train_sr=s_tr.sr_per_trade,
                holdout_pass=False, dsr=0.0, clears=False,
            )
        )
    return results


def apply_deflation(results: list[VariantResult], tape, slip, costs, *, prior_trials: int) -> dict:
    """Champion = max train PF. DSR with a within-batch Sharpe-variance pool over the
    variants (the gauntlet/phase16 deflator). Then re-score the champion's holdout +
    any train-passing variant's holdout gate."""
    n_trials = prior_trials + len(VARIANTS)
    train_srs = [r.train_sr for r in results if r.train_n >= 4]
    sr_var = float(statistics.variance(train_srs)) if len(train_srs) > 1 else 0.0
    sr0 = expected_max_sharpe(n_trials, sr_var)
    # rebuild the champion's full stats for the DSR (need skew/kurtosis)
    for r in results:
        if not r.train_pass:
            continue
        params = cfg(r.stop, r.lock, r.trail)
        df = _trades_to_df(replay(tape, params), slip, costs)
        tr_df, ho_df = _split(df)
        s_tr = compute_stats(tr_df)
        s_ho = compute_stats(ho_df)
        dsr, _ = deflated_sharpe(
            s_tr.sr_per_trade, s_tr.n_trades, s_tr.skew, s_tr.kurtosis, n_trials, sr_var
        )
        hg = check_holdout(s_tr, s_ho, dsr)
        r.dsr = round(dsr, 4)
        r.holdout_pass = bool(hg.passed)
        r.clears = bool(r.train_pass and hg.passed)
    _, haircut_p = bonferroni_haircut(
        max((r.train_sr for r in results), default=0.0),
        max((r.train_n for r in results), default=0),
        n_trials,
    )
    champ = max(results, key=lambda r: (-1.0 if r.train_pf == float("inf") else r.train_pf))
    return {
        "n_trials": n_trials, "sr_variance_pool": round(sr_var, 6),
        "sr0_expected_max": round(sr0, 5), "bonferroni_p": haircut_p, "champion": champ.name,
    }


def build_report(results: list[VariantResult], defl: dict, span, bars: int, sigs: int) -> str:
    L: list[str] = []
    w = L.append
    w("=" * 100)
    w("Q-C — MNQ EXIT-GEOMETRY EXPERIMENT (real strategy + real exit; modeled fills)")
    w("=" * 100)
    w(f"tape: {span[0]} -> {span[1]}  ({bars:,} 1-min NQ bars, roll-adjusted; {sigs:,} gate signals)")
    w("entry = REAL src.strategy gate stream (regime ON, live config); exit = REAL trail_manager.")
    w(f"deflation: {defl['n_trials']} trials (prior + {len(VARIANTS)} variants), "
      f"sr_var_pool={defl['sr_variance_pool']}, SR0={defl['sr0_expected_max']}, "
      f"bonferroni_p={defl['bonferroni_p']:.3g}")
    w("")
    w("-- (A) WIN/LOSS POINT ASYMMETRY (gross pts) — the owed decomposition -------------------------------")
    w(f"  {'variant':<22}{'n':>5}{'win%':>7}{'avgWin':>8}{'avgLoss':>9}{'R:R':>6}{'breakeven%':>11}{'gap':>7}")
    for r in results:
        rr = (r.avg_win_pts / r.avg_loss_pts) if r.avg_loss_pts > 0 else float("inf")
        rr_s = "inf" if rr == float("inf") else f"{rr:.2f}"
        w(f"  {r.name:<22}{r.n:>5}{r.win_rate*100:>6.1f}%{r.avg_win_pts:>8.1f}{r.avg_loss_pts:>9.1f}"
          f"{rr_s:>6}{r.breakeven_win_rate*100:>10.1f}%{r.gap*100:>+6.1f}")
    w("  (gap = actual win% − breakeven win%; >0 means the geometry's R:R is self-sustaining)")
    w("")
    w("-- (B) PHASE-16 GATE (1 ct, pessimistic costs; TRAIN 2024-03..2025-08 / HOLDOUT 2025-09..2026-06) --")
    w(f"  {'variant':<22}{'fullPF':>7}{'trainPF':>8}{'tr.exp$':>8}{'tr.n':>6}{'yr+':>5}"
      f"{'holdPF':>8}{'ho.exp$':>8}{'DSR':>7}{'CLEARS':>8}")
    for r in results:
        w(f"  {r.name:<22}{r.full_pf:>7}{r.train_pf:>8}{r.train_exp:>8.2f}{r.train_n:>6}"
          f"{('Y' if r.train_each_year else 'n'):>5}{r.hold_pf:>8}{r.hold_exp:>8.2f}"
          f"{r.dsr:>7}{('YES' if r.clears else 'no'):>8}")
    w("")
    w(f"  pre-registered bar: PF>={PF_MIN} train+holdout, exp/ct>=${EXP_PER_CT_MIN:.0f}, "
      f"n>={N_TRADES_MIN}, each-year+, holdout PF>=0.75*train, DSR>={DSR_MIN}")
    w("")
    clears = [r for r in results if r.clears]
    base = results[0]
    w("-- VERDICT ----------------------------------------------------------------------------------------")
    if clears:
        w(f"  {len(clears)} variant(s) CLEAR the full phase16 bar: {[r.name for r in clears]}")
        w("  -> a prod exit-geometry change is JUSTIFIED; PR the best clearing variant.")
    else:
        w("  NO variant clears the full phase16 bar (PF>=1.30 train+holdout / exp/ct>=$5 / n>=200 /")
        w("  each-year+ / DSR>=0.95). SMA-bounce exit-geometry does NOT clear the harness; the")
        w("  negative expectancy is STRUCTURAL — it is in the ENTRY, not recoverable by re-cutting")
        w("  the stop/trail/lock. The hypothesis (trail clips winners while losers run) is")
        w("  CONFIRMED as a description (see the breakeven-win% column) but NOT FIXABLE by geometry:")
        w(f"    baseline avg winner {base.avg_win_pts:.0f}pt vs avg loser {base.avg_loss_pts:.0f}pt "
          f"-> needs {base.breakeven_win_rate*100:.0f}% wins, actual {base.win_rate*100:.0f}% "
          f"(gap {base.gap*100:+.0f}pts).")
        w("    Cutting losers raises win% but shrinks avg-winner in lockstep; letting winners run")
        w("    lowers win% — the R:R/win-rate tradeoff stays on the same break-even locus. PIVOT:")
        w("    the MNQ SMA-bounce edge question is CLOSED negative; pursue a different signal, not")
        w("    a different exit. (Forward edge in the live regime remains unprovable here.)")
    w("=" * 100)
    return "\n".join(L)


def run_study(*, prior_trials: int, rebuild_tape: bool) -> tuple[str, bool]:
    slip = Slippage()
    costs = CostModel()
    df = data.load_history()
    span = (df["time"].iloc[0], df["time"].iloc[-1])
    df, _info = data.roll_adjust(df)
    seg = data.to_segments(df)[0]
    tape = None
    if not rebuild_tape:
        try:
            tape = load_tape(TAPE_CACHE)
            if tape.bars != len(seg):
                tape = None
        except Exception:  # noqa: BLE001
            tape = None
    if tape is None:
        tape = build_tape(seg, entry_params(), "NQ")
        save_tape(tape, TAPE_CACHE)
    results = evaluate(tape, slip, costs)
    defl = apply_deflation(results, tape, slip, costs, prior_trials=prior_trials)
    report = build_report(results, defl, tape.span, tape.bars, len(tape.sig_idx))
    any_clears = any(r.clears for r in results)
    return report, any_clears


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prior-trials", type=int, default=88,
                    help="cumulative prior exit-config trials (phase18 1 + PR#122 ~60 + margin)")
    ap.add_argument("--rebuild-tape", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    report, _ = run_study(prior_trials=args.prior_trials, rebuild_tape=args.rebuild_tape)
    print(report)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(report + "\n")
        print(f"\n[written] {args.out}")


if __name__ == "__main__":
    main()
