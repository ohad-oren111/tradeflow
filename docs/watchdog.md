# TradeFlow External Watchdog

External, host-level health monitor for the TradeFlow stack. Detects IB API death, container restart loops, resource exhaustion, and Supabase outages. Alerts via Telegram independently of the orchestrator. Auto-heals IB Gateway with safety guardrails. Sends a daily affirmative "green" report at 09:00 ET each weekday.

Built to close the 85-hour silent-outage failure mode from Session 9: IB Gateway's API listener died inside the container, the container reported `(healthy)` because its Docker healthcheck only verifies the socat TCP socket, and the orchestrator's own Telegram alerter died with it. The watchdog runs **outside Docker** on the VPS host — it survives container death, Docker daemon issues, and orchestrator restart loops.

## Architecture

```
                 ┌────────────────────────────────────────┐
                 │  Hetzner VPS  (5.78.212.37)            │
                 │                                        │
                 │  ┌──────────────────────────────────┐  │
                 │  │  Docker network: tradeflow-net   │  │
                 │  │                                  │  │
                 │  │  tradeflow-app   tradeflow-      │  │
                 │  │  (orchestrator)  ib-gateway      │  │
                 │  │       │             │ (socat +   │  │
                 │  │       │             │  IB Java)  │  │
                 │  │       └─────────────┘            │  │
                 │  └──────┬─────────────┬─────────────┘  │
                 │         │             │                │
                 │         ▼             ▼                │
                 │   port 8080      127.0.0.1:4002        │
                 │   (dashboard)    (IB API)              │
                 │         ▲             ▲                │
                 │         │             │                │
                 │  ┌──────┴─────────────┴────────────┐   │
                 │  │  WATCHDOG (host venv + cron)    │   │
                 │  │  ~/tradeflow/.watchdog-venv/    │   │
                 │  │  ~/.tradeflow-watchdog/         │   │
                 │  └──────┬──────────────────────────┘   │
                 └─────────┼──────────────────────────────┘
                           │ httpx POST sendMessage
                           ▼
                       Telegram API
                           │
                           ▼
                    Operator phone
```

## Installation

Single command (idempotent — safe to re-run):

```
bash ~/tradeflow/scripts/install_watchdog.sh
```

What this does:

1. Creates `~/.tradeflow-watchdog/` (mode 700) for state + logs.
2. Creates `~/tradeflow/.watchdog-venv/` (separate from the dev `.venv/`) and installs `requirements-watchdog.txt` (`ib-async`, `httpx`, `python-dotenv`).
3. Backs up the current crontab to `~/.tradeflow-watchdog/crontab.bak.<epoch>`.
4. Strips any prior `# tradeflow-watchdog` block from the crontab, then appends the current one.
5. Runs `--mode=self-test` — sends a Telegram message including the script SHA so you can confirm the deployed version.

Expected post-install Telegram: `TradeFlow watchdog self-test from VPS <hostname> at <UTC timestamp>. Script sha256 <hash>.`

## Verifying it works

```
~/tradeflow/.watchdog-venv/bin/python ~/tradeflow/scripts/tradeflow_watchdog.py --mode=self-test
```

To force one monitor cycle and inspect:

```
~/tradeflow/.watchdog-venv/bin/python ~/tradeflow/scripts/tradeflow_watchdog.py --mode=monitor
tail -20 ~/.tradeflow-watchdog/watchdog.log
```

To force a daily report on demand:

```
~/tradeflow/.watchdog-venv/bin/python ~/tradeflow/scripts/tradeflow_watchdog.py --mode=daily-report
```

## Alert types

All alerts are plain-text Telegram messages (no `parse_mode`, per §0.5.143). 15-minute dedup per type — first failure fires immediately, repeats within 15min are suppressed. Recovery transitions ("alert was active, now passing") fire a `RECOVERED:` message and clear state.

| Alert key | Trigger | Example message |
| --- | --- | --- |
| `ib_api_down` | `probe_ib_api` — connect + reqCurrentTime round-trip fails | `ALERT: IB API unreachable. Detail: TimeoutError` |
| `app_exited` | `tradeflow-app` container is not `running` + `healthy` | `ALERT: tradeflow-app container unhealthy. {...}` |
| `app_restart_loop` | RestartCount jumped ≥ 3 since last monitor cycle | `ALERT: tradeflow-app restart loop — 7 restarts since last cycle (count=42)` |
| `ibgw_container_down` | `tradeflow-ib-gateway` container not running + healthy | `ALERT: tradeflow-ib-gateway container unhealthy. {...}` |
| `supabase_unreachable` | httpx GET to `lifecycles?select=*&limit=1` returns non-200 | `ALERT: Supabase unreachable — http 503` |
| `dashboard_unreachable` | httpx GET to `http://127.0.0.1:8080/` returns non-{200,401} or refuses | `ALERT: dashboard unreachable — ConnectError` |
| `disk_usage_high` | `df -P /` or `/var/lib/docker` > 85% | `ALERT: disk usage above 85% — {'/': 91}` |
| `memory_usage_high` | `(MemTotal - MemAvailable) / MemTotal` > 90% | `ALERT: memory usage above 90% — {'used_pct': 93.2, ...}` |
| `manual_intervention_needed` | Auto-heal exhausted (3 attempts in 60 min) — IB still down | `MANUAL INTERVENTION NEEDED: IB API down and auto-heal exhausted (3 attempts in last 60min). Run: docker restart tradeflow-ib-gateway` |
| (no key — informational) | After successful auto-heal | `RECOVERED: IB API reachable after auto-heal — server_version=178 time=...` |

