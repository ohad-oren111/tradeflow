# TradeFlow — Handoff v27 (discovery campaign CLOSED · founding edge retired · system FLAT + HALTED)

*Handoff from end of 2026-06-11. The bot is FLAT (0 positions, 0 open orders) and HALTED — it will not take new entries until a human clears the halt. Do not clear the halt or restart trading until the partial-fill fix (§13, queued AUDIT PR) is merged and deployed. This session ran the honest foundational-edge audit that everything was waiting on: **TF's own gated strategy does NOT clear the bar it held every other candidate to.** The discovery campaign (Phase 15→18) is closed with NONE across the board. This doc captures the state, the verdict, the live order bug we caught, and the one fix queued.*

---

## 0. How to use this doc

Read sections 1–6 first — that's the state-of-the-system as of handoff. Sections 7–13 are reference material. Section 14 is the authority order to consult when this handoff disagrees with itself or a live observation.

**Do not trust this doc alone.** Run the verification block in §6 before writing any code. **Critical first action: confirm the bot is still FLAT and HALTED** (V0 below) before taking any action. The system was made safe by hand this session after an order bug; a fresh session must re-ground from broker truth, not from this narrative.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block. Self-contained commands, chained with `&&` or grouped. Source env vars explicitly in the same block. Expected output described immediately below each block, plus a decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**Learning-delivery discipline.** Every time you learn something new — a bug pattern, a corrected assumption, an environmental fact, a diagnostic finding — surface it immediately in the chat, formatted as a markdown snippet the owner can paste verbatim into the running handoff queue. Do not wait until end-of-session.

**Read before diagnosing.** When debugging a complex state bug, read the full startup log and 3–5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency language ("capital at risk", "churning fees", "spiraling"), hit the source of truth — live API (ib_async), live DB (Supabase service role), raw log file — not aggregated metrics.

**Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise. The owner does not run smoke tests by hand.

### TradeFlow project standing rules (§0.5.x — cumulative, carried forward verbatim)

- **§0.5.97 — probe-before-specify.** Probe external specs (broker contracts, exchange fees, schema, library APIs) against the source before baking into briefs. Never re-derive MNQ contract specs from memory.
- **§0.5.98 — broker/exchange state is ground truth, not internal DB tables.** For any position, fill, or capital claim: use the broker API (`ib_async` for IBKR). The DB is a projection and can be stale or wrong (it was wrong this session — see §4).
- **§0.5.99–105 — VPS CC bash discipline.** No `cd X &&`, no `;` separators, no `$(...)`, no `${VAR}`, no heredocs in VPS CC Bash calls. Use the Write tool for file content, `git -C path` instead of `cd`, Python helpers in `/tmp/` for interpolations, `--body-file` for PR bodies. Hardcoded CC safety heuristics that cannot be overridden via settings.json.
- **§0.5.208 — research lane (MEASURE) drives no prod path.** All `tools/eval/` work is research-only: no `src/` change, no deploy, REPORT autonomy, self-merge on green CI. Read-only against the broker (dedicated clientId, never prod 1 / healthcheck 98 / smoke 99).
- **§0.5.223 — risk-gate effectiveness ≠ firing.** A gate firing correctly is not evidence of edge. Measure coverage and net P&L contribution, not just that it blocks.
- **§0.5.224 — concurrency has DB-layer preconditions independent of config.** N>1 needs verified DB indexes/constraints, not just a config flag.
- **§0.5.225 — read live config from the env-derived RISK singleton, not bare `RiskParams()` defaults.** The default `exit_mode` is `fixed`; live is `trailing`. Always read from the container (`docker exec tradeflow-app python -c "from config.risk_params import RISK; ..."`).
- **§0.5.226 (NEW this session) — the honest edge test comes BEFORE infrastructure, not after.** A strategy's founding profitability claim is a hypothesis to be tested under train/holdout/deflated-Sharpe on a clean backtest, not a premise to build on. TradeFlow and Botty both failed the same way: built first, validated the periphery (fidelity, execution, risk-of-ruin), never validated the central edge until forced. If a strategy can't clear the bar on an honest backtest, there is nothing to build. The Phase-16 harness is the artifact that enforces this; point it at the founding strategy FIRST.
- **§0.5.227 (NEW this session) — the entry-fill handler must be single-shot/idempotent across partial fills.** A multi-lot MKT order fills as multiple partial executions; the per-execution callback path is NOT idempotent and races on the DB write (see §5). Any code touching the arm path must gate on full fill (`remaining==0`), not act per partial.

