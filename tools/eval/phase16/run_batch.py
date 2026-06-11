"""Phase-16 Batch runner / report.

    python -m tools.eval.phase16.run_batch [--no-roll] [--out PATH]

Prints (and saves) the pre-registered gates, the FULL family table including failures,
the survivor holdout + deflated Sharpe + per-year + cost sensitivity, and the blunt
verdict. OFFLINE research, READ-ONLY, drives NO prod path.
"""

from __future__ import annotations

import argparse
import math

from .dataset import (
    HOLDOUT_END,
    HOLDOUT_START,
    TRAIN_END,
    TRAIN_START,
    load_dataset,
)
from .gates import DSR_MIN, EXP_PER_CT_MIN, HOLDOUT_PF_DEGRADE_MAX, N_TRADES_MIN, PF_MIN
from .metrics import expected_max_sharpe
from .protocol import run_batch


def _pf(x: float) -> str:
    return "inf" if not math.isfinite(x) else f"{x:.2f}"


def format_report(results, summary) -> str:
    lines: list[str] = []
    w = lines.append
    w("=" * 88)
    w("PHASE-16 — WIDE STRATEGY DISCOVERY (book-grounded, anti-overfit) — BATCH 1")
    w("=" * 88)
    w("")
    w("PRE-REGISTERED GATES (committed before results; see gates.py):")
    w(
        f"  TRAIN   : PF >= {PF_MIN}, exp/ct >= ${EXP_PER_CT_MIN:.0f}, n >= {N_TRADES_MIN}, "
        f"profitable EACH train year"
    )
    w(
        f"  HOLDOUT : PF >= {PF_MIN}, exp/ct >= ${EXP_PER_CT_MIN:.0f}, each holdout year +, "
        f"PF >= {1 - HOLDOUT_PF_DEGRADE_MAX:.2f} x train PF"
    )
    w(f"  DEFLATE : Deflated Sharpe Ratio >= {DSR_MIN} (over ALL {summary['n_trials']} trials)")
    w("")
    w(
        f"SPLIT (once): TRAIN [{TRAIN_START} .. {TRAIN_END}) = {summary['train_rows']:,} bars | "
        f"HOLDOUT [{HOLDOUT_START} .. {HOLDOUT_END}) = {summary['holdout_rows']:,} bars"
    )
    w(
        f"Roll boundaries back-adjusted: {summary['roll_boundaries']} | "
        f"families: {summary['n_families']} | trials this batch: {summary['n_trials_this_batch']} "
        f"| prior: {summary['prior_trials']} | CUMULATIVE trials: {summary['n_trials']}"
    )
    sr0 = expected_max_sharpe(summary["n_trials"], summary["sr_variance"])
    w(
        f"E[max per-trade Sharpe] under null across {summary['n_trials']} trials = {sr0:.4f} "
        f"(SR var {summary['sr_variance']:.5f}) — the inflated benchmark a winner must beat"
    )
    w(
        "Costs: $0.62/side commission + conditional slippage (entry/stop/mkt 1 tick, target 1/2 "
        "tick); pessimistic fills (next-bar open, stop-before-target, gap-through at open)."
    )
    w("")

    # --- full family table (champions) ---
    w("FULL FAMILY TABLE — train champion per family (ALL families, incl. failures):")
    w("-" * 88)
    w(
        f"{'family':<10} {'champ':<12} {'n':>5} {'PF':>6} {'exp$/ct':>8} {'net$':>10} "
        f"{'yrs':>10} {'TRAIN GATE':>10}"
    )
    w("-" * 88)
    for r in results:
        s = r.train
        yrs = "/".join(f"{'+' if v > 0 else '-'}" for _, v in sorted(s.per_year.items()))
        w(
            f"{r.family.key:<10} {r.champion_label:<12} {s.n_trades:>5} {_pf(s.profit_factor):>6} "
            f"{s.expectancy_usd:>8.2f} {s.total_pnl_usd:>10,.0f} {yrs:>10} "
            f"{'PASS' if r.train_gate_pass else 'FAIL':>10}"
        )
    w("-" * 88)
    w("")

    # --- per-family full grid (every trial logged) ---
    w("EVERY TRIAL (full grid per family — logged for the deflation count):")
    w("-" * 88)
    for r in results:
        for vr in r.all_train:
            s = vr.train
            star = " *champ" if vr.label == r.champion_label else ""
            w(
                f"  {r.family.key:<10} {vr.label:<14} n={s.n_trades:<5} "
                f"PF={_pf(s.profit_factor):<6} "
                f"exp=${s.expectancy_usd:<7.2f} net=${s.total_pnl_usd:<10,.0f} "
                f"SRpt={s.sr_per_trade:.3f}{star}"
            )
    w("-" * 88)
    w("")

    # --- survivors: holdout + deflation ---
    survivors = [r for r in results if r.train_gate_pass]
    if not survivors:
        w(
            "TRAIN-SURVIVORS: none. No family cleared the structural train gates → "
            "holdout NOT touched (preserved for a future batch)."
        )
    else:
        w("TRAIN-SURVIVORS → HOLDOUT (touched once each) + deflation:")
        w("-" * 88)
        for r in survivors:
            h = r.holdout
            w(f"[{r.family.key}/{r.champion_label}]  {r.family.name}")
            w(f"   cite: {r.family.citation}")
            w(
                f"   TRAIN  : PF {_pf(r.train.profit_factor)} exp ${r.train.expectancy_usd:.2f} "
                f"n {r.train.n_trades} SRpt {r.train.sr_per_trade:.3f} "
                f"years {r.train.per_year}"
            )
            w(
                f"   HOLDOUT: PF {_pf(h.profit_factor)} exp ${h.expectancy_usd:.2f} "
                f"n {h.n_trades} years {h.per_year}"
            )
            w(f"   DEFLATE: DSR {r.dsr:.3f} (SR0 {r.sr0:.4f}) | Bonferroni p {r.haircut_p:.3g}")
            sens = " ".join(
                f"{int(t)}t:tr{_pf(v[0])}/ho{_pf(v[1])}"
                for t, v in sorted(r.cost_sensitivity.items())
            )
            w(f"   SLIP-SENS (PF): {sens}")
            for chk in r.holdout_gate_reasons:
                w(f"     - {chk}")
            w(f"   >>> {'CLEARS' if r.cleared else 'FAILS'} the campaign")
            w("")

    # --- verdict ---
    w("=" * 88)
    cleared = summary["cleared"]
    if cleared:
        w(f"VERDICT: {len(cleared)} family(ies) CLEAR train+holdout+deflation: {cleared}")
    else:
        w(
            "VERDICT: NONE. No family clears train + holdout + deflation under the "
            "pre-registered gates."
        )
        w("This is the honest finding (Botty / Phase-15 precedent), not a failure to search.")
        w("Batch-1 families exhausted: " + ", ".join(r.family.key for r in results))
    w("=" * 88)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-roll", action="store_true", help="skip Panama roll-adjust")
    ap.add_argument("--out", default=None, help="write report to this path")
    ap.add_argument(
        "--prior-trials",
        type=int,
        default=0,
        help="cumulative strategy trials from earlier batches (for deflation)",
    )
    args = ap.parse_args()

    ds = load_dataset(roll=not args.no_roll)
    results, summary = run_batch(ds, prior_trials=args.prior_trials)
    report = format_report(results, summary)
    print(report)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(report + "\n")
        print(f"\n[written] {args.out}")


if __name__ == "__main__":
    main()
