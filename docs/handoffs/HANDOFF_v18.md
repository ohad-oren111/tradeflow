# TradeFlow — Handoff v18 (first trailing-ratchet win PROVEN live; touch-parity premise disproven — SeanBot's live entry rule is unknown; hybrid-bracket + scoreboard bugs queued)

*Handoff from end of Session 18 (2026-06-02 ~19:30 UTC / 13:30 CR). The bot is RUNNING green on commit `63cfc75`, warm, not halted, `EXIT_MODE=trailing`, `bullish=2.0` live. This session cleared the v17 evaluator_error halt, shipped the real SeanBot-style ratcheted-STP exit, aligned entry-gate defaults, restored bullish=2.0, overhauled the Telegram alerts, and — most importantly — **proved the trailing exit works live** (a +$195.28 +50-lock win). It also disproved the central premise we'd been chasing: TF's "touch gate" is NOT broken; SeanBot's live entries don't sit on the 100-MA at all, so we cannot reconcile TF to SeanBot without the friend's actual current entry rule. Two real bugs are now queued (hybrid fixed-bracket-under-trailing; untrustworthy SeanBot scoreboard). This doc captures everything a new chat needs to pick up cleanly.*

> **VERSION NOTE:** Numbered **v18** (Session 18). The §16 publish step instructs VPS CC to confirm the latest handoff in `docs/handoffs/` and use `latest+1` if v18 is wrong — self-correcting, no operator action.

---

## 0. How to use this doc

Read §§1–6 first (state-of-system). §§7–13 are reference. §14 is the authority order. Canonical code truth: `main` at `63cfc75` or later.

**Do not trust this doc alone. Run §6 before any code. CRITICAL FIRST ACTIONS: (1) re-verify position from the broker — `bullish=2.0` went live mid-session and may have opened a new trade since the last probe; (2) the real strategy decision this session surfaced is a FORK, not a bug — read §5 and §7.1 before spending any effort on "SeanBot parity."**

---

## 0.5 Standing rules (permanent — do not remove)

**Copy-paste instruction style.** Every recommended action is a self-contained, paste-ready bash block, env sourced in-block, expected output described below it, decision tree if branches matter. No "you might want to…".

**Learning-delivery discipline.** Every new fact (bug pattern, corrected assumption, env fact, finding) is surfaced immediately as a paste-ready snippet for the handoff queue, not end-of-session.

**Read before diagnosing.** For complex state bugs, read the full startup log + 3–5 full cycle narratives before proposing root cause. `grep | wc -l` diagnosis is the #1 cause of wrong calls.

**Verify severity against the source of truth.** Before escalating urgency, hit the live broker/DB, not aggregated metrics.

**Always draft a VPS smoke-test runbook after a PR merge** unless told otherwise. The owner does not run smoke tests by hand.

