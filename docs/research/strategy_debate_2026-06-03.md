# Strategy debate — 2026-06-03

## 1-page distilled header (read this; the rest is raw material)

**Context.** This debate ran during the STABILIZE arc, when the operator pushed back
hard on strategy-research detours ("he gave us the code on a silver platter — why
aren't you stabilizing?"). The conclusion that matters: **the edge was never the
problem; the exit plumbing was.** Strategy invention is SHELVED. The bot faithfully
runs SeanBot's proven pullback rule; we fix execution and optimize only in small
increments after the exit is proven over several live trades.

**What the evidence said (broker truth + backtests, not opinion):**
- The P&L bleed was an **exit-order execution bug** (OCA-grouped trailing stop →
  Error 10326 → silent cancel → re-arm at base → oversell), not the entry/strategy.
  Fixed in STABILIZE-3 (#103) and hardened in STABILIZE-5 (standalone stop).
- **No backtest candidate cleared OOS PF ≥ 1.20** across the 26-month real NQ 1-min
  sweep (BT-1..BT-5). C1_ORB at equal drawdown is ~2× the baseline's net but is a
  thin, decaying edge (recent OOS third negative — regime decay). NOT promotable.
- SeanBot's **live entries span −77..+34 pts from the 100-MA** — wider than the
  `[−15,+5]` band in the code he shared (2026-05-19). **His live bot ≠ his shared
  code.** TF agrees with only ~46% of his entries on a settled-bar replay.
- SeanBot posts **no daily summary**; capture began mid-2026-05-28, so his P&L can't
  be reconstructed from captures → the dashboard uses operator-seeded anchors.

**Decision:** Stay in the STABILIZE/REPLICATE lanes. Do NOT reopen strategy
invention. Do NOT widen the entry gate blind (over-fire trap).

## Parked hypotheses (logged, NOT acted on)

- **PARKED-H1 — Entry alignment with SeanBot.** His live rule is wider/newer than his
  shared code and currently unknown. BLOCKED on the operator getting his friend's
  current entry rule. Do not widen TF's gate until the exit is proven over several
  trades AND the rule is known.
- **PARKED-H2 — Vol-regime floor on C1_ORB (BT-3).** Every floor tested ~PF 1.05 with
  a negative recent OOS third. Regime decay suspected. Not promotable as-is; revisit
  only with a regime-robustness pre-registration.
- **PARKED-H3 — Risk/portfolio scaling (BT-5).** C1 ~2× baseline net at equal
  drawdown, but on a thin decaying edge. Not a standalone improvement.
- **PARKED-H4 — Lock-in / trail ladder tuning.** The +50 lock is the single biggest
  edge for this low-win-rate strategy (per SeanBot V3/V12). Do not touch until exit
  fidelity is measured live.

## Raw LLM transcripts

> NOTE (honesty): full verbatim transcripts of the multi-model debate were not
> captured into the repo during this session. The distilled header above is the
> load-bearing record. Append raw transcripts below when re-running the debate; keep
> them clearly separated from the distilled header so the next reader trusts the
> 1-pager first (per the change-management OBSERVE discipline).

_(append raw transcripts here)_
