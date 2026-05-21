---
name: verification-before-completion
description: Enforce evidence-before-claims discipline before declaring any work done, fixed, deployed, or passing on production Python/Docker trading systems and pipelines. Use whenever you are about to say "done", "fixed", "deployed", "passing", "ready to merge", "should work now", or any variant — and whenever an agent (CC Web, VPS CC, subagent) reports success on a task. Also trigger before committing, opening a PR, marking a paper-trading day complete, or claiming a smoke test passed. Load this skill proactively at task end even if the user did not request it. Skip it and you risk claiming "243 deploys" when zero round-trip fills actually executed — the Botty pattern where bot-internal success counts diverged from broker-side reality for months.
---

# Verification Before Completion

Stop claiming work is done from inference. Run the verifier in this message, read the output, then state the result with evidence cited. Trading bots, video pipelines, and infra all have a long history of "the code ran successfully" reports that did not match reality at the source of truth — this skill is the institutional response to that pattern.

## The Iron Law

**NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE**

If you have not run the verifier in this message, you cannot claim the thing passes, deployed, fixed, or is ready. "I ran it earlier" does not count. "The agent said it succeeded" does not count. "It compiled" does not count.

## The gate

Before any success language leaves your mouth:

1. **IDENTIFY**: What command proves this claim against the source of truth?
2. **RUN**: Execute the full command in THIS message (fresh, not from memory)
3. **READ**: Full output. Exit code. Counts. Don't skim.
4. **RECONCILE**: Does the output confirm the claim?
   - YES → state claim WITH the evidence inline
   - NO  → state actual status WITH the evidence inline
5. **ONLY THEN**: open the PR / merge / mark complete / move on

Skip any step and you are lying, not verifying.

## Why this skill exists — the Botty AI lesson

Botty AI shipped 243 grid deploys across 5 strategy configurations. The internal counter said 243. The broker said 0 round-trip fills. Five strategy configurations × ~50 deploys each, and not one full grid cycle closed on Binance. The internal "deploys" metric was load-bearing for months. It was meaningless because nobody verified against `ccxt.fetch_my_trades` after each deploy — the broker is the only source of truth for fills, and we were reading our own log lines.

Standing rule §0.5.98 codifies this: broker/exchange API is ground truth for position, fill history, and capital claims — not internal DB tables, not log counts, not dashboard widgets. This skill is the operational version of that rule applied to every "done" claim, not just trading fills.

## Verification matrix — what proves what

Match the claim to the verifier. Each row is "claim → the only command that proves it":

| Claim | Verifier (run THIS, not anything else) | NOT acceptable |
|---|---|---|
| "Tests pass" | `pytest -q` exit 0 + full count in output | "I just ran them" / partial run / linter passed |
| "Linter clean" | `ruff check .` or project equivalent → 0 errors in output | "I think it's clean" |
| "Build passes" | `docker build -t <name> .` exit 0 in this message | "Linter passed" (linter ≠ compiler) |
| "Fix landed in prod" | `docker exec <c> git rev-parse HEAD` matches `git rev-parse origin/main` AND `docker exec <c> cat <file>` shows new code | `docker logs` doesn't error / restart succeeded |
| "Bug fixed" | Repro steps from original report → run them → original symptom does not occur | "The code looks right now" |
| "Order placed" (TradeFlow) | `ib.openTrades()` and `ib.positions()` via IBKR API show expected contract + qty | bot log says "order placed" / order ID exists in DB |
| "Trade closed" (TradeFlow) | `ib.fills()` shows matching exit fill with realized PnL | state machine transitioned to CLOSED |
| "Grid cycle round-tripped" (Botty class) | `ccxt.fetch_my_trades` shows both buy AND sell fills for the deployment_id timeframe | `deployments` table row exists / status=CLOSED |
| "PR merged" | `gh pr view <N> --json state,mergedAt` shows MERGED + non-null mergedAt | `gh pr merge` returned exit 0 (could be queued) |
| "CI passed" | `gh pr checks <N>` shows all checks SUCCESS | green checkmark on screen (stale) |
| "Smoke test passed" | The runbook's structured report-back block, all probes green | "the container is running" |
| "Subagent / VPS CC reports done" | `git log --oneline origin/<branch>..HEAD` + `git diff <base>..HEAD --stat` shows real changes | agent's summary message |
| "Requirements met" | Re-read the PR brief / handoff requirements → tick each off line-by-line with evidence | "tests pass so we're good" |
| "Paper trading day complete" | IBKR fills export for the day + state machine history reconcile | bot didn't crash today |

