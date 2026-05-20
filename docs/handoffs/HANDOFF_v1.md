# TradeFlow — Handoff v1 (Phase 0 closed, ready for Phase 1)

*Handoff from end of 2026-05-19. **TradeFlow is pre-deployment — no live container, no broker connection, no positions.** Phase 0 scaffolding shipped end-to-end: VPS provisioned, repo bootstrapped, CI green on main. Phase 1 (IB Gateway Docker) is the next functional work. Botty AI is paused (full status in §1). This doc captures everything a new chat needs to pick up Phase 1 cleanly.*

---

## 0. How to use this doc

Read sections **0.5, 1, 4, 5, 15** first — that's the state-of-the-system as of handoff. Sections 2–14 are reference material. Section 14 is the source-of-truth ranking when this handoff disagrees with itself or a live observation: `main` branch on `github.com/ohad-oren111/tradeflow` at commit `ce3a158` or later.

**Do not trust this doc alone.** Run the verification block in §6 before writing any code. **There is no live trading bot yet — Botty AI is shut down, TradeFlow has no broker connection. No urgency-driven actions are warranted.**

---

## 0.5 Standing rules (permanent — do not remove from handoff)

### Carry-forward from Botty AI lineage (§0.5.92–.98)

**§0.5.92 Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block. Self-contained commands, chained with `&&` or grouped. Source env vars explicitly in the same block. Expected output described immediately below each block, plus a decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**§0.5.93 Learning-delivery discipline.** Every time you learn something new — a bug pattern, a corrected assumption, an environmental fact, a diagnostic finding — surface it immediately in the chat, formatted as a markdown snippet the operator can paste verbatim into the running handoff queue. Do not wait until end-of-session.

**§0.5.94 Read before diagnosing.** When debugging a complex state bug, read the full startup log and 3-5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**§0.5.95 Verify severity against the source of truth.** Before escalating urgency language ("capital at risk", "churning fees", "spiraling"), hit the source of truth — live API (IBKR via `ib_async` for TradeFlow), live DB (Supabase REST for TradeFlow), raw log file — not aggregated metrics.

**§0.5.96 Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise. The operator does not run smoke tests by hand. CC Web ships PRs; VPS CC runs the smoke test runbook end-to-end and produces a §7 structured report.

**§0.5.97 Probe external specs against the source before baking into briefs.** Broker contracts (IBKR MNQ tick size, multiplier), exchange fees (commission tier), library API surfaces (`ib_async` vs deprecated `ib_insync`), schema column names, Hetzner SKU availability — all of these have drifted between memory and live reality. Any fact about an external system gets a quick verification probe *before* it appears in a brief, runbook, or PR. Six new §0.5.97 instances logged this session alone — see §10.

**§0.5.98 Broker/exchange state is ground truth, not internal DB tables.** For position, fill history, or capital claims, the source of truth is the broker API (`ib_async` for IBKR), not the project's persistence layer. Sibling to §0.5.97. Botty v61 shutdown surfaced this: project's `lifecycle_events` query returned 0 fills, but live Binance history showed 6 fills covering $137.26 deployed. Carries to TradeFlow: position truth is IBKR's API, not our `positions` table snapshot.

### TradeFlow-specific (§0.5.T1–T5, from Session 1 kickoff)

**§0.5.T1–T5 are defined verbatim in `TRADEFLOW_SESSION_1_KICKOFF.md`.** Carry forward as-is into v2. If the kickoff doc is lost, reconstruct from `ohad-oren111/tradeflow` once that doc lands in `docs/handoffs/` (target: end of Session 2).

### New rules ratified this session (§0.5.99–.102)

**§0.5.99 VPS CC sessions launch from inside the project root.** `cd ~/tradeflow && claude`, not `claude` from `~`. Two reasons: (a) avoids the dup-rules display in `/permissions` UI when home-dir and project-dir resolve to the same `.claude/settings.json`, (b) eliminates the need for `cd ~/tradeflow && ...` prefixes in runbook commands.

