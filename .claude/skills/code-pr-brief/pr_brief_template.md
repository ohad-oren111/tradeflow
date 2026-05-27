# PR Brief Master Template (v2 — autonomy-aware)

Use this structure exactly. Do not invent new sections. Do not drop sections. If a section has no content, write `N/A — no [thing] for this PR`.

**v2 changes from v1**: adds `## Autonomy Level` header, `## 🌍 Environmental Quick-Reference` section, `## ⚠️ Harness Denial Reference` section, and folds the post-merge smoke test into Task F as a runbook executed by CC VPS autonomously (no separate smoke runbook skill needed for PR work).

---

# [Project] — Claude Code PR Prompt: PR-[ID] — [One-line title]

## Autonomy Level: [AUTO | REPORT | AUDIT]

(See the `vps-cc-autonomy` skill / Autonomy Contract for definitions. In short:
- **AUTO**: CC VPS opens PR, waits CI green, auto-merges, auto-runs smoke, reports.
- **REPORT**: CC VPS opens PR, waits CI green, posts report. Operator types `merge` to authorize merge + smoke.
- **AUDIT**: CC VPS opens PR, waits CI green, posts report with PR URL. Operator reviews diff on GitHub, types `merge` after.)

State the level here and one sentence explaining the choice.

## Role

You are a senior Python developer working on [Project], [one-sentence what it is and stakes]. You write clean, tested, production-grade code. You never modify files you weren't asked to modify. You always study existing code patterns before writing new code.

You are verbose in your logging. Format: `[COMPONENT] symbol: action — reason`.

You second-guess your own assumptions. Before writing code, you state what you expect the existing pattern to be, then verify by reading the actual file. You NEVER trust this brief's claims about column semantics, file layout, or code behavior without running a quick verification first — see `VERIFY IN A.X` markers below.

## Context

[3-8 sentences. What exists today. Why this PR. What shipped recently and must remain intact. Exact commit hashes where relevant. Smoking-gun log lines or DB query output that prove the bug exists.]

## 🌍 Environmental Quick-Reference (verified facts — do not re-derive)

These are project-stable facts. Treat as ground truth; only `VERIFY IN A.X` if the brief explicitly says to.

- **VPS**: `tradeflow@5.78.212.37` (Hetzner CX32 / Ubuntu 22.04), repo at `~/tradeflow`
- **Default base**: `origin/main` (NOT local `main` — branch off origin)
- **Compose services vs container_names**:
  - Service `ib-gateway` → container `tradeflow-ib-gateway`
  - Service `app` → container `tradeflow-app`
  - Use SERVICE name for `docker compose` commands; container_name for `docker exec` / `docker logs` / `docker inspect`
- **Pytest**: `/home/tradeflow/tradeflow/.venv/bin/pytest` (host venv; prod container has no test deps)
- **Secrets file**: `/home/tradeflow/.tradeflow-secrets/.env` — operator-only write, CC VPS read-only
- **Repo .env example**: `~/tradeflow/.env.example` — keep in sync with `docker-compose.yml` defaults
- **IBKR paper account**: `DUQ331660`
- **IB connection from bot**: `ib-gateway:4004` (docker DNS) — socat forwards to internal IBKR Gateway 4002
- **Client IDs**: 1=broker, 2=feed, 98=healthcheck, 99=smoke probe (do not collide)

If any of these prove wrong, STOP and report — that's a brief confabulation worth memorializing.

## ⚠️ Harness Denial Reference (do not attempt; will be blocked)

CC VPS's harness denies these verbs. Do NOT include them in your plan; use the workarounds.

