"""Phase-18 — TF FOUNDATIONAL EDGE AUDIT (research-only).

The question: does TF's ACTUAL gated strategy have a demonstrated edge over the full
~27-month tape, under the SAME honest protocol that returned NONE for everything in
Phase-15/16/17?

This module does NOT re-implement the strategy. It drives the REAL prod code:

  * Entry/exit:  ``tools.eval.engine.simulate_segment`` + ``FastGateEntry`` — which call
                 ``src.strategy.evaluate_gates`` / ``_regime_ok`` (the live SMA100-bounce
                 long + 30m-EMA200 regime gate) and ``src.execution.trail_manager``
                 (base entry-75, +50 lock-in, 150 trail, 1000 hard-ceiling). The live
                 config is ``exit_mode='trailing'``, ``regime_gate_enabled=True``
                 (verified against the running container this session).
  * Data:        ``tools.eval.data.load_history`` over the Panama-roll-adjusted
                 ``research/data/nq_1min_raw.csv`` (2024-03 .. 2026-06), the SAME tape
                 every other phase used.

It scores those trades with the SAME pre-registered honest bar:

  * Costs:       ``tools.eval.phase16.costs`` — $0.62/side commission + conditional
                 1-tick slippage on every fill (pessimistic), at 1 contract so
                 expectancy IS $/contract.
  * Metrics:     ``tools.eval.phase16.metrics`` (PF, exp/ct, n, per-year, max DD,
                 deflated Sharpe / Bonferroni haircut).
  * Gates:       ``tools.eval.phase16.gates`` (TRAIN PF>=1.30, exp/ct>=$5, n>=200,
                 each-year +; HOLDOUT same + degradation cap; deflation).
  * Split:       the SAME fixed calendar boundary as Phase-16/17 (split-once;
                 TRAIN 2024-03..2025-08, HOLDOUT 2025-09..2026-06).

Two claims are reported SEPARATELY:
  (a) does the GATED strategy clear the bar?
  (b) what does the GATE add? — gated vs ungated same-entry P&L, plus the ungated
      trades partitioned by the real regime state at entry (above vs below the 30m
      EMA200), so we can see whether the edge is the entry, the gate, or neither.

    python -m tools.eval.phase18.run [--no-roll] [--prior-trials 27] [--out PATH]
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

import pandas as pd

from config.risk_params import RiskParams
from src.strategy import _regime_ok
from tools.eval import data, engine
from tools.eval.backtest import friday_force_flat
from tools.eval.phase16.costs import CostModel, Slippage
from tools.eval.phase16.dataset import (
    HOLDOUT_END,
    HOLDOUT_START,
    TRAIN_END,
    TRAIN_START,
)
from tools.eval.phase16.gates import (
    DSR_MIN,
    EXP_PER_CT_MIN,
    HOLDOUT_PF_DEGRADE_MAX,
    N_TRADES_MIN,
    PF_MIN,
    check_holdout,
    check_train,
)
from tools.eval.phase16.metrics import (
    Stats,
    bonferroni_haircut,
    compute_stats,
    deflated_sharpe,
)

_BUFFER = 7000  # FastGateEntry's live regime/indicator buffer

# exit_reason (engine.Trade) -> Slippage exit kind. The protective stop (base or
# ratcheted) is a stop-MARKET fill (§0.5.206); hard-ceiling / weekend force-close are
# market-outs. Both pay a full adverse tick under the Phase-16 pessimistic model.
_EXIT_KIND = {
    "stop": "stop",
    "ratchet_stop": "stop",
    "hard_ceiling": "market",
    "force_close": "market",
}


def _live_params(*, regime: bool) -> RiskParams:
    """The LIVE exit/entry config (verified vs the running container 2026-06-11):
    exit_mode trailing, stop 75, lock-in 50, trail 150, hard-ceiling 1000, cooldown 10.
    ``regime`` toggles ONLY the 30m-EMA200 gate (the one thing the audit isolates)."""
    return RiskParams(
        regime_gate_enabled=regime,
        exit_mode="trailing",
        stop_loss_pts=75.0,
        lock_in_pts=50.0,
        trail_offset_pts=150.0,
        hard_ceiling_pts=1000.0,
        cooldown_bars=10,
    )


def _trade_pnl_1ct(tr: engine.Trade, slip: Slippage, costs: CostModel) -> float:
    """Re-price one engine Trade under the Phase-16 pessimistic cost model at 1 contract.

    The engine already fills the entry at the signal-bar close and the protective stop
    AT the stop price intrabar. The Phase-16 layer adds the conditional adverse tick on
    every fill and the $0.62/side commission — the exact honesty layer Phase-16/17 used.
    LONG only (direction +1).
    """
    entry_px = slip.adjust_entry(tr.entry_price, +1)
    kind = _EXIT_KIND[tr.exit_reason]
    exit_px = slip.adjust_exit(tr.exit_price, +1, kind)
    return costs.net_pnl_usd(entry_px, exit_px, +1, contracts=1)


def _trades_to_df(trades: list[engine.Trade], slip: Slippage, costs: CostModel) -> pd.DataFrame:
    """Phase-16-shaped ledger: pnl_usd (1 ct, after costs), entry_time, exit_time, year(ET)."""
    rows = []
    for tr in trades:
        rows.append(
            {
                "pnl_usd": _trade_pnl_1ct(tr, slip, costs),
                "entry_time": pd.Timestamp(tr.entry_ts),
                "exit_time": pd.Timestamp(tr.exit_ts),
                "year": pd.Timestamp(tr.exit_ts).tz_convert("America/New_York").year,
                "exit_reason": tr.exit_reason,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["pnl_usd", "entry_time", "exit_time", "year", "exit_reason"])
    return pd.DataFrame(rows)


def _split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a ledger into TRAIN / HOLDOUT by ENTRY time on the Phase-16 boundary.

    Split by entry (not exit) so a trade belongs entirely to the regime it was opened in;
    the engine runs ONE continuous tape (live never restarts at the split) and trades are
    bucketed after the fact — no cold-start at the holdout boundary.
    """
    tr_lo = pd.Timestamp(TRAIN_START, tz="UTC")
    tr_hi = pd.Timestamp(TRAIN_END, tz="UTC")  # == HOLDOUT_START, exclusive upper for train
    ho_hi = pd.Timestamp(HOLDOUT_END, tz="UTC")
    if df.empty:
        return df, df
    train = df[(df["entry_time"] >= tr_lo) & (df["entry_time"] < tr_hi)].reset_index(drop=True)
    hold = df[(df["entry_time"] >= tr_hi) & (df["entry_time"] < ho_hi)].reset_index(drop=True)
    return train, hold


