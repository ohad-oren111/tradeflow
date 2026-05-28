# TradeFlow — Handoff v11 (LIVE bars at last + listener connected + RESOLVE mode codified)

*Handoff from end of 2026-05-28 (Session 12). For the first time in TradeFlow's existence, the orchestrator is receiving live MNQ 1-min bars and the strategy is evaluating every bar. Two PRs shipped clean (PR #47 stale-bar watchdog, PR #48 telegram-listener dialog-cache warmup). The SeanBot Telegram listener is connected to "Trading NQ Triggers" id=3914810152. The original v10 §0.5.171 claim that CME Real-Time was already active is **WRONG** — root cause of 0 lifecycles was delayed-only market data + silent `keepUpToDate` failure. New operating mode RESOLVE codified. Mid-session we also discovered an IB farm-flap → silent-subscription-death regression that needs PR-R1 next session. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

**Read order**: §0.5 (NEW §0.5.177–§0.5.183 RESOLVE mode + 6 other new rules), §1 (live state), §2 (the session arc — 3 wrong diagnoses before root cause; do NOT walk those rabbit holes again), §5 (the wrong-diagnosis postmortems), §6 (verification block — first commands of next session), §7 (pending work, PR-R1 is the top).

**The §6 verification block is the first command of the next session.** Bot was just restarted at end of session (post farm-flap regression). The §6 V-blocks confirm whether bars actually resumed, the listener is still connected, and live entitlement is still propagating. Don't ship more code until V0–V8 land.

The single most important paradigm shift this session: **RESOLVE mode (§0.5.177)** — chat-side Claude writes end-to-end work orders that CC VPS runs to completion, only pausing for genuinely operator-only blockers. The operator is now hands-off by design.

The single most important factual correction: **v10 §0.5.171 was wrong about CME Real-Time being active**. The $1.55/mo in v10 was likely OPRA (US options), not CME futures. Real CME futures L1 real-time for non-pro is the "US Securities Snapshot and Futures Value Bundle" (~$10/mo, waived at $30/mo commissions). Broker state (Error 420 "No market data permissions for CME FUT") overruled the handoff. See §0.5.183.

---

## 0.5 Standing rules (permanent — cumulative across sessions; do not remove)

**Carry-forward from HANDOFF_v9 §0.5.1 – §0.5.169 and HANDOFF_v10 §0.5.170 – §0.5.176.** All prior rules remain in force. New rules from Session 12 appended below as §0.5.177 – §0.5.183.

---

### 🎯 §0.5 BANNER — The automation strategy: AUTO / REPORT / AUDIT / RESOLVE (UPDATED v11)

Every PR brief / work order carries `## Mode: <LEVEL>`. CC VPS executes per level:

| Level | Scope | Operator role |
|---|---|---|
| **AUTO** | Docs, config tweaks, log format, tests-only, dependency patch bumps | Zero. Read structured report after CC VPS auto-merges. |
| **REPORT** | Bug fixes ≤5 files with strong test coverage, refactors with no public-API change | One word in chat: `merge` (or `stop`) |
| **AUDIT** | Order execution, strategy, kill switch, secrets, multi-file >50 LOC, broker-state-altering | Open PR in GitHub, scan diff (~2-5 min), type `merge`. |
| **RESOLVE** | Multi-step ops/diagnostic work that crosses tracks (probe → fix → verify → re-verify). End-to-end goal-driven. **NEW v11.** | Zero, except for genuinely external blockers (IBKR portal, operator-Telegram-identity auth, Supabase dashboard SQL paste). Each external block surfaces as `⛔ NEEDS OPERATOR: <exact action>`. |

Default if ambiguous for code work: REPORT. Default for any multi-step ops/diagnostic work: **RESOLVE**.

