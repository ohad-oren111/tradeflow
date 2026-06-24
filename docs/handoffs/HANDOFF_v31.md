# TradeFlow — Handoff v31 (broker-truth P&L live · MNQ edge CLOSED negative · watchers armed)

## 0. How to use this doc

Read §0.5 (standing rules — permanent), §1 (live state), §4 (the MNQ edge verdict — the
session's headline), §5 (deferred operator items — the ONE migration line you owe + the
on-occurrence watchers), §6 (what I got wrong). Re-verify every quantitative claim against
the broker/DB/logs (§0.5.98), never the dashboard. This continues the v30 autonomous loop;
the Q-B…Q-F items below all shipped.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

**Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block, self-contained, env sourced in the same block, expected output described immediately below, decision tree if more than one branch matters. No "you might want to…" — give the command or don't mention it.

**Learning-delivery discipline.** Every new fact (bug pattern, corrected assumption, environmental fact, diagnostic finding) gets surfaced immediately as a paste-ready markdown snippet for the running handoff queue. Do not wait until end-of-session.

**Read before diagnosing.** For a complex state bug, read the full startup log and 3–5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**Verify severity against the source of truth.** Before escalating urgency ("capital at risk", "spiraling"), hit the live broker/DB/raw log — not aggregated metrics or the dashboard.

**Always draft a VPS smoke-test runbook after PR merge** unless explicitly told otherwise. The owner does not run smoke tests by hand.

**Project standing rules (carried verbatim, cumulative — never shrink):**
- **§0.5.97** — Probe external specs (SeanBot code, third-party behavior) before baking them into TradeFlow. Don't build on a guess about what an external system does.
- **§0.5.98** — Broker/exchange is ground truth. Theorize in chat; **probe on the VPS**. Every quantitative claim re-verified against IBKR/Supabase, never the dashboard.
- **§0.5.197** — Do NOT trust the SeanBot scoreboard column or any "TF leads" headline. The Telegram listener over-captures SeanBot exits ~3.9×; SeanBot P&L from the listener is unreliable. First-party numbers from SeanBot's operator override it (see §0.5.232). *(The dashboard now SURFACES this — Q4a added an untrusted-comparison banner + an honest hedged headline when estimates are involved.)*
- **§0.5.228** — Explicit `--base main` on every PR create.
- **§0.5.230** — The n≥200 gate is the WRONG test for contractual/mechanical arbs (S05-class). Don't fail a real mechanical edge on sample size alone.
- **CC bash discipline** — In VPS CC Bash tool calls: no `cd X &&`, no `;` separators, no `$(...)`, no `${VAR}`, no heredocs. Write Python to `/tmp/` via the Write tool, then `python3 /tmp/x.py`. PR bodies via `--body-file`. Poll `gh pr checks` in a loop (NOT `--watch`). Single `sleep`/Python loop for waits.
- **VPS CC autonomy** — Not using `--dangerously-skip-permissions` (operator opted out; do not suggest it). Broad allows + targeted denies. AUDIT-gate the 6: security-policy mutation, secrets dir, live orders, push-to-main, capital at risk, explicit GATE marker.
- **§0.5.231** — **Stale feed + healthy gateway → check contract/front-month expiry FIRST**, before assuming feed degradation. A bot subscribed to an expired contract presents identically to a degraded bar subscription, but no reconnect/gateway-restart can fix it.
- **§0.5.232** — **First-party operator P&L beats our scraped/estimated figures.** SeanBot's ~+16K all-time is real (confirmed by its operator directly), from a window predating our comparison table. Do not over-apply §0.5.197 skepticism to a better first-party source.
- **§0.5.233 (NEW this session)** — **The MNQ SMA-bounce edge is CLOSED negative.** Three independent honest tests now agree it does not clear the phase16 bar: Phase-18 foundational audit (train PF 1.126), PR#122 exit-sweep walk-forward (OOS PF 1.111), and Q-C exit-GEOMETRY experiment (best variant train PF ~1.13, every variant DSR 0.0). The negative expectancy is **STRUCTURAL — in the ENTRY, not the exit**: cutting losers raises win% but shrinks the avg-winner in lockstep, and letting winners run lowers win% — the R:R/win-rate tradeoff sits on the same break-even locus. **Do NOT re-litigate the MNQ exit geometry.** The next edge work is a DIFFERENT signal (S05 carry — see §4/§0.5.230 — or a SeanBot-code review), not another MNQ stop/trail tweak. (Forward edge in the live regime remains the one thing no backtest settles.)
- **§0.5.234 (NEW this session)** — **The DB's `pnl_net`/`commission_total` are a fixed-rate ESTIMATE, not broker truth.** Broker-true realized P&L now lands additively in `lifecycles.realized_pnl_broker`/`commission_broker` (Q4b/#179), backfilled by the reconciler from `commissionReport` on the next real fill — but ONLY after the operator applies the migration (§5). The kill-switch still consumes the estimate `pnl_net` (unchanged). For money truth, read the broker columns once populated, or IBKR directly (§0.5.98).

