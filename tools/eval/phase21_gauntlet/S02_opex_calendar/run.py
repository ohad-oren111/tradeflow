"""Phase-21 S02 — OPEX calendar effect (research-only, offline eval).

HONEST LABEL (pre-registered): this tests the CALENDAR PATTERN around monthly equity
option expiration (3rd Friday) — documented as positive SPX returns into expiration
week and weak/negative returns the week after (option-hedging flow literature, e.g.
Stivers-Sun expiration-week studies). It does NOT test the dealer-gamma MECHANISM —
that needs options open-interest history, unobtainable free. The verdict applies to
the calendar form only.

Episodes (1 ES = SPX x $50, phase16 cost model, PR#154-verified ES specs):
    OPEX day  = 3rd Friday of the month, mapped to the last trading day <= that date
                (handles Good-Friday expirations).
    anchor    = 3rd Friday minus 7 calendar days (2nd Friday), same trading-day map.
    post end  = 3rd Friday plus 7 calendar days, same map.

PRE-REGISTERED (frozen before any result was computed; no expansion after results):
  * Variants (3):
      V1 "long_opex_week"  — long  anchor close -> OPEX close          (PRIMARY)
      V2 "short_post_week" — short OPEX close   -> post-end close
      V3 "combined"        — both round-trips per month
  * Sample: 1984-01-01 .. 2026-07-01 (index options trade from 1983; 1984 start is
    the first clean full year). ~509 months.
  * Split (by entry time): TRAIN 1984-01-01 -> 2012-01-01 (n ~ 336 >= 200);
    HOLDOUT 2012-01-01 -> 2026-07-01 (n ~ 174).
  * Gates: tools.eval.phase16.gates verbatim; deflation --prior-trials 81 + 3 = 84.
  * Champion: max TRAIN PF among V1/V2/V3.
  * Kill tests:
      K1 placebo Fridays — identical week-long trade anchored on the 1st, 2nd and
        4th Fridays (the three non-OPEX anchors, pooled); pass iff champion holdout
        PF beats placebo PF by >0.10 AND >= 1.30.
      K2 recency — PF >= 1.30 on BOTH 2015-01-01.. and 2020-01-01.. windows (the
        gamma-flow era is recent; a calendar edge that died is dead).
      K3 regime split — full-sample expectancy must be POSITIVE on BOTH sides of the
        SPX 200d-SMA regime (above / below) — else it is a regime artifact.
      K4 crisis-strip — GFC (2008-09-01..2009-06-30) + COVID (2020-02-15..2020-05-01)
        episodes removed; stripped train AND holdout PF >= 1.30.
      K5 cost-cliff — reported (no pass/fail).
      Monotonicity — N/A (calendar event, no magnitude). Documented.
  * VERDICT: PASS iff train gate + holdout gate (incl DSR>=0.95) + K1 + K2 + K3 + K4.
    NEAR-MISS iff PF (train+holdout) and DSR clear but exactly ONE structural gate
    fails. Else NONE.

    .venv/bin/python -m tools.eval.phase21_gauntlet.S02_opex_calendar.run --prior-trials 81
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
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

SPX_CSV = "/home/tradeflow/tradeflow/research/data/phase19/SPX_daily.csv"

SAMPLE_START = "1984-01-01"
TRAIN_END = "2012-01-01"  # exclusive
HOLDOUT_END = "2026-07-01"  # exclusive
RECENCY_2015 = "2015-01-01"
RECENCY_2020 = "2020-01-01"
CRISIS_WINDOWS = [("2008-09-01", "2009-06-30"), ("2020-02-15", "2020-05-01")]
SMA_DAYS = 200

ES_TICK = 0.25
ES_MULT = 50.0
ES_COMM_SIDE = 2.05

VARIANTS = ["long_opex_week", "short_post_week", "combined"]


@dataclass(frozen=True)
class EsLeg:
    slip: Slippage
    cost: CostModel

    def pnl_usd(self, entry_raw: float, exit_raw: float, direction: int) -> float:
        e = self.slip.adjust_entry(entry_raw, direction)
        x = self.slip.adjust_exit(exit_raw, direction, "market")
        return self.cost.net_pnl_usd(e, x, direction, contracts=1)


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


def _load_spx() -> pd.Series:
    df = pd.read_csv(SPX_CSV)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    s = df.set_index("time")["close"].astype(float).sort_index().dropna()
    return s[~s.index.duplicated(keep="first")]


def _nth_friday(year: int, month: int, n: int) -> dt.date:
    cal = calendar.Calendar()
    fridays = [d for d in cal.itermonthdates(year, month) if d.month == month and d.weekday() == 4]
    return fridays[n - 1]


def _last_td_at_or_before(idx: pd.DatetimeIndex, d: dt.date) -> int | None:
    """Index position of the last trading day <= calendar date d (None if before start)."""
    ts = pd.Timestamp(d, tz="UTC")
    i = idx.searchsorted(ts, side="right") - 1
    return int(i) if i >= 0 else None


# --------------------------------------------------------------------------- #
# Episode construction
# --------------------------------------------------------------------------- #
def _month_anchors(
    spx: pd.Series, *, friday_n: int, lo: str, hi: str
) -> list[tuple[int, int, int]]:
    """Per month in [lo, hi): (i_anchor, i_event, i_post) index positions where
    event = nth Friday (trading-day mapped), anchor = event-7cd, post = event+7cd."""
    idx = spx.index
    lo_d = pd.Timestamp(lo).date()
    hi_d = pd.Timestamp(hi).date()
    out = []
    y, m = lo_d.year, lo_d.month
    while dt.date(y, m, 1) < hi_d:
        ev_d = _nth_friday(y, m, friday_n)
        i_ev = _last_td_at_or_before(idx, ev_d)
        i_an = _last_td_at_or_before(idx, ev_d - dt.timedelta(days=7))
        i_po = _last_td_at_or_before(idx, ev_d + dt.timedelta(days=7))
        ok = i_ev is not None and i_an is not None and i_po is not None and i_an < i_ev < i_po
        if ok and lo_d <= idx[i_an].date() < hi_d:
            out.append((i_an, i_ev, i_po))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _build_ledger(spx: pd.Series, anchors: list[tuple[int, int, int]], variant: str, leg: EsLeg):
    idx = spx.index
    rows = []
    for i_an, i_ev, i_po in anchors:
        legs = []
        if variant in ("long_opex_week", "combined"):
            legs.append((i_an, i_ev, 1))
        if variant in ("short_post_week", "combined"):
            legs.append((i_ev, i_po, -1))
        for i_e, i_x, d in legs:
            rows.append(
                {
                    "pnl_usd": leg.pnl_usd(float(spx.iloc[i_e]), float(spx.iloc[i_x]), d),
                    "entry_time": idx[i_e],
                    "exit_time": idx[i_x],
                    "year": int(idx[i_x].year),
                    "entry_i": i_e,
                }
            )
    cols = ["pnl_usd", "entry_time", "exit_time", "year", "entry_i"]
    return pd.DataFrame(rows, columns=cols)


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
# Kill tests
# --------------------------------------------------------------------------- #
def _placebo_fridays(spx: pd.Series, variant: str, leg: EsLeg, real_ho_pf: float) -> dict:
    """K1: identical machinery anchored on the 1st, 2nd and 4th Fridays (pooled),
    judged on the holdout window."""
    pnls = []
    for n in (1, 2, 4):
        anchors = _month_anchors(spx, friday_n=n, lo=TRAIN_END, hi=HOLDOUT_END)
        led = _build_ledger(spx, anchors, variant, leg)
        pnls.append(led)
    pooled = pd.concat(pnls, ignore_index=True)
    placebo_pf = compute_stats(pooled).profit_factor
    _rp = real_ho_pf if math.isfinite(real_ho_pf) else 0.0
    _pp = placebo_pf if math.isfinite(placebo_pf) else 0.0
    return {
        "pass": bool(_rp - _pp > 0.10 and real_ho_pf >= PF_MIN),
        "real_holdout_pf": _pf(real_ho_pf),
        "placebo_pf_pooled_f124": _pf(placebo_pf),
        "n_placebo": int(len(pooled)),
        "reason": "holdout PF must beat 1st/2nd/4th-Friday placebo by >0.10 AND clear 1.30",
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
        "reason": "calendar edge must hold in the modern gamma-flow era",
    }


def _regime_split(spx: pd.Series, led: pd.DataFrame) -> dict:
    """K3: expectancy must be positive both above AND below the 200d SMA at entry."""
    sma = spx.rolling(SMA_DAYS).mean()
    above = []
    for _, r in led.iterrows():
        i = int(r["entry_i"])
        s = sma.iloc[i]
        above.append(bool(spx.iloc[i] > s) if math.isfinite(s) else None)
    led = led.assign(above=above).dropna(subset=["above"])
    e_above = float(led[led["above"] == True]["pnl_usd"].mean())  # noqa: E712
    e_below = float(led[led["above"] == False]["pnl_usd"].mean())  # noqa: E712
    n_above = int((led["above"] == True).sum())  # noqa: E712
    n_below = int((led["above"] == False).sum())  # noqa: E712
    return {
        "pass": bool(e_above > 0 and e_below > 0),
        "exp_above_sma200": round(e_above, 2),
        "n_above": n_above,
        "exp_below_sma200": round(e_below, 2),
        "n_below": n_below,
        "reason": "edge must not be a one-regime artifact",
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


def _cost_cliff(spx: pd.Series, anchors, variant: str) -> dict:
    cliff, series = None, []
    k = 0.5
    while k <= 8.0:
        led = _build_ledger(spx, anchors, variant, _make_leg(scale=k))
        _, ho = _split(led, SAMPLE_START, TRAIN_END, HOLDOUT_END)
        pf = compute_stats(ho).profit_factor
        series.append({"k": k, "holdout_pf": _pf(pf)})
        if cliff is None and pf < PF_MIN:
            cliff = k
        k = round(k + 0.5, 3)
    return {"cliff_multiple": cliff, "curve": series}


# --------------------------------------------------------------------------- #
def run_eval(*, prior_trials: int = 81) -> dict:
    print("[EVAL] S02 OPEX calendar: loading SPX")
    spx = _load_spx()
    n_trials = prior_trials + len(VARIANTS)
    anchors = _month_anchors(spx, friday_n=3, lo=SAMPLE_START, hi=HOLDOUT_END)
    print(
        f"[EVAL] {len(anchors)} OPEX months 1984->2026; deflator n_trials="
        f"{prior_trials}+{len(VARIANTS)}={n_trials}"
    )

    leg = _make_leg(1.0)
    leg0 = EsLeg(Slippage(0, 0, 0, 0, ES_TICK), CostModel(0.0, ES_MULT, ES_TICK))

    variants: dict[str, dict] = {}
    ledgers: dict[str, pd.DataFrame] = {}
    train_srs: list[float] = []
    for key in VARIANTS:
        led = _build_ledger(spx, anchors, key, leg)
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

    kill1 = _placebo_fridays(spx, champ_key, leg, s_ho.profit_factor)
    kill2 = _recency(champ_led)
    kill3 = _regime_split(spx, champ_led)
    kill4 = _crisis_strip(champ_led)
    kill5 = _cost_cliff(spx, anchors, champ_key)

    ho_pass = bool(holdout_gate.passed) if holdout_gate else False
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
        structural_fails += [f"train: {r}" for r in champ["train_gate"] if "<" in r]
    if holdout_gate and not holdout_gate.passed:
        structural_fails += [f"holdout: {r}" for r in holdout_gate.reasons if "<" in r]
    for kname, k in (
        ("K1-placebo", kill1),
        ("K2-recency", kill2),
        ("K3-regime", kill3),
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
        f"HO PF {_pf(s_ho.profit_factor)} dsr {round(dsr,3)} fails={structural_fails}"
    )

    led0 = _build_ledger(spx, anchors, champ_key, leg0)
    integrity = {
        "no_lookahead": "all anchors are deterministic calendar dates known ex-ante",
        "holdout_sealed_until_champion": True,
        "champion_selected_on": "train_profit_factor (pre-registered)",
        "costs_applied": bool(
            abs(float(led0["pnl_usd"].sum()) - float(champ_led["pnl_usd"].sum())) > 1e-6
        ),
        "gross_total_usd": round(float(led0["pnl_usd"].sum()), 2),
        "net_total_usd": round(float(champ_led["pnl_usd"].sum()), 2),
        "monotonicity_na": "calendar event without signal magnitude — kill test N/A",
        "honest_label": "calendar form only; gamma MECHANISM untested (needs options OI)",
    }

    return {
        "phase": "21-S02-opex-calendar",
        "verdict": verdict,
        "research_only": True,
        "data": {
            "spx_rows": int(len(spx)),
            "spx_span": [str(spx.index[0].date()), str(spx.index[-1].date())],
            "opex_months": len(anchors),
        },
        "split": {"train": [SAMPLE_START, TRAIN_END], "holdout": [TRAIN_END, HOLDOUT_END]},
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
            "1_placebo_fridays": kill1,
            "2_recency": kill2,
            "3_regime_split_sma200": kill3,
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
    ap.add_argument("--prior-trials", type=int, default=81)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    res = run_eval(prior_trials=args.prior_trials)
    out = (
        args.out
        or "/home/tradeflow/tradeflow/tools/eval/phase21_gauntlet/S02_opex_calendar/results.json"
    )
    with open(out, "w") as fh:
        json.dump(res, fh, indent=2)
    print(f"[EVAL] wrote {out} — verdict {res['verdict']}")


if __name__ == "__main__":
    main()
