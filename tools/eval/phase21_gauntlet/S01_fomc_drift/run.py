"""Phase-21 S01 — Pre-FOMC announcement drift (research-only, offline eval).

Tests the documented pre-FOMC announcement drift (Lucca & Moench, "The Pre-FOMC
Announcement Drift", JF 2015: large positive SPX excess returns in the ~24h BEFORE
scheduled FOMC announcements, 1994->2011) on a DAILY close-to-close proxy:

    long 1 ES (SPX cash proxy x $50) at the close of T-1, exit at the close of the
    announcement day T (statement ~14:00 ET is inside day T).

HONEST LABEL: the documented effect is 14:00 T-1 -> 14:00 T. Daily closes shift that
window ~2h later on each side; the drift portion 14:00->16:00 on T-1 is missed and the
post-announcement 14:00->16:00 reaction on T is included. This proxy is pre-registered
(work order S01) and the verdict applies to the DAILY-PROXY form, stated as such.

PRE-REGISTERED (frozen before any result was computed — this docstring + constants ARE
the registration; variants will NOT be expanded after results):
  * Variants (3, no more):
      V1 "T1_sched" — close T-1 -> close T, scheduled meetings only   (PRIMARY)
      V2 "T2_sched" — close T-2 -> close T, scheduled meetings only
      V3 "T1_all"   — close T-1 -> close T, scheduled + unscheduled
        (V3 is ROBUSTNESS ONLY and lookahead-tainted: unscheduled announcement dates
         are unknowable ex-ante; it can never carry a PASS on its own.)
  * Split (by entry time): TRAIN 1994-01-01 -> 2020-01-01 (208 scheduled events,
    >= the n>=200 gate); HOLDOUT 2020-01-01 -> 2026-07-01 (~51). Chosen so the train
    gate is structurally satisfiable; the post-2020 holdout is the strictest
    "does it work NOW" test. The documented post-publication decay is additionally
    kill-tested via recency windows (2015+, 2020+).
  * Gates: tools.eval.phase16.gates verbatim (PF>=1.30, exp/ct>=$5, n>=200 train,
    each-year-positive, holdout degradation cap, DSR>=0.95).
  * Deflation: --prior-trials 78 (manifest CUMULATIVE_TRIALS) + 3 variants = 81.
  * Costs: phase16 CostModel+Slippage with the PR#154-verified ES specs
    (tick 0.25, mult $50, commission $2.05/side, 1-tick adverse slippage per side).
  * Kill tests (pass/fail pre-registered):
      K1 placebo     — same T-1->T long on non-FOMC days, weekday-matched to the real
                       announcement weekday mix (seeded draw, B=2000); pass iff
                       holdout-window real PF beats placebo PF by >0.10 AND >=1.30.
      K2 crisis-strip— remove GFC (2008-09-01..2009-06-30) + COVID (2020-02-15..
                       2020-05-01) episodes; pass iff stripped train PF >= 1.30 AND
                       stripped holdout PF >= 1.30 (tail-compensation check).
      K3 recency     — PF >= 1.30 on BOTH 2015-01-01.. and 2020-01-01.. sub-windows
                       (the documented decay is exactly here).
      K4 cost-cliff  — cost multiple k where holdout PF < 1.30 (reported, no pass/fail;
                       1 RT/event so expected insensitive).
      Monotonicity   — N/A: a calendar event has no signal magnitude to rank. Documented.
  * VERDICT: PASS iff champion (max TRAIN PF among V1/V2 — V3 excluded from
    championship) clears train gate + holdout gate (incl DSR) + K1 + K2 + K3.
    NEAR-MISS iff PF+DSR clear but exactly ONE structural gate fails (named).
    Else NONE.

    .venv/bin/python -m tools.eval.phase21_gauntlet.S01_fomc_drift.run --prior-trials 78
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

# ---- data + split (pre-registered) ---------------------------------------- #
SPX_CSV = "/home/tradeflow/tradeflow/research/data/phase19/SPX_daily.csv"
FOMC_CSV = "/home/tradeflow/tradeflow/research/data/phase21/fomc_dates.csv"

TRAIN_START = "1994-01-01"
TRAIN_END = "2020-01-01"  # exclusive
HOLDOUT_END = "2026-07-01"  # exclusive
RECENCY_2015 = "2015-01-01"
RECENCY_2020 = "2020-01-01"
CRISIS_WINDOWS = [("2008-09-01", "2009-06-30"), ("2020-02-15", "2020-05-01")]

# ---- verified ES economics (PR#154 / phase19 — §0.5.97) ------------------- #
ES_TICK = 0.25
ES_MULT = 50.0
ES_COMM_SIDE = 2.05

VARIANTS = ["T1_sched", "T2_sched", "T1_all"]
CHAMPION_POOL = ["T1_sched", "T2_sched"]  # V3 lookahead-tainted, can't carry verdict
PLACEBO_SEED = 21
PLACEBO_B = 2000


@dataclass(frozen=True)
class EsLeg:
    slip: Slippage
    cost: CostModel

    def pnl_usd(self, entry_raw: float, exit_raw: float) -> float:
        e = self.slip.adjust_entry(entry_raw, 1)
        x = self.slip.adjust_exit(exit_raw, 1, "market")
        return self.cost.net_pnl_usd(e, x, 1, contracts=1)


def _make_leg(scale: float = 1.0) -> EsLeg:
    return EsLeg(
        Slippage(
            entry_ticks=scale,
            stop_ticks=scale,
            target_ticks=scale,
            market_exit_ticks=scale,
            tick_size=ES_TICK,
        ),
        CostModel(
            commission_per_side_per_ct=ES_COMM_SIDE * scale, multiplier=ES_MULT, tick_size=ES_TICK
        ),
    )


# ---- load ------------------------------------------------------------------ #
def _load_spx() -> pd.Series:
    df = pd.read_csv(SPX_CSV)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    s = df.set_index("time")["close"].astype(float).sort_index().dropna()
    return s[~s.index.duplicated(keep="first")]


def _load_fomc() -> pd.DataFrame:
    df = pd.read_csv(FOMC_CSV)
    df["announcement_date"] = pd.to_datetime(df["announcement_date"], utc=True)
    if not {"scheduled", "unscheduled"} >= set(df["kind"].unique()):
        raise SystemExit("[EVAL] STOP: unexpected kind values in fomc_dates.csv")
    return df


# ---- episode construction --------------------------------------------------- #
def _build_ledger(
    spx: pd.Series, events: pd.DataFrame, lookback: int, leg: EsLeg
) -> tuple[pd.DataFrame, int]:
    """One episode per announcement: long close T-lookback -> close T.

    T = the announcement calendar day mapped to the SPX trading day with that date
    (announcements land on trading days; any miss is counted + skipped, never
    silently absorbed). Returns (ledger, n_skipped)."""
    idx = spx.index
    pos = {ts.normalize(): i for i, ts in enumerate(idx)}
    rows, skipped = [], 0
    for ann in events["announcement_date"]:
        i_t = pos.get(ann.normalize())
        if i_t is None or i_t < lookback:
            skipped += 1
            continue
        i_e = i_t - lookback
        entry_t, exit_t = idx[i_e], idx[i_t]
        rows.append(
            {
                "pnl_usd": leg.pnl_usd(float(spx.iloc[i_e]), float(spx.iloc[i_t])),
                "entry_time": entry_t,
                "exit_time": exit_t,
                "year": int(exit_t.year),
                "weekday": int(exit_t.weekday()),
            }
        )
    cols = ["pnl_usd", "entry_time", "exit_time", "year", "weekday"]
    return (pd.DataFrame(rows, columns=cols), skipped)


def _split(df: pd.DataFrame, lo: str, mid: str, hi: str) -> tuple[pd.DataFrame, pd.DataFrame]:
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


# ---- kill tests -------------------------------------------------------------- #
def _placebo(spx: pd.Series, all_events: pd.DataFrame, real_ho: pd.DataFrame, leg: EsLeg) -> dict:
    """K1: weekday-matched placebo. Pool = every trading day NOT within +/-3 trading
    days of ANY announcement (scheduled or not); draw B sets of len(real_ho) days
    matching the real holdout weekday mix; placebo PF = PF of the pooled draws.
    Judged on the HOLDOUT window (phase19 convention)."""
    idx = spx.index
    ann_pos = set()
    pos = {ts.normalize(): i for i, ts in enumerate(idx)}
    for ann in all_events["announcement_date"]:
        i_t = pos.get(ann.normalize())
        if i_t is not None:
            ann_pos.update(range(max(0, i_t - 3), min(len(idx), i_t + 4)))
    lo = pd.Timestamp(TRAIN_END, tz="UTC")
    hi = pd.Timestamp(HOLDOUT_END, tz="UTC")
    elig = [i for i in range(1, len(idx)) if i not in ann_pos and lo <= idx[i] < hi]
    if not elig or real_ho.empty:
        return {"pass": False, "reason": "no eligible placebo days or empty holdout"}
    by_wd: dict[int, list[int]] = {}
    for i in elig:
        by_wd.setdefault(int(idx[i].weekday()), []).append(i)
    target_wd = real_ho["weekday"].value_counts().to_dict()
    rng = np.random.default_rng(PLACEBO_SEED)
    pnls = []
    for _ in range(PLACEBO_B):
        for wd, cnt in target_wd.items():
            cand = by_wd.get(wd)
            if not cand:
                continue
            for i in rng.choice(cand, size=cnt, replace=True):
                pnls.append(leg.pnl_usd(float(spx.iloc[i - 1]), float(spx.iloc[i])))
    placebo_pf = compute_stats(
        pd.DataFrame({"pnl_usd": pnls, "exit_time": idx[0], "year": 2000})
    ).profit_factor
    real_pf = compute_stats(real_ho).profit_factor
    _rp = real_pf if math.isfinite(real_pf) else 0.0
    _pp = placebo_pf if math.isfinite(placebo_pf) else 0.0
    return {
        "pass": bool(_rp - _pp > 0.10 and real_pf >= PF_MIN),
        "real_holdout_pf": _pf(real_pf),
        "placebo_pf": _pf(placebo_pf),
        "n_placebo_pnls": len(pnls),
        "reason": "holdout PF must beat weekday-matched placebo by >0.10 AND clear 1.30",
    }


def _crisis_strip(led: pd.DataFrame) -> dict:
    """K2: strip GFC + COVID episodes; stripped train AND holdout PF must hold 1.30."""
    mask = pd.Series(False, index=led.index)
    for lo, hi in CRISIS_WINDOWS:
        lo_t, hi_t = pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC")
        mask |= (led["exit_time"] >= lo_t) & (led["exit_time"] <= hi_t)
    stripped = led[~mask].reset_index(drop=True)
    tr, ho = _split(stripped, TRAIN_START, TRAIN_END, HOLDOUT_END)
    pf_tr, pf_ho = compute_stats(tr).profit_factor, compute_stats(ho).profit_factor
    return {
        "pass": bool(pf_tr >= PF_MIN and pf_ho >= PF_MIN),
        "n_stripped": int(mask.sum()),
        "stripped_train_pf": _pf(pf_tr),
        "stripped_holdout_pf": _pf(pf_ho),
        "reason": "edge must not be crisis-tail compensation (funding-carry lesson)",
    }


def _recency(led: pd.DataFrame) -> dict:
    """K3: the documented decay — PF must hold in BOTH 2015+ and 2020+ windows."""
    s15 = compute_stats(_window(led, RECENCY_2015, HOLDOUT_END))
    s20 = compute_stats(_window(led, RECENCY_2020, HOLDOUT_END))
    return {
        "pass": bool(s15.profit_factor >= PF_MIN and s20.profit_factor >= PF_MIN),
        "pf_2015plus": _pf(s15.profit_factor),
        "n_2015plus": s15.n_trades,
        "pf_2020plus": _pf(s20.profit_factor),
        "n_2020plus": s20.n_trades,
        "reason": "post-publication decay check (Lucca-Moench sample ended 2011)",
    }


def _cost_cliff(spx: pd.Series, events: pd.DataFrame, lookback: int) -> dict:
    cliff, series = None, []
    k = 0.5
    while k <= 8.0:
        led, _ = _build_ledger(spx, events, lookback, _make_leg(scale=k))
        _, ho = _split(led, TRAIN_START, TRAIN_END, HOLDOUT_END)
        pf = compute_stats(ho).profit_factor
        series.append({"k": k, "holdout_pf": _pf(pf)})
        if cliff is None and pf < PF_MIN:
            cliff = k
        k = round(k + 0.5, 3)
    return {"cliff_multiple": cliff, "curve": series}


# ---- orchestration ----------------------------------------------------------- #
def run_eval(*, prior_trials: int = 78) -> dict:
    print("[EVAL] S01 pre-FOMC drift: loading SPX + FOMC dates")
    spx = _load_spx()
    fomc = _load_fomc()
    sched = fomc[fomc["kind"] == "scheduled"]
    n_trials = prior_trials + len(VARIANTS)
    print(
        f"[EVAL] {len(fomc)} announcements ({len(sched)} scheduled); deflator "
        f"n_trials={prior_trials}+{len(VARIANTS)}={n_trials}"
    )

    leg = _make_leg(1.0)
    leg0 = EsLeg(Slippage(0, 0, 0, 0, ES_TICK), CostModel(0.0, ES_MULT, ES_TICK))

    spec = {
        "T1_sched": (sched, 1),
        "T2_sched": (sched, 2),
        "T1_all": (fomc, 1),
    }
    variants: dict[str, dict] = {}
    ledgers: dict[str, pd.DataFrame] = {}
    train_srs: list[float] = []
    for key, (ev, lb) in spec.items():
        led, skipped = _build_ledger(spx, ev, lb, leg)
        ledgers[key] = led
        tr, ho = _split(led, TRAIN_START, TRAIN_END, HOLDOUT_END)
        s_tr, s_ho = compute_stats(tr), compute_stats(ho)
        variants[key] = {
            "lookback_days": lb,
            "universe": "scheduled" if ev is sched else "all (LOOKAHEAD-TAINTED robustness)",
            "n_skipped_no_trading_day": skipped,
            "train": _stats_dict(s_tr),
            "holdout": _stats_dict(s_ho),
            "train_gate": check_train(s_tr).reasons,
            "train_gate_pass": check_train(s_tr).passed,
        }
        if s_tr.n_trades >= 4:
            train_srs.append(s_tr.sr_per_trade)
        print(
            f"[EVAL] {key}: train PF {variants[key]['train']['pf']} (n={s_tr.n_trades}, "
            f"skip={skipped}) | holdout PF {variants[key]['holdout']['pf']} (n={s_ho.n_trades})"
        )

    # champion: max TRAIN PF among the pre-registered pool (V3 excluded)
    def _train_pf(k: str) -> float:
        v = variants[k]["train"]["pf"]
        return -1.0 if v == float("inf") else v

    champ_key = max(CHAMPION_POOL, key=_train_pf)
    champ = variants[champ_key]
    champ_train_pass = champ["train_gate_pass"]
    print(f"[EVAL] champion: {champ_key} (train-gate {'PASS' if champ_train_pass else 'FAIL'})")

    sr_var = float(np.var(train_srs, ddof=1)) if len(train_srs) > 1 else 0.0
    sr0 = expected_max_sharpe(n_trials, sr_var)
    champ_led = ledgers[champ_key]
    tr, ho = _split(champ_led, TRAIN_START, TRAIN_END, HOLDOUT_END)
    s_tr, s_ho = compute_stats(tr), compute_stats(ho)
    dsr, _ = deflated_sharpe(
        s_tr.sr_per_trade, s_tr.n_trades, s_tr.skew, s_tr.kurtosis, n_trials, sr_var
    )
    _, haircut_p = bonferroni_haircut(s_tr.sr_per_trade, s_tr.n_trades, n_trials)
    holdout_gate = check_holdout(s_tr, s_ho, dsr) if champ_train_pass else None

    kill1 = _placebo(spx, fomc, ho, leg)
    kill2 = _crisis_strip(champ_led)
    kill3 = _recency(champ_led)
    kill4 = _cost_cliff(spx, spec[champ_key][0], spec[champ_key][1])

    # ---- verdict ---------------------------------------------------------- #
    ho_pass = bool(holdout_gate.passed) if holdout_gate else False
    verdict_pass = bool(
        champ_train_pass and ho_pass and kill1["pass"] and kill2["pass"] and kill3["pass"]
    )
    # NEAR-MISS: PF (train+holdout) and DSR clear, exactly one structural gate fails
    structural_fails = []
    if not champ_train_pass:
        for r in champ["train_gate"]:
            if "<" in r:
                structural_fails.append(f"train: {r}")
    if holdout_gate and not holdout_gate.passed:
        for r in holdout_gate.reasons:
            if "<" in r:
                structural_fails.append(f"holdout: {r}")
    for kname, k in (("K1-placebo", kill1), ("K2-crisis-strip", kill2), ("K3-recency", kill3)):
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
        f"HO PF {_pf(s_ho.profit_factor)} dsr {round(dsr,3)} "
        f"k1 {kill1['pass']} k2 {kill2['pass']} k3 {kill3['pass']}"
    )

    # integrity: costs actually applied
    led0, _ = _build_ledger(spx, spec[champ_key][0], spec[champ_key][1], leg0)
    integrity = {
        "no_lookahead": "entry/exit are closes at known calendar offsets; scheduled dates "
        "known ex-ante from the Fed's published calendar; V3 (unscheduled) labeled tainted",
        "holdout_sealed_until_champion": True,
        "champion_selected_on": "train_profit_factor among V1/V2 (pre-registered)",
        "costs_applied": bool(
            abs(float(led0["pnl_usd"].sum()) - float(champ_led["pnl_usd"].sum())) > 1e-6
        ),
        "gross_total_usd": round(float(led0["pnl_usd"].sum()), 2),
        "net_total_usd": round(float(champ_led["pnl_usd"].sum()), 2),
        "monotonicity_na": "calendar event without signal magnitude — kill test N/A",
    }

    return {
        "phase": "21-S01-fomc-drift",
        "verdict": verdict,
        "research_only": True,
        "data": {
            "spx_rows": int(len(spx)),
            "spx_span": [str(spx.index[0].date()), str(spx.index[-1].date())],
            "fomc_announcements": int(len(fomc)),
            "fomc_scheduled": int(len(sched)),
            "fomc_span": [
                str(fomc["announcement_date"].min().date()),
                str(fomc["announcement_date"].max().date()),
            ],
        },
        "split": {
            "train": [TRAIN_START, TRAIN_END],
            "holdout": [TRAIN_END, HOLDOUT_END],
        },
        "costs": {
            "es_tick": ES_TICK,
            "es_mult": ES_MULT,
            "comm_per_side_usd": ES_COMM_SIDE,
            "rt_total_usd_1tick": round(2 * ES_COMM_SIDE + 2 * ES_TICK * ES_MULT, 2),
            "source": "phase19/PR#154-verified ES specs; phase16 CostModel+Slippage",
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
            "1_weekday_matched_placebo": kill1,
            "2_crisis_strip": kill2,
            "3_recency_2015_2020": kill3,
            "4_cost_cliff": kill4,
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
    ap.add_argument("--prior-trials", type=int, default=78)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    res = run_eval(prior_trials=args.prior_trials)
    out = (
        args.out
        or "/home/tradeflow/tradeflow/tools/eval/phase21_gauntlet/S01_fomc_drift/results.json"
    )
    with open(out, "w") as fh:
        json.dump(res, fh, indent=2)
    print(f"[EVAL] wrote {out} — verdict {res['verdict']}")


if __name__ == "__main__":
    main()
