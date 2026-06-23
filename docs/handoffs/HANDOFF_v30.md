# TradeFlow — Handoff v30 (feed recovered + hardened; FLAT, armed, self-healing — edge still unproven)

*Handoff from end of 2026-06-22. The bot is LIVE on paper, FLAT, healthy, and self-healing after a five-PR session (#167–#171). It is not in a position and not halted. The hardening was all ops-resilience/safety — it did NOT touch the core strategy, which still has no demonstrated edge. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read sections 1–6 first — that's the state-of-the-system as of handoff. Sections 7–16 are reference. Section 14 ranks authority: when this doc disagrees with a live probe, the live probe wins.

**Do not trust this doc alone.** Run the §6 verification block before writing any code. Critical first checks: confirm the bot is FLAT with zero orphans, the drawdown brake reads ARMED (not INERT), and the feed is settling MNQU6 bars.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block, self-contained, env sourced in the same block, expected output described immediately below, decision tree if more than one branch matters. No "you might want to…" — give the command or don't mention it.

**Learning-delivery discipline.** Every new fact (bug pattern, corrected assumption, environmental fact, diagnostic finding) gets surfaced immediately as a paste-ready markdown snippet for the running handoff queue. Do not wait until end-of-session.

**Read before diagnosing.** For a complex state bug, read the full startup log and 3–5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency ("capital at risk", "spiraling"), hit the live broker/DB/raw log — not aggregated metrics or the dashboard.

**Always draft a VPS smoke-test runbook after PR merge** unless explicitly told otherwise. The owner does not run smoke tests by hand. (See Appendix A.)

**Project standing rules (carried verbatim, cumulative — never shrink):**
- **§0.5.97** — Probe external specs (SeanBot code, third-party behavior) before baking them into TradeFlow. Don't build on a guess about what an external system does.
- **§0.5.98** — Broker/exchange is ground truth. Theorize in chat; **probe on the VPS**. Every quantitative claim re-verified against IBKR/Supabase, never the dashboard.
- **§0.5.197** — Do NOT trust the SeanBot scoreboard column or any "TF leads" headline. The Telegram listener over-captures SeanBot exits ~3.9×; SeanBot P&L from the listener is unreliable. First-party numbers from SeanBot's operator override it (see §0.5.232).
- **§0.5.228** — Explicit `--base main` on every PR create.
- **§0.5.230** — The n≥200 gate is the WRONG test for contractual/mechanical arbs (S05-class). Don't fail a real mechanical edge on sample size alone.
- **CC bash discipline** — In VPS CC Bash tool calls: no `cd X &&`, no `;` separators, no `$(...)`, no `${VAR}`, no heredocs. Write Python to `/tmp/` via the Write tool, then `python3 /tmp/x.py`. PR bodies via `--body-file`. Single `sleep`/Python loop for waits.
- **VPS CC autonomy** — Not using `--dangerously-skip-permissions` (operator opted out; do not suggest it). Broad allows + targeted denies. AUDIT-gate the 6: security-policy mutation, secrets dir, live orders, push-to-main, capital at risk, explicit GATE marker.
- **§0.5.231 (NEW this session)** — **Stale feed + healthy gateway → check contract/front-month expiry FIRST**, before assuming feed degradation. A bot subscribed to an expired contract presents identically to a degraded bar subscription, but no reconnect/gateway-restart can fix it. (Origin: the entire Jun 18–22 outage was MNQM6 expiry, misdiagnosed first as feed degradation. See §5.)
- **§0.5.232 (NEW this session)** — **First-party operator P&L beats our scraped/estimated figures.** SeanBot's ~+16K all-time is real (confirmed by its operator directly), from a window predating our comparison table. Do not over-apply §0.5.197 skepticism to a better first-party source.

---

## 1. Where we are (as of handoff, 2026-06-22 ~23:30 UTC)

### Live production state
- `tradeflow-app`: running, healthy. `tradeflow-ib-gateway`: running, healthy. Supabase 200, dashboard auth-gated (live).
- Broker: **FLAT, zero orphans** — `positions=0 openTrades=0 portfolio=0` (verified via `_probe_ibkr.py` during the batch run, and in the reconciler full-scan log).
- Paper NAV ~$994,739 (IBKR DUQ331660).
- Kill-switch: **drawdown brake ARMED** — base $25,000, 33%, trips at $8,250, net-liq fallback if `ALLOCATION_USD` ever unset. Consecutive-loss halt (≥10) also active.
- Feed: live on **MNQU6**, per-minute evals. Current decisions are `noop_regime` — the 30m-EMA200 regime gate correctly holding the long-only strategy out of a downtrend. **This is the strategy working as designed, not a fault.**
- Channel: quiet. No `[ALERT]`/`MANUAL`/terminal-feed noise in healthy operation.
- No manual operational overrides active. No open PRs. main in sync.

### What just shipped (five PRs + docs)
- **#167** (HEAD after merge `1123476`) — `fix(roll)`: pin front-month via `INSTRUMENT` env; roll MNQM6→MNQU6. Operator-AUDIT-merged. *Restored the dead feed.*
- **#168** (in `1123476`) — feed self-heal escalation ladder (verify-against-bar-flow, not cosmetic reconnect; escalate to gateway restart after N failures; episode-scoped alerting `[ALERT]→[FEED]`). *A 3-day outage now yields ~3–4 messages, not ~800.*
- **#169** — `feat(kill)`: arm the drawdown brake (net-liq fallback + `ALLOCATION_USD=25000`, trip $8,250). Self-merged in the autonomous batch.
- **#170** — `feat(roll)`: dynamic front-month auto-roll from the live contract chain; flat-only roll at ~8-day window; `INSTRUMENT` pin removed (kept as optional override). **Design deviation (documented):** in-process hot-roll needed risky orchestrator-buffer surgery, so CC rolled via graceful self-restart (safer, reuses the proven boot-seed path). 5 files not 4 (added a package `__init__.py`); no protected file touched. Self-merged.
- **#171** (deployed app HEAD `5938610`) — `feat(feed)`: contract-expiry-suspected escalation hint when a gateway restart can't revive the feed. Self-merged + deployed.
- **#172** (repo HEAD `6c0cc1a`) — docs-only: `HANDOFF_v29.md` (CC's autonomous-batch handoff). App code identical to `5938610`.

**Deployed app commit: `5938610`. Repo main HEAD: `6c0cc1a` (v29 docs on top, app-identical).**

### What we discovered this session (not yet in code)
- **`pnl_epoch` does not persist** — it resets to boot time on every restart. This now **compounds with #170**: #170 rolls the contract *by restarting*, so each auto-roll (~4×/year) **plus every redeploy** silently zeroes the realized-drawdown accumulator the now-armed brake measures against. Evidence: CC Task-E finding on #169; `KILL_SWITCH_PNL_EPOCH` unset. → This is the #1 queued PR (§13). The drawdown brake is not fully trustworthy until this lands.
- **Original outage root cause: MNQM6 (June contract) expired and was never rolled.** The bot was subscribed to a dead contract. Confirmed by CC probing the contract chain — not by reasoning. (See §5.)

---

## 2. The session's work thread

1. **Opened on a strategy/trust question, not an ops one.** Operator challenged whether our testing is too pessimistic (SeanBot "makes money", TF loses). Worked the Phase-21 gauntlet results: zero strategies pass the honest harness; four are "real-but-uncertifiable" (S03 short-vol, S05 crypto carry, S06 crypto liq-fade [strongest OOS], S12 IPO-lockup [borrow eats it]).
2. **Reconciled the SeanBot number.** I first argued SeanBot "+16K" was almost certainly a measurement artifact (§0.5.197 over-capture; the pasted table showed SB cum −360). Operator corrected me: his friend who runs SB showed real first-party numbers — ~16K all-time is genuine, from a window before our table. I revised and took the first-party figure. → new §0.5.232.
3. **Decomposed TF's loss arithmetic from raw fills.** June 17 (−$1,470, 8W/8L): avg winner ~+$155 (clipped near +40pt), avg loser ~−$335 (runs near −82pt); break-even win rate ~67% vs ~50% actual. The trail clips winners and lets losers breathe — **negative expectancy by construction.** This is the real money problem; it is untouched by this session's work.
4. **Pivoted to ops when the feed turned out dead.** Read the Jun 18–22 Telegram feed: `evals=0` for days, `watchdog_stale_bars`/`feed_self_heal_reconnect` looping, ~30 `MANUAL INTERVENTION` pages on Sat Jun 20 (gateway outage), app restart-looping (20×).
5. **First diagnosis WRONG (see §5):** I called it "degraded bar subscription after the Saturday gateway outage." CC probed and found the truth: **MNQM6 had expired** — dead-contract feed, unfixable by reconnect.
6. **Recovered:** #167 rolled to MNQU6; #168 fixed the cosmetic-recovery loop + noise. Deployed `1123476`, feed live, FLAT.
7. **Hardened in an autonomous batch** (operator waived the AUDIT merge-gate for #169–#171): #169 armed the drawdown brake; #170 made front-month dynamic so the contract can't silently die at the next expiry (MNQU6 → 2026-09-18); #171 sharpened the terminal alert to name expiry as the suspected cause.
8. **CC ran the loop end-to-end**, self-merged all three, handled one CI-red (a pre-existing smoke test started connecting to live IB once boot-resolution was added — fixed by stubbing the resolver), deviated correctly on #170's mechanism, and wrote v29.

Rabbit holes now closed: feed degradation as the outage cause (it was expiry); "SeanBot P&L is fake" (it's real, first-party).

---

## 3. What the system is actually made of

**Single source of truth:** none as a single map file — this handoff + `docs/handoffs/HANDOFF_v29.md` (CC's) on `main` at `6c0cc1a` are the best available system docs.

Highlights:
- Container: `tradeflow-app` (orchestrator + execution + kill-switch + reconciler + feed watchdog), `tradeflow-ib-gateway` (gnzsnz/ib-gateway).
- Production-live code paths: boot → `front_month.resolve_front_month()` (NEW, #170) → `subscribe_bars` + warmup seed (7000-bar backfill) → strategy eval loop → router/exit → reconciler (drain 30s, full-scan 300s) → kill-switch (30s poll).
- Backend: Supabase (`lifecycles`, `seanbot_signals`, `signal_reconciliations`).
- Alerting: Telegram. `[ALERT]` = operator-facing; `[FEED]` = log-only chatter (post-#168).
- Feed self-heal: episode state machine (open → escalate-to-gateway-restart → MANUAL [now with expiry hint] → recovered), de-duped to ~bounded alerts per episode.
- Known landmine surfaces: `INSTRUMENT` env still exists as a manual override — do not delete; `pnl_epoch` unpersisted (see §13).

---

## 4. Verified facts (2026-06-22) — DO NOT challenge unless schema/contract migrates

- **Deployed app commit = `5938610`** (PR #171). Repo main HEAD = `6c0cc1a` (#172 docs, app-identical).
- **MNQ is quarterly**: H/M/U/Z, expiry 3rd Friday. MNQM6 expired ~2026-06-19 (root cause of the outage). Current front-month **MNQU6 expires 2026-09-18 (dte ≈ 87 at handoff).**
- **Front-month is now resolved dynamically** from the live IB contract chain (#170), not the `INSTRUMENT` pin. `INSTRUMENT`, if set, overrides; if unset, resolution is authoritative.
- **Auto-roll is FLAT-only** — never rolls out from under an open position; defers to the next flat moment.
- **#170 rolls via graceful self-restart**, not in-process hot-roll (mechanism deviation, documented).
- **Drawdown brake**: `ALLOCATION_USD=25000`; trips at 33% = $8,250; falls back to live net-liq if unset (never inert again).
- **New load-bearing fact:** `pnl_epoch` is unpersisted → resets on every restart, including every auto-roll. The armed drawdown brake's baseline is therefore reset by routine restarts until §13 ships. Evidence: CC #169 Task-E finding; `KILL_SWITCH_PNL_EPOCH` unset.
- **Regime gate** blocks longs when price ≤ 30m-EMA200. `noop_regime` in a downtrend is correct, not broken.

---

## 5. Wrong diagnoses this session — READ BEFORE YOU DEBUG

**Wrong diagnosis #1 — feed degradation.**
- *Diagnosis:* "The bar feed degraded after Saturday's gateway outage; the subscription silently failed to resume."
- *Evidence that misled:* days of `watchdog_stale_bars` + `feed_self_heal_reconnect_escalation` + `reconnect_recovered`, immediately following a real Sat Jun 20 IB-gateway outage and app restart-loop. It *looked exactly* like the known degraded-subscription family.
- *Why wrong:* the gateway and socket were fine. The bot was subscribed to **MNQM6, which had expired**. No reconnect or gateway restart can stream bars from a dead contract.
- *Correct diagnosis (CC probe):* expired front-month, never rolled. Fixed by #167 (roll) and prevented going forward by #170 (auto-roll).

**Wrong diagnosis #2 — SeanBot P&L is a measurement artifact.**
- *Diagnosis:* "SeanBot +16K is almost certainly not real" (leaned on §0.5.197 + the table showing SB cum −360).
- *Why wrong:* the table was the unreliable listener feed; the +16K is first-party from SB's operator, from a window before the table.
- *Correct:* take the first-party figure. → §0.5.232.

**Lesson for next session:** Both misses came from reasoning over a symptom that pattern-matched a familiar bug, instead of probing the one ground-truth fact that would discriminate (the contract's expiry date; the first-party P&L). When a symptom matches a known family, **find the single probe that rules the family in or out before acting** (§0.5.98, §0.5.231).

---

## 6. Verification block — run this before doing anything

All blocks are runnable from the Mac via the `tradeflow` SSH alias. Expected output is below each.

**V0 — deployed commit + container health**
```bash
ssh tradeflow 'cd /home/tradeflow/tradeflow && git rev-parse HEAD && docker inspect tradeflow-app --format "Restart={{.RestartCount}} Status={{.State.Status}}"'
# Expect: 6c0cc1a (or later); Status=running. RestartCount low/stable.
# If HEAD older than 5938610: deployed code is stale — rebuild (see §15 step 6).
```

**V1 — broker truth: FLAT + zero orphans**
```bash
ssh tradeflow 'docker logs tradeflow-app --since 8m 2>&1 | grep -E "portfolio — count|positions — count|open_trades — count" | tail -3'
# Expect: all three "count=0". 
# If any > 0: STOP. Position/orphan present — reconcile from broker before any code change (§0.5.98).
```

**V2 — drawdown brake ARMED (not INERT)**
```bash
ssh tradeflow 'docker logs tradeflow-app --since 90m 2>&1 | grep "KILL]" | grep -iE "ARMED|INERT|fallback" | tail -3'
# Expect: "drawdown brake ARMED — base=$25000 ... trip=$8250". 
# If "INERT" appears: #169 not deployed — rebuild + force-recreate tradeflow-app.
```

**V3 — front-month resolved + feed live**
```bash
ssh tradeflow 'docker logs tradeflow-app --since 90m 2>&1 | grep -E "ROLL]|BAR] MNQ" | tail -5'
# Expect: "[ROLL] resolved front-month=MNQU6 dte=8x" and "[BAR] MNQU6: settled" within ~the last 60s.
# If resolver returns an expired/empty symbol: STOP — this is the §0.5.231 failure; do not assume feed degradation.
```

**V4 — channel quiet (no false alerts in healthy op)**
```bash
ssh tradeflow 'docker logs tradeflow-app --since 90m 2>&1 | grep -cE "\[ALERT\]|MANUAL INTERVENTION"'
# Expect: 0 during healthy operation.
# If > 0: read the matching lines — a real episode is open, or #168 alert-routing regressed.
```

---

## 7. Pending work queue

Priority depends on §6 state, not this ordering.

### PR #173 — persist `pnl_epoch` (BLOCKER for trusting the drawdown brake)
The drawdown baseline resets on every restart, and #170 restarts on every roll. Store/restore `pnl_epoch` from Supabase (preferred) or pin via env. **Full brief in §13.** AUDIT-recommended (kill-switch), operator may batch-waive as before.

### Auto-roll: live proof pending (calendar-paced)
#170 is verified in wiring/tests but its first real quarterly roll won't fire until ~MNQU6 expiry (2026-09-18). Cannot be forced; flag a watch then.

### Self-heal escalation: live proof pending (regime-paced)
#168/#171 verified in tests; a real multi-day wedge escalating to gateway-restart-then-expiry-hint in prod can only be confirmed when one next occurs.

### The actual money question (carried, unsolved)
TF's SMA-bounce strategy has negative expectancy by construction (§2.3). Hardening did not touch this. Paths on the table: (a) exit-geometry experiment (let winners run / tighten stops, measure expectancy in days on live paper); (b) read SeanBot's real code when it arrives (operator requested it) and diff against TF; (c) the crypto candidates S05 (deploy-ready) / S06 (strongest OOS) on the Binance VPS. **None of these started.**

### Operational debt
- `INSTRUMENT` override env retained intentionally (do not delete).
- v29 + v30 handoffs both on main; fine.

---

## 8. Test safety — why we belabor this

Cumulative mocking traps prior sessions hit (do not regress):
1. Tests passing against a fictional schema because they mocked column names.
2. `side_effect` list wrong count → silent `StopIteration` → wrong assertions.
3. Mocked the raw library chain when code uses a wrapper → green tests, broken prod.
4. Shared `MagicMock()` state leaking between tests.
5. **New this session:** a pre-existing smoke test connected to **live IB** once #170 added boot-resolution — passed locally only because the VPS gateway was reachable. Lesson: any test that touches resolution must **stub the resolver/broker**, never hit live IB. CC fixed it by stubbing.

The §12 master-template guardrails prevent all of these. Do not ship tests that skip them.

---

## 9. Pitfalls from prior sessions

- "FLAT" in a prior handoff was once wrong (naked long). **Re-verify position from the broker (V1) every session.**
- Docker healthy ≠ broker healthy. Docker restart ≠ rebuild (must `build` + `--force-recreate`).
- Dashboard / SeanBot scoreboard untrusted (§0.5.197).
- Startup log line can contradict live behavior — trust the broker probe.
- **New:** a stale feed with a healthy gateway is NOT necessarily feed degradation — check contract expiry (§0.5.231).

**Next-session rule: if a claim is quantitative, re-verify it — P&L, position, orphan counts, deployed commit, dte. Especially do not trust handoff numbers without re-query.**

---

## 10. Session discipline lesson (2026-06-22) — orchestrator's own log

**What I (chat-tier) got wrong, and corrected.**
- I twice diagnosed from a symptom that pattern-matched a familiar failure instead of probing the one discriminating fact: (1) called the dead-contract outage "feed degradation"; CC's contract-chain probe corrected me. (2) Called SeanBot's +16K a measurement artifact; the operator's first-party data corrected me. Both corrections came from ground truth I had not yet probed — exactly the §0.5.98 pattern I'm supposed to enforce.
- My #170 brief over-specified the mechanism (in-process hot-roll). CC correctly deviated to roll-via-restart because hot-roll needed risky buffer surgery. The brief should have stated the *outcome* (atomic roll with warmup re-seed, flat-only) and left the mechanism to CC. Good outcome; my spec was too prescriptive.

**Lane discipline held.** The operator explicitly waived the AUDIT merge-gate for the #169–#171 batch. I honored the waiver (human gate is the operator's to waive) while keeping the *technical* stop-conditions non-waivable: CI red, non-empty protected-file diff, regressed test, or non-FLAT/orphaned broker state would halt-and-report. That distinction — operator can waive his own approval gate, but not the safety floor that keeps a broken fix out of a live loop — is the line that held, and should hold next time.

**Honest paper/edge note.** Everything shipped this session is ops-resilience and safety: armed brake, auto-roll, sharper alerts, quiet channel. It is real and it matters — but it is **hardening a strategy with no demonstrated edge**, on a **paper** account. The bot now robustly runs a system whose own fill arithmetic shows negative expectancy (~67% break-even win rate vs ~50% actual). "All green, FLAT, self-healing" must not be read as "working" in the money sense. The next session's most valuable move is the edge question (§7), not more hardening.

**Enforcement rules for next session:**
1. Before acting on any symptom that matches a known failure family, run the single probe that discriminates it (contract expiry, broker position, first-party number). State the expected pattern, then verify.
2. Write CC briefs that specify the **outcome and constraints**, not the implementation mechanism, unless the mechanism is itself the requirement.
3. Keep the lane line explicit: operator waivers apply to approval gates only; the CI/diff/test/broker-state safety floor is never waived.
4. Do not let hardening wins obscure the unsolved edge question. Lead the next session with it.

---

## 11. Logging verbosity — what to demand from any new code

- Every entry/exit/ratchet: `[COMPONENT] symbol: action — reason` (e.g. `[ROLL] rolling MNQU6→MNQZ6 — flat book`).
- Every state transition (episode open→escalate→recover; lifecycle state) logs old→new at INFO.
- Every swallowed exception logs the specific error + context.
- Retry/escalation loops log attempt number and reason.
- Kill-switch logs the armed base, threshold, and trip value at boot; logs every brake evaluation that crosses a warn/halt boundary.
- Any dedup/select-one-of-many logs which row won and why.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill (or copy from §13 below / handoff v29). It enforces patch constraints, code quality, the test-safety guardrails (§8), known gotchas, and the mandatory "What I got wrong during this PR" section.

---

## 13. Current PR brief in flight — hand to Claude Code as-is

`===== COPY FROM HERE — PR 173 brief =====`

# TradeFlow — Claude Code PR Prompt: PR 173 — Persist `pnl_epoch` (drawdown baseline survives restarts)

## Role
You are a senior Python developer working on TradeFlow, an autonomous MNQ futures bot on an IBKR **paper** account (~$994k NAV). This PR closes the last hole in the drawdown kill-switch — bugs here disable a capital protection, treat as production. Clean, tested, production-grade code; never modify unasked files; study existing patterns first; state your expected pattern then read the actual file to confirm; never trust prior claims about behavior without verifying. Verbose logging: `[COMPONENT] symbol: action — reason`.

## Context
The drawdown brake was armed in #169 (base $25,000, trip $8,250). But the brake measures realized drawdown from a baseline timestamp `pnl_epoch` that is **unpersisted** — it resets to boot time on every container start. #170 now rolls the contract by **restarting**, so every auto-roll (~4×/year) and every redeploy silently zeroes the drawdown accumulator, masking a real cumulative drawdown the armed brake should catch. Deployed app HEAD `5938610`; #167–#171 must remain intact. Evidence: #169 Task-E finding; `KILL_SWITCH_PNL_EPOCH` unset in compose.

## 🏗️ System Architecture & Recent Learnings
- Container: `tradeflow-app` (kill-switch runs as `tradeflow-kill-switch`, 30s poll)
- Language: Python 3.x, async
- Database: Supabase (service role env var already wired for `lifecycles`)
- Env Vars this PR touches: `KILL_SWITCH_PNL_EPOCH` (optional manual pin), new persisted-epoch row in Supabase
- Logging: `docker logs tradeflow-app`; module-level LOGGER

### Key Architecture Constraints
- Runtime: kill-switch reads realized P&L since `pnl_epoch`; epoch currently defaults to process start.
- Shell: CC bash discipline — no `&&`/`;`/`$(...)`/`${VAR}`/heredocs in Bash tool calls; scripts to `/tmp/` via Write tool then `python3 /tmp/x.py`.
- Schema: reuse the existing Supabase client/wrapper; do NOT add a new DB library. A single-row `kill_switch_state` table (or equivalent existing settings table) keyed by a constant is sufficient.
- Scope boundary: do NOT change the brake math, the consecutive-loss halt, order placement, exits, reconciler, or the #170 roll resolver.
- **Design decision (state default, recommend):** persist `pnl_epoch` to Supabase on first arm and **restore it on boot if a stored value exists** (so restarts/rolls keep the original baseline). `KILL_SWITCH_PNL_EPOCH` env, if set, overrides (manual reset escape hatch). Recommended default: Supabase store/restore — survives all restarts with no manual maintenance, vs an env pin that someone must remember to update. Provide a documented manual reset path (clear the row or set the env) for when the operator *wants* a fresh epoch.

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints
Files you WILL modify (EXACTLY 3):
- `src/execution/kill_switch.py` (load-or-create epoch on boot; persist on first arm)
- `docker-compose.yml` (document `KILL_SWITCH_PNL_EPOCH` optional override; no behavior change if unset)
- `tests/test_kill_switch.py` (new cases)

Files you MUST NOT modify: `src/instruments/front_month.py`, `src/execution/reconciler.py`, `src/execution/router.py`, `src/strategy.py`, anything under `.tradeflow-secrets/`.

Verification gates:
- `git diff main -- src/instruments/ src/execution/reconciler.py src/execution/router.py src/strategy.py` → MUST be empty
- `git diff main --stat` → EXACTLY 3 files

### 2. Code Quality
`black --check` + `ruff check` pass on the 3 files; type hints preserved; no signature changes to public methods; one import per line; verbose log format.

### 3. Safety
- All pre-existing tests pass. Carry forward known-red tests verbatim — do NOT fix them.
- No change to brake math or the consecutive-loss halt. No new broker order calls.
- Restore path must be **read-then-write-once**: read stored epoch on boot; only write a new epoch when none exists (or when the env override forces a reset). Never overwrite a valid stored epoch on a routine restart — that's the whole bug.
- Adjacent bug found → document, do NOT fix.

## 🧩 Current Mission: make the drawdown baseline durable

### Objective
On boot, restore `pnl_epoch` from Supabase if present; else create and persist it. `KILL_SWITCH_PNL_EPOCH` env overrides (manual reset). Routine restarts and #170 rolls keep the original baseline. The brake's trip value is unchanged.

### Task A: Audit
Read where `pnl_epoch` is set in `kill_switch.py` and where realized P&L is measured against it. Answer in the PR description: exact line the epoch is initialized; is there an existing settings/state table or client to reuse (name it); confirm the brake math and consecutive-loss path are untouched by this change.

### Task B: Implement
Boot: if env `KILL_SWITCH_PNL_EPOCH` set → use it and log `[KILL] epoch: override — <ts>`. Else read stored epoch: if present → restore and log `[KILL] epoch: restored — <ts> (age <N>h)`; if absent → set now(), persist, log `[KILL] epoch: created — <ts>`. Persist on first arm only.

### Task C: Add tests
(a) stored epoch present → restored, NOT overwritten on boot; (b) no stored epoch → created and persisted once; (c) env override wins over stored; (d) brake trip value and consecutive-loss halt unchanged. **Test-safety guardrails (non-negotiable):** fresh `MagicMock()` per test; mock at the Supabase **wrapper** level not the raw `.table().select().execute()` chain; no `side_effect` list without an explicit count comment; match the async decorator pattern of a verified neighbor in this file; assert via `call_args_list` filtered by first positional arg, not call index; set `mock_db.select.return_value`/`upsert.return_value` explicitly.

### Task D: Verify completeness
`grep -rn "pnl_epoch\|KILL_SWITCH_PNL_EPOCH" src/` — classify every hit; confirm boot is the only place that sets/restores it and no second site resets it on restart.

### Task E: Out-of-scope investigation
~10 min: confirm a manual epoch reset (operator intent) has a clean, documented path (clear row or set env) and that it's logged distinctly from the bug it fixes. Document only — do NOT build a UI.

### Task F: Post-merge smoke test
`docker logs tradeflow-app --since 5m 2>&1 | grep "KILL] epoch"` → expect exactly one of `created/restored/override`. Restart the container once and re-grep: expect `restored` (NOT `created`) — proves persistence. STOP and report if a routine restart logs `created`.

## 📤 Expected Output
3 files; git diff stat; PR description with all 10 required items including Task A finding, Task D grep classification, Task E paragraph, protected-file empty diffs, "This PR does NOT change brake math, the consecutive-loss halt, or trading logic," and **"What I got wrong during this PR."**

## 🔍 Pre-Push Checklist
Code quality + full TEST SAFETY GUARDRAILS block + production-safety gates per master template. All protected diffs empty. Confirm a restart restores (not recreates) the epoch.

## ⚠️ Known Gotchas
1. Never overwrite a valid stored epoch on a routine restart — that reset IS the bug.
2. #170 rolls via restart — so "restart" here includes every auto-roll; persistence must survive it.
3. Docker restart ≠ rebuild — owner/CC must `build` + `up -d --force-recreate tradeflow-app`.
4. CC Bash discipline: no `&&`/`;`/`$(...)`/`${VAR}`/heredocs; scripts via `/tmp/`.
5. `KILL_SWITCH_PNL_EPOCH` is a manual-reset escape hatch — keep it.
6. Pre-existing red tests are known — do not fix.
7. Any test touching resolution/broker must stub it — never hit live IB (the #170 CI-red lesson).

`===== COPY TO HERE =====`

---

## 14. Canonical references (in order of authority)

1. **Source code on `main`** at `6c0cc1a` (app `5938610`) — what actually runs.
2. **IBKR broker** (paper DUQ331660) via `ib_client` / `_probe_ibkr.py` — truth for position/orders/NAV.
3. **Supabase** (service role) — truth for lifecycles/signals.
4. **Live `docker logs tradeflow-app`** — truth for in-process behavior.
5. **`docs/handoffs/HANDOFF_v29.md`** (CC's batch handoff) — companion to this doc.
6. **This handoff (v30)** — session context, NOT long-term authority.
7. **v29 and earlier handoffs** — historical; ignore any claim that contradicts 1–4.

---

## 15. First 15 minutes of the next session

1. Read §0.5, §1, §2, §5, §10 of this handoff. §10 (orchestrator log) and §5 (wrong diagnoses) are the most important to internalize.
2. SSH in. Run the §6 block (V0–V4). Confirm: deployed HEAD ≥ `5938610`, broker FLAT + zero orphans, brake ARMED (not INERT), MNQU6 feed settling, channel quiet.
3. If anything in §6 fails, fix that first — do not start new work on a broken base.
4. Hand the §13 PR 173 brief (persist `pnl_epoch`) to VPS CC as an end-to-end mission. Operator decides whether to waive AUDIT again or merge by hand (kill-switch gate).
5. While CC runs, decide the session's real focus with the operator: the edge question (§7) — exit-geometry experiment, SeanBot code review when it lands, or the S05/S06 crypto path. Hardening is largely done; the money question is the point.
6. After #173 merges, run the Appendix A smoke-test runbook via VPS CC (verification-only) and confirm a restart logs `restored`, not `created`.

---

## 16. How to publish this handoff

**Path A — operator's one-shot from the Mac (preferred):** see the session's PUBLISH BLOCK (scp + ssh heredoc that branches off origin/main, commits, opens the PR, polls `gh pr checks` until the required check completes green, then `gh pr merge --squash --admin --delete-branch`, then resyncs main and prints `DONE: v30 merged`).

**Path B — VPS Claude Code brief:**
```
You are VPS Claude Code on the TradeFlow VPS. Save the provided content verbatim to
/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v30.md, then:
  git -C /home/tradeflow/tradeflow add docs/handoffs/HANDOFF_v30.md
  git -C /home/tradeflow/tradeflow commit -m "docs: add v30 handoff (feed recovery + hardening batch 167-171)"
  branch off origin/main, push, open PR --base main, poll gh pr checks until green, squash-merge.
Confirm the file exists, git log shows the commit, git status clean.
```

The handoff exists only once committed to `main`. Until then, treat this as draft.

---

## Appendix A — VPS Smoke-Test Runbook (post-batch: PRs 169–171, deployed HEAD `5938610`)

*Run by VPS Claude Code, verification-only. No code/secret/git mutation. STOP at first FAIL.*

### §1 Pre-flight
```bash
cd /home/tradeflow/tradeflow
git rev-parse HEAD
docker inspect tradeflow-app --format 'Restart={{.RestartCount}} Status={{.State.Status}}'
# Expect: HEAD 6c0cc1a (app 5938610); Status=running.
# If Restarting/Exited: STOP, tail logs, report.
```

### §2 Deployed-code check
```bash
docker exec tradeflow-app python -c "import src.instruments.front_month as f; print('front_month OK', hasattr(f,'resolve_front_month'))"
docker exec tradeflow-app sh -c 'grep -c "ALLOCATION_USD" src/execution/kill_switch.py'
# Expect: "front_month OK True" (#170 present); grep count >= 1 (#169 present).
# If import fails or count 0: STOP — image stale, rebuild: docker compose up -d --build --force-recreate tradeflow-app
```

### §3 State probes
```bash
docker logs tradeflow-app --since 10m > /tmp/tf_recent.log 2>&1
wc -l /tmp/tf_recent.log
grep -ciE 'error|exception|traceback' /tmp/tf_recent.log
# Expect: ~100–400 lines for 10m; error count 0 (the KILL ALLOCATION line is INFO/WARNING by design pre-#169, should be ARMED now).
# If >5000 lines: possible loop, STOP. If errors >0: sample and note.
```

### §4 Source-of-truth (broker + DB)
```bash
docker logs tradeflow-app --since 12m 2>&1 | grep -E "portfolio — count|positions — count|open_trades — count" | tail -3
# Expect: all count=0 (FLAT, zero orphans). If any >0: STOP (capital-state mismatch).
```
```bash
docker logs tradeflow-app --since 90m 2>&1 | grep "KILL]" | grep -iE "ARMED|INERT" | tail -2
# Expect: "drawdown brake ARMED ... trip=$8250"; NO "INERT". If INERT: FAIL — #169 not effective.
```

### §5 PR-specific behavior tail
```bash
docker logs tradeflow-app --since 90m 2>&1 | grep -E "ROLL] resolved|BAR] MNQU6: settled" | tail -4
# #170: expect "[ROLL] resolved front-month=MNQU6 dte=8x" and live MNQU6 bars.
docker logs tradeflow-app --since 90m 2>&1 | grep -cE "\[ALERT\]|MANUAL INTERVENTION"
# #168/#171: expect 0 in healthy op (episode-scoped alerting). If >0, inspect the lines.
```

### §6 Verdict
PASS if: HEAD matches, #169/#170 code present, FLAT+zero orphans, brake ARMED, MNQU6 feed live, channel quiet. FAIL on any INERT brake, non-FLAT broker, stale/expired feed symbol, or alert spam. INVESTIGATE for anything outside baseline that isn't clearly breakage.

### §7 Structured report
```markdown
# Smoke Test Report — PRs 169–171 (HEAD 5938610)
**Verdict:** PASS / FAIL / INVESTIGATE
## §1 Pre-flight — HEAD <hash> (exp 6c0cc1a); status <…>; restarts <N>
## §2 Deployed-code — front_month resolve: <T/F>; kill_switch ALLOCATION_USD: <count>
## §3 State — log lines <N>; errors <N>
## §4 Source-of-truth — positions/openTrades/portfolio: <0/0/0?>; brake: <ARMED/INERT>
## §5 Behavior — front-month resolved: <MNQU6 dte?>; alert/MANUAL count: <N>
## §6 Anomalies / next steps — <free text + recommendation>
```

---

*End of handoff v30. Target lifespan: until PR #173 (`pnl_epoch` persistence) merges and the edge question (§7) has a decided direction. Then supersede with v31.*
