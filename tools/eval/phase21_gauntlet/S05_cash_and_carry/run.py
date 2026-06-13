"""Phase-21 S05 — Crypto cash-and-carry basis on dated futures (research-only).

Delta-neutral carry: long spot + short Binance COIN-margined quarterly future,
hold to expiry; convergence locks the entry basis. PnL is computed from REALIZED
prices (entry F/S and final F/S), never from assumed-perfect convergence:

    pnl = NOTIONAL * [ (F_in - F_last)/S_in + (S_last - S_in)/S_in ] - costs
        = NOTIONAL * [ F_in/S_in - 1 - (F_last - S_last)/S_in ] - costs

HONEST LABELS (pre-registered):
  * EXIT is at the last FULL trading bar (expiry-EVE), not the settlement-day
    bar. Binance CM quarterlies settle ~08:00 UTC on the expiry day, so that
    day's daily bar closes at the 08:00 settlement while spot's daily bar closes
    at 24:00 UTC — a ~16h misalignment that injected a spurious ±8% "basis" on
    the final bar (fetch_data.py drops it; data integrity verified: full bars
    converge to <0.6% across all 46 contracts). Exiting one bar early leaves the
    tiny residual basis (~0.3%) on the table — conservative.
  * Linear-payoff approximation of an inverse (coin-margined) future; the
    second-order convexity of inverse contracts is ignored (small at basis <=30%
    ann over <=9 months) — results apply to the linear form.
  * n is STRUCTURALLY < 200 (Stage-0 flag): ~21 expired quarterlies/coin x 2
    coins. The n-gate verdict ceiling is NEAR-MISS; reported, not inflated
    (no per-day marking, no episode splitting).

Costs: phase20b constants verbatim (TAKER 0.05%/fill, slip 2bp/fill majors,
4 fills/RT) on $100k notional -> $280/RT at scale 1.

PRE-REGISTERED (frozen before any result; no expansion):
  * Variants (4) — entry rule per contract (one episode max per contract):
      V1 "thr5"   — enter first day annualized basis >= 5%
      V2 "thr10"  — enter first day annualized basis >= 10%
      V3 "thr15"  — enter first day annualized basis >= 15%
      V4 "roll60" — enter at last day >= 60 cd before expiry IF basis beats the
                    annualized cost hurdle (fully cost-derived, no tuning)
    Entry days require 14 <= days_to_expiry <= 270. Annualized basis =
    (F/S - 1) * 365/days_to_expiry.
  * Split BY EXPIRY (a contract belongs to the era its carry pays out in):
    TRAIN expiry < 2024-01-01 | HOLDOUT expiry >= 2024-01-01 (post-spot-ETF
    compression era — the Phase-20c lesson, now the OOS test).
  * Gates: phase16 verbatim. Deflation: --prior-trials 92 + 4 = 96.
  * Champion: max TRAIN PF (degenerate inf-PF deprioritised, phase19 convention).
  * Kill tests:
      K1 post-ETF holdout — holdout PF >= 1.30 AND holdout median annualized net
         carry reported (the economically meaningful number).
      K2 crisis-strip — drop episodes whose holding window overlaps COVID
         (2020-02-15..2020-05-01), the May-2021 deleveraging (2021-05-10..
         2021-07-20), or FTX (2022-11-01..2022-12-31); stripped full-sample
         PF >= 1.30 (normal-regime carry must clear costs on its own).
      K3 monotonicity — realized PnL must scale with entry annualized basis
         (mechanical arb -> near-monotone expected; machinery sanity).
      K4 cost-cliff — reported.
      K5 convergence integrity — max |F_last - S_last|/S_in < 2% per episode.
  * VERDICT: PASS iff train+holdout gates (incl DSR) + K1 + K2 + K3 + K5.
    NEAR-MISS iff PF (train+holdout) + DSR clear and exactly ONE structural gate
    fails. Else NONE.

    .venv/bin/python -m tools.eval.phase21_gauntlet.S05_cash_and_carry.run --prior-trials 92
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np
import pandas as pd

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
    expected_max_sharpe,
)

FUT_CSV = "/home/tradeflow/tradeflow/research/data/phase21/delivery_futures_daily.csv"
SPOT = {
    "BTC": "/home/tradeflow/tradeflow/research/data/phase20/BTC_spot.csv",
    "ETH": "/home/tradeflow/tradeflow/research/data/phase20/ETH_spot.csv",
}

NOTIONAL = 100_000.0
TAKER_FEE = 0.0005  # phase20b verbatim
SLIP_MAJOR = 0.0002
N_FILLS = 4.0

HOLDOUT_EXPIRY = "2024-01-01"
MIN_DTE, MAX_DTE = 14, 270
CRISIS_WINDOWS = [
    ("2020-02-15", "2020-05-01"),
    ("2021-05-10", "2021-07-20"),
    ("2022-11-01", "2022-12-31"),
]

VARIANTS = ["thr5", "thr10", "thr15", "roll60"]
THRESHOLDS = {"thr5": 0.05, "thr10": 0.10, "thr15": 0.15}


def _rt_cost(scale: float = 1.0) -> float:
    return N_FILLS * (TAKER_FEE + SLIP_MAJOR) * scale * NOTIONAL


def _load_futures() -> pd.DataFrame:
    df = pd.read_csv(FUT_CSV)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df["expiry"] = pd.to_datetime(df["expiry"], utc=True)
    return df


def _load_spot() -> dict[str, pd.Series]:
    out = {}
    for coin, path in SPOT.items():
        s = pd.read_csv(path)
        s["time"] = pd.to_datetime(s["time"], utc=True).dt.normalize()
        out[coin] = s.set_index("time")["close"].astype(float)
    return out


def _episodes(
    fut: pd.DataFrame, spot: dict[str, pd.Series], variant: str, cost_scale: float = 1.0
) -> pd.DataFrame:
    rows = []
    cost = _rt_cost(cost_scale)
    for sym, g in fut.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        coin = g["coin"].iloc[0]
        expiry = g["expiry"].iloc[0]
        sp = spot[coin]
        g = g[g["date"].isin(sp.index)]
        if g.empty:
            continue
        dte = (expiry - g["date"]).dt.days
        s_at = np.array([float(sp[d]) for d in g["date"]])
        ann_basis = (g["close"].to_numpy() / s_at - 1.0) * (365.0 / np.maximum(dte, 1))
        elig = (dte >= MIN_DTE) & (dte <= MAX_DTE)
        pick = None
        if variant in THRESHOLDS:
            hits = np.where(elig & (ann_basis >= THRESHOLDS[variant]))[0]
            if len(hits):
                pick = int(hits[0])
        else:  # roll60: last eligible day >= 60 cd before expiry, basis > cost hurdle
            cand = np.where(elig & (dte >= 60))[0]
            if len(cand):
                i = int(cand[-1])
                hurdle = (cost / NOTIONAL) * (365.0 / max(int(dte.iloc[i]), 1))
                if ann_basis[i] > hurdle:
                    pick = i
        if pick is None:
            continue
        last = g.iloc[-1]
        s_last = float(sp.get(last["date"], np.nan))
        if not math.isfinite(s_last):
            continue
        f_in, s_in = float(g["close"].iloc[pick]), float(s_at[pick])
        f_last = float(last["close"])
        gross = NOTIONAL * ((f_in - f_last) / s_in + (s_last - s_in) / s_in)
        rows.append(
            {
                "pnl_usd": gross - cost,
                "gross_usd": gross,
                "entry_time": g["date"].iloc[pick],
                "exit_time": last["date"],
                "year": int(expiry.year),
                "symbol": sym,
                "coin": coin,
                "expiry": expiry,
                "entry_ann_basis": float(ann_basis[pick]),
                "dte_at_entry": int(dte.iloc[pick]),
                "conv_err": abs(f_last - s_last) / s_in,
                "ann_net_carry": (gross - cost) / NOTIONAL * (365.0 / max(int(dte.iloc[pick]), 1)),
            }
        )
    cols = [
        "pnl_usd",
        "gross_usd",
        "entry_time",
        "exit_time",
        "year",
        "symbol",
        "coin",
        "expiry",
        "entry_ann_basis",
        "dte_at_entry",
        "conv_err",
        "ann_net_carry",
    ]
    return pd.DataFrame(rows, columns=cols).sort_values("expiry").reset_index(drop=True)


def _split_by_expiry(df: pd.DataFrame):
    cut = pd.Timestamp(HOLDOUT_EXPIRY, tz="UTC")
    tr = df[df["expiry"] < cut].reset_index(drop=True)
    ho = df[df["expiry"] >= cut].reset_index(drop=True)
    return tr, ho


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
        "per_year": {int(k): round(v, 2) for k, v in s.per_year.items()},
    }


def _monotonic(led: pd.DataFrame, q: int = 5) -> dict:
    d = led.dropna(subset=["entry_ann_basis"]).copy()
    if len(d) < q * 3:
        return {"pass": False, "reason": "insufficient-n", "quantile_means": []}
    d["qbin"] = pd.qcut(d["entry_ann_basis"].rank(method="first"), q, labels=False)
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


def _crisis_strip(led: pd.DataFrame) -> dict:
    mask = pd.Series(False, index=led.index)
    for lo, hi in CRISIS_WINDOWS:
        lo_t, hi_t = pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC")
        mask |= (led["entry_time"] <= hi_t) & (led["exit_time"] >= lo_t)
    stripped = led[~mask].reset_index(drop=True)
    pf = compute_stats(stripped).profit_factor
    return {
        "pass": bool(pf >= PF_MIN),
        "n_stripped": int(mask.sum()),
        "stripped_full_pf": _pf(pf),
        "stripped_exp": round(float(stripped["pnl_usd"].mean()), 2) if len(stripped) else None,
        "reason": "normal-regime carry must clear costs without crisis blowout entries",
    }


def _cost_cliff(fut: pd.DataFrame, spot: dict[str, pd.Series], variant: str) -> dict:
    cliff, series = None, []
    k = 0.5
    while k <= 8.0:
        led = _episodes(fut, spot, variant, cost_scale=k)
        _, ho = _split_by_expiry(led)
        pf = compute_stats(ho).profit_factor
        series.append({"k": k, "holdout_pf": _pf(pf)})
        if cliff is None and pf < PF_MIN:
            cliff = k
        k = round(k + 0.5, 3)
    return {"cliff_multiple": cliff, "curve": series}


def run_eval(*, prior_trials: int = 92) -> dict:
    print("[EVAL] S05 cash-and-carry: loading delivery futures + spot")
    fut = _load_futures()
    spot = _load_spot()
    n_trials = prior_trials + len(VARIANTS)
    print(
        f"[EVAL] {fut['symbol'].nunique()} contracts ({fut['coin'].nunique()} coins); "
        f"deflator n_trials={prior_trials}+{len(VARIANTS)}={n_trials}"
    )

    variants: dict[str, dict] = {}
    ledgers: dict[str, pd.DataFrame] = {}
    train_srs: list[float] = []
    for key in VARIANTS:
        led = _episodes(fut, spot, key)
        ledgers[key] = led
        tr, ho = _split_by_expiry(led)
        s_tr, s_ho = compute_stats(tr), compute_stats(ho)
        variants[key] = {
            "train": _stats_dict(s_tr),
            "holdout": _stats_dict(s_ho),
            "train_gate": check_train(s_tr).reasons,
            "train_gate_pass": check_train(s_tr).passed,
            "median_ann_net_carry_train": (
                round(float(tr["ann_net_carry"].median()), 4) if len(tr) else None
            ),
            "median_ann_net_carry_holdout": (
                round(float(ho["ann_net_carry"].median()), 4) if len(ho) else None
            ),
        }
        if s_tr.n_trades >= 4:
            train_srs.append(s_tr.sr_per_trade)
        print(
            f"[EVAL] {key}: train PF {variants[key]['train']['pf']} (n={s_tr.n_trades}, "
            f"med carry {variants[key]['median_ann_net_carry_train']}) | holdout PF "
            f"{variants[key]['holdout']['pf']} (n={s_ho.n_trades}, med carry "
            f"{variants[key]['median_ann_net_carry_holdout']})"
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
    tr, ho = _split_by_expiry(champ_led)
    s_tr, s_ho = compute_stats(tr), compute_stats(ho)
    dsr, _ = deflated_sharpe(
        s_tr.sr_per_trade, s_tr.n_trades, s_tr.skew, s_tr.kurtosis, n_trials, sr_var
    )
    _, haircut_p = bonferroni_haircut(s_tr.sr_per_trade, s_tr.n_trades, n_trials)
    holdout_gate = check_holdout(s_tr, s_ho, dsr) if champ_train_pass else None
    ho_pass = bool(holdout_gate.passed) if holdout_gate else False

    kill1 = {
        "pass": bool(s_ho.profit_factor >= PF_MIN),
        "holdout_pf": _pf(s_ho.profit_factor),
        "holdout_n": s_ho.n_trades,
        "median_ann_net_carry_holdout": champ["median_ann_net_carry_holdout"],
        "reason": "post-ETF era must still pay (Phase-20c compression lesson)",
    }
    kill2 = _crisis_strip(champ_led)
    kill3 = _monotonic(champ_led)
    kill4 = _cost_cliff(fut, spot, champ_key)
    max_conv = float(champ_led["conv_err"].max()) if len(champ_led) else 1.0
    kill5 = {
        "pass": bool(max_conv < 0.02),
        "max_convergence_error": round(max_conv, 5),
        "reason": "futures must converge to spot at expiry (data integrity)",
    }

    verdict_pass = bool(
        champ_train_pass
        and ho_pass
        and kill1["pass"]
        and kill2["pass"]
        and kill3["pass"]
        and kill5["pass"]
    )
    structural_fails = []
    if not champ_train_pass:
        structural_fails += [f"train: {r}" for r in champ["train_gate"] if "<" in r or "FAIL" in r]
    if holdout_gate and not holdout_gate.passed:
        structural_fails += [
            f"holdout: {r}" for r in holdout_gate.reasons if "<" in r or "FAIL" in r
        ]
    for kname, k in (
        ("K1-post-ETF", kill1),
        ("K2-crisis-strip", kill2),
        ("K3-monotonicity", kill3),
        ("K5-convergence", kill5),
    ):
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

    led0 = _episodes(fut, spot, champ_key, cost_scale=0.0)
    integrity = {
        "no_lookahead": "entry uses same-day basis (decision-at-close); expiry mechanical",
        "holdout_sealed_until_champion": True,
        "champion_selected_on": "train_profit_factor (degenerate inf deprioritised)",
        "costs_applied": bool(
            abs(float(led0["pnl_usd"].sum()) - float(champ_led["pnl_usd"].sum())) > 1e-6
        ),
        "gross_total_usd": round(float(led0["pnl_usd"].sum()), 2),
        "net_total_usd": round(float(champ_led["pnl_usd"].sum()), 2),
        "n_structural_flag": "episodes per contract capped at 1; ~2 coins x ~21 expired "
        "quarterlies -> n<200 by construction (Stage-0 flag; NEAR-MISS ceiling)",
        "linear_payoff_label": "inverse-futures convexity ignored (labeled)",
    }

    return {
        "phase": "21-S05-cash-and-carry",
        "verdict": verdict,
        "research_only": True,
        "data": {
            "contracts": int(fut["symbol"].nunique()),
            "rows": int(len(fut)),
            "span": [str(fut["date"].min().date()), str(fut["date"].max().date())],
        },
        "split": {"train_expiry": ["<", HOLDOUT_EXPIRY], "holdout_expiry": [">=", HOLDOUT_EXPIRY]},
        "costs": {
            "notional_usd": NOTIONAL,
            "taker_fee_per_fill": TAKER_FEE,
            "slip_per_fill": SLIP_MAJOR,
            "n_fills": N_FILLS,
            "rt_cost_usd": _rt_cost(1.0),
            "source": "phase20b constants verbatim (work-order requirement)",
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
            "holdout_gate": (holdout_gate.reasons if holdout_gate else "SEALED (train failed)"),
            "holdout_gate_pass": ho_pass,
        },
        "variants": variants,
        "kill_tests": {
            "1_post_etf_holdout": kill1,
            "2_crisis_strip": kill2,
            "3_monotonicity_entry_basis": kill3,
            "4_cost_cliff": kill4,
            "5_convergence_integrity": kill5,
        },
        "integrity": integrity,
        "gates": {
            "PF_MIN": PF_MIN,
            "EXP_PER_CT_MIN": EXP_PER_CT_MIN,
            "N_TRADES_MIN": N_TRADES_MIN,
            "DSR_MIN": DSR_MIN,
            "HOLDOUT_PF_DEGRADE_MAX": HOLDOUT_PF_DEGRADE_MAX,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prior-trials", type=int, default=92)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    res = run_eval(prior_trials=args.prior_trials)
    out = (
        args.out
        or "/home/tradeflow/tradeflow/tools/eval/phase21_gauntlet/S05_cash_and_carry/results.json"
    )
    with open(out, "w") as fh:
        json.dump(res, fh, indent=2)
    print(f"[EVAL] wrote {out} — verdict {res['verdict']}")


if __name__ == "__main__":
    main()
