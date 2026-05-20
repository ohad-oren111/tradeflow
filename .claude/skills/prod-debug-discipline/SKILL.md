---
name: prod-debug-discipline
description: Enforce probe-before-patching discipline when debugging production Python/Docker systems with real money at stake (crypto trading bots, futures trading bots, video pipelines). Use whenever the user reports a production bug, asks to diagnose a stuck loop, audio/video misalignment, orphan rows, state transitions failing, orders not executing, or pipeline hanging. Also trigger when the user says "the bot is stuck", "audio cuts off", "orders aren't executing", "the pipeline is failing", "why is X behaving weirdly", or "something's wrong in prod". Load this skill even if the user hasn't explicitly asked for discipline — it forces the diagnose-from-evidence loop (hit the source of truth, read raw logs not greps, don't retrofit wrong hypotheses) that catches root cause the first time. Skip it and you risk shipping a fix to the wrong bug from aggregated grep output.
---

# Production Debugging Discipline

Probe-before-patching loop for production bugs in Python/Docker systems with real money at stake. Goal: make the first diagnosis the correct one. Skip this and you ship a fix to the wrong bug from aggregated grep output.

## The loop — follow in order, do not skip

### Step 1 — Symptom is not cause

Write the symptom verbatim before doing anything else. Do not rephrase in a way that implies a cause.

"Audio cuts at 59s" is a number that may or may not be load-bearing — don't search code for "59" until probes say so. The real cause was often "music track is 59.585s long and `amix=shortest` truncated the output". The 59 was a red herring.

### Step 2 — Hit the source of truth, not aggregated metrics

| System | Source of truth | NOT |
|---|---|---|
| Crypto exchange | `ccxt.fetch_open_orders`, `fetch_balance`, `fetch_my_trades` | place/cancel log counts |
| DB row state | Direct SQL via service role | ORM aggregates, dashboard |
| Video/audio | `ffprobe -v error -show_entries stream=codec_type,duration` | perceived playback |
| Futures broker | IBKR API directly (`ib_insync`) | gateway log lines |
| Container state | `docker ps`, `docker inspect`, `docker stats` | dashboard, monitoring |
| Container logs | `docker logs <c> > /tmp/file.log` then read file | `docker logs ... \| grep ...` |

Why this matters: 2026-04-19, `docker logs | grep` showed doubled lines suggesting two loops running. Raw file showed one. Doubling was a streaming artifact. Same session nearly escalated "SOL is churning real capital through fees" — `fetch_open_orders` confirmed 0 open orders, zero capital impact.

### Step 3 — Read raw logs end-to-end, not greps

Read:

1. **Full startup log** (first 50–200 lines) — "Recovered N rows" type lines reveal whether in-memory state is even correct
2. **3–5 full cycle narratives** top to bottom — patterns hide in narrative, not counts. Smoking-gun lines like `syncing deployment_id X -> Y (DB is source of truth)` only appear when state diverges; grep summaries never surface this.
3. **For staged pipelines**: duration/state logs at every stage boundary. Find the *earliest* stage where delta > 0 — that's where the bug lives, not where the user perceived it.

If the user gives you a single-line summary or grep output, ask for the raw file: "I'd rather read the last 200 lines than the grep output — pattern X often hides in narrative, not counts."

### Step 4 — Separate KNOWN from HYPOTHESIZED

Before proposing any fix, write a summary with two sections:

```
KNOWN (from direct probes):
- [fact — cite source: "ffprobe shows audio_dur=59.54s video_dur=71.43s"]
- [fact — cite source: "Binance fetch_open_orders returned 0 orders for all 15 symbols"]

HYPOTHESIZED (not yet verified):
- [guess — what would confirm/refute it?]
```

Every HYPOTHESIZED needs a cheap verifier. If you can't cheaply verify it, probe it into KNOWN or drop it.

### Step 5 — If your diagnosis doesn't predict the next observation, throw it out

When you propose a root cause, predict what the next observation should look like if you're right. If it doesn't match, the diagnosis is wrong — re-read from scratch. **Do not retrofit.**

Botty AI 2026-04-19 example: diagnosed "stale zombie UNWINDING rows", predicted deletion stops the loop. Loop didn't stop. Correct response: throw out the diagnosis, re-read from scratch. *Not* "escalate to a more complex version of the same theory" — that's retrofitting.

### Step 6 — Rule of twice-wrong

If you've been wrong twice in a session about the same bug, **stop diagnosing.** Summarize what's known vs guessed, write a handoff doc (`session-handoff-writer` skill), and tell the user the next session should resume with fresh context. Thrashing costs credits; fresh context wins.

## Probe commands cheatsheet

