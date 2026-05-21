# TradeFlow — Handoff v3 (P0–P3 closed, PR #6 merged, smoke test BLOCKED on IBKR creds)

*Handoff from end of 2026-05-20 (Session 3, ~7 hours). Phase 1 PR 2 is merged on `main` at `f29ae56`. IB Gateway container is running but **rejected at IBKR login** with "Invalid username or password" — operator is mid-IBKR-portal-credential-reset when this handoff was written. Do not start trading work in the next session until the smoke test (Task F of PR #6) returns PASS. This doc captures everything Session 4 needs to pick up cleanly.*

---

## 0. How to use this doc

Read sections **0.5, 1, 2, 4, 5, 15 first.** Sections 7–13 are reference. §14 is the authority hierarchy when this doc disagrees with itself or a live observation. §5 (wrong diagnoses, 11 §0.5.97 instances logged this session) is the single most important to internalize before any debugging — Session 3 made 6 wrong calls in the verification path, all the same family as Session 2's 5 wrong calls.

**Do not trust this doc alone.** Run §6 verification block before any action. Critical first action for Session 4: confirm IBKR paper password reset has propagated and the gateway logs in cleanly. Until that's green, no orchestrator work, no PR 3 drafting.

---

## 0.5 Standing rules (permanent — carry forward verbatim; never paraphrase, never delete)

Rules .92–.105 from HANDOFF_v2 carry forward verbatim. Summary inline:

- **§0.5.92 — Copy-paste discipline.** Every action recommended is a copy-paste-ready bash block. Source env explicitly in the same block. Expected output below each. Decision tree if multiple branches matter.
- **§0.5.93 — Learning-delivery discipline.** Every new fact surfaced immediately as a markdown snippet for the running handoff queue. Not at end-of-session.
- **§0.5.94 — Read before diagnosing.** Read the full startup log and 3-5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.
- **§0.5.95 — Verify severity against source of truth.** Before escalating urgency, hit the live API or DB, not aggregated metrics or memory.
- **§0.5.96 — Active secrets path** is `/home/tradeflow/.tradeflow-secrets/.env`. (Botty-era variants like `.env.production` are deprecated and do not apply.)
- **§0.5.97 — Probe external specs before baking.** Broker contracts, fees, schema, library API surfaces, image registries, env var names — verify against source. Session 3 added 11 new instances (#23–#33) — see §5 and §10.
- **§0.5.98 — Broker/exchange API is ground truth** for position, fill history, capital claims. Not internal DB tables, not handoff docs.
- **§0.5.99 — Launch VPS CC from project root** (`cd ~/tradeflow && claude`) so project-local `.claude/settings.local.json` is read.
- **§0.5.100 — CC's directory-hook for `.claude/` reads** — accept the "allow reading from .claude/" offer when prompted at session start; decline write-access offers.
- **§0.5.101 — Prefer `git -C <path>` for VPS git operations** so each command states the repo path explicitly.
- **§0.5.102 — Verify merges via `git rev-parse origin/main` and `git ls-tree origin/main`** — UI "merged" signal can lie. PR #4 in Session 3 merged into a stale feature branch; only the ls-tree probe caught it.
- **§0.5.103 — Chained Bash commands check per-subcommand** against CC's allow rules. Prefer broad allows + targeted denies in `~/.claude/settings.json`.
- **§0.5.104 — CC's meta-safety on `.claude/`** distinguishes reads (low-risk, accept) from writes (high-risk, decline). Different family from §0.5.100.
- **§0.5.105 — Permission rule additions are comprehensive sweeps**, not iterative patches. After two wrong patches in the same family, stop and redesign.

**TradeFlow-specific (§0.5.T1–T5)** carry forward verbatim from v2 §0.5 and `TRADEFLOW_SESSION_1_KICKOFF.md`:
- §0.5.T1 — Repo env-var convention is `IBKR_*`; never rename to `IB_*` or `TWS_*` at the repo layer. Docker-compose maps `IBKR_*` → `TWS_*` only inside the gateway container.
- §0.5.T2 — Library is `ib_async` (active fork), NOT `ib_insync`.
- §0.5.T3 — `IBClient` exposes `get_positions()`, `get_portfolio()`, and `get_open_trades()` — all three are required, do not drop `get_portfolio()` if regenerating.
- §0.5.T4 — Supabase via **custom REST client** (`httpx` wrapper), NOT `supabase-py`.
- §0.5.T5 — All Python code targets Python 3.11 in containers; VPS host Python is 3.10.12 for ops only.

**New in Session 3 (ratified, append-only):**

- **§0.5.106 — Pasted probe output in a kickoff is a snapshot, not current state.** When state matters and a kickoff doc contains both a verification snapshot AND an explicit current-state claim, run V1 in real-time to break the tie before acting. Failure mode: Session 3 wrong call #1 (§0.5.97 instance #23).

- **§0.5.107 — GitHub branch protection uses Rulesets, not classic.** Verify via `gh api /repos/{owner}/{repo}/rules/branches/{branch}` (auth required) or by an operationally meaningful test (try to push to main directly and watch it be rejected). The classic `/branches/{branch}` API returns nulls for rulesets-only protection. Failure mode: §0.5.97 instances #24, #25.

- **§0.5.108 — Every PR brief delegating to CC Web MUST pin `--base main` explicitly** in the brief, in the gotchas section, AND in the pre-push checklist. CC Web defaults to its working-branch context, which can be a stale feature branch. Failure mode: §0.5.97 instance #26 (PR #4 lost to wrong base).

- **§0.5.109 — Docker-compose `env_file:` ≠ `${VAR}` interpolation source.** `env_file:` loads vars INTO the container; `${VAR}` interpolation in the compose file itself reads from shell env or working-directory `.env`. If a compose file uses BOTH against the same secrets file, the interpolation source must be set independently. Cleanest pattern: use ONE mechanism, not both. Failure mode: §0.5.97 instance #28.

- **§0.5.110 — `.env` file format is strict `KEY=VALUE`.** No inline comments after the value. The entire rest of the line is the value (including whitespace and `#...` text). When templating from `.env.example`, edit the value but DELETE the entire trailing comment, not just the `#`. Failure mode: §0.5.97 instance #29.

- **§0.5.111 — Never paste `docker compose config` output to chat.** It displays interpolated variable values inline as plaintext; compose masks `*PASSWORD*`-named keys but not `*USERID*` or others. Use compose config to verify interpolation works (no warnings), then discard or redact before sharing. Failure mode: §0.5.97 instance #30 (paper username leaked once this session).

- **§0.5.112 — Docker `healthcheck` status during `start_period` is misleading.** The container appears "healthy" optimistically until proven otherwise. Trust service-level logs (IBC's "logged in", `API server ... started`) over `docker compose ps` for IB Gateway readiness. Failure mode: §0.5.97 instance #31.

- **§0.5.113 — IB Gateway login failures present as a `GATEWAY`-titled modal.** IBC cannot auto-dismiss it (auto-dismissal is for non-error dialogs). Visual smoking gun via ephemeral `scrot` install inside container. Standing rule: when IB Gateway login appears hung post-Paper-Log-In, take a screenshot before diagnosing — the dialog text is the only place IBKR's actual error message surfaces. Failure mode: §0.5.97 instance #33.

- **§0.5.114 — Whitespace cleanup in `.env` is non-negotiable, not optional.** If the data-formatting bug bites two values, apply the cleanup to ALL values BEFORE proceeding — trailing whitespace is invisible in the editor but real to the consumer. Failure mode: §0.5.97 instance #32.

---

## 1. Where we are (as of handoff, 2026-05-20 ~20:00 UTC)

### Live production state
- **TradeFlow VPS:** Hetzner CPX21 Hillsboro, `5.78.212.37`, user `tradeflow`, host `ubuntu-4gb-hil-1`. Up, healthy at OS level, low load.
- **Container `tradeflow-ib-gateway`:** Running (PID tree alive — Xvfb + IBC start script + socat + Java/IbcGateway), but **IBKR login REJECTED** as "Invalid username or password". API server port 4002 (internal) has never opened; socat keeps logging connection refused every 30s.
- **Capital at risk:** $0. Phase 1 is pre-deployment; no IBKR connection succeeded, no positions, no orders. Botty AI (lineage project) is paused; $1462.80 USDT + ~$543 spot frozen on Binance.
- **Botty AI:** No change this session. Still shut down per HANDOFF_v2.

### What just shipped (commits visible on `main`)
- **`d5ea33a`** — Merge PR #5 (chore: port .claude/skills/ from Botty repo (4 skills)) — **P1 done.** PR #4 was a misroute to a stale feature branch (`claude/phase-0-repo-bootstrap-ZZGJX`); PR #5 corrected with `--base main` pinned.
- **`f29ae56`** — **Merge PR #6 (feat(phase-1): IB Gateway Docker + ib_async client + Supabase REST stub + smoke test)** — Phase 1 PR 2. **CURRENT HEAD of main.** 13 files changed, 775+ insertions / 2-deletions. CI green (`Lint, type-check, and test`).
- **GitHub Rulesets `protect-main`** active on `main`: PR required (0 approvals), required status check `Lint, type-check, and test`, conversation resolution, restrict deletions, block force pushes, no bypassers including operator. **Verified end-to-end** by attempting a direct push to main and watching GH013 rejection fire (Session 3 §0.5.97 instance #26 catch).

### What we discovered this session (verified facts, not yet on main)
- **IBKR paper account: `DUQxxxxxx`** (`DUQ` prefix, NOT `DUO891961` from prior memory).
- **IBKR paper credentials staged on VPS** at `~/.tradeflow-secrets/.env` (mode 600), BUT current values are rejected by IBKR — operator is resetting the paper password as of session close.
- **Symlink** `~/tradeflow/.env -> ~/.tradeflow-secrets/.env` exists on VPS — required because docker-compose `${VAR}` interpolation reads working-directory `.env`, not `env_file:` paths (§0.5.109).
- **gnzsnz/ib-gateway image** is at `ghcr.io/gnzsnz/ib-gateway:stable` (NOT `gnzsnz/ib-gateway:stable` on Docker Hub), shipping IB Gateway 10.45.1f + IBC 3.23.0 as of 2026-05-20.
- **Port mapping:** `127.0.0.1:4002:4004` — gnzsnz image socat-bridges container 4004 to IB Gateway internal 4002. NOT `4002:4002` as my brief originally claimed (CC Web caught this in Task A).
- **`scrot` installed ephemerally in container** (gone on next `docker compose up --build`) — needed for visual debugging of IB Gateway modals.

### What's pending (next session, in order)
1. Confirm IBKR paper password reset propagated; update `IBKR_PASSWORD` in `~/.tradeflow-secrets/.env`.
2. Restart `ib-gateway` container; screenshot to confirm login succeeded (Connection Status panel shows "API Server — connected").
3. Run VPS CC smoke-test runbook (re-derive via `vps-smoke-test-runbook` skill at `.claude/skills/vps-smoke-test-runbook/` in the repo).
4. Iff smoke test PASS → ready for Phase 1 PR 3 (orchestrator) brief drafting.

---

## 2. The session's bug thread

Numbered narrative, including wrong turns. Read this to understand what rabbit holes are closed.

1. **Started 2026-05-20 morning** with v2 handoff loaded. V0–V4 verification pasted from end-of-Session-2; I trusted it without re-probing. (§0.5.97 #23 — see §5.)
2. **P0 (branch protection) closed quickly** — operator used the newer GitHub Rulesets UI per ruleset `protect-main`. I gave a verification curl that hit the wrong API surface (classic vs rulesets — #24); then a corrected curl that needed auth which I didn't realize (#25). Operator's screenshots already proved the state; my API verification was belt-and-suspenders that turned into a 3-wrong-call distraction. Eventually verified operationally via direct-push-to-main probe (GH013 rejection — clean).
3. **P1 first attempt failed.** Drafted PR brief for porting 4 `.claude/skills/` from Botty repo. CC Web shipped PR #4 with `claude/port-botty-skills-HV5NH` → `claude/phase-0-repo-bootstrap-ZZGJX` (a stale feature branch). Branch protection blocked the merge from reaching main. Caught via `git ls-tree origin/main -- .claude/skills/` showing only `README.md`, no SKILL files. (§0.5.97 #26.)
4. **P1 corrected via PR #5** — opened directly from existing `claude/port-botty-skills-HV5NH` branch via GitHub compare URL with **`base: main`** verified. CI green, self-merged. `git ls-tree origin/main -- .claude/skills/` confirmed 8 files (README + 7 ports).
5. **P2 verified via IBKR portal.** Operator confirmed: `DUQxxxxxx` prefix (not `DUO891961` from memory), futures permissions approved, distinct paper username + password set. Memory carried wrong prefix forward; live observation wins. (§0.5.97 #27.)
6. **P3 brief drafted** for Phase 1 PR 2 (IB Gateway Docker + Python clients). Brief intentionally flagged 4 specs for CC Web to verify against upstream rather than trust the brief: gnzsnz image registry, port mapping, env var names, pyproject.toml pre-existence. **All four bakes were wrong in the brief; CC Web's Task A audit caught all four.** This was the discipline working — §0.5.97 functioning as designed. PR #6 shipped 13 files, 775+ insertions, base=main pinned (per §0.5.108 lesson from #26), CI green, self-merged as `f29ae56`.
7. **Post-merge smoke-test bring-up went sideways through 6 sub-issues, each diagnosed via probe→fix→re-probe:**
   - **First container start:** compose warned `IBKR_USERNAME variable is not set` four times. The `env_file:` vs `${VAR}` interpolation distinction (§0.5.97 #28). Fixed by symlinking `~/tradeflow/.env` → `~/.tradeflow-secrets/.env`.
   - **Second start:** compose interpolation worked but TWS_USERID leaked in `docker compose config` stdout (#30). Standing rule added.
   - **`.env` value parsing:** length probe caught `IBKR_PORT=46 chars` and `IBKR_PAPER_ACCOUNT=77 chars` — inline comments from `.env.example` had been left in (#29). Operator manually edited; lengths normalized.
   - **I deferred the sed cleanup as "optional"** thinking velocity > precision; later this was revealed as wrong call (§0.5.97 #32) — should have applied the cleanup to ALL values, not just the two we caught.
   - **Docker `healthcheck: healthy`** reported during start_period made the container LOOK ready; actual IBC logs showed login hung at a dialog (#31). Standing rule added.
   - **Login hung at a modal entitled "Gateway"**. IBC stopped logging after the dialog opened — no IBC log lines for 23+ minutes. Multiple hypotheses (2FA pending, TOTP missing, existing session, security warning). Probes ruled out IBC-dead (process alive), 2FA (operator confirmed no mobile push), TOTP (empty as expected). Container had no screenshot tools (#33 setup). Installed `scrot` ephemerally; screenshot revealed: **"Connection to server failed: Invalid username or password."** The whole 23-min mystery was wrong credentials. (§0.5.97 #33.)
8. **Whitespace cleanup ran post-discovery** — no whitespace found on USERNAME or PASSWORD. So the credentials themselves are wrong, not their formatting.
9. **Operator going to IBKR portal** to reset paper password as of session close. Session 4 picks up there.

**Wrong calls this session: 6.** Same failure mode as Session 2's 5: designing verifiers without enough probing of the verifier itself. Discipline that DID work: probe-before-baking in PR 2 brief caught 4 CC Web bakes. Discipline that FAILED: I still did API-based belt-and-suspenders verification when operator's UI screenshots already proved the state. Lesson hardened in §10.

---

## 3. What the system is actually made of

**Single source of truth:** None yet — `CLAUDE.md` references `docs/architecture/v0_brief.md` which doesn't exist (only `.gitkeep`). Out-of-scope tech debt deferred. This handoff is the best available system doc for TradeFlow.

### Repo state on `main` at `f29ae56`

Top-level layout (from CC Web Task A + post-merge ls-tree):
- `.github/workflows/ci.yml` — `Lint, type-check, and test` job: `ruff check .`, `black --check .`, `mypy config/` (strict, config-only), `pytest -q --cov=config`
- `.claude/skills/{code-pr-brief, prod-debug-discipline, session-handoff-writer, vps-smoke-test-runbook}/{SKILL.md, *_template.md}` — all 4 ported in PR #5
- `.claude/skills/README.md` — pre-existing, now stale ("will be copied... before Phase 1 PR 2"), out-of-scope to fix
- `.env.example` — IBKR_*, SUPABASE_*, TELEGRAM_*, TRADING_MODE, LOG_LEVEL, gateway tuning vars (TWOFA_TIMEOUT_ACTION, RELOGIN_AFTER_TWOFA_TIMEOUT, AUTO_RESTART_TIME, TIME_ZONE, READ_ONLY_API, BYPASS_WARNING) + healthcheck/smoke client-id slots
- `CLAUDE.md` — Phase 0 stub; references non-existent `docs/architecture/v0_brief.md` (out-of-scope)
- `config/` — pre-existing module from PR #1 / Phase 0 (4 config tests in `tests/test_config.py` — pre-existing, untouched in PR #6)
- `docker-compose.yml` — defines `ib-gateway` service (gnzsnz image, port 4002:4004, env_file + environment block, TCP healthcheck)
- `docs/handoffs/HANDOFF_v1.md`, `HANDOFF_v2.md` — committed; HANDOFF_v3.md is this doc, pending publish
- `docs/architecture/.gitkeep` — placeholder; no actual architecture doc
- `main.py` — 32-line argparse banner stub (Phase 0; gets wired in Phase 1 PR 3)
- `pyproject.toml` — Python >=3.11,<3.13; deps: ib-async, httpx, python-dotenv; dev: pytest, pytest-asyncio, black, ruff, mypy; tool configs for ruff/black/mypy/pytest
- `scripts/{__init__.py, healthcheck.py, test_ib_connect.py}` — smoke test + 2-layer (TCP + ib_async) healthcheck
- `src/__init__.py`, `src/clients/{__init__.py, ib_client.py, supabase_client.py}` — IBClient (ib_async wrapper, factory injection point), SupabaseClient (httpx REST wrapper, custom client per §0.5.T4)
- `supabase/schema.sql` — 5 tables (`trades`, `positions`, `daily_summary`, `kill_switch_events`, `signals`), NOT yet applied to any Supabase project
- `tests/{conftest.py, test_config.py, test_healthcheck.py, test_ib_client.py, test_supabase_client.py}` — 30 passing tests (4 pre-existing config + 26 new from PR #6)

### VPS state
- `~/tradeflow` — git repo, working tree clean at `f29ae56`
- `~/tradeflow/.env` — **symlink** → `~/.tradeflow-secrets/.env` (required for compose interpolation; §0.5.109)
- `~/.tradeflow-secrets/` — mode 700, owner tradeflow
- `~/.tradeflow-secrets/.env` — mode 600, populated with IBKR creds; **password currently rejected by IBKR**
- `~/.tradeflow-secrets/.env.bak-<TIMESTAMP>` — at least one whitespace-cleanup backup
- `~/.claude/settings.json` — 194 allow / 72 deny / `defaultMode: "default"` (from Session 2 comprehensive sweep; unchanged this session)
- `~/.claude/settings.json.bak-20260520T012729Z` — Session 2 backup
- `~/tradeflow/.claude/settings.local.json` — project-scoped Read(.claude/**) allow

### Container state (`tradeflow-ib-gateway`)
- Image: `ghcr.io/gnzsnz/ib-gateway:stable` — IB Gateway 10.45.1f, IBC 3.23.0
- Port: `127.0.0.1:4002 -> 4004/tcp` (loopback bind)
- env_file mount: `/home/tradeflow/.tradeflow-secrets/.env`
- Processes alive: bash run.sh, Xvfb :1, IBC start script, run_socat.sh, socat, Java/IbcGateway
- **API port 4002 (internal) NOT OPEN** — login rejected, IBC stuck at error dialog
- Ephemeral install: `scrot` (for screenshot debugging) — gone on next container rebuild

---

## 4. Verified facts (2026-05-20) — DO NOT CHALLENGE unless re-probed

**Carry-forward from v2 (still verified):**
- `ib_async` is the active fork of `ib_insync` (§0.5.T2); pip name `ib_async`, import `from ib_async import IB, ...`
- MNQ contract spec from SeanBot (§0.5.97-locked from v2): TICK_SIZE=0.25, MULTIPLIER=$2/point, COMMISSION_RT=$0.62, MARGIN_INTRADAY=$2000, CME maintenance ~$3636. Quarterly Mar/Jun/Sep/Dec, expiry 3rd Friday, roll ~8 days prior.
- IB Gateway paper API port is **4002** (NOT 7497 — that's TWS paper).
- VPS Python is 3.10.12; container Python is 3.11 (pyproject.toml `requires-python = ">=3.11,<3.13"`). Container is source of truth (§0.5.T5).

**New verified facts from Session 3:**

- **IBKR paper account ID: `DUQxxxxxx`** (prefix `DUQ`, NOT `DUO891961` as userMemory carried from v1/v2-era sessions). Operator verified via IBKR Client Portal, 2026-05-20. Live observation wins per §0.5.95. (§0.5.97 instance #27.)
- **IBKR paper credentials require explicit reset** if they don't match what IBKR has on file. The "Invalid username or password" failure mode this session is being resolved by operator-side portal password reset. Source of truth is the IBKR portal, not memory of what was set up.
- **gnzsnz/ib-gateway image registry:** `ghcr.io/gnzsnz/ib-gateway`, NOT `gnzsnz/ib-gateway` (GHCR, not Docker Hub).
- **gnzsnz/ib-gateway port mapping:** container exposes paper API on `4004` (socat bridge from IB Gateway's internal `4002`). Host-side compose mapping is `127.0.0.1:4002:4004`. The host-facing port is 4002 (what the smoke test connects to); the container-internal API is 4002 (what IB Gateway opens); socat bridges 4002→4004 inside the container.
- **gnzsnz/ib-gateway canonical env vars** (verified from upstream README during PR #6 Task A): `TWS_USERID`, `TWS_PASSWORD`, `TRADING_MODE`, `TWOFA_TIMEOUT_ACTION`, `RELOGIN_AFTER_TWOFA_TIMEOUT`, `AUTO_RESTART_TIME`, `TIME_ZONE`, `READ_ONLY_API`, `BYPASS_WARNING`, `EXISTING_SESSION_DETECTED_ACTION`. Repo env-var convention is `IBKR_*` (§0.5.T1); compose maps `IBKR_*` → `TWS_*` inside the gateway service only.
- **Docker-compose interpolation source ≠ env_file:** `env_file:` loads into container; `${VAR}` interpolation reads from shell env or working-directory `.env`. Symlink `~/tradeflow/.env → ~/.tradeflow-secrets/.env` resolves this. (§0.5.109)
- **GitHub Rulesets** is the active branch protection mechanism on `main` (not classic protection). Ruleset `protect-main` is ID-stable; verify via `gh api /repos/ohad-oren111/tradeflow/rules/branches/main` (auth required) or by attempted direct push (GH013 rejection).
- **Test count:** 30 (4 pre-existing config tests + 26 added by PR #6). All passing locally and in CI.
- **CI workflow runs:** `ruff check .`, `black --check .`, `mypy config/` (strict, config-only — does NOT cover `src/` or `scripts/` yet), `pytest -q --cov=config`. CI is path-filter-free; runs on all PRs to main including docs-only.
- **`.env` file format is strict KEY=VALUE.** No inline comments after the value. (§0.5.110)
- **Docker `healthcheck` during start_period defaults to "healthy" optimistically** — does NOT mean the service has started. (§0.5.112)

---

## 5. Wrong diagnoses (this session) — READ BEFORE YOU DEBUG

Eleven §0.5.97 instances logged. Six were wrong calls by me; five were verified-fact corrections caught by probes. The wrong calls are the operationally important ones to internalize.

### Wrong calls (6, in order)

**§0.5.97 instance #23 (Wrong call #1)** — Trusted a verification block pasted at top of kickoff context over the kickoff doc's explicit current-HEAD claim, without asking the operator for a fresh real-time probe to break the tie. The pasted block was a stale snapshot. Lesson: pasted probe output in a kickoff is a snapshot, not a guarantee of current state; when current state matters and there's a competing claim, run V1 first thing in real-time.

**§0.5.97 instance #24 (Wrong call #2)** — Gave the operator a verification curl against `/repos/{owner}/{repo}/branches/{branch}` to confirm protection, but they used the newer Rulesets path (preferred per GitHub). The classic protection endpoint returns nulls when a branch is protected by rulesets only — they're separate systems. Lesson: anticipate which API surface mirrors the path the user actually took; for rulesets, query `/rules/branches/{branch}` or `/rulesets`.

**§0.5.97 instance #25 (Wrong call #3)** — Assumed GitHub's rulesets API endpoints work unauthenticated for public repos. They require auth even on public repos. Earlier curl to `/branches/main` worked anonymously because classic branch-protection metadata is exposed anonymously for public repos; rulesets data is admin-tier and gated. Lesson: probe the API surface with a no-jq curl first when reaching for a new endpoint; let the response shape inform the verifier.

**Meta-pattern for #23–#25:** Session 3 made 3 wrong calls in the verification path for P0, all the same family as Session 2's 5 wrong calls — designing verifiers without enough probing of the verifier itself. The actual P0 enforcement landed cleanly on the first try via the operator's UI screenshots; my API-based verification was the noise. **Lesson:** when the user has already done the work via UI with confirmation banners, an API verification is belt-and-suspenders, not load-bearing — and adds risk-of-wrong-probe. Trust the screenshots; the push-rejection probe is the actual operationally meaningful test.

**§0.5.97 instance #26 (Wrong call #4)** — Brief delegated PR creation without explicitly pinning `--base main`. CC Web defaulted to its prior working branch (`claude/phase-0-repo-bootstrap-ZZGJX` from PR #1), shipped the merge into that dead-end branch. Branch protection caught it (main untouched), but the skills are stranded on a non-main branch. Lesson: every PR brief delegating to CC Web must specify base branch explicitly. Codified in §0.5.108. Add to `code-pr-brief` template's gotchas.

**§0.5.97 instance #28 (Wrong call #5)** — PR 2 brief baked a docker-compose pattern where `env_file:` loads secrets into the container AND `environment:` block uses `${IBKR_*}` interpolation to translate to `TWS_*`. The interpolation doesn't read from env_file paths — it reads from shell env or the working-directory `.env`. Result: container got blank `TWS_USERID`/`TWS_PASSWORD`, gateway can't log into IBKR. Lesson: in any compose file using BOTH `env_file:` and `${VAR}` interpolation against the same secrets file, document the requirement that the interpolation source must be set independently (shell or working-dir `.env`). Better pattern: use ONE mechanism, not both. Codified in §0.5.109.

**§0.5.97 instance #32 (Wrong call #6)** — Skipped a deferred `.env` whitespace cleanup citing "velocity over precision" — turned out the trailing-comment-residue pattern that caught IBKR_PORT and IBKR_PAPER_ACCOUNT also could have affected IBKR_PASSWORD (length 19 unverified for trailing junk). Lesson: when a data-formatting bug bites two values, apply the cleanup to ALL values in the file BEFORE proceeding, not just the two you caught. Trailing whitespace is invisible in the editor but real to the consumer. Codified in §0.5.114.

### Verified-fact corrections (5, caught by probes — these are the WINS this session)

**§0.5.97 instance #27** — IBKR paper account ID format is `DUQxxxxxx`, NOT `DUO891961` as the userMemory carried from prior sessions. Operator verified via IBKR portal, 2026-05-20. Live observation wins per §0.5.95.

**§0.5.97 instance #29** — `.env` file values cannot have inline comments after `=`. Operator's nano edit left descriptive text trailing some values, producing lengths 46 and 77 for IBKR_PORT and IBKR_PAPER_ACCOUNT. Caught by the length-probe-before-proceeding pattern. Lesson codified in §0.5.110.

**§0.5.97 instance #30** — `docker compose config` displays interpolated variable values inline as plaintext (compose masks `*PASSWORD*`-named keys but not `*USERID*`). Standing rule codified in §0.5.111.

**§0.5.97 instance #31** — Docker `healthcheck` status during `start_period` defaults to "healthy" optimistically; a "healthy" container during boot does NOT mean the underlying service has started. For IB Gateway specifically, trust the IBC logs (look for "logged in" / "API server ... started") over `docker compose ps`. Lesson codified in §0.5.112.

**§0.5.97 instance #33** — IB Gateway login failures present as a `GATEWAY`-titled modal that IBC cannot auto-dismiss because IBC's auto-dismissal logic is for non-error dialogs only. Visual smoking gun: screenshot via ephemeral `scrot` install inside container. Standing rule codified in §0.5.113.

### Lesson for Session 4

**Every wrong call this session involved designing a verifier without enough probing of the verifier itself.** Read sections §0.5.106 through §0.5.114 before any verification work in Session 4. When the operator has a working UI confirmation, prefer operationally meaningful tests (try-and-watch-it-fail) over API readout — the API readout adds new failure modes (auth, endpoint mismatch, response-shape assumptions) without proving anything new.

---

## 6. Verification block — run this first in Session 4

**V0 — Workstation + VPS sanity**
```bash
ssh tradeflow 'whoami && hostname && date -u'
# Expect: tradeflow @ ubuntu-4gb-hil-1 @ UTC sometime after 2026-05-20 ~20:00
```

**V1 — main HEAD is at the v3 publish commit (or a descendant)**
```bash
ssh tradeflow 'cd ~/tradeflow && git fetch origin main && git rev-parse origin/main && git log -5 --oneline origin/main'
# Expect HEAD = the merge commit of the v3 handoff PR (will be created during publish — see §16),
# descended from f29ae56 (Merge PR #6) -> d5ea33a (Merge PR #5) -> 73da9ca (v2 handoff) -> d956fa2 (v1 handoff) -> ce3a158 (CI workflow PR #3) -> 14fb5e4 (initial scaffold PR #1)
```

**V2 — branch protection still active on main**
```bash
ssh tradeflow 'cd ~/tradeflow && git checkout -b __probe_protection_$$ origin/main && echo "test" > /tmp/__probe_test && git config user.email "probe@local" && git config user.name "probe" && touch docs/__probe.tmp && git add docs/__probe.tmp && git commit -m "probe: branch protection (expect rejection)" && git push origin __probe_protection_$$:main 2>&1; echo "---exit:$?---" && git reset --hard origin/main && rm -f docs/__probe.tmp && git checkout main && git branch -D __probe_protection_$$'
# Expect: push rejected with GH013 violations including "Changes must be made through a pull request" and "Required status check 'Lint, type-check, and test' is expected". Exit 1. Local repo restored cleanly.
```

**V3 — IBKR creds staged + .env file health**
```bash
ssh tradeflow 'ls -la ~/.tradeflow-secrets/.env && for k in IBKR_USERNAME IBKR_PASSWORD IBKR_PAPER_ACCOUNT IBKR_HOST IBKR_PORT IBKR_TOTP_SECRET TRADING_MODE; do v=$(grep -E "^${k}=" ~/.tradeflow-secrets/.env | head -1 | cut -d= -f2-); if [ -n "$v" ]; then echo "${k}: length=${#v}"; else echo "${k}: EMPTY"; fi; done && ls -la ~/tradeflow/.env'
# Expect:
#   /home/tradeflow/.tradeflow-secrets/.env  mode -rw-------  owner tradeflow
#   IBKR_USERNAME: length=N (e.g. 15 — verify against IBKR portal value)
#   IBKR_PASSWORD: length=N (verify against IBKR-shown post-reset value)
#   IBKR_PAPER_ACCOUNT: length=9-10 (DUQxxxxxx)
#   IBKR_HOST: length=9 (127.0.0.1) OR length=10 (ib-gateway)
#   IBKR_PORT: length=4 (4002)
#   IBKR_TOTP_SECRET: EMPTY (or length=N if operator enabled TOTP-based 2FA)
#   TRADING_MODE: length=5 (paper)
#   ~/tradeflow/.env -> /home/tradeflow/.tradeflow-secrets/.env (symlink)
# If IBKR_PASSWORD length is the SAME as the failed-Session-3 value (19), operator hasn't reset yet — pause.
```

**V4 — IB Gateway container state**
```bash
ssh tradeflow 'cd ~/tradeflow && docker compose ps && echo "--- recent log tail ---" && docker logs tradeflow-ib-gateway --tail 30 2>&1 | grep -iE "logged in|api.*server|started|exception|fail|timeout|invalid|password" | tail -10'
# Expect (after password reset + restart):
#   tradeflow-ib-gateway "Up X minutes (healthy)" — and crucially:
#   Log contains "API server version XXX started" or "Logged in"
#   NO "Invalid username or password" lines
#   NO repeated "socat connection refused" lines after the first few seconds of boot
# If still "Invalid username or password": operator's reset didn't propagate, or new password wrong on paste.
```

**V5 — IB Gateway login screenshot (operationally meaningful)**
```bash
ssh tradeflow 'docker exec -u root tradeflow-ib-gateway sh -c "apt-get install -y -qq scrot 2>&1 | tail -3" && docker exec tradeflow-ib-gateway sh -c "DISPLAY=:1 scrot -z /tmp/screen.png && ls -la /tmp/screen.png" && docker cp tradeflow-ib-gateway:/tmp/screen.png /tmp/screen.png && ls -la /tmp/screen.png'
# Then from laptop:
#   scp tradeflow:/tmp/screen.png ~/Downloads/gateway_screen_v4_v5.png
#   Open the image — expect: "Connection Status / API Server: connected" (green or no error styling),
#   NO error dialog visible.
```

If V0–V5 all green, proceed to §15 step 3 (smoke test runbook). If anything red, fix that first.

---

## 7. Pending work queue

### Immediate (Session 4 priorities, in order)

**P0 — IBKR paper login: green light** (operator-side, ~10-30 min)
- IBKR portal: reset paper password (operator started this at session close)
- Update `IBKR_PASSWORD` in `~/.tradeflow-secrets/.env` with reset value (no trailing whitespace, no inline comments — §0.5.110, §0.5.114)
- Restart container: `docker compose down ib-gateway && docker compose up -d ib-gateway && sleep 90`
- Screenshot confirm: API Server status panel shows "connected" (V5)
- Decision tree if still "Invalid":
  - Wait 5 min for IBKR propagation, retry once
  - If still fails, wait 30 min for soft-lockout to clear (multiple failed attempts in Session 3 may have triggered)
  - If still fails after that, re-verify username casing in IBKR portal (§0.5.97 #33 pattern says screenshot first)

**P1 — VPS CC smoke-test runbook execution** (~10 min after P0)
- Re-derive runbook in Session 4 via the now-ported `vps-smoke-test-runbook` skill at `.claude/skills/vps-smoke-test-runbook/SKILL.md` + `runbook_template.md`. (Earlier draft from Session 3 is at `/mnt/user-data/outputs/PR6_smoke_test_runbook.md` locally on operator's laptop if still available.)
- Paste runbook to VPS CC; it executes §1–§7 end-to-end
- Operator reads §7 structured report (PASS/FAIL/INVESTIGATE)
- If PASS → P2
- If FAIL → diagnose using `prod-debug-discipline` skill

**P2 — Phase 1 PR 3 brief: orchestrator wiring** (~30-60 min drafting)
- Use `code-pr-brief` skill (now in repo at `.claude/skills/code-pr-brief/`)
- Scope: wire `IBClient` + `SupabaseClient` to `main.py`; basic event loop; logging; graceful shutdown on SIGTERM
- Reference: SeanBot's orchestrator patterns (operator has access; brief should require Task A audit of SeanBot if available, otherwise idiomatic asyncio loop)
- Pin `--base main` explicitly in the brief (§0.5.108)
- NO trading logic yet — that's PR 4+ (state machine + SMA100-bounce strategy)

### Lower-priority / deferred

- Stale `CLAUDE.md` reference to `docs/architecture/v0_brief.md` (doesn't exist) — defer to a docs-cleanup PR after Phase 1 is functional
- `pyproject.toml` uses version ranges (`>=X,<Y`) rather than exact pins — pre-existing, works fine, defer
- Add `vncpasswd` + VNC port mapping to docker-compose for visual gateway debugging — defer until needed for ongoing operational debugging (one-off scrot install worked this session)
- Apply `supabase/schema.sql` to a real Supabase project — needed before PR 3+ does actual DB writes; defer until orchestrator is wired
- 30-day post-TradeFlow-live cointegrated stat-arb pivot re-evaluation (Botty lineage; carry-forward from v2) — defer until TradeFlow is live + stable

### Operational debt
- `~/.tradeflow-secrets/.env.bak-<TIMESTAMP>` backups accumulating; trim periodically
- Container has `scrot` installed ephemerally — gone on next `docker compose up --build`. Re-install per V5 if needed for screenshot debugging
- `IBKR_TOTP_SECRET` is empty; if paper account ever gets enrolled in TOTP-based 2FA, populate from IBKey base32 seed

---

## 8. Test safety — why we belabor this (carry-forward)

Failures from prior projects (Botty AI lineage) that the master template's guardrails prevent:
1. Tests passed against a fictional schema because they mocked column names — never mock at the wrong layer
2. `side_effect` list off-by-one → silent `StopIteration` → wrong assertions
3. Mocked at raw library chain (`supabase.table().upsert().execute()`) when code uses a wrapper (`self._db.upsert`) → tests green, prod broken
4. Shared `MagicMock()` state leaked between tests — always use fixture factories
5. Async decorator assumption (`@pytest.mark.asyncio` when project uses `asyncio_mode = "auto"`)
6. Wrapper SYNC vs async confusion
7. Tests passed black/ruff but mypy strict caught a real bug — keep mypy strict

**TradeFlow specifically: PR #6 established the test pattern** (factory fixtures in `tests/conftest.py`; wrapper-level mocking via `ib_factory` / `http_client` injection points on `IBClient` / `SupabaseClient`). 30 tests pass. Carry this pattern forward into PR 3+. Do NOT introduce `patch()` on module-level factories — use injection.

Guardrails are in the `code-pr-brief` skill template `tests/conftest.py` example. Read before writing tests.

---

## 9. Pitfalls from prior sessions (carry-forward + Session 3 additions)

Things to NOT trust without re-verification:

**From v2 (carry-forward):**
- "State machine self-cleared zombies" — wrong, needed manual delete (Botty)
- Grep patterns missing writers because only one syntax form matched
- Handoff numbers (orphan counts, position counts) often stale — re-query
- "Pip3 is installed on VPS" — false; use `python3 -m pip`
- `hello-world:latest` docker image present (harmless leftover; delete with `docker rmi hello-world`)
- `docs/v0_brief.md` referenced in CLAUDE.md doesn't exist (Session 3 confirms: it's `docs/architecture/v0_brief.md` per PR #6 audit; still doesn't exist)
- CC Web UI "merged" signal can lie — verify via `git rev-parse origin/main` and `git ls-tree`
- CC Web sandbox branch state persists across PRs in the same task
- Heredoc paste mangles long content — write files directly via tools, don't shell-heredoc

**New from Session 3:**
- **Pasted verification block in kickoff is a snapshot** — not current state (§0.5.106)
- **Classic vs Rulesets branch protection** — different APIs, different auth requirements (§0.5.107)
- **CC Web defaults to working-branch base** — pin `--base main` explicitly (§0.5.108)
- **docker-compose env_file: ≠ ${VAR} interpolation** — different mechanisms (§0.5.109)
- **`.env` is strict KEY=VALUE** — no inline comments (§0.5.110)
- **`docker compose config` leaks values to stdout** — don't paste (§0.5.111)
- **`docker compose ps healthy` during start_period is optimistic** — trust service logs (§0.5.112)
- **IB Gateway login errors show in a "GATEWAY" modal** — screenshot first (§0.5.113)
- **Whitespace cleanup applies to ALL values** — not just the ones you caught (§0.5.114)

**Next session rule: if a claim is quantitative or stateful, re-verify it.** Especially: HEAD commit, container health, login state, .env file lengths, branch protection effective rules, IBKR portal credentials. Memory wins zero battles against probes.

---

## 10. Session 3 discipline lesson (2026-05-20)

**Headline:** 6 wrong calls in one session. All same family as Session 2's 5: designing verifiers without enough probing of the verifier itself. Three were in P0 verification (#23, #24, #25 — API surface mismatches); one was in PR delegation (#26 — wrong base); two were in PR 2 bring-up (#28 env_file vs interpolation, #32 skipped cleanup).

**The discipline that DID work this session:** PR 2 brief explicitly flagged 4 specs for CC Web to verify against upstream (gnzsnz image registry, port mapping, env var names, pyproject existence). All 4 bakes in the brief were wrong; CC Web caught all 4 via Task A audit. **This is §0.5.97 functioning exactly as designed.** Net: my wrong bakes cost zero because the audit-first discipline absorbed them.

**The discipline that FAILED this session:** I kept reaching for API-based belt-and-suspenders verification when the operator's UI screenshots already proved the state. P0 was clean on the first try via the UI; my three subsequent API curls were noise that added new failure modes (wrong endpoint, wrong auth assumption).

### Enforcement rules for Session 4

1. **When UI confirmation already proves the state, do NOT add an API verifier.** The operationally meaningful test (try the action and watch it fail/succeed) beats an API readout.
2. **Every PR brief pins `--base main`** in three places: architecture constraints, gotchas, pre-push checklist. (Codified §0.5.108.)
3. **Every PR brief explicitly says "verify these against upstream in Task A; do not bake from this brief"** for: external-service env var names, image registry/tag, port/socket conventions, config file syntax for external tools.
4. **Probe verifiers before trusting them.** When adding a new diagnostic curl/command, run it raw first (no jq, no pipes) and inspect the response shape. Then add the parsing.
5. **Apply data-formatting cleanups to ALL values when you find the pattern**, not just the values you happened to catch.

---

## 11. Logging verbosity — what to demand from PR 3+

Standing principles (carry-forward + extended):
- Module-level `LOGGER = logging.getLogger(__name__)` — no `print()` outside `__main__` blocks
- Format: `[component] action — reason` (e.g. `[ib_client] connect attempt — host=%s port=%s client_id=%s`, args)
- Use `%s` placeholders in logger calls, NOT f-strings (per Python logging best practice — avoids formatting work when level suppressed)
- Async functions log entry AND exit on the I/O boundary
- Every state transition logs `old → new` at INFO
- Every swallowed exception logs the specific error type + message + context
- Retry loops log attempt number + reason
- Any dedup / select-one-of-many logs which row won and why

PR 3 (orchestrator) MUST log: every IB Gateway reconnect attempt, every supabase upsert, every state-transition, every shutdown signal received.

---

## 12. Master template — use for every Claude Code PR

Use the `code-pr-brief` skill at `.claude/skills/code-pr-brief/SKILL.md` + `pr_brief_template.md` (now in repo as of PR #5). The template enforces: patch constraints, code quality, test safety guardrails, known gotchas, "what I got wrong" section.

**New PR 3+ requirement (Session 3 lesson):** every brief must explicitly pin `--base main` on `gh pr create` AND list it as a Pre-Push Checklist item.

---

## 13. Current PR brief in flight — none

No PR brief pending Session 4 start. PR #6 is merged. Session 4's first technical action is the smoke-test runbook (per §15), not a new PR brief.

PR 3 (orchestrator) brief drafting starts AFTER the smoke test returns PASS — see §15 step 5.

---

## 14. Canonical references (authority hierarchy)

1. **Source code on `origin/main`** at the v3 publish commit (descendant of `f29ae56`) — what actually runs / is meant to run
2. **`docs/handoffs/HANDOFF_v3.md`** on `origin/main` (this doc, after publish) — session context, NOT long-term authority
3. **IBKR Client Portal** (https://www.interactivebrokers.com/sso/Login) — TRUTH for account ID, paper credentials, futures permissions, account status
4. **IB Gateway / ib_async API** via paper port 4002 — truth for connection state, positions, open orders, server version (live runtime probe)
5. **gnzsnz/ib-gateway upstream docs** (https://github.com/gnzsnz/ib-gateway-docker) — truth for image env vars, version, port semantics
6. **GitHub Rulesets API** (`gh api /repos/ohad-oren111/tradeflow/rules/branches/main`, authenticated) — truth for active branch protection rules
7. **Operational push-rejection probe** (try direct push to `main`, watch GH013 fire) — operationally meaningful test of branch protection
8. **HANDOFF_v2.md** and earlier — historical context; ignore any claim that contradicts items 1–7

---

## 15. First 15 minutes of Session 4

1. **Read §0.5, 1, 2, 5, 9 of this handoff.** §5 is the single most important — 11 §0.5.97 instances, 6 wrong calls, all same failure mode (verifier design without probing). Internalize before any debugging.
2. **Run §6 verification block V0–V4.** Specifically:
   - V1 — confirm `origin/main` HEAD has advanced past `f29ae56` to the v3 handoff publish commit
   - V3 — confirm `~/.tradeflow-secrets/.env` has the reset IBKR_PASSWORD value (length differs from Session 3's failed value, or operator confirms reset)
   - V4 — check whether gateway container is logging "Invalid username or password" still, or has cleared
3. **If V4 still shows the invalid-credentials error:** ask operator to confirm IBKR portal password reset status (may need to wait for propagation or re-reset). Do NOT restart the container repeatedly — that risks soft-lockout. Wait 5 min between attempts.
4. **Once V4 shows "API server XXX started" (or no error):** run V5 (screenshot probe) to visually confirm — Connection Status panel green.
5. **Re-derive VPS CC smoke-test runbook** using `vps-smoke-test-runbook` skill at `.claude/skills/vps-smoke-test-runbook/`. Hand to VPS CC. Read its §7 report. If PASS, P1 is closed and Phase 1 PR 2 is operationally verified.
6. **Draft Phase 1 PR 3 brief (orchestrator wiring)** using `code-pr-brief` skill. Scope: `main.py` event loop, `IBClient` + `SupabaseClient` wiring, graceful SIGTERM, verbose logging. NO trading logic (that's PR 4+). **Pin `--base main` explicitly** per §0.5.108.

---

## 16. How to publish this handoff (Session 3 close — branch protection ACTIVE)

**Branch protection on `main` is now active** (PR #5 + PR #6 both used the protected flow). Direct push to `main` no longer works. The handoff publication MUST go through a PR.

### Path A (preferred) — Direct VPS push to feature branch + PR

This avoids CC Web ceremony for what's just a docs file. Operator runs these on laptop:

**Step 1 — scp handoff to VPS:**
```bash
scp ~/Downloads/HANDOFF_v3.md tradeflow:~/tradeflow/docs/handoffs/HANDOFF_v3.md
```
Expected: `HANDOFF_v3.md  100%  <size>`

**Step 2 — VPS: create feature branch, commit, push, open PR:**
```bash
ssh tradeflow '
cd ~/tradeflow
git fetch origin main
git checkout main
git pull --ff-only origin main
git checkout -b docs/v3-handoff
git add docs/handoffs/HANDOFF_v3.md
git status
git diff --cached --stat
git config user.email "ohad@tradeflow.local"
git config user.name "ohad-oren111"
git commit -m "docs: add v3 handoff (P0-P3 closed, PR #6 merged, smoke test blocked on IBKR creds)"
git push origin docs/v3-handoff
echo "---"
echo "Open PR at: https://github.com/ohad-oren111/tradeflow/compare/main...docs/v3-handoff"
'
```
Expected: push succeeds; printed URL is the GitHub compare page for the new PR.

**Step 3 — open PR in browser (one click, verify base=main):**

Go to: `https://github.com/ohad-oren111/tradeflow/compare/main...docs/v3-handoff`

- **Verify "base: main"** in the dropdown (PR #4 failure mode — don't skip)
- Title: `docs: add v3 handoff (P0-P3 closed, PR #6 merged, smoke test blocked on IBKR creds)`
- Body (paste):
  ```
  Adds HANDOFF_v3.md to docs/handoffs/.

  Captures Session 3 (2026-05-20): P0 (branch protection) → P3 (Phase 1 PR 2 merged) all closed. Documents the IB Gateway smoke-test bring-up which is currently blocked on IBKR paper credential rejection — operator-side IBKR portal password reset is pending.

  11 new §0.5.97 instances logged (#23-#33). 6 wrong calls by Claude this session, same family as Session 2's 5. New standing rules ratified §0.5.106-§0.5.114.

  Self-merge (approvals=0) once `Lint, type-check, and test` is green.
  ```
- Click **Create pull request**
- Wait for CI to go green (`Lint, type-check, and test` — runs on docs-only PRs per Session 3 verification)
- Click **Merge pull request** → **Confirm merge**

**Step 4 — verify on `main`:**
```bash
ssh tradeflow 'cd ~/tradeflow && git fetch origin main && git log origin/main -3 --oneline && git ls-tree origin/main -- docs/handoffs/'
```
Expected:
- Top commit is the merge of `docs/v3-handoff` (or the squash, depending on merge style)
- `docs/handoffs/` lists `HANDOFF_v1.md`, `HANDOFF_v2.md`, `HANDOFF_v3.md` (+ `.gitkeep`)

**Step 5 — clean up local laptop:** the downloaded `HANDOFF_v3.md` on laptop is no longer canonical — repo copy is. Delete or archive locally.

### Path B (fallback) — VPS CC brief

If Path A fails or operator prefers delegation, hand this to VPS CC:

```
You are VPS Claude Code on tradeflow VPS (5.78.212.37, user tradeflow). 

The operator has scp'd HANDOFF_v3.md to ~/tradeflow/docs/handoffs/HANDOFF_v3.md.

Verify the file exists, then:
  cd ~/tradeflow
  git fetch origin main
  git checkout main
  git pull --ff-only origin main
  git checkout -b docs/v3-handoff
  git add docs/handoffs/HANDOFF_v3.md
  git status  # confirm only HANDOFF_v3.md staged
  git commit -m "docs: add v3 handoff (P0-P3 closed, PR #6 merged, smoke test blocked on IBKR creds)"
  git push origin docs/v3-handoff

Then output the GitHub compare URL: https://github.com/ohad-oren111/tradeflow/compare/main...docs/v3-handoff

Confirm the file exists, the commit landed, the push succeeded. DO NOT open the PR yourself — the operator opens it in browser to verify base=main visually.

DO NOT push directly to main — branch protection blocks it.
```

---

*End of handoff v3. Target lifespan: until Phase 1 PR 3 (orchestrator) merges and the system has been stable for 24 hours. Then HANDOFF_v4 captures the next slice and v3 becomes historical.*
