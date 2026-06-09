# TradeFlow — Handoff v25 (N=2 concurrency LIVE + cluster mode ON; gate confirmed CORRECT; window=1 only ~53% effective)

*Handoff from end of 2026-06-09. Bot is FLAT, regime gate LIVE + armed + blocking, now running N=2 × M=2 concurrency with the kill-switch cluster flag ON — but DORMANT (deep below-trend, gate blocking all entries). Deployed image `226b83f`; live env `MAX_CONCURRENT=2` + `KILL_SWITCH_CLUSTER_MODE=true`. No live 2-stack has occurred yet. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read sections 1–6 first — that's the state-of-the-system as of handoff. Sections 7–13 are reference material. Section 14 is the authority order to consult when this handoff disagrees with itself or a live observation.

**Do not trust this doc alone.** Run the verification block in §6 before writing any code. **Confirm the bot is FLAT, the regime gate is enabled+armed+blocking, and the live config reads N=2 + cluster_mode=ON FROM THE LIVE CONTAINER (the env-derived `RISK` instance, not bare defaults — §0.5.225) before taking any action.** The concurrency flip is LIVE; the next move is a strategy *decision* (see §7), not an authorized coded change.

---

## 0.5 Standing rules (permanent — do not remove or shrink from handoff)

**Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block. Self-contained commands, chained with `&&` or grouped. Source env vars explicitly in the same block. Expected output described immediately below each block, plus a decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**Learning-delivery discipline.** Every time you learn something new — a bug pattern, a corrected assumption, an environmental fact, a diagnostic finding — surface it immediately in the chat, formatted as a markdown snippet the owner can paste verbatim into the running handoff queue. Do not wait until end-of-session.

**Read before diagnosing.** When debugging a complex state bug, read the full startup log and 3-5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency language ("capital at risk", "churning fees", "spiraling"), hit the source of truth — live API, live DB, raw log file — not aggregated metrics.

**Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise. The owner does not run smoke tests by hand. (This session: the §6 verification block doubles as the post-merge smoke runbook for the deployed changes — the prod deploys were verified inline at deploy time.)

### Project standing rules (TradeFlow / carried from Botty AI — cumulative, verbatim)

