# TradeFlow — Handoff v2 (P-1 closed, P0–P3 queued, no functional code change)

*Handoff from end of 2026-05-20 ~01:30 UTC. **TradeFlow is still pre-deployment — no live container, no broker connection, no positions.** Session 2 was a permission-rules-only session: comprehensive sweep of VPS CC `~/.claude/settings.json` landed (145/57 → 194/72). No application code changed. Branch protection on `main` is **still pending** the operator's GitHub UI step (P0 from v1 carries forward unchanged). Botty AI is paused (v1 §1 carries forward unchanged).*

---

## 0. How to use this doc

Read sections **0.5, 1, 4, 5, 15** first — that's the state-of-the-system as of handoff. Sections 2–14 are reference material. Section 14 is the source-of-truth ranking when this handoff disagrees with itself or a live observation: `main` branch on `github.com/ohad-oren111/tradeflow` at commit `d956fa2` or later.

**Do not trust this doc alone.** Run the verification block in §6 before writing any code. **There is no live trading bot yet — Botty AI is shut down, TradeFlow has no broker connection. No urgency-driven actions are warranted.** Read **§5 (wrong diagnoses)** before doing any further permission-rule work — Session 2 made 5 wrong calls in the same family.

---

## 0.5 Standing rules (permanent — do not remove from handoff)

### Carry-forward from Botty AI lineage (§0.5.92–.98)

**§0.5.92 Copy-paste instruction style.** Every action recommended to the owner must be a copy-paste-ready bash block. Self-contained commands, chained with `&&` or grouped. Source env vars explicitly in the same block. Expected output described immediately below each block, plus a decision tree if more than one branch matters. No "you might want to..." — either give the command or don't mention it.

**§0.5.93 Learning-delivery discipline.** Every time you learn something new — a bug pattern, a corrected assumption, an environmental fact, a diagnostic finding — surface it immediately in the chat, formatted as a markdown snippet the operator can paste verbatim into the running handoff queue. Do not wait until end-of-session.

**§0.5.94 Read before diagnosing.** When debugging a complex state bug, read the full startup log and 3–5 full cycle narratives before proposing a root cause. Diagnosing from `grep | wc -l` summaries is the #1 cause of wrong diagnoses.

**§0.5.95 Verify severity against the source of truth.** Before escalating urgency language ("capital at risk", "churning fees", "spiraling"), hit the source of truth — live API (IBKR via `ib_async` for TradeFlow), live DB (Supabase REST for TradeFlow), raw log file — not aggregated metrics.

**§0.5.96 Always draft a VPS smoke test runbook after PR merge** unless explicitly told otherwise. The operator does not run smoke tests by hand. CC Web ships PRs; VPS CC runs the smoke test runbook end-to-end and produces a §7 structured report.

**§0.5.97 Probe external specs against the source before baking into briefs.** Broker contracts, exchange fees, library API surfaces, schema column names, Hetzner SKU availability, **CC permission rule grammar** — all of these have drifted between memory and live reality. Any fact about an external system gets a quick verification probe *before* it appears in a brief, runbook, or PR. New instances #21 and #22 logged this session — see §10.

**§0.5.98 Broker/exchange state is ground truth, not internal DB tables.** For position, fill history, or capital claims, the source of truth is the broker API (`ib_async` for IBKR), not the project's persistence layer.

### TradeFlow-specific (§0.5.T1–T5)

**§0.5.T1–T5 are defined verbatim in `TRADEFLOW_SESSION_1_KICKOFF.md`.** Carry forward as-is.

### Session 1 ratified (§0.5.99–.102)

**§0.5.99 VPS CC sessions launch from inside the project root.** `cd ~/tradeflow && claude`.

**§0.5.100 Runbooks prefer `git -C <path>` over `cd <path> && git ...`.** CC has a hardcoded directory-hook safety check on `cd <dir> && ...` patterns, independent of `permissions.allow` rules.

**§0.5.101 Bash permission patterns must account for arbitrary flag ordering.** Positional rules fail when flags vary. Prefer broad allow patterns paired with targeted deny rules.

**§0.5.102 Confirm CC Web merges via `git rev-parse origin/main`, not UI signal.** The "merged" badge can lie.

### Session 2 ratified (§0.5.103–.105)

**§0.5.103 CC permission checks evaluate each sub-command in a chained Bash command (separated by `&&`, `;`, `|`) independently against allow rules.** A chain prompts if any sub-command lacks an allow match, even if the others are covered. Strategy: prefer broad allow patterns (`Bash(git *)`, `Bash(docker *)`, `Bash(jq *)`) paired with targeted denies (`Bash(* --force *)`, `Bash(rm -rf /home/*)`) over per-subcommand narrow allows. Periodically inspect `<project>/.claude/settings.local.json` for accumulated noise from in-session "Yes, and don't ask again" clicks — promote useful patterns to user-level, delete typos and exact-match noise.