Always source env in the same block — env vars do not persist across blocks. No "make sure you've sourced X first" side comments.

### Docker container state
```bash
docker ps -a --filter name=<container> --format 'table {{.Names}}\t{{.Status}}'
docker inspect <container> --format 'RestartPolicy={{.HostConfig.RestartPolicy.Name}} Mem={{.HostConfig.Memory}}'
docker logs <container> --since 10m > /tmp/container.log && wc -l /tmp/container.log
```

Note: `docker ps --format` does NOT support `{{.RestartCount}}`. Use `docker inspect` for that.

### Raw log reading (never grep for diagnosis)
```bash
docker logs <container> > /tmp/logs.txt 2>&1
less /tmp/logs.txt        # or: sed -n '1,200p' /tmp/logs.txt for startup
```

### Supabase direct query via service role
```bash
set -a && source /path/to/.env.production && set +a
python3 << 'EOF'
import os
from supabase import create_client
sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE'])
# Note: un-paginated queries silently cap at 1000 rows. Paginate for full counts.
r = sb.table('<table>').select('*', count='exact').limit(1).execute()
print(f'<table>: {r.count}')
EOF
```

### Binance open orders truth check
```bash
set -a && source /path/to/.env.production && set +a
python3 << 'EOF'
import asyncio, os, ccxt.async_support as ccxt_async
async def main():
    ex = ccxt_async.binance({
        'apiKey': os.environ['BINANCE_API_KEY'],
        'secret': os.environ['BINANCE_API_SECRET'],
        'enableRateLimit': True,
    })
    try:
        for s in ['SOL/USDT', 'BTC/USDT']:
            o = await ex.fetch_open_orders(s)
            print(f'{s}: {len(o)} open')
    finally:
        await ex.close()
asyncio.run(main())
EOF
```

### IBKR open positions truth check (TradeFlow)
```bash
python3 << 'EOF'
from ib_insync import IB
ib = IB()
ib.connect('127.0.0.1', 7497, clientId=99)  # paper port; 4001 for live
try:
    print('Positions:', [(p.contract.symbol, p.position) for p in ib.positions()])
    print('Open orders:', [(o.contract.symbol, o.order.action, o.order.totalQuantity) for o in ib.openTrades()])
finally:
    ib.disconnect()
EOF
```

### Video/audio duration probe
```bash
ffprobe -v error -show_entries stream=codec_type,duration -of default=nw=1 <file.mp4>
# Expect: video_dur ≈ audio_dur. Delta > 0.5s → find earliest divergent pipeline stage.
```

## Source of truth hierarchy

When sources disagree, highest wins:

1. Live API (Binance, IBKR, Gmail) — external state
2. Live database via admin/service role — internal state
3. Raw log files (dumped to disk, read top-to-bottom)
4. Code on the deployed commit (`docker exec <c> cat <f>`)
5. Code on `main` (local checkout)
6. Handoff docs / prior session summaries
7. Aggregated metrics, dashboards, grep counts ← do not trust in isolation

If a handoff says "200 orphan rows" and a direct query says 300, the query wins. Dashboard says CPU OK and `docker stats` says 98%? `docker stats` wins.

## Load-bearing don'ts

- Do not diagnose from `docker logs | grep | wc -l` output
- Do not retrofit a diagnosis when the next observation contradicts it
- Do not continue debugging after being wrong twice in a session — stop and hand off

## "I was wrong" template

Use this verbatim when a hypothesis fails. It forces a clean restart instead of escalating a broken theory.

```markdown
Previous diagnosis: [what I said]
Prediction that would have confirmed it: [X]
Actual observation: [Y]
Therefore: previous diagnosis is wrong. Re-reading from scratch. Not retrofitting.
```

## Examples

**Audio truncation**: User says "audio cuts at 59s." → Don't search code near 59. Probe `ffprobe` on the broken file, get music track duration (likely culprit), trace the duration-probe log trail forward through pipeline stages. Only after KNOWN facts on the table, propose root cause.

**Trading bot loop**: User says "Botty stuck in place/cancel loop on SOL every 90s." → Don't grep for "cancel". Probe `fetch_open_orders('SOL/USDT')` and `fetch_my_trades` for actual capital impact. Dump full logs, read last 3 cycles end-to-end. Look for `syncing deployment_id X -> Y` smoking-gun line. Only after KNOWN facts, propose root cause.

**Bad — jumping to fix**: User says "orders aren't placing." Wrong response: "Let me add a retry loop." Right response: "Three probes first — `fetch_balance` (funds?), `fetch_open_orders` (already open we don't know about?), raw logs last 100 lines (suppressed try/except?). Diagnose from output, not priors."
