"""Phase-17 STAGE-2 — multi-market daily-trend STRATEGY (Carver EWMAC, vol-targeted).

    python -m tools.eval.phase17.run_strategy [--prior-trials 24] [--out PATH]

Anti-overfit protocol: search the 3 well-separated EWMAC speeds on TRAIN ONLY, pick the
train champion by PF, run that champion ONCE on the holdout for the portfolio, deflate the
Sharpe over the CUMULATIVE campaign trial count. Reports per-market AND portfolio, the
traded-period correlation matrix, the 10Y sign-convention proof, and a cost sensitivity.

OFFLINE research, READ-ONLY (reads the Stage-1 CSVs), drives NO prod path. NONE remains a
valid verdict — do not tune to force a pass.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import gates17
from .costs17 import CostModel
from .dataset import HOLDOUT_END, HOLDOUT_START, TRAIN_END, build_panel
from .markets import core
from .portfolio import (
    DailyStats,
    PortfolioRun,
    bucket_weights,
    build_forecasts,
    daily_stats,
    run_portfolio,
)
from .signal import SPEEDS, VOL_SPAN, Speed, estimate_forecast_scalar

# Cumulative prior campaign trials (Phase-16 Batch-1 = 22 strategy trials; Phase-15 = 2
# dormant-window candidates). The conservative (higher) base for the deflation.
DEFAULT_PRIOR_TRIALS = 24
WITHIN_FAMILY_RHO = 0.55  # assumed ~corr among adjacent EWMAC speeds (reported vs empirical)


def _pf(x: float) -> str:
    return "inf" if not math.isfinite(x) else f"{x:.3f}"


@dataclass
class SpeedTrial:
    speed: Speed
    scalar: float
    idm: float
    run: PortfolioRun
    train_stats: DailyStats
    holdout_stats: DailyStats
    train_gate: gates17.GateResult


def _train_masks(panel):
    idx = panel.common_index
    train = pd.Series(panel.split_mask("train"), index=idx)
    hold = pd.Series(panel.split_mask("holdout"), index=idx)
    return train, hold


def _fit_and_run(panel, speed: Speed, cost: CostModel, weights, train_mask):
    """Fit the forecast scalar + IDM on TRAIN, run the portfolio over the full window."""
    norm_by_sym, _ = build_forecasts(panel, speed)
    tmask_by_sym = dict.fromkeys(panel.symbols, train_mask)
    scalar = estimate_forecast_scalar(norm_by_sym, tmask_by_sym)
    from .portfolio import _idm

    idm = _idm(panel, weights, train_mask)
    run = run_portfolio(panel, speed, cost=cost, scalar=scalar, idm=idm, weights=weights)
    return scalar, idm, run


def run(prior_trials: int = DEFAULT_PRIOR_TRIALS, slip_ticks: float = 1.0):
    markets = core()
    panel = build_panel(markets)
    weights = bucket_weights(markets)
    cost = CostModel(slip_ticks=slip_ticks)
    train_mask, hold_mask = _train_masks(panel)

    trials: list[SpeedTrial] = []
    for speed in SPEEDS:
        scalar, idm, run_ = _fit_and_run(panel, speed, cost, weights, train_mask)
        tr = daily_stats(run_.net_daily[train_mask.values])
        ho = daily_stats(run_.net_daily[hold_mask.values])
        tg = gates17.check_train(tr)
        trials.append(SpeedTrial(speed, scalar, idm, run_, tr, ho, tg))

    # --- deflation inputs: cross-trial variance of TRAIN per-day Sharpe; empirical family rho
    sr_pool = [t.train_stats.sr_per_day for t in trials]
    sr_variance = float(np.var(sr_pool, ddof=1)) if len(sr_pool) >= 2 else 0.0
    train_nets = pd.DataFrame({t.speed.label: t.run.net_daily[train_mask.values] for t in trials})
    cc = train_nets.corr().to_numpy()
    iu = np.triu_indices_from(cc, k=1)
    empirical_rho = float(np.nanmean(cc[iu])) if cc.size > 1 else float("nan")

    n_naive = prior_trials + len(SPEEDS)
    rho_for_eff = empirical_rho if not math.isnan(empirical_rho) else WITHIN_FAMILY_RHO
    n_eff = gates17.effective_trials(n_naive, len(SPEEDS), rho_for_eff)

    # --- champion = highest TRAIN PF (finite), then holdout touched ONCE
    def keypf(t: SpeedTrial) -> float:
        pf = t.train_stats.profit_factor
        return pf if math.isfinite(pf) else 999.0

    champ = max(trials, key=keypf)
    defl = gates17.deflate(champ.train_stats, n_naive, n_eff, sr_variance)
    hg = gates17.check_holdout(champ.train_stats, champ.holdout_stats, defl)
    cleared = champ.train_gate.passed and hg.passed

    summary = {
        "panel": panel,
        "weights": weights,
        "trials": trials,
        "champion": champ,
        "deflation": defl,
        "holdout_gate": hg,
        "cleared": cleared,
        "n_naive": n_naive,
        "n_eff": n_eff,
        "empirical_rho": empirical_rho,
        "rho_for_eff": rho_for_eff,
        "sr_variance": sr_variance,
        "prior_trials": prior_trials,
        "slip_ticks": slip_ticks,
        "train_mask": train_mask,
        "hold_mask": hold_mask,
        "cost": cost,
    }
    return summary


# --------------------------------------------------------------------------- #
# Per-market + correlation + 10Y proof
# --------------------------------------------------------------------------- #
def per_market_table(champ: SpeedTrial, mask: pd.Series, markets) -> list[str]:
    lines = [
        f"{'sym':<5} {'bucket':<7} {'wt':>6} {'net$':>10} {'PF':>6} "
        f"{'Sharpe':>7} {'avg|pos|':>9} {'turns':>8}"
    ]
    for m in markets:
        sym = m.symbol
        net = champ.run.per_market_net[sym][mask.values]
        pos = champ.run.per_market_pos[sym][mask.values]
        to = champ.run.per_market_turnover[sym][mask.values]
        s = daily_stats(net)
        lines.append(
            f"{sym:<5} {m.bucket:<7} {champ.run.weights[sym]:>6.4f} {s.total_pnl_usd:>10,.0f} "
            f"{_pf(s.profit_factor):>6} {s.sharpe_ann:>7.2f} {pos.abs().mean():>9.2f} "
            f"{to.sum():>8.1f}"
        )
    return lines


def corr_block(panel, mask: pd.Series) -> list[str]:
    dchg = panel.price.diff().loc[mask.values]
    corr = dchg.corr()
    syms = list(corr.columns)
    out = ["        " + " ".join(f"{s:>6}" for s in syms)]
    for s in syms:
        row = " ".join(f"{corr.loc[s, c]:>6.2f}" for c in syms)
        out.append(f"{s:>6}  {row}")
    iu = np.triu_indices_from(corr.to_numpy(), k=1)
    mean_abs = float(np.nanmean(np.abs(corr.to_numpy()[iu])))
    out.append(f"mean |pairwise corr| = {mean_abs:.3f}")
    return out


def ten_y_sign_proof(champ: SpeedTrial, panel) -> list[str]:
    """Prove the 10Y sign convention directly on the champion's own forecast: the signal is
    built on the NEGATED yield (``price = -yield``), so a long position (>0) must coincide
    with a FALLING yield (rising bond) and a short (<0) with a rising yield. We read the
    champion position at its most-long-bond and most-short-bond dates and show the trailing
    yield trend (over the slow span) has the matching sign. This isolates the CONVENTION
    from the slow signal's lag. ``trend_win`` = the slow span so the trend window matches
    the signal's own memory."""
    yld = panel.series["10Y"].df.set_index("time")["close"].reindex(panel.common_index)
    pos = champ.run.per_market_pos["10Y"]
    trend_win = champ.speed.slow
    yld_trend = yld - yld.shift(trend_win)  # <0 where yield FELL over the slow span
    warm = pos[pos.abs() > 1e-9]
    if warm.empty:
        return [
            "10Y sign-convention proof: champion signal never warm on 10Y in-window (256d burn-in)"
        ]
    out = [
        f"10Y sign-convention proof (price = -yield; champion {champ.speed.label}, "
        f"trailing trend over the {trend_win}d slow span):"
    ]
    for tag, when in (("most LONG-bond", warm.idxmax()), ("most SHORT-bond", warm.idxmin())):
        p = float(pos.loc[when])
        dy = float(yld_trend.loc[when]) if pd.notna(yld_trend.loc[when]) else float("nan")
        side = "LONG bond" if p > 0 else "SHORT bond"
        ytrend = "FALLING (bond up)" if dy < 0 else "RISING (bond down)"
        ok = (p > 0 and dy < 0) or (p < 0 and dy > 0)
        out.append(
            f"  {tag} {when.date()}: pos {p:+.3f} ({side}) | trailing yield {dy:+.3f}% "
            f"({ytrend}) -> {'CONVENTION OK' if ok else 'CHECK SIGN'}"
        )
    return out


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def format_report(summary) -> str:
    panel = summary["panel"]
    trials = summary["trials"]
    champ = summary["champion"]
    defl = summary["deflation"]
    hg = summary["holdout_gate"]
    markets = core()
    train_mask, hold_mask = summary["train_mask"], summary["hold_mask"]
    lines: list[str] = []
    w = lines.append

    w("=" * 92)
    w("PHASE-17 STAGE-2 — MULTI-MARKET DAILY TREND (Carver EWMAC, vol-targeted)")
    w("the breadth thesis: ONE slow trend signal across a diversified micro basket")
    w("=" * 92)
    w("")
    w("PRE-REGISTERED GATES (committed in gates17.py BEFORE results):")
    w(f"  TRAIN  : portfolio PF >= {gates17.PF_MIN} net, profitable EACH train year")
    w(
        f"  HOLDOUT: portfolio PF >= {gates17.PF_MIN} net, profitable EACH holdout year, "
        f"PF >= {1 - gates17.HOLDOUT_PF_DEGRADE_MAX:.2f} x train PF"
    )
    w("  DEFLATE: net per-day Sharpe SURVIVES the haircut (SR_hat > E[max Sharpe | N trials])")
    w("")
    n_common = len(panel.common_index)
    n_tr = int(train_mask.sum())
    n_ho = int(hold_mask.sum())
    w(
        f"COMMON WINDOW: {panel.common_index[0].date()} .. {panel.common_index[-1].date()} "
        f"= {n_common} bars | markets: {len(markets)} | speeds: {[s.label for s in SPEEDS]}"
    )
    w(
        f"SPLIT (once, campaign boundary {TRAIN_END}): TRAIN [.. {TRAIN_END}) = {n_tr} bars | "
        f"HOLDOUT [{HOLDOUT_START} .. {HOLDOUT_END}) = {n_ho} bars"
    )
    w(
        f"Signal: EWMA(fast)-EWMA(slow) on Panama series / EWMA price-vol (span {VOL_SPAN}); "
        f"forecast scalar -> mean|f|~10, cap +/-20. Vol-targeted, risk-parity-by-bucket."
    )
    w(
        f"Costs: per-contract per-side commission (to-verify, MBT richer) + "
        f"{summary['slip_ticks']:.0f}-tick slippage, close-to-close turnover."
    )
    w(
        f"Trials (cumulative): prior {summary['prior_trials']} + Stage-2 {len(SPEEDS)} = "
        f"NAIVE {summary['n_naive']} | EFFECTIVE {summary['n_eff']:.2f} "
        f"(EWMAC rho: emp {summary['empirical_rho']:.2f}, assumed {WITHIN_FAMILY_RHO}). "
        f"Deflate vs NAIVE (conservative)."
    )
    sr0 = gates17.expected_max_sharpe(summary["n_naive"], summary["sr_variance"])
    w(
        f"E[max per-day Sharpe] under null across {summary['n_naive']} trials = {sr0:.4f} "
        f"(SR var {summary['sr_variance']:.6f}) — the inflated benchmark the winner must beat."
    )
    w("")

    # --- the 3 speeds on TRAIN (the search) ---
    w("EWMAC SPEED SEARCH — TRAIN ONLY (champion by train PF):")
    w("-" * 92)
    w(
        f"  {'speed':<8} {'scalar':>8} {'IDM':>5} {'net$':>10} {'PF':>7} {'Sharpe':>7} "
        f"{'SR/day':>8} {'yrs':>10} {'GATE':>6}"
    )
    for t in trials:
        s = t.train_stats
        yrs = "/".join(f"{'+' if v > 0 else '-'}" for _, v in sorted(s.per_year.items()))
        star = " *" if t is champ else ""
        w(
            f"  {t.speed.label:<8} {t.scalar:>8.4f} {t.idm:>5.2f} {s.total_pnl_usd:>10,.0f} "
            f"{_pf(s.profit_factor):>7} {s.sharpe_ann:>7.2f} {s.sr_per_day:>8.4f} {yrs:>10} "
            f"{'PASS' if t.train_gate.passed else 'FAIL':>6}{star}"
        )
    w("-" * 92)
    for r in champ.train_gate.reasons:
        w(f"    train-gate[{champ.speed.label}]: {r}")
    w("")

    # --- champion holdout (touched once) + deflation ---
    w(f"CHAMPION {champ.speed.label} -> HOLDOUT (touched once) + deflation:")
    w("-" * 92)
    tr, ho = champ.train_stats, champ.holdout_stats
    w(
        f"  TRAIN  : PF {_pf(tr.profit_factor)} Sharpe {tr.sharpe_ann:.2f} "
        f"net ${tr.total_pnl_usd:,.0f} maxDD ${tr.max_drawdown_usd:,.0f} years {tr.per_year}"
    )
    w(
        f"  HOLDOUT: PF {_pf(ho.profit_factor)} Sharpe {ho.sharpe_ann:.2f} "
        f"net ${ho.total_pnl_usd:,.0f} maxDD ${ho.max_drawdown_usd:,.0f} years {ho.per_year}"
    )
    w(
        f"  DEFLATE: SR/day {defl.sr_hat:.4f} vs SR0 {defl.sr0:.4f} "
        f"-> haircut {defl.haircut_sharpe:+.4f} ({'SURVIVES' if defl.survives else 'KILLED'}) "
        f"| DSR {defl.dsr:.3f} | Bonferroni p {defl.bonferroni_p:.3g}"
    )
    for r in hg.reasons:
        w(f"    holdout-gate: {r}")
    w("")

    # --- per-market (champion, full common window) ---
    w(f"PER-MARKET (champion {champ.speed.label}, full common window):")
    w("-" * 92)
    full_mask = pd.Series(True, index=panel.common_index)
    for ln in per_market_table(champ, full_mask, markets):
        w("  " + ln)
    w("")

    # --- correlation on the traded period ---
    w("PER-MARKET RETURN CORRELATION (traded common window, daily price changes):")
    for ln in corr_block(panel, full_mask):
        w("  " + ln)
    w("")

    # --- 10Y sign proof ---
    for ln in ten_y_sign_proof(champ, panel):
        w("  " + ln)
    w("")

    # --- cost sensitivity on the champion ---
    w("COST SENSITIVITY (champion, re-fit nothing — same scalar/IDM, vary slippage ticks):")
    for st in (0.0, 1.0, 2.0):
        c2 = summary["cost"].with_slip(st)
        run2 = run_portfolio(
            panel,
            champ.speed,
            cost=c2,
            scalar=champ.scalar,
            idm=champ.idm,
            weights=champ.run.weights,
        )
        t2 = daily_stats(run2.net_daily[train_mask.values])
        h2 = daily_stats(run2.net_daily[hold_mask.values])
        w(
            f"  {int(st)}-tick: train PF {_pf(t2.profit_factor)} (${t2.total_pnl_usd:,.0f}) | "
            f"holdout PF {_pf(h2.profit_factor)} (${h2.total_pnl_usd:,.0f})"
        )
    w("")

    # --- verdict ---
    w("=" * 92)
    if summary["cleared"]:
        w(
            f"VERDICT: CANDIDATE FOR FORWARD PAPER-PROOF. The {champ.speed.label} portfolio clears "
            f"train + holdout + deflation under the pre-registered gates."
        )
        w(
            "This is DIRECTIONAL evidence over a single ~25-month regime + low trade count — NOT a "
            "decades-validated edge, and NOT 'deploy'. Next step is a forward paper-proof."
        )
    else:
        w("VERDICT: NONE. The portfolio does not clear train + holdout + deflation.")
        w(
            "Honest finding (Phase-15/16 precedent), not a failure to search. "
            "Not tuned to force a pass."
        )
    w("=" * 92)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prior-trials", type=int, default=DEFAULT_PRIOR_TRIALS)
    ap.add_argument("--slip-ticks", type=float, default=1.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    summary = run(prior_trials=args.prior_trials, slip_ticks=args.slip_ticks)
    report = format_report(summary)
    print(report)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(report + "\n")
        print(f"\n[written] {args.out}")


if __name__ == "__main__":
    main()