- **§0.5.97 — Probe external specs against source before baking into briefs** (broker contracts, exchange fees, schema, library APIs). MNQ spec is §0.5.97-verified: `TICK_SIZE=0.25`, `MULTIPLIER=$2/point`, `COMMISSION_RT=$0.62`, `MARGIN_REQ=$2000` day-trade, CME maintenance ~$3636. Do not re-derive from memory.
- **§0.5.98 — Broker/exchange/DB state is ground truth, not internal DB tables and not the story.** Applies to your OWN workflow/git state too — verify "I merged it / it's unbuilt" against `git log` / `gh pr view` / `git ls-files`, not memory or a stale working copy. *Reinforced v25:* the v24 handoff claimed "1 contract" and "zero post-gate trades" — both stale; the live Telegram fills + the live config showed M=2 and an n=1 winner (see §5).
- **§0.5.105 — Comprehensive permission/scope sweeps over iterative patches.**
- **§0.5.215 — A risk gate has three distinct states: enabled, armed, blocking.** All three must be verified from the live container, not inferred from a config flag.
- **§0.5.220 — Never default to "we wait."** When a live test could take weeks/months (e.g. regime-paced trades), proactively flag the long wait upfront and propose faster alternatives (backtest real history, synthetic-scenario harness, fault injection). Idle/closed-market windows = build time. *(v25 applied this: the N=2 dormant window was used to build the gate-calibration study + the concurrency replay rather than wait for a live 2-stack.)*
- **§0.5.221 — Durability.** Run CC and any long job INSIDE tmux (session `tf1`). Redirect long Python to a `/tmp/<name>.txt` file (survives an SSH drop). Pull results via `scp`. A dropped laptop must be a non-event. *(v25: one SSH drop occurred AFTER a CC report printed — a non-event, exactly as intended.)*
- **§0.5.222 — A surprising aggregate is a prompt to PROBE THE MECHANISM, not a finding to report.** *(v25 applied twice: the gate-calibration study's first "GATE-TOO-STRICT" was a multiple-comparison artifact; probing it produced the hardened both-samples-must-clear rule. See §5.)*
- **§0.5.223 (NEW v25) — A risk control firing ≠ the safety it implies being effective.** §0.5.215 (enabled+armed+blocking) is necessary but NOT sufficient: also validate the control's COVERAGE against real data. `cluster_mode=ON` is enabled, armed, and FIRES (collapses 222 loss-events on the historical tape) — yet it only covers ~53% of real correlated stops at `cluster_window_bars=1`, so the N=2 kill-switch trap is only PARTIALLY defused (peak streak 11 → 10, still ≥ halt@10). A flag that fires is not a flag that works; measure the coverage. (See §4, §5, §13.)
- **§0.5.224 (NEW v25) — Concurrency/sizing has DB-layer preconditions independent of app config.** A `MAX_CONCURRENT` config bump is silently inert if the DB still carries a `≤1 open` unique index. Verify the data layer (`pg_indexes`), not just the config dial, before trusting a sizing flip. *(v25: `lifecycles_one_open_per_setup` had to be migrated in — the held #113 migration — before N=2 could open a 2nd position. See §4.)*
- **§0.5.225 (NEW v25) — Read live config from the env-derived instance, never bare defaults.** Verify deployed config via `from config.risk_params import RISK` (the env-constructed singleton), NOT `RiskParams()` (which returns dataclass defaults and will mask the real values — e.g. `max_concurrent` default 3 vs live 2). (See §5, §6 V1.)
- **Two-tier model.** Chat tier = strategy / PR briefs / handoffs. VPS Claude Code = implementation end-to-end in tmux `tf1`. VPS CC NEVER git-pushes prod code on its own outside an AUTO research PR; chat never edits code. CC has NO raw-SQL/RPC channel to Supabase (only typed PostgREST) — DB migrations are operator-only.
- **VPS CC end-to-end mission model.** Every CC work order: implement → test → ship PR → self-merge AUTO after CI green → deploy → verify from broker/DB truth → report. Batch follow-ups into ONE run. PAUSE only for AUDIT (order / strategy / kill-switch / secrets / broker-state), strategy-param calls, or external blockers. Owner = hands-off PM, single-word approvals.
- **VPS CC bash trigger-pattern discipline.** Never use `$(...)`, `${VAR}`, heredocs, `cd X &&`, or `;` separators in CC bash invocations. Write Python to `/tmp/scriptN.py` via the Write tool; commit messages via `git commit -F /tmp/commitmsg.txt`; PR bodies via `--body-file /tmp/pr_body.md`. The ONE allowed literal env assignment is the deploy build (`GIT_COMMIT=<literal hash> docker compose ...`). (Heredocs ARE fine when the OWNER drives his own Mac shell — e.g. the publish block in §16.)
- **Operator autonomy preference.** Maximally hands-off; default to delegating. Operator explicitly opted OUT of `--dangerously-skip-permissions` — do not suggest it.
- **Docs/handoff publish discipline.** Direct push to main is blocked by a RULESET: the "Lint, type-check, and test" check must COMPLETE before any merge — `--admin` does NOT skip an in-progress check. Give the owner the COMPLETE block in ONE shot: branch off origin/main → commit → push → `gh pr create` → POLL `gh pr checks <branch>` in a loop (sleep 15, ~10-min cap, fail-fast on fail/error) until green → `gh pr merge --squash --admin --delete-branch` → resync. NEVER `gh pr checks --watch`. Use `set -uo pipefail` (NOT `-e` — the poll loop returns non-zero while the check is pending).

---

## 1. Where we are (as of handoff, 2026-06-09 ~late UTC)

### Live production state
- Containers: `tradeflow-app` Up (healthy), `tradeflow-ib-gateway` Up (healthy), `tradeflow-telegram-listener` Up ~11 days.
- Position: **FLAT.** realizedPNL on the account = $166.52 (the one post-gate winner, 2026-06-08).
- **Concurrency: N=2 × M=2, LIVE.** `MAX_CONCURRENT=2`, `contracts_per_trade=2` (4 contracts max book). **DORMANT** — the regime gate is blocking all entries (price ~28,700 vs 30m EMA200 ~29,800; ~1,000 pt below-trend). No live 2-stack has happened yet.
- **Kill-switch cluster mode: ON.** `KILL_SWITCH_CLUSTER_MODE=true`, `cluster_window_bars=1`, real entry-bar plumbing live. warn@6 / halt@10 consecutive **cluster-events**. ⚠️ See §4 — window=1 only ~53% effective; the trap is PARTIALLY (not fully) defused at N=2.
- Regime gate (30m EMA200): **LIVE + armed + blocking.** Losses concentrate entirely in below-trend longs; gate confirmed CORRECT this session (§1 findings, Phase 12).
- **Deployed image: `226b83f`** (= PR #130). The N=2 config (#131) is applied via env (compose), no rebuild — so the running container is image `226b83f` + the #131 env. **main HEAD: `cc6f1f3`** (= PR #134). The delta `226b83f`..`cc6f1f3` is config-applied-via-env (#131) + offline `tools/eval/` research (#132–#134) — **NO undeployed prod-code drift** (see §6 V2, §14).

### What just shipped (PRs #127–#134, all merged this session)
- **#127** (`c4a6b06`) — **feed-gap fix** (PROD, deployed). Root cause + fix in §2/§5. Cancel-prior-sub before resubscribe + escalate a wedged feed to a forced socket reconnect.
- **#128** (`b6e76e1`) — **shadow ledger, Phase 10** (research). Pairs SB's gate-blocked entries with his realized exits; scores WR/PF/net$ of what the gate denied.
- **#129** (`acfb66e`) — **kill-switch cluster math** (AUDIT, prod-inert). `kill_switch_cluster_mode` flag (default OFF) + `_collapse_loss_clusters` (collapses correlated stop-clusters by entry proximity into 1 loss event).
- **#130** (`226b83f`) — **entry-bar plumbing** (AUDIT, prod-inert). Wires real `entry_filled_at` (epoch-minutes) into `evaluate_triggers` so cluster mode operates on live data. Deployed (#129+#130 inert at `226b83f`).
- **#131** (`da6414b`) — **concurrency config flip** (PROD config, deployed). `MAX_CONCURRENT` 1→2 + `KILL_SWITCH_CLUSTER_MODE=true` in compose. The LIVE N=2 activation.
- **#132** (`45a7a3c`) — **shadow-ledger REVIEW, Phase 11** (research). Decision instrument: cohort split, per-day/cumulative, pre-committed decision bar, feed-regression flag.
- **#133** (`fd1a4b3`) — **gate-calibration study, Phase 12** (research). Verdict **GATE-CORRECT**.
- **#134** (`cc6f1f3`) — **concurrency/cluster replay, Phase 13** (research). Validated the N=2 ship; found window=1 only ~53% effective.
- **DB migration** (operator-applied via Supabase SQL editor): dropped `lifecycles_one_open_per_symbol_strategy`, created `lifecycles_one_open_per_setup` (migration `20260605000000`, the long-held #113 migration). The DB precondition for N=2.

### What we discovered this session (findings)
- **The gate is CORRECT (Phase 12).** Below-30m-EMA200 longs pool at OOS PF **1.026** over 26 months (far under the 1.20 bar, ~2× the above-book drawdown); no depth / time-of-day / EMA-slope sub-cohort robustly separates winners. SB's recent wins on the trades the gate blocks are variance, not a missed edge. Above-trend OOS anchor reproduces exactly: **n=1479, +$28,729, PF 1.174.**
- **window=1 only catches ~53% of real correlated stops (Phase 13).** Two positions stop together on one down-move but ENTERED on different pullback bars (median entry-gap 1 min, p90 8 min, max 498). Peak consecutive-loss run at N=2 over 26mo = **11** per-trade → cluster-w1 = **10**, **still ≥ halt@10**. The real `evaluate_triggers` returns PAUSE both ways at the worst poll. So the §4 trap is PARTIALLY defused at N=2 — acceptable (a halt is safe-conservative), but a PREREQUISITE to resolve before any N=3 (§13).
- **TF runs M=2, not M=1.** The v24 handoff's "1 contract" was stale. `contracts_per_trade=2` (hardcoded default) AND every Telegram fill reads "Bot size: 2 contracts / P&L (2 ct)". So pre-flip TF was N=1×M=2 (the study's Calmar-6.41 config); the flip took the max book 2→4 contracts, not 1→4.
- **First post-gate qualifying trade exists.** 2026-06-08 23:03 UTC: long @29,410 → +41.94 pt = **+$165.28** (WIN). n=1 toward the PF>1.2 / 20–30-trade bar. The exit profit-walk on a winner is now proven on the #114/`226b83f` build (clears a v24 passive-watch item).
- **Shadow ledger standing: INSUFFICIENT [NOISE].** n_paired=6 (< the 20 decision bar), net +$545 / PF 2.58 directional-only; 0 MISS-NO-BAR after feed fix #127 → no feed regression.

---

## 2. The session's work thread

1. Read v24, verified state (FLAT, gate blocking, deployed `237a7502` at session start). Cross-read the 24h Telegram tape + the SB-vs-TF dashboard against v24's narrative.
2. **Caught three stale v24 claims from the live tape** (§0.5.98): (a) "zero post-gate trades" → actually n=1 winner (Jun-8 +$165.28); (b) exit profit-walk now proven on a winner; (c) a ~5–6h feed blackout on the Jun-7 Sunday reopen (an infra leak v24 didn't capture) — and SB winning on the below-trend longs the gate blocks.
3. Operator directives, in order: fix the feed leak; build a shadow ledger; go concurrent (chose **N=2 × M=2** after weighing the study's Calmar); each step gated behind verification.
4. **#127 feed fix** — root-caused from raw logs (not greps): `subscribe_bars` re-issued `reqHistoricalDataAsync(keepUpToDate=True)` WITHOUT cancelling the prior subscription → the gateway cancelled the NEW query (Error 162, `seeded=0`) → every same-socket self-heal resubscribe (every 6 min, 22:01→03:25) was a no-op for 5.5h; recovery came ONLY from an unrelated gateway peer-close → full socket reconnect (`seeded=331`). Fix: cancel-prior-sub before resubscribe + escalate to a forced reconnect after 3 failed heals (caps a future blackout at ~15 min). Deployed + smoke-verified bars resumed.
5. **#128 shadow ledger** — built the gate-cost instrument from existing telemetry (`signal_reconciliations` + `seanbot_signals` FIFO). First read: n=6, noise.
6. **#129 + #130 kill-switch cluster stack** (AUDIT, default OFF, byte-identical) — math + real entry-bar plumbing. Built to CI-green, paused for approval, merged on operator word, deployed inert at `226b83f`, Task-F smoke confirmed `cluster_mode=False` in the container (then flipped — step 8).
7. **DB migration** — Stage-B probe found the N=2 blocker: the `≤1 open` index was still live (the #113 dedup migration had been HELD). Operator applied `20260605000000` in the Supabase SQL editor (CC has no SQL channel); confirmed `lifecycles_one_open_per_setup` live, old index dropped.
8. **#131 concurrency flip** — `MAX_CONCURRENT` 1→2 + `KILL_SWITCH_CLUSTER_MODE=true` via a compose PR, merged, recreated app (config-only, image stays `226b83f`); verified from the live `RISK` instance (`cluster_mode True, max_concurrent 2, contracts 2`) and the plumbing log firing.
9. **#132–#134 dormant-window research** — shadow-ledger review (decision instrument), gate-calibration study (**GATE-CORRECT**), concurrency replay (**window=1 only ~53% effective**). Two CC self-caught errors corrected pre-merge (§5).

The goal of this section: a new session reads it and does NOT re-walk the closed rabbit holes (the feed bug is fixed; the gate is confirmed correct; window=1's gap is known and characterized).

---

## 3. What the system is actually made of

**Single source of truth:** none as a one-file map — this handoff + the source on `main` (`cc6f1f3`) are the best available. The eval kit lives at `tools/eval/` with phases documented in `tools/eval/README.md` (now through Phase 13).

Highlights:
- **Prod-live code path:** the IBKR live bot — regime gate (`strategy._regime_ok`), entry (`orchestrator._handle_trade_signal` → `state_machine` count gate at `max_concurrent`), per-position trailing exit (`router.ratchet_stop_on_bar`, standalone parentId=0 STP per lifecycle), kill-switch (`src/execution/kill_switch.py`: `evaluate_triggers` + `_collapse_loss_clusters` + `_entry_bar_minutes`), feed subscribe/reconnect (`src/clients/ib_client.py`, `orchestrator._maybe_heal_stale_feed`).
- **Config:** `config/risk_params.py` — the env-constructed `RISK` singleton (read THIS, not `RiskParams()`, §0.5.225). `docker-compose.yml` `environment:` block is the repo-tracked injection point (literals override `.env`).
- **Research surface (NOT prod):** everything under `tools/eval/` + `tests/test_*_study.py` / `test_shadow_*.py` / `test_concurrency_replay.py`. Offline, drives NO prod path.
- **DB:** Supabase `lifecycles` (state machine; CLOSED rows carry `pnl_net`, `entry_filled_at`, `exit_filled_at`), `seanbot_signals` (SB entries/exits, `pnl_points` on exits), `signal_reconciliations` (TF's MISS-regime / MISS-NO-BAR / AGREE classification by `message_id`). Concurrency-enabling index: `lifecycles_one_open_per_setup` (per setup_key).
- **Phantom/ignore:** `docs/tf_research*.zip`, untracked `research/` (pre-existing, NOT load-bearing; CI never lints it).

---

## 4. Verified facts (2026-06-09) — DO NOT challenge unless the code/schema changes

- MNQ spec (§0.5.97): `TICK_SIZE=0.25`, `MULTIPLIER=$2/point`, `COMMISSION_RT=$0.62`, `MARGIN_REQ=$2000` day-trade, CME maint ~$3636. Friction is PER contract.
- **TF sizing is M=2 (2 contracts/position), NOT 1.** `contracts_per_trade=2` (hardcoded default, no env). Confirmed by every Telegram fill. (Corrects v24.)
- **Concurrency is a real config dial in the live path** — gated by `max_concurrent` in `state_machine` (raises `InvariantViolationError` when `len(existing) >= max_concurrent`; that is the `suppressed_in_position` path). Order management is per-position (per-lifecycle standalone STP; no shared OCA; no Error 328/10326 risk with 2 brackets).
- **§0.5.224 — concurrency needs the DB index `lifecycles_one_open_per_setup`** (per setup_key, `WHERE state <> 'CLOSED'`). The old `lifecycles_one_open_per_symbol_strategy` (≤1 open) is DROPPED. Without the swap, a 2nd insert 409s and concurrency is silently inert.
- **§0.5.223 — `cluster_mode=ON` (window=1) is only ~53% effective at N=2.** It FIRES (collapses 222 loss-events on the 26mo tape) but real correlated stops have entries >1 bar apart (median 1, p90 8 min), so the worst-case N=2 streak only drops 11→10 — STILL at halt@10. The kill-switch trap is PARTIALLY defused. Acceptable at N=2 (a halt is safe-conservative); a PREREQUISITE to resolve before N=3 (§13).
- **The regime gate is CORRECT** (Phase 12): below-trend longs pool at OOS PF 1.026 over 26mo; no sub-cohort separates winners. Do not loosen it. SB's blocked-trade wins are variance.

---

## 5. Wrong diagnoses this session — READ BEFORE YOU DEBUG

**Wrong diagnosis #1 — "GATE-TOO-STRICT" (gate-calibration first pass).**
- Diagnosis: a mid-depth below-trend band cleared the OOS bar (PF 1.25) → the gate is too strict, there's a tradable below-trend sub-cohort.
- Evidence that misled: the raw walk-forward OOS PF of one of 11 tested bands.
- Why wrong: multiple-comparison artifact. The band's FULL-sample PF was only 1.16 (more data, weaker), and the effect was non-monotonic (shallow band negative, mid pops, deep flat) — not a contiguous real edge.
- Correct diagnosis (§0.5.222): harden the rule to require PF≥1.20 on BOTH the OOS and the full sample (the larger sample must corroborate). Re-ran → **GATE-CORRECT**.

**Wrong diagnosis #2 — concurrency-replay report header showed `max_concurrent=3 cluster_mode=False`.**
- Diagnosis (header): the deployed config looked like defaults.
- Why wrong: the header read `RiskParams()` (dataclass defaults), not the env-derived `RISK` singleton (live = `max_concurrent=2, cluster_mode=True`). The replay body used N=2 explicitly, so the analysis was correct — only the printed header was misleading.
- Correct fix → new **§0.5.225**: always read live config from `RISK`, never bare `RiskParams()`.

**Wrong assumption (chat-tier) — v24's "1 contract" + "zero post-gate trades."** Both stale; the live Telegram fills (M=2) and the Jun-8 winner (n=1) corrected them (§0.5.98).

**Lesson for next session:** every wrong turn this session came from trusting an aggregate or a default over the live mechanism. Probe a surprising aggregate (§0.5.222); read config from the live env-derived instance (§0.5.225); a flag that fires is not a flag that fully works (§0.5.223).

---

## 6. Verification block — run this before doing anything

> Run inside `tmux tf1`. This doubles as the post-merge smoke runbook for this session's deploys (the prod deploys #127/#130/#131 were verified inline at deploy time).

**V0 — Bot FLAT + gate enabled+armed+blocking (§0.5.215, all three from the live container)**
```bash
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}"
docker logs --since 2h tradeflow-app 2>&1 | grep -Ei "regime gate (enabled|armed|BLOCKED)|position=" | tail -20
```
Expect: three containers Up/healthy; recent `regime gate BLOCKED` evals on live bars; every `updatePortfolio` shows `position=0.0`. If an open position or no gate state → STOP and re-ground.

**V1 — Live config is N=2 + cluster ON (read the env-derived `RISK`, NOT `RiskParams()` — §0.5.225)**
```bash
docker exec tradeflow-app python -c "from config.risk_params import RISK; print('cluster_mode', RISK.kill_switch_cluster_mode, 'window', RISK.cluster_window_bars, 'max_concurrent', RISK.max_concurrent, 'contracts', RISK.contracts_per_trade)"
```
Expect: `cluster_mode True window 1 max_concurrent 2 contracts 2`. Baseline as of this handoff. Any deviation = config drift, investigate before acting.

**V2 — Deployed image + main HEAD + drift check**
```bash
docker inspect --format "{{range .Config.Env}}{{println .}}{{end}}" tradeflow-app | grep -iE "MAX_CONCURRENT|CLUSTER|commit"
git -C /home/tradeflow/tradeflow fetch origin && git -C /home/tradeflow/tradeflow log --oneline -1 origin/main
git -C /home/tradeflow/tradeflow status --short
```
Expect: env `MAX_CONCURRENT=2`, `KILL_SWITCH_CLUSTER_MODE=true`, `TRADEFLOW_COMMIT=226b83f…`; `origin/main` HEAD = `cc6f1f3` (or later). **The deployed image `226b83f` is the last code build; `226b83f`..`cc6f1f3` is config-applied-via-env (#131) + offline `tools/eval/` research (#132–#134) — NOT undeployed prod-code drift. Do not flag it as drift.** Tree dirty only on known `docs/*.zip` + `research/`.

**V3 — DB concurrency index (operator-run; CC has no SQL channel)**
Supabase SQL editor: `select indexname from pg_indexes where tablename='lifecycles';`
Expect: `lifecycles_one_open_per_setup` present, `lifecycles_one_open_per_symbol_strategy` ABSENT. If the old one is back / the new one is gone → concurrency is silently inert (§0.5.224); STOP.

**V4 — Eval suite + lint (CI parity)**
```bash
cd /home/tradeflow/tradeflow && .venv/bin/python -m pytest tests/ -q
cd /home/tradeflow/tradeflow && .venv/bin/black --check . && .venv/bin/ruff check .
```
Expect: full suite green (baseline ~759); black/ruff clean on tracked files (the untracked `research/` dir has pre-existing lint errors CI never sees — ignore).

**V5 — Cluster plumbing is firing (the OFF→ON proof)**
```bash
docker logs --since 6m tradeflow-app 2>&1 | grep -Ei "KILL.*entry-bar plumbing|collapsed .* stops" | tail -5
```
Expect: `[KILL] entry-bar plumbing — supplied N entry bars (cluster_mode=True)` ~every 30s. A `collapsed N stops into 1 loss event` line = a real correlated cluster fired (watch for the FIRST one — that's the live trap-defusal proof; cross-check the streak didn't reach halt).

---

## 7. Pending work queue (priority order — depends on the operator's strategy decision)

The next move is a **decision**, not code. There is no authorized prod change.

1. **DECISION: the cluster_window_bars calibration (the open §13 brief).** window=1 only ~53% effective. Options: (a) leave window=1 — acceptable at N=2 (a worst-case halt is safe-conservative), revisit before N=3 [operator's stated lean at end of v25]; (b) recalibrate now (widen window / key on exit-proximity / raise halt for N>1) — §13 brief, AUDIT; (c) any N=3 move REQUIRES (a)→(b) first.
2. **PR — cluster-window recalibration (CONTINGENT / AUDIT).** Ready brief in §13. ONLY paste to CC if the operator decides to tune now or picks N=3.
3. **Live edge go/no-go bar:** PF >~1.2 over 20–30 qualifying above-trend trades. Now **n=1** (the Jun-8 winner). Dormant until the regime turns above-trend (~3.5% recovery from current).
4. **Shadow-ledger review trigger:** at n_paired ≥ 20, re-read the Phase-11 verdict (currently INSUFFICIENT/noise). The HISTORICAL gate verdict is already GATE-CORRECT, so the live ledger is a confirm/watch.
5. **First live 2-stack + correlated cluster** = the live proof of concurrency + the cluster collapse. Watch `collapsed N stops into 1 loss event` and confirm the streak stays under halt@10.
6. **Passive watch (carried from v24):** gap-reseed re-arm on the next real feed gap (the #127 escalation backstop should now handle it — watch). The §13 router stop-id-refresh PR remains HELD (ship condition not met).

### Uncommitted / operational debt
- SB's actual STRATEGY code (`ma_bounce.py` / `settings.py`) is NOT on the VPS (only his Telegram parser) → a **DATA GAP** for a code-level reconciliation of SB's real entry rule vs the gate. Low priority (the historical gate verdict is robust regardless); the chat-tier reverse-engineering lives in the operator's Session-24 upload, not on the VPS.
- Untracked `docs/tf_research*.zip`, `research/` — pre-existing artifacts; leave or clean, not load-bearing.

---

## 8. Test safety — why we belabor this

Carry forward the cumulative mocking-trap list (none newly hit this session — all new tests are deterministic synthetic-tape / hand-built telemetry, no DB mocks):
1. Tests passing against a fictional schema because they mocked column names.
2. `side_effect` list with the wrong count → silent `StopIteration` → wrong assertions.
3. `patch()` on a module-level factory when code uses a wrapper → green tests, broken prod.
4. Shared `MagicMock()` state leaking between tests.
5. Async decorator pattern assumed instead of verified against a neighbor.

The `code-pr-brief` Pre-Push Checklist (§12) prevents all these. Do not ship tests that skip them.

---

## 9. Pitfalls from prior sessions

- "I merged it / it's not built" — verify against `git log` / `gh pr view` / `git ls-files`, not memory (§5, v24).
- A surprising aggregate is not a finding — probe the mechanism (§0.5.222).
- A config flag being SET ≠ a gate blocking (§0.5.215) AND ≠ the safety being effective (§0.5.223, NEW).
- Config read from `RiskParams()` shows DEFAULTS, not the live env values — read `RISK` (§0.5.225, NEW).
- A `MAX_CONCURRENT` bump is inert without the DB index swap (§0.5.224, NEW).
- SeanBot's cumulative gap vs TF is partly overstated (est. days; pre-gate blowup); SB posts losers too (do not claim deletion).
- Background jobs not in tmux die on SSH drop (§0.5.221).

**Next session rule: if a claim is quantitative, re-verify it.** Especially open-position state, gate state, the live N=2 config (`RISK`), the DB index, and any PF / Calmar / streak number (re-derive, don't trust this handoff's table blind).

---

## 10. Orchestrator (chat-tier) comments — logged this session

- **What I (chat) got wrong / corrected.** I carried v24's "1 contract" and "zero post-gate qualifying trades" into early framing; the live Telegram fills (M=2) and the Jun-8 winner (n=1) corrected both (§0.5.98). I framed "beat SB" around the leverage dial; the gate-calibration study I proposed then confirmed the gate is CORRECT and SB's blocked-trade wins are noise — so I corrected the framing to: concurrency is the only available lever, and it will NOT beat SB daily on its own (he runs more trades AND ≥ size). I surfaced this honestly rather than letting the N=2 flip imply a daily-beat.
- **Lane discipline held.** Every prod change went through a PR with CI. AUDIT changes (#129/#130/#131) were built to CI-green and PAUSED for the operator's single-word approval — none auto-merged. The DB migration was operator-only (CC has no SQL channel). Research (#128/#132/#133/#134) AUTO-merged on green. The concurrency flip was deliberately sequenced (verify code-supports-it → DB migration → config flip → verify from the live container), never a blind toggle.
- **Honest paper/edge note.** The edge is THIN (PF ~1.1). Everything is paper / modeled fills; the live forward edge is UNPROVEN (n=1 post-gate trade). N=2 is DORMANT until the regime turns. The cluster safety is only ~53% effective at window=1 — the trap is partially, not fully, defused (§4/§5). Concurrency buys more gross dollars at WORSE Calmar (N=2 ~4.97 vs single-position 6.41) and a bigger correlated drawdown; single-position remained the best risk-adjusted config. Do not over-read backtest dollars; the live go/no-go bar (§7.3) is still the real test.
- **Process win.** The Phase-13 replay caught the window=1 coverage gap from REAL data BEFORE a live cluster relied on it — the validation did its job. Two CC self-caught errors (the multiple-comparison artifact, the host-venv header) were corrected pre-merge; neither affected an analysis.

**Enforcement rules for next session:**
1. Verify FLAT + gate enabled+armed+blocking + the live N=2 config from `RISK` (NOT `RiskParams()`) before anything (§0.5.215, §0.5.225).
2. A flag firing ≠ a flag effective — measure coverage against real data (§0.5.223).
3. Probe a surprising aggregate's mechanism before reporting it (§0.5.222).
4. Resolve the window=1 calibration (§13) BEFORE any N=3.

---

## 11. Logging verbosity — what to demand from any new code

- Every state transition logs old → new at INFO.
- Every gate evaluation logs the decision + reason (`regime gate BLOCKED — below 30m EMA200`).
- Every kill-switch evaluation logs the consecutive-loss count and the threshold; cluster collapses log `collapsed N stops into 1 loss event`.
- Every swallowed exception logs the specific error + context.
- Any dedup / select-one-of-many logs which row won and why.
- Format: `[COMPONENT] symbol: action — reason`.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill for the full template (Role / Context / Architecture / Engineering Standards / Mission Tasks A–F / Expected Output / Pre-Push Checklist incl. the 8 test-safety guardrails / Known Gotchas). The §13 brief below is built from it.

---

## 13. Current PR brief in flight — cluster-window recalibration (CONTINGENT / AUDIT-GATED — do NOT paste to CC unless the operator decides to tune the window now OR picks the N=3 path)

~~~
# TradeFlow — Claude Code PR Prompt: kill-switch cluster grouping — make correlated stops reliably collapse (flagged, default-safe)

## Role
You are a senior Python developer on TradeFlow, an autonomous MNQ futures bot on IBKR paper (~$1M NetLiq) that will eventually trade real money. Clean, tested, production-grade code. You never modify files you weren't asked to. Study existing patterns before writing; state your expected pattern then verify against the actual file; never trust prior claims about behavior without verifying.

You are verbose in logging. Format: `[COMPONENT] symbol: action — reason`.

**THIS IS AN AUDIT PR (it touches the kill-switch, a risk control). After CI is green you DO NOT self-merge — PAUSE and wait for the operator's explicit single-word approval. Default behavior must stay byte-identical until the operator turns the new behavior on.**

## Context
Phase-13 replay (#134) proved that on REAL above-trend NQ history at N=2, the shipped `cluster_window_bars=1` only collapses ~53% of correlated stops: two positions stop together on one down-move but ENTER on different pullback bars (median entry-gap 1 min, p90 8 min). The worst-case N=2 consecutive-loss run is 11 per-trade → only 10 after window=1 collapse → STILL ≥ halt@10. So the §4 kill-switch trap is only PARTIALLY defused. This PR makes correlated stops reliably group, so a concurrent book's correlated cluster counts as ONE loss event — required before any N=3.

## Architecture & Recent Learnings
- Kill-switch: `src/execution/kill_switch.py` — `evaluate_triggers` + `_collapse_loss_clusters` (collapses by ENTRY proximity within `cluster_window_bars`) + `_entry_bar_minutes`. Config: `config/risk_params.py` (`kill_switch_cluster_mode`, `cluster_window_bars`, `kill_switch_halt_consec_losses`). CLOSED lifecycles carry `entry_filled_at` AND `exit_filled_at`.
- The trap is N correlated stops read as N consecutive losses tripping the single-position-tuned halt@10.

### Key Architecture Constraints
- Runtime: kill-switch reads closed PnL newest-first; any bar list MUST stay index-aligned to the pnls.
- Scope boundary: do NOT change entry/exit/sizing/regime-gate/broker/secrets, or anything under `tools/eval/`.
- **Design decision (lay out, recommend, do NOT silently pick):**
  - **Option A — key the cluster on EXIT proximity** (recommended primary): correlated stops share an EXIT bar (they stop together) even when entries differ — this is the mechanistically-correct fix for the Phase-13 finding. Add an exit-proximity grouping path.
  - **Option B — widen the ENTRY window** (e.g. `cluster_window_bars` ~2–8): simpler, but false-merges loss clusters that are merely temporally near (can HIDE genuine independent losses from the halt).
  - **Option C — raise `halt_consec_losses` for a concurrent book** (e.g. scale with `max_concurrent`): blunt; weakens death-spiral detection uniformly.
  - Recommend A (or A behind a new sub-flag), keep all new behavior behind a default that reproduces today exactly. State the false-merge tradeoff loudly.

## Engineering Standards (Strict)
### 1. Patch Constraints
Files you WILL modify (confirm exact set in Task A — aim for EXACTLY 2): `src/execution/kill_switch.py`, `tests/test_kill_switch.py`. (Add a `config/risk_params.py` field only if the chosen option needs a new knob — if so it's 3 files; flag it, default-safe.)
Files you MUST NOT modify: entry/exit/sizing/regime-gate/broker/secrets; `tools/eval/`.
Verification gates: `git diff main -- <entry/exit/sizing/gate/broker paths>` MUST be empty; `git diff main --stat` = the intended file count.
### 2. Code Quality
`black --check` + `ruff check` pass; no unused imports; one import per line; <100 chars; no public signature changes (extend `evaluate_triggers` via defaulted kwargs only); logging `[KILL] : cluster mode — grouped N correlated stops (by <entry|exit> window <= W) into 1 loss event`.
### 3. Safety
All pre-existing tests pass. Default behavior byte-identical (the new grouping is OFF or reproduces today until the operator enables it). No DB writes, no broker calls. Adjacent bug → document, do not fix.

## Mission Tasks
- **A — Audit:** read `_collapse_loss_clusters` + `evaluate_triggers` + the `_evaluate` poller + 1 neighbor test. Confirm exact paths, how grouping is keyed today (entry proximity), and what exit-bar info is available at evaluation time. 3–5 line finding.
- **B — Implement** the chosen option (default A) behind a default-safe path. Mirror existing logging.
- **C — Tests:** (1) default path byte-identical to today (regression); (2) a real-shaped correlated cluster (entries 1–8 min apart, same exit bar) now collapses to ONE loss event; (3) genuinely independent losses are NOT merged (false-merge guard); (4) N=1 single-position book unaffected. Follow the 8 test-safety guardrails.
- **D — Completeness:** grep every caller of `evaluate_triggers` / every builder of the pnl+bar lists; classify touched/untouched.
- **E — Out-of-scope:** quantify (offline, do NOT build) the new coverage % vs the Phase-13 ~53% baseline and the false-merge rate at the chosen setting. Document.
- **F — Post-merge smoke (operator runs after approval + deploy):** `docker exec tradeflow-app python -c "from config.risk_params import RISK; print(RISK.kill_switch_cluster_mode, RISK.cluster_window_bars)"` and confirm the live behavior matches intent; STOP if the default changed unexpectedly.

## Expected Output
Diff stat (intended file count); PR description with Task A finding, the design option chosen + rationale + false-merge tradeoff, Task D grep + classifications, Task E coverage numbers, local + full pytest tails, empty protected-file diffs, explicit "This PR does NOT change entry/exit/sizing/concurrency and changes NO live behavior under the default", and a "What I got wrong" line.

## Pre-Push Checklist
Code quality (black/ruff/no unused/one-import-per-line/no signature change). 8 test-safety guardrails. Production safety (verification gates empty; grep complete; smoke included; scope statement; adjacent bugs noted; "what I got wrong"). **AUDIT: do NOT self-merge — PAUSE for operator approval.**

## ⚠️ Known Gotchas
1. MNQ spec is §0.5.97-verified — do not re-derive.
2. Kill-switch reads closed PnL newest-first — keep any bar list index-aligned.
3. Default behavior must be byte-identical (regression test #1 mandatory).
4. The collapse math (#129) + entry-bar plumbing (#130) already exist — extend, do not re-implement.
5. window=1 catches only ~53% of real correlated stops (Phase 13) — that is the bug this PR fixes.
6. Read live config from `RISK`, never `RiskParams()` (§0.5.225).
7. Docker restart != rebuild; deploy is config/code-dependent and operator-gated (NOT in this PR).
8. Pre-existing red tests (if any) are not yours to fix.
~~~

---

## 14. Canonical references (in order of authority)

1. **Live container** (`docker exec` / `docker logs tradeflow-app`) + **IBKR broker state** + **Supabase via service role** — truth for gate state, position, config (`RISK`), fills, and the `lifecycles` index (§0.5.98, §0.5.215, §0.5.224).
2. **Source code on `main`** at `cc6f1f3` — what actually runs (deployed image `226b83f` + #131 env; the delta to `cc6f1f3` is config-via-env + offline research, no prod-code drift).
3. **`tools/eval/` + `tests/` on `main`** — research tooling; `tools/eval/README.md` documents Phases 1–13.
4. **SB's uploaded code** — operator's Session-24 upload (NOT on the VPS; chat-tier only).
5. **This handoff (v25)** — session context, NOT long-term authority.
6. **v24 and earlier handoffs** — historical; ignore any claim contradicting 1–4 (v24's "1 contract" / "zero post-gate trades" are corrected here).

---

## 15. First 15 minutes of the next session

1. Read §0.5, §1, §4, §5, §10 of this handoff. **§4 (window=1 partial) + §5 (wrong diagnoses) are the most important.**
2. Reconnect; `tmux attach -t tf1` (or `tmux new -s tf1`). Run §6 V0–V5. Confirm: FLAT, gate enabled+armed+blocking, live config = N=2 + cluster ON (from `RISK`), DB index `lifecycles_one_open_per_setup` live, suite + lint green, plumbing log firing.
3. No uncommitted prod cleanup needed (tree clean beyond known artifacts).
4. Surface the §7.1 decision to the operator: the window=1 calibration (leave / tune now / required-for-N=3). Do NOT proceed to code without the operator's pick.
5. If — and only if — the operator decides to tune or picks N=3: paste the §13 brief to CC (AUDIT — CC PAUSES for approval before merge).
6. After any merge: the §6 block is the smoke runbook; a kill-switch PR gets the Task-F live `RISK` probe.

---

## 16. How to publish this handoff

**Path A — operator's one-shot Mac command (preferred; see the chat message that delivered this handoff).** `scp` the file up, then a single `ssh tradeflow 'bash -s' <<'PUBLISH_EOF'` heredoc that branches off origin/main, commits `docs: add v25 handoff (...)`, pushes, opens the PR, POLLS `gh pr checks <branch>` to green (sleep 15, ~10-min cap, fail-fast), then `gh pr merge --squash --admin --delete-branch` and resyncs main. `set -uo pipefail`, never `--watch` (the ruleset requires the check to COMPLETE before merge — `--admin` does not skip an in-progress check).

**Path B — VPS Claude Code brief (fallback):**
```
You are VPS Claude Code on the TradeFlow VPS. A file HANDOFF_v25.md has been scp'd to
/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v25.md. Publish it: branch off origin/main,
git add it, commit -F a /tmp commitmsg file ("docs: add v25 handoff (N=2 concurrency live;
gate confirmed correct; window=1 partial)"), push the branch, gh pr create --body-file a /tmp
body file, poll `gh pr checks <branch>` in a loop (sleep 15) until green — NEVER --watch — then
gh pr merge <branch> --squash --admin --delete-branch, checkout main, pull --ff-only.
Report the merged origin/main commit. (CC bash discipline: no heredocs/$()/;/cd && — use /tmp files.)
```

The handoff exists only once saved to disk and committed. Until then, treat the chat output as draft.

---

*End of handoff v25. Target lifespan: until the operator resolves the window=1 calibration (§7.1 / §13) and either (a) the live single-position-or-N=2 edge clears its go/no-go bar over a real above-trend window, or (b) a recalibrated cluster book passes a forward paper window. Then delete and rely on the source on `main` + whatever v26 captures.*
