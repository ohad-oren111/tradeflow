# `tools/eval` — TradeFlow evaluation kit (Session 24)

Accelerated, **offline** proofs of the live strategy + exit, plus **guarded** live/fault
tooling. The point (CLAUDE.md §0.5.220): never default to "we wait" on a regime-paced
measurement — prove everything that *can* be proven sooner so the only thing left waiting
is the genuinely unshortcuttable (the forward edge).

Everything here imports the **real** production code and drives it; nothing re-implements
strategy or exit math:

| What it drives (real prod code) | Module |
|---|---|
| `strategy.evaluate_gates` / `_regime_ok` / `_in_session_edge_window` + 10-bar cooldown | `engine.py` |
| `trail_manager.compute_ratcheted_stop` / `should_hard_exit` (base −75, +50 lock, 150 trail, 1000 ceiling) | `engine.py` |
| `kill_switch.evaluate_triggers` (warn@6 / halt@10) | `scenarios.py` |
| P&L: `(exit−entry)·2·$2` − friction (`config.instruments.MNQ`) | `engine.py` |

## Phases

- **Phase 1 — backtest** (`python -m tools.eval.backtest [--regime on|off] [--roll-adjust] [--limit N]`)
  Drives the real strategy + trailing-ratchet exit over the saved 26-month 1-min NQ
  series (`research/data/nq_1min_raw.csv`; NQ is point-identical to MNQ). Single-position
  (MAX_CONCURRENT=1), Friday weekend force-close, friction §0.5.97. Reports overall +
  per-month segmented stats and the gate funnel. Caveats printed in-band.

- **Phase 2 — synthetic scenarios** (`python -m tools.eval.scenarios`)
  Eight hand-built scenarios (below-trend block, profit-walk, base-stop loss, V-reversal
  give-back, chop, feed-gap, kill-switch streak, hard-ceiling), each run K≥5 with
  randomized timing/vol/slope; EXPECTED vs ACTUAL + determinism. Execution stubbed —
  no IB connection, no prod-DB write.

- **Phase 3 — live round-trip** (`live_roundtrip.py`, **AUDIT**) — real order plumbing on
  the paper gateway. Refuses to run without `--execute --i-confirm-prod-halted-and-flat`;
  separate clientId; guaranteed flatten + cancel-all in `finally`. Never touches Supabase.

- **Phase 4 — fault injection** (`fault_injection.py`, **AUDIT**) — socket-drop /
  gateway-restart / wedged-subscription on a **throwaway** instance + its own gateway.
  Refuses to run against the prod gateway port.

- **Phase 5 — consolidated report** (`python -m tools.eval.report [--quick]`) — assembles
  the backtest preview + scenario matrix + live/fault status + the one residual.

- **Phase 6 — exit sweep + walk-forward** (`python -m tools.eval.exit_sweep --validate`) —
  sweeps the exit knobs (stop_loss × lock_in × trail_offset) over the REAL exit, with
  rolling train→test **walk-forward** so no number is in-sample cherry-picked. Builds the
  entry tape ONCE (entries are invariant to the exit knobs) then replays each config
  trade-by-trade; `--validate` asserts the replay is byte-identical to `simulate_segment`
  on the baseline (also `tests/test_exit_sweep.py`, synthetic, CI-safe). Reports baseline
  anchor, in-sample sweep, neighbor-plateau robustness, and the OOS verdict vs the §7 bar.
  `sb_exit_probe.py` (READ-ONLY Supabase) runs captured SeanBot entries through TF's
  current vs the best swept exit — directional, small-n.

- **Phase 7 — below-trend long edge study** (`python -m tools.eval.below_trend_study
  [--validate] [--chop-threshold 20]`) — the question the regime gate raises: do the
  below-30m-EMA200 longs the gate now BLOCKS (the trades SB profits from in chop) have a
  real OUT-OF-SAMPLE edge — unconditionally, or only filtered to chop, and under a fast
  SB-style exit? It drives the REAL gates with regime OFF (so below-trend entries fire),
  tags each entry at fill time as ABOVE/BELOW the **same** 30m EMA200 `_regime_ok` uses
  (same window/resample/EMA200), splits the below set CHOP vs TREND by a **causal** 1-min
  ADX(14) at the entry bar, and runs each partition through the REAL exit as an independent
  single-position book — for TF-current (stop75/lock50/trail150), an SB-fast exit
  (lock15/trail40), and a LABELLED +50-TP what-if. Rolling 6mo→2mo walk-forward selects the
  (chop-threshold, exit) on train and scores it OOS; the unconditional below set is reported
  full-sample (no selection → no in-sample bias). `--validate` asserts the masked replay is
  byte-identical to `simulate_segment` (also `tests/test_below_trend_study.py`, CI-safe).

