# TradeFlow — Focus Brief, Session 15

*Paste this at the top of the new chat, alongside `HANDOFF_v12.md`. This is the orientation; the handoff is the detail; the broker/DB are the truth (§0.5.98).*

---

## Focus of this session
1. **Clear the orphan broker order(s) and fix bracket cleanup** — paste-ready work order **W-S14.3** is in hand. This is first because an orphan resting stop can flip the long-only bot SHORT.
2. **Read the bullish calibration forward** — it shipped last session but hasn't traded yet; start accumulating reconciliation-scorecard evidence over real sessions.
3. **Make the SeanBot scorecard durable** — the daily report shows "journal not found" because the journal is ephemeral; the queued Supabase reconciliation table fixes it.

## Where we are (end of Session 13–14, 2026-05-29)
W-S14.2 shipped end-to-end: PRs #55–#60 merged + deployed (398 tests green). Highlights: the **bullish gate, not touch**, was the SeanBot opportunity gap (backtest: 8/12 vs 2/12) → `ma_bullish_tolerance_pts=2.0` live; farm-flap resubscribe now fires on ANY of {2103,2105,10182}; commission matches the broker; hourly Telegram digest + richer daily report; docker/journal log rotation; listener healthcheck fixed. The autonomy/permission friction was fixed at the source (settings allow/deny + memory).

## Bot status (RE-VERIFY from broker before acting — §6 of the handoff)
- **Live, FLAT** as of ~17:51 UTC, **re-warming SMA** after the 16:51 UTC redeploy (warmup completes ~18:32 UTC; expect this done by next session).
- Two lifecycles today, both CLOSED: **#1 +$599.52 (TARGET), #2 −$367.98 (STOP, ~15pt slippage)**. Day net ≈ **+$233**.
- ⚠️ **Orphan resting order:** `SELL STP @30183 GTC ×2` (from CLOSED lifecycle #1) — and likely a second orphan target from #2's stop-out. **Probe + clear first.**
- Containers healthy; `origin/main` HEAD ≥ PR #60 (confirm with `git rev-parse origin/main`).

## Priorities (ranked — but order by live state, not this list)
1. **W-S14.3** orphan sweep + bracket-cleanup fix (AUDIT gates: the cancel + any execution-path fix).
2. Forward-measure the calibration (scorecard over multiple days; don't overfit — last session was a reversal day where SeanBot also lost).
3. Durable Supabase `signal_reconciliations` / `strategy_decisions` tables (+ optional persistent volume for journals) → scorecard survives restarts, unblocks the daily comparison digest.
4. Kill-switch PR (real-money-readiness gate).
5. Botty AI deferred re-eval (30 days post-TradeFlow-live).

## What the operator (Ohad) is doing
PM / orchestrator — **maximally hands-off**. Reads structured reports, types single-word approvals at the gates only. Does NOT implement, run smoke tests, or babysit. Pushes docs to the server and lets VPS CC publish to the repo. Wants minimal browser/UI involvement.

## How to work this session (the standing rules — §0.5.184/.185)
- **Two tiers:** chat = strategy / work orders / handoffs; **VPS CC = end-to-end execution** (implement → test → ship PR → self-merge REPORT/AUTO after CI green → deploy → verify from broker/DB → report). **Never probe-and-wait** unless probing is the explicit GOAL.
- **Gate (single-word approval) ONLY for:** AUDIT diffs (order execution / strategy / kill-switch / secrets / broker-state-altering), the one strategy-parameter decision, and genuine external blockers. Everything else flows.
- Write work orders in **RESOLVE mode** (GOAL + decision tree + full mission), and **batch** any merge gates so the operator approves in as few round-trips as possible.
- VPS CC bash discipline: `/tmp/scriptN.py` via Write, `-F /tmp/commitmsg.txt`, `--body-file`; no `cd &&`/`;`/`$(...)`/`${VAR}`/heredoc. Branch off `origin/main`. Every recreate resets SMA warmup (~99 min).

## First 15 minutes
1. Read handoff §0.5, 1 (incl. chat-side notes), 4, 5.
2. Run handoff §6 V0–V2; **confirm orphan-order status first**.
3. If orphans resting → paste **W-S14.3** to VPS CC.
4. Confirm warmup completed; capture the post-calibration decision distribution (fewer `noop_filter:bullish` expected).
5. Skills available: `code-pr-brief`, `session-handoff-writer`, `vps-smoke-test-runbook`, `prod-debug-discipline` (`/mnt/skills/user/`).
