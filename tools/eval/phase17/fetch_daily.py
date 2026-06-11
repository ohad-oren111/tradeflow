"""Phase-17 Stage-1 — backfill DAILY bars for the basket, stitch continuous (Panama),
DATA-QA, write per-market CSVs. READ-ONLY against the broker (reqContractDetails +
reqHistoricalData only), dedicated clientId, DRY by default.

    # plan only (no connect):
    python -m tools.eval.phase17.fetch_daily
    # actually fetch + write (shares the prod gateway — read-only, separate clientId):
    python -m tools.eval.phase17.fetch_daily --execute --client-id 117

Per market: resolve the full contract chain (``includeExpired=True``), fetch each
contract's daily bars over its life, volume-roll + Panama back-adjust into one continuous
series (``tools.eval.phase17.stitch``), QA it (``dataqa``), and write
``research/data/phase17/<sym>_daily.csv`` (+ ``<sym>_rolls.csv``). The live CONTFUT last
close is pulled once per market as a broker-truth reference (§0.5.98).

Stage 1 STOPS here for operator review — NO strategy, NO portfolio. Output is the data
table + QA verdict the operator greenlights before Stage 2.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

LOGGER = logging.getLogger("eval.phase17.fetch_daily")

HOST, PORT, CLIENT_ID = "127.0.0.1", 4002, 117
OUT_DIR = "/home/tradeflow/tradeflow/research/data/phase17"


def _bars_to_df(bars):
    import pandas as pd

    rows = []
    for b in bars:
        rows.append(
            {
                "time": pd.to_datetime(str(b.date)),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume) if b.volume is not None else 0.0,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    else:
        df["time"] = df["time"].dt.tz_convert("UTC")
    df["time"] = df["time"].dt.normalize()
    return df.dropna(subset=["close"]).sort_values("time").reset_index(drop=True)


def _parse_exp(s: str):
    s = str(s)
    if len(s) >= 8 and s[:8].isdigit():
        return datetime.strptime(s[:8], "%Y%m%d").replace(tzinfo=UTC)
    if len(s) >= 6 and s[:6].isdigit():
        return datetime.strptime(s[:6] + "15", "%Y%m%d").replace(tzinfo=UTC)
    return None


async def _fetch_market(ib, mkt, *, window_months: int, pace: float):
    """Return (continuous_df, RollReport, ref_close, n_contracts_fetched)."""
    import pandas as pd
    from ib_async import ContFuture, Future

    from .stitch import build_continuous

    now = datetime.now(UTC)
    lo = now - timedelta(days=int(window_months * 30.4))
    hi = now + timedelta(days=120)

    cds = await ib.reqContractDetailsAsync(
        Future(mkt.symbol, exchange=mkt.exchange, currency="USD", includeExpired=True)
    )
    # keep contracts whose expiry overlaps the study window
    kept = []
    for cd in cds:
        ex = _parse_exp(cd.contract.lastTradeDateOrContractMonth)
        if ex is None:
            continue
        if lo <= ex <= hi:
            kept.append((cd.contract, ex))
    kept.sort(key=lambda x: x[1])
    LOGGER.warning(
        "[P17] %s: chain=%d in-window=%d (%s..%s)",
        mkt.symbol,
        len(cds),
        len(kept),
        kept[0][0].lastTradeDateOrContractMonth if kept else "-",
        kept[-1][0].lastTradeDateOrContractMonth if kept else "-",
    )

    frames: dict[str, pd.DataFrame] = {}
    n_fetched = 0
    for contract, ex in kept:
        end_dt = ex + timedelta(days=2)
        try:
            bars = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime=end_dt,
                durationStr="1 Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,
                keepUpToDate=False,
            )
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(
                "[P17]   %s %s: fetch error %s",
                mkt.symbol,
                contract.lastTradeDateOrContractMonth,
                type(e).__name__,
            )
            await asyncio.sleep(pace)
            continue
        df = _bars_to_df(bars)
        if not df.empty:
            frames[contract.lastTradeDateOrContractMonth] = df
            n_fetched += 1
        await asyncio.sleep(pace)

    cont, report = build_continuous(frames)

    # broker-truth reference: live CONTFUT last close
    ref_close = None
    try:
        cf = ContFuture(mkt.symbol, exchange=mkt.exchange, currency="USD")
        q = await ib.qualifyContractsAsync(cf)
        if q:
            rb = await ib.reqHistoricalDataAsync(
                cf,
                endDateTime="",
                durationStr="5 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,
                keepUpToDate=False,
            )
            if rb:
                ref_close = float(rb[-1].close)
    except Exception as e:  # noqa: BLE001
        LOGGER.warning("[P17] %s: CONTFUT ref error %s", mkt.symbol, type(e).__name__)
    return cont, report, ref_close, n_fetched


async def _run(args) -> int:
    import pandas as pd
    from ib_async import IB

    from . import markets
    from .dataqa import format_qa, qa_series

    syms = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    basket = [
        m
        for m in markets.BASKET
        if (syms is None and not m.optional) or (syms is not None and m.symbol in syms)
    ]
    os.makedirs(OUT_DIR, exist_ok=True)

    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=args.client_id, timeout=20.0)
    LOGGER.warning(
        "[P17] connected clientId=%s server=%s — %d markets",
        args.client_id,
        ib.client.serverVersion(),
        len(basket),
    )

    qa_blocks, rows = [], []
    try:
        for mkt in basket:
            cont, report, ref, nf = await _fetch_market(
                ib, mkt, window_months=args.window_months, pace=args.pace
            )
            if cont.empty:
                LOGGER.warning("[P17] %s: EMPTY — skipping", mkt.symbol)
                qa_blocks.append(f"### {mkt.symbol} — FAIL\n- NO DATA STITCHED")
                continue
            # Bucket-aware spike tolerance — these are the ABNORMAL-but-REAL single-day
            # moves each asset genuinely produced in-sample (crypto 35%, the 2025-07
            # copper tariff-carveout crash ~24% for metals, energy oil shocks). The roll
            # ARTIFACT guard is separate (`no_residual_roll_spike`, spikes ON roll dates).
            spike = {"crypto": 35.0, "metals": 30.0, "energy": 25.0}.get(mkt.bucket, 20.0)
            res = qa_series(mkt.symbol, cont, report, ref_close=ref, ret_spike_pct=spike)
            qa_blocks.append(format_qa(res))
            rows.append(res)
            # write CSVs
            out = cont[["time", "open", "high", "low", "close", "volume", "active_expiry"]].copy()
            out.to_csv(f"{OUT_DIR}/{mkt.symbol}_daily.csv", index=False)
            roll_df = pd.DataFrame(
                [
                    {
                        "date": e.date.date(),
                        "old": e.old_expiry,
                        "new": e.new_expiry,
                        "gap_pts": e.gap_pts,
                    }
                    for e in report.rolls
                ]
            )
            roll_df.to_csv(f"{OUT_DIR}/{mkt.symbol}_rolls.csv", index=False)
            LOGGER.warning(
                "[P17] %s: wrote %d bars, %d rolls, %d contracts -> %s",
                mkt.symbol,
                len(out),
                len(report.rolls),
                nf,
                OUT_DIR,
            )
    finally:
        ib.disconnect()

    n_pass = sum(1 for r in rows if r.passed)
    print("\n" + "=" * 78)
    print(f"PHASE-17 STAGE-1 DATA-QA — {n_pass}/{len(rows)} markets PASS")
    print("=" * 78 + "\n")
    print("\n\n".join(qa_blocks))
    print("\n" + "=" * 78)
    print("Data written to", OUT_DIR, "— STOP for operator review before Stage 2.")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Phase-17 Stage-1 daily backfill (read-only).")
    ap.add_argument("--execute", action="store_true", help="actually connect + fetch (else DRY)")
    ap.add_argument("--client-id", type=int, default=CLIENT_ID)
    ap.add_argument("--symbols", default="", help="comma list (default = core basket)")
    ap.add_argument("--window-months", type=int, default=30)
    ap.add_argument("--pace", type=float, default=0.35, help="sleep between requests (s)")
    args = ap.parse_args()

    from . import markets

    if not args.execute:
        print("DRY RUN — Phase-17 Stage-1 daily backfill plan (read-only fetch).")
        print(f"  gateway {HOST}:{PORT} clientId={args.client_id} | window={args.window_months}mo")
        print(f"  out: {OUT_DIR}")
        core = markets.core()
        print(f"  core basket ({len(core)} markets):")
        for m in core:
            print(
                f"    {m.symbol:4s} {m.exchange:6s} [{m.bucket:7s}] "
                f"mult={m.multiplier:g} tick={m.min_tick:g} "
                f"tickval=${m.tick_value_usd:.3f}  {m.note}"
            )
        print(
            "\n  Pass --execute to fetch. Optional members:",
            ", ".join(m.symbol for m in markets.BASKET if m.optional),
        )
        return
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
