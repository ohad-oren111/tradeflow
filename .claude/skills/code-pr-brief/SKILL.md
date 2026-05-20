---
name: code-pr-brief
description: Generate scoped, production-grade PR briefs for delegating code changes to an AI coding agent, for Python/Docker projects (grid trading bots, video pipelines, futures trading bots, any prod system with real money at stake). Use whenever the user asks for a "PR brief", "PR template", "PR 32 brief", "hand this to the coding agent", "write a prompt for the agent to fix X", or is about to delegate a code change. Also trigger when the user is describing a specific bug fix or feature in a production Python system and asks how to prompt the agent — even if they don't use the word "brief". The user has a strict master template and expects output that matches it exactly; skip this skill and they'll get a generic PR prompt that misses the test-safety guardrails, file scope constraints, and "what I got wrong" section that prevent scope creep and regressions in their codebase.
---

# Claude Code PR Brief Generator

Generate a complete, ready-to-paste Claude Code PR brief using the master template. Output is a single markdown document the user pastes into Claude Code Web. Missing sections cause scope creep, broken tests, or regressions — that is the whole point of the template.

## When to use

- User says "write a PR brief for X", "draft a Claude Code prompt", "PR 32 brief", "hand this to Claude Code", "I want to delegate this fix"
- User has diagnosed a bug and is about to delegate the implementation
- User references a prior PR ("like PR 30") and wants a similar one

Do not use for inline edits the user is doing themselves. Only use when a change is being **delegated** to Claude Code Web. For post-merge verification on the VPS, use the `vps-smoke-test-runbook` skill instead.

## How to write one

**Read [pr_brief_template.md](pr_brief_template.md) before drafting.** It is the master template. Use the exact section headings; do not invent new sections; do not drop sections. If a section has no content, write `N/A — no [thing] for this PR` rather than skipping.

## Inputs you need before drafting

Ask only if not clear from context:

1. **Project** (Botty AI, CryptoCast, TradeFlow, ...) — gotchas differ between projects
2. **Objective** — one precise sentence. Not "fix the bug" but "make `recover_state` deterministic when multiple non-CLOSED rows exist per symbol"
3. **Root cause** — the actual underlying bug, not the symptom. If not nailed down, push back. See `prod-debug-discipline` skill.
4. **Files allowed** — exact list. "EXACTLY N files."
5. **Files forbidden** — explicit protected list, verified empty diff
6. **Known pre-existing test failures** — the 2-3 tests that fail before this PR and must not be "fixed". Carry forward from prior briefs.

Wrong scope is the #1 cause of bad PRs. Ask, don't guess.

## Load-bearing rules

- **Never shortcut the template.** Each section catches a specific failure mode.
- **One brief, one bug.** If user describes two bugs, write two briefs (PR N and PR N+1). The template assumes one objective.
- **Test safety guardrails (in §🔍 Pre-Push Checklist of the template) are non-negotiable.** Same five mocking traps recur across the user's repos: shared `MagicMock` state leaking, `side_effect` list off-by-one causing silent `StopIteration`, `patch()` on module-level factories instead of injection, mocking the raw DB library chain when code uses a wrapper, async decorator pattern assumption (verify a neighbor before assuming `@pytest.mark.asyncio`).
- **Always include the "What I got wrong" section.** Every brief, even trivial ones. It trains future handoffs to expect honesty about dead ends.
- **Carry forward known gotchas verbatim.** The `⚠️ Known Gotchas` section is cumulative. Never shrink between PRs. Ask the user for the current list, or read the project's handoff doc.
- **Push back if root cause isn't clear.** A brief with "make X work" objectives leads to scope creep.

## Examples

**Correct invocation**: "Write a brief for PR 32 — fix duplicate-deployment bug in Botty. Root cause: `recover_state` iterates per-status and overwrites in-memory state non-deterministically when a symbol has multiple non-CLOSED rows. Files: `src/grid/grid_state.py`, `src/bear/bear_state.py`, two test files. Known red tests: usual three." → Produce the full template filled in. Ask 0-2 clarifying questions only if critical info is missing (e.g. tie-break rule: state priority vs `updated_at`).

**Push back first**: "Write a brief to fix the audio bug in CryptoCast." → "What's the root cause you've diagnosed? A brief with 'make audio not cut off' as the objective leads to scope-creep patches. Have you run `ffprobe` on the broken output and traced divergence stage from the duration logs?" Only draft once root cause is clear.

**New project, no prior handoff**: "Brief for TradeFlow Phase 1, gateway docker setup." → Ask for the one-sentence objective, files in scope, test baseline (probably "no tests yet — bootstrapping"). Fill in the template with sensible defaults for a bootstrap PR. `Known Gotchas` can say "first PR, gotchas TBD".

## Don'ts

- Do not invent project-specific gotchas. Ask or leave placeholders.
- Do not combine multiple bugs into one brief.
- Do not produce a brief without a clear one-sentence objective.
