"""Vol-targeted risk-parity portfolio across the micro basket — the breadth mechanism.

Each market is sized so it contributes COMPARABLE risk (Carver's vol targeting), then the
markets are combined with RISK-PARITY-BY-BUCKET weights and a portfolio diversification
multiplier (IDM). The edge — if any — is the diversification, not any single market.

Sizing (Carver), per market m at date t
---------------------------------------
    inst_dollar_vol(t) = price_vol_m(t) * multiplier_m         # $ daily stdev / 1 contract
    base_contracts(t)  = forecast_m(t)/10 * (VOL_TARGET / inst_dollar_vol(t))
    position(t)        = IDM * weight_m * base_contracts(t)     # fractional (research)

* ``weight_m`` = RISK-PARITY BY BUCKET: each of the 6 buckets carries one risk unit, split
  equally within the bucket — ``weight = (1/n_buckets) * (1/bucket_size)``. This is what
  down-weights the correlated clusters the operator asked to treat as ~one bucket: the 3 FX
  micros (one tight USD cluster) together carry ONE bucket's risk (1/18 each), and the 2
  metals together carry one (1/12 each). No contract is dropped.
* ``IDM`` (diversification multiplier) = 1/sqrt(w' C w) on the TRAIN correlation matrix,
  capped at 2.5 (Carver). It scales the WHOLE portfolio up because diversification lowers
  combined vol.

Scale invariance (important for honesty)
----------------------------------------
``VOL_TARGET`` and ``IDM`` are global scalars. Gross daily P&L is LINEAR in position size
and so is the turnover cost (``|Δpos| * cost_per_contract``); therefore NET daily P&L
scales linearly with the global scale, and **PF and Sharpe are invariant to VOL_TARGET and
IDM** even after costs. The verdict rests on the cost-per-contract / dollar-vol-per-contract
friction ratio, the signal, the bucket weights and the correlations — NOT on the chosen
account size. We still compute IDM faithfully and report the resulting average contracts so
the reader can sanity-check realism (and the integer-rounding caveat).

P&L (close-to-close)
--------------------
    gross_m(t) = position_m(t-1) * (price_m(t) - price_m(t-1)) * multiplier_m
    turnover_m(t) = |position_m(t) - position_m(t-1)|
    cost_m(t)  = turnover_m(t) * cost.per_contract_side(m)
    net_m(t)   = gross_m(t) - cost_m(t)
For 10Y, ``price`` is the negated-yield proxy (see dataset.py), so a position>0 is
"long the bond" and its P&L is the bond-proxy P&L.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs17 import CostModel
from .dataset import Panel
from .markets import Market
from .signal import (
    Speed,
    price_vol,
    scaled_forecast,
    vol_normalized_forecast,
)

VOL_TARGET = 100.0  # per-instrument target daily $ vol at full forecast (scale-invariant)
IDM_CAP = 2.5
TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
# Risk-parity-by-bucket weights
# --------------------------------------------------------------------------- #
def bucket_weights(markets: list[Market]) -> dict[str, float]:
    """Each bucket = one risk unit, split equally within the bucket."""
    buckets: dict[str, list[str]] = {}
    for m in markets:
        buckets.setdefault(m.bucket, []).append(m.symbol)
    n_buckets = len(buckets)
    w: dict[str, float] = {}
    for syms in buckets.values():
        for sym in syms:
            w[sym] = (1.0 / n_buckets) * (1.0 / len(syms))
    return w


# --------------------------------------------------------------------------- #
# Per-market forecast + position (vol-targeted)
# --------------------------------------------------------------------------- #
def build_forecasts(
    panel: Panel, speed: Speed
) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    """Compute the vol-normalized forecast and sizing vol per market on the FULL series,
    re-indexed to the common window. Returns ``(norm_by_sym, vol_by_sym)``."""
    norm_by_sym, vol_by_sym = {}, {}
    for sym, ms in panel.series.items():
        full_price = ms.df.set_index("time")["price"]
        norm_full, _ = vol_normalized_forecast(full_price, speed)
        pv_full = price_vol(full_price)
        norm_by_sym[sym] = norm_full.reindex(panel.common_index)
        vol_by_sym[sym] = pv_full.reindex(panel.common_index)
    return norm_by_sym, vol_by_sym


def _idm(panel: Panel, weights: dict[str, float], train_mask: pd.Series) -> float:
    """IDM = 1/sqrt(w' C w) on the TRAIN price-change correlation matrix, capped."""
    syms = panel.symbols
    dchg = panel.price.diff().loc[train_mask.values]
    corr = dchg[syms].corr().to_numpy()
    corr = np.nan_to_num(corr, nan=0.0)
    np.fill_diagonal(corr, 1.0)
    w = np.array([weights[s] for s in syms], dtype=float)
    w = w / w.sum()
    var = float(w @ corr @ w)
    if var <= 0:
        return 1.0
    return min(IDM_CAP, 1.0 / math.sqrt(var))


@dataclass
class PortfolioRun:
    speed: Speed
    scalar: float
    idm: float
    weights: dict[str, float]
    net_daily: pd.Series  # portfolio net $ per day (common window)
    gross_daily: pd.Series
    per_market_net: dict[str, pd.Series]
    per_market_pos: dict[str, pd.Series]
    per_market_turnover: dict[str, pd.Series]


def run_portfolio(
    panel: Panel,
    speed: Speed,
    *,
    cost: CostModel,
    scalar: float,
    idm: float,
    weights: dict[str, float],
    vol_target: float = VOL_TARGET,
) -> PortfolioRun:
    """Run the vol-targeted portfolio for one speed over the FULL common window. The split
    is applied by the caller (slice ``net_daily``). ``scalar`` and ``idm`` are TRAIN-fit and
    passed in so the holdout uses no peeked parameter."""
    norm_by_sym, vol_by_sym = build_forecasts(panel, speed)
    idx = panel.common_index
    per_net, per_pos, per_to = {}, {}, {}
    port_net = pd.Series(0.0, index=idx)
    port_gross = pd.Series(0.0, index=idx)

    for sym, ms in panel.series.items():
        m = ms.market
        fc = scaled_forecast(norm_by_sym[sym], scalar)
        pv = vol_by_sym[sym]
        inst_dollar_vol = pv * m.multiplier
        base = (fc / 10.0) * (vol_target / inst_dollar_vol)
        pos = (idm * weights[sym] * base).reindex(idx)
        pos = pos.replace([np.inf, -np.inf], np.nan).fillna(0.0)

        dprice = panel.price[sym].diff()
        gross = pos.shift(1).fillna(0.0) * dprice * m.multiplier
        turnover = (pos - pos.shift(1).fillna(0.0)).abs()
        cost_usd = turnover * cost.per_contract_side(m)
        net = gross - cost_usd

        per_net[sym] = net
        per_pos[sym] = pos
        per_to[sym] = turnover
        port_net = port_net.add(net, fill_value=0.0)
        port_gross = port_gross.add(gross, fill_value=0.0)

    return PortfolioRun(
        speed=speed,
        scalar=scalar,
        idm=idm,
        weights=weights,
        net_daily=port_net,
        gross_daily=port_gross,
        per_market_net=per_net,
        per_market_pos=per_pos,
        per_market_turnover=per_to,
    )


# --------------------------------------------------------------------------- #
# Daily-stream stats (continuous strategy — PF/Sharpe on daily $)
# --------------------------------------------------------------------------- #
@dataclass
class DailyStats:
    n_days: int
    n_active_days: int
    total_pnl_usd: float
    profit_factor: float
    sharpe_ann: float
    sr_per_day: float
    skew: float
    kurtosis: float  # full (non-excess)
    max_drawdown_usd: float
    per_year: dict = field(default_factory=dict)


def _max_dd(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    return float((equity - np.maximum.accumulate(equity)).min())


def daily_stats(net: pd.Series) -> DailyStats:
    net = net.dropna()
    if len(net) == 0:
        return DailyStats(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 3.0, 0.0, {})
    pos = net[net > 0].sum()
    neg = -net[net < 0].sum()
    pf = float(pos / neg) if neg > 0 else (math.inf if pos > 0 else 0.0)
    sd = net.std(ddof=1)
    sr_day = float(net.mean() / sd) if sd > 0 and len(net) > 1 else 0.0
    sharpe_ann = sr_day * math.sqrt(TRADING_DAYS)
    sk = float(net.skew()) if len(net) > 2 else 0.0
    ku = float(net.kurtosis()) + 3.0 if len(net) > 3 else 3.0  # pandas gives EXCESS
    yr = net.groupby(net.index.year).sum().round(2)
    return DailyStats(
        n_days=int(len(net)),
        n_active_days=int((net != 0).sum()),
        total_pnl_usd=float(net.sum()),
        profit_factor=pf,
        sharpe_ann=sharpe_ann,
        sr_per_day=sr_day,
        skew=sk,
        kurtosis=ku,
        max_drawdown_usd=_max_dd(net.cumsum().to_numpy()),
        per_year={int(k): float(v) for k, v in yr.items()},
    )
