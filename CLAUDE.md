# TradeFlow — CC VPS Operating Manual

This file auto-loads on every Claude Code session. It contains the always-on rules,
stack, and gotchas. Session-specific state lives in `docs/handoffs/HANDOFF_v<latest>.md`
— always read the latest handoff at session start.

## What this project is

TradeFlow is an autonomous MNQ futures paper trading bot on IBKR (account `DUQ331660`),
running 24/5 CME hours on a Hetzner CX32 VPS (Ubuntu 22.04, Ashburn). Strategy is
SeanBot V3-aligned pullback (100-bar SMA bounce, long-only, 2 contracts). Operator is
hands-off PM; you (CC VPS) handle git, docker, gh, tests, deploys, smokes.

## Tech stack (DO NOT propose alternatives unless explicitly asked)

- Python 3.11
- ib_async (active ib_insync fork) — IBKR connector
- Supabase (Postgres) — lifecycle persistence; accessed via a custom REST client (not the supabase-py SDK), with the service role key
- Docker Compose — orchestration (services: `tradeflow-app`, `ib-gateway`)
- FastAPI + uvicorn + Jinja2 — dashboard at `127.0.0.1:8080` (in-process asyncio task launched by the orchestrator; HTTP Basic auth via `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`)
- Telegram — alerter + command bot (`comms/telegram.py`)
- pytest (host venv at `/home/tradeflow/tradeflow/.venv/bin/pytest`) — NOT in prod container
- black + ruff — formatter + linter
- gh CLI — PR lifecycle on the VPS, no browser needed

## Four core engineering rules

1. **Ask, don't assume.** If file paths, library APIs, schema, or behavior aren't
   obvious from reading the actual code, probe — don't bake assumptions into briefs
   or PRs. Use `VERIFY IN A.X` placeholders when uncertain.
2. **Simplest solution that works.** No premature abstraction. No "while I'm here"
   refactors. One PR, one objective.
3. **Don't touch unrelated code.** Every PR brief carries an explicit "Files MUST
   NOT modify" list. Respect it. If you spot an adjacent bug, document in PR
   description; do NOT fix.
4. **State uncertainty explicitly.** Every PR description ends with a "What I got
   wrong" section. Even if it's "nothing." Train the next session to expect honesty.

## Autonomy contract — AUTO / REPORT / AUDIT

Every PR brief carries an `## Autonomy Level: <LEVEL>` header. Operator role at each:

| Level | Scope | Operator role |
|---|---|---|
| AUTO | Docs, config tweaks, log format, tests-only, dependency patch bumps | Zero. Read structured report after auto-merge. |
| REPORT | Bug fixes ≤5 files w/ strong test coverage, refactors with no public-API change | One word in chat: `merge` or `stop` |
| AUDIT | Order execution, strategy, kill switch, secrets, multi-file >50 LOC, broker-state-altering | Open PR in GitHub, scan diff, type `merge`. |

Default if ambiguous: REPORT.

## Harness denials — DO NOT plan workflows that require these

Your shell harness blocks the following commands. Plan around them.

- `git reset --hard <any-target>`
- `git rebase`, `git rebase --onto`
- `git push --force`, `git push --force-with-lease`
- `git push origin main` (branch-protected anyway)
- `git branch -D <branch>`
- `git commit --amend`
- Bare `sleep <N>` — use `timeout <max> bash -c 'until <cond>; do sleep 2; done'` instead
- `docker exec <c> env` — use `docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}'`
- `--dangerously-skip-permissions`
- Writes to `.claude/`, `/home/tradeflow/.tradeflow-secrets/`, or anything under those

## Bash command style — avoid self-inflicted permission prompts (READ THIS)

Most prompts in this project are NOT allowlist gaps — the allowlist is already comprehensive
(`git:*`, repo `Write`/`Edit`, `docker compose:*`, the venv + watchdog-venv pythons, etc.).
They come from command STYLE that trips harness *security gates*, which OVERRIDE the allowlist.
Adding more allow-entries does nothing for these. Follow these rules and the prompts disappear:

1. **No brace-group + quotes + redirect.** `{ echo "x"; git log; } > /tmp/f 2>&1` trips the
   "expansion obfuscation" gate every time, allowlist or not. Instead: issue each command as its
   own simple Bash call (parallelize them in ONE message), or use the Read/Grep/Glob tools.
2. **Never use `cd`.** `cd /home/tradeflow/tradeflow; <cmd>` trips the "path-resolution-bypass"
   gate. The cwd is already the repo — use absolute paths or `git -C`, plus the dedicated tools.