## Daily report fields

The 09:00 ET weekday report message contains:

| Field | Meaning |
| --- | --- |
| `Health: N/M green` | How many of the M probes returned OK |
| Per-probe `OK / FAIL — detail` | One line per probe with status + supporting detail |
| `Lifecycles today` | Count of `lifecycles` rows with `created_at >= today 00:00 UTC` |
| `App restart count` | `tradeflow-app` container's cumulative RestartCount |
| `App last log` | Tail of `docker logs tradeflow-app --tail 1` (truncated to 120 chars) |

Reports always send — no dedup — so you get a confirmed-alive ping each weekday morning.

## Auto-heal behavior

**Scope**: ONLY `ib_api_down` triggers auto-heal. Other probe failures alert but do not attempt restart — restart loops dressed up as auto-heal are worse than no auto-heal.

**Action**: `docker restart tradeflow-ib-gateway`, then sleep 90s, then re-probe IB API.

**Guard**: up to **3 attempts per rolling 60-minute window**. On the 4th attempt within that window, the watchdog STOPS auto-restarting and fires the `manual_intervention_needed` alert instead.

**Override**: to take over manually, just run `docker restart tradeflow-ib-gateway` yourself. The next monitor cycle will detect the recovery and clear the alert state.

**Reset**: auto-heal counter is rolling — entries older than 60 minutes are pruned at each evaluation. So after IB has been stable for >60 minutes, you get 3 fresh attempts again.

## Troubleshooting

**I'm not getting alerts.**
1. `tail -30 ~/.tradeflow-watchdog/watchdog.log` — look for `[WATCHDOG] send_telegram: ...` errors.
2. Most common cause: env var name mismatch. The watchdog reads `TELEGRAM_BOT_TOKEN` + `TELEGRAM_OPERATOR_CHAT_ID` from `~/.tradeflow-secrets/.env`. Confirm both keys exist (the chat-id env is `TELEGRAM_OPERATOR_CHAT_ID`, not the more common `TELEGRAM_CHAT_ID`).
3. Re-run `--mode=self-test` and watch the log live: `tail -f ~/.tradeflow-watchdog/watchdog.log`.

**I got an alert but the bot looks fine.**
1. Note the exact alert key (e.g. `dashboard_unreachable`). Run that probe directly: `~/tradeflow/.watchdog-venv/bin/python ~/tradeflow/scripts/tradeflow_watchdog.py --mode=monitor` and inspect the log.
2. Check if there's a transient (network blip, slow container start). Single-cycle false positives are dedup'd for 15 minutes; if it doesn't repeat, no further alert fires.
3. If the probe seems wrong on the merits (e.g. dashboard threshold too tight), file an issue and adjust the threshold in `scripts/tradeflow_watchdog.py`.

**Auto-heal isn't running.**
1. Confirm the `tradeflow` user is in the `docker` group: `groups tradeflow` (expect `docker` in the list).
2. Check the `auto_heal_history` in state file: `cat ~/.tradeflow-watchdog/state.json` — if it has 3 entries dated within the last 60 minutes, you've hit the cap and need to intervene manually.
3. Look for `[WATCHDOG] auto_heal: ...` lines in the log.

## Uninstalling

Strip cron entries only (preserves venv + state for forensic review):

```
bash ~/tradeflow/scripts/uninstall_watchdog.sh
```

Strip cron entries AND remove `~/tradeflow/.watchdog-venv/` + `~/.tradeflow-watchdog/`:

```
bash ~/tradeflow/scripts/uninstall_watchdog.sh --purge
```

## Known limitations

- **Docker daemon dead** → watchdog can't `docker restart` anything. The IB API probe will still fail-alert via Telegram, but auto-heal is unavailable until the daemon is back up. The OS will normally restart `docker.service` itself; if it doesn't, that's an OS-level concern outside the watchdog's scope.
- **Host network down** → watchdog can't reach Telegram. Failures are still logged to `~/.tradeflow-watchdog/watchdog.log`, but no notifications go out. Once the network is back, the next monitor cycle will fire any pending alerts (dedup state is preserved across cycles).
- **VPS hard down** → no watchdog. Out of scope; that's what Hetzner monitoring + uptime checks are for.
- **Watchdog itself broken** → cron will email the user a "command failed" notice (if mail is configured) and the daily 09:00 ET report won't arrive. Both are "missing-signal" alarms — silence past 09:30 ET is itself a flag worth investigating.
