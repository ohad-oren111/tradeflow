"""Phase 4 [AUDIT — needs Ohad's go] — fault injection.

Goal: deterministically reproduce the failure modes the offline phases can only model.
Verifies:
  * a resting GTC protective stop SURVIVES a socket-drop and that a fresh connect
    (boot-recovery) re-adopts it from broker truth (§0.5.211) — the ``socket-drop``
    fault, run LIVE during the coordinated prod-stopped window;
  * the wedged-subscription self-heal GAP reproduces deterministically — the
    ``wedged-subscription`` fault, run OFFLINE (no gateway, no position): it drives
    the REAL ``Orchestrator._watchdog_check_bar_liveness`` / ``_maybe_heal_stale_feed``
    on a frozen clock with a resubscribe that never restores the feed, pinning the
    "resubscribe-forever, never-escalate" gap so the queued escalation fix becomes
    verifiable.

Faults:
  socket-drop      — LIVE. Place 1-lot MNQ + GTC STP on the (now-free) prod gateway
                     with a SEPARATE clientId, hard-drop the client socket, reconnect
                     same clientId, assert the stop + position survived and are re-adopted
                     from broker truth, then GUARANTEED flatten + cancel-all. Requires
                     ``--execute --i-confirm-prod-stopped-and-flat`` (prod must be stopped
                     so the shared paper account is isolated to this test).
  gateway-restart  — DEFERRED against the prod gateway: a 2nd IB Gateway on the same
                     paper login boots the first session, and restarting the prod gateway
                     risks the window-close health check. socket-drop already proves the
                     same survive+re-adopt property at broker truth. Runs only if a
                     genuinely separate ``--throwaway-gateway-port`` (≠ prod 4002) is given.
  wedged-subscription — OFFLINE deterministic reproduction (no gateway/position).

Run:
    # socket-drop (LIVE, in-window, prod stopped):
    python -m tools.eval.fault_injection --fault socket-drop --execute \
        --i-confirm-prod-stopped-and-flat --client-id 121
    # wedged-subscription (OFFLINE, anytime):
    python -m tools.eval.fault_injection --fault wedged-subscription --execute
"""

from __future__ import annotations

import argparse
import asyncio
import logging

LOGGER = logging.getLogger("eval.fault_injection")

PROD_GATEWAY_PORT = 4002  # host-mapped prod paper gateway (127.0.0.1:4002 -> container 4004)
DEFAULT_HOST = "127.0.0.1"

FAULTS = ("socket-drop", "gateway-restart", "wedged-subscription")

MATRIX = """\
FAULT MATRIX:
  socket-drop          → GTC stop + position survive a client socket-drop; fresh connect
                         (boot-recovery) re-adopts the resting stop from broker truth (§0.5.211)
  gateway-restart      → same survive+re-adopt property; DEFERRED on the prod gateway (needs a
                         separate paper login / 2nd gateway; socket-drop already proves it)
  wedged-subscription  → stale-bar watchdog fires + self-heal resubscribes forever WITHOUT
                         escalating while the feed stays wedged — the gap, pinned deterministically
"""


# --------------------------------------------------------------- socket-drop (LIVE)


