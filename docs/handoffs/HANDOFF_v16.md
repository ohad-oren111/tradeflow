# TradeFlow — Handoff v16 (kill switch live; bot reactive-from-boot; FLAT, 1 loss from auto-halt)

*Handoff from end of 2026-06-01. The bot is RUNNING GREEN on `origin/main = 0ecc9c7`, FLAT, not halted. The kill switch is live and the bot is exactly **one losing close away from an automatic consecutive-loss halt + flatten** (5-loss streak — see §1). Warmup is enabled, so it trades from boot. This doc captures everything a new chat needs to pick up cleanly.*

> **NOTE ON VERSION:** I'm numbering this **v16** as the best reconstruction (this is Session 16). The publish step in §16 instructs VPS CC to confirm the actual latest handoff in `docs/handoffs/` and use `latest+1` if v16 is wrong — self-correcting, no operator action needed.

---

## 0. How to use this doc

Read sections 1–6 first — that's the state-of-the-system. Sections 7–13 are reference. **Section 14** is the authority order when this doc disagrees with a live observation. Canonical code truth: `main` at commit `0ecc9c7` or later.

**Do not trust this doc alone.** Run the §6 verification block before writing any code. **Critical first check: is the bot halted?** The 5-loss streak means it may have auto-halted between this handoff and the next session — confirm `halted` state and the open position before anything else.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block. Self-contained, env vars sourced in the same block, expected output described immediately below, decision tree if more than one branch matters. No "you might want to…" — give the command or don't mention it.

**Learning-delivery discipline.** Every new fact (bug pattern, corrected assumption, environmental fact, diagnostic finding) is surfaced immediately as a paste-ready markdown snippet for the running handoff queue. Not end-of-session.

**Read before diagnosing.** For complex state bugs, read the full startup log and 3–5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency ("capital at risk", "spiraling"), hit the live broker/DB, not aggregated metrics.

**Always draft a VPS smoke test runbook after PR merge** unless told otherwise. The owner does not run smoke tests by hand.

### TradeFlow project standing rules (carried forward — verbatim)

- **§0.5.97 — probe external specs before baking into briefs.** Broker contracts, exchange fees, schema, library API surfaces, *and a benchmark's own config* are verified against source before they go in a brief. (This session: SeanBot's `TRAIL_OFFSET=250` contradicted its own comment + validated finding of 150 — probing caught it.)
- **§0.5.98 — broker/DB state is ground truth, not internal assumptions.** Position, fills, P&L, loss counts source from the broker API / `lifecycles`, never an in-memory counter (resets on restart, and we restart a lot).
- **§0.5.151 — Docker healthy ≠ broker API healthy.** A green container can mask a dead IB Gateway session.
- **§0.5.184 — AUDIT gate.** Order-execution / strategy / kill-switch / secrets / broker-state changes PAUSE for the operator's single-word `merge` after the diff is posted. All other (REPORT/AUTO) PRs self-merge after CI green.
- **§0.5.186 / .187 — VPS CC command discipline.** No sub-agents/sweeps; no heredocs, `$(...)`, `${VAR}`, `;` separators, `cd X &&`. Python content via the `Write` tool to `/tmp/scriptN.py` then `python3 /tmp/scriptN.py`; commit `-F /tmp/commitmsg.txt`; PR body `--body-file /tmp/pr_body.md`. **Branch off `origin/main` FIRST, before any commit.** Permission prompts are command-style issues, not allowlist gaps.
- **§0.5.188 — never ship NEW trading-path bugs into unattended windows.** Weekend/overnight: surface findings as recommendations, don't auto-deploy risk.
- **Batch-autonomy default.** Batch follow-ups into ONE autonomous VPS CC run: self-merge all REPORT PRs sequentially without pausing, report ONCE at the end. Pause ONLY for AUDIT (order/strategy/kill-switch/secrets/broker-state), strategy-param calls, or external blockers.
- **`--dangerously-skip-permissions` — operator opted OUT. Never suggest it.**
- **Maximize autonomy / minimize operator UI.** Default to delegating. Prefer one ssh command over multiple operator actions. Handoff publish via VPS CC + `gh` CLI, no operator browser.

---

## 1. Where we are (as of handoff, 2026-06-01 ~21:15 UTC)