**§0.5.104 CC has built-in meta-safety on writes to `.claude/` paths (modifying CC's own config), independent of `permissions.allow` rules.** Keep this — declining the "allow `.claude/` access" offers preserves the guardrail against agents silently widening their own permissions. **Reads** of `.claude/` (inspecting config for diagnostics) are a separate, lower-risk class — accepting the read-allow offer is correct. At a prompt: if CC offers "allow **reading** from `.claude/`" → accept. If it offers "allow `.claude/` access" or anything implying writes → decline.

**§0.5.105 Permission rule additions are done in comprehensive sweeps, not iterative patches.** When CC is consistently prompting, design the full intended rule set across all command categories (Python ecosystem, file mgmt, network, process inspection, archives, etc.) and ship in a single jq sweep paired with the deny boundaries. Iterative patching costs N prompts per session and erodes operator trust. Failure mode logged 2026-05-20: 5 wrong calls in one session about CC permissions led to ~6 needless prompts before the comprehensive sweep landed.

---

## 1. Where we are (as of handoff, 2026-05-20 ~01:30 UTC)

### Live production state — TradeFlow

- **No production containers running.** Phase 0 is scaffold-only.
- **No broker connection.** IBKR paper account (DU…) exists per kickoff but credentials not yet placed in `~/.tradeflow-secrets/.env`. Live trading is Phase 7+.
- **No positions, no open orders, no P&L.**
- **Hetzner VPS:** Hetzner CPX21, Hillsboro OR, Ubuntu 22.04.5 LTS, IP `5.78.212.37`, user `tradeflow`. Idle.
- **Repo:** `github.com/ohad-oren111/tradeflow` (public). HEAD on main: `d956fa2` (docs: add v1 handoff). Branch protection on main: **NOT YET ENABLED** — carry forward from v1 §7 P0.
- **Secrets dir:** `/home/tradeflow/.tradeflow-secrets/` (mode 700) — contains `.env.example` only.
- **VPS CC settings:** `~/.claude/settings.json` at **194 allow / 72 deny** (post-sweep, 2026-05-20T01:27 UTC). Backup at `~/.claude/settings.json.bak-20260520T012729Z` (6061 bytes, original pre-sweep). New file 7733 bytes.
- **VPS project-local `.claude/`:** `/home/tradeflow/tradeflow/.claude/settings.local.json` carries a project-scoped `Read(.claude/**)` allow (accepted via "Yes, allow reading from .claude/ from this project" mid-session). Plus `skills/` directory exists but is empty pending P1 skill port.

### Live production state — Botty AI

- **PAUSED as of 2026-05-19** (v61 close). VPS shut down. Capital frozen at $1462.80 USDT free + ~$543 spot inventory on Binance.
- **Deferred decision rule:** 30 days post-TradeFlow-live, re-evaluate the BTC/ETH cointegrated stat-arb pivot on Binance perpetual futures. Default action absent profitable TradeFlow: stay paused.

### What just shipped this session

- **`d956fa2`** (direct push from laptop via deploy key) — added `docs/handoffs/HANDOFF_v1.md` to the repo. Commit message: `docs: add v1 handoff (Phase 0 closed, ready for Phase 1)`. Verified via `git log origin/main --oneline -3` and `git ls-tree -r origin/main -- docs/handoffs/`.
- **VPS settings.json comprehensive permission sweep** (~01:27 UTC) — 145/57 → 194/72. Net delta +49 allow, +15 deny after dedup. Covers Python ecosystem (`python *`, `python3 *`, `pip *`, `pip3 *`, `pytest *`, `ruff *`, `mypy *`, `black *`, `uv *`), file mgmt (`mv *`, `cp *`, `rm *`, `touch *`, `ln *`, `stat *`, `diff *`, `file *`, `tree *`, `du *`, `rsync *`, `readlink *`, `realpath *`), file viewing (`less *`, `more *`, `nano *`, `vim *`, `vi *`), archives (`tar *`, `zip *`, `unzip *`, `gzip *`, `gunzip *`), pipe utilities (`tee *`, `xargs *`, `tr *`, `true`, `false`), process inspection (`ps *`, `top *`, `htop *`, `uptime *`, `free *`, `systemctl status *`, `systemctl is-active *`, `systemctl list-units *`, `systemctl show *`, `journalctl *`), network diagnostics (`ping *`, `dig *`, `host *`, `nslookup *`, `traceroute *`, `ip *`, `ss *`, `netstat *`, `lsof *`), hashing/encoding (`md5sum *`, `sha256sum *`, `sha1sum *`, `base64 *`, `hexdump *`, `xxd *`), control flow (`time *`, `timeout *`, `watch *`, `sleep *`), system info (`lscpu *`, `lsblk *`), misc (`yq *`, `rev *`, `tac *`, `column *`), and ssh/scp (`ssh *`, `scp *`). Targeted denies added across secrets-dir (read+write), system dirs, SSH keys, shell profile, cron, package mgmt mutations (`apt install/remove/purge`, `pip install/uninstall`), and `git push *` to enforce the Tier-3 verification-only rule.
- **Project-level `~/tradeflow/.claude/settings.local.json`** was wiped clean mid-session, then re-populated with a single project-scoped `Read(.claude/**)` rule after the operator accepted "allow reading from .claude/ from this project" on a diagnostic prompt.

### What we discovered this session (not yet in code)

- **`docs/v0_brief.md` does NOT exist on disk.** CLAUDE.md references it as "reading order step 1" but `find ~/tradeflow -maxdepth 3 -name "v0_brief.md"` returns nothing. Stale reference. Cleanup task for whatever PR next touches docs.
- **`pip3` binary is NOT installed on the VPS** — `command not found`. Python tooling on this VPS uses `python3 -m pip` if pip operations become relevant. Worth a separate followup before Phase 1 PR 2 if any installer-driven workflow surfaces.
- **`hello-world:latest` Docker image is present** as leftover from VPS bootstrap (25.9 kB on disk, 9.49 kB content). Harmless. Delete with `docker rmi hello-world` whenever.
- **CC permission grammar quirk:** `:*` patterns must be terminal. `Bash(scp tradeflow:* *)` is invalid; `Bash(scp * tradeflow:*)` is valid. Caught at CC launch via the Settings Warning UI. Logged as §0.5.97 instance #21 in §10.
- **CC chained-command matching:** chains separated by `&&` / `;` / `|` are checked per-subcommand against allow rules. A chain prompts if any subcommand lacks an allow match. Drove §0.5.103.
- **CC meta-safety on `.claude/` writes is independent of allow rules** and cannot be allowed away short of accepting a broad "allow `.claude/` access" offer. Treat as a guardrail; decline that offer. Drove §0.5.104.
- **The big jq sweep's `mv /tmp/settings.new.json ~/.claude/settings.json` step did NOT trigger the expected `.claude/` write meta-safety prompt.** Two plausible explanations: (a) the earlier accepted project-scoped `Read(.claude/**)` rule may have implicitly expanded coverage in CC's resolver, or (b) the meta-safety has a path-exemption for atomic moves of validated JSON. Operator observation — not fully understood; flagged for §9 pitfalls and future verification.

---

## 2. The session's work thread

1. **Verification block from v1 ran end-to-end clean.** V0–V4 all PASS. HEAD on main confirmed `ce3a158` (pre-handoff publish), CI green, settings.json at 136/49 baseline, Botty VPS unreachable.
2. **P0.5 ad-hoc — publish HANDOFF_v1.md.** Operator ran `scp ~/Downloads/HANDOFF_v1.md tradeflow:~/tradeflow/docs/handoffs/HANDOFF_v1.md` + `ssh tradeflow 'cd ~/tradeflow && git push origin main && git log -3 --oneline'`. Commit `d956fa2` landed on `origin/main`.
3. **Permission diagnosis kicked off.** Operator reported nagging prompts on routine commands. Per `prod-debug-discipline`, ran probes A–D: cat of `settings.local.json`, jq parse of user-level settings, full allow dump, verbatim prompt capture. Confirmed user-level settings clean (`defaultMode: "default"`, `ask: []`); identified per-subcommand chain matching as the actual mechanism.
4. **Wrong call #1: claimed "broad rule `Bash(git *)` confirmed working" from a chained torture-test** that ran prompt-free after one Yes click. Operator pushed back. Correct reading was the chain prompted once for `.claude/` access and Yes was a one-shot grant — couldn't isolate per-rule matching from grant scope. See §5.
5. **Single-command isolation test** — `git -C ~/tradeflow ls-tree -r HEAD -- docs/` — ran inside CC prompt-free. *Actually* confirmed broad-rule matching. The chain test couldn't have proven it.
6. **Wrong call #2: "decline all `.claude/` meta-safety offers."** Iterative refinement. Operator accepted "allow reading from .claude/" mid-session, demonstrating the read/write distinction. Rule revised to §0.5.104 final form.
7. **Wrong call #3: invalid `Bash(scp tradeflow:* *)` rule** in the first round of broad allows. Caught at next CC launch by the Settings Warning UI. Removed via jq filter. Logged as §0.5.97 instance #21.
8. **Wrong call #4: iterative patching after iterative patching.** Each round addressed one more missing category (ssh, scp, etc.) but missed comprehensive coverage. ~5 patches deep, operator challenged "you don't know what you are doing?" — fair call.
9. **Reset to comprehensive sweep.** Drafted big jq command covering ~70 broad allows across Python ecosystem, file mgmt, network, process inspection, archives, hashing, control, system info + ~50 targeted denies (secrets read+write, system dirs, ssh keys, shell profile, cron, apt mutations, pip install/uninstall, `git push *` Tier-3 enforcement).
10. **VPS CC executed the sweep.** Step §1 of the sweep prompt — JSON validation PASS, mv PASS, **no `.claude/` write prompt fired** (anticipated but didn't appear). Step §2 torture-test ran 14 diverse commands prompt-free. Step §3 deny spot-check verified all categories matched. §Report came back **PASS**.
11. **Discovered along the way:** `docs/v0_brief.md` doesn't exist (CLAUDE.md has stale reference); `pip3` not installed; `hello-world` Docker image present from bootstrap.

---

## 3. What the system is actually made of

**Single source of truth:** `github.com/ohad-oren111/tradeflow` on `main` at commit `d956fa2`. No system-map doc exists yet — this handoff is the best available system reference. v2/v3 should add `docs/architecture/SYSTEM_MAP.md` once Phase 1 lands real components.

Highlights:

- **5 database tables defined** in `supabase/schema.sql` (not yet applied to a Supabase project): `trades`, `positions`, `daily_summary`, `kill_switch_events`, `signals`.
- **Production-live code paths:** none yet. `main.py` is a 32-line stub argparse banner.
- **Dead/phantom surfaces:** `docs/v0_brief.md` referenced in CLAUDE.md but absent on disk. Stale reference.
- **Container topology (future):** Phase 1 PR 2 adds IB Gateway Docker container. Phase 1 PR 3+ adds the orchestrator container.
- **Automation gotchas (future):** no cron yet.
- **Open documented bugs:** none yet.

---

## 4. Verified facts about TradeFlow (2026-05-20)

**DO NOT challenge these unless the schema migrates or the broker spec changes.**

### MNQ contract spec (CME, §0.5.97-verified against SeanBot prod, carry-forward from v1)
- `TICK_SIZE = 0.25`, `MULTIPLIER = $2/point`, `COMMISSION_RT = $0.62`, `MARGIN_INTRADAY = $2000`, CME maintenance ~$3636
- Quarterly Mar/Jun/Sep/Dec, expiry 3rd Friday, roll ~8 days before
- Risk per trade: 75pt SL × 4 ticks/pt × $0.50/tick × 2 contracts = **$300 max loss**

### Repo state (verified 2026-05-20)
- HEAD on `main`: `d956fa2` (docs: add v1 handoff)
- Last 4 commits on main: `d956fa2` → `ce3a158` (PR #3 merge, PR 1.5) → `3a967c3` (PR 1.5 commit) → `14fb5e4` (PR #1 merge) → `e6a498a` (PR 1)
- 29 files tracked: 27 from PR 1 + `.github/workflows/ci.yml` (PR 1.5) + `docs/handoffs/HANDOFF_v1.md`
- **Branch protection on `main`: NOT YET ENABLED** — carry-forward P0 from v1 §7

### VPS state (verified 2026-05-20)
- Host: Hetzner CPX21 Hillsboro, IP `5.78.212.37`, Ubuntu 22.04.5
- Python: 3.10.12 (via system; `python3` works). **Note: `pip3` is NOT installed.** Use `python3 -m pip` if pip ops needed.
- Docker: 29.5.1, Compose v5.1.3, **leftover `hello-world:latest` image present** (delete whenever)
- User: `tradeflow` (uid=1000, sudo+docker groups)
- VPS CC: 2.1.145
- Secrets dir: `/home/tradeflow/.tradeflow-secrets/` (mode 700)

### VPS CC permission state (verified 2026-05-20 ~01:30 UTC)
- User-level `~/.claude/settings.json`: **194 allow / 72 deny**, `defaultMode: "default"`, `ask: []`
- File size: 7733 bytes
- Backup: `~/.claude/settings.json.bak-20260520T012729Z` (6061 bytes, original pre-sweep)
- Project-local `~/tradeflow/.claude/settings.local.json`: contains a `Read(.claude/**)` allow (project-scoped). Other entries from earlier this session were wiped and not regenerated.

### CC behavioral facts (verified 2026-05-20)
- **Chained Bash commands match per-subcommand** against allow rules. Any unmatched subcommand prompts the whole chain.
- **`:*` pattern grammar must be terminal.** `Bash(scp tradeflow:* *)` is rejected at CC launch; `Bash(scp * tradeflow:*)` is valid.
- **`.claude/` write meta-safety is independent of allow rules**, but its trigger conditions aren't fully understood (the §1-sweep `mv` didn't fire it — see §1 last bullet).
- **`.claude/` read meta-safety can be allowed away** via the "Yes, allow reading from .claude/" offer (scoped to project or user level depending on context).

### Library choices (locked, carry-forward from v1)
- IBKR client library: **`ib_async`**
- Strategy: SMA100-bounce, long-only, 2 contracts, 75pt SL, 150pt trail, max 5 positions
- Database: Supabase via custom REST client (not the supabase-py SDK)

---

## 5. Wrong diagnoses (if any) — READ BEFORE YOU DEBUG

This session made **5 wrong calls** in the same family — all in permission-rule diagnosis. Reading this section is the single most important thing the next session does before touching permissions or anything else.

### Wrong call #1: "Broad rule `Bash(git *)` confirmed working" from a chained test

**Diagnosis:** After running a chain of 8 commands (`git status && git log && git ls-tree && jq ~/.claude/... && docker --version && docker ps && find ... && head ...`) that completed after one Yes click on a `.claude/` access prompt, declared "broad rules work, per-subcommand whack-a-mole is dead, P-1 closed."

**Evidence that misled:** Chain ran end-to-end after a single Yes click. Inferred that the other 7 subcommands matched broad rules silently.

**Why it was wrong:** The single observation was consistent with two very different worlds — (a) broad rules matched 7/8 subcommands silently, .claude/ was the one prompt; or (b) Yes was a one-shot grant for the entire chain regardless of per-subcommand matching. The test design couldn't distinguish them.

**Correct diagnosis (confirmed later):** A single-command isolation test — `git -C ~/tradeflow ls-tree -r HEAD -- docs/` run inside CC — produced no prompt at all. That's the test that *actually* proves broad rule matching.

**Recovery:** Operator pushed back ("are you sure this is a success?"); I owned the retrofit, designed an isolating probe, ran it, got the clean answer.

### Wrong call #2: "Decline all `.claude/` meta-safety offers" (initial §0.5.104)

**Diagnosis:** Framed CC's `.claude/` meta-safety as a uniform signal worth keeping. Rule said: "decline any 'allow `.claude/` access' offer."

**Evidence that misled:** Wrote the rule from a high-level safety intuition without distinguishing categories of `.claude/` operation.

**Why it was wrong:** Conflated reads and writes. Reads of `settings.json` are low-risk diagnostics that we hit 4+ times per session. Writes are high-risk mutations. They're different threat classes; one rule applied to both forces friction on the wrong axis.

**Correct diagnosis:** Accept "allow **reading** from `.claude/`" offers; decline "allow `.claude/` access" (write-implying) offers. Rule revised to §0.5.104 final form.

### Wrong call #3: "Wipe `settings.local.json` clean"

**Diagnosis:** Early in the session, instructed the operator to replace `settings.local.json` with `{"permissions": {"allow": []}}` based on the assessment that all 7 entries were noise.

**Evidence that misled:** 2 entries had double-slash typos (`Read(//tmp/**)`, `Read(//home/...)`); 1 was hyper-specific curl exact-match; 1 was hyper-specific ssh exact-match; 3 were narrow ssh/getent rules.

**Why it was wrong:** The `Bash(ssh tradeflow *)` entry was legitimately useful — it covered the V0 `ssh tradeflow` command that prompted later in the same session. Wiping it removed working coverage that I then had to re-derive.

**Correct diagnosis:** Triage entries individually. Delete typos and exact-match noise; promote useful narrow patterns to user-level allow rules.

### Wrong call #4: `Bash(scp tradeflow:* *)` is valid grammar

**Diagnosis:** Added the rule to the broad-allow patch in the first sweep, assuming glob/shell intuition matched CC's permission grammar.

**Evidence that misled:** Intuition. No probe of CC's docs or a throwaway test rule.

**Why it was wrong:** CC's validator rejects `:*` patterns that aren't terminal. The Settings Warning UI fires at every CC launch until the rule is removed.

**Correct diagnosis:** Probe CC's grammar before adding rules. Logged as §0.5.97 instance #21.

### Wrong call #5: "24 broad rules covers the workflow" (first sweep)

**Diagnosis:** First broad-rule patch added git, docker, jq, find, wc, head, tail, sort, uniq, grep, sed, echo, printf, which, test, pwd, id, whoami, hostname, env, mkdir, scp upload, scp download. Estimated this covered the workflow.

**Evidence that misled:** Mental enumeration of "what does the operator run frequently?" — without checking against the actual command categories of a normal Python/Docker engineering workflow.

**Why it was wrong:** Missed obvious categories — ssh (used in V0/V4), python3 (will be used in Phase 1+), pip (ditto), mv/cp/rm (universal file ops), ps/journalctl/systemctl status (process inspection), ip/ss/lsof (network). Each missing category produced a future prompt. ~5 iterations of patching followed.

**Correct diagnosis:** Design the broad-allow set against workflow categories, not from memory. Logged as §0.5.105.

**Lesson for next session:** Every wrong call this session was the same failure mode — making claims without designing probes that distinguish between specific hypotheses. Per `prod-debug-discipline` Step 5 ("if your diagnosis doesn't predict the next observation, throw it out") and Step 6 (rule of twice-wrong: stop after the second wrong call and reset), I should have stopped iterating after wrong call #2 and gone comprehensive. Instead I patched 5 times. Next session: separate KNOWN from HYPOTHESIZED at every step; design single-confound probes; when iterative work exceeds 2 rounds, stop and redesign.

---

## 6. Verification block — run this before doing anything

**These blocks are VPS-native** (no `ssh tradeflow` indirection — operator's actual usage has been VPS-side via VPS CC). If running from the laptop side, prepend each block with `ssh tradeflow '...'` and quote appropriately.

**V0 — Identity + Claude Code version**
```bash
whoami && hostname && date -u && claude --version
```
Expect: `tradeflow`, `ubuntu-4gb-hil-1`, current UTC date, `2.1.145 (Claude Code)` or newer. If `claude --version` shows older: CC auto-updated, fine. If hostname differs: STOP, wrong host.

**V1 — Repo state on VPS**
```bash
git -C ~/tradeflow fetch origin main && git -C ~/tradeflow rev-parse origin/main && git -C ~/tradeflow log -5 --oneline origin/main
```
Expect: HEAD sha on `origin/main` is `d956fa2` or a descendant. Last commits include `d956fa2` (v1 handoff), `ce3a158` (PR #3 merge, PR 1.5), `3a967c3` (PR 1.5 commit), `14fb5e4` (PR #1 merge), `e6a498a` (PR 1). If HEAD has advanced past `d956fa2`: Session 3 has already shipped something — read the new commits before acting.

**V2 — CI status on main**
```bash
curl -s -H "Accept: application/vnd.github+json" "https://api.github.com/repos/ohad-oren111/tradeflow/actions/runs?per_page=5" | jq '.workflow_runs[0] | {name, head_sha: .head_sha[0:7], status, conclusion, html_url}'
```
Expect: most recent CI run has `conclusion: "success"`. Anything else: STOP, read the run log.

**V3 — VPS CC permissions loaded (updated baseline)**
```bash
jq '{allow: (.permissions.allow | length), deny: (.permissions.deny | length), defaultMode: .permissions.defaultMode, ask: (.permissions.ask // [] | length)}' ~/.claude/settings.json
```
Expect: `{allow: 194, deny: 72, defaultMode: "default", ask: 0}`. Expect a one-time prompt for `.claude/` read — accept it (per §0.5.104, reads are OK to allow at project level). If counts deviate >10%: settings.json drifted; restore from `~/.claude/settings.json.bak-20260520T012729Z`.

**V4 — Broad-rule sanity check (replaces v1's Botty-reachability check)**
```bash
git -C ~/tradeflow ls-tree -r HEAD -- docs/ && python3 --version && docker --version && ss -tln 2>&1 | head -3
```
Expect: chain runs **prompt-free**. Output is the docs tree (`docs/architecture/.gitkeep`, `docs/handoffs/.gitkeep`, `docs/handoffs/HANDOFF_v1.md`, `docs/handoffs/HANDOFF_v2.md` once published), Python version, Docker version, top 3 listening sockets. If any subcommand prompts: a broad rule isn't matching — investigate before continuing.

---

## 7. Pending work queue

Priority order is by V1 state, not by ordering below. Read V1 first.

### P0 — Enable branch protection on main *(operator, manual GitHub UI step — carry forward from v1 §7)*
Settings → Branches → Add rule → branch name pattern `main` → require "Lint, type-check, and test" status check before merging, require PR (approvals = 0), require conversation resolution, disallow force-pushes and deletions, "do not allow bypassing the above settings" checked. CI is green on main, safe to enable. **Until P0 lands, anyone (including CC Web) can land code that breaks CI on main.** Estimated: 2 minutes in browser. **Status: pending. Operator has not yet confirmed UI completion.**

### P1 — Skill port from Botty repo *(carry forward from v1 §7)*
Copy 4 skills directories from Botty repo into TradeFlow's `.claude/skills/`:
- `code-pr-brief/`
- `prod-debug-discipline/`
- `session-handoff-writer/`
- `vps-smoke-test-runbook/`

Commit as a single small PR via CC Web (required once P0 enables branch protection). Title: `chore: port .claude/skills/ from Botty repo (4 skills)`. Estimated: 15 minutes.

### P2 — IBKR paper account verification *(carry forward from v1 §7)*
Confirm paper trading account `DU…` is active and credentials are obtainable. **Do not start Phase 1 PR 2 without this verified.** Estimated: 5 minutes via IBKR portal.

### P3 — Phase 1 PR 2: IB Gateway Docker container *(carry forward from v1 §7)*
First functional PR. Brief drafted via `code-pr-brief` skill. Lifts SeanBot patterns 1–3. Estimated: 3–4 days end-to-end.

### Uncommitted files / operational debt

- **`vps_settings.json` reference copy not in repo.** Current `~/.claude/settings.json` (194/72) is only on the VPS. Worth committing a sanitized copy to the repo as `vps/settings.json.reference` so it's versioned. Add to a future small PR (could ride P1 if convenient).
- **`docs/v0_brief.md` stale reference in CLAUDE.md.** Either create the file or remove the reference. Cleanup for whatever PR next touches docs.
- **Skill `.claude/skills/` directory is empty.** Pending P1.

### Operational cleanup eventually

- Delete VPS `hello-world:latest` image (`docker rmi hello-world`) — bootstrap leftover, harmless but tidier without.
- Periodic audit of `~/tradeflow/.claude/settings.local.json` — promote useful project-scoped patterns to user-level, delete typos/exact-match noise (per §0.5.103).
- Investigate why the §1-sweep `mv ~/.claude/settings.json` didn't trigger the meta-safety prompt — unclear whether the project-level `Read(.claude/**)` allow expanded coverage or whether there's a path-exemption for atomic moves of validated JSON.
- Add `docs/architecture/SYSTEM_MAP.md` when Phase 1 ships real components.

---

## 8. Test safety — why we belabor this

Carry-forward verbatim from v1 §8. The five recurring test-mocking traps:
1. Tests passed against a fictional schema because they mocked column names that didn't exist in prod
2. `side_effect` list had wrong count → silent `StopIteration` → wrong assertions
3. Mocked at raw library chain when code uses a wrapper → tests green, prod broken
4. Shared `MagicMock()` state leaked between tests
5. Async decorator pattern assumption — verify a neighbor before assuming

Guardrails in the `code-pr-brief` skill template prevent all five. No new tests shipped this session — TradeFlow's first real tests still land in Phase 1 PR 2.

---

## 9. Pitfalls from prior sessions

Things the LLM got wrong before and should not be trusted on without verification:

- **CC Web UI "merged" signal lies.** Verify with `git rev-parse origin/main` after a claimed merge.
- **Heredoc paste mangles long content.** Use `scp` or CC's `Write` tool for any file > ~50 lines over SSH.
- **CC Web sandbox branch state persists across PRs in the same task.**
- **`cd <dir> && ...` triggers a CC built-in safety prompt** independent of permissions.
- **Skills must be read (`view`'d) before drafting.**
- **Hetzner CX SKU is in "Limited availability".**
- **Docker Compose is v5.x on current Ubuntu.**

New this session:
- **CC permission rules: design comprehensively, not iteratively.** N tactical patches cost N prompts. Per §0.5.105.
- **CC's `:*` pattern grammar must be terminal.**
- **Chained Bash commands check per-subcommand.** Per §0.5.103.
- **CC `.claude/` write meta-safety trigger conditions are not fully understood** — the §1-sweep `mv` didn't fire it. Treat as a probabilistic guardrail; don't assume it always fires; design rule patches as if it might or might not.
- **CC's `settings.local.json` accumulates project-scoped opt-ins.** Inspect periodically.
- **`docs/v0_brief.md` referenced in CLAUDE.md doesn't exist.** Don't trust CLAUDE.md as a source of truth for file existence.
- **`pip3` is NOT installed on the VPS.** Use `python3 -m pip` if pip operations become relevant.

**Next session rule (reinforced):** if a claim is quantitative or behavioral, re-verify it. Especially: settings.json rule counts, file existence, broad-rule matching, CI status, branch protection state.

---

## 10. Session discipline lesson (2026-05-20)

This session made **5 wrong calls** in one sitting — all about CC permissions. Pattern: each wrong call was a claim made without enough evidence to distinguish between specific hypotheses, then retrofitted when contradicted. Specifically:

- Wrong call #1 (chained-test "success") retrofitted from ambiguous evidence
- Wrong call #2 (decline all `.claude/` offers) overclaimed uniformity
- Wrong call #3 (wipe `settings.local.json`) overclaimed "noise"
- Wrong call #4 (bad scp pattern) baked grammar without probing
- Wrong call #5 (incomplete 24-rule patch) designed from memory not workflow

**§0.5.97 instances logged this session:**

- **§0.5.97 instance #21** — Baked an `scp` permission pattern (`Bash(scp tradeflow:* *)`) without probing CC's permission-rule grammar. The rule loaded, settings file stayed valid, but CC's validator rejected it at next launch with a Settings Warning UI. Lesson: even single-line settings rule additions get a probe.

- **§0.5.97 instance #22** — Retrofitted a "success" narrative onto a chained-test result that couldn't actually distinguish "broad rules work" from "Yes was a one-shot grant." Lesson: design probes that distinguish between hypotheses; multi-confound observations aren't tests.

**Enforcement rules for next session:**

1. **Single-confound probes only.** When testing a hypothesis about CC behavior, the probe must be able to distinguish the hypothesis from alternatives. Multi-command chains don't.
2. **Rule of twice-wrong (`prod-debug-discipline` §6) applies to permission-rule work, not just bug-debugging.** After two wrong patches, stop and design comprehensively.
3. **Probe CC's grammar/behavior before adding settings rules.** Throwaway directory + `claude` launch + add one rule + observe behavior > "should work."
4. **Maintain KNOWN-vs-HYPOTHESIZED separation in chat.** Don't claim "broad rules work" when the evidence is "chain ran after Yes."

---

## 11. Logging verbosity — what to demand from any new code

Carry-forward verbatim from v1 §11. Standing principles unchanged:

- Every IBKR order placement logs `[ORDER] MNQM6: PLACED LMT BUY 2 @ 25000.00 — sma100=24985.0, rsi=42`
- Every state transition logs old → new at INFO
- Every swallowed exception logs the specific error + symbol + position context
- Retry loops log attempt number and reason
- Async code logs entry AND exit
- Any dedup/select-one-of-many must log which row won and why
- `logging.basicConfig(level=logging.INFO)` set in `main.py` from PR 2 onwards

No code shipped this session — these standards remain forward-looking targets for Phase 1 PR 2+.

---

## 12. Master template — use for every Claude Code PR

See the `code-pr-brief` skill at `.claude/skills/code-pr-brief/` (once ported per §7 P1). It enforces: patch constraints, code quality, test safety guardrails, known gotchas (carry from §9), and the "what I got wrong" post-PR section. Until the skill ports into the TradeFlow repo, reference the skill directly via `view /mnt/skills/user/code-pr-brief/SKILL.md` in the chat session, or copy from Botty's `.claude/skills/code-pr-brief/pr_brief_template.md`.

---

## 13. Current PR brief in flight (if any) — hand this to Claude Code as-is

**None at handoff time.** Session 3 starts with P0 (branch protection — operator UI), then P1 (skill port — small PR via CC Web), then P2 (IBKR verify — operator browser), then P3 (Phase 1 PR 2 brief — drafted via `code-pr-brief` skill, handed to CC Web).

When drafting PR 2: `view` the `code-pr-brief` skill first (don't skip — last session demonstrated the cost), reference SeanBot patterns 1–3 (IB Gateway connection, market data subscription, healthcheck loop), specify the docker-compose service definition, mount `~/.tradeflow-secrets/.env`, and pin the `ib_async` version in `pyproject.toml`.

---

## 14. Canonical references (in order of authority)

1. **GitHub repo on `main` at `d956fa2` or later** — what actually runs / is committed
2. **VPS filesystem at `/home/tradeflow/tradeflow/`** — what's deployed on the box
3. **VPS CC settings at `/home/tradeflow/.claude/settings.json`** — current rule state (194/72 as of 2026-05-20)
4. **IBKR API via `ib_async`** (Phase 1+) — truth for positions, fills
5. **Supabase REST** (Phase 1+) — truth for trade/position rows
6. **GitHub Actions runs API** — truth for CI status
7. **`TRADEFLOW_SESSION_1_KICKOFF.md`** — original orchestration; §0.5.T1–T5 verbatim
8. **`docs/handoffs/HANDOFF_v1.md`** in repo — Session 1 context
9. **THIS handoff (v2)** — Session 2 context, NOT long-term authority
10. **Botty AI handoff v61 and earlier** — historical lineage; §0.5.92–.98 originated there

---

## 15. First 15 minutes of the next session

1. **Read sections 0.5, 1, 4, 5, 15** of this handoff. **§5 is the single most important** — it documents 5 wrong calls about CC permissions to prevent recurrence.
2. **Run §6 verification block (VPS-native).** Confirm: identity, HEAD on main `d956fa2` or descendant, CI green, settings.json 194/72, broad-rule sanity passes prompt-free.
3. **Execute P0** — enable branch protection on `main` via GitHub UI (~2 min). Refresh `https://github.com/ohad-oren111/tradeflow/settings/branches` to verify the rule appears.
4. **Execute P1** — port 4 skills from Botty repo into TradeFlow's `.claude/skills/`. Use CC Web small-PR flow now that branch protection is on, OR direct laptop push via deploy key if you set the protection rule with an admin bypass option. Title: `chore: port .claude/skills/ from Botty repo (4 skills)`.
5. **Execute P2** — verify IBKR paper account `DU…` in browser. Retrieve TWS account number + paper password. Stage them for `~/.tradeflow-secrets/.env` (Phase 1 PR 2 will mount this).
6. **Draft P3** — Phase 1 PR 2 brief (IB Gateway Docker) using `code-pr-brief` skill. Load the skill via `view` first. Hand brief to CC Web.
7. **After PR 2 merges:** draft VPS smoke test runbook via `vps-smoke-test-runbook` skill (load via `view` first), paste into VPS CC for end-to-end execution.

---

## 16. How to publish this handoff

**Path B — Manual scp + git from laptop (preferred for v2, since branch protection is still OFF):**

```bash
# 1. From laptop, push the handoff file to the VPS
scp ~/Downloads/HANDOFF_v2.md tradeflow:~/tradeflow/docs/handoffs/HANDOFF_v2.md

# 2. SSH to VPS, commit, and push
ssh tradeflow 'cd ~/tradeflow && git add docs/handoffs/HANDOFF_v2.md && git commit -m "docs: add v2 handoff (P-1 closed, P0-P3 queued)" && git push origin main && echo "---" && git log -3 --oneline'

# 3. Verify push from laptop
ssh tradeflow 'cd ~/tradeflow && git fetch origin main && git log origin/main --oneline -3 && git ls-tree -r origin/main -- docs/handoffs/'
```

Expected on step 2: file added, commit lands (new sha after `d956fa2`), push succeeds, log shows the new commit at HEAD.

Expected on step 3: `origin/main` advanced to the new commit; `docs/handoffs/HANDOFF_v2.md` appears in `git ls-tree` output alongside the existing `HANDOFF_v1.md`.

**Path A — VPS Claude Code brief (alternative, if VPS CC is convenient and laptop scp isn't):**

```
You are VPS Claude Code on the TradeFlow VPS. Save the following content verbatim
to /home/tradeflow/tradeflow/docs/handoffs/HANDOFF_v2.md, then:

  git -C /home/tradeflow/tradeflow add docs/handoffs/HANDOFF_v2.md
  git -C /home/tradeflow/tradeflow commit -m "docs: add v2 handoff (P-1 closed, P0-P3 queued)"
  git -C /home/tradeflow/tradeflow push origin main

Confirm the file exists, git log shows the commit at HEAD, `git status` is clean,
and report the commit hash + line count via `wc -l`.

<paste handoff content here>
```

The handoff exists only if saved to disk AND committed AND pushed to `origin/main`. Until then, treat this chat output as draft.

---

*End of handoff v2. Target lifespan: until Phase 1 PR 2 (IB Gateway) lands and the first orchestrator brief is in flight. Then v3 supersedes.*
