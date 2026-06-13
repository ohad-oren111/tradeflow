# TradeFlow — Handoff v26 (gate proven HEALTHY; SB-trigger DISABLED; gated strategy is at n=0; two N=2 order bugs fixed + deployed)

*Handoff from end of 2026-06-10. Bot is FLAT and correctly dormant below-trend. Deployed HEAD `cc09c539`. The big correction this session: both prior "post-gate wins" were SeanBot-trigger replicate trades that BYPASSED the regime gate — the gated strategy has taken ZERO trades since the gate went live (n=0, not n=1). The SB-trigger path is now OFF, so TF trades nothing below-trend for the first time. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read sections 1–6 first — state of the system as of handoff. Sections 7–13 are reference. Section 14 ranks authority when this doc disagrees with a live observation: source on `main` at `cc09c539` or later wins.

**Do not trust this doc alone.** Run the §6 verification block before any code. **Critical first fact to internalize: the gated strategy is at n=0. The two "+$165.28 / +$97.26 wins" were SB-trigger copies, not gated trades. Do not record either as a gated-strategy data point (§5).**

---

## 0.5 Standing rules (permanent — do not remove)

**Copy-paste instruction style.** Every action recommended to the owner is a copy-paste-ready bash block, env sourced in-block, expected output described immediately below, decision tree if more than one branch matters. No "you might want to…".

**Learning-delivery discipline.** Every new fact (bug pattern, corrected assumption, env fact, diagnostic finding) surfaced immediately as a markdown snippet for the running handoff queue, not at end-of-session.

**Read before diagnosing.** Read the full startup log + 3–5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating ("capital at risk", "naked position"), hit the broker API / DB / raw log, not aggregated metrics.

**Always draft a VPS smoke-test runbook after PR merge** unless told otherwise.

**Project standing rules carried forward (verbatim):**
- §0.5.97 probe-before-specify external specs against source. §0.5.98 broker/exchange state is ground truth, not DB tables. §0.5.99–105 VPS CC bash discipline (no `cd X &&`, no `;`, no `$(...)`, no `${VAR}`, no heredocs; Write tool for content; `git -C`; `--body-file`; edit EXISTING files with the edit tool, never `cat >>`).
- §0.5.205 **The ratchetable trailing STP cannot be OCA-grouped** (IBKR Error 10326 on OCA modify). Orphan protection is via the reconciler broker-truth sweep, NOT OCA. (Validated this session — see §5.)
- §0.5.207 NEVER-ORPHAN: no protective order may survive its position going flat.
- §0.5.209 force-recreate is safe when the bot is gated-dormant (not halted).
- §0.5.223 a control firing ≠ effective — measure coverage. §0.5.224 concurrency has DB-layer preconditions. §0.5.225 read live config from the env-derived `RISK` singleton, not `RiskParams()` defaults.
- **TradeFlow autonomy contract:** every CC work order is end-to-end (implement → test → ship PR → self-merge AUTO on green → deploy → verify from broker/DB truth → report once). Pause only for AUDIT (order/strategy/kill-switch/secrets/broker-state), strategy-param calls, or external blockers. Operator = hands-off PM, single-word approvals. Do NOT suggest `--dangerously-skip-permissions`.

---

## 1. Where we are (as of handoff, 2026-06-10 ~18:00 UTC)

### Live production state
- 3 containers healthy (`tradeflow-app` recreated 17:54Z on the new image, `tradeflow-ib-gateway`, `tradeflow-telegram-listener`).
- **Broker: FLAT** (DUQ331660, MNQM6 position 0.0, realizedPNL field 135.78 from the 06-10 episode). 0 resting orders / orphan canary clean.
- Regime gate **enabled + armed + blocking** every eval (`noop_regime`, price ~28,700 ≤ 30m-EMA200 ~29,480, ~2.6% below-trend).
- **SB-trigger path OFF** (`SB_TRIGGER_ENABLED=false`). N=2 cluster mode ON.
- No manual overrides. No open PRs.

