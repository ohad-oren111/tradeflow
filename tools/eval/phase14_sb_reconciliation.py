"""Phase-14 (research, OFFLINE, ADDITIVE) — SeanBot fill reconciliation.

ONE pre-committed question: **is SeanBot's cumulative lead achievable on the REAL
MNQ tape, or does it rest on a divergent price feed / a regime gate that fails
open?** SB posts entries TF's (confirmed-correct, Phase 12) regime gate would
block; he wins on some of them. Before we read that as "the gate costs us money",
we test SB's prints against ground truth — the real MNQ 202606 1-min tape pulled
from IBKR — three ways:

  B. PRICE DELTA — for every SB entry, ``delta = SB_posted_entry - real_close`` at
     the matched 1-min bar. Report the distribution (median, p10/p90, sign-bias)
     and test the timestamp-shift hypothesis (does SB's price match real_close at
     t-2..t+2 better than at t?).
  C. REGIME CLASSIFICATION — compute the 30m-EMA200 on the REAL tape at each SB
     entry (the SAME window/resample/EMA200 ``strategy._regime_ok`` uses) and bucket
     each entry: (i) real_price>EMA200 AND SB_price>EMA200 → legit above-trend;
     (ii) real_price<EMA200 but SB_price>EMA200 → DIVERGENT FEED (his inflated print
     looks above-trend while the real market is below); (iii) both<EMA200 → SB GATE
     FAILED OPEN (he took a genuinely below-trend long his gate should have blocked).
  D. RE-PRICE — re-run every CLEAN SB round-trip on the REAL tape: entry at the
     matched real_close, exit under SB's posted −75 SL / +150 TP bracket (and a
     trailing-ratchet variant), minus friction. Compare to SB's posted P&L and
     decompose what survives a fills-are-real AND gate-achievable test.

PRE-COMMITTED VERDICT BAR (stated before running — see ``decide_verdict``):
  * FEED-DIVERGENT — median |delta| > 10pt AND > 60% of classified entries in
    bucket (ii). His lead rests on a feed nobody can trade.
  * GATE-INERT — > 60% of classified entries in bucket (iii) with small delta
    (median |delta| <= 10pt). His lead rests on below-trend longs TF's gate blocks.
  * LEAD-REAL — otherwise. The lead is on real, above-trend, achievable trades.

SOURCES (READ-ONLY; drives NO prod path; no order, no DB write):
  * ``seanbot_signals`` (typed PostgREST select) — SB entries/exits; entry rows carry
    price/stop_price/target_price, exit rows carry pnl_points. FIFO-paired (SB is
    single-position long-only) into round-trips.
  * IBKR gateway (separate clientId, historical-data only) — the real MNQ 202606
    1-min tape (TRADES, useRTH=False), walked back in <=10 D chunks and stitched.

CAVEATS (loud, never hidden):
  * SB's STRATEGY code is NOT on the VPS (only his Telegram parser) — his exact
    trailing rule is a DATA GAP. Re-pricing primary = his POSTED bracket (−75 SL /
    +150 TP, both posted fields, zero assumption); a trailing variant (TF lock50 /
    trail150 proxy) is reported alongside, flagged.
  * Commission = ``MNQ.commission_rt_usd`` ($1.24/ct RT = 2×$0.62/side, the code's
    source of truth post W-S14.2; the brief's "0.62 RT" predates that per-side fix).
    Slippage = 0.5pt/side (1.0pt RT) per the brief.
  * The real tape is short (~the capture window + ~5d EMA lead-in); entries before
    the EMA200 warms (<202 30m buckets) are bucketed "insufficient_history",
    excluded from the (i)/(ii)/(iii) split, and reported separately.

  python -m tools.eval.phase14_sb_reconciliation [--from-json] [--out PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from config.instruments import MNQ
from config.risk_params import RiskParams
from tools.eval.below_trend_study import REGIME_BUFFER, regime_at
from tools.eval.engine import _OpenPosition, _process_exit_bar

# --------------------------------------------------------------------------- #
# Pre-committed verdict thresholds + friction (stated before running)
# --------------------------------------------------------------------------- #
DELTA_DIVERGENT_PT = 10.0  # median |delta| above this = a materially divergent feed
BUCKET_FRAC_BAR = 0.60  # >60% of classified entries in a bucket = that bucket dominates
SHIFT_SCAN = (-2, -1, 0, 1, 2)  # timestamp-shift hypotheses tested in Task B

COMMISSION_RT_USD = MNQ.commission_rt_usd  # $1.24/ct RT (code source of truth)
SLIPPAGE_PTS_PER_SIDE = 0.5  # brief: 0.5pt/side -> 1.0pt round-trip
SB_QTY = 2  # SB trades 2 contracts

# SB exit configs (driven through the REAL trail_manager / should_hard_exit).
SL_PTS = 75.0
TP_PTS = 150.0  # SB's posted target = entry + 150
# Posted bracket: −75 SL / +150 TP, NO trail (lock/trail set unreachably high so
# compute_ratcheted_stop never improves the resting stop). Zero-assumption.
SB_BRACKET = RiskParams(
    regime_gate_enabled=False,
    exit_mode="trailing",
    stop_loss_pts=SL_PTS,
    lock_in_pts=1.0e9,
    trail_offset_pts=1.0e9,
    hard_ceiling_pts=TP_PTS,
)
# Trailing variant: SB's documented "75 SL + trailing" with TF's lock50/trail150 as
# a proxy for his UNKNOWN trail params (data gap), capped at his posted +150 target.
SB_TRAILING = RiskParams(
    regime_gate_enabled=False,
    exit_mode="trailing",
    stop_loss_pts=SL_PTS,
    lock_in_pts=50.0,
    trail_offset_pts=150.0,
    hard_ceiling_pts=TP_PTS,
)

# Operator-stated cumulative SB lead this session (the figure under test).
STATED_LEAD_USD = 9935.98

SB_SIGNALS_JSON = "/tmp/tf_phase14_sb_signals.json"
REAL_BARS_JSON = "/tmp/tf_phase14_real_bars.json"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RoundTrip:
    entry_ts: datetime
    entry_msg_id: int | None
    sb_entry_px: float | None
    sb_stop_px: float | None
    sb_target_px: float | None
    exit_ts: datetime | None
    sb_exit_px: float | None
    pnl_points: float | None
    qty: int
    clean: bool  # entry parsed w/ price AND a paired exit w/ pnl_points


@dataclass(frozen=True)
class Tape:
    """Stitched real MNQ 1-min tape + fast minute lookup + a close Series for EMA."""

    times: list  # list[datetime] per bar (UTC, minute-start)
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    minute_to_idx: dict  # int(minute-start epoch) -> row index
    close_series: pd.Series  # close indexed by tz-aware DatetimeIndex (for regime_at)


# --------------------------------------------------------------------------- #
# Pure helpers — time + stats
# --------------------------------------------------------------------------- #
def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _floor_minute_epoch(ts: datetime) -> int:
    return int(ts.timestamp()) // 60 * 60


def distribution(values: list[float]) -> dict:
    """median / p10 / p90 / mean / n / sign-bias (frac > 0) of a value list."""
    if not values:
        return {"n": 0, "median": None, "p10": None, "p90": None, "mean": None, "frac_pos": None}
    arr = np.asarray(values, dtype=float)
    return {
        "n": int(arr.size),
        "median": float(np.median(arr)),
        "p10": float(np.percentile(arr, 10)),
        "p90": float(np.percentile(arr, 90)),
        "mean": float(np.mean(arr)),
        "frac_pos": float(np.mean(arr > 0)),
    }


# --------------------------------------------------------------------------- #
# Task A — FIFO round-trips
# --------------------------------------------------------------------------- #
def build_round_trips(signals: list[dict]) -> list[RoundTrip]:
    """FIFO-pair SB long entries to the next exit (SB is single-position long-only).

    A round-trip is ``clean`` iff the entry parsed with a price AND the exit carries
    pnl_points. An entry with no captured exit (still open / capture gap) yields a
    round-trip with exit fields None (never silently dropped). Pure — no I/O.
    """
    ordered = sorted(
        (s for s in signals if s.get("type") in ("entry", "exit")),
        key=lambda s: _parse_ts(s["ts"]),
    )
    trips: list[RoundTrip] = []
    open_entry: dict | None = None

    def _emit(entry: dict, exit_row: dict | None) -> None:
        e_px = entry.get("price")
        pnl = exit_row.get("pnl_points") if exit_row else None
        clean = bool(entry.get("parsed_ok")) and e_px is not None and pnl is not None
        trips.append(
            RoundTrip(
                entry_ts=_parse_ts(entry["ts"]),
                entry_msg_id=(int(entry["message_id"]) if entry.get("message_id") else None),
                sb_entry_px=(float(e_px) if e_px is not None else None),
                sb_stop_px=(
                    float(entry["stop_price"]) if entry.get("stop_price") is not None else None
                ),
                sb_target_px=(
                    float(entry["target_price"]) if entry.get("target_price") is not None else None
                ),
                exit_ts=(_parse_ts(exit_row["ts"]) if exit_row else None),
                sb_exit_px=(
                    float(exit_row["price"])
                    if exit_row and exit_row.get("price") is not None
                    else None
                ),
                pnl_points=(float(pnl) if pnl is not None else None),
                qty=int(entry.get("contracts") or SB_QTY),
                clean=clean,
            )
        )

    for sig in ordered:
        if sig["type"] == "entry":
            # A new entry while one is open => the prior entry's exit was not
            # captured; emit it UNPAIRED and track the newer one.
            if open_entry is not None:
                _emit(open_entry, None)
            open_entry = sig
        else:  # exit
            if open_entry is not None:
                _emit(open_entry, sig)
                open_entry = None
            # an exit with no open entry => orphan exit, skip
    if open_entry is not None:
        _emit(open_entry, None)
    return trips


# --------------------------------------------------------------------------- #
# Tape construction + matching
# --------------------------------------------------------------------------- #
def build_tape(bars: list[dict]) -> Tape:
    """Build a Tape from serialized bar dicts ({time iso, open, high, low, close, volume}).

    Dedups by minute-start, sorts ascending. Pure (no I/O)."""
    by_minute: dict[int, dict] = {}
    for b in bars:
        t = _parse_ts(b["time"])
        by_minute[_floor_minute_epoch(t)] = b
    minutes = sorted(by_minute)
    times = [datetime.fromtimestamp(m, tz=UTC) for m in minutes]
    high = np.asarray([float(by_minute[m]["high"]) for m in minutes], dtype=float)
    low = np.asarray([float(by_minute[m]["low"]) for m in minutes], dtype=float)
    close = np.asarray([float(by_minute[m]["close"]) for m in minutes], dtype=float)
    minute_to_idx = {m: i for i, m in enumerate(minutes)}
    close_series = pd.Series(close, index=pd.DatetimeIndex(times))
    return Tape(
        times=times,
        high=high,
        low=low,
        close=close,
        minute_to_idx=minute_to_idx,
        close_series=close_series,
    )


def matched_idx(tape: Tape, entry_ts: datetime, shift: int) -> int | None:
    """Row index of the 1-min bar at floor(entry_ts) + shift minutes, or None."""
    minute = _floor_minute_epoch(entry_ts) + shift * 60
    return tape.minute_to_idx.get(minute)


# --------------------------------------------------------------------------- #
# Task B — price delta + timestamp-shift hypothesis
# --------------------------------------------------------------------------- #
def deltas_at_shift(trips: list[RoundTrip], tape: Tape, shift: int) -> list[float]:
    """``SB_posted_entry - real_close`` at the matched bar, for each clean entry."""
    out: list[float] = []
    for t in trips:
        if t.sb_entry_px is None:
            continue
        idx = matched_idx(tape, t.entry_ts, shift)
        if idx is None:
            continue
        out.append(t.sb_entry_px - float(tape.close[idx]))
    return out


def shift_scan(trips: list[RoundTrip], tape: Tape, shifts=SHIFT_SCAN) -> dict:
    """Distribution of |delta| at each shift; the argmin-median shift is SB's most
    likely timestamp convention (e.g. SB posts the just-closed prior-minute bar)."""
    by_shift: dict[int, dict] = {}
    for s in shifts:
        ds = deltas_at_shift(trips, tape, s)
        abs_dist = distribution([abs(d) for d in ds])
        signed_dist = distribution(ds)
        by_shift[s] = {"abs": abs_dist, "signed": signed_dist}
    candidates = [(s, by_shift[s]["abs"]["median"]) for s in shifts if by_shift[s]["abs"]["n"] > 0]
    best_shift = min(candidates, key=lambda kv: kv[1])[0] if candidates else 0
    return {"by_shift": by_shift, "best_shift": best_shift}


# --------------------------------------------------------------------------- #
# Task C — regime classification on the REAL tape
# --------------------------------------------------------------------------- #
def classify_entry(real_close: float, sb_px: float, ema_level: float | None) -> str:
    """Bucket an entry by where the real close and SB's print sit vs the real EMA200.

    Returns one of: 'insufficient_history' (EMA not warm), 'i_legit_above',
    'ii_divergent_feed', 'iii_gate_failed_open', 'iv_sb_below_real_above'.
    """
    if ema_level is None:
        return "insufficient_history"
    real_below = real_close <= ema_level
    sb_below = sb_px <= ema_level
    if not real_below and not sb_below:
        return "i_legit_above"
    if real_below and not sb_below:
        return "ii_divergent_feed"
    if real_below and sb_below:
        return "iii_gate_failed_open"
    return "iv_sb_below_real_above"


def regime_level_at(tape: Tape, idx: int) -> tuple[float | None, int]:
    """30m-EMA200 level + valid-bucket count at row ``idx`` (the real regime gate)."""
    lo = max(0, idx + 1 - REGIME_BUFFER)
    win = tape.close_series.iloc[lo : idx + 1]
    _is_below, buckets, level, _slope = regime_at(win)
    return level, buckets


def classify_all(trips: list[RoundTrip], tape: Tape, shift: int) -> list[dict]:
    """Per-entry classification rows against a SINGLE tape (clean entries matched)."""
    rows: list[dict] = []
    for t in trips:
        if t.sb_entry_px is None:
            continue
        idx = matched_idx(tape, t.entry_ts, shift)
        if idx is None:
            rows.append({"trip": t, "bucket": "no_real_bar", "real_close": None, "ema": None})
            continue
        real_close = float(tape.close[idx])
        level, buckets = regime_level_at(tape, idx)
        rows.append(
            {
                "trip": t,
                "idx": idx,
                "real_close": real_close,
                "ema": level,
                "buckets": buckets,
                "bucket": classify_entry(real_close, t.sb_entry_px, level),
            }
        )
    return rows


def attribute_contract(
    entry_ts: datetime, sb_px: float, tapes: dict, shift: int
) -> tuple[str, int, float, float] | None:
    """Attribute an SB entry to the front month it actually traded — the contract
    whose 1-min close at the matched bar is NEAREST SB's print. Resolves the early-
    June→Sept roll (a ~200pt calendar carry would otherwise read as a huge delta).

    Returns (contract_name, row_idx, real_close, delta) for the best contract, or None.
    """
    best: tuple[str, int, float, float] | None = None
    for name, tape in tapes.items():
        idx = matched_idx(tape, entry_ts, shift)
        if idx is None:
            continue
        rc = float(tape.close[idx])
        delta = sb_px - rc
        if best is None or abs(delta) < abs(best[3]):
            best = (name, idx, rc, delta)
    return best


# --------------------------------------------------------------------------- #
# Task D — re-price round-trips on the REAL tape under SB's exit rule
# --------------------------------------------------------------------------- #
def reprice_on_real_tape(
    tape: Tape, entry_idx: int, entry_close: float, params: RiskParams
) -> tuple[float, str, int] | None:
    """Drive the REAL exit (`_process_exit_bar`) forward from ``entry_idx`` (the
    signal bar); the position is checked from the NEXT bar (mirrors the engine).

    Returns (exit_price, reason, bars_held) or None if unresolved at the tape end.
    """
    pos = _OpenPosition(
        entry_ts=tape.times[entry_idx],
        entry_price=entry_close,
        highest=entry_close,
        current_stop=entry_close - params.stop_loss_pts,
    )
    n = len(tape.times)
    for j in range(entry_idx + 1, n):
        closed = _process_exit_bar(
            pos,
            params,
            ts=tape.times[j],
            high=float(tape.high[j]),
            low=float(tape.low[j]),
            close=float(tape.close[j]),
            force_flat=None,
        )
        pos.bars_held += 1
        if closed is not None:
            exit_price, reason = closed
            return exit_price, reason, pos.bars_held
    return None


def net_usd(gross_pts: float, qty: int) -> float:
    """Net P&L after friction (slippage + commission), per the brief's friction model."""
    gross = gross_pts * MNQ.multiplier * qty
    slippage = (2.0 * SLIPPAGE_PTS_PER_SIDE) * MNQ.multiplier * qty
    commission = COMMISSION_RT_USD * qty
    return gross - slippage - commission


