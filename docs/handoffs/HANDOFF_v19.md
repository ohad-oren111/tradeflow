# TradeFlow — Handoff v19 (the bleed was the EXIT, not the entry: OCA-stop bug found + fixed; foreign-flatten guard queued)

*Handoff from end of 2026-06-03. The bot is LIVE, ARMED, and FLAT on deployed commit `f89b6e0` (STABILIZE-3). The real cause of the P&L bleed was found and fixed this session — a trailing stop that announced a level it didn't hold. PR #104 (foreign-position auto-flatten guard) is built and OPEN, **not merged, not deployed** — it awaits an operator merge decision. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read sections 1–6 first — state of the system as of handoff. Sections 7–13 are reference. Section 14 ranks sources of authority; when this doc disagrees with a live probe, the live probe wins.

**Do not trust this doc alone.** Run the §6 verification block before writing any code. **Critical first action: confirm the deployed commit is `f89b6e0` (or later if #104 was merged) and confirm the bot's broker position is what the lifecycles say it should be (flat, or a tracked long with exactly ONE ungrouped STP).**

The single most important thing to internalize this session: **we spent five backtest iterations chasing a strategy when the money was being lost in the exit-order plumbing. Read §5 before doing any "improve the edge" work.**

---

## 0.5 Standing rules (permanent — do not remove)

**Copy-paste instruction style.** Every recommended action is a self-contained, copy-paste-ready bash block; env vars sourced in-block; expected output described below it; decision tree if branches matter.

**Learning-delivery discipline.** Every new fact (bug pattern, corrected assumption, env fact) is surfaced immediately as a handoff-queue snippet, not saved for end-of-session.

**Read before diagnosing.** For complex state bugs, read the full startup log and 3–5 full cycle narratives before proposing a root cause. `grep | wc -l` diagnosing is the #1 cause of wrong root causes.

**Verify severity against source of truth.** Before escalating urgency, hit the live broker/DB, not aggregated metrics or the dashboard.

**Always draft a VPS smoke runbook after a PR merge** unless told otherwise.

**Carried-forward project rules (verbatim):**
- **§0.5.98 — Broker state is ground truth, not internal DB/state.** Confirm from the broker, not from what the bot thinks.
- **§0.5.151 — Docker healthy ≠ broker API healthy.**
- **§0.5.154 — Handoff publish is non-negotiable at end-of-session.**
- **§0.5.155 — CC VPS harness blocks git reset/rebase/force-push;** catch-up via cherry-pick onto a fresh branch off origin/main.
- **§0.5.158 — compose service name `ib-gateway` ≠ container_name `tradeflow-ib-gateway`.**
- **§0.5.159 — `.tradeflow-secrets/.env` shadows `${VAR:-default}`;** grep before assuming a compose default.
- **§0.5.160 — new PR branches off `origin/main`, not local main.**
- **§0.5.186 — probe discipline:** no sub-agents, no repo-wide sweeps, no heredocs in VPS CC Bash, run inside tmux.
- **§0.5.188 — weekend/unattended safety:** surface bugs as recommendations; don't auto-deploy when unmonitored.
- **§0.5.196 — trailing mode is STP-only;** no resting +150 LMT under trailing (the reconciler must not re-arm one).
- **§0.5.198 — at `has_new_bar`, `bars_obj[-1]` is the just-OPENED forming bar (opening tick only); `bars_obj[-2]` is the just-CLOSED settled bar.** Evaluate strategy + ratchet on `[-2]`.

**NEW standing rules (this session):**
- **§0.5.199 — An OCA-grouped order CANNOT be modified.** IBKR rejects a modify of any OCA-grouped order with **Error 10326 ("OCA group revision is not allowed") and CANCELS the order.** A *single-member* OCA group is NOT harmless: it silently destroys any order you later try to ratchet/modify. Never put a modifiable order (the trailing STP) in an OCA group. Fixed-mode STP+LMT pairs may stay OCA'd; trailing STP must be ungrouped.
- **§0.5.200 — Confirm order modifications from broker truth, not Trade status.** A modify acts on an already-resting order, so its status reads "resting" *before* an async rejection lands (~3 ms later). Re-read the live order (`find_open_order_by_id` / `openTrades`) and verify the `auxPrice` actually changed before announcing or persisting. Never announce a protective level the broker didn't accept.
- **§0.5.201 — A ratcheted stop level must be PERSISTED to the lifecycle (DB), not just held in memory + Telegram.** The reconciler's leg-heal reads the DB; if the ratcheted level isn't persisted, a reconnect/heal re-arms the stop at the stale entry−75 base. Persist on every ratchet; the heal re-arms at the persisted (ratcheted) level, never base.
- **§0.5.202 — An in-memory halt clears on container restart/redeploy.** A force-recreate restarts the process; if the account is flat there's no foreign position to re-trigger the halt, so **a redeploy silently UN-HALTS the bot.** If you want it parked after a deploy, re-halt explicitly.
- **§0.5.203 — Concurrent-session hazard.** Another session merged a PR (#101) mid-session, out from under the running session (working tree switched to main, files "went clean"). Before assuming you own a deploy: re-check `origin/main` HEAD and the merge author. Do not panic at a "lost" working tree — check the reflog first.
- **§0.5.204 — Foreign/unintended positions are handled by INTENT, not direction.** A long-only book is an emergent fact, not a coded rule. The guard reconciles net broker position vs net lifecycle intent (close-only, debounced) so it's correct today and the day shorts are added.

---

## 1. Where we are (as of handoff, 2026-06-03 ~15:00 UTC)

### Focus for the new chat (read this first)
- **The mission is STABILIZE → make the bot work on the proven edge → then optimize in small increments. Do NOT invent strategy.** The operator was explicit and repeatedly frustrated by strategy-research detours. We faithfully run SeanBot's proven rule and fix execution; we do not hunt for novel alpha.
- **The bleed cause is fixed (STABILIZE-3, live).** The next job is to *confirm it holds on a live trade*, then decide on PR #104.

### Live production state
- **Deployed commit: `f89b6e0` (STABILIZE-3).** `EXIT_MODE=trailing`, bullish=2.0, regime=False, 2 contracts, long-only, MNQ.
- Containers healthy: `tradeflow-app`, `tradeflow-ib-gateway`, `tradeflow-telegram-listener`.
- **Broker: FLAT, zero resting orders, no naked position, no ACTIVE lifecycle** (as of ~15:00Z).
- **Bot is ARMED** — the STABILIZE-3 force-recreate cleared the in-memory halt (§0.5.202). It will take the next qualifying signal. (If you want it parked, re-halt.)
- Realized P&L today ≈ **−$718.92** (one +$173.28 win, one −$299.48 stop loss, plus ~−$288 closing the bug-created naked short — see §2).
- Dashboard (now trustworthy): **TF cum −$2,310.40 vs SeanBot cum +$3,427.24** (SeanBot ahead ~$5,737). SeanBot column = operator-seeded daily anchors (see §4).

### What just shipped (this session)
- **PR #100 (`60ded90`)** — scoreboard dedup of SeanBot exit over-capture. Deployed.
- **PR #101 (`3d0ac73`)** — **settled-bar fix:** evaluate `bars_obj[-2]` (settled) not `[-1]` (forming) for BOTH entry and ratchet. Deployed. (Merged by a concurrent session — §0.5.203.)
- **PR #102 (`cfe4a1f`)** — **dashboard:** authoritative SeanBot daily P&L (operator anchors) + TF-vs-SeanBot cumulative comparison chart (inline SVG, no deps). Deployed.
- **PR #103 (`f89b6e0`)** — **STABILIZE-3, the real bleed fix:** trailing STP ungrouped; ratchet confirms broker truth before announce/persist; ratcheted level persisted; heal re-arms ungrouped at the ratcheted level. 627 tests. Deployed. **Current HEAD.**

### Built but NOT shipped
- **PR #104 (branch `claude/stabilize4-foreign-flatten-guard`, commit `c1606f3`, OPEN)** — intent-based, direction-agnostic foreign-position auto-flatten guard (flatten the unaccounted-for delta at market once persistently foreign, debounced; future-proof for shorts). 637 tests green. **AUDIT auto-liquidation — awaiting operator `merge`. Not merged, not deployed.**

### What we discovered this session (evidence in §2/§4/§5)
- The bleed was an **exit-order execution bug**, not the entry/strategy (§2, §5). Proven from broker fill history.
- SeanBot's live entries span **−77 to +34 pts from the 100-MA** — wider than his shared (2026-05-19) code's `[−15,+5]` band. **His live bot ≠ the code he shared.** Entry alignment needs his *current* rule (a question for the friend). PARKED.
- Even with the forming-bar fix, TF agrees with only **~46%** of SeanBot's entries on a settled-bar replay — the entry divergence is real but was **never** the bleed.
- SeanBot posts **no daily-summary** to the channel and capture began mid-2026-05-28, so his P&L cannot be reconstructed from captures → dashboard uses operator-seeded anchors (§4).

---

## 2. The session's work thread (with wrong turns — training data)

1. **Opened mid strategy-research** (BT-2 results in hand). Ran **BT-3** (vol-regime floor on C1_ORB) — honest IS-select/OOS-judge: **NOT promotable** (every floor ~PF 1.05, recent OOS third negative = regime decay). Ran **BT-5** (risk/portfolio): at equal drawdown C1 is ~2× the baseline's net, but it's still a thin, decaying edge.
2. **Operator pushed back hard** ("why aren't you stabilizing... you're taking me in circles... he gave us the code on a silver platter"). **Correct.** Pivoted: **shelve strategy-invention, stabilize-and-faithfully-replicate.**
3. **STABILIZE-1** (read-only diagnosis): proved at the ib_async library level that TF evaluated the **forming bar** (`bars_obj[-1]`) — firing on opening-tick noise, missing real touches. STOPPED without coding because the bar payload also feeds the proven trailing-exit ratchet.
4. **STABILIZE-2 / PR #101:** delivered `bars_obj[-2]` (settled) to both consumers. Deployed. (Merged concurrently by another session — §0.5.203.)
5. **DASH-FIX / PR #102:** probe proved SeanBot posts no daily summary and captures are lossy/late → seeded the operator's trusted anchors as authoritative; added the comparison chart. Deployed. **The working dashboard is what made the real bug legible.**
6. **Operator surfaced the smoking gun from his Telegram feed:** a long that announced "stop raised to 30,767.50 (+50 locked)" then **exited at 30,643.25 (−$299.48)** — the *original* entry stop, not the announced level. Meanwhile SeanBot's identical-style trades locked +50 and exited +46/+47/+40. **The bleed is the EXIT, not the entry.**
7. **STABILIZE-3 / PR #103 diagnosis (broker fill history):** the trailing STP was placed in a **single-member OCA group**. The ratchet's modify hit **Error 10326** → IBKR **cancelled** the stop while the code logged success. The reconciler then re-armed at **base** (ratcheted level never persisted) in a *different* OCA group. At the stop price, the resurrected original AND the heal duplicate **both fired** (sold 4 vs a +2 long) → **net −2 naked short.** Fix shipped (ungroup + broker-truth confirm + persist + heal-at-ratcheted). Deployed.
8. **Live safety:** broker truth contradicted the brief's "flat" premise — a live **naked −2 short** (~−$222, zero protective orders). Operator-approved flatten (BUY 2 MKT @ 30,715). The bug cost ~double the headline loss.
9. **STABILIZE-4 / PR #104:** built the intent-based foreign-position auto-flatten guard (the operator's "any short/foreign position → flatten + alert", designed direction-agnostic so adding shorts later needs zero change). Built, 637 tests, PR open. **Not merged** per the AUDIT auto-liquidation rule.

**Closed rabbit holes:** strategy invention (BT-1..BT-5) does not beat the proven rule and is not the problem; the entry divergence from SeanBot is real but is NOT the bleed; the forming-bar fix helped but wasn't the bleed either. The bleed was the OCA-stop bug, now fixed.

---

## 3. What the system is actually made of

**Single source of truth:** none as a single map file — this handoff + the source on `main @ f89b6e0` is the best available. Key live code paths:
- **Entry:** `src/strategy.py` (SMA100-touch, long-only, evaluated on the settled bar) → `OrderRouter.place_entry` → `bracket.py` (STP-only in trailing mode).
- **Exit:** bar-close ratchet `OrderRouter._ratchet_one` / `trail_manager.py` (ladder math) → broker modify of the resting STP; `reconciler.py` leg-heal as the never-naked backstop.
- **Reconciler:** `src/execution/reconciler.py` `full_scan` — leg-heal + (now) intent-based foreign-position handling.
- **Dashboard:** `dashboard/scoreboard.py` + `dashboard/seanbot_authoritative.py` (seeded anchors) + the inline-SVG chart; runs in-process inside `tradeflow-app`.
- **Listener:** separate container `tradeflow-telegram-listener` → writes `seanbot_signals` (Supabase). No shared volume with the app; they share state only via Supabase.

**Dead/phantom surfaces:** the per-exit SeanBot P&L reconstruction is now a fallback only (lossy); the `tf_research` harness (BT-1..BT-5) lives under `research/` and is NOT wired into prod.

---

## 4. Verified facts (DO NOT challenge unless schema/broker migrates)

- **MNQ spec (§0.5.97-verified):** TICK 0.25, MULTIPLIER $2/pt ($0.50/tick), COMMISSION ~$0.62/side, MARGIN ~$2,000 day-trade. Quarterly Mar/Jun/Sep/Dec, 3rd-Fri expiry, roll ~8d prior. Current front month: **MNQM6**.
- **IBKR paper account: DUQ prefix** (DUQ331660). Gateway host-reachable at `127.0.0.1:4002` (internal `ib-gateway:4004`).
- **TF P&L from `lifecycles.pnl_net` is trustworthy.** The bot's own fills.
- **SeanBot dashboard P&L = operator-seeded anchors** in `dashboard/seanbot_authoritative.py` (2026-05-26 +2108.18 / 05-27 +1380.94 / 05-28 −616.20 / 06-01 −866.72 / 06-02 −202.96; later days fall back to a lossy per-exit estimate flagged "est."). SeanBot posts NO daily summary; capture began mid-05-28.
- **New load-bearing (this session):**
  - **OCA-grouped orders are not modifiable (Error 10326 → cancel).** Evidence: `ERROR 10326, reqId 110: OCA group revision is not allowed` immediately followed by `Canceled order (id=110)` in the 09:23Z ratchet log.
  - **`has_new_bar` semantics:** `bars[-1]` forming / `bars[-2]` settled (ib_async `wrapper.historicalDataUpdate` appends then emits).
  - **A redeploy clears an in-memory halt** (§0.5.202).
  - **SeanBot's live entries span −77..+34 from the MA;** his shared 2026-05-19 code (`ma_bounce.py`, band `[−15,+5]`) does not reproduce them → his live rule is unknown/newer.

---

## 5. Wrong diagnoses — READ BEFORE YOU DEBUG

1. **Biggest one — wrong PROBLEM framing for most of the arc.** We treated the goal as "invent/refine a strategy that beats SeanBot" (BT-1..BT-5) and then "align TF's *entry* to SeanBot's." Both were the wrong target. The bot was taking reasonable trades and *giving the profit back through a broken exit*. Evidence that should have redirected us sooner: TF announced "+50 locked" and exited at base for a loss, repeatedly — visible in the operator's Telegram feed. **You cannot out-enter a broken exit.**
2. **STABILIZE-3 brief assumed the bot was flat.** It was holding a live naked −2 short (the oversell artifact). Always confirm broker truth (`reqAllOpenOrders` across clients) before trusting a "flat" assumption.
3. **Briefly mis-read the position as "naked" from a single clientId.** `openTrades()` on clientId-97 showed 0 orders; the orchestrator's GTC stop lived under clientId-1. Use `reqAllOpenOrders` for protective-stop checks.

**Lesson for next session (the meta-pattern):** When a bot running a *proven* strategy bleeds, suspect **execution/exit correctness before re-deriving the strategy.** Diagnose from **broker truth (fills, order-event history) > internal state/Telegram > backtest theory.** The working dashboard — not more backtests — is what surfaced the bug. Keep the strategy-invention shelf shelved.

---

## 6. Verification block — run before doing anything

**V0 — deployed commit + config**
```bash
git -C ~/tradeflow fetch origin && git -C ~/tradeflow log --oneline -1 origin/main
docker inspect tradeflow-app --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E "TRADEFLOW_COMMIT|EXIT_MODE"
```
Expect `origin/main` = `f89b6e0` (or later if #104 merged); `TRADEFLOW_COMMIT=f89b6e0`, `EXIT_MODE=trailing`. If the commit differs from the running container, a redeploy is pending.

**V1 — broker truth (the critical one)**
Run the read-only clientId-97 probe (portfolio + `reqAllOpenOrders` across clients).
Expect: **FLAT with 0 resting orders**, OR a tracked LONG with **exactly ONE SELL STP, ungrouped (ocaType=0), zero SELL LMT.** If you see a SHORT, an oversized position, or a position with no matching ACTIVE lifecycle → a foreign position exists (PR #104's job; until merged, flatten manually with operator approval).

**V2 — code truth (is the fix actually in the running image?)**
```bash
docker exec tradeflow-app grep -c "STABILIZE-3" /app/src/execution/bracket.py /app/src/execution/router.py /app/src/execution/reconciler.py
```
Expect non-zero counts in all three (bracket=1, router≈4, reconciler≈2). Zero ⇒ the deployed image predates #103 — do NOT trust the exit until redeployed.

**V3 — PR #104 status**
```bash
gh pr view 104 --repo ohad-oren111/tradeflow --json state,mergedAt,headRefName
```
Expect `OPEN` unless merged this session. If merged, confirm it was deployed (V0/V2) and re-run V1.

**V4 — recent trading + halt state**
```bash
docker logs tradeflow-app --since 2026-06-03T15:00:00 2>&1 | grep -iE "ENTRY|EXIT|STOP MOVED|halt_raised|foreign|ratchet" | tail -30
```
Watch for the **first post-#103 trade**: a ratchet `STOP MOVED` should be followed (on exit) by a fill **near the ratcheted level**, NOT a round-trip to base. That is the live confirmation the bleed fix works.

---

## 7. Pending work queue (priority depends on V1/V3, not this order)

### PR #104 — foreign-position auto-flatten guard (OPEN, awaiting merge)
AUDIT auto-liquidation. Built + 637 tests green. Decision needed: merge + deploy, or hold. If merged, deploy then verify (V0/V2) and confirm the must-NOT-flatten behavior on the next live entry (it must not flatten a just-opened tracked long). Debounce ≈ `confirm_ticks(2) × full_scan_interval(300s)` ≈ 10 min exposure before auto-flatten — lower `foreign_flatten_confirm_ticks` if faster liquidation is wanted (loses consecutive-confirmation safety).

### Live money gate (highest verification priority)
Watch the first post-#103 trade end-to-end: entry places an **ungrouped, modifiable STP**; on the first ratchet the **resting STP `auxPrice` equals the announced level**; the trade exits near the ratcheted level; never-naked. This proves STABILIZE-3 in the wild.

### Measure, then (only then) optimize
After a few clean live trades, compare TF live vs the touch-rule backtest (PF ~1.05) and vs SeanBot on the dashboard. Optimize in small increments — do NOT reopen strategy invention.

### PARKED — entry alignment with SeanBot
TF agrees with ~46% of his entries; his live rule is wider than his shared code and unknown. **Blocked on the operator getting his friend's current entry rule.** Do not widen TF's gate blind (over-fire trap). Only after the exit is proven to hold over several trades.

### Lower priority / debt
- SeanBot daily-summary ingestion if he ever posts one (dashboard still seeded anchors).
- The runtime "phantom $0.00 reconcile" close flag (separate item, non-distorting).
- `_confirm_stop_at_aux` robustness: it re-reads the same Order object identity; reliably catches the observed 10326 (order-gone) case, but a non-cancelling rejection that doesn't revert aux could pass on stale local state. A permId round-trip would be fully broker-authoritative (out of scope for #103).
- Untracked `docs/tf_research*.zip` in the repo working tree (harmless clutter).

---

## 8. Test safety — why we belabor this (carry-forward)

Cumulative failure modes to keep guarding against: tests passing against a fictional schema (mocked column names); `side_effect` list wrong-count → silent StopIteration → wrong assertions; mocking the raw library chain when prod uses a wrapper; shared MagicMock state leaking between tests. **This session's addition:** assert the broker MODIFY is actually issued (mock the broker, assert `place_order`/modify called with the new aux) — a status-only or state-only assertion would have passed while prod was broken. Weight position-guard tests toward the **must-NOT-act** cases (don't flatten a legit just-opened position; don't announce an unconfirmed stop).

---

## 9. Pitfalls from prior sessions (re-verify quantitative claims)

- The dashboard "TF leads" was bogus (over-capture); now corrected with seeded anchors. Don't trust reconstructed SeanBot P&L.
- "The position is naked" can be a single-clientId artifact — check `reqAllOpenOrders`.
- "The bot is flat" / "the bot is halted" can be stale — a redeploy un-halts (§0.5.202); confirm from broker truth.
- A "harmless" comment in code (the single-member OCA group) hid the whole bleed. Distrust "harmless".
- **If a claim is quantitative (P&L, position size, AGREE %, order count), re-verify from the broker/DB before acting.**

---

## 10. Session discipline lesson (2026-06-03)

The session's meta-failure was **solving the wrong problem for a long time** — strategy research and entry-alignment — while the money leaked through exit plumbing. It was the operator reading his own Telegram feed (announced +50, exited at base) that redirected us to the real bug.

**Enforcement rules for next session:**
1. For any "the bot is losing money" report, FIRST reconcile broker fills against announced exits before touching strategy or entries.
2. Broker truth (fills/order events) outranks internal state, Telegram, and backtests for diagnosing live behavior.
3. Keep strategy-invention shelved. The job is run-the-proven-rule + execution correctness + small increments.

---

## 11. Logging verbosity — what to demand from new code (carry-forward)

Every order action logs `[COMPONENT] symbol: action — reason`; every state transition logs old→new at INFO; every swallowed exception logs the specific error + context; ratchet/modify logs the requested aux AND a broker-truth confirmation of the resulting aux; any dedup/select-one logs which row won and why; auto-liquidation alerts BEFORE and AFTER.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill (patch constraints, code quality, test-safety guardrails, known gotchas, "what I got wrong" section). AUDIT-class (order/exit/kill-switch/secrets/broker-state) and especially **auto-liquidation** PRs: build + test + post the diff, then STOP for an operator merge — do not self-merge/deploy.

---

## 13. In-flight item — PR #104 (already built; merge/deploy/verify steps)

PR #104 is open, not a brief to write. To land it:
~~~
# 1) Merge (operator decision)
gh pr merge 104 --repo ohad-oren111/tradeflow --squash --delete-branch
# 2) Sync + deploy (in-process dashboard/reconciler → recreate restarts the bot; do it while FLAT)
git -C ~/tradeflow checkout main && git -C ~/tradeflow pull --ff-only origin main
GIT_COMMIT=$(git -C ~/tradeflow rev-parse --short HEAD) docker compose -f ~/tradeflow/docker-compose.yml build tradeflow-app
GIT_COMMIT=$(git -C ~/tradeflow rev-parse --short HEAD) docker compose -f ~/tradeflow/docker-compose.yml up -d --force-recreate tradeflow-app
# 3) Verify: V0 (commit matches), V2 (STABILIZE-4 markers / reconciler), V1 (broker flat), and that a just-opened tracked long is NOT flattened.
~~~
Note: recreate un-halts the bot (§0.5.202); if you want it parked, re-halt after.

---

## 14. Canonical references (in order of authority)

1. **Source on `main` @ `f89b6e0`** (or later) — what actually runs.
2. **Broker truth** — IBKR via ib_async clientId-97 `readonly`, `reqAllOpenOrders` across clients + fill/exec history — truth for position/orders.
3. **Production Supabase** (service role) — `lifecycles` (TF P&L), `seanbot_signals` (raw captures).
4. **This handoff (v19)** — session context, NOT long-term authority.
5. **v18 and earlier** — historical; ignore any claim contradicting 1–3.

---

## 15. First 15 minutes of the next session

1. Read §0.5, §1 (focus), §2, §5 of this handoff. §5 is the most important to internalize.
2. SSH in; run the §6 V-block (V0–V4). Confirm deployed commit, broker truth, and that the STABILIZE-3 markers are in the running image.
3. Confirm whether a trade happened since handoff and whether its ratcheted stop **held at the announced level** (the live money gate). If yes — the bleed fix is confirmed; say so.
4. Decide PR #104 with the operator (merge + deploy via §13, or hold).
5. If clean: measure live performance vs the backtest/SeanBot for a few sessions. Do NOT reopen strategy invention or widen the entry gate.
6. Entry alignment stays PARKED until the operator supplies SeanBot's current entry rule.

---

## 16. How to publish this handoff

**Path A — one-block SSH publish (operator runs locally).** See `publish_handoff_v19.sh`: SSH to `tradeflow@5.78.212.37`, branch `docs/handoff-v19`, write the file, commit, push, open a PR, poll checks, squash-merge, resync. Branch protection blocks direct push to main, so it goes through a PR. Commit message: `docs: add v19 handoff (stabilize arc: forming-bar + OCA-stop + dashboard; PR104 queued)`.

**Path B — manual scp fallback:**
```bash
scp HANDOFF_v19.md tradeflow@5.78.212.37:/tmp/HANDOFF_v19.md
ssh tradeflow@5.78.212.37 'cd ~/tradeflow && git checkout -b docs/handoff-v19 origin/main && cp /tmp/HANDOFF_v19.md docs/handoffs/HANDOFF_v19.md && git add docs/handoffs/HANDOFF_v19.md && git commit -m "docs: add v19 handoff (stabilize arc: forming-bar + OCA-stop + dashboard; PR104 queued)" && git push origin docs/handoff-v19 && gh pr create --base main --head docs/handoff-v19 --title "docs: add v19 handoff" --body "v19 session handoff" && gh pr merge docs/handoff-v19 --squash --delete-branch'
```

The handoff exists only once committed to `origin/main`. Until then, treat the chat output as draft.

---

*End of handoff v19. Target lifespan: until STABILIZE-3 is confirmed on several live trades and PR #104 is resolved. Then rely on source @ main + whatever v20 captures.*