| Denied verb | Workaround |
|---|---|
| `git reset --hard <any>` | Don't sync local main. Branch off `origin/main` directly. |
| `git rebase`, `git rebase --onto` | Use cherry-pick onto a fresh branch off origin/main. |
| `git push --force`, `--force-with-lease` | Push new branch with new name; orphan the old branch (harmless). |
| `git push origin main` | Blocked by branch protection. All changes go via PR. |
| `git branch -D <branch>` | Skip cleanup; orphan branches are harmless. |
| `docker exec <c> env` (full env dump) | Use `docker inspect <c> --format '{{range .Config.Env}}{{println .}}{{end}}'` + grep. |
| `sleep <N>` standalone | Use `until <condition>; do sleep 2; done` inside `timeout <max>` wrapper. |
| `cd X && Y` | Use `-C` flag (`git -C ~/path`) or `-f` flag (`docker compose -f ~/path/file`). |
| `;` chained commands | Use separate bash blocks. |
| `$(...)` command substitution | Stage helper to `/tmp/script.py` via Write tool. |
| `${VAR}` parameter expansion in bash | Stage Python helper instead. |
| Heredocs | Stage file content via Write tool. |
| `chained sleep` | Single `until` loop. |

## 🏗️ System Architecture & Recent Learnings

- Container: [name and what runs in it]
- Language: Python 3.x, async where relevant
- Database: [Supabase/Postgres/etc] + access key env var
- Env Vars: [list the ones this PR touches]
- Logging source: `docker logs <container>`; module-level LOGGER

### Key Architecture Constraints

- **Constraint 1 (Runtime)**: [e.g. db.upsert() is SYNC not async]
- **Constraint 2 (Shell)**: Tests run via host venv `/home/tradeflow/tradeflow/.venv/bin/pytest`. Not `docker exec`.
- **Constraint 3 (Schema)**: [relevant unique constraints, nullability, etc]
- **Constraint 4 (Scope boundary)**: [what you explicitly must NOT do]
- **Constraint 5 (Design decision)**: [if a real choice exists, lay out options A/B/C and state the default recommendation with rationale]

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints

Files you WILL modify (EXACTLY N):
- [file 1]
- [file 2]
- [...]

Files you MUST NOT modify:
- [explicit list, including `/home/tradeflow/.tradeflow-secrets/` always]

Verification gates (run before pushing):
- `git diff origin/main -- [protected path 1]` → MUST be empty
- [one line per protected path or group]
- `git diff origin/main --stat` → should show EXACTLY N files changed

### 2. Code Quality
- `black --check [files]` passes
- `ruff check [files]` passes
- No unused imports or variables
- Line length under 100 chars where possible
- Type hints preserved; no signature changes to public methods
- Verbose logging format: `[COMPONENT] symbol: action — reason`
- One import per line (ruff E401)
- Per §0.5.152: `load_dotenv()` never at module level — only in `main()`

### 3. Safety
- All pre-existing tests still pass. Baseline on `origin/main` is **N tests passing** (from prior PR). Run via `/home/tradeflow/tradeflow/.venv/bin/pytest --tb=no -q 2>&1 | tail -10` to confirm.
- Known failing tests (do NOT fix):
  * [test 1]
  * [test 2]
- No unexpected DB writes.
- No unexpected [external API] calls.
- No changes to method signatures in [affected modules].
- If you find a bug adjacent to the fix, DOCUMENT IT in the PR description (Task E). Do NOT fix it. Scope creep is the #1 cause of bad PRs.

## 🧩 Current Mission: [One-sentence objective]

### Objective
[Precise description of what changes and what stays the same.]

### Task A — Audit (BLOCKING; complete before Task B)
[Specific: "Read lines X-Y of file Z. Read neighbors. Answer Q1, Q2, Q3 before writing any code. Write a 3-5 line finding in the PR description."]

A.last: Confirm test baseline:
```bash
/home/tradeflow/tradeflow/.venv/bin/pytest --tb=no -q 2>&1 | tail -10
```
Expected: `<N> passed`. If different, STOP and report.

### Task B — Implement
[Specific change: file, line, old pattern, new pattern. Mirror existing patterns in neighboring code. Show the exact log line format for any new logging.]

### Task C — Add tests
[Exact test shape and names. What they assert. What they mock. Include TEST SAFETY GUARDRAILS — see Pre-Push Checklist below.]

### Task D — Verify completeness
[grep commands to confirm no other site was missed. List expected post-PR state. Every hit must be classified.]

### Task E — Out-of-scope investigation
[~10 minutes on adjacent concern, document findings, do NOT fix.]

### Task F — Merge + Post-merge smoke (executed per Autonomy Level)

The merge + smoke step is now part of the brief, executed by CC VPS autonomously based on this PR's Autonomy Level header.

