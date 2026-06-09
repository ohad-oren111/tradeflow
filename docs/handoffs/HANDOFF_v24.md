# TradeFlow — Handoff v24 (SeanBot reverse-engineered: same edge — "beat SB" is a leverage dial, not a missing edge)

*Handoff from end of 2026-06-09. Bot is FLAT, regime gate LIVE + armed + blocking, single-position, concurrency OFF — prod behavior UNCHANGED from v23 (this session shipped research tooling only, no prod path touched). This doc captures everything a new chat needs to pick up cleanly.*

---

## 0. How to use this doc

Read sections 1–6 first — that's the state-of-the-system as of handoff. Sections 7–13 are reference material. Section 14 is the authority order to consult when this handoff disagrees with itself or a live observation.

**Do not trust this doc alone.** Run the verification block in §6 before writing any code. **Confirm the bot is still FLAT and the regime gate is enabled+armed+blocking (all three, from the live container — §0.5.215) before taking any action.** No prod change is authorized; the next move is a strategy *decision* by the operator (see §7), not a coded change.

---

## 0.5 Standing rules (permanent — do not remove or shrink from handoff)

**Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block. Self-contained commands, chained with `&&` or grouped. Source env vars explicitly in the same block. Expected output described immediately below each block, plus a decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**Learning-delivery discipline.** Every time you learn something new — a bug pattern, a corrected assumption, an environmental fact, a diagnostic finding — surface it immediately in the chat, formatted as a markdown snippet the owner can paste verbatim into the running handoff queue. Do not wait until end-of-session.

**Read before diagnosing.** When debugging a complex state bug, read the full startup log and 3-5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency language ("capital at risk", "churning fees", "spiraling"), hit the source of truth — live API, live DB, raw log file — not aggregated metrics.

**Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise. The owner does not run smoke tests by hand.

### Project standing rules (TradeFlow / carried from Botty AI — cumulative, verbatim)

- **§0.5.97 — Probe external specs against source before baking into briefs** (broker contracts, exchange fees, schema, library APIs). MNQ spec is §0.5.97-verified: `TICK_SIZE=0.25`, `MULTIPLIER=$2/point`, `COMMISSION_RT=$0.62`, `MARGIN_REQ=$2000` day-trade, CME maintenance ~$3636. Do not re-derive from memory.
- **§0.5.98 — Broker/exchange/DB state is ground truth, not internal DB tables and not the story.** *Extended this session:* this applies to your OWN workflow/git state too — verify "I merged it / it's unbuilt" against `git log` / `gh pr view` / `git ls-files`, not against memory or a stale working copy. (See §5.)
- **§0.5.105 — Comprehensive permission/scope sweeps over iterative patches.**
- **§0.5.215 — A risk gate has three distinct states: enabled, armed, blocking.** All three must be verified from the live container, not inferred from a config flag.
- **§0.5.220 — Never default to "we wait."** When a live test could take weeks/months (e.g. regime-paced trades), proactively flag the long wait upfront and propose faster alternatives (backtest real history, synthetic-scenario harness, live paper round-trips off signals, fault injection). Idle/closed-market windows = build time.
- **§0.5.221 — Durability (NEW this session).** Run CC and any long job INSIDE tmux (session `tf1`). Redirect long Python to a `/tmp/<name>.txt` file (survives an SSH drop). Pull results via `scp` + upload — never tmux scrollback. A dropped laptop must be a non-event. (Three SSH disconnects this session, no tmux, killed background runs on SIGHUP — see §2.)
- **§0.5.222 — A surprising aggregate is a prompt to PROBE THE MECHANISM, not a finding to report (NEW this session).** The N=3 "concurrency loses money / −$6,641" headline was a kill-switch artifact, not an edge result. Probe before reporting a counter-intuitive headline. (See §5.)
- **Two-tier model.** Chat tier = strategy / PR briefs / handoffs. VPS Claude Code = implementation end-to-end. VPS CC NEVER git-pushes prod code on its own outside an AUTO research PR; chat never edits code.
- **VPS CC end-to-end mission model.** Every CC work order: implement → test → ship PR → self-merge AUTO after CI green → deploy → verify from broker/DB truth → report. Batch follow-ups into ONE run. PAUSE only for AUDIT (order / strategy / kill-switch / secrets / broker-state), strategy-param calls, or external blockers. Owner = hands-off PM, single-word approvals.
- **VPS CC bash trigger-pattern discipline.** Never use `$(...)`, `${VAR}`, heredocs, `cd X &&`, or `;` separators in CC bash invocations. Write Python to `/tmp/scriptN.py` via the Write tool; commit messages via `git commit -F /tmp/commitmsg.txt`; PR bodies via `--body-file /tmp/pr_body.md`. (Heredocs ARE fine when the OWNER drives his own Mac shell — e.g. the publish block in §16.)
- **Operator autonomy preference.** Maximally hands-off; default to delegating. Operator explicitly opted OUT of `--dangerously-skip-permissions` — do not suggest it.
- **Docs/handoff publish discipline.** Direct push to main is blocked by branch protection (PR + the "Lint, type-check, and test" check required even for docs). Give the owner the COMPLETE block in ONE shot: branch off origin/main → commit → push → `gh pr create` → POLL `gh pr checks <branch>` in a loop (sleep 15, ~10-min cap, fail-fast on fail/error) until green → `gh pr merge --squash --admin --delete-branch` → resync. NEVER `gh pr checks --watch`. Use `set -uo pipefail` (NOT `-e` — the poll loop returns non-zero while the check is pending).

