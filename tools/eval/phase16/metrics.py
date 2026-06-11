"""Net performance metrics + the multiple-testing deflators.

All metrics are on NET (after-cost) $ at 1 contract, so expectancy IS $/contract.

The headline anti-overfit instrument is the **Deflated Sharpe Ratio** (Bailey &
Lopez de Prado, "The Deflated Sharpe Ratio", 2014; AFML 2018 Ch.8 — to-verify-against-
source): after N independent trials the expected MAXIMUM Sharpe under the null is not
zero, so the winner's Sharpe must beat that inflated benchmark. We also report a
Bonferroni-haircut p-value (Harvey & Liu, "Backtesting", 2015 — to-verify) as a
cross-check. Pure stdlib: normal CDF via ``math.erf``; inverse via Acklam.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TRADING_DAYS = 252
_EULER = 0.5772156649015329  # Euler-Mascheroni gamma


# --------------------------------------------------------------------------- #
# Standard normal CDF / inverse (no scipy)
# --------------------------------------------------------------------------- #
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, ~1e-9)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    cc = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((cc[0] * q + cc[1]) * q + cc[2]) * q + cc[3]) * q + cc[4]) * q + cc[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((cc[0] * q + cc[1]) * q + cc[2]) * q + cc[3]) * q + cc[4]) * q + cc[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


# --------------------------------------------------------------------------- #
# Trade-ledger metrics
# --------------------------------------------------------------------------- #
@dataclass
class Stats:
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy_usd: float  # = $/contract (engine runs 1 ct)
    total_pnl_usd: float
    sharpe_ann: float  # annualized from daily $, for display only
    sr_per_trade: float  # per-trade Sharpe, the DSR input
    skew: float
    kurtosis: float  # full (non-excess)
    max_drawdown_usd: float
    per_year: dict = field(default_factory=dict)  # year -> net $
    per_year_trades: dict = field(default_factory=dict)


def _max_dd(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    return float((equity - np.maximum.accumulate(equity)).min())


def compute_stats(trades: pd.DataFrame) -> Stats:
    if trades is None or len(trades) == 0:
        return Stats(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, {}, {})
    pnl = trades["pnl_usd"].astype(float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gp, gl = float(wins.sum()), float(-losses.sum())
    pf = gp / gl if gl > 0 else (math.inf if gp > 0 else 0.0)

    sd = pnl.std(ddof=1)
    sr_pt = float(pnl.mean() / sd) if sd > 0 and len(pnl) > 1 else 0.0
    sk = float(pnl.skew()) if len(pnl) > 2 else 0.0
    ku = float(pnl.kurtosis()) + 3.0 if len(pnl) > 3 else 3.0  # pandas gives EXCESS; make full

    t = trades.copy()
    t["day"] = pd.to_datetime(t["exit_time"]).dt.normalize()
    daily = t.groupby("day")["pnl_usd"].sum()
    dsd = daily.std(ddof=1)
    sharpe_ann = (
        float(daily.mean() / dsd * math.sqrt(TRADING_DAYS)) if dsd > 0 and len(daily) > 1 else 0.0
    )

    per_year = t.groupby("year")["pnl_usd"].sum().round(2).to_dict()
    per_year_n = t.groupby("year")["pnl_usd"].size().to_dict()

    return Stats(
        n_trades=int(len(pnl)),
        win_rate=float((pnl > 0).mean()),
        profit_factor=float(pf),
        expectancy_usd=float(pnl.mean()),
        total_pnl_usd=float(pnl.sum()),
        sharpe_ann=sharpe_ann,
        sr_per_trade=sr_pt,
        skew=sk,
        kurtosis=ku,
        max_drawdown_usd=_max_dd(pnl.cumsum().to_numpy()),
        per_year={int(k): float(v) for k, v in per_year.items()},
        per_year_trades={int(k): int(v) for k, v in per_year_n.items()},
    )


# --------------------------------------------------------------------------- #
# Multiple-testing deflators
# --------------------------------------------------------------------------- #
def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max Sharpe] under the null across ``n_trials`` independent strategies, per
    Bailey & Lopez de Prado (2014) eq. for the expected maximum of N Gaussians:

        E[max] ~ sqrt(V) * [ (1-g)*Z^-1(1 - 1/N) + g*Z^-1(1 - 1/(N*e)) ]

    with V = cross-trial variance of the Sharpe estimates and g = Euler-Mascheroni.
    All Sharpes here are PER-TRADE (same frequency as ``sr_per_trade``).
    """
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    z1 = norm_ppf(1.0 - 1.0 / n_trials)
    z2 = norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sr_variance) * ((1 - _EULER) * z1 + _EULER * z2)


def deflated_sharpe(
    sr_hat: float,
    n_trades: int,
    skew: float,
    kurtosis: float,
    n_trials: int,
    sr_variance: float,
) -> tuple[float, float]:
    """Return (DSR, SR0) where DSR = P(true Sharpe > 0 | N trials), SR0 = E[max Sharpe].

    DSR = Z( (SR_hat - SR0) * sqrt(T-1) / sqrt(1 - skew*SR_hat + (kurt-1)/4 * SR_hat^2) )
    (Bailey & Lopez de Prado 2014; AFML Ch.8 — to-verify-against-source).
    """
    if n_trades < 4:
        return 0.0, 0.0
    sr0 = expected_max_sharpe(n_trials, sr_variance)
    denom = 1.0 - skew * sr_hat + (kurtosis - 1.0) / 4.0 * sr_hat * sr_hat
    if denom <= 0:
        return 0.0, sr0
    z = (sr_hat - sr0) * math.sqrt(n_trades - 1) / math.sqrt(denom)
    return norm_cdf(z), sr0


def bonferroni_haircut(sr_hat: float, n_trades: int, n_trials: int) -> tuple[float, float]:
    """Cross-check: single-test p-value of the per-trade Sharpe (t = SR*sqrt(T)) and its
    Bonferroni-adjusted p across ``n_trials`` (Harvey & Liu 2015 — to-verify)."""
    if n_trades < 2:
        return 1.0, 1.0
    t = sr_hat * math.sqrt(n_trades)
    p = 2.0 * (1.0 - norm_cdf(abs(t)))  # two-sided
    return p, min(1.0, p * n_trials)
