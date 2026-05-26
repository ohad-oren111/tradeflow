# TradeFlow — Handoff v8 (Session 9 close: watchdog + 24/5 + strategy realign + protections research)

*Handoff from end of 2026-05-26. **TradeFlow is operational under the post-PR-31 state**: orchestrator + IB Gateway + watchdog running healthy on `5.78.212.37`, paper account `DUQ331660` NetLiq ~$1,000,171 with positions=[] and 0 lifecycles today. PRs #32 (24/5 session expansion + Friday-only EOD + TP-TIF=GTC) and #33 (strategy re-alignment to SeanBot reference + C1 regime gate addendum) are **open on GitHub, CI passing on both, awaiting operator merge**. Do not restart anything before running the §6 verification block.*

---

## 0. How to use this doc

Read sections 0.5, 1, 2, 4, 5 first — that's state-of-the-system as of handoff. Sections 7–13 are reference material. Section 14 is the source-of-truth hierarchy when this handoff disagrees with itself or with a live observation.

**Do not trust this doc alone.** Run §6 verification before writing any code or merging anything. Specifically: confirm both PR #32 and PR #33 are still open and CI-green before merging in either order (whichever merges second rebases).

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended must be a copy-paste-ready bash block. Self-contained, env-sourced inline, expected output described immediately below.

**Learning-delivery discipline.** Every new fact discovered (bug pattern, corrected assumption, environmental fact, diagnostic finding) gets surfaced immediately as a markdown snippet for the running handoff queue.

**Read before diagnosing.** Read full startup log + 3-5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency language, hit live API / DB / raw log file, not aggregated metrics.

**Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise.

### Project-specific carry-forward (cumulative since v1)

- **§0.5.97 — Probe-before-specify.** Brief authors do not have working memory of every project file. Specifying field names, line numbers, or constants without reading the actual file is the #1 cause of CC VPS having to reject + reauthor briefs. Use "VERIFY IN A.X" placeholders.
- **§0.5.98 — Broker/exchange state is ground truth.** For position/fill/capital claims, IBKR API beats internal DB tables.
- **§0.5.103 — CC chained Bash heuristics per-subcommand.** Prefer broad allows + targeted denies in settings.local.json.
- **§0.5.104 — CC meta-safety on `.claude/`.** Reads accepted, writes declined.
- **§0.5.105 — Permission additions are comprehensive sweeps, not iterative patches.**
- **§0.5.117/.118 — Bash discipline (CC hardcoded heuristics).** Triggers: `cd X &&`, `;` separators, `$(...)`, `${VAR}`, heredocs, chained `sleep`. Workarounds: `git -C path`, one Bash call per `;`, Python helpers, Write tool for heredocs, `--body-file /tmp/...md`, `git commit -F /tmp/...txt`.
- **§0.5.142 — Multi-line `python -c` trips CC heuristic.** Stage to `/tmp/<name>.py` via Write tool.
- **§0.5.143 — Telegram `parse_mode=Markdown` is fragile.** Plain text only.
- **§0.5.144 — Secrets audit on every config-file merge.** `~/.tradeflow-secrets/.env` is read-only to VPS CC.
- **§0.5.145 — Live-state claims in handoff or PR description require same-session probe.** Don't transcribe from prior briefs; re-probe.
- **§0.5.147 — Dual-manifest dependency gotcha.** When adding a Python package, update BOTH `requirements.txt` AND `pyproject.toml` if both exist.
- **§0.5.148 — Container loopback ≠ host loopback.** A service bound on `127.0.0.1` inside a container is unreachable from the host unless port-forwarded.
- **§0.5.149 — Bash history expansion on `!`.** Never include `!` unquoted in shell strings.

### **New standing rules from Session 9 (codified here)**

- **§0.5.151 — Docker `(healthy)` ≠ broker API healthy.** Docker healthcheck only checks process liveness. IB Gateway can have its Java process dead while `socat` survives — container reports healthy but the API socket is closed. The watchdog's IB API probe (`ib_async.IB.connect()+reqCurrentTime()`) is the canonical reachability check, NOT docker ps output.

- **§0.5.152 — Module-level `load_dotenv` mutates `os.environ` for the entire Python interpreter.** PR #19 originally called `load_dotenv` at module-import time; this leaked the operator's `DASHBOARD_BIND=0.0.0.0` into pytest's `os.environ` during test collection, breaking 7 pre-existing orchestrator/dashboard tests with "address already in use" on port 8080. **Always defer `load_dotenv` to the entry-point `main()` function**, never module-level.

- **§0.5.153 — Brief authors lack working memory of every project file.** PR #19 required two patch rounds catching 7 chat-side slips because the brief specified details (env var names, file paths, port numbers, Python package names, SDK availability) without probing. **Use "VERIFY IN A.X" placeholders for anything not directly evidenced by reading source.** Task A audit by CC VPS is non-optional even when the brief feels well-researched. PR #31 applied this discipline and shipped clean.

- **§0.5.154 — `find <path>` exits 1 on permission-denied subdirectories**, which kills `set -e pipefail` scripts mid-run. Pattern: `/tmp` has `systemd-private-*` dirs root-owned. Fix: `{ find <path> 2>/dev/null || true; } | wc -l` — swallow stderr, force exit 0. PR #30 hotfix for cleanup script.

- **§0.5.155 (CANDIDATE, not yet codified) — Defensive fail-open gates don't fire if upstream buffer sizes are too small.** PR #33 C1 regime gate `_regime_ok` requires ≥202 30-min bars; the strategy's `_bars` deque has `maxlen=150` 1-min bars → resamples to ~5 30-min bars → C1 *always* fails-open in production. Gate is correctly written defensively but doesn't actually regime-filter anything until a larger history buffer is wired in. See §7 "Gaps to log" for the fix path.

---

## 1. Where we are (as of 2026-05-26 ~21:00 UTC, end of Session 9)

### Live production state

