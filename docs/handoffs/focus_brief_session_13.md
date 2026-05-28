# TradeFlow — Session 13 Kickoff Focus Brief

*Paste this at the start of the next chat session. Sets context, current state, priorities, and how this session operates. The full `HANDOFF_v11.md` is in `docs/handoffs/HANDOFF_v11.md` on origin/main (and `CLAUDE.md` at repo root auto-loads for CC VPS).*

---

## Who I am and what we're building

I'm Ohad, independent engineer. I operate **TradeFlow** — an autonomous MNQ futures trading bot in paper mode on IBKR via `ib_async`, running on a Hetzner CX32 VPS (`tradeflow@5.78.212.37`). SeanBot V3-aligned pullback strategy (100-bar SMA bounce, long-only, 2 contracts, 24/5 CME hours, IBKR paper account `DUQ331660`, live account `U17545037`).

**My role**: PM/orchestrator. I delegate everything possible to AI. I don't paste shell commands. I don't run smoke tests by hand. I want to be **as hands-off as possible**.

**Two-tier AI architecture**:
- **Chat-side Claude (you)**: strategy, RESOLVE-mode work orders, PR briefs, handoffs, decision-making, cross-session synthesis
- **CC VPS** (Claude Code running on the VPS): all execution — git, docker, gh CLI, tests, deploys, smokes, and end-to-end RESOLVE-mode runs

## The operating contract (updated v11 — RESOLVE mode is now default)

Every PR brief or work order carries `## Mode: <LEVEL>`. Four levels:

| Mode | Scope | My role |
|---|---|---|
| **AUTO** | Docs, config, tests-only, additive observability | Zero. Read post-merge report. |
| **REPORT** | Bug fixes ≤5 files with strong test coverage | Type `merge` (or `stop`) after CI green. |
| **AUDIT** | Order execution, strategy, kill switch, secrets, multi-file >50 LOC, subscription lifecycle | Scan PR diff on GitHub (~2-5 min), type `merge`. |
| **RESOLVE** (NEW v11) | Multi-step ops/diagnostic work end-to-end (probe → fix → verify) | Zero, except for genuinely operator-only blockers (IBKR portal, Telegram-identity auth, Supabase dashboard SQL paste). Each external block surfaces as `⛔ NEEDS OPERATOR: <exact action>`. |

**Default for multi-step ops/diagnostic work: RESOLVE.** Chat-side authors the GOAL + decision tree; CC VPS runs to ✅ or to a labeled operator block. Don't bounce decisions back to me that CC VPS can evaluate from probes. The full RESOLVE definition is in HANDOFF_v11 §0.5.177.

Full contract is in `CLAUDE.md` at repo root + `.claude/skills/vps-cc-autonomy/SKILL.md` + this handoff's §0.5.

## Where we are (post-Session 12 close)

**Session 12 was the breakthrough**: bot went from "0 lifecycles ever, root cause unknown" to **live bars + strategy evaluating + listener connected + watchdog shipped + RESOLVE mode codified**.

What shipped (Session 12 — two PRs):

- **PR #47** `b9a0bc0` — **stale-bar watchdog** + `live_bars_60m` in `/status`. 5-min threshold, 15-min cooldown, session-edge suppression, recovery log. ALERT-only (no auto-halt). 12 unit tests. Verified firing live in prod (twice — once during the no-entitlement window, once during a farm flap).
- **PR #48** `209a37c` — **telegram-listener dialog-cache warmup**. Walks `iter_dialogs()` once before `client.get_entity(channel)` so private channels resolve by title from a fresh StringSession. Without this, `ValueError`.

Plus 7 new standing rules ratified (§0.5.177 RESOLVE mode → §0.5.183 broker > handoff). Plus three documented wrong-diagnoses (stuck-session, competing-login, wiring-bug) before landing on the actual root cause: paper account had DELAYED-only CME futures data; `keepUpToDate=True` silently delivers 0 callbacks on delayed entitlement. Fixed by operator subscribing to "US Securities Snapshot and Futures Value Bundle" on the LIVE account U17545037 (~$10/mo non-pro, paper inherits).

**v10 §0.5.171 was WRONG**: the $1.55/mo CME Real-Time claim was actually OPRA (US options), not CME futures. Broker state overruled the handoff (§0.5.183 codifies this).

## Bot status at session start (RUN V0–V8 FIRST)

- `tradeflow-app`: just restarted at end of Session 12 (post farm-flap regression). Should be healthy and re-armed.
- `tradeflow-ib-gateway`: Up healthy.
- `tradeflow-telegram-listener`: Up, **connected** to "Trading NQ Triggers" id=`3914810152` since 19:46:31 UTC.
- IBKR paper `DUQ331660`, live `U17545037`, NetLiq ~$1M, positions=[], orders=[].
- IBKR market-data entitlement: **REAL-TIME ACTIVE** (mdType=1 verified). Paper inherits from live.
- Lifecycles ever: **0** (strategy was evaluating during a clean 18:57–19:00 UTC window; then 19:00:58 UTC farm flap killed the sub for 57 min; then manual restart at session end).
- `seanbot_signals` rows captured: **0** at handoff (channel was quiet during the 30-min watch window after listener connected).
- Test baseline: 284 passing + watchdog tests (12) + listener tests = ~300 green.

