# TradeFlow — Handoff v14 (W-S15.1/15.2/15.3 all shipped + verified; bot READY for Sunday reopen; P&L now fill-accurate; readiness visibility live)

*Handoff from end of 2026-05-29 ~23:20 UTC. The bot is **LIVE and FLAT**, 0 resting orders, deployed at **`8551cb3`**. CME is **closed until Sunday ~18:00 ET (22:00 UTC)** — no trades until then; the SMA re-warm from tonight's deploys is free. **Sunday-readiness verdict: READY-WITH-CAVEATS** (both caveats non-blocking — see §1/§7). This doc supersedes the (unpublished) v13 draft and captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc
Read §0.5, 1–6 first. §7–15 are reference. §14 ranks authority when this doc disagrees with a live probe — **the broker and the live DB always win (§0.5.98).**

**Do not trust this doc alone.** Run the §6 verification block before any code. No critical-first-action (flat, no orphans, no open PRs) — but **re-verify FLAT + 0 resting orders** anyway.

---

## 0.5 Standing rules (permanent — do not remove)

> **OPERATOR'S PRIME DIRECTIVE (Ohad, restated this session):** *maximize hands-off.* Every VPS CC work order is **end-to-end (RESOLVE)** — implement → test → ship → self-merge REPORT/AUTO after CI green → deploy → verify from ground truth → report. **Never probe-and-wait.** Ohad is PM/orchestrator and types single-word approvals at the §0.5.184 gates only. A work order that only gathers evidence is incomplete unless evidence-gathering was the explicit GOAL. Batch gates so approvals come in as few round-trips as possible.

- **Copy-paste instruction style.** Every owner action is a self-contained copy-paste bash block, env sourced inline, expected output below it, decision tree if it branches.
- **Learning-delivery discipline.** Surface each new fact immediately as a paste-ready snippet; don't wait for end-of-session.
- **Read before diagnosing.** Read full startup log + 3–5 full cycle narratives before a root cause. `grep | wc -l` summaries are the #1 cause of wrong diagnoses.
- **Verify severity against source of truth.** Hit the live broker/DB/raw log before urgency language.
- **Always draft a VPS smoke-test runbook after a PR merge** unless told otherwise. The owner does not run smoke tests by hand.
- **§0.5.97 — probe external specs** (broker contracts, fees, schema, library APIs) against the source before baking into briefs.
- **§0.5.98 — broker is ground truth** for fills, P&L, positions, and resting orders — NOT internal DB tables.
- **§0.5.168 — branch off `origin/main`, never local `main`** (`git -C <repo> checkout -B <branch> origin/main`).
- **§0.5.179 — deploy via `up -d --force-recreate`, not `restart`.** Every recreate resets SMA warmup (~99 min, SMA100 on 1-min bars).
- **§0.5.181 — farm flap** = some combination of 2103/2105/10182, socket stays up, bar sub dies.
- **§0.5.184 — autonomy contract.** Self-merge REPORT/AUTO PRs after CI green without asking. Pause ONLY for: AUDIT diffs (order execution / strategy / kill-switch / secrets / broker-state-altering), the one strategy-parameter decision, genuine external blockers. Default when uncertain: REPORT.
- **§0.5.185 — RESOLVE / end-to-end default** (see Prime Directive above — this is the load-bearing one).
- **§0.5.186 — probe discipline.** A probe is a handful of **direct, named** `view`/Read/Grep reads — **no spawned sub-agents, no repo-wide sweeps, no heredocs** (`<< 'EOF'` hangs on stdin over SSH). If a command hangs, interrupt it; don't spam `echo` liveness checks. Run VPS CC inside `tmux`/`screen` so an SSH drop detaches rather than kills. *(Origin: the first W-S15.2 attempt spawned a 150+ tool-call sub-agent + heredocs and the SSH pipe dropped.)*
- **§0.5.187 — command-style discipline (the permission-prompt root cause).** The recurring "Do you want to proceed?" prompts were **never an allowlist gap** — the commands were already allowed. Two command styles **bypass allowlist matching and force manual approval**:
  1. **No `cd` in Bash, ever** — `cd /repo; grep …` trips a harness "path-resolution-bypass" gate. Use absolute paths; the repo is already the working dir.
  2. **Prefer the Grep / Glob / Read tools** over shelled `grep`/`sed`/`cat`/`find` — they never trip that gate (and the harness prefers them).
  3. **No `VAR=value` env prefix on Bash** — `GIT_COMMIT=… docker compose build` makes the matcher read the first token as `GIT_COMMIT=…`, so `Bash(docker compose:*)` never matches. Put the var in compose `.env`, or accept the single approval.
  4. Batch with **parallel tool calls**, not `;`/`&&` mega-commands.
  **Settings load once at session start**, so editing the allowlist never fixes the running session — the durable fix is command style, not config.
