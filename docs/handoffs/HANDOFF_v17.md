# TradeFlow — Handoff v17 (hardened + resumed live; HALTED on kill-switch evaluator_error — diagnose FIRST)

*Handoff from end of Session 17 (2026-06-02 ~14:10 UTC). Overnight the bot was hardened with five merged changes and RESUMED live (paper) trading on the new tiered kill switch. It ran ~12 hours, took real trades, and the native bracket + connection self-heal both proved themselves live. **As of the last Telegram event (08:02 CR / ~14:02 UTC) the bot raised `kill_switch:evaluator_error` and is HALTED** — the evaluator threw and fail-safe-halted (most likely tied to the `KILL_SWITCH_ALLOCATION_USD` change dispatched at end of session). Halted = safe = flat-ish, but a *persistent* evaluator error means a permanent halt until diagnosed. This doc captures everything a new chat needs to pick up cleanly.*

> **VERSION NOTE:** Numbered **v17** (Session 17). The §16 publish step instructs VPS CC to confirm the latest handoff in `docs/handoffs/` and use `latest+1` if v17 is wrong — self-correcting, no operator action.

---

## 0. How to use this doc

Read §§1–6 first (state-of-system). §§7–13 are reference. §14 is the authority order. Canonical code truth: `main` at `7db2157` or later.

**Do not trust this doc alone. Run §6 before any code. CRITICAL FIRST ACTION: the bot raised `kill_switch:evaluator_error` and is halted — diagnose *why the evaluator threw* (V0) before clearing or changing anything. A recurring evaluator error will re-halt on every poll.**

---

## 0.5 Standing rules (permanent — do not remove)

**Copy-paste instruction style.** Every recommended action is a self-contained, paste-ready bash block, env sourced in-block, expected output described below it, decision tree if branches matter. No "you might want to…".

**Learning-delivery discipline.** Every new fact (bug pattern, corrected assumption, env fact, finding) is surfaced immediately as a paste-ready snippet for the handoff queue, not end-of-session.

**Read before diagnosing.** For complex state bugs, read the full startup log + 3–5 full cycle narratives before proposing root cause. `grep | wc -l` diagnosis is the #1 cause of wrong calls.

**Verify severity against the source of truth.** Before escalating urgency, hit the live broker/DB, not aggregated metrics.

**Always draft a VPS smoke-test runbook after a PR merge** unless told otherwise. The owner does not run smoke tests by hand.