---

## 1. Where we are (as of handoff, 2026-06-11 ~22:50 UTC)

### Live production state
- **Containers:** `tradeflow-app` (healthy), `tradeflow-ib-gateway` (healthy), `tradeflow-telegram-listener` (up 13 days). All green.
- **Position:** FLAT — broker truth `POS: 0`, `OPEN ORDERS: 0` (clientId-99 read-only probe, 22:44Z).
- **Halt:** RAISED at 22:08:50Z, never cleared. `/tmp/halt_clear` absent. Bot will not take new entries. **Leave it halted until §13 ships.**
- **Deployed HEAD:** container recreated 2026-06-11T13:53:25Z on commit **c6bd0a2** (PR #144, cancel-before-arm). PRs #145–149 merged this session are ALL research-only (`tools/eval/`), no `src/` change, so the deployed prod code is still effectively #144. NOTE: `TRADEFLOW_COMMIT=unknown` in the container env — the 13:53 rebuild omitted the `GIT_COMMIT` build arg (cosmetic; flagged, not yet fixed).
- **origin/main HEAD:** f998abe (PR #149, Phase-18 audit, research-only).
- **Kill-switch / cluster:** `KILL_SWITCH_CLUSTER_MODE=true`, N=2 concurrency config live but the bot is halted so nothing is arming.

### What just shipped this session (all research-only except #144 which shipped prior session)
- **PR #145 (d1b4868)** — Phase-15 dormant bake-off. 2 external candidates, verdict NONE.
- **PR #146 (abda246)** — Phase-16 wide discovery, 10 families / 22–24 trials, verdict NONE.
- **PR #147 (d6a009d)** — Phase-17 Stage-1 multi-market DATA gate, 9/9 markets PASS.
- **PR #148 (6ba28a5)** — Phase-17 Stage-2 EWMAC portfolio, verdict NONE (deflation kills champion; DSR 0.210).
- **PR #149 (f998abe)** — **Phase-18 TF FOUNDATIONAL EDGE AUDIT. Verdict: TF's gated strategy DOES NOT CLEAR the bar.** This is the headline of the session.

### What we discovered this session (the load-bearing findings)
- **TF's founding strategy has no demonstrated edge over 27 months under the honest protocol.** Phase-18 drove the REAL live prod entry+exit code (`src.strategy.evaluate_gates`/`_regime_ok` + `trail_manager`, verified live config) over the full tape with the same pre-registered bar that returned NONE for every other candidate. Result: gated full-tape PF **1.176**, train PF **1.126** (< 1.30 → FAIL), exp/ct $7.33. The gate correctly removes below-trend trades but **trims variance, not loss** (maxDD $2,175 gated vs $3,847 ungated); above-trend entries carry the P&L (PF 1.204) but still don't clear the bar after real costs.
- **SeanBot IS TF's strategy, line-for-line.** Operator uploaded SeanBot source this session (`/tmp/sb/` — not on the VPS, was a local chat upload). `ma_bounce.py` = same SMA50/100 bounce, same touch/bullish/ma_order/gap gates, same `_regime_ok` 30m-EMA200 gate (TF inherited this exact function, same comment lineage), SL 75 / TP 150. SeanBot's "$5K→$18,073, +261%" header is a **backtest with NO train/holdout split, NO deflated Sharpe**, reported as a headline RETURN; the file's own OOS note admits PF only 1.07. The 261% = leverage + compounding over a 3-yr NQ bull run, not edge. **SeanBot and TF are one strategy; Phase-18 already tested it; it has no edge.**
- **First-ever live gated entry happened this session (n=0 → n=1)** and immediately hit a NEW order bug (§5). The trade itself closed **+$134 real** (broker truth), but the DB row mis-records `pnl_net=-1.24` because it only ever tracked 1 of the 2 contracts. Broker truth is the record.
- **Cumulative campaign trial count: naive 27 / effective ~25.4.** A future research batch must pass `--prior-trials 27`.

---

## 2. The session's work thread

1. **Phase-17 Stage-1 (DATA gate) shipped clean** (PR #147). 9-micro basket, specs resolved from IBKR via read-only clientId-117 probe (not memory). Caught + fixed a roll-stitch bug mid-build: a first "max-volume within 9mo + monotonic" rule let a noisy deep-month volume print ratchet MCL ~9 months off the true front (−4.73% endpoint error) — caught by the broker-truth endpoint QA, root-caused, fixed to a front/back liquidity-filtered pointer (drift now 0.00%). Mean pairwise |corr| 0.244 → basket genuinely diversified.
2. **Phase-17 Stage-2 (EWMAC portfolio) shipped, verdict NONE** (PR #148). Carver EWMAC vol-targeted, risk-parity-by-bucket (FX as 1 bucket), IDM, explicit 10Y yield-sign, close-to-close MCL. Champion 64/256 train PF 0.978; deflation kills it (DSR 0.210). Only MGC/gold clears per-market. Cost-insensitive → friction isn't the cause; there's no trend edge on this ~25-month window.
3. **Operator interrogated the founding assumption** ("we started TF knowing the math works over time — test that, something's not right"). This was the correct instinct and it drove the session's most important work.
4. **Phase-18 (foundational edge audit) shipped, verdict: TF DOES NOT CLEAR THE BAR** (PR #149). The audit that should have run in week 1. Drove the real prod strategy over 27 months, same honest protocol. Decomposed gated vs ungated to isolate gate-edge from entry-edge. See §1 findings.
5. **The gate reopened for the first time since 2026-06-04** (price rallied back above the 30m EMA200 after the ~7-day downtrend) and TF took its **first real gated entry** at 22:06Z — valid `long_signal`, all gates True, LONG MNQ intended 2 contracts @ vwap ~29,496.88.
6. **The entry tripped a foreign-position event** → `halt_raised` → `foreign_position_detected broker=2 intended=1` → after settle-grace, `foreign_flatten` SELL 1 (order 207 @29518.25). CC diagnosed root cause (§5), broker-truth verified, read-only. NOT #144 failing, NOT #141 — a new partial-fill DB-write race.
7. **Operator chose: flatten 1 + stay halted (live position) + Option A gate-on-full-fill (fix scope).** CC executed the flatten (clientId-99 MKT SELL 1 @29543.75); the bot's reconciler then auto-closed lifecycle 8f2d0afb (ACTIVE→EXITING→CLOSED) and swept the orphan stop 206 itself. Final broker truth: FLAT, 0 orders, halted.
8. **Wrapped:** discovery campaign closed NONE across the board; founding edge retired; system safe; one fix queued (§13).

The goal of this section: a new session understands the campaign is **closed**, the founding strategy is **retired as a money-maker**, and the ONLY open code item is the partial-fill fix. Do not re-open the strategy search expecting a different answer — Phase 15→18 is exhaustive and honest. Do not re-run the edge audit expecting it to pass.

---

## 3. What the system is actually made of

**Single source of truth:** `CLAUDE.md` at repo root (auto-loads for VPS CC every session) + the `docs/handoffs/` series. No single system-map file; this handoff + CLAUDE.md are the best available.

Highlights to save a lookup:
- **Production-live code paths:** `src/strategy/` (`evaluate_gates`, `_regime_ok`), `src/execution/` (`router.py` arm path, `reconciler.py` foreign-position + never-orphan sweep, `trail_manager.py` trailing exit). Entry point: the orchestrator loop in `src/orchestrator.py`.
- **Research surfaces (drive NOTHING live):** `tools/eval/phase15..18/` + `tools/eval/{data,engine,...}`. Data on disk at `research/data/` (untracked, like the NQ tape). These are MEASURE-lane only.
- **State:** Supabase `lifecycles` + `lifecycle_events`. DB is a projection — broker is truth (§0.5.98).
- **Dead/phantom surfaces:** the DB `pnl_net` column is NOT reliable for multi-lot trades (see §4). `TRADEFLOW_COMMIT` env is currently `unknown` (cosmetic).
- **Open documented bug families:** the partial-fill foreign-flatten family (#141 → PR-3 settle-grace → this session's race). §13 fix closes it.

---

## 4. Verified facts (2026-06-11) — DO NOT challenge these unless the schema/contract migrates

- **MNQ contract spec (§0.5.97-verified):** TICK_SIZE=0.25 index points, MULTIPLIER=$2/point ($0.50/tick), COMMISSION_RT=$0.62, MARGIN_REQ=$2000 day-trade. Quarterly Mar/Jun/Sep/Dec, expiry 3rd Friday, roll ~8 days before. Do not re-derive from memory.
- **Live risk config (read from container this session):** `exit_mode=trailing`, `regime_gate_enabled=True`, stop 75 / lock-in 50 / trail 150 / hard-ceiling 1000 / cooldown 10, contracts=2. Read from the env-derived `RISK` singleton, NOT `RiskParams()` defaults (default exit_mode is `fixed` — wrong).
- **Gateway connection:** host `127.0.0.1:4002` → container `4004`. clientIds: prod=1, healthcheck=98, smoke=99. Research probes used 117. Use a fresh dedicated id for any read-only probe; never collide with 1/98/99.
- **NEW load-bearing fact — the DB `pnl_net` is wrong for the n=1 trade.** DB ground truth (queried read-only via the container's Supabase client): lifecycle `8f2d0afb-543c-4084-aff9-ef5d86db4e27` has `entry_qty=1, entry_price=29482.25` (the FIRST partial fill's values), and `pnl_net=-1.24`. **Broker truth is +$134.02 realized** (session realizedPNL). The DB only ever tracked 1 of the 2 contracts because of the partial-fill race (§5). **For any P&L claim, use broker truth, not the DB.**
- **NEW load-bearing fact — Phase-18 verdict.** TF gated strategy: full-tape PF 1.176 / train PF 1.126 (FAIL <1.30) / exp/ct $7.33 / maxDD $2,175. Ungated: PF 1.121 / maxDD $3,847. Above-trend entries: PF 1.204. The gate trims variance, not loss. Evidence: `tools/eval/phase18/RESULTS.txt` on main at f998abe.

---

## 5. Wrong diagnoses (READ BEFORE YOU DEBUG)

The foreign-position event at 22:06Z had **two tempting wrong diagnoses**, both ruled out by evidence before the real root cause was found:

- **Wrong diagnosis A: "PR #144 (cancel-before-arm) failed / regressed."** Evidence that misled: a foreign-position event fired on the first live n=2 entry *after* #144 deployed — looks like #144 didn't hold. **Why wrong:** the raw log shows #144's cancel-before-arm fired *correctly* — it cancelled the stale qty=1 stop (204) and left exactly one stop, no orphan. #144 did its job. Ruled out by reading the full 22:06–22:14 window top-to-bottom, not the grep summary.
- **Wrong diagnosis B: "#141 qty-accounting bug (`_extract_fill`) resurfaced."** Evidence that misled: `intended=1` vs `broker=2` is a qty mismatch, which is #141's signature. **Why wrong:** `_extract_fill` correctly summed to 2 *within one callback* — proven by the 2nd arm's seed price 29496.88 being the vwap of both fills. The qty math is fine.
- **Correct root cause (broker-truth verified):** The 2-lot MKT filled as **two 1-lot partial executions ~50ms apart**. `_on_fill → _handle_parent_fill` (`router.py:503-518`) fires **once per execution** with no full-fill gate; the line-510 cache-refresh is defeated by the ~50ms async gap. Both callbacks ran a full arm + ENTERING→ACTIVE transition + DB PATCH concurrently. The 1st wrote `entry_qty=1`, the 2nd wrote `entry_qty=2`, the PATCHes are unordered, and **the stale qty=1 write landed last** → DB permanently `entry_qty=1`. The STABILIZE-4 guard then read `intended_net=1` (`_signed_intended_qty` reads `entry_qty`) vs `broker=2`, called 1 real contract "foreign", and after the 90s settle-grace + 2-tick debounce sold a genuine contract. **This is the 3rd touch of the same bug family** (#141 → PR-3 → now); the architectural gap is that the entry-fill handler is not single-shot/idempotent across partial fills.

**Lesson for next session:** Both wrong diagnoses came from pattern-matching the *symptom* (qty mismatch / foreign-flatten) to a *known prior bug*. The real cause was a new concurrency path only visible by reading the millisecond-resolution fill sequence in the raw log. Read the full fill→arm→PATCH narrative before blaming a prior PR. (§0.5 "read before diagnosing.")

---

## 6. Verification block — run this before doing anything

All blocks are self-contained. Run from `tradeflow@5.78.212.37`.

**V0 — CONFIRM FLAT + HALTED (the critical first check)**
```bash
docker exec tradeflow-app python -c "from ib_async import IB; ib=IB(); ib.connect('ib-gateway',4004,clientId=96); print('POS:', [(p.contract.localSymbol, p.position, p.avgCost) for p in ib.positions()]); print('OPEN ORDERS:', [(t.contract.localSymbol, t.order.action, t.order.orderType, t.order.totalQuantity) for t in ib.openTrades()]); ib.disconnect()"
# Expect: POS: []   OPEN ORDERS: []   (FLAT, no resting orders)
# If POS shows a position or OPEN ORDERS is non-empty: STOP. The system is not in the safe state this handoff describes. Re-ground from broker truth before any code. Do NOT clear the halt.
```

**V0b — confirm halt persists**
```bash
docker logs tradeflow-app 2>&1 | grep -Ei "halt_clear|halt_lifted|unhalt|resume" | grep -v httpx | tail -3
# Expect: no halt-clear lines after 2026-06-11T22:08:50Z. Bot is parked.
```

**V1 — containers healthy + deployed HEAD**
```bash
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}" && docker inspect --format '{{.State.StartedAt}} {{.Config.Image}}' tradeflow-app
# Expect: 3 containers up/healthy; app started 2026-06-11T13:53:25Z (commit c6bd0a2 / PR #144). Research PRs #145-149 do not change prod code.
```

**V2 — deployed code is #144 (cancel-before-arm present), partial-fill fix NOT yet present**
```bash
docker exec tradeflow-app grep -n "cancel" /app/src/execution/router.py | head -5 && echo "--- full-fill gate check (should be ABSENT until §13 ships) ---" && docker exec tradeflow-app grep -n "remaining==0\|remaining == 0\|status=='Filled'\|orderStatus.status" /app/src/execution/router.py | head -5
# Expect: cancel-before-arm logic present. The full-fill gate from §13 should NOT be present yet (that's the queued fix).
```

**V3 — git state in sync**
```bash
git -C /home/tradeflow/tradeflow fetch origin && git -C /home/tradeflow/tradeflow log --oneline -3 origin/main && git -C /home/tradeflow/tradeflow status --porcelain | head
# Expect: origin/main HEAD = f998abe (Phase-18) or later. Clean working tree (ignore untracked research/data/ + any docs zips).
```

**V4 — n=1 trade record (broker truth, NOT the DB)**
```bash
docker logs tradeflow-app 2>&1 | grep -Ei "realizedPNL=41.51|realizedPNL.*134|8f2d0afb" | grep -v httpx | tail -5
# Expect: lifecycle 8f2d0afb closed; session realizedPNL ~ +$134. DB pnl_net=-1.24 is WRONG (tracked 1 of 2 lots). Broker is truth.
```

---

## 7. Pending work queue

Priority order depends on V0 state, not on this ordering.

### PR #150 (queued, AUDIT) — partial-fill full-fill gate
**Status: blocker for un-halting.** The full brief is in §13. Gate `_handle_parent_fill` on full fill (`remaining==0`); ignore partial executions; confirm the reconciler force-fill backstop still arms a stop for a stuck/never-completing partial. One arm → correct qty, one DB write, correct stop size, correct ratchet id. AUDIT: CC builds to green, operator reviews diff, merge → deploy into the now-FLAT book, smoke-test, then (operator decision) clear the halt.

### Strategic decision (NOT a code item) — what TradeFlow IS now
**Status: open, operator-owned, no rush.** The discovery campaign is closed NONE and the founding strategy is retired as a money-maker. The open reframe the operator has not yet answered: is the goal **building a systematic system** (already won — rare infra + discipline) or **returns** (honest answer for a solo retail operator: self-found edge ≈ 0; the math-backed path is broad index exposure, with systematic trading as a hobby funded by losable money)? Do NOT push the operator toward "try another strategy" — Phase 15→18 is the evidence that the category, not the execution, is the constraint. If the operator wants one last genuinely-different swing, the only un-run market-neutral candidate is MNQ/MES cointegration (see §7 deferred).

### Deferred / un-run
- **MNQ/MES cointegration pair** — the only genuinely-different (market-neutral) candidate left; the one Gemini+ChatGPT converged on for Botty. MES 27-mo backfill was stubbed (`fetch_history.py` body was a `NotImplementedError`); CC deferred firing the long unattended pull. If pursued, it's a fresh research arc under the same honest bar (`--prior-trials 27`).
- **`TRADEFLOW_COMMIT=unknown` cosmetic fix** — re-add the `GIT_COMMIT` build arg to the image build so digests stamp the real commit. Low priority.

### Uncommitted / operational debt
- Untracked on VPS: `research/data/` (intended — like the NQ tape), and any `docs/tf_research*.zip` chat uploads (ignore / clean up).

---

## 8. Test safety — why we belabor this

Carry forward the cumulative list of test-mocking failures prior sessions hit:
1. Tests passed against a fictional schema because they mocked the column names.
2. `side_effect` list had wrong count → silent `StopIteration` → wrong assertions.
3. Mocked at the raw library chain when code uses a wrapper → tests green, prod broken.
4. Shared `MagicMock()` state leaked between tests.
5. Async decorator pattern assumed (`@pytest.mark.asyncio`) without verifying a neighbor.

The §13 brief's Pre-Push Checklist enforces all five. The partial-fill fix is especially exposed to #2 and #3: the test must simulate **two partial executions** with a realistic async gap, and must mock at the wrapper (the `_on_fill`/`_handle_parent_fill` path), not the raw `ib_async` exec callback. A test that fires one full fill will pass while prod still races — do not ship that.

---

## 9. Pitfalls from prior sessions

- "State machine self-cleared zombies" — historically needed manual intervention; verify, don't assume.
- DB `pnl_net` / `entry_qty` are NOT trustworthy for multi-lot trades (proven this session). Re-query broker truth.
- Grep patterns miss writers when only one syntax form is matched — read raw narratives.
- `RiskParams()` defaults (`exit_mode=fixed`) ≠ live config (`trailing`). Always read from the container.
- Timezone confusion: this session a log event was misread as 16:06Z when it was 22:06Z (6h off). Confirm UTC against `date -u` and the raw log timestamp.
- The "+261% backtest" looked like proof and wasn't — a headline return with no honest validation. Do not accept a founding edge claim without running it through the Phase-16 bar.

**Next session rule: if a claim is quantitative, re-verify it. Especially P&L, position counts, open-order counts, and any edge/PF number. Broker + DB service-role queries only — not handoff numbers, not log greps.**

---

## 10. Session discipline lesson (2026-06-11) — orchestrator's logged comments

This is the chat-side orchestrator (me) logging what I got wrong, what I corrected, and the honest notes, per the operator's request.

**What I got wrong (owned, plainly):**
1. **I treated "the math works over time" as an established premise for nine sessions instead of a hypothesis to test.** The founding claim (SeanBot's +261% backtest) arrived *looking* settled, and I built strategy/PR/gate work on top of it without ever insisting the founding strategy itself go through the honest gauntlet. The Phase-18 audit should have been session 1, not this session. The discipline was airtight on the periphery (execution, fidelity, risk) and absent at the center.
2. **I let the same failure mode repeat from Botty.** Both projects: build the infrastructure first, validate everything except the central edge, discover late that the central edge was the one thing needing validation first. I did not flag the pattern early enough.
3. **The operator corrected this, not me.** Ohad's gut ("something's not right, test the assumption") forced the audit. Credit where due — the instinct ran against the grain of how the project was set up, and it was right.

**What I corrected:**
- Ran the foundational edge audit (Phase-18) the moment it was raised, with the same pre-registered bar as every other candidate — no special pleading for TF's own strategy. Reported the verdict bluntly (DOES NOT CLEAR) rather than softening it.
- When the operator uploaded SeanBot source, confirmed line-for-line that SeanBot IS TF's strategy and that the +261% was leverage+compounding on a PF-1.07 backtest, not edge — closing the "maybe SB knows something we don't" thread for good.

**Lane discipline held:**
- All Phase-15→18 work stayed in the MEASURE lane (`tools/eval/`, research-only, REPORT autonomy, read-only broker probes on dedicated clientIds). No research PR touched `src/` or deployed.
- The one prod-affecting action this session (the live flatten) was an explicit AUDIT decision the operator approved single-word, executed read-only-then-one-write, broker-truth verified before and after. The bot was left FLAT + HALTED, the safe state to fix code from.

**Honest paper/edge note:**
- TradeFlow is paper (DUQ331660, ~$1M paper NAV). The n=1 trade was the first real gated execution and closed +$134 broker-truth — but n=1 has zero statistical value, and Phase-18 already answered the edge question more completely than any single live trade could. **TF the strategy has no demonstrated edge over 27 months and is retired as a money-maker.** TF the *system* — the live reconciling execution layer (which performed flawlessly under a genuine partial-fill edge case this session), the honest anti-overfit harness, the multi-market data pipeline, the discipline — is the real, reusable asset. Built a good boat; the fish weren't in that pond. Per §0.5.226: next time, the honest test comes first.

**Enforcement rules for next session:**
1. Do not re-open the strategy search expecting a different answer. Phase 15→18 is exhaustive and honest; the verdict is NONE. If a new idea comes, it goes through the Phase-16 bar FIRST, before any infrastructure.
2. Do not clear the halt or restart trading until the §13 partial-fill fix is merged, deployed into the flat book, and smoke-tested.
3. For any P&L or position claim, hit broker truth. The DB lied this session.

---

## 11. Logging verbosity — what to demand from any new code

- Every upsert logs `[COMPONENT] symbol: action — reason`.
- Every state transition logs old → new at INFO (ENTERING→ACTIVE→EXITING→CLOSED).
- Every swallowed exception logs the specific error + context.
- Retry/poll loops log attempt number and reason.
- Async code logs entry AND exit — **and for the partial-fill fix specifically, log each execution received, the cumulative qty, and whether the handler is acting or skipping (full-fill gate decision).** The race this session was only diagnosable because fills were timestamped to the millisecond; keep that.
- Any dedup / select-one-of-many / arm-once must log which path won and why.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill for the full template (patch constraints, code quality, test-safety guardrails, known gotchas, "what I got wrong" post-PR section). The §13 brief below is built from it.

---

## 13. Current PR brief in flight — hand this to Claude Code as-is (AUDIT)

~~~
# PR #150 BRIEF — Partial-fill full-fill gate on the entry arm path (AUDIT)

## Autonomy Level: AUDIT
Order-execution change (broker-state-altering). Build to green, **paused for operator
approval before merge.** Do NOT self-merge.

## Objective (one sentence)
Make the entry-fill handler single-shot/idempotent across partial fills by gating
`_handle_parent_fill` on FULL fill (`remaining==0` / `orderStatus.status=='Filled'`),
so a multi-lot MKT order that fills as multiple partial executions arms exactly once,
with the correct aggregate qty/price, one DB write, one stop, one ratchet id.

## Root cause (verified this session — see HANDOFF_v27 §5)
A 2-lot MKT filled as two 1-lot partial executions ~50ms apart. `_on_fill →
_handle_parent_fill` (`src/execution/router.py` ~503-518) fires ONCE PER EXECUTION
with no full-fill gate; the line-510 cache-refresh loses the ~50ms async race. Both
callbacks ran a full arm + ENTERING→ACTIVE transition + DB PATCH concurrently. The
1st wrote `entry_qty=1`, the 2nd wrote `entry_qty=2`, PATCHes are unordered, the stale
qty=1 write landed last → DB permanently `entry_qty=1`. STABILIZE-4 then read
`intended_net=1` vs `broker=2`, called a real contract "foreign", and flattened it.
3rd touch of this family (#141 → PR-3 settle-grace → this). NOT #144 (worked), NOT #141
(`_extract_fill` summed correctly within one callback).

## The fix (Option A — gate on full fill)
In `_on_fill` / `_handle_parent_fill`: only run the arm + state-transition + DB PATCH
when the parent entry order is FULLY filled (`remaining==0`). Ignore intermediate
partial executions (log them, do not act). On the terminal full-fill callback, compute
the aggregate qty and vwap from the complete fill set (reuse `_extract_fill`) and arm
once.

## MANDATORY verification inside this PR (do not skip)
Confirm the reconciler **force-fill backstop still arms a protective stop for a
stuck/never-completing partial** (the rare case where an entry fills 1-of-2 and then
hangs). Gating on `remaining==0` must NOT leave a partially-filled-then-stalled
position permanently unarmed. Read `src/execution/reconciler.py` force-fill path,
confirm it still triggers, and add/extend a test for it. If the backstop does NOT
cover this case, STOP and report before implementing — do not trade one edge case for
another.

## Files allowed (EXACTLY these)
- `src/execution/router.py` — the `_on_fill`/`_handle_parent_fill` gate
- `tests/test_router_partial_fill.py` (new) — the partial-fill race test
- (only if the backstop needs a touch) `src/execution/reconciler.py` — minimal, flagged

## Files forbidden (verify empty diff)
- `src/strategy/*` — strategy is retired/untouched; this is execution only
- `config/*`, any secrets, `tools/eval/*`
- Any deploy/compose file

## Test requirements (test-safety guardrails — HANDOFF_v27 §8)
- Simulate TWO partial executions with a realistic async gap (not one full fill — a
  single-fill test passes while prod still races).
- Mock at the wrapper (`_on_fill`/`_handle_parent_fill`), NOT the raw `ib_async` exec
  callback.
- Assert: exactly ONE arm, ONE DB PATCH, `entry_qty` == aggregate (2), stop qty == 2,
  ratchet bound to the live stop id, NO foreign-position trigger.
- Add the stuck-partial backstop test (1-of-2 fills then stalls → reconciler force-fill
  arms a stop).
- Known pre-existing red tests: carry forward the usual list from the prior brief; do
  NOT "fix" them.

## Pre-Push Checklist
- [ ] `ruff` + `black --check` clean on changed files
- [ ] `pytest tests/test_router_partial_fill.py -q` green
- [ ] full suite green except the known pre-existing reds
- [ ] diff touches ONLY the allowed files
- [ ] backstop (stuck-partial) verified covered + tested
- [ ] logging: each execution received, cumulative qty, act/skip decision (§11)

## What I got wrong (fill in after implementation)
[Required. Document any dead end, wrong assumption about the callback path, or
backstop surprise found while implementing.]

## Ship sequence
Build to green → PAUSE for operator diff review → on approval, merge → deploy into the
FLAT book (rebuild + force-recreate tradeflow-app; ALSO re-add the GIT_COMMIT build arg
so TRADEFLOW_COMMIT stops showing "unknown") → run the §post-merge smoke runbook →
report. Operator decides separately whether to clear the halt.
~~~

---

## 14. Canonical references (in order of authority)

1. **Broker truth** — `ib_async` against the gateway (`127.0.0.1:4002`→4004, read-only dedicated clientId) — truth for position, fills, capital. **Overrides everything.**
2. **Production DB** — Supabase `lifecycles`/`lifecycle_events` via service role — truth for state, EXCEPT multi-lot `pnl_net`/`entry_qty` which are known-wrong this session (§4).
3. **Source code on `main`** at f998abe — what actually runs (prod = #144 c6bd0a2 deployed).
4. **`CLAUDE.md`** at repo root — auto-loaded system rules for VPS CC.
5. **`tools/eval/phase18/RESULTS.txt`** — authoritative for the foundational-edge verdict.
6. **This handoff (v27)** — session context, NOT long-term authority.
7. **v26 and earlier** — historical; ignore any claim contradicting 1–5.

---

## 15. First 15 minutes of the next session

1. Read §0.5, §1, §2, §5, §10 of this handoff. **§10 (the edge verdict + lane discipline) and §5 (the partial-fill root cause) are the two to internalize.**
2. SSH in. Run the §6 verification block V0→V4. **Confirm: FLAT, 0 orders, HALT still raised, containers healthy, deployed=#144.**
3. If V0 shows anything other than flat+halted: STOP, re-ground from broker truth, do not proceed to code.
4. Hand the §13 brief to VPS CC in tmux `tf1` as an AUDIT mission (implement → green → pause for diff review).
5. While CC builds: nothing to parallelize on strategy (campaign closed). If the operator wants, this is the window to have the "what is TradeFlow now" strategic conversation (§7) — building vs returns — but do not push it.
6. On CC's pause: review the diff (especially the backstop path), approve, merge, deploy into the flat book, run the post-merge smoke runbook (below), report. Halt-clear is a separate operator decision.

---

## 16. How to publish this handoff

**Path A — VPS Claude Code brief:**

```
You are VPS Claude Code on the TradeFlow VPS. Save the following content verbatim
to /home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v27.md, then:

  git -C /home/tradeflow/tradeflow add docs/handoffs/HANDOFF_v27.md
  git -C /home/tradeflow/tradeflow commit -m "docs: add v27 handoff (campaign closed; founding edge retired; FLAT+HALTED; partial-fill fix queued)"
  (publish via the branch+PR flow — direct push to main is blocked by branch protection)

Confirm the file exists, git log shows the commit, and git status is clean.

<paste handoff content>
```

**Path B — Manual fallback (operator drives own shell, heredocs OK) — see the PUBLISH BLOCK delivered with this handoff.** Direct push to main is rejected by branch protection; the publish block branches off origin/main, commits, pushes, opens a PR, POLLS `gh pr checks` until the required "Lint, type-check, and test" check completes green, then squash-merges with `--admin --delete-branch` and resyncs main.

The handoff exists only if saved to disk and committed. Until then, treat the chat output as draft.

---

*End of handoff v27. Target lifespan: until the §13 partial-fill fix is merged + deployed + smoke-tested and the operator has decided TradeFlow's direction (building vs returns). Then rely on CLAUDE.md + whatever v28 captures. The discovery campaign is closed; do not reopen it expecting a different answer.*
