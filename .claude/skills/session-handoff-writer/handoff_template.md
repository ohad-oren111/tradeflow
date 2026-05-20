# Handoff Template — 15 Sections

Use this structure verbatim. Skip no sections. If a section is empty for this session, write `N/A this session — see v<prev>` rather than deleting.

---

# [Project] — Handoff v[N] ([one-line status tag])

*Handoff from end of [date]. [One-sentence system status — e.g. "Orchestrator is currently stopped. Do not restart until PR 32 is merged." or "Pipeline passed verification on job X. Bot running green."]. This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read sections 1–6 first — that's the state-of-the-system as of handoff. Sections 7–13 are reference material. Section 14 is the single-file source of truth to consult when this handoff disagrees with itself or a live observation: `<canonical-file>` on `main` at commit `<hash>` or later.

**Do not trust this doc alone.** Run the verification block in §6 before writing any code. [Include any critical-first-action reminder — e.g. "Confirm the orchestrator is still stopped before taking any action."]

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block. Self-contained commands, chained with `&&` or grouped. Source env vars explicitly in the same block. Expected output described immediately below each block, plus a decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**Learning-delivery discipline.** Every time you learn something new — a bug pattern, a corrected assumption, an environmental fact, a diagnostic finding — surface it immediately in the chat, formatted as a markdown snippet the owner can paste verbatim into the running handoff queue. Do not wait until end-of-session.

**Read before diagnosing.** When debugging a complex state bug, read the full startup log and 3-5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency language ("capital at risk", "churning fees", "spiraling"), hit the source of truth — live API, live DB, raw log file — not aggregated metrics.

**Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise. The owner does not run smoke tests by hand.

[If project-specific: add any project standing rules.]

---

## 1. Where we are (as of handoff, [date] ~[time UTC])

### Live production state
- [Container/service statuses — running, stopped, degraded]
- [External state — open orders, positions, active jobs, portfolio value]
- [Any manual operational overrides — cron disabled, restart policy, etc.]
- [Postmortem or snapshot artifacts saved? Where?]
- [Financial state if relevant — realized P&L, position value, orphan counts]

