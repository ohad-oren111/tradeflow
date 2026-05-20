---
name: vps-smoke-test-runbook
description: Generate scoped, copy-paste-ready bash runbooks for VPS Claude Code (CLI on remote VPS) to execute end-to-end after a PR merges. Use whenever a Botty AI / CryptoCast PR has just been merged via Claude Code Web and the user needs to verify it landed in production. Also trigger when the user says "draft a runbook", "VPS CC runbook", "post-merge smoke test", "verify PR N in prod", "smoke test runbook", or "what should VPS Claude Code run". The runbook is executed end-to-end by VPS Claude Code, which produces a structured PASS/FAIL report — the user does not run smoke tests by hand. Skip this skill and the runbook will miss the source-of-truth probes, the deployed-code-vs-main check, or the structured report-back format, and the user has to babysit verification by hand.
---

# VPS Smoke Test Runbook Writer

Generate the bash runbook VPS Claude Code (the CLI agent on the production VPS) executes after a PR merges. Output is a single markdown document the user pastes into VPS Claude Code. Job: confirm the fix landed in the running container, didn't break anything else, and the source-of-truth state matches expected — without the user running anything by hand.

## When to use

- User says "draft a runbook for PR N", "VPS CC runbook", "post-merge smoke test", "verify PR 32 in prod", "smoke test runbook"
- A PR just merged via Claude Code Web and prod verification is the next step
- **Default after every PR merge** unless the user explicitly says skip it. Standing workflow rule.

Do not use for active debugging of a known-broken system — that's `prod-debug-discipline`. This skill is for verifying a *just-merged fix* landed cleanly. Do not use for code edits — VPS Claude Code is verification-only, no `git push` from the VPS.

## How to write one

**Read [runbook_template.md](runbook_template.md) before drafting.** It is the structure VPS Claude Code follows: pre-flight, deployed-code probe, state probes, source-of-truth check, PR-specific behavior log tail, decision tree, structured report-back. Use the exact section headings.

## Inputs you need

Ask only if not clear from context:

1. **Project + container** — which docker compose service? (e.g. `botty-orchestrator` for Botty AI)
2. **PR number, short title, merge commit hash** — for the report-back header and the deployed-code check
3. **Files the PR changed** — to verify the deployed container has the new code, not stale image
4. **What the PR fixes** — the specific behavior to look for in logs (smoking-gun log line if any, e.g. "expect dedup firing logs at startup")
5. **Source-of-truth check** — what live API/DB query confirms the post-PR state? (e.g. Binance `fetch_open_orders` for ALL 15 monitored symbols, Supabase row distribution by status)
6. **Known pre-existing anomalies** — things that are weird before the PR and should NOT trigger a FAIL

If the user just says "runbook for PR 32", reconstruct from conversation context or from the prior PR brief.

## Load-bearing rules

- **Every bash block is self-contained.** Source env in every block (env vars do not persist across blocks — known VPS gotcha). No "make sure you ran X first."
- **Expected output described immediately below each block.** Plus a decision tree if more than one branch matters.
- **Probe deployed code, not `main`.** Use `docker exec <container> cat <file>` or `docker exec <container> python -c "..."` to confirm the running container has the fix. A green diff on `main` does not mean prod has the fix — image rebuild may have been skipped.
- **Source-of-truth check is mandatory.** Every runbook ends with a live API or DB query confirming the world matches the post-PR expected state. Logs lie about external state; APIs and DBs don't.
- **Cover ALL monitored symbols / records, not a sample.** Botty has 15 monitored symbols — check all 15. Sampling has produced false PASSes before.
- **VPS Claude Code is verification-only.** The runbook is read-only except for log dumps to `/tmp/`. If you find yourself writing `git push`, `git commit`, edits to `/home/botty/.botty-secrets/`, or `docker exec <c> sh -c 'echo ... > /app/file'`, stop — that's wrong scope.
- **Structured report-back is mandatory.** End the runbook with a markdown report template VPS Claude Code fills in. Same shape every time so the user scans PASS/FAIL at a glance.
- **STOP-on-fail.** Each major section has a STOP rule. VPS Claude Code halts at the first FAIL and reports — does not press on to "see if other things are OK."

## Examples

**Standard post-merge runbook**: User says "Draft a runbook for PR 32 — `recover_state` dedup fix on Botty. Files: `grid_state.py`, `bear_state.py`. Expect: dedup firing logs at startup, no `SCANNING→CLOSED` transitions, all 15 Binance symbols match expected open-order counts (3 XLM buys, 3 ETH bear sells, others 0)." → Produce the full template filled in. Source-of-truth: Binance `fetch_open_orders` for all 15 symbols + Supabase row counts by status across `grid_deployments` and `bear_deployments`. Behavior tail: count `[recover_state] dedup fired` lines in startup log.

**Generic health snapshot (no specific PR)**: User says "VPS runbook for a Botty health check." → Treat as a runbook with no fix-specific behavior to verify. Pre-flight, container state, Binance open orders for all 15 symbols, Supabase row counts by status, recent error rows. No PR-specific behavior tail. Verdict reduces to "all probes within baseline" / "anomaly found".

**Push back**: User says "Runbook for PR 33 to fix the new bug." → If PR 33 hasn't merged yet, push back: "VPS runbook is post-merge verification. If PR 33 hasn't merged via Claude Code Web yet, the right move is a PR brief (`code-pr-brief` skill), not a runbook. Has the PR merged?"

## Don'ts

- Do not write a runbook that modifies code, secrets, or git state
- Do not skip the source-of-truth check — a runbook that only reads logs is incomplete; logs lie about external state
- Do not paraphrase the structured report format — same shape every time
- Do not sample symbols when "all" is required — Botty's 15 monitored symbols means 15, not 3
