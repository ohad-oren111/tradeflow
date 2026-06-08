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
DEFAULT_PORT = 4002  # ib-gateway paper port (host-mapped 127.0.0.1:4002 -> container 4004)
DEFAULT_INSTRUMENT = "MNQM6"  # front-month MNQ localSymbol (prod default)
TICK = 0.25  # MNQ min tick (config.instruments.MNQ.tick_size)
# A walked stop must never approach the live bid (it would fill instantly). Keep
# every walked level at least this far below the reference price.
WALK_SAFETY_BUFFER_PTS = 20.0


def _round_tick(price: float) -> float:
    return round(price / TICK) * TICK


def _mkt_order(action: str, qty: int):
    """A standalone 24/5 market order (outsideRth so it fills off-RTH too)."""
    from ib_async import Order

    o = Order()
    o.action = action
    o.totalQuantity = qty
    o.orderType = "MKT"
    o.tif = "GTC"
    o.outsideRth = True
    o.transmit = True
    return o


async def _await_fill(trade, timeout: float = 20.0) -> float | None:
    """Poll a Trade to a terminal state. Return avgFillPrice on Filled, else None."""
    waited = 0.0
    while waited < timeout:
        status = getattr(getattr(trade, "orderStatus", None), "status", None)
        if status == "Filled":
            return float(getattr(trade.orderStatus, "avgFillPrice", 0.0) or 0.0)
        if status in ("Cancelled", "ApiCancelled", "Inactive"):
            LOGGER.error("[LIVE] order reached terminal non-fill status=%s", status)
            return None
        await asyncio.sleep(0.5)
        waited += 0.5
    LOGGER.error("[LIVE] fill timeout after %.0fs (status=%s)", timeout, status)
    return None


async def _await_resting(ib, order_id: int, timeout: float = 10.0):
    """Poll until ``order_id`` appears among live open orders (resting at broker)."""
    waited = 0.0
    while waited < timeout:
        order = await ib.find_open_order_by_id(order_id)
        if order is not None:
            return order
        await asyncio.sleep(0.5)
        waited += 0.5
    return None


async def _await_aux(ib, order_id: int, target_aux: float, timeout: float = 8.0):
    """Poll until the resting order's auxPrice reflects ``target_aux`` (modify landed)."""
    waited = 0.0
    last = None
    while waited < timeout:
        order = await ib.find_open_order_by_id(order_id)
        if order is not None:
            last = float(getattr(order, "auxPrice", 0.0) or 0.0)
            if abs(last - target_aux) < TICK:
                return order
        await asyncio.sleep(0.5)
        waited += 0.5
    LOGGER.error(
        "[LIVE] modify did not land — id=%s want_aux=%.2f last_aux=%s", order_id, target_aux, last
    )
    return None


async def _reference_price(ib, contract) -> float | None:
    """Latest 1-min close as a reliable 'current price' proxy (prod already pulls
    these; avoids the paper-account mkt-data-subscription noise of reqMktData)."""
    try:
        bars = await ib.get_historical_bars(
            contract, bar_size="1 min", what_to_show="TRADES", duration="120 S"
        )
        if bars:
            return float(bars[-1].close)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[LIVE] reference_price fetch failed: %s", exc)
    return None


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


async def _flatten_and_cancel(ib, placed_order_ids: list[int]) -> bool:
    """GUARANTEED cleanup — cancel every order we placed (and any other order our
    clientId still has resting), flatten any MNQ position via an offsetting MKT,
    then re-verify FLAT from broker truth. Returns True iff the book is flat.

    Scope is naturally bounded to our window: openTrades() returns only this
    clientId's orders, and prod is stopped+flat so any MNQ position is ours.
    """
    # 1) cancel everything we placed, plus any straggler open order on our client.
    for oid in list(placed_order_ids):
        await ib.cancel_order_by_id(oid)
    try:
        for trade in await ib.get_open_trades():
            oid = getattr(getattr(trade, "order", None), "orderId", None)
            if oid is not None:
                await ib.cancel_order_by_id(oid)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[LIVE] cleanup: open-trade sweep failed: %s", exc)

    # 2) flatten any residual MNQ position using the position's own contract.
    positions = await ib.get_positions()
    for p in positions:
        c = getattr(p, "contract", None)
        if getattr(c, "symbol", "") != "MNQ":
            continue
        pos = int(getattr(p, "position", 0) or 0)
        if pos == 0:
            continue
        c.exchange = "CME"  # position-derived contracts omit exchange (§0.5.192)
        side = "SELL" if pos > 0 else "BUY"
        LOGGER.error("[LIVE] flattening residual MNQ position=%s via %s MKT", pos, side)
        flat_trade = await ib.place_order(c, _mkt_order(side, abs(pos)))
        await _await_fill(flat_trade, timeout=20.0)

    # 3) re-verify FLAT from broker truth.
    positions = await ib.get_positions()
    residual = sum(
        int(getattr(p, "position", 0) or 0)
        for p in positions
        if getattr(getattr(p, "contract", None), "symbol", "") == "MNQ"
    )
    open_after = await ib.get_open_trades()
    flat = residual == 0 and len(open_after) == 0
    LOGGER.warning(
        "[LIVE] cleanup verify — MNQ_residual=%s open_orders=%s FLAT=%s",
        residual,
        len(open_after),
        "YES" if flat else "NO",
    )
    return flat


