"""Phase 5 — consolidated, plain-English status report.

Assembles the backtest expectancy preview (segmented), the synthetic pass/fail matrix,
and placeholders for the live round-trip + fault-injection findings (which are AUDIT-
gated, Phases 3/4), then names the ONE residual none of it can shortcut: the forward
edge in the real regime.

    python -m tools.eval.report            # runs scenarios K=5 + a regime-ON backtest, then prints
    python -m tools.eval.report --quick    # scenarios K=2 + a 60k-bar backtest slice (fast)
"""

from __future__ import annotations

import argparse

from tools.eval import backtest as bt
from tools.eval import scenarios as scn
from tools.eval.metrics import compute_stats, segment_by_month


def consolidated(
    backtest_run: dict, scenario_results: list, *, live_done: bool = False, fault_done: bool = False
) -> str:
    res = backtest_run["result"]
    overall = compute_stats(res.trades)
    monthly = segment_by_month(res.trades)
    months_pos = sum(1 for s in monthly.values() if s.n >= 10 and s.expectancy_usd > 0)
    months_meaningful = sum(1 for s in monthly.values() if s.n >= 10)
    n_pass = sum(1 for r in scenario_results if r.all_passed)

    lines = []
    lines.append("#" * 78)
    lines.append("# TradeFlow eval kit — CONSOLIDATED STATUS (Phase 5)")
    lines.append("#" * 78)
    lines.append("")
    lines.append("## 1. Backtest expectancy preview (real strategy + real exit, modeled fills)")
    s0, s1 = backtest_run["span"]
    lines.append(
        f"   span {str(s0)[:10]} → {str(s1)[:10]}, {backtest_run['bars']:,} bars, "
        f"regime {'ON' if backtest_run['params'].regime_gate_enabled else 'OFF'}"
    )
    pf_str = "inf" if overall.profit_factor == float("inf") else f"{overall.profit_factor:.3f}"
    lines.append(
        f"   trades={overall.n}  win%={overall.win_rate * 100:.1f}  "
        f"expectancy=${overall.expectancy_usd:.2f}  PF={pf_str}  net=${overall.net_usd:,.0f}"
    )
    lines.append(
        f"   meaningful months (n>=10): {months_meaningful}; " f"expectancy-positive: {months_pos}"
    )
    bar = "MET" if (overall.expectancy_usd > 0 and overall.profit_factor > 1.2) else "NOT met"
    lines.append(f"   go/no-go bar (expectancy>0 AND PF>1.2): {bar}")
    lines.append("")
    lines.append("## 2. Synthetic scenario matrix (logic proofs; execution stubbed)")
    lines.append(f"   {n_pass}/{len(scenario_results)} scenarios PASS across all K variants")
    for r in scenario_results:
        tag = "PASS" if r.all_passed else "FAIL"
        det = "det" if r.deterministic else "NON-DET"
        lines.append(f"   [{tag}] {r.name}  ({det})")
    lines.append("")
    lines.append("## 3. Live round-trip (Phase 3, AUDIT)")
    lines.append(
        "   "
        + (
            "DONE — see live report."
            if live_done
            else "PENDING Ohad's go — proves the real order plumbing (entry fill, stop"
            " WITH exchange/no Error 321, live walked-stop adopt, guaranteed flatten)."
        )
    )
    lines.append("")
    lines.append("## 4. Fault injection (Phase 4, AUDIT)")
    lines.append(
        "   "
        + (
            "DONE — see fault report."
            if fault_done
            else "PENDING Ohad's go — proves the resting GTC stop survives socket-drop"
            " / gateway-restart + boot re-adopt (§0.5.211); reproduces the"
            " wedged-subscription self-heal gap."
        )
    )
    lines.append("")
    lines.append("## 5. The one residual none of this can shortcut")
    lines.append(
        "   The FORWARD edge in the REAL regime: does this strategy make money on tape it has"
    )
    lines.append(
        "   never seen, after costs? The backtest measures the entry+exit LOGIC over PAST data"
    )
    lines.append(
        "   (and is sample/period-dependent); the synthetic harness proves the PATHS behave;"
    )
    lines.append(
        "   the live tiers prove the PLUMBING. None prove future edge. That is measured forward,"
    )
    lines.append("   in the bot's own intended regime, against the §7 go/no-go bar — not patched.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    if args.quick:
        run = bt.run_backtest(regime=True, limit=60000)
        results = scn.run_all(k=2)
    else:
        run = bt.run_backtest(regime=True)
        results = scn.run_all(k=5)
    print(consolidated(run, results))


if __name__ == "__main__":
    main()
