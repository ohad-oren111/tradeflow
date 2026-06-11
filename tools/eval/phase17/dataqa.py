"""DATA-QA for a continuous back-adjusted daily series (Stage-1 gate).

Pure pandas. Given a stitched series + its ``RollReport`` (and an optional broker-truth
reference close from the live CONTFUT probe), produce a structured QA verdict the report
prints verbatim. The gate is the brief's: contract is active front month, roll boundaries
sane, no gaps/spikes at rolls, span + bar count reported, broker-truth verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .stitch import RollReport


@dataclass
class QAResult:
    symbol: str
    n_bars: int
    first: str
    last: str
    n_rolls: int
    n_contracts: int
    last_close: float
    ref_close: float | None
    ref_drift_pct: float | None
    max_abs_daily_ret: float
    max_ret_date: str
    max_roll_step_z: float  # largest |daily return at a roll date| in robust-sigma units
    worst_roll: str
    n_dupe_dates: int
    n_nan: int
    n_weekday_gaps: int  # business days with no bar (holidays + missing)
    max_gap_days: int
    checks: list = field(default_factory=list)  # (name, ok, detail)
    passed: bool = True


def _robust_sigma(x: np.ndarray) -> float:
    """MAD-based sigma (median absolute deviation * 1.4826) — outlier-robust scale."""
    x = x[~np.isnan(x)]
    if len(x) < 5:
        return float("nan")
    med = np.median(x)
    mad = np.median(np.abs(x - med))
    return float(mad * 1.4826)


def qa_series(
    symbol: str,
    df: pd.DataFrame,
    report: RollReport,
    *,
    ref_close: float | None = None,
    ret_spike_pct: float = 20.0,  # |daily return| above this is flagged a spike
) -> QAResult:
    df = df.sort_values("time").reset_index(drop=True)
    n = len(df)
    t = df["time"]
    close = df["close"].to_numpy(float)

    # daily log returns on the ADJUSTED close
    ret = np.full(n, np.nan)
    if n > 1:
        ret[1:] = np.diff(np.log(np.clip(close, 1e-9, None)))
    abs_ret = np.abs(ret)
    imax = int(np.nanargmax(abs_ret)) if n > 1 else 0
    max_abs = float(abs_ret[imax]) if n > 1 else 0.0

    # roll-boundary residual: the daily return ON each roll date, in robust-sigma units.
    sig = _robust_sigma(ret)
    roll_dates = {pd.Timestamp(ev.date).normalize() for ev in report.rolls}
    worst_z, worst_roll = 0.0, "-"
    if roll_dates and sig and not np.isnan(sig):
        for i in range(1, n):
            if t.iloc[i].normalize() in roll_dates:
                z = abs(ret[i]) / sig
                if z > worst_z:
                    worst_z = z
                    worst_roll = f"{t.iloc[i].date()} ret={ret[i]*100:+.2f}% ({z:.1f}σ)"

    # integrity
    n_dupe = int(t.duplicated().sum())
    n_nan = int(df[["open", "high", "low", "close"]].isna().to_numpy().sum())

    # weekday continuity — work in tz-NAIVE dates so bdate_range (naive) and the present
    # set compare on the same type (a tz-aware/naive mismatch silently flags every day).
    days_naive = t.dt.normalize().dt.tz_localize(None)
    full_bdays = (
        pd.bdate_range(days_naive.iloc[0], days_naive.iloc[-1]) if n else pd.DatetimeIndex([])
    )
    present = set(days_naive)
    n_gaps = sum(1 for d in full_bdays if d not in present)  # business days w/ no bar (~holidays)
    days = t.dt.normalize()
    max_gap = 0
    if n > 1:
        deltas = days.diff().dropna().dt.days.to_numpy()
        max_gap = int(deltas.max()) if len(deltas) else 0

    drift = None
    if ref_close is not None and ref_close:
        drift = float((close[-1] - ref_close) / ref_close * 100.0)

    res = QAResult(
        symbol=symbol,
        n_bars=n,
        first=str(t.iloc[0].date()) if n else "-",
        last=str(t.iloc[-1].date()) if n else "-",
        n_rolls=len(report.rolls),
        n_contracts=report.n_contracts_used,
        last_close=float(close[-1]) if n else float("nan"),
        ref_close=ref_close,
        ref_drift_pct=drift,
        max_abs_daily_ret=max_abs,
        max_ret_date=str(t.iloc[imax].date()) if n > 1 else "-",
        max_roll_step_z=worst_z,
        worst_roll=worst_roll,
        n_dupe_dates=n_dupe,
        n_nan=n_nan,
        n_weekday_gaps=n_gaps,
        max_gap_days=max_gap,
    )

    # ---- gate checks ----
    def chk(name: str, ok: bool, detail: str) -> None:
        res.checks.append((name, ok, detail))
        if not ok:
            res.passed = False

    chk("bars>=400", n >= 400, f"{n} bars (~{n/252:.1f}y daily)")
    chk("no_dupe_dates", n_dupe == 0, f"{n_dupe} dupes")
    chk("no_nan_ohlc", n_nan == 0, f"{n_nan} NaN")
    chk("dates_increasing", bool(t.is_monotonic_increasing), "monotonic")
    chk("rolls_present", len(report.rolls) >= 1, f"{len(report.rolls)} rolls")
    chk(
        "no_residual_roll_spike",
        worst_z < 6.0,
        f"worst roll-date move {worst_z:.1f}σ ({worst_roll})",
    )
    chk(
        "no_price_spike",
        max_abs < (ret_spike_pct / 100.0),
        f"max |daily ret| {max_abs*100:.1f}% on {res.max_ret_date}",
    )
    chk("max_gap<=6d", max_gap <= 6, f"largest date gap {max_gap}d")
    if drift is not None:
        chk("broker_truth_drift<2%", abs(drift) < 2.0, f"last vs live CONTFUT {drift:+.2f}%")
    return res


def format_qa(res: QAResult) -> str:
    head = (
        f"### {res.symbol}  —  {'PASS' if res.passed else 'FAIL'}\n"
        f"- span: {res.first} .. {res.last}  |  bars: {res.n_bars}  |  "
        f"contracts: {res.n_contracts}  |  rolls: {res.n_rolls}\n"
        f"- last_close(adj): {res.last_close:.5g}"
    )
    if res.ref_close is not None:
        head += f"  |  live CONTFUT: {res.ref_close:.5g}  |  drift: {res.ref_drift_pct:+.2f}%"
    head += (
        f"\n- max |daily ret|: {res.max_abs_daily_ret*100:.1f}% ({res.max_ret_date})  |  "
        f"worst roll-date move: {res.worst_roll}\n"
        f"- integrity: dupes={res.n_dupe_dates} nan={res.n_nan} "
        f"weekday_gaps={res.n_weekday_gaps} max_gap={res.max_gap_days}d\n"
    )
    lines = [head]
    for name, ok, detail in res.checks:
        lines.append(f"    [{'x' if ok else ' '}] {name}: {detail}")
    return "\n".join(lines)
