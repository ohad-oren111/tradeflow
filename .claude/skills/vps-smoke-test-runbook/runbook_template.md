# VPS Smoke Test Runbook Template

Use this structure verbatim. Every bash block is self-contained — source env in every block. STOP at the first FAIL and report.

---

# VPS Smoke Test Runbook — PR <N>: <one-line title>

## Role

You are VPS Claude Code running on the <project> VPS (e.g. Botty AI, Hetzner Helsinki, user `botty`). You execute this runbook end-to-end without modifying production code, secrets, or running `git push`. You read state, dump logs to `/tmp/`, and produce a structured report.

You stop and report at the first FAIL or unexpected state. You do not press on to "see if other things are OK". The owner reads the report and decides next steps.

You never edit `/home/<user>/.<project>-secrets/`. You never run `git push`. You never `docker exec <c> sh -c 'echo ... >'` to modify container state. Verification only.

---

## §1 — Pre-flight

Confirm we're on the expected commit and the container is healthy.

```bash
cd /home/<user>/<project>
git rev-parse HEAD
git log -1 --oneline
# Expect: HEAD == <merge-commit-hash from PR>
# If mismatch: STOP. Run `git pull origin main` and re-verify. If still mismatched after pull, report.
```

```bash
docker compose ps
# Expect: <container> "Up <duration>", no Restarting/Exited state
# If Restarting or Exited: STOP. Tail logs (§3) and report.
```

```bash
docker inspect <container> --format 'RestartCount={{.RestartCount}} Status={{.State.Status}} Started={{.State.StartedAt}}'
# Expect: RestartCount = <baseline, typically 0 since deploy>, Status = running
# If RestartCount unexpectedly high: note in report, do not stop yet — §3 will show why.
```

---

## §2 — Deployed-code check

Confirm the merged code is running in the container, not just sitting on `main` waiting for a rebuild.

```bash
docker exec <container> cat <changed_file_path> | sed -n '<line_around_fix>,<line_around_fix+15>p'
# Expect: the file shows the post-PR pattern — e.g. `<unique pattern from the fix>`
# If old code: STOP. Image needs rebuild:
#   docker compose up -d --build --force-recreate <service>
# Report image-not-rebuilt as the verdict; do not run remaining sections.
```

[Add more docker exec cat blocks if multiple files were changed and each fix has a distinct pattern.]

---

## §3 — State probes (container + recent logs)

Dump recent logs to a file and check size/error counts.

```bash
docker logs <container> --since 10m > /tmp/<container>_recent.log 2>&1
wc -l /tmp/<container>_recent.log
# Expect: <baseline range, e.g. 200–600 lines for 10m of normal Botty activity>
# If <50: container may be stalled. STOP and report.
# If >5000: log spam, possible loop. STOP and report tail of file.
```

```bash
grep -ciE 'error|exception|traceback' /tmp/<container>_recent.log
# Expect: <baseline error count — e.g. 0 for a healthy Botty cycle>
# If unexpectedly high: capture sample errors:
grep -iE 'error|exception|traceback' /tmp/<container>_recent.log | head -20
# Note in report; continue to §4 for source-of-truth.
```

---

## §4 — Source-of-truth check (live API / DB)

This is the most important section. Logs can lie. APIs and DBs do not.

### Live external state — <Binance / IBKR / etc>

```bash
set -a && source /home/<user>/.<project>-secrets/.env.production && set +a
python3 << 'EOF'
import asyncio, os, ccxt.async_support as ccxt_async

# ALL monitored symbols, not a sample
SYMBOLS = ['SYM1/USDT', 'SYM2/USDT', '...']  # for Botty: all 15

async def main():
    ex = ccxt_async.binance({
        'apiKey': os.environ['BINANCE_API_KEY'],
        'secret': os.environ['BINANCE_API_SECRET'],
        'enableRateLimit': True,
    })
    try:
        for s in SYMBOLS:
            o = await ex.fetch_open_orders(s)
            sides = [(x['side'], float(x['price']), float(x['amount'])) for x in o]
            print(f'{s}: {len(o)} open — {sides}')
    finally:
        await ex.close()

asyncio.run(main())
EOF
# Expect (PR-specific):
#   <symbol-A>: <expected count> open — <expected sides/prices>
#   <symbol-B>: 0 open
#   ...
# If any symbol deviates: note in report. STOP if a symbol has unexpected SELLs while bot is supposed to be in scan-only mode (capital risk).
```

