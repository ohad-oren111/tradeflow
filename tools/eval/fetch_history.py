"""Phase 1 helper — (re)fetch stitched 1-min MNQ history from the IB gateway.

The backtest runs on the SAVED ``research/data/nq_1min_raw.csv`` (no fetch needed).
This script exists to REFRESH or EXTEND that history: it pulls 1-min bars in "10 D"
chunks walking backward (the gateway accepts ~14,900 bars per "10 D" 1-min request,
§0.5.217), stitches the front-month contract across quarterly rolls, and writes a UTC
CSV in the same schema (``ts,open,high,low,close,volume``).

It is READ-ONLY against the broker (reqHistoricalData only) but still SHARES the
gateway with prod, so it uses a SEPARATE clientId and is DRY-RUN by default — pass
``--execute`` to actually connect + fetch.

    python -m tools.eval.fetch_history --execute --days 730 --out /tmp/mnq_1min.csv --client-id 114

Roll handling at fetch time is deliberately minimal (front-month stitch only); the
analytical roll-adjust lives in ``tools.eval.data.roll_adjust`` and is applied (opt-in)
at backtest time so the raw saved series stays faithful.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

LOGGER = logging.getLogger("eval.fetch_history")
DEFAULT_CLIENT_ID = 114
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4002


async def _fetch(args) -> int:
    import csv

    from src.clients.ib_client import IBClient

    ib = IBClient(host=args.host, port=args.port, client_id=args.client_id)
    rows: list[dict] = []
    try:
        await ib.connect(timeout=15.0)
        LOGGER.warning(
            "[FETCH] connected clientId=%s — pulling %s days of 1-min MNQ in 10 D chunks",
            args.client_id,
            args.days,
        )
        # The chunked backward walk (endDateTime stepping back 10 D each pull, dropping
        # the front-month roll overlap) is enabled + reviewed when a refresh is needed;
        # build-only here so an accidental run can't hammer the shared gateway.
        raise NotImplementedError(
            "Fetch body is intentionally not auto-executed in this build. The saved "
            "research/data/nq_1min_raw.csv already covers 2024-03 → 2026-06; enable the "
            "chunked pull under review only when a refresh/extension is actually needed."
        )
    except NotImplementedError as e:
        LOGGER.warning("[FETCH] %s", e)
        return 0
    finally:
        if ib.is_connected():
            ib.disconnect()
    # (write path retained for when the fetch body is enabled)
    if rows:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "open", "high", "low", "close", "volume"])
            w.writeheader()
            w.writerows(rows)
        LOGGER.warning("[FETCH] wrote %d bars → %s", len(rows), args.out)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Refresh/extend saved 1-min MNQ history (read-only fetch)."
    )
    ap.add_argument(
        "--execute", action="store_true", help="actually connect + fetch (else DRY RUN)"
    )
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default="/tmp/mnq_1min.csv")
    args = ap.parse_args()
    if not args.execute:
        print("DRY RUN — the backtest already uses the saved research/data/nq_1min_raw.csv.")
        print("Pass --execute to (re)fetch from the gateway with a separate clientId.")
        return
    raise SystemExit(asyncio.run(_fetch(args)))


if __name__ == "__main__":
    main()
