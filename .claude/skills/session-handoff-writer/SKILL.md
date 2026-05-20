---
name: session-handoff-writer
description: Write session handoff docs for multi-session engineering projects (crypto trading bots, futures trading bots, video pipelines, any production Python/Docker system the user maintains across chat sessions). Use when the user asks for a "handoff", "handoff doc", "context doc", "continuation doc", "end of session summary", "v20 handoff", "write a handoff for the next session", or "put together a handoff". Also trigger at end-of-session moments when significant work has shipped (PR merged, bug diagnosed, new production fact discovered) and the user will resume in a fresh chat. Skip it and the handoff omits load-bearing sections (verification block, standing rules, "what I got wrong") and the next session wastes credits re-diagnosing things this session already solved.
---

# Session Handoff Writer

Produce a session handoff doc that gets a fresh chat session productive within 15 minutes, without re-diagnosing bugs this session already solved. The doc is a single markdown file the user saves (typically `handoff_v<N>.md` in their project) and pastes into the next chat session alongside any source-of-truth files.

## When to use

- User says "write a handoff", "draft a handoff for v20", "context doc for next session", "put together a handoff", "write a continuation doc"
- Session is wrapping up after meaningful work shipped (PR merged, bug diagnosed, new prod fact)
- User says "we should wrap up", "remind me to write a handoff", or "we're running out of context"

Do not auto-trigger this skill. Wait for the user's cue.

## How to write one

**Read [handoff_template.md](handoff_template.md) before drafting.** It contains the 15-section structure. Use the exact section numbering and headings. Skip no sections — if a section has no content this session, write `N/A this session — see v<prev> for the last version of this info` rather than removing it.

## Inputs you need

Before writing, you need:

1. Project name and one-sentence system description
2. What shipped this session (PR/commit hashes)
3. Current live state (containers running? P&L? open orders? pipeline state?)
4. Pending work (next PR, blocked items, known bugs by ID)
5. Any wrong diagnoses made and corrected this session (explicit)
6. Verification commands the next session should run
7. Version number — what's the prior handoff's N? This is N+1.

If the user just says "write a handoff", reconstruct from conversation context and ask only for gaps. Version number is the most common gap.

## Standing rules — carry these forward verbatim into every handoff's §0.5

These are cumulative across handoffs. New ones get appended. Rules never get deleted unless explicitly retired.

- **Copy-paste instruction style.** Every action recommended must be a self-contained, copy-paste-ready bash block. Source env vars explicitly in the same block. Expected output described immediately below each block. Decision tree if more than one branch matters. No "you might want to..." — give the command or don't mention it.
- **Learning-delivery discipline.** Every new fact discovered (bug pattern, corrected assumption, environmental fact, diagnostic finding) gets surfaced immediately as a markdown snippet for the running handoff queue. Do not wait until end-of-session.
- **Read before diagnosing.** For complex state bugs, read the full startup log and 3-5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.
- **Verify severity against the source of truth.** Before escalating urgency language ("capital at risk", "spiraling", "churning fees"), hit the live API or DB, not aggregated metrics.
- **Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise. (See `vps-smoke-test-runbook` skill.)

## Load-bearing rules

- **Never omit the verification block (§6).** Even on a "nothing happened" session. The next session re-grounds from it.
- **Never paraphrase prior gotchas.** Carry forward verbatim from prior handoffs; append new ones. Known gotchas never shrink.
- **Document wrong turns explicitly (§5).** When this session made wrong diagnoses before finding root cause, write each one down: what the evidence was, why it misled, what the correct diagnosis was. This is the most valuable training data for the next session.

## How to publish the handoff (§16 in the template)

Every handoff ends with §16 "How to publish this handoff" containing two paths: (a) a brief for VPS Claude Code to save the file to `/home/<user>/<project>/docs/handoffs/HANDOFF_v<N>.md` and commit+push to origin/main; (b) a manual scp+commit fallback. Commit message: `docs: add v<N> handoff (<one-line tag>)`. The handoff exists only if saved to disk and committed.

## Examples

**Session ending after a merged PR**: User says "Write me a handoff for v19. Shipped PR 31, diagnosed the duplicate-row bug, stopped the bot, saved postmortem artifacts." → Produce the full 15-section doc. §5 (wrong diagnoses) gets the wrong turns from this session. §13 gets the in-flight next-PR brief if ready, otherwise placeholder.

**Short session, small win**: "Quick handoff — bumped RQ timeout to 3600s, nothing else." → Still produce the full structure. Empty sections say "N/A this session". §6 verification block still runs. §4 adds "RQ job timeout is 3600s, do not reduce" as a new verified fact.

**First handoff for a new project**: "First handoff for TradeFlow — Phase 0 done." → Version is v1. Most sections say "N/A — first session" or give baseline state. §4 lists initial environmental facts (VPS IP, OS, account type). §6 verification is lighter (`docker ps`, ssh connectivity). §14 canonical references thinner.

## Don'ts

- Do not write a narrative-only handoff with no runnable probes
- Do not claim quantitative state without evidence — "300 orphans" needs the query that produced the number
- Do not call the doc "final" — it's a snapshot; §14 ranks it below live probes