- **§0.5.188 — weekend / unattended-window safety override.** When shipping into an unattended window (weekend, overnight), do NOT auto-fix-and-deploy a NEW bug found in the **trading/execution/reconnect/warmup path** — **surface it with a recommendation and stop.** A correct fix we can't watch for days is riskier than a documented known issue. Observability/logging/test-only fixes still flow. *(This worked this session: the restart-band alert mismatch was surfaced, not shipped.)*
- **VPS CC bash discipline** — `/tmp/scriptN.py` via Write; commit `-F /tmp/commitmsg.txt`; PR body `--body-file`. No chained `&&`/`;`/`$(...)`/heredoc/`${VAR}`.

---

## 1. Where we are (as of handoff, 2026-05-29 ~23:20 UTC)

### Live production state
- **Containers:** `tradeflow-app` healthy (recreated 23:14 UTC for the W-S15.3 deploy), `tradeflow-ib-gateway` healthy, `tradeflow-telegram-listener` running. RestartCount=0 all.
- **Deployed code:** `8551cb3`, built with `GIT_COMMIT` baked. In-container markers confirmed: W-S15.1 (`_cancel_sibling_legs`/`_cancel_open_legs`), W-S15.2 (`_persist_reconciliation`), W-S15.3 (`_resolve_exit_price`, `get_fills`, `_readiness_fragment`), and `TRADEFLOW_COMMIT=8551cb3…`.
- **Position:** **FLAT.** Broker ground truth: `positions=0 openTrades=0 portfolio=0` (`scripts/_probe_ibkr.py`, clientId 97). **No orphans.**
- **Warmup:** re-seeding from the 23:14 recreate; can't complete until Sunday reopen (needs live bars) — expected, not a problem.
- **Day's lifecycles (all CLOSED):** `c44c5f95` (#1, TARGET), `9b6f2df8` (#2, STOP), `7775a248` (#3, first post-calibration trade, MANUAL flatten). **DB day P&L ≈ +$290.80; broker-true ≈ +$229.06** (DB carries a stale pre-#57 commission on #1 and the un-restated slipped stop on #2 — see §4).

### What just shipped (this session, all merged + deployed)
- **PR #62 (`6f47256`, W-S15.1, AUDIT)** — bracket **sibling-cancel on exit**, in-process via the app's own IB client, in **both** exit paths: `router._handle_exit_fill` and `reconciler._close_from_exiting`. **Verified in prod** (the `7775a248` manual flatten cancelled both children → 0 resting orders).
- **PR #63 (`085c469`, AUTO)** — committed `.claude/settings.json`. Operational debt cleared.
- **PR #64 (`94245ad`, REPORT)** — durable `signal_reconciliations` Supabase table + writer + daily-report reader (service-role). Scorecard survives recreates.
- **PR #66 (`bc8d841`, W-S15.3 Track E, REPORT)** — READINESS block in daily report + hourly digest; baked `TRADEFLOW_COMMIT` (Dockerfile ARG + compose build-arg); broker resting-order **orphan canary**.
- **PR #65 (`8551cb3`, W-S15.3 Track D+B, AUDIT)** — reconciler now records the **actual broker fill price** at exit (`_resolve_exit_price` → `IBClient.get_fills()`, qty-weighted; falls back to order price only when no fill visible) + a real-wiring lifecycle integration test (TARGET / STOP / missed-event). 438 tests green.