### Live production state
- `tradeflow-app` container: **running, healthy, RestartCount=0**, deployed commit `0ecc9c7`.
- Position: **FLAT** (no open lifecycle at handoff; re-verify in §6 — V3).
- **Halt state: NOT halted** at handoff — but the kill switch is **one loss from tripping** (see below). **Re-check first thing.**
- Realized P&L today = week = **−$1,873.44** (DB now broker-true after the §1 restatement). Equity base for DD = broker NetLiquidation ≈ **$1M** (paper).
- Dashboard live (all behind HTTP Basic auth): `/` (Live + recent-trades panel), `/trades`, `/pnl`, `/scoreboard`, `/divergence`.

### ⚠️ The thing to watch: 5-loss streak, 1 from auto-halt
Last 6 CLOSED lifecycles, newest→oldest `pnl_net`: **−19.48, −928.98, −924.98, −2.48, −307.48, +600.76**. That's a **5-loss streak**; only the 6th-back was a win. **The next losing close fires the 6-consecutive-loss kill switch → auto halt + flatten.** To resume after a trip: Telegram `/ack` **or** `touch /tmp/halt_clear` (reconciler clears the halt). This may also be the **first live exercise of the kill switch and the cancel-fix** at once.

### What just shipped this session (10 GitHub PRs + 1 restatement, #69 → #78)
- **#69 (AUDIT)** — protective stop on every entry path + `outsideRth=True` + per-contract reconciler entry. Fixed the weekend stop blow-through (stops were dormant on Globex overnight).
- **#70 (REPORT)** — dashboard `/trades` + `/pnl` read-only views.
- **#71 (AUDIT, cancel-fix)** `715776b` — `IBClient.cancel_order_by_id()` looks up the live Trade via `openTrades()` and cancels the real order; deleted the broken `_OrderIdRef` duck-type that threw `AttributeError` in `IB.cancelOrder`. Fixed 3 cancel sites incl. `cancel_all_for`.
- **#72 (REPORT, scoreboard)** `c6fb396` — `/scoreboard`: TF (`lifecycles.pnl_net`) vs SeanBot (`seanbot_signals` exit pnl) by UTC day, daily winner + cumulative.
- **`b39a4def` P&L restatement** — `scripts/restate_lifecycle_pnl.py`: corrupted notional 60890.12→30444.75, `pnl_net` −121206.96→−19.48. **Day TF P&L is now broker-true −$1,873.44.** (Do not re-challenge these restated values — §4.)
- **#73 (REPORT, PR C)** `c321818` — `decision_journal` flush fix: duplicate `(symbol, decision_ts)` in one batch → PG 21000 "ON CONFLICT cannot affect row twice" / HTTP 500 poisoned every flush since 04:41 UTC. `_dedupe_for_upsert` collapses to last-per-key + drops null-ts.
- **#74 + #75 (REPORT, one batch)** `62a25c7` — warmup SHADOW (`get_historical_bars` + `warmup_shadow.py`, instrumentation only) + `/divergence` dashboard (agree-enter / SeanBot-only / TF-only / agree-skip, TF skips by `failed_gate`).
- **#76 (REPORT)** `421bd0d` — Live page recent-trades panel + today's P&L. (The "no nav" screenshot was a **stale cached browser frame**, not a code bug — §5.)
- **#77 (AUDIT, warmup-enable)** `39f78f5` — strategy's real bar buffer is seeded from history at boot (`seed_bars()`), so SMA is warm immediately; **bot trades from the first live bar**, no ~99-min dead zone. Fail-safe: bad/short/absurd backfill → falls back to live warmup. WarmupShadow inverted to LIVE-ONLY (logs live-vs-backfill diff). Flag `WARMUP_BACKFILL_TRADE` (default on).
- **#78 (AUDIT, kill switch)** `0ecc9c7` **(= HEAD)** — halt-on-loss/drawdown circuit breaker; 30s poll from `lifecycles`; on trip → raise existing halt (blocks entries) → `flatten_all` (safe cancel+market-exit) → `[ALERT]` → stays halted until manual reset. Stop-only. Flag `kill_switch_enabled` (default on), `kill_switch_equity_base_usd` (default None → uses NetLiq).

