"""PRE-REGISTERED Stage-2 promotion gates — committed BEFORE any portfolio result is read.

The portfolio (vol-weighted sum across the 9 markets) CLEARS only if ALL hold. Changing a
threshold to fit a result is the failure mode; these constants are the contract. (NONE
remains a valid verdict — Phase-15/16 precedent.)

  TRAIN (search set, ~2024-05 .. 2025-08):
    * portfolio PF >= 1.30  (net, after costs, on daily $)
    * profitable EACH train calendar year
  HOLDOUT (~2025-09 .. 2026-06, touched ONCE for the portfolio):
    * portfolio PF >= 1.30  (net)
    * profitable EACH holdout calendar year
    * holdout PF within 25% of train  (holdout_PF >= 0.75 * train_PF)
  DEFLATION (multiple-testing, CUMULATIVE trials across the whole campaign):
    * net per-day Sharpe SURVIVES the haircut: SR_hat > SR0 = E[max Sharpe | N trials]
      (Bailey & Lopez de Prado deflation). Reported alongside the Deflated Sharpe Ratio
      and a Bonferroni-haircut p as cross-checks.

Deflation uses the CUMULATIVE campaign trial count (Phase-16 = 22, Phase-15 = 2, this
Stage-2 = the speeds searched). We report BOTH the naive cumulative count and an
effective-trials estimate (correlation-adjusted for the ~0.55 within-EWMAC-family
correlation) and DEFLATE AGAINST THE CONSERVATIVE (HIGHER) one — the naive count.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.eval.phase16.metrics import (
    bonferroni_haircut,
    deflated_sharpe,
    expected_max_sharpe,
)

from .portfolio import DailyStats

PF_MIN = 1.30
HOLDOUT_PF_DEGRADE_MAX = 0.25  # holdout_PF >= (1 - this) * train_PF


@dataclass
class GateResult:
    passed: bool
    reasons: list


def _years_all_positive(per_year: dict) -> tuple[bool, list]:
    if not per_year:
        return False, ["no-years"]
    notes = [f"{y}:{'+' if v > 0 else '-'}{abs(v):,.0f}" for y, v in sorted(per_year.items())]
    return all(v > 0 for v in per_year.values()), notes


def check_train(s: DailyStats) -> GateResult:
    reasons, ok = [], True
    c = s.profit_factor >= PF_MIN
    ok &= c
    reasons.append(f"train PF {s.profit_factor:.3f} {'>=' if c else '<'} {PF_MIN}")
    yr_ok, yr_notes = _years_all_positive(s.per_year)
    ok &= yr_ok
    reasons.append(f"train each-year {'OK' if yr_ok else 'FAIL'} [{' '.join(yr_notes)}]")
    return GateResult(ok, reasons)


def effective_trials(naive: int, n_correlated: int, rho: float) -> float:
    """Effective independent trials when ``n_correlated`` of the trials are an EWMAC speed
    family with mean pairwise correlation ``rho``: collapse that family to
    n_eff = m / (1 + (m-1)*rho), keep the rest as independent."""
    if n_correlated <= 1:
        return float(naive)
    fam_eff = n_correlated / (1.0 + (n_correlated - 1) * rho)
    return float(naive - n_correlated) + fam_eff


@dataclass
class Deflation:
    n_trials_naive: int
    n_trials_effective: float
    sr0: float  # E[max per-day Sharpe] under the null across naive trials
    sr_hat: float
    haircut_sharpe: float  # sr_hat - sr0 (per day); >0 == survives
    dsr: float
    bonferroni_p: float
    survives: bool


def deflate(
    train: DailyStats, n_trials_naive: int, n_trials_eff: float, sr_variance: float
) -> Deflation:
    """Deflate against the CONSERVATIVE (higher = naive) cumulative trial count."""
    dsr, sr0 = deflated_sharpe(
        train.sr_per_day, train.n_days, train.skew, train.kurtosis, n_trials_naive, sr_variance
    )
    _, bonf = bonferroni_haircut(train.sr_per_day, train.n_days, n_trials_naive)
    haircut = train.sr_per_day - sr0
    return Deflation(
        n_trials_naive=n_trials_naive,
        n_trials_effective=n_trials_eff,
        sr0=sr0,
        sr_hat=train.sr_per_day,
        haircut_sharpe=haircut,
        dsr=dsr,
        bonferroni_p=bonf,
        survives=haircut > 0.0,
    )


def check_holdout(train: DailyStats, hold: DailyStats, defl: Deflation) -> GateResult:
    reasons, ok = [], True
    c = hold.profit_factor >= PF_MIN
    ok &= c
    reasons.append(f"HO PF {hold.profit_factor:.3f} {'>=' if c else '<'} {PF_MIN}")
    yr_ok, yr_notes = _years_all_positive(hold.per_year)
    ok &= yr_ok
    reasons.append(f"HO each-year {'OK' if yr_ok else 'FAIL'} [{' '.join(yr_notes)}]")
    floor = (1.0 - HOLDOUT_PF_DEGRADE_MAX) * train.profit_factor
    c = hold.profit_factor >= floor
    ok &= c
    reasons.append(f"HO PF {hold.profit_factor:.3f} {'>=' if c else '<'} 0.75*train {floor:.3f}")
    c = defl.survives
    ok &= c
    reasons.append(
        f"deflation: SR/day {defl.sr_hat:.4f} {'>' if c else '<='} SR0 {defl.sr0:.4f} "
        f"(haircut {defl.haircut_sharpe:+.4f}; DSR {defl.dsr:.3f}; Bonf p {defl.bonferroni_p:.3g})"
    )
    return GateResult(ok, reasons)


__all__ = [
    "PF_MIN",
    "HOLDOUT_PF_DEGRADE_MAX",
    "GateResult",
    "check_train",
    "check_holdout",
    "deflate",
    "Deflation",
    "effective_trials",
    "expected_max_sharpe",
]
