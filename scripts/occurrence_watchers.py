"""Q-E — on-occurrence auto-verification watchers (log-assertion detectors).

Three data-blocked proofs cannot be forced; they confirm themselves when the next
real occurrence lands. Rather than busy-poll, each is a PURE detector over the
container log text that returns CONFIRMED + a one-line verdict once the occurrence's
signature appears. No hot-path change: this is a standalone read-only checker, meant
to be run on the log stream on-occurrence (operator, or wired into the host watchdog):

    docker logs tradeflow-app --since 24h 2>&1 | python3 scripts/occurrence_watchers.py -

Watchers:
  (1) quarterly_roll       — the next real front-month roll (~MNQU6 expiry 2026-09-18)
                             restarts FLAT, RESTORES the pnl_epoch, and re-seeds warmup.
  (2) feed_wedge_escalation— a real multi-day feed wedge escalates end-to-end: episode
                             open -> app self-heals exhausted -> gateway-restart handoff
                             -> watchdog expiry-suspected terminal hint.
  (3) broker_pnl_semantics — the next live round-trip backfills realized_pnl_broker and
                             the reconciler auto-logs the realizedPNL-vs-estimate verdict
                             ([WATCH] broker_pnl_semantics ...) — discharges the #179
                             live-verification automatically on the first fill.

Each returns ("PENDING" | "CONFIRMED" | "PARTIAL", detail). Pure string scanning — no
network, no broker. Signatures are matched against the REAL emit sites (verified
2026-06-23): front_month.py:187, orchestrator.py:891/960, kill_switch epoch line,
warmup-enable seed line, tradeflow_watchdog.py:600/604, reconciler [WATCH] line.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class WatchResult:
    name: str
    status: str  # PENDING | PARTIAL | CONFIRMED
    detail: str


def check_quarterly_roll(log: str) -> WatchResult:
    rolled = "[ROLL] rolling " in log  # front_month.py:187 (a REAL roll, not "no roll")
    restored = "epoch: restored" in log  # kill_switch restored epoch after the restart
    reseeded = "WARMUP-ENABLE" in log and "seeded from backfill" in log
    if rolled and restored and reseeded:
        return WatchResult(
            "quarterly_roll",
            "CONFIRMED",
            "roll fired + pnl_epoch RESTORED + warmup re-seeded after the graceful restart",
        )
    if rolled:
        return WatchResult(
            "quarterly_roll",
            "PARTIAL",
            f"roll fired but missing: {'restored-epoch ' if not restored else ''}"
            f"{'warmup-reseed' if not reseeded else ''}".strip(),
        )
    return WatchResult("quarterly_roll", "PENDING", "no real roll yet (expect ~2026-09-18)")


def check_feed_wedge_escalation(log: str) -> WatchResult:
    opened = "feed_episode_open" in log
    exhausted = "feed_episode_gateway_restart_needed" in log
    expiry_hint = "expiry_suspected" in log or "contract expiry suspected" in log
    recovered = "feed_episode_recovered" in log
    if opened and exhausted and (expiry_hint or recovered):
        tail = "expiry-suspected terminal hint" if expiry_hint else "recovered"
        return WatchResult(
            "feed_wedge_escalation",
            "CONFIRMED",
            f"episode open -> self-heals exhausted -> gateway-restart handoff -> {tail}",
        )
    if opened or exhausted:
        return WatchResult(
            "feed_wedge_escalation",
            "PARTIAL",
            f"open={opened} gateway_restart={exhausted} expiry_hint={expiry_hint} "
            f"recovered={recovered}",
        )
    return WatchResult("feed_wedge_escalation", "PENDING", "no multi-day wedge episode yet")


def check_broker_pnl_semantics(log: str) -> WatchResult:
    if "[WATCH] broker_pnl_semantics" not in log:
        return WatchResult(
            "broker_pnl_semantics",
            "PENDING",
            "no live round-trip has backfilled realized_pnl_broker yet (needs the "
            "migration applied AND a real fill)",
        )
    # surface the last verdict line
    last = ""
    for line in log.splitlines():
        if "[WATCH] broker_pnl_semantics" in line:
            last = line.strip()
    return WatchResult("broker_pnl_semantics", "CONFIRMED", last[-180:])


WATCHERS = (check_quarterly_roll, check_feed_wedge_escalation, check_broker_pnl_semantics)


def run_all(log: str) -> list[WatchResult]:
    return [w(log) for w in WATCHERS]


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        with open(sys.argv[1]) as fh:
            log = fh.read()
    else:
        log = sys.stdin.read()
    results = run_all(log)
    print("=== Q-E on-occurrence watchers ===")
    for r in results:
        print(f"[{r.status:<9}] {r.name:<22} {r.detail}")
    # exit 0 always — this is a status reporter, not a gate
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
