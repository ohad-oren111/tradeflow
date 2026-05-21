# TradeFlow — Handoff v4 (Phase 1 orchestrator verified end-to-end, paper-only, $0 at risk)

*Handoff from end of 2026-05-21 (covers Sessions 4 + 5, run back-to-back). System status: IB Gateway container is Up and healthy; tradeflow-app container exists and verifiably boots clean against IBKR paper account DUQ-prefix at $1M NetLiquidation, then SIGTERMs cleanly (exit 0); both containers are intentionally stopped between sessions. **$0 at risk.** No trading code exists yet — orchestrator is wiring only, state machine is PR #9 (next).*

---

## 0. How to use this doc

Read §§0.5, 1, 2, 4, 5, 6 first — that's the state-of-the-system as of handoff. Sections 7–13 are reference material. Section 14 is the single-file source of truth to consult when this handoff disagrees with itself or a live observation: this doc + `.claude/skills/vps-cc-autonomy/SKILL.md` on `main` at `a492999` or later. After that, code on main wins.

**Do not trust this doc alone.** Run the verification block in §6 before writing any code. The most important first action is: launch VPS CC from `cd ~/tradeflow && claude` (so cwd is the project root and the no-`cd` rule from the autonomy skill holds without effort).

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block. Self-contained commands, chained with `&&` (never `;`). Source env vars explicitly in the same block. Expected output described immediately below each block, plus a decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**Learning-delivery discipline.** Every time you learn something new — a bug pattern, a corrected assumption, an environmental fact, a diagnostic finding — surface it immediately in the chat, formatted as a markdown snippet the owner can paste verbatim into the running handoff queue.

**Read before diagnosing.** When debugging a complex state bug, read the full startup log and 3-5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth (§0.5.98 + verification-before-completion skill).** Before escalating urgency language ("capital at risk", "churning fees", "spiraling"), hit the source of truth — live API, live DB, raw log file — not aggregated metrics. The verification-before-completion skill codifies this at the *claim moment* for every "done/fixed/deployed/passing" assertion.

**Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise. The owner does not run smoke tests by hand.

### Carry-forward from v3 §0.5.92–.115 (Botty + early TradeFlow)