async def _run_socket_drop(args) -> int:
    """Place position + GTC stop, hard-drop the socket, reconnect, assert survival."""
    from src.clients.ib_client import IBClient
    from src.execution.bracket import build_protective_stop
    from src.orchestrator import _build_contract
    from src.state_machine import Direction
    from tools.eval.live_roundtrip import (
        TICK,
        _await_fill,
        _await_resting,
        _flatten_and_cancel,
        _mkt_order,
        _round_tick,
    )

    summary: dict = {
        "entry_fill": None,
        "stop_price": None,
        "stop_id": None,
        "stop_survived": None,
        "position_survived": None,
        "readopted": None,
        "final_flat": None,
    }
    placed_order_ids: list[int] = []
    ib = IBClient(host=args.host, port=PROD_GATEWAY_PORT, client_id=args.client_id)
    ib2: object | None = None
    rc = 1
    try:
        await ib.connect(timeout=15.0)
        LOGGER.warning("[FAULT] socket-drop connected clientId=%s — verifying FLAT", args.client_id)
        positions = await ib.get_positions()
        mnq = [
            p
            for p in positions
            if getattr(getattr(p, "contract", None), "symbol", "") == "MNQ"
            and int(getattr(p, "position", 0) or 0) != 0
        ]
        open_now = await ib.get_open_trades()
        if mnq or open_now:
            LOGGER.error("[FAULT] ABORT — account not flat (pos=%s open=%s)", mnq, len(open_now))
            return 2

        contract = _build_contract(args.instrument)
        contract.exchange = "CME"
        qualified = await ib._ib.qualifyContractsAsync(contract)
        if not qualified:
            LOGGER.error("[FAULT] ABORT — could not qualify %s", args.instrument)
            return 2
        contract = qualified[0]

        # entry + GTC protective stop
        entry_trade = await ib.place_order(contract, _mkt_order("BUY", args.qty))
        placed_order_ids.append(entry_trade.order.orderId)
        entry_fill = await _await_fill(entry_trade, timeout=20.0)
        if not entry_fill:
            LOGGER.error("[FAULT] entry did not fill — abort (cleanup in finally)")
            return 1
        summary["entry_fill"] = entry_fill
        stop_price = _round_tick(entry_fill - args.stop_pts)
        summary["stop_price"] = stop_price
        stp = build_protective_stop(direction=Direction.LONG, qty=args.qty, stop_price=stop_price)
        stop_trade = await ib.place_order(contract, stp)
        stop_id = int(stop_trade.order.orderId)
        summary["stop_id"] = stop_id
        placed_order_ids.append(stop_id)
        rested = await _await_resting(ib, stop_id, timeout=10.0)
        if rested is None:
            LOGGER.error("[FAULT] stop never rested — abort")
            return 1
        LOGGER.warning(
            "[FAULT] armed — entry=%.2f GTC STP id=%s @ %.2f. Dropping socket…",
            entry_fill,
            stop_id,
            stop_price,
        )

        # ---- INDUCE SOCKET DROP: hard-disconnect the client. Gateway stays up;
        #      the GTC order rests server-side at IBKR. ----
        ib.disconnect()
        await asyncio.sleep(3.0)
        LOGGER.warning(
            "[FAULT] socket dropped; reconnecting fresh clientId=%s (boot-recovery)", args.client_id
        )

        # ---- RECONNECT as the same clientId → boot-recovery re-discovers resting orders ----
        ib2 = IBClient(host=args.host, port=PROD_GATEWAY_PORT, client_id=args.client_id)
        await ib2.connect(timeout=15.0)
        await asyncio.sleep(2.0)  # let reqOpenOrders / position snapshot populate
        open_after = await ib2.get_open_trades()
        readopted_order = await ib2.find_open_order_by_id(stop_id)
        stop_survived = (
            readopted_order is not None
            and abs(float(getattr(readopted_order, "auxPrice", 0.0)) - stop_price) < TICK
        )
        positions2 = await ib2.get_positions()
        pos_qty = sum(
            int(getattr(p, "position", 0) or 0)
            for p in positions2
            if getattr(getattr(p, "contract", None), "symbol", "") == "MNQ"
        )
        position_survived = pos_qty == args.qty
        summary["stop_survived"] = stop_survived
        summary["position_survived"] = position_survived
        summary["readopted"] = readopted_order is not None
        LOGGER.warning(
            "[FAULT] after reconnect — open_orders=%s stop_survived=%s pos_qty=%s readopted=%s",
            len(open_after),
            stop_survived,
            pos_qty,
            readopted_order is not None,
        )
        rc = 0 if (stop_survived and position_survived) else 1
        if rc == 0:
            LOGGER.warning("[FAULT] §0.5.211 PROVEN — GTC stop survived socket-drop + re-adopted")
        else:
            LOGGER.error(
                "[FAULT] §0.5.211 NOT proven — survived=%s readopted=%s",
                position_survived,
                stop_survived,
            )
        return rc
    except Exception as e:  # noqa: BLE001
        LOGGER.error("[FAULT] socket-drop error: %s: %s", type(e).__name__, e)
        return 1
    finally:
        # GUARANTEED cleanup on whichever client is connected.
        cleanup_ib = ib2 if (ib2 is not None and ib2.is_connected) else ib
        try:
            if cleanup_ib.is_connected:
                flat = await _flatten_and_cancel(cleanup_ib, placed_order_ids)
                summary["final_flat"] = flat
                if not flat:
                    LOGGER.error("[FAULT] !! NOT FLAT after cleanup — MANUAL INTERVENTION NEEDED")
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[FAULT] cleanup errored: %s: %s", type(exc).__name__, exc)
        finally:
            for c in (ib, ib2):
                if c is not None and getattr(c, "is_connected", False):
                    c.disconnect()
        print("\n=== PHASE 4 socket-drop SUMMARY ===")
        for k, v in summary.items():
            print(f"  {k:18s}: {v}")