**CLAUDE.md at repo root (PR #43, `aab7f1b`)** auto-loads the operating manual for CC VPS every session. RESOLVE mode definition should be reflected there in a future docs PR (NOT this session — too late).

---

### §0.5.177 — RESOLVE mode (NEW) — end-to-end probe-AND-fix work orders

**Operator pain point that earned this rule**: Session 12 friction. Chat-side me kept handing back forks/conditional outcomes to the operator ("if A do X, if B do Y") when he just wanted directives. Operator said: *"I want your prompts to be end to end processes for cc vps - that it knows that it should continue working until the issue is resolved, and we're coming back to this chat only for the ongoing context and for external input."*

**The contract**:
- Chat-side authors a single work order with a clear GOAL (what "resolved" looks like, verified from the bot's own logs / broker state, not a probe).
- CC VPS runs end-to-end. Self-verifies every step. Does NOT stop at intermediate findings. Does NOT ask the operator to choose between options CC VPS can evaluate from probes.
- The ONLY valid reason to pause is a genuinely external blocker (operator portal, operator-Telegram-identity, dashboard SQL paste, operator-only credential). When paused, CC VPS posts `⛔ NEEDS OPERATOR: <exact action + how to verify>` and resumes automatically on reply.
- CC VPS reports to chat at: (a) each operator block, (b) PR ready/merged, (c) ✅ GOAL met or re-block. Otherwise keeps working.

**Examples this session**: W-S12.1 (Track A entitlement + Track B watchdog), L-S12.2 (probe + F.2 + F.4 + listener fix + verify). Both ran to ✅ with minimal operator round-trips.

**Anti-pattern to avoid**: a chat-side prompt that says "probe this and report back; I'll decide next." That's the old style. The RESOLVE-mode replacement: "Here's the goal; here's the decision tree; act on it; only pause for external blockers."

### §0.5.178 — Long-secret paste verification — length + SHA256 anchors

When surfacing any string >100 chars for operator paste (telethon session strings, API keys, PASTed migration SQL), the brief MUST include:
- The exact byte length the operator should see (`LEN=<N>`)
- A SHA256 of the expected value (`SHA256=<hex>`)
- A copy-paste verifier that prints `PASTE_OK` / `PASTE_BAD` without echoing the value

Lesson earned: this session a 353-char telethon session string got line-wrapped in chat and the operator pasted only 163 chars. CC VPS detected the length mismatch by comparing container env len to source file len (`ENV_FILE_LEN=163 / source=353`) and re-emitted with a SHA256 anchor. Cost a turn. Anchor every long secret paste going forward.

### §0.5.179 — `docker compose restart` doesn't re-read .env

`docker compose restart <service>` reuses the existing container's environment — it does NOT pick up changes to `.env` or compose env vars. After ANY change to `/home/tradeflow/.tradeflow-secrets/.env`, use:

```bash
docker compose up -d --force-recreate <service>
```

Confirmed this session via `docker inspect <container> --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -c "^TELEGRAM_SESSION_STRING="` returning 0 after a `restart` despite the operator having appended the key to `.env`.

### §0.5.180 — `keepUpToDate=True` is silent on delayed-only market data

`ib.reqHistoricalDataAsync(..., keepUpToDate=True)` is hard-wired to the LIVE market-data entitlement. If the account has DELAYED-only data:
- The historical seed succeeds via HMDS (no live entitlement needed) — you get bars back.
- The live update channel silently never fires. No `updateEvent`, no error log, just darkness.

Detect via `ib.reqMarketDataType(1)` + `ticker = ib.reqMktData(contract,'',False,False)` + read `ticker.marketDataType`. If it's 3 (delayed) and you see Warning 10167 ("displaying delayed market data"), keepUpToDate streaming will not work. Either upgrade entitlement or switch to a different live mechanism (`reqRealTimeBars` rides the live quote line; needs live entitlement too).

**Watchdog (PR #47) is the safety net** — if live data goes silent, it alerts. Without the watchdog, the bot can run blind for many hours (it ran 17h blind before this session).

### §0.5.181 — IB Gateway does NOT auto-resume `keepUpToDate` after farm flap

When the IBKR market-data farm momentarily drops:
- Warning 2103 (`Market data farm connection is broken: usfarm`)
- Warning 2105 (`HMDS data farm connection is broken: ushmds`)
- Error 10182 (`Failed to request live updates (disconnected)`)
- ~1 second later: Warning 2104 + 2106 (farms back OK)

The IB Gateway socket stays UP throughout — so the orchestrator's PR-A reconnect path doesn't fire (PR-A is for socket-level outages, not data-farm flaps). The `keepUpToDate` subscription DIED with the 10182 and is NOT auto-resumed when the farm returns.

**Today's observed pattern (2026-05-28 19:00:58 UTC):** bars stopped after that timestamp, healthchecks kept passing, no errors after the flap — pure silence. PR #47 watchdog correctly fired every 15 min. Manual `docker compose restart tradeflow-app` re-armed the subscription.

**The fix is PR-R1 (next session, AUDIT)**: orchestrator listens for the 2103/2105/10182 trio (or matching ib_async events) and calls `subscribe_bars` again, then sends a Telegram alert on auto-recovery. Without PR-R1, every farm flap = manual restart by the operator.

### §0.5.182 — Private Telegram channels need `iter_dialogs()` warmup before `get_entity`

Telethon `client.get_entity("Trading NQ Triggers")` on a freshly-loaded `StringSession` raises `ValueError("Cannot find any entity corresponding to ...")` for **private channels with no username**. The string-title path looks up the dialog cache, which is empty on first connect.

Fix: walk dialogs once at startup to populate the cache, then resolve by title:

```python
await client.start()
LOGGER.info("[TG_LISTENER] dialog cache warmed — count=%d", count)
async for _ in client.iter_dialogs():
    pass
entity = await client.get_entity(channel)
```

Shipped in PR #48 (`209a37c`). "Trading NQ Triggers" → id `3914810152`, broadcast=True, no username.

### §0.5.183 — Broker / external API state > any handoff carry-forward claim

Refinement of §0.5.97 and §0.5.98. When a prior handoff claims "operator already did X" about external state (entitlement, subscription, API permission, account flag), the next session MUST verify against the source-of-truth before trusting it. **Broker state wins, always.**

This session: v10 §0.5.171 said "CME Real-Time (NP,L1) subscription ACTIVE on live IBKR account, inherited by paper." Session 12 evidence: IBKR Error 420 "No market data permissions for CME FUT" + delayed-only fallback. The handoff was wrong (probably $1.55 was OPRA, not CME). Cost 12+ turns to unwind because chat-side me trusted the handoff claim instead of probing entitlement first.

**Rule**: every session's §6 verification block must hit external sources of truth for any prior-claimed external state. Never assume forward.

---

## 1. Where we are (as of handoff, 2026-05-28 ~19:57 UTC)

### Live production state

- `tradeflow-app` container: **Just restarted** (~19:57 UTC end-of-session, post farm-flap regression). Should be healthy and re-armed. **§6 V1 must verify.**
- `tradeflow-ib-gateway` container: Up healthy, last restart at 17:32:53 UTC (during F-S12.1 entitlement-restore sequence).
- `tradeflow-telegram-listener` container: Up, connected to "Trading NQ Triggers" id=3914810152 (per `[TG_LISTENER] connected — channel=Trading NQ Triggers id=3914810152` at 19:46:31 UTC post-PR-#48 deploy).
- IBKR paper account: `DUQ331660`, NetLiq ~$1M, positions=[], orders=[].
- IBKR live account: `U17545037` (paper inherits market data from this).
- IBKR market-data entitlement: **REAL-TIME ACTIVE** for CME futures L1, confirmed via `MKTDATATYPE=1` probe at 18:43 UTC. Operator subscribed to "US Securities Snapshot and Futures Value Bundle" (~$10/mo non-pro). v10's $1.55 figure was wrong.
- Lifecycles ever: **0**. Strategy has been evaluating live bars only briefly today (18:57–19:00 UTC clean window, then farm-flap regression at 19:00:58 → 0 bars for ~57 min → manual restart at session end). Decisions seen: `noop_warmup` (warmup seeding), `noop_filter_or_regime`.
- SeanBot signals captured to `seanbot_signals`: **0** at handoff time (listener was just connected; 30-min watch was running for first capture; the channel was quiet during the watch window).

### What just shipped (Session 12 — two PRs)

- **PR #47** (`b9a0bc0`) — **PR-W** `feat(observability): stale-bar watchdog + live_bars_60m in /status`. 3 files, +325/-1. In `src/orchestrator.py`: 5-min stale threshold, 15-min alert cooldown, session-edge suppression via `_in_session_edge_window` (reused from strategy for single-source-of-truth on "expected bar window"), recovery log on first bar after a stale window, deque-bounded ring buffer for `live_bars_60m`. In `comms/telegram.py`: surface `live_bars_60m` in `/status`. 12 new unit tests, full suite green at merge. ALERT-only (no auto-halt — halt would be AUDIT). Verified firing in prod intentionally during the no-entitlement window, then again at the 19:00:58 UTC farm flap. Both paged the operator on schedule.

- **PR #48** (`209a37c`) — **PR-L** `fix(listener): warm dialog cache so private-channel title resolves`. 2 files, +105/-0. `src/listeners/telegram_listener.py` now walks `iter_dialogs()` once at startup (logs `[TG_LISTENER] dialog cache warmed — count=<N>`) before `client.get_entity(channel)`. Without this, fresh `StringSession` raises `ValueError` resolving private channels by title. Plus 4 unit tests pinning the ordering so a refactor can't silently regress.

### What we discovered this session (operational facts not yet in code)

- **Root cause of 0 lifecycles ever**: paper account had DELAYED-only CME futures market data. `keepUpToDate=True` silently delivers 0 callbacks on delayed entitlement (the seed via HMDS works, the live update channel never opens). Took 4 wrong theories to land on this (see §5). Fixed by operator subscribing to the right entitlement product on the LIVE account U17545037 (paper inherits, ~15 min propagation).
- **v10 §0.5.171 was WRONG**. Claimed CME Real-Time (NP,L1) at $1.55/mo was active. Actual: $1.55 is OPRA (US options), not CME futures. The real product is "US Securities Snapshot and Futures Value Bundle" (~$10/mo non-pro). See §0.5.183 — broker state overrules handoff claims.
- **`subscribe_bars` wiring is CORRECT.** Initial chat-side suspicion was app wiring (signature mismatch, swallowed exception, etc.). The standalone clientId=99 probe with independent textbook wiring also got 0 callbacks under delayed entitlement. Wiring exonerated cleanly.
- **`docker compose restart` reuses container env** — does not pick up `.env` changes (§0.5.179). The 19:14 UTC listener restart did nothing for the new TELEGRAM_SESSION_STRING; required `docker compose up -d --force-recreate telegram-listener`.
- **Telethon `get_entity` by title fails on empty dialog cache** for private channels — fixed in PR #48 with `iter_dialogs()` warmup (§0.5.182). "Trading NQ Triggers" id=3914810152.
- **IB farm-flap → silent subscription death** (§0.5.181). Observed at 19:00:58 UTC. Single most concrete signature: `Warning 2103 ... Warning 2105 ... Error 10182, reqId 4: Failed to request live updates (disconnected) ... Warning 2106 ... Warning 2104` all within ~1 second, then bars never resume. PR-A reconnect doesn't fire because the socket stays up.
- **Migration F.2 schema fact**: `seanbot_signals` table exists in Supabase as of 2026-05-28 (HTTP 200 confirmed). Applied via dashboard SQL editor by operator (CC VPS cannot apply migrations — no psql/CLI; the SUPABASE_DB_URL is intentionally not in .env).
- **Telethon `StringSession` size**: ~353 characters for an account-authenticated session. Always anchor with length + SHA256 (§0.5.178).
- **Operator's IBKR-registered phone**: known to CC VPS via the F.4 auth flow (international format starting with +972). Do NOT log it back into chat; it's stored only in telethon's session blob.

---

## 2. The session's bug thread

This was a long and instructive arc with **three wrong diagnoses** before root cause. Full narrative — read carefully so the next session doesn't re-walk these rabbit holes (and see §5 for the explicit wrong-diagnosis postmortems).

1. **Session opened** with operator reporting: SeanBot is trading (screenshots showed live entries/exits on "Trading NQ Triggers") but TradeFlow is "only breaking" — 0 lifecycles ever, daily reports showing IB API TimeoutError flapping, restart_count=36 on May 27 report.

2. **Diagnostic D-S12.2 (read-only)**: chat-side me wrote a multi-section probe covering bar feed health, decision distribution, IB flap timeline, and source-of-truth checks. CC VPS executed and reported: instrumentation deployed (PR-D1/D2/D3a from v10 present), but **0 [BAR] in 24h** despite startup log showing `subscribe_bars — seed=121`. Strategy never invoked → 0 eval lines → 0 lifecycles. Two flap events in 24h (max elapsed_sec=22.9) — but bars were dead 3h BEFORE the first flap, so flap-warmup theory died upfront.

3. **D-S12.3 (after operator's friend suggested checking gateway connectivity)**: chat-side added live gateway probe (`reqCurrentTime`, qualified MNQM6, historical fetch, whatIf order). CC VPS confirmed socket healthy, `reqCurrentTime` OK, account `DUQ331660`. **But on-demand historical came back ~11 min stale (rolling, not frozen)** and 0 keepUpToDate callbacks over 150s during RTH. Chat-side interpreted this as: ~10-min rolling lag + zero stream = "delayed data, not real-time." Strong hypothesis.

4. **F-S12.1 (RESOLVE mode in spirit — first time)**: chat-side authored a fix-and-verify work order: restart gateway, re-probe with `reqMarketDataType(1)`, then restart app if entitlement looked live. Predicted: if stuck-session, restart fixes it; if delayed-entitlement, it doesn't. **WRONG DIAGNOSIS #1 ("stuck gateway session")**. CC VPS restarted gateway, app reconnected, but post-restart probe still showed 0 callbacks + ~10 min stale. SSH dropped mid-check (operator's laptop → VPS broken pipe) — but the bot containers ran on regardless. The check the SSH severed was the bar-flow confirmation.

5. **C-S12.1 confirmed STILL DEAD** after clean gateway + app restart: 0 [BAR], 0 eval, 0 errors. Stuck-session theory dead. Chat-side correctly avoided retrofitting — threw it out and re-read.

6. **C-S12.2 (RESOLVE mode formalized)**: fork between three remaining theories — competing-login, entitlement-gap, subscribe_bars-bug. Operator asked the meta-question: "Is there an option for me to have the same discussion with cc vps directly?" Chat-side acknowledged the workflow friction and started writing tighter end-to-end work orders. The reqRealTimeBars probe (independent live-bar mechanism) ALSO got 0 callbacks + Error 420 "No market data permissions for CME FUT". Then `reqMktData(type=3)` returned Warning 10167 "Requested market data is not subscribed. Displaying delayed market data." with bid/ask/last flowing every ~3s.

7. **Mid-investigation Error 10197 "competing live session"** appeared on the clientId=99 probes. **WRONG DIAGNOSIS #2 ("competing IBKR login elsewhere")**. CC VPS stopped the orchestrator entirely → 10197 STILL appeared → so it wasn't the bot competing with itself. Restarted gateway → 10197 cleared. So 10197 was actually a stuck gateway-state artifact, not a real external login. Refuted via probe.

8. **ROOT CAUSE LANDED**: paper account has DELAYED-only CME futures market data. `keepUpToDate=True` is hard-wired to LIVE entitlement and silently delivers 0 updates on delayed feeds (the seed works because pure historical pulls use HMDS, no live entitlement needed). Captured as §0.5.180. Three paths offered: (1) operator subscribes to real-time L1 in IBKR portal, (2) code: switch to reqMktData delayed + tick→bar aggregator, (3) code: poll reqHistoricalData every minute. Chat-side recommended path 1 — delayed data defeats paper validation.

9. **v10 §0.5.171 corrected**: chat-side web-searched the actual current IBKR market-data product. v10's "CME Real-Time (NP,L1) at $1.55/mo" doesn't exist as a CME product — $1.55 is OPRA (US options). Real CME futures L1 non-pro is the "US Securities Snapshot and Futures Value Bundle" (~$10/mo, waived at $30/mo commissions). Operator informed.

10. **W-S12.1 (RESOLVE mode, end-to-end)**: Track A (operator-gated entitlement) + Track B (autonomous watchdog).
    - **Track B = PR #47**: stale-bar watchdog. CC VPS built end-to-end: branched from origin/main, +325 lines across `src/orchestrator.py` and `comms/telegram.py`, 12 unit tests, full suite 284 green, ruff/black clean. PR opened, CI passed, operator typed `merge`, CC VPS squash-merged + deleted branch + pulled main + rebuilt + force-recreated tradeflow-app. Verified the watchdog fired at exactly the 5-min stale threshold via intentional observation window — `[WATCHDOG] no live bar in 5m during session — feed delayed/dead` at 18:37 UTC. Cooldown held (no spam). Recovery path tested implicitly later.
    - **Track A**: operator subscribed to the bundle on LIVE account U17545037 → "entitlement done" → CC VPS restarted gateway → `MKTDATATYPE=1, last=30314.75` confirmed (attempt 2 at T+10min after some propagation churn: 354 → 10197 transient → LIVE_OK) → restarted app → **first-ever [BAR] + [STRAT] eval** logged at 18:57–18:59 UTC. Decisions: `noop_warmup` (warmup seeding), `noop_filter_or_regime`. **0-lifecycles-ever bug officially RESOLVED.**

11. **Operator asked about the SeanBot Telegram listener** — was it capturing? Chat-side acknowledged honestly that prior session evidence (seanbot_signals 404 + listener RestartCount=833 + "no session" logs) pointed at "no." Wrote a probe; operator asked to make it end-to-end probe-AND-fix.

12. **L-S12.2 (RESOLVE mode, multi-step operator-gated)**:
    - Probe confirmed both F.2 (seanbot_signals 404) and F.4 (TELEGRAM_SESSION_STRING MISSING) blockers.
    - F.2: CC VPS displayed migration SQL → operator pasted into Supabase dashboard SQL editor → "F.2 done" → HTTP 200 confirmed.
    - F.4 (telethon 3-step interactive auth): phone (+972...) → code (Telegram-sent) → SessionString returned (~353 chars). First operator paste was line-wrapped at 163 chars; CC VPS detected via length mismatch (`ENV_FILE_LEN=163` vs source `353`), re-emitted with SHA256 anchor, operator re-pasted → PASTE_OK. Captured as §0.5.178.

13. **Listener crash on first connect** — `client.get_entity("Trading NQ Triggers")` raised `ValueError`. CC VPS walked dialogs to find the channel: id=3914810152, broadcast=True, no username (private). Verified `iter_dialogs()` warmup fixes the title lookup. Shipped PR #48 (REPORT level), CI green, operator merged via `gh pr merge --squash`, force-recreate listener → `[TG_LISTENER] connected — channel=Trading NQ Triggers id=3914810152` at 19:46:31 UTC. Captured as §0.5.182.

14. **Mid-session regression at 19:00:58 UTC**: PR #47 watchdog fired stale_min=5/20/35/50 alerts. CC VPS diagnosed from raw logs: at 19:00:58 UTC, IB market data farm broke (Warning 2103 + 2105 + Error 10182), came back ~1s later (2104 + 2106), but the `keepUpToDate` subscription died with that 10182 and was never re-armed. The IB socket stayed up so the orchestrator's PR-A reconnect path didn't fire. Bars silent for ~57 min until session-end manual restart. **WRONG DIAGNOSIS #3 (briefly): "Track A done"** — actually no, the bot still has no auto-resubscribe on farm flap. Captured as §0.5.181. Queued PR-R1 as the next session's top priority.

15. **Operator authorized `docker compose restart tradeflow-app`** at end of session. §6 V1/V3 will verify post-restart state.

**Meta-arc**: started Session 12 with "bot has never traded, cause unknown after Session 11's 24/5+observability work." Ended with **live bars + strategy evaluating + listener connected + watchdog shipped + RESOLVE mode codified + one new bug found (farm-flap)**. Net: from 0 lifecycles + 4 candidate theories to root cause proven + safety net deployed + only one new code item in the queue. RESOLVE mode is the single most important workflow change.

---

## 3. What the system is actually made of

**Single source of truth:** `origin/main` at commit `209a37c` (PR #48 merge). Plus Session 12's earlier merge `b9a0bc0` (PR #47).

Production-live code paths (verified deployed in containers at handoff time):
- `src/orchestrator.py` — main loop + PR-A resilience + PR #47 watchdog + `_count_live_bars_last_60m` + `_watchdog_check_bar_liveness`
- `src/clients/ib_client.py` — IBClient wrapper + `connect_with_resilience()` + `subscribe_bars` (keepUpToDate=True path) + [BAR] log at L334
- `src/strategy.py` — `Sma100BounceStrategy` + `_regime_ok()` C1 gate + `_in_session_edge_window()` (now also consumed by orchestrator watchdog) + [STRAT] eval log at L395
- `src/execution/bracket.py` — bracket-order builder
- `src/execution/force_close.py` — Friday-only EOD at 16:25 ET
- `src/execution/reconciler.py` — drain + scan loops
- `src/listeners/telegram_listener.py` — telethon main loop + **PR #48 dialog-cache warmup**
- `src/listeners/seanbot_parser.py` — regex parser (unchanged this session)
- `comms/telegram.py` — alerter + **PR #47 `live_bars_60m` in /status**
- `config/risk_params.py` — single config dataclass
- `main.py` — entrypoint
- `CLAUDE.md` at repo root — operating manual auto-loaded by CC VPS (should be updated to mention RESOLVE mode in a future docs PR — NOT this session)

**Docker services** (post Session 12):
- `tradeflow-app` (container_name = service name) — main bot
- `ib-gateway` (service) / `tradeflow-ib-gateway` (container_name) — IBKR gateway
- `telegram-listener` (service) / `tradeflow-telegram-listener` (container_name) — SeanBot capture, now connected

**Docker network**: `tradeflow_tradeflow-net` (NOT `tradeflow-net` or `tradeflow_default`; verified via `docker network ls`). Used by CC VPS this session when running standalone probe containers.

**Supabase tables in use** (verified via REST):
- `lifecycles` — empty (0 rows, expected)
- `lifecycle_events` — N/A
- `seanbot_signals` — **exists** as of this session (F.2 applied via dashboard). Schema includes `id, ts, channel, message_id, type (entry|exit|stop_moved|unknown), direction, symbol, price, stop_price, target_price, pnl_points, contracts, raw_text, parsed_ok, created_at`. UNIQUE(channel, message_id). Empty at handoff.

**Secrets path**: `/home/tradeflow/.tradeflow-secrets/.env`. Now contains TELEGRAM_SESSION_STRING (353 chars). **CC VPS must NEVER write to this directory** — operator-paste only (§0.5 v10 carry-forward).

---

## 4. Verified facts (cumulative — carry forward + this session)

**From HANDOFF_v10 §4 (carry forward verbatim):**

- MNQ specs: TICK_SIZE=0.25, MULTIPLIER=$2/point, COMMISSION_RT=$0.62 RT, MARGIN_REQ=$2,000 day-trade
- Compose service `ib-gateway` ≠ container_name `tradeflow-ib-gateway`
- Pytest is NOT in prod container — host venv at `/home/tradeflow/tradeflow/.venv/bin/pytest`
- `.tradeflow-secrets/.env` shadows compose `${VAR:-default}` patterns
- Branch off `origin/main`, never local `main`
- Harness denies destructive git verbs (reset --hard, rebase, push --force, branch -D, commit --amend)
- Harness denies bare `sleep <N>` — use `timeout <max> bash -c 'until <cond>; do sleep 2; done'`
- Harness denies `docker exec <c> env` — use `docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}'`
- IBKR client_id = 1 for orchestrator
- Strategy timeframe: 1-min bars (Sma100BounceStrategy)
- `ib_async.useRTH` on futures: True = RTH only (09:30–16:00 ET MNQ); False = full 24/5 minus daily 17:00–18:00 ET maintenance break
- `_adapter` callback fires on every tick when `keepUpToDate=True`; gate on `has_new_bar` to log only on bar close
- Migration pattern: `supabase/migrations/<yyyymmddhhmmss>_<name>.sql`, applied via Supabase dashboard SQL editor. No psql/CLI on VPS. CC VPS instructs operator to paste-and-run.
- Telethon `StringSession` stores session as serializable blob in `.env` as `TELEGRAM_SESSION_STRING=...`. No volume needed.
- Dockerfile HEALTHCHECK uses `main.py' in /proc/1/cmdline` — fails on listener (different entrypoint); listener healthcheck currently broken (cosmetic, doesn't affect function).
- `.env` line 47 has unquoted value containing "NQ" → must grep, NEVER source.

**NEW load-bearing facts this session:**

- **Front-month MNQ this session**: `MNQM6` (lastTradeDateOrContractMonth=`20260618`, conId=`770561201`). Confirmed via `ib.qualifyContractsAsync` repeated probes. `Future('MNQ', exchange='CME', currency='USD')` without explicit month returns AMBIGUOUS (5 fronts) — always pass the month for safety. The orchestrator's contract builder uses the resolved front month at startup; reuse it rather than re-qualifying generically.
- **`subscribe_bars` is at `src/clients/ib_client.py:290-360` approximately** — calls `reqHistoricalDataAsync(keepUpToDate=True)`, wires `bars.updateEvent += _adapter` where `_adapter(bars_obj, has_new_bar)` matches the documented telethon... wait, ib_async signature. **The wiring is CORRECT** and was exonerated by the standalone probe with independent textbook wiring also getting 0 callbacks under delayed entitlement.
- **`ib_async.IB.whatIfOrderAsync` returns a LIST** (not an OrderState object directly), at least under v1.0.0. Caller must `[0]` it and handle empty-list for failed-validation cases.
- **`docker compose restart` does NOT pick up .env changes** (§0.5.179). Use `docker compose up -d --force-recreate <service>` after any `.env` edit.
- **`keepUpToDate=True` silently delivers 0 callbacks on delayed market data** (§0.5.180). Always verify mdType=1 via `reqMarketDataType(1)` + `reqMktData` ticker before assuming a streaming sub will work.
- **IB Gateway does NOT auto-resume `keepUpToDate` after farm-flap** (§0.5.181). Signature: Warning 2103 + Warning 2105 + Error 10182 within ~1s, then 2104/2106 OK return, then silence. Socket stays up so PR-A reconnect doesn't fire. PR-R1 is the fix.
- **`client.get_entity("Trading NQ Triggers")` requires `iter_dialogs()` warmup** for fresh `StringSession` on a private channel (§0.5.182). PR #48 ships the warmup.
- **"Trading NQ Triggers" channel id**: `3914810152`. Broadcast=True. No username. Private.
- **IBKR market-data product for CME futures L1 real-time (non-pro)**: "US Securities Snapshot and Futures Value Bundle" (~$10/mo non-pro, waived at $30/mo commissions). NOT "CME Real-Time (NP,L1)" — that was a confab in v10. The $1.55 in v10 §0.5.171 is OPRA (US options). The portal shows exact name/price for the operator's account.
- **Subscriber Status default is Professional (~10x cost)**. Must be set to Non-Professional in Client Portal → Settings → Market Data Subscriptions → Subscriber Status. Operator confirmed Non-Pro this session.
- **Paper inherits live's market-data subscriptions** (~15 min propagation post-change). Cannot be subscribed paper-side.
- **Telethon `StringSession` length**: ~353 characters for an authenticated account. SHA256 length+hash anchors required for paste verification (§0.5.178).
- **Telethon 1.43.2** in app container; **ib_async 1.0.0** in app container (verified via `docker exec tradeflow-app python3 -c "import telethon; print(telethon.__version__)"` etc.).
- **Docker network name**: `tradeflow_tradeflow-net` (compose project name auto-prefix). NOT `tradeflow-net` or `tradeflow_default`. Standalone containers attaching for probes: `--network tradeflow_tradeflow-net`.
- **`seanbot_signals` table is REAL and EMPTY** as of handoff. Columns per the migration in `supabase/migrations/20260528004557_seanbot_signals.sql`.

---

## 5. Wrong diagnoses this session — READ BEFORE YOU DEBUG

**Three wrong theories before the actual root cause (delayed-only market data). Plus one mid-session regression mis-call. Chat-side me generated each; CC VPS refuted each via raw probes. The "rule of twice-wrong" almost triggered — read carefully.**

1. **"Stuck gateway session, restart fixes it"** (post-D-S12.3, mid-F-S12.1)
   - Evidence I cited: error 1100 in gateway log without matching 1101/1102 restore + HMDS "inactive but should be available upon demand" + persistent ~10min stale + 0 streaming callbacks.
   - Prediction that would have confirmed: bars flow after `docker compose restart ib-gateway` + clean re-login.
   - Actual observation: 0 [BAR] after clean gateway + app restart. Theory refuted by evidence.
   - **Correct diagnosis**: not the gateway session — the entitlement layer below it. The gateway WAS in a slightly stuck state (the 10197 cleared after restart), but the underlying problem was the account's market-data tier, not the session.
   - **Lesson**: persistent rolling-stale data + zero streaming despite a clean socket signals an entitlement-tier issue, not a session-state issue. Probe `reqMktData` + `marketDataType` BEFORE attempting restarts.

2. **"Competing IBKR login from external session"** (mid-C-S12.2)
   - Evidence I cited: error 10197 "No market data during competing live session" on the clientId=99 probe.
   - Prediction that would have confirmed: stopping the orchestrator clears 10197.
   - Actual observation: orchestrator stopped → 10197 STILL fired on the probe → not the bot competing with itself. Then gateway restart → 10197 cleared. So 10197 was a stuck-gateway artifact, not a real external login.
   - **Correct diagnosis**: 10197 in this case was a gateway internal state issue, cleared by restart. **It's still a meaningful signal in general** (could indicate real external login) — just not this time. Distinguish via the orchestrator-stopped probe.
   - **Lesson**: when 10197 appears, the cheap disambiguator is to stop the orchestrator and re-probe with a single fresh client. If 10197 persists, it's either a stuck gateway or a true external login; gateway restart distinguishes those.

3. **"Orchestrator `subscribe_bars` wiring bug"** (briefly considered post-stuck-gateway refutation)
   - Evidence I cited: 0 [BAR] after gateway + app restart + live entitlement appearing OK.
   - Prediction that would have confirmed: reading `subscribe_bars` source reveals a wiring bug (signature mismatch, swallowed exception, missing `+= adapter`, etc.).
   - Actual observation: the STANDALONE clientId=99 probe — using independent, textbook-correct `bars.updateEvent += handler` wiring — also got 0 callbacks. Two independent implementations, identical 0 result. Wiring exonerated.
   - **Correct diagnosis**: `keepUpToDate=True` is hard-wired to LIVE entitlement and fails SILENTLY on delayed-only feeds (§0.5.180). The seed via HMDS works, the live update channel never opens. The fact that the standalone probe failed too was the key — it ruled out the app and pointed at the env (entitlement).
   - **Lesson**: when the production code path produces 0 of something during a probe, **always test the same call with an independent, minimal-wiring probe**. If both fail, the bug is below the application layer (library, gateway, entitlement, network). If only the production code fails, then the bug is in the wiring.

4. **"Track A done; bars flowing"** (briefly, post-W-S12.1 success at 19:00 UTC) — refuted within an hour by PR #47 firing.
   - Evidence I cited: 3 [BAR] + 3 [STRAT] eval at 18:57–18:59 UTC, 1/min cadence, GOAL met.
   - Prediction that would have confirmed: bars keep flowing.
   - Actual observation: at 19:00:58 UTC the IB market data farm dropped briefly; `keepUpToDate` died with Error 10182; farm came back ~1s later; sub was never re-armed; bars silent for 57 min.
   - **Correct diagnosis**: GOAL was met in a snapshot sense, but the bot is not robust to farm flaps (§0.5.181). PR-R1 (auto-resubscribe) is the durable fix.
   - **Lesson**: "GOAL met" snapshots aren't durable until the bot has been running for at least a few hours through normal operational noise (farm flaps happen routinely). The PR #47 watchdog is the safety net that made this immediately observable instead of silent.

**Lesson for next session (meta-pattern):**

Every wrong diagnosis this session was chat-side me jumping a layer too narrow:
- jumped to gateway-state when it was entitlement-tier
- jumped to competing-login when it was stuck-gateway-state which was masking entitlement-tier
- jumped to app-wiring when it was the library/entitlement interaction below the app

The right move each time was CC VPS pushing the diagnosis DOWN a layer: standalone probes, source-of-truth checks, broker-direct queries. **RESOLVE mode (§0.5.177) formalizes this**: chat-side authors the goal + decision tree; CC VPS gathers evidence and acts; chat-side doesn't bounce mid-decision unless an external blocker shows up.

Secondary lesson: **broker state beats handoff claims, always (§0.5.183)**. v10's §0.5.171 said CME Real-Time was active. The broker said no live perms. The broker was right. The handoff was wrong. Always verify carry-forward external-state claims against the source-of-truth before trusting them.

---

## 6. Verification block — run this before doing anything

**V0 — Confirm origin/main HEAD + container health (post end-of-session-restart)**
```bash
git -C /home/tradeflow/tradeflow fetch origin
git -C /home/tradeflow/tradeflow log --oneline -1 origin/main
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"
```
Expect: origin/main HEAD = HANDOFF_v11 publish commit (or later). Three containers Up. `tradeflow-app` was just restarted at session-end so RunningFor will be relatively recent.

**V1 — Bot state baseline + RestartCount drift**
```bash
docker inspect tradeflow-app --format "RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}} Health={{.State.Health.Status}}"
docker inspect tradeflow-ib-gateway --format "RestartCount={{.RestartCount}} Health={{.State.Health.Status}}"
docker inspect tradeflow-telegram-listener --format "RestartCount={{.RestartCount}} State={{.State.Status}}"
```
Expect: app+gw RestartCount=0 on the post-Session-12 containers. Listener should have stabilized at a low RestartCount (no more "no session" loop since F.4 completed and PR #48 deployed). If listener RestartCount keeps climbing, it's a NEW bug — investigate via `docker logs tradeflow-telegram-listener --since 5m`.

**V2 — PR #47 + PR #48 present in deployed code**
```bash
docker exec tradeflow-app grep -cE "_watchdog_check_bar_liveness|live_bars_60m" /app/src/orchestrator.py
docker exec tradeflow-app grep -cE "live_bars_60m" /app/comms/telegram.py
docker exec tradeflow-telegram-listener grep -cE "iter_dialogs|dialog cache warmed" /app/src/listeners/telegram_listener.py
```
Expect: ≥2 / 1 / ≥1. If any 0 → wrong image rebuild; force-recreate.

**V3 — Live bar feed is actually flowing (post end-of-session-restart)**
```bash
docker logs tradeflow-app --since 10m 2>&1 | grep -cE "\[BAR\]"
docker logs tradeflow-app --since 10m 2>&1 | grep -cE "\[STRAT\].*eval ts="
docker logs tradeflow-app --since 10m 2>&1 | grep -E "\[STRAT\].*eval ts=" | tail -5
docker logs tradeflow-app --since 10m 2>&1 | grep -E "\[WATCHDOG\]" | tail -5
```
Decision tree:
- [BAR] count ~equals minutes since restart in RTH/extended hours, [STRAT] eval matches, no [WATCHDOG] alerts → ✅ HEALTHY.
- [BAR]=0 + [WATCHDOG] firing → farm-flap regression again (§0.5.181). Workaround: `docker compose restart tradeflow-app`. Real fix: PR-R1 (queued; see §13).
- [BAR]=0 + no watchdog alert AND outside CME session window → expected (daily break, weekend).
- [BAR]=0 + no watchdog alert during RTH → §0.5.180 / §0.5.181 / probe with `reqMarketDataType(1)` per §6 V5 below.

**V4 — Live entitlement still propagating (broker source-of-truth)**
```bash
# Write/run a tiny probe inside the app container to read live mdType.
docker exec -i tradeflow-app python3 - << 'PYEOF'
import asyncio
from ib_async import IB, Future
async def main():
    ib = IB()
    await asyncio.wait_for(ib.connectAsync('ib-gateway', 4004, clientId=99, timeout=10), 12)
    ib.reqMarketDataType(1)
    q = await ib.qualifyContractsAsync(Future('MNQ','20260618',exchange='CME',currency='USD'))
    tkr = ib.reqMktData(q[0],'',False,False)
    await asyncio.sleep(4)
    print('MKTDATATYPE=', tkr.marketDataType, 'last=', tkr.last)
    ib.cancelMktData(q[0]); ib.disconnect()
asyncio.run(main())
PYEOF
```
Expect: `MKTDATATYPE= 1` (live). If 3 (delayed) → entitlement lapsed; back to §0.5.180 / Track A from W-S12.1. clientId=99 may hit 10197 if orchestrator holds the line — that's expected; the mdType value is still read off the ticker on first tick.

**V5 — Telegram listener connected + capturing**
```bash
docker logs tradeflow-telegram-listener --since 10m 2>&1 | grep -E "\[TG_LISTENER\]" | tail -10
```
Expect (at least once after deploy): `[TG_LISTENER] dialog cache warmed — count=<N>` followed by `[TG_LISTENER] connected — channel=Trading NQ Triggers id=3914810152`. If `no session` is back → TELEGRAM_SESSION_STRING got dropped from .env or revoked.

**V6 — `seanbot_signals` table + captured rows**
```bash
F=/home/tradeflow/.tradeflow-secrets/.env
URL=$(grep -E "^SUPABASE_URL=" "$F" | cut -d= -f2-)
KEY=$(grep -E "^SUPABASE_SERVICE_ROLE_KEY=" "$F" | cut -d= -f2-)
curl -s -H "apikey: $KEY" -H "Authorization: Bearer $KEY" \
  "$URL/rest/v1/seanbot_signals?select=count&limit=1" -H "Prefer: count=exact" -i | head -8
curl -s -H "apikey: $KEY" -H "Authorization: Bearer $KEY" \
  "$URL/rest/v1/seanbot_signals?select=*&order=created_at.desc&limit=5"
```
Expect: HTTP 200 + count ≥ 0. If channel has been active since the listener connected (19:46:31 UTC) AND the count is still 0 AND no `[TG_LISTENER] signal —` lines in V5 → parser path or write path is broken. Otherwise normal.

**V7 — Lifecycles state**
```bash
F=/home/tradeflow/.tradeflow-secrets/.env
URL=$(grep -E "^SUPABASE_URL=" "$F" | cut -d= -f2-)
KEY=$(grep -E "^SUPABASE_SERVICE_ROLE_KEY=" "$F" | cut -d= -f2-)
curl -s -H "apikey: $KEY" -H "Authorization: Bearer $KEY" \
  "$URL/rest/v1/lifecycles?select=count&limit=1" -H "Prefer: count=exact" -i | head -8
```
Expect: 0 unless the strategy fires `long_signal` between handoff and next session (possible if it runs through an RTH window without farm flaps). If >0 → check `lifecycles?select=*&order=created_at.desc&limit=5` for the actual rows.

**V8 — Overnight long-tail (transient_disconnect/recovered + farm-flap)**
```bash
docker logs tradeflow-app --since 12h 2>&1 | grep -cE "\[ORCH\] healthcheck: transient_disconnect|\[ALERT\] reconnect_recovered"
docker logs tradeflow-app --since 12h 2>&1 | grep -cE "Warning 2103|Warning 2105|Error 10182"
docker logs tradeflow-app --since 12h 2>&1 | grep -cE "\[WATCHDOG\] no live bar"
```
Expect: transient_disconnect count == reconnect_recovered count (PR-A is working). Any 2103/2105/10182 trio that wasn't followed by a manual restart AND the watchdog WARN log = a farm flap that killed the sub but the bot stayed silent. That's exactly what PR-R1 needs to auto-fix.

---

## 7. Pending work queue (priority order)

### 1. **PR-R1 — auto-resubscribe on farm flap (AUDIT)** — HIGHEST priority next session

Without this, every IB market-data farm flap (observed at 19:00:58 UTC Session 12; 03:30 and 04:36 UTC earlier; happens routinely) silently kills the `keepUpToDate` subscription and the bot runs blind until manual restart. PR #47 watchdog catches it via alerts, but the operator has to react. PR-R1 makes the bot self-healing.

**Scope:**
- In `src/clients/ib_client.py`: subscribe to ib_async error events (`ib.errorEvent += ...`) or watch wrapper warnings (2103/2105/2106/2104) to detect the farm-broken → recovered transition.
- On detected transition, call `subscribe_bars` again with the same args (the existing subscribe_bars callable needs to be re-callable; verify the contract handle is reusable).
- Emit `[ORCH] bar_subscription auto-resubscribed after farm-flap` INFO log + `[ALERT] bar_sub_resubscribed_after_farm_flap` for Telegram.
- Idempotency: don't resubscribe if a subscription was created in the last 30s (avoid double-arming).
- Tests: simulate the 2103/2105 → 10182 → 2104 sequence and assert resubscribe + recovery alert.

**Autonomy**: AUDIT (touches subscription lifecycle + reconnect logic). Operator scans diff + types `merge`.

**RESOLVE-mode work order**: chat-side will draft for Session 13 paste. Goal = "after a simulated or natural farm flap, bars resume within 60s without manual restart, and a recovery alert fires."

### 2. **`detect_signal` refactor — split regime vs filter (REPORT, ~10 LOC)**

Currently `[STRAT] eval decision=noop_filter_or_regime` collapses two distinct gates. With live bars flowing, the actual decision distribution is now observable — and the dominant one will be one of these two. Splitting the boolean tells us WHICH gate to tune.

`src/strategy.py` `detect_signal` has inline boolean expressions for regime_ok / touch_ok / ma_order_ok / bullish_ok / gap_ok. Extract them to named locals. Update the eval log decision label to `noop_regime` | `noop_filter` distinctly.

Carry from v10 §7 / queued PR-D3b.1 cousin.

### 3. **PR-D3c — daily comparison digest (REPORT, ~150 LOC)** — depends on listener data accumulating

Join `seanbot_signals` (by timestamp) against `[STRAT] eval` logs (need to either tee logs to Supabase OR parse log files server-side). Categorize: `AGREE_ENTER` / `AGREE_NOOP` / `MISS` (SeanBot enters, TF doesn't) / `FALSE_POSITIVE` (TF enters, SeanBot doesn't). Push daily Telegram digest at 16:30 ET.

Don't pre-design — wait for empirical data shape from at least one full RTH session.

### 4. **PR-S1 — Secret rotation + log redaction (AUTO after operator rotates)**

Urgent security debt:
- IBKR_PASSWORD in plaintext via `docker inspect tradeflow-app .Config.Env`
- Telegram bot token leaks in app logs (every httpx GET to `api.telegram.org/bot<TOKEN>/getUpdates`)
- Now also: TELEGRAM_SESSION_STRING exposure via `docker inspect`

Move env vars from compose `environment:` to `env_file:` mount. Add httpx logger config to redact `bot<TOKEN>` and TELEGRAM_SESSION_STRING from log lines.

### 5. **Kill switch PR (AUDIT)** — real-money readiness gate

4-layer drawdown caps: 1.5% daily / 3% weekly / 6% monthly / 12% trailing-account. Reference equity hard-coded $100K (NOT $1M paper NAV).

### 6. **Watchdog tuning (DEFERRED)**

Don't touch unless 48h of post-PR-#47 telemetry shows watchdog-induced cascades or false alerts during thin overnight windows. Tunables: `_WATCHDOG_STALE_THRESHOLD_SEC=300`, `_WATCHDOG_ALERT_COOLDOWN_SEC=900`. Documented in `src/orchestrator.py`.

### 7. **PR-D3b.1 (legacy v10) — listener restart-loop + broken healthcheck**

Largely moot now that F.4 is done and the listener actually connects (no more `return` after the no-session error). The broken healthcheck on the listener (inherits `tradeflow-app`'s `main.py` HEALTHCHECK probe) is purely cosmetic — `docker ps` shows `(health: starting)` permanently. Fix when convenient via `healthcheck: { disable: true }` in compose. NOT urgent.

### Gaps carried forward (low priority)
- **G1** — C1 regime gate fail-open in production (buffer 150 1-min vs threshold 202 30-min). Lower priority now that empirical observation can show whether it matters.
- **G2** — `risk_params.py:signal_scan_start_et` comment misleading. Grep returns nothing — likely dropped during prior rebases. Confirm and either remove from G-list or update.
- **G3** — Seed depth 45 vs SMA warmup 100. Related to G1.

### Operational debt
- Local `main` ref still drifts cosmetically post-squash-merge (§0.5.168 — known, non-blocking)
- `risk_params.py` docstring carries old PR-#10-era language
- Orphan branches on origin (`claude/watchdog-stale-bars`, `claude/listener-dialog-warmup` — both deleted post-merge; check via `gh api repos/ohad-oren111/tradeflow/branches`)

---

## 8. Test safety — cumulative

Carry forward from HANDOFF_v10 §8. New this session:

- **Telethon test mocking gotcha (PR #48)**: tests that import `src.listeners.telegram_listener` need `telethon` installed in the venv (was missing initially; `.venv/bin/pip install "telethon>=1.34,<2.0"` resolved). Also: `StringSession` constructor must be patched alongside `TelegramClient` to avoid actual session deserialization during tests.
- **Dialog-warmup ordering test (PR #48)**: pin that `iter_dialogs()` is called BEFORE `get_entity(channel)`. A refactor that reorders or removes the warmup would silently regress the private-channel resolution path.
- **Watchdog cooldown test (PR #47)**: assert `_WATCHDOG_ALERT_COOLDOWN_SEC` prevents log spam during sustained staleness — alert fires at 5 min, suppressed at 6/7/8 min, fires again at 20 min, etc.
- **Session-edge suppression (PR #47)**: ensure `_in_session_edge_window` returns True during weekends / daily CME break / Friday cutoff so the watchdog doesn't cry wolf during legitimate market closures.

---

## 9. Pitfalls from prior sessions

Cumulative — see HANDOFF_v10 §9. Session 12 additions:

- **"`keepUpToDate=True` will work as long as the historical seed worked"** — false. The seed uses HMDS (no live entitlement); the live update channel needs LIVE entitlement and fails silently on delayed (§0.5.180).
- **"IB Gateway will auto-resume subscriptions after a farm flap"** — false. Socket stays up, sub dies, no auto-resume (§0.5.181). PR-R1 is the fix.
- **"`docker compose restart` will pick up new .env values"** — false (§0.5.179). Use `up -d --force-recreate`.
- **"Private Telegram channel by title will resolve from a fresh StringSession"** — false (§0.5.182). Need iter_dialogs() warmup.
- **"A handoff's claim that 'operator already did X' about external state can be trusted"** — false (§0.5.183). Broker / external API state always wins; verify carry-forward claims via probe.
- **"$1.55/mo CME Real-Time NP,L1 covers MNQ futures"** — false. $1.55 is OPRA (options). CME futures L1 non-pro is the "US Securities Snapshot and Futures Value Bundle" at ~$10/mo.
- **"Error 10197 means external IBKR login is contesting the data line"** — sometimes false. Can also be a stuck-gateway internal artifact cleared by a gateway restart. Distinguish by stopping the orchestrator and re-probing with a single fresh client.

**Next session rule: if a claim is quantitative or about external state, re-verify it before acting. Especially: market-data entitlement tier, container env vars, session string presence/length, subscription status from broker probes.**

---

## 10. Session discipline lesson (2026-05-28)

**Chat-side me made three wrong diagnoses this session before the actual root cause** (stuck-session → competing-login → wiring-bug → actual: delayed entitlement). Plus a brief mis-call on "Track A done" before the farm-flap regression. The "rule of twice-wrong" almost triggered.

**Pattern**: chat-side hypotheses kept jumping a layer too narrow. CC VPS, by gathering raw evidence each round (standalone probes, broker direct queries, source-of-truth checks), pushed the diagnosis DOWN until it landed at the entitlement layer.

**The right corrective**: RESOLVE mode (§0.5.177). Chat-side authors the goal + decision tree; CC VPS gathers and acts; chat-side doesn't bounce mid-decision unless a genuinely external blocker shows up. Two ratification moments this session:
- Operator: *"Is there an option for me to have the same discussion with cc vps directly instead of having it with you and then pasting it to cc vps?"* — workflow friction signal.
- Operator: *"I want your prompts to be end to end processes for cc vps - that it knows that it should continue working until the issue is resolved, and we're coming back to this chat only for the ongoing context and for external input."* — contract upgrade.

W-S12.1 and L-S12.2 demonstrated RESOLVE mode end-to-end. The operator round-trip count dropped sharply: from "every probe → chat → next probe" to "one work order → CC VPS does ~all steps → operator clicks/pastes the genuinely-external bits → DONE."

**Enforcement rules for next session:**
1. **RESOLVE mode is the default** for any multi-step ops/diagnostic work. Code-only PR work can still be REPORT/AUDIT.
2. **Broker / external API state > any handoff claim** (§0.5.183). Always probe carry-forward external claims before trusting them.
3. **Long-secret pastes need length + SHA256 anchors** (§0.5.178). No exceptions for session strings, API keys, migration SQL, etc.
4. **For "0 of X" probes, always test with an independent minimal-wiring probe** before assuming app code is the bug. If both fail, the bug is below the app.
5. **"GOAL met" isn't durable until the bot has survived hours of normal operational noise** (farm flaps, transient disconnects, daily resets). Don't declare victory on a single 5-min observation window.

---

## 11. Logging verbosity standards

Carry from HANDOFF_v10 §11. Session 12 additions:

- **`[WATCHDOG]`** namespace (PR #47):
  - `[WATCHDOG] no live bar in <m>m during session — feed delayed/dead` (WARN)
  - `[WATCHDOG] bar feed recovered — first bar after stale window` (WARN, recovery)
  - `[ALERT] watchdog_stale_bars: stale_min=<n>` (INFO, for Telegram alerter)
  - `[ALERT] watchdog_bar_recovered` (INFO, for Telegram alerter)

- **`[TG_LISTENER]`** namespace (PR #48 addition to existing):
  - `[TG_LISTENER] dialog cache warmed — count=<N>` (INFO, post-warmup)
  - `[TG_LISTENER] connected — channel=<name> id=<int>` (existing, INFO)
  - `[TG_LISTENER] signal — type=<X> parsed_ok=<bool> msg_id=<int>` (existing, INFO)

- **`/status` Telegram command** now includes `live_bars_60m: <N>` line (PR #47).

---

## 12. Master template — `pr_brief_template.md` v2

Unchanged from v10. The v2 template at `.claude/skills/code-pr-brief/pr_brief_template.md` on origin/main is canonical. Reference `CLAUDE.md §<section>` rather than re-pasting standing rules.

**RESOLVE mode work orders** (W-S12.1, L-S12.2 patterns) have a different structure than code PR briefs — see those work orders for the template. Key differences:
- Mode header: `## Mode: RESOLVE`
- `## GOAL (resolved =)` block defining verifiable success criteria
- Tracks (parallel work streams)
- `⛔ NEEDS OPERATOR: <exact action>` for external blocks
- No autonomy-level header (RESOLVE supersedes AUTO/REPORT/AUDIT for the work order; individual PRs within it carry their own autonomy)

---

## 13. Current PR brief in flight — PR-R1 (RESOLVE mode work order, draft)

For Session 13. Draft below; chat-side may refine before handing to CC VPS.

```
# TradeFlow — Work Order R-S13.1 (Mode: RESOLVE) — Auto-resubscribe on IB farm flap

## Mode: RESOLVE (per §0.5.177)
Run end-to-end. Self-verify every step. Do NOT stop at probe findings; if a blocker
is fixable autonomously, fix it; if it needs the operator, post "⛔ NEEDS OPERATOR:
<exact action>" and pause. Resume automatically on reply.

## GOAL (resolved =)
After a simulated or natural IB market-data farm flap (Warning 2103 + Warning 2105
+ Error 10182), the orchestrator detects it, calls subscribe_bars again, [BAR]
resumes within 60s, and a Telegram alert "[ALERT] bar_sub_resubscribed_after_farm_flap"
fires. Verified end-to-end from app's own logs + Telegram alerter + Supabase
lifecycles state remains stable.

## ROOT CAUSE (proven — §0.5.181)
IB Gateway does NOT auto-resume keepUpToDate after a farm flap. The orchestrator
socket stays up so PR-A reconnect doesn't fire. Bars silent until manual restart.

## TRACK A — Implement auto-resubscribe (AUDIT-level PR)
A.1 Read src/clients/ib_client.py subscribe_bars + its caller in src/orchestrator.py
    (_start_bar_subscription). Understand the contract handle / args reusability.
A.2 Subscribe to ib_async error events (ib.errorEvent += handler) and watch for the
    2103 + 2105 + 10182 trio within a short window (~5s).
A.3 On detected trio, after a brief debounce (~2-3s for farm to recover via 2104/2106),
    invoke subscribe_bars again with the same contract + args.
A.4 Emit logs:
    [ORCH] bar_subscription auto-resubscribed after farm-flap — elapsed_sec=<X>
    [ALERT] bar_sub_resubscribed_after_farm_flap: elapsed_sec=<X>
A.5 Idempotency: skip resubscribe if one was created in last 30s (avoid double-arm).
A.6 Tests: simulate the farm-flap event sequence, assert resubscribe is called,
    assert log + alert emitted, assert dedup of rapid-fire flaps.

## TRACK B — Smoke verify post-merge (autonomous)
B.1 Operator types `merge` (AUDIT-level acks).
B.2 CC VPS: squash-merge, force-recreate tradeflow-app, watch logs for 10 min.
B.3 If a natural farm flap occurs in the window, confirm the auto-resubscribe fires.
    Otherwise, leave it running — natural flaps happen multiple times per day.

## Constraints
- DO NOT touch order/strategy/kill-switch code (out of scope).
- DO NOT touch PR-A reconnect logic (different layer — socket-level, not data-farm-level).
- Tests must avoid real ib_async network calls (mock the IB client).

## Report cadence
- After Task A.6 tests green + ready for merge.
- After post-merge smoke window (B.3).
- ✅ GOAL: confirmed auto-resubscribe on natural or simulated flap.
```

---

## 14. Canonical references (in order of authority)

1. **`origin/main` at HANDOFF_v11's publish commit** — verified system reality
2. **`CLAUDE.md` at repo root on `origin/main`** — operating manual auto-loaded by CC VPS; will be updated to mention RESOLVE mode in a future docs PR
3. **Source code on `origin/main`** at `209a37c` (PR #48 merge) — what actually runs
4. **Production Supabase via service role** (`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` in `.env`) — truth for row/column data
5. **IBKR API via `ib_async`** — truth for positions/orders/account/MARKET DATA ENTITLEMENT TIER (§0.5.183)
6. **IBKR Client Portal → Settings → Market Data Subscriptions** — operator-only authoritative source for entitlement state
7. **Telegram alerter output** (real-time) — fast signal for production state changes
8. **`.claude/skills/` on `origin/main`** — autonomy contract spec + templates
9. **This handoff (v11)** — session context, NOT long-term authority
10. **v10 and earlier handoffs** — historical; **ignore if they contradict 1–7**. v10 §0.5.171 in particular is WRONG about CME entitlement (§0.5.183).
11. **Aggregated grep / dashboard metrics** — do not trust in isolation (§prod-debug-discipline)

---

## 15. First 15 minutes of the next session

1. **Operator pastes `focus_brief_session_13.md`** (separate doc — see `docs/handoffs/focus_brief_session_13.md` on origin/main). Sets chat-side context.
2. **Chat-side me reads** §0.5 banner (NEW §0.5.177–§0.5.183), §1 (live state), §2 (the arc), §5 (wrong diagnoses — CRITICAL), §7 (priority queue), §13 (PR-R1 draft brief).
3. **CC VPS runs the §0.5.165 pre-flight scan** as first action. Reports local-vs-origin divergence, open PRs, container state.
4. **CC VPS runs §6 V0–V8** verification block. Confirms post-end-of-session-restart bot state, listener still connected, entitlement still live, any new farm flap since handoff.
5. **Decide based on V3/V8 state:**
   - If [BAR] flowing healthily + no recent farm flap → proceed to drafting/shipping PR-R1.
   - If [BAR]=0 + watchdog firing → another farm-flap regression happened overnight; manual restart, then ship PR-R1 with elevated priority.
   - If listener disconnected → re-verify TELEGRAM_SESSION_STRING in .env + PR #48 deployed + force-recreate.
6. **Hand PR-R1 draft (§13) to CC VPS as a RESOLVE work order.** Refine the draft if any §6 evidence changes the scope (e.g., if a different farm-flap signature appears).
7. **Parallel observation track**: while PR-R1 is being implemented, accumulate empirical data — count [STRAT] eval decisions over the RTH window. Does it ever fire `long_signal`? What's the noop_filter_or_regime rate? Cross-reference with any captured `seanbot_signals` rows.

---

## 16. How to publish this handoff

**Path chosen for v11: operator scp's both files to /tmp on the VPS, then pastes the publish brief to CC VPS.**

**Operator steps** (from local machine where the handoff files were saved):

```bash
# Adjust path to wherever you saved the files
scp HANDOFF_v11.md focus_brief_session_13.md tradeflow@5.78.212.37:/tmp/
```

Then paste the CC VPS publish brief (provided separately in chat) — it branches from `origin/main`, places both files under `docs/handoffs/`, commits, pushes, opens a PR, waits CI, squash-merges, cleans up `/tmp`. AUTO autonomy (docs only).

**Path B — Manual fallback if CC VPS unavailable:**

```bash
scp HANDOFF_v11.md focus_brief_session_13.md tradeflow@5.78.212.37:/tmp/
ssh tradeflow@5.78.212.37 "cd /home/tradeflow/tradeflow \
  && git fetch origin \
  && git checkout -b claude/handoff-v11 origin/main \
  && mkdir -p docs/handoffs \
  && cp /tmp/HANDOFF_v11.md docs/handoffs/HANDOFF_v11.md \
  && cp /tmp/focus_brief_session_13.md docs/handoffs/focus_brief_session_13.md \
  && git add docs/handoffs/HANDOFF_v11.md docs/handoffs/focus_brief_session_13.md \
  && git commit -m 'docs: add v11 handoff (LIVE bars + listener connected + RESOLVE mode)' \
  && git push -u origin claude/handoff-v11 \
  && gh pr create --base main --head claude/handoff-v11 --title 'docs: add v11 handoff' --body 'Session 12 closeout' \
  && gh pr merge --squash --delete-branch"
```

The handoff exists only when `origin/main` has it. Until merged, treat as draft.

---

*End of handoff v11. Target lifespan: until PR-R1 ships and the bot has survived ≥48h of normal market-data farm flaps without manual restart, AND we have at least one full RTH session of [STRAT] eval + seanbot_signals data side-by-side. Then write v12 with empirical strategy comparison findings and the first lifecycles.*
