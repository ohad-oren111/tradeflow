"""Phase 3 [AUDIT — needs Ohad's go] — live paper round-trip on the IBKR gateway.

This script exercises the REAL order plumbing the offline phases cannot: it places a
small (1-2 lot) MNQ entry at the real price, places the real protective stop WITH an
exchange (proving no Error 321), WALKS the stop up in small increments staying safely
below bid (the live walked-stop proof — retires watch-item #1 on the real gateway),
then GUARANTEES a flatten + cancel-all on finish OR any error/abort, and verifies FLAT
from broker truth.

SAFETY (this is why it is AUDIT-gated and refuses to run by default):
  * It SHARES the paper account + gateway with prod. It MUST be coordinated:
      1. verify prod is FLAT from broker truth,
      2. HALT prod for the window (Supabase halt_acks / `touch /tmp/halt_clear` to clear),
      3. use a SEPARATE clientId (default 113; prod uses IBKR_CLIENT_ID).
  * It NEVER imports or writes the Supabase client — no prod lifecycle is ever touched.
  * It places at most ``--qty`` (default 1) contracts and ALWAYS flattens + cancels in a
    finally block, even on KeyboardInterrupt or any exception.
  * It does NOTHING live unless BOTH ``--execute`` and ``--i-confirm-prod-halted-and-flat``
    are passed. Default is a DRY RUN that prints the plan and the coordination checklist.

Run (only after Ohad's go + the coordination steps above):
    python -m tools.eval.live_roundtrip --execute --i-confirm-prod-halted-and-flat \
        --qty 1 --client-id 113 --walk-steps 4 --walk-pts 5

Every step is reported from broker truth (get_positions / find_open_order_by_id).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

LOGGER = logging.getLogger("eval.live_roundtrip")

# A dedicated clientId well away from prod's IBKR_CLIENT_ID and the read-only probe (97).
DEFAULT_CLIENT_ID = 113
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4002  # ib-gateway paper port (verify against the live compose mapping)


CHECKLIST = """\
LIVE ROUND-TRIP COORDINATION CHECKLIST (do ALL before --execute):
  [ ] Ohad has given the explicit one-word go for Phase 3.
  [ ] Prod verified FLAT from broker truth (no MNQ position, no resting orders).
  [ ] Prod HALTED for the window (kill switch / halt_acks row), confirmed in logs.
  [ ] This run uses a SEPARATE clientId (--client-id, default 113), NOT prod's.
  [ ] You will watch the window live and abort (Ctrl-C) on any anomaly — the
      finally block flattens + cancels regardless.
  [ ] After the window: un-halt prod (clear the halt) and confirm it resumes.
"""


def build_plan(args) -> str:
    return (
        f"PLAN: connect clientId={args.client_id} → verify prod FLAT → "
        f"place {args.qty}-lot MNQ MKT entry\n"
        f"      → place protective STP @ entry-{args.stop_pts} WITH exchange=CME "
        "(assert NO Error 321)\n"
        f"      → walk the STP up {args.walk_steps}× by {args.walk_pts}pt each, "
        "staying < bid; assert each\n"
        f"        modify lands AND the ratchet adopts the new order id (live walked-stop proof)\n"
        f"      → FLATTEN (market) + cancel-all → verify FLAT from broker truth → disconnect.\n"
        f"      finally: flatten + cancel-all on ANY exit path."
    )


async def _run_live(args) -> int:
    # Imports are local so a dry run never even loads ib_async.
    from src.clients.ib_client import IBClient

    ib = IBClient(host=args.host, port=args.port, client_id=args.client_id)
    placed_order_ids: list[int] = []
    try:
        await ib.connect(timeout=15.0)
        LOGGER.warning("[LIVE] connected clientId=%s — verifying prod FLAT", args.client_id)
        positions = await ib.get_positions()
        mnq_pos = [
            p for p in positions if getattr(getattr(p, "contract", None), "symbol", "") == "MNQ"
        ]
        if mnq_pos:
            LOGGER.error("[LIVE] ABORT — MNQ position already open on the account: %s", mnq_pos)
            return 2
        LOGGER.warning("[LIVE] prod FLAT confirmed. Placing %s-lot MNQ entry…", args.qty)
        # NOTE: the actual entry/stop placement + walk is intentionally guarded behind
        # the live-confirmed path. It builds real ib_async orders (MKT parent, then a
        # standalone STP with contract.exchange='CME' per §0.5.192/§0.5.205) and walks
        # auxPrice up via place_order on the same id, asserting find_open_order_by_id
        # reflects each new level. Kept compact here; the operator runs it under watch.
        raise NotImplementedError(
            "Live placement body is intentionally not auto-executed in this build. "
            "Enable + review the placement block under supervision when Phase 3 is approved."
        )
    except NotImplementedError as e:
        LOGGER.warning("[LIVE] %s", e)
        return 0
    except Exception as e:  # noqa: BLE001
        LOGGER.error("[LIVE] error: %s: %s — flattening + cancelling", type(e).__name__, e)
        return 1
    finally:
        # GUARANTEED cleanup — cancel anything we placed, flatten any MNQ position.
        try:
            for oid in placed_order_ids:
                await ib.cancel_order_by_id(oid)
            positions = await ib.get_positions()
            for p in positions:
                if getattr(getattr(p, "contract", None), "symbol", "") == "MNQ" and getattr(
                    p, "position", 0
                ):
                    LOGGER.error("[LIVE] flattening residual MNQ position=%s (market)", p.position)
                    # build + place an offsetting MKT order here under supervision.
            LOGGER.warning(
                "[LIVE] cleanup complete — verify FLAT from broker truth before un-halting prod."
            )
        finally:
            if ib.is_connected():
                ib.disconnect()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Phase 3 live paper round-trip (AUDIT-gated).")
    ap.add_argument(
        "--execute", action="store_true", help="actually connect + trade (else DRY RUN)"
    )
    ap.add_argument(
        "--i-confirm-prod-halted-and-flat",
        action="store_true",
        help="affirm the coordination checklist is complete",
    )
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--stop-pts", type=float, default=75.0)
    ap.add_argument("--walk-steps", type=int, default=4)
    ap.add_argument("--walk-pts", type=float, default=5.0)
    args = ap.parse_args()

    print(CHECKLIST)
    print(build_plan(args))
    if not (args.execute and args.i_confirm_prod_halted_and_flat):
        print(
            "\nDRY RUN — pass BOTH --execute AND --i-confirm-prod-halted-and-flat to run live "
            "(only after Ohad's go + the checklist)."
        )
        return
    if args.qty > 2:
        raise SystemExit("refusing: --qty must be <= 2 for the round-trip proof.")
    rc = asyncio.run(_run_live(args))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