async def _run_live(args) -> int:
    # Imports are local so a dry run never even loads ib_async.
    from src.clients.ib_client import IBClient
    from src.execution.bracket import build_protective_stop
    from src.orchestrator import _build_contract
    from src.state_machine import Direction

    ib = IBClient(host=args.host, port=args.port, client_id=args.client_id)
    placed_order_ids: list[int] = []
    errors: list[tuple] = []

    def _on_error(reqId, errorCode, errorString, contract=None):  # noqa: N803
        errors.append((reqId, errorCode, errorString))
        if errorCode == 321:
            LOGGER.error("[LIVE] !! Error 321 observed — reqId=%s msg=%s", reqId, errorString)

    summary: dict = {
        "entry_fill": None,
        "stop_price": None,
        "no_321": None,
        "stop_rested": None,
        "walk": [],
    }
    rc = 1
    try:
        await ib.connect(timeout=15.0)
        ib._ib.errorEvent += _on_error
        LOGGER.warning("[LIVE] connected clientId=%s — verifying prod FLAT", args.client_id)
        positions = await ib.get_positions()
        mnq_pos = [
            p
            for p in positions
            if getattr(getattr(p, "contract", None), "symbol", "") == "MNQ"
            and int(getattr(p, "position", 0) or 0) != 0
        ]
        open_now = await ib.get_open_trades()
        if mnq_pos or open_now:
            LOGGER.error(
                "[LIVE] ABORT — account not flat: MNQ pos=%s open_orders=%s", mnq_pos, len(open_now)
            )
            return 2

        # Resolve + qualify the front-month contract (exchange=CME per §0.5.192).
        contract = _build_contract(args.instrument)
        contract.exchange = "CME"
        qualified = await ib._ib.qualifyContractsAsync(contract)
        if not qualified:
            LOGGER.error("[LIVE] ABORT — could not qualify %s", args.instrument)
            return 2
        contract = qualified[0]
        LOGGER.warning(
            "[LIVE] contract resolved — localSymbol=%s conId=%s exchange=%s",
            contract.localSymbol,
            contract.conId,
            contract.exchange,
        )

        ref_price = await _reference_price(ib, contract)
        LOGGER.warning("[LIVE] reference (last 1-min close) = %s", ref_price)

        # ---- ENTRY: 1-lot MNQ MKT at real price ----
        LOGGER.warning("[LIVE] placing %s-lot MNQ MKT BUY…", args.qty)
        entry_trade = await ib.place_order(contract, _mkt_order("BUY", args.qty))
        placed_order_ids.append(entry_trade.order.orderId)
        entry_fill = await _await_fill(entry_trade, timeout=20.0)
        if entry_fill is None or entry_fill <= 0:
            LOGGER.error("[LIVE] entry did not fill — aborting (cleanup in finally)")
            return 1
        summary["entry_fill"] = entry_fill
        LOGGER.warning("[LIVE] ENTRY FILLED @ %.2f", entry_fill)
        # entry is the freshest real price; use the lower of it / ref as a
        # conservative bid proxy for the walk-safety ceiling.
        bid_proxy = min(x for x in (ref_price, entry_fill) if x)

        # ---- PROTECTIVE STOP @ entry-stop_pts WITH exchange=CME (assert NO 321) ----
        stop_price = _round_tick(entry_fill - args.stop_pts)
        summary["stop_price"] = stop_price
        stp = build_protective_stop(direction=Direction.LONG, qty=args.qty, stop_price=stop_price)
        LOGGER.warning("[LIVE] placing protective STP @ %.2f (exchange=CME)…", stop_price)
        stop_trade = await ib.place_order(contract, stp)
        stop_order_id = stop_trade.order.orderId
        placed_order_ids.append(stop_order_id)
        rested = await _await_resting(ib, stop_order_id, timeout=10.0)
        await asyncio.sleep(1.0)  # let any rejection error surface
        no_321 = not any(code == 321 for _, code, _ in errors)
        summary["no_321"] = no_321
        landed = await ib.find_open_order_by_id(stop_order_id)
        stop_rested = (
            rested is not None
            and landed is not None
            and abs(float(landed.auxPrice) - stop_price) < TICK
        )
        summary["stop_rested"] = stop_rested
        LOGGER.warning(
            "[LIVE] STP id=%s rested=%s aux=%s no_321=%s",
            stop_order_id,
            stop_rested,
            getattr(landed, "auxPrice", None),
            no_321,
        )
        if not (stop_rested and no_321):
            LOGGER.error(
                "[LIVE] stop did not rest cleanly (rested=%s no_321=%s) — aborting",
                stop_rested,
                no_321,
            )
            return 1

        # ---- WALK the stop up; each modify must LAND and KEEP the same order id ----
        for step in range(1, args.walk_steps + 1):
            new_stop = _round_tick(stop_price + step * args.walk_pts)
            if new_stop >= bid_proxy - WALK_SAFETY_BUFFER_PTS:
                LOGGER.warning(
                    "[LIVE] walk stop %d skipped — %.2f within safety buffer of bid≈%.2f",
                    step,
                    new_stop,
                    bid_proxy,
                )
                break
            order = await ib.find_open_order_by_id(stop_order_id)
            if order is None:
                LOGGER.error(
                    "[LIVE] walk %d — stop id %s no longer live; abort", step, stop_order_id
                )
                summary["walk"].append({"step": step, "ok": False, "reason": "id_gone"})
                return 1
            order.auxPrice = new_stop
            mod_trade = await ib.place_order(contract, order)  # same id => modify
            adopted_id = int(mod_trade.order.orderId)
            confirmed = await _await_aux(ib, stop_order_id, new_stop, timeout=8.0)
            ok = confirmed is not None and adopted_id == stop_order_id
            summary["walk"].append(
                {
                    "step": step,
                    "new_stop": new_stop,
                    "adopted_id": adopted_id,
                    "landed_aux": float(getattr(confirmed, "auxPrice", 0.0)) if confirmed else None,
                    "ok": ok,
                }
            )
            LOGGER.warning(
                "[LIVE] WALK %d → stop=%.2f adopted_id=%s (orig=%s) landed=%s ok=%s",
                step,
                new_stop,
                adopted_id,
                stop_order_id,
                getattr(confirmed, "auxPrice", None),
                ok,
            )
            if not ok:
                LOGGER.error("[LIVE] walk modify %d failed to land/adopt — abort", step)
                return 1

        walked = [w for w in summary["walk"] if w.get("ok")]
        rc = 0 if walked else 1
        if not walked:
            LOGGER.error("[LIVE] no walked-stop step landed — watch-item #1 NOT proven")
        else:
            LOGGER.warning(
                "[LIVE] walked-stop proof OK — %d modifies landed + adopted same id", len(walked)
            )
        return rc
    except Exception as e:  # noqa: BLE001
        LOGGER.error("[LIVE] error: %s: %s — flattening + cancelling", type(e).__name__, e)
        rc = 1
        return rc
    finally:
        # GUARANTEED cleanup on EVERY exit path (success / error / KeyboardInterrupt).
        try:
            if ib.is_connected:
                flat = await _flatten_and_cancel(ib, placed_order_ids)
                summary["final_flat"] = flat
                if not flat:
                    LOGGER.error("[LIVE] !! NOT FLAT after cleanup — MANUAL INTERVENTION NEEDED")
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[LIVE] cleanup itself errored: %s: %s", type(exc).__name__, exc)
        finally:
            if ib.is_connected:
                ib.disconnect()
        print("\n=== PHASE 3 SUMMARY ===")
        print(f"  entry_fill   : {summary.get('entry_fill')}")
        print(f"  stop_price   : {summary.get('stop_price')}")
        print(f"  no_Error_321 : {summary.get('no_321')}")
        print(f"  stop_rested  : {summary.get('stop_rested')}")
        for w in summary.get("walk", []):
            print(f"  walk         : {w}")
        print(f"  final_flat   : {summary.get('final_flat')}")


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
    ap.add_argument("--instrument", default=DEFAULT_INSTRUMENT, help="front-month localSymbol")
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
