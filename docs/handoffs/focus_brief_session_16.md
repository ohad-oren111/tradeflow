# TradeFlow — Focus Brief, Session 16

*Paste this at the top of the new chat, alongside `HANDOFF_v14.md`. This is the orientation; the handoff is the detail; the broker/DB are the truth (§0.5.98).*

---

## Focus of this session
1. **Observe the Sunday reopen cleanly** — the bot has shipped three fix/feature batches but hasn't traded a real target/stop fill since they landed. The first live fill (Sunday ~18:00 ET / 22:00 UTC) is the real test of the W-S15.1 bracket sibling-cancel. Watch the **orphan canary** in the daily report: a real fill should leave **0 resting orders**.
2. **Ship `strategy_decisions` + the daily SeanBot comparison digest (PR-D3c)** — now unblocked (durable scorecard live) and meaningful (P&L is fill-accurate). This is the #1 build item. RESOLVE mode, mirrors W-S15.2's shape, one operator-paste migration gate.
3. **Forward-measure the bullish calibration** — accumulate scorecard reads vs SeanBot over real sessions; don't overfit (last live trade was a reversal-day scratch).

## Where we are (end of Session 15, 2026-05-29 ~23:20 UTC)
Three batches shipped **and verified** this session, all deployed at **`8551cb3`**:
- **W-S15.1 (#62)** — bracket **sibling-cancel on exit** (router + reconciler). **Verified in prod** (the `7775a248` manual flatten cancelled both legs → 0 resting orders).
- **W-S15.2 (#63 settings, #64 reconciliation)** — durable `signal_reconciliations` Supabase table; the SeanBot scorecard now survives recreates ("journal not found" is gone).
- **W-S15.3 (#66 visibility, #65 P&L+integration)** — READINESS block in the reports + baked commit stamp + broker **orphan canary**; reconciler now records the **actual broker fill price** at exit (fixes the slipped-stop P&L understatement); real-wiring lifecycle integration test (TARGET/STOP/missed-event). **438 tests green.**

## Bot status (RE-VERIFY from broker before acting — §6 of the handoff)
- **LIVE, FLAT, 0 resting orders** (broker: `positions=0 openTrades=0 portfolio=0`). No orphans.
- **Deployed = `8551cb3`** (commit stamp confirmed in-container). All containers healthy, RestartCount=0.
- **Market CLOSED until Sunday ~18:00 ET (22:00 UTC).** Warmup re-seeds from live bars on reopen (~99 min) — no trades until then; the deploy-time warmup reset is free.
- **Sunday-readiness verdict: READY-WITH-CAVEATS.** Caveats (neither blocks reopen): (1) a cosmetic gateway-restart vs alert-suppression window mismatch → one possible spurious bar-staleness ALERT ~23:35 ET nightly (no halt); (2) the calibration is still forward-unproven.
- Day's P&L: DB ≈ **+$290.80** (3 closed) vs broker-true ≈ **+$229.06** — the DB carries a stale pre-#57 commission on #1 and the un-restated slipped stop on #2 (`9b6f2df8` −307.48 DB / −367.98 broker). **Forward closes are fill-accurate; historical rows are not restated — use broker executions for true historical P&L.**

## Priorities (ranked — but order by live state, not this list)
1. **Sunday reopen watch** (V-reopen in the handoff): bars resume, warmup → ready, no farm-flap/reconnect storm, first real TARGET/STOP fill leaves **0 resting orders**.
2. **`strategy_decisions` table + daily comparison digest (PR-D3c).**
3. Forward-measure the calibration (scorecard reads vs SeanBot; don't overfit).
4. Restart-band alert-window alignment (small; kills the spurious nightly alert).
5. OCA-at-placement (belt-and-suspenders for the app-down exit window).
6. Kill-switch PR (real-money-readiness gate).
7. Botty AI deferred re-eval (30 days post-TradeFlow-live).

## What the operator (Ohad) is doing — PRIME DIRECTIVE
PM / orchestrator — **maximally hands-off.** Reads structured reports, types single-word approvals **at the AUDIT gates only**. Does NOT implement, run smoke tests, or babysit. Pushes docs to the server (scp); VPS CC publishes to the repo. Wants minimal browser/UI.

**This is the #1 operating directive: keep Ohad hands-off and give VPS CC end-to-end (RESOLVE) tasks.** Every work order is implement → test → ship → self-merge REPORT/AUTO after CI green → deploy → verify from ground truth → report. **Never probe-and-wait.** Batch any gates. VPS CC owns each task to a verified, reported finish.

## How to work this session (the standing rules — handoff §0.5)
- **Two tiers:** chat = strategy / work orders / handoffs; **VPS CC = end-to-end execution** (§0.5.185).
- **Gate (single word) ONLY for:** AUDIT diffs (order execution / strategy / kill-switch / secrets / broker-state-altering), the one strategy-parameter decision, genuine external blockers (e.g. a Supabase DDL paste). Everything else flows.
- **§0.5.186 — probe discipline:** direct named reads only; **no sub-agents, no repo-wide sweeps, no heredocs.** Run VPS CC inside `tmux`. If a command hangs, interrupt it — don't spam `echo`.
- **§0.5.187 — command-style (the permission-prompt fix):** **no `cd` in Bash** (absolute paths), **prefer the Grep/Glob/Read tools** over shelled `grep`/`sed`/`cat`/`find`, **no `VAR=value` prefixes** on Bash (set vars in compose `.env` or accept one approval), batch via parallel tool calls. Allowlist edits never fix a running session — settings load at session start. *This, not the allowlist, is why the prompts kept firing.*
- **§0.5.188 — weekend/unattended-window safety:** never auto-ship a NEW trading/execution/reconnect-path bug into an unattended window — surface it with a recommendation. Observability/test fixes flow.
- **VPS CC bash discipline:** `/tmp/scriptN.py` via Write, `-F /tmp/commitmsg.txt`, `--body-file`; no `&&`/`;`/`$(...)`/`${VAR}`/heredoc. Branch off `origin/main`. Every recreate resets SMA warmup (~99 min).
- **Publish flow:** owner `scp`s docs → VPS CC commits + PRs + self-merges (handoff §16).

## First 15 minutes
1. Read handoff §0.5, 1, 4, 5 — and this brief.
2. Run handoff §6 V0–V3; confirm **FLAT + 0 resting orders**, deployed == `8551cb3`, readiness block + scorecard render.
3. If after Sunday reopen: run **V-reopen** and capture the first real fill's orphan-canary result + first scorecard reads vs SeanBot.
4. Draft + ship **#1 (`strategy_decisions` + comparison digest)** as the first work order (RESOLVE; one `done` gate for the migration).
5. Skills available in `/mnt/skills/user/`: `code-pr-brief`, `session-handoff-writer`, `vps-smoke-test-runbook`, `prod-debug-discipline`.

## How to publish these docs to the repo
Owner `scp`s `HANDOFF_v14.md` + `focus_brief_session_16.md` to `tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/`, then hands VPS CC the paste-ready publish brief in **handoff §16** (it PRs + self-merges both to `origin/main`, §0.5.187-clean — no `cd`/`VAR=`).