- **Phase 8 — short-side edge study** (`python -m tools.eval.short_side_study
  [--validate] [--adx-threshold 20]`) — the symmetric question: a long-only MA100-bounce
  bot can't profit in a sustained downtrend (it sits out via the regime gate). Does the
  MIRRORED SHORT — sell SMA100 *rejections* in the DOWN-regime (close < 30m EMA200) — have
  a real OUT-OF-SAMPLE edge? The entry is a **NEW mirrored entry** (`evaluate_gates_dir`):
  the documented long gates with the inequalities flipped (ma_order MA50>MA100; `high` in
  [ma_slow−buf, ma_slow+15]; bearish/doji confirm). **No prod short path exists — this does
  NOT drive prod.** The exit is the REAL direction-aware exit (`compute_ratcheted_stop` /
  `should_hard_exit` with `direction=SHORT`: stop ABOVE entry, ratchets DOWN). Entry+exit are
  DIRECTION-PARAMETERIZED in one path; the **fidelity anchor** (`--validate`,
  `tests/test_short_side_study.py`) asserts the same path with `direction=LONG` reproduces
  `simulate_segment` byte-for-byte, and that a price-REFLECTED long fixture fires an
  identical-P&L short. Primary partition = down-regime shorts ("trade the regime the long
  bot sits out"); above-regime shorts are reported for reference. Rolling 6mo→2mo
  walk-forward selects (adx-threshold, exit) on train and scores OOS; the unconditional
  down-regime set is the honest headline (no selection).

- **Phase 9 — portfolio sizing + drawdown-control study** (`python -m
  tools.eval.portfolio_study [--validate] [--start-capital 25000] [--rebuild-tape]`) — the
  existing engine is SINGLE-position; this adds the missing piece: a **portfolio-level**
  simulator that runs up to N concurrent positions (each M contracts, each its OWN
  independent stop/trail lifecycle) on the SAME validated edge, tracks a portfolio equity
  curve / aggregate exposure / portfolio MaxDD, and applies SB's risk controls (real
  `kill_switch.evaluate_triggers`, daily-loss cap, cooldown) at the portfolio level. It
  reuses the REAL entry (`FastGateEntry`, regime ON — a signal **tape** is built once, then
  each portfolio config replays cheaply with the cooldown re-applied) and the REAL exit
  (`_process_exit_bar` per position). The **fidelity anchor** (`--validate`,
  `tests/test_portfolio_study.py`) asserts N=1, M=2 reproduces `simulate_segment` (regime ON)
  byte-for-byte — only then are the multi-position numbers trustworthy. **Part A** reproduces
  SB's exact live config (N=3 × M=2, trailing, regime ON; his trail 250 vs ours 150) and
  reports net$, equity-curve shape, PF, Sharpe, MaxDD ($/%), the rolling-10-trading-day P&L
  distribution, and the **correlated-concurrency** amplification (3-concurrent MaxDD vs single
  × √3 vs × 3). **Part B** tests MODEST drawdown-control overlays (vol-scaled sizing,
  equity-curve de-risking, daily-loss cap, dynamic max-concurrent) under a rolling
  train→test walk-forward that selects by **Calmar** (net/MaxDD) on train and scores OOS, with
  a full-sample neighbor surface + selection-stability read. **This is OFFLINE research and
  drives NO prod path** — turning live sizing/concurrency on is a SEPARATE AUDIT for the
  operator AFTER these numbers + a forward paper window. Modeled fills; the MaxDD here is the
  real (correlated, amplified) one.

- **Phase 10 — SeanBot shadow ledger** (`python -m tools.eval.shadow_ledger [--qty 1]
  [--from-json] [--out PATH]`) — a FORWARD scorecard of the gate's opportunity cost, built
  from telemetry that ALREADY exists (no backtest, no live-bot path). For every SB LONG entry
  TF classified `MISS-regime` or `MISS-NO-BAR` (the regime gate or a blind-feed gap BLOCKED
  it), it pairs the entry with SB's OWN realized exit (FIFO over `seanbot_signals`;
  `type='exit'` carries `pnl_points`) and tallies realized net$, win rate, and PF of the
  trades the gate denied — joined to TF's `signal_reconciliations` classification by
  `message_id`. A gate that blocks net losers is doing its job; a gate that blocks a
  positive-PF cluster is too strict. **Why it exists (§0.5.220):** the live PF>1.2 go/no-go
  bar takes 6-12 months; SB posts every win AND loss in real time and TF already captures
  them, so the gate's cost accrues a (loud-caveated, noise-flagged) read in WEEKS off stored
  data. WR/PF are sizing-invariant; $ are at TF's sizing (`--qty`, default 1; SB ran 2). The
  fidelity invariants (FIFO pairing, scored-class scope, unpaired-not-dropped, the engine
  `_pnl` $ model) are pinned in `tests/test_shadow_ledger.py` on hand-built telemetry (no DB
  mocks). Writes a dated report to `/tmp/shadow_<date>.txt`. **OFFLINE research, READ-ONLY,
  drives NO prod path.**