### TradeFlow project standing rules (carried forward — verbatim, append-only)
- **§0.5.97 — probe external specs before baking into briefs.** Broker contracts, fees, schema, library API surfaces, and a benchmark's own config are verified against source before a brief.
- **§0.5.98 — broker/DB state is ground truth, not internal assumptions.** Position/fills/P&L/loss-counts source from broker API / `lifecycles`, never an in-memory counter (resets on the frequent restarts). *Reaffirmed hard this session: the v16 "FLAT" was stale; the broker showed a naked 2-ct long.*
- **§0.5.151 — Docker healthy ≠ broker API healthy.** A green container can mask a dead IB Gateway session.
- **§0.5.184 — AUDIT gate.** Order-execution / strategy / kill-switch / secrets / broker-state changes PAUSE for the operator's single-word `merge` after the diff is posted. *(Suspended this session by explicit operator instruction for a paper-account fast-forward — see §10. REINSTATE before any live-money operation.)*
- **§0.5.186 / .187 — VPS CC command discipline.** No sub-agents/sweeps; no heredocs, `$(...)`, `${VAR}`, `;`, `cd X &&`. Python via `Write` to `/tmp/scriptN.py` then `python3 …`; commit `-F /tmp/commitmsg.txt`; PR body `--body-file`. Branch off `origin/main` FIRST. Bare `git push` is gated — use `git -C ~/tradeflow push origin <branch>`.
- **§0.5.188 — never ship NEW trading-path bugs into unattended windows.** *(Also suspended this session for the paper fast-forward; reinstate for live.)*
- **§0.5.189 (NEW) — `--dangerously-skip-permissions` opted OUT. Never suggest it.**
- **§0.5.190 (NEW) — IBKR rejects a `TRAIL` order as a bracket CHILD of a `MKT` parent (Error 328: "Trailing stop orders can be attached to limit or stop-limit orders only"). A native trailing stop must be placed STANDALONE post-fill, or done in-app bar-by-bar. Confirmed live 2026-06-02 02:21Z.**
- **§0.5.191 (NEW) — seed the strategy buffer BEFORE starting the live bar subscription.** `seed_bars()` only fills an EMPTY buffer; a live bar arriving during the ~1.5 s historical fetch poisons the seed → cold SMA on boot. Order is load-bearing.
- **§0.5.192 (NEW) — `IB.positions()` contracts omit `exchange` → Error 321 on placement. Set `contract.exchange="CME"` before placing on a position-derived contract.**
- **§0.5.193 (NEW) — warmup historical fetch uses `"5 D"` (not `"1 D"`): `"1 D"` returns only the current session after the 18:00 ET reopen (~99 bars, 1 short of SMA100). `"5 D"` spans the weekend. `validate_seed >= 100` is the backstop. A Sunday-reopen edge may still fall short → escalate the window if ever observed.**
- **§0.5.194 (NEW) — boot recovery must synthesise full CLOSED fields for an orphan ENTERING lifecycle (entry never filled), or the CLOSED invariant fails → `InvariantViolationError` → crash-loop. `_synth_closed_fields` does this (boot sibling of `OrderRouter._close_pre_active`).**
- **Batch-autonomy default.** Batch follow-ups into ONE VPS CC run; pause ONLY for AUDIT / strategy-param calls / external blockers (per the gate's current state).
- **Maximize autonomy / minimize operator UI.** Prefer one ssh command over many operator actions. Handoff publish via VPS CC + `gh`, no operator browser.

---

## 1. Where we are (as of handoff, 2026-06-02 ~14:10 UTC)

### Live production state
- `tradeflow-app`: deployed commit **`7db2157`** (last code HEAD). A post-session `.env` change to set `KILL_SWITCH_ALLOCATION_USD` may have been dispatched — **unconfirmed; verify in §6 V0/V2.**
- **HALT STATE: HALTED — `kill_switch:evaluator_error` raised 08:02 CR / ~14:02 UTC (Telegram).** The kill-switch evaluator threw; fail-safe halted. **This is the critical first thing to diagnose.** Likely cause: the allocation-arming change introduced a throw in the drawdown path, OR a transient DB/broker error during evaluation. Do NOT assume — read the traceback (V0).
- Position: last known **FLAT** after the 07:35 CR stop-out of `f55d5e6f`; a new entry `7b01c277` placed 07:47 CR — **re-verify position from broker (V3); the 08:02 halt may have left it open or flat.**
- Realized P&L: the 09:00 UTC daily report showed −$2.48 (2 closed) *before* the day's later trades; `f55d5e6f` then stopped out at **−$303.48** (clean 75 pt stop). Re-query the true day total (V1) — do not trust this number.
- Dashboard live behind HTTP Basic auth: `/`, `/trades`, `/pnl`, `/scoreboard`, `/divergence`.

### What just shipped this session (11 PRs, #80–#88 + 3 follow-ups; plus 1 restatement)
- **Manual op (no PR):** re-armed the naked `3f0bb719` stop → it exited at market → first live exercise of the kill switch + cancel-fix; then restated.
- **`3f0bb719` P&L restatement** (one-shot, no PR): `pnl_net −2.48 → −437.72`, `exit_price → 30459.75`. Day total −1,875.92 → −2,311.16. *(Do not re-derive — §4.)*
- **#80 (AUDIT)** `6b59220` — reconciler self-heals a missing bracket leg for an ACTIVE-with-position lifecycle (the −$419 naked-stop bug class). Idempotent, OCA-linked, fail-safe. **Stays as the backstop.**
- **#81 (AUDIT)** `4599dc4` — warmup history fetch `"1 D" → "5 D"` (off-by-one after the reopen; §0.5.193).
- **#82 (AUDIT, "PR B")** `51230f4` — native server-side OCA entry bracket (parent + fixed-STP child + TP child, `parentId`-linked, `ocaType=1`). The STP is now a native bracket leg that **survives a disconnect/redeploy** — the root-cause fix for the naked-stop incident. Trailing-TP code added but **see §5 — it doesn't work (Error 328).**
- **#83 (AUDIT, "PR C")** `76eacf6` — bar-gap detect + re-seed after reconnect (§0.5.190-adjacent feed correctness). Never computes SMA across a gap.
- **#84 (AUDIT, "PR D")** `b5b4568` — bounded stale-feed self-heal: socket-alive-but-feed-dead → one rate-limited resubscribe. **Fired live and recovered (§1 wins).**
- **#85 (AUDIT, "PR A")** `b51d74f` — tiered kill switch (`6=notify`, `10=halt`, `33%`-of-allocation drawdown), all env-tunable, epoch=deploy-time. **This resumed the bot.**
- **#86 (fix-on-fly)** `8130078` — seed BEFORE subscribe (§0.5.191).
- **#87 (fix-on-fly)** `8e0bf5a` — `EXIT_MODE` default `trailing → fixed` (Error 328; §0.5.190).
- **#88 (fix-on-fly)** `7db2157` (= HEAD) — boot recovery synth-closes orphan lifecycles (§0.5.194); resolved an 11-restart crash loop.

### What we discovered/proved live this session (evidence)
- **The native OCA bracket WORKS live.** `f55d5e6f` entered 03:30 CR, stopped out at `exit_price=30463.75 pnl_net=−303.48 exit_reason=STOP` (07:35 CR) — a clean 75 pt stop, broker-resident, exactly as designed. **First clean completed trade on the new code.**
- **PR D connection self-heal FIRED LIVE and recovered.** Telegram: `reconnect_recovered elapsed_sec=0.4` and `=22.3`; `bar_sub_resubscribed_after_farm_flap` ×3 (10:01/10:12/10:18 PM CR); `ALERT IB API unreachable TimeoutError attempt 3/3` → `RECOVERED: IB API reachable after auto-heal`. The connection hardening is no longer just unit-proven.
- **PR A NOTIFY tier works.** `kill_switch_warning: reason=consecutive_losses_warning detail=6 consecutive losing trades (warn 6, halt 10)` — alert, no pause.
- **TF agreed with SeanBot on an entry:** `seanbot_reconciliation … class=AGREE_ENTER` (03:41 CR, @30542.5). TF and SeanBot are converging on entries.
- **Daily report 09:00 UTC: 7/7 green**, orphan canary 0 resting / pos=FLAT, app restarts since boot 0.
- **Trailing-TP edge is real and we're leaving it on the table** — see §4 / §5 / §7.

### What the operator (Ohad) is doing
Hands-off PM / orchestrator. Single-word approvals (`merge`, `go`, `5D`, `stop`). This session he made an explicit, informed call to **suspend the AUDIT gate for a paper-account fast-forward** — VPS CC built + merged + deployed + self-corrected all five PRs unattended while he rested (§10). He is now reviewing the overnight Telegram logs, has dispatched a `KILL_SWITCH_ALLOCATION_USD` arming command to VPS CC (output not yet seen), and is closing this session to open a fresh chat. **Next operator action:** publish this handoff via §16, paste the VPS CC output back, then open the new chat on the §15 plan.

---

## 2. The session's work thread

1. **Naked-position rescue.** v16 said FLAT; §6 probe found an ACTIVE 2-ct long (`3f0bb719`) with **no broker stop** (only a target). Re-armed the stop (operator-approved one-shot) → it exited at market (price already below entry−75) → 6th consecutive loss → kill switch's first live trip + flatten. Restated the foreign-client-exit P&L.
2. **Root-cause: restart-recovery stop gap.** The 21:10 (v16) redeploy dropped the GTC stop while the GTC target survived; the reconciler NOOP'd an ACTIVE-with-position. Shipped **#80** (self-heal).
3. **Warmup off-by-one.** A redeploy right after the 18:00 ET reopen seeded only 99 bars → cold SMA. Shipped **#81** (`"5 D"`).
4. **Paper fast-forward (operator-directed).** Operator overruled the AUDIT gate for paper: VPS CC built + merged + deployed B→C→D→A autonomously, then fixed three live-surfaced bugs on the fly (#86 cold-SMA race, #87 Error 328, #88 orphan crash loop).
5. **Resume.** PR A's new tiered logic let the 6-loss streak through (6 < 10) → bot resumed. First resume booted cold (#86 race), then took a trade whose **trailing** exit was IBKR-rejected (Error 328 → #87 fixed-mode), whose **orphan** then crash-looped boot (#88).
6. **~12 h live run.** Fixed-mode trading; native bracket proved out (`f55d5e6f` clean 75 pt stop); PR D reconnect/farm-flap self-heal fired and recovered repeatedly; TF/SeanBot AGREE_ENTER once.
7. **Allocation arming + evaluator_error halt.** Operator dispatched `KILL_SWITCH_ALLOCATION_USD`; at 08:02 CR the kill-switch evaluator threw → fail-safe halt. **Open — diagnose next session.**

Closed rabbit holes: the stop drop was a *client-simulated-stop-cancelled-on-disconnect* asymmetry (native LIMIT survived, simulated STP didn't) — the native-bracket fix (#82) addresses it at the source; the self-heal (#80) is the backstop.

---

## 3. What the system is actually made of

**Single source of truth:** none — this handoff + `main @ 7db2157` are the best system doc.

- **Prod code paths:** `src/orchestrator.py` (boot, bar loop, **seed-before-subscribe** `_startup`, bar-gap detect `_handle_post_resubscribe_gap`/`_reseed_strategy_after_gap`, stale-feed self-heal `_maybe_heal_stale_feed`, watchdog, recovery `_broker_field_updates_for`/`_synth_closed_fields`), `src/strategy.py` (`Sma100BounceStrategy`, `seed_bars`, `invalidate`, `last_bar_time`), `src/execution/bracket.py` (`build_entry_oca_bracket` NEW, `build_bracket`, `build_protective_stop`), `src/execution/router.py` (`place_entry` places the native OCA bracket; `_cancel_sibling_legs` backstop), `src/execution/reconciler.py` (`_heal_missing_legs`), `src/execution/kill_switch.py` (tiered `evaluate_triggers` + `KillSwitch`), `config/risk_params.py` (env-driven knobs), `dashboard/` (5 views).
- **Config knobs (env, `config/risk_params.py`):** `EXIT_MODE` (default `fixed`), `TRAIL_OFFSET` (150), `BAR_GAP_MAX_TOLERANCE_BARS` (1), `KILL_SWITCH_ENABLED`, `KILL_SWITCH_WARN_CONSEC_LOSSES` (6), `KILL_SWITCH_HALT_CONSEC_LOSSES` (10), `KILL_SWITCH_ALLOCATION_USD` (unset → drawdown brake inert), `KILL_SWITCH_MAX_DRAWDOWN_PCT` (33), `KILL_SWITCH_PNL_EPOCH` (deploy time).
- **Dead/vestigial:** `KillSwitch.equity_base` is now vestigial (tiers measure against allocation, not net-liq) — tidy-up queued. `build_bracket` retained only for the reconciler heal path. The old `max_daily_dd_pct`/`max_weekly_dd_pct` fields are superseded by the tiered drawdown.
- **Halt mechanism (reuse, don't rebuild):** `Orchestrator.raise_halt/clear_halt/is_halted`; manual reset via reconciler `_poll_halt_ack` (Supabase `halt_acks` from Telegram `/ack`, or `touch /tmp/halt_clear`).

---

## 4. Verified facts (2026-06-02) — DO NOT challenge unless schema/code migrates

- **MNQ spec (§0.5.97-verified):** TICK 0.25, MULTIPLIER $2/pt, MARGIN $2000. Quarterly Mar/Jun/Sep/Dec, 3rd-Fri expiry. Live contract `MNQM6` (conId 770561201, exp 2026-06-18). **Commission: the recorded round-trip on 2 MNQ was $2.48 (≈ $0.62/side/contract ⇒ ~$1.24 RT/contract), NOT the $0.62 RT total previously written. Re-verify before using in PF math.**
- **Native OCA bracket is the entry path** (`build_entry_oca_bracket`): parent MKT + fixed STP child + TP child, both children `parentId`-linked + `ocaType=1`, GTC, `outsideRth=True`, transmit chains on the last leg. STP is a native bracket leg → survives disconnect/redeploy. Proven live (`f55d5e6f` stopped at −75 pt).
- **`EXIT_MODE=fixed` is the live default.** `trailing` is NOT currently usable (Error 328, §0.5.190). Fixed mode = {STP @ entry−75, LMT TP @ entry+150}, both OCA-linked.
- **Kill switch is tiered (PR A):** `6=NOTIFY` (alert only), `10=HALT`, `33%`-of-`ALLOCATION_USD` realized drawdown (from epoch) `=HALT`. Drawdown brake INERT until `KILL_SWITCH_ALLOCATION_USD` set. Notifications idempotent (once per crossing). Evaluator error → fail-safe halt.
- **SeanBot validated V3 exit (the edge we're missing):** a **trailing stop** that starts at entry−75 and ratchets UP behind the high-water mark, exiting on pullback. Demonstrated live this session: SeanBot rode one trend from entry 30,373.25 to a `trail stop +139 pt → +$555.38` exit (plus smaller trail wins +$183/+$193/+$180). TF's fixed +150 would have capped that ~+$300. **The trailing redesign is the single highest-value pending item (§7, §13a).**

**New load-bearing fact (this session):** the consecutive-loss streak is currently **reset by a synthetic 0-pnl close** (orphan/manual): a `pnl_net=0.0` row sits newest and isn't `< 0`, so it breaks the streak like a win. After the #88 orphan closed at 0, the kill-switch streak read 0. A non-trade should not mask a real loss streak — **fix queued (§7).**

---

## 5. Wrong diagnoses / live surprises — READ BEFORE YOU DEBUG

1. **"Trailing TP works as a native bracket child."** *Evidence that misled:* it built cleanly, passed all unit tests, CI green. *Why wrong:* IBKR rejects a `TRAIL` as a child of a `MKT` parent — **Error 328**, only surfaced on the first LIVE placement. The transmit-chain fail-safe held (nothing transmitted, no naked position). *Correct:* trailing must be a **standalone post-fill** order or in-app bar-trail (§0.5.190, §13a). **Lesson: IBKR order-type-as-bracket-child constraints are NOT unit-testable — they need a live placement probe before shipping.**
2. **Cold-SMA on resume.** *Evidence:* `get_historical_bars` returned 5762 bars but `seed_bars` seeded 1. *Why:* subscribe-before-seed — a live bar landed in the buffer during the fetch, and `seed_bars` only fills an empty buffer. *Correct:* seed → subscribe (§0.5.191, #86).
3. **Orphan crash loop.** *Evidence:* `RestartCount=11`, container unhealthy. *Why:* the Error-328 entry left an ENTERING orphan; boot recovery's →CLOSED transition lacked synth fields → `InvariantViolationError` every boot. *Correct:* synth full CLOSED fields (§0.5.194, #88).

**Lesson for next session:** anything that depends on the broker's *acceptance* of an order shape (order types, bracket-child rules, OCA, exchange field) can pass tests and fail live — probe with a real/dry placement before declaring it done.

---

## 6. Verification block — run this before doing anything

VPS CC runs these (operator hands-off). All assume `~/tradeflow`. **V0 first.**

**V0 — WHY did the kill switch evaluator throw? (CHECK FIRST — bot is halted on evaluator_error)**
```bash
docker logs tradeflow-app 2>&1 | grep -E "evaluator_error|Traceback|\[KILL\]|halt_raised|kill_switch_tripped" | tail -40
```
Expect to find the traceback that preceded `halt_raised: kill_switch:evaluator_error`. **If it's a recurring throw** (every 30 s poll) → it will re-halt forever; fix the throw before clearing. **If it was a one-off transient** (e.g. a single Supabase/broker timeout) → confirm later polls are clean, then the halt can be cleared. Do NOT clear without reading this.

**V1 — current position + loss streak + day P&L (broker/DB truth)**
```bash
docker logs tradeflow-app 2>&1 | grep -E "exit_filled|entry_placed|pnl_net|lifecycle.*CLOSED|position=" | tail -12
```
Then a read-only DB SELECT of the last-6 CLOSED `pnl_net` + any non-CLOSED lifecycle (write `/tmp/v1.py`, run with `~/tradeflow/.venv/bin/python`). Re-derive the streak and day total from `lifecycles` — do NOT trust this doc's numbers (§9).

**V2 — deployed code + env truth**
```bash
docker inspect tradeflow-app --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -iE "COMMIT|KILL_SWITCH|EXIT_MODE"
git -C ~/tradeflow rev-parse origin/main
```
Expect container `TRADEFLOW_COMMIT=7db2157…` (or later). Note whether `KILL_SWITCH_ALLOCATION_USD` is set and to what — this is the prime suspect for V0.

**V3 — broker truth (position + warmup + bracket)**
```bash
docker logs tradeflow-app 2>&1 | grep -E "WARMUP-ENABLE|indicators_ready|place_parent|place_stop_child|FLAT|naked" | tail -8
```
Confirm warm (`indicators_ready=True`, ≥100 bars) and the real broker position. Then a read-only cross-client probe (clientId 97) of `IB.positions()` + open orders to confirm FLAT-or-bracketed, never naked (§0.5.98 — broker wins).

**V4 — connection self-heal + feed health (PR C/D proof continues)**
```bash
docker logs tradeflow-app 2>&1 | grep -E "reconnect_recovered|resubscribed_after_farm_flap|\[FEED\]|auto-heal|WATCHDOG" | tail -8
```
Informational: confirms PR C/D are exercising and recovering, not silently failing.

---

## 7. Pending work queue

Priority depends on V0/V1, not this ordering.

### 0. (BLOCKER) Diagnose + clear the `evaluator_error` halt
Per V0. If the allocation change caused the throw, fix the drawdown path (or unset `KILL_SWITCH_ALLOCATION_USD` and re-arm with a corrected value). The bot does not trade until this is resolved.

### 1. Trailing take-profit redesign (AUDIT) — the strategy's edge — brief in §13a
Standalone post-fill trailing stop matching SeanBot's ratcheting behavior (Error 328 blocks the bracket-child form). **Highest value** — SeanBot's +$555 trail winner this session is the demonstrated cost of running fixed.

### 2. Config-driven three-tier halt evaluator (AUDIT) — brief in §13b (operator-provided)
A richer kill-switch policy (operational-fault Tier 1, soft-pause Tier 2 with PF-floor / R-multiple / high-water-mark DD, hard-kill Tier 3). **Must be reconciled with the just-shipped `kill_switch.py` (PR A) — they overlap; this is its v2, not a parallel system.** PR A uses drawdown-from-epoch; this brief specifies drawdown-from-high-water-mark — decide which wins.

### 3. Fix the streak-reset-by-orphan quirk (AUDIT-adjacent)
Synthetic/zero-pnl closes (orphan, manual, recon) should not reset the consecutive-loss counter. Count only real trades, or treat `pnl_net==0` non-fills as neutral (don't break the streak).

### 4. Set `KILL_SWITCH_ALLOCATION_USD` (operator) — IN FLIGHT
Arms the 33% drawdown brake. Touches the secrets `.env` (operator's call who edits) + a restart. Prime suspect for the V0 evaluator_error — verify the drawdown code handles the now-set allocation before re-arming.

### Open items / operational debt
- **CLAUDE.md** — a pre-existing uncommitted working-tree change; excluded from every PR this session. Commit or discard.
- **Connection escalation ladder + active gateway-liveness probe** — PR D documented these as proposals (not built). Consider after the trailing redesign.
- **Drop vestigial `equity_base`** from `KillSwitch` + orchestrator wiring (tidy).
- **Unproven-live:** the native bracket's *disconnect-survival* hasn't been tested across a real restart-with-open-position yet (only the stop-fill is proven). PR C bar-gap re-seed hasn't fired on a real gap.

---

## 8. Test safety — why we belabor this

Cumulative mocking traps to keep preventing:
1. Tests green against a fictional schema (mocked column names that don't exist).
2. `side_effect` list wrong count → silent `StopIteration`. **Count-comment every `side_effect`.** (Bit us this session: the place_entry path went from 2 placements to 3 — two tests had 2-element lists.)
3. Mocked at the raw library chain when prod uses a wrapper → green tests, broken prod. **Mock at the code's boundary.**
4. Shared `MagicMock()` leaking state → **fresh mocks per test.**
5. **NEW: broker *acceptance* of an order shape is not unit-testable (Error 328, Error 321). A unit test asserting on the `Order` object cannot catch an IBKR rejection — needs a live/dry placement probe.**

Suite is at **559 passing** (8 benign `AsyncMock … never awaited` warnings — do not chase). Do not ship tests that skip the guardrails (master template §12 enforces them).

---

## 9. Pitfalls from prior sessions
- **VPS CC committed onto LOCAL main before branching** (caught by branch protection; moved to a feature branch). Branch off `origin/main` FIRST.
- **Warmup convergence diff resets on every redeploy** — re-accumulates from zero each restart.
- **Don't trust a single screenshot as root cause** (v16 had two screenshot-driven wrong diagnoses).
- **GitHub PR numbers ≠ brief names** — persistent offset; track real numbers (#80–#88 this session).
- **Handoff numbers are stale by design** — re-query streak, P&L, halt state, position, open-order count. **This session: do NOT trust the −$2.48 day P&L or the "FLAT" position — re-derive (V1/V3).**

**Next-session rule: if a claim is quantitative, re-verify it.**

---

## 10. Session discipline lesson (2026-06-02)

A heavy, productive session — 11 PRs, a live resume, and three live-surfaced bugs all self-corrected. **Meta-lessons, logged at the operator's request:**

1. **Unattended auto-deploy of AUDIT trading-path changes generates incidents — confirmed empirically.** The orchestrator (chat tier) recommended *building* autonomously but holding merges/deploys at the AUDIT gate. The operator made an informed call to override that for the **paper** account ("we'll fix mistakes on the fly; I'm only losing time"). The override was reasonable for paper and the self-correction worked — but the run then surfaced **three** bugs only at live deploy (cold-SMA race, Error 328 rejected orders, an 11-restart crash loop). **Standing rule for go-live: reinstate §0.5.184 / §0.5.188 — a human reviews the diff and watches the first live deploy when real money is involved.** Paper = fast-forward OK; live = gated.
2. **The trailing-TP miss is the headline gap.** The bot resumed on the *less-profitable* fixed exit; SeanBot's +$555 trail winner this session is the concrete cost. Prioritize §13a.
3. **"A redeploy disrupts transient state"** is the recurring theme tonight (dropped stops, reset warmup, orphan crash). Minimize restarts while a position is open; always verify *warm + protected + not-naked* after a deploy before resuming.
4. **Broker is ground truth** — the v16 "FLAT" was stale and hid a naked long. Always probe the broker, never trust the inherited claim.

**Enforcement rules for next session:**
1. Run §6 V0 (evaluator_error) before anything; a recurring throw re-halts forever.
2. Reinstate the AUDIT gate the moment real-money is on the table.
3. Probe live/dry-placement for any broker-acceptance-dependent order shape before declaring it done.

---

## 11. Logging verbosity — demand from any new code
- Every entry/exit/halt/heal logs `[COMPONENT] symbol: action — reason`.
- Every state transition logs old→new at INFO; every halt logs the trigger + inputs.
- Every swallowed exception logs the specific error + context (kill switch / warmup / heal fail-safes do this).
- Any dedup/select-one-of-many logs which row won and why.
- Async background tasks log `task_launched` + loop start.
- **Order placements log the full order shape** (type, parentId, ocaGroup/Type, transmit) so a broker rejection (Error 328/321) is diagnosable from logs alone.

---

## 12. Master template — use for every Claude Code PR
See the `code-pr-brief` skill (patch constraints, code quality, test-safety guardrails, known gotchas, "what I got wrong"). AUDIT PRs post the diff and pause for `merge`; REPORT/AUTO self-merge after CI green — **gate state per §0.5.184's current suspension note.**

---

## 13. Current PR briefs in flight — hand to VPS CC as-is

### §13a — Trailing take-profit redesign (AUDIT) — standalone post-fill trailing stop

~~~
# TradeFlow — Claude Code PR: Trailing-stop take-profit (standalone, post-fill) — AUDIT

> AUDIT. Implement → test → open PR → POST DIFF AND PAUSE for operator `merge`. After merge: deploy, verify, report. Branch off origin/main FIRST. §0.5.186/.187 discipline.

## Role
Senior dev on TradeFlow (paper MNQ bounce bot, DUQ331660). TF runs EXIT_MODE=fixed because IBKR rejects a TRAIL as a bracket child of a MKT parent (Error 328, §0.5.190). The strategy's edge lives in letting winners run — SeanBot rode one trend to +$555 (trail stop +139 pt) this session while TF's fixed +150 would have capped it ~+$300. Bring TF's trailing exit to SeanBot's validated shape via a STANDALONE post-fill trailing stop.

## Probe FIRST (§0.5.97)
Read SeanBot's trailing logic in its source (the uploaded reference codebase) and CONFIRM the exact rule before coding: the trail offset (the validated figure is 150 pt; the live "STOP MOVED" cadence suggests it ratchets behind the high-water mark — verify the exact offset and whether it trails per-bar or per-tick), the starting stop (entry−75), and the exit condition (pullback to the trailed stop). Quote what you find.

## Objective
Add EXIT_MODE="trailing" as a STANDALONE post-fill trailing stop (no separate +150 LMT TP in trailing mode — the trail IS the exit). On the entry parent fill: place ONE native IB TRAIL stop (SELL for LONG), trailStopPrice initialised at entry−75, auxPrice = the probed trail offset, GTC, outsideRth=True, parentId=0 (standalone — dodges Error 328). IB ratchets it server-side as price runs in profit; it never loosens. EXIT_MODE="fixed" keeps the current {STP, LMT +150} OCA bracket (no regression). Flag-guarded, revertible with no code change.

## Never-naked sequencing (the key design decision — lay out + recommend)
The entry bracket (build_entry_oca_bracket) already places a fixed STP child that is live from the fill. In trailing mode you must hand off from that fixed STP to the trailing stop WITHOUT a naked window and WITHOUT two live sell-stops. Options to evaluate in Task A: (A) keep the entry's fixed STP child, then on fill place the TRAIL and cancel the fixed STP in that order (brief overlap of two stops — acceptable? OCA them?); (B) place the entry bracket with the STP child as the ONLY exit leg, then on fill convert/replace it with the TRAIL. Recommend the safest. The #80 reconciler self-heal remains the backstop; never leave the position without at least one resting protective stop.

## Constraints
- Fixed protective floor never disappears mid-handoff. Trail is profit-side ratcheting only; the stop never moves DOWN.
- Native IB TRAIL preferred (server-side, survives disconnect, matches SeanBot). Fall back to in-app bar-trail ONLY if native standalone TRAIL is unworkable, and flag the choice. (NOTE: standalone TRAIL is NOT a bracket child, so Error 328 does not apply — verify with a dry/live placement probe per §8.5.)
- Entry selection unchanged — src/strategy.py gate logic empty diff.
- Fail-safe: if the TRAIL placement fails, KEEP the fixed STP live (never naked), log loudly, never raise into the order loop.
- AUDIT: post the diff, pause for merge. Probe-confirm the live placement is accepted (no Error 328/321) as part of post-merge verification — the unit tests cannot prove broker acceptance (§8 trap #5).

## Tasks
A — audit + SeanBot probe (quote findings): the on-fill placement site in router.py; how the entry bracket's STP child is tracked; the exact SeanBot trail rule; the never-naked handoff design (recommend A or B).
B — implement EXIT_MODE="trailing" standalone post-fill TRAIL + the never-naked handoff. Fixed mode preserved.
C — tests: trailing mode places a standalone TRAIL (parentId=0, trailStopPrice=entry−75, auxPrice=offset, GTC, outsideRth) on fill; the fixed STP is handed off without a naked gap and without two live stops; EXIT_MODE=fixed unchanged; placement-fail keeps the fixed STP. Fresh mocks, count-commented side_effects, mock at the IB wrapper boundary.
D — completeness: src/strategy.py empty diff; the stop never moves down (grep/inspection); --stat; black+ruff; full suite green.
E — out of scope (document, NOT build): in-app per-bar trail if native works; the three-tier evaluator (§13b).
F — post-merge: deploy; on the FIRST live entry confirm the TRAIL is accepted (no Error 328), rests, and ratchets (capture a "STOP MOVED"-equivalent log). STOP-and-report if Error 328/321 reappears.

## Gotchas
1. Error 328 — TRAIL standalone (parentId=0), NOT a bracket child. 2. Error 321 — set contract.exchange="CME" on a position-derived contract. 3. Never a naked window during the STP→TRAIL handoff. 4. Trail ratchets up only. 5. Broker acceptance isn't unit-testable — live/dry probe required. 6. AUDIT — pause for merge.
~~~

### §13b — Config-driven three-tier halt evaluator (AUDIT) — operator-provided, VERBATIM

> NOTE: assign the real GitHub PR number at creation (next is ~#89). This is the **v2 of the kill-switch policy** and MUST be reconciled with the just-shipped `src/execution/kill_switch.py` (PR A `b51d74f`) — they overlap (PR A already has notify/halt/drawdown tiers). Decide whether this evaluator supersedes PR A's `evaluate_triggers` or wraps it; in particular PR A measures drawdown from a deploy epoch while this brief specifies high-water-mark — pick one. Treat the brief's "Tier 1 = already exists" as the existing halt path and "the existing kill-switch function" as `KillSwitch.poll_once`/`raise_halt`+`flatten`.

~~~
# TradeFlow — Claude Code PR Prompt: PR [N] — Config-driven three-tier halt evaluator

> **AUDIT-TIER.** Do NOT self-merge. This touches the kill-switch and strategy-halt paths. Implement, open the PR, run CI to green, then STOP and report for operator approval of (a) the actual threshold values and (b) the Tier-3 flatten/cancel wiring. Single-word approval gates the merge.

## Role
You are a senior Python developer working on TradeFlow, an autonomous MNQ futures trading bot running on IBKR (paper account, DUQ prefix) via ib_async + Docker + Supabase on a Hetzner VPS. You write clean, tested, production-grade code. You never modify files you weren't asked to modify. You always study existing code patterns before writing new code. You understand this is a production system — bugs cost real money.

You are verbose in your logging. Format: `[COMPONENT] symbol: action — reason`.

You second-guess your own assumptions. Before writing code, you state what you expect the existing pattern to be, then verify by reading the actual file. You NEVER trust prior sessions' claims about column semantics, file paths, or code behavior without running a quick verification first. In particular, do NOT assume the names of the orchestrator entry point, the existing kill-switch/flatten function, or the config loader — verify them in Task A before writing.

## Context
Sessions 1–15 shipped the orchestrator (PR #10), durable `signal_reconciliations` storage, the bracket sibling-cancel money-safety fix, and an orphan canary in the READINESS block (HEAD `8551cb3`, bot left FLAT). What's missing is a deliberate circuit-breaker layer: today the bot has operational safety halts but no codified P&L/behavioral halt policy. The strategy is SMA100/MA100-bounce, long-only, 2 MNQ contracts — a low-win-rate (~30–45%), high-payoff trend profile, so raw consecutive-loss or fixed-percent triggers misfire on normal drawdowns. This PR adds a config-driven three-tier halt evaluator: Tier 1 = operational safety (already exists — this PR only routes to it), Tier 2 = soft pause when live deviates from the backtested envelope, Tier 3 = hard kill-switch floor + daily-loss limit. All threshold values are read from config and left as placeholders; the operator populates them from the SeanBot multi-year backtest distribution before any deploy.

## 🏗️ System Architecture & Recent Learnings
- Container: TradeFlow app container (orchestrator + strategy loop) — confirm exact name in Task A
- Language: Python 3.x, async (ib_async event loop)
- Database: Supabase (Postgres) + service key env var
- Env Vars: none new expected; confirm whether config path is env-driven in Task A
- Logging source: `docker logs <container>`; module-level LOGGER

### Key Architecture Constraints
- Constraint 1 (Runtime): broker state is ground truth, not the internal DB (§0.5.98) — daily P&L and current position MUST be read from IBKR account values / reconciled fills, not inferred from DB rows alone.
- Constraint 2 (Shell): tests run inside the container via `docker exec` (confirm in Task A) — `git -C ~/tradeflow ...` for all git ops, no `cd && `, no heredocs, no `$()`, no `${VAR}`, no `;` separators.
- Constraint 3 (Schema): if persisting halt events, confirm whether to reuse an existing table or add `halt_events`; do not invent columns — read an existing table's pattern first.
- Constraint 4 (Scope boundary): you do NOT design or tune the threshold values, and you do NOT build a new flatten/cancel routine — Tier 3 calls the EXISTING kill-switch path. If no such function exists, STOP and report; do not write one in this PR.
- Constraint 5 (Design decision): drawdown is measured from a tracked high-water mark (peak equity), NOT from initial allocation. Daily-loss is a separate same-session limit. Confirm the evaluator runs once per strategy-loop tick (recommended default) vs per-fill — state which and why in the Task A finding.

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints
Files you WILL modify (EXACTLY 3 — confirm/adjust paths in Task A before writing):
- `src/risk/halt_evaluator.py` (NEW — the tiered evaluator + dataclass result)
- `config/risk_limits.yaml` (NEW — placeholder thresholds, all values `null` or `# TODO from backtest`)
- `tests/test_halt_evaluator.py` (NEW)

Files you MUST NOT modify:
- the orchestrator entry point (you will only READ it in Task A to confirm the integration point; wiring it in is a follow-up REPORT-tier PR after thresholds are approved)
- `src/.../` strategy logic, secrets, anything under reconciliation/orphan-canary code
- any existing kill-switch/flatten module (READ ONLY)

Verification gates (run before pushing):
- `git -C ~/tradeflow diff main -- src/orchestrator.py` → MUST be empty (confirm actual filename)
- `git -C ~/tradeflow diff main` on the existing kill-switch module → MUST be empty
- `git -C ~/tradeflow diff main --stat` → should show EXACTLY 3 files changed

### 2. Code Quality
- `black --check` passes
- `ruff check` passes
- No unused imports or variables; one import per line (E401)
- Line length under 100 chars where possible
- Type hints on the evaluator's public function and the result dataclass
- Verbose logging format: `[HALT] account: tier-N triggered — <metric> <value> vs limit <limit>`

### 3. Safety
- All pre-existing tests still pass. Known failing (do NOT fix): carry forward the red-test list from the Session 15 HANDOFF — read it first; if none documented, state "none documented" in the PR. (v17 note: suite is 559 green + 8 benign AsyncMock warnings — do not chase the warnings.)
- No unexpected DB writes (if halt_events persistence is included, it is the ONLY new write).
- No IBKR order calls from the evaluator itself — it returns a decision; routing to the kill-switch is the caller's job (and is out of scope this PR).
- No changes to method signatures in any existing module.
- If you find a bug adjacent to the fix, DOCUMENT IT in the PR description. Do NOT fix it.

## 🧩 Current Mission: Add a pure, config-driven three-tier halt-decision module.

### Objective
Add `evaluate_halt(state, limits) -> HaltDecision` that, given current account/session metrics and a loaded `risk_limits` config, returns a structured decision: `{halt: bool, tier: int|None, reason: str, action: "none"|"soft_pause"|"hard_kill"}`. It performs NO side effects and places NO orders. Thresholds come entirely from config; this PR ships placeholder config with no real values.

### Task A: Audit
Read the orchestrator entry point and the existing kill-switch/flatten module. Read one existing config loader and one existing Supabase-writing function as pattern references. Answer in a 3–5 line PR-description finding: (1) exact orchestrator file + function where a per-tick hook would later attach; (2) exact name/signature of the existing kill-switch function Tier 3 will eventually call; (3) how config is currently loaded (env path? yaml? pydantic?); (4) the metric source for daily realized P&L and high-water mark — broker value vs reconciled fills. Write no code until these four are answered. (v17 note: the existing kill switch is `src/execution/kill_switch.py` — `KillSwitch.poll_once` + injected `raise_halt`/`flatten`; config is dataclass+env in `config/risk_params.py`, NOT yaml today — RECONCILE, don't duplicate.)

### Task B: Implement
In `src/risk/halt_evaluator.py` define a `HaltDecision` dataclass and `evaluate_halt(state, limits)`. Logic, evaluated in tier order, first match wins:
- **Tier 1 (action=`hard_kill`)**: if `state.operational_fault` is set (orphan detected, position≠intent, broker disconnect, fill-reconcile failure) → halt regardless of P&L. This PR only reads an already-computed flag; it does not detect faults.
- **Tier 2 (action=`soft_pause`)**: any of — single realized loss > `limits.max_single_loss_R` × R; rolling profit factor over last `limits.pf_window` trades < `limits.pf_floor`; trailing drawdown from high-water mark > `limits.dd_soft_pct`. Mirror the project's existing config-access pattern.
- **Tier 3 (action=`hard_kill`)**: trailing drawdown from peak > `limits.dd_hard_pct` OR same-session realized loss > `limits.daily_loss_pct`.
- Else `{halt: False, action: "none"}`.
Every triggered branch logs `[HALT] account: tier-N triggered — <metric> <value> vs limit <limit>`. Guard against `None` limits: if a referenced limit is unset, that specific check is SKIPPED and logged once as `[HALT] account: tier-N check skipped — <limit> unset`, never treated as 0.

`config/risk_limits.yaml`: all keys present, all values `null` with an inline `# TODO: from SeanBot backtest — set to <description>` comment (e.g. `dd_hard_pct: null  # TODO: max(1.5× backtest max DD, operator floor)`).

### Task C: Add tests
`tests/test_halt_evaluator.py`. Cover: each tier triggers in isolation; tier ordering (Tier 1 wins over Tier 3 when both true); unset limit skips its check and does NOT halt; clean state returns `action="none"`. Pure-function tests — no DB, no IBKR mocks needed. TEST SAFETY GUARDRAILS still apply (see checklist); build `state`/`limits` as fresh fixtures per test, never shared mutable dicts. Verify the async decorator pattern of a neighboring test file before assuming any decorator is needed (this module is likely sync — confirm).

### Task D: Verify completeness
`git -C ~/tradeflow grep -n "kill" src/` and `... grep -n "halt"` to confirm no existing halt/kill logic is being duplicated or shadowed. Classify every hit (existing-operational-safety / unrelated / now-superseded). Confirm the evaluator does not import any IBKR or order module.

### Task E: Out-of-scope investigation
~10 min: check whether the high-water-mark value the evaluator will need is already tracked anywhere (DB column, in-memory orchestrator field) or must be added later. Document findings; do NOT implement tracking in this PR.

### Task F: Post-merge smoke test
Copy-paste ready, run after approval+merge by VPS CC:
- `git -C ~/tradeflow fetch && git -C ~/tradeflow pull --ff-only origin main` → expect HEAD at merge commit
- `docker exec <container> python -c "from src.risk.halt_evaluator import evaluate_halt; print('import ok')"` → expect `import ok`
- `docker exec <container> python -m pytest tests/test_halt_evaluator.py -q` → expect all pass
- STOP and report if import fails or any new test fails — do NOT proceed to wire the orchestrator.

## 📤 Expected Output

### Files modified (EXACTLY 3)
- `src/risk/halt_evaluator.py`, `config/risk_limits.yaml`, `tests/test_halt_evaluator.py`

### Git diff stat
- evaluator ~80–130 lines; config ~15–25 lines; tests ~80–140 lines. No other files.

### PR description must include
1. Summary — one sentence
2. Task A audit — 3–5 line finding (the four answers)
3. Task D grep output — full list with classifications
4. Task E finding — one paragraph
5. Local test run — tail of pytest output
6. Full suite run — only documented failures
7. Protected-file diff verification — all empty
8. Smoke test — Task F commands
9. Explicit scope statement: "This PR does NOT set threshold values, wire the orchestrator, build a flatten routine, or detect operational faults."
10. "What I got wrong during this PR" — 1–3 lines, or "nothing".

## 🔍 Pre-Push Checklist

### Code Quality
- [ ] `black --check` passes
- [ ] `ruff check` passes
- [ ] No unused imports
- [ ] No multi-import lines
- [ ] No signature changes to existing modules

### Tests — TEST SAFETY GUARDRAILS
- [ ] Fresh fixture objects per test (never shared mutable state)
- [ ] No `side_effect` list without an explicit count comment
- [ ] No `patch()` on module-level factories; use direct construction/injection
- [ ] Async decorator pattern matches a verified neighbor (this module is likely sync — confirm before adding `@pytest.mark.asyncio`)
- [ ] Assertions check decision fields, not call indices

### Production Safety
- [ ] Every "Verification gates" entry shows empty diff
- [ ] Task D grep lists all sites; confirms nothing duplicated
- [ ] Evaluator imports no IBKR/order module (pure function)
- [ ] PR description states wiring + thresholds are NOT in this PR
- [ ] PR description notes any adjacent issues (Task E)
- [ ] PR description includes "What I got wrong" section
- [ ] **AUDIT gate: PR opened, CI green, then STOPPED for operator approval — NOT self-merged**

## ⚠️ Known Gotchas
1. Broker state is ground truth, not the internal DB (§0.5.98) — daily P&L / position come from IBKR, not inferred DB rows.
2. Docker healthy ≠ broker API healthy (§0.5.151) — irrelevant to this pure module but relevant when wiring later.
3. VPS CC Bash discipline: no heredocs, `$()`, `${VAR}`, `;`, or `cd X &&`. Write Python via the Write tool to `/tmp/scriptN.py`; commit msg via `git commit -F /tmp/commitmsg.txt`; PR body via `--body-file /tmp/pr_body.md`.
4. `--dangerously-skip-permissions` is opted out — do not use; fix permission prompts behaviorally (absolute paths, `git -C`, Grep/Glob/Read tools).
5. Drawdown is measured from high-water mark, not initial allocation — do not "simplify" to initial-capital math.
6. Unset (null) limit means SKIP that check, never treat as 0 — a 0 floor would halt instantly.
7. Pre-existing red tests from Session 15 HANDOFF — do not fix.
~~~

---

## 14. Canonical references (in order of authority)
1. **Source code on `main` @ `7db2157`** — what actually runs.
2. **Production DB** (`lifecycles` via service role) — truth for P&L / loss count / state (§0.5.98).
3. **IBKR** via `ib_async` (paper DUQ331660) — truth for position / fills / NetLiq.
4. **Dashboard** `/scoreboard` + `/divergence` — measured TF-vs-SeanBot truth (DB-sourced).
5. **Telegram** (TradeFlow + SeanBot channels) — the live event log for this session.
6. **This handoff (v17)** — session context, NOT long-term authority.
7. **v16 and earlier** — historical; ignore any claim contradicting 1–5.

---

## 15. First 15 minutes of the next session
1. Read §0.5, §1, §4, §5, §10 of this handoff. **Most important: §1's evaluator_error halt + §5's "broker acceptance isn't unit-testable".**
2. VPS CC pre-flight: `git -C ~/tradeflow fetch && git -C ~/tradeflow pull --ff-only origin main && ls -t docs/handoffs/ | head -3`, read this handoff from disk.
3. Run §6 **V0 first: why did the evaluator throw?** Then V1–V3 (position, streak, code/env). Decide: fix the throw vs unset/correct `KILL_SWITCH_ALLOCATION_USD` vs clear-if-transient. Do NOT clear blindly.
4. Commit-or-discard the floating `CLAUDE.md` (§7 debt).
5. Dispatch §13a (trailing-TP redesign) — the highest-value item — as the next AUDIT PR (pauses for `merge`). §13b (three-tier evaluator) is the one after, and must be reconciled with the shipped `kill_switch.py`.
6. On each diff: review → `merge` → deploy → draft the smoke-test runbook (`vps-smoke-test-runbook` skill). Verify warm + protected + not-naked before letting it resume.

---

## 16. How to publish this handoff

**Path A — VPS CC brief (preferred — no operator browser):**

~~~
You are VPS Claude Code on the TradeFlow VPS. Pre-flight:
  git -C ~/tradeflow fetch origin
  ls -t ~/tradeflow/docs/handoffs/ | head -5

Determine N: this handoff is v17. If the latest existing handoff is NOT v16, set
N = (latest existing) + 1 and update the filename AND the "v17" references in the body to vN.

Save the handoff content (from /tmp/HANDOFF_v17.md, scp'd by the operator, or written via the
Write tool from the pasted content) verbatim to ~/tradeflow/docs/handoffs/HANDOFF_v<N>.md, then:
  git -C ~/tradeflow add docs/handoffs/HANDOFF_v<N>.md
  git -C ~/tradeflow commit -F /tmp/handoff_commitmsg.txt
  git -C ~/tradeflow push origin main
where /tmp/handoff_commitmsg.txt contains:
  docs: add v<N> handoff (hardened+resumed live; halted on evaluator_error; trailing-TP + 3-tier queued)

(Docs-only commit to main is the allowed push-to-main exception — no prod code/secrets.
If branch protection blocks it, open a 1-file PR off origin/main and squash-merge it.)

THEN, same run, a READ-ONLY diagnosis of the live halt (do NOT change anything, do NOT clear):
  docker logs tradeflow-app 2>&1 | grep -E "evaluator_error|Traceback|\[KILL\]|halt_raised" | tail -40
  docker inspect tradeflow-app --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -iE "COMMIT|KILL_SWITCH_ALLOCATION"
Report: the handoff commit hash, the file path, AND the evaluator_error traceback + whether
KILL_SWITCH_ALLOCATION_USD is set — so the next chat starts with the halt cause in hand.
~~~

**Path B — manual fallback:**
```bash
scp HANDOFF_v17.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v17.md
ssh tradeflow@5.78.212.37 "git -C ~/tradeflow add docs/handoffs/HANDOFF_v17.md && git -C ~/tradeflow commit -m 'docs: add v17 handoff (hardened+resumed live; halted on evaluator_error; trailing-TP + 3-tier queued)' && git -C ~/tradeflow push origin main"
```

The handoff exists only once saved to disk and committed. Until then, treat this as draft.

---

*End of handoff v17. Target lifespan: until the evaluator_error halt is resolved, the trailing-TP redesign (§13a) is merged + the bot has run a no-redeploy window proving (a) trailing exits accepted live and (b) the native bracket surviving a real restart-with-position. Then rely on `main @ HEAD` + whatever v18 captures.*
