# TradeFlow — Handoff v20 (STABILIZE-5 standalone stop + SeanBot-triggered entry shipped; concurrency is next #1)

*Handoff from end of 2026-06-03 (~22:20 UTC). Bot is **armed, FLAT, healthy** on deployed HEAD `de7d34e`. Two changes are live but **UNCONFIRMED in the wild**: the STABILIZE-5 exit fix (no up-trade has ratcheted-and-held at the broker yet) and the SeanBot-triggered entry (no trigger fire yet). Do not start new strategy work until both are confirmed per §6/§7. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read §§1–6 first — that's the state of the system as of handoff. §§7–13 are reference. §14 is the authority order when this doc disagrees with itself or a live observation.

**Do not trust this doc alone.** Run the §6 verification block before writing any code. **Critical first action: confirm the book is FLAT and HEAD is `de7d34e` before touching anything.** The two live fixes are unconfirmed — the single most valuable thing the next session can do is *catch the first up-trade and the first SB-trigger fire*, not write new code.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner is a copy-paste-ready block. Owner (Ohad) is a hands-off PM: he pastes briefs into VPS Claude Code (CC VPS), gives single-word approvals, watches Telegram + dashboard. Minimize his UI/browser steps — prefer one SSH command or one CC VPS brief.

**Learning-delivery discipline.** Surface each new fact/bug-pattern/corrected-assumption immediately as a paste-ready snippet, not at end-of-session.

**Read before diagnosing.** Read full startup log + 3–5 full cycle narratives / the source of truth before proposing a root cause. Diagnosing from `grep | wc -l` is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth** (broker API, Supabase, raw log) before escalating urgency language.

**Always draft a VPS smoke-test runbook after a PR merge** unless told otherwise — the owner does not run smoke tests by hand.

**The §0.5.x numbered registry is canonical in `CLAUDE.md` on `main`** (read at pre-flight). It never shrinks. Load-bearing carry-forwards: §0.5.97 (probe external specs from source — never re-derive MNQ contract/fees/schema from memory); §0.5.98 (broker/exchange state = ground truth, not internal DB); §0.5.158 (compose service `ib-gateway` ≠ container `tradeflow-ib-gateway`); §0.5.159 (`.tradeflow-secrets/.env` shadows `${VAR:-default}` — grep before assuming a compose default); §0.5.160 (new PR branches off `origin/main`); §0.5.186/187 (CC VPS Bash discipline: no heredocs, no `cd &&`, no `;`, no `$(...)`, no `${VAR}`, no `VAR=` prefixes — use `git -C`, Write tool to `/tmp`, `python3 /tmp/x.py` for interpolation, `--body-file` for PR bodies, Python polling loops); §0.5.202 (force-recreate silently un-halts a flat bot — always confirm FLAT immediately before recreate); §0.5.T4 (kill switch raises an **in-process** halt+flatten, NOT exit-42/systemd; Docker uses `restart: unless-stopped`).

**New this session — §0.5.205–209 (already written into `CLAUDE.md`):**
- **§0.5.205** — IB gateway AUTO-OCAs parentId-linked bracket children (ocaType=3, group=parent permId) **even when code sets no `ocaGroup`**. OCA-grouped orders cannot be modified (Error 10326 → silent cancel). A modifiable/trailing stop MUST be a standalone `parentId=0`, ungrouped order — not a bracket child. STABILIZE-3 ("don't set the group") was insufficient; STABILIZE-5 removes the child entirely.
- **§0.5.206** — Protective stop = stop-**MARKET** (STP), never stop-limit (Harris Ch.4: a guaranteed exit beats a guaranteed price).
- **§0.5.207** — NEVER-ORPHAN: a standalone stop has no OCA sibling, so cancel it on any non-stop close via router `_cancel_sibling_legs` (event path) + reconciler `_cancel_open_legs` (broker-truth backstop).
- **§0.5.208** — Change-management discipline: every change runs the loop (observe → hypothesize → build → verify → promote → monitor) in one of four lanes, priority order **STABILIZE > REPLICATE > MEASURE > OPTIMIZE**; strategy/param changes must clear the promotion gate; never work a lower lane while a higher lane has an open defect; **never stack a change on an unconfirmed foundation**; strategy *invention* is shelved. External research = hypothesis generators, not authorities — validate on our own data.
- **§0.5.209** — Kill-switch restart = manual halt-ack (Supabase `halt_acks` row, or `touch /tmp/halt_clear` fallback if Supabase is down), NOT a process restart. There is no `/resume`. A force-recreate silently un-halts a flat bot (§0.5.202).

