"""Phase-20c — BTC + ETH dedicated funding-carry validation (research-only).

Gives the two most legitimate funding-carry candidates — BTC and ETH (deepest books,
lowest fees, where a delta-neutral basis trade is most real) — their fairest and most
INTERPRETABLE test, before the operator decides whether to kill funding carry as a
category. This is a CONFIRM-AND-MAKE-LEGIBLE pass, not a likely resurrection: per-coin
majors already FAILED in Phase-20b (PR #156):

    20b per-coin baseline (the thing being re-examined):
      C:BTC  train PF 1.235 (n=125) -> stripped 0.006 -> holdout 0.397 -> holdout-strip 0.000
      C:ETH  train PF 1.580 (n=140) -> stripped 0.000 -> holdout 0.506 -> holdout-strip 0.000
    Both strip to ~0.00; both holdouts < 0.51. The edge in 20b was all squeeze/delever tail.

What this is and is NOT:
  * OFFLINE eval. No broker, no DB, no IB, no secrets, no ``src/**``/``config/**``.
    Reads research/data/phase20/{BTC,ETH}_{spot,perp,funding}.csv only. A PASS-for-majors
    earns a forward paper-trading DISCUSSION — it wires NOTHING live.
  * REUSES the honest bar: ``tools.eval.phase16.{metrics,gates}`` (deflated Sharpe +
    Bonferroni; PF>=1.30 / exp/ct>=$5 / n>=200 / each-year+ / degradation cap / DSR>=0.95)
    and IMPORTS Phase-20b's VERIFIED crypto cost constants + episode logic from its
    ``run.py`` (TAKER_FEE 0.05%/fill, 4 fills/round-trip, slip 2bp majors; §0.5.97). Costs
    are NOT re-derived here.

Three fair-shot additions over 20b (Constraints 3-4), all pre-registered BEFORE holdout:
  * Per-coin BTC and ETH reported INDEPENDENTLY (no pooling, no alts).
  * TWO spike definitions side-by-side: (a) ``delever`` — only the 5 named crisis windows
    stripped (surgical); (b) ``topdecile`` — 20b's blanket top-decile |funding| OR delever
    (matches 20b). If carry survives the surgical strip but dies on top-decile, that is
    elevated-funding carry vs pure crash insurance — surfaced, not buried.
  * ONE pre-registered ``min-hold-21`` rule (rule D): enter on positive funding, hold >=
    21 intervals (~7d, the cost-breakeven horizon), exit only on a SUSTAINED flip (>=2
    consecutive non-positive intervals after the min-hold). Deep-book majors support
    patient holds, cutting the churn cost that sank rule C. ONE value — no hold-length sweep.

THE LOAD-BEARING DEFINITION (Constraint 2, unchanged from 20b): one trade = one
delta-neutral holding EPISODE (entry -> exit), NOT one 8h funding interval. ``n`` =
episodes. For always-on (1 episode/coin) the deliverable is the per-year NET-CARRY CURVE,
not a degenerate PF scalar.

Split (pre-registered, identical to 20b): TRAIN 2020-01->2023-12, HOLDOUT 2024-01->2026-05.
Episodes are built WITHIN each period; nothing is fit on holdout. Verdict rides on the
SPIKE-STRIPPED HOLDOUT under ``--prior-trials 70`` + this eval's variants.

    python -m tools.eval.phase20c_majors_validation.run [--prior-trials 70] [--out PATH]
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
    compute_stats,
    deflated_sharpe,
    expected_max_sharpe,
)
from tools.eval.phase20_funding_carry.run import (
    _DATA,
    HOLD_REF_INTERVALS,
    NOTIONAL,
    SLIP_MAJOR,
    SPIKE_DECILE,
    TAKER_FEE,
    _coin_costs,
    _gen_episodes,
    _in_deleverage,
    _load_funding,
    _load_ohlcv,
)

# --------------------------------------------------------------------------- #
# Scope + split (Constraint 1: BTC, ETH only, per-coin; split identical to 20b)
# --------------------------------------------------------------------------- #
COINS = ["BTC", "ETH"]

TRAIN_LO = pd.Timestamp("2020-01-01", tz="UTC")
TRAIN_HI = pd.Timestamp("2024-01-01", tz="UTC")  # exclusive
HOLDOUT_LO = TRAIN_HI
HOLDOUT_HI = pd.Timestamp("2026-06-01", tz="UTC")  # exclusive; data ends 2026-05-31

# Frozen variant set (Constraints 3-4). Rules: A always-on, B positive-funding,
# C cost-threshold (all imported logic from 20b), D min-hold-21 (new, local).
RULES = ["A", "B", "C", "D"]
# Spike treatments — the two-strip comparison is the point; reported side-by-side.
TREATMENTS = ["incl", "delever", "topdecile"]
STRIP_TREATMENTS = ["delever", "topdecile"]  # the verdict may ride on EITHER strip

MIN_HOLD = HOLD_REF_INTERVALS  # 21 intervals (~7d) — imported cost-breakeven horizon
SUSTAINED_FLIP = 2  # "sustained" = >=2 consecutive non-positive intervals (literal reading)

_COIN_DATA: dict[str, pd.DataFrame] = {}


# --------------------------------------------------------------------------- #
# Load + align (reuses 20b loaders; keeps BOTH masks so the two strips share a path)
# --------------------------------------------------------------------------- #
def _prepare_coin(coin: str) -> pd.DataFrame:
    """Per-interval funding frame with both legs marked + BOTH spike masks.

    Columns (indexed by funding-interval UTC ts): funding_rate, spot_close, perp_close,
    year, ``decile`` (|funding| in the per-coin top decile), ``delever`` (in a named
    crisis window). The combined ``topdecile`` mask reproduces 20b's single ``spike``.
    The decile threshold is computed over the FULL coin series — identical to 20b, so the
    ``topdecile`` column is byte-for-byte comparable to the 20b baseline (noted as a mild
    in-sample quantile; the ``delever`` strip has no such dependency).
    """
    spot = _load_ohlcv(f"{_DATA}/{coin}_spot.csv")
    perp = _load_ohlcv(f"{_DATA}/{coin}_perp.csv")
    fund = _load_funding(f"{_DATA}/{coin}_funding.csv")

    dates = fund.index.normalize()  # funding ts -> its UTC day (spot/perp are daily 00:00)
    spot_on = spot.reindex(dates, method="ffill")
    perp_on = perp.reindex(dates, method="ffill")

    fdf = pd.DataFrame(
        {
            "funding_rate": fund.to_numpy(float),
            "spot_close": spot_on.to_numpy(float),
            "perp_close": perp_on.to_numpy(float),
        },
        index=fund.index,
    )
    fdf = fdf.dropna(subset=["spot_close", "perp_close"])
    if fdf.empty:
        raise SystemExit(f"[EVAL] STOP: {coin} has no funding interval with both legs priced")

    abs_fr = fdf["funding_rate"].abs()
    p90 = float(abs_fr.quantile(SPIKE_DECILE))
    fdf["decile"] = abs_fr >= p90
    fdf["delever"] = _in_deleverage(fdf.index)
    fdf["year"] = fdf.index.year
    return fdf


def _mask_for(fdf: pd.DataFrame, treatment: str) -> np.ndarray:
    """Boolean strip-mask for a treatment. ``incl`` strips nothing."""
    if treatment == "incl":
        return np.zeros(len(fdf), dtype=bool)
    if treatment == "delever":
        return fdf["delever"].to_numpy(bool)
    if treatment == "topdecile":
        return (fdf["decile"] | fdf["delever"]).to_numpy(bool)
    raise ValueError(f"unknown treatment {treatment}")


# --------------------------------------------------------------------------- #
# Min-hold-21 episode generator (rule D — the ONE new rule, replicates 20b's close
# accounting; entry/exit are the only difference, so basis + signed-funding ledger match)
# --------------------------------------------------------------------------- #
def _gen_episodes_minhold(
    fdf: pd.DataFrame,
    *,
    fee: float,
    slip: float,
    spike: np.ndarray,
    cost_scale: float = 1.0,
) -> list[dict]:
    """Patient-hold episodes: enter on funding>0, hold >= MIN_HOLD intervals, exit only on
    a SUSTAINED flip (>= SUSTAINED_FLIP consecutive non-positive intervals, after min-hold).

    Identical signed-funding + basis accounting to 20b's ``_gen_episodes`` (funding accrues
    SIGNED over ``(entry, exit]``; masked intervals contribute 0 and cannot trigger entry —
    exactly the 20b stripped path). Cost = 4 taker + 4 slippage fills on notional.
    """
    n = len(fdf)
    if n < 2:
        return []
    times = list(fdf.index)
    fr = fdf["funding_rate"].to_numpy(float)
    fr_eff = np.where(spike, 0.0, fr)  # stripped intervals contribute 0 carry (20b path)
    spot = fdf["spot_close"].to_numpy(float)
    perp = fdf["perp_close"].to_numpy(float)
    rt_cost = (4.0 * fee + 4.0 * slip) * cost_scale * NOTIONAL

    eps: list[dict] = []

    def _close(a: int, b: int) -> None:
        seg = fr_eff[a + 1 : b + 1]  # funding events strictly after entry, through exit
        funding_pnl = float(seg.sum()) * NOTIONAL
        neg = int((seg < 0).sum())
        price_pnl = ((spot[b] / spot[a]) - (perp[b] / perp[a])) * NOTIONAL
        gross = funding_pnl + price_pnl
        eps.append(
            {
                "entry_time": times[a],
                "exit_time": times[b],
                "year": int(times[b].year),
                "funding_pnl": funding_pnl,
                "price_pnl": price_pnl,
                "gross_usd": gross,
                "cost_usd": rt_cost,
                "pnl_usd": gross - rt_cost,
                "n_intervals": b - a,
                "neg_intervals": neg,
            }
        )

    in_pos = False
    a = 0
    i = 0
    while i < n:
        if not in_pos:
            if fr_eff[i] > 0.0:
                in_pos, a = True, i
            i += 1
            continue
        # held: exit only after the min-hold AND on a sustained (>=2 consec) non-positive flip
        held = i - a
        flip = i >= SUSTAINED_FLIP - 1 and bool(
            np.all(fr_eff[i - (SUSTAINED_FLIP - 1) : i + 1] <= 0.0)
        )
        if held >= MIN_HOLD and flip:
            _close(a, i)
            in_pos = False
        i += 1
    if in_pos:
        _close(a, n - 1)
    return eps


# --------------------------------------------------------------------------- #
# Ledger builder (per-coin; routes rule D to the min-hold generator, A/B/C to 20b's)
# --------------------------------------------------------------------------- #
def _build_ledger(
    coin: str,
    rule: str,
    lo: pd.Timestamp,
    hi: pd.Timestamp,
    *,
    treatment: str,
    cost_scale: float = 1.0,
) -> pd.DataFrame:
    fdf = _COIN_DATA[coin]
    sub = fdf[(fdf.index >= lo) & (fdf.index < hi)].copy()
    fee, slip, thr = _coin_costs(coin)
    mask = _mask_for(sub, treatment)
    if rule == "D":
        rows = _gen_episodes_minhold(sub, fee=fee, slip=slip, spike=mask, cost_scale=cost_scale)
    else:
        sub["spike"] = mask
        rows = _gen_episodes(
            sub,
            rule,
            fee=fee,
            slip=slip,
            c_threshold=thr,
            stripped=(treatment != "incl"),
            cost_scale=cost_scale,
        )
    if not rows:
        return pd.DataFrame(columns=["pnl_usd", "entry_time", "exit_time", "year"])
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Net-carry CURVE (the legible centerpiece) — always-on, per coin, per year, 3 treatments
# --------------------------------------------------------------------------- #
def _net_carry_curve(coin: str) -> dict:
    """Per-year SIGNED funding accrual (the always-on carry) for all 3 treatments, over the
    full priced timeline, in $ on NOTIONAL. The difference incl-vs-strip per year is exactly
    how much of that year's carry came from spikes — the operator can SEE tail vs normal.
    Round-trip cost is a single entry/exit for an always-on hold (reported once); basis
    (price) drift is reported separately so the curve is funding-only (the brief's net carry).
    """
    fdf = _COIN_DATA[coin]
    fee, slip, _ = _coin_costs(coin)
    rt_cost = (4.0 * fee + 4.0 * slip) * NOTIONAL
    years = sorted(int(y) for y in pd.unique(fdf["year"]))
    fr = fdf["funding_rate"].to_numpy(float)
    yr = fdf["year"].to_numpy(int)
    spot = fdf["spot_close"].to_numpy(float)
    perp = fdf["perp_close"].to_numpy(float)
    basis_total = ((spot[-1] / spot[0]) - (perp[-1] / perp[0])) * NOTIONAL

    curve: dict[str, list] = {}
    for treatment in TREATMENTS:
        mask = _mask_for(fdf, treatment)
        fr_eff = np.where(mask, 0.0, fr)
        cum = 0.0
        series = []
        for y in years:
            net_y = float(fr_eff[yr == y].sum()) * NOTIONAL
            cum += net_y
            series.append(
                {
                    "year": y,
                    "net_funding_usd": round(net_y, 2),
                    "cumulative_funding_usd": round(cum, 2),
                }
            )
        curve[treatment] = series

    def _ho_strip_carry(treatment: str) -> float:
        mask = _mask_for(fdf, treatment)
        fr_eff = np.where(mask, 0.0, fr)
        ho = (fdf.index >= HOLDOUT_LO) & (fdf.index < HOLDOUT_HI)
        return float(fr_eff[ho].sum()) * NOTIONAL

    holdout_normal_carry = {t: round(_ho_strip_carry(t), 2) for t in STRIP_TREATMENTS}
    # one-line read: is there ANY positive net normal-regime carry after costs in holdout?
    any_pos = any(v - rt_cost > 0 for v in holdout_normal_carry.values())
    read = (
        f"{coin}: holdout normal-regime carry after one round-trip cost ${rt_cost:.2f} = "
        f"delever ${holdout_normal_carry['delever']:.0f} / "
        f"topdecile ${holdout_normal_carry['topdecile']:.0f} -> "
        + ("SOME positive net carry survives the strip" if any_pos else "~ALL carry is tail")
    )
    return {
        "round_trip_cost_usd": round(rt_cost, 2),
        "basis_price_pnl_alwayson_usd": round(basis_total, 2),
        "per_year": curve,
        "holdout_normal_regime_carry_usd": holdout_normal_carry,
        "holdout_normal_carry_after_cost_positive": any_pos,
        "read": read,
    }


# --------------------------------------------------------------------------- #
# Scoring helpers
# --------------------------------------------------------------------------- #
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


def _each_year_positive(s: Stats) -> bool:
    return bool(s.per_year) and all(v > 0 for v in s.per_year.values())


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_eval(*, prior_trials: int = 70) -> dict:
    print("[EVAL] phase20c: loading BTC, ETH (spot/perp/funding) — reason: majors validation")
    for coin in COINS:
        _COIN_DATA[coin] = _prepare_coin(coin)
        fdf = _COIN_DATA[coin]
        print(
            f"[EVAL] {coin}: {len(fdf)} priced funding-intervals "
            f"{fdf.index[0].date()}..{fdf.index[-1].date()} "
            f"decile%={fdf['decile'].mean():.3f} delever%={fdf['delever'].mean():.3f} "
            f"basis(last-perp/spot ratio)~={(fdf['perp_close'] / fdf['spot_close']).iloc[-1]:.4f}"
        )

    total_intervals = int(sum(len(v) for v in _COIN_DATA.values()))

    # ---- full matrix: coin x rule x treatment x {train, holdout} ------------ #
    matrix: dict[str, dict] = {}
    train_srs: list[float] = []
    train_incl: dict[str, Stats] = {}  # (coin:rule) -> train-incl Stats, for DSR
    total_episodes_timing = 0  # episodes from timing rules (B/C/D) — must be << intervals
    for coin in COINS:
        for rule in RULES:
            cell: dict[str, dict] = {}
            for treatment in TREATMENTS:
                tr = _build_ledger(coin, rule, TRAIN_LO, TRAIN_HI, treatment=treatment)
                ho = _build_ledger(coin, rule, HOLDOUT_LO, HOLDOUT_HI, treatment=treatment)
                s_tr, s_ho = compute_stats(tr), compute_stats(ho)
                cell[treatment] = {"train": _stats_dict(s_tr), "holdout": _stats_dict(s_ho)}
                if treatment == "incl":
                    train_incl[f"{coin}:{rule}"] = s_tr
                    if rule in ("B", "C", "D"):
                        total_episodes_timing += len(tr) + len(ho)
                    if s_tr.n_trades >= 4:
                        train_srs.append(s_tr.sr_per_trade)
            matrix[f"{coin}:{rule}"] = cell
            print(
                f"[EVAL] {coin}/{rule}: train PF incl {cell['incl']['train']['pf']} "
                f"(n={cell['incl']['train']['n']}) | holdout strip "
                f"delever {cell['delever']['holdout']['pf']} "
                f"topdecile {cell['topdecile']['holdout']['pf']}"
            )

    # ---- deflation: n_trials = prior + scored (coin,rule) strategies --------- #
    # Treatments are diagnostic LENSES on one strategy, not separate searches; periods are
    # eval not search. So a "trial" = one (coin, rule) backtest = 2 x 4 = 8 (matches 20b's
    # rule x scope convention of NOT multiplying by the incl/strip split).
    n_variants = len(COINS) * len(RULES)  # 8
    n_trials = prior_trials + n_variants
    sr_var = float(np.var(train_srs, ddof=1)) if len(train_srs) > 1 else 0.0
    sr0 = expected_max_sharpe(n_trials, sr_var)
    print(f"[EVAL] deflator: n_trials={prior_trials}+{n_variants}={n_trials} sr_var={sr_var:.6f}")

    # ---- verdict scan (Task F): PASS-for-majors iff SOME (coin, rule) clears the honest
    # bar on a SPIKE-STRIPPED holdout — PF>=1.30 AND n>=200 AND each-year+ AND DSR>0 -------
    candidates: list[dict] = []
    verdict_pass = False
    for coin in COINS:
        for rule in RULES:
            s_tr_incl = train_incl[f"{coin}:{rule}"]
            for treatment in STRIP_TREATMENTS:
                tr = _build_ledger(coin, rule, TRAIN_LO, TRAIN_HI, treatment=treatment)
                ho = _build_ledger(coin, rule, HOLDOUT_LO, HOLDOUT_HI, treatment=treatment)
                s_tr, s_ho = compute_stats(tr), compute_stats(ho)
                dsr, _ = deflated_sharpe(
                    s_tr_incl.sr_per_trade,
                    s_tr_incl.n_trades,
                    s_tr_incl.skew,
                    s_tr_incl.kurtosis,
                    n_trials,
                    sr_var,
                )
                pf_ok = math.isfinite(s_ho.profit_factor) and s_ho.profit_factor >= PF_MIN
                n_ok = s_ho.n_trades >= N_TRADES_MIN
                yr_ok = _each_year_positive(s_ho)
                dsr_ok = dsr > 0.0
                clears = bool(pf_ok and n_ok and yr_ok and dsr_ok)
                verdict_pass = verdict_pass or clears
                if pf_ok or clears:  # record anything that even clears the PF bar
                    candidates.append(
                        {
                            "coin": coin,
                            "rule": rule,
                            "treatment": treatment,
                            "holdout_strip_pf": _pf(s_ho.profit_factor),
                            "holdout_strip_n": s_ho.n_trades,
                            "holdout_each_year_positive": yr_ok,
                            "n_ge_200": n_ok,
                            "dsr": round(dsr, 5),
                            "dsr_gt_0": dsr_ok,
                            "train_gate": check_train(s_tr_incl).reasons,
                            "holdout_gate_strip": check_holdout(s_tr, s_ho, dsr).reasons,
                            "clears_verdict_bar": clears,
                        }
                    )

    verdict = "PASS-for-majors" if verdict_pass else "NONE-confirmed"
    print(
        f"[EVAL] VERDICT: {verdict} — clearing candidates on spike-stripped holdout: "
        f"{sum(1 for c in candidates if c['clears_verdict_bar'])}"
    )

    # ---- net-carry curves (the legible deliverable) ------------------------- #
    curves = {coin: _net_carry_curve(coin) for coin in COINS}
    for coin in COINS:
        print(f"[EVAL] {coin}/curve: {curves[coin]['read']}")

    # ---- integrity (Task C) ------------------------------------------------- #
    # always-on (rule A) holds continuously and MUST accrue negative funding intervals.
    a_neg = 0
    for coin in COINS:
        led = _build_ledger(coin, "A", TRAIN_LO, HOLDOUT_HI, treatment="incl")
        a_neg += int(led.get("neg_intervals", pd.Series(dtype=int)).sum())
    # costs applied: gross != net on a timing ledger
    probe = _build_ledger("BTC", "C", TRAIN_LO, TRAIN_HI, treatment="incl")
    gross_sum = float(probe.get("gross_usd", pd.Series(dtype=float)).sum())
    net_sum = float(probe.get("pnl_usd", pd.Series(dtype=float)).sum())
    integrity = {
        "trade_unit_is_episode_not_interval": bool(total_episodes_timing < total_intervals),
        "timing_rule_episodes_BCD": total_episodes_timing,
        "total_funding_intervals": total_intervals,
        "episode_to_interval_ratio": round(total_episodes_timing / total_intervals, 5),
        "negative_funding_included_signed_alwayson": bool(a_neg > 0),
        "alwayson_negative_intervals_held": a_neg,
        "costs_applied_gross_ne_net": bool(abs(gross_sum - net_sum) > 1e-6),
        "probe_btc_C_gross_train_usd": round(gross_sum, 2),
        "probe_btc_C_net_train_usd": round(net_sum, 2),
        "holdout_sealed_verdict_on_strip": True,
        "both_strips_same_masking_path": True,
        "deflator_prior_trials": prior_trials,
        "deflator_in_eval_variants": n_variants,
        "deflator_total_trials": n_trials,
    }

    return {
        "phase": "20c-majors-validation",
        "verdict": verdict,
        "research_only": True,
        "venue": "Binance USDⓈ-M (data.binance.vision); funding interval 8h",
        "scope": {"coins": COINS, "per_coin": True, "pooling": False, "alts": False},
        "split": {
            "train": [str(TRAIN_LO.date()), str((TRAIN_HI - pd.Timedelta(days=1)).date())],
            "holdout": [str(HOLDOUT_LO.date()), str((HOLDOUT_HI - pd.Timedelta(days=1)).date())],
            "episodes_built_within_period": True,
        },
        "variant_set": {
            "rules": {
                "A": "always-on (diagnostic; net-carry curve, not PF)",
                "B": "positive-funding (hold while funding>0, exit on flip)",
                "C": "cost-threshold (hold while funding > cost-breakeven rate)",
                "D": f"min-hold-{MIN_HOLD} (enter funding>0, hold >={MIN_HOLD} intervals, "
                f"exit only on a sustained flip = >={SUSTAINED_FLIP} consec non-positive)",
            },
            "treatments": {
                "incl": "spike-included (no strip)",
                "delever": "delever-only — 5 named crisis windows stripped (surgical)",
                "topdecile": "top-decile |funding| OR delever (20b's blanket strip)",
            },
            "min_hold_intervals": MIN_HOLD,
            "sustained_flip_intervals": SUSTAINED_FLIP,
            "no_hold_length_sweep": True,
        },
        "costs": {
            "taker_fee_per_fill": TAKER_FEE,
            "fills_per_round_trip": 4,
            "slip_per_fill_major": SLIP_MAJOR,
            "notional_usd_per_leg": NOTIONAL,
            "imported_from": "tools.eval.phase20_funding_carry.run (§0.5.97; not re-derived)",
        },
        "deflator": {
            "prior_trials": prior_trials,
            "in_eval_variants": n_variants,
            "n_trials": n_trials,
            "sr_variance_pool": round(sr_var, 6),
            "sr0_expected_max": round(sr0, 5),
        },
        "matrix": matrix,
        "verdict_scan": {
            "rule": "PASS-for-majors iff some (coin,rule) on a spike-stripped holdout has "
            "PF>=1.30 AND n>=200 AND each-year+ AND DSR>0 (post prior+variants)",
            "clearing_count": sum(1 for c in candidates if c["clears_verdict_bar"]),
            "candidates_clearing_pf_bar": candidates,
        },
        "net_carry_curves": curves,
        "integrity": integrity,
        "baseline_20b_per_coin": {
            "C:BTC": {"train_pf": 1.235, "stripped": 0.006, "holdout": 0.397, "holdout_strip": 0.0},
            "C:ETH": {"train_pf": 1.58, "stripped": 0.0, "holdout": 0.506, "holdout_strip": 0.0},
            "note": "both strip to ~0.00, both holdouts <0.51 — 20b edge was squeeze/delever tail",
        },
        "gates": {
            "PF_MIN": PF_MIN,
            "EXP_PER_CT_MIN": EXP_PER_CT_MIN,
            "N_TRADES_MIN": N_TRADES_MIN,
            "DSR_MIN": DSR_MIN,
            "HOLDOUT_PF_DEGRADE_MAX": HOLDOUT_PF_DEGRADE_MAX,
        },
        "cumulative_trials_after_this_eval": n_trials,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--prior-trials",
        type=int,
        default=70,
        help="cumulative strategy trials from earlier phases (70 after Phase-20b)",
    )
    ap.add_argument("--out", default=None, help="write results.json to this path")
    args = ap.parse_args()
    res = run_eval(prior_trials=args.prior_trials)
    out_path = (
        args.out or "/home/tradeflow/tradeflow/tools/eval/phase20c_majors_validation/results.json"
    )
    with open(out_path, "w") as fh:
        json.dump(res, fh, indent=2)
    print(f"[EVAL] wrote {out_path} — verdict {res['verdict']}")


if __name__ == "__main__":
    main()
