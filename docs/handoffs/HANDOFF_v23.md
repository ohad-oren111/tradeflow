# TradeFlow — Handoff v23 (regime gate LIVE + armed + durable + behaviorally proven; force-fill Error 321 dead-ratchet fixed; concurrency MERGED-but-HELD; Phase 3 clean-measurement begins)

*Handoff from end of 2026-06-05 (~20:25 UTC). Bot is **armed and FLAT**, healthy on deployed HEAD `237a750`. This session closed two mechanical leaks and proved both: (1) the force-fill protective stop was dying on IBKR Error 321 and pinning the ratchet to a dead id (round-trip-to-base re-opened) — fixed by #114; (2) TF was buying longs into downtrends with no regime filter — the regime gate is now LIVE, armable, durable through feed gaps, and behaviorally proven (first `noop_regime` block fired on the first eval after deploy). The bot now only takes its intended setup (above-trend pullback longs) with a working trailing exit. **We are entering Phase 3: measure-don't-touch.** The edge question is still open and is exactly what Phase 3 answers. This doc captures everything a fresh chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read §§0.5, 1, 2, 4, 5, 10 first — that's the state of the system and the lessons. §§7–13 are reference. §14 is the authority order when this doc disagrees with itself or a live observation.

**Do not trust this doc alone.** Run the §6 verification block before writing any code. **Critical first action: confirm deployed `TRADEFLOW_COMMIT=237a750` (or a later docs commit), that `RISK.regime_gate_enabled=True` AND `[REGIME-ARMABLE] would_arm=True gate_enabled=True` in the running container, and read live broker state (the bot was FLAT at handoff). "Enabled" ≠ "armed" ≠ "blocking" — verify all three (§0.5.215).**

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner is a copy-paste-ready block. Owner (Ohad) is a hands-off PM: he pastes briefs into VPS Claude Code (CC VPS), gives single-word approvals, watches Telegram + dashboard. Minimize his UI/browser steps — prefer one SSH command or one CC VPS brief.

**Learning-delivery discipline.** Surface each new fact/bug-pattern/corrected-assumption immediately as a paste-ready snippet, not at end-of-session.

**Read before diagnosing.** Read full startup log + 3–5 full cycle narratives / the source of truth before proposing a root cause. Diagnosing from `grep | wc -l` is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth** (broker API, Supabase, raw log) before escalating urgency language.

**Always draft a VPS smoke-test runbook after a PR merge** unless told otherwise — the owner does not run smoke tests by hand. (See Appendix A.)

**Always run CC VPS inside a named tmux session (`tf1`/`tf2`).** Launch tmux before starting work so SSH disconnects are non-events; `claude --resume` to restore context after any unplanned disconnect.

