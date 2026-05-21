---
name: vps-cc-autonomy
description: Codifies VPS Claude Code's autonomy default for TradeFlow. Use at the start of EVERY VPS CC session. Defines (a) the pre-flight discipline (`git pull` main, read latest handoff), (b) what counts as a critical decision that requires an operator gate, and (c) what to auto-decide without prompting. Skip this skill and you'll fall back to the verbose permission-prompt anti-pattern that Sessions 2-4 burned cycles on. Trigger words: "session start", "kickoff", "begin", "what should I do first", any new VPS CC session.
---

# VPS CC Autonomy Default

You operate on the TradeFlow VPS (Hetzner CPX21, `5.78.212.37`, user `tradeflow`). The operator (Ohad) is PM/orchestrator. He is paying real attention cost for every prompt you raise. **Your default is to decide and act.**

## Pre-flight (do FIRST in every session, before reading the operator's brief)

```bash
git -C ~/tradeflow fetch
git -C ~/tradeflow pull --ff-only origin main
ls -t docs/handoffs/ | head -3
```

Then read the latest `HANDOFF_v*.md` (and any pinned brief). If `pull --ff-only` fails (diverged), **STOP** — that IS a critical decision (Ohad has uncommitted work somewhere).

## Critical decisions — STOP and ask

These six only. Everything else: decide.

1. **Security policy mutation** — any `~/.claude/settings.json` edit not pre-approved by an active bootstrap brief.
2. **Secrets directory** — any write/delete under `/home/tradeflow/.tradeflow-secrets/`. Always immutable from VPS CC.
3. **Live IBKR orders** — place/cancel/modify on a real account. (Pre-deployment today; relevant from PR #11+.)
4. **Direct push to `main`** — branch protection already blocks it; if you find yourself designing a workaround, that's the signal.
5. **$ at risk** — anything that moves capital, takes a position, changes leverage, flips a kill switch.
6. **Explicit `GATE X` marker** in the operator's brief.

If you hit any of these, stop with this format:

```
GATE — <what's blocking>
Why I'm stopping: <which of the 6 criteria>
What I'd do if approved: <concrete action>
What I'd do if denied: <alternate path or "stop here">
```

## Auto-decide — no prompt

- All `sudo apt` / `apt-get` package installs + apt repo setup (keys, sources.list.d)
- All `docker` subcommands EXCEPT the security carve-outs (`docker exec * env*`, `docker exec * printenv*`, `docker exec -u root *`, `docker exec * sh *`, `docker exec * bash *` — those are denied at the policy layer; don't try to work around)
- All `git` on feature branches (`docs/*`, `feat/*`, `fix/*`, `chore/*`, `claude/*`) — commit, branch, push. Push to `main` stays denied.
- All `gh` CLI: PR create/view/merge/auto-merge, auth, repo ops
- File ops in `/tmp/`, `~/runbooks/`, `~/tradeflow/docs/`, `~/.claude/*.bak-*`
- Shell expansions, chaining, subshells — all permitted (`$$`, `$(...)`, `${VAR}`, `$?`, `&&`, `;`, `|`)
- `pip install`, `pytest`, `python3 -m *` in the project venv
- Reads anywhere under `/home/tradeflow/` and `/tmp/`

## Discipline carry-overs (still active)

- **§0.5.97** — probe before baking. Verify external specs (broker contracts, library API surface, env shape) against source, never memory.
- **§0.5.98** — broker/exchange API is ground truth for account state, not the DB, not the handoff doc.
- **§0.5.105** — permission rule changes are comprehensive sweeps, not patches. If you find a second mundane gate firing, redesign the sweep (escalate to operator as a "settings policy mutation" critical decision).
- **§0.5.108** — every PR you open has `--base main` pinned explicitly in `gh pr create`.
- **§0.5.110 / §0.5.114** — `.env` strict `KEY=VALUE`, no inline comments, rstrip ALL values when rewriting.
- **§0.5.111** — never paste `docker compose config` output to chat (interpolates secrets). Verify it works, discard.
- **§0.5.112** — `docker compose ps` "healthy" lies during `start_period`. Trust service-level logs.
- **§0.5.115** — autonomy default. This skill IS the rule. Any multi-step operator task should collapse to one VPS-CC-driven flow.

## Output discipline

- Compact verdicts: ✅ / ⛔ / PASS / FAIL / INVESTIGATE + one line.
- Redact account numbers (`DUQ\d+` → `DUQ…`) and passwords from any log dump before display.
- Don't echo PATs or secrets even via env. Use `gh auth login --with-token <<< "$PAT"` style and forget the variable.

## Failure mode you must avoid

**Patching the permissions allowlist one gate at a time mid-execution.** That's the Session 4 anti-pattern. If two mundane gates fire in one session, the policy is wrong — surface it as a critical decision ("Security policy mutation"), not a series of small ones.
