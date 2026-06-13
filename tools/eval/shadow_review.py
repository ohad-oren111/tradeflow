"""Phase-11 (research, OFFLINE, ADDITIVE) — SeanBot shadow-ledger REVIEW.

Turns the Phase-10 shadow ledger (`shadow_ledger.py`) from a flat tally into a
**decision instrument**. It reuses the exact same pairing + scorecard math (so the
$ numbers are identical to `shadow_ledger`) and layers on the three things an
operator needs to actually act on the gate's opportunity cost:

  1. **COHORT SPLIT** — `MISS-regime` (the regime gate blocked it) vs `MISS-NO-BAR`
     (a blind-feed gap blocked it) are scored SEPARATELY, never pooled. They are
     different failure modes: a positive MISS-regime cohort means the *gate* is too
     strict (a strategy question); a non-zero MISS-NO-BAR cohort AFTER the feed fix
     (#127, merged 2026-06-09 15:27 UTC) means the *feed* is still dropping bars (an
     ops regression). Any MISS-NO-BAR dated after the fix is flagged LOUD as a feed
     regression signal — it should trend to ~0.

  2. **PER-DAY + CUMULATIVE curve** — WR / PF / net$ of the gate-BLOCKED SB signals
     day-by-day and as a running cumulative, at TF's live sizing (`--qty`, default 2
     now). One day's spike does not move a gate decision; the cumulative does.

  3. **PRE-COMMITTED DECISION BAR** — the verdict is decided by a rule fixed BEFORE
     the data (so we can't move the goalposts):
       * INSUFFICIENT          — n_paired < DECISION_BAR_N (~20-30): no verdict yet,
                                  keep accumulating (this is the honest default).
       * GATE-TOO-STRICT       — n_paired >= bar AND the blocked set is net-POSITIVE:
                                  SB monetized what the gate denied; the gate is
                                  costing money → escalate to a gate-calibration study.
       * GATE-EARNING-ITS-KEEP — n_paired >= bar AND the blocked set is net-NEGATIVE:
                                  the gate filtered net losers; it is doing its job.
     A confidence tier (NOISE / THIN / EMERGING / FIRM) annotates how much to trust
     the standing read.

This is FORWARD capture, not a backtest — n is SMALL and GROWING. The decision bar
exists precisely so a 6-trade positive blip is reported as INSUFFICIENT, not as a
green light. The HISTORICAL counterpart (does the below-trend cohort have an edge
over 26 months?) is Phase 12 (`gate_calibration.py`); this is the LIVE, accumulating
half of the same question (§0.5.222 — probe the mechanism from both sides).

  python -m tools.eval.shadow_review [--qty 2] [--from-json] [--out PATH]

OFFLINE research, READ-ONLY (same telemetry as `shadow_ledger`), drives NO prod path.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime

from tools.eval.metrics import Stats, compute_stats
from tools.eval.shadow_ledger import (
    BLOCKED_CLASSES,
    ShadowTrade,
    _load,
    _parse_ts,
    scorecard,
)

# The feed fix (#127, cancel-prior-sub + wedged-feed reconnect) merged at this UTC
# instant. A MISS-NO-BAR classified at/after this is NOT explained by the old feed
# bug and is flagged as a fresh feed-regression signal.
FEED_FIX_TS = datetime(2026, 6, 9, 15, 27, 0, tzinfo=UTC)

# Pre-committed decision bar: below this many PAIRED blocked trades, the verdict is
# INSUFFICIENT regardless of sign (the lower edge of the brief's ~20-30 window; 30 =
# FIRM confidence). Fixed before the data so the goalposts can't move.
DECISION_BAR_N = 20
FIRM_N = 30


# --------------------------------------------------------------------------- #
# Confidence + verdict (pre-committed rules)
# --------------------------------------------------------------------------- #
def confidence_tier(n: int) -> str:
    """Map paired-trade count to a coarse confidence label (annotation only)."""
    if n < 10:
        return "NOISE"
    if n < DECISION_BAR_N:
        return "THIN"
    if n < FIRM_N:
        return "EMERGING"
    return "FIRM"


@dataclass(frozen=True)
class Verdict:
    label: str  # INSUFFICIENT | GATE-TOO-STRICT | GATE-EARNING-ITS-KEEP
    confidence: str  # NOISE | THIN | EMERGING | FIRM
    text: str


def decide(combined: Stats) -> Verdict:
    """The pre-committed decision rule over the COMBINED blocked-cohort stats."""
    n = combined.n
    conf = confidence_tier(n)
    pf = combined.profit_factor
    pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
    if n < DECISION_BAR_N:
        return Verdict(
            "INSUFFICIENT",
            conf,
            (
                f"n_paired={n} < decision bar {DECISION_BAR_N} — NO verdict yet. "
                f"Standing read (net ${combined.net_usd:+.0f} at PF {pf_str}) is "
                f"directional only; keep accumulating."
            ),
        )
    if combined.net_usd > 0:
        return Verdict(
            "GATE-TOO-STRICT",
            conf,
            (
                f"n_paired={n} >= {DECISION_BAR_N} AND blocked set is net-POSITIVE "
                f"(${combined.net_usd:+.0f}, PF {pf_str}) — SB monetized what the gate "
                f"denied. Escalate to a gate-calibration study (Phase 12)."
            ),
        )
    return Verdict(
        "GATE-EARNING-ITS-KEEP",
        conf,
        (
            f"n_paired={n} >= {DECISION_BAR_N} AND blocked set is net-NEGATIVE "
            f"(${combined.net_usd:+.0f}, PF {pf_str}) — the gate filtered net losers; "
            f"it is doing its job."
        ),
    )


# --------------------------------------------------------------------------- #
# Per-day + cumulative curve
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DayRow:
    day: date
    day_stats: Stats  # that day's blocked trades alone
    cum_stats: Stats  # all blocked trades up to AND including that day


def daily_breakdown(trades: list[ShadowTrade]) -> list[DayRow]:
    """Group paired blocked trades by entry DATE (UTC) and build a per-day row plus a
    running cumulative. Pure. Days with no blocked trade are omitted (no signal)."""
    by_day: dict[date, list[ShadowTrade]] = {}
    for t in trades:
        by_day.setdefault(t.entry_ts.date(), []).append(t)
    rows: list[DayRow] = []
    running: list[ShadowTrade] = []
    for day in sorted(by_day):
        running.extend(by_day[day])
        rows.append(
            DayRow(
                day=day,
                day_stats=compute_stats(by_day[day]),
                cum_stats=compute_stats(list(running)),
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Feed-regression flag (MISS-NO-BAR after the feed fix)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FeedRegression:
    total_no_bar: int  # every MISS-NO-BAR reconciliation (paired or not)
    post_fix: list[dict]  # MISS-NO-BAR rows dated at/after FEED_FIX_TS (the regressions)

    @property
    def is_regression(self) -> bool:
        return len(self.post_fix) > 0


def detect_feed_regression(
    reconciliations: list[dict], *, feed_fix_ts: datetime = FEED_FIX_TS
) -> FeedRegression:
    """Count MISS-NO-BAR classifications and isolate any dated at/after the feed fix.

    MISS-NO-BAR should trend to ~0 now that #127 shipped; a post-fix MISS-NO-BAR is a
    feed regression (the feed is still dropping the bar TF needed to evaluate the SB
    signal), distinct from the regime gate being too strict. Pure."""
    total = 0
    post: list[dict] = []
    for rec in reconciliations:
        if rec.get("classification") != "MISS-NO-BAR":
            continue
        total += 1
        ts = rec.get("signal_ts")
        if ts is not None and _parse_ts(ts) >= feed_fix_ts:
            post.append(rec)
    return FeedRegression(total_no_bar=total, post_fix=post)


# --------------------------------------------------------------------------- #
# Assemble the full review
# --------------------------------------------------------------------------- #
def build_review(
    reconciliations: list[dict],
    signals: list[dict],
    *,
    qty: int = 2,
    feed_fix_ts: datetime = FEED_FIX_TS,
) -> dict:
    """Full decision-oriented review. Wraps `shadow_ledger.scorecard` (identical $
    math) and adds the cohort split read, per-day/cumulative curve, feed-regression
    flag, and pre-committed verdict. Pure — no I/O."""
    sc = scorecard(reconciliations, signals, qty=qty)
    trades: list[ShadowTrade] = sc["_trades"]
    combined = compute_stats(trades)
    return {
        "qty": qty,
        "scorecard": sc,
        "combined_stats": combined,
        "daily": daily_breakdown(trades),
        "feed_regression": detect_feed_regression(reconciliations, feed_fix_ts=feed_fix_ts),
        "verdict": decide(combined),
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _pf_str(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def format_review(review: dict, *, generated_at: datetime) -> str:
    qty = review["qty"]
    sc = review["scorecard"]
    combined: Stats = review["combined_stats"]
    fr: FeedRegression = review["feed_regression"]
    verdict: Verdict = review["verdict"]
    lines: list[str] = []
    add = lines.append

    add("=" * 78)
    add("SeanBot SHADOW-LEDGER REVIEW — Phase 11 (the gate's running go/no-go scorecard)")
    add(f"generated: {generated_at.isoformat()}  |  sizing: qty={qty} contract(s) (TF live)")
    add("=" * 78)
    add("")
    add("Decision question: is TF's regime gate / blind-feed no-bar miss too strict vs SB's")
    add("realized edge? Scored = the SB entries TF marked MISS-regime / MISS-NO-BAR, paired")
    add("to SB's OWN realized exits (READ-ONLY telemetry; FORWARD capture, not a backtest).")
    add("")

    # ---- standing verdict (top of report — the one line that matters) ----
    add("-" * 78)
    add(f"STANDING: {verdict.label}  [confidence: {verdict.confidence}]")
    add(f"  {verdict.text}")
    add("-" * 78)
    add("")

    # ---- cohort split ----
    add("COHORT SPLIT (scored separately — different failure modes):")
    header = (
        f"  {'cohort':<14} {'n':>3} {'win%':>6} {'expect$':>9} "
        f"{'PF':>6} {'net$':>10} {'maxDD$':>9}"
    )
    add(header)
    add("  " + "-" * 60)
    for cls in BLOCKED_CLASSES:
        s = sc["by_class"][cls]
        add(
            f"  {cls:<14} {s['n']:>3} {s['win_rate']*100:>5.1f}% {s['expectancy_usd']:>9.2f} "
            f"{_pf_str_from_any(s['profit_factor']):>6} {s['net_usd']:>10.2f} "
            f"{s['max_drawdown_usd']:>9.2f}"
        )
    add("  " + "-" * 60)
    add(
        f"  {'COMBINED':<14} {combined.n:>3} {combined.win_rate*100:>5.1f}% "
        f"{combined.expectancy_usd:>9.2f} {_pf_str(combined.profit_factor):>6} "
        f"{combined.net_usd:>10.2f} {combined.max_drawdown_usd:>9.2f}"
    )
    add(
        f"  (blocked total {sc['blocked_total']}  |  paired {sc['paired']}  |  "
        f"unpaired/open {sc['unpaired']})"
    )
    add("")

    # ---- MISS-regime is the strategy question; MISS-NO-BAR is the feed question ----
    add("READ: MISS-regime = the GATE's strategy cost (a Phase-12 calibration question).")
    add("      MISS-NO-BAR = a FEED gap cost — should trend to ~0 after feed fix #127.")
    if fr.is_regression:
        add("")
        add(
            f"  *** FEED REGRESSION FLAG: {len(fr.post_fix)} MISS-NO-BAR AT/AFTER the #127 fix "
            f"({FEED_FIX_TS.date()}) ***"
        )
        add("      The feed is STILL dropping bars TF needs — this is an OPS regression, not a")
        add("      gate-strictness result. Investigate the feed, do not read it as gate cost:")
        for rec in fr.post_fix:
            add(
                f"        {rec.get('signal_ts')}  price={rec.get('price')} "
                f"(msg {rec.get('message_id')})"
            )
    else:
        add(
            f"      Feed status: {fr.total_no_bar} MISS-NO-BAR total, 0 after the #127 fix — "
            f"no feed regression."
        )
    add("")

    # ---- per-day + cumulative curve ----
    add("PER-DAY + CUMULATIVE (blocked-cohort realized, at TF sizing):")
    add(
        f"  {'date':<12} {'n':>3} {'day net$':>10} {'day PF':>7}  ||  "
        f"{'cum n':>5} {'cum WR':>7} {'cum PF':>7} {'cum net$':>11}"
    )
    add("  " + "-" * 72)
    for row in review["daily"]:
        d = row.day_stats
        c = row.cum_stats
        add(
            f"  {row.day.isoformat():<12} {d.n:>3} {d.net_usd:>10.2f} "
            f"{_pf_str(d.profit_factor):>7}  ||  {c.n:>5} {c.win_rate*100:>6.1f}% "
            f"{_pf_str(c.profit_factor):>7} {c.net_usd:>11.2f}"
        )
    if not review["daily"]:
        add("  (no paired blocked trades yet)")
    add("")

    # ---- the trades themselves (audit trail) ----
    if sc["_trades"]:
        add("Paired blocked trades (realized, chronological):")
        for t in sorted(sc["_trades"], key=lambda x: x.entry_ts):
            add(
                f"    {t.entry_ts.isoformat()}  {t.classification:<12} "
                f"{t.pnl_points:>+7.2f}pt  net=${t.net_usd:>+9.2f} (msg {t.message_id})"
            )
        add("")
    if sc["_unpaired_rows"]:
        add(f"Unpaired blocked entries (no captured exit yet): {sc['unpaired']}")
        for rec in sc["_unpaired_rows"]:
            add(
                f"    {rec.get('signal_ts')}  {rec.get('classification')}  "
                f"price={rec.get('price')} (msg {rec.get('message_id')})"
            )
        add("")

    add(
        "Caveats: FORWARD capture (n grows weekly); decision bar is pre-committed at "
        f"n>={DECISION_BAR_N}."
    )
    add("SB ran 2 contracts (WR/PF sizing-invariant; $ at qty above). Friction = §0.5.97 model.")
    add("This is the LIVE half; the 26mo HISTORICAL cohort edge is Phase 12 (gate_calibration).")
    add("=" * 78)
    return "\n".join(lines)


def _pf_str_from_any(pf) -> str:
    """PF from a scorecard as_dict (may be the string 'inf') -> display."""
    if isinstance(pf, str):
        return pf
    return f"{pf:.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(description="SeanBot shadow-ledger review (Phase 11, research)")
    ap.add_argument(
        "--qty", type=int, default=2, help="contracts for $ figures (default 2 = TF live)"
    )
    ap.add_argument("--from-json", action="store_true", help="reuse cached pulls, no DB call")
    ap.add_argument(
        "--out", default=None, help="report path (default /tmp/shadow_review_<date>.txt)"
    )
    args = ap.parse_args()

    generated_at = datetime.now(UTC)
    reconciliations, signals = _load(args.from_json)
    review = build_review(reconciliations, signals, qty=args.qty)
    report = format_review(review, generated_at=generated_at)

    out = args.out or f"/tmp/shadow_review_{generated_at.strftime('%Y-%m-%d')}.txt"
    with open(out, "w") as fh:
        fh.write(report + "\n")
    print(report)
    print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
