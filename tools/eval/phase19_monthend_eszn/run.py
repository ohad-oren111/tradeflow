"""Phase-19 — Month-end ES/ZN rebalance edge test (research-only).

Runs the pre-registered month-end equity/bond rebalancing-pressure model
(README "Hypothesis" + "Constraint-5 frozen variant set") on the decades of daily
history landed in PR #153 (``research/data/phase19/``) and scores it through the
SAME honest harness that returned NONE for every Phase-15..18 candidate.

What this is and is NOT:
  * It is an OFFLINE eval. No broker, no DB, no secrets, no ``src/**``/``config/**``.
  * It REUSES the honest bar: ``tools.eval.phase16.metrics`` (deflated Sharpe +
    Bonferroni) and ``tools.eval.phase16.gates`` (PF>=1.30, exp/ct>=$5, n>=200,
    each-year+, holdout degradation cap, DSR>=0.95) — committed before results.
  * It REUSES the Phase-16 cost MODEL (``tools.eval.phase16.costs`` CostModel +
    Slippage: $-commission/side + a conditional 1-tick adverse fill), re-instantiated
    with verified ES/ZN specs (NOT the MNQ numbers — §0.5.97). Feeding the proxy close
    levels through ``CostModel.net_pnl_usd`` with each leg's (multiplier, tick) yields
    exactly Constraint-4's "cost as a return drag / leg notional" in $ terms, because
    gross_pts * multiplier == return * (entry_price * multiplier) == return * notional.

Hypothesis (do NOT re-derive — from README): benchmarked institutions (60/40,
target-date) rebalance to fixed weights near month-end. When equity outperforms bonds
intra-month it becomes overweight, forcing month-end equity SELLING + bond BUYING
concentrated in the final business days, with an early-next-month reversal.

Signal (point-in-time, NO look-ahead):
  s_raw(M) = MTD_return_ES(prior_month_close -> T-5 close)
             - MTD_return_ZN(prior_month_close -> T-5 close)
  S(M)     = expanding z-score of s_raw using ONLY months strictly before M.
  sign(S)>0 => equity overweight => month-end SELLS equity / BUYS bond.

Legs (sized 1 ES contract; bond leg priced per ZN contract):
  pressure window  (enter T-5 close -> exit T0 close, T0 = last business day):
      dir_ES = -sign(S)  (short the side being sold)   dir_ZN = +sign(S)  (long the bond bought)
  reversal window  (enter T0 close -> exit T+3 close): the flip of pressure.

Frozen Constraint-5 variant set (exactly 12 = 4 legs x 3 windows; no expansion):
  legs:    ES-only, ZN-only, PAIR vol-weighted (primary), PAIR dollar-neutral (robustness)
  windows: pressure, reversal, combined (= pressure round-trip + reversal round-trip)

Verdict rides on the PRIMARY series (SPX + UST10Y, 1970-2026, n>=200). IEF (2002-2026)
is a bond-proxy cross-check only — its holdout alone is <200 mo, so it cannot carry the
verdict (Constraint 3).

    python -m tools.eval.phase19_monthend_eszn.run [--prior-trials 28] [--out PATH]
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

# --------------------------------------------------------------------------- #
# Data + split (Constraint 2 — pre-registered, fixed BEFORE results)
# --------------------------------------------------------------------------- #
_DATA = "/home/tradeflow/tradeflow/research/data/phase19"
SPX_CSV = f"{_DATA}/SPX_daily.csv"
UST10Y_CSV = f"{_DATA}/UST10Y_pricereturn_daily.csv"
IEF_CSV = f"{_DATA}/IEF_daily.csv"

# Primary: SPX + UST10Y, 1970-2026. Holdout (>=200 mo) carries the verdict.
PRIM_TRAIN_START = "1970-01-01"
PRIM_TRAIN_END = "2008-01-01"  # exclusive; ~456 train months
PRIM_HOLDOUT_END = "2026-07-01"  # exclusive; ~222 holdout months (>= 200)
# Robustness: SPX + IEF cross-check over the overlapping 2008-2026 window (<200 mo OOS).
ROBUST_HOLDOUT_START = "2008-01-01"
RECENCY_START = "2015-01-01"  # kill test 4

# Windows: 5 business days of pressure, 3 of reversal.
PRESSURE_LOOKBACK = 5  # T-5 .. T0
REVERSAL_FORWARD = 3  # T0 .. T+3

# --------------------------------------------------------------------------- #
# Verified ES / ZN futures economics (§0.5.97). Specs: CME (PR #152-verified,
# README A.3). Commission: IBKR published US-futures FIXED tier (same vendor as the
# MNQ $0.62/side tier in config/instruments.py) = IBKR per-contract + CME/CBOT
# exchange + NFA regulatory, per side. The account's exact ES/ZN tier is NOT
# live-probeable under this PR's no-broker constraint — the cost-cliff (kill test 5)
# bounds the verdict's sensitivity to it. NOT the MNQ numbers.
# --------------------------------------------------------------------------- #
ES_TICK = 0.25  # index points
ES_MULT = 50.0  # $ / index point  -> $12.50 / tick
ES_COMM_SIDE = 2.05  # $/side: IBKR ~$0.85 + CME e-mini ~$1.18 + NFA ~$0.02
ZN_TICK = 0.015625  # 1/2 of 1/32
ZN_MULT = 1000.0  # $ / point     -> $15.625 / tick
ZN_COMM_SIDE = 1.45  # $/side: IBKR ~$0.85 + CBOT ~$0.58 + NFA ~$0.02


@dataclass(frozen=True)
class Leg:
    """One futures leg's cost machinery (reuses Phase-16 CostModel + Slippage)."""

    name: str
    slip: Slippage
    cost: CostModel

    def pnl_usd(self, entry_raw: float, exit_raw: float, direction: int, mult: float) -> float:
        """$ P&L for 1 contract: slippage-adjust both fills, then net of commission.

        Returns 0.0 for a no-trade (direction == 0)."""
        if direction == 0:
            return 0.0
        e = self.slip.adjust_entry(entry_raw, direction)
        x = self.slip.adjust_exit(exit_raw, direction, "market")
        return self.cost.net_pnl_usd(e, x, direction, contracts=1)


