"""One-off (PR #71 Task 0): restate corrupted lifecycle P&L to broker-true.

The pre-#69 notional-entry bug stored ``entry_price = price × multiplier`` on rows
the reconciler force-filled (it read the broker ``avgCost`` — notional for futures —
instead of the per-contract fill). That produces a garbage ``pnl_net``. This script
restates ONLY such rows (flagged by an implausible notional entry price) to the
broker-true fills, and never touches rows whose stored P&L already reconciles to
the broker (e.g. ``347d5a12`` / ``c06ed026``, the −924.98 / −928.98 stops).

Dry-run by default — prints a before/after table and writes NOTHING. Writes only
with ``--apply`` (after operator ``restate`` approval). Refuses to write if any
flagged row lacks a known broker-true correction.

Broker-true values (IB executions, account DUQ331660, captured 2026-06-01 —
see /tmp/broker_pr71.txt):
  b39a4def: entry orderId 43 BOT 2 @ 30444.75; exit orderId 45 SLD 2 @ 30440.5
            (operator flatten — the bracket target LMT @ 30589 never filled, so
            exit_reason is restated TARGET -> MANUAL).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, "/home/tradeflow/tradeflow")

from dotenv import load_dotenv  # noqa: E402

from config.instruments import MNQ  # noqa: E402
from src.clients.supabase_client import SupabaseClient  # noqa: E402

# An entry_price above this is notional-contaminated: MNQ trades ~30,000, so a
# real per-contract price never exceeds this, but a notional (× $2) is ~60,000.
SANITY_ENTRY_MAX = 50000.0

# Broker-true corrections keyed by lifecycle_id (provenance in the module docstring).
CORRECTIONS: dict[str, dict[str, object]] = {
    "b39a4def-8ee6-49fc-bfc4-4801ce60fa61": {
        "entry_price": 30444.75,
        "exit_price": 30440.5,
        "exit_reason": "MANUAL",
    },
}

_FIELDS = ("entry_price", "exit_price", "exit_reason", "pnl_gross", "pnl_net")


def _compute_pnl(
    direction: str, entry: float, exit_: float, qty: int, commission: float
) -> tuple[float, float]:
    delta = exit_ - entry if direction == "LONG" else entry - exit_
    gross = round(delta * qty * MNQ.multiplier, 2)
    return gross, round(gross - commission, 2)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write the corrections (default: dry-run)"
    )
    args = parser.parse_args()

    load_dotenv("/home/tradeflow/.tradeflow-secrets/.env")
    db = SupabaseClient(url=os.environ["SUPABASE_URL"], key=os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    try:
        rows = await db.select("lifecycles", filters={"state": "eq.CLOSED"})
        flagged = [r for r in rows if float(r.get("entry_price") or 0.0) > SANITY_ENTRY_MAX]
        print(
            f"Closed lifecycles: {len(rows)} | "
            f"flagged (entry_price > {SANITY_ENTRY_MAX:.0f}): {len(flagged)}"
        )

        unexpected = [r["lifecycle_id"] for r in flagged if r["lifecycle_id"] not in CORRECTIONS]
        if unexpected:
            print(f"REFUSING — flagged rows without a known broker-true correction: {unexpected}")
            return 2

        plan: dict[str, dict[str, object]] = {}
        print()
        print(f"{'lifecycle':<14}{'field':<14}{'BEFORE':>16}{'AFTER':>16}")
        print("-" * 60)
        for r in flagged:
            lid = str(r["lifecycle_id"])
            corr = CORRECTIONS[lid]
            qty = int(r.get("entry_qty") or 0)
            commission = float(r.get("commission_total") or 0.0)
            gross, net = _compute_pnl(
                str(r.get("direction")),
                float(corr["entry_price"]),  # type: ignore[arg-type]
                float(corr["exit_price"]),  # type: ignore[arg-type]
                qty,
                commission,
            )
            updates = {
                "entry_price": corr["entry_price"],
                "exit_price": corr["exit_price"],
                "exit_reason": corr["exit_reason"],
                "pnl_gross": gross,
                "pnl_net": net,
            }
            plan[lid] = updates
            for field in _FIELDS:
                print(f"{lid[:12]:<14}{field:<14}{str(r.get(field)):>16}{str(updates[field]):>16}")
            print()

        if not flagged:
            print("Nothing to restate.")
            return 0

        if not args.apply:
            print("DRY-RUN — no writes. Re-run with --apply after operator `restate` approval.")
            return 0

        for lid, updates in plan.items():
            await db.update_lifecycle(lid, updates)
            print(f"APPLIED {lid[:12]}: {updates}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