# ---------------------------------------------- wedged-subscription (OFFLINE, deterministic)


class _FrozenClock:
    """Drop-in for ``datetime`` returning a frozen ``now`` (delegates the rest)."""

    def __init__(self, frozen):
        self._frozen = frozen

    def now(self, tz=None):
        if tz is None:
            return self._frozen.replace(tzinfo=None)
        return self._frozen.astimezone(tz)

    def __getattr__(self, name):
        from datetime import datetime as _dt  # noqa: N813

        return getattr(_dt, name)


async def _reproduce_wedged_subscription() -> int:
    """Deterministically pin the wedged-feed self-heal GAP.

    Drives the REAL orchestrator watchdog + self-heal on a frozen clock with a
    resubscribe that never restores the feed (``_last_bar_at`` never advances).
    Shows: while the feed stays wedged, the watchdog keeps detecting staleness and
    the self-heal keeps resubscribing on its 5-min cooldown, but the feed NEVER
    recovers and there is NO escalation to a harder action — the gap the queued
    escalation fix must close.
    """
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock, MagicMock

    import src.orchestrator as orch_mod
    from src.clients.ib_client import IBClient
    from src.clients.supabase_client import SupabaseClient
    from src.orchestrator import (
        _WATCHDOG_FEED_HEAL_COOLDOWN_SEC,
        _WATCHDOG_STALE_THRESHOLD_SEC,
        Orchestrator,
    )

    # Minimal orchestrator (no IO; we only call the sync watchdog + async self-heal).
    mock_ib = AsyncMock(spec=IBClient)
    mock_ib._host, mock_ib._port, mock_ib._client_id = DEFAULT_HOST, PROD_GATEWAY_PORT, 1
    mock_ib._ib = MagicMock(name="raw_IB")
    mock_db = AsyncMock(spec=SupabaseClient)
    orch = Orchestrator(mock_ib, mock_db, paper_account="DUQ331660")

    # The wedge: a resubscribe that "succeeds" but delivers NO new bar, so the
    # feed timestamp never advances — exactly the socket-alive/feed-dead outage.
    orch._start_bar_subscription = AsyncMock()

    # In-session Monday 14:00 UTC; last real bar at t0, then silence forever.
    t0 = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    orch._last_bar_at = t0

    real_dt = orch_mod.datetime
    detections = 0
    heals = 0
    timeline_min = 35  # > several 5-min heal cooldowns
    try:
        for minute in range(1, timeline_min + 1):
            now = t0 + timedelta(seconds=60 * minute)
            orch_mod.datetime = _FrozenClock(now)
            if orch._watchdog_check_bar_liveness():
                detections += 1
                before = orch._start_bar_subscription.await_count
                await orch._maybe_heal_stale_feed()
                if orch._start_bar_subscription.await_count > before:
                    heals += 1
            # feed stays wedged: the stub never advances _last_bar_at.
    finally:
        orch_mod.datetime = real_dt

    feed_still_wedged = orch._last_bar_at == t0
    # The current code has NO escalation beyond the repeated bounded resubscribe.
    has_escalation_hook = any(
        hasattr(orch, attr)
        for attr in ("_escalate_wedged_feed", "_feed_heal_escalation", "_force_reconnect_on_wedge")
    )
    threshold_min = _WATCHDOG_STALE_THRESHOLD_SEC // 60
    cooldown_min = _WATCHDOG_FEED_HEAL_COOLDOWN_SEC // 60
    expected_heals = (timeline_min - threshold_min) // cooldown_min + 1

    print("\n=== PHASE 4 wedged-subscription GAP reproduction (OFFLINE, deterministic) ===")
    print(f"  timeline_min            : {timeline_min}")
    print(f"  stale_threshold_min     : {threshold_min}")
    print(f"  heal_cooldown_min       : {cooldown_min}")
    print(f"  watchdog_detections     : {detections}  (every cycle past threshold)")
    print(f"  self_heal_resubscribes  : {heals}  (≈expected {expected_heals}, cooldown-limited)")
    print(f"  feed_still_wedged       : {feed_still_wedged}  (resubscribe never restored bars)")
    print(f"  escalation_hook_present : {has_escalation_hook}  (THE GAP: no escalation exists)")
    print("  ---")
    print("  GAP PINNED: feed wedged for the full window; watchdog detects + self-heal")
    print("  resubscribes on cooldown forever, but the feed never recovers and nothing")
    print("  escalates (force-reconnect / halt). The escalation fix is now verifiable:")
    print("  after N failed self-heals the orchestrator must escalate — add the hook +")
    print("  flip escalation_hook_present True and assert escalation fires here.")

    # Deterministic gap is reproduced when: stale detected, heals fired but bounded,
    # feed never recovered, and no escalation hook exists.
    gap_reproduced = detections > 0 and heals >= 1 and feed_still_wedged and not has_escalation_hook
    print(f"\n  GAP_REPRODUCED          : {gap_reproduced}")
    return 0 if gap_reproduced else 1


