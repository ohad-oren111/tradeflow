"""PRE-REGISTERED promotion gates — committed here BEFORE any result is read
(anti-overfit protocol step 2). Changing a threshold to fit a result is the failure
mode; these constants are the contract.

A family CLEARS the campaign only if ALL hold:
  TRAIN (search set, 2024-03..2025-08):
    * PF        >= 1.30  (net, after costs)
    * exp/ct    >= $5.00
    * n_trades  >= 200
    * profitable EACH train calendar year
  HOLDOUT (2025-09..2026-06, touched ONCE per train-survivor):
    * PF        >= 1.30
    * exp/ct    >= $5.00
    * profitable EACH holdout calendar year
    * PF within 25% of train  (holdout_PF >= 0.75 * train_PF — degradation cap)
  DEFLATION (multiple-testing, across ALL trials):
    * Deflated Sharpe Ratio >= 0.95
"""

from __future__ import annotations

from dataclasses import dataclass

from .metrics import Stats

PF_MIN = 1.30
EXP_PER_CT_MIN = 5.00
N_TRADES_MIN = 200
HOLDOUT_PF_DEGRADE_MAX = 0.25  # holdout_PF >= (1-this) * train_PF
DSR_MIN = 0.95


@dataclass
class GateResult:
    passed: bool
    reasons: list  # human-readable per-check verdicts


def _years_all_positive(per_year: dict) -> tuple[bool, list]:
    if not per_year:
        return False, ["no-years"]
    notes = [f"{y}:{'+' if v > 0 else '-'}{v:,.0f}" for y, v in sorted(per_year.items())]
    return all(v > 0 for v in per_year.values()), notes


def check_train(s: Stats) -> GateResult:
    """Structural gates on the TRAIN champion."""
    reasons, ok = [], True
    c = s.profit_factor >= PF_MIN
    ok &= c
    reasons.append(f"PF {s.profit_factor:.3f} {'>=' if c else '<'} {PF_MIN}")
    c = s.expectancy_usd >= EXP_PER_CT_MIN
    ok &= c
    reasons.append(f"exp/ct ${s.expectancy_usd:.2f} {'>=' if c else '<'} ${EXP_PER_CT_MIN}")
    c = s.n_trades >= N_TRADES_MIN
    ok &= c
    reasons.append(f"n {s.n_trades} {'>=' if c else '<'} {N_TRADES_MIN}")
    yr_ok, yr_notes = _years_all_positive(s.per_year)
    ok &= yr_ok
    reasons.append(f"each-year {'OK' if yr_ok else 'FAIL'} [{' '.join(yr_notes)}]")
    return GateResult(ok, reasons)


def check_holdout(train: Stats, hold: Stats, dsr: float) -> GateResult:
    """Holdout gates (degradation-capped vs train) + the deflation gate."""
    reasons, ok = [], True
    c = hold.profit_factor >= PF_MIN
    ok &= c
    reasons.append(f"HO PF {hold.profit_factor:.3f} {'>=' if c else '<'} {PF_MIN}")
    c = hold.expectancy_usd >= EXP_PER_CT_MIN
    ok &= c
    reasons.append(f"HO exp/ct ${hold.expectancy_usd:.2f} {'>=' if c else '<'} ${EXP_PER_CT_MIN}")
    yr_ok, yr_notes = _years_all_positive(hold.per_year)
    ok &= yr_ok
    reasons.append(f"HO each-year {'OK' if yr_ok else 'FAIL'} [{' '.join(yr_notes)}]")
    floor = (1.0 - HOLDOUT_PF_DEGRADE_MAX) * train.profit_factor
    c = hold.profit_factor >= floor
    ok &= c
    reasons.append(f"HO PF {hold.profit_factor:.3f} {'>=' if c else '<'} 0.75*train {floor:.3f}")
    c = dsr >= DSR_MIN
    ok &= c
    reasons.append(f"DSR {dsr:.3f} {'>=' if c else '<'} {DSR_MIN}")
    return GateResult(ok, reasons)