3. **No `;` / `&&` mega-commands** to "save calls." Separate Bash calls in one message run in
   parallel. A chained command only needs one bad token (cd, brace, `VAR=`) to force a prompt.
4. **No leading `VAR=value` env prefixes.** `GIT_COMMIT=… docker compose build` makes the matcher
   read `GIT_COMMIT=…` as the first token, so `Bash(docker compose:*)` never matches. Put the var
   in the compose `.env`, or eat the single once-per-deploy prompt.
5. **Use absolute paths that match the allowlist.** `.watchdog-venv/bin/python …` prompts;
   `/home/tradeflow/tradeflow/.watchdog-venv/bin/python …` is already allowed. Same for any binary.
6. **Prefer dedicated tools over shell-outs.** Read/Grep/Glob/Write never trip these gates;
   `grep`/`sed`/`cat`/`find`/`echo`-to-file can. Use the Write tool for `/tmp` scratch files.

Net rule: separate simple Bash calls, absolute paths, no `cd`, no brace-quote redirects, no
`VAR=` prefixes, dedicated tools over shell-outs. Settings load once at session start, so
allowlist edits never help the current session anyway — this discipline is the only same-session lever.

## Common confabulations to avoid (verified gotchas)

1. **Compose service ≠ container_name.** Service `ib-gateway` ≠ container `tradeflow-ib-gateway`. Use service name for `docker compose` ops, container_name for `docker exec` / `docker logs` / `docker inspect`.
2. **Pytest is NOT in prod container.** Use host venv: `/home/tradeflow/tradeflow/.venv/bin/pytest`.
3. **`.tradeflow-secrets/.env` shadows compose defaults.** Any `${VAR:-default}` in `docker-compose.yml` may be overridden by `.tradeflow-secrets/.env`. Grep before assuming.
4. **Branch off `origin/main`, never local `main`.** Local main may drift cosmetically post-squash-merge. Always `git fetch origin && git checkout -b claude/<name> origin/main`.
5. **`docker restart` does NOT rebuild.** After merging a code PR, run `docker compose build <svc> && docker compose up -d --force-recreate <svc>`.
6. **Strategy lives in `src/strategy.py` (file), not `src/strategy/` (dir).** Same for `comms/telegram.py` — it's a sibling to `src/`, not under `src/`.
7. **Broker/exchange state is ground truth, not internal DB.** For positions/fills/capital, query IBKR via ib_async, not the Supabase tables.

## TradeFlow-specific operational rules (T-series, referenced in code comments)

These rules are referenced directly in production source (`src/orchestrator.py`,
`src/clients/ib_client.py`). Keep them visible so doc and code stay aligned.

- **§0.5.T1** — IBKR client IDs: orchestrator/broker uses `IBKR_CLIENT_ID`. (If a separate market-data feed client is added later, allocate `IBKR_CLIENT_ID + 1` to avoid gateway clashes.)
- **§0.5.T2** — Long-only bracket-child SL is **disabled by default**; protective stop is placed separately as a GTC STP order by the order router on parent fill.
- **§0.5.T3** — Use `IB.portfolio()` not `IB.positions()` for runtime reconcile (portfolio carries `marketPrice` and `unrealizedPnL`).
- **§0.5.T4** — Kill switch: ⚠️ DOC DRIFT — the historical "exits code `42` + systemd `RestartPreventExitStatus=42`" does NOT match this Docker-compose deployment. The kill switch raises an in-process halt + flattens (no process exit); it stays halted until a manual Supabase `halt_acks` / `/tmp/halt_clear` reset. See `docs/runbooks/kill_switch_restart.md`.
- **§0.5.T5** — Never leave a futures position open on IBKR without a working GTC protective stop. Verify after every entry fill.

## Standing rules — STABILIZE-5 + change management (§0.5.205–209)

