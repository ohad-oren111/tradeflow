"""Phase-13 (research, OFFLINE, ADDITIVE) — concurrency / cluster historical replay.

Validates what PR #129–#131 just shipped (N=2 concurrency + kill-switch cluster mode,
``cluster_window_bars=1``) against REAL above-trend NQ history BEFORE the first live
2-stack — by driving the ACTUAL prod logic, not a re-implementation:

  * Stacking + correlated stops come from ``portfolio_study.simulate_portfolio`` at
    N=2 × M=2 over the regime-ON (above-trend) tape — the same replay whose N=1 anchor
    reproduces ``engine.simulate_segment`` byte-for-byte.
  * The cluster collapse is the REAL ``src.execution.kill_switch._collapse_loss_clusters``
    and ``evaluate_triggers``; the entry-bar minute derivation is the REAL
    ``kill_switch._entry_bar_minutes``. NOTHING here is re-implemented — if prod changes,
    this study changes with it.

Three things it proves / surfaces (§4 trap-defusal, on real data):
  1. **STACK** — 2 distinct-setup positions actually stack under the live concurrency
     gate (max-concurrent reaches N=2; bars spent 2-open > 0).
  2. **COLLAPSE FIRES** — feeding the real concurrent stop stream through the REAL
     ``_collapse_loss_clusters`` at the live ``cluster_window_bars=1`` actually merges
     correlated stop-clusters into one loss event (the mechanism works on real data).
  3. **THE KEY QUESTION (validate or CHALLENGE window=1)** — in real concurrent stops,
     are the two ENTRIES within ``cluster_window_bars=1`` (so the collapse fires), or are
     real concurrent entries often >1 bar apart (so window=1 does NOT collapse them)? We
     report the distribution of entry-gap-at-correlated-stop AND the peak consecutive-loss
     streak (the metric ``halt@10`` checks) under per-trade vs cluster-collapsed counting
     across candidate windows. This tells us whether window=1 actually defuses the trap.

NO prod change is proposed or applied. If window=1 under-covers, the report names a
WIDER-window CANDIDATE to EVALUATE LATER (a separate AUDIT, with the false-merge tradeoff
stated) — it does not touch config. The forward edge in the live regime is the one thing
this cannot prove (§0.5.220).

  python -m tools.eval.concurrency_replay [--n 2] [--m 2] [--out PATH]

OFFLINE research, READ-ONLY, drives NO prod path.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from config.risk_params import RISK
from src.execution.kill_switch import (
    _collapse_loss_clusters,
    _entry_bar_minutes,
    evaluate_triggers,
)
from tools.eval.engine import Trade
from tools.eval.portfolio_study import PortfolioConfig, load_tape, simulate_portfolio

PORTFOLIO_TAPE = "/tmp/tf_portfolio_tape.pkl"

# Candidate windows to probe coverage against the live default (cluster_window_bars=1).
CANDIDATE_WINDOWS: tuple[int, ...] = (1, 2, 5, 8, 15)

# Entry-gap histogram edges (minutes).
GAP_EDGES: tuple[tuple[float, float, str], ...] = (
    (0.0, 1.0, "<=1 (window=1 catches)"),
    (1.0, 2.0, "(1, 2]"),
    (2.0, 5.0, "(2, 5]"),
    (5.0, 30.0, "(5, 30]"),
    (30.0, float("inf"), "> 30"),
)


# --------------------------------------------------------------------------- #
# Pure helpers (all faithful to the REAL prod stream shape)
# --------------------------------------------------------------------------- #
def max_consecutive_loss_run(pnls_chronological: list[float]) -> int:
    """Longest run of consecutive losing closes (the peak of the metric ``halt@10``
    checks). Chronological order."""
    best = run = 0
    for p in pnls_chronological:
        run = run + 1 if p < 0 else 0
        best = max(best, run)
    return best


def correlated_stop_clusters(trades: list[Trade]) -> list[list[Trade]]:
    """Groups of >= 2 LOSING trades that exit on the SAME bar (exit_ts) — i.e. positions
    that stopped TOGETHER on one down-move (the §4 'correlated stop cluster'). Sorted by
    exit time; members within a cluster sorted by entry time."""
    by_exit: dict[object, list[Trade]] = {}
    for t in trades:
        if t.net_usd < 0:
            by_exit.setdefault(t.exit_ts, []).append(t)
    clusters = [sorted(v, key=lambda t: t.entry_ts) for v in by_exit.values() if len(v) >= 2]
    return sorted(clusters, key=lambda c: c[0].exit_ts)


def entry_gaps_minutes(cluster: list[Trade]) -> list[float]:
    """Pairwise adjacent entry gaps (minutes) within a cluster, via the REAL
    ``kill_switch._entry_bar_minutes`` (the exact derivation the live kill-switch uses)."""
    mins = [_entry_bar_minutes(t.entry_ts.isoformat()) for t in cluster]
    return [abs(b - a) for a, b in zip(mins[:-1], mins[1:], strict=False)]


def gap_histogram(gaps: list[float]) -> list[tuple[str, int]]:
    """Bucket entry gaps into GAP_EDGES; returns [(label, count)]."""
    g = np.asarray(gaps, dtype=float)
    out: list[tuple[str, int]] = []
    for lo, hi, label in GAP_EDGES:
        cnt = int((g <= hi).sum()) if lo == 0.0 else int(((g > lo) & (g <= hi)).sum())
        out.append((label, cnt))
    return out


def _newest_first_stream(trades: list[Trade]) -> tuple[list[float], list[float | None]]:
    """The exact shape the live kill-switch builds: closed pnls newest-first by exit
    time, with index-aligned entry-bar minutes (REAL ``_entry_bar_minutes``)."""
    ordered = sorted(trades, key=lambda t: t.exit_ts, reverse=True)
    pnls = [t.net_usd for t in ordered]
    ebars = [_entry_bar_minutes(t.entry_ts.isoformat()) for t in ordered]
    return pnls, ebars


def collapse_coverage(trades: list[Trade], windows=CANDIDATE_WINDOWS) -> dict[int, int]:
    """For each window, run the REAL ``_collapse_loss_clusters`` over the live-shaped
    stream and return {window: loss_events_merged} (= losses before - losses after)."""
    pnls, ebars = _newest_first_stream(trades)
    n_loss = sum(1 for p in pnls if p < 0)
    cov: dict[int, int] = {}
    for w in windows:
        collapsed = _collapse_loss_clusters(pnls, ebars, w)
        cov[w] = n_loss - sum(1 for p in collapsed if p < 0)
    return cov


def peak_streak_by_window(trades: list[Trade], windows=CANDIDATE_WINDOWS) -> dict[str, int]:
    """Peak consecutive-loss run per-trade and after the REAL cluster collapse at each
    window. Key 'per_trade' + 'w<window>'. This is the metric halt@10 actually checks."""
    pnls, ebars = _newest_first_stream(trades)  # newest-first
    chrono = list(reversed(pnls))
    out: dict[str, int] = {"per_trade": max_consecutive_loss_run(chrono)}
    for w in windows:
        collapsed_newest = _collapse_loss_clusters(pnls, ebars, w)
        out[f"w{w}"] = max_consecutive_loss_run(list(reversed(collapsed_newest)))
    return out


def halt_demo(trades: list[Trade], *, window: int, halt: int, warn: int) -> dict[str, str]:
    """Drive the REAL ``evaluate_triggers`` at the worst point of the run — the poll
    right after the longest per-trade loss run ends — with cluster_mode OFF vs ON, to
    show the actual halt VERDICT the trap produces and whether window=`window` defuses
    THAT specific halt. Uses the real trigger function, not a re-derivation."""
    chrono = sorted(trades, key=lambda t: t.exit_ts)
    pnls_chrono = [t.net_usd for t in chrono]
    # find the index where the longest leading-loss run ends (the worst poll instant).
    best = run = end = 0
    for i, p in enumerate(pnls_chrono):
        run = run + 1 if p < 0 else 0
        if run > best:
            best, end = run, i
    prefix = chrono[: end + 1]  # closed lifecycles up to and including that poll
    pnls, ebars = _newest_first_stream(prefix)
    off = evaluate_triggers(
        pnls,
        sum(pnls),
        None,
        warn_consec_losses=warn,
        halt_consec_losses=halt,
        max_drawdown_pct=33.0,
    )
    on = evaluate_triggers(
        pnls,
        sum(pnls),
        None,
        warn_consec_losses=warn,
        halt_consec_losses=halt,
        max_drawdown_pct=33.0,
        cluster_mode=True,
        cluster_window_bars=window,
        entry_bars_newest_first=ebars,
    )
    return {
        "worst_run": str(best),
        "off_action": off.action,
        "off_detail": off.detail,
        "on_action": on.action,
        "on_detail": on.detail,
    }


# --------------------------------------------------------------------------- #
# Verdict on the three points
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PointVerdict:
    name: str
    passed: bool
    detail: str


def assess_points(
    *,
    max_concurrent: int,
    bars_two_open: int,
    n_target: int,
    collapse_w1: int,
    peak: dict[str, int],
    halt_threshold: int,
    window: int,
    frac_within_window: float,
) -> list[PointVerdict]:
    """Pass/fail on the three brief points. Point 3 is PASS only if window=`window`
    both fires AND brings the peak streak below the halt threshold (genuine defusal);
    otherwise it is a CHALLENGE (mechanism works but under-covers)."""
    p1 = PointVerdict(
        "STACK",
        max_concurrent >= n_target and bars_two_open > 0,
        f"max-concurrent reached {max_concurrent} (target N={n_target}); "
        f"{bars_two_open:,} bars spent {n_target}-open.",
    )
    p2 = PointVerdict(
        "COLLAPSE-FIRES",
        collapse_w1 > 0,
        f"REAL _collapse_loss_clusters at window={window} merged {collapse_w1} loss-events "
        f"into correlated clusters on real data — the mechanism fires.",
    )
    peak_w = peak.get(f"w{window}", peak["per_trade"])
    defused = peak_w < halt_threshold
    p3 = PointVerdict(
        "WINDOW=1 DEFUSES?" + (" (VALIDATED)" if defused else " (CHALLENGED)"),
        defused,
        f"only {frac_within_window * 100:.1f}% of correlated-stop entries are within "
        f"window={window} min; peak consecutive-loss streak per-trade={peak['per_trade']} "
        f"vs cluster-w{window}={peak_w} (halt@{halt_threshold}). "
        + (
            "Cluster mode keeps the peak under halt — trap defused."
            if defused
            else f"Cluster mode reduces {peak['per_trade']}→{peak_w} but the peak is STILL "
            f">= halt@{halt_threshold} — window={window} does NOT fully defuse the trap."
        ),
    )
    return [p1, p2, p3]


def candidate_window(peak: dict[str, int], halt_threshold: int) -> int | None:
    """Smallest probed window whose peak streak drops BELOW the halt threshold, or None
    if no probed window does. A suggestion to EVALUATE LATER, never applied."""
    for w in CANDIDATE_WINDOWS:
        if peak.get(f"w{w}", 10**9) < halt_threshold:
            return w
    return None


# --------------------------------------------------------------------------- #
# Run + report
# --------------------------------------------------------------------------- #
def run_replay(*, n: int = 2, m: int = 2, tape_path: str = PORTFOLIO_TAPE) -> dict:
    tape = load_tape(tape_path)
    # kill-switch OFF so the full population of stacks + correlated stops is observed
    # over the WHOLE window (an ON run would halt early and hide the later clusters);
    # the kill-switch cluster logic is then exercised directly on that real stream.
    cfg = PortfolioConfig(name=f"N{n}M{m}", max_positions=n, contracts=m, kill_switch_enabled=False)
    res = simulate_portfolio(tape, cfg)
    clusters = correlated_stop_clusters(res.trades)
    gaps: list[float] = []
    for c in clusters:
        gaps.extend(entry_gaps_minutes(c))
    window = int(getattr(RISK, "cluster_window_bars", 1))
    halt = int(RISK.kill_switch_halt_consec_losses)
    cov = collapse_coverage(res.trades)
    peak = peak_streak_by_window(res.trades)
    demo = halt_demo(
        res.trades, window=window, halt=halt, warn=int(RISK.kill_switch_warn_consec_losses)
    )
    frac_within = float(np.mean(np.asarray(gaps) <= window)) if gaps else 0.0
    bars_two_open = res.concurrency_bar_count.get(n, 0)
    points = assess_points(
        max_concurrent=res.max_concurrent_seen,
        bars_two_open=bars_two_open,
        n_target=n,
        collapse_w1=cov.get(window, 0),
        peak=peak,
        halt_threshold=halt,
        window=window,
        frac_within_window=frac_within,
    )
    return {
        "n": n,
        "m": m,
        "tape_bars": tape.bars,
        "tape_span": tape.span,
        "result": res,
        "clusters": clusters,
        "gaps": gaps,
        "gap_hist": gap_histogram(gaps),
        "frac_within_window": frac_within,
        "window": window,
        "halt": halt,
        "coverage": cov,
        "peak": peak,
        "halt_demo": demo,
        "points": points,
        "candidate_window": candidate_window(peak, halt),
    }


def build_report(study: dict, *, generated_at: datetime) -> str:
    res = study["result"]
    n, m = study["n"], study["m"]
    window, halt = study["window"], study["halt"]
    lines: list[str] = []
    add = lines.append
    add("=" * 86)
    add("CONCURRENCY / CLUSTER HISTORICAL REPLAY — Phase 13 (validate the N=2 + window=1 ship)")
    add(
        f"generated: {generated_at.isoformat()}  |  span: {study['tape_span'][0]} .. "
        f"{study['tape_span'][1]}"
    )
    add(
        f"replay params: N={n} x M={m}; cluster_window_bars={window}, halt@{halt} (from RISK). "
        "Deployed"
    )
    add(
        "  container (per docker exec, 2026-06-09): max_concurrent=2, cluster_mode=True — the "
        "exact ship"
    )
    add(
        "  this study validates. (Host RISK defaults differ — env overrides live in the container "
        "only.)"
    )
    add("=" * 86)
    add("")
    add(f"Replay: REAL portfolio sim at N={n} x M={m}, regime-ON tape, kill-switch OFF (to observe")
    add("the FULL population of stacks + correlated stops). Cluster collapse + entry-bar minutes =")
    add("the REAL src.execution.kill_switch functions (imported, not re-implemented).")
    add("")
    add(
        f"bars={study['tape_bars']:,}  trades(N={n})={len(res.trades):,}  "
        f"losing={sum(1 for t in res.trades if t.net_usd < 0):,}  "
        f"max-concurrent={res.max_concurrent_seen}"
    )
    add(
        "  exposure histogram {n_open: bars}: "
        + ", ".join(f"{k}:{v:,}" for k, v in sorted(res.concurrency_bar_count.items()))
    )
    add("")
    add("-" * 86)
    add("THREE-POINT VERDICT")
    add("-" * 86)
    for p in study["points"]:
        add(f"  [{'PASS' if p.passed else 'CHALLENGE'}] {p.name}")
        add(f"         {p.detail}")
    add("")
    add("ENTRY-GAP-AT-CORRELATED-STOP (the key question — does window=1 catch real clusters?):")
    add(f"  correlated stop-clusters (>=2 losers exiting the SAME bar): {len(study['clusters']):,}")
    add(f"  pairwise entry gaps measured: {len(study['gaps']):,}")
    if study["gaps"]:
        g = np.asarray(study["gaps"])
        add(
            f"  entry-gap minutes: min={g.min():.0f} median={np.median(g):.0f} "
            f"p90={np.percentile(g, 90):.0f} max={g.max():.0f}"
        )
    for label, cnt in study["gap_hist"]:
        add(f"    {label:<26} {cnt:>5}")
    add(
        f"  => fraction of correlated-stop entries within window={window} min: "
        f"{study['frac_within_window'] * 100:.1f}%"
    )
    add("")
    add("REAL CLUSTER-COLLAPSE COVERAGE (loss-events merged by the REAL _collapse_loss_clusters):")
    for w, merged in study["coverage"].items():
        tag = "  <- LIVE window" if w == window else ""
        add(f"    window={w:>2} min: merged {merged:>4} loss-events{tag}")
    add("")
    add(f"PEAK CONSECUTIVE-LOSS STREAK vs window (the metric halt@{halt} checks):")
    add(
        f"    per-trade (cluster OFF): {study['peak']['per_trade']}"
        + (f"  >= halt@{halt} => TRIPS" if study["peak"]["per_trade"] >= halt else "")
    )
    for w in CANDIDATE_WINDOWS:
        pk = study["peak"][f"w{w}"]
        tag = "  <- LIVE window" if w == window else ""
        flag = f"  >= halt@{halt} => STILL TRIPS" if pk >= halt else f"  < halt@{halt} => defused"
        add(f"    cluster window={w:>2}: {pk}{flag}{tag}")
    add("")
    demo = study["halt_demo"]
    add(f"REAL evaluate_triggers at the worst poll (longest loss run = {demo['worst_run']}):")
    add(f"    cluster_mode OFF: action={demo['off_action'].upper()}  ({demo['off_detail']})")
    add(
        f"    cluster_mode ON (window={window}): action={demo['on_action'].upper()}  "
        f"({demo['on_detail']})"
    )
    add("")
    cw = study["candidate_window"]
    add("READ + CANDIDATE (NOT APPLIED):")
    if study["points"][2].passed:
        add(f"  window={window} fires AND keeps the peak streak under halt@{halt} on this tape —")
        add("  the shipped cluster_window_bars=1 is VALIDATED for the trap it was built to defuse.")
    else:
        add(
            f"  window={window} fires but under-covers: it catches only "
            f"{study['frac_within_window'] * 100:.1f}% of correlated-stop entries and reduces"
        )
        add(
            f"  the peak streak {study['peak']['per_trade']}->{study['peak'][f'w{window}']}, STILL "
            f">= halt@{halt}. On real above-trend data, window=1 does NOT fully defuse the N=2"
        )
        add("  correlated-stop trap — most correlated stops have entries MORE than 1 minute apart")
        add("  (they stop together on one down-move but entered on different pullback bars).")
        if cw is not None:
            add(
                f"  CANDIDATE to EVALUATE LATER (separate AUDIT, NOT applied): a wider "
                f"cluster_window_bars ~ {cw} min drops the peak streak below halt@{halt} here."
            )
            add("  TRADEOFF (state loudly): a wider window also merges loss clusters whose entries")
            add("  are merely near in time but NOT from the same down-move (false merges) — it can")
            add("  HIDE genuine independent consecutive losses from the halt. The right window is")
            add("  an operator AUDIT call weighing coverage vs false-merge risk + a forward test.")
        else:
            add("  No probed window <= 15 min drops the peak below halt — re-calibration needs")
            add("  more than a window change (e.g. a higher halt threshold for a concurrent book).")
    add("")
    add("CAVEATS: modeled fills; CORRELATED concurrency (real amplified DD). kill-switch OFF in")
    add("the replay to see the full population — the cluster math is exercised on that real stream")
    add("via the REAL prod functions. NOTHING applied to prod; sizing/window changes are AUDITs.")
    add("The forward edge in the live regime is unprovable here (§0.5.220).")
    add("=" * 86)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Concurrency/cluster historical replay (Phase 13)")
    ap.add_argument("--n", type=int, default=2, help="max concurrent positions (default 2 = live)")
    ap.add_argument("--m", type=int, default=2, help="contracts per position (default 2)")
    ap.add_argument(
        "--out", default=None, help="report path (default /tmp/concurrency_replay_<date>.txt)"
    )
    args = ap.parse_args()

    generated_at = datetime.now(UTC)
    study = run_replay(n=args.n, m=args.m)
    report = build_report(study, generated_at=generated_at)
    out = args.out or f"/tmp/concurrency_replay_{generated_at.strftime('%Y-%m-%d')}.txt"
    with open(out, "w") as fh:
        fh.write(report + "\n")
    print(report)
    print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