### What just shipped (all merged; deployed HEAD = `cc09c539`)
- **#136** (`9cdde66`, AUTO) — Phase-14 SeanBot fill reconciliation (`tools/eval/`). Verdict **LEAD-REAL**.
- **#137** (`c4ce3f7`, AUTO) — daily health-check hardening (docker-disk/log-cap, OOM/swap proximity, restart drift, bar-freshness). Live 10/10 green.
- **#138** (`4e3ab81`, AUDIT, deployed) — kill-switch log hygiene (per-poll collapse line INFO→DEBUG; per-eval streak DEBUG line; byte-identical verdicts).
- **#139** (`62573b2`, config, deployed) — disable SB-trigger via compose literal `SB_TRIGGER_ENABLED: "false"`.
- **#140** (`66c4fda`, AUTO) — dashboard SB dedup re-keyed on `(price, pnl_points)`.
- **#141** (`525bea4`, AUDIT, deployed) — multi-lot qty accounting: `_extract_fill` now sums across all executions; reconciler entry-settle grace (`foreign_flatten_entry_settle_sec=90`).
- **#142** (`cc09c539`, AUDIT, deployed) — NEVER-ORPHAN broker-truth sweep `_sweep_orphan_protective_orders`.

### What we discovered this session (the load-bearing findings)
- **The SB-trigger replicate path (PR #107, `_maybe_enter_on_seanbot`) was entering real positions that BYPASS the regime gate.** Both "post-gate wins" came from it. **The gated strategy is at n=0.** Now OFF.
- **GATE-HEALTHY** (PART-0 diagnostic): live 30m-EMA200 matches an independent recompute to −0.001 pt mean; current block legitimate (−669.78 pt / −2.27%); duty cycle **61.15% open over 27 months** (Apr-26 94.8%, May-26 83.2%); exact last gate-open **2026-06-04 19:58Z** → continuously below-trend ~6 days; **17/17** SB entries since 06-05 were below-trend. The dormancy is the market, not a broken filter. **Do not loosen the gate.**
- **Phase-14 verdict LEAD-REAL:** SB's prints are not phantom (roll-aware median |Δ| ~14 pt, unbiased once the early-June→Sept contract roll is attributed per entry). 81% of SB entries above-trend (TF-achievable); the 19% below-trend re-price NEGATIVE on the real tape → corroborates GATE-CORRECT. **Mechanism verdict, NOT a dollar audit** — only 37/70 round-trips had a parsed exit; SB's lead lives in uncaptured trips + TF's own pre-gate drawdown.
- **06-10 SB blowup:** SB ran 3 concurrent below-trend longs, all force-flattened @28954.88 for ~**−$4,250** in one event — exactly the correlated-cluster the gate keeps TF out of. TF copied one via the SB-trigger path (+135.78 broker / +97.26 booked) and tripped the two N=2 order bugs now fixed.

---

## 2. The session's work thread

1. Kickoff: ran §6 verification + two anomaly probes from the v25 tape. Confirmed N=2 starting state; kill-switch consec counter is consec-from-newest (benign); #127 feed fix in image but still unproven against a real gap.
2. Window=1 decision → operator chose **LEAVE** (acceptable at N=2; §13 stays the N=3 gate).
3. SB code arrived → Phase-14 reconciliation (#136). First surprise: SB entries reading +200–365 pt above TF's tape → **probed as a contract roll** (SB on Sept MNQU6, the study fetched June MNQM6) → made the study roll-aware → verdict LEAD-REAL.
4. Shipped #137 (health hardening) + #138 (kill-switch log hygiene; the session SSH-broke mid-PR on a `cat >>` heredoc — restarted, recovered via the Write/edit tools).
5. Operator asked: "are we capturing all of SB's notifications / is the data trustworthy?" → **integrity probe**. Found: SB capture is essentially complete (manual-flattens parse fine); the **dashboard undercounts** concurrent-exit days (dedup collapsed 3 distinct flattens); and the +97.26 was **not** a gated trade.
6. Root of it all: the **SB-trigger path bypasses the gate**. Operator chose **disable now** → #139.
7. Operator pushed: "is the gate blocking everything / is it broken?" → PART-0 gate-health diagnostic → **GATE-HEALTHY** (61% duty cycle, last open 06-04).
8. Order-bug root causes probed from DB truth (lifecycle `1f78453e`), not the briefs: qty mis-record (#141) + duplicate-armed orphan stop (#142). Both built green, paused, then operator approved **merge both** → merged (conflict only in the test file, both blocks kept), one rebuild+deploy, verified.

**Closed rabbit holes:** the "foreign position" was NOT an external/shared-account source (all fills clientId 1 — it was TF's own contract misclassified); the +200pt SB divergence was NOT a phantom feed (contract roll); "manual flatten" is NOT unparsed.

---

## 3. What the system is made of

**Single source of truth:** `MEMORY.md` index on the VPS + source on `main` at `cc09c539`.
- Two TF entry paths: (1) **gated strategy** `sma100_bounce` + 30m-EMA200 regime gate (dormant, n=0); (2) **SB-trigger replicate** `_maybe_enter_on_seanbot` (PR #107) — **now OFF**, and it bypasses the gate when on.
- Observational, independent of the trade paths: `seanbot_reconciler` (classifies SB entries vs the gate), shadow ledger, `tools/eval/` research phases (Phase 12 gate-calibration, Phase 13, Phase 14 reconciliation).
- Risk controls: regime gate; kill-switch (consec-loss from-newest + cluster-collapse, warn@6/halt@10, `cluster_window_bars=1`); reconciler foreign-position flatten + the new orphan sweep.
- DB: Supabase `lifecycles` + `lifecycle_events` + `seanbot_signals` + `signal_reconciliations`. Index `lifecycles_one_open_per_setup` live (N=2 precondition).

---

## 4. Verified facts (2026-06-10) — DO NOT challenge unless schema migrates

- **The SB-trigger path bypasses the regime gate by design.** It dispatches straight to `_handle_trade_signal`, never calling `_regime_ok`. Disabled via `SB_TRIGGER_ENABLED=false` (compose literal). **Do not re-enable without gating it to above-trend first.**
- **Gated strategy is at n=0.** No gated-strategy entry has ever filled. The live-edge bar (PF>1.2 over 20–30 above-trend gated trades) has NOT started. v25's "first post-gate qualifying trade +165.28 / n=1" was a **misattribution** — that was an SB-trigger copy.
- **§0.5.205: the trailing ratchet STP cannot be OCA-grouped** (Error 10326). OCA is not an option for orphan protection on the ratchet leg.
- `_extract_fill` now **sums shares across all executions** (VWAP price). Previously read only `fills[-1]` → a 2-lot order filling as two 1-lot executions recorded `entry_qty=1`.
- `RISK` attribute names: **`contracts_per_trade`** (not `contracts`), **`kill_switch_cluster_mode`** (not `cluster_mode`), `foreign_flatten_entry_settle_sec=90.0`.
- **Gate duty cycle ~61% over 27 months** — healthy; current dormancy is a genuine downtrend (last open 2026-06-04 19:58Z).
- **Dashboard SB P&L is partly estimated** ("est." days ≈ 34% of cum). Dedup now keys on `(price, pnl_points)` — distinct concurrent closes survive; identical re-announcements still collapse. Tradeoff: pnl-drift re-announcements are now over-counted rather than risk hiding a real loss.
- Container `docker logs` retain only ~18h and any force-recreate resets them — reconstruct incidents from Supabase + an IB clientId-99 probe, not logs.

---

## 5. Wrong diagnoses — READ BEFORE YOU DEBUG

This session corrected several assumptions (most were in the chat-tier briefs; CC grounded them from truth):
- **"+97.26 / +165.28 were gated-strategy trades" (carried from v25)** → both were SB-trigger replicates that bypassed the gate. Evidence: digests showed `long_signal=0, regime=59` blocked the whole hour; `signal_reconciliations` showed the SB signal classified `MISS-regime` ~30s before each TF entry.
- **"Foreign position = shared account / external source"** → all fills carried clientId 1; it was TF's own second contract misclassified by the qty bug. Evidence: the full execDetails ledger, all clientId=1.
- **"+200–365 pt SB divergence = phantom feed"** → contract roll (SB on Sept MNQU6, study fetched June MNQM6). Evidence: re-fetching MNQU6 collapsed the deltas to ±82 pt.
- **"Manual-flatten exits unparsed"** → all parsed `parsed_ok=true`. The parser keys on `EXIT` + `Closed @ <price>`.
- **PR-2 brief's OCA-group fix** → conflicts with §0.5.205 (ratchet can't be OCA-grouped). CC built the broker-truth sweep instead.

**Lesson:** every wrong turn this session came from a chat-tier hypothesis stated as fact in a brief; CC was right to ground each against DB/broker truth before coding (§0.5.98). Keep briefs hypothesis-flagged, not asserted.

---

## 6. Verification block — run before doing anything

Run inside `tmux tf1`. CC bash discipline applies.

**V0 — FLAT + gate blocking**
```
docker logs --since 5m tradeflow-app 2>&1 | grep -Ei "regime gate BLOCKED|position=" | tail -8
```
Expect: `regime gate BLOCKED entry: price=… <= 30m EMA200=…` firing ~every minute; `position=0.0`. If a position is open or evals stopped, STOP and diagnose (check for CME maintenance break 21:00–22:00 UTC before assuming a fault).

**V1 — live RISK config (read RISK, not defaults — §0.5.225)**
```
docker exec tradeflow-app python -c "from config.risk_params import RISK; print('sb_trigger', RISK.sb_trigger_enabled, '| regime', RISK.regime_gate_enabled, '| max_conc', RISK.max_concurrent, '| cluster', RISK.kill_switch_cluster_mode, '| contracts', RISK.contracts_per_trade, '| settle', RISK.foreign_flatten_entry_settle_sec)"
```
Baseline: `sb_trigger False | regime True | max_conc 2 | cluster True | contracts 2 | settle 90.0`. Any deviation means an unexpected redeploy.

**V2 — deployed commit**
```
docker inspect --format "{{range .Config.Env}}{{println .}}{{end}}" tradeflow-app | grep -iE "MAX_CONCURRENT|CLUSTER|SB_TRIGGER|COMMIT"
git -C /home/tradeflow/tradeflow log --oneline -1 origin/main
```
Expect `TRADEFLOW_COMMIT=cc09c539…`, `MAX_CONCURRENT=2`, `KILL_SWITCH_CLUSTER_MODE=true`, `SB_TRIGGER_ENABLED=false`; origin HEAD `cc09c539` or later.

**V3 — DB index (operator-run SQL; CC has no SQL channel)**
```sql
select indexname from pg_indexes where tablename='lifecycles';
```
Expect `lifecycles_one_open_per_setup` present.

**V4 — suite + lint green**
```
/home/tradeflow/tradeflow/.venv/bin/python -m pytest /home/tradeflow/tradeflow/tests/ -q
```
Baseline: 838 passed. black `--check` + ruff clean on tracked tree (ignore untracked `docs/*.zip`, `research/`).

**V5 — new order-safety paths live**
```
docker logs --since 8m tradeflow-app 2>&1 | grep -Ei "full_scan_complete|orphan|foreign" | tail -8
```
Expect a `full_scan_complete — non_closed=0 actions={}` tick (~every 300s) — orphan sweep runs and finds nothing on a clean book. No `naked`/`ERROR`/false foreign-flatten lines.

---

## 7. Pending work queue

### Arming-path dedup (PR — AUDIT) — PR-2's root-cause follow-up
PR-2 (#142) sweeps an orphan stop only once the symbol is **broker-flat**. It does NOT cover the N=2 same-symbol case where one leg closes while the other stays open. The real fix: **cancel the prior protective stop before re-arming** so the duplicate stop is never created. Prerequisite-quality before sustained N=2 above-trend trading. Not urgent (TF dormant). AUDIT — pause.

### Window=1 cluster recalibration (§13 brief, from v25) — AUDIT, the N=3 gate
Decision stands: **leave window=1 at N=2.** Treat the §13 recalibration (Option A, exit-proximity keying) as the hard prerequisite before any N=3 move. Do not code until operator picks N=3.

### Live edge bar — n=0, waiting on regime
The gated strategy needs an **above-trend window** to fire its first real trade (n=0 → n=1). Bar: PF>1.2 over 20–30 above-trend gated trades. Cannot be forced; gate opens when price reclaims the 30m line (~2.6% up from here).

### Passive watch
- #127 feed-gap escalation still **unproven against a real post-deploy gap** (no gap has hit since `c4a6b06`). Watch the next CME reopen / feed flap.
- Shadow-ledger review at `n_paired ≥ 20` (currently low).

### Operational
- Untracked `docs/tf_research*.zip` + `research/` — leave or gitignore eventually.

---

## 8. Test safety — why we belabor this
Carry forward the 8 guardrails (hand-built tapes/telemetry; correct `side_effect` counts; mock the wrapper not the raw library; no shared mutable mock state; assert real return shapes; no fictional-schema mocks). This session: PR-3 router tests had to set `fill.execution.price` on the MagicMock fixture (real Executions carry `.price`, read before `.avgPrice`) — a wrapper-vs-raw mismatch caught pre-merge. Do not ship tests that skip the guardrails.

---

## 9. Pitfalls from prior sessions
- Don't trust handoff/dashboard numbers without re-query — the dashboard undercounted 06-10 by ~$2,700; SB cum is ~34% estimate.
- Don't assume "dormant" means "no trades" — the SB-trigger path was trading while v25 called the bot dormant.
- Don't read fills from `fills[-1]` — multi-lot orders fill in parts.
- Don't OCA-group the ratchet stop (§0.5.205).
- A `regime_ok=False` everywhere is NOT evidence of a broken gate — confirm duty cycle on the tape (it's ~61% historically).

**Next-session rule: if a claim is quantitative (P&L, duty cycle, orphan count, n-of-trades), re-verify it from source.**

---

## 10. Orchestrator (chat-tier) log — 2026-06-10

*Chat-tier PM notes, per operator request: what I got wrong, what I corrected, lane discipline, and the honest edge picture.*

**What I got wrong**
- I carried v25's misattribution forward and told the operator the Jun-8 +$165.28 was "the first post-gate qualifying [gated] trade, n=1 toward the live-edge bar." It was an SB-trigger copy. The tell was in front of me — every digest that hour showed `long_signal=0, regime=59 blocked` — and I didn't reconcile "a gated trade happened" against "the gate blocked everything." I only caught it when the integrity probe forced the question.
- I wrote integrity-probe brief hypotheses as near-assertions ("expect foreign adoption", "manual-flatten likely unparsed"). CC overturned all of them from DB/broker truth. Lane error: a chat-tier guess in a brief reads as a premise.
- I recommended OCA-grouping for the orphan fix without checking §0.5.205 — the ratchet STP can't be OCA-grouped (Error 10326). CC caught it and built the broker-truth sweep instead.

**What I corrected / did right**
- When the operator worried the gate "blocks everything," I did not reassure — I had CC measure the duty cycle (61% over 27 months, last open 06-04) and prove GATE-HEALTHY from the tape.
- I surfaced the SB-trigger gate-bypass as the ROOT, not just the three downstream order bugs. Fixing the bugs alone would have left TF copying SB's −EV below-trend trades "safely." Disabling the path was the real fix.
- Held both AUDIT order-bug PRs at the pause gate; merged only on the operator's single word.

**Lane discipline (held)**
- Stayed chat-tier throughout: strategy, briefs, decisions, handoff. CC did every implementation end-to-end in tmux. I never edited code or drove the VPS.
- Every strategy-param move (leave window=1, disable SB-trigger, merge #141/#142) went to the operator as a single-word decision, not a unilateral call.
- Did not default to "we wait" — spent the dormant window on the reconciliation, the gate-health proof, and the order-bug fixes.

**Honest paper / edge note**
- This is a PAPER account (~$1M paper NetLiq). The 06-10 naked short and self-flatten were paper, but the bugs would behave identically on live capital.
- **TF has no demonstrated live edge yet.** The gated strategy is at n=0 — it has never traded. Every dollar of TF P&L this window came from the now-disabled SB-trigger path (one lucky +135.78 broker on a −EV copy).
- Do NOT read 06-10 as "TF beat SB." SB lost ~$4,250 on a below-trend cluster; TF didn't win — it correctly stayed out. SB's dashboard lead is real money but partly estimated and partly variance on trades the gate correctly blocks. "We're beating SB" is not established and won't be until the gated strategy trades an above-trend window (n→1) and clears the PF>1.2 bar.

**Enforcement rules for next session:**
1. Briefs flag hypotheses AS hypotheses; CC grounds them against DB/broker truth (§0.5.98) before implementing.
2. Reconstruct incidents from Supabase + IB clientId-99 probe — container logs are ~18h only.
3. Edit existing files with the edit tool, never `cat >>` heredocs (caused an SSH break this session).
4. Check standing rules (§0.5) before recommending a mechanism (the OCA miss).

---

## 11. Logging verbosity — demand from new code
Every order action logs `[COMPONENT] symbol: action — reason`; every state transition old→new at INFO; every swallowed exception logs the specific error + context; dedup/select-one-of-many logs which row won and why; the kill-switch logs the consec-streak every eval at DEBUG (added #138). Per-poll diagnostic spam stays at DEBUG, not INFO.

---

## 12. Master template — use for every CC PR
See the `code-pr-brief` skill (patch constraints, code quality, 8 test-safety guardrails, known gotchas, "what I got wrong" post-PR section).

---

## 13. Current PR brief in flight (hand to CC verbatim when operator greenlights)

This is the queued AUDIT follow-up to PR-2 (#142). PR-2's sweep is only the flat-book backstop; this prevents the duplicate protective stop from ever being armed. **Operator must approve before merge (AUDIT). Do not start until greenlit.**

# TradeFlow — Claude Code PR Prompt: PR 143 — Arming-path stop dedup (one protective stop per lifecycle, always)

## Role
You are a senior Python developer on TradeFlow, an autonomous MNQ futures PAPER-trading bot on IBKR (DUQ331660) — bugs here would cost real money on a live account. You write clean, tested, production-grade code, never modify files you weren't asked to, and study existing patterns before writing. You log verbosely: `[COMPONENT] symbol: action — reason`. You second-guess your own assumptions and verify against the actual file before coding. The prior session reached this root cause from DB truth (lifecycle `1f78453e`); do not re-diagnose from grep summaries.

## Context
On 2026-06-10 a 2-lot entry's position carried TWO resting protective stops — order 194 (router parent-fill arm) and order 195 (a reconciler ratchet re-arm). The lifecycle tracks a single `stop_order_id`, so 194 was untracked; when the position closed via 195, 194 survived, fired @28988.5, and opened a naked −1 short (flattened by 197). PR-2 (#142, deployed `cc09c539`) added a broker-truth sweep that cancels an untracked stop **once the symbol is broker-flat** — but it does NOT cover the window where one N=2 leg closes while the other is still open, and it is a backstop, not prevention. This PR fixes the root cause: every stop-arm site must cancel any existing resting protective stop for that lifecycle/symbol before placing the new one, so at most one protective stop ever rests per active lifecycle.

## 🏗️ System Architecture & Recent Learnings
- Container: `tradeflow-app` (orchestrator + strategy + router + reconciler).
- Python 3.x async. DB: Supabase (`SUPABASE_SERVICE_ROLE_KEY`). Broker: `ib_async`, clientId 1 live / 99 read-only probe.
- Logging: `docker logs tradeflow-app`; module LOGGER.

### Key Architecture Constraints
- Runtime: `ib_async` order ops are async; cancel via the existing `cancel_order_by_id` wrapper, our own clientId (no Error 10147).
- Shell/tests: `/home/tradeflow/tradeflow/.venv/bin/python -m pytest`; CC bash discipline (no `cd X &&`/`;`/`$()`/`${VAR}`/heredoc; Write tool; `git -C`).
- **Schema/design — §0.5.205: the ratchet STP CANNOT be OCA-grouped (Error 10326). Do NOT use OCA.** The fix is cancel-before-arm, not grouping.
- Scope boundary: do NOT touch the reconciler orphan-sweep (#142) — it stays as the backstop. Do NOT touch the regime gate, kill-switch, or SB-trigger flag.
- Design decision: (A, recommended) make each arm site cancel the prior tracked stop AND any untracked resting protective stop for that symbol before placing+tracking the new one; (B) funnel all arming through one helper that owns the invariant. Prefer A if the arm sites are few; choose B if there are ≥3 arm sites.

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints
Files you WILL modify (EXACTLY up to 3):
- `src/execution/router.py` (stop arm + ratchet re-arm sites)
- `src/execution/reconciler.py` (re-arm/heal site, if it arms a stop)
- `tests/test_execution_router.py` and/or `tests/test_execution_reconciler.py`

Files you MUST NOT modify: `config/risk_params.py`, the regime gate, `kill_switch.py`, `dashboard/`, secrets, `docker-compose.yml`.

Verification gates: `git -C ... diff main --stat` shows ONLY the files above; `git diff main -- src/execution/kill_switch.py` empty.

### 2. Code Quality
black + ruff clean; one import per line; no signature changes to public methods; log line `[ARM] symbol: cancelled prior stop <id> before placing <id> — reason`.

### 3. Safety
- All pre-existing tests pass (baseline 838). Known failing: none.
- No unexpected DB writes or order placements in tests.
- If you find an adjacent bug, DOCUMENT in the PR; do not fix.

## 🧩 Current Mission: guarantee at most one resting protective stop per active lifecycle at all times

### Task A: Audit (write a 3-5 line finding before coding)
Read every site that places a protective STP (initial arm at parent-fill AND ratchet re-arm). Answer: (1) which exact site armed 194 vs 195 on 06-10? (2) does the ratchet place a NEW stop then update tracking, leaving the old one resting? (3) is the old `stop_order_id` cancelled before the new is placed, and is there a window where both are live?

### Task B: Implement
At each arm site, BEFORE placing the new STP: cancel the lifecycle's current `stop_order_id` if set AND any untracked resting protective STP for that symbol/contract (broker-truth check), confirm cancellation, then place the new STP and atomically update `stop_order_id`. Mirror the existing cancel/replace pattern. Never leave two live stops across the re-arm.

### Task C: Add tests
- `test_rearm_cancels_prior_stop_before_placing_new` — entry → arm(194) → ratchet → assert 194 cancelled and exactly one resting stop (the new id) at all times.
- `test_rearm_cancels_untracked_sibling_stop` — simulate an untracked resting STP at re-arm → asserted cancelled.
- `test_n2_two_legs_each_one_stop` — two same-symbol legs → exactly two stops total, one per lifecycle, no orphan when one closes.
- Follow the 8 TEST SAFETY GUARDRAILS (fresh MagicMock per test; explicit return_values; no `side_effect` without count comment; mock the wrapper `self._ib.cancel_order_by_id`, not the raw chain; match the async pattern of a verified neighbor; assert via `call_args_list` filtered by arg).

### Task D: Verify completeness
grep all `place...STP` / `arm` / `stop_order_id =` sites; classify each as covered or N/A. Confirm none missed.

### Task E: Out-of-scope investigation (~10 min, document, do NOT fix)
Whether the ratchet should MODIFY the existing stop in place rather than cancel+replace (fewer order ops, but check Error-10326 interaction).

### Task F: Post-merge smoke test (owner runs)
```
docker logs --since 10m tradeflow-app 2>&1 | grep -Ei "\[ARM\]|orphan|full_scan_complete" | tail -10
```
Expect: `full_scan_complete actions={}` ticks; no orphan/naked lines. STOP if any `naked` or a second resting STP appears for one lifecycle.

## 📤 Expected Output
Files modified (≤3); PR description with the 10 required items incl. Task A finding, Task D grep classification, Task E note, protected-file empty diffs, "This PR does NOT touch the sweep/gate/kill-switch," and "What I got wrong."

## 🔍 Pre-Push Checklist
Code quality (black/ruff/imports/signatures) + the 8 test-safety guardrails + production safety (empty protected diffs, Task D complete, smoke test included, adjacent bugs noted, "what I got wrong" present). AUDIT — build to green, PAUSE for operator approval before merge.

## ⚠️ Known Gotchas (carry forward, never shrink)
1. `SB_TRIGGER_ENABLED` is a compose literal = `false`; do not flip.
2. RISK attrs: `contracts_per_trade`, `kill_switch_cluster_mode`, `foreign_flatten_entry_settle_sec`.
3. Docker restart ≠ rebuild — code changes need `GIT_COMMIT=<sha> docker compose build` + `up -d --force-recreate`.
4. §0.5.205 ratchet STP cannot be OCA-grouped (Error 10326) — cancel-before-arm only.
5. `_extract_fill` sums across all executions (#141) — don't revert to `fills[-1]`.
6. Container logs ~18h only; reconstruct from Supabase + IB clientId-99.
7. CC bash discipline (no `cd&&`/`;`/`$()`/`${VAR}`/heredoc; edit existing files with the edit tool).

---

## 14. Canonical references (authority order)
1. `MEMORY.md` index on `main` at `cc09c539` — system map.
2. Source on `main` at `cc09c539` — what runs.
3. Production Supabase via service role — row/column truth.
4. IBKR via `ib_async` clientId 99 (read-only) — broker truth (positions, fills, hours).
5. `tools/eval/` Phase-12/13/14 outputs — gate + reconciliation findings.
6. **This handoff (v26)** — session context, not long-term authority.
7. v25 and earlier — historical; ignore any claim contradicting 1–5 (notably v25's "first post-gate qualifying trade").

---

## 15. First 15 minutes of next session
1. Read §0.5, §1, §4, §5 (especially the n=0 / SB-trigger-bypass correction).
2. SSH in, `tmux tf1`, run §6 V0–V5. Confirm FLAT, gate blocking, `sb_trigger False`, commit `cc09c539`, suite 838 green.
3. Commit any untracked cleanup if desired (or leave the research zips).
4. There is no urgent code task — TF is correctly dormant. If the operator wants forward motion: either (a) hand CC the arming-path-dedup AUDIT brief (§13), or (b) wait for an above-trend window for the first gated trade. Do not loosen the gate (§4).
5. If a gated trade fires (n→1), capture it for the live-edge bar and watch the first live 2-stack for the cluster-collapse + orphan-sweep behavior.
6. After any merge, invoke the `vps-smoke-test-runbook` skill.

---

## 16. How to publish this handoff

**Path A — VPS Claude Code brief:**
```
You are VPS Claude Code on the TradeFlow VPS. Save the content of HANDOFF_v26.md verbatim to /home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v26.md, then (CC bash discipline — no cd&&/;/heredoc; use git -C and the Write tool for the file content):

  git -C /home/tradeflow/tradeflow add docs/handoffs/HANDOFF_v26.md
  git -C /home/tradeflow/tradeflow commit -m "docs: add v26 handoff (gate HEALTHY; SB-trigger OFF; gated strategy n=0; N=2 order bugs fixed)"

Direct push to main is rejected by branch protection — open a PR instead: create a branch off origin/main with this commit, push it, gh pr create, poll gh pr checks until green, then gh pr merge --squash --admin --delete-branch, and resync main. Confirm the file exists on main, git log shows the squash, and git status is clean.
```

**Path B — Manual fallback (operator drives own shell; heredocs OK here):**
```bash
scp HANDOFF_v26.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v26.md
ssh tradeflow@5.78.212.37 'cd /home/tradeflow/tradeflow && git checkout -b docs/handoff-v26 origin/main && git add docs/handoffs/HANDOFF_v26.md && git commit -m "docs: add v26 handoff (gate HEALTHY; SB-trigger OFF; gated strategy n=0; N=2 order bugs fixed)" && git push -u origin docs/handoff-v26 && gh pr create --fill --base main && gh pr merge --squash --admin --delete-branch'
```

The handoff exists only once saved to disk and committed. Until then, treat this as draft.

---

*End of handoff v26. Target lifespan: until the gated strategy takes its first above-trend trade (n→1) and N=2 runs a clean live 2-stack, or until the arming-path-dedup PR lands. Then supersede with v27.*