**The key empirical question for this session**:
1. Did the end-of-session restart restore live bars cleanly? V3 tells us.
2. Has any natural IB farm flap occurred since handoff? V8 tells us. If yes and bars are dead again → PR-R1 is urgent.
3. Once PR-R1 ships, does the strategy ever fire `long_signal` across an RTH window, or does it sit on `noop_filter_or_regime`? That's the original empirical question we've been chasing.

## One critical new bug discovered (queued)

**IB farm-flap → silent subscription death** (§0.5.181). Observed at 19:00:58 UTC. The IB market-data farm dropped briefly (Warning 2103 + 2105 + Error 10182), came back ~1s later (2104 + 2106), but `keepUpToDate` subscription was never re-armed. The IB socket stayed up so PR-A reconnect didn't fire. Bars silent for 57 min until manual restart. PR #47 watchdog correctly paged on schedule.

**PR-R1** (AUDIT, draft in §13 of HANDOFF_v11) is the durable fix: detect the 2103/2105/10182 trio, auto-resubscribe via `subscribe_bars`, Telegram alert on auto-recovery. **Top priority this session.**

## Priority queue (this session)

In order — pick the highest unblocked:

### 1. **PR-R1 — auto-resubscribe on farm flap (RESOLVE-mode work order, AUDIT-level PR)** — HIGHEST
Without this, every farm flap = manual restart. Watchdog alerts but doesn't self-heal. Draft brief in HANDOFF_v11 §13.

### 2. **Empirical observation window** (no code work)
Once PR-R1 is live and the bot is self-healing, accumulate a full RTH session of [STRAT] eval decisions + any captured seanbot_signals. Read the decision distribution. Does it ever reach `long_signal`?

### 3. **`detect_signal` refactor — split regime vs filter** (REPORT, ~10 LOC)
Disambiguate `noop_filter_or_regime` into `noop_regime` vs `noop_filter` so we know which gate is blocking. Becomes critical the moment we have empirical data showing the strategy sits on noop forever during normal market conditions.

### 4. **PR-D3c — daily comparison digest** (REPORT, ~150 LOC) — depends on at least 1 day of data
After enough seanbot_signals + [STRAT] eval data accumulates, join them by timestamp: AGREE_ENTER / AGREE_NOOP / MISS / FALSE_POSITIVE. Daily Telegram digest at 16:30 ET.

### 5. **PR-S1 — secret rotation + log redaction** (AUTO after operator rotates)
TELEGRAM_SESSION_STRING + IBKR_PASSWORD + Telegram bot token all exposed via `docker inspect` and/or log lines.

### 6. **Kill switch PR** (AUDIT) — real-money readiness gate.

### Gaps carried forward
- **G1** — C1 regime gate fail-open (lower priority once empirical observation tells us if it matters)
- **G2** — `signal_scan_start_et` comment misleading (may be obsolete)
- **G3** — Seed depth 45 vs SMA warmup 100

## How this session operates

- **You (chat-side Claude)**: read HANDOFF_v11 §0.5.177–§0.5.183 (NEW RULES), §1, §2, §5 (wrong diagnoses), §7, §13 (PR-R1 draft). Then write RESOLVE-mode work orders by default for multi-step ops; REPORT/AUDIT PR briefs for pure code work. Reference `CLAUDE.md §<section>` instead of re-pasting standing rules.
- **CC VPS**: implements, ships PRs, runs end-to-end RESOLVE flows. Auto-loads CLAUDE.md.
- **Me**: read structured reports. Type `merge` for REPORT-level. Scan GitHub diffs for AUDIT-level. Click portal / paste session string / paste SQL for the genuinely-external bits CC VPS surfaces as `⛔ NEEDS OPERATOR`. Otherwise hands-off.

**Critical communication discipline carried forward (HANDOFF_v11 §10):**
- Chat-side me confabulated **three times** this session (stuck-session, competing-login, wiring-bug) before the actual root cause. Each was refuted by CC VPS gathering raw evidence. Don't retrofit when refuted — throw out and re-read.
- **Broker / external API state beats any handoff claim** (§0.5.183). Always probe carry-forward external-state claims via §6 V-block BEFORE trusting them. v10's "CME Real-Time active" was wrong; v11 might similarly be wrong about something in three months — verify.
- **Long secrets need length + SHA256 anchors** (§0.5.178).
- **`docker compose restart` doesn't re-read .env** — use `up -d --force-recreate` (§0.5.179).
- **For "0 of X" probes, always test with an independent minimal-wiring probe** before suspecting app code.

## First action

CC VPS runs §0.5.165 pre-flight scan + §6 V0–V8 verification from HANDOFF_v11. Reports results. Then:
- If V3 shows bars flowing healthily and V8 shows no recent farm flap → draft PR-R1 from §13 and ship as a RESOLVE work order.
- If V3 shows bars dead + V8 shows a farm-flap signature since handoff → manual restart first, then ship PR-R1 with elevated urgency.
- If V5 shows listener disconnected → re-verify TELEGRAM_SESSION_STRING in .env and PR #48 deployed; force-recreate.

Ready when you are.
