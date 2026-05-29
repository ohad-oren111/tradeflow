# TradeFlow — Handoff v12 (first 2 real trades 1W/1L net ≈ +$233; W-S14.2 fix-all + calibration shipped; orphan stop OPEN)

*Handoff from end of 2026-05-29. The bot is LIVE and (as of ~17:51 UTC) FLAT, re-warming SMA after a 16:51 UTC redeploy (warmup completes ~18:32 UTC). Two lifecycles traded today, both CLOSED. **An orphan protective stop is resting at the broker (SELL STP @30183 GTC ×2) and possibly a second orphan (SELL LMT @30562.25) — clearing these is the first action next session (W-S14.3, already written).** This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read §1–6 first. §7–13 are reference. §14 ranks authority when this doc disagrees with a live probe — **the broker and the live DB always win (§0.5.98).**

**Do not trust this doc alone.** Run the §6 verification block before any code. **Critical first action: probe resting broker orders vs the actual position and clear the orphan stop (W-S14.3).**

---

## 0.5 Standing rules (permanent — do not remove)

- **Copy-paste instruction style.** Every owner action is a self-contained copy-paste bash block, env sourced inline, expected output described below it, decision tree if it branches.
- **Learning-delivery discipline.** Surface each new fact immediately as a paste-ready snippet; don't wait for end-of-session.
- **Read before diagnosing.** Read full startup log + 3–5 full cycle narratives before a root cause. `grep | wc -l` summaries are the #1 cause of wrong diagnoses.
- **Verify severity against source of truth.** Hit the live broker/DB/raw log before urgency language.
- **Always draft a VPS smoke-test runbook after a PR merge** unless told otherwise. The owner does not run smoke tests by hand.
- **§0.5.98 — broker is ground truth** for fills, P&L, positions, and resting orders — NOT internal DB tables.
- **§0.5.97 — probe external specs** (broker contracts, fees, schema, library APIs) against the source before baking into briefs.
- **§0.5.168 — branch off `origin/main`, never local `main`** (local drifts cosmetically post-squash; use `git checkout -B main origin/main`).
- **§0.5.179 — deploy via `up -d --force-recreate`, not `restart`.** Every recreate resets SMA warmup (~99 min, SMA100 on 1-min bars).
- **§0.5.181 — farm flap** = some combination of 2103/2105/10182, socket stays up, bar sub dies.
- **VPS CC bash discipline** — no chained `&&`/`;`/`$(...)`/heredoc/`${VAR}`; write `/tmp/scriptN.py` via Write, commit via `-F /tmp/commitmsg.txt`, PR body via `--body-file`.
- **NEW §0.5.184 — autonomy contract (this session, in memory).** Self-merge REPORT/AUTO PRs after CI green without asking. Pause ONLY for: AUDIT diffs (order execution / strategy / kill-switch / secrets / broker-state-altering), the one strategy-parameter decision, and genuine external blockers. Default when uncertain: REPORT.
- **NEW §0.5.185 — RESOLVE / end-to-end default; maximize hands-off.** Every work order to VPS CC is end-to-end (RESOLVE mode): GOAL + decision tree + full mission — implement, test, ship the PR, self-merge REPORT/AUTO after CI green, deploy, and verify from ground truth, then report. **NOT probe-and-wait.** VPS CC owns the task to completion and does not bounce a decision the evidence can settle; the owner is PM/orchestrator and types single-word approvals only at the §0.5.184 gates. A work order that only gathers evidence is incomplete unless evidence-gathering was the explicit GOAL. When uncertain whether a step needs a gate, default to REPORT and keep going.

---

## 1. Where we are (as of handoff, 2026-05-29 ~17:51 UTC)