### Internal state — <Supabase / Postgres>

```bash
set -a && source /home/<user>/.<project>-secrets/.env.production && set +a
python3 << 'EOF'
import os
from supabase import create_client
sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE'])

# Note: un-paginated queries silently cap at 1000 rows. Use range pagination for full counts.
# Example: row distribution by status across grid_deployments
for status in ['SCANNING', 'DEPLOYED', 'UNWINDING', 'CLOSED']:
    r = sb.table('grid_deployments').select('*', count='exact').eq('status', status).limit(1).execute()
    print(f'grid_deployments.status={status}: {r.count}')
# Repeat for bear_deployments, errors, etc as relevant to the PR.
EOF
# Expect (PR-specific):
#   grid_deployments.status=SCANNING: <baseline>
#   grid_deployments.status=DEPLOYED: <baseline>
#   ...
# If counts deviate from baseline by more than <threshold>: note in report.
```

---

## §5 — PR-specific behavior log tail

Look for the fix's smoking-gun log line, or the absence of the bug's smoking-gun log line.

```bash
# Did the fix fire?
grep -c '<smoking-gun-log-line-PR-specific>' /tmp/<container>_recent.log
# Expect: count > 0 (fix is running) — or count == 0 (bug is gone), depending on PR
```

```bash
# Did the bug recur?
grep '<bug-pattern-that-should-NOT-appear>' /tmp/<container>_recent.log | head -10
# Expect: empty output
# If matches: STOP. PR did not fix the bug, or fix has a regression. Report sample matches.
```

[Add more grep blocks for any specific behavior the PR is supposed to enable or suppress.]

---

## §6 — Verdict

Decide one of three:

- **PASS** — pre-flight clean, deployed code matches PR, all probes within expected ranges, source of truth matches expected post-PR state, behavior log tail confirms fix.
- **FAIL** — any probe returns an unexpected state in a way that contradicts the PR's intent, OR source-of-truth shows external state inconsistent with expected post-PR state.
- **INVESTIGATE** — probes return states outside expected ranges but don't clearly indicate breakage. Report raw output for human review.

---

## §7 — Structured report

Produce this report verbatim. Same format every time.

```markdown
# Smoke Test Report — PR <N>: <title>

**Verdict:** PASS / FAIL / INVESTIGATE

## §1 Pre-flight
- HEAD: <hash> (expected <hash>) — <match/mismatch>
- Container status: <Up X minutes / Restarting / Exited>
- RestartCount: <N>

## §2 Deployed-code check
- File: <path>
- Pattern check: <found / NOT FOUND>
- [If multiple files: one line per file]

## §3 State probes
- Recent log size: <N> lines (baseline <range>)
- Error/exception count: <N> (baseline <range>)
- [Sample errors if any: 1-3 lines]

## §4 Source-of-truth
### External (<API>)
- <symbol-A>: <state>
- <symbol-B>: <state>
- [...]
### Internal (<DB>)
- <table>.status=<value>: <count>
- [...]

## §5 Behavior log tail
- Fix firing count (<smoking-gun pattern>): <N>
- Bug recurrence (<bug pattern>): <N matches | none>

## §6 Anomalies / next steps
<Free text. List any unexpected states with raw output. Recommend next action: re-deploy / investigate / accept / rollback.>
```

---

*End of runbook. The owner reads §7 and decides. Do not edit prod state on the basis of this runbook — only the owner does that.*
