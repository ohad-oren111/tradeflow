# TradeFlow — Handoff v10 (24/5 bar feed unblocked, observability complete, SeanBot capture deployed)

*Handoff from end of 2026-05-27/28 (Session 11). Bot is healthy on the Hetzner VPS, both containers Up, RestartCount=0, no positions. Six PRs shipped clean: PR-D1 ([BAR] log), PR-D2 (`use_rth=False`), PR-D3a ([STRAT] eval log), PR-D3b (SeanBot telethon listener), plus CLAUDE.md at repo root and the PR-D2 follow-up corrections. The big structural unlock this session: 24/5 bar feed is now working (CME Real-Time subscription added by operator), per-bar strategy decision trace is live, and the SeanBot comparison harness is deployed but waiting on operator bootstrap (60 sec). Tomorrow 09:30 ET RTH open is the first real empirical test of the strategy with full instrumentation. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

**Read order**: §0.5 (standing rules, especially new §0.5.170–§0.5.176), §1 (live state), §5 (wrong diagnoses — chat-side confabulated 6 times this session), §6 (verification block — run before any code), §7 (pending work).

**The §6 verification block is the first command of the next session.** Bot is healthy at handoff time but the empirical question — does the strategy actually fire entries when bars flow extended-hours and during RTH — is unanswered. Don't ship more strategy code until V5/V6 land.

**Two operator tasks are queued** (not blockers for code work but blockers for SeanBot comparison):
1. Apply Supabase migration `supabase/migrations/20260528004557_seanbot_signals.sql` via Supabase dashboard SQL editor
2. Run telethon bootstrap one-liner over SSH (60 sec), paste result into `.tradeflow-secrets/.env`

The autonomy contract (AUTO/REPORT/AUDIT) from v9 §0.5 banner is the operating mode. Now reinforced by `CLAUDE.md` at repo root which auto-loads for CC VPS every session.

---

## 0.5 Standing rules (permanent — cumulative across sessions; do not remove)

**Carry-forward from HANDOFF_v9 §0.5.1 – §0.5.169.** All prior rules remain in force. New rules from Session 11 appended below as §0.5.170 – §0.5.176.

---

### 🎯 §0.5 BANNER — The automation strategy: AUTO / REPORT / AUDIT (unchanged from v9)

Every PR brief carries `## Autonomy Level: <LEVEL>`. CC VPS executes per level:

| Level | Scope | Operator role |
|---|---|---|
| **AUTO** | Docs, config tweaks, log format, tests-only, dependency patch bumps | Zero. Read structured report after CC VPS auto-merges. |
| **REPORT** | Bug fixes ≤5 files with strong test coverage, refactors with no public-API change | One word in chat: `merge` (or `stop`) |
| **AUDIT** | Order execution, strategy, kill switch, secrets, multi-file >50 LOC, broker-state-altering | Open PR in GitHub, scan diff (~2-5 min), type `merge`. |

Default if ambiguous: REPORT.

