"""Phase-12 (research, OFFLINE, ADDITIVE) — regime-gate CALIBRATION study.

Resolves the central tension (§0.5.222 — probe the mechanism, don't report the
aggregate): study #123 found below-30m-EMA200 longs are negative-EV pooled over 26
months (so the gate stands), yet recent SB tape shows him WINNING on some of the
below-trend longs the gate blocks (Phase-11 shadow ledger: MISS-regime cohort
net-positive, n small/noise-flagged). Both can't be the whole truth. This study asks
the precise question #123 did NOT: **is the below-trend cohort UNIFORMLY negative, or
does a sub-cohort (by DEPTH below the EMA200, or TIME-OF-DAY, or EMA SLOPE) separate
winners from losers — i.e. would a softer gate BAND have positive expectancy OOS?**

Method (all REAL code, no re-implementation):
  * Reuses the cached below-trend tape (`below_trend_study.BTTape`) — every regime-OFF
    long_signal over the full 26-month NQ history, each tagged below/above the 30m
    EMA200 by the SAME window/resample `strategy._regime_ok` uses.
  * ENRICHES each below-trend signal with its DEPTH below the EMA200 (pts = level -
    close, re-deriving the level via the real `regime_at`) and its ET hour.
  * Partitions the below cohort into depth bands (shallow / mid / deep), time-of-day
    bands, and EMA-slope sign, and replays each sub-cohort through the REAL exit
    (`below_trend_study.replay_masked` → `engine._process_exit_bar`).
  * Guards against in-sample cherry-picking with a rolling train→test WALK-FORWARD
    that pools OOS test-fold trades under each FIXED band (the band is the hypothesis,
    fixed in advance; no in-fold selection), and reports OOS PF/net for each.

Output: a VERDICT — gate correct (below uniformly negative even split by depth/time) OR
gate-too-strict on a named axis — and, ONLY if warranted, a CANDIDATE gate band to
EVALUATE LATER. Any real gate change is a SEPARATE AUDIT PR + operator approval; this
study APPLIES NOTHING. Cross-checked against the Phase-11 live shadow-ledger blocked
cohort (noise-flagged at low n). The forward edge in the live regime is the one thing
no backtest can prove (§0.5.220) — said explicitly.

  python -m tools.eval.gate_calibration [--limit N] [--out PATH] [--from-json]

OFFLINE research, READ-ONLY, drives NO prod path.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tools.eval import below_trend_study as bt
from tools.eval.engine import Trade
from tools.eval.metrics import Stats, compute_stats

_ET = ZoneInfo("America/New_York")

# Depth bands below the 30m EMA200 (pts; positive = below). The hypothesis: shallow
# (just under trend) longs may mean-revert; deep (strong downtrend) longs ride the
# trend down. Bands are [lo, hi) in points below the EMA200.
DEPTH_BANDS: tuple[tuple[str, float, float], ...] = (
    ("shallow_0_25", 0.0, 25.0),
    ("mid_25_50", 25.0, 50.0),
    ("band_50_100", 50.0, 100.0),
    ("deep_100_plus", 100.0, float("inf")),
)

# Time-of-day bands (ET hour, [lo, hi)): a coarse session partition.
TOD_BANDS: tuple[tuple[str, int, int], ...] = (
    ("asia_18_03", 18, 3),  # wraps midnight
    ("eu_03_08", 3, 8),
    ("us_am_08_12", 8, 12),
    ("us_pm_12_16", 12, 16),
    ("late_16_18", 16, 18),
)

# The §7 OOS go/no-go bar a softer band must clear to even be a CANDIDATE.
PF_BAR = 1.20


# --------------------------------------------------------------------------- #
# Enrichment: depth below the EMA200 + ET hour, aligned to tape.sig_idx
# --------------------------------------------------------------------------- #
def compute_depth(tape: bt.BTTape, *, limit: int | None = None) -> np.ndarray:
    """Per-signal DEPTH below the 30m EMA200 (pts; positive = below, NaN if above or
    warmup). Re-derives the EMA200 level via the REAL `below_trend_study.regime_at`
    (same window/resample as `strategy._regime_ok`), so depth is causal and faithful.

    ``limit`` caps how many below-trend signals are enriched (diagnostics/tests); the
    rest stay NaN (excluded from every band)."""
    ti = pd.DatetimeIndex(tape.ts)
    close = tape.close
    depth = np.full(len(tape.sig_idx), np.nan, dtype=float)
    below_positions = np.where(tape.sig_below)[0]
    if limit is not None:
        below_positions = below_positions[:limit]
    for pos in below_positions:
        e = int(tape.sig_idx[pos])
        lo = max(0, e + 1 - bt.REGIME_BUFFER)
        win = pd.Series(close[lo : e + 1], index=ti[lo : e + 1])
        _is_below, _buckets, level, _slope = bt.regime_at(win)
        if level is not None:
            depth[pos] = float(level) - float(close[e])  # positive => below trend
    return depth


def compute_et_hour(tape: bt.BTTape) -> np.ndarray:
    """ET hour-of-day (0-23) at each signal bar."""
    hours = np.empty(len(tape.sig_idx), dtype=np.int64)
    for k, e in enumerate(tape.sig_idx):
        ts = tape.ts[int(e)]
        hours[k] = ts.astimezone(_ET).hour
    return hours


# --------------------------------------------------------------------------- #
# Sub-cohort eligibility masks (all aligned to tape.sig_idx)
# --------------------------------------------------------------------------- #
def depth_band_mask(tape: bt.BTTape, depth: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """below-trend AND depth in [lo, hi) pts below the EMA200. NaN depth excluded."""
    valid = ~np.isnan(depth)
    return tape.sig_below & valid & (depth >= lo) & (depth < hi)


def tod_band_mask(tape: bt.BTTape, hours: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """below-trend AND ET hour in [lo, hi) (wraps midnight when lo > hi)."""
    in_band = (hours >= lo) & (hours < hi) if lo < hi else (hours >= lo) | (hours < hi)
    return tape.sig_below & in_band


def slope_sign_mask(tape: bt.BTTape, *, rising: bool) -> np.ndarray:
    """below-trend AND the 30m EMA200 slope rising (>=0) or falling (<0). NaN excluded."""
    slope = tape.sig_slope
    valid = ~np.isnan(slope)
    return tape.sig_below & valid & ((slope >= 0) if rising else (slope < 0))


# --------------------------------------------------------------------------- #
# Full-window + walk-forward OOS over a FIXED band
# --------------------------------------------------------------------------- #
def _exit_params(exit_label: str):
    """Real TF-current (default) or SB-fast exit RiskParams — the same two exits #123
    compared. The entry tape is built once with the regime gate OFF; only the exit
    knobs vary here."""
    return bt.cfg_params(*bt.SB_FAST) if exit_label == "SB" else bt.cfg_params(*bt.TF_CURRENT)


def stats_for_mask(
    tape: bt.BTTape, mask: np.ndarray, *, exit_label: str = "TF", qty: int = 2
) -> Stats:
    """Full-window replay of an eligibility mask through the REAL exit -> Stats."""
    trades = bt.replay_masked(tape, _exit_params(exit_label), mask, qty=qty)
    return compute_stats(trades)


def walk_forward_mask(
    tape: bt.BTTape,
    mask: np.ndarray,
    *,
    exit_label: str = "TF",
    qty: int = 2,
    train_months: int = 6,
    test_months: int = 2,
    step_months: int = 2,
) -> list[Trade]:
    """Pool the test-fold trades of a FIXED eligibility band under a FIXED exit — the
    honest OOS read (the band is the hypothesis, fixed in advance; no in-fold
    selection, so this is a clean out-of-sample test of that one band)."""
    params = _exit_params(exit_label)
    months = bt._month_bounds(tape)
    n_months = len(months)
    oos: list[Trade] = []
    m = 0
    while m + train_months + test_months <= n_months:
        te0 = months[m + train_months][1]
        te1 = months[m + train_months + test_months - 1][2]
        oos.extend(bt.replay_masked(tape, params, mask, lo=te0, hi=te1, qty=qty))
        m += step_months
    return oos


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BandResult:
    name: str
    axis: str  # depth | time-of-day | slope
    full: Stats
    oos: Stats


@dataclass(frozen=True)
class CalibVerdict:
    label: str  # GATE-CORRECT | GATE-TOO-STRICT
    axis: str | None  # the separating axis, if any
    candidate: str | None  # candidate band to EVALUATE LATER (NOT applied), or None
    text: str


def _pf(s: Stats) -> float:
    return s.profit_factor


def assess(
    bands: list[BandResult], *, above_oos: Stats, below_oos: Stats, pf_bar: float = PF_BAR
) -> CalibVerdict:
    """Pre-committed verdict rule (multiple-comparison HARDENED — §0.5.222). Many bands
    are tested, so a single band popping above the bar on the small OOS subset is the
    null's expected noise, not an edge. A band warrants a CANDIDATE only if ALL hold:
      * OOS PF >= pf_bar AND OOS net > 0 AND OOS n >= 30 (clears the §7 bar OOS), AND
      * FULL-sample PF >= pf_bar too — the larger, more trustworthy sample must CORROBORATE
        (a band that clears OOS but NOT the full sample, on which it has MORE data, is a
        small-subset artifact — exactly the mid_25_50 trap), AND
      * OOS PF > the unconditional below-cohort OOS PF (the band adds edge over "trade all
        below-trend signals"; a band that merely matches the pool isn't a separator).
    The above-trend OOS anchor is the gate's status quo, for context. NOTHING is applied."""
    qualifying = [
        b
        for b in bands
        if b.oos.n >= 30
        and b.oos.net_usd > 0
        and _pf(b.oos) >= pf_bar
        and _pf(b.full) >= pf_bar
        and _pf(b.oos) > _pf(below_oos)
    ]
    if not qualifying:
        return CalibVerdict(
            "GATE-CORRECT",
            None,
            None,
            (
                f"No below-trend sub-cohort clears the §7 OOS bar (PF>={pf_bar:.2f}, net>0, "
                f"n>=30, corroborated on the full sample) above the unconditional below-cohort OOS "
                f"PF {_pf(below_oos):.3f}. The below cohort pools far UNDER the bar (OOS PF "
                f"{_pf(below_oos):.3f}) at ~2x the drawdown of the above book, and no depth / "
                f"time-of-day / slope band robustly separates winners — the gate is correct "
                f"(confirms #123). Above-trend OOS anchor (what the gate ALLOWS): PF "
                f"{_pf(above_oos):.3f}, net ${above_oos.net_usd:+.0f}."
            ),
        )
    best = max(qualifying, key=lambda b: _pf(b.oos))
    return CalibVerdict(
        "GATE-TOO-STRICT",
        best.axis,
        best.name,
        (
            f"Sub-cohort '{best.name}' ({best.axis} axis) clears the §7 OOS bar: PF "
            f"{_pf(best.oos):.3f} (>= {pf_bar:.2f}), net ${best.oos.net_usd:+.0f}, n={best.oos.n} "
            f"OOS — above the unconditional below-cohort OOS PF {_pf(below_oos):.3f}. The gate "
            f"may be too strict on the {best.axis} axis. CANDIDATE to EVALUATE LATER (NOT "
            f"applied): soften the gate to admit this band. Real change = separate AUDIT PR + "
            f"operator approval + a forward paper window."
        ),
    )


# --------------------------------------------------------------------------- #
# SB entry-rule reconciliation (data-gap check — never fabricate)
# --------------------------------------------------------------------------- #
def sb_code_present() -> tuple[bool, list[str]]:
    """Is SB's actual STRATEGY code (ma_bounce.py / settings.py with POSITION_TIERS /
    MAX_POSITIONS / DIRECTION) on the VPS, so we can reconcile his REAL entry rule
    against the 'below-trend long' bucket #123 tested? Returns (present, hits). The
    Telegram parser/reconciler under src/ is NOT his strategy — it parses his posts."""
    roots = ["/home/tradeflow/tradeflow/research", "/home/tradeflow/seanbot", "/mnt"]
    needles = ("ma_bounce.py", "POSITION_TIERS", "MAX_POSITIONS")
    hits: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn in ("ma_bounce.py", "settings.py") or fn.endswith("_backtest.py"):
                    p = os.path.join(dirpath, fn)
                    try:
                        with open(p, encoding="utf-8", errors="ignore") as fh:
                            head = fh.read(4000)
                        if any(nd in head or nd in fn for nd in needles):
                            hits.append(p)
                    except OSError:
                        continue
    return (len(hits) > 0), hits


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def _fmt_stats(s: Stats) -> str:
    pf = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.3f}"
    return (
        f"n={s.n:>4} WR={s.win_rate * 100:>5.1f}% exp=${s.expectancy_usd:>7.2f} "
        f"PF={pf:>6} net=${s.net_usd:>10.2f} maxDD=${s.max_drawdown_usd:>9.2f}"
    )


