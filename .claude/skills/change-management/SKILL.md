---
name: change-management
description: The TradeFlow change-management framework — the loop, the four lanes, the promotion gate, the KPIs, and the cadence that govern every change to the bot. Use whenever you are about to start a piece of work and need to decide WHICH lane it belongs to, whether a change is allowed to PROMOTE to production, or what to measure before/after. Trigger when the user says "what should we work on", "is this ready to ship", "promote this", "which lane", "stabilize vs optimize", or when a change touches order execution / strategy / risk and you need the discipline that prevents the "solve the wrong problem for a week" failure mode (the strategy-research detour while the money leaked through the exit, 2026-06-03).
---

# TradeFlow change-management framework

This skill encodes how change happens on TradeFlow so that work stays ordered,
production stays safe, and we never again spend a week optimizing the wrong layer.
It is the operating discipline behind the autonomy contract (AUTO/REPORT/AUDIT) and
the §0.5 standing rules — not a replacement for them.

## The one rule above all

**Fix the layer that is actually broken, in the right order: STABILIZE before
REPLICATE before MEASURE before OPTIMIZE.** The 2026-06-03 meta-failure was running
five backtest iterations (an OPTIMIZE-lane activity) while the money bled through a
broken exit (a STABILIZE-lane bug). You cannot out-enter a broken exit. When a bot
running a *proven* strategy loses money, suspect execution correctness before
re-deriving the strategy. Diagnose from broker truth > internal state/Telegram >
backtest theory.

## The loop (every change, every lane)

```
OBSERVE → HYPOTHESIZE → BUILD → VERIFY → PROMOTE → MONITOR → (back to OBSERVE)
```

1. **OBSERVE** — from the source of truth. Broker fills/order-events for live
   behavior; `lifecycles` for TF P&L; the dashboard for TF-vs-SeanBot. Read full
   logs / cycle narratives, not `grep | wc -l`. State what you actually see.
2. **HYPOTHESIZE** — one root cause, falsifiable. Write down what evidence would
   refute it. If you cannot name the disconfirming evidence, you do not have a
   hypothesis yet.
3. **BUILD** — the simplest change that tests the hypothesis. One PR, one objective,
   an explicit "files MUST NOT modify" list. No "while I'm here" refactors.
4. **VERIFY** — tests that fail the way prod failed (assert the broker MODIFY is
   issued, not just that status reads "resting"). Weight toward the must-NOT-act
   cases. Confirm from broker truth, never an internal counter.
5. **PROMOTE** — only through the promotion gate below.
6. **MONITOR** — watch the first live instance end-to-end; confirm the fix holds in
   the wild before declaring victory.

## The four lanes

A change belongs to exactly one lane. The lane sets priority and the autonomy tier.

| Lane | Question it answers | Example | Default autonomy |
|---|---|---|---|
| **STABILIZE** | Does the plumbing do what we told it? | OCA-stop modify rejected; naked position; orphan stop | AUDIT (order/broker state) |
| **REPLICATE** | Do we faithfully run the proven rule? | settled-bar eval; SeanBot ratchet ladder; entry band | REPORT/AUDIT |
| **MEASURE** | What is actually happening, in numbers? | dashboard truth; TF-vs-SeanBot; live-vs-backtest | AUTO/REPORT |
| **OPTIMIZE** | Can we improve the edge — in small steps? | parameter sweeps; regime filters; new signals | AUDIT, gated |

**Ordering invariant:** never work a lower lane while a higher lane has an open
defect. OPTIMIZE is shelved until STABILIZE is clean, REPLICATE is faithful, and
MEASURE is trustworthy. Strategy invention stays shelved by default — the job is
run-the-proven-rule + execution correctness + small increments.

## The promotion gate (must ALL hold to ship to prod)

1. **Tests green** and they fail the way prod failed (no mock-passing-on-fiction).
2. **Broker-truth verified** for anything touching positions/orders (not internal
   state, not the dashboard, not Telegram).
3. **Autonomy tier honored** — AUDIT (order/strategy/kill-switch/secrets/
   broker-state/auto-liquidation) means open the PR, post the diff, STOP for an
   operator `merge`. Never self-merge an AUDIT change.
4. **Scope respected** — only the files the brief allowed; adjacent bugs documented,
   not fixed.
5. **Deploy is FLAT-safe** — broker-state-altering deploys (force-recreate restarts
   the bot and un-halts it, §0.5.202) happen while the book is FLAT, and re-halt
   after if the bot must stay parked.
6. **"What I got wrong" written** — every PR ends with it, even if "nothing".

If any item is uncertain → STOP and report, do not confabulate.

## KPIs (what MEASURE tracks)

- **Never-naked rate** — % of live time a futures position has a working GTC
  protective stop (target: 100%; §0.5.T5).
- **Stop integrity** — % of ratchets whose announced level equals the broker-held
  `auxPrice` (the STABILIZE-3 money gate; target: 100%).
- **Exit fidelity** — exit fill vs announced/ratcheted level (no round-trip to base).
- **TF vs SeanBot cumulative** — the dashboard comparison (truth = operator anchors).
- **Entry agreement %** — TF vs SeanBot on a settled-bar replay (PARKED metric until
  his current entry rule is known).
- **Test count + pass rate** — regression ratchet; it only goes up.

## Cadence

- **Per change:** run the loop; clear the promotion gate.
- **Per session:** pre-flight scan (§0.5.165) → read latest handoff → V-block (broker
  truth + deployed commit + code-in-image) → work the highest open lane → publish a
  handoff at end (§0.5.154, non-negotiable).
- **After a few clean live trades:** MEASURE live vs backtest/SeanBot; only then
  consider an OPTIMIZE increment, one small step at a time.

## Anti-patterns this framework exists to kill

- Optimizing the edge while the execution layer is broken (the founding lesson).
- Declaring "fixed/deployed/passing" without broker-truth evidence
  (see the `verification-before-completion` skill).
- A "harmless" detail hiding a load-bearing bug (the single-member OCA group).
- Trusting a redeploy to preserve an in-memory halt (it clears it, §0.5.202).
- Widening the entry gate before the exit is proven over several trades.