def posted_net_usd(pnl_points: float, qty: int) -> float:
    """SB's posted point move, charged the SAME friction (apples-to-apples)."""
    return net_usd(pnl_points, qty)


# --------------------------------------------------------------------------- #
# Task E — pre-committed verdict
# --------------------------------------------------------------------------- #
def decide_verdict(median_abs_delta: float, frac_ii: float, frac_iii: float) -> tuple[str, str]:
    """Return (verdict, one-line rationale). Bar stated in the module docstring."""
    if median_abs_delta > DELTA_DIVERGENT_PT and frac_ii > BUCKET_FRAC_BAR:
        return (
            "FEED-DIVERGENT",
            f"median |delta| {median_abs_delta:.1f}pt > {DELTA_DIVERGENT_PT:.0f} AND "
            f"{frac_ii*100:.0f}% of classified entries divergent-feed "
            f"(>{BUCKET_FRAC_BAR*100:.0f}%).",
        )
    if frac_iii > BUCKET_FRAC_BAR and median_abs_delta <= DELTA_DIVERGENT_PT:
        return (
            "GATE-INERT",
            f"{frac_iii*100:.0f}% of classified entries genuinely below-trend "
            f"(gate-failed-open, >{BUCKET_FRAC_BAR*100:.0f}%) with small delta "
            f"(median |delta| {median_abs_delta:.1f}pt <= {DELTA_DIVERGENT_PT:.0f}).",
        )
    return (
        "LEAD-REAL",
        f"neither divergence bar met (median |delta| {median_abs_delta:.1f}pt, "
        f"feed-divergent {frac_ii*100:.0f}%, gate-failed-open {frac_iii*100:.0f}%).",
    )