### What I discovered this session (facts — see §4/§5 for detail)
- **TF's entry selection matches or beats SeanBot's** — TF is NOT slower/stricter on entries. The morning's missed entries were **warmup blindness** (now fixed by #77), not a strategy flaw. (Earlier "TF tests close, too strict" hypothesis was wrong — §5.)
- **SeanBot's validated V3 exit = fixed SL 75pt + trailing TP offset 150pt** (NOT the 250 in their stale config). WR 32.9%, PF 1.13, Max DD ~40% — a low-win-rate runner whose edge lives in big winners.
- **Kill-switch DD triggers are near-inert at the default NetLiq base** (~$80k/$150k on a $1M paper acct); the 6-consecutive-loss trigger is the real brake today.

### What the operator (Ohad) is doing
Hands-off PM / orchestrator. Single-word approvals (`merge`, `go`, `stop`). Does not write code, does not run smoke tests by hand, minimizes browser/UI steps. This session: approved warmup-enable up-front ("change the code, go"), reviewed and merged the kill switch AUDIT. **Next operator action:** publish this handoff via VPS CC (§16), paste the VPS CC output back into chat → then we close this session and open a fresh chat. The trailing-TP brief (§13) is staged and fires in the next session once dispatched.

---

## 2. The session's work thread

1. **Pre-weekend hardening sprint.** Shipped the weekend stop blow-through fix (#69: protective stop on every entry + `outsideRth`), then dashboard read views (#70 `/trades`+`/pnl`).
2. **Money-safety: cancel-fix (#71).** The sibling/orphan cancel was broken — `_OrderIdRef` (duck-typed, only `.orderId`) threw `AttributeError` in `IB.cancelOrder` (which needs `.clientId`). Replaced with `cancel_order_by_id()` resolving the live Trade. **Live proof still pending** the next real STOP/TARGET fill.
3. **Benchmark instrumentation.** Built `/scoreboard` (#72) and `/divergence` (#75) to measure TF vs SeanBot per day and classify each decision — the foundation for iterating TF to beat SeanBot rather than clone it.
4. **P&L restatement (`b39a4def`).** A corrupted notional row had blown `pnl_net` to −$121k; restated to broker-true −$19.48 for that row, day total −$1,873.44.
5. **decision_journal flush fix (#73).** Captured the real PostgREST error (PG 21000) — duplicate keys in one upsert batch poisoned every flush since 04:41 UTC. `_dedupe_for_upsert` fixed it.
6. **SeanBot code audit.** Read SeanBot's uploaded codebase (`/mnt/user-data/uploads/seanbot-share.zip`). Confirmed TF matches SeanBot on touch/band/MA-order/regime/cooldown/entry/1-position; **divergences**: TF's bullish gate is looser (`close ≥ open − 2.0` vs strict green — deliberate calibration), session-edge 5min vs 30min, **TF uses fixed TP 150 vs SeanBot's trailing TP**, and **the kill switch was absent** (thresholds were dead code).
7. **Warmup shadow → enable (#74 → #77).** Shipped the shadow first (de-risk), then — on operator's explicit call, because it's paper and the dead zone was actively costing entries — flipped to live. Bot now trades from boot. Convergence monitoring kept (see §4/§9).
8. **Kill switch (#78).** Wired the dead `risk_params` thresholds into a real stop-only breaker reusing the existing halt + `flatten_all` + Supabase `halt_acks` reset. Verified live (dry probe, zero side-effects). Surfaced the 5-loss streak.

Closed rabbit holes: the strategy is NOT the problem on entries (warmup was); the nav was NOT broken (cached frame); the broken cancel was a duck-type `.clientId` mismatch, not an allowlist/perm issue.

---

## 3. What the system is actually made of

**Single source of truth:** none — this handoff + the code on `main @ 0ecc9c7` are the best available system doc.

- **Production code paths:** `src/orchestrator.py` (boot, bar loop, halt, flatten, background tasks), `src/strategy.py` (`Sma100BounceStrategy` — entry gates + `seed_bars()`), `src/execution/kill_switch.py` (NEW — breaker), `src/execution/router.py` (`close_position` = safe cancel+market-exit), `src/clients/ib_client.py` (`cancel_order_by_id`, `get_historical_bars`, `get_account_summary`), `src/warmup_shadow.py` (live-only SMA diff + seed helpers), `src/comparison/` (decision_journal, seanbot_reconciler), `dashboard/` (5 views).
- **Halt mechanism (reuse, don't rebuild):** `Orchestrator.raise_halt(symbol)` / `clear_halt(reason)` / `is_halted()` → `self._halt_new_entries`; entry block at `_handle_trade_signal`; manual reset via reconciler `_poll_halt_ack` (Supabase `halt_acks` row from Telegram `/ack`, or `/tmp/halt_clear` file).
- **Dead/phantom surfaces:** `max_simultaneous_positions=5` is dead code (effective cap is 1). `risk_params` DD thresholds were dead until #78 wired them. SeanBot's `scripts/` phased/staircase exits are **unvalidated experiments even on Sean's side** — do not pull TF toward them.
- **Strategy params (TF, live):** MNQ, 2 contracts, long-only, SMA100/MA50 bounce, touch band `[ma100−15, ma100+5]` on bar LOW, bullish `close ≥ open − 2.0`, gap 0.5, regime 30m EMA200 (fail-opens — buffer can't form 202 30-min bars), cooldown 10 bars, session-edge 5 min. Exit: **fixed SL 75 + fixed TP 150** (trailing TP is the next AUDIT change, §13).

---

## 4. Verified facts (2026-06-01) — DO NOT challenge unless schema/code migrates

- **MNQ spec (§0.5.97-verified):** TICK 0.25, MULTIPLIER $2/pt, COMMISSION_RT $0.62, MARGIN $2000. Quarterly Mar/Jun/Sep/Dec, 3rd-Friday expiry, roll ~8d before. Live contract symbol seen this session: `MNQM6`.
- **Loss count + P&L source = `lifecycles`** (`state='CLOSED'`, `pnl_net`, `exit_filled_at`), never an in-memory counter (§0.5.98).
- **Restated P&L (`b39a4def`) is broker-true:** day TF = −$1,873.44; the previously corrupted row is `pnl_net=−19.48`. Do not re-derive from the old corrupted notional.
- **Warmup is ENABLED:** `WARMUP_BACKFILL_TRADE` default on. At boot, `[WARMUP-ENABLE] strategy buffer seeded … indicators_ready=True`; bot trades from bar 1. Fail-safe rejects <100 bars / NaN / >500pt-from-price → live-warmup fallback.
- **Kill switch is LIVE:** `[KILL] poll loop started — interval=30s enabled=True consec=6 daily_dd=8% weekly_dd=15%`. Stop-only (no entry-open/resume code path). Manual reset only.
- **New load-bearing fact — equity baseline:** `kill_switch_equity_base_usd=None` → DD uses broker NetLiq (~$1M) → 8%/15% ≈ $80k/$150k, **near-inert** for 2-contract MNQ. The **6-consecutive-loss trigger is the effective brake.** To make DD bite, set `kill_switch_equity_base_usd` (e.g. `50_000` → trips at −$4k/−$7.5k). Operator's call; not invented.
- **SeanBot validated V3 exit:** fixed SL **75**, trailing TP offset **150** (their `TRAIL_OFFSET=250` is stale/contradicts its own comment). V3 backtest: $10k→$29.7k (+196.9%), WR 32.9%, PF 1.13, Max DD ~40%.

---

## 5. Wrong diagnoses this session — READ BEFORE YOU DEBUG

1. **"TF is stricter than SeanBot on entries (tests close, not low) — that's why it missed the 1:15 entry."** *Evidence that misled:* a screenshot of TF `noop_warmup, sma100=None` at a moment SeanBot entered. *Why wrong:* the read-only code audit showed TF tests the **bar LOW** and matches SeanBot on nearly every gate; TF is equal-or-more-aggressive, not stricter. *Correct diagnosis:* the miss was **warmup blindness** after a redeploy (SMA not yet warm), fixed by #77. **Don't "fix" TF's entry gates — they aren't the problem.**
2. **"The Live page nav is broken (#76)."** *Evidence:* operator screenshot showing no nav. *Why wrong:* the deployed `index.html` already extends `base.html` with all 5 links; htmx refreshes panels but not the page shell, so the screenshot was a **stale/cached frame**. *Correct:* hard browser refresh. The real gap was the missing trades panel, which #76 added.

**Lesson for next session:** probe the deployed code / run the read-only audit before concluding the strategy diverges or a UI element is broken. A single screenshot is a symptom, not a root cause — confirm against `main @ HEAD` and live state.

---

## 6. Verification block — run this before doing anything

VPS CC runs these (operator is hands-off). All assume cwd `~/tradeflow` at launch.

**V0 — is the bot halted? (CHECK FIRST — 5-loss streak)**
```bash
docker logs tradeflow-app 2>&1 | grep -E "\[KILL\]|halt_raised|HALT" | tail -10
```
Expect: `[KILL] poll loop started …`, no `halt_raised`. **If you see a halt raised:** the consecutive-loss trigger fired — the bot is halted + flat. To resume, operator does Telegram `/ack` or `touch /tmp/halt_clear`. Do NOT clear it without the operator's word.

**V1 — loss streak + realized P&L (the kill-switch inputs)**
```bash
docker logs tradeflow-app 2>&1 | grep -E "lifecycle.*CLOSED|pnl_net" | tail -8
```
Baseline at handoff: last-6 `pnl_net` = −19.48, −928.98, −924.98, −2.48, −307.48, +600.76 (5-loss streak). Realized today/week −$1,873.44. **If the streak hit 6 losses, expect a halt (V0).**

**V2 — deployed code truth**
```bash
docker inspect tradeflow-app --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -i COMMIT
git -C ~/tradeflow rev-parse origin/main
```
Expect both = `0ecc9c7…` (or later). Deviation = the container is behind `main` → rebuild.

**V3 — broker truth (position + warmup-from-boot)**
```bash
docker logs tradeflow-app 2>&1 | grep -E "WARMUP-ENABLE|indicators_ready|FLAT|position" | tail -6
```
Expect `[WARMUP-ENABLE] … indicators_ready=True` near boot; confirm FLAT (no open position) unless a trade is live. §0.5.98 — broker is ground truth; if logs and DB disagree, broker wins.

**V4 — warmup convergence diff (still pending — see §9)**
```bash
docker logs tradeflow-app 2>&1 | grep "WARMUP-SHADOW" | tail -5
```
Expect `[WARMUP-SHADOW] bar=N live_sma100=… backfill_sma100=… diff=…`. **The diff only becomes meaningful ~100 live bars after the LAST container restart** — every redeploy resets the live-only accumulation. If `diff≈0` once warm, backfill is trustworthy; if it drifts, fix the historical-fetch plumbing.

---

## 7. Pending work queue

Priority depends on V0/V1 state (if halted, the first task is reviewing the trip), not strictly on this ordering.

### 1. Trailing TP (AUDIT) — brief ready in §13
Replace TF's fixed TP 150 with a trailing-stop TP (offset 150, validated V3), keep the fixed 75 SL, OCA-group both legs (folds in the OCA-at-placement backlog item). Flag `EXIT_MODE` (trailing default, fixed = revert). Measure on `/scoreboard` over weeks. **Dispatch this first in the next session** (after V0/V1 are clean).

### 2. Entry-knob calibration (measure-first) — DO NOT tune blind
Bullish tolerance (`close ≥ open − 2.0`), session-edge (5 vs 30 min), gap (0.5 vs 2.0). Read `/divergence` data accumulated since #75 before changing anything. The strategy is not broken (§5) — only adjust on evidence.

### 3. Kill-switch after-close immediacy hook
Currently a 30s poll → up to 30s lag between a triggering close and the halt. Optional enhancement: evaluate on close, not just on poll. Low priority for paper.

### 4. kill_switch_equity_base_usd decision (operator)
DD triggers are near-inert at NetLiq base. Operator decides whether to set an allocated capital (e.g. 50_000) to make daily/weekly DD meaningful, or leave consec-loss as the sole brake.

### Open items / operational debt
- **Cancel-fix (#71) live proof** — pending the next real STOP/TARGET fill (expect `cancelled sibling order`, no `cancel_sibling_error`, orphan canary 0). Given the 5-loss streak, may coincide with the kill switch's first live trip.
- **OCA-at-placement** — folds into the trailing-TP PR (§13); not a separate PR.
- **Warmup convergence proof** — still needs a ~100-min stretch without a redeploy (§9).
- **8 benign `AsyncMock … never awaited` pytest warnings** — from an orchestrator test now also running the kill poll task at teardown; cosmetic, prod awaits correctly; not chased.

---

## 8. Test safety — why we belabor this

Cumulative mocking failures to keep preventing:
1. Tests green against a fictional schema (mocked column names that don't exist).
2. `side_effect` list wrong count → silent `StopIteration` → wrong assertions. **Count-comment every `side_effect`.**
3. Mocked at the raw library chain when prod uses a wrapper → tests green, prod broken. **Mock at the code's actual boundary.**
4. Shared `MagicMock()` leaking state between tests → **fresh mocks per test.**

This session's PRs followed these (493→507 tests, mocked at the DB / injected-callable / IB boundary, count-commented). Do not ship tests that skip them — the master template (§12) enforces them.

---

## 9. Pitfalls from prior sessions

- **VPS CC committed onto LOCAL `main` before branching (warmup-enable / #77).** Caught before any push-to-origin-main (branch protection is the net), moved the commit to a feature branch, re-checked-out main. **Rule reinforced: create the feature branch off `origin/main` FIRST, before any commit.** CC self-corrected on the very next PR (#78 branched correctly).
- **Warmup convergence diff resets on every redeploy.** The live-only shadow re-accumulates from zero each container restart, so the backfill-vs-live proof needs ~100 uninterrupted minutes. After the rapid #77→#78 redeploys it is **still unproven** — don't claim convergence until V4 shows a warm, near-zero diff over a no-redeploy window.
- **Don't trust a single screenshot as root cause** (§5 — both wrong diagnoses came from screenshots).
- **GitHub PR numbers ≠ brief names** — there's a persistent offset; track real GitHub numbers (#69–#78 this session).

**Next-session rule: if a claim is quantitative, re-verify it.** Loss streak, realized P&L, halt state, open-order count, the convergence diff — re-query, don't trust this doc's numbers.

---

## 10. Session discipline lesson (2026-06-01)

Strong session — 10 PRs, all verified from broker/DB truth, with honest "what I got wrong" on each. The meta-lesson: **the bot's problem was never the strategy; it was instrumentation and readiness.** Two wrong diagnoses both came from reading a symptom (a screenshot) instead of the deployed code. The fix pattern that worked all session: probe → baseline → measured change → verify from source of truth.

**Enforcement rules for next session:**
1. Run §6 (esp. V0 halt check) before any code — the bot may have auto-halted.
2. Dispatch trailing TP (§13) before touching entry knobs; entry knobs are measure-first off `/divergence`.
3. Branch off `origin/main` FIRST. Re-verify every quantitative claim.

---

## 11. Logging verbosity — demand from any new code

- Every entry/exit/halt logs `[COMPONENT] symbol: action — reason`.
- Every state transition logs old→new at INFO; every halt logs the trigger + the inputs.
- Every swallowed exception logs the specific error + context (the kill switch / warmup fail-safes do this).
- Any dedup/select-one-of-many logs which row won and why (`_dedupe_for_upsert`).
- Async background tasks log entry (`task_launched`) and loop start.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill (patch constraints, code quality, test-safety guardrails, known gotchas, "what I got wrong" section). Every PR this session used it. AUDIT PRs post the diff and pause for `merge`; REPORT/AUTO self-merge after CI green.

---

## 13. Current PR brief in flight — hand to VPS CC as-is (trailing TP, AUDIT)

~~~
# TradeFlow — Claude Code PR: Trailing TP (stop cutting winners short) — AUDIT

> MODE: RESOLVE end-to-end. Classification: AUDIT (exit/order path). Implement → test → open PR → post the diff and PAUSE for the operator's single-word `merge`. After merge: deploy, verify, report.
> Run in tmux. §0.5.186/.187 discipline. Branch off origin/main FIRST (baseline = current origin/main, which includes the kill switch 0ecc9c7). Re-checkout -B main origin/main after squash-merge.

## Role
Senior dev on TradeFlow (paper MNQ bounce bot, DUQ331660). TF exits on a fixed TP at entry+150, capping every winner — the audit flagged this vs the SeanBot benchmark. SeanBot's validated V3 lets winners run via a trailing TP (worth +$7.6K/3yr in its backtest: +$19.7K trailing vs +$12.1K fixed). Bring TF's exit to validated V3 shape: fixed SL unchanged + trailing TP, flag-guarded, measured forward.

## Validated V3 spec (probed from SeanBot source — use these exact values)
- Fixed SL = 75pt, NEVER trails (their data: trailing the stop destroys profit). The PR #69 protective stop stays fixed — do not touch it.
- Trailing TP offset = 150pt (their settings.py TRAIL_OFFSET=250 is stale/contradicts its own comment + the backtested Pine trailPts=150.0 — anchor to 150).
- No separate activation threshold: with fixed stop at entry-75 and a trailing stop trailing 150 below the peak, the fixed stop governs until price rises ~+75, then the trail takes over. Replicates V3 naturally.
- Reality: V3 is WR 32.9%, PF 1.13, Max DD ~40% — a low-win-rate runner. Expect bigger winners + higher variance, NOT higher win rate.

## Objective
Replace TF's fixed-150 TP limit with a trailing-stop TP, keep the fixed 75 SL, place both exit legs in one OCA group so a fill on either auto-cancels the sibling at the broker (no dangling exit, no double-fill). Flag-guarded; default trailing; revertible to fixed with no code change.

## Constraints
- Never touch the downside floor — fixed SL stays at entry-75, never trails. Trail is profit-side only.
- OCA at placement (folds in the backlog item): {fixed SL, trailing TP} as an OCA group = the app-down-at-fill defense. cancel_order_by_id (#71) stays the app-level/reconnect path; both coexist.
- Prefer a native IB trailing-stop order (broker trails tick-by-tick, survives disconnects, matches Pine) over in-app bar-by-bar stop-modify. Probe ib_async TRAIL + the current bracket/OCA wiring (Task A); fall back to in-app bar-trail ONLY if native isn't workable, and flag the choice.
- Entry selection unchanged — only the exit changes. src/strategy.py gate logic + entry signal = empty diff.
- Flag + revert: EXIT_MODE ("trailing" default | "fixed" = pre-PR behavior). TRAIL_OFFSET configurable, default 150. Flipping to fixed must need no code change.
- Fail-safe: if trailing-exit placement fails, still place at least the fixed SL — never leave a naked position, log loudly, never raise into the order loop.
- AUDIT: post the diff, pause for merge.

## Tasks
A — audit (probe first): where TF places the entry bracket + protective SL (#69) + the fixed-150 TP; how exits are grouped/cancelled today (OCA or app-cancel only?); whether ib_async cleanly supports TRAIL + OCA on this MNQ contract. Quote the placement sites. Confirm current TP is a fixed limit at entry+150.
B — implement: behind EXIT_MODE="trailing", replace the fixed TP leg with a trailing-stop TP (offset TRAIL_OFFSET default 150), keep the fixed 75 SL, OCA-group both. Native IB trail preferred. Fixed-mode path preserved.
C — tests: trailing mode places {fixed SL @ entry-75, trailing TP @ offset 150} in one OCA group (assert on real order objects, not stubs); SL is fixed and never modified to trail; a fill on one leg cancels the sibling; EXIT_MODE="fixed" still places legacy fixed-150 (no regression); placement failure → fail-safe (fixed SL still placed, logged, no naked position, no raise); entry selection unchanged. Mock at the IB boundary, fresh mocks, count-commented side_effects.
D — completeness: empty diff on src/strategy.py gate logic + entry signal; SL provably never trailed (grep/inspection); --stat; black+ruff; full suite green (baseline = current origin/main).
E — out of scope (document, NOT build): SeanBot scripts/ phased/staircase exits (unvalidated experiments — do not pull TF toward them); entry-knob changes (measure-first).
F — post-merge: deploy (warms from boot, no blind window); verify a real entry places trailing TP + fixed SL in an OCA group (working-order snapshot or dry placement, WITHOUT forcing a live trade); confirm EXIT_MODE reads trailing; confirm /trades records exit reasons so the scoreboard can later separate trail-exits from stop-exits. Report.

## Output
Files + --stat; Task A placement sites (quoted) + native-vs-in-app decision; OCA wiring; how to revert to fixed; test tails; "SL never trails" + "no naked position on failure" evidence; MUST-NOT empty diffs; Task F results; "What I got wrong".

## Gotchas
1. SL stays fixed at 75 — never trail it. 2. Offset = 150, not the stale 250. 3. OCA-group the exit legs at placement; keep cancel_order_by_id as the app-level path. 4. Fail-safe: placement failure → still place fixed SL, never naked, never raise. 5. Don't touch entry selection. 6. Measured change: trailing default but EXIT_MODE=fixed is the no-code revert — judge on the scoreboard over weeks. 7. AUDIT — post diff, pause for merge; deploy + verify after. §0.5.186/.187.
~~~

---

## 14. Canonical references (in order of authority)

1. **Source code on `main` @ `0ecc9c7`** — what actually runs.
2. **Production DB** (`lifecycles` via service role) — truth for P&L / loss count / state (§0.5.98).
3. **IBKR** via `ib_async` (paper DUQ331660) — truth for position / fills / NetLiq.
4. **Dashboard** `/scoreboard` + `/divergence` — measured TF-vs-SeanBot truth (read-only views; DB-sourced).
5. **This handoff (v16)** — session context, NOT long-term authority.
6. **v15 and earlier handoffs** — historical; ignore any claim contradicting 1–4.

---

## 15. First 15 minutes of the next session

1. Read §0.5, §1, §4, §5, §9 of this handoff. **Most important: §1's 5-loss-streak warning.**
2. VPS CC pre-flight: `git -C ~/tradeflow fetch && git -C ~/tradeflow pull --ff-only origin main && ls -t docs/handoffs/ | head -3`, then read this handoff from disk.
3. Run §6 (V0 first): **is the bot halted?** If yes, review the trip with the operator before clearing. If no, confirm FLAT + commit `0ecc9c7`.
4. Dispatch the trailing-TP brief (§13) to VPS CC — AUDIT, it pauses for the operator's `merge`. (Skip only if the bot is mid-trip and the operator wants to handle that first.)
5. While VPS CC builds: glance at `/divergence` + `/scoreboard` data accumulated since #72/#75 — that's the input for the (later) entry-knob calibration.
6. On the trailing-TP diff: review the OCA wiring + native-vs-in-app trail choice + the "SL never trails" evidence with the operator → `merge` → deploy → draft the smoke-test runbook (`vps-smoke-test-runbook` skill).

---

## 16. How to publish this handoff

**Path A — VPS CC brief (preferred — no operator browser):** paste the block below to VPS Claude Code.

~~~
You are VPS Claude Code on the TradeFlow VPS. Pre-flight first:

  git -C ~/tradeflow fetch origin
  ls -t ~/tradeflow/docs/handoffs/ | head -5

Determine N: this handoff is numbered v16. If the latest existing handoff in
docs/handoffs/ is NOT v15, then N = (latest existing version) + 1 — in that case
update the filename AND the "v16" references in the doc body to vN before saving.
Otherwise N = 16.

Save the handoff content (provided separately / from /tmp/HANDOFF_v16.md) verbatim
to ~/tradeflow/docs/handoffs/HANDOFF_v<N>.md, then:

  git -C ~/tradeflow add docs/handoffs/HANDOFF_v<N>.md
  git -C ~/tradeflow commit -F /tmp/handoff_commitmsg.txt
  git -C ~/tradeflow push origin main

where /tmp/handoff_commitmsg.txt contains:
  docs: add v<N> handoff (kill switch live; warmup-enabled; trailing TP queued)

NOTE: this is a docs-only commit to main (allowed — handoff publish is the one
push-to-main exception per the handoff rule; it touches no prod code or secrets).
If branch protection blocks a direct docs push, open a 1-file PR instead:
  git -C ~/tradeflow checkout -b docs/handoff-v<N> origin/main
  (add+commit as above) then:
  git -C ~/tradeflow push -u origin docs/handoff-v<N>
  gh pr create --repo ohad-oren111/tradeflow --base main --head docs/handoff-v<N> --title "docs: v<N> handoff" --body "Session 16 handoff." 
  gh pr merge --repo ohad-oren111/tradeflow --squash --delete-branch

Confirm: the file exists, `git -C ~/tradeflow log --oneline -1` shows the commit,
and `git -C ~/tradeflow status` is clean. Report the final HANDOFF_v<N>.md path
and the commit hash.
~~~

**Path B — manual fallback (if VPS CC unavailable):**
```bash
scp HANDOFF_v16.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v16.md
ssh tradeflow@5.78.212.37 "git -C ~/tradeflow add docs/handoffs/HANDOFF_v16.md && git -C ~/tradeflow commit -m 'docs: add v16 handoff (kill switch live; warmup-enabled; trailing TP queued)' && git -C ~/tradeflow push origin main"
```

The handoff exists only once saved to disk and committed. Until then, treat this as draft.

---

*End of handoff v16. Target lifespan: until trailing TP is merged + the bot has run a no-redeploy window long enough to (a) prove warmup convergence (§9 V4) and (b) get the cancel-fix + kill-switch first live exercise. Then rely on `main @ HEAD` + whatever v17 captures.*
