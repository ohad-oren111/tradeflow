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