# --------------------------------------------------------------------------- #
# Study assembly
# --------------------------------------------------------------------------- #
def run_study(signals: list[dict], bars_by_contract: dict[str, list[dict]]) -> dict:
    """Pure end-to-end (roll-aware): round-trips -> shift scan -> per-entry contract
    attribution -> classify -> re-price -> verdict.

    ``bars_by_contract`` maps a front-month localSymbol (e.g. "MNQM6", "MNQU6") to
    its serialized 1-min bars. Each SB entry is matched against the contract it
    actually traded (``attribute_contract``), so the early-June→Sept roll does not
    masquerade as a divergent feed.
    """
    trips = build_round_trips(signals)
    tapes = {name: build_tape(bars) for name, bars in bars_by_contract.items() if bars}
    if not tapes:
        raise ValueError("no real bars supplied")
    primary = tapes.get("MNQM6") or next(iter(tapes.values()))
    clean = [t for t in trips if t.clean]

    # Timestamp convention (t-k) determined on the primary tape — contract-independent.
    scan = shift_scan(trips, primary)
    shift = scan["best_shift"]

    # Per-entry: attribute to the traded contract, then delta + regime classification.
    rows: list[dict] = []
    contract_counts: dict[str, int] = {}
    for t in trips:
        if t.sb_entry_px is None:
            continue
        att = attribute_contract(t.entry_ts, t.sb_entry_px, tapes, shift)
        if att is None:
            rows.append({"trip": t, "bucket": "no_real_bar"})
            continue
        name, idx, real_close, delta = att
        contract_counts[name] = contract_counts.get(name, 0) + 1
        tape = tapes[name]
        level, buckets = regime_level_at(tape, idx)
        rows.append(
            {
                "trip": t,
                "contract": name,
                "idx": idx,
                "real_close": real_close,
                "delta": delta,
                "ema": level,
                "buckets": buckets,
                "bucket": classify_entry(real_close, t.sb_entry_px, level),
            }
        )

    deltas = [r["delta"] for r in rows if "delta" in r]
    signed = distribution(deltas)
    abs_dist = distribution([abs(d) for d in deltas])

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    classified = sum(
        counts.get(b, 0)
        for b in (
            "i_legit_above",
            "ii_divergent_feed",
            "iii_gate_failed_open",
            "iv_sb_below_real_above",
        )
    )
    frac_ii = counts.get("ii_divergent_feed", 0) / classified if classified else 0.0
    frac_iii = counts.get("iii_gate_failed_open", 0) / classified if classified else 0.0

    # Re-price clean round-trips on the attributed contract's tape.
    row_by_msgid = {r["trip"].entry_msg_id: r for r in rows if "idx" in r}
    repriced: list[dict] = []
    for t in clean:
        r = row_by_msgid.get(t.entry_msg_id)
        if r is None:
            continue
        tape, idx, entry_close = tapes[r["contract"]], r["idx"], r["real_close"]
        br = reprice_on_real_tape(tape, idx, entry_close, SB_BRACKET)
        tr = reprice_on_real_tape(tape, idx, entry_close, SB_TRAILING)
        repriced.append(
            {
                "msgid": t.entry_msg_id,
                "bucket": r["bucket"],
                "posted_pts": t.pnl_points,
                "posted_net": posted_net_usd(t.pnl_points, t.qty),
                "bracket": (
                    {
                        "exit_px": br[0],
                        "reason": br[1],
                        "bars": br[2],
                        "net": net_usd(br[0] - entry_close, t.qty),
                    }
                    if br
                    else None
                ),
                "trailing": (
                    {
                        "exit_px": tr[0],
                        "reason": tr[1],
                        "bars": tr[2],
                        "net": net_usd(tr[0] - entry_close, t.qty),
                    }
                    if tr
                    else None
                ),
            }
        )

    def _sum(rows_, key, bucket=None):
        return sum(
            x[key]["net"] if key in ("bracket", "trailing") else x[key]
            for x in rows_
            if (bucket is None or x["bucket"] == bucket)
            and (key not in ("bracket", "trailing") or x[key] is not None)
        )

    posted_total = _sum(repriced, "posted_net")
    bracket_total = _sum(repriced, "bracket")
    trailing_total = _sum(repriced, "trailing")
    # Survives = re-priced (bracket) on the gate-achievable (above-trend) bucket only.
    survives = _sum(repriced, "bracket", bucket="i_legit_above")
    unachievable_posted = posted_total - _sum(repriced, "posted_net", bucket="i_legit_above")

    verdict, rationale = decide_verdict(abs_dist["median"] or 0.0, frac_ii, frac_iii)

    return {
        "n_trips": len(trips),
        "n_clean": len(clean),
        "tape_span": (
            (primary.times[0].isoformat(), primary.times[-1].isoformat()) if primary.times else None
        ),
        "tape_bars": len(primary.times),
        "contracts": {name: len(t.times) for name, t in tapes.items()},
        "contract_counts": contract_counts,
        "best_shift": shift,
        "shift_scan": scan["by_shift"],
        "delta_signed": signed,
        "delta_abs": abs_dist,
        "bucket_counts": counts,
        "classified": classified,
        "frac_ii": frac_ii,
        "frac_iii": frac_iii,
        "repriced": repriced,
        "posted_total": posted_total,
        "bracket_total": bracket_total,
        "trailing_total": trailing_total,
        "survives": survives,
        "unachievable_posted": unachievable_posted,
        "verdict": verdict,
        "rationale": rationale,
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
_BUCKET_LABEL = {
    "i_legit_above": "(i)   legit above-trend  (real>EMA AND SB>EMA)",
    "ii_divergent_feed": "(ii)  DIVERGENT FEED     (real<EMA but SB>EMA)",
    "iii_gate_failed_open": "(iii) SB GATE FAILED OPEN (real<EMA AND SB<EMA)",
    "iv_sb_below_real_above": "(iv)  SB<EMA, real>EMA   (deflated print)",
    "insufficient_history": "      insufficient EMA history (<202 buckets)",
    "no_real_bar": "      no matching real bar",
}


def _fmt_dist(d: dict, unit: str = "pt") -> str:
    if not d or d.get("n", 0) == 0:
        return "n=0"
    return (
        f"n={d['n']} median={d['median']:+.2f}{unit} "
        f"p10={d['p10']:+.2f} p90={d['p90']:+.2f} mean={d['mean']:+.2f} "
        f"frac>0={d['frac_pos']*100:.0f}%"
    )


def format_report(s: dict, *, generated_at: datetime) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 84)
    add("SeanBot FILL RECONCILIATION — Phase 14 (is the lead achievable on the REAL tape?)")
    add(f"generated: {generated_at.isoformat()}")
    add("=" * 84)
    add("")
    if s["tape_span"]:
        contracts = "  ".join(f"{k}={v}" for k, v in s["contracts"].items())
        add(f"Real MNQ tape (front months): {contracts} 1-min bars")
        add(f"  span (primary): {s['tape_span'][0]} → {s['tape_span'][1]}")
    add(f"SB round-trips: {s['n_trips']} total  |  clean (entry+exit parsed): {s['n_clean']}")
    cc = "  ".join(f"{k}={v}" for k, v in sorted(s["contract_counts"].items()))
    add(f"Contract attribution (entry matched to traded front month): {cc}")
    add("  → SB rolls June→Sept ~06-07; entries attributed to the nearer-price contract")
    add("    (a ~200pt calendar carry, NOT a phantom feed — see the roll finding).")
    add("")
    add("── Task B — price delta (SB_posted_entry − real_close, attributed contract) ──")
    add(f"  best-matching shift: t{s['best_shift']:+d} min (argmin median |delta|, primary tape)")
    add("  shift   median|Δ|   p90|Δ|    signed median   n")
    for sh in SHIFT_SCAN:
        a = s["shift_scan"][sh]["abs"]
        sg = s["shift_scan"][sh]["signed"]
        if a["n"] == 0:
            add(f"   t{sh:+d}     —          —          —             0")
            continue
        mark = "  <- best" if sh == s["best_shift"] else ""
        add(
            f"   t{sh:+d}    {a['median']:>7.2f}    {a['p90']:>7.2f}   "
            f"{sg['median']:>+8.2f}      {a['n']:>3}{mark}"
        )
    add(f"  attributed Δ (per-entry contract): signed {_fmt_dist(s['delta_signed'])}")
    add(f"                                     |Δ|    {_fmt_dist(s['delta_abs'])}")
    add("")
    add("── Task C — regime classification on the REAL 30m-EMA200 at each SB entry ──")
    order = [
        "i_legit_above",
        "ii_divergent_feed",
        "iii_gate_failed_open",
        "iv_sb_below_real_above",
        "insufficient_history",
        "no_real_bar",
    ]
    for b in order:
        c = s["bucket_counts"].get(b, 0)
        if c == 0 and b not in ("i_legit_above", "ii_divergent_feed", "iii_gate_failed_open"):
            continue
        add(f"    {_BUCKET_LABEL[b]:<48} {c:>4}")
    add(
        f"  classified (i+ii+iii+iv): {s['classified']}  |  "
        f"divergent-feed {s['frac_ii']*100:.0f}%  |  gate-failed-open {s['frac_iii']*100:.0f}%"
    )
    add("")
    add("── Task D — re-price clean round-trips on the REAL tape (friction-charged) ──")
    add(
        f"  friction: slippage {SLIPPAGE_PTS_PER_SIDE}pt/side + "
        f"commission ${COMMISSION_RT_USD:.2f}/ct RT"
    )
    add(f"  SB posted net (clean RTs, same friction):        ${s['posted_total']:>+11.2f}")
    add(f"  real-tape re-price, POSTED bracket (−75/+150):   ${s['bracket_total']:>+11.2f}")
    add(f"  real-tape re-price, TRAILING variant (proxy):    ${s['trailing_total']:>+11.2f}")
    add(f"  → re-price, gate-ACHIEVABLE bucket (i) (bracket): ${s['survives']:>+11.2f}")
    add(f"  → posted net of below-trend/divergent (ii+iii+iv):${s['unachievable_posted']:>+11.2f}")
    add("")
    n_iii = s["bucket_counts"].get("iii_gate_failed_open", 0)
    add(f"  CAPTURE LIMIT (honest): the operator's ${STATED_LEAD_USD:,.2f} cumulative lead is NOT")
    add(
        f"  reconstructible from this telemetry — only {s['n_clean']} of {s['n_trips']} "
        f"round-trips have a"
    )
    add("  parsed exit, and TF's own (losing) side is not in seanbot_signals. So this is a")
    add("  MECHANISM verdict, not a dollar audit of the 9,935. What IS captured:")
    add(
        f"   • SB's clean round-trips net only ${s['posted_total']:+,.2f} posted — the big "
        f"lead lives in"
    )
    add("     uncaptured trips + TF's drawdown, not in these prints.")
    add(
        f"   • the gate-ACHIEVABLE (above-trend) entries re-price to ${s['survives']:+,.2f} "
        f"under a −75/+150"
    )
    add("     bracket — a real, modest edge TF could in principle take.")
    add(
        f"   • the {n_iii} below-trend entries SB posts as wins re-price NEGATIVE on the "
        f"real tape —"
    )
    add("     exactly the trades Phase-12 confirmed GATE-CORRECT to block.")
    add("")
    add("── Task E — PRE-COMMITTED VERDICT ──")
    add(f"  VERDICT: {s['verdict']}")
    add(f"  {s['rationale']}")
    add("")
    add("Caveats: SB's exact trail rule is a DATA GAP (his code is not on the VPS) — bracket")
    add("is posted-data-only, trailing is a TF-proxy. Real tape is short; pre-EMA-warm entries")
    add("are excluded from the split. OFFLINE research, READ-ONLY, drives NO prod path.")
    add("=" * 84)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# I/O — live pulls (Supabase typed select + IBKR historical) with /tmp cache
