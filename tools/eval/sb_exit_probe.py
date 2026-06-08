"""Phase-6 (research, OFFLINE) — SeanBot-signal exit probe (directional, small-n).

Reads the captured ``seanbot_signals`` LONG entries (READ-ONLY) and runs them
through the REAL exit (``engine._process_exit_bar``) over the saved 1-min history,
for (a) TF's CURRENT exit and (b) the BEST swept exit — then compares to TF's OWN
gate entries over the SAME window with the SAME (independent-per-entry) exit model.

This isolates the EXIT: same machinery on both entry sets, so any expectancy gap is
attributable to WHICH bars each strategy enters on. Each entry is evaluated
INDEPENDENTLY (no single-position/cooldown coupling) — we are scoring the exit on a
GIVEN set of entries, not simulating how live TF would interleave them.

CAVEATS (loud, never hidden):
  * n is TINY (~1.5 weeks of capture; only the portion inside the saved history is
    simulable) — DIRECTIONAL, not a verdict.
  * Entry re-priced to the history bar CLOSE at the signal minute (the backtest fill
    model); SB's reported price is shown alongside (mean abs delta) for alignment.
  * SB entries past the history end are DEFERRED (no bars) and counted, not dropped silently.
  * Modeled fills; §0.5.97 friction; NQ≈MNQ points, $2 mult.

  python -m tools.eval.sb_exit_probe [--best-stop 75 --best-lock 50 --best-trail 150] [--from-json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import numpy as np

from tools.eval import data, engine
from tools.eval.backtest import friday_force_flat
from tools.eval.engine import Trade, _OpenPosition, _pnl, _process_exit_bar
from tools.eval.exit_sweep import BASE_LOCK, BASE_STOP, BASE_TRAIL, cfg_params
from tools.eval.metrics import compute_stats

SB_JSON = "/tmp/tf_sb_entries.json"


async def _load_sb_entries_live() -> list[dict]:
    from dotenv import load_dotenv

    from src.clients.supabase_client import SupabaseClient

    load_dotenv("/home/tradeflow/.tradeflow-secrets/.env")
    db = SupabaseClient(url=os.environ["SUPABASE_URL"], key=os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    try:
        rows = await db.select(
            "seanbot_signals",
            filters={"type": "eq.entry", "parsed_ok": "eq.true", "order": "ts.asc"},
            columns="ts,direction,symbol,price",
        )
        return [r for r in rows if (r.get("direction") or "").lower() == "long"]
    finally:
        await db.close()


def load_sb_entries(from_json: bool) -> list[dict]:
    if from_json and os.path.exists(SB_JSON):
        with open(SB_JSON) as fh:
            return json.load(fh)
    return asyncio.run(_load_sb_entries_live())


def eval_entry_independent(arrays, k: int, entry_price: float, params, *, qty=2) -> Trade | None:
    """Run the real exit from bar k+1 forward, independently. Drops (None) if the
    position never closes before the history end (truncated)."""
    ts, high, low, close = arrays
    n = len(ts)
    pos = _OpenPosition(
        entry_ts=ts[k],
        entry_price=entry_price,
        highest=entry_price,
        current_stop=entry_price - params.stop_loss_pts,
        bars_held=0,
    )
    j = k + 1
    while j < n:
        c = _process_exit_bar(
            pos,
            params,
            ts=ts[j],
            high=float(high[j]),
            low=float(low[j]),
            close=float(close[j]),
            force_flat=friday_force_flat,
        )
        pos.bars_held += 1
        if c is not None:
            gp, gu, nf, nc = _pnl(pos.entry_price, c[0], engine.DEFAULT_FRICTION_PTS, qty)
            return Trade(
                entry_ts=ts[k],
                entry_price=entry_price,
                exit_ts=ts[j],
                exit_price=c[0],
                exit_reason=c[1],
                bars_held=pos.bars_held,
                highest=pos.highest,
                final_stop=pos.current_stop,
                gross_pts=gp,
                gross_usd=gu,
                net_usd=nf,
                net_usd_commission_only=nc,
            )
        j += 1
    return None  # truncated at history end


def _fmt(s) -> str:
    pf = f"{s.profit_factor:.3f}" if s.profit_factor != float("inf") else "inf"
    return (
        f"n={s.n:<4} win={s.win_rate*100:5.1f}% exp=${s.expectancy_usd:7.2f} "
        f"PF={pf:>6} net=${s.net_usd:9.2f} maxDD=${s.max_drawdown_usd:8.2f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--best-stop", type=float, default=BASE_STOP)
    ap.add_argument("--best-lock", type=float, default=BASE_LOCK)
    ap.add_argument("--best-trail", type=float, default=BASE_TRAIL)
    ap.add_argument("--from-json", action="store_true", help="read SB entries from cached json")
    args = ap.parse_args()

    base = cfg_params(BASE_STOP, BASE_LOCK, BASE_TRAIL)
    best = cfg_params(args.best_stop, args.best_lock, args.best_trail)

    df = data.load_history()
    ts = [t.to_pydatetime() if hasattr(t, "to_pydatetime") else t for t in df["time"].tolist()]
    arrays = (
        ts,
        df["high"].to_numpy(float),
        df["low"].to_numpy(float),
        df["close"].to_numpy(float),
    )
    hist_times = np.array([t.timestamp() for t in ts])
    hist_start, hist_end = ts[0], ts[-1]

    sb = load_sb_entries(args.from_json)
    from datetime import datetime

    def _parse(tss):
        return datetime.fromisoformat(tss)

    # Map each SB entry to the first history bar >= its ts. A bar more than
    # MAX_GAP_SEC after the signal means the signal fell in a history gap
    # (weekend / coverage hole) — searchsorted would jump to a far bar at a very
    # different price, so those are gap-deferred (NOT silently mis-simulated).
    # Two entry-price models, reported side by side (they bound the answer):
    #   @close  — re-price to the history bar close (the TF backtest fill model).
    #   @sbpx   — SB's ACTUAL reported entry price (faithful to SB's dip fill).
    # SB buys the pullback near the bar low, so @close systematically enters HIGHER
    # (worse) than SB did; @sbpx removes that bias. The signed delta is reported.
    max_gap_sec = 180.0
    deferred = gap_deferred = 0
    signed_deltas, price_deltas = [], []
    sb_close_base, sb_close_best, sb_sbpx_base, sb_sbpx_best = [], [], [], []
    for r in sb:
        ets = _parse(r["ts"])
        if ets < hist_start or ets > hist_end:
            deferred += 1
            continue
        k = int(np.searchsorted(hist_times, ets.timestamp(), side="left"))
        if k >= len(ts):
            deferred += 1
            continue
        if abs(ts[k].timestamp() - ets.timestamp()) > max_gap_sec:
            gap_deferred += 1
            continue
        hist_close = float(arrays[3][k])
        sbpx = float(r["price"]) if r.get("price") is not None else None
        if sbpx is not None:
            signed_deltas.append(sbpx - hist_close)
            price_deltas.append(abs(sbpx - hist_close))
        for px, blist, xlist in (
            (hist_close, sb_close_base, sb_close_best),
            (sbpx if sbpx is not None else hist_close, sb_sbpx_base, sb_sbpx_best),
        ):
            tb = eval_entry_independent(arrays, k, px, base)
            tx = eval_entry_independent(arrays, k, px, best)
            if tb is not None:
                blist.append(tb)
            if tx is not None:
                xlist.append(tx)

    # TF's OWN gate entries over the SAME window, same independent exit model.
    # Reuse the cached entry tape from the sweep if present (avoids a 17-min rebuild).
    import pickle

    from tools.eval.exit_sweep import TAPE_CACHE, build_tape, entry_params, load_tape

    tape = None
    try:
        tape = load_tape(TAPE_CACHE)
        if tape.bars != len(df):
            tape = None
    except (OSError, pickle.PickleError, TypeError, AttributeError):
        tape = None
    if tape is None:
        seg = data.to_segments(df)[0]
        tape = build_tape(seg, entry_params(), "NQ")
    sb_lo = min((_parse(r["ts"]) for r in sb), default=hist_start)
    win_lo = max(sb_lo, hist_start)
    tf_idx = [int(i) for i in tape.sig_idx if win_lo <= tape.ts[int(i)] <= hist_end]
    tf_base = [
        t
        for t in (eval_entry_independent(arrays, k, float(tape.close[k]), base) for k in tf_idx)
        if t
    ]
    tf_best = [
        t
        for t in (eval_entry_independent(arrays, k, float(tape.close[k]), best) for k in tf_idx)
        if t
    ]

    print("=" * 88)
    print("PHASE 6 — SEANBOT-SIGNAL EXIT PROBE (directional, small-n)")
    print("=" * 88)
    print(f"history span     : {hist_start}  ->  {hist_end}")
    print(
        f"SB long entries  : captured={len(sb)}  simulable={len(sb_close_base)}  "
        f"deferred(no bars)={deferred}  gap_deferred(history hole)={gap_deferred}"
    )
    if price_deltas:
        print(
            f"SB px - hist close: mean={np.mean(signed_deltas):+.2f}pt "
            f"mean|Δ|={np.mean(price_deltas):.2f}pt max={np.max(price_deltas):.2f}pt "
            f"(negative => SB filled below the bar close = the pullback/dip entry)"
        )
    print(
        f"best swept exit  : stop{args.best_stop:.0f}/lock{args.best_lock:.0f}/"
        f"trail{args.best_trail:.0f}"
    )
    print("")
    print(
        "-- SB entries through each exit (independent per-entry; two entry-price models) ----------"
    )
    print("  entry @ history bar close (TF fill model):")
    print(f"    TF current exit : {_fmt(compute_stats(sb_close_base))}")
    print(f"    best swept exit : {_fmt(compute_stats(sb_close_best))}")
    print("  entry @ SB's actual reported price (faithful to SB's dip fill):")
    print(f"    TF current exit : {_fmt(compute_stats(sb_sbpx_base))}")
    print(f"    best swept exit : {_fmt(compute_stats(sb_sbpx_best))}")
    print("")
    print(f"-- TF's OWN gate entries, same window ({win_lo.date()}..{hist_end.date()}) ----------")
    print(f"  TF current exit : {_fmt(compute_stats(tf_base))}")
    print(f"  best swept exit : {_fmt(compute_stats(tf_best))}")
    print("")
    print("-- CAVEATS ----------")
    print("  * n is TINY (~1.5 weeks; only the in-history portion simulable) — DIRECTIONAL only.")
    print(
        "  * Entries re-priced to history bar close; independent per-entry exit (no concurrency)."
    )
    print("  * Modeled fills; §0.5.97 friction; NQ≈MNQ points. Forward edge unprovable here.")


if __name__ == "__main__":
    main()