**The §0.5.x numbered registry is canonical in `CLAUDE.md` on `main`** (read at pre-flight). It never shrinks. Load-bearing carry-forwards from prior handoffs: §0.5.97 (probe external specs from source — never re-derive MNQ contract/fees/schema from memory); §0.5.98 (broker/exchange state = ground truth, not internal DB — **this session applied it to our own workflow: don't trust "I merged it", confirm the live container**); §0.5.158 (compose service `ib-gateway` ≠ container `tradeflow-ib-gateway`); §0.5.159 (`.tradeflow-secrets/.env` shadows `${VAR:-default}` — use a literal, not interpolation, for values that must be deterministic); §0.5.160 (new PR branches off `origin/main`); §0.5.186/187 (CC VPS Bash discipline: no heredocs, no `cd &&`, no `;`, no `$(...)`, no `${VAR}`, no `VAR=` prefixes — use `git -C`, Write tool to `/tmp`, `python3 /tmp/x.py` for interpolation, `--body-file` for PR bodies, Python/loop polling; **`gh pr checks --watch` exits prematurely — poll with a loop**); §0.5.202 (force-recreate silently un-halts a *halted* bot — confirm halt state before recreate); §0.5.205 (gateway AUTO-OCAs parentId-linked bracket children; a modifiable stop must be standalone `parentId=0`); §0.5.206 (protective stop = stop-MARKET; a few pts slippage is normal); §0.5.207 (NEVER-ORPHAN: cancel the standalone stop on any non-stop close); §0.5.208 (lane order STABILIZE > REPLICATE > MEASURE > OPTIMIZE; never stack on an unconfirmed foundation; one change at a time); §0.5.209 (kill-switch restart = manual halt-ack); §0.5.210 (ratchet armed only in router parent-fill / boot recovery / reconciler force-fill); §0.5.211 (deploy over an open trailing position is safe — standalone GTC stop survives restart, boot recovery re-arms); §0.5.212 (the `create_lifecycle` TOCTOU double-entry race — **fixed this session by GATE-1/#111**); §0.5.213 (`ocaType=3` benign on a standalone stop); §0.5.214 (handoff publish needs `--admin`; never `--watch`); §0.5.T4/T5 (kill switch raises in-process halt+flatten; a position must never be naked).

**New this session — §0.5.215–219 (write into `CLAUDE.md`):**
- **§0.5.215 — A risk gate has THREE distinct states; verify all three.** *Enabled* (config flag on) ≠ *armed* (data buffer deep enough that it evaluates instead of fail-opening) ≠ *blocking* (actually rejecting entries). The regime gate was enable-able but **permanently inert** for want of buffer depth. Always confirm from the live container: `RISK.<flag>=True`, `[REGIME-ARMABLE] would_arm=True`, and a real `noop_regime` block in the evals. A green flag alone proves nothing.
- **§0.5.216 — The regime gate (`strategy._regime_ok`, "SeanBot-C1").** Blocks a LONG when the latest 1-min close ≤ the 30-min EMA200 (1-min closes resampled to 30-min, EMA span=200). It needs **≥202 valid 30-min buckets** (~6,060 contiguous 1-min bars) or it **fail-opens** (allows the long; also fail-open on any exception). The strategy bar buffer is `deque(maxlen=_BAR_BUFFER_MAX)`, now **7000** (was 150). It short-circuits before the touch/bullish/ma_order gates (those log `None` when regime blocks). Diagnostic: `[REGIME-ARMABLE] thirty_min_buckets=N needed=202 would_arm=… gate_enabled=…`; block line: `[STRAT] regime gate BLOCKED entry: price=… <= 30m EMA200=…`.
- **§0.5.217 — IB ACCEPTS a "10 D" 1-min `reqHistoricalData` (~14,900 bars).** Despite the old "5 D cap" caution in `ib_client.get_historical_bars`, the live gateway returned 14,878 and 14,903 bars on two "10 D" requests this session. The regime warmup window is **"10 D" with a "5 D" fallback** (`orchestrator._fetch_warmup_seed`); the fallback exists for a rejection and was never exercised. (Resolves the v23 deploy-time uncertainty.)
- **§0.5.218 — The gap-reseed REPLACES the buffer, it does not patch.** `_reseed_strategy_after_gap` calls `strategy.invalidate()` → `_bars.clear()` then reseeds from history. So any buffer-depth-dependent gate (regime) MUST reseed via the full "10 D" window on the gap path too (now does, #118) — otherwise the gate disarms for ~5h after every feed gap (stale-bar watchdog / farm-flap / reconnect). Boot AND gap-reseed both route through `_fetch_warmup_seed`.
- **§0.5.219 — Concurrency is MERGED but HELD on two independent layers.** (a) compose literal `MAX_CONCURRENT: "1"` in the `tradeflow-app` environment block (literal so `.env` can't shadow it, §0.5.159); (b) the ≤1 partial unique DB index `lifecycles_one_open_per_symbol_strategy` is still in place — PR-B's index-swap migration was **NOT applied**. Do NOT enable concurrency (do not apply the index swap, do not unset/raise the pin) until single-position expectancy is proven positive (Phase 4). Concurrency multiplies expectancy — never turn it on over a net-negative strategy.

---

## 1. Where we are (as of handoff, 2026-06-05 ~20:25 UTC)

### Live production state
- `tradeflow-app` recreated **2026-06-05 20:22Z**, healthy, deployed commit `237a750` (verified `TRADEFLOW_COMMIT=237a750…` in the container env this session).
- `tradeflow-ib-gateway` healthy; `tradeflow-telegram-listener` up.
- Broker (DUQ331660 paper, ~$996k NetLiq): **FLAT** — MNQM6 pos=0, no resting orders (`recovery_loaded count=0`, broker truth confirmed not naked).
- **Regime gate: LIVE, ARMED, DURABLE, and behaviorally PROVEN.** `RISK.regime_gate_enabled=True`, `MAX_CONCURRENT=1`; boot `[REGIME-ARMABLE] thirty_min_buckets=234 needed=202 would_arm=True gate_enabled=True`; first `noop_regime` fired on the first post-deploy eval (`regime gate BLOCKED entry: price=28850.00 <= 30m EMA200=30283.10`).
- **Concurrency HELD OFF** (two layers, §0.5.219): compose pin `MAX_CONCURRENT="1"` + ≤1 DB index (PR-B index swap not applied).
- Restart policy `unless-stopped`. `ALLOCATION_USD` unset → 33% drawdown brake inert; the **10-consecutive-loss kill-switch is the only active hard brake** (warns at 6, fired warnings repeatedly on 06-05; halts at 10). With the gate on, expect **fewer entries** — it sidelines TF in downtrends.
- Front month: **MNQM6** (conId 770561201).

### What just shipped (this session — all merged to `origin/main`, HEAD `237a750`)
- **#111 GATE-1** (`2425a67`) — race-proof `create_lifecycle`: `asyncio.Lock` + probe fast-path + partial unique index `lifecycles_one_open_per_symbol_strategy` `UNIQUE(symbol,strategy) WHERE state<>'CLOSED'`; insert-409 → `InvariantViolationError`. Owner applied the index migration via the Supabase dashboard. Closes §0.5.212.
- **#112 PR-A** (`1ade668`) — concurrency machinery N-correct behind `max_concurrent` (default 1 = no-op): ratchet iterates `_active_trailing_lifecycles()`, recovery/kill-switch confirmed N-correct. Deployed, verified no-op.
- **#113 PR-B** (`0ca9ba1`) — concurrency ON in code (`max_concurrent=3`, aggregate cap 8, same-setup dedup via `metadata.setup_key`). **MERGED but DEPLOY HELD** — the index-swap migration was NEVER applied; concurrency stays off (§0.5.219).
- **#114** (`d801792`) — **force-fill protective-stop Error 321 / dead-ratchet fix:** in `ensure_protective_stop` (router.py ~1259) default `contract.exchange="CME"` when missing, mirroring siblings (router.py:799, reconciler.py:378). Deployed + verified; mechanism-proven 3/3 on the 06-05 17:40Z trade.
- **#115** (`2482b0b`) — pin `MAX_CONCURRENT: "1"` literal in `docker-compose.yml` (durable concurrency-hold on `main`).
- **#117** (`04b7652`) — **regime gate ARMABLE:** `_BAR_BUFFER_MAX` 150→7000; boot warmup seed "10 D" with "5 D" fallback (`_fetch_warmup_seed`); `[REGIME-ARMABLE]` diagnostics; new `thirty_min_bucket_count` helper + `regime_bucket_count()`. Gate stayed OFF. Verified live `would_arm=True` (234 buckets, IB returned 14,878 bars for "10 D").
- **#118** (`82aeed4`) — **gap-reseed ARMABLE:** route `_reseed_strategy_after_gap` through `_fetch_warmup_seed` (10 D / 5 D fallback); `[REGIME-ARMABLE … post-gap-reseed]`. Keeps the gate armed through feed gaps (§0.5.218).
- **#116** (`237a750`) — **`REGIME_GATE_ENABLED: "true"`** in the compose env. Gate LIVE + armed + behaviorally proven.

### What we discovered this session (evidence in §2/§4/§5)
- **The exit was NOT actually fixed on the common path until #114.** The force-fill protective stop was placed with an `IB.positions()` contract that omits `exchange` → IBKR **Error 321** cancels it ~1ms later → the reconciler heals with a new id the router never re-adopts → the ratchet stayed pinned to the dead id forever (**1,376 `ratchet_no_resting_stop`, 0 clean ratchets** on the PR-A build; all positions round-tripped to base). The re-opened GATE-ZERO failure. The "+590/+651" wins celebrated earlier were on the **old boot-recovery path** (adopted an already-healed live stop), not the common force-fill path. Fixed by #114; mechanism-proven 3/3 on a losing trade.
- **TF's losses are concentrated in below-trend long entries.** price<sma100: **−$5,267** (n=16) vs price≥sma100: **+$31** (n=11, flat). The regime gate (30-min EMA200) sim: cuts net loss **~50% on-sample** (−$5,236 → −$2,605); **~78% on the go-forward de-duplicated book** (−$3,382 → −$751, after removing the already-fixed 06-01 double-entry −$1,854).
- **SeanBot does NOT profit below-MA** (+$96 over n=6 = noise); SB lost −$1,880 *above* EMA200; **SB net ≈ −$1,784 this window**. Both bots lost — a hard down-and-chop tape. SB's relative edge is exit capture (fast +40–50 lock) and losing less, **not** an entry-regime signal it sees and TF doesn't.
- **The regime gate was PERMANENTLY INERT before #117/#118** (buffer `maxlen=150` → ~5 thirty-min buckets vs 202 needed → always fail-open; even a "5 D" boot seed ≈ 192 buckets, just under). Now armable (7000 buffer, 10 D seed → ~234 buckets).
- **Perf** (measured, host venv): per-bar DataFrame+indicators 6ms (150 rows) → 13ms (7000 rows); 30-min resample 0.7ms at 7000 rows. Negligible at 1 bar/min — resolved the minimal-buffer-vs-dedicated-30m-series architecture fork **in favor of the minimal buffer bump**, on data not opinion.

---

## 2. The session's work thread

1. **Diagnosed the `ratchet_no_resting_stop` flood from raw logs** (not greps): the force-fill stop is dead-on-arrival (Error 321, missing `exchange`) → ratchet pinned to a dead id → round-trip-to-base. Confirmed git-blame: latent since PR #69, **not** a PR-A regression. → **#114** (default exchange CME). Merged, deployed, verified.
2. **First "STOP MOVED" looked like the ratchet working** (06-05 11:41) — caught that it was a **base-stop correction** off the actual avg fill (still "protecting −75.06 pt", a loss level); the trade stopped out −$303.72. NOT a profit ratchet (§5.1). #114's *mechanism* was then proven 3/3 on that trade (live exchange-bearing stop, ratchet tracked the live id), but the **profit-walk on a winner remains unproven** on the #114 build.
3. **Owner premise "SB is staying out, we keep buying"** — corrected with CC's SB-by-regime data: SB also enters below-MA (27 below / 34 above) and also lost. SB isn't staying out; the divergence is exit/timing + losing less (§5.2).
4. **Regime analysis (read-only)** pinned the loss to below-trend entries (above +$31 / below −$5,267). Verdict: enable the coded-but-off regime gate.
5. **Probed the gate before flipping** — found it tests a 30-min EMA200, and (critically) that it was **permanently inert** (buffer too small). The warmup probe caught a false-protection trap (§5.5).
6. **Built the armable fix incrementally:** #117 (boot seed 10 D + buffer 7000) → verified `would_arm=True` live → #118 (gap-reseed 10 D, durable through gaps) → #116 (enable). Each step verified from logs before the next.
7. **#116 was believed merged but wasn't** — a ground-truth check (`gh pr view` + container env) found it still OPEN, gate still off (§5.4). Re-did it in the right order (#118 then #116, one deploy).
8. **Gate went live and proved itself** — first `noop_regime` blocked a long at 28,850 (≈1,433 pts below EMA200) on the very first post-deploy eval.

Closed rabbit holes: "the +590 winner proves the exit works on current code" (it was old boot-recovery code); "the first STOP MOVED is the ratchet walking" (base correction on a loss); "SB stays out of downtrends" (it doesn't; it also lost); "enabling the flag turns the gate on" (it was inert until the buffer fix); "#116 is merged" (it wasn't).

---

## 3. What the system is actually made of

**Single source of truth:** `CLAUDE.md` on `main` at `237a750` (the §0.5.x registry + autonomy contract).

Highlights:
- **Live entry paths (two):** (1) TF's own SMA100-touch gate (main `run()` loop); (2) SeanBot-triggered validity-checked path (`_maybe_enter_on_seanbot`, separate `asyncio` task). Both dispatch through `_handle_trade_signal → place_entry → create_lifecycle`. The double-entry race they used to share is now closed (#111: lock + DB partial unique index).
- **Entry regime filter (NEW live):** `strategy._regime_ok` blocks LONGs when price ≤ 30-min EMA200; needs ≥202 thirty-min buckets or fail-opens. Buffer `maxlen=7000`; boot + gap-reseed both seed via `_fetch_warmup_seed` ("10 D"/"5 D"). (§0.5.216/217/218.)
- **Exit (STABILIZE-5 + #109 + #114):** standalone `parentId=0` STP placed post-fill by `ensure_protective_stop` (now exchange-normalized); bar-close ratchet walks it. Armed in three places (§0.5.210). NEVER-ORPHAN cancel on non-stop close.
- **Concurrency machinery (merged, held):** `max_concurrent` (pinned 1), aggregate contract cap, same-setup dedup via `lifecycles.metadata.setup_key`. Off on two layers (§0.5.219).
- **Reconciler:** `SeanbotReconciler` polls `seanbot_signals`; `Reconciler._reconcile_entering` force-fills missed entries (the common path) and arms the ratchet.
- **Dashboard/scoreboard:** `dashboard/trades.py` (TF P&L), `dashboard/scoreboard.py` + `dashboard/seanbot_authoritative.py` (SeanBot trusted vs `est.` reconstruction).

---

## 4. Verified facts (2026-06-05) — DO NOT challenge unless the schema/contract migrates

- **`lifecycles`** keyed by `lifecycle_id`; `lifecycle_events.emitted_at` (not `created_at`); concurrency uses `lifecycles.metadata.setup_key`. TF dashboard column = `lifecycles.pnl_net` grouped by `exit_filled_at` UTC date. Trading started **05-29** (the "TF newly live 2026-06-01" caption is wrong — MEASURE item).
- **MNQ spec (§0.5.97-verified):** TICK 0.25 pt, MULTIPLIER $2/pt, COMMISSION_RT $0.62, day-trade margin $2,000. 2-lot RT friction ≈ 1.12 index pts.
- **Trailing config (boot log):** `EXIT_MODE=trailing — stop_loss=75.0 lock_in=50.0 trail_offset=150.0 hard_ceiling=1000.0 take_profit=150.0 (bracket=STP-only + bar-ratchet)`.
- **New load-bearing facts (this session):**
  - **§0.5.216** regime gate = 30-min EMA200, ≥202 buckets or fail-open. Evidence: `[REGIME-ARMABLE] thirty_min_buckets=234 … would_arm=True` and `regime gate BLOCKED entry: price=28850.00 <= 30m EMA200=30283.10`.
  - **§0.5.217** IB accepts "10 D" 1-min (~14,900 bars). Evidence: `get_historical_bars — symbol=MNQM6 bar_size=1 min bars=14903`; buffer seeded `bars=7000 duration=10 D`.
  - **§0.5.218** gap-reseed clears+replaces the buffer (`strategy.invalidate()`), so it must also use "10 D".
  - **§0.5.219** concurrency held on two layers; `RISK.max_concurrent` resolves to 1 in the container (verified).
  - **Exit P&L attribution:** below-EMA200 (gate-blocked) = 11 trades 1 win −$2,631; above-EMA200 (allowed) = 16 trades 4 wins −$2,605 (incl. the −$1,854 already-fixed double-entry → ~−$751 go-forward). The gate is loss-mitigation on down-legs, **not** a cure for the above-trend bleed.

---

## 5. Wrong diagnoses — READ BEFORE YOU DEBUG

1. **"The first STOP MOVED (06-05 11:41) is the ratchet walking — the exit works."** Evidence: a `STOP MOVED` alert fired on a force-fill entry. **Wrong** — the move was 29,412.75 → 29,415.25, the alert said *"protecting −75.06 pt"* (still a loss level), and it was just the base stop corrected to entry−75 off the *actual avg fill*. The trade stopped out −$303.72 three minutes later. The profit-walk on a real winner is **still unproven** on the #114 build.
2. **(Owner premise) "SeanBot is staying out; we keep buying."** Evidence: TF kept firing into the down-leg. **Partially wrong** — CC's SB-by-regime data shows SB also took below-MA entries (27 below / 34 above) and also lost (~−$1,784). SB doesn't simply stay out; the divergence is exit/timing + losing less. The correct shared truth: both bots bled in this tape; TF more, via the dead-ratchet (now fixed) + no regime filter (now on).
3. **(Chat/orchestrator) "We still need to run the regime analysis — send the mission."** **Wrong** — CC had already completed that exact analysis (the EMA200 simulation) in its prior report; I asked to re-send it. Corrected the next turn. Lesson: read what CC already returned before re-queuing.
4. **"#116 is merged; the gate is on."** Evidence: the owner typed a merge command and reported "merged". **Wrong** — a ground-truth check (`gh pr view 116` = OPEN; container env had no `REGIME_GATE_ENABLED`; `gate_enabled=False`) found it never landed. The gate was still off. Re-did it in the right order. This is §0.5.98 (source-of-truth over story) applied to our **own** workflow.
5. **"Enabling `REGIME_GATE_ENABLED` turns the gate on."** **Wrong/incomplete** — the gate was **permanently inert**: `_regime_ok` resamples a `maxlen=150` buffer → ~5 thirty-min buckets vs 202 needed → always fail-open. Caught by the warmup probe *before* we trusted false protection. Required #117 (buffer 7000 + 10 D seed) and #118 (gap-reseed) to actually arm.

**Lesson for next session:** every wrong turn was *story over source*. The fix each time was hitting the live container / reading source: the alert's own "protecting −X" text, CC's already-returned report, `gh pr view` + the container env, the buffer maxlen vs the resample threshold. And for any gate: **verify enabled AND armed AND blocking (§0.5.215)** — never infer protection from a flag.

---

## 6. Verification block — run this before doing anything

Run as CC VPS inside tmux. (CC VPS Bash discipline: no `$()`, `;`, heredocs, `${}`, `VAR=`.)

**V0 — deployed commit + flags (grep the running container, not just `main`)**
```bash
git -C /home/tradeflow/tradeflow fetch origin
git -C /home/tradeflow/tradeflow log --oneline -1 origin/main
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' tradeflow-app | grep -iE "TRADEFLOW_COMMIT|REGIME_GATE_ENABLED|MAX_CONCURRENT"
```
Expect: origin/main HEAD `237a750` (or a later docs commit on top); `TRADEFLOW_COMMIT=237a750…`, `REGIME_GATE_ENABLED=true`, `MAX_CONCURRENT=1`. Any mismatch → build + `up -d --force-recreate` (safe over a position, §0.5.211).

**V1 — gate live + armed + blocking (the three states, §0.5.215)**
```bash
docker exec tradeflow-app python -c "from config.risk_params import RISK; print('regime', RISK.regime_gate_enabled, 'max_conc', RISK.max_concurrent)"
docker logs tradeflow-app 2>&1 | grep -iE "REGIME-ARMABLE" | tail -2
docker logs tradeflow-app 2>&1 | grep -ciE "regime gate BLOCKED|decision=noop_regime"
```
Expect: `regime True max_conc 1`; `[REGIME-ARMABLE] thirty_min_buckets>=202 would_arm=True gate_enabled=True`; `noop_regime` count > 0 while price is below the 30-min EMA200 (gate screening). **If `would_arm=False`** → the gate disarmed (short buffer — e.g. a failed 10 D fetch, or a feed gap on pre-#118 code) → investigate per §0.5.216/218 before trusting protection.

**V2 — this session's code is in the running image**
```bash
docker exec tradeflow-app grep -c "exchange defaulted" /app/src/execution/router.py
docker exec tradeflow-app grep -c "_fetch_warmup_seed\|post-gap-reseed" /app/src/orchestrator.py
docker exec tradeflow-app python -c "from src.strategy import _BAR_BUFFER_MAX; print('buffer', _BAR_BUFFER_MAX)"
```
Expect: router ≥1 (#114), orchestrator ≥2 (#117/#118), buffer `7000` (#117). A `150` or any `0` = stale image → rebuild + force-recreate.

**V3 — broker truth (ground truth, §0.5.98 / TOCTOU canary §0.5.212)**
```bash
/home/tradeflow/tradeflow/.venv/bin/python /tmp/tf_broker_truth.py
```
Expect at handoff: MNQM6 pos=0, no resting orders (flat, not naked). If in position: a standalone GTC STP must be resting (entry−75 base or ratcheted above), and broker contract count == sum of ACTIVE lifecycle `entry_qty` (no extra bracket). If `/tmp` was cleared, recreate the read-only probe (clientId 97, `reqPositions` + `reqAllOpenOrders`).

**V4 — the two proofs still WANTED (watch-items, no action)**
```bash
docker logs tradeflow-app 2>&1 | grep -iE "trailing_stop_ratcheted|protecting \+|REGIME-ARMABLE.*post-gap-reseed"
```
A `trailing_stop_ratcheted` whose Telegram alert reads *"protecting +X pt"* on a **winner** on the #114 build = the exit profit-walk (UNPROVEN at handoff; awaits an above-EMA200 winner now that the gate blocks the below-trend losers). A `[REGIME-ARMABLE … post-gap-reseed] would_arm=True` = #118 keeping the gate armed through a feed gap (awaits the next natural gap; the unit test is the standing proof until then).

**V5 — kill-switch + concurrency-hold**
```bash
docker logs tradeflow-app 2>&1 | grep -iE "kill_switch|consecutive_losses" | tail -3
docker exec tradeflow-app python -c "from config.risk_params import RISK; print('max_conc', RISK.max_concurrent, 'agg_cap', RISK.aggregate_contract_cap)"
```
Expect: kill-switch warns at 6 consecutive losses, halts at 10; `max_conc 1`. **If `max_conc` != 1 → concurrency accidentally enabled → STOP (§0.5.219).**

---

## 7. Pending work queue

Priority depends on V1/V3/V4 state, not list order. **We are in Phase 3 (measure-don't-touch) — ship nothing unless something genuinely breaks.**

### Phase 3 — RUN CLEAN AND MEASURE (the active priority, no code)
With the exit fixed (#114) and the gate live+durable (#116/#117/#118), the bot now only takes above-EMA200 pullback longs. Let it run untouched and gather the first uncontaminated expectancy. The clock is counted in **qualifying (above-trend) trades**, not calendar days — the gate will keep TF quiet in downtrends (that is correct behavior, not breakage). **Pre-committed go/no-go bar:** net expectancy positive and profit factor > ~1.2 over ~20–30 qualifying closed trades. Positive → tune to widen the edge. Negative in its own intended regime → strategy-level decision (rethink entry, add shorts, or shelve), not another patch.

### WATCH-ITEMS (no action; confirm in logs as they occur)
- Exit profit-walk on a winner (`trailing_stop_ratcheted` → "protecting +X" on an above-trend winner). Still unproven on the #114 build.
- Gap-reseed re-arm (`[REGIME-ARMABLE … post-gap-reseed] would_arm=True`) on the next natural feed gap (#118).

### QUEUED PR (HELD until after the measurement window or if it actually bites) — router stop-id refresh on heal-replace
The router's in-memory `stop_order_id` (the ratchet's cached id) is not refreshed when the reconciler heal **re-places** a protective stop leg with a new id. #114 removed the **common** trigger (the force-fill stop no longer dies on Error 321), but a genuine cancel/heal-replace (real disconnect, OCA sibling cancel) would still leave the ratchet pinned to a dead id → `ratchet_no_resting_stop` → round-trip-to-base. Latent robustness gap. **Full brief in §13.** Do NOT ship during Phase 3; resume after the measurement window, or immediately if `ratchet_no_resting_stop` reappears in V4.

### CONCURRENCY — MERGED but HELD (§0.5.219); do NOT enable until Phase 4 says the strategy is positive
PR-B is on `main`; held by the compose pin + the ≤1 DB index (index-swap migration not applied). Enabling = apply the index swap + raise/unset `MAX_CONCURRENT`. Only after single-position expectancy is proven positive. Concurrency multiplies expectancy — never on a net-negative strategy.

### MEASURE — scoreboard trust (low lane)
SeanBot `est.` days are unverified reconstructions; the headline gap rests heavily on pre-TF-live + est. days; by clean reconstruction SB was ~−$1,784 this window (also negative). Fix the wrong "TF newly live 2026-06-01" caption (first trade 05-29).

### Operational debt
- `/tmp/tf_broker_truth.py` and forensic/probe scripts are ephemeral — recreate from §6/§4 if `/tmp` was cleared (all read-only).
- **Security flag (low):** earlier in the session a broad `IBKR_` env grep printed gateway credentials into a CC transcript. Nothing was changed. If those lines included the gateway password, rotate when convenient.

---

## 8. Test safety — why we belabor this

Carry-forward failure modes the suite must keep guarding: tests green against a fictional schema (mock real column names — `lifecycle_id`, `emitted_at`, `metadata.setup_key`); `side_effect` list count mismatch → silent `StopIteration` (always comment the call ordering); mocking the raw library when prod uses a wrapper (custom httpx `SupabaseClient`, never `supabase-py`); shared `MagicMock()` leaking between tests; `RISK` is a **frozen dataclass** — patch `src.<module>.RISK` (the module symbol), not the instance. Match the async test decorator a **neighbor** uses (`asyncio_mode=auto` in this repo → plain `async def`, no decorator — verify, don't assume). This session's new tests cover: exchange-default on force-fill stop (#114); buffer cap ≥ 202×30, bucket-count helper, seed retains >150 (#117); gap-reseed requests "10 D" + falls back to "5 D" + re-arms (#118). **Baseline is 690 green** (was 658 at v22). Do not "fix" unrelated reds.

---

## 9. Pitfalls from prior sessions

- Docker `healthy` ≠ broker API healthy (§0.5.151 — the 85-hour silent outage).
- "Stop is fine because the code doesn't set a group" — wrong (§0.5.205).
- "The ratchet is armed because the standalone stop is placed" — wrong (§0.5.210): placement ≠ arming. **And this session: a placed stop that's rejected by Error 321 (missing exchange) is dead-on-arrival → the ratchet pins to a dead id (#114).**
- "A green config flag means the gate protects us" — wrong (§0.5.215): enabled ≠ armed ≠ blocking.
- "I merged it" / "it's deployed" — re-verify against `gh pr view` + the container env before building on it (§5.4).
- Handoff/Telegram numbers drift — **if a claim is quantitative (P&L, peak excursion, bucket count, contract count), re-verify it from source.**
- `gh pr merge --squash` on a protected branch needs `--admin` AND the required check must **complete** first; `--admin` does NOT skip an in-progress check; `gh pr checks --watch` exits prematurely — **poll** (§0.5.214, §16).
- SSH drop kills in-flight work unless inside tmux — always `tmux new -A -s tf1` first.

**Next session rule: if a claim is quantitative, re-verify it. Especially P&L, bucket counts, and contract counts.**

---

## 10. Session discipline lesson (2026-06-05) — incl. orchestrator's logged comments

**Meta-pattern:** the highest-value work this session was, again, *diagnostic confirmation from the source of truth* — the Error-321 root cause from raw logs, the regime split from recomputed bars + the live container, the gate's permanent-inertness from the buffer maxlen, and the "is #116 actually merged" check. Every conclusion was confirmed from ground truth, not asserted from a plausible story.

**Orchestrator's own comments, logged at the owner's request:**
- **What I got wrong:** I let the first "STOP MOVED" read like the ratchet working before I parsed that the alert literally said *"protecting −75.06 pt"* (a loss-level base correction). I also asked to "send" the regime analysis that CC had **already** run and returned in front of me — a context-tracking slip. And I treated #116 as merged because the owner said "merged"; it hadn't landed. The first two I caught myself; the third was caught by CC's ground-truth check — which is exactly the discipline (§0.5.98) I should have applied to our own workflow without prompting.
- **What I corrected:** each of the above was corrected the same turn or the next, and the gate work was re-sequenced into the right order (#118 durability before #116 enable) rather than papering over the missed merge.
- **Lane discipline held:** I kept the sequence strict — the exit fix (#114) before touching the entry strategy; *armable* (#117) before *durable* (#118) before *enable* (#116), each verified from logs before the next; and I refused to let concurrency (PR-B) deploy on a net-negative strategy (§0.5.219). When the owner jumped to "enable #116" before the durability rung, I flagged the consequence (the gate would lapse ~5h after feed gaps) instead of letting it slide. The minimal-buffer-vs-dedicated-series fork was resolved by the perf probe (data), not by my opinion.
- **Honest paper/edge note (carried from v20/v22, still true and sharper now):** this is **paper**, and the edge question is **still open**. We have now removed the two mechanical leaks (dead-ratchet exit; below-trend entries) and proven both fixes — but TF was net-negative this window **and so was SeanBot** (~−$1.8k by reconstruction); it was a hard down-and-chop tape. We have **not** yet seen the strategy run clean in its intended regime, and the exit's profit-walk on a winner is **unproven** on the #114 build. "Downtrend bleed plugged" is **not** "strategy proven." Phase 3 (measure-don't-touch) is exactly the test, against the pre-committed bar in §7. Resist the urge to tune before the clean sample exists.

**Enforcement rules for next session:**
1. Confirm the mechanism from broker/DB/source/the-live-container before building *or claiming* anything — including "it's merged/deployed/protected."
2. For any gate: verify enabled AND armed AND blocking (§0.5.215). A flag is not protection.
3. One change at a time; do not tune the strategy or enable concurrency during the Phase 3 measurement window.
4. Always launch CC VPS inside tmux; `claude --resume` after any disconnect.

---

## 11. Logging verbosity — what to demand from any new code

Every entry/exit/stop/ratchet action logs `[COMPONENT] symbol: action — reason`; every lifecycle state transition logs old→new at INFO; every swallowed exception logs type + context; dedup/select-one logs which row won; the ratchet logs `trailing_stop_ratcheted: stop_id old new highest entry lifecycle_id`; arming logs `trailing_armed` / `trailing_armed_on_force_fill`; the regime gate logs `[REGIME-ARMABLE] thirty_min_buckets=… needed=… would_arm=… gate_enabled=… (<when>)` at boot + throttled + post-gap-reseed, and `[STRAT] regime gate BLOCKED entry: price=… <= 30m EMA200=…` on a block; the force-fill stop logs `[EXEC] symbol: stop contract exchange defaulted — CME — <path>` when it normalizes. Concurrency work (when unblocked) MUST log, per lifecycle, which position a fill/stop/cancel/ratchet belongs to.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill (master template). Entry/exit/strategy/risk/concurrency changes are **AUDIT** unless the owner explicitly waives the pause: green checks, then the owner's one-word merge. Carry the §⚠️ Known Gotchas list forward verbatim; it never shrinks.

---

## 13. Current PR brief in flight — HELD (hand to Claude Code only after the Phase 3 window, or if `ratchet_no_resting_stop` recurs)

~~~
# TradeFlow — Claude Code PR Prompt: PR (next) — Refresh the router's cached stop_order_id when the reconciler heal re-places a protective stop

## Role
You are a senior Python developer on TradeFlow, an autonomous MNQ futures bot on an IBKR paper account (~$1M NetLiq) that places real bracket orders. You write clean, tested, production-grade code, never modify files you weren't asked to, and study existing patterns before writing. Bugs here pin a trailing stop to a dead order id and round-trip a winner to base. You log verbosely: `[COMPONENT] symbol: action — reason`. You second-guess your own assumptions and verify against the actual file before claiming. The previous session found the related Error-321 bug by reading raw logs and source, not greps — keep that standard.

## Context
The bar-ratchet walks the protective stop by modifying the order id cached in the router's in-memory lifecycle copy (`OrderRouter._by_lifecycle_id`, read at router.py ~763). When the reconciler **heals** a missing/cancelled protective stop by **re-placing** it with a NEW order id, it updates the DB + state-machine copy but does NOT refresh the router's in-memory `stop_order_id`. The ratchet then keeps trying to modify a dead id → logs `ratchet_no_resting_stop` every bar → the stop never walks → round-trip-to-base. PR #114 (`d801792`) removed the COMMON trigger (the force-fill stop no longer dies on Error 321), so this is currently latent — but any genuine heal-replace (real disconnect cancel, OCA sibling cancel) still trips it. Smoking gun from this session (pre-#114): 1,376 `ratchet_no_resting_stop` on the PR-A build, ratchet pinned to a dead id=154 while the live healed stop was id=155.

## 🏗️ System Architecture & Recent Learnings
- Container: `tradeflow-app` (Python 3.11, async).
- DB: Supabase via a custom httpx wrapper (`SupabaseClient`) — NOT `supabase-py`. Env: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
- Logging: `docker logs tradeflow-app`; module-level LOGGER.

### Key Architecture Constraints
- Runtime: the ratchet reads the router's in-memory copy (`_by_lifecycle_id`), not the DB, when it modifies the stop.
- Heal site: `Reconciler` re-places a missing protective stop (the path that logged `re-placed STP id=155` this session); it must notify the router of the new id.
- Schema: `lifecycles.stop_order_id` is the durable copy; the router holds a separate in-memory copy.
- Scope boundary: fix ONLY the in-memory id refresh on heal-replace. Do NOT touch the ratchet math, the exit logic, #114's exchange normalization, or the regime gate.
- Design decision (recommend default): **(A)** give `OrderRouter` a small `update_stop_order_id(lifecycle_id, new_id)` method and call it from the reconciler's heal-replace site (explicit, testable) — RECOMMENDED; **(B)** have the ratchet re-resolve its stop id from the DB/broker when its cached id is not live (more defensive but a bigger change). Default to (A); note (B) as a follow-up.

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints
Files you WILL modify (EXACTLY 3):
- `src/execution/router.py` (add `update_stop_order_id` + log)
- `src/execution/reconciler.py` (call it at the heal-replace site)
- `tests/test_execution_router.py` (or the matching existing test file)

Files you MUST NOT modify: `src/strategy.py`, `src/orchestrator.py`, `src/execution/bracket.py`, `config/risk_params.py`, `docker-compose.yml`.

Verification gates:
- `git diff main -- src/strategy.py src/orchestrator.py src/execution/bracket.py docker-compose.yml` → MUST be empty
- `git diff main --stat` → EXACTLY the 3 files above

### 2. Code Quality
black + ruff clean; type hints preserved; no signature changes to public methods; one import per line; logging format `[ROUTER] symbol: stop_order_id refreshed — old=<id> new=<id> (heal-replace)`.

### 3. Safety
- All pre-existing tests pass (baseline **690 green** post-#118). Do NOT "fix" any pre-existing failure — document it.
- No unexpected broker calls or DB writes.
- If you find an adjacent bug, DOCUMENT it, do NOT fix it.

## 🧩 Current Mission: keep the ratchet tracking the live stop id after a heal-replace

### Objective
When the reconciler re-places a protective stop with a new order id, refresh the router's in-memory `stop_order_id` for that lifecycle so the next ratchet modify targets the live id, not the dead one.

### Task A: Audit
Read the reconciler heal-replace site (the path that re-places a missing STP and logs the new id) and `OrderRouter._by_lifecycle_id` / the ratchet's stop-id read (router.py ~763). Confirm in the PR description: the exact line where the new healed id is known, that the router copy is NOT currently updated there, and that the ratchet reads the router copy (not the DB). 3–5 line finding.

### Task B: Implement
Add `OrderRouter.update_stop_order_id(lifecycle_id, new_id)` (idempotent; no-op if unchanged; logs old→new). Call it from the reconciler immediately after a successful heal-replace, with the new order id.

### Task C: Add tests
(1) heal-replace updates the router's cached id → next ratchet targets the new id (assert by arg). (2) no-op when the id is unchanged. (3) heal-replace for an unknown lifecycle is a safe no-op (no raise). TEST SAFETY: fresh `MagicMock()` per test; mock the wrapper not the raw chain; explicit returns; no `side_effect` list without a count comment; match the neighbor's async pattern (`asyncio_mode=auto` → plain `async def`); assert by arg, not call index.

### Task D: Verify completeness
`grep -rn "stop_order_id\|re-placed STP\|register_recovered" src/execution/` → classify every site that mints or caches a stop id; confirm the heal-replace path is the only unrefreshed one (or cover the others).

### Task E: Out-of-scope investigation (~10 min, document only)
Whether design (B) — the ratchet self-re-resolving its stop id from broker/DB when the cached id isn't live — is worth a future defensive PR. Do NOT implement.

### Task F: Post-merge smoke test
After merge + deploy (safe over a position, §0.5.211): in the next genuine heal-replace, confirm a `[ROUTER] … stop_order_id refreshed … (heal-replace)` line and that the ratchet then walks the NEW id with zero `ratchet_no_resting_stop` for that lifecycle. STOP if `ratchet_no_resting_stop` persists after a heal-replace.

## 📤 Expected Output
Files modified (EXACTLY 3); diff stat; PR description with Task A finding, Task D grep classification, Task E paragraph, local + full test tails, protected-file empty diffs, explicit "This PR does NOT touch ratchet math / exit logic / #114 exchange normalization / the regime gate", and a "What I got wrong" section.

## 🔍 Pre-Push Checklist
Code quality (black/ruff/imports/signatures); TEST SAFETY GUARDRAILS (fresh mocks, wrapper-level mocking, explicit returns, async decorator matches neighbor, assertions by arg not index); production safety (empty protected diffs, Task D complete, smoke test included, "What I got wrong" present).

## ⚠️ Known Gotchas (carry forward, never shrink)
1. `SUPABASE_SERVICE_ROLE_KEY` (not `_KEY`).
2. DB wrapper is a custom httpx `SupabaseClient`, not `supabase-py`.
3. Docker restart ≠ rebuild — owner builds + `up -d --force-recreate` after merge (safe over an open position, §0.5.211).
4. `RISK` is a frozen dataclass — patch the module symbol.
5. `lifecycle_events.emitted_at` (not `created_at`); `lifecycles` keyed by `lifecycle_id`.
6. The ratchet reads the **router in-memory copy**, not the DB — refreshing the DB alone does NOT fix this (the entire point of the PR).
7. A protective stop placed without `exchange` dies on IBKR Error 321 (§#114) — do not regress that normalization.
8. AUDIT change — green checks then hold for the owner's one-word merge; protected-branch merge needs `--admin` after checks complete.
9. Pre-existing test baseline is 690 green (post-#118). Do not "fix" unrelated reds.
10. `asyncio_mode=auto` — plain `async def` tests, no `@pytest.mark.asyncio` decorator (verify a neighbor).
~~~

---

## 14. Canonical references (in order of authority)

1. **`CLAUDE.md`** on `main` at `237a750` — §0.5.x registry, autonomy contract, system rules.
2. **Source on `main`** at `237a750` — what actually runs (verify in-container per §6/V2).
3. **Production Supabase** (service role, read-only) — `lifecycles`, `lifecycle_events`, `signal_reconciliations`, `seanbot_signals`, `strategy_decisions`.
4. **IBKR paper (DUQ331660)** via `ib_async`, clientId 97 read-only — broker state truth.
5. **This handoff (v23)** — session context, not long-term authority.
6. **v22 and earlier** — historical; ignore any claim that contradicts 1–4.

---

## 15. First 15 minutes of the next session

1. Read §§0.5, 1, 2, 4, 5, 10. §0.5.215–219 and §5 are the most important.
2. `tmux new -A -s tf1`, then pre-flight: `git -C ~/tradeflow fetch && git -C ~/tradeflow pull --ff-only origin main && ls -t docs/handoffs/ | head -3`, then run the §6 block. Confirm deployed `237a750`+, gate enabled+armed+blocking (V1), and read live broker state (FLAT at handoff).
3. **Do NOT ship code.** We are in Phase 3 (measure-don't-touch). Read the clean expectancy accumulating (gate-on + exit-fixed trades only) against the §7 bar (positive expectancy + PF > ~1.2 over ~20–30 qualifying trades).
4. Confirm the two watch-items as they fire (V4): the exit profit-walk on a winner; the gap-reseed re-arm.
5. Only if `ratchet_no_resting_stop` recurs after a heal-replace (V4): hand the §13 brief to CC VPS. Otherwise leave the queue alone until the measurement window closes.
6. After any future merge, draft a VPS smoke-test runbook (`vps-smoke-test-runbook` skill; Appendix A is the current one).

---

## 16. How to publish this handoff

**Path A — owner self-serve (single block, run from your Mac; heredoc OK in your own shell).** The repo ruleset requires the "Lint, type-check, and test" check to **complete** before merge — `--admin` does NOT skip an in-progress check — so this **polls** `gh pr checks` to completion, then `--admin` merges. Uses `set -uo pipefail` (NOT `-e`, because the poll returns non-zero while pending). Never `--watch` (§0.5.214):

```bash
scp ~/Downloads/HANDOFF_v23.md tradeflow:/tmp/HANDOFF_v23.md
ssh tradeflow 'bash -s' <<'PUBLISH_EOF'
set -uo pipefail
cd /home/tradeflow/tradeflow
BRANCH=docs/handoff-v23
git fetch origin
git checkout -B "$BRANCH" origin/main
mkdir -p docs/handoffs
cp /tmp/HANDOFF_v23.md docs/handoffs/HANDOFF_v23.md
git add docs/handoffs/HANDOFF_v23.md
git commit -m "docs: add v23 handoff (regime gate live+armed+durable; force-fill Error 321 exit fix; concurrency held; Phase 3 measure)"
git push -u origin "$BRANCH" --force-with-lease
gh pr create --base main --head "$BRANCH" --title "docs: add v23 handoff" --body "Session 23 handoff. Docs-only. Regime gate live+armed+durable (#116/#117/#118); force-fill Error 321 exit fix (#114); concurrency held (#115); Phase 3 clean-measurement begins." || true
MERGED=0
for i in $(seq 1 40); do
  OUT="$(gh pr checks "$BRANCH" 2>&1)"; RC=$?
  printf '%s\n' "$OUT"
  if printf '%s' "$OUT" | grep -qiE '\b(fail|failure|error|cancelled|timed_out)\b'; then
    echo "CHECK FAILED — aborting, not merging."; exit 1
  fi
  if [ "$RC" -eq 0 ]; then echo "CHECKS GREEN"; MERGED=1; break; fi
  echo "checks pending (rc=$RC) — waiting 15s [$i/40]"
  sleep 15
done
if [ "$MERGED" -ne 1 ]; then echo "TIMEOUT waiting for checks — not merging."; exit 1; fi
gh pr merge "$BRANCH" --squash --admin --delete-branch
git checkout main
git pull --ff-only origin main
echo "DONE: v23 merged"
git log --oneline -1 origin/main
PUBLISH_EOF
```

**Path B — CC VPS brief (if you'd rather delegate):** save the content to `/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v23.md`, branch off `origin/main`, `git commit -F`, push, `gh pr create --body-file`, poll `gh pr checks <branch>` in a loop (NOT `--watch`) until green, then `gh pr merge <branch> --squash --admin --delete-branch`, resync main.

The handoff exists only once committed to `origin/main`.

---

## Appendix A — VPS smoke-test runbook (current shipped state: PR #114–#118, gate-live)

*Per the standing rule (always draft a runbook after a merge). All PRs this session already deployed + verified inline; this runbook is the standing post-merge / regression check for the gate-live state. Paste into VPS Claude Code inside tmux. Verification only — no `git push`, no secret edits, no container-state writes.*

### §1 — Pre-flight
```bash
cd /home/tradeflow/tradeflow
git rev-parse HEAD
git log -1 --oneline
docker compose ps
# Expect: origin/main HEAD = 237a750 (or a later docs commit); tradeflow-app "Up", not Restarting/Exited.
# If mismatch: STOP, git pull origin main, re-verify.
```

### §2 — Deployed-code check
```bash
docker exec tradeflow-app grep -c "exchange defaulted" /app/src/execution/router.py
docker exec tradeflow-app grep -c "post-gap-reseed" /app/src/orchestrator.py
docker exec tradeflow-app python -c "from src.strategy import _BAR_BUFFER_MAX; print(_BAR_BUFFER_MAX)"
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' tradeflow-app | grep -iE "REGIME_GATE_ENABLED|MAX_CONCURRENT|TRADEFLOW_COMMIT"
# Expect: router>=1, orchestrator>=1, buffer 7000; REGIME_GATE_ENABLED=true, MAX_CONCURRENT=1, TRADEFLOW_COMMIT=237a750...
# If buffer=150 or any 0/absent: STOP — stale image, rebuild + force-recreate.
```

### §3 — State probes
```bash
docker logs tradeflow-app --since 15m > /tmp/tf_recent.log 2>&1
wc -l /tmp/tf_recent.log
grep -ciE 'error|exception|traceback' /tmp/tf_recent.log
grep -iE 'error|exception|traceback' /tmp/tf_recent.log | grep -viE 'kill_switch|consecutive' | head -10
# Expect: a healthy 15m of 1-min evals/recon ticks; 0 unexpected errors. STOP if Error 321 or ratchet_no_resting_stop appear.
```

### §4 — Source-of-truth check (the most important section)
```bash
/home/tradeflow/tradeflow/.venv/bin/python /tmp/tf_broker_truth.py
# Expect: MNQM6 pos=0 (flat) OR in-position with a standalone GTC STP resting (not naked).
# Contract count MUST == sum of ACTIVE lifecycle entry_qty (no extra bracket — TOCTOU canary, §0.5.212).
# STOP if pos > expected (double-entry) or a position is naked (no resting stop).
docker exec tradeflow-app python -c "from config.risk_params import RISK; print('regime', RISK.regime_gate_enabled, 'max_conc', RISK.max_concurrent, 'agg_cap', RISK.aggregate_contract_cap)"
# Expect: regime True, max_conc 1. STOP if max_conc != 1 (concurrency accidentally enabled, §0.5.219).
```

### §5 — PR-specific behavior log tail
```bash
docker logs tradeflow-app 2>&1 | grep -iE "REGIME-ARMABLE" | tail -2
docker logs tradeflow-app 2>&1 | grep -ciE "decision=noop_regime|regime gate BLOCKED"
docker logs tradeflow-app 2>&1 | grep -ciE "ratchet_no_resting_stop|Error 321"
# Expect: [REGIME-ARMABLE] would_arm=True gate_enabled=True (buckets>=202); noop_regime count > 0 while price is below the 30m EMA200; ZERO ratchet_no_resting_stop / Error 321.
# If would_arm=False: gate disarmed (short buffer) — INVESTIGATE (§0.5.216/218).
# If ratchet_no_resting_stop > 0: STOP — the §13 heal-replace gap (or an Error 321 regression) bit; report.
```

### §6 — Verdict
- **PASS** — pre-flight clean, deployed code matches #114–#118, gate enabled+armed+blocking, broker flat/not-naked with no extra bracket, zero Error 321 / ratchet_no_resting_stop.
- **FAIL** — naked position, extra bracket (double-entry), max_concurrent != 1, Error 321 or ratchet_no_resting_stop present, or stale image.
- **INVESTIGATE** — `would_arm=False`, or unexpected error rate without a clear breakage.

### §7 — Structured report
```markdown
# Smoke Test Report — Gate-live state (PR #114–#118)
**Verdict:** PASS / FAIL / INVESTIGATE
## §1 Pre-flight — HEAD <hash> (expect 237a750+), container <Up/Restarting>
## §2 Deployed-code — router<n> orchestrator<n> buffer<n> env<flags>
## §3 State probes — log <N> lines, errors <N>
## §4 Source-of-truth — broker pos <N>, resting stops <N>, contract==entry_qty? <Y/N>; regime <T/F> max_conc <N>
## §5 Behavior — would_arm <T/F>, noop_regime <N>, ratchet_no_resting_stop/Error321 <N>
## §6 Anomalies / next steps — <free text + recommended action>
```

---

*End of handoff v23. Target lifespan: until Phase 3 produces a clean expectancy read against the §7 bar and the go/no-go strategy decision is made. Then rely on `CLAUDE.md` + v24. The two mechanical leaks are closed and proven; the open question is the edge itself — and that is measured, not patched.*