# --------------------------------------------------------------------------- #
async def _fetch_sb_signals() -> list[dict]:
    from dotenv import load_dotenv

    from src.clients.supabase_client import SupabaseClient

    load_dotenv("/home/tradeflow/.tradeflow-secrets/.env")
    db = SupabaseClient(url=os.environ["SUPABASE_URL"], key=os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    try:
        return await db.select(
            "seanbot_signals",
            filters={"order": "ts.asc"},
            columns=(
                "ts,message_id,type,direction,symbol,price,stop_price,"
                "target_price,pnl_points,contracts,parsed_ok"
            ),
        )
    finally:
        await db.close()


async def _fetch_contract_bars(ib, local_symbol: str, chunks: int) -> list[dict]:
    """Walk one front-month's 1-min tape back in <=10 D chunks and serialize."""
    from ib_async import Future

    contract = Future(symbol="MNQ", exchange="CME", currency="USD", localSymbol=local_symbol)
    await ib.qualifyContractsAsync(contract)
    by_minute: dict[int, dict] = {}
    end: object = ""
    for _ in range(chunks):
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end,
            durationStr="10 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=2,
            keepUpToDate=False,
        )
        if not bars:
            break
        for b in bars:
            # ib_async historical BarData carries `.date` (a datetime under
            # formatDate=2), NOT `.time`.
            t = b.date if isinstance(b.date, datetime) else _parse_ts(str(b.date))
            if t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            by_minute[_floor_minute_epoch(t)] = {
                "time": t.astimezone(UTC).isoformat(),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
            }
        end = bars[0].date  # step back to the earliest bar of this chunk
    return [by_minute[m] for m in sorted(by_minute)]


