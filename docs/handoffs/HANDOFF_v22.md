# TradeFlow — Handoff v22 (GATE-ZERO #109 deployed + the money gate is PROVEN live; create_lifecycle double-entry race is the concurrency blocker; concurrency is next after that)

*Handoff from end of 2026-06-04 (~16:30 UTC). Bot is **armed, IN POSITION** (long MNQM6 x2, lifecycle `2812b3fb`), healthy on deployed HEAD `7d82dbd` (PR #109). **The STABILIZE-5 + #109 money gate is CONFIRMED at the broker**: at 16:23:05Z the open position's standalone STP ratcheted off base 30216.25 → 30340.50 (entry+50) and is holding. The single confirmation the whole project was waiting for has landed. The next #1 is **concurrency** — but it is now hard-blocked behind a newly-found `create_lifecycle` TOCTOU double-entry race that must be fixed first. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read §§0.5, 1, 2, 4, 5, 10 first — that's the state of the system and the lessons. §§7–13 are reference. §14 is the authority order when this doc disagrees with itself or a live observation.

**Do not trust this doc alone.** Run the §6 verification block before writing any code. **Critical first action: confirm deployed HEAD is `7d82dbd` (or later), the running container has the `trailing_armed_on_force_fill` marker, and read the live broker/position state — the bot was IN POSITION at handoff, not flat.**

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner is a copy-paste-ready block. Owner (Ohad) is a hands-off PM: he pastes briefs into VPS Claude Code (CC VPS), gives single-word approvals, watches Telegram + dashboard. Minimize his UI/browser steps — prefer one SSH command or one CC VPS brief.

**Learning-delivery discipline.** Surface each new fact/bug-pattern/corrected-assumption immediately as a paste-ready snippet, not at end-of-session.

**Read before diagnosing.** Read full startup log + 3–5 full cycle narratives / the source of truth before proposing a root cause. Diagnosing from `grep | wc -l` is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth** (broker API, Supabase, raw log) before escalating urgency language.

**Always draft a VPS smoke-test runbook after a PR merge** unless told otherwise — the owner does not run smoke tests by hand.

**Always run CC VPS inside a named tmux session (`tf1`/`tf2`).** Launch tmux before starting work so SSH disconnects are non-events; use `claude --resume` (or just `resume`) to restore context after any unplanned disconnect. (This session lost a deploy-in-progress to an SSH drop; tmux makes it a non-event.)

**The §0.5.x numbered registry is canonical in `CLAUDE.md` on `main`** (read at pre-flight). It never shrinks. Load-bearing carry-forwards: §0.5.97 (probe external specs from source — never re-derive MNQ contract/fees/schema from memory); §0.5.98 (broker/exchange state = ground truth, not internal DB); §0.5.158 (compose service `ib-gateway` ≠ container `tradeflow-ib-gateway`); §0.5.159 (`.tradeflow-secrets/.env` shadows `${VAR:-default}` — grep before assuming a compose default); §0.5.160 (new PR branches off `origin/main`); §0.5.186/187 (CC VPS Bash discipline: no heredocs, no `cd &&`, no `;`, no `$(...)`, no `${VAR}`, no `VAR=` prefixes — use `git -C`, Write tool to `/tmp`, `python3 /tmp/x.py` for interpolation, `--body-file` for PR bodies, Python polling loops; **`gh pr checks --watch` exits prematurely — poll with a loop**); §0.5.202 (force-recreate silently un-halts a *halted* bot — confirm halt state before recreate; **see the §0.5.211 refinement below — this is about HALT state, not about open positions**); §0.5.205 (gateway AUTO-OCAs parentId-linked bracket children even with no `ocaGroup`; a modifiable stop must be standalone `parentId=0`); §0.5.206 (protective stop = stop-MARKET; a few pts of slippage is normal, not the bug); §0.5.207 (NEVER-ORPHAN: cancel the standalone stop on any non-stop close); §0.5.208 (lane order STABILIZE > REPLICATE > MEASURE > OPTIMIZE; never stack on an unconfirmed foundation; one change at a time); §0.5.209 (kill-switch restart = manual halt-ack, not a process restart); §0.5.T4/T5 (kill switch raises in-process halt+flatten; a position must never be naked).

**New this session — §0.5.210–214 (write into `CLAUDE.md`):**
- **§0.5.210 — Where the bar-ratchet is ARMED.** The ratchet only walks a stop for lifecycles registered in `OrderRouter._by_lifecycle_id` with a seeded `_highest`. That seeding happens in exactly three places: (1) `OrderRouter._handle_parent_fill` (the fill event the router sees directly); (2) `_recover_state → router.register_recovered` (boot recovery for every non-CLOSED lifecycle); (3) **NEW (#109)** `Reconciler._reconcile_entering → router.register_recovered` (force-fill path). A position that reaches ACTIVE by any path that does NOT hit one of these has a **working protective stop but an un-armed ratchet** — the stop stays at base and round-trips a winner. This was the entire GATE-ZERO failure.
- **§0.5.211 — Deploying over an OPEN position is safe (refines §0.5.202).** Force-recreate over a running (non-halted) bot that holds an open **trailing** position is safe because: (a) the protective STP is a standalone GTC order resting at the broker, which **survives a container restart** — no naked window; and (b) boot recovery (`_recover_state → register_recovered`) re-adopts open ACTIVE positions into the ratchet, so a redeploy actually **arms** a previously-un-armed position. §0.5.202's "confirm flat before recreate" is specifically about not silently un-halting a *halted* bot — it is NOT a blanket "never deploy over a position." Do still confirm halt state and that the position has a live broker-resting stop before recreate.
- **§0.5.212 — `create_lifecycle` has a TOCTOU double-entry race (CONCURRENCY BLOCKER).** The "≤1 non-CLOSED per (symbol, strategy)" invariant is enforced ONLY by a probe-before-insert in `StateMachine.create_lifecycle` (`state_machine.py:137-142`: `await select_non_closed` → `await insert_lifecycle`, comment "enforced in code, not DB"). There is **no `asyncio.Lock` and no DB unique index.** Two independent async tasks reach this — the strategy bar-eval (main `run()` loop) and the SeanBot-trigger task (`orchestrator.py:1321` `create_task(...)` → `_maybe_enter_on_seanbot` → `place_entry` → `create_lifecycle`). They interleave at the `await` boundary → both pass the probe → both insert → **double bracket / 4 contracts on one setup** (proven on 06-01, lifecycles `c06ed026` + `347d5a12`). PR #107 (SB-trigger) widened this window. **Concurrency intentionally adds concurrent entry paths and would turn this from once-in-6-days into routine — fix this BEFORE any concurrency work.**
- **§0.5.213 — `ocaType=3` on a standalone stop is benign.** The gateway stamps `ocaType=3` even on a standalone `parentId=0` stop, but the ratchet modify SUCCEEDS with no Error 10326 when there is no live OCA *sibling*. Observed on order 142 this session. Watch, don't act.
- **§0.5.214 — Handoff publish needs `--admin`.** The repo's branch protection requires status checks, so a plain `gh pr merge --squash` on a docs-only handoff PR can hang/fail. Merge docs-only handoff PRs with `gh pr merge <branch|N> --squash --admin --delete-branch`. Never use `gh pr checks --watch` (exits prematurely).

---

## 1. Where we are (as of handoff, 2026-06-04 ~16:30 UTC)

### Live production state
- `tradeflow-app` recreated **2026-06-04 16:21:59Z**, healthy. Image built 14:55:37Z off `7d82dbd` (#109 source baked, COPY src was cached from the post-merge build).
- `tradeflow-ib-gateway` healthy; `tradeflow-telegram-listener` up.
- Broker (DUQ331660 paper): **IN POSITION** — MNQM6 **long x2**, lifecycle `2812b3fb` (entry 30290.56, force-filled mid-run at 14:16Z). **Standalone GTC STP order 142 resting at `auxPrice=30340.50`** (ratcheted up from base 30216.25 = entry+50), `ocaGroup=''` `ocaType=3`. Unrealized ≈ **+$624** at last read; now protected behind a stop above entry.
- Restart policy `unless-stopped`. `ALLOCATION_USD` unset → 33% drawdown brake inert; the **10-consecutive-loss kill-switch is the only active hard brake** (it fired a *warning* at 6 losses earlier on 06-04; halt at 10).
- Front month: **MNQM6** (conId 770561201).
- `TRADEFLOW_COMMIT` env still reads `unknown` (not baked) — verify deployed code by grepping the running container, never the env var.

### What just shipped (this session)
- **PR #109 → `7d82dbd`** (GATE-ZERO — arm bar-ratchet on reconciler force-fill): the reconciler now adopts a force-filled **trailing** position into the router's ratchet via the existing `register_recovered` primitive (optional `router` handle behind a loose-coupled `RatchetArmer` Protocol; trailing-only; adds a `trailing_armed_on_force_fill` log). **Merged AND deployed AND verified live.** 658 tests (+3), ruff+black clean.

### What we proved / discovered this session (evidence in §2/§4/§5)
- **The money gate is CONFIRMED.** `trailing_stop_ratcheted: stop_id=142 old=30216.25 new=30340.50 highest=30468.50 entry=30290.56 lifecycle=2812b3fb` @16:23:05Z. The open position rode ~+155pt and its broker STP walked off base to entry+50 and held — the exact failure mode that cost the project for weeks (e912f9c2 rode +110→base, zero ratchets) is closed.
- **GATE-ZERO was a *second* layer below STABILIZE-5.** STABILIZE-5 made the stop *modifiable*; #109 makes the ratchet *armed* on the reconciler force-fill path — which was **3 of 4** post-deploy fills (the common path, not an edge).
- **TF's −$3,894 drawdown is dominated by two mechanical defects, not a dead strategy.** June-1 = −$1,854 from a double-entry whose stops blew through on a **gap** (benign, working stops); the ratchet give-backs (af66573f −294.72 peaked +111.94; e912f9c2 −363.22 peaked +110.31; f7efde81 −269.72 peaked +108.69) are what #109 fixes. TF also has real wins: c44c5f95 +600.76, 7b01c277 +553.28, 106471a5 +195.28, 875329ea +173.28.
- **New blocker found: the `create_lifecycle` TOCTOU double-entry race** (§0.5.212). This is the concurrency gate.

---

## 2. The session's work thread

1. **Kickoff asked "are we passing the gate?"** First trap avoided: the frozen-ratchet loss in the Telegram (long @30,717.50 → base −$299.48) was attributed to the new fix by reflex — but the **commit timeline** showed it exited ~8h *before* STABILIZE-5 deployed (running `f89b6e0`/`da57927`). Old bug, not a regression. Lesson banked: check the deployed commit at the time of the event before blaming the latest fix.
2. **Adjudicated the gate from broker truth** (4 post-deploy trades, peak excursion from IBKR 1-min bars): e912f9c2 +110.31 and f7efde81 +108.69 both **crossed +50 yet round-tripped to base with zero ratchet events**. Verdict: **FAIL**, not pending.
3. **Root-caused it from source** (corrected a first incomplete hypothesis — see §5): the ratchet is armed only in `_handle_parent_fill`; the reconciler force-fill path transitions ACTIVE + places the standalone STP but never seeds the router's in-memory copy/`_highest`. → **PR #109** (reuse `register_recovered`). Green, AUDIT, held.
4. **Numbers challenge from the owner** ("TF numbers look very different in Telegram"). Built a full lifecycle ledger; it ties to the dashboard **penny-exact** on all 5 days (cum −3894.26) and matches IBKR exit price/qty on the 4 in-window rows. The discrepancy was **UTC-vs-Costa-Rica time + an incomplete Telegram alert stream**, not a data error. Closed.
5. **"Why does SeanBot win and TF lose at the same time?"** Answer: mostly the exit bug (his trail locks +40–50, TF's broken trail round-trips to −74 base on the same up-move) + concurrency (he runs several positions) + they're mostly *not* the same trades (~90% MISS-filter) + the scoreboard overstates the gap (only ~$1,405 of the −$8,301 is a clean both-live head-to-head; ~$2,873 predates TF going live; ~$4,023 rests on unverified `est.` SeanBot days).
6. **June-1 forensic** (ledger surfaced two −$928 trades): two lifecycles `c06ed026`+`347d5a12`, identical entry 30559.25, 124ms apart, separate brackets = a genuine **double-entry of 4 contracts**; both stops filled ~155pt below trigger at 13:30Z with `exit_oid==stop_oid` and **no cancel/orphan event** → a **gap blow-through on working stops**, NOT the §0.5.205 naked-stop bug. The double-entry traced to the `create_lifecycle` TOCTOU race (§0.5.212) — a **live** structural hole, the concurrency blocker.
7. **SSH drop mid-deploy.** A flat-poller + the deploy were in flight when the connection reset; the work stalled (not in tmux at that moment). Recovered via `claude --resume` inside tmux `tf1`.
8. **Deploy-over-open-position (plan reversal, owner-approved).** CC found the boot-recovery path already arms open positions and the standalone GTC stop survives a restart (no naked window), so "hold deploy until flat" was over-conservative (§0.5.211). With go-ahead: `build` + `up -d --force-recreate` over the open position → boot recovery adopted `2812b3fb` → **ratchet fired at 16:23:05Z** → gate proven.

Closed rabbit holes: "the frozen stop is the new fix regressing" (it was old code); "deploy must wait for a flat book" (recovery path + broker-resting stop make it safe); "the June-1 loss was a naked stop" (it was a gap on working stops).

---

## 3. What the system is actually made of

**Single source of truth:** `CLAUDE.md` on `main` at `7d82dbd` (the §0.5.x registry + autonomy contract) + the repo docs added in v20 (`.claude/skills/change-management/SKILL.md`, `docs/ROADMAP.md`, `docs/runbooks/kill_switch_restart.md`).

Highlights:
- **Live entry paths (two):** (1) TF's own SMA100-touch gate (main `run()` loop); (2) SeanBot-triggered validity-checked path (`_maybe_enter_on_seanbot`, runs as a separate `asyncio` task). Both dispatch through `_handle_trade_signal → place_entry → create_lifecycle`. **The two tasks can interleave at `create_lifecycle`'s probe-before-insert (§0.5.212).**
- **Exit (STABILIZE-5 + #109):** standalone `parentId=0` STP placed post-fill by `ensure_protective_stop`; bar-close ratchet walks it. Armed in three places (§0.5.210). NEVER-ORPHAN cancel on non-stop close.
- **Reconciler:** `SeanbotReconciler` polls `seanbot_signals`; `Reconciler._reconcile_entering` force-fills entries the router's fill event missed (the common path — ~3 of 4 fills) and now arms the ratchet (#109).
- **Dashboard/scoreboard:** `dashboard/trades.py` (TF P&L from `lifecycles.pnl_net` by `exit_filled_at` UTC day), `dashboard/scoreboard.py` + `dashboard/seanbot_authoritative.py` (SeanBot trusted figures vs `est.` reconstruction from `seanbot_signals`).

---

## 4. Verified facts (2026-06-04) — DO NOT challenge unless the schema/contract migrates

- **`lifecycles`** keyed by `lifecycle_id`. Columns include `entry_price, entry_filled_at, exit_price, exit_filled_at, exit_reason, pnl_net, pnl_gross, state, stop_order_id, target_price, strategy, direction`. **`lifecycle_events`** timestamp is `emitted_at`. `strategy_decisions` was **not durable on 06-01** (0 rows) — don't expect a decision journal for early dates.
- **TF dashboard column = `lifecycles.pnl_net` grouped by `exit_filled_at` UTC date.** Reconciled penny-exact this session (cum −3894.26 over 21 closed lifecycles; trading started **05-29**, not 06-01 — the dashboard "TF newly live 2026-06-01" caption is wrong).
- **MNQ spec (§0.5.97-verified):** TICK 0.25 pt, MULTIPLIER $2/pt, COMMISSION_RT $0.62, day-trade margin $2,000. 2-lot round-trip friction ≈ 1.12 index pts.
- **Trailing config (boot log):** `EXIT_MODE=trailing — stop_loss=75.0 lock_in=50.0 trail_offset=150.0 hard_ceiling=1000.0 take_profit=150.0 (bracket=STP-only + bar-ratchet)`. `SB_TRIGGER=on — near_ma=[sma-15,sma+35] no_chase=+25 max_bar_age=180s`.
- **New load-bearing facts (this session):**
  - **§0.5.210** ratchet-arming sites (router parent-fill / boot recovery / reconciler force-fill). Evidence: `2812b3fb` armed silently via boot recovery (no `trailing_armed_on_force_fill` line) and ratcheted at 16:23:05Z; `7c62fc53` (router parent-fill) logged `trailing_armed` and ratcheted within a minute.
  - **§0.5.211** deploy-over-open-trailing-position is safe (standalone GTC stop survives restart; boot recovery re-arms). Proven this session.
  - **§0.5.212** `create_lifecycle` TOCTOU double-entry race. Evidence: `c06ed026`+`347d5a12`, identical params 124ms apart, separate brackets; source `state_machine.py:137-142` (probe-then-insert, no lock, no unique index).
  - **§0.5.213** `ocaType=3` benign on a standalone stop (modify succeeded, no Error 10326).

---

## 5. Wrong diagnoses — READ BEFORE YOU DEBUG

1. **"The frozen-ratchet loss in the live log means STABILIZE-5 regressed."** Evidence: a 30,717.50 trade announced a +50 ratchet then exited at base. **Wrong** — that exit was ~8h before STABILIZE-5 deployed; the digests show the running commit was the pre-fix `f89b6e0`/`da57927`. Correct read: it was the *old* bug, captured once more on old code. **Always check the deployed commit at the time of the event.**
2. **(CC) First GATE-ZERO root cause: "the missing `_highest` seed is the bug."** **Incomplete** — `_ratchet_one` defaults `_highest` to entry, so a missing seed alone wouldn't stop it. The real gate is `_active_trailing_lifecycle()` iterating `_by_lifecycle_id`, whose ACTIVE in-memory copy is never written on the force-fill path (stays ENTERING/`entry_price=None`). Corrected by reading the selection path before claiming.
3. **"Deploy must wait until the book is flat."** **Over-conservative** — the boot-recovery path already arms open positions and the standalone GTC stop survives a restart (no naked window). §0.5.202's flat-check is about not un-halting a *halted* bot, not about open positions. Reversed the plan (owner-approved) and the deploy-over-position is what proved the gate.
4. **"The June-1 −$1,854 was the §0.5.205 naked-stop bug."** **Wrong** — the timelines show `exit_oid==stop_oid`, the stop filled, and there was no cancel/orphan event. It was a gap blow-through on working stops. The real June-1 problem is the double-entry (§0.5.212), not a naked stop.

**Lesson for next session:** every wrong turn this session (and the ones avoided) came down to *source-of-truth-before-story*: the commit timeline, the in-memory selection path, the broker order id on the exit event. Confirm the mechanism from broker/DB/source before claiming a cause or a blocker.

---

## 6. Verification block — run this before doing anything

Run as CC VPS inside tmux. (CC VPS Bash discipline: no `$()`, `;`, heredocs, `${}`, `VAR=`.)

**V0 — deployed image is #109 (grep the running container, not the env var)**
```bash
git -C /home/tradeflow/tradeflow fetch origin
git -C /home/tradeflow/tradeflow log --oneline -1 origin/main
docker inspect --format '{{.Created}}' tradeflow-app
docker exec tradeflow-app grep -c trailing_armed_on_force_fill /app/src/execution/reconciler.py
```
Expect: code HEAD `7d82dbd` (or a later docs commit on top); container created `2026-06-04T16:21:59Z` or later; grep ≥1. Grep 0 = #109 not deployed → build + `up -d --force-recreate` (see §0.5.211 — safe over an open position with a broker-resting stop).

**V1 — broker truth (ground truth, §0.5.98)**
```bash
/home/tradeflow/tradeflow/.venv/bin/python /tmp/tf_broker_truth.py
```
Expect (at handoff): MNQM6 pos may be 0 or in-position. **If in a profitable position, the resting STP `auxPrice` should be ratcheted ABOVE base** (entry−75); if it's stuck at base while the trade is up >+50, that's a ratchet-arming regression — investigate per §0.5.210. If `/tmp` was cleared, recreate the read-only probe (clientId 97, `reqPositions` + `reqAllOpenOrders`).

**V2 — both fixes in the running image**
```bash
docker exec tradeflow-app grep -c "evaluate_sb_trigger\|_maybe_enter_on_seanbot" /app/src/orchestrator.py
docker exec tradeflow-app grep -c "ensure_protective_stop\|return parent, None, None" /app/src/execution/bracket.py /app/src/execution/router.py
docker exec tradeflow-app grep -c "RatchetArmer\|trailing_armed_on_force_fill" /app/src/execution/reconciler.py
```
Expect: orchestrator ≥5; bracket ≥3, router ≥5; reconciler ≥3. Any zero = wrong image.

**V3 — boot config**
```bash
docker logs tradeflow-app --since 2026-06-04T16:21:00Z 2>&1 | grep -iE "SB_TRIGGER|EXIT_MODE=|recovery_loaded|seanbot_reconciler: started"
```
Expect: `SB_TRIGGER=on ...`; `EXIT_MODE=trailing ...`; `recovery_loaded — count=N` (N = open lifecycles at boot).

**V4 — the gate proof + the forward proof we still want**
```bash
docker logs tradeflow-app 2>&1 | grep -iE "trailing_stop_ratcheted|trailing_armed_on_force_fill|\[SB-TRIGGER\] (valid|reject)"
```
A `trailing_stop_ratcheted` with `new>old` above base = the money gate (CONFIRMED this session @16:23:05Z for `2812b3fb`). **Still wanted:** a `trailing_armed_on_force_fill` line firing live on a *new* reconciler-force-filled entry — the recovery-path arming was proven, but the #109 code path's own log marker has not yet fired at runtime (the handoff position armed via boot recovery, silently).

**V5 — TOCTOU canary (until §0.5.212 is fixed)**
```bash
/home/tradeflow/tradeflow/.venv/bin/python /tmp/tf_broker_truth.py
```
Confirm the broker contract count == sum of ACTIVE lifecycle `entry_qty` (no extra bracket). If pos > expected (e.g. 4 ct when one 2-lot setup fired), the double-entry race may have triggered — capture the lifecycle pair and STOP before any concurrency work.

---

## 7. Pending work queue

Priority depends on V1/V4/V5 state, not list order.

### Forward proof of the #109 path (low effort — watch only)
The money gate is proven via the boot-recovery arming. The incremental confirmation we still want is the `trailing_armed_on_force_fill` log firing live on the next router-missed entry that gets force-filled under the new code. No code; just watch V4.

### GATE-1 — fix the `create_lifecycle` TOCTOU double-entry race (§0.5.212) — BLOCKS CONCURRENCY
Durable fix: a Postgres **partial unique index** `UNIQUE (symbol, strategy) WHERE state <> 'CLOSED'` (race-proof at the DB; the second insert 409s → caught as `InvariantViolationError`), optionally plus an `asyncio.Lock` around the entry critical section. **AUDIT.** One change. Brief is in §13 — hand it to CC VPS first thing after pre-flight. Concurrency cannot start until this lands.

### CONCURRENCY — owner's explicit #1, once GATE-1 lands
Allow TF to hold multiple simultaneous positions like SeanBot (the ~56% of the entry gap that is no-stack). **AUDIT / through the promotion gate.** Design questions to settle: max concurrent positions; per-position sizing + aggregate contract/margin cap; how the 10-loss kill-switch counts across concurrent lifecycles; per-lifecycle standalone-stop ownership (each owns its `parentId=0` STP + NEVER-ORPHAN + its own ratchet registration). On $1M paper it is proportionally low-risk. **Do not design it until the double-entry race is fixed** — concurrency adds exactly the concurrent entry paths that trip §0.5.212.

### PARKED — SeanBot exit / stop-move signals as validity-gated hints
Same pattern as the entry trigger, applied to SeanBot's EXIT / STOP-MOVED notifications. Now unblocked on the exit side (gate proven), but lower priority than GATE-1 + concurrency.

### MEASURE — scoreboard trust
SeanBot `est.` days are unverified Telegram reconstructions; the −$8,301 headline rests mostly on pre-live-TF days + est. days (~$1,405 is a clean both-live head-to-head). Also fix the wrong "TF newly live 2026-06-01" caption (first trade was 05-29). Lower lane; after the STABILIZE items.

### Operational debt
- `/tmp/tf_broker_truth.py`, `/tmp/wait_flat.py`, and the forensic scripts are ephemeral — recreate if `/tmp` was cleared (all read-only, reconstructable from §6/§4).
- `TRADEFLOW_COMMIT=unknown` (not baked) — cosmetic; verify code by grepping the container.

---

## 8. Test safety — why we belabor this

Carry-forward failure modes the suite must keep guarding: tests green against a fictional schema (mock real column names — `lifecycle_id`, `emitted_at`); `side_effect` list count mismatch → silent `StopIteration`; mocking the raw library when prod uses a wrapper (custom httpx Supabase stub, never `supabase-py`); shared `MagicMock()` leaking between tests; `RISK` is a **frozen dataclass** — patch `src.<module>.RISK` (the module symbol), not the instance attribute. #109's +3 tests cover trailing-arms-on-force-fill, fixed-mode-does-not-arm, no-router-is-safe. Keep the `code-pr-brief` guardrails on every PR.

---

## 9. Pitfalls from prior sessions

- Docker `healthy` ≠ broker API healthy (§0.5.151 — the 85-hour silent outage).
- "Stop is fine because the code doesn't set a group" — wrong (§0.5.205).
- "The ratchet is armed because the standalone stop is placed" — wrong (§0.5.210, this session): placement ≠ arming.
- Handoff numbers drift — **if a claim is quantitative (P&L, peak excursion, AGREE rate, contract count), re-verify it from source.**
- `gh pr merge --squash` on a protected branch may need `--admin` (§0.5.214); `gh pr checks --watch` exits prematurely — poll.
- SSH drop kills in-flight work unless inside tmux — always `tmux new -A -s tf1` first.

**Next session rule: if a claim is quantitative, re-verify it.**

---

## 10. Session discipline lesson (2026-06-04) — incl. orchestrator's logged comments

**Meta-pattern:** the highest-value work this session was, again, *diagnostic confirmation from the source of truth* — the gate verdict (broker + market-data), the numbers reconciliation (Supabase + IBKR), the June-1 forensic (lifecycle_events + source), and the TOCTOU find (reading the entry paths). Every cause was confirmed from ground truth, not asserted from a plausible story.

**Orchestrator's own comments, logged at the owner's request:**
- I (chat) nearly let the OLD-code frozen-ratchet loss read as a STABILIZE-5 regression. The commit timeline caught it. Logging this so the next session reflexively checks the deployed commit *at the time of an event* before blaming the newest fix.
- I repeatedly framed the deploy as "must wait for a flat book." CC found that was over-conservative — the boot-recovery path arms open positions and the broker-resting standalone stop means no naked window. The deploy-over-position is what actually proved the gate. Owned; codified as §0.5.211. I should not over-apply a halt-state rule (§0.5.202) to open positions.
- I kept the right discipline on lane order: when the ledger surfaced the TOCTOU race, I queued it (didn't let CC patch it the same session) and kept GATE-ZERO deploy + proof first (§0.5.208). One change at a time held.
- Honest standing note (carried from v20, still true): this is **paper**, and the path is paper-validation → live. The exit is now proven, but the edge question is still open — TF's −$3,894 is mostly two mechanical defects (June-1 gap double-entry; ratchet give-backs now fixed) plus real wins. We need a **clean both-live sample on fixed code** before "is the edge real vs SeanBot" is answerable. That conversation is closer, not here yet.

**Enforcement rules for next session:**
1. Confirm the mechanism from broker/DB/source before building *or claiming* anything — including blockers.
2. One change at a time; GATE-1 (double-entry race) must land before concurrency (§0.5.208/§0.5.212).
3. Always launch CC VPS inside tmux; `claude --resume` after any disconnect.

---

## 11. Logging verbosity — what to demand from any new code

Every entry/exit/stop/ratchet action logs `[COMPONENT] symbol: action — reason`; every lifecycle state transition logs old→new at INFO; every swallowed exception logs type + context; dedup/select-one logs which row won; the ratchet logs `trailing_stop_ratcheted: stop_id old new highest entry lifecycle_id`; arming logs `trailing_armed` / `trailing_armed_on_force_fill`. Concurrency work MUST log, per lifecycle, which position a fill/stop/cancel/ratchet belongs to.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill. Entry/exit/strategy/risk/concurrency changes are **AUDIT** unless the owner explicitly waives the pause.

---

## 13. Current PR brief in flight — hand this to Claude Code as-is (GATE-1)

~~~
# TradeFlow — Claude Code PR Prompt: PR (next) — Make create_lifecycle race-proof (durable single-open-position invariant)

## Role
You are a senior Python developer on TradeFlow, an autonomous MNQ futures bot running on an IBKR paper account (~$1M NetLiq) that places real bracket orders. You write clean, tested, production-grade code, never modify files you weren't asked to, and study existing patterns before writing. Bugs here place duplicate broker orders. You are verbose in logging: `[COMPONENT] symbol: action — reason`. You second-guess your own assumptions and verify against the actual file before claiming. The previous session found this bug by reading source, not greps — keep that standard.

## Context
The "≤1 non-CLOSED lifecycle per (symbol, strategy)" invariant is enforced ONLY by a probe-before-insert in `StateMachine.create_lifecycle` (`src/execution/state_machine.py:137-142`: `await select_non_closed(...)` then `await insert_lifecycle(...)`, comment "enforced in code, not DB"). There is no lock and no DB uniqueness. Two independent async tasks reach it — the strategy bar-eval (main `run()` loop) and the SeanBot-trigger task (`orchestrator.py:1321` `create_task` → `_maybe_enter_on_seanbot` → `place_entry` → `create_lifecycle`). They interleave at the `await` boundary → both pass the probe → both insert → a double bracket (4 contracts). Proven on 2026-06-01: lifecycles `c06ed026` and `347d5a12`, identical entry 30559.25, 124ms apart, separate brackets, −$1,854 combined. This blocks concurrency.

## 🏗️ System Architecture & Recent Learnings
- Container: `tradeflow-app` (Python 3.11, async).
- DB: Supabase via a custom httpx wrapper (`SupabaseClient`) — NOT `supabase-py`. Env: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
- Logging: `docker logs tradeflow-app`; module-level LOGGER.

### Key Architecture Constraints
- Runtime: `create_lifecycle` is async; `select`/`insert` go through the `SupabaseClient` wrapper.
- Shell/tests: pytest in the repo `.venv`; CC VPS Bash discipline (no heredocs/`;`/`$()`/`${}`/`VAR=`).
- Schema: `lifecycles` keyed by `lifecycle_id`; `state` values include ENTERING/ACTIVE/CLOSED; a "non-CLOSED" row is `state <> 'CLOSED'`.
- Scope boundary: fix ONLY the race in lifecycle creation. Do NOT touch ratchet/exit logic (#109 just shipped) or concurrency behavior.
- Design decision (recommend default): **(A) durable DB partial unique index** `UNIQUE (symbol, strategy) WHERE state <> 'CLOSED'` + handle the insert 409 as `InvariantViolationError` (race-proof, survives restarts) — RECOMMENDED; optionally **(B)** add an `asyncio.Lock` around the create critical section as defense-in-depth. Do NOT rely on (B) alone (in-process lock doesn't survive multi-task ordering edge cases or restarts).

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints
Files you WILL modify (EXACTLY 2-3):
- `src/execution/state_machine.py` (catch the unique-violation on insert → `InvariantViolationError`; optional lock)
- a new migration under the project's migrations path (the partial unique index) — match how prior additive migrations were added (see the v20 note: PR 50 was an additive migration)
- `tests/test_execution_state_machine.py` (or the matching existing test file)

Files you MUST NOT modify: `src/execution/reconciler.py`, `src/execution/router.py`, `src/execution/bracket.py`, `src/orchestrator.py` (entry dispatch), strategy code.

Verification gates:
- `git diff main -- src/execution/reconciler.py src/execution/router.py src/orchestrator.py` → MUST be empty
- `git diff main --stat` → EXACTLY the 2-3 files above

### 2. Code Quality
black + ruff clean; type hints preserved; no signature changes to public methods; one import per line; logging format `[COMPONENT] symbol: action — reason`.

### 3. Safety
- All pre-existing tests pass (suite was 658 green after #109). Do NOT "fix" any pre-existing failure — document it.
- No unexpected broker calls. The index migration must be additive and idempotent.
- If you find an adjacent bug, DOCUMENT it, do NOT fix it.

## 🧩 Current Mission: make the single-open-position invariant race-proof and durable

### Task A: Audit
Read `state_machine.py:120-160` and the two entry dispatch paths (`orchestrator.py` strategy eval + the `seanbot_task` handler). Confirm in the PR description: where exactly the `await` interleaving window is, and whether `create_lifecycle` is the only insertion site for `lifecycles` (grep for other inserters). 3-5 line finding.

### Task B: Implement
Add the partial unique index migration; in `create_lifecycle`, wrap the insert so a unique-violation (409) is caught and raised as `InvariantViolationError` (same exception the caller already handles for the in-code probe), with log `[STATE] {symbol}: create rejected — concurrent non-CLOSED lifecycle exists (db-unique)`. Keep the existing probe as a fast-path. Optionally add an `asyncio.Lock`.

### Task C: Add tests
Simulate two concurrent `create_lifecycle` calls for the same (symbol, strategy); assert exactly one succeeds and the second raises `InvariantViolationError`; assert no second bracket is placed. TEST SAFETY: fresh `MagicMock()` per test; mock the `SupabaseClient` wrapper (not the raw chain); set `select.return_value`/`insert` behavior explicitly; no `side_effect` list without a count comment; match the async test decorator used by neighbors in the file.

### Task D: Verify completeness
`grep -rn "insert_lifecycle\|\.table(.lifecycles.)" src/` → every insertion site classified; confirm `create_lifecycle` is the only one (or cover the others).

### Task E: Out-of-scope investigation (~10 min, document only)
Whether the router's ~75% parent-fill miss rate (which makes the reconciler force-fill path the common one) has a separate root cause worth a future MEASURE PR. Do NOT fix.

### Task F: Post-merge smoke test
Owner/CC runs after merge: confirm the index exists; run the two-concurrent-create test in-container; watch live logs for `create rejected — ... (db-unique)` if it ever fires; confirm broker contract count never exceeds expected. STOP if a 4-contract position appears.

## 📤 Expected Output
Files modified (EXACTLY 2-3); diff stat; PR description with Task A finding, Task D grep classification, Task E paragraph, local + full test tails, protected-file empty diffs, explicit "This PR does NOT touch ratchet/exit/concurrency logic", and a "What I got wrong" section.

## 🔍 Pre-Push Checklist
Code quality (black/ruff/imports/signatures); TEST SAFETY GUARDRAILS (fresh mocks, wrapper-level mocking, explicit returns, async decorator matches neighbor, assertions by arg not index); production safety (empty protected diffs, Task D complete, smoke test included, "What I got wrong" present).

## ⚠️ Known Gotchas (carry forward, never shrink)
1. `SUPABASE_SERVICE_ROLE_KEY` (not `_KEY`).
2. DB wrapper is a custom httpx `SupabaseClient`, not `supabase-py`.
3. Docker restart ≠ rebuild — owner builds + `up -d --force-recreate` after merge (safe over an open position, §0.5.211).
4. `RISK` is a frozen dataclass — patch the module symbol.
5. `lifecycle_events.emitted_at` (not `created_at`); `lifecycles` keyed by `lifecycle_id`.
6. AUDIT change — green checks then hold for the owner's one-word merge; merge docs/protected branches may need `--admin`.
7. Pre-existing test baseline is 658 green (post-#109). Do not "fix" unrelated reds.
~~~

---

## 14. Canonical references (in order of authority)

1. **`CLAUDE.md`** on `main` at `7d82dbd` — §0.5.x registry, autonomy contract, system rules.
2. **Source on `main`** at `7d82dbd` — what actually runs (verify in-container per §6/V2).
3. **Production Supabase** (service role, read-only) — `lifecycles`, `lifecycle_events`, `signal_reconciliations`, `strategy_decisions`.
4. **IBKR paper (DUQ331660)** via `ib_async`, clientId 97 read-only — broker state truth.
5. **This handoff (v22)** — session context, not long-term authority.
6. **v20/v21 and earlier** — historical; ignore any claim that contradicts 1–4.

---

## 15. First 15 minutes of the next session

1. Read §§0.5, 1, 2, 4, 5, 10. §0.5.210–212 and §5 are the most important.
2. `tmux new -A -s tf1`, then pre-flight: `git -C ~/tradeflow fetch && git -C ~/tradeflow pull --ff-only origin main && ls -t docs/handoffs/ | head -3`, then run the §6 block. Confirm deployed `7d82dbd`+, `trailing_armed_on_force_fill` in the container, and read live broker/position state (the bot was IN POSITION, not flat).
3. If `2812b3fb` (or any position) is up >+50 and its STP is stuck at base → ratchet-arming regression, investigate per §0.5.210 before anything else.
4. Hand the §13 GATE-1 brief to CC VPS (fix the `create_lifecycle` double-entry race). AUDIT — green checks, then the owner's one-word merge; deploy is safe over an open position (§0.5.211).
5. After GATE-1 lands + smoke-tests: open the **concurrency** design with the owner (his #1).
6. After any merge, draft a VPS smoke-test runbook (`vps-smoke-test-runbook` skill).

---

## 16. How to publish this handoff

**Path A — owner self-serve (single block, run from your Mac; heredoc OK in your own shell).** Uses `--admin` because the protected branch blocks a plain squash-merge (§0.5.214), and no `--watch`:

```bash
scp ~/Downloads/HANDOFF_v22.md tradeflow:/tmp/HANDOFF_v22.md
ssh tradeflow 'bash -s' <<'PUBLISH_EOF'
set -euo pipefail
cd /home/tradeflow/tradeflow
git fetch origin
git checkout -B docs/handoff-v22 origin/main
mkdir -p docs/handoffs
cp /tmp/HANDOFF_v22.md docs/handoffs/HANDOFF_v22.md
git add docs/handoffs/HANDOFF_v22.md
git commit -m "docs: add v22 handoff (GATE-ZERO #109 deployed+verified; double-entry race is next; concurrency after)"
git push -u origin docs/handoff-v22 --force-with-lease
gh pr create --base main --head docs/handoff-v22 --title "docs: add v22 handoff" --body "Session handoff v22. Docs-only. #109 deployed+verified live; create_lifecycle double-entry race queued (GATE-1); concurrency after." || true
sleep 5
gh pr merge docs/handoff-v22 --squash --admin --delete-branch || { sleep 10; gh pr merge docs/handoff-v22 --squash --admin --delete-branch; }
git checkout main
git pull --ff-only origin main
echo "DONE: v22 merged"
git log --oneline -1 origin/main
PUBLISH_EOF
```

**Path B — CC VPS brief (if you'd rather delegate):** save the content to `/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v22.md`, branch off `origin/main`, `git commit -F`, push, `gh pr create --body-file`, then `gh pr merge <branch> --squash --admin --delete-branch` (NOT `--watch`), resync main.

The handoff exists only once committed to `origin/main`.

---

*End of handoff v22. Target lifespan: until the `create_lifecycle` double-entry race (GATE-1) is fixed AND concurrency is designed + shipped, then rely on `CLAUDE.md` + v23. The money gate is PROVEN; the next gate is durable single-position safety before concurrency.*
