"""Phase-21 S04 — Treasury auction concession / reversal (research-only, offline).

Tests the auction-cycle pattern documented in the dealer-intermediation literature
(Lou-Yan-Zhang "Anticipated and Repeated Shocks in Liquid Markets", Fleming et al):
yields cheapen INTO a coupon auction (the concession) and richen after it (the
reversal). Daily proxy on yield-derived price-return indexes (phase19 machinery):
5Y=^FVX (D_mod 4.5), 10Y=^TNX on disk (D_mod 7.0), 30Y=^TYX (D_mod 17.0) — D_mod is
a constant multiplier (magnitude only, can't flip sign/timing). Auction dates from
fiscaldata (1,208 nominal 5/10/30Y coupon auctions 1979->2026; 2Y/3Y/7Y excluded —
no free index proxy, counted in fetch).

Futures-style costs per leg (§0.5.97 — CME specs; commission = phase19's verified
IBKR fixed-tier treasury number): ZF tick 1/4 of 1/32, ZN tick 1/2 of 1/32, ZB tick
1/32; mult $1000; $1.45/side; 1-tick adverse slippage/side.

PRE-REGISTERED (frozen before any result; no expansion after results):
  * Variants (4), episodes pooled across the three tenor legs (1 contract each):
      V1 "concession_T1" — SHORT tenor index close T-1 -> auction-day close T
      V2 "concession_T2" — SHORT close T-2 -> close T
      V3 "reversal_T3"   — LONG  close T   -> close T+3
      V4 "combined"      — V1 round-trip + V3 round-trip per auction
  * Dedupe: one episode per (tenor, auction-trading-day); same-day multi-tenor
    auctions are separate episodes (different instruments).
  * Split: TRAIN 1980-01-01 -> 2015-01-01 | HOLDOUT 2015-01-01 -> 2026-07-01
    (holdout contains the 2022+ QT/supply era the thesis says should be strongest).
  * Gates: phase16 verbatim. Deflation: --prior-trials 88 + 4 = 92.
  * Champion: max TRAIN PF.
  * Kill tests:
      K1 placebo-shift — identical trades anchored 7 trading days BEFORE each
        auction (shifted anchors falling within +/-3 td of ANY real auction are
        dropped); pass iff holdout real PF beats placebo by >0.10 AND >= 1.30.
      K2 per-tenor breadth — champion expectancy must be POSITIVE in ALL of
        5Y/10Y/30Y over the full sample.
      K3 recency — PF >= 1.30 on BOTH 2015+ and 2022+ (QT era).
      K4 crisis-strip — GFC + COVID windows removed; stripped train AND holdout
        PF >= 1.30.
      K5 cost-cliff — reported.
      Monotonicity — N/A (no ex-ante magnitude; bid-to-cover is post-auction).
  * VERDICT: PASS iff train gate + holdout gate (incl DSR>=0.95) + K1 + K2 + K3 +
    K4. NEAR-MISS iff PF (train+holdout) + DSR clear and exactly ONE structural
    gate fails. Else NONE.

    .venv/bin/python -m tools.eval.phase21_gauntlet.S04_auction_concession.run --prior-trials 88
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from tools.eval.phase16.costs import CostModel, Slippage
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

_P21 = "/home/tradeflow/tradeflow/research/data/phase21"
AUCTIONS_CSV = f"{_P21}/auctions.csv"
LEG_CSV = {
    "5Y": f"{_P21}/FVX_pricereturn_daily.csv",
    "10Y": "/home/tradeflow/tradeflow/research/data/phase19/UST10Y_pricereturn_daily.csv",
    "30Y": f"{_P21}/TYX_pricereturn_daily.csv",
}

SAMPLE_START = "1980-01-01"
TRAIN_END = "2015-01-01"
HOLDOUT_END = "2026-07-01"
RECENCY_2015 = "2015-01-01"
RECENCY_2022 = "2022-01-01"
CRISIS_WINDOWS = [("2008-09-01", "2009-06-30"), ("2020-02-15", "2020-05-01")]

TICK = {"5Y": 0.0078125, "10Y": 0.015625, "30Y": 0.03125}
MULT = 1000.0
COMM_SIDE = 1.45

VARIANTS = ["concession_T1", "concession_T2", "reversal_T3", "combined"]
PLACEBO_SHIFT_TD = 7


@dataclass(frozen=True)
class Leg:
    slip: Slippage
    cost: CostModel

    def pnl_usd(self, entry_raw: float, exit_raw: float, direction: int) -> float:
        e = self.slip.adjust_entry(entry_raw, direction)
        x = self.slip.adjust_exit(exit_raw, direction, "market")
        return self.cost.net_pnl_usd(e, x, direction, contracts=1)


def _make_legs(scale: float = 1.0) -> dict[str, Leg]:
    out = {}
    for tenor, tick in TICK.items():
        out[tenor] = Leg(
            Slippage(
                entry_ticks=scale,
                stop_ticks=scale,
                target_ticks=scale,
                market_exit_ticks=scale,
                tick_size=tick,
            ),
            CostModel(
                commission_per_side_per_ct=COMM_SIDE * scale, multiplier=MULT, tick_size=tick
            ),
        )
    return out


def _load_series(path: str) -> pd.Series:
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    s = df.set_index("time")["close"].astype(float).sort_index().dropna()
    return s[~s.index.duplicated(keep="first")]


def _load_auctions() -> pd.DataFrame:
    df = pd.read_csv(AUCTIONS_CSV)
    df["auction_date"] = pd.to_datetime(df["auction_date"], utc=True)
    return df


def _anchor_positions(series: pd.Series, dates: pd.Series) -> list[int]:
    """Map auction dates to index positions (last trading day <= date), deduped."""
    idx = series.index
    seen, out = set(), []
    for d in dates:
        i = int(idx.searchsorted(d.normalize(), side="right")) - 1
        if i > 0 and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _episodes_for_variant(
    series: pd.Series, anchors: list[int], variant: str, leg: Leg, tenor: str
) -> pd.DataFrame:
    idx = series.index
    rows = []
    for i_t in anchors:
        trades = []
        if variant in ("concession_T1", "combined"):
            trades.append((i_t - 1, i_t, -1))
        if variant == "concession_T2":
            trades.append((i_t - 2, i_t, -1))
        if variant in ("reversal_T3", "combined"):
            trades.append((i_t, i_t + 3, +1))
        for i_e, i_x, d in trades:
            if i_e < 0 or i_x >= len(series):
                continue
            rows.append(
                {
                    "pnl_usd": leg.pnl_usd(float(series.iloc[i_e]), float(series.iloc[i_x]), d),
                    "entry_time": idx[i_e],
                    "exit_time": idx[i_x],
                    "year": int(idx[i_x].year),
                    "tenor": tenor,
                }
            )
    return pd.DataFrame(rows, columns=["pnl_usd", "entry_time", "exit_time", "year", "tenor"])


def _build_ledger(
    legs_data: dict[str, pd.Series],
    anchors: dict[str, list[int]],
    variant: str,
    legs: dict[str, Leg],
) -> pd.DataFrame:
    parts = [_episodes_for_variant(legs_data[t], anchors[t], variant, legs[t], t) for t in LEG_CSV]
    led = pd.concat(parts, ignore_index=True).sort_values("entry_time").reset_index(drop=True)
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
        "exp_per_ct": round(s.expectancy_usd, 2),
        "net_usd": round(s.total_pnl_usd, 2),
        "win_rate": round(s.win_rate, 4),
        "sr_per_trade": round(s.sr_per_trade, 5),
        "max_dd_usd": round(s.max_drawdown_usd, 2),
        "per_year": {int(k): round(v, 2) for k, v in s.per_year.items()},
    }


# --------------------------------------------------------------------------- #
def _placebo(
    legs_data: dict[str, pd.Series],
    anchors: dict[str, list[int]],
    variant: str,
    legs: dict[str, Leg],
    real_ho_pf: float,
) -> dict:
    """K1: anchors shifted 7 td earlier; shifted anchors near ANY real auction dropped."""
    sh_anchors: dict[str, list[int]] = {}
    for t, al in anchors.items():
        real = set(al)
        near = set()
        for i in real:
            near.update(range(i - 3, i + 4))
        sh_anchors[t] = [
            i - PLACEBO_SHIFT_TD
            for i in al
            if (i - PLACEBO_SHIFT_TD) not in near and i - PLACEBO_SHIFT_TD > 2
        ]
    led = _build_ledger(legs_data, sh_anchors, variant, legs)
    _, ho = _split(led, SAMPLE_START, TRAIN_END, HOLDOUT_END)
    placebo_pf = compute_stats(ho).profit_factor
    _rp = real_ho_pf if math.isfinite(real_ho_pf) else 0.0
    _pp = placebo_pf if math.isfinite(placebo_pf) else 0.0
    return {
        "pass": bool(_rp - _pp > 0.10 and real_ho_pf >= PF_MIN),
        "real_holdout_pf": _pf(real_ho_pf),
        "placebo_holdout_pf": _pf(placebo_pf),
        "n_placebo_holdout": int(len(ho)),
        "reason": "auction-day PF must beat 7-td-earlier placebo by >0.10 AND clear 1.30",
    }


def _tenor_breadth(led: pd.DataFrame) -> dict:
    by = led.groupby("tenor")["pnl_usd"].agg(["mean", "count"])
    detail = {
        t: {"exp": round(float(r["mean"]), 2), "n": int(r["count"])} for t, r in by.iterrows()
    }
    ok = all(v["exp"] > 0 for v in detail.values()) and len(detail) == 3
    return {
        "pass": bool(ok),
        "per_tenor": detail,
        "reason": "expectancy must be positive in ALL of 5Y/10Y/30Y",
    }


def _recency(led: pd.DataFrame) -> dict:
    s15 = compute_stats(_window(led, RECENCY_2015, HOLDOUT_END))
    s22 = compute_stats(_window(led, RECENCY_2022, HOLDOUT_END))
    return {
        "pass": bool(s15.profit_factor >= PF_MIN and s22.profit_factor >= PF_MIN),
        "pf_2015plus": _pf(s15.profit_factor),
        "n_2015plus": s15.n_trades,
        "pf_2022plus": _pf(s22.profit_factor),
        "n_2022plus": s22.n_trades,
        "reason": "QT/supply era is where the thesis says it should be strongest",
    }


def _crisis_strip(led: pd.DataFrame) -> dict:
    mask = pd.Series(False, index=led.index)
    for lo, hi in CRISIS_WINDOWS:
        lo_t, hi_t = pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC")
        mask |= (led["exit_time"] >= lo_t) & (led["exit_time"] <= hi_t)
    stripped = led[~mask].reset_index(drop=True)
    tr, ho = _split(stripped, SAMPLE_START, TRAIN_END, HOLDOUT_END)
    pf_tr, pf_ho = compute_stats(tr).profit_factor, compute_stats(ho).profit_factor
    return {
        "pass": bool(pf_tr >= PF_MIN and pf_ho >= PF_MIN),
        "n_stripped": int(mask.sum()),
        "stripped_train_pf": _pf(pf_tr),
        "stripped_holdout_pf": _pf(pf_ho),
        "reason": "edge must not be crisis-tail compensation",
    }


def _cost_cliff(
    legs_data: dict[str, pd.Series], anchors: dict[str, list[int]], variant: str
) -> dict:
    cliff, series = None, []
    k = 0.5
    while k <= 8.0:
        led = _build_ledger(legs_data, anchors, variant, _make_legs(scale=k))
        _, ho = _split(led, SAMPLE_START, TRAIN_END, HOLDOUT_END)
        pf = compute_stats(ho).profit_factor
        series.append({"k": k, "holdout_pf": _pf(pf)})
        if cliff is None and pf < PF_MIN:
            cliff = k
        k = round(k + 0.5, 3)
    return {"cliff_multiple": cliff, "curve": series}


# --------------------------------------------------------------------------- #
def run_eval(*, prior_trials: int = 88) -> dict:
    print("[EVAL] S04 auction concession/reversal: loading auctions + 3 tenor indexes")
    auctions = _load_auctions()
    legs_data = {t: _load_series(p) for t, p in LEG_CSV.items()}
    legs = _make_legs(1.0)
    n_trials = prior_trials + len(VARIANTS)

    anchors: dict[str, list[int]] = {}
    for t in LEG_CSV:
        dates = auctions.loc[auctions["tenor_group"] == t, "auction_date"]
        anchors[t] = _anchor_positions(legs_data[t], dates)
    print(
        "[EVAL] anchors: "
        + ", ".join(f"{t}={len(a)}" for t, a in anchors.items())
        + f"; deflator n_trials={prior_trials}+{len(VARIANTS)}={n_trials}"
    )

    variants: dict[str, dict] = {}
    ledgers: dict[str, pd.DataFrame] = {}
    train_srs: list[float] = []
    for key in VARIANTS:
        led = _build_ledger(legs_data, anchors, key, legs)
        ledgers[key] = led
        tr, ho = _split(led, SAMPLE_START, TRAIN_END, HOLDOUT_END)
        s_tr, s_ho = compute_stats(tr), compute_stats(ho)
        variants[key] = {
            "train": _stats_dict(s_tr),
            "holdout": _stats_dict(s_ho),
            "train_gate": check_train(s_tr).reasons,
            "train_gate_pass": check_train(s_tr).passed,
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
    holdout_gate = check_holdout(s_tr, s_ho, dsr) if champ_train_pass else None
    ho_pass = bool(holdout_gate.passed) if holdout_gate else False

    kill1 = _placebo(legs_data, anchors, champ_key, legs, s_ho.profit_factor)
    kill2 = _tenor_breadth(champ_led)
    kill3 = _recency(champ_led)
    kill4 = _crisis_strip(champ_led)
    kill5 = _cost_cliff(legs_data, anchors, champ_key)

    verdict_pass = bool(
        champ_train_pass
        and ho_pass
        and kill1["pass"]
        and kill2["pass"]
        and kill3["pass"]
        and kill4["pass"]
    )
    structural_fails = []
    if not champ_train_pass:
        structural_fails += [f"train: {r}" for r in champ["train_gate"] if "<" in r or "FAIL" in r]
    if holdout_gate and not holdout_gate.passed:
        structural_fails += [
            f"holdout: {r}" for r in holdout_gate.reasons if "<" in r or "FAIL" in r
        ]
    for kname, k in (
        ("K1-placebo", kill1),
        ("K2-tenor-breadth", kill2),
        ("K3-recency", kill3),
        ("K4-crisis-strip", kill4),
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

    led0 = _build_ledger(legs_data, anchors, champ_key, _make_legs(scale=0.0))
    integrity = {
        "no_lookahead": "auction dates are announced days in advance (calendar known "
        "ex-ante); entries at closes at fixed offsets",
        "holdout_sealed_until_champion": True,
        "champion_selected_on": "train_profit_factor (pre-registered)",
        "costs_applied": bool(
            abs(float(led0["pnl_usd"].sum()) - float(champ_led["pnl_usd"].sum())) > 1e-6
        ),
        "gross_total_usd": round(float(led0["pnl_usd"].sum()), 2),
        "net_total_usd": round(float(champ_led["pnl_usd"].sum()), 2),
        "proxy_label": "yield-derived price indexes (D_mod constant; magnitude-only); "
        "2Y/3Y/7Y auctions excluded (no free index proxy)",
        "monotonicity_na": "no ex-ante magnitude (bid-to-cover is post-auction)",
    }

    return {
        "phase": "21-S04-auction-concession",
        "verdict": verdict,
        "research_only": True,
        "data": {
            "auctions_total": int(len(auctions)),
            "per_tenor_anchor_days": {t: len(a) for t, a in anchors.items()},
            "spans": {
                t: [str(s.index[0].date()), str(s.index[-1].date())] for t, s in legs_data.items()
            },
        },
        "split": {"train": [SAMPLE_START, TRAIN_END], "holdout": [TRAIN_END, HOLDOUT_END]},
        "costs": {
            "ticks": TICK,
            "mult": MULT,
            "comm_per_side_usd": COMM_SIDE,
            "source": "CME treasury specs + phase19 IBKR fixed-tier commission; "
            "1-tick adverse slippage/side; cost-cliff bounds tier uncertainty",
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
            "1_placebo_shift7td": kill1,
            "2_tenor_breadth": kill2,
            "3_recency_2015_2022": kill3,
            "4_crisis_strip": kill4,
            "5_cost_cliff": kill5,
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
    ap.add_argument("--prior-trials", type=int, default=88)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    res = run_eval(prior_trials=args.prior_trials)
    out = (
        args.out
        or "/home/tradeflow/tradeflow/tools/eval/phase21_gauntlet/"
        "S04_auction_concession/results.json"
    )
    with open(out, "w") as fh:
        json.dump(res, fh, indent=2)
    print(f"[EVAL] wrote {out} — verdict {res['verdict']}")


if __name__ == "__main__":
    main()