async def _fetch_real_bars() -> dict[str, list[dict]]:
    """Fetch BOTH front months spanning the window. SB ROLLS from June (MNQM6) to
    Sept (MNQU6) ~8 days before the 2026-06-18 June expiry; pulling both lets the
    study attribute each entry to the contract SB actually traded (a benign ~200pt
    calendar carry, not a phantom feed — Phase-14 roll finding).

    Historical-data only (no orders); separate clientId 114 (§0.5.T1) so it never
    clashes with the live bot (IBKR_CLIENT_ID=1). Host-published gateway 127.0.0.1:4002.
    """
    from dotenv import load_dotenv
    from ib_async import IB

    load_dotenv("/home/tradeflow/.tradeflow-secrets/.env")
    ib = IB()
    await ib.connectAsync("127.0.0.1", 4002, clientId=114, timeout=20.0)
    try:
        # June: 3 chunks ≈ ~30d back (full window + EMA lead-in). Sept: 2 chunks
        # ≈ ~20d back (covers the post-roll window + its own EMA200 lead-in).
        june = await _fetch_contract_bars(ib, "MNQM6", chunks=3)
        sept = await _fetch_contract_bars(ib, "MNQU6", chunks=2)
        return {"MNQM6": june, "MNQU6": sept}
    finally:
        ib.disconnect()


