"""Phase 1 — backtest the REAL strategy + REAL trailing-ratchet exit over saved
1-min history with modeled fills.

Drives ``strategy.evaluate_gates`` / ``_regime_ok`` (the live 30m-EMA200 regime gate
+ touch/bullish/ma_order/gap gates) and ``trail_manager.compute_ratcheted_stop`` /
``should_hard_exit`` (base entry-75, +50 lock, 150 trail, 1000 hard-ceiling) over the
roll-handled NQ series. Single-position (live MAX_CONCURRENT=1), Friday weekend
force-close, friction per §0.5.97. Reports overall + per-month segmented stats.

  python -m tools.eval.backtest [--regime on|off] [--roll-adjust] [--limit N] [--friction-pts F]

CAVEATS (printed in the report, never hidden):
  * MODELED fills (entry at signal-bar close; protective stop fills AT the stop
    price intrabar). Assumes the exit MECHANICALLY works — this measures the
    entry+exit LOGIC's expectancy, not the live order plumbing (that's Phase 3).
  * TF-own-entry path only (the SeanBot-triggered second entry path is not modeled).
  * NQ bars (point-identical to MNQ; MNQ $2 multiplier applied for $ P&lines).
  * The regime EMA200 is recomputed over the trailing <=7000-bar window each bar,
    exactly as live — so it is a lightly-warmed EMA (~234 buckets vs span 200), the
    real behavior, not a fully-converged one.
"""

from __future__ import annotations

import argparse
import time as _time
from datetime import datetime, time
from zoneinfo import ZoneInfo

from config.risk_params import RiskParams
from tools.eval import data, engine
from tools.eval.metrics import compute_stats, format_month_table, segment_by_month

_ET = ZoneInfo("America/New_York")


def friday_force_flat(ts: datetime) -> bool:
    """Live weekend force-close: Friday (ET) at/after 16:25 ET (force_close_et)."""
    et = ts.astimezone(_ET)
    return et.weekday() == 4 and et.time() >= time(16, 25)


def run_backtest(
    *,
    regime: bool = True,
    roll_adjust: bool = False,
    limit: int | None = None,
    friction_pts: float = engine.DEFAULT_FRICTION_PTS,
) -> dict:
    params = RiskParams(regime_gate_enabled=regime, exit_mode="trailing")
    df = data.load_history()
    if limit:
        df = df.iloc[:limit].reset_index(drop=True)
    roll_info = None
    if roll_adjust:
        df, roll_info = data.roll_adjust(df)
    span0, span1 = df["time"].iloc[0], df["time"].iloc[-1]
    seg = data.to_segments(df)[0]
    t0 = _time.perf_counter()
    res = engine.simulate_segment(
        seg,
        engine.FastGateEntry(params, "NQ"),
        params,
        friction_pts=friction_pts,
        force_flat=friday_force_flat,
    )
    elapsed = _time.perf_counter() - t0
    return {
        "params": params,
        "result": res,
        "span": (span0, span1),
        "bars": len(df),
        "elapsed": elapsed,
        "roll_info": roll_info,
        "friction_pts": friction_pts,
    }


def format_report(run: dict) -> str:
    res = run["result"]
    trades = res.trades
    overall = compute_stats(trades)
    monthly = segment_by_month(trades)
    reasons: dict[str, int] = {}
    reason_pnl: dict[str, float] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        reason_pnl[t.exit_reason] = reason_pnl.get(t.exit_reason, 0.0) + t.net_usd
    commission_net = sum(t.net_usd_commission_only for t in trades)

    lines = []
    lines.append("=" * 78)
    lines.append("PHASE 1 — BACKTEST (real strategy + real trailing-ratchet exit, modeled fills)")
    lines.append("=" * 78)
    s0, s1 = run["span"]
    lines.append(f"data span     : {s0}  ->  {s1}")
    lines.append(f"bars          : {run['bars']:,}  (1-min NQ, point-identical to MNQ)")
    lines.append(
        f"regime gate   : {'ON (30m EMA200)' if run['params'].regime_gate_enabled else 'OFF'}"
    )
    lines.append(
        f"exit mode     : {run['params'].exit_mode}  "
        "(base -75, +50 lock, 150 trail, 1000 ceiling; no +150 TP leg)"
    )
    lines.append(
        f"friction      : {run['friction_pts']} idx-pt / 2-lot RT (§0.5.97)  "
        f"| engine ran in {run['elapsed']:.0f}s"
    )
    if run["roll_info"] is not None:
        ri = run["roll_info"]
        lines.append(
            f"roll-adjust   : {len(ri.boundaries)} quarterly rolls, "
            f"offset {ri.total_offset_pts:.2f} pt"
        )
    else:
        lines.append(
            "roll-adjust   : OFF (raw front-month; ~9 quarterly rolls distort <0.3% of bars)"
        )
    lines.append("")
    lines.append("-- gate funnel --------------------------------------------------------------")
    lines.append(f"bars seen          : {res.bars_seen:,}")
    lines.append(f"raw long_signals   : {res.signals_raw:,}")
    lines.append(f"entries taken      : {res.entries:,}")
    lines.append(
        f"blocked: regime={res.blocked_regime:,}  filter={res.blocked_filter:,}  "
        f"session_edge={res.blocked_session_edge:,}  cooldown={res.blocked_cooldown:,}  "
        f"in_position={res.blocked_in_position:,}"
    )
    lines.append("")
    lines.append("-- OVERALL ------------------------------------------------------------------")
    for k, v in overall.as_dict().items():
        lines.append(f"  {k:<18}: {v}")
    lines.append(f"  {'net_usd (commission-only model)':<18}: {commission_net:.2f}")
    lines.append("")
    lines.append("-- exit reasons -------------------------------------------------------------")
    for r in sorted(reasons):
        lines.append(f"  {r:<14}: n={reasons[r]:<5} net=${reason_pnl[r]:,.2f}")
    lines.append("")
    lines.append("-- PER-MONTH (n<10 flagged as noise) ----------------------------------------")
    lines.append(format_month_table(monthly))
    lines.append("")
    lines.append("-- CAVEATS ------------------------------------------------------------------")
    lines.append("  * Modeled fills: entry at signal-bar close; protective stop fills AT the")
    lines.append("    stop price intrabar (§0.5.206). Assumes the exit MECHANICALLY works —")
    lines.append("    this is the LOGIC's expectancy, not the live order plumbing (Phase 3).")
    lines.append("  * TF-own-entry path only (SeanBot-triggered 2nd path not modeled).")
    lines.append("  * No active +150 take-profit in trailing mode (bracket.py: trailing returns")
    lines.append(
        "    no TP leg) — the only profit cap is hard_ceiling(1000); +150 is the trail offset."
    )
    lines.append("  * The forward edge in the REAL regime is the one thing no backtest can prove.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regime", choices=["on", "off"], default="on")
    ap.add_argument("--roll-adjust", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--friction-pts", type=float, default=engine.DEFAULT_FRICTION_PTS)
    args = ap.parse_args()
    run = run_backtest(
        regime=(args.regime == "on"),
        roll_adjust=args.roll_adjust,
        limit=args.limit,
        friction_pts=args.friction_pts,
    )
    print(format_report(run))


if __name__ == "__main__":
    main()
