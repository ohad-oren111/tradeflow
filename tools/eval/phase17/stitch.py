"""Continuous back-adjusted (Panama) daily series from per-contract daily bars.

Pure pandas — no IB import — so the stitch logic is testable offline (see ``__main__``
self-test) and a candidate series can never accidentally drive a live path.

Method (matches the difference / Panama convention of ``tools.eval.data.roll_adjust``,
generalised from quarterly-only to ANY roll cycle via volume selection):

  1. A liquidity filter first drops dead serial months (peak volume << the basket peak),
     so the roll only ever steps between LIQUID contracts (auto-handles gold G/J/M/Q/V/Z,
     crude all-monthly, FX quarterly). Then a FRONT/BACK POINTER advances to the
     IMMEDIATE NEXT liquid contract only — triggered by a volume crossover within
     ``roll_window_days`` of expiry, or a ``roll_buffer_days`` backstop at expiry. The
     pointer can never jump to a far-deferred contract, so a noisy deep-month volume print
     cannot ratchet the series off the true front (the failure mode an earlier
     "max-volume within N months, monotonic" rule hit on monthly crude).
  2. A ROLL is a date where the active contract changes. The roll GAP is measured on the
     roll date itself = (old contract close) - (new contract close) on that same date
     (both still trade), so it is the pure calendar spread, not mixed with an overnight
     move.
  3. Back-adjust NEWEST->OLDEST: add the cumulative gap to every bar STRICTLY BEFORE each
     roll, so the most-recent contract stays at its real (broker-true) price and the
     history is shifted to be continuous. Volume is never adjusted.

This makes roll boundaries EXPLICIT and auditable (returned in ``RollReport``) — the
DATA-QA layer asserts they are sane (one per cycle, gap within a tolerance, no residual
spike at the boundary).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class RollEvent:
    date: pd.Timestamp
    old_expiry: str
    new_expiry: str
    gap_pts: float  # old_close - new_close on the roll date (raw, pre-adjust)


@dataclass
class RollReport:
    rolls: list = field(default_factory=list)  # list[RollEvent]
    total_offset_pts: float = 0.0
    n_contracts_used: int = 0


def _parse_expiry(s: str) -> pd.Timestamp:
    """``lastTradeDateOrContractMonth`` -> the contract's last-trade Timestamp (UTC).

    Accepts ``YYYYMMDD`` (exact) or ``YYYYMM`` (approximated to month-end). Used only for
    ordering + the not-yet-expired test, so month-end is a safe approximation."""
    s = str(s)
    if len(s) >= 8 and s[:8].isdigit():
        return pd.Timestamp(s[:8], tz="UTC")
    if len(s) >= 6 and s[:6].isdigit():
        return pd.Timestamp(s[:6] + "01", tz="UTC") + pd.offsets.MonthEnd(0)
    raise ValueError(f"unparseable expiry: {s!r}")


def build_continuous(
    contract_frames: dict[str, pd.DataFrame],
    *,
    roll_buffer_days: int = 7,
    roll_window_days: int = 75,
    liquidity_frac: float = 0.05,
) -> tuple[pd.DataFrame, RollReport]:
    """Stitch ``{expiry_str: daily_df}`` into one continuous back-adjusted daily series.

    Each ``daily_df`` has columns ``time`` (tz-aware UTC, daily), ``open/high/low/close``
    and ``volume``. Returns ``(continuous_df, RollReport)``. ``continuous_df`` has the same
    OHLCV columns plus ``active_expiry`` (which contract sourced each bar, pre-adjust).
    """
    if not contract_frames:
        return (
            pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"]),
            RollReport(),
        )

    # --- long frame: one row per (date, contract) ---
    parts = []
    for exp, df in contract_frames.items():
        if df is None or df.empty:
            continue
        d = df[["time", "open", "high", "low", "close", "volume"]].copy()
        d["expiry"] = exp
        d["expiry_ts"] = _parse_expiry(exp)
        parts.append(d)
    if not parts:
        return (
            pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"]),
            RollReport(),
        )
    long = pd.concat(parts, ignore_index=True)
    long["time"] = pd.to_datetime(long["time"], utc=True).dt.normalize()
    long = long.dropna(subset=["close"]).drop_duplicates(["time", "expiry"], keep="last")

    # --- liquidity filter: drop dead serial months (peak vol << the basket peak) ---
    peak = long.groupby("expiry")["volume"].max()
    gpeak = float(peak.max()) if len(peak) else 0.0
    liquid = set(peak[peak >= liquidity_frac * gpeak].index) if gpeak > 0 else set(peak.index)
    long = long[long["expiry"].isin(liquid)]

    # ordered liquid contracts + fast lookups
    exp_order = (
        long[["expiry", "expiry_ts"]]
        .drop_duplicates()
        .sort_values("expiry_ts")
        .reset_index(drop=True)
    )
    exps = exp_order["expiry"].tolist()
    exp_ts = exp_order["expiry_ts"].tolist()
    last_i = len(exps) - 1
    dates = sorted(long["time"].unique())
    vol = long.set_index(["time", "expiry"])["volume"].to_dict()
    have = set(vol.keys())

    # --- front/back pointer roll (advance ONLY to the immediate next liquid contract) ---
    chosen: list[tuple[pd.Timestamp, str]] = []  # (date, expiry)
    ai = 0
    while ai < last_i and (exp_ts[ai] - dates[0]).days <= roll_buffer_days:
        ai += 1  # seed past any contract already at/near expiry on the first date
    for d in dates:
        # backstop: force-roll once the active contract is within the buffer of expiry
        while ai < last_i and (exp_ts[ai] - d).days <= roll_buffer_days:
            ai += 1
        # volume-crossover: near the roll window, roll if the next contract is more liquid
        if ai < last_i and 0 < (exp_ts[ai] - d).days <= roll_window_days:
            va = vol.get((d, exps[ai]), 0.0)
            vn = vol.get((d, exps[ai + 1]), 0.0)
            if vn > va:
                ai += 1
        e = exps[ai]
        if (d, e) in have:
            chosen.append((d, e))
        elif chosen and (d, chosen[-1][1]) in have:
            chosen.append((d, chosen[-1][1]))  # active has no bar today — carry prior

    if not chosen:
        return (
            pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"]),
            RollReport(),
        )

    chosen_df = pd.DataFrame(chosen, columns=["time", "active_expiry"])

    # --- assemble the active bar for each date ---
    key = long.set_index(["time", "expiry"])
    rows = []
    for _, r in chosen_df.iterrows():
        bar = key.loc[(r["time"], r["active_expiry"])]
        rows.append(
            {
                "time": r["time"],
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar["volume"]),
                "active_expiry": r["active_expiry"],
            }
        )
    out = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)

    # --- roll events + gaps (old_close - new_close on the roll date) ---
    report = RollReport(n_contracts_used=out["active_expiry"].nunique())
    roll_rows = out.index[out["active_expiry"] != out["active_expiry"].shift(1)].tolist()
    for i in roll_rows[1:]:  # skip the first (series start, not a roll)
        d = out.at[i, "time"]
        old_exp = out.at[i - 1, "active_expiry"]
        new_exp = out.at[i, "active_expiry"]
        try:
            old_close = float(key.loc[(d, old_exp), "close"])
        except KeyError:
            old_close = float(out.at[i - 1, "close"])  # old contract had no bar on roll date
        new_close = float(out.at[i, "close"])
        report.rolls.append(RollEvent(d, str(old_exp), str(new_exp), old_close - new_close))

    # --- Panama back-adjust: newest -> oldest. Shift bars BEFORE each roll by
    # (new_close - old_close) = -gap_pts so they align to the newer contract's scale
    # (the latest contract stays real). Mirrors tools.eval.data.roll_adjust's `+= gap`
    # where its gap is (new_open - old_close).
    adj = out[["open", "high", "low", "close"]].to_numpy(dtype=float, copy=True)
    times = out["time"]
    cum = 0.0
    for ev in sorted(report.rolls, key=lambda e: e.date, reverse=True):
        shift = -ev.gap_pts  # new_close - old_close
        cum += shift
        mask = (times < ev.date).to_numpy()
        adj[mask, :] += shift
    report.total_offset_pts = cum
    out[["open", "high", "low", "close"]] = adj
    return out, report


# --------------------------------------------------------------------------- #
# Synthetic self-test (run: python -m tools.eval.phase17.stitch)
# --------------------------------------------------------------------------- #
def _self_test() -> None:
    """Two contracts, a clean volume crossover, a KNOWN +20pt roll gap. After adjust the
    boundary must be continuous (no residual spike) and the latest contract unchanged."""
    base = pd.Timestamp("2025-01-01", tz="UTC")

    # Front F1: days 0..19, close 100..119, volume high early then fading.
    f1_rows = []
    px = 100.0
    for k in range(20):
        t = base + pd.Timedelta(days=k)
        v = 1000 if k < 12 else 100  # volume fades after day 12
        f1_rows.append(
            {"time": t, "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": v}
        )
        px += 1.0
    f1 = pd.DataFrame(f1_rows)
    # Next F2: days 10..29, priced 20pt BELOW F1 (gap), volume rises and overtakes at day 12.
    f2_rows = []
    px = 90.0  # at day 10 F1 close=110, F2 close=90 -> a 20pt roll gap
    for k in range(20):
        t = base + pd.Timedelta(days=10 + k)
        v = 50 if k < 2 else 2000  # overtakes F1 from day 12
        f2_rows.append(
            {"time": t, "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": v}
        )
        px += 1.0
    f2 = pd.DataFrame(f2_rows)

    # expiries sit AFTER each contract's last bar (F1 last 2025-01-20, F2 last 2025-01-30)
    frames = {"20250125": f1, "20250228": f2}
    cont, rep = build_continuous(frames)

    assert len(rep.rolls) == 1, f"expected 1 roll, got {len(rep.rolls)}"
    ev = rep.rolls[0]
    assert abs(ev.gap_pts - 20.0) < 1e-6, f"gap should be +20, got {ev.gap_pts}"
    # latest contract (F2) bars unchanged: last close real = F2 last close = 90+19 = 109
    assert abs(cont["close"].iloc[-1] - 109.0) < 1e-6, cont["close"].iloc[-1]
    # continuity across the boundary: the day-over-day close step at the roll must be the
    # normal +1 trend, NOT the 20pt raw gap.
    c = cont.sort_values("time")["close"].to_numpy()
    steps = np.diff(c)
    assert np.all(np.abs(steps - 1.0) < 1e-6), f"residual roll spike in steps: {steps}"
    print(
        "stitch self-test PASS — 1 roll, gap=+20, boundary continuous (+1/day), latest unadjusted."
    )


if __name__ == "__main__":
    _self_test()
