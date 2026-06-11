"""The anti-overfit protocol driver.

For each family: run every train variant, pick the train champion (highest train PF
among variants with enough trades), check the pre-registered TRAIN gates. Only
train-survivors get their champion run ONCE on the holdout. The Deflated Sharpe is
computed against the FULL trial count (every variant of every family = one trial), so
a win after N trials clears a higher bar than after 3 (AFML Ch.8).

Pure-research: imports only the Phase-16 engine/families (no prod entry/exit path).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .costs import CostModel, Slippage
from .engine import ExitSpec, run_backtest
from .families import Family, build_registry
from .gates import check_holdout, check_train
from .metrics import Stats, bonferroni_haircut, compute_stats, deflated_sharpe

_SEARCH_MIN_TRADES = 50  # a variant needs at least this many train trades to be champion


@dataclass
class VariantRun:
    family_key: str
    label: str
    spec: ExitSpec
    train: Stats


@dataclass
class FamilyResult:
    family: Family
    champion_label: str
    champion_spec: ExitSpec
    champion_pos_train: np.ndarray
    train: Stats
    train_gate_pass: bool
    train_gate_reasons: list
    all_train: list  # list[VariantRun] for this family (full grid)
    # holdout fields (only for train-survivors)
    holdout: Stats | None = None
    dsr: float = 0.0
    sr0: float = 0.0
    haircut_p: float = 1.0
    holdout_gate_pass: bool = False
    holdout_gate_reasons: list = field(default_factory=list)
    cost_sensitivity: dict = field(default_factory=dict)  # ticks -> (train_PF, holdout_PF)
    cleared: bool = False


def _pf(s: Stats) -> float:
    return s.profit_factor if math.isfinite(s.profit_factor) else 999.0


def _pick_champion(runs: list[VariantRun]) -> VariantRun:
    eligible = [r for r in runs if r.train.n_trades >= _SEARCH_MIN_TRADES]
    pool = eligible or runs
    return max(pool, key=lambda r: _pf(r.train))


def run_batch(
    dataset,
    *,
    costs: CostModel | None = None,
    slip: Slippage | None = None,
    registry: list[Family] | None = None,
    prior_trials: int = 0,
) -> tuple[list[FamilyResult], dict]:
    """Run the full protocol; return (per-family results, summary dict).

    ``prior_trials`` = the number of strategy trials run in EARLIER batches of this
    multi-session campaign. The deflation must account for the CUMULATIVE trial count
    (this batch + all prior), or the multiple-testing correction understates — a win in
    batch 3 must clear the bar implied by every strategy tried in batches 1-3.
    """
    costs = costs or CostModel()
    slip = slip or Slippage()
    registry = registry or build_registry()
    train_df = dataset.train()
    hold_df = dataset.holdout()

    # --- pass 1: every variant of every family on TRAIN (collect trials) ---
    fam_runs: dict[str, list[VariantRun]] = {}
    fam_pos: dict[str, dict] = {}
    for fam in registry:
        variants = fam.build(train_df)
        runs = []
        fam_pos[fam.key] = {}
        for label, pos, spec in variants:
            trades = run_backtest(train_df, pos, spec, costs=costs, slip=slip, contracts=1)
            runs.append(VariantRun(fam.key, label, spec, compute_stats(trades)))
            fam_pos[fam.key][label] = (pos, spec)
        fam_runs[fam.key] = runs

    # --- trial accounting for deflation: EVERY variant run is a trial (+ prior batches) ---
    all_runs = [r for runs in fam_runs.values() for r in runs]
    n_trials = len(all_runs) + prior_trials
    sr_pool = [r.train.sr_per_trade for r in all_runs if r.train.n_trades >= 2]
    sr_variance = float(np.var(sr_pool, ddof=1)) if len(sr_pool) >= 2 else 0.0

    # --- pass 2: champions, train gates, then holdout (once) for survivors ---
    results: list[FamilyResult] = []
    for fam in registry:
        runs = fam_runs[fam.key]
        champ = _pick_champion(runs)
        tg = check_train(champ.train)
        pos_tr, spec = fam_pos[fam.key][champ.label]
        fr = FamilyResult(
            family=fam,
            champion_label=champ.label,
            champion_spec=spec,
            champion_pos_train=pos_tr,
            train=champ.train,
            train_gate_pass=tg.passed,
            train_gate_reasons=tg.reasons,
            all_train=runs,
        )
        if tg.passed:
            # rebuild the champion's signal on the HOLDOUT slice and run ONCE
            ho_variants = {lbl: (p, sp) for lbl, p, sp in fam.build(hold_df)}
            pos_ho, sp_ho = ho_variants[champ.label]
            ho_trades = run_backtest(hold_df, pos_ho, sp_ho, costs=costs, slip=slip, contracts=1)
            fr.holdout = compute_stats(ho_trades)
            fr.dsr, fr.sr0 = deflated_sharpe(
                champ.train.sr_per_trade,
                champ.train.n_trades,
                champ.train.skew,
                champ.train.kurtosis,
                n_trials,
                sr_variance,
            )
            _, fr.haircut_p = bonferroni_haircut(
                champ.train.sr_per_trade, champ.train.n_trades, n_trials
            )
            hg = check_holdout(champ.train, fr.holdout, fr.dsr)
            fr.holdout_gate_pass = hg.passed
            fr.holdout_gate_reasons = hg.reasons
            fr.cleared = hg.passed
            # cost sensitivity on the champion (0/1/2-tick uniform slippage)
            for t in (0.0, 1.0, 2.0):
                su = Slippage.uniform(t)
                tr_t = run_backtest(train_df, pos_tr, spec, costs=costs, slip=su, contracts=1)
                ho_t = run_backtest(hold_df, pos_ho, sp_ho, costs=costs, slip=su, contracts=1)
                fr.cost_sensitivity[t] = (
                    compute_stats(tr_t).profit_factor,
                    compute_stats(ho_t).profit_factor,
                )
        results.append(fr)

    summary = {
        "n_families": len(registry),
        "n_trials": n_trials,
        "n_trials_this_batch": len(all_runs),
        "prior_trials": prior_trials,
        "sr_variance": sr_variance,
        "train_survivors": [r.family.key for r in results if r.train_gate_pass],
        "cleared": [r.family.key for r in results if r.cleared],
        "train_rows": len(train_df),
        "holdout_rows": len(hold_df),
        "roll_boundaries": dataset.roll_boundaries,
    }
    return results, summary