**NEW this session**: `CLAUDE.md` at repo root (PR #43, `aab7f1b`) auto-loads the operating manual for CC VPS every session. Future PR briefs reference `CLAUDE.md §<section>` instead of re-pasting standing rules. Briefs are ~30–40% shorter as a result.

---

### §0.5.170 — Brief authors MUST grep before claiming stack/codebase facts

This session, chat-side me confabulated 6 times in PR briefs (Redis in stack, Next.js dashboard, $1.50/mo CME subscription, `tradeflow_default` network, `telegram_session_data` volume, extracted `regime_ok`/`touch_ok` booleans). CC VPS caught all six via 5-second grep probes and corrected before shipping. Every confabulation was a §0.5.97 violation.

**Rule**: when a brief makes a factual claim about the codebase (file paths, function signatures, network names, volumes, variable extraction, dependency presence, env var names), the brief author MUST grep the repo first or mark `VERIFY IN A.X`. Never bake unverified claims into "Files you WILL modify" / "Constraints" / "Architecture" sections.

**Why it matters**: a confabulated brief that ships unchecked = wrong tests, wrong patches, hours of cleanup. CC VPS's audit caught the issues this session but a future AUTO-level brief might slip through.

### §0.5.171 — IBKR HMDS Error 162 = market-data subscription tier mismatch, NOT a code bug

When `reqHistoricalDataAsync` returns very few bars (1–5) compared to a baseline (~400+) on the same instrument/timeframe/contract, the smoking gun is the IBKR log line:

```
ERROR ib_async.wrapper Error 162, reqId N:
  Historical Market Data Service error message:
  HMDS query returned no data: <SYMBOL>@<EXCHANGE> Trades
```

This means the IBKR account lacks the relevant data subscription for the requested scope. Diagnosed during PR-D2 smoke: `useRTH=False` returned 1 bar (vs 450 with `useRTH=True`) because the paper account didn't have CME Real-Time data. Subscribed via IBKR Client Portal → Settings → Market Data Subscriptions → CME Real-Time (NP, L1) at $1.55/mo. Within ~15 min, paper account inherited the data; probe went 1 → 97 → 110 → 121 bars across successive runs.

**Rule**: when an HMDS Error 162 appears alongside an unexpectedly low bar count, check IBKR subscription tier BEFORE code-debugging.

### §0.5.172 — Per-bar observability logs require live bar closes

`[BAR]` (PR-D1) and `[STRAT] eval` (PR-D3a) only emit when the ib_async callback fires with a closed bar. During low-volume windows (Wed evening extended hours, weekends, CME maintenance break 17:00–18:00 ET Mon-Thu), bars may not close every minute even with `useRTH=False`. F.3 timeouts in smoke tests during these windows are EXPECTED, not a regression.

**Rule**: if [BAR] or [STRAT] eval log smoke fails, check the current ET clock against CME session boundaries before declaring a bug.

### §0.5.173 — IBKR paper account mirrors live account market-data subs

Paper accounts inherit the live account's market-data subscriptions (~15 min propagation after live-account change). You cannot subscribe paper-only. The live account is the source of truth for what data the paper account sees.

### §0.5.174 — Telethon `StringSession` is preferred over file-based sessions

`StringSession` produces a serializable blob that lives in `.env`. No docker volume needed for session persistence. Clean restart semantics: re-reading the env var fully recovers state. Generated once by the operator via `scripts/bootstrap_telegram_session.py` over interactive SSH.

### §0.5.175 — AUDIT merge doesn't require re-confirmation

When operator types `merge` on an AUDIT PR, CC VPS proceeds with merge + Task F autonomously. Do NOT re-ask "are you sure?" or pause for additional confirmation. Operator already made the decision.

### §0.5.176 — Listener / aux services use `unless-stopped` + idle loop, NOT one-shot return

When a service can't initialize (e.g. telethon listener with no session string), it should idle with `while True: await asyncio.sleep(60)` rather than `return` + restart-policy-driven loop. The one-shot-return pattern creates a 60-second crash loop that increments RestartCount and confuses watchdogs. Documented as defect in PR-D3b, slated for PR-D3b.1 fix.

---

## 1. Where we are (as of handoff, 2026-05-28 ~01:15 UTC)

### Live production state

- `tradeflow-app` container: **Up ~1h13m, healthy, RestartCount=0** (StartedAt=2026-05-28T00:10:08Z, post-PR-D3a rebuild)
- `tradeflow-ib-gateway` container: Up, healthy, RestartCount=0
- `tradeflow-telegram-listener` container: **Up ~5m, "health: starting" (broken healthcheck, see §5)**, RestartCount climbing (crash-loop while idle — defect, see §5)
- IBKR paper account: `DUQ331660`, NetLiq ~$1,000,514 (mostly base $1M + paper interest accrual), positions=[]
- Lifecycles ever: **0** (strategy has never fired an entry — see §1.3 for why)
- Telegram alerter: validated, polling every 30s
- Telegram listener: running but idle ("no session — run bootstrap" log every 60s)
- CME Real-Time (NP,L1) market-data subscription: **ACTIVE** on live IBKR account, inherited by paper (~22:30 UTC propagation)

### What just shipped (Session 11 — six PRs total, in order)

- **PR #41** (`6cca18a`) — **PR-D1** `feat(observability): [BAR] log in bar callback adapter`. 2 files, +71/-0. Adds one INFO log per closed bar inside `_adapter` in `src/clients/ib_client.py:334`, gated by `has_new_bar`. Closes the instrumentation gap discovered during PR-D survival probe attempt.
- **PR #42** (`1b0cad5`) — **PR-D2** `fix(orchestrator): subscribe bars with use_rth=False for 24/5 strategy`. 2 files, +53/-1. Production orchestrator now passes `use_rth=False` to `subscribe_bars()` at `src/orchestrator.py:315`. Surfaced IBKR subscription tier gap during smoke (HMDS Error 162). Operator added CME Real-Time subscription → fix is now end-to-end valid.
- **PR #43** (`aab7f1b`) — **CLAUDE.md** `docs: replace Phase 0 CLAUDE.md with full CC VPS operating manual`. 1 file, +122/-24. Auto-loads on every Claude Code session. Centralizes stack, harness denials, autonomy contract, common confabulations, session start protocol, T-series operational rules. CC VPS caught 2 chat-side confabulations during this PR (Redis, Next.js) and corrected.
- **PR #44** (`79935f7`) — **PR-D3a** `feat(observability): per-bar [STRAT] eval log for full decision trace`. 2 files, +77/-12. Refactors `Sma100BounceStrategy.on_new_bar()` to single-return with one INFO log emitting `[STRAT] <sym>: eval ts=<X> close=<Y> ma_fast=<F> ma_slow=<S> gap=<G> cooldown=<bool> decision=<str>`. Decision categories: `noop_cooldown` | `noop_session_edge` | `noop_warmup` | `long_signal` | `noop_filter_or_regime`. The last collapses regime-block and entry-filter-block (inline booleans not extracted per brief constraint — see §7 PR-D3b.1).
- **PR #45** (`587d6b2`) — **PR-D3b** `feat(listener): SeanBot Telegram signal capture`. 9 files, +551/-0. New `telegram-listener` docker-compose service (separate from trading bot), `src/listeners/seanbot_parser.py` (regex-based, handles Unicode minus + em-dash), `src/listeners/telegram_listener.py` (telethon main loop, `StringSession`, idempotent Supabase upsert), `scripts/bootstrap_telegram_session.py`, `supabase/migrations/20260528004557_seanbot_signals.sql`. Listener idle pending operator bootstrap.

### What we discovered this session (operational facts not yet in code)

- **PR-D2's "fix" actually exposed a second-layer bug**. After `use_rth=False` shipped, observable behavior didn't change (still 0 bars) because IBKR paper account lacked CME extended-hours data. A/B probe pre-subscription: useRTH=True → 450 bars, useRTH=False → 1 bar. Post-subscription: useRTH=False → 121 bars (and climbing as subscription window grows).
- **`Sma100BounceStrategy.on_new_bar` originally had 4 early-return branches** (cooldown / session_edge / warmup / no signal). PR-D3a refactored to single-return so the eval log fires exactly once per call.
- **`detect_signal` (module-level function in `src/strategy.py`) has inline boolean expressions** for regime_ok / touch_ok / ma_order_ok / bullish_ok / gap_ok — NOT extracted as named variables. The PR-D3a eval log captures the final decision but cannot distinguish regime-block from filter-block. PR-D3b.1 (queued) will extract the booleans.
- **Migration pattern**: `supabase/migrations/<yyyymmddhhmmss>_<name>.sql`, applied via Supabase dashboard SQL editor (no psql/CLI on VPS, no SUPABASE_DB_URL in `.env`). CC VPS does NOT run migrations directly.
- **Docker network is `tradeflow-net`** (not `tradeflow_default` as chat-side me confabulated). Verified via `docker compose config --services`.
- **Telethon `StringSession`** outputs a base64-ish blob storable in `.env`. No docker volume needed.
- **`tradeflow-app` Dockerfile HEALTHCHECK uses `main.py' in /proc/1/cmdline`** — when reused for the listener service (different entrypoint), healthcheck fails. PR-D3b.1 will disable healthcheck on the listener.

### Why the bot has 0 lifecycles (revised understanding)

Multi-causal — Session 10 attributed it solely to "pullback regime hasn't fired yet during residual window," but it's actually three stacked issues:

1. **PR-#38 (Session 10) deploy timing** — strategy was only in production from 14:12 ET 2026-05-27 onward; SeanBot's 3 entries today (11:46/12:00/12:12 ET) all preceded deploy
2. **`use_rth=True` default starved bars 70% of the configured 24/5 window** — pre-PR-D2 the bot was blind from 16:00 ET each day until 09:30 ET next day
3. **Pullback regime parameters may genuinely not have triggered** in the ~1h48m of in-RTH operation today; market may simply not have provided a setup

Issues 1+2 are now resolved. Issue 3 will be observable at tomorrow's 09:30 ET RTH open via [STRAT] eval logs.

---

## 2. The session's work thread

1. **Pre-flight + V0-V5 verification** from Session 10's HANDOFF_v9 §6. V0-V4 clean PASS. V5 (overnight transient_disconnect/recovered pattern) returned 0 events because container had only restarted ~2h before — pre-dated the overnight test window. CC VPS made the sharp catch.
2. **Plan pivot**: instead of waiting 7.5h for organic overnight, ship the bar subscription survival probe (PR-D) which forces a controlled flap and gives empirical data immediately. Halted at Task A because no `[BAR]` log line existed in the codebase. Pivoted to PR-D1 (add the log) first.
3. **PR-D1 shipped** (`#41`). One-line `LOGGER.info("[BAR] %s: new — close=%.2f ts=%s", ...)` inside `_adapter`. Task F.3 wait for first [BAR] **timed out** — initially baffling. CC VPS isolated root cause: `use_rth=True` default + post-RTH-close timing (16:51 ET).
4. **PR-D2 shipped** (`#42`). Flipped `use_rth=False` for production caller. CI green, AUDIT autonomy, operator approved merge. Task F.3 **timed out again**. CC VPS ran A/B probe directly inside container — `useRTH=True` returned 450 bars, `useRTH=False` returned 1. **HMDS Error 162** — subscription tier mismatch, not code bug. Surfaced cleanly because we added the [BAR] log first.
5. **Operator added CME Real-Time (NP,L1) subscription** ($1.55/mo) via IBKR Client Portal. Chat-side me confabulated the price first as $1.50, then as $10 (during a "correction"), before settling on the actually-correct $1.55. Two §0.5.97 violations in one conversation thread.
6. **CLAUDE.md shipped** (`#43`). Workflow upgrade — auto-loads operating manual for CC VPS every session. CC VPS caught and corrected 2 confabulations in chat-side me's draft (Redis, Next.js) during implementation.
7. **PR-D3a shipped** (`#44`). Per-bar `[STRAT] eval` log in `on_new_bar`. Single-return refactor done cleanly. The `noop_filter_or_regime` decision label collapses two distinct gate failures because `detect_signal` has inline-only booleans — flagged as PR-D3b.1 follow-up.
8. **Operator added Telegram API creds** (api_id 37365068, api_hash, channel "Trading NQ Triggers") via SSH one-liner to `.tradeflow-secrets/.env`. Chat-side me confabulated `tradeflow_default` network name and `telegram_session_data` volume in the PR-D3b brief — CC VPS corrected both before shipping.
9. **PR-D3b shipped** (`#45`). Telethon listener service, parser, bootstrap script, DB migration, tests. CI green, operator merged. Task F revealed two minor defects: (a) restart-loop crash pattern when idle, (b) healthcheck inherits from `main.py` probe and always fails on the listener. Both bounded — neither blocks operator's bootstrap work. PR-D3b.1 queued.
10. **Bot at end of session**: healthy, both trading containers Up RestartCount=0, listener idle with proper "no session — run bootstrap" log line, Supabase migration pending operator action.

**Meta-arc**: started Session 11 thinking we'd validate PR-A overnight, ended having shipped 6 PRs that took the bot from "literally blind 70% of configured hours, zero observability" to "24/5 vision + per-bar decision trace + comparison harness deployed." Empirical strategy validation begins tomorrow at RTH open.

---

## 3. What the system is actually made of

**Single source of truth:** `origin/main` at commit `587d6b2` (HANDOFF_v9's reference point was `c5eedd9`, plus this session's six merges: `6cca18a`, `1b0cad5`, `aab7f1b`, `79935f7`, `587d6b2`).

Production-live code paths (verified deployed in `tradeflow-app` container at handoff time):
- `src/orchestrator.py` — main loop with PR-A resilience, PR-#37 EOD + reconciler, **PR-D2 `use_rth=False`** at L315/L320
- `src/clients/ib_client.py` — IBClient wrapper + `connect_with_resilience()` + `BrokerExtendedOutageError` + **PR-D1 `[BAR]` log** at L334
- `src/strategy.py` — `Sma100BounceStrategy` + `_regime_ok()` C1 gate + `_in_session_edge_window()` + **PR-D3a `[STRAT] eval` log** at L395 (refactored to single-return)
- `src/execution/bracket.py` — bracket-order builder with `tif="GTC"` on TP child
- `src/execution/force_close.py` — Friday-only EOD at 16:25 ET
- `src/execution/reconciler.py` — drain + scan loops (unchanged this session)
- `comms/telegram.py` — alerter (unchanged this session)
- `config/risk_params.py` — single config dataclass
- `main.py` — entrypoint

**NEW this session:**
- `src/listeners/__init__.py` (empty)
- `src/listeners/seanbot_parser.py` — regex parser, 9 tests
- `src/listeners/telegram_listener.py` — telethon main loop, runs as separate service
- `scripts/bootstrap_telegram_session.py` — one-time interactive auth
- `supabase/migrations/20260528004557_seanbot_signals.sql` — schema (pending operator apply via dashboard)
- `CLAUDE.md` at repo root — operating manual

**Docker services** (after PR-D3b):
- `tradeflow-app` (container_name = service name) — main bot
- `ib-gateway` (service) / `tradeflow-ib-gateway` (container_name) — IBKR gateway
- `telegram-listener` (service) / `tradeflow-telegram-listener` (container_name) — SeanBot capture

**`.claude/skills/`**: unchanged this session, but CLAUDE.md at repo root is the new entry point. v2 PR brief template still authoritative for AUDIT/REPORT PRs.

---

## 4. Verified facts (cumulative — carry forward + this session)

**From HANDOFF_v9 §4 (carry forward verbatim):**

- MNQ specs: TICK_SIZE=0.25, MULTIPLIER=$2/point, COMMISSION_RT=$0.62 RT, MARGIN_REQ=$2,000 day-trade
- Compose service `ib-gateway` ≠ container_name `tradeflow-ib-gateway`
- Pytest is NOT in prod container — host venv at `/home/tradeflow/tradeflow/.venv/bin/pytest`
- `.tradeflow-secrets/.env` shadows compose `${VAR:-default}` patterns
- Branch off `origin/main`, never local `main`
- Harness denies destructive git verbs (reset --hard, rebase, push --force, branch -D, commit --amend)
- Harness denies bare `sleep <N>` (use `timeout <max> bash -c 'until <cond>; do sleep 2; done'`)
- Harness denies `docker exec <c> env` (use `docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}'`)
- IBKR client_id = 1 for orchestrator
- Strategy timeframe: 1-min bars (Sma100BounceStrategy)

**NEW load-bearing facts this session:**

- **`subscribe_bars` wrapper** at `src/clients/ib_client.py:282` signature: `(self, contract, *, bar_size, what_to_show, use_rth, duration, on_new_bar)`. Default `use_rth: bool = True`. Internally forwards as camelCase `useRTH=` to `ib_async.IB.reqHistoricalDataAsync`. Production orchestrator (post-PR-D2) passes `use_rth=False` explicitly at `src/orchestrator.py:315/320`.
- **`ib_async.useRTH` semantics on futures**: `True` ⇒ Regular Trading Hours only (09:30–16:00 ET for MNQ); `False` ⇒ full CME 24/5 session minus the daily 17:00–18:00 ET maintenance break. No futures-specific override.
- **CME data subscription**: CME Real-Time (NP,L1) at $1.55/mo covers MNQ at L1. Waived at $20/mo commissions threshold (~32 MNQ RT). Non-Professional tier. Paper inherits within ~15 min of live-account change.
- **HMDS Error 162** = subscription tier mismatch. Always check IBKR portal first before code-debugging.
- **Docker network name**: `tradeflow-net` (NOT `tradeflow_default`). Verified via `docker compose config --services`.
- **`_adapter` callback** (PR-D1) fires on every tick when `keepUpToDate=True`. Gate on `has_new_bar` to log only on bar close — otherwise 10–100 logs/sec.
- **`Sma100BounceStrategy.on_new_bar(bar: dict) -> Signal | None`** at `src/strategy.py:342` is the per-bar entry point. Post-PR-D3a it's single-return with one `[STRAT] eval` log emitted from one site at L395.
- **`detect_signal` (module-level in `src/strategy.py`)** has inline boolean expressions for regime/filter gates — not extracted as named variables. PR-D3b.1 will refactor to expose them so the eval log can distinguish `noop_regime` from `noop_filter`.
- **Migration pattern**: `supabase/migrations/<yyyymmddhhmmss>_<name>.sql`, applied via Supabase dashboard SQL editor. NO psql, NO supabase CLI, NO SUPABASE_DB_URL on the VPS. CC VPS instructs operator to paste-and-run.
- **Telethon `StringSession`** stores session as a serializable blob in `.env` as `TELEGRAM_SESSION_STRING=...`. No volume needed.
- **Dockerfile HEALTHCHECK** uses `main.py' in /proc/1/cmdline` — fails on any service with a different entrypoint. Aux services should override with `healthcheck: { disable: true }` or a process-alive check. PR-D3b.1 will fix the listener.

---

## 5. Wrong diagnoses this session — READ BEFORE YOU DEBUG

**Chat-side me confabulated 6 times this session. CC VPS caught all 6 via probe. This is the most important section for the next session to internalize.**

1. **"Redis is in the stack" (PR-D3a brief / CLAUDE.md draft)**
   - Evidence I cited: "Redis — state cache" baked into stack lock
   - Why wrong: zero grep hits in `src/`, `comms/`, `dashboard/`, `pyproject.toml`, `requirements.txt`, `docker-compose.yml`. No external cache in this project. State lives on `Orchestrator`, `DirtySet`, `Reconciler`, `halt` flag — all in-process.
   - Correct: removed from CLAUDE.md by CC VPS before merge.

2. **"Next.js dashboard" (CLAUDE.md draft)**
   - Evidence I cited: knew dashboard ran on `localhost:8080`, assumed Next.js
   - Why wrong: `dashboard/server.py` imports `uvicorn`, `fastapi.FastAPI`, `fastapi.templating.Jinja2Templates`. Zero Node/Next.js anywhere.
   - Correct: FastAPI + uvicorn + Jinja2, served at `127.0.0.1:8080` with HTTP Basic auth via `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`.

3. **"$1.50/mo CME Real-Time" → "$10 bundle correction" → "$1.55 was actually right"**
   - Evidence: memory-based pricing claim, no IBKR docs probe
   - Why wrong (twice): first claim was a stale memory of the price. The "correction" to the $10 US Securities Snapshot bundle was a different IBKR product entirely. Re-grounded against current IBKR pricing docs (2026-04): CME Real-Time (NP,L1) is $1.55/mo, waived at $20/mo commissions.
   - Correct: $1.55/mo, that's the one operator subscribed to.

4. **"`tradeflow_default` network" (PR-D3b brief)**
   - Evidence I cited: docker-compose convention is `<projectname>_default`
   - Why wrong: project uses an explicit `networks: tradeflow-net` block in `docker-compose.yml`, not the default convention.
   - Correct: `tradeflow-net`. CC VPS fixed before shipping.

5. **"`telegram_session_data` volume" (PR-D3b brief)**
   - Evidence I cited: telethon `.session` SQLite file pattern
   - Why wrong: we explicitly chose `StringSession` (env var) over file sessions. No volume needed at all.
   - Correct: no volume; session string lives in `.env`. CC VPS removed the volume declaration.

6. **"`regime_ok` / `touch_ok` / `gap_ok` as named booleans" (PR-D3a brief)**
   - Evidence I cited: assumed `detect_signal` extracted them as variables
   - Why wrong: they're inline boolean expressions inside `detect_signal`. Extracting them would require a refactor of `detect_signal` (forbidden by the brief).
   - Correct: PR-D3a uses a collapsed `decision` label (`noop_filter_or_regime`). PR-D3b.1 will refactor to expose the booleans cleanly.

**Lesson for next session (meta-pattern):** Every confabulation this session was a fact about the codebase that could have been resolved by a 5-second grep before writing the brief. I had high confidence on each claim and was wrong on each. **Standing rule §0.5.170 now mandates grep-before-bake for any factual claim about the codebase in PR briefs.** Use `VERIFY IN A.X` placeholders aggressively. CC VPS is the probe; let it discover ground truth — don't assume.

**Secondary lesson**: When making a "correction" to a prior claim, re-ground against an authoritative source. The $10/$1.55 thrash happened because the first "correction" was made from memory, not from IBKR docs. If you're correcting yourself, probe twice as hard.

---

## 6. Verification block — run this before doing anything

**V0 — Confirm origin/main HEAD and bot health**
```bash
git -C ~/tradeflow fetch origin
git -C ~/tradeflow log --oneline -1 origin/main
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"
```
Expect: origin/main HEAD = HANDOFF_v10 merge commit (or later). Three containers: `tradeflow-app` (Up healthy), `tradeflow-ib-gateway` (Up healthy), `tradeflow-telegram-listener` (Up — status varies depending on whether bootstrap is done).

**V1 — Bot state baseline**
```bash
docker inspect tradeflow-app --format "RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}} Health={{.State.Health.Status}}"
docker inspect tradeflow-ib-gateway --format "RestartCount={{.RestartCount}} Health={{.State.Health.Status}}"
docker inspect tradeflow-telegram-listener --format "RestartCount={{.RestartCount}} State={{.State.Status}}"
```
Expect: trading containers RestartCount=0 (unchanged from handoff baseline). Listener RestartCount may climb due to §0.5.176 defect — not a regression, will be fixed by PR-D3b.1.

**V2 — PR-D1 + PR-D2 + PR-D3a present in deployed code**
```bash
docker exec tradeflow-app grep -nE "\[BAR\] %s: new" /app/src/clients/ib_client.py
docker exec tradeflow-app grep -nE "use_rth=use_rth|use_rth = False" /app/src/orchestrator.py
docker exec tradeflow-app grep -nE "\[STRAT\].*eval ts=" /app/src/strategy.py
```
Expect: 1 hit each (line numbers ~334, ~315/320, ~395).

**V3 — Live [BAR] + [STRAT] eval flow (RTH-dependent)**
```bash
docker logs tradeflow-app --since 10m 2>&1 | grep -cE "\[BAR\]"
docker logs tradeflow-app --since 10m 2>&1 | grep -cE "\[STRAT\].*eval ts="
```
Expect during RTH (09:30–16:00 ET) or active extended hours: ~10 each in a 10-min window. Outside session hours (weekends, daily break 17:00–18:00 ET, low-volume evening): may be 0. If 0 during RTH, investigate per §0.5.171/172.

**V4 — Telegram listener state**
```bash
docker logs tradeflow-telegram-listener --since 5m 2>&1 | grep -E "\[TG_LISTENER\]" | tail -5
```
Decision tree:
- `[TG_LISTENER] no session — run scripts/bootstrap_telegram_session.py` → operator hasn't bootstrapped yet (see §7 task)
- `[TG_LISTENER] connected — channel=Trading NQ Triggers id=<N>` → listener is live and capturing
- `[TG_LISTENER] signal — type=<X> parsed_ok=<Y> msg_id=<Z>` → signals are flowing into Supabase

**V5 — Supabase `seanbot_signals` table exists (post-migration)**
```bash
# From the VPS, hit Supabase REST API directly (assumes SUPABASE_URL + service role key in .env)
source /home/tradeflow/.tradeflow-secrets/.env
curl -s -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  "$SUPABASE_URL/rest/v1/seanbot_signals?select=count&limit=1" -H "Prefer: count=exact" -i | head -10
```
Expect: HTTP 200 + `content-range: 0-0/<N>` header. If 404 or 42P01 error, migration not applied — operator must paste `supabase/migrations/20260528004557_seanbot_signals.sql` into Supabase dashboard SQL editor.

**V6 — Overnight long-tail (carry from v9 §6 V5)**
```bash
docker logs tradeflow-app --since 12h 2>&1 | grep -cE "\[ORCH\] healthcheck: transient_disconnect|\[ALERT\] reconnect_recovered"
docker logs tradeflow-app --since 12h 2>&1 | grep -E "\[CONN\] reconnect attempt" | head -5
```
Expect overnight: transient_disconnect count == reconnect_recovered count. If they diverge or RestartCount climbed, PR-A has a hole — investigate per §prod-debug-discipline.

---

## 7. Pending work queue (priority order)

### 1. Operator tasks (both ~60 seconds each, do whenever convenient)
- **F.2 — Apply Supabase migration**: Open Supabase dashboard → SQL editor → paste contents of `supabase/migrations/20260528004557_seanbot_signals.sql` → Run. Expect "Success. No rows returned." Then run V5 to confirm.
- **F.4 — Telethon bootstrap**: Run this over SSH:
  ```bash
  ssh tradeflow@5.78.212.37
  docker run --rm -it \
    --env-file /home/tradeflow/.tradeflow-secrets/.env \
    tradeflow-tradeflow-app:latest \
    python scripts/bootstrap_telegram_session.py
  ```
  Follow prompts (phone number → Telegram-sent code). Copy the printed `TELEGRAM_SESSION_STRING=<blob>` line, append to `/home/tradeflow/.tradeflow-secrets/.env`. Then `docker compose restart telegram-listener`. Verify with V4 — expect `[TG_LISTENER] connected — channel=Trading NQ Triggers`.

### 2. PR-D3b.1 — fix listener defects (REPORT, ~5 LOC)
- Bug A: `await asyncio.sleep(60); return` in `src/listeners/telegram_listener.py` creates restart-loop. Fix: `while True: await asyncio.sleep(60)`.
- Bug B: listener inherits `tradeflow-app`'s HEALTHCHECK which probes for `main.py' in /proc/1/cmdline` — always fails on listener. Fix: add `healthcheck: { disable: true }` to telegram-listener service in `docker-compose.yml`, OR add per-service healthcheck using a process-alive check.
- Files: `src/listeners/telegram_listener.py`, `docker-compose.yml`. ≤5 LOC total.
- Autonomy: REPORT.

### 3. Empirical observation window (no code work)
After operator F.2 + F.4 done and tomorrow 09:30 ET RTH opens, OBSERVE for 1–2 hours:
- Are [BAR] logs firing every minute? (V3)
- Are [STRAT] eval logs firing every minute with sensible field values? (V3)
- What decision values appear? Mostly `noop_filter_or_regime`? Any `long_signal`?
- Are seanbot_signals rows being inserted? (V5 with `?select=*&limit=20`)
- Does TradeFlow fire any entry when SeanBot does?

**Don't ship more code until this empirical window produces data.** §0.5.97 in action.

### 4. PR-D3c — Daily comparison digest (REPORT, ~150 LOC)
After 1 full RTH session of data flows, draft this. Joins `seanbot_signals` (by ts) against `[STRAT] eval` logs (need to either write logs to Supabase too, OR parse log files server-side). Categorizes `AGREE_ENTER` / `AGREE_NOOP` / `MISS` (SeanBot enters, we don't) / `FALSE_POSITIVE` (we enter, SeanBot doesn't). Pushes daily Telegram digest at 16:30 ET.

Architecture decision pending — depends on what the data shape looks like in practice. Don't pre-design.

### 5. PR-S1 — Secret rotation + log redaction (AUTO after operator rotates)
- IBKR_PASSWORD in plaintext via `docker inspect tradeflow-app` `.Config.Env`
- Telegram bot token leaks in app logs (every httpx GET to api.telegram.org/bot<TOKEN>/getUpdates)
- Move env vars from compose `environment:` to `env_file:` mount
- Add httpx logger config to redact `bot<TOKEN>` path
- Operator manual prerequisite: rotate IBKR paper password in IBKR portal + rotate Telegram bot token via BotFather

### 6. Kill switch PR (AUDIT)
- 4-layer drawdown caps: 1.5% daily / 3% weekly / 6% monthly / 12% trailing-account
- Reference equity hard-coded $100K (NOT $1M paper NAV)
- Real-money readiness gate

### 7. Watchdog tuning (DEFERRED — see v9 §7 PR-C)
Don't touch unless 48h of post-PR-A telemetry shows watchdog-induced cascades. PR-A may have made it moot.

### Gaps carried forward (low priority)
- **G1** — C1 regime gate fail-open in production (buffer 150 1-min vs threshold 202 30-min). Currently fires no-op every decision. Lower-priority now that PR-D3a eval log will show whether it matters empirically.
- **G2** — `risk_params.py:signal_scan_start_et` comment misleading. PR-D3b's adjacency check noted: grep returns no occurrences in `src/` or `config/` — the identifier may have been renamed or dropped during PR-#32/#33 rebase. Confirm and either remove from G-list or update.
- **G3** — Seed depth 45 vs required SMA warmup 100. Related to G1. Tactical fix.

### Operational debt (low priority)
- Local `main` ref still drifts cosmetically post-squash-merge (§0.5.168 — known, non-blocking)
- `risk_params.py` docstring carries old PR-#10-era language. 5-line doc PR if anyone cares.
- Orphan branches on origin from various sessions — harmless, `gh api -X DELETE` when convenient.

---

## 8. Test safety — cumulative

Carry forward from HANDOFF_v9 §8 verbatim. New this session:

- **Single-return refactor caveat (PR-D3a)**: when a brief specifies "exactly one log record per call", the implementation must be single-return — early-return branches produce zero or multiple log records depending on path. Test asserted `len(eval_lines) == 1` to catch this.
- **Telethon mocking**: CC VPS chose not to unit-test the listener loop directly in PR-D3b, only the parser. Integration test = operator running bootstrap + live message capture. Acceptable for AUDIT autonomy where operator verifies behavior empirically. Don't add brittle telethon mock tests in PR-D3b.1 unless the bug fix specifically exercises a code path that needs unit coverage.

---

## 9. Pitfalls from prior sessions

Cumulative — see HANDOFF_v9 §9. Session 11 additions:

- **"Compose default network is `<projectname>_default`"** — false when `networks:` block is explicit. Always `docker compose config --services` + read the actual config.
- **"Dockerfile HEALTHCHECK applies the same to every service"** — true mechanically but breaks aux services with different entrypoints. Override per-service.
- **"Telethon needs a docker volume for session"** — false when using `StringSession`.
- **"IBKR paper account market-data subs can be configured paper-side"** — false. Live account is the source of truth; paper inherits.
- **"`useRTH=True` is the safe default"** — false for any 24/5 strategy. Explicit `False`.
- **"Confabulated tech-stack claims in briefs ship cleanly because they look detailed"** — false. CC VPS's grep catches them. But don't make them in the first place — §0.5.170.

**Next session rule: any factual claim in a PR brief about the codebase (file paths, network names, volumes, dependencies, variable names) requires either a prior grep or a `VERIFY IN A.X` placeholder. No exceptions.**

---

## 10. Session discipline lesson (2026-05-28)

Chat-side me confabulated 6 times this session in PR briefs. CC VPS caught all 6 via 5-second grep probes. The cost of each confab was ~30 seconds of CC VPS audit time; the cost of NOT catching them would have been wrong tests, wrong patches, hours of cleanup. The system worked because CC VPS treats every brief as suspect and probes — but the brief author should have probed first.

**Enforcement rules for next session:**
1. **§0.5.170 — grep before baking** any codebase fact into a PR brief. No exceptions.
2. **§0.5.97 — probe external specs** (broker contracts, exchange fees, schema, library APIs) against source before quoting numbers. This includes IBKR pricing, IBKR data subscription names, ib_async signatures, supabase-py vs custom client patterns.
3. **CC VPS clarification questions get answered inline by chat-side me** (§0.5.169). When CC VPS surfaces a STOP-and-report on a confabulation, chat-side me revises the brief — not the operator.
4. **Handoff publish PR opens within last 30 min of session** (§0.5.154). No drift to next session.

The bigger meta-lesson: chat-side me has poor calibration on "codebase facts I know" vs "codebase facts I'm pattern-matching from similar projects." Default to assume the latter and grep.

---

## 11. Logging verbosity standards

Carry from HANDOFF_v9 §11. Session 11 additions:

- New `[BAR]` namespace (PR-D1): `[BAR] <symbol>: new — close=<float> ts=<iso>` on each closed bar
- New `[STRAT] eval` shape (PR-D3a): `[STRAT] <symbol>: eval ts=<iso> close=<f> ma_fast=<f> ma_slow=<f> gap=<f> cooldown=<bool> decision=<str>` once per `on_new_bar` call. Field order is fixed; downstream parsing can rely on it.
- New `[TG_LISTENER]` namespace (PR-D3b):
  - `[TG_LISTENER] no session — run scripts/bootstrap_telegram_session.py` (startup, idle)
  - `[TG_LISTENER] connected — channel=<name> id=<int>` (post-auth)
  - `[TG_LISTENER] signal — type=<str> parsed_ok=<bool> msg_id=<int>` (per inbound message)
  - `[TG_LISTENER] supabase write failed — msg_id=<int> err=<str>` (recoverable, logged + continue)

---

## 12. Master template — `pr_brief_template.md` v2

Unchanged from v9. The v2 template at `.claude/skills/code-pr-brief/pr_brief_template.md` on origin/main is canonical. With `CLAUDE.md` at repo root now auto-loading, future briefs can reference `CLAUDE.md §<section>` instead of re-pasting Harness Denials / Environmental Quick-Reference / most Known Gotchas. Briefs are ~30–40% shorter as a result. The v2 template's Autonomy Level header, Task A audit, Task F level-aware smoke, and "What I got wrong" sections remain mandatory.

---

## 13. Current PR brief in flight (if any)

**Recommended first PR for next session: PR-D3b.1** (REPORT autonomy):

```
# TradeFlow — PR-D3b.1 — Fix telegram-listener defects (REPORT)

## Autonomy Level: REPORT
Two small defects in PR-D3b (idle restart loop + broken healthcheck). Bug fix
≤5 files w/ strong test coverage. Operator types `merge` after CI green.

## Context
PR-D3b shipped the SeanBot telethon listener. Two defects surfaced in Task F:

1. When no TELEGRAM_SESSION_STRING is set, `main()` logs the error then does
   `await asyncio.sleep(60); return`. Container exits 0, `restart: unless-stopped`
   restarts it, infinite loop. RestartCount climbs every 60s.

2. The listener service reuses the `tradeflow-app` Docker image, which inherits
   a HEALTHCHECK that probes for `main.py' in /proc/1/cmdline`. Listener's
   cmdline is `python -m src.listeners.telegram_listener` — no `main.py` match,
   healthcheck always fails. Container reports `(unhealthy)` even in steady state.

Neither blocks the operator's bootstrap work. Watchdog doesn't probe the
listener so neither defect affects the trading bot. But both should be fixed
before we trust the listener's operational state for any monitoring.

## Files
WILL modify (EXACTLY 2):
- src/listeners/telegram_listener.py — change `return` to `while True: await asyncio.sleep(60)`
- docker-compose.yml — add `healthcheck: { disable: true }` to telegram-listener service

MUST NOT modify:
- src/orchestrator.py, src/strategy.py, src/clients/, src/execution/
- supabase/migrations/
- .tradeflow-secrets/

## Mission

### Task A — Audit
A.1: Read src/listeners/telegram_listener.py main() function. Confirm the
     `return` after the sleep is at the bottom of the no-session branch.
A.2: Read docker-compose.yml telegram-listener service block. Confirm no
     existing healthcheck override.
A.3: Verify the existing HEALTHCHECK in Dockerfile that's being inherited.

### Task B — Implement
B.1: In telegram_listener.py, replace:
       LOGGER.error(...)
       await asyncio.sleep(60)
       return
     With:
       LOGGER.error(...)
       while True:
           await asyncio.sleep(60)
B.2: In docker-compose.yml under telegram-listener service, add:
       healthcheck:
         disable: true

### Task C — Tests
No new unit tests needed — both fixes are operational (loop pattern + compose
config). Verification is post-merge via Task F observing RestartCount and
health status.

### Task D — Verify
git diff origin/main --stat  → expect 2 files
grep -nE "while True.*sleep|disable: true" src/listeners/telegram_listener.py docker-compose.yml

### Task E — Adjacency
None — out of scope.

### Task F — Post-merge (REPORT, operator types `merge`)
F.1: Pull + rebuild + recreate ONLY the listener:
  cd /home/tradeflow/tradeflow
  git fetch origin && git checkout origin/main
  docker compose build telegram-listener
  docker compose up -d --force-recreate telegram-listener

F.2: Verify the idle loop pattern (no restart over 3 min):
  START=$(date -u +%s)
  timeout 200 bash -c 'while [ $(($(date -u +%s) - '$START')) -lt 180 ]; do sleep 10; done'
  docker inspect tradeflow-telegram-listener --format "RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}}"
  # Expect: RestartCount unchanged from pre-rebuild baseline

F.3: Verify healthcheck is disabled (no health status reported):
  docker inspect tradeflow-telegram-listener --format "Health={{.State.Health}}"
  # Expect: "Health=<nil>" or no Health block

F.4: Confirm idle log is still present:
  docker logs tradeflow-telegram-listener --since 3m 2>&1 | grep "\[TG_LISTENER\] no session"
  # Expect: 1+ matches (should fire every 60s now without restart)

## Known Gotchas (per CLAUDE.md)
- docker compose service name = container_name for telegram-listener? Verify with `docker compose config --services`
- `healthcheck: { disable: true }` is the compose syntax; do NOT use `healthcheck: NONE`

END OF BRIEF — REPORT autonomy, operator types `merge` after CI green.
```

Drop into CC VPS after V0–V6 verification and operator has done F.2 + F.4 (or not — PR-D3b.1 is independent of operator tasks).

---

## 14. Canonical references (in order of authority)

1. **`origin/main` at HANDOFF_v10's merge commit** — verified system reality
2. **`CLAUDE.md` at repo root on `origin/main`** — operating manual auto-loaded by Claude Code; supersedes any conflicting claim in this handoff
3. **Source code on `origin/main`** — what actually runs
4. **Production Supabase via service role** — truth for row/column data
5. **IBKR API via `ib_async`** — truth for positions/orders/account
6. **Telegram alerter output** (real-time) — fast signal for production state changes
7. **`.claude/skills/` on `origin/main`** — autonomy contract spec + PR brief template
8. **This handoff (v10)** — session context, NOT long-term authority
9. **v9 and earlier handoffs** — historical; ignore if they contradict 1–7
10. **Aggregated grep / dashboard metrics** — do not trust in isolation (§prod-debug-discipline)

---

## 15. First 15 minutes of the next session

1. **Operator pastes the focus brief** (separate doc — see `focus_brief_session_12.md`). Sets context for chat-side me.
2. **Chat-side me reads** §0.5 banner + §0.5.170–§0.5.176 (new rules), §1 (live state), §5 (wrong diagnoses — CRITICAL to internalize the confabulation patterns), §7 (priority queue).
3. **CC VPS runs §0.5.165 pre-flight scan** as first action. Reports local-vs-origin divergence, open PRs, container state.
4. **CC VPS runs §6 V0–V6** verification. Confirms code deployed, listener state, and (post-operator-actions) Supabase table.
5. **Pick next priority based on operator F.2/F.4 state:**
   - If F.2 + F.4 done → chat-side drafts PR-D3b.1, ships REPORT-level, waits for operator merge
   - If F.2 not done → chat-side reminds operator; PR-D3b.1 can ship in parallel (doesn't depend on migration)
   - If F.4 not done → same; PR-D3b.1 doesn't depend on session string either
6. **If RTH is open (09:30–16:00 ET)**: observe [BAR] + [STRAT] eval flow for 30 min. Capture sample lines. This is the empirical data we've been waiting for.
7. **Don't ship more code** beyond PR-D3b.1 until empirical data from a full RTH session is available.

---

## 16. How to publish this handoff

**Path A — Autonomous via CC VPS (preferred per §0.5.161):**

Operator pastes the dedicated CC VPS publish brief (separate file `publish_handoff_v10_brief.md` or inline below). CC VPS:
- Branches off origin/main
- Writes `docs/handoffs/HANDOFF_v10.md` with this entire document content verbatim
- Commits, pushes, opens PR with proper title/body
- Waits for CI green
- AUTO-merges (squash + delete branch)
- Posts structured session-closeout report

AUTO autonomy. No operator gates.

**Path B — Manual scp fallback (only if VPS CC unavailable):**

```bash
scp HANDOFF_v10.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v10.md
ssh tradeflow@5.78.212.37 "cd /home/tradeflow/tradeflow && git checkout -b claude/handoff-v10 origin/main && git add docs/handoffs/HANDOFF_v10.md && git commit -m 'docs: add v10 handoff (24/5 bar feed + observability + SeanBot capture)' && git push origin claude/handoff-v10 && gh pr create --base main --head claude/handoff-v10 --title 'docs: add v10 handoff' --body 'Session 11 closeout'"
```

The handoff exists only when origin/main has it. Until merged, treat as draft.

---

*End of handoff v10. Target lifespan: until PR-D3b.1 ships, telethon auth is bootstrapped, and we have at least one full RTH session of [STRAT] eval + seanbot_signals data side-by-side. Then write v11 with empirical strategy comparison findings.*
