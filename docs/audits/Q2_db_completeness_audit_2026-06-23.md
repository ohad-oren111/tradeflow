# Q2 — DB completeness audit (2026-06-23)

**Scope:** does the Supabase (Postgres/PostgREST) persistence layer capture what it
should, and can the DB reconstruct what actually happened? Read-only probes against
live Supabase + a code map of every read/write path. No writes, no broker changes.

**Headline:** the DB is *internally consistent with broker truth* (0 orphaned/open
lifecycles while the book is FLAT) — **no HALT condition**. The gaps are about
*reconstruction fidelity* (money columns are approximations, daily history is
log-only) and one **schema-drift** bug (a live table with no repo migration).

Method: `scripts/_probe_supabase.py` auth pattern; probe script kept at
`/tmp/q2_db_audit.py`, raw output at `/tmp/q2_db_audit.json`. Secrets never printed.

---

## Live inventory (probed 2026-06-23 ~17:3xZ)

| Table | exists | rows | newest write | reader in app? |
|---|---|---|---|---|
| `lifecycles` | ✅ | 78 (all CLOSED) | 2026-06-18 12:27Z | yes (recon, kill-switch, daily-summary) |
| `lifecycle_events` | ✅ | 453 | 2026-06-18 12:27Z | no (append-only audit) |
| `halt_acks` | ✅ | **0** | — | yes (halt-clear poll) |
| `seanbot_signals` | ✅ | 208 | 2026-06-11 15:32Z | yes (SB reconciler) |
| `signal_reconciliations` | ✅ | 59 | 2026-06-11 15:31Z | no (write-only) |
| `strategy_decisions` | ✅ | 2558 | 2026-06-18 20:54Z | no (write-only) |

Stale `schema.sql` tables — all correctly **absent** (HTTP 404), confirming they are
dead: `trades`, `positions`, `daily_summary`, `kill_switch_events`, `signals`,
`kill_switch_state`.

`lifecycles` CLOSED exit-reason distribution: **STOP 68 / TARGET 2 / MANUAL 7 / EOD 1**
(= 78). 87% stop-outs — consistent with the known negative-expectancy SMA-bounce
strategy (handoff §2.3); an edge observation, not a completeness gap.

---

## Findings

### F1 — SCHEMA DRIFT: `strategy_decisions` live but has no repo migration  *(FIXED here)*
2558 production rows, written by `src/comparison/decision_journal.py`, but no
`CREATE TABLE` existed under `supabase/migrations/`. A clean rebuild / fresh
environment would lack the table, and the decision-journal flush **swallows errors
fire-and-forget** (`decision_journal.flush` → `except: …(swallowed)`), so the loss
would be **silent**. **Remediation in this PR:** added
`supabase/migrations/20260623180000_strategy_decisions.sql` (idempotent
`IF NOT EXISTS`, columns mirror `decision_to_row()`, `UNIQUE(symbol, decision_ts)` =
the app's ON CONFLICT key). No-op against prod; creates on a fresh DB.

### F2 — `schema.sql` is stale/aspirational and actively misleading  *(FIXED here)*
It documents 5 tables that don't exist and aren't used; the live model is
`lifecycles`/`lifecycle_events`. This is exactly the §0.5.96/§0.5.140 confabulation
hazard. **Remediation in this PR:** prepended a STALE banner pointing to
`supabase/migrations/` as ground truth. (Did not delete it — history/reference.)

### F3 — Money columns are INTERNALLY COMPUTED, not broker-true  *(report-only)*
`pnl_gross`, `pnl_net`, `commission_total` on `lifecycles` are derived from a fixed
`MNQ.commission_rt_usd` constant + entry/exit prices (`router.py` `_pnl_gross`,
`reconciler.py`), never IBKR's actual commission / realized-PnL reports. The DB
**cannot reconstruct true realized P&L** — per §0.5.98, IBKR remains ground truth for
money. Recommend: either (a) stamp broker `commissionReport`/realizedPNL onto the
lifecycle on the exit fill, or (b) explicitly label these columns "estimated" in
schema + any consumer. Not fixed here (touches the order/exit path = AUDIT-class).

### F4 — 1 CLOSED lifecycle with NULL `pnl_net` + `commission_total`  *(known artifact)*
`c1e08647…` (MANUAL exit 2026-06-12, exit_price=29672, pnl/commission NULL) — the
known 06-11/06-12 partial-fill self-flatten incident (foreign-flatten set exit_price
but never computed pnl). Historical, single-row; not systemic. Leave as-is.

### F5 — Daily summary is LOG-ONLY; no `daily_summary` table  *(report-only)*
`orchestrator._maybe_emit_daily_summary` reads closed lifecycles and emits an
`[ALERT] daily_summary` log line but persists nothing. Daily P&L history is not
queryable from the DB. **Relevant to Q3** (the "daily digest" is ephemeral) — if Q3
wants a durable digest, persist it. Flagged for the Q3 design, not fixed here.

### F6 — `halt_acks` empty (0 rows)  *(report-only)*
Every halt-clear to date used the `/tmp/halt_clear` file fallback, never the durable
Supabase path. Operationally fine; means halt history isn't in the DB. No action.

### F7 — Timestamp fidelity on reconciler paths  *(report-only)*
`entry_filled_at` / `exit_filled_at` written by the **reconciler** use `_now_iso()`
(internal clock), not the broker fill time (the router event path does use broker
truth). Minor; affects forensic timing only.

### F8 — Write-only tables + ephemeral local fallback  *(report-only)*
`lifecycle_events`, `signal_reconciliations`, `strategy_decisions` are never read back
by the app (external/forensic only). The `reconciliations.jsonl` local mirror is wiped
on container recreate — but the Supabase table already makes it durable (W-S15.2), so
no action.

### F9 — Freshness gap to 06-18 is EXPECTED, not a stalled writer  *(no action)*
`lifecycles` last 06-18 = no trades since (regime-blocked/below-trend).
`strategy_decisions` only captures `touch_ok is True` bars (entries + band
near-misses); regime-blocked bars leave `touch_ok` None → not captured, so the last
capture (06-18 20:54, a `noop_filter`/`ma_order` near-miss) is correct.
`seanbot_signals` last 06-11 = SB-trigger disabled (#139). Documented so the gap is
not misread as a silent failure.

---

## What I got wrong
Initial probes mis-handled PostgREST status codes — `count=exact` returns **206**
(Partial Content), not 200, and the `newest`/distribution queries omitted the auth
headers (ran unauthenticated → empty), so the first pass reported `count=None`/
`newest=None`. Corrected (accept 200|206; pass headers on every request); the table
above is the corrected read. No finding depended on the buggy first pass.

## Recommendation summary
- **Fixed in this PR:** F1 (migration), F2 (schema.sql banner).
- **Carry to Q3:** F5 (persist the daily digest if durability is wanted).
- **Separate AUDIT-class PR (touches order/exit path):** F3 (broker-true P&L/commission).
- **No action:** F4, F6, F7, F8, F9.
