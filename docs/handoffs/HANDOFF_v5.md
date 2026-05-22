# TradeFlow — Handoff v5 (bot live in paper, PR #11 deployed, first RTH Tuesday 2026-05-26)

*Handoff from end of Session 6, Friday 2026-05-22 evening (~02:00 UTC Sat 2026-05-23). Orchestrator is **running in paper** on PR #11 code (`4964e47`). Account `DUQ…` (paper, $1M NetLiq), positions = `[]`, openTrades = `[]`. Reconciler drain ticks every 30s, full-scan every 5 min. EOD scheduled for 3:58 pm ET daily. **Monday 2026-05-25 is Memorial Day — markets closed. First real RTH session is Tuesday 2026-05-26 09:30 ET.** Session 7 has a full weekend day of slack to refine process before any signal can fire. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read sections 1–6 first — that's state-of-the-system as of handoff. Sections 7–13 are reference material. Section 14 is the single-file source of truth to consult when this handoff disagrees with itself or a live observation: source code on `main` at commit `4964e47` or later.

**Do not trust this doc alone.** Run the verification block in §6 before writing any code. Specifically confirm: `git log -1` shows `4964e47` or later; `docker ps` shows `tradeflow-app` and `tradeflow-ib-gateway` both running with recent uptime; broker `positions()` and `openTrades()` are empty.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

### Baseline (from handoff_template.md, every project)

**Copy-paste instruction style.** Every action recommended to the operator must be a copy-paste-ready bash block. Self-contained commands, chained with `&&` or grouped. Source env vars explicitly in the same block. Expected output described immediately below each block, plus a decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**Learning-delivery discipline.** Every time you learn something new — a bug pattern, a corrected assumption, an environmental fact, a diagnostic finding — surface it immediately in the chat, formatted as a markdown snippet the operator can paste verbatim into the running handoff queue. Do not wait until end-of-session.

**Read before diagnosing.** When debugging a complex state bug, read the full startup log and 3–5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency language ("capital at risk", "churning fees", "spiraling"), hit the source of truth — live API, live DB, raw log file — not aggregated metrics.

**Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise. The operator does not run smoke tests by hand. Smoke runs end-to-end via VPS Claude Code, returns structured PASS/FAIL.

### Carried forward verbatim from HANDOFF_v4 §0.5

**§0.5.97–.123 + §0.5.T1–T5 — see `docs/handoffs/HANDOFF_v4.md` in repo on `main` for the full enumeration.** Highlights for quick reference:

- **§0.5.97 (probe-before-specify)** — Probe external specs (broker contracts, exchange fees, schema, library APIs, `.env` key names, compose interpolation) against source before baking into briefs. The single most common source of wrong PRs.
- **§0.5.98 (broker is ground truth)** — Broker/exchange state is ground truth for position/fill/capital claims — NOT internal DB tables.
- **§0.5.103** — CC chained Bash checks per-subcommand; prefer broad allows + targeted denies for permission management.
- **§0.5.104** — CC meta-safety on `.claude/` — reads accepted, writes declined.
- **§0.5.105** — Permission additions are comprehensive sweeps, not iterative patches.
- **§0.5.116** — CC Web dropped — operator works two-tier: chat for strategy/briefs/handoffs, VPS CC for implementation/ops/smoke tests.
- **§0.5.117/.118 (bash pattern discipline)** — CC hardcoded heuristics that cannot be silenced via settings.json. Triggers: `cd X &&`, `;` separators, `$(...)`, `${VAR}`, heredocs, chained `sleep`. Workarounds: use `git -C path` not `cd`; one Bash call per `;`; Python helpers for interpolation; Write tool for heredocs; `git commit -F /tmp/commitmsg.txt`; `--body-file /tmp/pr_body.md`; `/tmp/wait_*.py` polling helper for waits >10s.
- **§0.5.T2 (long-only bracket)** — Long-only bracket-child SL is DISABLED by default; use separate GTC stop placed in parent fillEvent handler (option γ). MKT parent (transmit=False) + LMT TP child (transmit=True, parentId=parent.orderId) submitted atomically.
- **§0.5.T3 (canonical IB position read)** — `IBClient.get_portfolio() → list[PortfolioItem]` is the canonical source for position state (qty + avgCost). `get_open_trades()` for working orders. `get_positions()` for symbol matching.
- **§0.5.T5** — Bracket invariants hold end-to-end. No OCA group.

### New this session (Session 6) — append-only

