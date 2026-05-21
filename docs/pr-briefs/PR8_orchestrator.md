# TradeFlow — Claude Code PR Prompt: PR #8 — Orchestrator wiring (`IBClient` + `SupabaseClient`, no trading logic)

> **§0.5.108 — PR BASE PIN.** Open this PR with `--base main` EXPLICITLY. CC Web defaults to whatever branch is currently checked out in its sandbox, which can be a stale feature branch. Session 3 lost PR #4 to that exact failure mode. Verify base = `main` at three places: (a) in this brief, (b) in the pre-push checklist, (c) at PR-create time (`gh pr create --base main --head ...`).

## Role

You are a senior Python developer working on **TradeFlow**, an autonomous MNQ futures trading bot on Interactive Brokers, currently in **paper-only pre-deployment** (capital at risk: **$0**). The plan is to graduate to live trading at $5k after 50 paper trades + 30 paper days. You write clean, tested, production-grade code. You never modify files you weren't asked to modify. You always study existing code patterns before writing new code. You understand this is a production system in the making — even paper bugs cost real time.

You are verbose in your logging. Format: `[COMPONENT] state: action — reason`. Mirror the existing `[ib_client] ...` / `[supabase] ...` shapes in `src/clients/`.

You second-guess your own assumptions. Before writing code, you state what you expect the existing pattern to be, then verify by reading the actual file. You NEVER trust prior sessions' claims about API behavior without running a quick verification first.

Phase-1 PR #6 (the prior PR) added `IBClient` and `SupabaseClient` as thin wrappers — this PR wires them into a long-running orchestrator. **NO trading logic, NO order placement, NO strategy.** Just lifecycle + healthcheck + graceful shutdown. State machine lands in PR #9.

## Context

- **Repo state at brief-write time**: HEAD on `origin/main` is `f5a39f8` (Session-3 v3 handoff merge). PR #6 (`f29ae56`, "feat(phase-1): IB Gateway Docker + ib_async client + Supabase REST stub + smoke test", 13 files, 775+ insertions) shipped the building blocks: `src/clients/ib_client.py`, `src/clients/supabase_client.py`, `scripts/test_ib_connect.py`, healthcheck, 30-test suite.
- **Session 4 (today, 2026-05-21)** verified PR #6 in prod end-to-end: smoke `[smoke] PASS`, `accountSummaryAsync` returns a $1M paper account, 30/30 tests green, IB Gateway logged in cleanly after a transient IBKR lockout cleared. **This PR builds on a verified-green PR #6 baseline.**
- The two clients are read-only by design at this stage: `IBClient.get_positions/get_portfolio/get_open_trades` and `SupabaseClient.select/insert/upsert`. PR #8 must not add new public methods to either client; it must call only what's already there.
- **Read-Only API mode is currently enabled in IBC config.** `openOrdersAsync` and `completedOrdersAsync` time out under this mode (confirmed in Session 4 smoke output). Use `reqCurrentTimeAsync()` or `accountSummaryAsync(IBKR_PAPER_ACCOUNT)` for healthchecks — both work under Read-Only.
- No `tradeflow-app` service exists in `docker-compose.yml` yet — PR #8 adds it. The `ib-gateway` service is already running on the VPS and stays untouched.

## 🏗️ System Architecture & Recent Learnings

- Container (new): `tradeflow-app` — runs the orchestrator process. Same `ib-gateway` peer container (already up).
- Language: Python 3.11 in the container (host Python is 3.10.12, ops only — §0.5.T5)
- Database: Supabase via **custom REST httpx wrapper** (`SupabaseClient`), NOT `supabase-py` (§0.5.T4)
- Broker library: **`ib_async`** (active fork), NOT `ib_insync` (§0.5.T2)
- Env vars this PR touches (read-only): `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `IBKR_PAPER_ACCOUNT`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE`, `TRADING_MODE` (must be `paper`), new `ORCH_HEALTHCHECK_INTERVAL_SEC` (default 60), new `ORCH_LOG_LEVEL` (default `INFO`)
- Logging: module-level `LOGGER = logging.getLogger(__name__)`, `[ORCH] state: action — reason` format
- Secrets path: `/home/tradeflow/.tradeflow-secrets/.env` (§0.5.96). Loaded via `python-dotenv` `load_dotenv()`, NOT bash `source` — §0.5.110 trap (one .env line has a value containing whitespace + `PM` that breaks bash sourcing).

