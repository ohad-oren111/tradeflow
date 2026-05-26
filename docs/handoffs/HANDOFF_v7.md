# TradeFlow — Handoff v7 (PR #16 + PR #18 shipped Friday; dashboard live on laptop; first RTH session Tuesday 2026-05-26)

*Handoff from end of Session 8, Friday 2026-05-22 evening (~22:00 UTC). **Three PRs shipped this session** (PR #15 was Session 7; PR #16 + PR #18 this session). Orchestrator is **running in paper** on PR #18 code (commit `80d4a2d` or later) on Hetzner VPS `5.78.212.37`. **Dashboard is live and reachable from operator's laptop via SSH tunnel + HTTP Basic auth.** Phone access deferred (Termius unresolved; operator deprioritized). **Monday 2026-05-25 is Memorial Day — markets closed. First real RTH session is Tuesday 2026-05-26 09:30 ET (signal-eligible from 09:35 ET).** Session 9 inherits a fully instrumented + alert-able + dashboard-monitored bot, with PR #19 (earnings + trade log) as the only remaining code PR before Tuesday observation.*

---

## 0. How to use this doc

Read sections 1–6 first — state-of-the-system as of handoff. Sections 7–13 are reference material. §14 is the single-file source of truth to consult when this handoff disagrees with itself or a live observation: source code on `main` at commit `80d4a2d` or later.

**Do not trust this doc alone.** Per §0.5.145 (codified this session), every live-state claim in §1 is timestamped to when it was last probed. Re-run §6 V0-V6 before writing any code or making decisions.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

### Baseline (every project, from handoff_template.md)

**Copy-paste instruction style.** Every action recommended to the operator must be a copy-paste-ready bash block. Self-contained commands. Source env vars in the same block. Expected output described immediately below each block. Decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**Learning-delivery discipline.** Every new fact discovered (bug pattern, corrected assumption, environmental fact, diagnostic finding) gets surfaced immediately as a markdown snippet for the running handoff queue. Do not wait until end-of-session.

**Read before diagnosing.** For complex state bugs, read the full startup log and 3-5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency language, hit the source of truth — live API, live DB, raw log file — not aggregated metrics.

**Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise.

### Carried forward verbatim from HANDOFF_v6 §0.5

§0.5.97–.144 + §0.5.T1–T5 — see `docs/handoffs/HANDOFF_v4.md` + `v5.md` + `v6.md` in repo on `main` for the full enumeration. Key entries:

- **§0.5.97 (probe-before-specify)** — Probe external specs against source before baking into briefs. Single most common source of wrong PRs.
- **§0.5.98 / §0.5.123 (broker is ground truth)** — Broker/exchange state is ground truth for position/fill/capital claims, NOT internal DB tables.
- **§0.5.117/.118 (bash discipline)** — Hardcoded heuristics: `cd X &&`, `;` separators, `$(...)`, `${VAR}`, heredocs, chained `sleep`. Workarounds in HANDOFF_v6.
- **§0.5.130** — Strategy sticky as `strategy="sma100_bounce"`.
- **§0.5.137** — Imports vs Dockerfile COPY. New top-level packages MUST be COPYed.
- **§0.5.139** — Class-method name collision. Grep before adding.
- **§0.5.140** — Probe column names against schema before specifying `select=`.
- **§0.5.141** — Pre-populated `settings.local.json` with comprehensive project-scope grants.
- **§0.5.142** — Multi-line `python -c` trips a Claude Code heuristic. Stage to `/tmp/<name>.py` via Write tool.
- **§0.5.143** — Telegram `parse_mode=Markdown` is fragile with underscore-containing content. Plain text only.
- **§0.5.144** — Secrets audit on every config-file merge.
- **§0.5.T1–T5** — IBKR/bracket invariants. See HANDOFF_v4.

### New this session (Session 8) — append-only

- **§0.5.145 — Handoff facts that describe live state require a same-session probe before being written.** Don't transcribe "the migration was applied" or "the container restarted at X" or "positions=Y" into the handoff from memory or from your own narration earlier in the session. If it's a live-state fact, run the probe (HTTP, docker inspect, broker API, raw log read), capture the output, then write the fact. This extends §0.5.97 (probe-before-specify) and §0.5.98/.123 (live state is ground truth) into the handoff-authoring workflow specifically. Session 8 failure mode: HANDOFF_v6 §1/§4 confidently claimed `halt_acks` was applied when it wasn't — Session 8 had to re-discover via a 404 probe.

- **§0.5.146 — Validate-before-schedule for async tasks.** Credential/config validation for an `asyncio.create_task(...)` must happen synchronously *before* the task is scheduled, not inside the coroutine. The try/except around `create_task` only catches errors from `create_task` itself; errors raised inside the coroutine fire when the loop next yields, not during the synchronous path. PR #18 brief had `run_uvicorn` raise `RuntimeError` for missing credentials, expected to be caught by orchestrator's try/except around `create_task` — wouldn't have worked. VPS CC caught it during implementation and exposed `load_credentials()` as public, calling it synchronously before `create_task`. Future briefs for async tasks with config validation must use this pattern.

- **§0.5.147 — Dual-manifest dependency gotcha.** TradeFlow has TWO dependency manifests: `requirements.txt` (Docker image manifest, read by the Dockerfile RUN pip install) and `pyproject.toml [project.dependencies]` (read by CI via `pip install -e ".[dev]"`). They are NOT auto-synced. PR #18 added deps to `requirements.txt` only and CI failed with `ModuleNotFoundError`. Future PR briefs adding runtime dependencies must explicitly list BOTH files and require updates to both. The PR #18 brief's MUST-NOT-MODIFY list for `pyproject.toml` was too broad — it should have allowed `[project.dependencies]` additions explicitly.

- **§0.5.148 — Container loopback ≠ host loopback in Docker.** Binding a service to `127.0.0.1` *inside* a container makes it unreachable from outside the container — including through Docker port mapping. The container has its own network namespace; its loopback is unrelated to the host's. "Loopback-only externally" defense lives in the docker-compose `ports:` spec (`127.0.0.1:HOSTPORT:CONTAINERPORT`), NOT the in-container bind address. **Container should bind `0.0.0.0`; host-side mapping is the security boundary.** PR #18 brief specified loopback-bind inside container as "defense layer 1" which was incorrect; the actual layer 1 was already in place (compose mapping `127.0.0.1:8080:8080`). Session 8 fix: added `DASHBOARD_BIND=0.0.0.0` to `~/.tradeflow-secrets/.env`, force-recreated. §0.5.97 violation — Docker networking model was assumed without probing.

- **§0.5.149 — Bash history expansion on `!` triggers even inside double quotes.** Passing credentials on the command line with `!` followed by digits or letters triggers bash history expansion, regardless of double-quoting. Single quotes DO suppress it (single-vs-double asymmetry). Either (a) read the value from a file via `$(grep ... | cut ...)` so the literal value never appears on the command line, OR (b) use single quotes around the credential. PR #18 smoke test used double-quoted credentials and the operator's password contained a `!`-sequence that bash interpreted as history expansion. Future smoke-test briefs that pass credentials on the command line MUST use pattern (a).

- **§0.5.150 — `docker compose restart` does NOT reload `env_file`.** `env_file` is read at container *creation*, not start. Changes to `~/.tradeflow-secrets/.env` (or any env_file) require `docker compose up -d --force-recreate <service>` to take effect. `restart` re-runs the same container with the same env it was created with. Session 8 discovery during the loopback fix: setting `DASHBOARD_BIND=0.0.0.0` in `.env` required `--force-recreate` to land.

---

## 1. Where we are (as of Friday 2026-05-22 ~22:00 UTC)

### Live production state — last probed at ~21:53 UTC

**§0.5.145 disclosure:** facts below are timestamped to the operator's last probe at ~21:53:50 UTC on Friday 2026-05-22. By the time Session 9 opens, these are stale — re-probe via §6 V0-V6.

**Probed evidence at 21:53 UTC:**
- HEAD on `main`: `80d4a2d` (PR #18 squash, GitHub PR #27)
- `tradeflow-app`: was `(healthy)` as of `docker ps` ~21:14 UTC; force-recreated ~21:40 UTC during loopback fix
- `tradeflow-ib-gateway`: `(healthy)`
- IBKR paper account `DUQ331660`, server version 178
- positions = `[]`, openTrades = `[]`
- NetLiq = $1,000,085.80, AvailableFunds = $1,000,000.00, BuyingPower = $4,000,000.00 (from dashboard screenshot)
- Supabase tables `lifecycles`, `lifecycle_events`, `halt_acks` all reachable (status=200), all 0 rows
- Telegram subsystem: `/status`, `/halt`, `/ack`, `/flatten`, `/exit SYMBOL`, `/confirm` all wired
- Dashboard subsystem: live at `127.0.0.1:8080` inside container, reachable from VPS host loopback via docker-compose `127.0.0.1:8080:8080` mapping, gated by HTTP Basic auth
- Container env: `DASHBOARD_BIND=0.0.0.0`, `DASHBOARD_USERNAME=ohad_tradeflow`, `DASHBOARD_PASSWORD=<redacted, stored in ~/.tradeflow-secrets/.env>`

**Operator workflow at session close:**
- Laptop access: opens `ssh -L 8080:localhost:8080 tradeflow@5.78.212.37` manually each session, browser to `http://localhost:8080`. Persistent SSH tunnel via launchd NOT set up (operator declined).
- Phone access: NOT working (Termius port-forward config issue, deprioritized).
- Telegram alerts: still primary "tell me when something happens" channel.

**Calendar:**
- Sunday 2026-05-24 normal
- **Monday 2026-05-25 is Memorial Day — markets closed**
- First RTH bar after handoff: **Tuesday 2026-05-26 09:30:00 ET. Strategy signal-eligible from 09:35:00 ET** (5-min session-edge buffer per `risk_params.session_edge_no_trade_minutes=5`).

### What just shipped (Session 8)

- **PR #16 (GitHub #26, squash `43bf65c`)** — `/flatten` + `/exit SYMBOL` + `/confirm` operator commands behind 60s-TTL confirm step. 5 files / +900/-4. Added: `comms/telegram.py` (PendingAction + 3 handlers + format helpers), `src/orchestrator.py` (FlattenResult/ExitResult + flatten_all/exit_symbol), `src/execution/router.py` (CloseResult + close_position + register_manual_exit + _manual_orders set), tests. 202 tests cumulative (186 baseline + 16 new). F3 phone smoke PASSED end-to-end.

- **PR #18 (GitHub #27, squash `80d4a2d`)** — Dashboard skeleton + 4 broker-sourced read-only panels. 18 files / +990/-0. Added: `dashboard/` package (FastAPI server + state aggregator + 4 partial templates + base.html + index.html + CSS), `src/clients/ib_client.py` (`get_account_summary()`), `src/orchestrator.py` (dashboard task launch in `_launch_background_tasks`), Dockerfile (COPY + EXPOSE), docker-compose.yml (`127.0.0.1:8080:8080`), requirements.txt + pyproject.toml [project.dependencies] (fastapi + uvicorn + jinja2), tests. 221 tests cumulative (202 + 19 new). HTTP Basic auth via `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` env vars. Defense in depth: host-side loopback mapping (compose) + Basic auth (app).

- **Operator-side fix (post-merge, no PR)** — `DASHBOARD_BIND=0.0.0.0` added to `~/.tradeflow-secrets/.env` to fix the container-loopback issue (§0.5.148). Container force-recreated. Dashboard then reachable from VPS host loopback.

- **Supabase migration applied (operator-side, no PR)** — `halt_acks` table created in live Supabase via dashboard SQL editor. PR #12 migration SQL existed in repo but had never been applied (HANDOFF_v6 narrative was wrong — §0.5.145 born). Verified via httpx probe: `lifecycles`/`lifecycle_events`/`halt_acks` all 200.

### What we discovered this session

- **§0.5.145** — HANDOFF_v6 §1/§4 claimed `halt_acks` was applied when it wasn't. Discovered via Session 8 V0-V6 probe returning 404 PGRST205 ("Could not find the table 'public.halt_acks'"). Operator pasted migration SQL into Supabase dashboard; re-probe returned 200.

- **§0.5.146** — Async task credential validation pattern. VPS CC caught during PR #18 implementation.

- **§0.5.147** — Dual-manifest dependency requirement. Discovered when PR #18's first CI run failed with `ModuleNotFoundError: fastapi` despite the dep being in `requirements.txt`. Operator-flagged-by-VPS-CC transparently; tiny follow-up commit synced `pyproject.toml`.

- **§0.5.148** — Container loopback ≠ host loopback. Discovered when post-merge curl from VPS host to `127.0.0.1:8080` returned "Recv failure: Connection reset by peer" while curl from INSIDE the container to `127.0.0.1:8080` would have worked (and `/proc/net/tcp` confirmed uvicorn was listening on container's loopback). Fix: bind 0.0.0.0 in container; host-side `127.0.0.1:8080:8080` mapping is the actual security boundary.

- **§0.5.149** — Bash history expansion in credentials. Operator's password contained `!`-pattern that triggered bash history expansion in double-quoted curl `-u` argument. Fix: read from `.env` via `$(grep ... | cut ...)`.

- **§0.5.150** — `docker compose restart` does NOT reload `env_file`. Required `up -d --force-recreate` for `DASHBOARD_BIND` to land.

- **Cold-boot IB Gateway race (carry-over from Session 7)** — confirmed expected. `tradeflow-app` first start races against `tradeflow-ib-gateway`'s IBC login, gets `TimeoutError`, Docker auto-restarts, second start succeeds. RestartCount=1 after `docker compose up --build --force-recreate` is normal. Not addressed this session; watch-item for HANDOFF_v8 + future PR.

- **last_heartbeat_at on dashboard is `n/a`** — no per-tick heartbeat timestamp stored on Orchestrator currently. `PanelStatus.last_heartbeat_at` is `None`; `fetched_at` on each panel doubles as freshness. Future PR could add a 5-line orchestrator change to store a heartbeat timestamp.

- **uvicorn `log_level="warning"` masks the startup banner.** PR #18's dashboard server set uvicorn log_level to "warning" which hid the standard `Uvicorn running on http://...` message. During the loopback bug debug, this made it look like uvicorn never bound. If dashboard hangs again, raising temporarily to `log_level="info"` would surface the bind step. Possible HANDOFF_v8 §0.5.151 candidate if we hit it again.

---

## 2. The session's bug + build thread

1. **Session opened** Friday May 22 evening (~04:10 UTC Saturday in UTC terms, end-of-Friday-ET) with post-Session-7 V0-V6 verification. **Two anomalies surfaced**: (a) `halt_acks` 404 from Supabase reachability probe, (b) `RestartCount=1` on `tradeflow-app`. Per HANDOFF_v6 §10, applied prod-debug-discipline: probe before fixing. VPS CC ran two diagnostic probes simultaneously.

2. **halt_acks diagnosis**: VPS CC's probe confirmed PGRST205 — table genuinely missing. Migration SQL existed in repo at `supabase/migrations/20260522150934_halt_acks.sql`. **HANDOFF_v6 narrative was wrong**: claimed migration applied during Session 7 dashboard SQL editor, but actually was never applied (or apply silently failed). **§0.5.145 born.** Operator pasted SQL into Supabase dashboard, "Success. No rows returned", re-probe returned 200. Resolved without code.

3. **RestartCount diagnosis**: VPS CC's `docker inspect` showed `ExitCode=0`, empty Error, 156ms FinishedAt→StartedAt gap. Consistent with the cold-boot IB race we'd already characterized in Session 7. Benign. No action.

4. **PR #16 designed**: `/flatten` + `/exit SYMBOL` + `/confirm` operator commands with 60s TTL confirm step. Naturally coupled (shared close primitive). Brief shipped via chat artifact, 5 mandatory pr-brief-lint checks at design time. VPS CC's Task A audit caught 3 chat-side brief slips that didn't make it to code: `src/execution/router.py` (NOT `order_router.py`), close_position for ACTIVE positions returns asynchronously, lifecycle event column is `payload` (NOT `event_data`). All corrected before implementation. Result: 16 new tests, 202 cumulative passing, CI green. Merged at `43bf65c`. Post-merge F1 had a path typo in my chat-side reminder; F2 had benign cold-boot RestartCount=1; F3 phone smoke from operator passed cleanly with all 6 interactions returning expected behavior.

5. **PR #18 designed v1 (Tailscale)**: First attempt at dashboard brief specified Tailscale-bound deployment. Operator rejected ("can't use Tailscale right now, overkill"). Rewrote brief v2: SSH tunnel + HTTP Basic auth + loopback binding. VPS CC's Task A audit caught 2 brief slips: (a) `src/clients/ib_client.py` had no `get_account_summary` — added one; (b) the actual class file is `src/execution/router.py` (carryover lesson from PR #16). Implementation went clean, 19 new tests, 221 cumulative.

6. **PR #18 ships with 2 deviations VPS CC flagged transparently**:
   - **Deviation A** — chat-side me specified `run_uvicorn` raising `RuntimeError` inside the coroutine for missing credentials, caught by orchestrator's try/except around `create_task`. VPS CC realized this wouldn't work (§0.5.146) — `create_task` schedules but doesn't run. Exposed `load_credentials()` as public, called synchronously before scheduling. Solid catch.
   - **Deviation B** — Brief listed `pyproject.toml` MUST-NOT-MODIFY. CI installs via `pip install -e ".[dev]"` reading `[project.dependencies]`. First CI run failed with `ModuleNotFoundError: fastapi`. Tiny follow-up commit synced the 3 new deps into pyproject.toml. §0.5.147 born.

7. **PR #18 merged at `80d4a2d`** via auto-merge. Operator rebuilt container.

8. **Dashboard not reachable from VPS host** (post-merge smoke) — curl from VPS to `127.0.0.1:8080` returned "Connection reset by peer". `ss -tlnp` returned no listener visible from host. Dashboard task launched per `[ORCH] dashboard: task_launched` log; `[DASH] server: starting` logged; but no uvicorn startup banner (because log_level=warning hid it). Initial diagnosis (chat-side or parallel chat): "uvicorn hanging in lifespan". Wrong.

9. **§0.5.148 discovered**: Operator's parallel chat probed `/proc/net/tcp` from INSIDE the container, found `0100007F:1F90` (127.0.0.1:8080 in LISTEN state). Uvicorn WAS bound — just to the container's loopback, which Docker can't reach via port mapping. Container loopback ≠ host loopback. Fix: `DASHBOARD_BIND=0.0.0.0` in `.env`. **§0.5.150 also surfaced** (`docker compose restart` doesn't reload env_file; needed `--force-recreate`).

10. **Dashboard end-to-end verified**:
    - VPS curl no-auth → 401 ✓
    - VPS curl with-auth → 200 ✓
    - Operator's laptop browser via SSH tunnel → 200 + Basic auth prompt + all 4 panels rendered with real numbers (NetLiq $1,000,085.80, etc.) + 10s auto-refresh confirmed via `fetched_at` advancing 21:52:04 → 21:53:50 UTC

11. **Phone access stuck**: Operator's Termius port-forward config not working. Deprioritized ("nbd"). Persistent SSH tunnel via launchd suggested for laptop friction; operator declined for now.

12. **PR #19 (earnings + trade log) NOT shipped this session**. Brief not yet drafted. Kill switch dropped from PR #19 scope per PM call (operator already has `/flatten` on Telegram, redundancy not worth the CSRF/form-handling complexity).

13. **Session 8 closes** with dashboard live on laptop, three PRs merged, six new standing rules codified, Tuesday 2026-05-26 09:30 ET as the upcoming binary. PR #19 is the only remaining code item before Tuesday observation.

**Rabbit holes closed this session:**
- ❌ "uvicorn hanging in async loop" — wrong. Uvicorn was bound, just in container's loopback. Real cause was Docker network namespace.
- ❌ "halt_acks is applied per the handoff" — wrong. Migration was never run. §0.5.145.
- ❌ "Tailscale is the right answer for dashboard auth" — operator-rejected. SSH tunnel + Basic auth path adopted.
- ❌ "Dashboard kill switch is worth shipping in PR #19" — PM-dropped. Telegram `/flatten` covers it.

---

## 3. What the system is actually made of

**Single source of truth:** `git -C ~/tradeflow ls-tree -r HEAD --name-only` on `main` at `80d4a2d` or later.

Highlights:

- **3 containers**: `tradeflow-app` (orchestrator PID 1 — runs orchestrator + reconciler + EOD scheduler + healthcheck + Telegram alerter/commands + **dashboard server**, all as asyncio background tasks), `tradeflow-ib-gateway` (IB Gateway). No third.

- **3 DB tables**: `lifecycles` + `lifecycle_events` (PR #9), `halt_acks` (PR #12 — finally applied in Session 8).

- **Production-live code paths**:
  - Orchestrator: `main.py` → `src/orchestrator.py:Orchestrator.run()`
  - Strategy: `_on_new_bar` → `Sma100BounceStrategy.detect_signal` → `_handle_trade_signal` → `OrderRouter.place_entry`
  - Fill handling: IB `fillEvent` → `OrderRouter.on_fill` → `_handle_parent_fill` / `_handle_exit_fill`
  - EOD: `EodForceClose.run_until_stopped` at 15:58 ET
  - Reconciler: `Reconciler.run_until_stopped` → 30s drain + 5-min full-scan + halt-ack poll
  - Halt API: `Orchestrator.raise_halt` / `clear_halt` / `is_halted` / `halt_raised_at`
  - Manual close (PR #16): `Orchestrator.flatten_all()` / `exit_symbol()` → `OrderRouter.close_position(symbol, reason)` → `_handle_exit_fill(ExitReason.MANUAL)`
  - Telegram (PR #14/#15/#16):
    - Alerter: `LOGGER.info("[ALERT] event_type: k=v")` → `TelegramAlertHandler` → httpx POST
    - Commands: long-poll → `/status`, `/halt SYMBOL`, `/ack`, `/flatten`, `/exit SYMBOL`, `/confirm`
  - Dashboard (PR #18 — NEW this session):
    - `dashboard/server.py:run_uvicorn` launched as background asyncio task by `Orchestrator._launch_background_tasks`
    - FastAPI app gated by HTTP Basic auth (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`)
    - Single `GET /` page with 4 panel partials: `/panel/status`, `/panel/account`, `/panel/positions`, `/panel/working_orders`
    - HTMX 10s auto-refresh per panel
    - All data sourced from `IBClient` via `DashboardAggregator` — NO Supabase queries in PR #18 (PR #19 territory)
    - Binds `0.0.0.0:8080` (with `DASHBOARD_BIND=0.0.0.0` env var)
    - Reachable via `127.0.0.1:8080` on VPS host (compose maps `127.0.0.1:8080:8080`)

- **Stub packages still empty**: `strategy/` (top-level NOT src/strategy), `risk/`, `features/`, `backtest/`, `data/`. `comms/` populated. `dashboard/` populated (NEW).

- **No public ports exposed.** Only mapped port is `127.0.0.1:8080:8080` for dashboard — host loopback only. Phone access via SSH tunnel (currently working laptop-only).

- **Operational debt** (rolled forward to §7):
  - Cold-boot IB Gateway race (RestartCount=1 expected)
  - Repo default branch cosmetic issue
  - Repo auto-merge enabled
  - `comms/__init__.py` empty (intentional)
  - DUQ account ID leak in logs accepted
  - `.env` line 17 `bash source` incompatibility
  - Co-Authored-By trailer deviation in PR #15
  - `last_heartbeat_at` returns `n/a` on dashboard (no per-tick timestamp)
  - Persistent SSH tunnel for laptop not configured (operator-declined)
  - Termius phone port-forward not working (operator-deferred)

---

## 4. Verified facts about TradeFlow (as of 2026-05-22)

**DO NOT challenge these unless probed against source.**

### IBKR + IB Gateway

- IB Gateway docker image: `ghcr.io/gnzsnz/ib-gateway:stable`, server version **178**
- Container port: `127.0.0.1:4002` (host) → `4004` (container)
- Paper account: **`DUQ331660`** (truncated to `DUQ` in `/status` for log safety)
- Library: `ib_async==1.0.0`
- `IBClient.get_portfolio() → list[PortfolioItem]` is canonical for positions (§0.5.T3)
- `IBClient.get_open_trades() → list[Trade]` is canonical for working orders (NEW: confirmed PR #18 audit)
- **`IBClient.get_account_summary() → dict[str, float]` (PR #18, NEW)** — returns NetLiquidation + AvailableFunds + BuyingPower keyed by tag
- MNQ contract spec: TICK_SIZE=0.25, MULTIPLIER=$2/point, COMMISSION_RT=$0.62, MARGIN_REQ=$2000. Front month: **MNQM6** (June 2026, expiry Fri 2026-06-19, roll target ~2026-06-11)

### Strategy

- Identifier: `strategy="sma100_bounce"` (sticky, §0.5.133)
- Real condition: MA50 > MA100 + ADX ≥ 20 + low ≤ MA100 + `ma_touch_buffer_pts` (5pt) + bullish close + NOT within `session_edge_no_trade_minutes` (5) of session edges
- Entry: MKT. Stop: separate GTC STP at -75pt. TP: bracket-child LMT at +150pt. ADX min: 20.0 period 14. LONG-only.

### Supabase

- Project ref prefix: `vzlpxaif*`, region us-east-1
- Tables: `lifecycles` (PK `lifecycle_id`), `lifecycle_events` (PK `event_id`), `halt_acks` (PK `halt_ack_id`, columns: `acked_at TIMESTAMPTZ DEFAULT now()`, `note TEXT`) — **finally applied in live DB during Session 8**
- RLS enabled on all three; service_role bypasses
- Custom httpx wrapper: `src/clients/supabase_client.py`

### State machine + halt API + manual close

- Allowed transitions: unchanged from HANDOFF_v6 §4
- Halt API (PR #12): `raise_halt(symbol=...)` / `clear_halt(reason=...)` / `is_halted() -> bool` / `halt_raised_at() -> datetime | None`
- Manual close (PR #16): `flatten_all() -> FlattenResult` / `exit_symbol(symbol) -> ExitResult` on Orchestrator; `close_position(symbol, reason) -> CloseResult` on OrderRouter. Lifecycle state transitions to **CLOSED** with `exit_reason=ExitReason.MANUAL.value`; `close_reason` carried in `lifecycle_events.payload`.
- File-flag fallback path: `/tmp/halt_clear` (inside container) — operator runs `docker exec tradeflow-app touch /tmp/halt_clear` if Supabase unreachable
- Lifecycle ID is stable UUID, no UNIQUE(symbol, state) constraint, no row deletion on transition

### Telegram (PR #14/#15/#16)

- Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OPERATOR_CHAT_ID` (in `~/.tradeflow-secrets/.env`)
- Auth: single operator chat_id whitelist. Foreign chat_id → "Unauthorized." reply
- Alerts emit via standard `LOGGER.info("[ALERT] event_type: k=v k=v")` lines. Subsystems do NOT import `comms.telegram`.
- 9 alert types: `entry_placed`, `exit_filled`, `halt_raised`, `halt_acked`, `eod_complete` (PR #14) + `flatten_requested`, `flatten_complete`, `exit_requested`, `exit_complete` (PR #16)
- 6 commands: `/status`, `/halt SYMBOL`, `/ack`, `/flatten`, `/exit SYMBOL`, `/confirm`
- `parse_mode`: NONE (plain text only, PR #15) — never use legacy Markdown for messages containing Python identifiers (§0.5.143)
- Bounded alert queue: 500 items, drops oldest on overflow
- Confirm-state for `/flatten`/`/exit`: single-slot `Optional[PendingAction]` on `TelegramAlerter` with 60s TTL. Cleared before dispatch (idempotency). Subsequent `/flatten` or `/exit` overwrites.

### Dashboard (PR #18 — NEW)

- Process: launched as asyncio task by `Orchestrator._launch_background_tasks`
- Module: `dashboard/server.py:run_uvicorn(orchestrator)` → `uvicorn.Server(config).serve()` (proper async, not blocking `uvicorn.run`)
- Auth: HTTP Basic via FastAPI `HTTPBasic` security dependency, applied globally via `dependencies=[Depends(verify)]`. `secrets.compare_digest` for credential comparison.
- Env vars (in `~/.tradeflow-secrets/.env`): `DASHBOARD_USERNAME=ohad_tradeflow`, `DASHBOARD_PASSWORD=<redacted>`, `DASHBOARD_BIND=0.0.0.0`, `DASHBOARD_PORT=8080`
- Fail-fast: missing/empty `DASHBOARD_USERNAME` or `DASHBOARD_PASSWORD` → `load_credentials()` raises `RuntimeError` synchronously (§0.5.146); orchestrator's try/except in `_launch_background_tasks` catches it, dashboard task never scheduled, orchestrator continues with telegram + trading
- Bind: `0.0.0.0:8080` inside container (§0.5.148 — container loopback ≠ host loopback)
- Docker port mapping: `127.0.0.1:8080:8080` (host loopback ONLY — defense-in-depth Layer 1)
- Reach: SSH tunnel from laptop (`ssh -L 8080:localhost:8080 tradeflow@5.78.212.37`), then browser to `http://localhost:8080`
- Panels: bot status, account snapshot, open positions, working orders. All broker-sourced via `IBClient`. Per-panel try/except — one failing panel doesn't bring down the page.
- Refresh: HTMX `hx-trigger="every 10s"` per panel partial. Server-rendered.
- Stack: FastAPI 0.115.0 + Uvicorn[standard] 0.32.0 + Jinja2 3.1.4 + HTMX 2.0.3 (CDN) + Bootstrap 5.3.3 (CDN)
- Static + templates: `dashboard/static/`, `dashboard/templates/`, `dashboard/templates/partials/`
- `last_heartbeat_at` panel field: returns `None` ("n/a" rendered) — no per-tick timestamp stored. `fetched_at` on each panel doubles as freshness.
- uvicorn `log_level="warning"` — hides startup banner. Aware tradeoff.

### Repo state at handoff

- HEAD: `80d4a2d` on `main` (PR #18 squash; possibly HANDOFF_v7 publish above it once §16 ships)
- Default branch displayed: still `claude/phase-0-repo-bootstrap-ZZGJX` (cosmetic, no impact)
- Branch protection: active. No direct pushes to main.
- gh auth: `gho_*` OAuth (no PAT in settings)
- Commit authorship: clean operator authorship (no `Co-Authored-By` trailers since PR #15 deviation)
- Settings: `.claude/settings.local.json` — 80 allow / 19 deny, PAT redacted (§0.5.144)

### Skills in repo (`.claude/skills/`)

- `architecture-question-gate`
- `code-pr-brief`
- `prod-debug-discipline`
- `verification-before-completion`
- `vps-cc-autonomy`
- `pr-brief-lint` (PR #12 + extended PR #13)

### Operator-side skills (`/mnt/skills/user/`)

- `code-pr-brief`
- `prod-debug-discipline`
- `session-handoff-writer`
- `vps-smoke-test-runbook` (referenced; not always present in chat session)

---

## 5. Wrong diagnoses (this session) — READ BEFORE YOU DEBUG

**FIVE chat-side brief slips this session.** Chat-side me is the most common source of upstream slips; VPS CC's Task A audit catches them. The `pr-brief-lint` skill should expand to encode all five.

### Wrong call 1: HANDOFF_v6 narrative claimed halt_acks was applied

- **What HANDOFF_v6 said**: §1 + §4 confidently stated migration was "applied via dashboard SQL editor by operator during Session 7"
- **Reality**: migration was never applied. Either the operator hit cancel, a tab closed, or the apply silently failed — no evidence either way
- **Discovered via**: Session 8 V0-V6 probe returning 404 PGRST205 ("Could not find the table 'public.halt_acks'")
- **Fix**: operator pasted migration SQL into Supabase dashboard, "Success. No rows returned", re-probe returned 200
- **Codified as**: **§0.5.145** — Handoff facts about live state require same-session probe

### Wrong call 2: PR #18 brief said async credential check via try/except around `create_task`

- **What brief said**: "Missing credentials → `RuntimeError` in `run_uvicorn`, caught by orchestrator's try/except around `asyncio.create_task`"
- **Reality**: `create_task` schedules but doesn't run. Errors raised inside the coroutine fire when the loop next yields, not in the synchronous path
- **Discovered via**: VPS CC noticed during PR #18 implementation; would have manifested as silent failure (dashboard never launches, no error visible)
- **Fix**: exposed `load_credentials()` as public, called synchronously before `create_task`. Orchestrator's try/except now catches it correctly.
- **Codified as**: **§0.5.146** — Validate-before-schedule

### Wrong call 3: PR #18 brief listed pyproject.toml MUST-NOT-MODIFY for dep additions

- **What brief said**: "pyproject.toml — no test config or asyncio_mode changes"; deps go in requirements.txt
- **Reality**: CI runs `pip install -e ".[dev]"` reading `[project.dependencies]`. requirements.txt is the Docker image manifest only. Two manifests, not auto-synced.
- **Discovered via**: PR #18's first CI run failed with `ModuleNotFoundError: fastapi`
- **Fix**: tiny follow-up commit added 3 deps to pyproject.toml `[project.dependencies]`. VPS CC flagged transparently.
- **Codified as**: **§0.5.147** — Dual-manifest gotcha

### Wrong call 4: PR #18 brief specified loopback bind inside container as "defense layer 1"

- **What brief said**: "Container binds 127.0.0.1:8080 inside container" as security layer
- **Reality**: Container has its own network namespace; its loopback is unrelated to the host's. Binding to 127.0.0.1 inside container makes service unreachable from outside the container ENTIRELY, including via Docker port mapping. The "loopback-only externally" defense lives in docker-compose's `ports:` spec.
- **Discovered via**: Post-merge curl from VPS host returned "Connection reset by peer". `/proc/net/tcp` from inside the container showed uvicorn WAS bound on 127.0.0.1:8080. Port mapping couldn't reach it.
- **Fix**: `DASHBOARD_BIND=0.0.0.0` in `.tradeflow-secrets/.env`. Force-recreate. Reachable.
- **Codified as**: **§0.5.148** — Container loopback ≠ host loopback

### Wrong call 5: smoke-test curl used double-quoted credentials

- **What brief said**: `curl ... -u "ohad:..." http://...`
- **Reality**: Operator's password contained `!` followed by digits/letters. Bash history expansion fired even inside double quotes. (Single quotes DO suppress it; double quotes do NOT.)
- **Discovered via**: `bash: !0: event not found`
- **Fix**: read password from `.env` via `$(grep ... | cut ...)` so the literal value never appears on the command line
- **Codified as**: **§0.5.149** — Bash history expansion on `!`

### Meta-lesson for Session 9

**FIVE chat-side slips in one session** — that's high. Pattern: every slip was an assumption made WITHOUT probing source / docs / Docker networking model. §0.5.97 (probe-before-specify) is supposed to prevent this; chat-side me violated it 5 times.

The `pr-brief-lint` skill currently has 5 rules (§0.5.137, §0.5.139, §0.5.140, §0.5.117/.118, §0.5.142). **It needs 5 more** to encode this session's lessons:

- New lint: probe runtime-state claims against live system (§0.5.145)
- New lint: probe async-task lifecycle for sync-vs-async validation (§0.5.146)
- New lint: dual-manifest sweep on dependency additions (§0.5.147)
- New lint: Docker networking model for any port binding spec (§0.5.148)
- New lint: bash history expansion check for credentials on command line (§0.5.149)

Extending `pr-brief-lint` is itself a small PR (~1 file, just the skill markdown). Candidate work for Session 9 or 10.

---

## 6. Verification block — run this before doing anything

### V0 — Pre-flight on VPS

```bash
ssh tradeflow@5.78.212.37
```

```bash
git -C ~/tradeflow fetch
```

```bash
git -C ~/tradeflow pull --ff-only origin main
```

```bash
git -C ~/tradeflow log -1 --oneline
```

Expect: HEAD at `80d4a2d` (PR #18) or later — possibly the HANDOFF_v7 publish commit above it.

```bash
ls -t ~/tradeflow/docs/handoffs/ | head -3
```

Expect: `HANDOFF_v7.md` as the newest entry.

### V1 — Containers up + healthy

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

Expect: `tradeflow-app` and `tradeflow-ib-gateway` both show `Up <duration> (healthy)`.

```bash
docker inspect tradeflow-app --format 'RestartCount={{.RestartCount}} Status={{.State.Status}} Health={{.State.Health.Status}}'
```

Expect: `RestartCount=1` (post-rebuild) or `2` (one more cold-boot race after a rebuild — still benign). `Status=running`, `Health=healthy`. RestartCount > 3 means investigate (probably IB Gateway issue or new bug).

### V2 — Deployed-code invariants (PR #16 + PR #18)

```bash
docker exec tradeflow-app grep -nE 'def flatten_all|def exit_symbol' /app/src/orchestrator.py
```

Expect: 2 matches (PR #16).

```bash
docker exec tradeflow-app grep -nE 'def close_position' /app/src/execution/router.py
```

Expect: 1 match (PR #16). **Note: file is `router.py`, NOT `order_router.py` — that's a historical brief typo, file actually never had that name.**

```bash
docker exec tradeflow-app grep -nE 'def run_uvicorn|def create_app|def load_credentials' /app/dashboard/server.py
```

Expect: 3 matches (PR #18).

```bash
docker exec tradeflow-app grep -nE 'parse_mode' /app/comms/telegram.py
```

Expect: ZERO matches (PR #15 invariant).

### V3 — IBKR + Supabase truth via committed script

```bash
bash ~/tradeflow/scripts/health_snapshot.sh
```

Expect:
- `serverVersion=178`
- `accounts=['DUQ331660']`
- `positions=[]`
- `openTrades=[]`
- `lifecycles status=200 count=0`
- `lifecycle_events status=200 count=0`
- `halt_acks status=200 count=0` ✓ (Session 8 applied)

If `positions` is non-empty: STOP — capital-at-risk state.

### V4 — Dashboard responsive + Basic auth gated

```bash
curl -sS -o /dev/null -w 'no-auth: %{http_code}\n' http://127.0.0.1:8080/
```

Expect: `401`.

```bash
curl -sS -o /dev/null -w 'with-auth: %{http_code}\n' \
  -u "ohad_tradeflow:$(grep '^DASHBOARD_PASSWORD=' ~/.tradeflow-secrets/.env | cut -d= -f2-)" \
  http://127.0.0.1:8080/
```

Expect: `200`.

**§0.5.149 reminder**: Use the `$(grep ... | cut ...)` pattern as above — passing the password literally in `-u "ohad:!password"` will trip bash history expansion.

### V5 — Cadence sanity (90s window)

```bash
docker logs tradeflow-app --since 90s > /tmp/cadence.log 2>&1
```

```bash
grep -E '\[ORCH\] healthcheck|\[RECON\] tick|\[telegram\]|\[DASH\]' /tmp/cadence.log
```

Expect:
- 1-2 `[ORCH] healthcheck: ok`
- 2-3 `[RECON] tick: drain_complete`
- 0-1 `[telegram]` lines (only on httpx events)
- 0-1 `[DASH]` lines (only on dashboard requests)

```bash
grep -iE 'error|exception|traceback' /tmp/cadence.log
```

Expect: empty.

### V6 — Phone Telegram smoke (optional, ~30s)

From operator's phone, send `/status` to the bot. Expect clean plain-text reply with `open_trades: 0` and `net_liq: $1000085.80` (or current paper value) — underscores intact (§0.5.143 invariant).

---

## 7. Pending work queue

### PR #19 — Earnings panel + recent trades log (kill switch dropped)

**Status**: planned, brief NOT yet drafted. The only remaining code work before Tuesday observation.

**Scope (PM-confirmed Session 8)**:
- **Earnings panel** — daily / 7d / 30d / all-time realized PnL aggregated from `lifecycles` where `state=CLOSED`. Commissions broken out separately if available.
- **Recent trades log** — last 20 CLOSED lifecycles with entry/exit price, qty, pnl, exit_reason. Bootstrap table.
- **NO kill switch button** — telegram `/flatten` + `/exit SYMBOL` + `/confirm` from PR #16 covers it. Drops CSRF/form-handling complexity from PR #18 scope.

**Files** (~7-8): new `dashboard/templates/partials/earnings.html` + `trades.html`; modify `dashboard/state.py` (add `PanelEarnings`, `PanelTrades`, aggregator methods); modify `dashboard/server.py` (add 2 routes + index.html partial includes); modify `dashboard/templates/index.html`; modify `src/clients/supabase_client.py` (add `get_pnl_summary()` + `get_recent_closed_lifecycles()`); tests in `tests/test_dashboard_state.py` + `tests/test_dashboard_server.py` + `tests/test_supabase_client.py`.

**Brief-design lints to pre-flight**: all 10 (§0.5.137, .139, .140, .117/.118, .142, .145, .146, .147, .148, .149).

**Estimated size**: ~3-4 hours VPS CC work. ~500 LOC.

**Ship target**: Saturday or Sunday. Buffer Monday. Live by Tuesday open.

### Tuesday observation (Session 9 or 10 main event)

**Status**: passive. Primary downstream activity.

**Scope**: observe Tuesday 2026-05-26 first RTH session starting 09:30 ET (signal-eligible 09:35 ET). Watch for:
- MA conditions triggering signal (`[STRAT] sma100_bounce: signal_emitted`)
- Telegram `entry_placed` alert
- Parent fill → bracket leg landing
- Reconciler drain handling post-fill
- Telegram `exit_filled` alert with pnl when bracket fires
- EOD at 15:58 ET (`eod_complete` alert)
- **NEW this session**: dashboard panels updating in real-time during a live trade

**Verification at day's end**: `SELECT * FROM lifecycles WHERE created_at >= '2026-05-26'::date` in Supabase dashboard.

### Backlog (post-Tuesday)

1. **PR #20+ — Cold-boot IB Gateway race fix** — docker-compose `depends_on` healthcheck + IBClient retry loop with backoff. Half-day. Removes the RestartCount=1 noise.
2. **PR #21+ — Extend `pr-brief-lint` skill** with 5 new lints from this session (§0.5.145-149). Tiny. ~1 file.
3. **PR #22+ — last_heartbeat_at real timestamp on dashboard** — 5-line orchestrator change, store timestamp on each `_healthcheck_once` tick, read into `PanelStatus`.
4. **Web kill switch button** — if operator decides phone telegram isn't sufficient. Deferred.
5. **Persistent SSH tunnel for laptop via launchd** — operator-side, no PR. Operator declined for now.
6. **Phone dashboard access** — fix Termius port-forward OR adopt Tailscale. Deferred.
7. **SHORT-side strategy enablement** — currently LONG-only. 1-2 days.
8. **PnL chart / equity curve on dashboard** — visualization tier.

### Operational debt (carry-forward)

- Repo default branch cosmetic (`claude/phase-0-repo-bootstrap-ZZGJX`)
- Repo auto-merge enabled
- Top-level stub packages empty: `strategy/`, `risk/`, `features/`, `backtest/`, `data/`
- `comms/__init__.py` empty
- DUQ account ID leak in logs accepted
- `.env` line 17 `bash source` incompatibility
- Co-Authored-By trailer in PR #15 commit (deviation)
- uvicorn `log_level="warning"` masks startup banner (PR #18) — fine for normal ops, raise to "info" if dashboard hangs

### Bugs by ID

None open. Session 8 closed all production bugs surfaced (halt_acks gap, container loopback, RestartCount cold-boot).

---

## 8. Test safety — why we belabor this

Cumulative test-mocking failures across sessions:

1. Tests passed against fictional schema — always probe migration SQL before column-assertion tests
2. `side_effect` list with wrong count → silent `StopIteration` → wrong assertions
3. Mocked at raw library chain when code uses a wrapper → mock at wrapper boundary
4. Shared `MagicMock` state leaked between tests — fresh instances per test
5. Async decorator pattern — TradeFlow uses `asyncio_mode=auto` so NO `@pytest.mark.asyncio` decorators
6. Integration tests at orchestrator level catch shadow-binding bugs better than unit mocks at router boundary (HANDOFF_v6 §8)
7. Tests don't catch external-renderer mangling (HANDOFF_v6 §8 — Markdown via telegram). For user-facing visual fidelity, operator screenshot is non-negotiable.
8. `EodForceClose.fire_once` `side_effect` count drift when new code paths added (HANDOFF_v6 §8)

**New this session:**

9. **uvicorn `log_level="warning"` masks startup banner.** Tests can't catch this — runtime symptom is "no error, no listener visible". Add a check that uvicorn DOES bind by probing the port after launch in a future PR's integration test. For now: if dashboard hangs again, temporarily raise to `log_level="info"`.

10. **TestClient is sync; DashboardAggregator is async.** Don't mix paradigms — TestClient tests must be sync functions; aggregator tests must be async (and rely on `asyncio_mode=auto` so no decorator).

---

## 9. Pitfalls from prior sessions

(Carry forward + new.)

LLM trust-but-verify list:

- "`pyproject.toml` is missing pandas/numpy" — wrong (HANDOFF_v5)
- "Dockerfile is in MUST-NOT-MODIFY so it can't be the issue" — wrong (HANDOFF_v5 PR #10). §0.5.137 born.
- "`_handle_signal` is the trading handler" — wrong (HANDOFF_v5 PR #11). §0.5.139 born.
- "Tests passing means deployed image is correct" — wrong. Smoke is source of truth post-merge.
- "Memorial Day is the last Friday in May" — wrong. Last Monday. 2026: Monday May 25.
- "Supabase `lifecycles` PK is `id`" — wrong (Session 7). It's `lifecycle_id`. §0.5.140.
- "Telegram `parse_mode=Markdown` is safe for variable-name content" — wrong (Session 7). §0.5.143.
- "Existing `settings.local.json` is safe to preserve verbatim" — wrong (Session 7 PAT discovery). §0.5.144.
- **NEW (Session 8): "HANDOFF_v6 says halt_acks migration was applied" — wrong. §0.5.145.**
- **NEW (Session 8): "async validation can be done inside the coroutine" — wrong. §0.5.146.**
- **NEW (Session 8): "pyproject.toml is the only dep manifest" — wrong, requirements.txt is also live. §0.5.147.**
- **NEW (Session 8): "binding 127.0.0.1 inside container is a security layer" — wrong. §0.5.148.**
- **NEW (Session 8): "double-quoted credentials are safe from bash interpretation" — wrong. `!` triggers history expansion. §0.5.149.**
- **NEW (Session 8): "docker compose restart picks up env_file changes" — wrong. Force-recreate required. §0.5.150.**

**Next session rule** (carry-forward): if a claim is quantitative or date-dependent, re-verify. Especially calendar facts, row counts, port numbers, env-var names, file paths, and Docker network model assumptions.

---

## 10. Session discipline lesson (Session 8)

**Five chat-side brief slips this session.** That's a new high. Pattern: every slip was an assumption made WITHOUT probing source/docs/Docker behavior.

| § | Source | Wrong claim | Probe that would have caught it |
|---|---|---|---|
| §0.5.145 | chat-side me | "halt_acks applied per HANDOFF_v6" | live httpx probe at session start |
| §0.5.146 | chat-side me | "raise inside coroutine, catch via try/except around create_task" | trace asyncio docs |
| §0.5.147 | chat-side me | "pyproject.toml is MUST-NOT-MODIFY for deps" | read pyproject.toml + Dockerfile carefully |
| §0.5.148 | chat-side me | "127.0.0.1 inside container = host loopback restriction" | read Docker networking docs |
| §0.5.149 | chat-side me | "double-quoted credentials safe from bash" | grep bash man page for history expansion |

VPS CC caught §0.5.146 and §0.5.147 at implementation time and fixed them transparently. The operator (with help from a parallel chat) caught §0.5.148. I caught §0.5.149 retroactively. §0.5.145 was caught by Session 8's V0-V6 probe — exactly the workflow the rule encodes.

**The `pr-brief-lint` skill needs to grow.** Currently 5 lints; should be 10. Candidate work for Session 9 or 10.

### Enforcement rules for Session 9

1. **`pr-brief-lint` mandatory pre-Task-A**. Every brief must cite grep output for all 10 lints (5 existing + 5 new from this session).
2. **Operator screenshots are part of post-merge smoke for any UI-touching PR.** Both telegram AND dashboard.
3. **Secrets audit on every config merge.** §0.5.144.
4. **Re-probe live state before transcribing into handoff.** §0.5.145.
5. **Test the network model.** For any port binding, verify with curl from BOTH inside container AND from host BEFORE declaring "secure" or "reachable."
6. **VPS CC bash-discipline slips remain operator-side prompts.** Pick one-time allow on chained-bash prompts.
7. **Tuesday observation is the highest-priority activity.** PR #19 ships before Tuesday; no other code work post-Tuesday until first signal observed.

---

## 11. Logging verbosity — what to demand from any new code

(Carry forward from HANDOFF_v6 + new.)

- Every state transition logs `[COMPONENT] symbol: action — reason` at INFO
- Every swallowed exception logs the specific error + context
- Async background tasks log `task_launched` + `task_exited`
- Healthcheck loop: `[ORCH] healthcheck: ok` every 60s
- Reconciler: `tick: drain_complete`, `tick: full_scan_complete`, per-lifecycle action enum value
- Foreign-position detection: `[RECON] foreign_position: ...` at WARNING
- Order placement: `entry_placed`, `bracket_placed`, `parent_filled`, `stop_placed`, `exit_filled`, `trade_closed` with pnl
- Telegram alerts: `[ALERT] event_type: k=v` standard, picked up by TelegramAlertHandler
- Halt API: `[ORCH] halt_raised: symbol=X` + `[ALERT] halt_raised: symbol=X` (sibling lines)

**New this session (PR #18):**

- **Dashboard subsystem uses `[DASH]` prefix:**
  - `[DASH] server: starting — host=X port=N`
  - `[DASH] server: stopped` (clean shutdown)
  - `[DASH] server: crashed` (unclean exit)
  - `[DASH] panel: <name>: aggregate_failed — <msg>` (per-panel error)
  - `[DASH] auth: failed — username=X` at WARNING
- **Orchestrator dashboard task launch:**
  - `[ORCH] dashboard: task_launched`
  - `[ORCH] dashboard: launch_failed — continuing without dashboard` (e.g. missing credentials)

**Caveat**: uvicorn's own startup banner is silenced by `log_level="warning"` in PR #18. If we ever need to see it during debug, raise temporarily to `log_level="info"`.

---

## 12. Master template — use for every Claude Code PR

See `.claude/skills/code-pr-brief/SKILL.md` (operator-side) for the master template. PR #12, #13, #14, #15, #16, #18 all followed it.

**MANDATORY for Session 9+ briefs** (per §10):

- Run all 10 `pr-brief-lint` checks at brief-design time (5 existing + 5 new from this session). Cite grep output in brief preamble.
- For any UI-touching PR (telegram OR dashboard), add operator screenshot step to Task F. Don't rely on unit tests for visual fidelity.
- Carry forward all §0.5.97 through §0.5.150 in Known Gotchas section verbatim.
- For Docker port-binding code: include a §6-style "verify reachability from both host AND inside container" probe in Task F (catches §0.5.148-class bugs).
- For credentials passed on command line: use `$(grep '^X=' .env | cut -d= -f2-)` pattern, NOT inline literal (catches §0.5.149).

---

## 13. Current PR brief in flight — none

No code PR in flight at handoff. **PR #19 brief is NEXT** — to be drafted at start of Session 9. Scope pre-committed:
- Earnings panel (daily / 7d / 30d / all-time)
- Recent trades log (last 20 CLOSED)
- NO kill switch (telegram covers it)

Useful artifacts on the VPS at session close:

- `scripts/health_snapshot.sh` — committed in PR #12, runs full V0-V5
- `scripts/_probe_ibkr.py` — IBKR paper state via host venv python
- `scripts/_probe_supabase.py` — three-table reachability check
- `/tmp/` mostly empty post-session

---

## 14. Canonical references (in order of authority)

1. **`src/` + `comms/` + `dashboard/` on `main` at `80d4a2d`** — verified system reality. The actual code that runs.
2. **`docs/handoffs/HANDOFF_v7.md`** — this doc, once published per §16.
3. **`docs/handoffs/HANDOFF_v6.md`** — Session 7 context, §0.5.140-.144 verbatim source.
4. **`docs/handoffs/HANDOFF_v5.md`** + **`HANDOFF_v4.md`** — earlier session context, older §0.5 rules verbatim.
5. **Supabase production DB** — `lifecycles` + `lifecycle_events` + `halt_acks`, queried via service_role.
6. **IBKR via `ib_async`** with env from `~/.tradeflow-secrets/.env` — truth for position/order/account state.
7. **`docker logs tradeflow-app`** — runtime narrative, last 24h typically.
8. **Dashboard via SSH tunnel** — operator-facing observability surface.
9. **Telegram bot from operator's phone** — operator-facing UI and push-alert channel.
10. **This handoff (v7) §1–6** — session context, NOT long-term authority. Re-verify against 1 if disagreement.

---

## 15. First 15 minutes of Session 9

**If Session 9 starts before Tuesday 09:30 ET** (Sat / Sun / Mon / Tue pre-market):

1. Read §0.5, §1, §5, §6, §7, §10, §15 of this handoff. §5 (wrong diagnoses — FIVE this session) and §10 (discipline lesson) are highest-leverage. ~7 minutes.
2. SSH to VPS. Run V0-V6 from §6. Confirm all green (RestartCount up to 2 OK if rebuilds happened). ~5 minutes.
3. Open `ssh -L 8080:localhost:8080 tradeflow@5.78.212.37` from laptop. Open browser to `http://localhost:8080`. Confirm dashboard loads with Basic auth, 4 panels render, fetched_at within 60s. ~2 minutes.
4. Draft PR #19 brief — earnings + trades log, kill switch dropped. Apply all 10 `pr-brief-lint` checks at design time. ~30-45 minutes chat-side.
5. Hand brief to VPS CC. Pause at Task A. Review findings before implementation. ~3-4h VPS CC wall clock.
6. Smoke test PR #19 post-merge using `vps-smoke-test-runbook` skill. Add a curl probe to /panel/earnings + /panel/trades.

**If Session 9 starts after Tuesday 09:30 ET** (Tuesday observation in progress):

1. Read §0.5, §1, §7's "Tuesday observation" entry. ~3 minutes.
2. SSH to VPS. Run V0-V6.
3. Tail logs: `docker logs tradeflow-app --since 6h | grep -E '\[STRAT\]|\[EXEC\]|\[ALERT\]|\[ORCH\] signal|\[RECON\]|\[DASH\]'`
4. Check Telegram chat for `entry_placed` / `halt_raised` / `eod_complete` alerts.
5. Open dashboard via SSH tunnel — monitor open positions + working orders panels in real-time.
6. Query Supabase `lifecycles WHERE created_at >= '2026-05-26'::date` — confirm any rows landed.
7. Draft HANDOFF_v8 with the day's narrative.

---

## 16. How to publish this handoff

**Path A — VPS Claude Code brief (preferred):**

Paste the brief in `publish_handoff_v7_brief.md` (separate artifact in chat) to VPS CC. VPS CC saves to disk, commits, pushes, opens PR, merges via auto-merge. The brief is in the chat above this handoff.

**Path B — Manual fallback:**

```bash
scp HANDOFF_v7.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v7.md
```

```bash
ssh tradeflow@5.78.212.37
```

```bash
cd ~/tradeflow
```

```bash
git checkout -b docs/handoff-v7
```

```bash
git add docs/handoffs/HANDOFF_v7.md
```

```bash
git commit -m "docs: add v7 handoff (Session 8 — PR #16 + PR #18 shipped, dashboard live)"
```

```bash
git push --set-upstream origin docs/handoff-v7
```

```bash
gh pr create --base main --title "docs: add v7 handoff (Session 8 — PR #16 + PR #18 shipped, dashboard live)" --body "Session 8 close. See docs/handoffs/HANDOFF_v7.md for the full doc."
```

```bash
gh pr merge --squash --delete-branch --auto
```

The handoff exists only if saved to disk AND committed AND merged to main.

---

*End of handoff v7. Target lifespan: until Tuesday 2026-05-26 close-of-trading; then v8 captures the first paper trade narrative and v7 becomes historical, ranked below v8 + live code in §14.*