# --------------------------------------------------------------------------- #
# Regime-at-entry tagging (claim b2) — uses the REAL _regime_ok, no re-impl.
# --------------------------------------------------------------------------- #
def _tag_regime_at_entry(
    trades: list[engine.Trade], seg: pd.DataFrame, params: RiskParams
) -> list[bool]:
    """For each (ungated) trade, recompute the live 30m-EMA200 gate at its entry bar.

    Calls the production ``_regime_ok`` on the SAME trailing 7000-bar tail FastGateEntry
    feeds it, with a regime-ENABLED params — i.e. exactly the decision the live gate would
    have made at that bar. Returns True == above-trend (gate would OPEN).
    """
    idx_by_ts = {ts.to_pydatetime(): i for i, ts in enumerate(seg["time"])}
    out: list[bool] = []
    for tr in trades:
        idx = idx_by_ts.get(tr.entry_ts)
        if idx is None:
            out.append(True)  # unmappable (shouldn't happen) — fail open, same as prod
            continue
        lo = max(0, idx + 1 - _BUFFER)
        tail = seg.iloc[lo : idx + 1]
        out.append(_regime_ok(tail, params))
    return out


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _pf(x: float) -> str:
    return "inf" if not math.isfinite(x) else f"{x:.3f}"


def _years_str(s: Stats) -> str:
    return " ".join(
        f"{y}:{'+' if v > 0 else '-'}{v:,.0f}({s.per_year_trades.get(y, 0)})"
        for y, v in sorted(s.per_year.items())
    )