- **§0.5.124 — `gh repo view --json autoMergeAllowed` errors.** Use `gh api repos/<owner>/<repo>` for repo metadata when the typed-field path fails. The `--json` flag rejects fields that aren't in gh's known schema; the raw API endpoint exposes everything.
- **§0.5.125 — `.env` quote discipline.** Values with whitespace need quoting. python-dotenv tolerates both quoted and unquoted; `bash source` requires quoting. Line 17 in `~/.tradeflow-secrets/.env` trips `source` but python-dotenv handles it cleanly. Don't try to "fix" it — orchestrator reads via python-dotenv, not source.
- **§0.5.126 — IBC `AutoRestartTime` format.** Expects 12-hour `H:MM AM/PM`, not 24-hour. `23:30` parses but silently fails to schedule. Use `11:30 PM`.
- **§0.5.127 — System `python3` lacks `python-dotenv`.** Always invoke `/home/tradeflow/tradeflow/.venv/bin/python3` for any script that reads from `.env`. System python (apt) doesn't have project deps.
- **§0.5.128 — `gh auth refresh -s <scopes>` upgrades classic PAT to OAuth.** Via device flow, retires the `~/.config/gh/hosts.yml` workaround. Current token: `gho_*` OAuth with scopes `gist,read:org,repo,workflow`.
- **§0.5.129 — Supabase project security config.** Project ref prefix `vzlpxaif*`, region us-east-1. Data API = ON, auto-expose = ON, auto-RLS = ON. `service_role` key bypasses RLS for orchestrator writes; never use `anon` for backend.
- **§0.5.130 — Strategy is MA50+MA100+ADX bounce, NOT pure SMA100.** Identifier `strategy="sma100_bounce"` is the stable schema string (§0.5.133), but real condition is: MA50 > MA100, ADX ≥ 20, low ≤ MA100 + 5pt buffer, bullish candle close, outside session-edge windows. Requires `pandas>=2.2,<3.0` and `numpy>=1.26,<3.0` already in `pyproject.toml`.
- **§0.5.131 — `InvariantViolation` → `InvariantViolationError`.** Renamed for ruff N818 compliance. Exception class hierarchy unchanged.
- **§0.5.132 — Recovery ENTERING→ACTIVE must repopulate broker fields.** Use `_broker_field_updates_for(lifecycle, target=State.ACTIVE)` which pulls avg cost from broker, computes entry_filled_at = now(). Don't trust the DB's entry_qty/entry_price on recovery — they may have been written before fill arrived.
- **§0.5.133 — `strategy="sma100_bounce"` is the stable schema identifier.** Despite the MA50+MA100+ADX reality (§0.5.130), the identifier is sticky. Changing it would orphan all existing lifecycle rows in `lifecycles.strategy` column. New strategies get new identifiers; existing ones don't get renamed.
- **§0.5.134 — SSH-resilient session discipline.** Stage long artifacts (PR bodies, commit messages, complex Python helpers) to `/tmp/` via the Write tool FIRST, then commit/push/PR. The Write confirm-prompt blocks the session — if SSH dies mid-prompt, the work is lost. Lesson from PR #10 timeout: body was inline in the brief, regenerated via paste-from-chat on resume. Cleaner: stage to /tmp/ early.
- **§0.5.137 — Brief-design lint: imports vs Dockerfile COPY.** Before declaring `Dockerfile` (or `docker-compose.yml`, `requirements.txt`) as MUST-NOT-MODIFY in a PR brief, grep the new code's imports against the Dockerfile's `COPY` set. PR #10 added `from config.instruments import MNQ` to three modules; Dockerfile only COPYed `src/` + `main.py`. Image crash-looped with `ModuleNotFoundError`. Hotfix PR #18 added `COPY config ./config`. **The grep is mandatory at brief-design time.**
- **§0.5.138 — Stub packages risk future Dockerfile gaps.** Top-level packages `strategy/`, `execution/`, `risk/`, `features/`, `backtest/`, `comms/`, `data/`, `scripts/` are empty `__init__.py` only as of `4964e47`. If any becomes populated AND imported by `src/`, the Dockerfile needs another `COPY <pkg> ./<pkg>` line OR a switch to `COPY . .` with tightened `.dockerignore`. §0.5.137 applies.
- **§0.5.139 — Class-namespace collision discipline (THE BIG CATCH OF THIS SESSION).** Before specifying a new method on an existing class in a PR brief, grep the class for the chosen method name. PR #10 added `Orchestrator._handle_signal(signal: Signal)` (trading handler) but the class already had `_handle_signal(signum, frame)` (SIGTERM handler from the daemon scaffold). Python silently keeps only the second definition; `_on_new_bar` → `_handle_signal(signal_or_none)` called the SIGTERM handler with a `Signal` dataclass as `signum` → `_stop_event.set()` → graceful shutdown. **Bug lived in production from `6d87c3c` (PR #10 merge) until `4964e47` (PR #11 merge).** Masked because PR #10 tests mocked the call site. **First real bar-event signal Tuesday would have shut the bot down instead of placing a trade.** Caught by VPS CC during PR #11 audit (reading every line of `orchestrator.py` before specifying the wire-up). PR #11 renamed the trading handler to `_handle_trade_signal`. Brief-design rule: **when adding a method, grep the class for that name first**.

---

## 1. Where we are (as of Friday 2026-05-22 ~02:00 UTC Sat 2026-05-23)

### Live production state

- **`tradeflow-app`** — running on Hetzner VPS `5.78.212.37` (`tradeflow@ubuntu-4gb-hil-1`), image rebuilt from `4964e47` post-PR-#11 smoke retry. Process running `python main.py` as PID 1. SIGTERM handler wired correctly (PR #11 fix). Healthcheck loop emits `[ORCH] healthcheck: ok` every 60s. Reconciler emits `[RECON] tick: drain_complete` every 30s. EOD task scheduled for next 3:58 pm ET fire.
- **`tradeflow-ib-gateway`** — `ghcr.io/gnzsnz/ib-gateway:stable`, healthy, logged in to paper account `DUQ…` (account ID leak accepted by operator). Server version 178. `ReadOnlyApi=no` confirmed at `/home/ibgateway/ibc/config.ini:396` post-flip.
- **Position state** — broker `positions()` = `[]`, `openTrades()` = `[]`, $1M paper NetLiq.
- **DB state** — Supabase project `vzlpxaif*` (us-east-1). `lifecycles` table empty. `lifecycle_events` table empty. RLS enabled on both; `service_role` bypasses.
- **No manual operational overrides** — no kill switches set, no halt flags raised, no crons paused.
- **Calendar** — Monday 2026-05-25 is Memorial Day (US markets closed). First RTH bar after handoff: Tuesday 2026-05-26 09:30:00 ET. Strategy gate opens at 09:35:00 ET (5-min session-edge buffer per `risk_params.session_edge_no_trade_minutes=5`).

### What just shipped (Session 6)

- **PR #9 (GitHub #16, `6313628`)** — state machine + lifecycle persistence + boot-time recovery. `lifecycles` + `lifecycle_events` schema; `StateMachine.transition()` with `ALLOWED_TRANSITIONS` invariant; `recover_state` repopulates from broker on startup. 68 tests landed.
- **PR #10 (GitHub #17, `6d87c3c`)** — MA50/MA100 bounce strategy (SeanBot port, LONG-only) + bracket placement (option γ, MKT parent + LMT TP child + separate GTC STP) + `OrderRouter.on_fill` + `EodForceClose` at 15:58 ET. 121 tests (cumulative).
- **PR #11 hotfix (GitHub #18, `d46268f`)** — one-line Dockerfile addition `COPY config ./config`. Root cause: PR #10 added `from config.*` imports but PR #10 brief listed `Dockerfile` in MUST-NOT-MODIFY scope. §0.5.137 born.
- **PR #11 reconciler (GitHub #19, `4964e47`)** — Flavor 2 reconciliation (30s dirty-drain + 5-min full-scan), 12-row conflict resolution matrix, foreign-position halt callback (in-memory `_halt_new_entries` flag, restart-to-clear). Renamed `_handle_signal(signal)` → `_handle_trade_signal(signal)` to fix the SIGTERM collision (§0.5.139). 153 tests (+32 new).