### Key Architecture Constraints

1. **Runtime (asyncio):** Everything that touches `ib_async` is async. `SupabaseClient` is async (httpx). The main loop is `asyncio.run(orchestrator.run())`. Use `asyncio.sleep`, never `time.sleep`.

2. **Signal handling — Docker gotcha:** `asyncio.add_signal_handler` does NOT work in some minimal Docker images / pid=1 contexts. Use `signal.signal(SIGTERM, handler)` where the handler does `loop.call_soon_threadsafe(stop_event.set)`. The orchestrator awaits `stop_event.wait()` in its main loop and exits cleanly when it fires. Verify the asyncio version of the same pattern by reading a neighbor — there's no neighbor in this repo yet, so check the asyncio docs / use the `signal.signal + threadsafe set` pattern as canonical for Docker.

3. **IBKR client IDs (§0.5.T1):** orchestrator uses `IBKR_CLIENT_ID`. The smoke script uses `IBKR_CLIENT_ID_SMOKE`. Future feed/broker split: feed=`IBKR_CLIENT_ID`, broker=`IBKR_CLIENT_ID + 1`. **In PR #8 the orchestrator is read-only, so only one client ID is used (`IBKR_CLIENT_ID`).** Do not split into feed/broker here — that lands in a later PR.

4. **Healthcheck must work under Read-Only API mode.** `await ib.reqCurrentTimeAsync()` is the cheapest probe. As an alternative, `await ib.accountSummaryAsync(IBKR_PAPER_ACCOUNT)` confirms account-binding too but is heavier. Use the lighter `reqCurrentTimeAsync` for the periodic loop; one `accountSummaryAsync` call at startup is acceptable for binding verification.