@dataclass
class Run:
    gated: pd.DataFrame
    ungated: pd.DataFrame
    g_full: Stats
    g_train: Stats
    g_hold: Stats
    u_full: Stats
    train_gate: object
    holdout_gate: object | None
    dsr: float
    sr0: float
    haircut_p: float
    n_trials: int
    above: Stats
    below: Stats
    seg_bars: int
    span: tuple
    roll_boundaries: int


def run_audit(*, roll: bool = True, prior_trials: int = 27) -> Run:
    slip = Slippage()  # 1-tick entry, 1-tick stop, 1-tick market (pessimistic)
    costs = CostModel()  # $0.62/side

    df = data.load_history()
    span = (df["time"].iloc[0], df["time"].iloc[-1])
    roll_n = 0
    if roll:
        df, info = data.roll_adjust(df)
        roll_n = len(info.boundaries)
    seg = data.to_segments(df)[0]

    # --- drive the REAL strategy + exit, gated and ungated, over one continuous tape ---
    res_gated = engine.simulate_segment(
        seg,
        engine.FastGateEntry(_live_params(regime=True), "NQ"),
        _live_params(regime=True),
        force_flat=friday_force_flat,
    )
    res_ungated = engine.simulate_segment(
        seg,
        engine.FastGateEntry(_live_params(regime=False), "NQ"),
        _live_params(regime=False),
        force_flat=friday_force_flat,
    )

    gated_df = _trades_to_df(res_gated.trades, slip, costs)
    ungated_df = _trades_to_df(res_ungated.trades, slip, costs)

    g_train_df, g_hold_df = _split(gated_df)
    g_full = compute_stats(gated_df)
    g_train = compute_stats(g_train_df)
    g_hold = compute_stats(g_hold_df)
    u_full = compute_stats(ungated_df)

    # --- claim (a): the pre-registered gate ---
    tg = check_train(g_train)
    holdout_gate = None
    dsr = sr0 = 0.0
    haircut_p = 1.0
    n_trials = prior_trials + 1  # TF's live config = ONE pre-registered trial this batch
    if tg.passed:
        dsr, sr0 = deflated_sharpe(
            g_train.sr_per_trade,
            g_train.n_trades,
            g_train.skew,
            g_train.kurtosis,
            n_trials,
            sr_variance=0.0,  # single config: no within-batch Sharpe pool (see report)
        )
        _, haircut_p = bonferroni_haircut(g_train.sr_per_trade, g_train.n_trades, n_trials)
        holdout_gate = check_holdout(g_train, g_hold, dsr)
    else:
        # still report the Bonferroni deflator as a cross-check even when train fails
        _, haircut_p = bonferroni_haircut(g_train.sr_per_trade, g_train.n_trades, n_trials)

    # --- claim (b2): partition the ungated trades by the REAL regime-at-entry ---
    above_mask = _tag_regime_at_entry(res_ungated.trades, seg, _live_params(regime=True))
    ungated_df = ungated_df.copy()
    ungated_df["above_trend"] = above_mask
    above = compute_stats(ungated_df[ungated_df["above_trend"]].reset_index(drop=True))
    below = compute_stats(ungated_df[~ungated_df["above_trend"]].reset_index(drop=True))

    return Run(
        gated=gated_df,
        ungated=ungated_df,
        g_full=g_full,
        g_train=g_train,
        g_hold=g_hold,
        u_full=u_full,
        train_gate=tg,
        holdout_gate=holdout_gate,
        dsr=dsr,
        sr0=sr0,
        haircut_p=haircut_p,
        n_trials=n_trials,
        above=above,
        below=below,
        seg_bars=len(seg),
        span=span,
        roll_boundaries=roll_n,
    )


def _stat_line(tag: str, s: Stats) -> str:
    return (
        f"  {tag:<22} n={s.n_trades:<5} PF={_pf(s.profit_factor):<6} "
        f"exp/ct=${s.expectancy_usd:<8.2f} net=${s.total_pnl_usd:<11,.0f} "
        f"win={s.win_rate*100:4.1f}% maxDD=${s.max_drawdown_usd:<10,.0f}"
    )


