# Runbook — kill switch trip & restart

**What actually happens (verified against `src/execution/kill_switch.py`, 2026-06-03):**
the kill switch does NOT exit the process. On a hard threshold
(`KILL_SWITCH_HALT_CONSEC_LOSSES`, default 10, consecutive losses; or realized
drawdown ≥ `KILL_SWITCH_MAX_DRAWDOWN_PCT`, default 33%, of
`KILL_SWITCH_ALLOCATION_USD` measured from `KILL_SWITCH_PNL_EPOCH`) it:
1. **raises the global halt** (blocks new entries), and
2. **flattens any open position** via the safe cancel + market-exit path.

It then **stays halted until a MANUAL operator reset** — it never auto-resumes. A
6–9-loss streak only NOTIFIES once (Telegram); it does not pause. Loss count and P&L
come from `lifecycles` (broker/DB truth), not an in-memory counter.

> ⚠️ The §0.5.T4 / §0.5.T5 doc note about "exit code 42 + systemd
> `RestartPreventExitStatus=42`" does NOT describe this Docker-compose deployment
> (`restart: unless-stopped`, no systemd unit). Trust this runbook for the live
> mechanism. (Tracked as doc drift in `docs/ROADMAP.md`.)

## 1. Confirm the trip (broker truth first)

```bash
docker logs tradeflow-app --since 2026-06-03T00:00:00 2>&1 | grep -iE "kill_switch_tripped|halt_raised|flatten" | tail -20
```
Then confirm the book is actually FLAT and there are no resting orders (the read-only
clientId-97 probe — `reqAllOpenOrders` across clients):
```bash
/home/tradeflow/tradeflow/.venv/bin/python /tmp/tf_broker_truth.py
```
Expect: FLAT, 0 resting orders. If a position or stop remains, resolve that BEFORE
clearing the halt.

## 2. Decide

- **Tripped on a real loss/drawdown streak** → leave it halted. Investigate the trades
  in `lifecycles` before re-enabling. Do not clear the halt just to keep trading.
- **Tripped on an evaluator error fail-safe** (transient Supabase/network) → safe to
  clear once the cause is gone and the book is FLAT.

## 3. Clear the halt (operator reset — pick ONE)

The reconciler polls Supabase first, then the file flag. Either clears it:

**A — Supabase `halt_acks` row (primary).** Insert an ack newer than the halt's
`raised_at`; the reconciler clears on its next poll (~30s). (Use the existing operator
tooling / Telegram command that writes this row; the table is the canonical path.)

**B — file flag (fallback when the network/table is unavailable):**
```bash
touch /tmp/halt_clear
```
The reconciler reads the file mtime; if it is newer than `raised_at`, it clears the
halt. Confirm:
```bash
docker logs tradeflow-app --since 2026-06-03T00:00:00 2>&1 | grep -iE "halt_acked|clear_halt" | tail -5
```

## 4. If you redeploy instead of clearing (IMPORTANT — §0.5.202)

A `docker compose up -d --force-recreate tradeflow-app` restarts the process and the
halt is **in-memory only**, so a redeploy SILENTLY UN-HALTS the bot if the account is
flat (no foreign position to re-trigger it). Consequences:
- If you want the bot to KEEP trading: a redeploy is sufficient (no ack needed).
- If you want it to STAY PARKED after a deploy: **re-halt explicitly after the deploy**,
  or do not redeploy.

## 5. Verify back to normal

```bash
docker ps --filter name=tradeflow --format "table {{.Names}}\t{{.Status}}"
docker logs tradeflow-app --since 2026-06-03T00:00:00 2>&1 | grep -iE "halt|ARMED|kill_switch" | tail -15
```
Expect healthy containers, the halt cleared, and (if intended) the bot ARMED. Re-run
the §1 broker-truth probe to confirm FLAT + no orphan orders before walking away.
