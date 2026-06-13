# TradeFlow — Handoff v28 (Phase-21 gauntlet S01–S05 all NONE; S05 re-landed on main; bot flat & un-halted on paper)

*Handoff from end of 2026-06-13. The bot is RUNNING, FLAT, healthy, and un-halted on the IBKR **paper** account (DUQ331660, ~$1M paper NAV) at deployed commit `e3c800d` (PR #158). `origin/main` is at `0032147` — every commit since `e3c800d` is research-only (no `src/` change), so the running bot code equals main's `src/`. All Phase-21 work this session is offline research; no real capital is at risk. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read sections 1–6 first — that's the state of the system as of handoff. Sections 7–13 are reference material; §13 is the next PR brief ready to paste into Claude Code, §13b is its post-merge runbook. Section 14 ranks authority when this doc disagrees with a live observation.

**Do not trust this doc alone.** Run the §6 verification block before doing anything. **Critical first check:** the bot is a live PAPER test bed left un-halted on purpose — confirm it is still FLAT and that `tradeflow-app` is healthy before any action.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block. Self-contained; expected output described immediately below; decision tree if more than one branch matters. No "you might want to…" — give the command or don't mention it.

**Learning-delivery discipline.** Every new fact (bug pattern, corrected assumption, environmental fact, diagnostic finding) is surfaced immediately as a paste-ready markdown snippet for the running handoff queue — not saved for end-of-session.

**Read before diagnosing.** For complex state bugs, read the full startup log and 3–5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency language, hit the live broker/DB/raw log, not aggregated metrics.

**Always draft a VPS smoke-test runbook after PR merge** unless told otherwise. The owner does not run smoke tests by hand.

### TradeFlow project standing rules (carried forward verbatim; never shrink)
- **§0.5.97** — Probe external specs (broker contracts, exchange fees, schema, library APIs, data formats) against the source before baking into briefs. Do not re-derive from memory.
- **§0.5.98** — Broker/exchange state is ground truth, not internal DB tables.
- **§0.5.205** — No OCA-grouping on ratchet trailing stops (root cause of IBKR Error 10326).
- **§0.5.207** — NEVER-ORPHAN: never leave a position without its protective stop; never leave a resting stop without its position.
- **§0.5.209** — `force-recreate`/`start` un-halts a flat bot. The soft halt is in-memory only; there is no external raise lever. Only an operator Telegram `/halt` or the kill-switch can raise a halt.
- **§0.5.215** — A risk gate has three states — enabled, armed, blocking. Verify all three from the live container, not from config alone.
- **§0.5.221** — All long-running CC jobs run inside `tmux` with output to `/tmp`.
- **§0.5.223** — A risk control firing ≠ effective. Measure coverage against real data.
- **§0.5.224** — Concurrency has DB-layer preconditions independent of app config.
- **§0.5.225** — Read live config from the env-derived RISK singleton, never bare `RiskParams()` defaults.
- **§0.5.227** — Partial-fill full-fill gate: an entry arms exactly once, on full fill; a partial fill does not arm. (PR #151.)
- **§0.5.T5** — No naked legs.
- **§0.5.228 (NEW this session)** — Every `gh pr create` passes EXPLICIT `--base main`. The repo's local `refs/remotes/origin/HEAD` symbolic ref is stale (points at `claude/phase-0-repo-bootstrap-ZZGJX`) even though the GitHub default branch is `main`; without an explicit base, a PR can default to the wrong branch. This is the confirmed root cause of PR #164 landing off-main.
- **§0.5.229 (NEW this session)** — Binance COIN-M delivery klines: exit at the last FULL bar BEFORE the settlement day. Two export artifacts otherwise corrupt the series: (1) expired symbols emit frozen zero-volume rows at the settlement price for months past expiry; (2) the settlement-day daily bar is a partial bar (COIN-M settles ~08:00 UTC while the SPOT daily bar closes 24:00 UTC — a ~16h gap that fakes ±8% "basis" on the final bar). With expiry-eve exit, 46/46 expired contracts converge to <0.6%.
- **§0.5.230 (NEW this session)** — The `n ≥ 200` gate is the WRONG test for a mechanical-convergence arb (e.g. S05 cash-and-carry): convergence is contractual, not statistical. For such strategies, certify by the mechanism (monotonicity `rho`, zero-loser rate, convergence integrity), not by a 200-trade backtest. Report the harness verdict (NONE) but flag the structural mismatch; never change the champion to rescue a verdict.

### CC operating rules (carried forward)
- **CC Bash discipline (absolute):** no `cd X &&`, no `;`, no `$(...)`, no `${VAR}`, no heredocs in CC Bash invocations. Use `git -C <path>`, the Write tool for file content, Python helpers in `/tmp/` for interpolation, `--body-file` for PR bodies, a Python polling loop for waits >10s. Never `git add -A`. (Heredocs ARE fine when the OWNER drives his own Mac→VPS shell — see §16.)
- **Autonomy contract:** AUTO (docs/config/tests-only) = zero operator action, CC auto-merges on CI green. REPORT (bug fixes ≤5 files, strong tests) = one-word approval. AUDIT (order execution / strategy / kill-switch / secrets / multi-file >50 LOC) = operator scans the diff 2–5 min, then approves.
- **Operator style:** hands-off PM, single-word approvals, end-to-end CC missions (implement→test→ship→self-merge REPORT/AUTO→deploy→verify→report once at end). No back-and-forth check-ins. Operator has opted OUT of `--dangerously-skip-permissions` — do not suggest it.
- **VPS CC pre-flight (mandatory first action every session):** `git -C ~/tradeflow fetch origin` then `git -C ~/tradeflow pull --ff-only origin main`, then `ls -t docs/handoffs/ | head -3` and read the latest `HANDOFF_v*.md`.

### MNQ contract spec (§0.5.97-verified — do not re-derive)
TICK_SIZE=0.25 index points; MULTIPLIER=$2/point; COMMISSION_RT=$0.62; MARGIN_REQ=$2,000 day-trade; CME maintenance ~$3,636. Quarterly cycle Mar/Jun/Sep/Dec, expiry 3rd Friday, roll ~8 days before.

---

## 1. Where we are (as of handoff, 2026-06-13 ~21:30 UTC)

### Live production state (PAPER)
- Containers: `tradeflow-app` Up (healthy), `tradeflow-ib-gateway` Up (healthy), `tradeflow-telegram-listener` Up. Deployed commit `TRADEFLOW_COMMIT=e3c800d…` (PR #158).
- Broker (IBKR paper DUQ331660): position FLAT (0.0), 0 open orders as of last reconciler scan. N=2 concurrency configured.
- Kill-switch: poll loop running, `enabled=True warn_consec=6 halt_consec=10 max_drawdown=33% allocation=UNSET`. The 10-consecutive-loss halt is the active hard brake; the 33% drawdown brake is INERT (`ALLOCATION_USD` unset) — acceptable on paper.
- Operational override: bot left UN-HALTED and running on purpose, as a live paper test bed (awaiting the next trade or a bug to study). No restart-policy changes.
- Postmortem artifacts: none new this session.

### What just shipped (all merged this session)
- **PR #158** (`e3c800d`, AUDIT) — force-fill sizes from per-lifecycle fill, not account net. New `_force_fill_qty` + `_filled_qty_for_order` in `src/execution/reconciler.py`. **Deployed** (rebuilt with GIT_COMMIT stamp, container recreated, verified flat + fix-present). The only prod-surface change this session.
- **PR #159** (Phase-21 Stage-0, research) — data-feasibility gate for all 18 hypotheses; created `tools/eval/phase21_gauntlet/MANIFEST.md`; 8 closed UNTESTABLE-HERE with STOP.md files.
- **PR #160** — S01 pre-FOMC drift → **NONE** (faded).
- **PR #161** — S02 OPEX calendar → **NONE** (beta; placebo beat it).
- **PR #162** — S03 VIX short-vol roll-yield → **NONE (structural)** — strongest raw result of Phase 15–21.
- **PR #163** — S04 Treasury auction concession → **NONE** (OOS sign flip).
- **PR #164** — S05 crypto cash-and-carry → **NONE (structural)** — **merged to the WRONG branch** (`claude/phase-0-repo-bootstrap-ZZGJX`), see §5.
- **PR #165** (`0032147`) — S05 re-land on `main`. File-snapshot overlay of the 6 S05 paths from `e0cadd6`; CI lint fix on inherited ruff nits; squash-merged. Manifest now `CUMULATIVE_TRIALS: 96`, `13/18 DONE, next up S06`.

### What we discovered this session (not yet in code, or new facts)
- Phase-21 verdicts so far: 5 testable evals run (S01–S05), **all NONE**; 8 UNTESTABLE-HERE; 5 still pending (S06, S09, S10, S11, S12).
- **S03 short-vol** is real and robust: contango variant train PF 2.895 / holdout 2.937, DSR 0.98, all 5 kill tests pass (the term-structure filter exited ahead of both Volmageddon +$63k and COVID −$8k; no cost cliff at 8×). It fails ONLY the n-gate (58/34) and each-year (4 neg years). Executable via SVXY on IBKR today.
- **S05 cash-and-carry** is a real mechanical arb: champion roll60 train PF 163 / HO 34; threshold variants inf-PF (0 losers in 6y, 100% win); monotonicity `rho=1.0`; survived post-ETF compression (+6.7% median annualized net carry in holdout). Fails ONLY n (14–26) and DSR 0.0. Executable on the owner's separate Binance-connected VPS.
- Across the whole diligent search there is **no certified, tradeable self-found edge yet**; two (S03, S05) are real-but-uncertifiable, both now known to be executable (execution is not the blocker — see §10).
- New environmental/data facts → §4.

---

## 2. The session's work thread

1. Delivered the Phase-21 "Gauntlet" standing work order (18 strategies, multi-session, manifest-driven resume).
2. PR #158 (force-fill per-lifecycle sizing) merged `e3c800d` and deployed via Task F; verified flat, commit-stamped, fix present, un-halted. The bot took live paper entries earlier and the #151 partial-fill gate fired correctly — concurrency was REAL, not a bug.
3. Ran the gauntlet on Claude Fable 5: Stage-0 (#159) → S01 (#160) → S02 (#161) → S03 (#162) → S04 (#163) → S05 data + eval. All NONE. Fable threw a mid-run "model may not exist / no access" error after ~71 min; resumed on Opus 4.8.
4. **Wrong turn (data):** S05's convergence integrity gate failed at 52% → first read was "the delivery-futures data is wrong." Probing raw bars disproved it (see §5). Root cause was a methodology bug (wrong exit bar), fixed by exiting expiry-eve.
5. **Process error:** S05 (#164) was merged with `--base claude/phase-0-repo-bootstrap-ZZGJX` instead of `main`. `main` was stuck at S04 (12/18, trials 92); the resume protocol would have re-run S05 and diverged the trial counter.
6. **Orchestrator (chat-side) wrong call:** I scored S03 and S05 as "not executable on the stack" and downgraded them. Wrong — SVXY trades on IBKR, the owner runs a separate Binance VPS, VIX-futures history is a buyable API. Corrected by rebuilding the strategy ratings matrix with execution removed as a filter (see §10).
7. Re-landed S05 on `main` via PR #165 (file-snapshot overlay; CI lint fix on inherited nits; squash-merged `0032147`). Verified `main` now 13/18, trials 96, S05 present, verdict reproduces NONE deterministically.

Closed rabbit holes for the next session: (a) S05 data is GOOD — don't re-investigate "bad data"; (b) execution is NOT a blocker for S03/S05; (c) S05 is properly on main now — don't re-run it.

---

## 3. What the system is actually made of

**Single source of truth:** none as a dedicated system-map file. For Phase-21 state, `tools/eval/phase21_gauntlet/MANIFEST.md` on `main` at `0032147` is authoritative. For system context, this handoff + v27.

Highlights:
- Production-live code paths (the running bot): `src/orchestrator` (entry/no-chase/own-entry path), `src/execution/reconciler.py` (force-fill + heal-missing-legs), `src/execution/kill_switch.py`, IBKR via `ib_async` → IB Gateway (Docker).
- Research surfaces (offline, never touch the bot): honest harness `tools/eval/phase16/{costs,metrics,gates,dataset}.py`; phase17–20 + phase21 eval dirs; data in `research/data/phase21/`; raw bulk caches in `research/cache/` (NEVER committed).
- The Phase-21 manifest tracks per-strategy status + `CUMULATIVE_TRIALS` (the deflation chain). Resume protocol = paste the resume line; the manifest tells a fresh session exactly where to pick up.
- Automation gotcha: the bot does NOT auto-restart from `stop`; `docker compose up -d`/`start` un-halts a flat bot (§0.5.209).

---

## 4. Verified facts (2026-06-13)

**DO NOT challenge these unless the schema/source migrates.**

Carried forward (still load-bearing): MNQ spec (§0.5 above); `openTrades()` is single-client — use `reqAllOpenOrders()` (all-clients sweep) for the true open-order book; broker is ground truth over the DB (§0.5.98); the honest harness is a conservative NO-detector (PF≥1.30, exp/ct≥$5, n≥200, each-year-positive, DSR≥0.95 with `--prior-trials`).

**New load-bearing facts (this session, with evidence):**
- **Deflation chain is at 96.** `MANIFEST.md` on `0032147`: `CUMULATIVE_TRIALS: 96` (started 78; S01+3→81, S02+3→84, S03+4→88, S04+4→92, S05+4→96). Every new eval passes the current value as `--prior-trials`, then writes back the incremented value.
- **Binance COIN-M delivery export artifacts** (§0.5.229). Evidence: `BTCUSD_210625` showed ±0.3% basis on every full-volume day to expiry-eve (2021-06-24 +0.05%) but +8.55% on the 622k-volume (vs ~5M avg) settlement-day partial bar; expired symbols emit frozen `10687.6 @ vol 0` rows for months. Exit expiry-eve → 46/46 converge <0.6%.
- **Treasury auctions:** use `api.fiscaldata.treasury.gov/.../auctions_query` (full history 1979→, 11,006 records). TreasuryDirect `TA_WS` **ignores year filters** — do not use it for historical pulls.
- **Yahoo purges expired futures contract months** (`CLZ20.NYM` → "No data found"). This kills all free commodity-curve work (S07/S08) — curve history is a paid unlock.
- **EDGAR is reachable** with a real `User-Agent`: `full-index/.../form.idx` (126,273 Form-4 in 2024Q1 sample) and `company_tickers.json` both 200. `data.binance.vision/.../futures/um/daily/metrics/` OI zips reachable.
- **Deployed bot commit `e3c800d` < main `0032147`, but the delta is research-only** — zero `src/` changes since #158, so the running bot code equals main's `src/`. A redeploy is only needed when a `src/`-touching PR merges (e.g. §13).
- **`refs/remotes/origin/HEAD` is stale** (points at the bootstrap branch); GitHub default is `main`. This is why #164 mis-defaulted (§0.5.228).

---

## 5. Wrong diagnoses (this session) — READ BEFORE YOU DEBUG

**Wrong diagnosis #1 — "S05 delivery-futures data is wrong."**
- Diagnosis: the convergence integrity gate failed (24–26/46 contracts within 1.5%), so the data must be bad.
- Evidence that misled: contracts showed prices running months past their expiry date; expiry-day close was >1.5% (up to 8.55%) off spot.
- Why wrong: those were two benign Binance export artifacts, not bad data — frozen post-expiry zero-volume rows, and a settlement-day partial bar time-misaligned with spot (~08:00 UTC settle vs 24:00 UTC spot close).
- Correct diagnosis: bar-by-bar probe showed basis was ±0.3% on every full-volume day to expiry-eve. Fix: exit the last FULL bar before settlement → 46/46 converge <0.6%. Caught at the integrity gate before any PnL existed; made the result MORE conservative.

**Wrong diagnosis #2 (orchestrator / chat-side) — "S03 and S05 aren't executable on this stack."**
- Diagnosis: filed both as real-but-unexecutable and downgraded them.
- Evidence that misled: assumed an MNQ/IBKR-only world and that the VPS's Binance geo-block (451) meant no crypto execution.
- Why wrong: SVXY trades on IBKR directly; the owner runs a SEPARATE Binance-connected VPS; VIX-futures history is a buyable API. Conflated "can't certify with a backtest" with "can't trade."
- Correction: rebuilt the ratings matrix with execution removed as a filter (S05 and S03 → Tier 1).

**Process error #3 — PR #164 merged off-main.** `--base claude/phase-0-repo-bootstrap-ZZGJX` instead of `main`; evidence: S05 absent from `origin/main`, `e0cadd6` reachable only on the bootstrap branch. Corrected via PR #165 (§0.5.228 added).

**Lesson for next session:** when data "looks wrong," probe the raw bars before declaring bad data — a methodology bug (wrong exit bar) mimics a data fault. And never assume an infrastructure constraint to rule a strategy out; the owner's stack is broader than the bot's instrument. Always pass explicit `--base main`.

---

## 6. Verification block — run this before doing anything

Run each as a separate CC Bash call (CC discipline: no `&&`/`;` chaining).

**V0 — main / gauntlet state**
```bash
git -C ~/tradeflow fetch origin
```
```bash
git -C ~/tradeflow log --oneline -1 origin/main
# Expect: 0032147 ... Phase-21.S05 re-land ... (#165)  — or LATER
```
```bash
git -C ~/tradeflow show origin/main:tools/eval/phase21_gauntlet/MANIFEST.md | grep -iE "cumulative_trials|gauntlet status"
# Expect: CUMULATIVE_TRIALS: 96   and   IN PROGRESS — 13/18 DONE, next up S06
# If trials != 96 or count != 13/18: a PR landed/regressed since handoff — reconcile before resuming.
```

**V1 — bot containers + flatness**
```bash
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}"
# Expect: tradeflow-app Up (healthy); tradeflow-ib-gateway Up (healthy); tradeflow-telegram-listener Up
# If tradeflow-app Exited/Restarting: STOP, tail logs, report.
```
```bash
docker logs tradeflow-app --since 5m 2>&1 | grep -iE "position|halt|flat|kill|error|traceback" | tail -25
# Expect: position 0.0 (flat); kill-switch poll loop running; no active halt; no tracebacks.
# If a non-zero position or a naked leg appears: STOP — this is the §0.5.207/T5 family, treat as AUDIT.
```

**V2 — deployed code is the #158 image (and equals main src)**
```bash
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' tradeflow-app | grep -i commit
# Expect: TRADEFLOW_COMMIT=e3c800d…  (main has moved to 0032147 but all commits since are research-only — bot src == main src)
```
```bash
docker exec tradeflow-app grep -n "_force_fill_qty\|_filled_qty_for_order" /app/src/execution/reconciler.py
# Expect: both present (PR #158 live). If absent: container is on a stale image — rebuild before trusting behavior.
```

**V3 — broker truth (read-only)**
```bash
docker logs tradeflow-app --since 15m 2>&1 | grep -iE "reqAllOpenOrders|open order|orphan|naked|RECON|reconcile" | tail -25
# Expect: 0 orphan/naked legs; reconciler scans clean; book matches DB.
# For a LIVE all-clients IBKR sweep (clientId 98, reqAllOpenOrders + reqPositions), use §13b runbook §4.
```

---

## 7. Pending work queue

Priority depends on §6 state, not on this ordering.

### Priority 1 — Phase-21 gauntlet, EDGAR equity block (operator decision: jump ahead of S06)
Per the strategy ratings done this session, **run S09 (SEC Form 4 insider cluster-buy) then S10 (micro-cap PEAD) BEFORE S06.** They are the only untested candidates that clear moat + n≥200 + execution (long-only US equities on IBKR) before returns are even seen. Resume line: *"Continue the Phase-21 gauntlet per tools/eval/phase21_gauntlet/MANIFEST.md — run S09 and S10 next, ahead of S06."* S09 likely dies (if at all) on excess-vs-SPX (bull beta); S10 on the 75bp/side micro-cap cost-cliff.

### Priority 2 — §13 PR: `_heal_missing_legs` net-vs-per-lifecycle sizing fix (AUDIT)
Sibling of #158, flagged by CC during #158. The heal path still sizes a re-placed protective leg from the account NET, not the lifecycle's own fill — same oversell family, lower frequency. Brief in §13; post-merge runbook in §13b. AUDIT (touches `src/execution/reconciler.py`).

### Priority 3 — remainder of gauntlet
S06 (crypto OI snapback), S11/S12 (EDGAR shorts — CONDITIONAL on borrow), then the Final Synthesis PR (`RATINGS.md` + `ratings.json`). Re-examine the data-buy candidates (S07 commodity carry, S16 VIX-ETP intraday) only after the EDGAR block resolves.

### Operational debt
- **Stale `origin/HEAD`** — operator action: `git remote set-head origin main` locally, and confirm the GitHub default is `main` (it reads `main` via `gh repo view`). Not blocking now that §0.5.228 forces explicit `--base main`.
- **Stray `e0cadd6`** on the bootstrap branch — harmless, leave it.
- **Untracked junk in the repo** — `docs/tf_research.zip`, `docs/tf_research_v2.zip`, stray `__pycache__` seen in `git status`. Never committed; clean up eventually (never `git add -A`).

### Bugs carried forward by ID
- The `_heal_missing_legs` net-sizing bug → §13 PR (this is the open one).
- Sibling smell at `reconciler.py` `qty_matches` (net-vs-single, audit-only/cosmetic) — documented, low priority.

---

## 8. Test safety — why we belabor this

Carried forward (cumulative — do not ship tests that skip these guardrails):
1. Tests passing against a fictional schema because they mocked column names.
2. `side_effect` list with the wrong count → silent `StopIteration` → wrong assertions.
3. Mocking the raw library chain when the code uses a wrapper → tests green, prod broken.
4. Shared `MagicMock()` state leaking between tests.
5. Async decorator pattern assumed instead of verified against a neighbor.

PR #158 added 3 tests (concurrent-N2 regression, single-unchanged, partial-then-full) with full suite green (900). The §13 brief's Pre-Push Checklist enforces all five guardrails.

---

## 9. Pitfalls from prior sessions

- `openTrades()` is single-client — a "0 open orders" read can be a false negative; use `reqAllOpenOrders()` all-clients sweep.
- A soft halt is in-memory only; `force-recreate`/`start` un-halts a flat bot (§0.5.209). Don't assume "I halted it" survives a recreate.
- A risk control "firing" ≠ effective (§0.5.223); measure coverage.
- Documented anomalies keep dying in the harness — pre-FOMC faded, OPEX was beta, the auction edge inverted. Best-positioned ≠ validated.
- Data that "looks wrong" may be a methodology bug (S05 expiry-eve). Probe raw bars first.

**Next session rule: if a claim is quantitative, re-verify it.** Especially position/flatness, open-order counts, the trial counter, and the manifest's done-count.

---

## 10. Session discipline lesson (2026-06-13) — orchestrator log

**What I (chat-side orchestrator) got wrong.** I scored S03 and S05 as "not executable on this stack" and used that to downgrade two of the only real edges in the set. Both are wrong: SVXY trades on IBKR today; the owner runs a separate Binance-connected VPS; VIX-futures history is a buyable API. I conflated "can't certify with a backtest" (a sample-size problem) with "can't trade" (an execution problem). That is an assumption I should have surfaced and checked, not baked into a recommendation.

**What I corrected.** Rebuilt the strategy ratings with execution removed as a primary filter. Result: S05 → Tier 1 (deployable now on the Binance VPS; the NONE is a backtest-shape artifact per §0.5.230); S03 → Tier 1 (executable via SVXY today, certify the tails with VIX-futures history before sizing); S09/S10 (Form 4, PEAD) → the highest-EV untested set and the next priority. The only permanently dead one is S18 Russell (n≈40 forever).

**Lane discipline held.** Chat stayed in strategy / briefs / synthesis the entire session; CC executed all code, git, deploy, and verification; I never touched prod, never pushed code, never edited `src/`. The two-tier model held — the one slip was an analytical assumption leaking into a recommendation, caught and corrected the same session.

**Honest paper/edge note.** This is a PAPER account — no real capital is at risk, and the bot is intentionally left running flat as a test bed. Across the full diligent search (founding SMA-bounce strategy, funding carry, month-end ES/ZN, and S01–S05) **every self-found edge is NONE under the honest harness.** Two are real-but-uncertifiable (S03 short-vol; S05 cash-and-carry) — real mechanisms, blocked by sample size, now known to be executable. The EDGAR equity block (S09/S10) is the best untested bet but is unproven and the harness has been brutal. The honest stance: deploy no real capital on anything uncertified; S05 is the closest to deployable (small, sized for exchange/counterparty risk) and is carry, not alpha.

**Enforcement rules for next session:**
1. Never rule a strategy out on an assumed infrastructure constraint — ask or treat execution as solvable.
2. Pass explicit `--base main` on every PR (§0.5.228); verify the PR base before merge.
3. Keep the harness honest — report NONE on uncertifiable real edges; flag the structural mismatch (§0.5.230) rather than rescuing the verdict.

---

## 11. Logging verbosity — what to demand from any new code

Carried forward: every reconciler decision logs `[COMPONENT] symbol: action — reason`; every state transition logs old→new at INFO; every swallowed exception logs the specific error + context; any "select one of many" (dedup, heal, force-fill) must log which row/quantity won and why; async paths log entry AND exit. The §13 fix must log the per-lifecycle qty it derived and the source (`get_fills` cumQty vs fallback).

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill / the brief in §13. It enforces patch constraints, code quality, the five test-safety guardrails, carried-forward known gotchas, and the "What I got wrong" post-PR section.

---

## 13. Current PR brief in flight — hand to Claude Code as-is

~~~
# TradeFlow — Claude Code PR Prompt: PR <next#> — _heal_missing_legs sizes from per-lifecycle fill, not account net (AUDIT)

## Role
You are a senior Python developer working on TradeFlow, an autonomous MNQ futures bot trading a LIVE IBKR PAPER account (DUQ331660, ~$1M paper NAV) with N=2 concurrency. You write clean, tested, production-grade code. You never modify files you weren't asked to modify. You study existing patterns before writing. Bugs here can create naked legs / oversells, so this is AUDIT-level care.

You are verbose in logging. Format: `[COMPONENT] symbol: action — reason`.

You second-guess your own assumptions: state the expected existing pattern, then verify by reading the file. Do NOT trust prior claims about line numbers or behavior without a quick read first (§0.5.97). PR #158 just fixed the SAME bug family on the force-fill path — mirror it; do not diverge.

## Context
PR #158 (`e3c800d`) fixed `_force_fill_qty` so the force-fill path sizes a re-placed entry+stop from the lifecycle's OWN fills (summed `cumQty` from `get_fills()`), not the account NET position. During that PR, CC flagged a sibling: `_heal_missing_legs` (~`reconciler.py:411` pre-#158 — VERIFY current line) still sizes a re-placed protective leg from the account NET (`position.position`) on the ACTIVE-lifecycle heal path. Under an N=2 ACTIVE book, healing a missing leg for ONE lifecycle from the combined net oversizes the stop → oversell (same family as #141/#151/#158, lower frequency). This is the concrete sibling bug; fix it the same way #158 did.

## 🏗️ System Architecture & Recent Learnings
- Container: `tradeflow-app` (orchestrator + reconciler + kill-switch); `tradeflow-ib-gateway` (IB Gateway).
- Language: Python 3.x, async (`ib_async`).
- Broker: IBKR paper DUQ331660 via IB Gateway. Broker is ground truth (§0.5.98).
- Env: RISK singleton is env-derived (§0.5.225); read live config from it, not `RiskParams()`.
- Logging source: `docker logs tradeflow-app`; module LOGGER.

### Key Architecture Constraints
- Runtime: `ib_async` is async; `get_fills()` returns per-order fills — sum `cumQty` for the lifecycle's own filled quantity (mirror `_filled_qty_for_order` from #158).
- Shell/tests: pytest in the repo venv (`~/tradeflow/.venv/bin/python -m pytest`). Full suite was 900 green after #158.
- Source of truth: `reqAllOpenOrders()` is all-clients; `openTrades()` is single-client (do not size from a single-client view).
- Scope boundary: ONLY the heal path. Do NOT touch `_force_fill_qty` (already fixed), the entry path, the kill-switch, or strategy params.
- Design decision: source per-lifecycle qty from `get_fills()` summed `cumQty` (primary). Fallback ONLY if fills are unavailable: `min(|net|, intended)` — NEVER bare account net. Mirror #158's helper; reuse `_filled_qty_for_order` if it fits.

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints
Files you WILL modify (EXACTLY 2):
- `src/execution/reconciler.py`
- `tests/execution/test_reconciler.py` (or the existing reconciler test module — verify the path)

Files you MUST NOT modify:
- `src/orchestrator/**`, `src/execution/kill_switch.py`, `config/**`, any secrets, any `tools/eval/**`.

Verification gates (run before pushing):
- `git -C ~/tradeflow diff main -- src/execution/kill_switch.py` → MUST be empty
- `git -C ~/tradeflow diff main -- src/orchestrator` → MUST be empty
- `git -C ~/tradeflow diff main -- config` → MUST be empty
- `git -C ~/tradeflow diff main --stat` → EXACTLY 2 files

### 2. Code Quality
- `black --check` and `ruff check` pass on both files (CI gates on these — see §0.5; #165 was bounced once on ruff).
- No unused imports/vars; line length <100 where possible; type hints preserved; no public signature changes; one import per line.
- Verbose logging: `[RECON] <symbol>: heal_leg qty=<n> from=<fills|fallback> lifecycle=<id> — reason`.

### 3. Safety
- All pre-existing tests still pass. Known failing (do NOT fix): none as of the #158-era suite (900 green); if reds appear unrelated to your diff, report — don't fix.
- No unexpected DB writes; no unexpected IBKR order placement during tests (mock the broker).
- If you find an adjacent bug, DOCUMENT it in the PR description; do NOT fix it.

## 🧩 Current Mission: make `_heal_missing_legs` size the re-placed leg from the lifecycle's own fills, not the account net

### Objective
On the active-lifecycle heal path, the replaced protective leg's quantity must equal the lifecycle's own filled quantity (summed `cumQty`), not `position.position` (account net). Behavior for a single-lifecycle book is unchanged; the N=2 oversize is eliminated.

### Task A: Audit
Read `_heal_missing_legs` and its neighbors in `src/execution/reconciler.py` (the line moved after #158 — find it, don't trust ~411). Read `_force_fill_qty` and `_filled_qty_for_order` (added in #158) to mirror them. Answer in the PR description (3–5 lines): (1) exact current line of `_heal_missing_legs`; (2) where it reads `position.position`; (3) can it reuse `_filled_qty_for_order`, or does the heal path need its own per-lifecycle sum; (4) what the single-lifecycle behavior is before/after (must be identical).

### Task B: Implement
Replace the net-sourced sizing with a per-lifecycle fill sum (reuse `_filled_qty_for_order`/`get_fills` cumQty; fallback `min(|net|, intended)`). Keep account net for presence/direction only, never for quantity. Add the `[RECON]` log line above.

### Task C: Add tests
Add to the reconciler test module, mirroring #158's three tests:
- `test_heal_missing_legs_n2_sizes_from_own_fill` — two concurrent lifecycles, heal one, assert the replaced leg qty == that lifecycle's fill (NOT the net).
- `test_heal_missing_legs_single_unchanged` — single lifecycle, behavior identical to before.
- `test_heal_missing_legs_partial_fill` — partial then full, qty tracks cumQty.
Follow the Pre-Push test-safety guardrails. Fresh `MagicMock()` per test; mock at the wrapper level; verify the async decorator against a neighbor in the same file.

### Task D: Verify completeness
`grep -n "position.position\|broker_qty\|\.position\b" src/execution/reconciler.py` — classify every hit as presence/direction (OK) vs quantity-sizing (must be per-lifecycle). Confirm no other sizing site reads account net.

### Task E: Out-of-scope investigation
~10 min on the `qty_matches` net-vs-single smell (documented audit-only). Document; do NOT fix.

### Task F: Post-merge smoke test
This PR touches `src/` → the bot must be rebuilt + recreated and verified. Run the companion VPS smoke-test runbook (§13b of HANDOFF_v28) end-to-end after merge. STOP if the deployed-code check fails or any non-flat/naked-leg state appears.

## 📤 Expected Output
### Files modified (EXACTLY 2)
- `src/execution/reconciler.py`
- `tests/execution/test_reconciler.py`
### Git diff stat
~15–40 lines in reconciler.py; ~60–110 lines of tests.
### PR description must include
1. Summary — one sentence. 2. Task A audit (3–5 lines). 3. Task D grep output with classifications. 4. Task E finding. 5. Local test tail. 6. Full-suite result (only documented failures). 7. Protected-file diffs all empty. 8. Task F note (run §13b runbook). 9. "This PR does NOT touch the force-fill path, entry path, kill-switch, or strategy params." 10. "What I got wrong during this PR" — 1–3 lines, or "nothing".

## 🔍 Pre-Push Checklist
### Code Quality
- [ ] `black --check` passes  - [ ] `ruff check` passes  - [ ] no unused imports  - [ ] no multi-import lines  - [ ] no signature changes
### Tests — TEST SAFETY GUARDRAILS
- [ ] Fresh `MagicMock()` per test (never shared)
- [ ] Broker mock returns set explicitly (fills, positions)
- [ ] No `side_effect` list without an explicit count comment (off-by-one StopIteration is the #1 silent failure)
- [ ] No `patch()` on module-level factories; use injection / instance attrs
- [ ] Async decorator matches a neighbor in the same file (verify; don't assume)
- [ ] Assertions filter `call_args_list` by first positional arg, not call index
- [ ] Mock at the wrapper level, not the raw `ib_async` chain
### Production Safety
- [ ] Every verification gate shows empty diff  - [ ] Task D grep classifies all sites  - [ ] PR description includes the Task F smoke-test pointer  - [ ] adjacent bug noted (Task E)  - [ ] "What I got wrong" included

## ⚠️ Known Gotchas (carried forward — never shrink)
1. CC Bash discipline: no `cd X &&`, `;`, `$(...)`, `${VAR}`, heredocs; use `git -C`, Write tool, `--body-file`, Python in `/tmp`. Never `git add -A`.
2. `openTrades()` is single-client; use `reqAllOpenOrders()` for the true book.
3. Docker restart ≠ rebuild. After merge, owner rebuilds with the GIT_COMMIT stamp and force-recreates (§13b).
4. Soft halt is in-memory only; recreate/start un-halts a flat bot (§0.5.209).
5. CI gates on `ruff` + `black` + pytest; a docs/test-only change still runs the full check (it must COMPLETE before merge — the repo ruleset blocks merge on an in-progress check).
6. RISK singleton is env-derived (§0.5.225); never size from bare `RiskParams()`.
7. No OCA-grouping on ratchet stops (§0.5.205); NEVER-ORPHAN / no naked legs (§0.5.207/T5).
8. Pre-existing test failures: none known post-#158. Do not "fix" unrelated reds — report.
~~~

---

## 13b. Companion VPS smoke-test runbook — run AFTER the §13 PR merges + deploys

~~~
# VPS Smoke Test Runbook — PR <next#>: _heal_missing_legs per-lifecycle sizing fix

## Role
You are VPS Claude Code on the TradeFlow VPS (Hetzner, user `tradeflow`). Execute end-to-end WITHOUT modifying production code, secrets, or running `git push`. Read state, dump logs to `/tmp/`, produce a structured report. Stop and report at the first FAIL. Verification only. Never edit secrets; never `git push`.

## §1 — Pre-flight
```bash
git -C ~/tradeflow fetch origin
```
```bash
git -C ~/tradeflow log -1 --oneline origin/main
# Expect: HEAD == the merged PR's squash commit. If not: STOP, report.
```
```bash
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}"
# Expect: tradeflow-app Up (healthy). If Restarting/Exited: STOP, tail logs (§3), report.
```

## §2 — Deployed-code check (the fix must be in the running image, not just on main)
```bash
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' tradeflow-app | grep -i commit
# Expect: TRADEFLOW_COMMIT == the merged commit. If still e3c800d: image NOT rebuilt — owner must rebuild+recreate. STOP.
```
```bash
docker exec tradeflow-app grep -n "heal_leg\|_heal_missing_legs" /app/src/execution/reconciler.py | head
# Expect: the new per-lifecycle sizing + the [RECON] heal_leg log line present. If old net-sizing pattern: STOP, report image-not-rebuilt.
```

## §3 — State probes
```bash
docker logs tradeflow-app --since 10m > /tmp/tf_recent.log 2>&1
wc -l /tmp/tf_recent.log
# Expect: a normal volume of lines for 10m. If ~0: container stalled — STOP.
```
```bash
grep -ciE 'error|exception|traceback' /tmp/tf_recent.log
# Expect: 0. If >0: capture sample:
grep -iE 'error|exception|traceback' /tmp/tf_recent.log | head -20
```

## §4 — Source-of-truth check (live IBKR, read-only, all-clients)
Write a read-only probe to `/tmp` (CC discipline: no heredoc), then run it with the repo venv. It connects to IB Gateway with a DEDICATED clientId (98), does `reqAllOpenOrders()` + `reqPositions()`, prints, and disconnects. It places NO orders.
```bash
~/tradeflow/.venv/bin/python /tmp/ib_probe.py
# Expect: position MNQ == 0 (FLAT) OR a single coherent lifecycle's qty with its matching protective stop (no naked leg);
#         open orders: every resting order has a matching position leg (NEVER-ORPHAN, §0.5.207/T5).
# If a naked leg, an oversized stop (qty > the lifecycle's own fill), or a short on this long-only bot: STOP — capital-shape error, report immediately.
```
(Probe contents: connect host/port from the bot's env (`IB_GATEWAY_HOST`/`PORT`), `clientId=98`, `reqAllOpenOrders()`, `reqPositions()`, print account `DUQ331660` positions + all open orders with side/qty/price, then `disconnect()`. Read-only.)

## §5 — PR-specific behavior log tail
```bash
grep -c "heal_leg" /tmp/tf_recent.log
# Expect: 0 if no heal fired in-window (fine), or >0 with qty == the lifecycle's own fill (NOT account net). Inspect:
grep "heal_leg" /tmp/tf_recent.log | tail -10
```
```bash
grep -iE "oversold|naked|orphan|qty mismatch" /tmp/tf_recent.log | head
# Expect: empty. If matches: STOP — the bug recurred or a regression. Report.
```

## §6 — Verdict
- PASS — pre-flight clean, deployed code shows the new heal sizing, no errors, IBKR shows flat-or-coherent (no naked/oversized legs), behavior tail clean.
- FAIL — any naked/oversized leg, a short position, image not rebuilt, or heal sizing from net.
- INVESTIGATE — states outside baseline but not clearly broken; report raw output.

## §7 — Structured report
```markdown
# Smoke Test Report — PR <N>: _heal_missing_legs per-lifecycle sizing
**Verdict:** PASS / FAIL / INVESTIGATE
## §1 Pre-flight
- HEAD: <hash> (expected <merge hash>) — match/mismatch
- tradeflow-app: Up <dur> / Restarting / Exited
## §2 Deployed-code
- TRADEFLOW_COMMIT: <hash> (expected <merge hash>)
- heal sizing pattern: found / NOT FOUND
## §3 State
- recent log lines: <N>; error/exception count: <N> (sample if any)
## §4 Source-of-truth (IBKR, all-clients)
- MNQ position: <qty>; open orders: <side/qty/price list>; naked legs: <none/list>
## §5 Behavior tail
- heal_leg fires: <N> (qty source: fills/net); bug-pattern matches: <none/N>
## §6 Anomalies / next steps
<free text + recommendation: accept / rebuild / investigate / rollback>
```
*End of runbook. Owner reads §7 and decides. Do not edit prod state on the basis of this runbook.*
~~~

---

## 14. Canonical references (in order of authority)
1. `tools/eval/phase21_gauntlet/MANIFEST.md` on `main` at `0032147` — authoritative Phase-21 state.
2. Source code on `main` at `0032147` — what runs (bot src unchanged since `e3c800d`).
3. IBKR paper account DUQ331660 via `ib_async`/IB Gateway — truth for positions/orders (all-clients sweep).
4. Production logs `docker logs tradeflow-app` — behavior, not external truth.
5. This handoff (v28) — session context, NOT long-term authority.
6. v27 and earlier handoffs — historical; ignore any claim contradicting 1–4.

---

## 15. First 15 minutes of the next session
1. Read §0.5, §1, §4, §5, §10 of this handoff. §10 (the executability correction) is the most important to internalize.
2. SSH in. Run the §6 verification block (V0–V3). Confirm: main at `0032147`+, manifest 13/18 / trials 96, `tradeflow-app` healthy + FLAT, deployed `e3c800d`, #158 fix present, no naked legs.
3. Resume the gauntlet with the operator's priority: *"Continue the Phase-21 gauntlet per tools/eval/phase21_gauntlet/MANIFEST.md — run S09 and S10 next, ahead of S06."* (Pass the current `CUMULATIVE_TRIALS` as `--prior-trials`; explicit `--base main` on every PR — §0.5.228.)
4. In parallel or after, hand the §13 `_heal_missing_legs` AUDIT brief to Claude Code.
5. On §13 merge, run the §13b VPS smoke-test runbook (it's a `src/` change → rebuild + recreate + verify).
6. Optional cleanup: `git remote set-head origin main` to fix the stale `origin/HEAD`.

---

## 16. How to publish this handoff

**Path A — owner's one-shot Mac→VPS publish (primary, see the chat that produced this doc for the exact block):** `scp` the file up, then one `ssh tradeflow 'bash -s'` heredoc that branches off `origin/main`, commits `docs/handoffs/HANDOFF_v28.md`, pushes, opens a PR with explicit `--base main`, POLLS `gh pr checks` until the required check COMPLETES green (sleep-15 loop, ~10-min cap, fail-fast on fail/error — never `--watch`), then `gh pr merge --squash --admin --delete-branch`, resyncs `main`, and prints `DONE: v28 merged`.

**Path B — VPS Claude Code fallback:**
```
You are VPS Claude Code on the TradeFlow VPS. Save the provided content verbatim to
/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v28.md, then on a branch off origin/main:
  git -C ~/tradeflow checkout -b docs/handoff-v28 origin/main
  (Write the file)
  git -C ~/tradeflow add docs/handoffs/HANDOFF_v28.md
  git -C ~/tradeflow commit -F /tmp/msg.txt   # "docs: add v28 handoff (Phase-21 gauntlet S01-S05 + S05 re-land)"
  git -C ~/tradeflow push -u origin docs/handoff-v28
  gh pr create --repo ohad-oren111/tradeflow --base main --head docs/handoff-v28 --title "docs: add v28 handoff" --body-file /tmp/pr.md
Poll `gh pr checks docs/handoff-v28` in a Python sleep-15 loop until it finishes; squash-merge --admin --delete-branch on green; resync main; confirm git log.
```

The handoff exists only once saved to disk and committed to `main`. Until then, treat the chat output as draft.

---

*End of handoff v28. Target lifespan: until the EDGAR block (S09/S10) resolves and the §13 `_heal_missing_legs` fix is merged + smoke-tested. Then rely on the MANIFEST + whatever v29 captures.*