### What we discovered this session (not yet in code, captured here)

- **`_handle_signal` collision (§0.5.139)** — covered above. Code fix is in `4964e47`; the lesson lives in §0.5.139 and §5.
- **Dockerfile COPY gap for config/ (§0.5.137)** — covered above.
- **`gh repo view --json autoMergeAllowed` errors (§0.5.124)** — gh CLI's `--json` rejects fields outside its known schema. Use `gh api repos/<owner>/<repo>` for full repo metadata.
- **`.env` line 17 `bash source` incompatibility (§0.5.125)** — python-dotenv tolerates; bash source doesn't. Don't try to "fix"; never use `source` on this file.
- **System `python3` lacks `python-dotenv` (§0.5.127)** — every probe script must invoke `/home/tradeflow/tradeflow/.venv/bin/python3`.
- **IBC `AutoRestartTime` 12-hr format (§0.5.126)** — silent failure on 24-hr input; doesn't reschedule.
- **`InvariantViolation` → `InvariantViolationError` rename (§0.5.131)** — ruff N818. State machine API otherwise unchanged.

---

## 2. The session's bug thread

1. **Started** with PR #9 brief from Session 5 carried forward; Supabase paused on schema provisioning. Operator provisioned project + applied lifecycles migration via dashboard SQL editor while VPS CC implemented PR #9.
2. **PR #9 landed cleanly** at `6313628`. 68 tests passing. State machine + recovery wired into orchestrator.
3. **PR #10 brief drafted** with "exactly 11 files" then revised to 13, landed at 15 (pyproject.toml was already populated, didn't need modification; probed before assuming missing — §0.5.97 worked here). MA50/MA100 strategy + bracket option γ + EOD all in. Operator's laptop slept mid-PR-body Write tool prompt; recovery via paste-from-chat — §0.5.134 born. Merged at `6d87c3c`.
4. **PR #10 smoke FAILED at Step 5** — `ModuleNotFoundError: No module named 'config'` in container boot logs. Dockerfile only COPYed `src/` + `main.py`. **First wrong call**: PR #10 brief had declared `Dockerfile` MUST-NOT-MODIFY without grepping new imports vs Dockerfile coverage. §0.5.137 born.
5. **PR #11 hotfix (one-line `COPY config ./config`)** shipped as GitHub #18, merged at `d46268f`. PR #10 smoke retry full PASS on all 6 steps.
6. **PR #11 reconciler brief drafted** — Flavor 2 design from PR #10 sketch (dirty-set + 5-min full-scan hybrid), 12-row conflict matrix as the spec, foreign-position halt callback. Operator picked "ship as drafted" without revisions.
7. **VPS CC audit during PR #11 implementation caught the `_handle_signal` collision** — pre-existing bug from PR #10 that would have killed first signal. VPS CC's read-orchestrator.py-end-to-end discipline (per the brief's Task A) found it. **Second wrong call** of the session, retroactively: PR #10 brief should have grepped class methods for collisions before specifying `_handle_signal` as the trading handler name. §0.5.139 born.
8. **PR #11 (reconciler + collision fix) landed** at `4964e47`. 153 tests green. Smoke retry confirmed `[RECON] task_launched`, drain cadence, healthcheck cadence, IBKR state, renamed handler in image.
9. **Session wrap** — operator noted Monday is Memorial Day, asked for v5 + Session 7 kickoff + publishing brief.

The goal of this section: Session 7 reads it and understands which rabbit holes are closed so it doesn't walk them again. **Two rabbit holes that are closed**: (a) "Dockerfile is in scope-protected list so leave it alone" — no, grep imports first; (b) "the orchestrator's `_handle_signal` is the trading handler" — no, it's `_handle_trade_signal` post-PR-#11, and adding methods to existing classes requires a name-collision grep.

---

## 3. What the system is actually made of

**Single source of truth:** `git -C ~/tradeflow ls-tree -r HEAD --name-only` on `main` at `4964e47` or later. No standalone system map doc yet — this handoff is the best available map.

Highlights to save a lookup:

