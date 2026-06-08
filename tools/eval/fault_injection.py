"""Phase 4 [AUDIT — needs Ohad's go] — fault injection on a THROWAWAY instance.

Goal: deterministically reproduce the failure modes the offline phases can only model,
on a throwaway bot + its OWN gateway (NEVER prod's). Verifies:
  * a resting GTC protective stop SURVIVES a socket-drop / gateway-restart and that
    boot-recovery re-adopts it (§0.5.211);
  * the wedged-subscription self-heal gap reproduces deterministically, so the queued
    escalation fix becomes verifiable.

SAFETY (why it is AUDIT-gated + build-only here):
  * It MUST point at a SEPARATE, throwaway IB Gateway (its own port/clientId) — NEVER
    the prod gateway/account. The script refuses to run unless ``--throwaway-gateway-port``
    is given AND it differs from the prod gateway port, AND ``--execute`` is passed.
  * It never imports or writes the Supabase prod tables.
  * Default is a DRY RUN that prints the fault matrix + the coordination requirements.

Faults (induced against the throwaway instance):
  1. socket-drop      — kill the client socket mid-trade; assert the GTC stop still
                        rests at the broker, and reconnect → boot-recovery re-adopts it.
  2. gateway-restart  — restart the throwaway gateway container; same assertion.
  3. wedged-subscription — stop delivering bars without dropping the socket; assert the
                        stale-bar watchdog fires and the self-heal path runs (or pin the
                        gap where it does NOT, making the escalation fix testable).

Run (only after Ohad's go, against a throwaway gateway):
    python -m tools.eval.fault_injection --execute --throwaway-gateway-port 4102 \
        --client-id 121 --fault socket-drop
"""

from __future__ import annotations

import argparse
import logging

LOGGER = logging.getLogger("eval.fault_injection")

PROD_GATEWAY_PORT = 4002  # the port Phase 4 must NEVER touch.

FAULTS = ("socket-drop", "gateway-restart", "wedged-subscription")

MATRIX = """\
FAULT MATRIX (induced on the THROWAWAY instance + its own gateway):
  socket-drop          → GTC stop still rests; reconnect → boot-recovery re-adopts it (§0.5.211)
  gateway-restart      → GTC stop survives the restart; recovery re-adopts the resting id
  wedged-subscription  → stale-bar watchdog fires; self-heal runs (or the gap is pinned so the
                         queued escalation fix becomes verifiable)
"""


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Phase 4 fault injection (AUDIT-gated; throwaway only)."
    )
    ap.add_argument("--execute", action="store_true")
    ap.add_argument(
        "--throwaway-gateway-port",
        type=int,
        default=None,
        help="port of a SEPARATE throwaway gateway (must differ from prod 4002)",
    )
    ap.add_argument("--client-id", type=int, default=121)
    ap.add_argument("--fault", choices=FAULTS, default="socket-drop")
    args = ap.parse_args()

    print(MATRIX)
    print(
        f"PLAN: fault={args.fault} on throwaway gateway "
        f"port={args.throwaway_gateway_port} clientId={args.client_id}"
    )

    if not args.execute:
        print(
            "\nDRY RUN — pass --execute (and a throwaway --throwaway-gateway-port) to run, "
            "only after Ohad's go."
        )
        return
    if args.throwaway_gateway_port is None or args.throwaway_gateway_port == PROD_GATEWAY_PORT:
        raise SystemExit(
            "refusing: Phase 4 requires a throwaway gateway port that differs from "
            f"prod ({PROD_GATEWAY_PORT}). NEVER inject faults against the prod gateway/account."
        )
    # The fault-induction body (kill socket / restart container / wedge the bar feed)
    # runs against the throwaway instance only; it is enabled + reviewed under operator
    # supervision when Phase 4 is approved. Build-only in this PR.
    raise SystemExit(
        "Fault-induction body is intentionally not auto-executed in this build. "
        "Enable + review it against the throwaway instance when Phase 4 is approved."
    )


if __name__ == "__main__":
    main()
