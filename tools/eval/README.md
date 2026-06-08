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