---

## 1. Where we are (as of handoff, 2026-06-03 ~22:20 UTC)

### Live production state
- `tradeflow-app` and `tradeflow-ib-gateway` **running, healthy**. App image built 22:15:28Z (right after the #107 merge).
- Broker (DUQ331660 paper, ~$1M NetLiq): **FLAT** — MNQM6 pos=0, **0 resting/open orders** across all clients (clientId 97 read-only probe).
- Recovery clean on boot: `recovery_loaded — count=0`.
- No manual overrides. Restart policy `unless-stopped`. Kill-switch NOT tripped. `ALLOCATION_USD` deliberately unset → the 33% drawdown brake is inert; the **10-consecutive-loss kill-switch (halt+flatten) is the only active hard brake**.
- Front month: **MNQM6** (conId 770561201, expiry 2026-06-18).

### What just shipped (this session, in order)
- **PR #104 → `da57927`** (STABILIZE-4): foreign-position auto-flatten guard. Merged + deployed. Verified idle on a flat book.
- **PR #106 → `37613f9`** (STABILIZE-5 — the real exit fix): trailing entry now places the parent **alone** (`build_entry_oca_bracket` returns `(parent, None, None)`); the protective STP is placed **standalone post-fill** via `ensure_protective_stop` (`parentId=0`, ungrouped) so the gateway can't auto-OCA it and the ratchet can modify it. NEVER-ORPHAN cancel added (`router._cancel_sibling_legs` + `reconciler._cancel_open_legs`). Stop kept as stop-MARKET. Same PR shipped the repo docs (see §3) and §0.5.205–209.
- **PR #107 → `de7d34e`** (REPLICATE — SeanBot-triggered entry): a SeanBot LONG MNQ notification triggers a TF entry IFF a validity check passes at TF's action time. Pure `evaluate_sb_trigger()` in `src/comparison/seanbot_reconciler.py`; orchestrator `_maybe_enter_on_seanbot()`. **FINAL DEPLOYED HEAD.**
- Suite: **655 passed (+14)**, ruff + black clean.

### What we discovered this session (evidence in §2/§4)
- **STABILIZE-3 was incomplete** — the gateway auto-OCAs the bracket-child stop regardless of code `ocaGroup` (§0.5.205). This is why no money-gate confirmation ever landed.
- **The exit bug's final bleed, quantified** (lifecycle `af66573f`): TF rode **+111.94 pt** unrealized, never ratcheted off base, round-tripped to a base-stop **−$294.72**, while SeanBot banked **+$200.38** on the identical entry — a ~**$495** swing on one trade.
- **The entry gap is dominated by concurrency, not band width.** Of SeanBot's 34 signals (trigger live), TF was **already in-position for 19 (~56%)**. TF holds one position at a time; SeanBot runs several concurrently. Detection coarseness (1-min bar) is a smaller, secondary lever; band width is minor.

---

## 2. The session's work thread

1. **Entered** on `f89b6e0` (STABILIZE-3) with PR #104 open and the money gate (confirm the ratchet holds) still unconfirmed.
2. **Merged PR #104** (STABILIZE-4 foreign-flatten guard) → `da57927`, deployed, verified idle.
3. **Money gate failed to confirm.** Read-only probe of the day's trade `af66573f`: entry 30582.81 ×2 @16:58:29Z, base stop 30509.75; **peak 30694.75 (+111.94 pt) @19:48Z**, lock-in +50 crossed 17:52Z — yet the stop **never moved off base**, exited 30509.75 @20:43:36Z reason STOP, **pnl_net −$294.72**, zero ratchet events. SeanBot's identical entry (30584.75) ratcheted to 30634.75 and exited +50/**+$200.38**.
4. **Root cause (corrected from STABILIZE-3):** the gateway auto-OCAs the parentId-linked child stop (ocaType=3) even with no code `ocaGroup`; OCA'd orders reject modification (Error 10326 → silent cancel), so the ratchet was a no-op. → **STABILIZE-5** removes the child and places a **standalone `parentId=0` STP** post-fill (`ensure_protective_stop`); NEVER-ORPHAN cancel added. Merged PR #106 → `37613f9`, deployed 21:29Z.
5. **Entry-divergence investigation.** `signal_reconciliations` N=34: TF own-gate AGREE 3/34 (9%). The decisive finding: **19/34 were no-stack (TF in-position)** — concurrency, not band width. SeanBot's entries cluster at the MA (median +1.25 pt, max +30.71); the touch-rejects were mostly a 1-min-bar timing/detection gap, not a too-tight band.
6. **Wrong call corrected** (mine, in chat): I had framed the entry gap as "blocked on getting SeanBot's rule from the friend." The data refuted it — the reconciliation log *is* his revealed rule; the gap is TF's own concurrency model + detection.
7. **SeanBot-triggered entry (REPLICATE).** Operator reframed: use his signal as *another input* — if it's still a good entry at action time, go in (not copy-trade his deals). Built `evaluate_sb_trigger()` + `_maybe_enter_on_seanbot()`; bounds **derived from data** (§3/§4). Validated with the **shipped** function over all 34 entries: AGREE 3→7 (9%→21%), **0 chases, 0 invented entries**. 655 tests green. Operator **waived the AUDIT pause** ("don't wait for me, double-check everything, push to prod") → CC VPS self-merged PR #107 → `de7d34e`, deployed 22:15Z, verified FLAT + healthy + code-in-image.
8. **End state:** both fixes live, both unconfirmed in the wild. Operator's stated next #1 = **concurrency** (after STABILIZE-5 is confirmed).

Closed rabbit holes: STABILIZE-3 "just don't set ocaGroup" (insufficient — gateway auto-OCAs anyway); "entry gap is the friend's band/rule" (it's concurrency + detection).

---

## 3. What the system is actually made of

**Single source of truth:** `CLAUDE.md` on `main` at `de7d34e` (the §0.5.x registry + autonomy contract). Plus this session's new repo docs.

- **Live entry paths (two):** (1) TF's own strategy gate (SMA100 touch); (2) **NEW** SeanBot-triggered validity-checked path (`_maybe_enter_on_seanbot` → `_handle_trade_signal`). Both dispatch through the same `_handle_trade_signal`, so halt + FLAT/no-stack (`create_lifecycle` → `InvariantViolationError`) are enforced once — the SB path **never stacks** and never double-enters an own-gate setup.
- **Exit/stop (STABILIZE-5):** standalone `parentId=0` STP placed post-fill by `ensure_protective_stop`; bar-ratchet modifies it; NEVER-ORPHAN cancel on non-stop close.
- **Repo docs added this session:** `.claude/skills/change-management/SKILL.md` (the loop + 4 lanes + gate); `docs/ROADMAP.md` (living backlog); `docs/research/strategy_debate_2026-06-03.md` (distilled hypotheses — NO raw LLM transcripts); `docs/research/book_principles.md` (distilled citations — NO book files, copyright); `docs/runbooks/kill_switch_restart.md`.
- **Reconciler:** `SeanbotReconciler` polls `seanbot_signals`, dedupes on `(channel, message_id)`, persists to `signal_reconciliations`, and now fires the optional `entry_handler` once per fresh entry.
- **Gotcha:** `TRADEFLOW_COMMIT` env reads `unknown` (not baked at build) — **verify deployed code by grepping source in the running container, never by the env var.**

---

## 4. Verified facts (2026-06-03) — DO NOT challenge unless the schema/contract migrates

- **`lifecycles`** keyed by `lifecycle_id` (NOT `id`). **`lifecycle_events`** timestamp is `emitted_at` (NOT `created_at`); a 400 on `lifecycles.id` / `decisions` is expected — the table is `strategy_decisions`.
- **`signal_reconciliations`** columns: `id, signal_ts, channel, message_id, seanbot_type, direction, symbol, price, classification, justification, tf_decision, acknowledged_at, created_at`. `tf_decision` records the **close**, not the gated low — do not infer the gated value from it.
- **TF touch gate:** `touch_ok = low ∈ [sma100 − 15, sma100 + 5]` (`ma_touch_buffer_pts = 5.0`).
- **MNQ spec (§0.5.97-verified):** TICK 0.25 pt, MULTIPLIER $2/pt ($0.50/tick), COMMISSION_RT $0.62, day-trade margin $2,000. **2-lot round-trip friction ≈ 1.12 index pts** (NOT 2.24 — a debate error we caught).
- **`reqHistoricalData` durationStr** unit must be `S|D|W|M|Y` (use e.g. `"18000 S"`, not `"5 H"`).
- **New load-bearing facts (this session):**
  - **§0.5.205 in practice:** a `parentId`-linked child stop is auto-OCA'd and **cannot be modified**; only a `parentId=0` standalone stop ratchets. Evidence: lifecycle `af66573f` peaked +111.94 pt, stop stayed at base, exited base −$294.72, zero ratchet events.
  - **SB-trigger bounds (data-derived, env-tunable `RISK.sb_*`):** `near_ma=[sma100−15, sma100+35]`, `no_chase ≤ SB_signal+25`, settled-bar freshness `≤180s`, `enabled=True`. Derived from: SB entry band (SB_price−sma100) min −12.88 / median +1.25 / p90 +11.94 / max +30.71; +1min drift vs SB signal median +2.5 / p90 +28.4 / max +70.8.

---

## 5. Wrong diagnoses — READ BEFORE YOU DEBUG

1. **"STABILIZE-3 (don't set `ocaGroup`) fixes the frozen stop."** Evidence at the time: code no longer set a group. **Wrong** — the gateway auto-OCAs parentId-linked children regardless. Correct fix: remove the child, place a standalone `parentId=0` STP (STABILIZE-5).
2. **(Orchestrator/chat) "The entry gap is blocked on the operator getting SeanBot's entry rule from his friend."** Evidence claimed: SB's band looked wider. **Wrong** — SB enters right at the MA (median +1.25); the dominant cause is **concurrency** (19/34 in-position, ~56%) plus 1-min detection coarseness; band width is minor (~7/24 of touch-rejects). The `signal_reconciliations` log already *is* his revealed rule — nothing was blocked on the friend.
3. **`§0.5.T4` doc drift** — earlier docs implied the kill switch exits the process (exit-42/systemd). **Wrong for this Docker deployment** — it raises an in-process halt+flatten; the container uses `restart: unless-stopped`. Corrected.

**Lesson for next session:** every wrong turn this session came from trusting a *plausible mechanism* over the *source of truth*. STABILIZE-3 was assumed-correct without a broker-confirmed ratchet; the "friend's rule" claim was made without reading the reconciliation log. Confirm the mechanism from broker/DB truth before building the fix — and before *claiming* a blocker.

---

## 6. Verification block — run this before doing anything

Run as CC VPS on the server. (CC VPS Bash discipline applies — no `$()`, `;`, heredocs, `${}`, `VAR=`.)

**V0 — HEAD + deployed image is the latest fix**
```bash
git -C /home/tradeflow/tradeflow fetch origin
git -C /home/tradeflow/tradeflow log --oneline -1 origin/main
docker inspect --format '{{.Created}} {{.Image}}' tradeflow-app
```
Expect: `de7d34e ... SeanBot-triggered ... (#107)`; image created `2026-06-03T22:15:..Z` or later. (Ignore `TRADEFLOW_COMMIT=unknown` — not baked.)

**V1 — broker is FLAT (ground truth, §0.5.98)**
```bash
/home/tradeflow/tradeflow/.venv/bin/python /tmp/tf_broker_truth.py
```
Expect: `MNQM6 ... pos=0.0` and `NO resting/open orders`. The probe is clientId 97 read-only (`reqAllOpenOrders` across clients). If `/tmp` was cleared, recreate it (read-only: qualify MNQM6, `reqPositions` + `reqAllOpenOrders`, print). **If pos≠0 or orders exist: STOP — do not recreate the container (§0.5.202); reconcile first.**

**V2 — both live fixes are in the running image (grep source, not the env var)**
```bash
docker exec tradeflow-app grep -c "evaluate_sb_trigger\|_maybe_enter_on_seanbot" /app/src/orchestrator.py
docker exec tradeflow-app grep -c "ensure_protective_stop\|return parent, None, None" /app/src/execution/bracket.py /app/src/execution/router.py
```
Expect: orchestrator ≥5; bracket ≥3 and router ≥5. Zero anywhere = wrong image deployed.

**V3 — boot config is what we think (SB-trigger on, trailing exit)**
```bash
docker logs tradeflow-app --since 2026-06-03T22:15:00Z 2>&1 | grep -iE "SB_TRIGGER|EXIT_MODE=|recovery_loaded|seanbot_reconciler: started"
```
Expect: `SB_TRIGGER=on — near_ma=[sma-15,sma+35] no_chase=+25 max_bar_age=180s`; `EXIT_MODE=trailing — stop_loss=75.0 lock_in=50.0 ... take_profit=150.0`; `recovery_loaded — count=0`.

**V4 — the two confirmations we are waiting for (search after any new trade)**
```bash
docker logs tradeflow-app 2>&1 | grep -iE "\[SB-TRIGGER\] (valid|reject)|ratchet|protective|lock"
```
A `[SB-TRIGGER] valid` line = the entry path fired. On a trade that runs up, confirm the **broker** STP `auxPrice` moved to the announced ratchet level (re-run V1 mid-trade) — that is the STABILIZE-5 money-gate proof.

---

## 7. Pending work queue

Priority depends on V1/V4 state, not list order.

### Confirm STABILIZE-5 (the money gate) — GATE-ZERO, blocks everything below
On the first trade that runs past +50: confirm the **broker** stop `auxPrice` == the announced ratchet level (normal few-pt slippage is fine, §0.5.206 — the bug was a round-trip to **base** after a higher announce, not slippage). This is the single confirmation the whole project has been waiting for.

### Confirm the SeanBot-trigger fires correctly
Watch `[SB-TRIGGER] valid/reject`: entry near SB's price (not a chase), no stacking, only when FLAT. Expect modest volume — most of SB's signals hit TF while in-position.

### CONCURRENCY — operator's explicit #1 priority once STABILIZE-5 is confirmed
Allow TF to hold multiple simultaneous positions like SeanBot (the ~56% of the entry gap that is no-stack). **AUDIT / through the promotion gate** — it is a risk-scaling decision. Design questions to settle: max concurrent positions; per-position sizing and aggregate contract/margin cap; how the 10-loss kill-switch counts across concurrent lifecycles; per-lifecycle standalone-stop management (each position owns its own `parentId=0` STP + NEVER-ORPHAN). On $1M paper it is proportionally low-risk (his ~6 ct on $100K vs 6 ct on $1M) — but **do not ship it on an unconfirmed exit** (§0.5.208).

### PARKED until exit confirmed — SB exit / stop-move signals as validity-gated hints
Operator's "or to go out, or update the stop" extension: same pattern as the entry trigger, applied to SeanBot's EXIT and STOP-MOVED notifications. Touches the just-rebuilt stop logic, so it waits until STABILIZE-5 is proven.

### Secondary — intrabar touch-detection granularity
Finer-than-1-min touch detection for TF's own gate. Minor lever vs concurrency; only after the above.

### Operational debt
- `/tmp/tf_broker_truth.py` and the §2/§3 probe scripts are ephemeral — recreate if `/tmp` was cleared (all read-only, reconstructable from §6/§4).

---

## 8. Test safety — why we belabor this

Carry-forward failure modes the suite must keep guarding: tests green against a fictional schema (mock the real column names — `lifecycle_id`, `emitted_at`); `side_effect` list count mismatch → silent `StopIteration`; mocking the raw library when prod uses a wrapper (custom httpx Supabase stub, never `supabase-py`); shared `MagicMock()` state leaking between tests. New this session: `RISK` is a **frozen dataclass** — patch `src.orchestrator.RISK` (the module symbol), not the instance attribute (`FrozenInstanceError`). The +14 SB-trigger tests cover boundary/chase/far/warmup/non-long, fire-once/dedup/error-swallow, and no-stack-in-position. Keep the `code-pr-brief` guardrails on every PR.

---

## 9. Pitfalls from prior sessions

- Docker `healthy` ≠ broker API healthy (§0.5.151 — the 85-hour silent outage).
- "Stop is fine because the code doesn't set a group" — wrong (§0.5.205, this session).
- Grep writers missed because only one syntax form was matched.
- Handoff numbers drift — **if a claim is quantitative (P&L, AGREE rate, in-position count, open orders), re-verify it from source.** The 9%→21% AGREE lift and 19/34 no-stack are from a one-time replay; re-run if you depend on them.

---

## 10. Session discipline lesson (2026-06-03) — incl. orchestrator's logged comments

**Meta-pattern:** the high-value work this session was *diagnostic confirmation*, not code volume. Both wins (STABILIZE-5, the SB-trigger bounds) came from hitting broker/DB truth; both errors came from skipping that step.

**Orchestrator's own comments, logged at the owner's request:**
- I spent turns generating frameworks, doc-trees, and A/B/C menus instead of driving the fix; the owner called it circling. Corrected to decisive action — probe → fix → ship.
- I wrongly asserted the entry gap was "blocked on the friend." It was not. Owned and corrected from the reconciliation data.
- Books earned their keep on one concrete point: Harris Ch.4 → the protective leg should be stop-MARKET, and a few points of stop slippage is *normal*, not the bug. That sharpened the money-gate criterion (the bug = round-trip to **base** after a higher announce). (Johnson's *Algorithmic Trading & DMA* PDF is **scanned — no extractable text**; treat as unavailable.)
- Honest standing note for the next session: this is a **paper** account; the path is paper-validation → live. Even SeanBot has red days (e.g. June 1, −$866). **If a correctly-executing, faithfully-replicated edge still does not track SeanBot once STABILIZE-5 is confirmed, that is a real "is this worth continuing" conversation — not another code change.**

**Enforcement rules for next session:**
1. Confirm the mechanism from broker/DB truth before building *or claiming* anything.
2. One change at a time; never stack on an unconfirmed foundation (§0.5.208). STABILIZE-5 must be confirmed before concurrency or SB exit/stop signals.
3. Drive to a probe or a fix; do not produce decision-menus when the data already answers the question.

---

## 11. Logging verbosity — what to demand from any new code

Every entry/exit/stop action logs `[COMPONENT] symbol: action — reason`; every lifecycle state transition logs old→new at INFO; every swallowed exception logs type + context (the `entry_handler` error path does this); dedup/select-one logs which row won; the SB-trigger logs `valid`/`reject` with `sb_price/current/sma100/reason` on every signal. Concurrency work must log, per lifecycle, which position a fill/stop/cancel belongs to.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill. It enforces patch constraints, code quality, the test-safety guardrails (§8), known gotchas (§0.5.x), and the "what I got wrong" post-PR section. Entry/exit/strategy/risk changes are **AUDIT** unless the owner explicitly waives the pause (he did so for #107).

---

## 13. Current PR brief in flight (if any)

N/A this session — no PR is mid-build. Both shipped PRs (#106, #107) are merged and deployed. The next brief (concurrency) should NOT be written until STABILIZE-5 is confirmed per §6/§7.

---

## 14. Canonical references (in order of authority)

1. **`CLAUDE.md`** on `main` at `de7d34e` — §0.5.x registry, autonomy contract, system rules.
2. **Source on `main`** at `de7d34e` — what actually runs (verify in-container per §6/V2).
3. **Production Supabase** (service role, read-only GETs) — `lifecycles`, `lifecycle_events`, `signal_reconciliations`, `strategy_decisions`, `halt_acks`.
4. **IBKR paper (DUQ331660)** via `ib_async`, clientId 97 read-only — broker state truth.
5. **This handoff (v20)** — session context, not long-term authority.
6. **v19 and earlier** — historical; ignore any claim that contradicts 1–4.

---

## 15. First 15 minutes of the next session

1. Read §§0.5, 1, 2, 4, 5, 10 of this doc. §5 is the most important to internalize.
2. Pre-flight: `git -C ~/tradeflow fetch && git -C ~/tradeflow pull --ff-only origin main && ls -t docs/handoffs/ | head -3`, then run the §6 block. Confirm HEAD `de7d34e`, FLAT, both fixes in-image, `SB_TRIGGER=on`.
3. Do NOT write code. The job is to **catch the two confirmations** (§7): the first SB-trigger fire and the first ratchet-and-hold at the broker. Run V4 after any trade.
4. When STABILIZE-5 is confirmed: open the **concurrency** design with the owner (AUDIT, through the gate) — his explicit #1. Settle the design questions in §7 before any brief.
5. Keep the daily TF-vs-SeanBot comparison surfaced (dashboard / `signal_reconciliations`); re-verify any quantitative claim (§9).
6. After any future merge, draft a VPS smoke-test runbook (`vps-smoke-test-runbook` skill).

---

*End of handoff v20. Target lifespan: until STABILIZE-5 is confirmed at the broker on a live up-trade AND the concurrency design is agreed, then rely on `CLAUDE.md` + v21. Both shipped fixes are UNCONFIRMED until §6/V4 says otherwise.*