### What just shipped
- **[PR/commit]** (commit hash, merged as PR #N, HEAD <hash>) — [one-line summary of what changed, where, and the verification that it landed in prod]

### What we discovered this session (not yet in code)
- [Factual finding #1 — with evidence: query output, log line, file:line]
- [Factual finding #2]
- [Any metric change that isn't in prior handoffs]

---

## 2. The session's bug thread (or work thread)

[Narrative. 5-10 numbered steps covering the actual debugging/building arc. Include wrong turns explicitly — they're training data for the next session. Format: "1. Morning: X merged cleanly. 2. First wrong diagnosis: Y. Based on Z evidence. Action taken: deleted W. Result: loop did not stop. Wrong root cause. 3. Second wrong diagnosis: ..."]

The goal of this section: a new session reads it and understands which rabbit holes are closed so it doesn't walk them again.

---

## 3. What the system is actually made of

**Single source of truth:** [Path to a canonical system map file on main at a specific commit hash. If none exists, say "none — this handoff is the best available system doc for now".]

Highlights to save a lookup:
- [N tables / services / pipeline stages]
- [Production-live code paths: list the entry points]
- [Dead/phantom surfaces: list the misleading files/tables that exist but aren't wired in]
- [Any automation gotchas — cron living where? Inert in-container crons? etc.]
- [N open documented bugs referenced by ID — GN-GM]

---

## 4. Verified facts about [columns / schema / API quirks / pipeline order] ([date])

**DO NOT challenge these unless the schema migrates.**

[List of load-bearing facts that prior sessions hit landmines on:
- Column X is always 0, don't sum
- Field Y is always NULL, use Z instead
- API endpoint returns this shape, not that one
- on_conflict target is column A, not id
- Primary table for state is X, secondary table Y is frozen/dead
- Pipeline stage order is A → B → C, never reorder
- etc.]

**New load-bearing fact (this session):** [Any fact discovered this session that overrides or adds to prior handoffs. Include the query output or log line as evidence — not just the claim.]

---

## 5. Wrong diagnoses (if any) — READ BEFORE YOU DEBUG

[If this session made wrong diagnoses before finding root cause, document each with:
- What the diagnosis was
- What evidence led to it
- Why it was wrong
- What the correct diagnosis was

This is the most valuable section for preventing next-session thrashing. Do not skip it if the session had wrong turns.]

**Lesson for next session:** [1-3 sentences on the meta-pattern that produced the wrong diagnosis. e.g. "Every wrong diagnosis this session was made from aggregated greps, not raw log narrative. Read full cycles top to bottom before proposing root cause."]

---

## 6. Verification block — run this before doing anything

[Copy-paste bash blocks. Every single one self-contained. Expected output described immediately below each. Decision tree if output has multiple meaningful branches.]

**V0 — [First thing that must be true, often: confirm the critical system state]**
```bash
<command>
# Expect: ...
# If not: ...
```

**V1 — [Row counts, state, critical metrics]**
```bash
<command>
```
Baseline values as of this handoff: [list them]. Deviations mean X.

**V2 — [Code truth check — is the latest fix actually in the deployed container?]**
```bash
<command>
```

**V3 — [External API state check]**
```bash
<command>
```

[Add V4, V5 as needed. Keep it to 4-6 blocks total — more becomes unread.]

---

## 7. Pending work queue

Priority order depends on V1 state, not on this ordering.

### [PR N / Work item 1 — title]
[Status: blocker / planned / optional. Scope. Design decisions needed. Estimated size.]

### [PR N+1 / Work item 2]
[...]

### [Bugs from the system doc — carry forward by ID]
- **G2** — [short description]. Priority.
- **G3** — [...]

### Uncommitted files / operational debt
- [File or item] — [status]
- [...]

### Operational cleanup eventually
- [Low-priority cleanups, DB migrations, etc.]

---

## 8. Test safety — why we belabor this

[Carry forward the cumulative list of test-mocking failures prior sessions hit:
1. Tests passed against a fictional schema because they mocked the column names
2. side_effect list had wrong count → silent StopIteration → wrong assertions
3. Mocked at raw library chain when code uses a wrapper → tests green, prod broken
4. Shared MagicMock() state leaked between tests
etc.]

Guardrails in the master template (§12) prevent all these. Do not ship tests that skip them.

---

## 9. Pitfalls from prior sessions

[Things the LLM got wrong before and should not be trusted on without verification. Examples from actual handoffs:
- "State machine self-cleared zombies" — wrong, needed manual delete
- "Table X is cosmetic" — wrong, it's load-bearing
- Grep patterns missed writers because only one syntax form was matched
- Underestimated parallel code paths for same operation
- Handoff said "2 stuck rows" when it was 18 — don't trust handoff numbers without re-query]

**Next session rule: if a claim is quantitative, re-verify it. Especially row counts, orphan counts, bug severity, and open-order counts.**

---

## 10. Session discipline lesson ([date])

[If this session had a meta-lesson about how the debugging went — e.g. "LLM was wrong three times in one session. Pattern: jumped to diagnosis before reading enough raw file content." — write it here. This trains the next session's self-awareness.]

**Enforcement rules for next session:**
1. [Concrete rule #1]
2. [Concrete rule #2]
3. [Concrete rule #3]

---

## 11. Logging verbosity — what to demand from any new code

[Standing principles for what "well-logged code" looks like in this project:
- Every upsert logs `[COMPONENT] symbol: action — reason`
- Every state transition logs old → new at INFO
- Every swallowed exception logs the specific error + context
- Retry loops log attempt number and reason
- Async code logs entry AND exit
- Any dedup/select-one-of-many must log which row won and why]

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill for the full template, or copy from handoff v<prev>. It enforces: patch constraints, code quality, test safety guardrails, known gotchas, and the "what I got wrong" post-PR section.

---

## 13. Current PR brief in flight (if any) — hand this to Claude Code as-is

[If the session is ending with a PR brief ready to paste into Claude Code, put it here verbatim. Wrap in a fenced block (use ~~~ if the brief itself contains ``` fences). This saves the next session from regenerating the brief.]

---

## 14. Canonical references (in order of authority)

1. **<canonical system map file>** on `main` at `<commit>` — verified system reality
2. **Source code on main** at `<commit>` — what actually runs
3. **Production DB** queried via admin/service role — truth for row/column data
4. **<External API>** via <library> with <env vars> — truth for external state
5. **Postmortem artifacts** at `<path>` — authoritative for what happened <date>
6. **This handoff (v<N>)** — session context, NOT long-term authority
7. **v<N-1> and earlier handoffs** — historical, ignore any claim that contradicts 1-5

---

## 15. First 15 minutes of the next session

1. Read sections 0.5, 1, 2, 4, 5 of this handoff. [Section X is the single most important to internalize.]
2. SSH in. Run §6 verification block. Confirm: [critical conditions].
3. [First action — usually: do any uncommitted cleanup like committing untracked files]
4. [Second action — usually: hand the in-flight PR brief to Claude Code, or start the next task]
5. [Third action — parallel work while Claude Code runs]
6. [Review + merge + smoke test instructions — invoke the `vps-smoke-test-runbook` skill]

---

## 16. How to publish this handoff

**Path A — VPS Claude Code brief:**

```
You are VPS Claude Code on <project> VPS. Save the following content verbatim
to /home/<user>/<project>/docs/handoffs/HANDOFF_v<N>.md, then:

  cd /home/<user>/<project>
  git add docs/handoffs/HANDOFF_v<N>.md
  git commit -m "docs: add v<N> handoff (<one-line tag>)"
  git push origin main

Confirm the file exists, git log shows the commit, and `git status` is clean.

<paste handoff content>
```

**Path B — Manual fallback (if VPS CC unavailable):**

```bash
scp handoff_v<N>.md <user>@<vps>:/home/<user>/<project>/docs/handoffs/HANDOFF_v<N>.md
ssh <user>@<vps> "cd /home/<user>/<project> && git add docs/handoffs/HANDOFF_v<N>.md && git commit -m 'docs: add v<N> handoff (<one-line tag>)' && git push origin main"
```

The handoff exists only if saved to disk and committed. Until then, treat the chat output as draft.

---

*End of handoff v<N>. Target lifespan: until <concrete condition> and the system is stable for <time>. Then delete and rely on <canonical doc> + whatever v<N+1> captures.*