# ----------------------------------------------------------------------------- main


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Phase 4 fault injection (AUDIT-gated).")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument(
        "--i-confirm-prod-stopped-and-flat",
        action="store_true",
        help="affirm prod is STOPPED + FLAT so the shared paper account is isolated (socket-drop)",
    )
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument(
        "--throwaway-gateway-port",
        type=int,
        default=None,
        help="port of a SEPARATE throwaway gateway (gateway-restart only; must differ from 4002)",
    )
    ap.add_argument("--client-id", type=int, default=121)
    ap.add_argument("--instrument", default="MNQM6")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--stop-pts", type=float, default=75.0)
    ap.add_argument("--fault", choices=FAULTS, default="socket-drop")
    args = ap.parse_args()

    print(MATRIX)
    print(f"PLAN: fault={args.fault} clientId={args.client_id} instrument={args.instrument}")

    if not args.execute:
        print(
            "\nDRY RUN — pass --execute (plus the per-fault confirmation) to run, "
            "only after Ohad's go."
        )
        return

    if args.qty > 2:
        raise SystemExit("refusing: --qty must be <= 2 for the fault proof.")

    if args.fault == "wedged-subscription":
        # OFFLINE — no gateway, no position, no account. Safe to run anytime.
        raise SystemExit(asyncio.run(_reproduce_wedged_subscription()))

    if args.fault == "socket-drop":
        if not args.i_confirm_prod_stopped_and_flat:
            raise SystemExit(
                "refusing: socket-drop shares the paper account with prod. Pass "
                "--i-confirm-prod-stopped-and-flat only when prod is STOPPED + FLAT."
            )
        raise SystemExit(asyncio.run(_run_socket_drop(args)))

    if args.fault == "gateway-restart":
        if args.throwaway_gateway_port is None or args.throwaway_gateway_port == PROD_GATEWAY_PORT:
            print(
                "\nDEFERRED — gateway-restart needs a genuinely separate throwaway gateway "
                f"(port ≠ prod {PROD_GATEWAY_PORT}). A 2nd IB Gateway on the same paper login "
                "boots the first session, and restarting the prod gateway risks the window-close "
                "health check. The socket-drop fault already proves GTC-stop survival + boot "
                "re-adoption (§0.5.211) at broker truth. Provide --throwaway-gateway-port to run."
            )
            raise SystemExit(0)
        raise SystemExit(
            "gateway-restart against a real throwaway gateway is enabled under operator "
            "supervision; wire the container-restart body for the throwaway instance here."
        )


if __name__ == "__main__":
    main()
