# PR Brief Master Template

Use this structure exactly. Do not invent new sections. Do not drop sections. If a section has no content, write `N/A — no [thing] for this PR`.

---

# [Project] — Claude Code PR Prompt: PR [N] — [One-line title]

## Role
You are a senior Python developer working on [Project], [one-sentence what it is and stakes — e.g. "an autonomous cryptocurrency grid trading bot running LIVE on Binance mainnet with real user capital (~$X)"]. You write clean, tested, production-grade code. You never modify files you weren't asked to modify. You always study existing code patterns before writing new code. You understand this is a production system — bugs cost real money.

You are verbose in your logging. Format: `[COMPONENT] symbol: action — reason`.

You second-guess your own assumptions. Before writing code, you state what you expect the existing pattern to be, then verify by reading the actual file. You NEVER trust prior sessions' claims about column semantics or code behavior without running a quick verification first.

[If relevant: add "The previous session made N wrong diagnoses on this bug before arriving at the root cause — do not repeat the pattern of diagnosing from grep summaries alone."]

## Context
[3-8 sentences. What exists today. Why this PR. What shipped recently and must remain intact. Exact commit hashes where relevant. Smoking-gun log lines or DB query output that prove the bug exists.]

## 🏗️ System Architecture & Recent Learnings
- Container: [name and what runs in it]
- Language: Python 3.x, async where relevant
- Database: [Supabase/Postgres/etc] + access key env var
- Env Vars: [list the ones this PR touches]
- Logging source: `docker logs <container>`; module-level LOGGER

### Key Architecture Constraints
- Constraint 1 (Runtime): [e.g. db.upsert() is SYNC not async]
- Constraint 2 (Shell): [how tests run — e.g. inside container via docker exec]
- Constraint 3 (Schema): [relevant unique constraints, nullability, etc]
- Constraint 4 (Scope boundary): [what you explicitly must NOT do]
- Constraint 5 (Design decision): [if a real choice exists, lay out options A/B/C and state the default recommendation with rationale]

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints
Files you WILL modify (EXACTLY N):
- [file 1]
- [file 2]
- [...]

Files you MUST NOT modify:
- [explicit list]

Verification gates (run before pushing):
- `git diff main -- [protected path 1]` → MUST be empty
- [one line per protected path or group]
- `git diff main --stat` → should show EXACTLY N files changed

### 2. Code Quality
- `black --check [files]` passes
- `ruff check [files]` passes
- No unused imports or variables
- Line length under 100 chars where possible
- Type hints preserved; no signature changes to public methods
- Verbose logging format: `[COMPONENT] symbol: action — reason`
- One import per line (ruff E401)

### 3. Safety
- All pre-existing tests still pass. Known failing (do NOT fix):
  * [test 1]
  * [test 2]
  * [test 3]
- No unexpected DB writes.
- No unexpected [external API] calls.
- No changes to method signatures in [affected modules].
- If you find a bug adjacent to the fix, DOCUMENT IT in the PR description. Do NOT fix it. Scope creep is the #1 cause of bad PRs.

## 🧩 Current Mission: [One-sentence objective]

### Objective
[Precise description of what changes and what stays the same.]

### Task A: Audit
[Specific: "Read lines X-Y of file Z. Read neighbors. Answer Q1, Q2, Q3 before writing any code. Write a 3-5 line finding in the PR description."]

### Task B: Implement
[Specific change: file, line, old pattern, new pattern. Mirror existing patterns in neighboring code. Show the exact log line format for any new logging.]

### Task C: Add tests
[Exact test shape and names. What they assert. What they mock. Include TEST SAFETY GUARDRAILS — see Pre-Push Checklist below.]

### Task D: Verify completeness
[grep commands to confirm no other site was missed. List expected post-PR state. Every hit must be classified.]

### Task E: Out-of-scope investigation
[~10 minutes on adjacent concern, document findings, do NOT fix.]

### Task F: Post-merge smoke test
[Exact bash the owner will run after merge. Every command copy-paste ready. Expected output described immediately below each command. Include a "STOP if X appears" rule.]

## 📤 Expected Output

### Files modified (EXACTLY N)
- [list]

### Git diff stat
[expected line counts per file]

### PR description must include
1. Summary — one sentence
2. Task A audit — 3-5 line finding
3. Task D grep output — full list with classifications
4. Task E finding — one paragraph
5. Local test run — tail of pytest output
6. Full suite run — only documented failures
7. Protected-file diff verification — all empty
8. Smoke test — Task F commands
9. Explicit scope statement: "This PR does NOT [X, Y, Z]."
10. "What I got wrong during this PR" — 1-3 lines on any assumption that turned out false while auditing or implementing. If nothing, say "nothing".

## 🔍 Pre-Push Checklist

### Code Quality
- [ ] `black --check` passes
- [ ] `ruff check` passes
- [ ] No unused imports
- [ ] No multi-import lines (`import x, y`)
- [ ] No signature changes

### Tests — TEST SAFETY GUARDRAILS
- [ ] Fresh `MagicMock()` per test (never shared)
- [ ] `mock_db.upsert.return_value = True` set explicitly (or equivalent for this project's DB wrapper)
- [ ] `mock_db.select.return_value = []` set if code calls select
- [ ] No `side_effect` list without an explicit count comment explaining call ordering (off-by-one StopIteration is the #1 silent failure in this repo)
- [ ] No `patch()` on module-level factories; use injection or mock instance attributes
- [ ] Async decorator pattern matches neighboring tests in the same file (verify one neighbor; do not assume `@pytest.mark.asyncio` is the pattern)
- [ ] Assertions use `call_args_list` filtered by first positional arg, NOT call index, to survive future call reordering
- [ ] Mock at the wrapper level (e.g. `self._db.upsert`), not the raw library chain (e.g. `supabase.table().upsert().execute()`)

### Production Safety
- [ ] Every entry in "Verification gates" shows empty diff
- [ ] Task D grep lists all sites; confirms nothing missed
- [ ] PR description includes smoke test for the owner
- [ ] PR description explicitly states cleanup is NOT in this PR (if true)
- [ ] PR description notes any adjacent bugs found (Task E)
- [ ] PR description includes "What I got wrong" section

## ⚠️ Known Gotchas

[Numbered list of project-specific gotchas. Carry forward the full list from the project's handoff doc every time. New ones get appended each PR. Examples:
1. [Env var name that's easy to get wrong]
2. [Column name / field name that differs from the obvious choice]
3. [Docker restart ≠ rebuild. After merging, owner must build + force-recreate.]
4. [Tests run INSIDE the container — use `docker cp` + `docker exec`]
5. [Pre-existing test failures are known. Do not fix.]
6. [Wrapper SYNC vs async]
7. [Shared schema constraints]
...]