## Red flags — stop and run the verifier

You are about to violate the rule if you catch yourself:

- Typing "should", "probably", "seems to", "looks like"
- Typing "Great!", "Perfect!", "All set!", "Done!", "Ready!", "Fixed!" — these are completion claims
- About to commit, push, open a PR, or `gh pr merge` without running tests in this message
- About to mark a TODO complete because the implementer subagent or VPS CC said so
- Trusting a status report from another agent (CC Web, VPS CC, subagent) without diffing
- Re-using a verification from earlier in this session (state changes between runs)
- Tired, end of session, wanting to wrap — "just this once"
- Wording the same claim differently to avoid triggering the rule on yourself

If any of those — **STOP**. Run the verifier. Quote the output. Then make the claim.

## Rationalization rejection table

| Excuse | Reality |
|---|---|
| "I'm confident the fix is right" | Confidence is not evidence. Run the verifier. |
| "I'll verify after I commit" | Verify before. Commits to broken code waste reviewer + CI cycles. |
| "Linter passed, that's enough" | Linter ≠ compiler ≠ tests ≠ broker reality. |
| "The agent reported success" | Diff the actual repo state. Agent reports lie when the agent is confused. |
| "It compiled, so it works" | Compilation is the lowest bar. The Botty bot compiled for months and never round-tripped. |
| "I ran it 10 minutes ago" | State changed. Containers restart. Branches advance. Re-run. |
| "Tests pass on my branch" | Did you run them after the last commit? Right now? Quote the output. |
| "Partial check is fine" | Partial proves the partial. Run the full thing. |
| "Smoke test container is up" | Up ≠ deployed code matches main ≠ behaviour correct. Run the runbook probes. |

## Patterns

### Tests
- ✅ [`pytest -q` ran in this message → "30 passed in 1.42s"] "All 30 tests pass."
- ❌ "Should pass now — I fixed the import."

### Deploy landed in prod
- ✅ [`docker exec tradeflow-bot git rev-parse HEAD` → `7a9c2f1`, matches `origin/main` → `7a9c2f1`] "Deploy landed."
- ❌ `docker compose up -d` returned exit 0 → "deploy done"

### Order actually placed (TradeFlow)
- ✅ [`ib.openTrades()` shows `MNQM6 BUY 2`, `ib.positions()` shows `MNQM6 +2`] "Long entry confirmed at broker."
- ❌ "state machine moved IDLE → ENTERING, log says 'placing order'"

### Subagent / VPS CC delegated work
- ✅ [Diff `git diff origin/main..HEAD` shows expected file changes + test additions + new commits with sane messages] "Implementation matches the brief."
- ❌ "VPS CC reported PR #N merged, marking task done"

### PR merged + CI green
- ✅ [`gh pr view 12 --json state,mergedAt,mergeCommit` + `gh pr checks 12` — all SUCCESS, mergedAt non-null] "PR 12 merged at `<SHA>`."
- ❌ "`gh pr merge --squash` exited 0, must be merged"

## When this skill is the wrong tool

This skill is for the **claim moment** — right before "done" / "fixed" / "passing" / "deployed" leaves the chat.

It is NOT for:

- The diagnosis phase mid-bug → use `prod-debug-discipline` (probe-before-patching)
- The architecture re-evaluation after repeated failures → use `architecture-question-gate`
- Designing the verification suite ahead of time → that's the PR brief's "Acceptance criteria" section, owned by `code-pr-brief`

## The bottom line

Honesty is a hard constraint on this codebase because real money clears or doesn't clear on the other side of the broker API. "I think it's fixed" is functionally a lie if it turns out untrue and someone shipped a position on it. Run the verifier. Quote the output. Then make the claim. No shortcuts.