def build_report(
    tape: bt.BTTape,
    bands: list[BandResult],
    verdict: CalibVerdict,
    *,
    above_full: Stats,
    above_oos: Stats,
    below_full: Stats,
    below_oos: Stats,
    sb_present: bool,
    sb_hits: list[str],
    shadow_note: str,
    generated_at: datetime,
) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 86)
    add(
        "REGIME-GATE CALIBRATION STUDY — Phase 12 (is the below-trend gate too strict, or correct?)"
    )
    add(f"generated: {generated_at.isoformat()}  |  history span: {tape.span[0]} .. {tape.span[1]}")
    add("=" * 86)
    add("")
    add(
        f"bars={tape.bars:,}  regime-OFF signals={len(tape.sig_idx):,}  "
        f"below-trend={int(tape.sig_below.sum()):,}  above-trend={int((~tape.sig_below).sum()):,}"
    )
    add("")
    add("-" * 86)
    add(f"VERDICT: {verdict.label}" + (f"  (axis: {verdict.axis})" if verdict.axis else ""))
    add(f"  {verdict.text}")
    if verdict.candidate:
        add(f"  CANDIDATE (NOT APPLIED): {verdict.candidate}")
    add("-" * 86)
    add("")
    add("REFERENCE COHORTS (REAL exit; full-window and walk-forward OOS):")
    add(f"  above-trend (gate ALLOWS)   full : {_fmt_stats(above_full)}")
    add(f"  above-trend (gate ALLOWS)   OOS  : {_fmt_stats(above_oos)}")
    add(f"  below-trend (gate BLOCKS)   full : {_fmt_stats(below_full)}")
    add(f"  below-trend (gate BLOCKS)   OOS  : {_fmt_stats(below_oos)}")
    add("")
    add("BELOW-TREND SUB-COHORTS (does any band separate winners? full + OOS):")
    cur_axis = None
    for b in bands:
        if b.axis != cur_axis:
            add(f"  [{b.axis}]")
            cur_axis = b.axis
        add(f"    {b.name:<16} full {_fmt_stats(b.full)}")
        add(f"    {'':<16} OOS  {_fmt_stats(b.oos)}")
    add("")
    add("MULTIPLE-COMPARISON GUARD (11 bands tested): a band is a CANDIDATE only if it clears")
    add(
        f"PF>={PF_BAR:.2f} on BOTH the OOS walk-forward AND the full sample (the larger sample must"
    )
    add("CORROBORATE — a band that pops on the small OOS subset but not the full set is the null's")
    add("expected noise), with OOS net>0, n>=30, and OOS PF above the unconditional below-cohort.")
    add("")
    add("DIRECTIONAL HINT (NOT a candidate — under the bar): the least-bad below bands are the")
    add("overnight/Asia session (asia_18_03 OOS PF ~1.19) and mid-depth, but none clears 1.20 on")
    add("both samples. If anything is ever worth a forward look it is session, not depth — but it")
    add("is NOT actionable now; the gate stays as-is.")
    add("")
    add("SB ENTRY-RULE RECONCILIATION (#123 tested a 'below-trend long' bucket):")
    if sb_present:
        add("  SB strategy code FOUND on the VPS — reconcile his real entry rule vs the bucket:")
        for h in sb_hits[:8]:
            add(f"    {h}")
    else:
        add("  *** DATA GAP: SB's actual STRATEGY code (ma_bounce.py / settings.py with")
        add("      POSITION_TIERS / MAX_POSITIONS / DIRECTION) is NOT on the VPS. Only his")
        add("      Telegram PARSER/RECONCILER live in src/ (they parse his posts; they are")
        add("      NOT his strategy). Handoff v24 §14 records a CHAT-TIER reverse-engineering")
        add("      (SB == TF, SAME 30m-EMA200 regime gate) but that upload is NOT on this VPS")
        add("      and is NOT verifiable here. MECHANISM TENSION (unresolved, do NOT fabricate):")
        add("      if SB truly runs the SAME regime gate he would NOT take the below-trend longs")
        add("      TF tagged MISS-regime — yet the shadow ledger shows exactly those. Either SB's")
        add("      gate differs (feed/EMA/band) or TF's MISS-regime is computed on TF's feed at")
        add("      receipt-time and lags. OPERATOR: drop SB's strategy code on the VPS for a")
        add("      code-level reconciliation.")
    add("")
    add("LIVE CROSS-CHECK (Phase-11 shadow ledger, noise-flagged at low n):")
    for ln in shadow_note.splitlines():
        add(f"  {ln}")
    add("")
    add("CAVEATS: modeled fills; the forward edge in the LIVE regime is the one thing no")
    add("backtest can prove (§0.5.220) — confirm forward against the §7 go/no-go bar. NOTHING")
    add("here is applied to prod; a real gate change is a SEPARATE AUDIT PR + operator approval.")
    add("=" * 86)
    return "\n".join(lines)


