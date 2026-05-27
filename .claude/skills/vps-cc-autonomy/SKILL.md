# CC VPS Autonomy Contract

Defines the three autonomy levels for delegating PR work to CC VPS, and what CC VPS auto-decides at each level. **Integrate into the `vps-cc-autonomy` skill** if that skill exists; otherwise this is its own skill or a section in `session-handoff-writer`'s handoff template (§17 Autonomy Contract).

## Goal

Maximize operator-hands-off operation while keeping operator judgment in the loop for high-risk changes. The operator's role at each level should be either zero (AUTO) or one word in chat (REPORT) or full review (AUDIT) — never multi-step shell pasting.

## The three levels

### AUTO — CC VPS ships end-to-end without operator approval

**Scope**: changes where regression risk is bounded and cheap to revert.
- Config tweaks (`docker-compose.yml`, `.env.example`, `pyproject.toml` minor)
- Docs (README, handoffs, comments, docstrings)
- Log format changes (no logic change)
- Dependency bumps within patch versions
- Test additions only (no production code change)
- Whitespace, lint, type-hint additions

**CC VPS behavior**:
1. Implement PR per brief
2. Open PR via `gh pr create`
3. Wait for CI green via `gh pr checks --watch`
4. **Auto-merge**: `gh pr merge --squash --delete-branch`
5. Auto-run Task F smoke test
6. Post structured report to operator
7. STOP

**Operator role**: Read the structured report. That's it. No "merge" instruction needed.

### REPORT — CC VPS prepares everything; operator types one word

**Scope**: bug fixes and small features touching ≤5 files with strong test coverage.
- Bug fixes touching ≤3 files in well-tested modules
- Refactors with no public-API change
- Single-feature isolated changes
- Connection/networking code (reconnect, retry, backoff)
- Notification/alerting code
- Reconciler tick logic (read-only state changes)

**CC VPS behavior**:
1. Implement PR per brief
2. Open PR via `gh pr create`
3. Wait for CI green
4. Post structured report including: files changed, test counts, PR URL, smoke-test commands ready to run
5. STOP (do NOT merge)

**Operator role**: Read report. Type `merge` to authorize merge + smoke, or `stop` to halt. After "merge", CC VPS runs `gh pr merge`, then Task F smoke, then posts a second structured report. No operator shell pasting.

### AUDIT — Operator reviews PR diff on GitHub before authorizing

**Scope**: anything where a regression could cost money, leak secrets, or wedge production.
- Order execution code (`src/execution/`, anything that places/cancels orders)
- Strategy logic (`src/strategy/`)
- Kill switch logic
- IBKR authentication / credentials handling
- Secret handling (rotation, redaction, .env touches)
- Multi-file changes >50 lines net
- Anything that changes broker-state-altering behavior

**CC VPS behavior**:
1. Implement PR per brief
2. Open PR via `gh pr create`
3. Wait for CI green
4. Post structured report with PR URL emphasized
5. STOP

**Operator role**: Open PR on GitHub, review diff, read PR description, comment / approve / request changes. Type `merge` in chat after approving. CC VPS proceeds to merge + smoke + report.

## How the brief author picks the level

Chat-side me (the brief author) picks the level when writing each brief. The level appears in a `## Autonomy Level: <LEVEL>` header right after `## Role` in every PR brief.

**Default for ambiguous cases**: REPORT. When in doubt, ask the operator to type one word rather than auto-merging or requiring full audit.

## Pre-conditions every level depends on

CC VPS executes a pre-flight scan at session start (or before any PR work):

```bash
git -C ~/tradeflow fetch origin
git -C ~/tradeflow log --oneline origin/main..main
git -C ~/tradeflow log --oneline main..origin/main
gh pr list --repo ohad-oren111/tradeflow --state open
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}"
```

Reports:
- Local-vs-origin divergence (if any) — flag cosmetic vs blocking
- Open PRs (so new work doesn't collide)
- Container state — healthy / flapping / down

If local main is divergent from origin/main, CC VPS does NOT attempt to sync via `git reset --hard` (denied by harness). Instead, ALL new PR branches are created off `origin/main` directly:

```bash
git -C ~/tradeflow checkout -b claude/<branch> origin/main
```

## Telegram kill switch (escape hatch)

At any autonomy level, operator can halt CC VPS by sending the literal string `STOP` to the Telegram alerter bot or chat. CC VPS polls for kill signals before each major action (open PR, merge PR, recreate container). If STOP is detected, CC VPS halts immediately and posts last-known state.

(Implementation detail: a future PR adds Telegram polling to CC VPS's loop. Until then, operator types `stop` in chat as the kill signal.)

## Structured report format (mandatory at every level)

Every CC VPS report-back uses this exact markdown shape so operator can scan in seconds:

```markdown
# PR-<id> <action> report

**Action**: <PR-opened / merged / smoke-passed / smoke-failed>
**PR URL**: <url>
**CI status**: <green/red/pending>
**Files changed**: <N>, +<adds>/-<dels>
**Test count delta**: <baseline>+<new>=<total> passed
**Autonomy level**: <AUTO/REPORT/AUDIT>
**Next step**: <auto-merging now / awaiting operator "merge" / awaiting operator audit on GitHub>

## Brief summary
<one paragraph>

## Notable findings (Task E, anything unexpected)
- <item or "none">

## Smoke test status (if applicable)
<output of Task F or "not yet run">

## What I got wrong during this PR
<1-3 lines or "nothing">
```

## When to deviate from the contract

CC VPS escalates to operator (regardless of level) when:
1. **Task A audit surfaces a brief confabulation** (e.g. the .env override discovery in PR-B). Auto-proceeding with the brief's wrong assumption would corrupt the fix.
2. **Harness denies a planned step** and the workaround isn't obvious. CC VPS reports the denial + options.
3. **Pre-existing test count differs from baseline.** Could indicate the baseline drifted.
4. **CI red on a change CC VPS expected to be CI-green** (e.g. an unrelated test now fails). Don't auto-fix the unrelated test.
5. **A scope-boundary trip** — CC VPS finds the brief's "Files you WILL modify" doesn't actually cover the fix. Don't expand scope unilaterally.

The contract is "operator approves judgment calls, CC VPS executes mechanics" — not "CC VPS does everything autonomously."