### TradeFlow project standing rules (carried forward — verbatim, append-only)
- **§0.5.97 — probe external specs before baking into briefs.** Broker contracts, fees, schema, library API surfaces, and a benchmark's own config are verified against source before a brief. **Extended this session (§0.5.195): probe the BENCHMARK's actual behaviour against true OHLC before assuming TF is the one that's wrong.**
- **§0.5.98 — broker/DB state is ground truth, not internal assumptions.** Position/fills/P&L/loss-counts source from broker API / `lifecycles`, never an in-memory counter. *Held up again: CC caught the open position turning over mid-run from the broker probe, not from internal state.*
- **§0.5.151 — Docker healthy ≠ broker API healthy.** A green container can mask a dead IB Gateway session.
- **§0.5.184 — AUDIT gate.** Order-execution / strategy / kill-switch / secrets / broker-state changes PAUSE for the operator's single-word `merge` after the diff is posted. *(Still SUSPENDED for the paper-account fast-forward by standing operator instruction — VPS CC self-merges + deploys unattended on paper. REINSTATE before any live-money operation.)*
- **§0.5.186 / .187 — VPS CC command discipline.** No sub-agents/sweeps; no heredocs, `$(...)`, `${VAR}`, `;`, `cd X &&`. Python via `Write` to `/tmp/scriptN.py` then `python3 …`; commit `-F /tmp/commitmsg.txt`; PR body `--body-file`. Branch off `origin/main` FIRST. Bare `git push` is gated — use `git -C ~/tradeflow push origin <branch>`.
- **§0.5.188 — never ship NEW trading-path bugs into unattended windows.** *(Suspended for paper; reinstate for live.)*
- **§0.5.189 — `--dangerously-skip-permissions` opted OUT. Never suggest it.**
- **§0.5.190 — IBKR rejects a `TRAIL` order as a bracket CHILD of a `MKT` parent (Error 328). A native trailing stop must be placed STANDALONE post-fill, or done in-app bar-by-bar.** *RESOLVED in practice this session: the exit is now a bot-ratcheted native STP walked on each bar close (#92), NOT a native TRAIL. The ratchet ladder is the production trailing mechanism.*
- **§0.5.191 — seed the strategy buffer BEFORE starting the live bar subscription.** Confirmed again at boot (warm, 150 bars, indicators_ready).
- **§0.5.192 — `IB.positions()` contracts omit `exchange` → Error 321. Set `contract.exchange="CME"` before placing on a position-derived contract.**
- **§0.5.193 — warmup historical fetch uses `"5 D"` (not `"1 D"`).** `validate_seed >= 100` is the backstop.
- **§0.5.194 — boot recovery must synthesise full CLOSED fields for an orphan ENTERING lifecycle, or the CLOSED invariant crash-loops.** `_synth_closed_fields`.
- **§0.5.195 (NEW) — the provided SeanBot source zip is STALE (~3 weeks) and does NOT reproduce SeanBot's LIVE entries.** SeanBot's announced entry "price" ≈ its MA level, NOT a market-touchable fill: true 1-min OHLC at the 18:41Z entry was 30597–30621 while the shared SMA100 ≈ 30645 (price was 30–40pt away from the MA). SeanBot is **not** trading a 100-MA touch. TF correctly implements the documented SMA100-bounce; the ~10% agreement = the two bots run **different entry logic**, not a TF bug. **Do NOT tune/loosen TF's entry to "match" SeanBot without the friend's CURRENT entry rule.** (See §5.)
- **§0.5.196 (NEW) — TF entries place a FIXED `{STP, LMT}` bracket even under `EXIT_MODE=trailing`** (recurring anomaly). The bar-ratchet walks the STP anyway (trailing works), but the `+150` LMT TARGET ALSO rests → a hybrid that caps upside at +150. "STP-only on new entries" (per the startup log) is NOT actually happening. Root-cause the entry-placement path; make the reconciler exit-mode-aware (it re-arms a missing TARGET leg and is NOT exit-mode-aware — WO5 finding). (See §4/§7.2.)
- **§0.5.197 (NEW) — the SeanBot scoreboard P&L is UNTRUSTWORTHY.** The listener over-captures exits (Chicago-Jun-1 sums to −3364 vs the true −866.72, ~3.9×; near-dup same-price/same-second exits, e.g. a 13:22:53 cluster) and captures NO authoritative daily-summary message. The dashboard "TF leads by $618.96" headline is **bogus**. Until fixed, the **operator's manual sum of the clean Telegram feed is truth** (verified this session: Jun 1 = −866.72, Jun 2 = −251.10, May 28 = −616.20, May 27 = +1380.94, May 26 = +2108.18). (See §5/§7.3.)
- **§0.5.198 (NEW) — the bar adapter reads `bars_obj[-1]` on `has_new_bar`** — in `ib_async` that's the just-OPENED/forming bar, not the just-closed `bars[-2]`. TF evaluates a FORMING bar (TF's recorded close 30612.75 ≠ the true closed 30607.5). Real correctness bug; the fix is an entry-timing change needing `ib_async` bar-semantics verification before shipping. (See §4/§7.4.)
- **Batch-autonomy default.** Batch follow-ups into ONE VPS CC run; pause ONLY for AUDIT / strategy-param calls / external blockers (per the gate's current state). *Operator was explicit this session: run autonomously, no back-and-forth — CC correctly STOPPED on the two genuinely unbounded parts (touch, scoreboard) and shipped the bounded one (Telegram).*
- **Maximize autonomy / minimize operator UI.** Prefer one ssh command over many operator actions. Handoff publish via VPS CC + `gh`, no operator browser.

---

## 1. Where we are (as of handoff, 2026-06-02 ~19:30 UTC / 13:30 CR)

### Live production state
- `tradeflow-app`: deployed commit **`63cfc75`** (= `main` HEAD). Container **healthy**, **warm** (150 bars, seed-before-subscribe, `indicators_ready=True`), `is_halted=False`, **0** `halt_raised`/`exit_mode_mismatch` since boot. Startup log: `EXIT_MODE=trailing — stop_loss=75 lock_in=50 trail_offset=150 hard_ceiling=1000 take_profit=150 (bracket=STP-only + bar-ratchet on new entries)`.
- Config LIVE: `exit_mode=trailing`, **`bullish=2.0`** (went live this session via #96), `regime=False`. `TRADEFLOW_COMMIT=63cfc75`.
- **Position: last known FLAT** after `106471a5` closed +$195.28 at 13:26 CR. **RE-VERIFY from broker (V1)** — `bullish=2.0` only went live ~13:24 CR and may have opened a new trade since.
- Realized P&L (re-query — do NOT trust this number, §9): TF cumulative ≈ **−$1,877** over ~13 closed trades (the v17 −$1,773 baseline of 11 trades + `109c32f1` stop −$299.72 + `106471a5` win +$195.28). **Dominated by pre-fix bug-era losses** (the −$925 pair on 06-01, the −$437 restated naked-stop). The current hardened+trailing system has barely traded clean yet.
- **HALT STATE: NOT halted.** The v17 `evaluator_error` halt was cleared by #90 (transient-fault tolerance).
- Dashboard behind HTTP Basic auth: `/`, `/trades`, `/pnl`, `/scoreboard` (**SeanBot column is bogus — §0.5.197**), `/divergence`.

### What just shipped this session (8 PRs, #90–#97)
- **#90** `a03e9ee` — kill-switch tolerates N=3 consecutive **TRANSIENT** eval faults (httpx Transport/Timeout/Connection) before fail-safe halting; logic errors still halt on first hit. **Cleared the v17 false halt** (a single Supabase ReadTimeout had halted a healthy bot). Knob `KILL_SWITCH_MAX_CONSEC_EVAL_ERRORS=3`. *Discovery: the bot was NOT flat at clear-time — held a live bracketed 2-ct long; native OCA bracket proven to survive restart-with-position.*
- **#91** `e79f84b` — first trailing attempt: standalone post-fill native IB `TRAIL` (parentId=0, avoids 328). **Superseded by #92.**
- **#92** `1bc90ee` — **the real SeanBot exit**: a bot-ratcheted native **STP** walked on each 1-min bar close. New `src/execution/trail_manager.py` (pure `compute_ratcheted_stop` + `should_hard_exit`), `IBClient.find_open_order_by_id` (modify-in-place via re-`place_order` same orderId), orchestrator bar hook → `router.ratchet_stop_on_bar`. Knobs: `lock_in_pts=50`, `hard_ceiling_pts=1000` (reuse `stop_loss_pts=75`, `trail_offset_pts=150`).
- **#93** `69da798` — entry-gate parity via env defaults (`regime_gate_enabled` True→False; `ma_bullish_tolerance_pts` 2.0→0.0) — no `strategy.py` code change.
- **#94** `7f241bf` — high-water mark persisted to `lifecycles.metadata.highest_price` JSONB (no migration), seeded on recovery clamped to entry.
- **#95** `777bb72` — exit-mode determinism: startup `EXIT_MODE` log, per-entry `entry exit_mode=/bracket=` log, `exit_mode_mismatch` WARNING guard. (These will surface the §0.5.196 anomaly on the next entry.)
- **#96** `8b14f61` — `BULLISH_TOLERANCE=2.0` in the compose `environment:` block (live-parity value; `risk_params` default stays 0.0). **Deployed this session → bullish=2.0 LIVE.**
- **#97** `63cfc75` (= HEAD) — **SeanBot-style Telegram alerts** (🟢 ENTRY / 🔒 STOP MOVED / 💰 EXIT(profit) / 🔴 EXIT(loss)) + **daily P&L summary** (📊). New `comms/alert_format.py` (pure, 12 tests); `telegram._format_alert` parses+dispatches; router adds `entry=<px>` to entry/exit alerts; `_maybe_emit_daily_summary` fires once per UTC day-rollover on the hourly-digest tick (no daily scheduler existed). Full suite 604 green.

### What we discovered / proved live this session (evidence)
- **★ FIRST PROVEN TRAILING-RATCHET WIN.** `106471a5`: entry **30626.56** (12:49 CR) → ratchet moved the STP to the **+50 lock** (30676.50, "now protecting +49.94 pt") → exited 30676.00 (13:26 CR) for **+$195.28** net. Math checks: 49.44 pt × $2 × 2 ct − $2.48 RT commission = $195.28. **The trailing exit ladder works in production** (resolves the v17 "trailing-TP unproven" open item — via the ratcheted STP, not a native TRAIL). Bonus: the trade entered under the old container and exited under the new one — **the redeploy recovered and kept ratcheting the live position** (broker-resident STP).
- **★ The touch-parity premise is FALSE (§0.5.195).** True OHLC at the 18:41Z SeanBot entry: all bars 30597–30621; shared SMA100 ≈ 30645; SeanBot "entered @30645.5" ≈ the MA level, a price the market never reached. SeanBot is not trading a 100-MA touch. TF's touch gate is correct. The two bots run different entry logic.
- **★ SeanBot scoreboard P&L can't be reconstructed from captured data (§0.5.197).** Summing announced exit `$` over-reports ~3.9× on Jun-1; listener started ~May 28 (no May 26/27 rows); no daily-summary captured.
- **Hybrid-bracket anomaly recurred (§0.5.196):** the new position got a fixed `{STP id93, LMT id94}` bracket despite `EXIT_MODE=trailing`. Not root-caused this run; the #95/#97 per-entry log + mismatch guard will catch it on the next entry.
- **Telegram overhaul is LIVE and correct:** the 13:25 🔒 STOP MOVED and 13:26 💰 EXIT(profit) rendered in clean SeanBot style with points/reason/net P&L.

### What the operator (Ohad) is doing
Hands-off PM / orchestrator. Single-word approvals. This session he drove an autonomous touch+scoreboard+Telegram run (explicitly: "be autonomous, stop the back-and-forth"), pasted CC outputs + the full SeanBot Telegram feed + two dashboard screenshots, and cross-checked SeanBot P&L against Gemini (which flagged the dashboard mismatch — correctly). He is now closing this session to open a fresh chat. **Next operator action:** publish this handoff via §16, paste the VPS CC output back here so we can confirm it landed, then open the new chat on the §15 plan. (Separately tracking the crypto rebalance + Botty AI — both PARKED, not part of this thread.)

---

## 2. The session's work thread

1. **Cleared the v17 halt (#90).** A single Supabase ReadTimeout had fail-safe-halted a healthy bot; added N=3 transient-fault tolerance. Found the bot was holding a live bracketed long — native OCA bracket survives restart-with-position.
2. **Built the SeanBot exit, twice.** #91 tried a standalone native `TRAIL`; replaced by #92's bot-ratcheted STP walked on bar close (the actual SeanBot mechanism). Knobs: +50 lock, +150 trail, +1000 hard ceiling.
3. **Aligned entry-gate defaults (#93–#95)** + persisted HWM + added exit-mode determinism logging. Suite green throughout.
4. **Baseline measured (WO5):** TF agrees with SeanBot on only ~10% of entries. Dominant miss = touch (57%), NOT bullish (10%); warmup churn 24% (self-inflicted by ~7 redeploys). **First reframe:** the bullish fix is minor; the touch gate is the apparent gap.
5. **Attempted the unshackle (WO5) — correctly BLOCKED.** Reconciler `_reconcile_active`/`_heal_missing_legs` re-arms a missing TARGET leg and is NOT exit-mode-aware, so cancelling the open trade's +150 LMT would just get re-armed. CC stopped, touched nothing. (The position later closed on its own — moot.)
6. **WO6 autonomous run:** touch diagnostic + scoreboard fix + Telegram overhaul.
   - **Touch (Part 1):** NOT a timing/lag issue as the brief framed it. CC fetched true OHLC and found SeanBot's entry price ≈ the MA, not a touchable price → **premise disproven** (§0.5.195). Per the stop-condition, CC did NOT ship a blind strategy change. **Second reframe (the big one):** there's no "touch bug" to fix; SeanBot's live entry rule is unknown.
   - **Scoreboard (Part 2):** summing announced exit `$` failed the validation anchors (over-reports ~3.9×; listener started ~May 28; no daily-summary captured). CC did NOT ship a still-wrong reconstruction (§0.5.197).
   - **Telegram (Part 3):** shipped clean (#97).
   - One deploy → `bullish=2.0` + Telegram live.
7. **Proved the trail live:** `106471a5` booked the first +50-lock win (+$195.28) right after deploy. Trailing exit confirmed end-to-end across a redeploy.
8. **Hybrid-bracket anomaly logged (§0.5.196)** for next-session root-cause.

**Closed rabbit holes:** (a) the touch gate is NOT too strict / NOT lagging — don't re-open it without SeanBot's real rule; (b) summing SeanBot exit-`$` does NOT reconstruct its daily P&L — don't retry that approach, capture the daily-summary message instead; (c) the native `TRAIL`-as-child path is dead (Error 328) — the ratcheted STP is the mechanism.

---

## 3. What the system is actually made of

**Single source of truth:** none single-file; this handoff + `main @ 63cfc75` are the best available system doc. `CLAUDE.md` (committed #94-era) is the in-repo orientation.

Highlights:
- **Stack:** `ib_async` (NOT `ib_insync`), Supabase, Docker Compose, Hetzner CX32 (Ashburn), IBKR paper **DUQ331660**. VPS: `tradeflow@5.78.212.37`, repo `~/tradeflow`, GitHub `ohad-oren111/tradeflow`.
- **Production-live code paths:** `src/orchestrator.py` (bar loop, warmup, hourly digest, daily-summary rollover), `src/strategy.py` (SMA50/100-bounce entry gates: ma_order, touch, bullish, gap, cooldown), `src/execution/router.py` (entry/exit placement + alerts), `src/execution/trail_manager.py` (ratchet compute), `src/execution/reconciler.py` (`_reconcile_active`/`_heal_missing_legs` — leg self-heal), `src/clients/ib_client.py` (bar adapter, `find_open_order_by_id`), `comms/telegram.py` + `comms/alert_format.py` (alerts).
- **Tables:** `lifecycles` (state/P&L truth, `metadata.highest_price` JSONB for HWM), `strategy_decisions` (TF's per-bar decisions: ts/close/sma100/gates — **does NOT persist bar LOW**), `signal_reconciliations` (`classification` column, `tf_decision` matched bar), `seanbot_signals` (captured Telegram: types `entry`/`exit`/`stop_moved` only — **no daily-summary captured**), `signal_reconciliations`.
- **Dead/misleading surfaces:** `config/settings.py` `TRAIL_OFFSET=250` is DEAD config (real offset is 150, three-way confirmed). Native `TRAIL`-as-child path (Error 328). The scoreboard's SeanBot P&L column (bogus — §0.5.197).
- **Automation:** hourly digest + daily-summary-on-UTC-rollover both live in the orchestrator loop (no separate scheduler).

---

## 4. Verified facts ( 2026-06-02 )

**DO NOT challenge these unless the schema migrates.**

- Broker state is ground truth, not the internal DB (§0.5.98).
- Native broker-resident OCA STP survives restart/disconnect/redeploy (proven repeatedly).
- Verified MNQ spec (do not re-derive): TICK 0.25, $2/pt, $0.62/side ($1.24 RT... — **commission RT actually $2.48 on 2 ct**, i.e. $0.62/side × 2 sides × 2 ct), MARGIN $2000/day-trade, quarterly Mar/Jun/Sep/Dec, roll ~8d before 3rd-Fri expiry. Front contract: **MNQM6**.
- Ratchet ladder (production exit): base STP entry−75; at peak ≥+50 → STP to entry+50 (lock); at peak ≥+150 → STP = max(entry, highest−150) trail; hard market-exit at entry+1000. Walked on each 1-min bar close, modify-in-place same orderId.

**New load-bearing facts (this session):**
- **§0.5.195 — SeanBot's announced entry price ≈ its MA level, NOT a market fill.** True 18:41Z OHLC 30597–30621 vs SMA100 ≈ 30645. SeanBot's live entry rule ≠ the documented SMA100-touch and ≠ the stale source zip. Evidence: `/tmp/barcheck.py` true-OHLC probe.
- **§0.5.196 — entries place a FIXED `{STP, LMT}` bracket under `EXIT_MODE=trailing`.** Evidence: post-deploy probe showed `106471a5` with STP id93 @ 30551.25 + LMT id94, despite trailing mode + the "STP-only on new entries" startup log.
- **§0.5.197 — captured SeanBot exits over-report ~3.9×** (Chicago-Jun-1 = −3364 vs true −866.72) and the listener started ~May 28; no daily-summary messages captured (`types: {entry, exit, stop_moved}`). Evidence: `/tmp/sb_pnl_diag.py`, `/tmp/sb_dup.py`, `/tmp/sb_summary.py`.
- **§0.5.198 — bar adapter reads `bars_obj[-1]` (forming bar) on `has_new_bar`**, not closed `bars[-2]`. Evidence: TF recorded close 30612.75 ≠ true closed 30607.5 at 18:41.
- `strategy_decisions` persists close + sma100 but **NOT bar LOW** (the touch gate's actual input) — `close` is only a proxy in diagnostics.
- Orchestrator subscribes `use_rth=False` (ETH / 24-5 CME) — TF and SeanBot share the same bar source (SMAs agree <1pt).

---

## 5. Wrong diagnoses — READ BEFORE YOU DEBUG

1. **"The touch gate is too strict / TF's SMA is misaligned."** Evidence that misled: 57% of SeanBot entries classified MISS-filter:touch; the 18:41 miss showed `close=30612.75 sma100=30645.135` while SeanBot entered @30645.5. **Why wrong:** TF and SeanBot AGREE on the SMA (~30645); the disagreement was the current price. We (chat tier) read that as a 1-bar timing offset and pre-authorized a "fix the lag" decision tree. **Correct diagnosis (CC, from true OHLC):** the market was 30–40pt BELOW the MA at that ts; SeanBot's "entry @30645.5" is the MA level, not a fill the market reached. SeanBot is NOT trading a 100-MA touch. There is no TF touch bug to fix — the bots run different entry logic, and SeanBot's live rule is unknown (the source zip is ~3 weeks stale). (§0.5.195)
2. **"Reconstruct SeanBot daily P&L by summing announced exit `$`."** Evidence that misled: the operator's clean Telegram feed summed to the anchors exactly (Jun-1 = −866.72, Jun-2 = −251.10). **Why wrong:** the captured DB has MORE exit rows than the clean feed (near-dup same-price/same-second exits; ~3.9× inflation on Jun-1), so summing the CAPTURED exits ≠ the feed. **Correct:** capture SeanBot's own daily-summary message, or dedup the captured exits. (§0.5.197)
3. **Early over-weighting of the bullish-tolerance fix.** It recovers ≤2/21 entries; touch dominated (then turned out unfixable). Minor lever.

**Lesson for next session (logged at the operator's request):** the meta-pattern was **assuming TF is the broken side and the benchmark is correct.** Both wrong diagnoses dissolved the moment we probed the BENCHMARK against the source of truth (true OHLC; the clean feed vs the captured DB) instead of tuning TF toward it. **Extend §0.5.97 to the reference bot itself: before reconciling TF to SeanBot, verify what SeanBot actually did against raw market data.** And the chat tier should not pre-author a fix decision tree off a framing it hasn't checked against raw data — the "timing offset" tree was built on a premise the first true-OHLC probe killed.

---

## 6. Verification block — run this before doing anything

**V0 — deployed code + config truth**
```bash
docker exec tradeflow-app python -c "from config.risk_params import RISK; print('exit_mode=',RISK.exit_mode,'bullish=',RISK.ma_bullish_tolerance_pts,'regime=',RISK.regime_gate_enabled)"
docker inspect tradeflow-app --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -E "TRADEFLOW_COMMIT|BULLISH_TOLERANCE|EXIT_MODE"
```
Expect: `exit_mode= trailing bullish= 2.0 regime= False`; `TRADEFLOW_COMMIT=63cfc75…`, `BULLISH_TOLERANCE=2.0`, `EXIT_MODE=trailing`. If commit ≠ 63cfc75-or-later → the deploy didn't land; rebuild from HEAD.

**V1 — broker truth: position + bracket + not-naked** (the critical first check — bullish=2.0 may have opened a trade)
Re-create the clientId-97 read-only probe (`/tmp/probe.py` from this session — connect IB clientId 97 @ 127.0.0.1:4002; print MNQM6 position qty/avgCost + all resting orders id/action/type/price/status; SELECT the ACTIVE `lifecycles` row's `stop_order_id`/`target_order_id`). Run it.
- FLAT, no resting orders → fine.
- LONG with a SELL STP resting → protected; **note whether a SELL LMT also rests → that's the §0.5.196 hybrid anomaly.**
- LONG with NO stop → NAKED → re-arm immediately (entry−75), never leave naked.

**V2 — container health / halt / warmup**
```bash
docker ps --filter name=tradeflow-app --format "table {{.Names}}\t{{.Status}}"
docker logs tradeflow-app 2>&1 | grep -E "startup: EXIT_MODE|indicators_ready|recovery_complete" | tail -5
docker logs tradeflow-app 2>&1 | grep -cE "halt_raised|exit_mode_mismatch"
```
Expect: healthy; startup EXIT_MODE log + warm (150 bars); count **0**. Nonzero halt/mismatch → read the lines.

**V3 — latest hourly digest (touch counts + SeanBot agreement, NOW under 63cfc75)**
```bash
docker logs tradeflow-app 2>&1 | grep "hourly_session_digest" | tail -1
```
Baseline (last pre-deploy digest, commit 777bb72): `evals=60 long_signal=9 noop_filter=51 (touch=36 bullish=15) SeanBot: 2 entries → 0 AGREE`. This is expected to stay touch-dominated — that is NOT a bug to fix (§0.5.195).

**V4 — did the last entry trip the hybrid-bracket anomaly?**
```bash
docker logs tradeflow-app 2>&1 | grep -E "\[ALERT\] entry_placed|entry exit_mode=|exit_mode_mismatch" | tail -10
```
If an entry shows a TARGET/LMT leg under `exit_mode=trailing` → §0.5.196 confirmed on a fresh entry — this is the §7.2 work item's reproduction.

---

## 7. Pending work queue

*Priority order depends on V1 state and on the §7.1 fork, not on numbering.*

### 7.1 — STRATEGIC FORK (decide FIRST — this gates everything else)
SeanBot's live entry rule is unknown and unmatchable from current data (§0.5.195). Two paths; the operator should pick:
- **(A) Get SeanBot's actual CURRENT entry logic** from the friend (the SeanBot operator). Without it, "parity" work is guesswork. If obtained → re-derive TF's entry gate to match and re-measure `/divergence`.
- **(B) Stop cloning SeanBot; validate TF's OWN edge.** TF faithfully runs the documented SMA100-bounce + the now-proven trailing ratchet. Let it accumulate trades and judge it on its OWN win-rate / profit-factor / P&L, using SeanBot only as a loose market-context peer (not a trade-for-trade target). The +$195 win shows the exit works; the question is whether the ENTRY has an edge on its own.

**Recommendation to put to the operator:** lean (B) for now (it costs nothing and produces real data), and pursue (A) in parallel via the friend. Either way, fix §7.2 first — pure trailing is correct under both paths.

### 7.2 — Fix the hybrid fixed-bracket-under-trailing anomaly (§0.5.196) — AUDIT, highest-value bug
Entries should be **STP-only + ratchet** in trailing mode; the +150 LMT should not rest (it caps upside). Diagnose via V4 / the #95 per-entry log on the next entry, then: (a) fix the entry-placement path to honor `exit_mode=trailing` (no TARGET leg), and (b) make `reconciler._heal_missing_legs` exit-mode-aware (do NOT re-arm a TARGET when `exit_mode=="trailing"` — the WO5 blocker). This also unblocks any future "let a winner run past +150."

### 7.3 — Fix the SeanBot scoreboard (§0.5.197)
Either capture SeanBot's own "Daily Summary" Telegram message in the listener (add a parser shape; authoritative) **or** dedup the over-captured exits before summing. Validate against the anchors (Jun-1 = −866.72, Jun-2 = −251.10, May-28 = −616.20, May-27 = +1380.94, May-26 = +2108.18). TF's own column stays `lifecycles.pnl_net`. Fix the "TF leads" headline. **Until shipped, the scoreboard's SeanBot numbers and the "TF leads" verdict are bogus — use the manual feed sum.**

### 7.4 — Bar-adapter closed-bar fix (§0.5.198)
Evaluate the just-closed `bars[-2]`, not the forming `bars[-1]`. Verify `ib_async` `realtimeBars`/`has_new_bar` semantics first (entry-timing change → test carefully). Doesn't close the SeanBot gap; it's a standalone correctness fix.

### Carried-forward / parked
- **Kill-switch §13b three-tier evaluator polish + streak-reset-by-orphan** — deferred (not a winning lever).
- **Crypto rebalance + Botty AI** — PARKED, separate threads.

### Uncommitted files / operational debt
- None known floating this session (CLAUDE.md committed earlier). Confirm `git status` clean in V-pre-flight.

---

## 8. Test safety — why we belabor this
Carry forward the cumulative list (do not regress):
1. Tests passing against a fictional schema because they mocked column names.
2. `side_effect` list wrong count → silent `StopIteration` → wrong assertions.
3. Mocked the raw library chain when code uses a wrapper → green tests, broken prod.
4. Shared `MagicMock()` state leaking between tests.
The master template (§12) guardrails prevent these. The #97 formatters are pure functions with explicit-arg tests (the right pattern) — keep §7.2/§7.3 logic in pure helpers the same way.

---

## 9. Pitfalls from prior sessions
- Handoff P&L numbers are stale by design — **re-query** TF cumulative / day total from `lifecycles` (the ≈−$1,877 here is reconstructed, not queried).
- "FLAT" in a prior handoff was wrong once (v16 → naked long). **Re-verify position from the broker (V1), every session.**
- Don't trust the dashboard SeanBot column (§0.5.197) or any "TF leads" headline until §7.3 ships.
- Docker healthy ≠ broker healthy (§0.5.151).
- The startup log says "STP-only on new entries" — **the live behaviour contradicts it** (§0.5.196). Trust the broker probe, not the log line.

**Next session rule: if a claim is quantitative, re-verify it — row counts, P&L, open-order counts, agreement rate.**

---

## 10. Session discipline lesson ( 2026-06-02 )
The autonomy contract worked: the operator suspended the AUDIT gate for the paper fast-forward and demanded a single autonomous run; VPS CC correctly **shipped the bounded part (Telegram) and STOPPED on the two unbounded parts (touch, scoreboard)** rather than ship unvalidated fixes — exactly the right call against the brief's stop/validation gates. The chat-tier failure was upstream: **twice I built a fix plan on a framing I hadn't checked against raw data.** The benchmark, not TF, was the thing to probe.

**Enforcement rules for next session:**
1. Before writing any "match SeanBot" brief, demand the true-OHLC / clean-feed probe of SeanBot's own behaviour first (§0.5.195, §0.5.97-extended).
2. Treat the scoreboard SeanBot column as untrusted input until §7.3 ships.
3. Fix §7.2 (pure trailing) before measuring TF's edge — a hybrid bracket contaminates the P&L signal.
4. Minimize redeploys during measurement windows (warmup churn cost ~24% of comparisons this session).

---

## 11. Logging verbosity — what to demand from any new code
- Every entry logs `[ALERT] entry_placed: … entry=<px> exit_mode=<mode> bracket=<shape>` (now present — keep it).
- Every ratchet advance logs `trailing_stop_ratcheted: old=<> new=<> highest=<> entry=<>`.
- Every exit logs `exit_filled: entry=<> exit_price=<> pnl_net=<> exit_reason=<>`.
- Every reconciler heal logs which leg, why, and whether exit-mode gated it (add this in §7.2).
- Any dedup/select-one-of-many (the §7.3 exit capture) must log which row won and why.
- Swallowed exceptions log type + msg + context (the daily-summary emit already does).

---

## 12. Master template — use for every Claude Code PR
See the `code-pr-brief` skill for the full template (patch constraints, code quality, test-safety guardrails, known gotchas, "What I got wrong"). §7.2 is **AUDIT** (entry/exit path) — open + CI green + **STOP for operator `merge`** if the gate is reinstated; on the current paper fast-forward, self-merge is allowed but the diff must still be posted.

---

## 13. Current PR brief in flight — hand to VPS CC after the §7.1 fork is decided

~~~
You are VPS Claude Code on the TradeFlow VPS. AUTONOMOUS run (paper fast-forward), ONE PR + deploy.
Pre-flight: git -C ~/tradeflow fetch origin ; git -C ~/tradeflow pull --ff-only origin main ; git -C ~/tradeflow log --oneline -1 origin/main ; ls -t ~/tradeflow/docs/handoffs/ | head -3 ; read the latest HANDOFF.
Discipline: no heredocs/$()/${VAR}/;/cd X &&; git -C; Write Python to /tmp/scriptN.py; commit -F; --body-file; branch off origin/main FIRST.

GOAL: make TF entries honor EXIT_MODE=trailing — STP-only, NO resting +150 LMT TARGET (§0.5.196). Today entries place a fixed {STP,LMT} bracket even in trailing mode; the ratchet walks the STP but the LMT also rests and caps upside.

PART A — diagnose (read-only): from the next/last entry's [ALERT] entry_placed + per-entry exit_mode log + a clientId-97 broker probe, confirm the entry path places a TARGET leg under exit_mode=trailing. Read src/execution/router.py entry placement + src/execution/reconciler.py (_reconcile_active / _heal_missing_legs). Quote the file:line where the TARGET leg is created and where the reconciler re-arms it.
PART B — fix: (1) entry placement: when exit_mode=="trailing", place the entry as parent + STP child only (no TP/LMT child). (2) reconciler: make _heal_missing_legs exit-mode-aware — in trailing mode, heal a missing STOP but NEVER re-arm a TARGET. Keep fixed-mode behavior unchanged. Pure-helper the decision where possible; add tests (trailing → no target placed + reconciler skips target re-arm; fixed → unchanged).
PART C — deploy ONE time (build --build-arg GIT_COMMIT=<sha> ; up -d --force-recreate) + verify: in-process exit_mode=trailing/bullish=2.0; the NEXT entry (or a forced re-eval) places STP-only, no LMT; not naked; no orphan; container healthy; per-entry log shows bracket=STP-only with no mismatch.
REPORT: file:line of the TARGET-leg creation + reconciler re-arm; the fix; broker proof of an STP-only entry; PR # + merge SHA; "What I got wrong".
~~~

---

## 14. Canonical references (in order of authority)
1. **Source code on `main` @ `63cfc75`** — what actually runs.
2. **Production DB** (`lifecycles` via service role) — truth for TF P&L / state (§0.5.98).
3. **IBKR** via `ib_async` (paper DUQ331660) — truth for position / fills / bracket / NetLiq.
4. **The operator's clean SeanBot Telegram feed (manual sum)** — truth for SeanBot P&L until §7.3 ships. The dashboard `/scoreboard` SeanBot column is BOGUS (§0.5.197).
5. **`/divergence`** — TF-vs-SeanBot entry agreement (interpret via §0.5.195 — disagreement is expected, not a TF bug).
6. **This handoff (v18)** — session context, NOT long-term authority.
7. **v17 and earlier** — historical; ignore any claim contradicting 1–5.

---

## 15. First 15 minutes of the next session
1. Read §0.5 (esp. §0.5.195–.198), §1, §5, §7.1. **Most important: §5 + §0.5.195 — there is no "touch bug"; do not re-open it.**
2. VPS CC pre-flight: `git -C ~/tradeflow fetch && git -C ~/tradeflow pull --ff-only origin main && ls -t docs/handoffs/ | head -3`, read this handoff from disk.
3. Run §6 **V1 first (broker position — bullish=2.0 may have opened a trade)**, then V0/V2/V3/V4.
4. **Put the §7.1 FORK to the operator** (get SeanBot's real entry rule, or validate TF's own edge). Single-word answer.
5. Dispatch §13 (the STP-only/exit-mode-aware fix) — correct under either fork. AUDIT-class; post the diff.
6. On the diff: review → `merge` (or self-merge on paper) → deploy → draft the smoke-test runbook (`vps-smoke-test-runbook` skill). Verify warm + STP-only + not-naked before letting it resume.
7. Then a no-redeploy window to measure TF's own win-rate / P&L on the proven trailing exit.

---

## 16. How to publish this handoff

**Path A — VPS CC brief (preferred — no operator browser):**

~~~
You are VPS Claude Code on the TradeFlow VPS. Pre-flight:
  git -C ~/tradeflow fetch origin
  ls -t ~/tradeflow/docs/handoffs/ | head -5

Determine N: this handoff is v18. If the latest existing handoff is NOT v17, set
N = (latest existing) + 1 and update the filename AND the "v18" references in the body to vN.

Save the handoff content (from /tmp/HANDOFF_v18.md, scp'd by the operator, or written via the
Write tool from the pasted content) verbatim to ~/tradeflow/docs/handoffs/HANDOFF_v<N>.md, then:
  git -C ~/tradeflow add docs/handoffs/HANDOFF_v<N>.md
  git -C ~/tradeflow commit -F /tmp/handoff_commitmsg.txt
  git -C ~/tradeflow push origin main
where /tmp/handoff_commitmsg.txt contains:
  docs: add v<N> handoff (trailing-ratchet first win proven; touch-parity premise disproven — SeanBot live rule unknown; hybrid-bracket + scoreboard bugs queued)

(Docs-only commit to main is the allowed push-to-main exception — no prod code/secrets.
If branch protection blocks it, open a 1-file PR off origin/main and squash-merge it.)

THEN, same run, a READ-ONLY state snapshot for the next chat (change NOTHING):
  docker exec tradeflow-app python -c "from config.risk_params import RISK; print('exit_mode=',RISK.exit_mode,'bullish=',RISK.ma_bullish_tolerance_pts,'regime=',RISK.regime_gate_enabled)"
  docker inspect tradeflow-app --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -E "TRADEFLOW_COMMIT|BULLISH_TOLERANCE|EXIT_MODE"
  docker logs tradeflow-app 2>&1 | grep -cE "halt_raised|exit_mode_mismatch"
Then re-create + run the clientId-97 read-only probe (/tmp/probe.py): MNQM6 position + all resting orders + the ACTIVE lifecycle's stop_order_id/target_order_id.
Report: the handoff commit hash + file path; the config line; the halt/mismatch count; AND the broker position + whether a SELL LMT rests alongside the STP (the §0.5.196 hybrid-bracket check) — so the next chat starts with live state in hand.
~~~

**Path B — manual fallback:**
```bash
scp HANDOFF_v18.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v18.md
ssh tradeflow@5.78.212.37 "git -C ~/tradeflow add docs/handoffs/HANDOFF_v18.md && git -C ~/tradeflow commit -m 'docs: add v18 handoff (trailing-ratchet first win proven; touch-parity premise disproven; hybrid-bracket + scoreboard bugs queued)' && git -C ~/tradeflow push origin main"
```

The handoff exists only once saved to disk and committed. Until then, treat this as draft.

---

*End of handoff v18. Target lifespan: until §7.2 (STP-only/exit-mode-aware) is merged, the §7.1 fork is decided, and TF has run a no-redeploy window long enough to measure its OWN win-rate / profit-factor on the proven trailing exit. Then rely on `main @ HEAD` + whatever v19 captures.*