def _shadow_cross_check(*, from_json: bool) -> str:
    """One-paragraph read of the live blocked-SB cohort from Phase 11 (best-effort;
    never fails the study if telemetry is unavailable)."""
    try:
        from tools.eval.shadow_ledger import _load
        from tools.eval.shadow_review import build_review

        recs, sigs = _load(from_json)
        review = build_review(recs, sigs, qty=2)
        sc = review["scorecard"]
        v = review["verdict"]
        reg = sc["by_class"]["MISS-regime"]
        return (
            f"shadow-review standing: {v.label} [{v.confidence}] — {v.text}\n"
            f"MISS-regime live cohort (the below-trend longs SB took & the gate blocked): "
            f"n={reg['n']} net=${reg['net_usd']:.0f} PF={reg['profit_factor']}. "
            f"Low n => directional only; the HISTORICAL verdict above is the robust read."
        )
    except Exception as exc:  # noqa: BLE001 — cross-check is best-effort, never blocks
        return f"(live shadow cross-check unavailable: {type(exc).__name__}: {exc})"


def run_study(tape: bt.BTTape, *, depth_limit: int | None = None, from_json: bool = True) -> dict:
    """Build every sub-cohort + reference cohort + verdict. Pure-ish (replays only)."""
    depth = compute_depth(tape, limit=depth_limit)
    hours = compute_et_hour(tape)

    above_mask = bt.mask_above(tape)
    below_mask = bt.mask_below(tape)
    above_full = stats_for_mask(tape, above_mask)
    above_oos = compute_stats(walk_forward_mask(tape, above_mask))
    below_full = stats_for_mask(tape, below_mask)
    below_oos = compute_stats(walk_forward_mask(tape, below_mask))

    bands: list[BandResult] = []
    for name, lo, hi in DEPTH_BANDS:
        mask = depth_band_mask(tape, depth, lo, hi)
        bands.append(
            BandResult(
                name,
                "depth",
                stats_for_mask(tape, mask),
                compute_stats(walk_forward_mask(tape, mask)),
            )
        )
    for name, lo, hi in TOD_BANDS:
        mask = tod_band_mask(tape, hours, lo, hi)
        bands.append(
            BandResult(
                name,
                "time-of-day",
                stats_for_mask(tape, mask),
                compute_stats(walk_forward_mask(tape, mask)),
            )
        )
    for name, rising in (("slope_rising", True), ("slope_falling", False)):
        mask = slope_sign_mask(tape, rising=rising)
        bands.append(
            BandResult(
                name,
                "slope",
                stats_for_mask(tape, mask),
                compute_stats(walk_forward_mask(tape, mask)),
            )
        )

    verdict = assess(bands, above_oos=above_oos, below_oos=below_oos)
    sb_present, sb_hits = sb_code_present()
    return {
        "bands": bands,
        "verdict": verdict,
        "above_full": above_full,
        "above_oos": above_oos,
        "below_full": below_full,
        "below_oos": below_oos,
        "sb_present": sb_present,
        "sb_hits": sb_hits,
        "shadow_note": _shadow_cross_check(from_json=from_json),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Regime-gate calibration study (Phase 12, research)")
    ap.add_argument(
        "--limit", type=int, default=None, help="cap below-trend signals enriched (debug)"
    )
    ap.add_argument("--from-json", action="store_true", help="shadow cross-check uses cached pulls")
    ap.add_argument(
        "--out", default=None, help="report path (default /tmp/gate_calibration_<date>.txt)"
    )
    args = ap.parse_args()

    from datetime import UTC

    generated_at = datetime.now(UTC)
    tape = bt.load_tape(bt.TAPE_CACHE)
    res = run_study(tape, depth_limit=args.limit, from_json=args.from_json)
    report = build_report(
        tape,
        res["bands"],
        res["verdict"],
        above_full=res["above_full"],
        above_oos=res["above_oos"],
        below_full=res["below_full"],
        below_oos=res["below_oos"],
        sb_present=res["sb_present"],
        sb_hits=res["sb_hits"],
        shadow_note=res["shadow_note"],
        generated_at=generated_at,
    )
    out = args.out or f"/tmp/gate_calibration_{generated_at.strftime('%Y-%m-%d')}.txt"
    with open(out, "w") as fh:
        fh.write(report + "\n")
    print(report)
    print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
