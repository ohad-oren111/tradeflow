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
- **§0.5.T4** — Kill switch exits process with code `42`; systemd has `RestartPreventExitStatus=42` so the unit will NOT auto-restart after a kill-switch trip.
- **§0.5.T5** — Never leave a futures position open on IBKR without a working GTC protective stop. Verify after every entry fill.

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
