# Book principles — distilled notes + citations

Distilled, applied notes only. **No book text, no files, no long quotes** (copyright).
Each entry: the principle, how it applies to TradeFlow, and a citation to find it.

## Market microstructure & order types

- **Stop-MARKET beats stop-limit for the protective leg.** A protective stop's job is
  a *guaranteed exit*, not a guaranteed price. In a fast move a stop-limit can fail to
  fill and leave the position naked; a stop-market always exits (worse price, but it
  exits). TradeFlow's protective STP is therefore always stop-MARKET (`STP`), never
  `STP LMT`. — Harris, *Trading and Exchanges* (2003), Ch. 4 (order types / standing
  orders). Encoded as §0.5.206 and STABILIZE-5.
- **Standing orders are options the market can exercise against you.** A resting SELL
  stop with no position behind it is a free option for the market to put you short.
  Hence the NEVER-ORPHAN rule: cancel a standalone stop the moment the position closes
  by any other path. — Harris Ch. 4–5 (liquidity, standing orders).

## Risk & position management

- **Never naked.** A futures position must always carry a working protective stop;
  the unprotected window between fill and stop placement must be bounded (synchronous
  post-fill placement + a reconciler heal backstop). — general futures risk practice;
  TradeFlow §0.5.T5.
- **Ratchet one direction only.** Trail the stop toward price, never away — losing the
  in-memory high-water mark can never un-ratchet a broker-resident stop. — trailing-stop
  convention; SeanBot V3/V12 ladder (+50 lock, +200 trail, +1000 hard cap).

## Strategy evaluation

- **In-sample select, out-of-sample judge.** Promote only on OOS performance; an
  IS-only edge is curve-fit. TradeFlow's BT sweeps use IS-select / OOS-judge and a
  PF ≥ 1.20 OOS bar. — standard quant backtesting hygiene (e.g. Bailey/López de Prado
  on backtest overfitting; *Advances in Financial Machine Learning*, 2018).
- **Regime decay is real.** A negative recent OOS third is a decay signal, not noise to
  average away. — López de Prado (2018), backtest-overfitting chapters.

## Execution correctness (the founding lesson)

- **You cannot out-enter a broken exit.** When a proven strategy bleeds, suspect the
  execution layer before re-deriving the edge; diagnose from broker truth first. —
  TradeFlow 2026-06-03 session lesson (not a book; recorded here because it outranks
  theory in this project). See the `change-management` skill.

> To add an entry: state the principle in one line, the TradeFlow application, and a
> findable citation (author, work, chapter). Never paste book text.