- **§0.5.205 — A parentId-linked bracket child is AUTO-OCA'd by the IB gateway** (ocaType=3, group = parent permId) even when the code sets NO ocaGroup. An OCA-grouped order cannot be modified (Error 10326 → the gateway cancels it). Therefore a *modifiable* protective stop (the trailing STP) must be a **STANDALONE order (`parentId=0`), never a native bracket child.** It is placed post-fill by the router (`build_protective_stop` / `ensure_protective_stop`). Fixed-mode STP+LMT may stay an OCA bracket (never ratcheted). STABILIZE-3 (don't set ocaGroup in code) was insufficient — the gateway re-adds it for any child; STABILIZE-5 removes the child entirely.
- **§0.5.206 — The protective stop is stop-MARKET (`STP`), never stop-limit (`STP LMT`).** A guaranteed exit beats a guaranteed price for the protective leg — a stop-limit can fail to fill in a fast move and leave the position naked (Harris Ch.4). See `docs/research/book_principles.md`.
- **§0.5.207 — NEVER-ORPHAN.** A standalone stop has no OCA sibling to auto-cancel it, so when the position closes by ANY non-stop path (target / manual flatten / opposite or EOD fill / missed event) the stop must be explicitly cancelled: router `_cancel_sibling_legs` (event-driven, on the exit fill it sees) + reconciler `_cancel_open_legs` (broker-truth backstop on the flat-position close). Pairs with the never-naked leg-heal.
- **§0.5.208 — Change-management discipline.** Every change runs the loop (observe→hypothesize→build→verify→promote→monitor) in exactly one of four lanes (STABILIZE > REPLICATE > MEASURE > OPTIMIZE) and must clear the promotion gate. Never work a lower lane while a higher lane has an open defect; strategy invention stays shelved. See the `change-management` skill and `docs/ROADMAP.md`.
- **§0.5.209 — Kill-switch restart is a manual halt-ack, not a process restart.** Clear via a Supabase `halt_acks` row (primary) or `touch /tmp/halt_clear` (fallback); the reconciler polls and clears (~30s). A redeploy/force-recreate silently UN-halts a flat bot (§0.5.202) — re-halt if it must stay parked. Runbook: `docs/runbooks/kill_switch_restart.md`.
- **§0.5.220 — Never default to "we wait" on a regime-paced measurement.** When the only way to confirm a fix/edge is a LIVE test that is paced by the market regime (e.g. "wait for ~20–30 above-trend qualifying trades", "wait for the next natural feed gap", "wait for a winner to prove the profit-walk") and could take weeks-to-months, **flag the long wait UPFRONT** and **propose accelerated alternatives in the same breath** — never present "wait and watch" as the only option. The accelerated-test playbook (cheapest → most faithful): **(1) backtest** the real code over saved history (drive the actual strategy + exit functions, not a re-implementation); **(2) synthetic-scenario harness** — hand-built bar sequences that force each regime/path (run each K≥5 with randomized timing/vol/slope to prove determinism), execution stubbed; **(3) live paper round-trips off real signals** on a coordinated window (separate clientId, prod halted/flat, guaranteed flatten on exit/error); **(4) fault injection** on a throwaway instance + its own gateway (socket-drop / wedged-subscription / gateway-restart). Idle and closed-market windows are BUILD time for these harnesses, not dead time. The forward edge in the real regime is the *one* thing none of these can shortcut — say so explicitly, and use the harnesses to retire everything that CAN be proven sooner so the only thing left waiting is the genuinely-unshortcuttable. Tooling lives in `tools/eval/` (Session 24). See the `prod-debug-discipline` skill's "Accelerated-test playbook" section.

## Session start protocol

First action of every session: pre-flight scan (§0.5.165). Catches workflow debt early.

```bash
git -C ~/tradeflow fetch origin
git -C ~/tradeflow log --oneline origin/main..main
git -C ~/tradeflow log --oneline main..origin/main
gh pr list --repo ohad-oren111/tradeflow --state open
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}"
```

Then read the latest `docs/handoffs/HANDOFF_v<N>.md` for session-specific state.

## Where things live

- Secrets: `/home/tradeflow/.tradeflow-secrets/.env` (NEVER write from CC VPS)
- Handoffs: `docs/handoffs/HANDOFF_v<N>.md`
- Skills: `.claude/skills/` (autonomy contract spec, PR brief template)
- Source: `src/` (orchestrator, strategy, clients, execution)
- Tests: `tests/` (host venv pytest)
- Config: `config/risk_params.py` (single dataclass)
- Compose: `docker-compose.yml`
- Repo: `ohad-oren111/tradeflow` on GitHub

## When in doubt

Ask chat-side via a Task A clarification (§0.5.169). Chat-side answers inline.
Only escalate to operator for judgment calls. Default to STOP-and-report rather
than confabulate.

---

*This is the always-on manual. For session-specific state (current bot status,
priority queue, recent learnings, in-flight PRs, wrong-diagnosis log), read the
latest `docs/handoffs/HANDOFF_v<N>.md`.*