§0.5.92–.105 verbatim from v3. The load-bearing ones for TradeFlow:
- **§0.5.95** — Active secrets path is `/home/tradeflow/.tradeflow-secrets/.env`; mode 600, owner tradeflow.
- **§0.5.97** — Always probe external specs (broker contracts, fees, schema, library API surfaces, **and env-file key names** — see §5 #35 below) against source before baking into briefs. Never derive from memory.
- **§0.5.98** — Broker/exchange API is ground truth for position, fill history, and capital claims — not internal DB tables.
- **§0.5.105** — Permission rule additions are comprehensive sweeps, not iterative patches.
- **§0.5.106** — Snapshots vs live state: pasted snapshots are stale; re-probe in real time.
- **§0.5.107** — Branch protection on main is verified via Rulesets, not legacy Classic Branch Protection.
- **§0.5.108** — `gh pr create --base main` pinned explicitly every time. Lost PR #4 in Session 3 to implicit base.
- **§0.5.109** — docker-compose `env_file:` and `${VAR}` interpolation read different sources.
- **§0.5.110 / §0.5.114** — `.env` strict `KEY=VALUE`, no inline comments, rstrip ALL values when rewriting.
- **§0.5.111** — Never paste `docker compose config` output to chat (interpolates secrets).
- **§0.5.112** — `docker compose ps` "healthy" lies during start_period. Trust service-level logs.
- **§0.5.113** — IB Gateway login error appears in a `GATEWAY` modal — `scrot` first, log-grep second.
- **§0.5.115** — Autonomy default. Any multi-step operator task should collapse to one VPS-CC-driven flow.

### New standing rules ratified Sessions 4–5

- **§0.5.116** — **CC Web tier is DROPPED.** Workflow is Tier 1 (chat: strategy, briefs, handoffs) + Tier 2 (VPS CC: implementation + ops + smoke tests). VPS CC ships code PRs directly via gh CLI. Branch protection on `main` is the safety net.

- **§0.5.117** — **No `cd X && ...` in any Bash tool call.** Hardcoded Claude Code safety heuristic fires regardless of `~/.claude/settings.json` allow rules. Operate from project root (launch VPS CC with `cd ~/tradeflow && claude`); for cross-repo work use `git -C /absolute/path <subcommand>` or absolute paths in tool args.

- **§0.5.118** — **Pattern discipline for VPS CC briefs (and chat-drafted bash).** No `$(...)` command substitution, no `${VAR}` parameter expansion, no `;` separators, no heredocs (`<<EOF`), no chained `sleep N` then probe — each is a hardcoded Claude Code safety detector that prompts above the allowlist. Workarounds: Python helpers in `/tmp/scriptN.py` for interpolations; separate Bash calls per logical step; `-F /tmp/commitmsg.txt` and `--body-file /tmp/pr_body.md` for git/gh content; `until COND; do sleep N; done` (no `;`) for polling.

- **§0.5.119** — **VPS CC pre-flight is non-optional, at session top.** `git -C ~/tradeflow fetch && git -C ~/tradeflow pull --ff-only origin main && ls -t docs/handoffs/ | head -3`, then read the latest HANDOFF_v*.md before anything else. Session 4 wasted cycles because VPS CC read a stale local HANDOFF_v2.

- **§0.5.120** — **Compose env vars that mean different things in host vs container contexts MUST NOT use `${VAR:-default}` interpolation.** Hardcode the container-side value. The trap: host-side `.env` sets `IBKR_HOST=127.0.0.1` for host scripts (smoke runners, probes); docker-compose interpolation reads from there and pipes loopback into the container's environment, where 127.0.0.1 means container-netns loopback (where nothing listens). Caught by PR #13. See §5 #36.

- **§0.5.121** — **Supabase env name is `SUPABASE_SERVICE_ROLE_KEY`, not `SUPABASE_SERVICE_ROLE`.** Match `.env` reality verbatim. Probe with `grep -E '^SUPABASE_' ~/.tradeflow-secrets/.env | sed 's/=.*/=<redacted>/'` before any new code references it. Caught by PR #12. See §5 #35.

- **§0.5.122** — **PR #8 scope: Supabase client is INSTANTIATED but unused.** Empty `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` is tolerable; orchestrator logs a WARNING. From PR #9 (state machine) onward, Supabase becomes load-bearing — populate `.env` before that PR ships.

- **§0.5.123** — **IBC Read-Only API is currently ON inside `tradeflow-ib-gateway`.** Even though `.env` sets `READ_ONLY_API=no`, the IBC runtime profile inside the container is still serving Read-Only mode. This is Session-4-era operational debt. Harmless for PR #8 (healthcheck uses `accountSummaryAsync`, a read-only path); blocking from PR #11+ (order placement). Resolution: requires touching `jts.ini` inside the container OR mounting a corrected config — operator-side decision (touches secrets-adjacent state).

---

## 1. Where we are (as of handoff, 2026-05-21 ~19:00 UTC)

### Live production state

- **`tradeflow-ib-gateway` container:** Running, healthy. Image `ghcr.io/gnzsnz/ib-gateway:stable`. Logged in to IBKR paper as `popoopopcrpaper` → account `DUQxxxxxx`. API server bound on container port 4004, host-mapped to `127.0.0.1:4002`. Server version 178.
- **`tradeflow-app` container:** Exists (built from `Dockerfile` on main). Currently stopped (cleanly exited 0 after Stage 3 SIGTERM probe). Will boot clean against the running gateway when restarted.
- **IBKR paper account:** `DUQ`-prefix (verified live this session via `ib_async.IB().accountSummaryAsync`). NetLiquidation $1,000,000.00 USD. Zero positions, zero open orders.
- **Capital at risk:** **$0.** Paper, pre-deployment. 50 trades + 30 paper days remain before any live-capital gate.

### What just shipped (Sessions 4–5)

- **PR #8** at `ccc5697` — added `docs/pr-briefs/PR8_orchestrator.md` (the orchestrator wiring brief) to main.
- **PR #9** at `710f75d` — `.claude/skills/vps-cc-autonomy/SKILL.md` — autonomy default for VPS CC sessions (pre-flight discipline, 6 critical-decision criteria, auto-decide list).
- **PR #10** at `8408623` — **Phase 1 PR 3 — orchestrator wiring code.** `src/orchestrator.py`, `main.py` (rewrite from Phase 0 argparse scaffold), `tests/test_orchestrator.py` (10 new tests, 40 total), `docker-compose.yml` `tradeflow-app` service, `Dockerfile`. No trading logic. Tests green, smoke initially blocked.
- **PR #11** at `65fa06d` — Skills port: `verification-before-completion` + `architecture-question-gate` (new) + additive patch to `prod-debug-discipline` (Step 2.5 multi-layer boundary instrumentation + Rationalization rejection table).
- **PR #12** at `ee6be43` — Orchestrator unblock: `SUPABASE_SERVICE_ROLE_KEY` (matches .env) + made Supabase optional for PR #8 scope (warns if missing).
- **PR #13** at `4ad4fc2` — Compose fix: hardcoded `IBKR_HOST: ib-gateway` and `IBKR_PORT: "4004"` (no `${VAR:-default}` interpolation) for the container-side environment block.
- **PR #14** at `a492999` — Documented Claude Code's hardcoded safety heuristics in the `vps-cc-autonomy` skill (the `cd && X` / `;` / `$(...)` / `${VAR}` / heredoc / chained-sleep traps + workarounds).

**`main` HEAD as of handoff: `a492999`.** Chain back through v3 publish at `f5a39f8` and PR #6 (Phase 1 PR 2 — IB Gateway Docker + ib_async client + Supabase REST stub + 30 tests) at `f29ae56`.

### What we discovered this session (not yet baked into code beyond what's above)

1. **IBKR_HOST means two different things.** Host-side `.env` (correctly) sets `IBKR_HOST=127.0.0.1` so smoke runners and probes on the VPS itself dial the gateway via the host-mapped port. Inside the docker network the orchestrator MUST dial `ib-gateway:4004`. PR #13 hardcoded the container-side; the host-side stays in `.env`. Don't reintroduce `${IBKR_HOST:-ib-gateway}` interpolation — caught us in Session 5.

2. **`.env` line 17 has a value that trips `bash source`** ("PM: command not found" error). `python-dotenv` (which `main.py` uses) parses it correctly. No urgency, but any future shell script that needs to `source` the secrets `.env` will choke. Cleanest fix is during the next IBKR password rotation when the helper script's Python rewrite will rstrip ALL values.

3. **gh PAT scopes:** `gh auth login --with-token` requires `read:org`. The PAT issued in Session 4 has `repo` + `workflow` only, so direct write to `~/.config/gh/hosts.yml` is the workaround (mode 600, owner tradeflow). All gh CLI flows used here (`pr create`, `pr checks --watch`, `pr merge --squash --delete-branch`, `pr view --json`) work fine without `read:org`.

4. **Repo default branch on GitHub is still `claude/phase-0-bootstrap`** — cosmetic only because every PR brief pins `--base main` per §0.5.108. If someone clones the repo with default settings they get the bootstrap branch.

5. **Repo `enablePullRequestAutoMerge` is off.** All Sessions 4-5 merges were manual `gh pr merge <N> --squash --delete-branch` after `gh pr checks <N> --watch` returned green. Working pattern; no urgency to flip.

6. **The DUQ account number leaked to chat scrollback** during Session 4's post-restart log dump (IBC's `Trader Workstation Configuration (Simulated Trading)` window-title line). Subsequent log dumps redact via `sed -E 's/DUQ[0-9]+/DUQ.../g'`. Operator decision: accept (paper, low-risk, easily rotated) or rotate IBKR-side.

