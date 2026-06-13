---
name: architecture-question-gate
description: Force an architectural stop-and-question after 3+ failed fixes targeting the same bug family in a production Python/Docker system. Use whenever the same bug or bug family has resisted 3 or more attempted fixes within a session or across sessions, when each fix reveals a new related symptom, when fixes start requiring "massive refactoring" to land, or when the user says "we keep hitting this", "same bug different place", "this keeps coming back", or "I've tried three things already". Load this skill before approving a 4th fix attempt. Skip it and you spend a session escalating a broken theory instead of recognizing the pattern is architectural — the Botty G105/G106 → Path C lesson, where 8+ fixes failed across weeks before identity-coupling was named as the real problem and rebuilt.
---

# Architecture Question Gate

After three failed fixes targeting the same bug family, the fourth fix attempt is almost always wrong. The bug is architectural, not local. This skill is the forced stop that prevents the fifth, sixth, seventh fix from compounding the wrong theory.

## When to invoke

Count fix attempts against the same **bug family**, not the same exact symptom. The family stays open if:

- Each fix lands, then a related symptom appears in a different code path
- Each fix requires touching more files than the last
- Each fix needs new "make X consistent with Y" patches in adjacent components
- The diagnosis keeps mutating: "stale rows" → "wrong ID column" → "ID changes on transition" → ...
- You catch yourself saying "we keep hitting this in different places"

Three of those in a session = invoke this gate. Do not propose Fix #4 until the gate clears.

## The Iron Law

**3+ FIXES FAILED = STOP. QUESTION THE ARCHITECTURE BEFORE FIX #4.**

This is not a soft suggestion. Fix #4 without architectural review is the failure mode this skill exists to prevent.

## Why this skill exists — the Botty G105/G106 lesson

The Botty AI grid trading bot hit a bug family across weeks: deployments would orphan, status transitions would fail, IDs would drift between `grid_deployments` rows and `clientOrderId` strings on Binance. Each fix made local sense:

- "Stale UNWINDING rows" → delete them on startup
- "Status enum desync" → add a backfill migration
- "Two loops running on same symbol" → add a per-symbol lock
- "Order IDs not matching" → re-key on `lifecycle_id`
- "Backfill not idempotent" → add upsert dedup
- "Recovery picks wrong row" → tighten filter on status
- "Deploy ID changes mid-flight" → ...

None of them held. The actual root cause, named in retrospect during the cross-LLM review with Gemini + ChatGPT: **identity was coupled to status.** `UNIQUE(symbol, status)` meant the deployment's primary key changed every time the bot transitioned its state, because the row's identity was its status. Every fix that assumed stable identity was treating a symptom of the wrong schema. The right answer was Path C — rebuild the schema with a stable `lifecycle_id` divorced from status, and migrate the trading core onto it. That decision did not get made until enough fixes had failed to make the architectural problem undeniable.

This skill is the gate that says: that decision should have been made at fix #3 or #4, not fix #7.

## The gate protocol

When fix #3 fails for the same bug family, run this protocol **before** proposing fix #4:

### Step 1 — Write the bug family one-liner

In one sentence, name the family using language that does not assume a specific local cause. Not "stale UNWINDING rows" — that's a hypothesized cause. The family is "deployments lose track of their identity across state transitions" or "fills don't reconcile with internal state."

Write it down. This is the unit of work for the rest of the protocol.

### Step 2 — List every prior fix attempt

For each prior fix:

```
Attempt N:
  Diagnosis: [what we thought was wrong]
  Fix: [what we changed]
  What happened: [why it failed — be honest about whether it landed at all]
  New symptom that appeared: [if any]
```

If the "new symptom" column starts looking like the same problem in a different file — that is the architectural smell.

### Step 3 — Look for the shared assumption

What does every prior fix assume to be true? In Botty's case: every fix assumed `deployment_id` was a stable identifier across the deployment's lifetime. It was not.

Common shared-assumption traps:

- "X is unique" — verify with a direct query: is it actually unique under all transitions?
- "Y doesn't change once set" — does it? Probe the audit log / event table.
- "Z is the source of truth" — when Z disagrees with the broker, which actually wins in code?
- "The pipeline is linear" — is there a race? An async edge? A retry that re-enters?

If you can name the shared assumption, you have probably named the architectural problem. If you can't, move to Step 4.

### Step 4 — Cross-LLM review when stuck

If you've named the shared assumption: skip to Step 5.

If you haven't: this is the moment for the **cross-LLM convergence pattern** used on Botty Path C. Hand the full bug family one-liner + the prior-fix table + the relevant schema/code to Gemini and ChatGPT (or Claude in a fresh session with no prior context). Ask each independently: "What is the architectural problem here, not the local one?" Convergent answers across models are high-signal. Divergent answers are also useful — they reveal the question is under-specified.

This is not "ask another LLM what to do." This is forcing a clean-context diagnosis from a model that doesn't have your retrofitted-theory baggage.

### Step 5 — Decide: keep patching, or rebuild

After Step 3 or Step 4, you have one of three outcomes:

| Outcome | Action |
|---|---|
| Shared assumption identified, fix is local | Propose fix #4 referencing the assumption. The bug family one-liner goes in the PR brief. |
| Shared assumption identified, fix requires schema / contract change | Stop patching. Write a Path C-style rebuild plan. Cross-LLM review the plan before coding. |
| No shared assumption found, family still open | Do not propose fix #4. Write a handoff doc (`session-handoff-writer` skill) summarizing the family + prior fixes. Resume next session. |

The third outcome is the hardest to accept — "we don't know enough to fix it right now" — but it is far cheaper than fix #4 through fix #7.

## Red flags — invoke this skill now

- "Just one more try, I think I see it now" (after 2+ failures on the same family)
- "This time it's different" (it usually isn't)
- "Massive refactoring would fix it but let's just patch X first"
- "Every fix introduces a new edge case in a different file"
- "We keep hitting this — let me just add another check here"
- Diagnosis keeps mutating between attempts on the same family
- Fix list is getting longer and the bug isn't closing

## Rationalization rejection table

| Excuse | Reality |
|---|---|
| "Quick patch now, refactor later" | Quick patches accumulate. Botty had 8+ of them. Refactor never happened until shutdown. |
| "Refactor is too big to justify" | If 4+ fixes have failed, the implicit cost of not refactoring already exceeds the refactor. |
| "I don't have time for a stop-and-think" | You have less time for fix #7. Stop-and-think is the speedup. |
| "This fix is different from the last three" | Different file, same family. The whole point of family-grouping is to catch this. |
| "Cross-LLM review feels like overkill" | $0.50 of Gemini API call is cheaper than a week of failed fixes plus a bot that doesn't fill. |
| "We don't need a handoff, I'll remember" | Across-session retrofitting of a broken theory is the most expensive bug in the catalog. |

## What this skill is NOT

- It is **not** "stop debugging." Active debugging continues — use `prod-debug-discipline` and `verification-before-completion`. This skill fires only when 3+ fixes against one family have already failed.
- It is **not** for unrelated bugs. Three different bugs solved with three different fixes do not invoke this gate.
- It is **not** a vote to rewrite from scratch. Path B (greenfield) was the wrong answer for Botty. Path C (targeted architectural change with trading core preserved) was the right one. Most architectural fixes are narrower than a rewrite.

## The bottom line

The Botty trading bot ran for months on a schema whose identity model was broken. Every fix that assumed the schema was right was a fix to a symptom. The lesson is not "rewrite earlier" — it's **count your fix attempts on the same bug family and stop at 3.** The third failed fix is the signal. The fourth is the trap.