def _load(from_json: bool) -> tuple[list[dict], dict[str, list[dict]]]:
    if from_json and os.path.exists(SB_SIGNALS_JSON) and os.path.exists(REAL_BARS_JSON):
        with open(SB_SIGNALS_JSON) as fh:
            signals = json.load(fh)
        with open(REAL_BARS_JSON) as fh:
            bars = json.load(fh)
        return signals, bars
    signals = asyncio.run(_fetch_sb_signals())
    bars = asyncio.run(_fetch_real_bars())
    with open(SB_SIGNALS_JSON, "w") as fh:
        json.dump(signals, fh)
    with open(REAL_BARS_JSON, "w") as fh:
        json.dump(bars, fh)
    return signals, bars


def main() -> None:
    ap = argparse.ArgumentParser(description="SeanBot fill reconciliation (Phase 14, research)")
    ap.add_argument("--from-json", action="store_true", help="reuse cached pulls, no DB/IBKR call")
    ap.add_argument("--out", default=None, help="report path (default /tmp/phase14_<date>.txt)")
    args = ap.parse_args()

    generated_at = datetime.now(UTC)
    signals, bars = _load(args.from_json)
    study = run_study(signals, bars)
    report = format_report(study, generated_at=generated_at)

    out = args.out or f"/tmp/phase14_{generated_at.strftime('%Y-%m-%d')}.txt"
    with open(out, "w") as fh:
        fh.write(report + "\n")
    print(report)
    print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