def format_report(r: Run, *, prior_trials: int) -> str:
    lines: list[str] = []
    w = lines.append
    w("=" * 92)
    w("PHASE-18 — TF FOUNDATIONAL EDGE AUDIT (real gated strategy vs the pre-registered bar)")
    w("=" * 92)
    w("")
    w("WHAT RAN: the LIVE prod entry+exit code (src.strategy.evaluate_gates / _regime_ok +")
    w("  src.execution.trail_manager), driven bar-by-bar over the saved tape. NOT a re-impl.")
    w("  Live config (verified vs container 2026-06-11): exit_mode=trailing, regime gate ON,")
    w("  stop 75 / lock-in 50 / trail 150 / hard-ceiling 1000 / cooldown 10. 1 contract.")
    w("")
    w("PRE-REGISTERED BAR (identical to Phase-16/17 — gates.py, committed before results):")
    w(
        f"  TRAIN   : PF >= {PF_MIN}, exp/ct >= ${EXP_PER_CT_MIN:.0f}, n >= {N_TRADES_MIN}, "
        "profitable EACH train year"
    )
    w(
        f"  HOLDOUT : PF >= {PF_MIN}, exp/ct >= ${EXP_PER_CT_MIN:.0f}, each holdout year +, "
        f"PF >= {1 - HOLDOUT_PF_DEGRADE_MAX:.2f} x train PF"
    )
    w(
        f"  DEFLATE : Bonferroni haircut over CUMULATIVE {r.n_trials} trials "
        f"(prior {prior_trials} + this 1); DSR target >= {DSR_MIN}"
    )
    w("")
    s0, s1 = r.span
    w(f"TAPE    : {s0} -> {s1}  ({r.seg_bars:,} 1-min NQ bars, point-identical to MNQ)")
    w(f"SPLIT   : TRAIN [{TRAIN_START}..{TRAIN_END}) | HOLDOUT [{HOLDOUT_START}..{HOLDOUT_END})")
    w("          (split-once, by ENTRY time; same boundary as Phase-16/17)")
    w(f"ROLL    : {r.roll_boundaries} Panama quarterly boundaries back-adjusted")
    w("COSTS   : $0.62/side commission + 1-tick adverse slippage on entry & every exit fill")
    w("          (pessimistic); engine fills entry at signal-bar close, stop AT stop intrabar.")
    w("")
    w("-" * 92)
    w("CLAIM (a) — DOES THE GATED STRATEGY CLEAR THE BAR?")
    w("-" * 92)
    w(_stat_line("GATED full tape", r.g_full))
    w(f"      per-year: {_years_str(r.g_full)}")
    w(_stat_line("GATED train", r.g_train))
    w(f"      per-year: {_years_str(r.g_train)}")
    w(_stat_line("GATED holdout", r.g_hold))
    w(f"      per-year: {_years_str(r.g_hold)}")
    w("")
    w("  TRAIN GATE checks:")
    for c in r.train_gate.reasons:
        w(f"     - {c}")
    w(f"  >>> TRAIN GATE: {'PASS' if r.train_gate.passed else 'FAIL'}")
    if r.holdout_gate is not None:
        w("")
        w("  HOLDOUT GATE checks (train passed, so holdout was touched):")
        for c in r.holdout_gate.reasons:
            w(f"     - {c}")
        w(f"     - DSR {r.dsr:.3f} (SR0 {r.sr0:.4f}) | Bonferroni p {r.haircut_p:.3g}")
        w(f"  >>> HOLDOUT GATE: {'PASS' if r.holdout_gate.passed else 'FAIL'}")
    else:
        w("  HOLDOUT: NOT touched — train gate failed, holdout preserved (protocol step 2).")
        w(
            f"  DEFLATION cross-check anyway: Bonferroni haircut p = {r.haircut_p:.3g} "
            f"over {r.n_trials} cumulative trials"
        )
        w("  (Note: the full Deflated Sharpe needs a within-batch Sharpe-variance pool; TF's")
        w("   live config is a SINGLE pre-registered strategy, not a grid winner, so the")
        w("   Bonferroni haircut is the appropriate standalone multiple-testing deflator here.)")
    cleared_a = r.train_gate.passed and (r.holdout_gate is not None and r.holdout_gate.passed)
    w("")
    w(
        f"  ==> CLAIM (a) VERDICT: GATED strategy {'CLEARS' if cleared_a else 'DOES NOT CLEAR'} "
        "the pre-registered bar."
    )
    w("")
    w("-" * 92)
    w("CLAIM (b) — WHAT DOES THE GATE ADD? (gated vs ungated, same entry+exit)")
    w("-" * 92)
    w("  b1) Full-system P&L with the gate ON vs OFF (the gate's net effect on the live bot):")
    w(_stat_line("GATED (gate ON)", r.g_full))
    w(f"      per-year: {_years_str(r.g_full)}")
    w(_stat_line("UNGATED (gate OFF)", r.u_full))
    w(f"      per-year: {_years_str(r.u_full)}")
    d_net = r.g_full.total_pnl_usd - r.u_full.total_pnl_usd
    w(
        f"      delta(net) gated-ungated = ${d_net:,.0f}  "
        f"(positive => the gate REMOVED net-losing trades)"
    )
    w("")
    w("  b2) UNGATED trades partitioned by the REAL regime state at entry (the live")
    w("      _regime_ok on the same 7000-bar tail) — isolates entry-edge from gate-edge:")
    w(_stat_line("above-trend entries", r.above))
    w(f"      per-year: {_years_str(r.above)}")
    w(_stat_line("below-trend entries", r.below))
    w(f"      per-year: {_years_str(r.below)}")
    w("")
    # interpretive read of (b)
    above_pos = r.above.total_pnl_usd > 0
    below_neg = r.below.total_pnl_usd < 0
    if not above_pos and not below_neg:
        read = (
            "NEITHER: the entry has no edge above-trend AND the gate isn't removing a "
            "clear loser — the signal itself doesn't pay after costs."
        )
    elif above_pos and below_neg:
        read = (
            "THE GATE: above-trend entries are net-positive while below-trend entries are "
            "net-negative — the gate adds value by removing losers; but check whether the "
            "above-trend cohort clears the BAR (claim a), not just zero."
        )
    elif above_pos and not below_neg:
        read = (
            "THE ENTRY (partly): above-trend entries carry the P&L; the gate's removed "
            "cohort isn't strongly negative, so the gate trims variance more than loss."
        )
    else:
        read = "MIXED: below-trend loses but above-trend doesn't clearly pay either."
    w(f"  ==> CLAIM (b) READ: {read}")
    w("")
    w("=" * 92)
    w("BLUNT VERDICT")
    w("=" * 92)
    if cleared_a:
        w("TF's gated strategy CLEARS the same pre-registered bar everything else was held to.")
    else:
        w("TF's gated strategy DOES NOT clear the pre-registered bar (PF>=1.30 / exp>=$5 / n>=200")
        w("/ each-year+ / holdout-degradation / deflation) — the SAME bar that returned NONE for")
        w("every Phase-15/16/17 candidate. By its own honest protocol, TF's foundational strategy")
        w("has NO demonstrated edge over the 27-month tape. That is the finding, however")
        w("uncomfortable: the gate correctly removes below-trend losers, but the surviving")
        w("above-trend entry does not, on this evidence, pay enough to clear the bar after real")
        w("costs. (Forward edge in the live regime is the one thing no backtest can settle — but")
        w("nothing here earns the benefit of the doubt that everything else was denied.)")
    w("=" * 92)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-roll", action="store_true", help="skip Panama roll-adjust")
    ap.add_argument(
        "--prior-trials",
        type=int,
        default=27,
        help="cumulative strategy trials from earlier phases (deflation)",
    )
    ap.add_argument("--out", default=None, help="write report to this path")
    args = ap.parse_args()

    r = run_audit(roll=not args.no_roll, prior_trials=args.prior_trials)
    report = format_report(r, prior_trials=args.prior_trials)
    print(report)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(report + "\n")
        print(f"\n[written] {args.out}")


if __name__ == "__main__":
    main()
