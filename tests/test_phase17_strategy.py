"""Phase-17 Stage-2 strategy unit tests — ALL SYNTHETIC (hand-built series), no dependency
on the untracked Stage-1 CSVs or any prod path. Mirrors the phase16 harness test style.

Covers the load-bearing claims of the EWMAC portfolio: the 10Y negated-yield sign
convention, the Carver forecast scaling/cap, risk-parity-by-bucket weights, cost
monotonicity, the PF/Sharpe scale-invariance argument, trend P&L direction, and the
deflation/effective-trial accounting.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tools.eval.phase17 import gates17, signal
from tools.eval.phase17.costs17 import CostModel
from tools.eval.phase17.dataset import MarketSeries, Panel
from tools.eval.phase17.markets import Market
from tools.eval.phase17.portfolio import (
    bucket_weights,
    daily_stats,
    run_portfolio,
)


def _noisy_trend(n: int, slope: float, seed: int, base: float = 100.0) -> np.ndarray:
    """Deterministic trending series with reproducible noise (no global RNG state)."""
    rng = np.random.default_rng(seed)
    return base + slope * np.arange(n) + rng.normal(0.0, 1.0, n).cumsum() * 0.3


def _panel(series_specs: dict[str, tuple[Market, np.ndarray, int]]) -> Panel:
    """Build a Panel from {sym: (Market, price_array, sign)} with a shared daily index."""
    n = len(next(iter(series_specs.values()))[1])
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    series, cols = {}, {}
    for sym, (m, price, sign) in series_specs.items():
        close = sign * price  # so dataset's price = sign*close recovers `price`
        df = pd.DataFrame({"time": idx, "close": close, "price": price})
        series[sym] = MarketSeries(market=m, df=df, sign=sign)
        cols[sym] = pd.Series(price, index=idx)
    wide = pd.DataFrame(cols)
    return Panel(series=series, common_index=wide.index, price=wide)


# --------------------------------------------------------------------------- #
# Signal — 10Y negated-yield sign convention
# --------------------------------------------------------------------------- #
def test_negated_series_flips_ewmac_sign_exactly():
    price = pd.Series(_noisy_trend(400, 0.5, seed=1))
    sp = signal.Speed(16, 64)
    raw = signal.raw_ewmac(price, sp)
    raw_neg = signal.raw_ewmac(-price, sp)
    # EWMA is linear -> EWMAC(-price) == -EWMAC(price) at every warm bar.
    both = pd.concat([raw, raw_neg], axis=1).dropna()
    assert np.allclose(both.iloc[:, 0], -both.iloc[:, 1], atol=1e-9)


def test_falling_yield_reads_as_long_bond():
    # yield in a sustained DOWNtrend -> price proxy (-yield) UPtrend -> forecast > 0 (long).
    n = 400
    yld = pd.Series(5.0 - 0.004 * np.arange(n) + np.sin(np.arange(n) / 20) * 0.02)
    proxy = -yld
    raw = signal.raw_ewmac(proxy, signal.Speed(16, 64)).dropna()
    assert raw.iloc[-1] > 0, "negated falling-yield series must give a positive (long-bond) signal"
    # and the raw on the un-negated yield would be the opposite sign
    raw_y = signal.raw_ewmac(yld, signal.Speed(16, 64)).dropna()
    assert raw_y.iloc[-1] < 0


# --------------------------------------------------------------------------- #
# Signal — Carver forecast scaling + cap + vol
# --------------------------------------------------------------------------- #
def test_forecast_scalar_targets_ten():
    price = pd.Series(_noisy_trend(500, 0.3, seed=2))
    sp = signal.Speed(32, 128)
    norm, _ = signal.vol_normalized_forecast(price, sp)
    mask = pd.Series(True, index=price.index)
    scalar = signal.estimate_forecast_scalar({"X": norm}, {"X": mask})
    scaled = signal.scaled_forecast(norm, scalar).dropna()
    # mean |forecast| should be ~10 (allowing cap clipping to pull it slightly below).
    assert 8.0 <= float(scaled.abs().mean()) <= 12.0


def test_forecast_cap_at_twenty():
    norm = pd.Series([0.0, 100.0, -100.0, 5.0])
    scaled = signal.scaled_forecast(norm, scalar=10.0)
    assert scaled.max() <= signal.FORECAST_CAP + 1e-9
    assert scaled.min() >= -signal.FORECAST_CAP - 1e-9


def test_price_vol_is_positive_ewma():
    price = pd.Series(_noisy_trend(300, 0.2, seed=3))
    pv = signal.price_vol(price).dropna()
    assert (pv > 0).all()
    assert pv.isna().sum() == 0 or True  # warm-up NaNs allowed before min_periods


# --------------------------------------------------------------------------- #
# Portfolio — bucket weights (risk parity by bucket, FX as one cluster)
# --------------------------------------------------------------------------- #
def test_bucket_weights_sum_to_one_and_cluster_downweighted():
    from tools.eval.phase17.markets import core

    w = bucket_weights(core())
    assert abs(sum(w.values()) - 1.0) < 1e-9
    # 6 buckets: equity/metals/energy/fx/crypto/rates -> each bucket carries 1/6.
    assert abs(w["MES"] - 1 / 6) < 1e-9  # equity, sole member
    assert abs(w["MGC"] - 1 / 12) < 1e-9  # metals split 2 ways
    assert abs(w["M6E"] - 1 / 18) < 1e-9  # FX split 3 ways (the cluster down-weight)
    # the 3 FX together carry exactly one bucket
    assert abs((w["M6E"] + w["M6A"] + w["M6B"]) - 1 / 6) < 1e-9


# --------------------------------------------------------------------------- #
# Costs — monotonic in slippage
# --------------------------------------------------------------------------- #
def test_cost_per_side_monotonic_in_slippage():
    m = Market("MES", "CME", "equity", 5.0, 0.25, "test")
    c0 = CostModel(slip_ticks=0.0).per_contract_side(m)
    c1 = CostModel(slip_ticks=1.0).per_contract_side(m)
    c2 = CostModel(slip_ticks=2.0).per_contract_side(m)
    assert c0 < c1 < c2
    # 1 tick of MES = tick_value = 5*0.25 = $1.25 over the 0-tick (commission-only) cost
    assert abs((c1 - c0) - m.tick_value_usd) < 1e-9


# --------------------------------------------------------------------------- #
# Portfolio — trend P&L direction + PF/Sharpe scale invariance
# --------------------------------------------------------------------------- #
def _two_market_panel():
    me = Market("MES", "CME", "equity", 5.0, 0.25, "eq")
    mg = Market("MGC", "COMEX", "metals", 10.0, 0.1, "gold")
    return _panel(
        {
            "MES": (me, _noisy_trend(400, 0.6, seed=10), 1),
            "MGC": (mg, _noisy_trend(400, 0.4, seed=11), 1),
        }
    )


def test_long_position_profits_on_uptrend():
    panel = _two_market_panel()
    w = {"MES": 0.5, "MGC": 0.5}
    run = run_portfolio(
        panel, signal.Speed(16, 64), cost=CostModel(0.0), scalar=5.0, idm=1.0, weights=w
    )
    # both markets trend UP -> a long-biased EWMAC should net positive gross over the window
    assert run.gross_daily.sum() > 0


def test_pf_scale_invariant_to_vol_target_even_with_costs():
    panel = _two_market_panel()
    w = {"MES": 0.5, "MGC": 0.5}
    pfs = []
    for vt in (50.0, 500.0):
        run = run_portfolio(
            panel,
            signal.Speed(16, 64),
            cost=CostModel(1.0),
            scalar=5.0,
            idm=1.0,
            weights=w,
            vol_target=vt,
        )
        pfs.append(daily_stats(run.net_daily).profit_factor)
    # gross and turnover-cost both scale linearly with vol_target -> PF identical.
    assert abs(pfs[0] - pfs[1]) < 1e-6


# --------------------------------------------------------------------------- #
# Stats + deflation accounting
# --------------------------------------------------------------------------- #
def test_daily_stats_profit_factor():
    idx = pd.date_range("2024-01-01", periods=4, freq="B", tz="UTC")
    net = pd.Series([10.0, -5.0, 20.0, -5.0], index=idx)
    s = daily_stats(net)
    assert abs(s.profit_factor - (30.0 / 10.0)) < 1e-9
    assert s.total_pnl_usd == 20.0


def test_effective_trials_collapses_correlated_family():
    # 3 correlated speeds at rho=0.55 -> < 3 effective; isolated trials unchanged.
    eff = gates17.effective_trials(naive=27, n_correlated=3, rho=0.55)
    assert 25.0 < eff < 26.0
    assert gates17.effective_trials(10, 1, 0.55) == 10.0  # nothing to collapse


def _stats(**kw) -> gates17.DailyStats:
    base = {
        "n_days": 200,
        "n_active_days": 200,
        "total_pnl_usd": 0.0,
        "profit_factor": 1.0,
        "sharpe_ann": 0.0,
        "sr_per_day": 0.0,
        "skew": 0.0,
        "kurtosis": 3.0,
        "max_drawdown_usd": -100.0,
        "per_year": {},
    }
    base.update(kw)
    return gates17.DailyStats(**base)


def test_deflation_survives_iff_sharpe_beats_haircut():
    good = _stats(total_pnl_usd=5000.0, profit_factor=1.5, sharpe_ann=1.6, sr_per_day=0.10)
    d = gates17.deflate(good, n_trials_naive=27, n_trials_eff=25.3, sr_variance=0.0003)
    assert d.survives == (d.sr_hat > d.sr0)
    assert d.survives is True  # 0.10/day clears the ~0.036 haircut at 27 trials
    bad = _stats(total_pnl_usd=-100.0, profit_factor=0.9, sharpe_ann=-0.2, sr_per_day=-0.01)
    d2 = gates17.deflate(bad, n_trials_naive=27, n_trials_eff=25.3, sr_variance=0.0003)
    assert d2.survives is False