### What we discovered this session (evidence in §4/§5)
- **W-S15.1 works in prod** — `7775a248` (LONG 2ct @30371.0, 18:39 UTC) was closed by an operator **manual flatten** at 20:29 UTC (`exit_reason=MANUAL`, `exit_order_id=25` = fresh MKT; children 19/20 cancelled). Broker then flat, 0 resting orders → the MANUAL branch cancelled both legs. *(TARGET/STOP-fill branches are unit- + integration-tested but not yet exercised on a real live fill — Sunday's first fill is the true test; the orphan canary is the net.)*
- **The P&L bug was in the reconciler, not the router.** Router exit path (`_handle_exit_fill`→`_extract_fill`) already used `execution.avgPrice` (correct). Reconciler `_close_from_exiting`→`_exit_price_for` used the STOP/TARGET **order** price, ignoring slippage → `9b6f2df8` recorded −307.48 (exit 30337.5) vs broker fill 30322.38 / true −367.98. Now fixed forward (§4).
- **Reopen-resilience reasoned clean** (§6 V-reopen): warmup self-recovers from live bars (NaN MA → `noop_warmup`, recomputed from a maxlen-150 deque, no nan-stuck state); farm-flap any-code + DNS-aware reconnect armed at boot; boot empty-case clean (`recovery_complete transitions_applied=0`); gateway auto-restart 23:30 ET vs Sunday reopen 18:00 ET → no collision.
- **Minor (queued, NOT fixed — §0.5.188):** gateway auto-restart 23:30 ET vs watchdog restart-band suppression 23:45–00:15 ET — a ~15-min gap that could throw one spurious bar-staleness ALERT ~23:35 ET nightly. ALERT-only, no auto-halt, not reopen-related.

### Chat-side orchestrator notes (my judgment calls this session — carry forward)
- **"Ready" ≠ "perfect."** The sibling-cancel on a real TARGET/STOP fill is test-verified + the cancel-against-IB plumbing is prod-proven (the manual flatten), but it has NOT run on a live target/stop fill (no market). That residual is irreducible without trading; the readiness-block **orphan canary** is the instrumented safety net for Sunday's first real fill.
- **Weekend-safety override (§0.5.188)** drove surfacing the restart-band mismatch instead of shipping it — correct for an unattended window.
- **Calibration divergence:** `7775a248` was the first post-calibration trade; reconciliations showed TF and SeanBot **diverging** (TF entered where SeanBot didn't; SeanBot's entries were TF-blocked on touch/bullish even at 2.0 tol). Measure forward via the now-durable scorecard over multiple real sessions; **don't overfit** a reversal-heavy Friday.
- **Deploy sequencing:** bundle deploys, deploy at a flat moment, and note that a warmup reset is free while the market is closed. Avoid piecemeal recreates.
- **Historical P&L is not restated:** the Track D fix is forward-only; `9b6f2df8` keeps its stored −307.48. Use broker executions for true historical P&L.

---

## 2. The session's work thread
1. **W-S15.1 (PR #62):** confirmed no orphans + the non-OCA root cause (STP `parentId=0`, no shared `ocaGroup`); shipped in-process sibling-cancel in router **and** reconciler; deployed mid-position (re-adoption verified clean); one AUDIT approval.
2. **W-S15.2 attempt #1 LOOPED** (sub-agent + heredocs → SSH drop, nothing shipped) → codified §0.5.186. Hardened re-run shipped settings (#63) + durable reconciliation table (#64); operator pasted the DDL; scorecard verified reading from Supabase.
3. **W-S15.3:** verified current health + explained `7775a248` (manual flatten, MANUAL branch verified in prod); wrote a real-wiring lifecycle integration test (#65 Track B); reasoned reopen-resilience clean (Track C); fixed the reconciler P&L bug (#65 Track D); added the readiness block (#66 Track E); bundled D+E into one deploy `8551cb3` and verified FLAT.
4. **Permissions root-caused (finally) — §0.5.187:** the prompts were command-style (`cd;` redirect gate + `VAR=` prefix), not an allowlist gap. Behavioral fix persisted.

---

## 3. What the system is made of
**Single source of truth:** none canonical; this handoff + source on `origin/main` is best available.
- Strategy: `src/strategy.py` `Sma100BounceStrategy` (SMA100-bounce, long-only, MA100>MA50, touch band `[sma−15, sma+5]`, `ma_bullish_tolerance_pts=2.0`, gap≥0.5, SL=75/TP=150). Exposes `bar_count`, `last_decision`.
- Execution: `src/execution/router.py` (`_handle_exit_fill`, `_cancel_sibling_legs`, `_extract_fill`) + `reconciler.py` (`_close_from_exiting`, `_cancel_open_legs`, `_resolve_exit_price`, helpers `_filled_order_id_for`/`_fill_price_for_order`/`_exit_price_for`). Both exit paths cancel the surviving leg + record the actual fill.
- Comparison: `src/comparison/seanbot_reconciler.py` — polls `seanbot_signals`, appends ephemeral JSONL (local fallback) **and** upserts durable `signal_reconciliations` (`on_conflict=channel,message_id`).
- Orchestrator: `src/orchestrator.py` — decision journal, hourly digest (`build_hourly_digest` + `_readiness_fragment`), recovery (`_recover_state`/`register_recovered`), watchdogs.
- IB client: `src/clients/ib_client.py` — `get_open_trades`/`get_positions`/`get_portfolio`/**`get_fills`** (in-session cache). Single Supabase client `src/clients/supabase_client.py` (`upsert(table, payload, on_conflict)`), service-role, built in `main.py`.
- Daily health report + cron: `scripts/tradeflow_watchdog.py` (host-side, ~09:00 UTC) — TRADING + new READINESS section.

---

## 4. Verified facts (2026-05-29) — DO NOT challenge unless schema migrates
Carry-forward (still true): `ma_bullish_tolerance_pts=2.0`; `commission_per_side_usd=0.62` / `commission_rt_usd=1.24`; `lifecycles` has `pnl_net`/`entry_price`/`exit_price`/`commission_total` (NO `realized_pnl` etc.); data reads MUST use service-role key; `_FARM_FLAP_CODES={2103,2105,10182}` any-code; MNQ TICK=0.25 / MULT=$2 / quarterly Mar-Jun-Sep-Dec; IBKR paper DUQ331660; `.tradeflow-secrets/.env` shadows compose `${VAR:-default}`; venv pytest = **`/home/tradeflow/tradeflow/.venv/bin/pytest`**; compose service `ib-gateway` ≠ container `tradeflow-ib-gateway`.

W-S15.2: **`signal_reconciliations`** cols `id, signal_ts, channel, message_id, seanbot_type, direction, symbol, price, classification, justification, tf_decision (jsonb), acknowledged_at, created_at`; **unique `(channel, message_id)`**; RLS deny-all anon / service-role bypass. The single `SupabaseClient` is service-role.

**New this session (W-S15.3):**
- **Reconciler exit price is now fill-accurate.** `_resolve_exit_price` looks up the actual fill via `IBClient.get_fills()` (qty-weighted across executions for the filled leg), and falls back to the order price (`_exit_price_for`) only when no matching fill is visible. **`IB.fills()` is an in-session cache** — empty after a reconnect that post-dates the fill → fallback to order price (no regression). The **router** path was already correct. **Never raises.**
- **Historical rows are NOT restated.** `9b6f2df8` keeps `−307.48` (order-price era). Forward reconciler-driven closes are slippage-accurate. **Use broker executions for true historical P&L.**
- **`TRADEFLOW_COMMIT`** is baked into the image (Dockerfile `ARG GIT_COMMIT` + compose `args: GIT_COMMIT: ${GIT_COMMIT:-unknown}`); build with `GIT_COMMIT=<hash> docker compose build tradeflow-app` (note: the `VAR=` prefix will prompt once — §0.5.187 — or set it in compose `.env`).
- **Readiness block fields** (daily report): deployed commit, RestartCount, reconnect/farm-flap counts (recent logs), broker resting-order **orphan canary** (`reqAllOpenOrders`, 0 when flat), warmup/last-bar-age (from the latest hourly digest). Hourly digest emits warmup/last-bar/commit natively.
- **Gateway auto-restart = 23:30 ET** (`AUTO_RESTART_TIME`, TZ America/New_York); watchdog restart-band suppression = **23:45–00:15 ET** — a 15-min gap (queued, not a trading-path bug).

---

## 5. Wrong diagnoses / wrong turns — READ BEFORE YOU DEBUG
1. **Permissions misdiagnosed across multiple sessions** as an allowlist gap → repeated `settings.json` edits that couldn't help (commands were already allowed; settings load only at session start). **Correct cause:** command style — `cd;`-redirect trips a path-bypass security gate and `VAR=` prefixes break matching (§0.5.187). The durable fix is behavioral.
2. **W-S15.2 attempt #1** ran the probe as a sub-agent + heredocs → SSH drop (§0.5.186).

No *technical* misdiagnoses this session — the W-S15.1 root cause, the W-S15.3 P&L bug (correctly localized to the reconciler, not the router), and the reopen-resilience reasoning were all right on the first pass from source-specific reads.

**Lesson:** the remaining failure modes are *execution mechanics over SSH* and *misattributing harness prompts to config*. Probes small and direct; never sub-agent/heredoc; never `cd`/`VAR=`; allowlist edits don't fix a running session.

---

## 6. Verification block — run before doing anything
**V0 — Orphan / resting-order check.** `scripts/_probe_ibkr.py` (clientId 97). **Expect FLAT: `positions=0 openTrades=0`.** Any resting order with no position = ORPHAN → the W-S15.1 fix regressed; investigate before trading.

**V1 — Lifecycles / P&L (service-role).** Query `lifecycles` last 24h. Baseline (DB-stored): `c44c5f95` +600.76, `9b6f2df8` −307.48 (broker-true −367.98, un-restated), `7775a248` −2.48. **Forward closes are fill-accurate; historical stop slippage is not restated — use broker executions for true P&L.**

**V2 — Deployed-code truth.** In `tradeflow-app`: `_cancel_sibling_legs`/`_cancel_open_legs`, `_persist_reconciliation`, `_resolve_exit_price`/`get_fills`, `_readiness_fragment` all present; `TRADEFLOW_COMMIT` env == `git rev-parse origin/main` (≥ `8551cb3`).

**V3 — Reconciliation durability + readiness.** `signal_reconciliations` service-role-readable (200, not 404/`42P01`). Daily-report scorecard renders from it ("N entries…"/"0 entries today", never "journal not found"). Readiness block renders: deployed commit, orphan canary "0 resting / pos=FLAT [OK]", reconnect/flap counts.

**V-reopen (Sunday) — the real test.** After ~18:00 ET reopen: bars resume 1/min; warmup re-seeds to ready (~99 min) staying `noop_warmup` until then; 0 farm-flap storm / 0 watchdog cascade on the reconnect; **first real TARGET/STOP fill leaves 0 resting orders** (watch the orphan canary). Capture first reconciliation-scorecard reads vs SeanBot.

**V4/V5 — feed + telemetry.** `[ALERT] hourly_session_digest` fires hourly with the readiness fragment; daily report TRADING+READINESS show real numbers via service-role.

---

## 7. Pending work queue (priority depends on V0/V-reopen, not this order)

### #1 — `strategy_decisions` table + daily SeanBot comparison digest (PR-D3c)
Now unblocked (durable `signal_reconciliations` live) **and** P&L is fill-accurate, so the digest will report honest numbers. Mirror TF's per-eval decision journal to a durable table + build the daily AGREE/MISS comparison digest. REPORT code + one operator-paste migration (same pattern as W-S15.2 — one `done` gate).

### Restart-band alert alignment (small, queued)
Align the watchdog suppression window with the 23:30 ET gateway restart (or widen it to 23:25–00:15 ET) to kill the spurious nightly bar-staleness alert. Touches the alert/restart band (reconnect-adjacent) → treat as AUDIT-lite; ship when the market context allows watching it.

### Forward-measure the calibration (passive)
Accumulate the durable scorecard over multiple real sessions from Sunday on; don't overfit. TF/SeanBot were diverging on the one post-calibration trade.

### OCA-at-placement (belt-and-suspenders, W-S15.1 Task E)
Place the bracket as a proper OCA group so the broker auto-cancels the sibling even if the app is down at the exit moment. Changes the entry/placement path (AUDIT). Lower priority now that in-process cancel is verified.

### Kill-switch PR
Real-money-readiness gate. Required before any non-paper consideration.

### Uncommitted files / operational debt — NONE
`.claude/settings.json` committed (#63). All branches deleted; working tree clean on `main`.

### Deferred
Botty AI: 30 days post-TradeFlow-live, re-eval BTC/ETH cointegrated stat-arb on Binance perp (Gemini R2 gates: tracking error <5%, zero exec-failure, profit factor >1.30 over 30+ cycles).

---

## 8. Test safety
Carry-forward: don't mock a fictional schema; `side_effect` lists need correct counts + a comment (silent StopIteration); mock at the **wrapper boundary** (`self._db.upsert`, `wd.httpx.get`, IB client boundary) not the raw lib chain; fresh `MagicMock()` per test; `asyncio_mode=auto`; assert via `call_args`/`await_args_list` filtered by identity, not call index. The new integration test (`tests/test_execution_lifecycle_integration.py`) drives the REAL router+reconciler+state-machine with in-memory IB/Supabase fakes — preferred pattern for exit-path coverage. Baseline: **438 green.**

## 9. Pitfalls from prior sessions
- Re-verify orphan/resting-order set and P&L from the broker — don't trust stored `pnl_net` on slipped exits (historical rows un-restated).
- "Table/column X exists" → verify against live PostgREST.
- Local `main` drifts post-squash — `git -C <repo> checkout -B main origin/main`.
- **Probes: no sub-agents/sweeps/heredocs (§0.5.186). Commands: no `cd`/`VAR=`, prefer Grep/Read tools (§0.5.187).**

## 10. Session discipline lesson (2026-05-29)
Technical reasoning was clean all session. The two failure modes were operational: (1) execution mechanics over SSH (sub-agent/heredoc); (2) misattributing harness prompts to the allowlist instead of command style.
**Enforcement next session:** (1) probes = direct named reads, no sub-agent/sweep; (2) no `cd`/`VAR=`, use Grep/Glob/Read tools; (3) don't touch `settings.json` to "fix" prompts — it can't help a running session; (4) P&L claims cite broker executions; (5) hold §0.5.188 on any unattended-window deploy.

## 11. Logging verbosity
`[STRAT] eval … decision= failed=`; transitions old→new; swallowed exceptions log type+context; cancels `[ROUTER]/[RECON] <lc>: cancelled … — <reason>`; reconciler fill-price `[RECON] <sym>: exit_price from broker fill — fill=… order_px=… reason=…`; persists `[RECON-CMP] <sym>: persisted reconciliation <class>`; hourly digest carries the readiness fragment.

## 12. Master template
Use the `code-pr-brief` skill for every PR (patch constraints, test guardrails, known gotchas, "what I got wrong").

## 13. Current PR brief in flight
None drafted. **#1 (`strategy_decisions` + comparison digest)** is the next work order — RESOLVE mode, mirrors the W-S15.2 shape (probe the per-eval decision-journal fields → operator-paste DDL → REPORT writer+reader → deploy → verify), one `done` gate for the migration. Draft it audit-first when resuming.

## 14. Canonical references (authority order)
1. **Broker** (ib_async, IB Gateway 4002) — truth for positions/fills/resting orders/**P&L**.
2. **Supabase** via **service-role** — lifecycles/events/reconciliations (forward `pnl_net` accurate; historical slipped stops un-restated).
3. **Source on `origin/main`** (≥ `8551cb3`) — what runs.
4. **Decision/reconciliation journals** (in-container, ephemeral) — durable copy in `signal_reconciliations`.
5. **This handoff (v14)** — session context, not long-term authority.
6. **v13 (unpublished draft) / v12 and earlier** — historical; ignore where they contradict 1–3.

## 15. First 15 minutes of the next session
1. Read §0.5 (esp. §0.5.185/.186/.187/.188), 1, 4, 5.
2. Run §6 V0–V3; confirm FLAT + 0 resting orders, deployed == `8551cb3`, readiness block + scorecard render.
3. If resuming after Sunday reopen, run **V-reopen** and capture the first real fill's orphan-canary result + first scorecard reads.
4. Draft + ship **#1 (`strategy_decisions` + comparison digest)** as the next work order (RESOLVE; one `done` gate for the migration).
5. Draft a VPS smoke-test runbook after each merge (`vps-smoke-test-runbook` skill).

---

## 16. How to publish this handoff (two-step: owner → server, then VPS CC → repo)

**Step 1 — Owner pushes BOTH docs to the server (scp):**
```bash
scp HANDOFF_v14.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v14.md
scp focus_brief_session_16.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/focus_brief_session_16.md
```
Expect two `100%` transfer lines. Then hand VPS CC the brief below.

**Step 2 — VPS CC commits + PRs to the repo (paste-ready, §0.5.187-clean — no `cd`, no `VAR=`):**
```
You are VPS Claude Code on the TradeFlow VPS. HANDOFF_v14.md and
focus_brief_session_16.md are already on disk in
/home/tradeflow/tradeflow/docs/handoffs/. Publish them to origin/main via a PR
(push origin main is blocked by branch protection). Use ssh-safe single steps —
no cd, no VAR= prefix, no heredoc; commit message via /tmp/handoff_commitmsg.txt,
PR body via --body-file /tmp/handoff_pr.md:

  git -C /home/tradeflow/tradeflow fetch origin
  git -C /home/tradeflow/tradeflow checkout -B claude/handoff-v14 origin/main
  git -C /home/tradeflow/tradeflow add docs/handoffs/HANDOFF_v14.md docs/handoffs/focus_brief_session_16.md
  (Write /tmp/handoff_commitmsg.txt = "docs: add v14 handoff + S16 focus brief (W-S15.1/2/3 shipped + verified; bot READY for Sunday)")
  git -C /home/tradeflow/tradeflow commit -F /tmp/handoff_commitmsg.txt
  git -C /home/tradeflow/tradeflow push -u origin claude/handoff-v14
  gh pr create --repo ohad-oren111/tradeflow --base main --head claude/handoff-v14 --title "docs: add v14 handoff + S16 focus brief" --body-file /tmp/handoff_pr.md
  gh pr checks 0 --repo ohad-oren111/tradeflow --watch   (use the PR number gh returns)
  gh pr merge --squash --delete-branch
  git -C /home/tradeflow/tradeflow fetch origin

Confirm both files exist on origin/main, report the squash-merge hash, and confirm
git status is clean. Per §0.5.185 this is end-to-end — carry it through the merge.
```

**Fallback (if VPS CC unavailable):** same branch+PR flow run manually. Do NOT push to main directly.

---

*End of handoff v14. Target lifespan: until the Sunday reopen is observed clean (V-reopen) and `strategy_decisions`/comparison digest (#1) lands. Then rely on the canonical probes + v15.*