### Live production state
- **Containers:** `tradeflow-app` healthy, `tradeflow-ib-gateway` healthy, `tradeflow-telegram-listener` running (healthcheck now **disabled** — no longer false "unhealthy"). All recreated at 16:51 UTC; RestartCount=0.
- **Deployed code:** app image built from the W-S14.2 batch (through PR #59). PR #60 (watchdog RLS fix) runs **host-side**, no rebuild needed. Confirm `origin/main` HEAD with `git rev-parse origin/main` — f5e1f3d was PR #59; **PR #60 merged after it** (final hash not captured in the session log).
- **Warmup:** re-seeding since 16:51 UTC; completes ~18:32 UTC. Until then `decision=noop_warmup`, `ma_slow=nan`.
- **Position:** as of the 17:51 UTC digest, **FLAT** (both lifecycles CLOSED). **RE-VERIFY from broker.**
- **⚠️ Resting orders / orphans:** `SELL STP @30183 GTC ×2` confirmed resting (orphan from CLOSED lifecycle #1 `c44c5f95`, never cancelled on its TARGET exit). Lifecycle #2's stop-out likely orphaned its `SELL LMT @30562.25` target too. **Probe and clear (W-S14.3) — orphan resting orders can fill with no position and flip the long-only bot SHORT.**
- **Day realized P&L (broker):** ≈ **+$231.54** = #1 `+599.52` (TARGET) + #2 `−367.98` (STOP, slipped ~15pt: filled 30322.38 vs 30337.25 stop).

### What just shipped (W-S14.2, all merged to origin/main, 398 tests green)
- **PR #55 (Track 2, AUDIT, D-1 approved)** — `ma_bullish_tolerance_pts=2.0`; `bullish_ok = close >= open − tol` (was `close > open`). Touch band unchanged.
- **PR #56 (Track 3, AUDIT)** — farm-flap resubscribe on **ANY** of `_FARM_FLAP_CODES={2103,2105,10182}` (was full trio); fixes the 04:03 UTC 37-min blind window.
- **PR #57 (Track 4b, REPORT)** — commission charged **both sides**: `commission_per_side_usd=0.62`, `commission_rt_usd` derived = `1.24`. 2ct nets `$599.52` (was `$600.76`). Track 4a correctly NOT done (columns don't exist — see §4).
- **PR #58 (Track 5, REPORT)** — hourly session digest (`[ALERT] hourly_session_digest`), suppressed-in-position counter, daily-report TRADING section, IB-probe retry (3× backoff).
- **PR #59 (Track 6, REPORT)** — docker log rotation (json-file 10m×3, all 3 services), `src/journal_rotation.py`, cleanup-script journal prune, listener healthcheck disabled, `COPY scripts` in Dockerfile.
- **PR #60 (Track 5c fixup, REPORT)** — daily-report **data reads use service-role key** via `_supabase_data_env()` (anon hit RLS deny-all → false zeros). Self-caught during verification.

### What we discovered this session (evidence in §4/§5)
- The SeanBot opportunity gap is the **BULLISH gate, not touch** — backtest recovered 8/12 SeanBot entries by relaxing bullish 2pt; widening touch recovered **0**.
- **Bracket cleanup is likely systemically broken** — the opposite leg isn't cancelled on exit (orphan @30183 evidence).
- `reconciliations.jsonl` is **ephemeral** — wiped by container recreate → daily-report SeanBot scorecard shows "journal not found".
- Lifecycle summary columns (`realized_pnl` etc.) **do not exist** in TradeFlow's schema.
- GitHub branch-protection ruleset requires up-to-date branches + CI re-pass; `--admin` cannot bypass.

### Chat-side orchestrator notes (logged this session — judgment calls to carry forward)
- **The bullish calibration is LIVE but UNPROVEN.** It hasn't traded yet (re-warming). Today was a **reversal day** (SeanBot also lost −74/−70pt), so do NOT read success or failure into the +$233 day. Measure forward over several sessions via the reconciliation scorecard before any further loosening. It's paper — experimentation is safe (informational risk, not capital), so bias toward shipping and measuring rather than stalling.
- **Both of today's trades entered PRE-calibration** (deploy 16:51 UTC; #2 entered ~15:12 UTC). The −$367.98 loss is the old strict strategy, not the new parameter.
- **The orphan stop @30183 is a genuine landmine** — it can fill with no position and flip the long-only bot SHORT, unmanaged. The pattern (opposite leg not cancelled on exit) is probably **systemic**, not a one-off — treat bracket cleanup as a real bug. Clear + fix (W-S14.3) before trusting more trades.
- **Stops slipped ~15pt** on #2 (filled 30322.38 vs 30337.25 stop) — watch execution quality as trade count grows.
- **The ~50 permission stops were almost all harness per-command prompts**, now fixed at the source (settings allow/deny). Residual gates are only AUDIT + the strategy-param call + external blockers (§0.5.184). The merge round-trips that remain are GitHub branch-protection, NOT Claude — enable auto-merge / relax "require branches up to date" to cut them (owner's call, security tradeoff).

---

## 2. The session's work thread

1. **A-S14.1 review** found TradeFlow's first trade (#1) won +$599.52, flagged touch as the dominant noop (566/725), and recommended a touch+bullish backtest.
2. **W-S14.2 Track 1 backtest** pulled 2,439 IB historical 1-min bars, validated the gate replay **bar-for-bar** against the live strategy (106==106, no-cooldown), and **corrected the A-S14.1 framing**: relaxing **bullish** 2pt → 8/12 SeanBot recovery; widening touch → 0. → D-1 approved 2pt bullish tolerance.
3. Shipped Tracks 2–6 as PRs #55–#59 off `origin/main`, full suite green each.
4. **Merge friction:** branch-protection ruleset blocked `--admin` and required each branch be updated + re-pass CI; resolved by serial update→wait→merge. #59 conflicted with #58 on the orchestrator import block — resolved with a merge (not rebase), 398 green.
5. **Track 7 batched deploy** at 16:51 UTC; gateway recreated too (compose logging drift) — open position + bracket survived intact.
6. **Bug caught in verification:** daily-report TRADING showed false zeros (anon/RLS) → PR #60 (service-role data reads) → re-ran, `lifecycles_today=2` correct.
7. Lifecycle #2 stopped out at ~17:50 UTC (−$367.98); bot went FLAT and is re-warming.
8. **Autonomy fix** (owner raised ~50 prompts): diagnosed as harness per-command prompts (not decision gates); applied `settings.json` + `settings.local.json` allow/deny; saved to memory.

---

## 3. What the system is made of

**Single source of truth:** no canonical system map; this handoff + source on `origin/main` is best available.
- Strategy: `src/strategy.py` `Sma100BounceStrategy` (SMA100-bounce, long-only, MA100>MA50 order, touch band `[sma−15, sma+5]`, bullish-with-tolerance, gap≥0.5, SL=75/TP=150).
- Execution: `src/execution/router.py` + `reconciler.py` (entry/exit, brackets). **Suspect: opposite-leg cancel on exit.**
- Comparison: `src/comparison/seanbot_reconciler.py` (app-side, polls Supabase `seanbot_signals`, writes `/app/logs/reconciliations.jsonl` — ephemeral).
- Orchestrator: `src/orchestrator.py` (decision journal ring + `/app/logs/decisions.jsonl`, hourly digest task, watchdogs, self-heal).
- Daily health report + cron: `scripts/tradeflow_watchdog.py` (host-side, 09:00 UTC).
- Self-heal: farm-flap resubscribe (`ib_client.py`, any-code) + socket-reconnect re-arm.

---

## 4. Verified facts ([2026-05-29]) — DO NOT challenge unless schema migrates

**NEW this session:**
- `ma_bullish_tolerance_pts = 2.0`; `bullish_ok = close >= open − tol`. Touch band unchanged.
- `commission_per_side_usd = 0.62` is the source of truth; `commission_rt_usd` is a **derived property = 1.24**. Call sites read `MNQ.commission_rt_usd` and get the true round trip.
- **`lifecycles` has NO `realized_pnl` / `initial_price` / `current_inventory` / `inventory_cost_basis`; `lifecycle_events` has NO `event_type`.** PostgREST returns `42703` for these. The real summary is **first-class columns**: `pnl_net`, `entry_price`, `commission_total`, `exit_*`. These are SeanBot grid-bot fields — DO NOT write to them.
- `_FARM_FLAP_CODES = frozenset({2103, 2105, 10182})`; **ANY** code (debounced 3s, 30s guard) triggers resubscribe.
- **Daily-report data reads MUST use the service-role key** (`_supabase_data_env()`). Anon → RLS deny-all → HTTP 200 + `[]` → silent false zeros. `probe_supabase` may keep anon (only needs a 200).
- Docker log rotation active (json-file `max-size:10m max-file:3`) on all 3 services; listener healthcheck disabled.
- App container image = W-S14.2 batch (through #59). #60 changed only the host-run watchdog (no rebuild).

**Carry-forward:** MNQ TICK=0.25 / MULT=$2 / maint margin ~$3,636 / quarterly Mar-Jun-Sep-Dec (§0.5.97-verified). IBKR paper DUQ331660. `.tradeflow-secrets/.env` shadows compose `${VAR:-default}` — grep before assuming. `pip3` absent (use `python3 -m pip`); host venv pytest `/home/tradeflow/.venv/bin/pytest`; compose service `ib-gateway` ≠ container `tradeflow-ib-gateway`.

---

## 5. Wrong diagnoses this session — READ BEFORE YOU DEBUG

1. **"Touch is the blocker"** (from A-S14.1's 566/725 touch noops). **Wrong.** Those touch noops are mostly bars while in-position / not at a setup. Backtesting against SeanBot's **actual entry bars** showed widening touch recovered **0**; the real gap was the **bullish** gate (7/12 SeanBot entries were red/doji touch bars). *Lesson: the aggregate noop distribution does not tell you which gate to relax — score against the benchmark's real entries.*
2. **"Gate-replay logic is broken"** (real strategy fired 105, manual replay 29). **Wrong.** Pure 10-bar cooldown difference; a no-cooldown comparison gave 106==106. *Lesson: validate a replay bar-for-bar against the real strategy before trusting a sweep.*
3. **"Daily report shows 0 lifecycles → counting bug."** Partly. The deeper cause was the **anon key + RLS deny-all** returning `[]`. *Lesson: RLS deny-all looks exactly like "no activity" — use the service-role key for data reads and LOG the query result.*

**Meta-lesson:** every wrong turn came from trusting an aggregate over the source-specific probe. Score against the benchmark's real bars; validate replays; use the privileged key.

---

## 6. Verification block — run before doing anything

**V0 — Orphan check (CRITICAL FIRST).** From a separate clientId, `reqAllOpenOrders` + `positions()`. Expect: resting orders == exactly the current position's bracket. **If a `SELL STP @30183` or any order not matching the live position is resting → ORPHAN → run W-S14.3 before anything else.**

**V1 — Lifecycles / P&L (service-role key).** Query `lifecycles` last 24h with `SUPABASE_SERVICE_ROLE_KEY`. Baseline: 2 CLOSED today (#1 `c44c5f95` pnl_net≈+599.52; #2 `9b6f2df8` pnl_net≈−367.98). Anon key returns `[]` — do not use it for data.

**V2 — Deployed-code truth.** In `tradeflow-app`: `ma_bullish_tolerance_pts` present in `config/risk_params.py`; `_FARM_FLAP_CODES` frozenset in `ib_client.py`; `commission_per_side_usd` in `config/instruments.py`. HEAD = `git rev-parse origin/main` (≥ PR #60).

**V3 — Broker truth.** `positions()` (expect FLAT unless a new trade opened) + executions for today (2× SLD @30408 for #1; stop fills for #2).

**V4 — Warmup + feed.** `[STRAT] eval` reaching non-warmup with new thresholds; bars 1/min; 0 farm-flap/watchdog alerts. Capture the **post-calibration decision distribution** (expect fewer `noop_filter:bullish`).

**V5 — Telemetry.** Confirm `[ALERT] hourly_session_digest` fired on the hour and the daily report TRADING section shows real numbers (not zeros).

---

## 7. Pending work queue (priority depends on V0/V1, not this order)

### W-S14.3 — Orphan-order safety sweep (TOP, already written, ready to paste)
Probe resting orders vs position; cancel confirmed orphans (AUDIT, one approval); audit + fix bracket cleanup in `router.py`/`reconciler.py` if systemic; verify resting == position bracket. File handed to owner.

### Forward-measure the bullish calibration
The 2pt change is LIVE but hasn't traded (re-warming). Measure its effect via the reconciliation scorecard over multiple days before any further loosening. Today was a reversal day (SeanBot also lost −74/−70pt) — do not overfit.

### Durable reconciliation/decision tables (queued)
Supabase `signal_reconciliations` + `strategy_decisions` (operator-paste migration) → daily-report SeanBot scorecard survives restarts (currently ephemeral "journal not found") + unblocks PR-D3c daily comparison digest. Optionally a **persistent volume** for the JSONL journals.

### Kill-switch PR
Real-money-readiness gate. Required before any live (non-paper) consideration.

### Operational
- Optional: enable GitHub auto-merge or relax "require branches up to date" to cut merge round-trips (security tradeoff — owner's call).
- Botty AI deferred decision: 30 days post-TradeFlow-live, re-eval BTC/ETH cointegrated stat-arb on Binance perp (Gemini R2 gates: tracking error <5%, zero exec-failure, profit factor >1.30 over 30+ cycles).

---

## 8. Test safety
Carry-forward: tests must not mock a fictional schema; `side_effect` lists need correct counts (silent StopIteration); mock at the wrapper boundary not the raw lib; fresh `MagicMock()` per test (no leaked state); `asyncio_mode=auto`. The master template (§12) guardrails prevent these.

## 9. Pitfalls from prior sessions
- Don't trust handoff quantitative claims (orphan counts, P&L) without re-query — **re-verify the orphan order set from the broker.**
- Grep writers in all syntax forms (the lifecycle POST vs polling GET confusion).
- "Table/column X exists" — verify against live PostgREST (the lifecycle-summary-columns trap).
- Local `main` drifts post-squash — always `git checkout -B main origin/main`.

## 10. Session discipline lesson (2026-05-29)
The session's good calls came from probing the specific source (backtest vs SeanBot bars; broker for orphans; service-role for data). **Enforcement next session:** (1) score strategy questions against the benchmark's real entries, not aggregate noops; (2) validate any replay against the live strategy; (3) data reads use the privileged key and log the result.

## 11. Logging verbosity
Every decision logs `[STRAT] eval ... decision= failed=`; transitions log old→new; swallowed exceptions log type+context; the hourly digest aggregates rather than per-bar spam. Demand this in new code.

## 12. Master template
Use the `code-pr-brief` skill for every PR (patch constraints, test guardrails, known gotchas, "what I got wrong").

## 13. Current PR brief in flight
**W-S14.3 (orphan-order safety sweep)** is written and in the owner's hands — paste it as-is to VPS CC. It embeds §0.5.184 (autonomy rule) and gates only the orphan cancel + any bracket-cleanup fix (both AUDIT).

## 14. Canonical references (authority order)
1. **Broker** (ib_async, IB Gateway 4004) — truth for positions/fills/resting orders.
2. **Supabase** via **service-role** key — truth for lifecycles/events.
3. **Source on `origin/main`** (≥ PR #60) — what runs.
4. **Decision/reconciliation journals** (ephemeral, in-container) — recent decisions only.
5. **This handoff (v12)** — session context, not long-term authority.
6. **v11 and earlier** — historical; ignore where they contradict 1–3.

## 15. First 15 minutes of next session
1. Read §0.5, 1, 4, 5.
2. SSH in; run §6 V0–V2. **Confirm orphan-order status first.**
3. If orphans resting → paste **W-S14.3** to VPS CC (clear them + fix bracket cleanup).
4. Confirm warmup completed and capture the post-calibration decision distribution (V4).
5. Decide on the durable reconciliation tables (queued) if continuing observability work.

---

## 16. How to publish this handoff (two-step: owner → server, then VPS CC → repo)

**Step 1 — Owner pushes both docs to the server (scp).** From the machine holding the files:
```bash
scp HANDOFF_v12.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v12.md
scp focus_brief_session_15.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/focus_brief_session_15.md
```
Expect: two `100%` transfer lines. Then hand VPS CC the brief below.

**Step 2 — VPS CC commits + PRs to the repo (paste-ready brief):**
```
You are VPS Claude Code on the TradeFlow VPS. HANDOFF_v12.md and
focus_brief_session_15.md are already on disk in
/home/tradeflow/tradeflow/docs/handoffs/. Publish them to origin/main via a PR
(push origin main is blocked by branch protection). Use ssh-safe single steps —
no chained &&, no heredoc; commit message via /tmp/handoff_commitmsg.txt, PR body
via --body-file /tmp/handoff_pr.md:

  git -C /home/tradeflow/tradeflow fetch origin
  git -C /home/tradeflow/tradeflow checkout -B claude/handoff-v12 origin/main
  git -C /home/tradeflow/tradeflow add docs/handoffs/HANDOFF_v12.md docs/handoffs/focus_brief_session_15.md
  (write /tmp/handoff_commitmsg.txt = "docs: add v12 handoff + S15 focus brief (W-S14.2 shipped; orphan stop open)")
  git -C /home/tradeflow/tradeflow commit -F /tmp/handoff_commitmsg.txt
  git -C /home/tradeflow/tradeflow push -u origin claude/handoff-v12
  gh pr create --repo ohad-oren111/tradeflow --base main --head claude/handoff-v12 --title "docs: add v12 handoff + S15 focus brief" --body-file /tmp/handoff_pr.md
  gh pr checks --watch
  gh pr merge --squash --delete-branch
  git -C /home/tradeflow/tradeflow fetch origin

Confirm both files exist on origin/main, report the squash-merge commit hash, and
confirm git status is clean. Per §0.5.185 this is end-to-end — do not stop at the
push; carry it through the merge and report the final commit.
```

**Fallback (if VPS CC unavailable):** same branch+PR flow run manually. Do NOT push to main directly. The handoff exists only once committed to `origin/main`; until then this is a draft.

---

*End of handoff v12. Target lifespan: until the orphan stop is cleared, bracket cleanup is fixed, and the bullish calibration has a multi-day forward read. Then rely on the canonical probes + v13.*