7. **Hardcoded Claude Code safety heuristics catalog (PR #14 documented):** `cd X && ...` (path-resolution bypass + git hook injection), `;` separators, `$(...)`, `${VAR}`, heredocs, chained `sleep`. None of these can be silenced via `~/.claude/settings.json`. Edit/Write tool prompts and `.claude/own-settings` guardrails are an additional layer not fully tamed yet.

---

## 2. The session thread (Sessions 4 + 5)

A narrative of what happened, in order, including wrong turns:

1. **Session 4 opened** with v3 already published. First action was V0–V5 verification block from v3 §6. V5 screenshot caught the IBKR "Invalid username or password" GATEWAY modal still showing after 21h — same Session-3-close failure state, no auto-recovery.

2. **First wrong move (resolved fast):** Initial diagnosis blamed the password value. The probe-first instruction (operator-driven) caught that `.env` was byte-clean (no trailing whitespace, no CR/LF, account prefix `DUQ` ✓) and the password length matched the failed-Session-3 value. Hypothesis revised: transient IBKR lockout, not bad credentials.

3. **`docker restart tradeflow-ib-gateway` cleared the lockout.** V5 screenshot post-restart showed "API Server: connected" (green lock). PR #6 smoke test ran end-to-end: 8/8 deployed files present, ib_async logged on server v178, accountSummaryAsync returned $1M paper account, 30/30 tests passed. Operator decided NOT to rotate password — saved a credential cycle.

4. **PR #8 brief (orchestrator wiring) published** to `docs/pr-briefs/PR8_orchestrator.md` on main at `ccc5697`. Brief was written for CC Web at the time.

5. **Session 5 opened** with an architecture pivot: operator pushed back on the chat→CC-Web→VPS-CC three-tier workflow as friction. Decision ratified: drop CC Web entirely. VPS CC now ships code PRs directly. Brief mechanics: VPS CC reads its own brief from main, implements, runs tests, opens PR via `gh`, watches CI, squash-merges.

6. **PR #9 (vps-cc-autonomy skill)** shipped first to formalize the autonomy default — 6 critical-decision criteria, auto-decide list, pre-flight discipline. VPS CC merged it solo to prove the pattern at `710f75d`.

7. **PR #10 (orchestrator code)** shipped next. `src/orchestrator.py`, `main.py` rewrite, `tests/test_orchestrator.py`, docker-compose tradeflow-app service, Dockerfile (created — brief said "only if needed", and one was needed since none existed). VPS CC handled 5 deviations within spec: main.py was REWRITE not NEW (Phase 0 scaffold existed), Dockerfile was NEW unconditionally, yaml.safe_load → substring check (PyYAML not in dev deps), test_main_module_smoke became sync (avoids asyncio.run() nested-loop), 10 tests not 8. 40/40 tests pass. Merged at `8408623`.

8. **Smoke test on PR #10 hit two cascading bugs** — both §0.5.97 violations on my (chat) part:

   **Wrong call #1 (§0.5.97 instance #35):** PR #8 brief specified `SUPABASE_SERVICE_ROLE`. The actual `.env` has `SUPABASE_SERVICE_ROLE_KEY`. Plus values were empty. Orchestrator fail-fast on missing was working as intended; the brief was wrong. Fixed in **PR #12** at `ee6be43`: `os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")` with a loud WARNING if missing. Supabase is unused in PR #8 scope anyway.

   **Wrong call #2 (§0.5.97 instance #36):** With PR #12 unblocking the Supabase gate, the smoke surfaced a second bug: `docker-compose.yml` used `IBKR_HOST: ${IBKR_HOST:-ib-gateway}`. The `:-default` only fires when the variable is **unset**. Host-side `.env` sets `IBKR_HOST=127.0.0.1` for HOST scripts (probes, smoke runners) — so the container saw `127.0.0.1` and dialed loopback in its own netns, restart-looping on `ConnectionRefusedError`. Fixed in **PR #13** at `4ad4fc2`: hardcoded `IBKR_HOST: ib-gateway` / `IBKR_PORT: "4004"` in the compose `environment:` block. This is `architecture-question-gate` territory in a sense (env-injection precedence is a system-design problem, not a local fix), but at fix #2 the gate hasn't fired yet (3+ failed fixes on same family is the trigger).

9. **Skills port PR #11** at `65fa06d` shipped in parallel: added `verification-before-completion` and `architecture-question-gate` skills, additively patched `prod-debug-discipline` with Step 2.5 (multi-layer boundary instrumentation) and a rationalization rejection table. Description lengths verified <1024 chars (vbc=747, aqg=792).

10. **Stage 3 smoke (post-PR #13) PASSED all 6 probes.** Orchestrator boots clean; `[ORCH] startup: ib_connected — server_version=178`; `[ORCH] startup: account_bound — prefix=DUQ net_liq=1000000.00`; healthcheck cadence 60s exact (17:14:51 → 17:15:51); SIGTERM → `shutdown: signal_received` → `ib_disconnected` → `db_closed` → `done — exit_code=0 duration_sec=95.3`. Runbook saved at `/home/tradeflow/runbooks/PR8_smoke_test.md` with the actual passing probes.

11. **Permission-prompt iteration arc (the failure mode of the session series).** Across Sessions 4-5, VPS CC hit a cascading series of permission prompts that the settings.json sweep couldn't reach. First iteration (Session 4) caught chained docker exec patterns. Second (Session 5 PR #9) added expansion patterns. Third (Session 5 PR #14) documented hardcoded heuristics as a skill section. Even after PR #14 there are residual Edit/Write tool prompts and `.claude/own-settings` guardrails. **Operator's path forward (open question for v5):** launch VPS CC with `--dangerously-skip-permissions` and accept the deny-rules tradeoff, OR keep clicking "always allow X" persistent options as they appear. Branch protection + GitHub auth + the in-skill behavioral rules remain regardless.

The goal of this section: a new session reads it and understands which rabbit holes are closed so it doesn't walk them again.

---

## 3. What the system is actually made of

**Single source of truth:** `docs/pr-briefs/PR8_orchestrator.md` + this handoff, both on main at `a492999`. No standalone architecture doc yet.

Highlights to save a lookup:

- **Container `tradeflow-ib-gateway`** (gnzsnz/ib-gateway:stable image): IBC + Java IB Gateway + socat sidecar. Host-maps `127.0.0.1:4002 → container:4004`. Reads `IBKR_USERNAME` / `IBKR_PASSWORD` / `IBKR_PAPER_ACCOUNT` / `TRADING_MODE` from env_file. Logs to `docker logs tradeflow-ib-gateway`.
- **Container `tradeflow-app`** (`python:3.11-slim` base, Dockerfile created PR #10): runs `python main.py`. depends_on `ib-gateway`. env_file `/home/tradeflow/.tradeflow-secrets/.env`. environment block hardcodes `IBKR_HOST: ib-gateway`, `IBKR_PORT: "4004"`, plus `ORCH_HEALTHCHECK_INTERVAL_SEC` and `ORCH_LOG_LEVEL` with defaults.
- **`main.py`** — loads `.env` via `python-dotenv` (NOT `bash source` per §0.5.110 trap on line 17), builds `OrchestratorConfig` from env, calls `asyncio.run(Orchestrator.run())`. Supabase is optional (warns if missing) per §0.5.122.
- **`src/orchestrator.py`** — `Orchestrator` class owns IBClient + SupabaseClient lifetime. Periodic healthcheck via `ib_async.IB._ib.reqCurrentTimeAsync()` (Awaitable[datetime], not coroutine). Signal handling: `signal.signal(SIGTERM/SIGINT, ...)` setting an `asyncio.Event` via `loop.call_soon_threadsafe` (the `asyncio.add_signal_handler` path didn't work in this Docker image).
- **`src/clients/ib_client.py`** — `IBClient` wrapper around `ib_async.IB()`. Exposes `connect`, `disconnect`, `get_positions`, `get_portfolio`, `get_open_trades`. Orchestrator reaches through `ib._ib` for raw `reqCurrentTimeAsync` / `accountSummaryAsync` (no new methods on IBClient — brief permitted).
- **`src/clients/supabase_client.py`** — Custom httpx REST wrapper (NOT supabase-py per §0.5.T4). `select`, `insert`, `upsert`, `close`.
- **`tests/`** — 40 tests total. Hermetic (no live IBKR / no live Supabase). `pyproject.toml` sets `asyncio_mode = "auto"` so tests use bare `async def` with no decorator.
- **`.claude/skills/` (in repo, 3 skills):** `vps-cc-autonomy`, `verification-before-completion`, `architecture-question-gate`. Plus `prod-debug-discipline` (patched).
- **`/mnt/skills/user/` (operator-side, 4 skills):** `code-pr-brief`, `prod-debug-discipline`, `session-handoff-writer`, `vps-smoke-test-runbook`.
- **Cron / automation:** None. Orchestrator runs as a long-lived container; no cron jobs, no schedulers.

No live trading code paths. No order placement. No state machine. **PR #9 (state machine) is the next feature PR.**

Dead/phantom surfaces: none of substance. The Phase 0 argparse `main.py` was overwritten by PR #10 and has no callers.

---

## 4. Verified facts about MNQ contract, IBKR API, ib_async, Supabase env, and pipeline order (2026-05-21)

**DO NOT challenge these unless the schema migrates.**

### MNQ contract (verified v3 §0.5.97-locked, from SeanBot `config/settings.py:32-35`)

- TICK_SIZE = 0.25 index points
- MULTIPLIER = $2/point ($0.50/tick)
- COMMISSION_RT = $0.62 round-trip
- MARGIN_REQ = $2,000 day-trade
- CME maintenance ~$3,636
- Quarterly cycle Mar/Jun/Sep/Dec, expiry 3rd Friday, roll ~8 days before expiry

### IBKR paper account

- Account ID prefix: **`DUQ`** (NOT `DUO` — the `DUO891961` in pre-v2 memory was wrong; corrected in v3 §0.5.97 #27)
- Server version (paper): 178 (verified Session 4 + Session 5 smoke)
- NetLiquidation: $1,000,000.00 USD
- AccountType: INDIVIDUAL
- Open positions: 0
- Open orders: 0

### ib_async API surface (probed Session 5)

- `IB.connectAsync(host, port, clientId=1, timeout=4.0, readonly=False, account="", ...)` — coroutine, raises on failure
- `IB.disconnect()` — sync, idempotent
- `IB.reqCurrentTimeAsync()` — returns `Awaitable[datetime]` (NOT coroutine; `await` works)
- `IB.accountSummaryAsync(account="")` — returns `List[AccountValue]` with `.tag`, `.value`, `.currency`, `.account`
- `IB.openTrades()` — returns list (currently empty, also blocked by IBC Read-Only)
- Critical: `ib_async`, not `ib_insync` (§0.5.T2)

### Supabase env (probed Session 5)

- `.env` key is **`SUPABASE_SERVICE_ROLE_KEY`** (NOT `SUPABASE_SERVICE_ROLE` — §0.5.121)
- `.env` also has `SUPABASE_URL` and `SUPABASE_ANON_KEY`
- All three currently EMPTY values (operator hasn't provisioned the Supabase project yet)
- `SupabaseClient` is a custom httpx wrapper (§0.5.T4) — does not validate URL/key on `__init__`, fails on first network call

### docker-compose interpolation order

- Compose reads `${VAR}` from the cwd's `.env` (the local `~/tradeflow/.env`, which is symlinked to `~/.tradeflow-secrets/.env`)
- `env_file:` directive injects variables into the container's environment
- The `environment:` block can override env_file values for a service
- **Container-routing constants (IBKR_HOST/IBKR_PORT inside the docker network) MUST NOT use `${VAR:-default}` interpolation** — hardcode them in the environment block per §0.5.120

### Pipeline order

For each healthcheck tick: `_ib.reqCurrentTimeAsync()` → log `[ORCH] healthcheck: ok — ib_time=<ISO>`. For each accountSummary fetch (only on startup): `accountSummaryAsync(account="")` → derive net_liq from the `NetLiquidation` tag → log `[ORCH] startup: account_bound — prefix=DUQ net_liq=<value>`.

**New load-bearing facts (this session series):**

- `READ_ONLY_API=no` in `.env` does NOT propagate into IBC's runtime profile — IBC is still serving Read-Only mode (§0.5.123). Operator decision pending. The healthcheck path is read-only so it works.
- `gh auth login --with-token` requires `read:org`; direct `~/.config/gh/hosts.yml` is the workaround for repo + workflow scoped tokens.

---

## 5. Wrong diagnoses — READ BEFORE YOU DEBUG

Two §0.5.97-family violations this session series. **Pattern: baking external specs into briefs from memory without probing the real artifact.**

### Wrong diagnosis #35 (Session 5 brief design) — SUPABASE_SERVICE_ROLE vs SUPABASE_SERVICE_ROLE_KEY

- **What the diagnosis was:** Chat-side brief for PR #8 specified `SUPABASE_SERVICE_ROLE` env var.
- **What evidence led to it:** None — I (chat) named it from memory of Supabase's official key naming, without probing the actual `.env`.
- **Why it was wrong:** `.env` has `SUPABASE_SERVICE_ROLE_KEY`. Orchestrator fail-fast on missing was correct behavior; the brief was the bug.
- **What the correct diagnosis was:** Code follows reality (rename), AND make Supabase optional for PR #8 scope (no DB writes yet anyway). PR #12 at `ee6be43` fixed both.

### Wrong diagnosis #36 (Session 5 compose config) — `${IBKR_HOST:-ib-gateway}` interpolation trap

- **What the diagnosis was:** Brief assumed `${IBKR_HOST:-ib-gateway}` in docker-compose would default to `ib-gateway` since "no IBKR_HOST is set for the new service."
- **What evidence led to it:** None — assumption from the syntax pattern, no probe of what's actually in `.env`.
- **Why it was wrong:** `:-default` only fires when the variable is UNSET. Host-side `.env` sets `IBKR_HOST=127.0.0.1` for host scripts (probes, smoke runners) — so the interpolation read `127.0.0.1` and piped it into the container's environment block. Container dialed loopback inside its own netns, restart-looped on `ConnectionRefusedError`.
- **What the correct diagnosis was:** Container-routing constants must NOT use `${VAR:-default}` interpolation — hardcode them. PR #13 at `4ad4fc2` hardcoded `IBKR_HOST: ib-gateway` / `IBKR_PORT: "4004"`.

### Process wrong moves (chat-side, my pattern)

- **Multiple iterations of "mega-brief"** when the user wanted brevity. Sessions 3-5 each had at least one chat reply that was a long mega-brief, and the user pushed back multiple times. Lesson: when the user says "stop making me work", default to the smallest possible artifact + a yes/no question, not a 30KB plan.
- **Designing autonomy sweeps without probing the actual prompt categories.** Each settings.json sweep covered the patterns we'd just seen, missing the next category (Edit/Write tool prompts, `.claude/own-settings` guardrails, hardcoded heuristics). Lesson: ask "what category of prompt is this" before generating allow patterns.
- **Forgetting that "verification-before-completion" applies to chat-drafted briefs too.** Specifying env var names without probing `.env` IS a verification-before-completion violation, just at brief-design time instead of claim time.

**Lesson for next session:** Every chat-drafted brief should pass through a "did I probe vs assume" filter on three axes:
1. **Env var names** — `grep ^FOO_BAR ~/.tradeflow-secrets/.env` before specifying.
2. **Library API surface** — `python3 -c "import X; print(dir(X.Y))"` before assuming methods exist.
3. **Compose interpolation behavior** — if a `${VAR:-default}` is in a config file, check whether `VAR` is set in the host `.env` first.

---

## 6. Verification block — run this before doing anything

**V0 — VPS sanity + pre-flight**

```bash
whoami && hostname && date -u && git -C ~/tradeflow fetch && git -C ~/tradeflow pull --ff-only origin main && git -C ~/tradeflow log -1 --oneline
```
Expect: `tradeflow` @ `ubuntu-4gb-hil-1`, current UTC, "Already up to date" or fast-forward, HEAD shows `a492999` or later. If HEAD is older than `a492999`, the pull above should bring you forward.

**V1 — Skills inventory (7 active)**

```bash
ls -la ~/tradeflow/.claude/skills/
```
Expect: 4 directories — `prod-debug-discipline`, `vps-cc-autonomy`, `verification-before-completion`, `architecture-question-gate`. Each contains `SKILL.md`. The operator-side personal skills (`code-pr-brief`, `session-handoff-writer`, `vps-smoke-test-runbook`) live in the user-skills mount, not in the repo — confirm via VPS CC's startup banner ("Skill(...) Successfully loaded" lines).

**V2 — Container state**

```bash
docker compose ps
```
Expect (post-handoff baseline): `tradeflow-ib-gateway` Up X (healthy). `tradeflow-app` either Up (if you left it running after smoke) or not listed (stopped & removed). If app is running: `docker logs tradeflow-app --tail 30` should show `[ORCH] startup: ib_connected — server_version=178` and `[ORCH] healthcheck: ok` lines.

**V3 — IBKR paper account live probe (source of truth, §0.5.98)**

```bash
.venv/bin/python -c "import asyncio,os; from dotenv import load_dotenv; load_dotenv('/home/tradeflow/.tradeflow-secrets/.env'); from ib_async import IB; ib=IB(); asyncio.run(ib.connectAsync(os.environ.get('IBKR_HOST','127.0.0.1'), int(os.environ.get('IBKR_PORT','4002')), clientId=99, timeout=10)); print('serverVersion=', ib.client.serverVersion()); print('positions=', ib.positions()); ib.disconnect()"
```
Note: this uses the HOST-side IBKR_HOST/IBKR_PORT (127.0.0.1:4002 via the host-mapped port), NOT the container-side. Run from VPS shell, not from inside `tradeflow-app`. Expect: `serverVersion= 178`, `positions= []`. If `ConnectionRefusedError`: ib-gateway needs restart (`docker restart tradeflow-ib-gateway`, wait 90s, retry).

**V4 — Tests still pass**

```bash
.venv/bin/python -m pytest tests/ -q
```
Expect: `40 passed in ~0.3s`. If any fail: do NOT proceed to new work; diagnose via `prod-debug-discipline` skill.

**V5 — Skills load cleanly in VPS CC**

Launch VPS CC: `cd ~/tradeflow && claude`. First lines should include `Skill(vps-cc-autonomy) Successfully loaded skill` (or equivalent for any auto-loaded skill). Confirm the autonomy default is active.

---

## 7. Pending work queue

Priority order depends on V1–V5 state, not this ordering.

### PR #9 / state machine (FEATURE) — next big thing

- **Status:** Planned, not yet briefed.
- **Scope:** Implement the IDLE → ENTERING → ACTIVE → EXITING → CLOSED state machine on top of the Orchestrator. State persistence in Supabase (this is when Supabase credentials become load-bearing per §0.5.122). One state transition per event, no trading logic yet — just the machine + persistence + invariants.
- **Design decisions needed:** (1) Where does state live — single `lifecycles` table (Botty-lesson identity-stable schema, §0.5.96 from v3), or split per-trade? (2) Reconciliation cadence — on every healthcheck tick, or separate scheduled tick? (3) What invariants must hold across transitions (e.g., can't transition to ACTIVE without a confirmed open order at broker)?
- **Estimated size:** Substantial — new module, new tests, Supabase schema migration, fresh PR brief. Expect 400-600 lines of code, 15-25 tests.

### IBC Read-Only API debt (operational)

- **Status:** Blocking from PR #11+, harmless before then. §0.5.123 documents.
- **Scope:** Flip Read-Only mode off inside `tradeflow-ib-gateway`. Resolution likely involves either (a) mounting a corrected `jts.ini`, (b) setting an IBC-recognized env var on the gateway container that the runtime profile respects (current `READ_ONLY_API=no` is ignored), or (c) re-baking the gateway image. Operator-side decision because it touches secrets-adjacent state.

### Supabase project provisioning (operational)

- **Status:** Required before PR #9 (state machine) can write state.
- **Scope:** Create a Supabase project (or use an existing one), get the URL + service_role_key, populate `~/.tradeflow-secrets/.env`. Then design the schema for `lifecycles` (Botty-stable-identity pattern).

### Operational cleanup eventually

- Repo default branch should flip from `claude/phase-0-bootstrap` to `main` in GitHub Settings → General → Branches.
- Repo auto-merge should be enabled (Settings → General → Pull Requests → Allow auto-merge) so VPS CC's `gh pr merge --auto` actually queues instead of failing.
- gh PAT could be regenerated with `read:org` scope to retire the direct `hosts.yml` workaround.
- `.env` line 17 cleanup at next IBKR password rotation.
- DUQ account ID: operator decides whether to scrub Session 4 chat messages or rotate the paper account.

---

## 8. Test safety — why we belabor this

Carry forward from v3:

1. Tests passed against a fictional schema because they mocked the column names → real schema diverged. The Supabase client tests dodge this by mocking at the HTTP layer (httpx), not the schema layer.
2. `side_effect` list with wrong count → silent StopIteration → wrong assertions. PR #10 tests followed the guardrails — `AsyncMock` for async methods, fresh `MagicMock()` per test, no shared state.
3. Mocked at raw library chain when code uses a wrapper → tests green, prod broken. PR #10 mocks `Orchestrator._ib` (the wrapper) and `Orchestrator._db`, not raw `ib_async.IB()`.
4. Shared MagicMock() state leaked between tests → use `_make_mock_ib()` factory pattern from `tests/test_orchestrator.py`.

**New guardrail this session:** When testing async code that itself calls `asyncio.run()` (like `main.main()`), make the test SYNC. Async tests inside an already-running event loop break with "cannot be called from a running event loop". `test_main_module_smoke` was async, failed, was made sync in PR #10's final iteration.

Guardrails in the `code-pr-brief` master template (§12) prevent all these. Do not ship tests that skip them.

---

## 9. Pitfalls from prior sessions

Things to not trust without verification:

- **"IBKR_HOST is `ib-gateway` inside the container by default"** — wrong, see §0.5.120 / PR #13. The interpolation default doesn't fire when the host `.env` sets the variable.
- **"`bash source ~/.tradeflow-secrets/.env` is a clean way to load env"** — wrong, line 17 trips it. Use `python-dotenv` from inside Python, or `set -a && source ... && set +a` and grep for the failing line.
- **"`gh auth login --with-token` works with any PAT that has `repo` scope"** — wrong, requires `read:org`. Workaround: direct `hosts.yml`.
- **"`docker exec env`-pattern is a fine debug probe"** — DANGEROUS, leaks all container env including secrets to scrollback. Denied at policy layer in PR #14's settings sweep (still in deny list).
- **"`asyncio.add_signal_handler` works in Docker"** — wrong in some images. Use `signal.signal(SIGTERM, ...)` setting an `asyncio.Event` via `loop.call_soon_threadsafe`. The orchestrator's signal handling does this.
- **"Aggregated metrics like `docker compose ps healthy` mean the service is actually working"** — wrong, §0.5.112. Healthcheck is "did the heartbeat fire", not "is the app functional". Trust service-level logs.
- **"`gh pr merge --squash --delete-branch` with auto-merge enables it"** — currently FALSE for this repo (auto-merge disabled at repo level). Use the form `gh pr checks <N> --watch` first, then `gh pr merge <N> --squash --delete-branch` after green.
- **"VPS CC won't prompt because settings.json is wide open"** — FALSE for hardcoded heuristics (PR #14), Edit/Write tool prompts, and `.claude/own-settings` guardrails. Use `--dangerously-skip-permissions` if zero prompts is the bar, or click "always allow X" persistent options.

**Next session rule: if a claim is quantitative, re-verify it.** Account balance, position count, open order count, test count, line counts in PR diff — all of these need a fresh probe before being repeated.

---

## 10. Session discipline lesson (Sessions 4–5)

**The meta-pattern this session series exposed: I (chat) repeatedly designed comprehensive solutions when the user explicitly wanted incremental, small-step movement.** Three sessions in a row, the user pushed back with some variant of "stop making me work" / "let me move faster" / "you're interrupting me again". Each time the response from me was another mega-artifact (long brief, full PR template, multi-stage runbook).

The autonomy bootstrap was a textbook case: instead of one small fix ("here's the immediate next step, then we iterate"), I shipped a 500-line bootstrap that VPS CC executed against a permission system whose architecture I didn't fully understand. The bootstrap took 4 iterations across PRs #9, #11, #12, #13, #14 to converge — and the residual Edit/Write tool prompts are still unresolved.

**The corrective lesson, codified for the next session:** when the user says "move faster", the first action is to ship the smallest verifiable thing, then ask "is this still the right direction?". Don't pre-build the full plan in one message. The cost of a wrong-direction mega-brief is the entire session.

**Enforcement rules for next session:**
1. **Smallest verifiable step.** Each chat reply ships either (a) one paste, one outcome — or (b) a "here's the decision point, pick A or B" — never a 30KB plan.
2. **Probe before specifying** any external interface (env vars, library methods, compose interpolation). The verification-before-completion skill applies to brief-design as much as code-claim moments.
3. **VPS CC's autonomy skill is the source of truth on pattern discipline.** When drafting a brief, mentally run it through `.claude/skills/vps-cc-autonomy/SKILL.md` — does it have `cd X &&` / `$(...)` / `;` / heredocs / chained `sleep`? Rewrite before sending.
4. **Trust VPS CC to drive.** Do not embed full file contents in briefs when VPS CC can read them from the repo or create them inline. Reduces brief size by 80%+ and makes the brief readable in chat.

---

## 11. Logging verbosity — what to demand from any new code

Standing principles for what well-logged code looks like in TradeFlow (refined Sessions 4-5):

- **Every state transition logs `[ORCH] state: action — reason`.** Old → new is implicit in the action verb.
- **Every IBKR API call that crosses the network logs entry AND exit.** `[ib_client] connect attempt — host=... port=...` then `[ib_client] connected — server_version=N`.
- **Every healthcheck tick logs once.** `[ORCH] healthcheck: ok — ib_time=<ISO>` on success, `[ORCH] healthcheck: failed — <reason>` on transient (do NOT raise from the healthcheck — the loop continues).
- **Signal handling logs the signal.** `[ORCH] shutdown: signal_received — signal=SIGTERM`.
- **Shutdown path logs each cleanup step.** `[ORCH] shutdown: ib_disconnected` → `db_closed` → `done — exit_code=0 duration_sec=<N>`.
- **Configuration warnings log loudly.** The Supabase-missing warning is the template: WARNING level, names the env vars, points at the next PR where it becomes load-bearing.
- **No `print()` calls in production paths.** Use `LOGGER = logging.getLogger(__name__)` and `LOGGER.info` / `.warning` / `.error`.
- **No silently swallowed exceptions.** Even transient retries log the attempt number and the exception class.

PR #10 orchestrator logging follows this. Use it as the template for PR #9 (state machine).

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill at `/mnt/skills/user/code-pr-brief/SKILL.md` for the full template. Carry-forward enforcement:

- Patch constraints (EXACTLY N files), verification gates with `git diff main -- ...` MUST-be-empty checks
- Test safety guardrails (fresh mocks per test, AsyncMock for async, factory pattern, no shared state)
- `--base main` explicit pin per §0.5.108
- "What I got wrong during this PR" section at the end (§5-equivalent)
- §0.5.108 reminder at the top of every brief — three places verify base = `main`
- Pattern discipline per §0.5.117–.118: no `cd && X`, no `$(...)` / `${VAR}` / `;` / heredocs in any bash, use `Write` for file content and `-F` / `--body-file` for git/gh

---

## 13. Current PR brief in flight (if any) — hand this to Claude Code as-is

**None in flight at handoff close.** Next brief to draft is **PR #9 — state machine**. It needs:

- Schema design for `lifecycles` table (Botty-stable-identity pattern — `lifecycle_id` UUID, `symbol`, `state` enum NOT in uniqueness constraint, `direction`, `created_at`, `updated_at`, plus state-specific columns)
- IDLE → ENTERING → ACTIVE → EXITING → CLOSED state transitions + invariants
- Persistence on every transition (Supabase upsert)
- Reconciliation cadence design (every healthcheck tick or separate scheduled tick — DECISION NEEDED)
- Tests: state transition matrix, invariant violation rejection, persistence assertions
- Smoke runbook: container boots, transitions a fake lifecycle through all 5 states, persists each transition, recovers correctly after restart

**Operator decision needed before drafting:** Supabase project provisioned + populated in `.env`? If not, that's blocker #1.

---

## 14. Canonical references (in order of authority)

1. **`main` HEAD on `ohad-oren111/tradeflow`** at `a492999` (or later) — what actually runs
2. **`docs/pr-briefs/PR8_orchestrator.md` + this handoff** at `a492999` — verified architecture reality
3. **IBKR paper account DUQ-prefix** queried via `ib_async.IB().accountSummaryAsync` — source of truth for account state (§0.5.98)
4. **`tradeflow-ib-gateway` container logs** via `docker logs tradeflow-ib-gateway` — IBC and Java gateway runtime truth
5. **`tradeflow-app` container logs** via `docker logs tradeflow-app` — orchestrator runtime truth when running
6. **`.claude/skills/vps-cc-autonomy/SKILL.md`** on `main` at `a492999` — VPS CC behavioral source of truth
7. **`/home/tradeflow/runbooks/PR8_smoke_test.md`** on the VPS — verified probes for PR #8 smoke
8. **This handoff (v4)** — session context, NOT long-term authority
9. **v3 and earlier handoffs** — historical, ignore any claim that contradicts 1-7

---

## 15. First 15 minutes of the next session

1. **Read §§0.5, 1, 2, 5, 6 of this handoff.** §5 is the single most important — both wrong diagnoses this session were §0.5.97 violations on env-var/compose-interpolation that the next session can avoid by probing before specifying.

2. **Launch VPS CC from project root** (so the no-`cd` rule from `vps-cc-autonomy` holds): operator decision is whether to use `--dangerously-skip-permissions`. The autonomy skill + branch protection + GitHub auth remain regardless.
   ```
   ssh tradeflow
   cd ~/tradeflow
   claude --dangerously-skip-permissions
   ```
   (or `claude` without the flag if you want to keep the in-flow prompts as defense-in-depth.)

3. **Run §6 verification block V0–V5.** Confirm: main HEAD ≥ `a492999`; 4 skills in repo (`vps-cc-autonomy`, `verification-before-completion`, `architecture-question-gate`, `prod-debug-discipline`); ib-gateway healthy; live IBKR probe returns server v178 + 0 positions; 40 tests pass; autonomy skill auto-loads.

4. **First action — operational debt cleanup decisions.** Operator decides which (if any) of: (a) flip IBC Read-Only off, (b) provision Supabase project, (c) flip repo default branch, (d) enable repo auto-merge, (e) regenerate gh PAT with `read:org`, (f) rotate IBKR password to scrub the DUQ leak. None are blocking PR #9; all are accumulating debt.

5. **Second action — draft PR #9 brief** for the state machine. Operator + chat decide together: schema shape (single `lifecycles` table per Botty-Path-C identity-stable pattern), reconciliation cadence, invariants. Chat drafts the brief; VPS CC implements.

6. **Smoke test PR #9 after merge** via the `vps-smoke-test-runbook` skill. Verify orchestrator can transition a fake lifecycle through all 5 states with persistence.

---

## 16. How to publish this handoff

**Path A — VPS Claude Code brief (preferred, autonomous):**

See the brief in the accompanying message — VPS CC pulls main, verifies HANDOFF_v3 is present, saves HANDOFF_v4 from embedded content, commits, opens PR, watches CI, squash-merges.

**Path B — Manual fallback (if VPS CC unavailable):**

```bash
scp HANDOFF_v4.md tradeflow:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v4.md
ssh tradeflow git -C /home/tradeflow/tradeflow add docs/handoffs/HANDOFF_v4.md
ssh tradeflow git -C /home/tradeflow/tradeflow commit -m "docs: add v4 handoff (Sessions 4-5, PR #6 smoke PASS + 6 PRs landed)"
ssh tradeflow git -C /home/tradeflow/tradeflow push origin main
```
(Will fail on the `push` step because of branch protection — fall back to creating a docs branch and opening a PR. The autonomous path A handles this correctly.)

The handoff exists only if saved to disk on `main` and committed. Until then, treat the chat output as draft.

---

*End of handoff v4. Target lifespan: until PR #9 (state machine) ships and the system has run paper for ≥ 2 weeks. Then v5 captures the state-machine learnings and v4 retires.*