- **VPS**: `tradeflow@5.78.212.37` (Hetzner CX32, Ubuntu 22.04, Ashburn region)
- **`tradeflow-app`**: running, healthy. RestartCount=17 (incremented during today's F-block smoke testing; stable since 18:05 UTC).
- **`tradeflow-ib-gateway`**: running, healthy. Java + socat both alive.
- **Watchdog**: 3 cron entries active (monitor every minute, daily-report Mon-Fri 09:00 ET, cleanup daily 03:00 UTC). State file at `~/.tradeflow-watchdog/state.json` shows alert_history clean, auto_heal_history has 3 entries from today's PR #19 F.5/F.6 smoke testing (ages out by 19:04 UTC tomorrow).
- **IBKR paper**: account `DUQ331660`, NetLiq ~$1,000,171.60, positions=[], 0 lifecycles today, dashboard endpoint `http://localhost:8080` returns HTTP 401 (auth-gated, up).
- **Telegram alerter**: validated end-to-end on operator's phone (PR #19 Task F, PR #31 F.2 confirmed live).

### What just shipped this session

- **PR #19** (`feat(watchdog): external host-side health monitor + auto-heal + daily ping + cleanup`) — squash commit `699c540`. 9 new files (~470 LOC + 44 tests). External cron-driven watchdog on host venv `.watchdog-venv/`, monitors container health + IB API + dashboard + disk + memory + orchestrator restart loop. Auto-heals via `docker restart tradeflow-ib-gateway` up to 3x/60min then escalates to MANUAL INTERVENTION on Telegram. Caught one self-discovered bug mid-implementation (module-level `load_dotenv` leaking → §0.5.152).
- **PR #30** (`fix(cleanup): tolerate find permission errors under set -e pipefail`) — squash `a999c20`. 6-line bash fix to `scripts/tradeflow_cleanup.sh` (the `find /tmp` hit `systemd-private-*` perm-denied dirs → §0.5.154).
- **PR #31** (`fix(watchdog): installer python detection + log dedup + recovery log + alert dedup`) — squash `a0f5c66`. 4 files modified. (a) installer detects `python3.11+` explicitly with ensurepip+venv check (Ubuntu 22.04 default `python3` is 3.10, no ensurepip). (b) TTY-conditional StreamHandler eliminates duplicate log lines. (c) Missing `LOGGER.info("[WATCHDOG] recovery: ib_api_down ...")` added. (d) `app_restart_loop` dedup hardened with `RESTART_LOOP_STABLE_CYCLES_TO_CLEAR=3` (was clearing too eagerly on a single delta=0 cycle).
- **PR #32 (OPEN, CI green)** — `https://github.com/ohad-oren111/tradeflow/pull/32`. Branch `claude/pr32-24x5-session-expansion`, latest commit `e315987`. 7 files modified: `src/strategy.py` (`_in_session_edge_window` rewrite for 24/5 — Sun 18:05 ET → Fri 16:25 ET with daily CME break + gateway-restart no-trade windows), `config/risk_params.py` (RTH-only fields dropped, 24/5 fields added, DST-safe ET conversion of weekend cutoff), `src/execution/force_close.py` (Friday-only EOD schedule at 16:25 ET, was daily 15:58 ET), `src/execution/bracket.py` (TP child `tif="DAY"` → `tif="GTC"` — scope-expansion fix surfaced in Task A.4 as a structural blocker for 24/5), plus 3 test files. 298 passed / 0 failed (275 baseline + 23 net new).
- **PR #33 (OPEN, CI green)** — `https://github.com/ohad-oren111/tradeflow/pull/33`. Branch `feat/pr-33-strategy-realign`, latest commit `44d1254` (after C1 addendum on top of `81c90e1`). 4 files modified. Re-aligns `Sma100BounceStrategy` to SeanBot's `strategy/ma_bounce.py` reference: MA-order inverted (`ma_slow > ma_fast`, was `ma_fast > ma_slow`), touch widened to windowed `[MA100-15, MA100+5]`, `ma_min_gap_pts` 2.0 → 0.5, ADX filter dropped, DECISION_TRACE logging added, C1 regime gate ported from `ma_bounce.py:55-91` with `regime_gate_enabled: bool = True` config flag. 283 passed / 0 failed.

### What we discovered this session (not yet in code)

- **The 85-hour outage** spanning Sunday 2026-05-24 ~10:00 UTC → Tuesday 2026-05-26 14:24:48 UTC. Root cause: IBKR weekly maintenance window killed IB Gateway's Java process inside `tradeflow-ib-gateway` container. `socat` proxy stayed alive. Docker `(healthy)` reported healthy because the docker healthcheck only checks process liveness, not API socket round-trip. `tradeflow-app` orchestrator restart-looped 10,802 times (`Up 9 seconds (healthy)` on every probe) because its IB API connect kept failing. Watchdog was not yet deployed; alerts didn't fire. Discovered ~09:46 ET Tuesday when operator checked. **Fix**: `docker restart tradeflow-ib-gateway` (NOT `docker compose restart`, since service name ≠ container name). Bot operational ~14:24:48 UTC. Codified as §0.5.151.

- **Strategy port divergence vs SeanBot live**: TradeFlow's `Sma100BounceStrategy` had at least 4 entry-side divergences from SeanBot's `strategy/ma_bounce.py`:
  1. MA-order condition INVERTED (`ma_fast > ma_slow` vs SeanBot's `ma100 > ma50`) — the load-bearing bug
  2. Touch condition asymmetric (only upper bound +5 vs SeanBot's windowed `[-15, +5]`)
  3. ADX(14)≥20 filter present in TradeFlow, not in SeanBot
  4. MA gap minimum 2.0 vs SeanBot V3 live config 0.5
  
  Plus a C1 regime gate (`_regime_ok`, 30-min EMA200 level filter, shipped by SeanBot 2026-05-18) was missing entirely. All 5 addressed in PR #33 + C1 addendum.

- **Exit-side divergence (deferred to future PR)**: SeanBot V12 runs a 3-phase trailing exit (`execution/executor.py:382-428`): Phase 1 fixed SL=entry-75, Phase 2 lock-in at +50pt (SL→entry+50), Phase 3 trailing SL=highest-150 (1:2 ratio). TradeFlow has simple fixed bracket SL=entry-75, TP=entry+150. This is why SeanBot's Telegram channel exits as `trail stop +49/+50pt` instead of `take_profit +150pt`.

- **TradeFlow strategy's expected economic profile** (from research synthesis, ChatGPT + Gemini converged):
  - Expected return per trade: ~$13.44 (1.07 PF × 36% WR × 1:2 R:R, $300 risk per 2-ct position)
  - Expected return per year: ~$2,625 on $100K real-money intent (~2.6% before costs)
  - Backtest MaxDD 61% over 3-year window
  - Implied recovery time for 12% drawdown: ~4.6 years at $2,625/yr; for 30% drawdown: ~11.4 years
  - **The strategy's edge is too thin to tolerate deep drawdowns.** Survival controls > alpha-enhancement controls. This is the strategic reframing that drives PR #34+.

- **Watchdog daily report measures by UTC day**, not ET trading day. A position opened Mon 22:00 ET = 02:00 UTC Tue counts in Tuesday's report. Awkward but not broken — no field assumes "flat at start of day". Documented in PR #32 Task A.5.

- **Pre-PR-32 diagnostic probe verdict** (2026-05-26 18:00 UTC): TradeFlow's strategy correctly suppressed today's 12:39 ET and 12:57 ET SeanBot LONG signals on legitimate filter grounds (MAs crossed DOWN per SeanBot's logic, but bearish candles + gap<0.5 + touch out-of-window). No code bug. The MA-order inversion was the load-bearing finding for PR #33.

---

## 2. The session's bug thread

1. **~09:46 ET Tuesday morning**: operator checks the bot. Observation Tuesday was supposed to be the first day of paper-trading observation post-PR #18. Bot didn't fire anything overnight. `docker ps` showed both containers `(healthy)` with `tradeflow-app` `Up 9 seconds`. RestartCount=10,801.

2. **First diagnosis (correct)**: orchestrator is restart-looping. But why? Container is `(healthy)`. Probed via `docker logs tradeflow-app --tail 100`: every cycle showed `[ORCH] ib_connecting → connection refused`. So IB Gateway-side problem, not orchestrator-side.

3. **Second diagnosis (correct)**: IB Gateway Java process died during IBKR's Sunday weekly maintenance window. `docker exec tradeflow-ib-gateway pgrep java` returned nothing. `socat` was alive. Docker healthcheck was passing because it checks process existence in the container, but socat doesn't proxy a dead Java instance.

4. **Fix #1**: `docker restart tradeflow-ib-gateway` triggered IBC re-login. Used `docker restart` not `docker compose restart` because service name ≠ container name. Bot recovered at 14:24:48 UTC.

5. **Outage narrative documented + standing rule §0.5.151 codified.** Watchdog PR #19 brief drafted chat-side.

6. **PR #19 brief authoring**: 7 §0.5.97 violations caught by CC VPS (chat-side specified env var names, port numbers, file paths without probing). Three patch rounds (v1 → v2 → v3) before CC VPS could implement. Lesson: §0.5.153 codified.

7. **PR #19 implementation**: ~470 LOC + 44 tests. CC VPS caught the module-level `load_dotenv` leakage mid-implementation (§0.5.152). PR squashed to `699c540`.

8. **PR #30 cleanup hotfix**: `scripts/tradeflow_cleanup.sh` silently exited due to `find /tmp` hitting `systemd-private-*` perm-denied subdirs under `set -e pipefail`. 6-line fix. §0.5.154 codified.

9. **PR #19 Task F live on operator's phone**: 14 Telegram messages, F.5 alert + auto-heal + RECOVERED ✓, F.6 cap exceeded → MANUAL INTERVENTION ✓.

10. **PR #31 four watchdog follow-ups**: installer Python detection (Ubuntu 22.04's `python3`=3.10 has no ensurepip), TTY-conditional StreamHandler (eliminates duplicate log lines), missing IB recovery LOGGER.info, `app_restart_loop` dedup hardening. 4 files modified, +10 tests. Squashed to `a0f5c66`.

11. **Strategy investigation**: operator's friend's SeanBot Telegram channel showed 8 LONG entries today (6 closed +49-50pt trail stops, ~$1,166 P&L on 2-ct). TradeFlow fired 0. Why?

12. **Diagnostic probe (Phase 1 pre-PR-32)**: CC VPS pulled 1-min MNQM6 bars via `ib_async` clientId=95 around the 12:39 / 12:57 ET SeanBot trigger times. Applied TradeFlow's gates manually. Result: both bars failed multiple gates, including `ma_fast > ma_slow` (the "uptrend" gate). **Strategy worked as written.** But SeanBot's `ma_bounce.py:122` requires `ma100 > ma50` — the OPPOSITE direction. **TradeFlow's port has the MA-order INVERTED.**

13. **SeanBot codebase deep-dive (chat-side)**: operator shared `seanbot-share.zip`. Read `strategy/ma_bounce.py` (183 lines), `settings.py`, `config/settings.py` (V3 live config), `execution/executor.py` (3-phase trailing exit). Confirmed 4 entry-side divergences + 1 missing regime gate + 1 exit-side architecture difference.

14. **PR #32 brief drafted chat-side**: 24/5 expansion. Operator-authorized 8 design defaults (D1-D8). Brief used "VERIFY IN A.X" placeholders for anything not directly evidenced. Sent to CC VPS via diagnostic probe → PR #32 pipeline.

15. **CC VPS PR #32 Task A surfaced bracket TP-TIF blocker**: `src/execution/bracket.py:73` had `tp_child.tif = "DAY"` (STP was already GTC at line 101). Under 24/5, DAY TP expires at end of each CME session → overnight positions lose upside automation. Conflicted with operator design default D5. Operator approved scope expansion to include `bracket.py` fix. CC VPS shipped PR #32 with 7 files modified (5-6 in original brief envelope + bracket.py + its test file). 298 passed.

16. **First read of "remove F1 regime gate" was WRONG**. Operator said friend told him to remove F1. I interpreted as "remove all regime gating" and drafted PR #33 without C1. Chat-side flagged the risk in PR #33 E.1 (SeanBot backtest shows no regime gate → MaxDD >100%). CC VPS shipped PR #33 without regime gate per the brief.

17. **Operator clarified F1/C1 confusion** by re-asking friend: "F1" was the OLD slope-check version (deprecated 2026-05-14); C1 is the current level-check version (shipped 2026-05-18) and SHOULD be on. Friend meant "don't port the old F1, the current C1 is what should ship."

18. **PR #33 C1 addendum**: small follow-up commit `44d1254` on the existing `feat/pr-33-strategy-realign` branch (not a new PR). Ported `_regime_ok` verbatim from `ma_bounce.py:55-91` (adjusted for TradeFlow's `time` column name and `[STRAT]` log prefix), added `regime_gate_enabled: bool = True` config flag, 4 new tests. 283 passed. PR #33 description updated.

19. **C1 production-buffer caveat surfaced (CANDIDATE §0.5.155)**: CC VPS noted in PR #33 E.1 that the strategy's `_bars` deque has `maxlen=150` 1-min bars → resamples to ~5 30-min bars → far below C1's 202-bar warmup threshold → C1 fails-open in production. Gate is defensively correct but doesn't fire. Tracked as gap; fix is a larger history buffer (PR #34+ candidate).

20. **Round 2 deep research** (operator ran in parallel): two questions tuned for ChatGPT and Gemini deep research, asking about account-protection mechanisms post-PR-32+33. Strong convergence on (1) daily loss kill switch, (2) correlation throttle, (3) news blackout. Divergence on 3-phase trailing exit (ChatGPT skip / Gemini ship). Synthesis below in §7.

21. **HANDOFF_v8 drafted** (this document).

---

## 3. What the system is actually made of

**Single source of truth**: TradeFlow has no canonical system map file yet. This handoff is the best available system doc until one ships (low-priority follow-up).

### Production-live code paths

- **Entry**: `src/orchestrator.py` (asyncio main, instantiates `IbClient`, `OrderRouter`, `EodForceClose`, `Sma100BounceStrategy`, dashboard FastAPI on `0.0.0.0:8080`)
- **Strategy**: `src/strategy.py:Sma100BounceStrategy` — buffer of 150 1-min bars, `detect_signal` evaluates gates per bar (post-PR-33: C1 regime → ma_order → touch → bullish → gap → DECISION_TRACE on block)
- **Execution**: `src/execution/router.py` → `src/execution/bracket.py` (parent MKT entry + GTC SL + GTC TP, both legs now GTC after PR #32) → `src/execution/force_close.py` (EOD: Friday 16:25 ET only after PR #32)
- **Persistence**: Supabase `lifecycles` + `lifecycle_events` tables
- **Comms**: `comms/telegram_alerter.py` (plain text, no parse_mode — §0.5.143)
- **Dashboard**: `dashboard/main.py` FastAPI, auth-gated, returns HTTP 401 to unauth probes (this is how watchdog confirms it's up)

### Watchdog code paths (PR #19 + #30 + #31)

- **Entry**: `scripts/tradeflow_watchdog.py` (3 modes: monitor / daily-report / self-test)
- **Host venv**: `~/tradeflow/.watchdog-venv/` (Python 3.11.15, separate from `.venv/`)
- **Cron entries**: 3 active (managed by `scripts/watchdog_crontab.template` via `scripts/install_watchdog.sh`)
- **State**: `~/.tradeflow-watchdog/state.json` (alert_history, auto_heal_history, last_restart_counts, restart_loop_stable_cycles)
- **Log**: `~/.tradeflow-watchdog/watchdog.log` (rotating, 14 backups)
- **Cleanup**: `scripts/tradeflow_cleanup.sh` daily 03:00 UTC (`/tmp` + old docker images + log rotation)

### Dead/phantom surfaces to know about

- `requirements-watchdog.txt` exists separately from `requirements.txt` (watchdog deps isolated to host venv)
- `src/indicators.py:add_adx` / `add_atr` are computed every bar by `add_all_indicators` but NOT READ by the strategy after PR #33 (ADX filter dropped). Kept because `tests/test_indicators.py` still exercises them. Could be removed in a future cleanup PR if anyone cares.

### Open documented bugs / gaps (track by ID — referenced in §7)

- **G1**: C1 regime gate fails-open in production due to buffer size mismatch (150 1-min bars → 5 30-min bars, vs 202 required). Fix: larger history buffer.
- **G2**: `config/risk_params.py:signal_scan_start_et` field has misleading comment "5min after open for SMA warmup" — the actual SMA warmup is 100 bars (~100 min), this field is just the session-edge filter. Doc-only fix.
- **G3**: Seed depth is 45 bars at startup; strategy needs 100 for SMA100 + ~200 30-min for C1. Today's outage cost ~9 min of trading window because of seed shortfall stacking on top of restart timing. Tiny tactical fix.

---

## 4. Verified facts about TradeFlow internals (2026-05-26)

**DO NOT challenge these unless re-verified against live system.**

- **VPS**: `tradeflow@5.78.212.37`. Repo at `~/tradeflow`. Secrets dir `~/.tradeflow-secrets/.env` (chmod 600, never edited by CC VPS).
- **Container ports**: `tradeflow-app` → `127.0.0.1:8080` (dashboard). `tradeflow-ib-gateway` → `127.0.0.1:4002` (IB API socket; internal container port is 4004).
- **IBKR paper account**: `DUQxxxxx` (NOT `DUO891961` — corrected Session 3). NetLiq ~$1M.
- **ib_async** library (active fork of deprecated `ib_insync`). Server version 178.
- **MNQM6 contract**: June 2026 expiry, 3rd Friday = 2026-06-19. Roll typically ~8 days before = ~June 11. Beyond this handoff's lifespan but worth noting.
- **MNQ contract spec**: TICK_SIZE=0.25 idx points, MULTIPLIER=$2/point ($0.50/tick), COMMISSION_RT=$0.62 RT (friend's tier), MARGIN_REQ=$2,000 day-trade (friend's tier), CME maintenance ~$3,636.
- **CME session**: Sun 18:00 ET → Fri 17:00 ET, with daily 17:00-18:00 ET maintenance break (Mon-Thu).
- **TradeFlow session post-PR-32**: Sun 18:05 ET → Fri 16:25 ET, with no-trade windows for daily break (16:55-18:05 ET ±5-min edges), gateway restart (23:45-00:15 ET), and weekend pre-cutoff (Fri 16:25-16:30 ET edge).
- **clientId reservations**: 1 orchestrator, 2 secondary orchestrator, 95 probes (free, used for diagnostic), 96 watchdog (reserved by `IBKR_CLIENT_ID_WATCHDOG=96`), 98 healthcheck, 99 smoke tests.
- **Bar buffer**: `_BAR_BUFFER_MAX = 150` 1-min bars per strategy instance.
- **Strategy params** (post-PR-33 defaults in `config/risk_params.py`):
  - `ma_fast=50, ma_slow=100` (1-min bars)
  - `ma_touch_buffer_pts=5.0` (upper bound; lower bound `-15.0` hardcoded as `_TOUCH_LOWER_BAND_PTS` in `src/strategy.py`)
  - `ma_min_gap_pts=0.5` (was 2.0 pre-PR-33)
  - `stop_loss_pts=75.0, take_profit_pts=150.0`
  - `cooldown_bars=10`
  - `contracts_per_trade=2, max_simultaneous_positions=5`
  - `session_edge_no_trade_minutes=5`
  - `regime_gate_enabled=True` (C1 gate, but fails-open in production per G1)
- **EOD force-close** (post-PR-32): Friday 16:25 ET only (was daily 15:58 ET). Owner: `src/execution/force_close.py:EodForceClose`.
- **Bracket order TIF** (post-PR-32): BOTH parent + SL + TP children are GTC. Was: TP child = DAY (latent bug for 24/5).
- **Watchdog Python**: requires 3.11+. Ubuntu 22.04's default `python3` is 3.10 (no ensurepip). Installer detects `python3.13` → `python3.12` → `python3.11` in descending preference and skips bare `python3` (PR #31 Fix A).
- **Test invocation**: `~/tradeflow/.venv/bin/python -m pytest` (NOT `docker exec`). Container does not have pytest.
- **CI duration**: ~40-45s for the full suite (~283 tests as of PR #33 + C1).
- **Strategy expected economic profile** (research-synthesized, not measured live yet):
  - Expected return: ~$13.44 per trade × ~195 trades/year ≈ +$2,625/year on $100K
  - 95th-percentile longest losing streak (binomial @ 64% loss-rate over 586 trades): ~21 consecutive losses → 21 × $300 = $6,300 frictional drawdown
  - 1 cluster of 5 simultaneous stop-outs = 1.5% of $100K = $1,500 worst-case before slippage
  - Implied recovery time for 12% drawdown: ~4.6 years at expected pace

**New load-bearing facts (this session)**:
- §0.5.151 Docker `(healthy)` ≠ broker API healthy
- §0.5.152 Module-level `load_dotenv` leaks `os.environ` to pytest
- §0.5.154 `find` perm-denied + `set -e pipefail` = silent exit
- Bracket TP was tif="DAY" pre-PR-32 (now GTC)
- SeanBot's `ma_bounce.py:122` uses `ma100 > ma50`, opposite of TradeFlow's pre-PR-33 port
- C1 regime gate ports verbatim but fails-open with 150-bar buffer (G1)

---

## 5. Wrong diagnoses — READ BEFORE YOU DEBUG

This session had four explicit wrong turns. Each documented.

### W1 — "Container is `(healthy)`, the orchestrator must be the problem"

- **Evidence**: `docker ps` showed `tradeflow-ib-gateway` with `(healthy)` status. `tradeflow-app` was restart-looping. Naive interpretation: IB Gateway is fine, app has a bug.
- **Why it was wrong**: Docker healthcheck only checks process liveness inside the container. `socat` was alive (process check passed) but the Java process serving the IB API was dead. The API socket was closed. Healthy status was a false positive.
- **Correct diagnosis**: probe the IB API directly via `ib_async.IB.connect()+reqCurrentTime()`. That's what the watchdog now does. Codified as §0.5.151.

### W2 — "PR brief author can specify field names from memory"

- **Evidence**: PR #19 brief v1 specified env var names, port numbers, file paths confidently. CC VPS Task A audit found 7 mismatches.
- **Why it was wrong**: chat-side me doesn't have working memory of every file in TradeFlow. Even when I'd read a file in a prior session, that's stale. Specifying details from memory is one form of confabulation.
- **Correct discipline**: use "VERIFY IN A.X" placeholders for anything not directly evidenced by source. PR #31 applied this and shipped clean on first pass. Codified as §0.5.153.

### W3 — "Remove F1 regime gate" means "remove all regime gating"

- **Evidence**: Operator's friend said "remove the F1 regime gate." The code calls it "F1" in a stale inline comment at `ma_bounce.py:100` even though the function docstring says "C1".
- **Why it was wrong**: I interpreted as "remove all regime gating." Drafted PR #33 without porting any regime gate. CC VPS shipped that version. Operator then re-asked friend, who clarified: F1 was the OLD slope-check (deprecated 2026-05-14), C1 is the current level-check that REPLACED F1, and "F1" was just shorthand for "the regime-gate slot." Friend wanted "skip the old F1 logic, the current C1 IS the regime gate."
- **Correct action**: shipped a C1 addendum on the existing PR #33 branch. Strategy now matches SeanBot's live state.

### W4 — "C1 gate will fire in production"

- **Evidence**: ported `_regime_ok` verbatim from `ma_bounce.py:55-91`. Includes fail-open semantics for <202 30-min bars.
- **Why it was wrong** (caught by CC VPS at the end of PR #33 C1 addendum): the strategy's `_bars` deque has `maxlen=150` 1-min bars. That resamples to ~5 30-min bars. The 202-bar warmup threshold is never reached in production with that buffer size. Gate fails-open permanently in production.
- **Correct fix path**: G1 in §7 — wire in a larger history buffer (could be a separate ring buffer or a periodic IB historical-data fetch on startup). PR #34+ candidate; not blocking PR #33 merge because the gate's defensive failure mode is safe (allows entries, doesn't break the strategy).

**Lesson for next session (meta-pattern)**: every wrong diagnosis this session came from the same root — "I think I know what's there, I don't need to re-read." Containers, friend-language, port semantics, buffer sizes. The §0.5.97 / §0.5.153 / §0.5.155 family is the prophylaxis: probe before specifying, even when the answer feels obvious.

---

## 6. Verification block — run this before doing anything

**V0 — Confirm git HEAD on `main`**:

```bash
ssh tradeflow@5.78.212.37
git -C ~/tradeflow fetch
git -C ~/tradeflow log -1 --oneline origin/main
```

Expected: `a0f5c66 fix(watchdog): installer python detection + log dedup + recovery log + alert dedup (PR #31) (#31)` — IF PR #32 and PR #33 haven't merged yet.

After PR #32 + PR #33 merge: HEAD will show the most recent squash commit (whichever merged second).

**V1 — Confirm both PRs are still open**:

```bash
gh pr list --state open --repo ohad-oren111/tradeflow
```

Expected: PR #32 and PR #33 both listed. CI: green on both. If either was already merged, the §15 next-action sequence shifts.

**V2 — Confirm containers + watchdog are healthy**:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.RunningFor}}'
```

Expected:
```
NAMES                       STATUS                     RUNNING FOR
tradeflow-app               Up X minutes (healthy)     ...
tradeflow-ib-gateway        Up X minutes (healthy)     ...
```

Both healthy. If `tradeflow-app` shows recent RestartCount changes via `docker inspect tradeflow-app --format '{{.RestartCount}}'`, investigate.

```bash
crontab -l | grep tradeflow-watchdog | wc -l
```

Expected: `3` (monitor + daily-report + cleanup). If 0, watchdog is uninstalled and PR #19 has been reverted. If 1 or 2, partial install — re-run `bash ~/tradeflow/scripts/install_watchdog.sh`.

**V3 — Confirm IBKR connectivity** (the §0.5.151 / G1 truth check):

```bash
~/tradeflow/.watchdog-venv/bin/python ~/tradeflow/scripts/tradeflow_watchdog.py --mode=self-test 2>&1 | tail -20
```

Expected: ends with `[WATCHDOG] self_test: completed`. A Telegram message arrives on operator's phone within 15s confirming.

If self-test fails on IB API probe → check `docker logs tradeflow-ib-gateway --tail 50` for IBC login errors; consider `docker restart tradeflow-ib-gateway`.

**V4 — Confirm baseline test pass**:

```bash
cd ~/tradeflow && .venv/bin/python -m pytest tests/ --tb=no -q 2>&1 | tail -5
```

Expected: `275 passed` IF PR #32 + #33 not merged. After both merge: ~298 passed (PR #32 adds 23 net) → ~302 passed (PR #33 + C1 adds 4 more on top, after rebasing whichever merged second).

Any deviation: STOP, investigate before any code changes.

**V5 — Confirm IBKR account state**:

```bash
ssh tradeflow@5.78.212.37 'docker logs tradeflow-app --since 10m | grep -E "DUQ|NetLiq|positions" | tail -5'
```

Expected: `DUQ331660`, NetLiq ~$1,000,171, positions count matches current state. If account number differs, configuration drift — STOP and reconcile.

---

## 7. Pending work queue

### **PR #34 — Daily Loss Kill Switch** (highest priority; convergent research recommendation)

**Status**: planned, brief drafted in §13 below.

**Scope**: 4-5 files. Implement layered equity kill switch per ChatGPT + Gemini convergent recommendation. Parameter set leans ChatGPT (tighter caps) given operator's 25-30% pain threshold and strategy's ~$2,625/year expected edge:

- Daily cap: -1.5% of reference equity (=$1,500 on $100K). Auto-reset next trading session.
- Weekly cap: -3.0%. Auto-reset Sun session open.
- Monthly cap: -6.0%. Manual reset required.
- Trailing account drawdown cap: -12.0% from high-water mark. Manual reset + auto-switch to paper mode.

**On breach**: flatten positions via market order, cancel resting orders, set `app_level_halt=True`, send Telegram. Daily/weekly auto-reset on schedule; monthly + trailing-DD breaches require manual `/unhalt`.

**Why this first**: cheapest insurance against widest failure mode. Both researchers ranked #1. Mirrors FTMO / Topstep / Apex prop-firm conventions.

**Reference equity caveat**: paper account is $1M; live intent is $100K. **Hard-code reference at $100,000 during paper validation** so percentage caps test against the live-equivalent value, not the inflated paper NAV. If we use the paper NAV, all caps will be 10× too loose in shadow mode.

**Estimated**: ~200-300 LOC + ~50-100 test LOC.

### **PR #35 — Correlation throttle** (convergent #2)

**Status**: planned.

**Scope**: max 2 new positions opened in any rolling 30-minute window. ChatGPT spec; Gemini's 15-min is also acceptable but 30-min gives more spacing for the strategy's natural cadence. Open-stop-risk cap of 0.6% of reference equity (= max ~$600 open risk) — caps the worst-case clustered stop-out at 60% of pre-PR-35 worst case ($1,500 → $600).

**Why second**: addresses cluster risk, which both researchers identified as the likely reason the 61% backtest MaxDD is so much worse than naïve i.i.d. math predicts.

**Estimated**: ~100-150 LOC + tests.

### **PR #36 — News-event blackout** (convergent #3)

**Status**: planned.

**Scope**: hard-coded calendar of tier-one US macro events — CPI, NFP, Core PCE, FOMC statement + press conference. No new entries from T-30 min to T+15 min. If position open at T-5 min, flatten (FOMC + NFP + CPI; not Core PCE which is lower variance).

**Why third**: high impact / low engineering cost. Removes exposure during the few minutes when the underlying process is least stable and slippage is worst.

**Estimated**: ~150-200 LOC + a curated calendar (start with hardcoded; later wire to an econ-calendar API).

### **PR #37 — 3-phase trailing exit** (DEFERRED — divergent research)

**Status**: deferred pending live data.

**Why deferred**: ChatGPT explicitly recommended SKIPPING this for TradeFlow. Argument: TradeFlow is a pullback/bounce (mean-reversion-ish) strategy, not a trend-follower. Trailing stops typically hurt mean-reversion entries by ejecting trades before reversion completes. Gemini recommended SHIPPING this as PR #4 with a different argument (truncates full-loss reversals).

Both arguments have merit. Resolution: ship PR #34-36, observe 30 days of live trading data with fixed bracket, then re-evaluate whether trail would improve or degrade expected return. Don't ship trail-exit without empirical evidence.

### **PR #20 (low priority) — Seed depth fix** (Gap G3)

**Status**: backlog.

**Scope**: orchestrator startup seeds 45 bars; strategy needs 100 for SMA100 + 200+ 30-min bars for C1 regime gate. Bump seed depth to 150-200 1-min bars + add a separate 30-min ring buffer with ~250-bar history fetched from IB at startup.

**Why low priority**: today's outage cost ~9 min of trading window due to seed shortfall stacking on restart timing. Tactical optimization. Could ship anytime.

### **PR (doc-only) — Fix risk_params.py misleading comment** (Gap G2)

**Status**: backlog.

**Scope**: `config/risk_params.py:signal_scan_start_et` field has comment "5min after open for SMA warmup". That field is actually just the session-edge filter; the SMA warmup is 100 bars (~100 min). The comment misled chat-side me during the strategy investigation. Correct the comment, possibly rename the field. Pure doc / cosmetic change.

**Estimated**: 5-10 lines.

### **Open gaps tracked by ID**

- **G1** — C1 regime gate fails-open in production due to bar-buffer size mismatch (150 1-min bars → 5 30-min bars vs 202 required). Fix is bundled with PR #20 seed-depth work. Until then: regime gate doesn't actually filter in production — strategy fires LONG entries even in downtrends. This is partly mitigated by the other PR-33 corrections (correct MA-order, narrow touch, MA gap 0.5) but the explicit downtrend filter is absent.

- **G2** — `risk_params.py:signal_scan_start_et` misleading comment. Doc-only PR.

- **G3** — Seed depth 45 < required warmup 100. PR #20 candidate.

### Uncommitted files / operational debt

- Today's `/tmp` artifacts (probe scripts, commit messages, PR body files) will get cleaned by the daily cron at 03:00 UTC.
- `state.json` retains 3 auto-heal entries from PR #19 F.5/F.6 smoke testing — ages out by 19:04 UTC 2026-05-27.

### Operational cleanup eventually

- Remove `src/indicators.py:add_adx`/`add_atr` once `tests/test_indicators.py` is updated to no longer exercise them. Low priority.
- Add a canonical system-map file at `docs/architecture/system.md`. Currently the best system doc is this handoff.

---

## 8. Test safety — why we belabor this

Carry-forward from prior handoffs, plus session-9 additions:

1. **Fresh `MagicMock()` per test, never shared** — shared mock state across tests was responsible for at least 3 flaky-test debugging detours in prior sessions.
2. **`side_effect` lists need explicit count comments** — silent `StopIteration` is the #1 silent failure in this repo.
3. **No `patch()` on module-level factories; patch at the import site** — `patch('src.strategy.detect_signal')` works; `patch('detect_signal')` does not.
4. **Mock at the wrapper level, not the raw library chain** — `self._db.upsert` not `supabase.table().upsert().execute()`. The wrapper changes; the raw chain breaks tests.
5. **No `@pytest.mark.asyncio` decorators** — TradeFlow uses `asyncio_mode=auto` per `pyproject.toml`. The decorator does nothing and creates a false impression of async-awareness.
6. **Time-dependent tests use explicit constructed timestamps**, not `datetime.now(UTC)`-relative. Reproducibility.
7. **Logger handler state reset in fixture between tests that touch `configure_logging`** — handlers persist across tests; PR #31 added `clean_logger` fixture for this.
8. **For `caplog`-based assertions, scope to a specific logger** — `caplog.at_level(logging.DEBUG, logger="src.strategy")`. Module-level captures get flaky from interleaved logs.

**New from session 9**:

9. **For tests that touch `configure_logging`, redirect ALL state paths in the fixture** — `STATE_DIR`, `STATE_FILE`, AND `LOG_FILE`. PR #31's first CI run failed because `tmp_state` redirected the first two but not `LOG_FILE`; `TimedRotatingFileHandler` then tried to open `/home/runner/.tradeflow-watchdog/watchdog.log` which doesn't exist on CI.

---

## 9. Pitfalls from prior sessions

- "Docker `(healthy)` means the container is fine" — **WRONG.** It means the process inside is alive, not that the API socket is reachable. §0.5.151.
- "I read this file last session, I remember the field name" — **WRONG.** Re-read or use `grep` before specifying. §0.5.153.
- "The regime gate is on because I ported the function" — **WRONG.** Defensive fail-open gates don't fire if upstream buffer sizes are wrong. §0.5.155 candidate / G1.
- "Whatever the friend said, just do it literally" — **WRONG.** "Remove F1" was shorthand. Re-ask if ambiguous. W3 today.
- "Backtest MaxDD = expected live MaxDD" — **WRONG framing.** Live is non-ergodic; single chronological path can hit absorbing-state ruin even if ensemble expectation is positive. ChatGPT + Gemini both made this point.
- Handoff number claims — re-verify. The 275-test baseline at `a0f5c66` is verified; the 283 after C1 addendum is verified. After PR #32 + PR #33 merge, **re-baseline** via V4 before quoting numbers.

**Next session rule**: if a claim is quantitative — row counts, lifecycle counts, P&L, position count, MaxDD percentage, expected return — re-verify. Source-of-truth hierarchy in §14.

---

## 10. Session discipline lesson (2026-05-26)

**The meta-pattern from this session**: every wrong diagnosis came from "I think I know what's there." That's confabulation, not knowledge. The §0.5.97 / §0.5.153 family is the prophylaxis: probe before specifying.

**Specific wins**:
- PR #19 brief required 3 patch rounds because chat-side me specified details from memory. PR #31 brief used VERIFY-IN-A.X placeholders and shipped clean on first pass. **The discipline works when applied.**
- CC VPS catching the bracket TP-TIF=DAY blocker during PR #32 Task A.4 — that's the audit phase paying off. Without the audit, the brief's design default D5 ("brackets persist") would have shipped with a latent bug.
- CC VPS catching the C1 production-buffer caveat (G1) — that's *the implementer* doing the probe-before-claim work even after both authors (chat-side me and the operator's friend) had moved on.

**Enforcement rules for next session**:

1. **Probe before specifying anything quantitative.** Field names, line numbers, port numbers, env var names, constants — read the file or grep. If you can't read, use "VERIFY IN A.X".
2. **Re-ask the operator if friend-language is ambiguous.** "Remove F1" had two valid interpretations. The 30 seconds of clarification saved a wasted PR round.
3. **Defensive fail-open gates need to be probed for actual firing.** Writing the function ≠ the function works in production. C1 was correct code with wrong production behavior because of upstream buffer.
4. **For research synthesis, look for convergence first.** ChatGPT + Gemini both ranking daily kill switch #1 is strong signal. The divergence on trailing exit needs more data, not more research.

---

## 11. Logging verbosity — what to demand from any new code

- Every order placement, modification, or cancel logs `[ROUTER] symbol: action — reason` at INFO
- Every state transition logs `old → new` at INFO with the lifecycle_id
- Every strategy decision logs at INFO on signal fire, DEBUG on block (DECISION_TRACE format from PR #33 — gate-by-gate result with values)
- Every swallowed exception logs the specific error + the function's input context
- Every retry loop logs attempt number + reason
- Every async task logs entry AND exit (PR #19 watchdog established this pattern; carry forward)
- Every dedup / select-one-of-many decision logs which row won and why
- **Watchdog-specific**: every monitor cycle logs `[WATCHDOG] probe_X: ok|fail — detail` for each of the 8 probes; recovery transitions log `[WATCHDOG] recovery: X — sending Telegram message` (this exact format added in PR #31 Fix C).

---

## 12. Master template — use for every Claude Code PR

See `code-pr-brief` skill or the brief inline at §13. Enforces:
- Patch constraints (exact file count + protected list)
- Code quality (black + ruff + type hints)
- Test safety guardrails (the 9 rules from §8)
- Known gotchas (cumulative §0.5 carry-forward)
- "What I got wrong during this PR" section (every brief, always)
- VERIFY IN A.X placeholders for unverified specifics (§0.5.153)

---

## 13. Current PR brief in flight — PR #34 Daily Loss Kill Switch

The convergent recommendation from Round 2 research is to ship PR #34 next. Below is the draft brief — paste into CC VPS once PR #32 and PR #33 are merged.

~~~markdown
# TradeFlow — Claude Code PR Prompt: PR #34 — Daily Loss Kill Switch (account-level layered drawdown halt)

**Brief author discipline note** (§0.5.153): uses "VERIFY IN A.X" placeholders for everything not directly evidenced. Resolve in Task A before implementing.

**Coordination**: PR #32 and PR #33 must be merged to `main` before this PR branches. This PR touches `src/orchestrator.py`, `config/risk_params.py`, and a new module `src/risk/kill_switch.py` (or similar — VERIFY IN A.2 the operator's preferred location).

## Role

You are a senior Python developer working on TradeFlow, an autonomous MNQ futures trading bot running paper on Interactive Brokers via Hetzner VPS with real-money intent at $100,000. The strategy has an expected return of approximately +$2,625/year (1.07 PF × 36% WR) and a backtest MaxDD of 61%. The operator's tolerance for live drawdown is 25-30%. **This PR exists because the strategy's edge is too thin to tolerate deep drawdowns**: a 12% drawdown takes ~4.6 years to recover at the expected pace, a 30% drawdown takes ~11.4 years. Survival controls outweigh alpha-enhancement controls.

You write clean, tested, production-grade code. You probe before specifying. You apply §0.5.153 throughout.

## Context

ChatGPT + Gemini Round 2 deep research both ranked "daily loss cap kill switch" as the #1 protection to add post-PR-32+33. Both cited prop-firm conventions (FTMO 5% daily, Topstep 3% daily, Apex 1.75% daily, FundedNext 5% daily). ChatGPT recommended a 4-layer system (1.5% daily / 3% weekly / 6% monthly / 12% trailing-account). Gemini recommended 2.5% daily as a single layer. Synthesis: ship the 4-layer system; the layers cost ~$50 of LOC to add once the underlying machinery exists.

**Reference equity**: paper account NAV is ~$1M; live intent is $100K. **Hard-code reference equity to $100,000 during paper validation** so percentage caps are tested against live-equivalent values. Wire as `risk_params.reference_equity_usd: int = 100_000`.

## 🏗️ System Architecture & Recent Learnings

[Carry-forward from HANDOFF_v8 §3-§4. Container layout unchanged. Watchdog handles uptime; kill switch handles drawdown.]

### Key Architecture Constraints

- **Constraint 1**: kill switch sits ABOVE strategy. A strategy bug must NOT bypass it. Check kill-switch state in the orchestrator's hot path before forwarding any new signal to the router.
- **Constraint 2**: equity calculation INCLUDES open floating P&L, not just realized P&L. Open positions count toward the daily loss. (FTMO + Topstep + FundedNext all enforce this convention.)
- **Constraint 3**: on breach, flatten + cancel + halt is atomic. Race conditions where a new signal fires between breach detection and halt activation must be impossible.
- **Constraint 4**: manual reset for monthly + trailing-DD breaches via `/unhalt` Telegram command. Daily + weekly auto-reset on schedule.
- **Constraint 5**: kill switch state survives orchestrator restart. State persisted to Supabase or local JSON; orchestrator restores on startup.

## 📏 Engineering Standards (Strict)

### Patch Constraints

Files modified (EXACTLY 4-5, depending on A.2):

1. `config/risk_params.py` — 5 new fields (`reference_equity_usd`, `daily_loss_cap_pct`, `weekly_loss_cap_pct`, `monthly_loss_cap_pct`, `trailing_dd_cap_pct`) + `kill_switch_enabled: bool = True`
2. `src/risk/kill_switch.py` (NEW, path VERIFY IN A.2) — the state machine + breach detection + flatten/cancel/halt action
3. `src/orchestrator.py` — wire kill-switch check before signal forward + restore state on startup
4. `tests/test_kill_switch.py` (NEW) — exhaustive tests for each layer + state persistence + race conditions
5. (Optional) `comms/telegram_alerter.py` — handler for `/unhalt` command (if not already present per A.3)

Files MUST NOT modify: `src/strategy.py`, `src/execution/`, watchdog code, dashboard, docker-compose, dockerfile.

### Code Quality
- Black + ruff clean
- Type hints preserved
- No new dependencies
- Logging format `[KILLSWITCH] layer: action — detail`

### Safety
- All pre-existing tests still pass (V4 baseline)
- Kill-switch state persistence is atomic (no half-written state)
- Idempotent on restart (re-applying the same equity calc shouldn't double-fire)

## 🧩 Current Mission: ship a 4-layer equity drawdown kill switch with automatic and manual reset semantics

### Objective

Add a halt mechanism that fires on any of 4 thresholds and halts new entries + flattens positions + cancels resting orders.

### Task A: Audit

A.1 — confirm 302-passed baseline (or wherever V4 lands after PR #32 + #33 merge + rebase)

A.2 — locate canonical place for new risk module:
```bash
ls src/risk/ 2>/dev/null
find src/ -name 'kill_switch*' -o -name 'risk_*' 2>/dev/null
```
If `src/risk/` exists, use `src/risk/kill_switch.py`. Else, decide between `src/risk/` (preferred, mirrors SeanBot) or `src/kill_switch.py` (flatter).

A.3 — does Telegram alerter handle commands? If not, manual reset goes through orchestrator's `/halt` mechanism (already present per memory):
```bash
grep -n 'halt\|unhalt\|handle_command' comms/telegram_alerter.py src/orchestrator.py
```

A.4 — equity calculation source of truth:
```bash
grep -nE 'NetLiq|equity|fetch_account|get_account' src/clients/ib_client.py src/orchestrator.py
```
Determine whether equity is pulled per-tick or per-bar. Kill switch should pull per-cycle minimum; per-tick is ideal but costs an extra IB API call.

A.5 — Supabase persistence for state:
```bash
grep -rn 'lifecycles\|kill_switch_state\|halt_state' supabase/migrations/ src/
```
Decide: persist kill-switch state to a new `kill_switch_state` table (1 row, current values) or to local JSON in `/var/lib/tradeflow/`. Prefer Supabase for cross-restart durability + dashboard visibility.

A.6 — high-water mark calculation: equity peak since account inception, OR rolling N-day high? Per ChatGPT: peak since inception (Topstep convention). Confirm.

### Task B: Implement

B.1 — `config/risk_params.py` additions:
```python
reference_equity_usd: int = 100_000  # hard-coded; live-equivalent during paper validation
kill_switch_enabled: bool = True
daily_loss_cap_pct: float = 1.5
weekly_loss_cap_pct: float = 3.0
monthly_loss_cap_pct: float = 6.0
trailing_dd_cap_pct: float = 12.0
```

B.2 — `src/risk/kill_switch.py` (or VERIFY-IN-A.2 path): state machine with 4 reset behaviors. Per-cycle entry point `check_and_halt(current_equity_usd: float) -> KillSwitchVerdict` returning {OK, BREACH_DAILY, BREACH_WEEKLY, BREACH_MONTHLY, BREACH_TRAILING}. On any BREACH_*: flatten + cancel + halt.

B.3 — wire into `src/orchestrator.py`'s signal forward path:
```python
if not self._kill_switch.check_and_halt(self._current_equity_usd).is_ok():
    LOGGER.warning("[ORCH] kill switch active — signal suppressed")
    return  # do not forward to router
```

B.4 — restore on startup. Read persisted state (last_reset_at, daily_pnl, weekly_pnl, monthly_pnl, hwm) from Supabase or local JSON; reconcile against current account NetLiq. Don't auto-clear breaches on restart — operator must `/unhalt`.

B.5 — `/unhalt` command handler. Clears the active breach + resets the appropriate counter. Audit-log to Supabase (`kill_switch_resets` table or append to existing audit table).

B.6 — Telegram alert on every breach: `[KILLSWITCH] BREACH_<LAYER>: equity=$X (cap=Y% from reference $Z) — flattened N positions, cancelled M orders, awaiting /unhalt`.

### Task C: Tests

Exhaustive layer-by-layer + state-machine tests. 30+ test cases expected. TEST SAFETY GUARDRAILS per §8 of handoff.

Specific cases:
- Each layer breaches independently (no interaction)
- Multiple layers breach simultaneously → flatten fires once
- Open floating P&L counts toward daily breach
- High-water-mark advances on equity gain; never retreats
- Restart preserves state (write → read → assertions match)
- Manual `/unhalt` clears only the active breach
- Auto-reset semantics for daily (session boundary) + weekly (Sun open) are correct across DST transitions

### Task D: Verify

Standard grep gates + protected-paths check. CI ~50s expected.

### Task E: Out-of-scope investigation
- Whether high-water mark should be set MANUALLY by operator on real-money cutover (the paper account's $1M peak isn't a meaningful HWM for the live $100K account)
- Whether to add a slippage-aware buffer (cap breaches at the threshold minus expected slippage to avoid breach-but-not-quite scenarios)
- Whether a "soft halt" pre-state (warn at 75% of breach) is worth adding

### Task F: Post-merge smoke test
Standard pattern. Smoke test verifies state file is written correctly + breaches fire on synthetic equity values + `/unhalt` resets.

## 📤 Expected Output

Files modified: 4-5. ~350-500 LOC total. ~80-150 LOC tests.

## 🔍 Pre-Push Checklist

Standard. Plus:
- [ ] Reference equity is $100K, not paper account NAV
- [ ] State persistence is atomic (use Supabase transaction or atomic JSON write)
- [ ] `/unhalt` only resets active breaches; doesn't clear historical audit log

## ⚠️ Known Gotchas

[Carry-forward from HANDOFF_v8 §0.5 + project-specific.]

Specific to PR #34:
- Daily reset boundary is CME session boundary (Sun 18:00 ET / nightly 18:00 ET), not calendar midnight
- Weekly reset is Sunday 18:00 ET (CME session open)
- Monthly + trailing-DD do NOT auto-reset — operator MUST `/unhalt`

~~~

---

## 14. Canonical references (in order of authority)

1. **TradeFlow `main` branch source** at the post-PR-32+PR-33 squash commit (TBD after merge) — what actually runs in `tradeflow-app` container
2. **Production VPS state** via SSH + `docker ps` + `docker logs` + `~/tradeflow/.venv/bin/python -m pytest` — truth for runtime
3. **IBKR API state** via `ib_async` clientId=95 from VPS host — truth for account balance, positions, open orders
4. **Supabase `lifecycles` + `lifecycle_events` tables** — truth for trade history (queried via service role; un-paginated queries silently cap at 1000 rows, paginate for full counts)
5. **PR #32 + PR #33 descriptions on GitHub** — authoritative for what each PR changed and why (CC VPS-written, contains Task A audit findings)
6. **SeanBot reference codebase** (operator's local `/home/claude/strategy/ma_bounce.py` + `config/settings.py` + `execution/executor.py`) — reference strategy spec for any future PR aligning to SeanBot's live behavior
7. **Round 2 research outputs** (ChatGPT + Gemini deep research outputs, summarized in §7) — strategic priority for PR #34+
8. **This handoff (v8)** — session context for next chat, NOT long-term authority
9. **v7 and earlier handoffs** — historical; ignore any claim that contradicts 1-7

---

## 15. First 15 minutes of the next session

1. **Read sections 0.5, 1, 2, 4, 5 of this handoff.** §5 is the single most important to internalize (4 wrong turns and their meta-pattern).
2. **SSH in. Run §6 verification block V0-V5.** Confirm: PRs #32 and #33 status (open or merged), containers healthy, watchdog cron active, IBKR connectable, tests passing.
3. **If PRs #32 + #33 are still open**: merge them in either order (whichever you pick rebases the other). Run `gh pr merge 32 --squash --delete-branch` then `gh pr merge 33 --squash --delete-branch` (or reverse). Pull `main`, restart `tradeflow-app` via `docker compose -f ~/tradeflow/docker-compose.yml restart tradeflow-app`, verify normal startup.
4. **Post-merge smoke** — invoke `vps-smoke-test-runbook` skill to draft a smoke runbook covering: confirm 24/5 session window honors the new no-trade bands, confirm strategy now logs DECISION_TRACE at DEBUG, confirm C1 regime gate fails-open in production (logged as G1; expected behavior, not a bug), confirm EOD scheduler shows `next_fire=Friday 16:25 ET` not daily 15:58 ET.
5. **Hand the PR #34 brief in §13 to CC VPS.** Operator stays out of the loop per "stop giving me work, let cc vps do all the work" directive.
6. **In parallel while CC VPS works**: review the SeanBot 3-phase trailing exit (`execution/executor.py:382-428`) and draft a notes doc on whether it'd fit TradeFlow's mean-reversion entry — input for the PR #37 decision after 30 days of live data.

---

## 16. How to publish this handoff

**Path A — VPS Claude Code brief**:

```
You are VPS Claude Code on the TradeFlow VPS at tradeflow@5.78.212.37 working in ~/tradeflow.
Save the following content verbatim to docs/handoffs/HANDOFF_v8.md, then:

  git -C ~/tradeflow add docs/handoffs/HANDOFF_v8.md
  git -C ~/tradeflow commit -F /tmp/v8_commit.txt
  git -C ~/tradeflow push origin main

The commit message file at /tmp/v8_commit.txt should contain (single line, no Co-Authored-By):

  docs: add v8 handoff (watchdog + 24/5 + strategy realign + C1 + protections research)

Then confirm:
  ls -la ~/tradeflow/docs/handoffs/HANDOFF_v8.md
  git -C ~/tradeflow log -1 --oneline
  git -C ~/tradeflow status

Expected: file exists, commit shown, status clean.

<paste HANDOFF_v8 content below>
```

**Path B — Manual fallback (if VPS CC unavailable)**:

```bash
scp HANDOFF_v8.md tradeflow@5.78.212.37:~/tradeflow/docs/handoffs/HANDOFF_v8.md
ssh tradeflow@5.78.212.37 'git -C ~/tradeflow add docs/handoffs/HANDOFF_v8.md && git -C ~/tradeflow commit -m "docs: add v8 handoff (watchdog + 24/5 + strategy realign + C1 + protections research)" && git -C ~/tradeflow push origin main'
```

The handoff exists only if saved to disk and committed. Until then, treat this chat output as draft.

---

*End of HANDOFF_v8. Target lifespan: until PR #34 (kill switch) merges and operator has 5-10 days of live data showing whether the layered caps need re-tuning. Then HANDOFF_v9 captures the live performance + any tuning adjustments. Until v9, this handoff + the canonical references in §14 are the source of truth.*