**§0.5.100 Runbooks prefer `git -C <path>` over `cd <path> && git ...`.** CC has a hardcoded directory-hook safety check that prompts on every `cd <dir> && git ...` pattern, independent of `permissions.allow` rules. Refactor runbooks to use `git -C ~/tradeflow status` / `git -C ~/tradeflow rev-parse HEAD` etc. The same applies in spirit to other tools that accept `-C` or path args.

**§0.5.101 Bash permission patterns must account for arbitrary flag ordering.** Positional rules like `Bash(curl -s https://api.github.com/*)` fail when flags appear between (e.g. `curl -s -H "..." https://api.github.com/...`). Prefer broad allow patterns (`Bash(curl *)`) paired with targeted deny rules for dangerous patterns (`Bash(curl * > /home/*)`, `Bash(curl * -o /home/*)`).

**§0.5.102 Confirm CC Web merges via `git rev-parse origin/main`, not UI signal.** The "merged" badge in CC Web's UI rendered for PR #2 on 2026-05-19 when no merge had actually happened. The branch existed with the commit, but no PR was open and `origin/main` was unchanged. Verify by fetching origin and inspecting HEAD before declaring victory.

---

## 1. Where we are (as of handoff, 2026-05-19 ~23:10 UTC)

### Live production state — TradeFlow

- **No production containers running.** No IB Gateway, no orchestrator. Phase 0 is scaffold-only.
- **No broker connection.** IBKR paper account (DU…) exists per kickoff but credentials not yet placed in `~/.tradeflow-secrets/.env`. Live trading is Phase 7+ work.
- **No positions, no open orders, no P&L.** Bot has not traded.
- **Hetzner VPS:** Hetzner CPX21, Hillsboro OR, Ubuntu 22.04.5 LTS, IP `5.78.212.37`, user `tradeflow`. Active, idle, no services besides system defaults.
- **Repo:** `github.com/ohad-oren111/tradeflow` (public). HEAD on main: `ce3a158` (PR 1.5 merge).
- **Secrets dir:** `/home/tradeflow/.tradeflow-secrets/` (mode 700) — contains `.env.example` only. No real credentials yet.

### Live production state — Botty AI (carry-forward)

- **PAUSED as of 2026-05-19** (per Botty v61 shutdown). Hetzner VPS shut down. No containers running, no orders, no PnL accrual.
- **Capital at shutdown:** $1462.80 USDT free + ~$543 spot inventory across BTC/ETH/SOL/BNB/XRP (Binance mainnet).
- **Strategy verdict:** EMA-cross + buy-grid is final — 243 deploys × 0 round-trip fills across 5 backtester configurations.
- **Deferred decision rule:** 30 days post-TradeFlow-live, re-evaluate the BTC/ETH cointegrated stat-arb pivot on Binance perpetual futures (per cross-LLM Round 2 convergent recommendation). Gemini R2 gates: tracking error <5%, zero execution failures, profit factor >1.30 over 30+ cycles. Default action absent profitable TradeFlow: stay paused.

### What just shipped this session