**For AUTO**: CC VPS runs the full sequence below after CI green, then posts the structured report.
**For REPORT**: CC VPS runs the sequence after operator types `merge` in chat.
**For AUDIT**: CC VPS runs the sequence after operator types `merge` post-GitHub-review.

```bash
gh pr merge <PR-NUM> --squash --delete-branch --repo <owner>/<repo>
```

Confirm merged:
```bash
gh pr view <PR-NUM> --repo <owner>/<repo> --json state,mergeCommit -q '.'
```

Get the merged working tree (avoid local-main sync):
```bash
git -C ~/tradeflow fetch origin
git -C ~/tradeflow checkout origin/main
```

[Add project-specific smoke commands here — recreate container, wait for healthy, probe broker socket, etc.]

Wait for container health (replaces banned standalone sleep):
```bash
timeout 150 bash -c 'until docker inspect <container> --format "{{.State.Health.Status}}" 2>/dev/null | grep -q healthy; do sleep 3; done'
```

[Probe commands — verify the fix landed in the running container.]

Final structured report (CC VPS posts back to operator):
```markdown
# PR-<id> merge + smoke report
- PR merged: <yes/no>, mergeCommit=<oid>
- Test baseline preserved: <yes/no>, <N> passed
- Working tree on: <commit>
- Probe results: <PASS/FAIL per probe>
- Verdict: PASS / FAIL / PARTIAL
- Telegram alerts during smoke: <count + summary>
- Notable observations: <any>
```

## 📤 Expected Output

### Files modified (EXACTLY N)
- [list]

### Git diff stat
[expected line counts per file]

### PR description must include
1. Summary — one sentence
2. Autonomy level — must match the brief's stated level
3. Task A audit — 3-5 line finding
4. Task A.last baseline test count
5. Constraint 5 chosen design — which option, why
6. Task D grep output — full list with classifications
7. Task E finding — one paragraph
8. Local test run — tail of pytest output (from host venv)
9. Full suite run — only documented failures
10. Protected-file diff verification — all empty
11. Smoke test plan — Task F commands (already executed if AUTO; awaiting authorization if REPORT/AUDIT)
12. Explicit scope statement: "This PR does NOT [X, Y, Z]."
13. "What I got wrong during this PR" — 1-3 lines on any assumption that turned out false while auditing or implementing. If nothing, say "nothing".

## 🔍 Pre-Push Checklist

### Code Quality
- [ ] `black --check` passes
- [ ] `ruff check` passes
- [ ] No unused imports
- [ ] No multi-import lines (`import x, y`)
- [ ] No signature changes
- [ ] No `load_dotenv()` at module level

### Tests — TEST SAFETY GUARDRAILS
- [ ] Fresh `MagicMock()` per test (never shared)
- [ ] `mock_db.upsert.return_value = True` set explicitly (or project-specific wrapper equivalent)
- [ ] `mock_db.select.return_value = []` set if code calls select
- [ ] No `side_effect` list without an explicit count comment explaining call ordering (off-by-one StopIteration is the #1 silent failure)
- [ ] No `patch()` on module-level factories; use injection or mock instance attributes
- [ ] Async decorator pattern matches neighboring tests in the same file (verify one neighbor; do not assume `@pytest.mark.asyncio`)
- [ ] Assertions use `call_args_list` filtered by first positional arg, NOT call index
- [ ] Mock at the wrapper level (e.g. `self._db.upsert`), not the raw library chain

### Production Safety
- [ ] Every entry in "Verification gates" shows empty diff
- [ ] Task D grep lists all sites; confirms nothing missed
- [ ] PR description includes Task F smoke plan
- [ ] PR description explicitly states cleanup is NOT in this PR (if true)
- [ ] PR description notes any adjacent bugs found (Task E)
- [ ] PR description includes "What I got wrong" section
- [ ] Autonomy level in PR description matches brief

## ⚠️ Known Gotchas

Carry forward verbatim from the project's latest handoff §0.5. Append new ones discovered during this PR.

[Numbered list of project-specific gotchas. Carry forward the full list from the project's handoff doc every time. New ones get appended each PR.]
