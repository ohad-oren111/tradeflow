# TradeFlow ROADMAP — living backlog

Single ordered backlog. Lanes per the `change-management` skill (STABILIZE >
REPLICATE > MEASURE > OPTIMIZE). Never work a lower lane while a higher lane has an
open defect. Keep this current at end of session; it is a backlog, not a history (the
handoffs are the history).

_Last updated: 2026-06-03._

## NOW (in flight)

- **[STABILIZE] STABILIZE-5 — standalone protective stop.** Make the trailing
  protective stop a standalone `parentId=0` STP so the gateway can't auto-OCA it
  (Error 10326 → silent cancel). Built + tested; PR OPEN, awaiting operator `merge`
  (AUDIT). Deploy while FLAT. — *this PR.*

## NEXT (highest open lanes)

- **[STABILIZE/MEASURE] Live money gate.** Watch the first post-STABILIZE-5 trade
  end-to-end: entry places an ungrouped standalone STP; first ratchet's broker
  `auxPrice` equals the announced level; exit fills near the ratcheted level; never
  naked; stop cancelled on close (never orphan). This proves the fix in the wild.
- **[MEASURE] Live vs backtest/SeanBot.** After a few clean trades, compare TF live
  P&L to the touch-rule backtest (PF ~1.05) and to SeanBot on the dashboard. Small
  increments only after this.

## LATER (gated)

- **[REPLICATE] Entry alignment with SeanBot — PARKED.** TF agrees ~46% of his
  entries; his live rule is wider/newer than his shared code. BLOCKED on the operator
  obtaining his friend's current entry rule. Do NOT widen the gate blind.
- **[OPTIMIZE] Edge increments — SHELVED.** BT-1..BT-5 did not beat the proven rule
  (no OOS PF ≥ 1.20). Stays shelved until STABILIZE clean + REPLICATE faithful +
  MEASURE trustworthy. Then one small step at a time.

## DEBT (non-blocking)

- `_confirm_stop_at_aux` re-reads the same Order identity; reliably catches the
  observed 10326 (order-gone) case, but a non-cancelling rejection that doesn't revert
  aux could pass on stale local state. A permId round-trip would be fully
  broker-authoritative.
- NEVER-ORPHAN residual: if the router's `_cancel_sibling_legs` fails to cancel the
  STP AND the lifecycle still transitions CLOSED, the reconciler won't revisit a CLOSED
  row → a rare orphan. Mitigation: the reconciler-driven close path (`_cancel_open_legs`)
  covers the missed-event case; a full broker-order orphan sweep is out of scope.
- SeanBot daily-summary ingestion if he ever posts one (dashboard still uses anchors).
- Runtime "phantom $0.00 reconcile" close flag (non-distorting).
- Untracked `docs/tf_research*.zip` clutter in the working tree.
- §0.5.T4 doc drift: it claims the kill switch exits code 42 with systemd
  `RestartPreventExitStatus=42`. The actual kill switch raises an in-process halt +
  flattens (no process exit), and the deployment is Docker compose `restart:
  unless-stopped`, not systemd. See `docs/runbooks/kill_switch_restart.md`. Reconcile
  the doc in a future docs pass.