- **Phase 11 — SeanBot shadow-ledger REVIEW** (`python -m tools.eval.shadow_review [--qty 2]
  [--from-json] [--out PATH]`) — promotes the Phase-10 tally into a **decision instrument**.
  It reuses `shadow_ledger.scorecard` verbatim (identical $ math) and adds the three things an
  operator acts on: (1) a **COHORT SPLIT** — `MISS-regime` (a gate/strategy question) vs
  `MISS-NO-BAR` (a feed question) scored separately, with any `MISS-NO-BAR` dated at/after the
  feed fix (#127, 2026-06-09 15:27 UTC) flagged LOUD as a **feed regression** (it should trend
  to ~0); (2) a **per-day + cumulative** WR/PF/net$ curve at TF's live sizing (default `--qty
  2`); (3) a **pre-committed DECISION BAR** — `INSUFFICIENT` (n_paired < 20), `GATE-TOO-STRICT`
  (n>=20 and blocked set net-positive → escalate to Phase 12), or `GATE-EARNING-ITS-KEEP`
  (n>=20 and net-negative), annotated with a NOISE/THIN/EMERGING/FIRM confidence tier. This is
  the LIVE, accumulating half of the gate question; the 26-month HISTORICAL cohort edge is
  Phase 12. Invariants (decision rule, confidence tiers, per-day cumulative, feed-regression
  flag, scorecard-$ parity) pinned in `tests/test_shadow_review.py` on hand-built telemetry (no
  DB mocks). Writes `/tmp/shadow_review_<date>.txt`. **OFFLINE research, READ-ONLY, drives NO
  prod path.**

- **Phase 12 — regime-gate CALIBRATION study** (`python -m tools.eval.gate_calibration [--limit
  N] [--from-json] [--out PATH]`) — resolves the §0.5.222 tension between #123 (below-30m-EMA200
  longs are negative-EV over 26 months → gate stands) and recent SB tape (he WINS on some
  below-trend longs the gate blocks). Reuses the cached `below_trend_study.BTTape` (every
  regime-OFF signal over the full NQ history, tagged below/above the 30m EMA200 by the real
  `regime_at`), ENRICHES each below-trend signal with its DEPTH below the EMA200 and ET hour,
  and replays depth / time-of-day / EMA-slope sub-cohorts through the REAL exit
  (`replay_masked`). A rolling train→test **walk-forward** pools OOS trades under each FIXED
  band; a **multiple-comparison-hardened** verdict (11 bands tested) only names a band a
  CANDIDATE if it clears PF≥1.20 on BOTH the OOS *and* full sample, net>0, n≥30, beating the
  unconditional below-cohort. **Verdict (2026-06-09): GATE-CORRECT** — below-trend pools at OOS
  PF 1.026 (far under the bar, ~2× the above-book drawdown) and no band robustly separates; the
  one band that popped on OOS (mid-depth, PF 1.25) was a multiple-comparison artifact the full
  sample (PF 1.16) did not corroborate. Above-trend anchor reproduces exactly (n=1479, +$28,729,
  PF 1.174). SB's actual STRATEGY code is NOT on the VPS (only his Telegram parser) → flagged as
  a **data gap** for the operator, never fabricated. Applies NOTHING; a real gate change is a
  SEPARATE AUDIT PR. Pure logic (masks, verdict, data-gap check) pinned in
  `tests/test_gate_calibration.py`. Writes `/tmp/gate_calibration_<date>.txt`. **OFFLINE
  research, READ-ONLY, drives NO prod path.**

- **Phase 13 — concurrency / cluster historical replay** (`python -m
  tools.eval.concurrency_replay [--n 2] [--m 2] [--out PATH]`) — validates the N=2 + kill-switch
  cluster-mode ship (#129–#131, `cluster_window_bars=1`) against REAL above-trend NQ history
  BEFORE the first live 2-stack, driving the ACTUAL prod logic: stacking/correlated-stops from
  `portfolio_study.simulate_portfolio` (N=2×M=2, regime-ON, kill-switch OFF to see the full
  population), and the cluster collapse + entry-bar minute derivation imported from
  `src.execution.kill_switch` (`_collapse_loss_clusters`, `evaluate_triggers`,
  `_entry_bar_minutes`) — **not re-implemented**. Findings (2026-06-09): **(1) STACK — PASS**
  (max-concurrent reaches 2; 305k bars 2-open); **(2) COLLAPSE-FIRES — PASS** (the real collapse
  merges 222 loss-events at window=1); **(3) window=1 — CHALLENGED**: only 52.6% of
  correlated-stop entries are within 1 min (they stop together on one down-move but entered on
  different pullback bars), and the peak consecutive-loss streak goes per-trade=11 → cluster-w1=10,
  **still ≥ halt@10** — so window=1 fires but does NOT fully defuse the N=2 trap; the real
  `evaluate_triggers` returns PAUSE both ways at the worst poll. A wider `cluster_window_bars`
  (~2 min here) drops the peak below halt, surfaced as a CANDIDATE to evaluate later with the
  false-merge tradeoff stated — **NOT applied** (a real change is a separate AUDIT). Pure
  analysis logic pinned in `tests/test_concurrency_replay.py`. Writes
  `/tmp/concurrency_replay_<date>.txt`. **OFFLINE research, READ-ONLY, drives NO prod path.**

- **Phase 14 — SeanBot fill reconciliation** (`python -m tools.eval.phase14_sb_reconciliation
  [--from-json] [--out PATH]`) — one pre-committed question: **is SB's cumulative lead achievable
  on the REAL MNQ tape, or does it rest on a divergent feed / a gate that fails open?** Pulls SB's
  entries/exits (typed PostgREST `seanbot_signals`), FIFO-pairs round-trips, and tests their prints
  against ground truth — the real MNQ 1-min tape pulled from IBKR (separate clientId 114,
  historical-data only). Three instruments: **(B) price delta** `SB_posted − real_close` with a
  timestamp-shift scan; **(C) regime classification** on the REAL 30m-EMA200 (the same window
  `regime_at` uses) bucketing each entry legit-above / DIVERGENT-FEED / GATE-FAILED-OPEN; **(D)
  re-price** each clean round-trip on the real tape under SB's posted −75/+150 bracket (driving the
  REAL `_process_exit_bar` / `should_hard_exit`) + a trailing proxy, friction-charged. Pre-committed
  verdict bar: FEED-DIVERGENT (median |Δ|>10pt AND >60% divergent-feed) / GATE-INERT (>60%
  gate-failed-open, small Δ) / LEAD-REAL otherwise. **Verdict (2026-06-09): LEAD-REAL** — SB's
  prints are NOT phantom (roll-aware median |Δ| ~14pt, UNBIASED once the early-June→Sept contract
  roll is resolved by attributing each entry to the front month it actually traded; 0% regime-
  flipping divergence); 81% of entries are above-trend (TF-achievable), 19% are below-trend longs
  the gate blocks — and those re-price NEGATIVE on the real tape, corroborating Phase-12
  GATE-CORRECT. **Roll finding:** SB rolls June→Sept ~8d before expiry; an un-attributed June-only
  fetch shows a spurious +200..+365pt "divergence" that is pure calendar carry (the study fetches
  BOTH MNQM6+MNQU6 and attributes per entry). **Capture limit (honest):** only 37/70 round-trips
  have a parsed exit and TF's own losing side is not in `seanbot_signals`, so this is a MECHANISM
  verdict, not a dollar audit of the operator's $9,935.98 lead. Pure logic (pairing, shift scan,
  classification, re-pricing, verdict, roll attribution) pinned in
  `tests/test_phase14_sb_reconciliation.py` on synthetic tapes warmed past the 202-bucket EMA
  threshold. Writes `/tmp/phase14_<date>.txt`. **OFFLINE research, READ-ONLY, drives NO prod path.**

- `fetch_history.py` — read-only (re)fetch of the saved 1-min history (dry-run by default).

## Fidelity

`engine.FastGateEntry` (the fast backtest driver) is proven **byte-identical** to the real
`Sma100BounceStrategy.on_new_bar` by `tests/test_eval_engine.py` (decisions *and* full-sim
trades, on synthetic and real data). It only diverges past a 7000-bar buffer, where the
windowing argument (SMA windows ≪ 7000; regime resamples the same last-7000 tail; unused
ewm columns) makes it identical anyway.

## What the kit does NOT prove

The **forward edge in the real regime**. The backtest is sample/period-dependent and
measures past data; the synthetic harness proves path behavior; the live tiers prove
plumbing. Future edge is measured forward against the §7 go/no-go bar — not patched.