---

## 1. Where we are (as of handoff, 2026-06-09 ~late UTC)

### Live production state
- Containers (from this session's pre-flight): `tradeflow-app` Up (healthy), `tradeflow-ib-gateway` Up (healthy), `tradeflow-telegram-listener` Up ~10 days.
- Position: **FLAT.** Single-position mode. Concurrency OFF.
- Regime gate (30m EMA200): **LIVE + armed + blocking** (Phase 3 "measure-don't-touch"). Re-verify all three states from the live container per §0.5.215 (V0 below) — do not infer from config.
- Kill-switch: **warn@6 / halt@10 CONSECUTIVE losses**, single-position calibration. **Not halted.** (This calibration is a landmine for any concurrent book — see §4 new fact + §5.)
- Pre-committed go/no-go bar (unchanged): positive expectancy + profit factor >~1.2 over 20–30 qualifying above-trend trades. CME reopens Sunday ~22:00 UTC.
- Prod path UNCHANGED this session. No deploy, no container rebuild.

### What just shipped (this session — ALL additive research tooling in `tools/eval/` + `tests/`, AUTO-merged, NO prod path)
- **Research campaign PRs #120–#125** answering "does the edge make money / can we beat SeanBot (SB)":
  - **#122** — exit sweep + SB-signal probe (`tools/eval/exit_sweep.py`, `sb_exit_probe.py`).
  - **#123** — below-trend-long study (`tools/eval/below_trend_study.py`).
  - **#124** — short-side study (`tools/eval/short_side_study.py`).
  - **#125** — portfolio sizing + drawdown study (`tools/eval/portfolio_study.py` + `tests/test_portfolio_study.py`, 8 tests + README Phase 9). *Note: #125 was already merged when CC re-entered — see §5 wrong-diagnosis #1. Any further local kill-switch-flag edits on branch `claude/portfolio-sizing-study` are research-only and not authorized to prod.*

### What we discovered this session (findings, not prod changes)
- **SB = us.** SB's actual uploaded code confirms his strategy IS TradeFlow's strategy: same SMA50/100 bounce AND the SAME 30m-EMA200 (C1) regime gate TF inherited from him. His own backtest numbers (his `config/settings.py`, `ma_bounce.py` docstrings): **PF 1.06–1.13, WR 32–36%, MaxDD 40–52%, +196% to +261% over 3yr** — a real but THIN, high-variance, high-drawdown edge. This MATCHES TF's backtests; our model of SB was never wrong.
- **The dollar gap is LEVERAGE, not edge.** SB live config: 2 contracts × up to 3 concurrent (`POSITION_TIERS=[(0,2)]`, `MAX_CONTRACTS=2`, `MAX_POSITIONS=3`), trailing exit (SL75, `TRAIL_OFFSET=250`), regime gate ON. TF = 1 contract, single position.
- **SB posts ALL trades (wins AND losses) real-time to Telegram/DB.** Do NOT claim he deletes losers. Owner reports SB ~$4,415 over 10 days.
- **Portfolio study verdict** (26mo NQ, modeled fills, $25k nominal) — see §2 for the table. Headline: single-position Calmar **6.41** > SB-leverage Calmar **4.70**. You already risk-adjusted-beat him; he just runs more size for bigger headline dollars (and bigger holes).

---

## 2. The session's work thread

1. **Mission framing (operator directive):** reverse-engineer SB's *actual* winning strategy from his uploaded code + signals and determine whether/how to beat him — not re-litigate whether an edge exists.
2. **Exit lever exhausted (#122):** no exit config clears PF>1.2 OOS. SB-style faster locks raise win% (~89%) but drop expectancy to ~$6 at the same PF. Exit is not the lever; the thin edge is entry/regime-driven.
3. **Below-trend longs have no robust edge (#123):** the longs the gate BLOCKS show pooled OOS PF ~1.03–1.04; chop-filtered walk-forward is NEGATIVE (−$5.71/trade, PF 0.952); ADX doesn't separate chop from trend. **Gate stands.**
4. **Shorts rejected (#124):** mirrored short entry has no edge; consistent with SB's own `DIRECTION=LONG` ("shorts lose on NQ") and his `short_backtest.py` / `short_filtered.py` rejection.
5. **Portfolio study (#125):** fidelity anchor PASSES byte-for-byte (N=1, M=2, no overlays == `engine.simulate_segment` regime-ON: **n=1479, +$28,729.08, PF 1.174, zero per-trade mismatch**).
6. **First wrong turn — stale working copy.** CC re-entered, read a stale 861-line `portfolio_study.py`, ran a 60k smoke, and briefly treated the mission as unbuilt. **Corrected by §0.5.98 git-ground-truth check** (`git log` / `gh pr view` / `git ls-files`) showing #125 already merged. Stopped before re-creating the PR.
7. **Second wrong turn — "concurrency loses money."** First full run showed N=3 = only 122 trades, **−$6,641** → looked like "concurrency is bad." **Probe (`probe_cooldown.py` / `probe_killswitch.py`) revealed the real mechanism:** the run HALTED in month 1 (2024-04-15) because TF's single-position halt@10 reads 3 correlated stop-outs as 3 consecutive losses. The "concurrency hurts" reading was a **kill-switch artifact**, not an edge finding.
8. **Restructure:** split Part A into **ks-ON** (the trap) vs **ks-OFF** (real leverage profile); ran Part B overlays on the un-halted book so they compete on drawdown control, not halt-dodging. Retuned the two vol overlays (ATR≥40 never fired; signal-bar ATR median 4.0 / p90 8.8 → thresholds p85=7.0 / p90=9.0) so they actually bite (caught by `probe_atr.py`).
9. **Canonical numbers (final report):**

| Config | Trades | WR | Net | PF | MaxDD | Calmar | Halt |
|---|---|---|---|---|---|---|---|
| single N=1, M=2 (ks-on) | ~1438 | — | **+$27,989** | 1.174 | **$4,558** | **6.41** | never |
| SB-EXACT N=3, M=2, t250 (ks-on) | 122 | — | **−$6,641** | 0.632 | $7,205 | — | **2024-04-15 (month 1)** |
| SB-lev N=3, M=2, t250 (ks-OFF) | 4069 | 62.2% | **+$67,434** | 1.147 | **$14,334 (57%)** | **4.70** | never |
| N=2, M=2 (ks-OFF) | 2796 | — | +$47,624 | — | $9,574 | ~4.97 | never |
| N=2, M=2 (ks-on) | 722 | — | +$8,393 | — | $9,156 | Nov-2024 |

- Correlated-concurrency: N=3 MaxDD $14,334 = **3.20× single**, ABOVE the perfectly-correlated ×3 line (~$13,674), nowhere near the √3 diversified line (~$7,757). Concurrency is a drawdown AMPLIFIER, not diversification.
- Rolling-10-day: single best/median/worst **+$5,679 / +$319 / −$2,829**; N=3 lev **+$14,385 / +$587 / −$9,347**.
10. **Part B (overlays, walk-forward, select-by-Calmar OOS, ks-OFF base):** pooled OOS SB-baseline net **$43,803**, MaxDD **$13,743**, Calmar **3.19** (this is the OOS-fold pool, distinct from the full-sample +$67,434). Overlays (vol-scaled, equity-derisk, daily-cap, dyn-conc) each cut drawdown but cut net proportionally — **NONE delivers ≤85% of base MaxDD at ≥90% of base net OOS.** Honest negative: modest overlays do not beat SB's risk-adjusted profile.

**The resolution:** you run the SAME edge more conservatively, and single-position Calmar (6.41) BEATS SB's leveraged Calmar (4.70). "Beat SB" is a leverage DIAL: more dollars via size at monotonically worse Calmar + bigger drawdown + the kill-switch trap; genuinely out-returning him risk-adjusted needs a real edge improvement (the hard frontier — overlays don't do it).

---

## 3. What the system is actually made of

**Single source of truth:** none as a one-file map — this handoff + the source on `main` are the best available. The eval kit lives at `tools/eval/` with phases documented in `tools/eval/README.md` (now through Phase 9).

Highlights:
- **Prod-live code path:** the IBKR live bot (regime gate, single-position entry, trailing exit, kill-switch) — UNCHANGED this session.
- **Research surface (NOT prod):** everything under `tools/eval/` (`engine.py`, `data.py`, `metrics.py`, the phase studies, `portfolio_study.py`) + `tests/test_*_study.py`. Offline, drives NO prod path.
- **Phantom/misleading surfaces to ignore:** `docs/tf_research.zip`, `docs/tf_research_v2.zip`, untracked `research/` artifacts — pre-existing, not load-bearing.
- The real kill-switch module (the one the study imports `evaluate_triggers` from) is the prod control referenced in §4 / §13 — confirm its exact path by audit before touching it.

---

## 4. Verified facts (2026-06-09) — DO NOT challenge unless the code/schema changes

- MNQ spec (§0.5.97): `TICK_SIZE=0.25`, `MULTIPLIER=$2/point`, `COMMISSION_RT=$0.62`, `MARGIN_REQ=$2000` day-trade, CME maint ~$3636. NQ≈MNQ points, $2 mult.
- Friction is applied **PER contract** (so an M-contract book nets exactly M× the per-contract result on the same trades).
- The regime gate buffer must hold ≥202 thirty-minute buckets to compute the 30m EMA200 (why maxlen was raised to 7000 + boot seed extended in #117/#118). The gate fail-OPENS (allows) on short segments that can't fill the buffer — relevant to test fixtures only.
- SB's exact live config = N=3 × M=2, trailing SL75 / TRAIL_OFFSET 250, regime gate ON, `DIRECTION=LONG`.
- **NEW load-bearing fact (this session) — the kill-switch trap.** TF's `warn@6 / halt@10 CONSECUTIVE losses` rule is calibrated for a SINGLE-position book. On a CONCURRENT book, N correlated longs stop together on one down-move = N "consecutive losses" per cluster, so ~3–4 bad clusters trip halt@10. Evidence: `probe_killswitch.py` → SB-exact N=3 halts 2024-04-15 after 122 trades (−$6,641); same sizing with halt disabled runs 4069 trades (+$67,434). **Concurrency cannot go live until the kill-switch is re-calibrated** (per-cluster accounting or a higher threshold). This is an operational gate, separate from the edge.

---

## 5. Wrong diagnoses this session — READ BEFORE YOU DEBUG

**Wrong diagnosis #1 — "the mission is unbuilt."**
- Diagnosis: portfolio study not yet built; rebuild from scratch.
- Evidence that misled: a stale 861-line `portfolio_study.py` working copy + a 60k-bar smoke run against it.
- Why wrong: #125 had ALREADY been merged in a prior context window; the working copy was stale, not absent.
- Correct diagnosis (via §0.5.98 git ground-truth check — `git log` / `gh pr view` / `git ls-files`): tooling shipped; verify, don't rebuild. CC stopped before redundantly re-creating the PR.

**Wrong diagnosis #2 — "concurrency loses money / the edge breaks under N=3."**
- Diagnosis: N=3 nets −$6,641 on 122 trades → concurrency is bad / no edge at size.
- Evidence that misled: the raw aggregate (122 trades, negative net) from the first full run, reported by the kill-switch-ON path.
- Why wrong: it was the KILL-SWITCH halting the book in month 1 (correlated stop clusters = consecutive losses), not the edge failing — only 2,083 of 47,596 signals were even evaluated.
- Correct diagnosis (via `probe_cooldown.py` / `probe_killswitch.py`): split ks-ON (trap, −$6,641) from ks-OFF (real leverage, +$67,434); the edge is intact at size, the drawdown just amplifies and the single-position kill-switch bricks it.

**Lesson for next session:** §0.5.98 + §0.5.222 — every wrong turn this session came from trusting a stored/aggregated artifact (a stale file; a halted-run total) over the live mechanism. Verify git/build state against `git`, and PROBE a surprising aggregate's mechanism before reporting it as a finding.

---

## 6. Verification block — run this before doing anything

> All research tooling this session is additive (no prod deploy, no container rebuild), so the "smoke test" is: gate state is intact + tooling is on `main` + imports + tests + fidelity anchor hold. There is no deployed-container probe because nothing prod-path changed. Run inside `tmux tf1`.

**V0 — Bot is FLAT and the gate is enabled+armed+blocking (§0.5.215, all three from live container)**
```bash
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}"
docker logs --since 2h tradeflow-app 2>&1 | grep -Ei "regime gate (enabled|armed|BLOCKED)|position|FLAT" | tail -20
```
Expect: three containers Up/healthy; recent `regime gate ... ENABLED`/`armed` and at least one `regime gate BLOCKED` eval; no open position. If you see an open position or no gate state, STOP and re-ground before any action.

**V1 — Pre-flight sync + tree clean**
```bash
git -C /home/tradeflow/tradeflow fetch origin && \
git -C /home/tradeflow/tradeflow log --oneline origin/main..main && \
git -C /home/tradeflow/tradeflow log --oneline main..origin/main && \
git -C /home/tradeflow/tradeflow status --short
```
Expect: both `log` ranges empty (local == origin), status clean (or only known untracked `docs/*.zip` / `research/` artifacts). Open PRs: `gh pr list --repo ohad-oren111/tradeflow --state open`.

**V2 — Research tooling is on main + imports clean**
```bash
git -C /home/tradeflow/tradeflow ls-files tools/eval/portfolio_study.py tests/test_portfolio_study.py && \
/home/tradeflow/tradeflow/.venv/bin/python -c "import tools.eval.portfolio_study as p; print('import OK', bool(p.verdict_beats_sb))"
```
Expect: both files listed; `import OK True`.

**V3 — Eval tests + fidelity anchor (the real proof the numbers are trustworthy)**
```bash
cd /home/tradeflow/tradeflow && \
.venv/bin/python -m pytest tests/test_portfolio_study.py tests/test_eval_engine.py tests/test_eval_metrics.py -q
```
Expect: all green (8 portfolio + 28 eval). To re-prove the full-data anchor (slow, optional — tape cached at `/tmp/tf_portfolio_tape.pkl`): `.venv/bin/python -m tools.eval.portfolio_study --validate` → anchor line `n=1479 ... net=$28,729.08 ... PF=1.174` with zero mismatch.

**V4 — Lint clean (CI gate parity)**
```bash
cd /home/tradeflow/tradeflow && \
.venv/bin/black --check tools/eval/ tests/ && .venv/bin/ruff check tools/eval/ tests/
```
Expect: both clean. CI runs `ruff check .` and `black --check .` repo-wide — no ignores.

---

## 7. Pending work queue (priority order — depends on the operator's strategy decision)

The next move is a **decision**, not code. There is no authorized prod change.

1. **DECISION: leverage dial.** (Operator only.)
   - *Want SB's dollars* → leverage to N=2 (the sweet spot: +$47,624, MaxDD $9,574, no halt) or N=3 (+$67,434). **AUDIT** — requires kill-switch re-calibration first (§13 brief), then a forward paper window. Inherits the bigger correlated drawdown.
   - *Want best risk-adjusted* → stay single-position (already running; Calmar 6.41 > SB 4.70).
   - *Want to genuinely out-return him risk-adjusted* → needs a real edge improvement (hard frontier; Part B confirms overlays don't do it).
2. **PR #126 (CONTINGENT / AUDIT) — kill-switch per-cluster accounting.** Ready brief in §13. ONLY paste to CC if the operator picks the concurrency path. Ships behind a flag, default OFF; CC must PAUSE for explicit approval before merge (AUDIT).
3. **Passive watch items (carried from v23):** exit profit-walk on a winner (unproven on the #114 build); gap-reseed re-arm behavior on the next real feed gap. The §13 router stop-id-refresh PR remains HELD (ship condition not yet met).
4. **Go/no-go on the live edge:** the pre-committed bar (PF >~1.2 over 20–30 qualifying above-trend trades) is still the gate for trusting the live forward edge. Zero post-gate qualifying trades yet.

### Uncommitted / operational debt
- Possible local edits on branch `claude/portfolio-sizing-study` (kill-switch-flag research additions). Research-only; reconcile vs main before reusing. Not authorized to prod.
- Untracked `docs/tf_research.zip`, `docs/tf_research_v2.zip`, `research/` — pre-existing artifacts; leave or clean, not load-bearing.

---

## 8. Test safety — why we belabor this

Carry forward the cumulative mocking-trap list (none newly hit this session — the new tests are deterministic synthetic-tape, no DB mocks):
1. Tests passing against a fictional schema because they mocked column names.
2. `side_effect` list with the wrong count → silent `StopIteration` → wrong assertions.
3. `patch()` on a module-level factory when code uses a wrapper → green tests, broken prod.
4. Shared `MagicMock()` state leaking between tests.
5. Async decorator pattern assumed instead of verified against a neighbor.

The `code-pr-brief` Pre-Push Checklist (§12) prevents all these. Do not ship tests that skip them.

---

## 9. Pitfalls from prior sessions

- "I merged it / it's not built" — wrong twice now. Verify git/build state against `git log` / `gh pr view` / `git ls-files`, not memory or a working copy (§5).
- A surprising aggregate is not a finding — probe the mechanism (§0.5.222).
- A config flag being set ≠ a gate blocking. Verify enabled+armed+blocking from the live container (§0.5.215).
- SeanBot's cumulative gap vs TF is partly overstated — only a subset is clean head-to-head overlap; and SB posts losers too (do not claim deletion).
- Background jobs not in tmux die on SSH drop (§0.5.221).

**Next session rule: if a claim is quantitative, re-verify it.** Especially open-position state, gate state, PR-merged state, and any P&L / Calmar / MaxDD number (re-derive from the report, don't trust this handoff's table blind).

---

## 10. Orchestrator (chat-tier) comments — logged this session

- **What I (chat) got wrong / corrected.** I had to guard against over-reading the "N=3 = −$6,641, concurrency loses money" headline as a real edge result; the correction was to push CC to probe the mechanism, which reframed it as a kill-switch artifact (the trap), not an edge finding. This is now codified as §0.5.222. I also relied on a prior-context claim that the portfolio study was in-flight when it was in fact already merged (#125); the §0.5.98 git-ground-truth check corrected it.
- **Lane discipline held.** Zero prod path touched all session. All six PRs (#120–#125) are additive research tooling in `tools/eval/` + `tests/`, AUTO-merged on green CI. Sizing/concurrency were explicitly kept as a SEPARATE AUDIT (not auto-anything) and the kill-switch re-calibration is parked as a CONTINGENT brief, not a queued merge.
- **Honest paper/edge note.** The edge is THIN (PF ~1.1). Every number here is MODELED fills on historical NQ with correlated concurrency — the forward edge in the live regime remains UNPROVEN offline. Single-position already risk-adjusted-beats SB (Calmar 6.41 vs 4.70); leverage buys more gross dollars at worse Calmar plus the kill-switch trap. Do not over-read backtest dollars; the live go/no-go bar (§7.4) is still the real test.
- **Process cost.** Three SSH disconnects (no tmux) cost real time and killed in-flight runs. Fixed going forward by §0.5.221.

**Enforcement rules for next session:**
1. Verify gate state (enabled+armed+blocking) and FLAT from the live container before anything (§0.5.215).
2. Probe the mechanism behind any surprising aggregate before reporting it (§0.5.222).
3. Run CC + long jobs in `tmux tf1`, redirect to `/tmp`, pull via scp (§0.5.221).

---

## 11. Logging verbosity — what to demand from any new code

- Every state transition logs old → new at INFO.
- Every gate evaluation logs the decision + reason (`regime gate BLOCKED — below 30m EMA200`).
- Every kill-switch evaluation logs the consecutive-loss count and the trigger threshold it checked against.
- Every swallowed exception logs the specific error + context.
- Any dedup / select-one-of-many logs which row won and why.
- Format: `[COMPONENT] symbol: action — reason`.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill for the full template (Role / Context / Architecture / Engineering Standards / Mission Tasks A–F / Expected Output / Pre-Push Checklist incl. the 8 test-safety guardrails / Known Gotchas). The §13 brief below is built from it.

---

## 13. Current PR brief in flight — PR #126 (CONTINGENT / AUDIT-GATED — do NOT paste to CC unless the operator picks the concurrency path)

~~~
# TradeFlow — Claude Code PR Prompt: PR #126 — Kill-switch per-cluster consecutive-loss accounting (flagged, default OFF)

## Role
You are a senior Python developer working on TradeFlow, an autonomous MNQ futures bot on IBKR paper (~$1M NetLiq) that will eventually trade real money. You write clean, tested, production-grade code. You never modify files you weren't asked to modify. You study existing patterns before writing. You second-guess your own assumptions: state the expected pattern, then verify by reading the actual file. You NEVER trust prior claims about behavior without a quick verification first.

You are verbose in logging. Format: `[COMPONENT] symbol: action — reason`.

**THIS IS AN AUDIT PR. It touches the kill-switch (a risk control). After CI is green, you DO NOT self-merge — you PAUSE and report, and wait for the operator's explicit single-word approval before merge.**

## Context
A 26-month portfolio study (#125) proved TF's single-position kill-switch (`warn@6 / halt@10 CONSECUTIVE losses`) bricks any concurrent book in month 1: N correlated longs stop together on one down-move = N "consecutive losses", so ~3–4 bad clusters trip halt@10. Evidence: SB-exact N=3 halted 2024-04-15 after 122 trades (−$6,641) vs 4069 trades (+$67,434) with the halt disabled. This PR adds a per-cluster accounting MODE so a correlated stop-cluster counts as ONE loss event — making the kill-switch safe for a future concurrent book — behind a config flag that DEFAULTS to today's exact behavior. This PR changes NO live behavior unless the flag is turned on (a separate operator decision).

## 🏗️ System Architecture & Recent Learnings
- Container: `tradeflow-app` (the live bot). Python 3.x, async where relevant.
- Logging source: `docker logs tradeflow-app`; module-level LOGGER.
- The kill-switch lives in the prod risk module that exposes `evaluate_triggers(...)` (the same function `tools/eval/portfolio_study.py` imports). **Confirm its exact path in Task A.**

### Key Architecture Constraints
- Constraint (Runtime): single live strategy instance; the kill-switch reads the closed-PnL stream newest-first.
- Constraint (Shell): tests run via the repo venv `.venv/bin/python -m pytest`.
- Constraint (Scope boundary): you must NOT change entry, exit, sizing, or concurrency logic — ONLY the kill-switch's loss-counting, and ONLY behind the new flag.
- Constraint (Design): "cluster" = consecutive losing closes whose entries opened within a configurable window (default: same bar / within `cluster_window_bars`, recommend 1). Default flag OFF ⇒ identical to current per-trade counting. Recommend default `kill_switch_cluster_mode=False`.

## 📏 Engineering Standards (Strict)

### 1. Patch Constraints
Files you WILL modify (EXACTLY 2 — confirm exact paths in Task A):
- `<kill_switch module>.py` (the `evaluate_triggers` owner)
- `tests/test_<kill_switch>.py`

Files you MUST NOT modify: entry, exit, sizing, regime-gate, broker, or secrets modules; anything under `tools/eval/`.

Verification gates (run before reporting):
- `git diff main -- <entry/exit/sizing/gate paths>` → MUST be empty
- `git diff main --stat` → EXACTLY 2 files

### 2. Code Quality
- `black --check` and `ruff check` pass. No unused imports. One import per line. Line length <100. Type hints preserved; no public signature changes (add the mode via an optional kwarg/config field, defaulted).
- Logging: `[KILL_SWITCH] : cluster mode — counted N stops in window as 1 loss event`.

### 3. Safety
- All pre-existing tests still pass. Known failing (do NOT fix): none documented — if any are red on main, list them, do not fix.
- No unexpected DB writes, no broker calls.
- Default behavior byte-identical to current (flag OFF). If you find an adjacent bug, DOCUMENT it, do not fix.

## 🧩 Current Mission: make the kill-switch correlation-aware behind a default-OFF flag

### Task A: Audit
Locate the kill-switch module and `evaluate_triggers`. Read it end-to-end + 1 neighbor test. Answer in the PR description: (1) exact file paths; (2) how consecutive losses are currently counted; (3) what entry-timestamp/bar info is available at evaluation time to group a cluster. 3–5 line finding.

### Task B: Implement
Add `kill_switch_cluster_mode: bool = False` and `cluster_window_bars: int = 1` to the config. When ON, collapse consecutive losing closes whose entries fall within `cluster_window_bars` into a single loss event before applying warn@6 / halt@10. When OFF, code path is unchanged. Mirror existing logging.

### Task C: Add tests
- `test_cluster_mode_off_is_identical`: a serial-loss stream yields the SAME halt point as today (regression guard).
- `test_cluster_mode_collapses_correlated_stops`: 3 stops in one window count as 1 loss event ⇒ does NOT halt where per-trade mode would.
- Deterministic, hand-built inputs (mirror `tests/test_portfolio_study.py` style — no DB mocks). Follow the 8 TEST SAFETY GUARDRAILS in the Pre-Push Checklist.

### Task D: Verify completeness
`grep` for every caller of `evaluate_triggers` / consecutive-loss counting; classify each as touched/untouched. Confirm no second counting site was missed.

### Task E: Out-of-scope investigation
~10 min: does the live bot even pass entry-bar info to the kill-switch today? If not, note what plumbing a real concurrent book would need. Document; do NOT build it.

### Task F: Post-merge smoke test (operator runs AFTER approval+merge)
Confirm flag defaults OFF in the running container and current halt behavior is unchanged:
```bash
docker exec tradeflow-app python -c "from <module> import <Config>; c=<Config>(); print('cluster_mode', getattr(c,'kill_switch_cluster_mode', 'MISSING'))"
# Expect: cluster_mode False  (STOP if True or MISSING)
```

## 📤 Expected Output
Files modified (EXACTLY 2); diff stat; PR description with Task A finding, Task D grep + classifications, Task E paragraph, local + full pytest tails, protected-file empty diffs, explicit "This PR does NOT change entry/exit/sizing/concurrency and changes NO live behavior with the flag OFF", and a "What I got wrong during this PR" line (or "nothing").

## 🔍 Pre-Push Checklist
Code quality (black/ruff/no unused/no multi-import/no signature change). Test safety guardrails (fresh MagicMock per test if any; explicit returns; no off-by-one side_effect; no patch() on factories; async pattern matches a neighbor; assertions by call_args not index; mock at wrapper not raw chain). Production safety (verification gates empty; grep complete; smoke test included; scope statement present; adjacent bugs noted; "what I got wrong" present). **AUDIT: do NOT self-merge — PAUSE for operator approval.**

## ⚠️ Known Gotchas
1. MNQ spec is §0.5.97-verified — do not re-derive.
2. The kill-switch reads closed PnL newest-first — confirm ordering in Task A.
3. Default OFF must be byte-identical to today (regression test is mandatory).
4. This is the ONLY safe path to live concurrency — getting it wrong re-introduces the month-1 brick.
5. Pre-existing red tests (if any) are not yours to fix.
~~~

---

## 14. Canonical references (in order of authority)

1. **Live container** (`docker exec`/`docker logs tradeflow-app`) + **IBKR broker state** — truth for gate state, position, and fills (§0.5.98, §0.5.215).
2. **Source code on `main`** — what actually runs. (Confirm HEAD via §6 V1.)
3. **`tools/eval/` + `tests/` on `main`** — research tooling; `tools/eval/README.md` documents Phases 1–9.
4. **SB's uploaded code** (`/mnt/user-data/uploads/seanbot-share.zip`, extracted ref) — truth for SB's actual strategy/config.
5. **This handoff (v24)** — session context, NOT long-term authority.
6. **v23 and earlier handoffs** — historical; ignore any claim contradicting 1–4.

---

## 15. First 15 minutes of the next session

1. Read §0.5, §1, §2, §4, §5, §10 of this handoff. **§5 (wrong diagnoses) + §4 (kill-switch trap) are the most important to internalize.**
2. Reconnect; `tmux attach -t tf1` (or `tmux new -s tf1`). Run §6 V0–V4. Confirm: FLAT, gate enabled+armed+blocking, tooling on main, tests + lint green.
3. Reconcile any leftover local edits on `claude/portfolio-sizing-study` vs main; commit nothing to prod.
4. Surface the §7 decision to the operator: leverage dial (N=2 / N=3 / stay single / pursue edge). Do NOT proceed to code without the operator's pick.
5. If — and only if — the operator picks concurrency: paste the §13 PR #126 brief to CC. It is AUDIT — CC PAUSES for approval before merge.
6. After any merge: draft a `vps-smoke-test-runbook` for it (the §6 block is the template for research tooling; a kill-switch PR gets the deployed-container probe in Task F).

---

## 16. How to publish this handoff

**Path A — operator's one-shot Mac command (preferred; see the chat message that delivered this handoff).** scp the file up, then a single `ssh tradeflow 'bash -s'` heredoc that branches off origin/main, commits `docs: add v24 handoff (...)`, pushes, opens the PR, POLLS `gh pr checks <branch>` to green (sleep 15, ~10-min cap, fail-fast), then `gh pr merge --squash --admin --delete-branch` and resyncs main. `set -uo pipefail`, never `--watch`.

**Path B — VPS Claude Code brief (fallback):**
```
You are VPS Claude Code on the TradeFlow VPS. A file HANDOFF_v24.md has been scp'd to
/home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v24.md. Publish it: branch off origin/main,
git add it, commit -F a /tmp commitmsg file ("docs: add v24 handoff (SeanBot reverse-engineered;
leverage dial, not missing edge)"), push the branch, gh pr create --body-file a /tmp body file,
poll `gh pr checks <branch>` in a loop (sleep 15) until green — NEVER --watch — then
gh pr merge <branch> --squash --admin --delete-branch, checkout main, pull --ff-only.
Report the merged origin/main commit. (CC bash discipline: no heredocs/$()/;/cd && — use /tmp files.)
```

The handoff exists only once saved to disk and committed. Until then, treat the chat output as draft.

---

*End of handoff v24. Target lifespan: until the operator resolves the leverage dial decision (§7) and either (a) the live single-position edge clears its go/no-go bar, or (b) PR #126 ships and a concurrent book passes a forward paper window. Then delete and rely on the source on main + whatever v25 captures.*