---

## 1. Where we are (live state, 2026-06-24 ~20:10 UTC)

- **Deployed commit:** `bbb5b8a` (Q-E). `tradeflow-app` Restart=0, healthy. `ib-gateway` healthy.
- **Broker: FLAT, zero orphans** — `positions/portfolio/open_trades count=0`; reconciler `non_closed=0`.
- **Kill-switch: drawdown brake ARMED** — base $25,000 / 33% / trip $8,250, carrying the **RESTORED** `pnl_epoch=2026-06-23T17:21:02.669737+00:00` (now survived 5 redeploys this session — the #174 persistence is durably proven). Consecutive-loss halt (≥10) also active. **Never INERT this session.**
- **Feed:** live on **MNQU6** (dte ~86), per-minute bars settling; decisions `noop_regime` (30m-EMA200 gate correctly holding the long-only strategy out of the downtrend — working as designed, not a fault).
- **Channel:** quiet — 0 `[ALERT]`/`MANUAL` in healthy op (Q3 signal-only demotion holding; hourly digest is now `[DIGEST]` log-only, the daily `[ALERT] daily_summary` card is the one digest that reaches Telegram).
- **No open PRs after this handoff merges. main in sync.**

---

## 2. What shipped (this session's autonomous loop)

Prior chat-session (context): #174 (pnl_epoch persistence, deployed), #175 (worklog), #176 (Q2 DB-completeness audit + strategy_decisions migration + schema.sql staleness banner), #177 (Q3 signal-only Telegram, deployed), #178 (Q4a+c dashboard reliability+honesty, deployed).

This loop:
- **#179 (Q-B)** — `feat(pnl)`: additive broker-truth realized P&L + commission (reconciler backfill from `commissionReport`, off the order hot path; `pnl_net`/kill-switch untouched). Squash-merged `050bd89`, **deployed + verified** (`broker_pnl_backfilled=0` pre-migration, brake ARMED restored epoch, FLAT).
- **#180** — worklog (gate + Q-B).
- **#181 (Q-D)** — `docs(research)`: S05 cash-and-carry build brief (`docs/research/S05_cash_and_carry_build_brief.md`) — go/no-go for the operator.
- **#182 (Q-E)** — `feat(observability)`: on-occurrence auto-verification watchers (`scripts/occurrence_watchers.py` + reconciler `[WATCH] broker_pnl_semantics` auto-log). Deployed `bbb5b8a`.
- **#183 (Q-C)** — `research(exit)`: MNQ exit-geometry experiment, verdict NONE (`tools/eval/exit_geometry_study.py` + `research/exit_geometry_study_2026-06-23.txt`). No strategy change shipped (correct).

---

## 3. Verification block — run before doing anything (carry verbatim from v30 §6)

```bash
# V0 — deployed commit + health (expect bbb5b8a or later; Restart low/stable)
ssh tradeflow 'cd /home/tradeflow/tradeflow && git rev-parse HEAD && docker inspect tradeflow-app --format "Restart={{.RestartCount}} Status={{.State.Status}}"'
# V1 — FLAT + zero orphans (expect all three count=0; if >0 STOP, reconcile from broker §0.5.98)
ssh tradeflow 'docker logs tradeflow-app --since 8m 2>&1 | grep -E "portfolio — count|positions — count|open_trades — count" | tail -3'
# V2 — brake ARMED, not INERT (expect "drawdown brake ARMED ... trip=$8250"; INERT => #169 not deployed)
ssh tradeflow 'docker logs tradeflow-app --since 90m 2>&1 | grep "KILL]" | grep -iE "ARMED|INERT|restored" | tail -3'
# V3 — front-month + feed (expect MNQU6 + bars within ~60s; expired/empty => §0.5.231, NOT feed degradation)
ssh tradeflow 'docker logs tradeflow-app --since 90m 2>&1 | grep -E "ROLL]|BAR] MNQ" | tail -5'
# V4 — channel quiet (expect 0)
ssh tradeflow 'docker logs tradeflow-app --since 90m 2>&1 | grep -cE "\[ALERT\]|MANUAL INTERVENTION"'
# V5 (NEW) — on-occurrence watchers status (expect all PENDING until each event occurs)
ssh tradeflow 'docker logs tradeflow-app --since 24h 2>&1 | python3 /home/tradeflow/tradeflow/scripts/occurrence_watchers.py -'
```

---

## 4. The edge picture (the money question)

- **MNQ SMA-bounce: CLOSED negative (§0.5.233).** Q-C drove 12 exit-geometry variants through the real strategy+exit over the roll-adjusted 786k-bar tape, scored by the phase16 harness verbatim, plus the owed win/loss-asymmetry → breakeven-win-rate decomposition. **No variant clears.** Baseline: avg winner 53pt vs avg loser 74pt (R:R 0.72) → breakeven 58% wins, actual 63% (gap +4.7pt) — the asymmetry is real but the edge is just too thin (PF 1.18) to survive honest costs + deflation. Geometry can't fix a structural entry. Full table: `research/exit_geometry_study_2026-06-23.txt`.
- **S05 cash-and-carry: the most credible mechanical edge surfaced — operator go/no-go pending.** `docs/research/S05_cash_and_carry_build_brief.md` (Q-D). Real basis-convergence arb, holdout +6.69% median annualized net carry post-spot-ETF, all 5 kill tests pass to 7× costs, rho 1.0, zero losers/6yr. "Failed" the gauntlet only on n<200/DSR (§0.5.230 says don't apply those to a mechanical arb). **Recommendation: GO to Phase-0 (read-only paper basis monitor) — low-risk, high-information.** A real-capital GO is the operator's call, gated on a clean Phase-0 (the backtest labels-ignores live inverse-future liquidation under a basis blowout). NOT built/deployed autonomously (go/no-go, not a fix).
- **Carried, unstarted:** read SeanBot's real code when it arrives (operator requested) and diff against TF; the S06 crypto liq-fade path (gauntlet "strongest OOS", uncertified). Hardening is done; the edge is the point.

---

## 5. Deferred operator items (the things VPS CC cannot do)

1. **Apply the Q4b migration (the ONE line you owe — no DDL creds on the VPS).** In the Supabase SQL editor:
   ```sql
   ALTER TABLE public.lifecycles
     ADD COLUMN IF NOT EXISTS commission_broker   NUMERIC(10, 4),
     ADD COLUMN IF NOT EXISTS realized_pnl_broker NUMERIC(12, 4);
   ```
   (File: `supabase/migrations/20260623200000_lifecycles_broker_pnl.sql`.) Additive, safe, no rush. Until then the dashboard "Broker P&L" column shows `—` and `broker_pnl_backfilled=0` (both expected). **Also apply `supabase/migrations/20260623180000_strategy_decisions.sql`** (Q2/#176) the same way — it back-fills the live `strategy_decisions` table's schema for reproducibility (idempotent; no-op against prod).
2. **On-occurrence watchers are armed; nothing to do — they self-confirm (§0.5.220).** Each fires when its event next occurs; check anytime with V5 above:
   - **broker_pnl_semantics** — discharges the #179 owed live-verification AUTOMATICALLY on the first real round-trip after the migration: the reconciler logs `[WATCH] broker_pnl_semantics: … verdict=<ESTIMATE-FAITHFUL|BROKER-HIGHER|BROKER-LOWER>` comparing IBKR realizedPNL to the estimate. No action needed.
   - **quarterly_roll** — confirms the next real MNQU6→MNQZ6 roll (~2026-09-18) restarts FLAT + restores the epoch + re-seeds warmup.
   - **feed_wedge_escalation** — confirms the next multi-day wedge escalates end-to-end with the expiry hint.
3. **S05 go/no-go** — greenlight Phase-0 (paper basis monitor) if you want to pursue the carry edge (§4).

---

## 6. What I got wrong (this session)

- **Q-C CI went red on first push** — I committed `tools/eval/exit_geometry_study.py` without running black+ruff on it (I'd linted the other files but missed the new one). Caught by the required check, fixed in-scope (line-wraps + `L`→`lines` + dead-var removal; zero logic change), re-merged green. Not a HALT — exactly the "CI red you CAN fix in-scope → fix it, don't page" path.
- **The Q-C hypothesis magnitudes were off in level** (brief guessed ~+40 winner/−82 loser; measured 53/74) and the baseline's actual win% (63%) is ABOVE its breakeven (58%) — so the exit isn't "bleeding"; the entry is simply too thin. Direction of the asymmetry was right; the "fixable by exit" framing was wrong, which is itself the finding.
- **Nothing broker-state went wrong** — FLAT throughout, brake ARMED throughout, epoch restored across all 5 redeploys, no orphans, no kill-switch INERT, no protected-file diffs. No floor was approached.

---

*Continues v30. Standing rules above are cumulative — carry them verbatim into v32.*