- **2 containers**: `tradeflow-app` (python:3.11-slim, runs `python main.py`, depends on ib-gateway, env-file from `~/.tradeflow-secrets/.env`) and `tradeflow-ib-gateway` (`ghcr.io/gnzsnz/ib-gateway:stable`, headless IBC-managed IB Gateway, host-mapped `127.0.0.1:4002` → container `4004`).
- **2 DB tables**: `lifecycles` (one row per trade lifecycle, stable `lifecycle_id` UUID across state transitions, no `UNIQUE(symbol, state)` constraint — Botty G105/G106 lesson) and `lifecycle_events` (append-only audit trail, `event_type` + `event_data` JSONB + `emitted_at`). Indexes: `lifecycles_symbol_state_idx`, `lifecycles_open_state_idx`, `lifecycles_strategy_idx`, `lifecycles_created_at_idx`, `lifecycle_events_lifecycle_emitted_idx`, `lifecycle_events_to_state_emitted_idx`.
- **Production-live code paths** (entry points):
  - Orchestrator: `main.py` → `src/orchestrator.py:Orchestrator.run()` → loop on `_stop_event`
  - Strategy: `_on_new_bar` (bar callback from IB) → `Sma100BounceStrategy.detect_signal` → `_handle_trade_signal` (PR #11 renamed) → `OrderRouter.place_entry`
  - Fill handling: IB `fillEvent` → `OrderRouter.on_fill` → routes to `_handle_parent_fill` or `_handle_exit_fill`
  - EOD: `EodForceClose.run_until_stopped` background task → fires at 15:58 ET → cancels working orders + market-closes any open position
  - Reconciler: `Reconciler.run_until_stopped` background task → drains `DirtySet` every 30s + full-scans `lifecycles` table every 5 min → applies conflict matrix
  - Recovery (boot-only): `Orchestrator._recover_state` → loads non-CLOSED lifecycles → `_broker_field_updates_for` → conditional transitions
- **Dead/phantom surfaces** (exist but not wired in):
  - Top-level stub packages `strategy/`, `execution/`, `risk/`, `features/`, `backtest/`, `comms/`, `data/`, `scripts/` — empty `__init__.py` only as of `4964e47` (§0.5.138).
  - `comms/` would be the telemetry/alert home if/when added (PR #13+).
- **Automation gotchas**:
  - Healthcheck loop is per-container (`[ORCH] healthcheck: ok`) — does NOT signal Docker healthcheck status (no `HEALTHCHECK` directive in Dockerfile). `docker ps` shows `Up <time>` without health column. If you want Docker health, wire `HEALTHCHECK` in a future PR.
  - SIGTERM works correctly post-PR-#11 (`signal.signal` + `loop.call_soon_threadsafe`, since `asyncio.add_signal_handler` doesn't work in python:3.11-slim). Clean exit 0 verified in smoke.
  - No crons. All scheduled work is asyncio tasks owned by the orchestrator.

- **0 open documented bugs** as of handoff. PR #11 closed the two production bugs from §2 (Dockerfile gap, `_handle_signal` collision). Operational debt is in §7.

---

## 4. Verified facts about TradeFlow (as of 2026-05-22)

**DO NOT challenge these unless probed against source.**

### IBKR + IB Gateway

- IB Gateway docker image: `ghcr.io/gnzsnz/ib-gateway:stable`, server version 178.
- Container port mapping: `127.0.0.1:4002` (host) → `4004` (container). The orchestrator connects via `IBKR_HOST=ib-gateway` `IBKR_PORT=4004` (docker network, not the host mapping).
- Paper account ID is `DUQ…` (prefix), not `DUO891961`. Operator accepted the account ID leak; no need to redact in code/logs at this stage.
- `ReadOnlyApi=no` confirmed at `/home/ibgateway/ibc/config.ini:396` post-PR-#11 smoke. Setting flipped by editing `~/.tradeflow-secrets/.env` line 19 `READ_ONLY_API=no` + `docker compose up -d --force-recreate ib-gateway`.
- `ib_async==1.0.0` library, active fork (not the deprecated `ib_insync`).
- `IBClient.get_portfolio() → list[PortfolioItem]` is the canonical position read (§0.5.T3). `get_open_trades() → list[Trade]` for working orders. `get_positions() → list[Position]` for symbol+qty matching.

### MNQ contract (verified, do not re-derive)

- TICK_SIZE = `0.25` index points
- MULTIPLIER = `$2/point` (`$0.50/tick`)
- COMMISSION_RT = `$0.62` per round-trip (friend's tier; verify in Tasks A audits if porting to operator's tier)
- MARGIN_REQ = `$2,000` day-trade (friend's broker tier)
- CME maintenance margin ~$3,636
- Quarterly contract cycle: Mar/Jun/Sep/Dec, expiry 3rd Friday, roll ~8 days before
- **Current front month**: MNQM6 (June 2026, expiry Friday 2026-06-19, roll target ~2026-06-11)

### Strategy

- Identifier: `strategy="sma100_bounce"` (sticky, see §0.5.133)
- Real condition: MA50 > MA100, ADX ≥ 20, low ≤ MA100 + `ma_touch_buffer_pts` (5pt), bullish candle close, NOT within `session_edge_no_trade_minutes` (5) of session open/close
- Entry: MKT at close of triggering bar
- Stop loss: `stop_loss_pts` (75 points below entry, separate GTC STP per §0.5.T2)
- Take profit: `take_profit_pts` (150 points above entry, child of MKT parent in bracket option γ)
- ADX min: `adx_min_threshold=20.0`, period 14
- LONG-only currently. SHORT enablement is a future PR.

### Supabase

- Project ref prefix: `vzlpxaif*`, region us-east-1
- Tables: `lifecycles` + `lifecycle_events` (see §3)
- RLS enabled on both; `service_role` key bypasses (orchestrator uses service_role; never use anon key from backend)
- Auto-expose ON, auto-RLS ON, Data API ON
- Custom client wrapper: `src/clients/supabase_client.py` (httpx-based, NOT supabase-py)

### State machine invariants (PR #9)

- Allowed transitions: IDLE→{ENTERING,CLOSED}, ENTERING→{ACTIVE,CLOSED}, ACTIVE→{EXITING}, EXITING→{CLOSED}, CLOSED→∅
- `ACTIVE → CLOSED` is **disallowed direct**; must walk via EXITING (PR #11 reconciler uses exit_order_id=0 sentinel for reconciliation-detected missing-known-order case)
- CLOSED row requires: `entry_*`, `exit_*`, `exit_reason`, `commission_total`, `pnl_gross`, `pnl_net` all non-null
- Exception class: `InvariantViolationError` (renamed from `InvariantViolation` in PR #10 per ruff N818, §0.5.131)
- Lifecycle ID is stable UUID across all transitions — no row deletion, no UNIQUE(symbol, state) constraint (Botty G105/G106 lesson)

### Repo state at handoff

- HEAD: `4964e47` on `main` (PR #11 reconciler squash-merge)
- Default branch: still `claude/phase-0-repo-bootstrap-ZZGJX` (cosmetic; `gh repo edit` done for main as protected branch, but the displayed-default in the UI hasn't been corrected). Doesn't affect operations.
- Branch protection: active on `main`, requires PR + CI green to merge. No direct pushes.
- gh auth token: `gho_*` OAuth (PAT was upgraded via `gh auth refresh -s` per §0.5.128), scopes `gist,read:org,repo,workflow`.
- Commit author discipline: clean operator authorship, no `Co-Authored-By` trailers (carry from Botty handoff convention).

---

## 5. Wrong diagnoses (this session) — READ BEFORE YOU DEBUG

### Wrong call 1: PR #10 brief declared Dockerfile MUST-NOT-MODIFY without grepping imports

- **What the brief did**: listed `Dockerfile` in the protected-paths block, with the verification gate `git diff main -- ... Dockerfile` MUST be empty.
- **What was missed**: the PR added `from config.instruments import MNQ` and `from config.risk_params import risk_params` to three modules (`orchestrator.py`, `strategy.py`, `router.py`). The Dockerfile only COPYed `src/` + `main.py`. Container image was missing `config/`.
- **Why local tests didn't catch it**: pytest runs with PYTHONPATH including the repo root, so `from config.*` resolves against on-disk `config/`. The Docker image is what's broken.
- **How it surfaced**: PR #10 smoke Step 5 failed with `ModuleNotFoundError: No module named 'config'` in `docker logs tradeflow-app`. Container in restart loop.
- **Correct diagnosis**: Dockerfile coverage gap. Fix is one line.
- **Lesson**: §0.5.137. The brief-design step must grep new imports against the Dockerfile's `COPY` set BEFORE declaring `Dockerfile` (or `pyproject.toml`, or `docker-compose.yml`) as MUST-NOT-MODIFY.

### Wrong call 2: PR #10 brief specified `_handle_signal` without grepping the class for name collisions

- **What the brief did**: instructed the implementer to add `async def _handle_signal(self, signal: Signal)` as the trading-signal handler on `Orchestrator`.
- **What was missed**: `Orchestrator` already had a SIGTERM handler `def _handle_signal(self, signum, frame)` from the daemon scaffold. Python class namespace allows only one binding per name; the second definition silently replaced the first.
- **Effect**: `_on_new_bar` thread-safe-scheduled `self._handle_signal(signal_or_none)` onto the loop, which invoked the SIGTERM handler with a `Signal` dataclass as `signum`. The SIGTERM handler called `loop.call_soon_threadsafe(self._stop_event.set)`, exiting the run loop. **The bot would have gracefully shut down on every signal Tuesday.**
- **Why tests didn't catch it**: PR #10 tests mocked `OrderRouter.place_entry` at the boundary and never exercised the `_on_new_bar → _handle_signal → place_entry` path in a way that distinguished which `_handle_signal` was called. The test stack passed because the SIGTERM call also doesn't raise — it just sets an event.
- **Why smoke didn't catch it**: smoke verifies plumbing (containers up, schemas reachable, log lines emitted at boot), NOT runtime signal-handling. Weekend timing meant no real signals fired.
- **How it surfaced**: VPS CC during PR #11 implementation read `orchestrator.py` end-to-end as required by the brief's Task A audit, saw two `_handle_signal` definitions, recognized the shadow.
- **Correct diagnosis**: name collision. Fix: rename trading handler to `_handle_trade_signal`. PR #11 included the rename.
- **Lesson**: §0.5.139. Before specifying a new method on an existing class in a PR brief, **grep the class for that method name first**. This includes `__init__`, `_on_*`, `_handle_*`, anything generic.

### Meta-lesson for Session 7

Both wrong calls were briefing-stage failures, not implementation failures. The implementer (VPS CC) followed the brief correctly each time. The fix is **upstream brief-design lints**:

1. Grep new imports against the Dockerfile's `COPY` set before listing Dockerfile as protected.
2. Grep class methods for name collisions before specifying a method name to add.
3. Always include an audit task (Task A in the master template) that reads target files end-to-end before specifying wire-ups. PR #11's audit caught the collision precisely because the operator's brief required it.

Both lints should be incorporated into the `code-pr-brief` skill or a new `pr-brief-lint` skill (a Session 7 process-improvement task).

---

## 6. Verification block — run this before doing anything

### V0 — Pre-flight on VPS

```bash
ssh tradeflow@5.78.212.37
cd ~/tradeflow
git fetch
git pull --ff-only origin main
git log -1 --oneline
```

Expect: HEAD at `4964e47` or later. If not, do not proceed — re-sync.

### V1 — Containers up and recent

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

Expect: `tradeflow-app` and `tradeflow-ib-gateway` both `Up <time>`. If `tradeflow-app` shows `Restarting (1) <time>` ago → boot failure, check `docker logs tradeflow-app --tail 80` before touching anything.

### V2 — Code truth (PR #11 reconciler + collision fix actually in the running image)

```bash
docker exec tradeflow-app grep -n '_handle_trade_signal' /app/src/orchestrator.py
docker exec tradeflow-app grep -n 'class Reconciler' /app/src/execution/reconciler.py
docker exec tradeflow-app grep -nE 'def _handle_signal\b' /app/src/orchestrator.py
```

Expect:
- At least one `_handle_trade_signal` match
- `class Reconciler:` present
- Only the SIGTERM signature under `def _handle_signal` (signum/frame), NO `def _handle_signal(self, signal: Signal)` shadow

If `_handle_signal(signal: Signal)` reappears → rollback or rebuild required; the SIGTERM bug is back.

### V3 — IBKR paper state (source of truth)

```bash
/home/tradeflow/tradeflow/.venv/bin/python3 /tmp/probe_smoke_step1.py
```

(That probe script lives at `/tmp/probe_smoke_step1.py` from Session 6; if missing, recreate from §13 of this handoff or from `docs/handoffs/HANDOFF_v5.md` once published.)

Expect: `serverVersion= 178`, `positions= []`, `openTrades= []`. Non-empty positions → a fill happened or there's a foreign position; **investigate before any action**. Look for `[RECON] foreign_position` lines in the orchestrator log.

### V4 — Supabase lifecycles state

```bash
/home/tradeflow/tradeflow/.venv/bin/python3 /tmp/probe_smoke_step3.py
```

Expect: `lifecycles status= 200`, `lifecycle_events status= 200`. Bodies may be `[]` (no trades yet) or contain rows (first trade landed Tuesday or later).

### V5 — Reconciler heartbeat (last 90s)

```bash
docker logs tradeflow-app --since 90s | grep -E '\[RECON\]|\[ORCH\] healthcheck'
```

Expect:
- 1-2 `[ORCH] healthcheck: ok` lines (60s cadence)
- 2-3 `[RECON] tick: drain_complete — dirty_count=0 actions={}` lines (30s cadence)
- 0 or 1 `[RECON] tick: full_scan_complete` lines (5-min cadence; rare in 90s window)

Zero RECON lines → reconciler task crashed silently. Check `docker logs tradeflow-app --tail 200 | grep -E 'Traceback|RECON|reconciler'`.

---

## 7. Pending work queue

Priority order depends on V1-V5 state, not this ordering.

### PR #12 — halt-ack mechanism (sketched in PR #11 body)

**Status**: planned, design-fork, not yet briefed.

**Scope**: replace the in-memory `_halt_new_entries = True` restart-to-clear with a real ack mechanism so the operator can clear a foreign-position halt without restarting the bot.

**Design options** (from PR #11 PR body, recommend Supabase as primary):

- Env var (`TRADEFLOW_HALT_ACK=lifecycle_id`) — restart-required, no audit, brittle.
- File flag (`/var/lib/tradeflow/halt_clear`) — restart-survives via volume mount, audit via mtime, race risk on read/clear.
- Telegram command (`/clear-halt`) — best UX, full chat audit, requires telegram-bot loop (not yet wired), widens auth.
- **Supabase row write (`halt_acks` table)** — natural audit trail, restart-survives, ack-multiple-events natural, needs schema migration + per-tick reconciler query.

**Estimated size**: 4-6 files (migration + `halt_acks` table, reconciler poll method, orchestrator wire). 200-400 lines of new code, ~15 new tests.

**Blockers**: none. Can start immediately.

### Process-improvement PR (no code, just `.claude/skills/`)

**Status**: planned, low-effort, high-leverage.

**Scope**: add a `.claude/skills/pr-brief-lint/SKILL.md` that encodes the two new lints from this session (§0.5.137 imports-vs-Dockerfile, §0.5.139 class-method-name-collision). Brief-design checklist that VPS CC reads before drafting any PR brief.

**Estimated size**: 1 file, ~80 lines of markdown.

**Blockers**: none.

### Tuesday morning: first paper-trade observation

**Status**: passive, watch-and-learn.

**Scope**: observe the first RTH session Tuesday 2026-05-26 starting 09:30 ET (signal eligible from 09:35 ET). Track:
- Did MA conditions trigger any signal?
- If yes, did `_handle_trade_signal` log fire? Did `OrderRouter.place_entry` complete? Did the bracket land?
- If a position opened, did the parent fillEvent → STP placement work?
- Did EOD at 15:58 ET force-close cleanly?
- Did reconciler drain handle the post-trade flow correctly?

**Verification commands** (Session 7 if Tuesday is the next session):

```bash
docker logs tradeflow-app --since 6h | grep -E '\[STRAT\]|\[EXEC\]|\[RECON\]|\[EOD\]|\[ORCH\] signal'
```

Followed by a Supabase `lifecycles` SELECT to confirm the row landed end-to-end.

### Future enablement work (post-first-trade success)

- SHORT-side strategy enablement (currently LONG-only per §0.5.130)
- Telemetry / telegram alerts (operator gets a ping on entry/exit/EOD/halt — currently must grep logs)
- Daily/weekly kill-switch PRs (mentioned in Session 6 kickoff but deferred)
- Observability dashboard (Grafana? Supabase Studio views?)

### Operational debt

- Repo default branch displayed as `claude/phase-0-repo-bootstrap-ZZGJX` (cosmetic only; `gh repo edit --default-branch main` works in practice). UI label is misleading.
- Repo auto-merge enabled (was a setting earlier in Session 6).
- Top-level stub packages strategy/, execution/, risk/, features/, backtest/, comms/, data/, scripts/ are empty (§0.5.138).
- DUQ account ID leak accepted; future PR could redact via env var.
- `.env` line 17 `bash source` incompatibility (§0.5.125) — leave as is, python-dotenv handles it.

### Bugs by ID

None open as of handoff. Session 6 closed the two production bugs (Dockerfile config/ gap; `_handle_signal` collision).

---

## 8. Test safety — why we belabor this

Carry forward the cumulative list of test-mocking failures prior sessions hit:

1. **Tests passed against a fictional schema** because they mocked column names rather than verifying against the live DDL. Always probe Supabase migration files OR `information_schema.columns` before writing tests that assert column existence.
2. **`side_effect` list with wrong count** → silent `StopIteration` → wrong assertions. Always count `side_effect` calls against actual mock invocations in the code path.
3. **Mocked at raw library chain** (e.g. `_ib._client.placeOrder`) when code uses a wrapper (`IBClient.place_order`) → tests green, prod broken. Always mock at the wrapper boundary.
4. **Shared `MagicMock()` state leaked between tests** — use `unittest.mock.AsyncMock(spec=...)` with explicit per-test instantiation.
5. **Async decorator pattern assumption** — verify a neighbor test in the same file before assuming `@pytest.mark.asyncio` is needed. Some pytest configurations use `asyncio_mode=auto`; others require the decorator.

**New from Session 6** (added to this list):

6. **Tests didn't catch the `_handle_signal` collision** because they mocked `OrderRouter.place_entry` and never exercised `_on_new_bar → _handle_signal → place_entry` with a path that distinguished which `_handle_signal` was called. The SIGTERM-handler call doesn't raise — it just sets an event the test doesn't check. **Lesson**: integration tests at the orchestrator level (not unit tests at the router/strategy boundary) catch shadow-binding bugs. Consider adding `tests/test_orchestrator_integration.py` that exercises the bar-event flow with a mocked IB but a REAL `_handle_trade_signal` path.

Guardrails in the master template (§12 of this handoff, formalized in the `code-pr-brief` skill) prevent these. Do not ship tests that skip them.

---

## 9. Pitfalls from prior sessions (LLM trust-but-verify)

Things the LLM (any chat or VPS CC) got wrong before and should not be trusted on without verification:

- **"`pyproject.toml` is missing pandas/numpy"** — wrong, PR #10 probed and they were already there. Don't assume scope. (§0.5.97)
- **"Dockerfile is in MUST-NOT-MODIFY scope so it can't be the issue"** — wrong in PR #10. Dockerfile coverage gap was the root cause of smoke FAIL. Re-verify protected-list claims against actual diff.
- **"`_handle_signal` is the trading handler"** — wrong from PR #10 → PR #11 merge. Class had two `_handle_signal` methods; Python kept the second. Always grep for collisions.
- **"Tests passing means the deployed image is correct"** — wrong, tests run with repo-root PYTHONPATH, Docker image is what runs in prod. Smoke is the source of truth post-merge.
- **"Reconciler will fix any state drift"** — partially true. Reconciler covers the conflict matrix from PR #11 PR body, but `EXITING + position + no working order` is logged as warning, NOT auto-fixed. EOD or manual cleanup required.
- **"Memorial Day is the last Friday in May"** — NO, it's the last MONDAY in May. (2026: Monday May 25.) Friday May 22 (handoff date) is a normal trading day. Tuesday May 26 is the next post-Memorial-Day RTH.

**Next session rule**: if a claim is quantitative or date-dependent, re-verify it. Especially calendar facts, row counts, and "is X in scope" claims.

---

## 10. Session discipline lesson (Session 6)

**The two bugs of Session 6 were both upstream brief-design failures** — the implementer (VPS CC) followed each brief correctly. The fix is process, not policy.

### Enforcement rules for Session 7

1. **Before writing a PR brief, grep new imports vs Dockerfile COPY.** If any new top-level package is imported and not in COPY, either add to the brief's scope OR explicitly document the Dockerfile gap and address it.
2. **Before specifying a method name on an existing class in a PR brief, grep the class for that name.** If a binding already exists, choose a different name OR explicitly note the rename as part of the PR scope.
3. **Every brief's Task A (audit) is non-negotiable.** Even velocity mode keeps Task A. PR #11's audit caught the collision precisely because the brief required reading orchestrator.py end-to-end.
4. **Smoke is plumbing-only by design.** Don't expect smoke to catch runtime signal-handling bugs. Code-path bugs need integration tests OR real signal events to surface. Tuesday is the integration test.
5. **Calendar discipline.** When pinning "next RTH" or "next opportunity", verify against US market holidays. Don't say Monday when Monday is Memorial Day.

---

## 11. Logging verbosity — what to demand from any new code

Standing principles, observed in PR #9/#10/#11:

- Every state transition logs `[COMPONENT] symbol: action — reason` at INFO. Component tags: `[ORCH]`, `[STRAT]`, `[EXEC]`, `[EOD]`, `[RECON]`, `[ib_client]`.
- Every swallowed exception logs the specific error + context. No bare `except Exception: pass`. Use `LOGGER.exception` or `LOGGER.warning` with `exc_info=True`.
- Async background tasks log `task_launched` on start and `task_exited` on clean exit. Cancellation logged at INFO.
- Healthcheck loop emits `[ORCH] healthcheck: ok` every 60s as the liveness signal.
- Reconciler emits `tick: drain_complete` + `tick: full_scan_complete` periodically; per-lifecycle action logs include the action enum value.
- Foreign-position detection logs `[RECON] foreign_position: symbol=X qty=Y — halting new entries` — at WARNING, not INFO, so it stands out.
- Order placement logs every step: `entry_placed`, `bracket_placed`, `parent_filled`, `stop_placed`, `exit_filled`, `trade_closed` with pnl.

**Demand this verbosity in every new PR**. If a PR's diff has new `if/elif/else` branches with no logging, push back at brief-design time.

---

## 12. Master template — use for every Claude Code PR

See `.claude/skills/code-pr-brief/SKILL.md` in repo for the full master template. It enforces: patch constraints (EXACTLY N files), protected-paths block with verification gates, Task A audit, Task D grep classification, Task E out-of-scope investigation, test safety guardrails, known gotchas, and "what I got wrong" section.

**New requirements for Session 7+ briefs** (from §10):

- Add a "brief-design lint" pre-check before issuing: imports vs Dockerfile coverage; class-method name collisions.
- Carry forward all §0.5 entries verbatim in every brief's Known Gotchas section.

---

## 13. Current PR brief in flight — none

No PR brief in flight at handoff. PR #11 merged; smoke retry confirmed. Next planned PR is #12 (halt-ack), but no brief written yet — Session 7 can draft fresh based on the design space sketched in PR #11's PR body.

Useful artifacts on the VPS at session close (may persist or may need recreation):

- `/tmp/probe_smoke_step1.py` — IBKR paper state probe
- `/tmp/probe_smoke_step3.py` — Supabase schema reachability probe
- `/tmp/wait_boot.py` — poll-for-healthcheck-or-traceback (60s budget)
- `/tmp/wait_recon_drain.py` — poll-for-[RECON]-drain-complete (65s budget)
- `/tmp/healthcheck_cadence.py` — sleep+count for the smoke step-6 cadence check

These are ephemeral; recreate from the smoke runbooks in §15 if missing.

---

## 14. Canonical references (in order of authority)

1. **`src/` on `main` at `4964e47`** — verified system reality. The actual code that runs.
2. **`docs/handoffs/HANDOFF_v5.md`** — this doc, once published per §16.
3. **`docs/handoffs/HANDOFF_v4.md`** — historical context, §0.5.97–.123 + §0.5.T1–T5 verbatim source.
4. **Supabase production DB** — `lifecycles` + `lifecycle_events` tables, queried via service_role.
5. **IBKR via `ib_async`** with env vars from `~/.tradeflow-secrets/.env` — truth for position/order/account state.
6. **`docker logs tradeflow-app`** — runtime narrative, last 24h typically.
7. **This handoff (v5) §1–6** — session context, NOT long-term authority. Re-verify against 1 if disagreement.

---

## 15. First 15 minutes of Session 7

If Session 7 is **before Tuesday** (Saturday/Sunday/Monday — process-improvement time):

1. Read §0.5, §1, §2, §5, §10 of this handoff. §5 (wrong diagnoses) and §10 (session discipline) are the highest-leverage reads.
2. SSH in. Run §6 V0–V5 block. Confirm all expectations met. Total ~3 minutes.
3. Pick one of:
   - **Process-improvement PR**: draft the `.claude/skills/pr-brief-lint/SKILL.md` (encodes §0.5.137 + §0.5.139 lints) and commit. ~1 hour.
   - **PR #12 design + brief**: draft the halt-ack PR brief based on the design space in PR #11 PR body (recommend Supabase as primary). ~30 min for design picker + 30 min for brief. VPS CC implementation later.
4. If both done with time to spare: review the Tuesday observation plan (§7) and pre-write the verification block Session 7 will use Tuesday morning.

If Session 7 is **Tuesday or later** (first-trade-observation time):

1. Read §0.5, §1, §6, §7 of this handoff. §7's Tuesday observation block is the focus.
2. SSH in. Run §6 V0–V5. Confirm green BEFORE 09:30 ET.
3. At 09:30 ET: market opens. Watch logs:
   ```bash
   docker logs tradeflow-app --since 5m -f | grep -E '\[STRAT\]|\[EXEC\]|\[ORCH\] signal|\[RECON\]'
   ```
4. If a signal fires:
   - Confirm `[ORCH] signal: received` then `[EXEC] entry_placed` then `[EXEC] bracket_placed`
   - Watch for parent fillEvent → `[EXEC] parent_filled` then `[EXEC] stop_placed`
   - Reconciler drain should pick up the lifecycle within 30s post-fill
5. At 15:58 ET: EOD fires. Watch:
   ```bash
   docker logs tradeflow-app --since 10m | grep -E '\[EOD\]|\[EXEC\]'
   ```
6. Post-close: query Supabase `lifecycles` for the day's rows; verify CLOSED state with correct `exit_reason` and non-zero pnl_*.
7. Write Session 7 handoff (v6) capturing the first paper trade narrative.

---

## 16. How to publish this handoff

**Path A — VPS Claude Code brief (preferred):**

Paste the following brief to VPS Claude Code on the TradeFlow VPS. The handoff content (this entire document) goes at the marked placeholder. VPS CC saves to disk, commits, pushes.

~~
You are VPS Claude Code on the TradeFlow VPS (`tradeflow@5.78.212.37`, repo at `~/tradeflow`). Save the following content VERBATIM to `/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v5.md`, then publish:

# Pre-flight
git -C ~/tradeflow fetch
git -C ~/tradeflow pull --ff-only origin main
git -C ~/tradeflow log -1 --oneline

Expect HEAD at `4964e47` or later.

# Branch
git -C ~/tradeflow checkout -b docs/handoff-v5

# Save content
Write the content below verbatim to `/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v5.md` via the Write tool. Do not paraphrase, do not summarize, do not omit sections. The file is the deliverable.

# Verify
ls -la /home/tradeflow/tradeflow/docs/handoffs/
wc -l /home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v5.md

Expect HANDOFF_v5.md present with line count >400.

# Stage + commit + push
git -C ~/tradeflow add docs/handoffs/HANDOFF_v5.md
git -C ~/tradeflow status

Expect: docs/handoffs/HANDOFF_v5.md as the only staged change.

Write to /tmp/handoff_v5_commit.txt via the Write tool:

```
docs: add v5 handoff (Session 6 — bot live in paper, PR #11 deployed)

Session 6 shipped PRs #9/#10/#18/#19. Bot is live on PR #11 code (4964e47),
paper account, $1M NetLiq, zero positions. Two production bugs caught:
Dockerfile config/ COPY gap (§0.5.137) and _handle_signal class-namespace
collision (§0.5.139). First RTH session is Tuesday 2026-05-26.

Covers: state machine + recovery, MA50/MA100 bounce + bracket + EOD,
Flavor 2 reconciliation cadence + foreign-position halt callback.

153 tests cumulative. Repo at d46268f → 4964e47 over Session 6.
```

git -C ~/tradeflow commit -F /tmp/handoff_v5_commit.txt
git -C ~/tradeflow push --set-upstream origin docs/handoff-v5

# PR + merge
Write to /tmp/handoff_v5_pr.md via the Write tool:

```
## Summary
Session 6 handoff. Bot is live in paper on PR #11 code; first RTH session is Tuesday 2026-05-26.

See docs/handoffs/HANDOFF_v5.md for the full 16-section doc.
```

gh pr create --base main --title "docs: add v5 handoff (Session 6 — bot live in paper, PR #11 deployed)" --body-file /tmp/handoff_v5_pr.md

Capture PR number.

gh pr checks <N> --watch
gh pr merge <N> --squash --delete-branch

# Post-merge confirm
git -C ~/tradeflow checkout main
git -C ~/tradeflow pull --ff-only origin main
git -C ~/tradeflow log -1 --oneline
ls -la /home/tradeflow/tradeflow/docs/handoffs/

Expect HEAD shows the v5 handoff squash commit and HANDOFF_v5.md is present in docs/handoffs/ on main.

# Report back
- PR number + URL
- Squash commit hash
- File path + line count
- `git status` clean
- HEAD oneline

<paste handoff content from chat verbatim here, starting with "# TradeFlow — Handoff v5 (bot live in paper, PR #11 deployed, first RTH Tuesday 2026-05-26)">
~~

**Path B — Manual fallback (if VPS CC unavailable):**

```bash
scp HANDOFF_v5.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v5.md
ssh tradeflow@5.78.212.37 "cd ~/tradeflow && git checkout -b docs/handoff-v5 && git add docs/handoffs/HANDOFF_v5.md && git commit -m 'docs: add v5 handoff (Session 6 — bot live in paper, PR #11 deployed)' && git push --set-upstream origin docs/handoff-v5 && gh pr create --base main --title 'docs: add v5 handoff' --body 'Session 6 handoff. See docs/handoffs/HANDOFF_v5.md.' && gh pr merge --squash --delete-branch --auto"
```

The handoff exists only if saved to disk AND committed AND merged to main. Until then, treat the chat output as draft.

---

*End of handoff v5. Target lifespan: until the first paper trade lands and Session 7 captures Tuesday's narrative in v6. Then v5 becomes historical, ranked below v6 + live code in §14.*
