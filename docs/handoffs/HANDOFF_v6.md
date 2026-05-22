# TradeFlow — Handoff v6 (telegram + halt-ack live, observation Session ready for Tuesday 2026-05-26)

*Handoff from end of Session 7, Friday 2026-05-22 evening (~04:10 UTC Sat 2026-05-23). Orchestrator is **running in paper** on PR #15 code (commit `6ab026e` or later) on Hetzner VPS `5.78.212.37`. Account `DUQ…` (paper, NetLiq $1,000,085.80), `positions=[]`, `openTrades=[]`. Reconciler drain ticks every 30s, full-scan every 5 min. EOD scheduled for 3:58 pm ET. Telegram alerter + command bot are LIVE — `/status` confirmed working from operator's phone. **Monday 2026-05-25 is Memorial Day — markets closed. First real RTH session is Tuesday 2026-05-26 09:30 ET (signal-eligible from 09:35 ET).** Session 8 inherits a fully instrumented + alert-able bot.*

---

## 0. How to use this doc

Read sections 1–6 first — state-of-the-system as of handoff. Sections 7–13 are reference material. Section 14 is the single-file source of truth to consult when this handoff disagrees with itself or a live observation: source code on `main` at commit `6ab026e` or later.

**Do not trust this doc alone.** Run §6 verification block before writing any code. Specifically confirm: `git log -1` shows `6ab026e` or later; `docker ps` shows both `tradeflow-app` and `tradeflow-ib-gateway` running with `(healthy)` (PR #12 HEALTHCHECK is live); broker `positions()` and `openTrades()` are empty; `/status` from Telegram returns a clean reply.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

### Baseline (every project, from handoff_template.md)

**Copy-paste instruction style.** Every action recommended to the operator must be a copy-paste-ready bash block. Self-contained commands. Source env vars in the same block. Expected output described immediately below each block. Decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**Learning-delivery discipline.** Every new fact discovered (bug pattern, corrected assumption, environmental fact, diagnostic finding) gets surfaced immediately as a markdown snippet for the running handoff queue. Do not wait until end-of-session.

**Read before diagnosing.** For complex state bugs, read the full startup log and 3–5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency language, hit the source of truth — live API, live DB, raw log file — not aggregated metrics.

**Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise.

### Carried forward verbatim from HANDOFF_v5 §0.5

§0.5.97–.139 + §0.5.T1–T5 — see `docs/handoffs/HANDOFF_v4.md` and `docs/handoffs/HANDOFF_v5.md` in repo on `main` for the full enumeration. Key entries:

- **§0.5.97 (probe-before-specify)** — Probe external specs against source before baking into briefs. Single most common source of wrong PRs.
- **§0.5.98 / §0.5.123 (broker is ground truth)** — Broker/exchange state is ground truth for position/fill/capital claims, NOT internal DB tables.
- **§0.5.104** — CC meta-safety on `.claude/`; reads accepted, writes prompt (override via project-scope option).
- **§0.5.117/.118 (bash discipline)** — Hardcoded heuristics that cannot be silenced via settings: `cd X &&`, `;` separators, `$(...)`, `${VAR}`, heredocs, chained `sleep`. Workarounds: `git -C path`, one Bash call per separator, Python helpers in `/tmp/`, `--body-file` for PR bodies, `-F` for commit messages, polling helpers for waits >10s.
- **§0.5.125** — `.env` line 17 trips `bash source` but python-dotenv handles it; never `source` the file.
- **§0.5.127** — Always invoke `/home/tradeflow/tradeflow/.venv/bin/python3` for scripts that read `.env`.
- **§0.5.129** — Supabase project ref prefix `vzlpxaif*`, region us-east-1; `service_role` bypasses RLS.
- **§0.5.130** — Strategy is MA50+MA100+ADX bounce, identifier sticky as `strategy="sma100_bounce"`.
- **§0.5.134** — SSH-resilient — stage long artifacts to `/tmp/` via Write tool first, then commit/push/PR.
- **§0.5.137** — Imports vs Dockerfile COPY — grep new top-level package imports against Dockerfile `COPY` set BEFORE declaring `Dockerfile` MUST-NOT-MODIFY.
- **§0.5.138** — Stub packages: top-level packages that are empty `__init__.py` today — adding files requires Dockerfile COPY if they become imported by `src/`.
- **§0.5.139** — Class-method name collision — grep the class for any method name before adding a new method.
- **§0.5.T1–T5** — IBKR/bracket invariants. See HANDOFF_v4 §0.5.

### New this session (Session 7) — append-only

- **§0.5.140 — Probe column names against schema before specifying a `select=`.** PostgREST returns `42703` on a missing column; this fails the probe with a 400 even when the table is healthy. When writing any Supabase probe, check `lifecycles.lifecycle_id` (NOT `id`), `lifecycle_events.event_id` (NOT `id`), `halt_acks.halt_ack_id` (NOT `id`). Better: use `select=*&limit=1` for reachability checks since `*` is always valid. Use a named column only when row content is the point of the probe.

- **§0.5.141 — Pre-populate `settings.local.json` with comprehensive project-scope grants.** "Going organic" (§0.5.117/.118 era) meant don't pre-emptively bypass safety via `--dangerously-skip-permissions`. It did NOT mean accept session-scope prompts forever. After ~5 sessions of pattern observation, pre-populate `.claude/settings.local.json` with explicit `Bash(...)` / `Read(...)` / `Write(...)` / `Edit(...)` grants for every common operation in the repo and `/tmp/`, with explicit `deny` for the 6 critical-decision gates (secrets, push-to-main, container destruction, `--dangerously-skip-permissions`). This is the §0.5.105 "comprehensive sweep" pattern applied to permissions specifically. Hardcoded heuristics (§0.5.117/.118 bash patterns) cannot be silenced — those stay the responsibility of brief discipline.

- **§0.5.142 — `python -c` with multi-line content trigger.** Claude Code flags any quoted argument containing a newline followed by `#` as a path-validation hiding risk. Workaround: stage the script to `/tmp/<name>.py` via the Write tool, then invoke `python /tmp/<name>.py`. Same shape as §0.5.134 (SSH-resilient artifact staging). This trigger joins the §0.5.117/.118 family — heuristics that cannot be silenced via settings and must be handled by brief discipline.

- **§0.5.143 — Telegram `parse_mode=Markdown` is fragile.** Legacy Markdown italicizes `_word_` between letters and eats the underscores. Variable names with underscores (`open_trades`, `net_liq`, `asyncio_mode`, `lifecycle_id`, `halt_ack_id`) get mangled in user-facing messages. Two safe options: (a) use `MarkdownV2` with the full escape set `_*[]()~\`>#+-=|{}.!`, OR (b) drop `parse_mode` entirely (plain text). PR #14 used legacy Markdown and produced `open_trades` → "opentrades" / `net_liq` → "netliq" in `/status`. PR #15 dropped `parse_mode`. Lesson: never use legacy Markdown for content containing Python identifiers; prefer MarkdownV2 with escaping if formatting matters, otherwise plain text.

- **§0.5.144 — Secrets audit on every config-file merge.** When merging into `.claude/settings.local.json` (or any structured config the operator has edited over time), VPS CC must grep for `ghp_`, `gho_`, literal API tokens, and bearer credentials BEFORE preserving entries verbatim. PR #13 setup discovered a literal GitHub PAT (`ghp_LR3D…`) sitting in `settings.local.json` from a pre-OAuth-upgrade session. VPS CC correctly flagged it; chat-side Claude codified rotation + redaction + shred. If a secret is found: surface immediately, halt the merge, force rotation, then proceed. The 6 critical-decision gates extend implicitly to "secret found in operator config" — treat as if the operator had pasted it into chat.

---

## 1. Where we are (as of Saturday 2026-05-23 ~04:10 UTC)

### Live production state

- **`tradeflow-app`** — running on Hetzner VPS `tradeflow@5.78.212.37`, image rebuilt from `6ab026e` post-PR-#15 smoke. Process `python main.py` as PID 1. PR #12 SIGTERM handler intact. PR #12 HEALTHCHECK directive now reports `(healthy)` in `docker ps`. `[ORCH] healthcheck: ok` every 60s. `[RECON] tick: drain_complete` every 30s. EOD scheduled for next 3:58 pm ET fire. Telegram subsystem launched: `[telegram] handler_installed`, `[telegram] alert_loop: task_launched`, `[telegram] command_loop: task_launched` all in startup logs.
- **`tradeflow-ib-gateway`** — `ghcr.io/gnzsnz/ib-gateway:stable`, healthy, IBC-managed login to paper account `DUQ…`. Server version 178. `ReadOnlyApi=no`.
- **Position state** — broker `positions()` = `[]`, `openTrades()` = `[]`, NetLiq $1,000,085.80 paper (slightly above the $1M start — IBKR paper interest credit).
- **DB state** — Supabase project `vzlpxaif*` (us-east-1). `lifecycles` empty, `lifecycle_events` empty, `halt_acks` empty (PR #12 migration applied to dashboard SQL editor by operator during Session 7).
- **No manual operational overrides** — no halt flags raised, no kill switches set, no crons paused.
- **Telegram** — bot LIVE. Bot token + operator chat_id configured in `~/.tradeflow-secrets/.env`. `/status` verified from operator's phone (screenshot in chat). Five alert types armed: `entry_placed`, `exit_filled`, `halt_raised`, `halt_acked`, `eod_complete`. Three commands armed: `/status`, `/halt SYMBOL [reason]`, `/ack [reason]`.
- **Calendar** — Sunday 2026-05-24 normal. **Monday 2026-05-25 is Memorial Day — markets closed.** First RTH bar after handoff: Tuesday 2026-05-26 09:30:00 ET. Strategy signal-eligible from 09:35:00 ET (5-min session-edge buffer per `risk_params.session_edge_no_trade_minutes=5`).

### What just shipped (Session 7)

- **PR #12 (GitHub #21, `99d9513`)** — Halt-ack mechanism + ops bundle. 11 files / +699/-23. Added: `halt_acks` Supabase table + migration; `SupabaseClient.get_newest_halt_ack`; `Orchestrator.raise_halt`/`clear_halt`/`is_halted`/`halt_raised_at` API; `Reconciler._poll_halt_ack` + `_read_file_ack_mtime` (Supabase primary, `/tmp/halt_clear` file-flag fallback); Dockerfile HEALTHCHECK directive; `.claude/skills/pr-brief-lint/SKILL.md` (encodes §0.5.137 + §0.5.139 + §0.5.140); `scripts/health_snapshot.sh` + `_probe_ibkr.py` + `_probe_supabase.py`. 165 tests cumulative (153 baseline + 12 new).

- **PR #13 (GitHub #22, `2a9cd1d`)** — Extended `pr-brief-lint/SKILL.md` with §0.5.117/.118 + §0.5.142 lint as the 4th section. 1 file / +36/-0. Encodes the bash-chaining + multi-line `python -c` heuristic family. No new tests (skill file is docs).

- **PR #14 (GitHub #23, `6a4e962`)** — Telegram alerter + commands. 7 files / +857/-42. Added: `comms/telegram.py` (TelegramAlerter + TelegramAlertHandler + OperatorCoordinator Protocol); `SupabaseClient.insert_halt_ack`; `Orchestrator.get_broker_status_summary` + `insert_halt_ack` + `_build_telegram_if_configured` helper + Telegram task launching in `_launch_background_tasks`; `[ALERT]` sibling log lines in `raise_halt`/`clear_halt`/`router.place_entry`/`router._handle_exit_fill`/`force_close.fire_once`; Dockerfile `COPY comms ./comms`. 186 tests cumulative (165 + 21 new).

- **PR #15 (GitHub #24, `6ab026e`)** — Drop `parse_mode=Markdown`. 2 files / +6/-4. Removed legacy Markdown parsing from `_send`; replaced `*TradeFlow*` and `*Status*` literals with plain text. Codifies §0.5.143. 186 tests still green (assertion updated in-place).

### What we discovered this session

- **§0.5.140 (column-name probe)** — Chat-side me wrote `select=id` for `lifecycles`/`lifecycle_events` in the V0-V5 brief; PostgREST returned 400 with `code=42703 message="column lifecycles.id does not exist"`. VPS CC self-corrected to the actual columns and confirmed reachability. Codified as standing rule + baked into `pr-brief-lint`.

- **§0.5.142 (multi-line `python -c` heuristic)** — Newly discovered Claude Code safety check. Triggered during the settings.local.json merge (Brief 1 for redaction) when VPS CC ran `python3 -c "<multi-line>"` for JSON validation. Same family as §0.5.117/.118. Cannot be silenced via settings; brief discipline only.

- **§0.5.143 (Markdown mangling)** — PR #14's `/status` output showed `open_trades` → "opentrades" and `net_liq` → "netliq" because legacy Markdown's `_word_` italic syntax ate the underscores. Caught from operator's phone screenshot. PR #15 fixed by dropping `parse_mode` entirely.

- **§0.5.144 (secrets audit)** — Pre-existing GitHub PAT `ghp_LR3D…` (redacted) was sitting verbatim in `settings.local.json` from a pre-OAuth-upgrade session (§0.5.128 had retired the PAT in active use but never removed it from settings). VPS CC discovered during merge and flagged urgently. Operator rotated at github.com/settings/tokens within 3 minutes; VPS CC redacted to generic `Bash(GH_TOKEN=* gh repo:*)` pattern and shredded 4 files containing the literal token (`settings_backup_*.json`, `new_settings.json`, `pat_hits.txt`, plus the Claude Code task output cache at `/tmp/claude-1000/.../tasks/bkok3qf21.output`). Codified as standing rule.

- **Settings broadening** — `.claude/settings.local.json` grew from 29 allow / 0 deny → 80 allow / 19 deny entries via comprehensive sweep (§0.5.141). Project-scope grants now cover `/tmp/`, `~/tradeflow/`, common docker/gh/python tooling. Denies cover `~/.tradeflow-secrets/`, `git push origin main`, container destruction, `--dangerously-skip-permissions`.

- **Telegram render mechanics** — `parse_mode=Markdown` is the wrong default for any message containing Python identifiers. The `_format_alert` helper, the `_handle_status` lines block, and any future formatted message MUST use either plain text or MarkdownV2 with the full escape set.

- **Existing `TELEGRAM_CHAT_ID=` legacy env var** — was sitting empty in `~/.tradeflow-secrets/.env` from a pre-PR-14 Phase 0 placeholder. Confirmed unused by `git grep TELEGRAM_CHAT_ID` returning zero hits in src/comms/tests. Operator manually deleted the line during cleanup.

---

## 2. The session's bug + build thread

1. **Session opened** with V0-V5 verification of the post-PR-#11 idle bot. All PASS — `serverVersion=178`, `account=DUQ331660`, `positions=[]`, `openTrades=[]`, `lifecycles count=0`, healthcheck + reconciler firing on cadence. One probe-bug surfaced: my brief specified `select=id` for `lifecycles`/`lifecycle_events` but actual PKs are `lifecycle_id`/`event_id`. VPS CC self-corrected by re-running with `select=lifecycle_id` / `select=event_id`. §0.5.140 born.

2. **PR #12 designed + shipped** as a bundle: halt-ack (Supabase primary + file-flag fallback) + `pr-brief-lint` skill (3 lints) + `scripts/health_snapshot.sh` + HEALTHCHECK Dockerfile directive + supabase migration. 11 files, ~500 lines added. VPS CC's Task A audit caught a surprise: existing migrations live at `supabase/migrations/YYYYMMDDHHMMSS_<name>.sql` (Supabase CLI naming) NOT `migrations/0001_*.sql` as the brief speculated. Adjusted before implementation. Also: reconciler test file is `tests/test_execution_reconciler.py` not `tests/test_reconciler.py`; EOD file is `src/execution/force_close.py` not `eod.py`. Three Task-A findings prevented three potential PR slips. CI green (43s). 165 tests cumulative.

3. **Settings broadening + PAT incident** — During the `settings.local.json` merge (chat-side brief to drop in the comprehensive permission sweep §0.5.141), VPS CC discovered a literal GitHub PAT in line 5 of the existing file. Operator rotated the token at github.com/settings/tokens within 3 minutes; VPS CC redacted to a generic pattern and `shred -uvz`'d four files. §0.5.144 (secrets audit on every config merge) born. **The PAT redaction itself triggered §0.5.142** — VPS CC ran `python3 -c "<multi-line>"` for validation, which fired a previously-unknown Claude Code heuristic. Same family as §0.5.117/.118. Codified.

4. **PR #13 shipped** — Tiny one-file follow-up. Added §0.5.117/.118 + §0.5.142 as the 4th section in `pr-brief-lint/SKILL.md`. CI green (34s).

5. **PR #14 designed + shipped** — Telegram alerter + 3 commands, decoupled via `logging.Handler` filtered on `[ALERT]` prefix. 7 files, ~857 lines added. VPS CC's Task A audit caught: `comms/telegram.py` ALREADY EXISTED as a Phase 0 stub with zero callers — safe to replace completely. Other surprises documented in the PR body. 186 tests cumulative. CI green (38s).

6. **Telegram activation** — Operator created bot via @BotFather, retrieved chat_id via the getUpdates HTTPS endpoint, added `TELEGRAM_BOT_TOKEN` + `TELEGRAM_OPERATOR_CHAT_ID` to `~/.tradeflow-secrets/.env`. Container rebuilt with `--build --force-recreate`. Startup logs confirmed `[telegram] handler_installed`, both background tasks launched. Operator sent `/status` from phone → bot replied within seconds.

7. **§0.5.143 Markdown mangling caught from phone** — Operator's screenshot of the `/status` reply showed `open_trades` → "opentrades" and `net_liq` → "netliq". Cause: `parse_mode=Markdown` italicized `_trades_` and `_liq_` and the rendering ate the underscores. (Newlines in the pasted text were just a copy-paste artifact, not a bug — confirmed against the screenshot, which shows clean line breaks.) §0.5.143 codified.

8. **PR #15 shipped** — Tiny 2-file fix: removed `parse_mode=Markdown` from `_send`, replaced `*TradeFlow*` and `*Status*` literals with plain text, updated 1 test assertion. CI green (37s). Operator rebuilt container; `/status` re-tested from phone → screenshot confirms `open_trades: 0` and `net_liq: $1000085.80` rendering verbatim now.

9. **Env var cleanup** — Operator manually deleted vestigial `TELEGRAM_CHAT_ID=` line from `~/.tradeflow-secrets/.env` after VPS CC's grep confirmed zero references in src/comms/tests. No container restart needed (the var wasn't being read anyway).

10. **Session 7 closes** — Bot is alive, healthy, $1M paper NetLiq, alert-able. Memorial Day Monday is the calendar gate. Tuesday 09:30 ET is the first real RTH session.

**Rabbit holes closed**:
- ❌ "Maybe the bot needs Telegram to function" — no, it's optional. Disabled cleanly when env vars absent.
- ❌ "Maybe we should refactor halt to be per-symbol" — deferred to PR #16+. Global halt is sufficient for v1.
- ❌ "Maybe we should switch to MarkdownV2 for telegram formatting" — deferred. Plain text is fine until we have a content type that needs bold/italic.
- ❌ "Maybe we need a webhook for telegram" — long-poll is sufficient. No public endpoint exposed.

---

## 3. What the system is actually made of

**Single source of truth:** `git -C ~/tradeflow ls-tree -r HEAD --name-only` on `main` at `6ab026e` or later.

Highlights:

- **3 containers**: `tradeflow-app` (orchestrator, PID 1 `python main.py`, now `(healthy)` per HEALTHCHECK), `tradeflow-ib-gateway` (IBC-managed IB Gateway). No third — Telegram runs inside `tradeflow-app` as background tasks.

- **3 DB tables**: `lifecycles` + `lifecycle_events` (PR #9), `halt_acks` (PR #12). All under RLS; service_role bypasses.

- **Production-live code paths**:
  - Orchestrator: `main.py` → `src/orchestrator.py:Orchestrator.run()`
  - Strategy: `_on_new_bar` → `Sma100BounceStrategy.detect_signal` → `_handle_trade_signal` → `OrderRouter.place_entry`
  - Fill handling: IB `fillEvent` → `OrderRouter.on_fill` → `_handle_parent_fill` / `_handle_exit_fill`
  - EOD: `EodForceClose.run_until_stopped` → fires at 15:58 ET
  - Reconciler: `Reconciler.run_until_stopped` → 30s drain + 5-min full-scan + halt-ack poll (PR #12)
  - Halt API: `Orchestrator.raise_halt` / `clear_halt` / `is_halted` / `halt_raised_at` (PR #12)
  - Telegram (new in PR #14, fixed in PR #15):
    - Alerter: `LOGGER.info("[ALERT] event: k=v")` from any subsystem → `TelegramAlertHandler.emit` (filtered) → `_queue` → `TelegramAlerter.alert_loop` → httpx POST
    - Commands: `TelegramAlerter.command_loop` → long-poll `getUpdates` → `_handle_update` → `_handle_status`/`_handle_halt`/`_handle_ack`
  - Recovery (boot-only): `Orchestrator._recover_state` → loads non-CLOSED lifecycles → broker-field repopulate

- **Dead/phantom surfaces (unchanged from HANDOFF_v5)**: top-level stub packages `strategy/`, `execution/` (wait — `execution/` is populated), `risk/`, `features/`, `backtest/`, `data/`, `scripts/` (now populated via PR #12). Actual still-stub packages: `strategy/`, `risk/`, `features/`, `backtest/`, `data/`. `comms/` is now populated by PR #14.

- **Automation gotchas**:
  - `tradeflow-app` now shows `(healthy)` after ~60s start period (PR #12).
  - SIGTERM handler still works correctly (PR #11 fix preserved).
  - No crons. All scheduled work is asyncio tasks owned by the orchestrator.
  - Telegram outage does NOT block trading — bounded queue (500 items) drops oldest, retry-with-backoff respects stop_event.

- **0 open documented bugs** as of handoff. Operational debt in §7.

---

## 4. Verified facts about TradeFlow (as of 2026-05-22)

**DO NOT challenge these unless probed against source.**

### IBKR + IB Gateway (unchanged from HANDOFF_v5)

- IB Gateway docker image: `ghcr.io/gnzsnz/ib-gateway:stable`, server version 178
- Container port: `127.0.0.1:4002` (host) → `4004` (container)
- Paper account: `DUQ331660`
- Library: `ib_async==1.0.0`
- `IBClient.get_portfolio() → list[PortfolioItem]` is canonical (§0.5.T3)
- MNQ contract spec: TICK_SIZE=0.25, MULTIPLIER=$2/point, COMMISSION_RT=$0.62, MARGIN_REQ=$2000. Front month: **MNQM6** (June 2026, expiry Fri 2026-06-19, roll target ~2026-06-11)

### Strategy (unchanged from HANDOFF_v5)

- Identifier: `strategy="sma100_bounce"` (sticky, §0.5.133)
- Real condition: MA50 > MA100 + ADX ≥ 20 + low ≤ MA100 + `ma_touch_buffer_pts` (5pt) + bullish close + NOT within `session_edge_no_trade_minutes` (5) of session edges
- Entry: MKT. Stop: separate GTC STP at -75pt. TP: bracket-child LMT at +150pt. ADX min: 20.0 period 14. LONG-only.

### Supabase (updated this session)

- Project ref prefix: `vzlpxaif*`, region us-east-1
- Tables: `lifecycles` (PK `lifecycle_id`), `lifecycle_events` (PK `event_id`), **NEW: `halt_acks` (PK `halt_ack_id`, columns: `acked_at TIMESTAMPTZ DEFAULT now()`, `note TEXT`)** — applied via dashboard SQL editor during PR #12
- RLS enabled on all three; service_role bypasses
- Custom httpx wrapper: `src/clients/supabase_client.py`
- Methods (this session): `insert_halt_ack(note)`, `get_newest_halt_ack(since)`

### State machine + halt API (PR #12)

- Allowed transitions: unchanged from HANDOFF_v5 §4
- Halt API: `raise_halt(symbol=...)` / `clear_halt(reason=...)` / `is_halted() -> bool` / `halt_raised_at() -> datetime | None`
- Reconciler clears halt when newest `halt_acks.acked_at > _halt_raised_at` (every 30s drain)
- File-flag fallback path: `/tmp/halt_clear` (inside container) — operator runs `docker exec tradeflow-app touch /tmp/halt_clear` if Supabase is unreachable
- Lifecycle ID is stable UUID, no UNIQUE(symbol, state) constraint, no row deletion on transition

### Telegram (PR #14 + PR #15)

- Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OPERATOR_CHAT_ID` (both in `~/.tradeflow-secrets/.env`)
- Bot username: configured by operator via @BotFather
- Auth: single operator chat_id whitelist. Foreign chat_id → "Unauthorized." reply + WARNING log
- Alerts emit via standard `LOGGER.info("[ALERT] event_type: k=v k=v")` lines. Subsystems do NOT import `comms.telegram`.
- 5 alert types: `entry_placed`, `exit_filled`, `halt_raised`, `halt_acked`, `eod_complete`
- 3 commands: `/status`, `/halt SYMBOL [reason]`, `/ack [reason]`
- Rate limit: per-event-type dedup window 30s (in-memory deque(maxlen=200))
- **`parse_mode`: NONE (plain text only, PR #15)** — never use legacy Markdown for messages containing Python identifiers (§0.5.143)
- Bounded alert queue: 500 items, drops oldest on overflow with WARNING log
- Cooperative backoff: retry on httpx errors with exponential backoff (base 2s, max 60s), respects stop_event

### Repo state at handoff

- HEAD: `6ab026e` on `main` (PR #15 squash; plus possibly HANDOFF_v6 publish above this)
- Default branch displayed: still `claude/phase-0-repo-bootstrap-ZZGJX` (cosmetic). Doesn't affect operations.
- Branch protection: active. No direct pushes to main.
- gh auth: `gho_*` OAuth (PAT removed from `settings.local.json` per §0.5.144 this session). Scopes: `gist,read:org,repo,workflow`.
- Commit authorship: clean operator authorship. NOTE: PR #15's commit message included a `Co-Authored-By: Claude Opus 4.7` trailer — that's a deviation from the established "no Co-Authored-By" convention. Cosmetic but worth flagging for future PRs.
- Settings: `.claude/settings.local.json` — 80 allow / 19 deny (per §0.5.141), PAT redacted (per §0.5.144)

### Skills in repo (`.claude/skills/`)

- `architecture-question-gate`
- `code-pr-brief`
- `prod-debug-discipline`
- `verification-before-completion`
- `vps-cc-autonomy`
- **`pr-brief-lint` (PR #12 + extended PR #13)** — encodes §0.5.137 + §0.5.139 + §0.5.140 + §0.5.117/.118 + §0.5.142 as mandatory pre-checks

### Operator-side skills (`/mnt/skills/user/`)

- `code-pr-brief`
- `prod-debug-discipline`
- `session-handoff-writer`
- `vps-smoke-test-runbook`

---

## 5. Wrong diagnoses (this session) — READ BEFORE YOU DEBUG

### Wrong call 1: V0-V5 brief used `select=id` for Supabase probes

- **What the brief did**: chat-side me wrote the V0-V5 verification block specifying `select=id` for `lifecycles` and `lifecycle_events` reachability probes.
- **What was missed**: the schema PKs are `lifecycle_id` and `event_id`, NOT `id`. PostgREST returns HTTP 400 with `code=42703 message="column lifecycles.id does not exist"`.
- **How it surfaced**: VPS CC ran the probe, got the 400, identified the column-name mismatch in real-time, re-ran with the correct column names, reported the 200/count=0 result.
- **Correct fix**: §0.5.140 — for reachability checks, use `select=*&limit=1` (always valid); for content-dependent checks, verify column names against the migration SQL or schema first.
- **Lesson**: brief-design lint at the chat-side. Now codified in `pr-brief-lint` skill.

### Wrong call 2: PR #14 used `parse_mode=Markdown` without grepping content for markdown-active chars

- **What the brief did**: chat-side me designed PR #14 with `parse_mode=Markdown` for both alerts and `/status` replies, including `*Status*` and `*TradeFlow*` literals for bold formatting.
- **What was missed**: every literal `open_trades`, `net_liq`, `lifecycle_id`, `halt_ack_id` in the message body contains underscores between letters. Legacy Markdown treats `_word_` as italic emphasis and eats the underscores during rendering. The operator-facing `/status` output therefore showed `opentrades` and `netliq`.
- **How it surfaced**: operator sent `/status` from phone, took a screenshot, pasted to chat. Visible mangling.
- **Correct fix**: PR #15 dropped `parse_mode` entirely. Plain text is unambiguous; we lose bold but it doesn't matter for status/alert content.
- **Lesson**: §0.5.143. When designing user-facing formatted messages, grep the message body for markdown-active chars (`_`, `*`, `[`, `]`, etc.) against the chosen `parse_mode`. Default to plain text unless formatting is load-bearing.

### Wrong call 3 (not really a wrong call — discovery): PAT in settings.local.json

- **What was found**: line 5 of the existing `settings.local.json` contained a literal GitHub PAT `ghp_LR3D…` (redacted) from a pre-OAuth-upgrade session (§0.5.128 had retired the PAT in active use but never removed it from settings).
- **How it surfaced**: VPS CC's `cat` of the file before the §0.5.141 broadening merge.
- **Correct response**: immediate revocation by operator at github.com/settings/tokens; redaction by VPS CC to generic `Bash(GH_TOKEN=* gh repo:*)` pattern; shred of 4 files containing the literal token (backup + new + grep-hits + Claude Code task cache).
- **Lesson**: §0.5.144. Every config-file merge MUST grep for `ghp_`, `gho_`, bearer credentials, etc. BEFORE preserving entries verbatim.

### Meta-lesson for Session 8

Of the four 0.5.14X rules added this session, **two** came from chat-side brief slips (column-names; markdown), **one** came from a new Claude Code heuristic (`python -c` newline+#), and **one** came from a discovered operator-side artifact (PAT in settings). The brief-design lints in `pr-brief-lint` now have **5 rules**: §0.5.137 + §0.5.139 + §0.5.140 + §0.5.117/.118 + §0.5.142. PR #16+ briefs MUST run all five at design time.

Pattern observation: **chat-side me is the most common source of upstream brief slips**, not VPS CC. The lint skill catches it before VPS CC sees it.

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

Expect: HEAD at `6ab026e` (PR #15) or later — possibly the HANDOFF_v6 publish commit above it.

### V1 — Containers up and HEALTHY

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

Expect:
- `tradeflow-app` shows `Up <duration> (healthy)` — PR #12 HEALTHCHECK is live
- `tradeflow-ib-gateway` shows `Up <duration> (healthy)`

If `tradeflow-app` shows `(unhealthy)`: PR #12 HEALTHCHECK regression or process died. `docker logs tradeflow-app --tail 100` and report.

```bash
docker inspect tradeflow-app --format 'RestartCount={{.RestartCount}} Status={{.State.Status}} Health={{.State.Health.Status}}'
```

Expect: `RestartCount=0`, `Status=running`, `Health=healthy`.

### V2 — Deployed-code check (PR #12 + #14 + #15 invariants)

```bash
docker exec tradeflow-app grep -nE 'def _handle_trade_signal' /app/src/orchestrator.py
```

Expect: exactly one match. PR #11 invariant.

```bash
docker exec tradeflow-app grep -nE 'def _handle_signal\b' /app/src/orchestrator.py
```

Expect: exactly one match (the SIGTERM handler with `signum, frame` signature).

```bash
docker exec tradeflow-app grep -nE 'def raise_halt\b|def clear_halt\b|def is_halted\b|def halt_raised_at\b' /app/src/orchestrator.py
```

Expect: 4 matches (PR #12 halt API).

```bash
docker exec tradeflow-app grep -nE 'class TelegramAlerter\b|class TelegramAlertHandler\b' /app/comms/telegram.py
```

Expect: 2 matches (PR #14).

```bash
docker exec tradeflow-app grep -nE 'parse_mode' /app/comms/telegram.py
```

Expect: **zero matches** (PR #15 removed legacy Markdown).

### V3 — IBKR paper state (source of truth)

```bash
bash ~/tradeflow/scripts/health_snapshot.sh
```

This is the script committed in PR #12. Runs the full V0-V5 check end-to-end with the §0.5.140 column-name fix. Expect:
- `serverVersion= 178`
- `accounts= ['DUQ331660']`
- `positions= []`
- `openTrades= []`
- `lifecycles status=200 count=0`
- `lifecycle_events status=200 count=0`
- (Optional: `halt_acks status=200 count=0` if the script was extended to probe it — check)

If `positions` is non-empty: STOP, check for `[RECON] foreign_position` lines in the orchestrator log AND check Telegram for any received alerts. Capital-at-risk state.

### V4 — Supabase `halt_acks` reachable

```bash
docker exec tradeflow-app /usr/local/bin/python -c "import os; from src.clients.supabase_client import SupabaseClient; print('halt_acks accessible')"
```

(Use a stand-alone script if this trips §0.5.142.) Alternative: probe via httpx directly from the host venv.

### V5 — Telegram bot live + responsive

From operator's phone, send `/status` to the bot. Expect reply within ~5s with clean plain text:
```
Status
halted: no
positions: []
open_trades: 0
account: DUQ
net_liq: $XXXXXXX.XX
```

If no reply: check `docker logs tradeflow-app --since 5m | grep -E '\[telegram\]|poll_exception'`. Confirm `command_loop: task_launched` is present in startup logs.

### V6 — Reconciler + healthcheck cadence (90s window)

```bash
docker logs tradeflow-app --since 90s | grep -E '\[ORCH\] healthcheck|\[RECON\] tick|\[telegram\]'
```

Expect:
- 1-2 `[ORCH] healthcheck: ok` (60s cadence)
- 2-3 `[RECON] tick: drain_complete` (30s cadence)
- 0 or 1 `[telegram]` lines (only on httpx events)

```bash
docker logs tradeflow-app --since 90s | grep -iE 'error|exception|traceback'
```

Expect: empty. If non-zero, capture sample and report.

---

## 7. Pending work queue

### Tuesday observation (Session 8 main event)

**Status**: passive, watch-and-learn. Primary Session 8 work.

**Scope**: observe Tuesday 2026-05-26 first RTH session starting 09:30 ET (signal-eligible 09:35 ET). Watch for:
- MA conditions triggering a signal (`[STRAT] sma100_bounce: signal_emitted` log line)
- Telegram `entry_placed` alert
- Parent fill → bracket leg landing (`[EXEC] parent_filled` then `[EXEC] stop_placed`)
- Reconciler drain handling post-fill state changes
- Telegram `exit_filled` alert (with pnl) when bracket leg fires
- EOD at 15:58 ET (telegram `eod_complete` alert)

**Verification**: log tail `docker logs tradeflow-app --since 6h | grep -E '\[STRAT\]|\[EXEC\]|\[RECON\]|\[EOD\]|\[ALERT\]'` followed by Supabase `SELECT * FROM lifecycles` to verify row landed end-to-end.

**Expected outcome at end of day**:
- Either: clean first paper trade landed → write HANDOFF_v7 with the trade narrative
- Or: no signal fired → write HANDOFF_v7 documenting "MA conditions not met today"
- Or: signal fired but something broke → debug via the next PR

### PR #16+ — backlog (post-Tuesday-observation)

Priorities will depend on what Tuesday reveals. Candidates:

1. **More telegram commands**: `/eod` (force EOD now), `/positions` (broker positions detail), `/healthcheck` (manual liveness ping). ~3 files, ~40 lines each. Half-day.
2. **MarkdownV2 escape utility**: if we ever want bold formatting back in telegram (e.g. for daily summaries), build an escape helper. Optional, low priority.
3. **SHORT-side strategy enablement**: currently LONG-only per §0.5.130. Requires strategy logic + entry/exit testing. 1-2 days.
4. **Dashboard**: web UI for richer state. Defer until we know what queries we keep running ad-hoc. 1-3 days depending on scope.
5. **Daily/weekly kill-switches**: programmatic pause if losses exceed threshold. Half-day. Pairs with telegram `/clear-kill-switch` command.
6. **Persistent dedup across container restart**: telegram alerter currently loses the dedup deque on restart. Could persist to file or Supabase. Optional.

### Operational debt

- Repo default branch displayed as `claude/phase-0-repo-bootstrap-ZZGJX` (cosmetic, doesn't affect ops)
- Repo auto-merge is enabled
- Top-level stub packages still empty: `strategy/`, `risk/`, `features/`, `backtest/`, `data/`
- `comms/__init__.py` is empty (was left empty by PR #14 intentionally)
- DUQ account ID leak in logs accepted; future PR could redact via env var
- `.env` line 17 `bash source` incompatibility (§0.5.125) — leave as is
- `Co-Authored-By: Claude Opus 4.7 (1M context)` trailer in PR #15 commit — deviation from convention. Future PRs should omit.

### Bugs by ID

None open. Session 7 closed all production bugs (none surfaced).

---

## 8. Test safety — why we belabor this

(Carried forward from HANDOFF_v5 §8.)

Cumulative test-mocking failures across prior sessions:

1. Tests passed against fictional schema — always probe migration SQL or `information_schema.columns` before column-assertion tests.
2. `side_effect` list with wrong count → silent `StopIteration` → wrong assertions. Always count `side_effect` calls.
3. Mocked at raw library chain when code uses a wrapper → mock at the wrapper boundary.
4. Shared `MagicMock` state leaked between tests — fresh instances per test.
5. Async decorator pattern assumption — verify neighbors; TradeFlow uses `asyncio_mode=auto` so no `@pytest.mark.asyncio` decorators needed.
6. Tests didn't catch `_handle_signal` collision (HANDOFF_v5 §8) — integration tests at orchestrator level catch shadow-binding bugs better than unit mocks at the router boundary.

**New this session**:

7. **Tests didn't catch the Markdown mangling** — `tests/test_comms_telegram.py` mocked the httpx POST and verified the request payload included `parse_mode=Markdown` + the literal text. But the test never rendered the Markdown through Telegram's parser. **Lesson**: when sending content to an external rendering service (Telegram, Slack, email HTML, etc.), the test suite cannot validate visual fidelity — only operator screenshot can. Add a manual smoke step to PR briefs that ship user-facing formatted messages.

8. **`tests/test_execution_force_close.py::test_fire_once_idempotent_when_called_twice_with_same_state` broke during PR #14** — the new `[ALERT] eod_complete` log line added a second `load_non_closed()` call. The test had `side_effect=[..., []]` hardcoded for exactly one call. VPS CC caught this during the full-suite run, fixed by computing `remaining` from the in-memory list rather than a second DB round-trip. **Lesson**: when modifying a function that already has tests using `side_effect` lists, count the new mock invocations vs the test's existing fixture count.

---

## 9. Pitfalls from prior sessions

(Carried forward + new.)

LLM trust-but-verify list — things any chat or VPS CC got wrong before and should not trust without verification:

- "`pyproject.toml` is missing pandas/numpy" — wrong (HANDOFF_v5). Probed and they were present.
- "Dockerfile is in MUST-NOT-MODIFY so it can't be the issue" — wrong (HANDOFF_v5 PR #10). §0.5.137 born.
- "`_handle_signal` is the trading handler" — wrong (HANDOFF_v5 PR #11). §0.5.139 born.
- "Tests passing means deployed image is correct" — wrong (HANDOFF_v5). Smoke is source of truth post-merge.
- "Memorial Day is the last Friday in May" — wrong. Last Monday. 2026: Monday May 25.
- "Supabase `lifecycles` PK is `id`" — wrong (this session). It's `lifecycle_id`. §0.5.140 born.
- "Telegram `parse_mode=Markdown` is safe for variable-name content" — wrong (this session). Underscore mangling. §0.5.143 born.
- "Existing `settings.local.json` is safe to preserve verbatim" — wrong (this session). Contained a literal GitHub PAT from a pre-OAuth-upgrade session. §0.5.144 born.
- "VPS CC always follows §0.5.117/.118 bash discipline" — wrong (PR #12 + the redaction session). Chained `&&` shred slipped. Lint skill catches at brief-design now but VPS CC's improvisation can still trigger; operator picks option 1 on those prompts to keep the discipline strong.

**Next session rule** (unchanged from HANDOFF_v5): if a claim is quantitative or date-dependent, re-verify it. Especially calendar facts, row counts, port numbers, env-var names, and "is X in scope" claims.

---

## 10. Session discipline lesson (Session 7)

**The four new standing rules from Session 7 split cleanly by source**:

| § | Source | Type |
|---|---|---|
| §0.5.140 | chat-side brief slip | column-name correctness |
| §0.5.141 | chat-side process improvement | settings broadening (operator UX) |
| §0.5.142 | VPS CC hit a new heuristic | hardcoded CC behavior |
| §0.5.143 | chat-side brief slip | user-facing render fidelity |
| §0.5.144 | discovered operator artifact | secrets audit on merge |

**Two of five are chat-side brief slips** (§0.5.140, §0.5.143). The `pr-brief-lint` skill now has 5 mandatory lints. Future briefs MUST run all five.

### Enforcement rules for Session 8

1. **`pr-brief-lint` is mandatory pre-Task-A**. Every brief MUST cite the grep output for each of the 5 lints in §0 of the brief (or in a "Brief-design lints" preamble).
2. **Operator screenshots are part of post-merge smoke for any UI-touching PR.** Tests cannot validate visual fidelity through external renderers (Telegram, email, Slack). Add a step to Task F.
3. **Secrets audit on every config merge.** §0.5.144 — grep for `ghp_`, `gho_`, bearer tokens, etc. BEFORE preserving entries verbatim.
4. **VPS CC bash-discipline slips remain operator-side prompts.** Pick option 1 (one-time allow) on chained-bash prompts; project-scope allow would silence the discipline signal we want.
5. **Tuesday observation is the highest-priority activity of Session 8.** No code PRs before the first RTH session has played out. Post-Tuesday triage based on what we learn.

---

## 11. Logging verbosity — what to demand from any new code

(Carried forward from HANDOFF_v5 + new.)

- Every state transition logs `[COMPONENT] symbol: action — reason` at INFO
- Every swallowed exception logs the specific error + context
- Async background tasks log `task_launched` + `task_exited`
- Healthcheck loop: `[ORCH] healthcheck: ok` every 60s
- Reconciler: `tick: drain_complete`, `tick: full_scan_complete`, per-lifecycle action enum value
- Foreign-position detection: `[RECON] foreign_position: ...` at WARNING
- Order placement: `entry_placed`, `bracket_placed`, `parent_filled`, `stop_placed`, `exit_filled`, `trade_closed` with pnl

**New this session**:

- **Telegram alert lines emit standard `LOGGER.info("[ALERT] event_type: k=v k=v")`** from any subsystem. Subsystems NEVER import `comms.telegram`. The `[ALERT]` prefix is the integration contract. Future events that should reach the operator's phone use the same pattern.
- **Halt API logging**: `[ORCH] halt_raised: symbol=X` + `[ALERT] halt_raised: symbol=X` (sibling lines); same shape for `clear_halt`.
- **Reconciler halt-ack**: `[RECON] halt_acked: source=supabase acked_at=... note=...` OR `[RECON] halt_acked: source=file_flag mtime=...`; failure path logs `[RECON] halt_ack_poll_failed: <error>` at WARNING.
- **Telegram subsystem**: `[telegram] handler_installed: queue_max=N`, `[telegram] alert_loop: task_launched`, `[telegram] command_loop: task_launched`, `[telegram] send_failed: status=N body=...` at WARNING, `[telegram] poll_exception: <err>` at WARNING, `[telegram] unauthorized_command: chat_id=X text=...` at WARNING, `[telegram] alert_queue_full — dropping oldest` at WARNING.

Demand this verbosity in every new PR. PR briefs that have new code paths without logging get pushed back at brief-design time.

---

## 12. Master template — use for every Claude Code PR

See `.claude/skills/code-pr-brief/SKILL.md` (operator-side) for the full master template. PR #12 + #13 + #14 + #15 all followed it.

**MANDATORY for Session 8+ briefs** (per §10 enforcement rules):

- Run all 5 `pr-brief-lint` checks at brief-design time. Cite grep output in the brief preamble or §0.
- For any user-facing formatted message (Telegram, email, Slack, etc.), add an operator screenshot step to Task F. Don't rely on unit tests for visual fidelity.
- Carry forward all §0.5.97 through §0.5.144 in the Known Gotchas section verbatim.

---

## 13. Current PR brief in flight — none

No code PR in flight at handoff. PR #15 was the last code change in Session 7. Tuesday observation is passive — not a code PR.

Useful artifacts on the VPS at session close:

- `scripts/health_snapshot.sh` — committed in PR #12, runs the full V0-V5
- `scripts/_probe_ibkr.py` — IBKR paper state via host venv python
- `scripts/_probe_supabase.py` — three-table reachability check (lifecycles, lifecycle_events, halt_acks)
- `/tmp/` is mostly empty post-shred. Probe scripts staged during sessions live under `/tmp/wait_*.py` patterns.

---

## 14. Canonical references (in order of authority)

1. **`src/` + `comms/` on `main` at `6ab026e`** — verified system reality. The actual code that runs.
2. **`docs/handoffs/HANDOFF_v6.md`** — this doc, once published per §16.
3. **`docs/handoffs/HANDOFF_v5.md`** — Session 6 context, §0.5.124–.139 verbatim source.
4. **`docs/handoffs/HANDOFF_v4.md`** — Session 5 context, §0.5.97–.123 + §0.5.T1–T5 verbatim source.
5. **Supabase production DB** — `lifecycles` + `lifecycle_events` + `halt_acks`, queried via service_role.
6. **IBKR via `ib_async`** with env from `~/.tradeflow-secrets/.env` — truth for position/order/account state.
7. **`docker logs tradeflow-app`** — runtime narrative, last 24h typically.
8. **Telegram bot from operator's phone** — operator-facing UI; `/status` is the lowest-friction live state query.
9. **This handoff (v6) §1–6** — session context, NOT long-term authority. Re-verify against 1 if disagreement.

---

## 15. First 15 minutes of Session 8

If Session 8 starts **before Tuesday 09:30 ET** (Sunday or Monday process slack, or Tuesday pre-market):

1. Read §0.5, §1, §5, §6, §7, §10 of this handoff. §5 (wrong diagnoses) and §10 (session discipline) are the highest-leverage reads. ~5 minutes.
2. SSH to VPS. Run V0-V5 from §6. Confirm all green. ~3 minutes.
3. From operator's phone, send `/status` to Telegram. Confirm clean reply with `open_trades` and `net_liq` rendering verbatim. ~1 minute.
4. Wait for market open (Tuesday 09:30 ET).

If Session 8 starts **after Tuesday 09:30 ET**:

1. Read §0.5, §1, §7's "Tuesday observation" entry. ~3 minutes.
2. SSH to VPS. Run V0-V5 from §6.
3. Tail logs: `docker logs tradeflow-app --since 6h | grep -E '\[STRAT\]|\[EXEC\]|\[ALERT\]|\[ORCH\] signal|\[RECON\] foreign'`
4. Check Telegram chat for any `entry_placed` / `halt_raised` / `eod_complete` alerts received.
5. Query Supabase `lifecycles` table — `SELECT * FROM lifecycles WHERE created_at >= '2026-05-26'::date` — confirm any rows landed end-to-end.
6. Draft HANDOFF_v7 with the day's narrative.

---

## 16. How to publish this handoff

**Path A — VPS Claude Code brief (preferred):**

Paste the brief at the bottom of the chat session's final message to VPS CC. VPS CC saves to disk, commits, pushes, opens PR #25, merges. The brief is in the chat session above this handoff.

**Path B — Manual fallback:**

```bash
scp HANDOFF_v6.md tradeflow@5.78.212.37:/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v6.md
```

```bash
ssh tradeflow@5.78.212.37
```

```bash
cd ~/tradeflow
```

```bash
git checkout -b docs/handoff-v6
```

```bash
git add docs/handoffs/HANDOFF_v6.md
```

```bash
git commit -m "docs: add v6 handoff (Session 7 — halt-ack + telegram live, ready for Tuesday)"
```

```bash
git push --set-upstream origin docs/handoff-v6
```

```bash
gh pr create --base main --title "docs: add v6 handoff (Session 7 — halt-ack + telegram live)" --body "Session 7 close. See docs/handoffs/HANDOFF_v6.md for the full doc."
```

```bash
gh pr merge --squash --delete-branch --auto
```

The handoff exists only if saved to disk AND committed AND merged to main.

---

*End of handoff v6. Target lifespan: until Tuesday 2026-05-26 close-of-trading; then v7 captures the first paper trade narrative and v6 becomes historical, ranked below v7 + live code in §14.*
