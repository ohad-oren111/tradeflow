# TradeFlow

Autonomous MNQ futures trading bot on Interactive Brokers. Long-only SMA100-bounce strategy. 2 contracts per trade. Paper-first deployment via IBKR paper account; live tier at $5k → $20k after 50 paper trades + 30 paper days.

## Architecture
- Python 3.11, ib_async, Supabase (custom REST client, not the supabase-py SDK), Hetzner VPS, Docker
- Three-tier tooling: chat (strategy + briefs) → Claude Code Web (production PRs) → VPS Claude Code (smoke tests, logs)

## Current phase
Phase 0 — repo bootstrap.

## Reading order for new sessions
1. `docs/architecture/v0_brief.md` (added in PR 1)
2. Latest `docs/handoffs/HANDOFF_v<N>.md`
3. `.claude/skills/code-pr-brief/SKILL.md` (after skills are copied from Botty)

## Standing rules (numbered, ported from Botty + new in TradeFlow)
- §0.5.97 — probe external specs (broker contracts, exchange fees, schema, library APIs) before depending on them
- §0.5.98 — broker/exchange state is ground truth; not internal DB tables
- §0.5.T1 — IBKR client IDs: feed = `IBKR_CLIENT_ID`, broker = `IBKR_CLIENT_ID + 1`
- §0.5.T2 — Long-only bracket-child SL is DISABLED by default; use separate GTC stop
- §0.5.T3 — Use `portfolio()` not `positions()` for runtime reconcile
- §0.5.T4 — Kill switch exits code 42; systemd has `RestartPreventExitStatus=42`
- §0.5.T5 — Never leave a futures position without a GTC stop on IBKR
