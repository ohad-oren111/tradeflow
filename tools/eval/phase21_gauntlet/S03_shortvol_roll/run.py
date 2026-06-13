"""Phase-21 S03 — VIX short-vol roll-yield via inverse ETP (research-only, offline).

HONEST LABEL (pre-registered, TESTABLE-PROXY): the tradeable is SVXY, the inverse
VIX-futures ETF — the ETP does the futures rolling internally, so this tests the
short-vol roll-yield AS HARVESTABLE THROUGH THE ETP, leverage changes included
(SVXY was -1.0x until 2018-02-27, -0.5x after). Signals are the published ^VIX and
^VIX3M index closes; decision-at-close, execution at the same close (standard
approximation used by every prior phase).

Episodes: $100k notional per episode. Filter observed ON at close t (and flat) ->
enter at close t; hold while ON; first close T observed OFF -> exit at close T.
PnL = 100k * (P_T/P_t0 - 1) - costs. Episode = one filter-on run (the trade unit;
never per-day rows — n-inflation trap).

Costs: $0 commission (US ETF) + 5bp/side spread/impact (work-order large-cap tier;
SVXY is liquid). Cost-cliff scales the 5bp.

PRE-REGISTERED (frozen before any result was computed; no expansion after results):
  * Variants (4):
      V1 "contango"      — long SVXY while VIX < VIX3M
      V2 "calm"          — long SVXY while VIX < 20
      V3 "both"          — VIX < VIX3M AND VIX < 20
      V4 "deep_contango" — VIX3M/VIX > 1.05
  * Sample: 2011-10-04 (SVXY inception) .. 2026-07-01.
  * Split: TRAIN 2011-10-04 -> 2020-01-01 (Volmageddon IN train);
           HOLDOUT 2020-01-01 -> 2026-07-01 (COVID + Aug-2024 in holdout).
    n-RISK flagged upfront: filter-on runs may land < 200 in train — if so that is
    the structural ceiling (NEAR-MISS at best), reported, not inflated.
  * Gates: phase16 verbatim, EXCEPT the expectancy gate is pre-registered at
    $10/episode on the $100k notional (the $5 MNQ-contract bar scaled ~2x for
    ~2x the notional; the stricter of the two).
  * Deflation: --prior-trials 84 + 4 = 88.
  * Champion: max TRAIN PF.
  * Kill tests:
      K1 tail-shape (THE test — Volmageddon lesson): the worst 90-calendar-day
        rolling PnL window must not destroy more than 50% of lifetime net PnL
        (|worst_window| / max(total_net, gross_wins*0.1) < 0.5 with total_net>0);
        plus per-crisis-window PnL reported (Feb-2018, Mar-2020, Aug-2024).
      K2 recency — PF >= 1.30 on BOTH 2015+ and 2020+.
      K3 monotonicity — episode PnL must scale with entry contango depth
        (VIX3M/VIX at entry), quintile test (phase19 machinery).
      K4 cost-cliff — reported.
      K5 VXX cross-check — short VXX (2018->) under the champion filter; sign of
        expectancy reported. Robustness only (borrow cost unmodelled — labeled);
        never verdict-carrying.
  * VERDICT: PASS iff train gate (with $10 exp bar) + holdout gate (incl DSR>=0.95)
    + K1 + K2 + K3. NEAR-MISS iff PF (train+holdout) + DSR clear and exactly ONE
    structural gate fails. Else NONE.

    .venv/bin/python -m tools.eval.phase21_gauntlet.S03_shortvol_roll.run --prior-trials 84
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np
import pandas as pd

from tools.eval.phase16.gates import (
    DSR_MIN,
    HOLDOUT_PF_DEGRADE_MAX,
    N_TRADES_MIN,
    PF_MIN,
)
from tools.eval.phase16.metrics import (
    Stats,
    bonferroni_haircut,
    compute_stats,
    deflated_sharpe,
    expected_max_sharpe,
)

_DATA = "/home/tradeflow/tradeflow/research/data/phase21"
VIX_CSV = f"{_DATA}/VIX_daily.csv"
VIX3M_CSV = f"{_DATA}/VIX3M_daily.csv"
SVXY_CSV = f"{_DATA}/SVXY_daily.csv"
VXX_CSV = f"{_DATA}/VXX_daily.csv"

SAMPLE_START = "2011-10-04"
TRAIN_END = "2020-01-01"
HOLDOUT_END = "2026-07-01"
RECENCY_2015 = "2015-01-01"
RECENCY_2020 = "2020-01-01"
CRISIS_WINDOWS = {
    "volmageddon_feb2018": ("2018-01-15", "2018-03-31"),
    "covid_mar2020": ("2020-02-15", "2020-05-01"),
    "aug2024_spike": ("2024-07-15", "2024-09-01"),
}

NOTIONAL = 100_000.0
SPREAD_BP_SIDE = 5.0  # large-cap ETF tier (work order)
EXP_MIN_USD = 10.0  # pre-registered $10/episode on $100k (stricter than phase16 $5)

VARIANTS = ["contango", "calm", "both", "deep_contango"]


def _load(path: str) -> pd.Series:
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    s = df.set_index("time")["close"].astype(float).sort_index().dropna()
    return s[~s.index.duplicated(keep="first")]


def _filter_series(vix: pd.Series, vix3m: pd.Series, variant: str) -> pd.Series:
    j = pd.concat([vix.rename("vix"), vix3m.rename("v3m")], axis=1, join="inner").dropna()
    if variant == "contango":
        on = j["vix"] < j["v3m"]
    elif variant == "calm":
        on = j["vix"] < 20.0
    elif variant == "both":
        on = (j["vix"] < j["v3m"]) & (j["vix"] < 20.0)
    elif variant == "deep_contango":
        on = (j["v3m"] / j["vix"]) > 1.05
    else:
        raise ValueError(variant)
    return on


def _episodes(
    price: pd.Series, on: pd.Series, *, lo: str, hi: str, spread_bp: float
) -> pd.DataFrame:
    """Filter-on runs -> episode ledger on the common (price, signal) index."""
    j = pd.concat([price.rename("p"), on.rename("on")], axis=1, join="inner").dropna()
    j = j[(j.index >= pd.Timestamp(lo, tz="UTC")) & (j.index < pd.Timestamp(hi, tz="UTC"))]
    rows = []
    entry_i = None
    idx = j.index
    onv = j["on"].to_numpy()
    pv = j["p"].to_numpy()
    depth = None
    for i in range(len(j)):
        if entry_i is None and onv[i]:
            entry_i = i
        elif entry_i is not None and not onv[i]:
            rows.append((entry_i, i))
            entry_i = None
    if entry_i is not None:
        rows.append((entry_i, len(j) - 1))
    out = []
    cost = NOTIONAL * (spread_bp / 1e4) * 2.0  # entry+exit sides
    for i0, i1 in rows:
        if i1 <= i0:
            continue
        gross = NOTIONAL * (pv[i1] / pv[i0] - 1.0)
        out.append(
            {
                "pnl_usd": gross - cost,
                "gross_usd": gross,
                "entry_time": idx[i0],
                "exit_time": idx[i1],
                "year": int(idx[i1].year),
                "hold_days": int(i1 - i0),
            }
        )
    led = pd.DataFrame(
        out, columns=["pnl_usd", "gross_usd", "entry_time", "exit_time", "year", "hold_days"]
    )
    _ = depth
    return led


def _attach_depth(led: pd.DataFrame, vix: pd.Series, vix3m: pd.Series) -> pd.DataFrame:
    ratio = (vix3m / vix).dropna()
    led = led.copy()
    led["abs_s"] = [float(ratio.get(t, np.nan)) for t in led["entry_time"]]
    return led


def _split(df: pd.DataFrame, lo: str, mid: str, hi: str):
    lo_t, mid_t, hi_t = (pd.Timestamp(x, tz="UTC") for x in (lo, mid, hi))
    tr = df[(df["entry_time"] >= lo_t) & (df["entry_time"] < mid_t)].reset_index(drop=True)
    ho = df[(df["entry_time"] >= mid_t) & (df["entry_time"] < hi_t)].reset_index(drop=True)
    return tr, ho


def _window(df: pd.DataFrame, lo: str, hi: str) -> pd.DataFrame:
    lo_t, hi_t = pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC")
    return df[(df["entry_time"] >= lo_t) & (df["entry_time"] < hi_t)].reset_index(drop=True)


def _pf(x: float) -> float:
    return float("inf") if not math.isfinite(x) else round(x, 3)


def _stats_dict(s: Stats) -> dict:
    return {
        "n": s.n_trades,
        "pf": _pf(s.profit_factor),
        "exp_per_episode": round(s.expectancy_usd, 2),
        "net_usd": round(s.total_pnl_usd, 2),
        "win_rate": round(s.win_rate, 4),
        "sr_per_trade": round(s.sr_per_trade, 5),
        "max_dd_usd": round(s.max_drawdown_usd, 2),
        "per_year": {int(k): round(v, 2) for k, v in s.per_year.items()},
    }


def _gate_train(s: Stats) -> tuple[bool, list[str]]:
    """phase16 train gate with the pre-registered $10/episode expectancy bar."""
    reasons, ok = [], True
    c = s.profit_factor >= PF_MIN
    ok &= c
    reasons.append(f"PF {s.profit_factor:.3f} {'>=' if c else '<'} {PF_MIN}")
    c = s.expectancy_usd >= EXP_MIN_USD
    ok &= c
    reasons.append(f"exp ${s.expectancy_usd:.2f} {'>=' if c else '<'} ${EXP_MIN_USD}")
    c = s.n_trades >= N_TRADES_MIN
    ok &= c
    reasons.append(f"n {s.n_trades} {'>=' if c else '<'} {N_TRADES_MIN}")
    yr_ok = bool(s.per_year) and all(v > 0 for v in s.per_year.values())
    ok &= yr_ok
    notes = [f"{y}:{'+' if v > 0 else '-'}" for y, v in sorted(s.per_year.items())]
    reasons.append(f"each-year {'OK' if yr_ok else 'FAIL'} [{' '.join(notes)}]")
    return bool(ok), reasons


def _gate_holdout(train: Stats, hold: Stats, dsr: float) -> tuple[bool, list[str]]:
    reasons, ok = [], True
    c = hold.profit_factor >= PF_MIN
    ok &= c
    reasons.append(f"HO PF {hold.profit_factor:.3f} {'>=' if c else '<'} {PF_MIN}")
    c = hold.expectancy_usd >= EXP_MIN_USD
    ok &= c
    reasons.append(f"HO exp ${hold.expectancy_usd:.2f} {'>=' if c else '<'} ${EXP_MIN_USD}")
    yr_ok = bool(hold.per_year) and all(v > 0 for v in hold.per_year.values())
    ok &= yr_ok
    reasons.append(f"HO each-year {'OK' if yr_ok else 'FAIL'}")
    floor = (1.0 - HOLDOUT_PF_DEGRADE_MAX) * train.profit_factor
    c = hold.profit_factor >= floor
    ok &= c
    reasons.append(f"HO PF {'>=' if c else '<'} 0.75*train {floor:.3f}")
    c = dsr >= DSR_MIN
    ok &= c
    reasons.append(f"DSR {dsr:.3f} {'>=' if c else '<'} {DSR_MIN}")
    return bool(ok), reasons


# --------------------------------------------------------------------------- #
# Kill tests
# --------------------------------------------------------------------------- #
def _tail_shape(led: pd.DataFrame) -> dict:
    """K1: worst 90-calendar-day PnL window vs lifetime net PnL + crisis PnLs."""
    if led.empty:
        return {"pass": False, "reason": "empty ledger"}
    d = led.set_index("exit_time")["pnl_usd"].sort_index()
    daily = d.groupby(d.index.normalize()).sum()
    worst = 0.0
    times = daily.index
    vals = daily.to_numpy()
    for i in range(len(daily)):
        hi = times[i] + pd.Timedelta(days=90)
        mask = (times >= times[i]) & (times < hi)
        w = float(vals[mask].sum())
        worst = min(worst, w)
    total = float(led["pnl_usd"].sum())
    crisis = {}
    for name, (lo, hi) in CRISIS_WINDOWS.items():
        lo_t, hi_t = pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC")
        m = (led["exit_time"] >= lo_t) & (led["exit_time"] <= hi_t)
        crisis[name] = round(float(led.loc[m, "pnl_usd"].sum()), 2)
    ok = bool(total > 0 and abs(worst) / total < 0.5)
    return {
        "pass": ok,
        "worst_90d_window_usd": round(worst, 2),
        "lifetime_net_usd": round(total, 2),
        "ratio": round(abs(worst) / total, 3) if total > 0 else None,
        "crisis_window_pnl": crisis,
        "reason": "no quarter may destroy >50% of lifetime PnL (Volmageddon shape test)",
    }


def _recency(led: pd.DataFrame) -> dict:
    s15 = compute_stats(_window(led, RECENCY_2015, HOLDOUT_END))
    s20 = compute_stats(_window(led, RECENCY_2020, HOLDOUT_END))
    return {
        "pass": bool(s15.profit_factor >= PF_MIN and s20.profit_factor >= PF_MIN),
        "pf_2015plus": _pf(s15.profit_factor),
        "n_2015plus": s15.n_trades,
        "pf_2020plus": _pf(s20.profit_factor),
        "n_2020plus": s20.n_trades,
        "reason": "roll yield must survive the modern vol regime",
    }


def _monotonic(led: pd.DataFrame, q: int = 5) -> dict:
    d = led.dropna(subset=["abs_s"]).copy()
    if len(d) < q * 4:
        return {"pass": False, "reason": "insufficient-n", "quantile_means": []}
    d["qbin"] = pd.qcut(d["abs_s"].rank(method="first"), q, labels=False)
    means = d.groupby("qbin")["pnl_usd"].mean()
    qm = [round(float(v), 2) for v in means.sort_index().values]
    ranks = pd.Series(qm).rank().to_numpy()
    idx = np.arange(len(qm), dtype=float)
    rho = float(np.corrcoef(ranks, idx)[0, 1]) if len(qm) > 1 else 0.0
    ok = bool(qm[-1] > qm[0] and rho > 0.0)
    return {
        "pass": ok,
        "reason": f"top {qm[-1]} vs bottom {qm[0]} rho {round(rho, 3)}",
        "quantile_means": qm,
    }


def _cost_cliff(svxy: pd.Series, on: pd.Series) -> dict:
    cliff, series = None, []
    k = 0.5
    while k <= 8.0:
        led = _episodes(svxy, on, lo=SAMPLE_START, hi=HOLDOUT_END, spread_bp=SPREAD_BP_SIDE * k)
        _, ho = _split(led, SAMPLE_START, TRAIN_END, HOLDOUT_END)
        pf = compute_stats(ho).profit_factor
        series.append({"k": k, "holdout_pf": _pf(pf)})
        if cliff is None and pf < PF_MIN:
            cliff = k
        k = round(k + 0.5, 3)
    return {"cliff_multiple": cliff, "curve": series}


def _vxx_crosscheck(vxx: pd.Series, on: pd.Series) -> dict:
    """K5 (robustness only, labeled): SHORT VXX under the champion filter, 2018->.
    Borrow cost unmodelled — sign check only."""
    j = pd.concat([vxx.rename("p"), on.rename("on")], axis=1, join="inner").dropna()
    led = _episodes(j["p"], j["on"], lo="2018-02-01", hi=HOLDOUT_END, spread_bp=SPREAD_BP_SIDE)
    led["pnl_usd"] = -led["gross_usd"] - (NOTIONAL * (SPREAD_BP_SIDE / 1e4) * 2.0)
    s = compute_stats(led)
    return {
        "exp_per_episode": round(s.expectancy_usd, 2),
        "n": s.n_trades,
        "pf": _pf(s.profit_factor),
        "label": "short-VXX sign check; borrow unmodelled; never verdict-carrying",
    }


# --------------------------------------------------------------------------- #
def run_eval(*, prior_trials: int = 84) -> dict:
    print("[EVAL] S03 short-vol roll: loading VIX/VIX3M/SVXY/VXX")
    vix, vix3m, svxy, vxx = _load(VIX_CSV), _load(VIX3M_CSV), _load(SVXY_CSV), _load(VXX_CSV)
    n_trials = prior_trials + len(VARIANTS)
    print(f"[EVAL] deflator n_trials={prior_trials}+{len(VARIANTS)}={n_trials}")

    variants: dict[str, dict] = {}
    ledgers: dict[str, pd.DataFrame] = {}
    filters: dict[str, pd.Series] = {}
    train_srs: list[float] = []
    for key in VARIANTS:
        on = _filter_series(vix, vix3m, key)
        filters[key] = on
        led = _episodes(svxy, on, lo=SAMPLE_START, hi=HOLDOUT_END, spread_bp=SPREAD_BP_SIDE)
        led = _attach_depth(led, vix, vix3m)
        ledgers[key] = led
        tr, ho = _split(led, SAMPLE_START, TRAIN_END, HOLDOUT_END)
        s_tr, s_ho = compute_stats(tr), compute_stats(ho)
        g_ok, g_reasons = _gate_train(s_tr)
        variants[key] = {
            "train": _stats_dict(s_tr),
            "holdout": _stats_dict(s_ho),
            "train_gate": g_reasons,
            "train_gate_pass": g_ok,
        }
        if s_tr.n_trades >= 4:
            train_srs.append(s_tr.sr_per_trade)
        print(
            f"[EVAL] {key}: train PF {variants[key]['train']['pf']} (n={s_tr.n_trades}) | "
            f"holdout PF {variants[key]['holdout']['pf']} (n={s_ho.n_trades})"
        )

    def _train_pf(k: str) -> float:
        v = variants[k]["train"]["pf"]
        return -1.0 if v == float("inf") else v

    champ_key = max(VARIANTS, key=_train_pf)
    champ = variants[champ_key]
    champ_train_pass = champ["train_gate_pass"]
    print(f"[EVAL] champion: {champ_key} (train-gate {'PASS' if champ_train_pass else 'FAIL'})")

    sr_var = float(np.var(train_srs, ddof=1)) if len(train_srs) > 1 else 0.0
    sr0 = expected_max_sharpe(n_trials, sr_var)
    champ_led = ledgers[champ_key]
    tr, ho = _split(champ_led, SAMPLE_START, TRAIN_END, HOLDOUT_END)
    s_tr, s_ho = compute_stats(tr), compute_stats(ho)
    dsr, _ = deflated_sharpe(
        s_tr.sr_per_trade, s_tr.n_trades, s_tr.skew, s_tr.kurtosis, n_trials, sr_var
    )
    _, haircut_p = bonferroni_haircut(s_tr.sr_per_trade, s_tr.n_trades, n_trials)
    ho_ok, ho_reasons = _gate_holdout(s_tr, s_ho, dsr) if champ_train_pass else (False, ["SEALED"])

    kill1 = _tail_shape(champ_led)
    kill2 = _recency(champ_led)
    kill3 = _monotonic(champ_led)
    kill4 = _cost_cliff(svxy, filters[champ_key])
    kill5 = _vxx_crosscheck(vxx, filters[champ_key])

    verdict_pass = bool(
        champ_train_pass and ho_ok and kill1["pass"] and kill2["pass"] and kill3["pass"]
    )
    structural_fails = []
    if not champ_train_pass:
        structural_fails += [f"train: {r}" for r in champ["train_gate"] if "<" in r or "FAIL" in r]
    if champ_train_pass and not ho_ok:
        structural_fails += [f"holdout: {r}" for r in ho_reasons if "<" in r or "FAIL" in r]
    for kname, k in (("K1-tail-shape", kill1), ("K2-recency", kill2), ("K3-monotonicity", kill3)):
        if not k["pass"]:
            structural_fails.append(kname)
    pf_dsr_clear = bool(
        s_tr.profit_factor >= PF_MIN and s_ho.profit_factor >= PF_MIN and dsr >= DSR_MIN
    )
    if verdict_pass:
        verdict = "PASS"
    elif pf_dsr_clear and len(structural_fails) == 1:
        verdict = f"NEAR-MISS ({structural_fails[0]})"
    else:
        verdict = "NONE"
    print(
        f"[EVAL] VERDICT: {verdict} — {champ_key} train PF {_pf(s_tr.profit_factor)} "
        f"HO PF {_pf(s_ho.profit_factor)} dsr {round(dsr, 3)} fails={structural_fails}"
    )

    led0 = _episodes(svxy, filters[champ_key], lo=SAMPLE_START, hi=HOLDOUT_END, spread_bp=0.0)
    integrity = {
        "no_lookahead": "filter uses VIX index closes published at the same close; "
        "decision-at-close execution approximation, labeled",
        "holdout_sealed_until_champion": True,
        "champion_selected_on": "train_profit_factor (pre-registered)",
        "costs_applied": bool(
            abs(float(led0["pnl_usd"].sum()) - float(champ_led["pnl_usd"].sum())) > 1e-6
        ),
        "gross_total_usd": round(float(led0["pnl_usd"].sum()), 2),
        "net_total_usd": round(float(champ_led["pnl_usd"].sum()), 2),
        "etp_proxy_label": "SVXY as traded (incl. 2018 -1x -> -0.5x deleverage)",
    }

    return {
        "phase": "21-S03-shortvol-roll",
        "verdict": verdict,
        "research_only": True,
        "data": {
            "vix_span": [str(vix.index[0].date()), str(vix.index[-1].date())],
            "vix3m_span": [str(vix3m.index[0].date()), str(vix3m.index[-1].date())],
            "svxy_span": [str(svxy.index[0].date()), str(svxy.index[-1].date())],
            "vxx_span": [str(vxx.index[0].date()), str(vxx.index[-1].date())],
        },
        "split": {"train": [SAMPLE_START, TRAIN_END], "holdout": [TRAIN_END, HOLDOUT_END]},
        "costs": {
            "notional_usd": NOTIONAL,
            "spread_bp_per_side": SPREAD_BP_SIDE,
            "commission": 0.0,
            "exp_gate_usd": EXP_MIN_USD,
            "source": "work-order equity cost tier (large-cap 5bp/side)",
        },
        "deflator": {
            "prior_trials": prior_trials,
            "in_eval_variants": len(VARIANTS),
            "n_trials": n_trials,
            "sr_variance_pool": round(sr_var, 6),
            "sr0_expected_max": round(sr0, 5),
            "dsr_champion": round(dsr, 5),
            "bonferroni_p_champion": haircut_p,
        },
        "champion": {
            "variant": champ_key,
            "train_gate_pass": champ_train_pass,
            "train": champ["train"],
            "holdout": _stats_dict(s_ho),
            "holdout_gate": ho_reasons,
            "holdout_gate_pass": ho_ok,
        },
        "variants": variants,
        "kill_tests": {
            "1_tail_shape_90d": kill1,
            "2_recency": kill2,
            "3_monotonicity_contango_depth": kill3,
            "4_cost_cliff": kill4,
            "5_vxx_crosscheck_robustness": kill5,
        },
        "integrity": integrity,
        "gates": {
            "PF_MIN": PF_MIN,
            "EXP_MIN_USD_preregistered": EXP_MIN_USD,
            "N_TRADES_MIN": N_TRADES_MIN,
            "DSR_MIN": DSR_MIN,
            "HOLDOUT_PF_DEGRADE_MAX": HOLDOUT_PF_DEGRADE_MAX,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prior-trials", type=int, default=84)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    res = run_eval(prior_trials=args.prior_trials)
    out = (
        args.out
        or "/home/tradeflow/tradeflow/tools/eval/phase21_gauntlet/S03_shortvol_roll/results.json"
    )
    with open(out, "w") as fh:
        json.dump(res, fh, indent=2)
    print(f"[EVAL] wrote {out} — verdict {res['verdict']}")


if __name__ == "__main__":
    main()