- **PR 1** (`e6a498a`, merged as PR #1 → `14fb5e4`) — Phase 0 repo bootstrap. 27 files, 641 insertions: `pyproject.toml`, `requirements.txt`, `.env.example`, `.gitignore`, `README.md`, `CLAUDE.md`, `INSTALL.md`, `main.py`, `config/{instruments,risk_params,settings}.py`, `comms/telegram.py` (stubs), `supabase/schema.sql` (5 tables), `tests/test_config.py` (4 tests), empty package `__init__.py` files. VPS smoke test PASS.
- **PR 1.5** (`3a967c3`, merged as PR #3 → `ce3a158`) — GitHub Actions CI workflow at `.github/workflows/ci.yml`. 43 lines: ubuntu-22.04 runner, Python 3.11, runs ruff/black/mypy/pytest on every push and PR to main. VPS smoke test PASS. CI on merge commit: completed/success in ~39s. PR-event run on `3a967c3` also success.
- **VPS bootstrap** (no PR, manual provisioning). All 13 steps in §15 of the kickoff completed: system tools, unattended-upgrades, 2GB swap, ufw, tradeflow user with sudo+docker groups, sshd hardened (root login disabled, key-only), Python 3.11.15, Docker CE 29.5.1, Compose v5.1.3, secrets dir, Claude Code 2.1.145 (native installer, authed to ohador@gmail.com Claude Max), final verification block passed.
- **VPS CC permissions** at `~/.claude/settings.json` (136 allow rules, 49 deny rules, schema URL `https://json.schemastore.org/claude-code-settings.json`). Source file in repo for reference: not yet committed — currently only on VPS at `/home/tradeflow/.claude/settings.json`.

### What we discovered this session (not yet in code)

- **`comms/telegram.py` stub silently swallows messages** when invoked without `logging.basicConfig()`. The stub uses `logger.info(...)` with no handler configured by default. Fix deferred to Phase 1+ when the first PR that actually emits logs lands (probably PR 2 IB Gateway). PR-1's brief should have specified the basicConfig — §0.5.97 carry-forward.
- **PR 1.5 nearly didn't merge.** CC Web's sandbox state from PR 1 persisted into the PR 1.5 task, causing PR #2 to be opened against the stale base branch `claude/phase-0-repo-bootstrap-ZZGJX` instead of `main`. CC Web's UI showed a "merged" label, but `git rev-parse origin/main` confirmed no actual merge happened. Recovered via a fresh PR #3 opened directly through the GitHub UI with base=main, head=claude/add-ci-workflow-Jt23W. See §5 wrong diagnoses.

---

## 2. The session's work thread

1. **Morning:** Kickoff doc loaded. VPS provisioning began.
2. **Hetzner reality check:** kickoff specified CX32 Ashburn at €8/mo; live Hetzner UI showed CX in "Limited availability" status, Ashburn not selectable in the current US flow. Pivoted to CPX21 Hillsboro at $13.99/mo. AMD silicon (CPX line) is plausibly a better fit for the eventual Java IB Gateway workload anyway. §0.5.97 flagged.
3. **VPS bootstrap:** 13 steps completed end-to-end. Three deviations from kickoff expectations: Docker Compose v5.x not v2.x (versioning realigned by Docker), Python 3.11 needed deadsnakes PPA (not in jammy main repos), Claude Code installs via native installer not npm. All §0.5.97-flagged.
4. **PR 1 bootstrap:** Drafted PR brief off-template (caught later by operator), but the brief landed clean and CC Web shipped 27 files cleanly. GitHub App per-repo install required manual add — Anthropic Claude GitHub App was scoped to "Only select repositories" and didn't auto-grant access to the new TradeFlow repo. Recovered via UI grant. PR merged at `14fb5e4`.
5. **PR 1 VPS smoke test:** PASS. Surfaced the `comms/telegram.py` logging-handler quirk (see §1). VPS CC offered to apply a `logging.basicConfig` fix to `main.py`; operator approved but exited before it ran. Decided to **defer** the fix to Phase 1+ — VPS CC editing prod code violates the verification-only rule (§0.5.96 sibling), and the Phase 0 main.py is a stub anyway.
6. **PR 1.5 CI workflow brief:** rewrote using the `code-pr-brief` skill template (operator caught the skipped-skill on first attempt — §0.5.97 carry-forward to discipline). CC Web shipped `3a967c3` but opened the PR with base = the stale PR 1 branch, not main. CI didn't fire (workflow filter is `branches: [main]`). The "merged" UI signal from CC Web was unreliable — `git rev-parse origin/main` showed `14fb5e4`, no merge had happened.
7. **Recovery:** opened PR #3 directly via GitHub UI compare URL with correct base=main, head=claude/add-ci-workflow-Jt23W. CI fired on the PR (success), CI fired again on the merge commit `ce3a158` (success), both in ~39s.
8. **VPS CC permissions:** drafted `~/.claude/settings.json` with 109 → 136 allow rules and 35 → 49 deny rules across two iterations. First scp deployed via terminal heredoc paste — got mangled, file truncated mid-allow-list. Second iteration deployed via scp directly. Verified content via `jq`. Curl pattern was initially too narrow (`Bash(curl -s https://api.github.com/*)` didn't match curl-with-flags); broadened to `Bash(curl *)` with strategic denies.
9. **`cd` prompts persist** despite settings — CC's built-in directory-hook protection fires on `cd <dir> && git ...` regardless of allow rules. Resolution: §0.5.99 (launch from project root) + §0.5.100 (prefer `git -C <path>`). Future runbooks refactor.
10. **PR 1.5 VPS smoke test:** PASS. HEAD on main is `ce3a158`, workflow file present, content matches brief, YAML parses, ruff/black/mypy/pytest all green locally on VPS (parity with CI), GitHub Actions API confirms CI run on merge commit succeeded.

---

## 3. What the system is actually made of

**Single source of truth:** `github.com/ohad-oren111/tradeflow` on `main` at commit `ce3a158`. No system-map doc exists yet — this handoff is the best available system reference for now. v2 should add `docs/architecture/SYSTEM_MAP.md` once Phase 1 lands real components.

Highlights:

- **5 database tables defined in `supabase/schema.sql`** (not yet applied to a Supabase project): `trades`, `positions`, `daily_summary`, `kill_switch_events`, `signals`. `trades.exit_reason` enum includes the three SeanBot failure modes: `ibkr_order_failed`, `limit_not_filled`, `false_signal_bad_sma`.
- **Production-live code paths:** none yet. `main.py` is a stub argparse banner (`--paper`, `--live`, `--backtest`). All execution/strategy/risk/data/features/backtest packages are empty `__init__.py` only.
- **Dead/phantom surfaces:** none yet — first PR shipped a clean scaffold.
- **Container topology (future):** Phase 1 PR 2 adds IB Gateway Docker container. Phase 1 PR 3+ adds the orchestrator container. Both run on Hetzner CPX21.
- **Automation gotchas (future):** no cron yet. Daily summary cron will land in Phase 4+.
- **Open documented bugs:** none yet (clean scaffold).

---

## 4. Verified facts about TradeFlow (2026-05-19)

**DO NOT challenge these unless the schema migrates or the broker spec changes.**

### MNQ contract spec (CME, verified against SeanBot prod at `config/settings.py:32-35` — §0.5.97-verified)
- `TICK_SIZE = 0.25` index points
- `MULTIPLIER = $2/point` ($0.50/tick)
- `COMMISSION_RT = $0.62` round-trip at friend's IBKR tier (may differ at other tiers — verify against your IBKR statement before going live)
- `MARGIN_INTRADAY = $2000` (friend's day-trade margin; CME maintenance margin ~$3636)
- Quarterly cycle Mar/Jun/Sep/Dec, expiry 3rd Friday, roll ~8 days before expiry
- **Risk per trade math:** 75pt SL × 4 ticks/pt × $0.50/tick × 2 contracts = **$300 max loss**

### Repo state (verified 2026-05-19 23:02 UTC)
- HEAD on `main`: `ce3a158`
- Last 3 commits on main: `ce3a158` (PR #3 merge, PR 1.5) → `3a967c3` (PR 1.5 commit) → `14fb5e4` (PR #1 merge) → `e6a498a` (PR 1 commit) → `b2cb279` (init main with placeholder README)
- 27 files from PR 1 scaffold + 1 file from PR 1.5 (`.github/workflows/ci.yml`) = 28 tracked files at handoff time
- Branch protection on `main`: **NOT YET ENABLED** — first action in Session 2 is to enable it via GitHub UI (Settings → Branches → Add rule → require "Lint, type-check, and test" before merging)

### VPS state (verified 2026-05-19)
- Host: Hetzner CPX21, Hillsboro OR, IP `5.78.212.37`
- OS: Ubuntu 22.04.5 LTS, x86_64
- Python: 3.11.15 (via deadsnakes PPA)
- Docker: 29.5.1, Compose v5.1.3 (note: v5.x not v2.x)
- Claude Code: 2.1.145 (native installer at `~/.local/bin/claude`)
- User: `tradeflow` (uid=1000, groups: tradeflow, sudo, docker)
- Memory: 3.7 GiB total + 2.0 GiB swap; disk 68G free of 75G
- Secrets dir: `/home/tradeflow/.tradeflow-secrets/` (mode 700)
- VPS CC settings: `~/.claude/settings.json` (136 allow, 49 deny, schema URL is `https://json.schemastore.org/claude-code-settings.json`)

### Library choices (locked, do not re-evaluate without explicit decision)
- IBKR client library: **`ib_async`** (active fork of deprecated `ib_insync`) — confirmed against SeanBot scaffold
- Strategy: SMA100-bounce, long-only, 2 contracts, 75pt SL, 150pt trail, max 5 positions — locked from kickoff
- Database: Supabase via custom REST client (not the supabase-py SDK — kickoff decision)

---

## 5. Wrong diagnoses (if any) — READ BEFORE YOU DEBUG

### Wrong call #1: "PR 1.5 was merged" (corrected mid-session)

**Diagnosis:** After CC Web pushed `3a967c3` and the UI displayed a "merged" label on what we thought was PR #2, declared PR 1.5 complete and proceeded to write the VPS smoke test runbook.

**Evidence that misled:** CC Web's chat output included the line "Got it — referencing PR #2 going forward" and a Merged-status badge in the UI dump.

**Why it was wrong:** No PR was actually opened against main, and no merge ever happened. The branch existed on origin with the commit, but `origin/main` was unchanged at `14fb5e4`. The VPS smoke test runbook caught this on first run (§2 HEAD mismatch → FAIL verdict).

**Correct diagnosis:** The "merged" signal in CC Web's UI was UI optimism / a branch-side rendering, not actual merge state. Verified by `git rev-parse origin/main` post-fetch and `git log --all --oneline -10` to confirm the commit only existed on `claude/add-ci-workflow-Jt23W`, not on main.

**Recovery:** Opened PR #3 via GitHub web UI compare URL with base=main, head=claude/add-ci-workflow-Jt23W. Merged cleanly. CI fired and went green.

### Wrong call #2: "Settings.json got the v1 deploy via heredoc cleanly" (corrected mid-session)

**Diagnosis:** After running `cat > ~/.claude/settings.json <<'EOF' ... EOF` via terminal paste, assumed the file deployed correctly. `/doctor` only flagged the schema URL, which CC Web fixed in-place.

**Evidence that misled:** `/doctor` reported only one issue (the schema URL), implying the JSON was at least syntactically valid.

**Why it was wrong:** The heredoc terminated mid-content (around `"Bash(file *)"`) and the entire deny list never landed. The JSON parsed because the EOF marker was hit somewhere in the truncated content, closing the structure prematurely. Result: 30+ allow rules missing and **zero deny rules** — the safety net (git push, rm -rf, systemctl, docker lifecycle) entirely absent.

**Correct diagnosis:** Heredoc paste mangles long multi-line content via SSH terminal sessions, full stop. Discovered via the truncated file content cat'd back in the user's terminal.

**Recovery:** Saved the full settings.json as a file artifact, deployed via `scp ~/Downloads/vps_settings.json tradeflow:~/.claude/settings.json`. Verified counts via `jq`.

**Lesson for next session:** Both wrong calls came from trusting UI signals or "looks-OK" partial outputs without re-grounding against the source of truth (git, jq, filesystem). §0.5.95 carries forward strongly — verify state via the source-of-truth tool, never the UI rendering or the partial output. This is also the meta-pattern that §0.5.97 was ratified for in Botty v61.

---

## 6. Verification block — run this before doing anything

**V0 — SSH connectivity and Claude Code presence**
```bash
ssh tradeflow 'whoami && hostname && date -u && claude --version'
```
Expect: `tradeflow`, `ubuntu-4gb-hil-1`, current UTC date, `Claude Code 2.1.x` (≥ 2.1.145).
If any deviate: STOP. The VPS is the wrong host or Claude Code is broken; re-verify against §4 facts.

**V1 — Repo state on VPS**
```bash
ssh tradeflow 'git -C ~/tradeflow fetch origin main && git -C ~/tradeflow rev-parse origin/main && git -C ~/tradeflow log -5 --oneline origin/main'
```
Expect: HEAD sha on `origin/main` is `ce3a158` or a descendant. Last commits include `ce3a158` (PR #3 merge), `3a967c3` (PR 1.5), `14fb5e4` (PR #1 merge), `e6a498a` (PR 1).
If HEAD has *advanced* past `ce3a158`: read the new commits — Session 2 may have already shipped something. Update §1 mental model accordingly before acting.

**V2 — CI status on main**
```bash
curl -s -H "Accept: application/vnd.github+json" "https://api.github.com/repos/ohad-oren111/tradeflow/actions/runs?per_page=5" | jq '.workflow_runs[0] | {name, head_sha: .head_sha[0:7], status, conclusion, html_url}'
```
Expect: most recent CI run on main has `conclusion: "success"`. If `"failure"`: STOP. Read the run log via the html_url before any other action — a red CI run on main blocks all PR merges.

**V3 — VPS CC permissions loaded**
```bash
ssh tradeflow 'jq ".permissions.allow | length" ~/.claude/settings.json && jq ".permissions.deny | length" ~/.claude/settings.json'
```
Expect: `136` and `49`. If different: settings.json drifted (probably overwritten by /update-config or /fewer-permission-prompts). Restore from `vps_settings.json` artifact in chat history or re-derive.

**V4 — Botty AI VPS is still off (sanity check)**
```bash
# Botty VPS hostname and IP from prior handoffs — confirm shut down. Adjust IP if it differs in your records.
ssh -o ConnectTimeout=5 botty 'docker ps -q' 2>&1 || echo "Botty VPS unreachable (expected — shut down 2026-05-19)"
```
Expect: connection refused or no containers. Botty was shut down as part of v61 close. If Botty's VPS comes back online unexpectedly, ignore for now — TradeFlow is the active build. Investigate only if planning the cointegrated stat-arb pivot.

---

## 7. Pending work queue

Priority order is by V1 state, not by ordering below. Read V1 first.

### P0 — Enable branch protection on main *(operator, manual GitHub UI step)*
Settings → Branches → Add rule → branch name pattern `main` → require "Lint, type-check, and test" status check before merging. Now that CI is green on main, this can be safely enabled. **Do not skip — without it, anyone (including CC Web) can land code that breaks CI on main.** Estimated: 2 minutes in browser.

### P1 — Skill port from Botty repo *(§15 step 7 from kickoff)*
Copy 4 skills directories from Botty repo into TradeFlow's `.claude/skills/`:
- `code-pr-brief/`
- `prod-debug-discipline/`
- `session-handoff-writer/`
- `vps-smoke-test-runbook/`

Commit as a single small PR (or direct push if branch protection allows admin override on initial sync). Title: `chore: port .claude/skills/ from Botty repo (4 skills)`. Estimated: 15 minutes (local git op).

### P2 — IBKR paper account verification *(§15 step 3 from kickoff)*
Confirm paper trading account `DU…` is active and credentials are obtainable (TWS account number + paper-trading password). These will populate `~/.tradeflow-secrets/.env` in Phase 1 PR 2. **Do not start Phase 1 PR 2 without this verified** — the Docker container needs creds to even start IB Gateway. Estimated: 5 minutes via IBKR portal.

### P3 — Phase 1 PR 2: IB Gateway Docker container
First functional PR. Drops IB Gateway in a Docker container managed by docker-compose, exposes the API socket to the host, mounts `~/.tradeflow-secrets/.env` for credentials, includes a healthcheck that pings IBKR via `ib_async` from a Python sidecar. Brief lifts patterns 1–3 from SeanBot. Estimated: 3-4 days (one PR brief, one CC Web ship, one VPS smoke test runbook, one verification cycle).

### P4 — Phase 1 PR 3: Orchestrator container scaffold
Empty orchestrator that connects to IB Gateway, subscribes to MNQ market data, logs `[ORCH] tick — bid/ask/last` lines. No trading logic yet. Validates the IB Gateway → orchestrator wiring. Lifts patterns 4–6 from SeanBot.

### Operational cleanup eventually
- **Commit `~/.claude/settings.json` to the repo as `vps_settings.json` reference** so it's versioned. Currently only on the VPS at `~/.claude/settings.json`. Add it to a future small PR.
- **Consider running `/fewer-permission-prompts` in VPS CC** after a few weeks of real use — it scans transcripts and auto-proposes additional allow rules based on actual usage patterns.
- **Add `docs/architecture/SYSTEM_MAP.md`** when Phase 1 ships real components, replacing this handoff as the canonical system reference.

---

## 8. Test safety — why we belabor this

Carry-forward from Botty AI lineage. The five recurring test-mocking traps:
1. Tests passed against a fictional schema because they mocked column names that didn't exist in prod
2. `side_effect` list had wrong count → silent `StopIteration` → wrong assertions ran without error
3. Mocked at raw library chain (`supabase.table().upsert().execute()`) when code uses a wrapper (`self._db.upsert(...)`) → tests green, prod broken
4. Shared `MagicMock()` state leaked between tests
5. Async decorator pattern assumption (`@pytest.mark.asyncio`) — verify a neighbor before assuming

Guardrails in the `code-pr-brief` skill template (Pre-Push Checklist → Tests section) prevent all five. Do not ship tests that skip them.

TradeFlow's first real tests land in Phase 1 PR 2 (IB Gateway healthcheck) and Phase 1 PR 3 (orchestrator wiring). Apply the guardrails from the start — don't wait for the first test bug to enforce.

---

## 9. Pitfalls from prior sessions

Things the LLM got wrong before and should not be trusted on without verification:

- **CC Web UI "merged" signal lies.** Verify with `git rev-parse origin/main` after a claimed merge.
- **Heredoc paste mangles long content.** Use `scp` or CC's `Write` tool for any file > ~50 lines of content over SSH.
- **CC Web sandbox branch state persists across PRs in the same task.** Force a fresh `main` checkout at the start of each new PR brief.
- **`cd <dir> && ...` triggers a CC built-in safety prompt** that is independent of permissions. Use `<tool> -C <dir>` or launch from the directory.
- **Skills must be read before drafting.** Both `code-pr-brief` and `vps-smoke-test-runbook` skills were skipped on first attempts this session; operator caught both. Future sessions: invoke the skill file via `view` before writing the artifact.
- **Hetzner CX SKU is in "Limited availability".** Don't assume kickoff-era pricing/regions are still bookable. Verify against the live console.
- **Docker Compose is v5.x on current Ubuntu.** Not v2.x. Docker realigned plugin versioning.

**Next session rule:** if a claim is quantitative (commit hash, row count, file count, container state), re-verify it via the source-of-truth tool (`git rev-parse`, `jq`, `docker ps`, the broker API). Don't trust the handoff's numbers without re-grounding.

---

## 10. Session discipline lesson (2026-05-19)

This session ratified **eleven new §0.5.97 instances** in a single sitting. Pattern: most came from baking external-system facts (Hetzner SKU, Docker Compose version, CC Web behavior, settings.json paste fidelity, CC's directory-hook protection) into briefs or runbooks from memory rather than probing the live system first.

**Enforcement rules for next session:**

1. **External system facts get a probe before they appear in a brief.** Hetzner SKU pricing → live console. Library API surfaces → docs or `--help`. Docker package versions → `<pkg> --version`. CC behavior → docs or test in a throwaway directory.
2. **Long file content over SSH goes via `scp` or `Write` tool, never terminal heredoc.** Verify the deployed file via `jq` or `wc -l` immediately after.
3. **Skills get loaded (via `view`) at the start of any drafting task — PR briefs, runbooks, handoffs.** Skipped skills cost a re-draft each time.
4. **Merges get verified via `git rev-parse origin/main`, not the CI tool's UI.** A "merged" label is a hypothesis; a fast-forwarded `main` HEAD is the fact.
5. **Branch protection comes AFTER the first green CI run on main, not before.** Chicken-and-egg.

---

## 11. Logging verbosity — what to demand from any new code

Standing principles for what "well-logged code" looks like in TradeFlow (carry from kickoff):

- Every IBKR order placement logs `[ORDER] MNQM6: PLACED LMT BUY 2 @ 25000.00 — sma100=24985.0, rsi=42`
- Every state transition logs old → new at INFO: `[STATE] MNQM6: SCANNING → ENTERED — sma100 reclaim`
- Every swallowed exception logs the specific error + symbol + position context
- Retry loops log attempt number and reason: `[RETRY] MNQM6: 2/5 — IBKR socket timeout`
- Async code logs entry AND exit (`[ENTRY] cycle.run`, `[EXIT] cycle.run — 1.3s`)
- Any dedup/select-one-of-many must log which row won and why
- `logging.basicConfig(level=logging.INFO)` set in `main.py` from PR 2 onwards (Phase 0 stub omitted this — see §1)

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill at `.claude/skills/code-pr-brief/` (once ported per §7 P1). It enforces: patch constraints, code quality, test safety guardrails, known gotchas (carry from this handoff's §9), and the "what I got wrong" post-PR section. Until the skill ports, copy from Botty's `.claude/skills/code-pr-brief/pr_brief_template.md` or from a prior PR brief verbatim.

---

## 13. Current PR brief in flight (if any) — hand this to Claude Code as-is

**None at handoff time.** Session 2 starts by enabling branch protection and porting skills (P0 + P1 from §7). Once both are done, the first new brief is Phase 1 PR 2: IB Gateway Docker. That brief gets drafted in Session 2 — not pre-written here.

When drafting PR 2: read `code-pr-brief` skill (loaded via `view`), reference SeanBot patterns 1–3 (IB Gateway connection management, market data subscription, healthcheck loop), specify the docker-compose service definition and the secrets mount, and pin the `ib_async` version in `pyproject.toml`.

---

## 14. Canonical references (in order of authority)

1. **GitHub repo on `main` at `ce3a158` or later** — what actually runs / is committed
2. **VPS filesystem at `/home/tradeflow/tradeflow/`** — what's deployed on the box (should match repo modulo uncommitted experiments)
3. **IBKR API via `ib_async`** (Phase 1+) — truth for positions, fills, account state
4. **Supabase REST** via SUPABASE_URL + service-role key (Phase 1+) — truth for trade/position rows
5. **GitHub Actions runs API** at `https://api.github.com/repos/ohad-oren111/tradeflow/actions/runs` — truth for CI status
6. **`TRADEFLOW_SESSION_1_KICKOFF.md`** — the original orchestration doc; verbatim definitions of §0.5.T1–T5 live here
7. **This handoff (v1)** — session context, NOT long-term authority
8. **Botty AI handoff v61 + earlier** — historical lineage; standing rules §0.5.92–.98 originated there

---

## 15. First 15 minutes of the next session

1. **Read sections 0.5, 1, 4, 5, 15** of this handoff. Section **5 (wrong diagnoses)** is the single most important to internalize before touching anything.
2. **Run §6 verification block.** Confirm: HEAD on main is `ce3a158` or descendant; CI's most recent run conclusion is `success`; settings.json allow=136, deny=49.
3. **Enable branch protection on `main`** (P0 from §7) — 2 minutes in the GitHub UI.
4. **Port the 4 skills from Botty** (P1 from §7) — clone Botty locally, copy `.claude/skills/` into the TradeFlow clone, commit, push. May need a small PR via CC Web if branch protection is now strict.
5. **Verify IBKR paper account** (P2 from §7) — log in, confirm `DU…` account is active, retrieve creds.
6. **Draft Phase 1 PR 2 brief** (P3 from §7) using the `code-pr-brief` skill. Hand to CC Web. After merge, draft VPS smoke test runbook via the `vps-smoke-test-runbook` skill. Standard rhythm from here on.

---

## 16. How to publish this handoff

**Path A — VPS Claude Code brief (preferred):**

~~~
You are VPS Claude Code on the TradeFlow VPS. Save the following content verbatim
to /home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v1.md (create the directory
if it does not exist), then:

  cd ~/tradeflow
  git status
  git add docs/handoffs/HANDOFF_v1.md
  git commit -m "docs: add v1 handoff (Phase 0 closed, ready for Phase 1)"
  git push origin main

Confirm the file exists at the path, git log shows the commit at HEAD, and
`git status` is clean. Report back with the commit hash and the file's
line count via `wc -l`.

<paste handoff content here>
~~~

**Path B — Manual scp + git from VPS (preferred for v1 since branch protection isn't on yet):**

See step-by-step commands below in the chat response — the operator runs them from their laptop.

The handoff exists only if saved to disk **and** committed **and** pushed. Until then, treat the chat output as draft.

---

*End of handoff v1. Target lifespan: until Phase 1 PR 2 (IB Gateway) lands and the first orchestrator brief is in flight. Then v2 supersedes.*