5. **Scope boundary (CRITICAL):** This PR does NOT
   - place orders
   - implement strategy
   - implement state machine (PR #9 territory)
   - touch `src/clients/*`
   - touch tests for IBClient or SupabaseClient
   - introduce new client methods or change their signatures
   - reconcile positions to DB rows (that's PR #10+)
   - introduce kill-switch logic (§0.5.T4 exit-42 + systemd `RestartPreventExitStatus=42` — that lands when the systemd unit lands, not here)

6. **Design decision (A vs B):**
   - **A (default):** `Orchestrator` class in `src/orchestrator.py` with `__init__(ib, db, healthcheck_interval)`, `async def run()`, `async def shutdown()`. `main.py` is a thin entry point that constructs the orchestrator and calls `asyncio.run(orchestrator.run())`.
   - **B (rejected):** procedural `main.py` with a `while not stop_event.is_set()` loop inline.
   - **Choose A.** Rationale: PR #9 (state machine) plugs into `Orchestrator.run()` cleanly; B would need a refactor at PR #9. Class-based also makes unit tests trivial — inject mock IB + DB at construction.

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints

Files you WILL modify (EXACTLY 5):
- `src/orchestrator.py` (NEW)
- `main.py` (NEW; repo root)
- `tests/test_orchestrator.py` (NEW)
- `docker-compose.yml` (MODIFY — add `tradeflow-app` service)
- `Dockerfile` (TOUCH ONLY IF needed; the existing image may already cover the orchestrator's needs — verify in Task A. If no change required, do not modify; if a change is required, the diff should be ≤5 lines, e.g. adding a `python-dotenv` line if it's not already in `pyproject.toml`. If `python-dotenv` is already in `pyproject.toml` and the image installs it, leave `Dockerfile` untouched.)

Files you MUST NOT modify:
- `src/clients/__init__.py`, `src/clients/ib_client.py`, `src/clients/supabase_client.py`
- `tests/test_ib_client.py`, `tests/test_supabase_client.py`, `tests/test_healthcheck.py`, `tests/conftest.py`
- `tests/test_config.py`
- `scripts/test_ib_connect.py`, `scripts/healthcheck.py`
- `.github/workflows/*`
- `docs/handoffs/*`, `docs/architecture/*`, `docs/pr-briefs/*` (except this brief stays as it is)
- `.claude/skills/*`, `.claude/settings*.json`
- `pyproject.toml` (unless `python-dotenv` is genuinely missing — verify in Task A; if so, that's a 1-line add and bumps `EXACTLY 5` to `EXACTLY 6`)
- `.env.example` (no new env vars need leaking to the example; if `ORCH_HEALTHCHECK_INTERVAL_SEC` and `ORCH_LOG_LEVEL` are added, append them to `.env.example` AS A FOLLOW-UP PR — out of scope here)
- `~/.tradeflow-secrets/.env` (forbidden hard — never write to the secrets path)

Verification gates (run before pushing):
- `git diff main -- src/clients/` → MUST be empty
- `git diff main -- tests/test_ib_client.py tests/test_supabase_client.py tests/test_healthcheck.py tests/conftest.py tests/test_config.py` → MUST be empty
- `git diff main -- scripts/` → MUST be empty
- `git diff main -- .github/workflows/` → MUST be empty
- `git diff main -- docs/handoffs/ .claude/` → MUST be empty
- `git diff main --stat` → should show EXACTLY 4 or 5 files changed (depending on Dockerfile)

### 2. Code Quality

- `black --check src/orchestrator.py main.py tests/test_orchestrator.py` passes
- `ruff check src/orchestrator.py main.py tests/test_orchestrator.py` passes
- No unused imports or variables
- Line length under 100 chars where possible
- Type hints on all public methods; no signature changes to `IBClient` or `SupabaseClient` public methods
- Verbose logging format: `[ORCH] state: action — reason`
- One import per line (ruff E401)

### 3. Safety

- All pre-existing tests still pass. Known failing (do NOT fix):
  * **None expected.** Run `.venv/bin/python -m pytest tests/ -q` in Task A. Should show `30 passed`. If anything fails BEFORE your changes, STOP and report — do not patch around it.
- No unexpected DB writes. `SupabaseClient.insert`/`upsert` must NOT be called by the orchestrator in this PR (no rows to write — strategy isn't here yet).
- No unexpected IBKR API calls. The orchestrator's only IB calls in this PR are: `connectAsync` (via `IBClient.connect`), `reqCurrentTimeAsync` (periodic healthcheck), `accountSummaryAsync` (one startup binding check), `disconnect`.
- No changes to method signatures in `src/clients/`.
- If you find a bug adjacent to the fix, DOCUMENT IT in the PR description (Task E). Do NOT fix it. Scope creep is the #1 cause of bad PRs.

## 🧩 Current Mission: Wire `IBClient` + `SupabaseClient` into a long-running orchestrator with graceful SIGTERM and periodic IBKR healthcheck. NO trading logic.

### Objective

Add an `Orchestrator` class that:

1. Owns lifetime of an `IBClient` and a `SupabaseClient` instance
2. On `run()`:
   - logs `[ORCH] startup: begin — reason=process_start`
   - connects `IBClient` (calls `IBClient.connect`), logs `[ORCH] startup: ib_connected — server_version=N`
   - runs a one-shot `accountSummaryAsync(IBKR_PAPER_ACCOUNT)` startup check, asserts at least `NetLiquidation` row is present, logs `[ORCH] startup: account_bound — account_prefix=DUQ`
   - enters a `while not stop_event.is_set()` loop
   - each iteration: `await ib.reqCurrentTimeAsync()` (cheap healthcheck), log `[ORCH] healthcheck: ok — ib_time=YYYY-MM-DD HH:MM:SS`
   - awaits `asyncio.wait_for(stop_event.wait(), timeout=ORCH_HEALTHCHECK_INTERVAL_SEC)`; on timeout, loop back. On set, exit.
3. On SIGTERM (or KeyboardInterrupt for local dev):
   - logs `[ORCH] shutdown: signal_received — signal=SIGTERM`
   - sets `stop_event` (via `loop.call_soon_threadsafe`)
   - run-loop drops out
   - `IBClient.disconnect()` called in `finally:`
   - logs `[ORCH] shutdown: done — exit_code=0`
4. On unhandled exception in the run loop:
   - logs `[ORCH] shutdown: exception — type=Foo msg=Bar`
   - `IBClient.disconnect()` in `finally:`
   - re-raises (process exits non-zero so Docker restarts under restart policy — but DO NOT set restart policy in this PR; that's an operational decision documented in §0.5.T4 for the systemd unit)
5. `main.py` is the entry point:
   - `load_dotenv()` from `os.environ.get('ENV_FILE', '.env')` (mirrors `scripts/test_ib_connect.py` pattern)
   - reads env vars
   - constructs `IBClient(host, port, client_id)` and `SupabaseClient(url, service_role)`
   - constructs `Orchestrator(ib, db, healthcheck_interval=int(os.environ.get('ORCH_HEALTHCHECK_INTERVAL_SEC', '60')))`
   - `asyncio.run(orchestrator.run())`

### Task A: Audit (do this BEFORE writing any code; report findings in PR description)

Read these files and report a 3-5 line finding:

1. `src/clients/ib_client.py` lines 1-92 (full file) — confirm `connect`/`disconnect`/`is_connected` shape; confirm `IB` instance is at `self._ib`. Cite the line numbers of the methods you'll call.
2. `src/clients/supabase_client.py` — confirm there's an `async close()` method (the orchestrator's `finally:` will call it). Cite line.
3. `scripts/test_ib_connect.py` — mirror its `load_dotenv()` + env-reading pattern; cite the pattern lines you'll mirror in `main.py`.
4. `docker-compose.yml` — read the existing `ib-gateway` service in full; the new `tradeflow-app` service mirrors its `env_file:` + restart/log shape; cite the relevant block. Also read `Dockerfile` and decide: does it already produce an image that can `python -m main`? If not, what's the minimal delta?
5. **Run** `.venv/bin/python -m pytest tests/ -q --no-header` — confirm baseline is **30 passed**. If not, STOP and report.
6. **Run** `grep -nE '@pytest.mark.asyncio|asyncio_mode' tests/conftest.py tests/test_*.py | head -10` — find the project's actual async-test pattern. `tests/conftest.py` may already set `asyncio_mode = "auto"`. DO NOT GUESS — verify, then mirror.

Write the audit findings into the PR description (3-5 lines).

### Task B: Implement

**`src/orchestrator.py`** — `Orchestrator` class per Objective above. ~80-120 lines. Key methods:

```python
class Orchestrator:
    def __init__(
        self,
        ib: IBClient,
        db: SupabaseClient,
        *,
        paper_account: str,
        healthcheck_interval: float = 60.0,
    ) -> None: ...

    async def run(self) -> int:
        """Returns process exit code (0 on clean shutdown, non-zero on exception)."""

    async def _startup(self) -> None: ...  # connect, account-bind check
    async def _healthcheck_once(self) -> None: ...  # reqCurrentTimeAsync + log
    async def _shutdown(self) -> None: ...  # disconnect, close db, log
    def _install_signal_handlers(self) -> None: ...  # signal.signal(SIGTERM, ...)
```

Logging format examples (every state transition emits a line):

```
[ORCH] startup: begin — pid=12345 healthcheck_interval=60
[ORCH] startup: ib_connecting — host=127.0.0.1 port=4002 client_id=1
[ORCH] startup: ib_connected — server_version=178
[ORCH] startup: account_bound — prefix=DUQ net_liq=1000000.00
[ORCH] healthcheck: ok — ib_time=2026-05-21T15:39:03Z
[ORCH] shutdown: signal_received — signal=SIGTERM
[ORCH] shutdown: ib_disconnected
[ORCH] shutdown: db_closed
[ORCH] shutdown: done — exit_code=0 duration_sec=42.1
```

**`main.py`** — entry point. ~40-60 lines:

```python
def _build_orchestrator_from_env() -> Orchestrator: ...

def main() -> int:
    logging.basicConfig(level=os.environ.get('ORCH_LOG_LEVEL', 'INFO'),
                       format='%(asctime)s %(levelname)s %(message)s')
    load_dotenv(os.environ.get('ENV_FILE', '.env'))
    orch = _build_orchestrator_from_env()
    return asyncio.run(orch.run())

if __name__ == '__main__':
    sys.exit(main())
```

**`docker-compose.yml`** — add a `tradeflow-app` service:

- Build context: `.` (repo root)
- Dockerfile: `Dockerfile` (existing)
- `env_file: ${HOME}/.tradeflow-secrets/.env` (mirror `ib-gateway`)
- Depends on `ib-gateway` (just `depends_on: [ib-gateway]` — DO NOT add `condition: service_healthy` because the gateway's "healthy" is misleading per §0.5.112)
- Restart policy: **`unless-stopped`** (so SIGTERM stops it; `--no-restart` for shutdown via `docker compose down`)
- Networks: mirror `ib-gateway` (so app can reach `ib-gateway:4002` if we ever move off `127.0.0.1`; for now the app uses `IBKR_HOST=ib-gateway` instead of `127.0.0.1` when inside the container — verify by reading the existing compose network setup in Task A)
- Container name: `tradeflow-app`

**`Dockerfile`** — TOUCH ONLY IF needed. If `python-dotenv` is already in `pyproject.toml` and the existing image installs the deps, leave it alone. Otherwise, document the 1-line delta in PR description.

### Task C: Add tests

**`tests/test_orchestrator.py`** — ~6-8 tests. Mock `IBClient` and `SupabaseClient` at the wrapper boundary (NEVER patch raw `ib_async.IB` — that's what `IBClient` exists to insulate).

Tests:

1. `test_run_calls_connect_then_disconnect` — orchestrator's `run()` (with `stop_event` pre-set) connects, calls one healthcheck, disconnects. Assertions: `mock_ib.connect.await_count == 1`, `mock_ib.disconnect.call_count == 1`.
2. `test_startup_logs_account_binding` — orchestrator emits `[ORCH] startup: account_bound` log on successful `accountSummaryAsync`. Use `caplog` filter on `[ORCH] startup: account_bound`.
3. `test_healthcheck_loop_runs_n_iterations` — with `healthcheck_interval=0.01` and a `stop_event` that sets after 30ms, run-loop iterates ~3 times. Use `asyncio.wait_for(orch.run(), timeout=2.0)` so a misbehaving loop times out cleanly.
4. `test_sigterm_triggers_shutdown` — call `orch._handle_signal(signal.SIGTERM, None)`, assert `stop_event.is_set()`. (Unit test the signal handler directly — do NOT spawn a subprocess.)
5. `test_exception_in_run_loop_disconnects_ib` — mock `IBClient.connect` to raise; assert `run()` returns non-zero AND `disconnect()` was still called in `finally:` (use `mock_ib.disconnect.assert_called_once()`). Make sure mock is fresh per test.
6. `test_main_module_smoke` — `main()` returns the orchestrator's exit code. Stub `_build_orchestrator_from_env` to return a mock orchestrator whose `run()` returns 0; assert `main() == 0`. (Tests the wiring, not the orchestrator itself.)
7. `test_accountSummary_no_NetLiquidation_raises` — mock `accountSummaryAsync` to return empty list; assert startup fails fast with a clear error message containing "NetLiquidation".
8. `test_compose_app_service_defined` — `import yaml; assert 'tradeflow-app' in yaml.safe_load(open('docker-compose.yml'))['services']`. (Cheap structural check.)

**TEST SAFETY GUARDRAILS (read these before writing the tests):**

- Fresh `MagicMock()` per test — do NOT share across tests
- `mock_ib = AsyncMock(spec=IBClient)` so attribute access matches the real surface
- `mock_db = AsyncMock(spec=SupabaseClient)` similarly
- For `accountSummaryAsync`, mock returns a list of `AccountValue`-shaped objects (use `MagicMock(tag='NetLiquidation', value='1000000.00', currency='USD')` — DO NOT import the real `AccountValue` type)
- No `side_effect` lists without an explicit count comment (off-by-one StopIteration is the #1 silent failure here)
- Async pattern: **verify by reading `tests/conftest.py`** — if `asyncio_mode = "auto"`, no decorator needed; otherwise use `@pytest.mark.asyncio`. **Do not assume.**
- Use `caplog` for log assertions, not patching `LOGGER.info` directly
- `assertions use call_args_list filtered by first positional arg`, not call index, to survive future call reordering

### Task D: Verify completeness

Run these greps from repo root after implementing, BEFORE pushing. Every hit must be classified as expected/new/leftover:

```bash
grep -rn "[ORCH]" src/ main.py tests/test_orchestrator.py
# Expect: only in src/orchestrator.py (definitions) and tests/test_orchestrator.py (assertions). Classify any other hit.

grep -rn "asyncio.add_signal_handler\|signal.signal" src/ main.py
# Expect: `signal.signal(SIGTERM, ...)` in src/orchestrator.py only. No add_signal_handler (Docker-pid=1 incompatibility).

grep -rn "time.sleep" src/ main.py tests/test_orchestrator.py
# Expect: 0 hits. (asyncio.sleep, not time.sleep.)

grep -rn "supabase\." src/ main.py | grep -v "src/clients/supabase_client.py"
# Expect: 0 hits — orchestrator uses SupabaseClient methods, not supabase-py directly. §0.5.T4.

grep -rn "from ib_async" src/ main.py | grep -v "src/clients/ib_client.py"
# Expect: 0 hits — orchestrator uses IBClient methods, not raw ib_async. (Tests may import for type hints; that's OK — classify.)

grep -n "tradeflow-app" docker-compose.yml
# Expect: 1 hit (the new service definition).
```

### Task E: Out-of-scope investigation (~10 minutes, DO NOT fix)

The Session 4 smoke discovered the IBKR API is currently in **Read-Only mode** (IBC config). PR #8 doesn't place orders, so it's not blocked. But PR #11 (first order placement) will be. **In this Task E**, spend ~10 minutes reading the gnzsnz/ib-gateway image docs / `jts.ini` configuration surface and document in the PR description:

- What env var or config knob flips Read-Only API mode off
- Whether it's a per-session login choice or persisted
- Any IBKR portal-side toggle that interacts with it

DO NOT change anything. Just document. A follow-up PR will flip it when we're ready for orders.

### Task F: Post-merge smoke test (the owner runs this on the VPS)

```bash
cd /home/tradeflow/tradeflow
git pull --ff-only origin main
git log -1 --oneline
# Expect: <merge-commit-hash> Merge pull request #8 ... PR #8 orchestrator wiring

docker compose build tradeflow-app
docker compose up -d tradeflow-app
sleep 5
docker compose ps
# Expect: tradeflow-app Up <few seconds>, no Restarting

docker logs tradeflow-app --tail 50
# Expect at minimum these log lines:
#   [ORCH] startup: begin — pid=...
#   [ORCH] startup: ib_connected — server_version=178
#   [ORCH] startup: account_bound — prefix=DUQ ...
#   [ORCH] healthcheck: ok — ib_time=...
# STOP if you see "[ORCH] shutdown: exception" — capture full traceback and report.

docker logs tradeflow-app --since 90s | grep -c '\[ORCH\] healthcheck: ok'
# Expect: ≥ 1 if interval=60s (the default), ≥ 2 if you used a shorter interval for smoke.

# Graceful shutdown probe:
docker compose stop tradeflow-app
docker logs tradeflow-app --tail 20
# Expect:
#   [ORCH] shutdown: signal_received — signal=SIGTERM
#   [ORCH] shutdown: ib_disconnected
#   [ORCH] shutdown: db_closed
#   [ORCH] shutdown: done — exit_code=0
# STOP if exit code is non-zero or any shutdown line is missing.

# Restart for clean state
docker compose up -d tradeflow-app
```

## 📤 Expected Output

### Files modified (EXACTLY 4 or 5)
- `src/orchestrator.py` (NEW)
- `main.py` (NEW)
- `tests/test_orchestrator.py` (NEW)
- `docker-compose.yml` (MODIFIED — `tradeflow-app` service added)
- `Dockerfile` (CONDITIONAL — only if `python-dotenv` is genuinely missing from the existing image)

### Git diff stat (expected)
- `src/orchestrator.py` | ~90-130 +/-0
- `main.py` | ~40-60 +/-0
- `tests/test_orchestrator.py` | ~150-200 +/-0
- `docker-compose.yml` | ~15-25 +/0
- `Dockerfile` | ≤5 +/-0 (only if needed)

### PR description must include

1. **Summary** — one sentence
2. **Task A audit** — 3-5 line finding (with cited line numbers)
3. **Task D grep output** — full list with classifications
4. **Task E finding** — one paragraph on Read-Only API mode toggle
5. **Local test run** — tail of `pytest -q` output (expect 30+8=38 passed)
6. **Full suite run** — only documented failures (should be 0)
7. **Protected-file diff verification** — every `git diff main -- <path>` line shows empty
8. **Smoke test commands** — Task F bash block, verbatim
9. **Explicit scope statement**: "This PR does NOT place orders, implement strategy, change `IBClient`/`SupabaseClient` surfaces, add restart policy beyond `unless-stopped`, or touch `.env.example`."
10. **"What I got wrong during this PR"** — 1-3 lines on any assumption that turned out false while auditing or implementing. If nothing, say "nothing".

## 🔍 Pre-Push Checklist

### Code Quality
- [ ] `black --check src/orchestrator.py main.py tests/test_orchestrator.py` passes
- [ ] `ruff check src/orchestrator.py main.py tests/test_orchestrator.py` passes
- [ ] No unused imports
- [ ] No multi-import lines (`import x, y`)
- [ ] No signature changes to `IBClient` or `SupabaseClient`

### Tests — TEST SAFETY GUARDRAILS
- [ ] Fresh `MagicMock()` / `AsyncMock()` per test (never shared)
- [ ] `mock_db.upsert.return_value = ...` set explicitly where used (if used at all in tests — orchestrator doesn't write in PR #8)
- [ ] `mock_db.select.return_value = []` set if code calls select (it shouldn't in PR #8)
- [ ] No `side_effect` list without an explicit count comment explaining call ordering (off-by-one StopIteration is the #1 silent failure)
- [ ] No `patch()` on module-level factories; use injection or mock instance attributes
- [ ] Async decorator pattern matches neighboring tests in the same file (read `tests/conftest.py` and one neighbor test; do not assume `@pytest.mark.asyncio` is the pattern — if `asyncio_mode = "auto"` no decorator is needed)
- [ ] Assertions use `call_args_list` filtered by first positional arg, NOT call index
- [ ] Mock at the wrapper level (`mock_ib.connect`), not the raw library chain (`ib_async.IB().connectAsync`)

### Production Safety
- [ ] Every entry in "Verification gates" shows empty diff (`src/clients/`, `tests/test_ib_client.py`, etc)
- [ ] Task D grep lists all sites; confirms nothing missed
- [ ] PR description includes Task F smoke test for the owner
- [ ] PR description explicitly states cleanup is NOT in this PR
- [ ] PR description notes any adjacent bugs found (Task E — Read-Only mode toggle)
- [ ] PR description includes "What I got wrong" section
- [ ] **PR base = `main`** (verified via `gh pr create --base main --head <branch>` — §0.5.108)

## ⚠️ Known Gotchas (carry forward verbatim; do NOT shrink)

1. **`asyncio.add_signal_handler` does NOT work in Docker pid=1 / minimal images.** Use `signal.signal(SIGTERM, handler)` where the handler does `loop.call_soon_threadsafe(stop_event.set)`. The orchestrator awaits `stop_event.wait()` in its main loop.
2. **`asyncio.sleep`, not `time.sleep`.** `time.sleep` blocks the event loop and breaks the healthcheck cadence.
3. **Shutdown test pattern: `asyncio.wait_for(orch.run(), timeout=2.0)`.** A misbehaving run-loop must time out cleanly, not hang the test runner.
4. **§0.5.108 — `--base main` explicit.** CC Web's default base = whatever its sandbox is on. Session 3 lost PR #4 to this. Pin three times: this brief, the pre-push checklist, the `gh pr create` invocation.
5. **§0.5.109 — `env_file:` ≠ `${VAR}` interpolation source.** Compose file's `${HOME}` interpolation reads shell env or working-directory `.env`. The new `tradeflow-app` service uses `env_file: ${HOME}/.tradeflow-secrets/.env` — same pattern as `ib-gateway`. The repo's `~/tradeflow/.env` is a symlink to `~/.tradeflow-secrets/.env` (Session-3 fix); do not remove that symlink.
6. **§0.5.110/.114 — `.env` strict `KEY=VALUE` format; whitespace cleanup non-negotiable.** Session 4 confirmed line 17 of the current `.env` has a value containing whitespace + `PM` that bash `source` chokes on. **`main.py` uses `load_dotenv()`, NOT bash `source`** — python-dotenv parses correctly. Do not introduce any code path that bash-sources the .env.
7. **§0.5.112 — Docker `healthcheck` status during `start_period` is misleading.** `tradeflow-app` should `depends_on: [ib-gateway]` WITHOUT `condition: service_healthy`; if you need to wait for IB Gateway, the orchestrator's `IBClient.connect(timeout=...)` is the right place to handle it (retry-with-backoff inside the orchestrator's `_startup`, NOT compose's healthcheck dance). For PR #8, a single `connect()` attempt is sufficient — retry logic lands later. If `connect()` raises, the orchestrator logs and exits non-zero.
8. **§0.5.T1 — IBKR client IDs.** Orchestrator uses `IBKR_CLIENT_ID` (single). Smoke uses `IBKR_CLIENT_ID_SMOKE` (separate). The feed/broker split (feed=`IBKR_CLIENT_ID`, broker=`IBKR_CLIENT_ID + 1`) lands when broker logic lands. **Not here.**
9. **§0.5.T2 — `ib_async`, NOT `ib_insync`.** pip name `ib_async`, import `from ib_async import IB`.
10. **§0.5.T3 — All three IBClient read methods (`get_positions`, `get_portfolio`, `get_open_trades`) must be preserved.** Orchestrator doesn't call them in PR #8, but it must not delete them.
11. **§0.5.T4 — Supabase via custom REST httpx wrapper.** `SupabaseClient`, NOT `supabase-py`. No `from supabase import` in `src/orchestrator.py` or `main.py`.
12. **§0.5.T5 — Python 3.11 in container.** Type hints can use 3.11 syntax (`list[X]`, `int | None`).
13. **Read-Only API mode caveat.** `openOrdersAsync` and `completedOrdersAsync` time out under Read-Only mode (confirmed in Session 4 smoke). Healthcheck uses `reqCurrentTimeAsync()` (works under Read-Only). `accountSummaryAsync` works too — used for startup binding check.
14. **§0.5.96 — Active secrets path is `/home/tradeflow/.tradeflow-secrets/.env`.** Not Botty-era variants.
15. **§0.5.115 — Autonomy default.** Drive Task A→F to completion. Do not ask the owner for permission to proceed within scope; STOP and ask only when scope is genuinely ambiguous or you hit unexpected state.
16. **Pre-existing test count is 30.** After PR #8 the count is ~38. If you find that count is anything else when you run `pytest -q` in Task A (before changes), STOP — drift in main since brief was drafted.