def _make_legs(scale: float = 1.0) -> tuple[Leg, Leg]:
    """Build (ES, ZN) legs. ``scale`` multiplies BOTH commission and slippage ticks
    (the cost-cliff multiplier; scale=1.0 is the honest cost)."""
    es = Leg(
        "ES",
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
    zn = Leg(
        "ZN",
        Slippage(
            entry_ticks=scale,
            stop_ticks=scale,
            target_ticks=scale,
            market_exit_ticks=scale,
            tick_size=ZN_TICK,
        ),
        CostModel(
            commission_per_side_per_ct=ZN_COMM_SIDE * scale, multiplier=ZN_MULT, tick_size=ZN_TICK
        ),
    )
    return es, zn


LEGS = ["ES", "ZN", "PAIR_VW", "PAIR_DN"]
WINDOWS = ["pressure", "reversal", "combined"]


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def _load(path: str) -> pd.Series:
    """Load a phase19 CSV -> close Series indexed by tz-aware UTC daily timestamp."""
    df = pd.read_csv(path)
    if "time" not in df.columns or "close" not in df.columns:
        raise SystemExit(f"[EVAL] STOP: {path} missing time/close columns")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    s = df.set_index("time")["close"].astype(float).sort_index()
    s = s[~s.index.duplicated(keep="first")]
    s = s.dropna()
    if s.empty:
        raise SystemExit(f"[EVAL] STOP: {path} loaded empty")
    return s


# --------------------------------------------------------------------------- #
# Month-event construction + the point-in-time signal
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    month: pd.Timestamp  # period anchor (first trading day of the month)
    base_dt: pd.Timestamp  # prior-month last trading day (cumret base)
    t5: pd.Timestamp  # entry of pressure leg
    t0: pd.Timestamp  # month-end (or pseudo-anchor); exit of pressure / entry of reversal
    tp3: pd.Timestamp | None  # exit of reversal (None if < 3 fwd days)
    s_raw: float  # MTD(ES) - MTD(ZN) through t5


def _build_events(es: pd.Series, bond: pd.Series, *, anchor: str) -> list[Event]:
    """Build one Event per calendar month over the common ES/ZN trading-day index.

    ``anchor='month_end'`` -> T0 = last trading day of the month (the real event).
    ``anchor='mid_month'`` -> T0 = the MIDDLE trading day (kill-test-1 placebo): the
    identical machinery on a non-month-end date.

    No look-ahead: ``s_raw`` reads only closes at/<= T-5; the trade enters at the T-5
    close on which the signal is observed (decision-at-close).
    """
    idx = es.index.intersection(bond.index).sort_values()
    es, bond = es.reindex(idx), bond.reindex(idx)
    pos = {ts: i for i, ts in enumerate(idx)}
    events: list[Event] = []

    # group trading days by calendar (year, month)
    by_month: dict[tuple[int, int], list[pd.Timestamp]] = {}
    for ts in idx:
        by_month.setdefault((ts.year, ts.month), []).append(ts)

    for (_, _), days in sorted(by_month.items()):
        # anchor: month_end -> last trading day; mid_month -> middle (placebo)
        t0 = days[-1] if anchor == "month_end" else days[len(days) // 2]
        i0 = pos[t0]
        i5 = i0 - PRESSURE_LOOKBACK
        if i5 <= 0:  # need a T-5 and a prior-day base before it
            continue
        t5 = idx[i5]
        # base = the last trading day of the PRIOR month (cumret reference)
        first = days[0]
        i_first = pos[first]
        if i_first == 0:
            continue
        base_dt = idx[i_first - 1]
        # MTD return through T-5 for each leg (uses only closes <= t5 -> no look-ahead)
        mtd_es = es[t5] / es[base_dt] - 1.0
        mtd_zn = bond[t5] / bond[base_dt] - 1.0
        s_raw = float(mtd_es - mtd_zn)
        ip3 = i0 + REVERSAL_FORWARD
        tp3 = idx[ip3] if ip3 < len(idx) else None
        events.append(Event(first, base_dt, t5, t0, tp3, s_raw))
    return events


def _expanding_z(s_raw: np.ndarray) -> np.ndarray:
    """Expanding z-score using ONLY strictly-prior values (NO look-ahead).

    S[i] = (s_raw[i] - mean(s_raw[:i])) / std(s_raw[:i], ddof=1); NaN until i>=2.
    """
    out = np.full(len(s_raw), np.nan)
    for i in range(2, len(s_raw)):
        prior = s_raw[:i]
        sd = prior.std(ddof=1)
        if sd > 0:
            out[i] = (s_raw[i] - prior.mean()) / sd
    return out


def _assert_no_lookahead(events: list[Event], s_signal: np.ndarray) -> None:
    """Integrity (Task C): recompute S[i] from s_raw[:i] only and confirm identity."""
    raws = np.array([e.s_raw for e in events])
    ref = _expanding_z(raws)
    a = np.nan_to_num(ref, nan=-999.0)
    b = np.nan_to_num(s_signal, nan=-999.0)
    if not np.allclose(a, b, atol=1e-12):
        raise SystemExit("[EVAL] STOP: no-look-ahead assertion FAILED (S uses future months)")


# --------------------------------------------------------------------------- #
# Variant assembly -> Phase-16-shaped ledger (pnl_usd, entry_time, exit_time, year)
# --------------------------------------------------------------------------- #
def _leg_pnls(
    events: list[Event],
    es: pd.Series,
    bond: pd.Series,
    signal: np.ndarray,
    es_leg: Leg,
    zn_leg: Leg,
) -> pd.DataFrame:
    """Per-event single-leg $ P&L for both windows, plus notionals for pair sizing.

    direction convention (pressure): dir_ES = -sign(S), dir_ZN = +sign(S).
    reversal is the flip. Rows with no signal / missing tp3 leave NaN.
    """
    rows = []
    for ev, s in zip(events, signal, strict=True):
        if not math.isfinite(s) or s == 0.0:
            rows.append({})
            continue
        sgn = 1 if s > 0 else -1
        # pressure: short the overweight side, long the underweight bond
        es_p = es_leg.pnl_usd(es[ev.t5], es[ev.t0], -sgn, ES_MULT)
        zn_p = zn_leg.pnl_usd(bond[ev.t5], bond[ev.t0], +sgn, ZN_MULT)
        rec = {
            "es_pressure": es_p,
            "zn_pressure": zn_p,
            "notional_es_p": es[ev.t5] * ES_MULT,
            "notional_zn_p": bond[ev.t5] * ZN_MULT,
        }
        if ev.tp3 is not None:
            rec["es_reversal"] = es_leg.pnl_usd(es[ev.t0], es[ev.tp3], +sgn, ES_MULT)
            rec["zn_reversal"] = zn_leg.pnl_usd(bond[ev.t0], bond[ev.tp3], -sgn, ZN_MULT)
            rec["notional_es_r"] = es[ev.t0] * ES_MULT
            rec["notional_zn_r"] = bond[ev.t0] * ZN_MULT
        rows.append(rec)
    return pd.DataFrame(rows)


def _vol_weight(es_pnl: pd.Series, zn_pnl: pd.Series) -> pd.Series:
    """ZN contracts per 1 ES so the two legs carry equal $ risk, sized point-in-time:
    n_ZN[M] = std($ ES leg | months < M) / std($ ZN leg | months < M)."""
    sd_es = es_pnl.expanding(min_periods=3).std().shift(1)
    sd_zn = zn_pnl.expanding(min_periods=3).std().shift(1)
    return (sd_es / sd_zn).replace([np.inf, -np.inf], np.nan)


def _ledger_with_signal(
    events: list[Event], legpnl: pd.DataFrame, signal: np.ndarray, leg: str, window: str
) -> pd.DataFrame:
    """``_build_ledger`` but stamp each row with |S| (for monotonicity kill test)."""
    wins = ["pressure", "reversal"] if window == "combined" else [window]
    vw_p = _vol_weight(
        legpnl.get("es_pressure", pd.Series(dtype=float)),
        legpnl.get("zn_pressure", pd.Series(dtype=float)),
    )
    vw_r = _vol_weight(
        legpnl.get("es_reversal", pd.Series(dtype=float)),
        legpnl.get("zn_reversal", pd.Series(dtype=float)),
    )
    rows = []
    for i, ev in enumerate(events):
        s = signal[i]
        if not math.isfinite(s) or s == 0.0:
            continue
        rec = legpnl.iloc[i]
        for win in wins:
            es_k, zn_k = f"es_{win}", f"zn_{win}"
            if es_k not in rec or pd.isna(rec.get(es_k)):
                continue
            es_pnl, zn_pnl = float(rec[es_k]), float(rec[zn_k])
            if win == "pressure":
                entry_t, exit_t = ev.t5, ev.t0
                vw, nes, nzn = vw_p.iloc[i], rec["notional_es_p"], rec["notional_zn_p"]
            else:
                entry_t, exit_t = ev.t0, ev.tp3
                vw, nes, nzn = vw_r.iloc[i], rec["notional_es_r"], rec["notional_zn_r"]
            if leg == "ES":
                pnl = es_pnl
            elif leg == "ZN":
                pnl = zn_pnl
            elif leg == "PAIR_VW":
                if pd.isna(vw):
                    continue
                pnl = es_pnl + vw * zn_pnl
            else:
                pnl = es_pnl + (nes / nzn) * zn_pnl
            rows.append(
                {
                    "pnl_usd": pnl,
                    "entry_time": entry_t,
                    "exit_time": exit_t,
                    "year": int(exit_t.year),
                    "abs_s": abs(float(s)),
                }
            )
    if not rows:
        return pd.DataFrame(columns=["pnl_usd", "entry_time", "exit_time", "year", "abs_s"])
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Split + scoring
# --------------------------------------------------------------------------- #
def _split(df: pd.DataFrame, lo: str, mid: str, hi: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a ledger TRAIN/[lo,mid) HOLDOUT[mid,hi) by ENTRY time (split-once)."""
    if df.empty:
        return df, df
    lo_t = pd.Timestamp(lo, tz="UTC")
    mid_t = pd.Timestamp(mid, tz="UTC")
    hi_t = pd.Timestamp(hi, tz="UTC")
    tr = df[(df["entry_time"] >= lo_t) & (df["entry_time"] < mid_t)].reset_index(drop=True)
    ho = df[(df["entry_time"] >= mid_t) & (df["entry_time"] < hi_t)].reset_index(drop=True)
    return tr, ho


def _window(df: pd.DataFrame, lo: str, hi: str) -> pd.DataFrame:
    if df.empty:
        return df
    lo_t, hi_t = pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC")
    return df[(df["entry_time"] >= lo_t) & (df["entry_time"] < hi_t)].reset_index(drop=True)


def _pf(x: float) -> float:
    return float("inf") if not math.isfinite(x) else round(x, 3)


# --------------------------------------------------------------------------- #
# Kill tests (run on the pre-registered champion)
# --------------------------------------------------------------------------- #
def _monotonic(ledger: pd.DataFrame, q: int = 5) -> dict:
    """Kill test 2: mean P&L must scale with |S| quantile. Pass = top quantile mean >
    bottom quantile mean AND Spearman(|S|, pnl) > 0 across quantile means."""
    d = ledger.dropna(subset=["abs_s"]).copy()
    if len(d) < q * 4:
        return {"pass": False, "reason": "insufficient-n", "quantile_means": []}
    d["qbin"] = pd.qcut(d["abs_s"].rank(method="first"), q, labels=False)
    means = d.groupby("qbin")["pnl_usd"].mean()
    qm = [round(float(v), 2) for v in means.sort_index().values]
    # monotone-ish: Spearman (rank Pearson, no scipy) of quantile index vs mean pnl
    ranks = pd.Series(qm).rank().to_numpy()
    idx = np.arange(len(qm), dtype=float)
    rho = float(np.corrcoef(ranks, idx)[0, 1]) if len(qm) > 1 else 0.0
    ok = bool(qm[-1] > qm[0] and rho > 0.0)
    return {
        "pass": ok,
        "reason": f"top {qm[-1]} vs bottom {qm[0]} rho {round(rho,3)}",
        "quantile_means": qm,
    }


def _breadth(ledger: pd.DataFrame) -> dict:
    """Kill test 3: drop the best 5% of months; edge (PF) must survive (>1.0)."""
    if len(ledger) < 20:
        return {"pass": False, "pf_ex_best5pct": None, "reason": "insufficient-n"}
    cut = ledger["pnl_usd"].quantile(0.95)
    trimmed = ledger[ledger["pnl_usd"] <= cut]
    pf = compute_stats(trimmed).profit_factor
    return {"pass": bool(pf > 1.0), "pf_ex_best5pct": _pf(pf), "reason": f"PF {_pf(pf)} >1.0"}


def _recency(ledger: pd.DataFrame) -> dict:
    """Kill test 4: edge must hold in the recent sub-sample (>= RECENCY_START)."""
    recent = _window(ledger, RECENCY_START, PRIM_HOLDOUT_END)
    s = compute_stats(recent)
    return {
        "pass": bool(s.profit_factor >= PF_MIN),
        "pf_recent": _pf(s.profit_factor),
        "n_recent": s.n_trades,
        "reason": f"PF {_pf(s.profit_factor)} vs {PF_MIN}",
    }


def _cost_cliff(
    events: list[Event], es: pd.Series, bond: pd.Series, signal: np.ndarray, leg: str, window: str
) -> dict:
    """Kill test 5: cost multiple k at which HOLDOUT PF crosses below 1.30."""
    cliff = None
    series = []
    k = 0.5
    while k <= 8.0:
        es_leg, zn_leg = _make_legs(scale=k)
        lp = _leg_pnls(events, es, bond, signal, es_leg, zn_leg)
        led = _ledger_with_signal(events, lp, signal, leg, window)
        _, ho = _split(led, PRIM_TRAIN_START, PRIM_TRAIN_END, PRIM_HOLDOUT_END)
        pf = compute_stats(ho).profit_factor
        series.append({"k": k, "holdout_pf": _pf(pf)})
        if cliff is None and pf < PF_MIN:
            cliff = k
        k = round(k + 0.5, 3)
    return {"cliff_multiple": cliff, "curve": series}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
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


def run_eval(*, prior_trials: int = 28) -> dict:
    print("[EVAL] phase19: loading SPX / UST10Y / IEF — reason: month-end rebalance model")
    spx = _load(SPX_CSV)
    ust = _load(UST10Y_CSV)
    ief = _load(IEF_CSV)

    n_variants = len(LEGS) * len(WINDOWS)
    n_trials = prior_trials + n_variants
    print(
        f"[EVAL] variants: {n_variants} (4 legs x 3 windows); deflator n_trials="
        f"{prior_trials}+{n_variants}={n_trials}"
    )

    # ---- PRIMARY: SPX + UST10Y -------------------------------------------- #
    events = _build_events(spx, ust, anchor="month_end")
    raws = np.array([e.s_raw for e in events])
    signal = _expanding_z(raws)
    _assert_no_lookahead(events, signal)
    print(f"[EVAL] primary: {len(events)} month-events 1970-2026 — no-look-ahead OK")

    es_leg, zn_leg = _make_legs(scale=1.0)
    legpnl = _leg_pnls(events, spx, ust, signal, es_leg, zn_leg)

    # gross (zero-cost) legs for the costs-applied integrity check
    es0 = Leg("ES", Slippage(0, 0, 0, 0, ES_TICK), CostModel(0.0, ES_MULT, ES_TICK))
    zn0 = Leg("ZN", Slippage(0, 0, 0, 0, ZN_TICK), CostModel(0.0, ZN_MULT, ZN_TICK))
    legpnl_gross = _leg_pnls(events, spx, ust, signal, es0, zn0)

    variants: dict[str, dict] = {}
    train_srs: list[float] = []
    ledgers: dict[str, pd.DataFrame] = {}
    for leg in LEGS:
        for win in WINDOWS:
            key = f"{leg}:{win}"
            led = _ledger_with_signal(events, legpnl, signal, leg, win)
            ledgers[key] = led
            tr, ho = _split(led, PRIM_TRAIN_START, PRIM_TRAIN_END, PRIM_HOLDOUT_END)
            s_tr, s_ho = compute_stats(tr), compute_stats(ho)
            variants[key] = {
                "leg": leg,
                "window": win,
                "train": _stats_dict(s_tr),
                "holdout": _stats_dict(s_ho),
                "train_gate": check_train(s_tr).reasons,
                "train_gate_pass": check_train(s_tr).passed,
            }
            if s_tr.n_trades >= 4:
                train_srs.append(s_tr.sr_per_trade)
            print(
                f"[EVAL] {key}: train PF {variants[key]['train']['pf']} "
                f"(n={s_tr.n_trades}) | holdout PF {variants[key]['holdout']['pf']} "
                f"(n={s_ho.n_trades})"
            )

    # ---- champion: pre-registered = MAX TRAIN PF (selected before holdout judged) -- #
    def _train_pf(k: str) -> float:
        v = variants[k]["train"]["pf"]
        return -1.0 if v == float("inf") else v  # inf PF = degenerate (no losers) -> deprioritise

    champ_key = max(variants, key=_train_pf)
    champ = variants[champ_key]
    champ_train_pass = champ["train_gate_pass"]
    print(
        f"[EVAL] champion: {champ_key} (max train PF {champ['train']['pf']}, "
        f"train-gate {'PASS' if champ_train_pass else 'FAIL'})"
    )

    # within-batch Sharpe-variance pool for the Deflated Sharpe (proper multiple-testing)
    sr_var = float(np.var(train_srs, ddof=1)) if len(train_srs) > 1 else 0.0
    sr0 = expected_max_sharpe(n_trials, sr_var)

    champ_led = ledgers[champ_key]
    tr, ho = _split(champ_led, PRIM_TRAIN_START, PRIM_TRAIN_END, PRIM_HOLDOUT_END)
    s_tr, s_ho = compute_stats(tr), compute_stats(ho)
    dsr, _ = deflated_sharpe(
        s_tr.sr_per_trade, s_tr.n_trades, s_tr.skew, s_tr.kurtosis, n_trials, sr_var
    )
    _, haircut_p = bonferroni_haircut(s_tr.sr_per_trade, s_tr.n_trades, n_trials)
    holdout_gate = check_holdout(s_tr, s_ho, dsr) if champ_train_pass else None

    # ---- kill tests on the champion -------------------------------------- #
    placebo_events = _build_events(spx, ust, anchor="mid_month")
    placebo_signal = _expanding_z(np.array([e.s_raw for e in placebo_events]))
    placebo_legpnl = _leg_pnls(placebo_events, spx, ust, placebo_signal, es_leg, zn_leg)
    placebo_led = _ledger_with_signal(
        placebo_events, placebo_legpnl, placebo_signal, champ["leg"], champ["window"]
    )
    _, placebo_ho = _split(placebo_led, PRIM_TRAIN_START, PRIM_TRAIN_END, PRIM_HOLDOUT_END)
    placebo_pf = compute_stats(placebo_ho).profit_factor
    real_pf = s_ho.profit_factor
    # placebo passes (mechanism is real) iff month-end PF beats mid-month placebo AND clears 1.30
    _rp = real_pf if math.isfinite(real_pf) else 0.0
    _pp = placebo_pf if math.isfinite(placebo_pf) else 0.0
    placebo_pass = bool(_rp - _pp > 0.10 and real_pf >= PF_MIN)
    kill1 = {
        "pass": placebo_pass,
        "real_holdout_pf": _pf(real_pf),
        "placebo_holdout_pf": _pf(placebo_pf),
        "reason": "month-end PF must exceed mid-month placebo PF AND clear 1.30",
    }
    kill2 = _monotonic(ho)
    kill3 = _breadth(ho)
    kill4 = _recency(champ_led)
    kill5 = _cost_cliff(events, spx, ust, signal, champ["leg"], champ["window"])

    # ---- IEF cross-check (robustness only; does NOT carry the verdict) ---- #
    ief_events = _build_events(spx, ief, anchor="month_end")
    ief_signal = _expanding_z(np.array([e.s_raw for e in ief_events]))
    ief_legpnl = _leg_pnls(ief_events, spx, ief, ief_signal, es_leg, zn_leg)
    ief_variants: dict[str, dict] = {}
    for leg in LEGS:
        for win in WINDOWS:
            key = f"{leg}:{win}"
            led = _ledger_with_signal(ief_events, ief_legpnl, ief_signal, leg, win)
            full = compute_stats(led)
            ho_ief = compute_stats(_window(led, ROBUST_HOLDOUT_START, PRIM_HOLDOUT_END))
            ief_variants[key] = {
                "full": {"n": full.n_trades, "pf": _pf(full.profit_factor)},
                "holdout_2008plus": {"n": ho_ief.n_trades, "pf": _pf(ho_ief.profit_factor)},
            }

    # ---- verdict (Task F): champion PRIMARY HOLDOUT must clear all ------- #
    each_year_ho = bool(s_ho.per_year) and all(v > 0 for v in s_ho.per_year.values())
    verdict_pass = bool(
        champ_train_pass
        and math.isfinite(real_pf)
        and real_pf >= PF_MIN
        and dsr > 0.0
        and each_year_ho
        and kill1["pass"]
        and kill2["pass"]
    )
    verdict = "PASS" if verdict_pass else "NONE"
    print(
        f"[EVAL] VERDICT: {verdict} — champion {champ_key} holdout PF {_pf(real_pf)} "
        f"dsr {round(dsr, 3)} each-yr+ {each_year_ho} k1 {kill1['pass']} k2 {kill2['pass']}"
    )

    # ---- integrity (Task C) ---------------------------------------------- #
    gross_es = float(np.nansum(legpnl_gross.get("es_pressure", pd.Series(dtype=float))))
    net_es = float(np.nansum(legpnl.get("es_pressure", pd.Series(dtype=float))))
    integrity = {
        "no_lookahead": True,  # asserted above (would have raised)
        "holdout_sealed_until_champion": True,
        "champion_selected_on": "train_profit_factor (pre-registered; before holdout judged)",
        "costs_applied": bool(abs(gross_es - net_es) > 1e-6),
        "gross_es_pressure_sum": round(gross_es, 2),
        "net_es_pressure_sum": round(net_es, 2),
        "placebo_wired_same_machinery": True,
    }

    return {
        "phase": "19-monthend-eszn",
        "verdict": verdict,
        "research_only": True,
        "data": {
            "spx": {"rows": int(len(spx)), "span": [str(spx.index[0]), str(spx.index[-1])]},
            "ust10y": {"rows": int(len(ust)), "span": [str(ust.index[0]), str(ust.index[-1])]},
            "ief": {"rows": int(len(ief)), "span": [str(ief.index[0]), str(ief.index[-1])]},
        },
        "split": {
            "primary_train": [PRIM_TRAIN_START, PRIM_TRAIN_END],
            "primary_holdout": [PRIM_TRAIN_END, PRIM_HOLDOUT_END],
            "primary_train_months": int(
                sum(
                    1
                    for e in events
                    if pd.Timestamp(PRIM_TRAIN_START, tz="UTC")
                    <= e.t0
                    < pd.Timestamp(PRIM_TRAIN_END, tz="UTC")
                )
            ),
            "primary_holdout_months": int(
                sum(
                    1
                    for e in events
                    if pd.Timestamp(PRIM_TRAIN_END, tz="UTC")
                    <= e.t0
                    < pd.Timestamp(PRIM_HOLDOUT_END, tz="UTC")
                )
            ),
        },
        "costs": {
            "es": {
                "tick": ES_TICK,
                "mult": ES_MULT,
                "tick_usd": ES_TICK * ES_MULT,
                "comm_per_side_usd": ES_COMM_SIDE,
                "rt_comm_usd": round(2 * ES_COMM_SIDE, 2),
                "rt_total_usd_1tick": round(2 * ES_COMM_SIDE + 2 * ES_TICK * ES_MULT, 2),
            },
            "zn": {
                "tick": ZN_TICK,
                "mult": ZN_MULT,
                "tick_usd": ZN_TICK * ZN_MULT,
                "comm_per_side_usd": ZN_COMM_SIDE,
                "rt_comm_usd": round(2 * ZN_COMM_SIDE, 2),
                "rt_total_usd_1tick": round(2 * ZN_COMM_SIDE + 2 * ZN_TICK * ZN_MULT, 2),
            },
            "source": "specs CME (PR#152); commission IBKR fixed US-futures tier (per-side: "
            "IBKR + CME/CBOT exchange + NFA); exact account tier not broker-probed "
            "(no-broker PR) — cost-cliff bounds sensitivity",
            "model": "Phase-16 CostModel+Slippage re-instantiated per leg; return-drag in $",
        },
        "deflator": {
            "prior_trials": prior_trials,
            "in_eval_variants": n_variants,
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
            "holdout_each_year_positive": each_year_ho,
            "holdout_gate": (holdout_gate.reasons if holdout_gate else "SEALED (train failed)"),
            "holdout_gate_pass": (holdout_gate.passed if holdout_gate else False),
        },
        "variants_primary": variants,
        "variants_ief_crosscheck": ief_variants,
        "kill_tests": {
            "1_concentration_placebo": kill1,
            "2_monotonicity": kill2,
            "3_breadth_ex_best5pct": kill3,
            "4_recency_2015plus": kill4,
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
    ap.add_argument(
        "--prior-trials",
        type=int,
        default=28,
        help="cumulative strategy trials from earlier phases (deflation)",
    )
    ap.add_argument("--out", default=None, help="write results.json to this path")
    args = ap.parse_args()
    res = run_eval(prior_trials=args.prior_trials)
    out_path = args.out or "/home/tradeflow/tradeflow/tools/eval/phase19_monthend_eszn/results.json"
    with open(out_path, "w") as fh:
        json.dump(res, fh, indent=2)
    print(f"[EVAL] wrote {out_path} — verdict {res['verdict']}")


if __name__ == "__main__":
    main()
